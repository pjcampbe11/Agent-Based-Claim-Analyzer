"""JSON Schema to GBNF, for grammar-constrained sampling on llama.cpp.

WHAT THIS BUYS
==============
Constrained decoding makes malformed output *impossible* rather than merely
detectable. The sampler is restricted at every token to continuations the
grammar permits, so the model cannot emit a stray code fence, a trailing
comma, or a verdict string outside the eight-member enum -- it is not that
those get rejected afterwards, it is that they are never sampled.

For a pipeline that runs the classifier once per claim across a large thread,
this is the difference between a repair loop firing constantly and firing
almost never.

WHAT IT DOES NOT BUY -- AND WHY THAT IS FINE
============================================
A grammar constrains SHAPE, not SEMANTICS. It can force ``verdict`` to be one
of eight strings. It cannot know that ``SUPPORTED`` requires a T0-T2 citation,
that ``evidence_quality`` must equal the best tier actually cited, or that a
``NORMATIVE`` claim may not be adjudicated at all. Those are the contract gates
in :mod:`abca.schema.core`, and they are enforced by pydantic validators after
generation.

The same applies to ``pattern`` constraints, which GBNF cannot express in
general: a ``Digest`` field is grammatically "any string" and is caught by the
validator instead.

So the two layers are complementary and both are required:

* **grammar** eliminates syntax failures cheaply, at sampling time;
* **validation** enforces meaning, and triggers repair when violated.

Neither is sufficient alone. A system with only the grammar would happily
produce well-formed lies.

SCOPE
=====
Handles the JSON Schema subset pydantic v2 actually emits for the models in
:mod:`abca.schema`: objects with ``properties``/``required``, ``$defs`` and
``$ref``, ``enum``/``const``, ``anyOf`` (including the ``Optional[X]`` form),
``allOf`` wrapping a single ``$ref``, arrays with ``items``/``minItems``, and
the scalar types. Unsupported keywords are ignored rather than raising, because
an ignored constraint degrades to "the validator catches it" -- which is a
working system -- whereas raising would make an entire model class ungeneratable
over a keyword the validator already handles.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Maximum $ref resolution depth. A self-referential schema would otherwise
#: recurse forever. abCA's own models are shallow; the guard exists so a
#: future model with a cycle fails loudly instead of hanging.
MAX_DEPTH: int = 24

#: GBNF rule names admit letters, digits and dashes only.
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9-]")


class GrammarError(ValueError):
    """Raised when a schema cannot be converted to a usable grammar."""


def _rule_name(raw: str) -> str:
    """Sanitize an arbitrary schema name into a legal GBNF rule name."""
    cleaned = _NAME_SAFE.sub("-", raw.replace("_", "-")).strip("-").lower()
    return cleaned or "anon"


def _terminal(text: str) -> str:
    """Render ``text`` as a GBNF terminal that matches it literally.

    ``json.dumps`` supplies the escaping, because GBNF string terminals use
    the same conventions as JSON for the characters that matter here (quote,
    backslash, control codes). Hand-rolling that escaping is a classic source
    of grammars that compile but never match.
    """
    return json.dumps(text)


def _json_terminal(value: Any) -> str:
    """GBNF terminal matching ``value`` AS IT APPEARS IN JSON.

    The double encoding is deliberate and is the easiest thing in this module
    to get wrong. A JSON object key is not the bare text ``confidence`` -- it
    is the six-character-plus-quotes sequence ``"confidence"``. So the GBNF
    terminal must itself contain those quotes::

        json.dumps("confidence")              -> '"confidence"'   (the JSON token)
        json.dumps('"confidence"')            -> '"\"confidence\""'  (the GBNF terminal)

    Getting this wrong produces a grammar that looks correct, compiles
    cleanly, and constrains the model to emit unquoted keys -- which then fail
    JSON parsing on every single attempt.

    Works uniformly for non-strings too: ``True`` -> ``true`` -> ``"true"``.
    """
    return json.dumps(json.dumps(value))


class GrammarBuilder:
    """Accumulates named GBNF rules and emits the final grammar text."""

    def __init__(self) -> None:
        self._rules: dict[str, str] = {}
        self._counter: dict[str, int] = {}
        #: Maps a ``$ref`` pointer to the rule generated for it, so a schema
        #: referenced from ten places produces one rule, not ten copies.
        self._ref_rules: dict[str, str] = {}

    def add(self, name: str, body: str) -> str:
        """Register a rule, uniquifying the name if it is already taken."""
        base = _rule_name(name)
        candidate = base
        while candidate in self._rules and self._rules[candidate] != body:
            self._counter[base] = self._counter.get(base, 0) + 1
            candidate = f"{base}-{self._counter[base]}"
        self._rules[candidate] = body
        return candidate

    def has(self, name: str) -> bool:
        return name in self._rules

    def render(self) -> str:
        """Emit the grammar with ``root`` first, then everything else sorted.

        ``root`` must come first: llama.cpp takes the first rule as the entry
        point. The rest are sorted so the output is deterministic -- a grammar
        that differed between runs would be one more thing making a recipe
        irreproducible.
        """
        if "root" not in self._rules:
            raise GrammarError("grammar has no root rule")
        lines = [f"root ::= {self._rules['root']}"]
        lines += [
            f"{name} ::= {body}"
            for name, body in sorted(self._rules.items())
            if name != "root"
        ]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Primitive rules
# --------------------------------------------------------------------------

def _install_primitives(builder: GrammarBuilder) -> None:
    """Register the JSON scalar rules every grammar needs.

    ``ws`` allows AT MOST ONE whitespace character between tokens. An
    unbounded ``[ \\t\\n]*`` is the classic GBNF footgun: it permits an
    infinite run of spaces, and a constrained model can wander into emitting
    them forever without ever completing the object. Since the output is
    parsed rather than read by a human, minimal whitespace costs nothing and
    saves tokens.
    """
    builder.add("ws", r'[ \t\n]?')

    # Excludes the raw control range and the unescaped quote/backslash, then
    # re-admits them through the escape alternative -- the JSON string
    # production, spelled out.
    builder.add("hex", r"[0-9a-fA-F]")
    builder.add(
        "char",
        r'[^"\\] | "\\" (["\\/bfnrt] | "u" hex hex hex hex)',
    )
    builder.add("string", r'"\"" char* "\""')

    builder.add("integer", r'"-"? ("0" | [1-9] [0-9]*)')
    builder.add(
        "number",
        r'"-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?',
    )
    builder.add("boolean", r'"true" | "false"')
    builder.add("null", r'"null"')

    # Free-form JSON, used when a schema permits arbitrary content
    # (``additionalProperties`` unset, or a bare ``dict`` field).
    builder.add(
        "value",
        "object | array | string | number | boolean | null",
    )
    builder.add(
        "object",
        r'"{" ws (string ws ":" ws value (ws "," ws string ws ":" ws value)*)? ws "}"',
    )
    builder.add("array", r'"[" ws (value (ws "," ws value)*)? ws "]"')


# --------------------------------------------------------------------------
# Schema walking
# --------------------------------------------------------------------------

def _resolve_ref(pointer: str, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``#/...`` JSON pointer against the root schema."""
    if not pointer.startswith("#/"):
        raise GrammarError(
            f"only local $refs are supported, got {pointer!r}. Remote schema "
            "references would make grammar generation depend on the network."
        )
    node: Any = root
    for part in pointer[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise GrammarError(f"$ref {pointer!r} does not resolve")
        node = node[part]
    if not isinstance(node, dict):
        raise GrammarError(f"$ref {pointer!r} does not point at a schema object")
    return node


def _enum_rule(values: list[Any]) -> str:
    """Alternation of JSON literals.

    This is where the closed vocabularies pay off: ``Verdict`` becomes eight
    string terminals, and the sampler is physically unable to invent a ninth.
    """
    if not values:
        raise GrammarError("enum with no values cannot be represented")
    return " | ".join(_json_terminal(value) for value in values)


def _object_rule(
    schema: dict[str, Any],
    root: dict[str, Any],
    builder: GrammarBuilder,
    depth: int,
    hint: str,
) -> str:
    """Build the production for an object schema.

    KEY ORDERING -- AND A BUG WORTH REMEMBERING
    -------------------------------------------
    The obvious construction is "all required properties first, then the
    optional ones". It is wrong, and wrong in a way that only shows up against
    a real model.

    Pydantic emits JSON in FIELD DECLARATION order, not required-first. For
    :class:`~abca.schema.core.Claim` that is ``id, text, span_start, span_end,
    claim_type, ...`` -- where ``span_start`` is optional and ``claim_type`` is
    required. A required-first grammar rejects that ordering, so the sampler
    would be fighting the model's natural output on every single token instead
    of guiding it: worse latency, worse quality, and constant repair loops.

    So properties are emitted in SCHEMA ORDER, with optionality expressed
    positionally through two mutually recursive rule families::

        head-i ::= member-i rest-(i+1)                    # property i is required
        head-i ::= member-i rest-(i+1) | head-(i+1)       # property i is optional
        rest-i ::= "," member-i rest-(i+1)                # required
        rest-i ::= ("," member-i rest-(i+1)) | rest-(i+1) # optional

    ``head`` covers the first member emitted (no leading comma); ``rest``
    covers every subsequent one. A required property is unskippable in both
    families, so it cannot be omitted, while optional ones may be included or
    skipped freely -- in declaration order, matching what the model wants to
    produce anyway.

    The construction is linear in the number of properties, unlike the naive
    "any subset in any order", which is factorial and unusable.

    Objects that permit additional properties fall back to the generic
    ``object`` rule rather than a half-constrained hybrid: a partially
    enforced object is a false sense of security, and the pydantic validator
    is the real gate for those.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    if not properties:
        return "object"

    required = set(schema.get("required") or [])
    keys = list(properties)

    # One named rule per member keeps the emitted grammar readable and lets a
    # repeated structure collapse to a single definition.
    member_rules: dict[str, str] = {}
    for key in keys:
        value_rule = _schema_rule(properties[key], root, builder, depth + 1, f"{hint}-{key}")
        member_rules[key] = builder.add(
            f"{hint}-{key}-kv", f'{_json_terminal(key)} ws ":" ws {value_rule}'
        )

    # Build both families back to front, so each rule can reference the one
    # after it by name.
    rest: list[str] = [""] * (len(keys) + 1)
    head: list[str] = [""] * (len(keys) + 1)
    rest[len(keys)] = ""   # empty tail: nothing left to emit
    head[len(keys)] = ""   # an object may legitimately end with no members

    for index in range(len(keys) - 1, -1, -1):
        key = keys[index]
        member = member_rules[key]
        following_rest = rest[index + 1]
        suffix = f" {following_rest}" if following_rest else ""

        if key in required:
            rest_body = f'"," ws {member}{suffix}'
            head_body = f"{member}{suffix}"
        else:
            skip = following_rest if following_rest else '""'
            rest_body = f'("," ws {member}{suffix}) | {skip}'
            next_head = head[index + 1]
            head_body = f"({member}{suffix}) | " + (next_head if next_head else '""')

        rest[index] = builder.add(f"{hint}-rest-{index}", rest_body)
        head[index] = builder.add(f"{hint}-head-{index}", head_body)

    entry = head[0] if head[0] else '""'
    return builder.add(f"{hint}-obj", f'"{{" ws {entry} ws "}}"')


def _schema_rule(
    schema: dict[str, Any],
    root: dict[str, Any],
    builder: GrammarBuilder,
    depth: int,
    hint: str,
) -> str:
    """Return the name of (or inline expression for) a rule matching ``schema``."""
    if depth > MAX_DEPTH:
        raise GrammarError(
            f"schema nesting exceeded {MAX_DEPTH} levels at {hint!r}; this "
            "usually means a self-referential model."
        )

    if not isinstance(schema, dict):
        raise GrammarError(f"expected a schema object at {hint!r}, got {type(schema).__name__}")

    # -- $ref: resolve once, reuse the rule everywhere it appears.
    if "$ref" in schema:
        pointer = schema["$ref"]
        if pointer in builder._ref_rules:
            return builder._ref_rules[pointer]
        target = _resolve_ref(pointer, root)
        name = pointer.rsplit("/", 1)[-1]
        # Reserve the name BEFORE recursing so a cyclic $ref terminates.
        placeholder = _rule_name(name)
        builder._ref_rules[pointer] = placeholder
        rule = _schema_rule(target, root, builder, depth + 1, name)
        builder._ref_rules[pointer] = rule
        return rule

    # -- allOf wrapping a single $ref: pydantic's shape for an annotated ref.
    all_of = schema.get("allOf")
    if all_of and len(all_of) == 1:
        return _schema_rule(all_of[0], root, builder, depth + 1, hint)

    # -- const / enum: the closed vocabularies.
    if "const" in schema:
        value = schema["const"]
        return builder.add(f"{hint}-const", _enum_rule([value]))
    if "enum" in schema:
        return builder.add(f"{hint}-enum", _enum_rule(list(schema["enum"])))

    # -- anyOf / oneOf: unions, including Optional[X] as anyOf[X, null].
    union = schema.get("anyOf") or schema.get("oneOf")
    if union:
        parts = [
            _schema_rule(option, root, builder, depth + 1, f"{hint}-{index}")
            for index, option in enumerate(union)
        ]
        # Deduplicate while preserving order: Optional[str] | None can produce
        # the same branch twice, and a duplicated alternative is noise.
        seen: list[str] = []
        for part in parts:
            if part not in seen:
                seen.append(part)
        return builder.add(f"{hint}-any", " | ".join(seen)) if len(seen) > 1 else seen[0]

    schema_type = schema.get("type")

    if schema_type == "object":
        return _object_rule(schema, root, builder, depth, hint)

    if schema_type == "array":
        items = schema.get("items")
        if not items:
            return "array"
        item_rule = _schema_rule(items, root, builder, depth + 1, f"{hint}-item")
        min_items = int(schema.get("minItems") or 0)
        if min_items >= 1:
            # At least one element required. Higher minimums are approximated
            # as "one or more" and the exact count is enforced by the
            # validator -- unrolling N copies would bloat the grammar for a
            # constraint the validator already checks.
            body = f'"[" ws {item_rule} (ws "," ws {item_rule})* ws "]"'
        else:
            body = f'"[" ws ({item_rule} (ws "," ws {item_rule})*)? ws "]"'
        return builder.add(f"{hint}-arr", body)

    # -- Scalars. `pattern`, `minLength`, `format` and the numeric bounds are
    #    intentionally ignored here; see the module docstring. They are
    #    enforced by the pydantic validator, which triggers the repair loop.
    if schema_type == "string":
        return "string"
    if schema_type == "integer":
        return "integer"
    if schema_type == "number":
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"

    # No type at all: an unconstrained field. Permit any JSON value rather
    # than failing -- the validator remains the gate.
    return "value"


def json_schema_to_gbnf(schema: dict[str, Any]) -> str:
    """Convert a JSON Schema document into a GBNF grammar.

    The returned grammar's entry point is ``root``. Output is deterministic
    for a given schema, which matters: the grammar is part of the recipe, and
    a grammar that varied between runs would make an otherwise-identical
    recipe produce different constraints.
    """
    if not isinstance(schema, dict):
        raise GrammarError(f"schema must be an object, got {type(schema).__name__}")

    builder = GrammarBuilder()
    _install_primitives(builder)
    root_rule = _schema_rule(schema, schema, builder, 0, schema.get("title", "root"))
    # `root` is registered last so it wins the name and renders first.
    builder._rules["root"] = root_rule
    return builder.render()


def gbnf_for_model(model_cls: type) -> str:
    """Convenience wrapper: grammar for a pydantic model class."""
    schema = model_cls.model_json_schema()
    return json_schema_to_gbnf(schema)


__all__ = ["MAX_DEPTH", "GrammarBuilder", "GrammarError", "gbnf_for_model", "json_schema_to_gbnf"]
