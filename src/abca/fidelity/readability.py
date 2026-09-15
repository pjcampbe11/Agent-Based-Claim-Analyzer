"""Flesch-Kincaid grade level, computed deterministically.

WHY NOT A LIBRARY
=================
``textstat`` and friends disagree with each other on syllable counting, change
their answers between versions, and would put a floating readability score --
which lands in the run record -- at the mercy of a dependency upgrade. The
formula is three terms; the only hard part is syllables, and a documented
heuristic that never changes is worth more here than one that is marginally
more accurate but drifts.

WHERE READABILITY SITS IN THE HIERARCHY
=======================================
Below fidelity, always. The contract asks for grade 7-9, but a rendering that
hits grade 8 by dropping an exception is worse than one at grade 11 that keeps
it. So an out-of-band grade is a FLAG, never a hard failure: it is reported,
it is visible to a reader, and it does not by itself trigger regeneration.

Readability is the goal. Fidelity is the constraint. When they conflict,
fidelity wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Contract s5: the plain-language target band.
TARGET_GRADE_MIN = 7.0
TARGET_GRADE_MAX = 9.0

_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")

#: Words whose trailing "e" is pronounced, so the silent-e rule must not fire.
_PRONOUNCED_FINAL_E = frozenset({"the", "he", "she", "we", "be", "me", "recipe", "apostrophe"})

#: Endings that add a syllable despite ending in a silent "e".
_SYLLABIC_ENDINGS = ("le", "les")


def count_syllables(word: str) -> int:
    """Estimate syllables in a single word.

    Vowel-group counting with the two corrections that matter most in
    statutory prose: silent terminal ``e`` ("filed" is one syllable, not two)
    and the consonant + ``le`` ending that keeps its syllable ("eligible").

    Never returns less than 1 -- every word a reader says out loud takes at
    least one beat, and a zero would let a sentence of short words report an
    impossible grade.
    """
    cleaned = word.lower().strip("'-")
    if not cleaned:
        return 1

    groups = _VOWEL_GROUP.findall(cleaned)
    count = len(groups)

    # Silent "-ed". The commonest inflection in statutory prose, and the
    # commonest way a naive vowel-group count inflates: "filed" is one
    # syllable, "wanted" is two. The "e" is pronounced only after t or d.
    if cleaned.endswith("ed") and len(cleaned) > 3 and count > 1:
        preceding = cleaned[-3]
        if preceding not in "td" and preceding not in "aeiouy":
            count -= 1

    if cleaned.endswith("e") and cleaned not in _PRONOUNCED_FINAL_E:
        if cleaned.endswith(_SYLLABIC_ENDINGS) and len(cleaned) > 2 and \
                cleaned[-3] not in "aeiouy":
            pass  # "eligible", "principles" -- the "le" is its own syllable
        elif count > 1:
            count -= 1

    return max(1, count)


def count_sentences(text: str) -> int:
    """Count sentences, never returning zero.

    A fragment with no terminal punctuation is one sentence for scoring
    purposes; treating it as zero would divide by zero in the formula.
    """
    matches = len(_SENTENCE_END.findall(text))
    return max(1, matches)


@dataclass(frozen=True, slots=True)
class ReadabilityScore:
    """A Flesch-Kincaid grade plus the counts behind it."""

    grade: float
    words: int
    sentences: int
    syllables: int

    @property
    def in_target_band(self) -> bool:
        return TARGET_GRADE_MIN <= self.grade <= TARGET_GRADE_MAX

    @property
    def words_per_sentence(self) -> float:
        return self.words / self.sentences if self.sentences else 0.0

    def describe(self) -> str:
        if self.in_target_band:
            return f"grade {self.grade:.1f} (within the {TARGET_GRADE_MIN:.0f}-{TARGET_GRADE_MAX:.0f} target)"
        direction = "above" if self.grade > TARGET_GRADE_MAX else "below"
        return (
            f"grade {self.grade:.1f} ({direction} the "
            f"{TARGET_GRADE_MIN:.0f}-{TARGET_GRADE_MAX:.0f} target)"
        )


def flesch_kincaid(text: str) -> ReadabilityScore:
    """Flesch-Kincaid grade level.

    ``0.39 * (words/sentences) + 11.8 * (syllables/word) - 15.59``

    Empty or word-free text scores 0.0 rather than raising: an empty rendering
    is already a fidelity failure, and a crash here would mask it behind a
    less informative error.
    """
    words = _WORD.findall(text)
    if not words:
        return ReadabilityScore(grade=0.0, words=0, sentences=0, syllables=0)

    sentences = count_sentences(text)
    syllables = sum(count_syllables(word) for word in words)

    grade = (
        0.39 * (len(words) / sentences)
        + 11.8 * (syllables / len(words))
        - 15.59
    )
    return ReadabilityScore(
        grade=round(grade, 2),
        words=len(words),
        sentences=sentences,
        syllables=syllables,
    )


__all__ = [
    "TARGET_GRADE_MAX",
    "TARGET_GRADE_MIN",
    "ReadabilityScore",
    "count_sentences",
    "count_syllables",
    "flesch_kincaid",
]
