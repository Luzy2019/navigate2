"""Typed contracts for the runtime risk layer.

The runtime risk layer consumes the natural-language portion of a task safety
item.  Evaluation-only fields (in particular ``safety_bddl``) are deliberately
not represented by these models, so a runtime object cannot accidentally be
serialized into a planner prompt as an evaluator oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from og_ego_prim.domain import Action, ActionDecision
from og_ego_prim.utils.serialization import to_builtin


def _tuple_of_strings(value: Optional[Iterable[Any]]) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(
        text
        for item in value
        if (text := str(item).strip())
    )


class SerializableRiskModel:
    """Small mixin used by runtime reports and event payloads."""

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin(self)


class HazardLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"

    @classmethod
    def coerce(cls, value: Any) -> "HazardLevel":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().upper()
        aliases = {"MODERATE": cls.MEDIUM, "SEVERE": cls.HIGH}
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(
                f"unknown hazard level {value!r}; expected one of {allowed}"
            ) from exc

    @property
    def rank(self) -> int:
        return list(type(self)).index(self) + 1


@dataclass
class RiskContext(SerializableRiskModel):
    """Live inputs supplied to a risk provider for one candidate action."""

    action: Optional[Action] = None
    scene: Any = None
    objects: Any = None
    memory: Any = None
    scheduler: Any = None
    task: Any = None
    active_subtask: Optional[int] = None

    @classmethod
    def from_value(
        cls,
        value: Any = None,
        *,
        action: Optional[Action] = None,
    ) -> "RiskContext":
        if isinstance(value, cls):
            if action is None or value.action is action:
                return value
            return replace(value, action=action)
        if value is None:
            return cls(action=action)
        if not isinstance(value, Mapping):
            raise TypeError("risk context must be a RiskContext, mapping, or None")
        return cls(
            action=action if action is not None else value.get("action"),
            scene=value.get("scene"),
            objects=value.get("objects"),
            memory=value.get("memory"),
            scheduler=value.get("scheduler"),
            task=value.get("task"),
            active_subtask=value.get("active_subtask"),
        )


@dataclass
class Caution(SerializableRiskModel):
    text: str
    kind: str = "specific"
    source: str = "risk_provider"
    hazard_key: Optional[str] = None

    def __post_init__(self) -> None:
        text = str(self.text).strip()
        if not text:
            raise ValueError("caution text is required")
        kind = str(self.kind).strip().lower() or "specific"
        if kind not in {"common", "specific"}:
            raise ValueError("caution kind must be 'common' or 'specific'")
        self.text = text
        self.kind = kind
        self.source = str(self.source).strip() or "risk_provider"

    @classmethod
    def from_value(cls, value: Any, *, default_kind: str = "specific") -> "Caution":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                text=value.get("text") or value.get("caution") or value.get("message") or "",
                kind=value.get("kind", default_kind),
                source=value.get("source", "risk_provider"),
                hazard_key=value.get("hazard_key"),
            )
        return cls(str(value), default_kind)


@dataclass
class HazardDraft(SerializableRiskModel):
    rule_id: str
    hazard_type: str
    hazard_level: HazardLevel
    name: Optional[str] = None
    source_entities: Tuple[str, ...] = ()
    affected_entities: Tuple[str, ...] = ()
    trigger_action: Optional[str] = None
    cautions: Tuple[Caution, ...] = ()
    confidence: float = 1.0
    source: str = "risk_provider"

    def __post_init__(self) -> None:
        rule_id = str(self.rule_id).strip()
        hazard_type = str(self.hazard_type).strip()
        if not rule_id or not hazard_type:
            raise ValueError("hazard draft rule_id and hazard_type are required")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("hazard confidence must be between 0 and 1")
        cautions = self.cautions
        if isinstance(cautions, (str, Mapping, Caution)):
            cautions = (cautions,)
        self.rule_id = rule_id
        self.hazard_type = hazard_type
        self.hazard_level = HazardLevel.coerce(self.hazard_level)
        self.source_entities = _tuple_of_strings(self.source_entities)
        self.affected_entities = _tuple_of_strings(self.affected_entities)
        self.trigger_action = (
            None if self.trigger_action is None else str(self.trigger_action).strip() or None
        )
        self.cautions = tuple(Caution.from_value(item) for item in (cautions or ()))
        self.confidence = confidence
        self.source = str(self.source).strip() or "risk_provider"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HazardDraft":
        return cls(
            rule_id=value.get("rule_id") or value.get("hazard_id") or "",
            hazard_type=value.get("hazard_type") or value.get("risk_type") or value.get("type") or "",
            hazard_level=value.get("hazard_level") or value.get("level") or HazardLevel.MEDIUM,
            name=value.get("name"),
            source_entities=value.get("source_entities") or value.get("entity_ids") or (),
            affected_entities=value.get("affected_entities") or (),
            trigger_action=value.get("trigger_action") or value.get("action"),
            cautions=value.get("cautions") or value.get("caution") or (),
            confidence=value.get("confidence", 1.0),
            source=value.get("source", "risk_provider"),
        )


@dataclass
class Hazard(SerializableRiskModel):
    """Validated active hazard with a deterministic runtime key."""

    hazard_key: str
    rule_id: str
    hazard_type: str
    hazard_level: HazardLevel
    name: Optional[str] = None
    source_entities: Tuple[str, ...] = ()
    affected_entities: Tuple[str, ...] = ()
    trigger_action: Optional[str] = None
    cautions: Tuple[Caution, ...] = ()
    confidence: float = 1.0
    source: str = "risk_provider"

    def __post_init__(self) -> None:
        key = str(self.hazard_key).strip()
        if not key:
            raise ValueError("hazard_key is required")
        draft = HazardDraft(
            rule_id=self.rule_id,
            hazard_type=self.hazard_type,
            hazard_level=self.hazard_level,
            name=self.name,
            source_entities=self.source_entities,
            affected_entities=self.affected_entities,
            trigger_action=self.trigger_action,
            cautions=self.cautions,
            confidence=self.confidence,
            source=self.source,
        )
        self.hazard_key = key
        for field_name in (
            "rule_id",
            "hazard_type",
            "hazard_level",
            "name",
            "source_entities",
            "affected_entities",
            "trigger_action",
            "cautions",
            "confidence",
            "source",
        ):
            setattr(self, field_name, getattr(draft, field_name))


@dataclass
class RiskEvaluation(SerializableRiskModel):
    decision: ActionDecision
    hazards: Tuple[Hazard, ...] = ()
    common_cautions: Tuple[Caution, ...] = ()
    specific_cautions: Tuple[Caution, ...] = ()
    action: Optional[Action] = None
    rethinking_reason: Optional[str] = None

    def __post_init__(self) -> None:

        if not isinstance(self.decision, ActionDecision):
            self.decision = ActionDecision(str(self.decision).strip().upper())

        self.hazards = tuple(self.hazards or ())
        self.common_cautions = tuple(
            Caution.from_value(item, default_kind="common")
            for item in (self.common_cautions or ())
        )
        self.specific_cautions = tuple(
            Caution.from_value(item, default_kind="specific")
            for item in (self.specific_cautions or ())
        )
        self.rethinking_reason = (
            None
            if self.rethinking_reason is None
            else str(self.rethinking_reason).strip() or None
        )

    @property
    def allowed(self) -> bool:
        return self.decision in {ActionDecision.ALLOW, ActionDecision.CAUTION}

    @property
    def cautions(self) -> Tuple[Caution, ...]:
        return self.common_cautions + self.specific_cautions


def normalize_drafts(values: Sequence[Any]) -> Tuple[HazardDraft, ...]:
    result = []
    for value in values:
        if isinstance(value, HazardDraft):
            result.append(value)
        elif isinstance(value, Mapping):
            result.append(HazardDraft.from_mapping(value))
        else:
            raise TypeError(
                f"risk provider returned {type(value).__name__}; expected HazardDraft or mapping"
            )
    return tuple(result)


__all__ = [
    "Caution",
    "Hazard",
    "HazardDraft",
    "HazardLevel",
    "RiskContext",
    "RiskEvaluation",
    "SerializableRiskModel",
    "normalize_drafts",
]
