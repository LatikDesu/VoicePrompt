--!nonstrict
local Intents = require("./lib/intents.luau")
local Document = require("./lib/document.luau")
local contracts = require("./lib/contracts.luau")
local view = noctalia.state.get("view") or {}
local text = view.current or ""
local publisher = nil
local setup = false
local menuOpen = false
local localError = nil
local microphoneName = nil
local microphoneUnavailable = false
local microphonePending = false
local function tr(key) return noctalia.tr("ui." .. key) end
local function emit(kind, extra)
    if not publisher or not view.owner_generation then return false end
    local action = { kind = kind, session_id = view.session_id, input_generation = view.input_generation,
        document_version = view.document_version }
    for key, value in pairs(extra or {}) do action[key] = value end
    local ok, outbox = pcall(publisher.enqueue, publisher, action)
    if not ok then localError = "intent_queue_full"; return false end
    if #outbox.actions > 0 then publisher:mark_published(outbox.actions[#outbox.actions].seq) end
    outbox.owner_generation = view.owner_generation
    noctalia.state.set("intent.editor", outbox)
    return true
end
local function initializePublisher()
    if not view.owner_generation then return end
    local id = "editor:" .. view.owner_generation .. ":" .. tostring(noctalia.nowMs())
    local instance = Intents.new(id)
    publisher = instance
    noctalia.state.watch("ack." .. id, function(seq) instance:ack(seq) end)
end
local render
local function refreshMicrophone()
    if microphonePending then return end
    microphonePending = true
    local accepted = noctalia.runAsync({ "wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@" }, function(result)
        microphonePending = false
        local output = result.stdout or ""
        if result.exitCode == 0 then
            microphoneName = string.match(output, 'node%.nick%s*=%s*"([^"]+)"')
                or string.match(output, 'node%.description%s*=%s*"([^"]+)"')
                or string.match(output, 'node%.name%s*=%s*"([^"]+)"')
        else
            microphoneName = nil
        end
        microphoneUnavailable = microphoneName == nil
        render()
    end)
    if not accepted then
        microphonePending, microphoneUnavailable, microphoneName = false, true, nil
    end
end
local function openMicrophoneSettings()
    noctalia.runAsync({ "noctalia", "msg", "panel-open", "control-center", "audio" })
end
local function icon(key, glyph, enabled, callback, primary)
    local active = enabled ~= false
    return ui.row({ width = 36, height = 36, radius = 18,
        align = "center", justify = "center",
        fill = primary and "primary" or "surface_variant",
        opacity = active and 1 or 0.4, tooltip = tr(key),
        onClick = active and callback or nil }, {
        ui.glyph({ name = glyph, size = 18, color = primary and "on_primary" or "on_surface" }),
    })
end
local function button(key, kind, enabled)
    return ui.button({ text = tr(key), enabled = enabled ~= false,
        controlSize = "sm", onClick = function() emit(kind) end })
end
local function recovery(key, event, enabled)
    return ui.button({ text = tr(key), enabled = enabled ~= false, controlSize = "sm",
        onClick = function()
            noctalia.runAsync({ "noctalia", "msg", "plugin", contracts.PLUGIN_ID .. ":service", "all", event })
        end })
end

