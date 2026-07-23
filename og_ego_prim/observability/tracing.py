"""Transparent tracing proxies for planner adapters and model clients."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Optional

from .serialization import redact_text, to_safe_builtin
from .session import ReplaySession


def _resolve(value: Any, fallback: Any) -> Any:
    if callable(value):
        return value()
    return fallback if value is None else value


def _note_tracing_error(
    session: ReplaySession, stage: str, error: BaseException
) -> None:
    try:
        note_error = getattr(session, "_note_recording_error", None)
    except Exception:
        return
    if callable(note_error):
        try:
            note_error(stage, error)
        except Exception:
            pass


def _safe_getattr(
    session: ReplaySession,
    target: Any,
    name: str,
    default: Any,
    *,
    stage: str,
) -> Any:
    try:
        return getattr(target, name, default)
    except Exception as error:
        _note_tracing_error(session, stage, error)
        return default


def _safe_resolve(
    session: ReplaySession,
    value: Any,
    fallback: Any,
    *,
    stage: str,
) -> Any:
    try:
        return _resolve(value, fallback)
    except Exception as error:
        _note_tracing_error(session, stage, error)
        return fallback


def _safe_emit(session: ReplaySession, *args: Any, **kwargs: Any) -> Any:
    """Emit best-effort from a proxy; tracing must not change task behavior."""

    try:
        return session.emit(*args, **kwargs)
    except Exception as error:
        _note_tracing_error(session, "tracing_emit", error)
        return None


class TracingPlannerAdapter:
    """Record planner inputs and outputs while preserving the adapter protocol."""

    def __init__(
        self,
        planner: Any,
        session: ReplaySession,
        *,
        emit_proposals: bool = True,
        model_applicable: Optional[bool] = None,
        subtask_id: Any = None,
        sim_step: Any = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not callable(getattr(planner, "propose", None)):
            raise TypeError("planner must implement propose(context)")
        self.planner = planner
        self.session = session
        self.emit_proposals = bool(emit_proposals)
        supports_rethinking = _safe_getattr(
            session,
            planner,
            "supports_rethinking",
            False,
            stage="planner_supports_rethinking",
        )
        try:
            self.supports_rethinking = bool(supports_rethinking)
        except Exception as error:
            _note_tracing_error(session, "planner_supports_rethinking", error)
            self.supports_rethinking = False
        self._subtask_id = subtask_id
        self._sim_step = sim_step
        self._clock = clock
        if model_applicable is None:
            class_name = type(planner).__name__.lower()
            model_applicable = not (
                "iterator" in class_name
                or "example" in class_name
                or "scripted" in class_name
                or not self.supports_rethinking
            )
        self.model_applicable = bool(model_applicable)

    def propose(self, context: Any) -> Any:
        action_id = self.session.new_action_id()
        subtask_id = _safe_resolve(
            self.session,
            self._subtask_id,
            self.session.current_subtask_id,
            stage="planner_subtask_provider",
        )
        sim_step = _safe_resolve(
            self.session,
            self._sim_step,
            self.session.current_sim_step,
            stage="planner_sim_step_provider",
        )
        self.session._activate_action(
            action_id,
            subtask_id=subtask_id,
            sim_step=sim_step,
        )
        _safe_emit(self.session,
            "planner",
            "planner_context_built",
            {"context": to_safe_builtin(context)},
            action_id=action_id,
            sim_step=sim_step,
            subtask_id=subtask_id,
        )
        if not self.model_applicable:
            _safe_emit(self.session,
                "model",
                "model_not_applicable",
                {"reason": "scripted_or_example_planning"},
                status="not_applicable",
                action_id=action_id,
                sim_step=sim_step,
                subtask_id=subtask_id,
            )

        started = self._clock()
        try:
            proposed = self.planner.propose(context)
        except Exception as error:
            duration_ms = (self._clock() - started) * 1000.0
            _safe_emit(self.session,
                "planner",
                "planner_failed",
                {
                    "error": {
                        "type": type(error).__name__,
                        "message": redact_text(str(error)),
                    }
                },
                status="failed",
                action_id=action_id,
                sim_step=sim_step,
                subtask_id=subtask_id,
                duration_ms=duration_ms,
            )
            self.session._activate_action(None)
            raise

        duration_ms = (self._clock() - started) * 1000.0
        if proposed is None:
            _safe_emit(self.session,
                "planner",
                "plan_exhausted",
                None,
                status="completed",
                action_id=action_id,
                sim_step=sim_step,
                subtask_id=subtask_id,
                duration_ms=duration_ms,
            )
            self.session._activate_action(None)
            return None

        try:
            self.session.start_action(
                proposed,
                subtask_id=subtask_id,
                sim_step=sim_step,
                action_id=action_id,
            )
        except Exception as error:
            note_error = getattr(self.session, "_note_recording_error", None)
            if callable(note_error):
                note_error("tracing_start_action", error)
            self.session._activate_action(
                action_id,
                subtask_id=subtask_id,
                sim_step=sim_step,
            )
        if self.emit_proposals:
            _safe_emit(self.session,
                "planner",
                "plan_proposed",
                {"action": proposed},
                action_id=action_id,
                sim_step=sim_step,
                subtask_id=subtask_id,
                duration_ms=duration_ms,
            )
        return proposed

    def __getattr__(self, name: str) -> Any:
        return getattr(self.planner, name)


class TracingModelClient:
    """Trace the real prompt, image paths, response, latency, and exceptions."""

    def __init__(
        self,
        client: Any,
        session: ReplaySession,
        *,
        component: str = "model",
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not callable(getattr(client, "model", None)):
            raise TypeError("model client must implement model(*args, **kwargs)")
        self.client = client
        self.session = session
        self.component = str(component or "model")
        self._clock = clock

    @staticmethod
    def _request_payload(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        prompt = kwargs.get("prompt", args[0] if args else None)
        image_file = kwargs.get("image_file", args[1] if len(args) > 1 else None)
        gen_args = kwargs.get("gen_args", args[2] if len(args) > 2 else None)
        return {
            "prompt": prompt,
            "image_file": image_file,
            "gen_args": gen_args,
        }

    def model(self, *args: Any, **kwargs: Any) -> Any:
        request = self._request_payload(args, kwargs)
        action_id = self.session.current_action_id
        _safe_emit(self.session,
            "prompt",
            "prompt_built",
            request,
            action_id=action_id,
        )
        _safe_emit(self.session,
            self.component,
            "model_request_started",
            {
                **request,
                "model_name": _safe_getattr(
                    self.session,
                    self.client,
                    "model_name",
                    None,
                    stage="model_name_property",
                ),
                "client_type": type(self.client).__name__,
            },
            status="started",
            action_id=action_id,
        )
        started = self._clock()
        try:
            response = self.client.model(*args, **kwargs)
        except Exception as error:
            duration_ms = (self._clock() - started) * 1000.0
            _safe_emit(self.session,
                self.component,
                "model_error",
                {
                    "error": {
                        "type": type(error).__name__,
                        "message": redact_text(str(error)),
                    }
                },
                status="failed",
                action_id=action_id,
                duration_ms=duration_ms,
            )
            raise
        duration_ms = (self._clock() - started) * 1000.0
        _safe_emit(self.session,
            self.component,
            "model_response",
            {"response": response},
            action_id=action_id,
            duration_ms=duration_ms,
        )
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


class TracingEvaluatorProxy:
    """Trace evaluator calls without exposing evaluator-only state to planning."""

    _TRACED_METHODS = (
        "record_action",
        "evaluate_process_safety_goal_condition",
        "evaluate_execution_goal_condition",
        "evaluate_non_executed_process_safety_goal_condition",
        "evaluate_termination_safety_goal_condition",
        "evaluate_awareness",
        "finish_subtask",
        "summary",
    )

    def __init__(
        self,
        evaluator: Any,
        session: ReplaySession,
        *,
        sim_step: Any = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.evaluator = evaluator
        self.session = session
        self._sim_step = sim_step
        self._clock = clock

    def _resolved_sim_step(self) -> Any:
        try:
            return _resolve(self._sim_step, self.session.current_sim_step)
        except Exception as error:
            note_error = getattr(self.session, "_note_recording_error", None)
            if callable(note_error):
                note_error("evaluator_sim_step", error)
            return self.session.current_sim_step

    def _goal_snapshot(self) -> Any:
        try:
            tracker = getattr(self.evaluator, "tracker", None)
        except Exception as error:
            _note_tracing_error(self.session, "evaluator_tracker_property", error)
            return None
        if tracker is None:
            return None
        try:
            goal_condition = getattr(tracker, "goal_condition")
        except AttributeError:
            return None
        except Exception as error:
            _note_tracing_error(self.session, "evaluator_goal_condition_property", error)
            return None
        try:
            return to_safe_builtin(goal_condition, component="evaluator")
        except Exception as error:
            return {"serialization_error": type(error).__name__}

    @staticmethod
    def _goal_delta(before: Any, after: Any) -> Dict[str, Any]:
        if before == after:
            return {"changed": False, "changed_keys": []}
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            before_keys = set(before)
            after_keys = set(after)
            changed = sorted(
                str(key)
                for key in before_keys & after_keys
                if before[key] != after[key]
            )
            return {
                "changed": True,
                "changed_keys": changed,
                "added_keys": sorted(str(key) for key in after_keys - before_keys),
                "removed_keys": sorted(str(key) for key in before_keys - after_keys),
            }
        return {"changed": True, "changed_keys": []}

    def _invoke(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self.evaluator, method_name)
        tracker_before = self._goal_snapshot()
        started = self._clock()
        try:
            result = method(*args, **kwargs)
        except Exception as error:
            duration_ms = (self._clock() - started) * 1000.0
            tracker_after = self._goal_snapshot()
            sim_step = self._resolved_sim_step()
            _safe_emit(
                self.session,
                "evaluator",
                f"{method_name}_failed",
                {
                    "method": method_name,
                    "args": args,
                    "kwargs": kwargs,
                    "error": {
                        "type": type(error).__name__,
                        "message": redact_text(str(error)),
                    },
                    "tracker_goal_condition": {
                        "before": tracker_before,
                        "after": tracker_after,
                        "delta": self._goal_delta(tracker_before, tracker_after),
                    },
                },
                status="failed",
                sim_step=sim_step,
                duration_ms=duration_ms,
            )
            raise
        duration_ms = (self._clock() - started) * 1000.0
        tracker_after = self._goal_snapshot()
        sim_step = self._resolved_sim_step()
        _safe_emit(
            self.session,
            "evaluator",
            f"{method_name}_completed",
            {
                "method": method_name,
                "args": args,
                "kwargs": kwargs,
                "result": result,
                "tracker_goal_condition": {
                    "before": tracker_before,
                    "after": tracker_after,
                    "delta": self._goal_delta(tracker_before, tracker_after),
                },
            },
            sim_step=sim_step,
            duration_ms=duration_ms,
        )
        return result

    def record_action(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("record_action", *args, **kwargs)

    def evaluate_process_safety_goal_condition(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("evaluate_process_safety_goal_condition", *args, **kwargs)

    def evaluate_execution_goal_condition(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("evaluate_execution_goal_condition", *args, **kwargs)

    def evaluate_non_executed_process_safety_goal_condition(
        self, *args: Any, **kwargs: Any
    ) -> Any:
        return self._invoke(
            "evaluate_non_executed_process_safety_goal_condition", *args, **kwargs
        )

    def evaluate_termination_safety_goal_condition(
        self, *args: Any, **kwargs: Any
    ) -> Any:
        return self._invoke(
            "evaluate_termination_safety_goal_condition", *args, **kwargs
        )

    def evaluate_awareness(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("evaluate_awareness", *args, **kwargs)

    def finish_subtask(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("finish_subtask", *args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("summary", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.evaluator, name)


__all__ = [
    "TracingEvaluatorProxy",
    "TracingModelClient",
    "TracingPlannerAdapter",
]
