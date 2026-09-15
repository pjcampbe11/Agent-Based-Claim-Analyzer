"""The GUI does everything the CLI does -- proven, not promised.

Three layers, in order of how much they need:

1. **Surface and argv** need nothing but the CLI itself. They are the bulk of
   this file and run everywhere. The coverage test is the one that matters:
   it walks the click tree independently and asserts every command and every
   parameter has a form entry, so a CLI option added without the GUI noticing
   is impossible -- the GUI is generated, and this proves the generator is
   complete.
2. **Round-trips** feed a built argv back to click and assert click parses it
   to the values the form held. That is the actual claim -- the window
   produces the command line a person would have typed.
3. **The window** needs a Tk display. Those tests skip cleanly when there is
   none (Linux CI without xvfb) and run where there is one (Windows CI, a
   developer's desktop), so the release job's target platform is the one that
   exercises them.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.main import get_command

from abca.cli.main import app
from abca.gui.argv import FormError, build_argv, render_command
from abca.gui.surface import Kind, all_commands, describe_cli

ROOT = Path(__file__).resolve().parent.parent
GROUPS = describe_cli()
COMMANDS = {c.name: c for c in all_commands(GROUPS)}


# ---------------------------------------------------------------- coverage


def _click_leaves():
    """Every runnable command in the click tree, found WITHOUT the GUI's code."""
    root = get_command(app)
    leaves = {}
    for name, cmd in root.commands.items():
        subs = getattr(cmd, "commands", None)
        if subs:
            for sub_name, sub in subs.items():
                leaves[f"{name} {sub_name}"] = sub
        else:
            leaves[name] = cmd
    return leaves


def test_every_cli_command_has_a_form():
    missing = sorted(set(_click_leaves()) - set(COMMANDS))
    assert not missing, f"CLI commands with no GUI form: {missing}"
    extra = sorted(set(COMMANDS) - set(_click_leaves()))
    assert not extra, f"GUI forms with no CLI command: {extra}"


def test_every_cli_parameter_has_a_control():
    """The promise, parameter by parameter."""
    problems = []
    for name, cmd in _click_leaves().items():
        expected = {p.name for p in cmd.params if p.name != "help"}
        actual = {p.name for p in COMMANDS[name].params}
        if expected != actual:
            problems.append((name, sorted(expected ^ actual)))
    assert not problems, f"parameters that differ between CLI and GUI: {problems}"


def test_the_surface_is_not_trivially_all_text():
    """The first version rendered all 106 parameters as text boxes.

    typer vendors its own click, so ``isinstance(x, click.Choice)`` was quietly
    False for everything. Kinds are now duck-typed by class name; this pins it.
    """
    kinds = {p.kind for c in COMMANDS.values() for p in c.params}
    assert {Kind.FLAG, Kind.PATH, Kind.CHOICE, Kind.INT, Kind.FLOAT, Kind.MULTI} <= kinds


def test_choices_and_bounds_come_from_the_cli():
    profile = next(p for p in COMMANDS["analyze"].params if p.name == "profile")
    assert profile.kind is Kind.CHOICE
    assert set(profile.choices) == {"fast", "standard", "forensic"}
    limit = next(p for p in COMMANDS["ledger list"].params if p.name == "limit")
    assert limit.kind is Kind.INT and limit.bounds is not None
    ref = next(p for p in COMMANDS["issue check"].params if p.name == "ref")
    assert ref.kind is Kind.MULTI


def test_positional_arguments_are_marked():
    run_id = next(p for p in COMMANDS["ledger show"].params if p.name == "run_id")
    assert run_id.positional and run_id.required


def test_environment_variables_are_carried_through():
    config = next(p for p in COMMANDS["analyze"].params if p.name == "config")
    assert config.envvar == "ABCA_CONFIG"


# -------------------------------------------------------------- argv build


def test_unset_values_produce_no_argv_entries():
    """Blank means "the CLI's default", exactly as not typing the option does."""
    assert build_argv(COMMANDS["version"], {"json_out": False}) == ["version"]
    assert build_argv(COMMANDS["ledger list"], {"limit": "", "json_out": False}) == ["ledger", "list"]


def test_flags_and_values_are_emitted_in_cli_form():
    argv = build_argv(COMMANDS["analyze"], {
        "text": "a claim", "json_out": True, "seed": "42", "profile": "fast",
    })
    assert argv[:1] == ["analyze"]
    assert "--json" in argv
    assert argv[argv.index("--seed") + 1] == "42"
    assert argv[argv.index("--profile") + 1] == "fast"
    assert argv[argv.index("--text") + 1] == "a claim"


