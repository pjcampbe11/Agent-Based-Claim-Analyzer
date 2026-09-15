"""``abca sources`` -- inspect connectors and the retrieval cache.

The commands here exist to answer one question a reader of a verdict will ask:
*where did this evidence come from, and can I get it myself?*

``sources fetch`` is the honest answer to that. It pulls a statute through the
same connector, the same extraction, and the same hashing the analyzer uses, so
the content hash it prints is the value that would appear in a citation. Anyone
doubting a published run can compare the two without installing anything else
or trusting this tool's report of its own behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from abca.cli import _render as render
from abca.sources.base import ConnectorError
from abca.sources.cache import SourceCache
from abca.sources.registry import ROUTING, build_connectors, tier_of

sources_app = typer.Typer(help="Inspect source connectors and the retrieval cache.",
                          no_args_is_help=True)

CacheRootOption = Annotated[
    Path | None,
    typer.Option("--cache-root", help="Source cache directory.", envvar="ABCA_SOURCE_CACHE"),
]


@sources_app.command("list")
def sources_list(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List available connectors, their tiers, and which claim types route to them.

    The tier column is read from each connector CLASS, not from configuration.
    That is the point: it is not something an operator or a model can change.
    """
    rows = []
    for name in sorted(build_connectors()):
        tier = tier_of(name)
        serves = sorted(
            claim_type.value for claim_type, names in ROUTING.items() if name in names
        )
        rows.append({"connector": name, "tier": tier.value if tier else "?", "serves": serves})

    if json_out:
        render.print_json(rows)
        return

    table = Table(title="source connectors")
    table.add_column("connector", no_wrap=True)
    table.add_column("tier", no_wrap=True, justify="center")
    table.add_column("serves claim types")
    for row in rows:
        table.add_row(row["connector"], row["tier"], ", ".join(row["serves"]) or "-")
    render.console.print(table)

    uncovered = sorted(
        claim_type.value for claim_type, names in ROUTING.items() if not names
    )
    if uncovered:
        render.console.print(
            f"\n[dim]No connector yet for: {', '.join(uncovered)}. "
            "UNSUPPORTED on those claim types means unchecked, not refuted.[/dim]"
        )


@sources_app.command("fetch")
def sources_fetch(
    locator: Annotated[str, typer.Argument(help='Citation, e.g. "10 ILCS 5/10-2".')],
    connector: Annotated[str, typer.Option("--connector", "-c")] = "ilcs",
    cache_root: CacheRootOption = None,
    offline: Annotated[bool, typer.Option("--offline", help="Cache only; never fetch.")] = False,
    show_text: Annotated[bool, typer.Option("--text", help="Print the extracted text.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Fetch one source and print its provenance.

    The content hash printed here is the exact value a citation to this source
    would carry, so it can be compared against a published run record by hand.
    """
    connectors = build_connectors(cache=SourceCache(cache_root), offline=offline)
    if connector not in connectors:
        render.err_console.print(
            f"[red]unknown connector {connector!r}; available: "
            f"{', '.join(sorted(connectors))}[/red]"
        )
        raise typer.Exit(code=1)

    try:
        document = connectors[connector].fetch(locator)
    except ConnectorError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_out:
        render.print_json({
            "id": document.id,
            "tier": document.tier.value,
            "title": document.title,
            "url": document.url,
            "locator": document.locator,
            "content_hash": document.content_hash,
            "retrieved_at": document.retrieved_at.isoformat(),
            "connector": document.connector,
            "chars": len(document.text),
            "text": document.text if show_text else None,
        })
        return

    render.console.print(
        render.kv_table(
            document.title,
            [
                ("tier", f"{document.tier.value} (fixed by the {document.connector} connector)"),
                ("locator", document.locator or "-"),
                ("url", document.url),
                ("retrieved", document.retrieved_at.isoformat()),
                ("content hash", document.content_hash),
                ("length", f"{len(document.text):,} chars"),
            ],
        )
    )
    if show_text:
        render.console.print()
        render.console.print(document.text)


@sources_app.command("cache-stats")
def sources_cache_stats(
    cache_root: CacheRootOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show how many documents are cached, per connector."""
    cache = SourceCache(cache_root)
    stats = cache.stats()
    if json_out:
        render.print_json({"root": str(cache.root), "entries": stats})
        return
    if not stats:
        render.console.print(f"[dim]No cached sources in {cache.root}[/dim]")
        return
    table = Table(title=f"cached sources in {cache.root}")
    table.add_column("connector", no_wrap=True)
    table.add_column("documents", justify="right")
    for name, count in sorted(stats.items()):
        table.add_row(name, str(count))
    render.console.print(table)


@sources_app.command("clear")
def sources_clear(
    connector: Annotated[str | None, typer.Option("--connector", "-c")] = None,
    cache_root: CacheRootOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete cached source documents.

    Clearing the cache does not invalidate any past run: citations carry their
    own content hashes, and drift is detected by re-fetching, not by what
    happens to be cached.
    """
    cache = SourceCache(cache_root)
    if not yes:
        target = connector or "all connectors"
        typer.confirm(f"Delete cached sources for {target} in {cache.root}?", abort=True)
    removed = cache.clear(connector)
    render.console.print(f"[green]removed[/green] {removed} cached document(s)")


__all__ = ["sources_app"]
