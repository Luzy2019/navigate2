"""Keep one headless physical-starter session alive between human annotations.

The service replaces only current-frame perception with human confirmation. It
keeps the normal scene graph post-processing, scheduler, GPT-4o risk review,
GPT-4o planner, and starter primitive execution in the same Isaac process.
After every successful action it writes an immutable frame-level physical
checkpoint plus a latest convenience pointer. A new service process restores a
checkpoint directly; it never replays historical actions after a frame snapshot
exists.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import socket
import traceback
from typing import Any, Dict, Mapping, Optional, Sequence

os.environ.setdefault("OMNIGIBSON_HEADLESS", "1")

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()

from og_ego_prim.utils.monkey_patch import add_monkey_patch

add_monkey_patch()

import torch

from og_ego_prim.cli.headless_manual_physical_step import (
    _build_benchmark,
    _capture,
    _configure_agent,
    _latest_raw_output,
    _observe_manual_frame,
    _replay,
    _scene_graph_payload,
    _subtask_instruction,
    _write_json,
    _write_llm_log,
    _write_scene_graph_artifacts,
)
from og_ego_prim.cli.safe_memory_benchmark_once import capture_robot_rgb_frame
from og_ego_prim.benchmark.lifelong_evaluator import LifelongEvaluator
from og_ego_prim.scene_graph.global_state import GlobalSceneGraphAccumulator
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter


SESSION_SCHEMA_VERSION = "isbench.headless_manual_physical_persistent_session.v1"
CHECKPOINT_SCHEMA_VERSION = "isbench.headless_manual_physical_checkpoint.v2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--config")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--local-llm-serve", action="store_true")
    parser.add_argument("--local-serve-ip", default="")
    parser.add_argument("--local-serve-key", default="sk-123456")
    parser.add_argument("--planner-work-dir", default="results")
    parser.add_argument(
        "--restore-frame",
        type=int,
        help="Restore immutable checkpoint/frame_NNNNNN.pt instead of latest.pt.",
    )
    parser.add_argument(
        "--restore-checkpoint",
        help="Restore a checkpoint from this or another session directory.",
    )
    parser.add_argument(
        "--bootstrap-session-dir",
        help=(
            "One-time source of a v1 replay session. Its action prefix is replayed "
            "only to create this service's first physical checkpoint."
        ),
    )
    parser.add_argument("--video-capture-interval", type=int, default=10)
    parser.add_argument("--video-fps", type=float, default=10.0)
    parser.add_argument("--video-output-size", default="512x512")
    parser.add_argument(
        "--post-action-settle-steps",
        type=int,
        default=30,
        help="Physical hold frames before capturing a successful action checkpoint.",
    )
    return parser


def _parse_size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in str(value).lower().split("x", 1))
    except ValueError as exc:
        raise ValueError("video-output-size must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0:
        raise ValueError("video-output-size dimensions must be positive")
    return width, height


class PersistentPhysicalSession:
    def __init__(self, args: argparse.Namespace) -> None:
        if args.video_capture_interval < 1:
            raise ValueError("video-capture-interval must be at least one")
        if args.video_fps <= 0:
            raise ValueError("video-fps must be positive")
        if args.post_action_settle_steps < 0:
            raise ValueError("post-action-settle-steps must be non-negative")
        self.args = args
        self.session_dir = Path(args.session_dir).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.annotations_dir = self.session_dir / "annotations"
        self.annotations_dir.mkdir(exist_ok=True)
        self.scene_graph_dir = self.session_dir / "scene_graph"
        self.scene_graph_dir.mkdir(exist_ok=True)
        self.checkpoint_dir = self.session_dir / "checkpoint"
        self.checkpoint_dir.mkdir(exist_ok=True)
        self.video_dir = self.session_dir / "first_person_video"
        self.video_dir.mkdir(exist_ok=True)
        self.session_path = self.session_dir / "session.json"
        self._session_existed_at_start = self.session_path.exists()
        self.checkpoint_path = self.checkpoint_dir / "latest.pt"
        self.previous_checkpoint_path = self.checkpoint_dir / "previous.pt"
        self.manifest_path = self.checkpoint_dir / "latest.json"
        self.video_capture_interval = args.video_capture_interval
        self.video_output_size = _parse_size(args.video_output_size)
        self.post_action_settle_steps = args.post_action_settle_steps
        self.session = self._load_session()
        self.benchmark, self.updater, runtime = _build_benchmark(args.task, args.config)
        self.runtime = runtime
        self.observer = ISBenchObservationAdapter(sensor_name=runtime.scene_graph.sensor_name)
        self.observer.reset()
        self.accumulator = GlobalSceneGraphAccumulator()
        self.active_subtask_index = int(self.session.get("active_subtask_index", 1))
        self.active_subtask_action_start = int(
            self.session.get("active_subtask_action_start", 0)
        )
        self.lifelong_evaluator = LifelongEvaluator(
            self.benchmark.env,
            self.benchmark.eval_task_config,
            eval_awareness=False,
        )
        self.agent = _configure_agent(self.benchmark, args, self.active_subtask_index)
        self.planner_adapter = self.benchmark.runtime_controller.components.planner
        self._install_video_callback()
        self.restored_from_checkpoint = False
        if args.restore_checkpoint and args.restore_frame is not None:
            raise ValueError("use either --restore-frame or --restore-checkpoint")
        requested_checkpoint = (
            Path(args.restore_checkpoint).resolve()
            if args.restore_checkpoint
            else self._checkpoint_path_for_frame(args.restore_frame)
        )
        if args.restore_checkpoint:
            if not requested_checkpoint.is_file():
                raise FileNotFoundError(
                    f"physical checkpoint does not exist: {requested_checkpoint}"
                )
            self._restore_checkpoint(requested_checkpoint)
        elif args.restore_frame is not None:
            if not requested_checkpoint.is_file():
                raise FileNotFoundError(
                    f"immutable frame checkpoint does not exist: {requested_checkpoint}"
                )
            self._restore_checkpoint(requested_checkpoint)
        elif self.checkpoint_path.exists():
            self._restore_checkpoint(self.checkpoint_path)
        elif self.session["completed_actions"]:
            self._bootstrap_checkpoint()
        else:
            self._capture_initial_frame()

    def _load_session(self) -> Dict[str, Any]:
        if not self.session_path.exists():
            if self.args.bootstrap_session_dir:
                source_path = Path(self.args.bootstrap_session_dir).resolve() / "session.json"
                if not source_path.is_file():
                    raise FileNotFoundError(f"bootstrap session does not exist: {source_path}")
                source = json.loads(source_path.read_text(encoding="utf-8"))
                if str(source.get("task")) != self.args.task:
                    raise ValueError("bootstrap session task does not match --task")
                return {
                    "schema_version": SESSION_SCHEMA_VERSION,
                    "task": self.args.task,
                    "completed_actions": deepcopy(source.get("completed_actions") or []),
                    "active_subtask_index": int(source.get("active_subtask_index", 1)),
                    "status": "bootstrapping_snapshot",
                    "bootstrap_source": str(source_path.parent),
                }
            return {
                "schema_version": SESSION_SCHEMA_VERSION,
                "task": self.args.task,
                "completed_actions": [],
                "status": "initializing",
            }
        value = json.loads(self.session_path.read_text(encoding="utf-8"))
        if value.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ValueError("session directory belongs to a different session format")
        if str(value.get("task")) != self.args.task:
            raise ValueError("session task does not match --task")
        return value

    def _bootstrap_checkpoint(self) -> None:
        _replay(
            self.benchmark,
            self.updater,
            self.accumulator,
            self.scene_graph_dir,
            self.session_dir,
            self.session["completed_actions"],
            self.agent,
        )
        current_index = len(self.session["completed_actions"])
        frame = _capture(
            self.benchmark,
            self.observer,
            self.session_dir / f"frame_{current_index:06d}_current.png",
            current_index,
        )
        self.session.update(
            current_frame=frame,
            status="waiting_for_annotation",
            bootstrap_complete=True,
        )
        self._save_session()
        self._save_checkpoint(save_frame=True)

    def _save_session(self) -> None:
        _write_json(self.session_path, self.session)

    def _llm_log_path(self, frame_index: int) -> Path:
        """Allocate an immutable planner/risk log path for one observation frame."""
        base_path = self.session_dir / f"llm_{frame_index:06d}.txt"
        retry_paths = sorted(
            self.session_dir.glob(f"llm_{frame_index:06d}_retry_*.txt")
        )
        if not base_path.exists() and not retry_paths:
            return base_path
        retry_index = 1
        while (self.session_dir / f"llm_{frame_index:06d}_retry_{retry_index:02d}.txt").exists():
            retry_index += 1
        return self.session_dir / f"llm_{frame_index:06d}_retry_{retry_index:02d}.txt"

    def _write_safety_replan_preview(self, frame_index: int, proposed: Any) -> None:
        """Persist a model safety-plan response before its first action executes."""
        raw_output = getattr(self.planner_adapter, "last_safety_plan_raw_output", None)
        if raw_output is None:
            return
        payload = {
            "frame_index": frame_index,
            "raw_output": raw_output,
            "payload": getattr(self.planner_adapter, "last_safety_plan_payload", None),
            "prepared_action": proposed.to_legacy_plan(),
        }
        _write_json(
            self.session_dir / f"safety_replan_frame_{frame_index:06d}.json",
            payload,
        )

    def _install_video_callback(self) -> None:
        tracker = self.benchmark.tracker
        tracker.video_fps = float(self.args.video_fps)
        original = self.benchmark.executor.step_callback

        def capture(context: Any) -> None:
            if original is not None:
                original(context)
            if context.global_step_index % self.video_capture_interval:
                return
            tracker.track_video_rgb(
                capture_robot_rgb_frame(
                    self.benchmark.env.robots[0], self.video_output_size
                )
            )

        self.benchmark.executor.step_callback = capture

    def _capture_initial_frame(self) -> None:
        if self.session.get("current_frame"):
            return
        frame = _capture(
            self.benchmark,
            self.observer,
            self.session_dir / "frame_000000_current.png",
            0,
        )
        self.session.update(current_frame=frame, status="waiting_for_annotation")
        self._save_session()

    def _runtime_checkpoint(self) -> Dict[str, Any]:
        scheduler = self.benchmark.runtime_controller.components.scheduler
        adapter = self.planner_adapter
        adapter_state = {
            name: deepcopy(getattr(adapter, name))
            for name in (
                "_start_step",
                "_preflight_done",
                "_loading",
                "_root_action",
                "_safety_goal",
                "_steps",
                "_inflight",
            )
            if hasattr(adapter, name)
        }
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "sim_state": self._dump_sim_state(),
            "robot_pose": self._robot_pose_checkpoint(),
            "task_object_states": self._task_object_state_checkpoint(),
            "task_object_names": self._task_object_names(),
            "robot_name": self.benchmark.env.robots[0].name,
            "global_scene": self.accumulator.to_state(),
            "object_registry": self.benchmark.runtime_controller.components.objects.to_dict(),
            "scheduler": scheduler.to_dict(),
            "executor_global_step": self.benchmark.executor.global_step_index,
            "updater_global_step": self.updater.global_step_index,
            "active_subtask_index": self.active_subtask_index,
            "active_subtask_action_start": self.active_subtask_action_start,
            "physical_held_object_id": self.benchmark._current_grasped_object_id(),
            "symbolic_carry_checkpoint": self._symbolic_carry_checkpoint(),
            "symbolic_carry": self._symbolic_carry_diagnostics(),
            "cooked_particle_payloads": self.benchmark.executor.cooked_particle_payload_checkpoint(),
            "runtime_controller": {
                "last_review": deepcopy(self.benchmark.runtime_controller.last_review),
                "last_outcome": deepcopy(self.benchmark.runtime_controller.last_outcome),
                "proposal_count": self.benchmark.runtime_controller.proposal_count,
                "pending_scheduler_state_changes": deepcopy(
                    self.benchmark.runtime_controller._pending_scheduler_state_changes
                ),
            },
            "agent": {
                "current_step": self.agent.current_step,
                "pending_manipulation": deepcopy(self.agent._pending_manipulation),
                "subtask_plan_start": self.agent._subtask_plan_start,
            },
            "planner_adapter": adapter_state,
            "lifelong_evaluator": {
                "results": deepcopy(self.lifelong_evaluator.results),
                "process_results": deepcopy(self.lifelong_evaluator._process_results),
                "awareness_results": deepcopy(self.lifelong_evaluator._awareness_results),
            },
            "tracker": {
                "plans": deepcopy(self.benchmark.tracker.plans),
                "raw_outputs": deepcopy(self.benchmark.tracker.raw_outputs),
                "risk_evaluations": deepcopy(self.benchmark.tracker.risk_evaluations),
                "risk_predictions": deepcopy(self.benchmark.tracker.risk_predictions),
                "execution_diagnostics": deepcopy(self.benchmark.tracker.execution_diagnostics),
            },
            "session": deepcopy(self.session),
        }

    @staticmethod
    def _dump_sim_state() -> Any:
        import omnigibson as og

        return og.sim.dump_state(serialized=False)

    def _robot_pose_checkpoint(self) -> Dict[str, Any]:
        """Store the base pose explicitly for reliable post-load restoration."""

        robot = self.benchmark.env.robots[0]
        position, orientation = robot.get_position_orientation(frame="scene")
        checkpoint = {
            "position": position.detach().cpu().clone(),
            "orientation": orientation.detach().cpu().clone(),
        }
        for name, getter in (
            ("joint_positions", "get_joint_positions"),
            ("joint_velocities", "get_joint_velocities"),
        ):
            value = getattr(robot, getter, lambda: None)()
            if value is not None:
                checkpoint[name] = value.detach().cpu().clone()
        return checkpoint

    def _restore_robot_pose(self, payload: Mapping[str, Any]) -> None:
        """Reapply the saved base pose after OmniGibson restores object state."""

        pose = payload.get("robot_pose")
        if not isinstance(pose, Mapping):
            raise ValueError("physical checkpoint has no explicit robot pose")
        position = pose.get("position")
        orientation = pose.get("orientation")
        if position is None or orientation is None:
            raise ValueError("physical checkpoint robot pose is incomplete")
        robot = self.benchmark.env.robots[0]
        robot.set_position_orientation(
            position=torch.as_tensor(position, dtype=torch.float32),
            orientation=torch.as_tensor(orientation, dtype=torch.float32),
            frame="scene",
        )
        joint_positions = pose.get("joint_positions")
        if joint_positions is not None:
            robot.set_joint_positions(torch.as_tensor(joint_positions, dtype=torch.float32))
        joint_velocities = pose.get("joint_velocities")
        if joint_velocities is not None:
            robot.set_joint_velocities(torch.as_tensor(joint_velocities, dtype=torch.float32))

    def _task_object_names(self) -> Dict[str, str]:
        """Bind pose-bearing stable task entities to transient simulator names."""

        scope = getattr(self.benchmark.env.task, "object_scope", {}) or {}
        names = {}
        for entity_id, reference in scope.items():
            obj = getattr(reference, "wrapped_obj", None)
            if not callable(getattr(obj, "get_position_orientation", None)):
                continue
            name = str(getattr(obj, "name", "") or "").strip()
            if name:
                names[str(entity_id)] = name
        return names

    @staticmethod
    def _open_state(obj: Any) -> Optional[bool]:
        for state_type, state in getattr(obj, "states", {}).items():
            if getattr(state_type, "__name__", "") != "Open":
                continue
            try:
                return bool(state.get_value())
            except Exception:
                return None
        return None

    def _task_object_state_checkpoint(self) -> Dict[str, Dict[str, Any]]:
        states: Dict[str, Dict[str, Any]] = {}
        scope = getattr(self.benchmark.env.task, "object_scope", {}) or {}
        for entity_id, reference in scope.items():
            obj = getattr(reference, "wrapped_obj", None)
            if not callable(getattr(obj, "get_position_orientation", None)):
                continue
            position, orientation = obj.get_position_orientation(frame="scene")
            states[str(entity_id)] = {
                "position": position.detach().cpu().clone(),
                "orientation": orientation.detach().cpu().clone(),
                "open": self._open_state(obj),
            }
        return states

    @staticmethod
    def _validate_task_object_mapping(
        saved_names: Mapping[str, Any],
        current_names: Mapping[str, str],
        registry: Mapping[str, Any],
    ) -> None:
        saved = {str(entity_id): str(name).strip() for entity_id, name in saved_names.items()}
        current = {str(entity_id): str(name).strip() for entity_id, name in current_names.items()}
        if not saved or set(saved) != set(current):
            raise ValueError("checkpoint task entity IDs do not match the current task")
        if any(not name for name in saved.values()) or any(not name for name in current.values()):
            raise ValueError("checkpoint task-object mapping contains an empty simulator name")
        if len(set(saved.values())) != len(saved) or len(set(current.values())) != len(current):
            raise ValueError("checkpoint task-object mapping is not one-to-one")
        missing = [name for name in saved.values() if name not in registry]
        if missing:
            raise ValueError(
                "checkpoint is missing simulator state for task objects: " + ", ".join(sorted(missing))
            )

    def _remap_sim_state(self, payload: Mapping[str, Any]) -> Any:
        """Map saved transient object names onto this process's task objects."""

        state = payload.get("sim_state")
        if not isinstance(state, Mapping):
            raise TypeError("physical simulator state must be a mapping")
        remapped = deepcopy(dict(state))
        scene_state = remapped.get(0)
        if not isinstance(scene_state, Mapping):
            raise ValueError("physical simulator state must contain scene zero")
        registry = scene_state.get("object_registry")
        if not isinstance(registry, Mapping):
            raise ValueError("physical simulator state has no object registry")
        remapped_registry = dict(registry)
        saved_names = payload.get("task_object_names")
        if not isinstance(saved_names, Mapping):
            raise ValueError("physical checkpoint has no task-object name mapping")
        current_names = self._task_object_names()
        self._validate_task_object_mapping(saved_names, current_names, remapped_registry)
        task_states = {
            entity_id: remapped_registry[str(saved_names[entity_id]).strip()]
            for entity_id in current_names
        }
        for saved_name in saved_names.values():
            remapped_registry.pop(str(saved_name).strip())
        for entity_id, current_name in current_names.items():
            remapped_registry[current_name] = task_states[entity_id]
        saved_robot_name = str(payload.get("robot_name") or "").strip()
        current_robot_name = self.benchmark.env.robots[0].name
        if not saved_robot_name or saved_robot_name not in remapped_registry:
            raise ValueError("checkpoint is missing simulator state for its robot")
        saved_robot_state = remapped_registry.pop(saved_robot_name)
        remapped_registry[current_robot_name] = saved_robot_state
        remapped[0] = {**dict(scene_state), "object_registry": remapped_registry}
        self._remove_suspended_symbolic_particle_system_states(remapped, payload)
        return remapped

    @staticmethod
    def _remove_suspended_symbolic_particle_system_states(
        sim_state: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        """Skip empty instancers whose payload is serialized by symbolic carry.

        OmniGibson cannot reliably load an initialized physical-particle system
        with an empty instancer: its renderer repeatedly rebuilds a prototype
        that no longer exists.  The corresponding particles are not lost;
        symbolic carry stores their complete local-frame payload separately and
        restores it when the held object is released.
        """
        carry = payload.get("symbolic_carry_checkpoint") or {}
        suspended_systems = {
            str(item.get("system_name") or "").strip()
            for item in carry.get("particle_states", ())
            if bool(item.get("suspended"))
        }
        if not suspended_systems:
            return
        scene_state = sim_state.get(0)
        registry = scene_state.get("system_registry") if isinstance(scene_state, Mapping) else None
        if not isinstance(registry, dict):
            return
        for system_name in suspended_systems:
            state = registry.get(system_name)
            if not isinstance(state, Mapping):
                continue
            counts = torch.as_tensor(state.get("instancer_particle_counts", ()))
            if counts.numel() and int(counts.sum().item()) != 0:
                raise ValueError(
                    "symbolic carry marks a non-empty particle system suspended: "
                    f"{system_name}"
                )
            registry.pop(system_name, None)

    @staticmethod
    def _particle_system_counts(sim_state: Any) -> Dict[str, int]:
        """Return the saved particle count for every active physical system."""

        if not isinstance(sim_state, Mapping):
            raise TypeError("physical simulator state must be a mapping")
        scene_state = sim_state.get(0)
        if not isinstance(scene_state, Mapping):
            raise ValueError("physical simulator state must contain scene zero")
        registry = scene_state.get("system_registry")
        if not isinstance(registry, Mapping):
            raise ValueError("physical simulator state has no system registry")
        counts: Dict[str, int] = {}
        for name, state in registry.items():
            if not isinstance(state, Mapping):
                raise ValueError(f"invalid saved state for particle system {name!r}")
            if "instancer_particle_counts" in state:
                values = torch.as_tensor(state["instancer_particle_counts"])
                counts[str(name)] = int(values.sum().item())
            elif "n_particles" in state:
                counts[str(name)] = int(torch.as_tensor(state["n_particles"]).item())
            else:
                counts[str(name)] = 0
        return counts

    def _initialize_missing_particle_systems(self, payload: Mapping[str, Any]) -> Dict[str, int]:
        """Initialize every checkpointed system before OmniGibson loads particles."""

        expected = self._particle_system_counts(payload.get("sim_state"))
        scene = self.benchmark.env._scene
        active = {
            str(system.name)
            for system in getattr(scene.system_registry, "objects", ())
        }
        for system_name in expected:
            if system_name in active:
                continue
            if system_name not in scene.available_systems:
                raise ValueError(
                    "checkpoint particle system is not available in the rebuilt scene: "
                    f"{system_name}"
                )
            scene.get_system(system_name, force_init=True)
        initialized = {
            str(system.name)
            for system in getattr(scene.system_registry, "objects", ())
        }
        missing = sorted(set(expected) - initialized)
        if missing:
            raise RuntimeError(
                "checkpoint particle systems were not initialized: " + ", ".join(missing)
            )
        return expected

    def _checkpoint_path_for_frame(self, frame_index: Optional[int]) -> Path:
        if frame_index is None:
            return self.checkpoint_path
        if frame_index < 0:
            raise ValueError("frame checkpoint index must be non-negative")
        return self.checkpoint_dir / f"frame_{frame_index:06d}.pt"

    def _checkpoint_manifest_path_for_frame(self, frame_index: int) -> Path:
        return self.checkpoint_dir / f"frame_{frame_index:06d}.json"

    def _checkpoint_manifest(self, payload: Mapping[str, Any], *, physical_state: str) -> Dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "physical_state": physical_state,
            "frame_index": self.session["current_frame"]["frame_index"],
            "completed_action_count": len(self.session["completed_actions"]),
            "executor_global_step": self.benchmark.executor.global_step_index,
            "active_subtask_index": self.active_subtask_index,
            "scheduler": self.benchmark.runtime_controller.components.scheduler.to_dict(),
            "global_scene_summary": self.accumulator.snapshot().to_dict().get("summary", {}),
            "task_entity_ids": sorted(payload["task_object_names"]),
            "held_object": payload["physical_held_object_id"],
            "cooked_particle_payloads": [
                {
                    "entity_id": item["entity_id"],
                    "system_name": item["system_name"],
                    "particle_count": int(item["local_positions"].shape[0]),
                }
                for item in payload.get("cooked_particle_payloads", ())
            ],
        }

    def _save_checkpoint(self, *, save_frame: bool = False) -> Path:
        payload = self._runtime_checkpoint()
        frame_index = int(self.session["current_frame"]["frame_index"])
        frame_path = self._checkpoint_path_for_frame(frame_index)
        if save_frame:
            if frame_path.exists():
                raise FileExistsError(
                    "refusing to overwrite immutable frame checkpoint: "
                    f"{frame_path}; restore it in a separate session directory before branching"
                )
            frame_temp_path = self.checkpoint_dir / f"frame_{frame_index:06d}.tmp.pt"
            torch.save(payload, frame_temp_path)
            frame_temp_path.replace(frame_path)
            _write_json(
                self._checkpoint_manifest_path_for_frame(frame_index),
                self._checkpoint_manifest(payload, physical_state=frame_path.name),
            )
        temp_path = self.checkpoint_dir / "latest.tmp.pt"
        torch.save(payload, temp_path)
        if self.checkpoint_path.exists():
            shutil.copy2(self.checkpoint_path, self.previous_checkpoint_path)
        temp_path.replace(self.checkpoint_path)
        _write_json(
            self.manifest_path,
            self._checkpoint_manifest(payload, physical_state=self.checkpoint_path.name),
        )
        return frame_path if save_frame else self.checkpoint_path

    def checkpoint(self) -> Path:
        """Persist the live checkpoint and its reconciled global scene graph."""

        snapshot = self.accumulator.snapshot()
        _write_scene_graph_artifacts(
            self.scene_graph_dir,
            frame_index=int(self.session["current_frame"]["frame_index"]),
            current_frame_snapshot=snapshot,
            global_snapshot=snapshot,
        )
        return self._save_checkpoint()

    @staticmethod
    def _checkpoint_held_object_id(payload: Mapping[str, Any]) -> Optional[str]:
        if "physical_held_object_id" in payload:
            saved = str(payload.get("physical_held_object_id") or "").strip()
            return saved or None
        saved = str(payload.get("physical_held_object_id") or "").strip()
        if saved:
            return saved
        held = [
            str(record.get("entity_id") or "").strip()
            for record in (payload.get("object_registry") or {}).get("objects") or ()
            if bool((record.get("states") or {}).get("held_by_robot"))
        ]
        held = [entity_id for entity_id in held if entity_id]
        if len(held) > 1:
            raise ValueError("checkpoint has multiple objects marked held_by_robot")
        return held[0] if held else None

    def _clear_unexpected_restored_grasp(self, expected_held_object_id: Optional[str]) -> None:
        """Release stale simulator grasp records when a checkpoint declares empty hands."""

        current_held_object_id = self.benchmark._current_grasped_object_id()
        if expected_held_object_id is not None or current_held_object_id is None:
            return
        controller = self.benchmark.executor.controller
        clear = getattr(controller, "_clear_symbolic_grasp_state", None)
        if callable(clear):
            clear(getattr(controller, "arm", "0"))
        else:
            self.benchmark.env.robots[0].release_grasp_immediately()
        actual_held_object_id = self.benchmark._current_grasped_object_id()
        if actual_held_object_id is not None:
            raise RuntimeError(
                "checkpoint declares empty hands but retained a simulator grasp: "
                f"{actual_held_object_id!r}"
            )
        self.benchmark._starter_grasped_object = None

    def _restore_physical_grasp(self, held_object_id: Optional[str]) -> None:
        if held_object_id is None:
            return
        current = self.benchmark._current_grasped_object_id()
        if current == held_object_id:
            self.benchmark._starter_grasped_object = held_object_id
            return
        if current is not None:
            raise RuntimeError(
                "physical checkpoint restored an unexpected held object: "
                f"expected={held_object_id!r} actual={current!r}"
            )
        object_ref = self.benchmark.env.task.object_scope.get(held_object_id)
        if object_ref is None:
            raise KeyError(f"checkpoint held object is absent from task scope: {held_object_id}")
        obj = object_ref.wrapped_obj
        controller = self.benchmark.executor.controller
        arm = getattr(controller, "arm", None)
        if arm is None:
            raise RuntimeError("starter controller has no active arm for grasp restoration")
        robot = self.benchmark.env.robots[0]
        eef_position = robot.eef_links[arm].get_position_orientation()[0]
        object_position = obj.get_position_orientation()[0]
        robot._establish_grasp(
            arm=arm,
            ag_data=(obj, obj.root_link),
            contact_pos=(eef_position + object_position) / 2.0,
        )
        restored = self.benchmark._current_grasped_object_id()
        if restored != held_object_id:
            raise RuntimeError(
                "physical grasp restoration failed: "
                f"expected={held_object_id!r} actual={restored!r}"
            )
        self.benchmark._starter_grasped_object = restored

    def _synchronize_symbolic_carry(self) -> bool:
        controller = self.benchmark.executor.controller
        synchronize = getattr(controller, "synchronize_symbolic_carry", None)
        return bool(callable(synchronize) and synchronize())

    def _symbolic_carry_diagnostics(self) -> Optional[Dict[str, Any]]:
        diagnostics = getattr(
            self.benchmark.executor.controller, "symbolic_carry_diagnostics", None
        )
        return diagnostics() if callable(diagnostics) else None

    def _symbolic_carry_checkpoint(self) -> Optional[Dict[str, Any]]:
        checkpoint = getattr(
            self.benchmark.executor.controller, "symbolic_carry_checkpoint", None
        )
        return checkpoint() if callable(checkpoint) else None

    def _has_symbolic_carry_state(self) -> bool:
        checker = getattr(
            self.benchmark.executor.controller, "has_symbolic_carry_state", None
        )
        return bool(callable(checker) and checker())

    @staticmethod
    def _assert_close(
        *, label: str, expected: Any, actual: Any, atol: float, quaternion: bool = False
    ) -> None:
        expected_tensor = torch.as_tensor(expected, dtype=torch.float32)
        actual_tensor = torch.as_tensor(actual, dtype=torch.float32)
        if expected_tensor.shape != actual_tensor.shape:
            raise RuntimeError(
                f"checkpoint restore mismatch for {label}: "
                f"expected shape {tuple(expected_tensor.shape)}, actual {tuple(actual_tensor.shape)}"
            )
        if quaternion:
            equal = torch.allclose(expected_tensor, actual_tensor, atol=atol, rtol=0.0)
            opposite = torch.allclose(expected_tensor, -actual_tensor, atol=atol, rtol=0.0)
            if equal or opposite:
                return
        elif torch.allclose(expected_tensor, actual_tensor, atol=atol, rtol=0.0):
            return
        maximum = float(torch.max(torch.abs(expected_tensor - actual_tensor)).item())
        raise RuntimeError(
            f"checkpoint restore mismatch for {label}: maximum absolute error={maximum:.6f}"
        )

    def _validate_restored_checkpoint(
        self,
        payload: Mapping[str, Any],
        *,
        expected_particle_counts: Mapping[str, int],
    ) -> Dict[str, Any]:
        saved_object_states = payload.get("task_object_states")
        if not isinstance(saved_object_states, Mapping):
            raise ValueError("physical checkpoint has no task-object state verification data")
        expected_names = payload.get("task_object_names")
        if not isinstance(expected_names, Mapping):
            raise ValueError("physical checkpoint has no task-object mapping")
        current_names = self._task_object_names()
        self._validate_task_object_mapping(expected_names, current_names, {
            name: object() for name in expected_names.values()
        })
        if set(saved_object_states) != set(current_names):
            raise ValueError("checkpoint task-object states do not match the current task")
        expected_robot = payload.get("robot_pose")
        if not isinstance(expected_robot, Mapping):
            raise ValueError("physical checkpoint has no robot verification data")
        actual_robot = self._robot_pose_checkpoint()
        self._assert_close(
            label="robot position",
            expected=expected_robot["position"],
            actual=actual_robot["position"],
            atol=0.005,
        )
        self._assert_close(
            label="robot orientation",
            expected=expected_robot["orientation"],
            actual=actual_robot["orientation"],
            atol=0.005,
            quaternion=True,
        )
        self._assert_close(
            label="robot joint positions",
            expected=expected_robot["joint_positions"],
            actual=actual_robot["joint_positions"],
            atol=0.03,
        )
        scope = getattr(self.benchmark.env.task, "object_scope", {}) or {}
        for entity_id, expected in saved_object_states.items():
            reference = scope.get(str(entity_id))
            obj = getattr(reference, "wrapped_obj", None)
            if not callable(getattr(obj, "get_position_orientation", None)):
                raise RuntimeError(
                    f"checkpoint task object is not pose-bearing: {entity_id}"
                )
            position, orientation = obj.get_position_orientation(frame="scene")
            self._assert_close(
                label=f"{entity_id} position",
                expected=expected["position"],
                actual=position,
                atol=0.03,
            )
            self._assert_close(
                label=f"{entity_id} orientation",
                expected=expected["orientation"],
                actual=orientation,
                atol=0.02,
                quaternion=True,
            )
            expected_open = expected.get("open")
            if expected_open is not None and self._open_state(obj) != bool(expected_open):
                raise RuntimeError(
                    f"checkpoint restore mismatch for {entity_id} open state"
                )
        expected_held = self._checkpoint_held_object_id(payload)
        actual_held = self.benchmark._current_grasped_object_id()
        if expected_held != actual_held:
            raise RuntimeError(
                "checkpoint restore mismatch for held object: "
                f"expected={expected_held!r} actual={actual_held!r}"
            )
        carry = self._symbolic_carry_diagnostics()
        if expected_held is not None:
            if carry is None:
                raise RuntimeError("checkpoint restore lost symbolic carried-object state")
            if carry["position_error"] > 0.02:
                raise RuntimeError(
                    "checkpoint restore mismatch for symbolic carried-object pose: "
                    f"error={carry['position_error']:.6f}"
                )
        elif carry is not None or self._has_symbolic_carry_state():
            raise RuntimeError(
                "checkpoint declares empty hands but retained symbolic carry state"
            )
        actual_particle_counts = self._particle_system_counts(self._dump_sim_state())
        if dict(expected_particle_counts) != actual_particle_counts:
            raise RuntimeError(
                "checkpoint restore mismatch for particle systems: "
                f"expected={dict(expected_particle_counts)} actual={actual_particle_counts}"
            )
        payload_diagnostics = self.benchmark.executor.cooked_particle_payload_diagnostics()
        incomplete_payloads = [
            item for item in payload_diagnostics if not bool(item["contained"])
        ]
        if incomplete_payloads:
            raise RuntimeError(
                "checkpoint restore mismatch for cooked particle containment: "
                f"{incomplete_payloads}"
            )
        if expected_held is None:
            suspended_payloads = [
                item
                for item in payload_diagnostics
                if bool(item["suspended_by_symbolic_carry"])
            ]
            if suspended_payloads:
                raise RuntimeError(
                    "checkpoint declares empty hands but left particle payload suspended: "
                    f"{suspended_payloads}"
                )
        return {
            "status": "passed",
            "task_entity_count": len(current_names),
            "held_object": actual_held,
            "symbolic_carry": carry,
            "symbolic_carry_empty": not self._has_symbolic_carry_state(),
            "robot_position": actual_robot["position"].tolist(),
            "particle_system_counts": actual_particle_counts,
            "cooked_particle_payloads": payload_diagnostics,
        }

    def _restore_checkpoint(self, checkpoint_path: Path) -> None:
        payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported physical checkpoint")
        import omnigibson as og

        expected_particle_counts = self._initialize_missing_particle_systems(payload)
        og.sim.load_state(self._remap_sim_state(payload), serialized=False)
        self._restore_robot_pose(payload)
        controller = self.benchmark.executor.controller
        synchronize_after_restore = getattr(
            controller, "synchronize_after_state_restore", None
        )
        controller_synchronized = bool(
            callable(synchronize_after_restore) and synchronize_after_restore()
        )
        held_object_id = self._checkpoint_held_object_id(payload)
        self._clear_unexpected_restored_grasp(held_object_id)
        self._restore_physical_grasp(held_object_id)
        restore_carry = getattr(controller, "restore_symbolic_carry_after_state_restore", None)
        if callable(restore_carry):
            object_ref = (getattr(self.benchmark.env.task, "object_scope", {}) or {}).get(
                held_object_id
            )
            restore_carry(
                getattr(object_ref, "wrapped_obj", None),
                payload.get("symbolic_carry_checkpoint"),
            )
        payloads = payload.get("cooked_particle_payloads") or ()
        if payloads:
            self.benchmark.executor.restore_cooked_particle_payloads(payloads)
        else:
            self.benchmark.executor.recover_cooked_particle_payloads_from_live_state()
        self._synchronize_symbolic_carry()
        validation = self._validate_restored_checkpoint(
            payload,
            expected_particle_counts=expected_particle_counts,
        )
        validation["physical_state"] = checkpoint_path.name
        validation["controller_synchronized_after_restore"] = controller_synchronized
        validation["frame_index"] = int(
            (payload.get("session") or {}).get("current_frame", {}).get("frame_index", -1)
        )
        _write_json(self.checkpoint_dir / "restore_validation.json", validation)
        self.accumulator.load_state(payload["global_scene"])
        self.updater.global_step_index = int(payload["updater_global_step"])
        self.benchmark.runtime_controller.components.objects.load_dict(payload["object_registry"])
        self.updater.snapshot = self.accumulator.apply_missing_object_registry_states(
            self.benchmark.runtime_controller.components.objects
        )
        scheduler = self.benchmark.runtime_controller.components.scheduler
        scheduler.load_pending(payload["scheduler"].get("pending", ()))
        self.benchmark.executor.global_step_index = int(payload["executor_global_step"]) + 1
        self.active_subtask_index = int(payload["active_subtask_index"])
        self.active_subtask_action_start = int(
            payload.get("active_subtask_action_start", 0)
        )
        lifelong_state = payload.get("lifelong_evaluator") or {}
        self.lifelong_evaluator.results = list(lifelong_state.get("results") or ())
        self.lifelong_evaluator._process_results = {
            int(index): values
            for index, values in dict(
                lifelong_state.get("process_results")
                or self.lifelong_evaluator._process_results
            ).items()
        }
        self.lifelong_evaluator._awareness_results = {
            int(index): values
            for index, values in dict(
                lifelong_state.get("awareness_results") or ()
            ).items()
        }
        self.benchmark.set_active_subtask(self.active_subtask_index)
        self.agent.begin_lifelong_subtask(
            task_instruction=_subtask_instruction(self.benchmark, self.active_subtask_index),
            subtask_index=self.active_subtask_index,
        )
        agent_state = payload["agent"]
        self.agent.current_step = int(agent_state["current_step"])
        self.agent._pending_manipulation = agent_state["pending_manipulation"]
        self.agent._subtask_plan_start = int(agent_state["subtask_plan_start"])
        controller_state = payload.get("runtime_controller") or {}
        self.benchmark.runtime_controller.last_review = controller_state.get("last_review")
        self.benchmark.runtime_controller.last_outcome = controller_state.get("last_outcome")
        self.benchmark.runtime_controller.proposal_count = int(
            controller_state.get("proposal_count") or 0
        )
        self.benchmark.runtime_controller._pending_scheduler_state_changes = list(
            controller_state.get("pending_scheduler_state_changes") or ()
        )
        for name, value in payload.get("planner_adapter", {}).items():
            setattr(self.planner_adapter, name, value)
        # Python generators cannot be checkpointed. The AgentPlanner's saved
        # pending manipulation and recorded tracker history recreate the next
        # physical-starter operation when its adapter is queried again.
        base_adapter = getattr(self.planner_adapter, "base", None)
        if base_adapter is not None:
            base_adapter._iterator = None
        if hasattr(self.planner_adapter, "_inflight"):
            inflight = self.planner_adapter._inflight
            if inflight is not None:
                inflight["outcome_marker"] = None
        for name, value in payload["tracker"].items():
            setattr(self.benchmark.tracker, name, value)
        self.session = self._rebase_restored_session(
            payload["session"], checkpoint_path.parent.parent
        )
        restored_frame_index = int(self.session["current_frame"]["frame_index"])
        source_frame = Path(
            str((payload.get("session") or {}).get("current_frame", {}).get("image") or "")
        )
        target_frame = self.session_dir / f"frame_{restored_frame_index:06d}_current.png"
        if source_frame.is_file() and source_frame.resolve() != target_frame.resolve():
            shutil.copy2(source_frame, target_frame)
        if not target_frame.is_file():
            raise FileNotFoundError(
                "checkpoint has no reusable native current-frame image: "
                f"{source_frame}"
            )
        self.session["current_frame"]["image"] = str(target_frame)
        self.session.update(status="waiting_for_annotation", restored_from_checkpoint=True)
        self.session["active_subtask_action_start"] = self.active_subtask_action_start
        self._save_session()
        if not self._checkpoint_path_for_frame(restored_frame_index).exists():
            self._save_checkpoint(save_frame=True)
        self.restored_from_checkpoint = True

    def _finish_active_subtask(self, action_end_index: int) -> Dict[str, Any]:
        """Evaluate a successful DONE before exposing the next lifelong subtask."""

        result = self.lifelong_evaluator.finish_subtask(
            subtask_index=self.active_subtask_index,
            action_start_index=self.active_subtask_action_start,
            action_end_index=action_end_index,
            termination_reason="done",
            instruction=_subtask_instruction(self.benchmark, self.active_subtask_index),
        )
        result_dict = result.to_dict()
        if not result.safe_success:
            raise RuntimeError(
                "planner emitted DONE before the active subtask goal was satisfied: "
                + json.dumps(result_dict, ensure_ascii=False)
            )
        if self.active_subtask_index >= len(self.lifelong_evaluator.subtasks):
            return result_dict
        self.active_subtask_index += 1
        self.active_subtask_action_start = action_end_index
        self.benchmark.set_active_subtask(self.active_subtask_index)
        self.agent.begin_lifelong_subtask(
            task_instruction=_subtask_instruction(self.benchmark, self.active_subtask_index),
            subtask_index=self.active_subtask_index,
        )
        base_adapter = getattr(self.planner_adapter, "base", None)
        if base_adapter is not None:
            base_adapter._iterator = None
        if hasattr(self.planner_adapter, "_inflight"):
            self.planner_adapter._inflight = None
        return result_dict

    def _rebase_restored_session(
        self,
        saved_session: Mapping[str, Any],
        source_session_dir: Path,
    ) -> Dict[str, Any]:
        """Keep a restored branch's artifacts in its requested session directory."""

        session = deepcopy(dict(saved_session))
        source_prefix = str(source_session_dir.resolve())
        target_prefix = str(self.session_dir)
        if source_prefix == target_prefix:
            return session

        if self._session_existed_at_start:
            previous_branch_source = str(
                self.session.get("snapshot_branch_source") or ""
            )
            has_immutable_frames = any(self.checkpoint_dir.glob("frame_*.pt"))
            if (
                previous_branch_source != source_prefix
                or has_immutable_frames
            ):
                raise ValueError(
                    "refusing to restore an external checkpoint into an existing session"
                )

        def rebase(value: Any) -> Any:
            if isinstance(value, str):
                return (
                    target_prefix + value[len(source_prefix):]
                    if value.startswith(source_prefix)
                    else value
                )
            if isinstance(value, list):
                return [rebase(item) for item in value]
            if isinstance(value, dict):
                return {key: rebase(item) for key, item in value.items()}
            return value

        session = rebase(session)
        session["snapshot_branch_source"] = str(source_session_dir)
        return session

    def restore_frame(self, frame_index: int) -> Dict[str, Any]:
        checkpoint_path = self._checkpoint_path_for_frame(frame_index)
        if not checkpoint_path.is_file():
            raise FileNotFoundError(f"immutable frame checkpoint does not exist: {checkpoint_path}")
        self._restore_checkpoint(checkpoint_path)
        return {
            "status": "restored",
            "frame_index": frame_index,
            "checkpoint": str(checkpoint_path),
            "current_frame": self.session["current_frame"],
        }

    def _save_action_video(self, frame_index: int) -> Optional[str]:
        tracker = self.benchmark.tracker
        if not tracker.video_cache:
            return None
        path = self.video_dir / f"frame_{frame_index:06d}.mp4"
        tracker.save_video(str(path))
        tracker.video_cache.clear()
        return str(path)

    def _post_action_physical_settle(self) -> Dict[str, Any]:
        """Advance real hold frames before a successful-frame checkpoint."""

        robot = self.benchmark.env.robots[0]
        executor = self.benchmark.executor
        start_position, _ = robot.get_position_orientation(frame="scene")
        start_velocity = robot.get_joint_velocities()
        for step_index in range(self.post_action_settle_steps):
            self._synchronize_symbolic_carry()
            executor._step_environment(
                executor.get_hold_action(),
                raw_plan="post_action_physical_settle",
                primitive_name="POST_ACTION_SETTLE",
                step_index=step_index,
            )
            self._synchronize_symbolic_carry()
        executor._synchronize_cooked_particle_payloads()
        end_position, _ = robot.get_position_orientation(frame="scene")
        end_velocity = robot.get_joint_velocities()
        return {
            "steps": self.post_action_settle_steps,
            "base_displacement": round(
                float(torch.norm(end_position - start_position).item()), 6
            ),
            "max_joint_velocity_before": round(
                float(torch.max(torch.abs(start_velocity)).item()), 6
            ),
            "max_joint_velocity_after": round(
                float(torch.max(torch.abs(end_velocity)).item()), 6
            ),
        }

    def advance(self, perception_json: str) -> Dict[str, Any]:
        current_frame = dict(self.session.get("current_frame") or {})
        if not current_frame:
            raise RuntimeError("session has no current frame")
        frame_index = int(current_frame["frame_index"])
        source = Path(perception_json).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"perception JSON does not exist: {source}")
        annotation = self.annotations_dir / f"frame_{frame_index:06d}.json"
        annotation.write_bytes(source.read_bytes())
        state_changes, current_snapshot, global_snapshot = _observe_manual_frame(
            self.benchmark,
            self.updater,
            self.accumulator,
            annotation,
            frame_index,
            self.scene_graph_dir,
        )
        self.benchmark.tracker.video_cache.clear()
        self.benchmark.tracker.track_video_rgb(
            capture_robot_rgb_frame(self.benchmark.env.robots[0], self.video_output_size)
        )
        proposed = self.benchmark.runtime_controller.propose()
        if proposed is None:
            raw_output = _latest_raw_output(self.benchmark)
            response = {
                "status": "planner_no_action",
                "frame_index": frame_index,
                "planner_raw_output": raw_output,
            }
            _write_json(self.session_dir / f"llm_{frame_index:06d}_planner_failure.json", response)
            self.session.update(status=response["status"], last_result=response)
            self._save_session()
            return response
        self._write_safety_replan_preview(frame_index, proposed)
        executed = self.benchmark.execute_plan(proposed)
        review = self.benchmark.runtime_controller.last_review
        outcome = self.benchmark.runtime_controller.last_outcome
        if review is None or outcome is None:
            raise RuntimeError("benchmark execution did not produce review/outcome")
        raw_output = _latest_raw_output(self.benchmark)
        if not executed or not outcome.succeeded:
            _write_llm_log(
                self.session_dir,
                frame_index=frame_index,
                output_path=self._llm_log_path(frame_index),
                planner_raw_output=raw_output,
                review=review,
                outcome=outcome,
                global_snapshot=global_snapshot,
                benchmark=self.benchmark,
            )
            response = {
                "status": "blocked_or_failed",
                "action": review.action.to_legacy_plan(),
                "decision": review.decision.value,
                "reason": review.reason,
                "outcome": outcome.reason,
            }
            self.session.update(status=response["status"], last_result=response)
            self._save_session()
            return response

        global_snapshot = self.accumulator.apply_successful_action(review.action)
        global_snapshot = self.accumulator.apply_state_changes(
            self.benchmark.runtime_controller.drain_scheduler_state_changes()
        )
        self.updater.snapshot = global_snapshot
        physical_settle = self._post_action_physical_settle()
        _write_scene_graph_artifacts(
            self.scene_graph_dir,
            frame_index=frame_index,
            current_frame_snapshot=current_snapshot,
            global_snapshot=global_snapshot,
        )
        _write_llm_log(
            self.session_dir,
            frame_index=frame_index,
            output_path=self._llm_log_path(frame_index),
            planner_raw_output=raw_output,
            review=review,
            outcome=outcome,
            global_snapshot=global_snapshot,
            benchmark=self.benchmark,
        )
        video_path = self._save_action_video(frame_index)
        completed = list(self.session["completed_actions"])
        completed.append(
            {
                "subtask_index": self.active_subtask_index,
                "action": review.action.to_legacy_plan(),
                "annotation": str(annotation),
                "raw_output": raw_output,
                "first_person_video": video_path,
            }
        )
        next_index = len(completed)
        completed_subtask = self.active_subtask_index
        subtask_result = None
        if review.action.name == "DONE":
            subtask_result = self._finish_active_subtask(next_index)
        next_frame = _capture(
            self.benchmark,
            self.observer,
            self.session_dir / f"frame_{next_index:06d}_current.png",
            next_index,
        )
        response = {
            "status": "waiting_for_annotation",
            "executed_action": review.action.to_legacy_plan(),
            "planner_raw_output": raw_output,
            "subtask_index": completed_subtask,
            "active_subtask_index": self.active_subtask_index,
            "completed_subtask": subtask_result,
            "decision": review.decision.value,
            "state_change_count": len(state_changes),
            "scheduler_pending": [
                process.to_dict()
                for process in self.benchmark.runtime_controller.components.scheduler.pending_for()
            ],
            "post_action_physical_settle": physical_settle,
            "first_person_video": video_path,
            "current_frame": next_frame,
        }
        self.session.update(
            completed_actions=completed,
            current_frame=next_frame,
            status=response["status"],
            last_result=response,
            active_subtask_index=self.active_subtask_index,
            active_subtask_action_start=self.active_subtask_action_start,
        )
        self._save_session()
        frame_checkpoint = self._save_checkpoint(save_frame=True)
        response["frame_checkpoint"] = str(frame_checkpoint)
        return response

    def status(self) -> Dict[str, Any]:
        return {
            **self.session,
            "service": {
                "headless": True,
                "persistent_process": True,
                "restored_from_checkpoint": self.restored_from_checkpoint,
                "checkpoint": str(self.checkpoint_path) if self.checkpoint_path.exists() else None,
                "frame_checkpoints": sorted(
                    str(path)
                    for path in self.checkpoint_dir.glob("frame_*.pt")
                ),
                "physical_held_object": self.benchmark._current_grasped_object_id(),
            },
        }

    def close(self) -> Dict[str, Any]:
        checkpoint_saved = self.session.get("status") != "blocked_or_failed"
        if checkpoint_saved:
            self._save_checkpoint()
        response = self.benchmark.close()
        self.session.update(
            status="closed",
            last_result={
                "close": response,
                "checkpoint_saved": checkpoint_saved,
            },
        )
        self._save_session()
        return response


def serve(args: argparse.Namespace) -> int:
    session = PersistentPhysicalSession(args)
    socket_path = session.session_dir / "control.sock"
    if socket_path.exists():
        socket_path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        server.listen(1)
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    request = json.loads(connection.recv(65536).decode("utf-8"))
                    command = request.get("command")
                    if command == "advance":
                        response = session.advance(str(request.get("perception_json") or ""))
                    elif command == "status":
                        response = session.status()
                    elif command == "checkpoint":
                        checkpoint_path = session.checkpoint()
                        response = {"status": "checkpoint_saved", "path": str(checkpoint_path)}
                    elif command == "restore":
                        response = session.restore_frame(int(request["frame_index"]))
                    elif command == "close":
                        response = session.close()
                        response["status"] = "closed"
                        connection.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                        return 0
                    else:
                        response = {"status": "error", "message": "unknown command"}
                except Exception as exc:
                    response = {
                        "status": "error",
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                connection.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    return serve(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
