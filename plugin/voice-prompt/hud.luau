--!nonstrict
local Intents = require("./lib/intents.luau")
local view = noctalia.state.get("view") or {}
local publisher = nil
local function tr(key) return noctalia.tr("ui." .. key) end
local function emit(kind)
    if not publisher or not view.owner_generation then return end
    local outbox = publisher:enqueue({ kind = kind, session_id = view.session_id })
    if #outbox.actions > 0 then publisher:mark_published(outbox.actions[#outbox.actions].seq) end
    outbox.owner_generation = view.owner_generation
    noctalia.state.set("intent.hud", outbox)
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
local function render()
    local elapsed = math.floor((view.elapsed_ms or 0) / 1000)
    local status = tr(view.status or "idle")
    if view.status == "recording" and view.model_loading then
        status = tr("recording_loading")
    elseif view.status == "starting" and view.model_loading then
        status = tr("loading_model")
    end
    panel.render(ui.column({ flexGrow = 1, padding = 8, gap = 8 }, {
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = "microphone", size = 20, color = "primary" }),
            ui.label({ text = status, fontWeight = "bold", flexGrow = 1 }),
            ui.label({ text = string.format("%02d:%02d", math.floor(elapsed / 60), elapsed % 60) }),
        }),
        ui.scroll({ key = "recording-transcript:" .. (view.session_id or "idle"), flexGrow = 1,
            gap = 6, stickToBottom = true }, {
            ui.label({ key = "committed", text = view.committed_text or "" }),
            ui.label({ key = "tentative", text = view.tentative_text or "", color = "on_surface/0.6" }),
        }),
        ui.label({ text = view.degraded and tr("degraded") or "", color = "on_surface/0.65", visible = view.degraded == true }),
        ui.row({ gap = 8 }, {
            icon("stop", "player-stop", true, function() emit("record-stop") end, true),
            icon("cancel", "x", true, function() emit("record-cancel") end),
            icon("copy", "copy", #(view.committed_text or "") + #(view.tentative_text or "") > 0,
                function() emit("copy") end),
        }),
    }))
end

local function initializePublisher()
    if not view.owner_generation then return end
    local id = "hud:" .. view.owner_generation .. ":" .. tostring(noctalia.nowMs())
    local instance = Intents.new(id)
    publisher = instance
    noctalia.state.watch("ack." .. id, function(seq) instance:ack(seq) end)
end
noctalia.state.watch("view", function(value)
    local oldOwner = view.owner_generation
    view = value or {}
    if not publisher or oldOwner ~= view.owner_generation then initializePublisher() end
    render()
end)
function onOpen()
    view = noctalia.state.get("view") or {}
    if not publisher then initializePublisher() end
    render()
end
