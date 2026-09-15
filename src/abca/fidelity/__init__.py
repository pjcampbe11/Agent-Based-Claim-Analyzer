"""Deterministic machinery for the plain-language fidelity gate (contract s5).

Nothing in this package calls a model. That is the point.

The gate's question -- "did the plain-language rewrite preserve the law?" --
is answered by :mod:`abca.pipeline.fidelity`, which orchestrates three model
calls. But the JUDGE of those calls lives here, in code:

* :mod:`.elements` decomposes a provision into operative pieces and diffs a
  reconstruction against them.
* :mod:`.readability` scores the rewrite's reading grade.
* :mod:`.linters` counts scope markers and locked terms of art on both sides.

Keeping the judgment out of a model is what makes the fidelity score
reproducible and what keeps the thing being audited outside the auditor. A
model asked "are these two the same?" would be grading the work of a model,
with no way for a third party to re-derive the answer.
"""

from abca.fidelity.elements import (
    MATCH_THRESHOLD,
    ElementDiff,
    ElementKind,
    LegalElement,
    Modal,
    content_tokens,
    detect_modal,
    diff_elements,
    elements_match,
    extract_anchors,
    similarity,
)
from abca.fidelity.linters import (
    LOCKED_GLOSSARY,
    SCOPE_MARKERS,
    GlossaryReport,
    ScopeReport,
    glossary_lint,
    scope_lint,
)
from abca.fidelity.readability import (
    TARGET_GRADE_MAX,
    TARGET_GRADE_MIN,
    ReadabilityScore,
    count_sentences,
    count_syllables,
    flesch_kincaid,
)

__all__ = [
    "LOCKED_GLOSSARY",
    "MATCH_THRESHOLD",
    "SCOPE_MARKERS",
    "TARGET_GRADE_MAX",
    "TARGET_GRADE_MIN",
    "ElementDiff",
    "ElementKind",
    "GlossaryReport",
    "LegalElement",
    "Modal",
    "ReadabilityScore",
    "ScopeReport",
    "content_tokens",
    "count_sentences",
    "count_syllables",
    "detect_modal",
    "diff_elements",
    "elements_match",
    "extract_anchors",
    "flesch_kincaid",
    "glossary_lint",
    "scope_lint",
    "similarity",
]
