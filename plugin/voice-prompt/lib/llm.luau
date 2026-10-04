--!nonstrict
local LLM = {}
local Client = {}
Client.__index = Client

-- IPv6 literals may only name a local endpoint. A literal is local when it is loopback ::1, inside
-- unique-local fc00::/7 or link-local fe80::/10. Malformed literals and IPv4-embedded mapped
-- addresses (::ffff:127.0.0.1, 0:0:0:0:0:ffff:7f00:1) are not local, so a public host cannot pass.
local function localIpv6(hostname)
    local function groups(part)
        if part == "" then return {} end
        local result, start = {}, 1
        while true do
            local colon = string.find(part, ":", start, true)
            local group = colon and string.sub(part, start, colon - 1) or string.sub(part, start)
            if group == "" or #group > 4 or not string.match(group, "^%x+$") then return nil end
            table.insert(result, tonumber(group, 16))
            if not colon then break end
            start = colon + 1
        end
        return result
    end
    local head, tail
    local first = string.find(hostname, "::", 1, true)
    if first then
        if string.find(hostname, "::", first + 2, true) then return false end
        head, tail = string.sub(hostname, 1, first - 1), string.sub(hostname, first + 2)
    else
        head, tail = hostname, ""
    end
    local left, right = groups(head), groups(tail)
    if not left or not right then return false end
    if first == nil then
        if #left ~= 8 then return false end
    elseif #left + #right > 7 then
        return false
    end
    local hextets = {}
    for _, value in ipairs(left) do table.insert(hextets, value) end
    for _ = #left + #right + 1, 8 do table.insert(hextets, 0) end
    for _, value in ipairs(right) do table.insert(hextets, value) end
    if (hextets[1] >= 0xfc00 and hextets[1] <= 0xfdff)
        or (hextets[1] >= 0xfe80 and hextets[1] <= 0xfebf) then return true end
    for index = 1, 7 do if hextets[index] ~= 0 then return false end end
    return hextets[8] == 1
end

-- Extract the host of an HTTP authority, or nil when the authority is malformed. A bracketed host
-- must be followed only by an optional decimal port; an unbracketed host must be a hostname/IPv4
-- with an optional decimal port, so an unbracketed IPv6 literal can never fall back to a leading
-- single label and a stray bracket suffix can never keep an extracted local IPv6 host.
local function authorityHostname(authority)
    if string.sub(authority, 1, 1) == "[" then
        local host, suffix = string.match(authority, "^%[([^%]]+)%](.*)$")
        if not host or (suffix ~= "" and not string.match(suffix, "^:%d+$")) then return nil end
        return host
    end
    local host, port = string.match(authority, "^([^:]+):(.*)$")
    if not host then
        -- No host before a colon: an IPv6 literal must be bracketed, so this authority is malformed.
        if string.find(authority, ":", 1, true) then return nil end
        return authority
    end
    if not string.match(port, "^%d+$") then return nil end
    return host
end

local function localHttpAuthority(authority)
    local host = authorityHostname(authority)
    if not host then return false end
    local hostname = string.lower(host)
    if hostname == "::1" or hostname == "localhost" or string.sub(hostname, -10) == ".localhost"
        or string.sub(hostname, -6) == ".local" or string.sub(hostname, -4) == ".lan" then return true end
    if string.find(hostname, ":", 1, true) then return localIpv6(hostname) end
    if not string.find(hostname, "[%.%[%]]") then return hostname ~= "" end
    local a, b, c, d = string.match(hostname, "^(%d+)%.(%d+)%.(%d+)%.(%d+)$")
    if not a then return false end
    a, b, c, d = tonumber(a), tonumber(b), tonumber(c), tonumber(d)
    if a > 255 or b > 255 or c > 255 or d > 255 then return false end
    return a == 10 or a == 127 or (a == 192 and b == 168)
        or (a == 172 and b >= 16 and b <= 31) or (a == 169 and b == 254)
end

