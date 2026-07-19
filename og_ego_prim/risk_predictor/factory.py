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
    ensure_risk_provider,
)
from .rules import build_task_safety_rules


ProviderFactory = Callable[..., RiskProvider]


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"", "0", "false", "no", "off", "disabled", "none"}


class RiskProviderRegistry:
    def __init__(self) -> None:
        self._factories: Dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
        key = str(name).strip().lower()
        if not key:
            raise ValueError("risk provider name must not be empty")
        if not callable(factory):
            raise TypeError("risk provider factory must be callable")
        if key in self._factories and not replace:
            raise ValueError(f"risk provider {key!r} is already registered")
        self._factories[key] = factory

    def create(self, name: str, **kwargs: Any) -> RiskProvider:
        key = str(name).strip().lower()
        if key not in self._factories:
            raise KeyError(f"unknown risk provider {name!r}; available: {', '.join(self.names())}")
        return ensure_risk_provider(self._factories[key](**kwargs))

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._factories))


DEFAULT_PROVIDER_REGISTRY = RiskProviderRegistry()
DEFAULT_PROVIDER_REGISTRY.register(
    "task_json",
    lambda task=None, **_: RuleRiskProvider(
        build_task_safety_rules(task) if task is not None else ()
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


def create_risk_provider(
    config: Any = None,
    *,
    task: Any = None,
    assessor: Optional[Callable[..., Any]] = None,
    registry: Optional[RiskProviderRegistry] = None,
) -> RiskProvider:
    """Create a provider without consulting any repository-global catalog."""
    registry = registry or DEFAULT_PROVIDER_REGISTRY
    if isinstance(config, RiskProvider) or callable(getattr(config, "assess", None)):
        return config
    if callable(config) and not isinstance(config, Mapping):
        return ModelRiskProvider(config)
    if config is None:
        return RuleRiskProvider.from_task(task) if task is not None else NullRiskProvider()
    if not isinstance(config, (str, Mapping)):
        if hasattr(config, "to_dict"):
            config = config.to_dict()
        elif hasattr(config, "__dict__"):
            config = dict(config.__dict__)
        else:
            raise TypeError("risk provider config must be a name, mapping, provider, callable, or None")
    if isinstance(config, str):
        config = {"provider": config}
    options = dict(config)
    if not _enabled(options.pop("enabled", True)):
        return NullRiskProvider()
    if "rules" in options:
        raise ValueError(
            "risk.rules is not supported; author runtime safety context in the task JSON"
        )
    provider_name = str(options.pop("provider", options.pop("type", "task_json"))).strip().lower()
    if provider_name == "model":
        model_assessor = options.pop("assessor", assessor)
        if model_assessor is None:
            raise ValueError("model risk provider requires an assessor callback")
        return ModelRiskProvider(
            model_assessor,
            provider_id=str(options.pop("provider_id", "model")),
        )
    if provider_name == "hybrid":
        nested = options.pop("providers", ())
        return HybridRiskProvider(
            create_risk_provider(
                item,
                task=task,
                assessor=assessor,
                registry=registry,
            )
            for item in nested
        )
    return registry.create(provider_name, task=task, **options)


def create_risk_predictor(
    config: Any = None,
    *,
    task: Any = None,
    assessor: Optional[Callable[..., Any]] = None,
    registry: Optional[RiskProviderRegistry] = None,
) -> RiskPredictor:
    """Build a runtime predictor from task JSON and optional provider config."""
    if config is None:
        options: Dict[str, Any] = {}
    elif isinstance(config, Mapping):
        options = dict(config)
    elif hasattr(config, "to_dict"):
        options = dict(config.to_dict())
    elif hasattr(config, "__dict__"):
        options = dict(config.__dict__)
    else:
        options = {"provider": config}
    enabled = _enabled(options.get("enabled", True))
    mode = "disabled" if not enabled else str(options.pop("mode", "enforce"))
    provider = create_risk_provider(
        options,
        task=task,
        assessor=assessor,
        registry=registry,
    )
    return RiskPredictor(provider, mode=mode)


__all__ = [
    "DEFAULT_PROVIDER_REGISTRY",
    "ProviderFactory",
    "RiskProviderRegistry",
    "create_risk_predictor",
    "create_risk_provider",
]
