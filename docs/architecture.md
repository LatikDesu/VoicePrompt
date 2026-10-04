# Architecture

VoicePrompt is a Noctalia v5 plugin (plugin API 32) plus a separately installed local Python
helper. The plugin owns the user interface, document state and LLM requests; the helper owns
recording, speech recognition and model processes. They meet at two channels: a private Unix
socket for commands and the helper's stdout for a versioned NDJSON event stream.

## Components

| Component | Location | Responsibility |
| --- | --- | --- |
| Plugin service | `plugin/voice-prompt/service.luau` | Single owner of document and control state, helper lifecycle and LLM requests |
| HUD panel | `plugin/voice-prompt/hud.luau` | Live status/transcript display; publishes stop, cancel and copy intents |
| Editor panel | `plugin/voice-prompt/editor.luau` | Text editing, Clean/Prompt, copy, history, diagnostics |
| Widget | `plugin/voice-prompt/widget.luau` | Compact bar entry point that publishes intents |
| Helper supervisor | `helper/src/voiceprompt/supervisor.py` | Owns runtime socket and lease, serializes commands, orders events |
| Engine | `helper/src/voiceprompt/engine.py` | Session state machine, capture/VAD orchestration, worker lifecycle |
| Inference worker | `helper/src/voiceprompt/worker.py` | Spawn-isolated process that loads one CT2 model and decodes jobs |
| Capture | `helper/src/voiceprompt/capture.py`, `pcm.py` | Owned `pw-record` child and raw mono float32 decoding |
| VAD and streaming | `helper/src/voiceprompt/vad.py`, `streaming.py` | Silero V6 gating and LocalAgreement-2 commit |
| Model setup | `helper/src/voiceprompt/models.py`, `model_download.py` | Local validation, pinned-profile download and publication |
| Installer | `scripts/install.sh` | Builds/installs the isolated helper environment and pinned model |

## Ownership and data flow

The service is the only writer of document and control state. Panels and the widget never start
capture: they publish intents through the shared publisher (`intent.editor`, `intent.hud`,
`intent.widget`) and render the `view` state the service publishes. The service translates intents
into helper commands and helper events into document reducer actions. The helper holds no document
state; history lives in a private root under the plugin data directory and is read/written through
staging commands.

1. A panel or the widget enqueues an action with its publisher sequence and current session/document
   fences.
2. The service consumes the action, reduces the document, or sends `configure` then `start` with a
   fresh session id.
3. The supervisor validates and serializes the command; the engine opens capture and admits the
   model worker.
4. The helper emits events (`capture_started`, `model_loading`, `model_ready`, `partial`,
   `finalizing`, `final`, `cancelled`, `error`).
5. The service matches each event to its session, updates document/control state and republishes
   `view` for the panels.

## Local capture and model workers

- **Capture**: the engine spawns `pw-record --raw --rate=16000 --channels=1 --format=f32
  --sample-count=1920000 -` (adding `--target` only when a microphone is configured). A 120 s
  duration limit stops recording.
- **Session audio**: append-only float32 samples in POSIX shared memory, capped at 1920000 samples
  (120 s). Inference jobs carry slice descriptors, not copies.
- **VAD**: Silero V6 CPU scoring over 512-sample frames, threshold 0.5; speech onset is committed
  300 ms before the first positive frame, and an utterance closes after 700 ms below threshold with
  300 ms trailing padding.
- **Scheduler**: one running decode plus one replaceable pending descriptor per session, so slow
  CPU/CUDA inference keeps capturing. Live offers use an adaptive cadence of at least 0.8 s and mark
  the session `degraded` once the live window reaches 30 s.
- **Worker**: a spawn-isolated child owns the model memory and performs serial decodes. Idle unload
  (`idle_unload_seconds`, default 120) terminates that worker, not the lightweight supervisor. CPU
  is the baseline; CUDA and evdev are optional and never required to import or run the CPU path.
- **Models**: CT2 directories are validated locally; pinned profiles are `cpu-small` (int8 CPU) and
  `cuda-turbo` (int8_float16 CUDA), with user-selected `custom` paths allowed. CUDA is probed in a
  fresh child process; the supervisor startup path never imports it.

