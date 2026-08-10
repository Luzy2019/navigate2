import base64
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)

from .utils import (
    bbox_from_mask,
    ensure_path_exists,
    insert_sys_path,
    model_root,
    repo_root,
    room_lookup_from_env,
    to_builtin,
)

_ACTIVE_SCENE_GRAPH_CONFIG = SceneGraphConfig()


def _set_scene_graph_config(config: Optional[SceneGraphConfig]) -> SceneGraphConfig:
    global _ACTIVE_SCENE_GRAPH_CONFIG
    _ACTIVE_SCENE_GRAPH_CONFIG = config or SceneGraphConfig()
    return _ACTIVE_SCENE_GRAPH_CONFIG


def _cfg() -> SceneGraphConfig:
    return _ACTIVE_SCENE_GRAPH_CONFIG


OPENAI_BASE_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE", "")

def _bbox_iou(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    if a is None or b is None:
        return 0.0
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return 0.0 if union <= 0 else inter / union


def _bbox_area(bbox: Optional[List[float]]) -> float:
    if bbox is None:
        return 0.0
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def _bbox_intersection_area(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    if a is None or b is None:
        return 0.0
    x1 = max(float(a[0]), float(b[0]))
    y1 = max(float(a[1]), float(b[1]))
    x2 = min(float(a[2]), float(b[2]))
    y2 = min(float(a[3]), float(b[3]))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _bbox_center(bbox: Optional[List[float]]) -> Optional[Tuple[float, float]]:
    if bbox is None:
        return None
    return ((float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0)


def _point_inside_bbox(point: Optional[Tuple[float, float]], bbox: Optional[List[float]]) -> bool:
    if point is None or bbox is None:
        return False
    x, y = point
    return float(bbox[0]) <= x <= float(bbox[2]) and float(bbox[1]) <= y <= float(bbox[3])


def _point_inside_mask(point: Optional[Tuple[float, float]], mask: Any) -> Optional[bool]:
    if point is None or mask is None:
        return None
    mask_array = np.asarray(mask, dtype=bool)
    if mask_array.ndim < 2 or mask_array.size == 0:
        return None
    x, y = point
    xi = int(round(x))
    yi = int(round(y))
    height, width = mask_array.shape[:2]
    if xi < 0 or yi < 0 or xi >= width or yi >= height:
        return False
    return bool(mask_array[yi, xi])


def _mask_coverage_for_bbox(bbox: Optional[List[float]], mask: Any) -> Optional[float]:
    if bbox is None or mask is None:
        return None
    mask_array = np.asarray(mask, dtype=bool)
    if mask_array.ndim < 2 or mask_array.size == 0:
        return None
    height, width = mask_array.shape[:2]
    x1 = max(0, min(width, int(np.floor(float(bbox[0])))))
    y1 = max(0, min(height, int(np.floor(float(bbox[1])))))
    x2 = max(0, min(width, int(np.ceil(float(bbox[2])))))
    y2 = max(0, min(height, int(np.ceil(float(bbox[3])))))
    if x2 <= x1 or y2 <= y1:
        return None
    crop = mask_array[y1:y2, x1:x2]
    return float(crop.mean()) if crop.size else None


def _bbox_match_metrics(
    vlm_bbox: Optional[List[float]],
    sam_bbox: Optional[List[float]],
    sam_mask: Any = None,
) -> Dict[str, Any]:
    iou = _bbox_iou(vlm_bbox, sam_bbox)
    vlm_area = _bbox_area(vlm_bbox)
    sam_area = _bbox_area(sam_bbox)
    intersection = _bbox_intersection_area(vlm_bbox, sam_bbox)
    center = _bbox_center(vlm_bbox)
    return {
        "iou": float(iou),
        "vlm_coverage": float(intersection / vlm_area) if vlm_area > 0 else 0.0,
        "sam_coverage": float(intersection / sam_area) if sam_area > 0 else 0.0,
        "area_ratio": float(sam_area / vlm_area) if vlm_area > 0 else float("inf"),
        "center_in_sam_bbox": _point_inside_bbox(center, sam_bbox),
        "center_in_sam_mask": _point_inside_mask(center, sam_mask),
        "mask_coverage": _mask_coverage_for_bbox(vlm_bbox, sam_mask),
    }


def _bbox_match_decision(metrics: Dict[str, Any], iou_threshold: float) -> Tuple[bool, str, float]:
    iou = float(metrics.get("iou") or 0.0)
    vlm_coverage = float(metrics.get("vlm_coverage") or 0.0)
    mask_coverage = metrics.get("mask_coverage")
    mask_coverage_value = 0.0 if mask_coverage is None else float(mask_coverage)
    area_ratio = float(metrics.get("area_ratio") or float("inf"))
    center_inside = metrics.get("center_in_sam_mask")
    if center_inside is None:
        center_inside = metrics.get("center_in_sam_bbox")

    coverage_threshold = _env_float("ISBENCH_SAMJAM_MATCH_VLM_COVERAGE", 0.5)
    mask_coverage_threshold = _env_float("ISBENCH_SAMJAM_MATCH_MASK_COVERAGE", 0.35)
    max_area_ratio = _env_float("ISBENCH_SAMJAM_MATCH_COVERAGE_MAX_AREA_RATIO", 20.0)
    center_min_coverage = _env_float("ISBENCH_SAMJAM_MATCH_CENTER_MIN_COVERAGE", 0.2)
    center_max_area_ratio = _env_float("ISBENCH_SAMJAM_MATCH_CENTER_MAX_AREA_RATIO", 12.0)

    if center_inside and iou >= iou_threshold:
        return True, "iou", iou
    if center_inside and area_ratio <= max_area_ratio and mask_coverage_value >= mask_coverage_threshold:
        return True, "mask_coverage", max(iou, mask_coverage_value)
    if center_inside and area_ratio <= max_area_ratio and vlm_coverage >= coverage_threshold:
        return True, "vlm_coverage", max(iou, vlm_coverage)
    if center_inside and area_ratio <= center_max_area_ratio and vlm_coverage >= center_min_coverage:
        return True, "center", max(iou, vlm_coverage)
    return False, "low_geometry", max(iou, vlm_coverage, mask_coverage_value)


def _env_bool(name: str, default: bool) -> bool:
    return _cfg().option_bool(name, default)


def _env_float(name: str, default: float) -> float:
    return _cfg().option_float(name, default)


def _env_int(name: str, default: int) -> int:
    return _cfg().option_int(name, default)


def _debug_matching_enabled() -> bool:
    return (
        _env_bool("ISBENCH_SCENE_GRAPH_DEBUG_MATCHING", False)
        or _env_bool("ISBENCH_SAMJAM_DEBUG_MATCHING", False)
        or bool(_cfg().debug_log_path)
        or bool(_cfg().output_debug_matching)
    )


def _debug_log_path() -> Path:
    explicit = _cfg().debug_log_path
    if explicit:
        return Path(explicit)
    output_dir = _cfg().output_dir or _cfg().option("ISBENCH_SAMJAM_OUTPUT_DIR")
    if output_dir:
        return Path(output_dir) / "scene_graph_debug.log"
    return Path("scene_graph_debug.log")


def _append_debug_log(lines: List[str]) -> None:
    if not _debug_matching_enabled() or not lines:
        return
    path = _debug_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(f"{line}\n")


def _suppress_vendor_output_enabled() -> bool:
    return bool(_cfg().suppress_vendor_output) and _env_bool(
        "ISBENCH_SAMJAM_SUPPRESS_VENDOR_OUTPUT", True
    )


@contextlib.contextmanager
def _maybe_suppress_vendor_output():
    if not _suppress_vendor_output_enabled():
        yield
        return
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*cannot import name '_C'.*")
        warnings.filterwarnings("ignore", message=".*Skipping the post-processing step.*")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield


def _normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _category_from_name(name: Optional[str]) -> str:
    category = _normalize_name(name)
    return category or "object"


def _canonical_object_name(name: Optional[str], is_hand: bool = False) -> str:
    """Map noisy VLM / detector labels onto stable scene-graph categories."""
    normalized = _category_from_name(name)
    allow_human_hands = _env_bool("ISBENCH_SAMJAM_ALLOW_HUMAN_HANDS", False)

    # SamJam may describe the Fetch arm with visual labels such as "blue arm".
    # Keep those detections separate from the gripper / hand contact region.
    robot_arm_names = {
        "blue_arm",
        "blue_robotic_arm",
        "robot_arm",
        "robotic_arm",
        "fetch_arm",
        "manipulator",
    }
    if normalized in robot_arm_names or ("robot" in normalized and "arm" in normalized):
        return "robot_arm"

    # In first-person robot videos, VLMs often call the gripper a human hand or
    # person. Unless explicitly allowed for debugging, canonicalize those labels
    # to the robot gripper so relations remain consistent across frames.
    if not allow_human_hands:
        if is_hand or "hand" in normalized or normalized in {"person", "human", "man", "woman"}:
            return "robot_gripper"

    # Normalize common countertop variants so the memory graph does not split
    # one physical support surface into multiple object nodes.
    aliases = {
        "black_slove": "stove",
        "black_stove": "stove",
        "half_banana": "banana",
        "tissue_box_box": "tissue_box",
    }
    if normalized in aliases:
        return aliases[normalized]
    if normalized in {"water_bottle", "waterbottle"}:
        return "bottle"
    if normalized in {"counter", "countertop", "kitchen_countertop"}:
        return "kitchen_counter"
    return normalized


def _task_category_name(value: Optional[str]) -> str:
    """Convert a BDDL entity ID into the category used by the VLM graph."""

    text = re.sub(r"\.n\.\d+_\d+$", "", str(value or "").strip())
    return _canonical_object_name(text)


CLOSED_RELATIONSHIPS = frozenset({"on", "in", "above", "attach to", "near"})


def _normalize_vlm_predicate(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "attached to": "attach to",
    }.get(text, text)


def _extract_json_object(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("VLM scene graph response must be a JSON object")
    return parsed


def _vlm_bbox_to_xyxy(bbox: Any, image_shape: Tuple[int, ...]) -> Optional[List[float]]:
    try:
        if bbox is None or len(bbox) != 4:
            return None
    except TypeError:
        return None
    height, width = int(image_shape[0]), int(image_shape[1])
    try:
        y_min, x_min, y_max, x_max = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    x1 = np.clip(x_min / 1000.0 * width, 0, width - 1)
    y1 = np.clip(y_min / 1000.0 * height, 0, height - 1)
    x2 = np.clip(x_max / 1000.0 * width, 0, width - 1)
    y2 = np.clip(y_max / 1000.0 * height, 0, height - 1)
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 <= x1 or y2 <= y1:
        return None
    return [float(x1), float(y1), float(x2), float(y2)]


@dataclass
class MaskCandidate:
    index: int
    mask: np.ndarray
    bbox: List[float]
    position: Optional[List[float]]
    room_id: str
    confidence: float
    attributes: Dict[str, Any]


class SAMJAMOutputWriter:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.scene_graph_dir = output_dir / "scene_graph_output"
        self.vis_dir = output_dir / "vis_output"
        for directory in (self.scene_graph_dir, self.vis_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        frame: FrameObservation,
        vlm_scene_graph: Optional[Dict[str, Any]],
        candidates: List[MaskCandidate],
        objects: List[PerceivedObject],
        relations: List[PerceivedRelation],
        scene_graph: Optional[Dict[str, Any]] = None,
        room_graph: Optional[Dict[str, Any]] = None,
        group_graph: Optional[Dict[str, Any]] = None,
        match_summary: Optional[Dict[str, Any]] = None,
        filter_report: Optional[Dict[str, Any]] = None,
        graph_objects: Optional[List[PerceivedObject]] = None,
    ) -> None:
        stage = "start"
        try:
            frame_index = frame.frame_index
            image = self._rgb_array(frame.rgb)

            stage = "save_scene_graph_json"
            objs_json = [
                {
                    "id": self._native_object_id(obj),
                    "name": obj.name,
                    "is_hand": bool(obj.attributes.get("is_hand", False)),
                    "is_moving": bool(obj.attributes.get("is_moving", False)),
                    "is_moved": bool(obj.attributes.get("is_moved", False)),
                }
                for obj in objects
            ]
            object_map = {obj.object_id: obj for obj in objects}
            rels_json = {
                f"{self._native_relation_id(rel.source_id, object_map)},{self._native_relation_id(rel.target_id, object_map)}": rel.relation
                for rel in relations
            }
            (self.scene_graph_dir / f"{frame_index}_objs.json").write_text(
                json.dumps(objs_json, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            (self.scene_graph_dir / f"{frame_index}_rels.json").write_text(
                json.dumps(rels_json, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            (self.scene_graph_dir / f"frame_{frame_index}_debug.json").write_text(
                json.dumps(
                    {
                        "frame_index": frame_index,
                        "backend": "samjam_sam2",
                        "frame_pose": {
                            "robot_position": to_builtin(frame.robot_position),
                            "camera_pose": to_builtin(frame.camera_pose),
                            "sensor_name": frame.sensor_name,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            # Keep this directory aligned with SAMJAM's native debug artifacts.
            # Persistent IS-Bench/UniGoal graphs are written by the benchmark report.

            stage = "draw_vlm_bbox"
            self._draw_vlm_bbox(
                image,
                vlm_scene_graph or {"objects": [], "relationships": []},
                self.vis_dir / f"frame_{frame_index}_vlm_bbox.jpg",
            )
            stage = "draw_full_masks"
            self._draw_masks(
                (image.shape[1], image.shape[0]),
                candidates,
                self.vis_dir / f"frame_{frame_index}_full_masks.jpg",
            )
            stage = "draw_matched_masks"
            self._draw_object_masks(
                (image.shape[1], image.shape[0]),
                objects,
                self.vis_dir / f"frame_{frame_index}_matched_masks.jpg",
            )
            stage = "draw_bbox_mask_matches"
            self._draw_bbox_mask_matches(
                image,
                vlm_scene_graph or {"objects": [], "relationships": []},
                candidates,
                objects,
                match_summary or {},
                filter_report or {},
                objects if graph_objects is None else graph_objects,
                self.vis_dir / f"frame_{frame_index}_bbox_mask_matches.jpg",
            )
            stage = "draw_matched_objects_relations"
            self._draw_objects_and_relations(
                image,
                objects,
                relations,
                self.vis_dir / f"frame_{frame_index}_matched_objs_rels.jpg",
            )
        except Exception as exc:
            raise RuntimeError(f"SAMJAMOutputWriter failed at {stage}: {exc}") from exc

    def _rgb_array(self, rgb: np.ndarray) -> np.ndarray:
        image = np.asarray(rgb)
        if image.ndim != 3 or image.shape[-1] < 3:
            raise ValueError(f"expected RGB image with shape HxWx3/4, got {image.shape}")
        image = image[:, :, :3]
        if image.dtype != np.uint8:
            if image.max(initial=0) <= 1.0:
                image = image * 255.0
            image = np.clip(image, 0, 255).astype(np.uint8)
        return np.ascontiguousarray(image)

    def _write_rgb_image(self, output_path: Path, image: np.ndarray) -> None:
        import cv2

        output_path.parent.mkdir(parents=True, exist_ok=True)
        ok = cv2.imwrite(str(output_path), np.ascontiguousarray(image[:, :, ::-1]))
        if not ok:
            raise RuntimeError(f"cv2.imwrite returned False for {output_path}")

    def _draw_vlm_bbox(self, image: np.ndarray, scene_graph: Dict[str, Any], output_path: Path) -> None:
        canvas = image.copy()
        height, width = canvas.shape[:2]
        for obj in scene_graph.get("objects", []):
            bbox = _vlm_bbox_to_xyxy(obj.get("bbox"), (height, width, 3))
            if bbox is None:
                continue
            color = self._color(obj.get("id"))
            self._rectangle(canvas, bbox, color)
            self._text(canvas, (bbox[0] + 2.0, bbox[1] + 14.0), f"{obj.get('name')} ({obj.get('id')})", color)
        for rel in scene_graph.get("relationships", []):
            source = self._find_vlm_object(scene_graph, rel.get("subj_id"))
            target = self._find_vlm_object(scene_graph, rel.get("obj_id"))
            if source is None or target is None:
                continue
            source_bbox = _vlm_bbox_to_xyxy(source.get("bbox"), (height, width, 3))
            target_bbox = _vlm_bbox_to_xyxy(target.get("bbox"), (height, width, 3))
            if source_bbox is None or target_bbox is None:
                continue
            self._relation_line(canvas, source_bbox, target_bbox, str(rel.get("predicate", "")))
        self._write_rgb_image(output_path, canvas)

    def _draw_masks(self, image_size: Tuple[int, int], candidates: List[MaskCandidate], output_path: Path) -> None:
        width, height = image_size
        pixels = np.zeros((height, width, 3), dtype=np.uint8)
        for candidate in sorted(candidates, key=lambda item: item.attributes.get("mask_area", 0), reverse=True):
            color = np.asarray(self._color(candidate.index), dtype=np.uint8)
            mask = candidate.mask
            if mask.shape[:2] == pixels.shape[:2]:
                pixels[mask] = color
        self._write_rgb_image(output_path, pixels)

    def _draw_object_masks(self, image_size: Tuple[int, int], objects: List[PerceivedObject], output_path: Path) -> None:
        width, height = image_size
        pixels = np.zeros((height, width, 3), dtype=np.uint8)
        for obj in sorted(objects, key=lambda item: item.attributes.get("mask_area", 0), reverse=True):
            if obj.mask is None:
                continue
            color = np.asarray(self._color(obj.object_id), dtype=np.uint8)
            mask = np.asarray(obj.mask, dtype=bool)
            if mask.shape[:2] == pixels.shape[:2]:
                pixels[mask] = color
        self._write_rgb_image(output_path, pixels)

    def _draw_bbox_mask_matches(
        self,
        image: np.ndarray,
        scene_graph: Dict[str, Any],
        candidates: List[MaskCandidate],
        objects: List[PerceivedObject],
        match_summary: Dict[str, Any],
        filter_report: Dict[str, Any],
        graph_objects: List[PerceivedObject],
        output_path: Path,
    ) -> None:
        canvas = image.copy()
        candidate_by_index = {int(candidate.index): candidate for candidate in candidates}
        object_by_native = {
            self._native_id_key(self._native_object_id(obj)): obj
            for obj in objects
            if self._native_object_id(obj) is not None
        }
        filter_by_native = self._filter_report_by_native_id(filter_report)
        graph_native_ids = self._graph_native_ids(graph_objects)
        drawn_count = 0

        for detail in match_summary.get("match_details", []):
            vlm_bbox = detail.get("vlm_bbox")
            native_id = detail.get("best_native_id")
            native_key = self._native_id_key(native_id)
            if not detail.get("accepted") or native_key not in graph_native_ids:
                continue
            obj = object_by_native.get(native_key)
            candidate = None
            try:
                candidate = candidate_by_index.get(int(native_id))
            except (TypeError, ValueError):
                candidate = None
            mask = self._mask_for_match_detail(detail, obj, candidate)
            if mask is None or mask.shape[:2] != canvas.shape[:2]:
                continue
            sam_bbox = (
                detail.get("sam_bbox")
                or (None if obj is None else obj.bbox)
                or (None if candidate is None else candidate.bbox)
            )

            filter_row = {**(filter_by_native.get(native_key) or {}), "valid_node": True}
            color = self._match_color(detail, filter_row)
            self._overlay_mask(canvas, mask, color, alpha=0.38)
            if vlm_bbox is not None:
                self._rectangle(canvas, vlm_bbox, color)
            if sam_bbox is not None:
                self._rectangle(canvas, sam_bbox, (255, 255, 255))

            label_bbox = vlm_bbox or sam_bbox
            if label_bbox is not None:
                self._text(
                    canvas,
                    (float(label_bbox[0]) + 2.0, float(label_bbox[1]) + 14.0),
                    self._match_label(detail, filter_row),
                    color,
                )
            drawn_count += 1

        if drawn_count == 0:
            self._text(
                canvas,
                (8.0, 18.0),
                "no promoted graph objects with valid bbox-mask matches",
                (255, 255, 255),
            )
        self._write_rgb_image(output_path, canvas)

    def _graph_native_ids(self, graph_objects: List[PerceivedObject]) -> set[str]:
        native_ids = set()
        for obj in graph_objects:
            if not obj.attributes.get("currently_visible", True):
                continue
            source_ids = obj.attributes.get("source_ids") or {}
            for value in (
                obj.attributes.get("samjam_id"),
                obj.attributes.get("source_object_id"),
                source_ids.get("samjam_object"),
                source_ids.get("samjam_object_id"),
                obj.object_id,
            ):
                native_key = self._native_id_key(value)
                if native_key is not None:
                    native_ids.add(native_key)
        return native_ids

    def _native_id_key(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        match = re.match(r"samjam_object:(\d+)$", str(value))
        return match.group(1) if match else str(value)

    def _filter_report_by_native_id(self, filter_report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        by_native: Dict[str, Dict[str, Any]] = {}
        for item in filter_report.get("kept_objects", []) or []:
            native_id = item.get("native_id")
            if native_id is not None:
                by_native[str(native_id)] = {"valid_node": True, **item}
        for item in filter_report.get("rejected_objects", []) or []:
            native_id = item.get("native_id")
            if native_id is not None and str(native_id) not in by_native:
                by_native[str(native_id)] = {"valid_node": False, **item}
        return by_native

    def _mask_for_match_detail(
        self,
        detail: Dict[str, Any],
        obj: Optional[PerceivedObject],
        candidate: Optional[MaskCandidate],
    ) -> Optional[np.ndarray]:
        debug_mask = detail.get("_debug_mask")
        if debug_mask is not None:
            return np.asarray(debug_mask, dtype=bool)
        if obj is not None and obj.mask is not None:
            return np.asarray(obj.mask, dtype=bool)
        if candidate is not None:
            return np.asarray(candidate.mask, dtype=bool)
        return None

    def _match_color(
        self,
        detail: Dict[str, Any],
        filter_row: Optional[Dict[str, Any]],
    ) -> Tuple[int, int, int]:
        if filter_row is not None:
            return (0, 220, 80) if filter_row.get("valid_node") else (255, 80, 80)
        return (0, 220, 80) if detail.get("accepted") else (255, 190, 0)

    def _match_label(
        self,
        detail: Dict[str, Any],
        filter_row: Optional[Dict[str, Any]],
    ) -> str:
        iou = detail.get("best_iou")
        try:
            iou_text = f"{float(iou):.3f}"
        except (TypeError, ValueError):
            iou_text = "n/a"
        accepted = "Y" if detail.get("accepted") else "N"
        if filter_row is None:
            node_text = "node=?"
            reason = detail.get("reason")
        else:
            node_text = "node=Y" if filter_row.get("valid_node") else "node=N"
            reason = None if filter_row.get("valid_node") else filter_row.get("reason")
        name = detail.get("canonical_name") or detail.get("vlm_name") or "object"
        native = detail.get("best_native_id")
        suffix = f" {reason}" if reason else ""
        score = detail.get("best_score")
        try:
            score_text = f"{float(score):.3f}"
        except (TypeError, ValueError):
            score_text = "n/a"
        accept_reason = detail.get("accept_reason")
        reason_text = f" via={accept_reason}" if accept_reason else ""
        return f"{name}->{native} iou={iou_text} score={score_text} match={accepted} {node_text}{reason_text}{suffix}"

    def _overlay_mask(
        self,
        image: np.ndarray,
        mask: np.ndarray,
        color: Tuple[int, int, int],
        *,
        alpha: float,
    ) -> None:
        if mask.shape[:2] != image.shape[:2]:
            return
        color_array = np.asarray(color, dtype=np.float32)
        image_float = image.astype(np.float32, copy=False)
        image_float[mask] = image_float[mask] * (1.0 - alpha) + color_array * alpha
        image[:] = np.clip(image_float, 0, 255).astype(np.uint8)

    def _draw_objects_and_relations(
        self,
        image: np.ndarray,
        objects: List[PerceivedObject],
        relations: List[PerceivedRelation],
        output_path: Path,
    ) -> None:
        canvas = image.copy()
        visible_objects = [
            obj for obj in objects if obj.attributes.get("currently_visible", True)
        ]
        object_map = {obj.object_id: obj for obj in visible_objects}
        for obj in visible_objects:
            if obj.bbox is None:
                continue
            color = self._color(obj.object_id)
            self._rectangle(canvas, obj.bbox, color)
            self._text(canvas, (obj.bbox[0] + 2.0, obj.bbox[1] + 14.0), f"{obj.name} ({obj.object_id})", color)
        for rel in relations:
            source = object_map.get(rel.source_id)
            target = object_map.get(rel.target_id)
            if source is None or target is None or source.bbox is None or target.bbox is None:
                continue
            self._relation_line(canvas, source.bbox, target.bbox, rel.relation)
        self._write_rgb_image(output_path, canvas)

    def _find_vlm_object(self, scene_graph: Dict[str, Any], object_id: Any) -> Optional[Dict[str, Any]]:
        for obj in scene_graph.get("objects", []):
            if obj.get("id") == object_id:
                return obj
        return None

    def _native_object_id(self, obj: PerceivedObject) -> Any:
        return obj.attributes.get("samjam_id", obj.object_id)

    def _native_relation_id(self, object_id: str, object_map: Dict[str, PerceivedObject]) -> Any:
        obj = object_map.get(object_id)
        if obj is not None:
            return self._native_object_id(obj)
        match = re.match(r"samjam_object:(\d+)$", str(object_id))
        return int(match.group(1)) if match else object_id

    def _relation_line(self, image: np.ndarray, source_bbox: List[float], target_bbox: List[float], label: str) -> None:
        import cv2

        source_center = ((float(source_bbox[0]) + float(source_bbox[2])) / 2.0, (float(source_bbox[1]) + float(source_bbox[3])) / 2.0)
        target_center = ((float(target_bbox[0]) + float(target_bbox[2])) / 2.0, (float(target_bbox[1]) + float(target_bbox[3])) / 2.0)
        if not all(np.isfinite([*source_center, *target_center])):
            return
        source_xy = (int(round(source_center[0])), int(round(source_center[1])))
        target_xy = (int(round(target_center[0])), int(round(target_center[1])))
        cv2.line(image, source_xy, target_xy, (255, 255, 0), 2)
        mid = ((source_center[0] + target_center[0]) / 2.0, (source_center[1] + target_center[1]) / 2.0)
        self._text(image, mid, label, (255, 255, 0))

    def _rectangle(self, image: np.ndarray, bbox: List[float], color: Tuple[int, int, int]) -> None:
        import cv2

        if bbox is None or len(bbox) != 4:
            return
        x1, y1, x2, y2 = [float(v) for v in bbox]
        if not all(np.isfinite([x1, y1, x2, y2])):
            return
        cv2.rectangle(
            image,
            (int(round(x1)), int(round(y1))),
            (int(round(x2)), int(round(y2))),
            color,
            2,
        )

    def _text(self, image: np.ndarray, xy: Tuple[float, float], text: str, color: Tuple[int, int, int]) -> None:
        import cv2

        x, y = xy
        if not all(np.isfinite([x, y])):
            return
        cv2.putText(
            image,
            str(text),
            (int(round(x)), int(round(y))),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            color,
            1,
            cv2.LINE_AA,
        )

    def _color(self, value: Any) -> Tuple[int, int, int]:
        digest = hashlib.md5(str(value).encode("utf-8")).digest()
        return digest[0], digest[1], digest[2]


class SAMJAMVLMAdapter:
    """OpenAI-compatible VLM adapter for SAMJAM frame scene graphs."""

    def __init__(
        self,
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ):
        self.scene_graph_config = scene_graph_config or _cfg()
        self.object_goal: Optional[str] = None
        self.prompt_path = (
            repo_root()
            / "og_ego_prim"
            / "scene_graph"
            / "vendor"
            / "samjam"
            / "vlms"
            / "prompts"
            / "generate_frame_scene_graph.txt"
        )
        self.isbench_prompt_path = self.prompt_path.with_name("isbench_adaptive_prompt.txt")
        self.prompt: Optional[str] = None
        self.allowed_object_names: Tuple[str, ...] = ()
        self.printed_request_config = False

    def set_allowed_object_names(self, names: Tuple[str, ...]) -> None:
        """Set the category-level task-object closure for subsequent VLM calls."""

        self.allowed_object_names = tuple(
            dict.fromkeys(
                _task_category_name(name)
                for name in names
                if _task_category_name(name) and _task_category_name(name) != "agent"
            )
        )

    def generate(
        self,
        frame: FrameObservation,
        task_instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        api_key = OPENAI_BASE_KEY
        base_url = OPENAI_BASE_URL
        if not api_key:
            raise RuntimeError(
                "SAMJAM VLM is enabled but OPENAI_API_KEY is not set. "
                "Set OPENAI_API_KEY or set scene_graph.backend_options.samjam_vlm_enabled=false."
            )

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("SAMJAM VLM requires the openai package") from exc

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        model = str(
            self.scene_graph_config.option("scene_graph_vlm_model", "gpt-4o-mini")
        ).strip()
        self._print_request_config(base_url, model, api_key)

        prompt = self._load_prompt()
        if self.allowed_object_names:
            prompt += (
                "\n\nClosed task object list (category names only):\n- "
                + "\n- ".join(self.allowed_object_names)
                + "\nUse objects[].name exactly from this list. This is a closed "
                "set: do not emit aliases, plural forms, instance suffixes, or "
                "task-external objects. Do not emit the robot, robot arm, gripper, "
                "or an agent node."
            )
        task_focus = re.sub(r"\.n\.\d+_\d+", "", str(task_instruction or ""))
        task_focus = re.sub(r"_+", " ", task_focus)
        task_focus = re.sub(r"\s+", " ", task_focus).strip()
        if task_focus:
            prompt += (
                f"\n\nCurrent subtask focus: {task_focus}\n"
                "Prioritize visible objects needed for this subtask and their direct "
                "support surfaces or containers. Do not infer object instance identity "
                "or emit a category only because the subtask mentions it."
            )
        if self.object_goal:
            prompt += (
                f"\n\nCurrent navigation target: {self.object_goal}. "
                "The robot has just navigated near this target. Make a dedicated second "
                "pass over the full image for it, including small or partially visible "
                "objects, and use this exact category name when it is visible. Do not "
                "invent it when it is absent."
            )
        attempts = 2 if self.object_goal else 1
        for attempt in range(attempts):
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{self._encode_rgb(frame.rgb)}",
                                    "detail": self.scene_graph_config.option(
                                        "ISBENCH_SAMJAM_VLM_IMAGE_DETAIL", "high"
                                    ),
                                },
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
                timeout=float(
                    self.scene_graph_config.option("ISBENCH_SAMJAM_VLM_TIMEOUT", 120)
                ),
            )
            content = completion.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if not content:
                raise RuntimeError("SAMJAM VLM returned an empty scene graph response")
            scene_graph = self._validate_scene_graph(_extract_json_object(content))
            if not self.object_goal or any(
                _normalize_name(obj.get("name")) == _normalize_name(self.object_goal)
                for obj in scene_graph["objects"]
            ):
                return scene_graph
            if attempt == 0:
                prompt += (
                    f"\n\nThe previous pass omitted the visible navigation target "
                    f"{self.object_goal}. Reinspect the image and include it with that "
                    "exact category name and a tight bounding box."
                )
        return scene_graph

    def _print_request_config(self, base_url: Optional[str], model: str, api_key: str) -> None:
        if self.printed_request_config:
            return
        resolved_base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        key_prefix = api_key[:6] if api_key else ""
        key_suffix = api_key[-4:] if api_key else ""
        print(
            "[samjam_sam2] VLM request "
            f"url={resolved_base_url}/chat/completions "
            f"base_url={resolved_base_url} "
            f"model={model} "
            f"api_key={key_prefix}...{key_suffix}"
        )
        self.printed_request_config = True

    def _load_prompt(self) -> str:
        if self.prompt is None:
            if not self.prompt_path.exists():
                raise FileNotFoundError(f"SAMJAM VLM prompt not found: {self.prompt_path}")
            if not self.isbench_prompt_path.exists():
                raise FileNotFoundError(
                    f"SAMJAM IS-Bench prompt not found: {self.isbench_prompt_path}"
                )
            self.prompt = "\n\n".join(
                (
                    self.prompt_path.read_text(encoding="utf-8"),
                    self.isbench_prompt_path.read_text(encoding="utf-8"),
                )
            )

        return self.prompt

    def _encode_rgb(self, rgb: np.ndarray) -> str:
        image = Image.fromarray(np.asarray(rgb, dtype=np.uint8)[:, :, :3]).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    def _validate_scene_graph(self, scene_graph: Dict[str, Any]) -> Dict[str, Any]:
        objects = scene_graph.get("objects") or []
        relationships = scene_graph.get("relationships") or []
        if not isinstance(objects, list) or not isinstance(relationships, list):
            raise ValueError("VLM scene graph must contain list fields: objects, relationships")

        normalized_objects = []
        for index, obj in enumerate(objects):
            if not isinstance(obj, dict):
                continue
            obj_id = obj.get("id", index)
            try:
                obj_id = int(obj_id)
            except (TypeError, ValueError):
                obj_id = index
            name = _task_category_name(obj.get("name"))
            if bool(obj.get("is_hand", False)):
                name = _canonical_object_name(obj.get("name"), is_hand=True)
            if self.allowed_object_names and name not in self.allowed_object_names:
                continue
            if name == "agent":
                continue
            normalized_objects.append(
                {
                    "id": obj_id,
                    "name": name or f"object_{obj_id}",
                    "bbox": obj.get("bbox"),
                    "is_hand": bool(obj.get("is_hand", False)),
                    "is_moving": bool(obj.get("is_moving", False)),
                    "is_vis": bool(obj.get("is_vis", True)),
                }
            )

        normalized_relationships = []
        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            try:
                subj_id = int(rel.get("subj_id"))
                obj_id = int(rel.get("obj_id"))
            except (TypeError, ValueError):
                continue
            predicate = _normalize_vlm_predicate(rel.get("predicate"))
            if not predicate:
                continue
            if predicate not in CLOSED_RELATIONSHIPS:
                raise ValueError(
                    "VLM scene graph predicate must be one of "
                    f"{sorted(CLOSED_RELATIONSHIPS)!r}, got {predicate!r}"
                )
            normalized_relationships.append(
                {"subj_id": subj_id, "obj_id": obj_id, "predicate": predicate}
            )

        return {"objects": normalized_objects, "relationships": normalized_relationships}


class SAMJAMSAM2Backend:
    name = "samjam_sam2"

    def __init__(
        self,
        sensor_name: Optional[str] = None,
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ):
        self.scene_graph_config = _set_scene_graph_config(scene_graph_config)
        self.adapter = ISBenchObservationAdapter(sensor_name=sensor_name)
        self.env = None
        self.mask_generator = None
        self.video_predictor = None
        self.samjam_object_type = None
        self.vlm_adapter: Optional[SAMJAMVLMAdapter] = None
        self.room_lookup = None
        self.output_writer: Optional[SAMJAMOutputWriter] = None
        self.pending_debug: Optional[Dict[str, Any]] = None
        self.last_result: Optional[PerceptionResult] = None
        self.object_goal: Optional[str] = None
        self.task_instruction: Optional[str] = None
        self.task_categories: Tuple[str, ...] = ()
        self._native_video_tmp: Optional[tempfile.TemporaryDirectory] = None
        self._native_video_dir: Optional[Path] = None
        self._reset_native_state()

    def reset(self, env: Any) -> None:
        self.env = env
        self.adapter.reset()
        self.adapter.ensure_robot_sensor_modalities(env)
        self.room_lookup = room_lookup_from_env(env)
        self.pending_debug = None
        output_dir = self.scene_graph_config.output_dir or self.scene_graph_config.option(
            "ISBENCH_SAMJAM_OUTPUT_DIR"
        )
        self.output_writer = SAMJAMOutputWriter(Path(output_dir)) if output_dir else None
        self._reset_native_state()
        self.last_result = None
        self.object_goal = None
        self.task_instruction = None

    def set_object_goal(self, target: str) -> None:
        self.object_goal = str(target).strip() or None
        if self.vlm_adapter is not None:
            self.vlm_adapter.object_goal = self.object_goal

    def set_task_instruction(self, instruction: Optional[str]) -> None:
        self.task_instruction = str(instruction or "").strip() or None

    def set_task_categories(self, categories: Tuple[str, ...]) -> None:
        self.task_categories = tuple(
            dict.fromkeys(
                _task_category_name(category)
                for category in categories
                if _task_category_name(category)
            )
        )
        if self.vlm_adapter is not None:
            self.vlm_adapter.set_allowed_object_names(self.task_categories)

    def observe(self, env: Any) -> FrameObservation:
        return self.adapter.observe(env)

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        with _maybe_suppress_vendor_output():
            generator = self._ensure_mask_generator()
        self._store_native_frame(frame)
        import torch

        autocast = (
            torch.autocast("cuda", dtype=torch.bfloat16)
            if generator.predictor.device.type == "cuda"
            else contextlib.nullcontext()
        )
        with _maybe_suppress_vendor_output(), autocast:
            masks = generator.generate(frame.rgb)
        vlm_enabled = self._vlm_enabled()
        vlm_scene_graph = {"objects": [], "relationships": []}
        match_summary: Dict[str, Any] = {}
        if vlm_enabled:
            vlm_scene_graph = self._ensure_vlm_adapter().generate(
                frame,
                task_instruction=self.task_instruction,
            )
            vlm_scene_graph = self._filter_vlm_scene_graph(vlm_scene_graph)
            candidates = self._mask_candidates(frame, masks, vlm_scene_graph)
            candidates = self._box_prompt_candidates(
                frame,
                vlm_scene_graph,
                candidates,
            )
            match_summary = self._update_native_samjam(frame, vlm_scene_graph, candidates)
            objects, relations = self._native_objects_and_relations(frame)
        else:
            candidates = self._mask_candidates(frame, masks)
            objects = self._objects_from_masks(frame, masks)
            relations = self._relations_from_overlaps(objects)
            match_summary = {
                "matched_object_count": len(objects),
                "unmatched_vlm_object_count": 0,
                "unmatched_mask_count": 0,
                "rejected_vlm_objects": [],
            }

        result = PerceptionResult(
            backend=self.name,
            frame_index=frame.frame_index,
            objects=objects,
            relations=relations,
            scene_graph={
                "nodes": [
                    {
                        "id": obj.object_id,
                        "name": obj.name,
                        "position": obj.position,
                        "category": obj.category,
                        "bbox": obj.bbox,
                    }
                    for obj in objects
                ],
                "edges": [
                    {"source": rel.source_id, "target": rel.target_id, "type": rel.relation}
                    for rel in relations
                ],
            },
            room_graph=self._room_graph(objects),
            group_graph=self._group_graph(objects, relations),
            goal_graph={},
            scene_goal_matches={"enabled": False, "reason": "samjam backend does not build goal graph in v1"},
            metadata={
                "sensor_name": frame.sensor_name,
                "rgb_shape": list(frame.rgb.shape),
                "depth_shape": None if frame.depth is None else list(frame.depth.shape),
                "mask_count": len(masks),
                "vendor": "SAMJAM/sam2",
                "graph_mode": "samjam_native_video_sgg",
                "identity_tracking": (
                    "sam2_video_predictor" if vlm_enabled else "none_vlm_disabled"
                ),
                "vlm_enabled": vlm_enabled,
                "vlm_object_count": len(vlm_scene_graph.get("objects", [])),
                "vlm_relation_count": len(vlm_scene_graph.get("relationships", [])),
                "matched_object_count": match_summary.get("matched_object_count", 0),
                "unmatched_vlm_object_count": match_summary.get("unmatched_vlm_object_count", 0),
                "unmatched_mask_count": match_summary.get("unmatched_mask_count", 0),
                "match_iou_threshold": _env_float("ISBENCH_SAMJAM_NATIVE_MATCH_IOU", 0.25),
                "rejected_vlm_objects": match_summary.get("rejected_vlm_objects", []),
                "samjam_match_details": [
                    {
                        key: value
                        for key, value in detail.items()
                        if not str(key).startswith("_debug_")
                    }
                    for detail in match_summary.get("match_details", [])
                ],
                "vlm_scene_graph": self._compact_vlm_scene_graph(vlm_scene_graph),
                "overlap_relations_enabled": self._overlap_relations_enabled(),
                "samjam_total_object_count": len(self.samjam_total_objs),
                "samjam_current_object_count": len(self.samjam_cur_objs),
                "samjam_relation_count": len(self.samjam_rels),
                "samjam_local_frame_index": len(self.samjam_frame_indices) - 1,
            },
        )
        self.pending_debug = {
            "frame": frame,
            "vlm_scene_graph": vlm_scene_graph,
            "candidates": candidates,
            "frame_objects": objects,
            "frame_relations": relations,
            "match_summary": match_summary,
        }
        self._write_samjam_detection_log(
            frame=frame,
            masks=masks,
            candidates=candidates,
            vlm_scene_graph=vlm_scene_graph,
            objects=objects,
            relations=relations,
            match_summary=match_summary,
        )
        self.last_result = result
        return result

    def update_memory(self, result: PerceptionResult) -> PerceptionResult:
        if self.pending_debug is None:
            raise RuntimeError("SAMJAM update_memory requires a preceding detect call")
        self._write_samjam_outputs(result)
        self.last_result = result
        return result

    def _write_samjam_detection_log(
        self,
        frame: FrameObservation,
        masks: List[Dict[str, Any]],
        candidates: List[MaskCandidate],
        vlm_scene_graph: Dict[str, Any],
        objects: List[PerceivedObject],
        relations: List[PerceivedRelation],
        match_summary: Dict[str, Any],
    ) -> None:
        if not _debug_matching_enabled():
            return
        max_items = _env_int("ISBENCH_SCENE_GRAPH_DEBUG_MAX_ITEMS", 40)
        local_frame_idx = len(self.samjam_frame_indices) - 1
        lines = [
            "[ISBench][SAMJAM] "
            f"frame={frame.frame_index} local_frame={local_frame_idx} "
            f"raw_masks={len(masks)} candidates={len(candidates)} "
            f"vlm_objects={len(vlm_scene_graph.get('objects', []))} "
            f"matched={match_summary.get('matched_object_count', 0)} "
            f"unmatched_vlm={match_summary.get('unmatched_vlm_object_count', 0)} "
            f"unmatched_masks={match_summary.get('unmatched_mask_count', 0)}"
        ]

        for vlm_index, vlm_obj in enumerate(vlm_scene_graph.get("objects", [])[:max_items]):
            lines.append(
                "[ISBench][SAMJAM][vlm_bbox] "
                f"idx={vlm_index} id={vlm_obj.get('id', vlm_index)} "
                f"name={vlm_obj.get('name')} "
                f"bbox={_vlm_bbox_to_xyxy(vlm_obj.get('bbox'), frame.rgb.shape)} "
                f"is_moving={vlm_obj.get('is_moving', False)} "
                f"is_hand={vlm_obj.get('is_hand', False)}"
            )

        for candidate in candidates[:max_items]:
            lines.append(
                "[ISBench][SAMJAM][mask_candidate] "
                f"idx={candidate.index} bbox={candidate.bbox} "
                f"area={candidate.attributes.get('mask_area')} "
                f"pred_iou={candidate.attributes.get('predicted_iou')} "
                f"stability={candidate.attributes.get('stability_score')}"
            )

        for detail in match_summary.get("match_details", [])[:max_items]:
            lines.append(
                "[ISBench][SAMJAM][bbox_mask_match] "
                f"vlm_id={detail.get('vlm_id')} "
                f"vlm_name={detail.get('vlm_name')} "
                f"native_id={detail.get('best_native_id')} "
                f"accepted={detail.get('accepted')} "
                f"iou={detail.get('best_iou')} "
                f"score={detail.get('best_score')} "
                f"accept_reason={detail.get('accept_reason')} "
                f"vlm_coverage={detail.get('vlm_coverage')} "
                f"mask_coverage={detail.get('mask_coverage')} "
                f"area_ratio={detail.get('area_ratio')} "
                f"center_in_mask={detail.get('center_in_sam_mask')} "
                f"vlm_bbox={detail.get('vlm_bbox')} "
                f"sam_bbox={detail.get('sam_bbox')} "
                f"mask_area={detail.get('mask_area')} "
                f"reason={detail.get('reason')}"
            )

        for obj in objects[:max_items]:
            lines.append(
                "[ISBench][SAMJAM][object] "
                f"id={obj.object_id} name={obj.name} bbox={obj.bbox} "
                f"mask_area={obj.attributes.get('mask_area')} "
                f"visible={obj.attributes.get('currently_visible')} "
                f"track={obj.attributes.get('tracking')}"
            )

        for rel in relations[:max_items]:
            lines.append(
                "[ISBench][SAMJAM][relation] "
                f"{rel.source_id} -[{rel.relation}]-> {rel.target_id}"
            )
        _append_debug_log(lines)

    def _reset_native_state(self) -> None:
        if self._native_video_tmp is not None:
            self._native_video_tmp.cleanup()
        self._native_video_tmp = None
        self._native_video_dir = None
        self.samjam_total_objs: Dict[int, Any] = {}
        self.samjam_cur_objs: Dict[int, Any] = {}
        self.samjam_rels: Dict[str, str] = {}
        self.samjam_next_id = 0
        self.samjam_frame_indices: List[int] = []

    def _update_native_samjam(
        self,
        frame: FrameObservation,
        vlm_scene_graph: Dict[str, Any],
        candidates: List[MaskCandidate],
    ) -> Dict[str, Any]:
        local_frame_idx = len(self.samjam_frame_indices) - 1
        if local_frame_idx == 0:
            initial_objs = self._samjam_objects_from_candidates(candidates, local_frame_idx)
            matched_objs, rels, summary = self._match_samjam_objects(
                vlm_scene_graph,
                initial_objs,
                local_frame_idx,
                frame.rgb.shape,
            )
            self.samjam_total_objs = dict(matched_objs)
            self.samjam_cur_objs = dict(matched_objs)
            self.samjam_rels = dict(rels)
            return summary

        propagated_objs, propagated_region = self._propagate_native_masks(
            previous_local_frame_idx=local_frame_idx - 1,
            current_local_frame_idx=local_frame_idx,
            image_shape=frame.rgb.shape,
        )
        propagated_native_ids = set(propagated_objs)
        sampled_objs = self._samjam_objects_from_candidates(
            candidates,
            local_frame_idx,
            exclude_region=propagated_region,
        )
        next_objs = {**propagated_objs, **sampled_objs}
        matched_objs, next_rels, summary = self._match_samjam_objects(
            vlm_scene_graph,
            next_objs,
            local_frame_idx,
            frame.rgb.shape,
            propagated_native_ids=propagated_native_ids,
        )

        next_cur_objs = {}
        for native_id, matched_obj in matched_objs.items():
            if native_id not in self.samjam_total_objs:
                self.samjam_total_objs[native_id] = matched_obj
            else:
                total_obj = self.samjam_total_objs[native_id]
                total_obj.name = matched_obj.name
                total_obj.is_hand = getattr(matched_obj, "is_hand", False)
                if (
                    getattr(total_obj, "is_moving", False)
                    or getattr(matched_obj, "is_moving", False)
                ):
                    self._remove_native_relations(native_id)
                    total_obj.is_moving = getattr(matched_obj, "is_moving", False)
                    if getattr(matched_obj, "is_moving", False):
                        total_obj.is_moved = True
                if local_frame_idx in matched_obj.frames:
                    total_obj.frames[local_frame_idx] = matched_obj.frames[local_frame_idx]
            next_cur_objs[native_id] = self.samjam_total_objs[native_id]

        self.samjam_cur_objs = next_cur_objs
        self.samjam_rels.update(next_rels)
        return summary

    def _samjam_objects_from_candidates(
        self,
        candidates: List[MaskCandidate],
        local_frame_idx: int,
        exclude_region: Optional[np.ndarray] = None,
    ) -> Dict[int, Any]:
        objects = {}
        for candidate in candidates:
            mask = np.asarray(candidate.mask, dtype=bool)
            if exclude_region is not None and mask.any():
                overlap_ratio = float(np.logical_and(mask, exclude_region).sum()) / float(mask.sum())
                if overlap_ratio >= 0.5:
                    continue
            obj = self._new_samjam_object()
            obj.add_frame_seg(local_frame_idx, mask, [float(value) for value in candidate.bbox])
            if candidate.attributes.get("box_prompt"):
                obj.box_prompt_vlm_id = candidate.attributes.get("box_prompt_vlm_id")
                obj.box_prompt_frame_index = local_frame_idx
            native_id = self.samjam_next_id
            objects[native_id] = obj
            self.samjam_next_id += 1
        return objects

    def _propagate_native_masks(
        self,
        previous_local_frame_idx: int,
        current_local_frame_idx: int,
        image_shape: Tuple[int, ...],
    ) -> Tuple[Dict[int, Any], np.ndarray]:
        height, width = int(image_shape[0]), int(image_shape[1])
        propagated_region = np.zeros((height, width), dtype=bool)
        if not self.samjam_cur_objs:
            return {}, propagated_region

        predictor = self._ensure_video_predictor()
        import torch

        autocast = (
            torch.autocast("cuda", dtype=torch.bfloat16)
            if predictor.device.type == "cuda"
            else contextlib.nullcontext()
        )
        with _maybe_suppress_vendor_output(), autocast:
            inference_state = predictor.init_state(
                video_path=str(self._native_video_dir),
                offload_video_to_cpu=True,
                offload_state_to_cpu=True,
            )
            try:
                for native_id, obj in self.samjam_cur_objs.items():
                    if previous_local_frame_idx not in obj.frames:
                        continue
                    predictor.add_new_mask(
                        inference_state=inference_state,
                        frame_idx=previous_local_frame_idx,
                        obj_id=native_id,
                        mask=np.asarray(obj.frames[previous_local_frame_idx]["seg"], dtype=bool),
                    )

                next_objs = {}
                for out_frame_idx, out_obj_ids, out_mask_logits in predictor.propagate_in_video(
                    inference_state,
                    start_frame_idx=current_local_frame_idx,
                    max_frame_num_to_track=0,
                ):
                    if out_frame_idx != current_local_frame_idx:
                        continue
                    for index, out_obj_id in enumerate(out_obj_ids):
                        out_mask = (
                            out_mask_logits[index] > 0.0
                        ).cpu().numpy().reshape((height, width))
                        if not out_mask.any():
                            continue
                        bbox = bbox_from_mask(out_mask)
                        if bbox is None or out_obj_id not in self.samjam_total_objs:
                            continue
                        propagated_region |= out_mask
                        self.samjam_total_objs[out_obj_id].add_frame_seg(
                            out_frame_idx,
                            out_mask,
                            bbox,
                        )
                        obj = self._new_samjam_object()
                        total_obj = self.samjam_total_objs[out_obj_id]
                        obj.name = total_obj.name
                        obj.is_hand = total_obj.is_hand
                        obj.is_moving = getattr(total_obj, "is_moving", False)
                        obj.is_moved = getattr(total_obj, "is_moved", False)
                        obj.box_prompt_vlm_id = getattr(total_obj, "box_prompt_vlm_id", None)
                        obj.box_prompt_frame_index = getattr(
                            total_obj, "box_prompt_frame_index", None
                        )
                        obj.add_frame_seg(out_frame_idx, out_mask, bbox)
                        next_objs[out_obj_id] = obj
                    break
                return next_objs, propagated_region
            finally:
                try:
                    predictor.reset_state(inference_state)
                except Exception:
                    pass

    def _match_samjam_objects(
        self,
        vlm_scene_graph: Dict[str, Any],
        samjam_objs: Dict[int, Any],
        local_frame_idx: int,
        image_shape: Tuple[int, ...],
        propagated_native_ids: Optional[set[int]] = None,
    ) -> Tuple[Dict[int, Any], Dict[str, str], Dict[str, Any]]:
        id_map = {}
        matched_objs = {}
        used_native_ids = set()
        propagated_native_ids = propagated_native_ids or set()
        unmatched_vlm_ids = []
        rejected_vlm_objects = []
        match_details = []
        match_threshold = _env_float("ISBENCH_SAMJAM_NATIVE_MATCH_IOU", 0.25)

        for vlm_index, vlm_obj in enumerate(vlm_scene_graph.get("objects", [])):
            vlm_bbox = _vlm_bbox_to_xyxy(vlm_obj.get("bbox"), image_shape)
            vlm_id = vlm_obj.get("id", vlm_index)
            if vlm_bbox is None:
                unmatched_vlm_ids.append(vlm_id)
                rejected_vlm_objects.append(
                    {"id": vlm_id, "name": vlm_obj.get("name"), "reason": "invalid_bbox"}
                )
                match_details.append(
                    {
                        "vlm_id": vlm_id,
                        "vlm_name": vlm_obj.get("name"),
                        "vlm_bbox": None,
                        "best_native_id": None,
                        "best_iou": 0.0,
                        "accepted": False,
                        "reason": "invalid_bbox",
                    }
                )
                continue

            best_iou = 0.0
            best_score = 0.0
            best_metrics: Dict[str, Any] = {}
            best_accept_reason = "low_iou"
            best_accepted = False
            best_propagated_match = False
            best_obj_id = None
            best_sam_obj = None
            for native_id, sam_obj in samjam_objs.items():
                if native_id in used_native_ids:
                    continue
                if (
                    getattr(sam_obj, "box_prompt_frame_index", None) == local_frame_idx
                    and getattr(sam_obj, "box_prompt_vlm_id", None) != str(vlm_id)
                ):
                    continue
                frame_data = sam_obj.frames.get(local_frame_idx)
                if frame_data is None:
                    continue
                metrics = _bbox_match_metrics(
                    vlm_bbox,
                    frame_data.get("bbox"),
                    frame_data.get("seg"),
                )
                accepted, accept_reason, score = _bbox_match_decision(
                    metrics,
                    match_threshold,
                )
                iou = float(metrics.get("iou") or 0.0)
                propagated_match = accepted and native_id in propagated_native_ids
                if (
                    best_sam_obj is None
                    or (accepted and not best_accepted)
                    or (
                        accepted == best_accepted
                        and (
                            (propagated_match and not best_propagated_match)
                            or (
                                propagated_match == best_propagated_match
                                and (
                                    score > best_score
                                    or (score == best_score and iou >= best_iou)
                                )
                            )
                        )
                    )
                ):
                    best_iou = iou
                    best_score = score
                    best_metrics = metrics
                    best_accept_reason = accept_reason
                    best_accepted = accepted
                    best_propagated_match = propagated_match
                    best_obj_id = native_id
                    best_sam_obj = sam_obj
            if best_sam_obj is None or not best_accepted:
                best_frame = (
                    {}
                    if best_sam_obj is None
                    else best_sam_obj.frames.get(local_frame_idx, {})
                )
                unmatched_vlm_ids.append(vlm_id)
                rejected_vlm_objects.append(
                    {
                        "id": vlm_id,
                        "name": vlm_obj.get("name"),
                        "reason": "low_iou",
                        "best_iou": float(best_iou),
                        "best_score": float(best_score),
                        "accept_reason": best_accept_reason,
                        **best_metrics,
                    }
                )
                match_details.append(
                    {
                        "vlm_id": vlm_id,
                        "vlm_name": vlm_obj.get("name"),
                        "vlm_bbox": vlm_bbox,
                        "best_native_id": best_obj_id,
                        "best_iou": float(best_iou),
                        "best_score": float(best_score),
                        "accept_reason": best_accept_reason,
                        "sam_bbox": best_frame.get("bbox"),
                        "_debug_mask": best_frame.get("seg"),
                        "mask_area": int(np.asarray(best_frame.get("seg"), dtype=bool).sum())
                        if best_frame.get("seg") is not None
                        else 0,
                        "accepted": False,
                        "reason": "low_iou",
                        **best_metrics,
                    }
                )
                continue

            try:
                vlm_id_int = int(vlm_id)
            except (TypeError, ValueError):
                vlm_id_int = vlm_index
            raw_name = str(vlm_obj.get("name") or f"object_{vlm_id_int}")
            raw_is_hand = bool(vlm_obj.get("is_hand", False))
            best_sam_obj.name = _canonical_object_name(raw_name, raw_is_hand)
            best_sam_obj.is_hand = bool(
                raw_is_hand and _env_bool("ISBENCH_SAMJAM_ALLOW_HUMAN_HANDS", False)
            )
            best_sam_obj.is_moving = bool(vlm_obj.get("is_moving", False))
            if best_sam_obj.is_moving:
                best_sam_obj.is_moved = True
            id_map[vlm_id_int] = best_obj_id
            matched_objs[best_obj_id] = best_sam_obj
            used_native_ids.add(best_obj_id)
            best_frame = best_sam_obj.frames.get(local_frame_idx, {})
            match_details.append(
                {
                    "vlm_id": vlm_id,
                    "vlm_name": raw_name,
                    "canonical_name": best_sam_obj.name,
                    "vlm_bbox": vlm_bbox,
                    "best_native_id": best_obj_id,
                    "best_iou": float(best_iou),
                    "best_score": float(best_score),
                    "accept_reason": best_accept_reason,
                    "sam_bbox": best_frame.get("bbox"),
                    "_debug_mask": best_frame.get("seg"),
                    "mask_area": int(np.asarray(best_frame.get("seg"), dtype=bool).sum())
                    if best_frame.get("seg") is not None
                    else 0,
                    "accepted": True,
                    "reason": "matched",
                    **best_metrics,
                }
            )

        rels = {}
        for rel in vlm_scene_graph.get("relationships", []):
            source_id = id_map.get(rel.get("subj_id"))
            target_id = id_map.get(rel.get("obj_id"))
            predicate = str(rel.get("predicate") or "").strip()
            if source_id is None or target_id is None or not predicate:
                continue
            rels[f"{source_id},{target_id}"] = predicate

        return (
            matched_objs,
            rels,
            {
                "matched_object_count": len(matched_objs),
                "unmatched_vlm_object_count": len(unmatched_vlm_ids),
                "unmatched_mask_count": max(0, len(samjam_objs) - len(matched_objs)),
                "unmatched_vlm_ids": unmatched_vlm_ids,
                "rejected_vlm_objects": rejected_vlm_objects,
                "match_details": match_details,
            },
        )

    def _remove_native_relations(self, native_id: int) -> None:
        for key in list(self.samjam_rels):
            source_id, target_id = key.split(",", 1)
            if int(source_id) == int(native_id) or int(target_id) == int(native_id):
                self.samjam_rels.pop(key, None)

    def _native_objects_and_relations(
        self,
        frame: FrameObservation,
    ) -> Tuple[List[PerceivedObject], List[PerceivedRelation]]:
        local_frame_idx = len(self.samjam_frame_indices) - 1
        objects = [
            self._perceived_from_samjam_object(native_id, obj, local_frame_idx)
            for native_id, obj in sorted(self.samjam_total_objs.items())
        ]
        relations = []
        object_ids = {obj.object_id for obj in objects}
        for key, predicate in sorted(self.samjam_rels.items()):
            try:
                source_native, target_native = [int(value) for value in key.split(",", 1)]
            except ValueError:
                continue
            source_id = f"samjam_object:{source_native}"
            target_id = f"samjam_object:{target_native}"
            if source_id not in object_ids or target_id not in object_ids:
                continue
            relations.append(
                PerceivedRelation(
                    source_id=source_id,
                    target_id=target_id,
                    relation=str(predicate),
                    confidence=1.0,
                    source=f"{self.name}:native_vlm",
                )
            )
        return objects, relations

    def _perceived_from_samjam_object(
        self,
        native_id: int,
        obj: Any,
        local_frame_idx: int,
    ) -> PerceivedObject:
        frame_data = obj.frames.get(local_frame_idx)
        visible = frame_data is not None
        if frame_data is None and obj.frames:
            latest_frame_idx = max(obj.frames)
            frame_data = obj.frames[latest_frame_idx]
        else:
            latest_frame_idx = local_frame_idx
        mask = None if frame_data is None else np.asarray(frame_data.get("seg"), dtype=bool)
        bbox = None if frame_data is None else [float(value) for value in frame_data.get("bbox")]
        mask_area = 0 if mask is None else int(mask.sum())
        name = str(getattr(obj, "name", "") or f"object_{native_id}")
        return PerceivedObject(
            object_id=f"samjam_object:{native_id}",
            name=name,
            category=_category_from_name(name),
            bbox=bbox,
            mask=mask if visible else None,
            position=None,
            room_id="unknown_room",
            confidence=1.0,
            attributes={
                "source": "samjam_native",
                "samjam_id": native_id,
                "currently_visible": visible,
                "latest_local_frame_index": latest_frame_idx,
                "external_frame_index": self.samjam_frame_indices[local_frame_idx],
                "is_hand": bool(getattr(obj, "is_hand", False)),
                "is_moving": bool(getattr(obj, "is_moving", False)),
                "is_moved": bool(getattr(obj, "is_moved", False)),
                "mask_area": mask_area,
                "tracking": "sam2_video_predictor",
            },
        )

    def _new_samjam_object(self):
        if self.samjam_object_type is None:
            vendor_root = repo_root() / "og_ego_prim" / "scene_graph" / "vendor" / "samjam"
            insert_sys_path([vendor_root])
            from Object import Object

            self.samjam_object_type = Object
        return self.samjam_object_type()

    def _store_native_frame(self, frame: FrameObservation) -> None:
        if self._native_video_dir is None:
            if self.output_writer is not None:
                self._native_video_dir = self.output_writer.output_dir / "native_video_frames"
            else:
                self._native_video_tmp = tempfile.TemporaryDirectory(prefix="isbench_samjam_")
                self._native_video_dir = Path(self._native_video_tmp.name)
            self._native_video_dir.mkdir(parents=True, exist_ok=True)
        local_frame_idx = len(self.samjam_frame_indices)
        self.samjam_frame_indices.append(frame.frame_index)
        image = np.asarray(frame.rgb)[:, :, :3]
        if image.dtype != np.uint8:
            if image.max(initial=0) <= 1.0:
                image = image * 255.0
            image = np.clip(image, 0, 255).astype(np.uint8)
        image_path = self._native_video_dir / f"{local_frame_idx:06d}.jpg"
        import cv2

        ok = cv2.imwrite(str(image_path), np.ascontiguousarray(image[:, :, ::-1]))
        if not ok:
            raise RuntimeError(f"cv2.imwrite returned False for {image_path}")

    def _ensure_video_predictor(self):
        if self.video_predictor is not None:
            return self.video_predictor

        vendor_root = repo_root() / "og_ego_prim" / "scene_graph" / "vendor" / "samjam"
        insert_sys_path([vendor_root])
        checkpoint = model_root(self.scene_graph_config) / "samjam" / "sam2.1_hiera_large.pt"
        config = "configs/sam2.1/sam2.1_hiera_l.yaml"
        ensure_path_exists(checkpoint, "SAM2 checkpoint")

        try:
            import torch
            import iopath.common.file_io  # noqa: F401
            from sam2.build_sam import build_sam2_video_predictor
        except ImportError as exc:
            raise ImportError(
                "SAMJAM video tracking requires vendored sam2 plus hydra-core / "
                "omegaconf / torch / iopath."
            ) from exc

        device = self.scene_graph_config.device or (
            "cuda" if torch.cuda.is_available() else "cpu"
        )
        target_device = torch.device(device)
        model_image_size = int(self.scene_graph_config.image_size[0])
        with _maybe_suppress_vendor_output(), torch.device("cpu"):
            predictor = build_sam2_video_predictor(
                config,
                str(checkpoint),
                device="cpu",
                hydra_overrides_extra=[
                    f"++model.image_size={model_image_size}",
                    "++model.image_encoder.neck.position_encoding.warmup_cache=false",
                    "++model.memory_encoder.position_encoding.warmup_cache=false",
                ],
            )
        self.video_predictor = predictor.to(
            device=target_device,
            dtype=torch.bfloat16 if target_device.type == "cuda" else torch.float32,
        )
        return self.video_predictor

    def _ensure_vlm_adapter(self) -> SAMJAMVLMAdapter:
        if self.vlm_adapter is None:
            self.vlm_adapter = SAMJAMVLMAdapter(self.scene_graph_config)
            self.vlm_adapter.object_goal = self.object_goal
            self.vlm_adapter.set_allowed_object_names(self.task_categories)
        return self.vlm_adapter

    def _ensure_mask_generator(self):
        if self.mask_generator is not None:
            return self.mask_generator

        vendor_root = repo_root() / "og_ego_prim" / "scene_graph" / "vendor" / "samjam"
        insert_sys_path([vendor_root])

        try:
            import iopath.common.file_io  # noqa: F401
            from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
        except ImportError as exc:
            raise ImportError(
                "SAMJAM backend requires vendored sam2 plus hydra-core / omegaconf / "
                "torch / iopath. Install requirements-scene-graph.txt and the local "
                "SAM2 package."
            ) from exc

        model = self._ensure_video_predictor()
        points_per_side = self.scene_graph_config.option_int(
            "ISBENCH_SAMJAM_POINTS_PER_SIDE", 32
        )
        self.mask_generator = SAM2AutomaticMaskGenerator(
            model=model,
            points_per_side=points_per_side,
            points_per_batch=self.scene_graph_config.option_int(
                "ISBENCH_SAMJAM_POINTS_PER_BATCH", 64
            ),
            stability_score_thresh=self.scene_graph_config.option_float(
                "ISBENCH_SAMJAM_STABILITY_THRESH", 0.8
            ),
            crop_n_layers=self.scene_graph_config.option_int(
                "ISBENCH_SAMJAM_CROP_N_LAYERS", 1
            ),
            crop_n_points_downscale_factor=2,
            use_m2m=True,
        )
        return self.mask_generator

    def _mask_candidates(
        self,
        frame: FrameObservation,
        masks: List[Dict[str, Any]],
        vlm_scene_graph: Optional[Dict[str, Any]] = None,
    ) -> List[MaskCandidate]:
        candidates = []
        max_masks = _env_int("ISBENCH_SAMJAM_MAX_MASKS", 40)
        sorted_masks = sorted(
            masks,
            key=lambda item: item.get("predicted_iou", item.get("stability_score", 0.0)) or 0.0,
            reverse=True,
        )
        vlm_bboxes = tuple(
            bbox
            for obj in (vlm_scene_graph or {}).get("objects", [])
            if (bbox := _vlm_bbox_to_xyxy(obj.get("bbox"), frame.rgb.shape)) is not None
        )
        for index, mask_info in enumerate(sorted_masks):
            mask = np.asarray(mask_info.get("segmentation"), dtype=bool)
            bbox = mask_info.get("bbox")
            if bbox is not None and len(bbox) == 4:
                x, y, w, h = [float(v) for v in bbox]
                bbox_xyxy = [x, y, x + w, y + h]
            else:
                bbox_xyxy = bbox_from_mask(mask)
            if bbox_xyxy is None:
                continue
            if index >= max_masks and not any(
                _bbox_intersection_area(bbox_xyxy, vlm_bbox) > 0.0
                for vlm_bbox in vlm_bboxes
            ):
                continue
            confidence = float(mask_info.get("predicted_iou", mask_info.get("stability_score", 1.0)) or 1.0)
            candidates.append(
                MaskCandidate(
                    index=index,
                    mask=mask,
                    bbox=bbox_xyxy,
                    position=None,
                    room_id="unknown_room",
                    confidence=confidence,
                    attributes={
                        "source": self.name,
                        "mask_area": int(mask_info.get("area", int(mask.sum()))),
                        "stability_score": float(mask_info.get("stability_score", 0.0) or 0.0),
                        "predicted_iou": float(mask_info.get("predicted_iou", 0.0) or 0.0),
                    },
                )
            )
        return candidates

    def _box_prompt_candidates(
        self,
        frame: FrameObservation,
        vlm_scene_graph: Dict[str, Any],
        candidates: List[MaskCandidate],
    ) -> List[MaskCandidate]:
        match_threshold = _env_float("ISBENCH_SAMJAM_NATIVE_MATCH_IOU", 0.25)
        missing_boxes = []
        for obj in vlm_scene_graph.get("objects", []):
            bbox = _vlm_bbox_to_xyxy(obj.get("bbox"), frame.rgb.shape)
            if bbox is None:
                continue
            if any(
                _bbox_match_decision(
                    _bbox_match_metrics(bbox, candidate.bbox, candidate.mask),
                    match_threshold,
                )[0]
                for candidate in candidates
            ):
                continue
            missing_boxes.append((obj.get("id"), bbox))
        if not missing_boxes:
            return candidates

        predictor = self._ensure_mask_generator().predictor
        import torch

        autocast = (
            torch.autocast("cuda", dtype=torch.bfloat16)
            if predictor.device.type == "cuda"
            else contextlib.nullcontext()
        )
        next_index = max((candidate.index for candidate in candidates), default=-1) + 1
        with _maybe_suppress_vendor_output(), autocast:
            predictor.set_image(frame.rgb)
            try:
                for vlm_id, bbox in missing_boxes:
                    masks, scores, _ = predictor.predict(
                        box=np.asarray(bbox, dtype=np.float32),
                        point_coords=np.asarray([_bbox_center(bbox)], dtype=np.float32),
                        point_labels=np.asarray([1], dtype=np.int32),
                        multimask_output=True,
                    )
                    best = None
                    for mask, score in zip(masks, scores):
                        mask = np.asarray(mask, dtype=bool)
                        mask_bbox = bbox_from_mask(mask)
                        if mask_bbox is None:
                            continue
                        metrics = _bbox_match_metrics(bbox, mask_bbox, mask)
                        accepted, _, match_score = _bbox_match_decision(
                            metrics,
                            match_threshold,
                        )
                        if not accepted:
                            continue
                        candidate = (match_score, float(score), mask, mask_bbox)
                        if best is None or candidate[:2] > best[:2]:
                            best = candidate
                    if best is None:
                        continue
                    _, score, mask, mask_bbox = best
                    candidates.append(
                        MaskCandidate(
                            index=next_index,
                            mask=mask,
                            bbox=mask_bbox,
                            position=None,
                            room_id="unknown_room",
                            confidence=score,
                            attributes={
                                "source": f"{self.name}:box_prompt",
                                "mask_area": int(mask.sum()),
                                "predicted_iou": score,
                                "box_prompt": True,
                                "box_prompt_vlm_id": str(vlm_id),
                            },
                        )
                    )
                    next_index += 1
            finally:
                predictor.reset_predictor()
        return candidates

    def _filter_vlm_scene_graph(self, scene_graph: Dict[str, Any]) -> Dict[str, Any]:
        if not self.task_categories:
            return scene_graph
        objects = [
            obj
            for obj in scene_graph.get("objects", [])
            if _canonical_object_name(obj.get("name")) in self.task_categories
        ]
        object_ids = {obj.get("id") for obj in objects}
        return {
            "objects": objects,
            "relationships": [
                relation
                for relation in scene_graph.get("relationships", [])
                if relation.get("subj_id") in object_ids and relation.get("obj_id") in object_ids
            ],
        }

    def _objects_from_masks(self, frame: FrameObservation, masks: List[Dict[str, Any]]) -> List[PerceivedObject]:
        objects = []
        for candidate in self._mask_candidates(frame, masks):
            objects.append(
                PerceivedObject(
                    object_id=f"frame_mask:{frame.frame_index}:{candidate.index}",
                    name=f"sam_mask_{candidate.index}",
                    category="sam_mask",
                    bbox=candidate.bbox,
                    mask=candidate.mask,
                    position=candidate.position,
                    room_id=candidate.room_id,
                    confidence=candidate.confidence,
                    attributes={
                        **candidate.attributes,
                        "source": "sam2_unmatched",
                        "transient": True,
                        "currently_visible": True,
                        "mask_index": candidate.index,
                    },
                )
            )
        return objects

    def _objects_from_vlm_and_masks(
        self,
        frame: FrameObservation,
        vlm_scene_graph: Dict[str, Any],
        masks: List[Dict[str, Any]],
    ) -> Tuple[List[PerceivedObject], List[PerceivedRelation], Dict[str, Any], List[MaskCandidate]]:
        candidates = self._mask_candidates(frame, masks)
        used_candidate_indices = set()
        objects = []
        vlm_id_to_object_id = {}
        match_threshold = _env_float("ISBENCH_SAMJAM_MATCH_IOU", 0.25)
        unmatched_vlm_ids = []
        rejected_vlm_objects = []
        match_details = []

        for vlm_index, vlm_obj in enumerate(vlm_scene_graph.get("objects", [])):
            vlm_bbox = _vlm_bbox_to_xyxy(vlm_obj.get("bbox"), frame.rgb.shape)
            if vlm_bbox is None:
                unmatched_vlm_ids.append(vlm_obj.get("id", vlm_index))
                rejected_vlm_objects.append(
                    {
                        "id": vlm_obj.get("id", vlm_index),
                        "name": vlm_obj.get("name"),
                        "reason": "invalid_bbox",
                    }
                )
                match_details.append(
                    {
                        "vlm_id": vlm_obj.get("id", vlm_index),
                        "vlm_name": vlm_obj.get("name"),
                        "vlm_bbox": None,
                        "best_native_id": None,
                        "best_iou": 0.0,
                        "accepted": False,
                        "reason": "invalid_bbox",
                    }
                )
                continue

            best_candidate = None
            best_iou = 0.0
            best_score = 0.0
            best_metrics: Dict[str, Any] = {}
            best_accept_reason = "low_iou"
            best_accepted = False
            for candidate in candidates:
                if candidate.index in used_candidate_indices:
                    continue
                metrics = _bbox_match_metrics(vlm_bbox, candidate.bbox, candidate.mask)
                accepted, accept_reason, score = _bbox_match_decision(metrics, match_threshold)
                iou = float(metrics.get("iou") or 0.0)
                if (
                    best_candidate is None
                    or (accepted and not best_accepted)
                    or (
                        accepted == best_accepted
                        and (score > best_score or (score == best_score and iou > best_iou))
                    )
                ):
                    best_iou = iou
                    best_score = score
                    best_metrics = metrics
                    best_accept_reason = accept_reason
                    best_accepted = accepted
                    best_candidate = candidate

            if best_candidate is None or not best_accepted:
                unmatched_vlm_ids.append(vlm_obj.get("id", vlm_index))
                rejected_vlm_objects.append(
                    {
                        "id": vlm_obj.get("id", vlm_index),
                        "name": vlm_obj.get("name"),
                        "reason": "low_iou",
                        "best_iou": float(best_iou),
                        "best_score": float(best_score),
                        "accept_reason": best_accept_reason,
                        **best_metrics,
                    }
                )
                match_details.append(
                    {
                        "vlm_id": vlm_obj.get("id", vlm_index),
                        "vlm_name": vlm_obj.get("name"),
                        "vlm_bbox": vlm_bbox,
                        "best_native_id": None if best_candidate is None else best_candidate.index,
                        "best_iou": float(best_iou),
                        "best_score": float(best_score),
                        "accept_reason": best_accept_reason,
                        "sam_bbox": None if best_candidate is None else best_candidate.bbox,
                        "_debug_mask": None if best_candidate is None else best_candidate.mask,
                        "mask_area": None if best_candidate is None else best_candidate.attributes.get("mask_area"),
                        "accepted": False,
                        "reason": "low_iou",
                        **best_metrics,
                    }
                )
                continue

            used_candidate_indices.add(best_candidate.index)
            vlm_id = int(vlm_obj.get("id", vlm_index))
            object_id = f"frame_vlm:{frame.frame_index}:{vlm_id}"
            raw_name = str(vlm_obj.get("name") or f"object_{vlm_id}")
            raw_is_hand = bool(vlm_obj.get("is_hand", False))
            name = _canonical_object_name(raw_name, raw_is_hand)
            is_moving = bool(vlm_obj.get("is_moving", False))
            objects.append(
                PerceivedObject(
                    object_id=object_id,
                    name=name,
                    category=_category_from_name(name),
                    bbox=best_candidate.bbox,
                    mask=best_candidate.mask,
                    position=best_candidate.position,
                    room_id=best_candidate.room_id,
                    confidence=float(best_candidate.confidence * max(best_score, best_iou, 0.01)),
                    attributes={
                        **best_candidate.attributes,
                        "source": "samjam_vlm_sam2_match",
                        "vlm_object_id": vlm_id,
                        "vlm_bbox": vlm_bbox,
                        "vlm_name": name,
                        "vlm_raw_name": raw_name,
                        "mask_index": best_candidate.index,
                        "match_iou": float(best_iou),
                        "match_score": float(best_score),
                        "match_accept_reason": best_accept_reason,
                        "is_hand": bool(raw_is_hand and _env_bool("ISBENCH_SAMJAM_ALLOW_HUMAN_HANDS", False)),
                        "vlm_raw_is_hand": raw_is_hand,
                        "is_moving": is_moving,
                        "transient": False,
                        "currently_visible": True,
                    },
                )
            )
            match_details.append(
                {
                    "vlm_id": vlm_id,
                    "vlm_name": raw_name,
                    "canonical_name": name,
                    "vlm_bbox": vlm_bbox,
                    "best_native_id": best_candidate.index,
                    "best_iou": float(best_iou),
                    "best_score": float(best_score),
                    "accept_reason": best_accept_reason,
                    "sam_bbox": best_candidate.bbox,
                    "_debug_mask": best_candidate.mask,
                    "mask_area": best_candidate.attributes.get("mask_area"),
                    "accepted": True,
                    "reason": "matched",
                    **best_metrics,
                }
            )
            vlm_id_to_object_id[vlm_id] = object_id

        relations = []
        for rel in vlm_scene_graph.get("relationships", []):
            source_id = vlm_id_to_object_id.get(rel.get("subj_id"))
            target_id = vlm_id_to_object_id.get(rel.get("obj_id"))
            predicate = str(rel.get("predicate") or "").strip()
            if source_id is None or target_id is None or not predicate:
                continue
            relations.append(
                PerceivedRelation(
                    source_id=source_id,
                    target_id=target_id,
                    relation=predicate,
                    confidence=1.0,
                    source=f"{self.name}:vlm",
                )
            )

        if _env_bool("ISBENCH_SAMJAM_KEEP_UNMATCHED_MASKS", False):
            for candidate in candidates:
                if candidate.index in used_candidate_indices:
                    continue
                objects.append(
                    PerceivedObject(
                        object_id=f"frame_mask:{frame.frame_index}:{candidate.index}",
                        name=f"sam_mask_{candidate.index}",
                        category="sam_mask",
                        bbox=candidate.bbox,
                        mask=candidate.mask,
                        position=candidate.position,
                        room_id=candidate.room_id,
                        confidence=candidate.confidence,
                        attributes={
                            **candidate.attributes,
                            "source": "sam2_unmatched",
                            "transient": True,
                            "currently_visible": True,
                            "mask_index": candidate.index,
                        },
                    )
                )
            relations.extend(self._relations_from_overlaps(objects))

        match_summary = {
            "matched_object_count": len(vlm_id_to_object_id),
            "unmatched_vlm_object_count": len(unmatched_vlm_ids),
            "unmatched_mask_count": len(candidates) - len(used_candidate_indices),
            "unmatched_vlm_ids": unmatched_vlm_ids,
            "rejected_vlm_objects": rejected_vlm_objects,
            "match_details": match_details,
        }
        return objects, relations, match_summary, candidates

    def _write_samjam_outputs(self, result: PerceptionResult) -> None:
        if self.output_writer is None or self.pending_debug is None:
            return
        try:
            self.output_writer.write(
                frame=self.pending_debug["frame"],
                vlm_scene_graph=self.pending_debug.get("vlm_scene_graph"),
                candidates=self.pending_debug.get("candidates", []),
                objects=result.objects,
                relations=result.relations,
                match_summary=self.pending_debug.get("match_summary", {}),
                graph_objects=result.objects,
            )
            result.metadata["samjam_output_dir"] = str(self.output_writer.output_dir)
        except Exception as exc:
            result.metadata["samjam_output_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }

    def _compact_object(self, obj: PerceivedObject) -> Dict[str, Any]:
        return {
            "object_id": obj.object_id,
            "name": obj.name,
            "category": obj.category,
            "visible": bool(obj.attributes.get("currently_visible", True)),
            "position": obj.position,
            "bbox": obj.bbox,
            "room_id": obj.room_id,
            "confidence": obj.confidence,
            "attributes": dict(obj.attributes),
        }

    def _relations_from_overlaps(self, objects: List[PerceivedObject]) -> List[PerceivedRelation]:
        if not self._overlap_relations_enabled():
            return []

        threshold = self.scene_graph_config.option_float(
            "ISBENCH_SAMJAM_OVERLAP_IOU_THRESHOLD", 0.05
        )
        relations = []
        for index, source in enumerate(objects):
            for target in objects[index + 1:]:
                if _bbox_iou(source.bbox, target.bbox) <= threshold:
                    continue
                relations.append(
                    PerceivedRelation(
                        source_id=source.object_id,
                        target_id=target.object_id,
                        relation="overlaps",
                        confidence=0.5,
                        source=self.name,
                    )
                )
        return relations

    def _overlap_relations_enabled(self) -> bool:
        return self.scene_graph_config.option_bool(
            "ISBENCH_SAMJAM_ENABLE_OVERLAP_RELATIONS", False
        )

    def _vlm_enabled(self) -> bool:
        return _env_bool("ISBENCH_SAMJAM_VLM_ENABLED", True)

    def _room_graph(self, objects: List[PerceivedObject]) -> Dict[str, Any]:
        rooms: Dict[str, List[str]] = {}
        for obj in objects:
            rooms.setdefault(obj.room_id or "unknown_room", []).append(obj.object_id)
        return {
            "rooms": [
                {"id": room_id, "caption": room_id, "object_count": len(object_ids), "objects": object_ids}
                for room_id, object_ids in sorted(rooms.items())
            ]
        }

    def _group_graph(
        self,
        objects: List[PerceivedObject],
        relations: Optional[List[PerceivedRelation]] = None,
    ) -> Dict[str, Any]:
        groups = []
        by_room: Dict[str, List[PerceivedObject]] = {}
        for obj in objects:
            by_room.setdefault(obj.room_id or "unknown_room", []).append(obj)
        for room_id, room_objects in sorted(by_room.items()):
            room_relations = [
                rel
                for rel in relations or []
                if any(obj.object_id == rel.source_id for obj in room_objects)
                and any(obj.object_id == rel.target_id for obj in room_objects)
            ]
            for group_index, component in enumerate(self._relation_components(room_objects, room_relations)):
                component_objects = [obj for obj in room_objects if obj.object_id in component]
                edge_count = len(
                    [
                        rel
                        for rel in room_relations
                        if rel.source_id in component and rel.target_id in component
                    ]
                )
                groups.append(
                    {
                        "id": f"{room_id}:group:{group_index}",
                        "room": room_id,
                        "caption": ", ".join(obj.name for obj in component_objects[:8]),
                        "objects": [obj.object_id for obj in component_objects],
                        "center": self._center(component_objects),
                        "edge_count": edge_count,
                    }
                )
        return {"groups": groups}

    def _relation_components(
        self,
        objects: List[PerceivedObject],
        relations: List[PerceivedRelation],
    ) -> List[set]:
        object_ids = {obj.object_id for obj in objects}
        if not relations:
            return [set(object_ids)] if object_ids else []

        adjacency = {object_id: set() for object_id in object_ids}
        for rel in relations:
            if rel.source_id not in object_ids or rel.target_id not in object_ids:
                continue
            adjacency[rel.source_id].add(rel.target_id)
            adjacency[rel.target_id].add(rel.source_id)

        components = []
        remaining = set(object_ids)
        while remaining:
            start = remaining.pop()
            component = {start}
            stack = [start]
            while stack:
                current = stack.pop()
                for neighbor in adjacency[current]:
                    if neighbor not in remaining:
                        continue
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
            components.append(component)
        return components

    def _center(self, objects: List[PerceivedObject]) -> Optional[List[float]]:
        positions = [obj.position for obj in objects if obj.position is not None]
        if not positions:
            return None
        center = np.asarray(positions, dtype=np.float32).mean(axis=0)
        return [float(v) for v in center]

    def _compact_vlm_scene_graph(self, scene_graph: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if scene_graph is None:
            return None
        return {
            "objects": [
                {
                    "id": obj.get("id"),
                    "name": obj.get("name"),
                    "bbox": obj.get("bbox"),
                    "is_hand": obj.get("is_hand", False),
                    "is_moving": obj.get("is_moving", False),
                }
                for obj in scene_graph.get("objects", [])
            ],
            "relationships": [
                {
                    "subj_id": rel.get("subj_id"),
                    "obj_id": rel.get("obj_id"),
                    "predicate": rel.get("predicate"),
                }
                for rel in scene_graph.get("relationships", [])
            ],
        }
