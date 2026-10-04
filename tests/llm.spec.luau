local Document = require("../plugin/voice-prompt/lib/document.luau")
local LLM = require("../plugin/voice-prompt/lib/llm.luau")
local state = Document.reduce(Document.new("s1"), { kind = "final", text = "Не меняй API" })
local requesting, snapshot = Document.begin_request(state, "r1", "prompt", 0)
local slow, disposition = Document.receive_result(requesting, snapshot, "Не меняй API.", 35000)
assert(disposition == "applied" and slow.current == "Не меняй API.", "A complete response after 35 seconds must remain applicable")

local now, stopped = 0, 0
local line, close, received, sent
local client = LLM.new(function() error("Chat must not use the host's 30-second HTTP transport") end,
    function() return now end, noctalia.json, function(request, onLine, onClose)
        sent, line, close = request, onLine, onClose
        return { stop = function() stopped += 1 end }
    end)
local options = { base_url = "http://127.0.0.1:18765/v1", model = "fixture", credential = "", system_prompt = "Edit" }
local function start(token)
    local document, request = Document.begin_request(state, token, "prompt", now)
    received = nil
    client:process(request, options, function(result) received = result end)
    return document, request
end
local function chunk(content, finish)
    line("data: " .. noctalia.json.encode({ choices = { { index = 0, delta = { content = content }, finish_reason = finish } } }))
    line("")
end
local pending, request = start("slow")
now = 35000
client:tick()
assert(received == nil, "Reasoning must not time out at 30 seconds")
chunk("Не меняй ")
chunk("API.", "stop")
assert(received == nil and pending.current == "Не меняй API", "Never apply partial output")
line("data: [DONE]")
close({ ok = true, status = 200 })
assert(received.kind == "success" and received.text == "Не меняй API.")
local applied, appliedDisposition = Document.receive_result(pending, request, received.text, now)
assert(appliedDisposition == "applied" and applied.current == "Не меняй API.")
assert(stopped == 1)
assert(sent.follow_redirects == false and sent.allow_insecure_tls == false)

local expired, expiredRequest = start("expired")
now = expiredRequest.deadline
client:tick()
assert(received.kind == "error" and received.code == "timeout" and stopped == 2)
received = nil
chunk("late", "stop")
close({ ok = true, status = 200 })
assert(received == nil and expired.current == "Не меняй API", "Late callbacks must not replace the document")

start("cancelled")
local oldLine, oldClose = line, close
client:cancel("cancelled")
oldLine('data: {"choices":[{"index":0,"delta":{"content":"late"},"finish_reason":"stop"}]}')
oldClose({ ok = true, status = 200 })
assert(received == nil and stopped == 3)

start("truncated")
chunk("Partial")
close({ ok = true, status = 200 })
assert(received.kind == "error" and received.code == "incomplete_response")
start("invalid")
line("data: not-json")
close({ ok = true, status = 200 })
assert(received.kind == "error" and received.code == "invalid_json")
start("large")
chunk(string.rep("x", 65537), "stop")
close({ ok = true, status = 200 })
assert(received.kind == "error" and received.code == "result_too_large")
start("refused")
line('data: {"choices":[{"index":0,"delta":{"refusal":"No"}}]}')
close({ ok = true, status = 200 })
assert(received.kind == "error" and received.code == "refusal")
local endings = {}
for _, ending in ipairs({ "length", "content_filter", "tool_calls" }) do
    start(ending)
    chunk("Partial output", ending)
    line("data: [DONE]")
    close({ ok = true, status = 200 })
    endings[ending] = received.code or received.kind
end
start("provider-event")
chunk("Partial output")
line('data: {"error":{"message":"Inference failed"}}')
line("")
line("data: [DONE]")
close({ ok = true, status = 200 })
endings.provider = received.code or received.kind
assert(endings.length == "incomplete_response" and endings.content_filter == "refusal"
    and endings.tool_calls == "nontext_response" and endings.provider == "provider_error",
    "Unsuccessful SSE must not succeed: " .. noctalia.json.encode(endings))
for _, pair in ipairs({ { 401, "authentication_failed" }, { 429, "rate_limited" }, { 503, "provider_error" } }) do
    start("http:" .. pair[1])
    line('{"error":{"message":"Rejected"}}')
    close({ ok = true, status = pair[1] })
    assert(received.kind == "error" and received.code == pair[2])
