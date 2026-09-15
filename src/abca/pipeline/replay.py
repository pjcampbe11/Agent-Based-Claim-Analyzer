"""Replaying a recorded run: the other half of ``abca verify``.

WHAT REPLAY MUST REBUILD
========================
Everything in ``input_digest``, and nothing else:

* the input text (supplied by the verifier, or from the local input store);
* the models, BY WEIGHTS HASH, not by name;
* the seed, temperature and other sampling parameters;
* the prompt files, by content hash;
* the source snapshots.

Anything the replay cannot reconstruct is refused rather than approximated. A
replay that quietly substituted a different model, or the same model tag with
different weights, would produce a comparison that means nothing while looking
authoritative.

THE MODEL PIN IS CHECKED, NOT ASSUMED
=====================================
:func:`build_replay_registry` resolves each role's model from the LIVE backend
and compares the resulting weights hash against the one in the record. A
mismatch raises. This is the step that makes "reproducible" mean something: it
is not enough that the record NAMES a pinned model — the machine replaying it
has to actually have that model.

For a run recorded as ``reproducible: false`` (a hosted API), replay is refused
outright and :func:`abca.ledger.verify.compare` reports ``UNREPLAYABLE``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from abca.canonical import digest_text
from abca.config import Config, ModelSpec
from abca.ledger.inputs import InputStore
from abca.ledger.verify import LedgerTampered
from abca.pipeline.ingest import normalize_text
from abca.pipeline.orchestrator import AnalyzeOptions, analyze_text, explain_citation
from abca.prompts import load_prompt
from abca.providers.base import ProviderError
from abca.providers.registry import ProviderRegistry, build_registry
from abca.schema.enums import StageName
from abca.schema.ledger import RunRecord
from abca.sources.base import Connector, SourceUnavailable
from abca.sources.registry import build_connectors
from abca.version import SCHEMA_VERSION


class ReplayImpossible(Exception):
    """The recorded recipe cannot be reconstructed on this machine."""


@dataclass(frozen=True, slots=True)
class ReplayInputs:
    """What a replay needs beyond the record itself."""

    text: str
    connectors: dict[str, Connector] | None = None


def resolve_input(
    record: RunRecord,
    *,
    supplied: str | None = None,
    store: InputStore | None = None,
) -> str:
    """Recover the exact text a run analyzed.

    ``supplied`` is normalized with the record's own recipe and its hash
    compared against ``input.content_hash``. A mismatch raises: a replay
    against different text is not a replay, and letting it proceed would
    produce a `DIVERGENT` result that blames the analyzer for the verifier's
    mistake.
    """
    if supplied is not None:
        normalized, _ = normalize_text(supplied)
        actual = digest_text(normalized)
        if actual != record.input.content_hash:
            raise ReplayImpossible(
                "the supplied input does not match the one this run analyzed.\n"
                f"  recorded: {record.input.content_hash}\n"
                f"  supplied: {actual}\n"
                "Both were normalized with recipe "
                f"{record.input.normalization!r}. Replay needs the identical text."
            )
        return normalized

    if store is not None:
        cached = store.get(record.input.content_hash)
        if cached is not None:
            return cached

    raise ReplayImpossible(
        f"the text analyzed by run {record.run_id} is not available locally. "
        "Run records store the input's hash, not its content, so that publishing "
        "a record does not republish someone else's writing. Supply the original "
        "text with --input/-t to replay this run."
    )


def check_schema_compatibility(record: RunRecord) -> None:
    """Refuse replay of a record written under a different schema version.

    ``schema_version`` is part of ``input_digest``, so a record written under
    1.0.0 can never produce a matching digest from a 1.1.0 tool -- the recipe
    itself differs. Reporting that as ``DIVERGENT`` would blame the analyzer
    for a version change, which is exactly the wrong signal.

    THE LIMITATION THIS CREATES, STATED PLAINLY
    -------------------------------------------
    A schema bump makes older records unreplayable by newer tools. That is a
    real cost and it is accepted deliberately: the alternative is excluding
    ``schema_version`` from the digest, which would let two runs recorded under
    different field semantics compare as the same recipe.

    What survives a schema bump forever is INTEGRITY verification. ``audit()``
    recomputes every digest and walks the stage chain using only the record's
    own contents, so ``abca verify --integrity-only`` still proves an old record
    has not been edited, however old it is. Tamper-evidence is permanent;
    byte-level replay is version-scoped.
    """
    if record.schema_version != SCHEMA_VERSION:
        raise ReplayImpossible(
            f"run {record.run_id} was recorded under schema {record.schema_version}; "
            f"this tool writes {SCHEMA_VERSION}. The schema version is part of the "
            "recipe, so a byte-comparable replay is not possible across the change.\n"
            "Integrity verification is unaffected and still proves the record has not "
            "been edited: `abca verify "
            f"{record.run_id} --integrity-only`."
        )


def prompt_variant(path: str) -> str | None:
    """Recover a prompt's variant from its recorded path.

    A :class:`~abca.schema.ledger.PromptRef` carries the STAGE it drives, and one
    stage can use several prompt files -- the fidelity gate is three model calls
    (extract, render, back-translate) implementing one stage. The stage alone is
    therefore not enough to find the file again, and looking up ``fidelity.md``
    would report every explain run as "prompt not present on this machine": a
    replay refused for a reason that is not true.

    ``prompts/sotp/0.1.0/fidelity.render.md`` -> ``"render"``;
    ``prompts/sotp/0.1.0/gate.md`` -> ``None``.
    """
    stem = PurePosixPath(path).name.removesuffix(".md")
    _, _, variant = stem.partition(".")
    return variant or None


def is_explain_record(record: RunRecord) -> bool:
    """Whether this record came from ``abca explain`` rather than ``abca analyze``.

    Decided from the recorded STAGE CHAIN, which the ledger already validates and
    hashes, rather than from a flag someone could set. An explain run reaches the
    fidelity stage and never segments, classifies or adjudicates anything; an
    analyze run is the reverse. There is no overlap to be ambiguous about, and
    replaying one recipe with the other's pipeline would produce a confidently
    wrong DIVERGENT verdict.
    """
    stages = {stage.name for stage in record.stages}
    return StageName.FIDELITY in stages and StageName.ADJUDICATE not in stages


def check_prompt_pins(record: RunRecord) -> None:
    """Confirm the prompt files on THIS machine match the ones the run used.

    An edited prompt is the quietest way a replay could diverge: the recipe
    would look identical everywhere a human glances, while the model was
    actually asked a different question. The hashes are in ``input_digest``
    precisely so this is checkable, and this is where it gets checked.
    """
    mismatched: list[str] = []
    for ref in record.config.prompts:
        try:
            current = load_prompt(ref.stage, variant=prompt_variant(ref.path))
        except FileNotFoundError:
            mismatched.append(f"{ref.path}: not present on this machine")
            continue
        if current.content_hash != ref.content_hash:
            mismatched.append(
                f"{ref.path}: recorded {ref.content_hash[:19]}..., "
                f"this machine has {current.content_hash[:19]}..."
            )
    if mismatched:
        raise ReplayImpossible(
            "prompt files differ from the ones this run used, so a replay would "
            "be asking a different question:\n  - " + "\n  - ".join(mismatched)
        )


def build_replay_registry(record: RunRecord, config: Config | None = None) -> ProviderRegistry:
    """Rebuild the providers, verifying each weights hash against the record.

    WHERE A BACKEND LIVES IS NOT PART OF THE RECIPE
    -----------------------------------------------
    The record stores WHICH model ran -- its name, provider kind, and weights
    hash -- but not its ``base_url``, timeout, or credentials. That split is
    deliberate: a run made against an Ollama on an EC2 box and a run made
    against the identical model on a laptop are the SAME recipe, and baking a
    host into ``input_digest`` would make them falsely divergent. It also keeps
    published records free of internal hostnames.

    The consequence is that replay needs both halves: the local ``config`` says
    where to connect, and the record says what must be found there. Connection
    details come from config; model identity is verified against the record and
    a mismatch raises.
    """
    if not record.reproducible:
        unpinned = [m.name for m in record.config.models if not m.is_pinnable]
        raise ReplayImpossible(
            f"run {record.run_id} used models whose weights cannot be pinned "
            f"({', '.join(unpinned)}), so no replay can establish that the same "
            "model produced the result."
        )

    specs: dict[str, ModelSpec] = {}
    for identity in record.config.models:
        provider = identity.provider
        if provider.startswith("api:"):
            raise ReplayImpossible(
                f"role {identity.role!r} used hosted provider {provider!r}; "
                "hosted runs are UNREPLAYABLE by construction."
            )

        # Connection details from the local config where available; model
        # identity always from the record. If the local config points a role at
        # a different model, the record's name wins and the weights check below
        # confirms the backend actually has it.
        base: ModelSpec | None = None
        if config is not None:
            try:
                base = config.spec(identity.role)
            except Exception:
                base = None

        if base is not None:
            specs[identity.role] = replace(base, provider=provider, model=identity.name)
        else:
            specs[identity.role] = ModelSpec(
                role=identity.role, provider=provider, model=identity.name
            )

    try:
        registry = build_registry(Config(models=specs), roles=list(specs))
    except ProviderError as exc:
        raise ReplayImpossible(f"cannot reach the recorded backends: {exc}") from exc

    recorded = {m.role: m.weights_hash for m in record.config.models}
    for role in registry.roles():
        try:
            live = registry.get(role).identity()
        except ProviderError as exc:
            raise ReplayImpossible(f"role {role!r}: {exc}") from exc
        if live.weights_hash != recorded.get(role):
            raise ReplayImpossible(
                f"role {role!r} resolves to different weights than the record.\n"
                f"  recorded: {recorded.get(role)}\n"
                f"  here:     {live.weights_hash}\n"
                "The model tag may point at a different build. Pull the exact "
                "model, or accept that this run is not reproducible on this machine."
            )
    return registry


class PipelineReplayer:
    """A :class:`~abca.ledger.verify.Replayer` backed by the real pipeline."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        supplied_input: str | None = None,
        input_store: InputStore | None = None,
        connectors: dict[str, Connector] | None = None,
        offline: bool = False,
    ) -> None:
        """
        ``config`` supplies connection details (base_url, timeouts). Model
        identity always comes from the record. See :func:`build_replay_registry`.
        """
        self.config = config
        self.supplied_input = supplied_input
        self.input_store = input_store if input_store is not None else InputStore()
        self.connectors = connectors
        self.offline = offline

    def replay(self, record: RunRecord) -> RunRecord:
        """Re-execute ``record``'s recipe and return a fresh run record."""
        problems = record.audit()
        if problems:
            raise LedgerTampered(record.run_id, problems)

        check_schema_compatibility(record)
        text = resolve_input(record, supplied=self.supplied_input, store=self.input_store)
        check_prompt_pins(record)
        registry = build_replay_registry(record, self.config)

        connectors = self.connectors
        if connectors is None:
            connectors = build_connectors(offline=self.offline)

        options = AnalyzeOptions(
            profile=record.config.profile,
            seed=record.config.seed,
            temperature=record.config.temperature,
            max_claims=record.config.max_claims,
            reading_level=record.config.reading_level,
            offline=self.offline,
        )

        if is_explain_record(record):
            # The stored input IS the citation -- explain ingests the citation,
            # not the statute, precisely so a replay has something reproducible
            # to start from while the statute stays pinned as a SourceSnapshot.
            options.red_team = False
            return explain_citation(
                text, registry, options, connectors=connectors
            ).record

        return analyze_text(
            text, registry, options, connectors=connectors, kind=record.input.kind
        ).record


