"""Selecting which mechanism entries a claim actually touches (doc 19 s3).

SELECTION IS A TABLE, NOT A JUDGMENT
====================================
Given the same pass-1 output, the same entries come back, every time. That is
required rather than merely nice: the selected entries carry citations into the
run record, so a selection that varied between runs would make two otherwise
identical analyses cite different sources and diff as ``DIVERGENT``.

So selection keys off three fields the dissect pass already produced --
``atomic_claims[].subtype``, ``domain_failure_patterns[].pattern`` and
``referent_candidates[].distortion_applied`` -- through the mapping tables below.
No model call, no scoring, no similarity threshold.

RECALL OVER PRECISION
=====================
Doc 19 s9: *"a missing entry is a worse brief, an extra entry is noise."* The
tables are therefore generous. An entry that turns out to be marginally relevant
costs the reader a paragraph; an entry that should have been there and was not
costs them the explanation that would have made the whole brief make sense.
"""

from __future__ import annotations

from dataclasses import dataclass

from abca.context_pack.corpus import ContextPack
from abca.context_pack.entry import ContextEntry
from abca.schema.sourceless import ClaimSubtype, SourcelessAnalysis
from abca.sourceless.distortions import distortion_by_name

#: Claim subtype -> the domains whose entries explain that kind of claim.
SUBTYPE_DOMAINS: dict[ClaimSubtype, tuple[str, ...]] = {
    ClaimSubtype.PROCEDURAL: ("procedure", "lawmaking"),
    ClaimSubtype.ROLL_CALL: ("procedure", "lawmaking"),
    ClaimSubtype.STATUTORY_CONTENT: ("lawmaking", "instruments"),
    ClaimSubtype.STATISTICAL: ("statistics",),
    ClaimSubtype.FACTUAL_EVENT: ("lawmaking",),
    ClaimSubtype.QUOTE: (),
    ClaimSubtype.CAUSAL: (),
    ClaimSubtype.MOTIVE: (),
}

#: Domain-failure pattern text -> domains. Matched as a substring, case-folded,
#: because the pattern strings are model-written prose and matching them exactly
#: would make selection brittle in the direction that loses entries.
PATTERN_DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("procedural", ("procedure",)),
    ("motion to recommit", ("procedure",)),
    ("motion to table", ("procedure",)),
    ("amendment", ("procedure",)),
    ("committee", ("procedure", "lawmaking")),
    ("chamber", ("lawmaking",)),
    ("introduced", ("lawmaking",)),
    ("title", ("lawmaking",)),
    ("omnibus", ("budget",)),
    ("cbo", ("budget",)),
    ("score", ("budget",)),
    ("appropriat", ("budget",)),
    ("executive order", ("instruments",)),
    ("guidance", ("instruments",)),
    ("rule", ("instruments",)),
    ("court", ("instruments",)),
    ("state", ("instruments",)),
    ("statistic", ("statistics",)),
    ("denominator", ("statistics",)),
    ("baseline", ("statistics",)),
    ("nominal", ("statistics",)),
    ("ballot", ("elections",)),
    ("primary", ("elections",)),
    ("contribution", ("elections",)),
)


@dataclass(frozen=True, slots=True)
class SelectedEntry:
    """One entry, with WHY it was selected and whether it is stale.

    ``selected_by`` is published. A reader who thinks an entry is irrelevant is
    entitled to see which field pulled it in, and an author fixing a bad
    selection needs to know which table to edit.
    """

    entry: ContextEntry
    selected_by: str
    stale: bool

    def relevance(self) -> str:
        return f"selected by {self.selected_by}"


def _domains_for(analysis: SourcelessAnalysis) -> list[tuple[str, str]]:
    """Every (domain, reason) pair this analysis implies, in a stable order."""
    hits: list[tuple[str, str]] = []

    for index, atom in enumerate(analysis.atomic_claims):
        for domain in SUBTYPE_DOMAINS.get(atom.subtype, ()):
            hits.append((domain, f"atomic_claims[{index}].subtype={atom.subtype.value}"))

    for index, pattern in enumerate(analysis.domain_failure_patterns):
        text = pattern.pattern.casefold()
        for needle, domains in PATTERN_DOMAINS:
            if needle in text:
                for domain in domains:
                    hits.append((domain, f"domain_failure_patterns[{index}]"))

    for index, candidate in enumerate(analysis.referent_candidates):
        distortion = distortion_by_name(candidate.distortion_applied)
        hits.append(
            (distortion.domain,
             f"referent_candidates[{index}].distortion_applied={distortion.name}")
        )

    return hits


def select_entries(
    analysis: SourcelessAnalysis,
    pack: ContextPack,
    *,
    limit: int = 6,
) -> list[SelectedEntry]:
    """Choose the mechanism entries this analysis touches. Deterministic.

    Entries are returned in corpus order within each domain, and domains in the
    order the analysis implied them, so the output is stable under reordering of
    equally-relevant hits. ``limit`` caps the brief's length; entries beyond it
    are dropped rather than truncated mid-entry, because half an explanation is
    worse than none.

    Contrast pairs ride along: if an entry names a ``distinguish_from`` target
    that is also in the corpus, that target is pulled in too. The most common
    political misreadings are confusions between two adjacent mechanisms, so an
    entry without its contrast is the half of the explanation that does not
    resolve the confusion.
    """
    chosen: dict[str, SelectedEntry] = {}

    for domain, reason in _domains_for(analysis):
        for entry in pack.in_domain(domain):
            if entry.id not in chosen:
                chosen[entry.id] = SelectedEntry(
                    entry=entry, selected_by=reason, stale=entry.volatility.is_stale()
                )

    # Second pass: contrast pairs. Marked with their own reason so the brief can
    # say why a seemingly unrelated entry appeared.
    for selected in list(chosen.values()):
        for ref in selected.entry.distinguish_from:
            if ref in chosen:
                continue
            partner = pack.by_id(ref)
            if partner is not None:
                chosen[ref] = SelectedEntry(
                    entry=partner,
                    selected_by=f"distinguish_from {selected.entry.id}",
                    stale=partner.volatility.is_stale(),
                )

    return list(chosen.values())[:limit]


__all__ = ["PATTERN_DOMAINS", "SUBTYPE_DOMAINS", "SelectedEntry", "select_entries"]
