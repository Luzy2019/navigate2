from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from og_ego_prim.utils.serialization import as_versioned_dict


EVAL_TASK_CONFIG_SCHEMA_VERSION = "isbench.eval_task_config.v1"


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _task_name(config: Mapping[str, Any]) -> str:
    task_info = _mapping(config.get("task_info"))
    return str(task_info.get("task_name") or config.get("task_name") or "")


def _mapping_items(value: Any) -> Tuple[Dict[str, Any], ...]:
    """Copy safety entries without normalizing or rewriting their fields."""

    if value is None:
        return ()
    values = (value,) if isinstance(value, Mapping) else value
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        copy.deepcopy(dict(item))
        for item in values
        if isinstance(item, Mapping)
    )


@dataclass(frozen=True)
class EvalSubtaskConfig:
    """Evaluator-only projection of one lifelong subtask.

    ``g_task`` and ``g_safe`` retain the source JSON values verbatim (usually
    BDDL strings or predicate lists).  No planner-facing fields are included.
    """

    subtask_index: int
    task_name: str
    g_task: Any = None
    g_safe: Any = None
    horizon_limit: Optional[int] = None
    schema_version: str = EVAL_TASK_CONFIG_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class EvalTaskConfig:
    """Read-only evaluator view projected from one task JSON document.

    Runtime/planner context is intentionally absent.  In particular, this
    object never derives goals from ``planning_context.goal_condition``.
    ``evaluation_goal_conditions`` and safety entries are deep-copied so a
    runner cannot mutate the task author's source mapping through the view.
    """

    task_name: str
    primitive_type: str = "ego"
    evaluation_cautions: Tuple[Dict[str, Any], ...] = ()
    evaluation_goal_conditions: Dict[str, Any] = field(default_factory=dict)
    lifelong_config: Dict[str, Any] = field(default_factory=dict)
    subtasks: Tuple[EvalSubtaskConfig, ...] = ()
    schema_version: str = EVAL_TASK_CONFIG_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def subtask(self, subtask_index: int) -> Optional[EvalSubtaskConfig]:
        index = int(subtask_index)
        return next((item for item in self.subtasks if item.subtask_index == index), None)

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


def build_eval_task_config(config: Mapping[str, Any]) -> EvalTaskConfig:
    """Project evaluator-owned fields from a task JSON mapping.

    The source mapping remains the only authoring source.  This function only
    copies fields and does not compile BDDL or infer any missing goal.
    """

    task_name = _task_name(config)
    if not task_name:
        raise ValueError("task definition is missing task_info.task_name")

    subtasks = []
    for ordinal, value in enumerate(config.get("subtasks") or (), start=1):
        if not isinstance(value, Mapping):
            continue
        index = int(value.get("subtask_index", ordinal))
        task_name_value = str(
            value.get("task_name")
            or value.get("source_task")
            or f"subtask_{index}"
        )
        task_goal = copy.deepcopy(value.get("G_task", value.get(f"G_task_{index}")))
        safe_goal = copy.deepcopy(value.get("G_safe", value.get(f"G_safe_{index}")))
        extensions = {}
        if value.get("source_task") is not None:
            extensions["source_task"] = str(value["source_task"])
        subtasks.append(
            EvalSubtaskConfig(
                subtask_index=index,
                task_name=task_name_value,
                g_task=task_goal,
                g_safe=safe_goal,
                horizon_limit=(
                    None
                    if value.get("H_limit") is None
                    else int(value["H_limit"])
                ),
                extensions=extensions,
            )
        )

    source_version = config.get("_version", config.get("schema_version"))
    task_info = _mapping(config.get("task_info"))
    extensions = {"source_version": source_version} if source_version is not None else {}
    return EvalTaskConfig(
        task_name=task_name,
        primitive_type=str(task_info.get("primitive_type") or "ego"),
        evaluation_cautions=_mapping_items(config.get("evaluation_cautions")),
        evaluation_goal_conditions=copy.deepcopy(
            _mapping(config.get("evaluation_goal_conditions"))
        ),
        lifelong_config=copy.deepcopy(_mapping(config.get("lifelong_config"))),
        subtasks=tuple(subtasks),
        extensions=extensions,
    )


__all__ = [
    "EVAL_TASK_CONFIG_SCHEMA_VERSION",
    "EvalSubtaskConfig",
    "EvalTaskConfig",
    "build_eval_task_config",
]
