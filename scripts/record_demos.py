"""Record the demo GIFs: the CLI and the GUI, every feature, real output.

    pip install pillow            # the only non-repo dependency
    python scripts/record_demos.py            # -> docs/demos/*.gif
    python scripts/record_demos.py --only cli # or gui

WHAT IS REAL AND WHAT IS NOT
============================
Every command shown runs for real: the actual CLI, the actual GUI, real HTTP to
a model server, real ledger writes, real hash chains, real source cache. The one
thing that is mocked is the MODEL -- scripts/mock_ollama.py answers on a
loopback port with scripted, deterministic verdicts, exactly as the test suite
and every demo_step script use it. So the output on screen is what the shipping
code produced, and the only reason it fits in a GIF is that no GPU had to
think.

Nothing is typed by a person and nothing is hand-drawn. The terminal in the CLI
GIFs is rendered frame by frame with Pillow from the captured stdout
the GUI
GIFs are screenshots of the real Tk window driven programmatically under a
virtual display. Re-running this script regenerates every GIF from scratch,
which is the point: a demo that cannot be regenerated is a demo that will one
day show a command that no longer exists.

Four recordings, split the way the release is split:

    1-cli-basics        abca.exe: configure, check the backend, analyze, audit
    2-gui               abca-gui.exe: the same analysis from the window
    3-cli-everything    abca.exe: sources, explain, issues, the queue, schema
    4-gui-everything    abca-gui.exe: ledger, verify, sources, queue, help, search
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "docs" / "demos"

# The brand palette, so the terminal looks like the GUI. Mirrors the dark
# theme; tests hold that to the identity tokens, and this is a picture of it.
INK, PANEL, TEXT, HINT, ACCENT, ACCENT_2 = (
    "#1B2A2E", "#2E474E", "#F4F1EA", "#C9A227", "#149E9E", "#0E6E6E",
)

STATEMENT = (
    "Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures to start a new "
    "party, and that's the only real hurdle. The whole thing is rigged against "
    "outsiders. Last week the Secretary of State said the process is "
    "straightforward. Anyway, my dog is asleep on the couch."
)

STATUTE = """(10 ILCS 5/10-2) (from Ch. 46, par. 10-2)

Sec. 10-2.
The term "political party", as hereinafter used in this
Article 10, shall mean any "established political party".

