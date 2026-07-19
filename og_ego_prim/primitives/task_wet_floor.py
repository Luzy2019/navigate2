"""Task-local wet-floor regions that couple particles to navigation hazards."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


Point2D = Tuple[float, float]
MapCell = Tuple[int, int]
MAX_WET_FLOOR_PARTICLES = 96


def _point_in_polygon(point: Point2D, polygon: Sequence[Point2D]) -> bool:
    """Return whether a point is inside or on the boundary of a polygon."""
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (
            abs(cross) <= 1e-9
            and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
            and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
        ):
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x <= intersection_x:
                inside = not inside
        previous = current
    return inside


def polygon_grid_points(polygon: Sequence[Point2D], count: int) -> List[Point2D]:
    """Return deterministic, evenly distributed points inside a polygon."""
    if count <= 0:
        raise ValueError("count must be positive")
    if len(polygon) < 3:
        raise ValueError("polygon must contain at least three points")

    min_x = min(point[0] for point in polygon)
    max_x = max(point[0] for point in polygon)
    min_y = min(point[1] for point in polygon)
    max_y = max(point[1] for point in polygon)
    width = max_x - min_x
    height = max_y - min_y
    if width <= 0.0 or height <= 0.0:
        raise ValueError("polygon must have positive area")

    density = max(2, int(math.ceil(math.sqrt(count))))
    candidates: List[Point2D] = []
    while len(candidates) < count:
        columns = max(2, int(math.ceil(density * math.sqrt(width / height))))
        rows = max(2, int(math.ceil(density * math.sqrt(height / width))))
        candidates = []
        for row in range(rows):
            y = min_y + (row + 0.5) * height / rows
            for column in range(columns):
                x = min_x + (column + 0.5) * width / columns
                if _point_in_polygon((x, y), polygon):
                    candidates.append((x, y))
        density *= 2
        if density > 8192 and len(candidates) < count:
            raise ValueError("could not sample the configured wet-floor polygon")

    if len(candidates) == count:
        return candidates
    stride = len(candidates) / count
    return [candidates[min(int(index * stride), len(candidates) - 1)] for index in range(count)]


def polygon_map_cells(
    polygon: Sequence[Point2D],
    *,
    map_size: int,
    map_resolution: float,
    shape: Tuple[int, int],
) -> List[MapCell]:
    """Rasterize world-frame polygon points using OmniGibson map conventions."""
    if len(polygon) < 3:
        raise ValueError("polygon must contain at least three points")
    if map_size <= 0 or map_resolution <= 0.0:
        raise ValueError("map_size and map_resolution must be positive")
    height, width = shape
    min_x = min(point[0] for point in polygon)
    max_x = max(point[0] for point in polygon)
    min_y = min(point[1] for point in polygon)
    max_y = max(point[1] for point in polygon)

    row_min = max(0, int(math.floor(min_y / map_resolution + map_size / 2.0)) - 1)
    row_max = min(height - 1, int(math.ceil(max_y / map_resolution + map_size / 2.0)) + 1)
    col_min = max(0, int(math.floor(min_x / map_resolution + map_size / 2.0)) - 1)
    col_max = min(width - 1, int(math.ceil(max_x / map_resolution + map_size / 2.0)) + 1)

    cells = []
    for row in range(row_min, row_max + 1):
        world_y = (row - map_size / 2.0) * map_resolution
        for column in range(col_min, col_max + 1):
            world_x = (column - map_size / 2.0) * map_resolution
            if _point_in_polygon((world_x, world_y), polygon):
                cells.append((row, column))
    return cells


@dataclass(frozen=True)
class WetFloorRegionSpec:
    region_id: str
    target_object: str
    wet_tool_object: str
    dry_tool_objects: Tuple[str, ...]
    particle_system: str
    polygon_world_xy: Tuple[Point2D, ...]
    floor_index: int = 0
    particle_count: int = 96
    particle_height_offset: float = 0.005

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WetFloorRegionSpec":
        polygon = tuple(
            (float(point[0]), float(point[1]))
            for point in raw.get("polygon_world_xy", ())
        )
        spec = cls(
            region_id=str(raw.get("region_id", "")).strip(),
            target_object=str(raw.get("target_object", "")).strip(),
            wet_tool_object=str(raw.get("wet_tool_object", "")).strip(),
            dry_tool_objects=tuple(str(item).strip() for item in raw.get("dry_tool_objects", ())),
            particle_system=str(raw.get("particle_system", "")).strip(),
            polygon_world_xy=polygon,
            floor_index=int(raw.get("floor_index", 0)),
            particle_count=int(raw.get("particle_count", 96)),
            particle_height_offset=float(raw.get("particle_height_offset", 0.005)),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        for field_name in ("region_id", "target_object", "wet_tool_object", "particle_system"):
            if not getattr(self, field_name):
                raise ValueError(f"wet-floor region {field_name} must not be empty")
        if not self.dry_tool_objects:
            raise ValueError("wet-floor region dry_tool_objects must not be empty")
        if len(self.polygon_world_xy) < 3:
            raise ValueError("wet-floor region polygon_world_xy needs at least three points")
        if self.floor_index < 0:
            raise ValueError("wet-floor region floor_index must be non-negative")
        if not 1 <= self.particle_count <= MAX_WET_FLOOR_PARTICLES:
            raise ValueError(
                "wet-floor region particle_count must be between 1 and "
                f"{MAX_WET_FLOOR_PARTICLES}"
            )
        if self.particle_height_offset < 0.0:
            raise ValueError("wet-floor region particle_height_offset must be non-negative")
        polygon_grid_points(self.polygon_world_xy, 1)


class TaskWetFloorRegionController:
    """Apply configured wet WIPE side effects without changing unrelated tasks."""

    def __init__(self, env: Any) -> None:
        self.env = env
        self.specs: Tuple[WetFloorRegionSpec, ...] = ()
        self._blocked_cells: Dict[str, Dict[MapCell, Any]] = {}
        self.last_event: Optional[Dict[str, Any]] = None
        self.configure(self._task_region_configs())

    def configure(self, raw_specs: Iterable[Mapping[str, Any]] | None) -> None:
        specs = tuple(WetFloorRegionSpec.from_mapping(raw) for raw in (raw_specs or ()))
        region_ids = [spec.region_id for spec in specs]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("wet-floor region_id values must be unique")
        self.specs = specs

    def _task_region_configs(self) -> Iterable[Mapping[str, Any]]:
        # Tests and custom environment builders may attach the complete task
        # definition directly. Prefer that current-environment metadata before
        # resolving the activity name through the repository task registry.
        for config in self._environment_task_configs():
            scene_info = config.get("scene_info", {})
            if isinstance(scene_info, Mapping) and "wet_floor_regions" in scene_info:
                return scene_info.get("wet_floor_regions") or ()
            if "wet_floor_regions" in config:
                return config.get("wet_floor_regions") or ()

        task = getattr(self.env, "task", None)
        task_name = getattr(task, "activity_name", None) or getattr(task, "_activity_name", None)
        if not task_name:
            env_config = getattr(self.env, "config", {}) or {}
            task_config = env_config.get("task", {}) if isinstance(env_config, Mapping) else {}
            task_name = task_config.get("activity_name") if isinstance(task_config, Mapping) else None
        if not task_name:
            return ()

        from og_ego_prim.utils.task_registry import get_task_config_path

        try:
            config_path = get_task_config_path(str(task_name))
            with open(config_path, "r", encoding="utf-8") as config_file:
                task_config = json.load(config_file)
        except (FileNotFoundError, KeyError, ValueError):
            return ()
        scene_info = task_config.get("scene_info", {})
        return scene_info.get("wet_floor_regions", ())

    def _environment_task_configs(self) -> Iterable[Mapping[str, Any]]:
        candidates = (
            getattr(self.env, "task_definition", None),
            getattr(self.env, "task_config", None),
            getattr(self.env, "config", None),
            getattr(self.env, "_config", None),
            getattr(self.env, "metadata", None),
            getattr(getattr(self.env, "task", None), "metadata", None),
        )
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            nested = candidate.get("task_definition")
            if isinstance(nested, Mapping):
                yield nested
            yield candidate

    def after_wipe(self, primitive: Any, target_obj: Any, cleaning_tool: Any):
        for spec in self.specs:
            if not self._matches_task_entity(target_obj, spec.target_object):
                continue
            if self._matches_task_entity(cleaning_tool, spec.wet_tool_object):
                system = self._resolve_system(spec.particle_system)
                if not self._is_saturated(cleaning_tool, system):
                    self._raise_action_error(
                        "PRE_CONDITION_ERROR",
                        "The configured wet-floor tool is not saturated with the required fluid.",
                        {"region": spec.region_id, "tool": cleaning_tool.name, "system": system.name},
                    )
                yield from self._establish_wet_region(primitive, spec, target_obj, system)
                return
            if any(
                self._matches_task_entity(cleaning_tool, identifier)
                for identifier in spec.dry_tool_objects
            ):
                system = self._resolve_system(spec.particle_system)
                if self._is_covered(target_obj, system):
                    self._raise_action_error(
                        "POST_CONDITION_ERROR",
                        "The dry WIPE did not remove the configured wet-floor water.",
                        {"region": spec.region_id, "target": target_obj.name, "system": system.name},
                    )
                restored = self._restore_region(spec)
                self.last_event = {
                    "region_id": spec.region_id,
                    "state": "dry",
                    "restored_cells": restored,
                }
                return

    def mark_wet_region(self, target_obj: Any) -> int:
        """Activate A* avoidance only after the plan chooses that safety branch."""

        for spec in self.specs:
            if not self._matches_task_entity(target_obj, spec.target_object):
                continue
            system = self._resolve_system(spec.particle_system)
            if not self._is_covered(target_obj, system):
                self._raise_action_error(
                    "PRE_CONDITION_ERROR",
                    "Only a currently wet configured region can be marked for avoidance.",
                    {
                        "region": spec.region_id,
                        "target": target_obj.name,
                        "system": system.name,
                    },
                )
            changed = self._block_region(spec)
            self.last_event = {
                "region_id": spec.region_id,
                "state": "wet_marked",
                "blocked_cells": changed,
            }
            print(
                "[task_wet_floor] "
                f"region={spec.region_id} state=wet_marked blocked_cells={changed}",
                flush=True,
            )
            return changed

        self._raise_action_error(
            "PRE_CONDITION_ERROR",
            "The target is not a configured wet-floor region.",
            {"target": getattr(target_obj, "name", None)},
        )

    def is_marked(self, target_obj: Any) -> bool:
        """Return whether the target's configured region is already blocked."""

        return any(
            spec.region_id in self._blocked_cells
            for spec in self.specs
            if self._matches_task_entity(target_obj, spec.target_object)
        )

    def restore_marked_region(self, target_obj: Any) -> int:
        """Restore the exact map snapshot for a configured target region."""

        for spec in self.specs:
            if self._matches_task_entity(target_obj, spec.target_object):
                return self._restore_region(spec)
        self._raise_action_error(
            "PRE_CONDITION_ERROR",
            "The target is not a configured wet-floor region.",
            {"target": getattr(target_obj, "name", None)},
        )

    def reserved_saturated_system_names(
        self,
        target_obj: Any,
        cleaning_tool: Any,
    ) -> frozenset[str]:
        """Return fluids generated by the bounded task hook, not generic WIPE."""

        return frozenset(
            spec.particle_system
            for spec in self.specs
            if self._matches_task_entity(target_obj, spec.target_object)
            and self._matches_task_entity(cleaning_tool, spec.wet_tool_object)
        )

    def _establish_wet_region(
        self,
        primitive: Any,
        spec: WetFloorRegionSpec,
        target_obj: Any,
        system: Any,
    ):
        from omnigibson import object_states
        import torch

        covered = target_obj.states[object_states.Covered]
        if covered.get_value(system):
            covered.set_value(system, False)
            yield from primitive._settle_robot()
            if covered.get_value(system):
                self._raise_action_error(
                    "POST_CONDITION_ERROR",
                    "The wet-floor hook could not clear the previous water patch.",
                    {
                        "region": spec.region_id,
                        "target": target_obj.name,
                        "system": system.name,
                    },
                )

        points = polygon_grid_points(spec.polygon_world_xy, spec.particle_count)
        upper_z = float(target_obj.aabb[1][2].item())
        particle_radius = float(getattr(system, "particle_radius", 0.01) or 0.01)
        z = upper_z + particle_radius + spec.particle_height_offset
        positions = torch.tensor(
            [(x, y, z) for x, y in points],
            dtype=torch.float32,
        )
        particle_count_before = int(system.n_particles)
        try:
            system.generate_particles(positions=positions)
            yield from primitive._settle_robot()

            particle_count_after = int(system.n_particles)
            generated_count = particle_count_after - particle_count_before
            if generated_count != spec.particle_count:
                self._raise_action_error(
                    "POST_CONDITION_ERROR",
                    "The wet-floor hook did not generate the configured particle count.",
                    {
                        "region": spec.region_id,
                        "expected": spec.particle_count,
                        "generated": generated_count,
                    },
                )

            if not covered.get_value(system):
                self._raise_action_error(
                    "POST_CONDITION_ERROR",
                    "The bounded wet-floor particles did not establish floor coverage.",
                    {
                        "region": spec.region_id,
                        "target": target_obj.name,
                        "system": system.name,
                    },
                )
        except Exception as action_error:
            particle_count_after = int(system.n_particles)
            if particle_count_after > particle_count_before:
                try:
                    system.remove_particles(
                        idxs=torch.arange(
                            particle_count_before,
                            particle_count_after,
                            dtype=torch.long,
                        )
                    )
                    yield from primitive._settle_robot()
                except Exception as rollback_error:
                    self._raise_action_error(
                        "POST_CONDITION_ERROR",
                        "The wet-floor hook failed and could not remove its partial particles.",
                        {
                            "region": spec.region_id,
                            "target": target_obj.name,
                            "system": system.name,
                            "action_error": str(action_error),
                            "rollback_error": str(rollback_error),
                        },
                    )
            raise
        self.last_event = {
            "region_id": spec.region_id,
            "state": "wet_unmarked",
            "particle_count": spec.particle_count,
            "blocked_cells": 0,
        }
        print(
            "[task_wet_floor] "
            f"region={spec.region_id} state=wet_unmarked "
            f"particles={spec.particle_count}",
            flush=True,
        )

    def _block_region(self, spec: WetFloorRegionSpec) -> int:
        trav_map = self.env.scene.trav_map
        if spec.floor_index >= len(trav_map.floor_map):
            raise ValueError(
                f"wet-floor region floor_index {spec.floor_index} exceeds scene floors"
            )
        floor_map = trav_map.floor_map[spec.floor_index]
        shape = tuple(int(value) for value in floor_map.shape)
        cells = polygon_map_cells(
            spec.polygon_world_xy,
            map_size=int(trav_map.map_size),
            map_resolution=float(trav_map.map_resolution),
            shape=shape,
        )
        if not cells:
            raise ValueError(f"wet-floor region {spec.region_id!r} does not overlap the trav map")

        snapshot = dict(self._blocked_cells.get(spec.region_id, {}))
        values_before_call: Dict[MapCell, Any] = {}
        changed = 0
        try:
            for row, column in cells:
                cell = (row, column)
                current = floor_map[row, column]
                saved_current = current.clone() if hasattr(current, "clone") else current
                values_before_call[cell] = saved_current
                if cell not in snapshot:
                    snapshot[cell] = saved_current
                scalar = current.item() if hasattr(current, "item") else current
                if scalar != 0:
                    changed += 1
                floor_map[row, column] = 0
        except Exception:
            for (row, column), value in values_before_call.items():
                floor_map[row, column] = value
            raise
        self._blocked_cells[spec.region_id] = snapshot
        return changed

    def _restore_region(self, spec: WetFloorRegionSpec) -> int:
        snapshot = self._blocked_cells.get(spec.region_id)
        if not snapshot:
            return 0
        floor_map = self.env.scene.trav_map.floor_map[spec.floor_index]
        restored_cells = []
        try:
            for (row, column), value in snapshot.items():
                floor_map[row, column] = value
                restored_cells.append((row, column))
        except Exception:
            for row, column in restored_cells:
                floor_map[row, column] = 0
            raise
        self._blocked_cells.pop(spec.region_id, None)
        print(
            "[task_wet_floor] "
            f"region={spec.region_id} state=dry restored_cells={len(snapshot)}",
            flush=True,
        )
        return len(snapshot)

    def _resolve_task_entity(self, identifier: str) -> Any:
        scope = getattr(getattr(self.env, "task", None), "object_scope", {})
        reference = scope.get(identifier)
        return getattr(reference, "wrapped_obj", reference)

    def _matches_task_entity(self, candidate: Any, identifier: str) -> bool:
        expected = self._resolve_task_entity(identifier)
        if expected is not None:
            return candidate is expected
        return getattr(candidate, "name", None) == identifier

    def _resolve_system(self, name: str) -> Any:
        get_system = self.env.scene.get_system
        try:
            system = get_system(name, force_init=True)
        except TypeError:
            system = get_system(name)
        if system is None:
            raise KeyError(f"configured wet-floor particle system not found: {name}")
        return system

    @staticmethod
    def _is_saturated(tool: Any, system: Any) -> bool:
        from omnigibson import object_states

        states = getattr(tool, "states", {})
        return bool(
            object_states.Saturated in states
            and states[object_states.Saturated].get_value(system)
        )

    @staticmethod
    def _is_covered(target: Any, system: Any) -> bool:
        from omnigibson import object_states

        states = getattr(target, "states", {})
        return bool(
            object_states.Covered in states
            and states[object_states.Covered].get_value(system)
        )

    @staticmethod
    def _raise_action_error(reason_name: str, message: str, context: Mapping[str, Any]) -> None:
        from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError

        raise ActionPrimitiveError(
            getattr(ActionPrimitiveError.Reason, reason_name),
            message,
            dict(context),
        )


__all__ = [
    "MAX_WET_FLOOR_PARTICLES",
    "TaskWetFloorRegionController",
    "WetFloorRegionSpec",
    "polygon_grid_points",
    "polygon_map_cells",
]
