"""Provider tests. No network, no GPU, no API key, no model files.

Every backend is exercised through
:class:`~abca.providers.transport.FakeTransport`, which is why the whole suite
runs in under a second on any machine.

The recurring theme is the pinning contract: a provider must derive
``weights_hash`` from its backend and must never fabricate one, because
``reproducible: true`` is computed from that field and every downstream
guarantee rests on it.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from abca.providers.anthropic import AnthropicProvider
from abca.providers.anthropic import MissingCredentials as AnthropicMissingKey
from abca.providers.base import (
    GenerationFailed,
    GenerationRequest,
    ModelNotFound,
    PinningUnavailable,
    Provider,
    ProviderTimeout,
    ProviderUnavailable,
)
from abca.providers.llamacpp import LlamaCppProvider, hash_weights_file
from abca.providers.ollama import OllamaProvider, _normalize_digest, _parse_version
from abca.providers.openai_compat import (
    MissingCredentials,
    OpenAICompatibleProvider,
    strictify_schema,
)
from abca.providers.transport import FakeTransport, HttpTransport, TransportError

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def ollama_transport(*, version="0.6.0", digest=DIGEST_A, response='{"ok":true}'):
    return FakeTransport({
        "/api/version": {"version": version},
        "/api/tags": {"models": [
            {"name": "qwen2.5:32b", "digest": digest, "size": 1234,
             "details": {"quantization_level": "Q5_K_M", "parameter_size": "32B",
                         "family": "qwen2"}},
            {"name": "nomic-embed-text", "digest": DIGEST_B, "size": 274,
             "details": {"quantization_level": "F16", "parameter_size": "137M"}},
        ]},
        "/api/show": {"details": {"quantization_level": "Q5_K_M"},
                      "model_info": {"qwen2.context_length": 32768}},
        "/api/generate": {"response": response, "prompt_eval_count": 12,
                          "eval_count": 7, "total_duration": 2_000_000_000,
                          "done_reason": "stop"},
        "/api/embed": {"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
    })


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


class TestTransport:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError, match="http or https"):
            HttpTransport("ftp://example.com")

    def test_suggests_a_tunnel_rather_than_exposure(self):
        """The setup guide's security model is tunnel-only; the error reinforces it."""
        with pytest.raises(ValueError, match="SSH tunnel"):
            HttpTransport("ws://example.com")

    def test_parses_host_port_and_prefix(self):
        transport = HttpTransport("https://proxy.example/ollama")
        assert (transport._host, transport._port, transport._prefix) == (
            "proxy.example", 443, "/ollama",
        )

    def test_fake_records_calls(self):
        fake = FakeTransport({"/x": {"ok": True}})
        fake.post_json("/x", {"a": 1})
        assert fake.call_count("/x") == 1
        assert fake.payloads_for("/x") == [{"a": 1}]

    def test_fake_scripts_a_sequence(self):
        fake = FakeTransport({"/x": [{"n": 1}, {"n": 2}]})
        assert [fake.post_json("/x", {})["n"] for _ in range(3)] == [1, 2, 2]


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------


class TestOllamaPinning:
    def test_digest_becomes_the_weights_hash(self):
        """The property that makes local-first worth the trouble."""
        provider = OllamaProvider("qwen2.5:32b", transport=ollama_transport())
        provider.health()
        identity = provider.identity()
        assert identity.weights_hash == f"sha256:{DIGEST_A}"
        assert identity.is_pinnable

    def test_bare_and_prefixed_digests_both_normalize(self):
        assert _normalize_digest(DIGEST_A) == f"sha256:{DIGEST_A}"
        assert _normalize_digest(f"sha256:{DIGEST_A}") == f"sha256:{DIGEST_A}"

    def test_malformed_digest_is_rejected_not_passed_through(self):
        assert _normalize_digest("not-a-hash") is None
        assert _normalize_digest("") is None

    def test_missing_digest_raises_rather_than_degrading(self):
        """Silently returning None would flip a run's reproducibility unnoticed."""
        transport = ollama_transport(digest="garbage")
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        with pytest.raises(PinningUnavailable, match="reproducible"):
            provider.identity()

    def test_quantization_and_context_length_are_recorded(self):
        """Context length is a recipe parameter: it changes output on identical input."""
        provider = OllamaProvider("qwen2.5:32b", transport=ollama_transport())
        provider.health()
        identity = provider.identity()
        assert identity.quantization == "Q5_K_M"
        assert identity.context_length == 32768

    def test_identity_is_cached(self):
        transport = ollama_transport()
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        for _ in range(5):
            provider.identity()
        assert transport.call_count("/api/tags") == 1


