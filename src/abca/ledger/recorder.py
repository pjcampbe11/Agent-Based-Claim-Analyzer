"""Building a run record as the pipeline executes.

The recorder is the ONLY sanctioned way to construct a :class:`RunRecord`.
Everything about the record that is easy to get wrong -- chain linkage,
sequence numbering, digest computation, the honesty of the ``reproducible``
flag -- is computed here rather than supplied by callers.

USAGE
=====
::

    recorder = RunRecorder(document=doc_ref, config=cfg)

    with recorder.stage(StageName.INGEST) as stage:
        stage.set_input({"locator": "..."})
        text = do_ingest()
        stage.set_output({"byte_length": len(text)})

    with recorder.stage(StageName.SEGMENT) as stage:
        ...

    record = recorder.finalize(result)

The context manager times the stage, hashes whatever the caller declared as
input and output, links the record to its predecessor, and appends it. A
stage that raises still gets recorded, marked with the exception, so a failed
run leaves an auditable trail instead of nothing.

WHAT THE CALLER DECLARES
========================
:meth:`StageContext.set_input` and :meth:`StageContext.set_output` take a
JSON-native summary, not the raw payload. This is a judgment call worth
stating plainly: the stage hash covers what the caller SAYS the stage
consumed and produced. That means a careless caller can under-declare and
weaken the chain.

The alternative -- hashing the full intermediate payloads -- was rejected
because those payloads routinely contain the analyzed text, and run records
are meant to be publishable without republishing other people's posts. The
mitigation is that the *summary shape* for each stage is fixed by the
pipeline code and covered by tests, so under-declaration shows up as a test
failure rather than as an unnoticed weakening.
"""

from __future__ import annotations

import platform
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from abca.canonical import digest_chain, digest_json
from abca.ids import new_run_id
from abca.schema.core import AnalysisResult, DocumentRef
from abca.schema.enums import StageName
from abca.schema.ledger import (
    EnvironmentInfo,
    RunConfig,
    RunRecord,
    SourceSnapshot,
    StageRecord,
)
from abca.version import SCHEMA_VERSION, TOOL_VERSION

#: Dependencies whose versions are recorded in every run. Kept short and
#: explicit rather than enumerating the whole environment: these are the
#: libraries whose behavior could plausibly change an analysis result.
_TRACKED_PACKAGES: tuple[str, ...] = ("pydantic", "typer")


def collect_environment() -> EnvironmentInfo:
    """Snapshot the machine and library context for the current process.

    Import failures are swallowed per-package and recorded as ``"absent"``
    rather than raising: a missing optional dependency is information worth
    recording, not a reason to abort a run that otherwise succeeded.
    """
    packages: dict[str, str] = {}
    for name in _TRACKED_PACKAGES:
        try:
            module = __import__(name)
            packages[name] = str(getattr(module, "__version__", "unknown"))
        except Exception:
            packages[name] = "absent"

    return EnvironmentInfo(
        tool_version=TOOL_VERSION,
        schema_version=SCHEMA_VERSION,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        packages=packages,
    )


@dataclass
class StageContext:
    """Mutable handle yielded by :meth:`RunRecorder.stage`.

    Exists so a stage's input and output summaries can be declared at the
    natural points in the caller's code rather than assembled up front.
    """

    name: StageName
    _input: Any = None
    _output: Any = None
    _notes: list[str] = field(default_factory=list)
    _input_set: bool = False
    _output_set: bool = False

    def set_input(self, value: Any) -> None:
        """Declare the stage's input summary. JSON-native types only."""
        self._input = value
        self._input_set = True

    def set_output(self, value: Any) -> None:
        """Declare the stage's output summary. JSON-native types only."""
        self._output = value
        self._output_set = True

    def note(self, message: str) -> None:
        """Attach an operator-facing note (truncation, fallback, degraded mode)."""
        self._notes.append(message)


