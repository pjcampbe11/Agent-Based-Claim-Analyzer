"""Structured-output schemas for the pipeline's model calls.

These are the objects models are constrained to produce. They are deliberately
SEPARATE from :mod:`abca.schema.core`:

* ``schema.core`` holds the published record -- what a reader of a verdict
  sees, governed by the contract gates.
* These are wire formats for one stage's model call: small, flat, and easy for
  a model to fill in correctly.

Keeping them apart matters. A model asked to emit a full
:class:`~abca.schema.core.Claim` would have to invent a verdict and an evidence
tier at segmentation time, before any evidence has been retrieved -- and
whatever it invented would then have to be discarded. Asking for exactly what
the stage needs is both cheaper and less likely to produce a confident
fabrication that later code has to unlearn.

Every model here is small on purpose. Constrained decoding cost and repair
frequency both scale with schema size, and the classifier runs once per claim.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from abca.fidelity.elements import ElementKind
from abca.schema.enums import ClaimType, RedTeamSeverity, Verdict

#: Shared config: reject unknown keys so an out-of-contract response is
#: retried rather than silently truncated to the fields we recognise.
_STRICT = ConfigDict(extra="forbid", frozen=True)

UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------


class GateDecision(BaseModel):
    """Whether one sentence concerns public affairs."""

    model_config = _STRICT

    sentence_index: int = Field(ge=0, description="Index of the sentence being judged.")
    in_scope: bool = Field(description="True when the sentence asserts something about public affairs.")
    reason: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "One short clause naming WHY. Read by a human auditing the gate, so "
            "'asserts a statutory signature requirement' beats 'it is political'."
        ),
    )


class GateOutput(BaseModel):
    """The gate's decisions for one batch of sentences."""

    model_config = _STRICT

    decisions: list[GateDecision] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Segment
# --------------------------------------------------------------------------


class ExtractedClaim(BaseModel):
    """One atomic assertion pulled out of a sentence."""

    model_config = _STRICT

    text: str = Field(
        min_length=1,
        max_length=2000,
        description="The claim as a standalone assertion, with references resolved.",
    )
    sentence_index: int = Field(ge=0, description="Which input sentence this came from.")
    verbatim_span: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Exact substring of the source sentence this claim is drawn from, when "
            "one exists. Null when the claim had to be rephrased to stand alone. "
            "An approximate quote is worse than none -- it fails to locate and the "
            "span is discarded anyway."
        ),
    )
    ambiguous_stance: bool = Field(
        default=False,
        description=(
            "True when sarcasm, irony or reported speech makes the author's own "
            "commitment to the claim unclear. Flagged rather than resolved: "
            "guessing at intent would put words in the author's mouth."
        ),
    )


class SegmentOutput(BaseModel):
    """Claims extracted from one batch of sentences."""

    model_config = _STRICT

    claims: list[ExtractedClaim] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Classify
# --------------------------------------------------------------------------


class ClaimClassification(BaseModel):
    """The type assigned to one claim, with the reasoning that justifies it."""

    model_config = _STRICT

    claim_id: str = Field(min_length=1, description="Id of the claim being classified.")
    claim_type: ClaimType = Field(description="One of the six types from contract s3.")
    confidence: UnitInterval = Field(
        description=(
            "Confidence in the CLASSIFICATION, not in the claim. A claim you are "
            "sure is false but sure is empirical gets a high confidence."
        )
    )
    reasoning: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "One sentence naming what would settle the claim. This is the audit "
            "trail for the most consequential routing decision in the pipeline."
        ),
    )


class ClassifyOutput(BaseModel):
    """Classifications for one batch of claims."""

    model_config = _STRICT

    classifications: list[ClaimClassification] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Adjudicate
# --------------------------------------------------------------------------


class CitationRef(BaseModel):
    """A model's reference to a retrieved source.

    THIS IS WHERE CITATION FABRICATION IS MADE STRUCTURALLY IMPOSSIBLE.

    Look at what is NOT here: no ``tier``, no ``url``, no ``title``, no
    ``content_hash``, no ``retrieved_at``. A model cannot supply any of them,
    because there is no field to put them in. It gets exactly two levers --
    which retrieved document it is pointing at, and what text it claims that
    document contains -- and both are checked against the document before a
    :class:`~abca.schema.core.Citation` is built from them.

    The alternative design, letting the model emit a full Citation, fails in a
    specific and dangerous way: a fluent model produces a plausible URL, a
    plausible tier, and a plausible-sounding quote, and the result passes every
    downstream gate while being entirely invented. The gates check tier and
    quote presence, not tier truthfulness.
    """

    model_config = _STRICT

    source_id: str = Field(
        min_length=1,
        description=(
            "Id of one of the sources provided in this prompt (e.g. 's-001'). "
            "A source that was not provided cannot be cited."
        ),
    )
    quote: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "Text copied VERBATIM from that source. Verified as a substring "
            "before the citation is accepted; a paraphrase is discarded."
        ),
    )
    locator: str | None = Field(
        default=None,
        max_length=200,
        description="Optional in-source pointer: section, subsection, paragraph.",
    )


