"""Run ID (ULID) tests."""

from __future__ import annotations

import pytest

from abca.ids import (
    ULID_LENGTH,
    InvalidRunId,
    new_run_id,
    normalize_run_id,
    parse_run_id,
    run_id_timestamp,
)


def test_length_and_alphabet():
    run_id = new_run_id()
    assert len(run_id) == ULID_LENGTH
    # Crockford Base32 excludes I, L, O, U so printed IDs cannot be
    # mis-transcribed into a different valid ID.
    assert not set(run_id) & set("ILOU")


def test_lexicographic_order_matches_chronological_order():
    """The property that makes `ls` a run history and removes the need for an index."""
    ids = [new_run_id(timestamp_ms=t) for t in (1_000, 2_000, 3_000, 1_700_000_000_000)]
    assert ids == sorted(ids)


def test_timestamp_round_trips():
    """A run ID pasted into a bug report carries its own creation time."""
    recovered = run_id_timestamp(new_run_id(timestamp_ms=1_700_000_000_123))
    assert int(recovered.timestamp() * 1000) == 1_700_000_000_123


def test_confusable_transcription_resolves():
    """An O copied for a 0, or an l for a 1, still finds the same run."""
    run_id = new_run_id(timestamp_ms=1_700_000_000_000)
    mangled = run_id.lower().replace("0", "o").replace("1", "l")
    assert normalize_run_id(mangled) == run_id


def test_uniqueness_within_one_millisecond():
    """80 bits of randomness per millisecond."""
    ids = {new_run_id(timestamp_ms=1_700_000_000_000) for _ in range(2_000)}
    assert len(ids) == 2_000


@pytest.mark.parametrize("bad", ["", "TOO-SHORT", "!" * 26, 12345])
def test_invalid_ids_rejected(bad):
    with pytest.raises(InvalidRunId):
        parse_run_id(bad)


def test_overflow_rejected():
    """26 base-32 chars carry 130 bits; a ULID uses only the low 128."""
    with pytest.raises(InvalidRunId, match="overflows"):
        parse_run_id("Z" * 26)
