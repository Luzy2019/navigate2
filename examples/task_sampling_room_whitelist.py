#!/usr/bin/env python3
"""Generate a BEHAVIOR task_custom_lists.json room/whitelist entry.

This mirrors the official task sampling workflow at a repo-local scale:

1. Read a task BDDL problem.
2. Map object synsets to dataset categories.
3. Choose whitelisted model IDs from local inventory, optionally preferring
   models already observed in cached scene templates.
4. Write the official task_custom_lists.json structure.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_SKIPPED_SYNSETS = {
    "agent.n.01",
    "floor.n.01",
    "water.n.06",
    "liquid_soap.n.01",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def load_task_config(root: Path, task: str) -> dict[str, Any]:
    task_path = root / "data" / "tasks" / f"{task}.json"
    if not task_path.exists():
        raise FileNotFoundError(f"Task config not found: {task_path}")
    return load_json(task_path)


def load_bddl_objects(root: Path, task: str, problem: int) -> dict[str, list[str]]:
    sys.path.insert(0, str(root / "bddl"))
    from bddl.parsing import parse_problem

    bddl_path = root / "data" / "bddl" / task / f"problem{problem}.bddl"
    if not bddl_path.exists():
        raise FileNotFoundError(f"BDDL problem not found: {bddl_path}")
    definition = bddl_path.read_text(encoding="utf-8")
    _, objects, _, _ = parse_problem(task, problem, "omnigibson", predefined_problem=definition)
    return objects


def load_category_mapping(root: Path) -> dict[str, str]:
    mapping_path = root / "bddl" / "bddl" / "generated_data" / "category_mapping.csv"
    mapping: dict[str, str] = {}
    with mapping_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            synset = (row.get("synset") or "").strip()
            category = (row.get("category") or "").strip()
            if synset and category:
                mapping.setdefault(synset, category)
    return mapping


def load_inventory_models(root: Path) -> dict[str, list[str]]:
    inventory_path = root / "bddl" / "bddl" / "generated_data" / "object_inventory.json"
    inventory = load_json(inventory_path)
    models: dict[str, set[str]] = defaultdict(set)
    for key in inventory.get("providers", {}):
        if "-" not in key:
            continue
        category, model = key.rsplit("-", 1)
        if category and model:
            models[category].add(model)
    return {category: sorted(ids) for category, ids in models.items()}


def load_scene_cached_models(root: Path, scene: str, max_templates: int) -> dict[str, list[str]]:
    if max_templates <= 0:
        return {}

    scene_dir = root / "data" / "scenes" / scene / "json"
    if not scene_dir.exists():
        return {}

    models: dict[str, set[str]] = defaultdict(set)
    for index, path in enumerate(sorted(scene_dir.glob("*_template.json"))):
        if index >= max_templates:
            break
        try:
            payload = load_json(path)
        except json.JSONDecodeError:
            continue
        for info in payload.get("objects_info", {}).values():
            args = info.get("args", {}) if isinstance(info, dict) else {}
            category = args.get("category")
            model = args.get("model")
            if category and model:
                models[category].add(model)
    return {category: sorted(ids) for category, ids in models.items()}


def normalize_model_spec(value: Any) -> dict[str, dict[str, None]]:
    """Normalize a spec whitelist entry to official category -> model -> null."""
    if value is None or value is False:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"Whitelist entry must be an object, null, or false, got {type(value).__name__}")

    normalized: dict[str, dict[str, None]] = {}
    for category, models in value.items():
        if models is None or models is False:
            normalized[category] = {}
        elif isinstance(models, dict):
            normalized[category] = {str(model): None for model in models}
        elif isinstance(models, list):
            normalized[category] = {str(model): None for model in models}
        elif isinstance(models, str):
            normalized[category] = {models: None}
        else:
            raise TypeError(f"Unsupported model list for category {category!r}: {type(models).__name__}")
    return normalized


def choose_models(
    category: str,
    inventory_models: dict[str, list[str]],
    scene_models: dict[str, list[str]],
    max_models: int,
) -> list[str]:
    chosen: list[str] = []
    for source in (scene_models.get(category, []), inventory_models.get(category, [])):
        for model in source:
            if model not in chosen:
                chosen.append(model)
            if len(chosen) >= max_models:
                return chosen
    return chosen


def path_from_macro(value: Any) -> Path | None:
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike)):
        return Path(value).expanduser()
    return None


def resolve_data_path(cli_data_path: Path | None) -> Path:
    if cli_data_path is not None:
        return cli_data_path.expanduser()

    for env_name in ("BEHAVIOR_TASK_INSTANCES_PATH", "CHALLENGE_TASK_INSTANCES_PATH"):
        env_value = os.environ.get(env_name)
        if env_value:
            path = Path(env_value).expanduser()
            return path.parent if path.name == "2026-challenge-task-instances" else path

    env_value = os.environ.get("OMNIGIBSON_DATA_PATH")
    if env_value:
        return Path(env_value).expanduser()

    try:
        from omnigibson.macros import gm
    except Exception as exc:
        raise RuntimeError(
            "Unable to import OmniGibson macros. Pass --data-path explicitly."
        ) from exc

    data_path = path_from_macro(getattr(gm, "DATA_PATH", None))
    if data_path is not None:
        return data_path

    dataset_path = path_from_macro(getattr(gm, "DATASET_PATH", None))
    if dataset_path is not None:
        # This OmniGibson version uses DATASET_PATH instead of DATA_PATH.
        # DATASET_PATH usually points at <data_root>/og_dataset.
        return dataset_path.parent if dataset_path.name == "og_dataset" else dataset_path

    raise RuntimeError(
        "Could not resolve OmniGibson data path. Pass --data-path, or set "
        "BEHAVIOR_TASK_INSTANCES_PATH / CHALLENGE_TASK_INSTANCES_PATH / OMNIGIBSON_DATA_PATH."
    )


def resolve_install_output(cli_data_path: Path | None) -> Path:
    return (
        resolve_data_path(cli_data_path)
        / "2026-challenge-task-instances"
        / "metadata"
        / "task_custom_lists.json"
    )


def build_task_entry(
    root: Path,
    task: str,
    scenes: list[str],
    room_types: list[str],
    problem: int,
    spec: dict[str, Any],
    max_models_per_category: int,
    max_scene_templates: int,
    use_default_skips: bool,
    extra_skips: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    objects = load_bddl_objects(root, task, problem)
    category_mapping = load_category_mapping(root)
    inventory_models = load_inventory_models(root)

    spec_whitelist = spec.get("whitelist", {})
    scene_specs = spec.get("scenes", {})
    skipped = set(extra_skips)
    if use_default_skips:
        skipped |= DEFAULT_SKIPPED_SYNSETS
    skipped |= set(spec.get("skip_synsets", []))

    entry: dict[str, Any] = {"room_types": room_types}
    report: dict[str, Any] = {
        "task": task,
        "scenes": scenes,
        "skipped_synsets": sorted(skipped),
        "missing_categories": [],
        "missing_models": [],
    }

    for scene in scenes:
        scene_spec = scene_specs.get(scene, {})
        merged_whitelist = dict(spec_whitelist)
        merged_whitelist.update(scene_spec.get("whitelist", {}))
        scene_models = load_scene_cached_models(root, scene, max_scene_templates)
        whitelist: dict[str, dict[str, dict[str, None]]] = {}

        for synset in sorted(objects):
            if synset in skipped:
                continue

            if synset in merged_whitelist:
                normalized = normalize_model_spec(merged_whitelist[synset])
                if normalized:
                    whitelist[synset] = normalized
                continue

            category = category_mapping.get(synset)
            if not category:
                report["missing_categories"].append({"scene": scene, "synset": synset})
                continue

            models = choose_models(category, inventory_models, scene_models, max_models_per_category)
            if not models:
                report["missing_models"].append({"scene": scene, "synset": synset, "category": category})
                whitelist[synset] = {category: {}}
                continue

            whitelist[synset] = {category: {model: None for model in models}}

        entry[scene] = {
            "whitelist": whitelist,
            "blacklist": scene_spec.get("blacklist", spec.get("blacklist", {})),
        }

    return entry, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an official-style task_custom_lists.json room/whitelist entry.",
    )
    parser.add_argument("--task", default="cook_tofu_and_vegetables__with_lighter")
    parser.add_argument("--problem", type=int, default=0)
    parser.add_argument("--scene", action="append", dest="scenes")
    parser.add_argument("--room-type", action="append", dest="room_types")
    parser.add_argument("--spec", type=Path, help="Optional JSON spec with room_types/scenes/whitelist overrides.")
    parser.add_argument("--output", type=Path, default=Path("examples/generated/task_custom_lists.json"))
    parser.add_argument("--install", action="store_true", help="Write into gm.DATA_PATH/2026-challenge-task-instances/metadata.")
    parser.add_argument(
        "--data-path",
        type=Path,
        help="OmniGibson data root. The challenge metadata lives under <data-path>/2026-challenge-task-instances/metadata.",
    )
    parser.add_argument("--merge-existing", action="store_true", help="Merge into an existing output JSON instead of replacing it.")
    parser.add_argument("--max-models-per-category", type=int, default=1)
    parser.add_argument("--max-scene-templates", type=int, default=24)
    parser.add_argument("--skip-synset", action="append", default=[])
    parser.add_argument("--no-default-skips", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    spec = load_json(args.spec) if args.spec else {}

    task_config = load_task_config(root, args.task)
    scene_info = task_config.get("scene_info", {})
    scenes = args.scenes or spec.get("scene_models") or spec.get("scenes_to_generate")
    if not scenes:
        default_scene = scene_info.get("default_scene_model")
        scenes = [default_scene] if default_scene else list(scene_info.get("scene_models", []))
    if not scenes:
        raise ValueError("No scene specified and task config has no scene models.")

    room_types = args.room_types or spec.get("room_types")
    if not room_types:
        room = scene_info.get("room")
        room_types = [room] if room else ["kitchen"]

    task_entry, report = build_task_entry(
        root=root,
        task=args.task,
        scenes=list(scenes),
        room_types=list(room_types),
        problem=args.problem,
        spec=spec,
        max_models_per_category=args.max_models_per_category,
        max_scene_templates=args.max_scene_templates,
        use_default_skips=not args.no_default_skips,
        extra_skips=set(args.skip_synset),
    )

    output = resolve_install_output(args.data_path) if args.install else (root / args.output)
    if args.merge_existing and output.exists():
        payload = load_json(output)
    else:
        payload = {}
    payload[args.task] = task_entry

    print(json.dumps(report, indent=2, sort_keys=True))
    if report["missing_categories"] or report["missing_models"]:
        print("Warning: generated whitelist has missing category/model entries.", file=sys.stderr)

    if args.dry_run:
        print(f"Dry run: would write {output}")
        return 0

    dump_json(output, payload)
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
