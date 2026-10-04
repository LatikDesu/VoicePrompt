--!nonstrict
local Document = {}
local MAX_TEXT_BYTES = 65536

local function checkText(text)
    if type(text) ~= "string" then error("invalid_text") end
    if #text > MAX_TEXT_BYTES then error("text_too_large") end
    return text
end

local function copy(state)
    local result = table.clone(state)
    result.undo = table.clone(state.undo)
    result.request = state.request and table.clone(state.request) or nil
    result.proposal = state.proposal and table.clone(state.proposal) or nil
    return result
end

local function pushUndo(state)
    table.insert(state.undo, state.current)
    if #state.undo > 10 then table.remove(state.undo, 1) end
end

local function replace(state, text, undo)
    checkText(text)
    if undo then pushUndo(state) end
    state.current = text
    state.document_version += 1
    state.input_generation += 1
end

local function cancel(state)
    state.request = nil
    state.proposal = nil
end

Document.MAX_TEXT_BYTES = MAX_TEXT_BYTES

-- Shared with the editor so an oversized paste is refused before it is published, keeping the
-- editor's local text identical to the document the service keeps.
function Document.accepts(text)
    return type(text) == "string" and #text <= MAX_TEXT_BYTES
end

function Document.new(session_id)
    assert(type(session_id) == "string" and session_id ~= "", "invalid_session")
    return { session_id = session_id, raw = nil, current = "", document_version = 0,
        input_generation = 0, undo = {}, proposal = nil, request = nil, deleted = false }
end

function Document.reduce(state, action)
    local next = copy(state)
    local kind = action.kind
    if kind == "delete" then
        next.raw = nil
        next.current = ""
        next.undo = {}
        next.deleted = true
        next.document_version += 1
        next.input_generation += 1
        cancel(next)
        return next
    end
    if next.deleted then error("document_deleted") end
    if kind == "final" then
        if next.raw ~= nil then error("final_already_set") end
        checkText(action.text)
        next.raw = action.text
        replace(next, action.text, false)
        cancel(next)
    elseif kind == "edit" then
        checkText(action.text)
        local count = action.edit_count
        if count == nil then count = action.text ~= next.current and 1 or 0 end
        if type(count) ~= "number" or count < 0 or count ~= math.floor(count) or count ~= count then
            error("invalid_edit_count")
        end
        if count > 0 then
            next.current = action.text
            next.document_version += count
        elseif action.text ~= next.current then
            error("invalid_edit_count")
        end
    elseif kind == "replace" then
        replace(next, action.text, true)
        cancel(next)
    elseif kind == "clear" then
        replace(next, "", true)
        cancel(next)
    elseif kind == "undo" then
        local text = table.remove(next.undo)
        if text ~= nil then replace(next, text, false) end
        cancel(next)
    elseif kind == "restore" then
        if next.raw ~= nil then replace(next, next.raw, true) end
        cancel(next)
    elseif kind == "apply_proposal" then
        if next.proposal ~= nil then replace(next, next.proposal.text, true) end
        cancel(next)
    elseif kind == "discard_proposal" then
        next.proposal = nil
    elseif kind == "cancel_request" then
        cancel(next)
    else
        error("unknown_document_action")
    end
    return next
end

function Document.begin_request(state, token, kind, now_ms)
    assert(kind == "clean" or kind == "prompt", "invalid_processing_kind")
    assert(type(token) == "string" and token ~= "", "invalid_request_token")
    assert(not state.deleted and #state.current > 0, "empty_document")
    local next = copy(state)
    local snapshot = { session_id = state.session_id, document_version = state.document_version,
        text = state.current, token = token, kind = kind, deadline = now_ms + 120000 }
    next.request = table.clone(snapshot)
    next.proposal = nil
    return next, snapshot
end

function Document.receive_result(state, snapshot, text, now_ms)
    local next = copy(state)
    local request = next.request
    if next.deleted or next.session_id ~= snapshot.session_id or request == nil
        or request.token ~= snapshot.token or now_ms >= request.deadline then
        if request and request.token == snapshot.token then next.request = nil end
        return next, "discarded"
    end
    checkText(text)
    next.request = nil
    if next.document_version ~= snapshot.document_version then
        next.proposal = { text = text, token = snapshot.token, kind = snapshot.kind,
            document_version = snapshot.document_version }
        return next, "proposal"
    end
    replace(next, text, true)
    next.proposal = nil
    return next, "applied"
end

return Document
