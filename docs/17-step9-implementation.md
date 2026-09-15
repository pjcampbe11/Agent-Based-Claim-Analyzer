# Step 9 — issues, evidence floors, and the governance line

What shipped, why each piece is shaped the way it is, and the one design
request that was answered differently from how it was asked.

## The capability

`abca issue` takes a political situation plus the evidence somebody actually
brought, and answers: what is established, what is a value call, what is still
unknown, and — with `--solve` — what could be done and who is allowed to decide.

```bash
abca issue check -t "<the issue>" --ref KIND:LOCATOR [--ref ...] [--solve] [--strict]
abca issue governance
```

## The evidence floor

`--ref` is required. Below the floor the command exits **12** and analyzes
nothing.

| | Default | `--strict` |
|---|---|---|
| References | 3 | 5 |
| Primary or official | 1 | 2 |
| Maximum social share | 50% | 25% |

`HARD_MINIMUM_TOTAL = 2` and `HARD_MINIMUM_PRIMARY = 1` cannot be configured
below. A configurable floor with no hard bottom is a floor that gets set to zero
on the first deadline, by someone with a good reason, and never gets set back.

The floor exists because a model handed a bare sentence will produce fluent,
confident, entirely unsourced policy analysis — the exact artifact the Analyzer
exists to detect in other people's work. The only reliable defense is for that
code path not to exist.

### Declared kind is a ceiling

`ReferenceKind.tier_ceiling` caps how strong a reference can ever be treated as;
the effective tier is the **weaker** of the ceiling and whatever the connector
resolved. Labelling generously costs nothing and buys nothing; labelling
modestly is honoured. `SOCIAL` is never evidence at any tier — a post is the
object of analysis, not proof of it.

Unfetchable references stay in the record at T4 with their reason attached.
Quotes that do not appear verbatim in the fetched source are dropped and the
reference is flagged.

## The governance line

The request was that approvals and voting move to automation and model analysis,
to remove red tape and greed-driven decisions. That was answered with a
three-tier split rather than full automation, and the reasoning is on the record
because the contract commits that the Analyzer "does not render a verdict on
whether a value claim is true."

| Tier | Rests on | Decision |
|---|---|---|
| `MECHANICAL` | No contestable claim | Fully automated |
| `VERIFIABLE` | Legal / empirical / attributive | Automated as a mandatory published check |
| `VALUE` | Any normative / definitional / predictive | Named human, publishing the run hash they had |

Derived from `CLAIM_GOVERNANCE_FLOOR`, never declared. `escalate()` is a one-way
ratchet. `PlanStep`'s validator refuses to serialize a step whose tier disagrees
with its claims, and a `VALUE` step without a named decider.

`PREDICTIVE` sits with the value claims. A prediction is not settleable by
evidence, so acting on one is a choice about which risk to accept — a value
decision wearing a forecast's clothes, and exactly where "the model said the
projection was favorable" would do the most damage.

**What this does and does not do.** It does not prevent a bad vote. It makes one
undeniable: the record shows what the decider knew. Corruption stops being
deniable rather than being prohibited by software. A design claiming the
stronger thing would be lying.

## Ranking

The model describes; code orders. Weights, printed with every run:
reversibility 0.30, speed 0.25, cost 0.25, evidence 0.20.

Reversibility is heaviest because the method pre-commits to publishing its own
contradictions, and that promise is inoperable if the plan already in motion
cannot be stopped. Cost is a **band** — a model asked for "$4.2 billion"
produces false precision. Every intervention needs a **falsifier**.

`RANKING_DISCLOSURE` names the weights as a value judgment, not a
finding, on every ranked list.

## Files

| Path | Purpose |
|---|---|
| `src/abca/schema/issue.py` | Reference kinds, governance tiers, the ratchet, cost/time/reversibility bands |
| `src/abca/schema/plan.py` | `Reference`, `PlanStep`, `Intervention` and their validators |
| `src/abca/issue/references.py` | Spec parsing, the floor, resolution against connectors |
| `src/abca/pipeline/frame.py` | Findings / values / open questions — deterministic |
| `src/abca/pipeline/solve.py` | Deterministic ranking and the disclosure |
| `src/abca/cli/_issue_cmd.py` | `abca issue check`, `abca issue governance` |
| `tests/test_issue.py` | 49 tests, mostly asserting that things did **not** happen |
| `scripts/demo_step9.py` | Four scenes, run against the live eCFR API |

## Exit codes

| Code | Meaning |
|---|---|
| 12 | evidence floor not met; nothing analyzed |
| 13 | a `--ref` value could not be parsed |
| 14 | `--solve` produced no plan clearing the governance gate |

## What is not built

`--solve` needs a configured backend to propose interventions. The governance
rules, the ranking, the plan schema and the validators are complete and tested;
wiring them to a live model is the remaining work. The command says so and
prints no plan it did not produce.
