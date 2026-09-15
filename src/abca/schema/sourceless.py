"""Schema for the sourceless-claim dissection module (doc 18).

WHAT THIS MODULE PROTECTS
=========================
The sourceless module reasons about a political claim that arrived with no link,
no citation, and no named source. It reconstructs what the claim is probably
*about* and hands that to retrieval as a search target.

It produces NO EVIDENCE. That sentence is the entire reason this file exists,
and every validator below enforces one corner of it:

* A bare social post is T4 (contract s2), and T4 can never raise a verdict above
  UNSUPPORTED. So ``evidence_quality`` is pinned to T4 unless a structural
  conflict carrying a real T0 citation was found.
* ``citations`` may appear in exactly ONE place in this object -- inside
  ``structural_conflict[]``. Nowhere else. A referent candidate cannot carry a
  citation, because a referent candidate is a guess, and a guess with a citation
  attached reads as a finding.
* ``CONTRADICTED`` is reachable only through that same cited conflict.
* Every array element that asserts something carries a ``register`` saying who
  is asserting it: the post, a source, or the model.

WHY THE REGISTER FIELD IS NOT OPTIONAL
======================================
The module mixes three kinds of statement in one object: what the post claimed,
what a citation established, and what the model inferred. Those look identical
in prose and are completely different in weight. A brief that renders an
inference in the same voice as a citation has laundered a guess into a fact,
which is the failure mode this project exists to detect in other people's work.

So ``register`` is required at the type level rather than requested in a prompt.
A model that omits it fails validation and is retried.
"""

from __future__ import annotations

import warnings
from enum import StrEnum

from pydantic import Field, model_validator

from abca.schema.core import ABCAModel, Citation
from abca.schema.enums import ClaimType, SourceTier, Verdict

# ``register`` is the field name doc 18 s8 specifies on the wire, and it shadows
# ``ModelMetaclass.register`` -- the ABC virtual-subclass registration hook that
# pydantic inherits and that nothing in this codebase calls. The field itself
# behaves correctly (instance access returns the value, serialization emits the
# key); only the shadow warning is a problem, because a warning printed on every
# import is a warning people learn to scroll past.
#
# Suppressed narrowly, by exact message and category, rather than by silencing
# UserWarning generally. tests/test_sourceless.py asserts that importing this
# module emits NO warnings, so a future field that shadows something REAL will
# still surface.
warnings.filterwarnings(
    "ignore",
    message=r'Field name "register" in "\w+" shadows an attribute in parent "ABCAModel"',
    category=UserWarning,
)

# ==========================================================================
# Vocabularies
# ==========================================================================


class Register(StrEnum):
    """Who is asserting a given line, and therefore how much it weighs.

    The three registers are never merged in output and never rendered in the
    same voice. See the module docstring.
    """

    ASSERTED = "ASSERTED"        # The post claims this. Not a finding.
    ESTABLISHED = "ESTABLISHED"  # A citation establishes this. Evidence.
    INFERRED = "INFERRED"        # The model is reasoning. Not evidence, ever.

    @property
    def is_evidence(self) -> bool:
        """Only ESTABLISHED carries weight, and only with a citation to back it."""
        return self is Register.ESTABLISHED


class ClaimSubtype(StrEnum):
    """Procedural granularity the base :class:`ClaimType` taxonomy does not carry.

    ``ClaimType`` answers "can evidence settle this?" -- the question the whole
    contract turns on. ``ClaimSubtype`` answers "what KIND of thing is being
    claimed?", which is what selects a connector and a context-pack entry.
    Keeping them separate means the contract taxonomy never has to grow a
    procedural branch it does not otherwise need.
    """

    FACTUAL_EVENT = "factual-event"
    ROLL_CALL = "roll-call"
    STATUTORY_CONTENT = "statutory-content"
    PROCEDURAL = "procedural"
    QUOTE = "quote"
    CAUSAL = "causal"
    STATISTICAL = "statistical"
    MOTIVE = "motive"

    @property
    def is_unfalsifiable(self) -> bool:
        """MOTIVE claims are not fact-checked (M7).

        "They did it because they hate X" has no observation that settles it.
        Fact-checking one produces a confident answer to an unanswerable
        question, which is worse than declining.
        """
        return self is ClaimSubtype.MOTIVE


