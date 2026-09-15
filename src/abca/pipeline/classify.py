"""Classify stage: assign each claim one of the six types.

THE MOST CONSEQUENTIAL ROUTING DECISION IN THE PIPELINE
=======================================================
The type decides whether a claim is ever adjudicated as supported or
contradicted at all. ``LEGAL``, ``EMPIRICAL`` and ``ATTRIBUTIVE`` proceed to
retrieval and adjudication. ``NORMATIVE``, ``PREDICTIVE`` and ``DEFINITIONAL``
resolve to ``UNVERIFIABLE`` and have their premises extracted instead
(contract s3).

Get this wrong in one direction and the tool renders a true/false verdict on a
value judgment -- which is a partisan instrument with a citation format. Get it
wrong in the other and a checkable factual claim escapes checking.

So this stage records its reasoning for every claim, not just its answer. The
reasoning names *what would settle the claim*, which is the question the
taxonomy actually turns on, and it is the audit trail a reader needs when they
disagree with a routing decision.

FAILURE BEHAVIOR
================
Unlike the gate, this stage does NOT fail open into a verdict-eligible type. A
claim whose classification failed is marked ``NORMATIVE``, which routes it to
``UNVERIFIABLE`` -- the analysis says "evidence cannot settle this" rather than
adjudicating something it never understood. Refusing to answer is a safe
failure; guessing that an unclassified claim is checkable is not.
"""

from __future__ import annotations

from abca.pipeline.base import StageOutcome, batched
from abca.pipeline.models import ClassifyOutput, DraftClaim
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.schema.enums import ClaimType, StageName

#: Type assigned when classification could not be obtained. NORMATIVE routes to
#: UNVERIFIABLE, which is the honest outcome for a claim the pipeline never
#: managed to understand. See the module docstring.
FALLBACK_TYPE = ClaimType.NORMATIVE

#: Below this, the classification is reported as low-confidence in the stage
#: notes so a reader can see which routing decisions were shaky.
LOW_CONFIDENCE = 0.6


def build_classify_prompt(prompt_text: str, batch: list[DraftClaim]) -> str:
    """Compose the classification call for one batch."""
    # Claims are keyed by ID, not by position: the model echoes the id back, so
    # a reordered response still maps correctly. Sentences use positional
    # indices instead because they have no natural id.
    listing = "\n".join(f"- {claim.id}: {claim.text}" for claim in batch)
    return (
        f"{prompt_text}\n\n"
        "---\n\n"
        "## Claims\n\n"
        f"{listing}\n\n"
        "Return one classification per claim, echoing the claim id exactly."
    )


def run_classify(
    provider: Provider,
    claims: list[DraftClaim],
    *,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
) -> StageOutcome[list[DraftClaim]]:
    """Assign a :class:`ClaimType` to every claim.

    Returns new :class:`DraftClaim` objects -- the model is frozen, so
    classification produces copies rather than mutating in place. That keeps
    the pre-classification state available for debugging and makes the stage
    a pure function of its inputs.
    """
    outcome: StageOutcome[list[DraftClaim]] = StageOutcome(value=[])
    if not claims:
        return outcome

    prompt_text = load_prompt(StageName.CLASSIFY).text
    results: dict[str, tuple[ClaimType, float, str]] = {}

    for batch in batched(claims, size_of=lambda c: len(c.text) + len(c.id) + 8):
        request = GenerationRequest(
            prompt=build_classify_prompt(prompt_text, batch),
            seed=seed,
            temperature=temperature,
            context_length=context_length,
        )
        try:
            result = generate_structured(
                provider, request, ClassifyOutput, max_attempts=max_attempts
            )
        except (StructuredOutputError, ProviderError) as exc:
            outcome.note(
                f"classification failed for {len(batch)} claim(s) "
                f"({type(exc).__name__}); they were marked {FALLBACK_TYPE.value} and "
                "will resolve to UNVERIFIABLE rather than being adjudicated on a guess"
            )
            continue

        outcome.absorb(result)
        valid = {claim.id for claim in batch}
        for classification in result.value.classifications:
            if classification.claim_id not in valid:
                outcome.note(
                    f"classification returned an unknown claim id "
                    f"{classification.claim_id!r}; discarded"
                )
                continue
            results[classification.claim_id] = (
                classification.claim_type,
                classification.confidence,
                classification.reasoning,
            )

    classified: list[DraftClaim] = []
    unclassified = 0
    low_confidence = 0

    for claim in claims:
        entry = results.get(claim.id)
        if entry is None:
            unclassified += 1
            classified.append(
                claim.model_copy(
                    update={
                        "claim_type": FALLBACK_TYPE,
                        "classification_confidence": 0.0,
                        "classification_reasoning": (
                            "classification unavailable; defaulted to a type that "
                            "evidence cannot settle, so this claim is not adjudicated"
                        ),
                    }
                )
            )
            continue

        claim_type, confidence, reasoning = entry
        if confidence < LOW_CONFIDENCE:
            low_confidence += 1
        classified.append(
            claim.model_copy(
                update={
                    "claim_type": claim_type,
                    "classification_confidence": confidence,
                    "classification_reasoning": reasoning,
                }
            )
        )

    if unclassified:
        outcome.note(
            f"{unclassified} claim(s) could not be classified and were marked "
            f"{FALLBACK_TYPE.value}"
        )
    if low_confidence:
        outcome.note(
            f"{low_confidence} claim(s) classified below {LOW_CONFIDENCE:.0%} "
            "confidence; the type decides whether a claim is adjudicated at all, "
            "so these routing decisions are worth a human's eye"
        )

    counts: dict[str, int] = {}
    for claim in classified:
        key = claim.claim_type.value if claim.claim_type else "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    outcome.note("types: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    outcome.value = classified
    outcome.dedupe_notes()
    return outcome


__all__ = ["FALLBACK_TYPE", "LOW_CONFIDENCE", "build_classify_prompt", "run_classify"]
