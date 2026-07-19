"""Replay session lifecycle and manifest construction."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

from .recorder import ReplayEventSink, ReplayJSONLRecorder, runtime_event_component
from .serialization import redact_text, to_safe_builtin


_ACTION_TEXT_UNSET = object()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ReplaySession:
    """Own one timeline, synchronized frame index, and replay manifest."""

    manifest_schema_version = "isbench.replay_manifest.v1"

    def __init__(
        self,
        output_dir: str | Path,
        task_id: Optional[str] = None,
        runner: str = "unknown",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeline_path = self.output_dir / "runtime_timeline.jsonl"
        self.manifest_path = self.output_dir / "replay_manifest.json"
        self.task_id = None if task_id is None else str(task_id)
        self.runner = str(runner or "unknown")
        self.metadata = to_safe_builtin(dict(metadata or {}))
        self.run_id = str(uuid4())
        self.started_at = _iso_now()
        self.recorder = ReplayJSONLRecorder(self.timeline_path)
        self.event_sink = ReplayEventSink(self)
        self._lock = threading.RLock()
        self._frames: list[Dict[str, Any]] = []
        self._current_action_id: Optional[str] = None
        self._last_action_id: Optional[str] = None
        self._current_action_text: Optional[str] = None
        self._last_action_text: Optional[str] = None
        self._current_subtask_id: Optional[str] = None
        self._current_sim_step: Optional[int] = None
        self._manifest: Optional[Dict[str, Any]] = None
        self._finishing_event_written = False
        self._recording_errors: list[Dict[str, Any]] = []
        self.emit(
            "runtime",
            "session_started",
            payload={
                "run_id": self.run_id,
                "task_id": self.task_id,
                "runner": self.runner,
                "metadata": self.metadata,
            },
            status="started",
        )

    @property
    def current_action_id(self) -> Optional[str]:
        with self._lock:
            return self._current_action_id

    @property
    def last_action_id(self) -> Optional[str]:
        with self._lock:
            return self._last_action_id

    @property
    def current_action_text(self) -> Optional[str]:
        with self._lock:
            return self._current_action_text

    @property
    def last_action_text(self) -> Optional[str]:
        with self._lock:
            return self._last_action_text

    @property
    def current_subtask_id(self) -> Optional[str]:
        with self._lock:
            return self._current_subtask_id

    @property
    def current_sim_step(self) -> Optional[int]:
        with self._lock:
            return self._current_sim_step

    @property
    def frames(self) -> tuple[Dict[str, Any], ...]:
        with self._lock:
            return tuple(deepcopy(frame) for frame in self._frames)

    @property
    def finalized(self) -> bool:
        with self._lock:
            return self._manifest is not None

    @property
    def recording_errors(self) -> tuple[Dict[str, Any], ...]:
        with self._lock:
            return tuple(deepcopy(self._recording_errors))

    def _note_recording_error(self, stage: str, error: BaseException) -> None:
        """Remember logging failures without allowing them to affect execution."""

        with self._lock:
            self._recording_errors.append(
                {
                    "stage": str(stage),
                    "type": type(error).__name__,
                    "message": redact_text(str(error)),
                    "timestamp": _iso_now(),
                }
            )

    def new_action_id(self) -> str:
        return str(uuid4())

    def set_subtask(self, subtask_id: Optional[Any]) -> None:
        with self._lock:
            self._current_subtask_id = (
                None if subtask_id is None else str(subtask_id)
            )

    def set_sim_step(self, sim_step: Optional[int]) -> None:
        with self._lock:
            self._current_sim_step = None if sim_step is None else int(sim_step)

    def _activate_action(
        self,
        action_id: Optional[str],
        *,
        subtask_id: Optional[Any] = None,
        sim_step: Optional[int] = None,
        action_text: Any = _ACTION_TEXT_UNSET,
    ) -> None:
        with self._lock:
            previous_action_id = self._current_action_id
            self._current_action_id = action_id
            if action_id is None:
                self._current_action_text = None
            elif action_text is not _ACTION_TEXT_UNSET:
                self._current_action_text = (
                    None if action_text is None else str(action_text)
                )
                if self._current_action_text is not None:
                    self._last_action_text = self._current_action_text
            elif action_id != previous_action_id:
                self._current_action_text = None
            if action_id is not None:
                self._last_action_id = str(action_id)
            if subtask_id is not None:
                self._current_subtask_id = str(subtask_id)
            if sim_step is not None:
                self._current_sim_step = int(sim_step)

    def emit(
        self,
        component: str,
        event_type: str,
        payload: Any = None,
        *,
        status: str = "completed",
        action_id: Optional[str] = None,
        sim_step: Optional[int] = None,
        subtask_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if self._manifest is not None:
                raise RuntimeError("replay session is finalized")
            if sim_step is not None:
                try:
                    explicit_step = int(sim_step)
                except (TypeError, ValueError):
                    explicit_step = sim_step
                else:
                    sim_step = explicit_step
                    if (
                        self._current_sim_step is None
                        or explicit_step > self._current_sim_step
                    ):
                        self._current_sim_step = explicit_step
            resolved_action_id = action_id if action_id is not None else self._current_action_id
            resolved_step = sim_step if sim_step is not None else self._current_sim_step
            resolved_subtask = (
                subtask_id if subtask_id is not None else self._current_subtask_id
            )
            try:
                return self.recorder.record(
                    component,
                    event_type,
                    payload=payload,
                    status=status,
                    action_id=resolved_action_id,
                    sim_step=resolved_step,
                    subtask_id=resolved_subtask,
                    duration_ms=duration_ms,
                )
            except Exception as error:  # Replay must be fail-open for the benchmark.
                self._note_recording_error("session_emit", error)
                try:
                    fallback_payload = to_safe_builtin(payload, component=component)
                except Exception as payload_error:
                    self._note_recording_error("session_payload", payload_error)
                    fallback_payload = {"serialization_error": type(payload).__name__}
                return {
                    "schema_version": self.recorder.schema_version,
                    "seq": None,
                    "timestamp": _iso_now(),
                    "monotonic_time": time.monotonic(),
                    "action_id": resolved_action_id,
                    "component": str(component or "").strip().lower(),
                    "event_type": str(event_type or "").strip().lower(),
                    "status": str(status or "completed").strip().lower(),
                    "duration_ms": duration_ms,
                    "sim_step": resolved_step,
                    "subtask_id": resolved_subtask,
                    "payload": fallback_payload,
                    "recorded": False,
                }

    def emit_runtime_event(self, event: Any) -> Dict[str, Any]:
        event_type = str(getattr(event, "event_type", "runtime_event"))
        details = dict(getattr(event, "details", None) or {})
        duration_ms = details.get("duration_ms")
        if duration_ms is None and details.get("seconds") is not None:
            duration_ms = float(details["seconds"]) * 1000.0
        runtime_action_id = getattr(event, "action_id", None)
        replay_action_id = self.current_action_id or runtime_action_id
        payload = {
            "event_id": getattr(event, "event_id", None),
            "task_id": getattr(event, "task_id", None),
            "runtime_action_id": runtime_action_id,
            "entity_ids": tuple(getattr(event, "entity_ids", ()) or ()),
            "before": getattr(event, "before", None),
            "after": getattr(event, "after", None),
            "confidence": getattr(event, "confidence", None),
            "source": getattr(event, "source", None),
            "sim_time": getattr(event, "sim_time", None),
            "schema_version": getattr(event, "schema_version", None),
            "extensions": getattr(event, "extensions", None),
            "details": details,
        }
        return self.emit(
            runtime_event_component(event),
            event_type,
            payload,
            status=(
                "failed"
                if event_type.lower().endswith(("failed", "error"))
                else "completed"
            ),
            action_id=replay_action_id,
            sim_step=getattr(event, "step", None),
            subtask_id=(
                getattr(event, "subtask_id", None)
                if getattr(event, "subtask_id", None) is not None
                else self.current_subtask_id
            ),
            duration_ms=duration_ms,
        )

    @staticmethod
    def _action_payload(action: Any) -> Any:
        if isinstance(action, str):
            return {"action": action}
        to_dict = getattr(action, "to_dict", None)
        if callable(to_dict):
            try:
                return to_dict()
            except Exception as error:
                return {
                    "type": type(action).__name__,
                    "serialization_error": redact_text(str(error)),
                }
        return action

    @staticmethod
    def _action_text(action: Any) -> Optional[str]:
        """Return a compact, readable action label for frame metadata."""

        if action is None:
            return None
        if isinstance(action, str):
            text = action.strip()
            return redact_text(text) if text else None
        if isinstance(action, Mapping):
            for key in ("raw", "action", "plan"):
                candidate = action.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return redact_text(candidate.strip())
        try:
            raw = getattr(action, "raw", None)
        except Exception:
            raw = None
        if isinstance(raw, str) and raw.strip():
            return redact_text(raw.strip())
        try:
            renderer = getattr(action, "to_legacy_plan", None)
        except Exception:
            renderer = None
        if callable(renderer):
            try:
                rendered = renderer(lowercase=False)
            except TypeError:
                try:
                    rendered = renderer()
                except Exception:
                    rendered = None
            except Exception:
                rendered = None
            if isinstance(rendered, str) and rendered.strip():
                return redact_text(rendered.strip())
        try:
            text = str(action).strip()
        except Exception:
            return None
        return redact_text(text) if text else None

    def start_action(
        self,
        action: Any,
        subtask_id: Optional[str] = None,
        sim_step: Optional[int] = None,
        *,
        action_id: Optional[str] = None,
    ) -> str:
        extensions = getattr(action, "extensions", None)
        if action_id is None and isinstance(extensions, Mapping):
            action_id = extensions.get("replay_action_id")
        action_id = str(action_id or self.new_action_id())
        if isinstance(extensions, dict):
            extensions["replay_action_id"] = action_id
        elif isinstance(action, dict):
            action_extensions = action.setdefault("extensions", {})
            if isinstance(action_extensions, dict):
                action_extensions["replay_action_id"] = action_id
        self._activate_action(
            action_id,
            subtask_id=subtask_id,
            sim_step=sim_step,
            action_text=self._action_text(action),
        )
        self.emit(
            "planner",
            "action_started",
            self._action_payload(action),
            status="started",
            action_id=action_id,
            sim_step=sim_step,
            subtask_id=subtask_id,
        )
        return action_id

    def end_action(
        self,
        *,
        action_id: Optional[str] = None,
        status: str = "completed",
        payload: Any = None,
        sim_step: Optional[int] = None,
        subtask_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        resolved_id = action_id or self.current_action_id
        event = self.emit(
            "runtime",
            "action_finished",
            payload,
            status=status,
            action_id=resolved_id,
            sim_step=sim_step,
            subtask_id=subtask_id,
            duration_ms=duration_ms,
        )
        with self._lock:
            if resolved_id == self._current_action_id:
                self._last_action_id = resolved_id
                if self._current_action_text is not None:
                    self._last_action_text = self._current_action_text
                self._current_action_id = None
                self._current_action_text = None
        return event

    @staticmethod
    def _benchmark_step(benchmark: Any) -> Optional[int]:
        controller = getattr(benchmark, "runtime_controller", None)
        step = getattr(controller, "step", None)
        if step is None:
            step = getattr(benchmark, "global_step", None)
        try:
            return None if step is None else int(step)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _execution_diagnostics(benchmark: Any) -> Dict[str, Any]:
        executor = getattr(benchmark, "executor", None)
        controller = getattr(benchmark, "runtime_controller", None)
        return {
            "executor": getattr(executor, "last_execution_diagnostics", None),
            "outcome": getattr(controller, "last_outcome", None),
        }

    def execute_plan(
        self,
        benchmark: Any,
        plan: Any,
        subtask_id: Optional[str] = None,
        *,
        emit_executor_events: bool = True,
    ) -> Any:
        action_id = self.current_action_id
        initial_step = self._benchmark_step(benchmark)
        if action_id is None:
            action_id = self.start_action(
                plan,
                subtask_id=subtask_id,
                sim_step=initial_step,
            )
        elif subtask_id is not None:
            self.set_subtask(subtask_id)
        self.emit(
            "executor" if emit_executor_events else "runtime",
            "executor_started" if emit_executor_events else "action_review_started",
            {
                "plan": self._action_payload(plan),
                "scope": "benchmark.execute_plan",
            },
            status="started",
            action_id=action_id,
            sim_step=initial_step,
            subtask_id=subtask_id,
        )
        self.emit(
            "runtime",
            "action_cycle_started",
            {"plan": self._action_payload(plan), "scope": "benchmark.execute_plan"},
            status="started",
            action_id=action_id,
            sim_step=initial_step,
            subtask_id=subtask_id,
        )
        started = time.perf_counter()
        try:
            result = benchmark.execute_plan(plan)
        except Exception as error:
            duration_ms = (time.perf_counter() - started) * 1000.0
            final_step = self._benchmark_step(benchmark)
            if final_step is not None:
                self.set_sim_step(final_step)
            payload = {
                "plan": self._action_payload(plan),
                "error": {
                    "type": type(error).__name__,
                    "message": redact_text(str(error)),
                },
                "diagnostics": self._execution_diagnostics(benchmark),
            }
            if emit_executor_events:
                self.emit(
                    "executor",
                    "executor_failed",
                    payload,
                    status="failed",
                    action_id=action_id,
                    sim_step=final_step,
                    subtask_id=subtask_id,
                    duration_ms=duration_ms,
                )
            self.emit(
                "runtime",
                "action_cycle_finished",
                {"status": "failed", "scope": "benchmark.execute_plan"},
                status="failed",
                action_id=action_id,
                sim_step=final_step,
                subtask_id=subtask_id,
                duration_ms=duration_ms,
            )
            self.end_action(
                action_id=action_id,
                status="failed",
                payload=payload,
                sim_step=final_step,
                subtask_id=subtask_id,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000.0
        final_step = self._benchmark_step(benchmark)
        if final_step is not None:
            self.set_sim_step(final_step)
        status = "completed" if result is not False else "failed"
        payload = {
            "plan": self._action_payload(plan),
            "result": result,
            "scope": "benchmark.execute_plan",
            "diagnostics": self._execution_diagnostics(benchmark),
        }
        outcome = getattr(getattr(benchmark, "runtime_controller", None), "last_outcome", None)
        blocked = bool(outcome is not None and not bool(getattr(outcome, "executed", True)))
        if blocked:
            payload["blocked_reason"] = getattr(outcome, "reason", None)
        if emit_executor_events:
            self.emit(
                "executor",
                "action_blocked" if blocked else (
                    "executor_completed" if status == "completed" else "executor_failed"
                ),
                payload,
                status="blocked" if blocked else status,
                action_id=action_id,
                sim_step=final_step,
                subtask_id=subtask_id,
                duration_ms=duration_ms,
            )
        elif status == "failed" and blocked:
            self.emit(
                "runtime",
                "action_blocked",
                {"reason": getattr(outcome, "reason", None), "plan": self._action_payload(plan)},
                status="blocked",
                action_id=action_id,
                sim_step=final_step,
                subtask_id=subtask_id,
            )
        self.emit(
            "runtime",
            "action_cycle_finished",
            {
                "status": "blocked" if blocked else status,
                "scope": "benchmark.execute_plan",
                "result": result,
            },
            status="blocked" if blocked else status,
            action_id=action_id,
            sim_step=final_step,
            subtask_id=subtask_id,
            duration_ms=duration_ms,
        )
        self.end_action(
            action_id=action_id,
            status=status,
            payload={"result": result},
            sim_step=final_step,
            subtask_id=subtask_id,
            duration_ms=duration_ms,
        )
        return result

    def record_frame(
        self,
        frame_index: int,
        video_time: float,
        sim_step: Optional[int] = None,
        *,
        global_step: Optional[int] = None,
        action_id: Optional[str] = None,
        subtask_id: Optional[str] = None,
        robot_pose: Any = None,
        held_object: Any = None,
        camera_frame_index: Optional[int] = None,
        topdown_frame_index: Optional[int] = None,
        action: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
        **extra_metadata: Any,
    ) -> Dict[str, Any]:
        if sim_step is not None and global_step is not None and int(sim_step) != int(global_step):
            raise ValueError("sim_step and global_step must match when both are provided")
        resolved_step = sim_step if sim_step is not None else global_step
        resolved_action = (
            action_id
            if action_id is not None
            else (self.current_action_id or self.last_action_id)
        )
        resolved_subtask = (
            subtask_id if subtask_id is not None else self.current_subtask_id
        )
        resolved_action_text = (
            action
            if action is not None
            else (self.current_action_text or self.last_action_text)
        )
        combined_metadata = dict(metadata or {})
        combined_metadata.update(extra_metadata)
        frame_payload = {
            "frame_index": int(frame_index),
            "video_time": float(video_time),
            "camera_frame_index": (
                int(frame_index) if camera_frame_index is None else int(camera_frame_index)
            ),
            "topdown_frame_index": (
                int(frame_index) if topdown_frame_index is None else int(topdown_frame_index)
            ),
            "robot_pose": robot_pose,
            "held_object": held_object,
            "action": resolved_action_text,
            "metadata": combined_metadata,
        }
        event = self.emit(
            "media",
            "frame_captured",
            frame_payload,
            action_id=resolved_action,
            sim_step=resolved_step,
            subtask_id=resolved_subtask,
        )
        frame = {
            "frame_index": int(frame_index),
            "video_time": float(video_time),
            "sim_step": None if resolved_step is None else int(resolved_step),
            "seq": event["seq"],
            "action_id": resolved_action,
            "subtask_id": resolved_subtask,
            "timestamp": event["timestamp"],
            "camera_frame_index": frame_payload["camera_frame_index"],
            "topdown_frame_index": frame_payload["topdown_frame_index"],
            "robot_pose": to_safe_builtin(robot_pose),
            "held_object": to_safe_builtin(held_object),
            "action": to_safe_builtin(resolved_action_text),
            "metadata": to_safe_builtin(combined_metadata),
        }
        with self._lock:
            self._frames.append(frame)
        return dict(frame)

    def _artifact_path(self, value: str | Path) -> str:
        raw = Path(value).expanduser()
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            # CLI callers often pass a path such as
            # ``results/<task>/<run>/report.json`` while the session stores an
            # absolute output directory.  Resolve that cwd-relative spelling
            # first when it already points inside this run; otherwise retain
            # the public API's session-relative interpretation (``video.mp4``).
            cwd_candidate = (Path.cwd() / raw).resolve()
            try:
                cwd_candidate.relative_to(self.output_dir)
            except ValueError:
                candidate = (self.output_dir / raw).resolve()
            else:
                candidate = cwd_candidate
        try:
            relative = candidate.relative_to(self.output_dir)
        except ValueError as error:
            raise ValueError("replay artifacts must be inside the session output directory") from error
        return relative.as_posix()

    def _normalize_media(self, media: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for name, value in dict(media or {}).items():
            if isinstance(value, (str, Path)):
                normalized[str(name)] = {"path": self._artifact_path(value)}
                continue
            if isinstance(value, Mapping):
                item = dict(value)
                for path_key in ("path", "file", "src", "abs_path"):
                    if path_key in item and item[path_key] is not None:
                        item[path_key] = self._artifact_path(item[path_key])
                normalized[str(name)] = to_safe_builtin(item)
                continue
            normalized[str(name)] = to_safe_builtin(value)
        return normalized

    def finalize(
        self,
        media: Optional[Mapping[str, Any]] = None,
        report_path: Optional[str | Path] = None,
        *,
        status: str = "completed",
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if self._manifest is not None:
                return dict(self._manifest)
            status_name = str(status or "completed").strip().lower()
            if not self._finishing_event_written:
                self.emit(
                    "runtime",
                    "session_finished",
                    payload={"status": status_name, "extra": dict(extra or {})},
                    status=status_name,
                )
                self._finishing_event_written = True
            finalized_at = _iso_now()
            manifest: Dict[str, Any] = {
                "schema_version": self.manifest_schema_version,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "runner": self.runner,
                "status": status_name,
                "started_at": self.started_at,
                "finalized_at": finalized_at,
                "metadata": self.metadata,
                "timeline": {
                    "path": self.timeline_path.name,
                    "event_count": self.recorder.event_count,
                    "component_counts": self.recorder.component_counts,
                },
                "frames": list(self._frames),
                "recording_errors": list(self._recording_errors),
                "media": self._normalize_media(media),
                "report_path": (
                    None if report_path is None else self._artifact_path(report_path)
                ),
                "extra": to_safe_builtin(dict(extra or {})),
            }
            if isinstance(self.metadata, Mapping):
                for key in ("scene", "model", "memory_mode", "primitive_type", "headless"):
                    if key in self.metadata:
                        manifest[key] = self.metadata[key]
            temporary_path = self.manifest_path.with_suffix(".json.tmp")
            try:
                with temporary_path.open("w", encoding="utf-8") as file:
                    json.dump(manifest, file, ensure_ascii=False, indent=2, allow_nan=False)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, self.manifest_path)
            except Exception as error:  # A completed task must survive viewer I/O failures.
                self._note_recording_error("manifest_write", error)
                manifest["manifest_write_error"] = {
                    "type": type(error).__name__,
                    "message": redact_text(str(error)),
                }
            try:
                self.recorder.close()
            except Exception as error:
                self._note_recording_error("recorder_close", error)
            manifest["recording_errors"] = list(self._recording_errors)
            self._manifest = manifest
            return dict(manifest)

    def close(self) -> Dict[str, Any]:
        return self.finalize(status="closed")

    def __enter__(self) -> "ReplaySession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, _traceback: Any) -> None:
        if exc is None:
            self.finalize()
        else:
            self.finalize(
                status="failed",
                extra={"error": {"type": exc_type.__name__, "message": str(exc)}},
            )


__all__ = ["ReplaySession"]
