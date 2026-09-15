"""One citation grammar, shared by every connector.

WHY THIS IS A MODULE AND NOT A REGEX PER CONNECTOR
==================================================
Retrieval is deterministic (:mod:`abca.pipeline.retrieve`): the set of sources
consulted is part of the recipe, so it cannot be chosen by a model. What decides
it instead is **what the claim names**. That makes citation parsing the routing
table of the entire evidence system, and a routing table spread across five
connectors is a routing table nobody can read.

So every citation form lives here, each one knowing which connector can serve
it. Adding a source becomes: write the pattern, write the connector, and the
router already works.

THE STRUCTURAL CONSEQUENCE, WHICH IS WORTH STATING PLAINLY
==========================================================
**A connector can only serve a claim that NAMES its source.**

"The unemployment rate was 4.1% in June" names no source. Sending it to a
statistics API means something has to CHOOSE a series -- and the only thing
capable of that choice is a model, which would make retrieval non-deterministic
and let a confident guess fetch a number the author never referred to. The
adjudicator would then quote it faithfully, producing a well-cited answer to a
question nobody asked.

That is why empirical claims still have no route, and it is a property of the
design rather than a gap in it. A claim gets checked when it points at something
checkable. The honest report for the rest is ``no source``, which is what the
tool says.

EVERY PATTERN IS STRICT
=======================
A near-match is not a match. Guessing at a malformed citation fetches the wrong
provision, and the adjudicator quotes the wrong provision faithfully -- the
worst failure available to a tool like this, because the output looks exactly
like a correct one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class CitationKind(StrEnum):
    """Which corpus a citation points into.

    The value doubles as the connector name that serves it, so routing is a
    dictionary lookup rather than a chain of ``isinstance`` checks that would
    have to be edited in two places every time a source is added.
    """

    ILCS = "ilcs"                 # Illinois Compiled Statutes
    CFR = "ecfr"                  # Code of Federal Regulations, via eCFR
    FR_DOCUMENT = "fedreg"        # Federal Register, by document number
    FR_PAGE = "fedreg-page"       # Federal Register, by volume and page
    USC = "usc"                   # United States Code
    CASE = "courtlistener"        # Published court decisions


#: Citation kinds this build can parse but cannot yet fetch.
#:
#: Recorded rather than silently dropped. A claim citing ``52 U.S.C. 30101``
#: should produce "recognised this citation, no connector serves it" -- which is
#: a different and more useful finding than "found no citation at all", and it
#: is the difference between a coverage gap somebody can act on and one nobody
#: can see.
#:
#: **U.S.C.**: uscode.house.gov renders its section text client-side, so the
#: initial HTML carries navigation and no statute; govinfo has the text but its
#: URLs embed a subtitle/chapter/subchapter path that cannot be derived from a
#: bare citation. Serving it needs a bulk index or an API key, and scraping a
#: JS-rendered page for primary legal text is exactly the shortcut this project
#: should not take.
#:
#: **Federal Register by volume and page** (``91 FR 56737``): the API exposes no
#: citation condition, a full-text search for the citation string returns
#: nothing because the string does not appear in the document that carries it,
#: and the site's /citation/ redirect is behind a bot wall. The DOCUMENT NUMBER
#: form (``FR Doc. 2026-18141``) is served -- see :mod:`abca.sources.fedreg`.
#:
#: **Court decisions** (``576 U.S. 644``): CourtListener's API is the obvious
#: source and throttles anonymous callers to 125 requests a day. A connector on
#: it would work for the first few claims of a session and then stop, for
#: reasons having nothing to do with the claims -- which is worse than no
#: connector, because a run would sometimes have evidence and sometimes not and
#: the difference would look like a finding. Serving it needs an API token
#: handled as a credential, like the hosted model keys in :mod:`abca.config`.
NO_CONNECTOR_YET: frozenset[CitationKind] = frozenset({
    CitationKind.USC,
    CitationKind.FR_PAGE,
    CitationKind.CASE,
})


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """A citation found in a claim, with the connector that can serve it."""

    kind: CitationKind
    #: Canonical text form, and the locator passed to the connector.
    locator: str
    #: Where in the claim text it appeared. For reporting, never for fetching.
    start: int = 0
    end: int = 0

    def __str__(self) -> str:
        return self.locator

    @property
    def connector(self) -> str:
        return self.kind.value

    @property
    def servable(self) -> bool:
        return self.kind not in NO_CONNECTOR_YET


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

#: ``10 ILCS 5/10-2``, ``5 ILCS 140/1``, ``20 ILCS 3960/4.5``.
#:
#: The section admits letters, dots and dashes -- real citations look like
#: ``1-101``, ``4.5``, ``9A-1`` -- but must NOT end on a dot or dash. A citation
#: at the end of a sentence is followed by a period, and a greedy trailing dot
#: produces the section ``10-2.``, which encodes to a filename that 404s on
#: every request.
ILCS_RE = re.compile(
    r"""
    \b(?P<chapter>\d{1,4})
    \s+ILCS\s+
    (?P<act>\d{1,4}(?:\.\d)?)
    \s*/\s*
    (?P<section>[0-9](?:[0-9A-Za-z.\-]*[0-9A-Za-z])?)
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: ``11 CFR 100.5``, ``29 C.F.R. § 1910.1200``, ``40 CFR Part 60``.
#:
#: The part number is derived from the section (``100.5`` -> part ``100``), so
#: a bare part citation and a section citation both resolve.
CFR_RE = re.compile(
    r"""
    \b(?P<title>\d{1,2})
    \s*C\.?\s?F\.?\s?R\.?\s*
    (?:§+\s*)?
    (?:[Pp]art\s+)?
    (?P<section>\d{1,4}(?:\.\d{1,4})?)
    (?P<subsection>(?:\([0-9a-zA-Z]{1,4}\))*)
    """,
    re.VERBOSE,
)

