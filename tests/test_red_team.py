"""Red-team stage: the ratchet, counter-evidence verification, independence.

Two properties dominate this file, and both are enforced in CODE rather than
requested in the prompt — a model cannot be talked out of a rule it never sees:

1. **The ratchet.** This stage can only make the analysis less assertive. It
   can downgrade a verdict and lower confidence; it can never do the reverse.
2. **Counter-evidence is verified.** The red team's quotes go through the same
   verbatim check the adjudicator's do. Otherwise "attack the analysis" becomes
   a licence to invent text that defeats any verdict.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from abca.config import Config, ModelSpec, load_config
from abca.pipeline.adjudicate import AdjudicationRecord
from abca.pipeline.models import DraftClaim
from abca.pipeline.orchestrator import AnalyzeOptions, analyze_text, to_published_claim
from abca.pipeline.red_team import (
    DEFAULT_MATERIAL_VERDICT,
    VERDICT_STRENGTH,
    build_red_team_prompt,
    permitted_downgrade,
    run_red_team,
)
from abca.pipeline.retrieve import run_retrieve
from abca.providers.base import Completion, GenerationRequest, ProviderUnavailable
from abca.providers.registry import build_registry
from abca.providers.transport import FakeTransport
from abca.schema.core import RedTeamFinding
from abca.schema.enums import ClaimType, RedTeamSeverity, SourceTier, Verdict
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
FABRICATED = "the petition requirement shall not apply to established parties"

IDENTITY = ModelIdentity(role="redteam", provider="ollama", name="rt",
                         weights_hash="sha256:" + "c" * 64)


@pytest.fixture
def connectors(tmp_path):
    cache = SourceCache(tmp_path / "sources")
    connector = ILCSConnector(cache=cache, offline=True)
    cache.put(build_document(
        connector=connector, doc_id="s-001", title="10 ILCS 5/10-2",
        url="https://www.ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm",
        text=STATUTE, locator="10 ILCS 5/10-2",
        retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
    ))
    return {"ilcs": connector}


@pytest.fixture
def claims():
    return [DraftClaim(
        id="c-001",
        text="Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures.",
        sentence_index=0, claim_type=ClaimType.LEGAL)]


@pytest.fixture
def retrieval(claims, connectors):
    return run_retrieve(claims, connectors).value


@pytest.fixture
def adjudications(retrieval):
    document = retrieval.documents[0]
    return {"c-001": AdjudicationRecord(
        claim_id="c-001", verdict=Verdict.SUPPORTED, confidence=0.95,
        reasoning="The statute requires 25,000 signatures.",
        citations=[document.to_citation(document.locate(REAL_QUOTE))],
    )}


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
                          prompt_tokens=300, completion_tokens=120, duration_ms=200)

    def embed(self, texts):  # pragma: no cover
        return []

    def health(self) -> None:
        return None


def assessment(**overrides):
    base = {
        "claim_id": "c-001",
        "severity": "MATERIAL",
        "counter_evidence": (
            "The statute sets the lesser of 1% or 25,000, so 25,000 is a ceiling "
            "rather than the requirement."
        ),
        "steelman": "In practice 25,000 is the number an organizer works to.",
        "overreach_flags": ["states a formula as a fixed number"],
        "counter_citations": [],
        "recommended_verdict": "MIXED",
    }
    base.update(overrides)
    return {"assessments": [base]}


# ==========================================================================
# The ratchet
# ==========================================================================


class TestRatchet:
    """The reason an adversarial pass can be trusted to change verdicts."""

    @pytest.mark.parametrize(
        ("current", "recommended", "expected"),
        [
            (Verdict.SUPPORTED, Verdict.MIXED, Verdict.MIXED),
            (Verdict.SUPPORTED, Verdict.UNSUPPORTED, Verdict.UNSUPPORTED),
            (Verdict.CONTRADICTED, Verdict.MIXED, Verdict.MIXED),
            (Verdict.MIXED, Verdict.UNSUPPORTED, Verdict.UNSUPPORTED),
        ],
    )
    def test_weakening_is_permitted(self, current, recommended, expected):
        assert permitted_downgrade(current, recommended) is expected

    @pytest.mark.parametrize(
        ("current", "recommended"),
        [
            (Verdict.UNSUPPORTED, Verdict.SUPPORTED),
            (Verdict.MIXED, Verdict.CONTRADICTED),
            (Verdict.MIXED, Verdict.SUPPORTED),
            (Verdict.UNSUPPORTED, Verdict.MIXED),
        ],
    )
    def test_strengthening_is_refused(self, current, recommended):
        """The worst a compromised red team can do is make the tool say less."""
        assert permitted_downgrade(current, recommended) is None

    def test_same_verdict_is_refused(self):
        assert permitted_downgrade(Verdict.MIXED, Verdict.MIXED) is None

    def test_no_recommendation_falls_to_unsupported(self):
        """If the verdict is not supportable as stated, the honest fallback is
        'the sources do not settle this', not a weaker assertion."""
        assert permitted_downgrade(Verdict.SUPPORTED, None) is DEFAULT_MATERIAL_VERDICT

    def test_unsupported_cannot_fall_further(self):
        assert permitted_downgrade(Verdict.UNSUPPORTED, None) is None

    @pytest.mark.parametrize("verdict", [Verdict.UNVERIFIABLE, Verdict.OUT_OF_SCOPE])
    def test_non_evidence_verdicts_are_untouchable(self, verdict):
        """These follow from the claim's TYPE and the gate, not from evidence,
        so an evidence-based objection has no purchase on them."""
        assert verdict not in VERDICT_STRENGTH
        assert permitted_downgrade(verdict, Verdict.UNSUPPORTED) is None

    def test_ratchet_is_enforced_in_the_stage(self, claims, adjudications, retrieval):
        """Not merely in the helper: a model recommending an upgrade is refused."""
        provider = ScriptedProvider([assessment(recommended_verdict="SUPPORTED")])
        adjudications["c-001"] = AdjudicationRecord(
            claim_id="c-001", verdict=Verdict.MIXED, confidence=0.6,
            reasoning="x", citations=list(adjudications["c-001"].citations))
        outcome = run_red_team(provider, claims, adjudications, retrieval, max_attempts=1)
        assert "c-001" not in outcome.value.downgrades
        assert any("more assertive" in note.lower() for note in outcome.notes)


# ==========================================================================
# Severity
# ==========================================================================


class TestSeverity:
    def _run(self, payload, claims, adjudications, retrieval):
        return run_red_team(ScriptedProvider([payload]), claims, adjudications,
                            retrieval, max_attempts=1)

    def test_material_downgrades(self, claims, adjudications, retrieval):
        outcome = self._run(assessment(), claims, adjudications, retrieval)
        assert outcome.value.downgrades["c-001"][0] is Verdict.MIXED

    @pytest.mark.parametrize("severity", ["NONE", "NOTED", "MINOR"])
    def test_lesser_severities_change_nothing(self, severity, claims, adjudications,
                                              retrieval):
        """A tool that downgraded on every objection would say nothing about anything."""
        outcome = self._run(assessment(severity=severity, recommended_verdict=None),
                            claims, adjudications, retrieval)
        assert outcome.value.downgrades == {}
        assert outcome.value.findings["c-001"].severity.value == severity

    def test_lesser_severities_are_still_published(self, claims, adjudications, retrieval):
        """A reader weighing a verdict is better served by seeing the objections
        that did NOT overturn it than by a silent pass."""
        outcome = self._run(assessment(severity="MINOR", recommended_verdict=None),
                            claims, adjudications, retrieval)
        finding = outcome.value.findings["c-001"]
        assert finding.counter_evidence
        assert finding.steelman

    def test_none_is_a_real_answer(self, claims, adjudications, retrieval):
        outcome = self._run(
            assessment(severity="NONE", counter_evidence="Nothing cuts against this.",
                       overreach_flags=[], recommended_verdict=None),
            claims, adjudications, retrieval)
        assert outcome.value.findings["c-001"].severity is RedTeamSeverity.NONE
        assert outcome.value.downgrades == {}


# ==========================================================================
# Counter-evidence verification
# ==========================================================================


class TestCounterEvidenceVerification:
    def _run(self, payload, claims, adjudications, retrieval):
        return run_red_team(ScriptedProvider([payload]), claims, adjudications,
                            retrieval, max_attempts=1)

    def test_real_counter_quote_becomes_a_citation(self, claims, adjudications, retrieval):
        outcome = self._run(
            assessment(counter_citations=[{"source_id": "s-001", "quote": SLATE_QUOTE,
                                           "locator": None}]),
            claims, adjudications, retrieval)
        finding = outcome.value.findings["c-001"]
        assert len(finding.counter_citations) == 1
        assert finding.counter_citations[0].tier is SourceTier.T0

    def test_fabricated_counter_quote_is_discarded(self, claims, adjudications, retrieval):
        """Otherwise 'attack the analysis' is a licence to invent text."""
        outcome = self._run(
            assessment(counter_citations=[{"source_id": "s-001", "quote": FABRICATED,
                                           "locator": None}]),
            claims, adjudications, retrieval)
        finding = outcome.value.findings["c-001"]
        assert finding.counter_citations == []
        assert "failed verbatim verification" in finding.counter_evidence

    def test_unverifiable_counter_evidence_alone_cannot_be_material(
        self, claims, adjudications, retrieval
    ):
        """An objection whose textual support evaporated is not MATERIAL on that
        support alone."""
        outcome = self._run(
            assessment(overreach_flags=[],
                       counter_citations=[{"source_id": "s-001", "quote": FABRICATED,
                                           "locator": None}]),
            claims, adjudications, retrieval)
        assert outcome.value.findings["c-001"].severity is RedTeamSeverity.MINOR
        assert outcome.value.downgrades == {}

    def test_analytical_objection_survives_without_citations(
        self, claims, adjudications, retrieval
    ):
        """"The statute is silent, so this rests on inference" is legitimate,
        often the strongest objection, and needs no quote."""
        outcome = self._run(
            assessment(counter_citations=[],
                       overreach_flags=["verdict rests on inference across provisions"]),
            claims, adjudications, retrieval)
        assert outcome.value.findings["c-001"].severity is RedTeamSeverity.MATERIAL
        assert "c-001" in outcome.value.downgrades

    def test_unknown_source_is_discarded(self, claims, adjudications, retrieval):
        outcome = self._run(
            assessment(counter_citations=[{"source_id": "s-999", "quote": SLATE_QUOTE,
                                           "locator": None}]),
            claims, adjudications, retrieval)
        assert outcome.value.findings["c-001"].counter_citations == []


# ==========================================================================
# Independence
# ==========================================================================


class TestIndependence:
    def test_independence_is_recorded_on_every_finding(self, claims, adjudications,
                                                       retrieval):
        outcome = run_red_team(ScriptedProvider([assessment()]), claims, adjudications,
                              retrieval, independent=False, max_attempts=1)
        assert outcome.value.findings["c-001"].independent is False

    def test_non_independence_is_noted_loudly(self, claims, adjudications, retrieval):
        """A model reviewing its own reasoning is the one least able to see
        where it reached; a reader must be told."""
        outcome = run_red_team(ScriptedProvider([assessment()]), claims, adjudications,
                              retrieval, independent=False, max_attempts=1)
        assert any("SAME model" in note for note in outcome.notes)

    def test_independent_run_is_not_noted(self, claims, adjudications, retrieval):
        outcome = run_red_team(ScriptedProvider([assessment()]), claims, adjudications,
                              retrieval, independent=True, max_attempts=1)
        assert not any("SAME model" in note for note in outcome.notes)
        assert outcome.value.findings["c-001"].independent is True

    def test_redteam_role_falls_back_to_adjudicator(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[models.adjudicator]\nmodel="big"\n')
        assert load_config(path).spec("redteam").model == "big"

    def test_explicit_redteam_role_wins(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[models.adjudicator]\nmodel="big"\n[models.redteam]\nmodel="other"\n')
        assert load_config(path).spec("redteam").model == "other"


# ==========================================================================
# Failure behaviour
# ==========================================================================


class TestFailureBehaviour:
    def test_failure_leaves_verdicts_unattacked_and_says_so(self, claims, adjudications,
                                                            retrieval):
        """The absence of the pass is exactly what the contract forbids, so it
        must be visible rather than the claim shipping as though reviewed."""
        provider = ScriptedProvider([ProviderUnavailable("down", provider="x")])
        outcome = run_red_team(provider, claims, adjudications, retrieval, max_attempts=1)
        assert outcome.value.findings == {}
        assert any("NOT adversarially reviewed" in note for note in outcome.notes)

    def test_unknown_claim_id_is_discarded(self, claims, adjudications, retrieval):
        outcome = run_red_team(
            ScriptedProvider([assessment(claim_id="c-999")]), claims, adjudications,
            retrieval, max_attempts=1)
        assert outcome.value.findings == {}

    def test_unadjudicated_claims_are_not_attacked(self, claims, retrieval):
        provider = ScriptedProvider([{"assessments": []}])
        outcome = run_red_team(provider, claims, {}, retrieval, max_attempts=1)
        assert provider.calls == 0
        assert any("no adjudicated verdicts" in note for note in outcome.notes)


# ==========================================================================
# Prompt construction
# ==========================================================================


class TestPrompt:
    def test_prompt_carries_the_verdict_and_its_citations(self, claims, adjudications,
                                                          retrieval):
        prompt = build_red_team_prompt(
            "PROMPT", claims, adjudications, retrieval.documents)
        assert "SUPPORTED" in prompt
        assert "0.95" in prompt
        assert "1% of the number of voters" in prompt

    def test_prompt_carries_the_same_sources_the_analyst_had(self, claims, adjudications,
                                                             retrieval):
        """The strongest counter-evidence is usually another passage in the same
        source; a red team without the text can only object in the abstract."""
        prompt = build_red_team_prompt(
            "PROMPT", claims, adjudications, retrieval.documents)
        assert "complete list of candidates" in prompt
        assert "[s-001]" in prompt


# ==========================================================================
# Published claim
# ==========================================================================


class TestPublishedClaim:
    def test_downgrade_is_applied_and_visible(self, retrieval):
        document = retrieval.documents[0]
        record = AdjudicationRecord(
            claim_id="c-001", verdict=Verdict.SUPPORTED, confidence=0.95,
            reasoning="the statute says so",
            citations=[document.to_citation(document.locate(REAL_QUOTE))])
        finding = RedTeamFinding(
            counter_evidence="the statute sets a formula, not a fixed number",
            steelman="in practice the number is what organizers work to",
            severity=RedTeamSeverity.MATERIAL,
            verdict_downgraded_from=Verdict.SUPPORTED)
        claim = to_published_claim(
            DraftClaim(id="c-001", text="x", sentence_index=0, claim_type=ClaimType.LEGAL),
            record, red_team=finding, downgrade=(Verdict.MIXED, 0.5))
        assert claim.verdict is Verdict.MIXED
        assert claim.confidence == 0.5
        assert "downgraded from SUPPORTED" in claim.reasoning
        assert claim.red_team is finding

    def test_finding_ships_inside_the_claim(self, retrieval):
        """Not in a log. A verdict published without its objections is a verdict
        published with the honest part removed."""
        document = retrieval.documents[0]
        record = AdjudicationRecord(
            claim_id="c-001", verdict=Verdict.MIXED, confidence=0.7, reasoning="x",
            citations=[document.to_citation(document.locate(REAL_QUOTE))])
        finding = RedTeamFinding(counter_evidence="none found", steelman="none",
                                 severity=RedTeamSeverity.NONE)
        claim = to_published_claim(
            DraftClaim(id="c-001", text="x", sentence_index=0, claim_type=ClaimType.LEGAL),
            record, red_team=finding)
        assert claim.red_team.severity is RedTeamSeverity.NONE
        assert claim.verdict is Verdict.MIXED  # NONE changed nothing


# ==========================================================================
# End to end
# ==========================================================================


TEXT = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to start a new party."
GATE = {"decisions": [{"sentence_index": 0, "in_scope": True, "reason": "statute"}]}
SEGMENT = {"claims": [{"text": TEXT, "sentence_index": 0, "verbatim_span": None,
                       "ambiguous_stance": False}]}
CLASSIFY = {"classifications": [{"claim_id": "c-001", "claim_type": "LEGAL",
                                 "confidence": 0.94, "reasoning": "read the statute"}]}
ADJUDICATE = {"adjudications": [{
    "claim_id": "c-001", "verdict": "SUPPORTED", "confidence": 0.95,
    "reasoning": "The statute requires 25,000 signatures.",
    "citations": [{"source_id": "s-001", "quote": REAL_QUOTE, "locator": None}]}]}


def transport(payloads):
    def gen(payload):
        return {"response": json.dumps(payload), "prompt_eval_count": 400,
                "eval_count": 90, "total_duration": 1_000_000_000, "done_reason": "stop"}
    return FakeTransport({
        "/api/version": {"version": "0.6.2"},
        "/api/tags": {"models": [
            {"name": "m", "digest": "a" * 64, "details": {"quantization_level": "Q5_K_M"}},
            {"name": "rt", "digest": "c" * 64, "details": {"quantization_level": "Q5_K_M"}},
        ]},
        "/api/show": {"details": {"quantization_level": "Q5_K_M"},
                      "model_info": {"qwen2.context_length": 32768}},
        "/api/generate": [gen(p) for p in payloads],
    })


def registry_for(payloads, *, separate_red_team: bool):
    models = {
        "classifier": ModelSpec(role="classifier", provider="ollama", model="m"),
        "adjudicator": ModelSpec(role="adjudicator", provider="ollama", model="m"),
    }
    if separate_red_team:
        models["redteam"] = ModelSpec(role="redteam", provider="ollama", model="rt")
    roles = ["classifier", "segmenter", "adjudicator", "redteam"]
    shared = transport(payloads)
    return build_registry(Config(models=models), roles=roles,
                          transports=dict.fromkeys(roles, shared))


class TestEndToEnd:
    def test_material_finding_downgrades_the_published_verdict(self, connectors):
        red_team = {"assessments": [{
            "claim_id": "c-001", "severity": "MATERIAL",
            "counter_evidence": "The statute sets the lesser of 1% or 25,000.",
            "steelman": "25,000 is the operative number in practice.",
            "overreach_flags": ["states a formula as a fixed number"],
            "counter_citations": [{"source_id": "s-001", "quote": SLATE_QUOTE,
                                   "locator": None}],
            "recommended_verdict": "MIXED"}]}
        registry = registry_for(
            [GATE, SEGMENT, CLASSIFY, ADJUDICATE, red_team], separate_red_team=True)
        record = analyze_text(TEXT, registry, AnalyzeOptions(offline=True),
                              connectors=connectors).record
        claim = record.result.claims[0]
        assert claim.verdict is Verdict.MIXED
        assert claim.red_team.verdict_downgraded_from is Verdict.SUPPORTED
        assert claim.red_team.independent is True
        assert len(claim.red_team.counter_citations) == 1

    def test_same_model_red_team_is_flagged_not_independent(self, connectors):
        red_team = {"assessments": [{
            "claim_id": "c-001", "severity": "NONE",
            "counter_evidence": "nothing found", "steelman": "n/a",
            "overreach_flags": [], "counter_citations": [],
            "recommended_verdict": None}]}
        registry = registry_for(
            [GATE, SEGMENT, CLASSIFY, ADJUDICATE, red_team], separate_red_team=False)
        record = analyze_text(TEXT, registry, AnalyzeOptions(offline=True),
                              connectors=connectors).record
        assert record.result.claims[0].red_team.independent is False

    def test_stage_appears_in_the_chain(self, connectors):
        red_team = {"assessments": []}
        registry = registry_for(
            [GATE, SEGMENT, CLASSIFY, ADJUDICATE, red_team], separate_red_team=True)
        record = analyze_text(TEXT, registry, AnalyzeOptions(offline=True),
                              connectors=connectors).record
        assert "red_team" in [s.name.value for s in record.stages]
        assert record.is_intact()

    def test_skipping_it_omits_the_stage_and_the_prompt(self, connectors):
        registry = registry_for(
            [GATE, SEGMENT, CLASSIFY, ADJUDICATE], separate_red_team=True)
        record = analyze_text(TEXT, registry, AnalyzeOptions(offline=True, red_team=False),
                              connectors=connectors).record
        assert "red_team" not in [s.name.value for s in record.stages]
        assert "red_team" not in {ref.stage.value for ref in record.config.prompts}
        assert record.config.red_team is False
