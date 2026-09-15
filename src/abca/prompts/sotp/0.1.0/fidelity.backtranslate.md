# Stage: fidelity — pass C (back-translate)

Below is a plain-language description of a legal rule. Read it and reconstruct
the **operative elements** it contains.

You have not seen the original provision, and you should not try to guess what
it said. Work only from the text in front of you. If the text does not mention
a penalty, there is no penalty element — do not supply one from what a statute
like this usually contains.

That instruction is the entire point of this pass. Someone is about to compare
your reconstruction against the real provision to find out what the plain-language
version lost. Filling a gap from your own knowledge of how such rules normally
work would hide exactly the loss they are looking for.

## The element kinds

| Kind | What it captures |
|---|---|
| `WHO_IS_BOUND` | Who or what the rule applies to |
| `REQUIREMENT` | Something that must be done |
| `PROHIBITION` | Something that must not be done |
| `PERMISSION` | Something that may be done |
| `CONDITION` | When the rule applies |
| `EXCEPTION` | When the rule does *not* apply |
| `EFFECTIVE_DATE` | When it takes effect, or sunsets |
| `PENALTY` | The consequence of not complying |
| `DEFINITION` | A term the text defines |

## Rules

**One assertion per element.**

**Record the force exactly as the text states it.** If the text says someone
"has to" do something, that is a requirement. If it says they "can choose to",
that is a permission. If it says they "are not allowed to", that is a
prohibition. Do not upgrade a choice into an obligation or soften an obligation
into advice.

**Copy numbers, dates and percentages exactly.** Including any that look
unusual.

**Report only what is there.** No element for anything the text leaves out. An
empty category is a finding, not a gap to fill.

**Ignore footnotes explaining terms of art.** Those are definitions of words,
not elements of the rule — unless the text presents one as the rule's own
definition.

## Output

For each element: its `kind` and its `text`, using the words of the text you
were given.
