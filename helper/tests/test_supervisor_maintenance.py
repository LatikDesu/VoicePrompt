"""Owner-watchdog maintenance failures and a running publication must not strand the helper."""

import asyncio
import json
import threading

import pytest

from voiceprompt import models
from voiceprompt.models import ModelError
from voiceprompt.private_files import DataError
from voiceprompt.supervisor import Supervisor
from test_model_download import _FinishedProcess, _managed_model, _prepared_event, _queued_results


class _QuietFiles:
    def expire(self):
        return None


class _ExplodingExpire:
    def expire(self):
        raise DataError("staging_cleanup_failed")


class _ExplodingTick:
    def __init__(self):
        self.closed = 0

    async def tick(self):
        raise ModelError("model_install_failed")

    async def close(self, cancel=True):
        self.closed += 1


def _drain(server):
    emitted = []
    while not server.events.empty():
        emitted.append(json.loads(server.events.get_nowait()))
    return emitted


@pytest.mark.parametrize("broken", ["private_files", "download"])
def test_maintenance_failure_stops_the_supervisor(tmp_path, broken):
    """An exception in owner maintenance must stop the helper, not kill the watchdog silently."""

    async def scenario():
        server = Supervisor(tmp_path / "runtime", "g1")
        if broken == "private_files":
            server.private_files = _ExplodingExpire()
        else:
            server.private_files = _QuietFiles()
            server.download = _ExplodingTick()
        watchdog = asyncio.create_task(server._watch_owner())
        done, _ = await asyncio.wait({watchdog}, timeout=3)
        assert done, "the watchdog must return after a maintenance failure while it still owns the loop"
        assert server.stop_requested.is_set(), "a maintenance failure must stop the supervisor explicitly"
        assert not watchdog.cancelled()
        watchdog.result()
        emitted = _drain(server)
        assert [event["event"] for event in emitted] == ["error"]
        assert emitted[0]["error_code"] == "helper_maintenance_failed"

    asyncio.run(scenario())


def test_watchdog_keeps_ticking_while_the_owner_publishes(tmp_path, monkeypatch):
    """Publishing a prepared artifact must not stop lease, heartbeat or engine maintenance."""
    target = tmp_path / "cpu-small"
    staging = tmp_path / ".download"
    prepared = _managed_model(staging / "completed")
    started, release = threading.Event(), threading.Event()
    publish = models.publish_model

    def blocked_publish(path, destination, profile, fingerprint):
        started.set()
        assert release.wait(5), "the publication thread must be released by the test"
        return publish(path, destination, profile, fingerprint)

    monkeypatch.setattr(models, "publish_model", blocked_publish)

    class Ticked:
        def __init__(self):
            self.count = 0

        async def tick(self, now_monotonic):
            self.count += 1

    async def scenario():
        server = Supervisor(tmp_path / "runtime", "g1")
        server.private_files = _QuietFiles()
        ticks = Ticked()
        server.engine = ticks
        download = server.download
        download.process, download.results = _FinishedProcess(), _queued_results(_prepared_event(prepared))
        download.destination, download.staging = target, staging
        watchdog = asyncio.create_task(server._watch_owner())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while ticks.count < 2 and loop.time() < deadline:
                await asyncio.sleep(0.05)
            assert ticks.count >= 2, "maintenance must continue while a publication is in flight"
            assert download.publication is not None and not download.publication.done()
        finally:
            release.set()
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            await download.close()

    asyncio.run(scenario())
