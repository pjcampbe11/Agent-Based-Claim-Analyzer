"""Build the Windows release folder and zip it, then prove the zip works.

    python scripts/build_release.py            # -> dist/abca-<version>-<platform>.zip
    python scripts/build_release.py --check    # build, then run the smoke checks only

WHAT "PROVE" MEANS HERE
=======================
A release that was built is not a release that works. After PyInstaller runs,
this script executes the frozen CLI from the dist folder -- `version --json`,
`schema`, `--help` for every command group -- and asserts on the output. On a
machine with a display it also constructs the frozen GUI's window for one
frame. A release zip is only written once every check passes, so a broken build
cannot be uploaded by accident; the CI job that calls this uploads whatever it
produces, and it produces nothing on failure.

The checks run against the FROZEN executables, not the source tree. That is the
point: a missing hidden import or an unbundled data file shows up only there.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "abca"


def sh(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(args))
    return subprocess.run(list(args), check=True, text=True, capture_output=True, **kwargs)


def build() -> None:
    if (ROOT / "build").exists():
        shutil.rmtree(ROOT / "build")
    if DIST.exists():
        shutil.rmtree(DIST)
    sh(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "abca.spec", cwd=str(ROOT))


def exe(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = DIST / f"{name}{suffix}"
    if not path.exists():
        raise SystemExit(f"expected {path} after build; the spec did not produce it")
    return path


def check() -> str:
    """Exercise the frozen binaries. Returns the version string they report."""
    cli = exe("abca")
    gui = exe("abca-gui")
    env = {**os.environ, "NO_COLOR": "1", "_TYPER_FORCE_DISABLE_TERMINAL": "1"}

    out = sh(str(cli), "version", "--json", env=env).stdout
    versions = json.loads(out)
    for key in ("tool_version", "schema_version", "prompt_contract_version"):
        assert key in versions, f"frozen `version --json` lacks {key}: {out}"
    print(f"  frozen CLI reports {versions}")

    schema = sh(str(cli), "schema", env=env).stdout
    assert '"$schema"' in schema or '"properties"' in schema, "frozen `schema` output is not a schema"

    # Every command group's help must render: this is where a missing hidden
    # import surfaces, because typer imports the command module to render it.
    for group in ("analyze", "explain", "ledger", "models", "config", "sources",
                  "issue", "queue", "verify"):
        text = sh(str(cli), group, "--help", env=env).stdout
        assert "Usage:" in text, f"frozen `{group} --help` did not render"
    print("  every command group renders its help from the frozen CLI")

    # The prompts travelled: the doctrine check reads them from the bundle.
    # Exercised through `explain --help`'s import chain above, and directly here
    # via a command that must load a prompt file to answer.
    text = sh(str(cli), "models", "grammar", "analysis", env=env).stdout
    assert "root" in text, "frozen `models grammar` produced no grammar"
    print("  package data (prompts, grammars) is present in the bundle")

    # The GUI finds its neighbour. Run the frozen GUI module's resolver.
    assert gui.exists()
    if os.environ.get("DISPLAY") or os.name == "nt":
        probe = sh(str(gui), "--abca-self-check", env=env, timeout=60)
        assert "self-check ok" in probe.stdout, probe.stdout + probe.stderr
        print("  frozen GUI constructed its window and located abca beside it")
    else:
        print("  (no display: frozen GUI window not constructed here)")
    return str(versions["tool_version"])


def package(version: str) -> Path:
    tag = f"{platform.system().lower()}-{platform.machine().lower()}"
    target = ROOT / "dist" / f"abca-{version}-{tag}.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                zf.write(path, Path("abca") / path.relative_to(DIST))
        zf.writestr("abca/README-RELEASE.txt", RELEASE_NOTE)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
    return target


RELEASE_NOTE = """\
abCA -- agent-based Claim Analyzer

  abca.exe       the command line.  abca --help
  abca-gui.exe   the desktop front end: every command as a form, dark by
                 default, F1 for help on any command.

Keep the two executables in this folder together: the GUI runs the CLI beside
it, so a run started from the window writes the same ledger record -- with the
same hashes -- as the same command typed in a terminal. The command line it
ran is shown above the Run button and can be copied.

A model is required for analysis. Install Ollama (https://ollama.com), then:
  abca config init
  abca models check

Run records are kept in %LOCALAPPDATA%\\abca\\runs. `abca verify <run id>`
re-executes any of them.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="build and check, do not zip")
    args = parser.parse_args()
    build()
    version = check()
    if not args.check:
        package(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
