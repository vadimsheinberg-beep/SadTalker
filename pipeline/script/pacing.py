"""Speech-rate arithmetic.

The deployed corpus sits at roughly 104 words per minute across 117 of 119
scripts. For a fast-cut format that reads as dead air between jokes; the band
the narration should land in is 130-155. This module is the arithmetic that
turns "too slow" into "cut 43 seconds or add 71 words", which is actionable,
and it is what the generator loops against.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SETTINGS, PacingPolicy
from ..contracts import Beat, Language, Script

# Beluga-style cuts are short. A beat past this length stops being a cut and
# becomes a monologue, which no amount of correct average wpm will fix.
MAX_BEAT_WORDS = 28
MIN_BEAT_WORDS = 3


def count_words(text: str) -> int:
    return len(text.split())


def wpm(words: int, duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    return words * 60.0 / duration_s


def duration_for(words: int, rate: float) -> float:
    """Seconds needed to speak ``words`` at ``rate`` words per minute."""
    if rate <= 0:
        raise ValueError("rate must be positive")
    return words * 60.0 / rate


def words_for(duration_s: float, rate: float) -> int:
    return int(round(duration_s * rate / 60.0))


@dataclass(frozen=True)
class PacingReport:
    words: int
    duration_s: float
    wpm: float
    min_wpm: float
    max_wpm: float
    long_beats: tuple[int, ...] = ()
    short_beats: tuple[int, ...] = ()

    @property
    def in_band(self) -> bool:
        return self.min_wpm <= self.wpm <= self.max_wpm

    @property
    def too_slow(self) -> bool:
        return self.wpm < self.min_wpm

    @property
    def word_delta(self) -> int:
        """Words to add (positive) or cut (negative) to reach the band edge.

        Aims at the nearest edge rather than the midpoint: the smallest edit
        that fixes the problem is the one least likely to damage the writing.
        """
        if self.in_band:
            return 0
        target = self.min_wpm if self.too_slow else self.max_wpm
        return words_for(self.duration_s, target) - self.words

    @property
    def suggested_duration_s(self) -> float:
        """Runtime that would put the current word count inside the band."""
        if self.in_band:
            return self.duration_s
        target = self.min_wpm if self.too_slow else self.max_wpm
        return round(duration_for(self.words, target), 1)

    def describe(self) -> str:
        if self.in_band and not self.long_beats and not self.short_beats:
            return f"{self.wpm:.0f} wpm over {self.duration_s:.0f}s — in band"
        parts = []
        if not self.in_band:
            verb = "add" if self.word_delta > 0 else "cut"
            parts.append(
                f"{self.wpm:.0f} wpm ({'slow' if self.too_slow else 'fast'}) — "
                f"{verb} {abs(self.word_delta)} words, or retime to "
                f"{self.suggested_duration_s:.0f}s"
            )
        if self.long_beats:
            parts.append(
                f"beats {list(self.long_beats)} exceed {MAX_BEAT_WORDS} words"
            )
        if self.short_beats:
            parts.append(
                f"beats {list(self.short_beats)} are under {MIN_BEAT_WORDS} words"
            )
        return "; ".join(parts)


def measure(
    script: Script, duration_s: float, policy: PacingPolicy | None = None
) -> PacingReport:
    policy = policy or SETTINGS.pacing
    return PacingReport(
        words=script.words,
        duration_s=duration_s,
        wpm=wpm(script.words, duration_s),
        min_wpm=policy.min_wpm,
        max_wpm=policy.max_wpm,
        long_beats=tuple(
            index
            for index, beat in enumerate(script.beats)
            if beat.words > MAX_BEAT_WORDS
        ),
        short_beats=tuple(
            index
            for index, beat in enumerate(script.beats)
            if beat.words < MIN_BEAT_WORDS
        ),
    )


def target_words(duration_s: float, policy: PacingPolicy | None = None) -> int:
    """Word budget to hand the generator for a given runtime."""
    policy = policy or SETTINGS.pacing
    return words_for(duration_s, policy.target_wpm)


def estimate_beat_duration(
    beat: Beat, language: Language, policy: PacingPolicy | None = None
) -> float:
    """Per-beat runtime estimate, used to time slides before any audio exists.

    Russian runs slower than English at equal word counts -- longer words, more
    syllables per word -- so the same script measured in words needs a little
    more room in RU. The 8% adjustment is a working figure to be recalibrated
    against real ElevenLabs output once a voice exists.
    """
    policy = policy or SETTINGS.pacing
    rate = policy.target_wpm * (0.92 if language is Language.RU else 1.0)
    return round(duration_for(beat.words, rate), 2)


def estimate_duration(script: Script, policy: PacingPolicy | None = None) -> float:
    return round(
        sum(
            estimate_beat_duration(beat, script.language, policy)
            for beat in script.beats
        ),
        2,
    )
