"""Recorder, hash chain, tamper detection and on-disk store."""

from __future__ import annotations

import json
from itertools import pairwise

import pytest

from abca.ids import new_run_id
from abca.ledger.recorder import RunRecorder
from abca.ledger.store import LedgerCorrupt, LedgerStore, RunNotFound, default_data_dir
from abca.schema.enums import Profile, StageName
from abca.schema.ledger import ModelIdentity, RunConfig


class TestRecorder:
    def test_finalized_record_audits_clean(self, record):
        assert record.audit() == []
        assert record.is_intact()

    def test_digests_are_recomputable(self, record):
        assert record.input_digest == record.compute_input_digest()
        assert record.semantic_digest == record.compute_semantic_digest()
        assert record.output_digest == record.compute_output_digest()
        assert record.ledger_hash == record.compute_ledger_hash()

    def test_stage_chain_is_linked(self, record):
        assert record.stages[0].prev_hash is None
        for earlier, later in pairwise(record.stages):
            assert later.prev_hash == earlier.record_hash

    def test_stage_sequence_is_contiguous(self, record):
        assert [s.sequence for s in record.stages] == list(range(len(record.stages)))

    def test_reproducible_flag_derived_from_pinnable_weights(self, record):
        assert record.reproducible is True

    def test_hosted_api_run_is_not_reproducible(self, document, environment, result):
        """No weights hash -> the recipe cannot be pinned -> the flag must say so."""
        config = RunConfig(
            profile=Profile.STANDARD, seed=42, temperature=0.0, max_claims=10,
            reading_level=8,
            models=[ModelIdentity(role="adjudicator", provider="api:vendor", name="hosted-x")],
        )
        rec = RunRecorder(document=document, config=config, environment=environment)
        out = rec.finalize(result)
        assert out.reproducible is False

    def test_failed_stage_is_still_recorded(self, recorder, result):
        """A run that dies halfway should leave evidence of how far it got."""
        with pytest.raises(RuntimeError), recorder.stage(StageName.INGEST) as stage:
            stage.set_input({"x": 1})
            raise RuntimeError("boom")

        record = recorder.finalize(result)
        assert len(record.stages) == 1
        assert any("boom" in note for note in record.stages[0].notes)

    def test_repeated_stage_timings_accumulate(self, recorder, result):
        """A retried stage should report total cost, not just the last attempt."""
        for _ in range(3):
            with recorder.stage(StageName.ADJUDICATE) as stage:
                stage.set_output({"attempt": True})
        record = recorder.finalize(result)
        assert record.timings_ms["adjudicate"] >= 0
        assert len(record.stages) == 3

    def test_source_recorded_twice_with_same_hash_is_fine(self, recorder, source_snapshot, result):
        recorder.record_source(source_snapshot)
        recorder.record_source(source_snapshot)
        record = recorder.finalize(result)
        assert len(record.sources) == 1

    def test_source_changing_mid_run_is_fatal(self, recorder, source_snapshot):
        """Recording either hash would make the ledger disagree with its citations."""
        changed = source_snapshot.model_copy(update={"content_hash": "sha256:" + "9" * 64})
        recorder.record_source(source_snapshot)
        with pytest.raises(ValueError, match="two different content"):
            recorder.record_source(changed)


class TestInputDigest:
    def test_seed_change_changes_input_digest(self, document, environment, config, result):
        """The recipe hash must move when the recipe moves."""
        a = RunRecorder(document=document, config=config, environment=environment).finalize(result)
        other = config.model_copy(update={"seed": 43})
        b = RunRecorder(document=document, config=other, environment=environment).finalize(result)
        assert a.input_digest != b.input_digest

    def test_model_list_order_does_not_matter(self, document, environment, config, result):
        """Ordering carries no meaning, so it must not change the hash."""
        a = RunRecorder(document=document, config=config, environment=environment).finalize(result)
        shuffled = config.model_copy(update={"models": list(reversed(config.models))})
        b = RunRecorder(document=document, config=shuffled, environment=environment).finalize(result)
        assert a.input_digest == b.input_digest

    def test_tool_version_is_not_in_the_input_digest(self, record, environment):
        """A docstring typo fix must not invalidate every published run hash."""
        bumped = record.model_copy(
            update={"environment": environment.model_copy(update={"tool_version": "9.9.9"})}
        )
        assert bumped.compute_input_digest() == record.input_digest

    def test_timings_are_not_in_any_digest(self, record):
        """Wall-clock is non-deterministic; hashing it would make replay impossible."""
        retimed = record.model_copy(update={"timings_ms": {"ingest": 99999}})
        assert retimed.compute_input_digest() == record.input_digest
        assert retimed.compute_output_digest() == record.output_digest
        assert retimed.compute_ledger_hash() == record.ledger_hash


