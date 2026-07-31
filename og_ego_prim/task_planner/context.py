"""Task configuration adapters for planning."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

from og_ego_prim.config.task_definition import build_agent_task_view
from og_ego_prim.utils.serialization import to_builtin
from og_ego_prim.utils.task_registry import get_task_config_path


def _extract_safety_tips(g_safe: Any) -> List[str]:
    """Pull natural-language safety tips out of a raw ``G_safe`` value.

    ``G_safe`` mixes evaluator BDDL goal strings with hazard dictionaries.
    Only the hazard dictionaries (or plain-text entries) carry text that may
    be shown to the planner; parenthesized BDDL is evaluator-only and skipped.
    """
    tips: List[str] = []
    for item in g_safe or ():
        if isinstance(item, Mapping):
            tip = item.get("safety_tip") or item.get("caution") or item.get("message")
            if tip:
                tips.append(str(tip).strip())
        elif isinstance(item, str):
            text = item.strip()
            if text and not text.startswith("("):
                tips.append(text)
    return tips


def _unique(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


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

        # Task-authored safety tips (GT) for the explicit v3 prompt setting.
        # ``build_agent_task_view`` deliberately strips evaluator G_safe, so
        # the raw config is parsed here instead.  Tips are grouped by the
        # 1-based subtask index used throughout the lifelong runners.
        self.subtask_safety_tips: Dict[int, List[str]] = {}
        subtasks = config.get("subtasks") or ()
        for position, subtask in enumerate(subtasks, start=1):
            if not isinstance(subtask, Mapping):
                continue
            raw_index = subtask.get("subtask_index", position)
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                index = position
            self.subtask_safety_tips[index] = _extract_safety_tips(
                subtask.get("G_safe")
            )
        if self.subtask_safety_tips:
            self.safety_tips = _unique(
                [
                    tip
                    for tips in self.subtask_safety_tips.values()
                    for tip in tips
                ]
            )
        else:
            self.safety_tips = _unique(
                _extract_safety_tips(config.get("G_safe"))
                or _extract_safety_tips(config.get("evaluation_cautions"))
            )

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
                "safety_tips": list(self.safety_tips),
                "subtask_safety_tips": {
                    index: list(tips)
                    for index, tips in self.subtask_safety_tips.items()
                },
            }
        )


__all__ = ["TaskPlanContext"]
