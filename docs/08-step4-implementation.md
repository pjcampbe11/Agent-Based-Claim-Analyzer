# Step 4 — Sources, Adjudication, Replay

**Status: complete.** 553 tests + 4 live-network tests, 90% coverage, lint clean.
`abca analyze` now checks legal claims against the actual Illinois statute, and
`abca verify` replays a run end to end.

## What was built

| Module | Purpose |
|---|---|
| `sources/base.py` | `Connector` protocol, `RetrievedDocument`, quote verification |
| `sources/text.py` | HTML → plain text for statute pages |
| `sources/cache.py` | Content-addressed source cache with a hash check on read |
| `sources/ilcs.py` | Illinois Compiled Statutes, T0, by citation |
| `sources/registry.py` | Connector lookup and claim-type routing |
| `pipeline/retrieve.py` | Deterministic citation extraction + connector lookup |
| `pipeline/adjudicate.py` | Verdicts, and the citation verification pipeline |
| `pipeline/replay.py` | Rebuilding a recorded recipe; drift probing |
| `ledger/inputs.py` | Local input store, so your own runs replay without re-typing |
| `prompts/.../adjudicate.md` | The adjudication prompt |
| `cli/_sources_cmd.py` | `abca sources list/fetch/cache-stats/clear` |

## The headline property: a model cannot produce a citation

It produces a **pointer** and a **quote**. Everything else is supplied by code.

```
model returns  {source_id: "s-001", quote: "..."}
      |
      v
1. does s-001 exist among the documents we retrieved?      no -> discard
      |
      v
2. does the quote appear verbatim in that document?        no -> discard
      |
      v
3. build the Citation with the CONNECTOR's tier, the real
   url, the real content hash, the real retrieval time
      |
      v
4. schema validators run: SUPPORTED with no surviving
   T0-T2 citation fails, and the repair loop re-asks
```

`CitationRef` has exactly three fields — `source_id`, `quote`, `locator`. There
is no `tier` field, no `url`, no `content_hash`, and no `retrieved_at`, so
there is nothing for a model to fabricate. The alternative design, letting a
model emit a full `Citation`, fails in a specific way: a fluent model produces
a plausible URL, a plausible tier and a plausible-sounding quote, and the
result passes every downstream gate while being entirely invented. The gates
check that a tier is high enough, not that it is *true*.

Step 2 is the one check a fluent model cannot talk its way past. Against the
real 10 ILCS 5/10-2:

| Quote | Result |
|---|---|
| `signed by 1% ... or 25,000 qualified voters, whichever is less` | ✅ verified |
| `signed by 25,000 qualified voters ... without exception` | ❌ not found |
| `signed by **5%** ... or 25,000 qualified voters, whichever is less` | ❌ not found |

The third row is the dangerous one: one character different from the statute,
legally opposite, and invisible to a skimming reader. Substring verification
cannot miss it.

Step 4 is what makes step 2 bite. If verification removes the support a verdict
requires, the verdict is **downgraded to UNSUPPORTED and the downgrade is
written into the claim's reasoning**, so a reader sees what happened rather than
the claim silently vanishing.

## Six decisions worth reviewing

### 1. Tier is a class attribute on the connector

`ILCSConnector.tier = SourceTier.T0`, fixed at class definition. Not a
parameter, not configurable, not a field anything can write.

`RetrievedDocument.to_citation()` deliberately has **no tier parameter** — a
test asserts that, because if that signature ever grows one, model output could
reach it and every evidence gate becomes decorative.

Config cannot set it either. A `config.toml` that could declare an arbitrary
connector T0 would hand the most important safety property in the system to
whoever edits that file.

### 2. Whitespace tolerance, and its exact limit

Verification collapses whitespace on both sides before comparing. Statute text
is hard-wrapped and no model reproduces that byte for byte when quoting;
rejecting a correct quote over a line break would push the adjudicator toward
citing nothing — the opposite of what the evidence gate is for.

Nothing else is relaxed. Different words fail, different numbers fail, a dropped
"not" fails.

And the **stored** quote is the source's own wording, recovered from the
document by mapping the collapsed match back to real offsets — not the model's
reflow. A citation is supposed to reproduce what the source says.

### 3. Retrieval is deterministic

No model call. Citation extraction plus connector lookup, both plain code, for
the same reason sentence splitting is: the set of sources consulted is part of
the recipe, and a model picking different sources between runs would make
identical input produce a different `input_digest`.

It also means "which statutes did you look at, and why those?" has an answer
that does not involve trusting a model.

Claims with **no** retrieved source and no unresolved citation are not sent to
the adjudicator at all. That saves a model call and, more importantly, removes
the invitation to invent a citation to justify a verdict about something nobody
could check.

An **unresolved** citation still gets adjudicated — "you cited a section that
does not exist" is a finding worth stating.

### 4. `UNSUPPORTED` means two different things, and the report keeps them apart

- **checked, not established** — sources were consulted and did not settle it
- **not checked** — no connector covers this claim's type or citation

