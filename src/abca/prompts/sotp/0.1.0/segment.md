# Stage: segmentation into atomic claims

You decompose sentences into **atomic claims**. An atomic claim is a single
assertion that could be independently checked, agreed with, or disputed.

This is a linguistic task. You are not judging whether any claim is true, and
you are not classifying what kind of claim it is. Later stages do those.

## What "atomic" means

One assertion per claim. A sentence carrying several assertions becomes several
claims.

> "Illinois requires 25,000 signatures and the deadline is in June."

becomes

1. "Illinois requires 25,000 signatures."
2. "The deadline is in June."

## Rules

1. **Preserve meaning exactly.** Rewrite only as much as is needed to make each
   claim stand alone. Resolve pronouns and other references using the
   surrounding sentences, because a claim that says "it doubled last year" is
   uncheckable on its own. Do not add detail the text does not contain, do not
   sharpen a vague claim into a precise one, and do not soften a strong claim
   into a defensible one. The reader must be able to see that the claim is
   what the author said.

2. **Keep the author's force.** "The law bans X" and "the law might ban X" are
   different claims. Hedges, intensifiers and modal verbs are part of the
   assertion, not noise to be tidied away.

3. **Keep quantities, names and citations verbatim.** Numbers, dates,
   jurisdictions, statute citations, and proper nouns are copied exactly as
   written, including any errors. A misquoted statute number is itself a
   finding; silently correcting it would erase it.

4. **Rhetorical questions that assert become claims.** "Do we really think a
   25,000-signature requirement is an accident?" asserts that the requirement
   is not an accident. Extract that assertion. A genuine question that asserts
   nothing produces no claim.

5. **Sarcasm and irony are flagged, not resolved.** If the literal reading and
   the apparent intent differ, extract the LITERAL assertion and set
   `ambiguous_stance` to true. Guessing at intent would put words in the
   author's mouth, and the flag lets a human see the ambiguity instead of
   inheriting your guess.

6. **A value judgment is a claim.** "That threshold is unfair" is one atomic
   claim. Extract it as written. Do not split it into the fact and the
   judgment; a later stage does that properly.

7. **Emit nothing rather than something.** A sentence with no assertion in it
   produces zero claims. Do not manufacture one to fill the slot.

## Output

For each sentence index you are given, return the atomic claims extracted from
it. Each claim carries:

- `text` — the claim as a standalone assertion
- `sentence_index` — which input sentence it came from
- `verbatim_span` — the exact substring of the source sentence the claim is
  drawn from, when one exists; null when the claim had to be rephrased to stand
  alone. Copy it character for character if you provide it; an approximate
  quote is worse than none, because it will fail to locate and the span will be
  discarded anyway.
- `ambiguous_stance` — true when sarcasm, irony, or reported speech makes the
  author's own commitment to the claim unclear
