"""Core analysis models: documents, citations, claims, results.

These are the objects the pipeline produces and the ledger records. Two
design rules run through all of them.

**Rule 1 -- the schema enforces the contract, not the prompt.**
Every gate from docs/01 that can be expressed as a structural constraint is
expressed here as a validator. A prompt asking a model politely not to
return SUPPORTED without a citation is a suggestion; a validator that
rejects the object is a guarantee. When a model violates one of these, the
provider layer sees a ``ValidationError`` and retries, which is exactly the
behavior we want.

**Rule 2 -- models are frozen.**
Every model is immutable. A run record is evidence, and evidence that can be
mutated after the fact by a stray assignment somewhere in the pipeline is
not evidence. Building a modified copy requires ``model_copy(update=...)``,
which is visible in a diff and in a code review.

``extra="forbid"`` is set everywhere for the same reason: a model that
returns an unexpected key is not "mostly right", it is out of contract, and
silently dropping the key would hide the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from abca.schema.enums import (
    ClaimType,
    InputKind,
    RedTeamSeverity,
    SourceTier,
    Verdict,
)

# A digest string as produced by abca.canonical. Validated by pattern rather
# than by a custom type so that the constraint shows up in the exported JSON
# Schema, which third-party verifiers read.
Digest = Annotated[
    str,
    Field(pattern=r"^sha256:[0-9a-f]{64}$", description="Prefixed lowercase SHA-256 digest"),
]

#: Confidence and similarity scores are bounded probabilities. The bound is
#: enforced because an out-of-range confidence is a sign the model is
#: free-associating rather than scoring.
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]


class ABCAModel(BaseModel):
    """Shared base: frozen, strict about unknown keys, JSON-dumpable.

    ``to_jsonable`` is the single conversion point between pydantic objects
    and the plain JSON types :func:`abca.canonical.canonical_json` accepts.
    Centralizing it means datetimes and enums are converted in exactly one
    place, so the conversion rules cannot drift between call sites.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        use_enum_values=False,
        validate_assignment=True,
    )

    def to_jsonable(self) -> dict[str, Any]:
        """Return a plain-JSON-typed dict (enums -> values, datetimes -> strings)."""
        return self.model_dump(mode="json")


