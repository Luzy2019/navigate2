"""Dependency-injection protocols that do not depend on simulator packages."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .contracts import Action


class PerceptionProvider(Protocol):
    name: str

    def reset(self, env: Any) -> Any:
        ...

    def observe(self, context: Optional[Any] = None) -> Any:
        ...

    def state_changes(
        self,
        snapshot: Any,
        *,
        subtask_id: Optional[str] = None,
    ) -> Any:
        ...


class ActionExecutor(Protocol):
    def execute(self, action: Action) -> Any:
        ...


__all__ = ["ActionExecutor", "PerceptionProvider"]
