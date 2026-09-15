"""The challenge lane and its promotion gate (doc 20).

The lane's job is to find a source for a claim that arrived without one. The risk
is that it finds *something* and calls it the source. So most of this suite
asserts refusals, and the single most important test is
``TestSubstitutionRule`` -- the one that proves a promoted claim is still the
claim that was posted.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from abca.canonical import digest_text
from abca.challenge.executor import (
    MAX_WIDENINGS,
    WIDENING_AXES,
    Budget,
    execute_plan,
    widen,
)
from abca.challenge.gate import check_p1, check_p2, check_p3, check_p4, evaluate
from abca.challenge.lane import (
    PromotionCascade,
    intake,
    lane_a_input,
    run_challenge,
)
from abca.challenge.queue import ChallengeQueue, ChallengeState
from abca.schema.challenge import (
    STRICT_PARTICULARS,
    ChallengeOutcome,
    ConfirmationParticular,
    Particular,
    PromotedFrom,
    PromotionGrade,
    RetrievedArtifact,
    Widening,
)
from abca.schema.enums import SourceTier, Verdict
from abca.schema.sourceless import (
    BranchOutcome,
    ReferentCandidate,
    ReferentConfidence,
    RetrievalPlan,
    SourcelessAnalysis,
)

CLAIM = "Congress voted to end veterans' benefits."
BRANCH = "a roll call on a motion to recommit"


def analysis(claim: str = CLAIM, *, hash_for: str | None = None,
             branches: bool = True, candidates: bool = True) -> SourcelessAnalysis:
    return SourcelessAnalysis(
        denatured_claim=claim,
        denatured_claim_hash=digest_text((hash_for or claim).strip()),
        referent_candidates=(
            (ReferentCandidate(
                candidate="motion to recommit on H.R. 1234",
                referent_confidence=ReferentConfidence.MEDIUM,
                distortion_applied="procedural_to_substantive", reasoning="r"),
             ReferentCandidate(
                candidate="an amendment vote",
                referent_confidence=ReferentConfidence.LOW,
                distortion_applied="amendment_to_passage", reasoning="r"))
            if candidates else ()),
        retrieval_plan=RetrievalPlan(
            queries=("motion to recommit veterans roll call",),
            decisive_artifact="the House roll call record",
            branch_outcomes=((BranchOutcome(if_found=BRANCH,
                                            then_disposition=Verdict.UNSUPPORTED),)
                             if branches else ())),
    )


def artifact(tier: SourceTier = SourceTier.T0) -> RetrievedArtifact:
    return RetrievedArtifact(
        url="https://clerk.house.gov/Votes/2026315", title="Roll Call 315",
        tier=tier, content_hash="sha256:" + "a" * 64,
        retrieved_at=datetime.now(UTC), connector="house", query="q")


def particulars(matched: int) -> tuple[ConfirmationParticular, ...]:
    out = []
    for index, particular in enumerate(Particular):
        if index < matched:
            out.append(ConfirmationParticular(
                particular=particular, matched=True, quote="the passage"))
        else:
            out.append(ConfirmationParticular(
                particular=particular, matched=False, note="did not line up"))
    return tuple(out)


def confirm_all(_artifact, _analysis):
    return particulars(STRICT_PARTICULARS), BRANCH


# ==========================================================================
# THE substitution rule
# ==========================================================================


class TestSubstitutionRule:
    """Doc 20 s4. The claim adjudicated is the claim that was posted."""

    def test_a_rewritten_claim_cannot_promote_even_when_all_else_passes(self):
        substituted = analysis(
            claim="The House rejected a motion to recommit H.R. 1234 on June 19.",
            hash_for=CLAIM,
        )
        result = evaluate(substituted, artifact=artifact(),
                          particulars=particulars(4), branch_matched=BRANCH)
        assert result.grade is PromotionGrade.UNPROMOTABLE
        passed = [c.name for c in result.conditions if c.passed]
        assert len(passed) == 3, "P1-P3 must pass, so P4 is doing the work alone"
        assert "hash MOVED" in result.explain()

    def test_the_error_says_what_would_have_happened(self):
        """A reader must understand the failure, not just see a code."""
        substituted = analysis(claim="something else entirely", hash_for=CLAIM)
        result = check_p4(substituted)
        assert "a sentence nobody posted" in result.reason

    def test_a_missing_hash_is_a_failure_not_a_pass(self):
        """No recorded hash means no way to show the claim was not substituted."""
        no_hash = SourcelessAnalysis(denatured_claim=CLAIM)
        assert not check_p4(no_hash).passed

    def test_the_hash_is_recomputed_not_trusted(self):
        """A hash written beside a substituted claim would agree with itself."""
        honest = analysis()
        assert check_p4(honest).passed
        assert honest.denatured_claim_hash == digest_text(CLAIM)

    def test_lane_a_receives_the_posted_sentence(self):
        queue_free = analysis()
        run = run_challenge(
            _challenge(), queue_free, lambda q: artifact(), confirm_all)
        assert run.enters_lane_a
        assert lane_a_input(run) == CLAIM
        assert "motion to recommit" not in lane_a_input(run)

    def test_lane_a_handoff_rechecks_the_hash(self):
        """Belt and braces: the gate passed, so a mismatch here is post-gate drift."""
        run = run_challenge(_challenge(), analysis(), lambda q: artifact(), confirm_all)
        run.analysis = run.analysis.model_copy(update={"denatured_claim": "rewritten"})
        with pytest.raises(PromotionCascade, match="rewritten after promotion"):
            lane_a_input(run)

    def test_an_unpromoted_run_has_no_lane_a_input(self):
        run = run_challenge(_challenge(), analysis(), lambda q: None, confirm_all)
        with pytest.raises(ValueError, match="does not enter Lane A"):
            lane_a_input(run)


# ==========================================================================
# The four conditions
# ==========================================================================


class TestGateConditions:
    def test_p1_needs_a_document(self):
        assert not check_p1(None).passed
        assert check_p1(artifact()).passed

    @pytest.mark.parametrize("tier", [SourceTier.T3, SourceTier.T4])
    def test_an_inadmissible_artifact_cannot_be_constructed(self, tier):
        """T3/T4 cannot carry a verdict, so promoting onto one is pointless."""
        with pytest.raises(ValueError, match="promotion requires T0-T2"):
            artifact(tier)

    def test_p2_fails_when_nothing_was_pre_registered(self):
        result = check_p2(analysis(branches=False), artifact(), BRANCH)
        assert not result.passed
        assert "rationalise whatever it found" in result.reason

    def test_p2_fails_on_an_unregistered_branch(self):
        result = check_p2(analysis(), artifact(), "something nobody predicted")
        assert not result.passed
        assert "NOT" in result.reason

    def test_p2_is_the_anti_rationalisation_control(self):
        """Finding something unpredicted means the reconstruction was wrong."""
        result = evaluate(analysis(), artifact=artifact(),
                          particulars=particulars(4), branch_matched="unpredicted")
        assert result.grade is PromotionGrade.UNPROMOTABLE

    def test_p3_requires_all_four_particulars_to_be_assessed(self):
        partial = (ConfirmationParticular(
            particular=Particular.ACTOR, matched=True, quote="q"),)
        result = check_p3(partial)
        assert not result.passed
        assert "not assessed at all" in result.reason

    def test_p3_four_of_four_is_strict(self):
        assert evaluate(analysis(), artifact=artifact(), particulars=particulars(4),
                        branch_matched=BRANCH).grade is PromotionGrade.PROMOTED

    def test_p3_three_of_four_is_weak_and_says_so(self):
        result = evaluate(analysis(), artifact=artifact(), particulars=particulars(3),
                          branch_matched=BRANCH)
        assert result.grade is PromotionGrade.PROMOTED_WEAK
        assert "WEAK promotion" in result.explain()

    def test_p3_two_of_four_does_not_promote(self):
        assert evaluate(analysis(), artifact=artifact(), particulars=particulars(2),
                        branch_matched=BRANCH).grade is PromotionGrade.UNPROMOTABLE

    def test_a_topical_match_is_not_a_confirmation(self):
        result = check_p3(particulars(1))
        assert "topical resemblance is not a confirmation" in result.reason

    def test_a_matched_particular_must_quote_the_artifact(self):
        with pytest.raises(ValueError, match="quotes nothing"):
            ConfirmationParticular(particular=Particular.ACTOR, matched=True)

    def test_an_unmatched_particular_must_say_why(self):
        with pytest.raises(ValueError, match="gives no reason"):
            ConfirmationParticular(particular=Particular.ACTOR, matched=False)

    def test_every_condition_is_reported_not_just_the_first(self):
        result = evaluate(analysis(branches=False), artifact=None, particulars=())
        assert len(result.conditions) == 4
        assert len(result.failures) >= 3


# ==========================================================================
# Grades
# ==========================================================================


class TestGrades:
    def test_only_promoted_grades_enter_lane_a(self):
        entering = [g for g in PromotionGrade if g.enters_lane_a]
        assert entering == [PromotionGrade.PROMOTED, PromotionGrade.PROMOTED_WEAK]

    def test_dormant_is_the_only_non_terminal_grade(self):
        assert [g for g in PromotionGrade if not g.is_terminal] == [PromotionGrade.DORMANT]

    def test_dormant_requires_a_plausible_future_artifact(self):
        result = evaluate(analysis(), artifact=None, particulars=(),
                          future_artifact_plausible=True, plan_exhausted=False)
        assert result.grade is PromotionGrade.DORMANT

    def test_an_exhausted_plan_is_unpromotable_not_dormant(self):
        result = evaluate(analysis(), artifact=None, particulars=(),
                          future_artifact_plausible=True, plan_exhausted=True)
        assert result.grade is PromotionGrade.UNPROMOTABLE

    def test_rejection_short_circuits_the_gate(self):
        result = evaluate(analysis(), rejected=True,
                          rejection_reason="motive attribution, unfalfisiable")
        assert result.grade is PromotionGrade.REJECTED
        assert len(result.conditions) == 1

    def test_a_promotion_without_an_artifact_cannot_be_recorded(self):
        with pytest.raises(ValueError, match="requires an artifact"):
            ChallengeOutcome(challenge_id="ch-1", denatured_claim=CLAIM,
                             denatured_claim_hash=digest_text(CLAIM),
                             grade=PromotionGrade.PROMOTED)

    def test_dormant_without_a_recheck_date_cannot_be_recorded(self):
        with pytest.raises(ValueError, match="must carry a re-check date"):
            ChallengeOutcome(challenge_id="ch-1", denatured_claim=CLAIM,
                             denatured_claim_hash=digest_text(CLAIM),
                             grade=PromotionGrade.DORMANT)

    def test_a_terminal_grade_cannot_carry_a_recheck_date(self):
        with pytest.raises(ValueError, match="terminal and must not carry"):
            ChallengeOutcome(challenge_id="ch-1", denatured_claim=CLAIM,
                             denatured_claim_hash=digest_text(CLAIM),
                             grade=PromotionGrade.UNPROMOTABLE,
                             recheck_after=date(2027, 1, 1))


# ==========================================================================
# The dual verdict
# ==========================================================================


class TestDualVerdict:
    def test_the_headline_is_the_characterization(self):
        """The referent verdict is usually SUPPORTED and usually uninteresting."""
        outcome = ChallengeOutcome(
            challenge_id="ch-1", denatured_claim=CLAIM,
            denatured_claim_hash=digest_text(CLAIM),
            grade=PromotionGrade.PROMOTED, artifact=artifact(),
            referent_verdict=Verdict.SUPPORTED,
            characterization_verdict=Verdict.MISLEADING_CONTEXT,
        )
        assert outcome.headline_verdict is Verdict.MISLEADING_CONTEXT

    def test_a_characterization_verdict_requires_a_promotion(self):
        """You cannot show a distortion without the thing that was distorted."""
        with pytest.raises(ValueError, match="requires a promoted claim"):
            ChallengeOutcome(
                challenge_id="ch-1", denatured_claim=CLAIM,
                denatured_claim_hash=digest_text(CLAIM),
                grade=PromotionGrade.UNPROMOTABLE,
                characterization_verdict=Verdict.MISLEADING_CONTEXT,
            )

    def test_the_referent_verdict_stands_alone_when_there_is_no_characterization(self):
        outcome = ChallengeOutcome(
            challenge_id="ch-1", denatured_claim=CLAIM,
            denatured_claim_hash=digest_text(CLAIM),
            grade=PromotionGrade.PROMOTED, artifact=artifact(),
            referent_verdict=Verdict.SUPPORTED)
        assert outcome.headline_verdict is Verdict.SUPPORTED


# ==========================================================================
# Provenance
# ==========================================================================


class TestProvenance:
    def test_the_disclosure_is_fixed_text(self):
        """A model asked to phrase this each time would eventually soften it."""
        p = PromotedFrom(challenge_id="ch-1", denatured_claim_hash=digest_text(CLAIM),
                         promotion_grade=PromotionGrade.PROMOTED)
        assert "cited no source" in p.DISCLOSURE
        assert "located by reconstructing" in p.DISCLOSURE

    def test_a_human_promotion_must_name_its_operator(self):
        with pytest.raises(ValueError, match="must record who performed it"):
            PromotedFrom(challenge_id="ch-1", denatured_claim_hash=digest_text(CLAIM),
                         promotion_grade=PromotionGrade.PROMOTED, human_promoted=True)

    def test_provenance_carries_the_widening_count(self):
        """How hard the system had to look is information about the promotion."""
        run = run_challenge(
            _challenge(), analysis(),
            lambda q: artifact() if "90 days" in q else None, confirm_all)
        assert run.promoted_from is not None
        assert run.promoted_from.widenings
        assert run.promoted_from.queries_executed > 1

    def test_no_promotion_cascade(self):
        queue = ChallengeQueue(Path(_tmp()))
        already = PromotedFrom(challenge_id="ch-earlier",
                               denatured_claim_hash=digest_text(CLAIM),
                               promotion_grade=PromotionGrade.PROMOTED)
        with pytest.raises(PromotionCascade, match="runs Lane A once"):
            intake(queue, CLAIM, promoted_from=already)


# ==========================================================================
# Bounded widening
# ==========================================================================


class TestWidening:
    def test_widening_axes_are_fixed(self):
        """A configurable axis set is an unbounded one with extra steps."""
        assert len(WIDENING_AXES) == 4
        assert {a for a, _ in WIDENING_AXES} == {
            "congress", "chamber", "date_window", "bill_number"}

    def test_every_axis_produces_a_different_query(self):
        base = "motion to recommit"
        widened = {widen(base, axis) for axis, _ in WIDENING_AXES}
        assert len(widened) == len(WIDENING_AXES)
        assert base not in widened

    def test_an_unknown_axis_does_not_widen(self):
        assert widen("q", "nonsense") == "q"

    def test_the_widening_cap_holds(self):
        result = execute_plan([f"q{i}" for i in range(20)], lambda q: None,
                              budget=Budget(max_widenings=3))
        assert len(result.widenings) <= 3
        assert "widening cap" in result.exhausted_reason

    def test_the_default_cap_is_bounded(self):
        assert MAX_WIDENINGS <= 12, "an unbounded search finds something for anything"

    def test_every_widening_is_recorded(self):
        result = execute_plan(["q"], lambda q: None)
        assert len(result.widenings) == len(WIDENING_AXES)
        for w in result.widenings:
            assert isinstance(w, Widening)
            assert w.query and w.axis

    def test_finding_only_after_widening_is_noted(self):
        result = execute_plan(["q"], lambda q: artifact() if "90 days" in q else None)
        assert result.artifact is not None
        assert any("after widening" in n for n in result.notes)

    def test_execution_stops_at_the_first_artifact(self):
        """Choosing among candidates is what branch outcomes exist to prevent."""
        result = execute_plan(["q1", "q2", "q3"], lambda q: artifact())
        assert len(result.queries_executed) == 1

    def test_a_failing_fetch_is_a_failed_query_not_a_crash(self):
        def boom(_q):
            raise RuntimeError("connector exploded")

        result = execute_plan(["q"], boom)
        assert result.artifact is None
        assert any("failed" in n for n in result.notes)

    def test_budget_exhaustion_is_distinguished_from_finding_nothing(self):
        """'We stopped looking' and 'we looked and found nothing' differ."""
        spent = execute_plan(["q"] * 10, lambda q: None, budget=Budget(max_queries=2))
        assert spent.budget_exhausted and not spent.plan_exhausted

        finished = execute_plan(["q"], lambda q: None)
        assert finished.plan_exhausted and not finished.budget_exhausted


# ==========================================================================
# The queue
# ==========================================================================


def _tmp() -> str:
    import tempfile

    return tempfile.mkdtemp()


def _challenge():
    queue = ChallengeQueue(Path(_tmp()))
    challenge, _ = queue.enqueue(CLAIM)
    return challenge


class TestQueue:
    def test_a_viral_narrative_is_one_challenge_not_ten_thousand(self):
        queue = ChallengeQueue(Path(_tmp()))
        for wording in ("Congress gutted benefits!", "congress GUTTED benefits",
                        "Congress voted to gut benefits."):
            queue.enqueue(wording, cluster_key="same-cluster")
        assert len(queue) == 1
        assert queue.stats()["posts_represented"] == 3

    def test_different_clusters_are_different_challenges(self):
        queue = ChallengeQueue(Path(_tmp()))
        queue.enqueue("a", cluster_key="k1")
        queue.enqueue("b", cluster_key="k2")
        assert len(queue) == 2

    def test_an_empty_claim_is_refused(self):
        with pytest.raises(ValueError, match="cannot enqueue an empty claim"):
            ChallengeQueue(Path(_tmp())).enqueue("   ")

    def test_ids_are_prefixed_so_they_cannot_be_read_as_run_ids(self):
        challenge, _ = ChallengeQueue(Path(_tmp())).enqueue(CLAIM)
        assert challenge.challenge_id.startswith("ch-")

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        root = Path(_tmp())
        queue = ChallengeQueue(root)
        queue.enqueue(CLAIM)
        assert list(root.glob("*.tmp")) == []

    def test_claiming_marks_running_and_counts_attempts(self):
        queue = ChallengeQueue(Path(_tmp()))
        queue.enqueue(CLAIM)
        claimed = queue.claim_next()
        assert claimed.state is ChallengeState.RUNNING
        assert claimed.attempts == 1

    def test_a_stranded_running_challenge_can_be_released(self):
        """Without this a killed drain silently stops the queue forever."""
        queue = ChallengeQueue(Path(_tmp()))
        queue.enqueue(CLAIM)
        claimed = queue.claim_next()
        queue.release(claimed)
        assert queue.get(claimed.challenge_id).state is ChallengeState.QUEUED

    def test_a_dormant_challenge_is_not_reclaimed_before_its_date(self):
        queue = ChallengeQueue(Path(_tmp()))
        challenge, _ = queue.enqueue(CLAIM)
        outcome = ChallengeOutcome(
            challenge_id=challenge.challenge_id, denatured_claim=CLAIM,
            denatured_claim_hash=digest_text(CLAIM), grade=PromotionGrade.DORMANT,
            recheck_after=datetime.now(UTC).date() + timedelta(days=30),
            recheck_trigger="pending bill")
        queue.record_outcome(challenge, outcome)
        assert queue.claim_next() is None
        assert queue.by_state(ChallengeState.DORMANT)

    def test_a_due_dormant_challenge_is_reclaimed(self):
        queue = ChallengeQueue(Path(_tmp()))
        challenge, _ = queue.enqueue(CLAIM)
        outcome = ChallengeOutcome(
            challenge_id=challenge.challenge_id, denatured_claim=CLAIM,
            denatured_claim_hash=digest_text(CLAIM), grade=PromotionGrade.DORMANT,
            recheck_after=datetime.now(UTC).date() - timedelta(days=1),
            recheck_trigger="the bill was scheduled")
        queue.record_outcome(challenge, outcome)
        assert queue.claim_next() is not None

    def test_a_resolved_challenge_is_never_reclaimed(self):
        queue = ChallengeQueue(Path(_tmp()))
        challenge, _ = queue.enqueue(CLAIM)
        outcome = ChallengeOutcome(
            challenge_id=challenge.challenge_id, denatured_claim=CLAIM,
            denatured_claim_hash=digest_text(CLAIM),
            grade=PromotionGrade.UNPROMOTABLE)
        queue.record_outcome(challenge, outcome)
        assert queue.claim_next() is None

    def test_a_resolved_cluster_does_not_absorb_new_posts(self):
        """A settled question should be re-asked, not silently merged."""
        queue = ChallengeQueue(Path(_tmp()))
        first, _ = queue.enqueue(CLAIM, cluster_key="k")
        queue.record_outcome(first, ChallengeOutcome(
            challenge_id=first.challenge_id, denatured_claim=CLAIM,
            denatured_claim_hash=digest_text(CLAIM),
            grade=PromotionGrade.UNPROMOTABLE))
        _, created = queue.enqueue(CLAIM, cluster_key="k")
        assert created

    def test_corrupt_files_are_surfaced_not_silently_skipped(self):
        root = Path(_tmp())
        queue = ChallengeQueue(root)
        queue.enqueue(CLAIM)
        (root / "ch-BROKEN.json").write_text("{not json", encoding="utf-8")
        assert len(queue.corrupt()) == 1
        assert len(queue) == 1, "the valid one still loads"

    def test_state_round_trips_through_disk(self):
        root = Path(_tmp())
        challenge, _ = ChallengeQueue(root).enqueue(CLAIM, platform="facebook")
        reloaded = ChallengeQueue(root).get(challenge.challenge_id)
        assert reloaded.platform == "facebook"
        assert reloaded.denatured_claim == CLAIM


# ==========================================================================
# End to end
# ==========================================================================


class TestEndToEnd:
    def test_a_full_promotion(self):
        run = run_challenge(_challenge(), analysis(), lambda q: artifact(), confirm_all)
        assert run.outcome.grade is PromotionGrade.PROMOTED
        assert run.outcome.artifact is not None
        assert run.promoted_from is not None
        assert lane_a_input(run) == CLAIM

    def test_an_unpromotable_run_still_publishes_what_it_searched(self):
        """The unpromotable records matter most (doc 20 s8)."""
        run = run_challenge(_challenge(), analysis(), lambda q: None, confirm_all)
        assert run.outcome.grade is PromotionGrade.UNPROMOTABLE
        assert run.outcome.queries_executed
        assert run.outcome.candidates_considered
        assert run.outcome.reasons

    def test_rejected_claims_do_not_execute_a_search(self):
        calls = []
        run = run_challenge(_challenge(), analysis(),
                            lambda q: calls.append(q), confirm_all,
                            rejected=True, rejection_reason="motive attribution")
        assert run.outcome.grade is PromotionGrade.REJECTED
        assert calls == [], "there was never a document to find"

    def test_a_dormant_run_gets_a_recheck_date(self):
        run = run_challenge(_challenge(), analysis(), lambda q: None, confirm_all,
                            future_artifact_plausible=True,
                            budget=Budget(max_queries=1),
                            recheck_trigger="the bill is pending")
        assert run.outcome.grade is PromotionGrade.DORMANT
        assert run.outcome.recheck_after > datetime.now(UTC).date()
        assert run.outcome.recheck_trigger == "the bill is pending"

    def test_rejected_candidates_are_published(self):
        """A record of what was considered and rejected is falsifiable."""
        run = run_challenge(_challenge(), analysis(), lambda q: artifact(), confirm_all)
        assert run.outcome.candidates_rejected == ("an amendment vote",)

    def test_the_outcome_serializes(self):
        run = run_challenge(_challenge(), analysis(), lambda q: artifact(), confirm_all)
        payload = json.dumps(run.outcome.to_jsonable())
        assert "PROMOTED" in payload

    def test_the_queue_records_the_outcome(self):
        root = Path(_tmp())
        queue = ChallengeQueue(root)
        challenge, _ = queue.enqueue(CLAIM)
        run = run_challenge(challenge, analysis(), lambda q: artifact(), confirm_all)
        queue.record_outcome(challenge, run.outcome)
        reloaded = ChallengeQueue(root).get(challenge.challenge_id)
        assert reloaded.state is ChallengeState.RESOLVED
        assert reloaded.outcome["grade"] == "PROMOTED"
