"""Pipe fragmentation and record limits must not corrupt accepted audio."""

import numpy as np
import pytest

from voiceprompt.audio_buffer import SessionAudio
from voiceprompt.pcm import PCMDecoder


def test_pipe_reads_split_float_samples_without_loss():
    values = np.array([0.125, -0.25, 0.5], dtype="<f4")
    raw = values.tobytes()
    parser = PCMDecoder()
    parts = [parser.feed(raw[:3]), parser.feed(raw[3:9]), parser.feed(raw[9:])]
    parser.finish()
    np.testing.assert_array_equal(np.concatenate(parts), values)


def test_incomplete_float_is_not_interpreted_as_a_sample():
    parser = PCMDecoder()
    assert parser.feed(b"\x00\x00\x00").size == 0
    with pytest.raises(ValueError, match="incomplete_pcm_sample"):
        parser.finish()


def test_recording_limit_never_overwrites_accepted_prefix():
    audio = SessionAudio(capacity_samples=8)
    try:
        assert audio.append(np.arange(6, dtype=np.float32)) == 6
        assert audio.append(np.arange(6, 12, dtype=np.float32)) == 2
        assert audio.samples_written == 8
        np.testing.assert_array_equal(audio.snapshot(0, 8).copy_samples(), np.arange(8, dtype=np.float32))
        assert audio.append(np.array([99], dtype=np.float32)) == 0
        np.testing.assert_array_equal(audio.snapshot(0, 8).copy_samples(), np.arange(8, dtype=np.float32))
    finally:
        audio.close()


def test_snapshot_prefix_is_immutable_while_capture_appends():
    audio = SessionAudio(capacity_samples=8)
    try:
        audio.append(np.array([1, 2, 3], dtype=np.float32))
        first = audio.snapshot(0, 3)
        audio.append(np.array([4, 5], dtype=np.float32))
        np.testing.assert_array_equal(first.copy_samples(), np.array([1, 2, 3], dtype=np.float32))
        with pytest.raises(ValueError, match="audio_bounds"):
            audio.snapshot(0, 6)
    finally:
        audio.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_audio_never_reaches_vad_or_asr(value):
    audio = SessionAudio(capacity_samples=8)
    try:
        with pytest.raises(ValueError, match="nonfinite_pcm"):
            audio.append(np.array([value], dtype=np.float32))
        assert audio.samples_written == 0
    finally:
        audio.close()