class Verifiability(StrEnum):
    """What tier of source could settle this atom, if one were found."""

    VERIFIABLE_PRIMARY = "verifiable-primary"      # T0-T1 would settle it.
    VERIFIABLE_SECONDARY = "verifiable-secondary"  # T2-T3 is the best available.
    CONTESTED = "contested"                        # Sources genuinely disagree.
    UNFALSIFIABLE = "unfalsifiable"                # No observation settles it.


class ReferentConfidence(StrEnum):
    """How firmly the module believes it identified what the post refers to.

    Deliberately SEPARATE from the disposition's ``confidence``. The module is
    routinely certain a claim is unsupported while having no idea what it refers
    to, and the reverse also happens. Collapsing the two is precisely how a
    hypothesis gets read as a finding.
    """

    HIGH = "high"      # Structural reasoning settles the referent.
    MEDIUM = "medium"  # Strong pattern match; one confirmation needed.
    LOW = "low"        # Plausible, but multiple readings survive.
    NONE = "none"      # No defensible referent. Saying so is a valid answer.


#: The dispositions this module may emit (doc 18 s5).
#:
#: Note what is ABSENT: SUPPORTED, MIXED, MISLEADING_CONTEXT and
#: REPORTED_UNVERIFIED. Every one of them requires evidence the module does not
#: have. ``adjudicate`` assigns those after retrieval, and the challenge lane
#: (doc 20 s5) is what makes MISLEADING_CONTEXT reachable at all.
ALLOWED_DISPOSITIONS: frozenset[Verdict] = frozenset({
    Verdict.UNSUPPORTED,
    Verdict.UNVERIFIABLE,
    Verdict.CONTRADICTED,
    Verdict.OUT_OF_SCOPE,
})


class Severity(StrEnum):
    """How far a fallacy finding reaches. It never reaches the disposition."""

    MINOR = "minor"
    MATERIAL = "material"
    DISQUALIFYING = "disqualifying"


# ==========================================================================
# Elements
# ==========================================================================


class AtomicClaim(ABCAModel):
    """One indivisible assertion pulled out of the post.

    ``load_bearing`` is the field that earns its keep. Viral posts bundle two to
    five atoms where ONE is true and carries the rest; naming the carrier is
    what turns "this post is misleading" into "this specific sentence is doing
    the work, and here is why it does not follow."
    """

    id: str = Field(pattern=r"^a-\d{3}$")
    text: str = Field(min_length=1)
    claim_type: ClaimType
    subtype: ClaimSubtype
    verifiability: Verifiability
    load_bearing: bool = Field(
        description="Does the post's rhetorical force collapse if this atom fails?"
    )
    register: Register = Field(
        default=Register.ASSERTED,
        description="Always ASSERTED: an atomic claim is what the POST said.",
    )

    @model_validator(mode="after")
    def _atoms_are_what_the_post_said(self) -> AtomicClaim:
        if self.register is not Register.ASSERTED:
            raise ValueError(
                f"atom {self.id}: register must be ASSERTED. An atomic claim is a "
                "restatement of the post's own assertion; marking one ESTABLISHED "
                "or INFERRED would attribute the model's words to the poster."
            )
        if self.subtype.is_unfalsifiable and self.verifiability is not Verifiability.UNFALSIFIABLE:
            raise ValueError(
                f"atom {self.id}: subtype 'motive' must carry verifiability "
                "'unfalsifiable' (M7). A motive attribution has no observation "
                "that settles it and is not fact-checked."
            )
        return self


class ReferentCandidate(ABCAModel):
    """A hypothesis about the real event the post distorted.

    Carries NO citation field, by omission and on purpose. A candidate is the
    model's guess; attaching a source to a guess is how a guess gets read as a
    finding. If a document confirms the candidate, that document is retrieved
    later and becomes ordinary evidence in ``adjudicate``.
    """

    candidate: str = Field(min_length=1)
    referent_confidence: ReferentConfidence
    distortion_applied: str = Field(
        min_length=1,
        description="The named transformation, from the versioned distortion taxonomy.",
    )
    reasoning: str = Field(min_length=1)
    register: Register = Field(default=Register.INFERRED)

    @model_validator(mode="after")
    def _candidates_are_always_inferred(self) -> ReferentCandidate:
        if self.register is not Register.INFERRED:
            raise ValueError(
                "a referent candidate is a hypothesis and must be INFERRED. "
                "Marking one ESTABLISHED would present a guess as a sourced fact."
            )
        return self


