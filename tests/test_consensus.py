"""Consensus mode: reporting disagreement instead of averaging it away.

One property dominates: **a split panel must never produce a confident verdict.**
Running three models and publishing the majority answer looks more trustworthy
than any single model and is, in the case that matters, less so — it hides the
most useful fact the run produced, that models reading the same sources reached
different conclusions.
"""

from __future__ import annotations

import json

import pytest

from abca.config import Config, ConfigError, ModelSpec, load_config
from abca.pipeline.adjudicate import AdjudicationRecord
from abca.pipeline.consensus import (
    CONFLICT_VERDICT,
    PanelVerdict,
    measure_disagreement,
    merge,
    merge_citations,
    run_consensus,
    weakest,
)
from abca.pipeline.models import DraftClaim
from abca.pipeline.orchestrator import AnalyzeOptions, analyze_text
from abca.pipeline.red_team import VERDICT_STRENGTH
from abca.pipeline.retrieve import run_retrieve
from abca.providers.base import Completion, ProviderUnavailable
from abca.providers.registry import ProviderRegistry, RoleProvider, build_registry
from abca.schema.enums import ClaimType, SourceTier, StageName, Verdict
from abca.schema.ledger import ModelIdentity
from abca.sources.base import build_document
from abca.sources.cache import SourceCache
from abca.sources.ilcs import ILCSConnector

