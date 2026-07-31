"""Utilities for saving clean top-down RGB scene captures from OmniGibson."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
from PIL import Image


XY = tuple[float, float]


def camera_look_at_quaternion(
    position: Sequence[float],
    target: Sequence[float],
    *,
    up_hint: Optional[Sequence[float]] = None,
):
    """Return an xyzw quaternion that points the viewer camera at ``target``."""
    import torch as th
    from omnigibson.utils import transform_utils as T

    camera_position = th.tensor(position, dtype=th.float32)
    camera_target = th.tensor(target, dtype=th.float32)
    forward = camera_target - camera_position
    forward = forward / th.linalg.vector_norm(forward).clamp_min(1e-6)
    up = (
        th.tensor(up_hint, dtype=th.float32)
        if up_hint is not None
        else th.tensor([0.0, 0.0, 1.0], dtype=th.float32)
    )
    up = up / th.linalg.vector_norm(up).clamp_min(1e-6)
    if abs(float(th.dot(forward, up).item())) > 0.95:
        up = th.tensor([0.0, 1.0, 0.0], dtype=th.float32)
    right = th.linalg.cross(forward, up)
    right = right / th.linalg.vector_norm(right).clamp_min(1e-6)
    camera_up = th.linalg.cross(right, forward)
    camera_up = camera_up / th.linalg.vector_norm(camera_up).clamp_min(1e-6)
    optical_rotation = th.stack((right, camera_up, -forward), dim=1)
    return T.mat2quat(optical_rotation)


def capture_topdown_scene(
    env: Any,
    output_path: Path,
    *,
    world_bounds: Optional[Sequence[float]] = None,
    snapshot: Optional[dict[str, Any]] = None,
    execution_diagnostics: Optional[list[dict[str, Any]]] = None,
    output_size: tuple[int, int] = (1920, 1080),
    camera_height: Optional[float] = None,
    yaw_degrees: float = 0.0,
    camera_quat: Optional[Sequence[float]] = None,
    focal_length: Optional[float] = 17.0,
    margin: float = 1.0,
    settle_steps: int = 5,
    show_robot: bool = False,
    restore_camera: bool = True,
    metadata_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Save a clean top-down RGB render using the active OmniGibson viewer camera."""
    import omnigibson as og
    import torch as th

    bounds, bounds_source = _resolve_world_bounds(
        env,
        world_bounds=world_bounds,
        snapshot=snapshot,
        execution_diagnostics=execution_diagnostics,
        margin=margin,
    )
    min_x, min_y, max_x, max_y = bounds
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    span_x = max(max_x - min_x, 0.1)
    span_y = max(max_y - min_y, 0.1)

    floor_z = _current_floor_height(env)
    if camera_height is None:
        camera_height = max(8.0, max(span_x, span_y) * 1.25)
    camera_position = [center_x, center_y, floor_z + float(camera_height)]

    if camera_quat is None:
        yaw = math.radians(float(yaw_degrees))
        camera_quat_tensor = camera_look_at_quaternion(
            camera_position,
            [center_x, center_y, floor_z],
            up_hint=[-math.sin(yaw), math.cos(yaw), 0.0],
        )
    else:
        if len(camera_quat) != 4:
            raise ValueError("camera_quat must contain x y z w")
        camera_quat_tensor = th.tensor([float(value) for value in camera_quat], dtype=th.float32)

    viewer_camera = og.sim.viewer_camera
    old_pose = None
    old_size = None
    old_focal_length = None
    if restore_camera:
        try:
            old_position, old_orientation = viewer_camera.get_position_orientation()
            old_pose = (old_position, old_orientation)
        except Exception:
            old_pose = None
        old_size = (
            getattr(viewer_camera, "image_width", None),
            getattr(viewer_camera, "image_height", None),
        )
        old_focal_length = getattr(viewer_camera, "focal_length", None)

    old_robot_visibility = []
    for robot in getattr(env, "robots", []) or []:
        if hasattr(robot, "visible"):
            old_robot_visibility.append((robot, robot.visible))
            robot.visible = bool(show_robot)

    try:
        width, height = output_size
        og.sim.viewer_width = int(width)
        og.sim.viewer_height = int(height)
        if focal_length is not None:
            viewer_camera.focal_length = float(focal_length)
        viewer_camera.set_position_orientation(
            position=th.tensor(camera_position, dtype=th.float32),
            orientation=camera_quat_tensor,
        )
        for _ in range(max(0, int(settle_steps))):
            og.sim.step()
        # Replicator can lag a moved viewer camera by multiple app updates.
        for _ in range(6):
            og.sim.render()
        obs, _info = viewer_camera.get_obs()
        rgb = _rgb_array(obs.get("rgb"))

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb).save(output_path)

        metadata = {
            "saved": True,
            "image": str(output_path),
            "world_bounds": list(bounds),
            "bounds_source": bounds_source,
            "camera_position": camera_position,
            "camera_orientation_xyzw": [float(value) for value in camera_quat_tensor.detach().cpu().tolist()],
            "camera_height": float(camera_height),
            "floor_z": float(floor_z),
            "yaw_degrees": float(yaw_degrees),
            "focal_length": None if focal_length is None else float(focal_length),
            "output_size": [int(width), int(height)],
            "show_robot": bool(show_robot),
        }
        metadata_path = Path(metadata_path) if metadata_path is not None else output_path.with_suffix(".json")
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return metadata
    finally:
        for robot, visible in old_robot_visibility:
            robot.visible = visible
        if restore_camera and old_size is not None:
            old_width, old_height = old_size
            try:
                if old_width is not None:
                    og.sim.viewer_width = int(old_width)
                if old_height is not None:
                    og.sim.viewer_height = int(old_height)
            except Exception:
                pass
        if restore_camera and old_focal_length is not None:
            try:
                viewer_camera.focal_length = float(old_focal_length)
            except Exception:
                pass
        if restore_camera and old_pose is not None:
            try:
                viewer_camera.set_position_orientation(*old_pose)
                for _ in range(6):
                    og.sim.render()
            except Exception:
                pass


