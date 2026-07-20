"""Short-lived planner candidate and decision history."""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Iterable, Optional, Tuple

from og_ego_prim.domain import PlannerEpisodeEntry
from og_ego_prim.utils.serialization import to_builtin

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


__all__ = ["PlannerEpisode"]
