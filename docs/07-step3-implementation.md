# Step 3 — Ingest, Segment, Classify, Gate

**Status: complete.** 456 tests passing, 93% coverage, lint clean.
`abca analyze -t "..."` runs end to end and writes a verifiable run record.

## What was built

| Module | Purpose |
|---|---|
| `prompts.py` | Versioned, hashed prompt loading; doctrine-free check |
| `prompts/sotp/0.1.0/*.md` | The gate, segment and classify prompts |
| `pipeline/sentences.py` | Deterministic sentence splitter |
| `pipeline/ingest.py` | Visible normalization → hashed `DocumentRef` |
| `pipeline/models.py` | Stage I/O schemas + the in-flight `DraftClaim` |
| `pipeline/base.py` | Batching and per-stage cost accounting |
| `pipeline/gate.py` | Political-relevance gate |
| `pipeline/segment.py` | Sentences → atomic claims, with span resolution |
| `pipeline/classify.py` | The six-type taxonomy |
| `pipeline/orchestrator.py` | Stage wiring, ledger recording, draft → published claim |
| `cli/_analyze_cmd.py` | `abca analyze` |
| `scripts/mock_ollama.py` | Threaded Ollama test double |
| `scripts/demo_step3.py` | Real CLI, real HTTP, end to end |

## Six decisions worth reviewing

### 1. Sentence splitting is code, not a model call

Spans are the coordinate system everything downstream refers to. A model that
split sentences differently between two runs would make identical input produce
different spans — a `DIVERGENT` verification for no substantive reason.

Not spaCy or nltk either: both carry model files versioned separately from the
library, so the run record would have to pin a file it does not control. A few
hundred lines of explicit rules is smaller and auditable, and every rule exists
because a specific input broke without it:

| Input | Naive splitter | Here |
|---|---|---|
| `See 10 ILCS 5/10-2. The statute...` | splits mid-citation | 2 sentences |
| `Under 52 U.S.C. 30101 a committee...` | splits twice | 1 sentence |
| `Brown v. Board of Ed. changed...` | splits after `v.` | correct |
| `Turnout fell 3.5 percent.` | splits at the decimal | 1 sentence |
| `He said "this is wrong." Then...` | splits before the quote | after it |
| `1. First\n2. Second` | one blob | 2 sentences |

One trade is recorded explicitly in the tests: a lone capital followed by a
period is read as an initial, so `J. R. Smith filed` does not split — at the
cost of `A. B. C.` reading as one sentence. In political and legal text, names
with initials are constant and single-letter sentences essentially never occur.

### 2. Normalization is visible; hashing is not

`abca.canonical` deliberately does **not** Unicode-normalize, because silently
mutating a citation quote would make it disagree with its source. So
normalization happens once, at ingest, where it is:

- **named** — `DocumentRef.normalization = "abca-nfc-1"`, so a verifier knows
  what to apply before re-hashing;
- **counted** — every change appears in the stage record and the notes.

A document arriving with zero-width characters — routinely used to defeat
exact-match search — is a reported fact, not a silent cleanup.

The NFC change counter has a subtlety worth keeping: a positional `zip()` diff
reports the whole document as changed, because NFC alters length and every
position after the first difference then compares unequal. `SequenceMatcher`
counts the real edit.

What is **not** done: case folding, punctuation straightening, intra-line
whitespace changes. The line is *normalize representation, never content*.

### 3. Span resolution has three tiers and no fuzzy matching

Model-reported character offsets are unreliable — models count tokens. So spans
are resolved by searching the source sentence:

| Tier | Meaning |
|---|---|
| `verbatim` | the model's quote located exactly in the sentence |
| `text` | no usable quote, but the claim text itself located exactly |
| `sentence` | neither located; the span covers the whole source sentence |

Tier 3 is a correct answer. A claim rewritten to stand alone — "it doubled"
becoming "the deficit doubled" — genuinely has no verbatim span, and pointing at
the sentence is honest. What is refused is fuzzy matching: an approximate span
points a reader at text the claim did not come from, which is worse than
admitting the span is broad.

One bounded relaxation: the first character's case may differ, because
extracting a clause into a standalone claim forces a capital. That is one
deterministic alternative at a known position, not similarity scoring.

The tier distribution is reported, because a model that never produces a
locatable quote is a finding.

### 4. Each stage fails in a chosen direction

This is the part that separates an analysis which is *incomplete and says so*
from one that is *quietly wrong*.