def save_topdown_occupancy_map(
    env: Any,
    output_path: Path,
    *,
    world_bounds: Optional[Sequence[float]] = None,
    snapshot: Optional[dict[str, Any]] = None,
    execution_diagnostics: Optional[list[dict[str, Any]]] = None,
    output_size: tuple[int, int] = (1920, 1080),
    margin: float = 1.0,
    free_color: Sequence[int] = (230, 235, 229),
    occupied_color: Sequence[int] = (42, 45, 50),
    unknown_color: Sequence[int] = (245, 247, 250),
    metadata_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Save a traversability / occupancy map over the same world bounds as a top-down capture."""
    bounds, bounds_source = _resolve_world_bounds(
        env,
        world_bounds=world_bounds,
        snapshot=snapshot,
        execution_diagnostics=execution_diagnostics,
        margin=margin,
    )
    floor_map, floor, trav_map = _current_floor_map(env)
    crop, crop_bounds_source = _crop_floor_map_to_bounds(floor_map, trav_map, bounds)
    # Traversability-map rows increase with world +Y, while camera image rows
    # increase toward world -Y. Flip vertically to match the top-down render.
    crop = np.flipud(crop)

    image_np = np.zeros((*crop.shape, 3), dtype=np.uint8)
    image_np[:, :] = np.asarray(occupied_color, dtype=np.uint8)
    image_np[crop > 0] = np.asarray(free_color, dtype=np.uint8)
    if crop.size == 0:
        image_np = np.zeros((1, 1, 3), dtype=np.uint8)
        image_np[:, :] = np.asarray(unknown_color, dtype=np.uint8)

    width, height = output_size
    image, resize_metadata = _resize_map_with_aspect_padding(
        image_np,
        output_size=(int(width), int(height)),
        padding_color=occupied_color,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)

    metadata = {
        "saved": True,
        "image": str(output_path),
        "world_bounds": list(bounds),
        "bounds_source": bounds_source,
        "floor": int(floor),
        "source_shape": list(floor_map.shape),
        "crop_shape": list(crop.shape),
        "crop_bounds_source": crop_bounds_source,
        "output_size": [int(width), int(height)],
        "free_color": [int(value) for value in free_color],
        "occupied_color": [int(value) for value in occupied_color],
        "unknown_color": [int(value) for value in unknown_color],
        "map_orientation": "world_x_right_world_y_up",
        "vertical_flip_to_match_camera": True,
        **resize_metadata,
    }
    metadata_path = Path(metadata_path) if metadata_path is not None else output_path.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return metadata


def _resize_map_with_aspect_padding(
    image_np: np.ndarray,
    *,
    output_size: tuple[int, int],
    padding_color: Sequence[int],
) -> tuple[Image.Image, dict[str, Any]]:
    """Resize a metric map without changing its horizontal / vertical scale."""
    width, height = output_size
    if width <= 0 or height <= 0:
        raise ValueError(f"output_size must be positive, got {output_size}")

    source_height, source_width = image_np.shape[:2]
    scale = min(width / source_width, height / source_height)
    content_width = max(1, min(width, round(source_width * scale)))
    content_height = max(1, min(height, round(source_height * scale)))
    resized = Image.fromarray(image_np).resize(
        (content_width, content_height),
        Image.Resampling.NEAREST,
    )

    left = (width - content_width) // 2
    top = (height - content_height) // 2
    right = width - content_width - left
    bottom = height - content_height - top
    image = Image.new("RGB", (width, height), tuple(int(value) for value in padding_color))
    image.paste(resized, (left, top))
    return image, {
        "resize_mode": "aspect_fit_with_occupied_padding",
        "content_size": [content_width, content_height],
        "padding_left_top_right_bottom": [left, top, right, bottom],
    }


def _rgb_array(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    arr = np.asarray(value)
    if arr.ndim != 3:
        raise ValueError(f"viewer RGB observation must be HxWxC, got shape={arr.shape}")
    if arr.shape[-1] > 3:
        arr = arr[..., :3]
    if not np.issubdtype(arr.dtype, np.number):
        raise ValueError(f"viewer RGB observation must be numeric, got dtype={arr.dtype}")
    values = np.asarray(arr, dtype=np.float32)
    if not np.isfinite(values).all():
        raise ValueError("viewer RGB observation contains non-finite values")
    if np.issubdtype(arr.dtype, np.floating) and values.size and float(values.max()) <= 1.0:
        values *= 255.0
    return np.clip(values, 0.0, 255.0).astype(np.uint8)


def _resolve_world_bounds(
    env: Any,
    *,
    world_bounds: Optional[Sequence[float]],
    snapshot: Optional[dict[str, Any]],
    execution_diagnostics: Optional[list[dict[str, Any]]],
    margin: float,
) -> tuple[list[float], str]:
    if world_bounds is not None:
        if len(world_bounds) != 4:
            raise ValueError("world_bounds must contain min_x min_y max_x max_y")
        min_x, min_y, max_x, max_y = [float(value) for value in world_bounds]
        if max_x <= min_x or max_y <= min_y:
            raise ValueError(f"invalid world_bounds: {world_bounds}")
        return [min_x, min_y, max_x, max_y], "explicit_world_bounds"

    points: list[XY] = []
    if isinstance(snapshot, dict):
        try:
            from og_ego_prim.scene_graph.visualization import _node_xy, _normalise_nodes

            points.extend(
                xy
                for node in _normalise_nodes(snapshot)
                for xy in [_node_xy(node)]
                if xy is not None
            )
        except Exception:
            pass

    try:
        from og_ego_prim.scene_graph.visualization import _extract_robot_pose, _extract_trajectories

        points.extend(point for path in _extract_trajectories(execution_diagnostics or [], snapshot=snapshot) for point in path)
        robot_pose = _extract_robot_pose(env)
        if robot_pose is not None:
            points.append(robot_pose[:2])
    except Exception:
        pass

    if points:
        return _point_bounds(points, padding=margin), "scene_graph_robot_trajectory"

    map_bounds = _trav_map_world_bounds(env)
    if map_bounds is not None:
        return map_bounds, "trav_map_extent"

    raise ValueError("Could not infer top-down bounds; pass --topdown-world-bounds.")


def _point_bounds(points: Iterable[XY], padding: float) -> list[float]:
    finite = [
        (float(x), float(y))
        for x, y in points
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]
    if not finite:
        raise ValueError("Cannot compute bounds from an empty point set")
    xs = [point[0] for point in finite]
    ys = [point[1] for point in finite]
    return [
        min(xs) - padding,
        min(ys) - padding,
        max(xs) + padding,
        max(ys) + padding,
    ]


def _current_floor_height(env: Any) -> float:
    trav_map = getattr(getattr(env, "scene", None), "trav_map", None)
    floor_heights = getattr(trav_map, "floor_heights", None) or [0.0]
    try:
        from og_ego_prim.scene_graph.visualization import _current_floor

        floor = _current_floor(env, trav_map)
    except Exception:
        floor = 0
    try:
        return float(floor_heights[floor])
    except Exception:
        return 0.0


def _trav_map_world_bounds(env: Any) -> Optional[list[float]]:
    scene = getattr(env, "scene", None)
    trav_map = getattr(scene, "trav_map", None)
    floor_maps = getattr(trav_map, "floor_map", None)
    if trav_map is None or floor_maps is None:
        return None
    try:
        from og_ego_prim.scene_graph.visualization import _current_floor, _to_numpy

        floor = _current_floor(env, trav_map)
        floor_map = _to_numpy(floor_maps[floor])
    except Exception:
        try:
            floor_map = np.asarray(floor_maps[0])
        except Exception:
            return None
    if floor_map.ndim != 2:
        return None
    height, width = floor_map.shape
    corners = [(0, 0), (0, width - 1), (height - 1, 0), (height - 1, width - 1)]
    points = []
    for row, col in corners:
        xy = _map_to_world(trav_map, (row, col))
        if xy is not None:
            points.append(xy)
    if not points:
        return None
    return _point_bounds(points, padding=0.0)


def _map_to_world(trav_map: Any, row_col: Sequence[float]) -> Optional[XY]:
    value = np.asarray(row_col, dtype=np.float32)
    for candidate in (value, value.tolist()):
        try:
            world = trav_map.map_to_world(candidate)
            arr = _to_numpy_local(world).reshape(-1)
            if arr.size >= 2 and np.all(np.isfinite(arr[:2])):
                return float(arr[0]), float(arr[1])
        except Exception:
            pass
    try:
        import torch as th

        world = trav_map.map_to_world(th.as_tensor(value, dtype=th.float32))
        arr = _to_numpy_local(world).reshape(-1)
        if arr.size >= 2 and np.all(np.isfinite(arr[:2])):
            return float(arr[0]), float(arr[1])
    except Exception:
        return None
    return None


def _current_floor_map(env: Any) -> tuple[np.ndarray, int, Any]:
    scene = getattr(env, "scene", None)
    trav_map = getattr(scene, "trav_map", None)
    floor_maps = getattr(trav_map, "floor_map", None)
    if trav_map is None or floor_maps is None:
        raise RuntimeError("The loaded scene does not expose env.scene.trav_map.floor_map.")
    try:
        from og_ego_prim.scene_graph.visualization import _current_floor, _to_numpy

        floor = _current_floor(env, trav_map)
        floor_map = _to_numpy(floor_maps[floor])
    except Exception:
        floor = 0
        floor_map = _to_numpy_local(floor_maps[0])
    floor_map = np.asarray(floor_map)
    if floor_map.ndim != 2 or floor_map.size == 0:
        raise RuntimeError(f"Invalid floor_map shape: {floor_map.shape}")
    return floor_map, floor, trav_map


def _crop_floor_map_to_bounds(
    floor_map: np.ndarray,
    trav_map: Any,
    bounds: Sequence[float],
) -> tuple[np.ndarray, str]:
    crop = _crop_pixels_from_world_bounds(trav_map, bounds, floor_map.shape)
    if crop is None:
        return floor_map, "full_map_crop_fallback"
    row_min, row_max, col_min, col_max = crop
    return floor_map[row_min:row_max, col_min:col_max], f"pixels:{row_min},{row_max},{col_min},{col_max}"


def _world_to_map(trav_map: Any, xy: XY) -> Optional[tuple[float, float]]:
    value = np.asarray(xy, dtype=np.float32)
    for candidate in (value, value.tolist()):
        try:
            map_xy = trav_map.world_to_map(candidate)
            arr = _to_numpy_local(map_xy).reshape(-1)
            if arr.size >= 2 and np.all(np.isfinite(arr[:2])):
                return float(arr[0]), float(arr[1])
        except Exception:
            pass
    try:
        import torch as th

        map_xy = trav_map.world_to_map(th.as_tensor(value, dtype=th.float32))
        arr = _to_numpy_local(map_xy).reshape(-1)
        if arr.size >= 2 and np.all(np.isfinite(arr[:2])):
            return float(arr[0]), float(arr[1])
    except Exception:
        return None
    return None


def _crop_pixels_from_world_bounds(
    trav_map: Any,
    bounds: Sequence[float],
    shape: tuple[int, int],
) -> Optional[tuple[int, int, int, int]]:
    if len(bounds) != 4:
        return None
    min_x, min_y, max_x, max_y = [float(value) for value in bounds]
    corners = [
        (min_x, min_y),
        (min_x, max_y),
        (max_x, min_y),
        (max_x, max_y),
    ]
    map_points = [_world_to_map(trav_map, corner) for corner in corners]
    map_points = [point for point in map_points if point is not None]
    if not map_points:
        return None
    rows = [point[0] for point in map_points]
    cols = [point[1] for point in map_points]
    height, width = shape
    row_min = max(0, int(math.floor(min(rows))))
    row_max = min(height, int(math.ceil(max(rows))) + 1)
    col_min = max(0, int(math.floor(min(cols))))
    col_max = min(width, int(math.ceil(max(cols))) + 1)
    if row_max <= row_min or col_max <= col_min:
        return None
    return row_min, row_max, col_min, col_max


def _to_numpy_local(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        return np.asarray(value.numpy())
    return np.asarray(value)
