"""abCA command-line interface.

PLATFORM SUPPORT
================
Pure Python, no platform-specific code paths. It runs identically on Windows
(the primary desktop target) and on Linux (the EC2 target -- see
docs/04-linux-ec2.md). The only platform-aware component is
:func:`abca.ledger.store.default_data_dir`, which resolves ``%LOCALAPPDATA%``
on Windows and honors ``XDG_DATA_HOME`` on POSIX.

WHAT EXISTS
===========
``analyze`` runs the real pipeline as far as it is built: ingest, segment,
classify and gate. Retrieval and adjudication land in build step 4, and every
output surface says so rather than letting UNSUPPORTED imply a search happened.

``explain`` renders one statute in plain language and proves the meaning
survived: a second, independent model reconstructs the rule from the rewrite
alone and the two element sets are diffed in code. A rewrite that cannot be
shown to preserve the provision is not shipped -- the statute's own words are
printed instead.

``verify`` does the half of its job that is real today: it re-audits the stored
record -- recomputing every digest and walking the stage hash chain -- which
detects any post-hoc edit to a run. The replay half needs the adjudication
stages, so the command says so explicitly and exits with a distinct code rather
than reporting a success it cannot back up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from abca.cli import _render as render
from abca.cli._analyze_cmd import analyze_app
from abca.cli._challenge_cmd import queue_app
from abca.cli._explain_cmd import explain_app
from abca.cli._issue_cmd import issue_app
from abca.cli._models_cmd import config_app, models_app
from abca.cli._sources_cmd import sources_app
from abca.ids import InvalidRunId
from abca.ledger.store import LedgerCorrupt, LedgerStore, RunNotFound
from abca.schema.ledger import RunRecord
from abca.version import PROMPT_CONTRACT_VERSION, SCHEMA_VERSION, TOOL_VERSION

app = typer.Typer(
    name="abca",
    help=(
        "abCA (agent-based Claim Analyzer) - reproducible, cited analysis of political claims.\n\n"
        "The analyzer is viewpoint-agnostic by construction: its prompts contain no "
        "editorial doctrine, and every run emits a content-addressed ledger that a "
        "third party can re-execute."
    ),
    no_args_is_help=True,
    add_completion=False,
)

ledger_app = typer.Typer(help="Inspect and audit stored run records.", no_args_is_help=True)
app.add_typer(ledger_app, name="ledger")

# Step 3: the analysis command itself.
app.add_typer(analyze_app, name="analyze")

# Step 2: model/backend inspection and configuration.
app.add_typer(models_app, name="models")
app.add_typer(config_app, name="config")

# Step 4: source connectors and the retrieval cache.
app.add_typer(sources_app, name="sources")

# Step 6: the plain-language fidelity gate.
app.add_typer(explain_app, name="explain")

# Step 9: issue framing and solution planning. Requires --ref.
app.add_typer(issue_app, name="issue")

# Step 10c: the challenge lane's operator surface.
app.add_typer(queue_app, name="queue")


# Shared option: lets tests and CI point at a scratch ledger instead of the
# user's real one. Threaded explicitly through every command rather than held
# in a module global, so there is no hidden state to get wrong.
LedgerRootOption = Annotated[
    Path | None,
    typer.Option(
        "--ledger-root",
        help="Directory holding run records. Defaults to the per-user data directory.",
        envvar="ABCA_LEDGER_ROOT",
    ),
]


def _store(root: Path | None) -> LedgerStore:
    return LedgerStore(root)


# --------------------------------------------------------------------------
# version
# --------------------------------------------------------------------------

@app.command()
def version(json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False) -> None:
    """Show tool, schema and prompt-contract versions.

    All three are printed because they answer different questions and only
    two of them affect run hashes. See abca/version.py.
    """
    payload = {
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
    }
    if json_out:
        render.print_json(payload)
        return
    render.console.print(
        render.kv_table(
            "abca",
            [
                ("tool", TOOL_VERSION),
                ("schema", SCHEMA_VERSION),
                ("prompt contract", PROMPT_CONTRACT_VERSION),
            ],
        )
    )


# --------------------------------------------------------------------------
# ledger path / list / show / audit
# --------------------------------------------------------------------------

@ledger_app.command("path")
def ledger_path(ledger_root: LedgerRootOption = None) -> None:
    """Print the directory where run records are stored."""
    render.console.print(str(_store(ledger_root).root))


@ledger_app.command("list")
def ledger_list(
    ledger_root: LedgerRootOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Maximum rows.")] = 20,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List stored runs, newest first.

    Ordering comes free from the ULID filenames -- no index is consulted.
    Records that fail to load are skipped here and reported by ``ledger audit``.
    """
    store = _store(ledger_root)
    records = list(store.iter_records(limit=limit))

    if json_out:
        render.print_json(
            [
                {
                    "run_id": r.run_id,
                    "created_at": r.created_at.isoformat(),
                    "profile": r.config.profile.value,
                    "claims": len(r.result.claims),
                    "reproducible": r.reproducible,
                    "ledger_hash": r.ledger_hash,
                }
                for r in records
            ]
        )
        return

    if not records:
        render.console.print(f"[dim]No runs in {store.root}[/dim]")
        return

    from rich.table import Table

    # Column widths are pinned on the identifying and status columns so that a
    # narrow terminal truncates `profile` -- the least load-bearing field --
    # rather than eliding a run id or the reproducibility flag. A truncated
    # run id is a run id nobody can copy.
    table = Table(title=f"Runs in {store.root}")
    table.add_column("run id", no_wrap=True, min_width=26)
    table.add_column("created (UTC)", no_wrap=True, min_width=19)
    table.add_column("profile", overflow="ellipsis")
    table.add_column("claims", justify="right", no_wrap=True, min_width=6)
    table.add_column("repro", justify="center", no_wrap=True, min_width=5)
    table.add_column("ledger", no_wrap=True, min_width=12)

    for record in records:
        table.add_row(
            record.run_id,
            record.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            record.config.profile.value,
            str(len(record.result.claims)),
            "[green]yes[/green]" if record.reproducible else "[magenta]no[/magenta]",
            render.short_digest(record.ledger_hash),
        )
    render.console.print(table)


