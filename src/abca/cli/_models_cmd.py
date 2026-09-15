"""``abca models`` and ``abca config`` commands.

Kept out of ``main.py`` so that module stays a thin router. These commands are
the operator's window into the two questions step 2 exists to answer:

* **Which model is actually going to run?** (``models show``)
* **Can a third party verify the result?** (the ``pinned`` column everywhere)

Every display path goes through :meth:`abca.config.ModelSpec.redacted`, so an
API key cannot reach the terminal, a screenshot, or a pasted bug report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from abca.cli import _render as render
from abca.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    write_starter_config,
)
from abca.providers.base import ProviderError
from abca.providers.registry import build_provider, build_registry

models_app = typer.Typer(help="Inspect configured models and backends.", no_args_is_help=True)
config_app = typer.Typer(help="Manage the abCA configuration file.", no_args_is_help=True)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config.toml.", envvar="ABCA_CONFIG"),
]


def _load(path: Path | None, *, required: bool = True) -> Config:
    """Load config, converting errors into clean CLI failures.

    A stack trace for a TOML typo is hostile; the message from
    :class:`~abca.config.ConfigError` already names the file and the fix.
    """
    try:
        return load_config(path, required=required)
    except ConfigError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=8) from exc


def _emit_warnings(config: Config) -> None:
    """Surface config warnings on stderr so ``--json`` stdout stays clean."""
    for warning in config.warnings:
        render.err_console.print(f"[yellow]warning:[/yellow] {warning}")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


@config_app.command("path")
def config_path() -> None:
    """Print where abCA looks for its configuration."""
    render.console.print(str(default_config_path()))


@config_app.command("init")
def config_init(
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented starter configuration.

    The starter file documents all four backends including credential handling
    for OpenAI and Claude, so the common next question -- "where do I put my
    API key?" -- is answered in the file itself rather than only in the docs.
    """
    try:
        written = write_starter_config(config, overwrite=force)
    except ConfigError as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=8) from exc
    render.console.print(f"[green]wrote[/green] {written}")
    render.console.print("Edit it, then run [bold]abca models check[/bold] to verify the backends.")


