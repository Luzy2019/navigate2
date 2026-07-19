"""Cross-module planning input/output conversion helpers."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional

from og_ego_prim.domain import Action


_JSON_CODE_BLOCK = re.compile(
    r"```json\s*(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def parse_json_code_block(output: str) -> Optional[Dict[str, Any]]:
    """Return the first JSON object fenced as ``json`` in a model response."""

    match = _JSON_CODE_BLOCK.search(output or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1).strip())
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_observation_images(directory: str | Path) -> List[str]:
    """List PNG observation paths in deterministic filename order."""

    path = Path(directory)
    if not path.is_dir():
        return []
    return [
        str(item)
        for item in sorted(path.iterdir(), key=lambda item: item.name)
        if item.is_file() and item.suffix.lower() == ".png"
    ]


def normalize_planner_action(value: Any) -> Optional[Action]:
    """Normalize legacy planner values into the shared typed action contract."""

    if value is None:
        return None
    if isinstance(value, Action):
        return value
    if isinstance(value, str):
        return Action.from_raw(value)
    if isinstance(value, Mapping):
        if value.get("action") is not None:
            action = normalize_planner_action(value["action"])
            if action is None:
                return None
            caution = value.get("caution")
            if caution is not None:
                extensions = dict(action.extensions)
                extensions["caution"] = caution
                action = replace(action, extensions=extensions)
            return action
        if value.get("name"):
            fields = {
                "name",
                "actor_id",
                "object_id",
                "target_id",
                "parameters",
                "raw",
                "schema_version",
                "extensions",
            }
            payload = {key: item for key, item in value.items() if key in fields}
            extensions = dict(payload.get("extensions") or {})
            extensions.update(
                {key: item for key, item in value.items() if key not in fields}
            )
            payload["extensions"] = extensions
            return Action(**payload)
    raise TypeError(f"unsupported planner action: {type(value).__name__}")


__all__ = [
    "list_observation_images",
    "normalize_planner_action",
    "parse_json_code_block",
]
