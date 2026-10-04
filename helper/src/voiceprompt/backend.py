"""Lazy local faster-whisper backend; this module is imported only in the worker."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .models import ModelError, ModelRef, supports_language
from .streaming import Word


@dataclass(frozen=True, slots=True)
class DecodeResult:
    text: str
    words: tuple[Word, ...]
    timings: dict[str, float | int | str]


class WhisperBackend:
    def __init__(self, model_ref: ModelRef, device: str, compute_type: str) -> None:
        if device not in {"cpu", "cuda"}:
            raise ModelError("device_invalid")
        self.model_ref = ModelRef(model_ref.repo, model_ref.revision, Path(model_ref.local_path),
                                  model_ref.checksum_manifest)
        model_path = self.model_ref.local_path
        if not model_path.is_dir():
            raise ModelError("model_missing")
        if not all((model_path / name).is_file() for name in ("model.bin", "config.json")):
            raise ModelError("model_missing")
        if not (model_path / "tokenizer.json").is_file():
            raise ModelError("tokenizer_missing")
        supported = self.compute_types(device)
        if compute_type not in supported:
            raise ModelError("compute_type_unsupported")
        if device == "cpu" and compute_type != "int8":
            raise ModelError("compute_type_unsupported")
        if device == "cuda" and compute_type != "int8_float16":
            raise ModelError("compute_type_unsupported")
        self._language_support = {
            language: supports_language(self.model_ref, language) for language in ("ru", "en")
        }
        try:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(
                str(self.model_ref.local_path),
                device=device,
                compute_type=compute_type,
                local_files_only=True,
            )
        except ModelError:
            raise
        except Exception as error:
            raise ModelError(self._load_error_code(error, device)) from None
        self.tokenizer = getattr(self.model, "hf_tokenizer", None)
        if self.tokenizer is None or not callable(getattr(self.tokenizer, "encode", None)):
            raise ModelError("tokenizer_unavailable")

    @staticmethod
    def compute_types(device: str) -> set[str]:
        if device not in {"cpu", "cuda"}:
            raise ModelError("device_invalid")
        try:
            import ctranslate2

            return set(ctranslate2.get_supported_compute_types(device))
        except Exception as error:
            if device == "cuda":
                raise ModelError("cuda_unavailable") from None
            raise ModelError("cpu_runtime_unavailable") from None

    @staticmethod
    def _load_error_code(error: Exception, device: str) -> str:
        message = str(error).casefold()
        if isinstance(error, MemoryError) or "out of memory" in message or "bad_alloc" in message:
            return "inference_out_of_memory"
        if device == "cuda" and any(term in message for term in ("cuda", "cudnn", "cublas", "no device")):
            return "cuda_unavailable"
        return "model_load_failed"

    def _encode(self, text: str) -> list[int]:
        if not text:
            return []
        try:
            encoded = self.tokenizer.encode(text)
            ids = encoded.ids if hasattr(encoded, "ids") else encoded
            return [int(value) for value in ids]
        except Exception:
            raise ModelError("tokenizer_unavailable") from None

    def _decode_ids(self, token_ids: list[int]) -> str:
        if not token_ids:
            return ""
        decode = getattr(self.tokenizer, "decode", None)
        if not callable(decode):
            raise ModelError("tokenizer_unavailable")
        try:
            return decode(token_ids)
        except Exception:
            raise ModelError("tokenizer_unavailable") from None

    def _bounded_parts(self, text: str, glossary: str, max_tokens: int) -> tuple[str, str]:
        if type(max_tokens) is not int or not 0 <= max_tokens <= 128:
            raise ValueError("context_token_limit")
        glossary_ids = self._encode(glossary)
        glossary_ids = glossary_ids[:max_tokens]
        prompt_ids = self._encode(text)
        remaining = max_tokens - len(glossary_ids)
        prompt_ids = prompt_ids[-remaining:] if remaining else []
        bounded_glossary = self._decode_ids(glossary_ids)
        bounded_prompt = self._decode_ids(prompt_ids)

        while (
            len(self._encode(bounded_glossary)) + len(self._encode(bounded_prompt)) > max_tokens
            or len(self._encode((bounded_glossary + " " + bounded_prompt).strip())) > max_tokens
        ):
            if prompt_ids:
                prompt_ids = prompt_ids[1:]
                bounded_prompt = self._decode_ids(prompt_ids)
            elif glossary_ids:
                glossary_ids = glossary_ids[:-1]
                bounded_glossary = self._decode_ids(glossary_ids)
            else:
                break

        return bounded_prompt, bounded_glossary

    def decode(
        self,
        audio: np.ndarray,
        kind: str,
        language: str | None,
        prompt: str,
        hotwords: str,
    ) -> DecodeResult:
        if kind not in {"live", "final"}:
            raise ValueError("decode_kind")
        if language not in {"ru", "en", "auto"}:
            raise ValueError("language_invalid")
        if not isinstance(audio, np.ndarray) or audio.ndim != 1 or audio.dtype != np.dtype("float32"):
            raise ValueError("audio_format")
        if audio.size > 1_920_000 or not np.isfinite(audio).all():
            raise ValueError("audio_bounds")
        if audio.size == 0:
            return DecodeResult("", (), {"inference_s": 0.0, "audio_samples": 0})
        if language in {"ru", "en"} and self._language_support[language] is False:
            raise ModelError("model_language_unsupported")

        # Final re-decodes from sample zero; live hypotheses are not preceding context.
        bounded_prompt, bounded_hotwords = self._bounded_parts(prompt if kind == "live" else "", hotwords, 128)
        started = time.perf_counter()
        try:
            segments, info = self.model.transcribe(
                audio,
                language=None if language == "auto" else language,
                task="transcribe",
                beam_size=1 if kind == "live" else 5,
                temperature=0,
                word_timestamps=(kind == "live"),
                condition_on_previous_text=False,
                vad_filter=(kind == "final"),
                initial_prompt=bounded_prompt or None,
                hotwords=bounded_hotwords or None,
            )
            materialized = list(segments)
            elapsed = time.perf_counter() - started
            text_parts: list[str] = []
            words: list[Word] = []
            for segment in materialized:
                segment_text = getattr(segment, "text", None)
                if not isinstance(segment_text, str):
                    raise ModelError("decode_result_invalid")
                text_parts.append(segment_text)
                if kind == "live":
                    for word in getattr(segment, "words", None) or ():
                        start_s = getattr(word, "start", None)
                        end_s = getattr(word, "end", None)
                        word_text = getattr(word, "word", None)
                        if (not isinstance(word_text, str) or not isinstance(start_s, (int, float))
                                or not isinstance(end_s, (int, float))
                                or not math.isfinite(start_s) or not math.isfinite(end_s)
                                or start_s < 0 or end_s < start_s):
                            raise ModelError("decode_result_invalid")
                        start_sample = min(audio.size, max(0, round(start_s * 16_000)))
                        end_sample = min(audio.size, max(start_sample, round(end_s * 16_000)))
                        words.append(Word(word_text, start_sample, end_sample))
            if len("".join(text_parts).encode("utf-8")) > 65_536:
                raise ModelError("transcript_too_large")
            return DecodeResult(
                "".join(text_parts), tuple(words),
                {"inference_s": elapsed, "audio_samples": int(audio.size),
                 "language": str(getattr(info, "language", language or "auto"))},
            )
        except ModelError:
            raise
        except MemoryError:
            raise ModelError("inference_out_of_memory") from None
        except Exception as error:
            message = str(error).casefold()
            if "out of memory" in message or "bad_alloc" in message:
                raise ModelError("inference_out_of_memory") from None
            raise ModelError("inference_failed") from None
