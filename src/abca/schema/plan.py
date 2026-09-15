"""Models for issue analysis, interventions, and plans (build step 9).

The enums these use, and the reasoning behind the governance line, are in
:mod:`abca.schema.issue`. This module is the data those rules operate on.

ONE RULE GOVERNS EVERY MODEL HERE
=================================
Anything a model is allowed to WRITE is a description. Anything that decides
what may be DONE is computed. So a model writes an intervention's mechanism,
its cost band, and how long it takes to work; code computes the ranking, the
governance tier, and whether the evidence floor was met. A model that could
write ``GovernanceTier.MECHANICAL`` onto a step could automate a value
decision by being confident about it, which is the failure this whole design
exists to prevent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field, field_validator, model_validator

from abca.schema.core import ABCAModel, Citation, Digest, _as_utc
from abca.schema.enums import ClaimType, SourceTier
from abca.schema.issue import (
    CostBand,
    GovernanceTier,
    ReferenceKind,
    Reversibility,
    TimeToEffect,
    governance_floor,
)

# ==========================================================================
# References
# ==========================================================================


class Reference(ABCAModel):
    """One piece of supporting material supplied with an issue.

    ``declared_kind`` is what the person at the command line said this is.
    ``resolved_tier`` is what the connector actually found, or ``None`` when
    nothing could fetch it. :attr:`effective_tier` is the WEAKER of the two,
    which is what everything downstream uses.

    That asymmetry is the point. Labelling a source generously costs the
    labeller nothing and buys them nothing; labelling it modestly is honoured.
    """

    id: str = Field(pattern=r"^r-\d{3}$", description="Stable id: r-001, r-002, ...")
    declared_kind: ReferenceKind = Field(
        description="What the submitter says this is. A CEILING on tier, never a promotion."
    )
    locator: str = Field(
        min_length=1,
        description="URL, citation, or a quoted statement with its attribution.",
    )
    title: str = Field(min_length=1, description="Human-readable name for the source.")
    resolved_tier: SourceTier | None = Field(
        default=None,
        description=(
            "Tier assigned by the connector that fetched this, or None when no "
            "connector could. Never set by a model."
        ),
    )
    retrieved_at: datetime | None = Field(
        default=None, description="When it was fetched. None when it was not fetchable."
    )
    content_hash: Digest | None = Field(
        default=None, description="Digest of the fetched content, when there is any."
    )
    quote: str | None = Field(
        default=None,
        min_length=1,
        description="The passage relied upon, verbatim, when the submitter gave one.",
    )
    unfetchable_reason: str | None = Field(
        default=None,
        description=(
            "Why this reference could not be fetched. Recorded rather than "
            "silently dropped: a reference nobody could open is a fact about "
            "the issue, and the reader is entitled to see it."
        ),
    )

    _normalize_retrieved_at = field_validator("retrieved_at")(_as_utc)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_tier(self) -> SourceTier:
        """The weaker of the declared ceiling and the resolved tier.

        An unfetched reference falls to its declared ceiling ONLY if that
        ceiling is already non-evidentiary; otherwise it falls to T4, because
        a source nobody could open has not been checked, whatever it claims
        to be.
        """
        ceiling = self.declared_kind.tier_ceiling
        if self.resolved_tier is None:
            return ceiling if not ceiling.can_support_verdict else SourceTier.T4
        return max(ceiling, self.resolved_tier, key=lambda tier: tier.rank)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_evidence(self) -> bool:
        """Whether this reference may support a verdict at its effective tier."""
        return self.declared_kind.is_evidence and self.effective_tier.can_support_verdict

    def as_citation(self) -> Citation | None:
        """Convert to a :class:`Citation`, or None when it cannot be one.

        Returns None unless the reference was actually fetched AND carries a
        quote. A citation without a verifiable snapshot and a passage is
        decoration, and the ``Citation`` contract refuses to hold one.
        """
        if self.retrieved_at is None or self.content_hash is None or not self.quote:
            return None
        return Citation(
            tier=self.effective_tier,
            title=self.title,
            url=self.locator,
            quote=self.quote,
            retrieved_at=self.retrieved_at,
            content_hash=self.content_hash,
        )


# ==========================================================================
# Plans
# ==========================================================================


class PlanStep(ABCAModel):
    """One step of an intervention, with who is allowed to execute it.

    ``governance`` is NOT accepted from a model. It is computed by
    :meth:`with_governance` from ``rests_on``, the classes of claim the step
    depends on. The field exists on the model so it can be serialized and
    audited; it is set by code exactly once.
    """

    id: str = Field(pattern=r"^s-\d{3}$")
    description: str = Field(min_length=1, description="What happens in this step.")
    rests_on: tuple[ClaimType, ...] = Field(
        default=(),
        description=(
            "Classes of claim this step depends on. EMPTY means the step is pure "
            "mechanism -- arithmetic, routing, disbursement against criteria "
            "already decided elsewhere."
        ),
    )
    governance: GovernanceTier = Field(
        default=GovernanceTier.VALUE,
        description=(
            "Who may execute this. DERIVED from rests_on, never declared. "
            "Defaults to the most constrained tier so that a step which somehow "
            "skips derivation fails safe toward a human rather than toward "
            "automation."
        ),
    )
    decider: str | None = Field(
        default=None,
        description="For VALUE steps: the accountable office. Required by the validator.",
    )
    days_to_execute: int = Field(
        default=0, ge=0, description="Working days this step takes once it starts."
    )

    @model_validator(mode="after")
    def _governance_matches_claims(self) -> PlanStep:
        """A step's tier must equal what its claims require, and VALUE needs a name.

        Enforced here rather than trusted, because this is the invariant the
        module exists to protect: no serialized plan can carry a step marked
        automatable that rests on a value judgment, whatever produced it.
        """
        required = governance_floor(self.rests_on)
        if self.governance is not required:
            raise ValueError(
                f"step {self.id}: governance is {self.governance.value} but the claims "
                f"it rests on require {required.value}. Governance tier is derived from "
                f"rests_on and is never declared -- use PlanStep.with_governance()."
            )
        if self.governance.requires_human_decider and not self.decider:
            raise ValueError(
                f"step {self.id}: a VALUE step must name the accountable decider. "
                "An unattributed value decision is exactly the thing this tool "
                "exists to make impossible."
            )
        return self

    @classmethod
    def with_governance(
        cls,
        *,
        id: str,
        description: str,
        rests_on: tuple[ClaimType, ...] = (),
        decider: str | None = None,
        days_to_execute: int = 0,
    ) -> PlanStep:
        """Build a step, deriving its governance tier. The only sanctioned path."""
        return cls(
            id=id,
            description=description,
            rests_on=rests_on,
            governance=governance_floor(rests_on),
            decider=decider,
            days_to_execute=days_to_execute,
        )


class Intervention(ABCAModel):
    """One candidate answer to the issue, with everything needed to rank it."""

    id: str = Field(pattern=r"^i-\d{3}$")
    title: str = Field(min_length=1)
    mechanism: str = Field(
        min_length=1,
        description="How this is supposed to work -- the causal story, in plain language.",
    )
    authority: str = Field(
        min_length=1,
        description=(
            "The legal authority this would act under, cited. 'New legislation' is "
            "an acceptable answer and is scored as the slow, hard-to-reverse thing "
            "it is."
        ),
    )
    cost_band: CostBand
    time_to_effect: TimeToEffect
    reversibility: Reversibility
    steps: tuple[PlanStep, ...] = Field(default=())
    falsifier: str = Field(
        min_length=1,
        description=(
            "The observation that would show this did NOT work. Required. An "
            "intervention nobody can be wrong about is not a proposal, it is a "
            "slogan, and this tool does not produce those."
        ),
    )
    citations: tuple[Citation, ...] = Field(default=())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def automatable_share(self) -> float:
        """Fraction of steps that may be executed without a human decision.

        The honest headline number for "how much red tape does this remove."
        Reported rather than optimized: a plan is not better for being more
        automatable, and one that scored 1.0 would mean nobody is accountable
        for anything in it.
        """
        if not self.steps:
            return 0.0
        automatable = sum(1 for s in self.steps if s.governance.may_automate_decision)
        return round(automatable / len(self.steps), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def value_decisions(self) -> int:
        """How many human value decisions this plan requires. Fewer is not better."""
        return sum(1 for s in self.steps if s.governance.requires_human_decider)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_to_execute(self) -> int:
        return sum(step.days_to_execute for step in self.steps)


__all__ = ["Intervention", "PlanStep", "Reference"]