end
start("offline")
close({ ok = false, status = 0 })
assert(received.kind == "error" and received.code == "offline")

local authorization = nil
options.credential = "test-native-key"
start("authenticated")
for _, header in ipairs(sent.headers) do
    if string.sub(header, 1, 14) == "Authorization:" then authorization = header end
end
assert(authorization == "Authorization: Bearer test-native-key")
options.credential = ""
start("cleared")
for _, header in ipairs(sent.headers) do
    assert(string.sub(header, 1, 14) ~= "Authorization:", "Clearing the key must remove Authorization")
end
options.credential = "bad\nkey"
received = nil
client:process(snapshot, options, function(result) received = result end)
assert(received.kind == "error" and received.code == "credential_invalid")
assert(LLM.normalize_base("https://provider.invalid/v1/") == "https://provider.invalid/v1")
assert(LLM.normalize_base("http://192.168.1.22:8080/v1") == "http://192.168.1.22:8080/v1")
assert(LLM.normalize_base("http://provider.example/v1") == nil)
assert(LLM.normalize_base("https://provider.example/v1/v1") == nil)

-- Plaintext HTTP is allowed for local endpoints only: an IPv6 literal must be loopback ::1,
-- unique-local fc00::/7 or link-local fe80::/10. Public, IPv4-mapped and malformed literals must
-- be refused before any transport call so neither the transcript nor the key can leave in plaintext.
local function gate(base_url)
    local result
    local _document, request = Document.begin_request(state, "gate:" .. base_url, "prompt", now)
    local accepted = client:process(request, { base_url = base_url, model = "fixture",
        credential = "test-native-key", system_prompt = "Edit" }, function(value) result = value end)
    client:cancel(request.token)
    return accepted, result and result.code
end
for _, url in ipairs({ "http://localhost:18765/v1", "http://127.0.0.1:18765/v1",
    "http://[::1]:18765/v1", "http://[0:0:0:0:0:0:0:1]/v1",
    "http://[fc00::1]:18765/v1", "http://[fd12:3456:789a::1]/v1",
    "http://[fe80::1]/v1", "http://[febf:ffff::1]/v1" }) do
    assert(LLM.normalize_base(url) == url, "local endpoint must stay allowed: " .. url)
    assert(gate(url) == true, "local endpoint must reach the transport: " .. url)
end
local function refuse(url)
    assert(LLM.normalize_base(url) == nil, "non-local endpoint must be refused: " .. url)
    local accepted, code = gate(url)
    assert(accepted == false and code == "plaintext_endpoint_not_local",
        "non-local endpoint must be blocked before the transport: " .. url)
end
for _, url in ipairs({ "http://[2001:db8::1]/v1", "http://[2606:4700:4700::1111]/v1",
    "http://[::ffff:192.168.1.1]/v1", "http://[::ffff:127.0.0.1]/v1",
    "http://[0:0:0:0:0:ffff:7f00:1]/v1", "http://[fe00::1]/v1", "http://[fec0::1]/v1",
    "http://[fc00:1234]/v1", "http://[fc00::1::2]/v1", "http://[2001:db8:::1]/v1",
    "http://[fc00::gg]/v1", "http://[1:2:3:4:5:6:7:8::]/v1" }) do refuse(url) end
-- An unbracketed IPv6 authority, a bracketed host with an unparsed suffix, and a non-numeric port
-- are malformed authorities: the host extractor must not fall back to a leading label.
for _, url in ipairs({ "http://2001:db8::1/v1", "http://2606:4700:4700::1111/v1",
    "http://2001:db8::1:8080/v1", "http://[2606:4700:4700::1111]:8080/v1",
    "http://[::1]junk/v1", "http://[::1]junk:8080/v1", "http://[::1]:8080junk/v1",
    "http://[::1]:8080:90/v1", "http://[fc00::1]evil.example/v1", "http://[::1]evil.example:80/v1",
    "http://localhost:80x/v1", "http://::1/v1" }) do refuse(url) end
assert(LLM.normalize_base("https://[2001:db8::1]/v1") == "https://[2001:db8::1]/v1",
    "HTTPS must stay allowed regardless of host")
return true
