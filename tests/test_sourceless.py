"""The sourceless module's invariants (doc 18).

Nearly every test here asserts that something is REFUSED. That is the shape the
module needs: it exists to reason about a claim with no source, and the entire
risk is that its reasoning gets mistaken for evidence. So the suite is mostly a
list of ways to try to smuggle a guess into the evidence path, each one blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from abca.schema.core import Citation
from abca.schema.enums import ClaimType, SourceTier, Verdict
from abca.schema.sourceless import (
    ALLOWED_DISPOSITIONS,
    AtomicClaim,
    BranchOutcome,
    ClaimSubtype,
    DomainFailurePattern,
    Fallacy,
    ReferentCandidate,
    ReferentConfidence,
    Register,
    RetrievalPlan,
    Severity,
    SourcelessAnalysis,
    StructuralConflict,
    StructuralFlag,
    Verifiability,
)
from abca.sourceless.distortions import (
    DISTORTION_TAXONOMY,
    TAXONOMY_VERSION,
    distortion_by_name,
    domains_for,
    rank_candidates,
)
from abca.sourceless.trigger import (
    ReferenceKindFound,
    find_external_references,
    is_sourceless,
    should_dissect,
)


def t0_citation(quote: str = "the House shall not do the thing") -> Citation:
    return Citation(
        tier=SourceTier.T0,
        title="Rules of the House of Representatives, Rule XIX",
        url="https://rules.house.gov/rule-xix",
        quote=quote,
        retrieved_at=datetime.now(UTC),
        content_hash="sha256:" + "a" * 64,
    )


def analysis(**kw) -> SourcelessAnalysis:
    base = dict(denatured_claim="Congress voted to end veterans' benefits.")
    base.update(kw)
    return SourcelessAnalysis(**base)


def atom(aid="a-001", subtype=ClaimSubtype.PROCEDURAL,
         verifiability=Verifiability.VERIFIABLE_PRIMARY, load_bearing=True) -> AtomicClaim:
    return AtomicClaim(
        id=aid, text="Congress voted to end benefits.", claim_type=ClaimType.LEGAL,
        subtype=subtype, verifiability=verifiability, load_bearing=load_bearing,
    )


# ==========================================================================
# Invariant 1 — the disposition vocabulary
# ==========================================================================


class TestDispositionVocabulary:
    def test_the_four_reachable_dispositions(self):
        assert {
            Verdict.UNSUPPORTED, Verdict.UNVERIFIABLE,
            Verdict.CONTRADICTED, Verdict.OUT_OF_SCOPE,
        } == ALLOWED_DISPOSITIONS

    @pytest.mark.parametrize("verdict", [
        Verdict.SUPPORTED, Verdict.MIXED,
        Verdict.MISLEADING_CONTEXT, Verdict.REPORTED_UNVERIFIED,
    ])
    def test_evidence_bearing_verdicts_are_unreachable(self, verdict):
        """Each of these requires evidence the module by definition lacks."""
        with pytest.raises(ValueError, match="not reachable from the sourceless module"):
            analysis(disposition=verdict)

    def test_the_error_names_misleading_context_specifically(self):
        """It is the verdict people will most want, so the message explains it."""
        with pytest.raises(ValueError, match="challenge lane"):
            analysis(disposition=Verdict.MISLEADING_CONTEXT)

    def test_a_branch_outcome_cannot_promise_an_unreachable_disposition(self):
        with pytest.raises(ValueError, match="cannot emit"):
            BranchOutcome(if_found="the roll call", then_disposition=Verdict.SUPPORTED)


# ==========================================================================
# Invariant 2 — CONTRADICTED needs a cited conflict
# ==========================================================================


class TestContradictedGate:
    def test_contradicted_without_a_conflict_is_refused(self):
        with pytest.raises(ValueError, match="requires a structural_conflict"):
            analysis(disposition=Verdict.CONTRADICTED)

    def test_contradicted_with_a_t0_conflict_is_allowed(self):
        a = analysis(
            disposition=Verdict.CONTRADICTED,
            structural_conflict=(StructuralConflict(conflict="chamber lacks the power",
                                                    citation=t0_citation()),),
            evidence_quality=SourceTier.T0,
        )
        assert a.disposition is Verdict.CONTRADICTED
        assert a.carries_evidence

    @pytest.mark.parametrize("tier", [SourceTier.T1, SourceTier.T2, SourceTier.T3, SourceTier.T4])
    def test_a_conflict_below_t0_is_refused(self, tier):
        """A claim about what the rules ARE must rest on the rule text."""
        cite = t0_citation().model_copy(update={"tier": tier})
        with pytest.raises(ValueError, match="T0 is required"):
            StructuralConflict(conflict="x", citation=cite)

    def test_an_uncited_intuition_has_somewhere_honest_to_go(self):
        flag = StructuralFlag(flag="the Senate cannot do that",
                              would_be_established_by="Senate Standing Rule XXII")
        assert flag.register is Register.INFERRED

    def test_a_flag_must_name_what_would_establish_it(self):
        """A hunch that cannot name its document is not actionable."""
        with pytest.raises(ValueError):
            StructuralFlag(flag="that seems impossible", would_be_established_by="")

    def test_a_flag_cannot_be_marked_established(self):
        with pytest.raises(ValueError, match="belongs in structural_conflict"):
            StructuralFlag(flag="x", would_be_established_by="y",
                           register=Register.ESTABLISHED)


# ==========================================================================
# Invariant 3 — evidence quality is pinned
# ==========================================================================


class TestEvidenceQualityPinning:
    def test_default_is_t4(self):
        assert analysis().evidence_quality is SourceTier.T4

    @pytest.mark.parametrize("tier", [SourceTier.T0, SourceTier.T1, SourceTier.T2, SourceTier.T3])
    def test_promoting_tier_without_a_conflict_is_refused(self, tier):
        """Reasoning about a sourceless post does not raise its tier."""
        with pytest.raises(ValueError, match="does not raise its tier"):
            analysis(evidence_quality=tier)

    def test_a_cited_conflict_requires_t0_and_nothing_else(self):
        conflict = (StructuralConflict(conflict="c", citation=t0_citation()),)
        with pytest.raises(ValueError, match="must be T0"):
            analysis(structural_conflict=conflict, evidence_quality=SourceTier.T4)


# ==========================================================================
# Invariant 4 — citations live in exactly one place
# ==========================================================================


class TestCitationContainment:
    def test_a_clean_analysis_has_no_citation_sites(self):
        assert analysis().citation_sites() == []

    def test_the_only_citation_site_is_the_structural_conflict(self):
        a = analysis(
            disposition=Verdict.CONTRADICTED,
            evidence_quality=SourceTier.T0,
            structural_conflict=(StructuralConflict(conflict="c", citation=t0_citation()),),
            referent_candidates=(ReferentCandidate(
                candidate="H.R. 1234 motion to recommit",
                referent_confidence=ReferentConfidence.MEDIUM,
                distortion_applied="committee vote -> floor vote",
                reasoning="closest single-step transformation"),),
            domain_failure_patterns=(DomainFailurePattern(
                pattern="procedural framed as substantive", span="voted to gut",
                explanation="the vote was on a motion"),),
        )
        sites = a.citation_sites()
        assert sites == ["structural_conflict[0].citation"], sites

    def test_a_referent_candidate_has_no_citation_field_at_all(self):
        """Not 'must be empty' -- the field does not exist. A guess cannot cite."""
        assert "citation" not in ReferentCandidate.model_fields
        assert "citations" not in ReferentCandidate.model_fields

    def test_a_candidate_cannot_be_marked_established(self):
        with pytest.raises(ValueError, match="hypothesis and must be INFERRED"):
            ReferentCandidate(candidate="x", referent_confidence=ReferentConfidence.HIGH,
                              distortion_applied="title_to_text", reasoning="r",
                              register=Register.ESTABLISHED)

    def test_citation_sites_walks_nested_structures(self):
        """The probe must find a citation anywhere, not just where we expect one."""
        a = analysis(
            disposition=Verdict.CONTRADICTED,
            evidence_quality=SourceTier.T0,
            structural_conflict=(
                StructuralConflict(conflict="one", citation=t0_citation("first")),
                StructuralConflict(conflict="two", citation=t0_citation("second")),
            ),
        )
        assert a.citation_sites() == [
            "structural_conflict[0].citation", "structural_conflict[1].citation",
        ]


# ==========================================================================
# Registers and the hard rules
# ==========================================================================


class TestRegisters:
    def test_only_established_is_evidence(self):
        assert [r for r in Register if r.is_evidence] == [Register.ESTABLISHED]

    def test_an_atom_is_always_what_the_post_said(self):
        with pytest.raises(ValueError, match="register must be ASSERTED"):
            AtomicClaim(id="a-001", text="x", claim_type=ClaimType.LEGAL,
                        subtype=ClaimSubtype.PROCEDURAL,
                        verifiability=Verifiability.VERIFIABLE_PRIMARY,
                        load_bearing=True, register=Register.ESTABLISHED)

    def test_m7_a_motive_atom_must_be_unfalsifiable(self):
        with pytest.raises(ValueError, match="unfalsifiable"):
            atom(subtype=ClaimSubtype.MOTIVE,
                 verifiability=Verifiability.VERIFIABLE_PRIMARY)

    def test_m7_an_all_motive_analysis_routes_to_unverifiable(self):
        motive = atom(subtype=ClaimSubtype.MOTIVE,
                      verifiability=Verifiability.UNFALSIFIABLE)
        with pytest.raises(ValueError, match="must be UNVERIFIABLE"):
            analysis(atomic_claims=(motive,), disposition=Verdict.UNSUPPORTED)
        assert analysis(atomic_claims=(motive,), disposition=Verdict.UNVERIFIABLE)

    def test_m4_confident_unsupported_needs_a_rationale(self):
        """So the reader knows it means 'confident nothing supports this'."""
        with pytest.raises(ValueError, match="not in the claim being false"):
            analysis(disposition=Verdict.UNSUPPORTED, confidence=0.9)
        assert analysis(disposition=Verdict.UNSUPPORTED, confidence=0.9,
                        confidence_rationale="confident no source exists")

    def test_m10_media_is_never_analyzed(self):
        with pytest.raises(ValueError, match="text-only by decision"):
            analysis(media_analyzed=True)

    def test_media_present_is_allowed_and_recorded(self):
        """A post CAN carry an image; the module just never reads it."""
        a = analysis(media_present=True)
        assert a.media_present and not a.media_analyzed

    def test_m9_there_is_nowhere_to_put_an_author(self):
        """A schema with no handle field cannot grow a dossier."""
        from abca.schema.sourceless import PostMetadata
        for banned in ("author", "handle", "account", "username", "user", "poster"):
            assert banned not in PostMetadata.model_fields

    def test_fallacies_have_no_path_to_the_disposition(self):
        """A claim can be true and fallaciously argued."""
        a = analysis(fallacies=(Fallacy(name="straw man", span="s", mechanism="m",
                                        severity=Severity.DISQUALIFYING),))
        assert a.disposition is Verdict.UNSUPPORTED, "a fallacy must not move the verdict"


# ==========================================================================
# The trigger
# ==========================================================================


class TestTrigger:
    @pytest.mark.parametrize("text", [
        "Congress just voted to gut veterans' benefits.",
        "They passed a law making it illegal. Wake up people.",
        "The Senate blocked the bill everyone wanted.",
    ])
    def test_bare_posts_are_sourceless(self, text):
        assert is_sourceless(text)

    @pytest.mark.parametrize("text,kind", [
        ("Under 10 ILCS 5/10-2 you need signatures.", ReferenceKindFound.CITATION),
        ("28 CFR 545.11 sets the order.", ReferenceKindFound.CITATION),
        ("18 U.S.C. 42 bans it.", ReferenceKindFound.CITATION),
        ("H.R. 1234 passed.", ReferenceKindFound.CITATION),
        ("Pub. L. 117-58 funded it.", ReferenceKindFound.CITATION),
        ("See https://congress.gov/x for text.", ReferenceKindFound.URL),
        ("Read it at www.gao.gov/report", ReferenceKindFound.URL),
        ("The CBO scored it at $2T.", ReferenceKindFound.PUBLICATION),
        ("According to the inspector general report, funds were misused.",
         ReferenceKindFound.QUOTED_DOCUMENT),
    ])
    def test_references_are_detected_by_kind(self, text, kind):
        found = find_external_references(text)
        assert found, f"nothing detected in {text!r}"
        assert kind in {r.kind for r in found}

    def test_detection_is_tolerant_by_design(self):
        """False-sourceless costs a wasted pass; false-sourced costs the answer."""
        assert not is_sourceless("see reuters.com/article/x")

    def test_spans_are_returned_so_a_report_can_show_them(self):
        text = "Under 10 ILCS 5/10-2 you need signatures."
        ref = find_external_references(text)[0]
        assert text[ref.start:ref.end] == ref.text

    def test_dissection_skips_non_verdict_eligible_types(self):
        d = should_dissect("It is unjust.", claim_type_is_verdict_eligible=False)
        assert not d.dissect and "not verdict-eligible" in d.reason

    def test_dissection_skips_image_only_content(self):
        d = should_dissect("x", claim_type_is_verdict_eligible=True, image_only=True)
        assert not d.dissect and "image_only_content" in d.reason

    def test_dissection_skips_a_claim_that_failed_the_gate(self):
        d = should_dissect("x", claim_type_is_verdict_eligible=True, survived_gate=False)
        assert not d.dissect

    def test_a_negative_decision_always_carries_a_reason(self):
        """'We did not reconstruct this' is itself checkable."""
        for kwargs in (
            dict(claim_type_is_verdict_eligible=False),
            dict(claim_type_is_verdict_eligible=True, image_only=True),
            dict(claim_type_is_verdict_eligible=True, survived_gate=False),
        ):
            assert should_dissect("H.R. 1", **kwargs).reason

    def test_a_sourced_claim_reports_which_kinds_were_found(self):
        d = should_dissect("H.R. 1234 passed, see https://congress.gov/x",
                           claim_type_is_verdict_eligible=True)
        assert not d.dissect
        assert "citation" in d.reason and "url" in d.reason
        assert d.references


# ==========================================================================
# The distortion taxonomy
# ==========================================================================


class TestDistortionTaxonomy:
    def test_it_is_versioned(self):
        assert TAXONOMY_VERSION.startswith("distortions/")

    def test_names_are_unique(self):
        names = [d.name for d in DISTORTION_TAXONOMY]
        assert len(names) == len(set(names))

    def test_labels_are_unique(self):
        labels = [d.label for d in DISTORTION_TAXONOMY]
        assert len(labels) == len(set(labels))

    def test_lookup_by_name_and_by_wire_label(self):
        assert distortion_by_name("committee_to_floor").name == "committee_to_floor"
        assert distortion_by_name("committee vote -> floor vote").name == "committee_to_floor"

    def test_an_unknown_distortion_coerces_rather_than_raising(self):
        """Coverage gaps must stay visible, not destroy the analysis."""
        assert distortion_by_name("no such thing").name == "other"

    def test_a_unicode_arrow_still_resolves(self):
        assert distortion_by_name("committee vote → floor vote").name == "committee_to_floor"

    def test_the_smallest_distortion_rule_is_arithmetic(self):
        """Doc 18 s2b, applied rather than merely stated."""
        ranked = rank_candidates(["committee_procedural_to_policy", "title_to_text"])
        assert [d.name for d in ranked] == ["title_to_text", "committee_procedural_to_policy"]
        assert ranked[0].steps < ranked[1].steps

    def test_other_sorts_last(self):
        ranked = rank_candidates(["other", "title_to_text"])
        assert ranked[-1].name == "other"

    def test_every_entry_maps_to_a_context_pack_domain(self):
        """The join to doc 19: a distortion selects its explaining entries."""
        valid = {"procedure", "lawmaking", "budget", "instruments", "elections", "statistics"}
        for d in DISTORTION_TAXONOMY:
            assert d.domain in valid, f"{d.name} -> unknown domain {d.domain}"

    def test_domains_are_deduplicated_in_rank_order(self):
        assert domains_for(["level_as_rate", "committee_to_floor", "baseline_shifted"]) == [
            "statistics", "lawmaking",
        ]

    def test_every_entry_explains_why_the_distortion_happens(self):
        for d in DISTORTION_TAXONOMY:
            assert len(d.note) > 40, f"{d.name} has no explanatory note"


# ==========================================================================
# Hygiene
# ==========================================================================


class TestHygiene:
    def test_importing_the_schema_emits_no_warnings(self):
        """The `register` shadow filter is narrow; a real shadow must still surface.

        Checked in a SUBPROCESS rather than with importlib.reload. Reloading the
        module rebuilds its enum classes, so the ``is`` comparisons in the
        validators would then be checking identity against the stale classes this
        test module imported -- which fails, and poisons every later test in the
        file. A fresh interpreter is also closer to what a user actually does.
        """
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-W", "error::UserWarning", "-c",
             "import abca.schema.sourceless as m; print(m.SourcelessAnalysis.__name__)"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            "importing abca.schema.sourceless raised a UserWarning:\n"
            + result.stderr[-800:]
        )

    def test_the_register_field_survives_serialization(self):
        payload = atom().to_jsonable()
        assert payload["register"] == "ASSERTED"

    def test_a_full_analysis_round_trips_through_json(self):
        a = analysis(
            atomic_claims=(atom(),),
            referent_candidates=(ReferentCandidate(
                candidate="motion to recommit on H.R. 1234",
                referent_confidence=ReferentConfidence.MEDIUM,
                distortion_applied="procedural vote -> substantive vote",
                reasoning="single-step transformation fits"),),
            retrieval_plan=RetrievalPlan(
                queries=("H.R. 1234 motion to recommit roll call",),
                decisive_artifact="the House roll call record",
                branch_outcomes=(BranchOutcome(if_found="the roll call",
                                               then_disposition=Verdict.UNSUPPORTED),)),
        )
        restored = SourcelessAnalysis.model_validate(a.to_jsonable())
        assert restored == a

    def test_summary_reports_the_counts_an_operator_needs(self):
        s = analysis(atomic_claims=(atom(), atom("a-002", load_bearing=False))).summary()
        assert s["atoms"] == 2 and s["load_bearing"] == 1


# ==========================================================================
# Wiring: the stage, the prompt family, and the versioning consequence
# ==========================================================================


class TestWiring:
    def test_the_stage_exists_and_sits_where_doc_18_puts_it(self):
        """Doc 18 s3: after cluster, before retrieve."""
        from abca.schema.enums import STAGE_ORDER, StageName

        order = list(STAGE_ORDER)
        assert StageName.SOURCELESS in order
        assert order.index(StageName.CLUSTER) < order.index(StageName.SOURCELESS)
        assert order.index(StageName.SOURCELESS) < order.index(StageName.RETRIEVE)

    def test_the_prompt_loads_from_its_own_family(self):
        """The module versions independently of the sotp contract."""
        from abca.prompts import load_prompt
        from abca.schema.enums import StageName

        prompt = load_prompt(StageName.SOURCELESS, contract="0.1.0", family="sourceless")
        assert prompt.path == "prompts/sourceless/0.1.0/sourceless.md"
        assert prompt.content_hash.startswith("sha256:")
        assert len(prompt.text) > 2000

    def test_the_default_family_is_unchanged(self):
        """Every existing caller must keep resolving to sotp."""
        from abca.prompts import prompt_path
        from abca.schema.enums import StageName

        assert "/sotp/" in prompt_path(StageName.CLASSIFY).as_posix()

    def test_a_missing_prompt_names_the_family_it_looked_in(self):
        from abca.prompts import PromptNotFound, load_prompt
        from abca.schema.enums import StageName

        with pytest.raises(PromptNotFound, match="nosuchfamily"):
            load_prompt(StageName.CLASSIFY, contract="0.1.0", family="nosuchfamily")

    def test_the_prompt_carries_no_party_doctrine(self):
        """The sourceless prompt is covered by the same guard as every other."""
        from abca.prompts import PROMPTS_ROOT

        text = (PROMPTS_ROOT / "sourceless" / "0.1.0" / "sourceless.md").read_text(
            encoding="utf-8"
        ).lower()
        for term in ("mongoose", "verdigris", "abca", "check it yourself"):
            assert term not in text

    def test_the_prompt_states_the_no_evidence_rule(self):
        """The single most important instruction in the file."""
        from abca.prompts import PROMPTS_ROOT

        text = (PROMPTS_ROOT / "sourceless" / "0.1.0" / "sourceless.md").read_text(
            encoding="utf-8"
        )
        assert "never evidence" in text.lower()
        assert "Unsourced is not false" in text

    def test_adding_the_stage_bumped_the_schema_version_not_the_contract(self):
        """StageName is persisted shape, not adjudication meaning.

        Bumping the contract for a pipeline change would make unrelated
        historical runs look incomparable; not bumping the schema would let two
        records with different stage vocabularies be byte-compared.
        """
        from abca.version import PROMPT_CONTRACT_VERSION, SCHEMA_VERSION

        assert SCHEMA_VERSION >= "1.2.0"
        assert PROMPT_CONTRACT_VERSION == "sotp/0.1.0"

    def test_the_prompt_ships_inside_the_wheel(self):
        """force-include covers the whole prompts tree, so a new family is carried."""
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(
            (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        assert "src/abca/prompts" in include


# ==========================================================================
# The stage
# ==========================================================================


class FailingProvider:
    """A provider that always errors, to exercise the fail-closed path."""

    name = "failing"

    def generate(self, request):
        from abca.providers.base import ProviderError

        raise ProviderError("backend unreachable")


class TestStage:
    def test_a_sourced_claim_is_skipped_with_a_reason(self):
        from abca.pipeline.sourceless import run_sourceless

        out = run_sourceless(FailingProvider(), "Under 10 ILCS 5/10-2 you need signatures.")
        assert out.value is None
        assert any("skipped" in n for n in out.notes)

    def test_a_non_verdict_eligible_claim_is_skipped(self):
        from abca.pipeline.sourceless import run_sourceless

        out = run_sourceless(FailingProvider(), "This is unjust.",
                             claim_type_is_verdict_eligible=False)
        assert out.value is None

    def test_image_only_content_is_skipped(self):
        from abca.pipeline.sourceless import run_sourceless

        out = run_sourceless(FailingProvider(), "bare claim", image_only=True)
        assert out.value is None
        assert any("image_only_content" in n for n in out.notes)

    def test_a_failed_call_fails_closed_rather_than_guessing(self):
        """A wrong referent sends retrieval after the wrong document."""
        from abca.pipeline.sourceless import run_sourceless

        out = run_sourceless(FailingProvider(), "Congress just voted to gut benefits.")
        analysis = out.value
        assert analysis is not None
        assert analysis.disposition is Verdict.UNSUPPORTED
        assert analysis.referent_confidence is ReferentConfidence.NONE
        assert analysis.referent_candidates == ()
        assert analysis.retrieval_plan.queries == ()
        assert analysis.evidence_quality is SourceTier.T4
        assert "did not run" in analysis.handoff_notes

    def test_the_fallback_still_carries_the_claim_and_its_hash(self):
        from abca.canonical import digest_text
        from abca.pipeline.sourceless import run_sourceless

        text = "Congress just voted to gut benefits."
        analysis = run_sourceless(FailingProvider(), text).value
        assert analysis.denatured_claim == text
        assert analysis.denatured_claim_hash == digest_text(text)

    def test_the_prompt_labels_context_as_not_evidence(self):
        from abca.pipeline.sourceless import build_dissect_prompt

        built = build_dissect_prompt("PROMPT", "the post", platform="facebook")
        assert "Context (not evidence)" in built
        assert "Platform: facebook" in built

    def test_context_is_omitted_entirely_when_absent(self):
        from abca.pipeline.sourceless import build_dissect_prompt

        assert "Context (not evidence)" not in build_dissect_prompt("PROMPT", "the post")

    def test_the_stage_takes_one_claim_not_a_batch(self):
        """Batching invites one post's referent to contaminate another's."""
        import inspect

        from abca.pipeline.sourceless import run_sourceless

        params = inspect.signature(run_sourceless).parameters
        assert "claim_text" in params
        assert not any(p == "claims" for p in params)


class ScriptedProvider:
    """Returns one canned JSON payload, so the success path is exercised."""

    name = "scripted"

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def generate(self, request):
        from abca.providers.base import Completion
        from abca.schema.ledger import ModelIdentity

        return Completion(
            text=self.payload,
            identity=ModelIdentity(
                role="sourceless", provider="scripted", name="scripted-1",
                weights_hash="sha256:" + "b" * 64,
            ),
            prompt_tokens=10,
            completion_tokens=20,
        )


def scripted_analysis_json(distortions: list[str]) -> str:
    """A minimal but valid SourcelessAnalysis payload with N candidates."""
    import json

    return json.dumps({
        "denatured_claim": "Congress voted to end veterans' benefits.",
        "referent_candidates": [
            {
                "candidate": f"candidate for {d}",
                "referent_confidence": "medium",
                "distortion_applied": d,
                "reasoning": "fits the wording",
                "register": "INFERRED",
            }
            for d in distortions
        ],
        "retrieval_plan": {
            "queries": ["motion to recommit roll call veterans"],
            "decisive_artifact": "the House roll call record",
            "branch_outcomes": [
                {"if_found": "a roll call on a motion", "then_disposition": "UNSUPPORTED"}
            ],
        },
    })


class TestStageSuccessPath:
    def test_provenance_is_stamped_from_what_actually_ran(self):
        from abca.pipeline.sourceless import MODULE_VERSION, run_sourceless

        provider = ScriptedProvider(scripted_analysis_json(["title_to_text"]))
        a = run_sourceless(provider, "Congress just voted to gut benefits.").value
        assert a.module_version == MODULE_VERSION
        assert a.prompt_hash.startswith("sha256:")

    def test_the_denatured_hash_is_computed_not_accepted(self):
        """Doc 20 s4 refuses promotion when this hash moves, so it must be ours."""
        from abca.canonical import digest_text
        from abca.pipeline.sourceless import run_sourceless

        provider = ScriptedProvider(scripted_analysis_json(["title_to_text"]))
        a = run_sourceless(provider, "Congress just voted to gut benefits.").value
        assert a.denatured_claim_hash == digest_text("Congress voted to end veterans' benefits.")

    def test_candidates_are_reordered_smallest_distortion_first(self):
        """Doc 18 s2b applied arithmetically, not left to the model to remember."""
        from abca.pipeline.sourceless import run_sourceless

        provider = ScriptedProvider(
            scripted_analysis_json(["committee_procedural_to_policy", "title_to_text"])
        )
        a = run_sourceless(provider, "Congress just voted to gut benefits.").value
        assert [c.distortion_applied for c in a.referent_candidates] == [
            "title_to_text", "committee_procedural_to_policy",
        ]

    def test_an_unrecognised_distortion_is_reported_not_hidden(self):
        from abca.pipeline.sourceless import run_sourceless

        provider = ScriptedProvider(scripted_analysis_json(["some_novel_transformation"]))
        out = run_sourceless(provider, "Congress just voted to gut benefits.")
        assert any("unrecognised" in n for n in out.notes)
        assert len(out.value.referent_candidates) == 1, "it must still be kept"

    def test_a_missing_branch_outcome_warns_about_the_promotion_gate(self):
        import json

        from abca.pipeline.sourceless import run_sourceless

        payload = json.loads(scripted_analysis_json(["title_to_text"]))
        payload["retrieval_plan"]["branch_outcomes"] = []
        out = run_sourceless(ScriptedProvider(json.dumps(payload)),
                             "Congress just voted to gut benefits.")
        assert any("promotion gate" in n for n in out.notes)

    def test_the_success_path_still_cannot_raise_the_tier(self):
        from abca.pipeline.sourceless import run_sourceless

        provider = ScriptedProvider(scripted_analysis_json(["title_to_text"]))
        a = run_sourceless(provider, "Congress just voted to gut benefits.").value
        assert a.evidence_quality is SourceTier.T4

    def test_citations_appear_only_where_a_source_actually_is(self):
        """The containment rule, stated exactly.

        Two sites are legitimate: a structural conflict (a quoted rule) and an
        institutional context entry (an ordinary corpus source). Both are real
        documents. What the rule forbids is a citation attached to the MODULE'S
        OWN REASONING -- a referent candidate, a failure pattern, a fallacy --
        because that is a guess wearing a source's authority.
        """
        from abca.pipeline.sourceless import run_sourceless

        provider = ScriptedProvider(scripted_analysis_json(["title_to_text"]))
        a = run_sourceless(provider, "Congress just voted to gut benefits.").value
        for site in a.citation_sites():
            assert site.startswith(("structural_conflict[", "institutional_context[")), site

    def test_the_corpus_is_pinned_even_when_nothing_was_selected(self):
        """'Nothing applied' is a claim about a specific corpus version."""
        from abca.pipeline.sourceless import attach_context
        from abca.schema.sourceless import SourcelessAnalysis

        a = attach_context(SourcelessAnalysis(denatured_claim="a bare claim"))
        assert a.institutional_context == ()
        assert a.context_pack_hash.startswith("sha256:")
        assert a.context_pack_version.startswith("corpus/institutional/")

    def test_a_broken_corpus_thins_the_brief_rather_than_aborting_the_run(self):
        """A missing explanation beats no analysis at all."""
        import abca.context_pack.corpus as corpus_module
        from abca.context_pack.corpus import CorpusError
        from abca.pipeline import sourceless as stage
        from abca.schema.sourceless import SourcelessAnalysis

        original = corpus_module.load_corpus
        corpus_module.load_corpus = lambda *a, **k: (_ for _ in ()).throw(
            CorpusError("simulated")
        )
        try:
            a = stage.attach_context(SourcelessAnalysis(denatured_claim="a bare claim"))
        finally:
            corpus_module.load_corpus = original
        assert a.institutional_context == ()
        assert a.context_pack_hash == ""
