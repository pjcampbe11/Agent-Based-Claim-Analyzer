# Step 1 — Schemas, Ledger, Verify

**Status: complete.** 128 tests passing. No inference code exists yet, by design.

## What was built

| Module | Purpose |
|---|---|
| `abca/canonical.py` | RFC 8785-style canonical JSON + SHA-256 digests + hash chaining |
| `abca/ids.py` | ULID run identifiers (sortable, timestamp-bearing, transcription-safe) |
| `abca/schema/enums.py` | Closed vocabularies: source tiers, claim types, verdicts, outcomes, stages |
| `abca/schema/core.py` | `Citation`, `Claim`, `DocumentRef`, `AnalysisResult` + the contract gates |
| `abca/schema/ledger.py` | `RunConfig`, `StageRecord`, `RunRecord` + the three digests + `audit()` |
| `abca/ledger/recorder.py` | Builds the stage hash chain during a run |
| `abca/ledger/store.py` | Atomic on-disk store, audit-on-read |
| `abca/ledger/verify.py` | The five-outcome decision tree |
| `abca/cli/main.py` | `version`, `schema`, `verify`, `ledger list/show/audit/path` |
| `scripts/demo_step1.py` | Generates a real run record so the ledger can be exercised today |

## Four decisions worth reviewing

**1. Floats are serialized at fixed six-decimal precision, not shortest-round-trip.**
This is a deliberate deviation from RFC 8785. Implementing ECMAScript
`Number::toString` exactly means matching its exponent thresholds and negative
exponent formatting forever, and getting it subtly wrong produces hashes a
third-party verifier cannot reproduce. Fixed precision is trivially portable —
any language can `printf("%.6f")`. The cost is that confidence differences
below 1e-6 are invisible, which is the correct behavior anyway. Values too
large to carry six decimals are rejected rather than silently rounded.

**2. Object keys sort by UTF-16 code units, not Python code points.**
These disagree for any key containing a supplementary character, because
UTF-16 encodes those as surrogate pairs in D800–DFFF, which sort *below*
E000–FFFF. Python's default sort would produce a hash no conforming JCS
implementation could reproduce. `utf16_sort_key()` and its test exist for
exactly this.

**3. Three digests, not one.**
`input_digest` (the recipe), `semantic_digest` (verdicts + citations, no
prose), `output_digest` (everything). Comparing them in order is what
produces IDENTICAL / EQUIVALENT / DRIFTED / DIVERGENT. One digest would force
every prose rewording to read as a disagreement, which would make the strict
outcome fire constantly and train people to ignore it.

`TOOL_VERSION` is deliberately **excluded** from `input_digest`. Including it
would invalidate every published run hash on any patch release.

**4. DRIFTED is checked before DIVERGENT, and DRIFTED is not a success.**
A changed statute explains a changed verdict; calling it a tool bug would
bury the signal that means "tool bug." But it is not a pass either — a
published position resting on a repealed statute is exactly what a human
needs to be told about. `VerifyOutcome.is_success` excludes it.

## The tamper-evidence property, end to end

Three threat models, all covered by tests:

| Forgery | Caught by |
|---|---|
| Edit a stage's contents, leave hashes alone | `record_hash` no longer matches its contents |
| Edit a stage AND recompute its `record_hash` | the next stage's `prev_hash` points at a hash that no longer exists |
| Edit a stage and relink the **entire** chain | the chain head changes, so `ledger_hash` no longer equals the value that was **published** |

The third case is the important one. A fully relinked forgery is internally
flawless — every check inside the file passes. It fails only because
`ledger_hash` is published alongside the verdict and is therefore already in
someone else's hands. **Publishing the hash is the load-bearing act, not the
hashing.** That is why every published verdict carries its run hash rather
than leaving it in a log.

Reproduce it:

```bash
python scripts/demo_step1.py --ledger-root .abca/runs
abca ledger audit --ledger-root .abca/runs            # exit 0, "all intact"

# flip one verdict in the stored JSON
python -c "import json,pathlib,glob; p=pathlib.Path(glob.glob('.abca/runs/*.json')[0]); \
d=json.loads(p.read_text()); d['result']['claims'][1]['verdict']='SUPPORTED'; \
p.write_text(json.dumps(d,indent=2))"

abca ledger audit --ledger-root .abca/runs            # exit 6, TAMPERED
```

## Contract gates now enforced structurally

These were prose in `docs/01`; they are now validators that raise, which means
a model violating one fails validation and gets retried rather than believed:

- `SUPPORTED` / `CONTRADICTED` / `MIXED` / `MISLEADING_CONTEXT` require at
  least one T0–T2 citation. (contract §2)
- `NORMATIVE`, `PREDICTIVE`, `DEFINITIONAL` claims can only be `UNVERIFIABLE`
  or `OUT_OF_SCOPE`. (contract §3)
- `REPORTED_UNVERIFIED` requires a T3 source *and* the absence of any T0–T2
  source. (contract §2)
- `evidence_quality` must equal the best tier actually cited — it is computed,
  never asserted.
- `InfluencePattern.attribution` is typed `None`. A value cannot be set.
  (contract §6)
- `backtranslate` model must differ from `adjudicator`. (contract §5)
- `reproducible: true` is rejected when any model lacks a weights hash.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`IDENTICAL` / `EQUIVALENT` / clean audit) |
| 1 | run not found |
| 2 | `DRIFTED` — a cited source changed; re-examine |
| 3 | `DIVERGENT` — same recipe, different verdicts; this is a bug |
| 4 | `UNREPLAYABLE` — hosted API, weights cannot be pinned |
| 5 | record unreadable / malformed |
| 6 | integrity audit failed — the record was edited |
| 7 | integrity OK, replay not performed (step 1 only; removed once steps 2–4 land) |

## Deliberately absent

`abca analyze` is **not registered**. There is no pipeline behind it, and a
command that pretends to work is worse than one that is missing. `abca verify`
*is* registered and does the half of its job that is real — full integrity
verification — then exits **7** rather than **0**, so it cannot be mistaken
for a completed replay.

## Next: step 2

Provider abstraction with Ollama and structured-output enforcement. Concretely:

1. `Provider` protocol: `generate(prompt, schema, seed, temperature)`, `embed`,
   `identity`.
2. `OllamaProvider` — reads `weights_hash` from `/api/show`'s manifest digest,
   which is what keeps `reproducible: true` honest for both the local and the
   EC2-over-tunnel topologies (see `docs/04-linux-ec2.md`).
3. `LlamaCppProvider` — direct GGUF, GBNF grammar-constrained sampling.
4. `ApiProvider` — marks runs `reproducible: false`, loudly.
5. Schema-validate-and-retry as the universal structured-output fallback, with
   the retry count recorded in the stage notes so a model that needs five
   attempts to emit valid JSON is visible rather than hidden.
