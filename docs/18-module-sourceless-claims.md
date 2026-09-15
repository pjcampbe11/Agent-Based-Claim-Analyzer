# Module — Sourceless Political Claim Dissection

Prompt artifact: `prompts/sourceless/0.1.0/dissect.md`
Module version: `sourceless/0.1.0-draft`
Contract: `sotp/0.1.0-draft`
Companions: doc 19 (institutional context pack) · doc 20 (challenge lane)

---

## 1. What this module is, and what it is not

**It is** a referent-reconstruction and triage stage that runs when a political claim arrives with no link, citation, or named source — a bare Facebook, Instagram, or X post. It converts an unsourced assertion into a **precise, executable retrieval target** plus a structural analysis of the claim's logic.

**It is not** a source. It produces **no evidence**. Nothing it emits may raise a claim's `evidence_quality` above `T4`, and nothing it emits may be cited in `claims[].citations`.

This distinction is the whole design. A bare social post is `T4` under contract §2, and `T4` can never raise a verdict above `UNSUPPORTED`. A module that adjudicated sourceless claims from model knowledge would be exactly the failure R1 and R4 exist to prevent: fluency substituting for a citation. So this module does the one thing that *can* be done without a source — reason about structure, logic, and plausible referents — and hands the result to `retrieve`.

### Why the module exists

Without it, a sourceless post reaches `retrieve` with nothing to retrieve. The stage has no bill number, no date, no chamber, no agency — just a distorted paraphrase. Retrieval either fails or returns noise, and the claim exits as `UNSUPPORTED` with no path forward. That is technically correct and operationally useless.

The insight the module runs on: **a viral political claim almost always has a real-world seed.** A procedural vote became a substantive one, a committee vote became a floor vote, a bill introduced became a bill passed. Naming the most plausible seed and the transformation applied to it turns an unretrievable claim into a retrievable one. That is a *hypothesis*, labeled as such, and it is checked by the normal retrieval and adjudication stages like anything else.

---

## 2. Scope: text only

**The module operates on text. Images are out of scope, in this version and by decision, not by omission.**

A post carrying a screenshot, photo, or infographic is processed as **its text content alone**. No OCR, no image provenance extraction, no reverse image search. If the post's only assertion lives inside an image, the claim does not enter the pipeline: the gate returns `OUT_OF_SCOPE` with reason `image_only_content`.

Consequences, stated plainly so nobody rediscovers them later:

- A screenshot of a real article is treated as sourceless even though a source exists in the image. The module will attempt reconstruction from the surrounding text and will often fail. That failure is honest and is published as `UNPROMOTABLE`.
- Fabricated screenshots — a real outlet's chrome around invented text — are **not detectable by this system**. The domain audit's "simulated screenshot laundered as reporting" pattern can fire on textual cues only.
- Any published output on a post containing an image carries a fixed note that the image was not analyzed.

The reason for the boundary is cost and correctness, not difficulty. Image handling brings OCR error, provenance heuristics, and reverse-image lookup — three subsystems whose failure modes are probabilistic and hard to cite. A pipeline whose entire credibility argument is hash-pinned reproducibility should not take on three probabilistic dependencies before the text path is proven. Revisit after the text path has a published track record.

---

## 3. Pipeline position

```
ingest → segment → classify → gate → cluster → [SOURCELESS] → retrieve → adjudicate
                                                                            ↓
                                        compose ← red-team ← fidelity (legal only)
```

**Trigger condition.** The module runs on a claim when all hold:

- The claim survived `gate` (political, in scope, and not `image_only_content`).
- Its `type` is verdict-eligible (`LEGAL`, `EMPIRICAL`, `ATTRIBUTIVE`). Normative, predictive, and definitional claims route to contract §3 decomposition as usual and skip this module.
- The source text carries no resolvable external reference: no URL, no citation string, no named document, no quoted publication.

**Output routing.** The module emits a `sourceless_analysis` object attached to the claim. Two fields are consumed downstream:

