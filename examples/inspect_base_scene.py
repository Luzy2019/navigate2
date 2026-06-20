#!/usr/bin/env python3
"""Inspect an OmniGibson base scene without loading any BEHAVIOR task.

This intentionally uses DummyTask and does not touch BDDL sampling. It is useful
for checking what rooms and original scene objects exist before creating a task
template.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import torch as th


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load and inspect an OmniGibson base scene.")
    parser.add_argument("--scene", default="Wainscott_0_int", help="Base scene model, e.g. Wainscott_0_int.")
    parser.add_argument("--room-type", action="append", help="Only load objects in this room type, e.g. kitchen.")
    parser.add_argument("--room-instance", action="append", help="Only load objects in this room instance, e.g. kitchen_0.")
    parser.add_argument("--category", action="append", help="Only load these object categories.")
    parser.add_argument("--exclude-category", action="append", default=["ceilings", "roof"])
    parser.add_argument("--no-robot", action="store_true", help="Load the scene without a Fetch robot.")
    parser.add_argument("--headless", action="store_true", help="Run without the viewer UI.")
    parser.add_argument("--keep-open", action="store_true", help="Keep the viewer open after printing the report.")
    parser.add_argument("--steps", type=int, default=0, help="Number of idle simulation steps to run.")
    parser.add_argument("--list-objects", action="store_true", help="Print object names, categories, models, and rooms.")
    parser.add_argument("--output", type=Path, help="Optional path to write a JSON inspection report.")
    return parser.parse_args()


def to_builtin(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_builtin(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_builtin(v) for v in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def object_rooms(obj: Any) -> list[str]:
    rooms = getattr(obj, "in_rooms", None)
    if rooms is None:
        return []
    if isinstance(rooms, str):
        return [rooms]
    return list(rooms)


def object_model(obj: Any) -> str | None:
    model = getattr(obj, "model", None)
    if model is not None:
        return model
    # DatasetObject stores model in some versions under model_name.
    return getattr(obj, "model_name", None)


def build_report(env: Any) -> dict[str, Any]:
    scene = env.scene
    objects = list(scene.objects)

    by_category = Counter(getattr(obj, "category", "unknown") for obj in objects)
    by_room: dict[str, int] = defaultdict(int)
    for obj in objects:
        rooms = object_rooms(obj)
        if not rooms:
            by_room["<none>"] += 1
        for room in rooms:
            by_room[room] += 1

    seg_map = getattr(scene, "seg_map", None)
    room_types: dict[str, list[str]] = {}
    if seg_map is not None:
        room_types = {
            room_type: sorted(instances)
            for room_type, instances in getattr(seg_map, "room_sem_name_to_ins_name", {}).items()
        }

    object_rows = []
    for obj in sorted(objects, key=lambda item: item.name):
        object_rows.append(
            {
                "name": obj.name,
                "category": getattr(obj, "category", None),
                "model": object_model(obj),
                "rooms": object_rooms(obj),
                "fixed_base": getattr(obj, "fixed_base", None),
            }
        )

    return {
        "scene_model": getattr(scene, "scene_model", None),
        "object_count": len(objects),
        "room_types": room_types,
        "category_counts": dict(sorted(by_category.items())),
        "room_counts": dict(sorted(by_room.items())),
        "objects": object_rows,
    }


def print_report(report: dict[str, Any], list_objects: bool) -> None:
    print(f"Scene: {report['scene_model']}")
    print(f"Objects loaded: {report['object_count']}")

    print("\nRoom types:")
    for room_type, instances in report["room_types"].items():
        print(f"  {room_type}: {', '.join(instances)}")

    print("\nTop categories:")
    for category, count in Counter(report["category_counts"]).most_common(40):
        print(f"  {category}: {count}")

    print("\nLoaded room instances:")
    for room, count in report["room_counts"].items():
        print(f"  {room}: {count}")

    if list_objects:
        print("\nObjects:")
        for obj in report["objects"]:
            rooms = ",".join(obj["rooms"]) if obj["rooms"] else "-"
            model = obj["model"] or "-"
            category = obj["category"] or "-"
            print(f"  {obj['name']}\t{category}\t{model}\t{rooms}")


def main() -> int:
    args = parse_args()

    import omnigibson as og
    from omnigibson.macros import gm

    gm.HEADLESS = args.headless
    gm.USE_GPU_DYNAMICS = True

    cfg: dict[str, Any] = {
        "scene": {
            "type": "InteractiveTraversableScene",
            "scene_model": args.scene,
            "load_object_categories": args.category,
            "not_load_object_categories": args.exclude_category,
            "load_room_types": args.room_type,
            "load_room_instances": args.room_instance,
            "load_task_relevant_only": False,
            "include_robots": not args.no_robot,
        },
        "robots": []
        if args.no_robot
        else [
            {
                "type": "Fetch",
                "obs_modalities": ["rgb"],
                "action_type": "continuous",
                "action_normalize": False,
            }
        ],
        "task": {"type": "DummyTask"},
    }

    env = og.Environment(configs=cfg)
    should_clear = True
    try:
        for _ in range(args.steps):
            action = th.zeros(env.robots[0].action_dim) if env.robots else th.empty(0)
            env.step(action)

        report = build_report(env)
        print_report(report, args.list_objects)

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(to_builtin(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"\nWrote {args.output}")

        if args.keep_open:
            should_clear = False
            print("\nViewer is open. Press Ctrl+C in this terminal to exit.")
            if not args.headless:
                og.sim.enable_viewer_camera_teleoperation()
            while True:
                action = th.zeros(env.robots[0].action_dim) if env.robots else th.empty(0)
                env.step(action)
    except KeyboardInterrupt:
        print("\nExiting scene inspector.")
    finally:
        if should_clear:
            og.clear()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
