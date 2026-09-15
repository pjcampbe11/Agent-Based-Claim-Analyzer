"""Pipeline tests: ingest, stages, orchestration.

Stages are exercised against scripted providers, so no model, network or GPU is
involved and the whole file runs in well under a second.

The recurring theme is FAILURE BEHAVIOR. Each stage fails in a specific
direction on purpose, and those directions are the difference between an
analysis that is incomplete-and-says-so and one that is quietly wrong.
"""

from __future__ import annotations

import json

import pytest

from abca.config import Config, ModelSpec
from abca.pipeline.classify import FALLBACK_TYPE, run_classify
from abca.pipeline.gate import run_gate
from abca.pipeline.ingest import NORMALIZATION_RECIPE, ingest_text, normalize_text
from abca.pipeline.models import DraftClaim
from abca.pipeline.orchestrator import (
    COVERAGE_NOTE,
    AnalyzeOptions,
    analyze_text,
    to_published_claim,
)
from abca.pipeline.segment import resolve_span, run_segment
from abca.pipeline.sentences import split_sentences
from abca.prompts import assert_doctrine_free, load_prompt
from abca.providers.base import Completion, GenerationRequest, ProviderUnavailable
from abca.providers.registry import build_registry
from abca.providers.transport import FakeTransport
from abca.schema.enums import ClaimType, StageName, Verdict
from abca.schema.ledger import ModelIdentity

IDENTITY = ModelIdentity(role="classifier", provider="ollama", name="test",
                         weights_hash="sha256:" + "a" * 64)


