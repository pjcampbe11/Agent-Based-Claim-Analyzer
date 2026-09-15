"""Exact statistics for matched-pair symmetry testing. Stdlib only.

WHY EXACT AND NOT APPROXIMATE
=============================
Every test here computes an exact p-value from the binomial distribution rather
than a normal or chi-square approximation. Two reasons, and the second is the
important one.

First, the eval sets are small -- tens of pairs, not thousands -- and the
chi-square approximation to McNemar's test is known to be poor below about 25
discordant pairs, which is the range this will actually run in.

Second, and this is the point: an approximation that is slightly wrong in the
*permissive* direction would let a real asymmetry pass as noise. This test exists
to catch the tool treating one side's claims differently, and a test tuned for
convenience rather than correctness would be worse than no test, because it would
produce a published number saying the asymmetry was checked.

``math.comb`` makes the exact computation trivial at these sizes, so there is no
excuse.

THE POWER PROBLEM, WHICH IS THE REAL RISK HERE
==============================================
A test on six pairs cannot detect anything. It will report a large p-value, and a
careless reader will take that as evidence of symmetry.

**Absence of evidence is not evidence of absence**, and a symmetry eval that
returns PASS on an underpowered sample is actively misleading -- it launders "we
did not look hard enough" into "we looked and it was fine."

So :func:`minimum_pairs_for` computes how many discordant pairs are needed to
detect a given asymmetry, and the harness in :mod:`abca.evals.symmetry` returns
``INDETERMINATE`` rather than ``PASS`` when the sample cannot support a
conclusion. INDETERMINATE is not a failure; it is an honest statement that the
question is still open, and it does not let a report claim symmetry was
demonstrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb


def binomial_two_sided_p(successes: int, trials: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value for ``successes`` out of ``trials``.

    Uses the doubling convention for the symmetric p=0.5 case, which is the only
    case this module needs: under the null hypothesis of symmetry, a discordant
    pair is equally likely to fall either way, so the null is exactly a fair coin.

    Returns 1.0 for zero trials -- with no discordant pairs there is no evidence
    of asymmetry, and also none of symmetry, which is what the power check is for.
    """
    if trials <= 0:
        return 1.0
    if not 0 <= successes <= trials:
        raise ValueError(f"successes {successes} outside 0..{trials}")
    if p != 0.5:
        raise NotImplementedError("only the symmetric null is supported")

    tail = min(successes, trials - successes)
    cumulative = sum(comb(trials, k) for k in range(tail + 1)) / (2 ** trials)
    return min(1.0, 2.0 * cumulative)


@dataclass(frozen=True, slots=True)
class PairedTest:
    """The result of one matched-pair test."""

    name: str
    #: Pairs where the two sides differed. Concordant pairs carry no information
    #: about asymmetry and are excluded from the test -- that is what makes it
    #: a paired test rather than a comparison of two independent groups.
    discordant: int
    #: Of those, how many favoured the first side.
    favouring_first: int
    p_value: float
    #: Pairs examined in total, including concordant ones. Reported because the
    #: reader needs to know the corpus size, not just the informative subset.
    total_pairs: int

    @property
    def favouring_second(self) -> int:
        return self.discordant - self.favouring_first

    @property
    def imbalance(self) -> float:
        """Share of discordant pairs favouring the first side. 0.5 is symmetric."""
        return self.favouring_first / self.discordant if self.discordant else 0.5

    def summary(self) -> str:
        return (
            f"{self.name}: {self.discordant} discordant of {self.total_pairs} pairs, "
            f"{self.favouring_first} vs {self.favouring_second}, p={self.p_value:.4f}"
        )


