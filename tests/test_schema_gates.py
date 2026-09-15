"""Tests for the contract gates encoded as schema validators.

These are the machine-checkable subset of docs/01-source-of-truth-prompt.md.
Each test names the contract section it enforces. If one of these ever gets
relaxed, the corresponding claim in the README stops being true.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from abca.schema.core import Citation, Claim, InfluencePattern
from abca.schema.enums import ClaimType, Profile, SourceTier, Verdict
from abca.schema.ledger import ModelIdentity, RunConfig


def _claim(**overrides):
    base = dict(
        id="c-1",
        text="a claim",
        claim_type=ClaimType.LEGAL,
        verdict=Verdict.SUPPORTED,
        confidence=0.9,
        evidence_quality=SourceTier.T0,
        citations=[],
    )
    base.update(overrides)
    return Claim(**base)


class TestEvidenceGate:
    """Contract s2: SUPPORTED/CONTRADICTED/MIXED require a T0-T2 citation."""

    def test_supported_requires_qualifying_citation(self, citation):
        assert _claim(citations=[citation]).verdict is Verdict.SUPPORTED

    @pytest.mark.parametrize("tier", [SourceTier.T3, SourceTier.T4])
    def test_supported_rejected_on_weak_tiers(self, citation, tier):
        """The load-bearing validator: a fluent model cannot assert its way to SUPPORTED."""
        weak = citation.model_copy(update={"tier": tier})
        with pytest.raises(ValidationError, match="requires at least one T0-T2 citation"):
            _claim(citations=[weak], evidence_quality=tier)

    def test_supported_rejected_with_no_citations_at_all(self):
        with pytest.raises(ValidationError, match="requires at least one T0-T2 citation"):
            _claim(citations=[], evidence_quality=None)


class TestTypeGate:
    """Contract s3: evidence cannot settle normative, predictive or definitional claims."""

    @pytest.mark.parametrize(
        "claim_type", [ClaimType.NORMATIVE, ClaimType.PREDICTIVE, ClaimType.DEFINITIONAL]
    )
    def test_non_eligible_types_cannot_be_adjudicated(self, citation, claim_type):
        with pytest.raises(ValidationError, match="evidence cannot"):
            _claim(claim_type=claim_type, verdict=Verdict.SUPPORTED, citations=[citation])

    @pytest.mark.parametrize("verdict", [Verdict.UNVERIFIABLE, Verdict.OUT_OF_SCOPE])
    def test_non_eligible_types_accept_unverifiable(self, verdict):
        claim = _claim(
            claim_type=ClaimType.NORMATIVE, verdict=verdict, confidence=0.0, evidence_quality=None
        )
        assert claim.verdict is verdict


class TestReportedUnverified:
    """Contract s2: REPORTED_UNVERIFIED means T3-only support, by definition."""

    def test_requires_a_t3_source(self):
        with pytest.raises(ValidationError, match="cites no T3 source"):
            _claim(verdict=Verdict.REPORTED_UNVERIFIED, evidence_quality=None, citations=[])

    def test_rejected_when_stronger_evidence_exists(self, citation):
        with pytest.raises(ValidationError, match="cites T0-T2 evidence"):
            _claim(verdict=Verdict.REPORTED_UNVERIFIED, citations=[citation])

    def test_accepts_t3_only(self, citation):
        t3 = citation.model_copy(update={"tier": SourceTier.T3})
        claim = _claim(
            verdict=Verdict.REPORTED_UNVERIFIED, citations=[t3], evidence_quality=SourceTier.T3
        )
        assert claim.evidence_quality is SourceTier.T3


class TestEvidenceQualityIsComputed:
    def test_mismatch_rejected(self, citation):
        """A hallucinated evidence_quality would defeat every downstream gate."""
        with pytest.raises(ValidationError, match="computed from citations"):
            _claim(citations=[citation], evidence_quality=SourceTier.T2)

    def test_best_tier_wins(self, citation):
        strong = citation
        weak = citation.model_copy(update={"tier": SourceTier.T3, "url": "https://news/x"})
        claim = _claim(citations=[weak, strong], evidence_quality=SourceTier.T0)
        assert claim.evidence_quality is SourceTier.T0


class TestSpans:
    def test_partial_span_rejected(self):
        with pytest.raises(ValidationError, match="both be set or both be omitted"):
            _claim(span_start=0, citations=[], verdict=Verdict.UNSUPPORTED, evidence_quality=None)

    def test_inverted_span_rejected(self, citation):
        with pytest.raises(ValidationError, match="must be greater than"):
            _claim(span_start=10, span_end=5, citations=[citation])


class TestInfluencePatterns:
    """Contract s6: patterns are cited to an authority; actors are never named."""

    def test_requires_t0_t2_documenting_source(self, citation):
        weak = citation.model_copy(update={"tier": SourceTier.T4})
        with pytest.raises(ValidationError, match="must cite a T0-T2 source"):
            InfluencePattern(pattern="narrative reuse", documented_in=weak)

    def test_attribution_is_structurally_null(self, citation):
        pattern = InfluencePattern(pattern="narrative reuse", documented_in=citation)
        assert pattern.attribution is None
        with pytest.raises(ValidationError):
            InfluencePattern(
                pattern="x", documented_in=citation, attribution="a foreign state"
            )


class TestImmutability:
    def test_models_are_frozen(self, citation):
        """Evidence that can be mutated after the fact is not evidence."""
        claim = _claim(citations=[citation])
        with pytest.raises(ValidationError):
            claim.verdict = Verdict.CONTRADICTED

    def test_unknown_keys_rejected(self):
        """extra='forbid': an out-of-contract key is not 'mostly right'."""
        with pytest.raises(ValidationError):
            Citation(
                tier=SourceTier.T0, title="t", url="u", quote="q",
                retrieved_at="2026-09-03T00:00:00Z", content_hash="sha256:" + "0" * 64,
                surprise="!",
            )


class TestRunConfigGates:
    def test_backtranslate_must_differ_from_adjudicator(self):
        """Contract s5: same-model back-translation makes the fidelity gate a no-op."""
        same = [
            ModelIdentity(role="adjudicator", provider="ollama", name="qwen2.5:32b"),
            ModelIdentity(role="backtranslate", provider="ollama", name="qwen2.5:32b"),
        ]
        with pytest.raises(ValidationError, match="must differ from adjudicator"):
            RunConfig(
                profile=Profile.STANDARD, seed=42, temperature=0.0,
                max_claims=10, reading_level=8, models=same,
            )

    def test_duplicate_roles_rejected(self):
        models = [
            ModelIdentity(role="adjudicator", provider="ollama", name="a"),
            ModelIdentity(role="adjudicator", provider="ollama", name="b"),
        ]
        with pytest.raises(ValidationError, match="duplicate model role"):
            RunConfig(
                profile=Profile.STANDARD, seed=42, temperature=0.0,
                max_claims=10, reading_level=8, models=models,
            )

    def test_consensus_role_may_repeat(self):
        """Consensus is intentionally a list of peers, not a single slot."""
        models = [
            ModelIdentity(role="consensus", provider="ollama", name="a"),
            ModelIdentity(role="consensus", provider="ollama", name="b"),
        ]
        config = RunConfig(
            profile=Profile.FORENSIC, seed=42, temperature=0.0,
            max_claims=10, reading_level=8, models=models, consensus=True,
        )
        assert len(config.models) == 2


class TestEnumHelpers:
    """The tier and type helpers encode contract rules; they are not decoration."""

    def test_tier_rank_is_strongest_first(self):
        assert SourceTier.T0.rank == 0
        assert SourceTier.T4.rank == 4
        assert SourceTier.T0.is_at_least(SourceTier.T2)
        assert not SourceTier.T3.is_at_least(SourceTier.T2)

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            (SourceTier.T0, True), (SourceTier.T1, True), (SourceTier.T2, True),
            (SourceTier.T3, False), (SourceTier.T4, False),
        ],
    )
    def test_can_support_verdict(self, tier, expected):
        assert tier.can_support_verdict is expected

    def test_preferred_tiers_route_by_claim_type(self):
        assert ClaimType.LEGAL.preferred_tiers == (SourceTier.T0, SourceTier.T1)
        assert ClaimType.EMPIRICAL.preferred_tiers == (SourceTier.T1, SourceTier.T2)
        # Non-eligible types retrieve only to support premise decomposition.
        assert ClaimType.NORMATIVE.preferred_tiers == ()

    def test_verdict_outcomes_success_set(self):
        from abca.schema.enums import VerifyOutcome

        assert VerifyOutcome.IDENTICAL.is_success
        assert VerifyOutcome.EQUIVALENT.is_success
        # Drift is not a pass: a verdict resting on changed text needs a human.
        assert not VerifyOutcome.DRIFTED.is_success
        assert not VerifyOutcome.DIVERGENT.is_success
        assert not VerifyOutcome.UNREPLAYABLE.is_success


class TestAnalysisResultHelpers:
    def test_duplicate_claim_ids_rejected(self, citation):
        from datetime import UTC, datetime

        from pydantic import ValidationError as VE

        from abca.schema.core import AnalysisResult, DocumentRef
        from abca.schema.enums import InputKind

        doc = DocumentRef(
            kind=InputKind.TEXT, locator="<inline>", content_hash="sha256:" + "a" * 64,
            retrieved_at=datetime.now(UTC), byte_length=1,
        )
        duplicate = _claim(citations=[citation])
        with pytest.raises(VE, match="duplicate claim id"):
            AnalysisResult(document=doc, claims=[duplicate, duplicate])

    def test_cited_source_hashes_collects_from_claims(self, record):
        hashes = record.result.cited_source_hashes()
        assert "https://ilga.gov/ilcs/10-2" in hashes

    def test_counts(self, record):
        assert record.result.verdict_counts()["MIXED"] == 1
        assert record.result.type_counts()["NORMATIVE"] == 1


class TestFidelityReport:
    def test_cannot_preserve_more_elements_than_the_source_has(self):
        from pydantic import ValidationError as VE

        from abca.schema.core import FidelityReport

        with pytest.raises(VE, match="recovered elements the source does not contain"):
            FidelityReport(
                applied=True, score=1.0, reading_grade=8.0,
                elements_source=5, elements_preserved=7, modal_verbs_preserved=True,
                regenerations=0,
            )

    def test_unresolved_is_a_reachable_outcome(self):
        from abca.schema.core import FidelityReport

        report = FidelityReport(
            applied=True, score=0.6, reading_grade=11.2, elements_source=5,
            elements_preserved=3, modal_verbs_preserved=False, regenerations=3,
            unresolved=True,
        )
        assert report.unresolved


class TestNaiveDatetimesRejected:
    def test_citation_requires_aware_timestamp(self, citation):
        """A record timestamped in an unknown zone cannot be ordered against others."""
        from datetime import datetime

        from pydantic import ValidationError as VE

        with pytest.raises(VE, match="timezone-aware"):
            citation.model_copy().model_validate(
                {**citation.to_jsonable(), "retrieved_at": datetime(2026, 9, 3, 12, 0).isoformat()}
            )
