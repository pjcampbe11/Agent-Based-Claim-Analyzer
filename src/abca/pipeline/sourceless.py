"""Sourceless stage: reconstruct what a claim with no source is probably about.

WHERE THIS SITS AND WHY
=======================
Between ``cluster`` and ``retrieve`` (doc 18 s3). By the time a claim reaches
here it has been segmented, typed, gated and deduplicated -- and if it carries no
resolvable reference, ``retrieve`` has literally nothing to search for. This
stage's whole job is to produce that something.

FAILURE BEHAVIOR: FAIL CLOSED, ALWAYS
=====================================
Every other stage in this pipeline has a defensible fallback. This one's fallback
is to produce nothing.

If the model call fails, returns unparseable output, or is exhausted by repair
attempts, the stage emits an analysis carrying the denatured claim, an
``UNSUPPORTED`` disposition, ``referent_confidence: none``, and a note saying the
reconstruction did not run. It does NOT emit a partial reconstruction, a guessed
referent, or an empty retrieval plan dressed up as a completed one.

The reason is asymmetric cost. A missing reconstruction costs a reader nothing
they did not already lack -- the claim was unsourced when it arrived. A WRONG
reconstruction sends retrieval after the wrong document, and if it finds one, the
challenge lane can promote a claim onto an artifact that has nothing to do with
it. A confident verdict about the wrong document is far worse than no verdict.

WHAT THIS STAGE MAY NOT DO
==========================
It may not raise a claim's evidence tier, write a citation anywhere except a
cited structural conflict, or reach any disposition outside the four in
:data:`~abca.schema.sourceless.ALLOWED_DISPOSITIONS`. Those are enforced by the
schema rather than by this module, so a model that tries fails validation and is
retried -- see :mod:`abca.schema.sourceless`.
"""

from __future__ import annotations

from abca.canonical import digest_text
from abca.pipeline.base import StageOutcome
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.schema.enums import StageName, Verdict
from abca.schema.sourceless import (
    InstitutionalContext,
    ReferentConfidence,
    SourcelessAnalysis,
)
from abca.sourceless.distortions import TAXONOMY_VERSION, distortion_by_name, rank_candidates
from abca.sourceless.trigger import should_dissect

#: The module's own version, independent of the sotp contract. Recorded on every
#: analysis so a brief can be traced to the spec revision that produced it.
MODULE_VERSION = "sourceless/0.1.0"

#: Prompt family and version. Separate from the contract on purpose -- see
#: :func:`abca.prompts.prompt_path`.
PROMPT_FAMILY = "sourceless"
PROMPT_CONTRACT = "0.1.0"


def build_dissect_prompt(prompt_text: str, claim_text: str, *, platform: str = "",
                         date: str = "", parent_context: str = "") -> str:
    """Assemble the dissection prompt for one claim.

    Takes ONE claim, not a batch. Every other stage batches for throughput;
    this one deliberately does not, because reconstruction is a chain of
    reasoning about a single post's specific wording and batching invites the
    model to let one post's referent contaminate another's. The stage runs on
    cluster representatives only (doc 18 s3), so the volume is small enough
    that per-claim calls are affordable.

    Post metadata is supplied as CONTEXT, clearly labelled, because a reply's
    meaning depends on what it replies to. It is never presented as evidence.
    """
    parts = [prompt_text, "", "---", "", "## The post", "", claim_text.strip()]
    context = [
        (label, value)
        for label, value in (
            ("Platform", platform),
            ("Date", date),
            ("Replying to", parent_context),
        )
        if value
    ]
    if context:
        parts += ["", "## Context (not evidence)", ""]
        parts += [f"- {label}: {value}" for label, value in context]
    parts += [
        "",
        "## Your output",
        "",
        "Return the dissection object. Every factual line carries a register.",
        "Invent no identifier you are not certain of; uncertain items go to",
        "`unverified_items` and into the retrieval plan.",
    ]
    return "\n".join(parts)


