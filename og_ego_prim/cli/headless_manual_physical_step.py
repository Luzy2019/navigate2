"""Advance one physical-starter action from a human-confirmed RGB frame.

Human annotation replaces only SAMJAM / SAM2 / UniGoal recognition.  The
normal canonical scene-graph, state-diff, ObjectRegistry, Scheduler, GPT-4o
risk assessment, GPT-4o task planner, and physical-starter execution remain in
the runtime path.  Each invocation starts one headless simulator, replays the
committed action prefix, executes at most one new planner action, and exits.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
from typing import Any, Dict, Optional, Sequence

os.environ.setdefault("OMNIGIBSON_HEADLESS", "1")

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()

from og_ego_prim.utils.monkey_patch import add_monkey_patch

add_monkey_patch()

import numpy as np

from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict
from og_ego_prim.scene_graph.global_state import GlobalSceneGraphAccumulator
from og_ego_prim.scene_graph.manual_current_frame import (
    load_manual_current_frame_perception,
)
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater
from og_ego_prim.task_planner import AgentPlanner, create_planner_adapter


SESSION_SCHEMA_VERSION = "isbench.headless_manual_physical_session.v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--perception-json")
    parser.add_argument("--config")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--local-llm-serve", action="store_true")
    parser.add_argument("--local-serve-ip", default="")
    parser.add_argument("--local-serve-key", default="sk-123456")
    parser.add_argument("--planner-work-dir", default="results")
    parser.add_argument(
        "command",
        choices=("capture", "advance", "replay_capture", "status"),
        help=(
            "capture initializes frame 0; advance consumes one manual frame and "
            "executes one planner action; replay_capture rebuilds committed state "
            "and writes the required next native RGB frame."
        ),
    )
    return parser


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON in {path} must be an object")
    return value


def _write_rgb(path: Path, rgb: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray(rgb).save(path)


def _runtime(config_path: Optional[str]) -> RuntimeConfig:
    runtime = RuntimeConfig.from_mapping(load_runtime_config_dict(config_path))
    runtime.runtime.headless = True
    runtime.scene_graph.backend = "disabled"
    runtime.scene_graph.step_interval = 0
    runtime.artifacts.sensor_image_size = (512, 512)
    runtime.artifacts.save_video = False
    runtime.artifacts.save_step_images = False
    runtime.artifacts.save_surrounding_observations = False
    runtime.artifacts.save_topdown_scene = False
    runtime.starter_primitives.explicit_grasp_use_object_navigation = True
    runtime.starter_primitives.explicit_grasp_navigation_max_goal_radius = 1.2
    return runtime


def _new_session(task: str) -> Dict[str, Any]:
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "task": task,
        "completed_actions": [],
        "status": "new",
    }


def _load_or_create_session(session_path: Path, task: str) -> Dict[str, Any]:
    if not session_path.exists():
        return _new_session(task)
    session = _read_json(session_path)
    if session.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise ValueError(f"unsupported session schema in {session_path}")
    if str(session.get("task")) != task:
        raise ValueError("session task does not match --task")
    return session


def _build_benchmark(task: str, config_path: Optional[str]):
    from omnigibson.macros import gm
    from og_ego_prim.benchmark import build_benchmark

    gm.USE_GPU_DYNAMICS = True
    runtime = _runtime(config_path)
    benchmark = build_benchmark(
        task=task,
        scene=None,
        ego_view=True,
        draw_bbox_2d=False,
        primitive_type="starter",
        scene_graph_step_interval=0,
        scene_graph_backend="disabled",
        use_initial_setup=False,
        use_self_caption=False,
        online_object_sampling=False,
        debug=False,
        eval_process_safety=True,
        eval_termination_safety=True,
        eval_awareness=False,
        eval_execution=True,
        runtime_config=runtime,
    )
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    updater.set_task_entities(benchmark.scene_graph_updater.task_entity_ids)
    updater.set_task_categories(benchmark.scene_graph_updater.task_entity_ids)
    updater.set_task_instruction(benchmark.task_instruction)
    benchmark.scene_graph_updater = updater
    benchmark.runtime_controller.components.perception = updater
    benchmark.scene_graph_step_interval = 10**12
    return benchmark, updater, runtime


def _capture(benchmark, observer, frame_path: Path, frame_index: int) -> Dict[str, Any]:
    frame = observer.observe(benchmark.env)
    _write_rgb(frame_path, frame.rgb)
    return {
        "frame_index": frame_index,
        "sensor_name": frame.sensor_name,
        "rgb_shape": list(frame.rgb.shape),
        "robot_position": frame.robot_position,
        "image": str(frame_path),
    }


def _scene_graph_payload(snapshot, *, scope: str) -> Dict[str, Any]:
    payload = snapshot.to_dict()
    summary = dict(payload.get("summary") or {})
    summary["scope"] = scope
    payload["summary"] = summary
    return payload


def _write_scene_graph_artifacts(
    scene_graph_dir: Path,
    *,
    frame_index: int,
    current_frame_snapshot,
    global_snapshot,
) -> None:
    _write_json(
        scene_graph_dir / f"frame_{frame_index:06d}.json",
        _scene_graph_payload(current_frame_snapshot, scope="current_frame"),
    )
    _write_json(
        scene_graph_dir / "current_global_state.json",
        _scene_graph_payload(global_snapshot, scope="current_global_state"),
    )


def _observe_manual_frame(
    benchmark,
    updater,
    accumulator: GlobalSceneGraphAccumulator,
    annotation_path: Path,
    frame_index: int,
    scene_graph_dir: Path,
):
    result = load_manual_current_frame_perception(annotation_path, frame_index=frame_index)
    current_frame_snapshot = updater._snapshot_from_result(
        result, context=None, skipped=False, force=True
    )
    global_snapshot = accumulator.merge_current_frame(current_frame_snapshot)
    updater.snapshot = global_snapshot
    state_changes = benchmark.runtime_controller.observe(global_snapshot)
    _write_scene_graph_artifacts(
        scene_graph_dir,
        frame_index=frame_index,
        current_frame_snapshot=current_frame_snapshot,
        global_snapshot=global_snapshot,
    )
    return state_changes, current_frame_snapshot, global_snapshot


def _copy_annotation(source: Path, annotations_dir: Path, frame_index: int) -> Path:
    target = annotations_dir / f"frame_{frame_index:06d}.json"
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target


def _subtask_instruction(benchmark, subtask_index: int) -> str:
    subtask = benchmark.agent_task_view.subtask(subtask_index)
    if subtask is None:
        raise ValueError(f"task has no subtask {subtask_index}")
    return subtask.instruction


def _configure_agent(benchmark, args: argparse.Namespace, subtask_index: int):
    Path(args.planner_work_dir, "benchmark").mkdir(parents=True, exist_ok=True)
    agent = AgentPlanner(
        task_name=args.task,
        scene_name=benchmark.scene_name,
        model_name=args.model,
        work_dir=args.planner_work_dir,
        local_llm_serve=args.local_llm_serve,
        local_serve_ip=args.local_serve_ip,
        local_serve_key=args.local_serve_key,
        prompt_setting="v1",
        primitive_type="starter",
        use_initial_setup=False,
        use_self_caption=False,
        observation_dir=None,
    )
    agent.set_tracker(benchmark.tracker)
    agent.set_runtime_controller(benchmark.runtime_controller)
    from og_ego_prim.risk_predictor.utils import install_vlm_risk_provider

    install_vlm_risk_provider(benchmark, agent.client)
    agent.begin_lifelong_subtask(
        task_instruction=_subtask_instruction(benchmark, subtask_index),
        subtask_index=subtask_index,
    )
    adapter = create_planner_adapter(
        "vlm_closed_loop",
        agent,
        use_obs=False,
        max_step=None,
        held_object_getter=benchmark._current_grasped_object_id,
    )
    benchmark.bind_planner_adapter(adapter, source=type(agent).__name__, emit_proposals=True)
    return agent


def _risk_model_trace(benchmark) -> Dict[str, Optional[str]]:
    predictor = benchmark.runtime_controller.components.risk_predictor
    provider = getattr(predictor, "provider", None)
    for item in getattr(provider, "providers", (provider,)):
        assessor = getattr(item, "assessor", None)
        if assessor is not None:
            return {
                "prompt": getattr(assessor, "last_prompt", None),
                "raw_response": getattr(assessor, "last_raw_response", None),
            }
    return {"prompt": None, "raw_response": None}


def _safety_replan_trace(benchmark) -> Dict[str, Optional[Any]]:
    planner = benchmark.runtime_controller.components.planner
    return {
        "raw_output": getattr(planner, "last_safety_plan_raw_output", None),
        "payload": getattr(planner, "last_safety_plan_payload", None),
    }


def _write_llm_log(
    session_dir: Path,
    *,
    frame_index: int,
    planner_raw_output: Optional[str],
    review,
    outcome,
    global_snapshot,
    benchmark,
    output_path: Optional[Path] = None,
    overwrite: bool = True,
) -> None:
    path = output_path or session_dir / f"llm_{frame_index:06d}.txt"
    if not overwrite and path.exists():
        return
    scheduler = benchmark.runtime_controller.components.scheduler
    risk_trace = _risk_model_trace(benchmark)
    safety_replan_trace = _safety_replan_trace(benchmark)
    payload = {
        "frame_index": frame_index,
        "planner": {
            "raw_output": planner_raw_output,
            "grounded_action": review.action.to_legacy_plan(),
        },
        "scheduler": {
            "gate": (
                review.temporal_gate.to_dict()
                if getattr(review, "temporal_gate", None) is not None
                else None
            ),
            "pending_after_execution": [
                process.to_dict() for process in scheduler.pending_for()
            ],
        },
        "risk_predictor": {
            "raw_prompt": risk_trace["prompt"],
            "raw_response": risk_trace["raw_response"],
            "evaluation": (
                None if review.risk_evaluation is None else review.risk_evaluation.to_dict()
            ),
            "latency_seconds": benchmark.runtime_controller.last_risk_latency,
        },
        "safety_replan": safety_replan_trace,
        "execution": {
            "decision": review.decision.value,
            "reason": review.reason,
            "outcome": None if outcome is None else outcome.to_dict(),
            "diagnostics": benchmark.executor.last_execution_diagnostics,
        },
        "global_scene_summary": global_snapshot.to_dict().get("summary", {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _replay(
    benchmark,
    updater,
    accumulator: GlobalSceneGraphAccumulator,
    scene_graph_dir: Path,
    session_dir: Path,
    completed: Sequence[Dict[str, Any]],
    agent,
) -> None:
    for frame_index, record in enumerate(completed):
        annotation_path = Path(record["annotation"])
        if not annotation_path.is_file():
            raise FileNotFoundError(f"missing replay annotation: {annotation_path}")
        subtask_index = int(record["subtask_index"])
        benchmark.set_active_subtask(subtask_index)
        if subtask_index != int(agent.runtime_controller.active_subtask_id):
            agent.begin_lifelong_subtask(
                task_instruction=_subtask_instruction(benchmark, subtask_index),
                subtask_index=subtask_index,
            )
        _, current_frame_snapshot, global_snapshot = _observe_manual_frame(
            benchmark,
            updater,
            accumulator,
            annotation_path,
            frame_index,
            scene_graph_dir,
        )
        agent.record_plan(record["action"], raw_output=record.get("raw_output"))
        if not benchmark.execute_plan(record["action"]):
            raise RuntimeError(f"could not replay action {frame_index}: {record['action']}")
        review = benchmark.runtime_controller.last_review
        outcome = benchmark.runtime_controller.last_outcome
        if review is None or outcome is None or not outcome.succeeded:
            raise RuntimeError(f"replay action {frame_index} did not succeed")
        global_snapshot = accumulator.apply_successful_action(review.action)
        global_snapshot = accumulator.apply_state_changes(
            benchmark.runtime_controller.drain_scheduler_state_changes()
        )
        updater.snapshot = global_snapshot
        _write_scene_graph_artifacts(
            scene_graph_dir,
            frame_index=frame_index,
            current_frame_snapshot=current_frame_snapshot,
            global_snapshot=global_snapshot,
        )
        _write_llm_log(
            session_dir,
            frame_index=frame_index,
            planner_raw_output=record.get("raw_output"),
            review=review,
            outcome=outcome,
            global_snapshot=global_snapshot,
            benchmark=benchmark,
            overwrite=False,
        )


def _latest_raw_output(benchmark) -> Optional[str]:
    outputs = benchmark.tracker.raw_outputs
    return outputs[-1]["content"] if outputs else None


def run(args: argparse.Namespace) -> Dict[str, Any]:
    session_dir = Path(args.session_dir).resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    annotations_dir = session_dir / "annotations"
    annotations_dir.mkdir(exist_ok=True)
    scene_graph_dir = session_dir / "scene_graph"
    scene_graph_dir.mkdir(exist_ok=True)
    session_path = session_dir / "session.json"
    session = _load_or_create_session(session_path, args.task)
    if args.command == "status":
        return session

    benchmark = None
    try:
        benchmark, updater, runtime = _build_benchmark(args.task, args.config)
        completed = list(session["completed_actions"])
        if args.command == "capture":
            if completed:
                raise ValueError("capture is only supported before the first planned action")
            observer = ISBenchObservationAdapter(sensor_name=runtime.scene_graph.sensor_name)
            observer.reset()
            frame = _capture(benchmark, observer, session_dir / "frame_000000_current.png", 0)
            session.update(current_frame=frame, status="waiting_for_annotation")
            _write_json(session_path, session)
            return session

        active_subtask_index = int(session.get("active_subtask_index", 1))
        agent = _configure_agent(benchmark, args, active_subtask_index)
        accumulator = GlobalSceneGraphAccumulator()
        _replay(
            benchmark,
            updater,
            accumulator,
            scene_graph_dir,
            session_dir,
            completed,
            agent,
        )
        observer = ISBenchObservationAdapter(sensor_name=runtime.scene_graph.sensor_name)
        observer.reset()
        current_index = len(completed)

        if args.command == "replay_capture":
            frame = _capture(
                benchmark,
                observer,
                session_dir / f"frame_{current_index:06d}_current.png",
                current_index,
            )
            response = {
                "status": "waiting_for_annotation",
                "replayed_action_count": current_index,
                "current_frame": frame,
            }
            session.update(current_frame=frame, status=response["status"], last_result=response)
            _write_json(session_path, session)
            return response

        if args.perception_json is None:
            raise ValueError("advance requires --perception-json")
        if str(benchmark.runtime_controller.active_subtask_id) != str(
            active_subtask_index
        ):
            agent.begin_lifelong_subtask(
                task_instruction=_subtask_instruction(benchmark, active_subtask_index),
                subtask_index=active_subtask_index,
            )
        annotation = _copy_annotation(
            Path(args.perception_json).resolve(), annotations_dir, current_index
        )
        state_changes, current_frame_snapshot, global_snapshot = _observe_manual_frame(
            benchmark,
            updater,
            accumulator,
            annotation,
            current_index,
            scene_graph_dir,
        )
        proposed = benchmark.runtime_controller.propose()
        if proposed is None:
            raise RuntimeError("task planner returned no next action")
        executed = benchmark.execute_plan(proposed)
        review = benchmark.runtime_controller.last_review
        outcome = benchmark.runtime_controller.last_outcome
        if review is None or outcome is None:
            raise RuntimeError("benchmark execution did not produce review/outcome")

        if not executed or not outcome.succeeded:
            _write_llm_log(
                session_dir,
                frame_index=current_index,
                planner_raw_output=_latest_raw_output(benchmark),
                review=review,
                outcome=outcome,
                global_snapshot=global_snapshot,
                benchmark=benchmark,
            )
            response = {
                "status": "blocked_or_failed",
                "action": review.action.to_legacy_plan(),
                "decision": review.decision.value,
                "reason": review.reason,
                "risk": None if review.risk_evaluation is None else review.risk_evaluation.to_dict(),
                "outcome": outcome.reason,
                "execution_diagnostics": benchmark.executor.last_execution_diagnostics,
            }
            session.update(status=response["status"], last_result=response)
            _write_json(session_path, session)
            return response

        global_snapshot = accumulator.apply_successful_action(review.action)
        global_snapshot = accumulator.apply_state_changes(
            benchmark.runtime_controller.drain_scheduler_state_changes()
        )
        updater.snapshot = global_snapshot
        _write_scene_graph_artifacts(
            scene_graph_dir,
            frame_index=current_index,
            current_frame_snapshot=current_frame_snapshot,
            global_snapshot=global_snapshot,
        )
        raw_output = _latest_raw_output(benchmark)
        _write_llm_log(
            session_dir,
            frame_index=current_index,
            planner_raw_output=raw_output,
            review=review,
            outcome=outcome,
            global_snapshot=global_snapshot,
            benchmark=benchmark,
        )

        completed.append(
            {
                "subtask_index": active_subtask_index,
                "action": review.action.to_legacy_plan(),
                "annotation": str(annotation),
                "raw_output": raw_output,
            }
        )
        # Commit the successful physical effect before camera capture.  If Isaac
        # exits during capture/cleanup, replay_capture can recover the frame
        # without rerunning or losing this already-successful action.
        session.update(
            completed_actions=completed,
            status="capturing_next_frame",
            last_result={
                "status": "capturing_next_frame",
                "executed_action": review.action.to_legacy_plan(),
                "subtask_index": active_subtask_index,
            },
        )
        _write_json(session_path, session)

        next_index = len(completed)
        next_frame = _capture(
            benchmark,
            observer,
            session_dir / f"frame_{next_index:06d}_current.png",
            next_index,
        )
        response = {
            "status": "waiting_for_annotation",
            "planner_source": type(agent).__name__,
            "executed_action": review.action.to_legacy_plan(),
            "subtask_index": active_subtask_index,
            "decision": review.decision.value,
            "reason": review.reason,
            "state_change_count": len(state_changes),
            "scheduler_pending": [
                process.to_dict()
                for process in benchmark.runtime_controller.components.scheduler.pending_for()
            ],
            "risk": None if review.risk_evaluation is None else review.risk_evaluation.to_dict(),
            "outcome": outcome.reason,
            "current_frame": next_frame,
        }
        session.update(
            completed_actions=completed,
            current_frame=next_frame,
            status=response["status"],
            last_result=response,
        )
        _write_json(session_path, session)
        return response
    finally:
        if benchmark is not None:
            benchmark.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    result = run(build_parser().parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
