"""Gate stage: does this sentence concern public affairs?

WHERE MOST OF A LARGE THREAD DIES, CHEAPLY
==========================================
On a 2,000-comment thread the overwhelming majority of sentences are jokes,
greetings, moderation notices and personal chatter. Running the adjudicator on
those would dominate the cost of the run and produce nothing.

So the gate runs FIRST and on the SMALL model. It is the cheapest possible
question -- routing, not analysis -- and answering it early is what makes the
``fast`` profile's sub-ten-second target reachable at all.

THE ASYMMETRY IS DELIBERATE
===========================
A false negative here is invisible: the claim silently never gets examined, and
nothing in the output says so. A false positive costs one cheap classification
downstream and then resolves to ``OUT_OF_SCOPE`` harmlessly.

Those costs are not symmetric, so the prompt tells the model to pass through
when uncertain, and this module treats any model failure as pass-through too
(see :func:`run_gate`). A gate that fails closed would quietly shrink the
analysis; a gate that fails open costs money and stays honest.
"""

from __future__ import annotations

from abca.pipeline.base import StageOutcome, batched, render_numbered
from abca.pipeline.models import GateDecision, GateOutput
from abca.pipeline.sentences import Sentence
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import (
    StructuredOutputError,
    generate_structured,
)
from abca.schema.enums import StageName

#: Sentences whose gate decision the model did not return. Kept, not dropped --
#: see the module docstring on the asymmetry.
_MISSING_DEFAULT_IN_SCOPE = True


def build_gate_prompt(prompt_text: str, batch: list[Sentence]) -> str:
    """Compose the gate call for one batch."""
    numbered = render_numbered((sentence.index, sentence.text) for sentence in batch)
    return (
        f"{prompt_text}\n\n"
        "---\n\n"
        "## Sentences\n\n"
        f"{numbered}\n\n"
        "Return one decision per sentence, using the indices shown."
    )


def run_gate(
    provider: Provider,
    sentences: list[Sentence],
    *,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
) -> StageOutcome[dict[int, GateDecision]]:
    """Decide which sentences concern public affairs.

    Returns a mapping from sentence index to its decision. Sentences the model
    omitted, and every sentence in a batch whose call failed outright, default
    to IN SCOPE with a note saying so -- failing open, for the reason in the
    module docstring.
    """
    outcome: StageOutcome[dict[int, GateDecision]] = StageOutcome(value={})
    if not sentences:
        return outcome

    prompt_text = load_prompt(StageName.GATE).text
    decisions: dict[int, GateDecision] = {}

    for batch in batched(sentences, size_of=lambda s: len(s.text) + 16):
        request = GenerationRequest(
            prompt=build_gate_prompt(prompt_text, batch),
            seed=seed,
            temperature=temperature,
            context_length=context_length,
        )
        try:
            result = generate_structured(
                provider, request, GateOutput, max_attempts=max_attempts
            )
        except (StructuredOutputError, ProviderError) as exc:
            # Fail open. The alternative -- dropping the batch -- would remove
            # claims from the analysis with no visible trace, which is exactly
            # the failure this project cannot have.
            outcome.note(
                f"gate failed for {len(batch)} sentence(s) ({type(exc).__name__}); "
                "they were passed through as in-scope rather than dropped, so "
                "nothing is silently excluded from the analysis"
            )
            continue

        outcome.absorb(result)
        valid_indices = {sentence.index for sentence in batch}
        for decision in result.value.decisions:
            if decision.sentence_index not in valid_indices:
                # A hallucinated index would silently attach a decision to the
                # wrong sentence, so it is discarded and reported.
                outcome.note(
                    f"gate returned a decision for sentence "
                    f"{decision.sentence_index}, which was not in the batch; discarded"
                )
                continue
            decisions[decision.sentence_index] = decision

    missing = [s.index for s in sentences if s.index not in decisions]
    if missing:
        outcome.note(
            f"gate returned no decision for {len(missing)} sentence(s); "
            "defaulted to in-scope"
        )
        for index in missing:
            decisions[index] = GateDecision(
                sentence_index=index,
                in_scope=_MISSING_DEFAULT_IN_SCOPE,
                reason="no decision returned; defaulted to in-scope",
            )

    in_scope = sum(1 for d in decisions.values() if d.in_scope)
    outcome.note(
        f"gate: {in_scope}/{len(sentences)} sentence(s) concern public affairs"
    )
    outcome.value = decisions
    outcome.dedupe_notes()
    return outcome


__all__ = ["build_gate_prompt", "run_gate"]