- `retrieval_plan.queries[]` and `retrieval_plan.primary_sources[]` feed `retrieve` as a **priority queue**, ahead of the type-routed default lookup.
- `structural_conflict[]` entries, if any carry a T0 citation, enter `adjudicate` as ordinary T0 evidence.

Everything else is reporting. It reaches `compose` and the reader; it never reaches the evidence path.

**Cost.** The module runs on cluster representatives only, never on every member. On `--profile fast` it is skipped and the claim is queued to the challenge lane (doc 20) instead, so the interactive path stays under its latency budget.

---

## 4. Role

The prompt fuses two specialists. Both are instructed capabilities, not claims of credential.

### (A) Congressional and U.S. political systems

Working command of: the legislative process in both chambers; House and Senate standing rules and their practical exploitation; committee and subcommittee jurisdiction; markup, discharge petitions, suspension of the rules, unanimous consent, and open/closed/structured rules from the Rules Committee; cloture, the filibuster, budget reconciliation and the Byrd rule; appropriations versus authorization; continuing resolutions, omnibus and minibus packages; CBO and JCT scoring conventions; the Congressional Review Act; motions to recommit, motions to table, and the other procedural votes routinely mischaracterized as substantive ones.

How the parties operate as institutions: conference versus caucus, leadership and whip structures, committee assignment politics, the campaign arms (NRCC/DCCC/NRSC/DSCC), ideological blocs and their internal incentives, primary dynamics, and the difference between a messaging bill and a bill with a path.

The boundaries between statute, regulation, executive order, agency guidance, court order, and state law — **because most viral political claims fail precisely at those boundaries.**

### (B) Argumentation and rhetoric

Informal logic, dialectic, and pragmatics. Decomposition of a statement into propositional content, implicature, enthymemes (the unstated premises doing the real work), and framing. Fallacies named precisely rather than by nearest available label.

### Non-partisan by construction

Identical scrutiny regardless of who the claim helps or hurts. No policy preference is volunteered, ever. If a claim is well-constructed but wrong, the construction is respected and the break located precisely. If a claim is likely accurate but rhetorically manipulative, both are stated. The module is covered by the symmetry eval (§10) and fails on asymmetry.

---

## 5. Hard rules

These extend the contract's R1–R4; they do not replace them.

**M1 — Never fabricate a citation.** No invented bill numbers, roll call numbers, vote tallies, dates, page numbers, or quotes. Unknown items are marked `UNVERIFIED` and moved to the retrieval plan. This is R1 restated because it is the rule this module is most tempted to break.

**M2 — Three registers, always separated and always labeled.** What the post asserts (`ASSERTED`), what is structurally established with a citation (`ESTABLISHED`), and what the module is inferring (`INFERRED`). Every factual line carries one of the three. An unlabeled line is a schema violation.

**M3 — Bill numbers are ambiguous across Congresses.** Never assert one without the Congress number. Reconstructed identifiers are `referent_candidates`, never findings, and always `INFERRED`.

**M4 — Unsourced is not false.** Absence of a source lowers *confidence*, not truth value. The module states this explicitly in every output rather than sliding into a negative verdict. `UNSUPPORTED` is never rounded toward `CONTRADICTED`.

**M5 — No counter-spin.** A claim's spin is not corrected with opposing spin. Neutral, load-free language only. The module does not produce rebuttals; contract §8 puts persuasive content out of scope, and that applies here in full.

**M6 — Private individuals.** If the claim targets a private individual or concerns a person's private life, the module halts and flags rather than building a dossier. Public officials acting in official capacity are in scope. Inherits `--private` and its warning.

**M7 — Falsifiable versus unfalsifiable.** Motive attributions ("they did it because they hate X") are classified `unfalsifiable` and are **not** fact-checked. They route to `UNVERIFIABLE`.

**M8 — No actor attribution.** Contract §6 governs unchanged. Patterns may be named with a T1 citation to the finding that documents them; states, services, and individuals may not be named as sources of a post.

