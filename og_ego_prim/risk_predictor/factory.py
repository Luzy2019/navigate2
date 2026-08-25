"""Construction helpers for runtime risk providers."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .predictor import RiskPredictor
from .providers import (
    HybridRiskProvider,
    ModelRiskProvider,
    NullRiskProvider,
    RiskProvider,
    RuleRiskProvider,
)
from .rules import build_task_safety_rules


ProviderFactory = Callable[..., RiskProvider]


class RiskProviderRegistry:
    def __init__(self) -> None:
        self._factories: Dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        self._factories[name] = factory

    def create(self, name: str, **kwargs: Any) -> RiskProvider:
        key = str(name).strip().lower()
        if key not in self._factories:
            raise KeyError(f"unknown risk provider {name!r}; available: {', '.join(self.names())}")
        return self._factories[key](**kwargs)

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._factories))


DEFAULT_PROVIDER_REGISTRY = RiskProviderRegistry()
DEFAULT_PROVIDER_REGISTRY.register(
    "task_json",
    lambda task=None, **_: RuleRiskProvider(
        build_task_safety_rules(task)
    ),
)
DEFAULT_PROVIDER_REGISTRY.register(
    "rules",
    lambda task=None, **kwargs: DEFAULT_PROVIDER_REGISTRY.create(
        "task_json", task=task, **kwargs
    ),
)
DEFAULT_PROVIDER_REGISTRY.register(
    "deterministic",
    lambda task=None, **kwargs: DEFAULT_PROVIDER_REGISTRY.create(
        "task_json", task=task, **kwargs
    ),
)
DEFAULT_PROVIDER_REGISTRY.register("none", lambda **_: NullRiskProvider())
DEFAULT_PROVIDER_REGISTRY.register("disabled", lambda **_: NullRiskProvider())


def _vlm_provider_factory(
    task=None,
    client=None,
    held_object_getter=None,
    **_: Any,
) -> RiskProvider:
    """Build a VLM-only risk provider from a model client (a RiskAssessor).

    The benchmark is constructed before the planner agent exists, so ``client``
    may be missing here; return a no-op provider in that case and let
    ``install_vlm_risk_provider`` replace it with the real VLM assessor once
    the agent's client is available.
    """
    if client is None:
        return NullRiskProvider()
    from .risk_assessor import RiskAssessor

    return ModelRiskProvider(
        RiskAssessor(
            client,
            held_object_getter=held_object_getter,
        ),
        provider_id="vlm",
    )


DEFAULT_PROVIDER_REGISTRY.register("vlm", _vlm_provider_factory)
DEFAULT_PROVIDER_REGISTRY.register("model", _vlm_provider_factory)
DEFAULT_PROVIDER_REGISTRY.register(
    "hybrid",
    lambda task=None, client=None, held_object_getter=None, **_: HybridRiskProvider(
        tuple(
            provider
            for provider in (
                (
                    RuleRiskProvider(build_task_safety_rules(task))
                    if task is not None
                    else None
                ),
                _vlm_provider_factory(
                    task=task,
                    client=client,
                    held_object_getter=held_object_getter,
                ),
            )
            if provider is not None
        )
    ),
)


'''
    task_json
    rules
    deterministic
    vlm
    model
    hybrid
    none
    disabled
'''
def create_risk_provider(
    name: str = "vlm",
    *,
    task: Any = None,
    client: Any = None,
    held_object_getter: Any = None,
) -> RiskProvider:
    """Create one of the runtime risk providers."""
    return DEFAULT_PROVIDER_REGISTRY.create(
        name,
        task=task,
        client=client,
        held_object_getter=held_object_getter,
    )


def create_risk_predictor(
    config: Optional[Mapping[str, Any]] = None,
    *,
    task: Any = None,
    client: Any = None,
    held_object_getter: Any = None,
) -> RiskPredictor:
    """Build the configured predictor used by the online runtime."""
    options = dict(config or {})
    enabled = bool(options.get("enabled", True))
    provider_name = str(options.get("provider", "vlm")) if enabled else "disabled"
    provider = create_risk_provider(
        provider_name,
        task=task,
        client=client,
        held_object_getter=held_object_getter,
    )
    return RiskPredictor(provider)


__all__ = [
    "DEFAULT_PROVIDER_REGISTRY",
    "ProviderFactory",
    "RiskProviderRegistry",
    "create_risk_predictor",
    "create_risk_provider",
]
