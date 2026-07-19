#!/usr/bin/env python3
"""Merge task-specific objects into a complete OmniGibson scene cache."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-scene", type=Path, required=True)
    parser.add_argument("--full-scene", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def object_info(scene: dict[str, Any]) -> dict[str, Any]:
    return scene["objects_info"]["init_info"]


def task_object_names(scene: dict[str, Any]) -> set[str]:
    mapping = scene.get("metadata", {}).get("task", {}).get("inst_to_name", {})
    return {name for name in mapping.values() if name in object_info(scene)}


def remove_source_task_objects(scene: dict[str, Any]) -> list[str]:
    """Remove sampled objects from the donor task while retaining scene fixtures."""
    registry = scene["state"]["object_registry"]
    info = object_info(scene)
    removed = []
    for name in sorted(task_object_names(scene)):
        args = info[name].get("args", {})
        is_sampled_or_robot = args.get("uuid") is None
        if not is_sampled_or_robot:
            continue
        registry.pop(name, None)
        info.pop(name, None)
        removed.append(name)
    return removed


def used_uuids(scene: dict[str, Any]) -> set[int]:
    uuids = set()
    for entry in object_info(scene).values():
        value = entry.get("args", {}).get("uuid")
        if value is not None:
            uuids.add(int(value))
    return uuids


def next_uuid(used: set[int]) -> int:
    candidate = 99_000_000
    while candidate in used:
        candidate -= 1
    used.add(candidate)
    return candidate


def merge_scenes(
    task_scene: dict[str, Any],
    full_scene: dict[str, Any],
    *,
    task_scene_source: Path,
    full_scene_source: Path,
) -> dict[str, Any]:
    merged = copy.deepcopy(full_scene)
    removed = remove_source_task_objects(merged)

    # Keep the task's loader configuration, but allow every room in the scene cache.
    merged["init_info"] = copy.deepcopy(task_scene["init_info"])
    merged["init_info"]["args"]["load_room_types"] = None
    merged["init_info"]["args"]["load_room_instances"] = None
    merged["metadata"]["task"] = copy.deepcopy(task_scene["metadata"]["task"])
    merged["state"]["system_registry"] = copy.deepcopy(
        task_scene["state"]["system_registry"]
    )

    merged_registry = merged["state"]["object_registry"]
    merged_info = object_info(merged)
    task_registry = task_scene["state"]["object_registry"]
    task_info = object_info(task_scene)
    used = used_uuids(merged)
    added = []
    uuid_remaps = {}

    for name in sorted(task_object_names(task_scene)):
        if name in merged_registry and name in merged_info:
            continue
        state_entry = copy.deepcopy(task_registry[name])
        info_entry = copy.deepcopy(task_info[name])
        uuid = info_entry.get("args", {}).get("uuid")
        if uuid is not None and int(uuid) in used:
            replacement = next_uuid(used)
            info_entry["args"]["uuid"] = replacement
            uuid_remaps[name] = {"old": int(uuid), "new": replacement}
        elif uuid is not None:
            used.add(int(uuid))
        merged_registry[name] = state_entry
        merged_info[name] = info_entry
        added.append(name)

    merged.setdefault("metadata", {}).setdefault("isbench_scene_merge", {}).update(
        {
            "full_scene_source": str(full_scene_source.resolve()),
            "task_scene_source": str(task_scene_source.resolve()),
            "removed_source_task_objects": removed,
            "added_task_objects": added,
            "uuid_remaps": uuid_remaps,
        }
    )
    return merged


def main() -> None:
    args = parse_args()
    task_scene = json.loads(args.task_scene.read_text(encoding="utf-8"))
    full_scene = json.loads(args.full_scene.read_text(encoding="utf-8"))
    merged = merge_scenes(
        task_scene,
        full_scene,
        task_scene_source=args.task_scene,
        full_scene_source=args.full_scene,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(merged, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"merged scene saved: {args.output} "
        f"objects={len(merged['state']['object_registry'])}"
    )


if __name__ == "__main__":
    main()
