"""Reading a URL into a thread (``-u``).

WHAT A FETCHED PAGE IS, AND IS NOT
==================================
It is the **object under analysis**. It is never evidence.

That distinction is structural rather than a rule anyone has to remember.
Evidence enters the pipeline through a connector, which declares its own tier
(:mod:`abca.sources.base`); a page fetched here enters as the analyzed
:class:`~abca.schema.core.DocumentRef` and never touches the citation path. So
there is no sequence of events in which an article's own assertion becomes a
citation supporting itself -- which is exactly the trick a motivated page would
otherwise play on a tool like this.

WHAT IS EXTRACTED, AND WHY SO LITTLE IS GUESSED
===============================================
Two strategies, in order:

1. **JSON-LD** (``schema.org`` ``Article``, ``Comment``,
   ``DiscussionForumPosting``). Publishers emit it for search engines, it names
   the body and each comment's author explicitly, and reading it involves no
   guessing at all.
2. **The whole page as one post**, and the note says so.

There is deliberately no third strategy that hunts for comment containers by
class name. Heuristic scraping mis-attributes: it puts a moderator's boilerplate
in one person's mouth, or splits one comment into three. For a tool whose entire
value is that claims are traceable to what somebody actually wrote, "roughly the
right comments" is not a usable standard.

FETCHING BEHAVIOUR
==================
One request to the URL the person typed, plus redirects, and nothing else. No
crawling, no link following, no asset fetching. The user agent identifies the
tool and the project, so an operator seeing it in a log can tell what it was.
Redirects are followed to a bounded depth and every hop is recorded, because a
page reached through three hops to a different host is not the page that was
asked for and a reader of the record deserves to see that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from abca.inputs.threads import DEFAULT_MAX_POSTS, Post, Thread
from abca.providers.base import ProviderError
from abca.providers.transport import HttpTransport, TextResponse, TransportError

#: Identifies the tool in someone else's server log. A real project URL rather
#: than a browser impersonation string: a tool that lies about what it is has
#: no business publishing an integrity argument.
USER_AGENT = "abca/0.1 (+https://github.com/pjcampbe11/abca)"

#: Redirect hops followed before giving up.
MAX_REDIRECTS = 5

#: Largest page body read.
MAX_PAGE_BYTES = 8 * 1024 * 1024

DEFAULT_TIMEOUT = 30.0

_LD_JSON = re.compile(
    r'(?is)<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)

#: schema.org types whose ``text``/``articleBody`` is a post someone wrote.
_POST_TYPES = frozenset({
    "article", "newsarticle", "blogposting", "report", "socialmediaposting",
    "discussionforumposting", "comment", "answer", "question", "liveblogposting",
})


class UrlReadError(ValueError):
    """A URL could not be read into something analyzable."""


@dataclass(slots=True)
class FetchedPage:
    """A fetched URL, parsed into a thread."""

    thread: Thread
    #: The URL actually read, after redirects.
    final_url: str
    #: ``json-ld`` when structured data was used, ``page`` for the whole-page
    #: fallback. Recorded, so a reader knows how the comments were obtained.
    extractor: str = "page"
    content_type: str = ""
    redirects: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _origin(url: str) -> tuple[str, str]:
    """Split a URL into ``(origin, path-with-query)``."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UrlReadError(
            f"-u takes an http or https URL; got {parsed.scheme or '<none>'}://. "
            "A local file goes to -f."
        )
    if not parsed.hostname:
        raise UrlReadError(f"{url!r} has no host in it.")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return f"{parsed.scheme}://{parsed.netloc}", path


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
    transport_factory=None,
) -> tuple[TextResponse, str, list[str]]:
    """GET ``url``, following redirects. Returns ``(response, final_url, hops)``.

    A fresh transport per hop, because a redirect can cross hosts and a
    connection is bound to one. ``transport_factory`` exists so tests drive
    this without a socket.
    """
    factory = transport_factory or (
        lambda origin: HttpTransport(
            origin,
            timeout=timeout,
            provider_name="url",
            headers={"User-Agent": USER_AGENT},
        )
    )

    current = url
    hops: list[str] = []
    for _ in range(max_redirects + 1):
        origin, path = _origin(current)
        transport = factory(origin)
        try:
            response = transport.get_text(
                path, accept="text/html,application/xhtml+xml,*/*", max_bytes=MAX_PAGE_BYTES
            )
        except (TransportError, ProviderError) as exc:
            raise UrlReadError(f"could not fetch {current}: {exc}") from exc
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()

        target = response.location
        if not target:
            if response.status >= 400:
                raise UrlReadError(
                    f"{current} returned HTTP {response.status}. Nothing was "
                    "analyzed; an error page is not the document you asked for."
                )
            return response, current, hops
        hops.append(current)
        current = urljoin(current, target)

    raise UrlReadError(
        f"{url} redirected more than {max_redirects} times "
        f"({' -> '.join(hops[:4])}...). Give the final URL directly."
    )


# --------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------


