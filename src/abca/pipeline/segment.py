"""Segment stage: sentences to atomic claims.

Sentence splitting is deterministic (:mod:`abca.pipeline.sentences`);
decomposing a sentence into separate assertions is not, and needs a model.

SPAN RESOLUTION
===============
Every claim carries a span into the normalized document so a reader can find
the text it came from. Model-reported character offsets are unreliable -- models
count tokens, not characters -- so offsets are never taken from the model.
Instead the span is RESOLVED by searching the source sentence, in three
descending tiers:

1. ``verbatim`` -- the model supplied an exact quote and it was located in the
   sentence. Tightest and best.
2. ``text``     -- no usable quote, but the claim text itself appears verbatim
   in the sentence.
3. ``sentence`` -- neither located; the span covers the whole source sentence.

Tier 3 is a correct answer, not a failure: a claim rewritten to stand alone
("it doubled" becoming "the deficit doubled") genuinely has no verbatim span,
and pointing at the sentence is the honest thing to do. What is NOT done is
fuzzy matching -- an approximate span points a reader at text the claim did not
come from, which is worse than admitting the span is broad.

The distribution across tiers is reported in the stage notes, because a model
that never produces a locatable quote is a finding.
"""

from __future__ import annotations

from abca.pipeline.base import StageOutcome, batched, render_numbered
from abca.pipeline.models import DraftClaim, SegmentOutput
from abca.pipeline.sentences import Sentence, locate
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.schema.enums import StageName

#: Claim id format. Zero-padded so ids sort in extraction order in any
#: alphabetical listing -- a report of 200 claims is unreadable if c-10 sorts
#: before c-2.
CLAIM_ID_FORMAT = "c-{:03d}"


def build_segment_prompt(prompt_text: str, batch: list[Sentence]) -> str:
    """Compose the segmentation call for one batch.

    Context sentences are NOT sent separately: the batch itself is the context,
    and the prompt tells the model to resolve references using surrounding
    sentences. Sending a sliding window would duplicate text across calls and
    let the same sentence be segmented twice with different results.
    """
    numbered = render_numbered((sentence.index, sentence.text) for sentence in batch)
    return (
        f"{prompt_text}\n\n"
        "---\n\n"
        "## Sentences\n\n"
        f"{numbered}\n\n"
        "Extract the atomic claims. Use the indices shown for `sentence_index`. "
        "A sentence containing no assertion produces no claims."
    )


def _swap_first_case(text: str) -> str | None:
    """Return ``text`` with only its first character's case flipped.

    Extracting a clause into a standalone claim forces a capital: "...and the
    deadline is in June" becomes the claim "The deadline is in June". That
    capital is a grammatical necessity, not a content change, and without this
    the claim fails to locate and falls back to the whole-sentence span for a
    purely typographic reason.

    This is ONE deterministic alternative spelling, not fuzzy matching. It
    changes exactly one character, at a known position, in a known way -- so it
    cannot point a reader at text the claim did not come from, which is the
    thing fuzzy matching would risk.
    """
    if not text:
        return None
    first = text[0]
    swapped = first.lower() if first.isupper() else first.upper()
    return None if swapped == first else swapped + text[1:]


def resolve_span(
    document: str,
    sentence: Sentence,
    claim_text: str,
    verbatim: str | None,
) -> tuple[int, int, str]:
    """Resolve a claim to a document span. See the module docstring for tiers."""
    window = (sentence.start, sentence.end)

    if verbatim:
        quote = verbatim.strip()
        found = locate(document, quote, within=window)
        if found is None and (alternative := _swap_first_case(quote)):
            found = locate(document, alternative, within=window)
        if found:
            return found[0], found[1], "verbatim"

    text = claim_text.strip()
    found = locate(document, text, within=window)
    if found is None and (alternative := _swap_first_case(text)):
        found = locate(document, alternative, within=window)
    if found:
        return found[0], found[1], "text"

    return sentence.start, sentence.end, "sentence"