**M9 — No person-level aggregation.** Fallacy and rhetorical-device findings attach to claim spans. They are never summed per author, per handle, or per account, and never rendered as a score. Contract §8.

**M10 — Text only.** §2. No assertion may rest on image content, and no output may imply an image was analyzed.

---

## 6. Stages

### Stage 0 — Intake

Restate the post's core assertion in one neutral sentence, stripped of adjectives, intensifiers, and framing. This is the **denatured claim**. Everything downstream operates on it, not on the post's wording. Its hash is recorded here and is load-bearing for promotion (doc 20 §4).

Record: implied timeframe, implied actor(s), implied jurisdiction, implied source-of-authority.

### Stage 1 — Atomization

Split into atomic claims — the smallest units that can independently hold or fail. Most viral posts bundle two to five, **where one is true and carries the others.** Identifying the carrier is a primary output.

Per atom:

| Field | Values |
|---|---|
| `type` | Canonical: `LEGAL` · `EMPIRICAL` · `ATTRIBUTIVE` · `PREDICTIVE` · `NORMATIVE` · `DEFINITIONAL` |
| `subtype` | `factual-event` · `roll-call` · `statutory-content` · `procedural` · `quote` · `causal` · `statistical` · `motive` |
| `verifiability` | `verifiable-primary` (T0–T1) · `verifiable-secondary` (T2–T3) · `contested` · `unfalsifiable` |
| `load_bearing` | Does the post's rhetorical force collapse if this atom fails? |

`type` uses the contract §3 taxonomy so the object merges cleanly. `subtype` carries the procedural granularity this module needs and the base taxonomy does not.

### Stage 2 — Referent reconstruction

**The core function.** Four questions, in order.

**2a. What is the most plausible real referent?** Up to three candidates — an actual event, bill, vote, hearing, ruling, or statement — each with reasoning and a confidence band. All `INFERRED`.

**2b. What is the smallest distortion that turns that event into this post?** Name the transformation explicitly:

procedural vote → substantive vote · committee vote → floor vote · amendment vote → final passage · introduced → passed · passed one chamber → "Congress" · proposed rule → enacted rule · executive order → statute · state law → federal law · draft text → final text · bill title → bill text

The smallest sufficient distortion is the most likely one. A claim requiring three independent distortions to reach a plausible seed probably has a different seed.

**2c. What would have to be true for the claim as written to be accurate?** State the required conditions, then assess each against known institutional constraints — "this would require unanimous consent in the Senate on a partisan measure," "this would require the agency to act outside its authorizing statute," "this would require the chamber to have been in session on a date it was not."

**2d. Structural conflict check.** Does the claim conflict with a rule of the system? Timeline impossible, chamber lacks the power, office has no such authority, the procedure does not exist, jurisdiction mismatch, body not in session.

This is the one finding the module can reach without external evidence — and only **partly**. Two outcomes, and the distinction is not negotiable:

- **`structural_conflict`** — anchored to a **T0 citation** (constitutional text, chamber standing rules, the authorizing statute, the enacted appropriation). Admissible evidence; can carry `CONTRADICTED` into `adjudicate`.
- **`structural_flag`** — asserted from model knowledge with no citation. `INFERRED`, **not** evidence, carries no verdict, and becomes a high-priority retrieval target: the retrieval plan must name the document that would establish it.

An uncited "the Senate cannot do that" is an unsourced claim about the law, which is the category of thing this system exists to catch. It does not get an exemption for being ours.

### Stage 3 — Domain audit

Run the claim against the known failure patterns. Flag every one that fires, quote the triggering span, explain how it applies.

