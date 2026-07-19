"""Shared conversion helpers for contracts, prompts, and debug artifacts."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, runtime_checkable
from uuid import UUID


ExtensionMap = Dict[str, Any]
Fallback = Optional[Callable[[Any], Any]]


@runtime_checkable
class VersionedPayload(Protocol):
    """Structural contract for data crossing runtime module boundaries."""

    schema_version: str
    extensions: ExtensionMap


def _convert(value: Any, fallback: Fallback) -> Any:
    if isinstance(value, Enum):
        return _convert(value.value, fallback)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _convert(getattr(value, item.name), fallback)
            for item in fields(value)
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _convert(to_dict(), fallback)
    if isinstance(value, Mapping):
        return {str(key): _convert(item, fallback) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(item, fallback) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_convert(item, fallback) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        return _convert(value.tolist(), fallback)
    if fallback is not None:
        return fallback(value)
    raise TypeError(
        f"value of type {type(value).__name__} is not JSON serializable; "
        "convert it to a builtin value before storing it in a contract"
    )


def to_builtin(value: Any) -> Any:
    """Recursively convert a value to JSON-compatible Python types."""

    return _convert(value, fallback=None)


def to_debug_builtin(value: Any) -> Any:
    """Convert debug payloads while stringifying unsupported diagnostic values."""

    return _convert(value, fallback=str)


def as_versioned_dict(value: VersionedPayload) -> Dict[str, Any]:
    """Serialize a versioned payload after validating its required metadata."""

    schema_version = getattr(value, "schema_version", None)
    if not isinstance(schema_version, str):
        raise TypeError("versioned payload must define a string schema_version")
    if not schema_version.strip():
        raise ValueError("versioned payload schema_version must not be empty")
    extensions = getattr(value, "extensions", None)
    if not isinstance(extensions, Mapping):
        raise TypeError("versioned payload must define a mapping extensions field")
    payload = to_builtin(value)
    if not isinstance(payload, dict):
        raise TypeError("versioned payload serialization must produce a dictionary")
    return payload


__all__ = [
    "ExtensionMap",
    "VersionedPayload",
    "as_versioned_dict",
    "to_builtin",
    "to_debug_builtin",
]
