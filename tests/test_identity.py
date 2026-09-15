"""The tool's identity is data, and it is held to the tool's own standard.

Three things are checked here, and each exists because the alternative is a
brand guide that quietly stops being true:

1. **``src/`` never reads ``identity/``.** The same rule that keeps every
   other input out of the Analyzer's code keeps its own mascot out of it. A
   tool that knows whose claims it is checking can, eventually, be kind to
   one of them -- and that holds whether the tell is a name or a color.
2. **The marks match their generator.** Every SVG in ``identity/logo/`` is
   derived from one outline. A file edited by hand is a second copy of the
   animal, and a second copy is how a project ends up with nine slightly
   different mascots.
3. **The style guide's contrast numbers are true.** They are measured from the
   hex values, not asserted. A guide that claims accessibility without
   measuring it is decoration.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "abca"
IDENTITY = REPO_ROOT / "identity"
LOGO = IDENTITY / "logo"


# --------------------------------------------------------------------------
# Separation
# --------------------------------------------------------------------------


class TestIdentityIsData:
    def test_src_never_reads_the_identity_directory(self):
        """The Analyzer cannot import its own self-description.

        A string search rather than an import graph: the risk is any hard-coded
        path into the directory, not only a statement Python would recognise as
        an import.
        """
        offenders: list[str] = []
        for path in SRC_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in ('"identity/', "'identity/", "from identity",
                           "import identity", 'Path("identity")'):
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
        assert offenders == [], (
            "src/ must not reach into identity/. The mascot, colors and motto "
            "are branding, and an Analyzer that can read them can be tuned "
            "by them:\n  " + "\n  ".join(offenders)
        )

    def test_the_prompts_never_name_the_tool_identity(self):
        """A prompt that knows the mascot knows whose side it is on."""
        from abca.prompts import PROMPTS_ROOT

        blob = " ".join(
            path.read_text(encoding="utf-8").lower()
            for path in PROMPTS_ROOT.rglob("*.md")
        )
        for term in ("mongoose", "verdigris", "abca", "check it yourself"):
            assert term not in blob, (
                f"the Analyzer's prompts mention {term!r}; the Analyzer is not "
                "supposed to know it belongs to anyone"
            )


# --------------------------------------------------------------------------
# The marks
# --------------------------------------------------------------------------


class TestMarks:
    EXPECTED: ClassVar[set[str]] = {
        "abca-seal.svg", "abca-seal-mono.svg", "abca-seal-reversed.svg",
        "abca-mark.svg", "abca-mark-graphite.svg", "abca-mark-reversed.svg",
        "abca-lockup.svg", "abca-lockup-mono.svg", "abca-lockup-reversed.svg",
        "abca-wordmark.svg", "abca-wordmark-reversed.svg",
        "favicon.svg", "abca-palette.svg",
    }

    def test_every_expected_mark_exists(self):
        assert {p.name for p in LOGO.glob("*.svg")} == self.EXPECTED

    def test_the_files_match_their_generator(self):
        """``build_identity.py --check`` is the single source of truth."""
        result = subprocess.run(
            [sys.executable, "scripts/build_identity.py", "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_each_mark_is_well_formed_xml(self, name):
        """A logo that does not parse is a logo that will not print."""
        from xml.etree import ElementTree

        root = ElementTree.parse(LOGO / name).getroot()
        assert root.tag.endswith("svg")
        assert root.get("viewBox"), f"{name} has no viewBox and will not scale"

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_no_mark_references_an_external_file(self, name):
        """Marks must be self-contained: no linked images, no webfonts.

        A mark that fetches something is a mark that renders differently on a
        machine with no network, which is every printer's machine.
        """
        text = (LOGO / name).read_text(encoding="utf-8")
        # The xmlns declaration is a namespace NAME that happens to look like a
        # URL. Nothing fetches it, so it is not a external reference.
        body = text.replace('xmlns="http://www.w3.org/2000/svg"', "")
        for forbidden in ("http://", "https://", "@import", "<image", "xlink:href"):
            assert forbidden not in body, f"{name} references {forbidden}"

    def test_the_only_colors_used_are_the_four_tokens(self):
        """No stray hex. The colorways are the identity."""
        tokens = json.loads((IDENTITY / "tokens.json").read_text(encoding="utf-8"))
        allowed = {v.upper() for v in {**tokens["core"], **tokens["derived"]}.values()}
        for path in LOGO.glob("*.svg"):
            for found in re.findall(r"#[0-9A-Fa-f]{6}", path.read_text(encoding="utf-8")):
                assert found.upper() in allowed, f"{path.name} uses {found}, not a token"

    def test_the_mongoose_appears_exactly_once_in_the_source(self):
        """One outline, one copy. A second copy is how the drift starts."""
        generator = (REPO_ROOT / "scripts" / "build_identity.py").read_text(encoding="utf-8")
        assert generator.count("MONGOOSE_HEAD = ") == 1
        # The head path is referenced by name everywhere else, never re-pasted.
        assert generator.count("C 173 113, 159 120, 143 126") == 1


# --------------------------------------------------------------------------
# The style guide tells the truth about itself
# --------------------------------------------------------------------------


class TestContrastClaims:
    def test_the_guide_matches_its_own_colors(self):
        result = subprocess.run(
            [sys.executable, "scripts/check_contrast.py"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_brass_is_documented_as_failing_on_light(self):
        """The two failures are load-bearing documentation, not an oversight.

        Somebody will eventually want gold text on parchment. The token file
        records that it is 2.14:1 so the answer is already written down.
        """
        tokens = json.loads((IDENTITY / "tokens.json").read_text(encoding="utf-8"))
        failures = {
            (row["foreground"], row["background"])
            for row in tokens["contrast"] if row["grade"] == "fail"
        }
        assert failures == {("brass", "parchment"), ("brass", "verdigris")}

    def test_the_brand_guide_quotes_the_measured_numbers(self):
        """README numbers are copied from tokens.json, so they must agree."""
        tokens = json.loads((IDENTITY / "tokens.json").read_text(encoding="utf-8"))
        guide = (IDENTITY / "README.md").read_text(encoding="utf-8")
        for row in tokens["contrast"]:
            quoted = f"{row['ratio']}:1" in guide or f"{row['ratio']:.2f}:1" in guide
            assert quoted, (
                f"identity/README.md does not quote the measured "
                f"{row['foreground']}-on-{row['background']} ratio {row['ratio']}:1"
            )

    def test_every_token_hex_appears_in_the_guide(self):
        tokens = json.loads((IDENTITY / "tokens.json").read_text(encoding="utf-8"))
        guide = (IDENTITY / "README.md").read_text(encoding="utf-8")
        for name, value in {**tokens["core"], **tokens["derived"]}.items():
            if name == "white":
                continue
            assert value in guide, f"{name} ({value}) is not documented in the guide"


# --------------------------------------------------------------------------
# The documents
# --------------------------------------------------------------------------


class TestIdentityDocuments:
    def test_the_guide_exists_and_is_substantive(self):
        text = (IDENTITY / "README.md").read_text(encoding="utf-8")
        assert len(text) > 800, "identity/README.md is a stub"

    def test_the_guide_never_claims_immunity(self):
        """'Resistant, not immune' is the mark's whole argument; keep it honest."""
        text = (IDENTITY / "README.md").read_text(encoding="utf-8")
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            low = sentence.lower()
            if "immune" in low and "not immune" not in low and "would be" not in low \
                    and "honest word" not in low:
                pytest.fail(f"unqualified immunity claim: {sentence.strip()!r}")
