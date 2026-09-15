"""Retrieval stage: find the sources a claim should be checked against.

DETERMINISTIC BY DESIGN
=======================
No model call happens here. Retrieval is citation extraction plus connector
lookup, both plain code, for the same reason sentence splitting is:
the set of sources consulted is part of the recipe, and a model that picked
different sources between two runs would make identical input produce a
different ``input_digest`` -- a `DIVERGENT` verification for no substantive
reason.

That also means a reader can check retrieval by hand. "Which statutes did you
look at, and why those?" has an answer that does not involve trusting a model.

WHAT COMES BACK, AND WHAT DOES NOT
==================================
Only claim types the routing table serves get retrieval at all
(:data:`abca.sources.registry.ROUTING`). ``NORMATIVE`` and ``PREDICTIVE``
claims retrieve nothing, because evidence cannot settle them -- spending
network calls on them would be spending money to produce a verdict the
contract forbids.

A claim that cites a statute which DOES NOT EXIST is a finding, not an error.
It is recorded in the stage notes and surfaces to the adjudicator as an absent
source, which is exactly the situation the ``CONTRADICTED`` verdict is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.pipeline.base import StageOutcome
from abca.pipeline.models import DraftClaim
from abca.sources.base import (
    Connector,
    ConnectorError,
    RetrievedDocument,
    SourceNotFound,
    SourceUnavailable,
)
from abca.sources.citations import NO_CONNECTOR_YET, SourceCitation, find_citations
from abca.sources.registry import connectors_for

#: Maximum documents retrieved per claim. A claim citing nine statutes is
#: almost always rhetorical rather than substantive, and each fetch costs a
#: request to someone else's server plus context in the adjudication prompt.
MAX_DOCUMENTS_PER_CLAIM = 4

#: Maximum documents in one run. A hard stop so a thread full of citations
#: cannot turn into a thousand requests to a state legislature.
MAX_DOCUMENTS_PER_RUN = 40


@dataclass(slots=True)
class RetrievalResult:
    """What retrieval found, indexed for the adjudication stage."""

    #: All distinct documents retrieved this run, in retrieval order.
    documents: list[RetrievedDocument] = field(default_factory=list)
    #: claim id -> the document ids relevant to it.
    by_claim: dict[str, list[str]] = field(default_factory=dict)
    #: Citations that were parsed out of a claim but did not resolve. A
    #: FINDING about the claim, carried forward rather than swallowed.
    unresolved: dict[str, list[str]] = field(default_factory=dict)

    def documents_for(self, claim_id: str) -> list[RetrievedDocument]:
        index = {document.id: document for document in self.documents}
        return [index[doc_id] for doc_id in self.by_claim.get(claim_id, []) if doc_id in index]

    def index(self) -> dict[str, RetrievedDocument]:
        """Document id -> document. The adjudicator's only handle on a source."""
        return {document.id: document for document in self.documents}


def candidate_citations(claim: DraftClaim) -> list[SourceCitation]:
    """Extract the source citations a claim points at, with their connectors.

    Deliberately literal: a claim that names a source gets that source.
    Guessing at which statute an unspecific claim "probably means" would fetch
    something the author never referred to, and the adjudicator would then quote
    it faithfully -- producing a confident, well-cited answer to a question
    nobody asked.
    """
    return find_citations(claim.text)


def candidate_locators(claim: DraftClaim) -> list[str]:
    """Locator strings only. Kept for callers that do not need the routing."""
    return [citation.locator for citation in candidate_citations(claim)]


