--!nonstrict
local Intents = require("./lib/intents.luau")
local view = noctalia.state.get("view") or {}
local publisher = nil
local function emit(kind)
    if not publisher or not view.owner_generation then return end
    local outbox = publisher:enqueue({ kind = kind, session_id = view.session_id })
    if #outbox.actions > 0 then publisher:mark_published(outbox.actions[#outbox.actions].seq) end
    outbox.owner_generation = view.owner_generation
    noctalia.state.set("intent.widget", outbox)
end
local function initialize()
    if not view.owner_generation then return end
    local id = "widget:" .. view.owner_generation .. ":" .. tostring(noctalia.nowMs()) .. ":" .. tostring(math.random(1, 100000000))
    local instance = Intents.new(id)
    publisher = instance
    noctalia.state.watch("ack." .. id, function(seq) instance:ack(seq) end)
end
local function render()
    local status = view.status or "idle"
    local recording = status == "recording"
    local processing = status == "finalizing" or status == "processing"
    barWidget.setText("")
    barWidget.setGlyph(processing and "hourglass" or "microphone")
    barWidget.setGlyphColor(recording and "primary" or "on_surface")
    barWidget.setTooltip("VoicePrompt: " .. noctalia.tr("ui." .. status))
end

noctalia.state.watch("view", function(value)
    local oldOwner = view.owner_generation
    view = value or {}
    if not publisher or oldOwner ~= view.owner_generation then initialize() end
    render()
end)
function update() render() end
function onClick() emit("record-toggle") end
function onRightClick() emit("show-editor") end
