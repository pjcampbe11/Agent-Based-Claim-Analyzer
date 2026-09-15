"""Loading and hashing the institutional corpus.

THE HASH IS THE POINT
=====================
``context_pack_hash`` joins the ledger. A brief that cites a mechanism entry is
reproducible only against the corpus version that produced it, so ``abca verify``
can diff the corpus exactly the way it diffs a retrieved source. Without the
hash, an entry could be silently reworded after publication and every past brief
would quietly start meaning something else.

The hash covers the canonical JSON of the entries, not the file bytes, so
reformatting the file does not invalidate a run while changing a single word
does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from abca.canonical import canonical_json, digest_bytes
from abca.context_pack.entry import ContextEntry

#: Shipped with the repo, alongside ``evals/``. Like ``evals/``, it is
#: DATA the analyzer is pointed at -- see the import-lint in tests.
CORPUS_PATH = Path(__file__).resolve().parents[3] / "corpus" / "institutional" / "entries.json"


class CorpusError(RuntimeError):
    """The corpus could not be loaded or is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """The loaded corpus, with the hash that pins it."""

    version: str
    entries: tuple[ContextEntry, ...]
    content_hash: str

    def by_id(self, entry_id: str) -> ContextEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def in_domain(self, domain: str) -> tuple[ContextEntry, ...]:
        return tuple(e for e in self.entries if e.domain == domain)

    def stale(self, *, today: date | None = None) -> tuple[ContextEntry, ...]:
        """Entries past their review date. Served with a warning, not withheld.

        Withholding a stale entry would silently make a brief worse; serving it
        with its staleness attached lets the reader weigh it.
        """
        return tuple(e for e in self.entries if e.volatility.is_stale(today=today))

    def summary(self) -> dict[str, object]:
        return {
            "version": self.version,
            "entries": len(self.entries),
            "hash": self.content_hash,
            "domains": {
                d: len(self.in_domain(d))
                for d in sorted({e.domain for e in self.entries})
            },
        }


def load_corpus(path: Path | None = None) -> ContextPack:
    """Load, validate and hash the corpus.

    Raises rather than degrading. A missing or malformed corpus must not produce
    an empty pack that silently makes every brief thinner -- that failure would
    be invisible in output and obvious only in a diff of published briefs.
    """
    source = path or CORPUS_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CorpusError(
            f"no institutional corpus at {source}. The context pack is data that "
            "ships with the repo; an absent corpus is a packaging failure, not an "
            "empty result."
        ) from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(f"corpus at {source} is not valid JSON: {exc}") from exc

    entries = tuple(ContextEntry.model_validate(e) for e in raw.get("entries", []))

    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise CorpusError(f"duplicate entry ids in the corpus: {duplicates}")

    known = set(ids)
    for entry in entries:
        missing = [ref for ref in entry.distinguish_from if ref not in known]
        if missing:
            raise CorpusError(
                f"entry {entry.id} distinguishes itself from unknown entries {missing}. "
                "Contrast pairs are what make the pack useful, so a dangling reference "
                "means a reader is told to compare against something that is not there."
            )

    payload = canonical_json([e.to_jsonable() for e in entries])
    return ContextPack(
        version=raw.get("version", "corpus/institutional/0.0.0"),
        entries=entries,
        content_hash=digest_bytes(payload),
    )


__all__ = ["CORPUS_PATH", "ContextPack", "CorpusError", "load_corpus"]
