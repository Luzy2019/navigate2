"""Deterministic prompt composition over registered semantic sections."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from og_ego_prim.domain import Registry

from .models import PromptContext
from .sections import PromptSectionProvider, default_section_registry


class SemanticPromptBuilder:
    def __init__(
        self,
        *,
        sections: Sequence[str] = ("task", "timers", "action"),
        registry: Optional[Registry[PromptSectionProvider]] = None,
        max_scene_items: int = 20,
    ) -> None:
        self.sections = tuple(str(section) for section in sections)
        self.registry = registry or default_section_registry(max_scene_items=max_scene_items)

    def register(self, name: str, provider: PromptSectionProvider, *, replace: bool = False) -> None:
        self.registry.register(name, provider, replace=replace)

    def build(self, context: PromptContext, *, sections: Optional[Iterable[str]] = None) -> str:
        rendered = []
        for name in sections or self.sections:
            provider = self.registry.require(str(name))
            value = provider.render(context).strip()
            if value:
                rendered.append(value)
        return "\n\n".join(rendered)

    def build_planning(self, context: PromptContext) -> str:
        return self.build(context)

    def build_rethinking(self, context: PromptContext) -> str:
        names = tuple(name for name in self.sections if name != "action") + (
            "action",
            "rethinking",
        )
        return self.build(context, sections=names)


__all__ = ["SemanticPromptBuilder"]
