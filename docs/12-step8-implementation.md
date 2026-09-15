# Step 8 — Consensus, Federal Connectors, Packaging

**Status: complete.** 847 tests + 9 live-network, 89% coverage, repo-wide lint clean.
Every command the architecture spec promised now exists, and the README's central
credibility claim is executable rather than aspirational.

## What was built

| Module | Purpose |
|---|---|
| `sources/citations.py` | One citation grammar for every corpus — the routing table of the evidence system |
| `sources/ecfr.py` | CFR sections via the eCFR versioner API (T0) |
| `sources/fedreg.py` | Federal Register: identified by its API, quoted from GPO (T0) |
| `pipeline/consensus.py` | A model panel that reports disagreement instead of averaging it |
| `providers/transport.py` | `get_text` gained gzip; eCFR refuses uncompressed requests |
| `pipeline/replay.py` | The drift probe is generic — every connector is probeable |
| `.github/workflows/ci.yml`, `abca.spec` | CI on Windows + Linux; a single-file exe |

---

## 1. The citation grammar is the routing table

Retrieval is deterministic, so what gets fetched is decided entirely by what a
claim **names**. That makes citation parsing the routing table of the whole
evidence system — and a routing table spread across five connectors is one
nobody can read. So every form lives in one module, each knowing which connector
serves it.

```
10 ILCS 5/10-2      → ilcs           11 CFR 100.5     → ecfr
FR Doc. 2026-18141  → fedreg         52 U.S.C. 30101  → (recognised, unserved)
89 FR 12345         → (unserved)     576 U.S. 644     → (unserved)
```

### Recognised-but-unfetchable is a first-class outcome

A citation this build can parse and cannot retrieve is reported as **unresolved**,
not ignored. "We read this citation and cannot serve it" is a coverage gap
somebody can act on. "Found no citation" looks like the claim cited nothing.

Each unserved form records *why*, in `NO_CONNECTOR_YET`:

- **U.S.C.** — uscode.house.gov renders section text client-side, so the initial
  HTML is navigation; govinfo has the text but its URLs embed a
  subtitle/chapter/subchapter path no bare citation contains.
- **`89 FR 12345`** — the API exposes no citation condition, a full-text search
  for the citation string returns nothing (the string does not appear in the
  document that carries it), and the `/citation/` redirect is bot-walled.
- **Case citations** — CourtListener throttles anonymous callers to 125/day. A
  connector that works for a session's first few claims and then stops, for
  reasons unrelated to the claims, is worse than no connector: a run would
  sometimes have evidence and sometimes not, and the difference would look like
  a finding.

### Why empirical claims still have no route

**A connector can only serve a claim that names its source.** "Unemployment was
4.1% in June" names none. Sending it to a statistics API means something must
*choose* a series, and the only thing capable of that choice is a model — which
would make retrieval non-deterministic and let a confident guess fetch a number
the author never referred to, which the adjudicator would then quote faithfully.

That is a property of the design, not a gap in it. It is now stated in
`registry.ROUTING` rather than left to be inferred from an empty tuple.

---

## 2. Two federal connectors, and what they cost to get right

### eCFR asks for the date rather than assuming it

`/full/{date}/title-{n}.xml` returns the title **as it stood on that date** —
exactly the property this project needs, since a verdict is only valid against
the snapshot it cited.

The first version defaulted to today and 404'd on everything. eCFR serves only
dates it has *issued*: title 11 was last amended in February and its latest
issue is June, so "today" does not exist. A guessed date makes a perfectly valid
citation look like a nonexistent section, so the connector asks
`/titles.json` for the title's latest issue, caches it per process, and records
the date it used. A replay passes the recorded date and gets the same bytes.

The endpoint also **refuses uncompressed requests** outright, which is why
`HttpTransport.get_text` gained gzip — with the size cap applied *during*
decompression, since a few kilobytes of gzip can expand to gigabytes and every
fetch here goes to somebody else's server.

### The Federal Register is identified by its API and quoted from GPO

Every full-text endpoint on federalregister.gov — `raw_text_url`,
`full_text_xml_url`, `body_html_url`, the `/citation/` redirect — sits behind a
bot wall that answers with a cross-origin redirect to
`unblock.federalregister.gov`.

The wall keys on the **TLS fingerprint**, not on headers or HTTP version:

```
curl --http1.1 -A "abca/0.1" <url>   → 200
python http.client, same headers     → 302 unblock.federalregister.gov
```

Getting past that means forging a browser's TLS ClientHello. **This project will
not do that.** Circumventing a publisher's access control to obtain evidence, in
a tool whose entire argument is epistemic honesty, would be self-refuting — and
the workaround exists anyway: GPO publishes the same printed page openly, and
GPO is the authority the Register is printed by. The provenance is if anything
better.

The JSON API is not walled, so identification still comes from the Register
itself. Only the bytes come from GPO.

### Evidence comes from the host that was asked

A connector follows same-host redirects and **refuses cross-host ones**. A
connector's tier is a promise about a publisher; following a redirect off the
host would attach `T0 — primary legal text` to whatever answered — a bot wall
today, and in the general case anything at all.

---

## 3. Consensus: the mode that must not average

`--consensus` runs adjudication on a panel and reports where members disagree.

