"""Task-specific structured memory with legacy text-memory compatibility."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import time
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple, Union

from og_ego_prim.domain import ActionRecord, StateChange
from og_ego_prim.utils.serialization import as_versioned_dict, to_builtin as _builtin


def _runtime_value(value: Any) -> Any:
    """Convert a structured runtime value without a field-name deny list."""

    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return _builtin(value)


def _memory_extensions(value: Any) -> Any:
    value = _runtime_value(value)
    if isinstance(value, Mapping):
        return {
            str(key): _memory_extensions(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_memory_extensions(item) for item in value]
    return value


@dataclass(frozen=True)
class MemoryRecord:
    """Legacy free-text note retained for import and caller compatibility."""

    content: str
    source: str = "agent"
    step: Optional[int] = None
    room: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    schema_version: str = "isbench.memory_note.v1"
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        content = str(self.content or "").strip()
        if not content:
            raise ValueError("memory content must not be empty")
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "source", str(self.source or "agent").strip() or "agent")
        object.__setattr__(self, "step", None if self.step is None else int(self.step))
        object.__setattr__(
            self,
            "room",
            None if self.room is None else (str(self.room).strip() or None),
        )
        object.__setattr__(self, "metadata", _runtime_value(dict(self.metadata or {})))
        object.__setattr__(self, "timestamp", float(self.timestamp))
        object.__setattr__(self, "schema_version", str(self.schema_version).strip())
        object.__setattr__(self, "extensions", _memory_extensions(dict(self.extensions or {})))

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


class MemoryStore:
    """Small compatibility store for explicit notes, not world snapshots."""

    def __init__(
        self,
        records: Optional[Iterable[MemoryRecord]] = None,
        *,
        extensions: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.schema_version = "isbench.memory_store.v1"
        self.extensions = _memory_extensions(dict(extensions or {}))
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
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryRecord:
        if not str(content).strip():
            raise ValueError("memory content must not be empty")
        record = MemoryRecord(
            content=str(content).strip(),
            source=str(source),
            step=step,
            room=room,
            metadata=dict(metadata or {}),
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
            "schema_version": self.schema_version,
            "records": [record.to_dict() for record in self.records],
            "extensions": _builtin(self.extensions),
        }


@dataclass(frozen=True)
class MemoryQuery:
    entity_ids: Tuple[str, ...] = ()
    room_id: Optional[str] = None
    action_name: Optional[str] = None
    limit: int = 20
    include_notes: bool = False
    schema_version: str = "isbench.memory_query.v1"
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        entity_ids = self.entity_ids
        if isinstance(entity_ids, str):
            entity_ids = (entity_ids,)
        elif not isinstance(entity_ids, (list, tuple, set, frozenset)):
            entity_ids = (entity_ids,) if entity_ids is not None else ()
        object.__setattr__(
            self,
            "entity_ids",
            tuple(
                dict.fromkeys(
                    str(value).strip() for value in entity_ids or () if str(value).strip()
                )
            ),
        )
        for field_name in ("room_id", "action_name"):
            value = getattr(self, field_name)
            normalized = None if value is None else (str(value).strip() or None)
            if field_name == "action_name" and normalized is not None:
                normalized = normalized.upper()
            object.__setattr__(self, field_name, normalized)
        object.__setattr__(self, "limit", max(int(self.limit), 0))
        object.__setattr__(self, "include_notes", bool(self.include_notes))
        object.__setattr__(self, "schema_version", str(self.schema_version).strip())
        object.__setattr__(self, "extensions", _memory_extensions(dict(self.extensions or {})))

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class MemoryRecall:
    state_changes: Tuple[StateChange, ...] = ()
    actions: Tuple[ActionRecord, ...] = ()
    notes: Tuple[MemoryRecord, ...] = ()
    schema_version: str = "isbench.memory_recall.v1"
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_changes", tuple(self.state_changes or ()))
        object.__setattr__(self, "actions", tuple(self.actions or ()))
        object.__setattr__(self, "notes", tuple(self.notes or ()))
        object.__setattr__(self, "schema_version", str(self.schema_version).strip())
        object.__setattr__(self, "extensions", _memory_extensions(dict(self.extensions or {})))

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)

    def to_prompt_context(self) -> str:
        lines: List[str] = []
        lines.extend(
            f"- state step={item.step}: {item.entity_id}.{item.key}: "
            f"{_runtime_value(item.old)!r} -> {_runtime_value(item.new)!r}"
            for item in self.state_changes
        )
        lines.extend(
            f"- action step={item.step}: {item.action.to_legacy_plan(lowercase=False)}"
            + (f" x{item.count}" if item.count > 1 else "")
            for item in self.actions
        )
        lines.extend(f"- note: {item.content}" for item in self.notes)
        return "\n".join(lines) if lines else "None"


class MemoryRetriever(Protocol):
    def recall(self, memory: "TaskMemory", query: MemoryQuery) -> MemoryRecall:
        ...


class ExactMemoryRetriever:
    """Deterministic entity/room/action retrieval used by v1."""

    def recall(self, memory: "TaskMemory", query: MemoryQuery) -> MemoryRecall:
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
        notes = (
            [
                record
                for record in memory.records
                if query.room_id is None or record.room == query.room_id
            ]
            if query.include_notes
            else []
        )
        limit = query.limit
        return MemoryRecall(
            state_changes=tuple(states[-limit:] if limit else ()),
            actions=tuple(actions[-limit:] if limit else ()),
            notes=tuple(notes[-limit:] if limit else ()),
        )


class MemoryConsolidator(Protocol):
    def consolidate_action(
        self,
        previous: Optional[ActionRecord],
        current: ActionRecord,
    ) -> Optional[ActionRecord]:
        ...

    def accept_state(
        self,
        previous: Optional[StateChange],
        current: StateChange,
    ) -> bool:
        ...

class DeduplicateConsolidator:
    """Default structural consolidation without generated summaries."""

    def consolidate_action(
        self,
        previous: Optional[ActionRecord],
        current: ActionRecord,
    ) -> Optional[ActionRecord]:
        if (
            previous is not None
            and previous.action.name == "WAIT"
            and current.action.name == "WAIT"
            and previous.action.actor_id == current.action.actor_id
            and previous.action.object_id == current.action.object_id
            and previous.action.target_id == current.action.target_id
            and _builtin(previous.action.parameters) == _builtin(current.action.parameters)
            and previous.task_id == current.task_id
            and previous.subtask_id == current.subtask_id
            and previous.room_id == current.room_id
        ):
            previous.count += current.count
            return None
        return current

    def accept_state(
        self,
        previous: Optional[StateChange],
        current: StateChange,
    ) -> bool:
        return not (
            current.old == current.new
            or (
                previous is not None
                and previous.key == current.key
                and previous.new == current.new
            )
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
        extensions: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(extensions=extensions)
        self.task_id = None if task_id is None else str(task_id)
        self.schema_version = "isbench.task_memory.v1"
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
        payload = _memory_extensions(record.to_dict())
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
        payload = _memory_extensions(change.to_dict())
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
        recall = self.retrieve(MemoryQuery(limit=limit, include_notes=False))
        return recall.to_prompt_context()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "active_subtask_id": self.active_subtask_id,
            "enabled": self.enabled,
            "state_changes": [
                _memory_extensions(change.to_dict())
                for changes in self.intermediate_states.values()
                for change in changes
            ],
            "actions": [_memory_extensions(record.to_dict()) for record in self.action_history],
            "extensions": _builtin(self.extensions),
        }

    def clear(self) -> None:
        super().clear()
        self.action_history.clear()
        self.intermediate_states.clear()


__all__ = [
    "ExactMemoryRetriever",
    "DeduplicateConsolidator",
    "MemoryQuery",
    "MemoryRecall",
    "MemoryRecord",
    "MemoryRetriever",
    "MemoryConsolidator",
    "MemoryStore",
    "TaskMemory",
]