class ScriptedProvider:
    """Returns canned JSON payloads in order. One per model call."""

    name = "scripted"

    def __init__(self, payloads: list[dict | Exception]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.prompts: list[str] = []

    def identity(self):
        return IDENTITY

    def generate(self, request: GenerationRequest) -> Completion:
        self.prompts.append(request.prompt)
        item = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return Completion(text=json.dumps(item), identity=IDENTITY,
                          prompt_tokens=100, completion_tokens=40, duration_ms=50)

    def embed(self, texts):  # pragma: no cover
        return []

    def health(self) -> None:
        return None


# ==========================================================================
# Prompts
# ==========================================================================


class TestPrompts:
    def test_shipped_prompts_are_doctrine_free(self):
        """The mechanical half of the separation the README promises."""
        assert_doctrine_free()

    def test_prompt_hash_is_stable(self):
        first = load_prompt(StageName.CLASSIFY)
        second = load_prompt(StageName.CLASSIFY)
        assert first.content_hash == second.content_hash

    def test_prompt_ref_carries_the_hash_not_the_text(self):
        """Run records are published; they carry provenance, not payload."""
        ref = load_prompt(StageName.GATE).to_ref()
        assert ref.content_hash.startswith("sha256:")
        assert not hasattr(ref, "text")

    def test_path_is_repo_relative_not_absolute(self):
        """An absolute path would differ per machine and leak the filesystem."""
        assert load_prompt(StageName.SEGMENT).path == "prompts/sotp/0.1.0/segment.md"


# ==========================================================================
# Ingest
# ==========================================================================


class TestIngest:
    def test_line_endings_normalized(self):
        result = ingest_text("One line.\r\nTwo lines.")
        assert "\r" not in result.text
        assert result.changes["line_endings"] == 1

    def test_invisible_characters_removed_and_reported(self):
        """Zero-width characters are used to defeat exact-match search."""
        result = ingest_text("The stat\u200bute requires signatures.")
        assert result.text == "The statute requires signatures."
        assert result.changes["invisible_removed"] == 1
        assert any("invisible" in note for note in result.notes)

    def test_nfc_change_count_is_the_real_edit(self):
        """A naive positional diff reports the whole document as changed.

        NFC changes length, so after the first difference every subsequent
        position compares unequal.
        """
        _, changes = normalize_text("café politics")
        assert changes["nfc_recomposed"] <= 3

    def test_trailing_whitespace_stripped(self):
        result = ingest_text("A line.   \nAnother.  ")
        assert result.text == "A line.\nAnother."

    def test_content_hash_is_of_normalized_text(self):
        """A verifier applies the named recipe and must get the same hash."""
        a = ingest_text("Text.\r\n")
        b = ingest_text("Text.\n")
        assert a.document.content_hash == b.document.content_hash

    def test_recipe_is_recorded(self):
        assert ingest_text("Text.").document.normalization == NORMALIZATION_RECIPE

    @pytest.mark.parametrize("raw", ["", "   ", "\n\t\n"])
    def test_empty_input_rejected(self, raw):
        """A record asserting nothing was found is worse than a clear error."""
        with pytest.raises(ValueError, match="empty|whitespace"):
            ingest_text(raw)

    def test_case_is_not_folded(self):
        """Normalize representation, never content."""
        assert ingest_text("The FEC and the fec.").text == "The FEC and the fec."


# ==========================================================================
# Gate
# ==========================================================================


class TestGate:
    def test_in_and_out_of_scope(self):
        sentences = split_sentences("Illinois requires 25,000 signatures. My dog sleeps.")
        provider = ScriptedProvider([{"decisions": [
            {"sentence_index": 0, "in_scope": True, "reason": "statutory requirement"},
            {"sentence_index": 1, "in_scope": False, "reason": "personal"},
        ]}])
        outcome = run_gate(provider, sentences)
        assert outcome.value[0].in_scope
        assert not outcome.value[1].in_scope

    def test_failure_passes_sentences_through(self):
        """Fail OPEN. A gate that fails closed silently shrinks the analysis."""
        sentences = split_sentences("A political claim. Another one.")
        provider = ScriptedProvider([ProviderUnavailable("down", provider="x")])
        outcome = run_gate(provider, sentences, max_attempts=1)
        assert all(decision.in_scope for decision in outcome.value.values())
        assert any("passed through as in-scope" in note for note in outcome.notes)

    def test_missing_decision_defaults_to_in_scope(self):
        sentences = split_sentences("First claim. Second claim.")
        provider = ScriptedProvider([{"decisions": [
            {"sentence_index": 0, "in_scope": True, "reason": "policy"},
        ]}])
        outcome = run_gate(provider, sentences)
        assert outcome.value[1].in_scope
        assert any("defaulted to in-scope" in note for note in outcome.notes)

    def test_hallucinated_index_is_discarded(self):
        """A decision attached to the wrong sentence would be worse than none."""
        sentences = split_sentences("Only one sentence here.")
        provider = ScriptedProvider([{"decisions": [
            {"sentence_index": 0, "in_scope": True, "reason": "ok"},
            {"sentence_index": 99, "in_scope": False, "reason": "invented"},
        ]}])
        outcome = run_gate(provider, sentences)
        assert 99 not in outcome.value
        assert any("not in the batch" in note for note in outcome.notes)

    def test_empty_input_makes_no_call(self):
        provider = ScriptedProvider([])
        assert run_gate(provider, []).value == {}
        assert provider.calls == 0


# ==========================================================================
# Segment
# ==========================================================================


class TestSpanResolution:
    def test_verbatim_quote_gives_a_tight_span(self):
        document = "Illinois requires 25,000 signatures and the deadline is June."
        sentence = split_sentences(document)[0]
        start, end, tier = resolve_span(
            document, sentence, "Illinois requires 25,000 signatures",
            "Illinois requires 25,000 signatures")
        assert tier == "verbatim"
        assert document[start:end] == "Illinois requires 25,000 signatures"

    def test_claim_text_located_when_no_quote(self):
        document = "Illinois requires 25,000 signatures and the deadline is June."
        sentence = split_sentences(document)[0]
        _, _, tier = resolve_span(document, sentence, "the deadline is June", None)
        assert tier == "text"

    def test_first_letter_capitalisation_still_locates(self):
        """Extracting a clause forces a capital; that is grammar, not content."""
        document = "Illinois requires signatures and the deadline is June."
        sentence = split_sentences(document)[0]
        start, end, tier = resolve_span(document, sentence, "The deadline is June", None)
        assert tier == "text"
        assert document[start:end] == "the deadline is June"

    def test_rephrased_claim_falls_back_to_the_sentence(self):
        """A correct answer, not a failure -- and never a fuzzy guess."""
        document = "It doubled last year."
        sentence = split_sentences(document)[0]
        start, end, tier = resolve_span(document, sentence, "The deficit doubled last year", None)
        assert tier == "sentence"
        assert (start, end) == (sentence.start, sentence.end)

    def test_wrong_quote_does_not_produce_a_wrong_span(self):
        document = "Illinois requires signatures."
        sentence = split_sentences(document)[0]
        _, _, tier = resolve_span(document, sentence, "something else", "not present anywhere")
        assert tier == "sentence"


class TestSegment:
    def test_compound_sentence_becomes_several_claims(self):
        document = "Illinois requires signatures and the deadline is June."
        sentences = split_sentences(document)
        provider = ScriptedProvider([{"claims": [
            {"text": "Illinois requires signatures.", "sentence_index": 0,
             "verbatim_span": "Illinois requires signatures", "ambiguous_stance": False},
            {"text": "The deadline is June.", "sentence_index": 0,
             "verbatim_span": "the deadline is June", "ambiguous_stance": False},
        ]}])
        outcome = run_segment(provider, document, sentences)
        assert [claim.text for claim in outcome.value] == [
            "Illinois requires signatures.", "The deadline is June."]
        assert [claim.id for claim in outcome.value] == ["c-001", "c-002"]

    def test_failure_falls_back_to_one_claim_per_sentence(self):
        """A worse analysis beats losing the author's words entirely."""
        document = "A claim here. Another claim."
        sentences = split_sentences(document)
        provider = ScriptedProvider([ProviderUnavailable("down", provider="x")])
        outcome = run_segment(provider, document, sentences, max_attempts=1)
        assert len(outcome.value) == 2
        assert any("one claim per sentence" in note for note in outcome.notes)

    def test_max_claims_truncates_and_says_so(self):
        document = " ".join(f"Claim number {i}." for i in range(20))
        sentences = split_sentences(document)
        provider = ScriptedProvider([{"claims": [
            {"text": s.text, "sentence_index": s.index, "verbatim_span": s.text,
             "ambiguous_stance": False} for s in sentences]}])
        outcome = run_segment(provider, document, sentences, max_claims=5)
        assert len(outcome.value) == 5
        assert any("claim limit of 5" in note for note in outcome.notes)

    def test_ambiguous_stance_is_flagged_not_resolved(self):
        document = "Oh sure, the process is totally fair."
        sentences = split_sentences(document)
        provider = ScriptedProvider([{"claims": [
            {"text": "The process is fair.", "sentence_index": 0,
             "verbatim_span": None, "ambiguous_stance": True}]}])
        outcome = run_segment(provider, document, sentences)
        assert outcome.value[0].ambiguous_stance
        assert any("ambiguous_stance" in note for note in outcome.notes)

    def test_span_tier_distribution_is_reported(self):
        """A model that never produces a locatable quote is a finding."""
        document = "Illinois requires signatures."
        sentences = split_sentences(document)
        provider = ScriptedProvider([{"claims": [
            {"text": "Illinois requires signatures.", "sentence_index": 0,
             "verbatim_span": None, "ambiguous_stance": False}]}])
        outcome = run_segment(provider, document, sentences)
        assert any("spans:" in note for note in outcome.notes)


# ==========================================================================
# Classify
# ==========================================================================


class TestClassify:
    def _claims(self):
        return [
            DraftClaim(id="c-001", text="Illinois requires signatures.", sentence_index=0),
            DraftClaim(id="c-002", text="That is unfair.", sentence_index=1),
        ]

    def test_types_assigned(self):
        provider = ScriptedProvider([{"classifications": [
            {"claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.9,
             "reasoning": "settled by reading the statute"},
            {"claim_id": "c-002", "claim_type": "NORMATIVE", "confidence": 0.85,
             "reasoning": "no observation settles fairness"},
        ]}])
        result = run_classify(provider, self._claims()).value
        assert result[0].claim_type is ClaimType.LEGAL
        assert result[1].claim_type is ClaimType.NORMATIVE

    def test_failure_falls_back_to_a_non_adjudicable_type(self):
        """Refusing to answer is safe; guessing a claim is checkable is not."""
        provider = ScriptedProvider([ProviderUnavailable("down", provider="x")])
        result = run_classify(provider, self._claims(), max_attempts=1).value
        assert all(claim.claim_type is FALLBACK_TYPE for claim in result)
        assert not FALLBACK_TYPE.is_verdict_eligible

    def test_unknown_claim_id_is_discarded(self):
        provider = ScriptedProvider([{"classifications": [
            {"claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.9, "reasoning": "x"},
            {"claim_id": "c-999", "claim_type": "EMPIRICAL", "confidence": 0.9, "reasoning": "y"},
        ]}])
        outcome = run_classify(provider, self._claims())
        assert any("unknown claim id" in note for note in outcome.notes)

    def test_low_confidence_is_surfaced(self):
        """The type decides whether a claim is adjudicated at all."""
        provider = ScriptedProvider([{"classifications": [
            {"claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.31, "reasoning": "unclear"},
            {"claim_id": "c-002", "claim_type": "NORMATIVE", "confidence": 0.9, "reasoning": "clear"},
        ]}])
        outcome = run_classify(provider, self._claims())
        assert any("below 60%" in note for note in outcome.notes)

    def test_claims_are_addressed_by_id_not_position(self):
        """A reordered response must still map correctly."""
        provider = ScriptedProvider([{"classifications": [
            {"claim_id": "c-002", "claim_type": "NORMATIVE", "confidence": 0.9, "reasoning": "b"},
            {"claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.9, "reasoning": "a"},
        ]}])
        result = run_classify(provider, self._claims()).value
        assert result[0].claim_type is ClaimType.LEGAL


# ==========================================================================
# Draft -> published claim
# ==========================================================================


class TestPublishedClaim:
    def test_normative_is_unverifiable_with_real_confidence(self):
        """A final, correct verdict -- it follows from a decision actually made."""
        claim = to_published_claim(DraftClaim(
            id="c-1", text="unfair", sentence_index=0,
            claim_type=ClaimType.NORMATIVE, classification_confidence=0.9))
        assert claim.verdict is Verdict.UNVERIFIABLE
        assert claim.confidence == 0.9

    def test_unchecked_claim_has_zero_confidence(self):
        """No verdict was reached; a nonzero number would imply one had."""
        claim = to_published_claim(DraftClaim(
            id="c-1", text="the law says X", sentence_index=0,
            claim_type=ClaimType.LEGAL, classification_confidence=0.95))
        assert claim.verdict is Verdict.UNSUPPORTED
        assert claim.confidence == 0.0

    def test_unchecked_reasoning_says_nobody_looked(self):
        """"We looked and found nothing" is a different finding from "nobody looked"."""
        claim = to_published_claim(DraftClaim(
            id="c-1", text="the law says X", sentence_index=0, claim_type=ClaimType.LEGAL))
        assert "No source was consulted" in claim.reasoning

    def test_gated_claim_is_out_of_scope(self):
        claim = to_published_claim(DraftClaim(
            id="x-1", text="my dog sleeps", sentence_index=0,
            claim_type=ClaimType.NORMATIVE, gated_out=True, gate_reason="personal"))
        assert claim.verdict is Verdict.OUT_OF_SCOPE
        assert "personal" in claim.reasoning

    def test_evidence_quality_is_none_with_no_citations(self):
        """The schema cross-checks this, so a mistake fails loudly."""
        claim = to_published_claim(DraftClaim(
            id="c-1", text="x", sentence_index=0, claim_type=ClaimType.EMPIRICAL))
        assert claim.evidence_quality is None
        assert claim.citations == []


# ==========================================================================
# Orchestration
# ==========================================================================


def ollama_transport(payloads: list[dict]) -> FakeTransport:
    return FakeTransport({
        "/api/version": {"version": "0.6.2"},
        "/api/tags": {"models": [{"name": "m", "digest": "a" * 64,
                                  "details": {"quantization_level": "Q4_K_M"}}]},
        "/api/show": {"details": {"quantization_level": "Q4_K_M"},
                      "model_info": {"qwen2.context_length": 32768}},
        "/api/generate": [
            {"response": json.dumps(payload), "prompt_eval_count": 500,
             "eval_count": 100, "total_duration": 1_000_000_000, "done_reason": "stop"}
            for payload in payloads
        ],
    })


RED_TEAM_NONE: dict = {"assessments": []}
GATE_OK = {"decisions": [
    {"sentence_index": 0, "in_scope": True, "reason": "statutory claim"},
    {"sentence_index": 1, "in_scope": False, "reason": "personal"},
]}
SEGMENT_OK = {"claims": [
    {"text": "Illinois requires 25,000 signatures.", "sentence_index": 0,
     "verbatim_span": "Illinois requires 25,000 signatures", "ambiguous_stance": False},
]}
CLASSIFY_OK = {"classifications": [
    {"claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.93,
     "reasoning": "settled by reading the statute"},
]}
ADJUDICATE_NONE: dict = {"adjudications": []}

TEXT = "Illinois requires 25,000 signatures. My dog is asleep."


@pytest.fixture
def registry():
    transport = ollama_transport([GATE_OK, SEGMENT_OK, CLASSIFY_OK, ADJUDICATE_NONE, RED_TEAM_NONE])
    config = Config(models={
        "classifier": ModelSpec(role="classifier", provider="ollama", model="m"),
        "adjudicator": ModelSpec(role="adjudicator", provider="ollama", model="m"),
    })
    roles = ["classifier", "segmenter", "adjudicator", "redteam"]
    return build_registry(config, roles=roles,
                          transports=dict.fromkeys(roles, transport))


@pytest.fixture
def no_connectors():
    """No source connectors: exercises the 'nothing to check against' path."""
    return {}


class TestOrchestrator:
    def test_produces_an_intact_record(self, registry):
        record = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record
        assert record.is_intact()
        assert record.audit() == []

    def test_stage_chain_is_in_canonical_order(self, registry):
        record = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record
        assert [s.name.value for s in record.stages] == [
            "ingest", "segment", "classify", "gate", "cluster", "retrieve",
            "adjudicate", "red_team", "compose"]

    def test_gated_sentence_is_recorded_not_deleted(self, registry):
        """Silently dropping text is how an analysis becomes unfalsifiable."""
        result = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={})
        excluded = [c for c in result.analysis.claims if c.verdict is Verdict.OUT_OF_SCOPE]
        assert len(excluded) == 1
        assert "dog" in excluded[0].text

    def test_coverage_note_is_the_first_note(self, registry):
        """UNSUPPORTED means different things at different coverage levels.

        A reader must not be able to see a verdict without seeing which sources
        the run could actually consult.
        """
        result = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={})
        assert result.analysis.notes[0] == COVERAGE_NOTE

    def test_red_team_recorded_as_true_when_it_ran(self, registry):
        """The ledger records whether the mandatory pass actually ran."""
        record = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record
        assert record.config.red_team is True

    def test_skipping_the_red_team_is_recorded_and_announced(self, registry):
        """Contract s7 makes it mandatory, so a run without it must say so."""
        from abca.pipeline.orchestrator import _RED_TEAM_SKIPPED_NOTE

        result = analyze_text(TEXT, registry, AnalyzeOptions(red_team=False), connectors={})
        assert result.record.config.red_team is False
        assert _RED_TEAM_SKIPPED_NOTE in result.analysis.notes

    def test_prompt_hashes_are_in_the_recipe(self, registry):
        record = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record
        stages = {ref.stage.value for ref in record.config.prompts}
        assert stages == {"gate", "segment", "classify", "adjudicate", "red_team"}
        assert all(ref.content_hash.startswith("sha256:") for ref in record.config.prompts)

    def test_editing_a_prompt_would_change_the_input_digest(self, registry):
        """The mechanism that stops a verdict being reclaimed under new wording."""
        record = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record
        original = record.compute_input_digest()
        tampered = record.config.prompts[0].model_copy(
            update={"content_hash": "sha256:" + "9" * 64})
        mutated = record.model_copy(update={
            "config": record.config.model_copy(
                update={"prompts": [tampered, *record.config.prompts[1:]]})})
        assert mutated.compute_input_digest() != original

    def test_model_identity_comes_from_the_backend(self, registry):
        record = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record
        assert record.reproducible is True
        assert all(m.weights_hash for m in record.config.models)

    def test_no_gate_option_skips_the_gate(self, registry):
        result = analyze_text(TEXT, registry, AnalyzeOptions(no_gate=True), connectors={})
        assert any("gate skipped" in note for note in result.analysis.notes)

    def test_empty_input_raises(self, registry):
        with pytest.raises(ValueError):
            analyze_text("   ", registry, AnalyzeOptions(), connectors={})

    def test_token_cost_is_recorded(self, registry):
        result = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={})
        assert any("model cost:" in note for note in result.analysis.notes)

    def test_run_is_reproducible_across_invocations(self, registry):
        """Same input, same recipe, same conclusions."""
        first = analyze_text(TEXT, registry, AnalyzeOptions(), connectors={}).record

        transport = ollama_transport([GATE_OK, SEGMENT_OK, CLASSIFY_OK, ADJUDICATE_NONE, RED_TEAM_NONE])
        config = Config(models={
            "classifier": ModelSpec(role="classifier", provider="ollama", model="m"),
            "adjudicator": ModelSpec(role="adjudicator", provider="ollama", model="m")})
        roles = ["classifier", "segmenter", "adjudicator", "redteam"]
        second_registry = build_registry(
            config, roles=roles, transports=dict.fromkeys(roles, transport))
        second = analyze_text(TEXT, second_registry, AnalyzeOptions(), connectors={}).record

        assert first.input_digest == second.input_digest
        assert first.semantic_digest == second.semantic_digest