def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC.

    Naive datetimes are rejected rather than assumed to be local time.
    A run record timestamped in an unknown zone is a record whose ordering
    relative to other records cannot be established, which defeats the point
    of recording it. Callers must be explicit.
    """
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime is not accepted; timestamps must be timezone-aware "
            "so that records from different machines remain comparable"
        )
    return value.astimezone(UTC)


class Citation(ABCAModel):
    """A single piece of cited evidence.

    The four audit fields -- ``tier``, ``url``, ``retrieved_at`` and
    ``content_hash`` -- are what make a citation checkable rather than
    decorative. ``quote`` is required because a citation to a 400-page
    statute that does not say which passage it relies on is not a citation.
    """

    tier: SourceTier = Field(
        description=(
            "Evidence tier. Set by the CONNECTOR that produced this citation, "
            "never by a model. See SourceTier docstring."
        )
    )
    title: str = Field(min_length=1, description="Human-readable source title.")
    url: str = Field(min_length=1, description="Canonical locator for the source.")
    quote: str = Field(
        min_length=1,
        description=(
            "The exact passage relied upon, verbatim. Required: an unquoted "
            "citation cannot be checked without re-reading the whole source."
        ),
    )
    retrieved_at: datetime = Field(
        description="When this source was fetched. A verdict is only valid against its snapshot."
    )
    content_hash: Digest = Field(
        description=(
            "Digest of the source content as retrieved. Comparing this against "
            "a fresh fetch is how DRIFTED is detected during verification."
        )
    )
    locator: str | None = Field(
        default=None,
        description="Optional in-source pointer: section, page, paragraph, docket entry.",
    )

    _normalize_retrieved_at = field_validator("retrieved_at")(_as_utc)

    @property
    def supports_verdict(self) -> bool:
        """Whether this citation is strong enough to carry SUPPORTED/CONTRADICTED."""
        return self.tier.can_support_verdict

    def semantic_key(self) -> tuple[str, str, str]:
        """Stable identity used when comparing two runs' citations.

        Deliberately excludes ``retrieved_at`` and ``quote``: a replay that
        cites the same tier of the same content at a different second is the
        same citation. Including the timestamp would make EQUIVALENT
        unreachable.
        """
        return (self.tier.value, self.url, self.content_hash)


class RedTeamFinding(ABCAModel):
    """Output of the mandatory adversarial pass (contract s7).

    This ships inside the claim, not in a log. A verdict published without its
    red-team findings is a verdict published without the part that makes it
    honest.

    COUNTER-EVIDENCE IS VERIFIED LIKE ANY OTHER EVIDENCE
    ----------------------------------------------------
    ``counter_citations`` carries the same :class:`Citation` objects the
    adjudicator's evidence does, produced by the same verification path: the
    red team names a retrieved source and quotes it, the quote is checked
    verbatim, and the tier comes from the connector.

    Without that, the red team would be a fabrication channel -- a model could
    "undercut" any verdict with invented counter-evidence, which is precisely
    the hole the adjudication stage closes. An adversarial pass that can assert
    freely is not a check; it is a second, unchecked opinion.

    Purely analytical objections ("the statute does not address this at all, so
    the verdict rests on inference") need no citation and carry none. The
    distinction is whether the objection claims the SOURCE says something.
    """

    counter_evidence: str = Field(
        description="Strongest available evidence against the adjudicated verdict."
    )
    steelman: str = Field(
        description="Best good-faith case for the position the analysis went against."
    )
    overreach_flags: list[str] = Field(
        default_factory=list,
        description="Places the analysis reached past what its sources support.",
    )
    counter_citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "Verified support for the counter-evidence. Built by the same "
            "verification path as any other citation: quote checked verbatim, "
            "tier from the connector."
        ),
    )
    severity: RedTeamSeverity = Field(
        default=RedTeamSeverity.NONE,
        description=(
            "How far the objection reaches. Only MATERIAL triggers an automatic "
            "verdict downgrade; NOTED and MINOR are recorded for the reader and "
            "change nothing."
        ),
    )
    independent: bool = Field(
        default=True,
        description=(
            "False when the red team ran on the SAME model that produced the "
            "verdict. A model reviewing its own reasoning is the one least able "
            "to see where it reached, so a reader weighing this finding needs "
            "to know."
        ),
    )
    verdict_downgraded_from: Verdict | None = Field(
        default=None,
        description=(
            "Set when the red-team pass materially undercut the original verdict "
            "and it was automatically downgraded. Recorded so the downgrade is "
            "visible rather than invisible."
        ),
    )

    @model_validator(mode="after")
    def _material_findings_need_substance(self) -> Self:
        """A MATERIAL objection must say something specific.

        MATERIAL is the only severity that changes a verdict, so it is the only
        one worth gaming. Requiring substance keeps "this could be wrong" from
        being enough to overturn a cited finding.
        """
        if self.severity is RedTeamSeverity.MATERIAL:
            has_substance = (
                bool(self.counter_citations)
                or bool(self.overreach_flags)
                or len(self.counter_evidence.strip()) >= 40
            )
            if not has_substance:
                raise ValueError(
                    "a MATERIAL red-team finding must cite counter-evidence, flag a "
                    "specific overreach, or state a substantive objection; it is the "
                    "only severity that overturns a verdict."
                )
        return self


class ClusterRef(ABCAModel):
    """Membership record for a claim that represents a cluster of near-duplicates.

    The scaling answer for large comment threads (architecture spec s3.5).
    Membership is published so a reader can check the grouping rather than
    take it on trust -- bad clustering would let one verdict be attributed to
    claims that do not actually say the same thing.
    """

    id: str = Field(min_length=1, description="Cluster identifier, unique within the run.")
    members: int = Field(ge=1, description="Number of source claims in this cluster.")
    min_similarity: UnitInterval = Field(
        description="Lowest pairwise similarity inside the cluster. Low values warrant review."
    )


class Claim(ABCAModel):
    """One atomic assertion extracted from the input, with its adjudication.

    The validators below implement the hard gates from the prompt contract.
    Read them as the machine-checkable subset of docs/01.
    """

    id: str = Field(min_length=1, description="Claim identifier, unique within the run.")
    text: str = Field(min_length=1, description="The claim as extracted, one assertion.")
    span_start: int | None = Field(default=None, ge=0, description="Offset into the source document.")
    span_end: int | None = Field(default=None, ge=0, description="End offset, exclusive.")

    claim_type: ClaimType = Field(description="Classification. Determines verdict eligibility.")
    verdict: Verdict = Field(description="Adjudication outcome from the closed vocabulary.")
    confidence: UnitInterval = Field(
        description="Calibrated confidence. Must track evidence quality, not fluency."
    )
    evidence_quality: SourceTier | None = Field(
        default=None,
        description="Best tier reached. None when no evidence was retrieved at all.",
    )
    model_disagreement: UnitInterval | None = Field(
        default=None,
        description="Cross-model variance in consensus mode. None when a single model ran.",
    )

    reasoning: str = Field(
        default="",
        description=(
            "Prose explanation. EXCLUDED from the semantic digest, because two "
            "runs that reach the same verdict from the same citations agree even "
            "if they word it differently."
        ),
    )
    citations: list[Citation] = Field(default_factory=list)
    cluster: ClusterRef | None = Field(default=None)
    red_team: RedTeamFinding | None = Field(default=None)

    # ---------------------------------------------------------------- validators

    @model_validator(mode="after")
    def _check_span(self) -> Self:
        """Spans must be well-formed if present at all."""
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("span_start and span_end must both be set or both be omitted")
        if (
            self.span_start is not None
            and self.span_end is not None
            and self.span_end <= self.span_start
        ):
            raise ValueError(
                f"span_end ({self.span_end}) must be greater than "
                f"span_start ({self.span_start})"
            )
        return self

    @model_validator(mode="after")
    def _enforce_evidence_gate(self) -> Self:
        """Contract s2: SUPPORTED/CONTRADICTED/MIXED require a T0-T2 citation.

        This is the load-bearing validator of the whole system. Without it,
        a fluent model can assert anything and label it SUPPORTED.
        """
        if not self.verdict.requires_supporting_evidence:
            return self

        qualifying = [c for c in self.citations if c.supports_verdict]
        if not qualifying:
            tiers = sorted({c.tier.value for c in self.citations}) or ["<none>"]
            raise ValueError(
                f"verdict {self.verdict.value} requires at least one T0-T2 citation, "
                f"but claim {self.id!r} cites only: {', '.join(tiers)}. "
                "Per contract s2, T3 alone caps the verdict at REPORTED_UNVERIFIED "
                "and T4 can never raise it above UNSUPPORTED."
            )
        return self

    @model_validator(mode="after")
    def _enforce_type_gate(self) -> Self:
        """Contract s3: non-eligible claim types are never adjudicated true or false.

        PREDICTIVE, NORMATIVE and DEFINITIONAL claims resolve to UNVERIFIABLE
        (or OUT_OF_SCOPE if they turned out not to be political at all). Their
        premises are extracted as separate claims and adjudicated on their own.
        """
        if self.claim_type.is_verdict_eligible:
            return self

        allowed = {Verdict.UNVERIFIABLE, Verdict.OUT_OF_SCOPE}
        if self.verdict not in allowed:
            raise ValueError(
                f"claim {self.id!r} is {self.claim_type.value}, which evidence cannot "
                f"settle, so its verdict must be one of "
                f"{sorted(v.value for v in allowed)}, not {self.verdict.value}. "
                "Extract its factual premises as separate claims instead (contract s3)."
            )
        return self

    @model_validator(mode="after")
    def _enforce_reported_unverified(self) -> Self:
        """Contract s2: REPORTED_UNVERIFIED means T3-only support, by definition.

        If a T0-T2 citation exists, the correct verdict is one of the
        evidence-backed ones, and returning REPORTED_UNVERIFIED understates
        what the run actually found.
        """
        if self.verdict is not Verdict.REPORTED_UNVERIFIED:
            return self
        if any(c.supports_verdict for c in self.citations):
            raise ValueError(
                f"claim {self.id!r} is REPORTED_UNVERIFIED but cites T0-T2 evidence; "
                "use an evidence-backed verdict instead (contract s2)."
            )
        if not any(c.tier is SourceTier.T3 for c in self.citations):
            raise ValueError(
                f"claim {self.id!r} is REPORTED_UNVERIFIED but cites no T3 source; "
                "with no qualifying source at all the verdict is UNSUPPORTED."
            )
        return self

    @model_validator(mode="after")
    def _enforce_evidence_quality_consistency(self) -> Self:
        """``evidence_quality`` must equal the best tier actually cited.

        A mismatch means the field was hallucinated rather than computed, and
        every downstream confidence gate reads this field.
        """
        best = min((c.tier for c in self.citations), key=lambda t: t.rank, default=None)
        if self.evidence_quality != best:
            raise ValueError(
                f"claim {self.id!r} declares evidence_quality="
                f"{self.evidence_quality.value if self.evidence_quality else None} "
                f"but its strongest citation is {best.value if best else None}. "
                "This field is computed from citations, not asserted."
            )
        return self

    # ------------------------------------------------------------------ helpers

    def semantic_projection(self) -> dict[str, Any]:
        """The parts of this claim that two runs must agree on to be EQUIVALENT.

        Deliberately excludes ``reasoning`` (prose varies), ``red_team`` prose,
        ``span`` offsets (whitespace normalization can shift them), and
        ``retrieved_at`` inside citations.

        ``confidence`` is rounded to two decimals rather than compared exactly.
        Two runs of the same model that differ by 0.003 in a self-reported
        confidence score agree in every sense a reader cares about, and
        treating that as DIVERGENT would make the strictest outcome fire on
        noise -- which trains people to ignore it.
        """
        return {
            "id": self.id,
            "claim_type": self.claim_type.value,
            "verdict": self.verdict.value,
            "confidence": round(self.confidence, 2),
            "evidence_quality": self.evidence_quality.value if self.evidence_quality else None,
            # Sorted so that citation ordering, which carries no meaning, does
            # not make two identical analyses look different.
            "citations": sorted(c.semantic_key() for c in self.citations),
        }


class DocumentRef(ABCAModel):
    """Provenance record for the analyzed input.

    Note that raw content is NOT stored here. The ledger records the hash of
    what was analyzed, not the text itself. That keeps published run records
    small, avoids republishing someone else's copyrighted article, and
    avoids the ledger becoming an archive of other people's posts. Content
    is kept in the local cache, addressed by this hash.
    """

    kind: InputKind = Field(description="How the input was supplied.")
    locator: str = Field(
        description=(
            "Where it came from: a URL, a file path, a handle, or the literal "
            "'<inline>' for -t/--stdin text."
        )
    )
    content_hash: Digest = Field(description="Digest of the normalized document text.")
    retrieved_at: datetime = Field(description="When the input was read or fetched.")
    byte_length: int = Field(ge=0, description="Length of the normalized text in UTF-8 bytes.")
    normalization: str = Field(
        default="NFC",
        description=(
            "Unicode normalization applied at ingest. Recorded because the "
            "content hash is a hash of the NORMALIZED text; a verifier must "
            "apply the same normalization to reproduce it."
        ),
    )

    _normalize_retrieved_at = field_validator("retrieved_at")(_as_utc)


class InfluencePattern(ABCAModel):
    """A matched influence-operation narrative pattern (contract s6).

    ``attribution`` exists and is always ``None``. It is present in the
    schema on purpose, as a permanent, visible reminder that this system
    identifies patterns and never names actors: open-source data cannot
    support attribution, and asserting it would be exactly the kind of
    unsupported claim the Analyzer exists to catch.
    """

    pattern: str = Field(min_length=1, description="The narrative pattern matched.")
    documented_in: Citation = Field(
        description=(
            "The T0-T2 source that documents this pattern. Required: the tool "
            "reports patterns that a citable authority has described, not "
            "patterns it invents."
        )
    )
    attribution: None = Field(
        default=None,
        description="Always null. Actor attribution is out of scope by design (contract s6).",
    )
    note: str = Field(
        default="Pattern match only. No actor attribution is made or implied.",
        description="Reader-facing caveat carried with every pattern match.",
    )

    @model_validator(mode="after")
    def _require_citable_authority(self) -> Self:
        """The documenting source must itself be T0-T2.

        A pattern claim sourced to a blog is a rumor about a rumor.
        """
        if not self.documented_in.supports_verdict:
            raise ValueError(
                "influence patterns must cite a T0-T2 source documenting the "
                f"pattern; got {self.documented_in.tier.value}."
            )
        return self


class FidelityReport(ABCAModel):
    """Result of the plain-language back-translation gate (contract s5).

    Only populated when legal text was rendered into plain language.
    """

    applied: bool = Field(description="Whether the gate ran at all.")
    score: UnitInterval = Field(description="Elements preserved / elements in source.")
    reading_grade: float = Field(ge=0.0, description="Flesch-Kincaid grade of the rendering.")
    elements_source: int = Field(ge=0, description="Legal elements extracted in pass A.")
    elements_preserved: int = Field(ge=0, description="Elements recovered by pass C back-translation.")
    modal_verbs_preserved: bool = Field(
        description="Whether shall/may/must-not strength survived the rewrite."
    )
    regenerations: int = Field(ge=0, description="How many times pass B had to be redone.")
    unresolved: bool = Field(
        default=False,
        description=(
            "True when three regenerations failed and verbatim text was emitted "
            "instead. This is an acceptable, reachable outcome, not an error."
        ),
    )

    @model_validator(mode="after")
    def _check_counts(self) -> Self:
        if self.elements_preserved > self.elements_source:
            raise ValueError(
                f"elements_preserved ({self.elements_preserved}) exceeds "
                f"elements_source ({self.elements_source}); the back-translation "
                "recovered elements the source does not contain, which means "
                "pass C invented content."
            )
        return self


class AnalysisResult(ABCAModel):
    """Everything the pipeline produced for one run.

    This is the payload the ledger wraps. It contains no timing or
    environment data -- those live on the run record, so that this object can
    be digested for reproducibility without volatile fields leaking in.
    """

    document: DocumentRef
    claims: list[Claim] = Field(default_factory=list)
    influence_patterns: list[InfluencePattern] = Field(default_factory=list)
    fidelity: FidelityReport | None = Field(default=None)
    notes: list[str] = Field(
        default_factory=list,
        description="Operator-facing warnings: truncation, budget stops, degraded retrieval.",
    )

    @model_validator(mode="after")
    def _unique_claim_ids(self) -> Self:
        """Claim ids must be unique, because the semantic digest is keyed on them."""
        seen: set[str] = set()
        for claim in self.claims:
            if claim.id in seen:
                raise ValueError(f"duplicate claim id {claim.id!r} in analysis result")
            seen.add(claim.id)
        return self

    def semantic_projection(self) -> dict[str, Any]:
        """The comparison surface for EQUIVALENT verification.

        Claims are sorted by id so that ordering differences between runs --
        which can arise from concurrency in the retrieval stage -- do not
        register as disagreement.
        """
        return {
            "document": self.document.content_hash,
            "claims": sorted(
                (c.semantic_projection() for c in self.claims),
                key=lambda projection: projection["id"],
            ),
            "influence_patterns": sorted(
                p.pattern for p in self.influence_patterns
            ),
            "fidelity_score": (
                round(self.fidelity.score, 2) if self.fidelity and self.fidelity.applied else None
            ),
        }

    def cited_source_hashes(self) -> dict[str, str]:
        """Map of cited URL -> content hash, for drift detection.

        If the same URL appears with two different hashes inside one run, the
        source changed mid-run; the first hash wins and the collision is
        surfaced by the caller as a note rather than silently merged.
        """
        hashes: dict[str, str] = {}
        for claim in self.claims:
            for citation in claim.citations:
                hashes.setdefault(citation.url, citation.content_hash)
        for pattern in self.influence_patterns:
            hashes.setdefault(pattern.documented_in.url, pattern.documented_in.content_hash)
        return hashes

    def verdict_counts(self) -> dict[str, int]:
        """Verdict histogram, used in report summaries and in the symmetry eval."""
        counts: dict[str, int] = {}
        for claim in self.claims:
            counts[claim.verdict.value] = counts.get(claim.verdict.value, 0) + 1
        return counts

    def type_counts(self) -> dict[str, int]:
        """Claim-type histogram."""
        counts: dict[str, int] = {}
        for claim in self.claims:
            counts[claim.claim_type.value] = counts.get(claim.claim_type.value, 0) + 1
        return counts


__all__ = [
    "ABCAModel",
    "AnalysisResult",
    "Citation",
    "Claim",
    "ClusterRef",
    "Digest",
    "DocumentRef",
    "FidelityReport",
    "InfluencePattern",
    "RedTeamFinding",
    "UnitInterval",
]
