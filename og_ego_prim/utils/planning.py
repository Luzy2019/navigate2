"""Cross-module planning input/output conversion helpers."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from og_ego_prim.domain import Action


_JSON_CODE_BLOCK = re.compile(
    r"```json\s*(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_BDDL_ENTITY_INSTANCE = re.compile(r"^(.+)\.n\.\d+_\d+$", re.IGNORECASE)


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


def parse_model_json_object(output: str) -> Dict[str, Any]:
    """Parse one complete model response as a JSON object.

    A response may be plain JSON or one complete ``json`` fence. Surrounding
    prose and non-object JSON values are rejected so planner and risk calls use
    the same model-output contract.
    """

    text = str(output or "").strip()
    fenced = re.fullmatch(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("model response must be one valid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("model response JSON must be an object")
    return payload


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


def planner_entity_candidates(
    entity_id: Any,
    allowed_entity_ids: Iterable[str],
) -> Tuple[str, ...]:
    """Return exact task entities represented by one planner identifier."""

    raw = str(entity_id or "").strip()
    allowed = tuple(
        str(value).strip() for value in allowed_entity_ids if str(value).strip()
    )
    exact = next((value for value in allowed if value.casefold() == raw.casefold()), None)
    if exact is not None:
        return (exact,)
    if not raw or _BDDL_ENTITY_INSTANCE.fullmatch(raw):
        return ()
    return tuple(
        value
        for value in allowed
        if (
            (match := _BDDL_ENTITY_INSTANCE.fullmatch(value)) is not None
            and match.group(1).casefold() == raw.casefold()
        )
    )


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


def validate_planner_action(
    value: Any,
    valid_primitives: Mapping[str, int],
    *,
    allowed_entity_ids: Iterable[str] = (),
    forbidden_actions: Iterable[str] = (),
) -> Action:
    """Normalize an action and enforce the active primitive and entity contract."""

    action = normalize_planner_action(value)
    if action is None:
        raise ValueError("planner action must not be empty")

    expected_arity = valid_primitives.get(action.name)
    if expected_arity is None:
        raise ValueError(f"action {action.name!r} is not in the active primitive set")
    if action.name in {str(name).strip().upper() for name in forbidden_actions}:
        raise ValueError(f"action {action.name!r} is not allowed in this planner response")
    if len(action.arguments) != int(expected_arity):
        raise ValueError(
            f"action {action.name!r} expects {expected_arity} arguments, "
            f"got {len(action.arguments)}"
        )

    allowed = tuple(
        str(entity_id).strip()
        for entity_id in allowed_entity_ids
        if str(entity_id).strip()
    )
    entity_arguments = tuple(action.arguments)
    if allowed:
        unknown = [
            entity_id
            for entity_id in entity_arguments
            if not planner_entity_candidates(entity_id, allowed)
        ]
        if unknown:
            raise ValueError(
                "action contains entities outside the allowed set: " + ", ".join(unknown)
            )
    parameters = dict(action.parameters)
    parameters["arguments"] = list(entity_arguments)
    if action.extensions.get("implicit_held_object"):
        return replace(
            action,
            target_id=entity_arguments[0] if entity_arguments else action.target_id,
            parameters=parameters,
        )
    return replace(
        action,
        object_id=entity_arguments[0] if entity_arguments else None,
        target_id=entity_arguments[1] if len(entity_arguments) > 1 else None,
        parameters=parameters,
    )


__all__ = [
    "list_observation_images",
    "normalize_planner_action",
    "parse_json_code_block",
    "parse_model_json_object",
    "planner_entity_candidates",
    "validate_planner_action",
]
