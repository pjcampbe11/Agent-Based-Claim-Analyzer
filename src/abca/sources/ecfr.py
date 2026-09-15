"""Code of Federal Regulations, via the eCFR API. Tier T0 -- primary legal text.

WHY eCFR AND NOT A SCRAPE
=========================
eCFR is the National Archives' official electronic CFR, and it publishes a
documented versioner API that returns a section as XML. That matters more here
than convenience: the text this connector returns is the text the government
publishes, retrieved through the interface it publishes it through, and the
content hash therefore means something. A connector that scraped a rendered
page would be hashing somebody's stylesheet along with the regulation.

THE DATE IS PART OF THE CITATION
================================
The CFR changes. ``/full/{date}/title-{n}.xml`` asks for the title **as it
stood on that date**, which is exactly the property this project needs: a
verdict is only valid against the snapshot it cited, and a replay must be able
to ask for the same snapshot rather than whatever is current today.

So the retrieved document records the date it asked for, the URL carries it,
and a replay of an old run re-fetches the same date and gets the same bytes.
A connector pinned to "today" would report DRIFTED every time a rule changed
anywhere in the title, which is an alarm that fires constantly and therefore an
alarm nobody reads.

COMPRESSION IS MANDATORY, NOT AN OPTIMISATION
=============================================
The versioner endpoint refuses an uncompressed request outright:

    {"message": "This endpoint requires response compression."}

Handled in :meth:`~abca.providers.transport.HttpTransport.get_text`, which asks
for gzip and decompresses with a cap applied DURING decompression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

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
from abca.sources.citations import CFR_RE
from abca.sources.text import html_to_text

BASE_URL = "https://www.ecfr.gov"
API_PATH = "/api/versioner/v1/full"
TITLES_PATH = "/api/versioner/v1/titles.json"
#: Human-readable page for the same section. This is the URL recorded on the
#: citation, because it is the one a reader can open -- the dated API URL that
#: was actually fetched is kept in the document's metadata.
READER_PATH = "/current/title-{title}/section-{section}"

_READER_RE = re.compile(r"/title-(?P<title>\d{1,2})/section-(?P<section>[\d.]+)")

#: XML elements whose text is regulation content rather than markup.
_XML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class CFRCitation:
    """A parsed CFR citation: title, section, and the part it lives in."""

    title: int
    section: str

    def __str__(self) -> str:
        return f"{self.title} CFR {self.section}"

    @property
    def part(self) -> str:
        """The part number, derived from the section.

        ``100.5`` lives in part ``100``. A bare part citation (``40 CFR Part
        60``) has no dot and is its own part. The API needs both, and deriving
        the part rather than asking for it means a citation a person would
        actually write is enough.
        """
        return self.section.split(".", 1)[0]

    def api_path(self, on: date) -> str:
        return (
            f"{API_PATH}/{on.isoformat()}/title-{self.title}.xml"
            f"?part={self.part}&section={self.section}"
        )

    @property
    def url(self) -> str:
        return BASE_URL + READER_PATH.format(title=self.title, section=self.section)


def parse_citation(text: str) -> CFRCitation:
    """Parse the FIRST CFR citation in ``text``.

    Strict by design. A guessed citation fetches the wrong regulation, and the
    adjudicator then quotes the wrong regulation faithfully -- an answer that
    looks exactly like a correct one.
    """
    match = CFR_RE.search(text)
    if not match:
        raise InvalidCitation(
            f"no CFR citation found in {text[:80]!r}; expected the form "
            "'11 CFR 100.5'",
            connector="ecfr",
        )
    return CFRCitation(title=int(match.group("title")), section=match.group("section"))


def xml_to_text(raw_xml: str) -> str:
    """Flatten eCFR section XML to plain text.

    eCFR XML is a shallow, well-formed structure of ``HEAD``/``P``/``I``
    elements, so stripping tags after inserting block breaks is lossless for
    the text -- unlike an HTML page, where dropping the wrong container loses
    content. Reuses :func:`~abca.sources.text.html_to_text` for entity
    unescaping and whitespace collapse rather than re-implementing them, so
    both corpora normalize identically and their hashes are comparable.
    """
    text = re.sub(r"(?i)</(HEAD|P|DIV\d|SECTION)>", "\n\n", raw_xml)
    text = re.sub(r"(?i)<(HEAD|P|DIV\d|SECTION)\b[^>]*>", "\n\n", text)
    return html_to_text(text)


class ECFRConnector:
    """Fetches CFR sections from the eCFR versioner API."""

    name = "ecfr"
    #: PRIMARY LEGAL TEXT. Fixed here, at class definition. Not a parameter.
    #: See abca.sources.base for why this must never be data a model can reach.
    tier = SourceTier.T0

    def __init__(
        self,
        *,
        cache: SourceCache | None = None,
        transport: HttpTransport | None = None,
        offline: bool = False,
        timeout: float = 30.0,
        on: date | None = None,
    ) -> None:
        self.cache = cache if cache is not None else SourceCache()
        self.offline = offline
        #: An explicitly requested CFR snapshot date, or None to use whatever
        #: the API reports as the title's latest issue. Set explicitly by a
        #: replay; left None by a fresh run.
        self.on = on
        self._transport = transport or HttpTransport(
            BASE_URL, timeout=timeout, provider_name="ecfr",
            headers={"User-Agent": "abca/0.1 (+https://github.com/pjcampbe11/abca)"},
        )
        self._counter = 0
        #: title number -> latest issue date, resolved once per process.
        self._issue_dates: dict[int, date] = {}

    def issue_date(self, title: int) -> date:
        """The snapshot date to request for ``title``.

        Asked of the API rather than assumed to be today, because eCFR only
        serves dates it has actually issued: a title amended in February may
        have a latest issue of June, and requesting today 404s. Guessing a date
        would make a perfectly valid citation look like a nonexistent section.

        Cached per process, so a run citing six sections of one title asks once.
        """
        if self.on is not None:
            return self.on
        if title in self._issue_dates:
            return self._issue_dates[title]

        try:
            payload = self._transport.get_json(TITLES_PATH)
        except (ProviderTimeout, TransportError) as exc:
            raise SourceUnavailable(
                f"could not resolve the current CFR issue date: {exc}", connector=self.name
            ) from exc

        for entry in payload.get("titles", []):
            if str(entry.get("number")) == str(title):
                stamp = entry.get("latest_issue_date")
                if stamp:
                    resolved = date.fromisoformat(stamp)
                    self._issue_dates[title] = resolved
                    return resolved
        raise SourceNotFound(
            f"CFR title {title} is not published by eCFR", connector=self.name
        )

    def _next_id(self) -> str:
        self._counter += 1
        return f"e-{self._counter:03d}"

    def _fetch_xml(self, path: str) -> str:
        try:
            response = self._transport.get_text(path, accept="application/xml")
        except ProviderTimeout as exc:
            raise SourceUnavailable(f"timed out fetching {path}", connector=self.name) from exc
        except TransportError as exc:
            raise SourceUnavailable(f"{exc}", connector=self.name) from exc

        if response.status == 404:
            raise SourceNotFound(f"no such CFR section at {path}", connector=self.name)
        if response.status >= 400:
            raise SourceUnavailable(
                f"HTTP {response.status} for {path}", connector=self.name
            )
        return response.text

    def fetch(self, locator: str) -> RetrievedDocument:
        """Fetch one CFR section by citation.

        Cache first, then network. In ``offline`` mode a miss raises rather than
        silently returning nothing, so a run that could not consult a source
        says so instead of quietly adjudicating without it.
        """
        citation = parse_citation(locator)
        key = str(citation)

        cached = self.cache.get(self.name, key)
        if cached is not None and (self.offline or not cached.stale):
            return cached.document

        if self.offline:
            raise SourceUnavailable(
                f"{key} is not cached and --offline was requested. Run once with "
                "network access to populate the cache.",
                connector=self.name,
            )

        on = self.issue_date(citation.title)
        try:
            raw = self._fetch_xml(citation.api_path(on))
        except SourceUnavailable:
            # A stale cached copy beats no evidence: the caller is told it is
            # stale, and old evidence that announces its age is still evidence.
            if cached is not None:
                return cached.document
            raise

        text = xml_to_text(raw)
        if not text.strip():
            raise SourceNotFound(
                f"{key} returned an empty section on {on.isoformat()}. The section "
                "may have been removed, or may not exist in that issue.",
                connector=self.name,
            )

        document = build_document(
            connector=self,
            doc_id=self._next_id(),
            title=key,
            # The reader page, not the API URL: this is what a reader opens.
            # The dated API URL that was actually fetched is in metadata, so the
            # exact bytes behind this document remain re-requestable.
            url=citation.url,
            text=text,
            locator=key,
            metadata={
                "as_of": on.isoformat(),
                "title_number": str(citation.title),
                "api_url": BASE_URL + citation.api_path(on),
            },
        )
        self.cache.put(document)
        return document

    def locator_for_url(self, url: str) -> str | None:
        """Turn one of this connector's URLs back into a citation.

        Used by the drift probe, which holds a run record's URLs and needs to
        re-fetch through the connector that produced them -- so the same
        extraction and normalization run and the hashes are comparable. A probe
        that fetched the raw page instead would report drift on every stylesheet
        change, and an alarm that fires constantly is one nobody reads.
        """
        if "ecfr.gov" not in url.lower():
            return None
        match = _READER_RE.search(url)
        if not match:
            return None
        return f"{int(match.group('title'))} CFR {match.group('section')}"

    def search(self, query: str, *, limit: int = 5) -> list[RetrievedDocument]:
        """Not supported. Returns empty rather than raising.

        eCFR does have a search API. It is not wired up, because retrieval here
        is deterministic: a claim gets the section it NAMES. Keyword search
        would mean choosing which of several results is "the" source, and the
        only thing capable of that choice is a model -- which would make the set
        of sources consulted depend on model output and the run irreproducible.
        """
        return []


__all__ = ["API_PATH", "BASE_URL", "CFRCitation", "ECFRConnector", "parse_citation", "xml_to_text"]
