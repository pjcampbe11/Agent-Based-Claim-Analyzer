"""The evidence floor, reference tiering, and the governance line.

The tests that matter most here are the REFUSALS. A capability that produces
policy analysis is only trustworthy to the extent it declines to produce it
from nothing, so most of what follows asserts that something did not happen.
"""

from __future__ import annotations

import pytest

from abca.issue.references import (
    HARD_MINIMUM_PRIMARY,
    HARD_MINIMUM_TOTAL,
    EvidenceFloor,
    ReferenceSpecError,
    check_floor,
    parse_reference_spec,
    resolve_references,
    tier_summary,
)
from abca.pipeline.frame import frame_issue
from abca.pipeline.solve import (
    RANKING_DISCLOSURE,
    WEIGHT_COST,
    WEIGHT_EVIDENCE,
    WEIGHT_REVERSIBILITY,
    WEIGHT_TIME,
    rank_interventions,
    tied_at_top,
)
from abca.schema.enums import ClaimType, SourceTier, Verdict
from abca.schema.issue import (
    CLAIM_GOVERNANCE_FLOOR,
    CostBand,
    GovernanceTier,
    ReferenceKind,
    Reversibility,
    TimeToEffect,
    escalate,
    governance_floor,
)
from abca.schema.plan import Intervention, PlanStep, Reference


def ref(spec: str, index: int = 1) -> Reference:
    return parse_reference_spec(spec, index=index)


# ==========================================================================
# Parsing
# ==========================================================================


class TestReferenceSpecs:
    def test_kind_and_locator(self):
        r = ref("primary:28 CFR 545.11")
        assert r.declared_kind is ReferenceKind.PRIMARY
        assert r.locator == "28 CFR 545.11"
        assert r.quote is None

    def test_quote_after_the_separator(self):
        r = ref("primary:28 CFR 545.11||Court-ordered restitution")
        assert r.quote == "Court-ordered restitution"

    def test_a_url_with_a_single_pipe_is_not_split(self):
        """The separator is `||` precisely so query strings survive."""
        r = ref("reporting:https://example.com/a?x=1|2&y=3")
        assert r.locator == "https://example.com/a?x=1|2&y=3"

    def test_missing_kind_is_refused_with_the_fix(self):
        with pytest.raises(ReferenceSpecError, match="has no kind"):
            ref("28 CFR 545.11")

    def test_unknown_kind_lists_the_valid_ones(self):
        with pytest.raises(ReferenceSpecError, match="is not a reference kind"):
            ref("gospel:something")

    def test_kind_without_locator_is_refused(self):
        with pytest.raises(ReferenceSpecError, match="no locator"):
            ref("primary:")

    def test_ids_are_stable_and_zero_padded(self):
        assert ref("primary:x", index=7).id == "r-007"

    def test_title_never_editorializes(self):
        """A title is derived mechanically; it must not describe the content."""
        assert ref("reporting:https://www.example.com/news/story").title == "example.com/story"


# ==========================================================================
# Tier ceilings
# ==========================================================================


class TestTierCeiling:
    def test_a_declared_kind_cannot_promote_a_source(self):
        """Typing `primary:` in front of a blog does not make it primary law."""
        blog = ref("primary:https://someblog.example/post")
        resolved = resolve_references([blog], fetch=lambda _: None)[0]
        assert resolved.effective_tier is SourceTier.T4
        assert resolved.is_evidence is False

    def test_a_declared_kind_can_demote(self):
        """Labelling modestly is honoured, so honesty is never punished."""

        class Doc:
            tier = SourceTier.T0
            content_hash = "sha256:" + "0" * 64
            title = "A regulation"
            retrieved_at = None

            def contains(self, _):
                return True

        modest = ref("reporting:28 CFR 545.11")
        resolved = resolve_references([modest], fetch=lambda _: Doc())[0]
        assert resolved.effective_tier is SourceTier.T3, "the T3 ceiling must hold"

    def test_social_is_never_evidence_at_any_tier(self):
        for tier in SourceTier:
            assert not ReferenceKind.SOCIAL.is_evidence
            assert ReferenceKind.SOCIAL.tier_ceiling is SourceTier.T4
            assert tier is tier  # every tier, same answer

    def test_an_unverifiable_quote_is_dropped_not_kept(self):
        """A quote a source does not contain would borrow its authority."""

        class Doc:
            tier = SourceTier.T0
            content_hash = "sha256:" + "1" * 64
            title = "A regulation"
            retrieved_at = None

            def contains(self, _):
                return False

        r = ref("primary:28 CFR 545.11||words that are not in the regulation")
        resolved = resolve_references([r], fetch=lambda _: Doc())[0]
        assert resolved.quote is None
        assert "does not appear verbatim" in resolved.unfetchable_reason

    def test_an_unfetchable_reference_is_kept_with_its_reason(self):
        """Dropping it would make a thin record look thorough."""
        resolved = resolve_references([ref("official:https://x.example")], fetch=lambda _: None)[0]
        assert resolved.unfetchable_reason
        assert resolved.effective_tier is SourceTier.T4


