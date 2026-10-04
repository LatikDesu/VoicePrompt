"""Session-fenced bridge from PipeWire capture/VAD to one serial local STT worker."""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

from .audio_buffer import SessionAudio
from .backend import WhisperBackend
from .capture import Capture
from .models import ModelError, ModelRef, check_profile, supports_language, validate_local
from .scheduler import DecodeJob, Scheduler
from .streaming import Agreement, Hypothesis
from .vad import VadGate
from .worker import Worker, WorkerError


_DEFAULT_CONFIG = {
    "config_schema": 1,
    "language": "ru",
    "device": "cpu",
    "model_profile": "cpu-small",
    "model_path": "",
    "microphone_target": "",
    "glossary": "",
    "model_load_timeout_seconds": 120,
    "final_timeout_seconds": 120,
    "idle_unload_seconds": 120,
    "preload": False,
}


class Engine:
    def __init__(self, emit, owner_generation: str, config: dict | None = None) -> None:
        if not callable(emit) or not isinstance(owner_generation, str) or not owner_generation:
            raise ValueError("engine_identity_invalid")
        self.emit = emit
        self.owner_generation = owner_generation
        self._config = dict(_DEFAULT_CONFIG)
        self._preload_pending = False
        self._preload_token = 0
        self._preload_task: asyncio.Task | None = None
        self._config_cache: dict[str, tuple[tuple, ModelRef, dict[str, bool | None]]] = {}
        self.capabilities: list[str] = []
        self._runtime_ready = False
        self._vad_gate: VadGate | None = None
        self._worker: Worker | None = None
        self._worker_ready = False
        self._worker_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._tick_lock = asyncio.Lock()
        self._idle_since: float | None = None
        self._closed = False
        self._session_counter = 0
        self._session_token = 0
        self._job_counter = 0
        self._session_id: str | None = None
        self._session_config: dict | None = None
        self._vad_verdict: asyncio.Event | None = None
        self._audio: SessionAudio | None = None
        self._capture: Capture | None = None
        self._capture_start_task: asyncio.Task | None = None
        self._capture_error: str | None = None
        self._capture_started = False
        self._capture_start_deadline: float | None = None
        self._model_load_deadline: float | None = None
        self._stop_requested = False
        self._final_requested = False
        self._state = "idle"
        self._vad: VadGate | None = None
        self._scheduler: Scheduler | None = None
        self._agreement: Agreement | None = None
        self._last_live_offer_at = 0.0
        self._last_live_offer_end = 0
        self._live_window_start = 0
        self._degraded_reported = False
        self._inflight_started: float | None = None
        self._inflight_kind: str | None = None
        if config is not None:
            self.configure(config)

    def configure(self, config: dict) -> None:
        """Validate and snapshot configuration for the next recording session."""
        if self._closed:
            raise ValueError("engine_closed")
        if not isinstance(config, dict):
            raise ValueError("configuration_invalid")
        candidate = {**self._config, **config}
        if type(candidate.get("config_schema")) is not int or candidate["config_schema"] != 1:
            raise ValueError("config_incompatible")
        if candidate.get("language") not in {"ru", "en", "auto"}:
            raise ValueError("configuration_invalid")
        if candidate.get("device") not in {"cpu", "cuda"}:
            raise ValueError("configuration_invalid")
        profile = candidate.get("model_profile")
        if profile not in {"cpu-small", "cuda-turbo", "custom"}:
            raise ValueError("configuration_invalid")
        if (profile == "cpu-small" and candidate["device"] != "cpu") or (
            profile == "cuda-turbo" and candidate["device"] != "cuda"
        ):
            raise ValueError("configuration_invalid")
        for key in ("model_path", "custom_ct2_path", "microphone_target", "glossary"):
            value = candidate.get(key, "")
            if value is None and key in {"model_path", "custom_ct2_path"}:
                continue
            if not isinstance(value, str):
                raise ValueError("configuration_invalid")
            maximum = 8192 if key == "glossary" else 1024
            if len(value.encode("utf-8")) > maximum:
                raise ValueError("configuration_invalid")
        for key in ("model_load_timeout_seconds", "final_timeout_seconds"):
            value = candidate.get(key, 120)
            if type(value) is not int or not 1 <= value <= 3600:
                raise ValueError("configuration_invalid")
        idle_timeout = candidate.get("idle_unload_seconds", 120)
        if type(idle_timeout) is not int or not 0 <= idle_timeout <= 3600:
            raise ValueError("configuration_invalid")
        if type(candidate.get("preload")) is not bool:
            raise ValueError("configuration_invalid")
        if "compute_type" in candidate and candidate["compute_type"] not in {"int8", "int8_float16"}:
            raise ValueError("configuration_invalid")
        self._config = candidate
        self._preload_pending = candidate.get("preload", False)
        # A configuration change invalidates any in-flight preload validation.
        self._preload_token += 1

    async def prepare_runtime(self) -> None:
        """Load/validate CPU STT runtime and Silero CPU VAD, but no STT model or CUDA."""
        if self._runtime_ready:
            return
        try:
            cpu_types = WhisperBackend.compute_types("cpu")
            if "int8" not in cpu_types:
                raise ModelError("cpu_runtime_unavailable")
            vad = VadGate()
        except ModelError:
            raise
        except Exception:
            raise ModelError("vad_runtime_unavailable") from None
        self._vad_gate = vad
        self.capabilities = ["cpu_stt"]
        self._runtime_ready = True

    def _model_fingerprint(self, path: Path) -> tuple:
        try:
            return tuple(
                (name, (path / name).stat().st_size, (path / name).stat().st_mtime_ns,
                 (path / name).stat().st_ino)
                for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json",
                             ".voiceprompt-model.json")
                if (path / name).is_file()
            )
        except OSError:
            raise ModelError("model_missing") from None
    @staticmethod
    def _model_directory(config: dict) -> Path | None:
        """Cheap pre-check: the configured model folder, or None before any hashing."""
        raw_path = config.get("model_path") or config.get("custom_ct2_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = Path(raw_path).expanduser()
        return path if path.is_dir() else None

    def _resolve_model(self, config: dict) -> ModelRef:
        raw_path = config.get("model_path") or config.get("custom_ct2_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ModelError("model_missing")
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise ModelError("model_missing")
        path = path.resolve()
        fingerprint = self._model_fingerprint(path)
        cache = self._config_cache.get(str(path))
        if cache is not None and cache[0] == fingerprint:
            ref, languages = cache[1], cache[2]
        else:
            ref = validate_local(path)
            languages = {language: supports_language(ref, language) for language in ("ru", "en")}
            self._config_cache[str(path)] = (fingerprint, ref, languages)
        language = config["language"]
        if language in {"ru", "en"} and languages.get(language) is False:
            raise ModelError("model_language_unsupported")
        check_profile(ref, config["model_profile"])
        return ref

    @staticmethod
    def _profile_compute_type(config: dict) -> str:
        profile = config["model_profile"]
        device = config["device"]
        if profile == "cpu-small":
            return "int8"
        if profile == "cuda-turbo":
            return "int8_float16"
        default = "int8" if device == "cpu" else "int8_float16"
        selected = config.get("compute_type", default)
        if (device == "cpu" and selected != "int8") or (
            device == "cuda" and selected != "int8_float16"
        ):
            raise ModelError("compute_type_unsupported")
        return selected

    def _worker_config(self, ref: ModelRef, compute_type: str, session_id: str) -> dict:
        session = self._session_config or self._config
        return {
            "owner_generation": self.owner_generation,
            "session_id": session_id,
            "repo": ref.repo,
            "revision": ref.revision,
            "model_path": str(ref.local_path),
            "checksum_manifest": dict(ref.checksum_manifest),
            "device": session["device"],
            "compute_type": compute_type,
        }

    async def start(self, session_id: str) -> None:
        if self._closed:
            raise ValueError("engine_closed")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_invalid")
        if self._session_id is not None:
            if self._session_id == session_id:
                return
            raise ValueError("session_busy")

        config = dict(self._config)
        if self._model_directory(config) is None:
            raise ModelError("model_missing")
        compute_type = self._profile_compute_type(config)
        if not self._runtime_ready:
            await self.prepare_runtime()
        if self._session_id is not None:
            if self._session_id == session_id:
                return
            raise ValueError("session_busy")

        self._session_counter += 1
        self._session_token += 1
        self._preload_token += 1  # a recording session supersedes idle preload
        token = self._session_token
        self._session_id = session_id
        self._session_config = config
        self._audio = SessionAudio()
        self._vad = self._vad_gate
        self._vad.reset()
        self._scheduler = Scheduler()
        self._agreement = Agreement()
        self._capture_error = None
        self._capture_started = False
        self._capture_start_deadline = time.monotonic() + 10.0
        # Bounds the whole open+validate+load phase, so a slow or hung hash can
        # never leave a stopped session waiting for a worker that never arrives.
        self._model_load_deadline = time.monotonic() + config["model_load_timeout_seconds"]
        self._vad_verdict = asyncio.Event()
        self._stop_requested = False
        self._final_requested = False
        self._state = "starting"
        self._last_live_offer_at = 0.0
        self._last_live_offer_end = 0
        self._live_window_start = 0
        self._degraded_reported = False
        self._inflight_started = None
        self._inflight_kind = None

        capture = Capture(
            lambda samples, current=token: self._on_samples(current, samples),
            on_started=lambda current=token: self._on_capture_started(current),
            on_error=lambda code, current=token: self._on_capture_error(current, code),
        )
        self._capture = capture
        task = asyncio.create_task(
            self._open_capture(token, capture, compute_type, config),
            name=f"voiceprompt-capture-open-{self._session_counter}",
        )
        self._capture_start_task = task
        task.add_done_callback(
            lambda finished, current=token: self._opening_settled(current, finished)
        )

    def _opening_settled(self, token: int, task: asyncio.Task) -> None:
        """Retrieve opening failures that no longer have an awaiting owner."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and self._is_current(token):
            self._capture_error = getattr(error, "code", "capture_failed")

    async def _open_capture(
        self, token: int, capture: Capture, compute_type: str, config: dict
    ) -> None:
        try:
            await asyncio.wait_for(
                capture.start(config.get("microphone_target") or None), timeout=10.0,
            )
        except asyncio.TimeoutError:
            if self._is_current(token):
                self._capture_error = "capture_start_timeout"
            capture.stop()
            return
        except Exception as error:
            if self._is_current(token):
                self._capture_error = getattr(error, "code", str(error) or "capture_failed")
            capture.stop()
            return
        if not self._is_current(token):
            capture.stop()
            return
        if capture.process is None:
            return
        if self._stop_requested and not await self._worker_needed(token):
            return
        # Hashing model weights must not delay capture or block its PCM reader.
        # No inference worker is admitted until full integrity validation succeeds.
        try:
            ref = await asyncio.to_thread(self._resolve_model, config)
        except ModelError as error:
            if self._is_current(token):
                self._capture_error = error.code
                capture.stop()
            return
        if not self._is_current(token):
            return
        if self._stop_requested and not await self._worker_needed(token):
            return
        await self._ensure_worker(token, ref, compute_type, self._session_id, config)

    async def _ensure_worker(
        self, token: int, ref: ModelRef, compute_type: str, session_id: str, config: dict
    ) -> None:
        async with self._worker_lock:
            signature = (
                ref.repo, ref.revision, str(ref.local_path),
                tuple(sorted(ref.checksum_manifest.items())), config["device"], compute_type,
            )
            if (self._worker is not None and self._worker.config_signature == signature
                    and self._worker.is_alive):
                if self._worker_ready:
                    self._state = "finalizing" if self._stop_requested else (
                        "recording" if self._capture_started else "starting"
                    )
                    self.emit("model_ready", session_id=session_id)
                    self._dispatch_next()
                else:
                    self._state = "loading"
                    self.emit("model_loading", session_id=session_id)
                return
            if self._worker is not None:
                await self._retire_worker_unlocked()
            if not self._is_current(token):
                return
            worker = Worker()
            self._worker = worker
            self._worker_ready = False
            self._model_load_deadline = time.monotonic() + config["model_load_timeout_seconds"]
            self._state = "loading"
            self.emit("model_loading", session_id=session_id)
            try:
                worker.start(self._worker_config(ref, compute_type, session_id))
            except WorkerError as error:
                self._capture_error = error.code
    def _is_current(self, token: int) -> bool:
        return token == self._session_token and self._session_id is not None

    async def _worker_needed(self, token: int) -> bool:
        """True only once stop flushed the final frame and that flush found speech."""
        verdict = self._vad_verdict
        if verdict is not None:
            await verdict.wait()
        if not self._is_current(token):
            return False
        return self._vad is not None and self._vad.speech_detected

    def _settle_vad_verdict(self) -> None:
        if self._vad_verdict is not None:
            self._vad_verdict.set()

    def _on_capture_started(self, token: int) -> None:
        if not self._is_current(token):
            return
        self._capture_started = True
        if self._worker_ready:
            self._state = "finalizing" if self._stop_requested else "recording"
        elif self._state != "loading":
            self._state = "starting"
        self.emit("capture_started", session_id=self._session_id)

    def _on_capture_error(self, token: int, code: str) -> None:
        if self._is_current(token):
            self._capture_error = code

    def _on_samples(self, token: int, samples) -> None:
        if not self._is_current(token) or self._audio is None or self._vad is None:
            return
        try:
            start = self._audio.samples_written
            accepted = self._audio.append(samples)
            if accepted:
                completed = self._vad.push(samples[:accepted], start)
                self._maybe_offer_live(time.monotonic(), utterance_end=bool(completed))
        except Exception as error:
            self._capture_error = getattr(error, "code", "audio_invalid")
            if self._capture is not None:
                self._capture.stop()

    def _new_job(self, kind: str, start_sample: int, end_sample: int) -> DecodeJob:
        if self._audio is None or self._session_id is None or self._scheduler is None:
            raise ValueError("session_unavailable")
        audio = self._audio.snapshot(start_sample, end_sample)
        config = self._session_config or self._config
        prompt = self._agreement.committed_text if kind == "live" and self._agreement else ""
        options = {
            "language": config["language"],
            "prompt": prompt[-4096:],
            "hotwords": config.get("glossary", ""),
        }
        self._job_counter += 1
        return DecodeJob(
            self.owner_generation, self._session_id,
            f"{self.owner_generation}:{self._job_counter}", kind, audio, options,
        )

    def _maybe_offer_live(self, now: float, utterance_end: bool = False) -> None:
        if (self._stop_requested or self._session_id is None or self._audio is None
                or self._vad is None or self._scheduler is None or self._agreement is None
                or (not self._vad.speech_active and not utterance_end) or self._agreement.degraded):
            return
        speech_start = self._vad.segment_start
        end = self._audio.samples_written
        if not utterance_end and (speech_start is None or end - speech_start < 16_000):
            return
        minimum_new = end - self._last_live_offer_end >= 8_000
        cadence = max(0.8, self._scheduler.cadence_s)
        if end <= self._last_live_offer_end or (not utterance_end and (not minimum_new or now - self._last_live_offer_at < cadence)):
            return
        window_start = self._live_window_start
        if end - window_start >= 480_000:
            self._agreement.degraded = True
            if not self._degraded_reported:
                self._degraded_reported = True
                self.emit(
                    "partial", session_id=self._session_id,
                    committed_text=self._agreement.committed_text,
                    tentative_text=self._agreement.tentative_text,
                    audio_end_ms=end // 16, degraded=True,
                )
            return
        job = self._new_job("live", window_start, end)
        self._scheduler.offer_live(job)
        self._last_live_offer_at = now
        self._last_live_offer_end = end

    def _dispatch_next(self) -> None:
        if (self._worker is None or not self._worker_ready or self._scheduler is None
                or self._session_id is None):
            return
        job = self._scheduler.next_job()
        if job is None:
            return
        if job.owner_generation != self.owner_generation or job.session_id != self._session_id:
            self._scheduler.complete(job.job_id, 0.8)
            return
        try:
            self._worker.submit(job)
        except WorkerError as error:
            self._capture_error = error.code
            return
        self._inflight_started = time.monotonic()
        self._inflight_kind = job.kind

    async def stop(self, session_id: str) -> None:
        async with self._stop_lock:
            if self._session_id is None or session_id != self._session_id:
                return
            if self._final_requested:
                return
            self._stop_requested = True
            self._state = "finalizing"
            if self._scheduler is not None:
                self._scheduler.stopping = True
                if self._scheduler.pending is not None and self._scheduler.pending.kind == "live":
                    self._scheduler.pending = None
            capture = self._capture
            if capture is not None:
                capture.stop()
            self.emit("finalizing", session_id=session_id)
            if capture is not None:
                try:
                    await capture.wait_closed(timeout=3)
                except ValueError as error:
                    self._capture_error = getattr(error, "code", str(error))
            if self._session_id != session_id:
                return
            if self._capture_error:
                await self._abort_session(self._capture_error, self._session_token)
                return
            if self._audio is None or self._vad is None or self._scheduler is None:
                await self._abort_session("session_unavailable", self._session_token)
                return
            try:
                self._vad.flush(self._audio.samples_written)
            except Exception as error:
                await self._abort_session(getattr(error, "code", "vad_failed"), self._session_token)
                return
            # The flushed frame is the last word on speech: release any opening task
            # that is waiting for this verdict before deciding on a worker.
            self._settle_vad_verdict()
            if not self._vad.speech_detected:
                self._scheduler.cancel_session(session_id)
                self._final_requested = True
                self._emit_final(session_id, "", speech_detected=False)
                return
            final_job = self._new_job("final", 0, self._audio.samples_written)
            self._scheduler.request_final(final_job)
            self._final_requested = True
            if self._worker_ready:
                self._dispatch_next()

    async def cancel(self, session_id: str) -> None:
        async with self._stop_lock:
            if self._session_id is None or session_id != self._session_id:
                return
            self._session_token += 1
            self._stop_requested = True
            self._state = "finalizing"
            if self._scheduler is not None:
                self._scheduler.cancel_session(session_id)
            capture = self._capture
            if capture is not None:
                capture.stop()
            if capture is not None:
                try:
                    await capture.wait_closed(timeout=3)
                except ValueError:
                    pass
            await self._retire_worker()
            if self._audio is not None and not self._audio.closed:
                self._audio.close()
            self._reset_session()
            self.emit("cancelled", session_id=session_id)

    async def unload(self) -> None:
        # Unload supersedes both queued intent and validation already in flight.
        self._preload_token += 1
        self._preload_pending = False
        if self._session_id is not None:
            await self.cancel(self._session_id)
        await self._retire_worker()
        self._idle_since = None

    async def close(self) -> None:
        if self._closed:
            return
        await self.unload()
        self._closed = True
        self._preload_token += 1
        preload = self._preload_task
        if preload is not None and not preload.done():
            preload.cancel()
            await asyncio.gather(preload, return_exceptions=True)

    def _emit_final(self, session_id: str, text: str, speech_detected: bool) -> None:
        try:
            self.emit("final", session_id=session_id, text=text, speech_detected=speech_detected)
        finally:
            if self._audio is not None and not self._audio.closed:
                self._audio.close()
            self._reset_session()
            if self._worker_ready:
                self._idle_since = time.monotonic()

    def _reset_session(self) -> None:
        self._session_token += 1
        self._session_id = None
        self._session_config = None
        self._audio = None
        self._capture = None
        opening = self._capture_start_task
        self._capture_start_task = None
        if opening is not None and not opening.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if opening is not current:
                opening.cancel()
        self._capture_error = None
        self._capture_started = False
        self._capture_start_deadline = None
        # Keep the model-load deadline while a worker is still loading, so a
        # sessionless half-loaded worker is retired instead of lingering.
        if self._worker is None or self._worker_ready:
            self._model_load_deadline = None
        # Release (and retire) whatever waited for the final VAD verdict.
        self._settle_vad_verdict()
        self._vad_verdict = None
        self._stop_requested = False
        self._final_requested = False
        self._state = "loading" if self._worker is not None and not self._worker_ready else "idle"
        self._vad = None
        self._scheduler = None
        self._agreement = None
        self._inflight_started = None
        self._inflight_kind = None
        if self._worker_ready and self._idle_since is None:
            self._idle_since = time.monotonic()

    async def _retire_worker_unlocked(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        was_started = worker.pid is not None
        self._worker_ready = False
        try:
            await worker.terminate_and_reap(3.0)
        finally:
            self._model_load_deadline = None
            self._idle_since = None
            if was_started:
                self.emit("worker_unloaded")

    async def _retire_worker(self) -> None:
        async with self._worker_lock:
            await self._retire_worker_unlocked()

    async def _abort_session(self, code: str, token: int) -> None:
        if not self._is_current(token):
            return
        session_id = self._session_id
        self._stop_requested = True
        self._state = "finalizing"
        if self._scheduler is not None and session_id is not None:
            self._scheduler.cancel_session(session_id)
        capture = self._capture
        if capture is not None:
            capture.stop()
            try:
                await capture.wait_closed(timeout=3)
            except ValueError:
                pass
        await self._retire_worker()
        if self._audio is not None and not self._audio.closed:
            self._audio.close()
        self._reset_session()
        self.emit("error", session_id=session_id, error_code=code)

    def _handle_worker_ready(self, worker: Worker, message: dict) -> None:
        if worker is not self._worker or not worker.is_alive:
            return
        self._worker_ready = True
        self._model_load_deadline = None
        session_id = self._session_id
        if session_id is not None:
            self._state = "finalizing" if self._stop_requested else (
                "recording" if self._capture_started else "starting"
            )
            self.emit("model_ready", session_id=session_id)
            self._dispatch_next()
        else:
            self._state = "idle"
            self._idle_since = time.monotonic()
            self.emit("model_ready")

    async def _handle_worker_message(self, worker: Worker, message: dict) -> None:
        if worker is not self._worker:
            return
        message_type = message["type"]
        if message_type == "model_ready":
            self._handle_worker_ready(worker, message)
            return
        if message_type == "load_error":
            code = message.get("error_code", "model_load_failed")
            if self._session_id is not None:
                await self._abort_session(code, self._session_token)
            else:
                await self._retire_worker()
                self.emit("error", error_code=code)
            return
        if message_type != "decode_result":
            return
        job = self._scheduler.running if self._scheduler is not None else None
        if (job is None or message.get("owner_generation") != self.owner_generation
                or message.get("session_id") != self._session_id
                or message.get("job_id") != job.job_id
                or job.owner_generation != self.owner_generation):
            return
        duration = message.get("timings", {}).get("inference_s", 0.8)
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            duration = 0.8
        self._scheduler.complete(job.job_id, float(duration))
        self._inflight_started = None
        self._inflight_kind = None
        if "error_code" in message:
            await self._abort_session(message["error_code"], self._session_token)
            return
        text = message.get("text")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 65_536:
            await self._abort_session("decode_result_invalid", self._session_token)
            return
        if job.kind == "live":
            await self._handle_live_result(job, message)
        else:
            self._emit_final(job.session_id, text, speech_detected=True)

    async def _handle_live_result(self, job: DecodeJob, message: dict) -> None:
        if self._agreement is None:
            return
        words = message.get("words")
        if not isinstance(words, tuple):
            await self._abort_session("decode_result_invalid", self._session_token)
            return
        try:
            snapshot = self._agreement.observe(Hypothesis(
                words=words,
                audio_end_sample=message["audio_end_sample"],
                window_start_sample=message["window_start_sample"],
            ))
        except (KeyError, TypeError, ValueError):
            await self._abort_session("decode_result_invalid", self._session_token)
            return
        if snapshot.safe_trim_sample is not None:
            self._live_window_start = max(self._live_window_start, snapshot.safe_trim_sample)
        if snapshot.degraded:
            self._degraded_reported = True
        self.emit(
            "partial", session_id=job.session_id,
            committed_text=snapshot.committed_text,
            tentative_text=snapshot.tentative_text,
            audio_end_ms=snapshot.audio_end_ms,
            degraded=snapshot.degraded,
        )
        self._dispatch_next()

    async def tick(self, now_monotonic: float) -> None:
        if not isinstance(now_monotonic, (int, float)) or not math.isfinite(now_monotonic):
            raise ValueError("monotonic_time_invalid")
        async with self._tick_lock:
            token = self._session_token
            if (self._session_id is None and self._worker is None and self._preload_task is None
                    and self._preload_pending):
                if self._model_directory(self._config) is None:
                    self._preload_pending = False
                    self.emit("error", error_code="model_missing")
                else:
                    self._preload_pending = False
                    self._preload_token += 1
                    self._preload_task = asyncio.create_task(
                        self._preload(self._preload_token), name="voiceprompt-preload")
            if (self._model_load_deadline is not None and not self._worker_ready
                    and now_monotonic >= self._model_load_deadline):
                if self._session_id is not None:
                    await self._abort_session("model_load_timeout", self._session_token)
                else:
                    await self._retire_worker()
                    self.emit("error", error_code="model_load_timeout")
                return
            if self._session_id is not None:
                if self._capture_error:
                    await self._abort_session(self._capture_error, token)
                    return
                if (self._capture_start_deadline is not None
                        and not self._capture_started and self._capture is not None
                        and not self._capture.closed.is_set()
                        and now_monotonic >= self._capture_start_deadline):
                    await self._abort_session("capture_start_timeout", token)
                    return
                if (self._inflight_started is not None and self._session_config is not None
                        and now_monotonic - self._inflight_started >= self._session_config["final_timeout_seconds"]):
                    timeout_code = "final_timeout" if self._inflight_kind == "final" else "inference_timeout"
                    await self._abort_session(timeout_code, token)
                    return
                if (self._capture is not None and self._capture.closed.is_set()
                        and not self._stop_requested):
                    session_id = self._session_id
                    await self.stop(session_id)
                    if self._session_id is None:
                        return

            worker = self._worker
            if worker is not None:
                try:
                    messages = worker.poll()
                except WorkerError as error:
                    if self._session_id is not None:
                        await self._abort_session(error.code, self._session_token)
                    else:
                        await self._retire_worker()
                    return
                for message in messages:
                    await self._handle_worker_message(worker, message)
                    if worker is not self._worker:
                        return
                if worker is self._worker and not worker.is_alive:
                    if self._session_id is not None:
                        await self._abort_session("worker_exited", self._session_token)
                        return
                    await self._retire_worker()
                    return
                if self._session_id is not None:
                    self._dispatch_next()
                elif (not self._config.get("preload", False) and self._worker_ready and self._idle_since is not None
                      and self._config["idle_unload_seconds"] > 0
                      and now_monotonic - self._idle_since >= self._config["idle_unload_seconds"]):
                    await self._retire_worker()

    def _preload_current(self, token: int) -> bool:
        return (token == self._preload_token and not self._closed
                and self._session_id is None and self._worker is None)

    async def _preload(self, token: int) -> None:
        """Validate and spawn the idle model without blocking command handling."""
        try:
            config = dict(self._config)
            try:
                ref = await asyncio.to_thread(self._resolve_model, config)
                compute_type = self._profile_compute_type(config)
                if not self._runtime_ready:
                    await self.prepare_runtime()
            except ModelError as error:
                if self._preload_current(token):
                    self.emit("error", error_code=error.code)
                return
            # A session, a new configuration or close may have invalidated this
            # validation while it ran: never revive the worker in that case.
            if not self._preload_current(token):
                return
            worker = Worker()
            try:
                worker.start(self._worker_config(
                    ref, compute_type, self.owner_generation + ":preload"))
            except WorkerError as error:
                self.emit("error", error_code=error.code)
                return
            self._worker = worker
            self._worker_ready = False
            self._state = "loading"
            self._model_load_deadline = time.monotonic() + config["model_load_timeout_seconds"]
            self.emit("model_loading")
        finally:
            self._preload_task = None

    def status(self) -> dict:
        model_loaded = bool(self._worker is not None and self._worker_ready and self._worker.is_alive)
        return {
            "state": self._state,
            "model_loaded": model_loaded,
            "capabilities": list(self.capabilities),
            "session_id": self._session_id,
            "capture_started": self._capture_started,
            "speech_detected": bool(self._vad is not None and self._vad.speech_detected),
            "degraded_live": bool(self._agreement is not None and self._agreement.degraded),
            "audio_samples": 0 if self._audio is None else self._audio.samples_written,
        }
