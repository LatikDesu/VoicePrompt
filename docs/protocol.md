# Helper protocol v1

The helper and the Noctalia service exchange two framed channels:

- **Commands** over a private Unix socket, one JSON request and one JSON acknowledgement per
  connection.
- **Events** as UTF-8 NDJSON on helper stdout, one flushed message per line.

Diagnostic text goes to stderr. Transcripts travel through the event stream and private staging
files, not argv or stderr. API keys stay in the Noctalia HTTP client and never enter this protocol.

## Envelope validation

A request is a JSON object with these fields:

| Field | Requirement |
| --- | --- |
| `protocol_version` | Integer exactly `1` |
| `request_id` | Nonempty UTF-8 string, at most 256 bytes |
| `owner_generation` | Nonempty UTF-8 string, at most 256 bytes |
| `command` | Nonempty UTF-8 string, at most 256 bytes |
| `session_id` | Required nonempty string for `start`/`stop`/`cancel`; optional elsewhere |
| `payload` | JSON object (defaults to `{}`) |

A line longer than 262144 bytes is rejected before JSON parsing. Invalid UTF-8/JSON or non-finite
constants, a non-object envelope, a wrong protocol version, invalid identifiers, a missing recording
session, and a non-object payload are rejected without dispatch. Stable codes:
`message_too_large`, `invalid_json`, `invalid_envelope`, `protocol_incompatible`, `invalid_identity`,
`session_required`, `invalid_session`, `invalid_payload`.

## Acknowledgements and ordering

An accepted acknowledgement is:

```json
{"protocol_version":1,"owner_generation":"<gen>","request_id":"<id>","accepted":true}
```

Rejections keep `accepted:false` and add a stable `error_code`. Some commands add fields (for
example `status` returns a `status` object, and private staging returns `path`/`token`). `accepted`
never means a future decode has completed.

Every event carries `event`, `protocol_version`, `owner_generation` and a strictly increasing `seq`;
session-bearing events also include `session_id`. The plugin rejects events from a stale owner or
session and any non-increasing `seq`.

## Identities, idempotency and lease

- `voiceprompt-helper identity` creates a fresh owner generation (UUID) with no side effects. The
  plugin installs it for the helper's lifetime and uses it as the request identity prefix.
- The helper keeps a bounded 256-entry cache of `(command, session_id, payload)` fingerprints and
  the acknowledgement each produced. An identical retry receives the original acknowledgement;
  reusing a `request_id` with different content is `request_conflict`. In-flight identities are
  reserved before any await, so a retry cannot execute twice or change the reply.
- A request whose `owner_generation` differs from the running helper's is rejected with
  `owner_mismatch`; a stale owner cannot renew or shut down the active helper.
- `renew_lease` is the only command that extends the owner lease. The lease TTL is 10 s and the
  plugin renews every 2 s. The helper heartbeat (every 2 s) is separate and does not renew the
  owner lease.

## Command reference

### Operational

| Command | Payload | Result |
| --- | --- | --- |
| `renew_lease` | `{}` | Extends the lease; rejected with `lease_expired` when already expired |
| `status` | `{}` | Returns engine state, model flags, capabilities and safe language/device config |
| `configure` | schema-1 config object | Validates and copies settings for the next session; emits `configured` |
| `shutdown` | `{}` | Stops the helper and releases its runtime |

`renew_lease` and `status` stay responsive while a slow mutating command is pending and change
neither session nor configuration state.

### Recording

| Command | Payload | Notes |
| --- | --- | --- |
| `start` | `{}` + `session_id` | Captures a session/config snapshot and opens PipeWire capture; no worker is admitted until model integrity and profile checks pass |
| `stop` | `{}` + `session_id` | Closes capture before the final decode; completion arrives as a `final` (or `error`) event, not the acknowledgement |
| `cancel` | `{}` + `session_id` | Discards pending final work and tears down the owned capture/worker |
| `unload` | `{}` | Frees the model worker and invalidates pending preload |

Recording commands require the current `session_id`; a repeated identical `start` for the active
session is a no-op, while a different id returns `session_busy`. `unload` does not require a session. When no
engine is attached, `stop`/`cancel`/`unload` are accepted as no-ops and `start` returns
`capture_unavailable`.

### Model setup

| Command | Payload | Result |
| --- | --- | --- |
| `model_download` | `{"profile":"cpu-small" \| "cuda-turbo"}` | Starts a pinned-revision download; emits bounded `model_download_progress`, then `model_download_complete` or `model_download_error` |
| `model_download_cancel` | `{}` | Terminates the owned downloader, waits for its publication thread and child reap, then removes only its staging tree |

An unknown profile returns `model_profile_download_unavailable`; starting a second transfer while
one is active returns `model_download_busy`.

### Private staging

| Command | Payload | Result |
| --- | --- | --- |
| `prepare_private_file` | `{"purpose":"history" \| "probe","max_bytes":<int>}` | Creates a short-lived 0600 file under a 0700 directory and returns `path`/`token` |
| `discard_private_file` | `{"token":"<hex>"}` | Removes that allocation |

