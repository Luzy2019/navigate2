"""Controller review and execution outcome contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from og_ego_prim.domain import (
    Action,
    ActionDecision,
    ActionRecord,
)
from og_ego_prim.utils.serialization import as_versioned_dict
from og_ego_prim.risk_predictor import RiskEvaluation


@dataclass
class ActionReview:
    action: Action
    decision: ActionDecision
    temporal_gate: Any = None
    risk_evaluation: Optional[RiskEvaluation] = None
    should_rethink: bool = False
    reason: Optional[str] = None
    schema_version: str = "isbench.action_review.v1"
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, Action):
            if not isinstance(self.action, Mapping):
                raise TypeError("action review action must be an Action or mapping")
            self.action = Action(**dict(self.action))
        if not isinstance(self.decision, ActionDecision):
            self.decision = ActionDecision(str(self.decision).strip().upper())
        if self.risk_evaluation is not None and not isinstance(
            self.risk_evaluation, RiskEvaluation
        ):
            if not isinstance(self.risk_evaluation, Mapping):
                raise TypeError(
                    "action review risk_evaluation must be a RiskEvaluation or mapping"
                )
            self.risk_evaluation = RiskEvaluation(**dict(self.risk_evaluation))
        self.should_rethink = bool(self.should_rethink)
        self.reason = None if self.reason is None else (str(self.reason).strip() or None)
        self.schema_version = str(self.schema_version or "").strip()
        self.extensions = dict(self.extensions or {})

    @property
    def allowed(self) -> bool:
        return self.decision in {ActionDecision.ALLOW, ActionDecision.CAUTION}

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass
class ActionOutcome:
    review: ActionReview
    executed: bool
    succeeded: bool
    action_record: Optional[ActionRecord] = None
    reason: Optional[str] = None
    schema_version: str = "isbench.action_outcome.v1"
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.review, ActionReview):
            if not isinstance(self.review, Mapping):
                raise TypeError("action outcome review must be an ActionReview or mapping")
            self.review = ActionReview(**dict(self.review))
        self.executed = bool(self.executed)
        self.succeeded = bool(self.succeeded)
        if self.action_record is not None and not isinstance(self.action_record, ActionRecord):
            if not isinstance(self.action_record, Mapping):
                raise TypeError("action outcome action_record must be an ActionRecord or mapping")
            self.action_record = ActionRecord(**dict(self.action_record))
        self.reason = None if self.reason is None else (str(self.reason).strip() or None)
        self.schema_version = str(self.schema_version or "").strip()
        self.extensions = dict(self.extensions or {})

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


__all__ = ["ActionOutcome", "ActionReview"]
