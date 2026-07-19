"""Typed contracts for the runtime risk layer.

The runtime risk layer consumes the natural-language portion of a task safety
item.  Evaluation-only fields (in particular ``safety_bddl``) are deliberately
not represented by these models, so a runtime object cannot accidentally be
serialized into a planner prompt as an evaluator oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from og_ego_prim.domain import Action, ActionDecision
from og_ego_prim.utils.serialization import as_versioned_dict


RISK_SCHEMA_VERSION = "isbench.risk.v1"


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


def _extensions(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return dict(value or {})


class SerializableRiskModel:
    """Small mixin used by runtime reports and event payloads."""

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


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


class CausalLevel(str, Enum):
    WEAK = "WEAK"
    POSSIBLE = "POSSIBLE"
    LIKELY = "LIKELY"
    DIRECT = "DIRECT"

    @classmethod
    def coerce(cls, value: Any) -> "CausalLevel":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().upper()
        try:
            return cls(normalized)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(
                f"unknown causal level {value!r}; expected one of {allowed}"
            ) from exc


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
    extensions: Mapping[str, Any] = field(default_factory=dict)

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
            return cls(
                action=action,
                scene=value.scene,
                objects=value.objects,
                memory=value.memory,
                scheduler=value.scheduler,
                task=value.task,
                active_subtask=value.active_subtask,
                extensions=value.extensions,
            )
        if value is None:
            return cls(action=action)
        if not isinstance(value, Mapping):
            raise TypeError("risk context must be a RiskContext, mapping, or None")
        known = {
            "action",
            "scene",
            "objects",
            "memory",
            "scheduler",
            "task",
            "active_subtask",
            "extensions",
        }
        extensions = dict(value.get("extensions") or {})
        extensions.update({key: item for key, item in value.items() if key not in known})
        return cls(
            action=action if action is not None else value.get("action"),
            scene=value.get("scene"),
            objects=value.get("objects"),
            memory=value.get("memory"),
            scheduler=value.get("scheduler"),
            task=value.get("task"),
            active_subtask=value.get("active_subtask"),
            extensions=extensions,
        )


@dataclass
class CausalEdge(SerializableRiskModel):
    source_ref: str
    relation: str
    target_ref: str
    causal_level: CausalLevel = CausalLevel.POSSIBLE
    confidence: float = 1.0
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = str(self.source_ref).strip()
        target = str(self.target_ref).strip()
        relation = str(self.relation).strip().upper()
        if not source or not target or not relation:
            raise ValueError("causal edge source_ref, relation, and target_ref are required")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("causal edge confidence must be between 0 and 1")
        self.source_ref = source
        self.target_ref = target
        self.relation = relation
        self.causal_level = CausalLevel.coerce(self.causal_level)
        self.confidence = confidence
        self.extensions = _extensions(self.extensions)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CausalEdge":
        return cls(
            source_ref=value.get("source_ref", value.get("source", "")),
            relation=value.get("relation", "causes"),
            target_ref=value.get("target_ref", value.get("target", "")),
            causal_level=value.get("causal_level", value.get("level", CausalLevel.POSSIBLE)),
            confidence=value.get("confidence", 1.0),
            schema_version=value.get("schema_version", RISK_SCHEMA_VERSION),
            extensions=value.get("extensions") or {},
        )


@dataclass
class Countermeasure(SerializableRiskModel):
    action_template: str
    priority: int = 0
    required_order: Tuple[str, ...] = ()
    description: Optional[str] = None
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action_template = str(self.action_template).strip()
        if not action_template:
            raise ValueError("countermeasure action_template is required")
        self.action_template = action_template
        self.priority = int(self.priority)
        self.required_order = _tuple_of_strings(self.required_order)
        self.extensions = _extensions(self.extensions)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Countermeasure":
        return cls(
            action_template=value.get("action_template", value.get("action", "")),
            priority=value.get("priority", 0),
            required_order=value.get("required_order") or (),
            description=value.get("description"),
            schema_version=value.get("schema_version", RISK_SCHEMA_VERSION),
            extensions=value.get("extensions") or {},
        )


@dataclass
class Caution(SerializableRiskModel):
    text: str
    kind: str = "specific"
    source: str = "risk_provider"
    hazard_key: Optional[str] = None
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

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
        self.extensions = _extensions(self.extensions)

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
                schema_version=value.get("schema_version", RISK_SCHEMA_VERSION),
                extensions=value.get("extensions") or {},
            )
        return cls(str(value), default_kind)


def _coerce_edge(value: Any) -> CausalEdge:
    if isinstance(value, CausalEdge):
        return value
    if isinstance(value, Mapping):
        return CausalEdge.from_mapping(value)
    raise TypeError(f"causal edge must be a CausalEdge or mapping, got {type(value).__name__}")


def _coerce_countermeasure(value: Any) -> Countermeasure:
    if isinstance(value, Countermeasure):
        return value
    if isinstance(value, Mapping):
        return Countermeasure.from_mapping(value)
    if isinstance(value, str):
        return Countermeasure(value)
    raise TypeError(
        "countermeasure must be a Countermeasure, mapping, or string, "
        f"got {type(value).__name__}"
    )


@dataclass
class HazardDraft(SerializableRiskModel):
    rule_id: str
    hazard_type: str
    hazard_level: HazardLevel
    name: Optional[str] = None
    source_entities: Tuple[str, ...] = ()
    affected_entities: Tuple[str, ...] = ()
    trigger_action: Optional[str] = None
    causal_edges: Tuple[CausalEdge, ...] = ()
    countermeasures: Tuple[Countermeasure, ...] = ()
    cautions: Tuple[Caution, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    confidence: float = 1.0
    source: str = "risk_provider"
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        rule_id = str(self.rule_id).strip()
        hazard_type = str(self.hazard_type).strip()
        if not rule_id or not hazard_type:
            raise ValueError("hazard draft rule_id and hazard_type are required")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("hazard confidence must be between 0 and 1")
        edges = self.causal_edges
        if isinstance(edges, Mapping):
            edges = (edges,)
        measures = self.countermeasures
        if isinstance(measures, (str, Mapping, Countermeasure)):
            measures = (measures,)
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
        self.causal_edges = tuple(_coerce_edge(item) for item in (edges or ()))
        self.countermeasures = tuple(_coerce_countermeasure(item) for item in (measures or ()))
        self.cautions = tuple(Caution.from_value(item) for item in (cautions or ()))
        self.evidence_refs = _tuple_of_strings(self.evidence_refs)
        self.confidence = confidence
        self.source = str(self.source).strip() or "risk_provider"
        self.extensions = _extensions(self.extensions)

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
            causal_edges=value.get("causal_edges") or value.get("causes") or (),
            countermeasures=value.get("countermeasures") or value.get("mitigations") or (),
            cautions=value.get("cautions") or value.get("caution") or (),
            evidence_refs=value.get("evidence_refs") or (),
            confidence=value.get("confidence", 1.0),
            source=value.get("source", "risk_provider"),
            schema_version=value.get("schema_version", RISK_SCHEMA_VERSION),
            extensions=value.get("extensions") or {},
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
    causal_edges: Tuple[CausalEdge, ...] = ()
    countermeasures: Tuple[Countermeasure, ...] = ()
    cautions: Tuple[Caution, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    confidence: float = 1.0
    source: str = "risk_provider"
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

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
            causal_edges=self.causal_edges,
            countermeasures=self.countermeasures,
            cautions=self.cautions,
            evidence_refs=self.evidence_refs,
            confidence=self.confidence,
            source=self.source,
            schema_version=self.schema_version,
            extensions=self.extensions,
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
            "causal_edges",
            "countermeasures",
            "cautions",
            "evidence_refs",
            "confidence",
            "source",
            "schema_version",
            "extensions",
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
    schema_version: str = RISK_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

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
        self.extensions = _extensions(self.extensions)

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
    "CausalEdge",
    "CausalLevel",
    "Caution",
    "Countermeasure",
    "Hazard",
    "HazardDraft",
    "HazardLevel",
    "RISK_SCHEMA_VERSION",
    "RiskContext",
    "RiskEvaluation",
    "SerializableRiskModel",
    "normalize_drafts",
]
