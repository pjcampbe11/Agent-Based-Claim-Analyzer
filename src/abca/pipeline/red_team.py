"""Red-team stage: attack the analysis before it ships (contract s7).

WHY THIS IS MANDATORY RATHER THAN A FLAG
========================================
Every other stage in this pipeline is trying to produce an answer. This one is
trying to break it. Without it, the tool has no step whose job is to be wrong-
finding, and a system that only ever confirms its own reasoning is the exact
posture the project exists to oppose.

Findings ship INSIDE the claim, not in a log. A verdict published without the
objections raised against it is a verdict published with the honest part
removed.

THE RATCHET: THIS STAGE CAN ONLY WEAKEN
=======================================
The red team may lower a verdict's assertiveness and lower its confidence. It
can never do the reverse -- no ``UNSUPPORTED`` becomes ``SUPPORTED`` here, and
no confidence goes up.

That constraint is the whole reason an adversarial pass can be trusted with the
power to change verdicts. Without it, "red team" would just be a second chance
to assert something, with adversarial framing as cover. With it, the worst a
compromised or overconfident red team can do is make the tool say less than it
knows -- which is a failure, but a safe one.

The ratchet is enforced in code (:data:`VERDICT_STRENGTH`), not requested in
the prompt. A model cannot be talked out of a rule it never sees.

COUNTER-EVIDENCE IS VERIFIED LIKE ANY OTHER EVIDENCE
====================================================
The red team names a source id and quotes it; the quote is checked verbatim
against the retrieved document, and the tier comes from the connector. This is
the same path :mod:`abca.pipeline.adjudicate` uses, deliberately.

An adversarial pass that could assert freely would be a fabrication channel:
any verdict could be undercut with invented text. Purely analytical objections
-- "the statute is silent, so this rests on inference" -- carry no citation and
need none. The line is whether the objection claims a SOURCE says something.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.pipeline.adjudicate import AdjudicationRecord, render_sources, verify_citations
from abca.pipeline.base import StageOutcome, batched
from abca.pipeline.models import DraftClaim, RedTeamAssessment, RedTeamOutput
from abca.pipeline.retrieve import RetrievalResult
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.schema.core import RedTeamFinding
from abca.schema.enums import RedTeamSeverity, StageName, Verdict
from abca.sources.base import RetrievedDocument

#: Claims per red-team call. Smaller than adjudication's batch: each claim
#: carries its verdict, reasoning, citations AND its sources, so the prompt
#: grows fast.
RED_TEAM_BATCH_SIZE = 3

#: How assertive each verdict is. The red team may only move a verdict to a
#: strictly LOWER number, never higher.
#:
#: UNVERIFIABLE and OUT_OF_SCOPE are absent on purpose: those verdicts follow
#: from the claim's TYPE and from the gate, not from evidence, so an
#: evidence-based objection has no purchase on them and they are never sent to
#: this stage at all.
VERDICT_STRENGTH: dict[Verdict, int] = {
    Verdict.SUPPORTED: 3,
    Verdict.CONTRADICTED: 3,
    Verdict.MIXED: 2,
    Verdict.MISLEADING_CONTEXT: 2,
    Verdict.REPORTED_UNVERIFIED: 1,
    Verdict.UNSUPPORTED: 0,
}

#: Where a MATERIAL finding lands when the red team recommends nothing usable.
#: UNSUPPORTED, not MIXED: if the objection is that the verdict is not
#: supportable as stated, the honest fallback is "the sources do not settle
#: this", not a weaker assertion that is still an assertion.
DEFAULT_MATERIAL_VERDICT = Verdict.UNSUPPORTED


@dataclass(slots=True)
class RedTeamResult:
    """Findings keyed by claim id, plus the adjusted verdicts."""

    findings: dict[str, RedTeamFinding] = field(default_factory=dict)
    #: claim id -> (new verdict, new confidence) for claims the ratchet moved.
    downgrades: dict[str, tuple[Verdict, float]] = field(default_factory=dict)


def permitted_downgrade(
    current: Verdict,
    recommended: Verdict | None,
) -> Verdict | None:
    """Return the verdict to move to, or ``None`` if no move is permitted.

    Implements the ratchet. A recommendation that is absent, unrecognised,
    equal to the current verdict, or STRONGER than it is refused -- and refused
    silently rather than clamped, because a red team recommending an upgrade is
    doing something the contract forbids and the caller records it.
    """
    current_strength = VERDICT_STRENGTH.get(current)
    if current_strength is None:
        return None  # not an evidence-based verdict; nothing to move

    target = DEFAULT_MATERIAL_VERDICT if recommended is None else recommended

    target_strength = VERDICT_STRENGTH.get(target)
    if target_strength is None:
        return None
    if target_strength >= current_strength:
        return None
    return target


def build_red_team_prompt(
    prompt_text: str,
    claims: list[DraftClaim],
    adjudications: dict[str, AdjudicationRecord],
    documents: list[RetrievedDocument],
) -> str:
    """Compose the red-team call for one batch.

    The analysis is presented in full -- verdict, confidence, reasoning and the
    exact quotes relied on -- alongside the SAME sources the adjudicator saw.
    That last part matters: the strongest counter-evidence to "the statute
    requires X" is usually another passage in the same statute, and a red team
    without the source text can only object in the abstract.
    """
    blocks: list[str] = []
    for claim in claims:
        record = adjudications[claim.id]
        citation_lines = "\n".join(
            f'    - [{citation.tier.value}] {citation.title}: "'
            + " ".join(citation.quote.split())
            + '"'
            for citation in record.citations
        ) or "    (none)"
        blocks.append(
            f"### {claim.id}\n"
            f"Claim: {claim.text}\n"
            f"Verdict: {record.verdict.value} (confidence {record.confidence:.2f})\n"
            f"Reasoning: {record.reasoning}\n"
            f"Citations relied on:\n{citation_lines}"
        )

    sources = render_sources(documents) if documents else (
        "_No sources were retrieved for these claims._"
    )
    return (
        f"{prompt_text}\n\n"
        "---\n\n"
        "## Sources (the same ones the analyst had)\n\n"
        f"{sources}\n\n"
        "## Analysis to attack\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn one assessment per claim, echoing the claim id exactly."
    )


def run_red_team(
    provider: Provider,
    claims: list[DraftClaim],
    adjudications: dict[str, AdjudicationRecord],
    retrieval: RetrievalResult,
    *,
    independent: bool = True,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
    batch_size: int = RED_TEAM_BATCH_SIZE,
) -> StageOutcome[RedTeamResult]:
    """Attack every adjudicated verdict.

    ``independent`` records whether this ran on a different model from the one
    that produced the verdicts. It is not enforced -- a same-model red team is
    weaker but still finds things -- but it IS recorded on every finding, so a
    reader can weigh it.
    """
    outcome: StageOutcome[RedTeamResult] = StageOutcome(value=RedTeamResult())
    result = outcome.value

    # Only claims that actually received an evidence-based verdict. UNVERIFIABLE
    # and OUT_OF_SCOPE follow from the claim's type and the gate, so there is
    # nothing here for an evidence-based objection to attack.
    targets = [
        claim
        for claim in claims
        if claim.id in adjudications
        and adjudications[claim.id].verdict in VERDICT_STRENGTH
    ]
    if not targets:
        outcome.note("no adjudicated verdicts to attack")
        return outcome

    if not independent:
        outcome.note(
            "red team ran on the SAME model that produced the verdicts. A model "
            "reviewing its own reasoning is the one least able to see where it "
            "reached; configure a separate [models.redteam] for an independent "
            "pass. Recorded on every finding."
        )

    prompt_text = load_prompt(StageName.RED_TEAM).text
    index = retrieval.index()
    rejected_total = 0
    refused_upgrades = 0

    for batch in batched(targets, size_of=lambda c: len(c.text) + 400, max_items=batch_size):
        documents: list[RetrievedDocument] = []
        seen: set[str] = set()
        for claim in batch:
            for document in retrieval.documents_for(claim.id):
                if document.id not in seen:
                    seen.add(document.id)
                    documents.append(document)

        request = GenerationRequest(
            prompt=build_red_team_prompt(prompt_text, batch, adjudications, documents),
            seed=seed,
            temperature=temperature,
            context_length=context_length,
        )
        try:
            generated = generate_structured(
                provider, request, RedTeamOutput, max_attempts=max_attempts
            )
        except (StructuredOutputError, ProviderError) as exc:
            # A failed red team must be VISIBLE, because its absence is exactly
            # what the contract forbids. The verdicts stand unattacked and the
            # record says so, rather than the claim quietly shipping as though
            # it had been reviewed.
            outcome.note(
                f"red team failed for {len(batch)} claim(s) ({type(exc).__name__}); "
                "their verdicts were NOT adversarially reviewed and carry no "
                "red-team finding"
            )
            continue

        outcome.absorb(generated)
        valid = {claim.id for claim in batch}

        for assessment in generated.value.assessments:
            if assessment.claim_id not in valid:
                outcome.note(
                    f"red team returned unknown claim id {assessment.claim_id!r}; discarded"
                )
                continue

            finding, downgrade, rejected, refused = _apply(
                assessment, adjudications[assessment.claim_id], index, independent=independent
            )
            rejected_total += rejected
            refused_upgrades += refused
            result.findings[assessment.claim_id] = finding
            if downgrade is not None:
                result.downgrades[assessment.claim_id] = downgrade

    unattacked = [claim.id for claim in targets if claim.id not in result.findings]
    if unattacked:
        outcome.note(
            f"{len(unattacked)} verdict(s) received no red-team finding and ship "
            "unattacked"
        )
    if rejected_total:
        outcome.note(
            f"{rejected_total} counter-citation(s) failed verbatim verification and "
            "were discarded. Counter-evidence is held to the same standard as the "
            "evidence it attacks."
        )
    if refused_upgrades:
        outcome.note(
            f"{refused_upgrades} red-team recommendation(s) would have made a verdict "
            "MORE assertive and were refused. This stage can only weaken (contract s7)."
        )

    severities: dict[str, int] = {}
    for finding in result.findings.values():
        severities[finding.severity.value] = severities.get(finding.severity.value, 0) + 1
    outcome.note(
        f"red team reviewed {len(result.findings)}/{len(targets)} verdict(s): "
        + ", ".join(f"{k}={v}" for k, v in sorted(severities.items()))
        + (f"; {len(result.downgrades)} downgraded" if result.downgrades else "")
    )

    outcome.dedupe_notes()
    return outcome


def _apply(
    assessment: RedTeamAssessment,
    adjudication: AdjudicationRecord,
    index: dict[str, RetrievedDocument],
    *,
    independent: bool,
) -> tuple[RedTeamFinding, tuple[Verdict, float] | None, int, int]:
    """Verify one assessment and decide whether it moves the verdict.

    Returns ``(finding, downgrade_or_None, rejected_count, refused_upgrades)``.
    """
    # Counter-evidence goes through the SAME verification as the adjudicator's
    # evidence. Reusing the function rather than reimplementing it is
    # deliberate: two verification paths would eventually diverge, and the
    # weaker one would become the way in.
    from abca.pipeline.models import Adjudication

    probe = Adjudication(
        claim_id=assessment.claim_id,
        verdict=adjudication.verdict,
        confidence=adjudication.confidence,
        reasoning="counter-evidence verification",
        citations=assessment.counter_citations,
    )
    counter_citations, rejected = verify_citations(probe, index)

    severity = assessment.severity
    counter_evidence = assessment.counter_evidence
    if rejected:
        counter_evidence += (
            f" [{len(rejected)} counter-citation(s) offered here failed verbatim "
            "verification and were discarded.]"
        )
        # An objection whose textual support evaporated cannot be MATERIAL on
        # that support alone. It survives as MATERIAL only if it also names a
        # specific overreach -- an analytical objection stands on its own.
        if severity is RedTeamSeverity.MATERIAL and not counter_citations \
                and not assessment.overreach_flags:
            severity = RedTeamSeverity.MINOR
            counter_evidence += (
                " Severity reduced to MINOR: the objection rested on counter-evidence "
                "that could not be verified."
            )

    downgrade: tuple[Verdict, float] | None = None
    downgraded_from: Verdict | None = None
    refused_upgrades = 0

    if severity.triggers_downgrade:
        target = permitted_downgrade(adjudication.verdict, assessment.recommended_verdict)
        if target is None:
            if assessment.recommended_verdict is not None:
                refused_upgrades = 1
        else:
            downgraded_from = adjudication.verdict
            # Confidence follows the verdict down. Keeping the original number
            # on a weakened verdict would be the loudest possible mixed signal.
            new_confidence = min(adjudication.confidence, 0.5) if target is not Verdict.UNSUPPORTED else 0.0
            downgrade = (target, new_confidence)

    finding = RedTeamFinding(
        counter_evidence=counter_evidence,
        steelman=assessment.steelman,
        overreach_flags=list(assessment.overreach_flags),
        counter_citations=counter_citations,
        severity=severity,
        independent=independent,
        verdict_downgraded_from=downgraded_from,
    )
    return finding, downgrade, len(rejected), refused_upgrades


__all__ = [
    "DEFAULT_MATERIAL_VERDICT",
    "RED_TEAM_BATCH_SIZE",
    "VERDICT_STRENGTH",
    "RedTeamResult",
    "build_red_team_prompt",
    "permitted_downgrade",
    "run_red_team",
]
