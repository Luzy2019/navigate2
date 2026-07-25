"""Run one independent T2 bottle-choice trial from the vase-on-table checkpoint."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from og_ego_prim.cli.headless_manual_physical_session import PersistentPhysicalSession


CHECKPOINT = Path(
    "outputs/headless_manual_hot_water_particle_frame16_coldrestore/"
    "checkpoint/frame_000021.pt"
)
ANNOTATION = Path(
    "outputs/headless_manual_hot_water_particle_frame16_coldrestore/"
    "annotations/frame_000021.json"
)
TASK = "data/tasks/composite/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3.json"
CONFIG = "entrypoints/configs/eval_safe_memory_hot_water_cooling_timer_test.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument(
        "--memory-mode",
        choices=("with-memory", "without-memory"),
        required=True,
    )
    return parser.parse_args()


def disable_scene_graph_memory(session: PersistentPhysicalSession) -> None:
    """Remove scene-graph memory only at the planner/risk prompt boundary."""
    controller = session.benchmark.runtime_controller
    original = controller.build_prompt_context

    def without_scene_graph_memory(**kwargs):
        context = original(**kwargs)
        return replace(context, current_scene=None, object_views=())

    controller.build_prompt_context = without_scene_graph_memory


def main() -> int:
    cli_args = parse_args()
    session_dir = Path(cli_args.session_dir)
    args = argparse.Namespace(
        task=TASK,
        session_dir=str(session_dir),
        config=CONFIG,
        model="gpt-4o",
        local_llm_serve=False,
        local_serve_ip="",
        local_serve_key="sk-123456",
        planner_work_dir="results",
        restore_frame=None,
        restore_checkpoint=str(CHECKPOINT),
        bootstrap_session_dir=None,
        video_capture_interval=10,
        video_fps=10.0,
        video_output_size="512x512",
        post_action_settle_steps=30,
    )
    session = PersistentPhysicalSession(args)
    try:
        if cli_args.memory_mode == "without-memory":
            disable_scene_graph_memory(session)
        result = session.advance(str(ANNOTATION.resolve()))
        payload = {
            "memory_mode": cli_args.memory_mode,
            "checkpoint": str(CHECKPOINT),
            "result": result,
        }
        (session_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
