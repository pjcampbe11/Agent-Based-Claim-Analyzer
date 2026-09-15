"""Claim clustering: what may be merged, and what must never be.

The whole file is about one asymmetry. A cluster that should have merged and
did not costs model calls. A cluster that merged and should not have publishes
one claim's verdict against another claim's words -- confidently, with a
citation, and with nothing on the surface to show it went wrong.

Only the second is a lie, so nearly every test below is about a merge that must
NOT happen.
"""

from __future__ import annotations

import json

import pytest

from abca.config import ModelSpec
from abca.inputs.threads import Post, Thread
from abca.pipeline.cluster import (
    CLUSTER_THRESHOLD,
    EXACT_PAIRWISE_LIMIT,
    REVIEW_SIMILARITY,
    run_cluster,
    signature,
    similarity,
)
from abca.pipeline.models import DraftClaim
from abca.pipeline.orchestrator import AnalyzeOptions, analyze_thread
from abca.providers.base import Completion
from abca.providers.registry import ProviderRegistry, RoleProvider
from abca.schema.enums import ClaimType, StageName
from abca.schema.ledger import ModelIdentity

SIGNATURES = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to form a new party."
REWORDED = "Illinois requires 25,000 signatures under 10 ILCS 5/10-2 to start a new political party."
DIFFERENT_NUMBER = "Under 10 ILCS 5/10-2 Illinois requires 20,000 signatures to form a new party."
DIFFERENT_SECTION = "Under 10 ILCS 5/10-3 Illinois requires 25,000 signatures to form a new party."
FULL_SLATE = "Under 10 ILCS 5/10-2 the petition must list candidates for all offices to be filled."


def claim(index: int, text: str, **overrides) -> DraftClaim:
    fields = {
        "id": f"c-{index:03d}",
        "text": text,
        "sentence_index": index,
        "claim_type": ClaimType.LEGAL,
    }
    fields.update(overrides)
    return DraftClaim(**fields)


class TestWhatMerges:
    def test_a_reworded_identical_claim_merges(self):
        result = run_cluster([claim(1, SIGNATURES), claim(2, REWORDED)]).value
        assert len(result.clusters) == 1
        assert result.merged_count == 1

    def test_an_exact_duplicate_merges(self):
        result = run_cluster([claim(1, SIGNATURES), claim(2, SIGNATURES)]).value
        assert result.clusters[0].min_similarity == 1.0

    def test_the_representative_is_the_first_seen(self):
        """Reproducible: the same input must always pick the same target."""
        claims = [claim(1, SIGNATURES), claim(2, REWORDED), claim(3, SIGNATURES)]
        result = run_cluster(claims).value
        assert result.clusters[0].representative.id == "c-001"
        assert result.represented_by == {"c-001": "c-001", "c-002": "c-001", "c-003": "c-001"}


class TestWhatMustNotMerge:
    def test_a_different_number_never_merges(self):
        """The number is exactly what the verdict turns on."""
        result = run_cluster([claim(1, SIGNATURES), claim(2, DIFFERENT_NUMBER)]).value
        assert len(result.clusters) == 2

    def test_a_different_statute_section_never_merges(self):
        """Answered from different sections, so they cannot share a verdict."""
        result = run_cluster([claim(1, SIGNATURES), claim(2, DIFFERENT_SECTION)]).value
        assert len(result.clusters) == 2

    def test_a_different_claim_type_never_merges(self):
        result = run_cluster([
            claim(1, SIGNATURES),
            claim(2, SIGNATURES, claim_type=ClaimType.EMPIRICAL),
        ]).value
        assert len(result.clusters) == 2

    def test_an_ambiguous_restatement_never_merges(self):
        """A sarcastic echo is not the same assertion as a sincere one."""
        result = run_cluster([
            claim(1, SIGNATURES),
            claim(2, SIGNATURES, ambiguous_stance=True),
        ]).value
        assert len(result.clusters) == 2

    def test_a_different_claim_about_the_same_statute_never_merges(self):
        result = run_cluster([claim(1, SIGNATURES), claim(2, FULL_SLATE)]).value
        assert len(result.clusters) == 2

    def test_gated_out_claims_are_never_clustered(self):
        """Their verdict comes from the gate, so there is nothing to share."""
        result = run_cluster([
            claim(1, "My dog is called Charlie.", gated_out=True),
            claim(2, "My dog is called Charlie.", gated_out=True),
        ]).value
        assert len(result.clusters) == 2
        assert result.merged_count == 0

    def test_the_threshold_is_far_stricter_than_the_fidelity_gate(self):
        """Opposite problems: a rewrite SHOULD differ; a duplicate should not."""
        from abca.fidelity.elements import MATCH_THRESHOLD

        assert CLUSTER_THRESHOLD > MATCH_THRESHOLD
        assert CLUSTER_THRESHOLD >= 0.7


