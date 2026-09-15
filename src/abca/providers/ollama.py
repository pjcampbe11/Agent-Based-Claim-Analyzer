"""Ollama provider -- the default backend.

WHY OLLAMA IS THE DEFAULT
=========================
It is the only widely-deployed local runtime that exposes a **content digest
for the exact model it is serving**. That single fact is what lets abCA mark a
run ``reproducible: true`` honestly: a third party can be told "this verdict
came from the model whose manifest digest is X", pull the same tag, and check
they got the same digest.

A hosted API cannot offer that. Neither can a runtime that only reports a
model *name*, because names are mutable and quantizations differ.

REMOTE OLLAMA IS THE SAME CODE PATH
===================================
The EC2 topology in docs/04-linux-ec2.md forwards the instance's Ollama to
``localhost:11434`` over an SSH tunnel. Nothing here knows or cares -- the
tunnel makes a remote model look local, and the digest comes back over it
intact. So weight pinning works identically for a model on this laptop and a
27B on a g6.xlarge, which is exactly the property that makes the cheap
topology viable.

THE DIGEST IS THE MANIFEST DIGEST
=================================
``/api/tags`` reports a digest over the model's manifest, which commits to the
weight blobs, the quantization, the template, and the default parameters. Two
consequences worth being explicit about:

* ``qwen2.5:32b-instruct-q4_K_M`` and ``...-q5_K_M`` have DIFFERENT digests.
  Correct: they produce different output and must not be treated as the same
  recipe.
* Re-tagging different weights under a name someone already published a
  verdict against changes the digest, so the substitution is detectable.

If the digest cannot be obtained, this provider raises
:class:`~abca.providers.base.PinningUnavailable` rather than returning
``None``. Silently degrading would flip a run from reproducible to not
without anyone noticing, and the flag would stop meaning anything.
"""

from __future__ import annotations

import re
import time
from typing import Any

from abca.canonical import DIGEST_PREFIX
from abca.providers.base import (
    Completion,
    Embedding,
    GenerationFailed,
    GenerationRequest,
    ModelNotFound,
    PinningUnavailable,
    ProviderUnavailable,
)
from abca.providers.transport import HttpTransport, Transport, monotonic_ms

#: Ollama's default listen address. The same value works for a tunnelled
#: remote instance, which is the point of the tunnel.
DEFAULT_BASE_URL = "http://127.0.0.1:11434"

