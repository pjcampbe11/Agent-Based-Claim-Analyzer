"""llama.cpp provider -- the strongest pinning and the strongest constraints.

WHY THIS BACKEND EXISTS ALONGSIDE OLLAMA
========================================
Ollama pins a model by its *manifest* digest, which is excellent and is the
default. This backend goes one step further and pins the **GGUF file itself**
by SHA-256, and it uses **GBNF grammars** for constrained decoding, which is
the reference implementation of that feature rather than a wrapper over it.

Use it when a verdict needs the strongest possible provenance -- a verdict
somebody intends to publish, say -- and Ollama's convenience is not needed.

TALKING TO ``llama-server``, NOT LINKING THE LIBRARY
====================================================
This drives llama.cpp's HTTP server rather than importing
``llama-cpp-python``. Three reasons:

1. ``llama-cpp-python`` is a compiled extension whose build depends on the
   local CUDA/Metal toolchain. Making it a hard dependency would mean abCA
   fails to install on machines that only ever intend to use Ollama.
2. The server process owns the model's lifetime, so weights stay resident
   across the thousands of classifier calls a large thread produces.
3. It is testable. Like every provider here, this one is exercised against
   :class:`~abca.providers.transport.FakeTransport` with no GPU and no model
   file.

HASHING A 18 GB FILE
====================
Pinning by content hash means actually hashing the weights, which for a
quantized 27B is tens of gigabytes and takes minutes. Doing that on every run
would be absurd; skipping it would make the pin a lie.

So the hash is computed once and cached, keyed by ``(path, size, mtime_ns)``.
If any of those three change, the cache misses and the file is re-hashed. The
cache is a convenience over an expensive pure function, never a substitute for
it -- there is no code path that reports a hash it did not compute from the
bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from abca.canonical import DIGEST_PREFIX
from abca.providers.base import (
    Completion,
    Embedding,
    GenerationFailed,
    GenerationRequest,
    PinningUnavailable,
    ProviderUnavailable,
)
from abca.providers.grammar import GrammarError, json_schema_to_gbnf
from abca.providers.transport import HttpTransport, Transport, monotonic_ms

DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: Read size when hashing weights. 8 MiB balances syscall overhead against
#: resident memory on a machine already hosting a large model.
_HASH_CHUNK = 8 * 1024 * 1024


def _weights_cache_path() -> Path:
    """Where computed weight hashes are remembered between runs."""
    from abca.ledger.store import default_data_dir

    return default_data_dir() / "weights-hashes.json"


def hash_weights_file(path: Path, *, cache: bool = True) -> str:
    """SHA-256 a GGUF file, memoized on ``(path, size, mtime_ns)``.

    The cache key includes size and modification time, so replacing the file
    invalidates the entry. That is weaker than re-hashing every time and
    stronger than trusting a filename: an attacker who can rewrite the weights
    while preserving both size and mtime could defeat it, but such an attacker
    already has write access to the machine producing the verdicts, at which
    point the ledger is not the weak link.
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise PinningUnavailable(
            f"weights file not found at {resolved}", provider="llamacpp"
        )

    stat = resolved.stat()
    key = f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}"

    cache_path = _weights_cache_path()
    entries: dict[str, str] = {}
    if cache and cache_path.is_file():
        try:
            entries = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt cache is a reason to recompute, never a reason to fail.
            entries = {}
        if key in entries:
            return entries[key]

    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    value = DIGEST_PREFIX + digest.hexdigest()

    if cache:
        entries[key] = value
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError:
            # Cache writes are best-effort; a read-only data dir must not
            # prevent a run from proceeding with a correctly computed hash.
            pass
    return value


