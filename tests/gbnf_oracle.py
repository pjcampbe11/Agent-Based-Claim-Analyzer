"""A tiny GBNF matcher, used ONLY as a test oracle.

WHY THIS EXISTS
===============
:mod:`abca.providers.grammar` emits GBNF that llama.cpp consumes. Asserting on
the emitted TEXT would be testing the formatter, not the grammar -- and the
failure mode that actually costs hours is a grammar that looks right, compiles
cleanly, and quietly constrains the model to something wrong (unquoted keys
being the classic). The only test that catches that is one which asks: does
this grammar accept the JSON we expect, and reject what we expect it to reject?

So this module implements a backtracking recogniser for the GBNF subset the
converter emits. It is deliberately NOT production code:

* no performance work -- inputs in tests are tens of bytes;
* no error recovery -- a malformed grammar raises;
* it recognises, it does not parse -- there is no parse tree.

It is an independent second implementation, which is the point. If the
converter and the oracle agree that ``{"verdict":"SUPPORTED"}`` matches, that
agreement is evidence; a test that merely re-derived the expected string from
the converter's own logic would be evidence of nothing.

SUPPORTED SUBSET
================
``name ::= alternation``; alternation of sequences separated by ``|``;
sequences of terms; terms being rule references, ``"literals"``,
``[char classes]`` (with ranges, negation and ``\\xNN`` / ``\\uNNNN`` escapes),
parenthesised groups, and the ``*``, ``+``, ``?`` postfix operators.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass


class GrammarSyntaxError(ValueError):
    """The oracle could not parse the grammar text."""


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Literal:
    text: str


@dataclass(frozen=True)
class CharClass:
    negated: bool
    ranges: tuple[tuple[str, str], ...]

    def matches(self, char: str) -> bool:
        inside = any(low <= char <= high for low, high in self.ranges)
        return inside != self.negated


@dataclass(frozen=True)
class Sequence:
    items: tuple[object, ...]


@dataclass(frozen=True)
class Alternation:
    options: tuple[object, ...]


@dataclass(frozen=True)
class Repeat:
    item: object
    minimum: int
    maximum: int | None  # None = unbounded


# --------------------------------------------------------------------------
# Grammar text parser
# --------------------------------------------------------------------------

_RULE_LINE = re.compile(r"^([A-Za-z0-9-]+)\s*::=\s*(.*)$")

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "[": "[", "]": "]", "/": "/"}


class _Cursor:
    """Character cursor over one rule body."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str | None:
        return self.text[self.pos] if self.pos < len(self.text) else None

    def take(self) -> str:
        char = self.text[self.pos]
        self.pos += 1
        return char

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def eof(self) -> bool:
        self.skip_ws()
        return self.pos >= len(self.text)


def _unescape(cursor: _Cursor) -> str:
    """Consume one (possibly escaped) character."""
    char = cursor.take()
    if char != "\\":
        return char
    code = cursor.take()
    if code == "x":
        return chr(int(cursor.take() + cursor.take(), 16))
    if code == "u":
        return chr(int("".join(cursor.take() for _ in range(4)), 16))
    return _ESCAPES.get(code, code)


def _parse_char_class(cursor: _Cursor) -> CharClass:
    cursor.take()  # consume '['
    negated = cursor.peek() == "^"
    if negated:
        cursor.take()

    ranges: list[tuple[str, str]] = []
    while True:
        char = cursor.peek()
        if char is None:
            raise GrammarSyntaxError("unterminated character class")
        if char == "]":
            cursor.take()
            break
        low = _unescape(cursor)
        if cursor.peek() == "-" and cursor.text[cursor.pos + 1 : cursor.pos + 2] != "]":
            cursor.take()
            high = _unescape(cursor)
            ranges.append((low, high))
        else:
            ranges.append((low, low))
    return CharClass(negated=negated, ranges=tuple(ranges))


def _parse_literal(cursor: _Cursor) -> Literal:
    cursor.take()  # consume opening quote
    out: list[str] = []
    while True:
        char = cursor.peek()
        if char is None:
            raise GrammarSyntaxError("unterminated literal")
        if char == '"':
            cursor.take()
            break
        out.append(_unescape(cursor))
    return Literal("".join(out))


def _parse_alternation(cursor: _Cursor) -> object:
    options = [_parse_sequence(cursor)]
    while True:
        cursor.skip_ws()
        if cursor.peek() == "|":
            cursor.take()
            options.append(_parse_sequence(cursor))
        else:
            break
    return options[0] if len(options) == 1 else Alternation(tuple(options))