class Adjudication(BaseModel):
    """One claim's verdict, as the model reports it.

    Distinct from :class:`~abca.schema.core.Claim`: this is the wire format,
    carrying only what the model decides. The published claim is assembled by
    the pipeline from this plus the verified citations plus the classification
    that was already settled upstream -- so a model cannot reach back and
    change a claim's type or text while adjudicating it.
    """

    model_config = _STRICT

    claim_id: str = Field(min_length=1)
    verdict: Verdict = Field(description="From the closed vocabulary; no TRUE/FALSE.")
    confidence: UnitInterval = Field(
        description="Confidence in the VERDICT given the evidence, not in the topic."
    )
    reasoning: str = Field(
        min_length=1,
        max_length=3000,
        description="One paragraph: what the source establishes, and how it meets the claim.",
    )
    citations: list[CitationRef] = Field(
        default_factory=list,
        description=(
            "Verified support. Empty is correct when the sources do not settle "
            "the claim; an invented citation to justify a verdict is not."
        ),
    )


class AdjudicateOutput(BaseModel):
    """Adjudications for one batch of claims."""

    model_config = _STRICT

    adjudications: list[Adjudication] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Red team
# --------------------------------------------------------------------------


class RedTeamAssessment(BaseModel):
    """One claim's adversarial review, as the model reports it.

    ``counter_citations`` reuses :class:`CitationRef` -- the same three-field
    pointer the adjudicator gets -- so counter-evidence goes through exactly
    the same verbatim verification. Anything else would make the red team a
    fabrication channel: a model could undercut any verdict with invented
    text, which is precisely the hole adjudication closes.
    """

    model_config = _STRICT

    claim_id: str = Field(min_length=1)
    severity: RedTeamSeverity = Field(
        description="Only MATERIAL changes a verdict. NONE is a real, frequent answer."
    )
    counter_evidence: str = Field(
        min_length=1,
        max_length=2000,
        description="What cuts against the verdict, or a plain statement that nothing does.",
    )
    steelman: str = Field(
        min_length=1,
        max_length=2000,
        description="The strongest good-faith case for the position the analysis went against.",
    )
    overreach_flags: list[str] = Field(
        default_factory=list,
        description="Specific places the analysis asserted more than its sources support.",
    )
    counter_citations: list[CitationRef] = Field(
        default_factory=list,
        description=(
            "Verbatim support for the counter-evidence. Verified before use; a "
            "quote that does not appear in the named source is discarded."
        ),
    )
    recommended_verdict: Verdict | None = Field(
        default=None,
        description=(
            "Only meaningful when severity is MATERIAL, and only ever a WEAKER "
            "verdict. The ratchet is enforced in code, not by this field."
        ),
    )


class RedTeamOutput(BaseModel):
    """Assessments for one batch of adjudicated claims."""

    model_config = _STRICT

    assessments: list[RedTeamAssessment] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Fidelity gate (contract s5) -- three passes, three wire formats
# --------------------------------------------------------------------------


class ExtractedElement(BaseModel):
    """One operative piece of a legal provision, as a model reports it.

    ONLY TWO FIELDS, AND THE OMISSIONS ARE THE DESIGN.

    There is no ``modal`` field and no ``anchors`` field, even though both are
    central to the diff. A model that could declare its own modal could declare
    the one it happened to preserve -- and the check that "shall" did not become
    "may" would then be a check on the model's self-report rather than on the
    text. So both are read mechanically from ``text`` by
    :meth:`~abca.fidelity.elements.LegalElement.build`.

    There is also no confidence, no reasoning, and no id. Pass A is a
    decomposition, not a judgment; anything else here would be a place for a
    model to argue with the diff.
    """

    model_config = _STRICT

    kind: ElementKind = Field(description="Which of the nine element roles this piece plays.")
    text: str = Field(
        min_length=1,
        max_length=1500,
        description=(
            "The element in the source's own operative words, trimmed to the piece "
            "it covers. Not a paraphrase: pass A records what the provision says "
            "before anyone rewrites it."
        ),
    )


