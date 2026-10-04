local Config = require("../plugin/voice-prompt/lib/config.luau")
local values = {
    model_profile = "cpu-small", device = "cuda", custom_ct2_path = "/prepared/custom",
    language = "ru", preload = false, idle_unload_seconds = 120,
    model_load_timeout_seconds = 120, final_timeout_seconds = 120,
    activation_mode = "toggle", evdev_device = "", evdev_chord = "", glossary = "",
}
local function get(key) return values[key] end
local function env(key) if key == "XDG_CACHE_HOME" then return "/cache" end end
local function expand(path) return path end
local cpu = Config.capture(get, env, expand)
assert(cpu.device == "cpu" and cpu.model_profile == "cpu-small")
assert(cpu.model_path == "/cache/voiceprompt/models/cpu-small")
values.model_profile, values.device = "cuda-turbo", "cpu"
local cuda = Config.capture(get, env, expand)
assert(cuda.device == "cuda" and cuda.model_path == "/cache/voiceprompt/models/cuda-turbo")
assert(cpu.device == "cpu" and cpu.model_path == "/cache/voiceprompt/models/cpu-small")
values.model_profile, values.device = "custom", "cpu"
local custom = Config.capture(get, env, expand)
assert(custom.device == "cpu" and custom.model_path == "/prepared/custom")
values.device = "cuda"
assert(Config.capture(get, env, expand).device == "cuda")
values.custom_ct2_path = ""
local ok = pcall(Config.capture, get, env, expand)
assert(not ok, "custom profile without a model must not target a fabricated cache path")
values.model_profile = "cpu-small"
local home = Config.capture(get, function(key) if key == "HOME" then return "/home/user" end end, expand)
assert(home.model_path == "/home/user/.cache/voiceprompt/models/cpu-small")
return true
