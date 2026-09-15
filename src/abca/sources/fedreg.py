"""Federal Register, via its public API. Tier T0 -- primary legal text.

WHAT THIS CONNECTOR SERVES, AND WHAT IT DOES NOT
================================================
It serves **document numbers** -- ``2026-18141``, or ``FR Doc. 2026-18141`` as
the Register itself prints them at the end of every document. Those resolve
through a documented API endpoint to the GPO's own plain text of the published
page, which is as close to the primary source as a network fetch gets.

It does **not** serve the volume-and-page form (``91 FR 56737``), which is what
people actually write. That is a real gap and it is recorded rather than
papered over:

* the API exposes no citation condition (``conditions[citation]`` returns
  *"is not a valid field"*);
* a full-text search for the citation string returns zero results, because the
  string does not appear in the document that carries it;
* the site's ``/citation/91-FR-56737`` redirect is behind a bot wall.

Resolving volume+page needs an index this build does not have. So the citation
grammar recognises the form, marks it unservable, and the run says "recognised
this citation, no connector serves it" -- which is a coverage gap somebody can
act on, and a different finding from "found no citation at all".

TWO HOSTS, AND WHY
==================
Metadata comes from federalregister.gov's JSON API. The **text** comes from
**govinfo.gov**, the Government Publishing Office's own repository, at
``/content/pkg/FR-{date}/html/{document}.htm``.

That split is not architectural taste. Every full-text endpoint on
federalregister.gov -- ``raw_text_url``, ``full_text_xml_url``,
``body_html_url``, and the ``/citation/`` redirect -- sits behind a bot wall
that answers with a cross-origin redirect to ``unblock.federalregister.gov``.
The wall keys on the **TLS fingerprint**, not on headers or HTTP version:
``curl --http1.1`` with any user agent gets HTTP 200 from the same URL that
Python's ``http.client`` gets a 302 from, with byte-identical request headers.

Getting past that would mean forging a browser's TLS ClientHello. This project
will not do that. Circumventing a publisher's stated access control to obtain
evidence, in a tool whose entire argument is epistemic honesty, would be
self-refuting -- and the workaround exists anyway: GPO publishes the same
printed page, openly, and GPO is the authority the Register is printed by. The
provenance is if anything better.

The JSON API is not walled, so identification still comes from the Register
itself. Only the bytes come from GPO.

EVIDENCE COMES FROM THE HOST THAT WAS ASKED
===========================================
:meth:`FederalRegisterConnector._get` follows same-host redirects and **refuses
cross-host ones**. A connector's tier is a promise about a publisher. Following
a redirect off the host would attach ``T0 -- primary legal text`` to whatever
answered: a bot wall today, and in the general case anything at all. The
refusal names the host that was offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

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
from abca.sources.citations import FR_DOCUMENT_RE
from abca.sources.text import html_to_text

BASE_URL = "https://www.federalregister.gov"
API_HOST = "www.federalregister.gov"
API_PATH = "/api/v1/documents"

#: The Government Publishing Office. Serves the printed page openly.
GPO_URL = "https://www.govinfo.gov"
GPO_HOST = "www.govinfo.gov"
GPO_PATH = "/content/pkg/FR-{date}/html/{number}.htm"

#: Fields requested. Asked for explicitly rather than taking the default
#: payload, so a change in the API's defaults cannot silently start or stop
#: including something this connector records.
FIELDS = (
    "document_number", "title", "citation", "html_url", "publication_date",
    "type", "start_page", "end_page", "volume",
)

_DOC_URL_RE = re.compile(r"/documents/\d{4}/\d{2}/\d{2}/(?P<number>[\w-]+)/")


@dataclass(frozen=True, slots=True)
class FRDocument:
    """A parsed Federal Register document number."""

    number: str

    def __str__(self) -> str:
        return f"FR Doc. {self.number}"

    @property
    def api_path(self) -> str:
        return f"{API_PATH}/{self.number}.json?" + "&".join(
            f"fields[]={field}" for field in FIELDS
        )


def parse_citation(text: str) -> FRDocument:
    """Parse the FIRST Federal Register document number in ``text``.

    Strict. A guessed document number fetches a different rule, and the
    adjudicator then quotes that rule faithfully -- an answer indistinguishable
    from a correct one.
    """
    match = FR_DOCUMENT_RE.search(text)
    if not match:
        raise InvalidCitation(
            f"no Federal Register document number found in {text[:80]!r}; expected "
            "the form 'FR Doc. 2026-18141'. The volume-and-page form "
            "('91 FR 56737') is recognised but cannot be resolved by this build "
            "-- see the module docstring.",
            connector="fedreg",
        )
    return FRDocument(number=match.group("number"))


class FederalRegisterConnector:
    """Fetches Federal Register documents by document number."""

    name = "fedreg"
    #: PRIMARY LEGAL TEXT. Fixed at class definition; never a parameter, never
    #: something a model can populate. See abca.sources.base.
    tier = SourceTier.T0

    def __init__(
        self,
        *,
        cache: SourceCache | None = None,
        transport: HttpTransport | None = None,
        text_transport: HttpTransport | None = None,
        offline: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.cache = cache if cache is not None else SourceCache()
        self.offline = offline
        agent = {"User-Agent": "abca/0.1 (+https://github.com/pjcampbe11/abca)"}
        #: Identification: the Register's own API.
        self._transport = transport or HttpTransport(
            BASE_URL, timeout=timeout, provider_name="fedreg", headers=agent,
        )
        #: The bytes: GPO. See the module docstring for why these differ.
        self._text_transport = text_transport or HttpTransport(
            GPO_URL, timeout=timeout, provider_name="fedreg-gpo", headers=agent,
        )
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"f-{self._counter:03d}"

    def _get(
        self,
        path: str,
        *,
        accept: str,
        transport: HttpTransport | None = None,
        host: str = API_HOST,
        hops: int = 2,
    ) -> str:
        """GET ``path``, following SAME-HOST redirects only.

        A connector's tier is a promise about a publisher. Following a redirect
        off ``host`` would attach ``T0 -- primary legal text`` to whatever
        answered, which for this source is literally a bot wall. So a cross-host
        redirect is refused, and the refusal names the host that was offered.
        """
        client = transport or self._transport
        current = path
        for _ in range(hops + 1):
            try:
                response = client.get_text(current, accept=accept)
            except ProviderTimeout as exc:
                raise SourceUnavailable(
                    f"timed out fetching {current}", connector=self.name
                ) from exc
            except TransportError as exc:
                raise SourceUnavailable(f"{exc}", connector=self.name) from exc

            target = response.location
            if target:
                parsed = urlparse(urljoin(f"https://{host}{current}", target))
                if parsed.hostname and parsed.hostname != host:
                    raise SourceUnavailable(
                        f"{current} redirected off {host} to {parsed.hostname}. "
                        "Refused: this connector's T0 tier is a promise about a "
                        "specific publisher, and following a redirect would attach "
                        "that promise to whatever answered.",
                        connector=self.name,
                    )
                current = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                continue

            if response.status == 404:
                raise SourceNotFound(
                    f"no Federal Register document at {current}", connector=self.name
                )
            if response.status >= 400:
                raise SourceUnavailable(
                    f"HTTP {response.status} for {current}", connector=self.name
                )
            return response.text

        raise SourceUnavailable(
            f"{path} redirected more than {hops} times", connector=self.name
        )

    def fetch(self, locator: str) -> RetrievedDocument:
        """Fetch one Federal Register document by number.

        Two requests: metadata, then GPO's plain text of the printed page. The
        metadata is fetched first because it carries the ``raw_text_url``, and
        because a 404 there is a clean "no such document" rather than a
        mysterious failure on a URL that was guessed at.
        """
        import json as _json

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

        try:
            raw_meta = self._get(citation.api_path, accept="application/json")
            metadata = _json.loads(raw_meta)
            published = metadata.get("publication_date")
            if not published:
                raise SourceNotFound(
                    f"{key} has no publication date, so its printed page cannot be "
                    "located (it may be a correction stub).",
                    connector=self.name,
                )
            gpo_path = GPO_PATH.format(date=published, number=citation.number)
            body = self._get(
                gpo_path,
                accept="text/html",
                transport=self._text_transport,
                host=GPO_HOST,
            )
        except SourceUnavailable:
            # A stale cached copy beats no evidence: the caller is told it is
            # stale, and old evidence that announces its age is still evidence.
            if cached is not None:
                return cached.document
            raise
        except _json.JSONDecodeError as exc:
            raise SourceUnavailable(
                f"{key}: the API returned a body that is not JSON", connector=self.name
            ) from exc

        # The full-text XML is a shallow structure of block elements, so the
        # shared converter flattens it losslessly -- and using the SAME
        # converter every other corpus uses means whitespace is normalized
        # identically and hashes across connectors are comparable.
        text = html_to_text(body)
        if not text.strip():
            raise SourceNotFound(f"{key} returned no text", connector=self.name)

        document = build_document(
            connector=self,
            doc_id=self._next_id(),
            title=metadata.get("title") or key,
            url=metadata.get("html_url") or f"{BASE_URL}/d/{citation.number}",
            text=text,
            locator=key,
            metadata={
                "document_number": citation.number,
                "citation": metadata.get("citation") or "",
                "publication_date": metadata.get("publication_date") or "",
                "type": metadata.get("type") or "",
                "text_source": GPO_URL + gpo_path,
                "pages": f"{metadata.get('start_page', '')}-{metadata.get('end_page', '')}",
            },
        )
        self.cache.put(document)
        return document

    def locator_for_url(self, url: str) -> str | None:
        """Turn one of this connector's URLs back into a locator.

        Used by the drift probe, which holds a record's URLs and re-fetches
        through the connector that produced them so the same extraction runs
        and the hashes are comparable.
        """
        if "federalregister.gov" not in url.lower():
            return None
        match = _DOC_URL_RE.search(url)
        if match:
            return f"FR Doc. {match.group('number')}"
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return f"FR Doc. {tail}" if FR_DOCUMENT_RE.fullmatch(f"FR Doc. {tail}") else None

    def search(self, query: str, *, limit: int = 5) -> list[RetrievedDocument]:
        """Not supported, deliberately.

        The Federal Register does have a full-text search. Wiring it up would
        mean something has to choose which of several results is "the" source,
        and the only thing capable of that choice is a model -- which would make
        the set of sources consulted depend on model output, and the run
        irreproducible. Retrieval here is deterministic: a claim gets what it
        names.
        """
        return []


__all__ = [
    "API_PATH",
    "BASE_URL",
    "FIELDS",
    "GPO_PATH",
    "GPO_URL",
    "FRDocument",
    "FederalRegisterConnector",
    "parse_citation",
]