def _parse_sequence(cursor: _Cursor) -> object:
    items: list[object] = []
    while True:
        cursor.skip_ws()
        char = cursor.peek()
        if char is None or char in "|)":
            break
        items.append(_parse_postfix(cursor))
    return Sequence(tuple(items))


def _parse_postfix(cursor: _Cursor) -> object:
    atom = _parse_atom(cursor)
    while True:
        char = cursor.peek()
        if char == "*":
            cursor.take()
            atom = Repeat(atom, 0, None)
        elif char == "+":
            cursor.take()
            atom = Repeat(atom, 1, None)
        elif char == "?":
            cursor.take()
            atom = Repeat(atom, 0, 1)
        else:
            return atom


def _parse_atom(cursor: _Cursor) -> object:
    char = cursor.peek()
    if char == '"':
        return _parse_literal(cursor)
    if char == "[":
        return _parse_char_class(cursor)
    if char == "(":
        cursor.take()
        inner = _parse_alternation(cursor)
        cursor.skip_ws()
        if cursor.peek() != ")":
            raise GrammarSyntaxError(f"expected ')' at {cursor.pos} in {cursor.text!r}")
        cursor.take()
        return inner
    name = ""
    while (char := cursor.peek()) is not None and (char.isalnum() or char == "-"):
        name += cursor.take()
    if not name:
        raise GrammarSyntaxError(f"unexpected character at {cursor.pos} in {cursor.text!r}")
    return Ref(name)


class Grammar:
    """A parsed GBNF grammar that can recognise strings."""

    def __init__(self, text: str) -> None:
        self.rules: dict[str, object] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _RULE_LINE.match(line)
            if not match:
                raise GrammarSyntaxError(f"not a rule: {line!r}")
            name, body = match.group(1), match.group(2)
            cursor = _Cursor(body)
            node = _parse_alternation(cursor)
            if not cursor.eof():
                raise GrammarSyntaxError(f"trailing input in rule {name!r}: {body!r}")
            self.rules[name] = node
        if "root" not in self.rules:
            raise GrammarSyntaxError("grammar has no root rule")

    # ---------------------------------------------------------------- matching

    def _match(self, node: object, text: str, pos: int, depth: int = 0) -> Iterator[int]:
        """Yield every position the node can consume up to, from ``pos``.

        Generators rather than a single return value because the grammar is
        ambiguous in places (``char*`` inside a string, optional tails), so a
        greedy match would produce false negatives. Yielding all possibilities
        makes the recogniser exact for this subset.
        """
        if depth > 200:
            return  # cycle guard; abCA grammars are shallow

        if isinstance(node, Literal):
            if text.startswith(node.text, pos):
                yield pos + len(node.text)
            return

        if isinstance(node, CharClass):
            if pos < len(text) and node.matches(text[pos]):
                yield pos + 1
            return

        if isinstance(node, Ref):
            target = self.rules.get(node.name)
            if target is None:
                raise GrammarSyntaxError(f"undefined rule {node.name!r}")
            yield from self._match(target, text, pos, depth + 1)
            return

        if isinstance(node, Alternation):
            for option in node.options:
                yield from self._match(option, text, pos, depth + 1)
            return

        if isinstance(node, Sequence):
            if not node.items:
                yield pos
                return
            head, *rest = node.items
            tail = Sequence(tuple(rest))
            for after_head in self._match(head, text, pos, depth + 1):
                yield from self._match(tail, text, after_head, depth + 1)
            return

        if isinstance(node, Repeat):
            seen: set[int] = set()

            def expand(current: int, count: int) -> Iterator[int]:
                if count >= node.minimum and current not in seen:
                    seen.add(current)
                    yield current
                if node.maximum is not None and count >= node.maximum:
                    return
                for nxt in self._match(node.item, text, current, depth + 1):
                    if nxt == current:  # zero-width; would loop forever
                        continue
                    yield from expand(nxt, count + 1)

            yield from expand(pos, 0)
            return

        raise GrammarSyntaxError(f"unknown node {node!r}")

    def accepts(self, text: str) -> bool:
        """Whether the grammar matches the ENTIRE string."""
        return any(end == len(text) for end in self._match(self.rules["root"], text, 0))


__all__ = ["Grammar", "GrammarSyntaxError"]
