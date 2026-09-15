"""Local store of analyzed input text, keyed by content hash.

WHY THE RUN RECORD DOES NOT CONTAIN THE TEXT
============================================
Run records are meant to be published. Publishing one should not mean
republishing somebody else's article, or two thousand strangers' posts. So the
record stores the input's HASH and nothing else.

That is the right call, and it has a consequence: replaying a run requires
somebody to supply the same input again. For a third party verifying a
published verdict, that is exactly correct — they should have to bring the text
and prove it hashes to what the record claims. It is the difference between
"trust this record" and "here is the statement, re-run it yourself".

WHY THIS STORE EXISTS ANYWAY
============================
For your OWN runs on your OWN machine, re-typing the input to verify a run you
just made is friction with no security benefit. So the analyzer keeps a local
copy, addressed by the same hash the record carries.

It is deliberately:

* **local** — under the user data directory, never inside a run record, never
  published, and covered by ``.gitignore``;
* **hash-addressed** — a stored input that does not hash to its key is
  discarded on read, so a hand-edited copy cannot be substituted for the text
  that was actually analyzed;
* **optional** — every command that uses it works without it, by asking for the
  input explicitly.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from abca.canonical import digest_text


def default_input_dir() -> Path:
    """Where analyzed inputs are cached. Beside the ledger and source cache."""
    from abca.ledger.store import default_data_dir

    return default_data_dir() / "inputs"


class InputStore:
    """Content-addressed store for analyzed text."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_input_dir()

    def _path(self, content_hash: str) -> Path:
        key = content_hash.split(":", 1)[-1]
        return self.root / key[:2] / f"{key}.txt"

    def put(self, text: str) -> str:
        """Store ``text``; return its content hash.

        Hashes the text EXACTLY as given. Callers pass already-normalized text,
        so the key matches ``DocumentRef.content_hash`` without this module
        needing to know the normalization recipe.
        """
        content_hash = digest_text(text)
        path = self._path(content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)

        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.stem}.", suffix=".tmp", delete=False,
        )
        try:
            with handle as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        return content_hash

    def get(self, content_hash: str) -> str | None:
        """Retrieve text by hash, verifying it still hashes to that value.

        A mismatch returns ``None`` rather than the stored text. Without this
        check, editing a cached input would let a replay run against different
        text while claiming to reproduce the original — which is precisely the
        substitution the whole verification story exists to prevent.
        """
        path = self._path(content_hash)
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return text if digest_text(text) == content_hash else None

    def has(self, content_hash: str) -> bool:
        return self.get(content_hash) is not None

    def count(self) -> int:
        return sum(1 for _ in self.root.rglob("*.txt")) if self.root.is_dir() else 0


__all__ = ["InputStore", "default_input_dir"]
