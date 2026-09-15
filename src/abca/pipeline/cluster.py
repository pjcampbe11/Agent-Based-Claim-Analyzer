"""Clustering near-duplicate claims, so a thread is adjudicated once.

THE PROBLEM
===========
Forty people in a comment thread assert the same thing about the same statute.
Adjudicated one at a time that is forty retrievals, forty adjudication calls and
forty red-team calls to reach one answer forty times -- which on a large thread
is the difference between a tool someone uses and a tool nobody can afford to
run.

THE DANGER, WHICH IS LARGER THAN THE PROBLEM
============================================
Clustering means **one claim's verdict is published against another claim's
words**. Get the grouping wrong and the tool attributes a verdict to something
that does not say what the adjudicated claim said -- confidently, with a
citation, and with no visible sign anything went wrong. That is a worse failure
than being slow.

So every decision here is made in the strict direction:

* **No model call.** The grouping is part of the recipe: it decides which text
  gets sent to the adjudicator. A model choosing it would make identical input
  produce a different ``input_digest`` and an unreproducible run.
* **Four exact gates before similarity is even measured.** Claim type, stance
  ambiguity, the numeric anchors in the text, and the statute citations named.
  Two claims that differ in any of those are different claims, however similar
  the prose.
* **A high overlap threshold**, well above the fidelity gate's -- that one is
  comparing a deliberate rewrite against its source, where different words are
  the point. Here, different words mean a different claim.
* **The grouping is published.** :class:`~abca.schema.core.ClusterRef` ships on
  every clustered claim with the member count and the lowest pairwise
  similarity inside the cluster, so a reader can check the grouping rather than
  take it on trust.

The asymmetry, stated plainly: a cluster that should have merged and did not
costs model calls. A cluster that merged and should not have publishes a wrong
verdict about somebody's words. Only the second one is a lie, so the thresholds
are set to make the first mistake instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from abca.fidelity.elements import content_tokens, extract_anchors
from abca.pipeline.base import StageOutcome
from abca.pipeline.models import DraftClaim
from abca.schema.core import ClusterRef
from abca.sources.ilcs import find_citations

#: Minimum token-overlap F1 for two claims to be treated as the same claim.
#:
#: Much stricter than the fidelity gate's 0.35, and for the opposite reason.
#: There, a rewrite is SUPPOSED to use different words, so a low bar is
#: correct. Here, different words are evidence of a different claim, and the
#: cost of merging wrongly is a verdict published against text that did not say
#: it. 0.75 admits "Illinois requires 25,000 signatures" alongside "Illinois
#: requires twenty-five thousand signatures to form a party" while keeping out
#: claims that merely share a subject.
CLUSTER_THRESHOLD = 0.75

#: Below this, a cluster is flagged for review in the run notes even though it
#: passed. A cluster whose weakest pair only just cleared the bar is the one a
#: reader should look at first.
REVIEW_SIMILARITY = 0.85

#: Claims per cluster beyond which the pairwise minimum is sampled rather than
#: computed in full. A 500-member cluster is 125,000 comparisons for a number
#: that is already an alarm, not a measurement.
EXACT_PAIRWISE_LIMIT = 60


def signature(claim: DraftClaim) -> tuple:
    """The exact-match part of cluster identity.

    Everything in here must be IDENTICAL for two claims to be considered for
    merging, before any similarity is measured:

    * ``claim_type`` -- a legal claim and an empirical one about the same
      subject are answered from different sources and cannot share a verdict.
    * ``ambiguous_stance`` -- a sarcastic restatement is not the same assertion
      as a sincere one, and merging them would publish a verdict against words
      whose author may have meant the opposite.
    * **anchors** -- "25,000 signatures" and "20,000 signatures" are different
      claims, and the number is exactly what the verdict turns on.
    * **citations named** -- a claim about 10 ILCS 5/10-2 and one about 5/10-3
      are answered from different sections.
    """
    return (
        claim.claim_type.value if claim.claim_type else "?",
        claim.ambiguous_stance,
        frozenset(extract_anchors(claim.text)),
        frozenset(str(citation) for citation in find_citations(claim.text)),
    )


def similarity(left: str, right: str) -> float:
    """F1 of stemmed content-token overlap between two claim texts.

    The same measure the fidelity gate uses, from the same tokenizer. One
    tokenizer rather than two on purpose: two would drift, and the quieter one
    would end up deciding something important without anyone re-reading it.
    """
    left_tokens, right_tokens = content_tokens(left), content_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    if not overlap:
        return 0.0
    coverage = overlap / len(left_tokens)
    precision = overlap / len(right_tokens)
    return 2 * coverage * precision / (coverage + precision)


@dataclass(slots=True)
class Cluster:
    """A group of claims that say the same thing."""

    id: str
    #: The claim actually sent to retrieval, adjudication and the red team.
    #: Always the first-seen member, so the choice is reproducible.
    representative: DraftClaim
    members: list[DraftClaim] = field(default_factory=list)
    #: Lowest pairwise similarity inside the cluster. 1.0 for a singleton.
    min_similarity: float = 1.0

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def is_singleton(self) -> bool:
        return self.size == 1

    def to_ref(self) -> ClusterRef:
        return ClusterRef(
            id=self.id, members=self.size, min_similarity=round(self.min_similarity, 4)
        )


@dataclass(slots=True)
class ClusterResult:
    """The grouping, plus the lookups the orchestrator needs."""

    clusters: list[Cluster] = field(default_factory=list)
    #: claim id -> cluster id.
    by_claim: dict[str, str] = field(default_factory=dict)
    #: claim id -> the id of the claim whose adjudication it inherits.
    represented_by: dict[str, str] = field(default_factory=dict)

    @property
    def representatives(self) -> list[DraftClaim]:
        """The claims that actually go downstream, in original order."""
        return [cluster.representative for cluster in self.clusters]

    @property
    def merged_count(self) -> int:
        """How many claims were spared a round trip."""
        return sum(cluster.size - 1 for cluster in self.clusters)

    def ref_for(self, claim_id: str) -> ClusterRef | None:
        """The published cluster record for a claim, or None if it stands alone.

        Singletons deliberately get ``None`` rather than a cluster of one. The
        overwhelmingly common case should add nothing to a record, and a
        ``ClusterRef`` present in a record should mean "this verdict was reached
        on another claim's text" -- a fact worth its own field.
        """
        cluster = self._cluster(claim_id)
        return None if cluster is None or cluster.is_singleton else cluster.to_ref()

    def _cluster(self, claim_id: str) -> Cluster | None:
        cluster_id = self.by_claim.get(claim_id)
        return next((c for c in self.clusters if c.id == cluster_id), None)


def _min_pairwise(members: list[DraftClaim]) -> float:
    """Lowest similarity between any two members.

    Computed over every pair for a normal cluster, because the published number
    claims to be the minimum and a sampled minimum that called itself exact
    would be a small lie in a field whose whole job is letting a reader check
    the grouping. Above :data:`EXACT_PAIRWISE_LIMIT` it falls back to
    representative-to-member distances, which is an UPPER bound -- so the note
    that carries it says so.
    """
    if len(members) < 2:
        return 1.0
    if len(members) > EXACT_PAIRWISE_LIMIT:
        head = members[0]
        return min(similarity(head.text, other.text) for other in members[1:])
    return min(
        similarity(left.text, right.text) for left, right in combinations(members, 2)
    )


def run_cluster(
    claims: list[DraftClaim],
    *,
    threshold: float = CLUSTER_THRESHOLD,
) -> StageOutcome[ClusterResult]:
    """Group near-identical claims. Deterministic; makes no model call.

    Greedy and first-fit: each claim joins the first existing cluster whose
    REPRESENTATIVE it matches, and starts a new one otherwise. Not the grouping
    an optimal clusterer would find, and that is fine -- what matters is that
    the same input always produces the same grouping, and that membership is
    decided against one fixed text rather than against a centroid that moves as
    members are added. A moving centroid is how a cluster drifts: each new
    member is close to the last one, and the tenth is nothing like the first.

    Gated-out claims are never clustered. Their verdict comes from the gate
    rather than from evidence, so there is nothing to save and nothing to share.
    """
    outcome: StageOutcome[ClusterResult] = StageOutcome(value=ClusterResult())
    result = outcome.value

    #: signature -> clusters carrying it, so only plausible candidates are compared.
    buckets: dict[tuple, list[Cluster]] = {}

    for claim in claims:
        if claim.gated_out:
            cluster = Cluster(
                id=f"k-{len(result.clusters) + 1:03d}", representative=claim,
                members=[claim],
            )
            result.clusters.append(cluster)
            result.by_claim[claim.id] = cluster.id
            result.represented_by[claim.id] = claim.id
            continue

        key = signature(claim)
        joined = False
        for cluster in buckets.get(key, ()):
            score = similarity(cluster.representative.text, claim.text)
            if score >= threshold:
                cluster.members.append(claim)
                result.by_claim[claim.id] = cluster.id
                result.represented_by[claim.id] = cluster.representative.id
                joined = True
                break

        if joined:
            continue

        cluster = Cluster(
            id=f"k-{len(result.clusters) + 1:03d}", representative=claim, members=[claim],
        )
        result.clusters.append(cluster)
        buckets.setdefault(key, []).append(cluster)
        result.by_claim[claim.id] = cluster.id
        result.represented_by[claim.id] = claim.id

    weak: list[str] = []
    sampled: list[str] = []
    for cluster in result.clusters:
        cluster.min_similarity = _min_pairwise(cluster.members)
        if cluster.is_singleton:
            continue
        if len(cluster.members) > EXACT_PAIRWISE_LIMIT:
            sampled.append(cluster.id)
        if cluster.min_similarity < REVIEW_SIMILARITY:
            weak.append(f"{cluster.id} ({cluster.size} claims, min {cluster.min_similarity:.2f})")

    if result.merged_count:
        outcome.note(
            f"clustered {len(claims)} claim(s) into {len(result.clusters)} group(s); "
            f"{result.merged_count} near-duplicate(s) inherit another claim's verdict "
            "rather than being adjudicated separately. Every clustered claim carries "
            "its group's size and weakest pairwise similarity, so the grouping can be "
            "checked rather than trusted."
        )
    else:
        outcome.note(f"no near-duplicate claims found among {len(claims)}")

    if weak:
        outcome.note(
            "cluster(s) whose weakest pair only just cleared the threshold, worth "
            "reading first: " + ", ".join(weak)
        )
    if sampled:
        outcome.note(
            f"cluster(s) {', '.join(sampled)} exceed {EXACT_PAIRWISE_LIMIT} members, so "
            "their reported min_similarity is measured against the representative "
            "rather than over every pair. It is an UPPER bound on the true minimum."
        )
    return outcome


__all__ = [
    "CLUSTER_THRESHOLD",
    "EXACT_PAIRWISE_LIMIT",
    "REVIEW_SIMILARITY",
    "Cluster",
    "ClusterResult",
    "run_cluster",
    "signature",
    "similarity",
]