@ledger_app.command("show")
def ledger_show(
    run_id: Annotated[str, typer.Argument(help="Run ID (ULID). Case-insensitive.")],
    ledger_root: LedgerRootOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the full record as JSON.")] = False,
    stages: Annotated[bool, typer.Option("--stages", help="Also print the stage hash chain.")] = False,
) -> None:
    """Show one run record.

    Reads with ``audit=False`` so a damaged record can still be displayed --
    hiding a corrupt record behind an exception is the opposite of useful
    when the corruption is what you are investigating. The integrity result
    is shown as a field instead.
    """
    store = _store(ledger_root)
    try:
        record = store.read(run_id, audit=False)
    except RunNotFound as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except (LedgerCorrupt, InvalidRunId) as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=5) from exc

    if json_out:
        render.print_json(record.to_jsonable())
        return

    problems = record.audit()
    integrity = "[green]intact[/green]" if not problems else f"[red]{len(problems)} problem(s)[/red]"

    render.console.print(
        render.kv_table(
            f"run {record.run_id}",
            [
                ("created (UTC)", record.created_at.isoformat()),
                ("integrity", integrity),
                ("input", f"{record.input.kind.value} {record.input.locator}"),
                ("input hash", record.input.content_hash),
                ("profile", record.config.profile.value),
                ("seed / temp", f"{record.config.seed} / {record.config.temperature}"),
                ("models", ", ".join(f"{m.role}={m.name}" for m in record.config.models)),
                ("reproducible", "yes" if record.reproducible else "no (unpinnable weights)"),
                ("claims", str(len(record.result.claims))),
                ("verdicts", json.dumps(record.result.verdict_counts(), sort_keys=True)),
                ("types", json.dumps(record.result.type_counts(), sort_keys=True)),
                ("sources", str(len(record.sources))),
                ("input digest", record.input_digest),
                ("semantic digest", record.semantic_digest),
                ("output digest", record.output_digest),
                ("ledger hash", record.ledger_hash),
            ],
        )
    )

    if problems:
        render.console.print("\n[red]Integrity problems:[/red]")
        for problem in problems:
            render.console.print(f"  [red]-[/red] {problem}")

    if stages and record.stages:
        from rich.table import Table

        table = Table(title="stage chain")
        table.add_column("#", justify="right")
        table.add_column("stage")
        table.add_column("ms", justify="right")
        table.add_column("in", no_wrap=True)
        table.add_column("out", no_wrap=True)
        table.add_column("record", no_wrap=True)
        table.add_column("ok", justify="center")
        for stage in record.stages:
            table.add_row(
                str(stage.sequence),
                stage.name.value,
                str(stage.duration_ms),
                render.short_digest(stage.input_hash, width=8),
                render.short_digest(stage.output_hash, width=8),
                render.short_digest(stage.record_hash, width=8),
                "[green]y[/green]" if stage.is_self_consistent() else "[red]N[/red]",
            )
        render.console.print(table)


