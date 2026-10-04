# VoicePrompt

VoicePrompt is a Noctalia plugin for turning speech into editable text. Speech recognition runs locally on your CPU or NVIDIA GPU. You can copy the result as-is, clean it up with an LLM, or turn a dictated task into a prompt.

It does not type into other applications or execute spoken commands. You decide what to edit, send to an LLM, and copy.

## What it does

- Shows live transcription in a recording overlay without taking keyboard focus.
- Opens the final transcript in an editor with Undo and Restore original.
- Runs multilingual Whisper locally: CPU small, CUDA turbo, or a custom CTranslate2 model.
- Provides optional Clean and Prompt actions through an OpenAI-compatible API.
- Supports a panel button, compositor shortcuts, and optional hold-to-record.
- Keeps up to 10 transcripts when history is enabled. History is off by default.

Recording is limited to 120 seconds. Audio stays in memory; VoicePrompt does not save recording files.

## Requirements

- Linux x86_64 with a Wayland session.
- Noctalia v5 with plugin API 32.
- PipeWire, `pw-record`, and WirePlumber's `wpctl`, with a working default input device.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.13 for the isolated helper environment.

CUDA is optional. It requires an NVIDIA driver, compatible CUDA libraries, and the CUDA model. You do not need an LLM account or API key for local dictation.

## Install

Run this from the VoicePrompt source directory:

```sh
bash scripts/install.sh
```

The installer creates an isolated helper, prepares the CPU-small model, and registers the plugin with a running Noctalia shell. It does not use system pip or sudo. Start Noctalia before installation if you want automatic plugin registration.

Open the plugin settings:

```sh
noctalia msg settings-open-plugin latikdesu/voice-prompt
```

Choose `cpu-small` and your recognition language (`ru`, `en`, or `auto`). The microphone is the system default input: select it in Noctalia's audio controls, not in the plugin settings.

For CUDA, a custom model, offline installation, or manual plugin registration, see the [installation guide](docs/install.md).

**The installer creates a CPU-only environment, including when updating an existing installation. CUDA and keyboard-access extras must be installed again afterward.** See [updating VoicePrompt](docs/update.md) before replacing a CUDA installation.

## Record and edit

Add the VoicePrompt widget to a Noctalia bar, or bind this command to a shortcut in your compositor:

```sh
noctalia msg plugin latikdesu/voice-prompt:service all record-toggle
```

1. Click the microphone or invoke the command to start recording.
2. Speak while the overlay shows the live transcript.
3. Click or invoke it again to stop. The microphone closes, then the final transcript opens in the editor.
4. Edit the text and click Copy. Paste it into the application you need.

Right-clicking the widget opens the editor. To open it without recording:

```sh
noctalia msg panel-open latikdesu/voice-prompt:editor
```

The editor's More menu contains Restore original, Clear, Delete, Microphone, Settings, and Diagnostics. Clear can be undone; Delete removes the current recording and its history entry. Closing the editor keeps the current text in memory.

Automatic processing and copying are off by default. You can change the action after transcription in the plugin settings.

## Optional LLM processing

In the plugin settings, enter an OpenAI-compatible base URL ending in `/v1`, a model ID, and an API key if the server requires one. Model IDs can be entered manually; model discovery is not required.

- Clean asks the LLM to fix punctuation and recognition mistakes without changing the meaning.
- Prompt reorganizes the dictated task into a prompt; it does not carry out that task.

These actions send the current text, not audio, history, or the contents of other applications. The endpoint must support streamed chat completions. Only a completed response is applied; if you edit while processing, the result becomes a proposal you can apply or discard.

The API key is visible and stored as plain text in Noctalia's settings. Do not share those settings or their backups. Use HTTPS for remote servers; plaintext HTTP is restricted to local endpoints. Read the [privacy guide](docs/privacy.md) before sending sensitive text.

## Help and development

If recording is unavailable, open More → Diagnostics. It reports helper, model, audio, and CUDA problems for the selected profile. See [troubleshooting](SUPPORT.md) for recovery steps.

- [Installation and model setup](docs/install.md)
- [Updates and removal](docs/update.md)
- [Plugin controls and settings](plugin/voice-prompt/README.md)
- [Helper development and CLI](helper/README.md)
- [Architecture](docs/architecture.md) and [helper protocol](docs/protocol.md)
- [Recognition benchmarking](docs/benchmark.md)

The plugin lives in `plugin/voice-prompt/`, the Python helper in `helper/`, and native Luau regression scenarios in `tests/`. Model files and Python environments are installed separately; they are not bundled with the plugin.

## License

[MIT](LICENSE). Dependency and model notices are listed in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
