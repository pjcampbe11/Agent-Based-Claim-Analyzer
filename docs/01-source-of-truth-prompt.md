# The Source-of-Truth Prompt Contract

Version: `sotp/0.1.0-draft`

This is the specification the prompt implements. The prompt itself is a versioned artifact under `prompts/`; this document is what it must satisfy. Changing the contract requires a version bump and a re-run of the golden eval suite.

---

## 1. Design constraints

The prompt is **rigid by construction**. It is not a personality, a stance, or a set of talking points. It is a procedure with defined inputs, a defined vocabulary, and defined refusals.

Four hard rules govern everything below.

**R1 — Evidence gates verdicts.** A claim's verdict is capped by the best source tier reached. No amount of model confidence substitutes for a citation.

**R2 — Claim type gates verdict eligibility.** Normative and predictive claims are not adjudicated as true or false. Ever. The tool decomposes them and checks what's underneath.

**R3 — Every adjudication is attacked before it ships.** A red-team pass that finds the strongest counter-evidence is mandatory, not a flag.

**R4 — Silence beats invention.** `UNSUPPORTED` and `UNVERIFIABLE` are correct, expected, frequent outputs. A prompt that never returns them is broken.

---

## 2. Source tiers

Sources are ranked. The tier of the best supporting source is recorded on every claim as `evidence_quality`.

| Tier | Class | Examples | Admissible for |
|---|---|---|---|
| **T0** | Primary legal text | U.S. Constitution; U.S. Code; CFR; Federal Register; enacted state statute (e.g. ILCS); slip opinions and published court decisions; ratified treaty text; the text of a bill as introduced/enrolled | Legal claims — authoritative |
| **T1** | Official record & official data | CBO, GAO, CRS; BLS, BEA, Census, FRED; agency Inspectors General; court dockets (CourtListener/PACER); FEC filings; SEC EDGAR; ODNI and DOJ public reports; state SOS / boards of elections | Legal and empirical claims — authoritative |
| **T2** | Peer-reviewed / method-transparent research | Journal articles with stated methods; NBER working papers; studies with available replication data | Empirical claims — strong |
| **T3** | Reporting with a corrections policy | Wire services and outlets that publish corrections | **Only** that an event occurred, or that a named person said a thing. Never causation, never interpretation. |
| **T4** | Everything else | Advocacy orgs, think tanks, op-eds, blogs, social posts, press releases | **Never evidence.** Admissible only as the *object* under analysis. |

### Tier rules

- `SUPPORTED` or `CONTRADICTED` requires **at least one T0–T2** citation.
- T3 alone caps the verdict at `REPORTED_UNVERIFIED`.
- T4 can never raise a verdict above `UNSUPPORTED`.
- A think tank's *underlying dataset* may be T2 if methods and data are published. The think tank's *conclusion* remains T4. These are scored separately.
- Where a T0 source and a T3 source conflict, T0 controls and the conflict is reported.
- Every citation carries a retrieval timestamp and content hash. Law changes; a verdict is only valid against the snapshot it cited.

---

## 3. Claim taxonomy

Input is decomposed into **atomic claims** — one assertion each. Every claim is classified before it is analyzed, because the class determines what analysis is even legitimate.

| Type | Shape | Resolvable against | Verdict eligible? |
|---|---|---|---|
| `LEGAL` | "The law requires/permits/prohibits X" | T0, T1 | Yes |
| `EMPIRICAL` | "N people did X"; "X caused Y" | T1, T2 | Yes |
| `ATTRIBUTIVE` | "Person P said/did S" | T1, T3 | Yes |
| `PREDICTIVE` | "If we do X, Y will follow" | — | **No.** Scored for mechanism + base rate only |
| `NORMATIVE` | "X is unjust / wrong / should be" | — | **No.** Decomposed into premises |
| `DEFINITIONAL` | "X *is* a Y" (contested term) | — | **No.** Competing definitions surfaced |

### Why this is the most important section

Most political disagreement is normative wearing empirical clothes. "This policy is a disaster" contains a hidden empirical claim (about outcomes), a hidden predictive claim (about what happens next), and a value judgment (about what counts as disaster). A tool that renders TRUE/FALSE on that sentence is not a fact-checker; it is a partisan weapon with a citation format.

The correct behavior is to split it, check the checkable part, and state clearly that the value question is not one evidence resolves. This is what makes the tool usable by someone who disagrees with whoever is running it — which is the only way it is worth anything.

### Normative decomposition

For a `NORMATIVE` claim the output is:

1. The value(s) being asserted, stated neutrally.
2. The empirical and legal **premises** the judgment rests on, extracted as their own claims and adjudicated normally.
3. An explicit statement: *this is a value claim; evidence can inform it but cannot settle it.*
4. The strongest premise-level case on each side — not a verdict.

---

## 4. Verdict vocabulary

Per claim. Deliberately excludes "true" and "false."

| Verdict | Meaning |
|---|---|
| `SUPPORTED` | T0–T2 evidence affirms the claim as stated |
| `CONTRADICTED` | T0–T2 evidence contradicts the claim as stated |
| `MIXED` | Substantive T0–T2 evidence on both sides; the claim is partly right |
| `MISLEADING_CONTEXT` | Component facts check out; the framing materially distorts them |
| `REPORTED_UNVERIFIED` | Only T3 support exists |
| `UNSUPPORTED` | No qualifying evidence found either way |
| `UNVERIFIABLE` | Not the kind of claim evidence settles (normative, predictive, definitional) |
| `OUT_OF_SCOPE` | Not a political claim |