class RunRecorder:
    """Accumulates stage records and finalizes them into a :class:`RunRecord`."""

    def __init__(
        self,
        *,
        document: DocumentRef,
        config: RunConfig,
        run_id: str | None = None,
        created_at: datetime | None = None,
        environment: EnvironmentInfo | None = None,
    ) -> None:
        """
        ``run_id``, ``created_at`` and ``environment`` are injectable purely so
        tests can produce byte-stable records. Production callers pass none of
        them.
        """
        self.run_id = run_id or new_run_id()
        self.created_at = created_at or datetime.now(UTC)
        self.document = document
        self.config = config
        self.environment = environment or collect_environment()

        self._stages: list[StageRecord] = []
        self._sources: dict[str, SourceSnapshot] = {}
        self._timings: dict[str, int] = {}

    # ------------------------------------------------------------------ sources

    def record_source(self, snapshot: SourceSnapshot) -> None:
        """Register a retrieved source so it enters the input digest.

        Keyed by URL. A second registration of the same URL with a DIFFERENT
        hash means the source changed while the run was in flight; the first
        observation is kept (it is what the earlier claims actually cited) and
        the collision is surfaced, because silently keeping either one would
        make the record disagree with the citations inside it.
        """
        existing = self._sources.get(snapshot.url)
        if existing is None:
            self._sources[snapshot.url] = snapshot
            return
        if existing.content_hash != snapshot.content_hash:
            raise ValueError(
                f"source {snapshot.url!r} was observed with two different content "
                f"hashes during a single run ({existing.content_hash} then "
                f"{snapshot.content_hash}). The source changed mid-run; abort and "
                "re-run rather than recording a self-inconsistent ledger."
            )

    # ------------------------------------------------------------------- stages

    @contextmanager
    def stage(self, name: StageName) -> Iterator[StageContext]:
        """Time, hash and link one pipeline stage.

        A stage that raises is still recorded, with the exception text in its
        notes, and the exception is re-raised. A run that failed halfway
        should leave evidence of exactly how far it got.
        """
        context = StageContext(name=name)
        started_at = datetime.now(UTC)
        # Monotonic clock for the duration: wall-clock can jump (NTP, DST) and
        # would occasionally yield negative durations.
        started_perf = time.perf_counter()

        try:
            yield context
        except BaseException as exc:
            context.note(f"stage raised {type(exc).__name__}: {exc}")
            self._append_stage(context, started_at, started_perf)
            raise
        else:
            self._append_stage(context, started_at, started_perf)

    def _append_stage(
        self,
        context: StageContext,
        started_at: datetime,
        started_perf: float,
    ) -> None:
        """Hash a completed stage and link it onto the chain."""
        ended_at = datetime.now(UTC)
        duration_ms = max(0, int((time.perf_counter() - started_perf) * 1000))

        # An undeclared input or output hashes as JSON null rather than being
        # rejected. Some stages genuinely have no meaningful summary (a gate
        # that dropped everything), and forcing a placeholder would be noise.
        input_hash = digest_json(context._input if context._input_set else None)
        output_hash = digest_json(context._output if context._output_set else None)

        prev_hash = self._stages[-1].record_hash if self._stages else None
        sequence = len(self._stages)

        payload = {
            "name": context.name.value,
            "sequence": sequence,
            "input_hash": input_hash,
            "output_hash": output_hash,
        }
        record_hash = digest_chain(prev_hash, payload)

        self._stages.append(
            StageRecord(
                name=context.name,
                sequence=sequence,
                started_at=started_at,
                ended_at=ended_at,
                input_hash=input_hash,
                output_hash=output_hash,
                prev_hash=prev_hash,
                record_hash=record_hash,
                duration_ms=duration_ms,
                notes=list(context._notes),
            )
        )
        # Accumulate rather than overwrite: a stage can legitimately run more
        # than once (retry after a validation failure) and the reported timing
        # should be the total cost, not the last attempt.
        self._timings[context.name.value] = (
            self._timings.get(context.name.value, 0) + duration_ms
        )

    # ----------------------------------------------------------------- finalize

    def finalize(self, result: AnalysisResult) -> RunRecord:
        """Compute all digests and return the immutable record.

        Digests are computed in dependency order: the three content digests
        first, then ``ledger_hash``, which commits to them and to the chain
        head. The record is constructed twice -- once with placeholder
        ``ledger_hash``, then copied with the real one -- because
        :meth:`RunRecord.compute_ledger_hash` reads the other digests off the
        instance, and building it in one shot would require duplicating that
        logic here where it could drift.
        """
        draft = RunRecord(
            run_id=self.run_id,
            schema_version=SCHEMA_VERSION,
            created_at=self.created_at,
            input=self.document,
            config=self.config,
            environment=self.environment,
            sources=sorted(self._sources.values(), key=lambda s: s.url),
            stages=list(self._stages),
            result=result,
            # The flag is derived, never asserted. RunRecord additionally
            # validates it, so an inconsistency here fails loudly.
            reproducible=self.config.is_fully_pinnable,
            timings_ms=dict(self._timings),
            input_digest="sha256:" + "0" * 64,
            semantic_digest="sha256:" + "0" * 64,
            output_digest="sha256:" + "0" * 64,
            ledger_hash="sha256:" + "0" * 64,
        )

        with_digests = draft.model_copy(
            update={
                "input_digest": draft.compute_input_digest(),
                "semantic_digest": draft.compute_semantic_digest(),
                "output_digest": draft.compute_output_digest(),
            }
        )
        final = with_digests.model_copy(
            update={"ledger_hash": with_digests.compute_ledger_hash()}
        )

        # Belt and braces: a record that leaves this function must audit
        # clean. If it does not, the recorder itself is broken and the run
        # should fail rather than emit a ledger that lies.
        problems = final.audit()
        if problems:
            raise AssertionError(
                "recorder produced an internally inconsistent run record; this is "
                "a bug in abca.ledger.recorder, not in the caller:\n  - "
                + "\n  - ".join(problems)
            )
        return final


__all__ = ["RunRecorder", "StageContext", "collect_environment"]
