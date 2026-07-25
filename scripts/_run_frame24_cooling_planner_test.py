"""Execute frame-24 cooling safety replanning with a restored timer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from og_ego_prim.cli.headless_manual_physical_session import PersistentPhysicalSession
from og_ego_prim.scheduler import ProcessStatus, ScheduledProcess


SESSION_DIR = Path("outputs/hwct10")
CHECKPOINT = Path("outputs/hwct3/frame_000024_cooling_backfill.pt")
ANNOTATION = Path("outputs/hwct3/frame_000024_no_change.json")


def main() -> None:
    args = argparse.Namespace(
        task="data/tasks/composite/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3.json",
        session_dir=str(SESSION_DIR),
        config="entrypoints/configs/eval_safe_memory_hot_water_cooling_timer_test.yaml",
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
    scheduler = session.benchmark.runtime_controller.components.scheduler
    process = ScheduledProcess(
        process_id="cooling:73dce88b6824d676",
        process_type="cooling",
        entity_ids=("water_bottle.n.01_1",),
        source_action_id="manual_backfill:toggle_off:frame_000010",
        start_step=2423,
        ready_step=9623,
        readiness_predicate="cooling_timer_elapsed",
        blocking_actions=(),
        completion_effects={},
        status=ProcessStatus.PENDING,
        extensions={
            "description": "hot water cooling timer",
            "gate_entity_ids": ["water_bottle.n.01_1"],
            "trigger_action": "TOGGLE_OFF",
            "backfilled_from": "frame_000010 toggle_off(microwave.n.02_1)",
        },
    )
    scheduler._pending[process.process_id] = process
    first = session.advance(str(ANNOTATION.resolve()))
    second = session.advance(str(ANNOTATION.resolve()))
    planner = session.benchmark.runtime_controller.components.planner
    result = {
        "first": first,
        "second": second,
        "safety_replan": {
            "raw_output": getattr(planner, "last_safety_plan_raw_output", None),
            "payload": getattr(planner, "last_safety_plan_payload", None),
        },
    }
    (SESSION_DIR / "frame_000024_execution_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    session.close()


if __name__ == "__main__":
    main()
