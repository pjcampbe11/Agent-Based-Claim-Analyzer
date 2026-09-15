"""Terminal rendering helpers.

Kept separate from command logic so the commands stay readable and so the
rendering can be swapped for a plain-text writer when stdout is not a TTY.

All output goes through a single :class:`rich.console.Console` instance, with
``soft_wrap`` disabled so that long digests are not wrapped mid-string --
a wrapped hash is a hash nobody can copy correctly, and these are meant to be
copied.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from abca.schema.enums import VerifyOutcome

#: stderr console for diagnostics, so `abca ledger show --json | jq` stays clean.
err_console = Console(stderr=True)
console = Console(soft_wrap=False)

#: Colors chosen so the meaning survives a monochrome terminal too -- every
#: status also carries a distinct word, never color alone.
_OUTCOME_STYLE: dict[VerifyOutcome, str] = {
    VerifyOutcome.IDENTICAL: "bold green",
    VerifyOutcome.EQUIVALENT: "green",
    VerifyOutcome.DRIFTED: "bold yellow",
    VerifyOutcome.DIVERGENT: "bold red",
    VerifyOutcome.UNREPLAYABLE: "bold magenta",
}


def print_json(value: Any) -> None:
    """Emit machine-readable JSON on stdout.

    ``sort_keys`` for stable diffs between invocations; this is presentation
    JSON, distinct from the canonical bytes used for hashing.
    """
    console.print_json(json.dumps(value, sort_keys=True, ensure_ascii=False))


def outcome_text(outcome: VerifyOutcome) -> Text:
    """Render a verification outcome with its style."""
    return Text(outcome.value, style=_OUTCOME_STYLE[outcome])


def short_digest(digest: str, *, width: int = 12) -> str:
    """Abbreviate a digest for table display.

    Strips the ``sha256:`` prefix and keeps the leading hex characters. Twelve
    is enough to distinguish runs by eye while staying narrow; the full value
    is always available from ``ledger show``.
    """
    body = digest.split(":", 1)[-1]
    return body[:width]


def kv_table(title: str, rows: list[tuple[str, str]]) -> Table:
    """Two-column key/value table used by the ``show`` and ``verify`` commands."""
    table = Table(title=title, show_header=False, box=None, pad_edge=False)
    table.add_column("field", style="dim", no_wrap=True)
    table.add_column("value", overflow="fold")
    for key, value in rows:
        table.add_row(key, value)
    return table


__all__ = ["console", "err_console", "kv_table", "outcome_text", "print_json", "short_digest"]
