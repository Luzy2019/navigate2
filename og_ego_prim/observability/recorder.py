"""Thread-safe JSONL recording for offline runtime replay."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

from .serialization import redact_text, to_safe_builtin


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def runtime_event_component(event: Any) -> str:
    event_type = str(getattr(event, "event_type", "") or "").lower()
    if "plan" in event_type or "prompt" in event_type:
        return "planner"
    if "risk" in event_type or "caution" in event_type:
        return "risk"
    if "memory" in event_type or "recall" in event_type:
        return "memory"
    if "scheduler" in event_type or "timer" in event_type or "temporal" in event_type:
        return "scheduler"
    if "execut" in event_type or event_type.startswith("action_"):
        return "executor"
    if "evaluat" in event_type or "termination" in event_type or "goal" in event_type:
        return "evaluator"
    if "scene" in event_type or "observ" in event_type or "caption" in event_type:
        return "observation"
    return str(getattr(event, "source", None) or "agent_runtime").strip().lower()


class ReplayJSONLRecorder:
    """Write one flushed JSON object per event with an atomic sequence number."""

    schema_version = "isbench.replay_event.v1"

    def __init__(
        self,
        path: str | Path,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A run owns its timeline.  Truncating avoids duplicate sequence numbers
        # when a caller reuses an output directory after an interrupted run.
        self._file = self.path.open("w", encoding="utf-8", buffering=1)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._seq = 0
        self._closed = False
        self._component_counts: Counter[str] = Counter()

    @property
    def event_count(self) -> int:
        with self._lock:
            return self._seq

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def component_counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._component_counts)

    @property
    def recording_errors(self) -> tuple[Dict[str, Any], ...]:
        """Recorder-level I/O errors are exposed by session sinks, if any."""

        return ()

    def record(
        self,
        component: str,
        event_type: str,
        *,
        payload: Any = None,
        status: str = "completed",
        action_id: Optional[str] = None,
        sim_step: Optional[int] = None,
        subtask_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        component_name = str(component or "").strip().lower()
        event_name = str(event_type or "").strip().lower()
        status_name = str(status or "").strip().lower()
        if not component_name:
            raise ValueError("replay event component must not be empty")
        if not event_name:
            raise ValueError("replay event_type must not be empty")
        if not status_name:
            raise ValueError("replay event status must not be empty")

        with self._lock:
            if self._closed:
                raise RuntimeError("replay recorder is closed")
            safe_payload = to_safe_builtin(payload, component=component_name)
            next_seq = self._seq + 1
            event: Dict[str, Any] = {
                "schema_version": self.schema_version,
                "seq": next_seq,
                "timestamp": self._wall_clock(),
                "monotonic_time": float(self._monotonic_clock()),
                "action_id": None if action_id is None else str(action_id),
                "component": component_name,
                "event_type": event_name,
                "status": status_name,
                "duration_ms": self._duration_value(duration_ms),
                "sim_step": None if sim_step is None else int(sim_step),
                "subtask_id": None if subtask_id is None else str(subtask_id),
                "payload": safe_payload,
            }
            line = json.dumps(
                event, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ) + "\n"
            self._file.write(line)
            self._file.flush()
            self._seq = next_seq
            self._component_counts[component_name] += 1
            return event

    @staticmethod
    def _duration_value(duration_ms: Optional[float]) -> Optional[float]:
        if duration_ms is None:
            return None
        value = float(duration_ms)
        return max(value, 0.0) if math.isfinite(value) else None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._file.flush()
            self._file.close()
            self._closed = True

    def __enter__(self) -> "ReplayJSONLRecorder":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class ReplayEventSink:
    """Duck-typed bridge from existing ``RuntimeEvent`` values to replay JSONL."""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.recording_errors: list[Dict[str, Any]] = []

    def emit(self, event: Any) -> None:
        try:
            emit_runtime_event = getattr(self.target, "emit_runtime_event", None)
            if callable(emit_runtime_event):
                emit_runtime_event(event)
                return

            event_type = str(getattr(event, "event_type", "runtime_event"))
            details = dict(getattr(event, "details", None) or {})
            payload = {
                "event_id": getattr(event, "event_id", None),
                "task_id": getattr(event, "task_id", None),
                "runtime_action_id": getattr(event, "action_id", None),
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
            self.target.record(
                runtime_event_component(event),
                event_type,
                payload=payload,
                status=(
                    "failed"
                    if event_type.lower().endswith(("failed", "error"))
                    else "completed"
                ),
                action_id=getattr(event, "action_id", None),
                sim_step=getattr(event, "step", None),
                subtask_id=getattr(event, "subtask_id", None),
            )
        except Exception as error:  # Observability must never alter task behavior.
            entry = {
                "type": type(error).__name__,
                "message": redact_text(str(error)),
            }
            self.recording_errors.append(entry)
            note_error = getattr(self.target, "_note_recording_error", None)
            if callable(note_error):
                note_error("event_sink", error)


__all__ = [
    "ReplayEventSink",
    "ReplayJSONLRecorder",
    "runtime_event_component",
    "utc_now_iso",
]
