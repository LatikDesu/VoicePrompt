"""Spawn-isolated, serial inference process with bounded descriptor/result queues."""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import queue
from pathlib import Path
from typing import Any

from .models import ModelError, ModelRef
from .scheduler import DecodeJob
from .cuda_runtime import spawn_environment


class WorkerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _publish(result_queue, result: dict[str, Any]) -> bool:
    try:
        result_queue.put(result, timeout=1.0)
        return True
    except (queue.Full, OSError, ValueError):
        return False


def _worker_main(config: dict, command_queue, result_queue) -> None:
    owner = config["owner_generation"]
    startup_session = config["session_id"]
    try:
        model_ref = ModelRef(
            config.get("repo"), config.get("revision"), Path(config["model_path"]),
            config.get("checksum_manifest", {}),
        )
        from .backend import WhisperBackend

        backend = WhisperBackend(model_ref, config["device"], config["compute_type"])
    except ModelError as error:
        _publish(result_queue, {
            "type": "load_error", "owner_generation": owner,
            "session_id": startup_session, "error_code": error.code,
        })
        return
    except Exception:
        _publish(result_queue, {
            "type": "load_error", "owner_generation": owner,
            "session_id": startup_session, "error_code": "model_load_failed",
        })
        return

    if not _publish(result_queue, {
        "type": "model_ready", "owner_generation": owner,
        "session_id": startup_session,
    }):
        return

    while True:
        try:
            message = command_queue.get()
        except (EOFError, OSError, ValueError):
            return
        if message == {"command": "shutdown"}:
            return
        if not isinstance(message, DecodeJob):
            continue
        job = message
        envelope = {
            "type": "decode_result", "owner_generation": job.owner_generation,
            "session_id": job.session_id, "job_id": job.job_id,
        }
        try:
            if job.owner_generation != owner or job.kind not in {"live", "final"}:
                raise ModelError("stale_decode_job")
            samples = job.audio.copy_samples()
            decoded = backend.decode(
                samples,
                job.kind,
                job.options.get("language", "ru"),
                job.options.get("prompt", ""),
                job.options.get("hotwords", ""),
            )
            absolute_words = []
            for word in decoded.words:
                start = job.audio.start_sample + word.start_sample
                end = job.audio.start_sample + word.end_sample
                if not job.audio.start_sample <= start <= end <= job.audio.end_sample:
                    raise ModelError("decode_result_invalid")
                absolute_words.append(type(word)(word.text, start, end))
            envelope.update({
                "text": decoded.text,
                "words": tuple(absolute_words),
                "timings": decoded.timings,
                "window_start_sample": job.audio.start_sample,
                "audio_end_sample": job.audio.end_sample,
            })
        except ModelError as error:
            envelope.update({"error_code": error.code})
        except (OSError, ValueError):
            envelope.update({"error_code": "audio_unavailable"})
        except Exception:
            envelope.update({"error_code": "inference_failed"})
        if not _publish(result_queue, envelope):
            return


class Worker:
    """Async-facing owner of one spawned model process.

    ``start(config)`` spawns and returns without waiting for model load. ``poll()`` drains
    bounded lifecycle/decode envelopes; ``submit(job)`` accepts one descriptor; and
    ``terminate_and_reap(timeout)`` guarantees the process is gone before it returns.
    ``config`` includes owner_generation, session_id, model_path, device, and compute_type.
    """

    def __init__(self) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._commands = None
        self._results = None
        self.owner_generation: str | None = None
        self.startup_session: str | None = None
        self.config_signature: tuple | None = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def start(self, config: dict) -> None:
        if self._process is not None:
            raise WorkerError("worker_already_started")
        required = {"owner_generation", "session_id", "model_path", "device", "compute_type"}
        if not isinstance(config, dict) or not required.issubset(config):
            raise WorkerError("worker_config_invalid")
        if (not isinstance(config["owner_generation"], str) or not config["owner_generation"]
                or not isinstance(config["session_id"], str) or not config["session_id"]):
            raise WorkerError("worker_config_invalid")
        self.owner_generation = config["owner_generation"]
        self.startup_session = config["session_id"]
        self._commands = self._context.Queue(maxsize=1)
        self._results = self._context.Queue(maxsize=2)
        self._process = self._context.Process(
            target=_worker_main, args=(dict(config), self._commands, self._results),
            name="voiceprompt-inference", daemon=True,
        )
        try:
            with spawn_environment(config["device"]):
                self._process.start()
        except Exception:
            self._discard_queues()
            self._process = None
            raise WorkerError("worker_start_failed") from None
        self.config_signature = (
            config.get("repo"), config.get("revision"), str(Path(config["model_path"]).resolve()),
            tuple(sorted(config.get("checksum_manifest", {}).items())),
            config["device"], config["compute_type"],
        )

    def submit(self, job: DecodeJob) -> None:
        if self._process is None or not self._process.is_alive():
            raise WorkerError("worker_not_running")
        if not isinstance(job, DecodeJob) or job.owner_generation != self.owner_generation:
            raise WorkerError("stale_decode_job")
        try:
            self._commands.put_nowait(job)
        except queue.Full:
            raise WorkerError("worker_busy") from None
        except (OSError, ValueError):
            raise WorkerError("worker_broken") from None

    def poll(self) -> list[dict[str, Any]]:
        if self._results is None:
            return []
        messages: list[dict[str, Any]] = []
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            except (OSError, ValueError):
                raise WorkerError("worker_broken") from None
            if not isinstance(result, dict) or not isinstance(result.get("type"), str):
                raise WorkerError("worker_result_invalid")
            if result.get("owner_generation") != self.owner_generation:
                continue
            messages.append(result)
        return messages

    async def terminate_and_reap(self, timeout: float = 3.0) -> None:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("worker_timeout")
        process = self._process
        if process is None:
            self._discard_queues()
            return
        if process.is_alive() and self._commands is not None:
            try:
                self._commands.put_nowait({"command": "shutdown"})
            except (queue.Full, OSError, ValueError):
                pass
        await asyncio.to_thread(process.join, timeout)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, max(1.0, timeout))
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if kill is not None:
                kill()
            else:
                process.terminate()
            await asyncio.to_thread(process.join, 2.0)
        if process.is_alive():
            raise WorkerError("worker_reap_failed")
        try:
            process.close()
        except (ValueError, OSError):
            pass
        self._process = None
        self.config_signature = None
        self._discard_queues()

    def _discard_queues(self) -> None:
        for channel in (self._commands, self._results):
            if channel is None:
                continue
            try:
                channel.cancel_join_thread()
                channel.close()
            except (AttributeError, OSError, ValueError):
                pass
        self._commands = None
        self._results = None
