"""Entry point for ``python -m abca`` and for the frozen executable.

Separate from ``abca.cli.main`` so PyInstaller has a module to analyze that is
not also the Typer app definition -- a spec pointed straight at ``main.py``
picks up the decorators at import time and produces confusing tracebacks when
anything in the command tree fails to import.
"""

from __future__ import annotations

from abca.cli.main import app


def run() -> None:
    app()


if __name__ == "__main__":
    run()
