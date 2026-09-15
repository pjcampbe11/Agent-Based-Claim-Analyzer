"""The window. tkinter, standard library, generated from the command tree.

Layout::

    +-------------------+--------------------------------------------------+
    | commands (tree)   |  <command help>                                  |
    |  analyze          |  [ form: one row per parameter, from surface.py ] |
    |  explain          |  ...                                             |
    |  ledger           |  environment: ABCA_CONFIG / LEDGER_ROOT / CACHE   |
    |    path           |  command:  abca analyze --text '...' --json       |
    |    list           |  [ Run ]  [ Cancel ]  [ Copy command ]  [ Clear ] |
    |    ...            |  output ------------------------------------------ |
    |                   |  ...streamed from the subprocess...                |
    +-------------------+--------------------------------------------------+

Everything on the right is rebuilt when the selection on the left changes.
No widget is created by hand for a specific option; if it were, the coverage
test in ``tests/test_gui.py`` would have nothing to check.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from abca.gui.argv import FormError, build_argv, render_command
from abca.gui.help import command_help, reference_text
from abca.gui.runner import CliNotFound, CliRun, cli_command
from abca.gui.surface import CommandSpec, GroupSpec, Kind, ParamSpec, describe_cli
from abca.gui.theme import PALETTE, apply_dark
from abca.version import TOOL_VERSION

#: Environment variables the CLI reads. Offered as fields so a run from the
#: window can be pointed at a config, a ledger and a source cache the same
#: way a shell session would be -- and shown in the command box as ``VAR=...``
#: so the copy-pasted line reproduces the run.
ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("ABCA_CONFIG", "config.toml to use (same as --config where a command takes it)"),
    ("ABCA_LEDGER_ROOT", "where run records are written and read"),
    ("ABCA_SOURCE_CACHE", "where fetched statutes are cached"),
)

_MONO = ("Consolas", 10) if sys.platform == "win32" else ("DejaVu Sans Mono", 10)


class ParamControl:
    """One parameter's widget(s) and how to read a value back out of them."""

    def __init__(self, parent: ttk.Frame, spec: ParamSpec, row: int) -> None:
        self.spec = spec
        self.var: tk.Variable | None = None
        self.text: tk.Text | None = None

        label = ttk.Label(parent, text=spec.label + (" *" if spec.required else ""))
        label.grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)

        if spec.kind is Kind.FLAG:
            self.var = tk.BooleanVar(value=False)
            ttk.Checkbutton(parent, variable=self.var).grid(row=row, column=1, sticky="w")
        elif spec.kind is Kind.CHOICE:
            self.var = tk.StringVar(value="")
            box = ttk.Combobox(parent, textvariable=self.var, values=("", *spec.choices),
                               state="readonly", width=18)
            box.grid(row=row, column=1, sticky="w")
        elif spec.kind is Kind.MULTI or spec.long_text:
            self.text = tk.Text(parent, height=4 if spec.kind is Kind.MULTI else 6,
                                width=52, wrap="word", font=_MONO)
            self.text.grid(row=row, column=1, sticky="ew")
        elif spec.kind is Kind.PATH:
            self.var = tk.StringVar(value="")
            frame = ttk.Frame(parent)
            frame.grid(row=row, column=1, sticky="ew")
            ttk.Entry(frame, textvariable=self.var, width=40).pack(side="left", fill="x", expand=True)
            ttk.Button(frame, text="Browse…", command=self._browse).pack(side="left", padx=(4, 0))
        else:
            self.var = tk.StringVar(value="")
            width = 12 if spec.kind in (Kind.INT, Kind.FLOAT) else 44
            ttk.Entry(parent, textvariable=self.var, width=width).grid(row=row, column=1, sticky="w")

        hint = spec.help
        if spec.default not in (None, False, "") and spec.kind is not Kind.FLAG:
            hint = f"{hint}  (default: {spec.default})" if hint else f"default: {spec.default}"
        if spec.envvar:
            hint = f"{hint}  [env: {spec.envvar}]"
        if spec.bounds and any(b is not None for b in spec.bounds):
            low, high = spec.bounds
            hint = f"{hint}  [{low if low is not None else ''}..{high if high is not None else ''}]"
        if hint:
            ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=330,
                      justify="left").grid(row=row, column=2, sticky="nw", padx=(8, 0))

    def _browse(self) -> None:
        if self.spec.exists:
            chosen = filedialog.askopenfilename()
        elif self.spec.name in {"out", "records_dir", "ledger_root", "cache_root",
                                "queue_root"}:
            chosen = filedialog.askdirectory() if self.spec.name.endswith(
                ("_root", "_dir")) else filedialog.asksaveasfilename()
        else:
            chosen = filedialog.askopenfilename()
        if chosen and self.var is not None:
            self.var.set(chosen)

    def value(self) -> Any:
        if self.text is not None:
            raw = self.text.get("1.0", "end").rstrip("\n")
            return raw.splitlines() if self.spec.kind is Kind.MULTI else raw
        assert self.var is not None
        return self.var.get()


