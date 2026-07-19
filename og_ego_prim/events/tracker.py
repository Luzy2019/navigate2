"""Runtime-event adapter for the existing OnlineEvalTracker storage."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from .models import RuntimeEvent


TrackerRoute = Callable[[Any, RuntimeEvent], None]


def _details(event: RuntimeEvent) -> Dict[str, Any]:
    return dict(event.details or {})


def _track_plan(tracker: Any, event: RuntimeEvent) -> None:
    payload = _details(event)
    payload.setdefault("step", event.step)
    tracker.track_plan(**payload)


def _track_raw_output(tracker: Any, event: RuntimeEvent) -> None:
    tracker.track_raw_output(**_details(event))


def _track_error(tracker: Any, event: RuntimeEvent) -> None:
    tracker.track_error(**_details(event))


def _track_execution_diagnostic(tracker: Any, event: RuntimeEvent) -> None:
    details = _details(event)
    tracker.track_execution_diagnostic(details.pop("diagnostic", details))


def _track_scene_graph(tracker: Any, event: RuntimeEvent) -> None:
    details = _details(event)
    snapshot = details.get("snapshot", event.after)
    tracker.track_scene_graph(snapshot, force=bool(details.get("force", False)))


def _track_latency(tracker: Any, event: RuntimeEvent) -> None:
    details = _details(event)
    tracker.track_latency(details["name"], details["seconds"])


def _kwargs_route(method_name: str) -> TrackerRoute:
    def route(tracker: Any, event: RuntimeEvent) -> None:
        getattr(tracker, method_name)(**_details(event))

    return route


DEFAULT_TRACKER_ROUTES: Mapping[str, TrackerRoute] = {
    "plan_proposed": _track_plan,
    "planner_raw_output": _track_raw_output,
    "error": _track_error,
    "execution_diagnostic": _track_execution_diagnostic,
    "scene_graph_observed": _track_scene_graph,
    "latency_observed": _track_latency,
    "process_safety_evaluated": _kwargs_route("track_process_safety_goal_condition"),
    "termination_safety_evaluated": _kwargs_route("track_termination_safety_goal_condition"),
    "execution_goal_evaluated": _kwargs_route("track_execution_goal_condition"),
    "awareness_observed": _kwargs_route("track_awareness"),
    "caption_observed": _kwargs_route("track_caption"),
    "terminated": _kwargs_route("track_termination"),
}


class OnlineTrackerEventSink:
    """Forward recognized events to tracker methods without retaining events."""

    def __init__(
        self,
        tracker: Any,
        *,
        routes: Optional[Mapping[str, TrackerRoute]] = None,
        include_default_routes: bool = True,
        strict: bool = False,
    ) -> None:
        self.tracker = tracker
        self.strict = bool(strict)
        self._routes: Dict[str, TrackerRoute] = {}
        if include_default_routes:
            self._routes.update(DEFAULT_TRACKER_ROUTES)
        for event_type, route in (routes or {}).items():
            self.register_route(event_type, route, replace=True)

    def register_route(
        self,
        event_type: str,
        route: TrackerRoute,
        *,
        replace: bool = False,
    ) -> None:
        key = str(event_type or "").strip().lower()
        if not key:
            raise ValueError("tracker route event_type must not be empty")
        if key in self._routes and not replace:
            raise ValueError(f"tracker route {key!r} is already registered")
        self._routes[key] = route

    def emit(self, event: RuntimeEvent) -> None:
        route = self._routes.get(event.event_type)
        if route is None:
            if self.strict:
                raise KeyError(f"no tracker route for event {event.event_type!r}")
            return
        route(self.tracker, event)


TrackerEventSink = OnlineTrackerEventSink


__all__ = [
    "DEFAULT_TRACKER_ROUTES",
    "OnlineTrackerEventSink",
    "TrackerEventSink",
    "TrackerRoute",
]