> Publishing the majority verdict produces a number that looks more trustworthy
> than any single model's and is, in the case that matters, less so.

When two models say `SUPPORTED` and one says `CONTRADICTED`, the majority answer
hides the most useful fact the run produced. A claim that splits a panel is not
67% true; it is a claim the sources did not settle for every reader of them.

So the panel publishes the **weakest verdict any member reached** and ships the
split as `model_disagreement` on the claim. Same reasoning as the red team's
one-way ratchet: a mechanism with the power to change verdicts is trustworthy in
proportion to how narrow that power is. The worst a disagreeing panel can do is
make the tool say *less* than one member knew.

| Panel | Disagreement | Published |
|---|---|---|
| 3 × `SUPPORTED` | 0.00 | `SUPPORTED`, confidence = the lowest reported |
| 2 × `SUPPORTED`, 1 × `MIXED` | 0.33 | `MIXED` — the weakest, never the majority |
| `SUPPORTED` + `CONTRADICTED` | 0.50 | `UNSUPPORTED` at 0.0 — see below |

### The case the strength table cannot settle

`SUPPORTED` and `CONTRADICTED` are both strength 3, so "weakest" does not choose
between them. That is not an oversight: they are **opposite, not ordered**.

A panel holding both resolves to `UNSUPPORTED` at zero confidence, with both
sides' citations attached and a note saying plainly what happened. `MIXED` was
the alternative and was rejected — MIXED is a claim about the **evidence**
("substantive support on both sides"), and asserting it because two models
disagreed would put the tool's own confusion into a slot reserved for a reading
of the sources.

### Two configuration rules

**At least two models.** A panel of one always agrees with itself.

**No duplicates**, compared by resolved weights hash rather than config string —
two sections can point at one model under different tags. At temperature 0.0 the
same weights return the same verdict, so a duplicated member reports unanimous
agreement produced by asking one model twice: the most misleading output this
mode can produce.

Confidence is the **lowest** any member reported, never the average. Citations
are the **union**, so a reader sees the passage that persuaded the dissenter. A
member that fails entirely is recorded as silent and named in the reasoning —
two models that answered are still a panel.

---

## 4. Packaging

- **`pyproject.toml`** — classifiers, URLs, keywords, and an explicit `[pdf]`
  extra.
- **`abca.spec`** — PyInstaller, single-file. The prompts are bundled as package
  data because every run record hashes them: a binary shipping different prompt
  bytes than the wheel would produce run hashes no wheel-based verifier could
  reproduce. The four provider backends are `hiddenimports` because they are
  imported lazily and PyInstaller cannot see a lazy import — without them the
  frozen build would support only whichever backend happened to be imported at
  build time.
- **`src/abca/__main__.py`** — `python -m abca`, and the frozen entry point.
- **`.github/workflows/ci.yml`** — lint and tests on Windows and Linux across
  Python 3.11 and 3.13; live-network tests weekly, reported but never blocking.

### The ruff configuration is now explicit

Without a `select` list, a ruff upgrade silently changes what CI enforces — a
build that passed yesterday fails today for reasons nobody chose, which is how a
team learns to ignore its linter. The set is now named, and every suppression
carries its reason in the config rather than as a bare `noqa`.

`E501` is not enforced, and that is a considered choice: this codebase's
defining feature is long explanatory prose, and a rule that fires forty times on
deliberate docstrings is one people learn to skip past.

---

## Fixes that came out of this step

**The drift probe only knew about one connector.** It `isinstance`-checked
`ILCSConnector` with a comment saying others would register their own — so every
connector added afterwards was silently un-probeable, and a changed source
behind one would have gone unreported while `verify` still called the run fine.
Each connector now implements `locator_for_url`, and the probe asks.

**`HttpTransport` had no public non-JSON GET.** The ILCS connector reached into
`_connect()` and `_headers`, so the one code path that hits somebody else's
server was the one bypassing the transport's timeout handling and
stale-keep-alive retry. Now `get_text` → `TextResponse`, shared by three callers.

**`StageOutcome` could not absorb another stage.** Consensus runs a whole
adjudication per panel member and must report the panel's cost as one stage;
passing a `StageOutcome` to `absorb()` raised, because it has an attempt *count*
where a `StructuredResult` has a list. `absorb_stage()` is the missing
primitive, and it labels the other stage's notes so a note from one member is
attributable to that member.

**The mock server answered questions it was not asked.** Given a document it had
no script for, it fell through to another document's canned response — so a
demo could report confident findings about a statement the run never contained.
It now answers unrecognised documents with nothing, which is what a test double
that cannot know the answer should say.

---

## Where this leaves the spec in `docs/02`

Implemented in this step: `--consensus`, federal connectors, packaging.
Still unimplemented and honestly so: `--budget-tokens` / `--budget-usd`,
`--format`/`--out` on `analyze`, `--reading-level` as a live band, `--since` on
`-U`, and the `models pull`/`bench` subcommands.

Connector coverage remains the largest real gap, and the reason is structural
rather than a matter of effort: a source can only be served deterministically
when a claim names it.

## Try it

```bash
python scripts/demo_step7.py         # inputs and clustering, no GPU or network
pytest -q                            # 847 tests
pytest -q -m network                 # 9 live tests against ilga.gov, eCFR, GPO
```
