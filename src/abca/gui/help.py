"""The Help tab: every command, its full ``--help``, searchable, in one place.

Generated from the same command tree as the forms, so it cannot disagree with
them. The reference text is the real ``--help`` output of each command --
rendered by typer into plain text with the terminal forced off -- not a second
description that would drift.
"""

from __future__ import annotations

import os
from functools import cache

from abca.gui.surface import CommandSpec, GroupSpec, Kind, ParamSpec

# Rendering help through typer must not pick up the desktop's colour settings.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")
os.environ.setdefault("NO_COLOR", "1")


@cache
def cli_help_text(path: tuple[str, ...]) -> str:
    """The exact ``abca <path> --help`` text, captured in-process."""
    from typer.testing import CliRunner

    from abca.cli.main import app

    result = CliRunner().invoke(app, [*path, "--help"], env={"COLUMNS": "96"})
    text = result.output.replace("Usage: root", "Usage: abca").replace("Usage: abca-cli", "Usage: abca")
    return text.strip() + "\n"


def parameter_line(spec: ParamSpec) -> str:
    """One reference line per parameter, in the form a person would type it."""
    if spec.positional:
        shape = spec.name.upper()
    elif spec.kind is Kind.FLAG:
        shape = spec.flag
    elif spec.kind is Kind.CHOICE:
        shape = f"{spec.flag} {{{'|'.join(spec.choices)}}}"
    elif spec.kind is Kind.MULTI:
        shape = f"{spec.flag} VALUE  (repeatable)"
    elif spec.kind is Kind.PATH:
        shape = f"{spec.flag} PATH"
    elif spec.kind in (Kind.INT, Kind.FLOAT):
        shape = f"{spec.flag} N"
    else:
        shape = f"{spec.flag} TEXT"
    if spec.short:
        shape = f"{spec.short}, {shape}"
    notes = []
    if spec.required:
        notes.append("required")
    if spec.default not in (None, False, "") and spec.kind is not Kind.FLAG:
        notes.append(f"default {spec.default}")
    if spec.envvar:
        notes.append(f"env {spec.envvar}")
    tail = f"  [{'; '.join(notes)}]" if notes else ""
    return f"  {shape:<34} {spec.help}{tail}"


def reference_text(groups: tuple[GroupSpec, ...]) -> str:
    """The whole CLI as one searchable page: every command, every parameter."""
    out = [
        "abCA COMMAND REFERENCE",
        "======================",
        "",
        "Every command below is also a form on the Commands tab. Whatever you",
        "fill in there becomes the command line shown above the Run button --",
        "copy it and it runs identically in a terminal. Search with the box",
        "above; F1 from any form opens this page at that command.",
        "",
    ]
    for group in groups:
        title = " ".join(group.path) or "top-level"
        out += [title.upper(), "-" * len(title)]
        if group.help:
            out += [group.help.split("\n\n", 1)[0], ""]
        for cmd in group.commands:
            out.append(f"abca {cmd.name}")
            first = (cmd.help or "").split("\n\n", 1)[0].replace("\n", " ")
            if first:
                out.append(f"  {first}")
            for spec in cmd.params:
                out.append(parameter_line(spec))
            out.append("")
        out.append("")
    return "\n".join(out)


def command_help(cmd: CommandSpec) -> str:
    """Full help for one command: the CLI's own --help, then the parameter table."""
    return cli_help_text(cmd.path) + "\n" + "\n".join(parameter_line(p) for p in cmd.params) + "\n"


__all__ = ["cli_help_text", "command_help", "parameter_line", "reference_text"]
