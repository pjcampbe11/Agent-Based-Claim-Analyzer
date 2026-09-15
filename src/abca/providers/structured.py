"""Structured-output enforcement: extract, validate, repair, retry.

WHAT THIS MODULE GUARANTEES
===========================
The pipeline never sees a model's prose. It sees a validated pydantic object
or an exception. There is no third outcome, and no code path where a partially
parsed or best-effort object reaches a stage.

That guarantee is what makes the contract gates in :mod:`abca.schema.core`
real. A validator rejecting ``SUPPORTED`` without a T0-T2 citation only
protects anything if invalid model output is *retried or raised*, never
coerced. This module is where that happens.

TWO ENFORCEMENT PATHS, ONE GUARANTEE
====================================
* **Constrained decoding** (Ollama ``format`` schema, llama.cpp GBNF). The
  backend makes invalid JSON impossible at sampling time. Cheap and near-
  certain, but it constrains *shape*, not *semantics* -- a grammar can force
  a ``verdict`` field to be one of eight strings; it cannot know that
  ``SUPPORTED`` requires a T0-T2 citation.
* **Validate-and-retry.** Parse, validate against the pydantic model, and on
  failure re-ask with the exact errors appended.

Both run. Constrained decoding is used when the backend supports it, and
validation runs regardless, because the semantic gates live in the pydantic
validators and no grammar can express them.

EXTRACTION IS NOT REPAIR
========================
:func:`extract_json` will look past prose and code fences to find a JSON
object. It will NOT mutate the bytes inside that object -- no trailing-comma
stripping, no quote fixing, no bracket balancing. That line is deliberate:

* **Extraction** answers "where does the object start and end?". The model
  produced valid JSON and wrapped it in chatter. Recovering it loses nothing.
* **Repair** would answer "what did the model probably mean?". That is a
  guess, it can silently change a verdict, and it hides a model that is not
  following instructions.

A model needing extraction fallbacks on every call is a finding, so which
strategy succeeded is recorded on every attempt and surfaced in the stage
notes. Silent leniency would erase exactly the signal an operator needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from abca.providers.base import (
    Completion,
    GenerationFailed,
    GenerationRequest,
    Provider,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Default attempt budget. Three is a deliberate compromise: one retry is not
#: enough for a model that emitted a near-miss, and more than three on a 27B
#: turns a slow stage into an unusable one. Runs that regularly need all three
#: should change the prompt or the model, not raise this number -- and the
#: attempt counts recorded in the ledger are how that becomes visible.
DEFAULT_MAX_ATTEMPTS: int = 3


class ExtractionStrategy(StrEnum):
    """How a JSON object was recovered from raw model output.

    Ordered from cleanest to most forgiving. Recorded per attempt so that a
    model degrading from DIRECT to BRACE_SCAN over a long run is observable
    rather than invisible.
    """

    DIRECT = "direct"          # The whole response parsed as JSON. Ideal.
    FENCED = "fenced"          # Wrapped in a ```json code fence.
    BRACE_SCAN = "brace_scan"  # Embedded in prose; located by bracket matching.


class FailureKind(StrEnum):
    """Why an attempt failed. Drives which repair message is sent back."""

    NO_JSON = "no_json"              # Nothing object-shaped in the output.
    MALFORMED_JSON = "malformed"     # Object-shaped but not parseable.
    SCHEMA_INVALID = "schema"        # Parsed, but violated the pydantic model.
    TRUNCATED = "truncated"          # Generation hit the token ceiling.


class StructuredOutputError(GenerationFailed):
    """Every attempt failed. Carries the full history for the ledger.

    Raised rather than returning a partial result, because a stage that
    proceeds on "mostly valid" output defeats the point of the schema gates.
    """

    def __init__(self, attempts: list[StructuredAttempt], *, provider: str) -> None:
        self.attempts = attempts
        summary = "; ".join(
            f"attempt {a.number}: {a.failure.value if a.failure else 'ok'}"
            + (f" ({a.error_summary})" if a.error_summary else "")
            for a in attempts
        )
        super().__init__(
            f"structured output failed after {len(attempts)} attempt(s): {summary}",
            provider=provider,
        )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str | None:
    """Return the contents of the first fenced block, if there is one.

    Handles ```json, ```JSON, and a bare ``` fence. Returns ``None`` when no
    fence is present so the caller can distinguish "no fence" from "empty
    fence".
    """
    marker = text.find("```")
    if marker == -1:
        return None

    after = text[marker + 3 :]
    # Skip an optional language tag on the same line as the opening fence.
    newline = after.find("\n")
    if newline == -1:
        return None
    tag = after[:newline].strip().lower()
    if tag and tag not in {"json", "json5", "javascript", "js"}:
        # A fence for some other language -- not our payload.
        return None

    body = after[newline + 1 :]
    closing = body.find("```")
    return body[:closing] if closing != -1 else body


