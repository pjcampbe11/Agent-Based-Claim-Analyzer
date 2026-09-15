"""Adjudication stage: what do the sources actually establish?

THE CITATION PIPELINE, AND WHY IT IS SHAPED THIS WAY
====================================================
A model never produces a citation. It produces a POINTER and a QUOTE, and this
module turns those into a citation only if both check out:

.. code-block:: text

    model returns  {source_id: "s-001", quote: "..."}
          |
          v
    1. does s-001 exist among the sources we retrieved?      no -> discard
          |
          v
    2. does the quote appear verbatim in that document?      no -> discard
          |
          v
    3. build Citation with the CONNECTOR's tier, the real
       url, the real content hash, the real retrieval time
          |
          v
    4. schema validators run: SUPPORTED with no surviving
       T0-T2 citation fails, and the repair loop re-asks

Step 2 is the single most valuable anti-hallucination measure available to a
system like this, because it is the one check that a fluent model cannot talk
its way past. A model can produce a confident verdict, plausible reasoning, and
a plausible-sounding quote; it cannot make that quote appear in a statute it
did not appear in.

Step 4 is what makes step 2 bite. If discarding fabricated quotes leaves a
``SUPPORTED`` verdict with nothing behind it, the pydantic validator rejects
the whole claim, and :func:`generate_structured` re-asks with the error
attached. The model does not get to keep the verdict and lose the evidence.

WHITESPACE TOLERANCE, AND ITS LIMIT
===================================
Verification collapses whitespace on both sides before comparing. Statute text
is full of hard line wraps and indentation that no model reproduces exactly
when quoting, and rejecting a correct quote over a line break would push the
adjudicator toward citing nothing -- the opposite of what the evidence gate is
for.

Nothing else is relaxed. Different words fail. Different numbers fail. A
dropped "not" fails. And the STORED quote is the source's own wording, recovered
from the document, not the model's whitespace-mangled version -- so a later
reader comparing the citation against the statute sees the statute's text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.pipeline.base import StageOutcome, batched
from abca.pipeline.models import AdjudicateOutput, Adjudication, DraftClaim
from abca.pipeline.retrieve import RetrievalResult
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.schema.core import Citation
from abca.schema.enums import StageName, Verdict
from abca.sources.base import RetrievedDocument

#: Characters of source text included per document in the prompt. Statutes run
#: long; a section of 12,000 characters would crowd out the claims themselves.
#: Truncation is REPORTED, so a verdict reached on a partial source is visible.
MAX_SOURCE_CHARS = 9_000

#: Claims per adjudication call. Small: each claim carries its own sources, so
#: a batch of ten claims can be a very large prompt, and a single bad entry
#: forces the whole batch to be retried.
ADJUDICATE_BATCH_SIZE = 4


@dataclass(slots=True)
class AdjudicationRecord:
    """One claim's adjudication after citation verification."""

    claim_id: str
    verdict: Verdict
    confidence: float
    reasoning: str
    citations: list[Citation] = field(default_factory=list)
    #: Citations the model returned that failed verification, with the reason.
    #: Kept and reported: a model that regularly fabricates quotes is a finding
    #: about the model, and hiding it would lose that signal.
    rejected: list[str] = field(default_factory=list)


def render_sources(documents: list[RetrievedDocument]) -> str:
    """Render retrieved sources for the prompt.

    The id is the model's ONLY handle on a source. Tier is shown for context
    but is not something the model can change -- it is read from the connector
    when the citation is built, whatever the model says here.
    """
    blocks: list[str] = []
    for document in documents:
        text = document.text
        truncated = len(text) > MAX_SOURCE_CHARS
        if truncated:
            text = text[:MAX_SOURCE_CHARS]
        blocks.append(
            f"### [{document.id}] {document.title}  (tier {document.tier.value})\n"
            f"Locator: {document.locator or document.url}\n"
            + ("_(truncated for length; quote only from the text shown)_\n" if truncated else "")
            + f"\n```\n{text}\n```"
        )
    return "\n\n".join(blocks)