def _types_of(node: dict) -> set[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return {raw.lower()}
    if isinstance(raw, list):
        return {item.lower() for item in raw if isinstance(item, str)}
    return set()


def _author_of(node: dict) -> str | None:
    """The author's NAME, from an ``author`` that may be a string, object or list."""
    author = node.get("author") or node.get("creator")
    if isinstance(author, list):
        author = author[0] if author else None
    if isinstance(author, str):
        return author.strip() or None
    if isinstance(author, dict):
        for key in ("alternateName", "name", "identifier"):
            value = author.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _body_of(node: dict) -> str:
    for key in ("articleBody", "text", "description", "headline"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _walk_ld(node: object, posts: list[Post], depth: int = 0) -> None:
    """Collect post-shaped nodes, depth-first, so replies follow their parent."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            _walk_ld(item, posts, depth)
        return
    if not isinstance(node, dict):
        return

    if node.get("@graph"):
        _walk_ld(node["@graph"], posts, depth)

    types = _types_of(node)
    if types & _POST_TYPES:
        body = _body_of(node)
        if body:
            posts.append(
                Post(
                    id="",
                    text=body,
                    author=_author_of(node),
                    depth=depth,
                )
            )
    for key in ("comment", "comments", "hasPart", "suggestedAnswer", "acceptedAnswer"):
        nested = node.get(key)
        if nested:
            _walk_ld(nested, posts, depth + 1)


def posts_from_json_ld(raw_html: str) -> list[Post]:
    """Pull article body and comments out of a page's JSON-LD blocks.

    A malformed block is skipped rather than fatal. Publishers ship broken
    JSON-LD constantly, and one bad block on a page is no reason to refuse to
    read the page -- the whole-page fallback is still there.
    """
    posts: list[Post] = []
    for match in _LD_JSON.finditer(raw_html):
        payload = match.group(1).strip()
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        _walk_ld(decoded, posts)
    return posts


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def read_url(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_posts: int = DEFAULT_MAX_POSTS,
    transport_factory=None,
) -> FetchedPage:
    """Fetch ``url`` and parse it into a thread."""
    from abca.sources.text import extract_title, html_to_text

    response, final_url, hops = fetch(
        url, timeout=timeout, transport_factory=transport_factory
    )
    content_type = response.content_type.split(";")[0].strip().lower()
    notes: list[str] = []

    if hops:
        notes.append(
            "followed " + " -> ".join([*hops, final_url])
            + ". The page analyzed is the final URL, not the one supplied."
        )
        if urlparse(final_url).hostname != urlparse(url).hostname:
            notes.append(
                f"the redirect crossed hosts: {urlparse(url).hostname} -> "
                f"{urlparse(final_url).hostname}. What was analyzed is served by a "
                "different site than the link named."
            )

    if content_type in {"application/json", "application/ld+json", "text/json"}:
        # A JSON endpoint (an API-backed thread). Reuse the export parser
        # rather than treating braces as prose.
        from abca.inputs.records import RecordFormatError, posts_from_json

        try:
            posts, title, mapping = posts_from_json(response.text)
        except RecordFormatError as exc:
            raise UrlReadError(f"{final_url}: {exc}") from exc
        notes.append(f"read as a JSON conversation; fields used: {mapping.describe()}")
        return FetchedPage(
            thread=Thread.build(
                posts, title=title, locator=final_url, notes=notes, max_posts=max_posts
            ),
            final_url=final_url,
            extractor="json",
            content_type=content_type,
            redirects=hops,
        )

    title = extract_title(response.text)
    structured = posts_from_json_ld(response.text)

    if structured:
        notes.append(
            f"extracted {len(structured)} post(s) from the page's schema.org JSON-LD. "
            "Authors come from the page's own structured data; no comment container "
            "was guessed at."
        )
        return FetchedPage(
            thread=Thread.build(
                structured, title=title, locator=final_url, notes=notes,
                max_posts=max_posts,
            ),
            final_url=final_url,
            extractor="json-ld",
            content_type=content_type,
            redirects=hops,
        )

    text = html_to_text(response.text) if content_type != "text/plain" else response.text
    if not text.strip():
        raise UrlReadError(
            f"{final_url} produced no text. If the page renders its content with "
            "JavaScript, this build cannot read it -- save the text and use -f."
        )

    notes.append(
        "no structured comment data on this page, so the WHOLE PAGE was analyzed as "
        "one document. Navigation, boilerplate and any comments are mixed together, "
        "and no claim below is attributed to a commenter. This build does not guess "
        "at comment containers: mis-attributing a sentence to the wrong person is a "
        "worse failure than not splitting the page."
    )
    return FetchedPage(
        thread=Thread.build(
            [Post(id="", text=text)], title=title, locator=final_url, notes=notes,
        ),
        final_url=final_url,
        extractor="page",
        content_type=content_type,
        redirects=hops,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_PAGE_BYTES",
    "MAX_REDIRECTS",
    "USER_AGENT",
    "FetchedPage",
    "UrlReadError",
    "fetch",
    "posts_from_json_ld",
    "read_url",
]
