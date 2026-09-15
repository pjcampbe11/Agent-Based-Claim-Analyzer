"""The solve stage: propose interventions, then rank them in code.

THE DIVISION OF LABOUR
======================
The model DESCRIBES. It writes what an intervention is, how it is supposed to
work, which authority it acts under, roughly what it costs, how fast it bites,
and how hard it is to undo. Those are descriptions, and a model is good at them.

Code DECIDES the order. The ranking below is arithmetic over the model's
structured answers, with fixed weights that are visible in this file and in the
published run. Nothing about "which option is best" passes through a model.

This matters more here than anywhere else in the pipeline. A ranked list of
policy options is the output most likely to be quoted as though the tool
endorsed the top item, and a ranking a model produced by free composition would
be an opinion with a number attached -- reproducible only by accident, and
tunable by anyone who could reword the prompt.

WHAT THE WEIGHTS ENCODE, AND WHY REVERSIBILITY IS HEAVIEST
==========================================================
The method assumes the analysis will sometimes be wrong and pre-commits to
publishing the contradiction when it is. That commitment is worth nothing if
the plan already in motion cannot be stopped. So an option that can be undone
is preferred over an equally effective one that cannot -- not because caution
is a virtue in itself, but because reversibility is what makes the
error-correction promise operable.

The weights are a VALUE JUDGMENT. They are the tool's defaults, stated here in
the open, they are published with every run, and anyone who prefers different
ones can change four numbers and re-run. What they are not is a fact, and the
report says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from abca.schema.enums import SourceTier
from abca.schema.issue import CostBand, Reversibility, TimeToEffect
from abca.schema.plan import Intervention

#: Ranking weights. Must sum to 1.0; asserted at import so a careless edit
#: fails loudly rather than silently rescaling every score in the archive.
WEIGHT_REVERSIBILITY = 0.30
WEIGHT_TIME = 0.25
WEIGHT_COST = 0.25
WEIGHT_EVIDENCE = 0.20

assert abs(
    WEIGHT_REVERSIBILITY + WEIGHT_TIME + WEIGHT_COST + WEIGHT_EVIDENCE - 1.0
) < 1e-9, "ranking weights must sum to 1.0"


def _normalize(rank: int, count: int) -> float:
    """Map an enum rank to 0..1 where 1 is best (rank 0) and 0 is worst."""
    return 1.0 - (rank / (count - 1)) if count > 1 else 1.0


def _evidence_score(intervention: Intervention) -> float:
    """Strength of the best citation backing this intervention.

    An uncited intervention scores zero rather than being excluded. Excluding
    it would hide it; scoring it zero puts it at the bottom of a list the
    reader can still see, which is the more honest failure.
    """
    if not intervention.citations:
        return 0.0
    best = min(intervention.citations, key=lambda c: c.tier.rank).tier
    return _normalize(best.rank, len(SourceTier))


@dataclass(frozen=True, slots=True)
class RankedIntervention:
    """An intervention with its computed score and the arithmetic behind it."""

    intervention: Intervention
    score: float
    components: dict[str, float]

    def explain(self) -> str:
        """One line showing how the score was reached. Printed in the report.

        A ranking nobody can reproduce by hand is a ranking that has to be
        taken on trust, and this repository does not ask for trust anywhere
        else either.
        """
        parts = " + ".join(
            f"{name}={value:.2f}x{weight:.2f}"
            for name, (value, weight) in self.components_with_weights().items()
        )
        return f"{self.score:.3f} = {parts}"

    def components_with_weights(self) -> dict[str, tuple[float, float]]:
        return {
            "reversibility": (self.components["reversibility"], WEIGHT_REVERSIBILITY),
            "speed": (self.components["speed"], WEIGHT_TIME),
            "cost": (self.components["cost"], WEIGHT_COST),
            "evidence": (self.components["evidence"], WEIGHT_EVIDENCE),
        }


def rank_interventions(interventions: list[Intervention]) -> list[RankedIntervention]:
    """Score and order interventions. Deterministic; no model involved.

    Ties break on ``id``, ascending, so an identical input always produces an
    identical order. Two options with the same score are genuinely tied and the
    report says so rather than implying a preference the arithmetic does not
    support.
    """
    ranked: list[RankedIntervention] = []
    for intervention in interventions:
        components = {
            "reversibility": _normalize(
                intervention.reversibility.rank, len(Reversibility)
            ),
            "speed": _normalize(intervention.time_to_effect.rank, len(TimeToEffect)),
            "cost": _normalize(intervention.cost_band.rank, len(CostBand)),
            "evidence": _evidence_score(intervention),
        }
        score = round(
            components["reversibility"] * WEIGHT_REVERSIBILITY
            + components["speed"] * WEIGHT_TIME
            + components["cost"] * WEIGHT_COST
            + components["evidence"] * WEIGHT_EVIDENCE,
            4,
        )
        ranked.append(RankedIntervention(intervention, score, components))

    ranked.sort(key=lambda r: (-r.score, r.intervention.id))
    return ranked


def tied_at_top(ranked: list[RankedIntervention]) -> tuple[RankedIntervention, ...]:
    """Every intervention sharing the top score.

    Exposed so the report can say "three options are tied" instead of printing
    the first one as though the arithmetic chose it.
    """
    if not ranked:
        return ()
    best = ranked[0].score
    return tuple(r for r in ranked if r.score == best)


#: The one sentence every ranked list is published with. Not a disclaimer to
#: be skipped -- it names the weights as a value judgment so that a reader
#: who disagrees knows exactly which four numbers to argue with.
RANKING_DISCLOSURE = (
    "This ordering is arithmetic over the four weights in "
    "abca/pipeline/solve.py: reversibility 0.30, speed 0.25, cost 0.25, "
    "evidence 0.20. Those weights are a value judgment, not a "
    "finding. Change them and re-run to see a different order."
)


__all__ = [
    "RANKING_DISCLOSURE",
    "WEIGHT_COST",
    "WEIGHT_EVIDENCE",
    "WEIGHT_REVERSIBILITY",
    "WEIGHT_TIME",
    "RankedIntervention",
    "rank_interventions",
    "tied_at_top",
]
