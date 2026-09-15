# levelset — comprehension-gap measurement instrument

Tool: `levelset/0.4.0` · Taxonomy: `gaps/1.0.0`
Script: `tools/levelset.py` — single file, stdlib only, Python 3.11+
**Outside the abCA pipeline. Not an Analyzer output. Never sent to anyone.**

---

## 1. What it is

A research instrument. It reads text-only political posts — posts with content but **no reference link** — and records, in a fixed taxonomy, the **comprehension gaps** their wording carries: places where the phrasing requires an assumption about how a process works that does not match how it works.

**The unit of analysis is the corpus, not the post.** A single reading is an intermediate record. The product is the aggregate: across N posts and threads, which mechanisms Americans most often get wrong, how often, in what combinations, and on which topics. That is a publishable finding about political language that requires adjudicating nothing.

What it does not do, by construction: issue verdicts (it has no sources), produce citations or hashes, make legal claims, record any author identity, or generate anything meant to be posted at anyone.

| | Analyzer (docs 01–20) | levelset |
|---|---|---|
| Unit | One claim | **A corpus** |
| Purpose | Adjudicate against evidence | Measure what is commonly misunderstood |
| Sources | T0–T2 required | **None** |
| Output | Verdict + citations + run hash | Coded gap records → frequency report |
| Publishable as a finding | Yes, with its hash | **No** |

---

## 2. The gap taxonomy — `gaps/1.0.0`

Free-text gaps cannot be counted, so every gap is assigned one of 22 fixed codes. Unknown codes are forced to `OTHER` and flagged. `levelset --taxonomy` prints the list.

| Group | Codes |
|---|---|
| Legislative process | `VOTE_TYPE` · `VOTE_STAGE` · `VENUE` · `CHAMBER_SCOPE` |
| Instruments & authority | `INSTRUMENT` · `GOVT_LEVEL` · `AUTHORITY` · `RULEMAKING` |
| Money | `FUNDING` · `SCORING` |
| Text & time | `TEXT_VS_TITLE` · `TIMING` · `SCOPE_QUANTIFIER` |
| Statistics | `STAT_LEVEL_RATE` · `STAT_DENOMINATOR` · `STAT_NOMINAL_REAL` · `STAT_BASELINE` |
| Reasoning & context | `CAUSAL` · `QUOTE_CONTEXT` · `RECENCY` · `PROCESS_OPACITY` · `OTHER` |

The taxonomy is versioned separately from the tool. Adding a code is a minor bump; changing what a code means is a major bump and invalidates comparison against earlier reports.

---

## 3. One-to-many: threads

A parent post plus its comment thread is **one unit**, not a pile of unrelated posts. Comments inherit the parent's topic and frequently repeat its gap verbatim.

- Entry 0 of a thread file is the parent, read with no parent context.
- Every comment is read **with the parent supplied as context**, because a reply's meaning depends on what it replies to.
- Records carry `thread_id`, `position`, and `role`.

**Thread weighting is the statistical core of the report.** Every code is counted twice:

- `raw` — every occurrence, once per post. What the corpus literally contains.
- `thr` — each code counted **at most once per thread**.

A parent's error echoed by 400 replies is one misunderstanding that spread, not 400 independent observations. Raw counts would let a single viral post dominate the entire finding. Standalone posts get a synthetic thread id, so a corpus of unrelated posts gives `raw == thr` and nothing is distorted. **The thread column is the headline number.**

---

## 4. Enforced constraints

The prompt asks; the code checks. A rule that lives only in a prompt fails silently and surfaces months later inside a published number.

| Lint | Action | Why |
|---|---|---|
| URLs, U.S. Code / CFR / ILCS cites, public laws, bill numbers, roll calls | **Redact** | The tool cannot verify a citation, so it emits none — *including correct ones*. An accurate citation the reader can't distinguish from an invented one is worse than none, because it teaches trust. |
| Verdict vocabulary (`true`, `false`, `misleading`, `debunked`…) | **Flag** | Sometimes legitimate in context. Reported in the footer so the reader discounts. |
| Deficiency attributions (`doesn't understand`, `is misinformed`, `lacks understanding`) | **Redact** | The thing this instrument must never produce. |
| Bare author references (`the poster`, `the author`, `OP`) | **Rewrite → "the post"** | "That is not the poster's fault" is a sentence about a person, but deleting it destroys meaning. Redirecting keeps the sense and drops the subject. |
| Off-taxonomy gap codes | **Coerce to `OTHER`, flag** | Keeps counts sound. |

