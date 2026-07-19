"""Reusable argparse value converters for benchmark entry points."""

from __future__ import annotations

import argparse
from typing import Any, Optional

from og_ego_prim.config.runtime_config import Size, parse_size


def parse_optional_bool(value: Any) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean value, got {value!r}")


def parse_optional_size(value: Any) -> Optional[Size]:
    try:
        return parse_size(value, allow_none=True)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "expected WIDTHxHEIGHT, raw, none, null, or 0"
        ) from exc


__all__ = ["parse_optional_bool", "parse_optional_size"]
