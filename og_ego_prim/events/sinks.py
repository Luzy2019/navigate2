"""Composable event sinks; sinks own delivery, not runtime state."""

from __future__ import annotations

from typing import Callable, Iterable, Protocol, Tuple, runtime_checkable

from .models import RuntimeEvent


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None:
        ...


class NullEventSink:
    def emit(self, event: RuntimeEvent) -> None:
        return None


class CallableEventSink:
    def __init__(self, callback: Callable[[RuntimeEvent], None]) -> None:
        self.callback = callback

    def emit(self, event: RuntimeEvent) -> None:
        self.callback(event)


class CompositeEventSink:
    def __init__(self, sinks: Iterable[EventSink] = ()) -> None:
        self.sinks: Tuple[EventSink, ...] = tuple(sinks)

    def emit(self, event: RuntimeEvent) -> None:
        for sink in self.sinks:
            sink.emit(event)


__all__ = [
    "CallableEventSink",
    "CompositeEventSink",
    "EventSink",
    "NullEventSink",
]
