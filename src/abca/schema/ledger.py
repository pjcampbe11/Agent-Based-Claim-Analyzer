"""Ledger models: the run record and everything needed to replay it.

A run record answers three questions, and it is structured around them:

1. **What was analyzed?**  -> ``input`` (:class:`~abca.schema.core.DocumentRef`)
2. **Under what recipe?**  -> ``config`` (:class:`RunConfig`)
3. **What came out?**      -> ``result`` (:class:`~abca.schema.core.AnalysisResult`)

Plus a fourth, which is what separates this from a log file:

4. **Can anyone check it?** -> ``stages`` (a tamper-evident hash chain) and
   the three digests described below.

THE THREE DIGESTS
=================
Different questions need different comparison surfaces, so a run carries
three separate digests rather than one.

``input_digest``
    Covers everything that *should* determine the output: the input content
    hash, the seed, the temperature, the model identities including weight
    file hashes, the prompt contract version and prompt file hashes, and the
    snapshot hashes of every retrieved source. Two runs with the same
    ``input_digest`` were asked the same question in the same way.

    Deliberately EXCLUDES ``TOOL_VERSION``. If a patch release changed the
    input digest, every published run hash would be invalidated by a typo
    fix in a docstring. The tool version is recorded in ``environment`` and
    reported as context when verification finds a mismatch.

``semantic_digest``
    Covers the parts of the output a reader actually relies on: per claim,
    the id, type, verdict, rounded confidence, evidence tier, and sorted
    citations. Excludes prose. Two runs with the same ``semantic_digest``
    reached the same conclusions from the same evidence.

``output_digest``
    Covers the entire result object, prose included. Byte-level identity.

Comparing them in that order is what produces the four verification
outcomes (see :mod:`abca.ledger.verify`).

WHAT IS NOT IN THE RECORD
=========================
The analyzed text itself. The record stores its hash. Publishing a run
should not mean republishing someone else's article or someone's posts, and
run records are meant to be publishable by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from abca.canonical import digest_chain, digest_json
from abca.schema.core import ABCAModel, AnalysisResult, Digest, DocumentRef
from abca.schema.enums import STAGE_ORDER, Profile, StageName
from abca.version import PROMPT_CONTRACT_VERSION, SCHEMA_VERSION, TOOL_VERSION


def _as_utc(value: datetime) -> datetime:
    """Coerce to aware UTC; reject naive datetimes. See core._as_utc."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not accepted; timestamps must be timezone-aware")
    return value.astimezone(UTC)


class ModelIdentity(ABCAModel):
    """Identity of one model used in a run.

    ``weights_hash`` is the entire reason abCA is local-first. A GGUF file
    has a SHA-256; a hosted API endpoint does not, and can be swapped
    underneath a caller without notice or version change. When
    ``weights_hash`` is ``None`` the run is marked ``reproducible=False``,
    because the recipe cannot be fully pinned -- and that flag is honest
    rather than cosmetic: :mod:`abca.ledger.verify` refuses to certify such
    a run as IDENTICAL.
    """

    role: str = Field(
        min_length=1,
        description="Pipeline role: classifier, adjudicator, backtranslate, embedder, consensus.",
    )
    provider: str = Field(min_length=1, description="ollama | llamacpp | api:<vendor>")
    name: str = Field(min_length=1, description="Model name/tag as the provider reports it.")
    weights_hash: Digest | None = Field(
        default=None,
        description="SHA-256 of the weights file. None for hosted APIs, which cannot be pinned.",
    )
    quantization: str | None = Field(default=None, description="e.g. Q5_K_M. None if not applicable.")
    context_length: int | None = Field(default=None, ge=1)

    @property
    def is_pinnable(self) -> bool:
        """Whether this model's exact weights can be verified by a third party."""
        return self.weights_hash is not None

    def replay_projection(self) -> dict[str, Any]:
        """Fields that must match for a replay to count as the same recipe."""
        return {
            "role": self.role,
            "provider": self.provider,
            "name": self.name,
            "weights_hash": self.weights_hash,
            "quantization": self.quantization,
        }