## Supervisor lease and protocol

- **Ownership**: a 0700 runtime directory (default `$XDG_RUNTIME_DIR/voiceprompt`) with a 0600
  `control.sock` and a 0600 `owner.lock` held under an exclusive `flock`. Only the socket inode this
  process created is unlinked on exit; the lock inode is preserved across reloads.
- **Peer check**: Linux `SO_PEERCRED` rejects a connecting UID other than the helper's.
- **Lease**: the lease carries a 10 s TTL from the last `renew_lease`; the plugin renews every 2 s.
  Lease expiry, SIGINT/SIGTERM or a broken stdout stream stops the helper and releases its runtime.
- **Serialization**: mutating commands run under one dispatch lock. `renew_lease` and `status` stay
  responsive while a mutation is pending and change neither session nor configuration state.
- **Idempotency**: requests are keyed by `request_id`. A 256-entry fingerprint cache replays
  identical retries, and in-flight identities are reserved before any await; reusing an id with
  different content is `request_conflict`.
- **Event stream**: one flushed NDJSON message per line on stdout with strictly increasing `seq`. A
  bounded 64-event queue plus a nonblocking writer means stdout backpressure can drop only
  skippable `partial`/`heartbeat` events; overflow of an essential event stops the helper rather
  than growing memory.

## UI state, history and LLM fencing

- **Document reducer** (`lib/document.luau`) holds `raw`, `current`, `document_version`,
  `input_generation`, a 10-entry undo stack, an optional proposal and the in-flight request.
  Actions are `final`, `edit`, `replace`, `clear`, `undo`, `restore`, `apply_proposal`,
  `discard_proposal`, `cancel_request` and `delete`. Text is capped at 65536 UTF-8 bytes.
- **Intents** (`lib/intents.luau`): each panel creates a publisher with a monotonically increasing
  sequence. The service consumes actions per publisher, rejects sequence gaps and coalesces
  consecutive edits. A rejected edit is still consumed so later edits and toolbar actions apply.
- **History** (`lib/history.luau`): records `{session_id, raw, current, timestamp}`, at most 10,
  persisted through helper staging. A revision counter tracks which snapshot reached disk, so only
  the saved revision clears dirtiness and a newer edit stays dirty. Records created in memory win
  over file contents after an owner replacement.
- **LLM fencing** (`lib/llm.luau` and `lib/document.luau`): every Clean/Prompt request carries a
  token and a 120 s deadline. A response applies only while document, session, token and deadline
  all still match; if the document changed meanwhile, the result becomes a proposal instead of
  overwriting it. Responses are capped at 64 KiB of text and stream over SSE with
  `follow_redirects=false` and no insecure TLS; `/models` uses a 30 s deadline.

The API key is the native Noctalia `llm_api_key` setting. The helper has no credential command and
never receives the key; the service sends it only in native HTTP headers.

## Source map

| Area | Files |
| --- | --- |
| Plugin state and control | `service.luau`, `lib/document.luau`, `lib/intents.luau`, `lib/config.luau`, `lib/contracts.luau` |
| Plugin UI | `hud.luau`, `editor.luau`, `widget.luau` |
| Helper transport | `supervisor.py`, `ownership.py`, `protocol.py` |
| Recording pipeline | `engine.py`, `capture.py`, `pcm.py`, `audio_buffer.py`, `vad.py`, `streaming.py`, `scheduler.py` |
| Inference | `worker.py`, `backend.py`, `cuda_runtime.py` |
| Model setup | `models.py`, `model_download.py`, `diagnostics.py` |
| Persistence | `private_files.py` (staging and history store), `lib/history.luau` |
| LLM requests | `lib/llm.luau` |
| CLI entry point | `cli.py` |

See [protocol.md](protocol.md) for the command/event reference, [install.md](install.md) for
installation, [benchmark.md](benchmark.md) for quality measurement and [privacy.md](privacy.md) for
data handling.
