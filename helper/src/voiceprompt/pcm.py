"""PipeWire raw mono float32 parsing across arbitrary pipe read boundaries."""

import numpy as np


class PCMDecoder:
    def __init__(self) -> None:
        self.pending = b""

    def feed(self, chunk: bytes) -> np.ndarray:
        raw = self.pending + chunk
        usable = len(raw) - len(raw) % 4
        self.pending = raw[usable:]
        samples = np.frombuffer(raw[:usable], dtype="<f4")
        if not np.isfinite(samples).all():
            raise ValueError("nonfinite_pcm")
        return samples

    def finish(self) -> None:
        if self.pending:
            raise ValueError("incomplete_pcm_sample")