def run_segment(
    provider: Provider,
    document: str,
    sentences: list[Sentence],
    *,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    max_claims: int = 200,
    context_length: int | None = None,
) -> StageOutcome[list[DraftClaim]]:
    """Extract atomic claims from ``sentences``.

    ``max_claims`` is a hard stop. When it is hit, extraction ends and a note
    records how much of the document was not processed -- a truncated analysis
    that says so is acceptable; one that looks complete is not.
    """
    outcome: StageOutcome[list[DraftClaim]] = StageOutcome(value=[])
    if not sentences:
        return outcome

    prompt_text = load_prompt(StageName.SEGMENT).text
    by_index = {sentence.index: sentence for sentence in sentences}
    claims: list[DraftClaim] = []
    span_tiers: dict[str, int] = {"verbatim": 0, "text": 0, "sentence": 0}
    truncated = False

    for batch in batched(sentences, size_of=lambda s: len(s.text) + 16):
        if len(claims) >= max_claims:
            truncated = True
            break

        request = GenerationRequest(
            prompt=build_segment_prompt(prompt_text, batch),
            seed=seed,
            temperature=temperature,
            context_length=context_length,
        )
        try:
            result = generate_structured(
                provider, request, SegmentOutput, max_attempts=max_attempts
            )
        except (StructuredOutputError, ProviderError) as exc:
            # Fall back to one claim per sentence rather than losing the batch.
            # A sentence treated as a single undecomposed assertion is a WORSE
            # analysis than a properly split one, but it is still an analysis;
            # dropping the batch would remove the author's words from the
            # record with no trace.
            outcome.note(
                f"segmentation failed for {len(batch)} sentence(s) "
                f"({type(exc).__name__}); fell back to one claim per sentence, "
                "so compound sentences in this batch were not decomposed"
            )
            for sentence in batch:
                if len(claims) >= max_claims:
                    truncated = True
                    break
                claims.append(
                    DraftClaim(
                        id=CLAIM_ID_FORMAT.format(len(claims) + 1),
                        text=sentence.text,
                        sentence_index=sentence.index,
                        span_start=sentence.start,
                        span_end=sentence.end,
                        span_source="sentence",
                    )
                )
                span_tiers["sentence"] += 1
            continue

        outcome.absorb(result)
        valid = {sentence.index for sentence in batch}

        for extracted in result.value.claims:
            if len(claims) >= max_claims:
                truncated = True
                break
            if extracted.sentence_index not in valid:
                outcome.note(
                    f"segmentation returned a claim for sentence "
                    f"{extracted.sentence_index}, which was not in the batch; discarded"
                )
                continue

            sentence = by_index[extracted.sentence_index]
            start, end, tier = resolve_span(
                document, sentence, extracted.text, extracted.verbatim_span
            )
            span_tiers[tier] += 1
            claims.append(
                DraftClaim(
                    id=CLAIM_ID_FORMAT.format(len(claims) + 1),
                    text=extracted.text.strip(),
                    sentence_index=extracted.sentence_index,
                    span_start=start,
                    span_end=end,
                    span_source=tier,
                    ambiguous_stance=extracted.ambiguous_stance,
                )
            )

    if truncated:
        outcome.note(
            f"claim limit of {max_claims} reached; the remainder of the document "
            "was not segmented. Raise --max-claims to analyze all of it."
        )

    ambiguous = sum(1 for claim in claims if claim.ambiguous_stance)
    if ambiguous:
        outcome.note(
            f"{ambiguous} claim(s) flagged ambiguous_stance: sarcasm, irony or "
            "reported speech makes the author's own commitment unclear. The "
            "literal assertion was extracted rather than a guess at intent."
        )

    located = span_tiers["verbatim"] + span_tiers["text"]
    if claims:
        outcome.note(
            f"spans: {located}/{len(claims)} resolved to an exact substring "
            f"(verbatim {span_tiers['verbatim']}, claim text {span_tiers['text']}); "
            f"{span_tiers['sentence']} fell back to the source sentence"
        )

    outcome.note(f"segmented {len(sentences)} sentence(s) into {len(claims)} claim(s)")
    outcome.value = claims
    outcome.dedupe_notes()
    return outcome


__all__ = ["CLAIM_ID_FORMAT", "build_segment_prompt", "resolve_span", "run_segment"]