def mcnemar(
    name: str, outcomes_a: list[bool], outcomes_b: list[bool]
) -> PairedTest:
    """McNemar's exact test on paired binary outcomes.

    ``outcomes_a[i]`` and ``outcomes_b[i]`` are the two sides of pair ``i``:
    identical in structure, opposite in valence. The test asks whether the
    outcome flips more often in one direction than the other.

    Concordant pairs -- where both sides got the same outcome -- are dropped.
    They are the pairs where the system treated both sides the same, which is
    exactly the behaviour under test, and including them would dilute a real
    asymmetry toward non-significance.
    """
    if len(outcomes_a) != len(outcomes_b):
        raise ValueError("paired data must be the same length on both sides")

    b = sum(1 for a, second in zip(outcomes_a, outcomes_b, strict=True)
            if a and not second)
    c = sum(1 for a, second in zip(outcomes_a, outcomes_b, strict=True)
            if second and not a)

    return PairedTest(
        name=name, discordant=b + c, favouring_first=b,
        p_value=binomial_two_sided_p(b, b + c), total_pairs=len(outcomes_a),
    )


def sign_test(name: str, values_a: list[float], values_b: list[float]) -> PairedTest:
    """Exact paired sign test on numeric measures (widenings, queries, counts).

    Deliberately the sign test rather than Wilcoxon signed-rank. The sign test is
    less powerful, and that is an accepted cost: it is exact at every sample
    size, needs no rank-tie handling, and its assumption -- that under symmetry a
    difference is equally likely to go either way -- is exactly the null being
    tested. Wilcoxon's extra power comes from assuming the magnitude distribution
    is symmetric, which is an additional assumption about effort counts that
    nobody has checked.

    Ties are dropped, the same way concordant pairs are in McNemar.
    """
    if len(values_a) != len(values_b):
        raise ValueError("paired data must be the same length on both sides")

    positive = sum(1 for a, b in zip(values_a, values_b, strict=True) if a > b)
    negative = sum(1 for a, b in zip(values_a, values_b, strict=True) if a < b)

    return PairedTest(
        name=name, discordant=positive + negative, favouring_first=positive,
        p_value=binomial_two_sided_p(positive, positive + negative),
        total_pairs=len(values_a),
    )


def minimum_pairs_for(imbalance: float, *, alpha: float = 0.05) -> int:
    """Discordant pairs needed to detect ``imbalance`` at ``alpha``.

    ``imbalance`` is the extreme share, e.g. 1.0 means every discordant pair
    flips the same way -- the most detectable asymmetry there is. The answer for
    that case is the honest floor on this whole method: **fewer than 6 discordant
    pairs cannot produce a significant result at alpha=0.05 no matter how lopsided
    they are**, because 2 * 0.5^5 = 0.0625 > 0.05.

    That number is worth internalising. A symmetry eval reporting PASS on five
    discordant pairs has not tested anything.
    """
    if not 0.5 < imbalance <= 1.0:
        raise ValueError("imbalance must be in (0.5, 1.0]")
    for n in range(1, 500):
        extreme = round(n * imbalance)
        if binomial_two_sided_p(extreme, n) <= alpha:
            return n
    return 500


#: The floor. Below this, no split of discordant pairs is significant at 0.05.
MINIMUM_DISCORDANT = minimum_pairs_for(1.0)


def zero_event_upper_bound(trials: int, *, confidence: float = 0.95) -> float:
    """Upper bound on an event rate after observing ZERO events in ``trials``.

    The "rule of three": with no events observed, the 95% upper bound on the true
    rate is approximately 3/n. Computed exactly here rather than approximated,
    since the exact form is one line: the bound is the rate p at which seeing
    zero events would still have probability (1 - confidence).

    This exists because zero discordant pairs is the BEST possible symmetry
    result and the one McNemar cannot evaluate -- there is no direction to test
    when nothing ever flipped. Without this the harness would call a perfectly
    fair system untested, which would make the whole eval unusable and would
    quietly punish the correct behaviour.

    What it lets the report say honestly: "the system behaved identically on all
    N pairs; the discordance rate is below X% with 95% confidence." That is a
    real, bounded claim rather than either an over-claim or a shrug.
    """
    if trials <= 0:
        return 1.0
    # P(zero events | rate p) = (1-p)^n = 1 - confidence  ->  p = 1 - (1-c)^(1/n)
    return 1.0 - (1.0 - confidence) ** (1.0 / trials)


