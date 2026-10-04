"""Append-only original audio; pending inference jobs carry descriptors, not copies."""

import uuid
from dataclasses import dataclass
from multiprocessing.shared_memory import SharedMemory

import numpy as np


@dataclass(frozen=True, slots=True)
class AudioSlice:
    segment_name: str
    start_sample: int
    end_sample: int

    def copy_samples(self) -> np.ndarray:
        shared = SharedMemory(name=self.segment_name, create=False, track=False)
        try:
            if not 0 <= self.start_sample <= self.end_sample <= shared.size // 4:
                raise ValueError("audio_bounds")
            return np.ndarray((self.end_sample - self.start_sample,), dtype="<f4",
                              buffer=shared.buf, offset=self.start_sample * 4).copy()
        finally:
            shared.close()


class SessionAudio:
    def __init__(self, capacity_samples: int = 1920000) -> None:
        if type(capacity_samples) is not int or capacity_samples <= 0 or capacity_samples > 1920000:
            raise ValueError("audio_capacity")
        self.capacity_samples = capacity_samples
        self.samples_written = 0
        self._shared = SharedMemory(name="voiceprompt-" + uuid.uuid4().hex,
                                    create=True, size=capacity_samples * 4)
        self._samples = np.ndarray((capacity_samples,), dtype="<f4", buffer=self._shared.buf)
        self.closed = False

    def append(self, samples: np.ndarray) -> int:
        if self.closed:
            raise ValueError("audio_closed")
        if samples.ndim != 1 or samples.dtype != np.dtype("<f4"):
            raise ValueError("pcm_format")
        if not np.isfinite(samples).all():
            raise ValueError("nonfinite_pcm")
        count = min(samples.size, self.capacity_samples - self.samples_written)
        start = self.samples_written
        self._samples[start:start + count] = samples[:count]
        self.samples_written += count
        return count

    def snapshot(self, start_sample: int, end_sample: int) -> AudioSlice:
        if self.closed or type(start_sample) is not int or type(end_sample) is not int:
            raise ValueError("audio_bounds")
        if not 0 <= start_sample <= end_sample <= self.samples_written:
            raise ValueError("audio_bounds")
        return AudioSlice(self._shared.name, start_sample, end_sample)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._samples = None
            self._shared.close()
            self._shared.unlink()
