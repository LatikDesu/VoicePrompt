# Installation

VoicePrompt ships as three separately installed artifacts:

- the Noctalia plugin (Luau/UI, manifest, translations) from its plugin source;
- the `voiceprompt-helper` Python package in an isolated user environment;
- the local speech-recognition model in a user cache.

The community store installs only the plugin. It does not install Python, PipeWire, the helper or model weights, and these instructions use only local artifacts from the received checkout. Development and validation commands live in [helper/README.md](../helper/README.md); this page covers user installation.

## Requirements

| Component | Requirement |
| --- | --- |
| Platform | Linux x86_64, Wayland session |
| Noctalia | v5 with plugin API 32 (only for desktop hookup; the helper installs without it) |
| Audio | PipeWire with `pw-record` and `wpctl` on PATH, plus an accessible default input source |
| `uv` | Required. The installer fails without it and uses no system `pip`, `sudo` or `curl \| sh` |
| Python | CPython `>=3.13,<3.14`, created automatically by `uv`; the system Python is never modified |
| Network | Needed to fetch missing Python/dependency packages and models during setup or updates. Local dictation works offline; remote LLM processing needs its endpoint |

Noctalia declares `dependencies = ["voiceprompt-helper", "pw-record", "wpctl"]` as metadata only: enabling the plugin never installs or validates them. You provide `uv`, PipeWire and Noctalia yourself; the installer creates the isolated Python environment and prepares the model automatically.

## Standard install

From the received checkout:

```sh
bash scripts/install.sh
```

The script performs these steps:

1. Selects the helper wheel declared by `helper/pyproject.toml` (building one with `uv build` from the local checkout when no matching wheel and CPU constraints are present).
2. Creates a fresh isolated environment with `uv venv --python 3.13 --relocatable` and installs the wheel under the generated CPU dependency constraints with `uv pip install --constraint`; an existing working environment is left untouched until the staged one starts.
3. Prepares the pinned `cpu-small` model and smoke-loads it in a child process, which never opens the microphone and never records audio. The model is downloaded once if absent.
4. Replaces the existing helper environment only after all of the above succeeds.
5. When a Noctalia shell is running, registers the plugin source and enables the plugin. A pre-existing source with the same name but another path is not overwritten, and the helper/model install still counts as successful if hookup fails.
6. Retains recovery assets (wheel, CPU constraints, the recorded wheel selection, and `install.sh`) under the install root so a later repair needs no checkout.

Running `bash scripts/install.sh --no-plugin` performs steps 1–4 and 6 without touching plugin registration or the plugin source. It is only for rebuilding or repairing the helper of an already installed plugin; it is not a first install and not a full update (see [update.md](update.md)).

## Default paths

All paths honor the XDG environment:

| Purpose | Path |
| --- | --- |
| Helper executable | `${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt/helper/bin/voiceprompt-helper` |
| Plugin source | `${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt/plugin/voice-prompt` |
| Recovery assets | `${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt/dist/helper`, `.../scripts/install.sh` |
| Model cache | `${XDG_CACHE_HOME:-$HOME/.cache}/voiceprompt/models/<profile>` |

The plugin locates the helper in this order: an explicit `helper_path` override (Advanced settings), then the standard `.../voiceprompt/helper/bin/voiceprompt-helper`, then `voiceprompt-helper` on `PATH`. It reports `helper_not_installed` if none exists.

## Recognition profiles and model preparation

The selected profile is set in **Settings → Plugins → VoicePrompt**; the default is `cpu-small`.

- **`cpu-small`** (baseline) uses the pinned `Systran/faster-whisper-small` revision `536b0662742c02347bc0e980a01041f333bce120` on CPU with `int8`. The model card is MIT-licensed and the prepared download is about 486 MB.
- **`cuda-turbo`** uses the pinned `dropbox-dash/faster-whisper-large-v3-turbo` revision `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` on the CUDA device with `int8_float16`. It needs the optional CUDA dependencies below plus a working NVIDIA driver; there is no silent CPU fallback.
- **`custom`** uses a complete prepared CTranslate2 Whisper folder you select, together with its matching device. It must contain non-empty `model.bin` and `config.json`, plus a valid `tokenizer.json`. It is not checked against a pinned revision; a `.voiceprompt-model.json` provenance file is validated only when present.

Preparation is always explicit, never during ordinary dictation. The installer prepares `cpu-small`. Use the installed helper to prepare any other profile, into the same cache path the plugin reads:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"