STATUTE = """(10 ILCS 5/10-2)

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
SLATE_QUOTE = (
    "shall at the time of filing contain a complete list of candidates of "
    "such party for all offices to be filled"
)


@pytest.fixture
def connectors(tmp_path):
    cache = SourceCache(tmp_path / "sources")
    connector = ILCSConnector(cache=cache, offline=True)
    cache.put(build_document(
        connector=connector, doc_id="s-001", title="10 ILCS 5/10-2",
        url="https://www.ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm",
        text=STATUTE, locator="10 ILCS 5/10-2",
    ))
    return {"ilcs": connector}


@pytest.fixture
def claims():
    return [DraftClaim(
        id="c-001",
        text="Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures.",
        sentence_index=0, claim_type=ClaimType.LEGAL,
    )]


@pytest.fixture
def retrieval(claims, connectors):
    return run_retrieve(claims, connectors).value


def citation(document, quote):
    return document.to_citation(document.locate(quote))


def record(verdict, confidence=0.9, citations=()):
    return AdjudicationRecord(
        claim_id="c-001", verdict=verdict, confidence=confidence,
        reasoning="because the statute says so", citations=list(citations),
    )


def votes(*pairs):
    return [PanelVerdict(model=name, record=rec) for name, rec in pairs]


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


class TestDisagreement:
    @pytest.mark.parametrize(
        ("verdicts", "expected"),
        [
            ([Verdict.SUPPORTED] * 3, 0.0),
            ([Verdict.SUPPORTED, Verdict.SUPPORTED, Verdict.MIXED], 0.3333),
            ([Verdict.SUPPORTED, Verdict.MIXED, Verdict.UNSUPPORTED], 0.6667),
            ([Verdict.SUPPORTED, Verdict.UNSUPPORTED], 0.5),
            ([Verdict.SUPPORTED], 0.0),
        ],
    )
    def test_it_is_one_minus_the_largest_bloc(self, verdicts, expected):
        assert measure_disagreement(verdicts) == pytest.approx(expected, abs=1e-4)

    def test_it_measures_verdicts_not_confidences(self):
        """Two models saying SUPPORTED at 0.7 and 0.9 agree in every sense a
        reader cares about. Averaging confidence would blur the real signal."""
        assert measure_disagreement([Verdict.SUPPORTED, Verdict.SUPPORTED]) == 0.0

    def test_weakest_picks_the_least_assertive(self):
        assert weakest([Verdict.SUPPORTED, Verdict.MIXED]) is Verdict.MIXED
        assert weakest([Verdict.MIXED, Verdict.UNSUPPORTED]) is Verdict.UNSUPPORTED
        assert VERDICT_STRENGTH[Verdict.UNSUPPORTED] < VERDICT_STRENGTH[Verdict.MIXED]


# --------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------


class TestMerge:
    def test_unanimous_keeps_the_verdict_and_the_lowest_confidence(self, retrieval):
        document = retrieval.documents[0]
        merged = merge("c-001", votes(
            ("a", record(Verdict.MIXED, 0.9, [citation(document, REAL_QUOTE)])),
            ("b", record(Verdict.MIXED, 0.6, [citation(document, REAL_QUOTE)])),
        ), [])
        assert merged.unanimous
        assert merged.merged.verdict is Verdict.MIXED
        assert merged.merged.confidence == 0.6
        assert "LOWEST any member reported" in merged.merged.reasoning

    def test_a_split_publishes_the_weakest_not_the_majority(self, retrieval):
        """A 2-1 split must not manufacture a confident verdict.

        The worst a disagreeing panel can do is make the tool say LESS than one
        member knew — a failure, but a safe one. Publishing the majority lets a
        2-1 split assert something a third of the evidence-readers rejected.
        """
        document = retrieval.documents[0]
        merged = merge("c-001", votes(
            ("a", record(Verdict.SUPPORTED, 0.9, [citation(document, REAL_QUOTE)])),
            ("b", record(Verdict.SUPPORTED, 0.9, [citation(document, REAL_QUOTE)])),
            ("c", record(Verdict.MIXED, 0.5, [citation(document, SLATE_QUOTE)])),
        ), [])
        assert merged.merged.verdict is Verdict.MIXED
        assert not merged.unanimous
        assert merged.disagreement == pytest.approx(0.3333, abs=1e-4)
        assert "not the majority" in merged.merged.reasoning

    def test_a_directional_conflict_settles_nothing(self, retrieval):
        """SUPPORTED and CONTRADICTED are equally assertive, not ordered.

        The strength table cannot choose between them, and that is correct:
        a panel holding both has not settled the claim.
        """
        document = retrieval.documents[0]
        merged = merge("c-001", votes(
            ("a", record(Verdict.SUPPORTED, 0.95, [citation(document, REAL_QUOTE)])),
            ("b", record(Verdict.CONTRADICTED, 0.9, [citation(document, SLATE_QUOTE)])),
        ), [])
        assert merged.conflicted
        assert merged.merged.verdict is CONFLICT_VERDICT
        assert merged.merged.confidence == 0.0
        assert "PANEL CONFLICT" in merged.merged.reasoning

    def test_a_conflict_is_not_reported_as_mixed(self):
        """MIXED is a claim about the EVIDENCE, not about the tool's confusion."""
        assert CONFLICT_VERDICT is not Verdict.MIXED
        assert CONFLICT_VERDICT is Verdict.UNSUPPORTED

    def test_a_conflict_keeps_every_sides_citations(self, retrieval):
        document = retrieval.documents[0]
        merged = merge("c-001", votes(
            ("a", record(Verdict.SUPPORTED, 0.95, [citation(document, REAL_QUOTE)])),
            ("b", record(Verdict.CONTRADICTED, 0.9, [citation(document, SLATE_QUOTE)])),
        ), [])
        quotes = {" ".join(c.quote.split()) for c in merged.merged.citations}
        assert len(quotes) == 2

    def test_citations_are_a_deduplicated_union_in_panel_order(self, retrieval):
        document = retrieval.documents[0]
        merged = merge_citations(votes(
            ("a", record(Verdict.MIXED, 0.9, [citation(document, REAL_QUOTE)])),
            ("b", record(Verdict.MIXED, 0.9, [
                citation(document, REAL_QUOTE), citation(document, SLATE_QUOTE),
            ])),
        ))
        assert len(merged) == 2
        assert merged[0].quote.strip().startswith("signed by 1%")

    def test_every_members_verdict_is_named_in_the_reasoning(self, retrieval):
        document = retrieval.documents[0]
        merged = merge("c-001", votes(
            ("qwen", record(Verdict.SUPPORTED, 0.9, [citation(document, REAL_QUOTE)])),
            ("llama", record(Verdict.MIXED, 0.5, [citation(document, REAL_QUOTE)])),
        ), [])
        assert "qwen said SUPPORTED" in merged.merged.reasoning
        assert "llama said MIXED" in merged.merged.reasoning

    def test_a_silent_member_is_named_not_ignored(self, retrieval):
        document = retrieval.documents[0]
        merged = merge("c-001", votes(
            ("a", record(Verdict.MIXED, 0.8, [citation(document, REAL_QUOTE)])),
            ("b", record(Verdict.MIXED, 0.8, [citation(document, REAL_QUOTE)])),
        ), ["c"])
        assert "c returned no verdict" in merged.merged.reasoning


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def write_config(path, body: str):
    path.write_text(body, encoding="utf-8")
    return path


PANEL_TOML = '''
[models.classifier]
provider = "ollama"
model = "small"

[[models.consensus]]
provider = "ollama"
model = "alpha"

[[models.consensus]]
provider = "ollama"
model = "beta"
'''