def _scan_balanced_object(text: str) -> str | None:
    """Find the first complete top-level ``{...}`` object in ``text``.

    Tracks string state so that braces inside string literals -- which appear
    constantly in this application, since claim text and legal quotes are full
    of them -- do not terminate the scan early. Backslash escapes are honored
    for the same reason.

    Returns the substring VERBATIM. Nothing inside it is altered; see the
    module docstring on extraction versus repair.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    # Unbalanced: almost always a truncated generation. Returning None lets the
    # caller report TRUNCATED, which produces a far more useful repair message
    # than a parse error would.
    return None


def extract_json(text: str) -> tuple[Any, ExtractionStrategy]:
    """Recover a JSON value from raw model output.

    Tries three strategies in order of decreasing cleanliness and returns which
    one worked, so degradation is measurable.

    Raises :class:`ValueError` when nothing object-shaped can be found; the
    caller converts that into a :class:`FailureKind`.
    """
    stripped = text.strip()

    # 1. The model did what it was told.
    try:
        return json.loads(stripped), ExtractionStrategy.DIRECT
    except json.JSONDecodeError:
        pass

    # 2. Wrapped in a code fence -- extremely common with chat-tuned models.
    fenced = _strip_code_fence(stripped)
    if fenced is not None:
        try:
            return json.loads(fenced.strip()), ExtractionStrategy.FENCED
        except json.JSONDecodeError:
            pass

    # 3. Buried in prose ("Here is the analysis: {...} Let me know if...").
    scanned = _scan_balanced_object(stripped)
    if scanned is not None:
        try:
            return json.loads(scanned), ExtractionStrategy.BRACE_SCAN
        except json.JSONDecodeError as exc:
            raise ValueError(f"located an object but it did not parse: {exc}") from exc

    raise ValueError("no JSON object found in model output")


# --------------------------------------------------------------------------
# Attempt accounting
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructuredAttempt:
    """Record of one generation attempt.

    Frozen and complete enough to reconstruct what happened without the raw
    text, which may be large and may contain the analyzed content.
    """

    number: int
    constrained: bool
    strategy: ExtractionStrategy | None = None
    failure: FailureKind | None = None
    error_summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.failure is None


@dataclass(slots=True)
class StructuredResult(Generic[ModelT]):
    """A validated object plus the history of how it was obtained.

    ``Generic[ModelT]`` rather than PEP 695 ``class StructuredResult[T]``
    syntax: the project targets Python 3.11, where PEP 695 is not available.
    """

    value: ModelT
    attempts: list[StructuredAttempt] = field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def repaired(self) -> bool:
        """Whether more than one attempt was needed."""
        return self.attempt_count > 1

    @property
    def total_tokens(self) -> int:
        """Tokens across ALL attempts, including failed ones.

        Failed attempts cost real tokens and real seconds. Budget accounting
        that counted only the successful attempt would understate the cost of a
        model that needs three tries, which is precisely the model an operator
        needs to know about.
        """
        return sum(a.prompt_tokens + a.completion_tokens for a in self.attempts)

    @property
    def total_duration_ms(self) -> int:
        return sum(a.duration_ms for a in self.attempts)

    def stage_notes(self) -> list[str]:
        """Operator-facing notes for the run ledger.

        Returns an empty list on a clean first-attempt success -- the common
        case should add no noise. Anything else is worth recording permanently,
        because "this verdict took three tries to come out well-formed" is
        context a reader of the published run deserves.
        """
        notes: list[str] = []
        if self.repaired:
            failures = [a for a in self.attempts if a.failure]
            notes.append(
                f"structured output required {self.attempt_count} attempts "
                f"({', '.join(f.failure.value for f in failures)})"
            )
        final = self.attempts[-1] if self.attempts else None
        if final and final.strategy and final.strategy is not ExtractionStrategy.DIRECT:
            notes.append(
                f"model output was not clean JSON; recovered via {final.strategy.value}"
            )
        if final and not final.constrained:
            notes.append(
                "backend did not support constrained decoding; output was validated "
                "after generation rather than enforced during it"
            )
        return notes


# --------------------------------------------------------------------------
# Repair prompts
# --------------------------------------------------------------------------


def _format_validation_errors(exc: ValidationError, *, limit: int = 12) -> str:
    """Render pydantic errors as a compact, model-readable list.

    Field paths are rendered as ``claims[0].verdict`` rather than pydantic's
    tuple form, because models correct dotted paths far more reliably than
    they correct tuples. Truncated at ``limit`` so a cascade of errors does not
    blow out the repair prompt's context -- fixing the first dozen almost
    always fixes the rest.
    """
    lines: list[str] = []
    for error in exc.errors()[:limit]:
        location = ""
        for part in error["loc"]:
            if isinstance(part, int):
                location += f"[{part}]"
            else:
                location = f"{location}.{part}" if location else str(part)
        lines.append(f"- {location or '<root>'}: {error['msg']}")

    remaining = len(exc.errors()) - limit
    if remaining > 0:
        lines.append(f"- ... and {remaining} more error(s)")
    return "\n".join(lines)


def build_repair_prompt(
    original: str,
    failure: FailureKind,
    detail: str,
    *,
    schema: dict[str, Any] | None = None,
) -> str:
    """Compose the follow-up prompt for a failed attempt.

    The repair message is tailored per failure kind. This matters more than it
    looks: telling a model that produced 4,000 truncated tokens to "fix the
    validation errors" is useless, because it did not produce validation
    errors -- it ran out of room. Naming the actual problem is what makes the
    second attempt land.

    The original request is restated rather than relying on conversational
    memory, because providers here are stateless by design: a stateful chat
    session would make the same prompt produce different output depending on
    history, which would break reproducibility.
    """
    if failure is FailureKind.TRUNCATED:
        instruction = (
            "Your previous response was cut off before it finished. Produce the "
            "same analysis again, but more concisely -- shorten free-text fields "
            "such as reasoning to the essential point. Do not drop any required "
            "field, and do not drop any citation."
        )
    elif failure in {FailureKind.NO_JSON, FailureKind.MALFORMED_JSON}:
        instruction = (
            "Your previous response could not be parsed as JSON.\n"
            f"Problem: {detail}\n\n"
            "Respond with a single JSON object and nothing else. No prose before "
            "or after it, no markdown code fence, no explanation."
        )
    else:  # SCHEMA_INVALID
        instruction = (
            "Your previous response was valid JSON but did not satisfy the "
            "required schema.\n"
            f"Errors:\n{detail}\n\n"
            "Correct exactly these problems and return the complete corrected "
            "JSON object. Do not include prose or a code fence. Do not change "
            "fields that were not listed as errors."
        )

    parts = [original, "", "---", instruction]
    if schema is not None and failure is FailureKind.SCHEMA_INVALID:
        # Restate the schema only for semantic failures. For a parse failure
        # the model already knows the shape; re-sending a large schema wastes
        # the context that the retry needs.
        parts += ["", "Required schema:", json.dumps(schema, indent=2, sort_keys=True)]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def generate_structured(
    provider: Provider,
    request: GenerationRequest,
    model_cls: type[ModelT],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> StructuredResult[ModelT]:
    """Generate until the output validates against ``model_cls``, or give up.

    ``request.json_schema`` is populated from ``model_cls`` when the caller has
    not set it, so constrained decoding and validation are always driven by the
    same schema. Letting them diverge would produce the worst possible failure
    mode: output the grammar accepts but the validator rejects, on every
    attempt, forever.

    The seed is held CONSTANT across attempts. It is tempting to vary it to
    "get a different answer", and that would be wrong twice over: it would
    make the run irreproducible, and it would paper over a prompt or model
    problem by rerolling until something passed. A retry here re-asks with the
    errors named; it does not gamble.
    """
    schema = request.json_schema or model_cls.model_json_schema()
    current = (
        request
        if request.json_schema is not None
        else GenerationRequest(
            prompt=request.prompt,
            system=request.system,
            json_schema=schema,
            seed=request.seed,
            temperature=request.temperature,
            context_length=request.context_length,
            max_tokens=request.max_tokens,
            stop=request.stop,
        )
    )

    attempts: list[StructuredAttempt] = []
    provider_name = getattr(provider, "name", "unknown")

    for number in range(1, max_attempts + 1):
        completion: Completion = provider.generate(current)

        failure, detail, strategy, value = _evaluate(completion, model_cls)

        attempts.append(
            StructuredAttempt(
                number=number,
                constrained=completion.constrained,
                strategy=strategy,
                failure=failure,
                error_summary=detail[:200],
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                duration_ms=completion.duration_ms,
            )
        )

        if failure is None:
            assert value is not None  # narrowed by failure is None
            return StructuredResult(value=value, attempts=attempts)

        if number == max_attempts:
            break

        current = current.with_prompt(
            build_repair_prompt(request.prompt, failure, detail, schema=schema)
        )

    raise StructuredOutputError(attempts, provider=provider_name)


def _evaluate(
    completion: Completion,
    model_cls: type[ModelT],
) -> tuple[FailureKind | None, str, ExtractionStrategy | None, ModelT | None]:
    """Classify one completion: extract, validate, and name the failure.

    Split out from the loop so the classification is testable directly, with
    no provider involved.
    """
    # Truncation is checked FIRST. A cut-off response usually also fails to
    # parse, but reporting "malformed JSON" for it sends the model chasing a
    # syntax problem it does not have.
    if completion.truncated:
        return (
            FailureKind.TRUNCATED,
            "generation stopped at the token limit",
            None,
            None,
        )

    try:
        payload, strategy = extract_json(completion.text)
    except ValueError as exc:
        kind = (
            FailureKind.MALFORMED_JSON
            if "did not parse" in str(exc)
            else FailureKind.NO_JSON
        )
        return kind, str(exc), None, None

    try:
        return None, "", strategy, model_cls.model_validate(payload)
    except ValidationError as exc:
        return FailureKind.SCHEMA_INVALID, _format_validation_errors(exc), strategy, None


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "ExtractionStrategy",
    "FailureKind",
    "StructuredAttempt",
    "StructuredOutputError",
    "StructuredResult",
    "build_repair_prompt",
    "extract_json",
    "generate_structured",
]
