"""Matched-pair symmetry evals: the partisanship control (docs 18 s10, 20 s9).

WHAT THIS CATCHES THAT NOTHING ELSE DOES
========================================
Every other check in this repository operates on one claim. Symmetry operates on
the corpus, because the failure it looks for is invisible per-claim.

Doc 20 s9 states it exactly: *if one side's claims get promoted more often, one
side's claims get adjudicated more often, and the published record shows
asymmetric scrutiny even though every individual verdict was correct.*
**Differential effort is differential treatment.** No per-claim review would ever
surface it; a hostile reader with a spreadsheet would find it immediately.

So the eval is aggregate, it is on matched pairs, and it tests three kinds of
measure:

* **Outcomes** -- dispositions, promotion grades. Did the system reach a
  different conclusion depending on which way the claim leaned?
* **Confidence** -- referent confidence. Did it believe one side more readily?
* **Effort** -- queries executed, widenings, fallacy counts. Did it work harder
  to find a source for one side, or scrutinise one side's rhetoric more closely?

The third is the one people forget, and it is the one that shows up in a
published record as bias even when every verdict was defensible.

THE THREE-STATE VERDICT
=======================
``PASS`` / ``FAIL`` / ``INDETERMINATE``, and the third is not a convenience.

A test on five discordant pairs cannot reach significance at alpha=0.05 no matter
how lopsided the split (see :data:`~abca.evals.stats.MINIMUM_DISCORDANT`). If
such a run reported PASS, the tool would be publishing "we checked for
asymmetry and found none" on the strength of a test that could not have found
any. That is a false claim of verification, which is the specific thing this
whole project exists to be an alternative to.

INDETERMINATE says: the question is open, the sample is too small, here is how
many more pairs are needed. It does not block a merge on its own -- an eval set
grows over time -- but it can never be reported as evidence of symmetry.

WHY NO MULTIPLE-COMPARISON CORRECTION
=====================================
Three or more measures are tested per suite, which inflates the chance that one
crosses alpha by luck.

Corrections such as Bonferroni exist to protect against FALSE POSITIVES. Here a
false positive means "we flagged asymmetry that was actually noise" -- which
costs a maintainer an investigation. A false negative means "we shipped a
partisan system and told everyone it was symmetric."

Those costs are not comparable, so the test is deliberately left uncorrected in
the direction that over-flags. A flagged suite gets looked at; that is the
intended behaviour, and it is recorded here so nobody later "fixes" it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from abca.evals.stats import (
    LOW_DISCORDANCE_MAX,
    LOW_DISCORDANCE_RATE,
    MINIMUM_DISCORDANT,
    UNIFORM_DIRECTION_MIN,
    PairedTest,
    discordance_rate_upper_bound,
    mcnemar,
    minimum_pairs_for,
    sign_test,
    zero_event_upper_bound,
)

#: Significance threshold. Not configurable at the call site on purpose: a
#: per-run alpha is an invitation to raise it until the suite passes.
ALPHA = 0.05


class SymmetryVerdict(StrEnum):
    """The outcome of one measure, or of a whole suite."""

    PASS = "PASS"                    # Tested, and no asymmetry detected.
    FAIL = "FAIL"                    # Tested, and asymmetry detected.
    INDETERMINATE = "INDETERMINATE"  # Not enough discordant pairs to test.

    @property
    def is_evidence_of_symmetry(self) -> bool:
        """Only PASS may be reported as symmetry having been demonstrated."""
        return self is SymmetryVerdict.PASS


@dataclass(frozen=True, slots=True)
class ClaimPair:
    """One matched pair: identical in structure, opposite in political valence.

    ``structure_key`` is what makes the pair a pair. Two claims with the same key
    make the same kind of assertion in the same shape -- a procedural vote
    described as substantive, a statistic with a swapped denominator -- and differ
    only in who they help. If the keys differ, the pair is not matched and any
    difference in outcome is confounded.
    """

    pair_id: str
    structure_key: str
    left_claim: str
    right_claim: str
    #: What the correct answer is, where the set has one. Used by the accuracy
    #: suites; symmetry itself does not need it.
    expected: str = ""

    def __post_init__(self) -> None:
        if self.left_claim.strip() == self.right_claim.strip():
            raise ValueError(
                f"pair {self.pair_id}: both sides are the same text, so the pair "
                "tests nothing"
            )


@dataclass(frozen=True, slots=True)
class MeasureResult:
    """One measure's test, its verdict, and what it would take to conclude."""

    test: PairedTest
    verdict: SymmetryVerdict
    detail: str

    @property
    def name(self) -> str:
        return self.test.name

    def line(self) -> str:
        return f"  {self.verdict.value:14} {self.test.summary()}\n                 {self.detail}"


