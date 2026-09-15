# PyInstaller spec for the Windows release: two executables, one folder.
#
#   abca.exe       the command line, console
#   abca-gui.exe   the desktop front end, windowed (no console flashes behind it)
#
#     pip install -e ".[release]"
#     pyinstaller abca.spec
#     -> dist/abca/            (both executables and their shared runtime)
#
# ONE FOLDER, NOT TWO ONE-FILE BINARIES
# ====================================
# The GUI does not re-implement a single command. It builds the argv a person
# would have typed and runs the real CLI as a subprocess -- so a result produced
# from the window carries the same ledger hashes as the same command typed by
# hand. In a frozen build that means abca-gui.exe has to FIND abca.exe, and it
# looks in its own folder (abca.gui.runner.cli_command). A one-file build would
# unpack each executable to a different temp directory and they would never
# meet. So this spec emits a COLLECT: one folder, both executables, one copy of
# the Python runtime between them.
#
# WHY THE PROMPTS MUST BE BUNDLED
# ==============================
# `abca.prompts` loads its templates from `src/abca/prompts/` as PACKAGE DATA
# and records the SHA-256 of each file it used in every run record. A frozen
# binary that omitted them would fail at run time, halfway through an analysis,
# with a missing-file error -- and a binary that shipped DIFFERENT prompt bytes
# than the wheel would silently produce run hashes that no wheel-based verifier
# could reproduce. The hash is the whole credibility argument, so the bytes
# travel with the executable.
#
# WHAT IS DELIBERATELY NOT BUNDLED
# ================================
# corpus/, identity/, evals/ -- these are data the CLI reads from a checkout
# by relative path when a command asks for them. A release is a tool, not a
# repository snapshot; a user who wants those data sets clones the repo and
# points the option at the directory. The GUI's dark theme mirrors the identity
# tokens by value for exactly this reason, and a test holds it to them.

from PyInstaller.utils.hooks import collect_data_files

prompts = collect_data_files("abca", includes=["prompts/**/*.md"])

# Imported lazily by abca.providers.registry so a missing optional backend
# never breaks import. PyInstaller cannot see a lazy import, so every backend
# is named here -- otherwise the frozen build would support only whichever
# provider happened to be imported at build time. Same for the GUI's help
# renderer, which imports typer.testing on first use.
hidden = [
    "abca.providers.ollama",
    "abca.providers.llamacpp",
    "abca.providers.openai_compat",
    "abca.providers.anthropic",
    "abca.gui.app",
    "abca.gui.help",
    "typer.testing",
]

cli_analysis = Analysis(
    ["src/abca/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=prompts,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The CLI never imports Tk; keeping it out of this analysis keeps the
    # console executable's own module graph honest. `pypdf` is the optional
    # [pdf] extra, excluded so a build machine that happens to have it does
    # not produce a binary whose behaviour differs from one that does not.
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "pypdf"],
    noarchive=False,
)

gui_analysis = Analysis(
    ["src/abca/gui/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=prompts,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pytest", "pypdf"],
    noarchive=False,
)

# Shared modules are deduplicated into one runtime so the folder holds one
# copy of Python, pydantic and typer, not two.
MERGE((cli_analysis, "abca", "abca"), (gui_analysis, "abca-gui", "abca-gui"))

cli_pyz = PYZ(cli_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="abca",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries trip antivirus heuristics
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="abca-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,      # windowed: the output pane is the console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    strip=False,
    upx=False,
    name="abca",
)
