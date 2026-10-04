--!nonstrict
local contracts = require("./lib/contracts.luau")
local helper = require("./lib/helper_client.luau")
local Document = require("./lib/document.luau")
local Intents = require("./lib/intents.luau")
local LLM = require("./lib/llm.luau")
local History = require("./lib/history.luau")
local Recording = require("./lib/recording.luau")
local Config = require("./lib/config.luau")

local owner = nil
local runtime = (noctalia.getenv("XDG_RUNTIME_DIR") or "") .. "/voiceprompt"
local ready, lastSeq, requestCounter, sessionCounter = false, 0, 0, 0
local initializedAt, lastHeartbeat, lastRenew = noctalia.nowMs(), 0, 0
local document = Document.new("unrecorded")
local previousDocument = nil
local state, errorCode, modelLoading, modelLoaded = "idle", nil, false, false
local committed, tentative, elapsedStart, degraded = "", "", nil, false
local queued, inFlight, busy, cursors = {}, nil, false, {}
local diagnostics, models, diagnosticsError = nil, {}, nil
local downloadProgress = nil
local diagnosticsPending, diagnosticsWanted, readinessEpoch = false, true, 0
local initializing, lastRecoveryCheck = false, 0
local processingKind = nil
local lastHistorySave = 0
local historyEnabled = false
local client = LLM.new(noctalia.http, noctalia.nowMs, noctalia.json, noctalia.httpStream)
local publish, beginRecording, perform, saveHistory

local function setting(key) return noctalia.getConfig(key) end
local function nextRequest()
    requestCounter += 1
    return (owner or "starting") .. ":r:" .. requestCounter
end

local function show(panelId)
    noctalia.runAsync({ "noctalia", "msg", "panel-open", contracts.PLUGIN_ID .. ":" .. panelId })
end
local function close(panelId)
    noctalia.runAsync({ "noctalia", "msg", "panel-close", contracts.PLUGIN_ID .. ":" .. panelId })
end
local function showEditor()
    close("hud")
    show("editor")
end

local function pump()
    if busy or #queued == 0 or not ready then return end
    local item = table.remove(queued, 1)
    busy, inFlight = true, item
    local envelope = { protocol_version = 1, owner_generation = owner,
        request_id = nextRequest(), command = item.name, payload = item.payload or {}, session_id = item.session_id }
    local accepted = helper.send(envelope, runtime, function(ack, err)
        if item.settled or envelope.owner_generation ~= owner then return end
        if inFlight == item then busy, inFlight = false, nil end
        item.settled = true
        if ack and (ack.owner_generation ~= owner or ack.request_id ~= envelope.request_id) then
            ack, err = nil, "helper_ack_invalid"
        end
        if item.callback then item.callback(ack, err) end
        pump()
    end)
    if not accepted then
        if inFlight == item then busy, inFlight = false, nil end
        if not item.settled then
            item.settled = true
            if item.callback then item.callback(nil, "helper_launch_failed") end
        end
    end
end
local function send(name, payload, session_id, callback)
    if not ready then
        if callback then callback(nil, "helper_not_ready") end
        return
    end
    if #queued >= 32 then
        if callback then callback(nil, "command_queue_full") end
        return
    end
    table.insert(queued, { name = name, payload = payload or {}, session_id = session_id, callback = callback })
    pump()
end
local history = History.new(send)
local recording = Recording.new(send, function()
    sessionCounter += 1
    return owner .. ":s:" .. sessionCounter
end)
local function markHistory()
    if not setting("history_enabled") or document.raw == nil or document.deleted then return end
    history:update(document)
    history:mark()
end

publish = function()
    noctalia.state.set("view", {
        owner_generation = owner, session_id = document.session_id,
        status = state, error_code = errorCode, model_loading = modelLoading, model_loaded = modelLoaded,
        helper_ready = ready, committed_text = committed, tentative_text = tentative, degraded = degraded,
        current = document.current, raw_available = document.raw ~= nil,
        input_generation = document.input_generation, document_version = document.document_version,
        undo_available = #document.undo > 0, proposal = document.proposal,
        processing_kind = processingKind, elapsed_ms = elapsedStart and math.max(0, noctalia.nowMs() - elapsedStart) or 0,
        diagnostics = diagnostics, diagnostics_error = diagnosticsError, models = models,
        history_enabled = setting("history_enabled"), history_records = history.records,
        activation_mode = setting("activation_mode"),
        auto_action = setting("after_transcription"),
        download_progress = downloadProgress,
        llm_configured = (setting("llm_base_url") or "") ~= "" and (setting("llm_model") or "") ~= "",
        llm_key_configured = (setting("llm_api_key") or "") ~= "",
        recording_ready = ready and diagnostics ~= nil and diagnostics.recording_ready == true,
        readiness_checking = diagnosticsPending,
    })
