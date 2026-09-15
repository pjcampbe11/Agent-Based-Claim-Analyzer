# Step 2 — Providers, Grammars, Structured Output

**Status: complete.** 347 tests passing, 92% coverage, lint clean. No pipeline
stages yet — that is step 3.

## What was built

| Module | Purpose |
|---|---|
| `providers/base.py` | `Provider` protocol, `GenerationRequest` / `Completion`, error hierarchy |
| `providers/transport.py` | Keep-alive stdlib HTTP client + `FakeTransport` |
| `providers/structured.py` | Extraction ladder, validate-and-retry, attempt accounting |
| `providers/grammar.py` | JSON Schema → GBNF for llama.cpp |
| `providers/ollama.py` | Ollama, with manifest-digest weight pinning |
| `providers/anthropic.py` | Claude via Messages API, structured output by forced tool-use |
| `providers/openai_compat.py` | OpenAI / Azure / OpenRouter / vLLM, `strict` schema mode |
| `providers/llamacpp.py` | llama-server, GGUF file hashing, GBNF grammars |
| `providers/registry.py` | Role → provider mapping, cross-role validation |
| `config.py` | `config.toml`, role definitions, credential resolution |
| `cli/_models_cmd.py` | `abca models` and `abca config` |
| `tests/gbnf_oracle.py` | Independent GBNF recogniser, used as a test oracle |

## Five decisions worth reviewing

### 1. Stdlib HTTP, not `httpx`

`EnvironmentInfo.packages` records every runtime-relevant library version in
each run record, and verification reports them on mismatch. Each dependency is
one more thing that can differ between the machine that produced a published
verdict and the machine trying to reproduce it. The entire surface needed here
is "POST JSON to localhost, get JSON back."

`http.client` rather than `urllib.request` specifically for **connection
reuse**: the `classifier` role runs once per claim, so a 2,000-comment thread
makes thousands of calls. A TCP handshake per call is measurable locally and
much worse over an SSH tunnel to EC2.

The transport is a `Protocol`, so every provider is tested against a fake with
no network, no GPU, no API key, and no model files. The full suite runs in
about five seconds.

### 2. Extraction is not repair

`extract_json` looks past prose and code fences to find a JSON object. It will
**not** mutate the bytes inside that object — no trailing-comma stripping, no
quote fixing, no bracket balancing.

- **Extraction** answers "where does the object start and end?" The model
  produced valid JSON and wrapped it in chatter. Recovering it loses nothing.
- **Repair** would answer "what did the model probably mean?" That is a guess,
  it can silently change a verdict, and it hides a model that is not following
  instructions.

Which strategy succeeded (`direct` / `fenced` / `brace_scan`) is recorded on
every attempt and surfaced in stage notes. A model that needs the fallback on
every call is a finding; silent leniency would erase exactly that signal.

### 3. The seed is held constant across retries

It is tempting to vary it to "get a different answer." That would be wrong
twice: it would make the run irreproducible, and it would paper over a prompt or
model problem by rerolling until something passed. A retry re-asks with the
validation errors named. It does not gamble.

Repair prompts are tailored per failure kind. Telling a model that produced
4,000 truncated tokens to "fix the validation errors" is useless — it did not
produce validation errors, it ran out of room. That case gets "be more concise,"
and the schema is not re-sent (it would eat the context the retry needs).

### 4. Declaration order in GBNF — a bug worth remembering

The obvious object-rule construction is "all required properties first, then the
optional ones." It is wrong, and it only shows up against a real model.

Pydantic emits JSON in **field declaration order**, not required-first. For
`Claim` that is `id, text, span_start, span_end, claim_type, ...` — where
`span_start` is optional and `claim_type` is required. A required-first grammar
rejects that ordering, so the sampler fights the model's natural output on every
token instead of guiding it.

The fix is two mutually recursive rule families that express optionality
positionally while preserving schema order:

```
head-i ::= member-i rest-(i+1)                    # property i is required
head-i ::= member-i rest-(i+1) | head-(i+1)       # property i is optional
rest-i ::= "," member-i rest-(i+1)                # required
rest-i ::= ("," member-i rest-(i+1)) | rest-(i+1) # optional
```

Linear in the number of properties, unlike "any subset in any order," which is
factorial.

A related bug, caught the same way: JSON object keys are not the bare text
`confidence`, they are `"confidence"` **including the quotes**, so the GBNF
terminal must be `"\"confidence\""` — a double encoding. Getting it wrong
produces a grammar that compiles cleanly and constrains the model to emit
unquoted keys, failing JSON parsing on every single attempt while looking
correct in the grammar text.

Both were found by `tests/gbnf_oracle.py`, an independent recogniser that
answers "does this grammar accept what it should and reject what it should."
Asserting on the emitted grammar text would have caught neither.

