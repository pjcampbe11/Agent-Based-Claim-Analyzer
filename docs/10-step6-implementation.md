# Step 6 — The Plain-Language Fidelity Gate, and `abca explain`

**Status: complete.** 655 tests + 4 live-network, 90% coverage.
Contract §5 is now implemented rather than described.

## The problem this step exists for

The contract promises legal text rewritten so an eighth-grader understands it,
"without the meaning drifting." Both halves are easy to promise. The second one
is hard to keep, and it fails *quietly*:

> A rewrite that drops an exception reads **better** than one that keeps it.

Fluency and fidelity pull in opposite directions, and only fluency is visible to
the reader. A plain-language rendering of a statute is also the single most
quotable thing this tool produces — somebody will screenshot it — so a rewrite
that lost an "unless" is wrong in a way nobody downstream can detect by looking
at it.

So the rewrite is not trusted. It is tested.

## What was built

| Module | Purpose |
|---|---|
| `fidelity/elements.py` | The diff core: element kinds, modal detection, numeric anchors, conservative stemming, greedy best-first matching |
| `fidelity/readability.py` | Flesch-Kincaid grade — a flag, never a hard failure |
| `fidelity/linters.py` | Scope-marker and locked-glossary counts, source vs. rendering |
| `fidelity/__init__.py` | The deterministic surface, in one import |
| `prompts/.../fidelity.extract.md` | Pass A |
| `prompts/.../fidelity.render.md` | Pass B |
| `prompts/.../fidelity.backtranslate.md` | Pass C |
| `pipeline/models.py` | `ExtractedElement`, `FidelityExtractOutput`, `FidelityRenderOutput`, `GlossaryFootnote`, `FidelityBacktranslateOutput` |
| `pipeline/fidelity.py` | The three-pass gate, regeneration, the verbatim fallback |
| `pipeline/orchestrator.py` | `explain_citation()`, `build_explain_config()`, `EXPLAIN_ROLES` |
| `cli/_explain_cmd.py` | `abca explain --cite` |
| `schema/ledger.py` | `replay_projection()` prompt sort fixed to `(stage, content_hash)` |
| `scripts/demo_step6.py` | Every failure mode, end to end, with no GPU and no network |

## The three passes

```
source provision
      |
      v
[A] extract        (renderer model)  -> operative elements, verbatim
      |
      +-----------------------------+
      v                             |
[B] render         (renderer model)  |  sees source + elements
      |                             |
      v                             |
  rendering  --------+              |
      |              |              |
      |              v              |
      |    [C] back-translate       |  DIFFERENT model.
      |        (backtranslate model) |  Sees ONLY the rewrite.
      |              |              |
      |              v              |
      |      reconstructed elements  |
      |              |              |
      +--------> deterministic diff <+
                     |
                     v
          pass -> ship the rendering
          fail -> regenerate [B], up to 3 attempts
          exhausted -> ship the SOURCE TEXT VERBATIM, unresolved=True
```

## The four design decisions that carry the step

### 1. Pass C's isolation is a function signature, not an instruction

```python
def build_backtranslate_prompt(prompt_text: str, rendering: str) -> str:
```

There is no parameter through which the provision could reach the
back-translating model. The prompt file *also* tells the model it has not seen
the original and must not fill gaps from general knowledge — but that
instruction is a courtesy to the model, not the enforcement. An instruction can
be diluted by a later edit and nothing would fail; a missing parameter cannot be
passed by accident.

This matters because a pass C that could see the statute would reconstruct the
statute whether or not the rewrite preserved it, and the diff would then be
measuring the model's memory instead of the rewrite's fidelity. The gate would
report success on everything.

Tested twice: once on the prompt builder, once on the prompts the gate actually
sent during a real run.

### 2. The two models must differ, and it is checked in three places

`config.py` at load, `RunConfig` at record time, `run_fidelity()` at run time.
A model that grades its own simplification reproduces its own misreadings: if it
read "shall" as "may" in pass B, it will read the "may" in its own rewrite as
faithful in pass C.