# ==========================================================================
# The floor
# ==========================================================================


class TestEvidenceFloor:
    def test_the_floor_cannot_be_configured_to_zero(self):
        with pytest.raises(ValueError, match="configurable upward only"):
            EvidenceFloor(minimum_total=0)

    def test_the_primary_requirement_cannot_be_configured_away(self):
        with pytest.raises(ValueError, match="at least"):
            EvidenceFloor(minimum_primary=0)

    def test_the_hard_minimums_are_not_zero(self):
        """Guards the guard: a future edit to 0 defeats the whole capability."""
        assert HARD_MINIMUM_TOTAL >= 2
        assert HARD_MINIMUM_PRIMARY >= 1

    def test_no_references_fails_and_says_what_to_add(self):
        result = check_floor([], EvidenceFloor())
        assert not result.met
        assert any("--ref KIND:LOCATOR" in s for s in result.shortfalls)
        assert any("primary" in s for s in result.shortfalls)

    def test_all_social_fails_even_when_numerous(self):
        refs = [ref(f"social:https://e.example/{i}", i) for i in range(1, 9)]
        result = check_floor(refs, EvidenceFloor())
        assert not result.met
        assert any("social posts" in s for s in result.shortfalls)

    def test_a_resolved_primary_set_passes(self):
        class Doc:
            tier = SourceTier.T0
            content_hash = "sha256:" + "2" * 64
            title = "A regulation"
            retrieved_at = None

            def contains(self, _):
                return True

        refs = resolve_references(
            [ref("primary:a", 1), ref("primary:b", 2), ref("social:https://e/1", 3)],
            fetch=lambda loc: None if loc.startswith("http") else Doc(),
        )
        assert check_floor(refs, EvidenceFloor()).met

    def test_strict_floor_is_stricter_in_every_dimension(self):
        default, strict = EvidenceFloor(), EvidenceFloor.strict()
        assert strict.minimum_total > default.minimum_total
        assert strict.minimum_primary > default.minimum_primary
        assert strict.maximum_social_share < default.maximum_social_share

    def test_the_refusal_message_says_it_is_a_refusal(self):
        report = check_floor([], EvidenceFloor()).report()
        assert "refusal, not an error" in report

    def test_tier_summary_counts_every_tier(self):
        counts = tier_summary([ref("social:https://e/1")])
        assert set(counts) == {t.value for t in SourceTier}
        assert counts["T4"] == 1


# ==========================================================================
# Governance
# ==========================================================================


