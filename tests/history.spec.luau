-- History must serialize every mutation of the persisted file, bind dirtiness to the snapshot that
-- actually reached disk, and settle abandoned transactions when the helper owner is replaced.
-- The host surface is controlled so this spec runs unchanged in the native VM and in plain Lua.
if type(noctalia) ~= "table" then
    noctalia = { pluginDataDir = function() return "/tmp/voiceprompt-history-spec" end }
end

local History = require("../plugin/voice-prompt/lib/history.luau")

local function document(session_id, raw, current)
    return { session_id = session_id, raw = raw, current = current, deleted = false }
end

local function record(session_id)
    return { session_id = session_id, raw = session_id .. " raw", current = session_id .. " current",
        timestamp = 1 }
end

local function copy_record(record)
    return { session_id = record.session_id, raw = record.raw, current = record.current,
        timestamp = record.timestamp }
end

-- A fake helper that keeps an in-memory file and only applies an effect when its command is
-- acknowledged, so every race can be driven deterministically.
local function scene()
    local pending, log, files = {}, {}, {}
    local disk, allocations, save_path = nil, 0, nil
    local history
    local function send(name, payload, session_id, callback)
        table.insert(log, name)
        table.insert(pending, { name = name, payload = payload or {}, session_id = session_id,
            callback = callback })
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
    local function reply(item, response)
        local answer = { accepted = true, owner_generation = "o1", request_id = "r1" }
        for key, value in pairs(response or {}) do answer[key] = value end
        if item.callback then item.callback(answer, nil) end
    end
    local function ack(name, response)
        local item = take(name)
        reply(item, response)
        return item
    end
    local function refuse(name, error_code)
        local item = take(name)
        if item.callback then
            item.callback({ accepted = false, error_code = error_code, owner_generation = "o1",
                request_id = "r1" }, nil)
        end
        return item
    end
    local function ack_load(records)
        allocations += 1
        local token, path = "token-" .. allocations, "/alloc/" .. allocations
        files[path] = { schema = 1, records = records }
        ack("history_load", { token = token, path = path })
        ack("discard_private_file")
    end
    -- prepare_private_file is answered with a fresh private file and its contents are consumed by the
    -- following history_save, exactly like the real helper.
    local function ack_prepare()
        allocations += 1
        local token, path = "token-" .. allocations, "/alloc/" .. allocations
        save_path = path
        ack("prepare_private_file", { token = token, path = path })
        return path
    end
    local function ack_save()
        local item = take("history_save")
        disk = files[save_path]
        reply(item, {})
    end
    local function ack_delete(session_id)
        local item = take("history_delete")
        local kept = {}
        for _, entry in ipairs(disk and disk.records or {}) do
            if entry.session_id ~= session_id then table.insert(kept, entry) end
        end
        disk = { schema = 1, records = kept }
        reply(item, {})
    end
    local function ack_clear()
        local item = take("history_clear")
        disk = { schema = 1, records = {} }
        reply(item, {})
    end
    history = History.new(send, {
        clock = function() return 1000 end,
        read = function(path) return files[path] end,
        write = function(path, value) files[path] = value; return true end,
        -- A value snapshot, like the host JSON codec, so a later edit cannot change an encoded one.
        json = {
            encode = function(value)
                local records = {}
                for _, entry in ipairs(value.records or {}) do records[#records + 1] = copy_record(entry) end
                return { schema = value.schema, records = records }
            end,
            decode = function(value) return value end,
        },
    })
    return { history = history, ack = ack, refuse = refuse, ack_load = ack_load, ack_prepare = ack_prepare,
        ack_save = ack_save, ack_delete = ack_delete, ack_clear = ack_clear, log = log,
        disk = function() return disk end, records = function() return history.records end }
end

-- A deletion issued while a save is waiting for its private file must not be followed by that
-- snapshot: the helper would otherwise restore explicitly deleted text.
local scene1 = scene()
local loaded, load_error = nil, nil
scene1.history:load(function(records, err) loaded, load_error = records, err end)
assert(load_error == nil)
scene1.ack_load({ record("a"), record("b") })
assert(#loaded == 2)
scene1.history:update(document("s1", "raw-1", "current-1"))
scene1.history:mark()
assert(scene1.history:dirty())
assert(scene1.history:save(function() end))
assert(#scene1.log == 3 and scene1.log[3] == "prepare_private_file")
scene1.history:delete("a")
assert(#scene1.log == 3, "the deletion must wait for the save that is already in flight")
scene1.ack_prepare()
assert(#scene1.log == 4 and scene1.log[4] == "history_save")
scene1.ack_save()
assert(#scene1.log == 5 and scene1.log[5] == "history_delete",
    "the snapshot must reach the helper before the deletion")
scene1.ack_delete("a")
for _, entry in ipairs(scene1.disk().records) do
    assert(entry.session_id ~= "a", "deleted history must not reappear")
end
assert(#scene1.records() == 2)

-- Clearing while a save is in flight must empty the file, not be overwritten by the earlier snapshot.
local scene2 = scene()
scene2.history:load(function() end)
scene2.ack_load({ record("a") })
scene2.history:update(document("s2", "raw-2", "current-2"))
scene2.history:mark()
scene2.history:save(function() end)
scene2.ack_prepare()
scene2.history:clear()
assert(scene2.log[#scene2.log] == "history_save", "the clear must wait for the in-flight save")
scene2.ack_save()
assert(scene2.log[#scene2.log] == "history_clear")
scene2.ack_clear()
assert(#scene2.disk().records == 0 and #scene2.records() == 0)

-- An acknowledgement for an older snapshot must not clear a newer edit's dirtiness.
local scene3 = scene()
scene3.history:load(function() end)
scene3.ack_load({})
scene3.history:update(document("s1", "raw", "first"))
scene3.history:mark()
assert(scene3.history:save(function() end))
scene3.ack_prepare()
scene3.history:update(document("s1", "raw", "second"))
scene3.history:mark()
scene3.ack_save()
assert(scene3.history:dirty(), "a newer revision must stay dirty after the older snapshot saved")
assert(scene3.history:save(function() end))
scene3.ack_prepare()
scene3.ack_save()
assert(not scene3.history:dirty())
assert(scene3.disk().records[1].current == "second", "the later edit must eventually persist")

-- Enabling history with an existing file must load it before the first save can replace it.
local scene4 = scene()
scene4.history:load(function() end)
scene4.history:update(document("s2", "raw-2", "current-2"))
scene4.history:mark()
local blocked = nil
assert(scene4.history:save(function(err) blocked = err end) == false)
assert(blocked == "history_not_loaded", "a save must settle instead of replacing an unread file")
assert(#scene4.log == 1, "no file may be written before the existing history is read")
scene4.ack_load({ record("old") })
assert(#scene4.records() == 2, "the existing records must be reconciled with the new recording")
assert(scene4.history:dirty())
assert(scene4.history:save(function() end))
scene4.ack_prepare()
scene4.ack_save()
local kept = scene4.disk().records
assert(#kept == 2 and kept[1].session_id == "old" and kept[2].session_id == "s2",
    "enabling history must not overwrite records the user never saw")

-- A malformed existing file must never be replaced by a partial save.
local scene5 = scene()
local malformed = nil
scene5.history:load(function(_, err) malformed = err end)
scene5.refuse("history_load", "history_malformed")
assert(malformed == "history_malformed")
scene5.history:update(document("s1", "raw", "current"))
scene5.history:mark()
assert(scene5.history:save(function() end) == false)
assert(#scene5.log == 1, "an unreadable existing history must be left untouched")

-- Replacing the helper owner settles the abandoned save, releases the lock and lets the new owner
-- persist the data that was never acknowledged.
local scene6 = scene()
scene6.history:load(function() end)
scene6.ack_load({})
scene6.history:update(document("s1", "raw", "current"))
scene6.history:mark()
local settled = nil
assert(scene6.history:save(function(err) settled = err end))
scene6.ack_prepare()
scene6.history:replace_owner()
assert(settled == "helper_owner_replaced", "an abandoned save must settle its caller")
assert(not scene6.history.busy, "the save lock must be released for the recovered helper")
scene6.ack("history_save")
assert(not scene6.history:dirty(), "an abandoned acknowledgement must not mark data persisted")
scene6.history:load(function() end)
scene6.ack_load({})
assert(scene6.history:dirty(), "unacknowledged records must be retried after recovery")
assert(scene6.history:save(function() end))
scene6.ack_prepare()
scene6.ack_save()
assert(not scene6.history:dirty())
assert(scene6.disk().records[1].current == "current", "recovered helpers must persist dirty history")

-- queued_clear_then_save_captures_post_mutation_snapshot_and_revision: a save behind an earlier
-- delete and clear must not resurrect records encoded while those mutations were still pending.
local scene7 = scene()
scene7.history:load(function() end)
scene7.ack_load({ record("a"), record("b") })
scene7.history:delete("a")
scene7.history:clear(function() scene7.history:mark() end)
scene7.history:update(document("new", "raw", "edited before clear"))
scene7.history:mark()
assert(scene7.history:save(function() end))
scene7.ack_delete("a")
assert(#scene7.records() == 0, "the earlier clear must run before the queued save")
scene7.ack_clear()
scene7.ack_prepare()
scene7.ack_save()
assert(#scene7.disk().records == 0, "a queued save must never restore explicitly cleared records")
assert(not scene7.history:dirty(), "the saved revision must be captured when the transaction begins")

-- failed_load_detaches_waiters_before_reentrant_retry: the first completion starts another load
-- and coalesces a new waiter; only callers of the old load may receive its failure.
local scene8 = scene()
local first_error, old_error, retry_records, retry_error, new_records, new_error
local old_calls, new_calls = 0, 0
scene8.history:load(function(_, err)
    first_error = err
    scene8.history:load(function(records, failure) retry_records, retry_error = records, failure end)
    scene8.history:load(function(records, failure)
        new_calls += 1
        new_records, new_error = records, failure
    end)
end)
scene8.history:load(function(_, err) old_calls += 1; old_error = err end)
scene8.refuse("history_load", "history_malformed")
assert(first_error == "history_malformed" and old_error == "history_malformed" and old_calls == 1)
assert(new_calls == 0, "a reentrant retry's waiter must not receive the previous load's result")
scene8.ack_load({ record("retry") })
assert(retry_error == nil and #retry_records == 1)
assert(new_calls == 1 and new_error == nil and new_records[1].session_id == "retry")

-- owner_reset_settles_coalesced_loads_without_stealing_reentrant_retry: detached old waiters
-- settle once even if the first callback immediately starts a load for the replacement owner.
local scene9 = scene()
local reset_error, old_reset_error, recovered_records, recovered_error, recovered_waiter_error
local old_reset_calls, recovered_calls = 0, 0
scene9.history:load(function(_, err)
    reset_error = err
    scene9.history:load(function(records, failure) recovered_records, recovered_error = records, failure end)
    scene9.history:load(function(_, failure) recovered_calls += 1; recovered_waiter_error = failure end)
end)
scene9.history:load(function(_, err) old_reset_calls += 1; old_reset_error = err end)
scene9.history:replace_owner()
assert(reset_error == "helper_owner_replaced")
assert(old_reset_calls == 1 and old_reset_error == "helper_owner_replaced",
    "every coalesced caller owed by the abandoned owner must settle")
assert(recovered_calls == 0 and scene9.history.loading,
    "owner replacement must leave reentrant new-owner loads pending")
scene9.ack("history_load") -- late old-owner acknowledgement is abandoned
assert(old_reset_calls == 1 and recovered_calls == 0)
scene9.ack_load({ record("recovered") })
assert(recovered_error == nil and recovered_records[1].session_id == "recovered")
assert(recovered_calls == 1 and recovered_waiter_error == nil)

return true
