"""Small named registry used by replaceable runtime providers."""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Dict, Generic, Iterator, Mapping, Optional, Tuple, TypeVar


T = TypeVar("T")


class DuplicateRegistrationError(ValueError):
    pass


class UnknownRegistrationError(KeyError):
    pass


def normalize_registration_name(name: str) -> str:
    normalized = str(name or "").strip().lower().replace("-", "_")
    if not normalized:
        raise ValueError("registration name must not be empty")
    return normalized


class Registry(Generic[T]):
    """Explicit registry with deterministic names and no import side effects."""

    def __init__(
        self,
        values: Optional[Mapping[str, T]] = None,
        *,
        normalizer: Callable[[str], str] = normalize_registration_name,
    ) -> None:
        self._normalizer = normalizer
        self._values: Dict[str, T] = {}
        for name, value in (values or {}).items():
            self.register(name, value)

    def register(self, name: str, value: T, *, replace: bool = False) -> T:
        key = self._normalizer(name)
        if key in self._values and not replace:
            raise DuplicateRegistrationError(f"provider {key!r} is already registered")
        self._values[key] = value
        return value

    def unregister(self, name: str) -> T:
        key = self._normalizer(name)
        try:
            return self._values.pop(key)
        except KeyError as exc:
            raise UnknownRegistrationError(key) from exc

    def get(self, name: str, default: Optional[T] = None) -> Optional[T]:
        return self._values.get(self._normalizer(name), default)

    def require(self, name: str) -> T:
        key = self._normalizer(name)
        try:
            return self._values[key]
        except KeyError as exc:
            available = ", ".join(sorted(self._values)) or "none"
            raise UnknownRegistrationError(
                f"unknown provider {key!r}; registered providers: {available}"
            ) from exc

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._values))

    def snapshot(self) -> Mapping[str, T]:
        return MappingProxyType(dict(self._values))

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            key = self._normalizer(name)
        except ValueError:
            return False
        return key in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __len__(self) -> int:
        return len(self._values)


__all__ = [
    "DuplicateRegistrationError",
    "Registry",
    "UnknownRegistrationError",
    "normalize_registration_name",
]
