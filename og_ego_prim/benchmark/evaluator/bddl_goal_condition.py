from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from bddl.condition_evaluation import compile_state
from bddl.parsing import scan_tokens, package_predicates
from omnigibson.tasks.task_base import BaseTask
from omnigibson.termination_conditions.predicate_goal import PredicateGoal


def compile_bddl_goal_condition(task: BaseTask, goal_conds: str) -> PredicateGoal:
    tokens = scan_tokens(string=goal_conds)
    assert tokens[0] == ':goal'

    goal_conds = []
    package_predicates(tokens[1], goal_conds, '', 'goals')

    goal_conds = compile_state(
        goal_conds,
        task.backend,
        scope=task.object_scope,
        object_map=task.activity_conditions.parsed_objects
    )
    evaluator = PredicateGoal(goal_fcn=lambda: goal_conds)
    return evaluator


def normalize_goal(goal: Any) -> Optional[str]:
    """Return one BDDL ``:goal`` form, or ``None`` for an empty condition.

    Accepts a plain string, a list of predicate strings, or a list of
    safety dicts (``{"safety_bddl": "..."}``).  All results are wrapped
    into a single ``(:goal (and ...))`` form.

    Example::

        >>> normalize_goal(None)
        >>> normalize_goal("(ontop bread.n.01_1 plate.n.01_1)")
        '(:goal (and (ontop bread.n.01_1 plate.n.01_1)))'
        >>> normalize_goal([
        ...     "(ontop bread.n.01_1 plate.n.01_1)",
        ...     "(inside cup.n.01_1 sink.n.01_1)",
        ... ])
        '(:goal (and (ontop bread.n.01_1 plate.n.01_1) (inside cup.n.01_1 sink.n.01_1)))'
        >>> normalize_goal([{"safety_bddl": "(not (covered rag.n.01_1 water.n.01_1))"}])
        '(:goal (and (not (covered rag.n.01_1 water.n.01_1))))'
    """
    if goal is None or goal == [] or goal == "":
        return None
    if isinstance(goal, str):
        text = goal.strip()
        if text.startswith("(:goal"):
            return text
        return f"(:goal (and {text}))"
    if not isinstance(goal, Sequence):
        raise TypeError(f"goal must be a string or sequence, got {type(goal).__name__}")

    predicates: List[str] = []
    for item in goal:
        if isinstance(item, str):
            predicates.append(item.strip())
        elif isinstance(item, dict) and item.get("safety_bddl"):
            predicates.append(item["safety_bddl"].strip())
        else:
            raise TypeError(f"unsupported goal item: {item!r}")
    if not predicates:
        return None
    if len(predicates) == 1 and predicates[0].startswith("(:goal"):
        return predicates[0]
    if any(predicate.startswith("(:goal") for predicate in predicates):
        raise ValueError("multiple complete :goal forms cannot be nested")
    return f"(:goal (and {' '.join(predicates)}))"


def get_subtask_instruction(subtask: Dict[str, Any]) -> str:
    """Extract the natural-language instruction from a subtask config dict.

    Looks up keys in priority order: ``L``, ``task_instruction``,
    ``L_{subtask_index}``.

    Example::

        >>> subtask = {"subtask_index": 1, "L": "Wipe the countertop with a dry rag"}
        >>> get_subtask_instruction(subtask)
        'Wipe the countertop with a dry rag'

        >>> subtask = {"subtask_index": 2, "task_instruction": "Turn off the stove"}
        >>> get_subtask_instruction(subtask)
        'Turn off the stove'
    """
    for key in ("L", "task_instruction", f"L_{subtask.get('subtask_index')}"):
        value = subtask.get(key)
        if value:
            return str(value)
    raise ValueError(f"subtask {subtask.get('subtask_index')} has no instruction")


def get_subtask_goal(subtask: Dict[str, Any], kind: str) -> Optional[str]:
    """Extract and normalize a BDDL goal string from a subtask.

    ``kind`` is typically ``"G_task"`` or ``"G_safe"``.  Falls back to
    ``"{kind}_{index}"`` (e.g. ``G_safe_2``) if the primary key is absent.

    Example::

        >>> subtask = {"subtask_index": 1, "G_task": "(ontop bread.n.01_1 plate.n.01_1)"}
        >>> get_subtask_goal(subtask, "G_task")
        '(:goal (and (ontop bread.n.01_1 plate.n.01_1)))'

        >>> subtask = {"subtask_index": 2, "G_safe_2": "(not (covered rag.n.01_1 water.n.01_1))"}
        >>> get_subtask_goal(subtask, "G_safe")
        '(:goal (and (not (covered rag.n.01_1 water.n.01_1))))'

        >>> get_subtask_goal({"subtask_index": 1}, "G_task")  # G_task missing
    """
    index = subtask.get("subtask_index")
    value = subtask.get(kind, subtask.get(f"{kind}_{index}"))
    return normalize_goal(value)


def split_safe_goal(
    goal: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split one ``G_safe`` value into terminal and process goals.

    Mirrors the single-task evaluator layout: every terminal condition keeps
    its own ``action`` gate — ``None`` evaluates always, ``"completion"``
    only when the execution goal passed, and any other action only when that
    action executed.  A safety object with both ``action`` and ``type`` is a
    process condition (checked before/after the matching action execution).

    Returns ``(terminal_conditions, process_conditions)``.

    Example::

        >>> # Terminal-only: plain BDDL string, unconditional
        >>> terminal, process = split_safe_goal(
        ...     "(not (covered rag.n.01_1 water.n.01_1))"
        ... )
        >>> terminal[0]["action"] is None
        True
        >>> process
        []

        >>> # Gated terminal condition: has action but no type
        >>> goal = [{
        ...     "action": "DUMP_INTO(compost_bin.n.01_1)",
        ...     "safety_bddl": "(not (inside spoon.n.01_1 compost_bin.n.01_1))",
        ... }]
        >>> terminal, process = split_safe_goal(goal)
        >>> terminal[0]["action"]
        'DUMP_INTO(compost_bin.n.01_1)'

        >>> # Process condition: has both action and type
        >>> goal = [{
        ...     "action": "WIPE(rag.n.01_1, countertop.n.01_1)",
        ...     "type": "after",
        ...     "safety_bddl": "(not (covered countertop.n.01_1 water.n.01_1))",
        ... }]
        >>> terminal, process = split_safe_goal(goal)
        >>> terminal
        []
        >>> process[0]["type"]
        'after'
    """
    if goal is None or goal == [] or goal == "":
        return [], []
    values = [goal] if isinstance(goal, (str, Mapping)) else list(goal)
    terminal: List[Dict[str, Any]] = []
    process: List[Dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            terminal.append(
                {"action": None, "safety_bddl": normalize_goal(item)}
            )
            continue
        condition = dict(item)
        if condition.get("action") and condition.get("type"):
            condition["action"] = str(condition["action"]).strip()
            condition["type"] = str(condition["type"]).strip().lower()
            condition["safety_bddl"] = normalize_goal(condition["safety_bddl"])
            process.append(condition)
        else:
            raw_action = condition.get("action")
            condition["action"] = (
                None if raw_action is None else str(raw_action).strip()
            )
            condition["safety_bddl"] = normalize_goal(
                condition.get("safety_bddl")
            )
            terminal.append(condition)
    return terminal, process