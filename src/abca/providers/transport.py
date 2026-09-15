"""Minimal HTTP transport for JSON APIs.

WHY STDLIB AND NOT httpx/requests
=================================
Two reasons, in order:

1. **Dependency discipline.** ``EnvironmentInfo.packages`` records the version
   of every runtime-relevant library in each run record, and verification
   reports them on mismatch. Each dependency added is one more thing that can
   differ between the machine that produced a published verdict and the
   machine trying to reproduce it. The whole surface needed here is "POST some
   JSON to localhost, get JSON back" -- that does not justify a dependency.

2. **Testability.** :class:`Transport` is a Protocol, so the entire provider
   layer is tested against a fake with no network, no Ollama, and no GPU.
   Every provider test in this repo runs in milliseconds on any machine.

CONNECTION REUSE
================
:class:`HttpTransport` keeps one :class:`http.client.HTTPConnection` alive
across calls rather than using ``urllib.request``, which opens a fresh socket
per request. This is not premature optimization: the ``classifier`` role runs
once per claim, so a 2,000-comment thread makes thousands of calls. Paying a
TCP handshake on each one is measurable, and over an SSH tunnel to EC2 (see
docs/04-linux-ec2.md) it is worse than measurable.

The connection is NOT thread-safe. Providers are single-threaded by design;
concurrency in this pipeline belongs at the claim level with separate
provider instances, so that a run's per-model token accounting stays
attributable.
"""

from __future__ import annotations

import http.client
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from abca.providers.base import ProviderTimeout, ProviderUnavailable

#: Default per-request timeout in seconds. Generous because a 27B model on a
#: cold L4 can take tens of seconds for its first token while weights load;
#: `OLLAMA_KEEP_ALIVE` is the real fix (docs/04-linux-ec2.md) but the default
#: must not fail a legitimate cold start.
DEFAULT_TIMEOUT_S: float = 300.0


