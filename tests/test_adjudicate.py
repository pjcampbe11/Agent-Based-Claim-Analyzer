"""Adjudication, citation verification, retrieval, and replay.

The property under test throughout: **a model cannot produce a citation.** It
produces a pointer and a quote, and both are checked against the retrieved
source before anything becomes evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from abca.config import Config, ModelSpec
from abca.ledger.inputs import InputStore
from abca.pipeline.adjudicate import (
    build_adjudicate_prompt,
    render_sources,
    run_adjudicate,
    verify_citations,
)
from abca.pipeline.models import (
    Adjudication,
    CitationRef,
    DraftClaim,
)
from abca.pipeline.orchestrator import AnalyzeOptions, analyze_text, to_published_claim
from abca.pipeline.replay import (
    PipelineReplayer,
    ReplayImpossible,
    check_prompt_pins,
    make_source_probe,
    resolve_input,
)
from abca.pipeline.retrieve import candidate_locators, run_retrieve
from abca.providers.base import Completion, GenerationRequest, ProviderUnavailable
from abca.providers.registry import build_registry
from abca.providers.transport import FakeTransport
from abca.schema.enums import ClaimType, SourceTier, Verdict, VerifyOutcome
from abca.schema.ledger import ModelIdentity
from abca.sources.base import build_document
from abca.sources.cache import SourceCache
from abca.sources.ilcs import ILCSConnector

STATUTE = """(10 ILCS 5/10-2) (from Ch. 46, par. 10-2)

Sec. 10-2.
Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled.
"""

REAL_QUOTE = (
    "signed by 1% of the number of voters who voted at the next preceding "
    "Statewide general election or 25,000 qualified voters, whichever is less"
)
FABRICATED_QUOTE = "signed by exactly 25,000 qualified voters in every case"

IDENTITY = ModelIdentity(role="adjudicator", provider="ollama", name="m",
                         weights_hash="sha256:" + "a" * 64)


@pytest.fixture
def cache(tmp_path):
    return SourceCache(tmp_path / "sources")


@pytest.fixture
def connectors(cache):
    """An offline ILCS connector seeded with real statute text."""
    connector = ILCSConnector(cache=cache, offline=True)
    cache.put(build_document(
        connector=connector, doc_id="s-001", title="10 ILCS 5/10-2",
        url="https://www.ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm",
        text=STATUTE, locator="10 ILCS 5/10-2",
        retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
    ))
    return {"ilcs": connector}


@pytest.fixture
def document(connectors):
    return connectors["ilcs"].fetch("10 ILCS 5/10-2")


class ScriptedProvider:
    name = "scripted"

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = 0
        self.prompts: list[str] = []

    def identity(self):
        return IDENTITY

    def generate(self, request: GenerationRequest) -> Completion:
        self.prompts.append(request.prompt)
        item = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return Completion(text=json.dumps(item), identity=IDENTITY,
                          prompt_tokens=200, completion_tokens=80, duration_ms=100)

    def embed(self, texts):  # pragma: no cover
        return []

    def health(self) -> None:
        return None


def adjudication(quote: str, *, verdict=Verdict.SUPPORTED, source_id="s-001"):
    return Adjudication(
        claim_id="c-001", verdict=verdict, confidence=0.9, reasoning="because",
        citations=[CitationRef(source_id=source_id, quote=quote)],
    )


# ==========================================================================
# Citation verification
# ==========================================================================


class TestCitationVerification:
    def test_real_quote_becomes_a_citation(self, document):
        verified, rejected = verify_citations(adjudication(REAL_QUOTE), {document.id: document})
        assert len(verified) == 1
        assert not rejected
        assert verified[0].tier is SourceTier.T0

    def test_fabricated_quote_is_discarded(self, document):
        verified, rejected = verify_citations(
            adjudication(FABRICATED_QUOTE), {document.id: document})
        assert verified == []
        assert "not found" in rejected[0]

    def test_one_altered_number_is_discarded(self, document):
        """Plausible, nearly identical, legally opposite. The dangerous case."""
        altered = REAL_QUOTE.replace("1%", "5%")
        verified, _ = verify_citations(adjudication(altered), {document.id: document})
        assert verified == []

    def test_unknown_source_id_is_discarded(self, document):
        verified, rejected = verify_citations(
            adjudication(REAL_QUOTE, source_id="s-999"), {document.id: document})
        assert verified == []
        assert "unknown source" in rejected[0]

    def test_stored_quote_is_the_sources_wording(self, document):
        """A citation reproduces the source, not the model's reflow of it."""
        verified, _ = verify_citations(adjudication(REAL_QUOTE), {document.id: document})
        assert verified[0].quote in document.text

    def test_citation_carries_the_documents_hash_and_time(self, document):
        verified, _ = verify_citations(adjudication(REAL_QUOTE), {document.id: document})
        assert verified[0].content_hash == document.content_hash
        assert verified[0].retrieved_at == document.retrieved_at

    def test_model_cannot_supply_a_tier(self):
        """The structural guarantee: there is no field to put one in."""
        assert "tier" not in CitationRef.model_fields
        assert "url" not in CitationRef.model_fields
        assert "content_hash" not in CitationRef.model_fields


