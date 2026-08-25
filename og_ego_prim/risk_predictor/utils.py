"""Small wiring helpers for model-backed runtime risk assessment."""

from __future__ import annotations

from typing import Any

from .providers import ModelRiskProvider, RiskProvider
from .risk_assessor import RiskAssessor


def install_vlm_risk_provider(benchmark: Any, client: Any) -> RiskProvider:
    """Replace the runtime risk provider with a VLM-only assessor."""

    risk_predictor = benchmark.runtime_controller.components.risk_predictor
    if not benchmark.runtime_config.risk.enabled:
        return risk_predictor.provider
    provider = ModelRiskProvider(
        RiskAssessor(
            client,
            held_object_getter=getattr(
                benchmark,
                "_current_grasped_object_id",
                None,
            ),
        ),
        provider_id="vlm",
    )
    risk_predictor.provider = provider
    benchmark.tracker.runtime_modules["risk_provider"] = type(provider).__name__
    benchmark.tracker.risk_predictor["provider"] = type(provider).__name__
    benchmark.tracker.risk_predictor["task_json_rule_count"] = 0
    return provider


__all__ = ["install_vlm_risk_provider"]