end

local function processingCancelled()
    if document.request then client:cancel(document.request.token) end
    document = Document.reduce(document, { kind = "cancel_request" })
    processingKind = nil
    if state == "processing" then state = "ready" end
end

saveHistory = function()
    if not setting("history_enabled") or document.raw == nil or not ready then return end
    history:update(document)
    if history:save(function(err)
        -- Only this snapshot may clear dirtiness: a newer edit keeps its own revision dirty.
        if err then diagnosticsError = err end
        lastHistorySave = noctalia.nowMs()
        publish()
    end) then
        lastHistorySave = noctalia.nowMs()
    end
end

local function captureConfig()
    return Config.capture(setting, noctalia.getenv, noctalia.expandPath)
end

local function checkReadiness()
    if diagnosticsPending then diagnosticsWanted = true; return end
    diagnosticsWanted = false
    local ok, config = pcall(captureConfig)
    if not ok then diagnostics, diagnosticsError = nil, "configuration_invalid"; publish(); return end
    local epoch = readinessEpoch
    diagnosticsPending = true
    helper.diagnosticsStream(config, function(report, err)
        diagnosticsPending = false
        if epoch ~= readinessEpoch then diagnosticsWanted = true; return end
        if report and (report.selected_device ~= config.device or report.model_profile ~= config.model_profile) then
            report, err = nil, "helper_diagnostics_invalid" end
        diagnostics, diagnosticsError = report, err
        publish()
    end)
    publish()
end

beginRecording = function()
    if not ready or state == "recording" or state == "finalizing" then return end
    processingCancelled()
    local ok, config = pcall(captureConfig)
    if not ok then state, errorCode = "error", "configuration_invalid"; publish(); showEditor(); return end
    local allocated = false
    recording:request(config, function(session_id)
        allocated = true
        previousDocument = document.raw ~= nil and document or nil
        document = Document.new(session_id)
        committed, tentative, degraded, errorCode = "", "", false, nil
        state, elapsedStart = "starting", noctalia.nowMs()
        publish(); show("hud")
    end, function(failure)
        state, errorCode = "error", failure
        if allocated and previousDocument then document = previousDocument end
        previousDocument = nil
        publish(); showEditor()
    end)
end

local function stopRecording(cancelled)
    if state ~= "starting" and state ~= "recording" and state ~= "finalizing" then return end
    local failed = false
    local outcome = recording:end_session(cancelled, function(ack, err)
        if not err and ack and ack.accepted then return end
        -- A refused stop settles synchronously when the helper is not reachable; keep its error.
        failed = true
        state, errorCode = "error", err or (ack and ack.error_code) or "stop_failed"
        publish(); showEditor()
    end)
    if failed then return end
    if outcome == "signalled" then state = "finalizing"; publish(); return end
    -- No session was allocated, so `document` was never replaced: keep the current document and drop
    -- the stale rollback snapshot. Replacing it would resurrect a previous transcription or blank
    -- the editor, and signalling the previous document's session would leave it stuck in finalizing.
    previousDocument = nil
    committed, tentative, degraded = "", "", false
    state, errorCode = document.raw ~= nil and "ready" or "idle", nil
    showEditor()
    publish()
end