def build_adjudicate_prompt(
    prompt_text: str,
    claims: list[DraftClaim],
    documents: list[RetrievedDocument],
    unresolved: dict[str, list[str]],
) -> str:
    """Compose the adjudication call for one batch."""
    claim_lines = []
    for claim in claims:
        line = f"- {claim.id} [{claim.claim_type.value if claim.claim_type else '?'}]: {claim.text}"
        missing = unresolved.get(claim.id)
        if missing:
            # Surfaced explicitly. A citation that does not resolve is evidence
            # about the claim, and the model should be able to say so.
            line += (
                f"\n  (NOTE: this claim cites {', '.join(missing)}, which could not be "
                "retrieved — the section may not exist. That is itself a finding.)"
            )
        claim_lines.append(line)

    sources = render_sources(documents) if documents else (
        "_No sources were retrieved for these claims._"
    )

    return (
        f"{prompt_text}\n\n"
        "---\n\n"
        "## Sources\n\n"
        f"{sources}\n\n"
        "## Claims to adjudicate\n\n"
        + "\n".join(claim_lines)
        + "\n\nReturn one adjudication per claim, echoing the claim id exactly. "
        "Quote only from the sources above, copied verbatim."
    )


def verify_citations(
    adjudication: Adjudication,
    index: dict[str, RetrievedDocument],
) -> tuple[list[Citation], list[str]]:
    """Turn model citation refs into real citations, discarding what fails.

    Returns ``(verified, rejection_reasons)``. See the module docstring for the
    four steps and why each exists.
    """
    verified: list[Citation] = []
    rejected: list[str] = []

    for ref in adjudication.citations:
        document = index.get(ref.source_id)
        if document is None:
            rejected.append(
                f"cited unknown source {ref.source_id!r} (not among the retrieved "
                "documents); discarded"
            )
            continue

        # Recover the SOURCE's wording for this quote. Returns None when the
        # quote is not present, which is the fabrication check.
        source_wording = document.locate(ref.quote)
        if source_wording is None:
            preview = " ".join(ref.quote.split())[:100]
            rejected.append(
                f"quote not found in {ref.source_id} ({document.title}): {preview!r}; "
                "discarded as unverified"
            )
            continue

        verified.append(document.to_citation(source_wording, locator=ref.locator))

    return verified, rejected