class TestPanelConfig:
    def test_an_array_of_tables_becomes_a_panel(self, tmp_path):
        config = load_config(write_config(tmp_path / "c.toml", PANEL_TOML))
        assert [spec.model for spec in config.consensus] == ["alpha", "beta"]
        assert config.has_panel

    def test_a_panel_of_one_is_refused(self, tmp_path):
        """A panel of one always agrees with itself."""
        body = PANEL_TOML.replace(
            '\n[[models.consensus]]\nprovider = "ollama"\nmodel = "beta"\n', "\n")
        with pytest.raises(ConfigError, match="one model in it"):
            load_config(write_config(tmp_path / "c.toml", body))

    def test_the_same_model_twice_is_refused(self, tmp_path):
        """At temperature 0.0 it returns the same verdict and reports unanimity.

        A strong-looking signal produced by asking one model twice is the most
        misleading output this mode can produce.
        """
        body = PANEL_TOML.replace('model = "beta"', 'model = "alpha"')
        with pytest.raises(ConfigError, match="more than\\s+once"):
            load_config(write_config(tmp_path / "c.toml", body))

    def test_an_inline_key_in_a_panel_entry_warns(self, tmp_path):
        body = PANEL_TOML.replace(
            '[[models.consensus]]\nprovider = "ollama"\nmodel = "alpha"',
            '[[models.consensus]]\nprovider = "openai"\nmodel = "alpha"\napi_key = "sk-x"',
        )
        config = load_config(write_config(tmp_path / "c.toml", body))
        assert any("inline api_key" in warning for warning in config.warnings)

    def test_a_panel_key_is_never_serialized(self, tmp_path):
        body = PANEL_TOML.replace(
            '[[models.consensus]]\nprovider = "ollama"\nmodel = "alpha"',
            '[[models.consensus]]\nprovider = "openai"\nmodel = "alpha"\napi_key = "sk-secret"',
        )
        config = load_config(write_config(tmp_path / "c.toml", body))
        assert "sk-secret" not in json.dumps(config.redacted())

    def test_consensus_without_a_panel_is_refused_at_build_time(self):
        config = Config(models={"classifier": ModelSpec(
            role="classifier", provider="ollama", model="m")})
        with pytest.raises(ConfigError, match="at least two"):
            build_registry(config, roles=["classifier"], consensus=True)


# --------------------------------------------------------------------------
# Through the pipeline
# --------------------------------------------------------------------------

TEXT = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to form a new party."


def _member(name: str, verdict: str, confidence: float, quote: str):
    identity = ModelIdentity(
        role="consensus", provider="ollama", name=name,
        weights_hash="sha256:" + f"{abs(hash(name)) % 10}" * 64,
    )

    class Member:
        def __init__(self):
            self.name = name

        def identity(self):
            return identity

        def generate(self, request):
            payload = {"adjudications": [{
                "claim_id": "c-001", "verdict": verdict, "confidence": confidence,
                "reasoning": f"{name} read the statute",
                "citations": [{"source_id": "s-001", "quote": quote, "locator": None}],
            }]}
            return Completion(
                text=json.dumps(payload), identity=identity,
                prompt_tokens=10, completion_tokens=10, duration_ms=1,
            )

        def embed(self, texts):
            return []

        def health(self):
            return None

    return Member()


class TestRunConsensus:
    def test_it_merges_a_split_panel(self, claims, retrieval):
        outcome = run_consensus(
            [
                _member("alpha", "SUPPORTED", 0.9, REAL_QUOTE),
                _member("beta", "MIXED", 0.6, REAL_QUOTE),
            ],
            claims, retrieval, max_attempts=1,
        )
        merged = outcome.value.records["c-001"]
        assert merged.merged.verdict is Verdict.MIXED
        assert merged.disagreement == 0.5
        assert outcome.value.panel == ["alpha", "beta"]
        assert any("split the panel" in note for note in outcome.notes)

    def test_a_conflict_is_announced_in_the_stage_notes(self, claims, retrieval):
        outcome = run_consensus(
            [
                _member("alpha", "SUPPORTED", 0.95, REAL_QUOTE),
                _member("beta", "CONTRADICTED", 0.9, SLATE_QUOTE),
            ],
            claims, retrieval, max_attempts=1,
        )
        assert outcome.value.records["c-001"].conflicted
        assert any("PANEL CONFLICT" in note for note in outcome.notes)

    def test_a_failed_member_does_not_fail_the_run(self, claims, retrieval):
        """Two models that answered are still a panel."""

        class Broken:
            name = "broken"

            def identity(self):
                raise ProviderUnavailable("down", provider="broken")

            def generate(self, request):
                raise ProviderUnavailable("down", provider="broken")

            def embed(self, texts):
                return []

            def health(self):
                return None

        outcome = run_consensus(
            [_member("alpha", "MIXED", 0.8, REAL_QUOTE), Broken()],
            claims, retrieval, max_attempts=1,
        )
        assert "c-001" in outcome.value.records
        assert outcome.value.records["c-001"].merged.verdict is Verdict.MIXED


