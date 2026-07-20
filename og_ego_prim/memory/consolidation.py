"""Structural memory consolidation extension point."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from og_ego_prim.domain import ActionRecord, Registry, StateChange
from og_ego_prim.utils.serialization import to_builtin as _builtin


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


MEMORY_CONSOLIDATORS: Registry[Any] = Registry()
MEMORY_CONSOLIDATORS.register("deduplicate", DeduplicateConsolidator)


def create_memory_consolidator(name: str = "deduplicate", **options: Any) -> MemoryConsolidator:
    factory = MEMORY_CONSOLIDATORS.require(name)
    return factory(**options)


__all__ = [
    "DeduplicateConsolidator",
    "MEMORY_CONSOLIDATORS",
    "MemoryConsolidator",
    "create_memory_consolidator",
]
