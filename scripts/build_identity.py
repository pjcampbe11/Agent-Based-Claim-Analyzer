"""Generate every abCA logo file from one source of truth.

WHY THE LOGOS ARE GENERATED RATHER THAN DRAWN
=============================================
A project's mark gets redrawn by whoever needs it -- a README, a slide deck, a
sticker vendor -- and the versions drift. Here the silhouette exists exactly once,
as ``MONGOOSE_HEAD`` below, and every variant (mark, seal, lockup, favicon,
mono, reversed) is derived from it. Changing the animal is one edit and a
rebuild; there is no second copy to forget.

It also means the mark is auditable. The outline is 20 lines of coordinates in
a text file, not a binary someone traced in a design tool, so a reviewer can
diff it.

Usage::

    python scripts/build_identity.py          # writes identity/logo/*.svg
    python scripts/build_identity.py --check  # fails if the files are stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "identity" / "logo"

# Palette. Duplicated from identity/tokens.json deliberately: an SVG must carry
# literal hex, and scripts/check_contrast.py asserts the two agree.
VERDIGRIS = "#0E6E6E"
GRAPHITE = "#1B2A2E"
BRASS = "#C9A227"
PARCHMENT = "#F4F1EA"

#: The mongoose head, in profile, facing right, in a 200x200 field.
#:
#: One closed loop traversed once: nose tip -> lower jaw -> neck cut -> nape ->
#: ear -> crown -> brow -> top of muzzle -> nose. The ear is a LOW ROUNDED bump,
#: not a point: a pointed ear reads as fox or cat, and the small round ear set
#: back on the skull is the feature that says mongoose.
MONGOOSE_HEAD = """M 184 106
  C 173 113, 159 120, 143 126
  C 126 132, 107 136, 89 137
  C 77 138, 65 137, 55 135
  L 33 104
  C 33  94,  36 83,  41 74
  C 44  69,  48 64,  52 61
  C 54  55,  59 51,  66 51
  C 72  51,  77 55,  79 60
  C 88  56,  99 55, 109 58
  C 118 61, 125 65, 131 71
  C 136 76, 141 81, 147 86
  C 157 94, 169 101, 178 104
  C 181 105, 183 106, 184 106
  Z"""

#: The eye, as a counter-shape rather than a second filled circle. With
#: ``fill-rule="evenodd"`` the hole is part of the same path, so the mark is a
#: SINGLE path that works on any background and cannot lose its eye when
#: somebody recolors it.
EYE = "M 114 77 a 7.5 6 -20 1 0 0.1 0 Z"

HEAD_PATH = f"{MONGOOSE_HEAD} {EYE}"

#: Type stack. The masters carry live text so anyone can re-set the wordmark;
#: production print files should have it converted to outlines, which is noted
#: in identity/README.md rather than assumed.
SANS = "Helvetica Neue, Helvetica, Arial, DejaVu Sans, sans-serif"


def _svg(body: str, *, w: int, h: int, view: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
        f'width="{w}" height="{h}" role="img">\n{body}\n</svg>\n'
    )


def mark(fill: str) -> str:
    """The head alone, on transparency. The atomic unit of the identity."""
    return _svg(
        f'  <title>abCA mongoose mark</title>\n'
        f'  <path fill="{fill}" fill-rule="evenodd" d="{HEAD_PATH}"/>',
        w=200, h=200, view="0 0 200 200",
    )


def favicon() -> str:
    """Rounded square, verdigris field, head knocked out. Reads at 16px."""
    return _svg(
        f'  <title>abCA</title>\n'
        f'  <rect width="64" height="64" rx="12" fill="{VERDIGRIS}"/>\n'
        f'  <g transform="translate(4 8) scale(0.28)">\n'
        f'    <path fill="{PARCHMENT}" fill-rule="evenodd" d="{HEAD_PATH}"/>\n'
        f'  </g>',
        w=64, h=64, view="0 0 64 64",
    )


def seal(*, ring: str, field: str, animal: str, star: str, text: str) -> str:
    """The abCA seal.

    Ring text is set in ENGLISH, not Latin. A tool whose central commitment is
    that legal language must be explained at an eighth-grade reading level
    cannot put a motto on its seal that most people can't read. The absence
    of Latin is the argument.
    """
    top = "M 200 200 m -168 0 a 168 168 0 1 1 336 0"
    bottom = "M 32 200 a 168 168 0 0 0 336 0"
    return _svg(
        f'  <title>Seal of the agent-based Claim Analyzer</title>\n'
        f'  <circle cx="200" cy="200" r="198" fill="{field}"/>\n'
        f'  <circle cx="200" cy="200" r="192" fill="none" stroke="{ring}" stroke-width="2.5"/>\n'
        f'  <circle cx="200" cy="200" r="146" fill="none" stroke="{ring}" stroke-width="7"/>\n'
        f'  <circle cx="200" cy="200" r="136" fill="{ring}"/>\n'
        f'  <defs>\n'
        f'    <path id="seal-top" d="{top}"/>\n'
        f'    <path id="seal-bottom" d="{bottom}"/>\n'
        f'  </defs>\n'
        f'  <g font-family="{SANS}" font-weight="700" fill="{text}"'
        f' font-size="23" letter-spacing="1.6">\n'
        f'    <text><textPath href="#seal-top" startOffset="50%" text-anchor="middle">'
        f'AGENT-BASED CLAIM ANALYZER</textPath></text>\n'
        f'    <text font-size="23" letter-spacing="5"><textPath href="#seal-bottom" '
        f'startOffset="50%" text-anchor="middle">CHECK IT YOURSELF</textPath></text>\n'
        f'  </g>\n'
        f'  <g fill="{star}">\n    <path transform="translate(26 200)" d="M 0.00 -11.00 L 2.70 -3.72 L 10.46 -3.40 L 4.37 1.42 L 6.47 8.90 L 0.00 4.60 L -6.47 8.90 L -4.37 1.42 L -10.46 -3.40 L -2.70 -3.72 Z"/>\n    <path transform="translate(374 200)" d="M 0.00 -11.00 L 2.70 -3.72 L 10.46 -3.40 L 4.37 1.42 L 6.47 8.90 L 0.00 4.60 L -6.47 8.90 L -4.37 1.42 L -10.46 -3.40 L -2.70 -3.72 Z"/>\n  </g>\n'
        f'  <g transform="translate(89.3 104.1) scale(1.02)">\n'
        f'    <path fill="{animal}" fill-rule="evenodd" d="{HEAD_PATH}"/>\n'
        f'  </g>',
        w=400, h=400, view="0 0 400 400",
    )


def lockup(*, animal: str, word: str, sub: str, rule: str) -> str:
    """Horizontal lockup: mark, vertical rule, wordmark over descriptor."""
    return _svg(
        f'  <title>abCA - agent-based Claim Analyzer</title>\n'
        f'  <g transform="translate(0 6) scale(0.62)">\n'
        f'    <path fill="{animal}" fill-rule="evenodd" d="{HEAD_PATH}"/>\n'
        f'  </g>\n'
        f'  <line x1="140" y1="26" x2="140" y2="106" stroke="{rule}" stroke-width="2"/>\n'
        f'  <text x="162" y="66" font-family="{SANS}" font-size="52" font-weight="700"'
        f' letter-spacing="2" fill="{word}">abCA</text>\n'
        f'  <text x="164" y="92" font-family="{SANS}" font-size="16" font-weight="500"'
        f' letter-spacing="2.2" fill="{sub}">AGENT-BASED CLAIM ANALYZER</text>',
        w=560, h=140, view="0 0 560 140",
    )


def wordmark(*, word: str, sub: str) -> str:
    return _svg(
        f'  <title>abCA wordmark</title>\n'
        f'  <text x="0" y="56" font-family="{SANS}" font-size="58" font-weight="700"'
        f' letter-spacing="2" fill="{word}">abCA</text>\n'
        f'  <text x="2" y="84" font-family="{SANS}" font-size="16.5" font-weight="500"'
        f' letter-spacing="2.4" fill="{sub}">AGENT-BASED CLAIM ANALYZER</text>',
        w=420, h=110, view="0 0 420 110",
    )


def palette_strip() -> str:
    """A swatch strip for READMEs, where a CSS table cannot render.

    Generated from the same four constants as everything else, so the strip
    cannot show a color the identity does not actually use.
    """
    swatches = [
        (VERDIGRIS, "VERDIGRIS", "#0E6E6E", PARCHMENT, "Primary"),
        (GRAPHITE, "GRAPHITE", "#1B2A2E", PARCHMENT, "Ink"),
        (BRASS, "BRASS", "#C9A227", GRAPHITE, "Accent"),
        (PARCHMENT, "PARCHMENT", "#F4F1EA", GRAPHITE, "Ground"),
    ]
    w, h = 150, 116
    cells = []
    for i, (fill, name, hexv, ink, role) in enumerate(swatches):
        x = i * w
        cells.append(
            f'  <g transform="translate({x} 0)">\n'
            f'    <rect width="{w}" height="{h}" fill="{fill}"/>\n'
            f'    <text x="16" y="34" font-family="{SANS}" font-size="13" font-weight="700"'
            f' letter-spacing="1.6" fill="{ink}">{name}</text>\n'
            f'    <text x="16" y="56" font-family="ui-monospace, Menlo, monospace"'
            f' font-size="13" fill="{ink}" opacity="0.82">{hexv}</text>\n'
            f'    <text x="16" y="98" font-family="{SANS}" font-size="11.5"'
            f' letter-spacing="1.2" fill="{ink}" opacity="0.66">{role.upper()}</text>\n'
            f'  </g>'
        )
    return _svg(
        '  <title>abCA palette</title>\n' + "\n".join(cells)
        + f'\n  <rect width="{w * 4}" height="{h}" fill="none" stroke="{GRAPHITE}"'
          f' stroke-width="1" opacity="0.18"/>',
        w=w * 4, h=h, view=f"0 0 {w * 4} {h}",
    )


FILES: dict[str, str] = {
    # The mark, in each of the three legal colorways and no others.
    "abca-mark.svg": mark(VERDIGRIS),
    "abca-mark-graphite.svg": mark(GRAPHITE),
    "abca-mark-reversed.svg": mark(PARCHMENT),
    # Seals. Primary is verdigris field, animal knocked out in parchment.
    "abca-seal.svg": seal(ring=VERDIGRIS, field=PARCHMENT, animal=PARCHMENT,
                          star=BRASS, text=GRAPHITE),
    "abca-seal-mono.svg": seal(ring=GRAPHITE, field=PARCHMENT, animal=PARCHMENT,
                               star=GRAPHITE, text=GRAPHITE),
    "abca-seal-reversed.svg": seal(ring=PARCHMENT, field=GRAPHITE, animal=GRAPHITE,
                                   star=BRASS, text=PARCHMENT),
    # Lockups.
    "abca-lockup.svg": lockup(animal=VERDIGRIS, word=GRAPHITE, sub=VERDIGRIS, rule=BRASS),
    "abca-lockup-mono.svg": lockup(animal=GRAPHITE, word=GRAPHITE, sub=GRAPHITE, rule=GRAPHITE),
    "abca-lockup-reversed.svg": lockup(animal=PARCHMENT, word=PARCHMENT, sub=PARCHMENT, rule=BRASS),
    # Wordmarks and favicon.
    "abca-wordmark.svg": wordmark(word=GRAPHITE, sub=VERDIGRIS),
    "abca-wordmark-reversed.svg": wordmark(word=PARCHMENT, sub=PARCHMENT),
    "favicon.svg": favicon(),
    # For READMEs and anywhere a CSS swatch table cannot render.
    "abca-palette.svg": palette_strip(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if any file on disk differs from what this "
                             "script generates, instead of writing")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, content in FILES.items():
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8")

    if args.check:
        if stale:
            print("These logo files no longer match scripts/build_identity.py:")
            for name in stale:
                print(f"  - identity/logo/{name}")
            print("\nRun: python scripts/build_identity.py")
            return 1
        print(f"All {len(FILES)} logo files match the generator.")
        return 0

    print(f"Wrote {len(FILES)} files to identity/logo/")
    for name in sorted(FILES):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