function LLM.normalize_base(url)
    if type(url) ~= "string" or #url > 4096 then return nil, "base_url_invalid" end
    local base = string.gsub(url, "/+$", "")
    if not string.match(base, "^https?://[^/%s?#@]+/") or string.sub(base, -3) ~= "/v1"
        or string.find(base, "[%s?#]") then
        return nil, "base_url_invalid"
    end
    if string.sub(base, -6) == "/v1/v1" then return nil, "base_url_invalid" end
    if string.sub(base, 1, 7) == "http://" then
        local authority = string.match(base, "^http://([^/]+)/")
        if not authority or not localHttpAuthority(authority) then
            return nil, "plaintext_endpoint_not_local"
        end
    end
    return base, nil
end

function LLM.new(http, now_ms, codec, stream)
    assert(type(http) == "function" and type(now_ms) == "function" and type(codec) == "table" and type(stream) == "function")
    return setmetatable({ http = http, stream = stream, now_ms = now_ms, codec = codec,
        pending = {}, processing_token = nil, models_token = nil, counter = 0 }, Client)
end

local function errorCode(response)
    if not response.ok then return "offline" end
    if response.status == 401 or response.status == 403 then return "authentication_failed" end
    if response.status == 429 then return "rate_limited" end
    if response.status >= 500 then return "provider_error" end
    if response.status < 200 or response.status >= 300 then return "http_error" end
    return nil
end

local function headers(credential)
    local result = { "Content-Type: application/json", "Accept: application/json" }
    if credential and credential ~= "" then
        if string.find(credential, "[\r\n]") then return nil end
        table.insert(result, "Authorization: Bearer " .. credential)
    end
    return result
end

function Client:cancel(token)
    local operation = self.pending[token]
    self.pending[token] = nil
    if self.processing_token == token then self.processing_token = nil end
    if self.models_token == token then self.models_token = nil end
    if operation and operation.handle then operation.handle.stop() end
end

function Client:_finish(token, result)
    local operation = self.pending[token]
    if operation == nil then return end
    self:cancel(token)
    result.token = token
    operation.callback(result)
end

function Client:tick()
    local now = self.now_ms()
    local expired = {}
    for token, operation in pairs(self.pending) do
        if now >= operation.deadline then table.insert(expired, token) end
    end
    for _, token in ipairs(expired) do
        self:_finish(token, { kind = "error", code = "timeout" })
    end
end

function Client:_send(token, request, deadline, callback, decode)
    self.pending[token] = { deadline = deadline, callback = callback }
    local accepted = self.http(request, function(response)
        local operation = self.pending[token]
        if operation == nil then return end
        if self.now_ms() >= operation.deadline then
            self:_finish(token, { kind = "error", code = "timeout" })
            return
        end
        local failure = errorCode(response)
        if failure then
            self:_finish(token, { kind = "error", code = failure, status = response.status })
            return
        end
        if type(response.body) ~= "string" or #response.body > 1048576 then
            self:_finish(token, { kind = "error", code = "result_too_large" })
            return
        end
        local ok, value = pcall(self.codec.decode, response.body)
        if not ok or type(value) ~= "table" then
            self:_finish(token, { kind = "error", code = "invalid_json" })
            return
        end
        local result = decode(value)
        self:_finish(token, result)
    end)
    if not accepted and self.pending[token] then
        self:_finish(token, { kind = "error", code = "request_rejected" })
    end
    return accepted
end

