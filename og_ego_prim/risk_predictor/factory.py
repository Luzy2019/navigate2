"""Construction helpers for runtime risk providers."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .predictor import RiskPredictor
from .providers import (
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


'''
    task_json
    rules
    deterministic
    none
    disabled
'''
def create_risk_provider(
    name: str = "task_json",
    *,
    task: Any = None,
) -> RiskProvider:
    """Create one of the fixed runtime risk providers."""
    return DEFAULT_PROVIDER_REGISTRY.create(name, task=task)


def create_risk_predictor(
    config: Optional[Mapping[str, Any]] = None,
    *,
    task: Any = None,
) -> RiskPredictor:
    """Build the configured predictor used by the online runtime."""
    options = dict(config or {})
    enabled = bool(options.get("enabled", True))
    provider_name = str(options.get("provider", "task_json")) if enabled else "disabled"
    provider = create_risk_provider(provider_name, task=task)
    return RiskPredictor(provider)


__all__ = [
    "DEFAULT_PROVIDER_REGISTRY",
    "ProviderFactory",
    "RiskProviderRegistry",
    "create_risk_predictor",
    "create_risk_provider",
]
