"""Consensus mode: run several models and report where they disagree.

THE THING THIS MODE MUST NOT DO
===============================
Average.

Running three models and publishing the majority verdict produces a number that
looks more trustworthy than any single model's and is, in the case that matters,
less so: when two models say SUPPORTED and one says CONTRADICTED, the majority
answer hides the single most useful fact the run produced -- that models reading
the same sources reached opposite conclusions. A claim that splits a panel is
not a claim that is 67% true. It is a claim that is not settled by what the
panel could see.

So this stage publishes **the weakest verdict the panel reached**, and reports
the split as a first-class number on the claim
(:attr:`~abca.schema.core.Claim.model_disagreement`). Every verdict is listed in
the reasoning, named by model.

WHY THE WEAKEST AND NOT THE MAJORITY
====================================
The same reason the red team can only ratchet downward (contract s7): a
mechanism with the power to change verdicts is trustworthy in proportion to how
narrow that power is. Publishing the weakest means the worst a disagreeing panel
can do is make the tool say LESS than one of its members knew -- a failure, but
a safe one. Publishing the majority means a 2-1 split can manufacture a
confident verdict that a third of the evidence-readers rejected.

Contract R4 says silence beats invention. A split panel is exactly the situation
that rule was written for.

THE CASE THE STRENGTH TABLE CANNOT SETTLE
=========================================
``SUPPORTED`` and ``CONTRADICTED`` are equally assertive -- both are strength 3
in :data:`~abca.pipeline.red_team.VERDICT_STRENGTH` -- so "weakest" does not
choose between them. That is not an oversight in the table; they are opposite,
not ordered.

A panel holding both is the most informative failure this tool can produce, and
it resolves to ``UNSUPPORTED`` at confidence 0.0 with both sides' citations
attached and a note saying plainly what happened. ``MIXED`` was the alternative
and was rejected: MIXED is a claim about the EVIDENCE ("substantive support on
both sides"), and asserting it because two models disagreed would put the
tool's own confusion into a slot reserved for a reading of the sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.pipeline.adjudicate import AdjudicationRecord, run_adjudicate
from abca.pipeline.base import StageOutcome
from abca.pipeline.models import DraftClaim
from abca.pipeline.red_team import VERDICT_STRENGTH
from abca.pipeline.retrieve import RetrievalResult
from abca.providers.base import Provider, ProviderError
from abca.schema.core import Citation
from abca.schema.enums import Verdict

#: Where a directional conflict lands. Not MIXED -- see the module docstring.
CONFLICT_VERDICT = Verdict.UNSUPPORTED

#: Verdicts that assert something in a DIRECTION. A panel holding both is in
#: conflict rather than merely split.
_DIRECTIONAL: frozenset[Verdict] = frozenset({Verdict.SUPPORTED, Verdict.CONTRADICTED})


@dataclass(slots=True)
class PanelVerdict:
    """One panel member's answer for one claim."""

    model: str
    record: AdjudicationRecord


@dataclass(slots=True)
class ConsensusRecord:
    """The merged answer for one claim, plus every input to it."""

    claim_id: str
    merged: AdjudicationRecord
    votes: list[PanelVerdict] = field(default_factory=list)
    #: 0.0 when unanimous; 1 - (largest bloc / panel size) otherwise.
    disagreement: float = 0.0
    #: True when the panel held both SUPPORTED and CONTRADICTED.
    conflicted: bool = False
    #: Models that produced no verdict for this claim at all.
    silent: list[str] = field(default_factory=list)

    @property
    def unanimous(self) -> bool:
        return self.disagreement == 0.0 and not self.conflicted

    def summary(self) -> str:
        """One line naming each model's verdict. For the report and the record."""
        return "; ".join(
            f"{vote.model}={vote.record.verdict.value}@{vote.record.confidence:.2f}"
            for vote in self.votes
        )


