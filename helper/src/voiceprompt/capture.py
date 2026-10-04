"""Owned PipeWire recorder. Inference never blocks the PCM reader."""

import asyncio
import json
from collections.abc import Callable

import numpy as np

from .pcm import PCMDecoder


async def list_microphones() -> list[dict]:
    process = await asyncio.create_subprocess_exec(
        "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("pipewire_unavailable") from None
    if process.returncode or len(stdout) > 4194304:
        raise ValueError("pipewire_unavailable")
    try:
        nodes = json.loads(stdout)
    except ValueError:
        raise ValueError("pipewire_invalid_response") from None
    sources = []
    for node in nodes:
        props = (node.get("info") or {}).get("props") or {}
        name = props.get("node.name")
        if props.get("media.class") == "Audio/Source" and isinstance(name, str) and not name.endswith(".monitor"):
            sources.append({"name": name, "description": props.get("node.description", name)})
    return sources


class Capture:
    def __init__(self, on_samples: Callable[[np.ndarray], None], on_started=None, on_error=None) -> None:
        self.on_samples = on_samples
        self.on_started = on_started
        self.on_error = on_error
        self.process = None
        self.stop_requested = False
        self.closed = asyncio.Event()
        self.reader_task = None
        self.timer_task = None
        self.started = False
        self.error_code = None

    async def start(self, target: str | None = None) -> None:
        if self.stop_requested:
            self.closed.set()
            return
        if self.process is not None:
            return
        if target:
            sources = await list_microphones()
            if not any(source["name"] == target for source in sources):
                raise ValueError("microphone_missing")
        if self.stop_requested:
            self.closed.set()
            return
        argv = ["pw-record", "--raw", "--rate=16000", "--channels=1", "--format=f32", "--sample-count=1920000", "-"]
        if target:
            argv.extend(["--target", target])
        self.process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        if self.stop_requested:
            self.process.terminate()
        self.reader_task = asyncio.create_task(self._read())
        self.timer_task = asyncio.create_task(self._duration_limit())

    async def _duration_limit(self) -> None:
        await asyncio.sleep(120)
        self.stop()

    async def _stderr(self) -> None:
        while await self.process.stderr.read(2048):
            pass

    async def _read(self) -> None:
        decoder = PCMDecoder()
        stderr_task = asyncio.create_task(self._stderr())
        try:
            while chunk := await self.process.stdout.read(4096):
                samples = decoder.feed(chunk)
                if samples.size:
                    if not self.started and not self.stop_requested:
                        self.started = True
                        if self.on_started:
                            self.on_started()
                    self.on_samples(samples)
            decoder.finish()
            returncode = await self.process.wait()
            if returncode != 0 and not self.stop_requested:
                self.error_code = "capture_failed"
        except (ValueError, OSError) as error:
            self.error_code = str(error) if isinstance(error, ValueError) else "capture_failed"
            self.stop()
        finally:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            await stderr_task
            if self.timer_task:
                self.timer_task.cancel()
            if self.error_code and self.on_error:
                self.on_error(self.error_code)
            self.closed.set()

    def stop(self) -> None:
        self.stop_requested = True
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
        elif self.process is None:
            self.closed.set()

    async def wait_closed(self, timeout: float = 3) -> None:
        try:
            await asyncio.wait_for(self.closed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if self.process is not None and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            if self.reader_task:
                await self.reader_task
            raise ValueError("capture_stop_timeout") from None
