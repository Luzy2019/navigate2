"""Shared scheduler normalization helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence, Tuple


def normalize_action_name(value: Any) -> str:
    """Return the stable action identifier used by temporal handlers."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)", text)
    return (match.group(1) if match else text).upper()


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    for name in ("entity_id", "object_id", "name"):
        candidate = _read(value, name)
        if candidate is not None:
            return str(candidate)
    return str(value)


def _string_tuple(values: Any) -> Tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    result = []
    for value in values:
        identifier = _identifier(value)
        if identifier and identifier not in result:
            result.append(identifier)
    return tuple(result)


def _as_names(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(name for name in (normalize_action_name(item) for item in value) if name)


def _as_fields(value: Any, default: Sequence[str]) -> Tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


__all__ = [
    "_as_fields",
    "_as_names",
    "_identifier",
    "_read",
    "_string_tuple",
    "_to_bool",
    "normalize_action_name",
]