class TestOllamaModelResolution:
    def test_latest_suffix_is_matched(self):
        transport = FakeTransport({
            "/api/version": {"version": "0.6.0"},
            "/api/tags": {"models": [{"name": "mistral:latest", "digest": DIGEST_A}]},
            "/api/show": {},
        })
        provider = OllamaProvider("mistral", transport=transport)
        assert provider.identity().name == "mistral"

    def test_missing_model_names_what_is_available(self):
        transport = FakeTransport({
            "/api/version": {"version": "0.6.0"},
            "/api/tags": {"models": [{"name": "llama3.1:8b", "digest": DIGEST_A}]},
        })
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        with pytest.raises(ModelNotFound, match="llama3.1:8b"):
            provider.health()

    def test_unreachable_daemon_suggests_the_fix(self):
        transport = FakeTransport(
            errors={"/api/version": TransportError("refused", provider="ollama")}
        )
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        with pytest.raises(ProviderUnavailable, match="ollama serve"):
            provider.health()

    def test_remote_failure_mentions_the_tunnel(self):
        """The EC2 topology's most common failure has its own hint."""
        transport = FakeTransport(
            errors={"/api/version": TransportError("timeout", provider="ollama")}
        )
        with pytest.raises(ProviderUnavailable, match="SSH tunnel"):
            OllamaProvider("m", transport=transport).health()


class TestOllamaGeneration:
    def test_schema_is_sent_as_format_on_modern_servers(self):
        transport = ollama_transport(version="0.6.0")
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        completion = provider.generate(
            GenerationRequest(prompt="hi", json_schema={"type": "object"})
        )
        assert transport.payloads_for("/api/generate")[0]["format"] == {"type": "object"}
        assert completion.constrained is True

    def test_old_servers_fall_back_to_plain_json_mode(self):
        """Pre-0.5 has no schema support; the retry loop carries the burden."""
        transport = ollama_transport(version="0.4.7")
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        completion = provider.generate(
            GenerationRequest(prompt="hi", json_schema={"type": "object"})
        )
        assert transport.payloads_for("/api/generate")[0]["format"] == "json"
        assert completion.constrained is False

    def test_unparseable_version_degrades_safely(self):
        assert _parse_version("0.0.0-rc1") == (0, 0, 0)
        assert _parse_version("") is None
        assert _parse_version("main") is None

    def test_determinism_knobs_are_forwarded(self):
        transport = ollama_transport()
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        provider.generate(
            GenerationRequest(prompt="hi", seed=99, temperature=0.0,
                              context_length=8192, max_tokens=512, stop=("END",))
        )
        options = transport.payloads_for("/api/generate")[0]["options"]
        assert options == {"temperature": 0.0, "seed": 99, "num_ctx": 8192,
                           "num_predict": 512, "stop": ["END"]}

    def test_keep_alive_is_sent(self):
        """Without it, a 27B reloads per claim and `standard` becomes unusable."""
        transport = ollama_transport()
        provider = OllamaProvider("qwen2.5:32b", transport=transport, keep_alive="45m")
        provider.health()
        provider.generate(GenerationRequest(prompt="hi"))
        assert transport.payloads_for("/api/generate")[0]["keep_alive"] == "45m"

    def test_streaming_is_off(self):
        """A partial stream cannot be schema-checked, so streaming buys nothing."""
        transport = ollama_transport()
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        provider.generate(GenerationRequest(prompt="hi"))
        assert transport.payloads_for("/api/generate")[0]["stream"] is False

    def test_token_counts_and_server_timing_are_used(self):
        provider = OllamaProvider("qwen2.5:32b", transport=ollama_transport())
        provider.health()
        completion = provider.generate(GenerationRequest(prompt="hi"))
        assert (completion.prompt_tokens, completion.completion_tokens) == (12, 7)
        assert completion.duration_ms == 2000  # from total_duration, not wall clock
        assert completion.total_tokens == 19

    def test_missing_response_field_fails_loudly(self):
        transport = ollama_transport()
        transport._responses["/api/generate"] = {"unexpected": True}
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        with pytest.raises(GenerationFailed, match="no 'response'"):
            provider.generate(GenerationRequest(prompt="hi"))


