--!nonstrict
-- History owns the persistence transaction: one helper command runs at a time, and each snapshot
-- is captured after preceding mutations. It also tracks which revision has reached disk so the
-- owner only clears dirtiness for the snapshot that actually succeeded.
local History = {}
History.__index = History

local function is_record(record)
    return type(record) == "table" and type(record.session_id) == "string" and record.session_id ~= ""
        and type(record.raw) == "string" and type(record.current) == "string"
        and type(record.timestamp) == "number"
end

-- Records captured in memory win over the file contents: a save resumed after an owner replacement
-- must not drop the record the user created while the old owner was gone.
local function reconcile(current, loaded)
    local merged, position = {}, {}
    for _, record in ipairs(loaded) do
        if is_record(record) then
            merged[#merged + 1] = record
            position[record.session_id] = #merged
        end
    end
    for _, record in ipairs(current) do
        if is_record(record) then
            local index = position[record.session_id]
            if index then merged[index] = record else merged[#merged + 1] = record end
        end
    end
    while #merged > 10 do table.remove(merged, 1) end
    return merged
end

function History.new(send, io)
    local source = io or {}
    local dir, err = noctalia.pluginDataDir()
    return setmetatable({ send = send,
        root = dir and (dir .. "/voiceprompt-private") or nil,
        records = {}, error = err, revision = 0, saved = 0,
        loaded = false, loading = false, load_waiters = {},
        operations = {}, running = nil, busy = false,
        clock = source.clock or noctalia.nowMs,
        read = source.read or noctalia.readFile,
        write = source.write or noctalia.writeFile,
        json = source.json or noctalia.json }, History)
end

function History:_pump()
    if self.running or #self.operations == 0 then return end
    local operation = table.remove(self.operations, 1)
    self.running = operation
    operation.action(function(err)
        if operation.abandoned or operation.finished then return end
        operation.finished = true
        if self.running == operation then self.running = nil end
        local callback = operation.callback
        operation.callback = nil
        if callback then callback(err) end
        self:_pump()
    end, operation)
end

function History:_schedule(callback, action)
    table.insert(self.operations, { callback = callback, action = action })
    self:_pump()
end

function History:load(callback)
    if not self.root then
        if callback then callback(nil, self.error or "data_directory_unavailable") end
        return
    end
    if self.loaded then
        if callback then callback(self.records, nil) end
        return
    end
    if self.loading then
        if callback then table.insert(self.load_waiters, callback) end
        return
    end
    self.loading = true
    local waiters = self.load_waiters
    self:_schedule(function(err)
        -- Detach this load before callbacks can start or coalesce a replacement load. Owner reset
        -- already detaches the list, but this closure still owes every caller in the old list.
        if self.load_waiters == waiters then
            self.loading = false
            self.load_waiters = {}
        end
        local records = nil
        if not err then records = self.records end
        if callback then callback(records, err) end
        for _, waiter in ipairs(waiters) do waiter(records, err) end
    end, function(done, operation)
        self.send("history_load", { root = self.root }, nil, function(ack, err)
            if operation.abandoned then return end
            if err or not ack or not ack.accepted then
                done(err or (ack and ack.error_code) or "history_load_failed")
                return
            end
            local content = self.read(ack.path)
            local value = content and self.json.decode(content)
            self.send("discard_private_file", { token = ack.token })
            if type(value) ~= "table" or value.schema ~= 1 or type(value.records) ~= "table" then
                done("history_malformed")
                return
            end
            self.records = reconcile(self.records, value.records)
            self.loaded = true
            done(nil)
        end)
    end)
end

function History:update(document)
    if document.raw == nil or document.deleted then return end
    local found = false
    for _, record in ipairs(self.records) do
        if record.session_id == document.session_id then
            record.raw, record.current = document.raw, document.current
            found = true
            break
        end
    end
    if not found then
        table.insert(self.records, { session_id = document.session_id, raw = document.raw,
            current = document.current, timestamp = self.clock() })
    end
    while #self.records > 10 do table.remove(self.records, 1) end
end

function History:mark()
    self.revision += 1
end

function History:dirty()
    return self.root ~= nil and self.loaded and self.revision > self.saved
end

-- Returns true when the snapshot was queued; the callback then settles with nil or an error.
-- Returns false when nothing was queued: a save is already running (the caller keeps its dirtiness
-- and retries) or the history file was not read yet (the callback reports history_not_loaded).
function History:save(callback)
    if self.busy then return false end
    local revision
    if not self.loaded then
        if callback then callback("history_not_loaded") end
        return false
    end
    if self.revision <= self.saved then return false end
    self.busy = true
    self:_schedule(function(err)
        self.busy = false
        if not err then self.saved = math.max(self.saved, revision) end
        if callback then callback(err) end
    end, function(done, operation)
        revision = self.revision
        local encoded = self.json.encode({ schema = 1, records = self.records })
        self.send("prepare_private_file", { purpose = "history", max_bytes = 8388608 }, nil, function(ack, err)
            if operation.abandoned then return end
            if err or not ack or not ack.accepted then
                done(err or (ack and ack.error_code) or "history_save_failed")
                return
            end
            if not self.write(ack.path, encoded) then
                self.send("discard_private_file", { token = ack.token })
                done("history_write_failed")
                return
            end
            self.send("history_save", { root = self.root, token = ack.token }, nil, function(saved, save_error)
                if operation.abandoned then return end
                done(save_error or (saved and not saved.accepted and saved.error_code) or nil)
            end)
        end)
    end)
    return true
end

function History:delete(session_id, callback)
    self:_schedule(callback, function(done, operation)
        local kept = {}
        for _, record in ipairs(self.records) do
            if record.session_id ~= session_id then table.insert(kept, record) end
        end
        self.records = kept
        self.send("history_delete", { root = self.root, session_id = session_id }, nil, function(ack, err)
            if operation.abandoned then return end
            local failure = err or (ack and not ack.accepted and ack.error_code) or nil
            if failure then self:mark() end
            done(failure)
        end)
    end)
end

function History:clear(callback)
    self:_schedule(callback, function(done, operation)
        self.records = {}
        self.send("history_clear", { root = self.root }, nil, function(ack, err)
            if operation.abandoned then return end
            local failure = err or (ack and not ack.accepted and ack.error_code) or nil
            if failure then self:mark() end
            done(failure)
        end)
    end)
end

-- The helper owner that owed these acknowledgements is gone: settle every pending transaction,
-- release the save lock and let the new owner re-persist whatever was never acknowledged.
function History:replace_owner()
    self.loaded, self.loading, self.load_waiters = false, false, {}
    self.busy, self.saved = false, 0
    local running = self.running
    self.running = nil
    local operations = self.operations
    self.operations = {}
    if running then table.insert(operations, 1, running) end
    for _, operation in ipairs(operations) do
        operation.abandoned, operation.finished = true, true
        local callback = operation.callback
        operation.callback = nil
        if callback then callback("helper_owner_replaced") end
    end
end

return History