@runtime_checkable
class Transport(Protocol):
    """A JSON-in / JSON-out HTTP client.

    Deliberately tiny. Anything a provider needs beyond this belongs in the
    provider, where it can be reasoned about, not hidden in transport code.
    """

    def get_text(
        self, path: str, *, accept: str = "text/html,*/*", max_bytes: int = 8 * 1024 * 1024
    ) -> TextResponse:
        """GET a non-JSON resource; return the decoded body and its headers."""
        ...


    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload`` as JSON to ``path``; return the decoded response."""
        ...

    def get_json(self, path: str) -> dict[str, Any]:
        """GET ``path``; return the decoded response."""
        ...


class TransportError(ProviderUnavailable):
    """Network-level failure. Subclasses ProviderUnavailable deliberately.

    From the pipeline's point of view "the socket died" and "the daemon is not
    running" call for the same operator action, so they share a type. The
    message distinguishes them for the human.
    """


#: Codecs tried when a response declares no usable charset, in order. cp1252
#: is last because it decodes almost any byte sequence into *something*, so
#: trying it earlier would mask a real UTF-8 page with one stray byte.
_BODY_CODECS: tuple[str, ...] = ("utf-8", "cp1252", "latin-1")


@dataclass(frozen=True, slots=True)
class TextResponse:
    """A non-JSON HTTP response: status, decoded body, and headers."""

    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = ""

    @property
    def location(self) -> str | None:
        """The redirect target, when this is a redirect."""
        return self.headers.get("location") if 300 <= self.status < 400 else None


def _decompress(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    """Undo ``Content-Encoding``, refusing anything that expands past the cap.

    The cap is checked DURING decompression, not after. A few kilobytes of gzip
    can expand to gigabytes, so decompressing first and measuring afterwards
    would hand a remote server the ability to exhaust this process's memory --
    and every fetch here goes to somebody else's server.
    """
    if "gzip" not in encoding.lower():
        return raw
    import gzip
    import zlib

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            out = stream.read(max_bytes + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise TransportError(
            f"response declared gzip but did not decompress: {exc}", provider="http"
        ) from exc
    return out


def _decode_body(raw: bytes, content_type: str) -> str:
    """Decode a response body, honouring a declared charset when there is one.

    Falls back through a fixed ladder and, as a last resort, replaces
    undecodable bytes. Replacement is acceptable HERE and nowhere else in this
    codebase: a fetched web page is the OBJECT under analysis, never evidence,
    and a page with three mojibake characters is still worth reading. A
    statute decoded that way would be a different matter, which is why the
    file reader refuses instead.
    """
    declared = ""
    if "charset=" in content_type.lower():
        declared = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    for codec in (declared, *_BODY_CODECS):
        if not codec:
            continue
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


class HttpTransport:
    """Keep-alive JSON HTTP client over :mod:`http.client`."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        provider_name: str = "http",
        headers: dict[str, str] | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                f"base_url must be http or https, got {base_url!r}. "
                "For a remote Ollama, forward it to localhost with an SSH "
                "tunnel rather than exposing it (see docs/04-linux-ec2.md)."
            )
        self.base_url = base_url.rstrip("/")
        self._scheme = parsed.scheme
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # A base URL may carry a path prefix (a reverse proxy in front of
        # Ollama). Preserved and prepended to every request path.
        self._prefix = parsed.path.rstrip("/")
        self.timeout = timeout
        self.provider_name = provider_name
        self._headers = {"Content-Type": "application/json", **(headers or {})}
        self._connection: http.client.HTTPConnection | None = None

    # ------------------------------------------------------------- connection

    def _connect(self) -> http.client.HTTPConnection:
        """Return the live connection, opening one if needed."""
        if self._connection is None:
            factory = (
                http.client.HTTPSConnection
                if self._scheme == "https"
                else http.client.HTTPConnection
            )
            self._connection = factory(self._host, self._port, timeout=self.timeout)
        return self._connection

    def close(self) -> None:
        """Drop the connection. Safe to call repeatedly."""
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- requests

    def _request(self, method: str, path: str, body: bytes | None) -> dict[str, Any]:
        """Issue one request, retrying ONCE on a stale keep-alive connection.

        The single retry is not a general retry policy -- it exists for one
        specific, benign case: the server closed an idle keep-alive connection
        between requests, which surfaces as an immediate
        ``RemoteDisconnected`` / ``BadStatusLine`` before any bytes were
        processed. Retrying that is safe because nothing happened.

        Real failures (timeouts, refused connections, HTTP errors) are NOT
        retried here. Retrying a timed-out 27B generation would silently
        double the wall-clock cost of an already-slow run, and retry policy
        for *content* failures belongs in
        :mod:`abca.providers.structured` where the attempts are counted and
        recorded.
        """
        url = f"{self._prefix}{path}"

        for attempt in (1, 2):
            connection = self._connect()
            try:
                connection.request(method, url, body=body, headers=self._headers)
                response = connection.getresponse()
                raw = response.read()
            except TimeoutError as exc:
                self.close()
                raise ProviderTimeout(
                    f"{method} {url} timed out after {self.timeout}s",
                    provider=self.provider_name,
                ) from exc
            except (http.client.RemoteDisconnected, http.client.BadStatusLine) as exc:
                # Stale keep-alive: reopen and try once more.
                self.close()
                if attempt == 1:
                    continue
                raise TransportError(
                    f"{method} {url} failed: connection dropped ({exc})",
                    provider=self.provider_name,
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                raise TransportError(
                    f"{method} {url} failed: {exc}. Is the backend running and "
                    "reachable? For a remote model, check the SSH tunnel.",
                    provider=self.provider_name,
                ) from exc

            return self._decode(method, url, response.status, raw)

        raise AssertionError("unreachable")  # pragma: no cover

    def _decode(self, method: str, url: str, status: int, raw: bytes) -> dict[str, Any]:
        """Turn a raw response into a dict, or raise with a useful message."""
        text = raw.decode("utf-8", errors="replace")

        if status >= 400:
            # Surface the backend's own error text: Ollama's "model not found"
            # message is far more actionable than a bare 404.
            detail = text.strip()[:500] or "<empty body>"
            raise TransportError(
                f"{method} {url} returned HTTP {status}: {detail}",
                provider=self.provider_name,
            )

        if not text.strip():
            return {}

        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TransportError(
                f"{method} {url} returned non-JSON body: {text[:200]!r}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(decoded, dict):
            # Every endpoint used here returns an object. A bare list or scalar
            # means the server is not what we think it is.
            raise TransportError(
                f"{method} {url} returned {type(decoded).__name__}, expected an object",
                provider=self.provider_name,
            )
        return decoded

    def get_text(
        self,
        path: str,
        *,
        accept: str = "text/html,*/*",
        max_bytes: int = 8 * 1024 * 1024,
        compress: bool = True,
    ) -> TextResponse:
        """GET a non-JSON resource. Returns the body, decoded, plus its headers.

        Exists because three callers need raw bytes over HTTP -- the statute
        connectors, which fetch HTML and XML, and the URL input reader. They
        used to reach into ``_connect`` and ``_headers`` directly, which meant
        the keep-alive retry and the timeout handling above were being bypassed
        by exactly the code paths that hit somebody else's server.

        ``compress`` requests gzip and decompresses the response. Not an
        optimisation: eCFR REFUSES an uncompressed request outright, and a
        statute API that cannot be reached is a connector that does not exist.
        Decompression is stdlib, and the size cap is applied to the
        DECOMPRESSED body, so a compression bomb cannot slip past it.

        Redirects are REPORTED, not followed. A redirect can cross hosts, which
        needs a different connection, and silently following one would also let
        a link resolve somewhere the person who typed it never named. The
        caller decides.
        """
        url = f"{self._prefix}{path}"
        headers = {**self._headers, "Accept": accept}
        headers.pop("Content-Type", None)  # no body on a GET
        if compress:
            headers["Accept-Encoding"] = "gzip"

        for attempt in (1, 2):
            connection = self._connect()
            try:
                connection.request("GET", url, headers=headers)
                response = connection.getresponse()
                raw = response.read(max_bytes + 1)
                response.read()  # drain, so the connection stays reusable
            except TimeoutError as exc:
                self.close()
                raise ProviderTimeout(
                    f"GET {url} timed out after {self.timeout}s",
                    provider=self.provider_name,
                ) from exc
            except (http.client.RemoteDisconnected, http.client.BadStatusLine) as exc:
                self.close()
                if attempt == 1:
                    continue
                raise TransportError(
                    f"GET {url} failed: connection dropped ({exc})",
                    provider=self.provider_name,
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                raise TransportError(
                    f"GET {url} failed: {exc}", provider=self.provider_name
                ) from exc

            headers_map = {key.lower(): value for key, value in response.getheaders()}
            raw = _decompress(raw, headers_map.get("content-encoding", ""), max_bytes)

            if len(raw) > max_bytes:
                raise TransportError(
                    f"GET {url} returned more than {max_bytes} bytes; refused rather "
                    "than truncated, because half a document analyzed as a whole one "
                    "is the failure this tool exists to avoid.",
                    provider=self.provider_name,
                )

            return TextResponse(
                status=response.status,
                text=_decode_body(raw, headers_map.get("content-type", "")),
                headers=headers_map,
                content_type=headers_map.get("content-type", ""),
            )

        raise AssertionError("unreachable")  # pragma: no cover

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        return self._request("POST", path, body)

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request("GET", path, None)


class FakeTransport:
    """Scripted transport for tests. Records calls; returns canned responses.

    Lives in the package rather than in ``tests/`` on purpose: it is also the
    supported way for a downstream integrator to exercise abCA's provider
    layer without a GPU, and keeping it here means it is covered by this
    repo's own tests rather than rotting.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        """
        ``responses`` maps a path to either a dict (returned every time) or a
        list of dicts (returned in order, so a repair loop can be scripted:
        bad output first, good output second).
        """
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._cursors: dict[str, int] = {}

    def _resolve(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        raw: bool = False,
    ) -> Any:
        """Return the scripted response. ``raw`` keeps non-dict values intact.

        Without ``raw`` this is the JSON accessor's helper and every response is
        a dict. :meth:`get_text` needs strings and :class:`TextResponse` objects
        through unchanged, so it opts out of that expectation rather than the
        two accessors keeping separate script tables that could disagree.
        """
        self.calls.append((method, path, payload))

        if path in self._errors:
            raise self._errors[path]

        if path not in self._responses:
            raise TransportError(f"no scripted response for {method} {path}", provider="fake")

        scripted = self._responses[path]
        if isinstance(scripted, list):
            index = self._cursors.get(path, 0)
            # Clamp rather than raise: a test that makes one extra call should
            # see the last scripted response, not a confusing IndexError.
            item = scripted[min(index, len(scripted) - 1)]
            self._cursors[path] = index + 1
            if isinstance(item, Exception):
                raise item
            return item
        return scripted

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._resolve("POST", path, payload)

    def get_json(self, path: str) -> dict[str, Any]:
        return self._resolve("GET", path, None)

    def get_text(
        self, path: str, *, accept: str = "text/html,*/*", max_bytes: int = 8 * 1024 * 1024
    ) -> TextResponse:
        """Scripted non-JSON GET.

        A scripted value may be a :class:`TextResponse` (to script a status or
        a redirect), a plain string (a 200 with that body), or a dict (encoded
        as JSON, so one fake can serve both accessors).
        """
        scripted = self._resolve("GET", path, None, raw=True)
        if isinstance(scripted, TextResponse):
            return scripted
        if isinstance(scripted, str):
            return TextResponse(status=200, text=scripted, content_type="text/html")
        return TextResponse(
            status=200, text=json.dumps(scripted), content_type="application/json"
        )

    # ------------------------------------------------------------- assertions

    def paths_called(self) -> list[str]:
        return [path for _, path, _ in self.calls]

    def payloads_for(self, path: str) -> list[dict[str, Any]]:
        return [p for _, called, p in self.calls if called == path and p is not None]

    def call_count(self, path: str) -> int:
        return sum(1 for _, called, _ in self.calls if called == path)


def monotonic_ms(started: float) -> int:
    """Elapsed milliseconds since ``started`` (a :func:`time.perf_counter` value).

    Uses the monotonic clock so an NTP step or a DST change cannot produce a
    negative duration in a run record.
    """
    return max(0, int((time.perf_counter() - started) * 1000))


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FakeTransport",
    "HttpTransport",
    "TextResponse",
    "Transport",
    "TransportError",
    "monotonic_ms",
]
