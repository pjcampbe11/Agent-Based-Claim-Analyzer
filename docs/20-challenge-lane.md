# The Challenge Lane — sourceless intake, adversarial pass, and promotion

Component: `lane-b/0.1.0-draft`
Depends on: `sourceless/0.1.0` (doc 18) · `context-pack/0.1.0` (doc 19)
Contract: `sotp/0.1.0-draft`

---

## 1. Why two lanes

The main pipeline assumes a claim arrives with something to retrieve against. A bare social post does not, and forcing it through anyway produces the worst possible output: a confident-looking `UNSUPPORTED` that took sixty seconds, cost tokens, and told the reader nothing.

So sourceless claims get their own lane, with different economics and a different exit condition.

| | **Lane A — Adjudication** | **Lane B — Challenge** |
|---|---|---|
| Input | Claim with a resolvable external reference | Claim with none |
| Question asked | *Is this supported by the evidence?* | *What is this actually about, and can a source be found?* |
| Latency posture | Interactive. `fast` under 10s | **Batch. Latency is irrelevant.** Runs deep, runs slow, runs on a queue |
| Exits with | A verdict | A **promotion decision** |
| Terminal? | Yes | **No.** A successful exit re-enters Lane A |

The economics matter. Lane A is bound by the requirement that analysis must not hold up the conversation. Lane B has no such constraint — nobody is waiting on it — so it can run `forensic` profiles, consensus across three models, exhaustive retrieval, and multiple reconstruction attempts on a single post. It is the lane where the system is allowed to be expensive and thorough.

```
                 ┌─ has reference ──────────────────────────────► LANE A ─► verdict
   ingest ─► gate┤
                 └─ no reference ─► LANE B ─► challenge ─► promotion gate
                                                                │
                                    ┌───────────────────────────┤
                                    ▼                           ▼
                              PROMOTED ─────────────────► LANE A ─► verdict (dual)
                              UNPROMOTABLE / DORMANT ───► published as-is
```

---

## 2. Lane B stages

**B1 — Intake and queue.** The claim is written to the challenge queue with its post metadata, cluster ID, and a stable `challenge_id`. Deduplicated against the queue by cluster: a narrative circulating on ten thousand accounts is one challenge, not ten thousand.

**B2 — Dissect.** Doc 18, pass 1. Denatured claim, atomization, referent candidates, distortion, domain patterns, argument audit, retrieval plan with branch outcomes.

**B3 — Context pack.** Doc 19, pass 2. Mechanism entries selected and attached.

**B4 — Challenge execution.** *New in this doc, and the reason the lane exists.* The retrieval plan is **run**, not just emitted. Each of the 3–7 queries executes against its named connector, in priority order, until the decisive artifact is found or the plan is exhausted. Because latency is free here, the executor may:

- run all referent candidates in parallel, not just the top one
- widen a failed query along a bounded set of axes (adjacent Congress, adjacent chamber, adjacent date window) and record every widening
- run consensus reconstruction — the dissect pass on N models — and take the intersection of referent candidates, which is a substantially stronger signal than any single model's top pick

**B5 — Promotion gate.** §3. The decision point.

**B6 — Disposition.** Promote, hold, or publish as unpromotable.

---

## 3. The promotion gate

Promotion means: *a source now exists for this claim, so it can be adjudicated properly.* Four conditions, all required.

**P1 — An artifact was retrieved.** A real document at a real URL, T0–T2, with a content hash and retrieval timestamp. Not a search result. Not a summary. The document.

**P2 — The artifact satisfies a pre-registered branch outcome.** The `branch_outcomes[]` written in B2 stated, *before* retrieval ran, what each possible result would mean. The retrieved artifact must match one of them. If it matches nothing that was pre-registered, the gate fails — the module found something, but not something it predicted, which means the reconstruction was wrong even if the document is interesting.

This is the anti-rationalization control. Without it, the executor searches until it finds something plausible and then declares that to be the referent.

**P3 — The referent link is confirmed, not assumed.** The artifact must contain identifying particulars that tie it to the post: matching actor, date window, jurisdiction, and subject matter. Confirmation is recorded as a list of matched particulars, each with the quoted passage. A topical match is not a confirmation. Three of four particulars matching is `PROMOTED_WEAK` and is labeled as such the whole way through.

**P4 — The claim survived unmodified.** §4. The non-negotiable one.

If P1–P4 hold, the claim is promoted. Otherwise:

