"""Turn form values into the exact command line the CLI would parse.

This module is what makes the window honest. It does not call into the pipeline;
it produces an ``argv`` -- the same list of strings a person would have typed --
and the window both displays that (copy-paste-able) and runs it. So a result
produced from the GUI is, by construction, reproducible from a terminal, and a
test can parse the built argv with click itself and assert it round-trips.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from abca.gui.surface import CommandSpec, Kind, ParamSpec


class FormError(ValueError):
    """A value the CLI would reject, caught before a process is spawned."""


def _is_unset(value: Any) -> bool:
    return value is None or value == "" or value is False or value == []


def _check_number(spec: ParamSpec, value: str) -> str:
    try:
        number = int(value) if spec.kind is Kind.INT else float(value)
    except ValueError as exc:
        kind = "an integer" if spec.kind is Kind.INT else "a number"
        raise FormError(f"{spec.label}: {value!r} is not {kind}") from exc
    if spec.bounds:
        low, high = spec.bounds
        if low is not None and number < low:
            raise FormError(f"{spec.label}: {number} is below the minimum {low}")
        if high is not None and number > high:
            raise FormError(f"{spec.label}: {number} is above the maximum {high}")
    return str(number) if spec.kind is Kind.INT else value


def build_argv(command: CommandSpec, values: dict[str, Any]) -> list[str]:
    """The argv for ``command`` given ``values`` keyed by ``ParamSpec.name``.

    Rules, each of which mirrors what typing the command would mean:

    * A flag that is unchecked is simply absent. The CLI's default applies.
    * An option left blank is absent, for the same reason -- the window never
      sends a default the CLI would have supplied itself, so ``--limit`` with
      the box showing ``20`` and ``--limit`` never mentioned are the same run.
    * A required argument left blank is an error HERE, with the label, rather
      than a usage error from a subprocess the user cannot see.
    * ``MULTI`` values are one ``--ref X`` per non-blank line.
    """
    argv: list[str] = list(command.path)
    positionals: list[tuple[ParamSpec, str]] = []

    for spec in command.params:
        value = values.get(spec.name)

        if spec.positional:
            if _is_unset(value):
                if spec.required:
                    raise FormError(f"{spec.label} is required")
                continue
            positionals.append((spec, str(value)))
            continue

        if spec.kind is Kind.FLAG:
            if value:
                argv.append(spec.flag)
            continue

        if spec.kind is Kind.MULTI:
            lines = value if isinstance(value, list) else str(value or "").splitlines()
            for line in lines:
                if line.strip():
                    argv += [spec.flag, line.strip()]
            continue

        if _is_unset(value):
            if spec.required:
                raise FormError(f"{spec.label} is required")
            continue

        text = str(value)
        if spec.kind in (Kind.INT, Kind.FLOAT):
            text = _check_number(spec, text)
        if spec.kind is Kind.CHOICE and text not in spec.choices:
            raise FormError(f"{spec.label}: {text!r} is not one of {', '.join(spec.choices)}")
        argv += [spec.flag, text]

    # Positionals go last, after ``--`` if any of them could be mistaken for an
    # option. None can today (run ids, roles, locators), but the guard costs
    # nothing and a locator like ``-5 ILCS`` would otherwise be a usage error.
    if positionals:
        if any(text.startswith("-") for _spec, text in positionals):
            argv.append("--")
        argv += [text for _spec, text in positionals]
    return argv


def render_command(argv: list[str], *, program: str = "abca", windows: bool = False) -> str:
    """The copy-paste-able form. Quoted for the shell the user is sitting at."""
    if windows:
        return " ".join([program, *(subprocess.list2cmdline([a]) for a in argv)])
    return " ".join([program, *(shlex.quote(a) for a in argv)])


__all__ = ["FormError", "build_argv", "render_command"]
