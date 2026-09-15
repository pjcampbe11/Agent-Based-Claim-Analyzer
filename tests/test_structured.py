"""Structured-output engine: extraction, validation, repair, accounting."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, field_validator

from abca.providers.base import Completion, GenerationRequest
from abca.providers.structured import (
    ExtractionStrategy,
    FailureKind,
    StructuredOutputError,
    build_repair_prompt,
    extract_json,
    generate_structured,
)
from abca.schema.ledger import ModelIdentity


class Answer(BaseModel):
    """Small model with a semantic rule a grammar could never express."""

    verdict: str
    confidence: float = Field(ge=0.0, le=1.0)
    cited: bool = False

    @field_validator("verdict")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in {"SUPPORTED", "CONTRADICTED"}:
            raise ValueError(f"unknown verdict {value!r}")
        return value


IDENTITY = ModelIdentity(role="adjudicator", provider="ollama", name="test-model",
                         weights_hash="sha256:" + "a" * 64)


class ScriptedProvider:
    """Returns pre-scripted completions in order. No network, no model."""

    name = "scripted"

    def __init__(self, texts: list[str], *, constrained: bool = False,
                 finish_reasons: list[str] | None = None) -> None:
        self.texts = texts
        self.constrained = constrained
        self.finish_reasons = finish_reasons or ["stop"] * len(texts)
        self.requests: list[GenerationRequest] = []

    def identity(self):
        return IDENTITY

    def generate(self, request: GenerationRequest) -> Completion:
        index = min(len(self.requests), len(self.texts) - 1)
        self.requests.append(request)
        return Completion(
            text=self.texts[index],
            identity=IDENTITY,
            prompt_tokens=10,
            completion_tokens=5,
            duration_ms=100,
            constrained=self.constrained,
            finish_reason=self.finish_reasons[index],
        )

    def embed(self, texts):  # pragma: no cover - unused here
        return []

    def health(self) -> None:  # pragma: no cover - unused here
        return None


VALID = '{"verdict":"SUPPORTED","confidence":0.9,"cited":true}'


class TestExtraction:
    def test_direct(self):
        assert extract_json(VALID)[1] is ExtractionStrategy.DIRECT

    def test_fenced(self):
        payload, strategy = extract_json(f"```json\n{VALID}\n```")
        assert strategy is ExtractionStrategy.FENCED
        assert payload["verdict"] == "SUPPORTED"

    def test_bare_fence(self):
        assert extract_json(f"```\n{VALID}\n```")[1] is ExtractionStrategy.FENCED

    def test_prose_wrapped(self):
        payload, strategy = extract_json(f"Here is my analysis: {VALID} Let me know!")
        assert strategy is ExtractionStrategy.BRACE_SCAN
        assert payload["confidence"] == 0.9

    def test_braces_inside_strings_do_not_end_the_scan(self):
        """Legal quotes and claim text are full of braces; this must not break."""
        text = 'Result: {"verdict":"SUPPORTED","confidence":0.5,"note":"a } brace"} done'
        payload, _ = extract_json(text)
        assert payload["note"] == "a } brace"

    def test_escaped_quotes_inside_strings(self):
        text = r'{"verdict":"SUPPORTED","confidence":0.5,"note":"he said \"hi\" and }"}'
        assert extract_json(text)[0]["note"] == 'he said "hi" and }'

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="no JSON object"):
            extract_json("I am not going to answer that.")

    def test_truncated_object_raises(self):
        with pytest.raises(ValueError):
            extract_json('{"verdict":"SUPPORTED","confid')

    def test_extraction_never_mutates_content(self):
        """Extraction locates the object; it must not 'fix' what is inside.

        Repairing malformed JSON would silently guess at meaning and hide a
        model that is not following instructions.
        """
        with pytest.raises(ValueError):
            extract_json('prefix {"verdict":"SUPPORTED",} suffix')  # trailing comma


class TestFirstAttemptSuccess:
    def test_clean_success_records_one_attempt(self):
        result = generate_structured(
            ScriptedProvider([VALID]), GenerationRequest(prompt="go"), Answer
        )
        assert result.value.verdict == "SUPPORTED"
        assert result.attempt_count == 1
        assert not result.repaired
        assert result.attempts[0].strategy is ExtractionStrategy.DIRECT

    def test_clean_success_produces_no_stage_noise(self):
        """The common case must add nothing to the ledger."""
        provider = ScriptedProvider([VALID], constrained=True)
        result = generate_structured(provider, GenerationRequest(prompt="go"), Answer)
        assert result.stage_notes() == []

    def test_schema_is_derived_from_the_model_when_absent(self):
        """Grammar and validator must be driven by the SAME schema.

        If they diverge, the backend can emit output the grammar accepts and
        the validator rejects -- on every attempt, forever.
        """
        provider = ScriptedProvider([VALID])
        generate_structured(provider, GenerationRequest(prompt="go"), Answer)
        assert provider.requests[0].json_schema == Answer.model_json_schema()


class TestRepairLoop:
    def test_schema_violation_then_success(self):
        bad = '{"verdict":"MAYBE","confidence":0.9}'
        provider = ScriptedProvider([bad, VALID])
        result = generate_structured(provider, GenerationRequest(prompt="go"), Answer)

        assert result.value.verdict == "SUPPORTED"
        assert result.attempt_count == 2
        assert result.attempts[0].failure is FailureKind.SCHEMA_INVALID
        assert "unknown verdict" in result.attempts[0].error_summary

    def test_repair_prompt_names_the_failing_field(self):
        provider = ScriptedProvider(['{"verdict":"MAYBE","confidence":0.9}', VALID])
        generate_structured(provider, GenerationRequest(prompt="analyze this"), Answer)
        second = provider.requests[1].prompt
        assert "verdict" in second
        assert "analyze this" in second  # original restated: providers are stateless

    def test_seed_is_held_constant_across_attempts(self):
        """Varying the seed would reroll until something passed.

        That would make the run irreproducible AND paper over a prompt or
        model problem. A retry re-asks with the errors named; it does not
        gamble.
        """
        provider = ScriptedProvider(["not json", '{"verdict":"MAYBE","confidence":1}', VALID])
        generate_structured(provider, GenerationRequest(prompt="go", seed=1234), Answer)
        assert {r.seed for r in provider.requests} == {1234}

    def test_malformed_json_then_success(self):
        provider = ScriptedProvider(["I refuse to answer.", VALID])
        result = generate_structured(provider, GenerationRequest(prompt="go"), Answer)
        assert result.attempts[0].failure is FailureKind.NO_JSON
        assert result.value.confidence == 0.9

    def test_truncation_is_diagnosed_as_truncation(self):
        """A cut-off response is not a syntax problem, and must not be reported as one."""
        provider = ScriptedProvider(
            ['{"verdict":"SUPPORTED","confi', VALID], finish_reasons=["length", "stop"]
        )
        result = generate_structured(provider, GenerationRequest(prompt="go"), Answer)
        assert result.attempts[0].failure is FailureKind.TRUNCATED
        assert "cut off" in provider.requests[1].prompt

    def test_exhausted_attempts_raise_with_full_history(self):
        provider = ScriptedProvider(['{"verdict":"NOPE","confidence":2}'] * 3)
        with pytest.raises(StructuredOutputError) as excinfo:
            generate_structured(provider, GenerationRequest(prompt="go"), Answer)
        assert len(excinfo.value.attempts) == 3
        assert "attempt 3" in str(excinfo.value)

    def test_max_attempts_is_respected(self):
        provider = ScriptedProvider(["garbage"] * 10)
        with pytest.raises(StructuredOutputError):
            generate_structured(
                provider, GenerationRequest(prompt="go"), Answer, max_attempts=2
            )
        assert len(provider.requests) == 2


class TestAccounting:
    def test_failed_attempts_count_toward_cost(self):
        """A model needing three tries costs three tries. Budgets must see that."""
        provider = ScriptedProvider(["bad", "still bad", VALID])
        result = generate_structured(provider, GenerationRequest(prompt="go"), Answer)
        assert result.total_tokens == 45  # 3 attempts * (10 + 5)
        assert result.total_duration_ms == 300

    def test_repair_is_recorded_in_stage_notes(self):
        provider = ScriptedProvider(["bad", VALID])
        notes = generate_structured(provider, GenerationRequest(prompt="go"), Answer).stage_notes()
        assert any("required 2 attempts" in note for note in notes)

    def test_fallback_extraction_is_recorded(self):
        """A model that never emits clean JSON is a finding, not a detail."""
        provider = ScriptedProvider([f"Sure! {VALID}"], constrained=True)
        notes = generate_structured(provider, GenerationRequest(prompt="go"), Answer).stage_notes()
        assert any("brace_scan" in note for note in notes)

    def test_unconstrained_backend_is_recorded(self):
        """Validated-after differs from enforced-during; the ledger says which."""
        provider = ScriptedProvider([VALID], constrained=False)
        notes = generate_structured(provider, GenerationRequest(prompt="go"), Answer).stage_notes()
        assert any("constrained decoding" in note for note in notes)


class TestRepairPromptShapes:
    def test_truncation_prompt_asks_for_brevity_not_syntax(self):
        prompt = build_repair_prompt("orig", FailureKind.TRUNCATED, "")
        assert "concise" in prompt
        assert "JSON" not in prompt.split("---")[1]

    def test_schema_prompt_includes_the_schema(self):
        prompt = build_repair_prompt(
            "orig", FailureKind.SCHEMA_INVALID, "- verdict: bad", schema={"type": "object"}
        )
        assert "Required schema" in prompt

    def test_parse_prompt_omits_the_schema(self):
        """Re-sending a large schema would eat the context the retry needs."""
        prompt = build_repair_prompt(
            "orig", FailureKind.NO_JSON, "nothing found", schema={"type": "object"}
        )
        assert "Required schema" not in prompt
