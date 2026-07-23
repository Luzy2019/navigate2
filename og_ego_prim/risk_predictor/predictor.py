"""Action-level runtime risk predictor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from og_ego_prim.domain import Action
from og_ego_prim.utils.task_registry import get_task_config_path

from .engine import RiskEngine, create_hazard, decision_for_hazards
from .models import RiskContext, RiskEvaluation
from .providers import RiskProvider, RuleRiskProvider


def _runtime_task_mapping(task: Any) -> Mapping[str, Any]:
    if isinstance(task, Mapping):
        config = dict(task)
    elif callable(getattr(task, "to_dict", None)):
        config = dict(task.to_dict())
    else:
        path = get_task_config_path(str(task))
        with Path(path).open("r", encoding="utf-8") as file:
            config = json.load(file)

    if "safety_cues" in config:
        return config
    if any(
        key in config
        for key in ("task_info", "evaluation_goal_conditions", "subtasks")
    ):
        from og_ego_prim.config.runtime_config import build_runtime_task_config

        return build_runtime_task_config(config).to_dict()
    return config

class RiskPredictor(RiskEngine):
    """Predict hazards and an action decision from live runtime context."""

    def __init__(
        self,
        provider: RiskProvider,
    ) -> None:
        super().__init__(provider)
        self.active_subtask: int | None = None

    @classmethod
    def from_task(cls, task: Any) -> "RiskPredictor":
        config = _runtime_task_mapping(task)
        return cls(RuleRiskProvider.from_task(config))

    @property
    def risks(self):
        return list(self.active_hazards)

    def set_active_subtask(self, subtask_index: int | None) -> None:
        self.active_subtask = None if subtask_index is None else int(subtask_index)

    def predict(self, action: Any, context: Any = None) -> RiskEvaluation:
        if not isinstance(action, Action):
            action = Action.from_raw(str(action))
        current = RiskContext.from_value(context, action=action)
        if current.active_subtask is None and self.active_subtask is not None:
            current = RiskContext(
                action=current.action,
                scene=current.scene,
                objects=current.objects,
                scheduler=current.scheduler,
                task=current.task,
                active_subtask=self.active_subtask,
            )
        return self.evaluate(action, current)

    def predict_dicts(self, action: Any, context: Any = None):
        return [hazard.to_dict() for hazard in self.predict(action, context).hazards]

__all__ = [
    "RiskPredictor",
    "create_hazard",
    "decision_for_hazards",
]
