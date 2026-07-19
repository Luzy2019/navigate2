"""Current object state and bounded per-object manipulation history."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple

from og_ego_prim.domain import StateChange
from og_ego_prim.utils.serialization import ExtensionMap, as_versioned_dict


def _object_mapping(value: Any) -> Dict[str, Any]:
    copied = deepcopy(dict(value or {}))
    return {str(key): item for key, item in copied.items()}


@dataclass
class ManipulationFact:
    action_id: str
    action: str
    step: int
    actor_id: Optional[str] = None
    tool_id: Optional[str] = None
    target_id: Optional[str] = None
    role: str = "object"
    count: int = 1
    schema_version: str = "isbench.manipulation_fact.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action_id = str(self.action_id or "").strip()
        self.action = str(self.action or "").strip().upper()
        self.step = int(self.step)
        self.role = str(self.role or "object").strip().lower()
        for field_name in ("actor_id", "tool_id", "target_id"):
            value = getattr(self, field_name)
            setattr(
                self,
                field_name,
                None if value is None else (str(value).strip() or None),
            )
        self.count = int(self.count)
        if not self.action_id or not self.action:
            raise ValueError("manipulation fact requires action_id and action")
        if self.count <= 0:
            raise ValueError("manipulation fact count must be greater than zero")
        self.schema_version = str(self.schema_version).strip()
        self.extensions = deepcopy(dict(self.extensions or {}))

    @property
    def signature(self) -> Tuple[Any, ...]:
        return (
            self.action,
            self.actor_id,
            self.tool_id,
            self.target_id,
            self.role,
        )

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass
class ObjectRecord:
    """Agent-visible object view; simulator objects remain the physical truth."""

    entity_id: str
    canonical_name: str
    aliases: Set[str] = field(default_factory=set)
    properties: Dict[str, Any] = field(default_factory=dict)
    states: Dict[str, Any] = field(default_factory=dict)
    capabilities: Set[str] = field(default_factory=set)
    room_id: Optional[str] = None
    available: bool = True
    last_seen_step: Optional[int] = None
    manipulations: List[ManipulationFact] = field(default_factory=list)
    manipulation_limit: int = 20
    schema_version: str = "isbench.object_record.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.entity_id = str(self.entity_id or "").strip()
        self.canonical_name = str(self.canonical_name or self.entity_id).strip()
        if not self.entity_id:
            raise ValueError("object record entity_id must not be empty")
        if not self.canonical_name:
            raise ValueError("object record canonical_name must not be empty")
        aliases = (self.aliases,) if isinstance(self.aliases, str) else (self.aliases or ())
        self.aliases = {str(value).strip() for value in aliases if str(value).strip()}
        self.aliases.add(self.entity_id)
        self.aliases.add(self.canonical_name)
        self.properties = _object_mapping(self.properties)
        self.states = _object_mapping(self.states)
        capabilities = (
            (self.capabilities,)
            if isinstance(self.capabilities, str)
            else (self.capabilities or ())
        )
        self.capabilities = {
            str(value).strip().lower()
            for value in capabilities
            if str(value).strip()
        }
        self.room_id = (
            (str(self.room_id).strip() or None) if self.room_id is not None else None
        )
        self.available = bool(self.available)
        if self.last_seen_step is not None:
            self.last_seen_step = int(self.last_seen_step)
        self.manipulation_limit = int(self.manipulation_limit)
        if self.manipulation_limit <= 0:
            raise ValueError("manipulation_limit must be greater than zero")
        self.manipulations = [
            value
            if isinstance(value, ManipulationFact)
            else ManipulationFact(**dict(value))
            for value in self.manipulations or ()
        ][-self.manipulation_limit :]
        self.schema_version = str(self.schema_version).strip()
        self.extensions = deepcopy(dict(self.extensions or {}))

    @property
    def actionable(self) -> bool:
        return self.available and self.states.get("actionable", True) is not False

    def add_aliases(self, *aliases: str) -> None:
        self.aliases.update(
            str(value).strip()
            for value in aliases
            if value is not None and str(value).strip()
        )

    def update_state(self, change: StateChange) -> None:
        if change.key == "room_id":
            self.room_id = str(change.new) if change.new is not None else None
        elif change.key == "available":
            self.available = bool(change.new)
        else:
            copied = _object_mapping({change.key: change.new})
            if change.key in copied:
                self.states[change.key] = copied[change.key]
        if change.room_id is not None:
            self.room_id = change.room_id
        if change.source == "perception":
            self.last_seen_step = max(self.last_seen_step or change.step, change.step)

    def add_manipulation(self, fact: ManipulationFact) -> None:
        if not isinstance(fact, ManipulationFact):
            fact = ManipulationFact(**dict(fact))
        self.manipulations.append(fact)
        if len(self.manipulations) > self.manipulation_limit:
            del self.manipulations[: -self.manipulation_limit]

    def clear_manipulations(self) -> None:
        self.manipulations.clear()

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


__all__ = ["ManipulationFact", "ObjectRecord"]