# ==========================================================================
# Verdict downgrade
# ==========================================================================


class TestVerdictDowngrade:
    def _run(self, payload, claims, retrieval):
        provider = ScriptedProvider([payload])
        return run_adjudicate(provider, claims, retrieval, max_attempts=1)

    @pytest.fixture
    def setup(self, connectors):
        claims = [DraftClaim(
            id="c-001", text="Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures.",
            sentence_index=0, claim_type=ClaimType.LEGAL)]
        retrieval = run_retrieve(claims, connectors).value
        return claims, retrieval

    def test_verified_citation_keeps_the_verdict(self, setup):
        claims, retrieval = setup
        payload = {"adjudications": [{
            "claim_id": "c-001", "verdict": "MIXED", "confidence": 0.88,
            "reasoning": "the statute sets a formula",
            "citations": [{"source_id": "s-001", "quote": REAL_QUOTE, "locator": None}]}]}
        record = self._run(payload, claims, retrieval).value["c-001"]
        assert record.verdict is Verdict.MIXED
        assert len(record.citations) == 1

    def test_fabricated_citation_downgrades_the_verdict(self, setup):
        """The model does not get to keep the verdict and lose the evidence."""
        claims, retrieval = setup
        payload = {"adjudications": [{
            "claim_id": "c-001", "verdict": "SUPPORTED", "confidence": 0.95,
            "reasoning": "the statute plainly says so",
            "citations": [{"source_id": "s-001", "quote": FABRICATED_QUOTE,
                           "locator": None}]}]}
        record = self._run(payload, claims, retrieval).value["c-001"]
        assert record.verdict is Verdict.UNSUPPORTED
        assert record.confidence == 0.0
        assert record.citations == []

    def test_downgrade_is_visible_in_the_reasoning(self, setup):
        """A silently rejected claim would just vanish from the reader's view."""
        claims, retrieval = setup
        payload = {"adjudications": [{
            "claim_id": "c-001", "verdict": "SUPPORTED", "confidence": 0.95,
            "reasoning": "the statute plainly says so",
            "citations": [{"source_id": "s-001", "quote": FABRICATED_QUOTE,
                           "locator": None}]}]}
        record = self._run(payload, claims, retrieval).value["c-001"]
        assert "downgraded from SUPPORTED" in record.reasoning

    def test_unsupported_needs_no_citation(self, setup):
        """The correct answer whenever the sources do not reach the claim."""
        claims, retrieval = setup
        payload = {"adjudications": [{
            "claim_id": "c-001", "verdict": "UNSUPPORTED", "confidence": 0.4,
            "reasoning": "the section does not address this", "citations": []}]}
        record = self._run(payload, claims, retrieval).value["c-001"]
        assert record.verdict is Verdict.UNSUPPORTED

    def test_adjudication_failure_leaves_the_claim_unadjudicated(self, setup):
        """Fail toward NOT adjudicating. Inventing a verdict is the worst outcome."""
        claims, retrieval = setup
        provider = ScriptedProvider([ProviderUnavailable("down", provider="x")])
        outcome = run_adjudicate(provider, claims, retrieval, max_attempts=1)
        assert outcome.value == {}
        assert any("remain UNSUPPORTED" in note for note in outcome.notes)


# ==========================================================================
# Scope of adjudication
# ==========================================================================


