"""Verify that identity/tokens.json tells the truth about its own contrast.

WHY THIS SCRIPT EXISTS
======================
A brand guide that asserts "AA compliant" without measuring it is decoration,
and this tool's entire claim is that it does not publish unverified assertions.
Its own style guide is not exempt from that. So the contrast numbers printed in
the guide are computed here, from the hex values in ``identity/tokens.json``,
and this script fails if the recorded numbers drift from the measured ones.

Someone editing a hex value to make a color "pop" will therefore break the
build rather than quietly ship an unreadable page.

WHAT IT CHECKS
==============
1. Every ``contrast`` row's ``ratio`` matches WCAG 2.1 relative luminance, to
   two decimal places.
2. Every ``grade`` matches the ratio it claims (AAA >= 7, AA >= 4.5,
   AA-large >= 3, otherwise fail).
3. Every color named in a row exists in ``core`` or ``derived``.
4. ``tokens.css`` declares a custom property for every token in the JSON, so
   the two files cannot drift apart.

The two rows graded ``fail`` are deliberate and are asserted to stay failing:
brass is an ornament color on light grounds and the guide says so. If a future
palette edit made them pass, that is still a change worth noticing.

Usage::

    python scripts/check_contrast.py        # exit 0 when the guide is honest
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS_JSON = ROOT / "identity" / "tokens.json"
TOKENS_CSS = ROOT / "identity" / "tokens.css"

#: Pairings the guide documents as failing on purpose. Kept explicit so that a
#: palette change which "fixes" them is still a visible diff.
EXPECTED_FAILURES = {("brass", "parchment"), ("brass", "verdigris")}


def _channel(value: int) -> float:
    """Linearize one 0-255 sRGB channel per WCAG 2.1."""
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    """WCAG relative luminance of a ``#RRGGBB`` string."""
    digits = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", digits):
        raise ValueError(f"not a 6-digit hex color: {hex_color!r}")
    r, g, b = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """Contrast ratio between two hex colors, rounded as the guide prints it."""
    la, lb = luminance(a), luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def grade_for(ratio: float) -> str:
    """The WCAG grade a ratio actually earns."""
    if ratio >= 7.0:
        return "AAA"
    if ratio >= 4.5:
        return "AA"
    if ratio >= 3.0:
        return "AA-large"
    return "fail"


def main() -> int:
    tokens = json.loads(TOKENS_JSON.read_text(encoding="utf-8"))
    colors: dict[str, str] = {**tokens["core"], **tokens["derived"]}
    problems: list[str] = []

    for row in tokens["contrast"]:
        fg, bg = row["foreground"], row["background"]
        for name in (fg, bg):
            if name not in colors:
                problems.append(f"{fg} on {bg}: no such token {name!r}")
        if fg not in colors or bg not in colors:
            continue

        measured = contrast(colors[fg], colors[bg])
        if measured != row["ratio"]:
            problems.append(
                f"{fg} on {bg}: guide says {row['ratio']}:1, measured {measured}:1"
            )
        earned = grade_for(measured)
        if earned != row["grade"]:
            problems.append(
                f"{fg} on {bg}: guide grades it {row['grade']}, {measured}:1 earns {earned}"
            )

    recorded_failures = {
        (r["foreground"], r["background"])
        for r in tokens["contrast"]
        if r["grade"] == "fail"
    }
    if recorded_failures != EXPECTED_FAILURES:
        problems.append(
            "the documented-failure set changed: "
            f"expected {sorted(EXPECTED_FAILURES)}, found {sorted(recorded_failures)}"
        )

    css = TOKENS_CSS.read_text(encoding="utf-8")
    for name, value in colors.items():
        if f"--abca-{name}: {value};" not in css:
            problems.append(f"tokens.css is missing or disagrees on --abca-{name}: {value}")

    if problems:
        print("identity/tokens.json does NOT match its own colors:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n{len(problems)} problem(s). The style guide is currently wrong.")
        return 1

    print(f"{len(tokens['contrast'])} pairings checked against WCAG 2.1 relative luminance.")
    for row in tokens["contrast"]:
        marker = "  " if row["grade"] != "fail" else "! "
        print(
            f"{marker}{row['foreground']:>14} on {row['background']:<14}"
            f"{row['ratio']:>6}:1  {row['grade']:<9} {row['use']}"
        )
    print("\nThe guide's contrast claims are accurate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
