"""Legal elements: the unit the fidelity gate diffs.

WHAT AN ELEMENT IS
==================
A single operative piece of a legal provision: who is bound, one thing required,
one condition, one exception, one penalty. Provisions are decomposed into these
so that "did the plain-language rewrite preserve the law?" becomes a checkable
question about a set rather than a judgment about a paragraph.

WHY THE DIFF IS DETERMINISTIC
=============================
The obvious way to compare a source element against a reconstructed one is to
ask a model whether they mean the same thing. That would be a mistake here, for
the same reason retrieval and sentence splitting are not model calls: the
fidelity score goes into the run record and drives regeneration, so a model
deciding "close enough" would make the gate irreproducible and would put the
judgment being audited inside the auditor.

So matching is code. It leans on three signals a model cannot blur:

1. **Kind** must match exactly. A REQUIREMENT that comes back as a CONDITION is
   not a labelling quibble -- it is the rewrite having changed what the
   provision does.
2. **Modal** must match exactly. ``shall`` is not ``may``; ``must not`` is not
   ``need not``. This is the single highest-signal check available, and it is
   fully mechanical.
3. **Numeric anchors** must all survive. Numbers, percentages and dates are
   where legal meaning most often changes under simplification, and they are
   trivially comparable.

Only after those three pass does a token-overlap threshold decide whether the
prose is about the same thing.

THE ASYMMETRY THAT SHAPES EVERY THRESHOLD
=========================================
A false FAIL costs a regeneration and, in the worst case, falls back to emitting
the statute verbatim -- which is honest, if less readable. A false PASS ships a
plain-language rendering that changed the law while claiming to preserve it.

Those costs are not comparable, so every threshold here is set strict. "We could
not safely simplify this; here is the exact text" is an acceptable outcome and
the code is written to reach it rather than to avoid it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class ElementKind(StrEnum):
    """What role an element plays in a provision.

    Kinds must match EXACTLY when diffing. The pairs most worth confusing are
    also the ones it is most dangerous to confuse:

    * REQUIREMENT / PROHIBITION / PERMISSION -- the obligation family. Mixing
      these up is exactly the modal error the gate exists to catch.
    * CONDITION / EXCEPTION -- a condition says *when the rule applies*; an
      exception says *when it does not*. Swapping them inverts the provision.
    """

    WHO_IS_BOUND = "WHO_IS_BOUND"
    REQUIREMENT = "REQUIREMENT"
    PROHIBITION = "PROHIBITION"
    PERMISSION = "PERMISSION"
    CONDITION = "CONDITION"
    EXCEPTION = "EXCEPTION"
    EFFECTIVE_DATE = "EFFECTIVE_DATE"
    PENALTY = "PENALTY"
    DEFINITION = "DEFINITION"


class Modal(StrEnum):
    """The force of an obligation.

    NOT a strength ordering. Any change between two of these is a failure,
    including changes that look like softening AND changes that look like
    strengthening: a rewrite that turns ``may`` into ``shall`` invents an
    obligation just as surely as one that turns ``shall`` into ``may`` erases
    one.
    """

    PROHIBITION = "PROHIBITION"        # shall not, must not, may not
    MANDATORY = "MANDATORY"            # shall, must, is required to
    ADVISORY = "ADVISORY"              # should
    PERMISSIVE = "PERMISSIVE"          # may, is permitted to, is authorized to
    NO_OBLIGATION = "NO_OBLIGATION"    # need not, is not required to
    NONE = "NONE"                      # the element carries no obligation


#: Modal detection patterns, in priority order.
#:
#: ORDER IS LOAD-BEARING. "shall not" must be tested before "shall", or every
#: prohibition in the corpus is silently read as a mandate -- which is the
#: single worst misreading this module could produce.
_MODAL_PATTERNS: tuple[tuple[Modal, re.Pattern[str]], ...] = (
    (Modal.PROHIBITION, re.compile(
        r"\b(?:shall|must|may|will|can)\s+(?:not|never)\b"
        r"|\bmay\s+no\b"
        r"|\b(?:is|are)\s+(?:prohibited|forbidden|barred|not\s+permitted|not\s+allowed)\b"
        r"|\bno\s+(?:\w+\s+){1,4}?(?:shall|may|must|will|can)\b",
        re.IGNORECASE)),
    (Modal.NO_OBLIGATION, re.compile(
        r"\bneed\s+not\b"
        r"|\b(?:is|are)\s+not\s+required\b"
        r"|\bnot\s+obligated\b",
        re.IGNORECASE)),
    (Modal.MANDATORY, re.compile(
        r"\b(?:shall|must)\b"
        r"|\b(?:is|are)\s+required\s+to\b"
        r"|\bhas\s+a\s+duty\s+to\b",
        re.IGNORECASE)),
    (Modal.ADVISORY, re.compile(r"\bshould\b", re.IGNORECASE)),
    (Modal.PERMISSIVE, re.compile(
        r"\bmay\b"
        r"|\b(?:is|are)\s+(?:permitted|authorized|entitled|allowed)\s+to\b"
        r"|\bhas\s+the\s+option\b",
        re.IGNORECASE)),
)

#: Numeric anchors. All of these must survive a rewrite intact.
#: Ordered longest-pattern-first so "25,000" is not split into "25" and "000",
#: and so "1%" is captured as a percentage rather than a bare number.
_ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("percent", re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent)", re.IGNORECASE)),
    ("money", re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")),
    ("date", re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September"
        r"|October|November|December)\s+\d{1,2},?\s+\d{4}\b"
        r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
        r"|\b(?:19|20)\d{2}\b", re.IGNORECASE)),
    ("duration", re.compile(
        r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years|hour|hours)\b",
        re.IGNORECASE)),
    ("number", re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")),
)

#: Words carrying no discriminating power in a token-overlap comparison.
_STOPWORDS: frozenset[str] = frozenset(
    ["a", "an", "and", "the", "of", "to", "in", "for", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "that", "this", "these", "those", "it", "its", "their", "his", "her", "they", "them", "there", "here", "or", "but", "if", "then", "than", "such", "which", "who", "whom", "whose", "what", "when", "where", "how", "any", "some", "all", "each", "every", "no", "not", "nor", "shall", "must", "may", "should", "will", "can", "would", "could", "have", "has", "had", "do", "does", "did"]
)

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")

#: Suffixes stripped by :func:`stem`, longest first.
_SUFFIXES: tuple[str, ...] = ("'s", "ies", "sses", "ing", "ed", "es", "s")

_VOWELS = frozenset("aeiouy")


def stem(word: str) -> str:
    """Reduce a word to a crude stem. Deliberately conservative.

    WHY THIS EXISTS
    ---------------
    A plain-language rewrite of a statute uses DIFFERENT WORDS -- that is the
    entire point of the exercise. Raw token comparison therefore punishes
    precisely the behaviour being asked for: "all offices to be filled" and
    "every office being filled" share almost no exact tokens while saying the
    same thing.

    WHY IT IS CONSERVATIVE
    ----------------------
    Over-stemming is the dangerous direction. A stemmer aggressive enough to
    collapse "filing" and "file" would also collapse words that differ in legal
    meaning, and two unrelated elements would start pairing up -- turning the
    diff into something that confirms itself. So this handles only plurals,
    possessives and the two commonest verb inflections, and leaves everything
    else alone.
    """
    for suffix in _SUFFIXES:
        if not word.endswith(suffix):
            continue
        stripped = word[: -len(suffix)]
        if suffix == "ies":
            stripped += "y"
        elif suffix == "sses":
            stripped += "ss"
        # Require a real stem left over, with a vowel in it, so "is" does not
        # become "i" and "gas" does not become "ga".
        if len(stripped) >= 3 and _VOWELS & set(stripped):
            # "filled" -> "fill", "stopped" -> "stop"
            if (
                suffix in {"ing", "ed"}
                and len(stripped) >= 4
                and stripped[-1] == stripped[-2]
                and stripped[-1] not in _VOWELS
            ):
                stripped = stripped[:-1]
            return _strip_final_e(stripped)
    return _strip_final_e(word)


def _strip_final_e(word: str) -> str:
    """Drop a trailing silent ``e``.

    Without this, "offices" stems to "offic" while "office" stays "office", and
    the two never unify -- the plural rule strips ``es`` where the singular has
    nothing to strip. Removing the final ``e`` from both lands them on the same
    stem, and it also unifies the "file"/"filing"/"filed" family, which appears
    constantly in filing statutes.
    """
    return word[:-1] if len(word) >= 4 and word.endswith("e") else word


def detect_modal(text: str) -> Modal:
    """Determine the modal force of ``text``.

    Deterministic and pattern-order dependent -- see ``_MODAL_PATTERNS``.

    Used to CROSS-CHECK whatever modal a model reports. A model that labels a
    "shall not" clause as MANDATORY has misread the provision, and taking its
    label at face value would let exactly that misreading through the gate. The
    detector's answer wins.
    """
    for modal, pattern in _MODAL_PATTERNS:
        if pattern.search(text):
            return modal
    return Modal.NONE


def extract_anchors(text: str) -> list[str]:
    """Pull the numeric and date anchors out of ``text``.

    Extracted from the TEXT, never supplied by a model. A model that could
    declare its own anchors could declare the ones it happened to preserve, and
    the check would confirm itself.

    Overlapping matches are resolved longest-first so "25,000 qualified voters"
    yields ``25,000`` rather than ``25`` and ``000``.
    """
    spans: list[tuple[int, int, str]] = []
    for _, pattern in _ANCHOR_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), match.group(0)))

    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    anchors: list[str] = []
    consumed_to = -1
    for start, end, value in spans:
        if start < consumed_to:
            continue  # inside a longer anchor already taken
        consumed_to = end
        anchors.append(_normalize_anchor(value))
    return anchors


def _normalize_anchor(value: str) -> str:
    """Canonicalize an anchor so trivial formatting does not read as a change.

    ``25,000`` and ``25000`` are the same number; ``1 percent`` and ``1%`` are
    the same proportion. Nothing that changes VALUE is normalized away.
    """
    text = value.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = text.replace(",", "")
    text = re.sub(r"\s*percent\b", "%", text)
    text = re.sub(r"\s*%", "%", text)
    text = re.sub(r"\$\s+", "$", text)
    return text


def content_tokens(text: str) -> set[str]:
    """Lowercase, stemmed content words, stopwords removed.

    Modal verbs are stopwords here on purpose: they are compared separately and
    exactly, so leaving them in the overlap score would let a matching ``shall``
    paper over otherwise unrelated prose.
    """
    return {
        stem(token) for token in _WORD.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 2
    }


@dataclass(frozen=True, slots=True)
class LegalElement:
    """One operative piece of a provision."""

    kind: ElementKind
    text: str
    #: Modal as detected from ``text``. Never taken from a model -- see
    #: :func:`detect_modal`.
    modal: Modal = Modal.NONE
    #: Numeric/date anchors extracted from ``text``.
    anchors: tuple[str, ...] = ()

    @classmethod
    def build(cls, kind: ElementKind, text: str) -> LegalElement:
        """Construct an element, deriving modal and anchors from the text."""
        cleaned = " ".join(text.split())
        return cls(
            kind=kind,
            text=cleaned,
            modal=detect_modal(cleaned),
            anchors=tuple(extract_anchors(cleaned)),
        )

    @property
    def tokens(self) -> set[str]:
        return content_tokens(self.text)

    #: Characters of element text shown in :meth:`summary`.
    _SUMMARY_CHARS = 100

    def summary(self) -> str:
        """Short human-readable form, for diff output.

        Truncation is marked. An unmarked cut looks like the element itself
        ended there, which in a diff report about dropped clauses is exactly
        the wrong impression to leave.
        """
        modal = f" [{self.modal.value}]" if self.modal is not Modal.NONE else ""
        text = self.text
        if len(text) > self._SUMMARY_CHARS:
            text = text[: self._SUMMARY_CHARS].rstrip() + "..."
        return f"{self.kind.value}{modal}: {text}"


#: Minimum token similarity for two elements to be considered the same, once
#: kind, modal and anchors already agree.
#:
#: This is the WEAKEST of the four signals and it is meant to be. Kind, modal
#: and numeric anchors are exact checks that already block every dangerous
#: confusion -- a requirement read as a prohibition, a "shall" turned into a
#: "may", a 25,000 that became 20,000. All this threshold has to do is stop two
#: UNRELATED elements that happen to share a kind and a modal from pairing up.
#:
#: Set too high, legitimate rewrites fail for using different words, which is
#: the one thing a plain-language rewrite is supposed to do.
MATCH_THRESHOLD = 0.35


def similarity(left: LegalElement, right: LegalElement) -> float:
    """F1 of stemmed content-token overlap. 0.0 when either side is empty.

    F1 rather than Jaccard because a good rewrite ADDS words -- articles,
    connectives, an explanatory noun -- and Jaccard charges the whole union for
    them. F1 balances "how much of the source is covered" against "how much of
    the candidate is on topic", which is the question actually being asked.
    """
    left_tokens, right_tokens = left.tokens, right.tokens
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    if not overlap:
        return 0.0
    coverage = overlap / len(left_tokens)
    precision = overlap / len(right_tokens)
    return 2 * coverage * precision / (coverage + precision)


def elements_match(source: LegalElement, candidate: LegalElement) -> tuple[bool, str]:
    """Whether ``candidate`` reconstructs ``source``. Returns ``(ok, reason)``.

    The reason is returned even on success so the diff can report near-misses,
    which is what an operator tuning a prompt actually needs to see.
    """
    if source.kind is not candidate.kind:
        return False, f"kind {source.kind.value} became {candidate.kind.value}"

    if source.modal is not candidate.modal:
        return False, (
            f"modal {source.modal.value} became {candidate.modal.value} -- "
            "the force of the obligation changed"
        )

    missing = [anchor for anchor in source.anchors if anchor not in candidate.anchors]
    if missing:
        return False, f"lost numeric anchor(s): {', '.join(missing)}"

    score = similarity(source, candidate)
    if score < MATCH_THRESHOLD:
        return False, f"token overlap {score:.2f} below {MATCH_THRESHOLD}"

    return True, f"matched at {score:.2f}"


@dataclass(slots=True)
class ElementDiff:
    """Result of diffing a reconstruction against the source elements."""

    matched: list[tuple[LegalElement, LegalElement]] = field(default_factory=list)
    #: In the source, absent from the reconstruction. The rewrite dropped it.
    dropped: list[tuple[LegalElement, str]] = field(default_factory=list)
    #: In the reconstruction, absent from the source. The rewrite invented it.
    added: list[LegalElement] = field(default_factory=list)

    @property
    def source_count(self) -> int:
        return len(self.matched) + len(self.dropped)

    @property
    def preserved_count(self) -> int:
        return len(self.matched)

    @property
    def score(self) -> float:
        """Elements preserved / elements in source. 1.0 when the source is empty."""
        total = self.source_count
        return 1.0 if total == 0 else self.preserved_count / total

    @property
    def passed(self) -> bool:
        """A clean pass requires nothing dropped AND nothing invented.

        Added elements fail as hard as dropped ones. A rewrite that introduces
        an obligation the statute does not contain is not a readability
        improvement, it is a different law.
        """
        return not self.dropped and not self.added

    def failures(self) -> list[str]:
        """Human-readable reasons this diff did not pass."""
        reasons = [f"DROPPED {element.summary()} ({why})" for element, why in self.dropped]
        reasons += [f"ADDED {element.summary()}" for element in self.added]
        return reasons


def diff_elements(
    source: list[LegalElement],
    reconstructed: list[LegalElement],
) -> ElementDiff:
    """Match a reconstruction against the source, greedily and best-first.

    Each reconstructed element is consumed by at most one source element, so a
    single vague sentence in the rewrite cannot stand in for three distinct
    statutory requirements -- which is precisely how a lossy simplification
    would otherwise score well.

    Pairing is best-first by similarity rather than in order, because a
    reconstruction is free to reorder elements and order carries no legal
    meaning.
    """
    diff = ElementDiff()
    available = list(range(len(reconstructed)))

    for element in source:
        best_index: int | None = None
        best_score = -1.0
        best_reason = "no candidate of the same kind, modal and anchors"

        for index in available:
            candidate = reconstructed[index]
            ok, reason = elements_match(element, candidate)
            if not ok:
                # Keep the most informative near-miss for the report: a modal
                # flip is far more useful to see than "no candidate".
                if best_index is None and "modal" in reason:
                    best_reason = reason
                continue
            score = similarity(element, candidate)
            if score > best_score:
                best_index, best_score, best_reason = index, score, reason

        if best_index is None:
            diff.dropped.append((element, best_reason))
        else:
            diff.matched.append((element, reconstructed[best_index]))
            available.remove(best_index)

    diff.added = [reconstructed[index] for index in available]
    return diff


__all__ = [
    "MATCH_THRESHOLD",
    "ElementDiff",
    "ElementKind",
    "LegalElement",
    "Modal",
    "content_tokens",
    "detect_modal",
    "diff_elements",
    "elements_match",
    "extract_anchors",
    "similarity",
]
