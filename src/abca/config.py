"""Configuration: which model fills which pipeline role.

FORMAT
======
TOML, read with stdlib :mod:`tomllib`. One section per pipeline role::

    [defaults]
    profile        = "standard"
    seed           = 42
    temperature    = 0.0
    max_claims     = 200
    reading_level  = 8

    [models.classifier]
    provider = "ollama"
    model    = "qwen2.5:7b-instruct"

    [models.adjudicator]
    provider = "ollama"
    model    = "qwen2.5:32b-instruct"

    [models.backtranslate]
    provider = "ollama"
    model    = "llama3.1:8b-instruct"

    [models.embedder]
    provider = "ollama"
    model    = "nomic-embed-text"

CREDENTIALS
===========
For hosted providers, the key is resolved in this order:

1. ``api_key`` in the config file -- convenient, but the file is then a
   secret. Never commit it. ``.gitignore`` covers ``config.toml`` and the
   loader warns when it sees an inline key.
2. ``api_key_env`` naming an environment variable (default
   ``OPENAI_API_KEY``). **This is the recommended path.**
3. Nothing, which is valid for a local OpenAI-compatible server.

Keys are held in memory only. They are never written to a run record, never
logged, and never included in any digest -- run records are meant to be
published, and a credential in one would be published with it.

WHY ROLES AND NOT ONE MODEL
===========================
The roles exist so cheap work runs cheap. ``classifier`` runs once per claim
-- thousands of times on a large thread -- while ``adjudicator`` runs on the
claims that survive gating. Putting a 27B in the classifier slot is the single
easiest way to make the ``fast`` profile useless.

``backtranslate`` is not a performance role, it is a correctness one: it MUST
be a different model from ``adjudicator``, because a model grading its own
simplification reproduces its own misreadings and the fidelity gate silently
becomes a no-op. That constraint is enforced in three places -- here, in
:class:`~abca.schema.ledger.RunConfig`, and in
:func:`abca.providers.registry.build_registry` -- because it is the kind of
mistake that produces no error and no visible symptom.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from abca.schema.enums import Profile

#: Roles the pipeline knows about. An unknown role in config is an error
#: rather than a silently ignored section -- a typo'd ``[models.adjudicater]``
#: would otherwise leave the real adjudicator unconfigured and the failure
#: would surface much later, somewhere confusing.
KNOWN_ROLES: tuple[str, ...] = (
    "classifier",
    "segmenter",
    "adjudicator",
    "redteam",
    "backtranslate",
    "embedder",
    "consensus",
)

#: Roles that fall back to another role when not configured. Segmentation is a
#: linguistic task that a small model handles well, so it shares the classifier
#: by default -- but decomposing a dense legal sentence into atomic claims
#: rewards a stronger model, so it can be split out without restructuring
#: anything.
ROLE_FALLBACKS: dict[str, str] = {
    "segmenter": "classifier",
    # The red team falls back to the adjudicator rather than failing, because a
    # same-model adversarial pass still finds things and refusing to run one at
    # all would be worse. But it is WEAKER -- a model reviewing its own
    # reasoning is the one least able to see where it reached -- so the
    # non-independence is recorded on every finding it produces.
    "redteam": "adjudicator",
}

#: Provider identifiers accepted in the ``provider`` field.
KNOWN_PROVIDERS: tuple[str, ...] = (
    "ollama",
    "llamacpp",
    "openai",
    "openai-compatible",
    "anthropic",
    "claude",  # alias for anthropic
)

#: Default credential environment variable per provider. Resolved lazily so a
#: config that names an Anthropic model does not have to restate the obvious
#: variable, and so an OpenAI section never accidentally reads an Anthropic key.
DEFAULT_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}

CONFIG_FILENAME = "config.toml"


class ConfigError(ValueError):
    """Raised for a malformed or internally inconsistent configuration."""


def default_config_path() -> Path:
    """Platform-appropriate config location.

    ``%APPDATA%`` on Windows -- roaming, unlike the run ledger, because a
    model configuration is a user preference worth following them between
    machines whereas run records are machine-local evidence. ``XDG_CONFIG_HOME``
    on POSIX.
    """
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "abca" / CONFIG_FILENAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "abca" / CONFIG_FILENAME
    return Path.home() / ".config" / "abca" / CONFIG_FILENAME


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """How to construct the provider for one role."""

    role: str
    provider: str
    model: str

    base_url: str | None = None
    timeout: float = 300.0

    # Hosted-provider credentials. `api_key` is held here in memory only and
    # is excluded from every serialization path in this class.
    api_key: str | None = None
    #: ``None`` means "use the provider's conventional variable", resolved by
    #: :meth:`resolved_key_env`. An explicit value always wins.
    api_key_env: str | None = None
    organization: str | None = None
    vendor: str = "openai"

    # llama.cpp
    model_path: str | None = None
    hash_weights: bool = True

    # ollama
    keep_alive: str = "30m"

    def resolved_key_env(self) -> str:
        """The environment variable this spec reads its key from."""
        return self.api_key_env or DEFAULT_KEY_ENV.get(self.provider, "OPENAI_API_KEY")

    #: Backends that authenticate. Local runtimes are excluded so their rows do
    #: not display a scary "missing key" for a credential they never use.
    _CREDENTIALED = frozenset({"openai", "openai-compatible", "anthropic", "claude"})

    def credential_status(self) -> str:
        """Where the key comes from -- or that there is none. Never the key."""
        if self.provider not in self._CREDENTIALED:
            return "n/a (local)"
        if self.api_key:
            return "inline (config file)"
        env_name = self.resolved_key_env()
        return f"env:{env_name}" if os.environ.get(env_name) else f"missing (${env_name})"

    def redacted(self) -> dict[str, Any]:
        """Serializable view with the credential removed.

        Everything that displays or records a spec goes through this. There is
        deliberately no code path that emits ``api_key``.
        """
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "credential": self.credential_status(),
            "model_path": self.model_path,
        }

    def __repr__(self) -> str:
        return f"ModelSpec({self.redacted()})"


@dataclass(frozen=True, slots=True)
class Defaults:
    """Run-level defaults, overridable by CLI flags."""

    profile: Profile = Profile.STANDARD
    seed: int = 42
    temperature: float = 0.0
    max_claims: int = 200
    reading_level: int = 8
    max_attempts: int = 3


@dataclass(slots=True)
class Config:
    """A loaded configuration."""

    defaults: Defaults = field(default_factory=Defaults)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    #: The consensus panel, in config order. A LIST, unlike every other role,
    #: because the whole point of consensus is running several models and
    #: reporting where they disagree -- averaging them into one would destroy
    #: the finding the mode exists to produce.
    consensus: list[ModelSpec] = field(default_factory=list)
    source: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def spec(self, role: str) -> ModelSpec:
        """Return the spec for ``role``, following a fallback if one applies."""
        if role in self.models:
            return self.models[role]

        fallback = ROLE_FALLBACKS.get(role)
        if fallback and fallback in self.models:
            # Re-labelled so the ledger records which role the model actually
            # filled, rather than silently attributing segmenter work to the
            # classifier entry.
            return replace(self.models[fallback], role=role)

        try:
            return self.models[role]
        except KeyError:
            configured = ", ".join(sorted(self.models)) or "<none>"
            raise ConfigError(
                f"no model configured for role {role!r}. Configured roles: {configured}. "
                f"Add a [models.{role}] section to {self.source or default_config_path()}."
            ) from None

    @property
    def has_panel(self) -> bool:
        """Whether a consensus panel large enough to disagree is configured."""
        return len(self.consensus) >= 2

    def redacted(self) -> dict[str, Any]:
        return {
            "source": str(self.source) if self.source else None,
            "consensus": [spec.redacted() for spec in self.consensus],
            "defaults": {
                "profile": self.defaults.profile.value,
                "seed": self.defaults.seed,
                "temperature": self.defaults.temperature,
                "max_claims": self.defaults.max_claims,
                "reading_level": self.defaults.reading_level,
                "max_attempts": self.defaults.max_attempts,
            },
            "models": {role: spec.redacted() for role, spec in sorted(self.models.items())},
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _parse_model_section(role: str, raw: dict[str, Any]) -> ModelSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"[models.{role}] must be a table, got {type(raw).__name__}")

    provider = str(raw.get("provider") or "ollama").lower()
    if provider not in KNOWN_PROVIDERS:
        raise ConfigError(
            f"[models.{role}] provider={provider!r} is not recognised. "
            f"Expected one of: {', '.join(KNOWN_PROVIDERS)}."
        )

    model = raw.get("model")
    if provider != "llamacpp" and not model:
        raise ConfigError(f"[models.{role}] requires a `model` name for provider {provider!r}")

    return ModelSpec(
        role=role,
        provider=provider,
        model=str(model or Path(str(raw.get("model_path", "llamacpp"))).name),
        base_url=raw.get("base_url"),
        timeout=float(raw.get("timeout", 300.0)),
        api_key=raw.get("api_key"),
        api_key_env=(str(raw["api_key_env"]) if raw.get("api_key_env") else None),
        organization=raw.get("organization"),
        vendor=str(raw.get("vendor", "openai")),
        model_path=raw.get("model_path"),
        hash_weights=bool(raw.get("hash_weights", True)),
        keep_alive=str(raw.get("keep_alive", "30m")),
    )


def load_config(path: Path | str | None = None, *, required: bool = False) -> Config:
    """Load configuration from ``path`` (default: the platform location).

    A missing file yields a Config with built-in defaults and no models, so
    read-only commands still work on a fresh install. ``required=True`` makes
    the absence an error, which is what commands that must actually run a
    model use.
    """
    resolved = Path(path) if path else default_config_path()

    if not resolved.is_file():
        if required:
            raise ConfigError(
                f"no configuration at {resolved}. Create one with `abca config init`."
            )
        return Config(source=None)

    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{resolved} is not valid TOML: {exc}") from exc

    warnings: list[str] = []

    defaults_raw = raw.get("defaults") or {}
    try:
        defaults = Defaults(
            profile=Profile(str(defaults_raw.get("profile", "standard"))),
            seed=int(defaults_raw.get("seed", 42)),
            temperature=float(defaults_raw.get("temperature", 0.0)),
            max_claims=int(defaults_raw.get("max_claims", 200)),
            reading_level=int(defaults_raw.get("reading_level", 8)),
            max_attempts=int(defaults_raw.get("max_attempts", 3)),
        )
    except ValueError as exc:
        raise ConfigError(f"[defaults] in {resolved} is invalid: {exc}") from exc

    models: dict[str, ModelSpec] = {}
    consensus: list[ModelSpec] = []
    for role, section in (raw.get("models") or {}).items():
        if role not in KNOWN_ROLES:
            raise ConfigError(
                f"[models.{role}] is not a known role. Expected one of: "
                f"{', '.join(KNOWN_ROLES)}. (A typo here would silently leave the "
                "real role unconfigured, so it is rejected rather than ignored.)"
            )

        # `consensus` is the one role written as an ARRAY of tables --
        # [[models.consensus]] -- because it names a panel rather than a model.
        if role == "consensus":
            sections = section if isinstance(section, list) else [section]
            for index, entry in enumerate(sections, start=1):
                spec = _parse_model_section("consensus", entry)
                if spec.api_key:
                    warnings.append(
                        f"[[models.consensus]] #{index} has an inline api_key. The "
                        f"config file is now a secret -- do not commit {resolved}. "
                        "Prefer api_key_env."
                    )
                consensus.append(spec)
            continue

        spec = _parse_model_section(role, section)
        if spec.api_key:
            warnings.append(
                f"[models.{role}] has an inline api_key. The config file is now a "
                f"secret -- do not commit {resolved}. Prefer api_key_env."
            )
        models[role] = spec

    config = Config(
        defaults=defaults, models=models, consensus=consensus,
        source=resolved, warnings=warnings,
    )
    _validate_roles(config)
    return config


def _validate_roles(config: Config) -> None:
    """Cross-role checks that a single section cannot express.

    Checked here, at config load, so the error arrives when the operator edits
    the file -- not minutes into a run.
    """
    names = [spec.model for spec in config.consensus]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ConfigError(
            f"the consensus panel lists {', '.join(sorted(duplicates))} more than "
            "once. A panel of one model asked twice is not a panel: at temperature "
            "0.0 it returns the same verdict and reports unanimous agreement, which "
            "is the single most misleading output this mode could produce."
        )
    if len(config.consensus) == 1:
        raise ConfigError(
            "the consensus panel has one model in it. Consensus reports where "
            "models DISAGREE; a panel of one always agrees with itself. Configure "
            "at least two [[models.consensus]] entries, or remove the section."
        )
    adjudicator = config.models.get("adjudicator")
    backtranslate = config.models.get("backtranslate")
    if adjudicator and backtranslate and adjudicator.model == backtranslate.model:
        raise ConfigError(
            f"[models.backtranslate] uses the same model as [models.adjudicator] "
            f"({adjudicator.model!r}). The plain-language fidelity gate requires an "
            "independent model: one that grades its own simplification reproduces "
            "its own misreadings, and the gate silently passes everything "
            "(contract s5, docs/01)."
        )


STARTER_CONFIG = '''\
# abCA configuration
#
# Roles exist so cheap work runs cheap. `classifier` runs once per claim --
# thousands of times on a large thread -- so it should be a small model.
# `adjudicator` runs only on claims that survive gating, so it can be large.
#
# `backtranslate` MUST be a different model from `adjudicator`. A model that
# grades its own plain-language rewrite reproduces its own misreadings, and the
# fidelity gate silently becomes a no-op. This is enforced, not advised.

[defaults]
profile       = "standard"   # fast | standard | forensic
seed          = 42
temperature   = 0.0          # 0.0 for determinism; raise only to experiment
max_claims    = 200
reading_level = 8            # target Flesch-Kincaid grade for plain-language output
max_attempts  = 3            # structured-output repair attempts before failing

# ---------------------------------------------------------------- local models
# Local open-weight models are the default because their weights can be pinned
# by hash, which is what lets a run be marked reproducible.

[models.classifier]
provider = "ollama"
model    = "qwen2.5:7b-instruct"

[models.adjudicator]
provider = "ollama"
model    = "qwen2.5:32b-instruct"

# The red team attacks every verdict before it ships. It falls back to the
# adjudicator when unset, but an INDEPENDENT model finds more: a model
# reviewing its own reasoning is the one least able to see where it reached.
# Whichever you choose is recorded on every finding.
[models.redteam]
provider = "ollama"
model    = "llama3.1:8b-instruct"

[models.backtranslate]
provider = "ollama"
model    = "llama3.1:8b-instruct"

[models.embedder]
provider = "ollama"
model    = "nomic-embed-text"

# ------------------------------------------------------------------ consensus
# `abca analyze --consensus` runs every model below on the same claims and
# reports where they disagree, rather than averaging the disagreement away.
#
# An array of tables, not a single model, and at least two entries. Where the
# panel splits, the WEAKEST verdict is published and the split is reported as a
# number on the claim: models that read the same sources differently is a
# finding about how settled the claim is, and hiding it behind a majority vote
# would throw away the most useful thing the mode produces.
#
# [[models.consensus]]
# provider = "ollama"
# model    = "qwen2.5:32b-instruct"
#
# [[models.consensus]]
# provider = "ollama"
# model    = "llama3.1:70b-instruct"

# ------------------------------------------------------- remote Ollama (EC2)
# The SSH tunnel from docs/04-linux-ec2.md makes a remote model look local, so
# no config change is needed:
#     ssh -N -L 11434:127.0.0.1:11434 ubuntu@<instance-ip>
# Weight pinning still works over the tunnel -- the manifest digest comes back
# intact -- so an EC2-hosted run is just as verifiable as a local one.
#
# [models.adjudicator]
# provider = "ollama"
# model    = "orcarouter/Qwen3.8-27B-Uncensored"
# base_url = "http://127.0.0.1:11434"

# ------------------------------------------------------------- hosted models
# Any run using a hosted API is marked `reproducible: false`, because the
# vendor can change the weights behind a model name without notice and no
# third party can confirm which model produced a published verdict.
# Fine for exploration and for a second opinion in consensus mode. Not for
# anything published under the tool's name.
#
# Credentials, in resolution order:
#   1. api_key     = "sk-..."         inline. Convenient; the file is then a
#                                     secret. Do not commit it.
#   2. api_key_env = "OPENAI_API_KEY" read from the environment. Recommended.
#   3. neither                        valid for a local OpenAI-compatible
#                                     server (vLLM, LM Studio, Ollama /v1).
#
# [models.adjudicator]
# provider    = "openai"
# model       = "gpt-4o"
# api_key_env = "OPENAI_API_KEY"   # optional; this is the default for openai
#
# Claude uses the Anthropic Messages API. Its structured output is the
# strongest of any hosted backend here: abCA forces a tool whose input_schema
# is the target schema, so the model returns a conforming object directly
# rather than JSON-in-prose that has to be extracted.
#
# [models.adjudicator]
# provider    = "anthropic"          # or "claude"
# model       = "claude-sonnet-4-5"
# api_key_env = "ANTHROPIC_API_KEY"  # optional; this is the default for anthropic
#
# Anthropic has no embeddings endpoint, so pair it with a local embedder:
#
# [models.embedder]
# provider = "ollama"
# model    = "nomic-embed-text"
#
# Any OpenAI-compatible endpoint works via base_url -- Azure, OpenRouter,
# Together, a local vLLM:
#
# [models.consensus]
# provider    = "openai-compatible"
# model       = "meta-llama/Llama-3.3-70B-Instruct"
# base_url    = "https://openrouter.ai/api/v1"
# api_key_env = "OPENROUTER_API_KEY"
# vendor      = "openrouter"

# ------------------------------------------------------------------ llama.cpp
# Strongest pinning: the GGUF file itself is hashed, and GBNF grammars make
# invalid output unsamplable. Start the server first:
#     llama-server -m /models/qwen2.5-32b-Q5_K_M.gguf --port 8080
#
# [models.adjudicator]
# provider   = "llamacpp"
# model_path = "/models/qwen2.5-32b-Q5_K_M.gguf"
# base_url   = "http://127.0.0.1:8080"
'''


def write_starter_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write the commented starter config. Refuses to clobber by default."""
    target = path or default_config_path()
    if target.exists() and not overwrite:
        raise ConfigError(f"{target} already exists; pass --force to overwrite it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_CONFIG, encoding="utf-8")
    return target


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_KEY_ENV",
    "KNOWN_PROVIDERS",
    "KNOWN_ROLES",
    "ROLE_FALLBACKS",
    "STARTER_CONFIG",
    "Config",
    "ConfigError",
    "Defaults",
    "ModelSpec",
    "default_config_path",
    "load_config",
    "write_starter_config",
]
