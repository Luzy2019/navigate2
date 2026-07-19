"""Configurable object lifecycle transitions without category-specific branches."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from og_ego_prim.domain import Action, ActionRecord
from og_ego_prim.utils.serialization import as_versioned_dict

from .models import ObjectRecord


@dataclass(frozen=True)
class LifecycleDirective:
    """A module-neutral side effect requested by a lifecycle rule."""

    directive_type: str
    options: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "isbench.lifecycle_directive.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        directive_type = str(self.directive_type or "").strip().lower()
        if not directive_type:
            raise ValueError("lifecycle directive_type must not be empty")
        object.__setattr__(self, "directive_type", directive_type)
        object.__setattr__(self, "options", dict(self.options or {}))
        object.__setattr__(self, "schema_version", str(self.schema_version or "").strip())
        object.__setattr__(self, "extensions", dict(self.extensions or {}))

    @classmethod
    def from_value(cls, value: Any) -> "LifecycleDirective":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(directive_type=value)
        if not isinstance(value, Mapping):
            raise TypeError("lifecycle directives must be strings, mappings, or directives")
        payload = dict(value)
        directive_type = payload.pop("directive_type", None)
        if directive_type is None:
            directive_type = payload.pop("type", None)
        if directive_type is None:
            directive_type = payload.pop("name", "")
        known = {"options", "schema_version", "extensions"}
        options = dict(payload.pop("options", {}) or {})
        options.update({key: item for key, item in payload.items() if key not in known})
        return cls(
            directive_type=str(directive_type),
            options=options,
            schema_version=str(
                payload.get("schema_version", "isbench.lifecycle_directive.v1")
            ),
            extensions=dict(payload.get("extensions") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class LifecycleTransition:
    available: Optional[bool] = None
    clear_location: bool = False
    state_updates: Mapping[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    directives: Tuple[LifecycleDirective, ...] = ()
    schema_version: str = "isbench.lifecycle_transition.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.available is not None:
            object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "clear_location", bool(self.clear_location))
        object.__setattr__(self, "state_updates", dict(self.state_updates or {}))
        object.__setattr__(
            self,
            "reason",
            None if self.reason is None else (str(self.reason).strip() or None),
        )
        directives = self.directives
        if isinstance(directives, (str, Mapping, LifecycleDirective)):
            directives = (directives,)
        object.__setattr__(
            self,
            "directives",
            tuple(LifecycleDirective.from_value(value) for value in directives or ()),
        )
        object.__setattr__(self, "schema_version", str(self.schema_version or "").strip())
        object.__setattr__(self, "extensions", dict(self.extensions or {}))

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class LifecycleRule:
    rule_id: str
    conditions: Mapping[str, Any]
    transition: LifecycleTransition
    priority: int = 0
    schema_version: str = "isbench.lifecycle_rule.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        rule_id = str(self.rule_id or "").strip()
        if not rule_id:
            raise ValueError("lifecycle rule_id must not be empty")
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "conditions", dict(self.conditions or {}))
        if not isinstance(self.transition, LifecycleTransition):
            if not isinstance(self.transition, Mapping):
                raise TypeError("lifecycle rule transition must be a transition or mapping")
            object.__setattr__(self, "transition", LifecycleTransition(**dict(self.transition)))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "schema_version", str(self.schema_version or "").strip())
        object.__setattr__(self, "extensions", dict(self.extensions or {}))

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


@dataclass(frozen=True)
class LifecycleContext:
    record: ActionRecord
    subject: ObjectRecord
    target: Optional[ObjectRecord] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def action(self) -> Action:
        return self.record.action


class EntityLifecyclePolicy(Protocol):
    def evaluate(self, context: LifecycleContext) -> Sequence[LifecycleTransition]:
        ...


class NullEntityLifecyclePolicy:
    def evaluate(self, context: LifecycleContext) -> Sequence[LifecycleTransition]:
        return ()


def _read_path(context: LifecycleContext, path: str) -> Any:
    segments = str(path).split(".")
    roots = {
        "action": context.action,
        "record": context.record,
        "subject": context.subject,
        "target": context.target,
        "metadata": context.metadata,
    }
    if not segments or segments[0] not in roots:
        raise ValueError(f"unsupported lifecycle condition path: {path!r}")
    current: Any = roots[segments[0]]
    for segment in segments[1:]:
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(segment)
        else:
            current = getattr(current, segment, None)
    return current


def _matches(actual: Any, expected: Any) -> bool:
    if callable(expected):
        return bool(expected(actual))
    if isinstance(actual, (tuple, list, set, frozenset)) and isinstance(
        expected, (tuple, list, set, frozenset)
    ):
        return all(value in actual for value in expected)
    if isinstance(expected, (tuple, list, set, frozenset)):
        return actual in expected
    if isinstance(actual, (tuple, list, set, frozenset)):
        return expected in actual
    return actual == expected


class RuleBasedLifecyclePolicy:
    """Match declarative dotted paths and return configured transitions."""

    def __init__(self, rules: Iterable[LifecycleRule] = ()) -> None:
        configured_rules = tuple(rules)
        for rule in configured_rules:
            if not str(rule.rule_id).strip():
                raise ValueError("lifecycle rule_id must not be empty")
            for path in rule.conditions:
                if str(path).split(".", 1)[0] not in {
                    "action",
                    "record",
                    "subject",
                    "target",
                    "metadata",
                }:
                    raise ValueError(f"unsupported lifecycle condition path: {path!r}")
        self._rules = tuple(
            sorted(configured_rules, key=lambda rule: (-int(rule.priority), rule.rule_id))
        )

    def evaluate(self, context: LifecycleContext) -> Sequence[LifecycleTransition]:
        transitions = []
        for rule in self._rules:
            if all(
                _matches(_read_path(context, path), expected)
                for path, expected in rule.conditions.items()
            ):
                transition = rule.transition
                if transition.reason is None:
                    transition = LifecycleTransition(
                        available=transition.available,
                        clear_location=transition.clear_location,
                        state_updates=transition.state_updates,
                        reason=rule.rule_id,
                        directives=transition.directives,
                        schema_version=transition.schema_version,
                        extensions=transition.extensions,
                    )
                transitions.append(transition)
        return tuple(transitions)


__all__ = [
    "EntityLifecyclePolicy",
    "LifecycleContext",
    "LifecycleDirective",
    "LifecycleRule",
    "LifecycleTransition",
    "NullEntityLifecyclePolicy",
    "RuleBasedLifecyclePolicy",
]