local function setupTree()
    local report = view.diagnostics
    local diagnosticText = tr("diagnostics_empty")
    if report then
        local lines = {
            tr("check_helper") .. ": " .. tostring(report.helper_version or tr("check_unavailable")),
            tr("check_pipewire") .. ": " .. ((report.pipewire and report.pipewire.server_reachable) and tr("check_ready") or tr("check_unavailable")),
            tr("check_cpu") .. ": " .. ((report.cpu and report.cpu.runtime_available) and tr("check_ready") or tr("check_unavailable")),
        }
        table.insert(lines, tr("check_vad") .. ": " .. ((report.vad and report.vad.runtime_available) and tr("check_ready") or tr("check_unavailable")))
        table.insert(lines, tr("check_model") .. ": " .. ((report.model and report.model.available) and tr("check_ready") or tr("check_unavailable")))
        for _, code in ipairs(report.error_codes or {}) do
            local key = "ui.error_" .. code
            local explanation = noctalia.tr(key)
            table.insert(lines, explanation == key and code or explanation)
        end
        if report.selected_device == "cuda" then
            local cuda = report.cuda
            if cuda == nil then
                table.insert(lines, "CUDA: " .. tr("check_not_checked"))
            elseif cuda.runtime_available then
                table.insert(lines, "CUDA: " .. tr("check_ready") .. " (int8_float16)")
                table.insert(lines, tr("check_gpu_count") .. ": " .. tostring(cuda.gpu_count))
                table.insert(lines, tr("cuda_model_separate"))
            else
                table.insert(lines, "CUDA: " .. tr("cuda_unavailable"))
                if cuda.missing_libraries and #cuda.missing_libraries > 0 then
                    table.insert(lines, tr("cuda_missing_libraries") .. ": " .. table.concat(cuda.missing_libraries, ", "))
                    table.insert(lines, tr("cuda_install_hint"))
                else
                    table.insert(lines, tr("cuda_driver_hint"))
                end
            end
        end
        diagnosticText = table.concat(lines, "\n")
    end
    local modelText = #((view.models) or {}) > 0 and table.concat(view.models, "\n") or tr("manual_model")
    local diagnosticsErrorText = view.diagnostics_error or ""
    if diagnosticsErrorText ~= "" then
        local key = "ui.error_" .. diagnosticsErrorText
        local explanation = noctalia.tr(key)
        if explanation ~= key then diagnosticsErrorText = explanation end
    end
    local historyRows = {}
    for _, record in ipairs(view.history_records or {}) do
        table.insert(historyRows, ui.button({ key = record.session_id, text = string.sub(record.current, 1, 100),
            onClick = function() setup = false; emit("open-history", { record_id = record.session_id }); render() end }))
    end
    return ui.column({ gap = 10 }, {
        ui.label({ text = tr(view.recording_ready and "recording_ready" or "recording_not_ready"), fontWeight = "bold" }),
        ui.label({ text = diagnosticsErrorText, color = "error", maxWidth = 480, maxLines = 3 }),
        ui.row({ gap = 8 }, {
            recovery("diagnostics", "diagnostics"),
            recovery("install_helper", "install-helper", not view.helper_ready),
        }),
        ui.row({ gap = 8 }, {
            button("download_model", "download-model", view.helper_ready),
            button("cancel_download", "cancel-download", view.helper_ready),
        }),
        ui.label({ text = view.download_progress and (tr("download_progress") .. ": "
            .. tostring(view.download_progress.downloaded_bytes or 0) .. "/" .. tostring(view.download_progress.total_bytes or "?")) or "",
            visible = view.download_progress and view.download_progress.event == "model_download_progress", maxLines = 2 }),
        ui.row({ gap = 8 }, {
            button("refresh_models", "refresh-models", view.helper_ready and view.llm_configured),
        }),
        ui.label({ text = tr("hold_warning"), maxWidth = 480, maxLines = 4,
            color = "on_surface/0.65", visible = view.activation_mode ~= "toggle" }),
        ui.label({ text = diagnosticText, maxWidth = 480, fontSize = 13 }),
        ui.label({ text = tr(view.llm_configured and "llm_parameters_set" or "llm_not_connected"), color = "on_surface/0.65" }),
        ui.label({ text = tr("shortcut_external"), color = "on_surface/0.65", maxLines = 2 }),
        ui.label({ text = tr("microphone_label") .. ": " .. (microphoneName
            or tr(microphoneUnavailable and "microphone_unavailable" or "microphone_loading")),
            fontWeight = "bold", maxWidth = 480, maxLines = 2 }),
        ui.button({ text = tr("microphone_settings"), glyph = "microphone", onClick = openMicrophoneSettings }),
        ui.label({ text = tr("microphone_settings_hint"), maxWidth = 480, maxLines = 3, color = "on_surface/0.65" }),
        ui.label({ text = tr("setup_description"), maxWidth = 480, maxLines = 4 }),
        ui.label({ text = modelText, maxWidth = 480, maxLines = 8, fontSize = 12 }),
        ui.column({ gap = 6 }, historyRows),
        button("clear_history", "clear-history", view.helper_ready),
    })
