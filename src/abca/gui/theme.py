"""Dark by default, from the abCA palette.

The window uses the abCA brand tokens rather than a generic dark theme, so the desktop app and the brand guide agree: verdigris on
ink. Colours are hard-coded here rather than read from the tokens file at run
time because the frozen executable does not ship the brand directory -- and
because ``src/`` is forbidden from reading it at all (the Analyzer must not be
tunable by its own self-description; a test scans for any such path). The
test suite reads both sides and asserts they match, so a palette change in one place fails
a test rather than drifting.

Tk has no native dark mode. ``ttk``'s "clam" theme is the one that honours
every colour option on every platform, including Windows, so it is used and
recoloured wholesale. On Windows the title bar is also asked to go dark through
the DWM attribute, which is the only part that needs a platform call.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk

#: Every value is a brand token, by name, and
#: tests/test_gui.py asserts that -- and measures every text/ground pair below
#: against the same WCAG contrast function the brand guide is held to. Dark
#: mode is not exempt from the standard the light palette had to meet.
#:
#:   text on ground       parchment on graphite        13.1  AAA
#:   text on panels       parchment on graphite-soft    8.8  AAA
#:   hints on ground      brass on graphite             6.1  AA
#:   button text          white on verdigris            6.0  AA
#:   warning button       graphite on brass             6.1  AA
PALETTE = {
    "ground": "#1B2A2E",     # graphite
    "panel": "#2E474E",      # graphite-soft: entries, tree, output
    "text": "#F4F1EA",       # parchment
    "hint": "#C9A227",       # brass
    "accent": "#0E6E6E",     # verdigris: buttons, selection
    "accent_hover": "#149E9E",  # verdigris-pale
    "accent_deep": "#0B5555",   # verdigris-deep: pressed
    "warn": "#C9A227",       # brass: destructive confirm
    "on_accent": "#FFFFFF",  # white
    "on_warn": "#1B2A2E",    # graphite
}

#: Which token each palette entry mirrors. The test reads the tokens file and
#: checks the hex values agree, so a palette edit in one place fails a test
#: instead of drifting the desktop app away from the brand guide.
TOKEN_OF = {
    "ground": "graphite", "panel": "graphite-soft", "text": "parchment",
    "hint": "brass", "accent": "verdigris", "accent_hover": "verdigris-pale",
    "accent_deep": "verdigris-deep", "warn": "brass", "on_accent": "white",
    "on_warn": "graphite",
}

#: (foreground, background) pairs that carry text, each of which must clear
#: WCAG AA (4.5:1). Measured by the test, not asserted here.
TEXT_PAIRS = (
    ("text", "ground"), ("text", "panel"), ("hint", "ground"),
    ("on_accent", "accent"), ("on_warn", "warn"),
)


def apply_dark(root: tk.Tk) -> ttk.Style:
    """Recolour every widget class the window uses. Returns the style for reuse.

    tkinter is imported here, not at module level, so the palette itself --
    which the tests compare against the brand tokens -- is importable on an
    interpreter with no Tk at all.
    """
    from tkinter import ttk

    p = PALETTE
    root.configure(background=p["ground"])
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=p["ground"], foreground=p["text"],
                    fieldbackground=p["panel"], bordercolor=p["accent_deep"],
                    lightcolor=p["accent_deep"], darkcolor=p["ground"],
                    troughcolor=p["panel"], selectbackground=p["accent"],
                    selectforeground=p["on_accent"], insertcolor=p["text"])
    style.configure("TFrame", background=p["ground"])
    style.configure("TLabel", background=p["ground"], foreground=p["text"])
    style.configure("Hint.TLabel", foreground=p["hint"])
    style.configure("Title.TLabel", font=("TkDefaultFont", 11, "bold"))
    style.configure("TLabelframe", background=p["ground"], bordercolor=p["accent_deep"])
    style.configure("TLabelframe.Label", background=p["ground"], foreground=p["hint"])
    style.configure("TEntry", fieldbackground=p["panel"], foreground=p["text"],
                    insertcolor=p["text"], bordercolor=p["accent_deep"])
    style.map("TEntry", fieldbackground=[("readonly", p["panel"])],
              foreground=[("readonly", p["text"])])
    style.configure("TCombobox", fieldbackground=p["panel"], foreground=p["text"],
                    background=p["panel"], arrowcolor=p["text"])
    style.map("TCombobox", fieldbackground=[("readonly", p["panel"])],
              foreground=[("readonly", p["text"])])
    style.configure("TCheckbutton", background=p["ground"], foreground=p["text"])
    style.map("TCheckbutton", background=[("active", p["ground"])],
              indicatorcolor=[("selected", p["accent"]), ("!selected", p["panel"])])
    style.configure("TButton", background=p["accent_deep"], foreground=p["text"],
                    bordercolor=p["accent_deep"], padding=(10, 4))
    style.map("TButton", background=[("active", p["accent"]), ("disabled", p["panel"])],
              foreground=[("active", p["on_accent"]), ("disabled", p["hint"])])
    style.configure("Run.TButton", background=p["accent"], foreground=p["on_accent"])
    style.map("Run.TButton", background=[("active", p["accent_hover"]), ("disabled", p["panel"])],
              foreground=[("disabled", p["hint"])])
    style.configure("Danger.TButton", background=p["warn"], foreground=p["on_warn"])
    style.configure("Treeview", background=p["panel"], fieldbackground=p["panel"],
                    foreground=p["text"], bordercolor=p["accent_deep"], rowheight=22)
    style.map("Treeview", background=[("selected", p["accent"])],
              foreground=[("selected", p["on_accent"])])
    style.configure("TNotebook", background=p["ground"], bordercolor=p["accent_deep"])
    style.configure("TNotebook.Tab", background=p["panel"], foreground=p["hint"], padding=(12, 5))
    style.map("TNotebook.Tab", background=[("selected", p["ground"])],
              foreground=[("selected", p["text"])])
    style.configure("TPanedwindow", background=p["ground"])
    style.configure("Sash", sashthickness=6, background=p["accent_deep"])
    style.configure("TScrollbar", background=p["accent_deep"], troughcolor=p["ground"],
                    arrowcolor=p["hint"], bordercolor=p["ground"])
    style.configure("TSpinbox", fieldbackground=p["panel"], foreground=p["text"])

    # Classic (non-ttk) widgets take options directly.
    root.option_add("*Text.background", p["panel"])
    root.option_add("*Text.foreground", p["text"])
    root.option_add("*Text.insertBackground", p["text"])
    root.option_add("*Text.selectBackground", p["accent"])
    root.option_add("*Text.selectForeground", p["on_accent"])
    root.option_add("*Text.highlightThickness", 0)
    root.option_add("*Canvas.background", p["ground"])
    root.option_add("*TCombobox*Listbox.background", p["panel"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])

    _dark_title_bar(root)
    return style


def _dark_title_bar(root: tk.Tk) -> None:
    """Ask Windows for a dark title bar. No-op elsewhere, harmless if refused."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 on Windows 10 20H1+, 19 before.
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:
        pass


__all__ = ["PALETTE", "TEXT_PAIRS", "TOKEN_OF", "apply_dark"]