class TestOllamaEmbedding:
    def test_batch_endpoint_preferred(self):
        transport = ollama_transport()
        provider = OllamaProvider("nomic-embed-text", transport=transport)
        embeddings = provider.embed(["a", "b"])
        assert [e.vector for e in embeddings] == [(0.1, 0.2), (0.3, 0.4)]
        assert transport.call_count("/api/embed") == 1

    def test_legacy_fallback(self):
        transport = FakeTransport(
            {"/api/version": {"version": "0.3.0"},
             "/api/tags": {"models": [{"name": "nomic-embed-text", "digest": DIGEST_A}]},
             "/api/show": {},
             "/api/embeddings": {"embedding": [1.0, 2.0]}},
            errors={"/api/embed": TransportError("404", provider="ollama")},
        )
        provider = OllamaProvider("nomic-embed-text", transport=transport)
        assert provider.embed(["a"])[0].vector == (1.0, 2.0)

    def test_empty_input_makes_no_call(self):
        transport = ollama_transport()
        assert OllamaProvider("m", transport=transport).embed([]) == []
        assert transport.calls == []

    def test_identity_travels_with_the_vector(self):
        """Embeddings from different models are not comparable; clustering must not mix them."""
        provider = OllamaProvider("nomic-embed-text", transport=ollama_transport())
        assert provider.embed(["a", "b"])[0].identity.weights_hash == f"sha256:{DIGEST_B}"


# --------------------------------------------------------------------------
# OpenAI-compatible
# --------------------------------------------------------------------------


def openai_transport(content='{"ok":true}'):
    return FakeTransport({
        "/chat/completions": {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        },
        "/models": {"data": [{"id": "gpt-4o", "owned_by": "openai"}]},
        "/embeddings": {"data": [{"index": 1, "embedding": [3.0]},
                                 {"index": 0, "embedding": [1.0]}]},
    })


