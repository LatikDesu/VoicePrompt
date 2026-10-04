"""Capture starts on actual PCM and stop reaps the owned recorder."""

import asyncio
import sys

import numpy as np

from voiceprompt.capture import Capture


def test_stop_before_start_never_launches_recorder(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("release was already latched; recorder must not launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

    async def scenario():
        capture = Capture(lambda samples: None)
        capture.stop()
        await capture.start()
        await capture.wait_closed()
        assert capture.process is None

    asyncio.run(scenario())


def test_actual_pipe_samples_survive_stop_and_owned_child_exits(monkeypatch):
    real_launch = asyncio.create_subprocess_exec

    async def recorder(*args, **kwargs):
        return await real_launch(
            sys.executable, "-c",
            "import struct,sys,time;sys.stderr.buffer.write(b'noise'*262144);sys.stderr.buffer.flush();"
            "sys.stdout.buffer.write(struct.pack('<4f',0.25,-0.5,0.75,1));sys.stdout.buffer.flush();time.sleep(60)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    async def scenario():
        captured = []
        arrived = asyncio.Event()
        started = []

        def samples(value):
            captured.append(value.copy())
            arrived.set()

        capture = Capture(samples, on_started=lambda: started.append(True))
        await capture.start()
        await asyncio.wait_for(arrived.wait(), timeout=3)
        capture.stop()
        await capture.wait_closed()
        assert capture.process.returncode is not None
        np.testing.assert_array_equal(np.concatenate(captured), np.array([0.25, -0.5, 0.75, 1], dtype=np.float32))
        assert started == [True]

    asyncio.run(scenario())