class TestSemanticDigest:
    def test_prose_change_does_not_move_semantic_digest(self, record):
        """Same verdicts from the same citations = the same finding."""
        claim = record.result.claims[0]
        reworded = claim.model_copy(update={"reasoning": "entirely different prose"})
        result = record.result.model_copy(
            update={"claims": [reworded, *record.result.claims[1:]]}
        )
        mutated = record.model_copy(update={"result": result})
        assert mutated.compute_semantic_digest() == record.semantic_digest
        assert mutated.compute_output_digest() != record.output_digest

    def test_verdict_change_moves_semantic_digest(self, record):
        from abca.schema.enums import Verdict

        claim = record.result.claims[0]
        flipped = claim.model_copy(update={"verdict": Verdict.CONTRADICTED})
        result = record.result.model_copy(update={"claims": [flipped, *record.result.claims[1:]]})
        mutated = record.model_copy(update={"result": result})
        assert mutated.compute_semantic_digest() != record.semantic_digest

    def test_confidence_noise_below_two_decimals_is_ignored(self, record):
        """0.003 of self-reported confidence is noise, not disagreement."""
        claim = record.result.claims[0]
        nudged = claim.model_copy(update={"confidence": claim.confidence + 0.003})
        result = record.result.model_copy(update={"claims": [nudged, *record.result.claims[1:]]})
        mutated = record.model_copy(update={"result": result})
        assert mutated.compute_semantic_digest() == record.semantic_digest


class TestTamperDetection:
    def test_editing_a_verdict_breaks_the_audit(self, record):
        from abca.schema.enums import Verdict

        claim = record.result.claims[0]
        flipped = claim.model_copy(update={"verdict": Verdict.CONTRADICTED})
        result = record.result.model_copy(update={"claims": [flipped, *record.result.claims[1:]]})
        forged = record.model_copy(update={"result": result})

        problems = forged.audit()
        assert any("semantic_digest mismatch" in p for p in problems)
        assert any("output_digest mismatch" in p for p in problems)

    def test_naive_middle_stage_edit_breaks_its_own_record_hash(self, record):
        """Threat model 1: someone edits a stage's contents and stops there.

        The record no longer hashes to its stored value. The chain after it is
        untouched, so only one problem is reported -- which is enough.
        """
        edited = record.stages[1].model_copy(update={"output_hash": "sha256:" + "f" * 64})
        forged = record.model_copy(update={"stages": [record.stages[0], edited, record.stages[2]]})

        problems = forged.audit()
        assert any("record_hash" in p and "does not match its contents" in p for p in problems)

    def test_sophisticated_middle_stage_edit_breaks_the_following_link(self, record):
        """Threat model 2: someone edits a stage AND recomputes its record_hash.

        This is the attack a naive integrity check misses. The chain catches it
        anyway: the recomputed hash no longer equals what the NEXT stage's
        prev_hash points at, and re-linking that one shifts the problem forward
        until it reaches the published ledger_hash, which cannot be changed
        without changing the value that was published.
        """
        from abca.canonical import digest_chain

        stage = record.stages[1]
        edited_payload = {**stage.chain_payload(), "output_hash": "sha256:" + "f" * 64}
        edited = stage.model_copy(
            update={
                "output_hash": "sha256:" + "f" * 64,
                "record_hash": digest_chain(stage.prev_hash, edited_payload),
            }
        )
        forged = record.model_copy(update={"stages": [record.stages[0], edited, record.stages[2]]})

        problems = forged.audit()
        # The edited record is now self-consistent...
        assert edited.is_self_consistent()
        # ...but the stage after it points at a hash that no longer exists.
        assert any("prev_hash" in p for p in problems)

    def test_fully_relinked_forgery_breaks_the_published_ledger_hash(self, record):
        """Threat model 3: someone edits a stage and relinks the ENTIRE chain.

        This defeats both per-record and per-link checks -- the forged record
        is internally perfect. It still fails, because relinking necessarily
        changes the chain head, the head is committed to by ``ledger_hash``,
        and ``ledger_hash`` is the value published alongside the verdict. The
        forger cannot change a number that is already in someone else's hands.

        This is the case that makes publishing the hash the load-bearing act,
        not the hashing itself.
        """
        from abca.canonical import digest_chain

        rebuilt = []
        previous = None
        for index, stage in enumerate(record.stages):
            payload = stage.chain_payload()
            if index == 1:
                payload = {**payload, "output_hash": "sha256:" + "f" * 64}
            new_hash = digest_chain(previous, payload)
            rebuilt.append(
                stage.model_copy(
                    update={
                        "output_hash": payload["output_hash"],
                        "prev_hash": previous,
                        "record_hash": new_hash,
                    }
                )
            )
            previous = new_hash

        forged = record.model_copy(update={"stages": rebuilt})

        # Internally flawless: every link checks out.
        assert all(s.is_self_consistent() for s in forged.stages)
        problems = forged.audit()
        assert not any("prev_hash" in p for p in problems)

        # But the recomputed ledger hash no longer equals the published one.
        assert forged.compute_ledger_hash() != record.ledger_hash
        assert any("ledger_hash mismatch" in p for p in problems)

    def test_deleting_a_stage_breaks_sequence_validation(self, record):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="contiguous"):
            record.model_validate(
                {**record.to_jsonable(), "stages": [s.to_jsonable() for s in record.stages[:1]
                                                    + record.stages[2:]]}
            )

    def test_out_of_order_stages_rejected(self, record):
        from pydantic import ValidationError

        reordered = [record.stages[2], record.stages[1], record.stages[0]]
        payload = {**record.to_jsonable(), "stages": []}
        for index, stage in enumerate(reordered):
            payload["stages"].append({**stage.to_jsonable(), "sequence": index})
        with pytest.raises(ValidationError, match="canonical pipeline order"):
            record.model_validate(payload)

    def test_dishonest_reproducible_flag_rejected(self, document, environment, result):
        from pydantic import ValidationError

        config = RunConfig(
            profile=Profile.STANDARD, seed=42, temperature=0.0, max_claims=10, reading_level=8,
            models=[ModelIdentity(role="adjudicator", provider="api:v", name="hosted")],
        )
        rec = RunRecorder(document=document, config=config, environment=environment).finalize(result)
        with pytest.raises(ValidationError, match="cannot be pinned"):
            rec.model_validate({**rec.to_jsonable(), "reproducible": True})