#: ``89 FR 12345``, ``91 Fed. Reg. 56737`` -- volume and page.
FEDERAL_REGISTER_RE = re.compile(
    r"\b(?P<volume>\d{2,3})\s*(?:FR|Fed\.?\s?Reg\.?)\s*(?P<page>\d{1,6})\b",
    re.IGNORECASE,
)

#: ``FR Doc. 2026-18141``, ``FR Doc No: 2026-18141`` -- the form the Register
#: itself prints at the foot of every document, and the only form that resolves.
#:
#: The ``FR Doc`` prefix is REQUIRED. A bare ``2026-18141`` is indistinguishable
#: from a date range, a case number or a page span, and fetching a rule because
#: a sentence contained two hyphenated numbers is exactly the confident-wrong
#: retrieval this grammar exists to prevent.
FR_DOCUMENT_RE = re.compile(
    r"\bFR\s*Doc\.?\s*(?:No\.?:?\s*)?(?P<number>(?:19|20)\d{2}-\d{3,6})\b",
    re.IGNORECASE,
)

#: ``576 U.S. 644``, ``410 U.S. 113``, ``558 F.3d 1082``, ``135 S. Ct. 2584``.
#:
#: Anchored on a reporter abbreviation, so a bare "page 644" cannot match. The
#: volume is bounded at three digits because no reporter in use has reached
#: four, and an unbounded number would match a year followed by a citation-like
#: fragment.
CASE_RE = re.compile(
    r"""
    \b(?P<volume>\d{1,3})
    \s+(?P<reporter>U\.?\s?S\.?|S\.?\s?Ct\.?|L\.?\s?Ed\.?(?:\s?2d)?
        |F\.?\s?(?:2d|3d|4th)|F\.?\s?Supp\.?(?:\s?[23]d)?)
    \s+(?P<page>\d{1,4})\b
    """,
    re.VERBOSE,
)

#: ``52 U.S.C. 30101``, ``52 USC § 30101``, ``18 U.S.C. §1030(a)(2)``.
USC_RE = re.compile(
    r"""
    \b(?P<title>\d{1,2})
    \s*U\.?\s?S\.?\s?C\.?\s*
    (?:§+\s*)?
    (?P<section>\d{1,5}[a-zA-Z]?(?:-\d{1,4})?)
    (?P<subsection>(?:\([0-9a-zA-Z]{1,4}\))*)
    """,
    re.VERBOSE,
)


