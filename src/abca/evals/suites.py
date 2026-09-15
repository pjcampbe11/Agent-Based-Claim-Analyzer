"""Loading eval pair sets, and the structural checks a pair set must pass.

WHY THE PAIR SET ITSELF IS VALIDATED
====================================
A symmetry test is only as good as its pairs. If the two sides of a pair are not
actually matched -- different structural error, different length, different
specificity -- then any difference in outcome is confounded, and the eval
measures the pair set rather than the system.

So the set is checked before it is used, and a malformed set fails the build
rather than producing a number nobody should trust.

ON PARTY NAMES IN THIS DATA
===========================
The fixture files name parties, because valence IS the variable under test: a
pair that did not differ in who it helps could not detect partisan treatment.

That does not make these files statements of position. They are eval input,
and the same import-lint applies as for every other input -- ``src/``
never reads ``evals/`` outside this loader, and the loader is only ever pointed
at a path a caller supplies.
"""

from __future__ import annotations

import json
from pathlib import Path

from abca.evals.symmetry import ClaimPair

#: Ships with the repo, alongside corpus/. Data, not code.
EVALS_ROOT = Path(__file__).resolve().parents[3] / "evals"

#: Below this, a symmetry suite cannot reach significance even if every pair
#: flips the same way. A set smaller than this is a set that has not been
#: finished, and saying so at load time is cheaper than discovering it in a
#: report that reads INDETERMINATE for reasons nobody can see.
MINIMUM_PAIRS = 6

#: How different two sides of a pair may be in length before the pairing is
#: suspect. A claim twice as long as its counterpart is not the same claim with
#: the valence flipped -- it gives the system more to work with on one side, and
#: that difference alone could move an effort measure.
MAX_LENGTH_RATIO = 1.6


class EvalSetError(RuntimeError):
    """A pair set that cannot support the test it is used for."""


def load_pairs(path: Path) -> tuple[str, list[ClaimPair]]:
    """Load and validate a pair set. Returns ``(suite_name, pairs)``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalSetError(f"no eval set at {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalSetError(f"{path} is not valid JSON: {exc}") from exc

    suite = raw.get("suite", path.stem)
    rows = raw.get("pairs", [])
    pairs = [
        ClaimPair(
            pair_id=row["pair_id"], structure_key=row["structure_key"],
            left_claim=row["left_claim"], right_claim=row["right_claim"],
            expected=row.get("expected", ""),
        )
        for row in rows
    ]
    problems = validate_pairs(pairs)
    if problems:
        raise EvalSetError(
            f"{path} cannot support a symmetry test:\n  - " + "\n  - ".join(problems)
        )
    return suite, pairs


def validate_pairs(pairs: list[ClaimPair]) -> list[str]:
    """Every structural problem with a pair set. Empty means usable."""
    problems: list[str] = []

    if len(pairs) < MINIMUM_PAIRS:
        problems.append(
            f"{len(pairs)} pair(s); at least {MINIMUM_PAIRS} are needed before any "
            "result can be significant at alpha=0.05"
        )

    ids = [p.pair_id for p in pairs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append(f"duplicate pair ids: {duplicates}")

    for pair in pairs:
        left, right = len(pair.left_claim), len(pair.right_claim)
        ratio = max(left, right) / max(1, min(left, right))
        if ratio > MAX_LENGTH_RATIO:
            problems.append(
                f"{pair.pair_id}: sides differ in length by {ratio:.1f}x "
                f"({left} vs {right} chars). A longer claim gives the system more "
                "to work with, which can move an effort measure on its own."
            )

    # The same structure appearing many times is fine and often deliberate, but a
    # set where EVERY pair shares one key tests one failure mode, not the system.
    keys = {p.structure_key for p in pairs}
    if len(pairs) >= MINIMUM_PAIRS and len(keys) == 1:
        problems.append(
            f"all {len(pairs)} pairs share structure key {keys.pop()!r}; the suite "
            "would test one failure mode rather than the system's behaviour"
        )

    return problems


def sourceless_symmetry_path() -> Path:
    return EVALS_ROOT / "sourceless" / "symmetry_pairs.json"


def challenge_symmetry_path() -> Path:
    return EVALS_ROOT / "challenge" / "symmetry_pairs.json"


def levelset_symmetry_path() -> Path:
    """The corpus symmetry gate for the levelset instrument (doc 21 s8).

    Loaded through the same validator as the lane suites even though levelset
    itself must not import anything from ``src/abca`` -- the CHECKER is not the
    tool, and holding both pair sets to one definition of "matched" is the only
    way the two gates mean the same thing.
    """
    return EVALS_ROOT / "levelset" / "symmetry_pairs.json"


__all__ = [
    "EVALS_ROOT",
    "MAX_LENGTH_RATIO",
    "MINIMUM_PAIRS",
    "EvalSetError",
    "challenge_symmetry_path",
    "levelset_symmetry_path",
    "load_pairs",
    "sourceless_symmetry_path",
    "validate_pairs",
]