- Bill **title** cited instead of bill **text**
- "Voted against X" derived from a vote on an omnibus or procedural vehicle containing X
- Motion to recommit or motion to table framed as a vote on the underlying policy
- Amendment vote framed as final passage
- Introduced or co-sponsored framed as passed or enacted
- One chamber framed as "Congress"
- Committee action framed as chamber action
- CBO score quoted without baseline, window, or assumptions
- Statistic with cherry-picked start date, wrong denominator, level-versus-rate confusion, or nominal-versus-real dollars
- Executive order, agency guidance, or court ruling framed as legislation
- State action framed as federal, or the reverse
- Quote accurate but stripped of immediate context or of the speaker's role
- Old event recirculated as current
- Satire or parody laundered as reporting; a simulated screenshot described in text (the image itself is not analyzed — §2)
- Anonymous "sources say" chain with no terminal origin

A firing pattern is `INFERRED` unless the corrected version is anchored to a citation. It sharpens the retrieval plan; it does not settle the claim.

### Stage 4 — Argument audit

Reconstruct the argument in standard form: stated premises, **unstated premises**, conclusion. The unstated premise is usually where the failure lives, and surfacing it is the point of the stage.

Then audit:

- **Relevance** — ad hominem (abusive, circumstantial, tu quoque), appeal to authority / popularity / emotion / fear, genetic fallacy, red herring, whataboutism
- **Structural** — straw man, hollow man, weak man, false dilemma, slippery slope, non sequitur, affirming the consequent, denying the antecedent
- **Causal** — post hoc, cum hoc, single cause, reversed causation, ignored confounding
- **Definitional and linguistic** — equivocation, amphiboly, motte-and-bailey, no true Scotsman, loaded question, persuasive definition
- **Evidential** — hasty generalization, anecdote as data, cherry-picking, survivorship bias, base rate neglect, unfalsifiable framing, shifted burden of proof, argument from ignorance
- **Rhetorical devices** (noted, not scored as fallacies) — presupposition smuggled into phrasing, scare quotes, agentless passive, nominalization hiding the actor, quantifier vagueness ("many," "some say"), false balance, innuendo by juxtaposition

Per finding: name it, quote the exact triggering span, explain the mechanism in one sentence, rate severity `minor` | `material` | `disqualifying`.

**Constraint.** A fallacy finding is a property of the *argument*, never of the *arguer*, and never of the claim's truth value. A claim can be true and fallaciously argued. The audit never contributes to the disposition — it ships alongside it. Enforced in schema: `fallacies[]` has no path to `disposition`.

### Stage 5 — Disposition

The module does **not** issue a verdict. It issues a **disposition**, drawn from the contract §4 vocabulary, that `adjudicate` may revise once retrieval runs.

| Disposition | When |
|---|---|
| `UNSUPPORTED` | The default and the overwhelmingly common outcome. No qualifying evidence reached. A referent may be identified; identification is not support. |
| `UNVERIFIABLE` | Normative, predictive, definitional, or motive-attributive. Not the kind of claim evidence settles. |
| `CONTRADICTED` | **Only** when a `structural_conflict` carries a T0 citation that directly contradicts the claim as stated. |
| `OUT_OF_SCOPE` | Not a political claim, an M6 halt, or `image_only_content`. |

`MIXED`, `MISLEADING_CONTEXT`, `REPORTED_UNVERIFIED`, and `SUPPORTED` are **unreachable from this module.** They require evidence the module by definition does not have. `adjudicate` assigns them after retrieval — and doc 20 §5 describes how promotion is what makes `MISLEADING_CONTEXT` reachable at all.

Attached to the disposition:

- `evidence_quality` — `T4` unless a `structural_conflict` was cited, in which case `T0`
- `confidence` — 0.0–1.0; on an `UNSUPPORTED` disposition it describes confidence *in the disposition*, not in the claim
- `referent_confidence` — separate band for the Stage 2 hypothesis: `high` (structural reasoning settles the referent), `medium` (strong pattern match, one confirmation needed), `low` (plausible referent, multiple readings survive), `none` (no defensible referent)

The two confidences are separate fields on purpose. The module can be highly confident a claim is unsupported and have no idea what it refers to; the opposite also happens. Collapsing them is how a hypothesis gets read as a finding.

