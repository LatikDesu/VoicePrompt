"""Stateful Silero V6 CPU scoring with original-timeline speech intervals."""

import importlib.metadata
import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class SpeechInterval:
    start_sample: int
    end_sample: int


class SileroScorer:
    def __init__(self) -> None:
        import onnxruntime as ort
        asset = importlib.metadata.distribution("faster-whisper").locate_file("faster_whisper/assets/silero_vad_v6.onnx")
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        self.session = ort.InferenceSession(str(asset), providers=["CPUExecutionProvider"], sess_options=options)
        if {value.name for value in self.session.get_inputs()} != {"input", "h", "c"}:
            raise ValueError("vad_model_contract")
        self.input = np.empty((1, 576), dtype=np.float32)
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(64, dtype=np.float32)

    def reset(self) -> None:
        self.h.fill(0)
        self.c.fill(0)
        self.context.fill(0)

    def score(self, frame: np.ndarray) -> float:
        if frame.shape != (512,) or frame.dtype != np.float32:
            raise ValueError("vad_frame_format")
        self.input[0, :64] = self.context
        self.input[0, 64:] = frame
        probability, self.h, self.c = self.session.run(None, {"input": self.input, "h": self.h, "c": self.c})
        self.context[:] = frame[-64:]
        result = float(probability.reshape(-1)[0])
        if not math.isfinite(result):
            raise ValueError("nonfinite_vad")
        return result


class VadGate:
    def __init__(self, scorer=None, threshold: float = 0.5) -> None:
        self.scorer = scorer if scorer is not None else SileroScorer()
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self.scorer.reset()
        self.pending = np.empty(0, dtype=np.float32)
        self.expected_start = 0
        self.frame_start = 0
        self.speech_detected = False
        self.speech_active = False
        self.last_speech_end = 0
        self.segment_start = None

    def _frame(self, frame: np.ndarray, actual_end: int) -> list[SpeechInterval]:
        probability = self.scorer.score(frame)
        if not math.isfinite(probability):
            raise ValueError("nonfinite_vad")
        if probability >= self.threshold:
            self.speech_detected = True
            if not self.speech_active:
                self.segment_start = max(0, self.frame_start - 4800)
                self.speech_active = True
            self.last_speech_end = actual_end
        elif self.speech_active and actual_end - self.last_speech_end >= 11200:
            interval = SpeechInterval(self.segment_start, min(actual_end, self.last_speech_end + 4800))
            self.speech_active = False
            self.segment_start = None
            return [interval]
        return []

    def push(self, samples: np.ndarray, absolute_start: int) -> list[SpeechInterval]:
        if absolute_start != self.expected_start or samples.ndim != 1 or samples.dtype != np.float32:
            raise ValueError("audio_offset_mismatch")
        if not np.isfinite(samples).all():
            raise ValueError("nonfinite_pcm")
        self.expected_start += samples.size
        joined = np.concatenate((self.pending, samples)) if self.pending.size else samples
        count = joined.size - joined.size % 512
        intervals = []
        for offset in range(0, count, 512):
            intervals.extend(self._frame(joined[offset:offset + 512], self.frame_start + 512))
            self.frame_start += 512
        self.pending = joined[count:].copy()
        return intervals

    def flush(self, end_sample: int) -> list[SpeechInterval]:
        if end_sample != self.expected_start:
            raise ValueError("audio_offset_mismatch")
        intervals = []
        if self.pending.size:
            frame = np.zeros(512, dtype=np.float32)
            frame[:self.pending.size] = self.pending
            intervals.extend(self._frame(frame, end_sample))
            self.pending = np.empty(0, dtype=np.float32)
            self.frame_start = end_sample
        if self.speech_active:
            intervals.append(SpeechInterval(self.segment_start, min(end_sample, self.last_speech_end + 4800)))
            self.speech_active = False
            self.segment_start = None
        return intervals