| Outcome | Meaning | What happens |
|---|---|---|
| `PROMOTED` | All four hold | Enters Lane A with provenance attached |
| `PROMOTED_WEAK` | P3 partial | Enters Lane A; confidence capped, weakness surfaced in the report |
| `DORMANT` | No artifact yet, but one plausibly will exist — pending bill, scheduled vote, docketed case, unreleased report | Held in queue with a re-check trigger and date |
| `UNPROMOTABLE` | The plan was exhausted; no artifact, no plausible future one | Published as `UNSUPPORTED` with the full challenge brief |
| `REJECTED` | The claim is unfalsifiable, normative, or M6-halted | Published as `UNVERIFIABLE` / `OUT_OF_SCOPE` |

`DORMANT` is the quietly valuable state. A large share of viral claims are about things that have not happened yet — a bill that might pass, a rule that might be finalized. The honest answer today is "no source exists," and the honest answer in six weeks may be different. The queue re-checks on the trigger date and promotes if the artifact appears, which produces a genuinely useful artifact: **a claim that was unsupported when posted and became checkable later, with both timestamps published.**

---

## 4. What gets promoted — the substitution rule

**The claim that enters Lane A is the denatured claim from B2, character for character. Never the reconstruction.**

This is the single most important rule in the lane, and it is enforced in schema by hashing the denatured claim at B2 and refusing promotion if the hash differs at B5.

The failure it prevents: the post says *"Congress just voted to gut veterans' benefits."* Reconstruction identifies a motion to recommit on an appropriations vehicle containing a VA line item. Retrieval pulls that roll call. If the promoted claim becomes *"the House rejected a motion to recommit H.R. ____ on [date],"* the analyzer will adjudicate **that** — and return `SUPPORTED`, correctly, about a sentence nobody posted. The reader sees a green check next to a claim they never made and a verdict that has nothing to do with what they read.

So the retrieved artifact becomes **evidence**, and the original sentence stays the **claim**. That is the ordinary relationship between a source and an assertion, and the lane exists to establish it, not to bypass it.

The reconstruction survives as provenance, never as subject:

```jsonc
"promoted_from": {
  "challenge_id": "ch-01J8...",
  "lane": "B",
  "denatured_claim_hash": "sha256:...",
  "referent_candidate_used": "",
  "distortion_applied": "committee vote -> floor vote",
  "referent_confidence": "medium",
  "confirmation_particulars": [
    { "particular": "actor", "matched": true, "quote": "" },
    { "particular": "date_window", "matched": true, "quote": "" },
    { "particular": "jurisdiction", "matched": true, "quote": "" },
    { "particular": "subject_matter", "matched": false, "note": "" }
  ],
  "branch_outcome_matched": "",
  "consensus_models": ["", "", ""],
  "queries_executed": 11,
  "widenings": [],
  "promotion_grade": "PROMOTED_WEAK"
}
```

---

## 5. Dual verdict on promotion

A promoted claim produces **two** verdicts, and separating them is what makes the whole lane worth building.

1. **Verdict on the referent** — what the retrieved document actually establishes. *The House rejected a motion to recommit on this date, 210–220.* Usually `SUPPORTED`, often uninteresting.
2. **Verdict on the post's characterization** — whether the post's sentence is a fair rendering of that document. *The post described a procedural motion as a vote to eliminate benefits.* This is where `MISLEADING_CONTEXT` lives, and it is now reachable, because a source exists to be mischaracterized.

The headline verdict is the second one, because the second one is what the reader asked about. The first ships as its supporting evidence.

This is also the answer to a gap in the base contract. `MISLEADING_CONTEXT` — "component facts check out; the framing materially distorts them" — is unreachable on a sourceless claim, because you cannot show a distortion without the thing that was distorted. The challenge lane's entire function is to go find that thing. **Promotion is what converts an `UNSUPPORTED` into a `MISLEADING_CONTEXT`,** and `MISLEADING_CONTEXT` is the verdict that describes most viral political content accurately.

The context pack (doc 19) does the explanatory work in the same report: the mechanism entry defines what a motion to recommit is, the retrieved roll call shows which one occurred, and the characterization verdict states the gap between them. All three are cited.

---

## 6. Guards

**No promotion cascade.** A promoted claim runs Lane A once and exits with a verdict. It cannot re-enter Lane B, cannot spawn a second challenge, and cannot be re-promoted. Enforced by `promoted_from` being present: its presence bars Lane B intake.

**No self-citation.** Nothing produced by Lane B is admissible as evidence in Lane A except (a) retrieved artifacts, which are ordinary sources, and (b) context-pack entries, which are ordinary corpus sources. The dissect output, the referent candidates, and the argument audit are never evidence. Enforced by the same schema rule as doc 18: citations appear only where citations are allowed.

