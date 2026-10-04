import asyncio

import pytest

from voiceprompt.engine import Engine
from voiceprompt.capture import Capture
from voiceprompt.models import ModelError


def test_missing_model_rejects_start_before_opening_microphone(monkeypatch, tmp_path):
    async def forbidden(self, target=None):
        raise AssertionError("missing model must not open a microphone")

    monkeypatch.setattr(Capture, "start", forbidden)

    async def scenario():
        engine = Engine(lambda event, **fields: None, "g1")
        engine.configure({"config_schema": 1, "device": "cpu", "language": "ru",
                          "model_path": str(tmp_path / "missing")})
        try:
            with pytest.raises(ModelError, match="model_missing"):
                await engine.start("s1")
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_capture_continues_while_model_integrity_is_checked(monkeypatch, tmp_path):
    import threading
    import numpy as np
    from voiceprompt import models
    from voiceprompt.vad import VadGate

    model_path = tmp_path / "model"
    model_path.mkdir()
    (model_path / "model.bin").write_bytes(b"fixture-weights")
    (model_path / "config.json").write_text('{}', encoding="utf-8")
    (model_path / "tokenizer.json").write_text(
        '{"model":{"vocab":{"<|en|>":1,"<|ru|>":2}}}', encoding="utf-8"
    )
    checking, release = threading.Event(), threading.Event()
    digest = models._digest

    def slow_digest(path):
        checking.set()
        if not release.wait(2):
            raise AssertionError("model validation blocked capture/event loop")
        return digest(path)

    async def capture_start(self, target=None):
        self.on_started()
        self.on_samples(np.zeros(1600, dtype=np.float32))
        # No physical microphone or child process in this regression.
        self.process = object()

    def capture_stop(self):
        self.stop_requested = True
        self.closed.set()

    monkeypatch.setattr(models, "_digest", slow_digest)
    monkeypatch.setattr(Capture, "start", capture_start)
    monkeypatch.setattr(Capture, "stop", capture_stop)

    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), "g1")
        engine.configure({"model_profile": "custom", "model_path": str(model_path)})
        engine._runtime_ready, engine._vad_gate = True, VadGate()
        try:
            await engine.start("s1")
            assert await asyncio.to_thread(checking.wait, 1)
            assert engine.status()["capture_started"] is True
            assert engine.status()["audio_samples"] == 1600
            # Integrity work must not prevent subsequent audio from arriving.
            engine._capture.on_samples(np.zeros(1600, dtype=np.float32))
            assert engine.status()["audio_samples"] == 3200
            assert engine._worker is None
        finally:
            release.set()
            await engine.close()

    asyncio.run(scenario())


def test_invalid_model_stops_capture_without_starting_inference(monkeypatch, tmp_path):
    import time
    from voiceprompt.vad import VadGate

    async def capture_start(self, target=None):
        self.on_started()
        self.process = object()

    def capture_stop(self):
        self.stop_requested = True
        self.closed.set()

    monkeypatch.setattr(Capture, "start", capture_start)
    monkeypatch.setattr(Capture, "stop", capture_stop)

    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), "g1")
        engine.configure({"model_path": str(tmp_path)})
        engine._runtime_ready, engine._vad_gate = True, VadGate()
        try:
            await engine.start("s1")
            capture = engine._capture
            await engine._capture_start_task
            await engine.tick(time.monotonic())
            assert capture.closed.is_set()
            assert engine._worker is None
            assert engine.status()["audio_samples"] == 0
            assert ("error", {"session_id": "s1", "error_code": "model_missing"}) in events
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_hard_live_window_never_slices_away_uncommitted_audio():
    import numpy as np
    from types import SimpleNamespace
    from voiceprompt.audio_buffer import SessionAudio
    from voiceprompt.scheduler import Scheduler
    from voiceprompt.streaming import Agreement
    engine = Engine(lambda event, **fields: None, "g1")
    engine._session_id = "s1"
    engine._session_config = dict(engine._config)
    engine._audio = SessionAudio()
    engine._audio.append(np.zeros(480001, dtype=np.float32))
    engine._vad = SimpleNamespace(speech_active=True, segment_start=0)
    engine._scheduler = Scheduler()
    engine._agreement = Agreement()
    try:
        engine._maybe_offer_live(100)
        assert engine._scheduler.pending is None
        assert engine._agreement.degraded is True
        assert engine._audio.samples_written == 480001
    finally:
        engine._audio.close()


