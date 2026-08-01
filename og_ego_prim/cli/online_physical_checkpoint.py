"""Online-runner adapter for the existing physical session checkpoint protocol.

This module deliberately subclasses ``PersistentPhysicalSession`` so the
physical dump/load, entity remapping, gripper, camera, particle, and restore
validation behavior stays identical to the long-lived manual-session path.
Only online-specific Python runtime state is added here.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
from typing import Any, Dict, Mapping, Optional

import torch

from og_ego_prim.cli.headless_manual_physical_session import (
    CHECKPOINT_SCHEMA_VERSION,
    PersistentPhysicalSession,
)
from og_ego_prim.cli.headless_manual_physical_step import _write_json
from og_ego_prim.scene_graph.global_state import GlobalSceneGraphAccumulator
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.task_planner.episode import PlannerEpisode, PlannerEpisodeEntry


ONLINE_CHECKPOINT_KIND = "isbench.online_physical_checkpoint.v1"
ONLINE_SESSION_SCHEMA_VERSION = "isbench.online_physical_checkpoint_session.v1"


class OnlinePhysicalCheckpointManager(PersistentPhysicalSession):
    """Persist online benchmark frames with complete physical and runtime state."""

    def __init__(
        self,
        benchmark: Any,
        output_dir: str | Path,
        *,
        task: str,
        scene: str,
        agent: Any,
        planner_adapter: Any,
        post_action_settle_steps: int = 30,
    ) -> None:
        if agent is None:
            raise ValueError("online physical checkpoints require a model planner agent")
        if post_action_settle_steps < 0:
            raise ValueError("post_action_settle_steps must be non-negative")

        self.benchmark = benchmark
        self.agent = agent
        self._bound_planner_adapter = planner_adapter
        self.planner_adapter = self._planner_core(planner_adapter)
        self.updater = benchmark.scene_graph_updater
        self.session_dir = Path(output_dir).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir = self.session_dir / "checkpoint"
        self.checkpoint_dir.mkdir(exist_ok=True)
        self.scene_graph_dir = self.session_dir / "checkpoint_scene_graph"
        self.scene_graph_dir.mkdir(exist_ok=True)
        self.session_path = self.checkpoint_dir / "session.json"
        self.checkpoint_path = self.checkpoint_dir / "latest.pt"
        self.previous_checkpoint_path = self.checkpoint_dir / "previous.pt"
        self.manifest_path = self.checkpoint_dir / "latest.json"
        self.post_action_settle_steps = int(post_action_settle_steps)
        self.observer = ISBenchObservationAdapter(
            sensor_name=getattr(self.updater, "sensor_name", None)
        )
        self.observer.reset()
        self._initial_native_sensor_parent_pose = self._native_sensor_parent_pose()
        self.accumulator = GlobalSceneGraphAccumulator()
        self.accumulator.merge_current_frame(self.updater.get_snapshot())
        self.active_subtask_index = self._active_subtask_index()
        self.active_subtask_action_start = 0
        self.session = {
            "schema_version": ONLINE_SESSION_SCHEMA_VERSION,
            "task": str(task),
            "scene": str(scene),
            "status": "running",
            "current_frame": {"frame_index": 0},
            "completed_actions": [],
            "active_subtask_index": self.active_subtask_index,
            "active_subtask_action_start": self.active_subtask_action_start,
        }
        self.successful_action_count = 0
        self.restored_from_checkpoint = False
        self._save_session()

    @staticmethod
    def _planner_core(adapter: Any) -> Any:
        """Strip observability wrappers but retain the VLM closed-loop adapter."""

        current = adapter
        seen = set()
        while id(current) not in seen and hasattr(current, "planner"):
            seen.add(id(current))
            nested = getattr(current, "planner")
            if nested is None:
                break
            current = nested
        return current

    def _active_subtask_index(self) -> int:
        value = getattr(self.benchmark.runtime_controller, "active_subtask_id", None)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    def _checkpoint_manifest(
        self,
        payload: Mapping[str, Any],
        *,
        physical_state: str,
    ) -> Dict[str, Any]:
        manifest = super()._checkpoint_manifest(payload, physical_state=physical_state)
        manifest.update(
            {
                "checkpoint_kind": ONLINE_CHECKPOINT_KIND,
                "scene_graph_backend": self.updater.backend_name,
                "planner_adapter": type(self.planner_adapter).__name__,
                "planner_memory_entries": len(
                    self.benchmark.runtime_controller.planner_episode
                ),
                "tracker_plan_count": len(self.benchmark.tracker.plans),
                "scheduler_pending_count": len(
                    self.benchmark.runtime_controller.components.scheduler.pending_for()
                ),
            }
        )
        return manifest

    def _runtime_checkpoint(self) -> Dict[str, Any]:
        payload = self._physical_checkpoint_payload()
        payload.update(
            {
                "checkpoint_kind": ONLINE_CHECKPOINT_KIND,
                "online_runtime": {
                    "perception": self.updater.checkpoint_state(),
                    "controller": self._controller_checkpoint_state(),
                    "tracker": self._tracker_checkpoint_state(),
                    "agent": self._agent_checkpoint_state(),
                    "planner_adapter": self._planner_checkpoint_state(),
                },
                "session": deepcopy(self.session),
            }
        )
        return payload

    def _controller_checkpoint_state(self) -> Dict[str, Any]:
        controller = self.benchmark.runtime_controller
        return {
            "active_subtask_id": controller.active_subtask_id,
            "latest_scene": deepcopy(controller.latest_scene),
            "latest_changes": deepcopy(controller.latest_changes),
            "visible_entity_ids": tuple(controller.visible_entity_ids),
            "rethinking_attempts": int(controller.rethinking_attempts),
            "proposal_count": int(controller.proposal_count),
            "emit_planner_proposals": bool(controller._emit_planner_proposals),
            "pending_scheduler_state_changes": deepcopy(
                controller._pending_scheduler_state_changes
            ),
            "last_review": deepcopy(controller.last_review),
            "last_outcome": deepcopy(controller.last_outcome),
            "last_risk_latency": controller.last_risk_latency,
            "planner_episode": controller.planner_episode.to_dict(),
        }

    def _tracker_checkpoint_state(self) -> Dict[str, Any]:
        tracker = self.benchmark.tracker
        return {
            name: deepcopy(value)
            for name, value in tracker.__dict__.items()
            if name != "planner_episode"
        }

    def _agent_checkpoint_state(self) -> Dict[str, Any]:
        names = (
            "current_step",
            "task_instruction",
            "objects_str",
            "goal_description",
            "safety_tips_str",
            "_pending_rethinking_prompt",
            "_pending_manipulation",
            "_last_plan_validation_error",
            "_subtask_plan_start",
            "last_prompt",
            "last_prompt_sequence",
            "prompt_records",
        )
        return {
            name: deepcopy(getattr(self.agent, name))
            for name in names
            if hasattr(self.agent, name)
        }

    def _planner_checkpoint_state(self) -> Dict[str, Any]:
        adapter = self.planner_adapter
        names = (
            "_start_step",
            "_preflight_done",
            "_loading",
            "_root_action",
            "_safety_goal",
            "_steps",
            "_inflight",
            "last_safety_plan_raw_output",
            "last_safety_plan_payload",
        )
        state = {
            name: deepcopy(getattr(adapter, name))
            for name in names
            if hasattr(adapter, name)
        }
        base = getattr(adapter, "base", None)
        return {
            "type": type(adapter).__name__,
            "state": state,
            # Python generators cannot be checkpointed.  AgentPlanner rebuilds
            # its next proposal from restored tracker history after resume.
            "base_iterator_was_active": bool(
                base is not None and getattr(base, "_iterator", None) is not None
            ),
        }

    def _sync_global_scene_memory(self, action: Any) -> Dict[str, Any]:
        current_snapshot = self.updater.get_snapshot()
        self.accumulator.merge_current_frame(current_snapshot)
        self.accumulator.apply_successful_action(action)
        scheduler_changes = self.benchmark.runtime_controller.drain_scheduler_state_changes()
        if scheduler_changes:
            self.accumulator.apply_state_changes(scheduler_changes)
        global_snapshot = self.accumulator.snapshot()
        frame_index = int(self.session["current_frame"]["frame_index"])
        _write_json(
            self.scene_graph_dir / f"frame_{frame_index:06d}.json",
            {
                "current_scene_graph": current_snapshot.to_dict(),
                "global_scene_memory": global_snapshot.to_dict(),
                "scheduler_state_change_count": len(scheduler_changes),
            },
        )
        return {
            "current_summary": current_snapshot.to_dict().get("summary", {}),
            "global_summary": global_snapshot.to_dict().get("summary", {}),
            "scheduler_state_change_count": len(scheduler_changes),
        }

    def save_after_success(self, action_text: str) -> Path:
        """Settle a successful action, then atomically save its immutable frame."""

        controller = self.benchmark.runtime_controller
        outcome = controller.last_outcome
        review = controller.last_review
        if outcome is None or not outcome.executed or not outcome.succeeded or review is None:
            raise RuntimeError("refusing to checkpoint an action without a successful runtime outcome")

        physical_settle = self._post_action_physical_settle()
        frame_index = len(self.session["completed_actions"]) + 1
        self.session["current_frame"] = {"frame_index": frame_index}
        scene_memory = self._sync_global_scene_memory(review.action)
        self.session["completed_actions"].append(
            {
                "action": str(action_text),
                "sim_step": int(self.benchmark.executor.global_step_index),
                "physical_settle": physical_settle,
                "scene_memory": scene_memory,
            }
        )
        self.active_subtask_index = self._active_subtask_index()
        self.session.update(
            status="running",
            active_subtask_index=self.active_subtask_index,
            active_subtask_action_start=self.active_subtask_action_start,
        )
        self.successful_action_count = len(self.session["completed_actions"])
        self._save_session()
        return self._save_checkpoint(save_frame=True)

    def restore(self, checkpoint_path: str | Path) -> Dict[str, Any]:
        """Restore one immutable online physical frame into this fresh process."""

        path = Path(checkpoint_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"physical checkpoint does not exist: {path}")
        payload = torch.load(path, map_location="cpu", weights_only=False)
        if payload.get("checkpoint_kind") != ONLINE_CHECKPOINT_KIND:
            raise ValueError("checkpoint is not an online physical checkpoint")
        validation = self._restore_physical_checkpoint_payload(payload, path)
        self._restore_online_runtime(payload)
        self._render_restored_observation()
        self.restored_from_checkpoint = True
        self.session.update(
            status="restored",
            restored_from_checkpoint=str(path),
            active_subtask_index=self.active_subtask_index,
            active_subtask_action_start=self.active_subtask_action_start,
        )
        self._save_session()
        validation["online_runtime"] = self._online_restore_diagnostics()
        validation["source_checkpoint"] = str(path)
        _write_json(self.checkpoint_dir / "restore_validation.json", validation)
        # Preserve a branch-local latest pointer without rewriting the source's
        # immutable frame.
        self._save_checkpoint()
        return validation

    def _restore_online_runtime(self, payload: Mapping[str, Any]) -> None:
        runtime = payload.get("online_runtime")
        if not isinstance(runtime, Mapping):
            raise ValueError("online checkpoint has no runtime state")
        controller = self.benchmark.runtime_controller
        controller_state = runtime.get("controller")
        if not isinstance(controller_state, Mapping):
            raise ValueError("online checkpoint has no controller state")

        controller.set_subtask(controller_state.get("active_subtask_id"))
        self.updater.restore_checkpoint_state(dict(runtime.get("perception") or {}))
        self.accumulator.load_state(payload["global_scene"])
        controller.components.objects.load_dict(payload["object_registry"])
        scheduler = controller.components.scheduler
        scheduler.load_pending((payload.get("scheduler") or {}).get("pending", ()))
        self.benchmark.executor.global_step_index = int(
            payload.get("executor_global_step") or 0
        ) + 1

        controller.latest_scene = deepcopy(controller_state.get("latest_scene"))
        if controller.latest_scene is None:
            controller.latest_scene = self.updater.get_snapshot()
        controller.latest_changes = tuple(
            deepcopy(controller_state.get("latest_changes") or ())
        )
        controller.visible_entity_ids = tuple(
            controller_state.get("visible_entity_ids") or ()
        )
        controller.rethinking_attempts = int(controller_state.get("rethinking_attempts") or 0)
        controller.proposal_count = int(controller_state.get("proposal_count") or 0)
        controller._emit_planner_proposals = bool(
            controller_state.get("emit_planner_proposals")
        )
        controller._pending_scheduler_state_changes = list(
            deepcopy(controller_state.get("pending_scheduler_state_changes") or ())
        )
        controller.last_review = deepcopy(controller_state.get("last_review"))
        controller.last_outcome = deepcopy(controller_state.get("last_outcome"))
        controller.last_risk_latency = controller_state.get("last_risk_latency")
        controller.planner_episode = self._restore_planner_episode(
            controller_state.get("planner_episode")
        )

        self._restore_tracker_state(runtime.get("tracker"), controller.planner_episode)
        self._restore_agent_state(runtime.get("agent"))
        self._restore_planner_state(runtime.get("planner_adapter"))
        self.session = deepcopy(dict(payload.get("session") or {}))
        if self.session.get("schema_version") != ONLINE_SESSION_SCHEMA_VERSION:
            raise ValueError("online checkpoint session schema does not match")
        self.active_subtask_index = int(
            self.session.get("active_subtask_index") or self._active_subtask_index()
        )
        self.active_subtask_action_start = int(
            self.session.get("active_subtask_action_start") or 0
        )
        self.successful_action_count = len(self.session.get("completed_actions") or ())

    @staticmethod
    def _restore_planner_episode(payload: Any) -> PlannerEpisode:
        data = dict(payload or {})
        episode = PlannerEpisode(max_entries=int(data.get("max_entries") or 100))
        for raw_entry in data.get("entries") or ():
            entry_data = dict(raw_entry)
            episode.append(PlannerEpisodeEntry(**entry_data))
        return episode

    def _restore_tracker_state(self, payload: Any, episode: PlannerEpisode) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("online checkpoint has no tracker state")
        tracker = self.benchmark.tracker
        for name, value in payload.items():
            setattr(tracker, name, deepcopy(value))
        tracker.planner_episode = episode
        # Resume latency should not include wall-clock downtime between processes.
        tracker._latency_started_at = time.perf_counter()

    def _restore_agent_state(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("online checkpoint has no agent state")
        for name, value in payload.items():
            setattr(self.agent, name, deepcopy(value))

    def _restore_planner_state(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("online checkpoint has no planner adapter state")
        expected_type = str(payload.get("type") or "")
        if expected_type != type(self.planner_adapter).__name__:
            raise ValueError(
                "checkpoint planner adapter does not match the active adapter: "
                f"{expected_type!r} != {type(self.planner_adapter).__name__!r}"
            )
        for name, value in dict(payload.get("state") or {}).items():
            setattr(self.planner_adapter, name, deepcopy(value))
        base = getattr(self.planner_adapter, "base", None)
        if base is not None and hasattr(base, "_iterator"):
            base._iterator = None

    def _online_restore_diagnostics(self) -> Dict[str, Any]:
        controller = self.benchmark.runtime_controller
        snapshot = self.updater.get_snapshot().to_dict()
        global_snapshot = self.accumulator.snapshot().to_dict()
        return {
            "scene_graph_backend": self.updater.backend_name,
            "scene_graph_summary": snapshot.get("summary", {}),
            "global_scene_memory_summary": global_snapshot.get("summary", {}),
            "tracker_plan_count": len(self.benchmark.tracker.plans),
            "planner_episode_entries": len(controller.planner_episode),
            "scheduler_pending": controller.components.scheduler.to_dict().get("pending", []),
            "agent_current_step": int(self.agent.current_step),
        }