The run-time check compares **resolved weights hashes**, not names — two config
entries can point at one model under different tags. A backend that cannot
report an identity is refused rather than waved through, because an unverifiable
independence claim is not an independence claim.

`backtranslate` is the only role in the system with **no fallback**. Every other
role degrades to a related one when unconfigured. This one raises, because the
degraded form of an independent back-translation is a gate that passes
everything — strictly worse than no gate, since the run record would then carry
a fidelity score that means nothing.

### 3. The diff is code, not a fourth model call

`fidelity_score` goes into the published run record and drives regeneration. A
model deciding "close enough" would make the score irreproducible and would put
the thing being audited inside the auditor.

Four signals, three of them exact:

| Signal | Comparison | Why exact |
|---|---|---|
| **Kind** | exact | A REQUIREMENT that returns as a CONDITION is the rewrite changing what the provision *does* |
| **Modal** | exact | `shall` ≠ `may` ≠ `must not` ≠ `need not`. Highest-signal check available, fully mechanical |
| **Numeric anchors** | all must survive | Numbers, percentages and dates are where legal meaning most often changes under simplification |
| **Token overlap** | ≥ 0.35 | The weakest signal, and meant to be — all it has to do is stop two *unrelated* elements from pairing up |

The modal and the anchors are **read from the element's text**, never reported
by the model. `ExtractedElement` has exactly two fields, `kind` and `text`, and
the omissions are the design: a model that could declare its own modal could
declare the one it happened to preserve.

Two subtleties that took iteration:

- **Stemming, not exact tokens.** A plain-language rewrite uses different
  words — that is the entire point — so raw token comparison punishes precisely
  the behaviour being asked for. The stemmer is deliberately conservative
  (plurals, possessives, `-ing`/`-ed`, trailing silent `e`); over-stemming is
  the dangerous direction, because two unrelated elements would start pairing up
  and the diff would begin confirming itself.
- **F1, not Jaccard.** A good rewrite *adds* words — articles, connectives, an
  explanatory noun — and Jaccard charges the whole union for them.

### 4. Every threshold is biased toward false FAIL

A false FAIL costs a regeneration and, at worst, emits the statute verbatim —
honest, if less readable. A false PASS ships a rendering that changed the law
while claiming to preserve it. Those costs are not comparable.

So `passed` requires **nothing dropped AND nothing added**. An invented
obligation fails as hard as a lost one: a rewrite that introduces a rule the
statute does not contain is a different law, not a clearer one — and it scores
1.00 on preservation, which is exactly why the added-element check exists.

## What the gate catches

From `scripts/demo_step6.py`, all decided in code.

One element, diffed against one reconstruction — the score is 0.00 whenever the
sole element fails to match:

```
PASS  faithful rewrite, different words       score=1.00
FAIL  modal softened to may                   score=0.00
FAIL  number changed (25,000 -> 20,000)       score=0.00
FAIL  percentage changed (1% -> 5%)           score=0.00
FAIL  unrelated element                       score=0.00
```

The full gate on 10 ILCS 5/10-2, whose three elements are one `WHO_IS_BOUND`
and two `REQUIREMENT`s:

```
PASS  faithful rewrite                        score=1.00   3/3 recovered
FAIL  dropped full-slate requirement          score=0.67   2/3, 3 attempts
FAIL  modal flip (shall -> may)               score=0.67   modal preserved: NO
FAIL  invented obligation                     score=1.00   3/3 -- and still FAIL
```

That last row is the one worth staring at. Every source element survived, so the
score alone looks perfect; the rewrite failed because it added a penalty the
statute does not contain.

A modal flip is reported as its own failure class, structurally rather than by
string-matching the diff's messages:

```
MODAL CHANGE (MANDATORY -> PERMISSIVE; REQUIREMENT -> PERMISSION): shall file a petition...
```

Dropping a rule and misstating its force are different failures, and a reader
deserves to see which one happened.