class TestAdjudicationScope:
    def test_non_eligible_types_are_never_sent(self, connectors):
        claims = [DraftClaim(id="c-001", text="That is unfair.", sentence_index=0,
                             claim_type=ClaimType.NORMATIVE)]
        provider = ScriptedProvider([{"adjudications": []}])
        run_adjudicate(provider, claims, run_retrieve(claims, connectors).value)
        assert provider.calls == 0

    def test_claims_with_no_source_are_not_sent(self, connectors):
        """Sending them wastes a call AND invites invented citations."""
        claims = [DraftClaim(id="c-001", text="The economy grew last year.",
                             sentence_index=0, claim_type=ClaimType.EMPIRICAL)]
        provider = ScriptedProvider([{"adjudications": []}])
        outcome = run_adjudicate(provider, claims, run_retrieve(claims, connectors).value)
        assert provider.calls == 0
        assert any("no source to check against" in note for note in outcome.notes)

    def test_unresolved_citation_still_gets_adjudicated(self, connectors):
        """"You cited a section that does not exist" is a finding worth stating."""
        claims = [DraftClaim(id="c-001", text="See 10 ILCS 5/99-999 for the rule.",
                             sentence_index=0, claim_type=ClaimType.LEGAL)]
        retrieval = run_retrieve(claims, connectors).value
        assert retrieval.unresolved["c-001"] == ["10 ILCS 5/99-999"]
        provider = ScriptedProvider([{"adjudications": [{
            "claim_id": "c-001", "verdict": "CONTRADICTED", "confidence": 0.8,
            "reasoning": "the cited section does not exist", "citations": []}]}])
        run_adjudicate(provider, claims, retrieval, max_attempts=1)
        assert provider.calls == 1

    def test_unresolved_citation_is_surfaced_in_the_prompt(self, connectors):
        claims = [DraftClaim(id="c-001", text="See 10 ILCS 5/99-999.", sentence_index=0,
                             claim_type=ClaimType.LEGAL)]
        retrieval = run_retrieve(claims, connectors).value
        prompt = build_adjudicate_prompt("PROMPT", claims, [], retrieval.unresolved)
        assert "could not be retrieved" in prompt

    def test_sources_are_rendered_with_ids_and_tiers(self, document):
        rendered = render_sources([document])
        assert "[s-001]" in rendered
        assert "tier T0" in rendered


# ==========================================================================
# Retrieval
# ==========================================================================


class TestRetrieval:
    def test_extracts_citations_from_claim_text(self):
        claim = DraftClaim(id="c-1", text="Under 10 ILCS 5/10-2 the rule applies.",
                           sentence_index=0, claim_type=ClaimType.LEGAL)
        assert candidate_locators(claim) == ["10 ILCS 5/10-2"]

    def test_deduplicates_across_claims(self, connectors):
        claims = [
            DraftClaim(id=f"c-{i}", text="Under 10 ILCS 5/10-2 the rule applies.",
                       sentence_index=i, claim_type=ClaimType.LEGAL)
            for i in range(5)
        ]
        result = run_retrieve(claims, connectors).value
        assert len(result.documents) == 1
        assert len(result.by_claim) == 5

    def test_gated_claims_are_skipped(self, connectors):
        claims = [DraftClaim(id="c-1", text="Under 10 ILCS 5/10-2 x.", sentence_index=0,
                             claim_type=ClaimType.LEGAL, gated_out=True)]
        assert run_retrieve(claims, connectors).value.documents == []

    def test_run_limit_truncates_and_says_so(self, connectors):
        claims = [DraftClaim(id="c-1", text="Under 10 ILCS 5/10-2 x.", sentence_index=0,
                             claim_type=ClaimType.LEGAL)]
        outcome = run_retrieve(claims, connectors, max_per_run=0)
        assert any("stopped at 0 documents" in note for note in outcome.notes)

    def test_retrieval_is_deterministic(self, connectors):
        """The set of sources consulted is part of the recipe."""
        claims = [DraftClaim(id="c-1", text="Under 10 ILCS 5/10-2 x.", sentence_index=0,
                             claim_type=ClaimType.LEGAL)]
        first = run_retrieve(claims, connectors).value
        second = run_retrieve(claims, connectors).value
        assert [d.content_hash for d in first.documents] == \
               [d.content_hash for d in second.documents]


