"""Config loading, credential resolution, and registry construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from abca.config import (
    DEFAULT_KEY_ENV,
    Config,
    ConfigError,
    ModelSpec,
    default_config_path,
    load_config,
    write_starter_config,
)
from abca.providers.registry import build_provider, build_registry
from abca.providers.transport import FakeTransport

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def ollama_fake(*models_and_digests):
    return FakeTransport({
        "/api/version": {"version": "0.6.0"},
        "/api/tags": {"models": [
            {"name": name, "digest": digest, "details": {"quantization_level": "Q5_K_M"}}
            for name, digest in models_and_digests
        ]},
        "/api/show": {"details": {"quantization_level": "Q5_K_M"},
                      "model_info": {"qwen2.context_length": 32768}},
    })


def write(tmp_path, body, name="config.toml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestLoading:
    def test_missing_file_is_not_fatal_by_default(self, tmp_path):
        """Read-only commands must work on a fresh install."""
        config = load_config(tmp_path / "absent.toml")
        assert config.models == {}
        assert config.source is None

    def test_missing_file_is_fatal_when_required(self, tmp_path):
        with pytest.raises(ConfigError, match="abca config init"):
            load_config(tmp_path / "absent.toml", required=True)

    def test_invalid_toml_names_the_file(self, tmp_path):
        path = write(tmp_path, "this is not = = toml")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(path)

    def test_starter_config_round_trips(self, tmp_path):
        path = write_starter_config(tmp_path / "config.toml")
        config = load_config(path)
        assert set(config.models) == {
            "classifier", "adjudicator", "redteam", "backtranslate", "embedder"}
        assert config.defaults.temperature == 0.0

    def test_starter_config_refuses_to_clobber(self, tmp_path):
        path = write_starter_config(tmp_path / "config.toml")
        with pytest.raises(ConfigError, match="--force"):
            write_starter_config(path)
        write_starter_config(path, overwrite=True)  # explicit opt-in works

    def test_unknown_role_is_rejected_not_ignored(self, tmp_path):
        """A typo'd section would otherwise leave the real role unconfigured."""
        path = write(tmp_path, '[models.adjudicater]\nmodel="x"\n')
        with pytest.raises(ConfigError, match="not a known role"):
            load_config(path)

    def test_unknown_provider_is_rejected(self, tmp_path):
        path = write(tmp_path, '[models.adjudicator]\nprovider="magic"\nmodel="x"\n')
        with pytest.raises(ConfigError, match="not recognised"):
            load_config(path)

    def test_missing_model_name_is_rejected(self, tmp_path):
        path = write(tmp_path, '[models.adjudicator]\nprovider="ollama"\n')
        with pytest.raises(ConfigError, match="requires a `model`"):
            load_config(path)


class TestFidelityGateIndependence:
    """The rule enforced in three places because it has no natural symptom."""

    def test_same_model_rejected_at_config_load(self, tmp_path):
        path = write(tmp_path, (
            '[models.adjudicator]\nmodel="qwen2.5:32b"\n'
            '[models.backtranslate]\nmodel="qwen2.5:32b"\n'
        ))
        with pytest.raises(ConfigError, match="independent model"):
            load_config(path)

    def test_different_names_same_weights_rejected_at_registry_build(self, tmp_path):
        """A string comparison would miss two names pointing at one model."""
        path = write(tmp_path, (
            '[models.adjudicator]\nmodel="qwen2.5:32b"\n'
            '[models.backtranslate]\nmodel="qwen2.5:32b-alias"\n'
        ))
        config = load_config(path)
        transports = {
            "adjudicator": ollama_fake(("qwen2.5:32b", DIGEST_A)),
            "backtranslate": ollama_fake(("qwen2.5:32b-alias", DIGEST_A)),
        }
        with pytest.raises(ConfigError, match="same model as adjudicator"):
            build_registry(config, transports=transports)

    def test_genuinely_different_models_accepted(self, tmp_path):
        path = write(tmp_path, (
            '[models.adjudicator]\nmodel="qwen2.5:32b"\n'
            '[models.backtranslate]\nmodel="llama3.1:8b"\n'
        ))
        registry = build_registry(load_config(path), transports={
            "adjudicator": ollama_fake(("qwen2.5:32b", DIGEST_A)),
            "backtranslate": ollama_fake(("llama3.1:8b", DIGEST_B)),
        })
        assert registry.roles() == ["adjudicator", "backtranslate"]

    def test_unreachable_backend_does_not_become_a_config_error(self, tmp_path):
        """Connectivity problems must be reported as connectivity problems."""
        from abca.providers.transport import TransportError

        path = write(tmp_path, (
            '[models.adjudicator]\nmodel="a"\n[models.backtranslate]\nmodel="b"\n'
        ))
        dead = FakeTransport(errors={"/api/tags": TransportError("down", provider="ollama")})
        registry = build_registry(
            load_config(path), transports={"adjudicator": dead, "backtranslate": dead}
        )
        assert set(registry.health_check()) == {"adjudicator", "backtranslate"}