@config_app.command("show")
def config_show(
    config: ConfigOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show the effective configuration, with credentials redacted."""
    loaded = _load(config)
    _emit_warnings(loaded)

    if json_out:
        render.print_json(loaded.redacted())
        return

    defaults = loaded.defaults
    render.console.print(
        render.kv_table(
            f"config: {loaded.source}",
            [
                ("profile", defaults.profile.value),
                ("seed / temperature", f"{defaults.seed} / {defaults.temperature}"),
                ("max claims", str(defaults.max_claims)),
                ("reading level", f"grade {defaults.reading_level}"),
                ("repair attempts", str(defaults.max_attempts)),
            ],
        )
    )

    table = Table(title="roles")
    table.add_column("role", no_wrap=True, min_width=13)
    table.add_column("provider", no_wrap=True, min_width=8)
    table.add_column("model", overflow="ellipsis")
    table.add_column("credential", no_wrap=True, min_width=10)
    for role, spec in sorted(loaded.models.items()):
        status = spec.credential_status()
        style = "red" if status.startswith("missing") else ""
        table.add_row(role, spec.provider, spec.model, f"[{style}]{status}[/{style}]" if style else status)
    render.console.print(table)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


@models_app.command("check")
def models_check(
    config: ConfigOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Probe every configured backend and report whether it is usable.

    Run this after editing the config. It front-loads every failure a run
    would otherwise hit -- a stopped Ollama, an unpulled model, a missing API
    key, a closed SSH tunnel -- and reports them all at once rather than one
    per attempt.

    Exit code 9 on any failure, so it can gate a deployment.
    """
    loaded = _load(config)
    _emit_warnings(loaded)

    try:
        registry = build_registry(loaded)
    except (ConfigError, ProviderError) as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    results = registry.health_check()
    healthy = [role for role, status in results.items() if status == "ok"]

    # Identity resolution is attempted only for healthy roles; asking an
    # unreachable backend for its digest would just produce a second, noisier
    # copy of the same failure.
    pinning: dict[str, str | None] = {}
    for role in healthy:
        try:
            pinning[role] = registry.get(role).identity().weights_hash
        except ProviderError as exc:
            results[role] = str(exc)
            pinning[role] = None

    failures = [role for role, status in results.items() if status != "ok"]

    if json_out:
        render.print_json(
            {
                "config": str(loaded.source),
                "roles": {
                    role: {
                        "status": status,
                        "model": loaded.models[role].model,
                        "provider": loaded.models[role].provider,
                        "weights_hash": pinning.get(role),
                        "pinned": pinning.get(role) is not None,
                    }
                    for role, status in sorted(results.items())
                },
                "failures": failures,
                "fully_pinnable": not failures and all(pinning.values()),
            }
        )
    else:
        table = Table(title="backend check")
        table.add_column("role", no_wrap=True, min_width=13)
        table.add_column("model", overflow="ellipsis")
        table.add_column("status", no_wrap=True, min_width=6)
        table.add_column("pinned", justify="center", no_wrap=True, min_width=6)
        for role, status in sorted(results.items()):
            ok = status == "ok"
            is_pinned = pinning.get(role) is not None
            table.add_row(
                role,
                loaded.models[role].model,
                "[green]ok[/green]" if ok else "[red]FAIL[/red]",
                ("[green]yes[/green]" if is_pinned else "[magenta]no[/magenta]") if ok else "-",
            )
        render.console.print(table)

        for role in failures:
            render.console.print(f"\n[red]{role}:[/red] {results[role]}")

        if not failures:
            unpinned = [role for role, value in pinning.items() if value is None]
            if unpinned:
                render.console.print(
                    f"\n[magenta]Note:[/magenta] {', '.join(unpinned)} use hosted models whose "
                    "weights cannot be pinned. Runs using them are recorded as "
                    "[bold]reproducible: false[/bold] and cannot be certified by a third party."
                )
            else:
                render.console.print(
                    "\n[green]All models pinnable.[/green] Runs will be recorded as reproducible."
                )

    if failures:
        raise typer.Exit(code=9)


@models_app.command("show")
def models_show(
    role: Annotated[str, typer.Argument(help="Pipeline role, e.g. adjudicator.")],
    config: ConfigOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Resolve one role's model identity from its live backend.

    This is the command that answers "what exactly will run, and can anyone
    check it?" -- the weights hash shown here is the value that ends up in
    every run record produced with this role.
    """
    loaded = _load(config)
    try:
        spec = loaded.spec(role)
        provider = build_provider(spec)
        provider.health()
        identity = provider.identity()
    except (ConfigError, ProviderError) as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    payload = {
        **spec.redacted(),
        "resolved": {
            "provider": identity.provider,
            "name": identity.name,
            "weights_hash": identity.weights_hash,
            "quantization": identity.quantization,
            "context_length": identity.context_length,
            "pinned": identity.is_pinnable,
        },
    }
    if json_out:
        render.print_json(payload)
        return

    render.console.print(
        render.kv_table(
            f"{role}: {identity.name}",
            [
                ("provider", identity.provider),
                ("credential", spec.credential_status()),
                ("quantization", identity.quantization or "-"),
                ("context length", str(identity.context_length or "-")),
                ("weights hash", identity.weights_hash or "[magenta]none (hosted)[/magenta]"),
                (
                    "verifiable",
                    "[green]yes[/green]"
                    if identity.is_pinnable
                    else "[magenta]no - runs marked reproducible: false[/magenta]",
                ),
            ],
        )
    )


@models_app.command("list")
def models_list(
    role: Annotated[str, typer.Option("--role", "-r", help="Role whose backend to query.")] = "adjudicator",
    config: ConfigOption = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List the models installed on a role's backend."""
    loaded = _load(config)
    try:
        provider = build_provider(loaded.spec(role))
        lister = getattr(provider, "list_models", None)
        if lister is None:
            render.err_console.print(
                f"[yellow]{loaded.spec(role).provider} does not support listing models[/yellow]"
            )
            raise typer.Exit(code=0)
        entries = lister()
    except (ConfigError, ProviderError) as exc:
        render.err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=9) from exc

    if json_out:
        render.print_json(entries)
        return

    table = Table(title=f"models available to role '{role}'")
    table.add_column("name", overflow="ellipsis", min_width=20)
    table.add_column("quant", no_wrap=True)
    table.add_column("params", no_wrap=True)
    table.add_column("digest", no_wrap=True, min_width=12)
    for entry in entries:
        digest = entry.get("digest")
        table.add_row(
            str(entry.get("name")),
            str(entry.get("quantization") or "-"),
            str(entry.get("parameter_size") or "-"),
            render.short_digest(digest) if digest else "[magenta]none[/magenta]",
        )
    render.console.print(table)


@models_app.command("grammar")
def models_grammar(
    model_name: Annotated[str, typer.Argument(help="Schema to compile: claim | analysis | citation.")] = "claim",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write to a file.")] = None,
) -> None:
    """Emit the GBNF grammar for one of abCA's output schemas.

    Useful for inspecting exactly what constrained decoding will permit, and
    for feeding to ``llama-server`` directly when debugging a model that keeps
    producing invalid output.
    """
    from abca.providers.grammar import gbnf_for_model
    from abca.schema.core import AnalysisResult, Citation, Claim

    schemas = {"claim": Claim, "analysis": AnalysisResult, "citation": Citation}
    target = schemas.get(model_name.lower())
    if target is None:
        render.err_console.print(
            f"[red]unknown schema {model_name!r}; expected one of: {', '.join(schemas)}[/red]"
        )
        raise typer.Exit(code=1)

    grammar = gbnf_for_model(target)
    if out:
        out.write_text(grammar, encoding="utf-8")
        render.err_console.print(f"[green]wrote[/green] {out} ({len(grammar.splitlines())} rules)")
    else:
        typer.echo(grammar)


__all__ = ["config_app", "models_app"]
