"""The distortion taxonomy, as versioned DATA rather than prose in a prompt.

WHY THIS IS A DATA FILE
=======================
Doc 18 listed the Stage 2b transformations inline in the prompt, and its own
open question 3 says they should be data instead. They are, here, for a reason
worth more than tidiness:

**A distortion named in prose can be described but not counted.** Once it is an
enumerated value the pipeline can report which transformations are most common
across ten thousand posts -- "committee vote reported as floor vote" happening
four times more often than everything else is a genuinely publishable finding
about political language, and it is the kind of thing that justifies keeping
a record at all.

It also constrains the model. A free-text distortion field invites invention;
selecting from a closed set means an unrecognised transformation shows up as
``OTHER`` and gets counted as an unrecognised transformation, which is the
honest outcome.

THE ORDERING PRINCIPLE
======================
Doc 18 s2b: *"The smallest sufficient distortion is the most likely one. A claim
requiring three independent distortions to reach a plausible seed probably has a
different seed."*

So each entry carries a ``steps`` count -- how many independent transformations
it represents -- and :func:`rank_candidates` sorts by it. That turns a stated
heuristic into arithmetic the pipeline actually applies.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bumped when an entry's MEANING changes, which invalidates cross-run counts.
#: Adding an entry is a minor bump; redefining one is a major bump.
TAXONOMY_VERSION = "distortions/1.0.0"


@dataclass(frozen=True, slots=True)
class Distortion:
    """One named transformation from a real event into the claim as posted."""

    name: str
    #: What the real thing was.
    source: str
    #: What the post turned it into.
    target: str
    #: How many independent transformations this represents. Fewer is likelier.
    steps: int
    #: The mechanism domain, for context-pack selection (doc 19 s3).
    domain: str
    #: Why this one happens -- the incentive, not a moral judgment.
    note: str

    @property
    def label(self) -> str:
        """The wire form the model emits: ``committee vote -> floor vote``."""
        return f"{self.source} -> {self.target}"


#: The taxonomy. Ordered by ``steps`` then by how often the transformation is
#: observed, so the cheapest explanations sort first.
#:
#: `domain` maps each entry onto the doc 19 context-pack domains, so selecting a
#: distortion also selects the mechanism entries that explain both sides of it.
#: That is the join that makes the brief useful: the reader is told what the
#: real thing was, what it became, and what the difference means.
DISTORTION_TAXONOMY: tuple[Distortion, ...] = (
    Distortion(
        "procedural_to_substantive", "procedural vote", "substantive vote", 1, "procedure",
        "A procedural motion is a vote about handling, not about policy. Reporting it "
        "as a policy vote is the single most common distortion in US political posts.",
    ),
    Distortion(
        "committee_to_floor", "committee vote", "floor vote", 1, "lawmaking",
        "A committee is a subset of a chamber. Attributing its action to the chamber "
        "inflates both the stakes and the number of people implicated.",
    ),
    Distortion(
        "amendment_to_passage", "amendment vote", "final passage", 1, "procedure",
        "An amendment vote is about one change to a text, not about the text.",
    ),
    Distortion(
        "introduced_to_passed", "introduced", "passed", 1, "lawmaking",
        "Anyone can introduce anything. Introduction signals intent and predicts "
        "almost nothing about outcome.",
    ),
    Distortion(
        "chamber_to_congress", "passed one chamber", "Congress", 1, "lawmaking",
        "Congress is two chambers. One chamber acting is half a process, not a law.",
    ),
    Distortion(
        "proposed_to_enacted_rule", "proposed rule", "enacted rule", 1, "instruments",
        "A notice of proposed rulemaking opens a comment period. It binds nobody yet.",
    ),
    Distortion(
        "order_to_statute", "executive order", "statute", 1, "instruments",
        "An executive order directs the executive branch and can be revoked by the "
        "next one. A statute cannot.",
    ),
    Distortion(
        "state_to_federal", "state law", "federal law", 1, "instruments",
        "Jurisdiction swap. Usually inflates scope from one state to the country.",
    ),
    Distortion(
        "federal_to_state", "federal law", "state law", 1, "instruments",
        "The reverse swap, usually to make a national rule look local and optional.",
    ),
    Distortion(
        "draft_to_final", "draft text", "final text", 1, "lawmaking",
        "Draft language is frequently struck before passage. Quoting a draft as "
        "enacted text is quoting something that never took effect.",
    ),
    Distortion(
        "title_to_text", "bill title", "bill text", 1, "lawmaking",
        "Bill titles are written to persuade. The operative text is what governs, "
        "and it routinely does not match.",
    ),
    Distortion(
        "guidance_to_law", "agency guidance", "binding law", 1, "instruments",
        "Guidance explains how an agency reads a rule. It is not itself the rule.",
    ),
    Distortion(
        "ruling_to_legislation", "court ruling", "legislation", 1, "instruments",
        "A court decides a case. It does not write statute, even when the effect "
        "is comparable.",
    ),
    Distortion(
        "vehicle_to_provision", "vote on an omnibus vehicle", "vote on one provision",
        1, "budget",
        "A vote on a package containing X is not a vote on X. This is how nearly "
        "every 'voted against' attack is constructed.",
    ),
    Distortion(
        "authorization_to_appropriation", "authorization", "appropriation", 1, "budget",
        "Authorizing a program permits spending. Appropriating provides the money. "
        "A program can be authorized and unfunded.",
    ),
    Distortion(
        "score_without_baseline", "CBO score in context", "CBO score without baseline",
        1, "budget",
        "A score is meaningless without its baseline, window and assumptions, all of "
        "which the score itself states.",
    ),
    Distortion(
        "old_as_current", "past event", "current event", 1, "lawmaking",
        "Recirculation. The event is real and the timing is what makes the post wrong.",
    ),
    Distortion(
        "quote_stripped", "quote in context", "quote without context", 1, "lawmaking",
        "The words are accurate and the surrounding sentence reverses them.",
    ),
    Distortion(
        "level_as_rate", "a level", "a rate", 1, "statistics",
        "A falling rate of increase is still an increase. Level-versus-rate is the "
        "most common statistical distortion and is usually arithmetically true.",
    ),
    Distortion(
        "nominal_as_real", "nominal dollars", "real dollars", 1, "statistics",
        "Uncorrected for inflation, any long time series shows growth.",
    ),
    Distortion(
        "denominator_swapped", "one denominator", "a different denominator", 1, "statistics",
        "Per-capita, per-household and absolute totals tell different stories about "
        "the same numerator.",
    ),
    Distortion(
        "baseline_shifted", "a full series", "a cherry-picked window", 1, "statistics",
        "Start-date selection. The series is real; the window was chosen for shape.",
    ),
    Distortion(
        "committee_procedural_to_policy", "committee procedural vote", "chamber policy vote",
        2, "procedure",
        "Two transformations at once. Under the smallest-distortion rule this should "
        "lose to any single-step explanation that fits.",
    ),
    Distortion(
        "other", "unrecognised", "unrecognised", 9, "lawmaking",
        "No catalogued transformation fits. Counted as unrecognised rather than "
        "forced into the nearest label, so the taxonomy's gaps stay visible.",
    ),
)

_BY_NAME = {d.name: d for d in DISTORTION_TAXONOMY}
_BY_LABEL = {d.label: d for d in DISTORTION_TAXONOMY}


def distortion_by_name(name: str) -> Distortion:
    """Look up a distortion by name or wire label. Unknown values map to ``other``.

    Coercing rather than raising is deliberate: an unrecognised transformation is
    a fact about the taxonomy's coverage, and losing the whole analysis over it
    would hide exactly the signal that tells us to add an entry.
    """
    key = name.strip()
    if key in _BY_NAME:
        return _BY_NAME[key]
    if key in _BY_LABEL:
        return _BY_LABEL[key]
    normalized = key.lower().replace("→", "->").replace("  ", " ")
    for label, distortion in _BY_LABEL.items():
        if label.lower() == normalized:
            return distortion
    return _BY_NAME["other"]


def rank_candidates(names: list[str]) -> list[Distortion]:
    """Order distortions cheapest-first, applying doc 18's smallest-distortion rule.

    Stable within a step count, so equal-cost explanations keep the order the
    model proposed them in rather than being silently reordered.
    """
    return sorted((distortion_by_name(n) for n in names), key=lambda d: d.steps)


def domains_for(names: list[str]) -> list[str]:
    """The context-pack domains these distortions touch, deduplicated, in order.

    The join to doc 19: naming a distortion also names the mechanism entries that
    explain both sides of it.
    """
    seen: list[str] = []
    for distortion in rank_candidates(names):
        if distortion.domain not in seen:
            seen.append(distortion.domain)
    return seen


__all__ = [
    "DISTORTION_TAXONOMY",
    "TAXONOMY_VERSION",
    "Distortion",
    "distortion_by_name",
    "domains_for",
    "rank_candidates",
]
