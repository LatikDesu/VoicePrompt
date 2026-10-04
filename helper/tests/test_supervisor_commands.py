"""Malformed commands and idle repeat must not invalidate an active owner."""

import asyncio
import json
import os
import struct
from types import SimpleNamespace

from voiceprompt.supervisor import Supervisor
from test_supervisor import command, running_helper


def test_stop_and_cancel_are_idempotent_at_idle(tmp_path):
    runtime = tmp_path / "runtime"
    with running_helper(runtime):
        for index, operation in enumerate(("stop", "stop", "cancel", "cancel"), 1):
            result = command(runtime, operation, request_id=f"g1:r{index}", session="s1")
            assert result["accepted"] is True
        status = command(runtime, "status", request_id="g1:r5")
        assert status["status"]["state"] == "idle"


def test_renew_lease_is_serviced_while_stop_finalizes(tmp_path):
    """A slow stop/cancel must never starve a healthy owner's lease renewal."""
    from voiceprompt.protocol import Command

    class SlowEngine:
        capabilities = []

        def __init__(self):
            self.entered = asyncio.Event()
            self.finish = asyncio.Event()
            self.stops = 0

        async def stop(self, session_id):
            self.stops += 1
            self.entered.set()
            await self.finish.wait()

        async def cancel(self, session_id):
            return None

        async def unload(self):
            return None

        def status(self):
            return {"state": "finalizing", "model_loaded": False}

    async def scenario():
        server = Supervisor(tmp_path / "runtime", "g1")
        engine = SlowEngine()
        server.engine = engine
        first = Command(1, "g1:stop", "g1", "stop", "s1")
        stop_task = asyncio.create_task(server.handle(first))
        await asyncio.wait_for(engine.entered.wait(), 1)
        try:
            renewal = await asyncio.wait_for(
                server.handle(Command(1, "g1:renew", "g1", "renew_lease")), 0.5)
            assert renewal["accepted"] is True
            status = await asyncio.wait_for(
                server.handle(Command(1, "g1:status", "g1", "status")), 0.5)
            assert status["accepted"] is True
            assert status["status"]["state"] == "finalizing"
            assert not stop_task.done(), "the stop must still be in flight"
            # The pending mutator's identity is reserved: another command reusing
            # it conflicts even while the mutator has not replied yet.
            conflict = await asyncio.wait_for(
                server.handle(Command(1, "g1:stop", "g1", "renew_lease")), 0.5)
            assert conflict["accepted"] is False
            assert conflict["error_code"] == "request_conflict"
            # An identical retry coalesces onto the original reply exactly once.
            retry = asyncio.create_task(server.handle(first))
            await asyncio.sleep(0)
            assert engine.stops == 1
            assert not retry.done()
            engine.finish.set()
            original = await asyncio.wait_for(stop_task, 1)
            assert await asyncio.wait_for(retry, 1) == original
            assert engine.stops == 1
        finally:
            engine.finish.set()
            await asyncio.gather(stop_task, return_exceptions=True)

    asyncio.run(scenario())


def test_foreign_uid_cannot_read_owner_status(tmp_path):
    class Writer:
        def __init__(self):
            self.data = bytearray()

        def get_extra_info(self, name):
            return SimpleNamespace(getsockopt=lambda *args: struct.pack("3i", 123, os.getuid() + 1, 123))

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def attempt():
        server = Supervisor(tmp_path / "runtime", "g1")
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"protocol_version":1,"request_id":"r1","owner_generation":"g1","command":"status","payload":{}}\n')
        reader.feed_eof()
        writer = Writer()
        await server._client(reader, writer)
        response = json.loads(writer.data)
        assert response["accepted"] is False
        assert response["error_code"] == "peer_uid_mismatch"
        assert "status" not in response

    asyncio.run(attempt())


def test_unrelated_configuration_preserves_held_keyboard_shortcut(tmp_path):
    from voiceprompt.hotkeys import ChordGate, EvdevAdapter
    from voiceprompt.protocol import Command

    async def scenario():
        server = Supervisor(tmp_path / "runtime", "g1")
        server.config.update({"evdev_enabled": True, "evdev_device": "/dev/input/example",
                              "evdev_chord": "Ctrl+Space"})
        adapter = EvdevAdapter("/dev/input/example", "Ctrl+Space", server.emit)
        adapter.gate = ChordGate(57, frozenset({29}))
        adapter.gate.feed(29, 1)
        assert adapter.gate.feed(57, 1) == "pressed"
        server.hotkeys = adapter
        try:
            ack = await server.handle(Command(1, "r1", "g1", "configure",
                                               payload={"config_schema": 1, "language": "en"}))
            assert ack["accepted"] is True
            emitted = []
            while not server.events.empty():
                emitted.append(json.loads(server.events.get_nowait())["event"])
            assert emitted == ["configured"], "reconfiguring recognition must not synthesize key release"
            assert adapter.gate.active is True
            ack = await server.handle(Command(1, "r2", "g1", "configure",
                                               payload={"config_schema": 1, "evdev_enabled": False}))
            assert ack["accepted"] is True
            emitted = []
            while not server.events.empty():
                emitted.append(json.loads(server.events.get_nowait())["event"])
            assert emitted == ["hotkey_released", "configured"]
            assert server.hotkeys is None
        finally:
            if server.hotkeys is not None:
                await server.hotkeys.close()

    asyncio.run(scenario())


def test_timed_out_identical_retry_cannot_cancel_other_ack_waiters(tmp_path):
    import pytest
    from voiceprompt.protocol import Command

    class SlowEngine:
        def __init__(self):
            self.entered = asyncio.Event()
            self.finish = asyncio.Event()
            self.stops = 0

        async def stop(self, session_id):
            self.stops += 1
            self.entered.set()
            await self.finish.wait()

    async def scenario():
        server = Supervisor(tmp_path / "runtime", "g1")
        engine = SlowEngine()
        server.engine = engine
        request = Command(1, "g1:stop", "g1", "stop", "s1")
        original = asyncio.create_task(server.handle(request))
        await asyncio.wait_for(engine.entered.wait(), 1)
        waiting = asyncio.create_task(server.handle(request))
        later = None
        try:
            await asyncio.sleep(0)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(server.handle(request), 0.01)
            later = asyncio.create_task(server.handle(request))
            await asyncio.sleep(0)
            assert not original.done()
            assert not waiting.done(), "an abandoned retry must not cancel another waiter"
            assert not later.done(), "subsequent retries must still await the original ACK"
            engine.finish.set()
            ack = await asyncio.wait_for(original, 1)
            assert ack["accepted"] is True
            assert await asyncio.wait_for(waiting, 1) == ack
            assert await asyncio.wait_for(later, 1) == ack
            assert engine.stops == 1
        finally:
            engine.finish.set()
            await asyncio.gather(original, waiting, *([later] if later else []),
                                 return_exceptions=True)

    asyncio.run(scenario())
