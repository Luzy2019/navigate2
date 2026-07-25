"""Pause a live ego-primitive task after every human-confirmed frame.

The process owns one OmniGibson environment.  Each ``advance`` command converts
the latest human-confirmed current-frame perception into the ordinary v2 scene
graph, runs scheduler and risk review for one planned action, executes that
action, saves the next native RGB image, then waits for the next annotation.
"""

from __future__ import annotations

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()

from og_ego_prim.utils.monkey_patch import add_monkey_patch

add_monkey_patch()

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import socket
import time
import traceback
from typing import Any, Dict, Optional, Sequence

import numpy as np

from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict
from og_ego_prim.scene_graph.manual_current_frame import (
    load_manual_current_frame_perception,
)
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater
from og_ego_prim.task_planner.adapters import IteratorPlannerAdapter
from og_ego_prim.utils.planning import normalize_planner_action


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--config")
    parser.add_argument("--perception-json", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def _ego_task_plan() -> list[dict[str, str]]:
    """The task JSON's starter plan projected to navigation-free Ego actions."""

    actions = (
        "OPEN(microwave.n.02_1)",
        "GRASP(water_bottle.n.01_1)",
        "PLACE_INSIDE(water_bottle.n.01_1, microwave.n.02_1)",
        "CLOSE(microwave.n.02_1)",
        "TOGGLE_ON(microwave.n.02_1)",
        "WAIT_FOR_COOKED(water_bottle.n.01_1)",
        "TOGGLE_OFF(microwave.n.02_1)",
        "OPEN(microwave.n.02_1)",
        "GRASP(water_bottle.n.01_1)",
        "PLACE_ON_TOP(water_bottle.n.01_1, table.n.02_1)",
        "GRASP(vase.n.01_1)",
        "PLACE_ON_TOP(vase.n.01_1, table.n.02_1)",
        "GRASP(water_bottle.n.01_2)",
        "POUR_INTO(water_bottle.n.01_2, vase.n.01_1)",
        "PLACE_ON_TOP(water_bottle.n.01_2, table.n.02_1)",
        "GRASP(water_bottle.n.01_1)",
        "PLACE_ON_TOP(water_bottle.n.01_1, floor.n.01_3)",
        "DONE()",
    )
    return [{"action": action, "caution": None} for action in actions]


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_rgb(path: Path, rgb: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray(rgb).save(path)


class InteractiveManualLoop:
    def __init__(
        self,
        *,
        task: str,
        config: Optional[str],
        perception_path: Path,
        output_dir: Path,
    ) -> None:
        self.task = task
        self.perception_path = perception_path.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        runtime = RuntimeConfig.from_mapping(load_runtime_config_dict(config))
        runtime.runtime.headless = True
        runtime.scene_graph.backend = "disabled"
        runtime.scene_graph.step_interval = 0
        runtime.artifacts.save_video = False
        runtime.artifacts.save_step_images = False
        runtime.artifacts.save_surrounding_observations = False

        from omnigibson.macros import gm
        from og_ego_prim.benchmark import build_benchmark

        gm.USE_GPU_DYNAMICS = True
        self.benchmark = build_benchmark(
            task=task,
            scene=None,
            ego_view=True,
            draw_bbox_2d=False,
            primitive_type="ego",
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
        self.observer = ISBenchObservationAdapter()
        self.observer.reset()
        self.current_frame = self._capture("frame_000000_initial")
        self.updater = PerceptionSceneGraphUpdater(backend_name="disabled")
        self.updater.set_task_entities(self.benchmark.scene_graph_updater.task_entity_ids)
        self.updater.set_task_categories(
            self.benchmark.scene_graph_updater.task_entity_ids
        )
        self.updater.set_task_instruction(self.benchmark.task_instruction)
        self.benchmark.scene_graph_updater = self.updater
        self.benchmark.runtime_controller.components.perception = self.updater
        self.benchmark.scene_graph_step_interval = 10**12
        self.benchmark.bind_planner_adapter(
            IteratorPlannerAdapter(_ego_task_plan()),
            source="InteractiveManualTaskPlanner",
            emit_proposals=True,
        )
        self.action_index = 0
        self.finished = False
        self.last_result: Dict[str, Any] = {}
        self._write_status("waiting_for_initial_annotation")

    def _capture(self, tag: str):
        frame = self.observer.observe(self.benchmark.env)
        image_path = self.output_dir / f"{tag}.png"
        _write_rgb(image_path, frame.rgb)
        _write_json(
            self.output_dir / f"{tag}.json",
            {
                "frame_index": frame.frame_index,
                "sensor_name": frame.sensor_name,
                "rgb_shape": list(frame.rgb.shape),
                "robot_position": frame.robot_position,
                "image": str(image_path),
            },
        )
        return frame

    def _write_status(self, status: str) -> None:
        _write_json(
            self.output_dir / "status.json",
            {
                "status": status,
                "action_index": self.action_index,
                "finished": self.finished,
                "current_frame_index": self.current_frame.frame_index,
                "current_frame_image": str(
                    self.output_dir / f"frame_{self.current_frame.frame_index:06d}_after_action.png"
                ) if self.current_frame.frame_index else str(self.output_dir / "frame_000000_initial.png"),
                "last_result": self.last_result,
            },
        )

    def advance(self) -> Dict[str, Any]:
        if self.finished:
            return {"status": "finished", "detail": self.last_result}
        subtask_index = 1 if self.action_index < 10 else 2 if self.action_index < 15 else 3
        self.benchmark.set_active_subtask(subtask_index)
        result = load_manual_current_frame_perception(
            self.perception_path,
            frame_index=self.current_frame.frame_index,
        )
        snapshot = self.updater._snapshot_from_result(
            result,
            context=None,
            skipped=False,
            force=True,
        )
        state_changes = self.benchmark.runtime_controller.observe(snapshot)
        action = self.benchmark.runtime_controller.propose()
        if action is None:
            self.finished = True
            self.last_result = {"reason": "planner_exhausted"}
            self._write_status("finished")
            return {"status": "finished", **self.last_result}
        if action.name == "DONE":
            review = self.benchmark.runtime_controller.review_action(action)
            response: Dict[str, Any] = {
                "status": "reviewed",
                "action": action.to_legacy_plan(),
                "decision": review.decision.value,
                "reason": review.reason,
                "state_change_count": len(state_changes),
                "scheduler_pending": [
                    process.to_dict()
                    for process in self.benchmark.runtime_controller.components.scheduler.pending_for()
                ],
                "risk": None if review.risk_evaluation is None else review.risk_evaluation.to_dict(),
            }
            if not review.allowed:
                outcome = self.benchmark.runtime_controller.record_blocked(review)
                response["status"] = "blocked"
                response["outcome"] = outcome.reason
                self.last_result = response
                self._write_status("blocked")
                return response
            self.finished = True
            self.benchmark.termination_evaluation()
            response["status"] = "finished"
            response["termination"] = self.benchmark.tracker.termination
            self.last_result = response
            self._write_status("finished")
            return response

        execution_succeeded = self.benchmark.execute_plan(action)
        review = self.benchmark.runtime_controller.last_review
        outcome = self.benchmark.runtime_controller.last_outcome
        if review is None or outcome is None:
            raise RuntimeError("benchmark execution completed without a runtime review/outcome")
        response: Dict[str, Any] = {
            "status": "executed" if execution_succeeded else "blocked_or_failed",
            "action": review.action.to_legacy_plan(),
            "decision": review.decision.value,
            "reason": review.reason,
            "state_change_count": len(state_changes),
            "scheduler_pending": [
                process.to_dict()
                for process in self.benchmark.runtime_controller.components.scheduler.pending_for()
            ],
            "risk": None if review.risk_evaluation is None else review.risk_evaluation.to_dict(),
            "executed": outcome.executed,
            "execution_succeeded": outcome.succeeded,
            "outcome": outcome.reason,
        }
        self.action_index += 1
        self.current_frame = self._capture(
            f"frame_{self.current_frame.frame_index + 1:06d}_after_action"
        )
        response["next_frame"] = {
            "frame_index": self.current_frame.frame_index,
            "image": str(
                self.output_dir
                / f"frame_{self.current_frame.frame_index:06d}_after_action.png"
            ),
        }
        self.last_result = response
        self._write_status("waiting_for_next_annotation")
        return response

    def close(self) -> Dict[str, Any]:
        report = self.benchmark.close()
        self.finished = True
        self.last_result = {"close": report}
        self._write_status("closed")
        return report


def serve(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    socket_path = output_dir / "control.sock"
    if socket_path.exists():
        socket_path.unlink()
    loop = InteractiveManualLoop(
        task=args.task,
        config=args.config,
        perception_path=Path(args.perception_json),
        output_dir=output_dir,
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        server.listen(1)
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    request = json.loads(connection.recv(4096).decode("utf-8"))
                    command = request.get("command")
                    if command == "advance":
                        response = loop.advance()
                    elif command == "status":
                        response = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
                    elif command == "close":
                        response = loop.close()
                        response["status"] = "closed"
                        connection.sendall(json.dumps(response).encode("utf-8"))
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
                    loop.last_result = response
                    loop._write_status("error")
                connection.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    return serve(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