class TestConsensusThroughTheOrchestrator:
    def _registry(self, *members):
        spec = ModelSpec(role="x", provider="ollama", model="m")

        def single(role, verdict="MIXED"):
            return RoleProvider(
                role=role, provider=_role_provider(role), spec=spec,
            )

        return ProviderRegistry(
            {role: single(role) for role in
             ("classifier", "segmenter", "adjudicator", "redteam")},
            [RoleProvider(role="consensus", provider=m, spec=spec) for m in members],
        )

    def test_disagreement_reaches_the_published_claim(self, connectors):
        registry = self._registry(
            _member("alpha", "SUPPORTED", 0.9, REAL_QUOTE),
            _member("beta", "MIXED", 0.6, REAL_QUOTE),
        )
        result = analyze_text(
            TEXT, registry, AnalyzeOptions(offline=True, consensus=True),
            connectors=connectors,
        )
        claim = next(c for c in result.analysis.claims if c.id == "c-001")
        assert claim.model_disagreement == 0.5
        assert claim.verdict is Verdict.MIXED
        assert claim.evidence_quality is SourceTier.T0

    def test_the_run_config_records_the_panel(self, connectors):
        registry = self._registry(
            _member("alpha", "MIXED", 0.9, REAL_QUOTE),
            _member("beta", "MIXED", 0.8, REAL_QUOTE),
        )
        record = analyze_text(
            TEXT, registry, AnalyzeOptions(offline=True, consensus=True),
            connectors=connectors,
        ).record
        assert record.config.consensus
        panel = [m for m in record.config.models if m.role == "consensus"]
        assert [m.name for m in panel] == ["alpha", "beta"]
        assert record.audit() == []

    def test_a_single_model_run_records_no_disagreement(self, connectors):
        """None, not 0.0. Nobody was asked, so nobody agreed."""
        registry = self._registry()
        result = analyze_text(
            TEXT, registry, AnalyzeOptions(offline=True), connectors=connectors,
        )
        assert all(c.model_disagreement is None for c in result.analysis.claims)

    def test_the_stage_chain_is_unchanged_by_consensus(self, connectors):
        """One stage runs on a panel; the pipeline's shape does not change."""
        registry = self._registry(
            _member("alpha", "MIXED", 0.9, REAL_QUOTE),
            _member("beta", "MIXED", 0.8, REAL_QUOTE),
        )
        record = analyze_text(
            TEXT, registry, AnalyzeOptions(offline=True, consensus=True),
            connectors=connectors,
        ).record
        assert StageName.ADJUDICATE in {s.name for s in record.stages}
        assert [s.name.value for s in record.stages] == [
            "ingest", "segment", "classify", "gate", "cluster", "retrieve",
            "adjudicate", "red_team", "compose",
        ]


def _role_provider(role: str):
    identity = ModelIdentity(
        role=role, provider="ollama", name=f"m-{role}",
        weights_hash="sha256:" + f"{abs(hash(role)) % 10}" * 64,
    )

    class Scripted:
        name = "scripted"

        def identity(self):
            return identity

        def generate(self, request):
            prompt = request.prompt
            if "public affairs" in prompt and "IN SCOPE" in prompt:
                payload = {"decisions": [
                    {"sentence_index": 0, "in_scope": True, "reason": "statutory claim"}
                ]}
            elif "atomic" in prompt:
                payload = {"claims": [{
                    "text": TEXT, "sentence_index": 0,
                    "verbatim_span": None, "ambiguous_stance": False,
                }]}
            elif "six types" in prompt or "what would settle this" in prompt:
                payload = {"classifications": [{
                    "claim_id": "c-001", "claim_type": "LEGAL",
                    "confidence": 0.9, "reasoning": "read the statute",
                }]}
            elif "attack the analysis" in prompt or "Analysis to attack" in prompt:
                payload = {"assessments": []}
            else:
                payload = {"adjudications": []}
            return Completion(
                text=json.dumps(payload), identity=identity,
                prompt_tokens=10, completion_tokens=10, duration_ms=1,
            )

        def embed(self, texts):
            return []

        def health(self):
            return None

    return Scripted()
