"""Canonicalization tests.

These guard the property everything else rests on: one value, one byte
sequence. Each test names the specific way naive JSON serialization would
break it.
"""

from __future__ import annotations

import pytest

from abca.canonical import (
    MAX_EXACT_FLOAT,
    CanonicalizationError,
    canonical_json,
    digest_chain,
    digest_json,
    digest_text,
    format_float,
    utf16_sort_key,
)


class TestKeyOrdering:
    def test_insertion_order_is_irrelevant(self):
        """The single most common way naive JSON hashing breaks."""
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_keys_sorted_utf16_not_codepoint(self):
        """RFC 8785 s3.2.3: UTF-16 code-unit order, not Python's code-point order.

        U+10000 encodes as the surrogate pair D800 DC00; 0xD800 sorts below
        0xE000, so the supplementary character comes FIRST in UTF-16 order and
        LAST in code-point order.
        """
        bmp = "\ue000"          # Private Use Area, single UTF-16 unit
        supplementary = "\U00010000"  # surrogate pair D800 DC00

        payload = {bmp: 1, supplementary: 2}
        expected = ('{"' + supplementary + '":2,"' + bmp + '":1}').encode("utf-8")
        assert canonical_json(payload) == expected

        # Python's default sort disagrees, which is exactly the trap.
        assert sorted([bmp, supplementary]) == [bmp, supplementary]
        assert sorted([bmp, supplementary], key=utf16_sort_key) == [supplementary, bmp]

    def test_nested_objects_sorted_at_every_depth(self):
        left = {"z": {"b": 1, "a": 2}}
        right = {"z": {"a": 2, "b": 1}}
        assert canonical_json(left) == canonical_json(right)

    def test_non_string_key_rejected(self):
        """Coercing int 1 and str '1' to the same key would let records collide."""
        with pytest.raises(CanonicalizationError, match="object keys must be strings"):
            canonical_json({1: "x"})


class TestFloats:
    def test_fixed_precision(self):
        assert format_float(0.86) == "0.860000"

    def test_equal_values_written_the_same_way_agree(self):
        """0.5 and 0.50 are the same double and must hash identically."""
        assert canonical_json({"x": 0.5}) == canonical_json({"x": 0.50})

    def test_negative_zero_normalized(self):
        """-0.0 == 0.0 in JSON semantics; emitting -0.000000 would split them."""
        assert format_float(-0.0) == "0.000000"
        assert canonical_json({"x": -0.0}) == canonical_json({"x": 0.0})

    def test_tiny_negative_does_not_emit_negative_zero(self):
        assert format_float(-1e-9) == "0.000000"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_rejected(self, value):
        """NaN/inf in a record means an upstream computation failed."""
        with pytest.raises(CanonicalizationError, match="non-finite"):
            format_float(value)

    def test_oversized_float_rejected(self):
        """Past MAX_EXACT_FLOAT the six-decimal guarantee would be a lie."""
        with pytest.raises(CanonicalizationError, match="MAX_EXACT_FLOAT"):
            format_float(MAX_EXACT_FLOAT * 10)

    def test_int_is_not_floated(self):
        """Ints keep integer form; 1 and 1.0 are distinguishable on the wire."""
        assert canonical_json({"x": 1}) == b'{"x":1}'
        assert canonical_json({"x": 1.0}) == b'{"x":1.000000}'

    def test_error_names_the_path(self):
        """A bad value in a 400-claim record must say WHERE it is."""
        with pytest.raises(CanonicalizationError, match=r"\$\.a\[1\]\.b"):
            canonical_json({"a": [0, {"b": float("inf")}]})


class TestScalars:
    def test_bool_before_int(self):
        """bool subclasses int in Python; True must not serialize as 1."""
        assert canonical_json({"x": True, "y": 1}) == b'{"x":true,"y":1}'

    def test_none_is_null(self):
        assert canonical_json(None) == b"null"

    def test_non_ascii_emitted_literally(self):
        """RFC 8785 emits UTF-8 directly rather than \\u escapes."""
        assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode()

    def test_control_characters_escaped(self):
        assert canonical_json("a\nb") == b'"a\\nb"'

    def test_unicode_not_normalized_at_hash_time(self):
        """NFC belongs at ingest, visibly -- not hidden inside the hasher.

        A citation quote must reproduce the source verbatim; silently
        normalizing it here would make the quote disagree with the source.
        """
        composed = "\u00e9"        # LATIN SMALL LETTER E WITH ACUTE
        decomposed = "e\u0301"     # e + COMBINING ACUTE ACCENT
        assert composed != decomposed
        assert canonical_json(composed) != canonical_json(decomposed)

    def test_unsupported_type_rejected(self):
        from datetime import datetime

        with pytest.raises(CanonicalizationError, match="not JSON-native"):
            canonical_json({"t": datetime.now()})


class TestArrays:
    def test_order_is_significant(self):
        """Arrays carry meaning in their order; producers must sort deliberately."""
        assert canonical_json([1, 2]) != canonical_json([2, 1])


class TestDigests:
    def test_digest_is_prefixed_hex(self):
        value = digest_json({"a": 1})
        assert value.startswith("sha256:")
        assert len(value) == len("sha256:") + 64

    def test_digest_stable_across_key_order(self):
        assert digest_json({"a": 1, "b": 2}) == digest_json({"b": 2, "a": 1})

    def test_digest_text_hashes_raw_utf8_not_json(self):
        """digest_text hashes the bytes; digest_json hashes the QUOTED form."""
        import hashlib

        assert digest_text("x") == "sha256:" + hashlib.sha256(b"x").hexdigest()
        assert digest_text("x") != digest_json("x")   # json adds the quotes

    def test_chain_genesis_distinct_from_empty_prev(self):
        """None (genesis) must not collide with a lost/blank predecessor hash."""
        assert digest_chain(None, {"a": 1}) != digest_chain("", {"a": 1})

    def test_chain_depends_on_predecessor(self):
        """The tamper-evidence property: same payload, different prev, different hash."""
        first = digest_chain(None, {"a": 1})
        assert digest_chain(first, {"a": 1}) != digest_chain(None, {"a": 1})
