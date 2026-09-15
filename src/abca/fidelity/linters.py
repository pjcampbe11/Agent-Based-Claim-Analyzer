"""Cheap deterministic checks on a plain-language rendering.

Two linters, both running on the source text and the rendering and comparing
the results. Neither is a hard gate -- the element diff is -- but both are
nearly free and they catch a different class of error.

THE SCOPE LINTER
================
The most dangerous simplification error is not a dropped sentence. It is a
dropped *word*: an "only", an "unless", a "not more than". The sentence still
reads well, still covers the same subject, and now says something else.

    "signed by 1% ... or 25,000 qualified voters, whichever is less"
    "signed by 1% ... or 25,000 qualified voters"

The element diff catches this when the scope word lives in its own element. It
does not when the word sits inside an element that otherwise matches -- token
overlap survives losing one word. So the scope linter counts these words
directly, on both texts, and flags a shortfall.

It flags rather than fails because a rewrite legitimately rephrases: "whichever
is less" can become "the smaller of the two" with no limiter word at all. A
hard failure would punish good writing; a flag puts it in front of a human.

THE GLOSSARY
============
Some phrases are terms of art whose plain-language paraphrase is wrong by
construction. "Reasonable suspicion" is not "a good reason to think";
"de novo" is not "again". Rewriting them away does not simplify the law, it
states a different law.

So the glossary is LOCKED: if the source contains one of these, the rendering
must contain it too, verbatim. The plain-language explanation belongs in a
footnote beside the term, not in place of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Words that limit, qualify or negate. Losing one changes the rule.
#:
#: Grouped by what they do, because an unexplained word list rots -- nobody
#: can tell later whether an entry is load-bearing or was pasted in.
SCOPE_MARKERS: dict[str, tuple[str, ...]] = {
    "negation": (
        "not", "no", "never", "neither", "nor", "cannot", "without", "none",
    ),
    "exception": (
        "except", "unless", "other than", "notwithstanding", "provided that",
        "but not", "excluding", "save for",
    ),
    "limiter": (
        "only", "solely", "at least", "at most", "no more than", "not more than",
        "no less than", "not less than", "whichever is less", "whichever is greater",
        "up to", "maximum", "minimum", "lesser of", "greater of",
    ),
    "universal": (
        "all", "every", "each", "any", "always", "entire", "whole",
    ),
}

#: Terms of art that may not be paraphrased away. If the source uses one, the
#: rendering must carry it verbatim; explain it in a footnote beside the term.
LOCKED_GLOSSARY: tuple[str, ...] = (
    "reasonable suspicion", "probable cause", "preponderance of the evidence",
    "beyond a reasonable doubt", "clear and convincing",
    "strict scrutiny", "intermediate scrutiny", "rational basis",
    "de novo", "prima facie", "mens rea", "actus reus",
    "notwithstanding", "ex parte", "in camera", "amicus curiae",
    "habeas corpus", "res judicata", "stare decisis", "ultra vires",
    "due process", "equal protection", "good faith", "bad faith",
    "burden of proof", "standard of review", "arbitrary and capricious",
    "eminent domain", "qualified immunity", "sovereign immunity",
)


def _count_phrase(text: str, phrase: str) -> int:
    """Count occurrences of ``phrase`` with word boundaries, case-insensitively."""
    pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b"
    return len(re.findall(pattern, text, re.IGNORECASE))


@dataclass(slots=True)
class ScopeReport:
    """Scope-marker counts in the source versus the rendering."""

    source_counts: dict[str, int] = field(default_factory=dict)
    rendering_counts: dict[str, int] = field(default_factory=dict)
    #: Categories where the rendering has FEWER markers than the source.
    weakened: list[str] = field(default_factory=list)
    #: Categories where the rendering has MORE. Less common, still worth seeing.
    strengthened: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.weakened and not self.strengthened

    def flags(self) -> list[str]:
        """Reader-facing descriptions of every mismatch."""
        messages: list[str] = []
        for category in self.weakened:
            source = self.source_counts.get(category, 0)
            rendering = self.rendering_counts.get(category, 0)
            messages.append(
                f"{category}: source has {source}, rendering has {rendering}. "
                f"Losing a {category} word can change what the rule covers."
            )
        for category in self.strengthened:
            source = self.source_counts.get(category, 0)
            rendering = self.rendering_counts.get(category, 0)
            messages.append(
                f"{category}: rendering has {rendering}, source has {source}. "
                "The rewrite may have narrowed or negated more than the source does."
            )
        return messages


def scope_lint(source: str, rendering: str) -> ScopeReport:
    """Compare scope-marker density between a source and its rendering.

    Counts by CATEGORY rather than by individual word, because a rewrite that
    turns "shall not" into "is prohibited from" has kept the negation while
    changing the word. Category counts survive that; word counts would not.
    """
    report = ScopeReport()
    for category, phrases in SCOPE_MARKERS.items():
        source_count = sum(_count_phrase(source, phrase) for phrase in phrases)
        rendering_count = sum(_count_phrase(rendering, phrase) for phrase in phrases)
        report.source_counts[category] = source_count
        report.rendering_counts[category] = rendering_count
        if rendering_count < source_count:
            report.weakened.append(category)
        elif rendering_count > source_count:
            report.strengthened.append(category)
    return report


@dataclass(slots=True)
class GlossaryReport:
    """Terms of art found in the source and whether the rendering kept them."""

    present: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.dropped

    def flags(self) -> list[str]:
        return [
            f"term of art {term!r} appears in the source but not in the rendering. "
            "Paraphrasing it states a different rule; keep the term and footnote it."
            for term in self.dropped
        ]


def glossary_lint(source: str, rendering: str) -> GlossaryReport:
    """Check that every locked term in the source survives into the rendering."""
    report = GlossaryReport()
    for term in LOCKED_GLOSSARY:
        if _count_phrase(source, term) == 0:
            continue
        report.present.append(term)
        if _count_phrase(rendering, term) > 0:
            report.preserved.append(term)
        else:
            report.dropped.append(term)
    return report


__all__ = [
    "LOCKED_GLOSSARY",
    "SCOPE_MARKERS",
    "GlossaryReport",
    "ScopeReport",
    "glossary_lint",
    "scope_lint",
]