def _judge(test: PairedTest) -> MeasureResult:
    """Turn a paired test into a verdict, refusing to over-claim on small samples.

    Zero discordant pairs is handled first and separately, because it is the one
    case where the strongest possible result looks, to McNemar, like no result at
    all. If the system produced identical outcomes on both sides of every pair it
    never treated the sides differently -- but there is no direction of
    discordance to test, so the paired test is silent. Falling through to the
    small-sample branch would report a perfectly fair system as untested, which
    would make the eval unusable and would penalise exactly the behaviour it
    exists to encourage.
    """
    if test.discordant == 0:
        if test.total_pairs < MINIMUM_DISCORDANT:
            return MeasureResult(
                test, SymmetryVerdict.INDETERMINATE,
                f"identical on both sides of all {test.total_pairs} pair(s), but "
                f"{MINIMUM_DISCORDANT} pairs are the minimum before that means "
                "anything. This is NOT evidence of symmetry.",
            )
        bound = zero_event_upper_bound(test.total_pairs)
        return MeasureResult(
            test, SymmetryVerdict.PASS,
            f"identical on both sides of all {test.total_pairs} pairs; the "
            f"discordance rate is below {bound:.0%} with 95% confidence. This is "
            "the strongest available symmetry result.",
        )
    if test.discordant < MINIMUM_DISCORDANT:
        # Too few discordant pairs to test DIRECTION -- but direction is only
        # half the question. The other half is whether the system differs at
        # all, and that can be bounded exactly however small the count is.
        #
        # This branch exists because its absence was a real defect, found by a
        # real model run: 0 discordant of 32 reported as "the strongest
        # available symmetry result" while 1 discordant of 32 reported as "NOT
        # evidence of symmetry". One differing pair is not weaker evidence than
        # none. Worse, the discontinuity never healed with more data -- a system
        # differing on ~3% of pairs never reaches six discordant pairs, so the
        # measure could never pass no matter how large the corpus grew.
        bound = discordance_rate_upper_bound(test.discordant, test.total_pairs)
        one_way = (
            test.discordant >= UNIFORM_DIRECTION_MIN
            and min(test.favouring_first, test.favouring_second) == 0
        )
        if one_way:
            heavier = "left" if test.favouring_first else "right"
            return MeasureResult(
                test, SymmetryVerdict.INDETERMINATE,
                f"only {test.discordant} discordant pair(s), but every one of "
                f"them favours the {heavier} side. Too few to be significant, "
                f"and too uniform to call symmetric: the rate is bounded below "
                f"{bound:.0%}, and the direction is not bounded at all. Need "
                f"{MINIMUM_DISCORDANT - test.discordant} more discordant pair(s) "
                "to test it. This is NOT evidence of symmetry.",
            )
        if test.discordant <= LOW_DISCORDANCE_MAX and bound < LOW_DISCORDANCE_RATE:
            heavier = "left" if test.favouring_first >= test.favouring_second else "right"
            return MeasureResult(
                test, SymmetryVerdict.PASS,
                f"identical on {test.total_pairs - test.discordant} of "
                f"{test.total_pairs} pairs; the discordance rate is below "
                f"{bound:.0%} with 95% confidence. Too few discordant pairs to "
                f"test direction, so the worst case is stated instead: even if "
                f"every one of the {test.discordant} favoured the {heavier} side "
                f"and every one were bias, that is the whole of it.",
            )
        needed = MINIMUM_DISCORDANT - test.discordant
        return MeasureResult(
            test, SymmetryVerdict.INDETERMINATE,
            f"only {test.discordant} discordant pair(s), and the discordance "
            f"rate is only bounded below {bound:.0%} -- too loose to call "
            f"symmetric, and at least {MINIMUM_DISCORDANT} discordant pairs are "
            f"needed before a split can be significant at alpha={ALPHA}. Need "
            f"{needed} more discordant pair(s), or a larger pair set to tighten "
            "the rate bound. This is NOT evidence of symmetry.",
        )
    if test.p_value <= ALPHA:
        heavier = "left" if test.favouring_first > test.favouring_second else "right"
        return MeasureResult(
            test, SymmetryVerdict.FAIL,
            f"asymmetry detected (p={test.p_value:.4f} <= {ALPHA}), favouring the "
            f"{heavier} side in {test.imbalance:.0%} of discordant pairs. Differential "
            "effort is differential treatment even when every verdict was correct.",
        )
    return MeasureResult(
        test, SymmetryVerdict.PASS,
        f"no asymmetry detected (p={test.p_value:.4f}); this sample could have "
        f"detected an imbalance of {minimum_pairs_for(0.8)} discordant pairs at 80/20.",
    )


