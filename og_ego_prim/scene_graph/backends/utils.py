from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.utils.serialization import to_debug_builtin as to_builtin

import numpy as np


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def model_root(scene_graph_config: Optional[SceneGraphConfig] = None) -> Path:
    if scene_graph_config is not None and scene_graph_config.model_dir:
        return Path(scene_graph_config.model_dir)
    return repo_root() / "data" / "models"


def ensure_path_exists(path: Path, description: str):
    if not path.exists():
        raise FileNotFoundError(f"{description} not found: {path}")


def insert_sys_path(paths: Iterable[Path]):
    import sys

    for path in reversed([str(path) for path in paths]):
        if path not in sys.path:
            sys.path.insert(0, path)


def bbox_from_mask(mask: np.ndarray) -> Optional[List[float]]:
    rows, cols = np.where(mask)
    if len(rows) == 0 or len(cols) == 0:
        return None
    return [
        float(cols.min()),
        float(rows.min()),
        float(cols.max()),
        float(rows.max()),
    ]


def mask_center_world(
    mask: Optional[np.ndarray],
    depth: Optional[np.ndarray],
    intrinsics: Optional[np.ndarray],
    camera_pose: Optional[np.ndarray],
) -> Optional[List[float]]:
    if mask is None or depth is None or intrinsics is None or camera_pose is None:
        return None

    mask = np.asarray(mask).astype(bool)
    depth = np.asarray(depth, dtype=np.float32)
    if depth.ndim == 3 and depth.shape[-1] == 1:
        depth = depth[:, :, 0]
    if mask.shape != depth.shape[:2]:
        return None

    ys, xs = np.where(mask & np.isfinite(depth) & (depth > 0))
    if len(xs) == 0:
        return None

    if len(xs) > 2048:
        indices = np.linspace(0, len(xs) - 1, 2048).astype(int)
        xs = xs[indices]
        ys = ys[indices]

    z = depth[ys, xs]
    fx = float(intrinsics[0, 0])
    fy = float(intrinsics[1, 1])
    cx = float(intrinsics[0, 2])
    cy = float(intrinsics[1, 2])
    x = (xs.astype(np.float32) - cx) * z / fx
    y = (ys.astype(np.float32) - cy) * z / fy
    points = np.stack([x, y, z, np.ones_like(z)], axis=0)
    world_points = np.asarray(camera_pose, dtype=np.float32) @ points
    center = np.median(world_points[:3], axis=1)
    return [float(v) for v in center]


def room_lookup_from_env(env):
    scene = getattr(env, "scene", None)
    seg_map = getattr(scene, "seg_map", None) or getattr(scene, "_seg_map", None)
    if seg_map is None:
        return lambda point: "unknown_room"

    def lookup(point):
        if point is None:
            return "unknown_room"
        xy = np.asarray(point, dtype=np.float32).reshape(-1)[:2]
        try:
            room_instance = seg_map.get_room_instance_by_point(xy)
        except Exception:
            room_instance = None
        if room_instance:
            return str(room_instance)
        try:
            room_type = seg_map.get_room_type_by_point(xy)
        except Exception:
            room_type = None
        return str(room_type) if room_type else "unknown_room"

    return lookup


def graph_to_room_graph(graph: Any) -> Dict[str, Any]:
    rooms = []
    for room_node in getattr(graph, "room_nodes", []):
        object_ids = [
            getattr(node, "caption", "object")
            for node in sorted(room_node.nodes, key=lambda item: getattr(item, "caption", ""))
        ]
        rooms.append(
            {
                "id": room_node.caption,
                "caption": room_node.caption,
                "object_count": len(room_node.nodes),
                "objects": object_ids,
                "group_count": len(room_node.group_nodes),
            }
        )
    return {"rooms": rooms}


def graph_to_group_graph(graph: Any) -> Dict[str, Any]:
    groups = []
    for room_node in getattr(graph, "room_nodes", []):
        for group_index, group_node in enumerate(room_node.group_nodes):
            groups.append(
                {
                    "id": f"{room_node.caption}:{group_index}",
                    "room": room_node.caption,
                    "caption": group_node.caption,
                    "center": to_builtin(group_node.center),
                    "center_object": (
                        getattr(group_node.center_node, "caption", None)
                        if group_node.center_node is not None
                        else None
                    ),
                    "objects": [getattr(node, "caption", "object") for node in group_node.nodes],
                    "edge_count": len(group_node.edges),
                    "corr_score": float(getattr(group_node, "corr_score", 0.0)),
                }
            )
    return {"groups": groups}
