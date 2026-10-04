"""Model downloads reuse the exact pin, refresh superseded revisions and reclaim only owned staging."""

import asyncio
from pathlib import Path
import hashlib
import json
import queue
import threading
import types

from voiceprompt import models, model_download
from voiceprompt.model_download import PROFILES, ModelDownload

PINNED_REPO, PINNED_REVISION = PROFILES["cpu-small"]


def _managed_model(path, revision=PINNED_REVISION, repo=PINNED_REPO):
    path.mkdir(parents=True)
    for name, content in {"model.bin": b"ct2", "config.json": b'{"model_type":"Whisper"}',
                          "tokenizer.json": b'{"model":{"vocab":{"<|ru|>":1}}}'}.items():
        (path / name).write_bytes(content)
    (path / ".voiceprompt-model.json").write_text(json.dumps({
        "schema": 1, "repo": repo, "revision": revision,
        "checksums": {name: hashlib.sha256((path / name).read_bytes()).hexdigest()
                      for name in ("model.bin", "config.json", "tokenizer.json")},
    }))
    return path


def test_cancel_removes_only_owned_staging(tmp_path):
    owned = tmp_path / ".model.voiceprompt-owned"
    owned.mkdir()
    (owned / "model.bin").write_bytes(b"partial")
    unrelated = tmp_path / ".model.voiceprompt-unrelated"
    unrelated.mkdir()
    (unrelated / "model.bin").write_bytes(b"other")

    class Process:
        def __init__(self):
            self.alive = True
        def is_alive(self):
            return self.alive
        def terminate(self):
            self.alive = False
        def join(self, timeout):
            return None
        def close(self):
            return None

    class Results:
        def cancel_join_thread(self):
            return None
        def close(self):
            return None

    async def scenario():
        download = ModelDownload(lambda event, **fields: None)
        download.process = Process()
        download.results = Results()
        download.staging = owned
        await download.close()
        assert not owned.exists()
        assert (unrelated / "model.bin").read_bytes() == b"other"

    asyncio.run(scenario())


def test_download_reuses_valid_existing_model(tmp_path):
    target = _managed_model(tmp_path / "cpu-small")
    events = []

    async def scenario():
        download = ModelDownload(lambda event, **fields: events.append((event, fields)))
        await download.start("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120", target)
        assert download.process is None

    asyncio.run(scenario())
    assert events[0][0] == "model_download_complete"
    assert models.validate_local(target).repo == "Systran/faster-whisper-small"


def test_repair_is_published_by_parent_after_child_finishes(tmp_path, monkeypatch):
    target = _managed_model(tmp_path / "cpu-small")
    (target / "tokenizer.json").write_text("broken")
    staging = tmp_path / ".owned-download"
    staging.mkdir()
    results = queue.Queue()
    def download_fixture(repo, revision, destination, **kwargs):
        return models.validate_local(_managed_model(Path(destination)))
    monkeypatch.setattr(models, "download_model", download_fixture)
    monkeypatch.setattr(model_download, "download_model", download_fixture, raising=False)
    model_download._download("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120",
                             str(target), str(staging), results)
    assert (target / "tokenizer.json").read_text() == "broken"
    class FinishedProcess:
        def is_alive(self): return False
        def join(self, timeout): pass
        def close(self): pass
    results.cancel_join_thread = lambda: None
    results.close = lambda: None
    downloader = ModelDownload(lambda *args, **kwargs: None)
    downloader.process, downloader.results = FinishedProcess(), results
    downloader.destination, downloader.staging = target, staging

    async def scenario():
        await downloader.tick()
        assert downloader.publication is not None, "the parent must own the artifact publication"
        await asyncio.gather(downloader.publication)
        await downloader.tick()

    asyncio.run(scenario())
    assert models.validate_local(target).repo == "Systran/faster-whisper-small"
    assert not staging.exists()


