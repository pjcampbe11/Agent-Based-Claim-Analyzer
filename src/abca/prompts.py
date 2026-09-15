"""Versioned, hashed prompt loading.

WHY PROMPTS ARE FILES AND NOT STRING LITERALS
=============================================
Three properties fall out of keeping them on disk, and all three are load-bearing:

1. **Diffable.** The README claims the Analyzer's prompts contain no political
   positions. That claim is only checkable if a reviewer can read them in one
   place and see the diff when they change. A prompt spliced together from
   f-strings across four modules is not auditable.

2. **Hashable.** Every run records the SHA-256 of each prompt file it used, and
   those hashes are part of ``input_digest``. So an edited prompt changes the
   recipe, and a verdict produced under the old wording cannot be silently
   claimed for the new one.

3. **Version-bump-proof.** ``PROMPT_CONTRACT_VERSION`` states intent; the file
   hash catches the case where someone edits a prompt and forgets to bump it.
   That is the failure mode that would otherwise let the contract drift with
   nobody noticing.

PACKAGE DATA, NOT REPO PATHS
============================
Prompts live under ``src/abca/prompts/`` so they ship with an installed wheel.
A repo-relative path would work from a git checkout and break the moment
someone ``pip install``s the tool -- and it would break at run time, halfway
through an analysis, rather than at import.

WHAT MAY NOT GO IN A PROMPT
===========================
No political position, no example that takes a side on a live political question,
and no instruction that would make one verdict easier to reach than its
opposite. :func:`assert_doctrine_free` encodes a blunt version of this as a
test. It is a smoke alarm, not a proof -- it cannot detect subtle steering --
but it catches the obvious regression, and its failure message names the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from abca.canonical import digest_bytes
from abca.schema.enums import StageName
from abca.schema.ledger import PromptRef
from abca.version import PROMPT_CONTRACT_VERSION

#: Root of the shipped prompt tree.
PROMPTS_ROOT = Path(__file__).parent / "prompts"

#: Contract version -> directory. Split out so a future contract revision can
#: ship alongside the current one and be A/B'd, rather than replacing it and
#: orphaning every published run hash.
_CONTRACT_DIR = PROMPT_CONTRACT_VERSION.replace("sotp/", "")


class PromptNotFound(FileNotFoundError):
    """A stage has no prompt file under the active contract version."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One loaded prompt: its text, its provenance, and its hash."""

    stage: StageName
    path: str
    text: str
    content_hash: str

    def to_ref(self) -> PromptRef:
        """The ledger's record of this prompt. Carries the hash, not the text."""
        return PromptRef(stage=self.stage, path=self.path, content_hash=self.content_hash)


def prompt_path(
    stage: StageName,
    *,
    variant: str | None = None,
    contract: str = _CONTRACT_DIR,
    family: str = "sotp",
) -> Path:
    """Path to a stage prompt, optionally a named variant within that stage.

    Some stages need more than one prompt. The fidelity gate is three separate
    model calls -- extract, render, back-translate -- that together implement
    one stage, and splitting them into three stages would misrepresent the
    pipeline's shape in the ledger.

    So a variant becomes ``<stage>.<variant>.md`` and its
    :class:`~abca.schema.ledger.PromptRef` still reports the stage it belongs
    to, with the variant distinguished by ``path``.

    ``family`` selects the prompt tree, and ``contract`` the version WITHIN it.
    Almost everything lives in ``sotp``, which is versioned by the Source-of-Truth
    contract. A module that versions independently gets its own family: the
    sourceless module (docs/18) is ``sourceless/0.1.0`` and moves when its own
    spec moves, not when the contract does. Collapsing the two would force a
    contract bump every time a module prompt changed, and the contract version is
    what every run record pins -- so it would make unrelated runs look
    incomparable.
    """
    name = f"{stage.value}.{variant}.md" if variant else f"{stage.value}.md"
    return PROMPTS_ROOT / family / contract / name