#: Structured outputs (`format` accepting a full JSON Schema) landed in
#: Ollama 0.5.0. Below that, only `format: "json"` exists, which constrains
#: output to *some* JSON object but not to our schema -- so the validate-and-
#: retry loop carries the whole burden there.
MIN_STRUCTURED_OUTPUT_VERSION = (0, 5, 0)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    """Extract a (major, minor, patch) tuple from an Ollama version string.

    Returns ``None`` for anything unparseable -- development builds report
    things like ``0.0.0-rc1`` or a bare git SHA. The caller treats unknown as
    "assume no structured-output support", which degrades to validate-and-retry
    rather than failing.
    """
    match = _VERSION_RE.search(raw or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _normalize_digest(raw: str | None) -> str | None:
    """Coerce Ollama's digest into abCA's ``sha256:<hex>`` form.

    Ollama has reported the digest both bare and prefixed across versions.
    Anything that is not a recognisable SHA-256 is rejected rather than
    passed through, because a malformed value would fail
    :class:`~abca.schema.ledger.ModelIdentity` validation much later, in a
    place that gives no hint about where it came from.
    """
    if not raw:
        return None
    value = raw.strip().lower()
    value = value.removeprefix(DIGEST_PREFIX)
    if not _HEX64.match(value):
        return None
    return DIGEST_PREFIX + value


class OllamaProvider:
    """Talks to an Ollama daemon, local or tunnelled."""

    def __init__(
        self,
        model: str,
        *,
        role: str = "adjudicator",
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 300.0,
        keep_alive: str = "30m",
    ) -> None:
        """
        ``keep_alive`` is passed through on every generation. The default of
        30 minutes is not arbitrary: on a 24 GB card, letting a 27B unload
        between claims means paying a multi-second model load per claim, which
        is the difference between a usable ``standard`` profile and one nobody
        will wait for (docs/04-linux-ec2.md).
        """
        self.name = "ollama"
        self.model = model
        self.role = role
        self.keep_alive = keep_alive
        self._transport: Transport = transport or HttpTransport(
            base_url, timeout=timeout, provider_name="ollama"
        )
        # Populated lazily by health(); cached because /api/tags is a real
        # round trip and identity() is called on every completion.
        self._identity_cache: Any = None
        self._supports_schema_format: bool | None = None

    # ----------------------------------------------------------------- health

    def health(self) -> None:
        """Verify the daemon is reachable and the model is present.

        Called once at run start. Failing here costs one second; failing
        halfway through a 2,000-comment thread costs the whole run.
        """
        try:
            version_payload = self._transport.get_json("/api/version")
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(
                f"cannot reach Ollama at {getattr(self._transport, 'base_url', '?')}: {exc}. "
                "Start it with `ollama serve`, or if the model is remote, check "
                "that the SSH tunnel is up (see docs/04-linux-ec2.md).",
                provider=self.name,
            ) from exc

        version = _parse_version(str(version_payload.get("version", "")))
        self._supports_schema_format = (
            version is not None and version >= MIN_STRUCTURED_OUTPUT_VERSION
        )

        # Resolving identity here doubles as the "is the model installed?" check.
        self.identity()

    # --------------------------------------------------------------- identity

    def _find_tag(self) -> dict[str, Any]:
        """Locate this model's entry in ``/api/tags``.

        Ollama treats ``qwen2.5:32b`` and ``qwen2.5:32b-instruct`` as distinct
        tags but reports a bare name as ``name:latest``, so both spellings are
        tried before giving up.
        """
        payload = self._transport.get_json("/api/tags")
        models = payload.get("models") or []
        wanted = {self.model, f"{self.model}:latest"}

        for entry in models:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") in wanted or entry.get("model") in wanted:
                return entry

        available = sorted(
            str(entry.get("name")) for entry in models if isinstance(entry, dict)
        )
        raise ModelNotFound(
            f"model {self.model!r} is not installed. Available: "
            + (", ".join(available) if available else "<none>")
            + f". Pull it with `ollama pull {self.model}`.",
            provider=self.name,
        )

    def _context_length(self, show: dict[str, Any]) -> int | None:
        """Dig the trained context length out of ``/api/show``.

        The key is architecture-prefixed (``qwen2.context_length``,
        ``llama.context_length``), so it is found by suffix rather than by a
        hardcoded list that would rot with every new model family.

        This is recorded because a model truncating at a different context
        length produces different output from identical input -- it is a
        recipe parameter, not a tuning knob.
        """
        info = show.get("model_info")
        if not isinstance(info, dict):
            return None
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
        return None

    def identity(self):
        """Return the pinned :class:`~abca.schema.ledger.ModelIdentity`.

        Cached: ``/api/tags`` is a round trip and this is called per
        completion. The cache is safe because a model cannot be swapped
        underneath a running Ollama without a restart, which would break the
        connection anyway.
        """
        from abca.schema.ledger import ModelIdentity  # local: avoids a cycle

        if self._identity_cache is not None:
            return self._identity_cache

        tag = self._find_tag()
        digest = _normalize_digest(tag.get("digest"))

        if digest is None:
            raise PinningUnavailable(
                f"Ollama reported no usable digest for {self.model!r} "
                f"(got {tag.get('digest')!r}). Without it this run cannot be "
                "marked reproducible, and silently continuing would make the "
                "`reproducible` flag meaningless. Upgrade Ollama, or run with "
                "an explicitly unreproducible provider if that is intended.",
                provider=self.name,
            )

        # /api/show is best-effort enrichment: quantization and context length
        # improve the record but their absence is not fatal, because the digest
        # already pins the model exactly.
        details: dict[str, Any] = {}
        show: dict[str, Any] = {}
        try:
            show = self._transport.post_json("/api/show", {"name": self.model})
            details = show.get("details") or {}
        except Exception:
            pass
        if not details:
            details = tag.get("details") or {}

        self._identity_cache = ModelIdentity(
            role=self.role,
            provider=self.name,
            name=self.model,
            weights_hash=digest,
            quantization=details.get("quantization_level"),
            context_length=self._context_length(show),
        )
        return self._identity_cache

    # ------------------------------------------------------------- generation

    def _build_options(self, request: GenerationRequest) -> dict[str, Any]:
        """Assemble Ollama's ``options`` block from the request.

        Only determinism-relevant knobs are set, and each one is also recorded
        in the run config. Anything set here that is NOT in the run record
        would be an unrecorded input to the output -- a silent hole in the
        reproducibility story -- so this function must stay in sync with
        :class:`~abca.schema.ledger.RunConfig`.
        """
        options: dict[str, Any] = {
            "temperature": request.temperature,
            "seed": request.seed,
        }
        if request.context_length is not None:
            options["num_ctx"] = request.context_length
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if request.stop:
            options["stop"] = list(request.stop)
        return options

    def generate(self, request: GenerationRequest) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            # Streaming is off deliberately. We need the complete object before
            # anything can be validated, and a partial stream cannot be
            # schema-checked -- so streaming would buy latency we cannot spend.
            "stream": False,
            "options": self._build_options(request),
            "keep_alive": self.keep_alive,
        }
        if request.system:
            payload["system"] = request.system

        constrained = False
        if request.json_schema is not None:
            if self._supports_schema_format is None or self._supports_schema_format:
                # Full schema-constrained decoding: invalid shapes are
                # unsamplable. `None` (health() not called) is treated as
                # supported so a caller that skipped health() still gets the
                # strong path; a server that rejects it raises a clear HTTP
                # error rather than silently producing garbage.
                payload["format"] = request.json_schema
                constrained = True
            else:
                # Pre-0.5 Ollama: forces *some* JSON object, not ours. The
                # validate-and-retry loop carries the rest.
                payload["format"] = "json"

        started = time.perf_counter()
        response = self._transport.post_json("/api/generate", payload)
        elapsed_ms = monotonic_ms(started)

        text = response.get("response")
        if not isinstance(text, str):
            raise GenerationFailed(
                f"Ollama returned no 'response' field for {self.model!r}; "
                f"got keys {sorted(response)}",
                provider=self.name,
            )

        return Completion(
            text=text,
            identity=self.identity(),
            prompt_tokens=int(response.get("prompt_eval_count") or 0),
            completion_tokens=int(response.get("eval_count") or 0),
            # Prefer Ollama's own nanosecond timer over our wall clock: it
            # excludes transport overhead, which over an SSH tunnel is real.
            duration_ms=(
                int(response["total_duration"] / 1_000_000)
                if isinstance(response.get("total_duration"), int)
                else elapsed_ms
            ),
            constrained=constrained,
            finish_reason=str(response.get("done_reason") or "stop"),
            raw=response,
        )

    # -------------------------------------------------------------- embedding

    def embed(self, texts: list[str]) -> list[Embedding]:
        """Embed a batch, preferring the modern batch endpoint.

        ``/api/embed`` (batch) arrived in Ollama 0.3; ``/api/embeddings``
        (single) is the legacy path. Both are supported because the clustering
        stage would otherwise be gated on a server upgrade, and the fallback is
        only slower, not different.
        """
        if not texts:
            return []

        identity = self.identity()
        try:
            response = self._transport.post_json(
                "/api/embed",
                {"model": self.model, "input": texts, "keep_alive": self.keep_alive},
            )
            vectors = response.get("embeddings")
            if isinstance(vectors, list) and len(vectors) == len(texts):
                return [Embedding(vector=tuple(float(x) for x in v), identity=identity)
                        for v in vectors]
        except ProviderUnavailable:
            # Fall through to the legacy endpoint below; a genuinely dead
            # daemon will fail there too, with the same error type.
            pass

        results: list[Embedding] = []
        for text in texts:
            response = self._transport.post_json(
                "/api/embeddings", {"model": self.model, "prompt": text}
            )
            vector = response.get("embedding")
            if not isinstance(vector, list):
                raise GenerationFailed(
                    f"Ollama returned no embedding for {self.model!r}", provider=self.name
                )
            results.append(
                Embedding(vector=tuple(float(x) for x in vector), identity=identity)
            )
        return results

    # ------------------------------------------------------------- inspection

    def list_models(self) -> list[dict[str, Any]]:
        """Enumerate installed models. Backs ``abca models list``."""
        payload = self._transport.get_json("/api/tags")
        models = payload.get("models") or []
        return [
            {
                "name": entry.get("name"),
                "digest": _normalize_digest(entry.get("digest")),
                "size_bytes": entry.get("size"),
                "quantization": (entry.get("details") or {}).get("quantization_level"),
                "parameter_size": (entry.get("details") or {}).get("parameter_size"),
                "family": (entry.get("details") or {}).get("family"),
            }
            for entry in models
            if isinstance(entry, dict)
        ]

    @property
    def supports_constrained_decoding(self) -> bool | None:
        """Whether the daemon accepts a JSON Schema as ``format``.

        ``None`` until :meth:`health` has run.
        """
        return self._supports_schema_format


__all__ = [
    "DEFAULT_BASE_URL",
    "MIN_STRUCTURED_OUTPUT_VERSION",
    "OllamaProvider",
]
