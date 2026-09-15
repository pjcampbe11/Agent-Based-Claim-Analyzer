"""Controlled vocabularies from the Source-of-Truth Prompt contract.

Every one of these is a CLOSED set. That is the point. A model cannot invent
a new verdict, a new claim type, or a new source tier, because structured
output is validated against these enums and a value outside the set is a
validation failure that triggers a retry.

The contract these implement is docs/01-source-of-truth-prompt.md.

WHICH VERSION A CHANGE HERE BUMPS
=================================
Two different things live in this module and they version differently.

The MEANING vocabularies -- :class:`SourceTier`, :class:`ClaimType`,
:class:`Verdict`, :class:`RedTeamSeverity` -- define what a verdict asserts.
Changing a member of any of those changes what every past run means, so it
requires bumping ``PROMPT_CONTRACT_VERSION``.

The SHAPE vocabularies -- :class:`StageName` and :data:`STAGE_ORDER` -- describe
which stages the pipeline runs. Adding a stage does not change what any verdict
means, so it does not touch the contract version. It does change a persisted
field's set of legal values, so it requires bumping ``SCHEMA_VERSION``.

Getting this backwards in either direction is costly. Bumping the contract for a
pipeline change would make unrelated historical runs look incomparable; NOT
bumping the schema would let two records with different stage vocabularies be
byte-compared as though they were the same format.
"""

from __future__ import annotations

from enum import StrEnum


class SourceTier(StrEnum):
    """Evidence tier of a cited source (contract s2).

    Tier is a property of the *connector* that produced the citation, never
    a judgment the model makes at inference time. This is deliberate and it
    is the single most important structural defense in the system: if a
    model could assign tiers, it could promote a blog post to primary law by
    being confident about it, and every downstream gate would fall over.
    """

    T0 = "T0"  # Primary legal text: Constitution, U.S. Code, CFR, statutes, opinions.
    T1 = "T1"  # Official record and official data: CBO, GAO, BLS, dockets, filings.
    T2 = "T2"  # Peer-reviewed or method-transparent research.
    T3 = "T3"  # Reporting with a corrections policy. Existence of events only.
    T4 = "T4"  # Everything else. NEVER evidence; admissible only as the object.

    @property
    def rank(self) -> int:
        """Numeric rank where LOWER is stronger. T0 -> 0 ... T4 -> 4.

        Provided so tier comparisons read as ordinary integer comparisons
        instead of string comparisons that happen to work by accident.
        """
        return int(self.value[1])

    def is_at_least(self, other: SourceTier) -> bool:
        """True when this tier is at least as strong as ``other``."""
        return self.rank <= other.rank

    @property
    def can_support_verdict(self) -> bool:
        """Whether this tier may support SUPPORTED / CONTRADICTED (contract s2).

        T0-T2 only. T3 alone caps a claim at REPORTED_UNVERIFIED; T4 can
        never raise a verdict above UNSUPPORTED.
        """
        return self.rank <= SourceTier.T2.rank


class ClaimType(StrEnum):
    """Classification of an atomic claim (contract s3).

    The class determines what analysis is legitimate at all. Three of these
    six types are NOT verdict-eligible, and that is the feature, not a
    limitation: most political disagreement is normative wearing empirical
    clothes, and a tool that renders true/false on a value judgment is a
    partisan instrument with a citation format.
    """

    LEGAL = "LEGAL"                # "The law requires/permits/prohibits X."
    EMPIRICAL = "EMPIRICAL"        # "N people did X." / "X caused Y."
    ATTRIBUTIVE = "ATTRIBUTIVE"    # "Person P said/did S."
    PREDICTIVE = "PREDICTIVE"      # "If we do X, Y will follow."
    NORMATIVE = "NORMATIVE"        # "X is unjust/wrong/should be."
    DEFINITIONAL = "DEFINITIONAL"  # "X *is* a Y" for a contested term.

    @property
    def is_verdict_eligible(self) -> bool:
        """Whether evidence can settle this class of claim.

        PREDICTIVE, NORMATIVE and DEFINITIONAL claims are never adjudicated
        as supported or contradicted. They resolve to UNVERIFIABLE, with
        their underlying premises extracted and adjudicated separately.
        """
        return self in {ClaimType.LEGAL, ClaimType.EMPIRICAL, ClaimType.ATTRIBUTIVE}

    @property
    def preferred_tiers(self) -> tuple[SourceTier, ...]:
        """Tiers the retrieval stage should target for this claim type.

        Advisory routing, not a hard gate -- the hard gate is
        :meth:`SourceTier.can_support_verdict`. Ordered strongest first.
        """
        match self:
            case ClaimType.LEGAL:
                return (SourceTier.T0, SourceTier.T1)
            case ClaimType.EMPIRICAL:
                return (SourceTier.T1, SourceTier.T2)
            case ClaimType.ATTRIBUTIVE:
                return (SourceTier.T1, SourceTier.T3)
            case _:
                # Non-eligible types still retrieve, but only to support the
                # decomposition of their premises -- never to reach a verdict.
                return ()


