#!/usr/bin/env python3
"""Sample a BEHAVIOR task scene and save the template without executing a plan.

This is intentionally narrower than og_ego_prim.cli.online_benchmark_once:
it loads the scene + task, runs online object sampling, saves the sampled task
scene JSON, and exits. It does not initialize perception, run example_planning,
capture surrounding images, or execute robot primitives.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# OmniGibson reads this during import, so set it before the runtime wrapper
# re-execs and before importing omnigibson itself.
if "--headless" in sys.argv:
    os.environ.setdefault("OMNIGIBSON_HEADLESS", "1")

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()

from og_ego_prim.utils.monkey_patch import add_monkey_patch

add_monkey_patch()

import bddl
import omnigibson as og
from omnigibson.macros import gm
from omnigibson.tasks import BehaviorTask
from omnigibson.utils.bddl_utils import BEHAVIOR_ACTIVITIES

from og_ego_prim.benchmark.custom_behavior_task import CustomBehaviorTask  # noqa: F401
from og_ego_prim.benchmark.data_utils import CUSTOMIZED_BEHAVIOR_ACTIVITIES, get_customized_definition_filename
from og_ego_prim.utils.constants import SCENES, TASKS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sample and save a task scene without executing plans.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--scene", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--install", action="store_true", help="Copy output into data/scenes/<scene>/json.")
    parser.add_argument("--room-type", action="append", default=None, help="Limit base scene loading to room type(s).")
    parser.add_argument("--room-instance", action="append", default=None, help="Limit base scene loading to room instance(s).")
    parser.add_argument("--no-robot", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def load_task_config(task: str) -> dict[str, Any]:
    path = Path(TASKS) / f"{task}.json"
    if not path.exists():
        raise FileNotFoundError(f"Task config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def task_scene_filename(scene: str, task: str, definition_id: int, instance_id: int) -> str:
    return BehaviorTask.get_cached_activity_scene_filename(
        scene_model=scene,
        activity_name=task,
        activity_definition_id=definition_id,
        activity_instance_id=instance_id,
    )


def build_env_config(args: argparse.Namespace, task_config: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    env_config_path = Path(og.example_config_path) / task_config["_base_config"]
    env_config = yaml.load(env_config_path.read_text(encoding="utf-8"), Loader=yaml.FullLoader)

    task_info = task_config["task_info"]
    scene_info = task_config["scene_info"]
    task_name = task_info["task_name"]

    if task_name not in BEHAVIOR_ACTIVITIES:
        if task_name not in CUSTOMIZED_BEHAVIOR_ACTIVITIES:
            raise ValueError(f"Task {task_name!r} is neither official nor customized in data/bddl.")
        og.tasks.behavior_task.BEHAVIOR_ACTIVITIES.append(task_name)
        bddl.parsing.get_definition_filename = get_customized_definition_filename

    scene = args.scene or scene_info.get("default_scene_model") or random.choice(scene_info["scene_models"])
    if scene not in scene_info["scene_models"]:
        raise ValueError(f"Task {task_name!r} is not configured for scene {scene!r}.")

    env_config["task"] = {
        "type": "CustomBehaviorTask",
        "activity_name": task_name,
        "activity_definition_id": task_info["activity_definition_id"],
        "activity_instance_id": task_info["activity_instance_id"],
        "predefined_problem": None,
        "online_object_sampling": True,
    }

    room_types = args.room_type
    if room_types is None and scene_info.get("room"):
        room_types = [scene_info["room"]]

    env_config["scene"].update(
        {
            "scene_model": scene,
            "load_room_types": room_types,
            "load_room_instances": args.room_instance,
            "load_task_relevant_only": False,
            "not_load_object_categories": ["ceilings", "roof"],
            "include_robots": not args.no_robot,
        }
    )

    if args.no_robot:
        env_config["robots"] = []

    fname = task_scene_filename(
        scene=scene,
        task=task_name,
        definition_id=task_info["activity_definition_id"],
        instance_id=task_info["activity_instance_id"],
    )
    return env_config, scene, fname


def main() -> int:
    args = parse_args()
    random.seed(args.seed)

    gm.HEADLESS = args.headless
    gm.USE_GPU_DYNAMICS = True

    task_config = load_task_config(args.task)
    env_config, scene, fname = build_env_config(args, task_config)

    output = args.output or Path("outputs") / f"{fname}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Sampling task={args.task} scene={scene}")
    print(f"Room filter: types={env_config['scene'].get('load_room_types')} instances={env_config['scene'].get('load_room_instances')}")
    print(f"Output: {output}")
    sys.stdout.flush()

    env = og.Environment(configs=env_config)
    try:
        env.task.save_task(path=str(output))
        print(f"Saved sampled task scene: {output}")

        if args.install:
            install_path = Path(SCENES) / scene / "json" / f"{fname}.json"
            install_path.parent.mkdir(parents=True, exist_ok=True)
            install_path.write_text(output.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Installed sampled task scene: {install_path}")
    finally:
        og.clear()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
