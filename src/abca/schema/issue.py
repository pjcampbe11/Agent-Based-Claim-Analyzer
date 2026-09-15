"""Vocabulary for issue analysis and solution planning (build step 9).

WHAT THIS ADDS TO THE CONTRACT
==============================
``abca analyze`` answers "is this claim supported?" This module supports the
next question: "given an issue and the evidence somebody actually brought, what
can be done about it, and who is allowed to decide?"

Three ideas carry the whole design, and each is a STRUCTURAL guarantee rather
than an instruction a model is asked to follow. That distinction is the reason
the rest of this repository is worth anything, and it does not get relaxed here.

1. THE EVIDENCE FLOOR. ``abca issue`` refuses to run without references. Not
   "warns" -- refuses, with a non-zero exit. An opinion with no sources is the
   thing this tool exists to be an alternative to, and a tool that produced
   confident policy analysis from a bare sentence would be that thing with a
   citation format bolted on.

2. A DECLARED KIND IS A CEILING, NEVER A PROMOTION. A user labels each
   reference at the command line. That label can only ever LOWER the tier the
   connector assigns, never raise it. Typing ``primary:`` in front of a blog
   post does not make it primary law, exactly as a model asserting ``T0`` does
   not (see :class:`~abca.schema.enums.SourceTier`).

3. GOVERNANCE TIER IS DERIVED, NOT DECLARED. Whether a step may be automated is
   computed from the CLASS of claim it rests on, by the table in this module.
   No model writes "MECHANICAL" anywhere. A step resting on a value judgment is
   a value decision no matter how mechanical it looks, and the escalation is
   one-way.

WHY THE THIRD ONE MATTERS MOST
==============================
The point of automating any of this is to remove red tape, delay, and
decisions bought by whoever is paying. All three of those live in MECHANICAL
work -- routing, eligibility arithmetic, disbursement, publication -- and in
VERIFIABLE work that today happens after the vote or not at all.

None of them live in the value question, and a model deciding value questions
would not remove the greed; it would relocate it into whoever wrote the
objective function, where it is harder to see and impossible to vote out. So:
mechanics automate completely, verification automates as a mandatory published
check, and the value decision stays with a human who must publish the run hash
of what was in front of them.

That last clause is the anti-corruption mechanism, and it is worth being precise
about what it does and does not do. It does not prevent a bad vote. It makes a
bad vote UNDENIABLE -- the record shows what the decider knew when they decided.
Corruption stops being deniable rather than being prohibited by software, and a
design that claimed the stronger thing would be lying.
"""

from __future__ import annotations

from enum import StrEnum

from abca.schema.enums import ClaimType, SourceTier

# ==========================================================================
# References
# ==========================================================================


class ReferenceKind(StrEnum):
    """What a supplied reference IS, declared by the person supplying it.

    The kinds are ordered by the strongest tier each can ever reach. That
    ceiling is the entire purpose of the enum: a person at a command line is
    not a trusted authority on how good their own source is, any more than a
    model is.
    """

    PRIMARY = "primary"      # Constitution, statute, regulation, court opinion.
    OFFICIAL = "official"    # Agency data, filing, docket, on-the-record statement.
    RESEARCH = "research"    # Peer-reviewed or method-transparent study.
    REPORTING = "reporting"  # Journalism with a published corrections policy.
    SOCIAL = "social"        # A post. See `is_evidence`.

    @property
    def tier_ceiling(self) -> SourceTier:
        """The strongest tier a reference of this kind may ever be treated as.

        The EFFECTIVE tier is the weaker of this ceiling and whatever the
        connector resolved, so the declaration can only ever cost a reference
        strength. There is deliberately no way to spend a flag and gain any.
        """
        match self:
            case ReferenceKind.PRIMARY:
                return SourceTier.T0
            case ReferenceKind.OFFICIAL:
                return SourceTier.T1
            case ReferenceKind.RESEARCH:
                return SourceTier.T2
            case ReferenceKind.REPORTING:
                return SourceTier.T3
            case ReferenceKind.SOCIAL:
                return SourceTier.T4

    @property
    def is_evidence(self) -> bool:
        """Whether this kind can support a verdict at all.

        SOCIAL cannot, ever. A post is admissible as the OBJECT of analysis --
        "this is the claim circulating, here is where it circulated" -- and
        never as proof that the claim is true. The tool accepts social
        references precisely because the post is often the thing being
        analyzed, and this property is what stops that from quietly becoming
        support for it.
        """
        return self.tier_ceiling.can_support_verdict

    @property
    def counts_toward_primary_floor(self) -> bool:
        """Whether this kind satisfies the required-primary part of the floor."""
        return self in {ReferenceKind.PRIMARY, ReferenceKind.OFFICIAL}


# ==========================================================================
# Governance
# ==========================================================================


class GovernanceTier(StrEnum):
    """Who may execute a step of a plan.

    Derived from the claims a step rests on, never declared. See
    :func:`governance_floor` and :func:`escalate`.
    """

    MECHANICAL = "MECHANICAL"    # Rests on no contestable claim. Automate fully.
    VERIFIABLE = "VERIFIABLE"    # Rests on checkable claims. Automate the CHECK.
    VALUE = "VALUE"              # Rests on a value judgment. A human decides.

    @property
    def may_automate_decision(self) -> bool:
        """Whether software may make this step's decision outright.

        True for MECHANICAL alone. This property is the line the whole module
        exists to draw, and there is no configuration flag that moves it.
        """
        return self is GovernanceTier.MECHANICAL

    @property
    def requires_human_decider(self) -> bool:
        """Whether a named, accountable person must make the call."""
        return self is GovernanceTier.VALUE

    @property
    def requires_published_check(self) -> bool:
        """Whether an Analyzer run must be published BEFORE this step executes.

        Both non-mechanical tiers. For VERIFIABLE the check is the decision;
        for VALUE the check is the record of what the human knew, which is what
        makes a decision against the evidence visible instead of deniable.
        """
        return self is not GovernanceTier.MECHANICAL


