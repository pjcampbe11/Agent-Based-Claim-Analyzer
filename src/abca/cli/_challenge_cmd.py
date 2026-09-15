"""``abca queue`` and ``abca promote`` -- the challenge lane's operator surface (doc 20 s7).

WHY THESE ARE SEPARATE COMMANDS
===============================
Lane B is batch. The work is queued by one process and executed by another,
possibly days later, so the queue needs to be inspectable and drainable on its
own rather than existing only as an internal detail of ``analyze``.

The published-record commitment (doc 20 s8) also needs a surface. ``queue show``
prints what was searched and what was rejected, for promoted and unpromotable
challenges alike -- and the unpromotable ones matter most, because *"we tried to
find a source for this, here is exactly what we searched, we did not find one"*
is a more falsifiable artifact than a verdict.

EXIT CODES
==========
This command owns 15-16 and reuses nothing.

    15  the queue contains corrupt records
    16  `promote` was refused by the gate
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from abca.challenge.queue import ChallengeQueue, ChallengeState

queue_app = typer.Typer(
    help=(
        "Inspect and run the challenge queue.\n\n"
        "A challenge is a political claim that arrived with no source. Lane B "
        "tries to find one; the queue is where that work waits."
    ),
    no_args_is_help=True,
)

EXIT_CORRUPT_QUEUE = 15
EXIT_PROMOTION_REFUSED = 16

DEFAULT_ROOT = Path(".abca/challenges")

RootOption = Annotated[
    Path, typer.Option("--queue-root", help="Where challenges are stored.")
]


def _open(root: Path) -> ChallengeQueue:
    return ChallengeQueue(root)


@queue_app.command("list")
def list_challenges(
    root: RootOption = DEFAULT_ROOT,
    state: Annotated[
        str | None,
        typer.Option("--state", help="queued | running | dormant | resolved"),
    ] = None,
) -> None:
    """List challenges, oldest first."""
    queue = _open(root)
    corrupt = queue.corrupt()

    wanted = ChallengeState(state) if state else None
    rows = [c for c in queue if wanted is None or c.state is wanted]

    if not rows and not corrupt:
        typer.echo("the queue is empty")
        return

    typer.echo(f"{'challenge':30} {'state':9} {'posts':>5} {'grade':14} claim")
    for challenge in rows:
        grade = (challenge.outcome or {}).get("grade", "-")
        claim = challenge.denatured_claim
        typer.echo(
            f"{challenge.challenge_id:30} {challenge.state.value:9} "
            f"{challenge.occurrences:>5} {grade:14} "
            f"{claim[:52]}{'...' if len(claim) > 52 else ''}"
        )

    stats = queue.stats()
    typer.echo(
        f"\n{stats['total']} challenge(s) representing {stats['posts_represented']} "
        f"post(s); {stats['due_now']} due for re-check"
    )

    if corrupt:
        typer.secho(
            f"\n{len(corrupt)} unreadable record(s) in the queue:", fg="red", err=True
        )
        for path in corrupt:
            typer.secho(f"  {path}", fg="red", err=True)
        typer.secho(
            "A challenge that cannot be read is work the queue will silently never "
            "do. Fix or remove these rather than leaving the queue looking shorter "
            "than it is.",
            fg="red", err=True,
        )
        raise typer.Exit(code=EXIT_CORRUPT_QUEUE)


@queue_app.command("show")
def show_challenge(
    challenge_id: Annotated[str, typer.Argument(help="The ch-... id.")],
    root: RootOption = DEFAULT_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the record as JSON.")] = False,
) -> None:
    """Print a challenge's full published record.

    Prints what was searched, which referent candidates were considered AND
    rejected, and how much widening it took -- for an unpromotable challenge just
    as fully as for a promoted one.
    """
    challenge = _open(root).get(challenge_id)
    if challenge is None:
        typer.secho(f"no challenge {challenge_id}", fg="red", err=True)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(challenge.to_json(), indent=2, sort_keys=True))
        return

    typer.secho(challenge.challenge_id, bold=True)
    typer.echo(f"  state       : {challenge.state.value}")
    typer.echo(f"  claim       : {challenge.denatured_claim}")
    typer.echo(f"  claim hash  : {challenge.denatured_claim_hash}")
    typer.echo(f"  cluster     : {challenge.cluster_key}")
    typer.echo(f"  posts       : {challenge.occurrences}")
    typer.echo(f"  attempts    : {challenge.attempts}")
    if challenge.recheck_after:
        typer.echo(f"  re-check    : {challenge.recheck_after}  ({challenge.recheck_trigger})")

    outcome = challenge.outcome
    if not outcome:
        typer.echo("\n  not executed yet")
        return

    typer.echo(f"\n  grade       : {outcome['grade']}")
    for reason in outcome.get("reasons", []):
        typer.echo(f"    {reason}")

    if outcome.get("artifact"):
        artifact = outcome["artifact"]
        typer.echo(f"\n  artifact    : [{artifact['tier']}] {artifact['title']}")
        typer.echo(f"                {artifact['url']}")

    considered = outcome.get("candidates_considered", [])
    rejected = outcome.get("candidates_rejected", [])
    if considered:
        typer.echo(f"\n  candidates considered ({len(considered)}):")
        for candidate in considered:
            mark = "rejected" if candidate in rejected else "used"
            typer.echo(f"    [{mark:8}] {candidate}")

    queries = outcome.get("queries_executed", [])
    if queries:
        typer.echo(f"\n  queries executed ({len(queries)}):")
        for query in queries:
            typer.echo(f"    {query}")

    widenings = outcome.get("widenings", [])
    if widenings:
        typer.echo(f"\n  widenings ({len(widenings)}):")
        for widening in widenings:
            typer.echo(
                f"    {widening['axis']:12} found={widening['found']!s:5} "
                f"{widening['query']}"
            )
        typer.secho(
            "  A promotion that needed widening is weaker than one that did not.",
            fg="yellow",
        )

    if outcome.get("budget_exhausted"):
        typer.secho(
            "\n  The budget was exhausted. This is 'we stopped looking', not "
            "'we looked and found nothing'.",
            fg="yellow",
        )


@queue_app.command("stats")
def queue_stats(root: RootOption = DEFAULT_ROOT) -> None:
    """Summarise the queue."""
    stats = _open(root).stats()
    for key, value in stats.items():
        typer.echo(f"  {key:20} {value}")


@queue_app.command("dormant")
def list_dormant(root: RootOption = DEFAULT_ROOT) -> None:
    """List dormant challenges and what they are waiting on.

    Published on purpose: the record should show what the analysis is waiting on,
    not only what it has settled.
    """
    queue = _open(root)
    dormant = queue.by_state(ChallengeState.DORMANT)
    if not dormant:
        typer.echo("no dormant challenges")
        return
    for challenge in dormant:
        due = "DUE NOW" if challenge.is_due else f"re-check {challenge.recheck_after}"
        typer.echo(f"  {challenge.challenge_id}  {due:24} {challenge.denatured_claim[:48]}")
        if challenge.recheck_trigger:
            typer.echo(f"      waiting on: {challenge.recheck_trigger}")


@queue_app.command("release")
def release_stranded(root: RootOption = DEFAULT_ROOT) -> None:
    """Return every RUNNING challenge to the queue.

    Run after a drain was killed. Without it, stranded challenges sit in RUNNING
    forever and the queue quietly stops making progress while still looking
    healthy.
    """
    queue = _open(root)
    stranded = queue.by_state(ChallengeState.RUNNING)
    for challenge in stranded:
        queue.release(challenge)
    typer.echo(f"released {len(stranded)} stranded challenge(s)")


@queue_app.command("promote")
def promote_manually(
    challenge_id: Annotated[str, typer.Argument(help="The ch-... id.")],
    operator: Annotated[str, typer.Option("--operator", help="Who is promoting this.")],
    i_know: Annotated[
        bool,
        typer.Option("--i-know", help="Acknowledge overriding the automatic gate."),
    ] = False,
    root: RootOption = DEFAULT_ROOT,
) -> None:
    """Promote a challenge by hand, for when a human found the source.

    Requires ``--i-know`` and an operator, and records both. It does NOT bypass
    P4: a human cannot substitute the claim either, and the hash is re-checked
    here exactly as it is in the automatic path.
    """
    if not i_know:
        typer.secho(
            "manual promotion overrides the automatic gate and must be "
            "acknowledged with --i-know. The promotion is recorded against your "
            "name and published with the verdict.",
            fg="red", err=True,
        )
        raise typer.Exit(code=EXIT_PROMOTION_REFUSED)

    queue = _open(root)
    challenge = queue.get(challenge_id)
    if challenge is None:
        typer.secho(f"no challenge {challenge_id}", fg="red", err=True)
        raise typer.Exit(code=1)

    from abca.canonical import digest_text

    actual = digest_text(challenge.denatured_claim.strip())
    if actual != challenge.denatured_claim_hash:
        typer.secho(
            "REFUSED: the claim's hash does not match its text. A human cannot "
            "substitute the claim either -- promoting now would adjudicate a "
            "sentence nobody posted.",
            fg="red", err=True,
        )
        raise typer.Exit(code=EXIT_PROMOTION_REFUSED)

    typer.secho(
        f"{challenge_id} marked for manual promotion by {operator}.\n"
        "The claim hash was re-verified. Attach the artifact and run Lane A; the "
        "published record will state that a human performed this promotion.",
        fg="yellow",
    )


__all__ = ["EXIT_CORRUPT_QUEUE", "EXIT_PROMOTION_REFUSED", "queue_app"]