**Provenance is never dropped.** Any published verdict on a promoted claim states, in the reader's language, that **the post cited nothing and the source was located by reconstruction.** A verdict that presents a reconstructed source as though the post had provided it is a misrepresentation, and it is the kind that would be found and used against whoever published it. The report line is fixed text, not model-composed.

**Widening is bounded and logged.** Query widening runs on a fixed axis set (Congress ±1, chamber, date window ±90 days, adjacent bill numbers) with a hard cap. Every widening is recorded. Unbounded search is how an executor eventually finds *something* for any claim whatsoever.

**Budget.** Lane B is batch, not free. Per-challenge token and wall-clock caps, with `UNPROMOTABLE` on exhaustion. A challenge that hits the cap is published as unpromotable with the cap noted, never left ambiguous.

---

## 7. CLI surface

Extends doc 02 §2.

```
abca challenge [INPUT] [OPTIONS]      # force a claim into Lane B
abca queue <list|show|drain|purge>    # inspect and run the challenge queue
abca queue drain --workers 4          # batch execution
abca promote <CHALLENGE_ID>           # manual promotion; requires --i-know, always logged
abca dormant <list|recheck>           # re-check dormant challenges whose triggers fired
```

New options on `analyze`:

```
--lane auto|a|b            default: auto (gate decides)
--no-challenge             sourceless claims exit UNSUPPORTED without entering Lane B
--challenge-budget-tokens N
--promote-threshold strict|weak    default: strict (P3 requires 4/4 particulars)
```

Sensible defaults: `analyze` on an interactive `fast` run does **not** block on Lane B. It returns `UNSUPPORTED` immediately with `challenge_id` attached and queues the challenge. The user gets an answer in under ten seconds and a better answer later. That is the right shape for the stated latency requirement — the conversation is not held up, and the work still happens.

`abca promote` exists for the case where a human finds the source the executor missed. It requires `--i-know`, records the operator, and marks the run `human_promoted: true`. It never bypasses P4 — a human cannot substitute the claim either.

---

## 8. Published record

Challenges and their outcomes are published, not just verdicts. A record that only shows what was settled is not a record, and the unpromotable ones matter most.

Per challenge: the denatured claim, the disposition, the referent candidates considered *and rejected*, the queries executed, the promotion grade, and the resulting verdict if promoted. A public record of *"we tried to find a source for this, here is exactly what we searched, we did not find one"* is a stronger and more falsifiable artifact than a verdict, because anyone can run the same queries and check.

Dormant claims are published with their re-check dates, so the record shows what the analysis is waiting on.

---

## 9. Evals

`evals/challenge/`. No merge on regression.

- **Promotion precision** — sourceless posts with a hand-verified true referent. Measures how often promotion lands on the *correct* document. A wrong promotion is far worse than a missed one, because it produces a confident verdict about the wrong artifact. Weighted accordingly.
- **Substitution set** — the P4 test. Posts where the reconstruction is materially different in wording from the original. Any run whose Lane A input hash differs from the B2 denatured claim hash is a **hard failure**, no exceptions.
- **Restraint set** — posts with no true referent, mixed with posts whose referent is real but obscure. The executor must return `UNPROMOTABLE` on the first group. A gate that promotes everything given enough widening fails.
- **Dual-verdict set** — promoted claims where the referent is `SUPPORTED` and the characterization is `MISLEADING_CONTEXT`. Confirms the two verdicts are actually separated and that the headline is the characterization.
- **Promotion symmetry** — the partisanship test for this lane, and the one most likely to fail silently. Matched sourceless post pairs, identical in structure, opposite in valence. **Promotion rates must be statistically indistinguishable across the pair set**, as must widening counts and average queries executed.

That last one deserves stating plainly: if one side's claims get promoted more often, one side's claims get adjudicated more often, and the published record shows asymmetric scrutiny even though every individual verdict was correct. Differential effort is differential treatment. It would be invisible in any per-claim review and obvious in aggregate to a hostile reader, so the eval is aggregate.

---

## 10. Open questions

1. **Dormant re-check triggers.** Currently a date. Better would be event subscriptions — congress.gov bill status change, Federal Register final-rule publication, docket entry. More useful, more plumbing.
2. **Consensus reconstruction cost.** Intersecting referent candidates across three models is a strong signal and triples B2 cost. Free in batch, but it caps queue throughput. Needs a measurement before it becomes the default.
3. **`PROMOTED_WEAK` display.** A 3-of-4 particular match still produces a real verdict on a real document. Whether that should be published alongside strict promotions or held in a separate tier is unresolved, and it is a credibility question more than a technical one.
