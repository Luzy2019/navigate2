"""Replaceable planner adapters over typed actions."""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from og_ego_prim.domain import Action, Registry
from og_ego_prim.prompting import PromptContext
from og_ego_prim.utils.planning import normalize_planner_action


@runtime_checkable
class PlannerAdapter(Protocol):
    supports_rethinking: bool

    def propose(self, context: PromptContext) -> Optional[Action]:
        ...




class CallablePlannerAdapter:
    def __init__(
        self,
        callback: Callable[[PromptContext], Any],
        *,
        supports_rethinking: bool = True,
    ) -> None:
        self.callback = callback
        self.supports_rethinking = bool(supports_rethinking)

    def propose(self, context: PromptContext) -> Optional[Action]:
        return normalize_planner_action(self.callback(context))

class IteratorPlannerAdapter:
    """Adapter for the existing Expert Planning generator."""
    supports_rethinking = False

    def __init__(self, plans: Iterable[Any]) -> None:
        self._plans: Iterator[Any] = iter(plans)

    def propose(self, context: PromptContext) -> Optional[Action]:
        del context
        try:
            return normalize_planner_action(next(self._plans))
        except StopIteration:
            return None

class AgentPlannerAdapter:
    """Adapter for the existing GPT/local AgentPlanner generator."""

    supports_rethinking = True

    def __init__(self, agent: Any, *, use_obs: bool = True, max_step: Optional[int] = None) -> None:
        self.agent = agent
        self.use_obs = bool(use_obs)
        self.max_step = max_step
        self._iterator: Optional[Iterator[Any]] = None

    def propose(self, context: PromptContext) -> Optional[Action]:
        del context
        if self._iterator is None:
            self._iterator = iter(self.agent.step(use_obs=self.use_obs, max_step=self.max_step))
        try:
            return normalize_planner_action(next(self._iterator))
        except StopIteration:
            return None


PlannerAdapterFactory = Callable[..., PlannerAdapter]
PLANNER_ADAPTERS: Registry[PlannerAdapterFactory] = Registry()
PLANNER_ADAPTERS.register("callable", CallablePlannerAdapter)

PLANNER_ADAPTERS.register("example", IteratorPlannerAdapter)
PLANNER_ADAPTERS.register("iterator", IteratorPlannerAdapter)
PLANNER_ADAPTERS.register("scripted", IteratorPlannerAdapter)

PLANNER_ADAPTERS.register("agent_planner", AgentPlannerAdapter)
PLANNER_ADAPTERS.register("model", AgentPlannerAdapter)

def register_planner_adapter(
    name: str,
    factory: PlannerAdapterFactory,
    *,
    replace: bool = False,
) -> PlannerAdapterFactory:
    if not callable(factory):
        raise TypeError("planner adapter factory must be callable")
    return PLANNER_ADAPTERS.register(name, factory, replace=replace)


def create_planner_adapter(
    config: Any,
    *args: Any,
    registry: Registry[PlannerAdapterFactory] = PLANNER_ADAPTERS,
    **overrides: Any,
) -> PlannerAdapter:
    """Construct a planner adapter from a registered name or config mapping."""

    if isinstance(config, PlannerAdapter):
        if args or overrides:
            raise ValueError("cannot apply constructor arguments to an existing planner adapter")
        return config
    options: Dict[str, Any]
    if isinstance(config, str):
        name = config
        options = {}
    elif isinstance(config, Mapping):
        values = dict(config)
        name = values.pop("adapter", values.pop("type", values.pop("name", "iterator")))
        options = dict(values.pop("options", {}) or {})
        options.update(values)
    else:
        raise TypeError("planner adapter config must be a registered name, mapping, or adapter")
    options.update(overrides)
    adapter = registry.require(str(name))(*args, **options)
    if not isinstance(adapter, PlannerAdapter):
        raise TypeError("planner adapter must implement propose(context) and supports_rethinking")
    return adapter


__all__ = [
    "CallablePlannerAdapter",
    "IteratorPlannerAdapter",
    "PLANNER_ADAPTERS",
    "AgentPlannerAdapter",
    "PlannerAdapter",
    "PlannerAdapterFactory",
    "create_planner_adapter",
    "normalize_planner_action",
    "register_planner_adapter",
]
