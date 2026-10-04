# VoicePrompt helper

`voiceprompt-helper` is the local Python component of the VoicePrompt Noctalia plugin. It runs
capture, Silero VAD, the faster-whisper CT2 decoder and model setup in an isolated user
environment; the Luau plugin talks to it over a private socket and NDJSON events. It is installed
separately from the Noctalia plugin.

## Requirements

- Python `>=3.13,<3.14` (provided by `uv`; the system Python is never modified).
- `uv` on `PATH`, PipeWire (`pw-record`) and Noctalia for live use.
- CPU is the baseline. CUDA and evdev are optional extras.

## Install

From the repository root:

```sh
bash scripts/install.sh              # isolated helper environment, pinned CPU model, plugin hookup
bash scripts/install.sh --no-plugin  # helper and model only
```

User installation, updating, offline installs and optional extras are documented in
[../docs/install.md](../docs/install.md) and [../docs/update.md](../docs/update.md).

## Development

Run these from the repository root:

```sh
uv sync --project helper --python 3.13 --group dev
uv run --project helper --no-sync pytest helper/tests -q
uv build --project helper --out-dir dist/helper
uv export --project helper --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file dist/helper/cpu-constraints.txt
```

The environment is `helper/.venv`; the wheel, sdist, and generated CPU constraints are in
`dist/helper/`, where the installer reads them. No system Python packages are changed.

For CUDA development, add `--extra cuda` to `uv sync`. For direct keyboard Hold, add
`--extra evdev`. If you need both, pass both options in the same command. Keep these options on
later syncs: an exact sync removes extras you no longer select. `uv run --no-sync` uses the
environment as prepared instead of synchronizing it again.

Run a helper command from the checkout with
`uv run --project helper --no-sync voiceprompt-helper`, followed by the subcommand.

## Command-line commands

```sh
voiceprompt-helper version
voiceprompt-helper identity
voiceprompt-helper diagnostics --json
voiceprompt-helper setup --device cpu --model-profile cpu-small --json
voiceprompt-helper setup-status --device cpu --model-profile cpu-small --json
voiceprompt-helper benchmark --corpus CORPUS.json --profile cpu-small --model MODEL_DIR --output REPORT.json
```

- `version` prints the installed package, protocol and config schema versions.
- `identity` prints a fresh owner generation without side effects.
- `diagnostics` inspects CPU STT/VAD imports and PipeWire without opening the microphone, loading a
  model or probing optional CUDA/evdev; `--device cuda` adds an isolated CUDA probe.
- `setup` prepares the selected model and smoke-loads it in a child process without capturing
  audio. Pinned models are downloaded or refreshed when needed; custom models use local files.
- `setup-status` reports device/profile/runtime/VAD/model/PipeWire readiness without downloading
  or loading the speech-recognition model.
- `benchmark` replays real corpus audio; see [../docs/benchmark.md](../docs/benchmark.md).

API keys are the native Noctalia `llm_api_key` setting, not a helper command.

## Internal protocol

The Noctalia service owns these two subcommands; they are not a separately enabled daemon:

```text
voiceprompt-helper supervise --owner OWNER_GENERATION [--runtime-dir DIRECTORY]
voiceprompt-helper command [--runtime-dir DIRECTORY] --json REQUEST_JSON
```

`supervise` serves the private control socket and event stream; `command` sends one bounded JSON
request and prints the acknowledgement. The plugin creates the owner generation and runtime
directory itself. Do not paste real transcripts, keys or history JSON into argv: private staging
files carry that data. The full wire format is in [../docs/protocol.md](../docs/protocol.md).

## References

- [Project README](../README.md)
- [Architecture](../docs/architecture.md)
- [Protocol v1](../docs/protocol.md)
- [Installation](../docs/install.md) and [updating](../docs/update.md)
- [Privacy](../docs/privacy.md)
- [Benchmark methodology](../docs/benchmark.md)