A political party which, at the last general election for State and
county officers, polled for its candidate for Governor more than 5% of
the entire vote cast for Governor, is hereby declared to be an
"established political party".

Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled in the State at the next ensuing election.
"""


# --------------------------------------------------------------------------
# A workspace: mock model, config, seeded source cache, empty ledger
# --------------------------------------------------------------------------


class Workspace:
    """Everything a demo needs, in a temp directory, torn down afterwards."""

    def __init__(self) -> None:
        from mock_ollama import MODEL, MODEL_B, serve

        self.httpd, self.port = serve()
        self.model = MODEL
        self.model_b = MODEL_B
        # A fixed, readable path: it appears in the recordings.
        self.root = Path(tempfile.gettempdir()) / "abca-demo"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self.config = self.root / "config.toml"
        self.ledger = self.root / "runs"
        self.cache = self.root / "sources"
        self.data_home = self.root / "data"
        self._write_config()
        self._seed_cache()

    def _write_config(self) -> None:
        base = f'provider = "ollama"\nmodel = "{self.model}"\nbase_url = "http://127.0.0.1:{self.port}"\n'
        self.config.write_text(
            "[defaults]\n"
            'profile = "standard"\nseed = 42\ntemperature = 0.0\nmax_claims = 200\n\n'
            f"[models.classifier]\n{base}\n"
            f"[models.segmenter]\n{base}\n"
            f"[models.adjudicator]\n{base}\n"
            # The red team and the back-translator must be INDEPENDENT of the
            # adjudicator -- the config loader refuses a back-translator that
            # is not, and the report flags a red team that is not. The mock
            # advertises two model names; the second is used for both.
            f'[models.redteam]\nprovider = "ollama"\nmodel = "{self.model_b}"\n'
            f'base_url = "http://127.0.0.1:{self.port}"\n\n'
            f'[models.backtranslate]\nprovider = "ollama"\nmodel = "{self.model_b}"\n'
            f'base_url = "http://127.0.0.1:{self.port}"\n',
            encoding="utf-8",
        )

    def _seed_cache(self) -> None:
        """A real statute in the cache so --offline citations verify verbatim."""
        from abca.sources.base import build_document
        from abca.sources.cache import SourceCache
        from abca.sources.ilcs import ILCSConnector

        cache = SourceCache(self.cache)
        cache.put(build_document(
            connector=ILCSConnector(cache=cache, offline=True),
            doc_id="s-001", title="10 ILCS 5/10-2",
            url="https://www.ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm",
            text=STATUTE, locator="10 ILCS 5/10-2",
            retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
        ))

    @property
    def env(self) -> dict[str, str]:
        env = {**os.environ,
               "PYTHONPATH": str(ROOT / "src"),
               "ABCA_CONFIG": str(self.config),
               "ABCA_LEDGER_ROOT": str(self.ledger),
               "ABCA_SOURCE_CACHE": str(self.cache),
               "XDG_DATA_HOME": str(self.data_home),
               "NO_COLOR": "1", "_TYPER_FORCE_DISABLE_TERMINAL": "1",
               "COLUMNS": "104"}
        env.pop("LOCALAPPDATA", None)
        return env

    def cli(self, *args: str) -> str:
        run = subprocess.run([sys.executable, "-m", "abca.cli.main", *args],
                             capture_output=True, text=True, env=self.env, check=False)
        out = run.stdout + (("\n" + run.stderr) if run.stderr.strip() else "")
        return out.rstrip("\n") + (f"\n[exit {run.returncode}]" if run.returncode else "")

    def close(self) -> None:
        self.httpd.shutdown()
        shutil.rmtree(self.root, ignore_errors=True)


# --------------------------------------------------------------------------
# Terminal renderer
# --------------------------------------------------------------------------


class Terminal:
    """Draw a terminal, one frame per event, into a GIF.

    Two kinds of frame: typing (the command appears a few characters at a
    time, with a cursor) and output (lines appear in chunks, as a real pipe
    delivers them). Long pauses after each command so the output can be read;
    a GIF that flashes past its own point is a screensaver.
    """

    COLS, ROWS = 104, 34
    CHAR_W, CHAR_H = 9, 19
    PAD = 18

    def __init__(self, title: str) -> None:
        from PIL import ImageFont

        self.title = title
        self.font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 15)
        self.bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 15)
        self.lines: list[tuple[str, str]] = []   # (text, role) role in {cmd, out, dim}
        self.frames: list = []
        self.durations: list[int] = []
        self.width = self.COLS * self.CHAR_W + 2 * self.PAD
        self.height = self.ROWS * self.CHAR_H + 2 * self.PAD + 30

    #: rich draws tables with heavy box-drawing characters, which DejaVu Sans
    #: Mono renders as a thin line along the top of the cell -- a broken-looking
    #: box. The light forms render centred. This is a glyph substitution in the
    #: PICTURE only; the captured output is untouched.
    _LIGHT = str.maketrans({"━": "─", "┃": "│", "┏": "┌", "┓": "┐", "┗": "└", "┛": "┘",
                            "┣": "├", "┫": "┤", "┳": "┬", "┻": "┴", "╋": "┼",
                            "┡": "├", "┩": "┤", "╇": "┼", "╈": "┼", "┯": "┬", "┷": "┴"})

    def _render(self, partial: str | None = None) -> None:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (self.width, self.height), INK)
        draw = ImageDraw.Draw(img)
        # Title bar.
        draw.rectangle([0, 0, self.width, 30], fill=PANEL)
        draw.text((self.PAD, 6), self.title, fill=HINT, font=self.bold)
        # Visible rows: the tail.
        visible = self.lines[-self.ROWS:] if partial is None else self.lines[-(self.ROWS - 1):]
        y = 30 + self.PAD
        for text, role in visible:
            colour = {"cmd": TEXT, "out": TEXT, "dim": HINT, "ok": ACCENT}.get(role, TEXT)
            font = self.bold if role == "cmd" else self.font
            draw.text((self.PAD, y), text[: self.COLS].translate(self._LIGHT), fill=colour, font=font)
            y += self.CHAR_H
        if partial is not None:
            draw.text((self.PAD, y), partial + "▍", fill=TEXT, font=self.bold)
        self.frames.append(img)

    def _add(self, duration_ms: int) -> None:
        self.durations.append(duration_ms)

    def note(self, text: str, hold_ms: int = 1800) -> None:
        """A dim comment line, as a narrator would type it."""
        for chunk in textwrap.wrap(text, self.COLS - 2) or [""]:
            self.lines.append((f"# {chunk}", "dim"))
        self._render()
        self._add(hold_ms)

    def command(self, shown: str, output: str, *, hold_ms: int = 3200,
                max_lines: int | None = None) -> None:
        prompt = "$ "
        # Type it.
        step = max(2, len(shown) // 18)
        for i in range(0, len(shown) + 1, step):
            self._render(partial=prompt + shown[:i])
            self._add(45)
        self.lines.append((prompt + shown, "cmd"))
        self._render()
        self._add(350)
        # Stream the output in chunks.
        out_lines = output.split("\n")
        if max_lines and len(out_lines) > max_lines:
            out_lines = [*out_lines[:max_lines], f"… ({len(out_lines) - max_lines} more lines)"]
        chunk = max(1, len(out_lines) // 8)
        for i in range(0, len(out_lines), chunk):
            for line in out_lines[i:i + chunk]:
                role = "ok" if line.startswith(("intact", "PASS", "All ")) else "out"
                for wrapped in (textwrap.wrap(line, self.COLS, replace_whitespace=False,
                                              drop_whitespace=False) or [""]):
                    self.lines.append((wrapped, role))
            self._render()
            self._add(140)
        self.lines.append(("", "out"))
        self._render()
        self._add(hold_ms)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = [f.quantize(colors=64, method=2) for f in self.frames]
        frames[0].save(path, save_all=True, append_images=frames[1:],
                       duration=self.durations, loop=0, optimize=True, disposal=1)
        print(f"wrote {path} ({path.stat().st_size:,} bytes, {len(frames)} frames)")


# --------------------------------------------------------------------------
# The CLI recordings
# --------------------------------------------------------------------------


def record_cli_basics(ws: Workspace) -> None:
    t = Terminal("abca.exe — configure, check, analyze, audit")
    t.note("abCA: the command line. Model calls go to a scripted mock on localhost, "
           "so every verdict below is deterministic — everything else is the real code.")
    t.command("abca version", ws.cli("version"))
    t.command("abca config show", ws.cli("config", "show"), max_lines=22)
    t.command("abca models check", ws.cli("models", "check"))
    t.note("A statement with one legal claim, one opinion, one attribution, and one sentence "
           "about a dog. --offline uses the cached statute; nothing is fetched.")
    t.command(f'abca analyze -t "{STATEMENT[:60]}…" --offline',
              ws.cli("analyze", "-t", STATEMENT, "--offline"), hold_ms=6500)
    t.note("Every run is a record. List it, show it, then audit its hash chain.")
    listing = ws.cli("ledger", "list")
    t.command("abca ledger list", listing)
    run_id = _first_run_id(ws)
    t.command(f"abca ledger show {run_id}", ws.cli("ledger", "show", run_id), max_lines=26, hold_ms=5000)
    t.command(f"abca verify {run_id} --integrity-only", ws.cli("verify", run_id, "--integrity-only"))
    t.note("Replay re-executes the run against the same model and compares every digest.")
    t.command(f'abca verify {run_id} -t "…the same statement…" --offline',
              ws.cli("verify", run_id, "-t", STATEMENT, "--offline"), hold_ms=6000)
    t.save(OUT / "1-cli-basics.gif")


def record_cli_everything(ws: Workspace) -> None:
    t = Terminal("abca.exe — sources, explain, issues, the queue, schema")
    t.note("Sources: which connectors exist, what tier they hold, and what is cached.")
    t.command("abca sources list", ws.cli("sources", "list"))
    t.command("abca sources cache-stats", ws.cli("sources", "cache-stats"))
    t.command('abca sources fetch "10 ILCS 5/10-2" --offline',
              ws.cli("sources", "fetch", "10 ILCS 5/10-2", "--offline"), hold_ms=4500)
    t.note("Explain: a statute rewritten at an eighth-grade level, then back-translated by an "
           "independent model and diffed against the original, element by element.")
    t.command('abca explain --cite "10 ILCS 5/10-2" --offline',
              ws.cli("explain", "--cite", "10 ILCS 5/10-2", "--offline"), max_lines=30, hold_ms=7000)
    t.note("Issues: who is allowed to decide each kind of step, and the evidence floor.")
    t.command("abca issue governance", ws.cli("issue", "governance"), max_lines=24, hold_ms=5000)
    t.command('abca issue check -t "Ballot access should be earned by organizing." --ref x',
              ws.cli("issue", "check", "-t", "Ballot access should be earned by organizing.",
                     "--ref", "https://www.ilga.gov/legislation/ilcs/", "--offline"), hold_ms=4500)
    t.note("The challenge queue: Lane B, for claims that arrive with no source at all.")
    t.command("abca queue stats", ws.cli("queue", "stats", "--queue-root", str(ws.root / "challenges")))
    t.command("abca queue list", ws.cli("queue", "list", "--queue-root", str(ws.root / "challenges")))
    t.note("The run-record schema, for anyone building a verifier without this code.")
    t.command("abca schema | head -30", "\n".join(ws.cli("schema").split("\n")[:30]), hold_ms=3500)
    t.command("abca models grammar analysis | head -16",
              "\n".join(ws.cli("models", "grammar", "analysis").split("\n")[:16]), hold_ms=3500)
    t.command("abca version --json", ws.cli("version", "--json"))
    t.save(OUT / "3-cli-everything.gif")


def _first_run_id(ws: Workspace) -> str:
    payload = json.loads(ws.cli("ledger", "list", "--json"))
    runs = payload if isinstance(payload, list) else payload.get("runs", payload)
    first = runs[0] if isinstance(runs, list) else runs
    return first["run_id"] if isinstance(first, dict) else str(first)


# --------------------------------------------------------------------------
# The GUI recordings
# --------------------------------------------------------------------------


class Screen:
    """Capture the Tk window under Xvfb, frame by frame, into a GIF."""

    def __init__(self, window) -> None:
        self.window = window
        self.frames: list = []
        self.durations: list[int] = []
        self.shot_dir = Path(tempfile.mkdtemp(prefix="abca-shots-"))

    def frame(self, hold_ms: int = 600, settle: int = 3) -> None:
        from PIL import Image

        for _ in range(settle):
            self.window.root.update()
            time.sleep(0.05)
        path = self.shot_dir / f"{len(self.frames):04d}.png"
        subprocess.run(["import", "-window", "root", str(path)], check=True)
        img = Image.open(path).convert("RGB")
        scale = 960 / img.width
        img = img.resize((960, int(img.height * scale)), Image.LANCZOS)
        self.frames.append(img)
        self.durations.append(hold_ms)

    def type_into_text(self, widget, text: str, per_chunk_ms: int = 90) -> None:
        step = max(3, len(text) // 14)
        for i in range(step, len(text) + step, step):
            widget.delete("1.0", "end")
            widget.insert("1.0", text[:i])
            self.window._refresh_command()
            self.frame(per_chunk_ms, settle=1)

    def type_into_var(self, var, text: str, per_chunk_ms: int = 120) -> None:
        for i in range(1, len(text) + 1):
            var.set(text[:i])
            self.frame(per_chunk_ms, settle=1)

    def wait_finished(self, timeout_s: float = 60) -> None:
        deadline = time.time() + timeout_s
        last = ""
        while time.time() < deadline:
            self.window.root.update()
            time.sleep(0.1)
            current = self.window.output.get("1.0", "end")
            if current != last:
                self.frame(220, settle=1)
                last = current
            if self.window.status_var.get().startswith("finished"):
                break
        self.frame(2500)

    def save(self, path: Path) -> None:
        frames = [f.quantize(colors=96, method=2) for f in self.frames]
        frames[0].save(path, save_all=True, append_images=frames[1:],
                       duration=self.durations, loop=0, optimize=True, disposal=1)
        shutil.rmtree(self.shot_dir, ignore_errors=True)
        print(f"wrote {path} ({path.stat().st_size:,} bytes, {len(frames)} frames)")


def _select(window, name: str) -> None:
    """Select in the tree AND show the form, as a click would."""
    window.tree.selection_set("cmd:" + name)
    window.tree.see("cmd:" + name)
    window.root.update()
    window.show_command(window.commands[name])


def _window(ws: Workspace):
    # The window runs the CLI as a subprocess, which inherits this environment.
    os.environ.update({k: v for k, v in ws.env.items() if k.startswith(("ABCA_", "XDG_"))})
    from abca.gui.app import AbcaWindow

    window = AbcaWindow()
    window.root.geometry("1240x800+0+0")
    window.root.update()
    return window


def record_gui(ws: Workspace) -> None:
    w = _window(ws)
    s = Screen(w)
    try:
        s.frame(1800)
        _select(w, "analyze")
        s.frame(1200)
        s.type_into_text(w.controls["text"].text, STATEMENT)
        w.controls["offline"].var.set(True)
        s.frame(700)
        w.controls["profile"].var.set("standard")
        s.frame(700)
        w.controls["seed"].var.set("42")
        s.frame(900)
        s.frame(1600)                         # read the command line
        w._on_run()
        s.frame(400)
        s.wait_finished()
        w.controls["json_out"].var.set(True)
        s.frame(1200)
        w._on_run()
        s.wait_finished()
        w.notebook.select(w.help_tab)
        w.jump_to_help("analyze")
        s.frame(2200)
        s.type_into_var(w.search_var, "offline")
        s.frame(2200)
        w._search(forward=True)
        s.frame(1500)
        w.notebook.select(0)
        s.frame(1500)
        s.save(OUT / "2-gui.gif")
    finally:
        w.close()


def record_gui_everything(ws: Workspace) -> None:
    w = _window(ws)
    s = Screen(w)
    try:
        s.frame(1200)
        for name in ("ledger list", "sources list", "sources cache-stats", "issue governance",
                     "queue stats", "config show", "models check"):
            _select(w, name)
            s.frame(900)
            w._on_run()
            s.frame(300)
            s.wait_finished(timeout_s=90)
        run_id = _first_run_id(ws)
        _select(w, "ledger show")
        s.frame(800)
        s.type_into_var(w.controls["run_id"].var, run_id, per_chunk_ms=60)
        w.controls["stages"].var.set(True)
        s.frame(900)
        w._on_run()
        s.wait_finished()
        _select(w, "verify")
        s.frame(800)
        w.controls["run_id"].var.set(run_id)
        w.controls["integrity_only"].var.set(True)
        s.frame(1000)
        w._on_run()
        s.wait_finished()
        _select(w, "sources fetch")
        s.frame(800)
        s.type_into_var(w.controls["locator"].var, "10 ILCS 5/10-2", per_chunk_ms=70)
        w.controls["offline"].var.set(True)
        s.frame(900)
        w._on_run()
        s.wait_finished()
        # Environment fields show up in the command line, and F1 opens help.
        w.env_vars["ABCA_LEDGER_ROOT"].set(str(ws.ledger))
        s.frame(1800)
        w.env_vars["ABCA_LEDGER_ROOT"].set("")
        s.frame(600)
        _select(w, "queue promote")
        s.frame(1400)
        w.show_help_for_current()
        s.frame(2500)
        s.type_into_var(w.search_var, "hash")
        s.frame(2200)
        s.save(OUT / "4-gui-everything.gif")
    finally:
        w.close()


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("cli", "gui"), help="record one half")
    args = parser.parse_args()
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise SystemExit("pip install pillow") from None

    OUT.mkdir(parents=True, exist_ok=True)
    ws = Workspace()
    try:
        if args.only != "gui":
            record_cli_basics(ws)
            record_cli_everything(ws)
        if args.only != "cli":
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                raise SystemExit("the GUI recordings need a display: xvfb-run -a python scripts/record_demos.py --only gui")
            # A fresh ledger so the GUI half shows its own run first.
            record_gui(ws)
            record_gui_everything(ws)
    finally:
        ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
