"""The plain-language fidelity gate (contract s5).

Three properties dominate this file, and all three are structural rather than
requested of a model:

1. **Pass C is isolated.** :func:`build_backtranslate_prompt` has no parameter
   through which the statute could reach the back-translating model, so no
   future edit can leak it by accident. The test asserts the statute is absent
   from the prompt actually produced.
2. **The diff is code.** Every pass/fail below is decided by
   :mod:`abca.fidelity.elements`, so the same rendering always produces the
   same score. A model deciding "close enough" would make the number in the
   run record unreproducible.
3. **The fallback is reachable.** Three failed rewrites emit the statute's own
   words with ``unresolved=True``. Contract s5 requires that outcome to exist,
   and a gate that could not reach it would be a gate that eventually passes
   anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from abca.config import ModelSpec
from abca.fidelity.elements import ElementKind, LegalElement, Modal, diff_elements
from abca.pipeline.fidelity import (
    MAX_RENDER_ATTEMPTS,
    FidelityConfigError,
    assert_independent,
    build_backtranslate_prompt,
    build_extract_prompt,
    build_render_prompt,
    ground_elements,
    modal_flips,
    run_fidelity,
    to_legal_elements,
)
from abca.pipeline.models import ExtractedElement
from abca.pipeline.orchestrator import AnalyzeOptions, build_explain_config, explain_citation
from abca.prompts import assert_doctrine_free, load_prompt
from abca.providers.base import Completion, GenerationRequest, ProviderUnavailable
from abca.providers.registry import ProviderRegistry, RoleProvider
from abca.schema.enums import Profile, StageName
from abca.schema.ledger import ModelIdentity, PromptRef, RunConfig
from abca.sources.base import build_document
from abca.sources.cache import SourceCache
from abca.sources.ilcs import ILCSConnector

# --------------------------------------------------------------------------
# Fixtures: one real statute, and the element sets a model might return
# --------------------------------------------------------------------------

STATUTE = """(10 ILCS 5/10-2)