class TestGovernance:
    def test_a_step_resting_on_nothing_is_mechanical(self):
        assert governance_floor([]) is GovernanceTier.MECHANICAL

    def test_checkable_claims_make_a_step_verifiable(self):
        assert governance_floor([ClaimType.LEGAL, ClaimType.EMPIRICAL]) is (
            GovernanceTier.VERIFIABLE
        )

    @pytest.mark.parametrize(
        "claim_type", [ClaimType.NORMATIVE, ClaimType.DEFINITIONAL, ClaimType.PREDICTIVE]
    )
    def test_one_value_claim_makes_the_whole_step_value(self, claim_type):
        assert governance_floor([ClaimType.LEGAL, claim_type]) is GovernanceTier.VALUE

    def test_the_ratchet_only_tightens(self):
        for lower in GovernanceTier:
            for higher in GovernanceTier:
                result = escalate(lower, higher)
                assert result in (lower, higher)
        assert escalate(GovernanceTier.VALUE, GovernanceTier.MECHANICAL) is (
            GovernanceTier.VALUE
        )

    def test_only_mechanical_may_automate_a_decision(self):
        automatable = [t for t in GovernanceTier if t.may_automate_decision]
        assert automatable == [GovernanceTier.MECHANICAL]

    def test_every_claim_type_has_a_floor(self):
        """A new claim type must be assigned deliberately, not defaulted."""
        assert set(CLAIM_GOVERNANCE_FLOOR) == set(ClaimType)

    def test_no_verdict_eligible_type_maps_to_value_and_vice_versa(self):
        for claim_type, tier in CLAIM_GOVERNANCE_FLOOR.items():
            if claim_type.is_verdict_eligible:
                assert tier is GovernanceTier.VERIFIABLE
            else:
                assert tier is GovernanceTier.VALUE

    def test_a_plan_step_cannot_be_serialized_with_a_forged_tier(self):
        """The invariant this module exists to protect."""
        with pytest.raises(ValueError, match="is never declared"):
            PlanStep(
                id="s-001",
                description="quietly automate a value call",
                rests_on=(ClaimType.NORMATIVE,),
                governance=GovernanceTier.MECHANICAL,
                decider="nobody",
            )

    def test_a_value_step_must_name_its_decider(self):
        with pytest.raises(ValueError, match="accountable decider"):
            PlanStep.with_governance(
                id="s-001", description="decide", rests_on=(ClaimType.NORMATIVE,)
            )

    def test_the_default_tier_fails_safe_toward_a_human(self):
        """A step that skipped derivation must not default to automatable."""
        assert PlanStep.model_fields["governance"].default is GovernanceTier.VALUE

    def test_value_and_verifiable_both_require_a_published_check(self):
        assert GovernanceTier.VALUE.requires_published_check
        assert GovernanceTier.VERIFIABLE.requires_published_check
        assert not GovernanceTier.MECHANICAL.requires_published_check


# ==========================================================================
# Framing
# ==========================================================================


def claim(cid, claim_type, verdict, **kw):
    from abca.schema.core import Claim

    base = dict(
        id=cid, text="A claim about something.", span_start=0, span_end=24,
        claim_type=claim_type, verdict=verdict, confidence=0.0, reasoning="because",
    )
    base.update(kw)
    return Claim(**base)


class TestFrame:
    def test_the_three_buckets_are_total_and_disjoint(self):
        claims = [
            claim("c-001", ClaimType.LEGAL, Verdict.UNSUPPORTED),
            claim("c-002", ClaimType.NORMATIVE, Verdict.UNVERIFIABLE),
            claim("c-003", ClaimType.EMPIRICAL, Verdict.UNSUPPORTED),
            claim("c-004", ClaimType.LEGAL, Verdict.OUT_OF_SCOPE),
        ]
        frame = frame_issue(claims)
        total = (
            len(frame.findings) + len(frame.values)
            + len(frame.open_questions) + len(frame.out_of_scope)
        )
        assert total == len(claims)

    def test_a_normative_claim_is_a_value_whatever_verdict_it_carries(self):
        """Defence in depth: the class decides, not the verdict."""
        frame = frame_issue([claim("c-001", ClaimType.NORMATIVE, Verdict.UNVERIFIABLE)])
        assert len(frame.values) == 1
        assert not frame.findings

    def test_reported_unverified_is_open_not_a_finding(self):
        """A thing being reported is a fact about the reporting.

        The contract gate requires a T3 citation for this verdict, so the
        fixture carries a real one -- the point under test is the bucket the
        claim lands in, not whether the gate works.
        """
        from datetime import UTC, datetime

        from abca.schema.core import Citation

        reporting = Citation(
            tier=SourceTier.T3, title="A newspaper", url="https://example.com/story",
            quote="a sentence from the story", retrieved_at=datetime.now(UTC),
            content_hash="sha256:" + "3" * 64,
        )
        frame = frame_issue([
            claim("c-001", ClaimType.EMPIRICAL, Verdict.REPORTED_UNVERIFIED,
                  citations=[reporting], evidence_quality=SourceTier.T3)
        ])
        assert not frame.findings
        assert len(frame.open_questions) == 1

    def test_checkable_share_excludes_out_of_scope(self):
        frame = frame_issue([
            claim("c-001", ClaimType.NORMATIVE, Verdict.UNVERIFIABLE),
            claim("c-002", ClaimType.LEGAL, Verdict.OUT_OF_SCOPE),
        ])
        assert frame.checkable_share == 0.0
        assert frame.summary()["out_of_scope"] == 1


