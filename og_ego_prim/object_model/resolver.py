"""Alias resolution across task, scene-graph, and simulator identifiers."""

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Set, Tuple


class AmbiguousEntityAliasError(LookupError):
    pass


def normalize_entity_alias(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\.n\.\d+", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def _alias_keys(value: object) -> Tuple[str, ...]:
    raw = str(value or "").strip().lower()
    normalized = normalize_entity_alias(raw)
    return tuple(dict.fromkeys(item for item in (raw, normalized) if item))


class EntityResolver:
    """Many-alias resolver that surfaces ambiguity instead of guessing."""

    def __init__(self) -> None:
        self._entity_aliases: Dict[str, Set[str]] = {}
        self._alias_entities: Dict[str, Set[str]] = {}

    def register(
        self,
        entity_id: str,
        aliases: Iterable[str] = (),
        *,
        replace: bool = False,
    ) -> str:
        canonical = str(entity_id or "").strip()
        if not canonical:
            raise ValueError("entity_id must not be empty")
        if replace:
            self.unregister(canonical)
        values = {canonical, *(str(alias).strip() for alias in aliases)}
        stored = self._entity_aliases.setdefault(canonical, set())
        for value in values:
            if not value:
                continue
            stored.add(value)
            for key in _alias_keys(value):
                self._alias_entities.setdefault(key, set()).add(canonical)
        return canonical

    def add_alias(self, entity_id: str, alias: str) -> None:
        if entity_id not in self._entity_aliases:
            self.register(entity_id)
        self.register(entity_id, (alias,))

    def unregister(self, entity_id: str) -> None:
        canonical = str(entity_id or "").strip()
        aliases = self._entity_aliases.pop(canonical, set())
        for value in aliases:
            for key in _alias_keys(value):
                entities = self._alias_entities.get(key)
                if entities is None:
                    continue
                entities.discard(canonical)
                if not entities:
                    self._alias_entities.pop(key, None)

    def resolve_all(self, alias: object) -> Tuple[str, ...]:
        raw = str(alias or "").strip()
        if raw in self._entity_aliases:
            return (raw,)
        matches: Set[str] = set()
        for key in _alias_keys(alias):
            matches.update(self._alias_entities.get(key, ()))
        return tuple(sorted(matches))

    def resolve(self, alias: object, *, strict: bool = True) -> Optional[str]:
        matches = self.resolve_all(alias)
        if not matches:
            return None
        if len(matches) > 1:
            if strict:
                raise AmbiguousEntityAliasError(
                    f"entity alias {alias!r} matches {', '.join(matches)}"
                )
            return None
        return matches[0]

    def aliases_for(self, entity_id: str) -> Tuple[str, ...]:
        return tuple(sorted(self._entity_aliases.get(entity_id, ())))

    def clear(self) -> None:
        self._entity_aliases.clear()
        self._alias_entities.clear()


__all__ = [
    "AmbiguousEntityAliasError",
    "EntityResolver",
    "normalize_entity_alias",
]