class TestStore:
    def test_round_trip(self, tmp_path, record):
        store = LedgerStore(tmp_path)
        path = store.write(record)
        assert path.is_file()
        assert store.read(record.run_id).ledger_hash == record.ledger_hash

    def test_case_insensitive_lookup(self, tmp_path, record):
        store = LedgerStore(tmp_path)
        store.write(record)
        assert store.read(record.run_id.lower()).run_id == record.run_id

    def test_overwrite_refused_by_default(self, tmp_path, record):
        """Run records are evidence; silent replacement is how a result gets laundered."""
        store = LedgerStore(tmp_path)
        store.write(record)
        with pytest.raises(FileExistsError, match="append-only evidence"):
            store.write(record)
        store.write(record, overwrite=True)  # explicit opt-in works

    def test_missing_run(self, tmp_path):
        with pytest.raises(RunNotFound):
            LedgerStore(tmp_path).read(new_run_id())

    def test_tampered_file_rejected_on_read(self, tmp_path, record):
        """Edit the JSON on disk; the audit at read time must catch it."""
        store = LedgerStore(tmp_path)
        path = store.write(record)

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result"]["claims"][0]["verdict"] = "SUPPORTED"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(LedgerCorrupt, match="failed its integrity audit"):
            store.read(record.run_id)

        # ... but it can still be INSPECTED, which is what the audit command needs.
        damaged = store.read(record.run_id, audit=False)
        assert damaged.audit()

    def test_malformed_json_rejected(self, tmp_path, record):
        store = LedgerStore(tmp_path)
        path = store.write(record)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(LedgerCorrupt, match="not valid JSON"):
            store.read(record.run_id)

    def test_file_formatting_does_not_affect_digests(self, tmp_path, record):
        """Reformatting a stored file must not change its hashes.

        The digests are computed over canonical bytes, not over the file's
        indentation -- so a third party reproduces them without matching our
        pretty-printer.
        """
        store = LedgerStore(tmp_path)
        path = store.write(record)
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(payload, indent=8, sort_keys=False), encoding="utf-8")
        assert store.read(record.run_id).ledger_hash == record.ledger_hash

    def test_listing_is_newest_first(self, tmp_path, recorder, document, config, environment, result):
        store = LedgerStore(tmp_path)
        written = []
        for millis in (1_700_000_000_000, 1_700_000_001_000, 1_700_000_002_000):
            rec = RunRecorder(
                document=document, config=config, environment=environment,
                run_id=new_run_id(timestamp_ms=millis),
            ).finalize(result)
            store.write(rec)
            written.append(rec.run_id)
        assert store.list_ids() == list(reversed(written))

    def test_corrupt_record_does_not_break_listing(self, tmp_path, record):
        """One bad file must not make the other runs unlistable."""
        store = LedgerStore(tmp_path)
        store.write(record)
        (tmp_path / "01BROKEN0000000000000000AA.json").write_text("{", encoding="utf-8")
        assert [r.run_id for r in store.iter_records()] == [record.run_id]

    def test_no_temp_files_left_behind(self, tmp_path, record):
        store = LedgerStore(tmp_path)
        store.write(record)
        assert not list(tmp_path.glob(".*tmp"))


class TestDataDir:
    def test_windows_uses_localappdata(self, monkeypatch):
        """LOCALAPPDATA, not APPDATA: run records are machine-local, not roaming.

        Asserted with string containment rather than path semantics because
        this test runs on POSIX too, where a Windows path is just a string.
        """
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\pat\AppData\Local")
        resolved = str(default_data_dir())
        assert resolved.startswith(r"C:\Users\pat\AppData\Local")
        assert resolved.endswith("abca")

    def test_posix_honors_xdg(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", "/srv/data")
        assert default_data_dir().as_posix() == "/srv/data/abca"

    def test_posix_default(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert default_data_dir().as_posix().endswith(".local/share/abca")
