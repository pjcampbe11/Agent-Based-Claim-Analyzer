# Step 7 — Real Input: `-f`, `-u`, `-U`, and Claim Clustering

**Status: complete.** 741 tests + 4 live-network, 89% coverage.
Every input the architecture spec promised now works, and the CLUSTER stage that
has been sitting in `STAGE_ORDER` since step 1 is now a real stage.

## What was built

| Module | Purpose |
|---|---|
| `inputs/threads.py` | `Post` / `Thread`: the one shape every input becomes. Author pseudonymization |
| `inputs/records.py` | JSON and CSV conversation exports, per-row field resolution |
| `inputs/files.py` | `-f`: .txt .md .json .csv .docx (stdlib) .pdf (optional extra) |
| `inputs/web.py` | `-u`: fetch, redirects, schema.org JSON-LD, whole-page fallback |
| `inputs/select.py` | One place where every flag becomes a thread |
| `pipeline/cluster.py` | The CLUSTER stage: deterministic near-duplicate grouping |
| `pipeline/orchestrator.py` | `analyze_thread()`; `analyze_text()` is now a wrapper |
| `providers/transport.py` | `get_text()` + `TextResponse` — a public non-JSON GET |
| `schema/ledger.py` | `audit_detail()`: problems and caveats are different things |
| `cli/_analyze_cmd.py` | The new flags, the `from` and `×` columns, the cluster block |

## Everything becomes a thread

```
-t "..."      ──┐
--stdin       ──┤
-f file.txt   ──┤
-f export.json──┼──▶ Thread ──▶ render() ──▶ one document + a span per post
-u https://...──┤                                    │
-U @a --from X──┘                                    ▼
                                    ingest → segment → classify → gate
                                       → cluster → retrieve → adjudicate
                                       → red_team → compose
```

A bare statement is a thread of one post. That is not a cute framing — it is
why `-t` cannot quietly diverge from `-f`. One code path, one normalization
recipe, one set of limits.

**The span map is the important half.** Which post a claim came from is tracked
*alongside* the text, never inside it. Writing `[a-01]` into the document would
hash it, feed it to the segmenter, and eventually get it extracted as though
the author had written it.

## Authors: pseudonymized before anything is recorded

Contract §8 refuses to score, rank or characterize a person. A run record is
meant to be published. Those two facts force a conclusion:

> A published record must not carry the handles of the people whose comments
> were analyzed.

So a handle becomes `a-01`, `a-02` in first-appearance order, and only the
pseudonym is ever written into a record — which for the current build means the
pseudonyms do not appear either, because the record stores **counts only**. The
real handles live on the in-memory `Thread`, where the local terminal report uses
them. That is exactly the rule the input text already follows: the record stores
the hash, the local input store keeps the text.

Positional stand-ins rather than hashed handles, deliberately. A hash of a
handle is reversible by anyone with a list of handles to try, which for a public
platform is everyone. `a-03` leaks the order somebody commented in and nothing
else.

There is a test that serializes a whole run record and asserts that no handle
appears anywhere in it.

## `-f`: five formats, and one rule

**Dispatch is by extension, never by sniffing content.** A `.txt` holding a JSON
array stays prose. Sniffing would silently reparse it as a conversation,
changing which words get attributed to whom, with no sign to the person who
named the file.

| Format | How | Notes |
|---|---|---|
| `.txt` `.md` `.log` `.rst` | decode | |
| `.json` | export parser | article + nested replies, depth preserved |
| `.csv` `.tsv` | export parser | dialect sniffed, falls back to comma |
| `.docx` | **stdlib** `zipfile` + `ElementTree` | whole-tree walk, so table and text-box content is not dropped |
| `.pdf` | optional `abca[pdf]` extra | lossiness note leads the run's notes |

### Two decoding bugs worth naming

**UTF-16 was in the ladder, and it should not have been.** `b"caf\xe9"` is four
bytes, so `bytes.decode("utf-16")` *succeeds* on it and returns two unrelated
CJK characters — no exception, no warning. That mojibake would then be
segmented, quoted and hashed as though somebody had written it. UTF-16 is now
tried only behind a byte-order mark, which is the only trustworthy evidence of
it. A test pins this.

**Nothing is ever decoded with `errors="replace"` on an input file.** A file no
listed codec reads is an error. The one place replacement is permitted is a
fetched web page, where the content is the *object under analysis* rather than
evidence, and three mojibake characters do not make a page unreadable. A statute
decoded that way would be a different matter, which is why the file reader
refuses instead.

