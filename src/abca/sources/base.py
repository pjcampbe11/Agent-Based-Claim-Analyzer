"""Source connectors: where evidence comes from, and who decides its tier.

THE SINGLE MOST IMPORTANT RULE IN THIS PACKAGE
==============================================
**Tier is a property of the connector. A model can never set it, raise it, or
argue for it.**

:class:`~abca.schema.enums.SourceTier` decides whether a claim may be
adjudicated ``SUPPORTED`` at all (contract s2). If a model could assign tiers,
it could promote a blog post to primary law by being confident about it, and
every downstream gate would fall over silently. So the tier is declared once,
as a class attribute on the connector, and the citation-building code reads it
from there. There is no code path that accepts a tier from model output.

WHAT A CONNECTOR IS
===================
Something that turns a query into documents, with provenance. It knows:

* its ``tier`` -- fixed, declared, not negotiable;
* its ``name`` -- recorded on every citation, so a reader can see which
  connector vouched for a source;
* how to fetch a document and hash exactly what it fetched.

It does NOT know what a claim is, what a verdict is, or which model is asking.
Retrieval is a lookup service; the pipeline decides what to do with what comes
back.

CONTENT HASHING AND DRIFT
=========================
Every retrieved document carries the SHA-256 of the text as retrieved, plus the
timestamp. That pair is what makes ``DRIFTED`` detectable: on replay, the same
URL is fetched again and the hashes compared. A statute that has been amended
since a verdict was published does not make that verdict dishonest -- but it
does mean a human needs to look again, which is why drift is reported rather
than swallowed.

The hash is of the EXTRACTED TEXT, not the raw HTML. Sites change their
markup, navigation and analytics constantly while the law they publish stays
identical; hashing the raw bytes would report drift every time a footer
changed, and an alarm that fires constantly is an alarm nobody reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from abca.canonical import digest_text
from abca.schema.core import Citation
from abca.schema.enums import SourceTier


class ConnectorError(Exception):
    """Base class for connector failures."""

    def __init__(self, message: str, *, connector: str = "unknown") -> None:
        self.connector = connector
        super().__init__(f"[{connector}] {message}")


class SourceNotFound(ConnectorError):
    """The requested document does not exist at this source.

    Distinct from :class:`SourceUnavailable`: a citation that does not resolve
    is a FINDING about the claim ("this statute section does not exist"),
    whereas an unreachable server is a problem with the run.
    """


class SourceUnavailable(ConnectorError):
    """The source could not be reached. A problem with the run, not the claim."""


class InvalidCitation(ConnectorError):
    """The citation could not be parsed into something this connector can fetch."""


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    """One document, with everything needed to cite and later re-verify it.

    Frozen: a retrieved document is evidence. Code that needed to "adjust" one
    after retrieval would be code that changes what the source said.
    """

    #: Stable id within one run, e.g. ``s-001``. This is the ONLY handle a
    #: model is given for a source. It cannot invent a URL or a tier because it
    #: never sees a field it could put one in.
    id: str
    tier: SourceTier
    title: str
    url: str
    text: str
    content_hash: str
    retrieved_at: datetime
    connector: str
    #: Connector-specific provenance: statute citation, docket number, series id.
    locator: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def can_support_verdict(self) -> bool:
        """Whether a citation to this document can carry SUPPORTED/CONTRADICTED."""
        return self.tier.can_support_verdict

    def contains(self, quote: str) -> bool:
        """Whether ``quote`` appears in this document, ignoring whitespace runs.

        Whitespace is normalized on both sides before comparison. Statute text
        is full of hard line wraps and indentation that a model will not
        reproduce byte-for-byte when quoting, and rejecting a correct quote
        over a line break would push the adjudicator toward citing nothing --
        the opposite of what the evidence gate is for.

        Nothing else is relaxed. Different words, different numbers, or a
        dropped "not" all fail, which is the whole point.

        An empty or whitespace-only quote NEVER verifies. Python's ``in``
        reports the empty string as present in every string, so without this
        guard a citation with a blank quote would pass verification against any
        source at all -- and the schema's ``min_length`` on the wire model is
        not enough, because this method is public and is the check every future
        connector will rely on.
        """
        squashed = _squash(quote)
        if not squashed:
            return False
        return squashed in _squash(self.text)

    def locate(self, quote: str) -> str | None:
        """Return the document's own wording for ``quote``, or None if absent.

        Used so the stored citation carries the SOURCE's text rather than the
        model's paraphrase of it. A citation quote is supposed to reproduce
        what the source says; storing the model's whitespace-mangled version
        would make the quote fail to match on a later verification.
        """
        squashed_quote = _squash(quote)
        if not squashed_quote:
            return None

        # Walk the original text, tracking where each squashed character came
        # from, so the matched region can be sliced out with its real wording.
        positions: list[int] = []
        squashed_chars: list[str] = []
        previous_was_space = False
        for index, char in enumerate(self.text):
            if char.isspace():
                if not previous_was_space and squashed_chars:
                    squashed_chars.append(" ")
                    positions.append(index)
                previous_was_space = True
                continue
            previous_was_space = False
            squashed_chars.append(char)
            positions.append(index)

        squashed_text = "".join(squashed_chars)
        offset = squashed_text.find(squashed_quote)
        if offset == -1:
            return None

        start = positions[offset]
        end_index = offset + len(squashed_quote) - 1
        end = positions[end_index] + 1
        return self.text[start:end]

    def to_citation(self, quote: str, *, locator: str | None = None) -> Citation:
        """Build a :class:`Citation` for this document.

        THE TIER COMES FROM THIS DOCUMENT, WHICH GOT IT FROM THE CONNECTOR.
        There is deliberately no parameter for it. A caller cannot pass one in,
        so no amount of model output can influence what tier a citation claims.
        """
        return Citation(
            tier=self.tier,
            title=self.title,
            url=self.url,
            quote=quote,
            retrieved_at=self.retrieved_at,
            content_hash=self.content_hash,
            locator=locator or self.locator,
        )


def _squash(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip the ends."""
    return " ".join(text.split())


