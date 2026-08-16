"""Task configuration adapters for planning."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional

from og_ego_prim.config.task_definition import build_agent_task_view
from og_ego_prim.utils.constants import BDDLS
from og_ego_prim.utils.serialization import to_builtin
from og_ego_prim.utils.task_registry import get_task_config_path


_INROOM_PATTERN = re.compile(
    r"\(\s*inroom\s+([A-Za-z0-9_.]+)\s+([A-Za-z0-9_.]+)\s*\)",
    re.IGNORECASE,
)


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


def _find_bddl_problem(task_name: str) -> Optional[str]:
    """Locate the BDDL problem file for a task, if any.

    The planner does not execute BDDL; it only reads the authoritative
    ``(:init (inroom <obj> <room>))`` assignments so the model can tell which
    floor / object belongs to which room.
    """
    if not task_name:
        return None
    task_dir = os.path.join(BDDLS, task_name)
    if not os.path.isdir(task_dir):
        return None
    for name in sorted(os.listdir(task_dir)):
        if name.startswith("problem") and name.endswith(".bddl"):
            return os.path.join(task_dir, name)
    return None


def _parse_inroom_assignments(bddl_path: str) -> Dict[str, str]:
    """Return {object_id: room_id} from ``(:init (inroom ...))`` clauses."""
    try:
        with open(bddl_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    assignments: Dict[str, str] = {}
    for match in _INROOM_PATTERN.finditer(text):
        obj, room = match.group(1).strip(), match.group(2).strip()
        if obj and room:
            assignments.setdefault(obj, room)
    return assignments


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
        # Room/area names from scene_info. Rooms describe where objects are;
        # they are context and must never become action targets.
        scene_info = config.get("scene_info") or {}
        self.rooms = list(
            str(room).strip()
            for room in (scene_info.get("rooms") or ())
            if str(room).strip()
        )

        # Authoritative room assignment for every object (including floors)
        # parsed from the BDDL ``(:init (inroom ...))`` clauses.  This lets the
        # model distinguish e.g. floor.n.01_1 (corridor) from floor.n.01_4
        # (living room) instead of guessing from the camera.
        task_name = str(view.task_name or "")
        bddl_problem = _find_bddl_problem(task_name) or _find_bddl_problem(
            str(config.get("task_info", {}).get("task_name") or "")
        )
        self.object_room_assignments: Dict[str, str] = (
            _parse_inroom_assignments(bddl_problem) if bddl_problem else {}
        )
        self.floor_room_map: str = self._render_floor_room_map()

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

        # Task-authored placement constraints (GT) for the starter prompt.
        # Each entry is a mapping with an optional ``subtask_index`` (1-based),
        # a target container entity, and a mandatory relation hint rendered as a
        # hard prompt rule (e.g. hamper must be filled with PLACE_INSIDE, never
        # PLACE_ON_TOP). Constraints without a subtask index apply globally.
        self.subtask_placement_constraints: Dict[int, List[str]] = {}
        self.global_placement_constraints: List[str] = []
        for entry in config.get("placement_constraints") or ():
            if not isinstance(entry, Mapping):
                continue
            rule = str(entry.get("rule") or "").strip()
            if not rule:
                continue
            raw_index = entry.get("subtask_index")
            if raw_index is None:
                self.global_placement_constraints.append(rule)
                continue
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                self.global_placement_constraints.append(rule)
                continue
            self.subtask_placement_constraints.setdefault(index, []).append(rule)
        self.placement_constraints = _unique(
            list(self.global_placement_constraints)
            + [
                rule
                for rules in self.subtask_placement_constraints.values()
                for rule in rules
            ]
        )

    @classmethod
    def from_task(cls, task: str) -> "TaskPlanContext":
        with get_task_config_path(task).open("r", encoding="utf-8") as file:
            return cls(json.load(file))

    def _render_floor_room_map(self) -> str:
        """Render a compact, prompt-safe floor-to-room mapping line.

        Rooms are context; the map only helps the model pick the right floor
        entity for navigation / staging.  Non-floor objects are omitted to
        keep the prompt lean and avoid leaking evaluator-only bindings.
        """
        if not self.object_room_assignments:
            return ""
        entries = []
        for obj_id, room_id in self.object_room_assignments.items():
            if not obj_id.lower().startswith("floor"):
                continue
            entries.append(f"{obj_id} is in {room_id}")
        return "; ".join(entries)

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
                "placement_constraints": list(self.placement_constraints),
                "subtask_placement_constraints": {
                    index: list(rules)
                    for index, rules in self.subtask_placement_constraints.items()
                },
            }
        )


__all__ = ["TaskPlanContext"]
