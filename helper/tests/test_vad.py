"""VAD preserves original offsets/tail and never ends the recording session."""

import numpy as np

from voiceprompt.vad import SpeechInterval, VadGate


class SequenceScorer:
    def __init__(self, values):
        self.values = values
        self.index = 0

    def reset(self):
        self.index = 0

    def score(self, frame):
        result = self.values[self.index]
        self.index += 1
        return result


def test_preroll_tail_and_pause_keep_original_sample_offsets():
    scorer = SequenceScorer([0.0] * 20 + [0.9] * 2 + [0.0] * 24)
    gate = VadGate(scorer=scorer)
    intervals = gate.push(np.zeros(46 * 512, dtype=np.float32), 0)
    assert intervals == [SpeechInterval(5440, 16064)]
    assert gate.speech_detected is True
    assert gate.speech_active is False


def test_silence_has_no_speech_interval():
    gate = VadGate(scorer=SequenceScorer([0.0] * 10))
    assert gate.push(np.zeros(10 * 512, dtype=np.float32), 0) == []
    assert gate.flush(10 * 512) == []
    assert gate.speech_detected is False


def test_stop_flush_includes_actual_partial_frame_not_padding():
    gate = VadGate(scorer=SequenceScorer([0.9, 0.9]))
    assert gate.push(np.zeros(612, dtype=np.float32), 0) == []
    assert gate.flush(612) == [SpeechInterval(0, 612)]


def test_new_session_resets_recurrent_scorer_state():
    class DelayedScorer:
        def __init__(self):
            self.previous = 0.0

        def reset(self):
            self.previous = 0.0

        def score(self, frame):
            result = self.previous
            self.previous = 0.9 if frame[0] > 0.1 else 0.0
            return result

    gate = VadGate(scorer=DelayedScorer())
    gate.push(np.ones(1024, dtype=np.float32), 0)
    assert gate.speech_detected is True
    gate.reset()
    gate.push(np.zeros(512, dtype=np.float32), 0)
    assert gate.speech_detected is False