class FidelityExtractOutput(BaseModel):
    """Pass A: the provision taken apart into its operative elements."""

    model_config = _STRICT

    elements: list[ExtractedElement] = Field(
        default_factory=list,
        description="One entry per discrete operative piece. Order carries no meaning.",
    )


class GlossaryFootnote(BaseModel):
    """A term of art kept verbatim, with the plain explanation attached to it.

    Terms of art are carried through unchanged and explained beside the text
    rather than paraphrased away (contract s5). "Reasonable suspicion" has a
    body of case law behind it that "a good reason to be suspicious" does not,
    so a rewrite that swaps one for the other has stated a different rule while
    looking more readable.
    """

    model_config = _STRICT

    term: str = Field(min_length=1, max_length=120, description="The term, exactly as in the source.")
    explanation: str = Field(
        min_length=1,
        max_length=600,
        description="What it means, in ordinary words. Explains the term; does not replace it.",
    )


class FidelityRenderOutput(BaseModel):
    """Pass B: the plain-language rewrite.

    Deliberately does NOT carry an element list. Asking the renderer which
    elements it preserved would be asking the graded party for its own grade;
    the only honest answer comes from pass C reconstructing the elements from
    the rewrite alone, with no knowledge of what the source contained.
    """

    model_config = _STRICT

    rendering: str = Field(
        min_length=1,
        max_length=20_000,
        description="The plain-language text. Every element from pass A must survive into it.",
    )
    footnotes: list[GlossaryFootnote] = Field(
        default_factory=list,
        description="One entry per term of art kept verbatim. Empty when the source has none.",
    )


class FidelityBacktranslateOutput(BaseModel):
    """Pass C: elements reconstructed from the rewrite alone.

    Structurally identical to :class:`FidelityExtractOutput`, and that is
    required rather than incidental -- the two are diffed against each other,
    so any field one carried and the other did not would be a field the diff
    could not compare.

    The model producing this has not seen the source provision. See
    ``prompts/sotp/0.1.0/fidelity.backtranslate.md``: filling a gap from
    general knowledge of how such rules usually work would conceal exactly the
    loss this pass exists to measure.
    """

    model_config = _STRICT

    elements: list[ExtractedElement] = Field(
        default_factory=list,
        description="Only what the plain-language text actually states. An empty category is a finding.",
    )


# --------------------------------------------------------------------------
# Internal working object
# --------------------------------------------------------------------------


class DraftClaim(BaseModel):
    """A claim in flight: extracted and classified, not yet adjudicated.

    This is the pipeline's own working type, never serialized into a run
    record. It exists so that segmentation and classification do not have to
    invent a verdict and an evidence tier for a claim that no retrieval has
    touched yet.

    The conversion to a published :class:`~abca.schema.core.Claim` happens once,
    in :mod:`abca.pipeline.orchestrator`, where the honest placeholder verdict
    is applied in one auditable place rather than scattered across stages.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    sentence_index: int
    span_start: int | None = None
    span_end: int | None = None
    #: How the span was resolved: "verbatim" (the model's quote located
    #: exactly), "text" (the claim text itself located exactly), or "sentence"
    #: (fell back to the whole source sentence). Reported in stage notes so a
    #: model that never produces locatable quotes is visible.
    span_source: str = "sentence"
    ambiguous_stance: bool = False

    claim_type: ClaimType | None = None
    classification_confidence: float | None = None
    classification_reasoning: str = ""

    #: Set when the gate ruled the source sentence out of public affairs.
    gated_out: bool = False
    gate_reason: str = ""

    @model_validator(mode="after")
    def _span_pair(self) -> DraftClaim:
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("span_start and span_end must both be set or both omitted")
        return self


__all__ = [
    "AdjudicateOutput",
    "Adjudication",
    "CitationRef",
    "ClaimClassification",
    "ClassifyOutput",
    "DraftClaim",
    "ExtractedClaim",
    "ExtractedElement",
    "FidelityBacktranslateOutput",
    "FidelityExtractOutput",
    "FidelityRenderOutput",
    "GateDecision",
    "GateOutput",
    "GlossaryFootnote",
    "RedTeamAssessment",
    "RedTeamOutput",
    "SegmentOutput",
]