Redaction is destructive on purpose. There is no flag to disable it.

**No author identity is recorded anywhere** — not in a record, not in a report. Platform and date only. A corpus of political speech that also stores who said it is a file on people, and this project does not build those.

---

## 5. Usage

```
levelset --taxonomy                                  # print the code list

levelset -t "<post text>"                            # one post, readable output
levelset -f post.txt --format json --out r.json

levelset --thread thread.txt --platform facebook     # parent + comments, one unit
levelset --corpus "posts/*.txt" --records-dir records
levelset --corpus "threads/*.txt" --thread-glob "x" --records-dir records

levelset --report "records/*.json"                   # the aggregate
levelset --report "records/*.json" --format json --out report.json
```

Options: `--consensus N` (a gap surviving only one run of N is demoted to low confidence, kept, and annotated), `--delim` (thread separator, default a line of `---`), `--backend ollama|openai`, `--model`, `--temperature 0.0`, `--seed 42`.

Text only — no `-u`, deliberately. A URL means a source exists, which means the claim belongs in the Analyzer.

A corpus run survives individual failures: a malformed model response is reported and skipped, failures are counted, and the run continues.

---

## 6. Verified behavior

Each stage tested against synthetic data, no model required. All five below are in `tests/test_levelset.py`, alongside tests that §7's isolation holds (by parsing the source, since a runtime check would pass a lazy import) and that no record field could hold an author:

1. **Thread splitting** — empty and doubled delimiters produce no phantom entries; blank lines inside an entry are preserved.
2. **Taxonomy normalization** — lowercase codes map correctly; unknown and blank codes coerce to `OTHER` and are flagged.
3. **Lints** — citations and URLs redacted; deficiency phrases removed; `"not the poster's fault"` → `"not the post's fault"`; clean text passes untouched.
4. **Aggregation** — out-of-scope posts excluded from the denominator; a 4-post thread repeating `VOTE_TYPE` collapses to `raw=4, thr=1`.
5. **Report** — renders both counts, co-occurrence pairs, topic breakdown, confidence distribution, and lint totals.

---

## 7. Isolation from the pipeline

`levelset.py` imports nothing from `src/abca/`, writes no ledger entry, and produces no artifact the Analyzer consumes. It could be deleted without affecting a build.

Preserve that. The Analyzer's credibility rests on every published claim carrying the hash of the run that checked it. A component producing unhashed, uncited readings must not feed the component producing hashed, cited verdicts, or the provenance chain has an unverified link and the argument collapses.

What the report *can* legitimately do is direct the explanatory work: if `VOTE_TYPE` is the most common gap across ten thousand posts, that says which explainer to write and which entries the institutional context pack (doc 19) needs first.

---

## 8. Status

**Built.** `tools/levelset.py`, 478 statements, stdlib only, 91% covered by
`tests/test_levelset.py` (72 tests). The uncovered remainder is the two HTTP backends,
which need a network. `scripts/demo_step11.py` runs the whole instrument end to end with
no model and no network.

Item 1 below was the blocker and is **closed**. Items 2–4 remain open.

### 8.1 Symmetry check on the corpus — DONE, and it is a gate

`scripts/check_levelset_symmetry.py`, over `evals/levelset/symmetry_pairs.json`: **48
matched post pairs**, same gap code, same grammatical shape, comparable length, opposite
valence. Nine measures:

| Measure | Catches |
|---|---|
| reading failed | the model failing to produce a parseable reading more often on one side — first, because it confounds everything below |
| in scope | one side read as "making a claim" more often |
| any gap recorded | one side coded as carrying a gap more often |
| expected code recorded | **the instrument being more accurate on one side** |
| gaps per post | the headline rate itself |
| high-confidence gaps | one side believed more readily |
| off-taxonomy coercions | the taxonomy fitting one side better |
| lints fired | the tool behaving worse on one side's language |
| characters redacted | **equal lint counts that eat unequal amounts of text** |

Two design decisions are worth recording because both were arrived at by getting them
wrong first.

