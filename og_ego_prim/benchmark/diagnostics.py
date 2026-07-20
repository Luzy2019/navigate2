"""Diagnostic helpers for benchmark goal evaluation."""

from __future__ import annotations

import os
import re
from typing import Any, Optional


def debug_object_position(obj: Any) -> Any:
    if obj is None:
        return None
    try:
        position = obj.get_position_orientation()[0]
        return [round(float(x), 6) for x in position.tolist()]
    except Exception:
        return None


def debug_goal_atoms(env: Any, goal_bddl: Optional[str]) -> None:
    """Print selected BDDL atom states when goal debugging is enabled."""
    if not goal_bddl or not os.environ.get("ISBENCH_DEBUG_GOAL_ATOMS"):
        return

    try:
        from omnigibson import object_states
    except Exception as exc:
        print(f"[lifelong_evaluator][goal_atom_debug] import_failed error={exc}")
        return

    state_map = {
        "inside": getattr(object_states, "Inside", None),
        "ontop": getattr(object_states, "OnTop", None),
        "under": getattr(object_states, "Under", None),
        "nextto": getattr(object_states, "NextTo", None),
        "covered": getattr(object_states, "Covered", None),
        "open": getattr(object_states, "Open", None),
    }

    object_scope = getattr(env.task, "object_scope", {})
    for predicate, object_name, target_name in re.findall(
        r"\((inside|ontop|under|nextto|covered)\s+([^\s()]+)\s+([^\s()]+)\)",
        goal_bddl,
        flags=re.IGNORECASE,
    ):
        state_cls = state_map.get(predicate.lower())
        obj_ref = object_scope.get(object_name)
        target_ref = object_scope.get(target_name)
        obj = getattr(obj_ref, "wrapped_obj", None)
        target = getattr(target_ref, "wrapped_obj", None)
        value = None
        if obj is not None and target is not None and state_cls in getattr(obj, "states", {}):
            try:
                value = bool(obj.states[state_cls].get_value(target))
            except Exception as exc:
                value = f"error:{type(exc).__name__}:{exc}"
        print(
            "[lifelong_evaluator][goal_atom_debug] "
            f"predicate={predicate.lower()} object={object_name} "
            f"sim_object={getattr(obj, 'name', None)} "
            f"object_pos={debug_object_position(obj)} "
            f"target={target_name} sim_target={getattr(target, 'name', None)} "
            f"target_pos={debug_object_position(target)} value={value}"
        )

    for predicate, object_name in re.findall(
        r"\((open)\s+([^\s()]+)\)",
        goal_bddl,
        flags=re.IGNORECASE,
    ):
        state_cls = state_map.get(predicate.lower())
        obj_ref = object_scope.get(object_name)
        obj = getattr(obj_ref, "wrapped_obj", None)
        value = None
        if obj is not None and state_cls in getattr(obj, "states", {}):
            try:
                value = bool(obj.states[state_cls].get_value())
            except Exception as exc:
                value = f"error:{type(exc).__name__}:{exc}"
        print(
            "[lifelong_evaluator][goal_atom_debug] "
            f"predicate={predicate.lower()} object={object_name} "
            f"sim_object={getattr(obj, 'name', None)} "
            f"object_pos={debug_object_position(obj)} value={value}"
        )
