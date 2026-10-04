--!nonstrict
local M = {}

function M.resolve()
    local configured = noctalia.getConfig("helper_path") or ""
    if configured ~= "" then
        local path = noctalia.expandPath(configured)
        if not noctalia.fileExists(path) then
            return nil, "helper_path_invalid"
        end
        return path, nil
    end
    local data = noctalia.getenv("XDG_DATA_HOME")
    if not data or data == "" then data = (noctalia.getenv("HOME") or "") .. "/.local/share" end
    local installed = data .. "/voiceprompt/helper/bin/voiceprompt-helper"
    if noctalia.fileExists(installed) then return installed, nil end
    if noctalia.commandExists("voiceprompt-helper") then
        return "voiceprompt-helper", nil
    end
    return nil, "helper_not_installed"
end

local function quote(value)
    return "'" .. string.gsub(value, "'", "'\\''") .. "'"
end

function M.diagnosticsStream(config, onResult)
    local executable, err = M.resolve()
    if executable == nil then onResult(nil, err); return false end
    local accepted = noctalia.runAsync({ executable, "setup-status", "--json", "--device", config.device,
        "--model-profile", config.model_profile, "--model-path", config.model_path }, function(result)
        if result.timedOut or result.stdoutTruncated or #(result.stdout or "") > 262144 then
            onResult(nil, "helper_diagnostics_timeout"); return
        end
        local report = noctalia.json.decode(result.stdout or "")
        if type(report) ~= "table" or type(report.recording_ready) ~= "boolean" then
            onResult(nil, "helper_diagnostics_invalid"); return
        end
        onResult(report, nil)
    end, 60000)
    if not accepted then onResult(nil, "helper_launch_failed") end
    return accepted
end

function M.install()
    local dir = noctalia.pluginDir()
    local script = dir and (dir .. "/../../scripts/install.sh") or nil
    if not script or not noctalia.fileExists(script) then return false, "installer_unavailable" end
    if not noctalia.runInTerminal("bash " .. quote(script)) then return false, "installer_launch_failed" end
    return true, nil
end

function M.identity(callback)
    local executable, err = M.resolve()
    if executable == nil then callback(nil, err); return false end
    return noctalia.runAsync({ executable, "identity" }, function(result)
        local value = noctalia.json.decode(result.stdout or "")
        if result.exitCode == 0 and type(value) == "table" and type(value.owner_generation) == "string" then
            callback(value.owner_generation, nil)
        else
            callback(nil, "helper_identity_failed")
        end
    end)
end

function M.launch(owner, runtime_dir, onLine)
    local executable, err = M.resolve()
    if executable == nil then return false, err end
    return noctalia.runStream("exec " .. quote(executable) .. " supervise --owner " .. quote(owner)
        .. " --runtime-dir " .. quote(runtime_dir), onLine)
end

function M.send(envelope, runtime_dir, callback)
    local executable, err = M.resolve()
    if executable == nil then
        if callback then callback(nil, err) end
        return false
    end
    local encoded = noctalia.json.encode(envelope)
    if encoded == nil or #encoded > 262144 then
        if callback then callback(nil, "command_too_large") end
        return false
    end
    local argv = { executable, "command", "--runtime-dir", runtime_dir, "--json", encoded }
    if callback == nil then return noctalia.runAsync(argv) end
    return noctalia.runAsync(argv, function(result)
        if result.timedOut or result.stdoutTruncated or #(result.stdout or "") > 262144 then
            callback(nil, "helper_command_timeout")
            return
        end
        local ack = noctalia.json.decode(result.stdout or "")
        if type(ack) ~= "table" then callback(nil, "helper_ack_invalid"); return end
        callback(ack, nil)
    end)
end

return M