local function process(kind)
    if state == "starting" or state == "recording" or state == "finalizing" or document.raw == nil or #document.current == 0 then return end
    processingCancelled()
    local token = nextRequest()
    local snapshot
    document, snapshot = Document.begin_request(document, token, kind, noctalia.nowMs())
    state, processingKind, errorCode = "processing", kind, nil
    publish()
    client:process(snapshot, { base_url = setting("llm_base_url"), model = setting("llm_model"), credential = setting("llm_api_key") or "",
        system_prompt = setting(kind == "clean" and "clean_system_prompt" or "prompt_system_prompt") }, function(result)
            if not document.request or document.request.token ~= result.token then return end
            processingKind = nil
            if result.kind == "error" then
                document = Document.reduce(document, { kind = "cancel_request" })
                state, errorCode = "error", result.code
            else
                local ok, nextDocument, disposition = pcall(Document.receive_result, document, snapshot, result.text, noctalia.nowMs())
                if ok then
                    document, state = nextDocument, "ready"
                    if disposition ~= "discarded" then markHistory() end
                else state, errorCode = "error", "processing_result_invalid" end
            end
            publish()
        end)
end

perform = function(action)
    local kind = action.kind
    if kind == "record-toggle" then
        if state == "starting" or state == "recording" then stopRecording(false)
        elseif state ~= "finalizing" then recording.wanted = true; state = "starting"; publish(); show("hud"); beginRecording() end
    elseif kind == "record-start" then
        if state ~= "finalizing" and state ~= "recording" and state ~= "starting" then recording.wanted = true; state = "starting"; publish(); show("hud"); beginRecording() end
    elseif kind == "record-stop" then stopRecording(false)
    elseif kind == "record-cancel" then stopRecording(true)
    elseif kind == "show-editor" then
        if state == "starting" or state == "recording" or state == "finalizing" then show("hud") else showEditor() end
    elseif kind == "copy" then
        local text = (state == "starting" or state == "recording" or state == "finalizing") and (committed .. (committed ~= "" and tentative ~= "" and " " or "") .. tentative) or document.current
        if #text > 0 then
            if noctalia.copyToClipboard(text, "text/plain") then
                noctalia.notify("VoicePrompt", noctalia.tr("ui.copied"))
                if setting("close_after_copy") and state ~= "recording" and state ~= "finalizing" then close("editor") end
            else errorCode = "copy_failed" end
        end
    elseif kind == "clean" or kind == "prompt" or kind == "retry" then process(kind == "retry" and (action.processing_kind or "clean") or kind)
    elseif kind == "close" then saveHistory(); close("editor")
    elseif kind == "clear" or kind == "undo" or kind == "restore" or kind == "apply_proposal" or kind == "discard_proposal" then
        if state ~= "starting" and state ~= "recording" and state ~= "finalizing" then
            if kind ~= "apply_proposal" and kind ~= "discard_proposal" then processingCancelled() end
            document = Document.reduce(document, { kind = kind })
            state, errorCode = "ready", nil
            markHistory()
        end
    elseif kind == "delete" then
        stopRecording(true); processingCancelled()
        local deletedSession = document.session_id
        document = Document.reduce(document, { kind = "delete" })
        if setting("history_enabled") then history:delete(deletedSession, function() publish() end) end
        send("unload", {})
        close("editor"); state = "idle"
        document = Document.new(owner .. ":empty:" .. nextRequest())
    elseif kind == "refresh-models" then
        client:refresh_models(setting("llm_base_url"), setting("llm_api_key") or "", function(result)
            if result.kind == "success" then models, diagnosticsError = result.models, nil else diagnosticsError = result.code end
            publish()
        end)
    elseif kind == "diagnostics" then
        checkReadiness()
    elseif kind == "download-model" then
        send("model_download", { profile = setting("model_profile") }, nil, function(ack, err)
            if err or not ack or not ack.accepted then diagnosticsError = err or (ack and ack.error_code) or "model_download_failed" end
            publish()
        end)
    elseif kind == "cancel-download" then send("model_download_cancel", {})
    elseif kind == "clear-history" then history:clear(function(err) diagnosticsError = err; publish() end)
    elseif kind == "open-history" then
        for _, record in ipairs(history.records) do
            if record.session_id == action.record_id then
                processingCancelled()
                document = Document.new(record.session_id)
                document = Document.reduce(document, { kind = "final", text = record.raw })
                document = Document.reduce(document, { kind = "replace", text = record.current })
                state = "ready"; showEditor(); break
            end
        end
    end
    publish()
end