| Stage | On failure | Why |
|---|---|---|
| **gate** | pass the sentence through as in-scope | A false negative is invisible — the claim silently never gets examined. A false positive costs one cheap classification. |
| **segment** | one claim per sentence, undecomposed | A worse analysis still contains the author's words. Dropping the batch removes them with no trace. |
| **classify** | mark `NORMATIVE` → `UNVERIFIABLE` | Refusing to answer is safe. Guessing that an unclassified claim is checkable is not. |

Note the gate and classify fail in *opposite* directions, and deliberately: the
gate errs toward examining more, the classifier errs toward adjudicating less.

### 5. Gated-out sentences are recorded, never deleted

A sentence the gate excludes still becomes a claim, marked `OUT_OF_SCOPE` with
the gate's stated reason. A reader must be able to see *what* was excluded and
*why*. Silently dropping text is how an analysis becomes unfalsifiable — and an
unfalsifiable analysis is the thing this project exists to oppose.

### 6. An unfinished pipeline says so, in every format

Retrieval and adjudication do not exist yet, so no verdict-eligible claim has
been checked against anything. Emitting `UNSUPPORTED` and moving on would be
literally true — it *is* defined as "no qualifying evidence found either way" —
and misleading, because it implies a search happened. Nobody looked.

So:

- `AnalysisResult.notes[0]` is the warning, so it survives `--json` and survives
  being read out of the ledger months later;
- each pending claim's own `reasoning` says "No source was consulted", so the
  caveat survives being read one claim at a time;
- verdict confidence is `0.0` for pending claims — a nonzero number would imply
  a verdict was reached;
- the terminal report prints `not yet checked` rather than `UNSUPPORTED`;
- `RunConfig.red_team` is recorded `False`, because the pass does not exist and
  recording `True` would be a lie in the ledger.

Claims whose verdict **is** final in this build — `OUT_OF_SCOPE` from the gate,
`UNVERIFIABLE` from a non-eligible type — carry real confidence, because those
follow from a decision that was actually made.

## Prompts

Three shipped prompts, under `src/abca/prompts/sotp/0.1.0/`, as package data so
they survive `pip install`. Each run records their SHA-256, and those hashes are
part of `input_digest` — so editing a prompt changes the recipe, and a verdict
produced under the old wording cannot be silently claimed for the new one.

`assert_doctrine_free()` runs as a test and rejects political-position and steering
markers. Its list is deliberately symmetric: a checker that flagged only one
side of the usual axes would itself be a bias. It is a smoke alarm, not a proof
— it cannot detect subtle steering, and it does not replace reading the diff.

## A bug the demo found

The first end-to-end run **deadlocked**. abCA builds one provider per role, and
each holds its own keep-alive connection, so a run using `classifier` and
`segmenter` opens two connections. A single-threaded `HTTPServer` stays inside
the first connection's request loop under HTTP/1.1 keep-alive and never accepts
the second.

Real Ollama and llama-server are both concurrent, so the fix was in the mock,
not the client — but it is exactly the class of bug that only appears once real
sockets are involved, which is why `demo_step3.py` and the CLI tests drive the
actual binary over real HTTP rather than a fake transport.

## The segmenter role

New optional role, falling back to `classifier` when unset. Segmentation is
linguistic and a small model handles it, but decomposing a dense legal sentence
rewards a stronger one — so it can be split out without restructuring anything:

```toml
[models.segmenter]
provider = "ollama"
model    = "qwen2.5:32b-instruct"
```

## Try it

```bash
python scripts/demo_step3.py
```

Starts a threaded mock Ollama, writes a config, and runs the real `abca analyze`
as a subprocess — then verifies the ledger and attempts two forgeries.

The forgeries are the interesting part, because they fail at *different layers*:

1. Flipping a verdict to `SUPPORTED` never reaches the hash check. The contract
   gate rejects it first: `SUPPORTED` requires a T0–T2 citation and the claim
   has none.
2. A schema-legal edit — `UNVERIFIABLE` → `OUT_OF_SCOPE` on a `NORMATIVE` claim,
   which the type gate permits — passes every validator and still fails, because
   the semantic and output digests no longer match.

## Next: step 4

Adjudicate against one T0 connector (ILCS).

1. Source layer with tiering fixed by the connector, never by a model.
2. `ilcs` connector: bulk download, local index, hybrid BM25 + dense retrieval —
   statutes are found by citation number, not by vibe.
3. `SourceSnapshot` recording, which is what makes `DRIFTED` detectable.
4. The adjudication prompt and the evidence gate operating on real citations.
5. `abca verify` gains its replay half, and the step-1 exit code 7 goes away.