class Verdict(StrEnum):
    """Adjudication outcome for a single claim (contract s4).

    Note what is absent: TRUE and FALSE. Those words invite the tool to
    overreach past its evidence, so the vocabulary does not contain them.
    """

    SUPPORTED = "SUPPORTED"                      # T0-T2 evidence affirms as stated.
    CONTRADICTED = "CONTRADICTED"                # T0-T2 evidence contradicts as stated.
    MIXED = "MIXED"                              # Substantive T0-T2 evidence both ways.
    MISLEADING_CONTEXT = "MISLEADING_CONTEXT"    # Facts check out; framing distorts.
    REPORTED_UNVERIFIED = "REPORTED_UNVERIFIED"  # Only T3 support exists.
    UNSUPPORTED = "UNSUPPORTED"                  # No qualifying evidence either way.
    UNVERIFIABLE = "UNVERIFIABLE"                # Not the kind of claim evidence settles.
    OUT_OF_SCOPE = "OUT_OF_SCOPE"                # Not a political claim.

    @property
    def requires_supporting_evidence(self) -> bool:
        """Whether this verdict is only reachable with a T0-T2 citation.

        Enforced as a model validator on :class:`abca.schema.core.Claim`, so
        a model that returns SUPPORTED with no qualifying citation fails
        validation and is retried rather than being believed.
        """
        return self in {
            Verdict.SUPPORTED,
            Verdict.CONTRADICTED,
            Verdict.MIXED,
            Verdict.MISLEADING_CONTEXT,
        }


class RedTeamSeverity(StrEnum):
    """How far a red-team objection reaches (contract s7).

    Only ``MATERIAL`` changes anything. That threshold exists because an
    adversarial pass with the power to overturn a verdict on any objection
    would simply move the failure: every cited finding could be talked down by
    a sufficiently fluent complaint, and the tool would drift toward saying
    nothing about anything.

    ``NOTED`` and ``MINOR`` are still published. A reader deciding how much
    weight to give a verdict is better served by seeing the objections that did
    not overturn it than by a silent pass.
    """

    NONE = "NONE"          # No meaningful objection found.
    NOTED = "NOTED"        # Worth a reader's attention; changes nothing.
    MINOR = "MINOR"        # A real weakness, insufficient to move the verdict.
    MATERIAL = "MATERIAL"  # Undercuts the verdict. Triggers automatic downgrade.

    @property
    def triggers_downgrade(self) -> bool:
        return self is RedTeamSeverity.MATERIAL


class VerifyOutcome(StrEnum):
    """Result of replaying a recorded run (architecture spec s7)."""

    IDENTICAL = "IDENTICAL"    # Byte-identical output digest.
    EQUIVALENT = "EQUIVALENT"  # Verdicts and citations match; prose differs.
    DRIFTED = "DRIFTED"        # A cited source changed since it was retrieved.
    DIVERGENT = "DIVERGENT"    # Verdicts differ under an identical recipe. A bug.
    UNREPLAYABLE = "UNREPLAYABLE"  # Recipe cannot be reconstructed (e.g. hosted API).

    @property
    def is_success(self) -> bool:
        """Whether this outcome means the original run is confirmed.

        DRIFTED is deliberately NOT a success. A verdict whose source has
        changed underneath it is a verdict that needs re-examination by a
        human, even though nothing was done wrong at the time.
        """
        return self in {VerifyOutcome.IDENTICAL, VerifyOutcome.EQUIVALENT}


class InputKind(StrEnum):
    """How the analyzed document was supplied (architecture spec s2)."""

    TEXT = "text"    # -t / --text
    FILE = "file"    # -f / --file
    URL = "url"      # -u / --url
    USER = "user"    # -U / --user
    STDIN = "stdin"  # --stdin


class Profile(StrEnum):
    """Latency/thoroughness profile (architecture spec s2)."""

    FAST = "fast"          # 1 small model, cache-first. Target < 10s.
    STANDARD = "standard"  # 1 mid model, full retrieval + red team.
    FORENSIC = "forensic"  # Consensus, exhaustive retrieval, second adversarial round.


class StageName(StrEnum):
    """Pipeline stages, in execution order (architecture spec s3).

    Declared as an enum so the ledger cannot record a stage name that the
    pipeline does not actually have, and so ``verify`` can compare stage
    sequences structurally.
    """

    INGEST = "ingest"
    SEGMENT = "segment"
    CLASSIFY = "classify"
    GATE = "gate"
    CLUSTER = "cluster"
    SOURCELESS = "sourceless"
    RETRIEVE = "retrieve"
    ADJUDICATE = "adjudicate"
    FIDELITY = "fidelity"
    RED_TEAM = "red_team"
    COMPOSE = "compose"


#: Canonical execution order. Used to validate that a recorded stage chain
#: is a subsequence of the real pipeline -- a record claiming ADJUDICATE ran
#: before CLASSIFY is corrupt or forged.
STAGE_ORDER: tuple[StageName, ...] = (
    StageName.INGEST,
    StageName.SEGMENT,
    StageName.CLASSIFY,
    StageName.GATE,
    StageName.CLUSTER,
    StageName.SOURCELESS,
    StageName.RETRIEVE,
    StageName.ADJUDICATE,
    StageName.FIDELITY,
    StageName.RED_TEAM,
    StageName.COMPOSE,
)


__all__ = [
    "STAGE_ORDER",
    "ClaimType",
    "InputKind",
    "Profile",
    "RedTeamSeverity",
    "SourceTier",
    "StageName",
    "Verdict",
    "VerifyOutcome",
]