def build_document(
    *,
    connector: Connector,
    doc_id: str,
    title: str,
    url: str,
    text: str,
    locator: str | None = None,
    retrieved_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> RetrievedDocument:
    """Construct a :class:`RetrievedDocument`, taking the tier from ``connector``.

    The single chokepoint through which every document is created. Keeping it a
    function rather than letting connectors build documents themselves means
    the tier assignment is written once and can be audited in one place.
    """
    return RetrievedDocument(
        id=doc_id,
        tier=connector.tier,
        title=title,
        url=url,
        text=text,
        content_hash=digest_text(text),
        retrieved_at=retrieved_at or datetime.now(UTC),
        connector=connector.name,
        locator=locator,
        metadata=metadata or {},
    )


@runtime_checkable
class Connector(Protocol):
    """What every source must offer.

    Deliberately small. A connector fetches and identifies; ranking, batching
    and budget live in the retrieval stage, so a new source can be added
    without touching pipeline logic.
    """

    #: Short id recorded on every citation this connector produces.
    name: str

    #: Evidence tier. A CLASS attribute, fixed at definition. See the module
    #: docstring for why this must never be data a model can reach.
    tier: SourceTier

    def fetch(self, locator: str) -> RetrievedDocument:
        """Fetch one document by its native identifier.

        For a statute connector that is a citation ("10 ILCS 5/10-2"); for a
        docket connector, a case number. Raises :class:`SourceNotFound` when it
        does not exist and :class:`SourceUnavailable` when the source is
        unreachable -- the caller treats those very differently.
        """
        ...

    def search(self, query: str, *, limit: int = 5) -> list[RetrievedDocument]:
        """Find documents matching a free-text query.

        May return an empty list. A connector with no search capability returns
        empty rather than raising, so the retrieval stage can treat "no
        keyword search here" and "no results" the same way.
        """
        ...


__all__ = [
    "Connector",
    "ConnectorError",
    "InvalidCitation",
    "RetrievedDocument",
    "SourceNotFound",
    "SourceUnavailable",
    "build_document",
]
