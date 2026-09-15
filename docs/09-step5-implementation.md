# Step 5 — The Red-Team Pass

**Status: complete.** 602 tests + 4 live-network, 90% coverage, lint clean.
Every structural promise in `docs/01` is now kept by the code.

## What was built

| Module | Purpose |
|---|---|
| `schema/enums.py` | `RedTeamSeverity` — the four levels, one of which changes anything |
| `schema/core.py` | `RedTeamFinding` extended: verified counter-citations, severity, independence |
| `prompts/.../red_team.md` | The adversarial prompt |
| `pipeline/models.py` | `RedTeamAssessment` — the wire format |
| `pipeline/red_team.py` | The stage, the ratchet, counter-evidence verification |
| `pipeline/replay.py` | Schema-version guard |
| `cli/_analyze_cmd.py` | `--no-red-team --i-know`, findings in the report |

## The design question that mattered

Not "does it run" but **"what is it allowed to do."** A pass with the power to
change verdicts is also a pass that could change them for the wrong reasons —
so the interesting work is all in the constraints.

### 1. The ratchet: it can only weaken

The red team may lower a verdict's assertiveness and lower its confidence. It
can never do the reverse. `UNSUPPORTED` never becomes `SUPPORTED` here, and no
confidence goes up.

```
SUPPORTED    + "MIXED"        -> MIXED
SUPPORTED    + "UNSUPPORTED"  -> UNSUPPORTED
SUPPORTED    + (no rec)       -> UNSUPPORTED
MIXED        + "CONTRADICTED" -> REFUSED   (sideways is not weaker)
UNSUPPORTED  + "SUPPORTED"    -> REFUSED
UNVERIFIABLE + anything       -> REFUSED
```

This is the whole reason an adversarial pass can be trusted with verdict
authority. Without it, "red team" is just a second chance to assert something
with adversarial framing as cover. With it, the worst a compromised or merely
contrarian red team can do is make the tool say **less** than it knows — a
failure, but a safe one.

`UNVERIFIABLE` and `OUT_OF_SCOPE` are absent from the strength table entirely.
Those verdicts follow from the claim's *type* and from the gate, not from
evidence, so an evidence-based objection has no purchase on them and they are
never sent to this stage.

The ratchet lives in `VERDICT_STRENGTH` and `permitted_downgrade()` — **code,
not prompt**. A model cannot be talked out of a rule it never sees.

### 2. Counter-evidence is verified like any other evidence

The red team names a `source_id` and quotes it; the quote is checked verbatim
against the retrieved document; the tier comes from the connector. It reuses
`verify_citations()` rather than reimplementing it, deliberately — two
verification paths would eventually diverge and the weaker one would become the
way in.

Without this, the red team would be a **fabrication channel**: any verdict could
be undercut with invented text, which is precisely the hole step 4 closed on the
adjudication side. An adversarial pass that can assert freely is not a check, it
is a second unchecked opinion.