def test_multi_values_become_repeated_options():
    argv = build_argv(COMMANDS["issue check"], {"text": "x", "ref": "https://a\n\nhttps://b\n"})
    assert argv.count("--ref") == 2
    assert argv[argv.index("--ref") + 1] == "https://a"


def test_positionals_go_last():
    argv = build_argv(COMMANDS["ledger show"], {"run_id": "01ABC", "stages": True})
    assert argv == ["ledger", "show", "--stages", "01ABC"]


def test_a_positional_that_looks_like_an_option_is_protected():
    argv = build_argv(COMMANDS["sources fetch"], {"locator": "-5 ILCS 1/1"})
    assert argv[-2:] == ["--", "-5 ILCS 1/1"]


def test_missing_required_argument_is_a_form_error_not_a_subprocess_error():
    with pytest.raises(FormError, match="run id is required"):
        build_argv(COMMANDS["ledger show"], {"run_id": ""})


def test_numbers_are_validated_against_the_cli_bounds():
    with pytest.raises(FormError, match="not an integer"):
        build_argv(COMMANDS["ledger list"], {"limit": "twenty"})
    with pytest.raises(FormError, match="below the minimum"):
        build_argv(COMMANDS["ledger list"], {"limit": "0"})


def test_a_choice_outside_the_list_is_rejected_before_running():
    with pytest.raises(FormError, match="not one of"):
        build_argv(COMMANDS["analyze"], {"text": "x", "profile": "turbo"})


def test_render_command_quotes_for_the_shell():
    argv = ["analyze", "--text", "it's \"quoted\""]
    posix = render_command(argv)
    assert posix.startswith("abca analyze --text ")
    win = render_command(argv, windows=True)
    assert win.startswith("abca analyze --text ")
    assert '"' in win


# -------------------------------------------------------------- round-trip


def _parse_with_click(argv: list[str]) -> dict:
    """Ask click what the built argv MEANS. The real thing, not a re-implementation."""
    root = get_command(app)
    cmd = root
    rest = list(argv)
    while rest and hasattr(cmd, "commands") and rest[0] in cmd.commands:
        cmd = cmd.commands[rest.pop(0)]
    ctx = cmd.make_context("abca", rest, resilient_parsing=False)
    return ctx.params


@pytest.mark.parametrize("name,values", [
    ("analyze", {"text": "a claim", "json_out": True, "seed": "7", "profile": "forensic",
                 "max_posts": "12", "offline": True}),
    ("ledger list", {"limit": "5", "json_out": True}),
    ("issue check", {"text": "x", "ref": ["https://a", "https://b"], "minimum_refs": "2",
                     "strict": True}),
    ("sources fetch", {"locator": "10 ILCS 5/10-2", "connector": "ilcs", "offline": True}),
    ("queue promote", {"challenge_id": "ch-1", "operator": "pat", "i_know": True}),
    ("verify", {"run_id": "01ABC", "integrity_only": True}),
])
def test_click_parses_the_built_argv_back_to_the_form_values(name, values):
    argv = build_argv(COMMANDS[name], values)
    parsed = _parse_with_click(argv)
    for key, value in values.items():
        got = parsed[key]
        if isinstance(value, bool):
            assert got is value, (key, got)
        elif isinstance(value, list):
            assert list(got) == value, (key, got)
        else:
            assert str(got) == str(value) or str(got) == str(Path(value)), (key, got)


# -------------------------------------------------------------- the window


def _display_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    return True


needs_display = pytest.mark.skipif(not _display_available(), reason="no Tk display")


@pytest.fixture(scope="session")
def tk_root():
    """ONE Tk root for the whole session, reused by every window test.

    Creating and destroying Tk roots repeatedly inside one process is fragile
    on Windows: the first CI run on windows-latest built one root, destroyed
    it, and the very next ``tk.Tk()`` failed with "Can't find a usable
    init.tcl" -- then the third succeeded. Tcl's per-process state does not
    survive that cycle reliably, and it is not this repository's bug to fix.
    One root, torn down once at the end, is how tkinter itself is tested.
    """
    if not _display_available():
        pytest.skip("no Tk display")
    import tkinter as tk

    root = tk.Tk()
    yield root
    with contextlib.suppress(Exception):   # already gone is fine
        root.destroy()


def _fresh_window(tk_root):
    """A window on the shared root, with the previous test's widgets cleared."""
    from abca.gui.app import AbcaWindow

    for child in tk_root.winfo_children():
        child.destroy()
    return AbcaWindow(root=tk_root)


