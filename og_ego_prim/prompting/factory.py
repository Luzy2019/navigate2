"""Registry-backed construction for semantic prompt builders."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Protocol, runtime_checkable

from og_ego_prim.domain import Registry

from .builder import SemanticPromptBuilder
from .models import PromptContext


@runtime_checkable
class PromptBuilder(Protocol):
    def build_planning(self, context: PromptContext) -> str:
        ...

    def build_rethinking(self, context: PromptContext) -> str:
        ...


PromptBuilderFactory = Callable[..., PromptBuilder]
PROMPT_BUILDERS: Registry[PromptBuilderFactory] = Registry()
PROMPT_BUILDERS.register("semantic", SemanticPromptBuilder)


def register_prompt_builder(
    name: str,
    factory: PromptBuilderFactory,
    *,
    replace: bool = False,
) -> PromptBuilderFactory:
    if not callable(factory):
        raise TypeError("prompt builder factory must be callable")
    return PROMPT_BUILDERS.register(name, factory, replace=replace)


def create_prompt_builder(
    config: Any = None,
    *,
    registry: Registry[PromptBuilderFactory] = PROMPT_BUILDERS,
    **overrides: Any,
) -> PromptBuilder:
    """Create a builder from a config object, mapping, registered name, or instance."""

    if isinstance(config, PromptBuilder):
        if overrides:
            raise ValueError("cannot apply prompt builder overrides to an existing instance")
        return config

    options: Dict[str, Any]
    if config is None:
        name = "semantic"
        options = {}
    elif isinstance(config, str):
        name = config
        options = {}
    elif isinstance(config, Mapping):
        values = dict(config)
        name = values.pop("builder", values.pop("type", values.pop("name", "semantic")))
        options = dict(values.pop("options", {}) or {})
        sections = values.pop("sections", None)
        values.pop("max_recalled_items", None)
        if sections is not None:
            options.setdefault("sections", sections)
        options.update(values)
    elif hasattr(config, "builder"):
        name = getattr(config, "builder")
        options = dict(getattr(config, "options", {}) or {})
        sections = getattr(config, "sections", None)
        if sections is not None:
            options.setdefault("sections", sections)
    else:
        raise TypeError(
            "prompt builder config must be a mapping, registered name, builder instance, or None"
        )
    options.update(overrides)
    builder = registry.require(str(name))(**options)
    if not isinstance(builder, PromptBuilder):
        raise TypeError("prompt builder must implement build_planning() and build_rethinking()")
    return builder


__all__ = [
    "PROMPT_BUILDERS",
    "PromptBuilder",
    "PromptBuilderFactory",
    "create_prompt_builder",
    "register_prompt_builder",
]