class LlamaCppProvider:
    """Drives a running ``llama-server`` instance."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        role: str = "adjudicator",
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 300.0,
        model_name: str | None = None,
        hash_weights: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        model_path
            Path to the GGUF being served. Optional: when omitted, the path is
            discovered from the server's ``/props``. Supplying it explicitly is
            necessary when the server runs on a different machine, where its
            reported path means nothing locally.
        hash_weights
            Set ``False`` only when the weights file is genuinely unreachable
            (a remote server). The run is then marked unreproducible, which is
            the honest outcome -- the alternative would be a pin that nobody
            can check.
        """
        self.name = "llamacpp"
        self.role = role
        self.model_path = Path(model_path) if model_path else None
        self.model_name = model_name or (self.model_path.name if self.model_path else "llamacpp")
        self.hash_weights = hash_weights
        self._transport: Transport = transport or HttpTransport(
            base_url, timeout=timeout, provider_name="llamacpp"
        )
        self._identity_cache: Any = None
        self._props: dict[str, Any] | None = None

    # ----------------------------------------------------------------- health

    def health(self) -> None:
        try:
            self._props = self._transport.get_json("/props")
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(
                f"cannot reach llama-server: {exc}. Start it with "
                "`llama-server -m <model.gguf> --port 8080`.",
                provider=self.name,
            ) from exc
        self.identity()

    def _discover_model_path(self) -> Path | None:
        """Ask the server which file it loaded.

        Only usable when the server is on this machine. A path reported by a
        remote server is meaningless locally, which is exactly the case
        ``hash_weights=False`` exists for.
        """
        if self._props is None:
            try:
                self._props = self._transport.get_json("/props")
            except ProviderUnavailable:
                return None

        for key in ("model_path", "model"):
            value = self._props.get(key)
            if isinstance(value, str) and value:
                candidate = Path(value)
                if candidate.is_file():
                    return candidate

        settings = self._props.get("default_generation_settings")
        if isinstance(settings, dict):
            value = settings.get("model")
            if isinstance(value, str) and Path(value).is_file():
                return Path(value)
        return None

    # --------------------------------------------------------------- identity

    def identity(self):
        from abca.schema.ledger import ModelIdentity  # local: avoids a cycle

        if self._identity_cache is not None:
            return self._identity_cache

        weights_hash: str | None = None
        if self.hash_weights:
            path = self.model_path or self._discover_model_path()
            if path is None:
                raise PinningUnavailable(
                    "could not determine the GGUF path, so the weights cannot be "
                    "hashed. Pass model_path=..., or set hash_weights=False to "
                    "run unpinned (the run will be marked reproducible: false).",
                    provider=self.name,
                )
            weights_hash = hash_weights_file(path)
            # A path discovered from /props must also supply the display name.
            # Without this, `model_name` keeps its "llamacpp" placeholder and
            # the quantization is parsed from that instead of from the real
            # GGUF filename -- silently losing metadata in the one case where
            # the user did not spell the path out themselves.
            if self.model_path is None:
                self.model_name = path.name
            self.model_path = path

        context_length = None
        if isinstance(self._props, dict):
            settings = self._props.get("default_generation_settings")
            if isinstance(settings, dict) and isinstance(settings.get("n_ctx"), int):
                context_length = settings["n_ctx"]

        self._identity_cache = ModelIdentity(
            role=self.role,
            provider=self.name,
            name=self.model_name,
            weights_hash=weights_hash,
            quantization=_quantization_from_name(self.model_name),
            context_length=context_length,
        )
        return self._identity_cache

    # ------------------------------------------------------------- generation

    def generate(self, request: GenerationRequest) -> Completion:
        prompt = request.prompt
        if request.system:
            # llama-server's /completion is a raw-text endpoint with no chat
            # template applied, so the system message is prepended explicitly.
            # Deliberate: applying a template here would silently change the
            # prompt and make the recorded prompt hash disagree with what the
            # model actually saw.
            prompt = f"{request.system}\n\n{prompt}"

        payload: dict[str, Any] = {
            "prompt": prompt,
            "temperature": request.temperature,
            "seed": request.seed,
            "stream": False,
            "cache_prompt": True,
        }
        if request.max_tokens is not None:
            payload["n_predict"] = request.max_tokens
        if request.stop:
            payload["stop"] = list(request.stop)

        constrained = False
        if request.json_schema is not None:
            try:
                payload["grammar"] = json_schema_to_gbnf(request.json_schema)
                constrained = True
            except GrammarError as exc:
                # A schema we cannot express as a grammar degrades to
                # validate-and-retry rather than failing the run. Recorded on
                # the completion so the weaker path is visible in the ledger.
                payload["grammar"] = None
                payload.pop("grammar")
                constrained = False
                _ = exc

        started = time.perf_counter()
        response = self._transport.post_json("/completion", payload)
        elapsed_ms = monotonic_ms(started)

        text = response.get("content")
        if not isinstance(text, str):
            raise GenerationFailed(
                f"llama-server returned no 'content'; got keys {sorted(response)}",
                provider=self.name,
            )

        # llama-server signals truncation two ways depending on version.
        stopped_by_limit = bool(response.get("stopped_limit")) or (
            response.get("stop_type") == "limit"
        )

        return Completion(
            text=text,
            identity=self.identity(),
            prompt_tokens=int(response.get("tokens_evaluated") or 0),
            completion_tokens=int(response.get("tokens_predicted") or 0),
            duration_ms=elapsed_ms,
            constrained=constrained,
            finish_reason="length" if stopped_by_limit else "stop",
            raw=response,
        )

    def embed(self, texts: list[str]) -> list[Embedding]:
        """Embed via ``/embedding``.

        Requires the server to have been started with ``--embedding``; without
        it llama-server returns an error, which surfaces as a transport-level
        failure with the server's own message attached.
        """
        if not texts:
            return []
        identity = self.identity()
        results: list[Embedding] = []
        for text in texts:
            response = self._transport.post_json("/embedding", {"content": text})
            vector = response.get("embedding")
            if isinstance(vector, list) and vector and isinstance(vector[0], list):
                vector = vector[0]  # some builds nest the vector one level
            if not isinstance(vector, list):
                raise GenerationFailed(
                    "llama-server returned no embedding; was it started with "
                    "--embedding?",
                    provider=self.name,
                )
            results.append(Embedding(vector=tuple(float(x) for x in vector), identity=identity))
        return results


def _quantization_from_name(name: str) -> str | None:
    """Recover the quantization tag from a GGUF filename.

    GGUF files conventionally embed it (``...-Q5_K_M.gguf``). Best-effort
    metadata only -- the weights hash is what actually pins the model, so a
    miss here costs nothing.
    """
    stem = os.path.basename(name).rsplit(".", 1)[0]
    for part in reversed(stem.replace(".", "-").split("-")):
        upper = part.upper()
        if upper.startswith(("Q2", "Q3", "Q4", "Q5", "Q6", "Q8", "F16", "F32", "BF16", "IQ")):
            return upper
    return None


__all__ = ["DEFAULT_BASE_URL", "LlamaCppProvider", "hash_weights_file"]
