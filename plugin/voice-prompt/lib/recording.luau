--!nonstrict
-- Recording owns the configure/start capture handshake so a stop issued before the helper answers
-- cannot signal a session that does not exist, and so late acknowledgements from an abandoned
-- attempt can never reopen one.
local Recording = {}
Recording.__index = Recording

local function clear(recording)
    recording.wanted, recording.allocated, recording.stopping, recording.cancelled = false, false, false, false
    recording.session_id = nil
end

function Recording.new(send, new_session_id)
    return setmetatable({ send = send, new_session_id = new_session_id, epoch = 0, wanted = false,
        allocated = false, stopping = false, cancelled = false, session_id = nil }, Recording)
end

function Recording:request(config, on_session, on_error)
    self.epoch += 1
    local epoch = self.epoch
    self.wanted, self.allocated, self.stopping, self.cancelled, self.session_id = true, false, false, false, nil
    local function live() return epoch == self.epoch and self.wanted end
    self.send("configure", config, nil, function(ack, err)
        if not live() then return end
        if err or not ack or not ack.accepted then
            clear(self)
            on_error(err or (ack and ack.error_code) or "configure_failed")
            return
        end
        local session_id = self.new_session_id()
        self.session_id, self.allocated = session_id, true
        on_session(session_id)
        self.send("start", {}, session_id, function(started, start_error)
            -- Fence on the attempt, not on the desire: a stop may have cleared `wanted` while the
            -- helper was still answering, and a rejected start must still settle the attempt so the
            -- service can leave finalizing instead of waiting for an event an idle session never emits.
            if epoch ~= self.epoch or self.session_id ~= session_id then return end
            if start_error or not started or not started.accepted then
                clear(self)
                on_error(start_error or (started and started.error_code) or "start_failed")
            end
        end)
    end)
end

-- Settle a stop/cancel request. Returns "idle" when no capture attempt exists, "pending" when the
-- handshake was abandoned before the helper allocated a session, and "signalled" when a helper
-- command was sent and the helper's own events will settle the attempt. The user's desire ends in
-- every case, so a capture that starts late can never rewind a stopped session into recording.
function Recording:end_session(cancelled, callback)
    local pending = self.wanted and not self.allocated
    self.wanted = false
    if self.allocated then
        if self.stopping or self.cancelled then return "signalled" end
        if cancelled then self.cancelled = true else self.stopping = true end
        local epoch, session_id = self.epoch, self.session_id
        self.send(cancelled and "cancel" or "stop", {}, session_id, function(ack, err)
            if self.epoch ~= epoch or self.session_id ~= session_id then return end
            if callback then callback(ack, err) end
        end)
        return "signalled"
    end
    if not pending then return "idle" end
    self.epoch += 1
    clear(self)
    return "pending"
end

-- The helper reported that it is already finishing the capture: a later stop must not signal again.
function Recording:finalizing()
    if self.allocated then self.stopping = true end
end

-- The helper's capture actually started. Only an attempt the user still wants may become recording;
-- a capture_started that races a stop must not rewind finalizing.
function Recording:capture_started()
    return self.allocated and self.wanted
end

-- The helper settled the attempt with final, cancelled or error. Only an allocated attempt is settled:
-- a late event for the previous session must not drop a configure/start handshake that is still pending.
function Recording:settle()
    if not self.allocated then return end
    clear(self)
end

-- The helper owner this attempt belonged to is gone; fence every pending acknowledgement.
function Recording:reset()
    self.epoch += 1
    clear(self)
end

return Recording
