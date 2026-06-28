import base64
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

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
)

OPENAI_BASE_KEY = "sk-psVLXPX5aNmSC0Wm4Bt7X4BM85izhpMMaiyfBBTtGHxqY4Tj"
OPENAI_BASE_URL = "https://llm-api.net/v1"
MODEL_TYPE = "gpt-4o-mini"

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


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


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
    if normalized in {"counter", "countertop", "kitchen_countertop"}:
        return "kitchen_counter"
    return normalized


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
        self.resized_images_dir = output_dir / "resized_images"
        self.scene_graph_dir = output_dir / "scene_graph_output"
        self.vis_dir = output_dir / "vis_output"
        for directory in (self.resized_images_dir, self.scene_graph_dir, self.vis_dir):
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
    ) -> None:
        stage = "start"
        try:
            frame_index = frame.frame_index
            stage = "save_resized_image"
            image = self._rgb_array(frame.rgb)
            image_path = self.resized_images_dir / f"{frame_index:06d}.jpg"
            self._write_rgb_image(image_path, image)

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

    def __init__(self):
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
        self.prompt: Optional[str] = None
        self.printed_request_config = False

    def generate(self, frame: FrameObservation) -> Dict[str, Any]:
        api_key = OPENAI_BASE_KEY
        base_url = OPENAI_BASE_URL
        if not api_key:
            raise RuntimeError(
                "SAMJAM VLM is enabled but OPENAI_API_KEY is not set. "
                "Set OPENAI_API_KEY or disable it with ISBENCH_SAMJAM_VLM_ENABLED=0."
            )

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("SAMJAM VLM requires the openai package") from exc

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        model = MODEL_TYPE
        self._print_request_config(base_url, model, api_key)

        completion = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._load_prompt()},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{self._encode_rgb(frame.rgb)}",
                                "detail": os.environ.get("ISBENCH_SAMJAM_VLM_IMAGE_DETAIL", "high"),
                            },
                        },
                    ],
                }
            ],
            response_format={"type": "json_object"},
            timeout=float(os.environ.get("ISBENCH_SAMJAM_VLM_TIMEOUT", "120")),
        )
        content = completion.choices[0].message.content
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        if not content:
            raise RuntimeError("SAMJAM VLM returned an empty scene graph response")
        return self._validate_scene_graph(_extract_json_object(content))

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
            base_prompt = self.prompt_path.read_text(encoding="utf-8")
            suffix = os.environ.get(
                "ISBENCH_SAMJAM_PROMPT_SUFFIX",
                (
                    "\n\nIS-Bench robot-scene constraints:\n"
                    "- The image is from a robot first-person camera in simulation.\n"
                    "- Do not label the robot arm, gripper, or tool as a human hand or person.\n"
                    "- Use stable object names with underscores, e.g. robot_arm, robot_gripper, "
                    "kitchen_counter, tennis_ball.\n"
                    "- Only use hand_left, hand_right, or person if an actual human body part is visible.\n"
                    "- Prefer task-relevant physical objects and support surfaces; avoid walls/floor "
                    "unless needed for a relationship.\n"
                ),
            )
            self.prompt = base_prompt + suffix
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
            normalized_objects.append(
                {
                    "id": obj_id,
                    "name": str(obj.get("name") or f"object_{obj_id}"),
                    "bbox": obj.get("bbox"),
                    "is_hand": bool(obj.get("is_hand", False)),
                    "is_moving": bool(obj.get("is_moving", False)),
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
            predicate = str(rel.get("predicate") or "").strip()
            if not predicate:
                continue
            normalized_relationships.append(
                {"subj_id": subj_id, "obj_id": obj_id, "predicate": predicate}
            )

        return {"objects": normalized_objects, "relationships": normalized_relationships}


class SAMJAMSAM2Backend:
    name = "samjam_sam2"

    def __init__(self, sensor_name: Optional[str] = None):
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
        self._native_video_tmp: Optional[tempfile.TemporaryDirectory] = None
        self._native_video_dir: Optional[Path] = None
        self._reset_native_state()

    def reset(self, env: Any) -> None:
        self.env = env
        self.adapter.reset()
        self.adapter.ensure_robot_sensor_modalities(env)
        self.room_lookup = room_lookup_from_env(env)
        self.pending_debug = None
        output_dir = os.environ.get("ISBENCH_SAMJAM_OUTPUT_DIR")
        self.output_writer = SAMJAMOutputWriter(Path(output_dir)) if output_dir else None
        self._reset_native_state()
        self.last_result = None

    def observe(self, env: Any) -> FrameObservation:
        return self.adapter.observe(env)

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        generator = self._ensure_mask_generator()
        self._store_native_frame(frame)
        masks = generator.generate(frame.rgb)
        vlm_enabled = self._vlm_enabled()
        vlm_scene_graph = {"objects": [], "relationships": []}
        match_summary: Dict[str, Any] = {}
        candidates = self._mask_candidates(frame, masks)
        if vlm_enabled:
            vlm_scene_graph = self._ensure_vlm_adapter().generate(frame)
            match_summary = self._update_native_samjam(frame, vlm_scene_graph, candidates)
            objects, relations = self._native_objects_and_relations(frame)
        else:
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
                "match_iou_threshold": _env_float("ISBENCH_SAMJAM_NATIVE_MATCH_IOU", 0.1),
                "rejected_vlm_objects": match_summary.get("rejected_vlm_objects", []),
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
        }
        self.last_result = result
        return result

    def update_memory(self, result: PerceptionResult) -> PerceptionResult:
        if self.pending_debug is None:
            raise RuntimeError("SAMJAM update_memory requires a preceding detect call")
        self._write_samjam_outputs(result)
        self.last_result = result
        return result

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
        inference_state = predictor.init_state(video_path=str(self._native_video_dir))
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
    ) -> Tuple[Dict[int, Any], Dict[str, str], Dict[str, Any]]:
        id_map = {}
        matched_objs = {}
        unmatched_vlm_ids = []
        rejected_vlm_objects = []
        match_threshold = _env_float("ISBENCH_SAMJAM_NATIVE_MATCH_IOU", 0.1)

        for vlm_index, vlm_obj in enumerate(vlm_scene_graph.get("objects", [])):
            vlm_bbox = _vlm_bbox_to_xyxy(vlm_obj.get("bbox"), image_shape)
            vlm_id = vlm_obj.get("id", vlm_index)
            if vlm_bbox is None:
                unmatched_vlm_ids.append(vlm_id)
                rejected_vlm_objects.append(
                    {"id": vlm_id, "name": vlm_obj.get("name"), "reason": "invalid_bbox"}
                )
                continue

            best_iou = 0.0
            best_obj_id = None
            best_sam_obj = None
            for native_id, sam_obj in samjam_objs.items():
                frame_data = sam_obj.frames.get(local_frame_idx)
                if frame_data is None:
                    continue
                iou = _bbox_iou(vlm_bbox, frame_data.get("bbox"))
                if iou >= best_iou:
                    best_iou = iou
                    best_obj_id = native_id
                    best_sam_obj = sam_obj
            if best_sam_obj is None or best_iou < match_threshold:
                unmatched_vlm_ids.append(vlm_id)
                rejected_vlm_objects.append(
                    {
                        "id": vlm_id,
                        "name": vlm_obj.get("name"),
                        "reason": "low_iou",
                        "best_iou": float(best_iou),
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
        checkpoint = model_root() / "samjam" / "sam2.1_hiera_large.pt"
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

        device = os.environ.get(
            "ISBENCH_SCENE_GRAPH_DEVICE",
            "cuda" if torch.cuda.is_available() else "cpu",
        )
        self.video_predictor = build_sam2_video_predictor(
            config,
            str(checkpoint),
            device=device,
        )
        return self.video_predictor

    def _ensure_vlm_adapter(self) -> SAMJAMVLMAdapter:
        if self.vlm_adapter is None:
            self.vlm_adapter = SAMJAMVLMAdapter()
        return self.vlm_adapter

    def _ensure_mask_generator(self):
        if self.mask_generator is not None:
            return self.mask_generator

        vendor_root = repo_root() / "og_ego_prim" / "scene_graph" / "vendor" / "samjam"
        insert_sys_path([vendor_root])

        checkpoint = model_root() / "samjam" / "sam2.1_hiera_large.pt"
        config = "configs/sam2.1/sam2.1_hiera_l.yaml"
        ensure_path_exists(checkpoint, "SAM2 checkpoint")

        try:
            import torch
            import iopath.common.file_io  # noqa: F401
            from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
            from sam2.build_sam import build_sam2
        except ImportError as exc:
            raise ImportError(
                "SAMJAM backend requires vendored sam2 plus hydra-core / omegaconf / "
                "torch / iopath. Install requirements-scene-graph.txt and the local "
                "SAM2 package."
            ) from exc

        device = os.environ.get(
            "ISBENCH_SCENE_GRAPH_DEVICE",
            "cuda" if torch.cuda.is_available() else "cpu",
        )
        model = build_sam2(config, str(checkpoint), device=device)
        points_per_side = int(os.environ.get("ISBENCH_SAMJAM_POINTS_PER_SIDE", "32"))
        self.mask_generator = SAM2AutomaticMaskGenerator(
            model=model,
            points_per_side=points_per_side,
            points_per_batch=int(os.environ.get("ISBENCH_SAMJAM_POINTS_PER_BATCH", "64")),
            stability_score_thresh=float(os.environ.get("ISBENCH_SAMJAM_STABILITY_THRESH", "0.8")),
            crop_n_layers=int(os.environ.get("ISBENCH_SAMJAM_CROP_N_LAYERS", "1")),
            crop_n_points_downscale_factor=2,
            use_m2m=True,
        )
        return self.mask_generator

    def _mask_candidates(
        self,
        frame: FrameObservation,
        masks: List[Dict[str, Any]],
    ) -> List[MaskCandidate]:
        candidates = []
        max_masks = _env_int("ISBENCH_SAMJAM_MAX_MASKS", 40)
        sorted_masks = sorted(
            masks,
            key=lambda item: item.get("predicted_iou", item.get("stability_score", 0.0)) or 0.0,
            reverse=True,
        )[:max_masks]
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
                continue

            best_candidate = None
            best_iou = 0.0
            for candidate in candidates:
                if candidate.index in used_candidate_indices:
                    continue
                iou = _bbox_iou(vlm_bbox, candidate.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_candidate = candidate

            if best_candidate is None or best_iou < match_threshold:
                unmatched_vlm_ids.append(vlm_obj.get("id", vlm_index))
                rejected_vlm_objects.append(
                    {
                        "id": vlm_obj.get("id", vlm_index),
                        "name": vlm_obj.get("name"),
                        "reason": "low_iou",
                        "best_iou": float(best_iou),
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
                    confidence=float(best_candidate.confidence * max(best_iou, 0.01)),
                    attributes={
                        **best_candidate.attributes,
                        "source": "samjam_vlm_sam2_match",
                        "vlm_object_id": vlm_id,
                        "vlm_bbox": vlm_bbox,
                        "vlm_name": name,
                        "vlm_raw_name": raw_name,
                        "mask_index": best_candidate.index,
                        "match_iou": float(best_iou),
                        "is_hand": bool(raw_is_hand and _env_bool("ISBENCH_SAMJAM_ALLOW_HUMAN_HANDS", False)),
                        "vlm_raw_is_hand": raw_is_hand,
                        "is_moving": is_moving,
                        "transient": False,
                        "currently_visible": True,
                    },
                )
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

        threshold = float(os.environ.get("ISBENCH_SAMJAM_OVERLAP_IOU_THRESHOLD", "0.05"))
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
        value = os.environ.get("ISBENCH_SAMJAM_ENABLE_OVERLAP_RELATIONS", "0")
        return value.strip().lower() in {"1", "true", "yes", "on"}

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
