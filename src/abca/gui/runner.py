"""Run the real CLI as a subprocess and stream its output to the window.

WHY A SUBPROCESS, NOT AN IN-PROCESS CALL
========================================
``typer.testing.CliRunner`` could invoke the app in this process and capture
its output, and it would be simpler. It is the wrong choice for three reasons:

* **Streaming.** A long analysis prints progress as it goes. In-process capture
  hands the window a block of text at the end; a subprocess pipe hands it each
  line as it happens, which is the difference between a window that looks hung
  for four minutes and one that does not.
* **Cancellation.** A subprocess can be terminated. A thread running the
  orchestrator cannot be, not cleanly.
* **Isolation.** The run ledger is written by the process doing the analysis.
  A crash in Tk must not be a crash in the process holding a half-written
  record with a hash chain.

HOW THE CLI IS LOCATED
======================
In development, ``[sys.executable, "-m", "abca.cli.main"]``. In a PyInstaller
bundle ``sys.executable`` is the GUI itself, so the console executable that the
same spec builds alongside it is used: ``abca.exe`` next to ``abca-gui.exe``.
The spec keeps them in one folder for exactly this reason, and
:func:`cli_command` says plainly which it found rather than guessing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path


class CliNotFound(RuntimeError):
    """The console executable is missing from a frozen bundle."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def cli_command() -> list[str]:
    """The argv prefix that runs the abCA CLI from where this program is."""
    if not is_frozen():
        return [sys.executable, "-m", "abca.cli.main"]
    here = Path(sys.executable).resolve().parent
    for name in ("abca.exe", "abca"):
        candidate = here / name
        if candidate.exists():
            return [str(candidate)]
    raise CliNotFound(
        f"no console executable next to {sys.executable}; the release zip "
        "ships abca.exe and abca-gui.exe together and they must stay together"
    )


class CliRun:
    """One running CLI invocation, streaming lines to a callback."""

    def __init__(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self.argv = argv
        self.env = env or {}
        self.cwd = cwd
        self.on_line = on_line
        self.on_exit = on_exit
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    @property
    def command(self) -> list[str]:
        return [*cli_command(), *self.argv]

    def start(self) -> None:
        merged = {**os.environ, **self.env}
        # Colour off and a wide terminal on: the output pane is a text widget,
        # not a terminal, and rich's escape codes would render as noise.
        merged.setdefault("NO_COLOR", "1")
        merged.setdefault("COLUMNS", "120")
        merged["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
        creation = 0
        if os.name == "nt":
            # No console window flashing behind a windowed executable.
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=merged,
            cwd=self.cwd,
            creationflags=creation,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self.on_line(line.rstrip("\n"))
        code = self._process.wait()
        self.on_exit(code)

    def cancel(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None


__all__ = ["CliNotFound", "CliRun", "cli_command", "is_frozen"]