### 5. Pinning honesty is enforced, not documented

`ModelIdentity.weights_hash` is the load-bearing field of the whole layer.
`reproducible: true` is computed from it and every downstream guarantee rests on
it, so:

- Providers derive it **from the backend**, never from config.
- Ollama **raises** `PinningUnavailable` if the digest is missing rather than
  returning `None` — silently degrading would flip a run's reproducibility
  without anyone noticing.
- Hosted providers return `None` and the run is marked `reproducible: false`.
  Verification then returns `UNREPLAYABLE` **even when the replay output is
  byte-identical**, because agreement by coincidence is not verification.
- `RunRecord` rejects `reproducible: true` when any model lacks a hash.

## Backend comparison

| Backend | Pinning mechanism | Constrained decoding | Notes |
|---|---|---|---|
| `llamacpp` | SHA-256 of the GGUF, memoized on `(path, size, mtime)` | GBNF grammar | Strongest provenance |
| `ollama` | manifest digest from `/api/tags` | JSON Schema as `format` (≥0.5) | Default; works over an SSH tunnel |
| `anthropic` | none | forced tool-use with `input_schema` | Strongest hosted structured output |
| `openai` | none | `strict` json_schema mode | Schema auto-strictified |

**Ollama version handling.** Structured outputs landed in 0.5.0. Below that,
only `format: "json"` exists, which constrains output to *some* JSON object but
not to our schema — so the retry loop carries the whole burden, and
`constrained=False` is recorded so the ledger says which path ran.

**Claude's tool-forcing** is the strongest hosted path here: instead of asking
for JSON and hoping, abCA declares a tool whose `input_schema` is the target
schema and forces it. The response's `tool_use.input` is already a conforming
object — no prose to strip, no fence to unwrap. The extraction ladder reports
`DIRECT` every time on this path.

**OpenAI strict mode** requires every object to set `additionalProperties:
false` and every property to appear in `required`. Pydantic emits neither, so
`strictify_schema()` rewrites the schema: all properties become required, and
previously-optional ones become nullable. That is safe for these models, since
every optional field is either `X | None` already or has a default. If a schema
can't be strictified the provider falls back to plain JSON mode rather than
failing — a weaker path is a working system; a hard failure is not.

**Anthropic sends no `seed`.** The Messages API has no such parameter, and
quietly dropping a determinism knob the caller asked for is the sort of hidden
divergence the ledger exists to expose. The run is already marked
unreproducible, which is the honest signal.

## Credentials

Resolution order: explicit `api_key` argument → `api_key_env` variable →
nothing (valid for a local OpenAI-compatible server). Defaults are
provider-specific: `OPENAI_API_KEY` for OpenAI, `ANTHROPIC_API_KEY` for Claude.

Guarantees, each with a test:

- Keys are **never** written to a run record, logged, or included in a digest.
- `repr()` on a provider or a `ModelSpec` shows `<redacted>`.
- An OpenAI section never reads an Anthropic key, and vice versa.
- An inline key in config produces a warning, because the file then *is* a
  secret.
- Local providers report `n/a (local)`, not a scary "missing key."

## The fidelity-gate rule, enforced three times

`backtranslate` must differ from `adjudicator`. Checked at config load, at
registry construction, and in `RunConfig` validation.

Three places for one rule is not duplication. Each catches it at a different
moment, and the failure it prevents — a fidelity gate that silently passes
everything — produces no error and no visible symptom of its own. A rule with no
natural symptom needs redundant enforcement.

The registry check compares **resolved weights hashes**, not config strings.
Two sections can name the same model differently (`qwen2.5:32b` and a re-tagged
alias, or two llama.cpp paths to one GGUF) and a string comparison would miss
it. The weights hash cannot be fooled that way, which is why identity is derived
from the backend.

## Try it

```bash
python scripts/demo_step2.py    # full structured-output path, no model needed
```

Shows weight pinning, GBNF generation, a clean first-try adjudication, the
repair loop correcting an invented verdict, and a payload that is grammatically
perfect but semantically forbidden — the case that proves why validate-and-retry
runs even when constrained decoding is available.

## Next: step 3

Ingest / segment / classify / gate, `-t` only.

1. `Document` normalization with visible NFC at ingest (the canonicaliser
   deliberately does not normalize — that would silently mutate citation quotes).
2. Claim segmentation: one assertion per claim, compound sentences decomposed,
   sarcasm flagged as `ambiguous_stance` rather than guessed at.
3. Classification into the six-member taxonomy, using the small `classifier`
   model.
4. The political-relevance gate — where most of a large thread dies, cheaply.
5. Wire `RunRecorder` stages around each, so the first real `abca analyze -t`
   emits a complete, auditable ledger.