class TestCredentials:
    def test_provider_specific_env_var_defaults(self):
        assert DEFAULT_KEY_ENV["anthropic"] == "ANTHROPIC_API_KEY"
        assert DEFAULT_KEY_ENV["openai"] == "OPENAI_API_KEY"

    def test_claude_is_an_alias_for_anthropic(self, tmp_path):
        path = write(tmp_path, '[models.adjudicator]\nprovider="claude"\nmodel="claude-x"\n')
        spec = load_config(path).spec("adjudicator")
        assert spec.resolved_key_env() == "ANTHROPIC_API_KEY"

    def test_openai_section_never_reads_the_anthropic_key(self, tmp_path, monkeypatch):
        """Cross-vendor key leakage would be a security bug, not a convenience."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        path = write(tmp_path, '[models.adjudicator]\nprovider="openai"\nmodel="gpt-4o"\n')
        assert load_config(path).spec("adjudicator").credential_status().startswith("missing")

    def test_explicit_env_var_overrides_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-x")
        path = write(tmp_path, (
            '[models.adjudicator]\nprovider="openai"\nmodel="gpt-4o"\napi_key_env="MY_KEY"\n'
        ))
        assert load_config(path).spec("adjudicator").credential_status() == "env:MY_KEY"

    def test_inline_key_warns_but_works(self, tmp_path):
        path = write(tmp_path, (
            '[models.adjudicator]\nprovider="openai"\nmodel="gpt-4o"\napi_key="sk-secret"\n'
        ))
        config = load_config(path)
        assert any("do not commit" in warning for warning in config.warnings)
        assert config.spec("adjudicator").credential_status() == "inline (config file)"

    def test_local_providers_report_no_credential_need(self, tmp_path, monkeypatch):
        """A scary 'missing key' on an Ollama row would be noise."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        path = write(tmp_path, '[models.adjudicator]\nprovider="ollama"\nmodel="x"\n')
        assert load_config(path).spec("adjudicator").credential_status() == "n/a (local)"

    def test_redaction_covers_every_display_path(self, tmp_path):
        spec = ModelSpec(role="adjudicator", provider="openai", model="gpt-4o",
                         api_key="sk-VERY-SECRET")
        assert "VERY-SECRET" not in repr(spec)
        assert "VERY-SECRET" not in str(spec.redacted())

    def test_config_redaction_is_recursive(self, tmp_path):
        path = write(tmp_path, (
            '[models.adjudicator]\nprovider="openai"\nmodel="gpt-4o"\napi_key="sk-LEAK"\n'
        ))
        assert "sk-LEAK" not in repr(load_config(path).redacted())


