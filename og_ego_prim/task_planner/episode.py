"""Short-lived planner candidate and decision history."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, Optional, Tuple
from uuid import uuid4

from og_ego_prim.domain import Action, ActionDecision
from og_ego_prim.utils.serialization import ExtensionMap, as_versioned_dict, to_builtin


@dataclass
class PlannerEpisodeEntry:
    """One proposed action and its runtime gate decision."""

    step: int
    action: Action
    decision: ActionDecision
    entry_id: str = field(default_factory=lambda: str(uuid4()))
    attempt: int = 0
    reason: Optional[str] = None
    schema_version: str = "isbench.planner_episode_entry.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, Action):
            if not isinstance(self.action, dict):
                raise TypeError("planner episode action must be an Action")
            self.action = Action(**self.action)
        if not isinstance(self.decision, ActionDecision):
            self.decision = ActionDecision(str(self.decision).strip().upper())
        self.step = int(self.step)
        self.entry_id = str(self.entry_id or "").strip() or str(uuid4())
        self.attempt = int(self.attempt)
        if self.attempt < 0:
            raise ValueError("planner episode attempt must be non-negative")
        self.reason = None if self.reason is None else (str(self.reason).strip() or None)
        self.extensions = dict(self.extensions or {})

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)

'''
    PlannerEpisodeEntry的历史记录，查看当前快照的管理工具
'''
class PlannerEpisode:
    def __init__(
        self,
        entries: Iterable[PlannerEpisodeEntry] = (),
        *,
        max_entries: int = 100,
    ) -> None:
        self.max_entries = max(int(max_entries), 1)
        self.entries: Deque[PlannerEpisodeEntry] = deque(
            entries,
            maxlen=self.max_entries,
        )

    def append(self, entry: PlannerEpisodeEntry) -> PlannerEpisodeEntry:
        self.entries.append(entry)
        return entry

    def latest(self) -> Optional[PlannerEpisodeEntry]:
        return self.entries[-1] if self.entries else None

    def snapshot(self) -> Tuple[PlannerEpisodeEntry, ...]:
        return tuple(self.entries)

    def clear(self) -> None:
        self.entries.clear()

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin(
            {
                "entries": [entry.to_dict() for entry in self.entries],
                "max_entries": self.max_entries,
            }
        )

    def __len__(self) -> int:
        return len(self.entries)


__all__ = ["PlannerEpisode", "PlannerEpisodeEntry"]
