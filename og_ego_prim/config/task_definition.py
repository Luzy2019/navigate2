"""Versioned task views that separate agent context from evaluation oracles."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from og_ego_prim.utils.serialization import as_versioned_dict, to_builtin
from og_ego_prim.utils.task_registry import get_task_config_path
from .eval_config import EvalTaskConfig, build_eval_task_config
from .runtime_config import RuntimeTaskConfig, build_runtime_task_config

AGENT_TASK_SCHEMA_VERSION = "isbench.agent_task.v1"
TASK_DEFINITION_SCHEMA_VERSION = "isbench.task_definition.v2"


def _tuple_of_strings(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item) for item in value)


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _plain_text_goal(value: Any) -> str:
    """Normalize only the public, natural-language goal authored for the planner."""

    if value is None:
        return ""
    if isinstance(value, Mapping):
        # Legacy composite tasks placed evaluator predicates under ``G_task``.
        # They are intentionally ignored. A mapping may opt in only through an
        # explicit natural-language field while those tasks are migrated.
        value = value.get("text") or value.get("description")
        if value is None:
            return ""
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise TypeError("planning_context.goal_condition must be plain text or a list of text")
    lines = [str(item).strip() for item in values if str(item).strip()]
    if any(line.lower().startswith("(:goal") for line in lines):
        raise ValueError("planning_context.goal_condition must not contain a BDDL goal")
    return "\n".join(lines)


@dataclass(frozen=True)
class AgentSubtaskView:
    subtask_index: int
    task_name: str
    instruction: str
    horizon_limit: Optional[int] = None
    example_planning: Tuple[Dict[str, Any], ...] = ()
    schema_version: str = AGENT_TASK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class AgentTaskView:
    """Planner/object context that is allowed to reach agent prompts."""

    task_name: str
    task_type: str
    primitive_type: Optional[str]
    instruction: str
    goal_description: str
    initial_setup: Tuple[str, ...]
    object_ids: Tuple[str, ...]
    object_abilities: Dict[str, Tuple[str, ...]]
    wash_rules: Tuple[Any, ...]
    room_id: Optional[str]
    room_ids: Tuple[str, ...]
    subtasks: Tuple[AgentSubtaskView, ...]
    example_planning: Tuple[Dict[str, Any], ...]
    schema_version: str = AGENT_TASK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def subtask(self, subtask_index: int) -> Optional[AgentSubtaskView]:
        index = int(subtask_index)
        return next((item for item in self.subtasks if item.subtask_index == index), None)

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class TaskDefinition:
    agent: AgentTaskView
    runtime: RuntimeTaskConfig
    evaluation: EvalTaskConfig
    source_path: Optional[str] = None
    schema_version: str = TASK_DEFINITION_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin(
            {
                "agent": self.agent.to_dict(),
                "runtime": self.runtime.to_dict(),
                "evaluation": self.evaluation.to_dict(),
                "source_path": self.source_path,
                "schema_version": self.schema_version,
                "extensions": deepcopy(self.extensions),
            }
        )


def _build_subtasks(config: Mapping[str, Any]) -> Tuple[AgentSubtaskView, ...]:
    result = []
    for ordinal, value in enumerate(config.get("subtasks") or (), start=1):
        if not isinstance(value, Mapping):
            continue
        index = int(value.get("subtask_index", ordinal))
        task_name = str(value.get("task_name") or value.get("source_task") or f"subtask_{index}")
        instruction = str(
            value.get("L")
            or value.get(f"L_{index}")
            or value.get("instruction")
            or ""
        )
        example_planning = tuple(
            deepcopy(item)
            for item in value.get("example_planning") or ()
            if isinstance(item, Mapping)
        )
        extensions = {}
        if value.get("source_task") is not None:
            extensions["source_task"] = str(value["source_task"])
        result.append(
            AgentSubtaskView(
                subtask_index=index,
                task_name=task_name,
                instruction=instruction,
                horizon_limit=None if value.get("H_limit") is None else int(value.get("H_limit")),
                example_planning=example_planning,
                extensions=extensions,
            )
        )
    return tuple(result)


def build_agent_task_view(config: Mapping[str, Any]) -> AgentTaskView:
    task_info = _mapping(config.get("task_info"))
    scene_info = _mapping(config.get("scene_info"))
    planning = _mapping(config.get("planning_context"))
    abilities = {
        str(entity_id): _tuple_of_strings(values)
        for entity_id, values in _mapping(planning.get("object_abilities")).items()
    }
    task_name = str(task_info.get("task_name") or config.get("task_name") or "")
    if not task_name:
        raise ValueError("task definition is missing task_info.task_name")
    source_version = config.get("_version")
    extensions = {"source_version": source_version} if source_version is not None else {}
    return AgentTaskView(
        task_name=task_name,
        task_type=str(task_info.get("task_type") or "BehaviorTask"),
        primitive_type=(
            None if task_info.get("primitive_type") is None else str(task_info.get("primitive_type"))
        ),
        instruction=str(planning.get("task_instruction") or config.get("task_instruction") or ""),
        goal_description=_plain_text_goal(planning.get("goal_condition")),
        initial_setup=_tuple_of_strings(planning.get("initial_setup")),
        object_ids=_tuple_of_strings(planning.get("object_list")),
        object_abilities=abilities,
        wash_rules=tuple(
            deepcopy(item)
            for item in planning.get("wash_rules") or ()
            if isinstance(item, (str, Mapping))
        ),
        room_id=None if scene_info.get("room") is None else str(scene_info.get("room")),
        room_ids=_tuple_of_strings(scene_info.get("rooms")),
        subtasks=_build_subtasks(config),
        example_planning=tuple(
            deepcopy(item)
            for item in config.get("example_planning") or ()
            if isinstance(item, Mapping)
        ),
        extensions=extensions,
    )


TaskDefinitionSource = Union[str, Path, Mapping[str, Any]]

# 从 source(任务定义的路径或字典)加载任务定义，并返回 TaskDefinition 对象
class TaskDefinitionLoader:
    def load(self, source: TaskDefinitionSource) -> TaskDefinition:
        source_path: Optional[Path] = None
        if isinstance(source, Mapping):
            config = deepcopy(dict(source))
        else:
            raw_path = Path(source)
            if raw_path.exists() and raw_path.is_file():
                source_path = raw_path.resolve()
            else:
                source_path = get_task_config_path(str(source))
            with source_path.open("r", encoding="utf-8") as file:
                config = json.load(file)
        if not isinstance(config, Mapping):
            raise TypeError("task JSON root must be a mapping")
        source_version = config.get("schema_version", config.get("_version"))
        extensions = (
            {"source_version": str(source_version)}
            if source_version is not None
            else {}
        )
        return TaskDefinition(
            agent=build_agent_task_view(config),
            runtime=build_runtime_task_config(config),
            evaluation=build_eval_task_config(config),
            source_path=None if source_path is None else str(source_path),
            extensions=extensions,
        )


def load_task_definition(source: TaskDefinitionSource) -> TaskDefinition:
    return TaskDefinitionLoader().load(source)


def load_task_views(
    source: TaskDefinitionSource,
) -> Tuple[AgentTaskView, RuntimeTaskConfig, EvalTaskConfig]:
    definition = load_task_definition(source)
    return definition.agent, definition.runtime, definition.evaluation


def inject_execution_goal_into_bddl_problem(
    problem_text: str,
    execution_goal: str,
) -> str:
    """Project the task-JSON execution goal into an in-memory BDDL problem."""

    from bddl.parsing import scan_tokens

    problem_tokens = scan_tokens(string=str(problem_text))
    goal_tokens = scan_tokens(string=str(execution_goal))
    if not isinstance(problem_tokens, list) or not problem_tokens:
        raise ValueError("BDDL problem must be one non-empty S-expression")
    if problem_tokens[0] != "define":
        raise ValueError("BDDL problem must start with define")
    if (
        not isinstance(goal_tokens, list)
        or len(goal_tokens) != 2
        or goal_tokens[0] != ":goal"
        or not goal_tokens[1]
    ):
        raise ValueError("execution_goal_condition must be a non-empty BDDL :goal")

    replaced = False
    for index, group in enumerate(problem_tokens[1:], start=1):
        if isinstance(group, list) and group and group[0] == ":goal":
            problem_tokens[index] = goal_tokens
            replaced = True
            break
    if not replaced:
        problem_tokens.append(goal_tokens)

    def render(value: Any) -> str:
        if isinstance(value, list):
            return "(" + " ".join(render(item) for item in value) + ")"
        return str(value)

    return render(problem_tokens)


__all__ = [
    "AGENT_TASK_SCHEMA_VERSION",
    "AgentSubtaskView",
    "AgentTaskView",
    "EvalTaskConfig",
    "RuntimeTaskConfig",
    "TASK_DEFINITION_SCHEMA_VERSION",
    "TaskDefinition",
    "TaskDefinitionLoader",
    "TaskDefinitionSource",
    "build_agent_task_view",
    "build_eval_task_config",
    "build_runtime_task_config",
    "load_task_definition",
    "load_task_views",
    "inject_execution_goal_into_bddl_problem",
]
