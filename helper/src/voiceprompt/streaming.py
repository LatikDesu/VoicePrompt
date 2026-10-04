"""LocalAgreement-2 on original sample offsets; never globally delete repeats."""

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_sample: int
    end_sample: int


@dataclass(frozen=True, slots=True)
class Hypothesis:
    words: tuple[Word, ...]
    audio_end_sample: int
    window_start_sample: int


@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    committed_text: str
    tentative_text: str
    audio_end_ms: int
    safe_trim_sample: int | None
    degraded: bool


def render_words(words) -> str:
    result = ""
    for word in words:
        text = word.text
        if result and text and not text[0].isspace() and text[0] not in ",.!?;:)]»":
            result += " "
        result += text
    return result.strip()


class Agreement:
    def __init__(self, guard_samples: int = 4800) -> None:
        self.guard_samples = guard_samples
        self.committed = []
        self.previous_uncommitted = ()
        self.degraded = False

    @property
    def committed_text(self) -> str:
        return render_words(self.committed)

    @property
    def tentative_text(self) -> str:
        return render_words(self.previous_uncommitted)

    @staticmethod
    def normalized(word: str) -> str:
        return unicodedata.normalize("NFC", word).casefold().strip().strip(".,!?;:«»\"()").strip()

    def _align(self, words: tuple[Word, ...]) -> tuple[Word, ...]:
        if not self.committed:
            return words
        committed_end = self.committed[-1].end_sample
        boundary_tolerance = 1600
        current = tuple(word for word in words if word.end_sample > committed_end - boundary_tolerance)
        for length in range(min(len(self.committed), len(current), 16), 0, -1):
            previous = self.committed[-length:]
            candidate = current[:length]
            if all(
                self.normalized(old.text) == self.normalized(new.text)
                and new.start_sample < old.end_sample + boundary_tolerance
                and new.end_sample > old.start_sample - boundary_tolerance
                for old, new in zip(previous, candidate)
            ):
                return current[length:]
        return current

    def observe(self, hypothesis: Hypothesis) -> TranscriptSnapshot:
        if not 0 <= hypothesis.window_start_sample <= hypothesis.audio_end_sample <= 1920000:
            raise ValueError("hypothesis_bounds")
        last_start = hypothesis.window_start_sample
        for word in hypothesis.words:
            if not isinstance(word.text, str) or not 0 <= word.start_sample <= word.end_sample <= hypothesis.audio_end_sample:
                raise ValueError("word_bounds")
            if word.start_sample < last_start:
                raise ValueError("word_order")
            last_start = word.start_sample
        current = self._align(hypothesis.words)
        safe_end = hypothesis.audio_end_sample - self.guard_samples
        agreed = 0
        for previous, word in zip(self.previous_uncommitted, current):
            if (self.normalized(previous.text) != self.normalized(word.text)
                    or word.end_sample > safe_end
                    or word.start_sample > previous.end_sample + 1600
                    or previous.start_sample > word.end_sample + 1600):
                break
            self.committed.append(word)
            agreed += 1
        remaining = current[agreed:]
        self.previous_uncommitted = remaining
        committed_text = render_words(self.committed)
        tentative_text = render_words(remaining)
        if len((committed_text + tentative_text).encode("utf-8")) > 65536:
            raise ValueError("transcript_too_large")
        trim = None
        window_length = hypothesis.audio_end_sample - hypothesis.window_start_sample
        if self.committed and window_length >= 240000:
            boundary = self.committed[-1].end_sample - 16000
            if remaining:
                boundary = min(boundary, remaining[0].start_sample)
            if boundary > hypothesis.window_start_sample:
                trim = boundary
        if window_length >= 480000 and trim is None:
            self.degraded = True
        return TranscriptSnapshot(committed_text, tentative_text, hypothesis.audio_end_sample // 16, trim, self.degraded)