def test_superseded_revision_is_not_reported_as_a_complete_download(tmp_path, monkeypatch):
    target = _managed_model(tmp_path / "cpu-small", revision="b" * 40)
    events = []
    spawned = []

    class Process:
        def start(self):
            return None

    class Context:
        def Queue(self, maxsize):
            return queue.Queue(maxsize=maxsize)

        def Process(self, **kwargs):
            spawned.append(kwargs)
            return Process()

    monkeypatch.setattr(model_download, "multiprocessing",
                        types.SimpleNamespace(get_context=lambda method: Context()))
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))

    asyncio.run(download.start(PINNED_REPO, PINNED_REVISION, target))

    assert spawned, "a superseded revision must be refreshed instead of accepted as complete"
    assert [event for event, _ in events] == []


def test_exact_pinned_revision_is_reused_without_spawning_a_download(tmp_path, monkeypatch):
    target = _managed_model(tmp_path / "cpu-small")
    monkeypatch.setattr(model_download, "multiprocessing",
                        types.SimpleNamespace(get_context=lambda method: _forbidden_context()))
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))

    asyncio.run(download.start(PINNED_REPO, PINNED_REVISION, target))

    assert download.process is None
    assert events[0][0] == "model_download_complete"
    assert events[0][1]["revision"] == PINNED_REVISION


def _forbidden_context():
    raise AssertionError("no download may be spawned for an already pinned model")


class _FinishedProcess:
    def is_alive(self):
        return False

    def join(self, timeout):
        return None

    def close(self):
        return None


def _spawn_recording_context(spawned):
    class Process:
        def start(self):
            return None

    class Context:
        def Queue(self, maxsize):
            return queue.Queue(maxsize=maxsize)

        def Process(self, **kwargs):
            spawned.append(kwargs)
            return Process()

    return types.SimpleNamespace(get_context=lambda method: Context())


def _queued_results(*events):
    results = queue.Queue()
    for event in events:
        results.put(event)
    results.cancel_join_thread = lambda: None
    results.close = lambda: None
    return results


def _prepared_event(prepared, fingerprint=None, profile="cpu-small"):
    return {"event": "model_download_prepared", "path": str(prepared),
            "profile": profile, "target_fingerprint": fingerprint}


def test_start_hashes_in_a_thread_and_keeps_the_event_loop_responsive(tmp_path, monkeypatch):
    """The destination hash must never run on the event loop that serves lease renewals."""
    target = tmp_path / "cpu-small"
    checking, release = threading.Event(), threading.Event()
    validate = models.validate_profile

    def slow_validate(path, profile):
        checking.set()
        assert release.wait(5), "validation must be released by the test"
        return validate(path, profile)

    spawned = []
    monkeypatch.setattr(models, "validate_profile", slow_validate)
    monkeypatch.setattr(model_download, "multiprocessing", _spawn_recording_context(spawned))
    download = ModelDownload(lambda event, **fields: None)

    async def scenario():
        start = asyncio.create_task(download.start(PINNED_REPO, PINNED_REVISION, target))
        assert await asyncio.to_thread(checking.wait, 1)
        await asyncio.wait_for(download.tick(), 0.5)
        assert not start.done(), "start must not finish while the validation thread is mid-hash"
        release.set()
        await asyncio.wait_for(start, 3)

    asyncio.run(scenario())
    assert spawned, "a missing model must still spawn the owned downloader after validation"


def test_duplicate_prepared_events_publish_the_model_only_once(tmp_path, monkeypatch):
    target = tmp_path / "cpu-small"
    staging = tmp_path / ".download"
    prepared = _managed_model(staging / "completed")
    published = []
    publish = models.publish_model

    def counted_publish(path, destination, profile, fingerprint):
        published.append(path)
        return publish(path, destination, profile, fingerprint)

    monkeypatch.setattr(models, "publish_model", counted_publish)
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process = _FinishedProcess()
    download.results = _queued_results(_prepared_event(prepared), _prepared_event(prepared))
    download.destination, download.staging = target, staging

    async def scenario():
        await download.tick()
        assert download.publication is not None, "the prepared artifact must be published by the owner"
        await asyncio.gather(download.publication)
        await download.tick()

    asyncio.run(scenario())
    assert len(published) == 1, "a repeated prepared event must not publish or validate twice"
    assert [event for event, _ in events] == ["model_download_complete"]
    assert not staging.exists()
    assert (target / "model.bin").is_file()


