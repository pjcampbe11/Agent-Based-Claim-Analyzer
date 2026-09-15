# Institutional Context Pack — the second pass of the sourceless module

Component: `context-pack/0.1.0-draft`
Corpus: `corpus/institutional/`
Parent module: `sourceless/0.1.0` (doc 18)
Contract: `sotp/0.1.0-draft`

---

## 1. The distinction this component rests on

There are two kinds of factual content relevant to an unsourced political claim, and the system treats them completely differently.

**Claim-specific facts.** *"H.R. 1234 passed the House 220–210 on March 4."* These are facts **about the claim**. They require retrieval against a live source, they change, and the sourceless module cannot supply them. Asserting one from model knowledge is fabrication under M1.

**Mechanism facts.** *"A motion to recommit is a procedural vote on whether to return a bill to committee. It is not a vote on the underlying policy."* These are facts **about the machinery the claim invokes**. They were true before the post existed, they are true regardless of how the claim resolves, they are anchored in constitutional text, chamber standing rules, statute, or published agency procedure, and they change on the order of years.

The second category is a legitimate supporting dataset. It is what the sourceless module is missing — and it is what makes the difference between a brief that says *"unsupported, go look it up"* and a brief that says *"unsupported, and here is what a motion to recommit actually is, cited, so you can see why the post's framing does not follow."*

That second brief is useful to a reader immediately, without waiting for retrieval, and every word of it is sourced.

---

## 2. What it is

A **pre-built, versioned, hash-pinned corpus** of institutional mechanism entries, each anchored to a T0 or T1 citation. It is not model output. It is a curated data file that ships with the repo and is retrieved from, exactly like any other source connector.

This is the critical property: **the context pack is a source, not a generation.** The model does not write mechanism facts at inference time. It *selects* entries from a corpus whose citations were verified when the entry was written and are re-verified in CI. An entry with a broken or drifted citation fails the build.

If the model were allowed to generate mechanism facts on demand, this component would be a fabrication engine with a confident tone — the single most dangerous thing that could be added to this system. It is a lookup table instead, and the rigidity is the point.

---

## 3. Pipeline position

The sourceless module becomes two passes over the same claim.

```
gate → cluster → [SOURCELESS pass 1: dissect] → [pass 2: context pack] → retrieve → adjudicate
```

**Pass 1 (doc 18)** produces the denatured claim, atomic claims, referent candidates, the distortion, the domain-pattern flags, the argument audit, and the retrieval plan.

**Pass 2 (this component)** reads pass 1's output and selects the mechanism entries the claim actually touches. Selection keys off three fields pass 1 already produces:

- `atomic_claims[].subtype` — `procedural` pulls the procedural-vote entries, `statutory-content` pulls the statute-versus-regulation entries
- `domain_failure_patterns[].pattern` — each pattern maps to the entries that explain why it is a failure
- `referent_candidates[].distortion_applied` — each distortion maps to entries defining both sides of the transformation

Selection is deterministic: a mapping table, not a judgment. Same pass-1 output, same entries, every time.

**Downstream.** Entries enter the run record as `institutional_context[]` with their citations intact. Because they are T0/T1 and genuinely cited, they *are* admissible evidence — but only for what they actually say. A cited entry establishing that a motion to recommit is procedural can support `MISLEADING_CONTEXT` on a claim that framed one as a policy vote. It can never establish what any particular vote was.

---

## 4. Entry format

```jsonc
{
  "id": "proc.mtr",
  "domain": "procedure",
  "term": "motion to recommit",
  "aliases": ["MTR", "motion to recommit with instructions"],

  "statement": "A motion to recommit is a procedural motion to return a bill to committee before final passage. A vote on it is a vote on that return, not on the policy in the bill.",
  "plain_language": "Before the House takes a final vote on a bill, one member can move to send it back to committee. Voting on that motion is voting on whether to send it back. It is not voting for or against what the bill does.",
  "reading_grade": 8.1,

  "citation": {
    "tier": "T0",
    "title": "Rules of the House of Representatives, Rule XIX cl. 2(b)",
    "url": "https://...",
    "quote": "...",
    "retrieved_at": "2026-09-04T00:00:00Z",
    "content_hash": "sha256:..."
  },
  "secondary_citations": [ { "tier": "T1", "title": "CRS R...", "url": "" } ],

  "distinguish_from": ["proc.motion_to_table", "proc.final_passage"],
  "common_distortion": "A vote against an MTR reported as a vote against the policy the MTR's instructions named.",

  "volatility": "low",
  "valid_as_of": "2026-09-04",
  "review_due": "2027-09-04",
  "entry_version": "1.0.0",
  "authored_by": "",
  "verified_by": ""
}
```

Every field is load-bearing. `plain_language` runs through the §5 fidelity gate at authoring time, not at inference — the back-translation diff is part of accepting an entry into the corpus, so the simplified rendering is verified once and then reused forever. That is a large latency win over re-simplifying legal text on every run, and it is a larger correctness win, because a human reviewed the diff.

`distinguish_from` is what makes the pack useful in practice. The most common political misreadings are confusions between two adjacent mechanisms, so entries are written in contrast pairs and the pack surfaces both sides.

---

## 5. Coverage

Initial corpus targets roughly 150–250 entries across six domains. Written in order of how often the mechanism appears in distorted claims, not alphabetically.