#: How strong each governance tier is, for the one-way ratchet below. Higher
#: means more constrained -- more human involvement required, never less.
#:
#: Modelled on VERDICT_STRENGTH in the red-team stage for the same reason: a
#: pass that could move a step in EITHER direction would eventually be argued
#: into automating something it should not, one plausible step at a time.
GOVERNANCE_STRENGTH: dict[GovernanceTier, int] = {
    GovernanceTier.MECHANICAL: 0,
    GovernanceTier.VERIFIABLE: 1,
    GovernanceTier.VALUE: 2,
}


#: The floor each class of claim imposes on a step that rests on it.
#:
#: PREDICTIVE sits with the value claims, and that placement is the one most
#: likely to be argued with, so: a prediction is not settleable by evidence
#: (contract s3), which means acting on one is a choice about which risk to
#: accept. That is a value decision wearing a forecast's clothes, and it is
#: exactly where "the model said the projection was favorable" would do the
#: most damage.
CLAIM_GOVERNANCE_FLOOR: dict[ClaimType, GovernanceTier] = {
    ClaimType.LEGAL: GovernanceTier.VERIFIABLE,
    ClaimType.EMPIRICAL: GovernanceTier.VERIFIABLE,
    ClaimType.ATTRIBUTIVE: GovernanceTier.VERIFIABLE,
    ClaimType.PREDICTIVE: GovernanceTier.VALUE,
    ClaimType.NORMATIVE: GovernanceTier.VALUE,
    ClaimType.DEFINITIONAL: GovernanceTier.VALUE,
}


def escalate(current: GovernanceTier, proposed: GovernanceTier) -> GovernanceTier:
    """Return the MORE constrained of two tiers. Never the less constrained.

    The ratchet. Combining evidence about a step can only ever move it toward
    requiring a human, which means no sequence of individually reasonable
    adjustments can end with a value decision handed to software.
    """
    return max((current, proposed), key=lambda tier: GOVERNANCE_STRENGTH[tier])


def governance_floor(claim_types: object) -> GovernanceTier:
    """Compute a step's governance tier from the claims it rests on.

    A step resting on NO contestable claim is MECHANICAL -- that is arithmetic,
    routing, publication, and disbursement against already-decided criteria,
    which is where the delay and the paperwork actually live. One normative
    claim anywhere in the step makes the whole step VALUE.

    ``claim_types`` is any iterable of :class:`ClaimType`.
    """
    tier = GovernanceTier.MECHANICAL
    for claim_type in claim_types:  # type: ignore[attr-defined]
        tier = escalate(tier, CLAIM_GOVERNANCE_FLOOR[claim_type])
    return tier


# ==========================================================================
# Intervention properties
# ==========================================================================


class CostBand(StrEnum):
    """Order-of-magnitude annual public cost.

    BANDS, not point estimates, and that is a considered choice rather than
    imprecision. A model asked for "$4.2 billion" will produce a number with
    two significant figures of false confidence; a model asked which power of
    ten something lands in is answering a question it can actually answer, and
    the band is what a first-pass comparison needs anyway.
    """

    NONE = "NONE"                # Costs nothing new; a rule change.
    UNDER_10M = "UNDER_10M"
    M10_TO_100M = "M10_TO_100M"
    M100M_TO_1B = "M100M_TO_1B"
    B1_TO_10B = "B1_TO_10B"
    OVER_10B = "OVER_10B"

    @property
    def rank(self) -> int:
        return list(CostBand).index(self)


class TimeToEffect(StrEnum):
    """How long until the intervention changes anything measurable."""

    DAYS = "DAYS"
    MONTHS = "MONTHS"
    ONE_TO_TWO_YEARS = "ONE_TO_TWO_YEARS"
    OVER_TWO_YEARS = "OVER_TWO_YEARS"

    @property
    def rank(self) -> int:
        return list(TimeToEffect).index(self)


class Reversibility(StrEnum):
    """How hard it is to undo if it turns out to be wrong.

    Weighted heavily in the ranking, because the whole method assumes the
    analysis will sometimes be wrong. An approach that cannot be reversed converts
    being wrong from a correctable error into a permanent one, and a publisher
    that pre-commits to publishing its contradictions had better prefer the options
    it can still act on afterward.
    """

    TRIVIAL = "TRIVIAL"          # Stop doing it. Nothing is stranded.
    MODERATE = "MODERATE"        # Unwinding costs real money or a session.
    HARD = "HARD"                # Built infrastructure, vested entitlements.
    IRREVERSIBLE = "IRREVERSIBLE"  # Cannot be undone at any price.

    @property
    def rank(self) -> int:
        return list(Reversibility).index(self)


__all__ = [
    "CLAIM_GOVERNANCE_FLOOR",
    "GOVERNANCE_STRENGTH",
    "CostBand",
    "GovernanceTier",
    "ReferenceKind",
    "Reversibility",
    "TimeToEffect",
    "escalate",
    "governance_floor",
]