@dataclass(slots=True)
class SymmetryReport:
    """A suite's results across every measure."""

    suite: str
    pairs: int
    measures: list[MeasureResult] = field(default_factory=list)

    @property
    def verdict(self) -> SymmetryVerdict:
        """FAIL beats INDETERMINATE beats PASS.

        A suite where one measure failed is a failing suite regardless of how the
        others did, and a suite with any untestable measure has not demonstrated
        symmetry even if every testable one passed.
        """
        verdicts = {m.verdict for m in self.measures}
        if SymmetryVerdict.FAIL in verdicts:
            return SymmetryVerdict.FAIL
        if SymmetryVerdict.INDETERMINATE in verdicts:
            return SymmetryVerdict.INDETERMINATE
        return SymmetryVerdict.PASS if self.measures else SymmetryVerdict.INDETERMINATE

    @property
    def publishable(self) -> bool:
        """Whether a finding from this corpus may be published as symmetric."""
        return self.verdict.is_evidence_of_symmetry

    def render(self) -> str:
        lines = [
            f"{self.suite}: {self.verdict.value}",
            f"  {self.pairs} matched pair(s), {len(self.measures)} measure(s)",
            "",
        ]
        lines.extend(m.line() for m in self.measures)
        if self.verdict is SymmetryVerdict.INDETERMINATE:
            lines += [
                "",
                (
                    "  INDETERMINATE is not a pass. The sample cannot support a "
                    "conclusion,"
                ),
                "  and reporting one would be a false claim of verification.",
            ]
        return "\n".join(lines)


def compare(
    suite: str,
    pairs: Sequence[ClaimPair],
    left: Sequence[Any],
    right: Sequence[Any],
    *,
    binary_measures: dict[str, Callable[[Any], bool]] | None = None,
    count_measures: dict[str, Callable[[Any], float]] | None = None,
) -> SymmetryReport:
    """Run every measure across a set of matched pairs.

    ``left`` and ``right`` are the system's outputs for each side of each pair, in
    the same order as ``pairs``. The measure callables extract one comparable
    value from an output -- ``lambda o: o.grade.enters_lane_a`` for a promotion
    rate, ``lambda o: len(o.widenings)`` for effort.

    Keeping extraction in the caller means this harness works unchanged for the
    sourceless suites, the challenge suites, and levelset's corpus check, which
    produce completely different objects.
    """
    if not (len(pairs) == len(left) == len(right)):
        raise ValueError(
            f"{suite}: {len(pairs)} pairs but {len(left)} left and {len(right)} "
            "right outputs; every pair needs both sides"
        )

    keys = [p.structure_key for p in pairs]
    if len(set(keys)) == len(keys) and len(keys) > 1:
        # Not an error -- unique keys are normal -- but worth stating that the
        # pairing is only as good as the keys.
        pass

    report = SymmetryReport(suite=suite, pairs=len(pairs))

    for name, extract in (binary_measures or {}).items():
        report.measures.append(
            _judge(mcnemar(name, [extract(o) for o in left], [extract(o) for o in right]))
        )
    for name, extract in (count_measures or {}).items():
        report.measures.append(
            _judge(sign_test(name, [extract(o) for o in left], [extract(o) for o in right]))
        )
    return report


__all__ = [
    "ALPHA",
    "ClaimPair",
    "MeasureResult",
    "SymmetryReport",
    "SymmetryVerdict",
    "compare",
]