class TestRegistry:
    def test_partial_role_construction(self, tmp_path):
        """The fast profile does not need backtranslate; building it would waste a call."""
        path = write(tmp_path, (
            '[models.classifier]\nmodel="small"\n'
            '[models.adjudicator]\nmodel="big"\n'
            '[models.backtranslate]\nmodel="other"\n'
        ))
        registry = build_registry(
            load_config(path), roles=["classifier"],
            transports={"classifier": ollama_fake(("small", DIGEST_A))},
        )
        assert registry.roles() == ["classifier"]

    def test_pinnability_is_derived_from_the_backend(self, tmp_path):
        path = write(tmp_path, '[models.adjudicator]\nmodel="qwen2.5:32b"\n')
        registry = build_registry(load_config(path), transports={
            "adjudicator": ollama_fake(("qwen2.5:32b", DIGEST_A))
        })
        assert registry.is_fully_pinnable
        assert registry.unpinnable_models() == []

    def test_hosted_model_reports_as_unpinnable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        path = write(tmp_path, (
            '[models.adjudicator]\nprovider="anthropic"\nmodel="claude-sonnet-4-5"\n'
        ))
        registry = build_registry(load_config(path), transports={
            "adjudicator": FakeTransport({"/messages": {"content": []}})
        })
        assert not registry.is_fully_pinnable
        assert registry.unpinnable_models() == ["claude-sonnet-4-5"]

    def test_unknown_role_lookup_is_a_clean_error(self, tmp_path):
        path = write(tmp_path, '[models.adjudicator]\nmodel="x"\n')
        registry = build_registry(load_config(path), transports={
            "adjudicator": ollama_fake(("x", DIGEST_A))
        })
        with pytest.raises(ConfigError, match="no provider built"):
            registry.get("embedder")

    def test_missing_role_in_config_is_a_clean_error(self):
        with pytest.raises(ConfigError, match=r"\[models.adjudicator\]"):
            Config().spec("adjudicator")

    def test_every_provider_kind_can_be_built(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        weights = tmp_path / "m.gguf"
        weights.write_bytes(b"w")

        specs = [
            ModelSpec(role="adjudicator", provider="ollama", model="m"),
            ModelSpec(role="adjudicator", provider="openai", model="gpt-4o"),
            ModelSpec(role="adjudicator", provider="anthropic", model="claude-x"),
            ModelSpec(role="adjudicator", provider="llamacpp", model="m.gguf",
                      model_path=str(weights)),
        ]
        for spec in specs:
            provider = build_provider(spec, transport=FakeTransport({}))
            assert provider.name


class TestConfigPath:
    def test_windows_uses_appdata_roaming(self, monkeypatch):
        """Config roams between machines; run records do not. Different dirs on purpose."""
        monkeypatch.setenv("APPDATA", r"C:\Users\pat\AppData\Roaming")
        assert str(default_config_path()).startswith(r"C:\Users\pat\AppData\Roaming")

    def test_posix_honors_xdg_config_home(self, monkeypatch):
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", "/srv/cfg")
        assert default_config_path().as_posix() == "/srv/cfg/abca/config.toml"


# ---------------------------------------------------------------------------
# The CI model config (.github/ci-model-config.toml)
# ---------------------------------------------------------------------------
# This file is what makes the `model` CI job possible: it pins runner-sized
# open-weight models so the two checks that used to be permanently deferred for
# want of a configured model can actually execute.
#
# It is never loaded by the test suite's normal paths, so nothing else would
# notice if it drifted out of the schema. A CI job that fails at 3am on a
# schedule with a config error is a job people stop reading.


CI_MODEL_CONFIG = Path(__file__).resolve().parent.parent / ".github" / "ci-model-config.toml"


def test_the_ci_model_config_exists_and_loads():
    assert CI_MODEL_CONFIG.exists(), (
        "the model CI job points ABCA_CONFIG at this file; without it the job "
        "falls back to defaults that pin a 32B model no runner can serve"
    )
    load_config(CI_MODEL_CONFIG)


def test_the_ci_model_config_fills_every_required_role():
    """A missing role fails at the registry, deep into the job, not at load."""
    from abca.pipeline.orchestrator import REQUIRED_ROLES

    config = load_config(CI_MODEL_CONFIG)
    missing = [role for role in REQUIRED_ROLES if role not in config.models]
    assert not missing, f"CI config is missing required role(s): {missing}"


def test_the_ci_backtranslator_is_independent_of_the_adjudicator():
    """The fidelity gate's whole validity rests on this.

    A model that grades its own simplification reproduces its own misreadings,
    so the gate passes everything and reports a number that means nothing. The
    loader enforces it; this test pins the intent, because the cheapest way to
    "fix" a future loader error would be to point both roles at whatever model
    the runner already has cached.
    """
    config = load_config(CI_MODEL_CONFIG)
    assert config.models["backtranslate"].model != config.models["adjudicator"].model


def test_the_ci_models_are_pinned_to_exact_tags():
    """A floating tag makes scheduled runs incomparable to each other."""
    config = load_config(CI_MODEL_CONFIG)
    for role, spec in config.models.items():
        assert ":" in spec.model, (
            f"[models.{role}] uses {spec.model!r} with no tag; a floating tag "
            "can change the model under a scheduled job, and two runs of a "
            "scheduled check that used different weights cannot be compared"
        )
        assert not spec.model.endswith(":latest"), (
            f"[models.{role}] is pinned to :latest, which is not a pin"
        )