class PromptRef(ABCAModel):
    """A prompt file used in the run, pinned by content hash.

    Both the contract version and the file hash are recorded. The version
    states the intent; the hash catches the case where someone edits a
    prompt and forgets to bump the version, which is the failure mode that
    would otherwise let the contract silently drift.
    """

    stage: StageName = Field(description="Pipeline stage this prompt drives.")
    path: str = Field(min_length=1, description="Repo-relative path, e.g. prompts/sotp/0.1.0/adjudicate.md")
    content_hash: Digest = Field(description="SHA-256 of the prompt file bytes.")


class SourceSnapshot(ABCAModel):
    """One retrieved source, pinned at the moment it was read.

    The set of these is what makes DRIFTED detectable: on replay, each URL is
    re-fetched and its hash compared. A changed statute does not mean the
    original verdict was wrong; it means the verdict needs re-examination,
    which is why DRIFTED is reported rather than swallowed.
    """

    url: str = Field(min_length=1)
    content_hash: Digest
    retrieved_at: datetime
    connector: str = Field(
        min_length=1,
        description="Connector that produced it. Determines tier; see SourceTier docstring.",
    )

    _normalize_retrieved_at = field_validator("retrieved_at")(_as_utc)


class RunConfig(ABCAModel):
    """The replay recipe.

    Everything here is an input to ``input_digest``. If a field would not
    change the output, it does not belong in this object -- it belongs in
    :class:`EnvironmentInfo`.
    """

    profile: Profile
    seed: int = Field(description="Sampling seed. Default 42; recorded regardless.")
    temperature: float = Field(ge=0.0, le=2.0, description="Default 0.0 for determinism.")
    max_claims: int = Field(ge=1)
    reading_level: int = Field(ge=1, le=16, description="Target Flesch-Kincaid grade for output.")
    consensus: bool = Field(default=False, description="Whether multiple models were run.")
    red_team: bool = Field(
        default=True,
        description=(
            "Whether the mandatory adversarial pass ran. Disabling it requires an "
            "explicit acknowledgement flag; the record shows when it was skipped."
        ),
    )
    offline: bool = Field(default=False, description="Cache-only retrieval.")
    private_subjects: bool = Field(
        default=False,
        description="Whether analysis of non-public figures was explicitly permitted.",
    )
    models: list[ModelIdentity] = Field(min_length=1)
    prompts: list[PromptRef] = Field(default_factory=list)
    prompt_contract_version: str = Field(default=PROMPT_CONTRACT_VERSION)

    @model_validator(mode="after")
    def _unique_roles(self) -> Self:
        """One model per role, except ``consensus`` which is intentionally a list."""
        seen: set[str] = set()
        for model in self.models:
            if model.role == "consensus":
                continue
            if model.role in seen:
                raise ValueError(f"duplicate model role {model.role!r} in run config")
            seen.add(model.role)
        return self

    @model_validator(mode="after")
    def _backtranslate_must_differ(self) -> Self:
        """Contract s5: pass C must not run on the adjudicating model.

        Same-model back-translation reproduces the same misreadings, so the
        fidelity gate would pass everything and prove nothing. This is the
        one place where an architectural mistake is silent rather than loud,
        so it is enforced structurally.
        """
        by_role = {m.role: m for m in self.models}
        adjudicator = by_role.get("adjudicator")
        backtranslate = by_role.get("backtranslate")
        if adjudicator and backtranslate and adjudicator.name == backtranslate.name:
            raise ValueError(
                f"backtranslate model must differ from adjudicator (both are "
                f"{adjudicator.name!r}); a same-model back-translation reproduces the "
                "same misreadings and the fidelity gate becomes a no-op (contract s5)."
            )
        return self

    @property
    def is_fully_pinnable(self) -> bool:
        """True when every model's weights can be verified by a third party."""
        return all(m.is_pinnable for m in self.models)

    def replay_projection(self) -> dict[str, Any]:
        """Recipe fields, normalized for hashing.

        Models and prompts are sorted so that list ordering -- which carries
        no meaning here -- cannot make two identical recipes hash differently.
        """
        return {
            "profile": self.profile.value,
            "seed": self.seed,
            "temperature": self.temperature,
            "max_claims": self.max_claims,
            "reading_level": self.reading_level,
            "consensus": self.consensus,
            "red_team": self.red_team,
            "offline": self.offline,
            "private_subjects": self.private_subjects,
            "prompt_contract_version": self.prompt_contract_version,
            "models": sorted(
                (m.replay_projection() for m in self.models),
                key=lambda m: (m["role"], m["name"]),
            ),
            "prompts": sorted(
                ({"stage": p.stage.value, "content_hash": p.content_hash} for p in self.prompts),
                # Sorted by (stage, hash), not by stage alone. One stage can use
                # SEVERAL prompts -- the fidelity gate is three model calls
                # (extract, render, back-translate) that together implement one
                # stage -- and a sort on stage alone leaves their relative order
                # to whatever the input list happened to be. That would make two
                # identical recipes hash differently, which is exactly what this
                # projection exists to prevent. Records whose stages are all
                # distinct sort identically under both keys, so this is safe for
                # everything already written.
                key=lambda p: (p["stage"], p["content_hash"]),
            ),
        }


