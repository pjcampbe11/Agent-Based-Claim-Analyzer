"""Illinois Compiled Statutes connector. Tier T0 -- primary legal text.

WHY ILCS IS THE FIRST CONNECTOR
===============================
It is the smallest real T0 corpus that is also directly useful: Illinois
ballot-access law is short, public, and argued about constantly, so it is a
statute worth being able to quote exactly. Building
retrieval against a source whose answers can be checked by hand keeps the
adjudicator honest while it is being developed.

THE URL SCHEME, WHICH IS NOT DOCUMENTED ANYWHERE
================================================
ILGA encodes a citation into a filename. Verified against the live site:

===================  ==========================
citation             file
===================  ==========================
``10 ILCS 5/10-2``   ``001000050K10-2.htm``
``10 ILCS 5/10-6``   ``001000050K10-6.htm``
``5 ILCS 140/1``     ``000501400K1.htm``
===================  ==========================

Decoded: the chapter zero-padded to four digits, then the act as four digits
plus one decimal digit (so act ``5`` becomes ``00050`` and act ``140`` becomes
``01400``), then ``K``, then the section verbatim.

The decimal digit is not decorative -- ILCS act numbers genuinely can carry
one. Treating the act as a plain five-digit zero-pad would produce ``00005``
for act 5 and 404 on every request.

TIER IS FIXED AT THE CLASS LEVEL
================================
``tier = SourceTier.T0``. It is a class attribute, not a parameter and not a
field a model can populate. See :mod:`abca.sources.base` for why that matters
more than anything else in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from abca.providers.base import ProviderTimeout
from abca.providers.transport import HttpTransport, TransportError
from abca.schema.enums import SourceTier
from abca.sources.base import (
    InvalidCitation,
    RetrievedDocument,
    SourceNotFound,
    SourceUnavailable,
    build_document,
)
from abca.sources.cache import SourceCache
from abca.sources.citations import ILCS_RE
from abca.sources.text import extract_title, html_to_text

BASE_URL = "https://www.ilga.gov"
DOCUMENT_PATH = "/documents/legislation/ilcs/documents"

#: The ILCS pattern now lives in :mod:`abca.sources.citations`, with every other
#: citation form, because citation parsing IS the routing table of the evidence
#: system and a routing table spread across five connectors is one nobody can
#: read. Re-exported here so existing callers and tests keep working.
CITATION_RE = ILCS_RE


@dataclass(frozen=True, slots=True)
class ILCSCitation:
    """A parsed ILCS citation."""

    chapter: int
    act: str
    section: str

    def __str__(self) -> str:
        return f"{self.chapter} ILCS {self.act}/{self.section}"

    @property
    def filename(self) -> str:
        """The ILGA filename for this citation. See the module docstring."""
        if "." in self.act:
            whole, decimal = self.act.split(".", 1)
            decimal = decimal[:1] or "0"
        else:
            whole, decimal = self.act, "0"
        return f"{self.chapter:04d}{int(whole):04d}{decimal}K{self.section}.htm"

    @property
    def url(self) -> str:
        return f"{BASE_URL}{DOCUMENT_PATH}/{quote(self.filename)}"


def parse_citation(text: str) -> ILCSCitation:
    """Parse the FIRST ILCS citation in ``text``.

    Raises :class:`InvalidCitation` when there is none. Deliberately strict:
    guessing at a malformed citation would fetch the wrong statute and the
    adjudicator would then quote it faithfully, producing a confident,
    well-cited, wrong answer -- the worst possible failure for this tool.
    """
    match = CITATION_RE.search(text)
    if not match:
        raise InvalidCitation(
            f"no ILCS citation found in {text[:80]!r}; expected the form "
            "'10 ILCS 5/10-2'",
            connector="ilcs",
        )
    return ILCSCitation(
        chapter=int(match.group("chapter")),
        act=match.group("act"),
        section=match.group("section"),
    )


def find_citations(text: str, *, limit: int = 10) -> list[ILCSCitation]:
    """Find every distinct ILCS citation in ``text``, in order of appearance."""
    seen: list[ILCSCitation] = []
    for match in CITATION_RE.finditer(text):
        citation = ILCSCitation(
            chapter=int(match.group("chapter")),
            act=match.group("act"),
            section=match.group("section"),
        )
        if citation not in seen:
            seen.append(citation)
        if len(seen) >= limit:
            break
    return seen


class ILCSConnector:
    """Fetches Illinois statute sections from ilga.gov."""

    name = "ilcs"
    #: PRIMARY LEGAL TEXT. Fixed here, at class definition. Not a parameter.
    tier = SourceTier.T0

    def __init__(
        self,
        *,
        cache: SourceCache | None = None,
        transport: HttpTransport | None = None,
        offline: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.cache = cache if cache is not None else SourceCache()
        self.offline = offline
        self._transport = transport or HttpTransport(
            BASE_URL, timeout=timeout, provider_name="ilcs",
            # ILGA serves HTML, not JSON. The transport's JSON helpers are
            # unusable here, so raw fetching goes through _fetch_html below.
            headers={"User-Agent": "abca/0.1 (+https://github.com/pjcampbe11/abca)"},
        )
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"s-{self._counter:03d}"

    def _fetch_html(self, path: str) -> str:
        """GET a page and return its body as text.

        Goes through :meth:`HttpTransport.get_text` rather than reaching into
        the transport's connection, so this fetch gets the same timeout
        handling and the same stale-keep-alive retry as every other network
        call in the tool. It used to poke at the private members, which meant
        the one code path that hits somebody else's server was the one path
        bypassing that handling.

        Transport failures are translated into connector failures, because the
        caller treats "this section does not exist" (a finding about the claim)
        very differently from "the server is unreachable" (a problem with the
        run).
        """
        try:
            response = self._transport.get_text(path, accept="text/html,*/*")
        except ProviderTimeout as exc:
            raise SourceUnavailable(f"timed out fetching {path}", connector=self.name) from exc
        except TransportError as exc:
            raise SourceUnavailable(f"{exc}", connector=self.name) from exc

        if response.status == 404:
            raise SourceNotFound(f"no such section at {path}", connector=self.name)
        if response.status >= 400:
            raise SourceUnavailable(
                f"HTTP {response.status} for {path}", connector=self.name
            )
        return response.text

    def locator_for_url(self, url: str) -> str | None:
        """Turn one of this connector's URLs back into a citation.

        The inverse of :attr:`ILCSCitation.filename`. Needed by the drift probe,
        which holds a run record's URLs -- the URL is what a reader can click,
        and the locator is an implementation detail of one connector -- and has
        to re-fetch through the connector that produced them so the same
        extraction runs and the hashes are comparable.
        """
        if "ilga.gov" not in url.lower():
            return None
        filename = url.rsplit("/", 1)[-1].removesuffix(".htm")
        if "K" not in filename:
            return None
        prefix, section = filename.split("K", 1)
        if len(prefix) != 9 or not prefix.isdigit() or not section:
            return None
        chapter = int(prefix[:4])
        whole = int(prefix[4:8])
        decimal = prefix[8]
        act = f"{whole}.{decimal}" if decimal != "0" else str(whole)
        return f"{chapter} ILCS {act}/{section}"

    def fetch(self, locator: str) -> RetrievedDocument:
        """Fetch one statute section by citation.

        Cache first, then network. In ``offline`` mode a miss raises rather
        than silently returning nothing, so a run that could not consult a
        source says so instead of quietly adjudicating without it.
        """
        citation = parse_citation(locator)
        key = str(citation)

        cached = self.cache.get(self.name, key)
        if cached is not None and (self.offline or not cached.stale):
            return cached.document

        if self.offline:
            raise SourceUnavailable(
                f"{key} is not cached and --offline was requested. Run once "
                "with network access to populate the cache.",
                connector=self.name,
            )

        try:
            raw = self._fetch_html(f"{DOCUMENT_PATH}/{quote(citation.filename)}")
        except SourceUnavailable:
            # A stale cached copy beats no evidence: the caller is told it is
            # stale, and old evidence that announces its age is still evidence.
            if cached is not None:
                return cached.document
            raise

        text = html_to_text(raw)
        if not text.strip():
            raise SourceNotFound(f"{key} returned an empty page", connector=self.name)

        document = build_document(
            connector=self,
            doc_id=self._next_id(),
            title=extract_title(raw) or key,
            url=citation.url,
            text=text,
            locator=key,
            metadata={
                "chapter": citation.chapter,
                "act": citation.act,
                "section": citation.section,
            },
        )
        self.cache.put(document)
        return document

    def search(self, query: str, *, limit: int = 5) -> list[RetrievedDocument]:
        """Resolve any ILCS citations appearing in ``query``.

        This connector does NOT do keyword search over the statute corpus.
        ILGA offers no search API, and scraping its search UI would produce
        results whose ranking this tool cannot explain -- which is exactly the
        kind of unexplainable step that should not sit between a claim and a
        verdict.

        Citation lookup is the honest capability, and it is the one that
        matters: statutes are found by number, not by vibe. Keyword retrieval
        over a locally indexed bulk corpus is a later step; returning an empty
        list here is a truthful "this connector cannot answer that".
        """
        documents: list[RetrievedDocument] = []
        for citation in find_citations(query, limit=limit):
            try:
                documents.append(self.fetch(str(citation)))
            except (SourceNotFound, SourceUnavailable, InvalidCitation):
                # A citation that does not resolve is a finding about the
                # claim, surfaced by the retrieval stage. It must not abort
                # the search for the other citations in the same claim.
                continue
        return documents


__all__ = [
    "BASE_URL",
    "CITATION_RE",
    "DOCUMENT_PATH",
    "ILCSCitation",
    "ILCSConnector",
    "find_citations",
    "parse_citation",
]
