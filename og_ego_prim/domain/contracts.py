"""Stable data contracts shared by the modular agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from og_ego_prim.utils.serialization import ExtensionMap, as_versioned_dict


def _identifier(value: Optional[Any]) -> Optional[str]:
    '''
        clearn parameter value, if value is None or empty string, return None
    '''
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _confidence(value: float) -> float:
    '''
        normalize confidence value, must be between 0 and 1
    '''
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return normalized


def _split_action_arguments(text: str) -> Tuple[str, ...]:
    '''
        Description:
            Split action arguments from a string, handling quotes, escapes, and nested delimiters.
            Returns a tuple of argument strings.

        Example:
            _split_action_arguments("apple, plate")
            # ("apple", "plate")
    '''
    arguments = []
    current = []
    quote: Optional[str] = None
    escaped = False
    depth = 0
    for character in text:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\" and quote is not None:
            current.append(character)
            escaped = True
            continue
        if quote is not None:
            current.append(character)
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            current.append(character)
            quote = character
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
            if depth < 0:
                raise ValueError("action arguments contain an unmatched closing delimiter")
        if character == "," and depth == 0:
            value = "".join(current).strip()
            if not value:
                raise ValueError("action arguments must not contain empty values")
            arguments.append(value)
            current = []
        else:
            current.append(character)
    if quote is not None or depth != 0:
        raise ValueError("action arguments contain an unterminated quote or delimiter")
    value = "".join(current).strip()
    if value:
        arguments.append(value)
    elif arguments:
        raise ValueError("action arguments must not end with an empty value")
    return tuple(arguments)


def _entity_argument(value: str) -> Tuple[Optional[str], Optional[str]]:
    '''
        Description:
            Parse an entity argument of the form "entity@descriptor" or just "entity".
            Returns a tuple of (entity, descriptor), where either may be None if not present.

        Example:
            _entity_argument("bread@loaf")
            # ("bread", "loaf")
    '''
    token = str(value).strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        token = token[1:-1].strip()
    if not token:
        return None, None
    entity, separator, descriptor = token.partition("@")
    return _identifier(entity), (_identifier(descriptor) if separator else None)


class ActionDecision(str, Enum):
    ALLOW = "ALLOW"
    CAUTION = "CAUTION"
    BLOCK = "BLOCK"
    RETHINKING = "RETHINKING"



@dataclass
class Action:
    """
    A planner action independent of any concrete primitive backend.

    Field roles:
    - ``actor_id``: the agent or robot performing the action.
    - ``object_id``: the entity being manipulated or changed.
    - ``target_id``: the destination, receiver, or reference target of the action.
    
    example:
    
    Action(
        name="CUT",
        actor_id="RobotArm",
        object_id="bread",
        target_id="cutting_board",
        parameters={
            "tool_id": "knife",
            "entity_ids": ["bread", "knife", "cutting_board", "crumbs"],
        },
    )

