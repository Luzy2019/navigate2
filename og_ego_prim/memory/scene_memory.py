"""Semantic memory adapter for scene-graph snapshots."""

from __future__ import annotations

from typing import Any, Optional

from .core import MemoryStore


class SceneMemory(MemoryStore):
    """Compatibility facade that no longer persists full scene snapshots."""

    def observe(self, snapshot: Any, *, step: Optional[int] = None, room: Optional[str] = None) -> None:
        # Current scene state belongs to perception/object_model. Structured
        # state changes are written through TaskMemory.record_state_change().
        del snapshot, step, room