class TestPublishedGrouping:
    def test_a_singleton_publishes_no_cluster_ref(self):
        """A ClusterRef in a record should MEAN something: another claim's text."""
        result = run_cluster([claim(1, SIGNATURES)]).value
        assert result.ref_for("c-001") is None

    def test_members_publish_size_and_weakest_similarity(self):
        result = run_cluster([claim(1, SIGNATURES), claim(2, REWORDED)]).value
        ref = result.ref_for("c-002")
        assert ref is not None
        assert ref.members == 2
        assert 0.0 < ref.min_similarity <= 1.0

    def test_min_similarity_is_the_true_pairwise_minimum(self):
        """It claims to be a minimum, so it has to be one.

        Representative-to-member distance would be an upper bound: two members
        can each be close to the representative and further from each other.
        """
        claims = [claim(1, SIGNATURES), claim(2, REWORDED), claim(3, SIGNATURES)]
        cluster = run_cluster(claims).value.clusters[0]
        worst = min(
            similarity(left.text, right.text)
            for left in cluster.members for right in cluster.members
            if left.id != right.id
        )
        assert cluster.min_similarity == pytest.approx(worst)

    def test_a_weak_cluster_is_flagged_for_review(self):
        outcome = run_cluster([claim(1, SIGNATURES), claim(2, REWORDED)])
        cluster = outcome.value.clusters[0]
        if cluster.min_similarity < REVIEW_SIMILARITY:
            assert any("worth reading first" in note for note in outcome.notes)

    def test_a_large_cluster_says_its_minimum_is_a_bound(self):
        """Reporting a sampled number as exact would be a small lie in the
        one field whose job is letting a reader check the grouping."""
        claims = [claim(i, SIGNATURES) for i in range(EXACT_PAIRWISE_LIMIT + 5)]
        outcome = run_cluster(claims)
        assert any("UPPER bound" in note for note in outcome.notes)


class TestSignature:
    def test_signature_captures_the_four_exact_gates(self):
        left = signature(claim(1, SIGNATURES))
        assert left == signature(claim(2, SIGNATURES))
        assert left != signature(claim(3, DIFFERENT_NUMBER))
        assert left != signature(claim(4, DIFFERENT_SECTION))
        assert left != signature(claim(5, SIGNATURES, claim_type=ClaimType.EMPIRICAL))
        assert left != signature(claim(6, SIGNATURES, ambiguous_stance=True))

    def test_similarity_is_symmetric_and_bounded(self):
        assert similarity(SIGNATURES, REWORDED) == similarity(REWORDED, SIGNATURES)
        assert similarity(SIGNATURES, SIGNATURES) == 1.0
        assert similarity("", SIGNATURES) == 0.0


# --------------------------------------------------------------------------
# Through the pipeline
# --------------------------------------------------------------------------


def _provider(role: str):
    identity = ModelIdentity(
        role=role, provider="ollama", name=f"m-{role}",
        weights_hash="sha256:" + f"{abs(hash(role)) % 10}" * 64,
    )

    class Scripted:
        name = "scripted"

        def identity(self):
            return identity

        def generate(self, request):
            prompt = request.prompt
            if "public affairs" in prompt and "IN SCOPE" in prompt:
                payload = {"decisions": [
                    {"sentence_index": i, "in_scope": True, "reason": "statutory claim"}
                    for i in range(6)
                ]}
            elif "atomic" in prompt:
                payload = {"claims": [
                    {"text": SIGNATURES, "sentence_index": 0,
                     "verbatim_span": None, "ambiguous_stance": False},
                    {"text": REWORDED, "sentence_index": 1,
                     "verbatim_span": None, "ambiguous_stance": False},
                    {"text": SIGNATURES, "sentence_index": 2,
                     "verbatim_span": None, "ambiguous_stance": False},
                ]}
            elif "six types" in prompt or "what would settle this" in prompt:
                payload = {"classifications": [
                    {"claim_id": f"c-{i:03d}", "claim_type": "LEGAL",
                     "confidence": 0.9, "reasoning": "settled by reading the statute"}
                    for i in range(1, 4)
                ]}
            elif "attack the analysis" in prompt or "Analysis to attack" in prompt:
                payload = {"assessments": []}
            else:
                payload = {"adjudications": []}
            return Completion(
                text=json.dumps(payload), identity=identity,
                prompt_tokens=10, completion_tokens=10, duration_ms=1,
            )

        def embed(self, texts):
            return []

        def health(self):
            return None

    return Scripted()