@dataclass(slots=True)
class ConsensusResult:
    """Merged adjudications, plus the panel detail behind them."""

    records: dict[str, ConsensusRecord] = field(default_factory=dict)
    #: Model names in panel order, as resolved from the live backends.
    panel: list[str] = field(default_factory=list)

    def adjudications(self) -> dict[str, AdjudicationRecord]:
        """The merged records, in the shape the rest of the pipeline expects."""
        return {claim_id: record.merged for claim_id, record in self.records.items()}

    def disagreement(self, claim_id: str) -> float | None:
        record = self.records.get(claim_id)
        return record.disagreement if record else None

    @property
    def split_count(self) -> int:
        return sum(1 for record in self.records.values() if not record.unanimous)


def measure_disagreement(verdicts: list[Verdict]) -> float:
    """``1 - (largest bloc / panel size)``. 0.0 when unanimous.

    Deliberately about VERDICTS, not confidences. Two models that both say
    SUPPORTED at 0.7 and 0.9 agree in every sense a reader cares about; two that
    say SUPPORTED and UNSUPPORTED do not, however close their self-reported
    numbers happen to be. Averaging confidences would blur exactly the signal
    this mode exists to surface.

    A panel of 2 that splits scores 0.5; a panel of 3 splitting 2-1 scores 0.33;
    a panel of 3 with three different verdicts scores 0.67.
    """
    if len(verdicts) < 2:
        return 0.0
    counts: dict[Verdict, int] = {}
    for verdict in verdicts:
        counts[verdict] = counts.get(verdict, 0) + 1
    return round(1.0 - (max(counts.values()) / len(verdicts)), 4)


def weakest(verdicts: list[Verdict]) -> Verdict:
    """The least assertive verdict in the list.

    Ties are broken by the order of :data:`VERDICT_STRENGTH`, which is stable,
    so the merge is reproducible. A verdict absent from the table (UNVERIFIABLE,
    OUT_OF_SCOPE) never reaches this stage -- those follow from the claim's type
    and the gate, not from evidence.
    """
    return min(verdicts, key=lambda verdict: (VERDICT_STRENGTH.get(verdict, 0),))


def merge_citations(votes: list[PanelVerdict]) -> list[Citation]:
    """Union of every panel member's VERIFIED citations, deduplicated.

    The union rather than the winning member's, because a reader should see
    everything the panel found -- including the passage that persuaded the
    dissenter. Each citation was already verified verbatim against the
    retrieved source by the member that offered it, so the union contains no
    unverified text.

    Deduplicated on (url, quote) and kept in panel order, so the merge is
    reproducible.
    """
    seen: set[tuple[str, str]] = set()
    merged: list[Citation] = []
    for vote in votes:
        for citation in vote.record.citations:
            key = (citation.url, " ".join(citation.quote.split()))
            if key in seen:
                continue
            seen.add(key)
            merged.append(citation)
    return merged


def merge(claim_id: str, votes: list[PanelVerdict], silent: list[str]) -> ConsensusRecord:
    """Combine one claim's panel verdicts into the record that gets published."""
    verdicts = [vote.record.verdict for vote in votes]
    disagreement = measure_disagreement(verdicts)
    conflicted = len(_DIRECTIONAL & set(verdicts)) == 2
    citations = merge_citations(votes)

    detail = "; ".join(
        f"{vote.model} said {vote.record.verdict.value} "
        f"({vote.record.confidence:.2f}): {vote.record.reasoning}"
        for vote in votes
    )

    if conflicted:
        verdict = CONFLICT_VERDICT
        confidence = 0.0
        reasoning = (
            "PANEL CONFLICT. Members of the panel read the same sources and reached "
            "OPPOSITE conclusions, so this run does not settle the claim, and the "
            "disagreement is the finding. Published as UNSUPPORTED at zero "
            f"confidence with every side's citations attached. Panel: {detail}"
        )
    elif disagreement == 0.0:
        verdict = verdicts[0]
        confidence = min(vote.record.confidence for vote in votes)
        reasoning = (
            f"Panel of {len(votes)} agreed unanimously on {verdict.value}. "
            f"Confidence is the LOWEST any member reported, not the average. {detail}"
        )
    else:
        verdict = weakest(verdicts)
        confidence = min(vote.record.confidence for vote in votes)
        reasoning = (
            f"Panel of {len(votes)} SPLIT (disagreement {disagreement:.2f}). Published "
            f"as {verdict.value}, the least assertive verdict any member reached -- "
            "not the majority. A split panel means the sources did not settle the "
            f"claim for every reader of them. {detail}"
        )

    if silent:
        reasoning += (
            f" [{', '.join(silent)} returned no verdict for this claim; the merge "
            "covers only the members that answered.]"
        )

    # The schema's evidence gate applies to the merged verdict exactly as it
    # would to a single model's. A conflict resolving to UNSUPPORTED needs no
    # citation, and keeping the union anyway is what lets a reader see both
    # sides of the disagreement rather than taking the conflict on trust.
    merged = AdjudicationRecord(
        claim_id=claim_id,
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        citations=citations,
        rejected=[reason for vote in votes for reason in vote.record.rejected],
    )
    return ConsensusRecord(
        claim_id=claim_id,
        merged=merged,
        votes=votes,
        disagreement=disagreement,
        conflicted=conflicted,
        silent=silent,
    )


