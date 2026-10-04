# Privacy and permissions

VoicePrompt is trusted, unsandboxed Noctalia code. Installing it grants its scripts filesystem, process, clipboard, network and microphone abilities under your user account. The separately installed `voiceprompt-helper` is a user-owned environment run under the same account. The boundaries below describe what each part touches.

## Audio

- The helper opens the system default PipeWire input only while a recording is active. UI shows **Starting** before capture begins and **Recording** only after it starts.
- Original PCM is bounded in memory to at most 120 seconds and is not written to a permanent audio file by default.
- The supervisor owns and reaps the inference worker; owner-lease expiry or a broken stdout stops capture even after a shell crash.

## Text and network

- Local speech-to-text runs on prepared local model files. It does not upload audio and does not download model weights during transcription; weights are fetched only by an explicit setup action.
- **Clean** and **Prompt** send only the selected current text to the configured OpenAI-compatible `/v1/chat/completions` endpoint as a native SSE request. The answer is buffered and applied only after successful completion, with a 120-second deadline.
- The default action after transcription is to show the editor: there is no automatic LLM request, no automatic Copy and no history by default. `/models` is fetched only on an explicit Refresh. Audio, history, other credentials, window context, tools and auto-paste are never included.

## API key

- `llm_api_key` is visible in **Settings → Plugins → VoicePrompt** and stored openly by Noctalia in its plugin settings storage, including backups. It is not encrypted or masked; do not share those settings files or their backups.
- An empty field omits the Authorization header, which is permitted for endpoints without authentication. The key is never passed to the helper, plugin state, history, logs, shell or argv.

## Transport

- HTTPS verifies certificates and redirects are disabled. An explicitly configured local HTTP endpoint is plaintext.
- Cancellation stops the native stream, but it cannot retract a request the provider has already received.

## History and local files

- History is off by default. With it enabled, at most 10 records store raw text, current text and a timestamp in a private plugin data location; no key or audio is stored. Clearing history is an explicit action, and deleting removes one recording with its history entry. Closing without saving keeps text only in memory until the shell exits.
- Text and history files use a `0700` helper-controlled directory with `0600` files. Noctalia writes text into helper-created private staging, and the helper validates ownership, type, mode and size before consuming it.
- Protocol stdout carries bounded text snapshots during live recognition, not model, audio or secret bytes; stderr carries technical status codes. API keys do not use this transport.
- Same-user processes can still read host settings and private text files.

## Optional keyboard access

- Direct hold (`hold-evdev`) requires the separately installed `evdev` extra and access to a selected keyboard device; installing it technically grants keyboard-read capacity.
- The adapter filters the selected chord, does not grab the keyboard or suppress its key in other applications, and never stores or logs unrelated key events. It does not grant input-group or udev permissions automatically.
- If lock-screen state is unavailable, the 120-second maximum recording duration and the owner lease are the available backstops, not proof of immediate lock cancellation.
