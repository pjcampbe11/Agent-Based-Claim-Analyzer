"""Run the matched-pair symmetry evals (docs 18 s10, 20 s9).

WHAT THIS IS FOR
================
Every other check in this repository looks at one claim. This one looks at the
corpus, because the failure it hunts is invisible per-claim: the tool treating
one side's claims differently while every individual verdict remains defensible.

Doc 20 s9 states the stakes exactly -- *if one side's claims get promoted more
often, one side's claims get adjudicated more often, and the published record
shows asymmetric scrutiny even though every individual verdict was correct.*

    python scripts/check_symmetry.py              # structural checks on the sets
    python scripts/check_symmetry.py --run        # + execute the lane on both sides

The structural pass runs in CI on every commit: it verifies the pair sets are
actually matched, large enough, and varied enough to test anything. The --run
pass executes the challenge lane on both sides of every pair with a deterministic
stub, which measures the CODE's symmetry -- the parts of the system that are not
a model. Model symmetry needs a configured backend and is a separate run.

EXIT CODES
==========
    0  every suite PASSED
    1  a suite FAILED -- asymmetry was detected
    2  a suite is INDETERMINATE -- not a failure, but not evidence of symmetry
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from abca.canonical import digest_text
from abca.evals.stats import MINIMUM_DISCORDANT
from abca.evals.suites import (
    EvalSetError,
    challenge_symmetry_path,
    load_pairs,
    sourceless_symmetry_path,
)
from abca.evals.symmetry import SymmetryVerdict, compare

EXIT_ASYMMETRY = 1
EXIT_INDETERMINATE = 2


def _run_lane(claim: str):
    """Run the real challenge lane on one claim with a deterministic stub fetcher.

    The fetcher's behaviour depends ONLY on the claim's hash, never on its
    wording. That is the point: any asymmetry this run detects is asymmetry in
    the pipeline's own code paths, not in a stub that was written to favour one
    side. A stub that keyed off the text would be testing the stub.
    """
    from datetime import UTC, datetime

    from abca.challenge.executor import Budget
    from abca.challenge.lane import run_challenge
    from abca.challenge.queue import Challenge
    from abca.schema.challenge import (
        ConfirmationParticular,
        Particular,
        RetrievedArtifact,
    )
    from abca.schema.enums import SourceTier, Verdict
    from abca.schema.sourceless import (
        BranchOutcome,
        ReferentCandidate,
        ReferentConfidence,
        RetrievalPlan,
        SourcelessAnalysis,
    )

    claim_hash = digest_text(claim.strip())
    # Deterministic pseudo-behaviour from the hash: found-or-not and how many
    # particulars confirm. Identical inputs give identical results, and the
    # hash of a left claim carries no information about its valence.
    seed = int(claim_hash[7:15], 16)
    branch = "the document the reconstruction predicted"

    analysis = SourcelessAnalysis(
        denatured_claim=claim,
        denatured_claim_hash=claim_hash,
        referent_candidates=(ReferentCandidate(
            candidate="the predicted referent", referent_confidence=ReferentConfidence.MEDIUM,
            distortion_applied="procedural_to_substantive", reasoning="structural fit"),),
        retrieval_plan=RetrievalPlan(
            queries=("primary query", "fallback query"),
            decisive_artifact="the record",
            branch_outcomes=(BranchOutcome(if_found=branch,
                                           then_disposition=Verdict.UNSUPPORTED),)),
    )

    def fetch(_query):
        if seed % 3 == 0:
            return None
        return RetrievedArtifact(
            url="https://example.gov/doc", title="The record", tier=SourceTier.T0,
            content_hash="sha256:" + f"{seed:064x}"[:64],
            retrieved_at=datetime.now(UTC), connector="stub", query=_query)

    def confirm(_artifact, _analysis):
        matched = 4 if seed % 5 else 3
        out = []
        for index, particular in enumerate(Particular):
            if index < matched:
                out.append(ConfirmationParticular(
                    particular=particular, matched=True, quote="the passage"))
            else:
                out.append(ConfirmationParticular(
                    particular=particular, matched=False, note="not established"))
        return tuple(out), branch

    challenge = Challenge(
        challenge_id="ch-eval", denatured_claim=claim,
        denatured_claim_hash=claim_hash, cluster_key=claim_hash)
    return run_challenge(challenge, analysis, fetch, confirm, budget=Budget())


def structural_report(path: Path) -> tuple[str, int, list[str]]:
    try:
        suite, pairs = load_pairs(path)
    except EvalSetError as error:
        return path.stem, 0, [str(error)]
    return suite, len(pairs), []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="execute the challenge lane on both sides of every pair")
    args = parser.parse_args()

    paths = [sourceless_symmetry_path(), challenge_symmetry_path()]
    problems: list[str] = []

    print("pair sets\n")
    for path in paths:
        suite, count, errors = structural_report(path)
        if errors:
            problems.extend(errors)
            print(f"  FAIL  {suite}")
            for error in errors:
                print(f"        {error}")
        else:
            print(f"  OK    {suite:24} {count} matched pair(s)")

    if problems:
        print(f"\n{len(problems)} problem(s) with the pair sets. A symmetry test is "
              "only as good as its pairs.")
        return EXIT_ASYMMETRY

    if not args.run:
        print(f"\nStructural checks passed. Run with --run to execute the lane.\n"
              f"(Reminder: fewer than {MINIMUM_DISCORDANT} discordant pairs can "
              "never be significant at alpha=0.05.)")
        return 0

    print("\nexecuting the challenge lane on both sides of every pair\n")
    verdicts: list[SymmetryVerdict] = []

    suite, pairs = load_pairs(challenge_symmetry_path())
    left = [_run_lane(p.left_claim) for p in pairs]
    right = [_run_lane(p.right_claim) for p in pairs]

    report = compare(
        suite, pairs, left, right,
        binary_measures={
            "promotion rate": lambda r: r.outcome.grade.enters_lane_a,
            "artifact found": lambda r: r.outcome.artifact is not None,
        },
        count_measures={
            "queries executed": lambda r: len(r.outcome.queries_executed),
            "widenings": lambda r: len(r.outcome.widenings),
            "particulars matched": lambda r: sum(
                1 for p in r.outcome.particulars if p.matched),
        },
    )
    print(report.render())
    verdicts.append(report.verdict)

    print()
    if SymmetryVerdict.FAIL in verdicts:
        print("ASYMMETRY DETECTED. Differential effort is differential treatment.")
        return EXIT_ASYMMETRY
    if SymmetryVerdict.INDETERMINATE in verdicts:
        print("INDETERMINATE. Not a failure, and NOT evidence of symmetry: the "
              "sample cannot support a conclusion. Grow the pair set.")
        return EXIT_INDETERMINATE
    print("Every suite passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
