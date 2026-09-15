"""Deterministic sentence segmentation.

WHY THIS IS NOT A MODEL CALL
============================
Sentence boundaries are the coordinate system everything downstream refers to:
claim spans point into them, the gate operates on them, and a reader auditing a
verdict needs to find the sentence it came from. A model that split sentences
differently between two runs would make identical input produce different spans
-- a `DIVERGENT` verification for no substantive reason.

So this is plain code: same input, same output, always, with no seed and no
backend involved.

WHY NOT spaCy OR nltk
=====================
Both are large dependencies whose model files are versioned separately from the
library, which means the run record would have to pin a model file it does not
control in order for splits to be reproducible. For the actual requirement --
split English prose containing legal citations -- a few hundred lines of
explicit rules is both smaller and more auditable, and every rule below exists
because a specific real input broke without it.

THE HARD CASES, AND WHY THEY MATTER HERE
========================================
This domain is unusually hostile to naive splitting:

* ``10 ILCS 5/10-2.`` -- a statute citation ending in a digit then a period.
* ``52 U.S.C. 30101`` -- internal periods that are not boundaries.
* ``Brown v. Board of Ed.`` -- an abbreviation ending a sentence.
* ``increased 3.5 percent`` -- a decimal.
* ``He said "this is wrong." Then he left.`` -- the boundary is after the quote.
* ``1. First item`` -- an enumerator, not a sentence end.

Splitting ``10 ILCS 5/10-2`` in half would produce two garbage claims and a
citation that resolves to nothing, so these are correctness problems rather
than tidiness ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

#: Tokens that end in a period WITHOUT ending a sentence. Matched
#: case-insensitively against the word immediately before a candidate boundary.
#:
#: Grouped by why each is here, because an unexplained abbreviation list rots:
#: nobody knows whether an entry is load-bearing or was copied from a blog post.

# six times as long and no clearer, and this is a table meant to be read.
# A readable table, not a 60-element list literal: this is data a human has
# to scan and extend, and the literal form would be six times as long.
_ABBREVIATIONS: frozenset[str] = frozenset(
    # Legal citation -- the ones this domain actually hits.
    "u.s u.s.c u.s.c.a c.f.r f.2d f.3d f.supp s.ct l.ed stat pub.l "
    "v vs ex rel et al et seq id ibid cf supra infra "
    "art amend sec secs cl para paras pt ch chs "
    "no nos p pp fn nn "
    "ct cir dist div app "
    "cong reg regs rev proc "
    # Government and organisation names.
    "dept govt admin comm assn assoc bur "
    "inc corp co ltd llc llp plc "
    "fed natl intl "
    # Titles and honorifics.
    "mr mrs ms dr prof rev hon gen col capt lt sgt "
    "jr sr st ste "
    "sen rep gov pres atty "
    # Everyday abbreviations that end in a period mid-sentence.
    "e.g i.e etc vol ed eds trans approx est max min avg "
    "jan feb mar apr jun jul aug sept sep oct nov dec "
    "mon tue wed thu fri sat sun "
    "a.m p.m "
    "fig figs tbl eq "

    # this is data a human scans and extends.
    .split()
)

#: A candidate boundary is terminal punctuation, optionally followed by closing
#: quotes or brackets, then whitespace. Capturing the closers matters: in
#: ``He said "no." Then...`` the sentence ends after the quote, not before it.
_BOUNDARY = re.compile(r'([.!?]+)([")\]\'”’]*)(\s+|$)')

#: A bare enumerator at the start of a line: ``1.`` / ``a)`` / ``iv.``
#: These end in a period and are never sentence boundaries.
_ENUMERATOR = re.compile(r"^\s*(\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,5})[.)]\s*$")

#: The word immediately preceding a candidate boundary, including any internal
#: periods, so ``U.S.C.`` is captured whole rather than as ``C``.
_PRECEDING_TOKEN = re.compile(r"([\w.'’-]+)$", re.UNICODE)

#: Blank line, i.e. a paragraph break. Always a hard boundary.
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

#: A line break followed by a list marker. Also a hard boundary.
#:
#: Without this rule, "1. First claim\n2. Second claim" comes out as ONE
#: sentence: the enumerator veto correctly stops "1." and "2." from ending a
#: sentence, and a single newline is not a paragraph break, so nothing splits
#: them. List items are separate assertions and must become separate claims --
#: people write their strongest political claims in bullet lists.
_LIST_ITEM_BREAK = re.compile(
    r"\n[ \t]*(?=(?:\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,5})[.)]\s|[-*\u2022\u2013]\s)"
)


@dataclass(frozen=True, slots=True)
class Sentence:
    """One sentence with its exact span into the normalized document.

    ``start``/``end`` index the NORMALIZED text -- the same text whose hash is
    recorded as ``DocumentRef.content_hash``. That is what makes a span
    checkable: a reader can slice the document they were given and see the
    sentence the claim came from.
    """

    index: int
    text: str
    start: int
    end: int

    def __len__(self) -> int:
        return self.end - self.start


def _is_abbreviation(text: str, position: int) -> bool:
    """Whether the period at ``position`` belongs to a known abbreviation."""
    match = _PRECEDING_TOKEN.search(text[:position])
    if not match:
        return False
    token = match.group(1).lower().rstrip(".")
    if token in _ABBREVIATIONS:
        return True
    # A single letter followed by a period is an initial: "J. Smith",
    # "Q. Whitfield". Never a boundary on its own.
    return len(token) == 1 and token.isalpha()


def _is_enumerator(text: str, start: int, position: int) -> bool:
    """Whether the period closes a list enumerator rather than a sentence.

    Looks back to the start of the line, so ``1.`` at the head of a list item
    is recognised while ``ending in 1.`` mid-sentence is not.
    """
    line_start = text.rfind("\n", start, position) + 1
    if line_start <= 0:
        line_start = start
    return bool(_ENUMERATOR.match(text[line_start : position + 1]))


def _is_citation_interior(text: str, position: int) -> bool:
    """Whether the period sits inside a statute or reporter citation.

    Catches the shape ``digit . digit`` with no space -- ``F.2d``, ``3.5``,
    ``10.2`` -- which naive splitters mangle. The boundary regex already
    requires whitespace after the period, so this only fires for a period whose
    neighbours are both digits, which is never a sentence end.
    """
    before = text[position - 1] if position > 0 else ""
    after = text[position + 1] if position + 1 < len(text) else ""
    return before.isdigit() and after.isdigit()


def _next_char_starts_a_sentence(text: str, position: int) -> bool:
    """Whether what follows looks like the start of a new sentence.

    A lowercase letter after a period usually means the period was not a
    boundary (``etc. and then``). Quotes, digits, brackets and dashes are all
    legitimate sentence openers, so only a lowercase letter vetoes.
    """
    for index in range(position, len(text)):
        char = text[index]
        if char.isspace():
            continue
        return not char.islower()
    return True  # end of text


def find_boundaries(text: str) -> list[int]:
    """Return the offsets at which ``text`` should be cut into sentences.

    Each offset is the index one past the end of a sentence. Exposed separately
    from :func:`split_sentences` so the rules can be tested directly on the
    positions they produce.
    """
    boundaries: list[int] = []

    # Paragraph breaks are unconditional. A blank line ends a sentence whatever
    # punctuation preceded it -- including none, which is common in social
    # media text where people simply stop.
    for match in _PARAGRAPH_BREAK.finditer(text):
        boundaries.append(match.start())

    # List items: each bullet or numbered item is its own assertion.
    for match in _LIST_ITEM_BREAK.finditer(text):
        boundaries.append(match.start())

    for match in _BOUNDARY.finditer(text):
        punctuation_start = match.start(1)
        # Position of the final punctuation character in the run.
        last_punctuation = match.end(1) - 1

        if _is_citation_interior(text, last_punctuation):
            continue
        # A run like "?!" or "..." is decided by its FIRST character: an
        # ellipsis is not an abbreviation even though "..." ends in a period.
        if match.group(1) == "." and _is_abbreviation(text, punctuation_start):
            continue
        if _is_enumerator(text, 0, last_punctuation):
            continue
        if not _next_char_starts_a_sentence(text, match.end(2)):
            continue

        # Cut AFTER any closing quote or bracket: the punctuation belongs to
        # the sentence it terminates.
        boundaries.append(match.end(2))

    return sorted(set(boundaries))


def split_sentences(text: str, *, min_length: int = 1) -> list[Sentence]:
    """Split ``text`` into sentences with exact spans.

    ``min_length`` drops fragments shorter than this many non-whitespace
    characters. The default of 1 keeps everything except pure whitespace;
    callers that want to skip stray punctuation raise it.

    Spans are exact: ``text[s.start:s.end] == s.text`` holds for every returned
    sentence, and that invariant is asserted in the test suite because
    everything downstream relies on it.
    """
    if not text.strip():
        return []

    cuts = [0, *find_boundaries(text), len(text)]
    sentences: list[Sentence] = []

    for start, end in pairwise(cuts):
        if end <= start:
            continue
        raw = text[start:end]
        stripped = raw.strip()
        if len(stripped) < min_length:
            continue

        # Tighten the span onto the non-whitespace content so the recorded
        # offsets point at the sentence rather than at the gap before it.
        offset = start + (len(raw) - len(raw.lstrip()))
        sentences.append(
            Sentence(
                index=len(sentences),
                text=stripped,
                start=offset,
                end=offset + len(stripped),
            )
        )

    return sentences


def locate(haystack: str, needle: str, *, within: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """Find ``needle`` verbatim in ``haystack``, optionally restricted to a range.

    Used to resolve a claim's quoted substring back to an exact document span.
    Returns ``None`` on any miss rather than guessing at a fuzzy match: an
    approximate span points a reader at text the claim did not come from, which
    is worse than admitting the span is unknown.
    """
    if not needle:
        return None
    start, end = within or (0, len(haystack))
    position = haystack.find(needle, start, end)
    if position == -1:
        return None
    return position, position + len(needle)


__all__ = ["Sentence", "find_boundaries", "locate", "split_sentences"]
