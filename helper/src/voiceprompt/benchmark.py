"""ASR quality metrics; measurements require actual consented speech/reference pairs."""

import re
import unicodedata
from collections.abc import Sequence
import json
import wave
from pathlib import Path

import numpy as np
import importlib.metadata
import os
import platform
import time
from datetime import datetime, timezone

from .models import validate_local
from .backend import WhisperBackend
from .vad import VadGate
from .streaming import Agreement, Hypothesis, Word


class BenchmarkError(ValueError):
    pass


def load_corpus(corpus_path: Path) -> list[dict]:
    """Validate references and actual audio files before loading any model."""
    path = Path(corpus_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise BenchmarkError("corpus_unavailable") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list) or not payload["entries"]:
        raise BenchmarkError("corpus_invalid")
    result = []
    seen = set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict):
            raise BenchmarkError("corpus_invalid")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise BenchmarkError("corpus_duplicate_id")
        seen.add(identifier)
        if (entry.get("split") not in {"tuning", "held_out"}
                or entry.get("language") not in {"ru", "en"}
                or not isinstance(entry.get("reference_text"), str)
                or not isinstance(entry.get("critical_tokens"), list)
                or not all(isinstance(token, str) for token in entry["critical_tokens"])):
            raise BenchmarkError("corpus_invalid")
        audio_file = entry.get("audio_path")
        if not isinstance(audio_file, str) or not audio_file or Path(audio_file).is_absolute():
            raise BenchmarkError("corpus_invalid")
        resolved = (path.parent / audio_file).resolve()
        if not resolved.is_relative_to(path.parent.resolve()):
            raise BenchmarkError("corpus_invalid")
        if not resolved.is_file():
            raise BenchmarkError("audio_missing")
        result.append({**entry, "audio_path": str(resolved)})
    return result


def read_pcm_wave(path: Path) -> np.ndarray:
    """Decode approved 16 kHz mono PCM16 WAV once onto the original 16 kHz timeline."""
    try:
        with wave.open(str(path), "rb") as stream:
            if (stream.getnchannels(), stream.getframerate(), stream.getsampwidth(), stream.getcomptype()) != (1, 16000, 2, "NONE"):
                raise BenchmarkError("audio_format_unsupported")
            if stream.getnframes() > 1920000:
                raise BenchmarkError("audio_too_long")
            raw = stream.readframes(stream.getnframes())
    except (OSError, EOFError, wave.Error) as error:
        raise BenchmarkError("audio_unreadable") from error
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def edit_distance(reference: Sequence, hypothesis: Sequence) -> int:
    """Levenshtein distance with a row bounded by the shorter input."""
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, 1):
        current = [index]
        for column, actual in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (expected != actual)))
        previous = current
    return previous[-1]


def critical_preservation(reference: str, hypothesis: str, tokens: list[str]) -> dict[str, bool]:
    """Preserve exact annotated tokens; never match a number/negation substring."""
    result = {}
    for token in tokens:
        if not isinstance(token, str) or not token:
            raise ValueError("critical_token_invalid")
        expression = r"(?<![\w/.-])" + re.escape(token) + r"(?![\w/.-])"
        if re.search(expression, reference) is None:
            raise ValueError("critical_token_not_in_reference")
        result[token] = re.search(expression, hypothesis) is not None
    return result


def quality_metrics(reference: str, hypothesis: str, critical_tokens: list[str]) -> dict:
    """NFC+casefold whitespace-token WER and character CER; punctuation retained."""
    expected = unicodedata.normalize("NFC", reference).casefold()
    actual = unicodedata.normalize("NFC", hypothesis).casefold()
    words = expected.split()
    word_errors = edit_distance(words, actual.split())
    character_errors = edit_distance(expected, actual)
    return {
        "normalization": "NFC+casefold; whitespace words; punctuation retained; critical tokens exact",
        "word_errors": word_errors, "reference_words": len(words),
        "wer": word_errors / len(words) if words else None,
        "character_errors": character_errors, "reference_characters": len(expected),
        "cer": character_errors / len(expected) if expected else None,
        "critical_preservation": critical_preservation(reference, hypothesis, critical_tokens),
    }


