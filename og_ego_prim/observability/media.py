"""Camera frame capture and synchronized replay media helpers.

The helpers in this module observe existing callbacks or tracker writes. They
never advance the simulator and keep the replay capture state separate from
the benchmark's legacy video cache.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable, Mapping, Optional

import numpy as np
from PIL import Image

from .session import ReplaySession


def _to_numpy_rgb(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.ndim != 3:
        raise ValueError(f"RGB frame must be HxWxC, got {array.shape}")
    if array.shape[-1] > 3:
        array = array[:, :, :3]
    if array.shape[-1] != 3:
        raise ValueError(f"RGB frame must have three channels, got {array.shape}")
    if array.dtype != np.uint8:
        if array.size and float(array.max()) <= 1.0:
            array = array * 255.0
        array = np.clip(array, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(array)


def _resize_rgb(rgb: np.ndarray, output_size: Optional[tuple[int, int]]) -> np.ndarray:
    if output_size is None:
        return rgb
    width, height = (int(output_size[0]), int(output_size[1]))
    if width <= 0 or height <= 0:
        raise ValueError("media output dimensions must be positive")
    if rgb.shape[1] == width and rgb.shape[0] == height:
        return rgb
    return np.ascontiguousarray(
        np.asarray(Image.fromarray(rgb).resize((width, height), Image.Resampling.LANCZOS))
    )


def _to_list(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (tuple, list)):
        return list(value)
    return value


def robot_pose(robot: Any) -> dict[str, Any]:
    """Return a JSON-friendly base pose without retaining simulator objects."""

    try:
        position, orientation = robot.get_position_orientation()
    except Exception:
        return {}
    return {
        "position": _to_list(position),
        "orientation": _to_list(orientation),
    }


def held_object(executor: Any) -> Optional[str]:
    controller = getattr(executor, "controller", None)
    getter = getattr(controller, "_get_obj_in_hand", None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:
        return None
    if value is None:
        return None
    return str(getattr(value, "name", value))


def _action_label(value: Any, *, _seen: Optional[set[int]] = None) -> Optional[str]:
    """Extract a compact raw-plan label from tracker/executor diagnostics."""

    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return None
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            for key in ("action", "raw", "plan"):
                label = _action_label(value.get(key), _seen=seen)
                if label:
                    return label
            return None
        try:
            raw = getattr(value, "raw", None)
        except Exception:
            raw = None
        if raw is value:
            return None
        return _action_label(raw, _seen=seen)
    finally:
        seen.discard(identity)


def frame_action_label(
    session: ReplaySession,
    *,
    tracker: Any = None,
    executor: Any = None,
) -> Optional[str]:
    """Resolve an action label even when a frame arrives after action cleanup.

    Safe-memory frames are written through the legacy tracker callback, which
    does not carry the raw plan.  The executor diagnostics retain that plan for
    the duration of the action (and after it), so use them as a boundary-only
    fallback without changing simulator execution.
    """

    try:
        session_candidates = (
            getattr(session, "current_action_text", None),
            getattr(session, "last_action_text", None),
        )
    except Exception:
        session_candidates = ()
    for candidate in session_candidates:
        label = _action_label(candidate)
        if label:
            return label

    try:
        diagnostics = getattr(executor, "last_execution_diagnostics", None)
    except Exception:
        diagnostics = None
    if isinstance(diagnostics, Mapping):
        label = _action_label(diagnostics.get("plan"))
        if label:
            return label

    try:
        plans = getattr(tracker, "plans", None)
    except Exception:
        plans = None
    try:
        latest = plans[-1] if plans else None
    except (IndexError, TypeError, KeyError):
        latest = None
    if isinstance(latest, Mapping):
        label = _action_label(latest.get("plan") or latest.get("action"))
        if label:
            return label
    return None


def capture_robot_rgb(robot: Any, output_size: Optional[tuple[int, int]] = None) -> np.ndarray:
    obs, _ = robot.get_obs()
    for sensor_obs in obs.values():
        if isinstance(sensor_obs, dict) and "rgb" in sensor_obs:
            return _resize_rgb(_to_numpy_rgb(sensor_obs["rgb"]), output_size)
    raise RuntimeError(f"No robot RGB observation found. Available keys: {list(obs.keys())}")


class ReplayMediaRecorder:
    """Capture a continuous camera stream from an existing executor callback."""

    def __init__(
        self,
        session: ReplaySession,
        *,
        robot: Any,
        executor: Any,
        fps: float,
        capture_interval: int = 1,
        output_size: Optional[tuple[int, int]] = None,
    ) -> None:
        if float(fps) <= 0:
            raise ValueError("replay camera fps must be positive")
        if int(capture_interval) <= 0:
            raise ValueError("replay capture interval must be positive")
        self.session = session
        self.robot = robot
        self.executor = executor
        self.fps = float(fps)
        self.capture_interval = int(capture_interval)
        self.output_size = output_size
        self.frames: list[np.ndarray] = []
        self._installed = False
        self._original_callback: Optional[Callable[[Any], None]] = None

    def install(self) -> None:
        if self._installed:
            return
        original = getattr(self.executor, "step_callback", None)
        self._original_callback = original

        def callback(context: Any) -> None:
            if original is not None:
                original(context)
            step = getattr(context, "global_step_index", None)
            if step is None:
                return
            if int(step) % self.capture_interval != 0:
                return
            self.capture(
                global_step=int(step),
                action=getattr(context, "raw_plan", None),
                metadata={
                    "primitive_name": getattr(context, "primitive_name", None),
                    "low_level_step": getattr(context, "step_index", None),
                },
            )

        self.executor.step_callback = callback
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        self.executor.step_callback = self._original_callback
        self._installed = False

    def capture(
        self,
        *,
        global_step: Optional[int] = None,
        action: Any = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            frame = capture_robot_rgb(self.robot, self.output_size)
        except Exception as error:
            self.session.emit(
                "media",
                "frame_capture_failed",
                {"error": {"type": type(error).__name__, "message": str(error)}},
                status="failed",
                sim_step=global_step,
            )
            return None
        if action is None:
            action = frame_action_label(self.session, executor=self.executor)
        self.frames.append(frame)
        index = len(self.frames) - 1
        return self.session.record_frame(
            index,
            index / self.fps,
            global_step=global_step,
            robot_pose=robot_pose(self.robot),
            held_object=held_object(self.executor),
            action=action,
            metadata=metadata or {},
        )

    def save_camera(self, output_path: str | Path) -> Optional[dict[str, Any]]:
        if not self.frames:
            return None
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        height, width = self.frames[0].shape[:2]
        if any(frame.shape[:2] != (height, width) for frame in self.frames):
            raise ValueError("all replay camera frames must have identical dimensions")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required to save replay camera videos")
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(self.fps),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            for frame in self.frames:
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            return_code = process.wait()
        except Exception:
            process.kill()
            process.wait()
            raise
        if return_code != 0:
            raise RuntimeError(f"ffmpeg failed to save replay camera: {stderr.strip()}")
        return {
            "path": output_path.name,
            "kind": "replay_camera",
            "fps": self.fps,
            "frames": len(self.frames),
            "width": width,
            "height": height,
            "abs_path": str(output_path),
        }


def observe_tracker_frames(
    tracker: Any,
    session: ReplaySession,
    *,
    robot: Any,
    executor: Any,
) -> Callable[[], None]:
    """Observe legacy tracker frames and return a restore callback."""

    original = tracker.track_video_rgb

    def wrapped(rgb: Any) -> Any:
        frame_index = len(getattr(tracker, "video_cache", ()))
        result = original(rgb)
        fps = float(getattr(tracker, "video_fps", 1.0) or 1.0)
        session.record_frame(
            frame_index,
            frame_index / fps,
            global_step=getattr(executor, "global_step_index", None),
            robot_pose=robot_pose(robot),
            held_object=held_object(executor),
            action_id=session.current_action_id,
            subtask_id=session.current_subtask_id,
            action=frame_action_label(session, tracker=tracker, executor=executor),
            metadata={"source": "legacy_tracker"},
        )
        return result

    tracker.track_video_rgb = wrapped

    def restore() -> None:
        tracker.track_video_rgb = original

    return restore


def install_executor_trace(executor: Any, session: ReplaySession) -> Callable[[], None]:
    """Trace the concrete primitive call and return a restore callback."""

    if getattr(executor, "_replay_executor_wrapped", False):
        return lambda: None
    original = executor.execute_plan

    def traced_execute(plan: Any) -> Any:
        action_id = session.current_action_id
        subtask_id = session.current_subtask_id
        initial_step = getattr(executor, "global_step_index", None)
        session.emit(
            "executor",
            "executor_started",
            {"plan": plan},
            status="started",
            action_id=action_id,
            sim_step=initial_step,
            subtask_id=subtask_id,
        )
        started = time.perf_counter()
        try:
            result = original(plan)
        except Exception as error:
            final_step = getattr(executor, "global_step_index", None)
            session.emit(
                "executor",
                "executor_failed",
                {
                    "plan": plan,
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                    "diagnostics": getattr(executor, "last_execution_diagnostics", None),
                },
                status="failed",
                action_id=action_id,
                sim_step=final_step,
                subtask_id=subtask_id,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            raise
        final_step = getattr(executor, "global_step_index", None)
        diagnostics = getattr(executor, "last_execution_diagnostics", None) or {}
        failed = bool(result is False) or str(
            diagnostics.get("status", "")
        ).lower() in {"failed", "failure", "error"}
        session.emit(
            "executor",
            "executor_failed" if failed else "executor_completed",
            {"plan": plan, "result": result, "diagnostics": diagnostics},
            status="failed" if failed else "completed",
            action_id=action_id,
            sim_step=final_step,
            subtask_id=subtask_id,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return result

    executor.execute_plan = traced_execute
    executor._replay_executor_wrapped = True
    executor._replay_original_execute_plan = original

    def restore() -> None:
        if getattr(executor, "_replay_executor_wrapped", False):
            executor.execute_plan = original
            executor._replay_executor_wrapped = False

    return restore


__all__ = [
    "ReplayMediaRecorder",
    "capture_robot_rgb",
    "frame_action_label",
    "held_object",
    "install_executor_trace",
    "observe_tracker_frames",
    "robot_pose",
]