| Domain | Contents |
|---|---|
| `procedure` | Motion to recommit, motion to table, cloture, unanimous consent, suspension of the rules, discharge petition, closed/open/structured rules, amendment votes vs. final passage, quorum calls, voice vs. recorded votes, engrossment and enrollment |
| `lawmaking` | Introduced vs. reported vs. passed vs. enacted; one chamber vs. Congress; committee vs. floor action; conference and amendments between houses; presentment, signature, veto, pocket veto, override |
| `budget` | Authorization vs. appropriation; reconciliation and the Byrd rule; CR, omnibus, minibus; CBO and JCT scoring conventions — baseline, window, dynamic vs. static; sequestration; the debt limit as distinct from spending |
| `instruments` | Statute vs. regulation vs. executive order vs. agency guidance vs. court order vs. state law; notice-and-comment rulemaking; the Congressional Review Act; preemption; the difference between an EO's text and its legal effect |
| `elections` | Ballot access thresholds; primary types; the difference between party rules and state law; certification; the campaign committees and what they can and cannot do |
| `statistics` | Level vs. rate; nominal vs. real; seasonal adjustment; the denominators that get swapped; baseline and window selection; how BLS and BEA revisions work |

The `statistics` domain is the one most likely to be underestimated. A large share of viral economic claims are arithmetically true and structurally misleading, and the entry that explains why is more useful than any single data point.

---

## 6. Maintenance and honesty constraints

**Volatility and review.** Every entry carries `volatility` and `review_due`. Chamber rules change at the start of each Congress; an entry citing House rules is flagged for review on the convening date, automatically. An entry past `review_due` is served with a staleness warning attached to the output. An entry whose citation URL 404s or whose content hash drifts **fails CI**, and the build does not pass with a broken entry in the corpus.

**No conclusions.** An entry states what a mechanism *is*. It never states whether using it was good, whether anyone abused it, or what a particular use of it meant. The context pack is data and cannot be imported by `src/` outside its loader — it is checked by the same import-lint as the eval sets, and an entry containing an evaluative clause fails review.

**Symmetry review.** Entries are authored in the abstract, but the *examples* in `common_distortion` are where partisanship would enter. Every entry's example is reviewed against the requirement that the distortion described is one both parties' supporters commit, and where a mechanism is genuinely used more by one side, the entry says so with a T1 citation or says nothing.

**The pack does not adjudicate.** It is a strict addition to the brief, never a substitute for retrieval. A claim with a full context pack and no retrieval is still `UNSUPPORTED`. The pack changes what the reader understands; it does not change what the evidence shows.

---

## 7. Additions to the run record

Appends to the `sourceless_analysis` object in doc 18:

```jsonc
"institutional_context": [
  {
    "entry_id": "proc.mtr",
    "entry_version": "1.0.0",
    "selected_by": "domain_failure_patterns[0]",
    "statement": "",
    "plain_language": "",
    "citation": { "tier": "T0", "title": "", "url": "", "quote": "",
                  "retrieved_at": "", "content_hash": "sha256:..." },
    "staleness": null,
    "relevance": "The post frames the vote as a policy vote; this entry defines what the vote was on."
  }
],
"context_pack_version": "corpus/institutional/0.1.0",
"context_pack_hash": "sha256:..."
```

`context_pack_hash` joins the ledger. A brief citing a mechanism entry is reproducible only against the corpus version that produced it, and `abca verify` diffs the corpus the same way it diffs retrieved sources.

---

## 8. Effect on the brief

The human-readable brief from doc 18 §8 gains one section, placed **before** the referent hypothesis:

> **3. How this actually works** — the selected mechanism entries in plain language, each with its citation.

Placing it before the hypothesis is deliberate. The reader gets the sourced, stable, certain material first, and the speculative material second, visually and structurally subordinate. A brief that leads with "this post probably refers to H.R. 1234" and buries the cited mechanism explanation has inverted its own confidence ordering, and readers will remember the guess.

---

## 9. Evals

Added to `evals/sourceless/`:

- **Selection set** — pass-1 outputs with hand-labeled correct entry sets. Measures precision and recall of entry selection. Recall matters more: a missing entry is a worse brief, an extra entry is noise.
- **Citation integrity** — every entry in the corpus, every build. Fetch the citation, compare the content hash, confirm the quoted passage is present verbatim. Any failure blocks the build. This runs on the corpus, not on model output, and it is the cheapest high-value check in the entire system.
- **Fidelity set** — every `plain_language` field against its `statement` and citation, using the §5 three-pass gate. Run at authoring time and re-run in CI, because an entry's simplification is reused across every run that selects it, so an error there propagates further than any single adjudication error.
- **No-conclusion lint** — evaluative language detection across all entries. Flags modal and normative constructions in `statement` and `common_distortion`.

---

## 10. Why this is the right shape for "supporting factual content"

The instinct behind this component is correct: a brief that can only say *"no source, cannot verify"* is honest and useless, and useless honesty loses to useful spin every time.

The resolution is that the useful content has to come from a **corpus with citations**, not from the model's confidence. Mechanism facts qualify because they are stable, citable, and reusable — which also means they can be written once, verified by a human, hashed, and served at near-zero cost forever.

That is a genuine asset and it compounds. Every entry added makes every future sourceless brief better, and none of it depends on the model knowing anything.
