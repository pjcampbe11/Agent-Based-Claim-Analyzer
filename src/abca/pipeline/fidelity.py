"""The plain-language fidelity gate: three passes, one deterministic verdict.

WHAT THIS STAGE IS FOR
======================
The contract promises legal text rewritten so an eighth-grader understands it
"without the meaning drifting" (contract s5). Every part of that promise is easy
to make and hard to keep, because simplification is exactly where legal meaning
dies -- and it dies quietly. A rewrite that drops an exception reads BETTER than
one that keeps it. Fluency and fidelity pull in opposite directions, and only
one of them is visible to the reader.

So the rewrite is not trusted. It is tested.

THE THREE PASSES
================

.. code-block:: text

    source provision
          |
          v
    [A] extract        (renderer model)  -> operative elements, verbatim
          |
          +-----------------------------+
          v                             |
    [B] render         (renderer model)  |  sees source + elements
          |                             |
          v                             |
      rendering  --------+              |
          |              |              |
          |              v              |
          |    [C] back-translate       |  DIFFERENT model.
          |        (backtranslate model) |  Sees ONLY the rendering.
          |              |              |
          |              v              |
          |      reconstructed elements  |
          |              |              |
          +--------> deterministic diff <+
                         |
                         v
              pass -> ship the rendering
              fail -> regenerate [B], up to MAX_RENDER_ATTEMPTS
              exhausted -> ship the SOURCE TEXT VERBATIM, unresolved=True

WHY PASS C GETS A DIFFERENT MODEL, AND ONLY THE RENDERING
==========================================================
Two separate isolations, and losing either one makes the gate a no-op that
reports success:

**Different model.** A model that grades its own simplification reproduces its
own misreadings. If it read "shall" as "may" in pass B, it will read the "may"
in its own rewrite as faithful in pass C. The gate would pass everything and
prove nothing. Enforced in three places -- :mod:`abca.config` at load,
:class:`~abca.schema.ledger.RunConfig` at record time, and
:func:`run_fidelity` at run time -- because it is the kind of mistake that
produces no error and no visible symptom.

**No source access.** :func:`build_backtranslate_prompt` takes the rendering and
nothing else. Not "is told to ignore the source"; it never receives one. A
model that could see the provision would reconstruct the provision, whether or
not the rewrite preserved it, and the diff would measure the model's memory
instead of the rewrite's fidelity.

WHY THE DIFF IS CODE AND NOT A FOURTH MODEL CALL
=================================================
``fidelity_score`` goes into the published run record and drives regeneration.
A model deciding "close enough" would make the score irreproducible, and would
put the thing being audited inside the auditor. See
:mod:`abca.fidelity.elements`.

THE FALLBACK IS A FEATURE
=========================
Three failed attempts emit the statute's own words with ``unresolved=True``.
The contract calls this outcome acceptable and requires it to be reachable, and
the code is written to reach it rather than to avoid it: a false FAIL costs
readability, while a false PASS ships a rewrite that changed the law while
claiming to preserve it. Those costs are not comparable, so every threshold in
the gate is set strict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.fidelity.elements import (
    MATCH_THRESHOLD,
    ElementDiff,
    ElementKind,
    LegalElement,
    diff_elements,
    extract_anchors,
    similarity,
)
from abca.fidelity.linters import GlossaryReport, ScopeReport, glossary_lint, scope_lint
from abca.fidelity.readability import ReadabilityScore, flesch_kincaid
from abca.pipeline.base import StageOutcome
from abca.pipeline.models import (
    ExtractedElement,
    FidelityBacktranslateOutput,
    FidelityExtractOutput,
    FidelityRenderOutput,
    GlossaryFootnote,
)
from abca.prompts import load_prompt
from abca.providers.base import GenerationRequest, Provider, ProviderError
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.schema.core import FidelityReport
from abca.schema.enums import StageName

#: Render attempts before the gate gives up and emits verbatim text.
#: Contract s5: "Three consecutive failures -> emit the verbatim text with a
#: note that faithful simplification was not achieved."
MAX_RENDER_ATTEMPTS = 3

#: Characters of provision text sent to passes A and B.
#:
#: A provision longer than this is a signal the caller passed a whole Act rather
#: than a section. Truncating is reported loudly rather than silently, because a
#: fidelity score computed over the first half of a statute would be a
#: confident number about the wrong thing.
MAX_PROVISION_CHARS = 12_000

#: Kinds that carry an obligation. A swap WITHIN this family is a modal error
#: even when the detected :class:`~abca.fidelity.elements.Modal` happens to
#: agree -- "must file" becoming "can file" changes the law whether the
#: detector reads it as a kind change, a modal change, or both.
_OBLIGATION_KINDS: frozenset[ElementKind] = frozenset(
    {ElementKind.REQUIREMENT, ElementKind.PROHIBITION, ElementKind.PERMISSION}
)


class FidelityConfigError(RuntimeError):
    """The gate was asked to run in a configuration that cannot test anything.

    Raised rather than warned. A fidelity gate whose independence is broken
    does not degrade gracefully -- it reports success on everything, which is
    strictly worse than not running at all, because the run record would then
    carry a fidelity score that means nothing.
    """


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


def _render_element_list(elements: list[LegalElement]) -> str:
    """Number the elements so the renderer can be told which one it lost."""
    return "\n".join(
        f"[{index}] {element.kind.value}: {element.text}"
        for index, element in enumerate(elements, start=1)
    )


def build_extract_prompt(prompt_text: str, source_text: str, *, title: str = "") -> str:
    """Compose pass A: take the provision apart."""
    heading = f"## The provision{f' -- {title}' if title else ''}\n\n"
    return f"{prompt_text}\n\n---\n\n{heading}```\n{source_text}\n```\n"


def build_render_prompt(
    prompt_text: str,
    source_text: str,
    elements: list[LegalElement],
    *,
    title: str = "",
    previous_failures: list[str] | None = None,
) -> str:
    """Compose pass B: write the plain-language version.

    On a regeneration the specific diff failures are named. That is a
    deliberate choice with a real cost worth stating: naming what was lost
    invites a renderer to write toward the checker rather than toward the
    reader. It is acceptable here only because the checker is not a keyword
    match -- passing means an INDEPENDENT model, reading only the rewrite,
    reconstructs that element with the same kind, the same modal force and the
    same numbers. The cheapest way to satisfy that is to actually say the
    thing.

    A blind regeneration was the alternative, and it is worse: the same model
    at temperature 0.0 given the same prompt produces the same rewrite, so a
    retry that adds no information is not a retry at all.
    """
    heading = f"## The provision{f' -- {title}' if title else ''}"
    parts = [
        prompt_text,
        "---",
        f"{heading}\n\n```\n{source_text}\n```",
        "## The operative elements (every one must survive into your rewrite)\n\n"
        + _render_element_list(elements),
    ]
    if previous_failures:
        parts.append(
            "## Your previous attempt did not survive the check\n\n"
            "An independent reader was given ONLY your rewrite -- not the provision "
            "-- and asked to reconstruct the rule from it. This is what went wrong:\n\n"
            + "\n".join(f"- {failure}" for failure in previous_failures)
            + "\n\nRewrite it so a reader with no access to the provision would "
            "recover each of those. Keep the readability you had; do not delete "
            "anything that was working."
        )
    return "\n\n".join(parts)


def build_backtranslate_prompt(prompt_text: str, rendering: str) -> str:
    """Compose pass C: reconstruct the rule from the rewrite alone.

    THIS FUNCTION TAKES NO SOURCE TEXT, AND THAT IS THE ENFORCEMENT.

    The isolation pass C depends on is not an instruction in the prompt file --
    an instruction can be diluted by a later edit, and nothing would fail. It is
    the signature: there is no parameter through which the provision could reach
    this prompt, so no future change to this module can leak it by accident.
    """
    return (
        f"{prompt_text}\n\n"
        "---\n\n"
        "## The text to reconstruct\n\n"
        f"```\n{rendering}\n```\n"
    )


# --------------------------------------------------------------------------
# Element handling
# --------------------------------------------------------------------------


def to_legal_elements(reported: list[ExtractedElement]) -> list[LegalElement]:
    """Turn a model's reported elements into diffable ones.

    :meth:`LegalElement.build` derives the modal and the numeric anchors FROM
    THE TEXT. The model supplies neither, so it cannot declare a modal it did
    not preserve.
    """
    return [LegalElement.build(item.kind, item.text) for item in reported]


def ground_elements(
    elements: list[LegalElement], source_text: str
) -> tuple[list[LegalElement], list[str]]:
    """Drop pass-A elements carrying numbers the provision does not contain.

    An extractor that invents a figure poisons everything downstream: the diff
    would then demand that the rewrite reproduce a number the law never stated,
    every attempt would fail on it, and the gate would blame the renderer for an
    extraction error and fall back to verbatim text.

    Only ANCHORS are checked, and only for presence. Anchors are exact, checkable
    and the highest-consequence thing pass A can get wrong. Prose is left alone:
    an element is a trimmed piece of the provision, so any stricter textual test
    would reject correct extractions for having been trimmed.

    Returns the surviving elements and one note per discard.
    """
    available = set(extract_anchors(source_text))
    kept: list[LegalElement] = []
    notes: list[str] = []
    for element in elements:
        invented = [anchor for anchor in element.anchors if anchor not in available]
        if invented:
            notes.append(
                f"pass A extraction discarded: {element.summary()} -- cites "
                f"{', '.join(invented)}, which does not appear in the provision. "
                "This is an extraction error, not a rewrite error, and it is "
                "removed so the gate does not charge the rewrite for it."
            )
            continue
        kept.append(element)
    return kept, notes


def modal_flips(
    diff: ElementDiff, reconstructed: list[LegalElement]
) -> list[tuple[LegalElement, LegalElement]]:
    """Dropped elements whose closest reconstruction changed the obligation.

    Contract s5 singles out modal-strength changes, so they are identified
    structurally rather than by reading the diff's human-readable reasons. A
    string check would break the moment someone reworded a message.

    "Closest" means the highest token similarity above
    :data:`~abca.fidelity.elements.MATCH_THRESHOLD` -- the reconstruction is
    talking about the same subject matter, and got the force of it wrong. That
    is a materially different failure from simply not mentioning it, and a
    reader deserves to see which one happened.
    """
    flips: list[tuple[LegalElement, LegalElement]] = []
    for element, _reason in diff.dropped:
        best: LegalElement | None = None
        best_score = MATCH_THRESHOLD
        for candidate in reconstructed:
            score = similarity(element, candidate)
            if score >= best_score:
                best, best_score = candidate, score
        if best is None:
            continue
        modal_changed = best.modal is not element.modal
        family_changed = (
            element.kind in _OBLIGATION_KINDS
            and best.kind in _OBLIGATION_KINDS
            and best.kind is not element.kind
        )
        if modal_changed or family_changed:
            flips.append((element, best))
    return flips


def describe_flip(source: LegalElement, candidate: LegalElement) -> str:
    """One line naming exactly how the force of an obligation changed."""
    parts: list[str] = []
    if source.modal is not candidate.modal:
        parts.append(f"{source.modal.value} -> {candidate.modal.value}")
    if source.kind is not candidate.kind:
        parts.append(f"{source.kind.value} -> {candidate.kind.value}")
    return f"MODAL CHANGE ({'; '.join(parts)}): {source.text[:120]}"


# --------------------------------------------------------------------------
# Result objects
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RenderAttempt:
    """One pass B/C round: what was written, and what came back."""

    attempt: int
    rendering: str
    footnotes: list[GlossaryFootnote] = field(default_factory=list)
    reconstructed: list[LegalElement] = field(default_factory=list)
    diff: ElementDiff | None = None
    #: Modal-strength changes found in this attempt, already described.
    flips: list[str] = field(default_factory=list)
    #: Reasons this attempt failed. Empty on a pass.
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.diff is not None and self.diff.passed and not self.flips


@dataclass(slots=True)
class PlainLanguageResult:
    """Everything the gate produced, including the evidence for its own verdict.

    ``text`` is what a reader should be shown. On a clean pass it is the
    plain-language rendering; after three failures it is the provision's own
    words, because emitting a rewrite the gate could not verify would be
    publishing the exact failure the gate exists to catch.
    """

    #: The provision as given (possibly truncated -- see ``truncated``).
    source_text: str
    #: What to show the reader: the rendering, or the source verbatim.
    text: str
    #: True when the gate fell back to verbatim source text.
    verbatim_fallback: bool = False
    #: True when the provision exceeded MAX_PROVISION_CHARS and was cut.
    truncated: bool = False

    title: str = ""
    locator: str | None = None

    elements: list[LegalElement] = field(default_factory=list)
    footnotes: list[GlossaryFootnote] = field(default_factory=list)
    attempts: list[RenderAttempt] = field(default_factory=list)

    readability: ReadabilityScore | None = None
    scope: ScopeReport | None = None
    glossary: GlossaryReport | None = None

    @property
    def best_attempt(self) -> RenderAttempt | None:
        """The attempt that passed, or the last one tried."""
        for attempt in self.attempts:
            if attempt.passed:
                return attempt
        return self.attempts[-1] if self.attempts else None

    @property
    def diff(self) -> ElementDiff | None:
        best = self.best_attempt
        return best.diff if best else None

    def to_report(self) -> FidelityReport:
        """The record-ready :class:`~abca.schema.core.FidelityReport`.

        ``regenerations`` counts REDOS -- attempts after the first -- so a
        rewrite that passed immediately reports 0 and one that exhausted three
        attempts reports 2. The number answers "how much did this have to be
        redone", which is what a reader of the record is asking.

        On the verbatim fallback the score is the LAST attempt's, not zero. The
        rewrite did preserve most of the provision; it just could not be shown
        to preserve all of it, and reporting 0.0 would overstate the failure as
        badly as reporting 1.0 would hide it.
        """
        diff = self.diff
        best = self.best_attempt
        return FidelityReport(
            applied=True,
            score=diff.score if diff else 0.0,
            reading_grade=self.readability.grade if self.readability else 0.0,
            elements_source=len(self.elements),
            elements_preserved=diff.preserved_count if diff else 0,
            modal_verbs_preserved=bool(best and not best.flips),
            regenerations=max(0, len(self.attempts) - 1),
            unresolved=self.verbatim_fallback,
        )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def assert_independent(renderer: Provider, backtranslator: Provider) -> None:
    """Refuse to run when pass C would grade the model that wrote pass B.

    Compared by resolved WEIGHTS HASH first, because two config entries can
    point at one model under different tags and a name comparison would miss
    it. A backend that cannot report an identity is treated as a failure, not
    waved through: an unverifiable independence claim is not an independence
    claim, and this gate's whole value rests on it.
    """
    try:
        left = renderer.identity()
        right = backtranslator.identity()
    except Exception as exc:
        raise FidelityConfigError(
            "could not resolve model identities to confirm the fidelity gate's "
            f"independence ({type(exc).__name__}: {exc}). The gate is refused rather "
            "than run unverified: pass C grading the model that wrote pass B "
            "reproduces its misreadings and reports success on everything."
        ) from exc

    if left.weights_hash and right.weights_hash:
        same = left.weights_hash == right.weights_hash
    else:
        same = (left.provider, left.name) == (right.provider, right.name)

    if same:
        raise FidelityConfigError(
            f"the back-translation model is the same model as the renderer "
            f"({right.name!r}). Contract s5 requires a separate model instance: a "
            "model grading its own simplification reproduces its own misreadings, "
            "so the gate would pass everything and prove nothing. Configure a "
            "different [models.backtranslate] in your config.toml."
        )


def run_fidelity(
    renderer: Provider,
    backtranslator: Provider,
    *,
    source_text: str,
    title: str = "",
    locator: str | None = None,
    seed: int = 42,
    temperature: float = 0.0,
    max_attempts: int = 3,
    context_length: int | None = None,
    max_render_attempts: int = MAX_RENDER_ATTEMPTS,
) -> StageOutcome[PlainLanguageResult]:
    """Run the three-pass gate over one legal provision.

    ``renderer`` runs passes A and B; ``backtranslator`` runs pass C and must be
    a different model (:func:`assert_independent`).

    The stage never raises on a model failure. A provider error in pass B or C
    is recorded as a failed attempt and the loop continues, because the honest
    end state of "we could not verify a simplification" already exists -- the
    verbatim fallback -- and reaching it is better than propagating an exception
    that would lose the elements pass A already extracted.
    """
    assert_independent(renderer, backtranslator)

    provision = source_text.strip()
    truncated = len(provision) > MAX_PROVISION_CHARS
    if truncated:
        provision = provision[:MAX_PROVISION_CHARS]

    result = PlainLanguageResult(
        source_text=provision,
        text=provision,
        verbatim_fallback=True,  # until a rendering passes
        truncated=truncated,
        title=title,
        locator=locator,
    )
    outcome: StageOutcome[PlainLanguageResult] = StageOutcome(value=result)

    if truncated:
        outcome.note(
            f"provision exceeded {MAX_PROVISION_CHARS} characters and was truncated. "
            "The elements below cover only the text shown, so the fidelity score "
            "describes part of the provision, not all of it. Pass a single section."
        )
    if not provision:
        outcome.note("no provision text was supplied; the fidelity gate did not run")
        return outcome

    extract_prompt = load_prompt(StageName.FIDELITY, variant="extract")
    render_prompt = load_prompt(StageName.FIDELITY, variant="render")
    backtranslate_prompt = load_prompt(StageName.FIDELITY, variant="backtranslate")

    # -- pass A ------------------------------------------------------------
    try:
        extracted = generate_structured(
            renderer,
            GenerationRequest(
                prompt=build_extract_prompt(extract_prompt.text, provision, title=title),
                seed=seed,
                temperature=temperature,
                context_length=context_length,
            ),
            FidelityExtractOutput,
            max_attempts=max_attempts,
        )
    except (StructuredOutputError, ProviderError) as exc:
        # Without elements there is nothing to check a rewrite against, so a
        # rewrite would be unverifiable by construction. Emit the statute.
        outcome.note(
            f"pass A (extract) failed ({type(exc).__name__}: {exc}). With no "
            "operative elements there is nothing to check a rewrite against, so "
            "the provision is emitted verbatim."
        )
        return outcome

    outcome.absorb(extracted)
    elements, grounding_notes = ground_elements(
        to_legal_elements(extracted.value.elements), provision
    )
    for note in grounding_notes:
        outcome.note(note)
    result.elements = elements

    if not elements:
        outcome.note(
            "pass A extracted no operative elements from the provision. A rewrite "
            "cannot be verified against an empty element set, so the provision is "
            "emitted verbatim rather than simplified on trust."
        )
        return outcome

    outcome.note(
        f"pass A extracted {len(elements)} operative element(s): "
        + ", ".join(sorted({element.kind.value for element in elements}))
    )

    # -- passes B and C, with regeneration ---------------------------------
    previous_failures: list[str] = []

    for attempt_number in range(1, max_render_attempts + 1):
        attempt = RenderAttempt(attempt=attempt_number, rendering="")
        result.attempts.append(attempt)

        try:
            rendered = generate_structured(
                renderer,
                GenerationRequest(
                    prompt=build_render_prompt(
                        render_prompt.text,
                        provision,
                        elements,
                        title=title,
                        previous_failures=previous_failures,
                    ),
                    seed=seed,
                    temperature=temperature,
                    context_length=context_length,
                ),
                FidelityRenderOutput,
                max_attempts=max_attempts,
            )
        except (StructuredOutputError, ProviderError) as exc:
            attempt.failures = [f"pass B (render) failed: {type(exc).__name__}: {exc}"]
            previous_failures = list(attempt.failures)
            outcome.note(attempt.failures[0])
            continue

        outcome.absorb(rendered)
        attempt.rendering = rendered.value.rendering.strip()
        attempt.footnotes = list(rendered.value.footnotes)

        try:
            back = generate_structured(
                backtranslator,
                GenerationRequest(
                    # Only the rendering crosses this line. The provision is not
                    # in scope for this call and there is no parameter for it.
                    prompt=build_backtranslate_prompt(
                        backtranslate_prompt.text, attempt.rendering
                    ),
                    seed=seed,
                    temperature=temperature,
                    context_length=context_length,
                ),
                FidelityBacktranslateOutput,
                max_attempts=max_attempts,
            )
        except (StructuredOutputError, ProviderError) as exc:
            # A failed pass C is NOT a pass. An unchecked rewrite is exactly
            # what the gate exists to prevent shipping.
            attempt.failures = [
                (
                    f"pass C (back-translate) failed: {type(exc).__name__}: {exc}. "
                    "The rewrite was not checked, so it is treated as unverified."
                )
            ]
            previous_failures = list(attempt.failures)
            outcome.note(attempt.failures[0])
            continue

        outcome.absorb(back)
        attempt.reconstructed = to_legal_elements(back.value.elements)
        attempt.diff = diff_elements(elements, attempt.reconstructed)
        attempt.flips = [
            describe_flip(source, candidate)
            for source, candidate in modal_flips(attempt.diff, attempt.reconstructed)
        ]
        # Modal changes are listed FIRST: contract s5 names them specifically,
        # and a reader scanning failures should see the dangerous one at the top.
        attempt.failures = [*attempt.flips, *attempt.diff.failures()]

        if attempt.passed:
            result.text = attempt.rendering
            result.footnotes = attempt.footnotes
            result.verbatim_fallback = False
            outcome.note(
                f"fidelity gate PASSED on attempt {attempt_number}: all "
                f"{attempt.diff.source_count} element(s) survived the "
                "back-translation with modal force intact"
            )
            break

        previous_failures = list(attempt.failures)
        outcome.note(
            f"attempt {attempt_number} failed the fidelity gate "
             f"(score {attempt.diff.score:.2f}): "
            + "; ".join(attempt.failures[:4])
            + (" ..." if len(attempt.failures) > 4 else "")
        )

    if result.verbatim_fallback:
        outcome.note(
            f"FAITHFUL SIMPLIFICATION NOT ACHIEVED after {len(result.attempts)} "
            "attempt(s). The provision's own words are emitted instead. This is an "
            "acceptable outcome (contract s5): an unverified rewrite of a statute "
            "is worse than a hard-to-read statute."
        )

    # -- cheap linters, run every time -------------------------------------
    # These never fail the gate. They are advisory signals about the text a
    # reader will actually see, so they run on the fallback too -- where they
    # are trivially clean, which is itself informative.
    result.readability = flesch_kincaid(result.text)
    result.scope = scope_lint(provision, result.text)
    result.glossary = glossary_lint(provision, result.text)

    if not result.readability.in_target_band and not result.verbatim_fallback:
        outcome.note(
            f"readability {result.readability.describe()}. Flagged, not failed: "
            "forcing a grade band by cutting clauses is how a rewrite loses an "
            "exception, so fidelity outranks readability whenever they conflict."
        )
    for flag in result.scope.flags():
        outcome.note(f"scope linter: {flag}")
    for flag in result.glossary.flags():
        outcome.note(f"glossary linter: {flag}")

    outcome.dedupe_notes()
    return outcome


__all__ = [
    "MAX_PROVISION_CHARS",
    "MAX_RENDER_ATTEMPTS",
    "FidelityConfigError",
    "PlainLanguageResult",
    "RenderAttempt",
    "assert_independent",
    "build_backtranslate_prompt",
    "build_extract_prompt",
    "build_render_prompt",
    "describe_flip",
    "ground_elements",
    "modal_flips",
    "run_fidelity",
    "to_legal_elements",
]
