--!nonstrict
-- Run each scenario in a fresh native VM. The runner injects voicepromptLoadService(host), which binds
-- the controlled host before requiring a source copy with its lifecycle functions returned at the end.
-- Real service, helper_client, Recording and other libraries run against this controlled host.
local scenario = voicepromptServiceScenario or "accepted_stop"
assert(type(voicepromptLoadService) == "function", "inject a fresh source-copy service loader")
local pending, commands, panels, values, watches, encoded = {}, {}, {}, {}, {}, {}
local now, codecCounter, eventSeq, streamCallback = 0, 0, 0, nil
local settings = { helper_path = "/fixture/helper", model_profile = "cpu-small", language = "en",
    history_enabled = false, after_transcription = "show", activation_mode = "toggle",
    preload = true, idle_unload_seconds = 120, model_load_timeout_seconds = 120,
    final_timeout_seconds = 120 }
local function encode(value)
    codecCounter += 1
    local token = "json:" .. codecCounter
    encoded[token] = value
    return token
end
local function decode(value) return encoded[value] end
local function forbidden() error("provider calls are forbidden in service regressions") end
noctalia = {
    getenv = function(key)
        if key == "XDG_RUNTIME_DIR" then return "/fixture/runtime" end
        if key == "HOME" then return "/fixture/home" end
        return nil
    end,
    getConfig = function(key) return settings[key] end,
    expandPath = function(path) return path end,
    fileExists = function(path) return path == "/fixture/helper" end,
    commandExists = function() return false end,
    nowMs = function() return now end,
    pluginDataDir = function() return "/fixture/data" end,
    readFile = function() error("unexpected history read") end,
    writeFile = function() error("unexpected history write") end,
    json = { encode = encode, decode = decode },
    http = forbidden, httpStream = forbidden,
    setUpdateInterval = function() end,
    state = {
        set = function(key, value) values[key] = value end,
        watch = function(key, callback) watches[key] = callback end,
    },
    runStream = function(_, callback) streamCallback = callback; return true end,
    runAsync = function(argv, callback)
        if argv[1] == "noctalia" then
            panels[#panels + 1] = { action = argv[3], panel = argv[4] }
        elseif argv[2] == "identity" then
            pending[#pending + 1] = { name = "identity", callback = callback }
        elseif argv[2] == "command" then
            local envelope = decode(argv[6])
            assert(type(envelope) == "table", "helper must send an encoded command envelope")
            local item = { name = envelope.command, envelope = envelope, callback = callback }
            pending[#pending + 1] = item
            commands[#commands + 1] = item
        elseif argv[2] == "setup-status" then
            callback({ exitCode = 0, stdout = encode({ recording_ready = true,
                selected_device = "cpu", model_profile = "cpu-small" }) })
        else
            error("unexpected host command " .. tostring(argv[2]))
        end
        return true
    end,
}
local function take(name)
    for index, item in ipairs(pending) do
        if item.name == name then table.remove(pending, index); return item end
    end
    error("no pending " .. name)
end
local function count(name)
    local total = 0
    for _, item in ipairs(commands) do if item.name == name then total += 1 end end
    return total
end
local function reply(item, fields)
    local ack = { protocol_version = 1, owner_generation = item.envelope.owner_generation,
        request_id = item.envelope.request_id, accepted = true }
    for key, value in pairs(fields or {}) do ack[key] = value end
    item.callback({ exitCode = 0, stdout = encode(ack) })
end
local function ack(name, fields)
    local item = take(name)
    reply(item, fields)
    return item
end
local function event(kind, fields)
    eventSeq += 1
    local value = { protocol_version = 1, owner_generation = values.view.owner_generation,
        seq = eventSeq, event = kind }
    for key, field in pairs(fields or {}) do value[key] = field end
    streamCallback(encode(value))
end
local function identify(owner)
    take("identity").callback({ exitCode = 0, stdout = encode({ owner_generation = owner }) })
    eventSeq = 0
    event("ready", { protocol_min = 1, protocol_max = 1, config_schema = 1 })
end
local function editor_opens()
    local total = 0
    for _, item in ipairs(panels) do
        if item.action == "panel-open" and item.panel == "latikdesu/voice-prompt:editor" then total += 1 end
    end
    return total
end
local service = voicepromptLoadService(noctalia)
assert(type(service) == "table" and type(service.onIpc) == "function"
    and type(service.update) == "function" and type(service.onConfigChanged) == "function"
    and type(service.onExit) == "function", "source copy must return existing lifecycle exports")
local intentCounter = 0
-- Publish one editor action through the same intent channel the real editor uses. Every call uses a
-- fresh publisher so the service cursor consumes the action exactly once.
local function editor_intent(action)
    intentCounter += 1
    local view = values.view
    local value = { seq = 1, kind = action.kind, session_id = view.session_id,
        input_generation = view.input_generation, document_version = view.document_version }
    for key, field in pairs(action) do value[key] = field end
    watches["intent.editor"]({ owner_generation = view.owner_generation,
        publisher_id = "editor:test:" .. intentCounter, actions = { value } })
end
-- Run one complete successful capture through the real configure/start/stop handshake.
local function record(text)
    service.onIpc("record-start")
    ack("configure")
    local session = ack("start").envelope.session_id
    event("capture_started", { session_id = session })
    service.onIpc("record-stop")
    ack("stop")
    event("final", { session_id = session, text = text, speech_detected = true })
    return session
end
identify("owner-1")

if scenario == "old_owner_slot" then
    -- old_owner_ack_cannot_release_new_owner_slot: the abandoned ACK arrives while replacement
    -- configure is pending; another configure must remain queued until the matching ACK arrives.
    local old = take("configure")
    now = 3000
    service.update()
    ack("renew_lease", { accepted = false, error_code = "lease_expired" })
    now = 6000
    service.update()
    identify("owner-2")
    local replacement = take("configure")
    assert(count("configure") == 2)
    reply(old)
    service.onConfigChanged()
    assert(count("configure") == 2, "a stale ACK must not release the new owner's busy slot")
    reply(replacement)
    assert(count("configure") == 3, "the matching ACK must release the queued configure")
    ack("configure")
elseif scenario == "settled_ack_slot" then
    -- settled_duplicate_ack_cannot_release_next_slot: even a repeated same-owner ACK must not
    -- release an unrelated command that was dispatched after its first completion.
    local completed = ack("configure")
    service.onConfigChanged()
    local running = take("configure")
    reply(completed)
    service.onConfigChanged()
    assert(count("configure") == 2, "a settled ACK must not release the next command's slot")
    reply(running)
    assert(count("configure") == 3)
    ack("configure")
elseif scenario == "pending_stop_preserves_document" then
    -- Stop before the configure ACK allocated no session, so it must keep the current document
    -- (final B plus its edit and Undo) instead of resurrecting the stale previousDocument (final A).
    ack("configure")
    record("Alpha")
    record("Beta")
    assert(values.view.current == "Beta")
    editor_intent({ kind = "restore" })
    editor_intent({ kind = "edit", text = "Beta edited" })
    local edited = values.view
    assert(edited.current == "Beta edited" and edited.undo_available == true)
    service.onIpc("record-toggle")
    service.onIpc("record-toggle")
    assert(values.view.status == "ready", "a pending stop settles locally back to ready")
    assert(values.view.current == "Beta edited", "a pending Stop must keep the edited document")
    assert(values.view.undo_available == true, "a pending Stop must not erase Undo")
    assert(values.view.session_id == edited.session_id and values.view.error_code == nil)
elseif scenario == "pending_stop_keeps_first_document" then
    -- The first recording leaves previousDocument nil, so a pending Stop must not blank the editor.
    ack("configure")
    record("Alpha")
    assert(values.view.current == "Alpha")
    service.onIpc("record-toggle")
    service.onIpc("record-toggle")
    assert(values.view.status == "ready" and values.view.current == "Alpha",
        "the first document must survive a pending Stop")
elseif scenario == "cancelled_attempt_clear" then
    -- A settled cancel restores the previous document and forgets it, so a later pending Stop
    -- cannot resurrect it over an edited document.
    ack("configure")
    record("Alpha")
    service.onIpc("record-start")
    ack("configure")
    local session = ack("start").envelope.session_id
    event("capture_started", { session_id = session })
    service.onIpc("record-cancel")
    ack("cancel")
    event("cancelled", { session_id = session })
    assert(values.view.status == "ready" and values.view.current == "Alpha")
    editor_intent({ kind = "edit", text = "Alpha edited" })
    service.onIpc("record-toggle")
    service.onIpc("record-toggle")
    assert(values.view.status == "ready" and values.view.current == "Alpha edited",
        "a cancelled attempt must not leave a stale previous document")
elseif scenario == "error_attempt_clear" then
    -- A settled failure after the session was allocated restores the previous document and forgets
    -- it, so a later pending Stop keeps the edited document.
    ack("configure")
    record("Alpha")
    service.onIpc("record-start")
    ack("configure")
    local session = ack("start").envelope.session_id
    event("capture_started", { session_id = session })
    service.onIpc("record-stop")
    ack("stop")
    event("error", { session_id = session, error_code = "capture_failed" })
    assert(values.view.status == "error" and values.view.current == "Alpha",
        "an allocated failure must roll the document back")
    editor_intent({ kind = "restore" })
    editor_intent({ kind = "edit", text = "Alpha edited" })
    service.onIpc("record-toggle")
    service.onIpc("record-toggle")
    assert(values.view.current == "Alpha edited",
        "an error attempt must not leave a stale previous document")
else
    ack("configure")
    service.onIpc("record-start")
    ack("configure")
    local started = ack("start")
    local session = started.envelope.session_id
    event("capture_started", { session_id = session })
    assert(values.view.status == "recording")
    service.onIpc("record-stop")
    assert(values.view.status == "finalizing")
    local before = editor_opens()
    local stop = take("stop")
    if scenario == "accepted_stop" then
        -- accepted_stop_ack_keeps_service_finalizing_until_final.
        reply(stop)
        assert(values.view.status == "finalizing" and values.view.error_code == nil,
            "an accepted Stop ACK is not a recording failure")
        assert(editor_opens() == before, "accepted Stop must not expose the editor before inference")
        event("final", { session_id = session, text = "completed transcript", speech_detected = true })
        assert(values.view.status == "ready" and values.view.current == "completed transcript")
        assert(values.view.error_code == nil, "a successful final must not retain stop_failed")
        assert(editor_opens() == before + 1)
    elseif scenario == "rejected_stop" then
        -- rejected_stop_ack_enters_failure_branch.
        reply(stop, { accepted = false, error_code = "stop_refused" })
        assert(values.view.status == "error" and values.view.error_code == "stop_refused")
        assert(editor_opens() == before + 1)
    elseif scenario == "missing_stop" then
        -- missing_stop_ack_enters_failure_branch.
        stop.callback({ exitCode = 0, stdout = "not-json" })
        assert(values.view.status == "error" and values.view.error_code == "helper_ack_invalid")
        assert(editor_opens() == before + 1)
    elseif scenario == "timeout_stop" then
        -- errored_stop_ack_enters_failure_branch.
        stop.callback({ timedOut = true, exitCode = 1, stdout = "" })
        assert(values.view.status == "error" and values.view.error_code == "helper_command_timeout")
        assert(editor_opens() == before + 1)
    else
        error("unknown service scenario " .. tostring(scenario))
    end
end
return true