print(action.entity_ids)
    
    """

    name: str
    actor_id: Optional[str] = None
    object_id: Optional[str] = None
    target_id: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    raw: Optional[str] = None
    schema_version: str = "isbench.action.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip().upper()
        if not self.name:
            raise ValueError("action name must not be empty")
        self.actor_id = _identifier(self.actor_id)
        self.object_id = _identifier(self.object_id)
        self.target_id = _identifier(self.target_id)
        self.parameters = dict(self.parameters or {})
        self.extensions = dict(self.extensions or {})
        if self.raw is not None:
            self.raw = str(self.raw)

    @property
    def entity_ids(self) -> Tuple[str, ...]:
        related = self.parameters.get("entity_ids", ())
        if isinstance(related, str):
            related = (related,)
        elif not isinstance(related, (list, tuple, set, frozenset)):
            related = (related,) if related is not None else ()
        ordered = (
            self.actor_id,
            self.object_id,
            self.target_id,
            _identifier(self.parameters.get("tool_id")),
            *(_identifier(value) for value in related or ()),
        )
        return tuple(dict.fromkeys(value for value in ordered if value is not None))

    @property
    def arguments(self) -> Tuple[str, ...]:
        values = self.parameters.get("arguments")
        if values is None:
            values = tuple(
                value for value in (self.object_id, self.target_id) if value is not None
            )
        if isinstance(values, str):
            return (values,)
        return tuple(str(value) for value in values)

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)

    @classmethod
    def from_raw(
        cls,
        raw: str,
        *,
        actor_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        extensions: Optional[ExtensionMap] = None,
    ) -> "Action":
        """Parse the planner's legacy ``OP(arg, ...)`` syntax without an allowlist."""

        text = str(raw or "").strip()
        text = re.sub(r"^\s*\d+\.\s*", "", text)
        if not text:
            raise ValueError("raw action must not be empty")
        if text.upper() == "DONE":
            name = "DONE"
            arguments: Tuple[str, ...] = ()
        else:
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*", text, re.DOTALL)
            if match is None:
                raise ValueError(f"invalid action syntax: {raw!r}")
            name = match.group(1)
            arguments = _split_action_arguments(match.group(2).strip())

        object_id, object_descriptor = (
            _entity_argument(arguments[0]) if arguments else (None, None)
        )
        target_id, target_descriptor = (
            _entity_argument(arguments[1]) if len(arguments) > 1 else (None, None)
        )
        action_parameters = dict(parameters or {})
        action_parameters.setdefault("arguments", list(arguments))
        if object_descriptor is not None or target_descriptor is not None:
            descriptors = dict(action_parameters.get("descriptors") or {})
            if object_descriptor is not None:
                descriptors["object"] = object_descriptor
            if target_descriptor is not None:
                descriptors["target"] = target_descriptor
            action_parameters["descriptors"] = descriptors
        if len(arguments) > 2:
            action_parameters.setdefault("extra_arguments", list(arguments[2:]))
        return cls(
            name=name,
            actor_id=actor_id,
            object_id=object_id,
            target_id=target_id,
            parameters=action_parameters,
            raw=str(raw),
            extensions=dict(extensions or {}),
        )

    parse = from_raw

    def to_legacy_plan(self, *, lowercase: bool = True) -> str:
        """Render an action for the existing string-based primitive executor."""

        name = self.name.lower() if lowercase else self.name
        return f"{name}({', '.join(self.arguments)})"


@dataclass
class StateChange:
    """One observed or derived semantic property transition."""

    step: int
    entity_id: str
    key: str
    old: Any
    new: Any
    subtask_id: Optional[str] = None
    room_id: Optional[str] = None
    source: str = "perception"
    confidence: float = 1.0

    schema_version: str = "isbench.state_change.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.step = int(self.step)
        self.entity_id = _identifier(self.entity_id) or ""
        self.key = str(self.key or "").strip()
        if not self.entity_id:
            raise ValueError("state change entity_id must not be empty")
        if not self.key:
            raise ValueError("state change key must not be empty")
        self.subtask_id = _identifier(self.subtask_id)
        self.room_id = _identifier(self.room_id)
        self.source = str(self.source or "unknown").strip() or "unknown"
        self.confidence = _confidence(self.confidence)
        self.extensions = dict(self.extensions or {})

    @property
    def old_value(self) -> Any:
        return self.old

    @property
    def new_value(self) -> Any:
        return self.new

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass
class ActionRecord:
    """Execution result used to project successful actions into object/memory views."""

    action: Action
    step: int
    action_id: str = field(default_factory=lambda: str(uuid4()))
    task_id: Optional[str] = None
    subtask_id: Optional[str] = None
    room_id: Optional[str] = None
    succeeded: bool = True
    source: str = "executor"
    count: int = 1
    schema_version: str = "isbench.action_record.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, Action):
            if isinstance(self.action, dict):
                self.action = Action(**self.action)
            else:
                raise TypeError("action record action must be an Action")
        self.step = int(self.step)
        self.action_id = _identifier(self.action_id) or str(uuid4())
        self.task_id = _identifier(self.task_id)
        self.subtask_id = _identifier(self.subtask_id)
        self.room_id = _identifier(self.room_id)
        self.succeeded = bool(self.succeeded)
        self.source = str(self.source or "executor").strip() or "executor"
        self.count = int(self.count)
        if self.count <= 0:
            raise ValueError("action record count must be greater than zero")
        self.extensions = dict(self.extensions or {})

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


__all__ = [
    "Action",
    "ActionDecision",
    "ActionRecord",
    "StateChange",
]