def run_retrieve(
    claims: list[DraftClaim],
    connectors: dict[str, Connector],
    *,
    max_per_claim: int = MAX_DOCUMENTS_PER_CLAIM,
    max_per_run: int = MAX_DOCUMENTS_PER_RUN,
) -> StageOutcome[RetrievalResult]:
    """Retrieve sources for every claim whose type has a route.

    Documents are deduplicated by locator across claims: a thread where forty
    people cite the same statute fetches it once.
    """
    outcome: StageOutcome[RetrievalResult] = StageOutcome(value=RetrievalResult())
    result = outcome.value

    #: locator -> already-retrieved document, so repeated citations reuse it.
    seen: dict[str, RetrievedDocument] = {}
    considered = 0
    skipped_no_route = 0
    truncated = False
    #: Citation kinds this build parses but cannot fetch.
    unservable: set[str] = set()
    #: Connectors a citation asked for that this run did not build.
    skipped_unrouted: set[str] = set()

    for claim in claims:
        if claim.gated_out or claim.claim_type is None:
            continue

        routed = connectors_for(claim.claim_type, connectors)
        if not routed:
            # Either the type cannot be settled by evidence, or no connector
            # covers it yet. Both are legitimate; neither is an error.
            skipped_no_route += 1
            continue

        citations = candidate_citations(claim)
        if not citations:
            continue

        considered += 1
        found: list[str] = []
        missing: list[str] = []
        by_name = {connector.name: connector for connector in routed}

        for citation in citations[:max_per_claim]:
            locator = citation.locator
            if len(result.documents) >= max_per_run and locator not in seen:
                truncated = True
                break

            if locator in seen:
                found.append(seen[locator].id)
                continue

            if citation.kind in NO_CONNECTOR_YET:
                # A RECOGNISED citation with no connector. Reported as
                # unresolved, which is a different and far more useful finding
                # than "no citation found": one is a coverage gap somebody can
                # act on, the other looks like the claim cited nothing.
                unservable.add(citation.kind.value)
                missing.append(locator)
                continue

            # Routed by what the claim CITES, not merely by its type. A legal
            # claim naming a CFR section must not be sent to the Illinois
            # statute connector: it would 404 there, and on a source that
            # happened to answer it would return the wrong provision.
            target = by_name.get(citation.connector)
            if target is None:
                skipped_unrouted.add(citation.connector)
                missing.append(locator)
                continue

            document = _fetch_first([target], locator, outcome)
            if document is None:
                missing.append(locator)
                continue

            seen[locator] = document
            result.documents.append(document)
            found.append(document.id)

        if found:
            result.by_claim[claim.id] = found
        if missing:
            result.unresolved[claim.id] = missing

    if truncated:
        outcome.note(
            f"retrieval stopped at {max_per_run} documents; later citations in this "
            "run were not fetched"
        )
    if result.unresolved:
        total = sum(len(items) for items in result.unresolved.values())
        outcome.note(
            f"{total} cited source(s) did not resolve. That is a finding about the "
            "claim, not a retrieval error: a citation to a section that does not "
            "exist is evidence about the claim's accuracy."
        )
    if skipped_no_route:
        outcome.note(
            f"{skipped_no_route} claim(s) had no routed connector for their type; "
            "no sources were retrieved for them"
        )
    if unservable:
        outcome.note(
            "recognised citation(s) this build cannot fetch: "
            + ", ".join(sorted(unservable))
            + ". The citation was READ and could not be retrieved, which is a "
            "coverage gap -- not the same as the claim citing nothing. See "
            "abca.sources.citations.NO_CONNECTOR_YET for why each is unserved."
        )
    if skipped_unrouted:
        outcome.note(
            "citation(s) asked for connector(s) not built for this run: "
            + ", ".join(sorted(skipped_unrouted))
        )
    outcome.note(
        f"retrieved {len(result.documents)} document(s) for {len(result.by_claim)} "
        f"of {considered} claim(s) carrying a citation"
    )
    outcome.dedupe_notes()
    return outcome


def _fetch_first(
    connectors: list[Connector],
    locator: str,
    outcome: StageOutcome[RetrievalResult],
) -> RetrievedDocument | None:
    """Try each routed connector in order; return the first document found.

    Order is the routing table's order, which is strongest tier first. A
    connector that raises ``SourceUnavailable`` is a run problem and is noted;
    ``SourceNotFound`` is a claim problem and is left for the caller to record
    as unresolved.
    """
    for connector in connectors:
        try:
            return connector.fetch(locator)
        except SourceNotFound:
            continue
        except SourceUnavailable as exc:
            outcome.note(
                f"{connector.name} unavailable while fetching {locator}: {exc}. "
                "The claim was adjudicated without this source."
            )
            continue
        except ConnectorError as exc:
            outcome.note(f"{connector.name} could not fetch {locator}: {exc}")
            continue
    return None


__all__ = [
    "MAX_DOCUMENTS_PER_CLAIM",
    "MAX_DOCUMENTS_PER_RUN",
    "RetrievalResult",
    "candidate_citations",
    "candidate_locators",
    "run_retrieve",
]