### Why DOCX has no dependency and PDF does

A `.docx` is a zip containing XML. `zipfile` and `xml.etree` are stdlib, the
extraction is one screen, and a reviewer can check it — which matters more here
than in most projects, because a run record's package list is part of its
reproducibility story.

PDF has no honest hundred-line version: its text layer is a positioned glyph
soup with no paragraph structure. So it is an optional extra, and absent the
library a PDF is **refused with the install command** rather than half-read. A
stub extractor returning partial text would produce an analysis reporting that a
document makes no claims — which is not the same finding as looking and finding
none. A scanned PDF with no text layer is refused for the same reason.

## `-u`: structured data, or the whole page and say so

Two strategies, in order:

1. **schema.org JSON-LD** (`Article`, `Comment`, `DiscussionForumPosting`).
   Publishers emit it for search engines, it names the body and each comment's
   author explicitly, and reading it involves no guessing.
2. **The whole page as one post**, with a note saying navigation, boilerplate and
   comments are mixed together and nothing is attributed to a commenter.

There is deliberately **no third strategy** that hunts for comment containers by
class name. Heuristic scraping mis-attributes: it puts a moderator's boilerplate
in someone's mouth, or splits one comment into three. For a tool whose entire
value is that claims trace to what somebody actually wrote, "roughly the right
comments" is not a usable standard.

Redirects are followed to a bounded depth, every hop is recorded, and a
cross-host redirect is called out by name — a page reached at a different site
than the link named is not the page that was asked for.

**A fetched page is the object under analysis and never evidence.** That is
structural, not a rule to remember: evidence enters through a connector that
declares its own tier, and a fetched page enters as the analyzed `DocumentRef`.
There is no sequence of events in which an article's own assertion becomes a
citation supporting itself.

## `-U`: a filter, not a fetcher

`-U @someone` names an account. It does **not** go and get that account's posts.

The only way to do that for a real platform is to scrape it — brittle, generally
against the platform's terms, and it would make this a surveillance tool the
moment it worked. So `-U` takes `--from`: an export the person already has, or a
thread URL. The account is a filter over posts already in hand.

Handle matching is deliberately literal (`@Alice` → `alice`, nothing more). Near
matching would eventually attribute one person's words to another. An unknown
handle is an error, not an empty analysis — empty output would read as "this
account made no claims."

Every `-U` run carries `USER_SCOPE_NOTE` as the first entry in its record:

> CLAIMS, NOT A PERSON. […] There is no aggregate score for the account, no
> ratio, and no characterization of the person: contract §8 refuses those,
> because a per-person truth score is a harassment instrument whatever it is
> called.

Into the record, not only the terminal. A caveat printed to a terminal is gone
the moment somebody shares the JSON, and "we analyzed this account's posts" is
exactly the sentence that needs the rest of itself attached wherever it travels.

`--private` (contract §8) takes a second `--i-know` flag and is recorded in
`RunConfig.private_subjects` rather than merely warned about.

## Clustering: the dangerous optimization

Forty people assert the same thing about the same statute. One at a time that is
forty retrievals, forty adjudications and forty red-team calls to reach one
answer forty times.

The danger is larger than the problem:

> Clustering means **one claim's verdict is published against another claim's
> words.**

Get it wrong and the tool attributes a verdict to something that does not say
what the adjudicated claim said — confidently, with a citation, with nothing on
the surface to show it. So every decision is made in the strict direction.

### Four exact gates, before similarity is measured at all

| Gate | Why |
|---|---|
| Claim type | A legal claim and an empirical one are answered from different sources |
| Ambiguous stance | A sarcastic echo is not the same assertion as a sincere one |
| Numeric anchors | The number is what the verdict turns on |
| Statute citations named | 10 ILCS 5/10-2 and 5/10-3 are different sections |

The anchor gate earns its place immediately. From the demo:

```
MERGE     reworded, same claim          overlap=0.90  prose overlap cleared the bar
SEPARATE  different number (20,000)     overlap=1.00  an exact gate differs
SEPARATE  different section (5/10-3)    overlap=0.90  an exact gate differs
SEPARATE  different claim, same statute overlap=0.35  an exact gate differs
```

**25,000 and 20,000 have identical prose overlap**, because a stemmed token
comparison cannot see the difference between two numbers. Similarity alone would
have merged them and published one's verdict against the other. The anchor gate
runs first for exactly that reason.