class StructuralConflict(ABCAModel):
    """A conflict with a rule of the system, ANCHORED TO A T0 CITATION.

    The only finding this module can reach without retrieval, and the only place
    in the whole object where a citation may appear. The T0 requirement is not
    stylistic: an uncited "the Senate cannot do that" is itself an unsourced
    claim about the law, which is the exact category of thing the Analyzer
    exists to catch. Ours does not get an exemption.

    Uncited structural intuitions go to :class:`StructuralFlag` instead.
    """

    conflict: str = Field(min_length=1)
    citation: Citation
    register: Register = Field(default=Register.ESTABLISHED)

    @model_validator(mode="after")
    def _conflict_must_be_t0_and_established(self) -> StructuralConflict:
        if self.citation.tier is not SourceTier.T0:
            raise ValueError(
                f"structural conflict cites {self.citation.tier.value}; T0 is required. "
                "A structural conflict is a claim about what the rules ARE, so it must "
                "rest on the rule text itself -- constitutional text, chamber standing "
                "rules, the authorizing statute, or the enacted appropriation."
            )
        if self.register is not Register.ESTABLISHED:
            raise ValueError("a cited structural conflict is ESTABLISHED by definition")
        return self


class StructuralFlag(ABCAModel):
    """A structural intuition with NO citation. Not evidence; a retrieval target.

    This class exists so the module has somewhere honest to put "I think the
    chamber cannot do that" without that sentence acquiring the weight of a
    finding. ``would_be_established_by`` is required: an intuition that cannot
    name the document that would confirm it is not actionable, and saying so
    forces the model to convert a hunch into a search.
    """

    flag: str = Field(min_length=1)
    would_be_established_by: str = Field(
        min_length=1,
        description="The document that would turn this flag into a cited conflict.",
    )
    register: Register = Field(default=Register.INFERRED)

    @model_validator(mode="after")
    def _flags_are_never_evidence(self) -> StructuralFlag:
        if self.register is not Register.INFERRED:
            raise ValueError(
                "an uncited structural flag is INFERRED. If it can be cited it "
                "belongs in structural_conflict[] instead."
            )
        return self


class DomainFailurePattern(ABCAModel):
    """One known distortion pattern that fired, with the span that triggered it."""

    pattern: str = Field(min_length=1)
    span: str = Field(min_length=1, description="The exact triggering text, quoted.")
    explanation: str = Field(min_length=1)
    register: Register = Field(default=Register.INFERRED)


class Fallacy(ABCAModel):
    """A defect in the ARGUMENT. Never in the arguer, never in the truth value.

    A claim can be true and fallaciously argued. This finding ships alongside
    the disposition and has no path to it -- see
    :meth:`SourcelessAnalysis._fallacies_do_not_touch_disposition`.
    """

    name: str = Field(min_length=1)
    span: str = Field(min_length=1)
    mechanism: str = Field(min_length=1, description="How it fails, in one sentence.")
    severity: Severity


class RhetoricalDevice(ABCAModel):
    """Noted, not scored. A device is a style observation, not a defect."""

    device: str = Field(min_length=1)
    span: str = Field(min_length=1)


class ArgumentReconstruction(ABCAModel):
    """The post's argument in standard form.

    ``unstated_premises`` is the point of the whole stage. The failure in a
    viral political claim almost always lives in a premise the post never says
    out loud, and surfacing it is more useful to a reader than any fallacy name.
    """

    stated_premises: tuple[str, ...] = ()
    unstated_premises: tuple[str, ...] = ()
    conclusion: str = ""


class BranchOutcome(ABCAModel):
    """What a given retrieval result would mean -- written BEFORE retrieval runs.

    The module's commitment device. Doc 20's promotion gate (P2) refuses to
    promote a claim on an artifact matching no pre-registered branch, which is
    what stops an executor from searching until it finds something plausible and
    then declaring that to be the referent.
    """

    if_found: str = Field(min_length=1)
    then_disposition: Verdict

    @model_validator(mode="after")
    def _branch_dispositions_stay_in_vocabulary(self) -> BranchOutcome:
        if self.then_disposition not in ALLOWED_DISPOSITIONS:
            raise ValueError(
                f"branch outcome names {self.then_disposition.value}, which this module "
                f"cannot emit. Allowed: {sorted(v.value for v in ALLOWED_DISPOSITIONS)}."
            )
        return self


