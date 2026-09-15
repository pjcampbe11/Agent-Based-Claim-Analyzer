# Stage: fidelity — pass B (render)

Rewrite the legal provision so an eighth-grader understands it, **without
changing what it means**.

You are given the provision and the list of operative elements extracted from
it. Every one of those elements must survive into your rewrite.

## The constraint that outranks everything else

Simplification is exactly where legal meaning dies. A rewrite that reads well
and drops an exception is worse than no rewrite at all, because it looks
trustworthy while being wrong.

So: readability is the goal, fidelity is the constraint. When they conflict,
fidelity wins and the sentence stays a little harder to read.

## Rules

**Every element appears.** Including the awkward ones. Do not drop an exception,
a condition, or a proviso because it makes the paragraph clumsy. If you cannot
fit it gracefully, fit it ungracefully.

**Modal verbs keep their force.** "Shall" is not "should". "May" is not "must".
"Must not" is not "does not need to". Use plain equivalents — "has to", "is not
allowed to", "can choose to" — but never change which one it is.

**Numbers, dates and percentages exactly as written.** Do not round, do not
approximate, do not convert "1% or 25,000, whichever is less" into "about
25,000". That conversion is the single most common way a rewrite changes a
statute.

**Scope words are load-bearing.** "Only", "unless", "except", "all", "at least",
"not more than", "whichever is less" — each one changes what the rule covers.
Keep them, or keep what they do.

**Terms of art stay verbatim.** Phrases like "reasonable suspicion", "probable
cause", "de novo", "notwithstanding" have specific legal meanings that their
plain-language paraphrases do not carry. Keep the term and explain it in a
footnote right after it, like this:

> The officer needs *reasonable suspicion*.
> (**reasonable suspicion**: a specific, factual reason to think something is
> wrong — more than a hunch, less than proof.)

**Write for a reader, not for a score.** Short sentences, ordinary words, active
voice, one idea at a time. Say "must file" rather than "shall be required to
submit". Break a 60-word sentence into three.

**Add nothing.** No context the provision does not contain, no examples of your
own, no guidance about what someone should do. You are translating, not advising.

## Output

- `rendering` — the plain-language text
- `footnotes` — one entry per term of art you kept, with its plain explanation