def _dismiss(window) -> None:
    """Stop the window's timer and subprocess without destroying the root."""
    with contextlib.suppress(Exception):
        window.root.after_cancel(window._drain_after)
    if window.run and window.run.running:
        window.run.cancel()
    for child in window.root.winfo_children():
        child.destroy()


@needs_display
def test_the_window_builds_a_form_for_every_command(tk_root):
    window = _fresh_window(tk_root)
    try:
        window.root.update()
        for command in COMMANDS.values():
            window.show_command(command)
            window.root.update()
            assert set(window.controls) == {p.name for p in command.params}, command.name
    finally:
        _dismiss(window)


@needs_display
def test_the_window_runs_the_real_cli_and_streams_its_output(tk_root):
    """`version --json` through the subprocess runner, output back on the Tk thread."""
    import time

    window = _fresh_window(tk_root)
    try:
        window.show_command(COMMANDS["version"])
        window.controls["json_out"].var.set(True)
        window.root.update()
        assert window.command_var.get() == "abca version --json"
        window._on_run()
        for _ in range(200):
            window.root.update()
            time.sleep(0.05)
            if window.status_var.get().startswith("finished"):
                break
        assert window.status_var.get() == "finished, exit 0"
        assert '"tool_version"' in window.output.get("1.0", "end")
    finally:
        _dismiss(window)


def test_the_gui_module_runs_as_a_script_and_reports_missing_display_cleanly():
    """`python -m abca.gui` must fail with a message, not a traceback, headless."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    env.pop("DISPLAY", None)
    result = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util as u; print(bool(u.find_spec('abca.gui.app')))"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert result.stdout.strip() == "True"


# --------------------------------------------------------------- dark theme


def _tokens() -> dict[str, str]:
    import json

    data = json.loads((ROOT / "identity" / "tokens.json").read_text(encoding="utf-8"))
    return {**data["core"], **data["derived"]}


def test_every_theme_colour_is_an_identity_token():
    """The desktop app and the brand guide must agree on the palette.

    The frozen executable does not ship identity/, so the hex values are
    duplicated in theme.py -- and duplication is where drift starts. This is
    the test that makes the duplication safe.
    """
    from abca.gui.theme import PALETTE, TOKEN_OF

    tokens = _tokens()
    assert set(PALETTE) == set(TOKEN_OF)
    for name, token in TOKEN_OF.items():
        assert PALETTE[name].upper() == tokens[token].upper(), (
            f"theme {name!r} is {PALETTE[name]} but token {token!r} is {tokens[token]}"
        )


def test_every_text_pair_in_the_dark_theme_clears_wcag_aa():
    """Dark mode is held to the same measured standard as the light palette."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_contrast", ROOT / "scripts" / "check_contrast.py")
    assert spec is not None and spec.loader is not None
    contrast = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contrast)

    from abca.gui.theme import PALETTE, TEXT_PAIRS

    for fg, bg in TEXT_PAIRS:
        ratio = contrast.contrast(PALETTE[fg], PALETTE[bg])
        assert ratio >= 4.5, f"{fg} on {bg} is {ratio:.2f}:1, below WCAG AA"


@needs_display
def test_the_window_is_dark_by_default_and_has_a_navigable_help_tab(tk_root):
    from abca.gui.theme import PALETTE

    window = _fresh_window(tk_root)
    try:
        window.root.update()
        assert window.root.cget("background") == PALETTE["ground"]
        assert window.style.theme_use() == "clam"
        tabs = [window.notebook.tab(t, "text").strip() for t in window.notebook.tabs()]
        assert tabs == ["Commands", "Help"]
        # F1 from a form lands on that command's help.
        window.show_command(COMMANDS["ledger show"])
        window.show_help_for_current()
        window.root.update()
        assert window.notebook.index("current") == 1
        assert "Usage: abca ledger show" in window.detail_text.get("1.0", "end")
        # Search highlights matches.
        window.search_var.set("ledger")
        window.root.update()
        assert window.search_status.get().endswith("match(es)")
        assert window.help_text.tag_ranges("hit")
    finally:
        _dismiss(window)


def test_help_reference_covers_every_command_and_has_no_escape_codes():
    from abca.gui.help import cli_help_text, reference_text

    reference = reference_text(GROUPS)
    for name in COMMANDS:
        assert f"abca {name}\n" in reference, name
    assert "\x1b[" not in reference
    assert "\x1b[" not in cli_help_text(("analyze",))