# pinned profiles (downloads once, then smoke-loads on the selected device)
"$VP/helper/bin/voiceprompt-helper" setup --device cpu  --model-profile cpu-small  --json
"$VP/helper/bin/voiceprompt-helper" setup --device cuda --model-profile cuda-turbo --json   # after the CUDA extra and driver

# custom folder: validates and smoke-loads, downloads nothing
"$VP/helper/bin/voiceprompt-helper" setup --device cpu --model-profile custom \
  --model-path /absolute/path/to/ct2-model --json
```

`setup` prepares the model, loads it in a child process, and prints a JSON readiness report; download progress goes to stderr. `setup-status` reports readiness without downloading or loading a model. Neither command opens the microphone.

In the editor you can instead select the profile in **Settings → Plugins → VoicePrompt** and use **More → Diagnostics → Download selected model**, which prepares the currently selected pinned profile. For `custom` there is no download: set the profile to `custom`, fill in the Advanced **Local model folder** (`custom_ct2_path`) and **device** settings, then recheck with Diagnostics.

The installed helper is CPU-only by default: only `cpu-small` is prepared, and the default dependency set excludes CUDA and evdev. Their absence is expected and is not an installation error. Dictation loads a prepared local model; it never downloads weights.

## CUDA

The `cuda` extra in `helper/pyproject.toml` is authoritative: `nvidia-cublas-cu12` and `nvidia-cudnn-cu12>=9,<10` (cuDNN pulls NVRTC). Install it into the actual installed helper environment, then prepare the CUDA model:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
uv pip install --python "$VP/helper/bin/python" nvidia-cublas-cu12 'nvidia-cudnn-cu12>=9,<10'
"$VP/helper/bin/voiceprompt-helper" setup --device cuda --model-profile cuda-turbo --json
```

Use `uv pip install` for the extra: it keeps unrelated packages in the environment unless one conflicts; `uv sync` may remove packages that are not part of the project's locked set, so do not use `uv sync` against an installed helper environment.

The extra installs Python CUDA runtime libraries only; the NVIDIA driver comes from your distribution or vendor, and `cuda-turbo` requires `int8_float16` support. An installer run replaces this environment with a fresh CPU-only one, so reinstall the extra afterwards (see [update.md](update.md)).

## evdev (direct keyboard hold)

Direct keyboard hold is optional and uses the `evdev` extra (`evdev>=1.7,<2`) from `helper/pyproject.toml`:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
uv pip install --python "$VP/helper/bin/python" 'evdev>=1.7,<2'
```

It does not change permissions: access to the selected keyboard node is a permission you grant yourself, and no udev rules are written and no user is added to the `input` group. Toggle and compositor Hold do not need it.

## Manual source registration

If Noctalia was not running during a standard installation, register the installed plugin source and enable it once the shell is running. Skip source registration if `voiceprompt` already points to your installed plugin directory:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
noctalia msg plugins source add voiceprompt path "$VP/plugin"
noctalia msg plugins enable latikdesu/voice-prompt
```

Set **Helper executable** only if the executable is neither at the standard path nor on `PATH`.

## Offline installation

The project-generated constraints file pins exact versions and hashes. On a connected machine, prepare the helper wheel, its compatible dependency wheelhouse, a matching CPython 3.13 runtime and the model files. Use only wheels built for the target interpreter, architecture and libc. In the disconnected environment:

```sh
uv venv --python 3.13 --relocatable /absolute/path/to/helper
uv pip install --python /absolute/path/to/helper/bin/python \
  --no-index --find-links /absolute/path/to/wheelhouse \
  --constraint /absolute/path/to/cpu-constraints.txt \
  /absolute/path/to/voiceprompt_helper-0.1.0-py3-none-any.whl
```

The wheelhouse must contain every constrained transitive runtime wheel. For the model, transfer a complete prepared folder (`model.bin`, `config.json`, `tokenizer.json`; an optional `.voiceprompt-model.json` is validated only if present), or use the `custom` profile for a model that has no pinned repository identity. Diagnostics never repair an incomplete offline installation with a network request.

## Troubleshooting entry point

**More → Diagnostics** in the editor reports readiness and offers recovery actions: install helper, download/cancel the selected model, refresh microphone state and recheck without restarting Noctalia. Failure reasons and recovery steps are listed before optional explanations. See [SUPPORT.md](../SUPPORT.md) for troubleshooting and [update.md](update.md) for updates, rollback and uninstall.
