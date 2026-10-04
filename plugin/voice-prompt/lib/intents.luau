--!nonstrict
local Intents = {}
local Publisher = {}
Publisher.__index = Publisher

local function snapshot(self)
    local actions = {}
    for _, action in ipairs(self.actions) do table.insert(actions, table.clone(action)) end
    return { publisher_id = self.publisher_id, actions = actions }
end

function Intents.new(publisher_id)
    assert(type(publisher_id) == "string" and publisher_id ~= "", "invalid_publisher")
    return setmetatable({ publisher_id = publisher_id, actions = {}, next_seq = 1,
        published = 0, acknowledged = 0 }, Publisher)
end

function Publisher:enqueue(action)
    local next = table.clone(action)
    if next.kind == "edit" then
        local count = next.edit_count or 1
        assert(type(count) == "number" and count >= 1 and count == math.floor(count), "invalid_edit_count")
        next.edit_count = count
        local last = self.actions[#self.actions]
        if last and last.kind == "edit" and last.seq > self.published
            and last.session_id == next.session_id and last.input_generation == next.input_generation then
            last.text = next.text
            last.edit_count += count
            return snapshot(self)
        end
    end
    if #self.actions >= 32 then error("intent_queue_full") end
    next.seq = self.next_seq
    self.next_seq += 1
    table.insert(self.actions, next)
    return snapshot(self)
end

function Publisher:mark_published(last_seq)
    assert(type(last_seq) == "number" and last_seq >= 0 and last_seq < self.next_seq, "invalid_publish_seq")
    self.published = math.max(self.published, last_seq)
end

function Publisher:ack(last_seq)
    assert(type(last_seq) == "number" and last_seq >= 0 and last_seq <= self.published, "invalid_ack")
    if last_seq <= self.acknowledged then return end
    self.acknowledged = last_seq
    local kept = {}
    for _, action in ipairs(self.actions) do
        if action.seq > last_seq then table.insert(kept, action) end
    end
    self.actions = kept
end

function Intents.consume(outbox, last_seen)
    assert(type(outbox) == "table" and type(outbox.publisher_id) == "string"
        and type(outbox.actions) == "table" and #outbox.actions <= 32, "invalid_outbox")
    local result = {}
    local previous = last_seen
    for _, action in ipairs(outbox.actions) do
        assert(type(action.seq) == "number" and action.seq >= 1 and action.seq == math.floor(action.seq), "invalid_intent_seq")
        if action.seq > previous then
            if action.seq ~= previous + 1 then error("intent_sequence_gap") end
            table.insert(result, table.clone(action))
            previous = action.seq
        end
    end
    return result, previous
end

local function rejection_code(message)
    if type(message) == "table" and type(message.code) == "string" then return message.code end
    if type(message) == "string" then
        -- The reducer reports failures as messages like "document.luau:8: text_too_large".
        local code = string.match(message, "([%a_][%w_]*)$")
        if code then return code end
    end
    return "edit_rejected"
end

function Intents.apply_editor_edit(state, action, cursor, reduce_document)
    local next_cursor = table.clone(cursor)
    if action.seq <= cursor.last_seq then return state, next_cursor, false end
    if action.seq ~= cursor.last_seq + 1 then error("intent_sequence_gap") end
    next_cursor.last_seq = action.seq
    if action.kind ~= "edit" or action.session_id ~= state.session_id
        or action.input_generation ~= state.input_generation or state.deleted then
        return state, next_cursor, false
    end
    if action.publisher_id and action.publisher_id ~= cursor.publisher_id then
        return state, next_cursor, false
    end
    local ok, next = pcall(reduce_document, state, { kind = "edit", text = action.text, edit_count = action.edit_count })
    if not ok then
        -- A rejected edit is still consumed: the cursor advances so the publisher can drop it instead
        -- of replaying it ahead of every later edit and toolbar action.
        return state, next_cursor, false, rejection_code(next)
    end
    return next, next_cursor, true
end

return Intents
