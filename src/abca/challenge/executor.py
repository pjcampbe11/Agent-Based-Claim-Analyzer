"""Challenge execution: running the retrieval plan, not just emitting it (doc 20 B4).

WHAT MAKES LANE B DIFFERENT
===========================
Lane A emits a retrieval plan and moves on under a latency budget. Lane B RUNS
it. Nobody is waiting, so the executor may work the plan exhaustively -- every
query, every candidate, and bounded widening when a query fails.

WIDENING IS BOUNDED, AND THE BOUND IS THE FEATURE
=================================================
Given unlimited freedom to broaden a search, an executor eventually finds
*something* for any claim whatsoever, and then the promotion gate is deciding
between real matches and things that turned up after forty attempts. So widening
runs on a fixed axis set with a hard cap, and every widening is recorded in the
published outcome.

The record matters as much as the cap. A reader seeing "promoted after 2 queries"
and a reader seeing "promoted after 3 widenings on the date window" are looking at
two different degrees of confidence in the same grade, and the outcome shows them
which one they have.

BUDGET
======
Batch is not free. A challenge that exhausts its token or wall-clock cap is
published as UNPROMOTABLE with the cap noted -- never left ambiguous, and never
silently extended. "We stopped looking because we hit the budget" is a different
statement from "we looked and found nothing", and the outcome distinguishes them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from abca.schema.challenge import RetrievedArtifact, Widening

#: The axes a failed query may be widened along, and how far. Fixed, because a
#: configurable axis set is an unbounded one with extra steps.
WIDENING_AXES: tuple[tuple[str, str], ...] = (
    ("congress", "the adjacent Congress (+/- 1)"),
    ("chamber", "the other chamber"),
    ("date_window", "the date window (+/- 90 days)"),
    ("bill_number", "adjacent bill numbers"),
)

#: Hard cap on widenings per challenge, across all axes.
MAX_WIDENINGS = 8

#: Hard cap on queries per challenge, including widened ones.
MAX_QUERIES = 40


@dataclass(slots=True)
class Budget:
    """Per-challenge limits. Exhaustion is an outcome, not an error."""

    max_tokens: int = 100_000
    max_wall_clock_ms: int = 120_000
    max_queries: int = MAX_QUERIES
    max_widenings: int = MAX_WIDENINGS

    tokens_spent: int = 0
    started_ms: float = field(default_factory=lambda: time.monotonic() * 1000)

    @property
    def elapsed_ms(self) -> int:
        return int(time.monotonic() * 1000 - self.started_ms)

    def exhausted(self, *, queries: int, widenings: int) -> str:
        """The reason the budget is spent, or an empty string."""
        if self.tokens_spent >= self.max_tokens:
            return f"token budget exhausted at {self.tokens_spent}/{self.max_tokens}"
        if self.elapsed_ms >= self.max_wall_clock_ms:
            return f"wall-clock budget exhausted at {self.elapsed_ms}ms/{self.max_wall_clock_ms}ms"
        if queries >= self.max_queries:
            return f"query cap reached at {queries}/{self.max_queries}"
        if widenings >= self.max_widenings:
            return f"widening cap reached at {widenings}/{self.max_widenings}"
        return ""


@dataclass(slots=True)
class ExecutionResult:
    """Everything the gate and the published record need from an execution."""

    artifact: RetrievedArtifact | None = None
    queries_executed: list[str] = field(default_factory=list)
    widenings: list[Widening] = field(default_factory=list)
    exhausted_reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def budget_exhausted(self) -> bool:
        return bool(self.exhausted_reason)

    @property
    def plan_exhausted(self) -> bool:
        """Every query ran and nothing was found, without hitting a cap."""
        return self.artifact is None and not self.budget_exhausted


def widen(query: str, axis: str) -> str:
    """Produce the widened form of a query along one axis.

    Deliberately simple string transforms rather than a query language. The
    executor's job is to try a slightly broader search, not to build a planner --
    and a transform a reader cannot predict from the axis name is a transform
    nobody can audit from the published record.
    """
    match axis:
        case "congress":
            return f"{query} OR previous Congress OR next Congress"
        case "chamber":
            return f"{query} (House OR Senate)"
        case "date_window":
            return f"{query} within 90 days"
        case "bill_number":
            return f"{query} adjacent bill numbers"
        case _:
            return query


def execute_plan(
    queries: Sequence[str],
    fetch: Callable[[str], RetrievedArtifact | None],
    *,
    budget: Budget | None = None,
    allow_widening: bool = True,
) -> ExecutionResult:
    """Run every query in priority order until an artifact is found or the plan ends.

    ``fetch`` takes a query string and returns an artifact or None. Injected so
    the executor is testable without a network and so the connector registry is
    not a hidden dependency of this module.

    Stops at the FIRST artifact. That is correct for a gate that then checks the
    artifact against a pre-registered branch: continuing to search after a hit
    would mean choosing among several candidates, and choosing is exactly the
    judgment the branch-outcome mechanism exists to take away from the executor.
    """
    budget = budget or Budget()
    result = ExecutionResult()

    for query in queries:
        reason = budget.exhausted(
            queries=len(result.queries_executed), widenings=len(result.widenings)
        )
        if reason:
            result.exhausted_reason = reason
            return result

        result.queries_executed.append(query)
        try:
            artifact = fetch(query)
        except Exception as error:
            result.notes.append(f"query {query!r} failed: {type(error).__name__}")
            artifact = None

        if artifact is not None:
            result.artifact = artifact
            return result

        if not allow_widening:
            continue

        for axis, _description in WIDENING_AXES:
            reason = budget.exhausted(
                queries=len(result.queries_executed), widenings=len(result.widenings)
            )
            if reason:
                result.exhausted_reason = reason
                return result

            widened = widen(query, axis)
            result.queries_executed.append(widened)
            try:
                artifact = fetch(widened)
            except Exception as error:
                result.notes.append(f"widened query failed: {type(error).__name__}")
                artifact = None

            result.widenings.append(
                Widening(axis=axis, from_value=query, to_value=widened,
                         query=widened, found=artifact is not None)
            )
            if artifact is not None:
                result.artifact = artifact
                result.notes.append(
                    f"found only after widening on {axis}; a reader should weigh "
                    "this promotion accordingly"
                )
                return result

    return result


__all__ = [
    "MAX_QUERIES",
    "MAX_WIDENINGS",
    "WIDENING_AXES",
    "Budget",
    "ExecutionResult",
    "execute_plan",
    "widen",
]