def _unreconstructed(claim_text: str, reason: str) -> SourcelessAnalysis:
    """The fail-closed result. Honest about having done nothing.

    Carries the denatured claim (the raw text, since denaturing is itself a
    model step that did not run) so downstream stages and the published record
    still have the sentence under analysis, and nothing else.
    """
    return SourcelessAnalysis(
        module_version=MODULE_VERSION,
        denatured_claim=claim_text.strip() or "(empty)",
        denatured_claim_hash=digest_text(claim_text.strip()),
        disposition=Verdict.UNSUPPORTED,
        referent_confidence=ReferentConfidence.NONE,
        confidence=0.0,
        handoff_notes=(
            f"reconstruction did not run: {reason}. No referent was proposed and no "
            "retrieval plan was produced. This claim reaches retrieval with nothing "
            "to search for, which is the state it arrived in."
        ),
    )


def attach_context(
    analysis: SourcelessAnalysis,
    *,
    pack: object | None = None,
    limit: int = 6,
) -> SourcelessAnalysis:
    """Pass 2 (doc 19): select mechanism entries and pin the corpus hash.

    Runs AFTER dissection, keying off what pass 1 produced. Selection is a table
    lookup in :mod:`abca.context_pack.select`, not a model call, so the same
    pass-1 output always pulls the same entries -- required, because these
    entries carry citations into the run record and a selection that varied
    between runs would make two identical analyses cite different sources.

    The corpus hash is recorded whether or not any entry was selected. A brief
    is reproducible only against the corpus version that produced it, and
    "nothing was selected" is a claim about a specific corpus.

    Loading the pack is best-effort: a corpus that fails to load leaves the
    analysis untouched rather than aborting the run, because a missing
    explanation is a thinner brief and an aborted run is no brief at all. The
    build-time check (scripts/check_context_pack.py) is what makes a broken
    corpus impossible to ship in the first place.
    """
    from abca.context_pack.corpus import CorpusError, load_corpus
    from abca.context_pack.select import select_entries

    if pack is None:
        try:
            pack = load_corpus()
        except CorpusError:
            return analysis

    selected = select_entries(analysis, pack, limit=limit)  # type: ignore[arg-type]
    context = tuple(
        InstitutionalContext(
            entry_id=s.entry.id,
            entry_version=s.entry.entry_version,
            selected_by=s.selected_by,
            statement=s.entry.statement,
            plain_language=s.entry.plain_language,
            citation=s.entry.citation,
            staleness=(
                f"past review_due {s.entry.volatility.review_due}" if s.stale else None
            ),
            relevance=s.relevance(),
        )
        for s in selected
    )
    return analysis.model_copy(update={
        "institutional_context": context,
        "context_pack_version": pack.version,      # type: ignore[union-attr]
        "context_pack_hash": pack.content_hash,    # type: ignore[union-attr]
    })


def run_sourceless(
    provider: Provider,
    claim_text: str,
    *,
    claim_type_is_verdict_eligible: bool = True,
    survived_gate: bool = True,
    image_only: bool = False,
    platform: str = "",
    date: str = "",
    parent_context: str = "",
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
) -> StageOutcome[SourcelessAnalysis | None]:
    """Dissect one sourceless claim.

    Returns ``None`` as the value when the trigger says this claim should not be
    dissected at all -- a sourced claim, a non-verdict-eligible type, or
    image-only content. The reason is always noted, because "we did not
    reconstruct this" is itself something a reader may want to check.
    """
    outcome: StageOutcome[SourcelessAnalysis | None] = StageOutcome(value=None)

    decision = should_dissect(
        claim_text,
        claim_type_is_verdict_eligible=claim_type_is_verdict_eligible,
        survived_gate=survived_gate,
        image_only=image_only,
    )
    if not decision.dissect:
        outcome.note(f"sourceless dissection skipped: {decision.reason}")
        return outcome

    prompt = load_prompt(StageName.SOURCELESS, contract=PROMPT_CONTRACT, family=PROMPT_FAMILY)
    request = GenerationRequest(
        prompt=build_dissect_prompt(
            prompt.text, claim_text, platform=platform, date=date,
            parent_context=parent_context,
        ),
        seed=seed,
        temperature=temperature,
        context_length=context_length,
    )

    try:
        result = generate_structured(
            provider, request, SourcelessAnalysis, max_attempts=max_attempts
        )
    except (StructuredOutputError, ProviderError) as exc:
        outcome.value = _unreconstructed(claim_text, f"{type(exc).__name__}")
        outcome.note(
            f"sourceless dissection failed ({type(exc).__name__}); emitted an "
            "explicitly unreconstructed analysis rather than a partial one. A wrong "
            "referent is worse than no referent, because it sends retrieval after "
            "the wrong document."
        )
        return outcome

    outcome.absorb(result)
    analysis = _finalize(result.value, claim_text, prompt.content_hash, outcome)
    analysis = attach_context(analysis)
    if analysis.institutional_context:
        outcome.note(
            f"{len(analysis.institutional_context)} institutional context entr"
            f"{'y' if len(analysis.institutional_context) == 1 else 'ies'} selected "
            f"from {analysis.context_pack_version}"
        )
        stale = [c.entry_id for c in analysis.institutional_context if c.staleness]
        if stale:
            outcome.note(f"context entries past review and served with a warning: {stale}")
    outcome.value = analysis
    return outcome


