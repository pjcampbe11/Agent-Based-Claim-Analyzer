"""Walk the CLI's command tree into a plain, testable description.

This is the whole basis of the GUI's "does everything the CLI does" claim, so it
is deliberately boring: no Tk, no I/O, a pure function from the Typer app to a
tree of frozen dataclasses. ``tests/test_gui.py`` calls it and asserts the tree
matches the click tree parameter-for-parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from typer.main import get_command


class Kind(StrEnum):
    """What control a parameter needs. Derived from the click type, never guessed."""

    FLAG = "flag"          # --json                     -> checkbox
    TEXT = "text"          # --text "..."               -> entry (multi-line for long inputs)
    INT = "int"            # --limit 20                 -> spinbox
    FLOAT = "float"        # --temperature 0.0          -> entry with float validation
    PATH = "path"          # --config path              -> entry + Browse
    CHOICE = "choice"      # --profile fast|standard    -> dropdown
    MULTI = "multi"        # --ref a --ref b            -> one line per value


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One CLI parameter, with everything a form needs to render and emit it."""

    name: str                       # click's python name, e.g. "ledger_root"
    flag: str                       # the long option, e.g. "--ledger-root"; "" for arguments
    short: str                      # "-t" or ""
    kind: Kind
    help: str
    required: bool
    positional: bool
    default: Any = None
    choices: tuple[str, ...] = ()
    envvar: str | None = None
    #: For INT/FLOAT with a click range: (min, max); None for unbounded.
    bounds: tuple[float | None, float | None] | None = None
    #: A hint that the value is a long piece of text (statement to analyze).
    long_text: bool = False
    #: For PATH: True when the CLI expects an existing file (Browse -> open),
    #: False when it writes (Browse -> save-as), None when unknown.
    exists: bool | None = None

    @property
    def label(self) -> str:
        return self.flag or self.name.replace("_", " ")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One runnable command: its path and the parameters it takes."""

    path: tuple[str, ...]           # ("ledger", "show")
    help: str
    params: tuple[ParamSpec, ...]
    #: Set for commands that mutate state or need a model, so the window can
    #: colour the Run button and confirm before destructive ones.
    destructive: bool = False

    @property
    def name(self) -> str:
        return " ".join(self.path)


@dataclass(frozen=True, slots=True)
class GroupSpec:
    path: tuple[str, ...]
    help: str
    commands: tuple[CommandSpec, ...] = field(default_factory=tuple)


#: Long-text inputs, by parameter name. This is the ONE place the GUI knows
#: anything the click tree does not say: that a statement to analyze deserves a
#: multi-line box rather than a one-line entry. Purely cosmetic -- the argv is
#: identical either way.
_LONG_TEXT = {"text", "input", "statement"}

#: Commands whose effects are irreversible or slow enough to confirm first.
_DESTRUCTIVE = {("sources", "clear"), ("queue", "promote"), ("queue", "release"),
                ("config", "init")}


def _type_name(ptype: Any) -> str:
    """The click type's class name, with typer's ``Typer`` prefix stripped.

    Duck-typed on purpose. Recent typer versions vendor their own copy of click
    as ``typer._click``, so ``isinstance(param.type, click.Choice)`` is quietly
    False for every parameter -- the first version of this module rendered all
    106 of them as plain text boxes. Class NAMES are stable across both copies;
    class identities are not.
    """
    name = type(ptype).__name__
    return name[5:] if name.startswith("Typer") else name


def _kind_of(param: Any) -> tuple[Kind, tuple[str, ...], tuple | None, bool | None]:
    """Map a click type onto a control kind. Returns (kind, choices, bounds, exists)."""
    ptype = param.type
    tname = _type_name(ptype)
    if getattr(param, "is_flag", False) or tname == "BoolParamType":
        return Kind.FLAG, (), None, None
    if getattr(param, "multiple", False):
        return Kind.MULTI, (), None, None
    if tname == "Choice":
        return Kind.CHOICE, tuple(str(c) for c in ptype.choices), None, None
    if tname == "Path":
        exists = getattr(ptype, "exists", False)
        return Kind.PATH, (), None, (True if exists else None)
    if tname == "IntRange":
        return Kind.INT, (), (getattr(ptype, "min", None), getattr(ptype, "max", None)), None
    if tname == "FloatRange":
        return Kind.FLOAT, (), (getattr(ptype, "min", None), getattr(ptype, "max", None)), None
    if tname == "IntParamType":
        return Kind.INT, (), None, None
    if tname == "FloatParamType":
        return Kind.FLOAT, (), None, None
    return Kind.TEXT, (), None, None


def _describe_param(param: Any) -> ParamSpec:
    kind, choices, bounds, exists = _kind_of(param)
    long_opts = [o for o in param.opts if o.startswith("--")]
    short_opts = [o for o in param.opts if o.startswith("-") and not o.startswith("--")]
    positional = getattr(param, "param_type_name", "") == "argument"
    help_text = (getattr(param, "help", None) or "").strip()
    default = param.default
    if callable(default):
        default = None
    return ParamSpec(
        name=param.name or "",
        flag=long_opts[0] if long_opts else "",
        short=short_opts[0] if short_opts else "",
        kind=kind,
        help=help_text,
        required=bool(param.required),
        positional=positional,
        default=default,
        choices=choices,
        envvar=param.envvar if isinstance(param.envvar, str) else None,
        bounds=bounds,
        long_text=(param.name or "") in _LONG_TEXT and kind is Kind.TEXT,
        exists=exists,
    )


def _describe_command(cmd: Any, path: tuple[str, ...]) -> CommandSpec:
    params = tuple(
        _describe_param(p) for p in cmd.params if p.name != "help"
    )
    return CommandSpec(
        path=path,
        help=(cmd.help or "").strip(),
        params=params,
        destructive=path in _DESTRUCTIVE,
    )


def describe_cli(app: Any | None = None) -> tuple[GroupSpec, ...]:
    """The whole CLI as a tree of groups and commands.

    A Typer sub-app with a callback and no sub-commands -- ``analyze`` and
    ``explain`` are built that way -- is itself the runnable command, so it
    is described as a one-command group named after itself. Top-level
    commands such as ``version`` are grouped under an empty path so the window
    can show them together.
    """
    if app is None:
        from abca.cli.main import app as default_app
        app = default_app

    root = get_command(app)
    groups: list[GroupSpec] = []
    loose: list[CommandSpec] = []

    for name, cmd in root.commands.items():   # type: ignore[attr-defined]
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            groups.append(GroupSpec(
                path=(name,),
                help=(cmd.help or "").strip(),
                commands=tuple(
                    _describe_command(sub, (name, sub_name))
                    for sub_name, sub in subcommands.items()
                ),
            ))
        elif subcommands is not None:
            # A callback-only group: the group IS the command.
            groups.append(GroupSpec(
                path=(name,),
                help=(cmd.help or "").strip(),
                commands=(_describe_command(cmd, (name,)),),
            ))
        else:
            loose.append(_describe_command(cmd, (name,)))

    if loose:
        groups.insert(0, GroupSpec(path=(), help="Top-level commands", commands=tuple(loose)))
    return tuple(groups)


def all_commands(groups: tuple[GroupSpec, ...] | None = None) -> list[CommandSpec]:
    groups = groups if groups is not None else describe_cli()
    return [c for g in groups for c in g.commands]


__all__ = ["CommandSpec", "GroupSpec", "Kind", "ParamSpec", "all_commands", "describe_cli"]
