"""Explicit model download in a separately owned process; cancel never waits on HTTP."""

import asyncio
import multiprocessing
import queue
import os
import shutil
import tempfile
from pathlib import Path

from .models import ModelError
from . import models

PROFILES = {
    "cpu-small": ("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"),
    "cuda-turbo": ("dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
}


def _download(repo, revision, destination, staging, results):
    def progress(downloaded, total):
        try:
            results.put_nowait({"event": "model_download_progress", "downloaded_bytes": downloaded, "total_bytes": total})
        except queue.Full:
            pass
    try:
        profile = next((name for name, source in PROFILES.items() if source == (repo, revision)), None)
        if profile is None:
            raise ModelError("model_profile_download_unavailable")
        target = Path(destination)
        fingerprint = models.model_fingerprint(target)
        model = models.prepare_model(profile, target, progress, staging=Path(staging) / "completed", publish=False)
        results.put({"event": "model_download_prepared", "path": str(model.local_path), "profile": profile,
                     "target_fingerprint": fingerprint}, timeout=3)
    except ModelError as error:
        results.put({"event": "model_download_error", "error_code": error.code}, timeout=3)
    except OSError:
        results.put({"event": "model_download_error", "error_code": "model_download_failed"}, timeout=3)


class ModelDownload:
    """Owned downloader: the child streams bytes, only the event loop publishes the result.

    Validation and publication hash the whole artifact set, so both run in a worker thread
    while the event loop stays responsive. ``generation`` fences the asynchronous
    publication: a job that was cancelled or replaced never emits into its successor, and
    its staging tree is reclaimed only once no owned thread can still be reading it.
    """

    def __init__(self, emit) -> None:
        self.emit = emit
        self.process = None
        self.results = None
        self.reaping = None
        self.destination = None
        self.staging = None
        self.generation = 0
        self.admission = None
        self.publication = None
        self.prepared_seen = False
        self.settled = False
        self.close_lock = asyncio.Lock()

    async def start(self, repo, revision, destination):
        if (self.process is not None or self.reaping is not None or self.publication is not None
                or self.admission is not None or self.staging is not None or self.close_lock.locked()):
            raise ModelError("model_download_busy")
        profile = next((name for name, source in PROFILES.items() if source == (repo, revision)), None)
        if profile is None:
            raise ModelError("model_profile_download_unavailable")
        # Reserve the job before the first await: admission is atomic, so two commands can
        # never validate, stage or spawn into the same slot.
        self.admission = asyncio.current_task()
        try:
            self.generation += 1
            generation = self.generation
            self.destination = Path(destination)
            self.prepared_seen, self.settled = False, False
            try:
                existing = await asyncio.to_thread(models.validate_profile, self.destination, profile)
            except ModelError:
                models.model_fingerprint(self.destination)
            else:
                if self.generation != generation:
                    raise ModelError("model_download_cancelled")
                self.settled = True
                self.emit("model_download_complete", path=str(self.destination), repo=existing.repo, revision=existing.revision)
                return
            if self.generation != generation:
                # Cancelled while hashing: a retired job must stage and spawn nothing.
                raise ModelError("model_download_cancelled")
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            self.staging = Path(tempfile.mkdtemp(prefix=".voiceprompt-download-", dir=self.destination.parent))
            context = multiprocessing.get_context("spawn")
            self.results = context.Queue(maxsize=16)
            self.process = context.Process(target=_download, args=(repo, revision, str(destination), str(self.staging), self.results),
                                           name="voiceprompt-model-download", daemon=True)
            self.process.start()
        finally:
            self.admission = None

    async def tick(self):
        if self.close_lock.locked():
            # A cancel or shutdown owns this job's child, queue and staging: the watchdog
            # neither consumes the retired queue nor reclaims anything until it is done.
            return
        process, results, generation = self.process, self.results, self.generation
        if process is None and self.publication is None and self.staging is None:
            return
        complete = False
        if process is not None:
            while True:
                try:
                    event = results.get_nowait()
                except queue.Empty:
                    break
                kind = event.pop("event")
                stale = self.generation != generation
                if kind == "model_download_prepared":
                    if self.prepared_seen or stale:
                        continue
                    self.prepared_seen = True
                    self.publication = asyncio.create_task(
                        self._publish(event, self.destination, generation))
                    continue
                if kind in {"model_download_complete", "model_download_error"}:
                    complete = True
                    if not stale:
                        self.settled = True
                if stale:
                    # A retired job reports nothing into the live one.
                    continue
                self.emit(kind, **event)
            if complete or not process.is_alive():
                await self._release_process(kill=False)
        if self.publication is not None and self.publication.done():
            publication, self.publication = self.publication, None
            if not publication.cancelled():
                publication.result()
        if self.process is None and self.reaping is None and not self._publication_pending():
            # No owned child or thread can still be reading the staging tree.
            self._discard_staging()

    def _publication_pending(self):
        return self.publication is not None and not self.publication.done()

    async def _publish(self, event, destination, generation):
        """Finish one prepared artifact in a worker thread and report it at most once."""
        try:
            prepared = Path(event["path"])
            if prepared != destination:
                ref = await asyncio.to_thread(models.publish_model, prepared, destination,
                                              event["profile"], event["target_fingerprint"])
            else:
                ref = await asyncio.to_thread(models.validate_profile, destination, event["profile"])
            payload = {"path": str(destination), "repo": ref.repo, "revision": ref.revision}
            kind = "model_download_complete"
        except (ModelError, OSError) as error:
            payload = {"error_code": error.code if isinstance(error, ModelError) else "model_install_failed"}
            kind = "model_download_error"
        if self.generation != generation:
            return
        self.settled = True
        self.emit(kind, **payload)

    async def close(self):
        async with self.close_lock:
            if (self.process is None and self.results is None and self.reaping is None
                    and self.publication is None and self.staging is None and self.admission is None):
                return
            # Fence the job before waiting: an admission or publication that finishes after
            # this point belongs to a retired job and must not emit into a live one.
            self.generation += 1
            await self._release_process(kill=True)
            publication = self.publication
            if publication is not None:
                # Ownership stays visible for the whole wait, so a concurrent watchdog tick
                # cannot reclaim the staging tree this publication thread is still reading.
                await asyncio.gather(publication, return_exceptions=True)
                if self.publication is publication:
                    self.publication = None
            self._discard_staging()
            if not self.settled:
                self.settled = True
                self.emit("model_download_error", error_code="model_download_cancelled")

    async def _release_process(self, kill):
        if self.process is None and self.reaping is None:
            return
        reaping = self.reaping
        if reaping is None:
            reaping = self.reaping = asyncio.create_task(self._reap(kill))
        # Every caller shares one physical reap: the child, its queue and the staging tree
        # stay owned until it is gone, and no process is ever joined or closed twice.
        await asyncio.shield(reaping)

    async def _reap(self, kill):
        process, results = self.process, self.results
        try:
            if process.is_alive() and kill:
                process.terminate()
            await asyncio.to_thread(process.join, 1)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 2)
            process.close()
            results.cancel_join_thread()
            results.close()
        finally:
            self.process, self.results, self.reaping = None, None, None

    def _discard_staging(self):
        staging, self.staging = self.staging, None
        if staging is not None:
            shutil.rmtree(staging)
