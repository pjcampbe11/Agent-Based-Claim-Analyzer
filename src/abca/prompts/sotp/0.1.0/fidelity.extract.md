# Stage: fidelity — pass A (extract)

You are reading a legal provision. Break it into its **operative elements**: the
discrete pieces that together are what the provision does.

You are not summarizing, explaining or simplifying. That is a later pass. Here
you are taking the provision apart so that a plain-language rewrite can later be
checked against the pieces.

## The element kinds

| Kind | What it captures |
|---|---|
| `WHO_IS_BOUND` | Who or what the provision applies to |
| `REQUIREMENT` | Something that must be done |
| `PROHIBITION` | Something that must not be done |
| `PERMISSION` | Something that may be done |
| `CONDITION` | When the rule applies |
| `EXCEPTION` | When the rule does *not* apply |
| `EFFECTIVE_DATE` | When it takes effect, or sunsets |
| `PENALTY` | The consequence of non-compliance |
| `DEFINITION` | A term the provision defines |

## Rules

**One assertion per element.** A sentence carrying a requirement and an
exception becomes two elements, not one.

**Copy the operative words.** Each element's text should use the provision's own
language, trimmed to the piece it covers. Do not paraphrase here — the whole
point of this pass is to record what the provision says before anyone rewrites
it.

**Modal verbs stay exactly as written.** "shall", "may", "must not", "need not"
are the force of the obligation. Never substitute one for another, and never
smooth "shall" into "will" or "must".

**Numbers, dates and percentages verbatim.** Copy them character for character,
including any that look wrong. A misprinted figure is a fact about the source.

**Distinguish CONDITION from EXCEPTION carefully.** A condition says when the
rule *applies*; an exception says when it does *not*. Confusing them inverts the
provision. If a clause begins "unless", "except", "other than", or "provided
that", it is almost always an EXCEPTION.

**Do not merge for tidiness.** Three separate requirements are three elements
even when they appear in one sentence. Merging them is how one of them later
disappears without trace.

**Do not invent.** If the provision does not state a penalty, there is no
PENALTY element. Silence is not an element.

## Output

For each element: its `kind` and its `text`. Nothing else — the modal and the
numeric anchors are read from your text mechanically, so you do not report them.