## Regeneration, and its one real cost

A failed attempt feeds the specific losses back into the next render prompt.
That is a deliberate trade with a cost worth naming: **telling a renderer what
it lost invites writing toward the checker rather than toward the reader.**

It is acceptable here only because the checker is not a keyword match. Passing
means an independent model, reading only the rewrite, reconstructs that element
with the same kind, the same modal force and the same numbers. The cheapest way
to satisfy that is to actually say the thing.

The alternative — a blind retry — is worse: the same model at temperature 0.0
given the same prompt produces the same rewrite, so a retry that adds no
information is not a retry at all.

## The fallback is a feature

Three failures emit the provision's own words with `unresolved=True`. Contract
§5 calls this outcome acceptable and requires it to be reachable, and the code
is written to reach it rather than to avoid it.

The score reported on a fallback is the **last attempt's**, not zero. The
rewrite did preserve most of the provision; it just could not be shown to
preserve all of it, and reporting 0.0 would overstate the failure as badly as
reporting 1.0 would hide it.

Failures the gate treats as "not a pass" rather than "a pass with a warning":

| Failure | Result |
|---|---|
| Pass A errors, or extracts nothing | verbatim — with no elements there is nothing to check a rewrite against |
| Pass B errors | counts as a failed attempt; loop continues |
| Pass C errors | **not a pass.** An unchecked rewrite is what the gate exists to stop |
| Pass A invents a number the statute lacks | that element is discarded, loudly — an extraction error, and the rewrite must not be blamed for it |

## Readability is a flag, fidelity is a gate

Flesch-Kincaid grade 7.0–9.0 is the target. Out of band is reported and never
fails the gate, because forcing a grade band by cutting clauses is precisely how
a rewrite loses an exception. Same for the scope and glossary linters: they run
every time, on the fallback text too, and they advise.

Fidelity outranks readability whenever they conflict. That ordering is stated in
the render prompt and enforced by which of the two can fail a run.

## `abca explain`

```
$ abca explain --cite "10 ILCS 5/10-2" --elements

FIDELITY GATE PASSED
elements in statute         3
recovered from the rewrite  3  (by an independent model that never saw the statute)
fidelity score              1.00
modal force preserved       yes
rewrites attempted          1
reading grade               grade 7.9 (within the 7-9 target)

plain language

A group of people who want to start a new political party across the whole
state must file a petition. The petition must be signed by 1% of the voters
who voted in the last statewide general election, or by 25,000 qualified
voters, whichever is less. The petition must also list the party's candidates.
It must name a candidate for every office to be filled. That list must be
there on the day the petition is filed.
```

**The gate's numbers print before the rendering, always.** The tool would look
more polished printing just the nice paragraph. It would also be the thing the
contract exists to prevent.

Stages recorded: `ingest → retrieve → fidelity → compose` — a valid subsequence
of `STAGE_ORDER`, so the record passes the same structural checks an `analyze`
record does and `abca verify` needs no special case.

The document ingested is the **citation**, not the statute. That is the input a
reader supplied and the thing a replay must reproduce; the statute is recorded
separately as a `SourceSnapshot` pinned by content hash, which is what makes a
later amendment surface as `DRIFTED` rather than silently changing what the run
appears to have said.

Retrieval routes through the ordinary `run_retrieve` stage rather than calling a
connector directly — same citation parser, same caps, same cache, same
unresolved reporting. A second source path would be a second place for source
handling to drift.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Gate passed; the rendering is the output |
| 2 | No citation given |
| 8 | No configuration |
| 9 | Backend unreachable, or the fidelity gate refused to run |
| 10 | **Gate failed.** The statute's own words were printed instead |
| 11 | The citation did not resolve to any source |

Exit 10 is load-bearing: a script piping this into a publishing step has to be
able to tell that what it received is the statute rather than a rewrite. It is
10 and not 3 because `verify` already owns 2–4 for `DRIFTED` / `DIVERGENT` /
`UNREPLAYABLE`, and a shared exit-code table where the same number means two
unrelated things will eventually mislead someone's CI.

