"""Capture and check a whole-scene initialization gate for an IS-Bench task.

The audit deliberately performs no task action. It records the sampled scene,
holds the robot through a declared idle window, and captures global, per-room,
and oblique views before any navigation or manipulation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _parse_bounds(values: Optional[Sequence[float]]) -> Optional[list[float]]:
    if values is None:
        return None
    if len(values) != 4:
        raise ValueError("world bounds must contain four values")
    min_x, min_y, max_x, max_y = (float(value) for value in values)
    if not (min_x < max_x and min_y < max_y):
        raise ValueError("world bounds must satisfy min < max")
    return [min_x, min_y, max_x, max_y]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    if isinstance(value, np.ndarray):
        return value.astype(float).tolist()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _finite_numbers(value: Any) -> bool:
    try:
        return bool(np.isfinite(np.asarray(value, dtype=float)).all())
    except (TypeError, ValueError):
        return False


def _norm(value: Any) -> Optional[float]:
    try:
        return float(np.linalg.norm(np.asarray(value, dtype=float)))
    except (TypeError, ValueError):
        return None


def _distance(first: Any, second: Any) -> Optional[float]:
    try:
        return float(np.linalg.norm(np.asarray(first, dtype=float) - np.asarray(second, dtype=float)))
    except (TypeError, ValueError):
        return None


def _safe_call(obj: Any, method: str, default: Any = None) -> Any:
    try:
        return getattr(obj, method)()
    except Exception:
        return default


def _bbox(obj: Any) -> Optional[dict[str, Any]]:
    try:
        center, orientation, extent, _ = obj.get_base_aligned_bbox(visual=False)
    except Exception:
        try:
            center, orientation, extent, _ = obj.get_base_aligned_bbox()
        except Exception:
            return None
    center = np.asarray(_json_value(center), dtype=float)
    extent = np.asarray(_json_value(extent), dtype=float)
    if center.shape != (3,) or extent.shape != (3,) or not _finite_numbers(center) or not _finite_numbers(extent):
        return None
    half = np.abs(extent) * 0.5
    return {
        "center": center.tolist(),
        "orientation_xyzw": _json_value(orientation),
        "extent": extent.tolist(),
        "min": (center - half).tolist(),
        "max": (center + half).tolist(),
    }


def _room_bounds(records: Iterable[Mapping[str, Any]], room: str, margin: float) -> Optional[list[float]]:
    points = []
    for record in records:
        if room not in (record.get("in_rooms") or []):
            continue
        bbox = record.get("aabb") or {}
        if "min" in bbox and "max" in bbox:
            points.extend([bbox["min"], bbox["max"]])
    if not points:
        return None
    arr = np.asarray(points, dtype=float)
    lower = arr[:, :2].min(axis=0) - margin
    upper = arr[:, :2].max(axis=0) + margin
    for axis in range(2):
        if upper[axis] - lower[axis] < 2.0:
            center = (upper[axis] + lower[axis]) * 0.5
            lower[axis], upper[axis] = center - 1.0, center + 1.0
    return [float(lower[0]), float(lower[1]), float(upper[0]), float(upper[1])]


def _all_bounds(records: Iterable[Mapping[str, Any]], margin: float) -> Optional[list[float]]:
    points = []
    for record in records:
        bbox = record.get("aabb") or {}
        if "min" in bbox and "max" in bbox:
            points.extend([bbox["min"], bbox["max"]])
    if not points:
        return None
    arr = np.asarray(points, dtype=float)
    lower = arr[:, :2].min(axis=0) - margin
    upper = arr[:, :2].max(axis=0) + margin
    return [float(lower[0]), float(lower[1]), float(upper[0]), float(upper[1])]


def _task_mapping(benchmark: Any) -> dict[str, Any]:
    mapping = {}
    for bddl_name, ref in (getattr(benchmark.env.task, "object_scope", {}) or {}).items():
        obj = getattr(ref, "wrapped_obj", None)
        if obj is not None:
            mapping[bddl_name] = obj
    return mapping


def _state_summary(obj: Any) -> dict[str, Any]:
    result = {}
    for state_name, state in (getattr(obj, "states", {}) or {}).items():
        label = getattr(state_name, "__name__", str(state_name))
        if label in {"OnTop", "Inside", "Under", "Contains", "Touching", "Covered", "NextTo"}:
            continue
        try:
            value = state.get_value()
        except Exception:
            continue
        if isinstance(value, (bool, int, float, str)):
            result[label] = value
    return result


def _object_record(obj: Any, *, task_names: set[str]) -> dict[str, Any]:
    try:
        position, orientation = obj.get_position_orientation()
    except Exception:
        position, orientation = None, None
    position = _json_value(position)
    orientation = _json_value(orientation)
    linear_velocity = _json_value(_safe_call(obj, "get_linear_velocity"))
    angular_velocity = _json_value(_safe_call(obj, "get_angular_velocity"))
    bbox = _bbox(obj)
    scale = _json_value(getattr(obj, "scale", None))
    if scale is None:
        scale = _json_value(getattr(obj, "_scale", None))
    quaternion_norm = _norm(orientation)
    return {
        "name": str(getattr(obj, "name", "")),
        "category": str(getattr(obj, "category", "")),
        "model": str(getattr(obj, "model", "")),
        "scale": scale,
        "in_rooms": list(getattr(obj, "in_rooms", []) or []),
        "position": position,
        "orientation_xyzw": orientation,
        "quaternion_norm": quaternion_norm,
        "linear_velocity": linear_velocity,
        "angular_velocity": angular_velocity,
        "aabb": bbox,
        "states": _state_summary(obj),
        "task_object": str(getattr(obj, "name", "")) in task_names,
        "finite_pose": _finite_numbers(position) and _finite_numbers(orientation),
        "finite_motion": _finite_numbers(linear_velocity) and _finite_numbers(angular_velocity),
        "normalized_quaternion": quaternion_norm is not None and abs(quaternion_norm - 1.0) <= 1e-2,
    }


def _parse_expected_relations(bddl_path: Path) -> dict[str, Any]:
    from bddl.parsing import scan_tokens

    expected: dict[str, Any] = {"ontop": [], "inside": [], "inroom": [], "states": []}
    problem = scan_tokens(string=bddl_path.read_text(encoding="utf-8"))
    init = next(
        (
            group
            for group in problem[1:]
            if isinstance(group, list) and group and str(group[0]).lower() == ":init"
        ),
        None,
    )
    if init is None:
        raise ValueError(f"BDDL problem has no :init section: {bddl_path}")

    for raw_atom in init[1:]:
        if not isinstance(raw_atom, list) or not raw_atom:
            continue
        atom = raw_atom
        value = True
        if str(atom[0]).lower() == "not":
            if len(atom) != 2 or not isinstance(atom[1], list) or not atom[1]:
                continue
            atom = atom[1]
            value = False
        predicate = str(atom[0]).lower()
        arguments = [str(item) for item in atom[1:]]
        if value and predicate in {"ontop", "inside", "inroom"} and len(arguments) == 2:
            expected[predicate].append(arguments)
        elif len(arguments) == 1:
            expected["states"].append(
                {"predicate": predicate, "object": arguments[0], "value": value}
            )
    return expected


def _look_at_quaternion(position: Sequence[float], target: Sequence[float]):
    import torch
    from omnigibson.utils import transform_utils as T

    pos = torch.tensor(position, dtype=torch.float32)
    target_tensor = torch.tensor(target, dtype=torch.float32)
    forward = target_tensor - pos
    forward = forward / torch.linalg.norm(forward).clamp_min(1e-6)
    up = torch.tensor([0.0, 0.0, 1.0], dtype=torch.float32)
    if abs(float(torch.dot(forward, up))) > 0.95:
        up = torch.tensor([0.0, 1.0, 0.0], dtype=torch.float32)
    right = torch.linalg.cross(forward, up)
    right = right / torch.linalg.norm(right).clamp_min(1e-6)
    camera_up = torch.linalg.cross(right, forward)
    camera_up = camera_up / torch.linalg.norm(camera_up).clamp_min(1e-6)
    rotation = torch.stack((right, camera_up, -forward), dim=1)
    return T.mat2quat(rotation)


def _capture_oblique(benchmark: Any, path: Path, position: Sequence[float], target: Sequence[float]) -> dict[str, Any]:
    import omnigibson as og
    import torch
    from PIL import Image

    for robot in getattr(benchmark.env, "robots", []) or []:
        if hasattr(robot, "visible"):
            robot.visible = False
    og.sim.viewer_width = 960
    og.sim.viewer_height = 540
    camera = og.sim.viewer_camera
    quaternion = _look_at_quaternion(position, target)
    camera.set_position_orientation(
        position=torch.tensor(position, dtype=torch.float32),
        orientation=quaternion,
    )
    for _ in range(5):
        og.sim.step()
    obs, _ = camera.get_obs()
    rgb = obs.get("rgb")
    if hasattr(rgb, "detach"):
        rgb = rgb.detach().cpu().numpy()
    rgb = np.asarray(rgb)
    if rgb.dtype != np.uint8:
        if rgb.max() <= 1.0:
            rgb = rgb * 255.0
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    rgb = rgb[..., :3]
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(path)
    return {
        "image": str(path),
        "camera_position": [float(value) for value in position],
        "target": [float(value) for value in target],
        "camera_orientation_xyzw": _json_value(quaternion),
        "output_size": [960, 540],
    }


def _write_overlay(image_path: Path, output_path: Path, records: list[dict[str, Any]], bounds: Sequence[float]) -> None:
    from PIL import Image, ImageDraw

    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    min_x, min_y, max_x, max_y = (float(value) for value in bounds)
    width, height = image.size
    task_names = {record["name"] for record in records if record.get("task_object")}
    for record in records:
        position = record.get("position")
        if not position or not _finite_numbers(position):
            continue
        x = int((float(position[0]) - min_x) / max(max_x - min_x, 1e-6) * width)
        y = int((1.0 - (float(position[1]) - min_y) / max(max_y - min_y, 1e-6)) * height)
        if not (0 <= x < width and 0 <= y < height):
            continue
        is_task = record["name"] in task_names
        color = (220, 30, 30) if is_task else (40, 100, 220)
        radius = 5 if is_task else 3
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        label = record["name"] if is_task else record["category"]
        draw.text((x + radius + 2, y - radius), label[:28], fill=color)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="entrypoints/configs/eval_safe_memory_knife_hidden_hamper.yaml")
    parser.add_argument("--task", default="lifelong_crossroom__beechwood__knife_hidden_in_hamper_v1")
    parser.add_argument("--scene", default="Beechwood_0_int")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--idle-steps", type=int, default=120)
    parser.add_argument("--margin", type=float, default=1.0)
    parser.add_argument("--world-bounds", nargs=4, type=float, default=None)
    parser.add_argument("--bddl", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.idle_steps < 120:
        raise SystemExit("--idle-steps must be at least 120 for cloth/particle scenes")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMNIGIBSON_HEADLESS", "1")
    from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

    maybe_reexec_with_omnigibson_python()

    from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict
    from og_ego_prim.benchmark import build_benchmark
    from og_ego_prim.utils.monkey_patch import add_monkey_patch
    from og_ego_prim.utils.task_registry import get_task_config_path

    add_monkey_patch()
    import omnigibson as og
    from omnigibson.macros import gm
    gm.USE_GPU_DYNAMICS = True

    config = load_runtime_config_dict(args.config)
    config.setdefault("task", {})["name"] = args.task
    config.setdefault("task", {})["scene"] = args.scene
    config.setdefault("scene_graph", {})["backend"] = "disabled"
    config.setdefault("scene_graph", {})["step_interval"] = 0
    runtime_config = RuntimeConfig.from_mapping(config)
    task_json_path = get_task_config_path(args.task)
    task_json = json.loads(task_json_path.read_text(encoding="utf-8"))
    canonical_task_name = str(task_json["task_info"]["task_name"])

    benchmark = None
    try:
        benchmark = build_benchmark(
            task=args.task,
            scene=args.scene,
            # Use the normal ego-view initialization path so OmniGibson warms
            # the camera/render pipeline before the first viewer capture.
            ego_view=True,
            draw_bbox_2d=False,
            primitive_type="starter",
            scene_graph_step_interval=0,
            scene_graph_backend="disabled",
            use_initial_setup=False,
            use_self_caption=False,
            online_object_sampling=False,
            debug=False,
            eval_process_safety=False,
            eval_termination_safety=False,
            eval_awareness=False,
            eval_execution=False,
            runtime_config=runtime_config,
        )

        task_objects = _task_mapping(benchmark)
        all_objects = list(getattr(benchmark.env.scene, "objects", []) or [])
        records_before = [_object_record(obj, task_names={obj.name for obj in task_objects.values()}) for obj in all_objects]
        records_before.sort(key=lambda record: record["name"])

        # The only simulation after sampling is a hold/no-op settle window.
        benchmark.executor._simulator_loop(args.idle_steps)
        records_after = [_object_record(obj, task_names={obj.name for obj in task_objects.values()}) for obj in all_objects]
        records_after.sort(key=lambda record: record["name"])
        after_by_name = {record["name"]: record for record in records_after}
        for before in records_before:
            after = after_by_name.get(before["name"])
            if after is None:
                continue
            before["idle_displacement_m"] = _distance(before.get("position"), after.get("position"))
            before["idle_linear_speed_mps"] = _norm(after.get("linear_velocity"))
            before["idle_angular_speed_rps"] = _norm(after.get("angular_velocity"))

        all_bounds = _parse_bounds(args.world_bounds) or _all_bounds(records_after, args.margin)
        if all_bounds is None:
            raise RuntimeError("could not derive whole-scene bounds from object AABBs")

        from og_ego_prim.utils.topdown_capture import capture_topdown_scene

        views: dict[str, Any] = {}
        views["global"] = capture_topdown_scene(
            benchmark.env,
            output_dir / "global_topdown.png",
            world_bounds=all_bounds,
            output_size=(1280, 720),
            camera_height=max(8.0, max(all_bounds[2] - all_bounds[0], all_bounds[3] - all_bounds[1]) * 1.25),
            settle_steps=10,
            show_robot=False,
            metadata_path=output_dir / "global_topdown.json",
        )
        _write_overlay(output_dir / "global_topdown.png", output_dir / "object_overlay.png", records_after, all_bounds)

        room_names = list(config.get("task", {}).get("rooms") or [])
        if not room_names:
            room_names = list(task_json.get("scene_info", {}).get("rooms") or [])
        for room in room_names:
            bounds = _room_bounds(records_after, room, args.margin) or all_bounds
            room_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", room)
            views[f"room:{room}"] = capture_topdown_scene(
                benchmark.env,
                output_dir / f"room_{room_key}_topdown.png",
                world_bounds=bounds,
                output_size=(1024, 576),
                camera_height=max(6.0, max(bounds[2] - bounds[0], bounds[3] - bounds[1]) * 1.4),
                settle_steps=10,
                show_robot=False,
                metadata_path=output_dir / f"room_{room_key}_topdown.json",
            )
            lower = np.asarray(bounds[:2], dtype=float)
            upper = np.asarray(bounds[2:], dtype=float)
            center_xy = (lower + upper) * 0.5
            span = np.maximum(upper - lower, 2.0)
            target = [float(center_xy[0]), float(center_xy[1]), 0.65]
            oblique_specs = [
                ("southwest", [-0.85, -0.85]),
                ("northeast", [0.85, 0.85]),
            ]
            for suffix, direction in oblique_specs:
                position = [
                    float(center_xy[0] + direction[0] * span[0]),
                    float(center_xy[1] + direction[1] * span[1]),
                    float(max(2.2, max(span) * 0.65)),
                ]
                views[f"oblique:{room}:{suffix}"] = _capture_oblique(
                    benchmark,
                    output_dir / f"room_{room_key}_oblique_{suffix}.png",
                    position,
                    target,
                )

        bddl_path = (
            Path(args.bddl)
            if args.bddl
            else REPO_ROOT / "data" / "bddl" / canonical_task_name / "problem0.bddl"
        )
        if not bddl_path.is_absolute():
            bddl_path = (REPO_ROOT / bddl_path).resolve()
        expected = _parse_expected_relations(bddl_path)
        sampled_scene_path = (
            output_dir
            / f"{args.scene}_task_{task_json_path.stem}_0_0_template.json"
        )
        benchmark.env.task.save_task(path=str(sampled_scene_path), override=True)

        removed = []
        for name in task_json.get("scene_info", {}).get("scene_file_remove_objects", []) or []:
            if not any(record["name"] == name for record in records_after):
                removed.append(name)
        finite_failures = [record["name"] for record in records_after if not (record["finite_pose"] and record["finite_motion"] and record["normalized_quaternion"])]
        settle_unstable = [
            record["name"] for record in records_before
            if (record.get("idle_displacement_m") or 0.0) > 0.05
            or (record.get("idle_linear_speed_mps") or 0.0) > 0.08
            or (record.get("idle_angular_speed_rps") or 0.0) > 0.8
        ]
        robot = benchmark.env.robots[0] if benchmark.env.robots else None
        robot_position, robot_orientation = (None, None)
        if robot is not None:
            robot_position, robot_orientation = robot.get_position_orientation()
            robot_position, robot_orientation = _json_value(robot_position), _json_value(robot_orientation)
        report = {
            "schema_version": "isbench.scene_initialization_audit.v1",
            "task": args.task,
            "canonical_task_name": canonical_task_name,
            "task_json_path": str(task_json_path),
            "bddl_path": str(bddl_path),
            "scene": args.scene,
            "online_object_sampling": False,
            "idle_window": {
                "steps": args.idle_steps,
                "mode": "executor_hold_action",
                "thresholds": {
                    "max_object_displacement_m": 0.05,
                    "max_linear_speed_mps": 0.08,
                    "max_angular_speed_rps": 0.8,
                },
                "settle_unstable_objects": settle_unstable,
            },
            "coverage": {
                "global_bounds": all_bounds,
                "bounds_source": "all_loaded_object_aabbs_plus_margin" if args.world_bounds is None else "explicit_world_bounds",
                "rooms": room_names,
                "views": views,
            },
            "expected_bddl_init": expected,
            "removed_scene_objects_absent": removed,
            "finite_pose_failures": finite_failures,
            "robot_after_idle": {
                "position": robot_position,
                "orientation_xyzw": robot_orientation,
                "finite": _finite_numbers(robot_position) and _finite_numbers(robot_orientation),
                "quaternion_norm": _norm(robot_orientation),
            },
            "object_count": len(records_after),
            "task_object_names": sorted(task_objects),
            "task_object_mapping": {
                bddl_name: str(getattr(obj, "name", ""))
                for bddl_name, obj in sorted(task_objects.items())
            },
            "objects_before_idle": records_before,
            "objects_after_idle": records_after,
            "sampled_scene_json": str(sampled_scene_path),
            "human_visual_review_pass": False,
            "runtime_pass": False,
            "status": "pending_human_visual_review",
        }
        (output_dir / "audit.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (output_dir / "human_review.md").write_text(
            "# Scene initialization visual review\n\n"
            "Complete only after opening every image listed in audit.json.\n\n"
            "- reviewer: pending\n"
            "- reviewed_at: pending\n"
            "- human_visual_review_pass: false\n"
            "- findings: pending\n",
            encoding="utf-8",
        )
        print(f"scene initialization audit: {output_dir}")
        print(f"objects={len(records_after)} rooms={len(room_names)} settle_unstable={settle_unstable}")
        return 0
    finally:
        if benchmark is not None:
            try:
                benchmark.close()
            except Exception:
                pass


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = int(main() or 0)
    except Exception:
        import traceback

        traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    if "omnigibson" in sys.modules:
        os._exit(exit_code)
    raise SystemExit(exit_code)