def test_silence_final_is_empty_without_submitting_asr():
    import numpy as np
    from voiceprompt.audio_buffer import SessionAudio
    from voiceprompt.scheduler import Scheduler
    from voiceprompt.vad import VadGate
    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), "g1")
        engine._session_id = "s1"
        engine._session_config = dict(engine._config)
        engine._audio = SessionAudio()
        engine._vad = VadGate()
        engine._scheduler = Scheduler()
        scheduler = engine._scheduler
        samples = np.zeros(16000, dtype=np.float32)
        engine._audio.append(samples)
        engine._vad.push(samples, 0)
        await engine.stop("s1")
        finals = [data for event, data in events if event == "final"]
        assert finals == [{"session_id": "s1", "text": "", "speech_detected": False}]
        assert scheduler.running is None and scheduler.pending is None
        assert engine.status()["audio_samples"] == 0
        await engine.close()
    asyncio.run(scenario())


class _Scorer:
    """Deterministic VAD stand-in: zero-leading frames are silence, others speech."""

    def reset(self):
        pass

    def score(self, frame):
        return 0.9 if frame[0] != 0 else 0.1


class _FakeWorker:
    """In-process stand-in for the spawned inference process."""

    def __init__(self):
        self.config = None
        self.config_signature = None
        self.jobs = []
        self.messages = []
        self.alive = True
        self.retired = False
        self._pid = None

    @property
    def is_alive(self):
        return self.alive

    @property
    def pid(self):
        return self._pid

    def start(self, config):
        self.config = dict(config)
        self._pid = 4321

    def submit(self, job):
        self.jobs.append(job)

    def poll(self):
        messages, self.messages = self.messages, []
        return messages

    async def terminate_and_reap(self, timeout=3.0):
        self.retired = True
        self.alive = False
        self._pid = None


def _install_fake_worker(monkeypatch):
    created = []

    def factory():
        worker = _FakeWorker()
        created.append(worker)
        return worker

    monkeypatch.setattr("voiceprompt.engine.Worker", factory)
    return created


def _model_fixture(tmp_path):
    path = tmp_path / "model"
    path.mkdir()
    (path / "model.bin").write_bytes(b"isolated-weights")
    (path / "config.json").write_text('{"model_type":"Whisper"}', encoding="utf-8")
    (path / "tokenizer.json").write_text('{"model":{"vocab":{"<|ru|>":1}}}', encoding="utf-8")
    return path


class _BlockingDigest:
    """Holds models._digest (whole-model hashing) until the test releases it."""

    def __init__(self):
        import threading
        from voiceprompt import models
        self.started = threading.Event()
        self.release = threading.Event()
        self._real = models._digest

    def __call__(self, path):
        self.started.set()
        if not self.release.wait(5):
            raise AssertionError("model validation blocked the event loop")
        return self._real(path)

    def patch(self, monkeypatch):
        monkeypatch.setattr("voiceprompt.models._digest", self)


def _stub_capture(monkeypatch, samples):
    async def capture_start(self, target=None):
        self.on_started()
        if samples.size:
            self.on_samples(samples)
        self.process = object()
        self.started = True

    def capture_stop(self):
        self.stop_requested = True
        self.closed.set()

    monkeypatch.setattr(Capture, "start", capture_start)
    monkeypatch.setattr(Capture, "stop", capture_stop)


def _configured_engine(events, tmp_path, **overrides):
    from voiceprompt.vad import VadGate

    engine = Engine(lambda event, **fields: events.append((event, fields)), "g1")
    config = {"model_profile": "custom", "model_path": str(_model_fixture(tmp_path))}
    config.update(overrides)
    engine.configure(config)
    engine._runtime_ready = True
    engine._vad_gate = VadGate(scorer=_Scorer())
    return engine


def test_stop_does_not_wait_for_model_validation(monkeypatch, tmp_path):
    import numpy as np

    digest = _BlockingDigest()
    digest.patch(monkeypatch)
    _stub_capture(monkeypatch, np.full(1600, 0.5, dtype=np.float32))
    _install_fake_worker(monkeypatch)

    async def scenario():
        events = []
        engine = _configured_engine(events, tmp_path)
        try:
            await engine.start("s1")
            assert await asyncio.to_thread(digest.started.wait, 2)
            # Hashing is still in flight; stop must acknowledge without awaiting it.
            await asyncio.wait_for(engine.stop("s1"), 1)
            assert engine.status()["session_id"] == "s1"
            assert not [data for event, data in events if event == "final"]
            assert not [data for event, data in events if event == "error"]
        finally:
            digest.release.set()
            await engine.close()

    asyncio.run(scenario())


