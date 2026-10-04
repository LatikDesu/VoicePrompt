--!nonstrict
local Config = {}

function Config.capture(get, env, expand)
    local profile = get("model_profile") or "cpu-small"
    local device, path
    if profile == "custom" then
        device = get("device") or "cpu"
        local custom = get("custom_ct2_path") or ""
        assert(custom ~= "", "custom_model_missing")
        path = expand(custom)
    else
        assert(profile == "cpu-small" or profile == "cuda-turbo", "model_profile_invalid")
        device = profile == "cuda-turbo" and "cuda" or "cpu"
        local cache = env("XDG_CACHE_HOME")
        if not cache or cache == "" then
            local home = env("HOME")
            assert(home and home ~= "", "cache_directory_unavailable")
            cache = home .. "/.cache"
        end
        path = cache .. "/voiceprompt/models/" .. profile
    end
    assert(device == "cpu" or device == "cuda", "device_invalid")
    return {
        config_schema = 1, language = get("language"), device = device,
        model_profile = profile, model_path = path,
        microphone_target = "", glossary = get("glossary") or "",
        preload = get("preload"), idle_unload_seconds = get("idle_unload_seconds"),
        model_load_timeout_seconds = get("model_load_timeout_seconds"),
        final_timeout_seconds = get("final_timeout_seconds"),
        evdev_enabled = get("activation_mode") == "hold-evdev",
        evdev_device = get("evdev_device") or "", evdev_chord = get("evdev_chord") or "",
    }
end

return Config
