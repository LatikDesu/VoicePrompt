import asyncio
import pytest

from voiceprompt.engine import Engine


def test_explicit_keep_warm_prevents_idle_unload():
    class LoadedWorker:
        is_alive = True
        pid = 123
        retired = False
        def poll(self):
            return []
        async def terminate_and_reap(self, timeout):
            self.retired = True
            self.is_alive = False

    async def scenario():
        engine = Engine(lambda event, **fields: None, 'owner')
        engine.configure({'config_schema': 1, 'preload': True, 'idle_unload_seconds': 1})
        worker = LoadedWorker()
        engine._worker = worker
        engine._worker_ready = True
        engine._idle_since = 1
        try:
            await engine.tick(10)
            assert engine.status()['model_loaded'] is True
            assert not worker.retired
        finally:
            await engine.close()
        assert worker.retired
    asyncio.run(scenario())


@pytest.mark.parametrize("idle_seconds, retained", [(0, True), (5, False)])
def test_idle_unload_zero_retains_model_and_positive_threshold_expires(idle_seconds, retained):
    class LoadedWorker:
        is_alive = True
        pid = 123
        retired = False

        def poll(self):
            return []

        async def terminate_and_reap(self, timeout):
            self.retired = True
            self.is_alive = False

    async def scenario():
        engine = Engine(lambda event, **fields: None, "owner")
        engine.configure({"config_schema": 1, "preload": False,
                          "idle_unload_seconds": idle_seconds})
        worker = LoadedWorker()
        engine._worker, engine._worker_ready, engine._idle_since = worker, True, 1
        try:
            await engine.tick(5.9)
            assert engine.status()["model_loaded"] is True
            await engine.tick(6)
            assert engine.status()["model_loaded"] is retained
            assert worker.retired is not retained
            if retained:
                await engine.tick(1000000)
                assert engine.status()["model_loaded"] is True
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_preload_missing_model_reports_once_without_capture():
    import time
    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), 'owner')
        engine.configure({'config_schema': 1, 'preload': True, 'model_path': ''})
        try:
            await engine.tick(time.monotonic())
            await engine.tick(time.monotonic())
            assert events == [('error', {'error_code': 'model_missing'})]
            assert engine.status()['capture_started'] is False
            assert engine.status()['audio_samples'] == 0
        finally:
            await engine.close()
    asyncio.run(scenario())


def test_preload_validation_runs_off_the_event_loop(monkeypatch, tmp_path):
    import threading
    from voiceprompt import models
    from test_engine import _install_fake_worker, _model_fixture

    path = _model_fixture(tmp_path)
    started, release = threading.Event(), threading.Event()
    digest = models._digest

    def slow_digest(target):
        started.set()
        if not release.wait(2):
            raise AssertionError("preload validation blocked the event loop")
        return digest(target)

    monkeypatch.setattr(models, "_digest", slow_digest)
    created = _install_fake_worker(monkeypatch)

    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), 'owner')
        engine.configure({'config_schema': 1, 'preload': True, 'model_profile': 'custom',
                          'model_path': str(path)})
        engine._runtime_ready = True
        try:
            # Admission must return while whole-model hashing is still running.
            await asyncio.wait_for(engine.tick(1000.0), 2)
            assert await asyncio.to_thread(started.wait, 1)
            assert engine._worker is None
            ran = []
            asyncio.get_running_loop().call_soon(ran.append, 'command')
            await asyncio.sleep(0)
            assert ran == ['command'], "the command loop must stay free during preload"
            assert engine._worker is None
            release.set()
            await asyncio.wait_for(engine._preload_task, 2)
            assert len(created) == 1
            assert 'model_loading' in [event for event, _ in events]
            assert engine._model_load_deadline is not None
        finally:
            release.set()
            await engine.close()

    asyncio.run(scenario())


def test_preload_validation_finishing_after_reconfiguration_is_discarded(monkeypatch, tmp_path):
    import threading
    from voiceprompt import models
    from test_engine import _install_fake_worker, _model_fixture

    path = _model_fixture(tmp_path)
    release = threading.Event()
    digest = models._digest

    def slow_digest(target):
        if not release.wait(2):
            raise AssertionError("preload validation blocked the event loop")
        return digest(target)

    monkeypatch.setattr(models, "_digest", slow_digest)
    created = _install_fake_worker(monkeypatch)

    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), 'owner')
        engine.configure({'config_schema': 1, 'preload': True, 'model_profile': 'custom',
                          'model_path': str(path)})
        engine._runtime_ready = True
        try:
            await engine.tick(1000.0)
            task = engine._preload_task
            assert task is not None
            engine.configure({'config_schema': 1, 'language': 'en'})
            release.set()
            await asyncio.wait_for(task, 2)
            assert created == [], "a stale validation must not revive a worker"
            assert [event for event, _ in events] == []
            assert engine._worker is None
            assert engine._preload_pending is True, "the new configuration must preload later"
        finally:
            release.set()
            await engine.close()

    asyncio.run(scenario())


def test_unload_during_preload_validation_prevents_late_worker_admission(monkeypatch, tmp_path):
    import threading
    from voiceprompt import models
    from test_engine import _install_fake_worker, _model_fixture

    path = _model_fixture(tmp_path)
    started, release = threading.Event(), threading.Event()
    digest = models._digest

    def slow_digest(target):
        started.set()
        if not release.wait(2):
            raise AssertionError("preload validation blocked the event loop")
        return digest(target)

    monkeypatch.setattr(models, "_digest", slow_digest)
    created = _install_fake_worker(monkeypatch)

    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), 'owner')
        engine.configure({'config_schema': 1, 'preload': True, 'model_profile': 'custom',
                          'model_path': str(path)})
        engine._runtime_ready = True
        try:
            await engine.tick(1000.0)
            task = engine._preload_task
            assert task is not None
            assert await asyncio.to_thread(started.wait, 1)
            await asyncio.wait_for(engine.unload(), 1)
            release.set()
            await asyncio.wait_for(task, 2)
            await engine.tick(1001.0)
            assert created == [], "an explicit unload must supersede validation already in flight"
            assert engine._worker is None
            assert engine.status()['state'] == 'idle'
            assert not engine._preload_pending
            assert events == []
        finally:
            release.set()
            await engine.close()

    asyncio.run(scenario())


def test_unload_clears_preload_intent_before_validation_starts(monkeypatch, tmp_path):
    from test_engine import _install_fake_worker, _model_fixture

    path = _model_fixture(tmp_path)
    created = _install_fake_worker(monkeypatch)

    async def scenario():
        engine = Engine(lambda event, **fields: None, 'owner')
        engine.configure({'config_schema': 1, 'preload': True, 'model_profile': 'custom',
                          'model_path': str(path)})
        engine._runtime_ready = True
        try:
            await engine.unload()
            await engine.tick(1000.0)
            assert engine._preload_task is None
            assert created == []
        finally:
            await engine.close()

    asyncio.run(scenario())