# ==========================================================================
# Published claim assembly
# ==========================================================================


class TestPublishedClaim:
    def test_evidence_quality_is_computed_from_citations(self, document):
        from abca.pipeline.adjudicate import AdjudicationRecord

        citation = document.to_citation(document.locate(REAL_QUOTE))
        record = AdjudicationRecord(
            claim_id="c-001", verdict=Verdict.MIXED, confidence=0.9,
            reasoning="x", citations=[citation])
        claim = to_published_claim(
            DraftClaim(id="c-001", text="x", sentence_index=0, claim_type=ClaimType.LEGAL),
            record)
        assert claim.evidence_quality is SourceTier.T0

    def test_unchecked_claim_says_nobody_looked(self):
        claim = to_published_claim(
            DraftClaim(id="c-1", text="x", sentence_index=0, claim_type=ClaimType.EMPIRICAL),
            None, had_connector=False)
        assert claim.verdict is Verdict.UNSUPPORTED
        assert "No source was consulted" in claim.reasoning

    def test_checked_but_unestablished_reads_differently(self):
        """The distinction a reader must be able to make."""
        claim = to_published_claim(
            DraftClaim(id="c-1", text="x", sentence_index=0, claim_type=ClaimType.LEGAL),
            None, had_connector=True)
        assert "No source was consulted" not in claim.reasoning


# ==========================================================================
# Replay
# ==========================================================================


GATE = {"decisions": [{"sentence_index": 0, "in_scope": True, "reason": "statute"}]}
SEGMENT = {"claims": [{
    "text": "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures.",
    "sentence_index": 0, "verbatim_span": None, "ambiguous_stance": False}]}
CLASSIFY = {"classifications": [{
    "claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.9,
    "reasoning": "settled by reading the statute"}]}
ADJUDICATE = {"adjudications": [{
    "claim_id": "c-001", "verdict": "MIXED", "confidence": 0.88,
    "reasoning": "the statute sets a formula, not a flat number",
    "citations": [{"source_id": "s-001", "quote": REAL_QUOTE, "locator": "10 ILCS 5/10-2"}]}]}

TEXT = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures."


def ollama_transport():
    def gen(payload):
        return {"response": json.dumps(payload), "prompt_eval_count": 400,
                "eval_count": 90, "total_duration": 1_000_000_000, "done_reason": "stop"}
    return FakeTransport({
        "/api/version": {"version": "0.6.2"},
        "/api/tags": {"models": [{"name": "m", "digest": "a" * 64,
                                  "details": {"quantization_level": "Q5_K_M"}}]},
        "/api/show": {"details": {"quantization_level": "Q5_K_M"},
                      "model_info": {"qwen2.context_length": 32768}},
        "/api/generate": [gen(GATE), gen(SEGMENT), gen(CLASSIFY), gen(ADJUDICATE)],
    })


def make_registry():
    transport = ollama_transport()
    config = Config(models={
        "classifier": ModelSpec(role="classifier", provider="ollama", model="m"),
        "adjudicator": ModelSpec(role="adjudicator", provider="ollama", model="m"),
    })
    roles = ["classifier", "segmenter", "adjudicator"]
    return config, build_registry(config, roles=roles, transports=dict.fromkeys(roles, transport))