local function consume(outbox)
    if type(outbox) ~= "table" or outbox.owner_generation ~= owner or type(outbox.publisher_id) ~= "string" then return end
    local cursor = cursors[outbox.publisher_id] or { publisher_id = outbox.publisher_id, last_seq = 0 }
    local ok, actions = pcall(Intents.consume, outbox, cursor.last_seq)
    if not ok then errorCode = "intent_sequence_gap"; publish(); return end
    for _, action in ipairs(actions) do
        if action.kind == "edit" then
            local nextDocument, nextCursor, accepted, rejection = Intents.apply_editor_edit(document, action, cursor, Document.reduce)
            document, cursor = nextDocument, nextCursor
            if accepted then
                errorCode = nil
                markHistory()
            elseif rejection then
                -- The intent is consumed even though it was refused, so the publisher drops it and
                -- later valid edits and toolbar actions still reach the document.
                errorCode = rejection
            end
        else
            cursor.last_seq = action.seq
            if action.kind == "record-toggle" or action.kind == "show-editor" or action.session_id == document.session_id then
                perform(action)
            end
        end
    end
    cursors[outbox.publisher_id] = cursor
    noctalia.state.set("ack." .. outbox.publisher_id, cursor.last_seq)
    publish()
end
noctalia.state.watch("intent.editor", consume)
noctalia.state.watch("intent.hud", consume)
noctalia.state.watch("intent.widget", consume)

local function onEvent(line)
    if #line > 262144 then return end
    local event = noctalia.json.decode(line)
    if type(event) ~= "table" or event.protocol_version ~= 1 or event.owner_generation ~= owner
        or type(event.seq) ~= "number" or event.seq <= lastSeq then return end
    lastSeq, lastHeartbeat = event.seq, noctalia.nowMs()
    if event.event == "ready" then
        if event.protocol_min > 1 or event.protocol_max < 1 or event.config_schema ~= 1 then
            state, errorCode = "error", "helper_incompatible"; publish(); return
        end
        ready = true; pump()
        initializing = false
        diagnosticsWanted = true
        local ok, config = pcall(captureConfig)
        if not ok then state, errorCode = "error", "configuration_invalid"; publish(); return end
        send("configure", config, nil, function(ack, err)
            if err or not ack or not ack.accepted then
                state, errorCode = "error", err or (ack and ack.error_code) or "configure_failed"
            elseif state == "error" then
                state, errorCode = document.raw ~= nil and "ready" or "idle", nil
            end
            publish()
        end)
        historyEnabled = setting("history_enabled") == true
        if historyEnabled then
            history:load(function(records, err)
                if err and err ~= "helper_owner_replaced" then diagnosticsError = err end
                publish()
            end)
        end
        if recording.wanted then beginRecording() end
    elseif event.event == "worker_unloaded" then modelLoaded, modelLoading = false, false
    elseif event.event == "model_download_progress" then downloadProgress = event
    elseif event.event == "model_download_complete" then diagnosticsError = nil; downloadProgress = event; diagnosticsWanted = true
    elseif event.event == "model_download_error" then diagnosticsError = event.error_code
    elseif event.event == "hotkey_pressed" then perform({ kind = "record-start" })
    elseif event.event == "hotkey_released" then perform({ kind = "record-stop" })
    elseif event.event == "input_unavailable" then diagnosticsError = event.error_code
    elseif event.session_id == document.session_id then
        if event.event == "capture_started" then if recording:capture_started() then state = "recording" end
        elseif event.event == "model_loading" then modelLoading = true
        elseif event.event == "model_ready" then modelLoading, modelLoaded = false, true
        elseif event.event == "partial" then
            if type(event.committed_text) == "string" and type(event.tentative_text) == "string" then
                committed, tentative, degraded = event.committed_text, event.tentative_text, event.degraded or false
            end
        elseif event.event == "finalizing" then state = "finalizing"; recording:finalizing()
        elseif event.event == "cancelled" then
            recording:settle()
            document = previousDocument or Document.new(owner .. ":empty:" .. nextRequest())
            previousDocument = nil
            state, elapsedStart = document.raw ~= nil and "ready" or "idle", nil
            showEditor()
        elseif event.event == "final" and type(event.text) == "string" then
            recording:settle()
            document = Document.reduce(document, { kind = "final", text = event.text })
            previousDocument = nil
            state, elapsedStart = "ready", nil
            if not event.speech_detected then errorCode = "speech_not_detected" end
            showEditor(); markHistory(); saveHistory()
            local automatic = setting("after_transcription")
            if #document.current > 0 and automatic ~= "show" then perform({ kind = automatic }) end
        elseif event.event == "error" then
            recording:settle()
            state, errorCode, elapsedStart = "error", event.error_code, nil
            if document.raw == nil and previousDocument then document = previousDocument end
            previousDocument = nil
            showEditor()
        end
    elseif event.event == "error" then state, errorCode = "error", event.error_code end
    publish()
