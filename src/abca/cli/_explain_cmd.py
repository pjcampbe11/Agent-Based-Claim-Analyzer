"""``abca explain`` -- a statute in plain English, with the check shown.

WHY THE OUTPUT LEADS WITH THE GATE RESULT
=========================================
A plain-language rendering of a statute is the single most quotable thing this
tool produces, and the most dangerous. Somebody will screenshot it. If the
rewrite dropped an exception, the screenshot is wrong in a way no reader can
detect by looking at it -- the rewrite reads BETTER for having dropped it.

So the report never shows a rendering without showing what happened when it was
tested: how many operative elements the statute has, how many an independent
model recovered from the rewrite alone, and whether the force of every
obligation survived. On a failure the statute's own words are printed instead,
labelled, with the specific losses listed.

The tool would look more polished if it printed just the nice paragraph. It
would also be the thing the contract exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from abca.cli import _render as render
from abca.config import ConfigError, load_config
from abca.ledger.inputs import InputStore
from abca.ledger.store import LedgerStore
from abca.pipeline.fidelity import FidelityConfigError, PlainLanguageResult
from abca.pipeline.ingest import normalize_text
from abca.pipeline.orchestrator import (
    EXPLAIN_ROLES,
    AnalyzeOptions,
    ExplainResult,
    explain_citation,
)
from abca.providers.base import ProviderError
from abca.providers.registry import build_registry
from abca.schema.enums import Profile

explain_app = typer.Typer(no_args_is_help=True)


def _render_gate(rendering: PlainLanguageResult) -> None:
    """Print the gate's own verdict on the rendering, before the rendering."""
    report = rendering.to_report()
    passed = not rendering.verbatim_fallback

    headline = (
        "[green]FIDELITY GATE PASSED[/green]"
        if passed
        else "[red]FIDELITY GATE FAILED — showing the statute's own words[/red]"
    )
    render.console.print(f"\n{headline}")

    rows = [
        ("elements in statute", str(report.elements_source)),
        (
            "recovered from the rewrite",
            (
                f"{report.elements_preserved}  [dim](by an independent model that "
                "never saw the statute)[/dim]"
            ),
        ),
        ("fidelity score", f"{report.score:.2f}"),
        (
            "modal force preserved",
            "[green]yes[/green]" if report.modal_verbs_preserved
            else "[red]NO — an obligation changed strength[/red]",
        ),
        ("rewrites attempted", str(len(rendering.attempts))),
        (
            "reading grade",
            rendering.readability.describe() if rendering.readability else "-",
        ),
    ]
    render.console.print(render.kv_table("fidelity gate", rows))

    # Every failed attempt is listed, not just the last. A rewrite that failed
    # three different ways is a different signal from one that failed the same
    # way three times -- the first is a hard provision, the second is a model
    # that cannot see the thing it keeps losing.
    failed = [attempt for attempt in rendering.attempts if not attempt.passed]
    if failed:
        render.console.print("\n[bold]what the check found[/bold]")
        for attempt in failed:
            score = f"{attempt.diff.score:.2f}" if attempt.diff else "n/a"
            render.console.print(f"  [dim]attempt {attempt.attempt} (score {score})[/dim]")
            for failure in attempt.failures:
                style = "red" if failure.startswith("MODAL CHANGE") else "yellow"
                render.console.print(f"    [{style}]•[/{style}] {failure}")


def _render_explain(result: ExplainResult, *, show_elements: bool) -> None:
    """Print the whole report."""
    if not result.resolved:
        render.err_console.print(
            "[yellow]that citation did not resolve to any source in this build.[/yellow]"
        )
        for note in result.record.result.notes:
            render.console.print(f"  • {note}")
        return

    source = result.source
    rendering = result.rendering
    assert rendering is not None  # resolved implies a gate ran

    render.console.print(
        render.kv_table(
            "source",
            [
                ("title", source.title),
                ("tier", source.tier.value),
                ("locator", source.locator or "-"),
                ("url", source.url),
                ("retrieved", source.retrieved_at.isoformat()),
                ("content hash", source.content_hash),
            ],
        )
    )

    _render_gate(rendering)

    label = (
        "plain language"
        if not rendering.verbatim_fallback
        else "statute, verbatim (faithful simplification not achieved)"
    )
    render.console.print(f"\n[bold]{label}[/bold]\n")
    render.console.print(rendering.text)

    if rendering.footnotes:
        render.console.print("\n[bold]terms of art[/bold] [dim](kept verbatim, "
                             "explained rather than replaced)[/dim]")
        for footnote in rendering.footnotes:
            render.console.print(f"  [bold]{footnote.term}[/bold]: {footnote.explanation}")

    if show_elements:
        render.console.print("\n[bold]operative elements[/bold] [dim](pass A, from the "
                             "statute's own words)[/dim]")
        for index, element in enumerate(rendering.elements, start=1):
            anchors = f"  [dim]{', '.join(element.anchors)}[/dim]" if element.anchors else ""
            render.console.print(f"  [{index}] {element.summary()}{anchors}")

    render.console.print(
        render.kv_table(
            "run",
            [
                ("run id", result.record.run_id),
                ("ledger hash", result.record.ledger_hash),
                ("reproducible", "yes" if result.record.reproducible else "no (hosted model)"),
            ],
        )
    )

    if result.record.result.notes:
        render.console.print("\n[bold]notes[/bold]")
        for note in result.record.result.notes:
            render.console.print(f"  • {note}")


