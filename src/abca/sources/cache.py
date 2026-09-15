"""On-disk cache for retrieved source documents.

WHY THE CACHE IS LOAD-BEARING, NOT AN OPTIMISATION
==================================================
Three things depend on it, and only the first is about speed:

1. **Latency.** The ``fast`` profile targets under ten seconds. A live fetch of
   a statute page is most of that budget, and the same handful of sections get
   cited over and over across a thread.
2. **``--offline``.** A run with no network at all must still adjudicate
   against sources it has already seen. That matters for an air-gapped review
   and for reproducing someone else's published run.
3. **Politeness.** Analyzing a 2,000-comment thread must not mean two thousand
   requests to a state legislature's web server.

WHAT IS STORED
==============
The EXTRACTED TEXT, not the raw HTML, plus everything needed to rebuild a
:class:`~abca.sources.base.RetrievedDocument` exactly.

``retrieved_at`` is preserved from the ORIGINAL fetch and never refreshed on a
cache hit. A citation says "this is what the source said at this moment"; a
timestamp that quietly advanced to now would make a stale quote look fresh and
would defeat drift detection, which is the one thing the timestamp is for.

THE HASH CHECK ON READ IS A SECURITY BOUNDARY
=============================================
Every entry is re-hashed when loaded and discarded on mismatch. That catches a
truncated write, and -- more usefully -- any hand-edit of a cache file. Without
it, someone with write access to the cache could plant fabricated statute text
that the adjudicator's quote verification would then confirm perfectly, because
quotes are checked against exactly this text. The cache is the one place where
a forged source would look completely legitimate downstream.

STALENESS
=========
Entries carry a TTL used only to decide whether to re-fetch when the network is
available. An expired entry is still served in ``--offline`` mode, with the
caller told it is stale -- old evidence that announces its age beats no
evidence at all.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from abca.canonical import digest_text
from abca.schema.enums import SourceTier
from abca.sources.base import RetrievedDocument

#: Default lifetime before a cached document is re-fetched when online.
#: Seven days is a compromise: statutes change on legislative timescales, not
#: daily, but a verdict resting on month-old text is worth re-checking.
DEFAULT_TTL = timedelta(days=7)

#: Bumped when the on-disk entry format changes, so stale-format entries are
#: ignored rather than mis-parsed.
CACHE_FORMAT = 1


#: Environment override for the source cache, honoured by EVERY path that
#: builds a default cache -- the analyzer, replay, and the sources command
#: alike. Mirrors ``ABCA_LEDGER_ROOT`` for the run ledger.
CACHE_ENV = "ABCA_SOURCE_CACHE"


def default_cache_dir() -> Path:
    """Platform-appropriate source cache location.

    Sits beside the run ledger under the same data directory: both are
    machine-local derived state, and a user clearing one usually means both.

    ``ABCA_SOURCE_CACHE`` overrides it, and the override is read HERE rather
    than only at the ``abca sources`` command line, because of a bug worth
    recording. The command honoured the variable; ``analyze --offline`` did not
    -- it built ``SourceCache()`` and read the real machine cache. The test
    suite's offline fixture seeded a cache through the variable and asserted
    on verified citations, and passed for months on a machine whose real
    cache the network tests had filled with genuine statute text. On a fresh
    CI runner the real cache was empty, no source was consulted, and three
    tests failed on the first push. An environment override that only some
    entry points respect is a variable that lies.
    """
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)

    from abca.ledger.store import default_data_dir

    return default_data_dir() / "sources"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cached document plus how old it is."""

    document: RetrievedDocument
    cached_at: datetime
    stale: bool


class SourceCache:
    """Content cache keyed by ``(connector, locator)``."""

    def __init__(self, root: Path | str | None = None, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self.root = Path(root) if root is not None else default_cache_dir()
        self.ttl = ttl

    def _path(self, connector: str, locator: str) -> Path:
        """Hash the locator into a filename.

        Locators contain slashes and other characters that are not filename
        safe ("10 ILCS 5/10-2"), and hashing sidesteps every escaping question
        as well as path-length limits. The human-readable locator is stored
        INSIDE the entry, so the cache stays inspectable.
        """
        key = digest_text(f"{connector} {locator}").split(":", 1)[1]
        return self.root / connector / key[:2] / f"{key}.json"

    def get(self, connector: str, locator: str) -> CacheEntry | None:
        """Return the cached document, or ``None`` on a miss."""
        path = self._path(connector, locator)
        if not path.is_file():
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt entry is a miss, never an error. The document can
            # always be fetched again; failing a run over a bad cache file
            # would be absurd.
            return None

        if payload.get("format") != CACHE_FORMAT:
            return None

        try:
            document = RetrievedDocument(
                id=payload["id"],
                tier=SourceTier(payload["tier"]),
                title=payload["title"],
                url=payload["url"],
                text=payload["text"],
                content_hash=payload["content_hash"],
                retrieved_at=datetime.fromisoformat(payload["retrieved_at"]),
                connector=payload["connector"],
                locator=payload.get("locator"),
                metadata=payload.get("metadata") or {},
            )
        except (KeyError, ValueError):
            return None

        # See "THE HASH CHECK ON READ IS A SECURITY BOUNDARY" above.
        if digest_text(document.text) != document.content_hash:
            return None

        cached_at = datetime.fromisoformat(payload["cached_at"])
        return CacheEntry(
            document=document,
            cached_at=cached_at,
            stale=datetime.now(UTC) - cached_at > self.ttl,
        )

    def put(self, document: RetrievedDocument) -> Path:
        """Store a document. Atomic, like the ledger writer."""
        path = self._path(document.connector, document.locator or document.url)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "format": CACHE_FORMAT,
            "id": document.id,
            "tier": document.tier.value,
            "title": document.title,
            "url": document.url,
            "text": document.text,
            "content_hash": document.content_hash,
            "retrieved_at": document.retrieved_at.isoformat(),
            "connector": document.connector,
            "locator": document.locator,
            "metadata": document.metadata,
            "cached_at": datetime.now(UTC).isoformat(),
        }

        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.stem}.", suffix=".tmp", delete=False,
        )
        try:
            with handle as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        return path

    def stats(self) -> dict[str, int]:
        """Per-connector entry counts, for ``abca sources cache-stats``."""
        if not self.root.is_dir():
            return {}
        return {
            directory.name: sum(1 for _ in directory.rglob("*.json"))
            for directory in self.root.iterdir()
            if directory.is_dir()
        }

    def clear(self, connector: str | None = None) -> int:
        """Delete cached entries. Returns how many were removed."""
        target = self.root / connector if connector else self.root
        if not target.is_dir():
            return 0
        removed = 0
        for path in target.rglob("*.json"):
            path.unlink()
            removed += 1
        return removed


__all__ = ["CACHE_FORMAT", "DEFAULT_TTL", "CacheEntry", "SourceCache", "default_cache_dir"]
