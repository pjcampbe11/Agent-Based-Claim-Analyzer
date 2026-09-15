"""Provider protocol: the one interface the pipeline sees.

WHY A PROTOCOL AND NOT A BASE CLASS
===================================
Nothing in the pipeline may know which backend it is talking to. If a stage
can ask "am I on Ollama?", backend-specific behavior leaks into analysis
logic, and two runs recorded as the same recipe stop meaning the same thing.
A :class:`typing.Protocol` enforces that structurally: providers share no
implementation, only a shape.

WHAT A PROVIDER IS RESPONSIBLE FOR
==================================
Three things, and nothing else:

1. **Generate** text from a prompt, honoring seed and temperature, optionally
   constrained to a JSON Schema.
2. **Embed** text (used by the clustering stage in build step 7).
3. **Identify itself** precisely enough that a third party can pin the exact
   model -- :meth:`Provider.identity` returns a
   :class:`~abca.schema.ledger.ModelIdentity` whose ``weights_hash`` is what
   makes ``reproducible: true`` mean something.

A provider does NOT retry on invalid structured output, does NOT parse JSON,
and does NOT know what a claim is. Those belong to
:mod:`abca.providers.structured` and to the pipeline. Keeping providers thin
is what lets the retry policy be identical across backends -- otherwise
"three attempts" would mean something different on each one.

THE PINNING CONTRACT
====================
``identity().weights_hash`` is the load-bearing field of this whole module.

* Local backends (Ollama, llama.cpp) can return a content hash of the exact
  weights. Runs using them are marked ``reproducible: true``.
* Hosted APIs cannot. They return ``None``, the run is marked
  ``reproducible: false``, and :func:`abca.ledger.verify.compare` refuses to
  certify it as IDENTICAL.

A provider that fabricates a ``weights_hash`` it cannot actually stand behind
is the single worst bug possible in this codebase, because every downstream
guarantee is computed from that field. :meth:`Provider.identity` implementations
are expected to derive it from the backend, never to accept it from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from abca.schema.ledger import ModelIdentity

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for every provider failure.

    Carries the provider name so a multi-provider run's traceback says which
    backend broke without the caller having to reconstruct it.
    """

    def __init__(self, message: str, *, provider: str = "unknown") -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class ProviderUnavailable(ProviderError):
    """The backend could not be reached at all.

    Distinct from :class:`GenerationFailed` because the operator response is
    different: this means "start Ollama" or "open the SSH tunnel", not
    "the model produced something unusable".
    """


class ProviderTimeout(ProviderError):
    """The backend accepted the request but did not answer in time.

    Separated from :class:`ProviderUnavailable` because on a loaded GPU this
    is routine and retryable, whereas an unreachable host is not.
    """


class ModelNotFound(ProviderError):
    """The requested model is not present on the backend.

    Raised eagerly at provider construction where possible, so a run fails
    before it starts rather than halfway through a 2,000-comment thread.
    """


class GenerationFailed(ProviderError):
    """The backend returned an error, or returned nothing usable."""


class PinningUnavailable(ProviderError):
    """The backend cannot supply a weights hash.

    NOT raised for hosted APIs -- those legitimately return ``None`` and the
    run is marked unreproducible. This is raised when a backend that SHOULD be
    pinnable (a local Ollama) fails to report a digest, which usually means an
    unexpected server version. Failing loudly is right: silently degrading to
    ``None`` would flip a run from reproducible to not without anyone noticing.
    """


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Everything a provider needs for one generation.

    Frozen because a request is part of the audit trail: the pipeline hashes a
    summary of it into the stage record, and a request object mutated after
    hashing would make that record a lie.

    ``json_schema`` carries a full JSON Schema, not a flag. Backends that
    support constrained decoding (Ollama's ``format``, llama.cpp's GBNF) use it
    to make invalid output *impossible*; backends that do not fall back to
    validate-and-retry in :mod:`abca.providers.structured`. The same schema
    drives both paths, so the guarantee is the same either way -- only the cost
    differs.
    """

    prompt: str
    system: str | None = None
    json_schema: dict[str, Any] | None = None

    # Determinism knobs. All three are recorded in the run config and are part
    # of the input digest, so changing any of them is a different recipe.
    seed: int = 42
    temperature: float = 0.0

    # `num_ctx` on Ollama. Recorded because a model truncating at a different
    # context length produces different output from identical inputs -- it is a
    # recipe parameter, not a performance tuning knob.
    context_length: int | None = None

    max_tokens: int | None = None
    stop: tuple[str, ...] = ()

    def with_prompt(self, prompt: str) -> GenerationRequest:
        """Return a copy carrying a new prompt.

        Used by the repair loop, which re-asks with validation errors appended.
        A copy rather than a mutation so the original request stays hashable
        and the attempt history is a real history.
        """
        return GenerationRequest(
            prompt=prompt,
            system=self.system,
            json_schema=self.json_schema,
            seed=self.seed,
            temperature=self.temperature,
            context_length=self.context_length,
            max_tokens=self.max_tokens,
            stop=self.stop,
        )


@dataclass(frozen=True, slots=True)
class Completion:
    """One generation result.

    ``prompt_tokens`` / ``completion_tokens`` are recorded rather than
    estimated. They drive ``--budget-tokens`` and they are the honest measure
    of what a run cost; a tokenizer-based estimate would drift from what the
    backend actually charged.

    ``constrained`` records whether the backend enforced the schema during
    decoding or whether the output was merely validated afterwards. That
    distinction belongs in the ledger: a run where every output had to be
    repaired is a different kind of run from one where invalid output was
    impossible, even when both produced valid results.
    """

    text: str
    identity: ModelIdentity
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    constrained: bool = False
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        """Whether generation stopped because it ran out of room.

        Worth checking explicitly: a truncated JSON object fails validation
        with a confusing parse error, and the real fix is a bigger
        ``max_tokens``, not a retry.
        """
        return self.finish_reason in {"length", "max_tokens"}


@dataclass(frozen=True, slots=True)
class Embedding:
    """One embedding vector plus the identity that produced it.

    The identity travels with the vector because embeddings from different
    models are not comparable, and the clustering stage must not silently mix
    them.
    """

    vector: tuple[float, ...]
    identity: ModelIdentity

    def __len__(self) -> int:
        return len(self.vector)


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """What every backend must offer. See module docstring for the rationale."""

    #: Short backend name recorded in ``ModelIdentity.provider``:
    #: ``ollama`` | ``llamacpp`` | ``api:<vendor>``.
    name: str

    def identity(self) -> ModelIdentity:
        """Return the pinned identity of the model this provider serves.

        MUST derive ``weights_hash`` from the backend, never from config.
        Returns ``None`` for that field only when the backend genuinely cannot
        supply one (hosted APIs).
        """
        ...

    def generate(self, request: GenerationRequest) -> Completion:
        """Produce one completion. Raises a :class:`ProviderError` on failure."""
        ...

    def embed(self, texts: list[str]) -> list[Embedding]:
        """Embed a batch of texts."""
        ...

    def health(self) -> None:
        """Raise :class:`ProviderUnavailable` if the backend is not usable.

        Called once at run start so a missing model or a closed SSH tunnel
        fails before any analysis work is done rather than partway through.
        """
        ...


__all__ = [
    "Completion",
    "Embedding",
    "GenerationFailed",
    "GenerationRequest",
    "ModelNotFound",
    "PinningUnavailable",
    "Provider",
    "ProviderError",
    "ProviderTimeout",
    "ProviderUnavailable",
]