### The threshold, and why it is the opposite of the fidelity gate's

`CLUSTER_THRESHOLD = 0.75` against the fidelity gate's `MATCH_THRESHOLD = 0.35`,
using the same tokenizer and the same F1. Opposite problems:

* The fidelity gate compares a **deliberate rewrite** against its source, where
  different words are the entire point. A low bar is correct.
* Clustering compares two claims that are supposed to be **the same claim**.
  Different words are evidence they are not.

### First-fit against a fixed representative, not a centroid

Each claim joins the first existing cluster whose *representative* it matches.
Not what an optimal clusterer would find, and that is fine: what matters is that
the same input always produces the same grouping, and that membership is decided
against **one fixed text**. A moving centroid is how a cluster drifts — each new
member is close to the last one, and the tenth is nothing like the first.

### The grouping is published

`ClusterRef` — which has been in `schema/core.py` since step 1, so **no schema
bump was needed** — ships on every clustered claim with the member count and the
lowest pairwise similarity inside the cluster. Singletons get `None`, so a
`ClusterRef` present in a record *means* something: this verdict was reached on
another claim's text.

`min_similarity` is the true pairwise minimum, computed over every pair.
Representative-to-member distance would be an upper bound — two members can each
be close to the representative and further from each other — and a sampled
number calling itself a minimum would be a small lie in the one field whose job
is letting a reader check the grouping. Above 60 members it falls back to the
bound, and the note says so.

### Every member says it inherited its verdict

```
[This claim was NOT adjudicated on its own text. It was grouped with 3
near-identical claims and carries the verdict reached for c-001, which reads:
"Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to form a new party."
The grouping's weakest pairwise similarity is 0.86 and is published so it can be
checked. Re-run with --no-cluster to adjudicate every claim separately.]
```

A reader must never have to work out from a cluster id that the sources were
consulted for somebody else's sentence. `--no-cluster` is always safe and always
slower.

An inherited claim takes the representative's adjudication, red-team finding
**and** downgrade — all three together. Taking the verdict without the objections
raised against it would publish the confident half of an analysis and drop the
honest half.

## Three fixes that came out of this step

**`HttpTransport` had no public non-JSON GET.** The ILCS connector reached into
`_connect()` and `_headers` directly, which meant the one code path that hits
somebody else's server was the one path bypassing the transport's timeout
handling and stale-keep-alive retry. Now `get_text()` → `TextResponse`, used by
both the connector and the URL reader, and the four live-network tests still
pass against ilga.gov.

**`audit()` conflated problems with caveats.** The semantic digest is the one
recomputation whose shape lives in *code* rather than in the record — it hashes
`AnalysisResult.semantic_projection()`. A future version adding a field to that
projection would recompute a different digest for an untouched record and report
"the record was edited": a false accusation from the exact mechanism whose
purpose is detecting real ones, which is how people learn to ignore an alarm. It
is now skipped, loudly, for a record written under a different schema version,
and `audit_detail()` returns `(problems, caveats)` because they mean opposite
things. Nothing is lost — replay already refuses to cross a schema change, and
every other digest and the whole stage chain still run.

**The acknowledgment flags fired after the health check.** Somebody with no model
running who passed `--no-red-team` was told their backend was unreachable, when
what they actually needed was a second flag. `--no-red-team` and `--private` are
argument errors and now fail before the config is read and before any backend is
built — which also means they cost no hosted API call.

## Where this differs from the spec in `docs/02`

`--reading-level`, `--budget-tokens`, `--budget-usd`, `--format` and `--out`
remain unimplemented; `--consensus` lands in step 8. `-U` takes `--from` rather
than fetching, for the reasons above — that is a deliberate divergence from the
spec's `abca analyze -U @somehandle --since 2025-01-01`, and `--since` is not
implemented either, because there is nothing to filter until a history source
exists.

Cross-post reference resolution is **not** attempted. A reply reading "that's
wrong, it's actually 25,000" is analyzed without the post it answers, so the
relevance gate usually drops it. Stated in `_THREAD_CONTEXT_NOTE`, which every
multi-post run carries, rather than only in this document. Guessing at what a
comment referred to would put words in somebody's mouth and then adjudicate them.

## Try it

```bash
python scripts/demo_step7.py     # no GPU, no network
pytest tests/test_inputs.py tests/test_cluster.py -q
```

## What is left

- **Step 8** — consensus mode, remaining connectors, packaging.