@lru_cache(maxsize=32)
def load_prompt(
    stage: StageName,
    *,
    variant: str | None = None,
    contract: str = _CONTRACT_DIR,
    family: str = "sotp",
) -> Prompt:
    """Load and hash the prompt for ``stage`` (and ``variant``, if given).

    Cached because the classifier prompt is used once per claim -- thousands of
    times on a large thread -- and re-reading plus re-hashing a file that many
    times is pure waste. The cache is keyed on stage and contract, so a
    different contract version loads separately.

    Reads bytes and decodes explicitly as UTF-8 rather than using
    :meth:`Path.read_text` with a platform default, so the hash is identical on
    Windows and Linux. A prompt hash that differed by platform would make every
    cross-platform verification fail for no real reason.
    """
    path = prompt_path(stage, variant=variant, contract=contract, family=family)
    if not path.is_file():
        available = sorted(p.stem for p in path.parent.glob("*.md")) if path.parent.is_dir() else []
        label = f"{stage.value}.{variant}" if variant else stage.value
        raise PromptNotFound(
            f"no prompt for {label!r} under {family!r}/{contract!r} "
            f"(looked in {path.parent}). Available: {', '.join(available) or '<none>'}"
        )

    raw = path.read_bytes()
    return Prompt(
        stage=stage,
        # Stored as a stable repo-relative path so the ledger reads the same
        # whether the tool ran from a checkout, a wheel, or a frozen exe.
        path=f"prompts/{family}/{contract}/{path.name}",
        text=raw.decode("utf-8"),
        content_hash=digest_bytes(raw),
    )


def load_prompts(*stages: StageName, contract: str = _CONTRACT_DIR) -> list[Prompt]:
    """Load several prompts at once. Order is preserved."""
    return [load_prompt(stage, contract=contract) for stage in stages]


def available_stages(*, contract: str = _CONTRACT_DIR) -> list[StageName]:
    """Which stages have a prompt under this contract version."""
    directory = PROMPTS_ROOT / "sotp" / contract
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.md")):
        # "fidelity.extract.md" -> stage "fidelity". A stage with variants
        # appears once per variant; callers that want distinct stages dedupe.
        stem = path.stem.split(".", 1)[0]
        try:
            found.append(StageName(stem))
        except ValueError:
            # A markdown file that is not a stage prompt (a README in the
            # directory, say). Ignored rather than raising: extra documentation
            # next to the prompts is a good thing.
            continue
    return found


# --------------------------------------------------------------------------
# Doctrine check
# --------------------------------------------------------------------------

#: Terms that would indicate a prompt has acquired a political position.
#: Deliberately blunt and deliberately symmetric -- it names both sides of the
#: usual axes, because a checker that only flagged one side would itself be a
#: bias. It cannot detect subtle steering; it catches the obvious regression.
_DOCTRINE_MARKERS: tuple[str, ...] = (
    "democrat", "republican", "liberal", "conservative", "progressive",
    "left-wing", "right-wing", "leftist", "far-right", "far-left",
    "maga", "woke", "socialist", "fascist", "communist",
    "our party", "the party believes", "the party holds", "abca believes",
)

#: Phrases that would tilt the analysis regardless of subject matter.
_STEERING_MARKERS: tuple[str, ...] = (
    "you should conclude", "always find", "never find", "tends to be true",
    "give the benefit of the doubt to", "be skeptical of claims by",
)


def assert_doctrine_free(*, contract: str = _CONTRACT_DIR) -> None:
    """Raise if any shipped prompt contains a doctrine or steering marker.

    Run as a test. This is the mechanical half of the separation the README
    promises; the other half is that no data directory (``corpus/``, ``evals/``,
    ``identity/``) is ever imported by ``src/``. Neither is sufficient alone, and neither replaces a human reading
    the diff.
    """
    problems: list[str] = []
    directory = PROMPTS_ROOT / "sotp" / contract
    for path in sorted(directory.glob("*.md")):
        stem, _, variant = path.stem.partition(".")
        try:
            stage = StageName(stem)
        except ValueError:
            continue
        prompt = load_prompt(stage, variant=variant or None, contract=contract)
        lowered = prompt.text.lower()
        for marker in _DOCTRINE_MARKERS:
            # Word-boundary match so "conservative estimate" in ordinary prose
            # does not trip the alarm.
            if re.search(rf"\b{re.escape(marker)}\b", lowered):
                problems.append(f"{prompt.path}: contains doctrine marker {marker!r}")
        for marker in _STEERING_MARKERS:
            if marker in lowered:
                problems.append(f"{prompt.path}: contains steering phrase {marker!r}")

    if problems:
        raise AssertionError(
            "shipped prompts must contain no editorial doctrine and no steering "
            "instructions (README: 'the Analyzer's prompts contain no political "
            "positions'):\n  - " + "\n  - ".join(problems)
        )


__all__ = [
    "PROMPTS_ROOT",
    "Prompt",
    "PromptNotFound",
    "assert_doctrine_free",
    "available_stages",
    "load_prompt",
    "load_prompts",
    "prompt_path",
]
