"""Typed runtime events shared by controller projections and tracker sinks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple
from uuid import uuid4

from og_ego_prim.domain import ActionRecord
from og_ego_prim.utils.serialization import ExtensionMap, as_versioned_dict


@dataclass
class RuntimeEvent:
    event_type: str
    step: int
    event_id: str = field(default_factory=lambda: str(uuid4()))
    task_id: Optional[str] = None
    subtask_id: Optional[str] = None
    sim_time: Optional[float] = None
    entity_ids: Tuple[str, ...] = ()
    action_id: Optional[str] = None
    before: Any = None
    after: Any = None
    source: str = "agent_runtime"
    confidence: float = 1.0
    details: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = "isbench.runtime_event.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.event_type = str(self.event_type or "").strip().lower()
        if not self.event_type:
            raise ValueError("runtime event type must not be empty")
        self.step = int(self.step)
        self.event_id = str(self.event_id or uuid4()).strip()
        self.task_id = str(self.task_id).strip() if self.task_id is not None else None
        self.subtask_id = (
            str(self.subtask_id).strip() if self.subtask_id is not None else None
        )
        self.sim_time = float(self.sim_time) if self.sim_time is not None else None
        self.entity_ids = tuple(
            dict.fromkeys(str(value).strip() for value in self.entity_ids if str(value).strip())
        )
        self.action_id = str(self.action_id).strip() if self.action_id is not None else None
        self.source = str(self.source or "agent_runtime").strip() or "agent_runtime"
        self.confidence = float(self.confidence)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("runtime event confidence must be between 0 and 1")
        self.details = dict(self.details or {})
        self.extensions = dict(self.extensions or {})

    @classmethod
    def from_action_record(
        cls,
        record: ActionRecord,
        *,
        event_type: str = "action_executed",
        sim_time: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> "RuntimeEvent":
        return cls(
            event_type=event_type,
            step=record.step,
            task_id=record.task_id,
            subtask_id=record.subtask_id,
            sim_time=sim_time,
            entity_ids=record.action.entity_ids,
            action_id=record.action_id,
            source=record.source,
            details={
                "action_record": record.to_dict(),
                **dict(details or {}),
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


def entity_ids(values: Iterable[object]) -> Tuple[str, ...]:
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


__all__ = ["RuntimeEvent", "entity_ids"]