@pytest.fixture
def registry():
    spec = ModelSpec(role="x", provider="ollama", model="m")
    return ProviderRegistry({
        role: RoleProvider(role=role, provider=_provider(role), spec=spec)
        for role in ("classifier", "segmenter", "adjudicator", "redteam")
    })


@pytest.fixture
def thread():
    return Thread.build([
        Post(id="", text="Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures.",
             author="@alice"),
        Post(id="", text="Illinois requires 25,000 signatures under 10 ILCS 5/10-2.",
             author="bob"),
        Post(id="", text="Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures.",
             author="@carol"),
    ], locator="thread.json")


class TestClusteredRun:
    def test_the_cluster_stage_sits_in_canonical_order(self, registry, thread):
        record = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        ).record
        assert [s.name.value for s in record.stages] == [
            "ingest", "segment", "classify", "gate", "cluster", "retrieve",
            "adjudicate", "red_team", "compose",
        ]
        assert record.audit() == []

    def test_only_the_representative_reaches_retrieval(self, registry, thread):
        """Asserted on the in-memory result, because a StageRecord stores only
        the HASH of its input and output -- those payloads routinely contain the
        analyzed text, and a published record must not republish it."""
        result = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        )
        assert result.clusters is not None
        assert result.clusters.merged_count == 2
        assert len(result.clusters.representatives) == 1

    def test_every_member_says_it_inherited_its_verdict(self, registry, thread):
        """The most important sentence in a clustered run's output.

        A reader must never have to infer from a cluster id that the evidence
        was consulted for somebody else's sentence.
        """
        result = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        )
        members = [c for c in result.analysis.claims if c.cluster and c.id != "c-001"]
        assert members
        for member in members:
            assert "NOT adjudicated on its own text" in member.reasoning
            assert "c-001" in member.reasoning

    def test_the_representative_does_not_claim_to_have_inherited(self, registry, thread):
        result = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        )
        representative = next(c for c in result.analysis.claims if c.id == "c-001")
        assert "NOT adjudicated on its own text" not in representative.reasoning
        assert representative.cluster is not None  # it is still in the group

    def test_no_cluster_adjudicates_every_claim_separately(self, registry, thread):
        result = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True, cluster=False), connectors={}
        )
        assert all(c.cluster is None for c in result.analysis.claims)
        assert result.clusters.merged_count == 0
        assert len(result.clusters.representatives) == 3

    def test_the_cluster_stage_is_recorded_either_way(self, registry, thread):
        """Its absence would be indistinguishable from a run of an older tool."""
        for enabled in (True, False):
            record = analyze_thread(
                thread, registry, AnalyzeOptions(offline=True, cluster=enabled),
                connectors={},
            ).record
            assert StageName.CLUSTER in {s.name for s in record.stages}

    def test_claims_map_back_to_the_post_they_came_from(self, registry, thread):
        result = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        )
        posts = {result.post_of(c) for c in result.analysis.claims}
        assert posts == {"p-001", "p-002", "p-003"}

    def test_the_record_carries_counts_but_never_handles(self, registry, thread):
        """Contract s8: a published record must not name who said what."""
        record = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        ).record
        blob = json.dumps(record.to_jsonable()).lower()
        for handle in ("alice", "bob", "carol"):
            assert handle not in blob

    def test_handles_stay_available_in_memory_for_the_local_report(
        self, registry, thread
    ):
        """The same rule the input text follows: hash in the record, text local."""
        result = analyze_thread(
            thread, registry, AnalyzeOptions(offline=True), connectors={}
        )
        assert result.thread is not None
        assert {p.display_author for p in result.thread.posts} == {
            "@alice", "bob", "@carol"
        }