def run_consensus(
    panel: list[Provider],
    claims: list[DraftClaim],
    retrieval: RetrievalResult,
    *,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
) -> StageOutcome[ConsensusResult]:
    """Adjudicate every claim on every panel member, then merge.

    Members run in PANEL ORDER, sequentially. Order is part of the recipe: it
    decides how citations are deduplicated and how ties break, so a concurrent
    version would have to sort the results back into this order anyway -- and
    would have made the run's cost harder to attribute in the meantime.

    A member that fails entirely is recorded as silent rather than allowed to
    fail the run. Two models that answered are still a panel; the merge says how
    many answered, and the reasoning names who did not.
    """
    outcome: StageOutcome[ConsensusResult] = StageOutcome(value=ConsensusResult())
    result = outcome.value

    by_model: dict[str, dict[str, AdjudicationRecord]] = {}
    for provider in panel:
        try:
            name = provider.identity().name
        except ProviderError:
            name = getattr(provider, "name", "unknown")
        result.panel.append(name)

        try:
            member = run_adjudicate(
                provider, claims, retrieval,
                seed=seed, temperature=temperature,
                max_attempts=max_attempts, context_length=context_length,
            )
        except ProviderError as exc:
            outcome.note(
                f"consensus member {name} failed entirely ({type(exc).__name__}): "
                f"{exc}. The panel continues without it, and every claim below "
                "records that this member did not answer."
            )
            by_model[name] = {}
            continue

        outcome.absorb_stage(member, prefix=f"[{name}] ")
        by_model[name] = member.value

    claim_ids = [claim.id for claim in claims]
    for claim_id in claim_ids:
        votes = [
            PanelVerdict(model=name, record=records[claim_id])
            for name, records in by_model.items()
            if claim_id in records
        ]
        if not votes:
            continue
        silent = [name for name, records in by_model.items() if claim_id not in records]
        result.records[claim_id] = merge(claim_id, votes, silent)

    conflicts = [r.claim_id for r in result.records.values() if r.conflicted]
    if conflicts:
        outcome.note(
            f"PANEL CONFLICT on {len(conflicts)} claim(s) ({', '.join(conflicts[:5])}"
            + (", ..." if len(conflicts) > 5 else "")
            + "): members reached OPPOSITE conclusions from the same sources. Those "
            "claims are published as UNSUPPORTED at zero confidence, with every "
            "side's citations attached. The disagreement is the finding."
        )
    if result.split_count:
        outcome.note(
            f"{result.split_count} of {len(result.records)} claim(s) split the panel. "
            "Each is published with the LEAST assertive verdict any member reached, "
            "never the majority: a split means the sources did not settle the claim "
            "for every reader of them."
        )
    outcome.note(
        f"consensus panel of {len(result.panel)}: {', '.join(result.panel)}; "
        f"{len(result.records)} claim(s) merged"
    )
    outcome.dedupe_notes()
    return outcome


__all__ = [
    "CONFLICT_VERDICT",
    "ConsensusRecord",
    "ConsensusResult",
    "PanelVerdict",
    "measure_disagreement",
    "merge",
    "merge_citations",
    "run_consensus",
    "weakest",
]
