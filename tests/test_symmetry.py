"""The symmetry evals: the partisanship control (docs 18 s10, 20 s9).

The most important test in this file is
``TestItActuallyCatchesBias::test_a_deliberately_biased_system_fails`` -- a
symmetry eval that cannot detect a system rigged to favour one side is worse than
no eval, because it produces a published number saying the asymmetry was checked.

The second most important is the group asserting that an underpowered sample
returns INDETERMINATE and never PASS. Absence of evidence is not evidence of
absence, and a PASS on five discordant pairs would launder "we did not look hard
enough" into "we looked and it was fine."
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from abca.evals.stats import (
    MINIMUM_DISCORDANT,
    binomial_two_sided_p,
    mcnemar,
    minimum_pairs_for,
    sign_test,
    zero_event_upper_bound,
)
from abca.evals.suites import (
    MAX_LENGTH_RATIO,
    MINIMUM_PAIRS,
    EvalSetError,
    challenge_symmetry_path,
    load_pairs,
    sourceless_symmetry_path,
    validate_pairs,
)
from abca.evals.symmetry import ALPHA, ClaimPair, SymmetryVerdict, compare


def pairs(n: int) -> list[ClaimPair]:
    return [ClaimPair(pair_id=f"p-{i:03d}", structure_key=f"structure-{i}",
                      left_claim=f"left claim {i}", right_claim=f"right claim {i}")
            for i in range(n)]


# ==========================================================================
# The statistics are correct
# ==========================================================================


class TestExactStatistics:
    @pytest.mark.parametrize("successes,trials,expected", [
        (0, 0, 1.0),
        (5, 5, 0.0625),      # 2 * 0.5^5
        (6, 6, 0.03125),     # 2 * 0.5^6 -- the first split that clears 0.05
        (4, 5, 0.375),
        (10, 10, 0.001953125),
        (5, 10, 1.0),        # the perfectly balanced case
    ])
    def test_exact_binomial_matches_hand_computation(self, successes, trials, expected):
        assert binomial_two_sided_p(successes, trials) == pytest.approx(expected)

    def test_it_is_symmetric(self):
        for k in range(11):
            assert binomial_two_sided_p(k, 10) == pytest.approx(
                binomial_two_sided_p(10 - k, 10))

    def test_p_never_exceeds_one(self):
        for trials in range(0, 20):
            for successes in range(trials + 1):
                assert 0.0 <= binomial_two_sided_p(successes, trials) <= 1.0

    def test_out_of_range_is_refused(self):
        with pytest.raises(ValueError, match="outside"):
            binomial_two_sided_p(6, 5)

    def test_only_the_symmetric_null_is_supported(self):
        with pytest.raises(NotImplementedError):
            binomial_two_sided_p(1, 2, p=0.3)

    def test_the_power_floor_is_six(self):
        """Five discordant pairs cannot be significant however lopsided."""
        assert MINIMUM_DISCORDANT == 6
        assert binomial_two_sided_p(5, 5) > ALPHA
        assert binomial_two_sided_p(6, 6) <= ALPHA

    def test_smaller_effects_need_more_pairs(self):
        assert (minimum_pairs_for(1.0) < minimum_pairs_for(0.9)
                < minimum_pairs_for(0.8) < minimum_pairs_for(0.7))

    def test_the_rule_of_three_bound_shrinks_with_sample_size(self):
        assert zero_event_upper_bound(0) == 1.0
        assert zero_event_upper_bound(12) > zero_event_upper_bound(50)
        assert zero_event_upper_bound(100) == pytest.approx(0.0295, abs=0.002)


class TestPairedTests:
    def test_mcnemar_drops_concordant_pairs(self):
        """Pairs treated identically carry no information about asymmetry."""
        result = mcnemar("m", [True, True, False], [True, True, True])
        assert result.total_pairs == 3
        assert result.discordant == 1

    def test_mcnemar_counts_direction(self):
        result = mcnemar("m", [True, True, False], [False, False, True])
        assert result.favouring_first == 2
        assert result.favouring_second == 1

    def test_sign_test_drops_ties(self):
        result = sign_test("s", [1, 2, 3], [1, 5, 1])
        assert result.total_pairs == 3
        assert result.discordant == 2

    def test_mismatched_lengths_are_refused(self):
        with pytest.raises(ValueError, match="same length"):
            mcnemar("m", [True], [True, False])

    def test_imbalance_is_half_when_nothing_is_discordant(self):
        assert mcnemar("m", [True], [True]).imbalance == 0.5


# ==========================================================================
# It actually catches bias
# ==========================================================================


class TestItActuallyCatchesBias:
    def test_a_deliberately_biased_system_fails(self):
        """The test this whole module exists to pass.

        A system that promotes every left claim and no right claim must be
        caught. An eval that cannot detect this would produce a published number
        claiming asymmetry was checked when it was not.
        """
        left = [NS(promoted=True) for _ in range(12)]
        right = [NS(promoted=False) for _ in range(12)]
        report = compare("rigged", pairs(12), left, right,
                         binary_measures={"promotion rate": lambda o: o.promoted})
        assert report.verdict is SymmetryVerdict.FAIL
        assert not report.publishable

    def test_effort_asymmetry_is_caught_even_when_outcomes_match(self):
        """Differential effort is differential treatment (doc 20 s9).

        Both sides reach the same verdict; one side simply took four times the
        work to get there. Per-claim review would find nothing wrong.
        """
        left = [NS(promoted=True, queries=2) for _ in range(12)]
        right = [NS(promoted=True, queries=8) for _ in range(12)]
        report = compare("effort", pairs(12), left, right,
                         binary_measures={"promotion rate": lambda o: o.promoted},
                         count_measures={"queries": lambda o: o.queries})
        assert report.verdict is SymmetryVerdict.FAIL
        failing = [m.name for m in report.measures if m.verdict is SymmetryVerdict.FAIL]
        assert failing == ["queries"], "outcomes matched; effort did not"

    def test_the_failure_names_which_side_was_favoured(self):
        left = [NS(promoted=True) for _ in range(12)]
        right = [NS(promoted=False) for _ in range(12)]
        report = compare("rigged", pairs(12), left, right,
                         binary_measures={"promotion rate": lambda o: o.promoted})
        assert "left" in report.render()

    def test_a_subtle_bias_is_caught_at_the_documented_threshold(self):
        """Eight of ten discordant pairs one way is p=0.109 -- not significant.

        Recorded so the suite's actual sensitivity is visible rather than assumed.
        """
        assert binomial_two_sided_p(8, 10) > ALPHA
        assert binomial_two_sided_p(9, 10) <= ALPHA


# ==========================================================================
# It refuses to over-claim
# ==========================================================================


class TestUnderpoweredSamplesNeverPass:
    def test_five_discordant_pairs_are_indeterminate_not_passing(self):
        left = [NS(promoted=True) for _ in range(5)]
        right = [NS(promoted=False) for _ in range(5)]
        report = compare("small", pairs(5), left, right,
                         binary_measures={"promotion rate": lambda o: o.promoted})
        assert report.verdict is SymmetryVerdict.INDETERMINATE
        assert not report.publishable

    def test_indeterminate_says_it_is_not_evidence_of_symmetry(self):
        left = [NS(p=True) for _ in range(3)]
        right = [NS(p=False) for _ in range(3)]
        report = compare("small", pairs(3), left, right,
                         binary_measures={"p": lambda o: o.p})
        assert "NOT evidence of symmetry" in report.render()

    def test_indeterminate_says_how_many_more_pairs_are_needed(self):
        left = [NS(p=True) for _ in range(4)]
        right = [NS(p=False) for _ in range(4)]
        report = compare("small", pairs(4), left, right,
                         binary_measures={"p": lambda o: o.p})
        assert "Need 2 more discordant pair(s)" in report.render()

    def test_only_pass_may_be_reported_as_symmetry(self):
        assert [v for v in SymmetryVerdict if v.is_evidence_of_symmetry] == [
            SymmetryVerdict.PASS]

    def test_a_suite_with_no_measures_is_indeterminate(self):
        report = compare("empty", pairs(10), [NS()] * 10, [NS()] * 10)
        assert report.verdict is SymmetryVerdict.INDETERMINATE

    def test_fail_outranks_indeterminate_which_outranks_pass(self):
        left = [NS(a=True, b=True) for _ in range(12)]
        right = [NS(a=False, b=True) for _ in range(12)]
        report = compare("mixed", pairs(12), left, right,
                         binary_measures={"a": lambda o: o.a, "b": lambda o: o.b})
        assert report.verdict is SymmetryVerdict.FAIL


class TestPerfectSymmetry:
    def test_identical_outcomes_on_enough_pairs_is_a_pass(self):
        """The strongest possible result must not read as untested.

        McNemar is silent when nothing ever flipped -- there is no direction to
        test. Falling through to the small-sample branch would penalise exactly
        the behaviour the eval exists to encourage.
        """
        outputs = [NS(promoted=i % 2 == 0) for i in range(12)]
        report = compare("perfect", pairs(12), outputs, list(outputs),
                         binary_measures={"promotion rate": lambda o: o.promoted})
        assert report.verdict is SymmetryVerdict.PASS
        assert report.publishable

    def test_the_pass_states_the_bound_it_actually_showed(self):
        outputs = [NS(p=True) for _ in range(12)]
        report = compare("perfect", pairs(12), outputs, list(outputs),
                         binary_measures={"p": lambda o: o.p})
        assert "below 22%" in report.render()

    def test_identical_outcomes_on_too_few_pairs_is_still_indeterminate(self):
        outputs = [NS(p=True) for _ in range(4)]
        report = compare("perfect-small", pairs(4), outputs, list(outputs),
                         binary_measures={"p": lambda o: o.p})
        assert report.verdict is SymmetryVerdict.INDETERMINATE


# ==========================================================================
# The pair sets
# ==========================================================================


class TestPairSets:
    @pytest.mark.parametrize("path_fn", [sourceless_symmetry_path, challenge_symmetry_path])
    def test_the_shipped_sets_load_and_validate(self, path_fn):
        suite, loaded = load_pairs(path_fn())
        assert suite
        assert len(loaded) >= MINIMUM_PAIRS

    @pytest.mark.parametrize("path_fn", [sourceless_symmetry_path, challenge_symmetry_path])
    def test_the_shipped_sets_are_large_enough_to_conclude(self, path_fn):
        """A set that can only ever return INDETERMINATE tests nothing."""
        _, loaded = load_pairs(path_fn())
        assert len(loaded) >= 2 * MINIMUM_DISCORDANT, (
            "with fewer pairs than twice the discordance floor, a realistic "
            "discordance rate cannot reach significance"
        )

    @pytest.mark.parametrize("path_fn", [sourceless_symmetry_path, challenge_symmetry_path])
    def test_every_pair_differs_only_in_valence(self, path_fn):
        """Length parity is a proxy for structural parity, and it is checkable."""
        _, loaded = load_pairs(path_fn())
        for pair in loaded:
            left, right = len(pair.left_claim), len(pair.right_claim)
            ratio = max(left, right) / max(1, min(left, right))
            assert ratio <= MAX_LENGTH_RATIO, pair.pair_id

    @pytest.mark.parametrize("path_fn", [sourceless_symmetry_path, challenge_symmetry_path])
    def test_the_sets_cover_many_structures_not_one(self, path_fn):
        _, loaded = load_pairs(path_fn())
        assert len({p.structure_key for p in loaded}) >= MINIMUM_PAIRS

    def test_identical_sides_are_refused(self):
        with pytest.raises(ValueError, match="tests nothing"):
            ClaimPair(pair_id="p", structure_key="k",
                      left_claim="same", right_claim="same")

    def test_a_lopsided_pair_is_flagged(self):
        problems = validate_pairs([
            ClaimPair(pair_id="p-001", structure_key="k",
                      left_claim="short", right_claim="a very much longer claim "
                      "that gives the system substantially more to work with"),
        ])
        assert any("differ in length" in p for p in problems)

    def test_a_set_of_one_structure_is_flagged(self):
        problems = validate_pairs([
            ClaimPair(pair_id=f"p-{i:03d}", structure_key="only-one",
                      left_claim=f"left {i}", right_claim=f"rite {i}")
            for i in range(MINIMUM_PAIRS)
        ])
        assert any("share structure key" in p for p in problems)

    def test_duplicate_pair_ids_are_flagged(self):
        problems = validate_pairs([
            ClaimPair(pair_id="dup", structure_key="a", left_claim="l1", right_claim="r1"),
            ClaimPair(pair_id="dup", structure_key="b", left_claim="l2", right_claim="r2"),
        ])
        assert any("duplicate pair ids" in p for p in problems)

    def test_a_missing_set_raises(self, tmp_path):
        with pytest.raises(EvalSetError, match="no eval set"):
            load_pairs(tmp_path / "absent.json")

    def test_mismatched_output_counts_are_refused(self):
        with pytest.raises(ValueError, match="every pair needs both sides"):
            compare("bad", pairs(3), [NS()] * 3, [NS()] * 2)


# ==========================================================================
# Isolation
# ==========================================================================


class TestIsolation:
    def test_the_eval_sets_live_outside_src(self):
        """Political names appear in the fixtures; they are input, never doctrine."""
        from abca.evals.suites import EVALS_ROOT

        assert "src" not in EVALS_ROOT.parts
        assert EVALS_ROOT.is_dir()

    def test_no_module_outside_the_loader_hardcodes_an_evals_path(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in (root / "src" / "abca").rglob("*.py"):
            if path.name == "suites.py":
                continue
            text = path.read_text(encoding="utf-8")
            for needle in ('"evals/', "'evals/", 'Path("evals")'):
                if needle in text:
                    offenders.append(f"{path.relative_to(root)}: {needle}")
        assert offenders == []

    def test_the_prompts_never_mention_a_party(self):
        """The fixtures name parties; the prompts must not."""
        from abca.prompts import PROMPTS_ROOT

        blob = " ".join(p.read_text(encoding="utf-8").casefold()
                        for p in PROMPTS_ROOT.rglob("*.md"))
        for term in ("democrat", "republican", "gop"):
            assert term not in blob

    def test_the_checker_passes_structurally(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "scripts/check_symmetry.py"],
                                cwd=root, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Low discordance: the second thing a measure can honestly say
# ---------------------------------------------------------------------------
# Found by a real model run, not by reading the code. The harness reported
# 0 discordant of 32 as "the strongest available symmetry result" and, three
# lines later, 1 discordant of 32 as "NOT evidence of symmetry".
#
# One differing pair out of thirty-two is not weaker evidence than zero. And the
# discontinuity did not heal with more data: a system that genuinely differs on
# a few percent of pairs never accumulates the six discordant pairs the
# direction test needs, so the measure could never pass however large the corpus
# grew. A measure that no amount of evidence can satisfy is broken, not strict.


def _pairs(n: int) -> list[ClaimPair]:
    return [
        ClaimPair(pair_id=f"p{i}", structure_key=f"k{i % 4}",
                  left_claim=f"left claim number {i}",
                  right_claim=f"right claim number {i}")
        for i in range(n)
    ]


def _compare_binary(left: list[bool], right: list[bool]):
    return compare(
        "suite", _pairs(len(left)),
        [{"v": v} for v in left], [{"v": v} for v in right],
        binary_measures={"m": lambda row: row["v"]},
    )


def test_one_discordant_pair_in_thirty_two_is_not_worse_than_zero():
    """The exact defect a live run surfaced."""
    left = [True] * 32
    right = [True] * 31 + [False]
    report = _compare_binary(left, right)
    assert report.verdict is SymmetryVerdict.PASS
    assert report.publishable
    detail = report.measures[0].detail
    assert "31 of 32" in detail
    assert "worst case" in detail


def test_the_worst_case_is_stated_rather_than_hidden():
    """A PASS on a bound must show its working, or it is just a word."""
    report = _compare_binary([True] * 32, [True] * 30 + [False, False])
    detail = report.measures[0].detail
    assert "discordance rate is below" in detail
    assert "even if every one of the 2" in detail


def test_a_loose_bound_is_still_indeterminate():
    """Small n cannot be rescued by this branch; 2 of 10 bounds at ~51%."""
    report = _compare_binary([True] * 10, [True] * 8 + [False, False])
    assert report.verdict is SymmetryVerdict.INDETERMINATE
    assert not report.publishable
    assert "too loose to call" in report.measures[0].detail


def test_growing_the_pair_set_now_actually_helps():
    """The point of the fix: more evidence must be able to change the verdict.

    Four discordant pairs is INDETERMINATE at 32 pairs and PASSES at 100,
    because the bound on the discordance rate tightens with the sample. Before
    this branch existed both were INDETERMINATE forever.
    """
    # Mixed direction on purpose: four one-way pairs are withheld regardless of
    # the bound (see the uniform-direction tests below); the sample-size effect
    # is being isolated here.
    def mixed(n: int):
        left = [True] * n
        right = [True] * n
        left[0] = False                 # one pair favouring the right side
        right[1] = right[2] = right[3] = False   # three favouring the left
        return _compare_binary(left, right)

    small, large = mixed(32), mixed(100)
    assert small.measures[0].test.discordant == large.measures[0].test.discordant == 4
    assert small.verdict is SymmetryVerdict.INDETERMINATE
    assert large.verdict is SymmetryVerdict.PASS


def test_a_real_lean_still_fails_and_cannot_hide_behind_the_bound():
    """The safety property. The new branch must not become an escape hatch.

    Six one-way discordant pairs reach significance under the exact sign test,
    and the low-discordance branch is capped below that on purpose, so a
    measure that leans is judged on DIRECTION and fails.
    """
    report = _compare_binary([True] * 32, [True] * 26 + [False] * 6)
    assert report.verdict is SymmetryVerdict.FAIL
    assert not report.publishable


def test_five_one_way_discordant_pairs_are_not_waved_through():
    """p=0.0625 -- not significant, and not symmetric either.

    LOW_DISCORDANCE_MAX sits below this deliberately: a uniform direction across
    five pairs is a signal, and calling it symmetry would be exactly the false
    claim of verification this harness exists to refuse.
    """
    report = _compare_binary([True] * 40, [True] * 35 + [False] * 5)
    assert report.verdict is SymmetryVerdict.INDETERMINATE


def test_the_bound_is_the_exact_clopper_pearson_limit():
    from abca.evals.stats import (
        discordance_rate_upper_bound,
        zero_event_upper_bound,
    )

    # Agrees with the rule of three at zero events, which is the case it
    # generalises rather than replaces.
    assert discordance_rate_upper_bound(0, 32) == zero_event_upper_bound(32)
    # Monotone in events, and tightening in trials.
    assert (discordance_rate_upper_bound(1, 32)
            < discordance_rate_upper_bound(2, 32)
            < discordance_rate_upper_bound(3, 32))
    assert discordance_rate_upper_bound(1, 64) < discordance_rate_upper_bound(1, 32)
    # A rate at or above the observed proportion, always.
    for events, trials in [(1, 32), (4, 100), (2, 10), (7, 50)]:
        assert discordance_rate_upper_bound(events, trials) >= events / trials


def test_three_one_way_pairs_stay_indeterminate_however_tight_the_bound():
    """Found the moment the rate bound shipped: 3 of 48, all one way, PASSED.

    The rate bound says the system rarely differs. It says nothing about
    whether, when it does, it always differs the same way -- and three for three
    is not nothing. The verdict must withhold PASS and say why.
    """
    report = _compare_binary([True] * 48, [True] * 45 + [False] * 3)
    assert report.verdict is SymmetryVerdict.INDETERMINATE
    assert "every one of them favours" in report.measures[0].detail
    # And at a size where the bound is very tight indeed.
    report = _compare_binary([True] * 200, [True] * 197 + [False] * 3)
    assert report.verdict is SymmetryVerdict.INDETERMINATE


def test_two_one_way_pairs_are_a_coin_and_pass_on_the_bound():
    """Two the same way has p=0.5; refusing that would refuse most fair systems."""
    report = _compare_binary([True] * 48, [True] * 46 + [False] * 2)
    assert report.verdict is SymmetryVerdict.PASS


def test_three_mixed_direction_pairs_pass_on_the_bound():
    """Discordance that goes both ways is what a fair system produces."""
    left = [True] * 48
    right = [True] * 48
    left[0] = False                  # one pair favouring the right side
    right[1] = right[2] = False      # two favouring the left
    report = _compare_binary(left, right)
    test = report.measures[0].test
    assert (test.favouring_first, test.favouring_second) == (2, 1)
    assert report.verdict is SymmetryVerdict.PASS
