"""One running job and one replaceable pending descriptor per recording."""

from dataclasses import dataclass

from .audio_buffer import AudioSlice


@dataclass(frozen=True, slots=True)
class DecodeJob:
    owner_generation: str
    session_id: str
    job_id: str
    kind: str
    audio: AudioSlice
    options: dict


class Scheduler:
    def __init__(self) -> None:
        self.running = None
        self.pending = None
        self.stopping = False
        self.identity = None
        self.cadence_s = 0.8

    def _admit(self, job: DecodeJob, kind: str) -> None:
        if job.kind != kind:
            raise ValueError("decode_job_kind")
        identity = (job.owner_generation, job.session_id)
        if self.identity is None:
            self.identity = identity
        if identity != self.identity:
            raise ValueError("stale_decode_job")

    def offer_live(self, job: DecodeJob) -> None:
        self._admit(job, "live")
        if not self.stopping:
            self.pending = job

    def request_final(self, job: DecodeJob) -> None:
        self._admit(job, "final")
        self.stopping = True
        self.pending = job

    def next_job(self) -> DecodeJob | None:
        if self.running is not None or self.pending is None:
            return None
        self.running, self.pending = self.pending, None
        return self.running

    def complete(self, job_id: str, duration_s: float) -> bool:
        if self.running is None or self.running.job_id != job_id:
            return False
        self.running = None
        self.cadence_s = max(0.8, duration_s)
        return True

    def cancel_session(self, session_id: str) -> None:
        if self.identity is not None and self.identity[1] == session_id:
            self.pending = None
            self.stopping = True