def _ilcs(match: re.Match[str]) -> str:
    return (
        f"{int(match.group('chapter'))} ILCS "
        f"{match.group('act')}/{match.group('section')}"
    )


def _cfr(match: re.Match[str]) -> str:
    # Subsections are dropped from the LOCATOR because eCFR serves whole
    # sections; they stay in the claim text, where the adjudicator can see them.
    # Keeping them here would make "11 CFR 100.5" and "11 CFR 100.5(a)" two
    # cache entries for one document.
    return f"{int(match.group('title'))} CFR {match.group('section')}"


def _federal_register(match: re.Match[str]) -> str:
    return f"{int(match.group('volume'))} FR {int(match.group('page'))}"


def _fr_document(match: re.Match[str]) -> str:
    return f"FR Doc. {match.group('number')}"


def _usc(match: re.Match[str]) -> str:
    return f"{int(match.group('title'))} U.S.C. {match.group('section')}"


def _case(match: re.Match[str]) -> str:
    reporter = re.sub(r"\s+", " ", match.group("reporter")).strip()
    return f"{int(match.group('volume'))} {reporter} {int(match.group('page'))}"


#: Pattern -> (kind, canonicalizer). Order matters only for reporting; the
#: patterns themselves do not overlap, because each anchors on a distinct corpus
#: token (ILCS / CFR / FR / USC).
_PATTERNS: tuple[tuple[CitationKind, re.Pattern[str], object], ...] = (
    (CitationKind.ILCS, ILCS_RE, _ilcs),
    (CitationKind.CFR, CFR_RE, _cfr),
    (CitationKind.FR_DOCUMENT, FR_DOCUMENT_RE, _fr_document),
    (CitationKind.FR_PAGE, FEDERAL_REGISTER_RE, _federal_register),
    (CitationKind.USC, USC_RE, _usc),
    (CitationKind.CASE, CASE_RE, _case),
)

#: Hard cap on citations taken from one claim. A claim naming nine statutes is
#: almost always rhetorical rather than substantive, and each one costs a
#: request to somebody else's server plus context in the adjudication prompt.
DEFAULT_LIMIT = 10


def find_citations(text: str, *, limit: int = DEFAULT_LIMIT) -> list[SourceCitation]:
    """Every distinct citation in ``text``, in order of appearance.

    Deduplicated by locator, so a claim that names the same section three times
    fetches it once. Sorted by position rather than by pattern, so the order
    matches the order a reader encounters them.
    """
    found: list[SourceCitation] = []
    seen: set[str] = set()

    matches: list[tuple[int, CitationKind, str, int]] = []
    for kind, pattern, canonical in _PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), kind, canonical(match), match.end()))

    for start, kind, locator, end in sorted(matches, key=lambda item: item[0]):
        if locator in seen:
            continue
        seen.add(locator)
        found.append(SourceCitation(kind=kind, locator=locator, start=start, end=end))
        if len(found) >= limit:
            break
    return found


def parse_citation(text: str, *, kind: CitationKind | None = None) -> SourceCitation:
    """Parse the FIRST citation in ``text``, optionally of a given kind.

    Raises :class:`ValueError` when there is none, rather than returning a
    best guess. See the module docstring: a guessed citation fetches the wrong
    provision, and the wrong provision is then quoted faithfully.
    """
    for citation in find_citations(text, limit=DEFAULT_LIMIT):
        if kind is None or citation.kind is kind:
            return citation
    expected = f" of kind {kind.value}" if kind else ""
    raise ValueError(
        f"no citation{expected} found in {text[:80]!r}. Recognised forms: "
        "'10 ILCS 5/10-2', '11 CFR 100.5', 'FR Doc. 2026-18141', '89 FR 12345', "
        "'52 U.S.C. 30101', '576 U.S. 644'."
    )


__all__ = [
    "CASE_RE",
    "CFR_RE",
    "DEFAULT_LIMIT",
    "FEDERAL_REGISTER_RE",
    "FR_DOCUMENT_RE",
    "ILCS_RE",
    "NO_CONNECTOR_YET",
    "USC_RE",
    "CitationKind",
    "SourceCitation",
    "find_citations",
    "parse_citation",
]
