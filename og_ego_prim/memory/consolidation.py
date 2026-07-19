"""Structural memory consolidation extension point."""

from __future__ import annotations

from typing import Any

from og_ego_prim.domain import Registry

from .core import DeduplicateConsolidator, MemoryConsolidator


MEMORY_CONSOLIDATORS: Registry[Any] = Registry()
MEMORY_CONSOLIDATORS.register("deduplicate", DeduplicateConsolidator)


def create_memory_consolidator(name: str = "deduplicate", **options: Any) -> MemoryConsolidator:
    factory = MEMORY_CONSOLIDATORS.require(name)
    return factory(**options)


__all__ = [
    "MEMORY_CONSOLIDATORS",
    "MemoryConsolidator",
    "create_memory_consolidator",
]