def _finalize(
    analysis: SourcelessAnalysis,
    claim_text: str,
    prompt_hash: str,
    outcome: StageOutcome[SourcelessAnalysis | None],
) -> SourcelessAnalysis:
    """Stamp provenance and apply the smallest-distortion rule in code.

    Three things happen here that the model is not trusted to do:

    1. ``denatured_claim_hash`` is computed, not accepted. Doc 20 s4 refuses
       promotion when this hash moves, so a model-supplied value would let a
       reconstruction be substituted for the claim -- the exact failure the
       hash exists to prevent.
    2. ``prompt_hash`` and ``module_version`` are stamped from what actually
       ran, so a brief traces to the revision that produced it.
    3. Referent candidates are re-ordered cheapest-first by the distortion
       taxonomy. Doc 18 s2b states the smallest-distortion rule; applying it
       arithmetically means the ordering does not depend on the model
       remembering to sort.
    """
    update: dict[str, object] = {
        "module_version": MODULE_VERSION,
        "prompt_hash": prompt_hash,
        "denatured_claim_hash": digest_text(analysis.denatured_claim.strip()),
    }

    if analysis.referent_candidates:
        ranked = rank_candidates([c.distortion_applied for c in analysis.referent_candidates])
        ordered = []
        seen: set[int] = set()
        for distortion in ranked:
            for index, candidate in enumerate(analysis.referent_candidates):
                if index in seen:
                    continue
                if distortion_by_name(candidate.distortion_applied) is distortion:
                    ordered.append(candidate)
                    seen.add(index)
                    break
        # Anything the taxonomy could not place keeps its original position at
        # the end, rather than being dropped.
        ordered += [c for i, c in enumerate(analysis.referent_candidates) if i not in seen]
        update["referent_candidates"] = tuple(ordered)

        unrecognised = [
            c.distortion_applied for c in analysis.referent_candidates
            if distortion_by_name(c.distortion_applied).name == "other"
        ]
        if unrecognised:
            outcome.note(
                f"{len(unrecognised)} distortion(s) not in {TAXONOMY_VERSION} and "
                f"counted as unrecognised: {unrecognised}. Coverage gaps stay visible "
                "rather than being forced into the nearest label."
            )

    if analysis.structural_flag:
        outcome.note(
            f"{len(analysis.structural_flag)} uncited structural flag(s); each is a "
            "retrieval target, not a finding"
        )
    if not analysis.retrieval_plan.branch_outcomes:
        outcome.note(
            "no branch outcomes were pre-registered; doc 20's promotion gate (P2) "
            "will refuse to promote this claim on any artifact, because there is "
            "nothing it committed to in advance"
        )

    return analysis.model_copy(update=update)


__all__ = [
    "MODULE_VERSION",
    "PROMPT_CONTRACT",
    "PROMPT_FAMILY",
    "attach_context",
    "build_dissect_prompt",
    "run_sourceless",
]
