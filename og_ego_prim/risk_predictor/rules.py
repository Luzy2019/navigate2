"""In-memory task safety rules used by the runtime risk provider.

There is intentionally no filesystem-backed rule catalog here.  A benchmark
task supplies its own safety items; this module only normalizes those items
into runtime objects.  In particular, evaluator BDDL is never copied into a
``RiskRule`` or ``HazardDraft``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from .models import (
    Caution,
    HazardDraft,
    HazardLevel,
    RiskContext,
)

RISK_SCHEMA_VERSION = "isbench.risk_rule.v1"


RULE_CATALOG_SCHEMA_VERSION = "isbench.task_safety_rules.v1"


def _strings(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item).strip() for item in value if str(item).strip())

def _action_parts(value: Any) -> Tuple[Optional[str], Tuple[str, ...]]:
    '''
        _action_parts("PLACE_INSIDE(apple, cabinet)")
        # ("PLACE_INSIDE", ("apple", "cabinet"))
    '''
    if value is None:
        return None, ()
    text = str(value).strip()
    if not text:
        return None, ()
    if text.lower() in {"completion", "complete", "done"}:
        return "DONE", ()
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?\s*", text)
    if match is None:
        return text.upper(), ()
    operator = match.group(1).upper()
    raw_args = (match.group(2) or "").strip()
    args = tuple(item.strip().lower() for item in raw_args.split(",") if item.strip())
    return operator, args


def _action_from_context(context: RiskContext) -> Tuple[Optional[str], Tuple[str, ...]]:
    '''
        context = RiskContext(
            action=Action(
                name="PLACE_INSIDE",
                object_id="apple",
                target_id="cabinet",
            )
        )
        _action_from_context(context)
        # ("PLACE_INSIDE", ("apple", "cabinet"))
    '''
    action = context.action
    if action is None:
        return None, ()
    name = str(getattr(action, "name", "")).upper() or None
    args = tuple(str(item).strip().lower() for item in getattr(action, "arguments", ()) or ())
    if not args:
        args = tuple(
            str(item).strip().lower()
            for item in (getattr(action, "object_id", None), getattr(action, "target_id", None))
            if item
        )
    return name, args


def _item_id(item: Mapping[str, Any], index: int) -> str:
    """Build a short, deterministic ID from the complete rule identity."""
    rule_id = str(item.get("rule_id") or "").strip().lower()
    hazard_id = str(item.get("hazard_id") or item.get("id") or "").strip().lower()
    risk_type = str(
        item.get("risk_type")
        or item.get("hazard_type")
        or item.get("type")
        or ""
    ).strip().lower()
    action = item.get("action") or item.get("trigger_action")
    normalized_action = ""
    if action:
        operator, arguments = _action_parts(action)
        normalized_action = operator or ""
        if arguments:
            normalized_action += "(" + ",".join(arguments) + ")"
        normalized_action = normalized_action.lower()
    identity = {
        "rule_id": rule_id,
        "hazard_id": hazard_id,
        "risk_type": risk_type,
        "action": normalized_action,
    }
    if not any(identity.values()):
        return f"safety-{index}"
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]
    label = next(value for value in identity.values() if value)
    prefix = re.sub(r"[^a-z0-9]+", "-", label).strip("-")[:24] or "safety"
    return f"{prefix}-{digest}"


def _item_level(item: Mapping[str, Any]) -> HazardLevel:
    # Severity is optional task metadata.  It is not a decision and therefore
    # does not prescribe BLOCK/ALLOW in the task file.
    return HazardLevel.coerce(item.get("hazard_level") or item.get("severity") or item.get("level") or "MEDIUM")


@dataclass
class RuleCondition:
    """Optional live-context predicate retained for provider extensions."""

    source: str
    path: str
    operator: str = "eq"
    value: Any = True
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuleCondition":
        return cls(
            source=str(value.get("source", "context")),
            path=str(value.get("path", "")),
            operator=str(value.get("operator", "eq")),
            value=value.get("value", True),
            schema_version=value.get("schema_version", RISK_SCHEMA_VERSION),
            extensions=dict(value.get("extensions") or {}),
        )

    def matches(self, context: RiskContext) -> bool:
        current: Any = context
        path = self.path.strip()
        if self.source and self.source not in {"context", ""}:
            current = getattr(context, self.source, None)
        for part in path.split(".") if path else ():
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
        operator = self.operator.strip().lower()
        if operator in {"eq", "=="}:
            return current == self.value
        if operator in {"neq", "!="}:
            return current != self.value
        if operator == "in":
            try:
                return current in self.value
            except TypeError:
                return False
        if operator == "contains":
            try:
                return self.value in current
            except TypeError:
                return False
        if operator in {"truthy", "exists"}:
            return bool(current)
        if operator == "falsy":
            return not bool(current)
        raise ValueError(f"unsupported risk rule condition operator {self.operator!r}")


@dataclass
class RiskRule:
    rule_id: str
    hazard_type: str
    hazard_level: HazardLevel = HazardLevel.MEDIUM
    name: Optional[str] = None
    action_names: Tuple[str, ...] = ()
    entity_ids: Tuple[str, ...] = ()
    conditions: Tuple[RuleCondition, ...] = ()
    source_entities: Tuple[str, ...] = ()
    affected_entities: Tuple[str, ...] = ()
    trigger_action: Optional[str] = None
    common_cautions: Tuple[Caution, ...] = ()
    specific_cautions: Tuple[Caution, ...] = ()
    confidence: float = 1.0
    enabled: bool = True
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rule_id = str(self.rule_id).strip()
        self.hazard_type = str(self.hazard_type).strip()
        if not self.rule_id or not self.hazard_type:
            raise ValueError("risk rule rule_id and hazard_type are required")
        self.hazard_level = HazardLevel.coerce(self.hazard_level)
        self.action_names = tuple(str(item).strip().upper() for item in _strings(self.action_names))
        self.entity_ids = tuple(str(item).strip().lower() for item in _strings(self.entity_ids))
        self.conditions = tuple(
            item if isinstance(item, RuleCondition) else RuleCondition.from_mapping(item)
            for item in (self.conditions or ())
        )
        self.source_entities = _strings(self.source_entities)
        self.affected_entities = _strings(self.affected_entities)
        self.trigger_action = (
            None if self.trigger_action is None else str(self.trigger_action).strip()
        )
        self.common_cautions = tuple(
            item if isinstance(item, Caution) else Caution.from_value(item, default_kind="common")
            for item in (self.common_cautions or ())
        )
        self.specific_cautions = tuple(
            item if isinstance(item, Caution) else Caution.from_value(item, default_kind="specific")
            for item in (self.specific_cautions or ())
        )
        self.confidence = min(max(float(self.confidence), 0.0), 1.0)
        self.enabled = bool(self.enabled)
        self.extensions = dict(self.extensions or {})

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, index: int = 0) -> "RiskRule":
        """Normalize one flat task safety item into a runtime rule.

        ``safety_bddl`` is intentionally ignored.  It belongs to the evaluator
        projection and must never reach a runtime hazard or planner prompt.
        """
        item = dict(value)
        rule_id = _item_id(item, index)
        hazard_type = item.get("hazard_type") or item.get("risk_type") or item.get("type") or "Safety Hazard"
        action = item.get("trigger_action") or item.get("action")
        action_names = item.get("action_names") or (() if not action else (action,))
        entity_ids = item.get("entity_ids") or item.get("source_entities") or ()
        if action:
            _, args = _action_parts(action)
            entity_ids = tuple(entity_ids) + args
        tip = item.get("safety_tip") or item.get("caution") or item.get("message")
        if not tip and item.get("G_safe"):
            candidate_tip = str(item.get("G_safe")).strip()
            # A parenthesized expression is evaluator BDDL, never a runtime
            # message.  Plain-text G_safe is accepted as a caution source.
            if not candidate_tip.startswith("("):
                tip = candidate_tip
        principle = item.get("safety_principle")
        caution_values = item.get("cautions") or ()
        if isinstance(caution_values, (str, Mapping)):
            caution_values = (caution_values,)
        if tip:
            caution_values = tuple(caution_values) + (str(tip),)
        if principle and not tip:
            caution_values = tuple(caution_values) + (str(principle),)
        common = item.get("common_cautions") or ()
        specific = tuple(caution_values)
        subtask = item.get("triggered_during_subtask") or item.get("subtask")
        extensions = dict(item.get("runtime_extensions") or {})
        if subtask is not None:
            extensions["triggered_subtask"] = int(re.search(r"\d+", str(subtask)).group(0)) if re.search(r"\d+", str(subtask)) else subtask
        return cls(
            rule_id=rule_id,
            hazard_type=str(hazard_type),
            hazard_level=_item_level(item),
            name=item.get("name") or item.get("safety_principle"),
            action_names=_strings(action_names),
            entity_ids=_strings(entity_ids),
            conditions=item.get("conditions") or (),
            source_entities=_strings(item.get("source_entities") or entity_ids),
            affected_entities=_strings(item.get("affected_entities") or entity_ids),
            trigger_action=action,
            common_cautions=common,
            specific_cautions=specific,
            confidence=item.get("confidence", 1.0),
            enabled=item.get("enabled", True),
            extensions=extensions,
        )

    def matches(self, context: RiskContext) -> bool:
        if not self.enabled:
            return False
        triggered_subtask = self.extensions.get("triggered_subtask")
        if triggered_subtask is not None:
            if context.active_subtask is None:
                return False
            try:
                if int(triggered_subtask) != int(context.active_subtask):
                    return False
            except (TypeError, ValueError):
                if str(triggered_subtask) != str(context.active_subtask):
                    return False
        if not all(condition.matches(context) for condition in self.conditions):
            return False
        operator, arguments = _action_from_context(context)
        if not self.action_names and not self.entity_ids:
            return True
        action_matches = False
        for configured in self.action_names:
            expected_operator, expected_args = _action_parts(configured)
            if expected_operator is None:
                continue
            if expected_operator == operator and all(arg in arguments for arg in expected_args):
                action_matches = True
                break
        if self.action_names:
            return action_matches
        return bool(self.entity_ids and set(self.entity_ids) & set(arguments))

    def to_draft(self, context: RiskContext) -> HazardDraft:
        cautions = list(self.common_cautions) + list(self.specific_cautions)
        return HazardDraft(
            rule_id=self.rule_id,
            hazard_type=self.hazard_type,
            hazard_level=self.hazard_level,
            name=self.name,
            source_entities=self.source_entities,
            affected_entities=self.affected_entities,
            trigger_action=self.trigger_action,
            cautions=cautions,
            confidence=self.confidence,
            source="task_json",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "hazard_type": self.hazard_type,
            "hazard_level": self.hazard_level.value,
            "name": self.name,
            "action_names": list(self.action_names),
            "entity_ids": list(self.entity_ids),
            "conditions": [condition.__dict__ for condition in self.conditions],
            "source_entities": list(self.source_entities),
            "affected_entities": list(self.affected_entities),
            "trigger_action": self.trigger_action,
            "cautions": [item.to_dict() for item in self.common_cautions + self.specific_cautions],
            "confidence": self.confidence,
            "enabled": self.enabled,
            "schema_version": self.schema_version,
            "extensions": dict(self.extensions),
        }


class RuleCatalog:
    """Small explicit in-memory collection; no implicit global files."""

    def __init__(self, rules: Iterable[RiskRule] = (), *, schema_version: str = RULE_CATALOG_SCHEMA_VERSION, extensions: Optional[Mapping[str, Any]] = None):
        self.schema_version = schema_version
        self.extensions = dict(extensions or {})
        self._rules: Dict[str, RiskRule] = {}
        self.extend(rules)

    def register(self, rule: RiskRule, *, replace: bool = False) -> None:
        if not isinstance(rule, RiskRule):
            raise TypeError("rule catalog entries must be RiskRule instances")
        if rule.rule_id in self._rules and not replace:
            raise ValueError(f"risk rule {rule.rule_id!r} is already registered")
        self._rules[rule.rule_id] = rule

    def extend(self, rules: Iterable[RiskRule], *, replace: bool = False) -> None:
        for rule in rules:
            self.register(rule, replace=replace)

    def get(self, rule_id: str) -> Optional[RiskRule]:
        return self._rules.get(str(rule_id))

    @property
    def rules(self) -> Tuple[RiskRule, ...]:
        return tuple(self._rules.values())

    def __iter__(self) -> Iterator[RiskRule]:
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self._rules)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rules": [rule.to_dict() for rule in self.rules],
            "extensions": dict(self.extensions),
        }


def iter_task_safety_items(task: Any) -> Iterator[Mapping[str, Any]]:
    """Yield validated cues from a runtime task projection.

    This module deliberately does not understand the source task JSON layout.
    ``runtime_config.build_runtime_task_config`` owns that projection boundary.
    """
    if task is None:
        return
    if hasattr(task, "to_dict") and not isinstance(task, Mapping):
        task = task.to_dict()
    if isinstance(task, Mapping):
        values = task.get("safety_cues")
        if values is None and any(
            task.get(key)
            for key in ("risk_type", "hazard_type", "safety_tip", "caution")
        ):
            values = (task,)
    else:
        values = task
    if isinstance(values, Mapping):
        values = (values,)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return

    evaluator_only_keys = {
        "G_task",
        "G_safe",
        "decision",
        "evaluation_cautions",
        "evaluation_goal_conditions",
        "safety_bddl",
    }
    for item in values:
        if not isinstance(item, Mapping):
            continue
        forbidden = evaluator_only_keys & set(item)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"runtime safety cue contains forbidden fields: {names}")
        if not item.get("action") or not item.get("type"):
            raise ValueError("runtime safety cues require action and type")
        yield dict(item)


def build_task_safety_rules(value: Any) -> Tuple[RiskRule, ...]:
    """Compile a runtime task projection into provider-owned rules."""
    if isinstance(value, RiskRule):
        return (value,)
    items = list(iter_task_safety_items(value))
    return tuple(RiskRule.from_mapping(item, index=index) for index, item in enumerate(items))


__all__ = [
    "RULE_CATALOG_SCHEMA_VERSION",
    "RuleCatalog",
    "RuleCondition",
    "RiskRule",
    "build_task_safety_rules",
    "iter_task_safety_items",
]