### Stage 6 — Retrieval plan

The executable output. Consumed by `retrieve` in Lane A, and **executed** by the challenge lane in Lane B (doc 20 §2, B4).

- **Primary sources in priority order** — congress.gov bill text and actions, House and Senate roll call records, the Congressional Record, committee reports, the Federal Register, GAO/CBO/CRS, agency releases, court opinions, official transcripts. Each maps to a contract §5 connector by name.
- **Three to seven literal queries**, written to run as-is against the named connector. Not descriptions of searches — the search strings.
- **The decisive artifact** — the single document that would resolve the claim outright. If there isn't one, say so; that is itself a finding.
- **Branch outcomes** — for each plausible retrieval result, the disposition it would produce. Written before retrieval runs, so the module cannot fit its conclusion to what comes back.

Branch outcomes are the module's own commitment device, and doc 20's promotion gate (P2) enforces them: an artifact that matches no pre-registered branch cannot promote a claim.

---

## 7. Red-team interaction

The mandatory red-team pass (contract §7) applies with one addition specific to this module:

**The red-team must attack the referent hypothesis.** Given `referent_candidates`, it produces the strongest case that the top candidate is the *wrong* seed, and names at least one alternative reading that survives the module's reasoning. If the alternative is not clearly weaker, `referent_confidence` is downgraded automatically and the downgrade is recorded.

This matters more here than anywhere else in the pipeline. Referent reconstruction is a plausibility judgment, and plausibility judgments are where a partisan thumb lands without anyone noticing — the "obvious" seed for a claim tends to be the one that makes the claim's target look the way the reader expects. The red-team pass on Stage 2 is the control for that, and it is not optional.

---

## 8. Output object

Nests as `sourceless_analysis` on the claim object in the contract §6 run record. It does not define a parallel schema.

```jsonc
{
  "module_version": "sourceless/0.1.0",
  "prompt_hash": "sha256:...",
  "triggered_by": "no_external_reference",
  "media_present": false,
  "media_analyzed": false,

  "denatured_claim": "",
  "denatured_claim_hash": "sha256:...",
  "post_metadata": { "platform": "", "date": "", "parent_context": "" },

  "atomic_claims": [
    { "id": "", "text": "", "type": "LEGAL",
      "subtype": "procedural", "verifiability": "verifiable-primary",
      "load_bearing": true, "register": "ASSERTED" }
  ],

  "referent_candidates": [
    { "candidate": "", "referent_confidence": "medium",
      "distortion_applied": "committee vote -> floor vote",
      "reasoning": "", "register": "INFERRED" }
  ],

  "conditions_required_for_truth": [
    { "condition": "", "institutional_plausibility": "", "register": "INFERRED" }
  ],

  "structural_conflict": [
    { "conflict": "", "citation": { "tier": "T0", "title": "", "url": "",
      "quote": "", "retrieved_at": "", "content_hash": "sha256:..." },
      "register": "ESTABLISHED" }
  ],
  "structural_flag": [
    { "flag": "", "would_be_established_by": "", "register": "INFERRED" }
  ],

  "domain_failure_patterns": [ { "pattern": "", "span": "", "explanation": "" } ],

  "argument_reconstruction": {
    "stated_premises": [], "unstated_premises": [], "conclusion": ""
  },
  "fallacies": [ { "name": "", "span": "", "mechanism": "", "severity": "material" } ],
  "rhetorical_devices": [ { "device": "", "span": "" } ],

  "disposition": "UNSUPPORTED",
  "evidence_quality": "T4",
  "confidence": 0.0,
  "confidence_rationale": "",
  "referent_confidence": "medium",
  "unverified_items": [],

  "retrieval_plan": {
    "primary_sources": [ { "connector": "congress", "target": "", "priority": 1 } ],
    "queries": [],
    "decisive_artifact": "",
    "branch_outcomes": [ { "if_found": "", "then_disposition": "" } ]
  },

  "red_team": {
    "alternative_referent": "",
    "case_against_top_candidate": "",
    "referent_confidence_downgraded_from": null
  },

  "challenge_id": null,
  "handoff_notes": ""
}
```

