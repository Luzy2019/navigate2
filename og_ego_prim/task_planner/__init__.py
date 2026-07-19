"""Task planning module."""

from importlib import import_module

from og_ego_prim.domain import Action, ActionDecision, PlannerEpisodeEntry
from og_ego_prim.utils.metric import track_planning_latency
from og_ego_prim.utils.planning import normalize_planner_action

from .adapters import (
    CallablePlannerAdapter,
    IteratorPlannerAdapter,
    PLANNER_ADAPTERS,
    PlannerAdapter,
    PlannerAdapterFactory,
    PlanningAgentAdapter,
    create_planner_adapter,
    register_planner_adapter,
)
from .episode import PlannerEpisode


# Keep the modular runtime importable without initializing simulator-only
# dependencies used by the model-backed planning agents.
_AGENT_EXPORTS = {
    "BadAgentPlanError",
    "ExamplePlanner",
    "PlanningAgent",
    "TaskPlanContext",
    "get_obs_from_dir",
    "parse_output",
}


def __getattr__(name):
    if name not in _AGENT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".agent", __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "Action",
    "ActionDecision",
    "BadAgentPlanError",
    "ExamplePlanner",
    "PlanningAgent",
    "PlannerAdapter",
    "PlannerAdapterFactory",
    "PlannerEpisode",
    "PlannerEpisodeEntry",
    "CallablePlannerAdapter",
    "IteratorPlannerAdapter",
    "PLANNER_ADAPTERS",
    "PlanningAgentAdapter",
    "TaskPlanContext",
    "create_planner_adapter",
    "get_obs_from_dir",
    "parse_output",
    "track_planning_latency",
    "normalize_planner_action",
    "register_planner_adapter",
]
