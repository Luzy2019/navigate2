"""Composable providers for the runtime risk engine."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterable, Protocol, Sequence, Tuple, runtime_checkable

from .models import HazardDraft, RiskContext, normalize_drafts
from .rules import RiskRule, RuleCatalog, build_task_safety_rules


@runtime_checkable
class RiskProvider(Protocol):
    """Extension point for task rules, online models, or hybrid providers."""

    provider_id: str

    def assess(self, context: RiskContext) -> Sequence[HazardDraft]:
        ...


class RuleRiskProvider:
    provider_id = "task_json"

    def __init__(self, rules: Iterable[RiskRule] = ()) -> None:
        self.catalog = rules if isinstance(rules, RuleCatalog) else RuleCatalog(rules)

    @classmethod
    def from_task(cls, task: Any) -> "RuleRiskProvider":
        return cls(build_task_safety_rules(task))

    def assess(self, context: RiskContext) -> Tuple[HazardDraft, ...]:
        return tuple(
            rule.to_draft(context)
            for rule in self.catalog
            if rule.matches(context)
        )


ModelAssessor = Callable[[RiskContext], Sequence[Any]]


class ModelRiskProvider:
    """Adapter for an injected online or local hazard assessor."""

    def __init__(self, assessor: ModelAssessor, *, provider_id: str = "model") -> None:
        if not callable(assessor):
            raise TypeError("model risk assessor must be callable")
        self.assessor = assessor
        self.provider_id = str(provider_id).strip() or "model"

    def assess(self, context: RiskContext) -> Tuple[HazardDraft, ...]:
        drafts = normalize_drafts(tuple(self.assessor(context) or ()))
        return tuple(
            draft
            if draft.source != "risk_provider"
            else replace(draft, source=self.provider_id)
            for draft in drafts
        )


class HybridRiskProvider:
    provider_id = "hybrid"

    def __init__(self, providers: Iterable[RiskProvider]) -> None:
        self.providers = tuple(ensure_risk_provider(provider) for provider in providers)
        if not self.providers:
            raise ValueError("hybrid risk provider requires at least one provider")

    def assess(self, context: RiskContext) -> Tuple[HazardDraft, ...]:
        result = []
        for provider in self.providers:
            provider_id = getattr(provider, "provider_id", type(provider).__name__)
            for draft in normalize_drafts(tuple(provider.assess(context) or ())):
                if draft.source == "risk_provider":
                    draft = replace(draft, source=str(provider_id))
                result.append(draft)
        return tuple(result)


class NullRiskProvider:
    """Explicit no-op provider for disabled runs and ablations."""

    provider_id = "none"

    def assess(self, context: RiskContext) -> Tuple[HazardDraft, ...]:
        return ()


def ensure_risk_provider(value: Any) -> RiskProvider:
    if isinstance(value, RiskProvider) or callable(getattr(value, "assess", None)):
        return value
    if callable(value):
        return ModelRiskProvider(value)
    raise TypeError("risk provider must implement assess(context) or be a callable assessor")


__all__ = [
    "HybridRiskProvider",
    "ModelAssessor",
    "ModelRiskProvider",
    "NullRiskProvider",
    "RiskProvider",
    "RuleRiskProvider",
    "ensure_risk_provider",
]
