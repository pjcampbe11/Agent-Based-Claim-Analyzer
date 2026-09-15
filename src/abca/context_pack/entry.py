"""Institutional context-pack entries (doc 19).

WHAT A MECHANISM FACT IS, AND WHY IT IS ALLOWED TO EXIST HERE
=============================================================
There are two kinds of factual content relevant to an unsourced political claim,
and this package handles exactly one of them.

**Claim-specific facts** -- *"H.R. 1234 passed 220-210 on March 4"* -- are facts
ABOUT the claim. They require retrieval, they change, and asserting one from
model knowledge is fabrication under doc 18's M1.

**Mechanism facts** -- *"a political committee is a group that receives
contributions aggregating over $1,000 in a calendar year"* -- are facts about the
MACHINERY the claim invokes. They were true before the post existed, they stay
true regardless of how the claim resolves, they are anchored in statute or
regulation, and they change on the order of years.

THE PROPERTY THAT MAKES THIS SAFE
=================================
**The context pack is a source, not a generation.** No model writes a mechanism
fact at inference time. Entries are curated data, their citations were fetched
and quote-verified when the entry was written, and they are re-verified on every
build. An entry whose citation drifts or 404s fails CI.

If a model were allowed to generate mechanism facts on demand, this would be a
fabrication engine with an authoritative tone -- the single most dangerous thing
that could be added to this system. It is a lookup table instead, and the
rigidity is the entire point.

WHAT AN ENTRY MAY NOT CONTAIN
=============================
An entry says what a mechanism IS. It never says whether using it was good,
whether anyone abused it, or what a particular use of it meant. That is enforced
by :func:`evaluative_language` and checked in CI: a corpus that editorialises is
an opinion wearing a citation.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from pydantic import Field, model_validator

from abca.schema.core import ABCAModel, Citation
from abca.schema.enums import SourceTier

#: The six domains from doc 19 s5. A closed set so selection can be a table
#: lookup rather than a judgment, and so a typo becomes a build failure.
DOMAINS: frozenset[str] = frozenset({
    "procedure", "lawmaking", "budget", "instruments", "elections", "statistics",
})


class Volatility(ABCAModel):
    """How often an entry needs re-checking, and when it was last confirmed.

    Chamber rules change at the start of each Congress; a definition in the CFR
    changes when the agency amends it. An entry that does not say how fast it
    goes stale is an entry nobody will ever revisit.
    """

    rate: str = Field(pattern=r"^(low|medium|high)$")
    valid_as_of: date
    review_due: date

    @model_validator(mode="after")
    def _review_follows_validity(self) -> Volatility:
        if self.review_due <= self.valid_as_of:
            raise ValueError("review_due must be after valid_as_of")
        return self

    def is_stale(self, *, today: date | None = None) -> bool:
        """Whether this entry is past review.

        Defaults to the UTC date, not the local one. A review-due comparison in
        local time flips a day earlier or later depending on the machine's
        timezone, which would make the corpus check give different answers on a
        developer's laptop and on CI -- and the whole value of this corpus is
        that it gives the same answer everywhere.
        """
        return (today or datetime.now(UTC).date()) > self.review_due


#: Words and constructions that turn a description into a judgment.
#:
#: Deliberately blunt. A few false positives on an entry that says "should" in a
#: quoted regulation cost one author one rewrite; a single evaluative clause that
#: ships makes the corpus arguable, and the corpus's whole value is that it is
#: not arguable.
_EVALUATIVE = re.compile(
    r"""(?xi)\b(
        should | ought | must\s+not | wrongly | rightly | unfairly | fairly
      | abuse[sd]? | abusive | exploit(?:s|ed|ing)? | loophole
      | undemocratic | anti-democratic | corrupt(?:ion)?
      | outrageous | shameful | egregious | cynical
      | obviously | clearly | of\s+course
      | good | bad | better | worse | best | worst
    )\b"""
)


def evaluative_language(text: str) -> list[str]:
    """Every evaluative token in ``text``. Empty means the prose is descriptive.

    Used by the no-conclusion lint in CI. Returns the matches rather than a
    boolean so a failing build can tell the author exactly which word to fix.
    """
    return sorted({m.group(0).lower() for m in _EVALUATIVE.finditer(text)})


class ContextEntry(ABCAModel):
    """One mechanism fact, with the citation that establishes it.

    ``plain_language`` is verified ONCE, at authoring time, through the same
    three-pass fidelity gate the Analyzer uses on statutes -- not on every run.
    That is a large latency win and a larger correctness win, because a human
    reviewed the diff. It also means an error there propagates further than any
    single adjudication error, which is why the fidelity eval re-runs it in CI.
    """

    id: str = Field(pattern=r"^[a-z]+\.[a-z0-9_]+$", description="e.g. elections.political_committee")
    domain: str
    term: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()

    statement: str = Field(
        min_length=1, description="What the mechanism IS. Never what it is worth."
    )
    plain_language: str = Field(
        min_length=1, description="The same fact at roughly an eighth-grade reading level."
    )

    citation: Citation
    secondary_citations: tuple[Citation, ...] = ()

    distinguish_from: tuple[str, ...] = Field(
        default=(),
        description=(
            "Ids of the adjacent mechanisms this is confused with. The most common "
            "political misreadings are confusions between two neighbouring "
            "mechanisms, so entries are written in contrast pairs."
        ),
    )
    common_distortion: str = Field(
        default="",
        description="How this mechanism is typically misdescribed. Never who does it.",
    )

    volatility: Volatility
    entry_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")

    @model_validator(mode="after")
    def _entry_is_wellformed(self) -> ContextEntry:
        if self.domain not in DOMAINS:
            raise ValueError(
                f"entry {self.id}: domain {self.domain!r} is not one of "
                f"{sorted(DOMAINS)}. Selection is a table lookup, so an unknown "
                "domain would silently never be selected."
            )
        if not self.id.startswith(f"{self.domain[:4]}") and self.id.split(".")[0] != self.domain:
            raise ValueError(
                f"entry {self.id}: the id prefix must be its domain ({self.domain})"
            )
        if self.citation.tier.rank > SourceTier.T1.rank:
            raise ValueError(
                f"entry {self.id}: citation is {self.citation.tier.value}; a mechanism "
                "entry must rest on T0 or T1. The corpus is admissible as evidence for "
                "what it actually says, so it may not rest on reporting."
            )
        for field_name in ("statement", "common_distortion"):
            found = evaluative_language(getattr(self, field_name))
            if found:
                raise ValueError(
                    f"entry {self.id}: {field_name} contains evaluative language "
                    f"{found}. An entry states what a mechanism IS; whether its use "
                    "was good is a value claim and belongs in platform/, which src/ "
                    "cannot import."
                )
        if self.id in self.distinguish_from:
            raise ValueError(f"entry {self.id}: cannot distinguish itself from itself")
        return self

    @property
    def matches(self) -> tuple[str, ...]:
        """Every string that should select this entry: its term and its aliases."""
        return (self.term, *self.aliases)


__all__ = ["DOMAINS", "ContextEntry", "Volatility", "evaluative_language"]
