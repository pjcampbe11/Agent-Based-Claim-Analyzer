"""The promotion gate: P1-P4 (doc 20 s3).

WHAT THIS DECIDES
=================
Whether a challenge found a real source for a sourceless claim, such that the
claim can be adjudicated properly in Lane A.

Four conditions, ALL required. They are evaluated independently and every failure
is reported, not just the first -- a challenge that failed three conditions
should say so, because "we found nothing" and "we found something that matched no
prediction and confirmed no particulars" are different facts about the claim.

WHY EACH CONDITION EXISTS
=========================
**P1 -- an artifact was retrieved.** A real document at a real URL, T0-T2, with a
content hash and a timestamp. Not a search result, not a summary. Without this
there is nothing to adjudicate against and the claim is exactly as unsourced as
it arrived.

**P2 -- it satisfies a pre-registered branch outcome.** The dissect pass wrote,
BEFORE retrieval ran, what each possible result would mean. The artifact must
match one of those. This is the anti-rationalization control: without it, an
executor searches until it finds something plausible and then declares that to be
the referent. A document that matches no prediction means the reconstruction was
wrong, even when the document is interesting.

**P3 -- the referent link is confirmed, not assumed.** Four identifying
particulars -- actor, date window, jurisdiction, subject matter -- each with the
passage that establishes it. A topical match is not a confirmation; thousands of
documents are "about veterans' benefits". Three of four is PROMOTED_WEAK and is
labelled as such the whole way through.

**P4 -- the claim survived unmodified.** The substitution rule. See
:mod:`abca.schema.challenge`. This one is checked LAST in reporting order and
FIRST in authority: no combination of the other three can promote a claim whose
hash moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.canonical import digest_text
from abca.schema.challenge import (
    STRICT_PARTICULARS,
    WEAK_PARTICULARS,
    ChallengeOutcome,
    ConfirmationParticular,
    Particular,
    PromotionGrade,
    RetrievedArtifact,
)
from abca.schema.sourceless import SourcelessAnalysis


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """One gate condition, its verdict, and why."""

    name: str
    passed: bool
    reason: str


@dataclass(slots=True)
class GateResult:
    """The gate's decision, with every condition's outcome preserved."""

    grade: PromotionGrade
    conditions: list[ConditionResult] = field(default_factory=list)

    @property
    def failures(self) -> list[ConditionResult]:
        return [c for c in self.conditions if not c.passed]

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.reason}" for c in self.conditions)

    def explain(self) -> str:
        lines = [f"promotion grade: {self.grade.value}", ""]
        for condition in self.conditions:
            lines.append(f"  {'PASS' if condition.passed else 'FAIL'}  "
                         f"{condition.name}  {condition.reason}")
        return "\n".join(lines)


def check_p1(artifact: RetrievedArtifact | None) -> ConditionResult:
    """A real document was retrieved."""
    if artifact is None:
        return ConditionResult(
            "P1 artifact", False,
            "no document was retrieved; the claim is as unsourced as it arrived",
        )
    # Tier admissibility is enforced by the model, so reaching here means T0-T2.
    return ConditionResult(
        "P1 artifact", True,
        f"{artifact.tier.value} document from {artifact.connector}: {artifact.title}",
    )


def check_p2(
    analysis: SourcelessAnalysis, artifact: RetrievedArtifact | None,
    branch_matched: str,
) -> ConditionResult:
    """The artifact satisfies something the dissect pass predicted in advance."""
    branches = analysis.retrieval_plan.branch_outcomes
    if not branches:
        return ConditionResult(
            "P2 pre-registered branch", False,
            "the dissect pass registered no branch outcomes, so there is nothing "
            "the artifact could have been predicted to match; without a prediction "
            "the executor is free to rationalise whatever it found",
        )
    if artifact is None:
        return ConditionResult(
            "P2 pre-registered branch", False,
            f"no artifact to match against {len(branches)} registered branch(es)",
        )
    if not branch_matched.strip():
        return ConditionResult(
            "P2 pre-registered branch", False,
            "the executor named no branch outcome for this artifact",
        )
    known = {b.if_found for b in branches}
    if branch_matched not in known:
        return ConditionResult(
            "P2 pre-registered branch", False,
            f"the artifact was matched to {branch_matched!r}, which was NOT "
            f"pre-registered. Registered branches: {sorted(known)}. Finding "
            "something unpredicted means the reconstruction was wrong, even if the "
            "document is interesting.",
        )
    return ConditionResult(
        "P2 pre-registered branch", True, f"matched pre-registered branch {branch_matched!r}",
    )


def check_p3(particulars: tuple[ConfirmationParticular, ...]) -> ConditionResult:
    """The artifact is tied to the post by identifying particulars, each quoted."""
    seen = {p.particular for p in particulars}
    missing = sorted(p.value for p in Particular if p not in seen)
    if missing:
        return ConditionResult(
            "P3 referent confirmed", False,
            f"particulars not assessed at all: {missing}. All four must be examined; "
            "an unexamined particular is not a passing one.",
        )
    matched = sum(1 for p in particulars if p.matched)
    if matched >= STRICT_PARTICULARS:
        return ConditionResult(
            "P3 referent confirmed", True, f"all {STRICT_PARTICULARS} particulars matched",
        )
    if matched == WEAK_PARTICULARS:
        unmatched = [p.particular.value for p in particulars if not p.matched]
        return ConditionResult(
            "P3 referent confirmed", True,
            f"{matched}/{STRICT_PARTICULARS} particulars matched (missing "
            f"{unmatched}); this is a WEAK promotion and is labelled as such "
            "everywhere it appears",
        )
    return ConditionResult(
        "P3 referent confirmed", False,
        f"only {matched}/{STRICT_PARTICULARS} particulars matched; a topical "
        "resemblance is not a confirmation",
    )


def check_p4(analysis: SourcelessAnalysis) -> ConditionResult:
    """The claim entering Lane A is the one that was posted. THE rule.

    Recomputes the hash from the denatured claim rather than trusting the stored
    value, because a stored hash that was written alongside a substituted claim
    would agree with itself. The check is only meaningful if it is done against
    the text that will actually be adjudicated.
    """
    recorded = analysis.denatured_claim_hash
    actual = digest_text(analysis.denatured_claim.strip())
    if not recorded:
        return ConditionResult(
            "P4 claim unmodified", False,
            "no denatured claim hash was recorded at dissection, so there is "
            "nothing to compare and no way to show the claim was not substituted",
        )
    if recorded != actual:
        return ConditionResult(
            "P4 claim unmodified", False,
            f"the denatured claim hash MOVED between dissection and promotion "
            f"(recorded {recorded[:22]}..., now {actual[:22]}...). Something "
            "rewrote the claim. Promoting now would adjudicate a sentence nobody "
            "posted and show the reader a verdict on it.",
        )
    return ConditionResult(
        "P4 claim unmodified", True, "the claim is character-for-character what was dissected",
    )


def evaluate(
    analysis: SourcelessAnalysis,
    *,
    artifact: RetrievedArtifact | None = None,
    particulars: tuple[ConfirmationParticular, ...] = (),
    branch_matched: str = "",
    plan_exhausted: bool = False,
    future_artifact_plausible: bool = False,
    rejected: bool = False,
    rejection_reason: str = "",
) -> GateResult:
    """Run the gate. Returns a grade and every condition's result.

    ``rejected`` short-circuits for claims that are not the kind of thing a source
    settles -- unfalsifiable, normative, or halted under M6. Those are not
    failures of the search; there was never a document to find.

    ``future_artifact_plausible`` distinguishes DORMANT from UNPROMOTABLE. A
    pending bill, a scheduled vote, a docketed case or an unreleased report will
    plausibly produce an artifact later, and the honest answer changes over time.
    """
    if rejected:
        return GateResult(
            PromotionGrade.REJECTED,
            [ConditionResult("rejected", True,
                             rejection_reason or "not the kind of claim a source settles")],
        )

    conditions = [
        check_p1(artifact),
        check_p2(analysis, artifact, branch_matched),
        check_p3(particulars),
        check_p4(analysis),
    ]
    result = GateResult(PromotionGrade.UNPROMOTABLE, conditions)

    # P4 is absolute. It is evaluated alongside the others for reporting, but no
    # combination of passes can override it -- this is the one condition whose
    # failure means the whole exercise would produce a verdict on the wrong
    # sentence.
    p4 = conditions[3]
    if not p4.passed:
        result.grade = PromotionGrade.UNPROMOTABLE
        return result

    if all(c.passed for c in conditions):
        matched = sum(1 for p in particulars if p.matched)
        result.grade = (
            PromotionGrade.PROMOTED if matched >= STRICT_PARTICULARS
            else PromotionGrade.PROMOTED_WEAK
        )
        return result

    if artifact is None and future_artifact_plausible and not plan_exhausted:
        result.grade = PromotionGrade.DORMANT
        return result

    result.grade = PromotionGrade.UNPROMOTABLE
    return result


def outcome_from(
    analysis: SourcelessAnalysis,
    gate: GateResult,
    *,
    challenge_id: str,
    artifact: RetrievedArtifact | None = None,
    particulars: tuple[ConfirmationParticular, ...] = (),
    branch_matched: str = "",
    queries: tuple[str, ...] = (),
    widenings: tuple = (),
    candidates_rejected: tuple[str, ...] = (),
    recheck_after=None,
    recheck_trigger: str = "",
    tokens_spent: int = 0,
    wall_clock_ms: int = 0,
    budget_exhausted: bool = False,
) -> ChallengeOutcome:
    """Assemble the publishable record of a challenge, promoted or not."""
    return ChallengeOutcome(
        challenge_id=challenge_id,
        denatured_claim=analysis.denatured_claim,
        denatured_claim_hash=digest_text(analysis.denatured_claim.strip()),
        grade=gate.grade,
        reasons=gate.reasons,
        artifact=artifact if gate.grade.enters_lane_a else None,
        particulars=particulars,
        branch_outcome_matched=branch_matched if gate.grade.enters_lane_a else "",
        candidates_considered=tuple(c.candidate for c in analysis.referent_candidates),
        candidates_rejected=candidates_rejected,
        queries_executed=queries,
        widenings=widenings,
        recheck_after=recheck_after if gate.grade is PromotionGrade.DORMANT else None,
        recheck_trigger=recheck_trigger if gate.grade is PromotionGrade.DORMANT else "",
        tokens_spent=tokens_spent,
        wall_clock_ms=wall_clock_ms,
        budget_exhausted=budget_exhausted,
    )


__all__ = [
    "ConditionResult",
    "GateResult",
    "check_p1",
    "check_p2",
    "check_p3",
    "check_p4",
    "evaluate",
    "outcome_from",
]
