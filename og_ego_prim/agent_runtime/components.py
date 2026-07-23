"""Dependency container for the modular agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeComponents:
    perception: Any
    objects: Any
    scheduler: Any
    planner: Any = None
    prompt_builder: Any = None
    executor: Any = None
    evaluator: Any = None
    event_sink: Any = None
    risk_predictor: Any = None


__all__ = ["RuntimeComponents"]
