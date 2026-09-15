# Architecture & Design Spec

Version: `0.1.0-draft`
Target: Windows 10/11 x64, single-binary CLI. Python 3.11+. Local-first inference.

---

## 1. Design posture

**Local-first.** Open-weight models via Ollama or llama.cpp are the default execution path. Hosted APIs are an opt-in accelerator, never a dependency. Three reasons, in order of importance:

1. **Reproducibility.** A local GGUF has a SHA-256. A hosted endpoint silently changes underneath you. §7's audit ledger only means something if the weights can be pinned.
2. **Independence.** A fact engine that can be switched off by a vendor is not independent.
3. **Cost and privacy.** Analyzing a 2,000-comment thread should not cost money or ship the thread to a third party.

**Deterministic orchestration.** This is a pipeline with typed stages, not a free-roaming agent. Agents are constrained workers inside fixed control flow. No stage decides what to do next; the graph does. This is a hard requirement for auditability — a run you can't replay is a run you can't defend.

**Latency is a feature.** The stated requirement is analysis that doesn't hold up the conversation. `--profile fast` must return a usable verdict on a single statement in **under 10 seconds** on a mid-range GPU laptop. Everything in the design bends toward that: aggressive caching, claim clustering, cheap early gating, small models for classification and large ones only for adjudication.

---

## 2. CLI surface

```
abca analyze [INPUT] [OPTIONS]
abca explain --cite <CITATION> [OPTIONS]
abca verify <RUN_ID>
abca models <list|pull|bench|hash>
abca sources <list|refresh|cache-stats>
abca config <get|set|path|init>
```

### Input selection (mutually exclusive, required for `analyze`)

| Flag | Long | Input |
|---|---|---|
| `-t` | `--text` | Raw statement as a string |
| `-f` | `--file` | Path to `.txt`, `.md`, `.json`, `.csv`, `.pdf`, `.docx` |
| `-u` | `--url` | URL — article, post, or a thread with comments |
| `-U` | `--user` | A public account handle; analyzes their political posting history |
| | `--stdin` | Pipe input |

### Core options

```
--profile fast|standard|forensic     default: standard
--models MODEL[,MODEL...]            override configured models
--consensus                          run N models, report disagreement
--format json|md|txt|table           default: table (tty), json (piped)
--out PATH                           write report
--reading-level N                    default 8
--seed N                             default 42
--temperature F                      default 0.0
--offline                            no network; cached sources only
--budget-tokens N / --budget-usd F   hard stop
--max-claims N                       default 200
--include-red-team / --no-red-team   default: included (removal requires --i-know)
--private                            permit analysis of non-public figures (warns)
--explain-fidelity                   emit the full back-translation diff
-v / -vv                             verbosity
```

### Profiles

| Profile | Models | Retrieval | Red-team | Target latency |
|---|---|---|---|---|
| `fast` | 1 small (7–8B) | Cache-only + 1 live lookup | Abbreviated | < 10s / statement |
| `standard` | 1 mid (14–32B) | Full retrieval, 3 sources/claim | Full | < 60s / statement |
| `forensic` | 3+ consensus | Exhaustive, all tiers | Full + adversarial second round | Minutes; correctness over speed |

### Examples

```powershell
abca analyze -t "The new law bans all firearm sales in Cook County." --profile fast
abca analyze -u https://example.com/thread/1234 --max-claims 500 --format json --out run.json
abca explain --cite "10 ILCS 5/10-2" --reading-level 8 --explain-fidelity
abca analyze -U @somehandle --since 2025-01-01 --consensus
abca verify 01J8X4K2QW7  # re-runs and diffs against the published hash
```

---

## 3. Pipeline

```
  ingest → segment → classify → gate → cluster → retrieve → adjudicate
                                                                 ↓
                              compose ← red-team ← fidelity (legal only)
```