class TestReplay:
    @pytest.fixture
    def analyzed(self, connectors):
        _, registry = make_registry()
        return analyze_text(TEXT, registry, AnalyzeOptions(offline=True),
                            connectors=connectors).record

    def test_adjudicated_run_carries_a_verified_citation(self, analyzed):
        claim = analyzed.result.claims[0]
        assert claim.verdict is Verdict.MIXED
        assert claim.citations[0].tier is SourceTier.T0
        assert analyzed.sources[0].connector == "ilcs"

    def test_input_hash_must_match(self, analyzed):
        """A replay against different text is not a replay."""
        with pytest.raises(ReplayImpossible, match="does not match"):
            resolve_input(analyzed, supplied="something else entirely")

    def test_supplying_the_right_text_resolves(self, analyzed):
        assert resolve_input(analyzed, supplied=TEXT)

    def test_missing_input_is_refused_not_guessed(self, analyzed, tmp_path):
        with pytest.raises(ReplayImpossible, match="not available locally"):
            resolve_input(analyzed, store=InputStore(tmp_path))

    def test_input_store_round_trip(self, analyzed, tmp_path):
        from abca.pipeline.ingest import normalize_text

        store = InputStore(tmp_path)
        store.put(normalize_text(TEXT)[0])
        assert resolve_input(analyzed, store=store) == normalize_text(TEXT)[0]

    def test_edited_cached_input_is_rejected(self, tmp_path):
        """A substituted input would let a replay claim to reproduce a run it did not."""
        store = InputStore(tmp_path)
        content_hash = store.put("original text")
        path = store._path(content_hash)
        path.write_text("substituted text", encoding="utf-8")
        assert store.get(content_hash) is None

    def test_prompt_pins_pass_on_an_unmodified_tree(self, analyzed):
        check_prompt_pins(analyzed)

    def test_edited_prompt_blocks_replay(self, analyzed):
        """The quietest possible divergence: same recipe on its face, different question."""
        tampered = analyzed.config.prompts[0].model_copy(
            update={"content_hash": "sha256:" + "9" * 64})
        mutated = analyzed.model_copy(update={
            "config": analyzed.config.model_copy(
                update={"prompts": [tampered, *analyzed.config.prompts[1:]]})})
        with pytest.raises(ReplayImpossible, match="prompt files differ"):
            check_prompt_pins(mutated)

    def test_full_replay_is_equivalent(self, analyzed, connectors):
        """IDENTICAL is unreachable across time -- the ingest timestamp differs --
        so EQUIVALENT is the practical success outcome for a real replay."""
        from abca.ledger.verify import compare

        config, registry = make_registry()
        replayer = PipelineReplayer(config=config, supplied_input=TEXT,
                                    connectors=connectors, offline=True)
        # Inject the already-built registry so the fake transport is used.
        replayer_registry = registry

        from abca.pipeline.replay import build_replay_registry  # noqa: F401

        replayed = analyze_text(TEXT, replayer_registry, AnalyzeOptions(offline=True),
                                connectors=connectors).record
        report = compare(analyzed, replayed)
        assert report.outcome in {VerifyOutcome.IDENTICAL, VerifyOutcome.EQUIVALENT}
        assert report.ok
        _ = replayer

    def test_hosted_run_cannot_be_replayed(self, connectors):
        """No weights hash means no third party can confirm which model ran."""
        from abca.canonical import digest_text
        from abca.ledger.recorder import RunRecorder
        from abca.pipeline.replay import build_replay_registry
        from abca.schema.core import AnalysisResult, DocumentRef
        from abca.schema.enums import InputKind, Profile
        from abca.schema.ledger import RunConfig

        document = DocumentRef(kind=InputKind.TEXT, locator="<inline>",
                               content_hash=digest_text("x"),
                               retrieved_at=datetime.now(UTC), byte_length=1)
        config = RunConfig(
            profile=Profile.STANDARD, seed=42, temperature=0.0, max_claims=10,
            reading_level=8,
            models=[ModelIdentity(role="adjudicator", provider="api:anthropic",
                                  name="claude-x")])
        record = RunRecorder(document=document, config=config).finalize(
            AnalysisResult(document=document))
        with pytest.raises(ReplayImpossible, match="cannot be pinned"):
            build_replay_registry(record)


class TestILCSFilenameDecoding:
    """The URL -> locator inverse, needed for drift probing."""

    #: The URL decoding moved onto the connector itself, as
    #: ``locator_for_url``. The drift probe used to ``isinstance``-check the one
    #: connector that existed, which meant every connector added afterwards was
    #: silently un-probeable and its drift would have gone unreported while
    #: ``verify`` still said the run was fine.
    BASE = "https://www.ilga.gov/documents/legislation/ilcs/documents/"

    @pytest.mark.parametrize(
        ("filename", "citation"),
        [
            ("001000050K10-2", "10 ILCS 5/10-2"),
            ("000501400K1", "5 ILCS 140/1"),
            ("002039600K4.5", "20 ILCS 3960/4.5"),
        ],
    )
    def test_round_trip(self, filename, citation, tmp_path):
        connector = ILCSConnector(cache=SourceCache(tmp_path), offline=True)
        assert connector.locator_for_url(f"{self.BASE}{filename}.htm") == citation

    @pytest.mark.parametrize("bad", ["", "nonsense", "K10-2", "0010"])
    def test_malformed_filenames_return_none(self, bad, tmp_path):
        connector = ILCSConnector(cache=SourceCache(tmp_path), offline=True)
        assert connector.locator_for_url(f"{self.BASE}{bad}.htm") is None

    def test_a_foreign_url_belongs_to_no_connector(self, tmp_path):
        """The probe asks every connector; none may claim a URL it did not make."""
        connectors = {
            "ilcs": ILCSConnector(cache=SourceCache(tmp_path), offline=True),
        }
        assert make_source_probe(connectors)("https://example.com/x") is None


