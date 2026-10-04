"""Word agreement preserves repeats, offsets and unsafe tail audio."""

from voiceprompt.streaming import Agreement, Hypothesis, Word
from voiceprompt.scheduler import DecodeJob, Scheduler
from voiceprompt.audio_buffer import AudioSlice


def job(identifier, kind, end):
    return DecodeJob("g1", "s1", identifier, kind, AudioSlice("scheduler-test", 0, end), {})


def test_intentional_repetition_is_not_globally_deduplicated():
    words = (Word("очень", 0, 8000), Word("очень", 9000, 16000), Word("важно", 17000, 23000))
    agreement = Agreement()
    agreement.observe(Hypothesis(words, 32000, 0))
    snapshot = agreement.observe(Hypothesis(words, 40000, 0))
    assert snapshot.committed_text == "очень очень важно"


def test_last_word_inside_end_guard_remains_tentative():
    words = (Word("не", 0, 8000), Word("удаляй", 9000, 15000))
    agreement = Agreement()
    agreement.observe(Hypothesis(words, 16000, 0))
    result = agreement.observe(Hypothesis(words, 16000, 0))
    assert result.committed_text == "не"
    assert result.tentative_text == "удаляй"


def test_overlap_deduplicates_only_timestamped_committed_boundary():
    agreement = Agreement()
    first = (Word("очень", 0, 8000), Word("важно", 9000, 16000))
    agreement.observe(Hypothesis(first, 32000, 0))
    agreement.observe(Hypothesis(first, 40000, 0))
    next_words = (Word("важно", 9000, 16000), Word("важно", 24000, 32000))
    agreement.observe(Hypothesis(next_words, 48000, 8000))
    result = agreement.observe(Hypothesis(next_words, 56000, 8000))
    assert result.committed_text == "очень важно важно"


def test_comparison_preserves_yo_and_negative_numbers():
    assert Agreement.normalized("ёлка,") != Agreement.normalized("елка")
    assert Agreement.normalized("-42") != Agreement.normalized("42")


def test_final_replaces_pending_live_with_full_original_audio_descriptor():
    scheduler = Scheduler()
    scheduler.offer_live(job("j1", "live", 16000))
    assert scheduler.next_job().job_id == "j1"
    scheduler.offer_live(job("j2", "live", 32000))
    scheduler.offer_live(job("j3", "live", 48000))
    scheduler.request_final(job("j4", "final", 1920000))
    scheduler.complete("j1", duration_s=2)
    final = scheduler.next_job()
    assert (final.kind, final.job_id, final.audio.end_sample) == ("final", "j4", 1920000)
    scheduler.offer_live(job("j5", "live", 64000))
    scheduler.complete("j4", duration_s=3)
    assert scheduler.next_job() is None


def test_cancel_drops_pending_session_work():
    scheduler = Scheduler()
    scheduler.offer_live(job("j1", "live", 16000))
    scheduler.cancel_session("s1")
    assert scheduler.next_job() is None