class EnvironmentInfo(ABCAModel):
    """Machine and library context.

    Recorded but NOT part of ``input_digest``. These fields should not change
    the output; when verification fails they are the first thing to look at,
    which is precisely why they are captured but not hashed into the recipe.
    """

    tool_version: str = Field(default=TOOL_VERSION)
    schema_version: str = Field(default=SCHEMA_VERSION)
    python_version: str
    platform: str
    packages: dict[str, str] = Field(
        default_factory=dict, description="Version of each runtime-relevant dependency."
    )


class StageRecord(ABCAModel):
    """One link in the tamper-evident stage chain.

    Each record commits to the hash of the record before it, so editing any
    earlier stage invalidates every hash after it. A reader holding the
    published ``ledger_hash`` can therefore detect any post-hoc edit to the
    middle of a run -- which is the specific attack any fact engine with an
    owner invites.

    Stage inputs and outputs are stored as HASHES, not as content. The full
    intermediate payloads can be enormous (a 2,000-comment thread's segment
    output), and they frequently contain the analyzed text, which the record
    deliberately does not carry.
    """

    name: StageName
    sequence: int = Field(ge=0, description="Position in the run, starting at 0.")
    started_at: datetime
    ended_at: datetime
    input_hash: Digest = Field(description="Digest of the stage's canonicalized input.")
    output_hash: Digest = Field(description="Digest of the stage's canonicalized output.")
    prev_hash: Digest | None = Field(
        default=None, description="record_hash of the preceding stage; None for the genesis stage."
    )
    record_hash: Digest = Field(description="This record's own chain hash.")
    duration_ms: int = Field(ge=0)
    notes: list[str] = Field(default_factory=list)

    _normalize_started = field_validator("started_at")(_as_utc)
    _normalize_ended = field_validator("ended_at")(_as_utc)

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError(
                f"stage {self.name.value} ended before it started "
                f"({self.ended_at} < {self.started_at})"
            )
        return self

    def chain_payload(self) -> dict[str, Any]:
        """The fields this record's hash commits to.

        Wall-clock timestamps and ``duration_ms`` are EXCLUDED. They are
        genuinely non-deterministic -- the same run replayed will always take
        a different number of milliseconds -- so including them would make
        the chain unverifiable on replay while adding nothing: an attacker
        editing a run would edit the substance, and the substance is the
        stage identity and its input/output hashes.
        """
        return {
            "name": self.name.value,
            "sequence": self.sequence,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
        }

    def expected_record_hash(self) -> str:
        """Recompute this record's chain hash from its own contents."""
        return digest_chain(self.prev_hash, self.chain_payload())

    def is_self_consistent(self) -> bool:
        """Whether ``record_hash`` matches what the contents imply."""
        return self.record_hash == self.expected_record_hash()


