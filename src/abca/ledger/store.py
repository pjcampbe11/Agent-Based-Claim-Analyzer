"""On-disk storage for run records.

DESIGN
======
One JSON file per run, named by run ID, in a flat directory. Deliberately
boring:

* **No database.** A run record is an append-only document that is written
  once and read occasionally. A database would add a dependency, a schema
  migration story, and a binary format that a third party cannot inspect
  with ``cat``. The whole value proposition is that anyone can check the
  record; that argues for the most inspectable format available.
* **Flat directory, ULID names.** Because ULIDs sort chronologically,
  ``ls`` is already a run history and listing needs no index (see
  :mod:`abca.ids`).
* **Pretty-printed JSON on disk, canonical bytes for hashing.** These are
  different jobs. The file is for humans and diff tools, so it is indented
  and key-sorted. The hash is computed over
  :func:`abca.canonical.canonical_json` output, independent of file
  formatting -- so reformatting a stored file does not change its digests,
  and a verifier reproduces the hash without needing to match our
  indentation.

ATOMICITY
=========
Writes go to a temporary file in the same directory, are flushed and fsynced,
and are then moved into place with :func:`os.replace`, which is atomic on
both POSIX and Windows. A crash mid-write therefore leaves either the old
file or the new one, never a half-written record. Same-directory placement
matters: ``os.replace`` across filesystems is not atomic.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from abca.ids import normalize_run_id
from abca.schema.ledger import RunRecord


class RunNotFound(KeyError):
    """Raised when a run ID has no record in the store."""


class LedgerCorrupt(ValueError):
    """Raised when a stored record cannot be parsed or fails its own audit."""


def default_data_dir() -> Path:
    """Return the platform-appropriate abCA data directory.

    Windows is the primary target, so ``%LOCALAPPDATA%`` is checked first --
    LOCALAPPDATA rather than APPDATA because run records are machine-local
    state that should not follow a roaming profile across machines. On
    POSIX, ``XDG_DATA_HOME`` is honored, falling back to the XDG default.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "abca"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "abca"

    return Path.home() / ".local" / "share" / "abca"


class LedgerStore:
    """Reads and writes run records under a root directory."""

    def __init__(self, root: Path | str | None = None) -> None:
        """``root`` defaults to ``<data dir>/runs``. Tests pass a tmp_path."""
        self.root = Path(root) if root is not None else default_data_dir() / "runs"

    # --------------------------------------------------------------- locations

    def path_for(self, run_id: str) -> Path:
        """Return the file path for a run ID, normalizing the ID first.

        Normalization means ``abca verify 01j8x...`` and a copy where an O was
        transcribed for a 0 both resolve to the same stored file.
        """
        return self.root / f"{normalize_run_id(run_id)}.json"

    def exists(self, run_id: str) -> bool:
        return self.path_for(run_id).is_file()

    # ------------------------------------------------------------------- write

    def write(self, record: RunRecord, *, overwrite: bool = False) -> Path:
        """Persist a run record atomically.

        Refuses to overwrite by default. A run record is evidence; silently
        replacing one would be the easiest possible way to launder a bad
        result, so it takes an explicit flag that no production code path
        sets.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(record.run_id)

        if target.exists() and not overwrite:
            raise FileExistsError(
                f"run {record.run_id} already exists at {target}. Run records are "
                "append-only evidence; pass overwrite=True only in tests."
            )

        # Sort keys and indent for human diffability. This formatting is
        # irrelevant to the digests, which are computed over canonical bytes.
        payload = json.dumps(record.to_jsonable(), indent=2, sort_keys=True, ensure_ascii=False)

        # Temp file in the SAME directory so os.replace is atomic.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{target.stem}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                # fsync so the bytes are durable before the rename. Without
                # this, a power loss after replace() can leave an empty file.
                os.fsync(stream.fileno())
            os.replace(handle.name, target)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            Path(handle.name).unlink(missing_ok=True)
            raise

        return target

    # -------------------------------------------------------------------- read

    def read(self, run_id: str, *, audit: bool = True) -> RunRecord:
        """Load a run record.

        ``audit=True`` (the default) recomputes every digest and every chain
        link before returning, so a tampered or truncated file is caught at
        read time rather than being quietly used. Callers that specifically
        want to INSPECT a damaged record -- the ``abca ledger audit`` command
        -- pass ``audit=False`` and call :meth:`RunRecord.audit` themselves.
        """
        path = self.path_for(run_id)
        if not path.is_file():
            raise RunNotFound(f"no run record for {run_id} at {path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LedgerCorrupt(f"{path} is not valid JSON: {exc}") from exc

        try:
            record = RunRecord.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError and friends
            raise LedgerCorrupt(f"{path} does not match the run record schema: {exc}") from exc

        if audit:
            problems = record.audit()
            if problems:
                raise LedgerCorrupt(
                    f"{path} failed its integrity audit -- the file has been modified "
                    "since it was written:\n  - " + "\n  - ".join(problems)
                )
        return record

    def read_raw(self, run_id: str) -> dict:
        """Load the stored JSON without schema validation.

        Needed by the audit command: a record whose schema no longer parses
        still needs to be shown to a human rather than hidden behind an
        exception.
        """
        path = self.path_for(run_id)
        if not path.is_file():
            raise RunNotFound(f"no run record for {run_id} at {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    # -------------------------------------------------------------------- list

    def list_ids(self, *, limit: int | None = None, newest_first: bool = True) -> list[str]:
        """List stored run IDs.

        Sorting the filenames is sufficient to sort chronologically, because
        ULIDs are lexicographically ordered by their embedded timestamp. No
        stat calls, no index.
        """
        if not self.root.is_dir():
            return []
        ids = sorted(
            (p.stem for p in self.root.glob("*.json") if not p.name.startswith(".")),
            reverse=newest_first,
        )
        return ids[:limit] if limit is not None else ids

    def iter_records(self, *, limit: int | None = None) -> Iterator[RunRecord]:
        """Yield records newest-first, skipping any that fail to load.

        Corrupt records are skipped rather than aborting the whole listing:
        one bad file should not make the other 500 runs unlistable. The audit
        command is the place that reports them.
        """
        for run_id in self.list_ids(limit=limit):
            try:
                yield self.read(run_id)
            except (LedgerCorrupt, RunNotFound):
                continue


__all__ = ["LedgerCorrupt", "LedgerStore", "RunNotFound", "default_data_dir"]
