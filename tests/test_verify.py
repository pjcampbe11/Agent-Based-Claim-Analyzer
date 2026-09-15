"""Verification decision-tree tests.

One test per outcome, plus the ordering property that matters most: DRIFTED
is checked BEFORE DIVERGENT, so a changed statute is not reported as a tool
bug.
"""

from __future__ import annotations

import pytest

from abca.ledger.recorder import RunRecorder
from abca.ledger.verify import (
    LedgerTampered,
    compare,
    detect_drift,
    diff_recipes,
    diff_verdicts,
    verify,
)
from abca.schema.enums import Profile, Verdict, VerifyOutcome
from abca.schema.ledger import ModelIdentity, RunConfig


class StubReplayer:
    """Returns a canned record. Lets the ledger be tested with no pipeline."""

    def __init__(self, record):
        self._record = record

    def replay(self, record):
        return self._record


def _rerun(document, config, environment, result, sources=()):
    """Produce a second record from the same recipe, as a replay would."""
    recorder = RunRecorder(document=document, config=config, environment=environment)
    for snapshot in sources:
        recorder.record_source(snapshot)
    return recorder.finalize(result)


class TestIdentical:
    def test_same_recipe_same_output(self, record, document, config, environment, result,
                                     source_snapshot):
        replay = _rerun(document, config, environment, result, [source_snapshot])
        report = compare(record, replay)
        assert report.outcome is VerifyOutcome.IDENTICAL
        assert report.ok
        assert report.exit_code == 0

    def test_run_id_and_timestamps_do_not_matter(self, record, document, config, environment,
                                                 result, source_snapshot):
        """A replay is a NEW run; its id and clock must not affect the comparison."""
        replay = _rerun(document, config, environment, result, [source_snapshot])
        assert replay.run_id != record.run_id
        assert compare(record, replay).outcome is VerifyOutcome.IDENTICAL


class TestEquivalent:
    def test_prose_differs_only(self, record, document, config, environment, result,
                                source_snapshot):
        reworded = result.claims[0].model_copy(update={"reasoning": "different words, same finding"})
        altered = result.model_copy(update={"claims": [reworded, *result.claims[1:]]})
        replay = _rerun(document, config, environment, altered, [source_snapshot])

        report = compare(record, replay)
        assert report.outcome is VerifyOutcome.EQUIVALENT
        assert report.ok
        assert report.exit_code == 0


class TestDrifted:
    def test_changed_source_reported_as_drift_not_divergence(
        self, record, document, config, environment, result, source_snapshot
    ):
        """The ordering property. A repealed statute is not a tool bug."""
        flipped = result.claims[0].model_copy(update={"verdict": Verdict.CONTRADICTED})
        altered = result.model_copy(update={"claims": [flipped, *result.claims[1:]]})
        replay = _rerun(document, config, environment, altered, [source_snapshot])

        def probe(url: str) -> str:
            return "sha256:" + "9" * 64  # the source changed

        report = compare(record, replay, probe=probe)
        assert report.outcome is VerifyOutcome.DRIFTED
        assert report.drifted_sources[0]["status"] == "changed"
        assert report.verdict_diff[0]["claim_id"] == "c-001"

    def test_drift_is_not_success(self, record, document, config, environment, result,
                                  source_snapshot):
        """Nothing was done wrong, but a human must look again."""
        flipped = result.claims[0].model_copy(update={"verdict": Verdict.CONTRADICTED})
        altered = result.model_copy(update={"claims": [flipped, *result.claims[1:]]})
        replay = _rerun(document, config, environment, altered, [source_snapshot])
        report = compare(record, replay, probe=lambda url: "sha256:" + "9" * 64)

        assert not report.ok
        assert report.exit_code == 2

    def test_unreachable_source_counts_as_drift(self, record, document, config, environment,
                                                result, source_snapshot):
        """A 404 is not evidence the verdict still holds."""
        flipped = result.claims[0].model_copy(update={"verdict": Verdict.CONTRADICTED})
        altered = result.model_copy(update={"claims": [flipped, *result.claims[1:]]})
        replay = _rerun(document, config, environment, altered, [source_snapshot])
        report = compare(record, replay, probe=lambda url: None)

        assert report.outcome is VerifyOutcome.DRIFTED
        assert report.drifted_sources[0]["status"] == "unreachable"

    def test_no_probe_means_no_drift_detected(self, record, document, config, environment,
                                              result, source_snapshot):
        """With no retrieval layer wired up, drift is structurally unobservable."""
        assert detect_drift(record, None) == []


