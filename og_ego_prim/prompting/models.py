"""Typed, versioned input to semantic prompt rendering."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from og_ego_prim.domain import Action
from og_ego_prim.utils.serialization import as_versioned_dict


def _copy_context(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _copy_context(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_copy_context(item) for item in value]
    return deepcopy(value)


@dataclass
class PromptContext:
    task_instruction: str = ""
    current_scene: Any = None
    object_views: Sequence[Any] = ()
    memory_recall: Any = None
    pending_timers: Sequence[Any] = ()
    candidate_action: Optional[Action] = None
    allowed_actions: Sequence[str] = ()
    rethinking_reason: Optional[str] = None
    section_data: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "isbench.prompt_context.v1"
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.task_instruction = str(self.task_instruction or "")
        if self.candidate_action is not None and not isinstance(self.candidate_action, Action):
            if isinstance(self.candidate_action, Mapping):
                self.candidate_action = Action(**dict(self.candidate_action))
            else:
                raise TypeError("prompt candidate_action must be an Action or mapping")
        self.object_views = tuple(self.object_views or ())
        self.pending_timers = tuple(self.pending_timers or ())
        self.allowed_actions = tuple(
            str(value).strip().upper()
            for value in self.allowed_actions or ()
            if str(value).strip()
        )
        self.section_data = _copy_context(dict(self.section_data or {}))
        self.extensions = _copy_context(dict(self.extensions or {}))

    @property
    def relevant_entity_ids(self) -> Tuple[str, ...]:
        configured = self.section_data.get("relevant_entity_ids", ()) or ()
        if isinstance(configured, str):
            configured = (configured,)
        values = list(configured)
        if self.candidate_action is not None:
            values.extend(self.candidate_action.entity_ids)
        return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))

    def to_dict(self) -> Dict[str, Any]:
        return _copy_context(as_versioned_dict(self))


__all__ = ["PromptContext"]