**1. Ingest.** Normalizes any input to a canonical `Document` with provenance. URLs go through readability extraction; comment threads are preserved as a tree (parent/child matters — a reply's meaning depends on what it replies to). Records fetch timestamp and content hash.

**2. Segment.** Splits into atomic claims — one assertion each. Compound sentences are decomposed. Rhetorical questions carrying assertions are captured. Sarcasm and irony are flagged as `ambiguous_stance` rather than guessed at.

**3. Classify.** Assigns the §3 claim type. Small fast model; this is a cheap gate that determines everything downstream.

**4. Gate.** Non-political → `OUT_OF_SCOPE`, dropped before any expensive work. On a large thread this is where most of the input dies, cheaply.

**5. Cluster.** *The scaling answer.* In a 2,000-comment thread, most claims are restatements of a few dozen originals. Embed all claims, cluster by cosine similarity, analyze cluster **representatives**, propagate the verdict to members with a similarity score attached. Turns O(comments) into O(distinct claims). Cluster membership is in the output so a reader can check the grouping.

**6. Retrieve.** Type-routed lookup:
- `LEGAL` → statute/CFR/court corpora (see §5)
- `EMPIRICAL` → statistical agencies, then research indexes
- `ATTRIBUTIVE` → transcripts, dockets, filings, then T3
Cached hard. Each retrieval records URL, timestamp, hash, and the passage relied on.

**7. Adjudicate.** Per-claim verdict against the §4 vocabulary. Structured output enforced by JSON Schema with bounded retry.

**8. Fidelity.** Only when rendering legal text in plain language. The three-pass extract/render/back-translate gate. Pass C **must** run on a separate model instance with no source access, or it proves nothing.

**9. Red-team.** Mandatory adversarial pass. Findings ship in the output. Verdicts get auto-downgraded when the red-team materially undercuts them.

**10. Compose.** Report at the requested reading level and format.

Every stage appends a signed record to the run ledger.

---

## 4. Provider abstraction

One interface, three implementations, no leakage of provider concepts into the pipeline.

```python
class Provider(Protocol):
    def generate(self, prompt: str, schema: dict | None, *,
                 seed: int, temperature: float,
                 max_tokens: int) -> Completion: ...
    def embed(self, texts: list[str]) -> list[Vector]: ...
    def identity(self) -> ModelIdentity: ...   # name, version, weights hash, quant
```

| Implementation | Backend | Notes |
|---|---|---|
| `OllamaProvider` | Local Ollama daemon | Default. Model + digest recorded per call. |
| `LlamaCppProvider` | Direct GGUF via `llama-cpp-python` | For pinned weights and full determinism; no daemon. |
| `ApiProvider` | Anthropic / OpenAI / OpenRouter / Azure | Opt-in. **Marks the run `reproducible: false`** — a hosted endpoint cannot be pinned by hash. |

`ModelIdentity` is what makes the ledger meaningful. For local providers it includes the weights file SHA-256, quantization, and context length. For API providers it includes the model string and a note that the underlying weights are unverifiable.

**Structured output** is normalized across backends: native JSON mode where available, grammar-constrained sampling (GBNF) for llama.cpp, schema-validate-and-retry as the universal fallback. Never regex-scrape a model's prose.

**Model roles** are configurable and separable, so cheap work runs cheap:

```toml
[models]
classifier   = "qwen2.5:7b-instruct"      # segment, classify, gate
adjudicator  = "qwen2.5:32b-instruct"     # verdicts
backtranslate = "llama3.1:8b-instruct"    # MUST differ from adjudicator
embedder     = "nomic-embed-text"
consensus    = ["qwen2.5:32b", "llama3.3:70b", "mistral-small"]
```

Enforced constraint: `backtranslate` may not be the same model as `adjudicator`. Same-model back-translation reproduces the same misreadings and the gate silently passes everything.

---

## 5. Source layer

A cached, tiered retrieval service with pluggable connectors.

| Connector | Covers | Tier |
|---|---|---|
| `uscode` | U.S. Code (OLRC bulk XML) | T0 |
| `ecfr` | eCFR API | T0 |
| `ilcs` | Illinois Compiled Statutes | T0 |
| `courtlistener` | Opinions and dockets (CAP/CourtListener API) | T0/T1 |
| `federalregister` | Federal Register API | T0 |
| `congress` | Bill text and status (congress.gov API) | T0 |
| `fred` / `bls` / `census` | Economic and demographic series | T1 |
| `gao` / `cbo` / `crs` | Oversight and analysis | T1 |
| `fec` / `edgar` | Campaign finance, securities filings | T1 |
| `openalex` / `pubmed` | Peer-reviewed literature | T2 |
| `news` | Corrections-policy outlets, allowlisted | T3 |

Notes:

- **Tier is a property of the connector**, not a model judgment. The model cannot promote a source. This is deliberate — tier assignment is the one thing that must not be up for negotiation at inference time.
- Bulk corpora (U.S. Code, ILCS, CFR) are downloaded and indexed locally so `--offline` and `fast` work without network round-trips.
- Every cached document stores content hash + retrieval timestamp. Verdicts cite the snapshot, and `abca verify` compares against it. Law changes; a verdict is valid only against what it read.
- Retrieval is **hybrid** — BM25 for citation-shaped queries (statutes are found by number, not by vibe) plus dense vectors for conceptual lookup.

---

## 6. Output schema

```jsonc
{
  "run_id": "01J8X4K2QW7",
  "schema_version": "1.0.0",
  "prompt_version": "sotp/0.1.0",
  "input": { "kind": "url", "value": "...", "content_hash": "sha256:...",
             "retrieved_at": "2026-09-03T14:02:11Z" },
  "config": { "profile": "standard", "seed": 42, "temperature": 0.0,
              "models": [{ "role": "adjudicator", "name": "qwen2.5:32b",
                           "weights_hash": "sha256:...", "quant": "Q5_K_M" }] },
  "reproducible": true,
  "summary": { "claims_total": 84, "claims_analyzed": 31, "clusters": 31,
               "by_verdict": { "SUPPORTED": 9, "CONTRADICTED": 6, "MIXED": 4,
                               "UNSUPPORTED": 5, "UNVERIFIABLE": 7 },
               "by_type": { "LEGAL": 8, "EMPIRICAL": 11, "NORMATIVE": 7,
                            "PREDICTIVE": 3, "ATTRIBUTIVE": 2 } },
  "claims": [
    {
      "id": "c-014",
      "text": "The statute requires a 25,000-signature petition statewide.",
      "span": { "start": 412, "end": 470 },
      "type": "LEGAL",
      "verdict": "MIXED",
      "confidence": 0.86,
      "evidence_quality": "T0",
      "model_disagreement": 0.11,
      "cluster": { "id": "k-07", "members": 23, "min_similarity": 0.88 },
      "reasoning": "The statute sets 1% of the last statewide general election vote OR 25,000, whichever is less...",
      "citations": [
        { "tier": "T0", "title": "10 ILCS 5/10-2",
          "url": "https://ilga.gov/...", "quote": "signed by 1% of the number of voters ... or 25,000 qualified voters, whichever is less",
          "retrieved_at": "2026-09-03T14:02:31Z", "content_hash": "sha256:..." }
      ],
      "red_team": {
        "counter_evidence": "...",
        "steelman": "...",
        "overreach_flags": ["confidence exceeds single-source support"],
        "verdict_downgraded_from": "SUPPORTED"
      }
    }
  ],
  "fidelity": { "applied": true, "score": 1.0, "reading_grade": 8.2,
                "elements_source": 7, "elements_preserved": 7,
                "modal_verbs_preserved": true, "regenerations": 1 },
  "influence_patterns": [
    { "pattern": "narrative reuse across unrelated accounts",
      "documented_in": { "tier": "T1", "title": "ODNI ... assessment", "url": "..." },
      "attribution": null,
      "note": "Pattern match only. No actor attribution is made or implied." }
  ],
  "ledger_hash": "sha256:...",
  "timings_ms": { "ingest": 340, "segment": 210, "classify": 480,
                  "cluster": 150, "retrieve": 2100, "adjudicate": 4900,
                  "red_team": 1600, "compose": 300 }
}
```

`ledger_hash` covers everything above it. It is the publishable artifact — a verdict circulated without its hash is not a verdict this system stands behind.

---

## 7. Reproducibility ledger

`abca verify <RUN_ID>` re-executes a run from its stored config and diffs.

Recorded per run: input hash · model identities incl. weights hashes · seed · temperature · prompt template version + hash · every retrieval's URL/timestamp/hash · per-stage inputs and outputs · library versions · wall clock.

Verify outcomes:

- `IDENTICAL` — byte-identical reproduction.
- `EQUIVALENT` — verdicts and citations match; prose differs.
- `DRIFTED` — a cited source changed since retrieval. The diff shows what.
- `DIVERGENT` — verdicts differ under identical inputs. **This is a bug**, and it means either non-determinism leaked in or the config was misreported.

---

## 8. Repo layout

```
abca/
├─ README.md
├─ pyproject.toml
├─ docs/
│  ├─ 01-source-of-truth-prompt.md
│  └─ 02-architecture.md
├─ prompts/                     # versioned, hashed, diffable — no political positions
│  ├─ registry.toml
│  └─ sotp/0.1.0/{segment,classify,adjudicate,render,backtranslate,redteam}.md
├─ src/abca/
│  ├─ cli/                      # typer app, one module per command
│  ├─ pipeline/                 # ingest, segment, classify, cluster, retrieve,
│  │                            # adjudicate, fidelity, redteam, compose
│  ├─ providers/                # ollama, llamacpp, api, base protocol
│  ├─ sources/                  # connectors + tier registry + cache
│  ├─ schema/                   # pydantic models, JSON Schema export
│  ├─ ledger/                   # run records, hashing, verify
│  └─ report/                   # renderers: table, md, json
├─ evals/
│  ├─ golden/                   # fixed inputs + expected verdicts
│  ├─ fidelity/                 # legal passages + required elements
│  └─ adversarial/              # inputs designed to induce overreach
└─ tests/
```

**`evals/`, `corpus/` and `identity/` are data, never imported by `src/`.** Enforced by import-lint tests in CI. This is the code-level expression of the separation the README describes: if the analyzer can't import an opinion, it can't be quietly tuned by one.

---

## 9. Evals

Ship with the repo, run in CI, no merge on regression.

- **Golden set** — fixed inputs with hand-verified expected verdicts and citations. Guards against prompt regressions.
- **Fidelity set** — legal passages with hand-extracted required elements. Measures the back-translation gate against ground truth. Includes deliberately nasty cases: nested exceptions, double negatives, `notwithstanding` clauses, cross-references.
- **Adversarial set** — inputs engineered to induce overreach: normative claims phrased as empirical, confident falsehoods with plausible fake citations, claims where the true answer is `UNSUPPORTED`. **A model that never returns `UNSUPPORTED` fails this suite.**
- **Symmetry set** — claim pairs identical in structure, opposite in political valence. Verdict distributions must be statistically indistinguishable across the pair set. This is the partisanship test, and it is the one that matters most.

---

## 10. Packaging

- `pyproject.toml`, `src/` layout, `typer` CLI, `pydantic` v2 schemas, `rich` terminal output.
- PyInstaller one-file `abca.exe`. Signed. Later: winget and scoop manifests.
- Config at `%APPDATA%\abca\config.toml`; cache at `%LOCALAPPDATA%\abca\cache`.
- First-run wizard: detect Ollama, offer to pull default models, download and index bulk legal corpora.
- No admin rights required.

---

## 11. Build order

1. Schemas + ledger + `verify`. Auditability first — retrofitting it is how it ends up decorative.
2. Provider abstraction with Ollama, plus structured-output enforcement.
3. Ingest / segment / classify / gate. `-t` only.
4. Adjudicate against one T0 connector (ILCS — it's the smallest real corpus and directly useful).
5. Red-team pass.
6. Fidelity gate + `explain`. This is the hardest piece; do it once the surrounding machinery is stable.
7. `-f`, `-u`, clustering. Then `-U`.
8. Consensus mode, remaining connectors, packaging.

Evals are written alongside each step, not after.
