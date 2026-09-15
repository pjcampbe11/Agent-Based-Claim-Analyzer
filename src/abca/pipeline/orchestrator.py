"""Wire the stages together and produce an auditable run record.

WHAT THIS MODULE OWNS
=====================
Three things, and it is worth being explicit because each is a place where
honesty could quietly leak out of the system:

1. **Stage sequencing and recording.** Every stage runs inside
   :meth:`~abca.ledger.recorder.RunRecorder.stage`, so the hash chain covers
   the real execution rather than a summary written afterwards.

2. **The DraftClaim to Claim conversion.** This is the ONLY place a published
   verdict is assigned. Doing it in one function rather than scattering it
   across stages means the placeholder logic below can be read, reviewed and
   changed in one place when adjudication lands.

3. **The honesty of an unfinished pipeline.** See below.

REPORTING AN UNFINISHED PIPELINE HONESTLY
=========================================
Retrieval and adjudication do not exist yet (build step 4). So no
verdict-eligible claim in this build has been checked against anything.

The temptation is to emit ``UNSUPPORTED`` and move on -- it is, after all,
defined as "no qualifying evidence found either way", which is literally true.
It is also misleading, because it implies a search happened. Nobody looked.

So this build does three things instead:

* ``AnalysisResult.notes`` carries a prominent warning as its FIRST entry.
* Every pending claim's ``reasoning`` says so explicitly, in the record.
* Verdict confidence is ``0.0`` for pending claims, because no verdict has
  been reached and a nonzero number would imply one had.

Claims whose verdict IS final in this build -- ``OUT_OF_SCOPE`` from the gate,
``UNVERIFIABLE`` from a non-eligible type -- carry real confidence, because
those verdicts follow directly from a decision that was actually made.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abca.config import Config
from abca.inputs.threads import PostSpan, Thread
from abca.ledger.recorder import RunRecorder
from abca.pipeline.adjudicate import AdjudicationRecord, run_adjudicate
from abca.pipeline.classify import run_classify
from abca.pipeline.cluster import ClusterResult, run_cluster
from abca.pipeline.consensus import ConsensusResult, run_consensus
from abca.pipeline.fidelity import (
    FidelityConfigError,
    PlainLanguageResult,
    run_fidelity,
)
from abca.pipeline.gate import run_gate
from abca.pipeline.ingest import ingest_text
from abca.pipeline.models import DraftClaim
from abca.pipeline.red_team import RedTeamResult, run_red_team
from abca.pipeline.retrieve import RetrievalResult, run_retrieve
from abca.pipeline.segment import run_segment
from abca.pipeline.sentences import split_sentences
from abca.prompts import load_prompt, load_prompts
from abca.providers.registry import ProviderRegistry
from abca.schema.core import (
    AnalysisResult,
    Citation,
    Claim,
    ClusterRef,
    DocumentRef,
    FidelityReport,
    RedTeamFinding,
)
from abca.schema.enums import ClaimType, InputKind, Profile, StageName, Verdict
from abca.schema.ledger import RunConfig, RunRecord, SourceSnapshot
from abca.sources.base import Connector, RetrievedDocument
from abca.sources.registry import build_connectors

#: Prepended to every analysis. States exactly which connectors this build can
#: consult, because "UNSUPPORTED" means something different when the only
#: source available is Illinois statute than it would with a full corpus.
COVERAGE_NOTE = (
    "SOURCE COVERAGE: this build can consult Illinois Compiled Statutes (T0) by "
    "citation only. Empirical, attributive and federal-law claims have no "
    "connector yet, so UNSUPPORTED on those means 'no source was available to "
    "check it', not 'no evidence exists'. Connector coverage is being built out; "
    "read every UNSUPPORTED verdict against the coverage above."
)

#: Written into the reasoning of an eligible claim that no connector could
#: serve, so the caveat survives being read one claim at a time.
_NO_CONNECTOR_REASONING = (
    "No source was consulted: no connector in this build covers this claim's type "
    "or citation. UNSUPPORTED here means unchecked, not refuted."
)

#: Written into the notes when the red team was deliberately skipped. Skipping
#: it is possible and is never silent: contract s7 makes the pass mandatory, so
#: a run without it must announce that fact wherever the run is read.
_RED_TEAM_SKIPPED_NOTE = (
    "RED TEAM SKIPPED. The mandatory adversarial pass (contract s7) was disabled "
    "for this run, so no verdict below has been attacked for counter-evidence, "
    "steelmanned, or checked for overreach. Verdicts here are one model's "
    "unreviewed opinion."
)

#: Added to every multi-post run. A limitation this build has, stated where a
#: reader of the analysis will see it rather than only in the docs.
_THREAD_CONTEXT_NOTE = (
    "POSTS WERE ANALYZED INDEPENDENTLY. A reply like \u201cthat's wrong, it's 25,000\u201d "
    "is read without the post it answers, so claims whose meaning depends on an "
    "earlier comment are usually dropped by the relevance gate rather than "
    "resolved. This build does not infer what a comment referred to; guessing "
    "would put words in somebody's mouth and then adjudicate them."
)

#: Written into the notes of any run that targeted one named account. States
#: the refusal in the record itself, because the record is what gets quoted --
#: and "we analyzed this person's posts" is exactly the phrase that needs the
#: rest of the sentence attached to it.
USER_SCOPE_NOTE = (
    "CLAIMS, NOT A PERSON. This run analyzed posts attributed to one account. Each "
    "claim below stands or falls on its own evidence. There is no aggregate score "
    "for the account, no ratio, and no characterization of the person: contract s8 "
    "refuses those, because a per-person truth score is a harassment instrument "
    "whatever it is called. Counting the verdicts below to produce one would be "
    "using this tool for the thing it was built to refuse."
)

#: Roles this build needs. Backtranslate is not built here, so constructing it
#: would mean a pointless health check and, for a hosted backend, a pointless
#: billable request.
REQUIRED_ROLES: tuple[str, ...] = ("classifier", "segmenter", "adjudicator", "redteam")

#: Roles ``explain`` needs. A much smaller set, and deliberately so: the
#: plain-language gate does not classify, segment or adjudicate anything, so
#: building those backends would mean a pointless health check and, for a hosted
#: backend, a pointless billable request.
#:
#: ``backtranslate`` has no fallback here or anywhere else. Every other role
#: degrades to a related one when unconfigured; this one raises, because the
#: degraded version of an independent back-translation is a gate that passes
#: everything (contract s5).
EXPLAIN_ROLES: tuple[str, ...] = ("adjudicator", "backtranslate")

#: Prepended to every explain run. The gate certifies that the ELEMENTS
#: survived; it does not certify that a lawyer would give the same advice.
EXPLAIN_NOTE = (
    "PLAIN-LANGUAGE RENDERING, NOT LEGAL ADVICE. The fidelity gate checks that "
    "the operative elements of the provision -- who is bound, what is required, "
    "the conditions, the exceptions, the numbers and the force of each obligation "
    "-- survive the rewrite, by having an independent model reconstruct the rule "
    "from the rewrite alone and diffing that against the statute's own elements. "
    "It does not check that the provision is current, that a court reads it this "
    "way, or that it is the provision you need."
)


@dataclass(slots=True)
class AnalyzeOptions:
    """Run-level knobs. Defaults come from config; the CLI overrides them."""

    profile: Profile = Profile.STANDARD
    seed: int = 42
    temperature: float = 0.0
    max_claims: int = 200
    reading_level: int = 8
    max_attempts: int = 3
    context_length: int | None = None
    #: Run the mandatory adversarial pass. Disabling it is possible but never
    #: silent -- see _RED_TEAM_SKIPPED_NOTE.
    red_team: bool = True
    #: Skip the gate and analyze every sentence. Useful when the input is
    #: known to be political and the gate's cost is pure overhead.
    no_gate: bool = False
    #: Cache-only retrieval. A source that is not cached raises rather than
    #: being silently skipped, so a run that could not consult a source says so.
    offline: bool = False
    #: Group near-identical claims so a thread is adjudicated once rather than
    #: once per commenter. Disabling it is always SAFE -- every claim is then
    #: adjudicated on its own text -- and always slower.
    cluster: bool = True
    #: Explicit acknowledgment that the subject may not be a public figure.
    #: Recorded in the run config (contract s8), never inferred.
    private_subjects: bool = False
    #: Adjudicate on a panel and report where its members disagree, rather than
    #: on one model. Slower by a factor of the panel size; never averages.
    consensus: bool = False

    @classmethod
    def from_config(cls, config: Config) -> AnalyzeOptions:
        defaults = config.defaults
        return cls(
            profile=defaults.profile,
            seed=defaults.seed,
            temperature=defaults.temperature,
            max_claims=defaults.max_claims,
            reading_level=defaults.reading_level,
            max_attempts=defaults.max_attempts,
        )


@dataclass(slots=True)
class AnalyzeResult:
    """What :func:`analyze_thread` returns: the record, plus in-flight detail.

    ``thread`` and ``spans`` are in-memory only and never reach the record.
    They carry the real author handles, which is exactly what a published
    record must not contain (see :mod:`abca.inputs.threads`) -- but the local
    terminal report can use them, the same way it uses the input text that the
    record only stores a hash of.
    """

    record: RunRecord
    drafts: list[DraftClaim] = field(default_factory=list)
    thread: Thread | None = None
    spans: list[PostSpan] = field(default_factory=list)
    clusters: ClusterResult | None = None
    #: Populated only in consensus mode. Holds each panel member's verdict, so
    #: the local report can show the split that the record summarizes.
    consensus: ConsensusResult | None = None

    @property
    def analysis(self) -> AnalysisResult:
        return self.record.result

    def post_of(self, claim: Claim | DraftClaim) -> str | None:
        """Which post a claim came from. Local report only."""
        if self.thread is None or claim.span_start is None:
            return None
        return self.thread.post_for_offset(claim.span_start, self.spans)


# --------------------------------------------------------------------------
# Draft -> published claim
# --------------------------------------------------------------------------


def to_published_claim(
    draft: DraftClaim,
    adjudication: AdjudicationRecord | None = None,
    *,
    had_connector: bool = False,
    red_team: RedTeamFinding | None = None,
    downgrade: tuple[Verdict, float] | None = None,
    cluster: ClusterRef | None = None,
    inherited_from: DraftClaim | None = None,
    model_disagreement: float | None = None,
) -> Claim:
    """Convert a working claim into a record-ready :class:`Claim`.

    Precedence, in order:

    ==================================== ================== ==================
    situation                             verdict            confidence
    ==================================== ================== ==================
    gated out of public affairs           OUT_OF_SCOPE       gate's
    type evidence cannot settle           UNVERIFIABLE       classification's
    adjudicated against real sources      the adjudicator's  the adjudicator's
    eligible, no connector covered it     UNSUPPORTED        0.0
    ==================================== ================== ==================

    The last row is the honest one to get right. ``UNSUPPORTED`` means "the
    sources consulted do not settle this". When NO source could be consulted,
    that is a different situation, and the reasoning says so rather than
    letting the verdict imply a search happened.
    """
    claim_type = draft.claim_type or ClaimType.NORMATIVE
    reasoning_parts: list[str] = []
    if draft.classification_reasoning:
        reasoning_parts.append(f"Classification: {draft.classification_reasoning}")

    citations: list[Citation] = []
    evidence_quality = None

    if draft.gated_out:
        verdict = Verdict.OUT_OF_SCOPE
        confidence = draft.classification_confidence or 0.0
        reasoning_parts.append(
            f"Gated as outside public affairs: {draft.gate_reason or 'no reason given'}."
        )
    elif not claim_type.is_verdict_eligible:
        # Final and correct. Contract s3: evidence cannot settle these, and
        # their premises are extracted separately once that stage exists.
        verdict = Verdict.UNVERIFIABLE
        confidence = draft.classification_confidence or 0.0
        reasoning_parts.append(
            f"{claim_type.value} claims are not adjudicated as supported or "
            "contradicted; evidence can inform them but cannot settle them."
        )
    elif adjudication is not None:
        verdict = adjudication.verdict
        confidence = adjudication.confidence
        citations = list(adjudication.citations)

        # The red-team ratchet applies here, at the single point where a
        # published verdict is assembled. Applying it inside the red-team stage
        # would scatter verdict authority across two modules.
        if downgrade is not None:
            verdict, confidence = downgrade
        # Computed from the surviving citations, never asserted. The schema
        # cross-checks it, so a mistake here fails loudly.
        evidence_quality = min(
            (citation.tier for citation in citations), key=lambda t: t.rank, default=None
        )
        reasoning_parts.append(adjudication.reasoning)
        if downgrade is not None and red_team is not None:
            reasoning_parts.append(
                f"[Red team: verdict downgraded from "
                f"{red_team.verdict_downgraded_from.value if red_team.verdict_downgraded_from else '?'} "
                f"to {verdict.value}. {red_team.counter_evidence}]"
            )
        if adjudication.rejected:
            reasoning_parts.append(
                f"({len(adjudication.rejected)} offered citation(s) failed verbatim "
                "verification against the retrieved sources and were discarded.)"
            )
    else:
        verdict = Verdict.UNSUPPORTED
        confidence = 0.0
        reasoning_parts.append(
            "No adjudication was produced for this claim."
            if had_connector
            else _NO_CONNECTOR_REASONING
        )

    if inherited_from is not None and inherited_from.id != draft.id:
        # The single most important line this function can write. A reader must
        # never have to work out from a cluster id that the sources were
        # consulted for somebody else's sentence.
        reasoning_parts.append(
            f"[This claim was NOT adjudicated on its own text. It was grouped with "
            f"{cluster.members if cluster else '?'} near-identical claims and carries "
            f"the verdict reached for {inherited_from.id}, which reads: "
            f"\u201c{inherited_from.text}\u201d. The grouping's weakest pairwise "
            f"similarity is {cluster.min_similarity:.2f} and is published so it can be "
            "checked. Re-run with --no-cluster to adjudicate every claim separately.]"
        )

    if draft.ambiguous_stance:
        reasoning_parts.append(
            "Stance is ambiguous (sarcasm, irony or reported speech): the literal "
            "assertion was extracted rather than a guess at the author's intent."
        )
    if draft.span_source == "sentence":
        reasoning_parts.append(
            "Span covers the whole source sentence; the claim was rephrased to "
            "stand alone and has no verbatim substring."
        )

    return Claim(
        id=draft.id,
        text=draft.text,
        span_start=draft.span_start,
        span_end=draft.span_end,
        claim_type=claim_type,
        verdict=verdict,
        confidence=confidence,
        evidence_quality=evidence_quality,
        reasoning=" ".join(reasoning_parts),
        citations=citations,
        # Only meaningful for a claim that was actually adjudicated. A gated-out
        # or type-ineligible claim never reached the panel, and reporting 0.0
        # for it would read as "every model agreed" when no model was asked.
        model_disagreement=(
            model_disagreement if adjudication is not None else None
        ),
        cluster=cluster,
        red_team=red_team,
    )


def _is_independent(red_teamer, adjudicator) -> bool:
    """Whether the red team is a genuinely different model from the adjudicator.

    Compared by resolved WEIGHTS HASH rather than by name, for the same reason
    the fidelity-gate check is: two config entries can point at one model under
    different tags, and a string comparison would miss it.

    A provider that cannot report an identity (an unreachable backend) is
    treated as NOT independent -- the conservative direction, since the flag
    only ever weakens how much a reader should trust the finding.
    """
    try:
        left = red_teamer.identity()
        right = adjudicator.identity()
    except Exception:
        return False
    if left.weights_hash and right.weights_hash:
        return left.weights_hash != right.weights_hash
    return (left.provider, left.name) != (right.provider, right.name)


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


def build_run_config(
    registry: ProviderRegistry,
    options: AnalyzeOptions,
) -> RunConfig:
    """Assemble the recipe recorded in the ledger.

    Model identities are resolved from the LIVE backends, never from config
    (see :meth:`ProviderRegistry.to_model_identities`), and prompt hashes come
    from the files actually loaded. Both are what make ``input_digest`` mean
    "this exact recipe" rather than "roughly this configuration".
    """
    stages = [StageName.GATE, StageName.SEGMENT, StageName.CLASSIFY, StageName.ADJUDICATE]
    if options.red_team:
        stages.append(StageName.RED_TEAM)
    prompts = load_prompts(*stages)
    return RunConfig(
        profile=options.profile,
        seed=options.seed,
        temperature=options.temperature,
        max_claims=options.max_claims,
        reading_level=options.reading_level,
        consensus=options.consensus,
        red_team=options.red_team,
        offline=options.offline,
        private_subjects=options.private_subjects,
        models=registry.to_model_identities(),
        prompts=[prompt.to_ref() for prompt in prompts],
    )


def analyze_text(
    raw: str,
    registry: ProviderRegistry,
    options: AnalyzeOptions,
    *,
    connectors: dict[str, Connector] | None = None,
    kind: InputKind = InputKind.TEXT,
    locator: str = "<inline>",
    run_id: str | None = None,
) -> AnalyzeResult:
    """Analyze a bare statement. A thread of one post; see :func:`analyze_thread`.

    Kept as the entry point for ``-t`` and ``--stdin`` so those call sites read
    the way they always did, but it is a wrapper rather than a second pipeline.
    One code path means ``-t`` cannot quietly diverge from ``-f`` and ``-u``.
    """
    return analyze_thread(
        Thread.single(raw, locator=locator),
        registry,
        options,
        connectors=connectors,
        kind=kind,
        run_id=run_id,
    )


def analyze_thread(
    thread: Thread,
    registry: ProviderRegistry,
    options: AnalyzeOptions,
    *,
    connectors: dict[str, Connector] | None = None,
    kind: InputKind = InputKind.TEXT,
    run_id: str | None = None,
) -> AnalyzeResult:
    """Run the full pipeline over a conversation.

    A thread is flattened into ONE document before anything else happens, so
    every stage below ingest sees the same shape it always has: one string, with
    offsets into it. Which post a claim came from is tracked alongside the text
    in a span map, never inside it -- see :mod:`abca.inputs.threads` for why a
    ``[a-01]`` marker in the document would be a mistake.

    Stage order note: the gate is recorded AFTER segment and classify in the
    ledger's canonical ordering, but it is *decided* on sentences before
    segmentation runs, because gating first is what keeps a large thread cheap.
    The recorder enforces canonical stage order, so the gate's decisions are
    computed early and its stage record is written in its proper slot with the
    decisions it made.
    """
    raw, spans = thread.render()
    ingested = ingest_text(raw, kind=kind, locator=thread.locator)
    document: DocumentRef = ingested.document

    run_config = build_run_config(registry, options)
    recorder = RunRecorder(document=document, config=run_config, run_id=run_id)

    classifier = registry.get("classifier")
    segmenter = registry.get("segmenter") if "segmenter" in registry else classifier
    adjudicator = registry.get("adjudicator") if "adjudicator" in registry else classifier
    red_teamer = registry.get("redteam") if "redteam" in registry else adjudicator
    # Compared by resolved weights hash, not by role name or model string: two
    # config entries can name the same model differently, and the hash cannot
    # be fooled that way.
    red_team_independent = _is_independent(red_teamer, adjudicator)

    if connectors is None:
        connectors = build_connectors(offline=options.offline)

    notes: list[str] = [COVERAGE_NOTE, *thread.notes, *ingested.notes]
    if not options.red_team:
        notes.insert(1, _RED_TEAM_SKIPPED_NOTE)
    if len(thread.posts) > 1:
        notes.append(_THREAD_CONTEXT_NOTE)

    # -- ingest -----------------------------------------------------------
    with recorder.stage(StageName.INGEST) as stage:
        stage.set_input({
            "kind": kind.value,
            "raw_bytes": len(raw.encode("utf-8")),
            "posts": len(thread.posts),
        })
        sentences = split_sentences(ingested.text)
        stage.set_output(
            {
                "normalized_bytes": document.byte_length,
                "content_hash": document.content_hash,
                "sentences": len(sentences),
                "normalization": document.normalization,
                "changes": dict(sorted(ingested.changes.items())),
                "posts": len(thread.posts),
                # COUNTS ONLY. Handles never reach a record: contract s8 refuses
                # to characterize a person, and a published record listing who
                # said what alongside verdicts would do exactly that.
                "authors": thread.author_count,
                "truncated": thread.truncated,
            }
        )
        for note in [*thread.notes, *ingested.notes]:
            stage.note(note)

    if not sentences:
        # Nothing to analyze. Still produces a valid, auditable record rather
        # than an exception -- "the input contained no sentences" is a finding.
        notes.append("input contained no sentences after normalization")
        result = AnalysisResult(document=document, claims=[], notes=notes)
        return AnalyzeResult(
            record=recorder.finalize(result), drafts=[], thread=thread, spans=spans
        )

    # -- gate (decided here; recorded in canonical order below) ------------
    if options.no_gate:
        gate_decisions = {}
        gate_notes = ["gate skipped (--no-gate); every sentence treated as in scope"]
        gate_outcome = None
    else:
        gate_outcome = run_gate(
            classifier,
            sentences,
            seed=options.seed,
            temperature=options.temperature,
            max_attempts=options.max_attempts,
            context_length=options.context_length,
        )
        gate_decisions = gate_outcome.value
        gate_notes = gate_outcome.notes

    def _passes_gate(sentence) -> bool:
        """A sentence proceeds unless the gate explicitly excluded it.

        Note the default: a sentence with NO decision passes. run_gate already
        fills in missing decisions as in-scope, so this is belt and braces --
        but the direction of the default is the point. Failing open here means
        a gate bug costs money; failing closed would mean it silently shrinks
        the analysis.
        """
        if options.no_gate:
            return True
        decision = gate_decisions.get(sentence.index)
        return decision is None or decision.in_scope

    in_scope = [sentence for sentence in sentences if _passes_gate(sentence)]

    # -- segment ----------------------------------------------------------
    with recorder.stage(StageName.SEGMENT) as stage:
        stage.set_input({"sentences": len(in_scope)})
        segment_outcome = run_segment(
            segmenter,
            ingested.text,
            in_scope,
            seed=options.seed,
            temperature=options.temperature,
            max_attempts=options.max_attempts,
            max_claims=options.max_claims,
            context_length=options.context_length,
        )
        drafts = segment_outcome.value
        stage.set_output({"claims": len(drafts)})
        for note in segment_outcome.notes:
            stage.note(note)
    notes.extend(segment_outcome.notes)

    # -- classify ---------------------------------------------------------
    with recorder.stage(StageName.CLASSIFY) as stage:
        stage.set_input({"claims": len(drafts)})
        classify_outcome = run_classify(
            classifier,
            drafts,
            seed=options.seed,
            temperature=options.temperature,
            max_attempts=options.max_attempts,
            context_length=options.context_length,
        )
        drafts = classify_outcome.value
        counts: dict[str, int] = {}
        for draft in drafts:
            key = draft.claim_type.value if draft.claim_type else "UNKNOWN"
            counts[key] = counts.get(key, 0) + 1
        stage.set_output({"types": dict(sorted(counts.items()))})
        for note in classify_outcome.notes:
            stage.note(note)
    notes.extend(classify_outcome.notes)

    # -- gate record ------------------------------------------------------
    # Written in canonical order even though the decision was made earlier, so
    # the stage chain reflects the pipeline's declared shape. The recorder
    # validates that ordering, so this cannot drift.
    with recorder.stage(StageName.GATE) as stage:
        stage.set_input({"sentences": len(sentences)})
        dropped = len(sentences) - len(in_scope)
        stage.set_output({"in_scope": len(in_scope), "dropped": dropped})
        for note in gate_notes:
            stage.note(note)
    notes.extend(gate_notes)

    # Sentences the gate excluded still produce claims -- marked OUT_OF_SCOPE
    # rather than deleted. A reader must be able to see WHAT was excluded and
    # why; silently dropping text is how an analysis becomes unfalsifiable.
    excluded = [s for s in sentences if s not in in_scope]
    for sentence in excluded:
        decision = gate_decisions.get(sentence.index)
        drafts.append(
            DraftClaim(
                id=f"x-{sentence.index:03d}",
                text=sentence.text,
                sentence_index=sentence.index,
                span_start=sentence.start,
                span_end=sentence.end,
                span_source="sentence",
                claim_type=ClaimType.NORMATIVE,
                classification_confidence=0.0,
                gated_out=True,
                gate_reason=decision.reason if decision else "excluded by gate",
            )
        )

    # -- cluster ----------------------------------------------------------
    # Deterministic, like retrieval and sentence splitting. The grouping decides
    # WHICH TEXT reaches the adjudicator, so it is part of the recipe; a model
    # choosing it would make identical input produce a different input_digest
    # and an unreproducible run.
    cluster_outcome = None
    with recorder.stage(StageName.CLUSTER) as stage:
        stage.set_input({"claims": len(drafts), "enabled": options.cluster})
        if options.cluster:
            cluster_outcome = run_cluster(drafts)
            clusters: ClusterResult = cluster_outcome.value
        else:
            # Every claim its own cluster: the identity grouping. Written this
            # way rather than as a None special case so every path below sees
            # the same object and there is no "clustering off" branch to get
            # wrong further down.
            clusters = run_cluster(drafts, threshold=1.01).value
            stage.note(
                "clustering disabled (--no-cluster); every claim was adjudicated "
                "on its own text"
            )
        targets = clusters.representatives
        stage.set_output({
            "clusters": len(clusters.clusters),
            "adjudicated": len(targets),
            "inherited": clusters.merged_count,
        })
        for note in (cluster_outcome.notes if cluster_outcome else []):
            stage.note(note)
    if cluster_outcome:
        notes.extend(cluster_outcome.notes)

    # -- retrieve ---------------------------------------------------------
    # Deterministic: citation extraction plus connector lookup, no model call.
    # The set of sources consulted is part of the recipe, so a model choosing
    # them would make identical input produce a different input_digest.
    with recorder.stage(StageName.RETRIEVE) as stage:
        stage.set_input({"claims": len(targets)})
        retrieve_outcome = run_retrieve(targets, connectors)
        retrieval: RetrievalResult = retrieve_outcome.value
        # `source` deliberately, not `document`: `document` is the ingested
        # DocumentRef that the AnalysisResult is built from, and shadowing it
        # here silently replaced it with the last retrieved statute.
        for source in retrieval.documents:
            recorder.record_source(
                SourceSnapshot(
                    url=source.url,
                    content_hash=source.content_hash,
                    retrieved_at=source.retrieved_at,
                    connector=source.connector,
                )
            )
        stage.set_output({
            "documents": len(retrieval.documents),
            "claims_with_sources": len(retrieval.by_claim),
            "unresolved_citations": sum(len(v) for v in retrieval.unresolved.values()),
        })
        for note in retrieve_outcome.notes:
            stage.note(note)
    notes.extend(retrieve_outcome.notes)

    # -- adjudicate -------------------------------------------------------
    # In consensus mode the SAME stage runs on a panel and merges. One stage
    # rather than two, because the pipeline's shape does not change -- what
    # changes is how many models answered, which is recorded in the config and
    # on every claim as `model_disagreement`.
    consensus_result: ConsensusResult | None = None
    with recorder.stage(StageName.ADJUDICATE) as stage:
        stage.set_input({
            "eligible": sum(
                1 for d in targets
                if d.claim_type is not None and d.claim_type.is_verdict_eligible
            ),
            "sources": len(retrieval.documents),
            "panel": len(registry.panel) if options.consensus else 1,
        })
        if options.consensus:
            adjudicate_outcome = run_consensus(
                registry.panel_providers(),
                targets,
                retrieval,
                seed=options.seed,
                temperature=options.temperature,
                max_attempts=options.max_attempts,
                context_length=options.context_length,
            )
            consensus_result = adjudicate_outcome.value
            adjudications: dict[str, AdjudicationRecord] = consensus_result.adjudications()
        else:
            adjudicate_outcome = run_adjudicate(
                adjudicator,
                targets,
                retrieval,
                seed=options.seed,
                temperature=options.temperature,
                max_attempts=options.max_attempts,
                context_length=options.context_length,
            )
            adjudications = adjudicate_outcome.value
        stage.set_output({
            "adjudicated": len(adjudications),
            "citations": sum(len(r.citations) for r in adjudications.values()),
            "rejected_citations": sum(len(r.rejected) for r in adjudications.values()),
            "split": consensus_result.split_count if consensus_result else 0,
        })
        for note in adjudicate_outcome.notes:
            stage.note(note)
    notes.extend(adjudicate_outcome.notes)

    # -- red team ---------------------------------------------------------
    # Contract s7: mandatory. Runs on every adjudicated verdict, and can only
    # make the analysis LESS assertive (see abca.pipeline.red_team).
    red_team_result = RedTeamResult()
    red_team_outcome = None
    if options.red_team:
        with recorder.stage(StageName.RED_TEAM) as stage:
            stage.set_input({"verdicts": len(adjudications)})
            red_team_outcome = run_red_team(
                red_teamer,
                targets,
                adjudications,
                retrieval,
                independent=red_team_independent,
                seed=options.seed,
                temperature=options.temperature,
                max_attempts=options.max_attempts,
                context_length=options.context_length,
            )
            red_team_result = red_team_outcome.value
            severities: dict[str, int] = {}
            for finding in red_team_result.findings.values():
                key = finding.severity.value
                severities[key] = severities.get(key, 0) + 1
            stage.set_output({
                "reviewed": len(red_team_result.findings),
                "downgraded": len(red_team_result.downgrades),
                "severities": dict(sorted(severities.items())),
                "independent": red_team_independent,
            })
            for note in red_team_outcome.notes:
                stage.note(note)
        notes.extend(red_team_outcome.notes)

    # -- compose ----------------------------------------------------------
    with recorder.stage(StageName.COMPOSE) as stage:
        stage.set_input({"claims": len(drafts)})
        served = set(retrieval.by_claim) | set(retrieval.unresolved)
        by_id = {draft.id: draft for draft in drafts}

        claims = []
        for draft in drafts:
            # A clustered claim inherits its representative's adjudication,
            # red-team finding and downgrade -- all three, together. Taking the
            # verdict without the objections raised against it would publish the
            # confident half of an analysis and drop the honest half.
            source_id = clusters.represented_by.get(draft.id, draft.id)
            representative = by_id.get(source_id, draft)
            claims.append(
                to_published_claim(
                    draft,
                    adjudications.get(source_id),
                    had_connector=source_id in served,
                    red_team=red_team_result.findings.get(source_id),
                    downgrade=red_team_result.downgrades.get(source_id),
                    cluster=clusters.ref_for(draft.id),
                    inherited_from=representative,
                    model_disagreement=(
                        consensus_result.disagreement(source_id)
                        if consensus_result else None
                    ),
                )
            )

        result = AnalysisResult(document=document, claims=claims, notes=notes)
        stage.set_output(
            {
                "verdicts": dict(sorted(result.verdict_counts().items())),
                "types": dict(sorted(result.type_counts().items())),
                "clustered": sum(1 for c in claims if c.cluster is not None),
            }
        )

    # Token accounting across every stage, including failed repair attempts.
    # Recorded as a note rather than a schema field so the ledger format does
    # not change; step 4 promotes it to a first-class field once retrieval
    # costs are in play too.
    stage_outcomes = (
        gate_outcome, segment_outcome, classify_outcome, cluster_outcome,
        retrieve_outcome, adjudicate_outcome, red_team_outcome,
    )
    spent = sum(o.total_tokens for o in stage_outcomes if o is not None)
    calls = sum(o.attempts for o in stage_outcomes if o is not None)
    repaired = sum(o.repaired_calls for o in stage_outcomes if o is not None)
    notes.append(
        f"model cost: {spent} tokens across {calls} call(s)"
        + (f", {repaired} of which needed repair" if repaired else "")
    )
    result = result.model_copy(update={"notes": notes})

    return AnalyzeResult(
        record=recorder.finalize(result),
        drafts=drafts,
        thread=thread,
        spans=spans,
        clusters=clusters,
        consensus=consensus_result,
    )



# --------------------------------------------------------------------------
# explain: one provision, rendered and gated
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ExplainResult:
    """What :func:`explain_citation` returns."""

    record: RunRecord
    #: None when the citation did not resolve to any source.
    rendering: PlainLanguageResult | None = None
    #: The source the rendering was made from, when one was retrieved.
    source: RetrievedDocument | None = None

    @property
    def resolved(self) -> bool:
        return self.source is not None


def build_explain_config(
    registry: ProviderRegistry,
    options: AnalyzeOptions,
) -> RunConfig:
    """The replay recipe for an ``explain`` run.

    Three prompt refs share the stage ``fidelity`` -- extract, render and
    back-translate are three model calls implementing one stage, and splitting
    them into three stages would misrepresent the pipeline's shape in the
    ledger. :meth:`RunConfig.replay_projection` sorts prompts by
    ``(stage, content_hash)`` precisely so that this case hashes deterministically.

    ``red_team=False`` is accurate rather than a shortcut: there is no verdict
    here to attack. The adversarial pass in an explain run is pass C, and it is
    not optional.
    """
    prompts = [
        load_prompt(StageName.FIDELITY, variant=variant)
        for variant in ("extract", "render", "backtranslate")
    ]
    return RunConfig(
        profile=options.profile,
        seed=options.seed,
        temperature=options.temperature,
        max_claims=options.max_claims,
        reading_level=options.reading_level,
        consensus=False,
        red_team=False,
        offline=options.offline,
        models=registry.to_model_identities(),
        prompts=[prompt.to_ref() for prompt in prompts],
    )


def explain_citation(
    citation: str,
    registry: ProviderRegistry,
    options: AnalyzeOptions,
    *,
    connectors: dict[str, Connector] | None = None,
    run_id: str | None = None,
) -> ExplainResult:
    """Retrieve one legal provision and render it in plain language, gated.

    Stages recorded: ``ingest -> retrieve -> fidelity -> compose``. A valid
    subsequence of :data:`~abca.schema.enums.STAGE_ORDER`, so the record passes
    the same structural checks an ``analyze`` record does and ``abca verify``
    needs no special case for it.

    The document ingested is the CITATION, not the statute. That is the input a
    reader supplied and the thing a replay must reproduce; the statute itself is
    recorded separately as a :class:`~abca.schema.ledger.SourceSnapshot`, pinned
    by content hash, which is what makes a later amendment show up as DRIFTED
    rather than silently changing what this run appears to have said.
    """
    ingested = ingest_text(citation, kind=InputKind.TEXT, locator=citation)
    document: DocumentRef = ingested.document

    run_config = build_explain_config(registry, options)
    recorder = RunRecorder(document=document, config=run_config, run_id=run_id)

    renderer = registry.get("adjudicator")
    backtranslator = registry.get("backtranslate")

    if connectors is None:
        connectors = build_connectors(offline=options.offline)

    notes: list[str] = [EXPLAIN_NOTE, *ingested.notes]

    # -- ingest -----------------------------------------------------------
    with recorder.stage(StageName.INGEST) as stage:
        stage.set_input({"kind": InputKind.TEXT.value, "citation": citation})
        stage.set_output({"content_hash": document.content_hash})
        for note in ingested.notes:
            stage.note(note)

    # -- retrieve ---------------------------------------------------------
    # Routed through the ordinary retrieval stage rather than calling a
    # connector directly, so an explain run consults sources by exactly the same
    # deterministic path an analyze run does -- same citation parser, same
    # caps, same cache, same unresolved reporting. A second retrieval path here
    # would be a second place for source handling to drift.
    probe = DraftClaim(
        id="c-001",
        text=citation,
        sentence_index=0,
        claim_type=ClaimType.LEGAL,
        classification_confidence=1.0,
        classification_reasoning="explain: the citation supplied on the command line",
    )
    with recorder.stage(StageName.RETRIEVE) as stage:
        stage.set_input({"citation": citation})
        retrieve_outcome = run_retrieve([probe], connectors)
        retrieval: RetrievalResult = retrieve_outcome.value
        for source in retrieval.documents:
            recorder.record_source(
                SourceSnapshot(
                    url=source.url,
                    content_hash=source.content_hash,
                    retrieved_at=source.retrieved_at,
                    connector=source.connector,
                )
            )
        stage.set_output({
            "documents": len(retrieval.documents),
            "unresolved": sum(len(v) for v in retrieval.unresolved.values()),
        })
        for note in retrieve_outcome.notes:
            stage.note(note)
    notes.extend(retrieve_outcome.notes)

    documents = retrieval.documents
    if not documents:
        # An unresolved citation is a FINDING, and it produces a real, auditable
        # record rather than an exception. "That section does not exist" is
        # exactly the kind of answer this tool should be able to give.
        notes.append(
            f"{citation!r} did not resolve to any source in this build. No plain-language "
            "rendering was produced; nothing was guessed at."
        )
        with recorder.stage(StageName.COMPOSE) as stage:
            stage.set_input({"documents": 0})
            result = AnalysisResult(document=document, claims=[], notes=notes)
            stage.set_output({"resolved": False})
        return ExplainResult(record=recorder.finalize(result))

    source = documents[0]
    if len(documents) > 1:
        notes.append(
            f"{len(documents)} sources matched; rendered the first ({source.locator or source.url}). "
            "Run explain once per citation to render the others."
        )

    # -- fidelity ---------------------------------------------------------
    fidelity_report: FidelityReport | None = None
    rendering: PlainLanguageResult | None = None
    fidelity_outcome = None
    with recorder.stage(StageName.FIDELITY) as stage:
        stage.set_input({
            "source": source.locator or source.url,
            "source_hash": source.content_hash,
            "source_chars": len(source.text),
        })
        try:
            fidelity_outcome = run_fidelity(
                renderer,
                backtranslator,
                source_text=source.text,
                title=source.title,
                locator=source.locator,
                seed=options.seed,
                temperature=options.temperature,
                max_attempts=options.max_attempts,
                context_length=options.context_length,
            )
        except FidelityConfigError as exc:
            # Not caught and downgraded to a note: a gate that cannot test
            # anything must not produce output that looks tested. The stage
            # records the refusal and the exception propagates to the caller.
            stage.note(str(exc))
            stage.set_output({"refused": True})
            raise

        rendering = fidelity_outcome.value
        fidelity_report = rendering.to_report()
        stage.set_output({
            "passed": not rendering.verbatim_fallback,
            "score": round(fidelity_report.score, 4),
            "elements_source": fidelity_report.elements_source,
            "elements_preserved": fidelity_report.elements_preserved,
            "modal_verbs_preserved": fidelity_report.modal_verbs_preserved,
            "regenerations": fidelity_report.regenerations,
            "reading_grade": fidelity_report.reading_grade,
        })
        for note in fidelity_outcome.notes:
            stage.note(note)
    notes.extend(fidelity_outcome.notes)

    # -- compose ----------------------------------------------------------
    with recorder.stage(StageName.COMPOSE) as stage:
        stage.set_input({"documents": len(documents)})
        result = AnalysisResult(
            document=document, claims=[], fidelity=fidelity_report, notes=notes
        )
        stage.set_output({
            "resolved": True,
            "verbatim_fallback": rendering.verbatim_fallback,
        })

    spent = fidelity_outcome.total_tokens + retrieve_outcome.total_tokens
    calls = fidelity_outcome.attempts + retrieve_outcome.attempts
    notes.append(f"model cost: {spent} tokens across {calls} call(s)")
    result = result.model_copy(update={"notes": notes})

    return ExplainResult(
        record=recorder.finalize(result), rendering=rendering, source=source
    )

__all__ = [
    "COVERAGE_NOTE",
    "EXPLAIN_NOTE",
    "EXPLAIN_ROLES",
    "REQUIRED_ROLES",
    "USER_SCOPE_NOTE",
    "AnalyzeOptions",
    "AnalyzeResult",
    "ExplainResult",
    "analyze_text",
    "analyze_thread",
    "build_explain_config",
    "build_run_config",
    "explain_citation",
    "to_published_claim",
]
