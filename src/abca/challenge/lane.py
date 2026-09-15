"""Lane B end to end: intake, dissect, context, execute, gate, publish (doc 20 s2).

THE SEQUENCE
============
    B1 intake      -> the queue, deduplicated by cluster
    B2 dissect     -> doc 18, pass 1
    B3 context     -> doc 19, pass 2
    B4 execute     -> RUN the retrieval plan, with bounded widening
    B5 gate        -> P1-P4
    B6 disposition -> promote, hold dormant, or publish as unpromotable

Each step is a separate function so a caller can run one without the others, and
so the gate can be tested against hand-built inputs without a model or a network.

WHAT RE-ENTERS LANE A, AND WHAT DOES NOT
========================================
Only PROMOTED and PROMOTED_WEAK. Everything else is published as it stands.

And what re-enters is the DENATURED CLAIM -- the sentence that was posted -- with
the retrieved artifact attached as evidence and the reconstruction attached as
provenance. Never the reconstruction as the claim. See
:mod:`abca.schema.challenge`.

NO PROMOTION CASCADE
====================
A promoted claim runs Lane A once and exits with a verdict. It cannot re-enter
Lane B, cannot spawn a second challenge, and cannot be re-promoted. Enforced by
the presence of ``promoted_from``: :func:`intake` refuses a claim that carries
one. Without that guard a claim could bounce between lanes, accumulating
provenance and widenings until something eventually matched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from abca.challenge.executor import Budget, ExecutionResult, execute_plan
from abca.challenge.gate import GateResult, evaluate, outcome_from
from abca.challenge.queue import Challenge, ChallengeQueue
from abca.schema.challenge import (
    ChallengeOutcome,
    ConfirmationParticular,
    PromotedFrom,
    PromotionGrade,
    RetrievedArtifact,
)
from abca.schema.sourceless import SourcelessAnalysis

#: How far ahead a dormant challenge is re-checked when no better trigger exists.
#: Deliberately weeks, not days: the claims that go dormant are about pending
#: legislation and unreleased reports, and re-asking daily spends budget on a
#: question whose answer cannot have changed.
DEFAULT_RECHECK_DAYS = 30


class PromotionCascade(RuntimeError):
    """A claim that already carries promotion provenance tried to re-enter Lane B."""


def intake(
    queue: ChallengeQueue,
    denatured_claim: str,
    *,
    cluster_key: str | None = None,
    promoted_from: PromotedFrom | None = None,
    platform: str = "",
    posted_date: str = "",
    parent_context: str = "",
) -> tuple[Challenge, bool]:
    """B1. Queue a sourceless claim, deduplicated by cluster.

    Refuses a claim carrying ``promoted_from``: that claim has already been
    through the lane, and letting it back in is how a challenge loops until
    something matches.
    """
    if promoted_from is not None:
        raise PromotionCascade(
            f"claim already promoted by challenge {promoted_from.challenge_id}; "
            "a promoted claim runs Lane A once and exits. Re-entering Lane B would "
            "let it accumulate widenings until something matched (doc 20 s6)."
        )
    return queue.enqueue(
        denatured_claim, cluster_key=cluster_key, platform=platform,
        posted_date=posted_date, parent_context=parent_context,
    )


@dataclass(slots=True)
class ChallengeRun:
    """Everything one execution produced, for publishing and for debugging."""

    challenge: Challenge
    analysis: SourcelessAnalysis
    execution: ExecutionResult
    gate: GateResult
    outcome: ChallengeOutcome
    promoted_from: PromotedFrom | None = None

    @property
    def enters_lane_a(self) -> bool:
        return self.outcome.grade.enters_lane_a


def run_challenge(
    challenge: Challenge,
    analysis: SourcelessAnalysis,
    fetch: Callable[[str], RetrievedArtifact | None],
    confirm: Callable[[RetrievedArtifact, SourcelessAnalysis],
                      tuple[tuple[ConfirmationParticular, ...], str]],
    *,
    budget: Budget | None = None,
    future_artifact_plausible: bool = False,
    recheck_trigger: str = "",
    rejected: bool = False,
    rejection_reason: str = "",
    today: date | None = None,
) -> ChallengeRun:
    """B4-B6. Execute the plan, run the gate, and build the publishable outcome.

    ``confirm`` inspects a retrieved artifact and returns the four particulars
    plus the branch outcome it matches. Injected rather than implemented here
    because confirmation is a reading task -- it needs the artifact's text -- and
    keeping it out of this module lets the whole lane be tested deterministically.

    ``rejected`` short-circuits the gate for claims no source can settle. Those
    are not search failures; there was never a document to find.
    """
    budget = budget or Budget()
    execution = ExecutionResult()
    particulars: tuple[ConfirmationParticular, ...] = ()
    branch_matched = ""

    if not rejected:
        execution = execute_plan(
            list(analysis.retrieval_plan.queries), fetch, budget=budget
        )
        if execution.artifact is not None:
            particulars, branch_matched = confirm(execution.artifact, analysis)

    gate = evaluate(
        analysis,
        artifact=execution.artifact,
        particulars=particulars,
        branch_matched=branch_matched,
        plan_exhausted=execution.plan_exhausted,
        future_artifact_plausible=future_artifact_plausible,
        rejected=rejected,
        rejection_reason=rejection_reason,
    )

    recheck_after = None
    if gate.grade is PromotionGrade.DORMANT:
        base = today or datetime.now(UTC).date()
        recheck_after = base + timedelta(days=DEFAULT_RECHECK_DAYS)

    outcome = outcome_from(
        analysis, gate,
        challenge_id=challenge.challenge_id,
        artifact=execution.artifact,
        particulars=particulars,
        branch_matched=branch_matched,
        queries=tuple(execution.queries_executed),
        widenings=tuple(execution.widenings),
        candidates_rejected=tuple(
            c.candidate for c in analysis.referent_candidates[1:]
        ),
        recheck_after=recheck_after,
        recheck_trigger=recheck_trigger or (
            "no better trigger than a date; see doc 20 open question 1"
            if gate.grade is PromotionGrade.DORMANT else ""
        ),
        tokens_spent=budget.tokens_spent,
        wall_clock_ms=budget.elapsed_ms,
        budget_exhausted=execution.budget_exhausted,
    )

    promoted_from = None
    if gate.grade.enters_lane_a:
        top = analysis.referent_candidates[0] if analysis.referent_candidates else None
        promoted_from = PromotedFrom(
            challenge_id=challenge.challenge_id,
            denatured_claim_hash=analysis.denatured_claim_hash,
            referent_candidate_used=top.candidate if top else "",
            distortion_applied=top.distortion_applied if top else "",
            referent_confidence=analysis.referent_confidence.value,
            confirmation_particulars=particulars,
            branch_outcome_matched=branch_matched,
            queries_executed=len(execution.queries_executed),
            widenings=tuple(execution.widenings),
            promotion_grade=gate.grade,
        )

    return ChallengeRun(
        challenge=challenge, analysis=analysis, execution=execution,
        gate=gate, outcome=outcome, promoted_from=promoted_from,
    )


def lane_a_input(run: ChallengeRun) -> str:
    """The exact text that goes to Lane A. THE substitution-rule checkpoint.

    Returns the denatured claim, never the reconstruction, and re-verifies the
    hash before handing it over. Callers should use this rather than reading the
    analysis directly, so there is exactly one place where a substitution could
    happen and exactly one place that checks for it.
    """
    from abca.canonical import digest_text

    if not run.enters_lane_a:
        raise ValueError(
            f"challenge {run.challenge.challenge_id} graded "
            f"{run.outcome.grade.value} and does not enter Lane A"
        )
    text = run.analysis.denatured_claim
    if digest_text(text.strip()) != run.analysis.denatured_claim_hash:
        raise PromotionCascade(
            "the denatured claim hash does not match its text at the Lane A "
            "handoff. The gate passed P4, so this changed afterwards -- refusing "
            "to adjudicate a sentence that was rewritten after promotion."
        )
    return text


__all__ = [
    "DEFAULT_RECHECK_DAYS",
    "ChallengeRun",
    "PromotionCascade",
    "intake",
    "lane_a_input",
    "run_challenge",
]