def test_cancelled_publication_cannot_report_into_a_restarted_job(tmp_path, monkeypatch):
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
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process = _FinishedProcess()
    download.results = _queued_results(_prepared_event(prepared))
    download.destination, download.staging = target, staging

    async def scenario():
        await download.tick()
        assert await asyncio.to_thread(started.wait, 1)
        cancel = asyncio.create_task(download.close())
        await asyncio.sleep(0.05)
        assert not cancel.done(), "cancel must not report before the owned publication stops"
        assert download.staging is not None, "staging must never be removed under a running publication"
        release.set()
        await asyncio.wait_for(cancel, 3)

    asyncio.run(scenario())
    assert (target / "model.bin").is_file(), "the owned publication must run to completion"
    assert [event for event, _ in events] == ["model_download_error"]
    assert events[0][1]["error_code"] == "model_download_cancelled"
    assert not staging.exists()


def test_watchdog_tick_during_cancel_cannot_delete_staging_under_a_running_publication(tmp_path, monkeypatch):
    """A watchdog tick racing a cancel must not reclaim staging while the thread still reads it."""
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
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process = _FinishedProcess()
    download.results = _queued_results(_prepared_event(prepared))
    download.destination, download.staging = target, staging

    async def scenario():
        await download.tick()
        assert await asyncio.to_thread(started.wait, 1)
        cancel = asyncio.create_task(download.close())
        await asyncio.sleep(0)
        assert not cancel.done(), "the cancel must still be waiting for the owned publication"
        await download.tick()
        assert download.publication is not None, "publication ownership must stay visible to the watchdog"
        assert download.staging is not None, "a concurrent tick must not reclaim staging under the thread"
        release.set()
        await asyncio.wait_for(cancel, 3)

    asyncio.run(scenario())
    assert (target / "model.bin").is_file(), "the owned publication must run to completion"
    assert [event for event, _ in events] == ["model_download_error"]
    assert events[0][1]["error_code"] == "model_download_cancelled"
    assert not staging.exists()


class _BlockingProcess:
    """A child whose join suspends in a worker thread until the test releases it."""

    def __init__(self, gate):
        self.gate = gate
        self.terminated = False

    def is_alive(self):
        return not self.gate.is_set()

    def terminate(self):
        self.terminated = True

    def kill(self):
        return None

    def join(self, timeout):
        assert self.gate.wait(5), "the child join must be released by the test"

    def close(self):
        return None


def test_watchdog_tick_during_child_join_cannot_reclaim_staging(tmp_path):
    """While a cancel reaps the child, a watchdog tick must not delete the staging tree."""
    target = tmp_path / "cpu-small"
    staging = tmp_path / ".download"
    staging.mkdir()
    (staging / "partial.bin").write_bytes(b"downloading")
    gate = threading.Event()
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process, download.results = _BlockingProcess(gate), _queued_results()
    download.destination, download.staging = target, staging

    async def scenario():
        cancel = asyncio.create_task(download.close())
        await asyncio.sleep(0)
        assert not cancel.done()
        await download.tick()
        assert download.process is not None, "the child stays owned until it is physically reaped"
        assert download.staging is not None
        assert (staging / "partial.bin").is_file(), "staging must survive a tick during the cancel join"
        gate.set()
        await asyncio.wait_for(cancel, 3)

    asyncio.run(scenario())
    assert not staging.exists()
    assert [event for event, _ in events] == ["model_download_error"]
    assert events[0][1]["error_code"] == "model_download_cancelled"


