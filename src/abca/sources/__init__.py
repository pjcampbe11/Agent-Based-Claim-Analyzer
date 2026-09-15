"""Source connectors and the retrieval cache.

One rule dominates this package: **the evidence tier of a source is a property
of its connector, declared as a class attribute, and no model output can
influence it.** See :mod:`abca.sources.base`.

Currently implemented:

======  ====  ====================================================
name    tier  covers
======  ====  ====================================================
ilcs    T0    Illinois Compiled Statutes, by citation
======  ====  ====================================================
"""

from abca.sources.base import (
    Connector,
    ConnectorError,
    InvalidCitation,
    RetrievedDocument,
    SourceNotFound,
    SourceUnavailable,
    build_document,
)
from abca.sources.cache import CacheEntry, SourceCache, default_cache_dir
from abca.sources.ilcs import ILCSCitation, ILCSConnector, find_citations, parse_citation
from abca.sources.registry import ROUTING, build_connectors, connectors_for, tier_of

__all__ = [
    "ROUTING", "CacheEntry", "Connector", "ConnectorError", "ILCSCitation",
    "ILCSConnector", "InvalidCitation", "RetrievedDocument", "SourceCache",
    "SourceNotFound", "SourceUnavailable", "build_connectors", "build_document",
    "connectors_for", "default_cache_dir", "find_citations", "parse_citation",
    "tier_of",
]
