"""Capture and check a whole-scene initialization gate for an IS-Bench task.

The audit deliberately performs no task action. It records the sampled scene,
holds the robot through a declared idle window, and captures global, per-room,
and oblique views before any navigation or manipulation.
"""

from __future__ import annotations

import argparse
import hashlib
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


def _quaternion_angle(first: Any, second: Any) -> Optional[float]:
    """Return the shortest rotation between two xyzw quaternions."""
    try:
        q1 = np.asarray(first, dtype=float)
        q2 = np.asarray(second, dtype=float)
        if q1.shape != (4,) or q2.shape != (4,):
            return None
        n1, n2 = np.linalg.norm(q1), np.linalg.norm(q2)
        if n1 <= 1e-8 or n2 <= 1e-8:
            return None
        dot = float(np.clip(abs(np.dot(q1 / n1, q2 / n2)), 0.0, 1.0))
        return float(2.0 * math.acos(dot))
    except (TypeError, ValueError):
        return None


def _safe_call(obj: Any, method: str, default: Any = None) -> Any:
    try:
        return getattr(obj, method)()
    except Exception:
        return default


def _hash_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _inherit_agent_task_mapping(source: Optional[Path], destination: Path) -> None:
    """Keep cached robot bindings that OmniGibson's save_task omits."""
    if source is None or not source.is_file() or not destination.is_file():
        return
    source_data = json.loads(source.read_text(encoding="utf-8"))
    destination_data = json.loads(destination.read_text(encoding="utf-8"))
    source_mapping = source_data.get("metadata", {}).get("task", {}).get("inst_to_name", {})
    agent_mapping = {
        name: sim_name
        for name, sim_name in source_mapping.items()
        if str(name).startswith("agent.")
    }
    if not agent_mapping:
        return
    destination_mapping = destination_data.setdefault("metadata", {}).setdefault(
        "task", {}
    ).setdefault("inst_to_name", {})
    destination_mapping.update(agent_mapping)
    destination.write_text(
        json.dumps(destination_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _image_check(path: Path) -> dict[str, Any]:
    from PIL import Image

    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not result["exists"]:
        result.update({"valid": False, "reason": "missing"})
        return result
    try:
        with Image.open(path) as image:
            rgb = np.asarray(image.convert("RGB"))
        mean = float(rgb.mean())
        std = float(rgb.std())
        result.update(
            {
                "valid": bool(
                    rgb.ndim == 3
                    and rgb.shape[-1] == 3
                    and rgb.size > 0
                    and np.isfinite(rgb).all()
                    and std > 0.0
                ),
                "size": [int(rgb.shape[1]), int(rgb.shape[0])],
                "mean": mean,
                "std": std,
                "nonuniform": std > 0.0,
            }
        )
    except Exception as exc:
        result.update({"valid": False, "reason": f"{type(exc).__name__}: {exc}"})
    return result


def _particle_positions(system: Any) -> Optional[np.ndarray]:
    instancers = getattr(system, "particle_instancers", None)
    if isinstance(instancers, Mapping):
        arrays = []
        for instancer in instancers.values():
            value = getattr(instancer, "particle_positions", None)
            if value is None:
                continue
            try:
                array = np.asarray(_json_value(value), dtype=float)
            except (TypeError, ValueError):
                continue
            if array.ndim == 2 and array.shape[1] == 3:
                arrays.append(array)
        if arrays:
            return np.concatenate(arrays, axis=0)
        try:
            return np.empty((0, 3), dtype=float) if int(system.n_particles) == 0 else None
        except (TypeError, ValueError, AttributeError, NotImplementedError):
            return None

    getter = getattr(system, "get_particles_position_orientation", None)
    candidates = []
    if callable(getter):
        try:
            value = getter()
            candidates.append(value[0] if isinstance(value, tuple) else value)
        except Exception:
            pass
    candidates.extend(
        [
            getattr(system, "particle_positions", None),
            _safe_call(system, "get_particle_positions"),
        ]
    )
    for value in candidates:
        if value is None:
            continue
        try:
            array = np.asarray(_json_value(value), dtype=float)
        except (TypeError, ValueError):
            continue
        if array.ndim == 2 and array.shape[1] == 3:
            return array
    return None


def _particle_summary(system: Any) -> tuple[dict[str, Any], Optional[np.ndarray]]:
    positions = _particle_positions(system)
    velocities = None
    instancers = getattr(system, "particle_instancers", None)
    if isinstance(instancers, Mapping):
        arrays = []
        for instancer in instancers.values():
            try:
                value = getattr(instancer, "particle_velocities", None)
            except Exception:
                value = None
            if value is None:
                continue
            try:
                array = np.asarray(_json_value(value), dtype=float)
            except (TypeError, ValueError):
                continue
            if array.ndim == 2 and array.shape[1] == 3:
                arrays.append(array)
        if arrays:
            velocities = np.concatenate(arrays, axis=0)
    else:
        try:
            value = getattr(system, "particle_velocities", None)
        except Exception:
            value = None
        if value is not None:
            try:
                candidate = np.asarray(_json_value(value), dtype=float)
                if candidate.ndim == 2 and candidate.shape[1] == 3:
                    velocities = candidate
            except (TypeError, ValueError):
                pass
    try:
        count = int(getattr(system, "n_particles"))
    except (TypeError, ValueError, AttributeError, NotImplementedError):
        count = None
    if positions is not None:
        finite = bool(_finite_numbers(positions))
        summary = {
            "name": str(getattr(system, "name", "")),
            "class": type(system).__name__,
            "n_particles": count if count is not None else int(len(positions)),
            "position_count": int(len(positions)),
            "finite_positions": finite,
            "position_min": positions.min(axis=0).tolist() if len(positions) else None,
            "position_max": positions.max(axis=0).tolist() if len(positions) else None,
            "position_centroid": positions.mean(axis=0).tolist() if len(positions) else None,
        }
    else:
        summary = {
            "name": str(getattr(system, "name", "")),
            "class": type(system).__name__,
            "n_particles": count,
            "position_count": None,
            "finite_positions": None,
            "position_min": None,
            "position_max": None,
            "position_centroid": None,
        }
    summary.update(
        {
            "finite_velocities": None if velocities is None else bool(_finite_numbers(velocities)),
            "velocity_count": None if velocities is None else int(len(velocities)),
            "max_speed_mps": (
                float(np.linalg.norm(velocities, axis=1).max())
                if velocities is not None and len(velocities) and _finite_numbers(velocities)
                else None
            ),
        }
    )
    return summary, positions


def _particle_snapshot(env: Any) -> tuple[list[dict[str, Any]], dict[str, Optional[np.ndarray]]]:
    registry = getattr(getattr(env, "scene", None), "system_registry", None)
    systems = list(getattr(registry, "objects", []) or [])
    summaries: list[dict[str, Any]] = []
    positions: dict[str, Optional[np.ndarray]] = {}
    for system in systems:
        summary, values = _particle_summary(system)
        try:
            summary["physical"] = bool(
                env.scene.is_physical_particle_system(system_name=system.name)
            )
        except Exception:
            summary["physical"] = None
        summaries.append(summary)
        positions[summary["name"]] = values
    summaries.sort(key=lambda item: item["name"])
    return summaries, positions


def _cloth_snapshot(obj: Any) -> Optional[dict[str, Any]]:
    root_link = getattr(obj, "root_link", None)
    compute = getattr(root_link, "compute_particle_positions", None)
    if not callable(compute):
        return None
    try:
        positions = np.asarray(_json_value(compute()), dtype=float)
    except Exception:
        return None
    if positions.ndim != 2 or positions.shape[1] != 3:
        return None
    velocities = None
    raw_velocities = getattr(root_link, "particle_velocities", None)
    if raw_velocities is not None:
        try:
            candidate = np.asarray(_json_value(raw_velocities), dtype=float)
            if candidate.shape == positions.shape:
                velocities = candidate
        except (TypeError, ValueError):
            pass
    return {"positions": positions, "velocities": velocities}


def _cloth_summary(snapshot: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if snapshot is None:
        return None
    positions = snapshot["positions"]
    velocities = snapshot.get("velocities")
    finite_velocities = None if velocities is None else bool(_finite_numbers(velocities))
    return {
        "vertex_count": int(len(positions)),
        "finite_positions": bool(_finite_numbers(positions)),
        "position_min": positions.min(axis=0).tolist() if len(positions) else None,
        "position_max": positions.max(axis=0).tolist() if len(positions) else None,
        "position_centroid": positions.mean(axis=0).tolist() if len(positions) else None,
        "max_speed_mps": (
            float(np.linalg.norm(velocities, axis=1).max())
            if velocities is not None and len(velocities) and finite_velocities
            else None
        ),
        "finite_velocities": finite_velocities,
    }


def _cloth_drift(before: Optional[dict[str, Any]], after: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if before is None or after is None:
        return None
    first = before["positions"]
    second = after["positions"]
    if first.shape != second.shape:
        return {"same_shape": False, "max_displacement_m": None, "mean_displacement_m": None}
    displacement = np.linalg.norm(second - first, axis=1)
    return {
        "same_shape": True,
        "max_displacement_m": float(displacement.max()) if len(displacement) else 0.0,
        "mean_displacement_m": float(displacement.mean()) if len(displacement) else 0.0,
    }


def _unordered_point_drift(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    """Compare particle clouds without assuming stable PhysX array indices."""
    if first.shape != second.shape:
        return {"same_shape": False, "max_displacement_m": None, "mean_displacement_m": None}
    if not len(first):
        return {"same_shape": True, "max_displacement_m": 0.0, "mean_displacement_m": 0.0}

    def nearest_distances(source: np.ndarray, target: np.ndarray) -> np.ndarray:
        chunk_size = max(1, 1_000_000 // len(target))
        chunks = []
        for start in range(0, len(source), chunk_size):
            pairwise = np.linalg.norm(
                source[start : start + chunk_size, None, :] - target[None, :, :],
                axis=2,
            )
            chunks.append(pairwise.min(axis=1))
        return np.concatenate(chunks)

    displacement = np.concatenate(
        [nearest_distances(first, second), nearest_distances(second, first)]
    )
    return {
        "same_shape": True,
        "max_displacement_m": float(displacement.max()),
        "mean_displacement_m": float(displacement.mean()),
    }


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


def _all_bounds(
    records: Iterable[Mapping[str, Any]],
    margin: float,
    particle_summaries: Iterable[Mapping[str, Any]] = (),
) -> Optional[list[float]]:
    points = []
    for record in records:
        bbox = record.get("aabb") or {}
        if "min" in bbox and "max" in bbox:
            points.extend([bbox["min"], bbox["max"]])
    for summary in particle_summaries:
        if summary.get("finite_positions"):
            lower = summary.get("position_min")
            upper = summary.get("position_max")
            if lower is not None and upper is not None:
                points.extend([lower, upper])
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
    # OmniGibson exposes rigid / cloth motion on the object's root link.  The
    # StatefulObject wrapper itself does not consistently forward these
    # methods, especially for fixed-base and deformable objects.
    motion_obj = getattr(obj, "root_link", None) or obj
    linear_velocity = _json_value(_safe_call(motion_obj, "get_linear_velocity"))
    angular_velocity = _json_value(_safe_call(motion_obj, "get_angular_velocity"))
    if linear_velocity is None and motion_obj is not obj:
        linear_velocity = _json_value(_safe_call(obj, "get_linear_velocity"))
    if angular_velocity is None and motion_obj is not obj:
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
        # Missing velocity APIs are recorded as unavailable; only values that
        # exist are subject to the finite-number gate.
        "motion_available": linear_velocity is not None or angular_velocity is not None,
        "finite_motion": all(
            _finite_numbers(value)
            for value in (linear_velocity, angular_velocity)
            if value is not None
        ),
        "normalized_quaternion": quaternion_norm is not None and abs(quaternion_norm - 1.0) <= 1e-2,
    }


def _parse_expected_relations(bddl_path: Path) -> dict[str, Any]:
    from bddl.parsing import scan_tokens

    expected: dict[str, Any] = {
        "ontop": [],
        "inside": [],
        "inroom": [],
        "particle_relations": [],
        "particle_source_relations": [],
        "states": [],
    }
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
        elif value and predicate == "insource" and len(arguments) == 2:
            expected["particle_source_relations"].append(
                {"predicate": predicate, "arguments": arguments, "declarative": True}
            )
        elif value and predicate in {"filled", "covered", "saturated", "contains"} and len(arguments) == 2:
            expected["particle_relations"].append(
                {"predicate": predicate, "arguments": arguments, "value": value}
            )
        elif len(arguments) == 1:
            expected["states"].append(
                {"predicate": predicate, "object": arguments[0], "value": value}
            )
    return expected


def _evaluate_initial_conditions(benchmark: Any) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate the exact native BDDL init conditions after post-load setup."""
    from og_ego_prim.benchmark.lifelong_evaluator import LifelongEvaluator
    from omnigibson.termination_conditions.predicate_goal import PredicateGoal

    evaluator = PredicateGoal(
        goal_fcn=lambda: list(getattr(benchmark.env.task, "activity_initial_conditions", []) or [])
    )
    _, success = evaluator.step(benchmark.env.task, benchmark.env, None)
    return bool(success), LifelongEvaluator._goal_atom_results(evaluator)


def _task_pose_snapshot(benchmark: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    scope = getattr(getattr(benchmark.env, "task", None), "object_scope", {}) or {}
    for bddl_name, ref in scope.items():
        obj = getattr(ref, "wrapped_obj", None)
        if obj is None or not hasattr(obj, "get_position_orientation"):
            continue
        try:
            position, orientation = obj.get_position_orientation()
        except Exception:
            continue
        snapshot[str(bddl_name)] = {
            "sim_name": str(getattr(obj, "name", "")),
            "position": _json_value(position),
            "orientation_xyzw": _json_value(orientation),
        }
    return snapshot


def _pose_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(before) | set(after)):
        first = before.get(name)
        second = after.get(name)
        if first is None or second is None:
            if first != second:
                changes.append({"object": name, "before": first, "after": second})
            continue
        displacement = _distance(first.get("position"), second.get("position"))
        if (displacement or 0.0) > 1e-6 or first.get("orientation_xyzw") != second.get("orientation_xyzw"):
            changes.append(
                {
                    "object": name,
                    "sim_name": second.get("sim_name"),
                    "displacement_m": displacement,
                    "before": first,
                    "after": second,
                }
            )
    return changes


def _loaded_room_names(benchmark: Any, records: Iterable[Mapping[str, Any]]) -> list[str]:
    names = {
        str(room)
        for record in records
        for room in (record.get("in_rooms") or [])
        if room
    }
    seg_map = getattr(getattr(benchmark.env, "scene", None), "seg_map", None)
    mapping = getattr(seg_map, "room_ins_id_to_ins_name", {}) or {}
    names.update(str(room) for room in mapping.values() if room)
    return sorted(names)


def _install_audit_benchmark_hooks(
    *,
    scene_file: Optional[Path],
    raw_scene_path: Path,
) -> tuple[Any, Any]:
    """Install process-local hooks for isolated scene loading and raw capture."""
    from og_ego_prim.benchmark.online_benchmark import ONLINE_BENCHMARKS, OnlineBehaviorBenchmark

    old_benchmark = ONLINE_BENCHMARKS["BehaviorTask"]

    class AuditedOnlineBehaviorBenchmark(OnlineBehaviorBenchmark):
        _audit_scene_file = scene_file
        _audit_raw_scene_path = raw_scene_path

        def _validate_cached_scene_requirements(
            self,
            scene_path: str,
            required_models: Mapping[str, Any],
        ) -> None:
            # The parent resolves and validates the canonical cache before this
            # audit hook can replace env_config["scene"]["scene_file"].  When
            # --scene-file is supplied, skip only that premature canonical
            # check; init_env_config below explicitly validates the isolated
            # candidate before installing it into the process-local config.
            if self._audit_scene_file is not None:
                candidate = self._audit_scene_file.resolve()
                if Path(scene_path).resolve() != candidate:
                    return
            OnlineBehaviorBenchmark._validate_cached_scene_requirements(
                scene_path,
                required_models,
            )

        def init_env_config(self, task: str, scene: str, config: dict[str, Any]):
            env_config = super().init_env_config(task, scene, config)
            if self._audit_scene_file is not None:
                if not self._audit_scene_file.is_file():
                    raise FileNotFoundError(f"scene-file does not exist: {self._audit_scene_file}")
                required_models = config.get("scene_info", {}).get("scene_asset_requirements", {}).get(
                    "required_instance_models", {}
                )
                OnlineBehaviorBenchmark._validate_cached_scene_requirements(
                    str(self._audit_scene_file),
                    required_models,
                )
                env_config["scene"]["scene_file"] = str(self._audit_scene_file)
            return env_config

        def _save_raw_sample(self) -> None:
            self._audit_raw_scene_path.parent.mkdir(parents=True, exist_ok=True)
            self.env.task.save_task(path=str(self._audit_raw_scene_path), override=True)
            _inherit_agent_task_mapping(self._audit_scene_file, self._audit_raw_scene_path)

        def _record_post_load_stage(self, stage: str, before: dict[str, Any]) -> None:
            after = _task_pose_snapshot(self)
            self._audit_post_load_steps.append(
                {"stage": stage, "pose_changes": _pose_changes(before, after)}
            )

        def _apply_scene_runtime_removals(self, config: dict[str, Any]) -> None:
            self._audit_post_load_steps = []
            self._save_raw_sample()
            before = _task_pose_snapshot(self)
            super()._apply_scene_runtime_removals(config)
            self._record_post_load_stage("scene_runtime_removals", before)

        def _apply_robot_initial_pose(self, config: dict[str, Any]) -> None:
            before = _task_pose_snapshot(self)
            super()._apply_robot_initial_pose(config)
            self._record_post_load_stage("robot_initial_pose", before)

        def _apply_object_initial_poses(self, config: dict[str, Any]) -> None:
            before = _task_pose_snapshot(self)
            super()._apply_object_initial_poses(config)
            self._record_post_load_stage("object_initial_poses", before)

        def _apply_object_initial_relations(self, config: dict[str, Any]) -> None:
            before = _task_pose_snapshot(self)
            super()._apply_object_initial_relations(config)
            self._record_post_load_stage("object_initial_relations", before)

        def _apply_task_trav_map_obstacles(self, config: dict[str, Any]) -> None:
            before = _task_pose_snapshot(self)
            super()._apply_task_trav_map_obstacles(config)
            self._record_post_load_stage("task_trav_map_obstacles", before)

    ONLINE_BENCHMARKS["BehaviorTask"] = AuditedOnlineBehaviorBenchmark
    return ONLINE_BENCHMARKS, old_benchmark


def _capture_oblique(benchmark: Any, path: Path, position: Sequence[float], target: Sequence[float]) -> dict[str, Any]:
    import omnigibson as og
    import torch
    from PIL import Image
    from og_ego_prim.utils.topdown_capture import camera_look_at_quaternion

    camera = og.sim.viewer_camera
    old_robot_visibility = []
    for robot in getattr(benchmark.env, "robots", []) or []:
        if hasattr(robot, "visible"):
            old_robot_visibility.append((robot, robot.visible))
            robot.visible = True
    old_pose = None
    try:
        old_pose = camera.get_position_orientation()
    except Exception:
        pass
    old_size = (og.sim.viewer_width, og.sim.viewer_height)
    try:
        og.sim.viewer_width = 960
        og.sim.viewer_height = 540
        quaternion = camera_look_at_quaternion(position, target)
        camera.set_position_orientation(
            position=torch.tensor(position, dtype=torch.float32),
            orientation=quaternion,
        )
        for _ in range(6):
            og.sim.render()
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
            "show_robot": True,
        }
    finally:
        for robot, visible in old_robot_visibility:
            robot.visible = visible
        og.sim.viewer_width, og.sim.viewer_height = old_size
        if old_pose is not None:
            try:
                camera.set_position_orientation(*old_pose)
                for _ in range(6):
                    og.sim.render()
            except Exception:
                pass


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
    parser.add_argument(
        "--online-object-sampling",
        action="store_true",
        help="Sample a fresh isolated candidate instead of loading the canonical cache.",
    )
    parser.add_argument(
        "--scene-file",
        default=None,
        help="Load this isolated cached scene JSON instead of the canonical scene file.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.idle_steps < 120:
        raise SystemExit("--idle-steps must be at least 120 for cloth/particle scenes")
    if args.online_object_sampling and args.scene_file:
        raise SystemExit("--online-object-sampling and --scene-file are mutually exclusive")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    scene_file = Path(args.scene_file).resolve() if args.scene_file else None

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
    task_json_path = get_task_config_path(args.task)
    task_json = json.loads(task_json_path.read_text(encoding="utf-8"))
    canonical_task_name = str(task_json["task_info"]["task_name"])
    config.setdefault("task", {})["primitive_type"] = str(
        task_json["task_info"].get("primitive_type", "ego")
    )
    runtime_config = RuntimeConfig.from_mapping(config)

    canonical_scene_filename = (
        f"{args.scene}_task_{canonical_task_name}_0_0_template.json"
    )
    raw_scene_path = output_dir / "raw_scene" / canonical_scene_filename
    settled_scene_path = output_dir / "settled_scene" / canonical_scene_filename
    hooks, old_benchmark = _install_audit_benchmark_hooks(
        scene_file=scene_file,
        raw_scene_path=raw_scene_path,
    )

    benchmark = None
    try:
        benchmark = build_benchmark(
            task=args.task,
            scene=args.scene,
            # Use the normal ego-view initialization path so OmniGibson warms
            # the camera/render pipeline before the first viewer capture.
            ego_view=True,
            draw_bbox_2d=False,
            primitive_type=None,
            scene_graph_step_interval=0,
            scene_graph_backend="disabled",
            use_initial_setup=False,
            use_self_caption=False,
            online_object_sampling=args.online_object_sampling,
            debug=False,
            eval_process_safety=False,
            eval_termination_safety=False,
            eval_awareness=False,
            eval_execution=False,
            runtime_config=runtime_config,
        )

        task_objects = _task_mapping(benchmark)
        all_objects = list(getattr(benchmark.env.scene, "objects", []) or [])
        robot = benchmark.env.robots[0] if benchmark.env.robots else None
        robot_before_position, robot_before_orientation = (None, None)
        if robot is not None:
            robot_before_position, robot_before_orientation = robot.get_position_orientation()
            robot_before_position = _json_value(robot_before_position)
            robot_before_orientation = _json_value(robot_before_orientation)
        records_before = [_object_record(obj, task_names={obj.name for obj in task_objects.values()}) for obj in all_objects]
        records_before.sort(key=lambda record: record["name"])
        cloth_before = {}
        for obj in all_objects:
            snapshot = _cloth_snapshot(obj)
            if snapshot is not None:
                cloth_before[str(getattr(obj, "name", ""))] = snapshot
        particle_before, particle_positions_before = _particle_snapshot(benchmark.env)

        # The only simulation after sampling is a hold/no-op settle window.
        benchmark.executor._simulator_loop(args.idle_steps)
        records_after = [_object_record(obj, task_names={obj.name for obj in task_objects.values()}) for obj in all_objects]
        records_after.sort(key=lambda record: record["name"])
        cloth_after = {}
        for obj in all_objects:
            snapshot = _cloth_snapshot(obj)
            if snapshot is not None:
                cloth_after[str(getattr(obj, "name", ""))] = snapshot
        particle_after, particle_positions_after = _particle_snapshot(benchmark.env)
        after_by_name = {record["name"]: record for record in records_after}
        for before in records_before:
            after = after_by_name.get(before["name"])
            if after is None:
                continue
            before["idle_displacement_m"] = _distance(before.get("position"), after.get("position"))
            before["idle_rotation_rad"] = _quaternion_angle(
                before.get("orientation_xyzw"), after.get("orientation_xyzw")
            )
            before["idle_linear_speed_mps"] = _norm(after.get("linear_velocity"))
            before["idle_angular_speed_rps"] = _norm(after.get("angular_velocity"))

        cloth_checks = []
        for name in sorted(set(cloth_before) | set(cloth_after)):
            before = cloth_before.get(name)
            after = cloth_after.get(name)
            item = {
                "object": name,
                "before": _cloth_summary(before),
                "after": _cloth_summary(after),
                "drift": _cloth_drift(before, after),
            }
            cloth_checks.append(item)

        before_by_system = {item["name"]: item for item in particle_before}
        after_by_system = {item["name"]: item for item in particle_after}
        particle_checks = []
        for name in sorted(set(before_by_system) | set(after_by_system)):
            before_present = name in before_by_system
            after_present = name in after_by_system
            first = before_by_system.get(name, {})
            second = after_by_system.get(name, {})
            first_positions = particle_positions_before.get(name)
            second_positions = particle_positions_after.get(name)
            drift = None
            if first_positions is not None and second_positions is not None and first_positions.shape == second_positions.shape:
                if first.get("physical"):
                    drift = _unordered_point_drift(first_positions, second_positions)
                else:
                    distances = np.linalg.norm(second_positions - first_positions, axis=1)
                    drift = {
                        "same_shape": True,
                        "max_displacement_m": float(distances.max()) if len(distances) else 0.0,
                        "mean_displacement_m": float(distances.mean()) if len(distances) else 0.0,
                    }
            elif first_positions is not None or second_positions is not None:
                drift = {"same_shape": False, "max_displacement_m": None, "mean_displacement_m": None}
            particle_checks.append(
                {
                    "name": name,
                    "before_present": before_present,
                    "after_present": after_present,
                    "before": first,
                    "after": second,
                    "drift": drift,
                }
            )

        all_bounds = _parse_bounds(args.world_bounds) or _all_bounds(
            records_after,
            args.margin,
            [*particle_before, *particle_after],
        )
        if all_bounds is None:
            raise RuntimeError("could not derive whole-scene bounds from object AABBs or particle positions")

        bddl_path = (
            Path(args.bddl)
            if args.bddl
            else REPO_ROOT / "data" / "bddl" / canonical_task_name / "problem0.bddl"
        )
        if not bddl_path.is_absolute():
            bddl_path = (REPO_ROOT / bddl_path).resolve()
        expected = _parse_expected_relations(bddl_path)
        try:
            initial_conditions_pass, initial_condition_atoms = _evaluate_initial_conditions(benchmark)
            initial_condition_error = None
        except Exception as exc:
            initial_conditions_pass = False
            initial_condition_atoms = []
            initial_condition_error = f"{type(exc).__name__}: {exc}"

        inroom_checks = []
        for subject_name, room_name in expected["inroom"]:
            subject = task_objects.get(subject_name)
            inroom_checks.append(
                {
                    "subject": subject_name,
                    "target": room_name,
                    "value": subject is not None
                    and room_name in (getattr(subject, "in_rooms", []) or []),
                    "error": None if subject is not None else "object not resolved",
                }
            )
        inroom_failures = [item for item in inroom_checks if not item["value"] or item["error"]]
        actual_init_relations = {"inroom": inroom_checks}

        pose_override_contacts: dict[str, Any] = {}
        for bddl_name in task_json.get("scene_info", {}).get("object_initial_poses", {}):
            obj = task_objects.get(bddl_name)
            if obj is None:
                pose_override_contacts[bddl_name] = {
                    "error": "object not resolved",
                    "pairs": [],
                }
                continue
            pairs = []
            seen = set()
            try:
                for contact in obj.contact_list():
                    pair = (str(contact.body0), str(contact.body1))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    pairs.append({"body0": pair[0], "body1": pair[1]})
            except Exception as exc:
                pose_override_contacts[bddl_name] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "pairs": [],
                }
                continue
            pose_override_contacts[bddl_name] = {
                "error": None,
                "pairs": pairs,
            }

        # Freeze the exact post-idle candidate before any camera capture can
        # advance the simulator or disturb dynamic objects.
        settled_scene_path.parent.mkdir(parents=True, exist_ok=True)
        benchmark.env.task.save_task(path=str(settled_scene_path), override=True)
        _inherit_agent_task_mapping(Path(args.scene_file).resolve() if args.scene_file else None, settled_scene_path)

        from og_ego_prim.utils.topdown_capture import (
            capture_topdown_scene,
            save_topdown_occupancy_map,
        )

        views: dict[str, Any] = {}
        views["global"] = capture_topdown_scene(
            benchmark.env,
            output_dir / "global_topdown.png",
            world_bounds=all_bounds,
            output_size=(1280, 720),
            camera_height=max(8.0, max(all_bounds[2] - all_bounds[0], all_bounds[3] - all_bounds[1]) * 1.25),
            settle_steps=0,
            show_robot=False,
            metadata_path=output_dir / "global_topdown.json",
        )
        occupancy = save_topdown_occupancy_map(
            benchmark.env,
            output_dir / "global_occupancy.png",
            world_bounds=all_bounds,
            output_size=(1280, 720),
            metadata_path=output_dir / "global_occupancy.json",
        )
        _write_overlay(output_dir / "global_topdown.png", output_dir / "object_overlay.png", records_after, all_bounds)

        task_rooms = list(config.get("task", {}).get("rooms") or [])
        if not task_rooms:
            task_rooms = list(task_json.get("scene_info", {}).get("rooms") or [])
        loaded_rooms = _loaded_room_names(benchmark, records_after)
        room_names = list(dict.fromkeys([*task_rooms, *loaded_rooms]))
        room_bounds_sources = {}
        room_bounds_fallbacks = []
        for room in room_names:
            bounds = _room_bounds(records_after, room, args.margin)
            if bounds is None:
                bounds = all_bounds
                room_bounds_sources[room] = "global_fallback"
                room_bounds_fallbacks.append(room)
            else:
                room_bounds_sources[room] = "room_object_aabbs"
            room_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", room)
            views[f"room:{room}"] = capture_topdown_scene(
                benchmark.env,
                output_dir / f"room_{room_key}_topdown.png",
                world_bounds=bounds,
                output_size=(1024, 576),
                camera_height=max(6.0, max(bounds[2] - bounds[0], bounds[3] - bounds[1]) * 1.4),
                settle_steps=0,
                show_robot=False,
                metadata_path=output_dir / f"room_{room_key}_topdown.json",
            )
            lower = np.asarray(bounds[:2], dtype=float)
            upper = np.asarray(bounds[2:], dtype=float)
            center_xy = (lower + upper) * 0.5
            span = np.maximum(upper - lower, 2.0)
            oblique_specs = [
                ("southwest", [-1.0, -1.0]),
                ("northeast", [1.0, 1.0]),
            ]
            for suffix, direction in oblique_specs:
                position = [
                    float(center_xy[0] + direction[0] * span[0] * 0.22),
                    float(center_xy[1] + direction[1] * span[1] * 0.22),
                    float(max(4.5, max(span) * 0.75)),
                ]
                target = [
                    float(center_xy[0]),
                    float(center_xy[1]),
                    0.65,
                ]
                views[f"oblique:{room}:{suffix}"] = _capture_oblique(
                    benchmark,
                    output_dir / f"room_{room_key}_oblique_{suffix}.png",
                    position,
                    target,
                )

        configured_removed = list(
            task_json.get("scene_info", {}).get("scene_file_remove_objects", []) or []
        )
        removed = [
            name
            for name in configured_removed
            if not any(record["name"] == name for record in records_after)
        ]
        finite_before_failures = [
            record["name"]
            for record in records_before
            if not (record["finite_pose"] and record["finite_motion"] and record["normalized_quaternion"])
        ]
        finite_failures = [record["name"] for record in records_after if not (record["finite_pose"] and record["finite_motion"] and record["normalized_quaternion"])]
        settle_unstable = [
            record["name"] for record in records_before
            if (record.get("idle_displacement_m") or 0.0) > 0.05
            or (record.get("idle_rotation_rad") or 0.0) > 0.2
            or (
                record.get("category") != "agent"
                and (record.get("idle_linear_speed_mps") or 0.0) > 0.08
            )
            or (
                record.get("category") != "agent"
                and (record.get("idle_angular_speed_rps") or 0.0) > 0.8
            )
            or (
                record.get("category") != "agent"
                and
                record.get("linear_velocity") is not None
                and (
                    not _finite_numbers(record["linear_velocity"])
                    or (_norm(record["linear_velocity"]) or 0.0) > 0.08
                )
            )
            or (
                record.get("category") != "agent"
                and
                record.get("angular_velocity") is not None
                and (
                    not _finite_numbers(record["angular_velocity"])
                    or (_norm(record["angular_velocity"]) or 0.0) > 0.8
                )
            )
        ]
        cloth_unstable = [
            item["object"]
            for item in cloth_checks
            if not (item.get("before") and item.get("after"))
            or not item["after"].get("finite_positions", False)
            or item["after"].get("finite_velocities") is False
            or (item.get("drift") or {}).get("same_shape") is False
            or (item.get("drift") or {}).get("max_displacement_m") is not None
            and (item.get("drift") or {}).get("max_displacement_m") > 0.05
            or (item.get("after") or {}).get("max_speed_mps") is not None
            and (
                not np.isfinite((item.get("after") or {}).get("max_speed_mps"))
                or (item.get("after") or {}).get("max_speed_mps") > 0.08
            )
        ]
        particle_count_changes = [
            item["name"]
            for item in particle_checks
            if item.get("before", {}).get("n_particles")
            != item.get("after", {}).get("n_particles")
        ]
        particle_nonfinite = [
            item["name"]
            for item in particle_checks
            if item.get("before", {}).get("finite_positions") is False
            or item.get("after", {}).get("finite_positions") is False
            or item.get("before", {}).get("finite_velocities") is False
            or item.get("after", {}).get("finite_velocities") is False
        ]
        particle_motion_unstable = [
            item["name"]
            for item in particle_checks
            if (item.get("before", {}).get("max_speed_mps") or 0.0) > 0.08
            or (item.get("after", {}).get("max_speed_mps") or 0.0) > 0.08
        ]
        particle_drift_unstable = [
            item["name"]
            for item in particle_checks
            if (item.get("drift") or {}).get("same_shape") is False
            or ((item.get("drift") or {}).get("max_displacement_m") or 0.0) > 0.05
        ]
        particle_presence_changes = [
            item["name"]
            for item in particle_checks
            if item.get("before_present") != item.get("after_present")
        ]
        particle_missing_positions = [
            item["name"]
            for item in particle_checks
            if (
                item.get("before_present")
                and (item.get("before", {}).get("n_particles") or 0) > 0
                and item.get("before", {}).get("position_count") is None
            )
            or (
                item.get("after_present")
                and (item.get("after", {}).get("n_particles") or 0) > 0
                and item.get("after", {}).get("position_count") is None
            )
        ]
        init_atom_failures = [
            atom for atom in initial_condition_atoms if not atom.get("satisfied", False)
        ]
        ignored_robot_pose_atoms = [
            atom
            for atom in init_atom_failures
            if any(str(argument).startswith("agent.") for argument in atom.get("arguments", []))
        ]
        physical_init_atom_failures = [
            atom for atom in init_atom_failures if atom not in ignored_robot_pose_atoms
        ]
        robot_position, robot_orientation = (None, None)
        if robot is not None:
            robot_position, robot_orientation = robot.get_position_orientation()
            robot_position, robot_orientation = _json_value(robot_position), _json_value(robot_orientation)
        robot_finite = _finite_numbers(robot_position) and _finite_numbers(robot_orientation)
        robot_before_quaternion_norm = _norm(robot_before_orientation)
        robot_quaternion_norm = _norm(robot_orientation)
        robot_displacement = _distance(robot_before_position, robot_position)
        robot_rotation = _quaternion_angle(robot_before_orientation, robot_orientation)
        view_checks = {
            key: _image_check(Path(value["image"]))
            for key, value in views.items()
            if isinstance(value, Mapping) and value.get("image")
        }
        view_checks["occupancy"] = _image_check(Path(occupancy["image"]))
        view_checks["overlay"] = _image_check(output_dir / "object_overlay.png")
        image_pass = all(item.get("valid", False) for item in view_checks.values())
        room_view_keys = {f"room:{room}" for room in room_names}
        oblique_view_keys = {
            f"oblique:{room}:{suffix}"
            for room in room_names
            for suffix in ("southwest", "northeast")
        }
        room_view_pass = room_view_keys.issubset(view_checks)
        oblique_view_pass = oblique_view_keys.issubset(view_checks)
        bounds_pass = all(record.get("aabb") for record in records_after)
        expected_object_names: set[str] = set()
        for predicate in ("ontop", "inside"):
            for subject, target in expected[predicate]:
                expected_object_names.update((str(subject), str(target)))
        expected_object_names.update(str(subject) for subject, _ in expected["inroom"])
        for relation_key in ("particle_relations", "particle_source_relations"):
            for relation in expected[relation_key]:
                expected_object_names.update(str(value) for value in relation["arguments"])
        expected_object_names.update(str(item["object"]) for item in expected["states"])
        expected_scene_object_names = {
            name for name in expected_object_names if not name.startswith("agent.")
        }
        task_mapping_pass = expected_scene_object_names.issubset(task_objects)
        machine_checks = {
            "initial_conditions_pass": initial_condition_error is None
            and not physical_init_atom_failures,
            "finite_objects_pass": not finite_before_failures and not finite_failures,
            "idle_settle_pass": not settle_unstable and not cloth_unstable,
            "particle_pass": not particle_count_changes
            and not particle_presence_changes
            and not particle_nonfinite
            and not particle_motion_unstable
            and not particle_drift_unstable
            and not particle_missing_positions,
            "inroom_pass": not inroom_failures,
            "removed_objects_pass": removed == configured_removed,
            "robot_pose_pass": robot_finite
            and _finite_numbers(robot_before_position)
            and _finite_numbers(robot_before_orientation)
            and robot_quaternion_norm is not None
            and robot_before_quaternion_norm is not None
            and abs(robot_before_quaternion_norm - 1.0) <= 1e-2
            and robot_displacement is not None
            and robot_rotation is not None
            and abs(robot_quaternion_norm - 1.0) <= 1e-2
            and robot_displacement <= 0.05
            and robot_rotation <= 0.2,
            "coverage_pass": bounds_pass
            and room_view_pass
            and oblique_view_pass
            and not room_bounds_fallbacks,
            "image_pass": image_pass,
            "scene_artifacts_pass": raw_scene_path.is_file() and settled_scene_path.is_file(),
            "task_mapping_pass": task_mapping_pass,
        }
        machine_pass = all(machine_checks.values())
        review_images = [
            {"key": key, "path": str(Path(value["image"]).resolve()), "status": "PENDING"}
            for key, value in views.items()
            if isinstance(value, Mapping) and value.get("image")
        ]
        review_images.extend(
            [
                {"key": "occupancy", "path": str(Path(occupancy["image"]).resolve()), "status": "PENDING"},
                {"key": "overlay", "path": str((output_dir / "object_overlay.png").resolve()), "status": "PENDING"},
            ]
        )
        human_review = {
            "schema_version": "isbench.scene_initialization_human_review.v1",
            "reviewer": "pending",
            "reviewed_at": "pending",
            "human_visual_review_pass": False,
            "images": review_images,
            "rooms": [{"room": room, "status": "PENDING", "findings": []} for room in room_names],
            "objects": [
                {"object": record["name"], "status": "PENDING", "findings": []}
                for record in records_after
            ],
            "findings": [],
        }
        report = {
            "schema_version": "isbench.scene_initialization_audit.v1",
            "task": args.task,
            "canonical_task_name": canonical_task_name,
            "task_json_path": str(task_json_path),
            "bddl_path": str(bddl_path),
            "scene": args.scene,
            "online_object_sampling": bool(args.online_object_sampling),
            "scene_file_override": str(scene_file) if scene_file is not None else None,
            "scene_artifacts": {
                "input_scene_sha256": _hash_file(scene_file) if scene_file is not None else None,
                "raw_sampled_scene_json": str(raw_scene_path),
                "raw_sampled_scene_sha256": _hash_file(raw_scene_path),
                "settled_scene_json": str(settled_scene_path),
                "settled_scene_sha256": _hash_file(settled_scene_path),
            },
            "post_load_stages": getattr(benchmark, "_audit_post_load_steps", []),
            "idle_window": {
                "steps": args.idle_steps,
                "mode": "executor_hold_action",
                "thresholds": {
                    "max_object_displacement_m": 0.05,
                    "max_object_rotation_rad": 0.2,
                    "max_linear_speed_mps": 0.08,
                    "max_angular_speed_rps": 0.8,
                },
                "settle_unstable_objects": settle_unstable,
                "cloth_unstable_objects": cloth_unstable,
            },
            "coverage": {
                "global_bounds": all_bounds,
                "bounds_source": "all_loaded_object_aabbs_and_particles_plus_margin" if args.world_bounds is None else "explicit_world_bounds",
                "rooms": room_names,
                "room_bounds_sources": room_bounds_sources,
                "room_bounds_fallbacks": room_bounds_fallbacks,
                "views": views,
                "occupancy": occupancy,
            },
            "expected_bddl_init": expected,
            "actual_bddl_init": actual_init_relations,
            "initial_condition_evaluation": {
                "pass": initial_conditions_pass,
                "error": initial_condition_error,
                "atom_results": initial_condition_atoms,
                "failures": init_atom_failures,
                "ignored_robot_pose_atoms": ignored_robot_pose_atoms,
                "physical_failures": physical_init_atom_failures,
            },
            "particle_systems_before_idle": particle_before,
            "particle_systems_after_idle": particle_after,
            "particle_idle_checks": particle_checks,
            "particle_presence_changes": particle_presence_changes,
            "particle_nonfinite_systems": particle_nonfinite,
            "particle_motion_unstable_systems": particle_motion_unstable,
            "particle_drift_unstable_systems": particle_drift_unstable,
            "particle_missing_position_systems": particle_missing_positions,
            "cloth_idle_checks": cloth_checks,
            "pose_override_contacts_after_idle": pose_override_contacts,
            "removed_scene_objects_absent": removed,
            "finite_pose_failures_before_idle": finite_before_failures,
            "finite_pose_failures": finite_failures,
            "robot_after_idle": {
                "before_position": robot_before_position,
                "before_orientation_xyzw": robot_before_orientation,
                "before_quaternion_norm": robot_before_quaternion_norm,
                "position": robot_position,
                "orientation_xyzw": robot_orientation,
                "finite": _finite_numbers(robot_position) and _finite_numbers(robot_orientation),
                "quaternion_norm": _norm(robot_orientation),
                "idle_displacement_m": robot_displacement,
                "idle_rotation_rad": robot_rotation,
            },
            "object_count": len(records_after),
            "task_object_names": sorted(task_objects),
            "task_object_mapping": {
                bddl_name: str(getattr(obj, "name", ""))
                for bddl_name, obj in sorted(task_objects.items())
            },
            "objects_before_idle": records_before,
            "objects_after_idle": records_after,
            "sampled_scene_json": str(settled_scene_path),
            "capture_success": image_pass,
            "coverage_pass": machine_checks["coverage_pass"],
            "machine_checks": machine_checks,
            "machine_pass": machine_pass,
            "view_checks": view_checks,
            "human_visual_review_pass": False,
            "runtime_pass": False,
            "status": "pending_human_visual_review" if machine_pass else "machine_failed",
        }
        (output_dir / "audit.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (output_dir / "human_review.json").write_text(
            json.dumps(human_review, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
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
        print(
            f"objects={len(records_after)} rooms={len(room_names)} "
            f"settle_unstable={settle_unstable} cloth_unstable={cloth_unstable} "
            f"machine_pass={machine_pass}"
        )
        return 0 if machine_pass else 2
    finally:
        if benchmark is not None:
            try:
                benchmark.close()
            except Exception:
                pass
        hooks["BehaviorTask"] = old_benchmark


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
