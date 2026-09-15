"""The challenge queue: intake, deduplication, and durable state (doc 20 B1).

WHY A QUEUE AT ALL
==================
Lane B has different economics from Lane A. Nobody is waiting on it, so it can
run forensic profiles, consensus across models, and exhaustive retrieval on a
single post. That only works if the work is DEFERRED -- which means it has to be
written down somewhere durable, with enough context to run later, by a different
process, possibly days later.

DEDUPLICATION IS THE POINT, NOT AN OPTIMIZATION
===============================================
A narrative circulating on ten thousand accounts is ONE challenge, not ten
thousand. Without dedup the queue would be dominated by whichever claim went most
viral, the budget would be spent re-answering the same question, and the
published record would imply the tool investigated ten thousand things when it
investigated one.

Dedup is by CLUSTER, not by exact text, because the same claim circulates in
thousands of slightly different wordings. The cluster key is supplied by the
caller (the cluster stage already computed it); the queue's job is to honour it.

ON-DISK FORMAT
==============
One JSON file per challenge, named by id, in a flat directory -- the same shape
as the run ledger, for the same reasons: a flat directory of content-named files
is trivially inspectable, survives partial writes, and needs no index to stay
consistent. Writes are atomic (temp file, fsync, os.replace) so a crash mid-write
cannot leave a half-parsed challenge that silently disappears from a drain.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from abca.canonical import digest_text
from abca.ids import new_run_id
from abca.schema.challenge import ChallengeOutcome, PromotionGrade


class ChallengeState(StrEnum):
    """Where a challenge is in its life."""

    QUEUED = "queued"        # Intake done, nothing executed.
    RUNNING = "running"      # A drain claimed it.
    DORMANT = "dormant"      # Executed; waiting on an artifact that may appear.
    RESOLVED = "resolved"    # Terminal: promoted, unpromotable or rejected.


def new_challenge_id() -> str:
    """A sortable, unique challenge id. ``ch-`` + ULID.

    Prefixed so it can never be confused with a run id in a log line or a URL.
    They are the same shape and mean completely different things, and a reader
    who mixes them up would be looking at the wrong record entirely.
    """
    return f"ch-{new_run_id()}"


@dataclass(slots=True)
class Challenge:
    """One queued sourceless claim, with everything needed to run it later."""

    challenge_id: str
    denatured_claim: str
    denatured_claim_hash: str
    cluster_key: str
    state: ChallengeState = ChallengeState.QUEUED
    platform: str = ""
    posted_date: str = ""
    parent_context: str = ""
    #: How many distinct posts fell into this cluster. Published, because it is
    #: the honest measure of how far a narrative travelled -- and it is NOT a
    #: per-person count, which contract s8 forbids.
    occurrences: int = 1
    created_at: str = ""
    updated_at: str = ""
    recheck_after: str = ""
    recheck_trigger: str = ""
    attempts: int = 0
    outcome: dict | None = None

    def to_json(self) -> dict:
        return {
            "challenge_id": self.challenge_id,
            "denatured_claim": self.denatured_claim,
            "denatured_claim_hash": self.denatured_claim_hash,
            "cluster_key": self.cluster_key,
            "state": self.state.value,
            "platform": self.platform,
            "posted_date": self.posted_date,
            "parent_context": self.parent_context,
            "occurrences": self.occurrences,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "recheck_after": self.recheck_after,
            "recheck_trigger": self.recheck_trigger,
            "attempts": self.attempts,
            "outcome": self.outcome,
        }

    @classmethod
    def from_json(cls, payload: dict) -> Challenge:
        data = dict(payload)
        data["state"] = ChallengeState(data.get("state", "queued"))
        return cls(**data)

    @property
    def is_due(self) -> bool:
        """Whether a dormant challenge's re-check date has arrived."""
        if self.state is not ChallengeState.DORMANT or not self.recheck_after:
            return False
        return datetime.now(UTC).date() >= date.fromisoformat(self.recheck_after)