end
render = function()
    local editable = view.status ~= "starting" and view.status ~= "recording" and view.status ~= "finalizing"
    local processingAllowed = editable and view.raw_available and #text > 0
    local showSetup = setup
    local children = {}
    if showSetup then
        table.insert(children, ui.scroll({ flexGrow = 1 }, { setupTree() }))
    else
        table.insert(children, ui.input({
            key = (view.session_id or "empty") .. ":" .. tostring(view.input_generation or 0),
            value = text, multiline = true, frameVisible = false, flexGrow = 1, enabled = editable,
            onChange = function(value)
                if value ~= text then
                    if not Document.accepts(value) then
                        localError = "text_too_large"
                        render()
                        return
                    end
                    localError = nil
                    text = value
                    emit("edit", { text = value, edit_count = 1 })
                    render()
                end
            end,
        }))
    end
    if menuOpen and not showSetup then
        table.insert(children, ui.column({ gap = 4 }, {
            ui.button({ text = tr("restore"), enabled = editable and view.raw_available, controlSize = "sm",
                onClick = function() menuOpen = false; emit("restore"); render() end }),
            ui.button({ text = tr("clear"), enabled = editable, controlSize = "sm",
                onClick = function() menuOpen = false; emit("clear"); render() end }),
            ui.button({ text = tr("delete"), enabled = editable, variant = "ghost", controlSize = "sm",
                onClick = function() menuOpen = false; emit("delete") end }),
            ui.button({ text = tr("microphone_label"), glyph = "microphone", controlSize = "sm",
                onClick = openMicrophoneSettings }),
            ui.button({ text = tr("settings"), glyph = "settings", controlSize = "sm",
                onClick = function() menuOpen = false; noctalia.openSettings() end }),
            ui.button({ text = tr("diagnostics"), controlSize = "sm",
                onClick = function() menuOpen = false; setup = true; refreshMicrophone(); render() end }),
        }))
    end
    local actions = {}
    if showSetup then
        table.insert(actions, icon("editor", "arrow-left", true, function() setup = false; render() end))
    else
        table.insert(actions, icon("record", "microphone", view.helper_ready and (view.recording_ready or not editable) and view.status ~= "finalizing", function() emit("record-toggle") end))
        table.insert(actions, icon("copy", "copy", #text > 0, function() emit("copy") end, true))
        table.insert(actions, icon("clean_hint", "brush", processingAllowed, function() emit("clean") end))
        table.insert(actions, icon("prompt_hint", "file-pencil", processingAllowed, function() emit("prompt") end))
        if editable and view.undo_available then
            table.insert(actions, icon("undo", "arrow-back-up", true, function() emit("undo") end))
        end
        if view.proposal then
            table.insert(actions, icon("apply", "check", true, function() emit("apply_proposal") end))
            table.insert(actions, icon("discard", "x", true, function() emit("discard_proposal") end))
        end
    end
    table.insert(actions, ui.spacer({ flexGrow = 1 }))
    if view.error_code or localError then
        local code = tostring(view.error_code or localError)
        local key = "ui.error_" .. code
        local explanation = noctalia.tr(key)
        if explanation == key then explanation = code end
        table.insert(actions, ui.row({ width = 24, height = 36, align = "center", justify = "center",
            tooltip = explanation }, {
            ui.glyph({ name = "alert-circle", size = 18, color = "error" }),
        }))
    elseif view.status == "processing" then
        table.insert(actions, ui.glyph({ name = "hourglass", size = 18, color = "on_surface/0.6" }))
    end
    if not showSetup then
        table.insert(actions, icon("more", "dots", true, function() menuOpen = not menuOpen; render() end))
    end
    table.insert(children, ui.row({ gap = 6, height = 36, align = "center" }, actions))
    panel.render(ui.column({ flexGrow = 1, gap = 8, padding = 8 }, children))
end
noctalia.state.watch("view", function(value)
    local old = view
    view = value or {}
    if not publisher or old.owner_generation ~= view.owner_generation then initializePublisher() end
    if old.session_id ~= view.session_id or old.input_generation ~= view.input_generation then text = view.current or "" end
    render()
end)
function onOpen(context)
    setup, menuOpen = context == "readiness", false
    view = noctalia.state.get("view") or {}
    text = view.current or ""
    if not publisher then initializePublisher() end
    refreshMicrophone()
    render()
end
function onClose()
    setup = false
end
noctalia.setUpdateInterval(2000)
function update()
    if setup then refreshMicrophone() end
end