Collapsing those would be the most misleading thing this tool could do: a
reader would take "we looked and found nothing" from a claim nobody looked at.
The table prints `no source` for the second case, the claim's own reasoning says
"No source was consulted", and the coverage note is the first entry in the run
record's notes so it survives `--json` and survives being read from the ledger
months later.

Current coverage is Illinois statutes by citation. That is one connector, and
the output says so on every run.

### 5. The cache's read-time hash check is a security boundary

Every cached entry is re-hashed on load and discarded on mismatch. Without it,
anyone with write access to the cache could plant fabricated statute text — and
the adjudicator's quote verification would then confirm it **perfectly**,
because quotes are checked against exactly this text.

The cache is the one place where a forged source would look entirely legitimate
downstream. A test plants one and asserts it is rejected.

`retrieved_at` is preserved from the original fetch and never refreshed on a
cache hit. A timestamp that quietly advanced would make stale evidence look
fresh and would defeat drift detection, which is the one thing it is for.

### 6. Replay needs two halves, and that split is deliberate

The record stores **which** model ran — name, provider kind, weights hash — but
not `base_url`, timeouts or credentials.

A run against an Ollama on an EC2 box and a run against the identical model on a
laptop are the **same recipe**. Baking a host into `input_digest` would make
them falsely divergent, and would put internal hostnames into published records.

So replay takes connection details from the local config and model identity from
the record — then **verifies** the resolved weights hash against it and raises on
mismatch. "Reproducible" has to mean the machine actually has that model, not
merely that the record names one.

Prompt hashes are checked the same way. An edited prompt is the quietest
possible divergence: the recipe looks identical everywhere a human glances,
while the model was asked a different question.

## Input handling

Run records store the input's **hash**, not its text, so publishing a record does
not republish someone else's writing.

That is correct, and it has a consequence: a third party verifying a published
verdict must bring the statement themselves and prove it hashes to what the
record claims. `abca verify <run-id> --input "..."` does exactly that and
refuses on mismatch.

For your own runs, `analyze` caches the normalized text locally under the data
directory — never in the record, never published — so `verify` just works. That
store is hash-addressed and re-verifies on read, so an edited cached input
cannot be substituted for the text that was actually analyzed.

## The ILCS URL scheme

Undocumented anywhere; derived and verified against the live site:

| citation | file |
|---|---|
| `10 ILCS 5/10-2` | `001000050K10-2.htm` |
| `5 ILCS 140/1` | `000501400K1.htm` |
| `20 ILCS 3960/4.5` | `002039600K4.5.htm` |

Chapter zero-padded to four digits, then the act as four digits **plus one
decimal digit** (act `5` → `00050`, act `140` → `01400`), then `K`, then the
section. The decimal digit is not decorative — ILCS act numbers genuinely carry
one, and a plain five-digit zero-pad 404s on every request.

## Two bugs the tests caught

**A greedy section pattern.** `[0-9][0-9A-Za-z.\-]*` swallowed the
sentence-ending period in "…under 10 ILCS 5/10-2." producing section `10-2.`,
filename `...K10-2..htm`, and a 404 on any citation that ended a sentence —
which is most of them. The pattern now cannot end on a dot or dash.

**An empty quote verified against everything.** Python reports the empty string
as present in every string, so `document.contains("")` returned `True`. The
schema's `min_length` on `CitationRef.quote` made it unreachable through the
normal path, but `contains` is public and is the check every future connector
will rely on. It now returns `False` for empty and whitespace-only quotes.

A third, caught by the recorder rather than a test: a loop variable named
`document` in the retrieve stage **shadowed** the ingested `DocumentRef`, so the
analysis result was built from the last retrieved statute instead of the input.
The schema rejected it immediately — the value was the wrong type — which is
the validators doing exactly what they exist for.

## `IDENTICAL` versus `EQUIVALENT`

A real replay returns `EQUIVALENT`, not `IDENTICAL`, and that is expected.
`output_digest` covers the whole result object including the ingest timestamp,
which necessarily differs when a run is replayed later. `semantic_digest` covers
verdicts, confidences and citations — the things a reader relies on — and that
is what matches.

`EQUIVALENT` is the practical success outcome. `IDENTICAL` is reachable only
when the timestamps coincide, which in practice means a deterministic test.

## Try it

```bash
python scripts/demo_step4.py     # live ILCS, quote verification, replay
python scripts/demo_step3.py     # full pipeline end to end through the real CLI
abca sources fetch "10 ILCS 5/10-2"
pytest -m network                # the 4 tests that actually hit ilga.gov
```

## Next: step 5

The mandatory red-team pass (contract §7). Every verdict gets attacked before it
ships: strongest counter-evidence, best steelman of the position the analysis
went against, and explicit flags where the analysis reached past its sources.
Findings ship **inside** the claim, not in a log, and a verdict the red team
materially undercuts is downgraded automatically.

That pass is the one remaining structural promise in `docs/01` that the code
does not yet keep — `RunConfig.red_team` is recorded `False` on every run until
it lands.
