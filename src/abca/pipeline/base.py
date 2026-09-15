"""Shared machinery for pipeline stages.

Every stage in this package follows the same shape:

* it takes already-validated input and a :class:`~abca.providers.base.Provider`;
* it batches its work so no single model call runs out of context;
* it returns its result together with the accounting the ledger needs --
  tokens spent, repair attempts, and operator-facing notes.

The batching and accounting live here so each stage is only its own logic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from abca.providers.structured import StructuredResult

T = TypeVar("T")

#: Characters of payload per model call. Conservative on purpose: a batch that
#: overflows the context window produces a truncated response, which costs a
#: full repair round-trip -- far more than the extra call that splitting would
#: have cost. Sized for an 8k-token context with room for the prompt, the
#: schema and the response.
DEFAULT_BATCH_CHARS = 6_000

#: Hard cap on items per call regardless of size. A batch of 400 one-word
#: sentences fits the character budget easily but produces a response with 400
#: entries, where a single mis-indexed item forces the whole batch to be
#: retried. Smaller batches localise that damage.
DEFAULT_BATCH_ITEMS = 40


@dataclass(slots=True)
class StageOutcome(Generic[T]):
    """A stage's result plus everything the ledger records about producing it."""

    value: T
    notes: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    #: Model calls made, including repair attempts.
    attempts: int = 0
    #: Calls that needed more than one attempt.
    repaired_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def absorb(self, result: StructuredResult[Any]) -> None:
        """Fold one structured call's accounting into this outcome.

        Failed attempts are counted, not just the successful one. A model that
        needs three tries costs three tries, and a budget that ignored that
        would understate the run.
        """
        self.prompt_tokens += sum(a.prompt_tokens for a in result.attempts)
        self.completion_tokens += sum(a.completion_tokens for a in result.attempts)
        self.duration_ms += result.total_duration_ms
        self.attempts += result.attempt_count
        if result.repaired:
            self.repaired_calls += 1
        self.notes.extend(result.stage_notes())

    def absorb_stage(self, other: StageOutcome[Any], *, prefix: str = "") -> None:
        """Fold ANOTHER STAGE's accounting into this one.

        Needed by consensus, which runs a whole adjudication stage per panel
        member and has to report the panel's total cost as one stage. Distinct
        from :meth:`absorb`, which folds in a single structured model call --
        passing a StageOutcome to that one raises, because a StageOutcome has an
        attempt COUNT where a StructuredResult has a list of attempts.

        ``prefix`` labels the other stage's notes, so a note from one panel
        member is attributable to that member rather than to the panel.
        """
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.duration_ms += other.duration_ms
        self.attempts += other.attempts
        self.repaired_calls += other.repaired_calls
        self.notes.extend(
            f"{prefix}{note}" if prefix else note for note in other.notes
        )

    def note(self, message: str) -> None:
        self.notes.append(message)

    def dedupe_notes(self) -> None:
        """Collapse repeated notes, keeping first-seen order.

        A twenty-batch run where every batch reports "output was not clean
        JSON" should say that once, not twenty times. The count of repaired
        calls carries the magnitude.
        """
        seen: set[str] = set()
        unique: list[str] = []
        for note in self.notes:
            if note not in seen:
                seen.add(note)
                unique.append(note)
        self.notes = unique


def batched(
    items: Iterable[T],
    *,
    size_of: Callable[[T], int],
    max_chars: int = DEFAULT_BATCH_CHARS,
    max_items: int = DEFAULT_BATCH_ITEMS,
) -> Iterator[list[T]]:
    """Group ``items`` into batches bounded by both character count and length.

    An item larger than ``max_chars`` on its own still gets its own batch
    rather than being dropped or truncated. Truncating it would silently
    analyze less than the author wrote; the oversized call may fail, but it
    fails visibly.
    """
    batch: list[T] = []
    used = 0

    for item in items:
        size = size_of(item)
        if batch and (used + size > max_chars or len(batch) >= max_items):
            yield batch
            batch, used = [], 0
        batch.append(item)
        used += size

    if batch:
        yield batch


def render_numbered(items: Iterable[tuple[int, str]]) -> str:
    """Render ``(index, text)`` pairs as a stable numbered block for a prompt.

    Indices are the model's handle for referring to an item, so they are shown
    explicitly rather than left implicit in list position -- a model that
    reorders its output would otherwise silently mis-assign every result.
    """
    return "\n".join(f"[{index}] {text}" for index, text in items)


__all__ = [
    "DEFAULT_BATCH_CHARS",
    "DEFAULT_BATCH_ITEMS",
    "StageOutcome",
    "batched",
    "render_numbered",
]