@explain_app.callback(invoke_without_command=True)
def explain(
    ctx: typer.Context,
    cite: Annotated[
        str | None,
        typer.Option("--cite", help='Statute citation, e.g. "10 ILCS 5/10-2".'),
    ] = None,
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
    offline: Annotated[
        bool, typer.Option("--offline", help="Use cached sources only; never fetch.")
    ] = False,
    show_elements: Annotated[
        bool, typer.Option("--elements", help="Print the operative elements pass A extracted.")
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the full run record as JSON.")
    ] = False,
    no_save: Annotated[
        bool, typer.Option("--no-save", help="Do not write a run record to the ledger.")
    ] = False,
) -> None:
    """Explain a statute in plain language, and prove the meaning survived.

    Three passes (contract s5). One model extracts the provision's operative
    elements and writes the plain-language version. A DIFFERENT model, given
    only that rewrite and no access to the statute, reconstructs the rule from
    it. The two element sets are diffed in code -- not by a model -- and the
    rewrite ships only if nothing was dropped, nothing was invented, and no
    obligation changed force.

    Three failures emit the statute verbatim instead. That is an acceptable
    outcome: an unverified rewrite of a law is worse than a hard-to-read law.
    """
    if ctx.invoked_subcommand is not None:  # pragma: no cover - no subcommands
        return

    if not cite:
        render.err_console.print(
            '[red]no citation. Use --cite "10 ILCS 5/10-2".[/red]\n'
            "[dim]Illinois Compiled Statutes are the only connector in this build.[/dim]"
        )
        raise typer.Exit(code=2)

    try:
        loaded = load_config(config, required=True)
    except ConfigError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=8) from exc
    for warning in loaded.warnings:
        render.err_console.print(f"[yellow]warning:[/yellow] {warning}")

    try:
        registry = build_registry(loaded, roles=list(EXPLAIN_ROLES))
    except (ConfigError, ProviderError) as exc:
        # Reaching here usually means no [models.backtranslate] section. That
        # role has no fallback anywhere in the system, on purpose: the degraded
        # form of an independent back-translation is a gate that passes
        # everything, which is worse than no gate at all.
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    health = registry.health_check()
    broken = {role: status for role, status in health.items() if status != "ok"}
    if broken:
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
    options.offline = offline
    options.red_team = False  # no verdicts here; pass C is the adversarial step

    try:
        result = explain_citation(cite, registry, options)
    except FidelityConfigError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc
    except ProviderError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    if not no_save:
        # Cache the citation locally, addressed by the same hash the record
        # carries, so `abca verify` on this machine does not require it to be
        # pasted back in. Never written INTO the record: a published record
        # carries provenance, not payload -- the rule is the same here as in
        # `analyze`, even though a citation is short and harmless, because a
        # second rule is a second thing to get wrong later.
        try:
            InputStore().put(normalize_text(cite)[0])
        except OSError as exc:
            render.err_console.print(
                f"[yellow]could not cache input for replay: {exc}[/yellow]"
            )

        store = LedgerStore(ledger_root)
        try:
            store.write(result.record)
        except OSError as exc:
            render.err_console.print(f"[yellow]could not write run record: {exc}[/yellow]")

    if json_out:
        render.print_json(result.record.to_jsonable())
    else:
        _render_explain(result, show_elements=show_elements)

    # A failed gate is not a crashed program, but it is not a success either --
    # a script piping this into a publishing step must be able to tell that what
    # it received is the statute rather than a rewrite.
    #
    # 10 and 11 rather than the low numbers, which are already spoken for:
    # `verify` uses 2-4 for DRIFTED / DIVERGENT / UNREPLAYABLE. Reusing them for
    # unrelated outcomes would make a shared exit-code table meaningless and
    # would eventually mislead someone's CI.
    if result.resolved and result.rendering and result.rendering.verbatim_fallback:
        raise typer.Exit(code=10)
    if not result.resolved:
        raise typer.Exit(code=11)


__all__ = ["explain_app"]