def test_stop_with_pending_validation_settles_with_bounded_error(monkeypatch, tmp_path):
    import numpy as np

    digest = _BlockingDigest()
    digest.patch(monkeypatch)
    _stub_capture(monkeypatch, np.full(1600, 0.5, dtype=np.float32))

    async def scenario():
        events = []
        engine = _configured_engine(events, tmp_path, model_load_timeout_seconds=5)
        try:
            await engine.start("s1")
            assert await asyncio.to_thread(digest.started.wait, 2)
            await engine.stop("s1")
            assert not [data for event, data in events if event == "final"]
            deadline = engine._model_load_deadline
            assert deadline is not None, "validation must carry a model-load deadline"
            await engine.tick(deadline + 0.001)
            assert ("error", {"session_id": "s1", "error_code": "model_load_timeout"}) in events
            assert engine.status()["session_id"] is None
        finally:
            digest.release.set()
            await engine.close()

    asyncio.run(scenario())


def test_tail_only_speech_admits_worker_after_flush(monkeypatch, tmp_path):
    import numpy as np
    import time

    digest = _BlockingDigest()
    digest.patch(monkeypatch)
    samples = np.zeros(1600, dtype=np.float32)
    samples[1536:] = 0.5  # silence for every full frame, speech only in the flushed tail
    _stub_capture(monkeypatch, samples)
    created = _install_fake_worker(monkeypatch)

    async def scenario():
        events = []
        engine = _configured_engine(events, tmp_path)
        try:
            await engine.start("s1")
            assert await asyncio.to_thread(digest.started.wait, 2)
            await engine.stop("s1")
            assert [event for event, _ in events].count("finalizing") == 1
            assert created == [], "no worker may be admitted before the final VAD verdict"
            digest.release.set()
            task = engine._capture_start_task
            assert task is not None
            await asyncio.gather(task, return_exceptions=True)
            assert len(created) == 1, "flushed tail speech must still admit a worker"
            created[0].messages.append({"type": "model_ready"})
            await engine.tick(time.monotonic())
            kinds = [job.kind for job in created[0].jobs]
            assert kinds == ["final"], "the flushed tail must reach a final decode"
            assert engine.status()["state"] == "finalizing"
        finally:
            digest.release.set()
            await engine.close()

    asyncio.run(scenario())


def test_cancel_during_validation_never_admits_worker(monkeypatch, tmp_path):
    import numpy as np

    digest = _BlockingDigest()
    digest.patch(monkeypatch)
    _stub_capture(monkeypatch, np.full(1600, 0.5, dtype=np.float32))
    created = _install_fake_worker(monkeypatch)

    async def scenario():
        events = []
        engine = _configured_engine(events, tmp_path)
        try:
            await engine.start("s1")
            assert await asyncio.to_thread(digest.started.wait, 2)
            opening = engine._capture_start_task
            await engine.cancel("s1")
            digest.release.set()
            await asyncio.gather(opening, return_exceptions=True)
            await engine.tick(1e9)
            assert created == []
            assert "model_loading" not in [event for event, _ in events]
            assert engine.status()["session_id"] is None
            assert engine.status()["model_loaded"] is False
        finally:
            digest.release.set()
            await engine.close()

    asyncio.run(scenario())


def test_worker_exit_after_readiness_aborts_active_session():
    import time
    from voiceprompt.audio_buffer import SessionAudio
    from voiceprompt.scheduler import Scheduler
    from voiceprompt.streaming import Agreement
    from voiceprompt.vad import VadGate

    async def scenario():
        events = []
        engine = Engine(lambda event, **fields: events.append((event, fields)), "g1")
        engine._session_id = "s1"
        engine._session_config = dict(engine._config)
        engine._audio = SessionAudio()
        engine._vad = VadGate(scorer=_Scorer())
        engine._scheduler = Scheduler()
        engine._agreement = Agreement()
        engine._capture_started = True
        engine._state = "recording"
        worker = _FakeWorker()
        engine._worker = worker
        engine._worker_ready = True
        worker.alive = False
        try:
            await engine.tick(time.monotonic())
            assert ("error", {"session_id": "s1", "error_code": "worker_exited"}) in events
            assert engine.status()["session_id"] is None
            assert engine.status()["state"] == "idle"
        finally:
            await engine.close()
    asyncio.run(scenario())


def test_dead_ready_worker_without_session_is_retired():
    import time

    async def scenario():
        engine = Engine(lambda event, **fields: None, "g1")
        worker = _FakeWorker()
        worker.alive = False
        engine._worker = worker
        engine._worker_ready = True
        await engine.tick(time.monotonic())
        assert worker.retired is True
        assert engine._worker is None
        assert engine.status()["model_loaded"] is False
        await engine.close()
    asyncio.run(scenario())