Allocation limits and errors are listed under [Private staging and text limits](#private-staging-and-text-limits).

### History

| Command | Payload | Result |
| --- | --- | --- |
| `history_save` | `{"root":"<dir>","token":"<hex>"}` | Consumes a staging token, validates schema 1 and writes the records |
| `history_load` | `{"root":"<dir>"}` | Returns a staging `path`/`token` plus `count` and `schema` instead of inlining the records |
| `history_delete` | `{"root":"<dir>","session_id":"<id>"}` | Removes one record |
| `history_clear` | `{"root":"<dir>"}` | Replaces the file with an empty record list |

Loads existing records before a replacement save; an unreadable or incompatible history file is
never replaced with an unseen empty list. Unknown commands return `unsupported_command`.

## Configuration schema 1

`configure` requires `payload.config_schema` to be the integer `1`; otherwise it returns
`config_incompatible`. Values are merged over the running configuration and re-validated:

| Field | Accepted values | Default |
| --- | --- | --- |
| `language` | `ru`, `en`, `auto` | `ru` |
| `device` | `cpu`, `cuda` | `cpu` |
| `model_profile` | `cpu-small`, `cuda-turbo`, `custom` | `cpu-small` |
| `model_path` | Path string (empty accepted by `configure`; `start` needs a valid model directory) | `""` |
| `microphone_target` | PipeWire node name, empty for the system default source | `""` |
| `glossary` | String, at most 8192 UTF-8 bytes | `""` |
| `preload` | Boolean | `false` |
| `idle_unload_seconds` | Integer 0–3600 | `120` |
| `model_load_timeout_seconds` | Integer 1–3600 | `120` |
| `final_timeout_seconds` | Integer 1–3600 | `120` |
| `evdev_enabled` / `evdev_device` / `evdev_chord` | Optional direct keyboard hold | disabled |
| `compute_type` | `int8` (CPU) or `int8_float16` (CUDA), when set | profile-derived |

`cpu-small` requires `device=cpu` and `cuda-turbo` requires `device=cuda`; a mismatch or an
unaccepted value returns `configuration_invalid`. The plugin builds this object in
`plugin/voice-prompt/lib/config.luau`.

## Private staging and text limits

| Limit | Value |
| --- | --- |
| Message (request, ack or event line) | 262144 bytes |
| Dictated/edited text | 65536 UTF-8 bytes |
| Identifier (`request_id`, `owner_generation`, `command`, `session_id`) | 256 UTF-8 bytes |
| Staging `history` purpose | 8388608 bytes |
| Staging `probe` purpose | 65536 bytes |
| Concurrent staging allocations | 4 |
| Staging allocation lifetime | 30 s |
| History file | 8388608 bytes, at most 10 records; `raw`/`current` each ≤ 65536 bytes |

Staging files are created 0600 inside a per-token 0700 directory. `private_directory_invalid`,
`staging_limit_invalid`, `staging_busy`, `staging_token_invalid`, `staging_invalid`,
`data_too_large` and `staging_cleanup_failed` report staging failures. History validation reports
`schema_incompatible`, `history_malformed`, `history_text_limit` or `history_too_large`.

## Lifecycle

1. The plugin generates an owner generation, then launches
   `exec voiceprompt-helper supervise --owner <generation> --runtime-dir <dir>` (the runtime
   directory defaults to `$XDG_RUNTIME_DIR/voiceprompt`; `--runtime-dir` overrides it).
2. The helper acquires the owner lock, opens `control.sock` and emits `ready` with
   `helper_version`, `protocol_min=1`, `protocol_max=1`, `config_schema=1`, capabilities
   (`control`, `private_files`, `history`, `model_setup`, `evdev_optional` and the engine's
   `cpu_stt`) and `seq`. The plugin checks compatibility before capture and sends an initial
   `configure`.
3. `configure` → `start` opens capture; the engine emits `capture_started`, then `model_loading`
   and `model_ready` as integrity validation and worker load complete (or `error`).
4. While speaking, the engine emits `partial` snapshots (`committed_text`, `tentative_text`,
   `audio_end_ms`, `degraded`) and `heartbeat` keepalives.
5. `stop` emits `finalizing`, closes capture, runs the final decode and emits `final` with the text
   and `speech_detected`, or `error`. `cancel` emits `cancelled`.
6. `unload` retires the worker (`worker_unloaded`); `shutdown`, lease expiry, SIGINT/SIGTERM or a
   broken stdout stream stop the helper, release its owned processes and remove its control socket.

Ready reports actual CPU capability; CUDA is not advertised from GPU presence alone. Optional
evdev hold publishes `hotkey_pressed`, `hotkey_released` and `input_unavailable` and never records
independently. Missing or invalid local models produce actionable `error` events rather than fake
transcriptions.

## Model validation and publication

- **Validation**: resolving a local model fingerprints the asset files and validates structure and
  the pinned profile. The hash runs in a worker thread (`asyncio.to_thread`), outside the supervisor
  event loop, so capture and lease monitoring stay responsive. Loading is bounded by
  `model_load_timeout_seconds`.
- **Download**: a spawned child process streams bytes and reports progress; the supervisor event
  loop is the only publisher. `model_download` reserves its slot before the first await, and a
  generation counter fences asynchronous work so a cancelled or replaced job emits nothing into its
  successor.
- **Publication**: the prepared artifact finishes in a worker thread while the loop stays
  responsive. `model_download_cancel` (and helper shutdown) increments the generation, then waits
  for the owned publication thread and the child reap before removing the staging tree, so no
  reclaim can race a thread that is still reading it.

## Credentials

The helper exposes no credential or key-store command. `llm_api_key` is a native Noctalia setting
that the service reads and sends only inside native HTTP headers; Clean/Prompt use SSE with a 120 s
deadline and a 64 KiB text limit, and `/models` uses ordinary native HTTP with a 30 s deadline.
