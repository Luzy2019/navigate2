"""Task configuration adapters for planning."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from og_ego_prim.config.task_definition import build_agent_task_view
from og_ego_prim.utils.serialization import to_builtin
from og_ego_prim.utils.task_registry import get_task_config_path


class TaskPlanContext:
    """Normalized planner inputs shared by scripted and model planners."""

    def __init__(self, config: Dict[str, Any]):
        
        # 从task的json文件中读取对应的任务配置
        view = build_agent_task_view(config)
        self.task_instruction = view.instruction
        self.initial_setup = list(view.initial_setup)
        self.object_list = list(view.object_ids)
        self.object_abilities = {
            entity_id: list(abilities)
            for entity_id, abilities in view.object_abilities.items()
        }
        self.wash_rules = list(view.wash_rules)
        self.goal_description = view.goal_description

    @classmethod
    def from_task(cls, task: str) -> "TaskPlanContext":
        with get_task_config_path(task).open("r", encoding="utf-8") as file:
            return cls(json.load(file))

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin(
            {
                "task_instruction": self.task_instruction,
                "initial_setup": list(self.initial_setup),
                "object_list": list(self.object_list),
                "object_abilities": dict(self.object_abilities),
                "wash_rules": list(self.wash_rules),
                "goal_description": self.goal_description,
            }
        )


class ExamplePlanner:
    """Parse per-task ``example_planning`` entries into executable plans."""

    _ACTION = re.compile(r"(?:\d+\.\s+)?([a-zA-Z_]+)\(([^)]*)\)")

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        plans = []
        for item in config.get("example_planning", []) or []:
            action = str(item.get("action", "")).strip()
            if action.upper() == "DONE" or action.upper().startswith("DONE("):
                normalized = "done()"
            else:
                match = cls._ACTION.search(action)
                if match is None:
                    raise ValueError(f"invalid example planning action: {action!r}")
                normalized = f"{match.group(1).lower()}({match.group(2).strip().lower()})"
            plans.append({"action": normalized, "caution": item.get("caution")})
        return plans

    @classmethod
    def from_task(cls, task: str) -> List[Dict[str, Any]]:
        with get_task_config_path(task).open("r", encoding="utf-8") as file:
            return cls.from_config(json.load(file))


__all__ = ["ExamplePlanner", "TaskPlanContext"]
