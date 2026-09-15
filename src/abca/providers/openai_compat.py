"""OpenAI-compatible provider (OpenAI, Azure, OpenRouter, vLLM, LM Studio...).

WHEN TO USE THIS
================
When you want a frontier hosted model's reasoning and are willing to give up
third-party verifiability to get it. That trade is real and this module makes
it explicit rather than hiding it:

    **Every run using this provider is marked ``reproducible: false``.**

Not as a penalty -- as a fact. ``ModelIdentity.weights_hash`` is ``None``
because a hosted endpoint has no content hash. The vendor can change the
weights behind ``gpt-4o`` overnight without changing the model string, so a
third party cannot re-execute a published verdict and confirm they got the
same model. :func:`abca.ledger.verify.compare` therefore returns
``UNREPLAYABLE`` for these runs, even when the replay output is byte-identical
-- because agreement by coincidence is not verification.

Use hosted models for exploration, for a second opinion in consensus mode, or
where verifiability genuinely does not matter. Use local open-weight models for
anything published under the tool's name.

CREDENTIALS
===========
Three ways to supply a key, in resolution order:

1. ``api_key="sk-..."`` passed directly in code.
2. ``api_key_env="MY_VAR"`` naming an environment variable (default:
   ``OPENAI_API_KEY``).
3. Nothing -- valid for a local OpenAI-compatible server (vLLM, LM Studio,
   Ollama's own ``/v1``) that needs no auth.

The key is NEVER written to a run record, never logged, and is redacted in
``repr()``. Run records are meant to be published; a credential leaking into
one would be published with it.
"""

from __future__ import annotations

import copy
import os
import time
from typing import Any

from abca.providers.base import (
    Completion,
    Embedding,
    GenerationFailed,
    GenerationRequest,
    ProviderUnavailable,
)
from abca.providers.transport import HttpTransport, Transport, monotonic_ms

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class MissingCredentials(ProviderUnavailable):
    """No API key was supplied and the endpoint appears to require one."""


# --------------------------------------------------------------------------
# Strict structured outputs
# --------------------------------------------------------------------------

def _is_local_endpoint(base_url: str) -> bool:
    """Whether this looks like a server that needs no credentials.

    Used only to decide whether a MISSING key is an error. A local vLLM or
    Ollama ``/v1`` endpoint legitimately has no auth; api.openai.com does not.
    """
    lowered = base_url.lower()
    return any(
        marker in lowered
        for marker in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ".local")
    )


