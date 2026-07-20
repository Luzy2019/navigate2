"""Model configuration for :class:`AgentPlanner`."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Dict, Optional, Union


@dataclass(frozen=True)
class AgentModelConfig:
    """Connection settings for one model used by ``AgentPlanner``."""

    model_name: str
    model_type: str = "close_source"
    api_key: Optional[str] = None
    api_base: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("model_name must not be empty")
        if self.model_type not in {"close_source", "local"}:
            raise ValueError("model_type must be 'close_source' or 'local'")


AGENT_MODEL_CONFIGS: Dict[str, AgentModelConfig] = {
    "gpt-4o": AgentModelConfig(model_name="gpt-4o"),
    "gemini-2.5": AgentModelConfig(model_name="gemini_direct/gemini-2.5-pro"),
}


def resolve_agent_model_config(
    model: Union[str, AgentModelConfig],
    *,
    local: bool = False,
    api_key: str = "",
    api_base: str = "",
) -> AgentModelConfig:
    """Resolve a named preset or arbitrary model name into client settings."""

    if isinstance(model, AgentModelConfig):
        return model
    model_name = str(model).strip()
    if not model_name:
        raise ValueError("model must not be empty")
    if local:
        return AgentModelConfig(
            model_name=model_name,
            model_type="local",
            api_key=api_key,
            api_base=api_base,
        )
    preset = AGENT_MODEL_CONFIGS.get(model_name)
    if preset is not None:
        return AgentModelConfig(
            model_name=preset.model_name,
            model_type=preset.model_type,
            api_key=preset.api_key or os.environ.get("OPENAI_API_KEY"),
            api_base=preset.api_base or os.environ.get("OPENAI_API_BASE"),
        )
    return AgentModelConfig(
        model_name=model_name,
        api_key=os.environ.get("OPENAI_API_KEY"),
        api_base=os.environ.get("OPENAI_API_BASE"),
    )


__all__ = ["AGENT_MODEL_CONFIGS", "AgentModelConfig", "resolve_agent_model_config"]