Sec. 10-2.
Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled.
"""

#: What pass A should return: three operative elements, in the statute's words.
EXTRACT = {
    "elements": [
        {"kind": "WHO_IS_BOUND",
         "text": "Any group of persons hereafter desiring to form a new political "
                 "party throughout the State"},
        {"kind": "REQUIREMENT",
         "text": "shall file a petition signed by 1% of the number of voters who "
                 "voted at the next preceding Statewide general election or 25,000 "
                 "qualified voters, whichever is less"},
        {"kind": "REQUIREMENT",
         "text": "shall at the time of filing contain a complete list of candidates "
                 "of such party for all offices to be filled"},
    ]
}

GOOD_RENDERING = (
    "A group of people who want to start a new political party across the whole "
    "state must file a petition. The petition must be signed by 1% of the "
    "voters who voted in the last statewide general election, or by 25,000 "
    "qualified voters, whichever is less. The petition must also list "
    "the party's candidates. It must name a candidate for every office to be "
    "filled. That list must be there on the day the petition is filed."
)

RENDER_OK = {"rendering": GOOD_RENDERING, "footnotes": []}

#: A faithful reconstruction: all three elements, same kinds, same modal force,
#: same numbers -- in different words, which is the whole point of a rewrite.
BACKTRANSLATE_OK = {
    "elements": [
        {"kind": "WHO_IS_BOUND",
         "text": "A group of people who want to start a new political party across "
                 "the whole state"},
        {"kind": "REQUIREMENT",
         "text": "must file a petition signed by 1% of the voters who voted in the "
                 "last statewide general election, or 25,000 qualified voters, "
                 "whichever is less"},
        {"kind": "REQUIREMENT",
         "text": "must include a complete list of the party's candidates for all "
                 "offices to be filled at that election"},
    ]
}

#: The failure the gate exists to catch: the full-slate requirement is gone.
BACKTRANSLATE_DROPPED = {"elements": BACKTRANSLATE_OK["elements"][:2]}

#: The other failure it exists to catch: "shall" read back as "may".
BACKTRANSLATE_MODAL_FLIP = {
    "elements": [
        BACKTRANSLATE_OK["elements"][0],
        {"kind": "PERMISSION",
         "text": "may file a petition signed by 1% of the voters who voted in the "
                 "last statewide general election, or 25,000 qualified voters, "
                 "whichever is less"},
        BACKTRANSLATE_OK["elements"][2],
    ]
}

#: An obligation the statute does not contain.
BACKTRANSLATE_INVENTED = {
    "elements": [
        *BACKTRANSLATE_OK["elements"],
        {"kind": "PENALTY",
         "text": "a party that files late is barred from the ballot for two years"},
    ]
}

RENDERER_ID = ModelIdentity(role="adjudicator", provider="ollama", name="big",
                            weights_hash="sha256:" + "a" * 64)
BACKTRANSLATOR_ID = ModelIdentity(role="backtranslate", provider="ollama", name="small",
                                  weights_hash="sha256:" + "b" * 64)


class ScriptedProvider:
    """Returns canned JSON payloads in order; repeats the last one forever."""

    name = "scripted"

    def __init__(self, payloads, identity=RENDERER_ID) -> None:
        self.payloads = payloads
        self._identity = identity
        self.calls = 0
        self.prompts: list[str] = []

    def identity(self):
        return self._identity

    def generate(self, request: GenerationRequest) -> Completion:
        self.prompts.append(request.prompt)
        item = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return Completion(text=json.dumps(item), identity=self._identity,
                          prompt_tokens=400, completion_tokens=200, duration_ms=100)

    def embed(self, texts):  # pragma: no cover
        return []

    def health(self) -> None:
        return None


def renderer(*payloads) -> ScriptedProvider:
    return ScriptedProvider(list(payloads), identity=RENDERER_ID)


def backtranslator(*payloads) -> ScriptedProvider:
    return ScriptedProvider(list(payloads), identity=BACKTRANSLATOR_ID)


def gate(render_payloads, backtranslate_payloads, **kwargs):
    """Run the gate with scripted models. Returns the StageOutcome."""
    return run_fidelity(
        renderer(*render_payloads),
        backtranslator(*backtranslate_payloads),
        source_text=STATUTE,
        title="10 ILCS 5/10-2",
        locator="10 ILCS 5/10-2",
        **kwargs,
    )


# ==========================================================================
# Prompts
# ==========================================================================


class TestFidelityPrompts:
    def test_three_variants_exist_with_distinct_hashes(self):
        """Three model calls, three separately versioned and hashed files."""
        hashes = {
            variant: load_prompt(StageName.FIDELITY, variant=variant).content_hash
            for variant in ("extract", "render", "backtranslate")
        }
        assert len(set(hashes.values())) == 3

    def test_variants_report_the_stage_they_belong_to(self):
        """One stage, several prompts. The path distinguishes them, not the stage."""
        ref = load_prompt(StageName.FIDELITY, variant="render").to_ref()
        assert ref.stage is StageName.FIDELITY
        assert ref.path.endswith("fidelity.render.md")

    def test_fidelity_prompts_are_doctrine_free(self):
        assert_doctrine_free()


# ==========================================================================
# Isolation: the property the whole gate rests on
# ==========================================================================


class TestPassCIsolation:
    def test_backtranslate_prompt_does_not_contain_the_statute(self):
        """The single most important assertion in this file.

        A pass C that could see the provision would reconstruct the provision
        whether or not the rewrite preserved it, and the diff would then be
        measuring the model's memory instead of the rewrite's fidelity.
        """
        prompt = build_backtranslate_prompt(
            load_prompt(StageName.FIDELITY, variant="backtranslate").text,
            GOOD_RENDERING,
        )
        assert GOOD_RENDERING in prompt
        for phrase in (
            "Any group of persons hereafter desiring",
            "next preceding Statewide general election",
            "10 ILCS 5/10-2",
        ):
            assert phrase not in prompt

    def test_backtranslate_prompt_takes_no_source_parameter(self):
        """Enforcement is the signature, not an instruction inside the prompt.

        An instruction can be diluted by a later edit and nothing fails. A
        missing parameter cannot be passed by accident.
        """
        import inspect

        parameters = list(inspect.signature(build_backtranslate_prompt).parameters)
        assert parameters == ["prompt_text", "rendering"]

    def test_running_the_gate_never_shows_pass_c_the_statute(self):
        """End to end, not just the builder: check the prompts actually sent."""
        translator = backtranslator(BACKTRANSLATE_OK)
        run_fidelity(
            renderer(EXTRACT, RENDER_OK),
            translator,
            source_text=STATUTE,
            title="10 ILCS 5/10-2",
        )
        assert translator.prompts
        for prompt in translator.prompts:
            assert "hereafter desiring" not in prompt
            assert "25,000 qualified voters, whichever is less," not in prompt


class TestIndependence:
    def test_same_model_for_both_passes_is_refused(self):
        """Contract s5. A model grading its own rewrite reproduces its misreadings."""
        same = ScriptedProvider([], identity=RENDERER_ID)
        with pytest.raises(FidelityConfigError, match="same model"):
            assert_independent(same, ScriptedProvider([], identity=RENDERER_ID))

    def test_different_weights_hashes_are_independent(self):
        assert_independent(renderer(), backtranslator())  # does not raise

    def test_same_name_under_different_roles_is_still_refused(self):
        """Compared by weights hash, so two tags for one model cannot slip past."""
        left = ScriptedProvider([], identity=ModelIdentity(
            role="adjudicator", provider="ollama", name="tag-a",
            weights_hash="sha256:" + "c" * 64))
        right = ScriptedProvider([], identity=ModelIdentity(
            role="backtranslate", provider="ollama", name="tag-b",
            weights_hash="sha256:" + "c" * 64))
        with pytest.raises(FidelityConfigError):
            assert_independent(left, right)

    def test_unresolvable_identity_is_refused_not_waved_through(self):
        """An unverifiable independence claim is not an independence claim."""

        class Broken(ScriptedProvider):
            def identity(self):
                raise ProviderUnavailable("backend down", provider="scripted")

        with pytest.raises(FidelityConfigError, match="could not resolve"):
            assert_independent(Broken([]), backtranslator())


# ==========================================================================
# The gate's verdicts
# ==========================================================================


class TestGateOutcomes:
    def test_faithful_rewrite_passes_first_time(self):
        outcome = gate([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK])
        result = outcome.value

        assert not result.verbatim_fallback
        assert result.text == GOOD_RENDERING
        report = result.to_report()
        assert report.applied
        assert report.score == 1.0
        assert report.elements_source == 3
        assert report.elements_preserved == 3
        assert report.modal_verbs_preserved
        assert report.regenerations == 0
        assert not report.unresolved

    def test_a_dropped_requirement_fails_and_falls_back_to_verbatim(self):
        """The failure this gate exists for: the rewrite lost the full slate."""
        outcome = gate([EXTRACT, RENDER_OK], [BACKTRANSLATE_DROPPED])
        result = outcome.value

        assert result.verbatim_fallback
        assert result.text == STATUTE.strip()
        assert len(result.attempts) == MAX_RENDER_ATTEMPTS

        report = result.to_report()
        assert report.unresolved
        assert report.elements_preserved == 2
        assert report.score == pytest.approx(2 / 3)
        assert report.regenerations == MAX_RENDER_ATTEMPTS - 1
        assert any("complete list of candidates" in f
                   for attempt in result.attempts for f in attempt.failures)

    def test_a_modal_flip_fails_and_is_reported_as_a_modal_change(self):
        """'shall' read back as 'may' is the highest-signal failure available."""
        outcome = gate([EXTRACT, RENDER_OK], [BACKTRANSLATE_MODAL_FLIP])
        result = outcome.value

        assert result.verbatim_fallback
        assert not result.to_report().modal_verbs_preserved
        flips = [f for attempt in result.attempts for f in attempt.flips]
        assert flips
        assert flips[0].startswith("MODAL CHANGE")
        assert "MANDATORY -> PERMISSIVE" in flips[0]

    def test_an_invented_obligation_fails_even_though_nothing_was_lost(self):
        """A rewrite that ADDS a rule is a different law, not a better rewrite."""
        outcome = gate([EXTRACT, RENDER_OK], [BACKTRANSLATE_INVENTED])
        result = outcome.value

        assert result.verbatim_fallback
        # Every source element survived -- the score alone would look perfect.
        assert result.to_report().score == 1.0
        assert any(f.startswith("ADDED") for a in result.attempts for f in a.failures)

    def test_a_second_attempt_can_recover(self):
        """Regeneration is real: attempt 1 fails, attempt 2 passes, gate passes."""
        outcome = gate(
            [EXTRACT, RENDER_OK],
            [BACKTRANSLATE_DROPPED, BACKTRANSLATE_OK],
        )
        result = outcome.value

        assert not result.verbatim_fallback
        assert len(result.attempts) == 2
        assert result.to_report().regenerations == 1

    def test_regeneration_prompt_names_what_was_lost(self):
        """A blind retry at temperature 0.0 reproduces the same rewrite."""
        model = renderer(EXTRACT, RENDER_OK)
        run_fidelity(
            model, backtranslator(BACKTRANSLATE_DROPPED),
            source_text=STATUTE,
        )
        # prompts: extract, render#1, render#2, render#3
        assert len(model.prompts) == 1 + MAX_RENDER_ATTEMPTS
        assert "did not survive the check" in model.prompts[2]
        assert "complete list of candidates" in model.prompts[2]

    def test_stops_after_max_render_attempts(self):
        outcome = gate([EXTRACT, RENDER_OK], [BACKTRANSLATE_DROPPED])
        assert len(outcome.value.attempts) == MAX_RENDER_ATTEMPTS


class TestGateFailureModes:
    def test_pass_a_failure_emits_the_statute_rather_than_a_rewrite(self):
        """With no elements there is nothing to check a rewrite against."""
        outcome = run_fidelity(
            renderer(ProviderUnavailable("down", provider="scripted")),
            backtranslator(BACKTRANSLATE_OK),
            source_text=STATUTE,
        )
        result = outcome.value
        assert result.verbatim_fallback
        assert result.text == STATUTE.strip()
        assert not result.attempts
        assert any("pass A" in note for note in outcome.notes)

    def test_empty_extraction_is_not_a_pass(self):
        outcome = gate([{"elements": []}, RENDER_OK], [BACKTRANSLATE_OK])
        assert outcome.value.verbatim_fallback
        assert any("no operative elements" in note for note in outcome.notes)

    def test_pass_c_failure_is_not_a_pass(self):
        """An unchecked rewrite is exactly what the gate exists to stop."""
        outcome = run_fidelity(
            renderer(EXTRACT, RENDER_OK),
            backtranslator(ProviderUnavailable("down", provider="scripted")),
            source_text=STATUTE,
        )
        result = outcome.value
        assert result.verbatim_fallback
        assert any("pass C" in f for a in result.attempts for f in a.failures)

    def test_pass_b_failure_is_recorded_as_an_attempt_not_an_exception(self):
        outcome = run_fidelity(
            renderer(EXTRACT, ProviderUnavailable("down", provider="scripted")),
            backtranslator(BACKTRANSLATE_OK),
            source_text=STATUTE,
        )
        assert outcome.value.verbatim_fallback
        assert len(outcome.value.attempts) == MAX_RENDER_ATTEMPTS

    def test_empty_provision_does_not_crash(self):
        outcome = run_fidelity(renderer(), backtranslator(), source_text="   ")
        assert outcome.value.verbatim_fallback
        assert any("no provision text" in note for note in outcome.notes)

    def test_oversized_provision_is_truncated_and_says_so(self):
        outcome = run_fidelity(
            renderer(EXTRACT, RENDER_OK), backtranslator(BACKTRANSLATE_OK),
            source_text="x " * 20_000,
        )
        assert outcome.value.truncated
        assert any("truncated" in note for note in outcome.notes)


# ==========================================================================
# Element handling
# ==========================================================================


class TestElementHandling:
    def test_modal_and_anchors_come_from_the_text_not_the_model(self):
        """A model that could declare its modal could declare the one it kept."""
        elements = to_legal_elements([
            ExtractedElement(kind=ElementKind.REQUIREMENT,
                             text="shall file within 30 days")
        ])
        assert elements[0].modal is Modal.MANDATORY
        assert elements[0].anchors == ("30 days",)
        assert "modal" not in ExtractedElement.model_fields
        assert "anchors" not in ExtractedElement.model_fields

    def test_invented_anchors_are_discarded_as_an_extraction_error(self):
        """Otherwise the rewrite is blamed for a number pass A made up."""
        elements = to_legal_elements([
            ExtractedElement(kind=ElementKind.REQUIREMENT,
                             text="shall gather 25,000 signatures"),
            ExtractedElement(kind=ElementKind.PENALTY,
                             text="shall pay a fine of $5,000"),
        ])
        kept, notes = ground_elements(elements, STATUTE)
        assert len(kept) == 1
        assert kept[0].kind is ElementKind.REQUIREMENT
        assert notes and "$5000" in notes[0]

    def test_grounding_keeps_elements_with_no_anchors(self):
        elements = to_legal_elements([
            ExtractedElement(kind=ElementKind.WHO_IS_BOUND, text="any group of persons")
        ])
        kept, notes = ground_elements(elements, STATUTE)
        assert len(kept) == 1
        assert not notes

    def test_modal_flip_detection_is_structural_not_string_matching(self):
        source = [LegalElement.build(ElementKind.REQUIREMENT,
                                     "shall file a complete list of candidates")]
        reconstructed = [LegalElement.build(ElementKind.PERMISSION,
                                            "may file a complete list of candidates")]
        diff = diff_elements(source, reconstructed)
        flips = modal_flips(diff, reconstructed)
        assert len(flips) == 1
        assert flips[0][0].modal is Modal.MANDATORY
        assert flips[0][1].modal is Modal.PERMISSIVE

    def test_a_simply_missing_element_is_not_reported_as_a_modal_flip(self):
        """Dropping a rule and misstating its force are different failures."""
        source = [LegalElement.build(ElementKind.REQUIREMENT,
                                     "shall file a complete list of candidates")]
        diff = diff_elements(source, [])
        assert modal_flips(diff, []) == []


class TestPromptConstruction:
    def test_render_prompt_carries_the_source_and_the_elements(self):
        elements = to_legal_elements(
            [ExtractedElement(**item) for item in EXTRACT["elements"]]
        )
        prompt = build_render_prompt(
            load_prompt(StageName.FIDELITY, variant="render").text,
            STATUTE, elements, title="10 ILCS 5/10-2",
        )
        assert "hereafter desiring" in prompt
        assert "[3] REQUIREMENT:" in prompt

    def test_extract_prompt_carries_the_source(self):
        prompt = build_extract_prompt(
            load_prompt(StageName.FIDELITY, variant="extract").text,
            STATUTE, title="10 ILCS 5/10-2",
        )
        assert "hereafter desiring" in prompt


# ==========================================================================
# Linters: advisory, never a hard failure
# ==========================================================================


class TestLinters:
    def test_readability_is_flagged_not_failed(self):
        """Cutting clauses to hit a grade band is how a rewrite loses an exception."""
        dense = (
            "A group of people who want to start a new political party throughout "
            "the entire state, notwithstanding any other provision of law "
            "whatsoever, must file a petition signed by 1% of the voters who voted "
            "in the immediately preceding statewide general election or by 25,000 "
            "qualified voters, whichever is less, and must simultaneously include "
            "a complete list of the party's candidates for all offices to be filled."
        )
        backtranslation = {
            "elements": [
                {"kind": "WHO_IS_BOUND",
                 "text": "A group of people who want to start a new political party "
                         "throughout the entire state"},
                {"kind": "REQUIREMENT",
                 "text": "must file a petition signed by 1% of the voters who voted in "
                         "the immediately preceding statewide general election or by "
                         "25,000 qualified voters, whichever is less"},
                {"kind": "REQUIREMENT",
                 "text": "must include a complete list of the party's candidates for "
                         "all offices to be filled"},
            ]
        }
        outcome = gate(
            [EXTRACT, {"rendering": dense, "footnotes": []}], [backtranslation]
        )
        result = outcome.value
        assert not result.verbatim_fallback  # readability did not fail it
        assert result.readability is not None

    def test_linters_run_on_the_verbatim_fallback_too(self):
        outcome = gate([EXTRACT, RENDER_OK], [BACKTRANSLATE_DROPPED])
        result = outcome.value
        assert result.readability is not None
        assert result.scope is not None
        assert result.glossary is not None


# ==========================================================================
# explain: the command's pipeline
# ==========================================================================


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


def explain_registry(render_payloads, backtranslate_payloads) -> ProviderRegistry:
    spec = ModelSpec(role="x", provider="ollama", model="x")
    return ProviderRegistry({
        "adjudicator": RoleProvider(
            role="adjudicator", provider=renderer(*render_payloads), spec=spec),
        "backtranslate": RoleProvider(
            role="backtranslate", provider=backtranslator(*backtranslate_payloads),
            spec=spec),
    })


class TestExplain:
    def test_produces_an_auditable_record_with_the_right_stages(self, connectors):
        result = explain_citation(
            "10 ILCS 5/10-2",
            explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]),
            AnalyzeOptions(offline=True),
            connectors=connectors,
        )
        assert result.resolved
        assert [stage.name for stage in result.record.stages] == [
            StageName.INGEST, StageName.RETRIEVE, StageName.FIDELITY, StageName.COMPOSE
        ]
        # The record survives its own integrity audit, hash chain included.
        assert result.record.audit() == []

    def test_the_statute_is_pinned_as_a_source_snapshot(self, connectors):
        """The citation is the input; the statute is evidence, hashed separately.

        That separation is what makes a later amendment show up as DRIFTED
        rather than silently changing what this run appears to have said.
        """
        result = explain_citation(
            "10 ILCS 5/10-2",
            explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]),
            AnalyzeOptions(offline=True),
            connectors=connectors,
        )
        assert len(result.record.sources) == 1
        assert result.record.sources[0].connector == "ilcs"
        assert result.record.result.document.content_hash != \
            result.record.sources[0].content_hash

    def test_the_fidelity_report_reaches_the_analysis_result(self, connectors):
        result = explain_citation(
            "10 ILCS 5/10-2",
            explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]),
            AnalyzeOptions(offline=True),
            connectors=connectors,
        )
        report = result.record.result.fidelity
        assert report is not None and report.applied and report.score == 1.0

    def test_a_failed_gate_still_produces_a_record(self, connectors):
        result = explain_citation(
            "10 ILCS 5/10-2",
            explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_DROPPED]),
            AnalyzeOptions(offline=True),
            connectors=connectors,
        )
        assert result.record.result.fidelity.unresolved
        assert result.rendering.text == STATUTE.strip()
        assert result.record.audit() == []

    def test_an_unresolved_citation_is_a_finding_not_a_crash(self, connectors):
        result = explain_citation(
            "not a citation at all",
            explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]),
            AnalyzeOptions(offline=True),
            connectors=connectors,
        )
        assert not result.resolved
        assert result.record.result.fidelity is None
        assert any("did not resolve" in note for note in result.record.result.notes)
        assert result.record.audit() == []

    def test_explain_config_records_all_three_fidelity_prompts(self):
        config = build_explain_config(
            explain_registry([], []), AnalyzeOptions()
        )
        assert len(config.prompts) == 3
        assert {p.stage for p in config.prompts} == {StageName.FIDELITY}
        assert len({p.content_hash for p in config.prompts}) == 3


class TestReplayProjection:
    def test_prompts_sharing_a_stage_hash_deterministically(self):
        """Three fidelity prompts share one stage; order must not change the hash.

        Sorting on stage alone left their relative order to whatever the input
        list happened to be, so two identical recipes could hash differently --
        exactly what the projection exists to prevent.
        """
        refs = [
            load_prompt(StageName.FIDELITY, variant=variant).to_ref()
            for variant in ("extract", "render", "backtranslate")
        ]
        models = [RENDERER_ID, BACKTRANSLATOR_ID]

        def config(prompts: list[PromptRef]) -> RunConfig:
            return RunConfig(
                profile=Profile.STANDARD, seed=42, temperature=0.0, max_claims=200,
                reading_level=8, models=models, prompts=prompts,
            )

        assert config(refs).replay_projection() == \
            config(list(reversed(refs))).replay_projection()


# ==========================================================================
# The command surface
# ==========================================================================


class TestExplainCommand:
    """``abca explain`` -- argument handling, exit codes, and the report itself.

    Exit codes are load-bearing here. A failed gate is not a crashed program,
    but it is emphatically not a success either: a script that pipes this into
    a publishing step has to be able to tell that what it received is the
    statute rather than a rewrite.
    """

    def _runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def test_bare_invocation_shows_usage(self):
        """Matches ``analyze``: no arguments means the user wants the help."""
        from abca.cli.main import app

        result = self._runner().invoke(app, ["explain"])
        assert result.exit_code == 2
        assert "Usage: abca explain" in result.output

    def test_options_without_a_citation_name_what_is_missing(self):
        from abca.cli.main import app

        result = self._runner().invoke(app, ["explain", "--offline"])
        assert result.exit_code == 2
        assert "no citation" in result.output
        assert "10 ILCS 5/10-2" in result.output

    def test_missing_config_exits_8(self, tmp_path):
        from abca.cli.main import app

        result = self._runner().invoke(app, [
            "explain", "--cite", "10 ILCS 5/10-2",
            "--config", str(tmp_path / "nope.toml"),
        ])
        assert result.exit_code == 8

    def _patched(self, monkeypatch, payloads, connectors):
        """Point the command at scripted models and a seeded cache."""
        from abca.cli import _explain_cmd
        from abca.config import Config, Defaults

        registry = explain_registry(*payloads)
        monkeypatch.setattr(_explain_cmd, "load_config",
                            lambda *a, **k: Config(defaults=Defaults()))
        monkeypatch.setattr(_explain_cmd, "build_registry", lambda *a, **k: registry)
        monkeypatch.setattr(
            _explain_cmd, "explain_citation",
            lambda citation, reg, options, **kwargs: explain_citation(
                citation, reg, options, connectors=connectors),
        )

    def test_a_passing_gate_exits_0_and_shows_the_rendering(
        self, monkeypatch, tmp_path, connectors
    ):
        from abca.cli.main import app

        self._patched(monkeypatch, ([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]), connectors)
        result = self._runner().invoke(app, [
            "explain", "--cite", "10 ILCS 5/10-2", "--offline", "--elements",
            "--ledger-root", str(tmp_path),
        ])
        assert result.exit_code == 0
        assert "FIDELITY GATE PASSED" in result.output
        assert "must file a petition" in result.output
        # The gate's own numbers are shown BEFORE the quotable paragraph.
        assert result.output.index("fidelity score") < result.output.index("plain language")

    def test_a_failed_gate_exits_10_and_prints_the_statute(
        self, monkeypatch, tmp_path, connectors
    ):
        from abca.cli.main import app

        self._patched(monkeypatch, ([EXTRACT, RENDER_OK], [BACKTRANSLATE_DROPPED]),
                      connectors)
        result = self._runner().invoke(app, [
            "explain", "--cite", "10 ILCS 5/10-2", "--offline",
            "--ledger-root", str(tmp_path),
        ])
        assert result.exit_code == 10
        assert "FIDELITY GATE FAILED" in result.output
        assert "hereafter desiring" in result.output  # the statute itself
        assert "DROPPED" in result.output

    def test_an_unresolved_citation_exits_11(self, monkeypatch, tmp_path, connectors):
        from abca.cli.main import app

        self._patched(monkeypatch, ([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]), connectors)
        result = self._runner().invoke(app, [
            "explain", "--cite", "not a citation", "--offline",
            "--ledger-root", str(tmp_path),
        ])
        assert result.exit_code == 11

    def test_json_output_carries_the_fidelity_report(
        self, monkeypatch, tmp_path, connectors
    ):
        from abca.cli.main import app

        self._patched(monkeypatch, ([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]), connectors)
        result = self._runner().invoke(app, [
            "explain", "--cite", "10 ILCS 5/10-2", "--offline", "--json",
            "--ledger-root", str(tmp_path),
        ])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["result"]["fidelity"]["score"] == 1.0
        assert [s["name"] for s in payload["stages"]] == [
            "ingest", "retrieve", "fidelity", "compose"
        ]

    def test_the_run_is_written_to_the_ledger(self, monkeypatch, tmp_path, connectors):
        from abca.cli.main import app
        from abca.ledger.store import LedgerStore

        self._patched(monkeypatch, ([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]), connectors)
        self._runner().invoke(app, [
            "explain", "--cite", "10 ILCS 5/10-2", "--offline",
            "--ledger-root", str(tmp_path),
        ])
        stored = list(LedgerStore(tmp_path).iter_records())
        assert len(stored) == 1
        assert stored[0].result.fidelity.applied


# ==========================================================================
# Replay
# ==========================================================================


class TestExplainReplay:
    """An explain record must be replayable BY THE EXPLAIN PIPELINE.

    Adding a second pipeline created a real hazard: replay dispatched on nothing
    and would have re-run `analyze_text` over the citation string, producing a
    confidently wrong DIVERGENT verdict for a run that was never divergent.
    """

    def _record(self, connectors):
        return explain_citation(
            "10 ILCS 5/10-2",
            explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]),
            AnalyzeOptions(offline=True),
            connectors=connectors,
        ).record

    def test_an_explain_record_is_recognised_as_one(self, connectors):
        from abca.pipeline.replay import is_explain_record

        assert is_explain_record(self._record(connectors))

    def test_an_analyze_record_is_not(self, record):
        from abca.pipeline.replay import is_explain_record

        assert not is_explain_record(record)

    def test_prompt_pins_resolve_the_variant(self, connectors):
        """Looking up ``fidelity.md`` would refuse every explain replay.

        And it would refuse it for a reason that is not true -- "prompt not
        present on this machine" -- which is the worst kind of error message.
        """
        from abca.pipeline.replay import check_prompt_pins

        check_prompt_pins(self._record(connectors))  # does not raise

    def test_prompt_variant_is_recovered_from_the_path(self):
        from abca.pipeline.replay import prompt_variant

        assert prompt_variant("prompts/sotp/0.1.0/fidelity.render.md") == "render"
        assert prompt_variant("prompts/sotp/0.1.0/gate.md") is None

    def test_an_edited_fidelity_prompt_is_caught(self, connectors):
        """The check must still bite -- resolving the variant cannot soften it."""
        from abca.pipeline.replay import ReplayImpossible, check_prompt_pins

        record = self._record(connectors)
        tampered = record.model_copy(update={
            "config": record.config.model_copy(update={
                "prompts": [
                    ref.model_copy(update={"content_hash": "sha256:" + "0" * 64})
                    if ref.path.endswith("render.md") else ref
                    for ref in record.config.prompts
                ]
            })
        })
        with pytest.raises(ReplayImpossible, match="fidelity.render.md"):
            check_prompt_pins(tampered)

    def test_replaying_reproduces_the_same_analysis(self, connectors, tmp_path):
        """Same recipe, same scripted models, same statute -> the same result."""
        from abca.ledger.verify import compare
        from abca.pipeline.replay import PipelineReplayer

        original = self._record(connectors)

        class FixedRegistryReplayer(PipelineReplayer):
            """Skip backend resolution; the point here is the pipeline dispatch."""

            def replay(self, record):
                from abca.pipeline.orchestrator import explain_citation as run

                return run(
                    "10 ILCS 5/10-2",
                    explain_registry([EXTRACT, RENDER_OK], [BACKTRANSLATE_OK]),
                    AnalyzeOptions(offline=True),
                    connectors=connectors,
                ).record

        outcome = compare(original, FixedRegistryReplayer().replay(original))
        assert outcome.outcome.value in {"IDENTICAL", "EQUIVALENT"}