function Client:_stream(token, request, deadline, callback)
    local operation = { deadline = deadline, callback = callback, parts = {}, event = {}, bytes = 0, text_bytes = 0 }
    self.pending[token] = operation
    local function consumeEvent()
        if #operation.event == 0 or operation.failure or operation.done then return end
        local data = table.concat(operation.event, "\n")
        table.clear(operation.event)
        if data == "[DONE]" then operation.done = true; return end
        local ok, value = pcall(self.codec.decode, data)
        if not ok or type(value) ~= "table" then operation.failure = "invalid_json"; return end
        if value.error ~= nil then operation.failure = "provider_error"; return end
        local choices = value.choices
        local choice = type(choices) == "table" and choices[1] or nil
        if type(choice) ~= "table" then return end
        if choice.index ~= nil and choice.index ~= 0 then return end
        local delta = choice.delta
        if type(delta) ~= "table" then operation.failure = "nontext_response"; return end
        if delta.refusal and delta.refusal ~= "" then operation.failure = "refusal"; return end
        if delta.content ~= nil then
            if type(delta.content) ~= "string" then operation.failure = "nontext_response"; return end
            operation.text_bytes += #delta.content
            if operation.text_bytes > 65536 then operation.failure = "result_too_large"; return end
            table.insert(operation.parts, delta.content)
        end
        if choice.finish_reason == "stop" then operation.finished = true
        elseif choice.finish_reason == "content_filter" then operation.failure = "refusal"
        elseif choice.finish_reason == "tool_calls" or choice.finish_reason == "function_call" then operation.failure = "nontext_response"
        elseif choice.finish_reason ~= nil then operation.failure = "incomplete_response" end
    end
    local handle = self.stream(request, function(line)
        if self.pending[token] ~= operation then return end
        if self.now_ms() >= deadline then self:_finish(token, { kind = "error", code = "timeout" }); return end
        operation.bytes += #line + 1
        if operation.bytes > 1048576 then self:_finish(token, { kind = "error", code = "result_too_large" }); return end
        if line == "" then consumeEvent()
        elseif string.sub(line, 1, 5) == "data:" then
            local data = string.sub(line, 6)
            if string.sub(data, 1, 1) == " " then data = string.sub(data, 2) end
            table.insert(operation.event, data)
        end
    end, function(response)
        if self.pending[token] ~= operation then return end
        if self.now_ms() >= deadline then self:_finish(token, { kind = "error", code = "timeout" }); return end
        local failure = errorCode(response)
        if not failure then consumeEvent(); failure = operation.failure end
        if not failure and not operation.done and not operation.finished then failure = "incomplete_response" end
        if not failure and operation.text_bytes == 0 then failure = "empty_response" end
        if failure then self:_finish(token, { kind = "error", code = failure, status = response.status })
        else self:_finish(token, { kind = "success", text = table.concat(operation.parts) }) end
    end)
    if self.pending[token] == operation then
        operation.handle = handle
        if not handle then self:_finish(token, { kind = "error", code = "request_rejected" }) end
    elseif handle then handle.stop() end
    return handle ~= nil
end

function Client:process(snapshot, settings, callback)
    local base, failure = LLM.normalize_base(settings.base_url)
    if not base then callback({ kind = "error", code = failure, token = snapshot.token }); return false end
    if type(settings.model) ~= "string" or settings.model == "" then
        callback({ kind = "error", code = "model_missing", token = snapshot.token }); return false
    end
    local requestHeaders = headers(settings.credential)
    if not requestHeaders then callback({ kind = "error", code = "credential_invalid", token = snapshot.token }); return false end
    if self.processing_token then self:cancel(self.processing_token) end
    local token = snapshot.token
    self.processing_token = token
    local request = { url = base .. "/chat/completions", method = "POST",
        headers = requestHeaders, follow_redirects = false, allow_insecure_tls = false,
        body = self.codec.encode({ model = settings.model, stream = true,
            messages = { { role = "system", content = settings.system_prompt },
                { role = "user", content = snapshot.text } } }) }
    request.headers[2] = "Accept: text/event-stream"
    return self:_stream(token, request, snapshot.deadline, callback)
end

function Client:refresh_models(base_url, credential, callback)
    local base, failure = LLM.normalize_base(base_url)
    if not base then callback({ kind = "error", code = failure }); return false end
    local requestHeaders = headers(credential)
    if not requestHeaders then callback({ kind = "error", code = "credential_invalid" }); return false end
    if self.models_token then self:cancel(self.models_token) end
    self.counter += 1
    local token = "models:" .. self.counter
    self.models_token = token
    return self:_send(token, { url = base .. "/models", method = "GET", headers = requestHeaders,
        follow_redirects = false, allow_insecure_tls = false }, self.now_ms() + 30000, callback, function(value)
        if type(value.data) ~= "table" then return { kind = "error", code = "models_unavailable" } end
        local models, seen = {}, {}
        for _, item in ipairs(value.data) do
            if type(item) == "table" and type(item.id) == "string" and item.id ~= "" and not seen[item.id] then
                table.insert(models, item.id)
                seen[item.id] = true
                if #models >= 512 then break end
            end
        end
        return { kind = "success", models = models }
    end)
end

return LLM