class PrimarySourceTarget(ABCAModel):
    """One connector to consult, in priority order."""

    connector: str = Field(min_length=1)
    target: str = Field(min_length=1)
    priority: int = Field(ge=1)


class RetrievalPlan(ABCAModel):
    """The executable output. Lane A consumes it; Lane B runs it.

    ``queries`` are literal search strings, not descriptions of searches. That
    distinction is enforced by the eval suite rather than the type system, but
    it is the difference between a plan and a wish.
    """

    primary_sources: tuple[PrimarySourceTarget, ...] = ()
    queries: tuple[str, ...] = ()
    decisive_artifact: str = Field(
        default="",
        description=(
            "The single document that would resolve the claim outright. An empty "
            "string means there isn't one, which is itself a finding worth publishing."
        ),
    )
    branch_outcomes: tuple[BranchOutcome, ...] = ()


class ReferentRedTeam(ABCAModel):
    """The adversarial pass against the referent hypothesis (doc 18 s7).

    Mandatory because referent reconstruction is a plausibility judgment, and
    plausibility judgments are where a partisan thumb lands unnoticed: the
    "obvious" seed for a claim tends to be the one that makes the claim's target
    look the way the reader already expects.
    """

    alternative_referent: str = ""
    case_against_top_candidate: str = ""
    referent_confidence_downgraded_from: ReferentConfidence | None = None


__all__ = [
    "ALLOWED_DISPOSITIONS",
    "ArgumentReconstruction",
    "AtomicClaim",
    "BranchOutcome",
    "ClaimSubtype",
    "DomainFailurePattern",
    "Fallacy",
    "InstitutionalContext",
    "PostMetadata",
    "PrimarySourceTarget",
    "ReferentCandidate",
    "ReferentConfidence",
    "ReferentRedTeam",
    "Register",
    "RetrievalPlan",
    "RhetoricalDevice",
    "Severity",
    "SourcelessAnalysis",
    "StructuralConflict",
    "StructuralFlag",
    "Verifiability",
]


# ==========================================================================
# The analysis object
# ==========================================================================


class PostMetadata(ABCAModel):
    """What is known about where the claim came from.

    Deliberately has no author, handle, or account field. Contract s8 and M9:
    findings attach to claim spans and are never aggregated per person. A schema
    with nowhere to put a handle cannot accidentally grow a dossier.
    """

    platform: str = ""
    date: str = ""
    parent_context: str = ""


class InstitutionalContext(ABCAModel):
    """One mechanism entry selected from the context pack (doc 19 s7).

    Carries a citation, and is therefore the SECOND place in this object where
    one may appear -- but it is not an exception to the containment rule. A
    context entry is an ordinary corpus source, exactly like a retrieved
    document; what the rule forbids is a citation attached to the module's own
    reasoning. ``citation_sites()`` reports both, and the test suite asserts that
    every site is either a structural conflict or a context entry.

    ``selected_by`` is published so a reader who thinks an entry is irrelevant
    can see which field pulled it in.
    """

    entry_id: str = Field(min_length=1)
    entry_version: str = Field(default="1.0.0")
    selected_by: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    plain_language: str = Field(min_length=1)
    citation: Citation
    staleness: str | None = Field(
        default=None,
        description="Set when the entry is past review_due. Served with a warning, never withheld.",
    )
    relevance: str = ""

    @model_validator(mode="after")
    def _context_rests_on_primary_or_official(self) -> InstitutionalContext:
        if self.citation.tier.rank > SourceTier.T1.rank:
            raise ValueError(
                f"context entry {self.entry_id}: citation is {self.citation.tier.value}; "
                "a mechanism entry must rest on T0 or T1."
            )
        return self


