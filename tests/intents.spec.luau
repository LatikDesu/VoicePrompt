local Document = require("../plugin/voice-prompt/lib/document.luau")
local Intents = require("../plugin/voice-prompt/lib/intents.luau")

local state = Document.reduce(Document.new("s1"), { kind = "final", text = "исходный" })
local base, generation = state.document_version, state.input_generation
local publisher = Intents.new("editor:g1")
publisher:enqueue({ kind = "edit", session_id = "s1", input_generation = generation,
    document_version = base, edit_count = 1, text = "первая правка" })
publisher:mark_published(1)
local outbox = publisher:enqueue({ kind = "edit", session_id = "s1", input_generation = generation,
    document_version = base, edit_count = 1, text = "вторая правка" })
publisher:mark_published(2)
local actions = Intents.consume(outbox, 0)
assert(#actions == 2)
local cursor = { publisher_id = "editor:g1", last_seq = 0 }
for _, action in ipairs(actions) do
    local accepted
    state, cursor, accepted = Intents.apply_editor_edit(state, action, cursor, Document.reduce)
    assert(accepted)
end
assert(state.current == "вторая правка" and state.document_version == base + 2)
assert(state.input_generation == generation and cursor.last_seq == 2)
assert(#Intents.consume(outbox, cursor.last_seq) == 0)

state = Document.reduce(Document.new("s2"), { kind = "final", text = "исходный" })
local snapshot
state, snapshot = Document.begin_request(state, "r2", "clean", 0)
base, generation = state.document_version, state.input_generation
publisher = Intents.new("editor:g2")
publisher:enqueue({ kind = "edit", session_id = "s2", input_generation = generation,
    document_version = base, edit_count = 1, text = "изменённый" })
outbox = publisher:enqueue({ kind = "edit", session_id = "s2", input_generation = generation,
    document_version = base, edit_count = 1, text = "исходный" })
assert(#outbox.actions == 1 and outbox.actions[1].edit_count == 2)
publisher:mark_published(outbox.actions[1].seq)
actions = Intents.consume(outbox, 0)
local accepted
state, cursor, accepted = Intents.apply_editor_edit(state, actions[1],
    { publisher_id = "editor:g2", last_seq = 0 }, Document.reduce)
assert(accepted and state.current == "исходный" and state.document_version == base + 2)
assert(state.input_generation == generation)
local received, disposition = Document.receive_result(state, snapshot, "ответ LLM", 1)
assert(disposition == "proposal" and received.current == "исходный")
assert(received.proposal.text == "ответ LLM")

-- A refused oversized edit must be acknowledged and dropped so the following edit and toolbar action
-- still reach the document instead of replaying the invalid text.
local bounded = Document.reduce(Document.new("s3"), { kind = "final", text = "исходный" })
local oversized = Intents.new("editor:g3")
oversized:enqueue({ kind = "edit", session_id = "s3", input_generation = bounded.input_generation,
    document_version = bounded.document_version, edit_count = 1, text = string.rep("я", 32769) })
oversized:mark_published(1)
oversized:enqueue({ kind = "edit", session_id = "s3", input_generation = bounded.input_generation,
    document_version = bounded.document_version, edit_count = 1, text = "уже меньше" })
oversized:mark_published(2)
local toolbar = oversized:enqueue({ kind = "record-stop", session_id = "s3" })
oversized:mark_published(toolbar.actions[#toolbar.actions].seq)
local toolbarCursor = { publisher_id = "editor:g3", last_seq = 0 }
local queuedActions = Intents.consume(toolbar, 0)
assert(#queuedActions == 3)
local refused, refusedCursor, refusedAccepted, rejection = Intents.apply_editor_edit(bounded,
    queuedActions[1], toolbarCursor, Document.reduce)
assert(not refusedAccepted and rejection == "text_too_large")
assert(refused.current == "исходный" and refused.document_version == bounded.document_version)
assert(refusedCursor.last_seq == queuedActions[1].seq, "the refused edit must still be acknowledged")
local fixed, fixedCursor, fixedAccepted = Intents.apply_editor_edit(refused, queuedActions[2],
    refusedCursor, Document.reduce)
assert(fixedAccepted and fixed.current == "уже меньше")
local _afterToolbar, toolbarFinal, toolbarAccepted = Intents.apply_editor_edit(fixed, queuedActions[3],
    fixedCursor, Document.reduce)
assert(not toolbarAccepted and toolbarFinal.last_seq == queuedActions[3].seq,
    "a toolbar action behind the refused edit must still be consumed")
assert(not Document.accepts(string.rep("я", 32769)) and Document.accepts("ок"))
assert(Document.accepts(string.rep("a", 65536)))
local exact = Document.reduce(bounded, { kind = "edit", text = string.rep("a", 65536) })
assert(exact.current == string.rep("a", 65536), "the editor must accept exactly what the reducer accepts")
return true
