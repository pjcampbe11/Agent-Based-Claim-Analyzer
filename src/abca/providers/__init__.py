"""Provider layer: one interface, four backends.

The pipeline talks only to :class:`abca.providers.base.Provider`. Nothing
downstream knows which backend is answering, which is what keeps backend
behavior out of analysis logic.

Backends, ordered by how well a third party can verify a run made with them:

============== ============================= ==================================
Backend        Pinning                       Constrained decoding
============== ============================= ==================================
``llamacpp``   SHA-256 of the GGUF file      GBNF grammar (reference impl.)
``ollama``     manifest digest via /api/tags JSON Schema as ``format``
``anthropic``  none (hosted)                 forced tool-use with input_schema
``openai``     none (hosted)                 ``strict`` json_schema mode
============== ============================= ==================================

Only the first two produce ``reproducible: true`` runs. That is a property of
the backends, not a policy choice -- a hosted endpoint has no content hash to
pin, so no claim about which weights produced a verdict can be checked.
"""

from abca.providers.base import (
    Completion,
    Embedding,
    GenerationFailed,
    GenerationRequest,
    ModelNotFound,
    PinningUnavailable,
    Provider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from abca.providers.registry import (
    ProviderRegistry,
    RoleProvider,
    build_provider,
    build_registry,
)
from abca.providers.structured import (
    ExtractionStrategy,
    FailureKind,
    StructuredAttempt,
    StructuredOutputError,
    StructuredResult,
    extract_json,
    generate_structured,
)
from abca.providers.transport import FakeTransport, HttpTransport, Transport

__all__ = [
    "Completion", "Embedding", "ExtractionStrategy", "FailureKind", "FakeTransport",
    "GenerationFailed", "GenerationRequest", "HttpTransport", "ModelNotFound",
    "PinningUnavailable", "Provider", "ProviderError", "ProviderRegistry",
    "ProviderTimeout", "ProviderUnavailable", "RoleProvider", "StructuredAttempt",
    "StructuredOutputError", "StructuredResult", "Transport", "build_provider",
    "build_registry", "extract_json", "generate_structured",
]