class SourcelessAnalysis(ABCAModel):
    """The whole pass-1 output, nested on a claim as ``sourceless_analysis``.

    THE FOUR INVARIANTS, all enforced below rather than requested in a prompt:

    1. ``disposition`` is drawn from :data:`ALLOWED_DISPOSITIONS` only.
    2. ``CONTRADICTED`` requires a ``structural_conflict[]`` entry with a T0
       citation. There is no other route to it.
    3. ``evidence_quality`` is T4 unless that same cited conflict exists.
    4. Citations live in ``structural_conflict[]`` and nowhere else in the
       object -- checked structurally, not by convention.

    Together they say one thing: this module cannot manufacture evidence, no
    matter how confident the model that filled it in happened to be.
    """

    module_version: str = Field(default="sourceless/0.1.0")
    prompt_hash: str = Field(default="")
    triggered_by: str = Field(default="no_external_reference")

    #: Always false in this version, and ASSERTED rather than omitted (M10).
    #: An absent field reads as "not considered"; an explicit false reads as
    #: "considered and deliberately not done", which is the true statement.
    media_present: bool = False
    media_analyzed: bool = False

    denatured_claim: str = Field(
        min_length=1,
        description=(
            "The post's assertion in one neutral sentence, stripped of framing. "
            "Everything downstream operates on THIS, not the post's wording, and "
            "doc 20 s4 hashes it to stop the reconstruction being substituted for it."
        ),
    )
    denatured_claim_hash: str = Field(default="")
    post_metadata: PostMetadata = Field(default_factory=PostMetadata)

    atomic_claims: tuple[AtomicClaim, ...] = ()
    referent_candidates: tuple[ReferentCandidate, ...] = ()
    conditions_required_for_truth: tuple[str, ...] = ()
    structural_conflict: tuple[StructuralConflict, ...] = ()
    structural_flag: tuple[StructuralFlag, ...] = ()
    domain_failure_patterns: tuple[DomainFailurePattern, ...] = ()

    argument_reconstruction: ArgumentReconstruction = Field(
        default_factory=ArgumentReconstruction
    )
    fallacies: tuple[Fallacy, ...] = ()
    rhetorical_devices: tuple[RhetoricalDevice, ...] = ()

    disposition: Verdict = Verdict.UNSUPPORTED
    evidence_quality: SourceTier = SourceTier.T4
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_rationale: str = ""
    referent_confidence: ReferentConfidence = ReferentConfidence.NONE
    unverified_items: tuple[str, ...] = ()

    retrieval_plan: RetrievalPlan = Field(default_factory=RetrievalPlan)
    red_team: ReferentRedTeam = Field(default_factory=ReferentRedTeam)

    #: Doc 19 s7. Selected mechanism entries, with their citations intact.
    #:
    #: These ARE admissible evidence -- but only for what they actually say. A
    #: cited entry establishing that a contribution limit applies per election
    #: can support MISLEADING_CONTEXT on a claim that framed it as annual. It can
    #: never establish what any particular contribution was.
    institutional_context: tuple[InstitutionalContext, ...] = ()
    context_pack_version: str = ""
    context_pack_hash: str = ""

    challenge_id: str | None = None
    handoff_notes: str = ""

    # ---- invariants -----------------------------------------------------

    @model_validator(mode="after")
    def _disposition_is_in_vocabulary(self) -> SourcelessAnalysis:
        """Invariant 1. SUPPORTED and MIXED are unreachable without evidence."""
        if self.disposition not in ALLOWED_DISPOSITIONS:
            raise ValueError(
                f"disposition {self.disposition.value} is not reachable from the "
                "sourceless module: it requires evidence this module does not have. "
                f"Allowed: {sorted(v.value for v in ALLOWED_DISPOSITIONS)}. "
                "MISLEADING_CONTEXT in particular becomes reachable only after the "
                "challenge lane finds the artifact that was mischaracterized."
            )
        return self

    @model_validator(mode="after")
    def _contradicted_requires_a_cited_conflict(self) -> SourcelessAnalysis:
        """Invariant 2. The ONLY route to a negative verdict without retrieval."""
        if self.disposition is Verdict.CONTRADICTED and not self.structural_conflict:
            raise ValueError(
                "disposition CONTRADICTED requires a structural_conflict[] entry "
                "carrying a T0 citation. Without one this is an uncited assertion "
                "that the claim is false, which is the exact thing the Analyzer "
                "exists to catch in other people's work."
            )
        return self

    @model_validator(mode="after")
    def _evidence_quality_is_pinned_to_t4(self) -> SourcelessAnalysis:
        """Invariant 3. A bare post is T4 and reasoning about it does not promote it."""
        expected = SourceTier.T0 if self.structural_conflict else SourceTier.T4
        if self.evidence_quality is not expected:
            raise ValueError(
                f"evidence_quality is {self.evidence_quality.value} but the analysis "
                f"{'carries' if self.structural_conflict else 'carries no'} cited "
                f"structural conflict, so it must be {expected.value}. Reasoning about "
                "a sourceless post does not raise its tier."
            )
        return self

    @model_validator(mode="after")
    def _unsupported_is_not_rounded_toward_contradicted(self) -> SourcelessAnalysis:
        """M4. Absence of a source lowers confidence, not truth value.

        A high-confidence UNSUPPORTED is fine -- it means "confident nothing
        supports this". What is not fine is treating that as a negative finding,
        so the rationale must exist to say which one is meant.
        """
        if self.disposition is Verdict.UNSUPPORTED and self.confidence > 0.0 \
                and not self.confidence_rationale:
            raise ValueError(
                "a non-zero confidence on an UNSUPPORTED disposition needs a "
                "confidence_rationale saying it describes confidence IN THE "
                "DISPOSITION, not in the claim being false (M4)."
            )
        return self

    @model_validator(mode="after")
    def _motive_claims_route_to_unverifiable(self) -> SourcelessAnalysis:
        """M7. If every atom is a motive attribution, nothing here is checkable."""
        atoms = self.atomic_claims
        if atoms and all(a.subtype.is_unfalsifiable for a in atoms) \
                and self.disposition not in {Verdict.UNVERIFIABLE, Verdict.OUT_OF_SCOPE}:
            raise ValueError(
                "every atomic claim is a motive attribution, which is unfalsifiable "
                f"(M7), so the disposition must be UNVERIFIABLE, not "
                f"{self.disposition.value}."
            )
        return self

    @model_validator(mode="after")
    def _media_is_never_analyzed_in_this_version(self) -> SourcelessAnalysis:
        """M10 / doc 18 s2. Text only, asserted rather than left ambiguous."""
        if self.media_analyzed:
            raise ValueError(
                "media_analyzed must be false: this version is text-only by "
                "decision (doc 18 s2). A post whose only assertion lives in an "
                "image is gated OUT_OF_SCOPE with reason image_only_content."
            )
        return self

    # ---- structural properties -------------------------------------------

    def citation_sites(self) -> list[str]:
        """Every path in this object that holds a Citation. Invariant 4's probe.

        Walks the model rather than trusting the class layout, so a future field
        that quietly gains a citation is caught by the test that calls this
        instead of shipping.
        """
        sites: list[str] = []

        def walk(value: object, path: str) -> None:
            if isinstance(value, Citation):
                sites.append(path)
            elif isinstance(value, ABCAModel):
                for name in type(value).model_fields:
                    walk(getattr(value, name), f"{path}.{name}" if path else name)
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        walk(self, "")
        return sites

    @property
    def carries_evidence(self) -> bool:
        """Whether anything here may be used as evidence downstream."""
        return bool(self.structural_conflict)

    @property
    def load_bearing_atoms(self) -> tuple[AtomicClaim, ...]:
        """The atoms whose failure collapses the post's rhetorical force."""
        return tuple(a for a in self.atomic_claims if a.load_bearing)

    def summary(self) -> dict[str, object]:
        """Compact operator view. Never the published brief."""
        return {
            "disposition": self.disposition.value,
            "evidence_quality": self.evidence_quality.value,
            "referent_confidence": self.referent_confidence.value,
            "atoms": len(self.atomic_claims),
            "load_bearing": len(self.load_bearing_atoms),
            "candidates": len(self.referent_candidates),
            "cited_conflicts": len(self.structural_conflict),
            "uncited_flags": len(self.structural_flag),
            "patterns": len(self.domain_failure_patterns),
            "fallacies": len(self.fallacies),
            "queries": len(self.retrieval_plan.queries),
            "branches": len(self.retrieval_plan.branch_outcomes),
        }
