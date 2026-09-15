"""Demonstrate `abca issue`: the evidence floor, framing, and the governance line.

Runs against the LIVE eCFR connector, because the point of the evidence floor is
that real sources get fetched and real quotes get verified. Nothing here is
mocked except the interventions, which would need a configured model.

    python scripts/demo_step9.py

Four scenes:

  1. An issue with no references is REFUSED.
  2. An issue with only social posts is REFUSED.
  3. The same issue with real regulations clears the floor and is framed.
  4. A plan is built and every step is classified by who may execute it --
     including an attempt to forge a MECHANICAL tier onto a value decision,
     which the schema rejects.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from abca.issue.references import (
    EvidenceFloor,
    build_fetcher,
    check_floor,
    parse_reference_spec,
    resolve_references,
)
from abca.pipeline.solve import (
    RANKING_DISCLOSURE,
    rank_interventions,
)
from abca.schema.enums import ClaimType
from abca.schema.issue import (
    CostBand,
    GovernanceTier,
    Reversibility,
    TimeToEffect,
)
from abca.schema.plan import Intervention, PlanStep

ISSUE = (
    "People in federal prison work, and the money they earn does not reach the "
    "families they supported before they were incarcerated."
)

RULE = "-" * 74


def scene(number: int, title: str) -> None:
    print(f"\n{RULE}\n{number}. {title}\n{RULE}")


def show(specs: list[str], *, resolve: bool) -> tuple[list, object]:
    references = [parse_reference_spec(s, index=i) for i, s in enumerate(specs, start=1)]
    if resolve:
        references = resolve_references(references, fetch=build_fetcher())
    for reference in references:
        marker = "evidence" if reference.is_evidence else "context only"
        print(
            f"  {reference.id}  {reference.declared_kind.value:9} "
            f"{reference.effective_tier.value}  {marker:12}  {reference.title}"
        )
        if reference.quote:
            print(f'            verified quote: "{reference.quote[:56]}..."')
    result = check_floor(references, EvidenceFloor())
    print()
    print("  " + result.report().replace("\n", "\n  "))
    return references, result


def main() -> int:
    print(f"ISSUE: {ISSUE}\n")

    scene(1, "No references. The command refuses.")
    show([], resolve=False)

    scene(2, "Only social posts. Still refused.")
    show(
        [
            "social:https://example.com/post/1",
            "social:https://example.com/post/2",
            "social:https://example.com/post/3",
        ],
        resolve=False,
    )

    scene(3, "Real regulations, fetched live. The floor is met.")
    references, result = show(
        [
            "primary:28 CFR 545.11||Special Assessments imposed under 18 U.S.C. 3013",
            "primary:28 CFR 345.51||receive pay at five levels ranging from 5th grade pay",
            "primary:28 CFR 345.50||Title 18 U. S. Code section 4126 authorizes FPI",
            "social:https://example.com/post/1",
        ],
        resolve=True,
    )
    if not result.met:  # type: ignore[attr-defined]
        print("\n  (The network was unavailable; scenes 3-4 need the live eCFR API.)")
        return 0

    scene(4, "A plan, with every step classified by who may execute it.")
    steps = (
        PlanStep.with_governance(
            id="s-001",
            description="Compute each worker's remittance share from filed dependant data",
            days_to_execute=0,
        ),
        PlanStep.with_governance(
            id="s-002",
            description="Disburse the share to the designated dependant each pay period",
            days_to_execute=0,
        ),
        PlanStep.with_governance(
            id="s-003",
            description="Confirm the remittance order does not conflict with 28 CFR 545.11",
            rests_on=(ClaimType.LEGAL,),
            days_to_execute=2,
        ),
        PlanStep.with_governance(
            id="s-004",
            description="Decide what share of pay is remitted, and to whom",
            rests_on=(ClaimType.NORMATIVE,),
            decider="Bureau of Prisons Director, on the record",
            days_to_execute=30,
        ),
    )
    for step in steps:
        automate = "AUTOMATED" if step.governance.may_automate_decision else "human"
        check = "check published first" if step.governance.requires_published_check else ""
        print(f"  {step.id}  {step.governance.value:11} {automate:10} {check}")
        print(f"        {step.description}")
        if step.decider:
            print(f"        accountable: {step.decider}")

    print("\n  Attempting to mark the value decision as automatable:")
    try:
        PlanStep(
            id="s-004",
            description="Decide what share of pay is remitted, and to whom",
            rests_on=(ClaimType.NORMATIVE,),
            governance=GovernanceTier.MECHANICAL,
            decider="an optimizer",
        )
        print("        NOT REFUSED -- this is a bug.")
        return 1
    except ValueError as error:
        print(f"        REFUSED: {str(error).splitlines()[0][:96]}")

    option = Intervention(
        id="i-001",
        title="Dependant remittance from existing prison pay",
        mechanism=(
            "Route a majority share of existing FPI pay to a designated dependant "
            "before the financial-plan priority order consumes it."
        ),
        authority="28 CFR 345.50, 28 CFR 545.11 (amendment)",
        cost_band=CostBand.UNDER_10M,
        time_to_effect=TimeToEffect.MONTHS,
        reversibility=Reversibility.TRIVIAL,
        steps=steps,
        falsifier=(
            "Recipient households show no measurable change in material hardship "
            "relative to matched households after two years."
        ),
        citations=tuple(
            c for c in (r.as_citation() for r in references) if c is not None
        ),
    )
    ranked = rank_interventions([option])[0]
    print(f"\n  score  {ranked.explain()}")
    print(f"  automatable steps      {option.automatable_share:.0%}")
    print(f"  human value decisions  {option.value_decisions}")
    print(f"  citations carried      {len(option.citations)}")
    print(f"\n  {RANKING_DISCLOSURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
