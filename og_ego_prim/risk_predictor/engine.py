"""Runtime risk evaluation and active-hazard maintenance."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from og_ego_prim.domain import Action, ActionDecision

from .models import (
    Caution,
    Hazard,
    HazardDraft,
    HazardLevel,
    RiskContext,
    RiskEvaluation,
    normalize_drafts,
)
from .providers import RiskProvider, ensure_risk_provider


def _normalized_identifier(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalized_trigger(value: Optional[str]) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _hazard_key(draft: HazardDraft, action: Optional[Action] = None) -> str:
    entity_ids = set(draft.source_entities) | set(draft.affected_entities)
    if action is not None:
        entity_ids.update(action.entity_ids)
    payload = {
        "rule_id": _normalized_identifier(draft.rule_id),
        "entity_ids": sorted(_normalized_identifier(item) for item in entity_ids if item),
        "trigger": _normalized_trigger(draft.trigger_action or (action.name if action else None)),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    prefix = re.sub(r"[^a-z0-9._-]+", "-", payload["rule_id"]).strip("-") or "hazard"
    return f"{prefix}:{digest}"


def create_hazard(value: Any, *, action: Optional[Action] = None) -> Hazard:
    """Validate a provider draft and assign its deterministic runtime key."""
    if isinstance(value, Hazard):
        draft = HazardDraft(
            rule_id=value.rule_id,
            hazard_type=value.hazard_type,
            hazard_level=value.hazard_level,
            name=value.name,
            source_entities=value.source_entities,
            affected_entities=value.affected_entities,
            trigger_action=value.trigger_action,
            causal_edges=value.causal_edges,
            countermeasures=value.countermeasures,
            cautions=value.cautions,
            evidence_refs=value.evidence_refs,
            confidence=value.confidence,
            source=value.source,
            schema_version=value.schema_version,
            extensions=value.extensions,
        )
    elif isinstance(value, HazardDraft):
        draft = value
    elif isinstance(value, Mapping):
        draft = HazardDraft.from_mapping(value)
    else:
        raise TypeError("create_hazard expects a HazardDraft, Hazard, or mapping")
    key = _hazard_key(draft, action)
    cautions = tuple(
        caution
        if caution.hazard_key == key
        else replace(caution, hazard_key=key)
        for caution in draft.cautions
    )
    return Hazard(
        rule_id=draft.rule_id,
        hazard_type=draft.hazard_type,
        hazard_level=draft.hazard_level,
        name=draft.name,
        source_entities=draft.source_entities,
        affected_entities=draft.affected_entities,
        trigger_action=draft.trigger_action,
        causal_edges=draft.causal_edges,
        countermeasures=draft.countermeasures,
        cautions=cautions,
        evidence_refs=draft.evidence_refs,
        confidence=draft.confidence,
        source=draft.source,
        schema_version=draft.schema_version,
        extensions=draft.extensions,
        hazard_key=key,
    )


def _merge_unique(left: Iterable[Any], right: Iterable[Any], key) -> Tuple[Any, ...]:
    result = []
    seen = set()
    for item in tuple(left) + tuple(right):
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return tuple(result)


def _merge_hazards(left: Hazard, right: Hazard) -> Hazard:
    if right.hazard_level.rank > left.hazard_level.rank or (
        right.hazard_level.rank == left.hazard_level.rank
        and right.confidence > left.confidence
    ):
        primary, secondary = right, left
    else:
        primary, secondary = left, right
    return replace(
        primary,
        source_entities=tuple(sorted(set(primary.source_entities + secondary.source_entities))),
        affected_entities=tuple(sorted(set(primary.affected_entities + secondary.affected_entities))),
        causal_edges=_merge_unique(
            primary.causal_edges,
            secondary.causal_edges,
            lambda item: (item.source_ref, item.relation, item.target_ref, item.causal_level.value),
        ),
        countermeasures=_merge_unique(
            primary.countermeasures,
            secondary.countermeasures,
            lambda item: (item.action_template, item.required_order),
        ),
        cautions=_merge_unique(
            primary.cautions,
            secondary.cautions,
            lambda item: (item.kind, item.text),
        ),
        evidence_refs=tuple(sorted(set(primary.evidence_refs + secondary.evidence_refs))),
        extensions={**secondary.extensions, **primary.extensions},
    )


def decision_for_hazards(hazards: Iterable[Hazard]) -> ActionDecision:
    levels = tuple(hazard.hazard_level for hazard in hazards)
    if not levels:
        return ActionDecision.ALLOW
    maximum = max(levels, key=lambda level: level.rank)
    if maximum.rank >= HazardLevel.HIGH.rank:
        return ActionDecision.BLOCK
    return ActionDecision.CAUTION


def _deduplicate_cautions(values: Iterable[Caution]) -> Tuple[Caution, ...]:
    result = []
    seen = set()
    for caution in values:
        marker = (caution.kind, caution.text)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(caution)
    return tuple(result)


class RiskEngine:
    """Recompute current hazards for every candidate action."""

    MODES = frozenset({"audit", "enforce", "disabled"})

    def __init__(self, provider: RiskProvider, *, mode: str = "enforce") -> None:
        self.provider = ensure_risk_provider(provider)
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in self.MODES:
            raise ValueError(f"risk mode must be one of {', '.join(sorted(self.MODES))}")
        self.mode = normalized_mode
        self._active_hazards: Dict[str, Hazard] = {}

    @property
    def active_hazards(self) -> Tuple[Hazard, ...]:
        return tuple(self._active_hazards[key] for key in sorted(self._active_hazards))

    def clear(self) -> None:
        self._active_hazards.clear()

    def refresh(
        self,
        context: Any = None,
        *,
        action: Optional[Action] = None,
    ) -> RiskEvaluation:
        current = RiskContext.from_value(context, action=action)
        if self.mode == "disabled":
            self.clear()
            return RiskEvaluation(
                decision=ActionDecision.ALLOW,
                action=current.action,
                extensions={
                    "risk_mode": self.mode,
                    "recommended_decision": ActionDecision.ALLOW.value,
                },
            )
        active: Dict[str, Hazard] = {}
        for draft in normalize_drafts(tuple(self.provider.assess(current) or ())):
            hazard = create_hazard(draft, action=current.action)
            existing = active.get(hazard.hazard_key)
            active[hazard.hazard_key] = (
                hazard if existing is None else _merge_hazards(existing, hazard)
            )
        self._active_hazards = active
        hazards = self.active_hazards
        recommended = decision_for_hazards(hazards)
        decision = recommended if self.mode == "enforce" else ActionDecision.ALLOW
        common = _deduplicate_cautions(
            caution
            for hazard in hazards
            for caution in hazard.cautions
            if caution.kind == "common"
        )
        specific = _deduplicate_cautions(
            caution
            for hazard in hazards
            for caution in hazard.cautions
            if caution.kind == "specific"
        )
        reason = None
        if recommended == ActionDecision.BLOCK:
            messages = [item.text for item in specific + common]
            if not messages:
                messages = [hazard.name or hazard.hazard_type for hazard in hazards]
            reason = "; ".join(dict.fromkeys(messages)) or "candidate action has a high safety risk"
        return RiskEvaluation(
            decision=decision,
            hazards=hazards,
            common_cautions=common,
            specific_cautions=specific,
            action=current.action,
            rethinking_reason=reason,
            extensions={
                "risk_mode": self.mode,
                "recommended_decision": recommended.value,
            },
        )

    def evaluate(self, action: Action, context: Any = None) -> RiskEvaluation:
        if not isinstance(action, Action):
            raise TypeError("RiskEngine.evaluate requires an og_ego_prim.domain.Action")
        return self.refresh(context, action=action)


__all__ = [
    "RiskEngine",
    "create_hazard",
    "decision_for_hazards",
]
