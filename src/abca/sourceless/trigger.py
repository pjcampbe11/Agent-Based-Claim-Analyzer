"""Deciding whether a claim is sourceless, and therefore whether to dissect it.

WHY THIS IS CODE AND NOT A MODEL CALL
=====================================
"Does this text contain a resolvable external reference?" is a question about
character patterns, not about meaning. A URL is a URL. ``10 ILCS 5/10-2`` is a
citation. Asking a model produces a probabilistic answer to a deterministic
question and makes the trigger itself irreproducible -- two runs of the same post
could take different lanes, which would make every downstream comparison
meaningless.

So it is a regex pass over the same citation grammar the retrieval layer already
uses, plus URL and named-publication detection.

THE FAILURE DIRECTION THAT MATTERS
==================================
Two ways to be wrong, and they are NOT symmetric.

* **False sourceless** -- a claim that HAS a reference is sent to dissection.
  Cost: a wasted reconstruction pass. Retrieval then finds the real source
  anyway, because the reference was there all along. Recoverable.

* **False sourced** -- a claim with NO reference is sent straight to retrieval.
  Cost: retrieval has nothing to search for, returns nothing, and the claim
  exits ``UNSUPPORTED`` having never been reconstructed. The reader gets a
  confident-looking dead end, which is the exact output doc 20 s1 says is the
  worst thing the pipeline can produce.

So the detector is TOLERANT: anything that looks like it might be a reference
counts as one. It would rather send a sourceless claim down the sourced path
never -- and a sourced claim down the sourceless path sometimes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ReferenceKindFound(StrEnum):
    """What kind of external reference was detected."""

    URL = "url"
    CITATION = "citation"
    PUBLICATION = "publication"
    QUOTED_DOCUMENT = "quoted_document"


@dataclass(frozen=True, slots=True)
class ExternalReference:
    """One detected reference, with the span that produced it."""

    kind: ReferenceKindFound
    text: str
    start: int
    end: int


#: A bare URL or a bare domain. Deliberately loose -- ``see reuters.com`` is a
#: reference even without a scheme, and treating it as one costs only a wasted
#: pass while missing it costs the reader an answer.
_URL = re.compile(
    r"""(?xi)
    \b(
        https?://\S+
      | www\.[a-z0-9-]+\.[a-z]{2,}\S*
      | [a-z0-9-]+\.(?:com|org|net|gov|edu|mil|int|us|uk|de|io|news)\b/\S*
    )""",
)

#: Legal citation shapes the retrieval layer can already resolve, plus the
#: common congressional identifiers. Kept in sync with sources/citations.py by
#: the test suite rather than by import, because this module must stay usable
#: before the connector registry is constructed.
_CITATION = re.compile(
    r"""(?xi)
      \b\d+\s+U\.?\s?S\.?\s?C\.?\s*(?:§+\s*)?\d+          # 18 U.S.C. 42
    | \b\d+\s+C\.?\s?F\.?\s?R\.?\s*(?:§+\s*)?[\d.]+       # 28 CFR 545.11
    | \b\d+\s+ILCS\s+[\d/.\-]+                            # 10 ILCS 5/10-2
    | \bPub\.?\s?L\.?\s*(?:No\.?)?\s*\d+[-–]\d+      # Pub. L. 117-58
    | \b(?:H\.?\s?R\.?|S\.?)\s?\d{1,5}\b                  # H.R. 1234 / S. 123
    | \bH\.?\s?(?:Res|J\.?\s?Res|Con\.?\s?Res)\.?\s?\d+   # H.Res. 123
    | \b\d+\s+Fed\.?\s?Reg\.?\s+\d+                       # 90 Fed. Reg. 1234
    | \b\d+\s+F\.\s?(?:2d|3d|4th|Supp\.?)\s?\d+           # 410 F.3d 123
    | \b\d+\s+U\.?\s?S\.?\s+\d+\b                         # 410 U.S. 113
    | \bRoll\s+Call\s+(?:No\.?\s*)?\d+                    # Roll Call 315
    """,
)

#: Named publications and official bodies whose mention implies a locatable
#: document. Not exhaustive and not meant to be: it catches the common cases and
#: the URL and citation patterns catch the rest.
_PUBLICATIONS = re.compile(
    r"""(?xi)\b(
        Congressional\s+Record | Federal\s+Register | Congressional\s+Budget\s+Office
      | CBO | GAO | CRS | Joint\s+Committee\s+on\s+Taxation | JCT
      | Bureau\s+of\s+Labor\s+Statistics | BLS | Census\s+Bureau
      | Supreme\s+Court\s+(?:opinion|ruling|decision)
      | committee\s+report | conference\s+report
      | Reuters | Associated\s+Press | \bAP\b | New\s+York\s+Times | Washington\s+Post
      | Wall\s+Street\s+Journal | NPR | BBC | Politico | Axios | ProPublica
    )\b""",
)

#: "according to the <something> report/study/memo/filing" -- a named document
#: without a link is still a reference someone can go find.
_QUOTED_DOCUMENT = re.compile(
    r"""(?xi)\b(?:
        according\s+to\s+(?:the\s+)?[\w\s'-]{3,40}\s+
        (?:report|study|memo|filing|opinion|ruling|analysis|survey|audit)
      | (?:the\s+)?[\w\s'-]{3,40}\s+(?:report|study)\s+(?:found|said|showed|concluded)
    )""",
)

_PATTERNS: tuple[tuple[ReferenceKindFound, re.Pattern[str]], ...] = (
    (ReferenceKindFound.URL, _URL),
    (ReferenceKindFound.CITATION, _CITATION),
    (ReferenceKindFound.PUBLICATION, _PUBLICATIONS),
    (ReferenceKindFound.QUOTED_DOCUMENT, _QUOTED_DOCUMENT),
)


def find_external_references(text: str) -> list[ExternalReference]:
    """Every resolvable external reference in ``text``, in document order.

    Overlapping matches from different patterns are all reported. The caller
    only needs to know whether the list is empty, but the spans are returned so
    a report can show the reader exactly what was treated as a reference.
    """
    found: list[ExternalReference] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            found.append(
                ExternalReference(kind=kind, text=match.group(0).strip(),
                                  start=match.start(), end=match.end())
            )
    return sorted(found, key=lambda r: (r.start, r.kind.value))


def is_sourceless(text: str) -> bool:
    """True when nothing in ``text`` points at a document anyone could fetch."""
    return not find_external_references(text)


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """Whether to dissect, and the reason -- which is published either way."""

    dissect: bool
    reason: str
    references: tuple[ExternalReference, ...] = ()


def should_dissect(
    text: str,
    *,
    claim_type_is_verdict_eligible: bool,
    survived_gate: bool = True,
    image_only: bool = False,
) -> TriggerDecision:
    """Apply doc 18 s3's trigger conditions, in the order they are stated.

    All three must hold: the claim survived the gate, its type is one evidence
    could settle, and it carries no resolvable external reference.

    The reason string is always populated, including on a negative decision. A
    claim that skipped dissection should be able to say why, because "we did not
    reconstruct this" is itself something a reader may want to check.
    """
    if image_only:
        return TriggerDecision(
            False,
            "image_only_content: the assertion lives in an image, and this version "
            "is text-only by decision (doc 18 s2). Gated OUT_OF_SCOPE.",
        )
    if not survived_gate:
        return TriggerDecision(False, "the claim did not survive the relevance gate")
    if not claim_type_is_verdict_eligible:
        return TriggerDecision(
            False,
            "claim type is not verdict-eligible: normative, predictive and "
            "definitional claims route to contract s3 decomposition and are never "
            "reconstructed, because there is no referent to find.",
        )
    references = find_external_references(text)
    if references:
        kinds = sorted({r.kind.value for r in references})
        return TriggerDecision(
            False,
            f"a resolvable external reference is present ({', '.join(kinds)}); "
            "the claim goes to ordinary retrieval",
            tuple(references),
        )
    return TriggerDecision(
        True,
        "no URL, citation, named publication or named document was found, so "
        "retrieval has nothing to search for without reconstruction",
    )


__all__ = [
    "ExternalReference",
    "ReferenceKindFound",
    "TriggerDecision",
    "find_external_references",
    "is_sourceless",
    "should_dissect",
]
