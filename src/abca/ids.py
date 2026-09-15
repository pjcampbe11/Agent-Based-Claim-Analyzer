"""Run identifiers.

abCA run IDs are ULIDs: 128 bits made of a 48-bit millisecond timestamp
followed by 80 bits of cryptographic randomness, rendered as 26 characters
of Crockford Base32.

WHY ULID AND NOT UUID4
======================
Run IDs are used as filenames and are listed constantly during debugging
and auditing. Three properties matter, and UUID4 has none of them:

1. **Lexicographic order equals chronological order.** ``ls`` on the run
   directory comes out in the order the runs happened. No index needed.
2. **The timestamp is recoverable from the ID.** A run ID pasted into a
   bug report carries its own creation time, which is the single most
   useful piece of context when a published verdict is disputed.
3. **Crockford Base32 is transcription-safe.** The alphabet excludes I, L,
   O, and U, so a run ID read aloud or copied by hand from a printed
   report does not turn into a different valid ID. Run IDs are meant to be
   published next to verdicts, so humans will handle them.

WHY IMPLEMENTED HERE RATHER THAN ADDED AS A DEPENDENCY
=====================================================
It is roughly forty lines. Every dependency in this package is a dependency
the reproducibility story has to account for, because installed versions are
recorded in each run's environment block and reported on verification
mismatch. A forty-line implementation with tests is cheaper than that.

COLLISION SAFETY
================
80 random bits per millisecond. Even generating a thousand runs inside a
single millisecond, the collision probability is on the order of 1e-19.
:func:`secrets.token_bytes` is used rather than :mod:`random` because the
IDs are published and should not be predictable.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime
from typing import Final

#: Crockford Base32. Note the deliberate omissions: I, L, O, U.
#: I/L are excluded because they are confusable with 1; O with 0; U is
#: excluded by Crockford to avoid accidental obscenities in generated IDs.
_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Reverse lookup for decoding. Built once at import.
_DECODE: Final[dict[str, int]] = {ch: i for i, ch in enumerate(_ALPHABET)}

#: A ULID is 128 bits, which is 26 base-32 characters (26 * 5 = 130 bits,
#: with the top 2 bits of the first character always zero).
ULID_LENGTH: Final[int] = 26

#: 48-bit millisecond timestamp ceiling. Corresponds to year 10889.
_MAX_TIMESTAMP_MS: Final[int] = (1 << 48) - 1


class InvalidRunId(ValueError):
    """Raised when a string is not a well-formed ULID run identifier."""


def _encode_base32(value: int, length: int) -> str:
    """Render ``value`` as exactly ``length`` Crockford Base32 characters.

    Big-endian: most significant group first, which is what makes
    lexicographic string order match numeric order.
    """
    chars = [""] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_run_id(*, timestamp_ms: int | None = None) -> str:
    """Generate a fresh run ID.

    ``timestamp_ms`` exists solely so tests can pin time; production callers
    should never pass it.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    if not 0 <= timestamp_ms <= _MAX_TIMESTAMP_MS:
        raise InvalidRunId(f"timestamp {timestamp_ms} outside 48-bit ULID range")

    randomness = int.from_bytes(secrets.token_bytes(10), "big")  # 80 bits
    combined = (timestamp_ms << 80) | randomness
    return _encode_base32(combined, ULID_LENGTH)


def parse_run_id(run_id: str) -> int:
    """Decode a run ID to its 128-bit integer value, validating as it goes.

    Accepts lowercase input by upcasing, and applies Crockford's standard
    confusable substitutions (I/L -> 1, O -> 0) so that an ID mis-transcribed
    from print still resolves. This is a read-path convenience only:
    :func:`new_run_id` never emits those characters.
    """
    if not isinstance(run_id, str):
        raise InvalidRunId(f"run id must be a string, got {type(run_id).__name__}")

    normalized = (
        run_id.strip()
        .upper()
        .replace("I", "1")
        .replace("L", "1")
        .replace("O", "0")
    )
    if len(normalized) != ULID_LENGTH:
        raise InvalidRunId(
            f"run id must be {ULID_LENGTH} characters, got {len(normalized)}: {run_id!r}"
        )

    value = 0
    for char in normalized:
        digit = _DECODE.get(char)
        if digit is None:
            raise InvalidRunId(f"invalid character {char!r} in run id {run_id!r}")
        value = (value << 5) | digit

    # 26 base-32 characters carry 130 bits; a valid ULID uses only the low
    # 128. If the top two bits are set the string is 26 valid characters but
    # not a valid ULID, so reject it rather than silently truncating.
    if value >= (1 << 128):
        raise InvalidRunId(f"run id {run_id!r} overflows 128 bits")
    return value


def run_id_timestamp(run_id: str) -> datetime:
    """Recover the creation time embedded in a run ID, as an aware UTC datetime."""
    milliseconds = parse_run_id(run_id) >> 80
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def normalize_run_id(run_id: str) -> str:
    """Return the canonical uppercase form of a possibly mis-transcribed ID.

    Used by the CLI so that ``abca verify 01j8x...`` finds the run stored as
    ``01J8X...``.
    """
    return _encode_base32(parse_run_id(run_id), ULID_LENGTH)


__all__ = [
    "ULID_LENGTH",
    "InvalidRunId",
    "new_run_id",
    "normalize_run_id",
    "parse_run_id",
    "run_id_timestamp",
]
