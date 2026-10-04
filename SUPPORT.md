# Troubleshooting

Start with **More → Diagnostics** in the VoicePrompt editor. It checks the selected recognition profile and shows the reason recording is unavailable. After fixing a dependency, model, or audio problem, run Diagnostics again.

Local dictation does not require LLM settings or a global shortcut. Test the microphone button or the `record-toggle` command independently of those features.

## Helper not found

The plugin looks for the helper in this order:

1. The explicit `helper_path` setting, if it is not empty.
2. `$XDG_DATA_HOME/voiceprompt/helper/bin/voiceprompt-helper`, or `$HOME/.local/share/voiceprompt/helper/bin/voiceprompt-helper` when `XDG_DATA_HOME` is unset.
3. `voiceprompt-helper` on PATH.

An explicit override takes precedence even if it points to an old or missing environment. Clear it to use the standard installation, or point it at the executable you actually installed.

If the standard helper is missing, follow the [installation guide](docs/install.md). Install and repair actions do not grant microphone or keyboard permissions.

## Microphone unavailable or no speech detected

VoicePrompt uses the system default PipeWire input. Open **More → Microphone** and select the input in Noctalia's audio controls. Check that it is not muted and that its input level changes while you speak.

`pw-record` must be installed, `wpctl` must be available, and the PipeWire session must be reachable by your user. Changing an LLM key or a keyboard setting will not repair an audio problem.

If capture works but the final text is empty, the speech detector may not have detected speech. Check microphone gain, speak closer to the microphone, and try a longer phrase.

## Model missing or rejected

Choose the profile you intend to use, then download its model through Diagnostics or follow the helper setup commands in the [installation guide](docs/install.md).

- `cpu-small` uses its pinned multilingual small model on CPU.
- `cuda-turbo` uses its pinned turbo model on CUDA.
- `custom` uses the device and local CTranslate2 directory you supply.

Do not point a pinned profile at an arbitrary model directory. A custom directory must contain a converted CTranslate2 model, not the original PyTorch weights. Invalid or incomplete files are rejected instead of being downloaded during recording.

## CPU works but CUDA does not

Selecting `cuda-turbo` does not install CUDA libraries. The helper needs a compatible NVIDIA driver, cuBLAS, cuDNN, and their runtime dependencies inside the environment that the active `helper_path` resolves to.

The standard installer replaces that environment with a CPU-only one. If CUDA stopped working after an update, reinstall the optional libraries in the installed helper, then re-enable the plugin. Follow [CUDA installation](docs/install.md#cuda) and [update instructions](docs/update.md).

A CUDA out-of-memory error requires more free GPU memory or a smaller model. CPU-small is an explicit alternative profile; VoicePrompt does not silently switch devices.

## A shortcut does nothing

VoicePrompt does not choose a global shortcut or edit your compositor configuration. The command to bind is:

```sh
noctalia msg plugin latikdesu/voice-prompt:service all record-toggle
```

If the command works but the key does not, check the compositor binding and whether another action already uses that key combination.

Hold mode needs actual press and release events from the compositor. Direct keyboard Hold also needs the optional `evdev` package and permission to read the selected keyboard. Use Toggle if you do not want that access; adding your account to a broad keyboard-access group is not required for dictation.

## Clean or Prompt is unavailable

Enter a base URL ending in `/v1` and a model ID in the plugin settings. Enter an API key only if the endpoint requires one. A failed model-list refresh does not prevent you from entering a model ID manually.

Remote endpoints must use HTTPS. The server must support OpenAI-compatible streamed chat completions. Authentication failures, provider errors, incomplete responses, and the 120-second processing deadline leave the current document unchanged. If you edited during processing, use Apply or Discard for the returned proposal.

## Reporting a problem

Include your Noctalia and helper versions, recognition profile, Linux distribution, and the error code shown by Diagnostics. Describe what action failed and whether the microphone button and direct IPC command behave differently.

Do not attach API keys, Noctalia settings backups, private transcripts, history files, or recordings unless you deliberately intend to share their contents. Start with the error code and a minimal description rather than a dump of your configuration.
