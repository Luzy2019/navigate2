"""Safe serialization for replay artifacts."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
import math
from pathlib import Path
import re
from typing import Any, Mapping, MutableSet, Optional
from uuid import UUID


REDACTED = "[REDACTED]"
CIRCULAR = "[CIRCULAR]"

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "client_secret",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "access_token",
    "auth_token",
    "token",
    "secret",
}
_BEARER_PATTERN = re.compile(
    r"(?i)\bbearer\s+(?:\[REDACTED\]|[A-Za-z0-9._~+/=-]+)"
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"""
    (?P<prefix>
        (?<![A-Za-z0-9_])
        (?P<key_quote>["']?)
        (?:
            api[_ -]?key|apikey|authorization|cookie|client[_ -]?secret|
            password|passwd|private[_ -]?key|refresh[_ -]?token|
            access[_ -]?token|auth[_ -]?token|token|secret
        )
        (?P=key_quote)
        (?![A-Za-z0-9_])
        \s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"\\])*" |
        '(?:\\.|[^'\\])*' |
        \[REDACTED\] |
        (?:bearer|basic)\s+(?:\[REDACTED\]|[^\s,;\]}&]+) |
        [^\s,;\]}&]+
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _normalized_key(key: object) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def is_sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_access_token")
        or normalized.endswith("_refresh_token")
        or normalized.endswith("_auth_token")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
    )


def redact_text(value: str) -> str:
    """Remove common credential shapes without altering ordinary prompt text."""

    def redact_assignment(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        quote = raw_value[:1]
        replacement = REDACTED
        if quote in {"\"", "'"} and raw_value.endswith(quote):
            replacement = f"{quote}{REDACTED}{quote}"
        return match.group("prefix") + replacement

    value = _SENSITIVE_ASSIGNMENT_PATTERN.sub(redact_assignment, value)
    value = _BEARER_PATTERN.sub("Bearer " + REDACTED, value)
    return _OPENAI_KEY_PATTERN.sub(REDACTED, value)


def _safe_builtin(
    value: Any,
    *,
    seen: MutableSet[int],
    depth: int,
    max_depth: int,
) -> Any:
    if depth > max_depth:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Enum):
        return _safe_builtin(
            value.value,
            seen=seen,
            depth=depth + 1,
            max_depth=max_depth,
        )
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"))

    identity = id(value)
    if identity in seen:
        return CIRCULAR
    seen.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            source = {item.name: getattr(value, item.name) for item in fields(value)}
            return _safe_builtin(
                source,
                seen=seen,
                depth=depth + 1,
                max_depth=max_depth,
            )

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                converted = to_dict()
            except Exception as error:  # Debug logging must not break the run.
                return {
                    "type": type(value).__name__,
                    "serialization_error": redact_text(str(error)),
                }
            return _safe_builtin(
                converted,
                seen=seen,
                depth=depth + 1,
                max_depth=max_depth,
            )

        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                string_key = str(key)
                if is_sensitive_key(string_key):
                    result[string_key] = REDACTED
                    continue
                result[string_key] = _safe_builtin(
                    item,
                    seen=seen,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            return result

        if isinstance(value, (list, tuple)):
            return [
                _safe_builtin(
                    item,
                    seen=seen,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for item in value
            ]
        if isinstance(value, (set, frozenset)):
            return [
                _safe_builtin(
                    item,
                    seen=seen,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for item in sorted(value, key=str)
            ]

        detached = getattr(value, "detach", None)
        if callable(detached):
            try:
                value = detached()
                cpu = getattr(value, "cpu", None)
                value = cpu() if callable(cpu) else value
            except Exception:
                return f"<{type(value).__name__}>"
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            try:
                return _safe_builtin(
                    tolist(),
                    seen=seen,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            except Exception:
                return f"<{type(value).__name__}>"
        return f"<{type(value).__name__}>"
    finally:
        seen.discard(identity)


def to_safe_builtin(
    value: Any,
    *,
    component: Optional[str] = None,
    max_depth: int = 64,
) -> Any:
    """Convert arbitrary diagnostic values to JSON-safe, redacted builtins."""

    _ = component
    return _safe_builtin(
        value,
        seen=set(),
        depth=0,
        max_depth=max(int(max_depth), 1),
    )


__all__ = [
    "CIRCULAR",
    "REDACTED",
    "is_sensitive_key",
    "redact_text",
    "to_safe_builtin",
]
