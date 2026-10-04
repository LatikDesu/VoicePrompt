# Recognition benchmark

`voiceprompt-helper benchmark` measures transcription quality of the shipped pipeline by replaying
real, consented WAV files through the same VAD, streaming and CT2 decoding policies the helper uses
live. It is a file-replay tool: it reports text quality and processed-audio throughput, not
microphone-to-editor UX latency. The repository ships no corpus and no measured numbers.

## Corpus and annotations

Use consented recordings stored outside the plugin. The corpus is one JSON file:

```json
{
  "entries": [
    {
      "id": "unique-id",
      "audio_path": "audio/unique-id.wav",
      "split": "tuning",
      "language": "ru",
      "reference_text": "Проверь --dry-run, не удаляй файлы.",
      "critical_tokens": ["--dry-run", "не"]
    }
  ]
}
```

| Field | Requirement |
| --- | --- |
| `id` | Nonempty string, unique across the corpus |
| `audio_path` | Nonempty relative path resolved under the corpus directory; the file must exist |
| `split` | `tuning` or `held_out` |
| `language` | `ru` or `en` |
| `reference_text` | Exact transcript of the recording |
| `critical_tokens` | Strings that must appear in `reference_text` and survive verbatim (paths, commands, numbers, negations) |

Audio must be PCM16 mono 16 kHz WAV, at most 1920000 frames (120 s). Any other channel count,
sample rate, sample width or compression, an unreadable file, or audio longer than 120 s aborts
the replay (`audio_format_unsupported`, `audio_too_long`, `audio_unreadable`). Missing files and
invalid corpus metadata are rejected before model loading. Tune on `tuning` entries only; reserve
`held_out` entries for the final report and never reuse tuning phrases as held-out data.

## Running a replay

```sh
uv run --project helper --no-sync voiceprompt-helper benchmark \
  --corpus /absolute/path/to/consented/corpus.json \
  --model /absolute/path/to/prepared/ct2-small \
  --profile cpu-small \
  --output /absolute/path/to/cpu-report.json
```

`--corpus`, `--model` and `--output` are required; `--profile` currently supports `cpu-small` only
(any other value is `benchmark_profile_unsupported`). The model directory is validated locally
(`validate_local`) before it is used. Missing audio or an invalid corpus fails before weights are
loaded.

## Decoding policy

The replay uses the supplied local model on CPU with `int8` and the shipped Silero V6 VAD:

| Stage | Policy |
| --- | --- |
| Live streaming | `beam_size=1`, word timestamps on, VAD filter off, committed streaming text as the initial prompt, no glossary; context bounded to 128 tokens |
| Final (streaming) | `beam_size=5`, VAD filter on, no live prompt, full original audio |
| Offline baseline | Same as final: `beam_size=5`, VAD filter on, no live prompt, same language and model |

All stages use `temperature=0`, `task=transcribe`, `condition_on_previous_text=false` and the
recording's declared language. The streaming stages use LocalAgreement-2 over original sample
offsets; the offline baseline is not raw unfiltered Whisper. When final decoding enables the bundled
faster-whisper VAD filter, its default parameters apply (`min_silence_duration_ms=2000`,
`speech_pad_ms=400`), so only silence longer than 2 s is removed with 400 ms of speech padding.

## Reported metrics

Each entry reports `streaming_final` and `offline_baseline` text plus:

- `wer` — word error rate over whitespace tokens after NFC + casefold normalization, with
  punctuation retained;
- `cer` — character error rate over the same normalized text;
- `critical_preservation` — per-token exact match with word-boundary guards, so a number or negation
  is never matched as a substring;
- `live_updates`, `file_replay_first_tentative_s`, `file_replay_stop_to_final_s`,
  `processed_audio_s`, `total_file_replay_wall_s` and `processed_audio_to_wall_ratio`.

The top-level report records `created_at`, `method`, `limitations`, `platform`, `versions`, the
`model` identity and the `decode` policy, then the per-entry list. `stable_lag_s` is `null`: stable
word lag needs real speech onset and word-end annotations plus live host timing, which a file replay
cannot supply.

## Scope and limitations

- Replay timestamps (first tentative, stop-to-final) are not microphone-to-text UX latency. Measure
  that with real speech through the Noctalia HUD.
- Encoder duty and peak process-tree RSS/VRAM are not measured by this command; the pinned runtime
  exposes no instrumented encoder phase, and GPU utilization is not a substitute.
- Results are specific to the machine, model revision and corpus reported. Report the corpus size,
  the split used and the platform alongside any numbers.