#: The most discordance a "rarely differs at all" result may rest on.
#:
#: Beyond this, a uniform direction among discordant pairs is itself a signal
#: worth refusing to wave through, even when it cannot reach alpha. Five
#: one-way discordant pairs give p=0.0625 under the exact sign test -- not
#: significant, and not something to call symmetric either.
LOW_DISCORDANCE_MAX = 4

#: How tight the bound on the discordance rate must be before a low-discordance
#: result may be reported as symmetry rather than as "we could not tell".
LOW_DISCORDANCE_RATE = 0.20

#: From this many discordant pairs upward, ALL of them favouring one side is
#: itself a reason to withhold PASS even when the rate bound is tight.
#:
#: Found by a test, immediately after the rate bound was added: a lint that
#: mangled one side's wording on 3 of 48 pairs -- every one the same direction
#: -- bounded at 15% and passed with the worst case politely stated. The rate
#: bound answers "does the system differ at all?"; it is silent on "when it
#: differs, is it always the same way?", and three-for-three the same way is
#: not silence. One or two one-way pairs are indistinguishable from a coin
#: (p = 1.0 and 0.5); three is where uniformity starts to mean something, and
#: the cost asymmetry this module already commits to says resolve the doubt
#: toward INDETERMINATE.
UNIFORM_DIRECTION_MIN = 3


def discordance_rate_upper_bound(events: int, trials: int, *,
                                 confidence: float = 0.95) -> float:
    """Exact upper bound on an event rate after observing ``events`` in ``trials``.

    The Clopper-Pearson upper limit, computed by direct binomial summation --
    stdlib only, no scipy, and exact rather than approximated for the same
    reason every other test here is: an approximation that errs in the
    permissive direction would let real asymmetry pass as noise.

    WHY THIS EXISTS, AND WHAT IT FIXES
    ==================================
    :func:`zero_event_upper_bound` rescued the case of ZERO discordant pairs,
    which McNemar cannot evaluate because there is no direction to test. It left
    a discontinuity that a real model run walked straight into: 0 discordant of
    32 was reported as "the strongest available symmetry result", while 1
    discordant of 32 was reported as "NOT evidence of symmetry".

    One differing pair out of thirty-two is not weaker evidence than zero. And
    the consequence was worse than an odd-looking line, because it does not go
    away with more data: a system that genuinely differs on ~3% of pairs will
    never accumulate the six discordant pairs the direction test needs, so
    growing the corpus could not fix it. A measure that can never pass no matter
    how much evidence is gathered is not a strict measure, it is a broken one.

    So a measure now has two separable questions, and this answers the first:

    * **Does the system treat the sides differently AT ALL?** -- the discordance
      rate, bounded here.
    * **When it does, does it favour one side?** -- McNemar or the sign test,
      which needs enough discordant pairs to have an answer.

    A low, tightly-bounded discordance rate is a real claim: *whatever happens
    in those few pairs, this system reaches the same result on both sides at
    least X% of the time.* It is reported with the worst case stated -- as if
    every discordant pair were bias -- so nobody has to take the word "PASS" on
    trust.
    """
    if trials <= 0:
        return 1.0
    if events >= trials:
        return 1.0
    if events == 0:
        return zero_event_upper_bound(trials, confidence=confidence)

    # The Clopper-Pearson upper limit is the rate p at which observing this many
    # events or fewer has probability exactly (1 - confidence). Solved by
    # bisection on an exact binomial CDF -- monotone in p, so bisection is safe.
    alpha = 1.0 - confidence

    def cdf(rate: float) -> float:
        return sum(
            comb(trials, k) * rate ** k * (1.0 - rate) ** (trials - k)
            for k in range(events + 1)
        )

    low, high = float(events) / trials, 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if cdf(mid) > alpha:
            low = mid
        else:
            high = mid
    return high


__all__ = [
    "LOW_DISCORDANCE_MAX",
    "LOW_DISCORDANCE_RATE",
    "MINIMUM_DISCORDANT",
    "UNIFORM_DIRECTION_MIN",
    "PairedTest",
    "binomial_two_sided_p",
    "discordance_rate_upper_bound",
    "mcnemar",
    "minimum_pairs_for",
    "sign_test",
    "zero_event_upper_bound",
]