def test_cancel_during_child_reap_waits_for_the_owned_child(tmp_path):
    """A cancel arriving during the watchdog's child reap must share it, not discard staging."""
    target = tmp_path / "cpu-small"
    staging = tmp_path / ".download"
    staging.mkdir()
    (staging / "partial.bin").write_bytes(b"downloading")
    gate = threading.Event()
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process, download.results = _BlockingProcess(gate), _queued_results(
        {"event": "model_download_error", "error_code": "model_download_failed"})
    download.destination, download.staging = target, staging

    async def scenario():
        reap = asyncio.create_task(download.tick())
        await asyncio.sleep(0)
        assert not reap.done(), "the watchdog must be suspended reaping the child"
        cancel = asyncio.create_task(download.close())
        await asyncio.sleep(0)
        assert not cancel.done(), "the cancel must wait for the child the watchdog is reaping"
        assert download.staging is not None
        assert (staging / "partial.bin").is_file(), "staging must survive a cancel during the reap"
        gate.set()
        await asyncio.wait_for(cancel, 3)
        await asyncio.wait_for(reap, 3)

    asyncio.run(scenario())
    assert not staging.exists(), "the shared reap must still reclaim staging once the child is gone"
    assert [event for event, _ in events] == ["model_download_error"]
    assert events[0][1]["error_code"] == "model_download_failed"


def test_tick_while_cancel_holds_the_lock_cannot_consume_the_retired_child_queue(tmp_path, monkeypatch):
    """A tick during a cancel must not adopt the old child's artifact with a bumped generation."""
    target = tmp_path / "cpu-small"
    staging = tmp_path / ".download"
    prepared = _managed_model(staging / "completed")
    published = []
    publish = models.publish_model

    def counted_publish(path, destination, profile, fingerprint):
        published.append(path)
        return publish(path, destination, profile, fingerprint)

    monkeypatch.setattr(models, "publish_model", counted_publish)
    gate = threading.Event()
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process, download.results = _BlockingProcess(gate), _queued_results(_prepared_event(prepared))
    download.destination, download.staging = target, staging

    async def scenario():
        cancel = asyncio.create_task(download.close())
        await asyncio.sleep(0)
        assert download.close_lock.locked(), "the cancel owns the job while it reaps the child"
        assert download.process is not None, "the child stays owned until it is physically reaped"
        await download.tick()
        assert download.publication is None, "a retired child's artifact must not be adopted by the watchdog"
        gate.set()
        await asyncio.wait_for(cancel, 3)

    asyncio.run(scenario())
    assert published == [], "the retired artifact must never be published"
    assert [event for event, _ in events] == ["model_download_error"]
    assert events[0][1]["error_code"] == "model_download_cancelled"
    assert not staging.exists()


def test_restarted_job_does_not_inherit_the_cancelled_publication(tmp_path, monkeypatch):
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
    spawned = []
    monkeypatch.setattr(model_download, "multiprocessing", _spawn_recording_context(spawned))
    events = []
    download = ModelDownload(lambda event, **fields: events.append((event, fields)))
    download.process = _FinishedProcess()
    download.results = _queued_results(_prepared_event(prepared))
    download.destination, download.staging = target, staging
    repo, revision = PROFILES["cuda-turbo"]

    async def scenario():
        await download.tick()
        assert await asyncio.to_thread(started.wait, 1)
        cancel = asyncio.create_task(download.close())
        release.set()
        await asyncio.wait_for(cancel, 3)
        assert download.process is None
        await asyncio.wait_for(download.start(repo, revision, tmp_path / "cuda-turbo"), 3)

    asyncio.run(scenario())
    assert spawned, "the restarted job must spawn its own downloader"
    assert [event for event, _ in events] == ["model_download_error"], (
        "the fenced publication must not report a completion into the restarted job")
    assert events[0][1]["error_code"] == "model_download_cancelled"
