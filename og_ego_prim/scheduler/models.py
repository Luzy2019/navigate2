from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from og_ego_prim.utils.serialization import to_builtin


SCHEMA_VERSION = "isbench.scheduler.v1"


def normalize_action_name(value: Any) -> str:
    """Return the stable action identifier used by temporal handlers."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)", text)
    return (match.group(1) if match else text).upper()


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    for name in ("entity_id", "object_id", "name"):
        candidate = _read(value, name)
        if candidate is not None:
            return str(candidate)
    return str(value)


def _string_tuple(values: Any) -> Tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    result = []
    for value in values:
        identifier = _identifier(value)
        if identifier and identifier not in result:
            result.append(identifier)
    return tuple(result)


class ProcessStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TemporalEvent:
    """Normalized successful action event consumed by process handlers.

    ``from_action`` deliberately accepts mappings and typed domain actions. This
    keeps the scheduler independent from planner and event-storage packages.
    """

    action_name: str
    step: int
    event_id: Optional[str] = None
    action_id: Optional[str] = None
    actor_id: Optional[str] = None
    object_id: Optional[str] = None
    target_id: Optional[str] = None
    entity_ids: Tuple[str, ...] = ()
    parameters: Any = field(default_factory=dict)
    success: bool = True
    attributes: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action_name = normalize_action_name(self.action_name)
        self.step = int(self.step)
        self.entity_ids = _string_tuple(self.entity_ids)
        self.attributes = dict(self.attributes or {})
        self.extensions = dict(self.extensions or {})

    @classmethod
    def from_action(
        cls,
        action: Any,
        *,
        step: int,
        event_id: Optional[str] = None,
        action_id: Optional[str] = None,
        success: Optional[bool] = None,
        attributes: Optional[Mapping[str, Any]] = None,
        extensions: Optional[Mapping[str, Any]] = None,
    ) -> "TemporalEvent":
        nested_action = _read(action, "action")
        raw_action = action if isinstance(action, str) else nested_action if isinstance(nested_action, str) else None
        parsed_action = None
        if raw_action:
            try:
                from og_ego_prim.domain import Action

                parsed_action = Action.from_raw(raw_action)
            except (TypeError, ValueError):
                # Extension actions may intentionally use a bare registered
                # name. Their name still normalizes below; callers can provide
                # entity IDs through the event envelope.
                parsed_action = None
        source = (
            parsed_action
            or (nested_action if nested_action is not None and not isinstance(nested_action, str) else action)
        )

        raw_name = (
            _read(source, "name")
            or _read(source, "action_name")
            or _read(action, "action_name")
            or raw_action
            or _read(source, "raw")
        )
        actor_id = _identifier(_read(source, "actor_id") or _read(action, "actor_id"))
        object_id = _identifier(_read(source, "object_id") or _read(action, "object_id"))
        target_id = _identifier(_read(source, "target_id") or _read(action, "target_id"))

        explicit_entities = _read(action, "entity_ids") or _read(source, "entity_ids")
        entity_ids = list(_string_tuple(explicit_entities))
        for identifier in (object_id, target_id):
            if identifier and identifier not in entity_ids:
                entity_ids.append(identifier)

        parameters = _read(source, "parameters", _read(action, "parameters", {}))
        if not entity_ids:
            positional = (
                parameters.get("arguments", tuple(parameters.values()))
                if isinstance(parameters, Mapping)
                else parameters
            )
            if isinstance(positional, Sequence) and not isinstance(positional, (str, bytes)):
                entity_ids.extend(_string_tuple(positional))

        source_success = _read(action, "success", _read(action, "succeeded", True))
        source_schema_version = _read(action, "schema_version")
        event_extensions = (
            dict(_read(source, "extensions", {}) or {})
            | dict(_read(action, "extensions", {}) or {})
            | dict(extensions or {})
        )
        if source_schema_version and source_schema_version != SCHEMA_VERSION:
            event_extensions.setdefault("source_schema_version", str(source_schema_version))
        return cls(
            action_name=normalize_action_name(raw_name),
            step=int(_read(action, "step", step)),
            event_id=event_id or _identifier(_read(action, "event_id")),
            action_id=action_id or _identifier(_read(action, "action_id") or _read(source, "action_id")),
            actor_id=actor_id,
            object_id=object_id,
            target_id=target_id,
            entity_ids=tuple(entity_ids),
            parameters=parameters,
            success=bool(source_success if success is None else success),
            attributes=dict(_read(action, "attributes", {}) or {}) | dict(attributes or {}),
            schema_version=SCHEMA_VERSION,
            extensions=event_extensions,
        )

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin({
            "schema_version": self.schema_version,
            "action_name": self.action_name,
            "step": self.step,
            "event_id": self.event_id,
            "action_id": self.action_id,
            "actor_id": self.actor_id,
            "object_id": self.object_id,
            "target_id": self.target_id,
            "entity_ids": list(self.entity_ids),
            "parameters": self.parameters,
            "success": self.success,
            "attributes": dict(self.attributes),
            "extensions": dict(self.extensions),
        })


def make_process_id(
    process_type: str,
    entity_ids: Iterable[str],
    *,
    instance_key: Optional[str] = None,
) -> str:
    identity = "|".join(
        (str(process_type).strip().lower(), *sorted(_string_tuple(entity_ids)), instance_key or "")
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{str(process_type).strip().lower()}:{digest}"


@dataclass
class ScheduledProcess:
    process_id: str
    process_type: str
    entity_ids: Tuple[str, ...]
    start_step: int
    ready_step: Optional[int]
    source_action_id: Optional[str] = None
    readiness_predicate: Optional[str] = None
    blocking_actions: Tuple[str, ...] = ()
    completion_effects: Dict[str, Any] = field(default_factory=dict)
    status: ProcessStatus = ProcessStatus.PENDING
    schema_version: str = SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.process_type = str(self.process_type).strip().lower()
        self.entity_ids = _string_tuple(self.entity_ids)
        self.start_step = int(self.start_step)
        self.ready_step = None if self.ready_step is None else int(self.ready_step)
        self.blocking_actions = tuple(normalize_action_name(value) for value in self.blocking_actions)
        self.completion_effects = dict(self.completion_effects or {})
        self.status = ProcessStatus(self.status)
        self.extensions = dict(self.extensions or {})

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin({
            "schema_version": self.schema_version,
            "process_id": self.process_id,
            "process_type": self.process_type,
            "entity_ids": list(self.entity_ids),
            "source_action_id": self.source_action_id,
            "start_step": self.start_step,
            "ready_step": self.ready_step,
            "readiness_predicate": self.readiness_predicate,
            "blocking_actions": list(self.blocking_actions),
            "completion_effects": dict(self.completion_effects),
            "status": self.status.value,
            "extensions": dict(self.extensions),
        })

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScheduledProcess":
        return cls(
            process_id=str(value["process_id"]),
            process_type=str(value["process_type"]),
            entity_ids=_string_tuple(value.get("entity_ids")),
            source_action_id=_identifier(value.get("source_action_id")),
            start_step=int(value["start_step"]),
            ready_step=None if value.get("ready_step") is None else int(value["ready_step"]),
            readiness_predicate=value.get("readiness_predicate"),
            blocking_actions=_string_tuple(value.get("blocking_actions")),
            completion_effects=dict(value.get("completion_effects") or {}),
            status=ProcessStatus(value.get("status", ProcessStatus.PENDING.value)),
            schema_version=str(value.get("schema_version", SCHEMA_VERSION)),
            extensions=dict(value.get("extensions") or {}),
        )


@dataclass
class ProcessUpdate:
    process_id: str
    process_type: str
    status: ProcessStatus
    step: int
    entity_ids: Tuple[str, ...] = ()
    reason: Optional[str] = None
    state_effects: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = ProcessStatus(self.status)
        self.step = int(self.step)
        self.entity_ids = _string_tuple(self.entity_ids)
        self.state_effects = dict(self.state_effects or {})
        self.extensions = dict(self.extensions or {})

    @property
    def terminal(self) -> bool:
        return self.status in {
            ProcessStatus.READY,
            ProcessStatus.CANCELLED,
            ProcessStatus.FAILED,
        }

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin({
            "schema_version": self.schema_version,
            "process_id": self.process_id,
            "process_type": self.process_type,
            "status": self.status.value,
            "step": self.step,
            "entity_ids": list(self.entity_ids),
            "reason": self.reason,
            "state_effects": dict(self.state_effects),
            "extensions": dict(self.extensions),
        })


@dataclass
class TemporalGate:
    action_name: str
    decision: str
    step: int
    blocking_process_ids: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()
    retry_at_step: Optional[int] = None
    schema_version: str = SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action_name = normalize_action_name(self.action_name)
        self.decision = str(self.decision).upper()
        if self.decision not in {"ALLOW", "BLOCK"}:
            raise ValueError("temporal gate decision must be ALLOW or BLOCK")
        self.step = int(self.step)
        self.blocking_process_ids = _string_tuple(self.blocking_process_ids)
        self.reasons = tuple(str(reason) for reason in self.reasons)
        self.retry_at_step = None if self.retry_at_step is None else int(self.retry_at_step)
        self.extensions = dict(self.extensions or {})

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin({
            "schema_version": self.schema_version,
            "action_name": self.action_name,
            "decision": self.decision,
            "allowed": self.allowed,
            "step": self.step,
            "blocking_process_ids": list(self.blocking_process_ids),
            "reasons": list(self.reasons),
            "retry_at_step": self.retry_at_step,
            "extensions": dict(self.extensions),
        })