class ChallengeQueue:
    """A durable queue of challenges on disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- storage ---------------------------------------------------------

    def _path(self, challenge_id: str) -> Path:
        return self.root / f"{challenge_id}.json"

    def _write(self, challenge: Challenge) -> Path:
        """Atomic write: temp file, fsync, replace.

        Not a context manager, deliberately -- ``with open(...)`` closes the
        handle before the rename, and the fsync has to happen while it is open
        or a crash can leave the rename pointing at unflushed data.
        """
        challenge.updated_at = datetime.now(UTC).isoformat()
        payload = json.dumps(challenge.to_json(), indent=2, sort_keys=True) + "\n"
        target = self._path(challenge.challenge_id)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp"
        )
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, target)
        return target

    def get(self, challenge_id: str) -> Challenge | None:
        path = self._path(challenge_id)
        if not path.is_file():
            return None
        return Challenge.from_json(json.loads(path.read_text(encoding="utf-8")))

    def __iter__(self) -> Iterator[Challenge]:
        """Every challenge, oldest first. Ids are ULIDs, so name order is time order."""
        for path in sorted(self.root.glob("ch-*.json")):
            try:
                yield Challenge.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError):
                # A corrupt file is reported by `abca queue list`, never silently
                # skipped in a way that makes the queue look shorter than it is.
                continue

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def corrupt(self) -> list[Path]:
        """Files that failed to parse. Surfaced rather than silently dropped."""
        bad: list[Path] = []
        for path in sorted(self.root.glob("ch-*.json")):
            try:
                Challenge.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError):
                bad.append(path)
        return bad

    # ---- intake ----------------------------------------------------------

    def enqueue(
        self,
        denatured_claim: str,
        *,
        cluster_key: str | None = None,
        platform: str = "",
        posted_date: str = "",
        parent_context: str = "",
    ) -> tuple[Challenge, bool]:
        """Add a claim, or increment the existing challenge for its cluster.

        Returns ``(challenge, created)``. ``created`` is False when the claim
        joined an existing cluster, which is the common case for anything viral
        and is the whole reason the queue does not grow with reach.

        ``cluster_key`` defaults to the denatured claim's hash -- exact-text
        dedup. That is the weakest useful key and it is the honest default: the
        cluster stage computes a real one, and when the caller has it, it should
        pass it.
        """
        text = denatured_claim.strip()
        if not text:
            raise ValueError("cannot enqueue an empty claim")
        claim_hash = digest_text(text)
        key = cluster_key or claim_hash

        for existing in self:
            if existing.cluster_key == key and existing.state is not ChallengeState.RESOLVED:
                existing.occurrences += 1
                self._write(existing)
                return existing, False

        now = datetime.now(UTC).isoformat()
        challenge = Challenge(
            challenge_id=new_challenge_id(),
            denatured_claim=text,
            denatured_claim_hash=claim_hash,
            cluster_key=key,
            platform=platform,
            posted_date=posted_date,
            parent_context=parent_context,
            created_at=now,
            updated_at=now,
        )
        self._write(challenge)
        return challenge, True

    # ---- lifecycle -------------------------------------------------------

    def claim_next(self) -> Challenge | None:
        """Take the oldest runnable challenge and mark it RUNNING.

        Dormant challenges are only returned once their re-check date has
        arrived, so a drain does not spend budget re-asking a question whose
        answer cannot have changed yet.
        """
        for challenge in self:
            runnable = challenge.state is ChallengeState.QUEUED or challenge.is_due
            if runnable:
                challenge.state = ChallengeState.RUNNING
                challenge.attempts += 1
                self._write(challenge)
                return challenge
        return None

    def record_outcome(self, challenge: Challenge, outcome: ChallengeOutcome) -> Challenge:
        """Store a finished challenge's result and set its terminal state."""
        challenge.outcome = outcome.to_jsonable()
        if outcome.grade is PromotionGrade.DORMANT:
            challenge.state = ChallengeState.DORMANT
            challenge.recheck_after = (
                outcome.recheck_after.isoformat() if outcome.recheck_after else ""
            )
            challenge.recheck_trigger = outcome.recheck_trigger
        else:
            challenge.state = ChallengeState.RESOLVED
            challenge.recheck_after = ""
            challenge.recheck_trigger = ""
        self._write(challenge)
        return challenge

    def release(self, challenge: Challenge) -> Challenge:
        """Return a RUNNING challenge to the queue after a crash or interruption.

        Without this a killed drain would strand every claimed challenge in
        RUNNING forever, and the queue would quietly stop making progress while
        continuing to look healthy.
        """
        if challenge.state is ChallengeState.RUNNING:
            challenge.state = ChallengeState.QUEUED
            self._write(challenge)
        return challenge

    # ---- views -----------------------------------------------------------

    def by_state(self, state: ChallengeState) -> list[Challenge]:
        return [c for c in self if c.state is state]

    def due(self) -> list[Challenge]:
        return [c for c in self if c.is_due]

    def stats(self) -> dict[str, int]:
        counts = {s.value: 0 for s in ChallengeState}
        occurrences = 0
        for challenge in self:
            counts[challenge.state.value] += 1
            occurrences += challenge.occurrences
        counts["total"] = sum(counts[s.value] for s in ChallengeState)
        counts["posts_represented"] = occurrences
        counts["due_now"] = len(self.due())
        return counts


__all__ = ["Challenge", "ChallengeQueue", "ChallengeState", "new_challenge_id"]