class TestOpenAICredentials:
    def test_env_var_is_the_default_path(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        provider = OpenAICompatibleProvider("gpt-4o", transport=openai_transport())
        assert provider.credential_source == "env:OPENAI_API_KEY"

    def test_explicit_key_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        provider = OpenAICompatibleProvider(
            "gpt-4o", api_key="sk-explicit", transport=openai_transport()
        )
        assert provider.credential_source == "argument"

    def test_custom_env_var_name(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-custom")
        provider = OpenAICompatibleProvider(
            "x", api_key_env="MY_KEY", transport=openai_transport()
        )
        assert provider.credential_source == "env:MY_KEY"

    def test_missing_key_on_a_hosted_endpoint_is_an_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(MissingCredentials, match="OPENAI_API_KEY"):
            OpenAICompatibleProvider("gpt-4o", transport=openai_transport())

    @pytest.mark.parametrize(
        "url", ["http://localhost:8000/v1", "http://127.0.0.1:11434/v1"]
    )
    def test_local_endpoints_need_no_key(self, monkeypatch, url):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = OpenAICompatibleProvider("m", base_url=url, transport=openai_transport())
        assert provider.credential_source == "none"

    def test_repr_redacts_the_key(self, monkeypatch):
        """A key must never reach a log, a traceback, or a pasted bug report."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
        provider = OpenAICompatibleProvider("gpt-4o", transport=openai_transport())
        assert "sk-super-secret-value" not in repr(provider)
        assert "<redacted>" in repr(provider)


class TestOpenAIBehaviour:
    def test_never_pinnable(self, monkeypatch):
        """Not a limitation to route around -- the honest answer."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        provider = OpenAICompatibleProvider("gpt-4o", transport=openai_transport())
        assert provider.identity().weights_hash is None
        assert not provider.identity().is_pinnable

    def test_strict_schema_mode_is_used_when_possible(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        transport = openai_transport()
        provider = OpenAICompatibleProvider("gpt-4o", transport=transport)
        completion = provider.generate(
            GenerationRequest(prompt="hi", json_schema={
                "title": "T", "type": "object",
                "properties": {"a": {"type": "string"}}, "required": ["a"]})
        )
        response_format = transport.payloads_for("/chat/completions")[0]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        assert completion.constrained is True

    def test_plain_json_mode_when_strict_disabled(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        transport = openai_transport()
        provider = OpenAICompatibleProvider(
            "gpt-4o", transport=transport, strict_schema=False
        )
        completion = provider.generate(
            GenerationRequest(prompt="hi", json_schema={"type": "object"})
        )
        assert transport.payloads_for("/chat/completions")[0]["response_format"] == {
            "type": "json_object"
        }
        assert completion.constrained is False

    def test_refusal_is_reported(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        transport = FakeTransport({"/chat/completions": {
            "choices": [{"message": {"refusal": "I cannot help with that"},
                         "finish_reason": "stop"}]}})
        provider = OpenAICompatibleProvider("gpt-4o", transport=transport)
        with pytest.raises(GenerationFailed, match="refusal"):
            provider.generate(GenerationRequest(prompt="hi"))

    def test_embeddings_are_reordered_by_index(self, monkeypatch):
        """The API does not promise response order; a shuffled batch would misassign."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        provider = OpenAICompatibleProvider("text-embedding-3", transport=openai_transport())
        assert [e.vector for e in provider.embed(["a", "b"])] == [(1.0,), (3.0,)]


class TestStrictify:
    def test_all_properties_become_required(self):
        from abca.schema.core import Claim

        strict = strictify_schema(Claim.model_json_schema())
        assert set(strict["required"]) == set(strict["properties"])

    def test_additional_properties_disabled(self):
        from abca.schema.core import Claim

        assert strictify_schema(Claim.model_json_schema())["additionalProperties"] is False

    def test_previously_optional_fields_become_nullable(self):
        strict = strictify_schema({
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        })
        assert strict["properties"]["a"]["type"] == "string"
        assert strict["properties"]["b"]["type"] == ["integer", "null"]

    def test_nested_definitions_are_transformed(self):
        from abca.schema.core import Claim

        strict = strictify_schema(Claim.model_json_schema())
        citation = strict["$defs"]["Citation"]
        assert citation["additionalProperties"] is False
        assert set(citation["required"]) == set(citation["properties"])


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


def anthropic_transport(*, blocks=None, stop_reason="tool_use"):
    return FakeTransport({"/messages": {
        "content": blocks if blocks is not None else [
            {"type": "tool_use", "name": "emit_abca_output", "input": {"ok": True}}
        ],
        "usage": {"input_tokens": 30, "output_tokens": 11},
        "stop_reason": stop_reason,
    }})


class TestAnthropic:
    def test_env_var_credential(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        provider = AnthropicProvider(transport=anthropic_transport())
        assert provider.credential_source == "env:ANTHROPIC_API_KEY"

    def test_explicit_key(self):
        provider = AnthropicProvider(api_key="sk-ant-inline", transport=anthropic_transport())
        assert provider.credential_source == "argument"

    def test_missing_key_is_an_error(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(AnthropicMissingKey, match="ANTHROPIC_API_KEY"):
            AnthropicProvider(transport=anthropic_transport())

    def test_repr_redacts(self):
        provider = AnthropicProvider(api_key="sk-ant-SECRET", transport=anthropic_transport())
        assert "SECRET" not in repr(provider)

    def test_never_pinnable(self):
        provider = AnthropicProvider(api_key="k", transport=anthropic_transport())
        assert provider.identity().weights_hash is None

    def test_system_prompt_is_a_top_level_field(self):
        """Sending it as a message would silently downgrade it to user text."""
        transport = anthropic_transport()
        provider = AnthropicProvider(api_key="k", transport=transport)
        provider.generate(GenerationRequest(prompt="p", system="s"))
        payload = transport.payloads_for("/messages")[0]
        assert payload["system"] == "s"
        assert payload["messages"] == [{"role": "user", "content": "p"}]

    def test_max_tokens_is_always_sent(self):
        """The Messages API rejects a request without it."""
        transport = anthropic_transport()
        AnthropicProvider(api_key="k", transport=transport).generate(
            GenerationRequest(prompt="p")
        )
        assert transport.payloads_for("/messages")[0]["max_tokens"] > 0

    def test_seed_is_not_silently_dropped_into_the_payload(self):
        """Anthropic has no seed parameter; sending one would be a lie.

        The run is already marked unreproducible, which is the honest signal.
        """
        transport = anthropic_transport()
        AnthropicProvider(api_key="k", transport=transport).generate(
            GenerationRequest(prompt="p", seed=7)
        )
        assert "seed" not in transport.payloads_for("/messages")[0]

    def test_structured_output_uses_forced_tool_use(self):
        transport = anthropic_transport()
        provider = AnthropicProvider(api_key="k", transport=transport)
        completion = provider.generate(
            GenerationRequest(prompt="p", json_schema={"type": "object"})
        )
        payload = transport.payloads_for("/messages")[0]
        assert payload["tool_choice"]["type"] == "tool"
        assert payload["tools"][0]["input_schema"] == {"type": "object"}
        assert completion.constrained is True

    def test_tool_input_parses_via_the_direct_strategy(self):
        """Tool-forcing yields a clean object -- no fence, no prose, no scan."""
        from abca.providers.structured import ExtractionStrategy, extract_json

        provider = AnthropicProvider(api_key="k", transport=anthropic_transport())
        completion = provider.generate(
            GenerationRequest(prompt="p", json_schema={"type": "object"})
        )
        payload, strategy = extract_json(completion.text)
        assert strategy is ExtractionStrategy.DIRECT
        assert payload == {"ok": True}

    def test_missing_tool_block_falls_back_and_reports_unconstrained(self):
        """If the guarantee did not hold, the ledger must not claim it did."""
        transport = anthropic_transport(
            blocks=[{"type": "text", "text": '{"ok":true}'}], stop_reason="end_turn"
        )
        provider = AnthropicProvider(api_key="k", transport=transport)
        completion = provider.generate(
            GenerationRequest(prompt="p", json_schema={"type": "object"})
        )
        assert completion.constrained is False
        assert completion.text == '{"ok":true}'

    def test_max_tokens_stop_reason_maps_to_truncated(self):
        transport = anthropic_transport(
            blocks=[{"type": "text", "text": "cut off"}], stop_reason="max_tokens"
        )
        completion = AnthropicProvider(api_key="k", transport=transport).generate(
            GenerationRequest(prompt="p")
        )
        assert completion.truncated

    def test_version_header_is_pinned(self):
        from abca.providers.anthropic import ANTHROPIC_VERSION

        transport = HttpTransport("https://api.anthropic.com/v1",
                                  headers={"anthropic-version": ANTHROPIC_VERSION})
        assert transport._headers["anthropic-version"] == ANTHROPIC_VERSION

    def test_embeddings_are_refused_with_a_pointer_to_the_fix(self):
        provider = AnthropicProvider(api_key="k", transport=anthropic_transport())
        with pytest.raises(GenerationFailed, match="nomic-embed-text"):
            provider.embed(["a"])

    def test_token_usage_is_recorded(self):
        completion = AnthropicProvider(api_key="k", transport=anthropic_transport()).generate(
            GenerationRequest(prompt="p")
        )
        assert (completion.prompt_tokens, completion.completion_tokens) == (30, 11)


# --------------------------------------------------------------------------
# llama.cpp
# --------------------------------------------------------------------------


class TestLlamaCpp:
    @pytest.fixture
    def weights(self, tmp_path):
        path = tmp_path / "model-Q5_K_M.gguf"
        path.write_bytes(b"weights" * 1000)
        return path

    def test_weights_hash_matches_a_direct_sha256(self, weights):
        expected = "sha256:" + hashlib.sha256(weights.read_bytes()).hexdigest()
        assert hash_weights_file(weights, cache=False) == expected

    def test_hash_changes_when_the_file_changes(self, weights):
        first = hash_weights_file(weights, cache=False)
        weights.write_bytes(b"different")
        assert hash_weights_file(weights, cache=False) != first

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(PinningUnavailable, match="not found"):
            hash_weights_file(tmp_path / "absent.gguf", cache=False)

    def test_gbnf_grammar_is_sent(self, weights):
        transport = FakeTransport({
            "/props": {"model_path": str(weights)},
            "/completion": {"content": '{"ok":true}', "tokens_evaluated": 5,
                            "tokens_predicted": 3, "stop_type": "eos"},
        })
        provider = LlamaCppProvider(weights, transport=transport)
        completion = provider.generate(GenerationRequest(
            prompt="hi",
            json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                         "required": ["ok"]},
        ))
        grammar = transport.payloads_for("/completion")[0]["grammar"]
        assert grammar.startswith("root ::=")
        assert completion.constrained is True

    def test_system_prompt_is_prepended_not_templated(self, weights):
        """Applying a chat template here would make the recorded prompt a lie."""
        transport = FakeTransport({
            "/props": {"model_path": str(weights)},
            "/completion": {"content": "x", "tokens_evaluated": 1, "tokens_predicted": 1},
        })
        LlamaCppProvider(weights, transport=transport).generate(
            GenerationRequest(prompt="P", system="S")
        )
        assert transport.payloads_for("/completion")[0]["prompt"] == "S\n\nP"

    def test_unpinned_mode_is_explicit(self, weights):
        """For a remote server whose file we cannot see. Honest, not silent."""
        transport = FakeTransport({
            "/props": {}, "/completion": {"content": "x"},
        })
        provider = LlamaCppProvider(
            model_name="remote.gguf", transport=transport, hash_weights=False
        )
        assert provider.identity().weights_hash is None

    def test_undiscoverable_path_raises_with_the_two_options(self, weights):
        transport = FakeTransport({"/props": {}})
        provider = LlamaCppProvider(transport=transport)
        with pytest.raises(PinningUnavailable, match="hash_weights=False"):
            provider.identity()

    def test_truncation_is_detected(self, weights):
        transport = FakeTransport({
            "/props": {"model_path": str(weights)},
            "/completion": {"content": "cut", "stopped_limit": True},
        })
        completion = LlamaCppProvider(weights, transport=transport).generate(
            GenerationRequest(prompt="hi")
        )
        assert completion.truncated


# --------------------------------------------------------------------------
# Protocol conformance
# --------------------------------------------------------------------------


class TestProtocolConformance:
    def test_every_backend_satisfies_the_protocol(self, tmp_path, monkeypatch):
        """The pipeline must never be able to tell which backend answered."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        weights = tmp_path / "m.gguf"
        weights.write_bytes(b"w")

        providers = [
            OllamaProvider("qwen2.5:32b", transport=ollama_transport()),
            OpenAICompatibleProvider("gpt-4o", transport=openai_transport()),
            AnthropicProvider(api_key="k", transport=anthropic_transport()),
            LlamaCppProvider(weights, transport=FakeTransport({"/props": {}})),
        ]
        for provider in providers:
            assert isinstance(provider, Provider), provider.name


class TestWeightsHashCache:
    """The cache is a convenience over an expensive pure function.

    There is deliberately no path that reports a hash it did not compute from
    the bytes -- the cache only skips recomputation when path, size and mtime
    all match.
    """

    @pytest.fixture
    def cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        return tmp_path

    def test_second_call_hits_the_cache(self, cache_dir):
        weights = cache_dir / "m.gguf"
        weights.write_bytes(b"x" * 1000)
        first = hash_weights_file(weights)
        second = hash_weights_file(weights)
        assert first == second
        cache_file = cache_dir / "data" / "abca" / "weights-hashes.json"
        assert cache_file.is_file()
        assert len(json.loads(cache_file.read_text())) == 1

    def test_changed_file_invalidates_the_entry(self, cache_dir):
        weights = cache_dir / "m.gguf"
        weights.write_bytes(b"x" * 1000)
        first = hash_weights_file(weights)
        weights.write_bytes(b"y" * 2000)
        assert hash_weights_file(weights) != first

    def test_corrupt_cache_is_recomputed_not_fatal(self, cache_dir):
        weights = cache_dir / "m.gguf"
        weights.write_bytes(b"x" * 10)
        cache_file = cache_dir / "data" / "abca" / "weights-hashes.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{ not json")
        expected = "sha256:" + hashlib.sha256(b"x" * 10).hexdigest()
        assert hash_weights_file(weights) == expected


class TestUnusedProviderSurfaces:
    def test_ollama_list_models_shape(self):
        entries = OllamaProvider("qwen2.5:32b", transport=ollama_transport()).list_models()
        assert entries[0]["name"] == "qwen2.5:32b"
        assert entries[0]["digest"] == f"sha256:{DIGEST_A}"
        assert entries[0]["quantization"] == "Q5_K_M"

    def test_openai_list_models_reports_no_digest(self, monkeypatch):
        """Hosted models have nothing to pin, and the listing says so."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        entries = OpenAICompatibleProvider("gpt-4o", transport=openai_transport()).list_models()
        assert entries == [{"name": "gpt-4o", "digest": None, "owned_by": "openai"}]

    def test_openai_health_reports_unreachable(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        transport = FakeTransport(errors={"/models": TransportError("down", provider="api")})
        with pytest.raises(ProviderUnavailable):
            OpenAICompatibleProvider("gpt-4o", transport=transport).health()

    def test_anthropic_health_uses_a_one_token_call(self):
        """Turns a bad key into an error at second one, not at claim four hundred."""
        transport = anthropic_transport()
        AnthropicProvider(api_key="k", transport=transport).health()
        assert transport.payloads_for("/messages")[0]["max_tokens"] == 1

    def test_anthropic_health_wraps_a_rejected_credential(self):
        transport = FakeTransport(
            errors={"/messages": TransportError("HTTP 401", provider="api:anthropic")}
        )
        with pytest.raises(ProviderUnavailable, match="credential was rejected"):
            AnthropicProvider(api_key="bad", transport=transport).health()

    def test_openai_empty_embed_makes_no_call(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        transport = openai_transport()
        assert OpenAICompatibleProvider("e", transport=transport).embed([]) == []
        assert transport.calls == []

    def test_llamacpp_empty_embed_makes_no_call(self, tmp_path):
        weights = tmp_path / "m.gguf"
        weights.write_bytes(b"w")
        transport = FakeTransport({"/props": {"model_path": str(weights)}})
        assert LlamaCppProvider(weights, transport=transport).embed([]) == []

    def test_llamacpp_embedding_requires_the_server_flag(self, tmp_path):
        weights = tmp_path / "m.gguf"
        weights.write_bytes(b"w")
        transport = FakeTransport({
            "/props": {"model_path": str(weights)},
            "/embedding": {"nope": True},
        })
        with pytest.raises(GenerationFailed, match="--embedding"):
            LlamaCppProvider(weights, transport=transport).embed(["a"])

    def test_llamacpp_health_suggests_the_start_command(self):
        transport = FakeTransport(errors={"/props": TransportError("down", provider="llamacpp")})
        with pytest.raises(ProviderUnavailable, match="llama-server -m"):
            LlamaCppProvider(model_name="x", transport=transport, hash_weights=False).health()

    def test_llamacpp_discovers_the_path_from_props(self, tmp_path):
        weights = tmp_path / "found-Q4_K_M.gguf"
        weights.write_bytes(b"w" * 50)
        transport = FakeTransport({"/props": {"model_path": str(weights)}})
        provider = LlamaCppProvider(transport=transport)
        provider.health()
        assert provider.identity().weights_hash is not None
        assert provider.identity().quantization == "Q4_K_M"

    def test_ollama_timeout_propagates_as_timeout(self):
        transport = FakeTransport(
            errors={"/api/generate": ProviderTimeout("slow", provider="ollama")}
        )
        transport._responses.update(ollama_transport()._responses)
        transport._errors = {"/api/generate": ProviderTimeout("slow", provider="ollama")}
        provider = OllamaProvider("qwen2.5:32b", transport=transport)
        provider.health()
        with pytest.raises(ProviderTimeout):
            provider.generate(GenerationRequest(prompt="hi"))