**Schema constraints enforced in `src/abca/schema/`:**

- `disposition` ∈ {`UNSUPPORTED`, `UNVERIFIABLE`, `CONTRADICTED`, `OUT_OF_SCOPE`}
- `disposition == "CONTRADICTED"` requires a non-empty `structural_conflict[]` with a valid T0 citation
- `evidence_quality == "T4"` unless the above holds
- every array element carrying a factual assertion has a `register` field
- `citations` appear only inside `structural_conflict[]`; nowhere else in the object
- `media_analyzed` is always `false` in this version and is asserted, not omitted

---

## 9. Human-readable brief

Composed at the configured reading level. Order is fixed:

1. **The denatured claim** — one sentence.
2. **Disposition and what it means in plain words** — including, explicitly, *"no source was attached; this lowers confidence, it does not make the claim false."*
3. **How this actually works** — the institutional context-pack entries (doc 19), in plain language, each cited. Placed here, before the hypothesis, so the sourced material leads and the speculative material is subordinate.
4. **Most likely referent and the distortion** — flagged as a hypothesis in the reader's language, not in schema language.
5. **What would settle it** — the decisive artifact, named so a reader can pull it themselves. This is the seal motto rendered operationally.
6. **Argument structure**, if the audit found anything material.
7. **Red-team**, if it downgraded anything.

If the post contained an image, a fixed line states that the image was not analyzed.

Tone: clinical. No hedging theater, no both-sides padding, no moralizing about misinformation. Short sentences. If a claim is fine, one line and move on. If the post is well-constructed but wrong, respect the construction and locate the break precisely.

---

## 10. Evals

`evals/sourceless/`. No merge on regression.

- **Referent set** — sourceless posts with a hand-verified true seed. Measures top-1 and top-3 referent accuracy and, separately, whether the named distortion is correct. A module that finds the right bill for the wrong reason is a failure.
- **Restraint set** — sourceless posts with *no* recoverable referent. The module must return `referent_confidence: none` and say so. **A module that always produces a confident referent fails this suite**, and this is the suite most likely to catch overreach.
- **Structural set** — claims that genuinely conflict with a citable rule, mixed with claims that merely *feel* impossible. The first must produce `structural_conflict` with a real T0 citation; the second must produce at most `structural_flag`. Any uncited conflict promoted to `CONTRADICTED` is a hard failure.
- **Symmetry set** — sourceless post pairs identical in structure, opposite in valence. Three distributions must be statistically indistinguishable across the pair set: dispositions, `referent_confidence`, and **fallacy counts by severity**. The third is specific to this module: rhetorical auditing is the easiest place in the system for asymmetry to hide, because the labels are qualitative and feel objective.
- **Fabrication set** — claims that invite an invented bill number, roll call, or tally. Any fabricated identifier is a hard failure regardless of how the rest of the output scores.

---

## 11. Open questions

1. **Referent search grounding.** Stage 2 currently reasons from model knowledge. A cheap grounding pass — BM25 over a local bill-and-roll-call index before generating candidates — would raise accuracy substantially and cut fabrication risk. It also adds latency to a stage that runs before retrieval. Probably worth it at `standard` and above; unresolved. Note that in Lane B latency is free, so this may be a lane-dependent setting rather than a global one.
2. **~~Screenshot provenance.~~** **Decided 2026-09-04: text only.** See §2. Image handling is out of scope; posts whose only assertion is in an image are gated `OUT_OF_SCOPE`. Revisit only after the text path has a published track record.
3. **Distortion taxonomy as data.** The Stage 2b transformation list is currently prose in the prompt. It should be a versioned data file so distortions can be counted across runs and the pipeline can report which transformations are most common — a genuinely publishable finding.