class AbcaWindow:
    """The main window. Construct, then ``run()``."""

    def __init__(self, root: tk.Tk | None = None) -> None:
        self.root = root or tk.Tk()
        self.root.title(f"abCA {TOOL_VERSION} — every command, the same command line")
        self.root.geometry("1240x800")
        self.groups: tuple[GroupSpec, ...] = describe_cli()
        self.commands: dict[str, CommandSpec] = {
            c.name: c for g in self.groups for c in g.commands
        }
        self.controls: dict[str, ParamControl] = {}
        self.current: CommandSpec | None = None
        self.run: CliRun | None = None
        self.lines: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.env_vars: dict[str, tk.StringVar] = {
            name: tk.StringVar(value=os.environ.get(name, "")) for name, _ in ENV_FIELDS
        }
        self.style = apply_dark(self.root)
        self._build()
        self.root.bind("<F1>", lambda _e: self.show_help_for_current())
        self._drain_after = self.root.after(100, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------ building

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        commands_tab = ttk.Frame(self.notebook)
        self.notebook.add(commands_tab, text="  Commands  ")
        self._build_help_tab()

        paned = ttk.Panedwindow(commands_tab, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, width=230)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        for group in self.groups:
            if group.path:
                node = self.tree.insert("", "end", iid="grp:" + group.path[0],
                                        text=group.path[0], open=True)
                for cmd in group.commands:
                    self.tree.insert(node, "end", iid="cmd:" + cmd.name,
                                     text=cmd.path[-1] if len(cmd.path) > 1 else cmd.name)
            else:
                for cmd in group.commands:
                    self.tree.insert("", "end", iid="cmd:" + cmd.name, text=cmd.name)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        paned.add(left, weight=0)

        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        self.help_var = tk.StringVar(value="Pick a command on the left.")
        header = ttk.Frame(right)
        header.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(header, textvariable=self.help_var, wraplength=800, justify="left",
                  style="Title.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Button(header, text="Help for this command (F1)",
                   command=self.show_help_for_current).pack(side="right")

        # Scrollable form.
        form_outer = ttk.Frame(right)
        form_outer.pack(fill="both", expand=True, padx=8)
        canvas = tk.Canvas(form_outer, highlightthickness=0, height=360)
        bar = ttk.Scrollbar(form_outer, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.form, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        env = ttk.LabelFrame(right, text="environment (shown in the command line, applied to the run)")
        env.pack(fill="x", padx=8, pady=4)
        for row, (name, help_text) in enumerate(ENV_FIELDS):
            ttk.Label(env, text=name).grid(row=row, column=0, sticky="w", padx=(4, 8))
            ttk.Entry(env, textvariable=self.env_vars[name], width=40).grid(row=row, column=1, sticky="w")
            ttk.Label(env, text=help_text, style="Hint.TLabel").grid(row=row, column=2, sticky="w", padx=8)
            self.env_vars[name].trace_add("write", lambda *_: self._refresh_command())

        cmdrow = ttk.Frame(right)
        cmdrow.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(cmdrow, text="command").pack(side="left")
        self.command_var = tk.StringVar(value="")
        ttk.Entry(cmdrow, textvariable=self.command_var, font=_MONO, state="readonly").pack(
            side="left", fill="x", expand=True, padx=8)

        buttons = ttk.Frame(right)
        buttons.pack(fill="x", padx=8, pady=4)
        self.run_button = ttk.Button(buttons, text="Run", command=self._on_run, style="Run.TButton")
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=4)
        ttk.Button(buttons, text="Copy command", command=self._on_copy).pack(side="left", padx=4)
        ttk.Button(buttons, text="Clear output", command=lambda: self.output.delete("1.0", "end")).pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="")
        ttk.Label(buttons, textvariable=self.status_var, style="Hint.TLabel").pack(side="left", padx=12)

        self.output = scrolledtext.ScrolledText(right, height=10, font=_MONO, wrap="none")
        self.output.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        try:
            self.output.insert("end", "CLI: " + " ".join(cli_command()) + "\n")
        except CliNotFound as exc:
            self.output.insert("end", f"ERROR: {exc}\n")

    def _build_help_tab(self) -> None:
        """A searchable reference of every command, plus per-command --help.

        Navigation, in order of how people actually look for things: a search
        box that jumps between matches and highlights them; a command list on
        the left that scrolls the reference to that command; F1 or the button
        on any form, which opens this tab at the command being edited.
        """
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Help  ")
        self.help_tab = tab

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="search").pack(side="left")
        self.search_var = tk.StringVar(value="")
        entry = ttk.Entry(top, textvariable=self.search_var, width=40)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _e: self._search(forward=True))
        entry.bind("<Shift-Return>", lambda _e: self._search(forward=False))
        ttk.Button(top, text="Next", command=lambda: self._search(forward=True)).pack(side="left")
        ttk.Button(top, text="Previous", command=lambda: self._search(forward=False)).pack(side="left", padx=4)
        self.search_status = tk.StringVar(value="Enter to search, Shift+Enter for previous")
        ttk.Label(top, textvariable=self.search_status, style="Hint.TLabel").pack(side="left", padx=12)
        self.search_var.trace_add("write", lambda *_: self._search(forward=True, from_start=True))

        outer = ttk.Panedwindow(tab, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(outer, width=220)
        self.help_index = tk.Listbox(left, activestyle="none", exportselection=False,
                                     background=PALETTE["panel"], foreground=PALETTE["text"],
                                     selectbackground=PALETTE["accent"],
                                     selectforeground=PALETTE["on_accent"],
                                     highlightthickness=0, borderwidth=0, font=_MONO)
        self.help_index.pack(fill="both", expand=True)
        for name in self.commands:
            self.help_index.insert("end", f"abca {name}")
        self.help_index.bind("<<ListboxSelect>>", self._on_help_index)
        outer.add(left, weight=0)

        # Right side, stacked: the selected command's full --help on top, the
        # whole reference below. Both in the tab, no pop-ups -- a help window
        # that opens other windows is not easy to navigate.
        right = ttk.Panedwindow(outer, orient="vertical")
        outer.add(right, weight=1)

        detail_frame = ttk.Labelframe(right, text="this command's --help")
        self.detail_text = scrolledtext.ScrolledText(detail_frame, font=_MONO, wrap="none", height=16)
        self.detail_text.pack(fill="both", expand=True)
        self.detail_text.insert("end", "Pick a command on the left, or press F1 on any form.\n")
        self.detail_text.configure(state="disabled")
        right.add(detail_frame, weight=1)

        ref_frame = ttk.Labelframe(right, text="every command and option (search above)")
        self.help_text = scrolledtext.ScrolledText(ref_frame, font=_MONO, wrap="word")
        self.help_text.pack(fill="both", expand=True)
        self.help_text.tag_configure("hit", background=PALETTE["accent"], foreground=PALETTE["on_accent"])
        self.help_text.tag_configure("current", background=PALETTE["accent_hover"], foreground=PALETTE["on_accent"])
        self.help_text.tag_configure("heading", foreground=PALETTE["hint"], font=(_MONO[0], _MONO[1], "bold"))
        self.help_text.insert("end", reference_text(self.groups))
        self._mark_headings()
        self.help_text.configure(state="disabled")
        right.add(ref_frame, weight=2)

    def _mark_headings(self) -> None:
        """Command names in the reference get the heading tag and a mark to jump to."""
        text = self.help_text
        for name in self.commands:
            needle = f"abca {name}\n"
            index = text.search(needle, "1.0", stopindex="end", exact=True)
            if index:
                text.mark_set(self._mark(name), index)
                text.mark_gravity(self._mark(name), "left")
                text.tag_add("heading", index, f"{index} lineend")

    @staticmethod
    def _mark(name: str) -> str:
        """A Tk mark name for a command. Spaces are not legal in index expressions."""
        return "cmd_" + name.replace(" ", "_").replace("-", "_")

    def _on_help_index(self, _event: Any = None) -> None:
        selected = self.help_index.curselection()
        if not selected:
            return
        name = self.help_index.get(selected[0])[len("abca "):]
        self.jump_to_help(name)

    def jump_to_help(self, name: str) -> None:
        """Scroll the reference to a command and put its full --help in view."""
        self.notebook.select(self.help_tab)
        text = self.help_text
        text.configure(state="normal")
        text.tag_remove("hit", "1.0", "end")
        text.tag_remove("current", "1.0", "end")
        mark = self._mark(name)
        text.see(mark)
        text.mark_set("insert", mark)
        line_end = text.index(f"{mark} lineend")
        text.tag_add("current", mark, line_end)
        detail = command_help(self.commands[name])
        self._show_detail(detail)
        text.configure(state="disabled")
        if name in self.commands:
            idx = list(self.commands).index(name)
            self.help_index.selection_clear(0, "end")
            self.help_index.selection_set(idx)
            self.help_index.see(idx)

    def _show_detail(self, detail: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("end", detail)
        self.detail_text.configure(state="disabled")

    def show_help_for_current(self) -> None:
        if self.current is not None:
            self.jump_to_help(self.current.name)
        else:
            self.notebook.select(self.help_tab)

    def _search(self, *, forward: bool, from_start: bool = False) -> None:
        needle = self.search_var.get().strip()
        text = self.help_text
        text.configure(state="normal")
        text.tag_remove("hit", "1.0", "end")
        text.tag_remove("current", "1.0", "end")
        if not needle:
            self.search_status.set("Enter to search, Shift+Enter for previous")
            text.configure(state="disabled")
            return
        # Highlight every hit, then move to the next/previous relative to the cursor.
        count = 0
        start = "1.0"
        while True:
            index = text.search(needle, start, stopindex="end", nocase=True)
            if not index:
                break
            end = f"{index}+{len(needle)}c"
            text.tag_add("hit", index, end)
            count += 1
            start = end
        origin = "1.0" if from_start else "insert"
        if forward:
            index = text.search(needle, f"{origin}+1c" if not from_start else origin,
                                stopindex="end", nocase=True) or text.search(
                needle, "1.0", stopindex="end", nocase=True)
        else:
            index = text.search(needle, origin, stopindex="1.0", backwards=True, nocase=True) \
                or text.search(needle, "end", stopindex="1.0", backwards=True, nocase=True)
        if index:
            end = f"{index}+{len(needle)}c"
            text.tag_add("current", index, end)
            text.mark_set("insert", end if forward else index)
            text.see(index)
            self.search_status.set(f"{count} match(es)")
        else:
            self.search_status.set("no matches")
        text.configure(state="disabled")

    def _on_select(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if not selected or not selected[0].startswith("cmd:"):
            return
        self.show_command(self.commands[selected[0][4:]])

    def show_command(self, command: CommandSpec) -> None:
        """Rebuild the form for ``command``. Public so tests can drive it."""
        for child in self.form.winfo_children():
            child.destroy()
        self.controls = {}
        self.current = command
        # First paragraph only. `analyze` explains itself at length, and that
        # length belongs in --help, not above a form it would push off screen.
        first = (command.help or "(no help text)").split("\n\n", 1)[0].replace("\n", " ")
        self.help_var.set(f"abca {command.name} — {first}")
        for row, spec in enumerate(command.params):
            control = ParamControl(self.form, spec, row)
            self.controls[spec.name] = control
            if control.var is not None:
                control.var.trace_add("write", lambda *_: self._refresh_command())
            if control.text is not None:
                control.text.bind("<KeyRelease>", lambda _e: self._refresh_command())
        self.form.columnconfigure(1, weight=1)
        self.form.columnconfigure(2, weight=0, minsize=340)
        self._refresh_command()

    # ------------------------------------------------------------ values

    def values(self) -> dict[str, Any]:
        return {name: control.value() for name, control in self.controls.items()}

    def environment(self) -> dict[str, str]:
        return {name: var.get().strip() for name, var in self.env_vars.items() if var.get().strip()}

    def current_argv(self) -> list[str]:
        if self.current is None:
            raise FormError("no command selected")
        return build_argv(self.current, self.values())

    def _refresh_command(self) -> None:
        try:
            argv = self.current_argv()
        except FormError as exc:
            self.command_var.set(f"({exc})")
            return
        env_prefix = " ".join(
            f"{k}={v}" if sys.platform != "win32" else f"$env:{k}='{v}';"
            for k, v in self.environment().items()
        )
        line = render_command(argv, windows=sys.platform == "win32")
        self.command_var.set(f"{env_prefix} {line}".strip())

    # ------------------------------------------------------------ running

    def _on_run(self) -> None:
        if self.run and self.run.running:
            return
        try:
            argv = self.current_argv()
        except FormError as exc:
            messagebox.showerror("Cannot run", str(exc))
            return
        assert self.current is not None
        if self.current.destructive and not messagebox.askyesno(
            "Confirm", f"abca {self.current.name} changes state on disk. Run it?"
        ):
            return
        self.output.insert("end", f"\n$ {render_command(argv)}\n")
        self.output.see("end")
        self.run = CliRun(
            argv, env=self.environment(),
            on_line=lambda line: self.lines.put(("line", line)),
            on_exit=lambda code: self.lines.put(("exit", code)),
        )
        try:
            self.run.start()
        except (CliNotFound, OSError) as exc:
            messagebox.showerror("Cannot run", str(exc))
            return
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("running…")

    def _on_cancel(self) -> None:
        if self.run:
            self.run.cancel()

    def _on_copy(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.command_var.get())
        self.status_var.set("command copied")

    def _drain(self) -> None:
        """Move subprocess output onto the Tk thread. Tk is not thread-safe."""
        try:
            while True:
                kind, payload = self.lines.get_nowait()
                if kind == "line":
                    self.output.insert("end", payload + "\n")
                else:
                    self.output.insert("end", f"[exit {payload}]\n")
                    self.run_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.status_var.set(f"finished, exit {payload}")
                self.output.see("end")
        except queue.Empty:
            pass
        self._drain_after = self.root.after(100, self._drain)

    def close(self) -> None:
        """Stop the drain timer, stop any run, and destroy the window.

        Destroying a Tk root with a pending ``after`` callback leaves Tcl
        trying to call a command that no longer exists; harmless, but it
        prints a traceback on the way out, and a tool that prints tracebacks
        on a clean exit trains people to ignore tracebacks.
        """
        with contextlib.suppress(Exception):   # already gone is fine
            self.root.after_cancel(self._drain_after)
        if self.run and self.run.running:
            self.run.cancel()
        self.root.destroy()

    def run_forever(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    """Open the window -- or, with ``--abca-self-check``, prove it can.

    The self-check is what the release build runs against the FROZEN GUI: it
    constructs the whole window (every widget, the theme, the help reference),
    resolves the CLI beside it, runs one real command through the subprocess
    runner, and exits 0 with "self-check ok". A GUI that cannot do that is not
    a GUI that should be zipped.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--abca-self-check" in args:
        import time

        window = AbcaWindow()
        try:
            window.root.update()
            print("CLI:", " ".join(cli_command()))
            window.show_command(window.commands["version"])
            window.controls["json_out"].var.set(True)
            window._on_run()
            for _ in range(600):
                window.root.update()
                time.sleep(0.05)
                if window.status_var.get().startswith("finished"):
                    break
            if window.status_var.get() != "finished, exit 0":
                print("self-check FAILED:", window.status_var.get())
                print(window.output.get("1.0", "end"))
                return 1
            print("self-check ok")
            return 0
        finally:
            window.close()
    AbcaWindow().run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
