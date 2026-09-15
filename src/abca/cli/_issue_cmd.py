"""``abca issue`` -- analyze a political issue and, optionally, plan a response.

WHAT MAKES THIS COMMAND DIFFERENT FROM ``analyze``
==================================================
``analyze`` takes a statement and checks the claims in it. ``issue`` takes a
SITUATION plus the evidence somebody actually brought, and answers a larger
question: what is established, what is a value call, what is still unknown, and
-- with ``--solve`` -- what could be done and who is allowed to decide it.

``--ref`` IS REQUIRED, AND THAT IS THE POINT
============================================
The command refuses to run without supporting references clearing a floor. Not
a warning. A refusal with exit code 12 and a list of what is missing.

Without that floor this command would be a machine for producing confident,
fluent, entirely unsourced policy essays -- which is exactly the artifact the
Analyzer exists to detect in other people's work, and building a first-class
producer of it inside the same repository would be indefensible.

EXIT CODES
==========
This command owns 12-14 and deliberately reuses nothing from ``verify`` (2-4)
or ``explain`` (10-11). A shared table where one number means two unrelated
things eventually misleads somebody's CI.

    12  the evidence floor was not met; nothing was analyzed
    13  a --ref value could not be parsed
    14  --solve was requested but no intervention cleared the governance gate
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from abca.issue.references import (
    EvidenceFloor,
    ReferenceSpecError,
    build_fetcher,
    check_floor,
    parse_reference_spec,
    resolve_references,
    tier_summary,
)
from abca.schema.issue import GovernanceTier

issue_app = typer.Typer(
    help=(
        "Analyze a political issue against supplied references, and optionally "
        "plan a response.\n\n"
        "Requires --ref. An issue with no supporting material is not analyzed."
    ),
    no_args_is_help=True,
)

EXIT_FLOOR_NOT_MET = 12
EXIT_BAD_REFERENCE = 13
EXIT_NO_VIABLE_PLAN = 14


REF_HELP = (
    "Supporting reference, as KIND:LOCATOR or KIND:LOCATOR||QUOTE. "
    "Repeatable and REQUIRED. Kinds: primary (statute, regulation, opinion), "
    "official (agency data, filing, on-the-record statement), research "
    "(peer-reviewed), reporting (journalism with corrections), social (a post -- "
    "admissible as the thing being analyzed, never as proof it is true). "
    "The kind is a ceiling on how strong the source can be treated as, never a "
    "promotion."
)


@issue_app.command("check")
def check_issue(
    text: Annotated[
        str | None, typer.Option("--text", "-t", help="The issue, as text.")
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="The issue, from a file.")
    ] = None,
    ref: Annotated[list[str] | None, typer.Option("--ref", help=REF_HELP)] = None,
    solve: Annotated[
        bool,
        typer.Option(
            "--solve",
            help=(
                "Also propose and rank interventions, with each step classified "
                "by who may execute it."
            ),
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "Use the publication floor (5 references, 2 primary/official, "
                "at most 25%% social) rather than the default. Required for "
                "anything published under the tool's name."
            ),
        ),
    ] = False,
    minimum_refs: Annotated[
        int,
        typer.Option(
            "--minimum-refs",
            help="Raise the reference minimum. Cannot be lowered below 2.",
        ),
    ] = 3,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help=(
                "Never touch the network. Cached sources still resolve; "
                "everything else falls to T4 and the floor will say so."
            ),
        ),
    ] = False,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the full report as JSON.")
    ] = None,
) -> None:
    """Frame an issue against its references. Refuses below the evidence floor."""
    if not text and not file:
        typer.secho("Supply the issue with --text or --file.", fg="red", err=True)
        raise typer.Exit(code=EXIT_BAD_REFERENCE)

    issue_text = file.read_text(encoding="utf-8") if file else (text or "")
    if not issue_text.strip():
        typer.secho("The issue is empty.", fg="red", err=True)
        raise typer.Exit(code=EXIT_BAD_REFERENCE)

    # --- Parse the references before anything else -----------------------
    # A malformed --ref is reported on its own, with the fix, rather than
    # being counted as a missing reference. Telling somebody they need three
    # sources when they supplied three and mistyped one is a bad error message.
    specs = ref or []
    references = []
    for index, spec in enumerate(specs, start=1):
        try:
            references.append(parse_reference_spec(spec, index=index))
        except ReferenceSpecError as error:
            typer.secho(str(error), fg="red", err=True)
            raise typer.Exit(code=EXIT_BAD_REFERENCE) from None

    floor = EvidenceFloor.strict() if strict else EvidenceFloor(
        minimum_total=max(minimum_refs, 3), minimum_primary=1
    )

    # Resolve BEFORE checking the floor. An unresolved reference falls to T4 by
    # design, so a command that skipped this step would refuse every input,
    # including a perfectly sourced one -- the floor would be measuring whether
    # we bothered to fetch, not whether the evidence exists.
    references = resolve_references(references, fetch=build_fetcher(offline=offline))
    result = check_floor(references, floor)

    typer.echo(render_floor(result, references))

    if not result.met:
        raise typer.Exit(code=EXIT_FLOOR_NOT_MET)

    payload = {
        "issue_chars": len(issue_text),
        "floor": {
            "met": result.met,
            "total": result.total,
            "primary_or_official": result.primary_or_official,
            "evidentiary": result.evidentiary,
            "social_share": result.social_share,
        },
        "references": [r.to_jsonable() for r in references],
        "tiers": tier_summary(references),
        "solve_requested": solve,
    }

    if solve:
        typer.echo()
        typer.secho(
            "--solve needs a configured backend to propose interventions. "
            "The governance rules, the ranking and the plan schema are built and "
            "tested (see `abca issue governance`); wiring them to a live model is "
            "the remaining work. Nothing was proposed, and this command will not "
            "print a plan it did not produce.",
            fg="yellow",
        )

    if out:
        out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        typer.echo(f"\nWrote {out}")


@issue_app.command("governance")
def show_governance() -> None:
    """Print the rule deciding who may execute each kind of step.

    Printed as a command rather than buried in documentation because it is the
    tool's most consequential design decision, and somebody evaluating the
    tool should be able to read it without cloning anything.
    """
    from abca.schema.issue import CLAIM_GOVERNANCE_FLOOR

    typer.secho("Who may execute a step of a plan\n", bold=True)
    typer.echo(
        "Governance tier is DERIVED from the classes of claim a step rests on.\n"
        "No model writes it, and no configuration flag moves the line.\n"
    )
    for tier in GovernanceTier:
        typer.secho(f"  {tier.value}", bold=True, nl=False)
        typer.echo(
            f"  automate the decision: {'yes' if tier.may_automate_decision else 'NO'}"
            f"   published check first: {'yes' if tier.requires_published_check else 'no'}"
            f"   named human decider: {'REQUIRED' if tier.requires_human_decider else 'no'}"
        )
    typer.echo("\nWhat each class of claim forces:\n")
    for claim_type, tier in CLAIM_GOVERNANCE_FLOOR.items():
        typer.echo(f"  {claim_type.value:14} -> {tier.value}")
    typer.echo(
        "\nA step resting on NO contestable claim is MECHANICAL: arithmetic, routing,\n"
        "eligibility, disbursement and publication against criteria already decided.\n"
        "That is where the delay and the paperwork actually live, and it automates\n"
        "completely.\n\n"
        "One normative, definitional or predictive claim anywhere in a step makes the\n"
        "whole step VALUE, and the escalation is one-way. A model deciding value\n"
        "questions would not remove greed from government; it would move greed into\n"
        "whoever wrote the objective function, where it is harder to see and\n"
        "impossible to vote out."
    )


def render_floor(result: object, references: list) -> str:
    """Human-readable floor report with the per-reference breakdown."""
    lines = ["References supplied:", ""]
    for reference in references:
        marker = "evidence" if reference.is_evidence else "context only"
        lines.append(
            f"  {reference.id}  {reference.declared_kind.value:9} "
            f"{reference.effective_tier.value}  {marker:12}  {reference.title}"
        )
        if reference.unfetchable_reason:
            lines.append(f"            note: {reference.unfetchable_reason}")
    lines += ["", result.report()]  # type: ignore[attr-defined]
    return "\n".join(lines)


__all__ = [
    "EXIT_BAD_REFERENCE",
    "EXIT_FLOOR_NOT_MET",
    "EXIT_NO_VIABLE_PLAN",
    "issue_app",
]