def run(corpus_path: Path, model_path: Path, profile: str, output_path: Path) -> dict:
    """Replay real consented WAV through the shipped VAD/ASR/agreement policies.

    This accelerated file replay measures inference and text quality. It is not a live
    microphone latency benchmark; capture/GUI and annotated word-lag stay unmeasured.
    """
    if profile != "cpu-small":
        raise BenchmarkError("benchmark_profile_unsupported")
    entries = load_corpus(corpus_path)
    model_ref = validate_local(model_path)
    backend = WhisperBackend(model_ref, device="cpu", compute_type="int8")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "accelerated 16kHz PCM16 file replay; original timeline; real Silero/CT2/LocalAgreement",
        "limitations": ["No physical microphone/GUI latency from file replay", "Encoder duty unavailable: pinned runtime has no instrumented encoder phase", "Peak concurrent RSS/VRAM not measured by this command"],
        "platform": {"system": platform.system(), "architecture": platform.machine(), "cpu": platform.processor(),
                     "python": platform.python_version(), "cores": os.cpu_count()},
        "versions": {name: importlib.metadata.version(name) for name in ("faster-whisper", "ctranslate2", "onnxruntime")},
        "model": {"repo": model_ref.repo, "revision": model_ref.revision,
                  "path": str(model_ref.local_path), "device": "cpu", "compute_type": "int8"},
        "decode": {"live_beam": 1, "final_beam": 5, "condition_on_previous_text": False,
                   "live_vad_filter": False, "final_vad_filter": True, "language": "per-corpus-entry"},
        "entries": [],
    }
    for entry in entries:
        samples = read_pcm_wave(Path(entry["audio_path"]))
        gate = VadGate()
        agreement = Agreement()
        last_offer_end, processed_samples, live_window_start = 0, 0, 0
        partial_count = 0
        tentative_at = None
        started = time.perf_counter()
        for offset in range(0, samples.size, 512):
            end = min(samples.size, offset + 512)
            gate.push(samples[offset:end], offset)
            if (gate.speech_active and end >= 16000 and end - last_offer_end >= 12800
                    and end - live_window_start < 480000):
                window_start = live_window_start
                decoded = backend.decode(samples[window_start:end], "live", entry["language"],
                                         agreement.committed_text, "")
                processed_samples += end - window_start
                hypothesis = Hypothesis(tuple(Word(word.text, word.start_sample + window_start,
                                                  word.end_sample + window_start) for word in decoded.words), end, window_start)
                snapshot = agreement.observe(hypothesis)
                if snapshot.committed_text or snapshot.tentative_text:
                    partial_count += 1
                    if tentative_at is None:
                        tentative_at = time.perf_counter() - started
                if snapshot.safe_trim_sample is not None:
                    live_window_start = max(live_window_start, snapshot.safe_trim_sample)
                last_offer_end = end
        gate.flush(samples.size)
        stop_at = time.perf_counter()
        if gate.speech_detected:
            final = backend.decode(samples, "final", entry["language"], "", "")
            processed_samples += samples.size
            text = final.text
        else:
            text = ""
        finalized_at = time.perf_counter()
        offline = backend.decode(samples, "final", entry["language"], "", "")
        report["entries"].append({
            "id": entry["id"], "split": entry["split"], "language": entry["language"],
            "duration_s": samples.size / 16000, "speech_detected": gate.speech_detected,
            "streaming_final": {"text": text, **quality_metrics(entry["reference_text"], text, entry["critical_tokens"])},
            "offline_baseline": {"text": offline.text, **quality_metrics(entry["reference_text"], offline.text, entry["critical_tokens"])},
            "live_updates": partial_count, "file_replay_first_tentative_s": tentative_at,
            "file_replay_stop_to_final_s": finalized_at - stop_at,
            "processed_audio_s": processed_samples / 16000,
            "total_file_replay_wall_s": finalized_at - started,
            "processed_audio_to_wall_ratio": processed_samples / 16000 / max(finalized_at - started, 1e-9),
            "stable_lag_s": None,
        })
    result_path = Path(output_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report

