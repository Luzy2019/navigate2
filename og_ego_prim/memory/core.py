"""Task-specific structured memory with legacy text-memory compatibility."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import time
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple, Union

from og_ego_prim.domain import ActionRecord, StateChange
from og_ego_prim.utils.serialization import to_builtin as _builtin

from .consolidation import DeduplicateConsolidator, MemoryConsolidator


@dataclass
class MemoryRecord:
    """Legacy free-text note retained for import and caller compatibility."""

    content: str
    source: str = "agent"
    step: Optional[int] = None
    room: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.content = (self.content or "").strip()
        if not self.content:
            raise ValueError("memory content must not be empty")
        self.source = (self.source or "agent").strip() or "agent"
        self.room = (self.room or "").strip() or None

    def to_dict(self) -> Dict[str, Any]:
        return _builtin(self)


class MemoryStore:
    """Small compatibility store for explicit notes, not world snapshots."""

    def __init__(
        self,
        records: Optional[Iterable[MemoryRecord]] = None,
    ) -> None:
        self.records: List[MemoryRecord] = []
        for record in records or ():
            self.records.append(
                record if isinstance(record, MemoryRecord) else MemoryRecord(**dict(record))
            )

    def remember(
        self,
        content: str,
        *,
        source: str = "agent",
        step: Optional[int] = None,
        room: Optional[str] = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            content=content,
            source=source,
            step=step,
            room=room,
        )
        self.records.append(record)
        return record

    def recall(self, query: Optional[str] = None, *, room: Optional[str] = None) -> List[MemoryRecord]:
        records = [record for record in self.records if room is None or record.room == room]
        normalized_query = str(query or "").strip().lower()
        if not normalized_query:
            return list(records)
        terms = tuple(term for term in normalized_query.split() if term)
        return [
            record
            for record in records
            if normalized_query in record.content.lower()
            or any(term in record.content.lower() for term in terms)
        ]

    def latest(self, limit: int = 10) -> List[MemoryRecord]:
        limit = int(limit)
        if limit < 0:
            raise ValueError("limit must be non-negative")
        return self.records[-limit:] if limit else []

    def to_prompt_context(self, limit: int = 20) -> str:
        records = self.latest(limit)
        if not records:
            return "None"
        return "\n".join(
            f"- {record.content}" + (f" [room={record.room}]" if record.room else "")
            for record in records
        )

    def clear(self) -> None:
        self.records.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "records": [record.to_dict() for record in self.records],
        }


@dataclass
class MemoryQuery:
    entity_ids: Tuple[str, ...] = ()
    room_id: Optional[str] = None
    action_name: Optional[str] = None
    limit: int = 20

    def __post_init__(self) -> None:
        self.entity_ids = tuple(dict.fromkeys(self.entity_ids))
        self.action_name = (self.action_name or "").upper() or None
        self.limit = max(self.limit, 0)

    def to_dict(self) -> Dict[str, Any]:
        return _builtin(self)


@dataclass
class MemoryRecall:
    state_changes: Tuple[StateChange, ...] = ()
    actions: Tuple[ActionRecord, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return _builtin(self)

    def to_prompt_context(self) -> str:
        lines: List[str] = []
        lines.extend(
            f"- state step={item.step}: {item.entity_id}.{item.key}: "
            f"{_builtin(item.old)!r} -> {_builtin(item.new)!r}"
            for item in self.state_changes
        )
        lines.extend(
            f"- action step={item.step}: {item.action.to_legacy_plan(lowercase=False)}"
            + (f" x{item.count}" if item.count > 1 else "")
            for item in self.actions
        )
        return "\n".join(lines) if lines else "None"


class MemoryRetriever(Protocol):
    def recall(self, memory: "TaskMemory", query: MemoryQuery) -> MemoryRecall:
        ...


class ExactMemoryRetriever:
    """Deterministic entity/room/action retrieval used by v1."""

    def recall(self, memory: TaskMemory, query: MemoryQuery) -> MemoryRecall:
        entity_ids = set(query.entity_ids)
        states = sorted(
            (
            change
            for changes in memory.intermediate_states.values()
            for change in changes
            if (not entity_ids or change.entity_id in entity_ids)
            and (query.room_id is None or change.room_id == query.room_id)
            ),
            key=lambda change: (change.step, change.entity_id, change.key),
        )
        actions = [
            record
            for record in memory.action_history
            if (not entity_ids or bool(entity_ids.intersection(record.action.entity_ids)))
            and (query.room_id is None or record.room_id == query.room_id)
            and (
                query.action_name is None
                or record.action.name == query.action_name
            )
        ]
        limit = query.limit
        return MemoryRecall(
            state_changes=tuple(states[-limit:] if limit else ()),
            actions=tuple(actions[-limit:] if limit else ()),
        )


class TaskMemory(MemoryStore):
    """Bounded task context projected from successful runtime events."""

    def __init__(
        self,
        *,
        task_id: Optional[str] = None,
        enabled: bool = True,
        max_actions: int = 50,
        max_states_per_object: int = 20,
        retriever: Optional[MemoryRetriever] = None,
        consolidator: Optional[MemoryConsolidator] = None,
    ) -> None:
        super().__init__()
        self.task_id = None if task_id is None else str(task_id)
        self.enabled = bool(enabled)
        self.active_subtask_id: Optional[str] = None
        self.action_history: Deque[ActionRecord] = deque(maxlen=max(int(max_actions), 1))
        self.intermediate_states: Dict[str, Deque[StateChange]] = defaultdict(
            lambda: deque(maxlen=max(int(max_states_per_object), 1))
        )
        self.retriever = retriever or ExactMemoryRetriever()
        self.consolidator = consolidator or DeduplicateConsolidator()

    def begin_subtask(self, subtask: Union[Dict[str, Any], str, int]) -> None:
        if isinstance(subtask, Mapping):
            value = subtask.get("subtask_id", subtask.get("subtask_index"))
        else:
            value = subtask
        self.active_subtask_id = None if value is None else str(value)

    def record_action(self, record: ActionRecord) -> bool:
        if not self.enabled or not record.succeeded:
            return False
        payload = _builtin(record.to_dict())
        payload["extensions"] = {}
        action_payload = payload.get("action")
        if isinstance(action_payload, Mapping):
            action_payload["extensions"] = {}
        record = ActionRecord(**payload)
        consolidated = self.consolidator.consolidate_action(
            self.action_history[-1] if self.action_history else None,
            record,
        )
        if consolidated is not None:
            self.action_history.append(consolidated)
        return True

    def record_state_change(self, change: StateChange) -> bool:
        if not self.enabled:
            return False
        if str(change.key).strip().lower() in {"ground_truth", "oracle", "oracle_state"}:
            return False
        payload = _builtin(change.to_dict())
        payload["extensions"] = {}
        change = StateChange(**payload)
        changes = self.intermediate_states[change.entity_id]
        previous = next(
            (item for item in reversed(changes) if item.key == change.key),
            None,
        )
        if not self.consolidator.accept_state(previous, change):
            return False
        changes.append(change)
        return True

    def retrieve(self, query: Optional[MemoryQuery] = None) -> MemoryRecall:
        if not self.enabled:
            return MemoryRecall()
        return self.retriever.recall(self, query or MemoryQuery())

    def forget_entity(
        self,
        entity_id: str,
        *,
        forget_states: bool = True,
        forget_actions: bool = True,
    ) -> Dict[str, int]:
        """Forget records linked to one exact entity ID without recording the deletion."""

        identifier = str(entity_id or "").strip()
        if not identifier:
            raise ValueError("forget_entity entity_id must not be empty")
        removed = {"state_changes": 0, "actions": 0}
        if forget_states:
            removed_states = self.intermediate_states.pop(identifier, ())
            removed["state_changes"] = len(removed_states)
        if forget_actions:
            retained_actions = [
                record
                for record in self.action_history
                if identifier not in record.action.entity_ids
            ]
            removed["actions"] = len(self.action_history) - len(retained_actions)
            self.action_history.clear()
            self.action_history.extend(retained_actions)
        return removed

    def recall(
        self,
        query: Optional[Union[str, MemoryQuery]] = None,
        *,
        room: Optional[str] = None,
    ) -> Union[List[MemoryRecord], MemoryRecall]:
        if isinstance(query, MemoryQuery):
            return self.retrieve(query)
        return super().recall(query, room=room)

    def to_prompt_context(self, limit: int = 20) -> str:
        recall = self.retrieve(MemoryQuery(limit=limit))
        return recall.to_prompt_context()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "active_subtask_id": self.active_subtask_id,
            "enabled": self.enabled,
            "state_changes": [
                _builtin(change.to_dict())
                for changes in self.intermediate_states.values()
                for change in changes
            ],
            "actions": [_builtin(record.to_dict()) for record in self.action_history],
        }

    def clear(self) -> None:
        super().clear()
        self.action_history.clear()
        self.intermediate_states.clear()


__all__ = [
    "ExactMemoryRetriever",
    "MemoryQuery",
    "MemoryRecall",
    "MemoryRecord",
    "MemoryRetriever",
    "MemoryStore",
    "TaskMemory",
]
