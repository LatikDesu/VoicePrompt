-- The recording handshake must never signal a capture session the helper has not allocated, must not
-- rewind a stopped attempt when a late capture or acknowledgement arrives, and must settle a rejected
-- start so the service cannot hang in finalizing.
local Recording = require("../plugin/voice-prompt/lib/recording.luau")

local function scene()
    local pending, log, counter = {}, {}, 0
    local function send(name, payload, session_id, callback)
        table.insert(log, { name = name, session_id = session_id })
        table.insert(pending, { name = name, session_id = session_id, callback = callback })
        return true
    end
    local function take(name)
        for index, item in ipairs(pending) do
            if item.name == name then
                table.remove(pending, index)
                return item
            end
        end
        error("no pending " .. name .. " command")
    end
    local function ack(name, response)
        local item = take(name)
        local reply = { accepted = true, owner_generation = "o1", request_id = "r1" }
        for key, value in pairs(response or {}) do reply[key] = value end
        if item.callback then item.callback(reply, nil) end
        return item
    end
    local function signal(name)
        for _, item in ipairs(log) do
            if item.name == name then return true end
        end
        return false
    end
    local function stopped_names()
        local names = {}
        for _, item in ipairs(pending) do
            if item.name == "stop" or item.name == "cancel" then names[#names + 1] = item.name end
        end
        return names
    end
    local recording = Recording.new(send, function()
        counter += 1
        return "session-" .. counter
    end)
    return recording, ack, signal, function() return pending end, stopped_names
end

-- Stop before the configure acknowledgement: no session exists, so nothing may be signalled and the
-- editor must be able to start again immediately.
local recording, ack, signal, pending, stopped = scene()
local session, failure = nil, nil
recording:request({ device = "cpu" }, function(id) session = id end, function(err) failure = err end)
assert(session == nil and failure == nil and not signal("start"))
assert(recording:end_session(false) == "pending", "an unallocated attempt is settled locally")
ack("configure")
assert(session == nil and failure == nil, "a late configure ack must not allocate an abandoned attempt")
assert(not signal("start") and not signal("stop") and not signal("cancel"),
    "an abandoned attempt must not signal the helper")
assert(not recording:capture_started())
recording:request({ device = "cpu" }, function(id) session = id end, function(err) failure = err end)
ack("configure")
assert(session == "session-1" and failure == nil)
ack("start")
assert(recording:capture_started())
assert(recording:end_session(false) == "signalled")
assert(ack("stop").session_id == "session-1")

-- Stop between the configure and start acknowledgements: the allocated session must be stopped once
-- and a late successful start acknowledgement must not reactivate an attempt the user has stopped.
recording, ack, signal, pending, stopped = scene()
session, failure = nil, nil
recording:request({ device = "cpu" }, function(id) session = id end, function(err) failure = err end)
ack("configure")
assert(session == "session-1")
assert(recording:end_session(false) == "signalled")
assert(not recording.wanted, "a stopped attempt no longer wants capture")
assert(ack("stop").session_id == "session-1")
ack("start")
assert(failure == nil and not recording:capture_started(),
    "a late capture must not rewind a stopped attempt into recording")
assert(#stopped() == 0, "a stopped attempt must not signal the helper again")

-- The helper already reported finalizing: a stop is acknowledged as settled and signals nothing new.
recording, ack, signal, pending, stopped = scene()
recording:request({ device = "cpu" }, function() end, function() end)
ack("configure")
ack("start")
recording:finalizing()
assert(recording:end_session(false) == "signalled")
assert(#pending() == 0 and not signal("stop"), "finalizing must not queue a second stop")

-- An attempt settled by the helper's own event must ignore a late stop acknowledgement and a late
-- rejected start acknowledgement.
recording, ack, signal, pending, stopped = scene()
local late = nil
recording:request({ device = "cpu" }, function() end, function(err) late = err end)
ack("configure")
recording:end_session(false, function() error("a settled attempt must not report its stop") end)
recording:settle()
ack("stop")
ack("start", { accepted = false, error_code = "capture_unavailable" })
assert(late == nil, "a settled attempt must not report a late rejected start")

-- A rejected start after the stop was already accepted must still settle the attempt: the helper
-- allocated no session, so an accepted idle stop emits no event and the service would hang.
recording, ack, signal, pending, stopped = scene()
local latest, start_failure = nil, nil
recording:request({ device = "cpu" }, function(id) latest = id end, function(err) start_failure = err end)
ack("configure")
assert(recording:end_session(false) == "signalled")
assert(ack("stop").session_id == "session-1")
assert(start_failure == nil)
ack("start", { accepted = false, error_code = "capture_unavailable" })
assert(start_failure == "capture_unavailable", "a rejected start must settle a stopped attempt")
assert(not recording:capture_started())
assert(recording:end_session(false) == "idle", "a settled attempt must leave the owner reusable")
recording:request({ device = "cpu" }, function(id) latest = id end, function(err) start_failure = err end)
ack("configure")
ack("start")
assert(latest == "session-2" and recording:capture_started(), "recording must work after the failure")

-- A replaced helper owner fences every pending acknowledgement and leaves the owner reusable.
recording, ack, signal, pending, stopped = scene()
session, failure = nil, nil
recording:request({ device = "cpu" }, function(id) session = id end, function(err) failure = err end)
recording:reset()
ack("configure")
assert(session == nil and failure == nil, "a replaced owner must not allocate its abandoned attempt")
assert(recording:end_session(false) == "idle")
recording:request({ device = "cpu" }, function(id) session = id end, function(err) failure = err end)
ack("configure")
assert(session == "session-1" and failure == nil, "the new owner must be able to record")

return true
