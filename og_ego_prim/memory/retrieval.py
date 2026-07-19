"""Registry-backed memory retrieval extension point."""

from __future__ import annotations

from typing import Any

from og_ego_prim.domain import Registry

from .core import ExactMemoryRetriever, MemoryRetriever


MEMORY_RETRIEVERS: Registry[Any] = Registry()
MEMORY_RETRIEVERS.register("exact", ExactMemoryRetriever)


def create_memory_retriever(name: str = "exact", **options: Any) -> MemoryRetriever:
    factory = MEMORY_RETRIEVERS.require(name)
    return factory(**options)


__all__ = ["MEMORY_RETRIEVERS", "MemoryRetriever", "create_memory_retriever"]
