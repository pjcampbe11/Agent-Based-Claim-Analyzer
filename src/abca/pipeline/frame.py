"""The frame stage: separate what evidence settles from what it cannot.

WHY THIS IS NOT A MODEL CALL
============================
The partition is already determined by data the pipeline has: each claim
carries a :class:`~abca.schema.enums.ClaimType` from the classify stage and a
:class:`~abca.schema.enums.Verdict` from adjudication. Which bucket a claim
belongs in follows from those two facts by a rule, and a rule is cheaper,
reproducible, and cannot be argued with.

Asking a model to do it again would introduce a second, softer opinion about a
question the contract has already answered -- and the failure mode of that
second opinion is always the same direction. A value claim reclassified as a
finding is somebody asserting that their preferences are facts, which is
precisely the move this tool exists to refuse.

WHAT THE THREE BUCKETS MEAN TO A READER
=======================================
FINDINGS      Evidence settled it, at T0-T2, and the citation is in the record.
              Argue with these by producing better sources.

VALUES        Evidence can inform these and cannot settle them. Argue with
              these by disagreeing, which is legitimate and expected. The
              tool marks them as values rather than findings.

OPEN          Nobody knows yet, or nobody brought the evidence. Distinguished
              from VALUES on purpose: "we have not checked" and "this is not
              checkable" are different admissions, and collapsing them lets a
              speaker hide the first inside the second.
"""

from __future__ import annotations

from dataclasses import dataclass

from abca.schema.core import Claim
from abca.schema.enums import Verdict


@dataclass(frozen=True, slots=True)
class Frame:
    """An issue partitioned into what can, cannot, and has not been settled."""

    findings: tuple[Claim, ...]
    values: tuple[Claim, ...]
    open_questions: tuple[Claim, ...]
    out_of_scope: tuple[Claim, ...]

    @property
    def checkable_share(self) -> float:
        """Fraction of in-scope claims that evidence actually settled.

        The number a publisher should be uncomfortable seeing get smaller. A
        body of statements trending toward all-values is trending toward
        being unfalsifiable, which is comfortable and worthless.
        """
        considered = len(self.findings) + len(self.values) + len(self.open_questions)
        return round(len(self.findings) / considered, 4) if considered else 0.0

    def summary(self) -> dict[str, int | float]:
        return {
            "findings": len(self.findings),
            "values": len(self.values),
            "open_questions": len(self.open_questions),
            "out_of_scope": len(self.out_of_scope),
            "checkable_share": self.checkable_share,
        }


#: Verdicts that mean evidence reached a conclusion about the claim.
SETTLED: frozenset[Verdict] = frozenset({
    Verdict.SUPPORTED,
    Verdict.CONTRADICTED,
    Verdict.MIXED,
    Verdict.MISLEADING_CONTEXT,
})

#: Verdicts that mean nobody has established anything yet. REPORTED_UNVERIFIED
#: lives here rather than in findings: a thing being reported is a fact about
#: the reporting, not about the world, and treating it as settled is how a
#: sourced-looking claim gets built out of a single news cycle.
UNSETTLED: frozenset[Verdict] = frozenset({
    Verdict.UNSUPPORTED,
    Verdict.REPORTED_UNVERIFIED,
})


def frame_issue(claims: list[Claim]) -> Frame:
    """Partition adjudicated claims into findings, values, open questions.

    Total and deterministic: every claim lands in exactly one bucket, and the
    same input always produces the same partition.
    """
    findings: list[Claim] = []
    values: list[Claim] = []
    open_questions: list[Claim] = []
    out_of_scope: list[Claim] = []

    for claim in claims:
        if claim.verdict is Verdict.OUT_OF_SCOPE:
            out_of_scope.append(claim)
        elif not claim.claim_type.is_verdict_eligible:
            # The claim's CLASS decides this, not its verdict. A normative
            # claim is a value even if a model somehow attached SUPPORTED to
            # it -- the contract gate should have caught that upstream, and
            # this ordering means it stays harmless if one ever slips through.
            values.append(claim)
        elif claim.verdict in SETTLED:
            findings.append(claim)
        elif claim.verdict in UNSETTLED:
            open_questions.append(claim)
        else:
            # UNVERIFIABLE on a verdict-eligible claim: the type says evidence
            # could settle it, the adjudicator says it did not. That is an
            # open question, not a value.
            open_questions.append(claim)

    return Frame(
        findings=tuple(findings),
        values=tuple(values),
        open_questions=tuple(open_questions),
        out_of_scope=tuple(out_of_scope),
    )


__all__ = ["SETTLED", "UNSETTLED", "Frame", "frame_issue"]
