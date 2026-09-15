"""Replaying a recorded run and classifying the result.

THE DECISION TREE
=================
Verification compares an original :class:`RunRecord` against a replay and
returns exactly one of five outcomes. The order of the checks is the whole
design, because several conditions can be true at once and reporting the
wrong one misleads the reader.

::

    0. Is the original record internally intact?
         no  -> LedgerTampered (raised, not an outcome; the file was edited)

    1. Was the recipe reconstructible at all?
         no  -> UNREPLAYABLE   (hosted API: weights cannot be pinned)

    2. Do the input digests match?
         no  -> DIVERGENT      (the replay asked a DIFFERENT question;
                                reported with a recipe diff, since this is
                                almost always operator error)

    3. Do the output digests match?
         yes -> IDENTICAL

    4. Do the semantic digests match?
         yes -> EQUIVALENT     (same verdicts, same citations, different prose)

    5. Did any cited source change since it was retrieved?
         yes -> DRIFTED        (checked BEFORE DIVERGENT: a changed statute
                                explains a changed verdict, and calling that
                                a bug would train people to ignore the alarm
                                that actually means "bug")

    6. otherwise -> DIVERGENT  (identical recipe, different conclusions --
                                non-determinism leaked in, or the config is
                                misreported. This is a real defect.)

WHY DRIFTED IS NOT A SUCCESS
============================
:attr:`VerifyOutcome.is_success` excludes DRIFTED deliberately. Nothing was
done wrong when a source changes under a verdict, but the verdict now needs
a human to look at it. Treating drift as success would let a published
position quietly rest on a repealed statute.

WHY DIVERGENT IS A BUG
======================
A run with the same input digest was asked the same question with the same
seed, the same temperature, and the same pinned weights. If the answers
differ, either something non-deterministic leaked into the pipeline
(unsorted iteration, wall-clock in a prompt, a concurrent retrieval race) or
the recorded config does not describe what actually ran. Both are defects in
the tool, not in the run, and the report says so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from abca.schema.core import AnalysisResult
from abca.schema.enums import VerifyOutcome
from abca.schema.ledger import RunRecord


class LedgerTampered(Exception):
    """Raised when the ORIGINAL record fails its own integrity audit.

    This is not a verification outcome. An outcome describes the relationship
    between two runs; this says the thing being compared against is not the
    record it claims to be, so no comparison is meaningful.
    """

    def __init__(self, run_id: str, problems: list[str]) -> None:
        self.run_id = run_id
        self.problems = problems
        super().__init__(
            f"run {run_id} failed its integrity audit and cannot be used as a "
            "verification baseline:\n  - " + "\n  - ".join(problems)
        )


class Replayer(Protocol):
    """What ``verify`` needs from the pipeline in order to re-execute a run.

    Declared as a Protocol so :mod:`abca.ledger` stays free of any dependency
    on the inference layer. Step 1 ships the ledger with no pipeline at all;
    steps 2-4 will supply a concrete implementation, and the tests here
    supply fakes. The auditability machinery is testable end to end before a
    single model call exists, which is the point of building it first.
    """

    def replay(self, record: RunRecord) -> RunRecord:
        """Re-execute ``record``'s recipe and return a fresh run record."""
        ...


#: Callable that re-fetches a URL and returns its current content digest, or
#: ``None`` when the source is unreachable. Injected rather than imported so
#: the ledger has no HTTP dependency.
SourceProbe = Callable[[str], str | None]


