"""Short-lived planner candidate and decision history."""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Iterable, Mapping, Optional, Tuple

from og_ego_prim.domain import PlannerEpisodeEntry
from og_ego_prim.utils.serialization import to_builtin


class PlannerEpisode:
    def __init__(
        self,
        entries: Iterable[PlannerEpisodeEntry] = (),
        *,
        max_entries: int = 100,
        schema_version: str = "isbench.planner_episode.v1",
        extensions: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.max_entries = max(int(max_entries), 1)
        self.entries: Deque[PlannerEpisodeEntry] = deque(
            entries,
            maxlen=self.max_entries,
        )
        self.schema_version = str(schema_version or "isbench.planner_episode.v1")
        self.extensions = dict(extensions or {})

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
                "schema_version": self.schema_version,
                "entries": [entry.to_dict() for entry in self.entries],
                "max_entries": self.max_entries,
                "extensions": self.extensions,
            }
        )

    def __len__(self) -> int:
        return len(self.entries)


__all__ = ["PlannerEpisode"]
