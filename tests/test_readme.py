"""The README is a claim, so it is checked like one.

This repository's central argument is that a claim without verification is
decoration. The README makes claims about itself -- a table of contents that
says where things are, test counts, a build-status table -- and there is no
reason those should be exempt from the standard everything else is held to.

Every failure here is a README that lies to a reader in a small way. Small
inaccuracies in a document whose entire subject is accuracy are not small.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def _lines() -> list[str]:
    return README.read_text(encoding="utf-8").split("\n")


def _headings() -> list[tuple[int, str]]:
    """Every ## and ### heading outside a fenced code block.

    The fence tracking matters: the Quick start section is full of shell
    comments beginning with ``#``, and counting those as headings would produce
    a table of contents pointing at nothing.
    """
    in_fence = False
    found: list[tuple[int, str]] = []
    for line in _lines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,3}) (.+?)\s*$", line)
        if match:
            found.append((len(match.group(1)), match.group(2)))
    return found


def _anchor(text: str) -> str:
    """GitHub's heading-slug rules, as far as this README exercises them.

    Each space becomes ONE hyphen; runs of spaces are NOT collapsed. That
    distinction is not pedantry: a heading containing an em dash surrounded by
    spaces loses the dash and keeps both spaces, so its anchor carries a double
    hyphen. Collapsing here would generate a contents link that resolves in the
    test and 404s on GitHub -- the worst kind of green check.
    """
    slug = text.strip().lower().replace("&", "")
    slug = re.sub(r"[^\w\s-]", "", slug, flags=re.UNICODE)
    return re.sub(r"\s", "-", slug)


def _toc_entries() -> list[tuple[str, str, str]]:
    """``(number, label, anchor)`` for every row of the table of contents."""
    lines = _lines()
    start = lines.index("## Contents")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    entries: list[tuple[str, str, str]] = []
    for line in lines[start:end]:
        match = (
            re.match(r"^(\d+)\.\s+\[(.+?)\]\(#(.+?)\)\s*$", line)
            or re.match(r"^   - \*\*(\d+\.\d+)\*\* \[(.+?)\]\(#(.+?)\)\s*$", line)
        )
        if match:
            entries.append(match.groups())
    return entries


def test_the_table_of_contents_is_not_empty():
    assert len(_toc_entries()) > 50


def test_every_table_of_contents_link_points_at_a_real_heading():
    """A numbered, clickable contents list whose links 404 is worse than none."""
    valid = {_anchor(text) for _level, text in _headings()}
    broken = [(num, label, target) for num, label, target in _toc_entries()
              if target not in valid]
    assert not broken, f"table of contents links with no heading: {broken}"


def test_every_heading_appears_in_the_table_of_contents():
    """The other direction: a section nobody can find from the top is lost."""
    listed = {target for _num, _label, target in _toc_entries()}
    missing = [text for _level, text in _headings()
               if text != "Contents" and _anchor(text) not in listed]
    assert not missing, f"headings absent from the table of contents: {missing}"


def test_the_numbering_is_sequential_and_nesting_matches_heading_depth():
    """A reader uses the numbers to navigate; wrong numbers misdirect them."""
    entries = _toc_entries()
    heads = [h for h in _headings() if h[1] != "Contents"]
    assert len(entries) == len(heads)

    major = 0
    minor = 0
    for (number, _label, _target), (level, _text) in zip(entries, heads, strict=True):
        parts = number.split(".")
        if level == 2:
            major += 1
            minor = 0
            assert parts == [str(major)], f"{number} should be {major}"
        else:
            minor += 1
            assert parts == [str(major), str(minor)], f"{number} should be {major}.{minor}"


def test_the_contents_are_in_document_order():
    """A contents list in a different order than the document is a maze."""
    listed = [target for _num, _label, target in _toc_entries()]
    actual = [_anchor(text) for _level, text in _headings() if text != "Contents"]
    assert listed == actual


def test_no_duplicate_anchors():
    """Two headings with the same text would make one contents link unreachable."""
    anchors = [_anchor(text) for _level, text in _headings()]
    duplicates = sorted({a for a in anchors if anchors.count(a) > 1})
    assert not duplicates, f"duplicate heading anchors: {duplicates}"


def test_every_relative_link_points_at_a_file_that_exists():
    """Links into docs/, identity/, tools/ and scripts/ must resolve."""
    text = README.read_text(encoding="utf-8")
    targets = re.findall(r"\]\((?!https?://|#)([^)\s]+)\)", text)
    missing = sorted({t for t in targets if not (ROOT / t.split("#")[0]).exists()})
    assert not missing, f"README links to files that do not exist: {missing}"


def test_every_in_page_anchor_link_points_at_a_real_heading():
    """Not just the contents list -- cross-references in the body too.

    A link from one section to another that quietly stops resolving is exactly
    the kind of small rot this repository argues against everywhere else.
    """
    text = README.read_text(encoding="utf-8")
    valid = {_anchor(head) for _level, head in _headings()}
    targets = re.findall(r"\]\(#([^)\s]+)\)", text)
    broken = sorted({t for t in targets if t not in valid})
    assert not broken, f"in-page links with no matching heading: {broken}"


def test_the_test_count_the_readme_advertises_matches_the_badge():
    """Two places state the suite size; they must not drift apart."""
    text = README.read_text(encoding="utf-8")
    counts = set(re.findall(r"(\d{3,5})\s+(?:passed|tests)", text))
    badge = set(re.findall(r"tests-(\d{3,5})%20passing", text))
    assert badge, "no test-count badge found"
    assert counts, "no test count quoted in the verification section"
    assert badge <= counts, (
        f"badge says {badge} but the verification output says {counts}; one of "
        "them is wrong and a reader cannot tell which"
    )


def test_the_coverage_the_readme_advertises_matches_the_badge():
    """The same guard as the test count, for the number that actually drifted.

    Coverage is stated in three canonical places: the badge, the ``TOTAL`` line
    of the quoted sweep, and the ``--cov`` line under "Running the tests". They
    are three renderings of one measurement, so a reader who sees them disagree
    cannot tell which one to believe.

    Prose percentages elsewhere are deliberately *not* collected. The README
    legitimately discusses other coverage figures - the pipeline's number before
    the GUI existed, and ``tools/levelset.py`` measured on its own - and a test
    that demanded every percent sign in the document agree would force those
    true statements out to satisfy a regex.
    """
    text = README.read_text(encoding="utf-8")

    badge = re.findall(r"coverage-(\d{1,3})%25", text)
    total = re.findall(r"TOTAL\s+\d+\s+\d+\s+(\d{1,3})%", text)
    cov_cmd = re.findall(r"--cov-report=term\s+#\s*(\d{1,3})%\s*coverage", text)

    assert badge, "no coverage badge found"
    assert total, "no TOTAL line quoted in the verification section"
    assert cov_cmd, "no coverage figure under 'Running the tests'"

    stated = set(badge) | set(total) | set(cov_cmd)
    assert len(stated) == 1, (
        f"the README states coverage as {sorted(stated)} in different places "
        f"(badge={badge}, TOTAL={total}, --cov line={cov_cmd}); they are one "
        "measurement and a reader cannot tell which is current"
    )


@pytest.mark.parametrize("script", [
    "scripts/verify_all.sh",
    "scripts/check_symmetry.py",
    "scripts/check_levelset_symmetry.py",
    "scripts/check_context_pack.py",
    "scripts/check_contrast.py",
    "scripts/build_identity.py",
    "tools/levelset.py",
])
def test_every_command_the_readme_tells_a_reader_to_run_exists(script):
    text = README.read_text(encoding="utf-8")
    assert script in text, f"{script} is not mentioned in the README"
    assert (ROOT / script).exists()
