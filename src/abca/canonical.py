"""Canonical JSON serialization and content-addressed hashing.

WHY THIS MODULE EXISTS
======================
Every credibility claim abCA makes reduces to one sentence: *the same input,
under the same recipe, produces the same output, and here is the hash that
proves it.* That sentence is worthless if two runs that are semantically
identical serialize to different bytes. Ordinary ``json.dumps`` does exactly
that -- key order varies with insertion order, whitespace is arbitrary, and
float formatting is implementation-defined.

So all hashing in abCA goes through :func:`canonical_json`, which produces
one and only one byte sequence for a given JSON value.

RELATIONSHIP TO RFC 8785 (JCS)
==============================
This is RFC 8785 "JSON Canonicalization Scheme" with ONE deliberate,
documented deviation, described under "The float decision" below.

What we implement faithfully:

* Object keys sorted by their UTF-16 code-unit sequence (RFC 8785 s3.2.3).
  This is NOT the same as Python's default string sort, which orders by
  Unicode code point. The two disagree for any key containing a character
  above the Basic Multilingual Plane, because in UTF-16 those are encoded
  as surrogate pairs in the range U+D800..U+DFFF, which sorts *below*
  U+E000..U+FFFF. Getting this wrong would produce hashes that a
  conforming implementation in another language could not reproduce.
* Minimal whitespace: no spaces after ``:`` or ``,`` (RFC 8785 s3.2.1).
* Minimal string escaping, matching ECMAScript ``JSON.stringify``: only
  ``"``, ``\\``, and the C0 control characters are escaped, using the short
  forms ``\\b \\f \\n \\r \\t`` where they exist and ``\\u00xx`` otherwise.
  Non-ASCII characters are emitted literally as UTF-8.
* UTF-8 output, no BOM.
* Literal ``true`` / ``false`` / ``null``.

What we deliberately do NOT do:

* We do not Unicode-normalize strings here. RFC 8785 does not require it,
  and doing it at hash time would silently mutate content that a citation
  quote is supposed to reproduce verbatim. NFC normalization belongs at
  *ingest* (see the ingest stage), where it is a visible, logged
  transformation of the input document -- not a hidden side effect of
  hashing.

THE FLOAT DECISION (the one deviation)
======================================
RFC 8785 requires floats be printed using the ECMAScript
``Number::toString`` algorithm -- shortest decimal string that round-trips,
with exponential notation only outside the range 1e-7 .. 1e21. Python's
``repr()`` is also shortest-round-trip, but it switches to exponential
notation at *different* thresholds (``repr(1e16)`` is ``'1e+16'`` where JS
gives ``'10000000000000000'``) and formats negative exponents differently
(``'1e-07'`` vs ``'1e-7'``).

Rather than reimplement ECMAScript number formatting and carry the bug risk
forever, abCA takes a stricter and simpler route:

    **Every float is serialized as a fixed-precision decimal with exactly
    FLOAT_PRECISION digits after the point.**

So ``0.86`` serializes as ``0.860000``. This is:

* Deterministic across languages and platforms with no clever code --
  any implementation can produce it with a printf-style format.
* Lossless for the values abCA actually hashes. Confidences, similarity
  scores, and fidelity ratios are bounded in [0, 1] and are never
  meaningful past six decimal places. A model that reports a confidence
  difference of 1e-7 is reporting noise, and treating two such runs as
  identical is correct behavior, not a bug.
* Loud when misused. Values whose magnitude exceeds
  ``MAX_EXACT_FLOAT`` are rejected rather than silently rounded, because
  past that point a double cannot represent six decimal places and the
  guarantee would be a lie.

Non-finite floats (``NaN``, ``inf``, ``-inf``) are rejected outright: they
have no JSON representation at all, and their presence in a record means
an upstream computation went wrong and should fail loudly here.

Integers are emitted as integers (no decimal point), matching RFC 8785's
treatment of values with no fractional part. ``bool`` is checked before
``int`` throughout, because in Python ``bool`` is a subclass of ``int`` and
``True`` would otherwise serialize as ``1``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

# --------------------------------------------------------------------------
# Tunables. Changing either of these changes every hash the tool produces,
# so both are effectively part of SCHEMA_VERSION. Do not touch without a
# schema bump.
# --------------------------------------------------------------------------

#: Digits after the decimal point for every serialized float. See module docstring.
FLOAT_PRECISION: Final[int] = 6

#: Largest magnitude for which an IEEE-754 double still carries six decimal
#: places of real precision. 2**53 is the last integer representable exactly;
#: dividing by 10**6 gives the bound at which the fractional guarantee holds.
MAX_EXACT_FLOAT: Final[float] = (2**53) / (10**FLOAT_PRECISION)

#: Prefix used on every digest string emitted by this module. Keeping the
#: algorithm name attached to the value means a future migration to another
#: hash function is detectable in old records instead of ambiguous.
DIGEST_PREFIX: Final[str] = "sha256:"


class CanonicalizationError(ValueError):
    """Raised when a value cannot be canonicalized deterministically.

    This is always a programming error or a corrupted upstream computation,
    never a user-input problem -- user input is converted to plain JSON
    types by the schema layer before it ever reaches here. Failing loudly
    is correct: a silently-coerced value would produce a hash that means
    nothing.
    """


# --------------------------------------------------------------------------
# Key ordering
# --------------------------------------------------------------------------

def utf16_sort_key(key: str) -> bytes:
    """Return the sort key RFC 8785 mandates for an object member name.

    RFC 8785 s3.2.3 sorts object keys by their UTF-16 code units, compared
    as unsigned 16-bit integers. Encoding to big-endian UTF-16 and comparing
    the resulting bytes gives exactly that ordering, because big-endian byte
    comparison of 16-bit units is equivalent to numeric comparison of those
    units.

    ``surrogatepass`` is specified so that a lone surrogate (which can occur
    in strings decoded from malformed input) raises later at encode time
    rather than exploding here with a confusing error.

    Worked example of why Python's default sort is wrong::

        >>> sorted(["\\ue000", "\\U00010000"])            # code-point order
        ['\\ue000', '\\U00010000']
        >>> sorted(["\\ue000", "\\U00010000"], key=utf16_sort_key)
        ['\\U00010000', '\\ue000']

    U+10000 encodes as the surrogate pair D800 DC00, and 0xD800 < 0xE000,
    so in UTF-16 order it sorts first. A conforming JCS implementation in
    JavaScript would agree with the second result, not the first.
    """
    return key.encode("utf-16-be", errors="surrogatepass")


# --------------------------------------------------------------------------
# Scalar formatting
# --------------------------------------------------------------------------

def format_float(value: float) -> str:
    """Serialize a float to its canonical fixed-precision decimal string.

    See "THE FLOAT DECISION" in the module docstring for the rationale.

    Rejects non-finite values and values too large to carry
    ``FLOAT_PRECISION`` decimal places. Normalizes negative zero to ``0``
    with the same number of decimals, because ``-0.0 == 0.0`` in JSON
    semantics and emitting ``-0.000000`` would make two equal values hash
    differently.
    """
    if not math.isfinite(value):
        raise CanonicalizationError(
            f"non-finite float cannot be canonicalized: {value!r}. "
            "NaN/inf in a run record means an upstream computation failed; "
            "fix the producer rather than the serializer."
        )
    if abs(value) > MAX_EXACT_FLOAT:
        raise CanonicalizationError(
            f"float magnitude {value!r} exceeds MAX_EXACT_FLOAT "
            f"({MAX_EXACT_FLOAT!r}); at this magnitude an IEEE-754 double "
            f"cannot represent {FLOAT_PRECISION} decimal places, so the "
            "fixed-precision canonical form would silently lose information. "
            "Store such values as integers or strings instead."
        )
    # `+ 0.0` collapses -0.0 to 0.0 without affecting any other value.
    text = f"{value + 0.0:.{FLOAT_PRECISION}f}"
    # Guard against the "-0.000000" case that survives when the value is a
    # tiny negative number that rounds to zero (e.g. -1e-9).
    if text.startswith("-") and float(text) == 0.0:
        text = text[1:]
    return text


def _format_int(value: int) -> str:
    """Serialize an int. JSON has no integer size limit, so nothing to do.

    Note the caller is responsible for having already excluded ``bool``.
    """
    return str(value)


def _escape_string(value: str) -> str:
    """Serialize a string with RFC 8785 / ECMAScript-minimal escaping.

    ``json.dumps`` with ``ensure_ascii=False`` already implements precisely
    the required escaping rules: it escapes ``"``, ``\\``, and C0 control
    characters (using the two-character short forms where ECMAScript defines
    them), and emits every other character literally. Delegating here rather
    than hand-rolling an escaper avoids a whole family of subtle bugs, and
    the behavior is stable across CPython versions.
    """
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------
# Recursive serializer
# --------------------------------------------------------------------------

def _write(value: Any, out: list[str], path: str) -> None:
    """Append the canonical serialization of ``value`` to ``out``.

    Written as an explicit recursive walk appending to a list rather than as
    string concatenation, so that deeply nested records do not incur
    quadratic copying, and so that ``path`` can name the exact location of a
    bad value in the error message. Debuggability matters here: a
    canonicalization failure in a 400-claim run record is useless if it does
    not say *which* field broke.
    """
    # Order of these checks is load-bearing.
    #  - None first: cheap identity check.
    #  - bool BEFORE int: bool is a subclass of int in Python, so an
    #    isinstance(value, int) test would swallow True/False and emit 1/0.
    #  - str BEFORE Sequence: str is a Sequence of str, and would otherwise
    #    recurse forever one character at a time.
    #  - Mapping/Sequence via collections.abc so that dict/list subclasses
    #    and other mapping types serialize correctly.
    if value is None:
        out.append("null")
        return

    if isinstance(value, bool):
        out.append("true" if value else "false")
        return

    if isinstance(value, int):
        out.append(_format_int(value))
        return

    if isinstance(value, float):
        try:
            out.append(format_float(value))
        except CanonicalizationError as exc:
            raise CanonicalizationError(f"at {path}: {exc}") from exc
        return

    if isinstance(value, str):
        out.append(_escape_string(value))
        return

    if isinstance(value, Mapping):
        # Keys must be strings; JSON has no other key type. Reject anything
        # else rather than coercing, because coercion (e.g. int 1 and str
        # "1" both becoming "1") would let two distinct records collide.
        items = []
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"at {path}: object key {key!r} is {type(key).__name__}, "
                    "but JSON object keys must be strings. Convert explicitly "
                    "at the schema layer so the conversion is visible."
                )
            items.append(key)
        items.sort(key=utf16_sort_key)

        out.append("{")
        for index, key in enumerate(items):
            if index:
                out.append(",")
            out.append(_escape_string(key))
            out.append(":")
            _write(value[key], out, f"{path}.{key}")
        out.append("}")
        return

    if isinstance(value, Sequence):
        # Arrays preserve order -- that ordering is semantic content, not
        # incidental. Anything that needs order-independent hashing must be
        # sorted by its producer *before* it gets here, so the sort key is
        # an explicit, reviewable decision rather than a hidden one.
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out, f"{path}[{index}]")
        out.append("]")
        return

    raise CanonicalizationError(
        f"at {path}: value of type {type(value).__name__} is not JSON-native. "
        "Convert it in the schema layer (e.g. datetime -> RFC 3339 string, "
        "Enum -> its value, Decimal -> str) so the conversion is explicit "
        "and reviewable."
    )


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` to its single canonical UTF-8 byte sequence.

    Accepts only JSON-native Python types: ``None``, ``bool``, ``int``,
    ``float``, ``str``, and mappings/sequences of the same. Anything else
    raises :class:`CanonicalizationError` -- conversions are the schema
    layer's job, deliberately, so that no lossy transformation happens
    invisibly inside the hash function.

    The output is stable: for any two values that compare equal under JSON
    semantics, the bytes are identical.
    """
    parts: list[str] = []
    _write(value, parts, "$")
    return "".join(parts).encode("utf-8")