def make_source_probe(connectors: dict[str, Connector]):
    """Build a drift probe: URL -> its content hash right now, or ``None``.

    Re-fetches through the connector that originally supplied the document, so
    the same extraction and normalization run and the hashes are comparable. A
    probe that fetched raw HTML instead would report drift on every footer
    change, and an alarm that fires constantly is an alarm nobody reads.

    WHICH CONNECTOR OWNS A URL IS ASKED, NOT GUESSED
    ------------------------------------------------
    Each connector implements ``locator_for_url``, turning one of its own URLs
    back into the locator it fetches by. This used to be an ``isinstance`` check
    against the one connector that existed, with a comment saying others would
    register their own -- which meant every connector added after it was
    silently un-probeable, and its drift would have gone unreported while
    ``verify`` still said the run was fine.

    A connector without the method simply owns no URLs, so adding one that
    cannot be probed is possible but visible.
    """

    def probe(url: str) -> str | None:
        for connector in connectors.values():
            resolve = getattr(connector, "locator_for_url", None)
            if resolve is None:
                continue
            locator = resolve(url)
            if not locator:
                continue
            try:
                return connector.fetch(locator).content_hash
            except SourceUnavailable:
                # Unreachable now says nothing about whether the source
                # changed. Reporting DRIFTED here would blame the source for a
                # network problem.
                return None
            except Exception:
                return None
        return None

    return probe


__all__ = [
    "PipelineReplayer",
    "ReplayImpossible",
    "ReplayInputs",
    "build_replay_registry",
    "check_prompt_pins",
    "check_schema_compatibility",
    "is_explain_record",
    "make_source_probe",
    "prompt_variant",
    "resolve_input",
]