class TestDivergent:
    def test_same_recipe_unchanged_sources_different_verdicts_is_a_bug(
        self, record, document, config, environment, result, source_snapshot
    ):
        flipped = result.claims[0].model_copy(update={"verdict": Verdict.CONTRADICTED})
        altered = result.model_copy(update={"claims": [flipped, *result.claims[1:]]})
        replay = _rerun(document, config, environment, altered, [source_snapshot])

        report = compare(record, replay, probe=lambda url: source_snapshot.content_hash)
        assert report.outcome is VerifyOutcome.DIVERGENT
        assert report.exit_code == 3
        assert "defect in the analyzer" in " ".join(report.notes)

    def test_recipe_mismatch_reports_the_field_that_moved(
        self, record, document, config, environment, result, source_snapshot
    ):
        """An operator who changed a flag needs to be told WHICH flag."""
        other = config.model_copy(update={"seed": 43})
        replay = _rerun(document, other, environment, result, [source_snapshot])

        report = compare(record, replay)
        assert report.outcome is VerifyOutcome.DIVERGENT
        assert report.recipe_diff["config.seed"] == (42, 43)
        assert "operator error" in " ".join(report.notes)

    def test_fewer_claims_is_a_disagreement(self, record, document, config, environment,
                                            result, source_snapshot):
        """A replay that dropped a claim disagrees, even if the rest match."""
        trimmed = result.model_copy(update={"claims": result.claims[:1]})
        replay = _rerun(document, config, environment, trimmed, [source_snapshot])
        report = compare(record, replay, probe=lambda url: source_snapshot.content_hash)

        assert report.outcome is VerifyOutcome.DIVERGENT
        missing = [d for d in report.verdict_diff if d["claim_id"] == "c-002"]
        assert missing and missing[0]["replay"] is None


class TestUnreplayable:
    def test_hosted_api_run_cannot_be_certified(self, document, environment, result):
        """The honest outcome for a recipe that cannot be pinned by a third party."""
        config = RunConfig(
            profile=Profile.STANDARD, seed=42, temperature=0.0, max_claims=10, reading_level=8,
            models=[ModelIdentity(role="adjudicator", provider="api:vendor", name="hosted-x")],
        )
        original = _rerun(document, config, environment, result)
        replay = _rerun(document, config, environment, result)

        report = compare(original, replay)
        assert report.outcome is VerifyOutcome.UNREPLAYABLE
        assert report.exit_code == 4
        assert "cannot be pinned" in " ".join(report.notes)
        assert not report.ok

    def test_unreplayable_takes_precedence_over_identical(self, document, environment, result):
        """Even a byte-identical hosted replay is not a certification.

        The second run could have hit different weights behind the same
        endpoint and coincidentally agreed. Reporting IDENTICAL there would
        be a claim the record cannot support.
        """
        config = RunConfig(
            profile=Profile.STANDARD, seed=42, temperature=0.0, max_claims=10, reading_level=8,
            models=[ModelIdentity(role="adjudicator", provider="api:vendor", name="hosted-x")],
        )
        original = _rerun(document, config, environment, result)
        replay = _rerun(document, config, environment, result)
        assert original.output_digest == replay.output_digest
        assert compare(original, replay).outcome is VerifyOutcome.UNREPLAYABLE


class TestTamperedBaseline:
    def test_forged_original_refuses_to_be_a_baseline(self, record, document, config,
                                                      environment, result, source_snapshot):
        """Comparing against a record that is not what it claims to be is meaningless."""
        forged = record.model_copy(update={"semantic_digest": "sha256:" + "0" * 64})
        replay = _rerun(document, config, environment, result, [source_snapshot])

        with pytest.raises(LedgerTampered) as excinfo:
            compare(forged, replay)
        assert "semantic_digest mismatch" in str(excinfo.value)


class TestVerifyEntryPoint:
    def test_verify_uses_the_injected_replayer(self, record, document, config, environment,
                                               result, source_snapshot):
        replay = _rerun(document, config, environment, result, [source_snapshot])
        report = verify(record, StubReplayer(replay))
        assert report.outcome is VerifyOutcome.IDENTICAL

    def test_report_serializes(self, record, document, config, environment, result,
                               source_snapshot):
        replay = _rerun(document, config, environment, result, [source_snapshot])
        payload = verify(record, StubReplayer(replay)).to_jsonable()
        assert payload["outcome"] == "IDENTICAL"
        assert payload["run_id"] == record.run_id


class TestDiffHelpers:
    def test_recipe_diff_uses_dotted_paths(self, record, document, config, environment, result):
        other = config.model_copy(update={"temperature": 0.7})
        replay = _rerun(document, other, environment, result)
        assert diff_recipes(record, replay)["config.temperature"] == (0.0, 0.7)

    def test_verdict_diff_is_empty_when_equal(self, record, result):
        assert diff_verdicts(record.result, result) == []