class TestReplayRegistry:
    """Rebuilding providers: connection details local, model identity from the record."""

    def _record(self, connectors):
        _, registry = make_registry()
        return analyze_text(TEXT, registry, AnalyzeOptions(offline=True),
                            connectors=connectors).record

    def test_weights_mismatch_is_refused(self, connectors, monkeypatch):
        """'Reproducible' must mean the machine actually HAS that model.

        A replay that quietly ran a different build of the same tag would
        produce a comparison that means nothing while looking authoritative.
        """
        from abca.pipeline.replay import build_replay_registry

        record = self._record(connectors)
        different = FakeTransport({
            "/api/version": {"version": "0.6.2"},
            "/api/tags": {"models": [{"name": "m", "digest": "b" * 64,
                                      "details": {"quantization_level": "Q5_K_M"}}]},
            "/api/show": {"details": {}, "model_info": {}},
        })

        import abca.pipeline.replay as replay_module

        real_build = replay_module.build_registry

        def patched(config, *, roles=None, transports=None):
            return real_build(config, roles=roles,
                              transports=dict.fromkeys(roles or [], different))

        monkeypatch.setattr(replay_module, "build_registry", patched)
        with pytest.raises(ReplayImpossible, match="different weights"):
            build_replay_registry(record)

    def test_connection_details_come_from_local_config(self, connectors):
        """The record stores WHICH model ran, not WHERE it lives.

        Baking a host into input_digest would make the same model on a laptop
        and on EC2 falsely divergent, and would put internal hostnames into
        published records.
        """
        record = self._record(connectors)
        assert all(not m.provider.startswith("api:") for m in record.config.models)
        # base_url is nowhere in the recipe.
        assert "base_url" not in json.dumps(record.replay_projection())

    def test_source_probe_decodes_a_recorded_url(self, connectors):
        """Drift probing has to turn a stored URL back into a connector locator."""
        from abca.pipeline.replay import make_source_probe

        record = self._record(connectors)
        probe = make_source_probe(connectors)
        # The offline connector has the document cached, so the probe resolves.
        assert probe(record.sources[0].url) == record.sources[0].content_hash

    def test_probe_returns_none_for_an_unknown_url(self, connectors):
        from abca.pipeline.replay import make_source_probe

        assert make_source_probe(connectors)("https://example.com/whatever") is None

    def test_drift_is_detected_when_a_source_changes(self, connectors):
        """A changed statute does not make the old verdict dishonest -- but it
        does mean a human needs to look again."""
        from abca.ledger.verify import compare

        record = self._record(connectors)
        _, registry = make_registry()
        replayed = analyze_text(TEXT, registry, AnalyzeOptions(offline=True),
                                connectors=connectors).record

        # Force a differing verdict so the comparison reaches the drift check.
        flipped = replayed.result.claims[0].model_copy(
            update={"verdict": Verdict.UNSUPPORTED, "confidence": 0.0,
                    "citations": [], "evidence_quality": None})
        mutated = replayed.model_copy(update={
            "result": replayed.result.model_copy(update={"claims": [flipped]})})
        # model_copy does not recompute digests, so they must be refreshed --
        # otherwise the stale semantic_digest still matches and the comparison
        # never reaches the drift check.
        mutated = mutated.model_copy(update={
            "semantic_digest": mutated.compute_semantic_digest(),
            "output_digest": mutated.compute_output_digest()})

        report = compare(record, mutated, probe=lambda url: "sha256:" + "9" * 64)
        assert report.outcome is VerifyOutcome.DRIFTED
        assert not report.ok  # drift is never a pass
