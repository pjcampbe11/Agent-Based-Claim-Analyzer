"""``abca analyze`` -- the command the whole project exists to provide.

SAYING WHAT WAS ACTUALLY CHECKED
===============================
Claims are now adjudicated against real retrieved sources, with every quote
verified verbatim. But connector coverage is partial -- Illinois statutes by
citation, and nothing else yet -- so ``UNSUPPORTED`` carries two very different
meanings that this report keeps apart:

* **checked, not established** -- sources were consulted and did not settle it;
* **not checked** -- no connector covers this claim's type or citation.

Collapsing those would be the most misleading thing this command could do: a
reader would take "we looked and found nothing" from a claim nobody looked at.
So the table marks the second case ``no source``, and the coverage note is the
first entry in the run record's notes so it survives ``--json`` and survives
being read out of the ledger months later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from abca.cli import _render as render
from abca.config import Config, ConfigError, load_config
from abca.inputs.select import InputError, resolve
from abca.inputs.threads import DEFAULT_MAX_POSTS
from abca.ledger.inputs import InputStore
from abca.ledger.store import LedgerStore
from abca.pipeline.ingest import normalize_text
from abca.pipeline.orchestrator import (
    REQUIRED_ROLES,
    USER_SCOPE_NOTE,
    AnalyzeOptions,
    AnalyzeResult,
    analyze_thread,
)
from abca.providers.base import ProviderError
from abca.providers.registry import build_registry
from abca.schema.enums import ClaimType, InputKind, Profile, RedTeamSeverity, Verdict

analyze_app = typer.Typer(no_args_is_help=True)

#: Verdict colours. OUT_OF_SCOPE and UNVERIFIABLE are dimmed rather than
#: coloured: they are correct, final, and uninteresting, and colouring them
#: would compete for attention with findings that matter.
_VERDICT_STYLE: dict[Verdict, str] = {
    Verdict.SUPPORTED: "green",
    Verdict.CONTRADICTED: "red",
    Verdict.MIXED: "yellow",
    Verdict.MISLEADING_CONTEXT: "yellow",
    Verdict.REPORTED_UNVERIFIED: "cyan",
    Verdict.UNSUPPORTED: "dim",
    Verdict.UNVERIFIABLE: "dim",
    Verdict.OUT_OF_SCOPE: "dim",
}

#: Severity colours. NONE is dimmed: "we attacked it and found nothing" is a
#: real and frequent result that should not compete for attention with findings
#: that actually moved a verdict.
_SEVERITY_STYLE: dict[RedTeamSeverity, str] = {
    RedTeamSeverity.NONE: "dim",
    RedTeamSeverity.NOTED: "cyan",
    RedTeamSeverity.MINOR: "yellow",
    RedTeamSeverity.MATERIAL: "bold red",
}

_TYPE_ABBREV: dict[ClaimType, str] = {
    ClaimType.LEGAL: "LEG",
    ClaimType.EMPIRICAL: "EMP",
    ClaimType.ATTRIBUTIVE: "ATT",
    ClaimType.PREDICTIVE: "PRE",
    ClaimType.NORMATIVE: "NRM",
    ClaimType.DEFINITIONAL: "DEF",
}


def _load(config_path: Path | None) -> Config:
    try:
        return load_config(config_path, required=True)
    except ConfigError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=8) from exc


def _render_clusters(result: AnalyzeResult) -> None:
    """Show every group whose verdict was reached on another claim's words.

    Printed as its own block rather than left to the ``×`` column, because
    "this verdict was reached for a different sentence" is the one thing about a
    clustered run a reader must be able to check, and checking it means seeing
    which sentences were grouped together.
    """
    clusters = result.clusters
    if clusters is None:
        return
    grouped = [c for c in clusters.clusters if not c.is_singleton]
    if not grouped:
        return

    render.console.print(
        f"\n[bold]clusters[/bold] [dim]({clusters.merged_count} claim(s) inherited a "
        "verdict reached on another claim's text; the grouping is published so it "
        "can be checked)[/dim]"
    )
    for cluster in grouped:
        style = "yellow" if cluster.min_similarity < 0.85 else "dim"
        render.console.print(
            f"  [bold]{cluster.id}[/bold] {cluster.size} claims, "
            f"[{style}]weakest pair {cluster.min_similarity:.2f}[/{style}] "
            f"— adjudicated as [bold]{cluster.representative.id}[/bold]"
        )
        for member in cluster.members:
            marker = "→" if member.id == cluster.representative.id else " "
            render.console.print(f"    {marker} [dim]{member.id}[/dim] {member.text}")

def _render_panel(result: AnalyzeResult) -> None:
    """Show every claim the panel did not agree on, member by member.

    Printed rather than summarized, because the split IS the finding. A reader
    who sees only the merged verdict learns that the tool was cautious; a reader
    who sees which model said what learns whether the caution was warranted.
    """
    consensus = result.consensus
    if consensus is None:
        return

    render.console.print(
        f"\n[bold]consensus panel[/bold] [dim]({', '.join(consensus.panel)}; "
        f"{consensus.split_count} of {len(consensus.records)} claim(s) split)[/dim]"
    )
    if not consensus.split_count:
        render.console.print(
            "  [green]every member reached the same verdict on every claim[/green] "
            "[dim]— published confidence is the LOWEST any member reported, never "
            "the average[/dim]"
        )
        return

    for record in consensus.records.values():
        if record.unanimous:
            continue
        style = "bold red" if record.conflicted else "yellow"
        label = "CONFLICT" if record.conflicted else f"split {record.disagreement:.2f}"
        render.console.print(
            f"  [bold]{record.claim_id}[/bold] [{style}]{label}[/{style}] "
            f"→ published {record.merged.verdict.value}"
        )
        for vote in record.votes:
            render.console.print(
                f"    [dim]{vote.model}[/dim] {vote.record.verdict.value} "
                f"({vote.record.confidence:.2f})"
            )
        if record.conflicted:
            render.console.print(
                "    [red]Members read the same sources and reached opposite "
                "conclusions. This run does not settle the claim.[/red]"
            )

def _render_report(result: AnalyzeResult, *, show_reasoning: bool) -> None:
    """Print the human-readable report."""
    analysis = result.record.result
    thread = result.thread
    is_thread = thread is not None and len(thread.posts) > 1
    #: post id -> the author to show LOCALLY. The record has pseudonyms only;
    #: the person at this terminal is looking at their own source document, so
    #: showing them a stand-in for a handle they can already read helps nobody.
    authors = {post.id: post.display_author for post in (thread.posts if thread else [])}

    checked = sum(1 for claim in analysis.claims if claim.citations)
    header = (
        f"\n[dim]Sources consulted: {len(result.record.sources)}. "
        f"{checked} claim(s) carry verified citations. Connector coverage is "
        "Illinois statutes by citation only — see notes."
    )
    if is_thread:
        header += (
            f" Input: {len(thread.posts)} post(s) from {thread.author_count} "
            "account(s); handles are shown here but never written to the record."
        )
    render.console.print(header + "[/dim]\n")

    table = Table(title=f"{len(analysis.claims)} claim(s)")
    table.add_column("id", no_wrap=True, min_width=6)
    if is_thread:
        table.add_column("from", no_wrap=True, overflow="ellipsis", max_width=14)
    table.add_column("type", no_wrap=True, min_width=4)
    table.add_column("verdict", no_wrap=True, min_width=18)
    table.add_column("conf", justify="right", no_wrap=True, min_width=4)
    table.add_column("src", justify="center", no_wrap=True, min_width=3)
    table.add_column("×", justify="right", no_wrap=True, min_width=2)
    if result.consensus is not None:
        table.add_column("split", justify="right", no_wrap=True, min_width=5)
    table.add_column("claim", overflow="fold")

    for claim in analysis.claims:
        style = _VERDICT_STYLE.get(claim.verdict, "")
        verdict = claim.verdict.value
        # The distinction that matters: "we looked and found nothing" is not
        # the same finding as "nobody looked".
        if claim.verdict is Verdict.UNSUPPORTED and not claim.citations:
            verdict = "no source" if "No source was consulted" in claim.reasoning \
                else "not established"
        row = [claim.id]
        if is_thread:
            post = result.post_of(claim)
            row.append(authors.get(post, "[dim]-[/dim]") if post else "[dim]-[/dim]")
        row += [
            _TYPE_ABBREV.get(claim.claim_type, "?"),
            f"[{style}]{verdict}[/{style}]" if style else verdict,
            f"{claim.confidence:.2f}",
            (f"[green]{claim.evidence_quality.value}[/green]"
             if claim.evidence_quality else "[dim]-[/dim]"),
            # The × column is the honesty column for clustering: it says how many
            # claims this verdict covers, right next to the verdict.
            (f"[yellow]{claim.cluster.members}[/yellow]" if claim.cluster
             else "[dim]·[/dim]"),
        ]
        if result.consensus is not None:
            # The split column is the same idea for consensus: how much the
            # panel disagreed, right next to what was published.
            if claim.model_disagreement is None:
                row.append("[dim]·[/dim]")
            elif claim.model_disagreement == 0.0:
                row.append("[green]0.00[/green]")
            else:
                row.append(f"[yellow]{claim.model_disagreement:.2f}[/yellow]")
        row += [
            claim.text,
        ]
        table.add_row(*row)
    render.console.print(table)

    _render_clusters(result)
    _render_panel(result)

    cited = [claim for claim in analysis.claims if claim.citations]
    if cited:
        render.console.print("\n[bold]citations[/bold] [dim](every quote verified "
                             "verbatim against the retrieved source)[/dim]")
        for claim in cited:
            for citation in claim.citations:
                quote = " ".join(citation.quote.split())
                render.console.print(
                    f"  [bold]{claim.id}[/bold] [{citation.tier.value}] "
                    f"{citation.title} — [dim]{citation.url}[/dim]"
                )
                render.console.print(f"      \u201c{quote}\u201d")

    attacked = [claim for claim in analysis.claims if claim.red_team]
    if attacked:
        downgraded = [c for c in attacked if c.red_team.verdict_downgraded_from]
        independent = all(c.red_team.independent for c in attacked)
        render.console.print(
            f"\n[bold]red team[/bold] [dim]({len(attacked)} verdict(s) attacked"
            + (f", {len(downgraded)} downgraded" if downgraded else "")
            + ("" if independent else "; NOT independent — same model as the adjudicator")
            + ")[/dim]"
        )
        for claim in attacked:
            finding = claim.red_team
            if finding.severity is RedTeamSeverity.NONE and not finding.overreach_flags:
                continue
            style = _SEVERITY_STYLE.get(finding.severity, "")
            header = f"  [bold]{claim.id}[/bold] [{style}]{finding.severity.value}[/{style}]"
            if finding.verdict_downgraded_from:
                header += (f"  [yellow]{finding.verdict_downgraded_from.value} → "
                           f"{claim.verdict.value}[/yellow]")
            render.console.print(header)
            render.console.print(f"      counter: {finding.counter_evidence}")
            for flag in finding.overreach_flags:
                render.console.print(f"      [yellow]overreach:[/yellow] {flag}")
            for citation in finding.counter_citations:
                quote = " ".join(citation.quote.split())
                render.console.print(
                    f"      [{citation.tier.value}] {citation.title}: \u201c{quote}\u201d"
                )
            render.console.print(f"      [dim]steelman: {finding.steelman}[/dim]")

    if show_reasoning:
        render.console.print()
        for claim in analysis.claims:
            render.console.print(f"[bold]{claim.id}[/bold] {claim.reasoning}")

    render.console.print(
        render.kv_table(
            "run",
            [
                ("run id", result.record.run_id),
                ("ledger hash", result.record.ledger_hash),
                ("reproducible", "yes" if result.record.reproducible else "no (hosted model)"),
                ("verdicts", json.dumps(analysis.verdict_counts(), sort_keys=True)),
                ("types", json.dumps(analysis.type_counts(), sort_keys=True)),
            ],
        )
    )

    if analysis.notes:
        render.console.print("\n[bold]notes[/bold]")
        for note in analysis.notes:
            render.console.print(f"  • {note}")


@analyze_app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    text: Annotated[
        str | None,
        typer.Option("--text", "-t", help="Analyze this statement."),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f",
                     help="Analyze a file: .txt .md .json .csv .docx .pdf."),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", "-u", help="Analyze an article or thread at a URL."),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", "-U",
                     help="Analyze one account's posts within --from. Needs --from."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--from", help="Where -U looks: an export file, or a thread URL."),
    ] = None,
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the statement from standard input.")
    ] = False,
    max_posts: Annotated[
        int,
        typer.Option("--max-posts", min=1,
                     help="Posts carried into analysis from a thread or export."),
    ] = DEFAULT_MAX_POSTS,
    no_cluster: Annotated[
        bool,
        typer.Option("--no-cluster",
                     help="Adjudicate every claim separately, even near-duplicates."),
    ] = False,
    consensus: Annotated[
        bool,
        typer.Option("--consensus",
                     help="Adjudicate on the configured panel and report disagreement."),
    ] = False,
    private: Annotated[
        bool,
        typer.Option("--private",
                     help="Acknowledge the subject may not be a public figure. "
                          "Requires --i-know."),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to config.toml.", envvar="ABCA_CONFIG"),
    ] = None,
    ledger_root: Annotated[
        Path | None,
        typer.Option("--ledger-root", help="Where to write the run record.",
                     envvar="ABCA_LEDGER_ROOT"),
    ] = None,
    profile: Annotated[
        Profile | None, typer.Option("--profile", help="fast | standard | forensic.")
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="Sampling seed.")] = None,
    temperature: Annotated[
        float | None, typer.Option("--temperature", min=0.0, max=2.0)
    ] = None,
    max_claims: Annotated[
        int | None, typer.Option("--max-claims", min=1, help="Hard stop on claims extracted.")
    ] = None,
    no_gate: Annotated[
        bool,
        typer.Option("--no-gate", help="Skip the relevance gate; analyze every sentence."),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Use cached sources only; never fetch."),
    ] = False,
    no_red_team: Annotated[
        bool,
        typer.Option("--no-red-team",
                     help="Skip the mandatory adversarial pass. Requires --i-know."),
    ] = False,
    i_know: Annotated[
        bool,
        typer.Option("--i-know", help="Acknowledge disabling a contract-mandated pass."),
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the full run record as JSON.")] = False,
    show_reasoning: Annotated[
        bool, typer.Option("--reasoning", help="Print each claim's reasoning.")
    ] = False,
    no_save: Annotated[
        bool, typer.Option("--no-save", help="Do not write a run record to the ledger.")
    ] = False,
) -> None:
    """Analyze a political statement: extract, classify and adjudicate its claims.

    Legal claims carrying an ILCS citation are checked against the statute
    itself, and every quote is verified verbatim against the retrieved text
    before it becomes a citation. Other claim types have no connector yet, and
    the report marks those ``no source`` rather than letting UNSUPPORTED imply
    a search happened.

    Every verdict is then attacked by the red team, which can only make the
    analysis LESS assertive -- it may downgrade a verdict but never strengthen
    one. Its counter-evidence is verified against the sources exactly as the
    adjudicator's was.
    """
    if ctx.invoked_subcommand is not None:  # pragma: no cover - no subcommands yet
        return

    # -- input ------------------------------------------------------------
    # Resolution lives in abca.inputs.select, not here, so every input becomes
    # a Thread by the same rules whether it arrived as a flag or from a test.
    try:
        resolved = resolve(
            text=text,
            file=file,
            url=url,
            user=user,
            source=source,
            stdin=sys.stdin.read() if stdin else None,
            max_posts=max_posts,
        )
    except InputError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    # -- acknowledgments ---------------------------------------------------
    # Checked HERE: before the config is read and before any backend is built.
    # These are argument errors, and an argument error should not require a
    # working configuration to discover -- nor should it cost a hosted API call.
    # They sat after the health check until this was noticed, which meant
    # somebody with no model running was told their backend was unreachable
    # when what they actually needed was a second flag.
    #
    # Contract s7 makes the red team mandatory. Contract s8 limits analysis to
    # public figures and public statements. Both stay overridable -- a locked
    # flag just gets worked around -- but each takes a second, deliberate flag,
    # and each is recorded permanently rather than merely warned about, because
    # a caveat that lives only in a terminal is gone the moment the record is
    # shared.
    if no_red_team and not i_know:
        render.err_console.print(
            "[red]--no-red-team disables the adversarial pass that contract s7 "
            "makes mandatory.[/red]\n"
            "Verdicts from such a run are one model's unreviewed opinion, and the "
            "run record says so permanently.\n"
            "Pass [bold]--i-know[/bold] as well if that is what you intend."
        )
        raise typer.Exit(code=2)

    if private and not i_know:
        render.err_console.print(
            "[red]--private permits analysis of someone who may not be a public "
            "figure.[/red]\n"
            "Contract s8 limits this tool to public figures and public statements by "
            "default. The acknowledgment is recorded permanently in the run config.\n"
            "Pass [bold]--i-know[/bold] as well if that is what you intend."
        )
        raise typer.Exit(code=2)

    # -- config and backends ----------------------------------------------
    loaded = _load(config)
    for warning in loaded.warnings:
        render.err_console.print(f"[yellow]warning:[/yellow] {warning}")

    try:
        registry = build_registry(
            loaded, roles=list(REQUIRED_ROLES), consensus=consensus
        )
    except (ConfigError, ProviderError) as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    health = registry.health_check()
    broken = {role: status for role, status in health.items() if status != "ok"}
    if broken:
        # Fail before any work rather than partway through. The check costs a
        # second; discovering a stopped daemon after segmenting 400 sentences
        # costs the whole run.
        for role, status in broken.items():
            render.err_console.print(f"[red]{role}:[/red] {status}")
        raise typer.Exit(code=9)

    options = AnalyzeOptions.from_config(loaded)
    if profile is not None:
        options.profile = profile
    if seed is not None:
        options.seed = seed
    if temperature is not None:
        options.temperature = temperature
    if max_claims is not None:
        options.max_claims = max_claims
    options.no_gate = no_gate
    options.offline = offline
    options.cluster = not no_cluster
    options.consensus = consensus

    options.private_subjects = private

    options.red_team = not no_red_team

    # -- run --------------------------------------------------------------
    if resolved.kind is InputKind.USER:
        # Into the RECORD as well as the terminal. A caveat printed to a
        # terminal is gone the moment somebody shares the JSON, and "we analyzed
        # this account's posts" is exactly the sentence that needs the rest of
        # itself attached wherever it travels.
        resolved.thread.notes.insert(0, USER_SCOPE_NOTE)
        render.err_console.print(f"[yellow]{USER_SCOPE_NOTE}[/yellow]\n")

    try:
        result = analyze_thread(
            resolved.thread, registry, options, kind=resolved.kind
        )
    except ValueError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ProviderError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    # -- persist ----------------------------------------------------------
    if not no_save:
        # Cache the analyzed text LOCALLY, addressed by the same hash the record
        # carries, so `abca verify` on your own machine does not require you to
        # paste the statement back in. Never written into the run record: a
        # published record must not republish someone else's writing.
        try:
            InputStore().put(normalize_text(resolved.thread.render()[0])[0])
        except OSError as exc:
            render.err_console.print(
                f"[yellow]could not cache input for replay: {exc}[/yellow]"
            )

        store = LedgerStore(ledger_root)
        try:
            store.write(result.record)
        except OSError as exc:
            # A failed ledger write must not discard an analysis the user
            # already paid for -- report it and still print the result.
            render.err_console.print(f"[yellow]could not write run record: {exc}[/yellow]")

    # -- report -----------------------------------------------------------
    if json_out:
        render.print_json(result.record.to_jsonable())
    else:
        _render_report(result, show_reasoning=show_reasoning)


__all__ = ["analyze_app"]
