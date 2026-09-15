"""Regenerate the README's numbered table of contents from its headings.

The contents list is generated, not hand-maintained, for the same reason the
identity marks are: a hand-edited copy of something derivable is where drift
starts. ``tests/test_readme.py`` verifies the result -- every link resolves,
every heading is listed, the numbering matches the depth, the order matches the
document -- so this script and that test together mean the contents cannot rot.

    python scripts/build_readme_toc.py          # rewrite README.md in place
    python scripts/build_readme_toc.py --check  # exit 1 if it would change

GitHub's anchor rule, as far as this README exercises it: lowercase, strip
punctuation, replace EACH space with one hyphen. Runs of spaces are not
collapsed -- a heading with a spaced em dash loses the dash, keeps both spaces,
and gets a double hyphen. Collapsing here would produce a link that resolves in
a test and 404s on GitHub, which is the worst kind of green check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def anchor(text: str) -> str:
    slug = text.strip().lower().replace("&", "")
    slug = re.sub(r"[^\w\s-]", "", slug, flags=re.UNICODE)
    return re.sub(r"\s", "-", slug)


def headings(lines: list[str]) -> list[tuple[int, str]]:
    in_fence = False
    found: list[tuple[int, str]] = []
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,3}) (.+?)\s*$", line)
        if match and match.group(2) != "Contents":
            found.append((len(match.group(1)), match.group(2)))
    return found


def build(lines: list[str]) -> list[str]:
    """A two-level contents list that GitHub renders as a nested list.

    Sections are an ordered list; their subsections are an unordered list
    nested three spaces under the parent item, each carrying its own
    ``major.minor`` number in bold. The first version of this script indented
    ``2.1.`` lines by four spaces, which CommonMark does not treat as a nested
    list marker at all -- the lines rendered as loose text hanging under each
    number. A contents list has one job, and that one was not doing it.
    """
    out = ["## Contents", ""]
    major = minor = 0
    for level, text in headings(lines):
        link = f"[{text.replace('`', '')}](#{anchor(text)})"
        if level == 2:
            major, minor = major + 1, 0
            out.append(f"{major}. {link}")
        else:
            minor += 1
            out.append(f"   - **{major}.{minor}** {link}")
    return [*out, ""]


def main() -> int:
    lines = README.read_text(encoding="utf-8").split("\n")
    start = lines.index("## Contents")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    new = lines[:start] + build(lines) + lines[end:]
    if "--check" in sys.argv:
        if new != lines:
            print("README contents list is stale; run scripts/build_readme_toc.py")
            return 1
        print("README contents list is current")
        return 0
    README.write_text("\n".join(new), encoding="utf-8")
    print(f"{len(headings(lines))} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
