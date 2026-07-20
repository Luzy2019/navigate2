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
from .providers import RiskProvider

# 给hazard生成一个唯一的key，便于在后续的处理过程中进行识别和管理
def _hazard_key(draft: HazardDraft, action: Optional[Action] = None) -> str:
    '''
    Deterministically generate a unique key for a hazard draft.

    Example:
        draft.rule_id = "wet_floor"
        draft.source_entities = ("floor", "sink")
        draft.affected_entities = ("robot",)
        draft.trigger_action = "PICK_UP"
        action.entity_ids = ("robot", "mop", "floor")
    Output:
        "wet-floor:2c30d918b215ca31
    '''
    entity_ids = set(draft.source_entities) | set(draft.affected_entities)
    if action is not None:
        entity_ids.update(action.entity_ids)
    payload = {
        "rule_id": draft.rule_id,
        "entity_ids": sorted(item for item in entity_ids if item),
        "trigger": draft.trigger_action or (action.name if action else None),
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
            cautions=value.cautions,
            confidence=value.confidence,
            source=value.source,
        )
    elif isinstance(value, HazardDraft):
        draft = value
    else:
        raise TypeError("create_hazard expects a HazardDraft or Hazard")

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
        cautions=cautions,
        confidence=draft.confidence,
        source=draft.source,
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
        cautions=_merge_unique(
            primary.cautions,
            secondary.cautions,
            lambda item: (item.kind, item.text),
        ),
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
        self.provider = provider
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