@ledger_app.command("audit")
def ledger_audit(
    run_id: Annotated[str | None, typer.Argument(help="Run to audit. Omit to audit every run.")] = None,
    ledger_root: LedgerRootOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Recompute every digest and chain link for one run or the whole store.

    This is the command that answers "has anything been edited since it was
    written?". Exit code 6 on any failure, so CI can gate on it -- which is
    which is how a publisher of verdicts is kept honest about its own records.
    """
    store = _store(ledger_root)
    run_ids = [run_id] if run_id else store.list_ids()

    results: list[dict] = []
    failures = 0

    for candidate in run_ids:
        entry: dict = {"run_id": candidate}
        try:
            record = store.read(candidate, audit=False)
        except RunNotFound as exc:
            entry.update(status="missing", problems=[str(exc)])
            failures += 1
        except (LedgerCorrupt, InvalidRunId) as exc:
            entry.update(status="unreadable", problems=[str(exc)])
            failures += 1
        else:
            problems = record.audit()
            entry.update(
                status="intact" if not problems else "tampered",
                problems=problems,
                ledger_hash=record.ledger_hash,
            )
            if problems:
                failures += 1
        results.append(entry)

    if json_out:
        render.print_json({"audited": len(results), "failures": failures, "results": results})
    else:
        if not results:
            render.console.print(f"[dim]No runs in {store.root}[/dim]")
        for entry in results:
            if entry["status"] == "intact":
                render.console.print(f"[green]{'OK':<9}[/green] {entry['run_id']}")
            else:
                render.console.print(f"[red]{entry['status'].upper():<9}[/red] {entry['run_id']}")
                for problem in entry["problems"]:
                    render.console.print(f"          [red]-[/red] {problem}")
        render.console.print(
            f"\naudited {len(results)}, "
            + (f"[red]{failures} failed[/red]" if failures else "[green]all intact[/green]")
        )

    if failures:
        raise typer.Exit(code=6)


# --------------------------------------------------------------------------
# schema export
# --------------------------------------------------------------------------

@app.command("schema")
def schema_export(
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write to a file instead of stdout.")] = None,
) -> None:
    """Emit the JSON Schema for a run record.

    Published so that a third-party verifier -- someone checking a claim
    somebody else published -- can validate a run record without installing this tool or
    trusting this codebase. "Re-run it yourself" only works if the format is
    documented.
    """
    document = RunRecord.model_json_schema()
    document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    document["title"] = f"abCA RunRecord {SCHEMA_VERSION}"
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)

    if out:
        out.write_text(text + "\n", encoding="utf-8")
        render.err_console.print(f"[green]wrote[/green] {out}")
    else:
        typer.echo(text)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

@app.command()
def verify(
    run_id: Annotated[str, typer.Argument(help="Run ID to verify.")],
    text: Annotated[
        str | None,
        typer.Option("--input", "-t", help="The original statement, for replay."),
    ] = None,
    input_file: Annotated[
        Path | None, typer.Option("--input-file", help="File containing the original statement.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config supplying backend connection details.",
                     envvar="ABCA_CONFIG"),
    ] = None,
    ledger_root: LedgerRootOption = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use cached sources only during replay.")
    ] = False,
    integrity_only: Annotated[
        bool,
        typer.Option("--integrity-only", help="Audit the record without replaying it."),
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Re-execute a recorded run and compare it against the original.

    Two halves, both real:

    **Integrity** recomputes every digest and walks the stage hash chain,
    detecting any edit to the stored record. Runs always.

    **Replay** rebuilds the recipe -- input text, pinned model weights, prompt
    hashes -- re-runs the pipeline and classifies the result as IDENTICAL,
    EQUIVALENT, DRIFTED, DIVERGENT or UNREPLAYABLE.

    Replay needs the original text. Run records store the input's HASH, not its
    content, so that publishing a record does not republish someone else's
    writing -- which means a third party verifying a published verdict has to
    bring the statement themselves and prove it hashes to what the record
    claims. Your own runs are cached locally and replay without it.
    """
    from abca.config import ConfigError, load_config
    from abca.ledger.inputs import InputStore
    from abca.ledger.verify import LedgerTampered, compare
    from abca.pipeline.replay import PipelineReplayer, ReplayImpossible, make_source_probe
    from abca.sources.registry import build_connectors

    store = _store(ledger_root)
    try:
        record = store.read(run_id, audit=False)
    except RunNotFound as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except (LedgerCorrupt, InvalidRunId) as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=5) from exc

    problems = record.audit()
    if problems:
        # A tampered record cannot serve as a baseline: comparing against
        # something that is not what it claims to be proves nothing.
        payload = {
            "run_id": record.run_id,
            "integrity": "tampered",
            "integrity_problems": problems,
            "outcome": None,
        }
        if json_out:
            render.print_json(payload)
        else:
            render.console.print(f"[red]TAMPERED[/red] {record.run_id}")
            for problem in problems:
                render.console.print(f"  [red]-[/red] {problem}")
            render.console.print(
                "\n[dim]A record that has been edited cannot be used as a "
                "verification baseline.[/dim]"
            )
        raise typer.Exit(code=6)

    if integrity_only:
        if json_out:
            render.print_json({
                "run_id": record.run_id, "integrity": "intact",
                "ledger_hash": record.ledger_hash, "outcome": None,
            })
        else:
            render.console.print(f"[green]intact[/green] {record.run_id}")
        raise typer.Exit(code=0)

    supplied = text
    if input_file is not None:
        supplied = input_file.read_text(encoding="utf-8")

    # The record says WHICH model ran; the local config says WHERE to reach it.
    # A missing config is not fatal -- the providers fall back to their default
    # endpoints, which is correct for a plain local Ollama.
    try:
        loaded = load_config(config, required=False)
    except ConfigError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=8) from exc

    connectors = build_connectors(offline=offline)
    replayer = PipelineReplayer(
        config=loaded, supplied_input=supplied, input_store=InputStore(),
        connectors=connectors, offline=offline,
    )

    try:
        replayed = replayer.replay(record)
    except ReplayImpossible as exc:
        # Distinguished from a failed comparison: nothing was compared. Exit 4
        # matches UNREPLAYABLE, because from the caller's point of view the
        # answer is the same -- this run could not be certified here.
        render.err_console.print(f"[magenta]cannot replay:[/magenta] {exc}")
        raise typer.Exit(code=4) from exc
    except LedgerTampered as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=6) from exc

    report = compare(record, replayed, probe=make_source_probe(connectors))

    if json_out:
        render.print_json(report.to_jsonable())
    else:
        render.console.print()
        render.console.print(render.outcome_text(report.outcome))
        render.console.print(
            render.kv_table(
                f"verify {record.run_id}",
                [
                    ("integrity", "intact"),
                    ("original ledger", record.ledger_hash),
                    ("replay ledger", replayed.ledger_hash),
                    ("reproducible", "yes" if record.reproducible else "no (hosted model)"),
                ],
            )
        )
        for note in report.notes:
            render.console.print(f"  • {note}")
        for field, (before, after) in report.recipe_diff.items():
            render.console.print(f"  [yellow]{field}[/yellow]: {before!r} -> {after!r}")
        for source in report.drifted_sources:
            render.console.print(
                f"  [yellow]drift[/yellow] {source['status']}: {source['url']}"
            )
        for difference in report.verdict_diff:
            original = difference["original"] or {}
            replay = difference["replay"] or {}
            render.console.print(
                f"  [red]{difference['claim_id']}[/red]: "
                f"{original.get('verdict', '<absent>')} -> {replay.get('verdict', '<absent>')}"
            )

    raise typer.Exit(code=report.exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