**The stub is paired.** The first version gave each side of a pair its own hash-seeded
model reply. It was valence-blind and it was still wrong: two independent random draws
per pair manufacture real random discordance, so every measure became a coin flip against
noise the harness itself injected. Re-salting the hash twelve times made the suite pass
once and flag five different measures across the other eleven runs. A gate that fails
eleven times in twelve gets switched off, and a switched-off gate is worse than none. The
fix is to hold the model constant — the structural half of the reply is seeded off the
*pair*, identical for both sides — so the only thing that varies is the post's own words,
which is the only input levelset's code reads directly.

**Both halves are required.** The stub run measures levelset's CODE. `--backend` measures
the MODEL. A corpus is not publishable until both pass, and the tool prints that in the
footer of every report it produces rather than relying on anyone remembering it.

The gate is itself controlled by three tests, because a control that has never been shown
to fail is decoration:

- a lint biased toward one side's vocabulary, touching 22 of the first 32 pairs → asserts **FAIL**
- a model that finds one extra gap on every left-hand post → asserts **FAIL**
- a bias too narrow to detect → asserts **INDETERMINATE**, never PASS

The third is the subtle one. The danger is not that the gate misses a small bias; it must.
The danger is that it reports the miss as evidence of symmetry.

### 8.1.1 The model half, measured

CI's `model` job provisions Ollama on a plain runner (`.github/ci-model-config.toml`) and
runs the gate against `qwen2.5:3b-instruct`. Run here first, 32 pairs, every reading
written to disk: 0 of 64 failed, 61 in scope, 59 with gaps — real readings on both sides.
Verdict **INDETERMINATE**: 7 of 9 measures pass; `any gap recorded` sat at 3 discordant
pairs, all three the model finding a gap on the right-hand post only, and `expected code
recorded` at 3 (2 vs 1). The corpus was grown to 48 in response, which is the remedy the
gate prescribes, and re-run: **PASS on all nine measures**, 0 of 96 failed, 92 in scope,
and the leaning measure resolved to 2 vs 4 (p = 0.69) — noise, now shown to be noise.
Every reading is in `evals/levelset/model-run-qwen2.5-3b/`. This is the first corpus to
clear both halves of the gate.

Four things that run found, each now enforced in code and pinned by a test:

1. **Uncapped generation stalls a run.** One 1.5B call passed 2,300 tokens without
   finishing. `DEFAULT_MAX_TOKENS` truncates into a counted failure instead of a silent
   hang.
2. **Soft length hints are ignored by small models.** `levelset-prompt/0.5.0` states
   limits in words with an explicit stop; per-reading time fell from ~25s to ~2.5s.
3. **A single malformed reply must not end the gate.** It now becomes a failed reading,
   counted and measured as its own dimension.
4. **A model too weak to read the corpus produces a false calm.** `qwen2.5:1.5b` marked
   46 of 52 readable posts out of scope; every measure returned zero, and zero agreed
   with zero. The gate now refuses a run with under 50% of posts in scope and names the
   model as the finding.

And one in the harness itself: 0 discordant of 32 was "the strongest available symmetry
result" while 1 of 32 was "NOT evidence of symmetry" — a discontinuity that never healed
with more data, because a system differing on ~3% of pairs never reaches the six
discordant pairs the direction test needs. The harness now bounds the discordance rate
exactly when direction is untestable, states the worst case, and — the part the tests
pin hardest — still fails six one-way pairs, stays indeterminate on five, and withholds
PASS from three or more that all point the same way however tight the bound.

### 8.2 Still open

1. **Inter-model agreement.** Run the same corpus on two different local models and report
   code-level agreement. Low agreement means the taxonomy is underspecified, not that one
   model is right.
2. **Prompt extraction.** The prompt is embedded in the script. `PROMPT_VERSION` is bumped
   by hand and recorded in every report, so reports at least say which prompt produced
   them, but the text should move to a versioned file like the pipeline's prompts.
3. **Reading-level check** on the `how_this_generally_works` output, to hold the tool to
   the same eighth-grade standard the tool requires elsewhere. The fidelity gate already
   does this for the Analyzer; levelset cannot reuse it without importing from
   `src/abca/`, which §7 forbids, so it needs its own stdlib implementation.