class RunRecord(ABCAModel):
    """The complete, publishable record of one analysis run."""

    run_id: str = Field(min_length=1)
    schema_version: str = Field(default=SCHEMA_VERSION)
    created_at: datetime

    input: DocumentRef
    config: RunConfig
    environment: EnvironmentInfo
    sources: list[SourceSnapshot] = Field(default_factory=list)

    stages: list[StageRecord] = Field(default_factory=list)
    result: AnalysisResult

    reproducible: bool = Field(
        description=(
            "False when any model's weights cannot be pinned by hash (hosted "
            "APIs). Such a run can still be inspected, but it cannot be "
            "certified IDENTICAL by a third party, and verify() will say so."
        )
    )
    timings_ms: dict[str, int] = Field(default_factory=dict)

    input_digest: Digest
    semantic_digest: Digest
    output_digest: Digest
    ledger_hash: Digest

    _normalize_created = field_validator("created_at")(_as_utc)

    # ---------------------------------------------------------------- validators

    @model_validator(mode="after")
    def _stages_in_pipeline_order(self) -> Self:
        """Recorded stages must be a subsequence of the real pipeline order.

        A record claiming ADJUDICATE ran before CLASSIFY is corrupt or
        forged. Sequence numbers must also be contiguous from zero, so a
        deleted stage is detectable independently of the hash chain.
        """
        if not self.stages:
            return self

        for index, stage in enumerate(self.stages):
            if stage.sequence != index:
                raise ValueError(
                    f"stage sequence numbers must be contiguous from 0; "
                    f"position {index} declares sequence {stage.sequence}"
                )

        order = {name: position for position, name in enumerate(STAGE_ORDER)}
        positions = [order[stage.name] for stage in self.stages]
        if positions != sorted(positions):
            names = " -> ".join(s.name.value for s in self.stages)
            raise ValueError(
                f"stages are not in canonical pipeline order: {names}. "
                "See STAGE_ORDER in abca.schema.enums."
            )
        return self

    @model_validator(mode="after")
    def _reproducible_flag_is_honest(self) -> Self:
        """``reproducible=True`` requires every model to be pinnable.

        Without this check the flag would be a claim the record makes about
        itself with nothing backing it, which is the exact failure mode this
        whole subsystem exists to prevent.
        """
        if self.reproducible and not self.config.is_fully_pinnable:
            unpinned = [m.name for m in self.config.models if not m.is_pinnable]
            raise ValueError(
                f"run declares reproducible=True but these models have no weights "
                f"hash and therefore cannot be pinned: {unpinned}. Hosted APIs are "
                "not reproducible by a third party."
            )
        return self

    # ------------------------------------------------------------------ digests

    def replay_projection(self) -> dict[str, Any]:
        """Everything that should determine the output. Basis of ``input_digest``.

        Sources are sorted by URL because retrieval may run concurrently and
        completion order carries no meaning.
        """
        return {
            "schema_version": self.schema_version,
            "input": {
                "kind": self.input.kind.value,
                "content_hash": self.input.content_hash,
                "normalization": self.input.normalization,
            },
            "config": self.config.replay_projection(),
            "sources": sorted(
                ({"url": s.url, "content_hash": s.content_hash, "connector": s.connector}
                 for s in self.sources),
                key=lambda s: s["url"],
            ),
        }

    def compute_input_digest(self) -> str:
        return digest_json(self.replay_projection())

    def compute_semantic_digest(self) -> str:
        return digest_json(self.result.semantic_projection())

    def compute_output_digest(self) -> str:
        return digest_json(self.result.to_jsonable())

    def compute_ledger_hash(self) -> str:
        """Hash covering the whole record: recipe, chain head, and outputs.

        Only the chain HEAD is included rather than every stage, because each
        stage record already commits to its predecessor -- including the head
        transitively commits to all of them. Including the full list too
        would be redundant and would make partial-stage records awkward to
        hash consistently.
        """
        head = self.stages[-1].record_hash if self.stages else None
        return digest_json(
            {
                "run_id": self.run_id,
                "schema_version": self.schema_version,
                "input_digest": self.input_digest,
                "semantic_digest": self.semantic_digest,
                "output_digest": self.output_digest,
                "stage_chain_head": head,
                "stage_count": len(self.stages),
                "reproducible": self.reproducible,
            }
        )

    # ------------------------------------------------------------------- audit

    def audit(self) -> list[str]:
        """Recompute every digest and chain link; return a list of problems.

        An empty list means the record is internally consistent: nothing has
        been edited since it was written, assuming the caller trusts the
        ``ledger_hash`` it is comparing against (published separately).

        Returns problems rather than raising, because the CLI wants to show a
        human ALL the inconsistencies at once, not just the first.
        """
        return self.audit_detail()[0]

    def audit_detail(self) -> tuple[list[str], list[str]]:
        """``(problems, caveats)``.

        Split because they mean opposite things and collapsing them would make
        one of the two useless. A PROBLEM is evidence the record was edited. A
        CAVEAT is a check this tool could not perform on this record -- which is
        information a reader needs, but is not an accusation.
        """
        problems: list[str] = []
        caveats: list[str] = []

        expected_input = self.compute_input_digest()
        if self.input_digest != expected_input:
            problems.append(
                f"input_digest mismatch: recorded {self.input_digest}, recomputed {expected_input}"
            )

        # The semantic digest is the ONE recomputation whose shape lives in
        # code rather than in the record: it hashes
        # `AnalysisResult.semantic_projection()`, and a future version that adds
        # a field to that projection would recompute a different digest for an
        # untouched record.
        #
        # Every other check here is version-proof, because it recomputes from
        # the record's own stored contents. So this one is skipped -- loudly --
        # when the record was written under a different schema version. Reporting
        # "the record was edited" about a record nobody edited would be a false
        # accusation from the exact mechanism whose whole purpose is detecting
        # real ones, and it would train people to ignore the alarm.
        #
        # Nothing is lost: the semantic digest is a comparison surface for
        # replay, and `abca.pipeline.replay` already refuses to replay across a
        # schema change. Tamper-evidence for an old record rests on the input,
        # output and ledger digests and the stage chain, all of which still run.
        if self.schema_version != SCHEMA_VERSION:
            caveats.append(
                f"semantic_digest NOT CHECKED: this record was written under schema "
                f"{self.schema_version} and this tool writes {SCHEMA_VERSION}. The "
                "semantic projection's shape is defined in code, so recomputing it "
                "here would compare two different things. Every other digest and the "
                "whole stage chain were checked."
            )
        else:
            expected_semantic = self.compute_semantic_digest()
            if self.semantic_digest != expected_semantic:
                problems.append(
                    f"semantic_digest mismatch: recorded {self.semantic_digest}, "
                    f"recomputed {expected_semantic}"
                )

        expected_output = self.compute_output_digest()
        if self.output_digest != expected_output:
            problems.append(
                f"output_digest mismatch: recorded {self.output_digest}, "
                f"recomputed {expected_output}"
            )

        # Walk the chain. Each link must (a) hash to its recorded value and
        # (b) point at its actual predecessor.
        previous: str | None = None
        for stage in self.stages:
            if stage.prev_hash != previous:
                problems.append(
                    f"stage {stage.sequence} ({stage.name.value}): prev_hash is "
                    f"{stage.prev_hash}, but the preceding record hashes to {previous}"
                )
            if not stage.is_self_consistent():
                problems.append(
                    f"stage {stage.sequence} ({stage.name.value}): record_hash "
                    f"{stage.record_hash} does not match its contents "
                    f"(expected {stage.expected_record_hash()})"
                )
            previous = stage.record_hash

        expected_ledger = self.compute_ledger_hash()
        if self.ledger_hash != expected_ledger:
            problems.append(
                f"ledger_hash mismatch: recorded {self.ledger_hash}, "
                f"recomputed {expected_ledger}"
            )

        return problems, caveats

    def is_intact(self) -> bool:
        """Convenience wrapper: True when :meth:`audit` finds nothing."""
        return not self.audit()


__all__ = [
    "EnvironmentInfo",
    "ModelIdentity",
    "PromptRef",
    "RunConfig",
    "RunRecord",
    "SourceSnapshot",
    "StageRecord",
]
