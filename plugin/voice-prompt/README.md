# VoicePrompt

Local dictation and text preparation for Noctalia. The recording overlay shows live text without taking keyboard focus; the final transcript opens in an editor. Copy it directly, edit it, or use optional Clean and Prompt actions with your own LLM endpoint.

Plugin ID: `latikdesu/voice-prompt`. Requires Noctalia v5 with plugin API 32, Linux/Wayland, PipeWire (`pw-record`, `wpctl`), and the separately installed `voiceprompt-helper` in a Python 3.13 environment. CPU recognition works without CUDA or keyboard-device access.

Installing the plugin alone does not install the helper or model. From the VoicePrompt source directory, run `bash scripts/install.sh` to prepare the standard CPU installation. For the complete installation, CUDA, and update procedures, see the project [README](../../README.md) and [installation guide](../../docs/install.md).

## Recording controls

Add the VoicePrompt widget to a Noctalia bar. Left-click starts or stops recording; right-click opens the editor. The widget is an icon with a status tooltip.

You can bind the same action to a compositor shortcut:

```sh
noctalia msg plugin latikdesu/voice-prompt:service all record-toggle
```

Invoke it once to start and again to stop. The editor does not need to be open. The plugin does not assign a shortcut or change your compositor configuration.

For separate press/release bindings or cancellation, replace `record-toggle` with `record-start`, `record-stop`, or `record-cancel`. Hold mode requires a compositor that sends both press and release events. Advanced `hold-evdev` observes a selected keyboard chord directly and requires the optional package and device access; it does not grab keys or grant permissions for you.

Open the editor without starting a recording:

```sh
noctalia msg panel-open latikdesu/voice-prompt:editor
```

The HUD panel is a display surface, not a start-recording command.

## The editor

Recording ends when you stop it or reach the 120-second limit. The microphone closes before final decoding; the final transcript can differ from the live text.

- Copy sends the current text to the clipboard, without pasting it into another application.
- Clean corrects the text through your LLM endpoint.
- Prompt turns the text into a prompt through your LLM endpoint, without executing the dictated task.
- Undo restores an earlier edit. Restore original returns to the final transcript.
- Apply and Discard appear when an LLM result arrives after you have edited the text.

The More menu contains Restore original, Clear, Delete, Microphone, Settings, and Diagnostics. Clear keeps Undo. Delete removes the current recording and its history entry. Closing the editor retains its text in memory; persistent history is a separate option.

Input and LLM output are limited to 64 KiB of UTF-8 text. An oversized paste is rejected without replacing the current text.

## Settings

Open **Settings → Plugins → VoicePrompt**, or run:

```sh
noctalia msg settings-open-plugin latikdesu/voice-prompt
```

| Setting | Behavior |
| --- | --- |
| Recognition profile | `cpu-small` uses CPU; `cuda-turbo` uses CUDA; `custom` uses your local CT2 directory and selected device. |
| Language | `ru`, `en`, or `auto`; default `ru`. |
| Activation mode | Toggle by default; compositor Hold or direct keyboard Hold are optional. |
| Preload | Off by default. Enable it to load the model in advance and keep it warm. |
| After transcription | Show the editor by default; automatic Clean, Prompt, or Copy are opt-in. |
| Close after Copy | Off by default. |
| History | Off by default; stores up to 10 transcripts when enabled. |
| LLM URL, model, key | Needed only for Clean/Prompt; the URL must end in `/v1`. Model IDs can be entered manually. |

Advanced settings include the helper executable override, custom model directory/device, glossary, model/final deadlines, idle unload timeout, and LLM system prompts. With preload off, the model unloads after 120 idle seconds by default; an idle timeout of `0` keeps it loaded.

The default microphone is selected in **Noctalia's audio controls**. More → Microphone opens those controls; there is no second microphone selector inside VoicePrompt.

## Diagnostics and privacy

More → Diagnostics reports readiness for the selected profile and provides model download/cancel, history, and LLM model-list actions. Use it again after repairing a dependency or changing the selected model.

Dictation stays local. Audio is held in memory and is not saved or uploaded. Clean/Prompt send only the current text to your configured endpoint; they apply a completed response, not partial streamed output, with a 120-second deadline.

The API key is visible and stored as plain text in Noctalia's settings. Do not share the settings or their backups. Remote endpoints require HTTPS; local HTTP is plaintext. Cancelling a request cannot retract text already received by the server.

VoicePrompt runs as trusted plugin code under your user account. Its helper owns the recording/model processes; optional direct keyboard Hold also grants access to the selected keyboard's events. See the project [privacy guide](../../docs/privacy.md) and [troubleshooting](../../SUPPORT.md).
