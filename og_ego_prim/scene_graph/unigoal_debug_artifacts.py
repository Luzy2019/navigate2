import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image
from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.utils.serialization import to_debug_builtin as to_builtin

def output_dir_from_config(scene_graph_config: Optional[SceneGraphConfig] = None) -> Optional[Path]:
    output_dir = scene_graph_config.output_dir if scene_graph_config is not None else None
    return Path(output_dir) if output_dir else None


def write_frame_debug_artifacts(
    output_dir: Optional[Path],
    frame: Any,
    mapping_debug: Optional[Dict[str, Any]],
    segment: Optional[Dict[str, Any]] = None,
) -> None:
    if output_dir is None:
        return
    output_dir = Path(output_dir)
    scene_graph_dir = output_dir / "scene_graph_output"
    frame_dir = output_dir / "frame_observations"
    vis_dir = output_dir / "vis_output"
    for directory in (scene_graph_dir, frame_dir, vis_dir):
        directory.mkdir(parents=True, exist_ok=True)

    frame_index = int(getattr(frame, "frame_index", 0))
    mapping_debug = mapping_debug or {}
    rgb = _rgb_array(getattr(frame, "rgb", None))
    rgb_missing = rgb is None
    if rgb is None:
        rgb = _blank_rgb(mapping_debug)
    rgb_jpg = frame_dir / f"frame_{frame_index}_rgb.jpg"
    _imwrite_rgb(rgb_jpg, rgb)

    masks = _segment_masks(segment)

    _save_raw_bbox_image(
        rgb,
        mapping_debug,
        vis_dir / f"frame_{frame_index}_vlm_bbox.jpg",
    )
    _save_masks_image(
        masks,
        rgb.shape,
        vis_dir / f"frame_{frame_index}_full_mask.jpg",
    )

    _cleanup_frame_observation_intermediates(frame_dir, frame_index)
    _cleanup_stale_visual_artifacts(vis_dir, frame_index)

    payload = {
        "frame_index": frame_index,
        "backend": "unigoal_grounded_sam",
        "frame_pose": _frame_pose_payload(frame),
        "mapping_debug": mapping_debug,
        "frame_files": {
            "rgb_jpg": str(rgb_jpg),
            "visuals": {
                "raw_full_mask_jpg": str(vis_dir / f"frame_{frame_index}_full_mask.jpg"),
                "raw_bbox_rels_jpg": str(vis_dir / f"frame_{frame_index}_vlm_bbox.jpg"),
                "matched_mask_jpg": str(vis_dir / f"frame_{frame_index}_matched_mask.jpg"),
                "matched_objs_rels_jpg": str(vis_dir / f"frame_{frame_index}_matched_objs_rels.jpg"),
            },
        },
        "artifact_status": {
            "rgb_missing": rgb_missing,
            "mask_count": 0 if masks is None else int(len(masks)),
        },
    }
    (scene_graph_dir / f"frame_{frame_index}_debug.json").write_text(
        json.dumps(to_builtin(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_result_artifacts(
    output_dir: Optional[Path],
    frame: Any,
    result: Any,
) -> None:
    if output_dir is None:
        return
    output_dir = Path(output_dir)
    scene_graph_dir = output_dir / "scene_graph_output"
    scene_graph_dir.mkdir(parents=True, exist_ok=True)
    frame_index = int(getattr(frame, "frame_index", getattr(result, "frame_index", 0)))

    objects = list(getattr(result, "objects", []) or [])
    relations = list(getattr(result, "relations", []) or [])
    object_map = {obj.object_id: obj for obj in objects}
    objs_json = [_object_json(obj) for obj in objects]
    rels_json = {
        f"{_native_object_id(rel.source_id, object_map)},{_native_object_id(rel.target_id, object_map)}": rel.relation
        for rel in relations
    }
    (scene_graph_dir / f"{frame_index}_objs.json").write_text(
        json.dumps(to_builtin(objs_json), ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    (scene_graph_dir / f"{frame_index}_rels.json").write_text(
        json.dumps(to_builtin(rels_json), ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    debug_path = scene_graph_dir / f"frame_{frame_index}_debug.json"
    debug_payload: Dict[str, Any] = {}
    if debug_path.exists():
        try:
            debug_payload = json.loads(debug_path.read_text(encoding="utf-8"))
        except Exception:
            debug_payload = {}
    debug_payload.update(
        {
            "frame_index": frame_index,
            "backend": debug_payload.get("backend") or getattr(result, "backend", None),
            "frame_pose": _frame_pose_payload(frame),
        }
    )
    debug_path.write_text(
        json.dumps(to_builtin(debug_payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rgb = _rgb_array(getattr(frame, "rgb", None))
    if rgb is not None:
        vis_dir = output_dir / "vis_output"
        vis_dir.mkdir(parents=True, exist_ok=True)
        _save_result_masks_image(
            (rgb.shape[1], rgb.shape[0]),
            objects,
            vis_dir / f"frame_{frame_index}_matched_mask.jpg",
        )
        _save_objects_relations_image(
            rgb,
            objects,
            relations,
            vis_dir / f"frame_{frame_index}_matched_objs_rels.jpg",
        )
        _cleanup_stale_visual_artifacts(vis_dir, frame_index)


def render_unigoal_debug_artifacts(run_dir: Path) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    debug_files = sorted(run_dir.rglob("scene_graph_output/frame_*_debug.json"))
    rendered = 0
    skipped_visuals_missing_masks = 0
    errors: List[Dict[str, Any]] = []
    for debug_path in debug_files:
        output_dir = debug_path.parent.parent
        try:
            payload = json.loads(debug_path.read_text(encoding="utf-8"))
            frame_index = int(payload.get("frame_index", _frame_index_from_path(debug_path)))
            mapping_debug = payload.get("mapping_debug") or payload
            frame_dir = output_dir / "frame_observations"
            rgb = _load_rgb(frame_dir / f"frame_{frame_index}_rgb.jpg", mapping_debug)
            masks = _load_masks(frame_dir / f"frame_{frame_index}_masks.npz")
            frame_dir.mkdir(parents=True, exist_ok=True)
            _imwrite_rgb(frame_dir / f"frame_{frame_index}_rgb.jpg", rgb)
            vis_dir = output_dir / "vis_output"
            vis_dir.mkdir(parents=True, exist_ok=True)
            if masks is None:
                skipped_visuals_missing_masks += 1
            else:
                _render_visual_artifacts(
                    rgb,
                    mapping_debug,
                    masks,
                    vis_dir,
                    frame_index,
                    overwrite=True,
                )
            _cleanup_frame_observation_intermediates(frame_dir, frame_index)
            rendered += 1
        except Exception as exc:
            errors.append(
                {
                    "path": str(debug_path),
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                }
            )
    metadata = {
        "saved": True,
        "debug_file_count": len(debug_files),
        "rendered_frames": rendered,
        "skipped_visuals_missing_masks": skipped_visuals_missing_masks,
        "errors": errors,
    }
    (run_dir / "unigoal_debug_artifacts.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def save_scene_graph_diagnostic_videos(
    snapshots: Optional[List[Dict[str, Any]]],
    output_dir: Path,
    *,
    latest_snapshot: Optional[Dict[str, Any]] = None,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    task_room: Optional[str] = None,
    scene_graph_config: Optional[SceneGraphConfig] = None,
) -> Dict[str, Any]:
    output_dir = Path(output_dir)
    global_meta = _save_diagnostic_video(
        snapshots,
        output_dir,
        latest_snapshot=latest_snapshot,
        env=env,
        execution_diagnostics=execution_diagnostics,
        filename="scene_graph_bev_diagnostic_history.mp4",
        metadata_filename="scene_graph_bev_diagnostic_history.json",
        crop_to_task_scene=False,
        task_room=None,
        scene_graph_config=scene_graph_config,
    )
    task_meta = _save_diagnostic_video(
        snapshots,
        output_dir,
        latest_snapshot=latest_snapshot,
        env=env,
        execution_diagnostics=execution_diagnostics,
        filename="scene_graph_bev_task_scene_diagnostic_history.mp4",
        metadata_filename="scene_graph_bev_task_scene_diagnostic_history.json",
        crop_to_task_scene=True,
        task_room=task_room,
        scene_graph_config=scene_graph_config,
    )
    return {"global": global_meta, "task_scene": task_meta}


def save_unigoal_diagnostic_videos(
    snapshots: Optional[List[Dict[str, Any]]],
    output_dir: Path,
    *,
    latest_snapshot: Optional[Dict[str, Any]] = None,
    env: Any = None,
    execution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    task_room: Optional[str] = None,
    scene_graph_config: Optional[SceneGraphConfig] = None,
) -> Dict[str, Any]:
    """Backward-compatible name for the backend-independent diagnostic videos."""
    return save_scene_graph_diagnostic_videos(
        snapshots,
        output_dir,
        latest_snapshot=latest_snapshot,
        env=env,
        execution_diagnostics=execution_diagnostics,
        task_room=task_room,
        scene_graph_config=scene_graph_config,
    )


def _render_visual_artifacts(
    rgb: np.ndarray,
    mapping_debug: Dict[str, Any],
    masks: Optional[np.ndarray],
    vis_dir: Path,
    frame_index: int,
    *,
    overwrite: bool,
) -> None:
    paths = {
        "vlm_bbox": vis_dir / f"frame_{frame_index}_vlm_bbox.jpg",
        "full_mask": vis_dir / f"frame_{frame_index}_full_mask.jpg",
    }
    if not overwrite and all(path.exists() for path in paths.values()):
        return
    if overwrite or not paths["vlm_bbox"].exists():
        _save_raw_bbox_image(rgb, mapping_debug, paths["vlm_bbox"])
    if overwrite or not paths["full_mask"].exists():
        _save_masks_image(masks, rgb.shape, paths["full_mask"])
    _cleanup_stale_visual_artifacts(vis_dir, frame_index)


def _cleanup_frame_observation_intermediates(frame_dir: Path, frame_index: int) -> None:
    for stale in (
        frame_dir / f"frame_{frame_index}_rgb.npy",
        frame_dir / f"frame_{frame_index}_masks.npz",
        frame_dir / f"frame_{frame_index}_masks.jpg",
    ):
        if stale.exists():
            stale.unlink()


def _cleanup_stale_visual_artifacts(vis_dir: Path, frame_index: int) -> None:
    for stale in (
        vis_dir / f"frame_{frame_index}_grounded_sam.jpg",
        vis_dir / f"frame_{frame_index}_mapping_matches.jpg",
        vis_dir / f"frame_{frame_index}_full_masks.jpg",
        vis_dir / f"frame_{frame_index}_matched_masks.jpg",
    ):
        if stale.exists():
            stale.unlink()


def _object_json(obj: Any) -> Dict[str, Any]:
    source_ids = dict(getattr(obj, "attributes", {}).get("source_ids") or {})
    return {
        "id": source_ids.get("unigoal_object", getattr(obj, "object_id", None)),
        "object_id": getattr(obj, "object_id", None),
        "uid": getattr(obj, "attributes", {}).get("uid"),
        "name": getattr(obj, "name", None),
        "position": getattr(obj, "position", None),
        "room_id": getattr(obj, "room_id", None),
        "confidence": getattr(obj, "confidence", None),
        "visible": bool(getattr(obj, "attributes", {}).get("currently_visible", True)),
        "bbox_3d": getattr(obj, "attributes", {}).get("bbox_3d"),
        "source_ids": source_ids,
    }


def _native_object_id(object_id: str, object_map: Dict[str, Any]) -> Any:
    obj = object_map.get(object_id)
    if obj is None:
        return object_id
    return getattr(obj, "attributes", {}).get("source_ids", {}).get(
        "unigoal_object",
        object_id,
    )


def _rgb_array(value: Any) -> Optional[np.ndarray]:
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    rgb = np.asarray(value)
    if rgb.ndim != 3 or rgb.shape[-1] < 3:
        return None
    rgb = rgb[:, :, :3]
    if rgb.dtype != np.uint8:
        if rgb.max(initial=0) <= 1.0:
            rgb = rgb * 255.0
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb)


def _segment_masks(segment: Optional[Dict[str, Any]]) -> Optional[np.ndarray]:
    if not isinstance(segment, dict):
        return None
    masks = segment.get("mask")
    if masks is None:
        return None
    if hasattr(masks, "detach"):
        masks = masks.detach().cpu()
    if hasattr(masks, "numpy"):
        masks = masks.numpy()
    try:
        array = np.asarray(masks)
    except Exception:
        return None
    if array.ndim == 2:
        array = array[None, ...]
    if array.ndim != 3:
        return None
    return array.astype(bool)


def _frame_index_from_path(path: Path) -> int:
    name = path.stem
    parts = name.split("_")
    for part in parts:
        if part.isdigit():
            return int(part)
    return 0


def _load_rgb(path: Path, mapping_debug: Dict[str, Any]) -> np.ndarray:
    candidates = [path]
    if path.suffix != ".jpg":
        candidates.insert(0, path.with_suffix(".jpg"))
    if path.suffix != ".npy":
        candidates.append(path.with_suffix(".npy"))
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".npy":
                rgb = _rgb_array(np.load(candidate))
            else:
                rgb = _rgb_array(np.asarray(Image.open(candidate).convert("RGB")))
            if rgb is not None:
                return rgb
        except Exception:
            pass
    return _blank_rgb(mapping_debug)


def _blank_rgb(mapping_debug: Dict[str, Any]) -> np.ndarray:
    shape = mapping_debug.get("rgb_shape") or [512, 512, 3]
    height = int(shape[0]) if len(shape) >= 1 else 512
    width = int(shape[1]) if len(shape) >= 2 else 512
    image = np.zeros((max(1, height), max(1, width), 3), dtype=np.uint8)
    image[:, :] = (242, 244, 247)
    _draw_label(image, "missing raw RGB observation", (12, 28), (24, 28, 33))
    return image


def _load_masks(path: Path) -> Optional[np.ndarray]:
    if not path.exists():
        return None
    try:
        return np.load(path)["masks"].astype(bool)
    except Exception:
        return None


def _save_grounded_sam_image(
    rgb: np.ndarray,
    mapping_debug: Dict[str, Any],
    masks: Optional[np.ndarray],
    save_path: Path,
) -> None:
    canvas = rgb.copy()
    items = mapping_debug.get("grounded_sam", []) or []
    if masks is not None:
        for index, mask in enumerate(masks):
            color = _color(index)
            if mask.shape[:2] != canvas.shape[:2]:
                continue
            overlay = canvas.copy()
            overlay[mask] = color
            canvas = np.where(mask[..., None], (0.58 * canvas + 0.42 * overlay).astype(np.uint8), canvas)
    for index, item in enumerate(items):
        bbox = _xyxy(item.get("xyxy"), canvas.shape)
        if bbox is None:
            continue
        label = (
            f"{item.get('index', index)} {item.get('caption') or item.get('class_name') or 'object'} "
            f"conf={_short_float(item.get('confidence'))} area={item.get('mask_area')}"
        )
        _draw_box(canvas, bbox, label, _color(item.get("index", index)))
    if not items:
        _draw_label(canvas, "no GroundingDINO + SAM detections", (12, 28), (255, 255, 255))
    _imwrite_rgb(save_path, canvas)


def _save_raw_bbox_image(
    rgb: np.ndarray,
    mapping_debug: Dict[str, Any],
    save_path: Path,
) -> None:
    canvas = rgb.copy()
    items = mapping_debug.get("grounded_sam", []) or []
    for index, item in enumerate(items):
        bbox = _xyxy(item.get("xyxy"), canvas.shape)
        if bbox is None:
            continue
        label = f"{_display_name(item.get('caption') or item.get('class_name') or 'object')} ({item.get('index', index)})"
        _draw_box(canvas, bbox, label, _color(item.get("index", index)))
    if not items:
        _draw_label(canvas, "no GroundingDINO boxes", (12, 28), (255, 255, 255))
    _imwrite_rgb(save_path, canvas)


def _save_masks_image(
    masks: Optional[np.ndarray],
    image_shape: Tuple[int, ...],
    save_path: Path,
) -> None:
    height, width = image_shape[:2]
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    if masks is None or len(masks) == 0:
        canvas[:, :] = (242, 244, 247)
        _draw_label(canvas, "no SAM masks", (12, 28), (24, 28, 33))
        _imwrite_rgb(save_path, canvas)
        return
    for index, mask in enumerate(masks):
        if mask.shape[:2] != canvas.shape[:2]:
            continue
        canvas[mask] = np.asarray(_color(index), dtype=np.uint8)
    _imwrite_rgb(save_path, canvas)


def _save_result_masks_image(
    image_size: Tuple[int, int],
    objects: List[Any],
    save_path: Path,
) -> None:
    width, height = image_size
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    visible_objects = [
        obj for obj in objects if _object_visible(obj) and _object_mask(obj) is not None
    ]
    if not visible_objects:
        canvas[:, :] = (242, 244, 247)
        _draw_label(canvas, "no matched masks", (12, 28), (24, 28, 33))
        _imwrite_rgb(save_path, canvas)
        return
    for obj in sorted(visible_objects, key=_object_mask_area, reverse=True):
        mask = _object_mask(obj)
        if mask is None or mask.shape[:2] != canvas.shape[:2]:
            continue
        canvas[mask] = np.asarray(_color(_object_key(obj)), dtype=np.uint8)
    _imwrite_rgb(save_path, canvas)


def _save_objects_relations_image(
    rgb: np.ndarray,
    objects: List[Any],
    relations: List[Any],
    save_path: Path,
) -> None:
    canvas = rgb.copy()
    visible_objects = [obj for obj in objects if _object_visible(obj)]
    object_map = {_object_id(obj): obj for obj in visible_objects}
    for index, obj in enumerate(visible_objects):
        bbox = _object_bbox(obj)
        if bbox is None:
            continue
        label = f"{_display_name(getattr(obj, 'name', None))} ({_object_display_id(obj, index)})"
        _draw_box(canvas, bbox, label, _color(_object_key(obj)))
    drawn_relations = 0
    for rel in relations:
        source = object_map.get(getattr(rel, "source_id", None))
        target = object_map.get(getattr(rel, "target_id", None))
        if source is None or target is None:
            continue
        source_bbox = _object_bbox(source)
        target_bbox = _object_bbox(target)
        if source_bbox is None or target_bbox is None:
            continue
        _draw_relation_line(canvas, source_bbox, target_bbox, str(getattr(rel, "relation", "")))
        drawn_relations += 1
    if not visible_objects:
        _draw_label(canvas, "no matched objects", (12, 28), (255, 255, 255))
    elif drawn_relations == 0:
        _draw_label(canvas, "no matched relations", (12, 28), (255, 255, 255))
    _imwrite_rgb(save_path, canvas)


def _save_mapping_matches_image(
    rgb: np.ndarray,
    mapping_debug: Dict[str, Any],
    save_path: Path,
) -> None:
    canvas = rgb.copy()
    decisions = mapping_debug.get("merge_decisions", []) or []
    for decision in decisions:
        detection = decision.get("detection") or {}
        bbox = _xyxy(detection.get("xyxy"), canvas.shape)
        if bbox is None:
            continue
        label = (
            f"det{detection.get('index')} {detection.get('class_name')} "
            f"{decision.get('action')} obj={decision.get('matched_object_index')} "
            f"score={_short_float(decision.get('matched_score_after_threshold'))}"
        )
        _draw_box(canvas, bbox, label, _color(detection.get("index")))
    if not decisions:
        _draw_label(canvas, "no UniGoal mapping decisions", (12, 28), (255, 255, 255))
    _imwrite_rgb(save_path, canvas)


def _object_id(obj: Any) -> Optional[str]:
    object_id = getattr(obj, "object_id", None)
    return None if object_id is None else str(object_id)


def _object_key(obj: Any) -> str:
    return _object_id(obj) or str(id(obj))


def _object_display_id(obj: Any, fallback: int) -> Any:
    attributes = getattr(obj, "attributes", {}) or {}
    uid = attributes.get("uid")
    if uid is not None:
        return uid
    object_id = _object_id(obj)
    if object_id:
        return object_id.rsplit(":", 1)[-1]
    return fallback


def _object_visible(obj: Any) -> bool:
    attributes = getattr(obj, "attributes", {}) or {}
    return bool(attributes.get("currently_visible", attributes.get("is_vis", True)))


def _object_bbox(obj: Any) -> Optional[List[int]]:
    bbox = getattr(obj, "bbox", None)
    if bbox is None:
        return None
    try:
        values = [float(item) for item in bbox[:4]]
    except Exception:
        return None
    if not np.isfinite(values).all():
        return None
    x1, y1, x2, y2 = values
    if x2 <= x1 or y2 <= y1:
        return None
    return [int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))]


def _object_mask(obj: Any) -> Optional[np.ndarray]:
    mask = getattr(obj, "mask", None)
    if mask is None:
        return None
    try:
        return np.asarray(mask, dtype=bool)
    except Exception:
        return None


def _object_mask_area(obj: Any) -> int:
    attributes = getattr(obj, "attributes", {}) or {}
    value = attributes.get("mask_area")
    if value is not None:
        try:
            return int(value)
        except Exception:
            pass
    mask = _object_mask(obj)
    return 0 if mask is None else int(mask.sum())


def _xyxy(value: Any, image_shape: Tuple[int, ...]) -> Optional[List[int]]:
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    try:
        x1, y1, x2, y2 = [float(item) for item in value[:4]]
    except Exception:
        return None
    if not np.isfinite([x1, y1, x2, y2]).all():
        return None
    height, width = image_shape[:2]
    x1 = int(np.clip(round(x1), 0, max(0, width - 1)))
    x2 = int(np.clip(round(x2), 0, max(0, width - 1)))
    y1 = int(np.clip(round(y1), 0, max(0, height - 1)))
    y2 = int(np.clip(round(y2), 0, max(0, height - 1)))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def _draw_box(image: np.ndarray, bbox: List[int], label: str, color: Tuple[int, int, int]) -> None:
    import cv2

    x1, y1, x2, y2 = bbox
    bgr = (int(color[2]), int(color[1]), int(color[0]))
    cv2.rectangle(image, (x1, y1), (x2, y2), bgr, 2)
    _draw_label(image, label, (x1 + 3, max(16, y1 - 5)), color)


def _draw_relation_line(image: np.ndarray, source_bbox: List[int], target_bbox: List[int], label: str) -> None:
    import cv2

    sx = (float(source_bbox[0]) + float(source_bbox[2])) / 2.0
    sy = (float(source_bbox[1]) + float(source_bbox[3])) / 2.0
    tx = (float(target_bbox[0]) + float(target_bbox[2])) / 2.0
    ty = (float(target_bbox[1]) + float(target_bbox[3])) / 2.0
    if not np.isfinite([sx, sy, tx, ty]).all():
        return
    color = (255, 255, 0)
    cv2.arrowedLine(
        image,
        (int(round(sx)), int(round(sy))),
        (int(round(tx)), int(round(ty))),
        color,
        2,
        tipLength=0.08,
    )
    _draw_label(
        image,
        _display_name(label),
        (int(round((sx + tx) / 2.0)), int(round((sy + ty) / 2.0))),
        color,
    )


def _draw_label(image: np.ndarray, label: str, xy: Tuple[int, int], color: Tuple[int, int, int]) -> None:
    import cv2

    x, y = xy
    bgr = (int(color[2]), int(color[1]), int(color[0]))
    cv2.putText(
        image,
        str(label),
        (int(x), int(y)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        bgr,
        1,
        cv2.LINE_AA,
    )


def _display_name(value: Any) -> str:
    text = str(value or "object").strip()
    if not text:
        text = "object"
    text = "_".join(text.split())
    return text[:32]


def _imwrite_rgb(path: Path, rgb: np.ndarray) -> None:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(path), np.ascontiguousarray(rgb[:, :, ::-1]))
    if not ok:
        raise RuntimeError(f"cv2.imwrite returned False for {path}")


def _short_float(value: Any) -> str:
    if value is None:
        return "None"
    try:
        return f"{float(value):.3f}"
    except Exception:
        return str(value)


def _color(value: Any) -> Tuple[int, int, int]:
    import hashlib

    digest = hashlib.md5(str(value).encode("utf-8")).digest()
    return digest[0], digest[1], digest[2]


def _save_diagnostic_video(
    snapshots: Optional[List[Dict[str, Any]]],
    output_dir: Path,
    *,
    latest_snapshot: Optional[Dict[str, Any]],
    env: Any,
    execution_diagnostics: Optional[List[Dict[str, Any]]],
    filename: str,
    metadata_filename: str,
    crop_to_task_scene: bool,
    task_room: Optional[str],
    scene_graph_config: Optional[SceneGraphConfig],
) -> Dict[str, Any]:
    from .visualization import (
        _history_snapshots,
        _history_task_scene_context,
        _render_scene_graph_bev,
        _robot_pose_from_frame_pose,
        _video_frame_array,
        set_visualization_config,
    )
    set_visualization_config(scene_graph_config)

    output_dir = Path(output_dir)
    timeline = _history_snapshots(snapshots, latest_snapshot)
    renderable = [
        snapshot
        for snapshot in timeline
        if isinstance(snapshot, dict)
    ]
    detection_index = _detection_image_index(output_dir)
    frame_pose_index = _frame_pose_index(output_dir)
    metadata_path = output_dir / metadata_filename
    if not detection_index:
        metadata = {
            "saved": False,
            "reason": "missing_detection_images",
            "frame_count": 0,
            "detection_image_count": len(detection_index),
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        metadata = {"saved": False, "reason": "ffmpeg_not_found", "frame_count": 0}
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    fixed_task_scene = None
    if crop_to_task_scene:
        fixed_task_scene = _history_task_scene_context(
            renderable,
            execution_diagnostics=execution_diagnostics,
            env=env,
            task_room=task_room,
        )
    task_scene_max_side = None
    task_scene_max_scale = None
    if crop_to_task_scene:
        config = scene_graph_config or SceneGraphConfig()
        task_scene_max_side = config.option_int(
            "ISBENCH_SCENE_GRAPH_TASK_SCENE_BEV_MAX_SIDE",
            4096,
        )
        task_scene_max_scale = config.option_float(
            "ISBENCH_SCENE_GRAPH_TASK_SCENE_BEV_MAX_SCALE",
            48.0,
        )

    indexed_snapshots = sorted(
        (
            _snapshot_frame_index(snapshot, index),
            index,
            snapshot,
        )
        for index, snapshot in enumerate(renderable)
    )
    rendered_rows = []
    for index, frame_index in enumerate(sorted(detection_index)):
        snapshot_frame_index, snapshot = _snapshot_at_or_before(
            indexed_snapshots,
            frame_index,
        )
        canvas = None
        frame_metadata: Dict[str, Any] = {
            "saved": False,
            "reason": "missing_scene_graph_snapshot",
            "drawn_node_count": 0,
            "relation_edge_count": 0,
        }
        frame_pose = _frame_pose_at_or_before(frame_pose_index, frame_index)
        robot_pose_override = _robot_pose_from_frame_pose(frame_pose)
        if snapshot is not None:
            canvas, frame_metadata = _render_scene_graph_bev(
                snapshot,
                env=env,
                execution_diagnostics=execution_diagnostics,
                task_room=task_room,
                crop_to_task_scene=crop_to_task_scene,
                task_scene_override=fixed_task_scene,
                max_side_override=task_scene_max_side,
                max_scale_override=task_scene_max_scale,
                robot_pose_override=robot_pose_override,
            )
        bev = _video_frame_array(canvas) if canvas is not None else None
        rendered_rows.append(
            {
                "index": index,
                "frame_index": frame_index,
                "snapshot_frame_index": snapshot_frame_index,
                "bev": bev,
                "metadata": frame_metadata,
            }
        )

    bev_height = max(
        (row["bev"].shape[0] for row in rendered_rows if row["bev"] is not None),
        default=720,
    )
    bev_width = max(
        (row["bev"].shape[1] for row in rendered_rows if row["bev"] is not None),
        default=960,
    )
    frames = []
    frame_summaries = []
    for row in rendered_rows:
        frame_index = row["frame_index"]
        frame_metadata = row["metadata"]
        bev = _normalise_bev_frame(
            row["bev"],
            width=bev_width,
            height=bev_height,
            frame_index=frame_index,
            reason=frame_metadata.get("reason"),
        )
        previous_frame = _previous_detection_frame(detection_index, frame_index)
        frames.append(
            _compose_video_frame(
                bev,
                detection_index.get(previous_frame),
                detection_index.get(frame_index),
                previous_frame,
                frame_index,
            )
        )
        frame_summaries.append(
            {
                "index": row["index"],
                "frame_index": frame_index,
                "snapshot_frame_index": row["snapshot_frame_index"],
                "previous_frame_index": previous_frame,
                "scene_graph_rendered": row["bev"] is not None,
                "bev_reason": frame_metadata.get("reason"),
                "drawn_node_count": frame_metadata.get("drawn_node_count", 0),
                "relation_edge_count": frame_metadata.get("relation_edge_count", 0),
                "robot_heading_source": (
                    "frame_camera_pose" if robot_pose_override is not None else "env_robot_pose"
                ),
            }
        )

    if not frames:
        metadata = {"saved": False, "reason": "no_renderable_frames", "frame_count": 0}
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    video_path = output_dir / filename
    config = scene_graph_config or SceneGraphConfig()
    fps = config.option_float("ISBENCH_SCENE_GRAPH_BEV_VIDEO_FPS", 2.0)
    try:
        _write_video(frames, video_path, fps)
    except Exception as exc:
        metadata = {
            "saved": False,
            "reason": "video_write_failed",
            "error": f"{exc.__class__.__name__}: {exc}",
            "frame_count": len(frames),
            "fps": fps,
            "width": int(frames[0].shape[1]),
            "height": int(frames[0].shape[0]),
            "detection_overlay": True,
            "detection_layout": "previous_current_stacked_bev",
            "detection_image_count": len(detection_index),
            "frame_pose_count": len(frame_pose_index),
            "task_scene": fixed_task_scene,
            "frames": frame_summaries,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata
    metadata = {
        "saved": True,
        "video": str(video_path),
        "frame_count": len(frames),
        "fps": fps,
        "width": int(frames[0].shape[1]),
        "height": int(frames[0].shape[0]),
        "detection_overlay": True,
        "detection_layout": "previous_current_stacked_bev",
        "detection_image_count": len(detection_index),
        "frame_pose_count": len(frame_pose_index),
        "task_scene": fixed_task_scene,
        "frames": frame_summaries,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def _frame_pose_payload(frame: Any) -> Dict[str, Any]:
    return {
        "robot_position": getattr(frame, "robot_position", None),
        "camera_pose": getattr(frame, "camera_pose", None),
        "sensor_name": getattr(frame, "sensor_name", None),
    }


def _detection_image_index(root: Path) -> Dict[int, Path]:
    index: Dict[int, Path] = {}
    patterns = [
        "vis_output/frame_*_matched_objs_rels.jpg",
        "vis_output/frame_*_vlm_bbox.jpg",
        "vis_output/frame_*_grounded_sam.jpg",
    ]
    for pattern in patterns:
        for path in sorted(root.rglob(pattern)):
            frame_index = _frame_index_from_path(path)
            index.setdefault(frame_index, path)
    return index


def _frame_pose_index(root: Path) -> Dict[int, Dict[str, Any]]:
    index: Dict[int, Dict[str, Any]] = {}
    for path in sorted(root.rglob("scene_graph_output/frame_*_debug.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        frame_index = int(payload.get("frame_index", _frame_index_from_path(path)))
        frame_pose = payload.get("frame_pose")
        if isinstance(frame_pose, dict):
            index[frame_index] = frame_pose
    return index


def _frame_pose_at_or_before(
    index: Dict[int, Dict[str, Any]],
    frame_index: int,
) -> Optional[Dict[str, Any]]:
    if frame_index in index:
        return index[frame_index]
    previous = [candidate for candidate in index if candidate < frame_index]
    return index[max(previous)] if previous else None


def _snapshot_frame_index(snapshot: Dict[str, Any], fallback: int) -> int:
    summary = snapshot.get("summary") or snapshot.get("metadata") or {}
    value = summary.get("frame_index")
    if value is None:
        value = summary.get("global_step_index")
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _snapshot_at_or_before(
    indexed_snapshots: List[Tuple[int, int, Dict[str, Any]]],
    frame_index: int,
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    selected_frame = None
    selected_snapshot = None
    for snapshot_frame, _, snapshot in indexed_snapshots:
        if snapshot_frame > frame_index:
            break
        selected_frame = snapshot_frame
        selected_snapshot = snapshot
    return selected_frame, selected_snapshot


def _normalise_bev_frame(
    bev: Optional[np.ndarray],
    *,
    width: int,
    height: int,
    frame_index: int,
    reason: Optional[str],
) -> np.ndarray:
    panel = np.zeros((height, width, 3), dtype=np.uint8)
    panel[:, :] = (248, 249, 250)
    if bev is not None:
        panel[: bev.shape[0], : bev.shape[1]] = bev[:, :, :3]
        return panel

    _draw_label(panel, "Scene Graph BEV", (24, 36), (24, 28, 33))
    _draw_label(panel, f"frame: {frame_index}", (24, 68), (60, 67, 76))
    _draw_label(
        panel,
        "no promoted scene graph nodes with 3D position",
        (24, 116),
        (91, 99, 110),
    )
    if reason:
        _draw_label(panel, f"reason: {reason}", (24, 148), (91, 99, 110))
    return panel


def _previous_detection_frame(index: Dict[int, Path], frame_index: int) -> Optional[int]:
    previous = [candidate for candidate in index if candidate < frame_index]
    return max(previous) if previous else None


def _compose_video_frame(
    bev: np.ndarray,
    previous_path: Optional[Path],
    current_path: Optional[Path],
    previous_frame: Optional[int],
    current_frame: int,
) -> np.ndarray:
    panel_width = max(420, min(760, max(420, bev.shape[1] // 4)))
    if bev.shape[0] >= 440:
        top_height = max(220, bev.shape[0] // 2)
        bottom_height = bev.shape[0] - top_height
    else:
        top_height = max(1, bev.shape[0] // 2)
        bottom_height = max(1, bev.shape[0] - top_height)
    previous = _image_panel(
        previous_path,
        f"previous detection: {previous_frame}",
        panel_width,
        top_height,
    )
    current = _image_panel(
        current_path,
        f"current detection: {current_frame}",
        panel_width,
        bottom_height,
    )
    detection_column = np.concatenate([previous, current], axis=0)
    return np.concatenate([detection_column, bev[:, :, :3]], axis=1)


def _image_panel(path: Optional[Path], title: str, width: int, height: int) -> np.ndarray:
    panel = np.zeros((height, width, 3), dtype=np.uint8)
    panel[:, :] = (242, 244, 247)
    _draw_label(panel, title, (12, 24), (24, 28, 33))
    if path is None or not path.exists():
        _draw_label(panel, "missing detection image", (18, 64), (91, 99, 110))
        return panel
    try:
        image = np.asarray(Image.open(path).convert("RGB"))
    except Exception as exc:
        _draw_label(panel, f"failed to open: {exc}", (18, 64), (120, 52, 45))
        return panel
    target_w = max(1, width - 24)
    target_h = max(1, height - 72)
    scale = min(target_w / image.shape[1], target_h / image.shape[0])
    new_w = max(1, int(round(image.shape[1] * scale)))
    new_h = max(1, int(round(image.shape[0] * scale)))
    resized = np.asarray(Image.fromarray(image).resize((new_w, new_h), Image.Resampling.BILINEAR))
    x = 12 + (target_w - new_w) // 2
    y = 56 + (target_h - new_h) // 2
    panel[y : y + new_h, x : x + new_w] = resized
    return panel


def _write_video(frames: List[np.ndarray], path: Path, fps: float) -> None:
    height, width = frames[0].shape[:2]
    even_width = width + (width % 2)
    even_height = height + (height % 2)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to save diagnostic videos")
    tmp_path = _video_temp_path(path)
    if tmp_path.exists():
        tmp_path.unlink()
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
        f"{even_width}x{even_height}",
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
        str(tmp_path),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for frame in frames:
            if frame.shape[:2] != (even_height, even_width):
                padded = np.zeros((even_height, even_width, 3), dtype=np.uint8)
                padded[:, :] = (248, 249, 250)
                padded[: frame.shape[0], : frame.shape[1]] = frame[:, :, :3]
                frame = padded
            process.stdin.write(np.ascontiguousarray(frame[:, :, :3]).tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        _unlink_if_exists(tmp_path)
        raise
    if return_code != 0:
        _unlink_if_exists(tmp_path)
        raise RuntimeError(f"ffmpeg failed to save diagnostic video: {stderr.strip()}")
    validation_error = _video_validation_error(tmp_path)
    if validation_error is not None:
        _unlink_if_exists(tmp_path)
        raise RuntimeError(f"ffmpeg wrote an invalid diagnostic video: {validation_error}")
    os.replace(tmp_path, path)


def _video_validation_error(path: Path) -> Optional[str]:
    if not path.exists():
        return "output file does not exist"
    size = path.stat().st_size
    if size < 1024:
        return f"output file is too small ({size} bytes)"
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return probe.stderr.strip() or f"ffprobe exited with {probe.returncode}"
    return None


def _video_temp_path(path: Path) -> Path:
    return path.with_name(f".{path.stem}.tmp{path.suffix}")


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