class VerifyReport:
    """Structured result of a verification, suitable for rendering or JSON.

    A plain class rather than a pydantic model: this object is derived,
    never persisted, and never hashed, so the validation machinery would buy
    nothing.
    """

    def __init__(
        self,
        *,
        outcome: VerifyOutcome,
        original: RunRecord,
        replay: RunRecord | None,
        recipe_diff: dict[str, tuple[Any, Any]] | None = None,
        verdict_diff: list[dict[str, Any]] | None = None,
        drifted_sources: list[dict[str, str]] | None = None,
        notes: list[str] | None = None,
    ) -> None:
        self.outcome = outcome
        self.original = original
        self.replay = replay
        self.recipe_diff = recipe_diff or {}
        self.verdict_diff = verdict_diff or []
        self.drifted_sources = drifted_sources or []
        self.notes = notes or []

    @property
    def ok(self) -> bool:
        """Whether the original run is confirmed. DRIFTED is not ok; see module docstring."""
        return self.outcome.is_success

    @property
    def exit_code(self) -> int:
        """Process exit code, so CI can gate on verification.

        Distinct codes per failure mode so a pipeline can treat drift
        (re-examine) differently from divergence (file a bug).
        """
        return {
            VerifyOutcome.IDENTICAL: 0,
            VerifyOutcome.EQUIVALENT: 0,
            VerifyOutcome.DRIFTED: 2,
            VerifyOutcome.DIVERGENT: 3,
            VerifyOutcome.UNREPLAYABLE: 4,
        }[self.outcome]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "ok": self.ok,
            "run_id": self.original.run_id,
            "original_ledger_hash": self.original.ledger_hash,
            "replay_ledger_hash": self.replay.ledger_hash if self.replay else None,
            "recipe_diff": {k: list(v) for k, v in self.recipe_diff.items()},
            "verdict_diff": self.verdict_diff,
            "drifted_sources": self.drifted_sources,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# Diff helpers
# --------------------------------------------------------------------------

