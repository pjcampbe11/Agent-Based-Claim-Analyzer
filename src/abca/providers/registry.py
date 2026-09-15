"""Building providers from configuration.

This is the only place that maps a ``provider = "..."`` string to a class. The
pipeline never constructs a provider directly, so every backend gets the same
health checks, the same cross-role validation, and the same honest accounting
of what can and cannot be pinned.

THE THREE-PLACE RULE
====================
``backtranslate`` must differ from ``adjudicator``. That is checked here, in
:func:`abca.config.load_config`, and in
:class:`~abca.schema.ledger.RunConfig`. Three places for one rule looks like
duplication and is not: each catches it at a different moment -- config edit,
registry construction, and record validation -- and the failure it prevents
(a fidelity gate that silently passes everything) produces no error and no
visible symptom of its own. A rule with no natural symptom needs redundant
enforcement.

REPRODUCIBILITY IS COMPUTED, NEVER DECLARED
===========================================
:meth:`ProviderRegistry.to_model_identities` derives every identity from the
live backend. A run is reproducible if and only if every model in it returned
a real weights hash. No config flag can assert it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from abca.config import Config, ConfigError, ModelSpec
from abca.providers.base import Provider, ProviderError
from abca.providers.transport import Transport


@dataclass(frozen=True, slots=True)
class RoleProvider:
    """One constructed provider bound to its role and originating spec."""

    role: str
    provider: Provider
    spec: ModelSpec


def build_provider(
    spec: ModelSpec,
    *,
    transport: Transport | None = None,
) -> Provider:
    """Construct the provider described by ``spec``.

    ``transport`` is injectable so the whole registry can be exercised against
    :class:`~abca.providers.transport.FakeTransport` with no daemon, no
    network, no GPU and no API key.
    """
    # Imported lazily so that a missing optional backend never breaks import of
    # this module, and so provider modules stay independently importable.
    if spec.provider == "ollama":
        from abca.providers.ollama import DEFAULT_BASE_URL, OllamaProvider

        return OllamaProvider(
            spec.model,
            role=spec.role,
            base_url=spec.base_url or DEFAULT_BASE_URL,
            transport=transport,
            timeout=spec.timeout,
            keep_alive=spec.keep_alive,
        )

    if spec.provider == "llamacpp":
        from abca.providers.llamacpp import DEFAULT_BASE_URL, LlamaCppProvider

        return LlamaCppProvider(
            spec.model_path,
            role=spec.role,
            base_url=spec.base_url or DEFAULT_BASE_URL,
            transport=transport,
            timeout=spec.timeout,
            model_name=spec.model,
            hash_weights=spec.hash_weights,
        )

    if spec.provider in {"anthropic", "claude"}:
        from abca.providers.anthropic import DEFAULT_BASE_URL, AnthropicProvider

        return AnthropicProvider(
            spec.model,
            role=spec.role,
            api_key=spec.api_key,
            api_key_env=spec.resolved_key_env(),
            base_url=spec.base_url or DEFAULT_BASE_URL,
            transport=transport,
            timeout=spec.timeout,
        )

    if spec.provider in {"openai", "openai-compatible"}:
        from abca.providers.openai_compat import (
            DEFAULT_BASE_URL,
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(
            spec.model,
            role=spec.role,
            api_key=spec.api_key,
            api_key_env=spec.resolved_key_env(),
            base_url=spec.base_url or DEFAULT_BASE_URL,
            vendor=spec.vendor,
            organization=spec.organization,
            transport=transport,
            timeout=spec.timeout,
        )

    raise ConfigError(f"unknown provider {spec.provider!r} for role {spec.role!r}")


class ProviderRegistry:
    """Role-to-provider map for one run."""

    def __init__(
        self,
        providers: dict[str, RoleProvider],
        panel: list[RoleProvider] | None = None,
    ) -> None:
        self._providers = providers
        #: The consensus panel, in config order. A LIST, not another dict entry,
        #: because several models share the role -- and because the order is
        #: what makes a run reproducible: the same panel asked in the same order
        #: produces the same merged verdict.
        self._panel = list(panel or [])

    @property
    def panel(self) -> list[RoleProvider]:
        return list(self._panel)

    def panel_providers(self) -> list[Provider]:
        return [entry.provider for entry in self._panel]

    @property
    def has_panel(self) -> bool:
        """Whether a panel large enough to disagree was built."""
        return len(self._panel) >= 2

    def __contains__(self, role: object) -> bool:
        return role in self._providers

    def roles(self) -> list[str]:
        return sorted(self._providers)

    def get(self, role: str) -> Provider:
        try:
            return self._providers[role].provider
        except KeyError:
            raise ConfigError(
                f"no provider built for role {role!r}; configured roles: "
                f"{', '.join(self.roles()) or '<none>'}"
            ) from None

    def spec(self, role: str) -> ModelSpec:
        return self._providers[role].spec

    # ------------------------------------------------------------------ checks

    def health_check(self) -> dict[str, str]:
        """Probe every backend. Returns ``role -> "ok"`` or the failure text.

        Never raises. A partial failure is reported per role so the operator
        sees everything wrong at once -- fixing one missing model only to
        discover the next one is a bad experience, and the whole point of
        checking up front is to front-load the bad news.
        """
        results: dict[str, str] = {}
        for role, entry in sorted(self._providers.items()):
            try:
                entry.provider.health()
                results[role] = "ok"
            except ProviderError as exc:
                results[role] = str(exc)
        return results

    def to_model_identities(self) -> list[Any]:
        """Resolve every role's identity from its live backend.

        These go straight into :class:`~abca.schema.ledger.RunConfig`, and
        their ``weights_hash`` values decide whether the run is reproducible.
        Derived here, never taken from config.
        """
        singles = [entry.provider.identity() for _, entry in sorted(self._providers.items())]
        # Panel identities carry role "consensus" -- several of them, which
        # RunConfig permits for this role alone. They are appended in PANEL
        # ORDER rather than sorted, because that order is part of the recipe.
        return singles + [entry.provider.identity() for entry in self._panel]

    @property
    def is_fully_pinnable(self) -> bool:
        """True when every configured model can be verified by a third party."""
        return all(identity.is_pinnable for identity in self.to_model_identities())

    def unpinnable_models(self) -> list[str]:
        """Names of models with no weights hash. Drives the operator warning."""
        return [
            identity.name
            for identity in self.to_model_identities()
            if not identity.is_pinnable
        ]


def build_registry(
    config: Config,
    *,
    roles: list[str] | None = None,
    transports: dict[str, Transport] | None = None,
    consensus: bool = False,
) -> ProviderRegistry:
    """Construct providers for ``roles`` (default: everything configured).

    Parameters
    ----------
    roles
        Restrict construction to these roles. The ``fast`` profile does not
        need ``backtranslate``, and building an unused provider would mean a
        pointless health check and, for a hosted backend, a pointless billable
        request.
    transports
        Per-role transport injection, for tests.
    """
    wanted = roles if roles is not None else sorted(config.models)
    built: dict[str, RoleProvider] = {}

    for role in wanted:
        spec = config.spec(role)
        transport = (transports or {}).get(role)
        built[role] = RoleProvider(
            role=role, provider=build_provider(spec, transport=transport), spec=spec
        )

    panel: list[RoleProvider] = []
    if consensus:
        if not config.has_panel:
            raise ConfigError(
                "--consensus needs at least two [[models.consensus]] entries. "
                "Consensus reports where models DISAGREE, and a panel of one always "
                "agrees with itself -- running it would report unanimity that means "
                "nothing."
            )
        panel = [
            RoleProvider(
                role="consensus", provider=build_provider(spec), spec=spec
            )
            for spec in config.consensus
        ]
        _validate_panel_distinct(panel)

    _validate_independence(built)
    return ProviderRegistry(built, panel)


def _validate_panel_distinct(panel: list[RoleProvider]) -> None:
    """Refuse a consensus panel whose members are the same model twice.

    Checked by RESOLVED WEIGHTS HASH, not by config string, for the same reason
    the fidelity gate's independence check is: two sections can point at one
    model under different tags, and a string comparison would miss it.

    A duplicated panel member is the most misleading configuration this mode
    admits. At temperature 0.0 the same weights return the same verdict, so the
    run reports unanimous agreement -- a strong-looking signal produced by
    asking one model twice.
    """
    seen: dict[str, str] = {}
    for entry in panel:
        try:
            identity = entry.provider.identity()
        except ProviderError:
            # Unreachable backend; health_check() reports that properly. Do not
            # turn a connectivity problem into a confusing config error.
            continue
        key = identity.weights_hash or f"{identity.provider}:{identity.name}"
        if key in seen:
            raise ConfigError(
                f"consensus panel members {seen[key]!r} and {identity.name!r} resolve "
                "to the same model. At temperature 0.0 they return the same verdict, "
                "so the run would report unanimous agreement produced by asking one "
                "model twice -- the most misleading output this mode can produce."
            )
        seen[key] = identity.name


def _validate_independence(built: dict[str, RoleProvider]) -> None:
    """Enforce that the fidelity gate has an independent grader.

    Compares the RESOLVED model identities rather than the config strings.
    That matters: two config sections can name the same model differently
    (``qwen2.5:32b`` and ``qwen2.5:32b-instruct`` pointing at one tag, or two
    llama.cpp sections loading the same GGUF by different paths) and a
    string comparison would miss it. The weights hash cannot be fooled that
    way, which is precisely why identity is derived from the backend.
    """
    adjudicator = built.get("adjudicator")
    backtranslate = built.get("backtranslate")
    if adjudicator is None or backtranslate is None:
        return

    try:
        left = adjudicator.provider.identity()
        right = backtranslate.provider.identity()
    except ProviderError:
        # The backend is unreachable; health_check() reports that properly.
        # Do not turn a connectivity problem into a confusing config error.
        return

    same_weights = (
        left.weights_hash is not None and left.weights_hash == right.weights_hash
    )
    if same_weights or (left.provider == right.provider and left.name == right.name):
        raise ConfigError(
            f"the backtranslate role resolves to the same model as adjudicator "
            f"({left.name!r}"
            + (f", weights {left.weights_hash[:19]}..." if left.weights_hash else "")
            + "). The plain-language fidelity gate requires an independent model: "
            "one that grades its own simplification reproduces its own misreadings, "
            "so the gate would silently pass everything (contract s5, docs/01)."
        )


__all__ = [
    "ProviderRegistry",
    "RoleProvider",
    "build_provider",
    "build_registry",
]