def run_adjudicate(
    provider: Provider,
    claims: list[DraftClaim],
    retrieval: RetrievalResult,
    *,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
    batch_size: int = ADJUDICATE_BATCH_SIZE,
) -> StageOutcome[dict[str, AdjudicationRecord]]:
    """Adjudicate every verdict-eligible claim against its retrieved sources.

    Claims whose type cannot be settled by evidence are not sent at all -- the
    contract forbids adjudicating them, so spending a model call would be
    buying an answer that must then be discarded.
    """
    outcome: StageOutcome[dict[str, AdjudicationRecord]] = StageOutcome(value={})

    # Only claims with something to adjudicate AGAINST. A claim with no
    # retrieved source and no unresolved citation has nothing for the model to
    # read, and sending it anyway does two bad things: it spends a model call
    # for nothing, and it invites the model to invent a citation to justify a
    # verdict -- which verification then rejects, producing a confusing
    # "3 citations failed verification" note about a claim nobody could check.
    #
    # An UNRESOLVED citation still qualifies: "you cited a section that does
    # not exist" is a finding the adjudicator should be able to state.
    eligible = [
        claim
        for claim in claims
        if not claim.gated_out
        and claim.claim_type is not None
        and claim.claim_type.is_verdict_eligible
        and (retrieval.by_claim.get(claim.id) or retrieval.unresolved.get(claim.id))
    ]
    unserved = [
        claim
        for claim in claims
        if not claim.gated_out
        and claim.claim_type is not None
        and claim.claim_type.is_verdict_eligible
        and not (retrieval.by_claim.get(claim.id) or retrieval.unresolved.get(claim.id))
    ]
    if unserved:
        outcome.note(
            f"{len(unserved)} verdict-eligible claim(s) had no source to check against "
            "and were not sent to the adjudicator; they are reported as unchecked "
            "rather than as unsupported-after-search"
        )
    if not eligible:
        outcome.note("no claim had a retrieved source to adjudicate against")
        return outcome

    prompt_text = load_prompt(StageName.ADJUDICATE).text
    index = retrieval.index()
    records: dict[str, AdjudicationRecord] = {}
    total_rejected = 0

    for batch in batched(eligible, size_of=lambda c: len(c.text) + 32, max_items=batch_size):
        documents: list[RetrievedDocument] = []
        seen_ids: set[str] = set()
        for claim in batch:
            for document in retrieval.documents_for(claim.id):
                if document.id not in seen_ids:
                    seen_ids.add(document.id)
                    documents.append(document)

        request = GenerationRequest(
            prompt=build_adjudicate_prompt(prompt_text, batch, documents, retrieval.unresolved),
            seed=seed,
            temperature=temperature,
            context_length=context_length,
        )
        try:
            result = generate_structured(
                provider, request, AdjudicateOutput, max_attempts=max_attempts
            )
        except (StructuredOutputError, ProviderError) as exc:
            # Fail toward NOT adjudicating. A claim whose adjudication failed
            # is left UNSUPPORTED by the caller, which reads as "not
            # established" -- the safe direction. Inventing a verdict here
            # would be the single worst failure this stage could have.
            outcome.note(
                f"adjudication failed for {len(batch)} claim(s) ({type(exc).__name__}); "
                "they remain UNSUPPORTED rather than being assigned a guessed verdict"
            )
            continue

        outcome.absorb(result)
        valid = {claim.id for claim in batch}

        for adjudication in result.value.adjudications:
            if adjudication.claim_id not in valid:
                outcome.note(
                    f"adjudication returned unknown claim id {adjudication.claim_id!r}; discarded"
                )
                continue

            citations, rejected = verify_citations(adjudication, index)
            total_rejected += len(rejected)

            verdict = adjudication.verdict
            confidence = adjudication.confidence
            reasoning = adjudication.reasoning

            # If verification removed the support a verdict requires, the
            # verdict cannot stand. Downgraded here rather than left for the
            # schema to reject, so the DOWNGRADE is recorded in the reasoning
            # a reader sees -- a silently rejected claim would just vanish.
            if verdict.requires_supporting_evidence and not any(
                citation.supports_verdict for citation in citations
            ):
                reasoning = (
                    f"[Verdict downgraded from {verdict.value} to UNSUPPORTED: every "
                    f"citation offered failed verification against the retrieved "
                    f"sources.] {reasoning}"
                )
                verdict = Verdict.UNSUPPORTED
                confidence = 0.0
                citations = []

            records[adjudication.claim_id] = AdjudicationRecord(
                claim_id=adjudication.claim_id,
                verdict=verdict,
                confidence=confidence,
                reasoning=reasoning,
                citations=citations,
                rejected=rejected,
            )

    missing = [claim.id for claim in eligible if claim.id not in records]
    if missing:
        outcome.note(
            f"{len(missing)} eligible claim(s) received no adjudication; they remain "
            "UNSUPPORTED"
        )
    if total_rejected:
        outcome.note(
            f"{total_rejected} citation(s) failed verbatim verification against the "
            "retrieved sources and were discarded. A model that does this often is a "
            "finding about the model, not a detail."
        )

    counts: dict[str, int] = {}
    for record in records.values():
        counts[record.verdict.value] = counts.get(record.verdict.value, 0) + 1
    outcome.note(
        f"adjudicated {len(records)}/{len(eligible)} eligible claim(s): "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )

    outcome.value = records
    outcome.dedupe_notes()
    return outcome


__all__ = [
    "ADJUDICATE_BATCH_SIZE",
    "MAX_SOURCE_CHARS",
    "AdjudicationRecord",
    "build_adjudicate_prompt",
    "render_sources",
    "run_adjudicate",
    "verify_citations",
]
