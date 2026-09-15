"""Ingest: raw input to a hashed, normalized document.

NORMALIZATION IS VISIBLE, NOT HIDDEN
====================================
:mod:`abca.canonical` deliberately does NOT Unicode-normalize when hashing,
because silently mutating a citation quote would make it disagree with the
source it claims to reproduce. Normalization belongs here instead, where it is:

* **named** -- ``DocumentRef.normalization`` records the recipe id, so a
  verifier knows exactly what transformation to apply before re-hashing;
* **counted** -- every change is reported in the stage notes, so a document
  that arrived full of zero-width characters is a visible fact rather than a
  silent cleanup;
* **applied once**, before anything else sees the text.

The content hash is of the NORMALIZED text. Everything downstream -- sentence
spans, claim spans, the ledger -- indexes that same string.

WHY NORMALIZE AT ALL
====================
Three concrete problems, all of which show up in real pasted text:

1. **Unicode equivalence.** ``é`` can be one code point or two. Un-normalized,
   the same visible document produces two different hashes depending on which
   editor produced it, and a verifier re-fetching the source would report
   spurious drift.
2. **Line endings.** ``\\r\\n`` versus ``\\n`` shifts every span by one per line.
3. **Invisible characters.** Zero-width joiners and directional marks are
   routinely used to evade text matching. Stripping them is a substantive
   decision, so it is recorded rather than assumed.

WHAT IS NOT DONE
================
Case is not folded, punctuation is not straightened, and whitespace inside a
line is left alone. Those would change what the author wrote. The line is:
normalize representation, never content.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher

from abca.canonical import digest_text
from abca.schema.core import DocumentRef
from abca.schema.enums import InputKind

#: Identifier recorded in ``DocumentRef.normalization``. Any change to the
#: recipe below REQUIRES a new id here, because a verifier applies the recipe
#: named by this string to reproduce the content hash. Silently changing the
#: recipe under a stable name would break every published run hash.
NORMALIZATION_RECIPE = "abca-nfc-1"

#: Zero-width and directional formatting characters. Invisible to a reader and
#: routinely used to defeat exact-match search, so they are removed -- and the
#: removal is counted and reported, never silent.
_INVISIBLE = frozenset(
    "\u200b‌‍⁠﻿"          # zero-width space/joiners, BOM
    "‎‏"                             # LTR/RTL marks
    "‪‫‬‭‮"           # bidi embedding/override
    "⁦⁧⁨⁩"                 # bidi isolates
)

#: Non-breaking and exotic spaces, folded to an ordinary space. Kept as spaces
#: rather than deleted: they separate words, and deleting them would join words
#: that the author kept apart.
_SPACE_LIKE = frozenset("        "
                        "       　")


@dataclass(frozen=True, slots=True)
class IngestResult:
    """The normalized document plus a record of what normalization changed."""

    text: str
    document: DocumentRef
    notes: list[str] = field(default_factory=list)
    #: Per-transformation counts, for the stage record.
    changes: dict[str, int] = field(default_factory=dict)

    @property
    def was_modified(self) -> bool:
        return any(self.changes.values())


def normalize_text(raw: str) -> tuple[str, dict[str, int]]:
    """Apply the ``abca-nfc-1`` recipe. Returns the text and a change count.

    The order matters and is fixed:

    1. Line endings to ``\\n``.
    2. Invisible formatting characters removed.
    3. Exotic spaces folded to U+0020.
    4. NFC composition.
    5. Trailing whitespace stripped per line; leading/trailing blank lines
       removed from the document.

    NFC comes AFTER the character-level substitutions so the substitutions see
    the code points that were actually in the input, not composed forms that
    NFC would have produced.
    """
    changes: dict[str, int] = {}

    # 1. Line endings. \r\n first so a lone \r is not double-counted.
    crlf = raw.count("\r\n")
    text = raw.replace("\r\n", "\n")
    lone_cr = text.count("\r")
    text = text.replace("\r", "\n")
    if crlf or lone_cr:
        changes["line_endings"] = crlf + lone_cr

    # 2. Invisible formatting characters.
    invisible = sum(1 for char in text if char in _INVISIBLE)
    if invisible:
        text = "".join(char for char in text if char not in _INVISIBLE)
        changes["invisible_removed"] = invisible

    # 3. Exotic spaces.
    exotic = sum(1 for char in text if char in _SPACE_LIKE)
    if exotic:
        text = "".join(" " if char in _SPACE_LIKE else char for char in text)
        changes["spaces_folded"] = exotic

    # 4. Unicode NFC.
    composed = unicodedata.normalize("NFC", text)
    if composed != text:
        # Count the characters NFC actually touched, not a boolean: one
        # decomposed accent is a different situation from a wholly decomposed
        # document, and the operator may want to know which.
        #
        # A naive positional zip() is wrong here. NFC changes LENGTH (e +
        # combining acute -> a single code point), so after the first
        # difference every subsequent position compares unequal and the count
        # reports the whole document as changed. SequenceMatcher counts the
        # real edit, which for one accent is 1.
        matcher = SequenceMatcher(None, text, composed, autojunk=False)
        changes["nfc_recomposed"] = sum(
            max(old_end - old_start, new_end - new_start)
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes()
            if tag != "equal"
        )
        text = composed

    # 5. Trailing whitespace. Removed per line because it is invisible, shifts
    #    spans, and is never meaningful.
    lines = text.split("\n")
    trimmed = [line.rstrip() for line in lines]
    if trimmed != lines:
        changes["trailing_whitespace"] = sum(
            1 for a, b in zip(lines, trimmed, strict=True) if a != b
        )
    text = "\n".join(trimmed).strip("\n")

    return text, changes


def ingest_text(
    raw: str,
    *,
    kind: InputKind = InputKind.TEXT,
    locator: str = "<inline>",
    retrieved_at: datetime | None = None,
) -> IngestResult:
    """Normalize ``raw`` and build its :class:`DocumentRef`.

    Raises :class:`ValueError` on empty input rather than producing a document
    with nothing in it -- an empty analysis would still write a ledger entry,
    and a run record asserting that nothing was found is worse than a clear
    error at the point of entry.
    """
    if not raw or not raw.strip():
        raise ValueError(
            "input is empty. Provide a statement with -t/--text, a file with "
            "-f/--file, or pipe it in with --stdin."
        )

    text, changes = normalize_text(raw)
    if not text:
        raise ValueError("input contained nothing but whitespace after normalization")

    notes: list[str] = []
    if changes.get("invisible_removed"):
        notes.append(
            f"removed {changes['invisible_removed']} invisible formatting "
            "character(s) (zero-width or bidirectional). These are often used "
            "to defeat exact-match search; the removal is recorded rather than "
            "silent."
        )
    if changes.get("line_endings"):
        notes.append(f"normalized {changes['line_endings']} line ending(s) to \\n")
    if changes.get("spaces_folded"):
        notes.append(f"folded {changes['spaces_folded']} non-standard space character(s)")
    if changes.get("nfc_recomposed"):
        notes.append(f"applied Unicode NFC ({changes['nfc_recomposed']} character(s) changed)")
    if changes.get("trailing_whitespace"):
        notes.append(f"stripped trailing whitespace from {changes['trailing_whitespace']} line(s)")

    document = DocumentRef(
        kind=kind,
        locator=locator,
        content_hash=digest_text(text),
        retrieved_at=retrieved_at or datetime.now(UTC),
        byte_length=len(text.encode("utf-8")),
        normalization=NORMALIZATION_RECIPE,
    )
    return IngestResult(text=text, document=document, notes=notes, changes=changes)


__all__ = [
    "NORMALIZATION_RECIPE",
    "IngestResult",
    "ingest_text",
    "normalize_text",
]