One consequence, encoded in `_apply()`: an objection whose textual support
evaporates cannot be `MATERIAL` **on that support alone**. It drops to `MINOR`
and the verdict stands. But if it *also* names a specific overreach, it survives
as `MATERIAL` — an analytical objection ("the statute is silent, so this rests
on inference") is legitimate, often the strongest kind, and quotes nothing.

### 3. Only MATERIAL moves anything

| Severity | Effect |
|---|---|
| `NONE` | published, changes nothing |
| `NOTED` | published, changes nothing |
| `MINOR` | published, changes nothing |
| `MATERIAL` | downgrades the verdict |

A pass that downgraded on any objection would move the failure rather than fix
it: every cited finding could be talked down by a sufficiently fluent complaint,
and the tool would drift toward saying nothing about anything.

So `MATERIAL` carries a burden — a schema validator requires verified
counter-evidence, a named overreach, or a substantive stated objection. "This
might be wrong" is not `MATERIAL`.

And the lesser severities are still **published**. A reader deciding how much
weight to give a verdict is better served by seeing the objections that did not
overturn it than by a silent pass.

`NONE` is a real and frequent answer. A red team that finds something material
in every verdict is being contrarian, not rigorous.

### 4. Independence is recorded, not required

`redteam` is a role that falls back to `adjudicator`. A same-model red team is
weaker — a model reviewing its own reasoning is the one least able to see where
it reached — but refusing to run one at all would be worse.

So the non-independence is **recorded on every finding** (`independent: false`),
noted loudly in the stage output, and surfaced in the report header. Comparison
is by resolved **weights hash**, not by role name or model string: two config
entries can point at one model under different tags, and a string comparison
would miss it.

This is a softer rule than the fidelity gate's, which hard-fails on a same-model
back-translation. The difference: back-translation is a *mechanical* check where
the same model reproduces the same misreadings deterministically, so a
same-model gate is a genuine no-op. An adversarial pass reframed as "attack
this" does produce different output from "adjudicate this" — weaker, not
worthless.

### 5. Skipping it is possible, and never silent

`--no-red-team` alone exits 2. It takes `--i-know` as well, because contract §7
makes the pass mandatory and a single flag should not switch off a contractual
guarantee.

A locked-down flag would just get worked around, so the design accepts the
escape hatch and makes it loud: `RunConfig.red_team` records `false`, the
`RED_TEAM SKIPPED` note goes into the run's notes — surviving `--json` and
surviving being read out of the ledger months later — and the prompt is omitted
from the recipe, so the run's `input_digest` differs from a reviewed run's.

### 6. Failure is visible

If the red-team call fails, the verdicts stand **unattacked** and the record
says so. The absence of the pass is exactly what the contract forbids, so it
cannot be the quiet outcome — a claim must never ship looking reviewed when it
was not.

## The schema bump, and what it costs

`RedTeamFinding` gained `counter_citations`, `severity` and `independent`, so
`SCHEMA_VERSION` moved to `1.1.0`.

`schema_version` is part of `input_digest`, so a record written under 1.0.0 can
never produce a matching digest from a 1.1.0 tool — the recipe itself differs.
Reporting that as `DIVERGENT` would blame the analyzer for a version change,
which is the wrong signal entirely. So `check_schema_compatibility()` refuses
replay with an explicit message instead.

**That is a real cost, accepted deliberately.** The alternative — excluding
`schema_version` from the digest — would let two runs recorded under different
field semantics compare as the same recipe.

What survives a schema bump forever is **integrity verification**. `audit()`
recomputes every digest and walks the stage chain using only the record's own
contents, so `abca verify <run-id> --integrity-only` still proves an old record
has not been edited, however old it is.

> Tamper-evidence is permanent. Byte-level replay is version-scoped.

## Contract §7, line by line

| Requirement | Where it lives |
|---|---|
| Runs after adjudication, before composition | `STAGE_ORDER`, enforced by the recorder |
| Strongest counter-evidence to each verdict | `counter_evidence` + verified `counter_citations` |
| Best steelman of the opposing position | `steelman`, required and non-empty |
| Where the analysis reached past its evidence | `overreach_flags` |
| Where confidence outruns the tier | prompt §4; surfaces as an overreach flag |
| Findings ship in the output, not a log | `Claim.red_team`, in the record and in `--json` |
| Material undercutting downgrades automatically | the ratchet |
| The downgrade is recorded | `verdict_downgraded_from` + written into `reasoning` |

## Try it

```bash
python scripts/demo_step5.py     # the ratchet, verification, severities, independence
python scripts/demo_step3.py     # full pipeline end to end through the real CLI
```

The demo shows the same objection twice — once with real statutory
counter-evidence (verdict downgraded) and once with fabricated text (severity
reduced, verdict stands) — which is the clearest way to see why counter-evidence
had to be verified.

## What is left

Every structural promise in `docs/01` is now implemented. What remains is
breadth and polish rather than architecture:

| Step | Scope |
|---|---|
| 6 | Fidelity gate + `explain` — plain-language legal rewriting with the back-translation diff |
| 7 | `-f`, `-u`, claim clustering, then `-U` |
| 8 | Consensus mode, more connectors, packaging |

Step 6 is the hardest remaining piece and the one with the most interesting
failure mode: simplification is exactly where legal meaning dies, so the gate
tests the rewrite rather than trusting it.
