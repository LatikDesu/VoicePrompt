"""Live agreement must preserve history without freezing unconfirmed ASR guesses."""

import numpy as np

from voiceprompt.audio_buffer import SessionAudio
from voiceprompt.engine import Engine
from voiceprompt.scheduler import Scheduler
from voiceprompt.streaming import Agreement, Hypothesis, Word
from voiceprompt.vad import VadGate


def test_shorter_hypothesis_keeps_committed_but_revises_tentative():
    agreement = Agreement()
    stable = Word('Не', 0, 4000)
    agreement.observe(Hypothesis((stable,), 16000, 0))
    agreement.observe(Hypothesis((stable,), 24000, 0))
    agreement.observe(Hypothesis((stable, Word('удаляй', 8000, 12000), Word('сорок', 13000, 18000)), 32000, 0))
    changed = agreement.observe(Hypothesis((stable, Word('меняй', 8000, 12000)), 40000, 0))
    assert changed.committed_text == 'Не'
    assert changed.tentative_text == 'меняй'


def test_same_word_at_different_time_is_not_false_agreement():
    agreement = Agreement()
    agreement.observe(Hypothesis((Word('нет', 0, 4000),), 16000, 0))
    snapshot = agreement.observe(Hypothesis((Word('нет', 32000, 36000),), 48000, 0))
    assert snapshot.committed_text == ''
    assert snapshot.tentative_text == 'нет'


def test_trim_does_not_cross_overlapping_unconfirmed_word():
    agreement = Agreement()
    confirmed = Word('проверь', 180000, 230000)
    overlap = Word('конфигурацию', 200000, 239000)
    agreement.observe(Hypothesis((confirmed, overlap), 240000, 0))
    result = agreement.observe(Hypothesis((confirmed, overlap), 240000, 0))
    assert result.committed_text == 'проверь'
    assert result.tentative_text == 'конфигурацию'
    assert result.safe_trim_sample <= overlap.start_sample


def test_utterance_end_schedules_one_tail_decode_not_repeated_silence(monkeypatch):
    class Scores:
        def reset(self):
            self.index = 0
        def score(self, frame):
            self.index += 1
            return 0.9 if self.index <= 10 else 0.0

    monkeypatch.setattr('voiceprompt.engine.time.monotonic', lambda: 100.0)
    engine = Engine(lambda event, **fields: None, 'g1')
    engine._session_id = 's1'
    engine._session_config = dict(engine._config)
    engine._audio = SessionAudio()
    engine._vad = VadGate(scorer=Scores())
    engine._scheduler = Scheduler()
    engine._agreement = Agreement()
    try:
        # Short speech ends before first regular ~1s live context. Completion must flush it.
        engine._on_samples(engine._session_token, np.zeros(32 * 512, dtype=np.float32))
        pending = engine._scheduler.pending
        assert pending is not None
        assert pending.audio.end_sample == 32 * 512
        assert pending.kind == 'live'
        assert engine._session_id == 's1'
        engine._on_samples(engine._session_token, np.zeros(16000, dtype=np.float32))
        assert engine._scheduler.pending is pending
        assert engine._audio.samples_written == 32 * 512 + 16000
    finally:
        engine._audio.close()


def test_two_minute_transcript_survives_repeated_safe_window_trims():
    agreement = Agreement()
    window_start = 0
    previous_text = ''
    for second in range(1, 121):
        end = second * 16000
        words = tuple(Word(f'слово{i}', (i - 1) * 16000 + 100, i * 16000 - 8000)
                      for i in range(1, second + 1) if (i - 1) * 16000 + 100 >= window_start)
        snapshot = agreement.observe(Hypothesis(words, end, window_start))
        assert snapshot.committed_text.startswith(previous_text)
        assert end - window_start <= 480000
        assert not snapshot.degraded
        if snapshot.safe_trim_sample is not None:
            window_start = snapshot.safe_trim_sample
        previous_text = snapshot.committed_text
    assert snapshot.committed_text == ' '.join(f'слово{i}' for i in range(1, 120))
    assert snapshot.tentative_text == 'слово120'
