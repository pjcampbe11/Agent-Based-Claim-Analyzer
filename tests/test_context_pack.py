"""The institutional context pack (doc 19).

The component's whole safety argument is one sentence: **it is a source, not a
generation**. These tests are the mechanical form of that sentence -- the corpus
is data, its citations are real and quote-verified, selection is deterministic,
and nothing in it may editorialise.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from abca.context_pack.corpus import CORPUS_PATH, CorpusError, load_corpus
from abca.context_pack.entry import DOMAINS, ContextEntry, Volatility, evaluative_language
from abca.context_pack.select import (
    PATTERN_DOMAINS,
    SUBTYPE_DOMAINS,
    select_entries,
)
from abca.schema.enums import ClaimType, SourceTier
from abca.schema.sourceless import (
    AtomicClaim,
    ClaimSubtype,
    DomainFailurePattern,
    ReferentCandidate,
    ReferentConfidence,
    SourcelessAnalysis,
    Verifiability,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pack():
    return load_corpus()


# ==========================================================================
# The corpus is data, and it is real
# ==========================================================================


class TestCorpus:
    def test_it_loads_and_is_not_empty(self, pack):
        assert pack.entries
        assert pack.version.startswith("corpus/institutional/")

    def test_the_hash_pins_the_content_not_the_formatting(self, tmp_path):
        """Reformatting must not invalidate a published brief; rewording must."""
        raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

        reformatted = tmp_path / "reformatted.json"
        reformatted.write_text(json.dumps(raw, indent=8), encoding="utf-8")
        assert load_corpus(reformatted).content_hash == load_corpus().content_hash

        reworded = tmp_path / "reworded.json"
        raw["entries"][0]["statement"] += " And one more clause."
        reworded.write_text(json.dumps(raw), encoding="utf-8")
        assert load_corpus(reworded).content_hash != load_corpus().content_hash

    def test_every_citation_is_t0_or_t1(self, pack):
        """The pack is admissible evidence for what it says, so no reporting."""
        for entry in pack.entries:
            assert entry.citation.tier.rank <= SourceTier.T1.rank, entry.id

    def test_every_entry_carries_a_verbatim_quote(self, pack):
        for entry in pack.entries:
            assert len(entry.citation.quote.strip()) >= 12, entry.id

    def test_every_entry_carries_a_content_hash(self, pack):
        """Without it, drift cannot be detected."""
        for entry in pack.entries:
            assert entry.citation.content_hash.startswith("sha256:"), entry.id

    def test_ids_are_unique(self, pack):
        ids = [e.id for e in pack.entries]
        assert len(ids) == len(set(ids))

    def test_a_dangling_contrast_reference_fails_to_load(self, tmp_path):
        """Telling a reader to compare against something absent is a broken brief."""
        raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        raw["entries"][0]["distinguish_from"] = ["procedure.does_not_exist"]
        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CorpusError, match="unknown entries"):
            load_corpus(broken)

    def test_duplicate_ids_fail_to_load(self, tmp_path):
        raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        raw["entries"].append(raw["entries"][0])
        dupe = tmp_path / "dupe.json"
        dupe.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CorpusError, match="duplicate entry ids"):
            load_corpus(dupe)

    def test_a_missing_corpus_raises_rather_than_returning_empty(self, tmp_path):
        """An empty pack would silently thin every brief and show up in no test."""
        with pytest.raises(CorpusError, match="packaging failure"):
            load_corpus(tmp_path / "absent.json")

    def test_malformed_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(CorpusError, match="not valid JSON"):
            load_corpus(bad)


# ==========================================================================
# No conclusions
# ==========================================================================


class TestNoConclusions:
    def test_descriptive_prose_passes(self):
        assert evaluative_language(
            "A political committee receives contributions over $1,000 in a year."
        ) == []

    @pytest.mark.parametrize("phrase,expected", [
        ("this loophole is abused", "loophole"),
        ("the chamber should not do this", "should"),
        ("an obviously cynical maneuver", "obviously"),
        ("a corrupt practice", "corrupt"),
    ])
    def test_evaluative_prose_is_caught(self, phrase, expected):
        assert expected in evaluative_language(phrase)

    def test_no_shipped_entry_editorialises(self, pack):
        for entry in pack.entries:
            for field in ("statement", "plain_language", "common_distortion"):
                assert evaluative_language(getattr(entry, field)) == [], (
                    f"{entry.id}.{field}"
                )

    def test_an_evaluative_entry_is_refused_at_construction(self, pack):
        payload = pack.entries[0].to_jsonable()
        payload["statement"] = "This mechanism is a loophole."
        with pytest.raises(ValueError, match="evaluative language"):
            ContextEntry.model_validate(payload)

    def test_common_distortion_never_names_who_does_it(self, pack):
        """Naming an actor would make the corpus an opinion wearing a citation."""
        for entry in pack.entries:
            low = entry.common_distortion.casefold()
            for actor in ("republican", "democrat", "gop", "the left", "the right"):
                assert actor not in low, entry.id


# ==========================================================================
# Volatility
# ==========================================================================


class TestVolatility:
    def test_review_must_follow_validity(self):
        with pytest.raises(ValueError, match="review_due must be after"):
            Volatility(rate="low", valid_as_of=date(2026, 1, 1),
                       review_due=date(2025, 1, 1))

    def test_staleness_is_computed_against_a_date(self):
        v = Volatility(rate="low", valid_as_of=date(2026, 1, 1),
                       review_due=date(2026, 6, 1))
        assert v.is_stale(today=date(2026, 7, 1))
        assert not v.is_stale(today=date(2026, 5, 1))

    def test_no_shipped_entry_is_stale_today(self, pack):
        assert pack.stale() == ()

    def test_a_stale_entry_is_served_with_a_warning_not_withheld(self, pack):
        """Withholding it silently makes the brief worse; flagging it does not."""
        future = datetime.now(UTC).date() + timedelta(days=400 * 10)
        assert len(pack.stale(today=future)) == len(pack.entries)


# ==========================================================================
# Selection is deterministic
# ==========================================================================


def analysis_with(**kw) -> SourcelessAnalysis:
    base = dict(denatured_claim="A claim about a federal committee threshold.")
    base.update(kw)
    return SourcelessAnalysis(**base)


class TestSelection:
    def test_every_subtype_has_an_entry_in_the_table(self):
        """A new subtype must be routed deliberately, not defaulted to nothing."""
        assert set(SUBTYPE_DOMAINS) == set(ClaimSubtype)

    def test_every_mapped_domain_is_a_real_domain(self):
        for domains in SUBTYPE_DOMAINS.values():
            assert set(domains) <= DOMAINS
        for _, domains in PATTERN_DOMAINS:
            assert set(domains) <= DOMAINS

    def test_selection_is_stable_across_runs(self, pack):
        a = analysis_with(
            atomic_claims=(AtomicClaim(
                id="a-001", text="x", claim_type=ClaimType.LEGAL,
                subtype=ClaimSubtype.STATUTORY_CONTENT,
                verifiability=Verifiability.VERIFIABLE_PRIMARY, load_bearing=True),),
        )
        first = [s.entry.id for s in select_entries(a, pack)]
        second = [s.entry.id for s in select_entries(a, pack)]
        assert first == second

    def test_an_empty_analysis_selects_nothing(self, pack):
        assert select_entries(analysis_with(), pack) == []

    def test_a_subtype_pulls_its_domains(self, pack):
        a = analysis_with(atomic_claims=(AtomicClaim(
            id="a-001", text="x", claim_type=ClaimType.LEGAL,
            subtype=ClaimSubtype.STATUTORY_CONTENT,
            verifiability=Verifiability.VERIFIABLE_PRIMARY, load_bearing=True),))
        selected = select_entries(a, pack)
        assert selected
        assert {s.entry.domain for s in selected} <= {"lawmaking", "instruments"}

    def test_a_distortion_pulls_its_domain(self, pack):
        a = analysis_with(referent_candidates=(ReferentCandidate(
            candidate="a rule", referent_confidence=ReferentConfidence.LOW,
            distortion_applied="proposed_to_enacted_rule", reasoning="r"),))
        assert any(s.entry.domain == "instruments" for s in select_entries(a, pack))

    def test_a_failure_pattern_pulls_its_domain(self, pack):
        a = analysis_with(domain_failure_patterns=(DomainFailurePattern(
            pattern="contribution limit described as annual", span="s",
            explanation="e"),))
        assert any(s.entry.domain == "elections" for s in select_entries(a, pack))

    def test_contrast_pairs_ride_along(self, pack):
        """An entry without its contrast is the half that does not resolve it."""
        a = analysis_with(referent_candidates=(ReferentCandidate(
            candidate="a rule", referent_confidence=ReferentConfidence.LOW,
            distortion_applied="proposed_to_enacted_rule", reasoning="r"),))
        selected = select_entries(a, pack, limit=20)
        ids = {s.entry.id for s in selected}
        for s in selected:
            for ref in s.entry.distinguish_from:
                assert ref in ids, f"{s.entry.id} lost its contrast {ref}"

    def test_every_selection_records_why(self, pack):
        a = analysis_with(referent_candidates=(ReferentCandidate(
            candidate="a rule", referent_confidence=ReferentConfidence.LOW,
            distortion_applied="proposed_to_enacted_rule", reasoning="r"),))
        for s in select_entries(a, pack):
            assert s.selected_by
            assert s.relevance().startswith("selected by")

    def test_the_limit_is_respected(self, pack):
        a = analysis_with(
            atomic_claims=(AtomicClaim(
                id="a-001", text="x", claim_type=ClaimType.LEGAL,
                subtype=ClaimSubtype.STATUTORY_CONTENT,
                verifiability=Verifiability.VERIFIABLE_PRIMARY, load_bearing=True),),
            referent_candidates=(ReferentCandidate(
                candidate="c", referent_confidence=ReferentConfidence.LOW,
                distortion_applied="title_to_text", reasoning="r"),),
        )
        assert len(select_entries(a, pack, limit=2)) <= 2

    def test_a_motive_subtype_selects_nothing(self, pack):
        """Motive claims are not fact-checked, so there is no mechanism to explain."""
        a = analysis_with(
            atomic_claims=(AtomicClaim(
                id="a-001", text="x", claim_type=ClaimType.LEGAL,
                subtype=ClaimSubtype.MOTIVE,
                verifiability=Verifiability.UNFALSIFIABLE, load_bearing=True),),
            disposition=__import__("abca.schema.enums", fromlist=["Verdict"]).Verdict.UNVERIFIABLE,
        )
        assert select_entries(a, pack) == []


# ==========================================================================
# Isolation — the corpus is data, like evals/
# ==========================================================================


class TestIsolation:
    def test_the_corpus_lives_outside_src(self):
        assert "src" not in CORPUS_PATH.parts, (
            "the corpus is data the analyzer is pointed at, like evals/"
        )
        assert CORPUS_PATH.is_file()

    def test_src_never_hardcodes_a_path_into_the_corpus(self):
        """Only corpus.py may name the directory, and only to locate it."""
        offenders = []
        for path in (REPO_ROOT / "src" / "abca").rglob("*.py"):
            if path.name == "corpus.py":
                continue
            text = path.read_text(encoding="utf-8")
            for needle in ('"corpus/', "'corpus/", 'Path("corpus")'):
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
        assert offenders == [], offenders

    def test_the_prompts_never_mention_a_corpus_entry(self):
        """A prompt that knows an entry's text would be generating, not selecting."""
        from abca.prompts import PROMPTS_ROOT

        blob = " ".join(p.read_text(encoding="utf-8").casefold()
                        for p in PROMPTS_ROOT.rglob("*.md"))
        for entry in load_corpus().entries:
            assert entry.statement.casefold()[:60] not in blob, entry.id

    def test_the_integrity_script_passes_offline(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/check_context_pack.py"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