def strictify_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Rewrite a pydantic schema into OpenAI's strict structured-output form.

    OpenAI's ``strict: true`` mode gives genuine constrained decoding -- the
    same guarantee GBNF gives on llama.cpp -- but it imposes two requirements
    that pydantic's output does not satisfy:

    1. Every object must set ``additionalProperties: false``.
    2. Every property must appear in ``required``. Optionality is expressed by
       making the TYPE nullable, not by omitting the key.

    This function performs that transform. Marking an optional field
    ``required`` and nullable is semantically safe for our models: every
    optional field in :mod:`abca.schema.core` is either ``X | None`` already or
    has a default, so a model emitting ``null`` produces the same validated
    object as one omitting the key.

    Returns ``None`` when the schema uses a construct this transform cannot
    safely handle, in which case the caller falls back to plain JSON mode plus
    validate-and-retry. Returning ``None`` rather than raising is deliberate:
    a schema we cannot strictify is a reason to use the weaker path, not a
    reason to fail the run.
    """
    try:
        converted = _strictify_node(copy.deepcopy(schema), depth=0)
    except _Unstrictifiable:
        return None
    return converted


class _Unstrictifiable(Exception):
    """Internal signal that the strict transform cannot proceed."""


def _strictify_node(node: Any, depth: int) -> Any:
    if depth > 24:
        raise _Unstrictifiable("schema too deep")
    if not isinstance(node, dict):
        return node

    for key in ("$defs", "definitions"):
        if key in node and isinstance(node[key], dict):
            node[key] = {
                name: _strictify_node(sub, depth + 1) for name, sub in node[key].items()
            }

    for key in ("anyOf", "oneOf", "allOf"):
        if key in node and isinstance(node[key], list):
            node[key] = [_strictify_node(item, depth + 1) for item in node[key]]

    if "items" in node:
        node["items"] = _strictify_node(node["items"], depth + 1)

    properties = node.get("properties")
    if isinstance(properties, dict):
        node["additionalProperties"] = False
        node["properties"] = {
            name: _strictify_node(sub, depth + 1) for name, sub in properties.items()
        }
        previously_required = set(node.get("required") or [])
        # Every property becomes required; the optional ones become nullable.
        node["required"] = list(node["properties"])
        for name, sub in node["properties"].items():
            if name not in previously_required:
                node["properties"][name] = _make_nullable(sub)

    return node


def _make_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """Allow ``null`` for a schema node without disturbing its other keywords."""
    if not isinstance(node, dict):
        return node
    if "anyOf" in node:
        options = node["anyOf"]
        if not any(isinstance(o, dict) and o.get("type") == "null" for o in options):
            node["anyOf"] = [*options, {"type": "null"}]
        return node
    if "$ref" in node:
        # A bare $ref cannot carry sibling keywords in strict mode; wrap it.
        return {"anyOf": [{"$ref": node.pop("$ref")}, {"type": "null"}]}
    node_type = node.get("type")
    if isinstance(node_type, str):
        node["type"] = [node_type, "null"]
    elif isinstance(node_type, list) and "null" not in node_type:
        node["type"] = [*node_type, "null"]
    return node


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


class OpenAICompatibleProvider:
    """Chat-completions client for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str,
        *,
        role: str = "adjudicator",
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        vendor: str = "openai",
        organization: str | None = None,
        transport: Transport | None = None,
        timeout: float = 300.0,
        extra_headers: dict[str, str] | None = None,
        strict_schema: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        model
            Any model the endpoint serves -- ``gpt-4o``, ``o3``, a vLLM-served
            open-weight model, whatever. Not validated against a hardcoded
            list, deliberately: such a list is wrong the week after it ships.
        api_key
            Literal key. Takes precedence over ``api_key_env``. Convenient for
            a notebook; do not commit it.
        api_key_env
            Environment variable to read the key from. Defaults to
            ``OPENAI_API_KEY``. This is the recommended path.
        base_url
            Point at Azure, OpenRouter, Together, a local vLLM, or Ollama's own
            ``/v1`` shim. The last case is interesting: it works, but it loses
            the weight pinning that :class:`~abca.providers.ollama.OllamaProvider`
            gives you over the SAME server -- so prefer the native provider for
            Ollama.
        strict_schema
            Attempt OpenAI's ``strict`` structured-output mode, which is real
            constrained decoding. Falls back to plain JSON mode automatically
            when the schema cannot be strictified.
        """
        self.name = f"api:{vendor}"
        self.vendor = vendor
        self.model = model
        self.role = role
        self.base_url = base_url
        self.strict_schema = strict_schema

        # Resolution order: explicit argument, then environment. Recorded so
        # `abca models show` can tell the operator WHERE the key came from
        # without ever showing the key.
        self._api_key = api_key or os.environ.get(api_key_env) or None
        self.credential_source = (
            "argument" if api_key else (f"env:{api_key_env}" if self._api_key else "none")
        )

        if self._api_key is None and not _is_local_endpoint(base_url):
            raise MissingCredentials(
                f"no API key for {base_url}. Set ${api_key_env}, or pass "
                "api_key=... in code, or set api_key/api_key_env in the "
                "[models.<role>] section of your abca config.toml.",
                provider=self.name,
            )

        headers = {**(extra_headers or {})}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if organization:
            headers["OpenAI-Organization"] = organization

        self._transport: Transport = transport or HttpTransport(
            base_url, timeout=timeout, provider_name=self.name, headers=headers
        )

    def __repr__(self) -> str:
        """Redacted. A key must never reach a log, a traceback, or a bug report."""
        return (
            f"OpenAICompatibleProvider(model={self.model!r}, base_url={self.base_url!r}, "
            f"credential_source={self.credential_source!r}, api_key=<redacted>)"
        )

    # ----------------------------------------------------------------- health

    def health(self) -> None:
        """Confirm the endpoint answers. Does not validate the model name.

        Some compatible servers do not implement ``/models``, so a failure
        there is reported as unavailable but a missing model is left to surface
        on first use -- being stricter would lock out working endpoints.
        """
        try:
            self._transport.get_json("/models")
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(
                f"cannot reach {self.base_url}: {exc}", provider=self.name
            ) from exc

    # --------------------------------------------------------------- identity

    def identity(self):
        """Identity with ``weights_hash=None`` -- see the module docstring.

        This is the honest answer, not a limitation to work around. Anyone
        tempted to synthesize a hash here (from the model string, say) should
        note that it would make ``reproducible: true`` a claim the record
        cannot support, which is the one failure this system must not have.
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

    def _response_format(self, schema: dict[str, Any] | None) -> tuple[dict | None, bool]:
        """Choose the strongest available output constraint.

        Returns ``(response_format, constrained)`` where ``constrained``
        records whether the backend actually enforced the schema during
        decoding -- that flag ends up in the ledger, so it must be truthful.
        """
        if schema is None:
            return None, False

        if self.strict_schema:
            strict = strictify_schema(schema)
            if strict is not None:
                return (
                    {
                        "type": "json_schema",
                        "json_schema": {
                            "name": str(schema.get("title") or "abca_output"),
                            "schema": strict,
                            "strict": True,
                        },
                    },
                    True,
                )

        # Plain JSON mode: guarantees a syntactically valid object and nothing
        # more. The validate-and-retry loop enforces the schema.
        return {"type": "json_object"}, False

    def generate(self, request: GenerationRequest) -> Completion:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            # Best-effort only. OpenAI documents `seed` as a hint, not a
            # guarantee -- which is another reason these runs are marked
            # unreproducible rather than merely unpinned.
            "seed": request.seed,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = list(request.stop)

        response_format, constrained = self._response_format(request.json_schema)
        if response_format is not None:
            payload["response_format"] = response_format

        started = time.perf_counter()
        response = self._transport.post_json("/chat/completions", payload)
        elapsed_ms = monotonic_ms(started)

        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GenerationFailed(
                f"{self.vendor} returned no choices; got keys {sorted(response)}",
                provider=self.name,
            )
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            # A refusal comes back in its own field on newer OpenAI models.
            refusal = message.get("refusal")
            raise GenerationFailed(
                f"{self.vendor} returned no content"
                + (f" (refusal: {refusal})" if refusal else ""),
                provider=self.name,
            )

        usage = response.get("usage") or {}
        return Completion(
            text=text,
            identity=self.identity(),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            duration_ms=elapsed_ms,
            constrained=constrained,
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            raw=response,
        )

    # -------------------------------------------------------------- embedding

    def embed(self, texts: list[str]) -> list[Embedding]:
        if not texts:
            return []
        response = self._transport.post_json(
            "/embeddings", {"model": self.model, "input": texts}
        )
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise GenerationFailed(
                f"{self.vendor} returned {len(data) if isinstance(data, list) else '?'} "
                f"embeddings for {len(texts)} inputs",
                provider=self.name,
            )
        identity = self.identity()
        # Sort by index: the API does not promise response order matches input
        # order, and a silently shuffled batch would mis-assign every vector.
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        return [
            Embedding(vector=tuple(float(x) for x in item["embedding"]), identity=identity)
            for item in ordered
        ]

    def list_models(self) -> list[dict[str, Any]]:
        payload = self._transport.get_json("/models")
        return [
            {"name": entry.get("id"), "digest": None, "owned_by": entry.get("owned_by")}
            for entry in (payload.get("data") or [])
            if isinstance(entry, dict)
        ]


__all__ = [
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BASE_URL",
    "MissingCredentials",
    "OpenAICompatibleProvider",
    "strictify_schema",
]