## Where this differs from the spec in `docs/02`

Two deviations, both deliberate, both worth naming rather than quietly shipping.

**`--elements` instead of `--explain-fidelity`.** The spec imagined a flag that
emits "the full back-translation diff". The diff summary is not optional here —
it prints on every run, above the rendering — so the flag would have toggled
nothing worth toggling. What is genuinely optional is the pass-A element list,
which is long and only interesting when you are debugging a failure. So the flag
became `--elements` and the diff became unconditional.

**`--reading-level` is not wired to the target band.** `reading_level` is read
from config and recorded in the run record, but `TARGET_GRADE_MIN/MAX` in
`readability.py` are fixed at 7.0–9.0. Making the band configurable is easy and
was left undone on purpose: the band is a flag rather than a gate, so a
configurable value would change nothing about what passes — it would only change
which runs print an advisory line, while making the run record imply a
constraint that was never enforced. Wire it when it does something.

## Two fixes that came out of this step

`RunConfig.replay_projection()` sorted prompts by `stage` alone. That was
harmless while every stage used one prompt; the fidelity gate is three prompts
sharing one stage, so their relative order was left to whatever the input list
happened to be — and two identical recipes could hash differently, which is
exactly what the projection exists to prevent. Now sorted by
`(stage, content_hash)`. Records whose stages are all distinct sort identically
under both keys, so nothing already written is affected.

**Replay had nothing to dispatch on.** Adding a second pipeline created a hazard
that would not have surfaced until someone ran `abca verify` on an explain run:
`PipelineReplayer.replay()` always called `analyze_text`, so it would have
segmented and classified the citation string and reported a confidently wrong
`DIVERGENT` for a run that was never divergent. It would in fact have failed
earlier and more confusingly — `check_prompt_pins` looked up prompts by stage
alone, so a fidelity ref resolved to `fidelity.md`, which does not exist, and
every explain replay would have been refused with "prompt not present on this
machine". That message is not merely unhelpful; it is false.

Both fixed: `prompt_variant()` recovers the variant from the recorded path, and
`is_explain_record()` dispatches on the recorded **stage chain** — which the
ledger already validates and hashes — rather than on a flag. An explain run
reaches `fidelity` and never adjudicates; an analyze run is the reverse, so
there is no overlap to be ambiguous about.

`explain` also caches its input (the citation) in the `InputStore`, exactly as
`analyze` does, so a local `abca verify` does not require the citation to be
pasted back in. The record itself still stores only the hash.

## Contract §5, line by line

| Requirement | Where |
|---|---|
| Pass A extracts operative elements verbatim, with the controlling modal | `fidelity.extract.md`, `ExtractedElement`, `LegalElement.build` |
| Pass B targets FK 7.0–9.0 | `readability.py`, reported not enforced |
| Modal verbs preserved exactly | `detect_modal`, exact match in `elements_match` |
| Locked glossary carried through with footnotes | `linters.LOCKED_GLOSSARY`, `GlossaryFootnote` |
| Pass C: separate model, only the Pass B output | `assert_independent`, `build_backtranslate_prompt` |
| Dropped element → FAIL, regenerate | `ElementDiff.dropped`, the render loop |
| Added element → FAIL, regenerate | `ElementDiff.added` |
| Modal strength change → FAIL, regenerate | `modal_flips` |
| Three failures → verbatim, and reachable | `MAX_RENDER_ATTEMPTS`, `verbatim_fallback` |
| `fidelity_score` ships with the output | `FidelityReport.score`, shown in the report |
| Negation/scope linter | `scope_lint` |

## Try it

```bash
python scripts/demo_step6.py     # no GPU, no network
pytest tests/test_fidelity.py -q
```

## What is left

- **Step 7** — `-f` and `-u` inputs, claim clustering, then `-U`.
- **Step 8** — consensus mode, remaining connectors, packaging.
