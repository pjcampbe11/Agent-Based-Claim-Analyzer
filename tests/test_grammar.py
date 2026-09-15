"""GBNF conversion tests, verified against an independent recogniser.

Every test here asks the question that matters -- does the grammar ACCEPT what
it should and REJECT what it should -- rather than asserting on emitted text.
The oracle in ``tests/gbnf_oracle.py`` is a separate implementation, so
agreement between it and the converter is evidence rather than tautology.
"""

from __future__ import annotations

import json

import pytest
from gbnf_oracle import Grammar

from abca.providers.grammar import GrammarError, gbnf_for_model, json_schema_to_gbnf
from abca.schema.core import Citation, Claim
from abca.schema.enums import ClaimType, SourceTier, Verdict


def grammar_for(schema: dict) -> Grammar:
    return Grammar(json_schema_to_gbnf(schema))


SIMPLE = {
    "title": "Simple",
    "type": "object",
    "properties": {
        "verdict": {"enum": ["SUPPORTED", "CONTRADICTED"]},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["verdict", "confidence"],
}


class TestKeysAreQuoted:
    def test_quoted_keys_accepted(self):
        assert grammar_for(SIMPLE).accepts('{"verdict":"SUPPORTED","confidence":0.9}')

    def test_unquoted_keys_rejected(self):
        """The double-encoding bug this converter had once.

        A grammar emitting `"confidence"` as a terminal matches the BARE word,
        constraining the model to emit invalid JSON on every attempt while
        looking correct in the grammar text.
        """
        assert not grammar_for(SIMPLE).accepts('{verdict:"SUPPORTED",confidence:0.9}')

    def test_unquoted_enum_value_rejected(self):
        assert not grammar_for(SIMPLE).accepts('{"verdict":SUPPORTED,"confidence":0.9}')


class TestRequiredAndOptional:
    def test_required_only(self):
        assert grammar_for(SIMPLE).accepts('{"verdict":"SUPPORTED","confidence":0.9}')

    def test_with_optional(self):
        assert grammar_for(SIMPLE).accepts(
            '{"verdict":"SUPPORTED","confidence":0.9,"note":"hi"}'
        )

    def test_missing_required_rejected(self):
        assert not grammar_for(SIMPLE).accepts('{"confidence":0.9}')
        assert not grammar_for(SIMPLE).accepts('{"verdict":"SUPPORTED"}')

    def test_empty_object_rejected_when_required_exist(self):
        assert not grammar_for(SIMPLE).accepts("{}")


class TestClosedVocabularies:
    def test_invented_enum_member_is_unsamplable(self):
        """The point of constrained decoding: a ninth verdict cannot be emitted."""
        assert not grammar_for(SIMPLE).accepts('{"verdict":"TRUE","confidence":0.9}')

    def test_every_real_verdict_is_accepted(self):
        grammar = Grammar(gbnf_for_model(Claim))
        for verdict in Verdict:
            payload = json.dumps(
                {"id": "c", "text": "x", "claim_type": "LEGAL",
                 "verdict": verdict.value, "confidence": 0.5},
                separators=(",", ":"),
            )
            assert grammar.accepts(payload), verdict

    def test_every_real_claim_type_is_accepted(self):
        grammar = Grammar(gbnf_for_model(Claim))
        for claim_type in ClaimType:
            payload = json.dumps(
                {"id": "c", "text": "x", "claim_type": claim_type.value,
                 "verdict": "MIXED", "confidence": 0.5},
                separators=(",", ":"),
            )
            assert grammar.accepts(payload), claim_type


class TestDeclarationOrder:
    """The bug that only shows up against a real model.

    Pydantic emits fields in DECLARATION order, not required-first. A
    required-first grammar fights the model on every token.
    """

    def test_declaration_order_with_interleaved_optionals(self):
        grammar = Grammar(gbnf_for_model(Claim))
        schema = Claim.model_json_schema()
        # span_start/span_end are OPTIONAL and sit BETWEEN two required fields.
        assert list(schema["properties"])[2:4] == ["span_start", "span_end"]
        payload = json.dumps(
            {"id": "c", "text": "x", "span_start": 0, "span_end": 1,
             "claim_type": "LEGAL", "verdict": "MIXED", "confidence": 0.5},
            separators=(",", ":"),
        )
        assert grammar.accepts(payload)

    def test_out_of_order_keys_rejected(self):
        grammar = Grammar(gbnf_for_model(Claim))
        payload = '{"verdict":"MIXED","id":"c","text":"x","claim_type":"LEGAL","confidence":0.5}'
        assert not grammar.accepts(payload)


class TestNestedStructures:
    def test_claim_with_a_citation(self):
        grammar = Grammar(gbnf_for_model(Claim))
        citation_props = list(Citation.model_json_schema()["properties"])
        citation = {
            "tier": "T0", "title": "10 ILCS 5/10-2", "url": "https://ilga.gov/x",
            "quote": "signed by 1% ...", "retrieved_at": "2026-09-03T12:00:00Z",
            "content_hash": "sha256:" + "a" * 64,
        }
        ordered_citation = {k: citation[k] for k in citation_props if k in citation}
        payload = json.dumps(
            {"id": "c", "text": "x", "claim_type": "LEGAL", "verdict": "MIXED",
             "confidence": 0.5, "evidence_quality": "T0", "citations": [ordered_citation]},
            separators=(",", ":"),
        )
        assert grammar.accepts(payload)

    def test_invented_tier_inside_a_nested_citation_rejected(self):
        grammar = Grammar(gbnf_for_model(Claim))
        payload = (
            '{"id":"c","text":"x","claim_type":"LEGAL","verdict":"MIXED","confidence":0.5,'
            '"citations":[{"tier":"T9","title":"t","url":"u","quote":"q",'
            '"retrieved_at":"2026-09-03T12:00:00Z","content_hash":"sha256:aaa"}]}'
        )
        assert not grammar.accepts(payload)

    def test_empty_array_accepted(self):
        grammar = Grammar(gbnf_for_model(Claim))
        assert grammar.accepts(
            '{"id":"c","text":"x","claim_type":"LEGAL","verdict":"MIXED",'
            '"confidence":0.5,"citations":[]}'
        )


class TestOptionalNulls:
    def test_optional_enum_accepts_null(self):
        grammar = Grammar(gbnf_for_model(Claim))
        assert grammar.accepts(
            '{"id":"c","text":"x","claim_type":"LEGAL","verdict":"MIXED",'
            '"confidence":0.5,"evidence_quality":null}'
        )

    def test_optional_enum_accepts_a_real_tier(self):
        grammar = Grammar(gbnf_for_model(Claim))
        for tier in SourceTier:
            payload = (
                '{"id":"c","text":"x","claim_type":"LEGAL","verdict":"MIXED",'
                f'"confidence":0.5,"evidence_quality":"{tier.value}"}}'
            )
            assert grammar.accepts(payload), tier


class TestNumbers:
    @pytest.mark.parametrize("value", ["0", "0.5", "-3", "1.5e10", "-0.25", "1e-7"])
    def test_valid_numbers(self, value):
        assert grammar_for(SIMPLE).accepts(f'{{"verdict":"SUPPORTED","confidence":{value}}}')

    @pytest.mark.parametrize("value", ["01", "+1", ".5", "1.", "NaN", "Infinity"])
    def test_invalid_numbers_rejected(self, value):
        assert not grammar_for(SIMPLE).accepts(f'{{"verdict":"SUPPORTED","confidence":{value}}}')


class TestStrings:
    def test_escaped_quote_inside_a_string(self):
        assert grammar_for(SIMPLE).accepts(
            r'{"verdict":"SUPPORTED","confidence":1,"note":"he said \"hi\""}'
        )

    def test_unicode_escape(self):
        assert grammar_for(SIMPLE).accepts(
            r'{"verdict":"SUPPORTED","confidence":1,"note":"é"}'
        )

    def test_unescaped_quote_rejected(self):
        assert not grammar_for(SIMPLE).accepts(
            '{"verdict":"SUPPORTED","confidence":1,"note":"a"b"}'
        )


class TestLimitsAndErrors:
    def test_deep_recursion_is_caught(self):
        """A self-referential schema must fail loudly, not hang."""
        schema: dict = {"$ref": "#/$defs/Node", "$defs": {}}
        node: dict = {"type": "object", "properties": {}, "required": []}
        schema["$defs"]["Node"] = node
        # Build a chain deeper than MAX_DEPTH.
        current = node
        for index in range(40):
            child = {"type": "object", "properties": {}, "required": []}
            current["properties"][f"p{index}"] = child
            current["required"].append(f"p{index}")
            current = child
        with pytest.raises(GrammarError, match="nesting exceeded"):
            json_schema_to_gbnf(schema)

    def test_remote_ref_rejected(self):
        """A network fetch during grammar generation would be a hidden dependency."""
        with pytest.raises(GrammarError, match="only local"):
            json_schema_to_gbnf({"$ref": "https://example.com/schema.json"})

    def test_output_is_deterministic(self):
        """The grammar is part of the recipe; it must not vary between runs."""
        assert gbnf_for_model(Claim) == gbnf_for_model(Claim)

    def test_root_rule_comes_first(self):
        """llama.cpp takes the first rule as the entry point."""
        assert gbnf_for_model(Claim).splitlines()[0].startswith("root ::=")


class TestGrammarCannotEnforceSemantics:
    def test_grammar_accepts_what_the_validator_rejects(self):
        """The division of labour, demonstrated.

        A grammar constrains shape. It cannot know that SUPPORTED requires a
        T0-T2 citation. This payload is grammatically perfect and semantically
        forbidden -- which is exactly why validate-and-retry runs even when
        constrained decoding is available.
        """
        from pydantic import ValidationError

        payload = (
            '{"id":"c","text":"x","claim_type":"LEGAL","verdict":"SUPPORTED",'
            '"confidence":0.9,"citations":[]}'
        )
        assert Grammar(gbnf_for_model(Claim)).accepts(payload)
        with pytest.raises(ValidationError, match="requires at least one T0-T2"):
            Claim.model_validate(json.loads(payload))
