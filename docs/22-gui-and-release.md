# Step 12 — the desktop front end and the Windows release

`src/abca/gui/` · `abca.spec` · `scripts/build_release.py` · CI job `release`

---

## 1. What was built

A tkinter window that exposes every CLI command as a form, and a PyInstaller
pipeline that freezes the CLI and the window into one Windows folder and proves
the frozen result works before it is zipped.

| | |
|---|---|
| `abca-gui` / `python -m abca.gui` | the window |
| `abca.spec` | one `COLLECT` folder: `abca.exe` (console) + `abca-gui.exe` (windowed) |
| `scripts/build_release.py` | build → execute the frozen binaries → zip only on pass |
| `.github/workflows/ci.yml` → `release` | runs that on `windows-latest` every push; attaches the zip to a GitHub Release on a `v*` tag |

## 2. Three design rules

**Generated, not copied.** `abca.gui.surface.describe_cli()` walks the Typer
command tree at start-up into plain dataclasses; every form is built from that.
There is no hand-written option list anywhere in the package. That is the only
way "the GUI does everything the CLI does" can be tested rather than promised:
`tests/test_gui.py` walks the click tree independently and asserts every command
(29) and every parameter (106) has a control.

**Never re-implements a command.** `abca.gui.argv.build_argv()` produces the
argv a person would have typed; the window shows it (quoted for the user's
shell, with any environment variables in front) and runs the real CLI as a
subprocess. A run from the window therefore writes the same ledger record with
the same hashes as the same command typed by hand, and nothing the GUI does is
unreproducible from a terminal. A parametrised test feeds built argvs back to
click and asserts they parse to the form's values.

**Standard library only.** tkinter, for the reason the CLI has three
dependencies. The dark theme (`abca.gui.theme`) mirrors the brand tokens by
value because `src/` is forbidden from reading the identity directory; a test
reads both sides and asserts they agree, and measures every text/ground pair
against WCAG AA with the same contrast function the brand guide is held to.

## 3. Things found while building it

- **typer vendors its own click.** `isinstance(param.type, click.Choice)` was
  quietly False for every parameter and the first version rendered all 106 as
  text boxes. Kinds are duck-typed by class name; a test pins that the surface
  is not all-text.
- **A floating help pop-up is not navigable help.** Replaced with an in-tab
  layout: command index, the selected command's full `--help`, and a
  searchable reference with matches highlighted. F1 from any form lands there.
- **A one-file freeze would break the GUI.** `abca-gui.exe` finds `abca.exe`
  in its own folder; two one-file binaries unpack to different temp
  directories and never meet. Hence one `COLLECT` with a `MERGE`d runtime.
- **A wrong schema name in the release check** was refused by the frozen CLI
  — the check was wrong, the binary was right, and the release script did not
  write a zip. That is the behaviour it exists for.
- **The packaging test caught a malformed extra** (`[project.optional-dependencies.release]`
  as a table) before CI did.

## 4. Verified

Linux, in a clean Python 3.12 venv under Xvfb: the window builds all 29 forms;
`version --json` runs through the subprocess runner and streams back; the
frozen `abca` answers `version`, `schema`, every group's `--help` and a
grammar; the frozen `abca-gui --abca-self-check` constructs the whole window,
locates `abca` beside it, runs a command through it and exits 0; the zip is
written. Windows verification is the CI `release` job, which has a display and
runs the Tk tests for real.

## 5. Open

1. **Code signing.** Unsigned executables draw SmartScreen warnings. A
   certificate is a project decision, not a build one; the spec has the field.
2. **`--stdin` in the window.** Exposed as a flag because the CLI has it; the
   window has no stdin to give it. Harmless, but a form could hide it.
3. **A frozen `levelset`.** `tools/levelset.py` is outside the package by
   design and is not in the bundle. It runs from a checkout.
