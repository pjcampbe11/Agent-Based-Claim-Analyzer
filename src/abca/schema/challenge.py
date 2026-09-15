"""Schema for the challenge lane and its promotion gate (doc 20).

WHAT PROMOTION MEANS
====================
*A source now exists for this claim, so it can be adjudicated properly.*

That is a narrow statement and the narrowness is the whole design. Promotion does
NOT mean the claim is true, does not mean the reconstruction was right, and does
not change what is being adjudicated. It means a document was found that a
pre-registered branch outcome said would be decisive, and the claim -- the
original one, unmodified -- can now go through Lane A like any sourced claim.

THE SUBSTITUTION RULE IS THE ONE THAT MATTERS
=============================================
Doc 20 s4. The claim that enters Lane A is the denatured claim from B2,
character for character. Never the reconstruction.

The failure it prevents is subtle and would be devastating. A post says
*"Congress just voted to gut veterans' benefits."* Reconstruction identifies a
motion to recommit on an appropriations vehicle. Retrieval pulls that roll call.
If the promoted claim became *"the House rejected a motion to recommit H.R. ____
on [date]"*, the Analyzer would adjudicate THAT -- and return SUPPORTED,
correctly, about a sentence nobody posted. The reader would see a green check
next to a claim they never made.

So the retrieved artifact becomes EVIDENCE and the original sentence stays the
CLAIM. That is the ordinary relationship between a source and an assertion, and
the lane exists to establish it, not to bypass it.

Enforced by hashing the denatured claim at B2 and refusing promotion when the
hash differs at B5. :meth:`PromotionGate.evaluate` will not return PROMOTED on a
hash mismatch under any combination of the other three conditions.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from abca.schema.core import ABCAModel, Citation, _as_utc
from abca.schema.enums import SourceTier, Verdict


class PromotionGrade(StrEnum):
    """The outcome of the promotion gate (doc 20 s3)."""

    PROMOTED = "PROMOTED"              # All four conditions hold.
    PROMOTED_WEAK = "PROMOTED_WEAK"    # P3 partial: 3 of 4 particulars matched.
    DORMANT = "DORMANT"                # No artifact yet; one plausibly will exist.
    UNPROMOTABLE = "UNPROMOTABLE"      # Plan exhausted; no artifact, none coming.
    REJECTED = "REJECTED"              # Unfalsifiable, normative, or an M6 halt.

    @property
    def enters_lane_a(self) -> bool:
        """Whether this outcome sends the claim on for adjudication."""
        return self in {PromotionGrade.PROMOTED, PromotionGrade.PROMOTED_WEAK}

    @property
    def is_terminal(self) -> bool:
        """Whether the challenge is finished. DORMANT is the one that is not.

        A dormant challenge is the quietly valuable state: a large share of viral
        claims are about things that have not happened yet, so the honest answer
        today is "no source exists" and the honest answer in six weeks may differ.
        """
        return self is not PromotionGrade.DORMANT


class Particular(StrEnum):
    """The four identifying details that tie an artifact to a post (doc 20, P3).

    A topical match is not a confirmation. "This document is also about veterans'
    benefits" is true of thousands of documents; what confirms a referent is that
    the actor, the timing, the jurisdiction and the subject all line up.
    """

    ACTOR = "actor"
    DATE_WINDOW = "date_window"
    JURISDICTION = "jurisdiction"
    SUBJECT_MATTER = "subject_matter"


#: All four must match for a strict promotion. Three gives PROMOTED_WEAK.
STRICT_PARTICULARS = len(Particular)
WEAK_PARTICULARS = STRICT_PARTICULARS - 1


class ConfirmationParticular(ABCAModel):
    """One identifying detail, matched or not, with the passage that shows it."""

    particular: Particular
    matched: bool
    quote: str = Field(
        default="",
        description="The passage in the artifact that establishes the match.",
    )
    note: str = Field(default="", description="Why it did not match, when it did not.")

    @model_validator(mode="after")
    def _a_match_must_show_its_work(self) -> ConfirmationParticular:
        """An unquoted match is an assertion that the artifact fits.

        The gate counts these, so a match nobody can check is a vote nobody can
        audit. Requiring the quote is what makes P3 a confirmation rather than a
        claim of confirmation.
        """
        if self.matched and not self.quote.strip():
            raise ValueError(
                f"particular {self.particular.value} is marked matched but quotes "
                "nothing from the artifact. A confirmation without the passage that "
                "confirms it is an assertion, and the gate counts it as evidence."
            )
        if not self.matched and not self.note.strip():
            raise ValueError(
                f"particular {self.particular.value} did not match and gives no "
                "reason; the published record needs to say what failed to line up."
            )
        return self


class RetrievedArtifact(ABCAModel):
    """The document the executor found. P1's subject.

    Not a search result, not a summary -- the document, with the hash and
    timestamp that make it checkable later.
    """

    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tier: SourceTier
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retrieved_at: datetime
    connector: str = Field(min_length=1)
    query: str = Field(default="", description="The query that found it.")

    # Naive datetimes are rejected rather than assumed local, exactly as on
    # Citation: an artifact timestamped in an unknown zone cannot be compared
    # against a claim's date window, which is one of the four particulars.
    _normalize_retrieved_at = field_validator("retrieved_at")(_as_utc)

    @model_validator(mode="after")
    def _artifact_must_be_admissible(self) -> RetrievedArtifact:
        if not self.tier.can_support_verdict:
            raise ValueError(
                f"artifact is {self.tier.value}; promotion requires T0-T2. A T3 or T4 "
                "document cannot carry a verdict, so promoting a claim onto one would "
                "produce an adjudication that could never resolve above "
                "REPORTED_UNVERIFIED anyway."
            )
        return self

    def as_citation(self, quote: str) -> Citation:
        return Citation(
            tier=self.tier, title=self.title, url=self.url, quote=quote,
            retrieved_at=self.retrieved_at, content_hash=self.content_hash,
        )


class Widening(ABCAModel):
    """One bounded expansion of a failed query, recorded (doc 20 s6).

    Every widening is logged because unbounded search is how an executor
    eventually finds *something* for any claim whatsoever. The record lets a
    reader see how hard the system had to look, which is itself information about
    how well-founded the promotion is.
    """

    axis: str = Field(min_length=1)
    from_value: str = ""
    to_value: str = ""
    query: str = Field(min_length=1)
    found: bool = False


class PromotedFrom(ABCAModel):
    """Provenance carried into Lane A (doc 20 s4).

    Never dropped. Any published verdict on a promoted claim states, in the
    reader's language, that the post cited nothing and the source was located by
    reconstruction. A verdict presenting a reconstructed source as though the
    post had supplied it is a misrepresentation, and it is exactly the kind that
    would be found and used against whoever published it.
    """

    challenge_id: str = Field(min_length=1)
    lane: str = Field(default="B", pattern=r"^B$")
    denatured_claim_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    referent_candidate_used: str = ""
    distortion_applied: str = ""
    referent_confidence: str = ""
    confirmation_particulars: tuple[ConfirmationParticular, ...] = ()
    branch_outcome_matched: str = ""
    consensus_models: tuple[str, ...] = ()
    queries_executed: int = Field(default=0, ge=0)
    widenings: tuple[Widening, ...] = ()
    promotion_grade: PromotionGrade
    human_promoted: bool = False
    operator: str = ""

    #: Fixed text, not model-composed. A model asked to phrase this each time
    #: would eventually phrase it softly, and the sentence exists precisely to
    #: not be soft.
    DISCLOSURE: str = Field(
        default=(
            "The post cited no source. This source was located by reconstructing "
            "what the post most likely referred to, and the reconstruction is "
            "published alongside the verdict so it can be checked."
        ),
        frozen=True,
    )

    @model_validator(mode="after")
    def _a_human_promotion_names_its_operator(self) -> PromotedFrom:
        if self.human_promoted and not self.operator.strip():
            raise ValueError(
                "a human promotion must record who performed it. An unattributed "
                "override of the gate is exactly what the gate exists to prevent."
            )
        return self


class ChallengeOutcome(ABCAModel):
    """The published result of one challenge (doc 20 s8).

    Published whether or not it promoted, and the UNPROMOTABLE ones matter most.
    A public record of *"we tried to find a source for this, here is exactly what
    we searched, we did not find one"* is a stronger and more falsifiable artifact
    than a verdict, because anyone can run the same queries and check.
    """

    challenge_id: str = Field(min_length=1)
    denatured_claim: str = Field(min_length=1)
    denatured_claim_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    grade: PromotionGrade
    reasons: tuple[str, ...] = ()

    artifact: RetrievedArtifact | None = None
    particulars: tuple[ConfirmationParticular, ...] = ()
    branch_outcome_matched: str = ""

    candidates_considered: tuple[str, ...] = ()
    candidates_rejected: tuple[str, ...] = ()
    queries_executed: tuple[str, ...] = ()
    widenings: tuple[Widening, ...] = ()

    #: Set on DORMANT. The record shows what the analysis is waiting on.
    recheck_after: date | None = None
    recheck_trigger: str = ""

    tokens_spent: int = Field(default=0, ge=0)
    wall_clock_ms: int = Field(default=0, ge=0)
    budget_exhausted: bool = False

    #: The dual verdict (doc 20 s5). Populated after Lane A runs.
    referent_verdict: Verdict | None = None
    characterization_verdict: Verdict | None = None

    @model_validator(mode="after")
    def _outcome_is_coherent(self) -> ChallengeOutcome:
        if self.grade.enters_lane_a and self.artifact is None:
            raise ValueError(
                f"grade {self.grade.value} requires an artifact: promotion means a "
                "source was found, and a promotion with no document is a claim that "
                "one exists."
            )
        if self.grade is PromotionGrade.DORMANT and self.recheck_after is None:
            raise ValueError(
                "a DORMANT challenge must carry a re-check date. Dormancy without a "
                "trigger is an unpromotable challenge that nobody will revisit, "
                "labelled optimistically."
            )
        if self.grade is not PromotionGrade.DORMANT and self.recheck_after is not None:
            raise ValueError(
                f"grade {self.grade.value} is terminal and must not carry a re-check date"
            )
        if not self.grade.enters_lane_a and self.characterization_verdict is not None:
            raise ValueError(
                "a characterization verdict requires a promoted claim: you cannot show "
                "a distortion without the artifact that was distorted (doc 20 s5)."
            )
        return self

    @property
    def headline_verdict(self) -> Verdict | None:
        """The verdict a reader asked about (doc 20 s5).

        The CHARACTERIZATION, not the referent. The referent verdict is usually
        SUPPORTED and usually uninteresting -- the document says what it says.
        What the reader wants to know is whether the post was a fair rendering of
        it, and that is the second verdict.
        """
        return self.characterization_verdict or self.referent_verdict


__all__ = [
    "STRICT_PARTICULARS",
    "WEAK_PARTICULARS",
    "ChallengeOutcome",
    "ConfirmationParticular",
    "Particular",
    "PromotedFrom",
    "PromotionGrade",
    "RetrievedArtifact",
    "Widening",
]