end

local function initialize()
    if initializing then return end
    initializing, initializedAt = true, noctalia.nowMs()
    helper.identity(function(generation, err)
        if not generation then initializing = false; state, errorCode = "error", err; publish(); return end
        owner = generation
        if document.raw == nil and document.current == "" then document = Document.new(owner .. ":empty") end
        lastSeq, lastHeartbeat, lastRenew = 0, 0, 0
        local abandoned = queued
        queued, busy = {}, false
        if inFlight then
            table.insert(abandoned, inFlight)
            inFlight = nil
        end
        -- The previous owner can no longer answer: settle its transactions so the recovered helper
        -- can retry dirty history and the UI is not left waiting on abandoned callbacks.
        history:replace_owner()
        recording:reset()
        for _, item in ipairs(abandoned) do
            if item.callback and not item.settled then
                item.settled = true
                item.callback(nil, "helper_owner_replaced")
            end
        end
        local accepted, launchError = helper.launch(owner, runtime, onEvent)
        if not accepted then initializing = false; state, errorCode = "error", launchError or "helper_launch_failed" end
        publish()
    end)
end
noctalia.setUpdateInterval(500)
function update()
    client:tick()
    local now = noctalia.nowMs()
    if not ready and not initializing and now - lastRecoveryCheck >= 3000 then
        lastRecoveryCheck = now
        if helper.resolve() then initialize() end
    end
    if diagnosticsWanted and not diagnosticsPending then checkReadiness() end
    if ready and now - lastRenew >= 2000 then
        lastRenew = now
        helper.send({ protocol_version = 1, request_id = nextRequest(), owner_generation = owner,
            command = "renew_lease", payload = {} }, runtime, function(ack, err)
            if err or not ack or not ack.accepted then ready, state, errorCode = false, "error", err or "helper_lease_failed"; publish() end
        end)
    end
    if not ready and now - initializedAt >= 10000 then initializing = false; if state ~= "error" then state, errorCode = "error", "helper_startup_timeout"; publish() end end
    if ready and lastHeartbeat > 0 and now - lastHeartbeat >= 10000 then ready, state, errorCode = false, "error", "helper_disconnected" end
    if setting("history_enabled") and history:dirty() and now - lastHistorySave >= 2000 then saveHistory() end
    if state == "recording" or state == "starting" or state == "finalizing" then publish() end
end
function onIpc(event, payload)
    if event == "record-start" or event == "record-stop" or event == "record-toggle" or event == "record-cancel" or event == "show-editor" or event == "clear" then
        perform({ kind = event })
    elseif event == "diagnostics" or event == "download-model" or event == "cancel-download" then
        perform({ kind = event })
    elseif event == "install-helper" then
        local accepted, err = helper.install()
        if not accepted then errorCode = err; publish() end
    end
end
function onConfigChanged()
    processingCancelled()
    readinessEpoch += 1
    diagnostics, diagnosticsError, diagnosticsWanted = nil, nil, true
    local enabled = setting("history_enabled") == true
    if enabled and not historyEnabled and ready then
        -- Records written while history was off must be reconciled before a save can replace them.
        history:load(function(records, err)
            if err and err ~= "helper_owner_replaced" then diagnosticsError = err end
            publish()
        end)
    end
    historyEnabled = enabled
    if ready and state ~= "starting" and state ~= "recording" and state ~= "finalizing" then
        local ok, config = pcall(captureConfig)
        if ok then send("configure", config) else errorCode = "configuration_invalid" end
    end
    publish()
end
function onExit(signal, reason)
    if owner then helper.send({ protocol_version = 1, request_id = nextRequest(), owner_generation = owner,
        command = "shutdown", payload = {} }, runtime, nil) end
end
publish()
initialize()
