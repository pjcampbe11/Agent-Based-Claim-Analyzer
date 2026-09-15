"""Anthropic (Claude) provider.

WHY A SEPARATE MODULE RATHER THAN A base_url ON THE OPENAI PROVIDER
===================================================================
The Messages API differs from OpenAI's chat-completions shape in four ways
that matter here, and papering over them with a shared client would produce
subtle breakage rather than a clean abstraction:

* **Auth header.** ``x-api-key`` plus a required ``anthropic-version``, not
  ``Authorization: Bearer``.
* **System prompt.** A top-level ``system`` field, not a message with
  ``role: "system"``.
* **``max_tokens`` is required**, not optional. A request without it is
  rejected outright.
* **Structured output is tool-forcing**, not a ``response_format``. There is
  no JSON mode; the equivalent guarantee comes from declaring a tool whose
  ``input_schema`` is the target schema and then forcing that tool.

That last point is the interesting one, and it is genuinely the STRONGEST
structured-output path of any hosted provider here.

TOOL-FORCING AS CONSTRAINED OUTPUT
==================================
Instead of asking for JSON and hoping, abCA declares a single tool whose
``input_schema`` is the pydantic schema and sets
``tool_choice={"type": "tool", "name": ...}``. The model must then emit a
``tool_use`` block, and that block's ``input`` is already a structured object
conforming to the declared schema -- there is no prose to strip, no code fence
to unwrap, and no brace-scanning fallback.

So ``constrained=True`` is honest on this path, and the extraction ladder in
:mod:`abca.providers.structured` should report ``DIRECT`` every time. If it
ever reports anything else here, something is wrong upstream.

REPRODUCIBILITY
===============
Same as every hosted backend: ``weights_hash`` is ``None``, the run is marked
``reproducible: false``, and verification returns ``UNREPLAYABLE``. Anthropic
does not expose a content hash for the weights behind a model name, and
inventing one would make the flag meaningless. Claude is a good choice for
exploration and for a dissenting voice in consensus mode; local open-weight
models remain the choice for anything published.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from abca.providers.base import (
    Completion,
    Embedding,
    GenerationFailed,
    GenerationRequest,
    ProviderError,
    ProviderUnavailable,
)
from abca.providers.transport import HttpTransport, Transport, monotonic_ms

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"

#: Pinned API version header. Anthropic requires it on every request, and
#: pinning it means a future breaking change surfaces as an explicit upgrade
#: here rather than as mysterious behavior drift mid-run.
ANTHROPIC_VERSION = "2023-06-01"

#: The Messages API requires `max_tokens`. This default is generous enough for
#: a full claim adjudication with citations and red-team findings; the caller
#: overrides it via GenerationRequest.max_tokens.
DEFAULT_MAX_TOKENS = 4096

#: Name of the forced tool. Arbitrary but stable -- it appears in the request
#: and would show up in anyone's API logs, so it is legible rather than cryptic.
_TOOL_NAME = "emit_abca_output"


class MissingCredentials(ProviderUnavailable):
    """No Anthropic API key was supplied."""


class AnthropicProvider:
    """Claude via the Anthropic Messages API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        *,
        role: str = "adjudicator",
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 300.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        model
            Any Claude model string. Not validated against a hardcoded list --
            such a list is stale the week after it ships, and a wrong name
            produces a clear 404 from the API anyway.
        api_key
            Literal key; takes precedence over ``api_key_env``. Convenient for
            a notebook, but do not commit it.
        api_key_env
            Environment variable holding the key. Defaults to
            ``ANTHROPIC_API_KEY``. This is the recommended path.
        """
        self.name = "api:anthropic"
        self.vendor = "anthropic"
        self.model = model
        self.role = role
        self.base_url = base_url
        self.max_tokens = max_tokens

        self._api_key = api_key or os.environ.get(api_key_env) or None
        self.credential_source = (
            "argument" if api_key else (f"env:{api_key_env}" if self._api_key else "none")
        )
        if self._api_key is None:
            raise MissingCredentials(
                f"no Anthropic API key. Set ${api_key_env}, or pass api_key=... in "
                "code, or set api_key/api_key_env in the [models.<role>] section "
                "of your abca config.toml.",
                provider=self.name,
            )

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            **(extra_headers or {}),
        }
        self._transport: Transport = transport or HttpTransport(
            base_url, timeout=timeout, provider_name=self.name, headers=headers
        )

    def __repr__(self) -> str:
        """Redacted -- a key must never reach a log, traceback, or bug report."""
        return (
            f"AnthropicProvider(model={self.model!r}, base_url={self.base_url!r}, "
            f"credential_source={self.credential_source!r}, api_key=<redacted>)"
        )

    # ----------------------------------------------------------------- health

    def health(self) -> None:
        """Confirm the endpoint answers and the credential is accepted.

        Uses a one-token generation rather than a listing endpoint, because
        that is the only call guaranteed to exercise both reachability and
        auth. It costs a fraction of a cent and it turns "invalid API key"
        into an error at second one instead of at claim four hundred.
        """
        try:
            self._transport.post_json(
                "/messages",
                {
                    "model": self.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        except ProviderError as exc:
            raise ProviderUnavailable(
                f"cannot reach the Anthropic API or the credential was rejected: {exc}",
                provider=self.name,
            ) from exc

    # --------------------------------------------------------------- identity

    def identity(self):
        """Identity with ``weights_hash=None``. See the module docstring.

        As with every hosted backend, this is the honest answer. Synthesizing
        a hash from the model string would make ``reproducible: true`` a claim
        the record cannot support.
        """
        from abca.schema.ledger import ModelIdentity  # local: avoids a cycle

        return ModelIdentity(
            role=self.role,
            provider=self.name,
            name=self.model,
            weights_hash=None,
            quantization=None,
            context_length=None,
        )

    # ------------------------------------------------------------- generation

    @staticmethod
    def _extract_text(blocks: list[Any]) -> str:
        """Concatenate the text blocks of a Messages response."""
        return "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    @staticmethod
    def _extract_tool_input(blocks: list[Any]) -> dict[str, Any] | None:
        """Return the forced tool's input object, if the model emitted one."""
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                payload = block.get("input")
                if isinstance(payload, dict):
                    return payload
        return None

    def generate(self, request: GenerationRequest) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens or self.max_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": request.temperature,
        }
        if request.system:
            # Top-level field, not a message. Sending it as a message would
            # silently downgrade it to ordinary user text.
            payload["system"] = request.system
        if request.stop:
            payload["stop_sequences"] = list(request.stop)

        # NOTE: `seed` is deliberately NOT sent. The Messages API has no seed
        # parameter, and quietly dropping a determinism knob the caller asked
        # for is exactly the sort of hidden divergence the ledger exists to
        # expose. The run is already marked unreproducible, which is the
        # honest signal.

        constrained = False
        if request.json_schema is not None:
            payload["tools"] = [
                {
                    "name": _TOOL_NAME,
                    "description": (
                        "Emit the analysis result. Every field must satisfy the "
                        "declared schema."
                    ),
                    "input_schema": request.json_schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": _TOOL_NAME}
            constrained = True

        started = time.perf_counter()
        response = self._transport.post_json("/messages", payload)
        elapsed_ms = monotonic_ms(started)

        blocks = response.get("content")
        if not isinstance(blocks, list):
            raise GenerationFailed(
                f"Anthropic returned no content blocks; got keys {sorted(response)}",
                provider=self.name,
            )

        if constrained:
            tool_input = self._extract_tool_input(blocks)
            if tool_input is None:
                # Forced tool use should make this impossible. If it happens,
                # fall back to the text blocks so the structured layer can try
                # its extraction ladder rather than failing the run outright --
                # but report constrained=False, because the guarantee did not
                # hold and the ledger must say so.
                text = self._extract_text(blocks)
                constrained = False
            else:
                # Re-serialize so the structured layer has one uniform input
                # type across every provider. It parses back to exactly this
                # object via the DIRECT strategy.
                text = json.dumps(tool_input)
        else:
            text = self._extract_text(blocks)

        usage = response.get("usage") or {}
        stop_reason = str(response.get("stop_reason") or "end_turn")

        return Completion(
            text=text,
            identity=self.identity(),
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            duration_ms=elapsed_ms,
            constrained=constrained,
            # Normalize to the vocabulary Completion.truncated understands.
            finish_reason="length" if stop_reason == "max_tokens" else stop_reason,
            raw=response,
        )

    # -------------------------------------------------------------- embedding

    def embed(self, texts: list[str]) -> list[Embedding]:
        """Not supported: Anthropic does not offer an embeddings endpoint.

        Raised rather than silently returning nothing, because the clustering
        stage would otherwise degrade to "every claim is its own cluster" and
        the run would just be quietly more expensive with no explanation.
        Configure a separate ``[models.embedder]`` -- a local ``nomic-embed-text``
        on Ollama is the usual answer and costs nothing.
        """
        raise GenerationFailed(
            "Anthropic does not provide an embeddings API. Configure a separate "
            "[models.embedder] role (e.g. provider=\"ollama\", "
            "model=\"nomic-embed-text\").",
            provider=self.name,
        )


__all__ = [
    "ANTHROPIC_VERSION",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "AnthropicProvider",
    "MissingCredentials",
]