# ==========================================================================
# Ranking
# ==========================================================================


def intervention(iid, cost, time, rev, steps=()):
    return Intervention(
        id=iid, title=f"Option {iid}", mechanism="m", authority="a",
        cost_band=cost, time_to_effect=time, reversibility=rev,
        falsifier="the measured harm does not fall", steps=steps,
    )


class TestRanking:
    def test_weights_sum_to_one(self):
        total = WEIGHT_REVERSIBILITY + WEIGHT_TIME + WEIGHT_COST + WEIGHT_EVIDENCE
        assert abs(total - 1.0) < 1e-9

    def test_reversibility_is_the_heaviest_weight(self):
        """The error-correction promise is inoperable without it."""
        assert max(WEIGHT_TIME, WEIGHT_COST, WEIGHT_EVIDENCE) < WEIGHT_REVERSIBILITY

    def test_cheap_fast_reversible_outranks_the_opposite(self):
        best = intervention("i-001", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL)
        worst = intervention(
            "i-002", CostBand.OVER_10B, TimeToEffect.OVER_TWO_YEARS, Reversibility.IRREVERSIBLE
        )
        assert rank_interventions([worst, best])[0].intervention.id == "i-001"

    def test_ranking_is_deterministic_under_reordering(self):
        options = [
            intervention("i-001", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL),
            intervention("i-002", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL),
            intervention("i-003", CostBand.B1_TO_10B, TimeToEffect.MONTHS, Reversibility.HARD),
        ]
        forward = [r.intervention.id for r in rank_interventions(options)]
        backward = [r.intervention.id for r in rank_interventions(list(reversed(options)))]
        assert forward == backward

    def test_ties_are_reported_rather_than_broken_by_preference(self):
        options = [
            intervention("i-001", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL),
            intervention("i-002", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL),
        ]
        assert len(tied_at_top(rank_interventions(options))) == 2

    def test_an_uncited_option_scores_zero_evidence_but_is_still_listed(self):
        ranked = rank_interventions([intervention("i-001", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL)])
        assert ranked[0].components["evidence"] == 0.0
        assert len(ranked) == 1, "excluding it would hide it"

    def test_the_score_is_reproducible_by_hand(self):
        ranked = rank_interventions(
            [intervention("i-001", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL)]
        )[0]
        assert ranked.score == pytest.approx(
            1.0 * WEIGHT_REVERSIBILITY + 1.0 * WEIGHT_TIME + 1.0 * WEIGHT_COST + 0.0
        )
        assert "reversibility=" in ranked.explain()

    def test_the_disclosure_names_the_weights_as_a_value_judgment(self):
        assert "value judgment" in RANKING_DISCLOSURE
        assert "not a finding" in RANKING_DISCLOSURE

    def test_a_falsifier_is_required(self):
        with pytest.raises(ValueError):
            Intervention(
                id="i-001", title="t", mechanism="m", authority="a",
                cost_band=CostBand.NONE, time_to_effect=TimeToEffect.DAYS,
                reversibility=Reversibility.TRIVIAL, falsifier="",
            )

    def test_automatable_share_counts_only_mechanical_steps(self):
        steps = (
            PlanStep.with_governance(id="s-001", description="compute eligibility"),
            PlanStep.with_governance(id="s-002", description="check the statute",
                                     rests_on=(ClaimType.LEGAL,)),
            PlanStep.with_governance(id="s-003", description="adopt the threshold",
                                     rests_on=(ClaimType.NORMATIVE,), decider="Council"),
        )
        option = intervention(
            "i-001", CostBand.NONE, TimeToEffect.DAYS, Reversibility.TRIVIAL, steps
        )
        assert option.automatable_share == pytest.approx(1 / 3, abs=1e-4)
        assert option.value_decisions == 1