Each verdict carries:

- `confidence` — 0.0–1.0, and it must move with evidence quality, not with fluency
- `evidence_quality` — best tier reached
- `model_disagreement` — variance across models when `--consensus` is on
- `citations[]` — every one with URL, retrieved-at, content hash, and the specific quoted passage relied on

---

## 5. The plain-language fidelity gate

The requirement: legal text rewritten so an eighth-grader understands it, **without the meaning drifting**. Simplification is exactly where meaning dies, so simplification is not trusted — it is tested.

### Three passes

**Pass A — Extract (structured, verbatim).** From the source legal text, pull the operative provision word-for-word with its citation, then extract as discrete fields:

- who is bound
- what is required / prohibited / permitted
- triggering conditions
- exceptions and carve-outs
- effective dates and sunset
- penalties / enforcement mechanism
- the controlling modal verb for each obligation

**Pass B — Render.** Write the plain-language version. Constraints:

- Target Flesch-Kincaid grade 7.0–9.0.
- **Modal verbs are load-bearing and preserved exactly.** `shall` ≠ `may` ≠ `must not` ≠ `is not required to`. A "must" that becomes a "should" is a failure, not a style choice.
- **Locked glossary.** Terms of art may not be paraphrased away. `reasonable suspicion`, `probable cause`, `strict scrutiny`, `de novo`, `notwithstanding`, `prima facie`, `mens rea` and the rest of the registry are carried through verbatim with a plain-language footnote attached. Paraphrasing them silently changes the law.
- Every element from Pass A must appear. No exception may be dropped for readability.

**Pass C — Back-translate and diff.** A **separate model instance, given only the Pass B output and no access to the source**, reconstructs the Pass A field structure. That reconstruction is diffed against the real Pass A.

- Any dropped element → **FAIL**, regenerate.
- Any added element not in the source → **FAIL**, regenerate.
- Any modal verb strength change → **FAIL**, regenerate.
- Three consecutive failures → emit the verbatim text with a note that faithful simplification was not achieved. This outcome is acceptable and must be reachable.

`fidelity_score` (elements preserved / elements in source) ships with the output. Anything under 1.0 is surfaced to the reader.

### Automated linters (cheap, run every time)

- **Reading level** — FK grade in band.
- **Negation/scope linter** — counts negations, universal and existential quantifiers (`all`, `only`, `none`, `at least`, `not more than`), and exception clauses in source vs. rendering. Mismatch flags for review. This catches the single most common simplification error: dropping an "only" or an "unless."

---

## 6. Influence-operation pattern matching

Foreign influence claims come up constantly in political speech. Handling them rigorously is what separates analysis from conspiracism, so the constraint is tight:

**The tool identifies patterns. It never attributes actors.**

Permitted output: *"This narrative matches a pattern documented in [ODNI 2024 assessment / DOJ indictment 24-cr-XXX], which described [specific documented campaign]."* — with a T1 citation to the document making that finding.

Forbidden output: *"This is Russian disinformation."* / *"This account is a foreign asset."*

Attribution requires intelligence not available to an open-source tool. Asserting it anyway would be an unsupported claim of exactly the kind the Analyzer exists to catch — and the first time a published verdict got one wrong, the tool's credibility would be finished. Pattern-matching to a cited government or academic finding is defensible. Naming a state is not.

Signals the tool may legitimately report, each with a citation to the methodology it uses: narrative reuse across unrelated accounts, coordinated timing, claim provenance that traces to a T4 origin later amplified, and recycled framing from documented campaigns.

---

## 7. Mandatory red-team pass

After adjudication and before composition, a separate pass runs with a single instruction: **attack the analysis.**

It must produce:

- The strongest available counter-evidence to each `SUPPORTED` / `CONTRADICTED` verdict.
- The best steelman of the position the analysis went against.
- Any place the analysis reached past its evidence.
- Any claim where the source tier does not actually justify the confidence assigned.

Red-team findings ship **in the output**, not in a log. If the red-team pass materially undercuts a verdict, the verdict is downgraded automatically and the downgrade is recorded.

---

## 8. Refusals and hard limits

The prompt refuses, by design:

- To score, rank, or characterize a **person**. Claims only.
- To analyze private individuals by default. Public figures and public statements; `--private` requires explicit acknowledgment and emits a warning.
- To produce persuasive content. The output format is analytical. It is not a rebuttal generator, and "write me a takedown of X" is out of scope.
- To adjudicate normative claims (§3).
- To attribute to state actors (§6).
- To proceed when the input is too vague to yield an atomic claim — it says so and asks for specifics.

---

## 9. Output determinism

Temperature `0.0` by default. Fixed seed. Structured output enforced against a JSON Schema with a bounded retry-on-invalid loop. Prompt template version recorded in every run.

The point is not that the model is right. The point is that the run is **auditable and repeatable** — same input, same weights hash, same seed, same source snapshot, same output, same content hash. That property is the entire credibility argument, and it is why local open-weight models are the primary target: you can pin a weights file by hash. You cannot pin a hosted API endpoint.