# --------------------------------------------------------------------------
# Digests
# --------------------------------------------------------------------------

def digest_bytes(data: bytes) -> str:
    """Hash raw bytes, returning a prefixed lowercase hex digest.

    Used for opaque payloads -- file contents, fetched web pages, model
    weight files -- where the bytes are the artifact and no canonicalization
    applies.
    """
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    """Hash a text string as UTF-8.

    Note this hashes the string *as given*. Callers that want normalized
    text must normalize first, visibly. This function will not do it for
    them, for the reason given in the module docstring.
    """
    return digest_bytes(text.encode("utf-8"))


def digest_json(value: Any) -> str:
    """Canonicalize ``value`` and hash the result.

    This is the workhorse used for every structural digest in the ledger:
    input digests, stage records, semantic projections, and the run's final
    ``ledger_hash``.
    """
    return digest_bytes(canonical_json(value))


def digest_chain(previous: str | None, payload: Any) -> str:
    """Compute one link of the tamper-evident stage chain.

    Each stage record's hash commits to the hash of the stage before it, so
    altering any earlier stage invalidates every hash that follows. This is
    the same construction a blockchain uses, minus the distributed
    consensus, which is unnecessary here: the ledger is published alongside
    its hash, so a reader who has the published hash can detect any edit.

    ``previous`` is ``None`` for the genesis stage, which is encoded
    explicitly as JSON ``null`` rather than as an empty string, so that a
    genesis record can never be confused with a record whose predecessor
    hash was lost.
    """
    return digest_json({"prev": previous, "payload": payload})


__all__ = [
    "DIGEST_PREFIX",
    "FLOAT_PRECISION",
    "MAX_EXACT_FLOAT",
    "CanonicalizationError",
    "canonical_json",
    "digest_bytes",
    "digest_chain",
    "digest_json",
    "digest_text",
    "format_float",
    "utf16_sort_key",
]
