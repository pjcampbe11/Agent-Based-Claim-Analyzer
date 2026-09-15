"""Connector registry: name to connector, with tiers fixed at registration.

Small on purpose. Its whole job is to be the one place that knows which
connectors exist, so adding a source is a one-line change here rather than an
edit scattered through the pipeline.

The tier of each connector is read off the class, never configured. A
``config.toml`` that could set ``tier = "T0"`` on an arbitrary connector would
hand the most important safety property in the system to whoever edits that
file.

ROUTING IS BY CITATION, NOT BY CLAIM TYPE ALONE
===============================================
:data:`ROUTING` says which connectors a claim TYPE may consult at all. Which one
actually runs is decided by what the claim CITES -- see
:mod:`abca.sources.citations`, where each citation form names the connector that
serves it. Two gates rather than one: a legal claim citing a CFR section must
not be sent to the Illinois statute connector, and an empirical claim must not
be sent to a statute connector at all.
"""

from __future__ import annotations

from abca.schema.enums import ClaimType, SourceTier
from abca.sources.base import Connector
from abca.sources.cache import SourceCache
from abca.sources.ecfr import ECFRConnector
from abca.sources.fedreg import FederalRegisterConnector
from abca.sources.ilcs import ILCSConnector

#: Connector classes, by the name the citation grammar uses for them. One entry
#: per source; adding a connector is this line plus the module.
CONNECTOR_CLASSES: dict[str, type] = {
    "ilcs": ILCSConnector,
    "ecfr": ECFRConnector,
    "fedreg": FederalRegisterConnector,
}

#: Which connectors serve which claim types (contract s3 routing).
#:
#: EMPIRICAL is deliberately empty, and it is worth being explicit about why,
#: because it looks like an omission and is not. Retrieval here is
#: deterministic: a claim gets the source it NAMES. "Unemployment was 4.1% in
#: June" names no source, so serving it would mean something has to CHOOSE a
#: statistical series -- and the only thing capable of that choice is a model,
#: which would make the set of sources consulted depend on model output and the
#: run irreproducible. A confident guess would then fetch a number the author
#: never referred to, and the adjudicator would quote it faithfully.
#:
#: So empirical claims report ``no source`` until a connector exists for
#: series that claims actually cite. That is a property of the design, not a
#: gap in it.
ROUTING: dict[ClaimType, tuple[str, ...]] = {
    ClaimType.LEGAL: ("ilcs", "ecfr", "fedreg"),
    ClaimType.EMPIRICAL: (),
    ClaimType.ATTRIBUTIVE: (),
    ClaimType.PREDICTIVE: (),
    ClaimType.NORMATIVE: (),
    ClaimType.DEFINITIONAL: (),
}


def build_connectors(
    *,
    cache: SourceCache | None = None,
    offline: bool = False,
    names: list[str] | None = None,
) -> dict[str, Connector]:
    """Construct the requested connectors, or all known ones.

    ``offline`` is threaded through rather than checked at call sites so that
    a run declared offline cannot reach the network through any connector.
    """
    shared_cache = cache if cache is not None else SourceCache()
    wanted = names if names is not None else list(CONNECTOR_CLASSES)
    return {
        name: CONNECTOR_CLASSES[name](cache=shared_cache, offline=offline)
        for name in wanted
        if name in CONNECTOR_CLASSES
    }


def connectors_for(claim_type: ClaimType, connectors: dict[str, Connector]) -> list[Connector]:
    """Return the connectors that should be consulted for ``claim_type``."""
    return [connectors[name] for name in ROUTING.get(claim_type, ()) if name in connectors]


def tier_of(name: str) -> SourceTier | None:
    """The declared tier of a connector, by name. For display and docs."""
    connector = CONNECTOR_CLASSES.get(name)
    return connector.tier if connector else None


__all__ = [
    "CONNECTOR_CLASSES",
    "ROUTING",
    "build_connectors",
    "connectors_for",
    "tier_of",
]