def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested JSON into dotted paths, so diffs name the exact field.

    ``{"config": {"seed": 42}}`` becomes ``{"config.seed": 42}``. Lists are
    indexed. Used only for human-facing diff output, never for hashing.
    """
    flat: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        flat[prefix] = value
    return flat


def diff_recipes(original: RunRecord, replay: RunRecord) -> dict[str, tuple[Any, Any]]:
    """Field-level diff of two replay projections.

    Answers the question an operator actually has when verification fails
    with a recipe mismatch: *which knob moved?*
    """
    left = _flatten(original.replay_projection())
    right = _flatten(replay.replay_projection())
    changed: dict[str, tuple[Any, Any]] = {}
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            changed[key] = (left.get(key), right.get(key))
    return changed


def diff_verdicts(original: AnalysisResult, replay: AnalysisResult) -> list[dict[str, Any]]:
    """Claim-by-claim comparison of the semantic projections.

    Matches claims by id. Claims present in only one run are reported with
    the missing side as ``None``, because a replay that produced FEWER claims
    is a substantive disagreement even if every shared claim matches.
    """
    left = {c["id"]: c for c in original.semantic_projection()["claims"]}
    right = {c["id"]: c for c in replay.semantic_projection()["claims"]}

    differences: list[dict[str, Any]] = []
    for claim_id in sorted(set(left) | set(right)):
        a, b = left.get(claim_id), right.get(claim_id)
        if a == b:
            continue
        differences.append(
            {
                "claim_id": claim_id,
                "original": None if a is None else {
                    "verdict": a["verdict"],
                    "confidence": a["confidence"],
                    "evidence_quality": a["evidence_quality"],
                    "citation_count": len(a["citations"]),
                },
                "replay": None if b is None else {
                    "verdict": b["verdict"],
                    "confidence": b["confidence"],
                    "evidence_quality": b["evidence_quality"],
                    "citation_count": len(b["citations"]),
                },
            }
        )
    return differences


def detect_drift(record: RunRecord, probe: SourceProbe | None) -> list[dict[str, str]]:
    """Re-fetch every cited source and report any whose content hash changed.

    Sources that cannot be reached are reported with status ``unreachable``
    rather than being treated as unchanged. An unreachable source is not
    evidence that the verdict still holds -- a statute page that 404s is
    itself a reason to look again.

    Returns an empty list when no probe is supplied, which is the step-1
    situation: there is no retrieval layer yet, so drift is structurally
    impossible rather than merely unobserved.
    """
    if probe is None:
        return []

    drifted: list[dict[str, str]] = []
    for snapshot in record.sources:
        current = probe(snapshot.url)
        if current is None:
            drifted.append(
                {
                    "url": snapshot.url,
                    "status": "unreachable",
                    "recorded_hash": snapshot.content_hash,
                    "current_hash": "",
                }
            )
        elif current != snapshot.content_hash:
            drifted.append(
                {
                    "url": snapshot.url,
                    "status": "changed",
                    "recorded_hash": snapshot.content_hash,
                    "current_hash": current,
                }
            )
    return drifted


# --------------------------------------------------------------------------
# The comparison itself
# --------------------------------------------------------------------------

def compare(
    original: RunRecord,
    replay: RunRecord,
    *,
    probe: SourceProbe | None = None,
) -> VerifyReport:
    """Classify the relationship between an original run and its replay.

    Implements the decision tree in the module docstring. Split out from
    :func:`verify` so it can be tested with two hand-built records and no
    pipeline at all.
    """
    notes: list[str] = []

    # -- 0. The baseline must be trustworthy before anything is compared.
    problems = original.audit()
    if problems:
        raise LedgerTampered(original.run_id, problems)

    # -- 1. A run whose weights cannot be pinned cannot be certified.
    #       Reported honestly rather than being quietly graded on a curve.
    if not original.reproducible:
        unpinned = [m.name for m in original.config.models if not m.is_pinnable]
        notes.append(
            "original run used models whose weights cannot be pinned by hash "
            f"({', '.join(unpinned)}); a third party cannot certify this run. "
            "Re-run with local open-weight models for a verifiable result."
        )
        return VerifyReport(
            outcome=VerifyOutcome.UNREPLAYABLE,
            original=original,
            replay=replay,
            notes=notes,
        )

    # -- 2. Same question, same way? If not, the comparison is meaningless
    #       and the useful output is the diff showing what moved.
    if original.input_digest != replay.input_digest:
        recipe_diff = diff_recipes(original, replay)
        notes.append(
            "input digests differ: the replay did not ask the same question under "
            "the same recipe. This is normally operator error (a changed flag, a "
            "different model tag, an edited prompt file) rather than a tool defect."
        )
        return VerifyReport(
            outcome=VerifyOutcome.DIVERGENT,
            original=original,
            replay=replay,
            recipe_diff=recipe_diff,
            notes=notes,
        )

    # -- 3. Byte-identical output.
    if original.output_digest == replay.output_digest:
        return VerifyReport(
            outcome=VerifyOutcome.IDENTICAL,
            original=original,
            replay=replay,
            notes=notes,
        )

    # -- 4. Same conclusions from the same evidence; only prose moved.
    if original.semantic_digest == replay.semantic_digest:
        notes.append(
            "verdicts, confidences and citations match; only free-text prose "
            "differs between the runs."
        )
        return VerifyReport(
            outcome=VerifyOutcome.EQUIVALENT,
            original=original,
            replay=replay,
            notes=notes,
        )

    # -- 5. Before calling it a bug, check whether the world moved.
    drifted = detect_drift(original, probe)
    if drifted:
        notes.append(
            f"{len(drifted)} cited source(s) changed or became unreachable since "
            "retrieval; the verdict rests on text that no longer matches what was "
            "read. Re-examine rather than assuming either result is correct."
        )
        return VerifyReport(
            outcome=VerifyOutcome.DRIFTED,
            original=original,
            replay=replay,
            drifted_sources=drifted,
            verdict_diff=diff_verdicts(original.result, replay.result),
            notes=notes,
        )

    # -- 6. Identical recipe, unchanged sources, different conclusions.
    notes.append(
        "identical recipe and unchanged sources produced different verdicts. This "
        "is a defect in the analyzer: either non-determinism leaked into the "
        "pipeline (unsorted iteration, wall-clock or randomness in a prompt, a "
        "concurrent retrieval race) or the recorded config does not describe what "
        "actually ran."
    )
    return VerifyReport(
        outcome=VerifyOutcome.DIVERGENT,
        original=original,
        replay=replay,
        verdict_diff=diff_verdicts(original.result, replay.result),
        notes=notes,
    )


def verify(
    record: RunRecord,
    replayer: Replayer,
    *,
    probe: SourceProbe | None = None,
) -> VerifyReport:
    """Replay ``record`` through ``replayer`` and classify the result."""
    replayed = replayer.replay(record)
    return compare(record, replayed, probe=probe)


__all__ = [
    "LedgerTampered",
    "Replayer",
    "SourceProbe",
    "VerifyReport",
    "compare",
    "detect_drift",
    "diff_recipes",
    "diff_verdicts",
    "verify",
]
