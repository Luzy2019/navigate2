import hashlib
import json
import math
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont


XY = Tuple[float, float]
Pixel = Tuple[float, float]
DEFAULT_BEV_MAX_SIDE = 3840


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(0.1, value)


def _style_scale(max_side: int) -> float:
    default = max(1.0, min(2.6, max_side / 1600.0))
    return _env_float("ISBENCH_SCENE_GRAPH_BEV_FONT_SCALE", default)


def _metadata_filename(filename: str) -> str:
    path = Path(filename)
    if path.suffix:
        return f"{path.stem}.json"
    return f"{filename}.json"


def save_scene_graph_task_scene_bev_visualization(
    snapshot: Optional[Dict[str, Any]],
    output_dir: Path,
    *,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    task_room: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    return save_scene_graph_bev_visualization(
        snapshot,
        output_dir,
        env=env,
        execution_diagnostics=execution_diagnostics,
        filename="scene_graph_bev_task_scene.png",
        metadata_filename="scene_graph_bev_task_scene.json",
        task_room=task_room,
        crop_to_task_scene=True,
    )


def save_scene_graph_task_scene_bev_video(
    snapshots: Optional[List[Dict[str, Any]]],
    output_dir: Path,
    *,
    latest_snapshot: Optional[Dict[str, Any]] = None,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    task_room: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    return save_scene_graph_bev_video(
        snapshots,
        output_dir,
        latest_snapshot=latest_snapshot,
        env=env,
        execution_diagnostics=execution_diagnostics,
        filename="scene_graph_bev_task_scene_history.mp4",
        metadata_filename="scene_graph_bev_task_scene_history.json",
        task_room=task_room,
        crop_to_task_scene=True,
    )


def lifelong_scene_graph_snapshot(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert metadata.lifelong_scene_graph into the standard snapshot shape."""
    if snapshot is None:
        return None
    metadata = snapshot.get("metadata") or {}
    lifelong = metadata.get("lifelong_scene_graph")
    if lifelong is None:
        backend_metadata = metadata.get("backend_metadata") or {}
        lifelong = backend_metadata.get("lifelong_scene_graph")
    if not isinstance(lifelong, dict):
        return None

    native_nodes = [node for node in lifelong.get("nodes", []) if isinstance(node, dict)]
    native_edges = [edge for edge in lifelong.get("edges", []) if isinstance(edge, dict)]
    id_map: Dict[Any, str] = {}
    uid_map: Dict[Any, str] = {}
    nodes = []
    for node in native_nodes:
        uid = node.get("uid")
        object_id = str(node.get("object_id") or f"lifelong:{uid if uid is not None else node.get('id')}")
        label = str(node.get("label") or node.get("id") or object_id)
        id_map[node.get("id")] = object_id
        if uid is not None:
            uid_map[uid] = object_id
        nodes.append(
            {
                "object_id": object_id,
                "name": label,
                "category": label,
                "visible": bool(node.get("is_vis", False)),
                "position": node.get("position"),
                "states": {
                    "uid": uid,
                    "native_id": node.get("id"),
                    "is_coarse": bool(node.get("is_coarse", True)),
                    "is_vis": bool(node.get("is_vis", False)),
                    "last_seen_step": node.get("last_seen_step"),
                    "source": "lifelong_scene_graph",
                },
            }
        )

    edges = []
    for edge in native_edges:
        source_id = id_map.get(edge.get("source")) or uid_map.get(edge.get("source_uid"))
        target_id = id_map.get(edge.get("target")) or uid_map.get(edge.get("target_uid"))
        if source_id is None or target_id is None:
            continue
        edges.append(
            {
                "source_id": source_id,
                "target_id": target_id,
                "relation": edge.get("type"),
                "source": "lifelong_scene_graph",
                "confidence": 1.0,
            }
        )

    return {
        "step_index": snapshot.get("step_index"),
        "primitive_name": snapshot.get("primitive_name"),
        "raw_plan": snapshot.get("raw_plan"),
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            **metadata,
            "perception_backend": metadata.get("perception_backend"),
            "scene_graph_source": "metadata.lifelong_scene_graph",
            "lifelong_summary": summarize_lifelong_scene_graph(snapshot),
        },
    }


def summarize_lifelong_scene_graph(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    lifelong_snapshot = None
    if snapshot is not None:
        metadata = snapshot.get("metadata") or {}
        lifelong_snapshot = metadata.get("lifelong_scene_graph")
        if lifelong_snapshot is None:
            lifelong_snapshot = (metadata.get("backend_metadata") or {}).get("lifelong_scene_graph")
    if not isinstance(lifelong_snapshot, dict):
        return {
            "nodes": 0,
            "edges": 0,
            "coarse_nodes": 0,
            "fine_nodes": 0,
            "visible_nodes": 0,
            "relation_counts": {},
        }
    nodes = [node for node in lifelong_snapshot.get("nodes", []) if isinstance(node, dict)]
    edges = [edge for edge in lifelong_snapshot.get("edges", []) if isinstance(edge, dict)]
    relation_counts: Dict[str, int] = {}
    for edge in edges:
        relation = str(edge.get("type") or "unknown")
        relation_counts[relation] = relation_counts.get(relation, 0) + 1
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "coarse_nodes": sum(1 for node in nodes if node.get("is_coarse")),
        "fine_nodes": sum(1 for node in nodes if not node.get("is_coarse")),
        "visible_nodes": sum(1 for node in nodes if node.get("is_vis")),
        "relation_counts": relation_counts,
    }


def save_lifelong_scene_graph_bev_visualization(
    snapshot: Optional[Dict[str, Any]],
    output_dir: Path,
    *,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    filename: str = "lifelong_scene_graph_bev.png",
    metadata_filename: str = "lifelong_scene_graph_bev.json",
    task_room: Optional[str] = None,
    crop_to_task_scene: bool = False,
) -> Optional[Dict[str, Any]]:
    converted = lifelong_scene_graph_snapshot(snapshot)
    if converted is None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "saved": False,
            "reason": "missing_lifelong_scene_graph",
            "scene_graph_source": "metadata.lifelong_scene_graph",
        }
        (output_dir / metadata_filename).write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return metadata
    return save_scene_graph_bev_visualization(
        converted,
        output_dir,
        env=env,
        execution_diagnostics=execution_diagnostics,
        filename=filename,
        metadata_filename=metadata_filename,
        task_room=task_room,
        crop_to_task_scene=crop_to_task_scene,
    )


def save_scene_graph_bev_visualization(
    snapshot: Optional[Dict[str, Any]],
    output_dir: Path,
    *,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    filename: str = "scene_graph_bev.png",
    metadata_filename: Optional[str] = None,
    task_room: Optional[str] = None,
    crop_to_task_scene: bool = False,
) -> Optional[Dict[str, Any]]:
    """Save a final BEV map overlaid with scene-graph nodes and relations."""
    if snapshot is None:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    canvas, metadata = _render_scene_graph_bev(
        snapshot,
        env=env,
        execution_diagnostics=execution_diagnostics,
        task_room=task_room,
        crop_to_task_scene=crop_to_task_scene,
    )
    metadata_path = output_dir / (metadata_filename or _metadata_filename(filename))
    if canvas is None:
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return metadata

    image_path = output_dir / filename
    canvas.save(image_path)
    metadata["image"] = str(image_path)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return metadata


def save_scene_graph_bev_video(
    snapshots: Optional[List[Dict[str, Any]]],
    output_dir: Path,
    *,
    latest_snapshot: Optional[Dict[str, Any]] = None,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    filename: str = "scene_graph_bev_history.mp4",
    metadata_filename: Optional[str] = None,
    task_room: Optional[str] = None,
    crop_to_task_scene: bool = False,
) -> Optional[Dict[str, Any]]:
    """Save a time-lapse BEV video from scene_graph_history snapshots."""
    timeline = _history_snapshots(snapshots, latest_snapshot)
    if not timeline:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / (metadata_filename or _metadata_filename(filename))
    video_path = output_dir / filename
    renderable = [
        snapshot
        for snapshot in timeline
        if any(_node_xy(node) is not None for node in _normalise_nodes(snapshot))
    ]
    if not renderable:
        metadata = {
            "saved": False,
            "reason": "no_positioned_scene_graph_nodes",
            "frame_count": 0,
            "scene_graph_source": "benchmark.tracker.scene_graph_history",
            "simulator_truth_overlay": False,
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return metadata

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        metadata = {
            "saved": False,
            "reason": "ffmpeg_not_found",
            "frame_count": len(renderable),
            "scene_graph_source": "benchmark.tracker.scene_graph_history",
            "simulator_truth_overlay": False,
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return metadata

    fixed_task_scene = None
    if crop_to_task_scene:
        fixed_task_scene = _history_task_scene_context(
            renderable,
            execution_diagnostics=execution_diagnostics,
            env=env,
            task_room=task_room,
        )

    fps = _env_float("ISBENCH_SCENE_GRAPH_BEV_VIDEO_FPS", 2.0)
    first_canvas, first_metadata = _render_scene_graph_bev(
        renderable[0],
        env=env,
        execution_diagnostics=execution_diagnostics,
        task_room=task_room,
        crop_to_task_scene=crop_to_task_scene,
        task_scene_override=fixed_task_scene,
    )
    if first_canvas is None:
        metadata = dict(first_metadata)
        metadata.update(
            {
                "saved": False,
                "reason": metadata.get("reason") or "first_frame_not_renderable",
                "frame_count": 0,
                "scene_graph_source": "benchmark.tracker.scene_graph_history",
                "simulator_truth_overlay": False,
            }
        )
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return metadata

    first_frame = _video_frame_array(first_canvas)
    height, width = first_frame.shape[:2]
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(video_path),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    written = 0
    frame_summaries: List[Dict[str, Any]] = []
    try:
        for index, snapshot in enumerate(renderable):
            if index == 0:
                canvas = first_canvas
                frame_metadata = first_metadata
            else:
                canvas, frame_metadata = _render_scene_graph_bev(
                    snapshot,
                    env=env,
                    execution_diagnostics=execution_diagnostics,
                    task_room=task_room,
                    crop_to_task_scene=crop_to_task_scene,
                    task_scene_override=fixed_task_scene,
                )
                if canvas is None:
                    continue
            frame = _video_frame_array(canvas)
            if frame.shape[:2] != (height, width):
                frame_image = Image.fromarray(frame, mode="RGB").resize(
                    (width, height),
                    Image.Resampling.BILINEAR,
                )
                frame = np.asarray(frame_image, dtype=np.uint8)
            process.stdin.write(np.ascontiguousarray(frame).tobytes())
            written += 1
            frame_summaries.append(
                {
                    "index": index,
                    "frame_index": (snapshot.get("metadata") or {}).get("frame_index"),
                    "global_step_index": (snapshot.get("metadata") or {}).get("global_step_index"),
                    "drawn_node_count": frame_metadata.get("drawn_node_count"),
                    "relation_edge_count": frame_metadata.get("relation_edge_count"),
                }
            )
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise

    if return_code != 0:
        raise RuntimeError(f"ffmpeg failed to save scene graph BEV video: {stderr.strip()}")

    metadata = {
        "saved": True,
        "video": str(video_path),
        "kind": "task_scene" if crop_to_task_scene else "global",
        "frame_count": written,
        "fps": fps,
        "width": width,
        "height": height,
        "task_scene": fixed_task_scene,
        "scene_graph_source": "benchmark.tracker.scene_graph_history",
        "simulator_truth_overlay": False,
        "frames": frame_summaries,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return metadata


def _render_scene_graph_bev(
    snapshot: Dict[str, Any],
    *,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    task_room: Optional[str] = None,
    crop_to_task_scene: bool = False,
    task_scene_override: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Image.Image], Dict[str, Any]]:
    nodes = _normalise_nodes(snapshot)
    positioned_nodes = [node for node in nodes if _node_xy(node) is not None]
    if not positioned_nodes:
        return None, {
            "saved": False,
            "reason": "no_positioned_scene_graph_nodes",
            "node_count": len(nodes),
        }

    map_ctx = _extract_map_context(env)
    trajectories = _extract_trajectories(execution_diagnostics or [])
    robot_pose = _extract_robot_pose(env)
    if task_scene_override is not None:
        task_scene = task_scene_override
    else:
        task_scene = _task_scene_context(
            nodes=positioned_nodes,
            trajectories=trajectories,
            robot_pose=robot_pose,
            task_room=task_room,
            enabled=crop_to_task_scene,
        )
    points = [xy for node in positioned_nodes for xy in [_node_xy(node)] if xy is not None]
    points.extend(point for path in trajectories for point in path)
    if robot_pose is not None:
        points.append(robot_pose[:2])

    max_side = _env_int("ISBENCH_SCENE_GRAPH_BEV_MAX_SIDE", DEFAULT_BEV_MAX_SIDE)
    style_scale = _style_scale(max_side)
    map_image, project, map_metadata = _build_base_image(
        map_ctx,
        points,
        max_side=max_side,
        crop_bounds=task_scene.get("bounds") if task_scene else None,
    )
    map_draw = ImageDraw.Draw(map_image, "RGBA")
    font = _font(round(12 * style_scale))
    small_font = _font(round(10 * style_scale))

    node_positions = {
        node["object_id"]: _node_xy(node)
        for node in positioned_nodes
        if node.get("object_id") and _node_xy(node) is not None
    }
    pixel_positions: Dict[str, Pixel] = {}
    for object_id, xy in node_positions.items():
        pixel = project(xy)
        if pixel is not None:
            pixel_positions[object_id] = pixel

    _draw_trajectories(map_draw, project, trajectories, style_scale)
    relation_edges = _visible_relation_edges(
        _relation_edges(snapshot, node_positions),
        node_positions,
        project,
    )
    _draw_relation_edges(map_draw, project, relation_edges, node_positions, small_font, style_scale)
    indexed_nodes = _draw_nodes(map_draw, project, positioned_nodes, font, style_scale)
    _draw_robot(map_draw, project, robot_pose, style_scale)

    panel_width = _env_int(
        "ISBENCH_SCENE_GRAPH_BEV_PANEL_WIDTH",
        int(round(520 * style_scale)),
    )
    line_height = max(18, int(round(16 * style_scale)))
    duplicate_names = {
        name: count
        for name, count in Counter(
            str(node.get("name") or "object")
            for node in [item["node"] for item in indexed_nodes]
            if node.get("category") not in {"room", "group"}
        ).items()
        if count > 1
    }
    panel_height = (
        int(round(260 * style_scale))
        + line_height * min(len(indexed_nodes), 55)
        + int(round(14 * style_scale)) * min(len(duplicate_names), 12)
    )
    final_height = max(map_image.height, panel_height)
    canvas = Image.new("RGB", (map_image.width + panel_width, final_height), (248, 249, 250))
    canvas.paste(map_image.convert("RGB"), (0, 0))
    panel_draw = ImageDraw.Draw(canvas)
    _draw_panel(
        panel_draw,
        left=map_image.width,
        width=panel_width,
        height=final_height,
        snapshot=snapshot,
        nodes=nodes,
        indexed_nodes=indexed_nodes,
        duplicate_names=duplicate_names,
        relation_count=len(relation_edges),
        map_metadata=map_metadata,
        task_scene=task_scene,
        style_scale=style_scale,
    )

    metadata = {
        "saved": True,
        "kind": "task_scene" if crop_to_task_scene else "global",
        "node_count": len(nodes),
        "positioned_node_count": len(positioned_nodes),
        "drawn_node_count": len(indexed_nodes),
        "relation_edge_count": len(relation_edges),
        "duplicate_names": duplicate_names,
        "map": map_metadata,
        "task_scene": task_scene,
        "scene_graph_source": "benchmark.tracker.latest_scene_graph",
        "simulator_truth_overlay": False,
        "image_size": list(canvas.size),
        "style_scale": style_scale,
        "nodes": [
            {
                "index": item["index"],
                "object_id": item["node"].get("object_id"),
                "name": item["node"].get("name"),
                "category": item["node"].get("category"),
                "visible": item["node"].get("visible"),
                "position": item["node"].get("position"),
                "pixel": list(item["pixel"]) if item.get("pixel") is not None else None,
            }
            for item in indexed_nodes
        ],
    }
    return canvas, metadata


def _history_snapshots(
    snapshots: Optional[List[Dict[str, Any]]],
    latest_snapshot: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    timeline = [snapshot for snapshot in (snapshots or []) if isinstance(snapshot, dict)]
    if isinstance(latest_snapshot, dict):
        latest_key = _snapshot_key(latest_snapshot)
        if not timeline or _snapshot_key(timeline[-1]) != latest_key:
            timeline.append(latest_snapshot)
    return timeline


def _snapshot_key(snapshot: Dict[str, Any]) -> Tuple[Any, Any, int, int]:
    metadata = snapshot.get("metadata") or {}
    return (
        metadata.get("global_step_index"),
        metadata.get("frame_index"),
        len(snapshot.get("nodes") or []),
        len(snapshot.get("edges") or []),
    )


def _history_task_scene_context(
    snapshots: List[Dict[str, Any]],
    *,
    execution_diagnostics: Optional[List[Dict[str, Any]]],
    env: Any,
    task_room: Optional[str],
) -> Optional[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        nodes.extend(
            node
            for node in _normalise_nodes(snapshot)
            if _node_xy(node) is not None
        )
    trajectories = _extract_trajectories(execution_diagnostics or [])
    robot_pose = _extract_robot_pose(env)
    room_name = _normalise_room_name(task_room)
    margin = _env_float("ISBENCH_SCENE_GRAPH_TASK_SCENE_MARGIN_M", 0.8)
    bounds, source = _task_scene_point_bounds(
        nodes=nodes,
        trajectories=trajectories,
        robot_pose=robot_pose,
        task_room=room_name,
        margin=margin,
    )
    if bounds is None:
        return {
            "enabled": True,
            "room": room_name,
            "source": "none",
            "bounds": None,
            "margin_m": margin,
            "history_stable_crop": True,
        }
    return {
        "enabled": True,
        "room": room_name,
        "source": f"{source}_history",
        "bounds": bounds,
        "margin_m": margin,
        "history_stable_crop": True,
    }


def _video_frame_array(image: Image.Image) -> np.ndarray:
    rgb = image.convert("RGB")
    width, height = rgb.size
    even_width = width + (width % 2)
    even_height = height + (height % 2)
    if (even_width, even_height) != (width, height):
        padded = Image.new("RGB", (even_width, even_height), (248, 249, 250))
        padded.paste(rgb, (0, 0))
        rgb = padded
    return np.asarray(rgb, dtype=np.uint8)


def _normalise_nodes(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = snapshot.get("nodes", [])
    return [node for node in nodes if isinstance(node, dict)]


def _normalise_room_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text.replace(" ", "_")


def _task_scene_context(
    *,
    nodes: List[Dict[str, Any]],
    trajectories: List[List[XY]],
    robot_pose: Optional[Tuple[float, float, Optional[float]]],
    task_room: Optional[str],
    enabled: bool,
) -> Optional[Dict[str, Any]]:
    if not enabled:
        return None

    room_name = _normalise_room_name(task_room)
    margin = _env_float("ISBENCH_SCENE_GRAPH_TASK_SCENE_MARGIN_M", 0.8)
    bounds, source = _task_scene_point_bounds(
        nodes=nodes,
        trajectories=trajectories,
        robot_pose=robot_pose,
        task_room=room_name,
        margin=margin,
    )
    if bounds is None:
        return {
            "enabled": True,
            "room": room_name,
            "source": "none",
            "bounds": None,
            "margin_m": margin,
        }
    return {
        "enabled": True,
        "room": room_name,
        "source": source,
        "bounds": bounds,
        "margin_m": margin,
    }


def _task_scene_point_bounds(
    *,
    nodes: List[Dict[str, Any]],
    trajectories: List[List[XY]],
    robot_pose: Optional[Tuple[float, float, Optional[float]]],
    task_room: Optional[str],
    margin: float,
) -> Tuple[Optional[List[float]], str]:
    points: List[XY] = []
    room_nodes = [node for node in nodes if _node_in_task_room(node, task_room)]
    if room_nodes:
        points.extend(xy for node in room_nodes for xy in [_node_xy(node)] if xy is not None)
        source = "scene_graph_task_room_nodes"
    else:
        points.extend(xy for node in nodes for xy in [_node_xy(node)] if xy is not None)
        source = "scene_graph_nodes"
    points.extend(point for path in trajectories for point in path)
    if robot_pose is not None:
        points.append(robot_pose[:2])
    if not points:
        return None, source
    min_x, max_x, min_y, max_y = _point_bounds(points, padding=margin)
    return [min_x, min_y, max_x, max_y], source


def _node_in_task_room(node: Dict[str, Any], task_room: Optional[str]) -> bool:
    if not task_room:
        return False
    states = node.get("states") or {}
    attrs = states.get("attributes") or {}
    candidates = [
        states.get("room_id"),
        attrs.get("room_id"),
        attrs.get("room"),
        node.get("room_id"),
    ]
    for candidate in candidates:
        candidate_norm = _normalise_room_name(candidate)
        if candidate_norm and (
            candidate_norm == task_room or candidate_norm.startswith(f"{task_room}_")
        ):
            return True
    return False


def _node_xy(node: Dict[str, Any]) -> Optional[XY]:
    position = node.get("position")
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        return None
    try:
        x = float(position[0])
        y = float(position[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return x, y


def _to_numpy(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        return np.asarray(value.numpy())
    return np.asarray(value)


def _extract_map_context(env: Any) -> Optional[Dict[str, Any]]:
    scene = getattr(env, "scene", None)
    trav_map = getattr(scene, "trav_map", None)
    floor_maps = getattr(trav_map, "floor_map", None)
    if trav_map is None or floor_maps is None:
        return None

    floor = _current_floor(env, trav_map)
    try:
        floor_map = floor_maps[floor]
    except Exception:
        floor = 0
        floor_map = floor_maps[0]
    floor_np = _to_numpy(floor_map)
    if floor_np.ndim != 2 or floor_np.size == 0:
        return None
    return {
        "floor": floor,
        "floor_map": floor_np,
        "trav_map": trav_map,
        "resolution": getattr(trav_map, "map_resolution", None),
    }


def _current_floor(env: Any, trav_map: Any) -> int:
    floor_heights = getattr(trav_map, "floor_heights", None)
    if not floor_heights:
        return 0
    robots = getattr(env, "robots", None) or []
    if not robots:
        return 0
    try:
        position = _to_numpy(robots[0].get_position_orientation()[0]).reshape(-1)
        z = float(position[2])
    except Exception:
        return 0
    diffs = [abs(z - float(height)) for height in floor_heights]
    return int(min(range(len(diffs)), key=diffs.__getitem__))


def _build_base_image(
    map_ctx: Optional[Dict[str, Any]],
    points: List[XY],
    *,
    max_side: int,
    crop_bounds: Optional[List[float]] = None,
) -> Tuple[Image.Image, Callable[[XY], Optional[Pixel]], Dict[str, Any]]:
    if map_ctx is not None:
        floor_np = np.asarray(map_ctx["floor_map"])
        row_min, col_min = 0, 0
        crop_metadata = None
        if crop_bounds is not None:
            crop = _crop_pixels_from_world_bounds(
                map_ctx["trav_map"],
                crop_bounds,
                floor_np.shape,
            )
            if crop is not None:
                row_min, row_max, col_min, col_max = crop
                floor_np = floor_np[row_min:row_max, col_min:col_max]
                crop_metadata = {
                    "bounds": crop_bounds,
                    "pixels": [row_min, row_max, col_min, col_max],
                }
        image_np = np.zeros((*floor_np.shape, 3), dtype=np.uint8)
        free = floor_np > 0
        image_np[:, :] = np.asarray([42, 45, 50], dtype=np.uint8)
        image_np[free] = np.asarray([230, 235, 229], dtype=np.uint8)
        base = Image.fromarray(image_np)
        max_scale = _env_float("ISBENCH_SCENE_GRAPH_BEV_MAX_SCALE", 32.0)
        scale = max(min(max_side / max(base.width, base.height), max_scale), 0.1)
        resized = base.resize(
            (max(1, int(round(base.width * scale))), max(1, int(round(base.height * scale)))),
            Image.Resampling.NEAREST,
        ).convert("RGBA")

        def project(xy: XY) -> Optional[Pixel]:
            map_xy = _world_to_map(map_ctx["trav_map"], xy)
            if map_xy is None:
                return None
            row, col = map_xy
            row = float(row) - row_min
            col = float(col) - col_min
            if row < 0 or col < 0 or row >= floor_np.shape[0] or col >= floor_np.shape[1]:
                return None
            return col * scale, row * scale

        metadata = {
            "mode": "traversable_map",
            "floor": map_ctx.get("floor"),
            "shape": list(floor_np.shape),
            "scale": scale,
            "resolution": map_ctx.get("resolution"),
            "target_max_side": max_side,
        }
        if crop_metadata is not None:
            metadata["crop"] = crop_metadata
        return resized, project, metadata

    if crop_bounds is not None:
        min_x, min_y, max_x, max_y = crop_bounds
    else:
        min_x, max_x, min_y, max_y = _point_bounds(points)
    width = max_side
    height = int(round(max_side * 0.75))
    margin = max(120, int(round(max_side * 0.045)))
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    base = Image.new("RGBA", (width, height), (236, 239, 242, 255))
    draw = ImageDraw.Draw(base, "RGBA")
    for i in range(0, width, 50):
        draw.line((i, 0, i, height), fill=(205, 211, 218, 120), width=1)
    for i in range(0, height, 50):
        draw.line((0, i, width, i), fill=(205, 211, 218, 120), width=1)

    def project(xy: XY) -> Optional[Pixel]:
        x, y = xy
        if x < min_x or x > max_x or y < min_y or y > max_y:
            return None
        return margin + (x - min_x) * scale, height - margin - (y - min_y) * scale

    return base, project, {
        "mode": "world_extent_fallback",
        "bounds": [min_x, min_y, max_x, max_y],
        "scale": scale,
        "target_max_side": max_side,
    }


def _world_to_map(trav_map: Any, xy: XY) -> Optional[Tuple[float, float]]:
    value = np.asarray(xy, dtype=np.float32)
    for candidate in (value, value.tolist()):
        try:
            map_xy = trav_map.world_to_map(candidate)
            arr = _to_numpy(map_xy).reshape(-1)
            if arr.size >= 2 and np.all(np.isfinite(arr[:2])):
                return float(arr[0]), float(arr[1])
        except Exception:
            pass
    try:
        import torch

        map_xy = trav_map.world_to_map(torch.as_tensor(value, dtype=torch.float32))
        arr = _to_numpy(map_xy).reshape(-1)
        if arr.size >= 2 and np.all(np.isfinite(arr[:2])):
            return float(arr[0]), float(arr[1])
    except Exception:
        return None
    return None


def _crop_pixels_from_world_bounds(
    trav_map: Any,
    bounds: List[float],
    shape: Tuple[int, int],
) -> Optional[Tuple[int, int, int, int]]:
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
    pad = _env_int("ISBENCH_SCENE_GRAPH_TASK_SCENE_PIXEL_PADDING", 8)
    height, width = shape
    row_min = max(0, int(math.floor(min(rows))) - pad)
    row_max = min(height, int(math.ceil(max(rows))) + pad + 1)
    col_min = max(0, int(math.floor(min(cols))) - pad)
    col_max = min(width, int(math.ceil(max(cols))) + pad + 1)
    if row_max <= row_min or col_max <= col_min:
        return None
    return row_min, row_max, col_min, col_max


def _point_bounds(points: List[XY], padding: float = 0.75) -> Tuple[float, float, float, float]:
    if not points:
        return -1.0, 1.0, -1.0, 1.0
    xs = [point[0] for point in points if math.isfinite(point[0])]
    ys = [point[1] for point in points if math.isfinite(point[1])]
    if not xs or not ys:
        return -1.0, 1.0, -1.0, 1.0
    return min(xs) - padding, max(xs) + padding, min(ys) - padding, max(ys) + padding


def _extract_robot_pose(env: Any) -> Optional[Tuple[float, float, Optional[float]]]:
    robots = getattr(env, "robots", None) or []
    if not robots:
        return None
    try:
        position, orientation = robots[0].get_position_orientation()
        pos = _to_numpy(position).reshape(-1)
        orn = _to_numpy(orientation).reshape(-1)
    except Exception:
        return None
    if pos.size < 2:
        return None
    yaw = None
    if orn.size >= 4:
        x, y, z, w = [float(value) for value in orn[:4]]
        yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return float(pos[0]), float(pos[1]), yaw


def _extract_trajectories(diagnostics: Iterable[Dict[str, Any]]) -> List[List[XY]]:
    paths: List[List[XY]] = []
    state_path: List[XY] = []
    for item in diagnostics:
        for state_key in ("start_state", "end_state"):
            xy = _xy_from_value((item.get(state_key) or {}).get("base_position"))
            if xy is not None:
                state_path.append(xy)
        nav = item.get("navigation")
        if isinstance(nav, dict):
            for key, value in nav.items():
                if "waypoint" in key or "path" in key:
                    if not isinstance(value, (list, tuple)):
                        continue
                    path = [_xy_from_value(point) for point in value or []]
                    path = [point for point in path if point is not None]
                    if len(path) >= 2:
                        paths.append(path)
    if len(state_path) >= 2:
        paths.insert(0, state_path)
    return paths


def _xy_from_value(value: Any) -> Optional[XY]:
    if not isinstance(value, (list, tuple, np.ndarray)) or len(value) < 2:
        return None
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return x, y


def _relation_edges(
    snapshot: Dict[str, Any],
    node_positions: Dict[str, XY],
) -> List[Dict[str, Any]]:
    edges = []
    for edge in snapshot.get("edges", []):
        if not isinstance(edge, dict):
            continue
        relation = str(edge.get("relation") or "")
        if relation in {"in_room", "in_group"}:
            continue
        source_id = str(edge.get("source_id") or "")
        target_id = str(edge.get("target_id") or "")
        if source_id in node_positions and target_id in node_positions and source_id != target_id:
            edges.append(edge)
    return edges


def _visible_relation_edges(
    edges: List[Dict[str, Any]],
    node_positions: Dict[str, XY],
    project: Callable[[XY], Optional[Pixel]],
) -> List[Dict[str, Any]]:
    visible = []
    for edge in edges:
        source_xy = node_positions.get(str(edge.get("source_id") or ""))
        target_xy = node_positions.get(str(edge.get("target_id") or ""))
        if source_xy is None or target_xy is None:
            continue
        if project(source_xy) is None or project(target_xy) is None:
            continue
        visible.append(edge)
    return visible


def _draw_trajectories(
    draw: ImageDraw.ImageDraw,
    project: Callable[[XY], Optional[Pixel]],
    trajectories: List[List[XY]],
    style_scale: float,
) -> None:
    dot_radius = max(2, int(round(2 * style_scale)))
    for index, path in enumerate(trajectories):
        pixels = [project(point) for point in path]
        pixels = [pixel for pixel in pixels if pixel is not None]
        if len(pixels) < 2:
            continue
        color = (38, 120, 84, 210) if index == 0 else (76, 154, 255, 130)
        draw.line(
            pixels,
            fill=color,
            width=max(2, int(round((3 if index == 0 else 2) * style_scale))),
        )
        for pixel in pixels[:: max(1, len(pixels) // 12)]:
            draw.ellipse(
                (
                    pixel[0] - dot_radius,
                    pixel[1] - dot_radius,
                    pixel[0] + dot_radius,
                    pixel[1] + dot_radius,
                ),
                fill=color,
            )


def _draw_relation_edges(
    draw: ImageDraw.ImageDraw,
    project: Callable[[XY], Optional[Pixel]],
    edges: List[Dict[str, Any]],
    node_positions: Dict[str, XY],
    font: ImageFont.ImageFont,
    style_scale: float,
) -> None:
    for edge in edges:
        source_xy = node_positions.get(str(edge.get("source_id") or ""))
        target_xy = node_positions.get(str(edge.get("target_id") or ""))
        if source_xy is None or target_xy is None:
            continue
        source_px = project(source_xy)
        target_px = project(target_xy)
        if source_px is None or target_px is None:
            continue
        draw.line(
            [source_px, target_px],
            fill=(225, 116, 38, 180),
            width=max(2, int(round(2 * style_scale))),
        )
        mid = ((source_px[0] + target_px[0]) / 2.0, (source_px[1] + target_px[1]) / 2.0)
        offset = max(3, int(round(3 * style_scale)))
        draw.text(
            (mid[0] + offset, mid[1] + offset),
            _shorten(str(edge.get("relation")), 18),
            fill=(150, 70, 20, 230),
            font=font,
        )


def _draw_nodes(
    draw: ImageDraw.ImageDraw,
    project: Callable[[XY], Optional[Pixel]],
    nodes: List[Dict[str, Any]],
    font: ImageFont.ImageFont,
    style_scale: float,
) -> List[Dict[str, Any]]:
    indexed = []
    index = 1
    for node in sorted(nodes, key=lambda item: (str(item.get("category")), str(item.get("name")), str(item.get("object_id")))):
        xy = _node_xy(node)
        if xy is None:
            continue
        pixel = project(xy)
        if pixel is None:
            continue
        color = _node_color(node)
        category = node.get("category")
        visible = bool(node.get("visible", True))
        radius = max(8, int(round((8 if category != "group" else 11) * style_scale)))
        outline = (20, 24, 28, 255) if visible else (120, 124, 128, 255)
        fill_alpha = 230 if visible else 115
        x, y = pixel
        if category == "group":
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                outline=(0, 116, 130, 210),
                width=max(2, int(round(3 * style_scale))),
                fill=(0, 116, 130, 40),
            )
        else:
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=(*color, fill_alpha),
                outline=outline,
                width=max(2, int(round(2 * style_scale))),
            )
        label = str(index)
        label_box = draw.textbbox((0, 0), label, font=font)
        label_w = label_box[2] - label_box[0]
        label_h = label_box[3] - label_box[1]
        pad = max(3, int(round(3 * style_scale)))
        label_dx = max(2, int(round(2 * style_scale)))
        draw.rectangle(
            (
                x + radius - label_dx,
                y - radius - label_dx,
                x + radius + label_w + pad * 2,
                y - radius + label_h + pad,
            ),
            fill=(255, 255, 255, 220),
            outline=(20, 24, 28, 180),
        )
        draw.text(
            (x + radius + pad, y - radius),
            label,
            fill=(20, 24, 28, 255),
            font=font,
        )
        indexed.append({"index": index, "node": node, "pixel": pixel})
        index += 1
    return indexed


def _draw_robot(
    draw: ImageDraw.ImageDraw,
    project: Callable[[XY], Optional[Pixel]],
    robot_pose: Optional[Tuple[float, float, Optional[float]]],
    style_scale: float,
) -> None:
    if robot_pose is None:
        return
    pixel = project(robot_pose[:2])
    if pixel is None:
        return
    x, y = pixel
    yaw = robot_pose[2]
    if yaw is None:
        half = max(8, int(round(8 * style_scale)))
        draw.rectangle((x - half, y - half, x + half, y + half), fill=(30, 88, 210, 230), outline=(255, 255, 255, 255))
        return
    forward_px = project(
        (
            robot_pose[0] + 0.35 * math.cos(yaw),
            robot_pose[1] + 0.35 * math.sin(yaw),
        )
    )
    if forward_px is None:
        half = max(8, int(round(8 * style_scale)))
        draw.rectangle((x - half, y - half, x + half, y + half), fill=(30, 88, 210, 230), outline=(255, 255, 255, 255))
        return
    heading = np.asarray([forward_px[0] - x, forward_px[1] - y], dtype=float)
    norm = float(np.linalg.norm(heading))
    if norm <= 1e-6:
        half = max(8, int(round(8 * style_scale)))
        draw.rectangle((x - half, y - half, x + half, y + half), fill=(30, 88, 210, 230), outline=(255, 255, 255, 255))
        return
    heading = heading / norm
    left = np.asarray([-heading[1], heading[0]], dtype=float)
    center = np.asarray([x, y], dtype=float)
    front = 14.0 * style_scale
    rear = 9.0 * style_scale
    side = 8.0 * style_scale
    points = [
        center + heading * front,
        center - heading * rear + left * side,
        center - heading * rear - left * side,
    ]
    draw.polygon([tuple(point) for point in points], fill=(30, 88, 210, 235), outline=(255, 255, 255, 255))


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    *,
    left: int,
    width: int,
    height: int,
    snapshot: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    indexed_nodes: List[Dict[str, Any]],
    duplicate_names: Dict[str, int],
    relation_count: int,
    map_metadata: Dict[str, Any],
    task_scene: Optional[Dict[str, Any]],
    style_scale: float,
) -> None:
    draw.rectangle((left, 0, left + width, height), fill=(248, 249, 250), outline=(210, 214, 220))
    margin = max(18, int(round(18 * style_scale)))
    x = left + margin
    y = margin
    title_font = _font(round(16 * style_scale))
    font = _font(round(12 * style_scale))
    small_font = _font(round(10 * style_scale))
    title_line = max(28, int(round(26 * style_scale)))
    body_line = max(18, int(round(17 * style_scale)))
    small_line = max(15, int(round(15 * style_scale)))
    dup_line = max(14, int(round(14 * style_scale)))
    max_chars = max(42, int(width / max(8.5 * style_scale, 1.0)))
    metadata = snapshot.get("metadata", {})
    backend = metadata.get("perception_backend")
    frame_index = metadata.get("frame_index")
    object_count = sum(1 for node in nodes if node.get("category") not in {"room", "group"})
    group_count = sum(1 for node in nodes if node.get("category") == "group")
    room_count = sum(1 for node in nodes if node.get("category") == "room")

    draw.text((x, y), "Scene Graph BEV", fill=(24, 28, 33), font=title_font)
    y += title_line
    for line in [
        f"backend: {backend}",
        f"frame: {frame_index}   objects: {object_count}   rooms: {room_count}   groups: {group_count}",
        f"relations on map: {relation_count}",
        f"map: {map_metadata.get('mode')} floor={map_metadata.get('floor')} shape={map_metadata.get('shape')}",
    ]:
        draw.text((x, y), _shorten(line, max_chars), fill=(52, 58, 66), font=font)
        y += body_line
    if task_scene is not None:
        line = (
            f"task scene: room={task_scene.get('room')} "
            f"source={task_scene.get('source')}"
        )
        draw.text((x, y), _shorten(line, max_chars), fill=(52, 58, 66), font=font)
        y += body_line

    y += max(8, int(round(8 * style_scale)))
    legend_y = y + max(7, int(round(6 * style_scale)))
    _legend_marker(draw, x, legend_y, (38, 120, 84), "robot path", font, style_scale=style_scale)
    relation_x = x + int(round(130 * style_scale))
    _legend_marker(draw, relation_x, legend_y, (225, 116, 38), "relation", font, line=True, style_scale=style_scale)
    y += max(28, int(round(28 * style_scale)))

    if duplicate_names:
        draw.text((x, y), "Duplicate names", fill=(24, 28, 33), font=font)
        y += body_line
        for name, count in sorted(duplicate_names.items(), key=lambda item: (-item[1], item[0]))[:12]:
            draw.text(
                (x + int(round(8 * style_scale)), y),
                _shorten(f"{count} x {name}", max_chars),
                fill=(132, 76, 24),
                font=small_font,
            )
            y += dup_line
        y += max(6, int(round(6 * style_scale)))

    draw.text((x, y), "Node Index", fill=(24, 28, 33), font=font)
    y += body_line
    marker_radius = max(5, int(round(5 * style_scale)))
    marker_gap = max(18, int(round(18 * style_scale)))
    for item in indexed_nodes[:55]:
        node = item["node"]
        color = _node_color(node)
        row_y = y + marker_radius + 1
        draw.ellipse(
            (x, row_y - marker_radius, x + marker_radius * 2, row_y + marker_radius),
            fill=color,
            outline=(20, 24, 28),
        )
        visible = "vis" if node.get("visible", True) else "hid"
        object_id = str(node.get("object_id") or "")
        line = f"{item['index']:02d} {visible} {_short_id(object_id)} {node.get('name')}"
        draw.text(
            (x + marker_gap, y),
            _shorten(line, max_chars),
            fill=(52, 58, 66),
            font=small_font,
        )
        y += small_line
    if len(indexed_nodes) > 55:
        draw.text(
            (x + marker_gap, y),
            f"... {len(indexed_nodes) - 55} more nodes",
            fill=(92, 98, 106),
            font=small_font,
        )


def _legend_marker(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    color: Tuple[int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    *,
    diamond: bool = False,
    line: bool = False,
    style_scale: float = 1.0,
) -> None:
    marker = max(7, int(round(7 * style_scale)))
    if line:
        draw.line((x, y, x + int(round(18 * style_scale)), y), fill=color, width=max(3, int(round(3 * style_scale))))
    elif diamond:
        draw.polygon([(x + marker, y - marker), (x + marker * 2, y), (x + marker, y + marker), (x, y)], fill=color)
    else:
        draw.ellipse((x, y - marker, x + marker * 2, y + marker), fill=color)
    draw.text(
        (x + int(round(20 * style_scale)), y - marker),
        text,
        fill=(52, 58, 66),
        font=font,
    )


def _node_color(node: Dict[str, Any]) -> Tuple[int, int, int]:
    if node.get("category") == "group":
        return 0, 116, 130
    name = str(node.get("name") or node.get("category") or node.get("object_id") or "object")
    digest = hashlib.md5(name.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    sat = 0.55 + digest[1] / 255.0 * 0.25
    val = 0.78 + digest[2] / 255.0 * 0.15
    return _hsv_to_rgb(hue, sat, val)


def _hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int, int, int]:
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i = i % 6
    values = {
        0: (v, t, p),
        1: (q, v, p),
        2: (p, v, t),
        3: (p, q, v),
        4: (t, p, v),
        5: (v, p, q),
    }[i]
    return tuple(int(round(channel * 255)) for channel in values)


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _shorten(value: Any, max_chars: int) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "..."


def _short_id(object_id: str) -> str:
    if ":" in object_id:
        prefix, suffix = object_id.rsplit(":", 1)
        return f"{prefix[:3]}:{suffix}"
    return _shorten(object_id, 10)
