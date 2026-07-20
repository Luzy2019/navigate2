"""Registry-backed construction for optional runtime event delivery."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

from og_ego_prim.domain import Registry

from .sinks import (
    CallableEventSink,
    CompositeEventSink,
    EventSink,
    NullEventSink,
)
from .tracker import OnlineTrackerEventSink


EventSinkFactory = Callable[..., EventSink]
EVENT_SINKS: Registry[EventSinkFactory] = Registry()

EVENT_SINKS.register("null", NullEventSink)
EVENT_SINKS.register("disabled", NullEventSink)
EVENT_SINKS.register("callable", CallableEventSink)
EVENT_SINKS.register("composite", CompositeEventSink)

# EVENT_SINKS.register("online_tracker", OnlineTrackerEventSink)
EVENT_SINKS.register("tracker", OnlineTrackerEventSink)


def register_event_sink(
    name: str,
    factory: EventSinkFactory,
    *,
    replace: bool = False,
) -> EventSinkFactory:
    if not callable(factory):
        raise TypeError("event sink factory must be callable")
    return EVENT_SINKS.register(name, factory, replace=replace)


def create_event_sink(
    name: Any = None,
    *args: Any,
    registry: Registry[EventSinkFactory] = EVENT_SINKS,
) -> EventSink:

    options: Dict[str, Any] = {}
    sink = registry.require(str(name))(*args, **options)
    if not isinstance(sink, EventSink):
        raise TypeError("event sink must implement emit(event)")
    return sink


__all__ = [
    "EVENT_SINKS",
    "EventSinkFactory",
    "create_event_sink",
    "register_event_sink",
]
