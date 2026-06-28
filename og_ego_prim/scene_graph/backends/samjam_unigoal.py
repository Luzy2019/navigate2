import hashlib
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)

from .utils import insert_sys_path, repo_root, to_builtin


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _category(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return value or "object"


COARSE_VOCAB = {
    "table", "chair", "cabinet", "sofa", "bed", "window", "mirror",
    "curtain", "painting", "picture", "plant", "tv", "shelf", "desk",
    "dresser", "nightstand", "counter", "stool", "toilet", "sink",
    "bathtub", "shower", "refrigerator", "oven", "stove", "microwave",
    "trash can", "bin",
}
FINE_VOCAB = {
    "apple", "banana", "book", "bottle", "bowl", "box", "clock", "cloth",
    "cup", "fork", "knife", "lamp", "laptop", "lighter", "mug", "paper",
    "phone", "pillow", "plate", "potato", "rag", "remote", "soap", "sponge",
    "spoon", "tennis ball", "tissue box", "tofu", "tomato", "towel", "vase",
    "vegetable", "vegetables",
}
CAPTION_ALIAS = {
    "books": "book",
    "bottles": "bottle",
    "bowls": "bowl",
    "couch": "sofa",
    "countertop": "counter",
    "cups": "cup",
    "dining table": "table",
    "dining-table": "table",
    "garbage bin": "bin",
    "kitchen cabinet": "cabinet",
    "kitchen counter": "counter",
    "mugs": "mug",
    "night stand": "nightstand",
    "pillows": "pillow",
    "plates": "plate",
    "potted plant": "plant",
    "tennis_ball": "tennis ball",
    "tissue_box": "tissue box",
    "trash bin": "bin",
    "tv monitor": "tv",
    "tv_monitor": "tv",
    "windows": "window",
}
NODE_SPACE_TERMS = sorted(COARSE_VOCAB | FINE_VOCAB, key=len, reverse=True)
ATTACHABLE_COARSE_VOCAB = {
    "bed", "sofa", "table", "desk", "nightstand", "shelf", "counter",
    "sink", "bathtub", "shower", "cabinet", "dresser", "refrigerator",
    "stove", "oven", "microwave", "trash can", "bin",
}
SUPPORT_SURFACE_VOCAB = {
    "bed", "sofa", "table", "desk", "nightstand", "shelf", "counter",
    "stove", "oven", "microwave",
}
CONTAINER_COARSE_VOCAB = {
    "cabinet", "dresser", "refrigerator", "sink", "bathtub", "shower",
    "trash can", "bin",
}
COARSE_NEAR_OBSTACLE_VOCAB = {
    "table", "chair", "cabinet", "sofa", "bed", "shelf", "desk",
    "dresser", "nightstand", "counter", "stool", "toilet", "sink",
    "bathtub", "shower", "refrigerator", "oven", "stove", "microwave",
    "plant", "trash can", "bin",
}
FINE_PARENT_RELATION_PRIORITY = {"on": 4, "in": 3, "attach to": 2, "above": 1}
ALLOWED_LIFELONG_RELATIONS = {"near", "on", "in", "above", "attach to"}


def _caption_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace(".n.01", "")
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_caption(caption: Any) -> str:
    text = _caption_text(caption)
    if not text:
        return "object"
    text = CAPTION_ALIAS.get(text, text)
    if text in COARSE_VOCAB or text in FINE_VOCAB:
        return text
    matches = []
    for term in NODE_SPACE_TERMS:
        index = text.find(term)
        if index >= 0:
            matches.append((index, -len(term), term))
    if matches:
        matches.sort()
        return matches[0][2]
    return text


def _is_coarse_caption(caption: Any) -> bool:
    return _normalize_caption(caption) not in FINE_VOCAB


def _normalize_relation_with_direction(relation: Any) -> Tuple[Optional[str], bool]:
    text = str(relation or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None, False
    aliases = {
        "adjacent to": ("near", False),
        "attached to": ("attach to", False),
        "beside": ("near", False),
        "besides": ("near", False),
        "close to": ("near", False),
        "contains": ("in", True),
        "inside": ("in", False),
        "is above": ("above", False),
        "is next to": ("near", False),
        "is on": ("on", False),
        "is on top of": ("on", False),
        "next to": ("near", False),
        "on top of": ("on", False),
        "over": ("above", False),
        "rests on": ("on", False),
        "sitting on": ("on", False),
        "supported by": ("on", False),
        "supports": ("on", True),
        "under": ("above", True),
        "below": ("above", True),
    }
    if text in aliases:
        return aliases[text]
    if text in {"near", "on", "in", "above", "attach to"}:
        return text, False
    return text, False


@dataclass
class UniGoalMappedGraph:
    objects: List[PerceivedObject]
    relations: List[PerceivedRelation]
    scene_graph: Dict[str, Any]
    room_graph: Dict[str, Any]
    group_graph: Dict[str, Any]
    metadata: Dict[str, Any]


class SAMJAMUniGoalGraphAdapter:
    """Feed SAMJAM masks and labels into UniGoal's native 3D graph pipeline."""

    def __init__(self, room_lookup: Optional[Callable[[List[float]], str]] = None):
        self.room_lookup = room_lookup
        self.env = None
        self.graph = None
        self.edge_type = None
        self.class_names: List[str] = []
        self.node_ids: Dict[Any, str] = {}
        self.node_uids: Dict[Any, int] = {}
        self.node_moved: Dict[Any, bool] = {}
        self.track_to_map_object: Dict[str, Any] = {}
        self.track_moved: Dict[str, bool] = {}
        self.segment_frame_indices: List[int] = []
        self.next_node_id = 0
        self.next_node_uid = 0
        self.manipulated_node_uids: Set[int] = set()
        self.pending_manipulation_events: List[Dict[str, Any]] = []
        self.manipulation_event_history: List[Dict[str, Any]] = []
        self.manipulation_resolutions: List[Dict[str, Any]] = []

    def reset(self, room_lookup: Optional[Callable[[List[float]], str]] = None) -> None:
        self.room_lookup = room_lookup
        self.graph = None
        self.edge_type = None
        self.class_names.clear()
        self.node_ids.clear()
        self.node_uids.clear()
        self.node_moved.clear()
        self.track_to_map_object.clear()
        self.track_moved.clear()
        self.segment_frame_indices.clear()
        self.next_node_id = 0
        self.next_node_uid = 0
        self.manipulated_node_uids.clear()
        self.pending_manipulation_events.clear()
        self.manipulation_event_history.clear()
        self.manipulation_resolutions.clear()

    def set_env(self, env: Any) -> None:
        self.env = env

    def note_manipulation_event(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        self.pending_manipulation_events.append(event)
        self.manipulation_event_history.append(event)
        del self.pending_manipulation_events[:-50]
        del self.manipulation_event_history[:-100]

    def mark_manipulated_nodes(self, node_uids: List[int]) -> None:
        self.manipulated_node_uids.update(int(uid) for uid in node_uids)

    def update(
        self,
        frame: FrameObservation,
        frame_objects: List[PerceivedObject],
        frame_relations: List[PerceivedRelation],
    ) -> UniGoalMappedGraph:
        self._validate_frame(frame)
        graph = self._ensure_graph(frame)
        frame_object_by_id = {obj.object_id: obj for obj in frame_objects}
        gobs = self._to_gobs(frame, frame_objects)
        depth_stats = self._depth_stats(frame.depth)
        gobs_stats = self._gobs_depth_stats(frame.depth, gobs)
        graph.classes = list(self.class_names)
        segment_index = self._run_external_unigoal_mapping(
            graph=graph,
            gobs=gobs,
            image_rgb=frame.rgb,
            image_depth=frame.depth,
            camera_matrix=frame.intrinsics,
            pose_matrix=frame.camera_pose,
            navigate_steps=frame.frame_index,
        )
        self.segment_frame_indices.append(frame.frame_index)

        moving_source_ids = self._moving_source_ids(frame_object_by_id, frame_relations)
        reassociations = self._reassociate_moving_tracks(
            graph=graph,
            segment_index=segment_index,
            frame_object_by_id=frame_object_by_id,
            moving_source_ids=moving_source_ids,
        )
        invalid_objects = self._prune_invalid_map_objects(graph)
        graph.get_caption()
        graph.update_node()
        source_to_node = self._source_to_node(segment_index)
        self._prepare_lifelong_nodes(
            segment_index=segment_index,
            source_to_node=source_to_node,
        )
        self._sync_moving_state(source_to_node, frame_object_by_id, moving_source_ids)
        current_frame_relations = self._build_current_frame_relations(
            source_to_node=source_to_node,
            frame_relations=frame_relations,
        )
        self._resolve_pending_manipulations(source_to_node)
        self._update_structured_edges(current_frame_relations)
        graph.scenegraph = self._lifelong_scene_graph()
        graph.update_group()

        objects = self._perceived_objects(
            segment_index,
            source_to_node,
            frame_object_by_id,
            moving_source_ids,
        )
        relations = self._perceived_relations()
        room_graph = self._room_graph()
        group_graph = self._group_graph()
        scene_graph = {
            "nodes": [
                {
                    "id": obj.object_id,
                    "uid": obj.attributes.get("uid"),
                    "name": obj.name,
                    "position": obj.position,
                    "category": obj.category,
                    "bbox": obj.bbox,
                    "visible": bool(obj.attributes.get("currently_visible", False)),
                    "is_vis": bool(obj.attributes.get("is_vis", False)),
                    "is_coarse": bool(obj.attributes.get("is_coarse", True)),
                    "label": obj.attributes.get("lifelong_label", obj.name),
                }
                for obj in objects
            ],
            "edges": [
                {"source": rel.source_id, "target": rel.target_id, "type": rel.relation}
                for rel in relations
            ],
        }
        return UniGoalMappedGraph(
            objects=objects,
            relations=relations,
            scene_graph=scene_graph,
            room_graph=room_graph,
            group_graph=group_graph,
            metadata={
                "mapping": "UniGoal.Graph.mapping3d",
                "vendor": "SAMJAM/sam2 + UniGoal/Graph.mapping3d",
                "fusion_mode": "unigoal_primary_samjam_current_frame",
                "identity_matching": (
                    "unigoal_3d_point_cloud_overlap"
                    "+samjam_moving_track_reassociation"
                ),
                "semantic_identity_matching": False,
                "map_object_count": len(graph.objects),
                "unigoal_object_count": len(graph.nodes),
                "stable_object_count": len(graph.nodes),
                "visible_object_count": sum(
                    bool(obj.attributes.get("currently_visible", False)) for obj in objects
                ),
                "samjam_current_object_count": int(len(gobs.get("source_object_id", []))),
                "depth_stats": depth_stats,
                "gobs_depth_stats": gobs_stats,
                "track_reassociation_count": len(reassociations),
                "track_reassociations": reassociations,
                "invalid_map_object_count": len(invalid_objects),
                "invalid_map_objects": invalid_objects,
                "lifelong_scene_graph": to_builtin(graph.scenegraph),
                "lifelong_summary": self._lifelong_summary(graph.scenegraph),
                "manipulation_event_history": list(self.manipulation_event_history),
                "pending_manipulation_events": list(self.pending_manipulation_events),
                "manipulation_resolutions": list(self.manipulation_resolutions[-50:]),
                "ambiguous_manipulated_objects": [
                    item for item in self.manipulation_resolutions[-50:]
                    if item.get("status") == "ambiguous"
                ],
                "unresolved_manipulated_objects": [
                    item for item in self.manipulation_resolutions[-50:]
                    if item.get("status") == "unresolved"
                ],
                "spatial_similarity": str(graph.cfg.spatial_sim_type),
                "spatial_similarity_threshold": float(graph.cfg.sim_threshold_spatial),
                "native_scene_graph": to_builtin(graph.scenegraph),
            },
        )

    def _validate_frame(self, frame: FrameObservation) -> None:
        missing = []
        if frame.depth is None:
            missing.append("depth")
        if frame.intrinsics is None:
            missing.append("intrinsics")
        if frame.camera_pose is None:
            missing.append("camera_pose")
        if missing:
            raise RuntimeError(
                "SAMJAM-UniGoal 3D mapping requires " + ", ".join(missing)
            )

    def _depth_stats(self, depth: Optional[np.ndarray]) -> Dict[str, Any]:
        if depth is None:
            return {"available": False}
        depth_array = np.asarray(depth, dtype=np.float32)
        if depth_array.ndim == 3 and depth_array.shape[-1] == 1:
            depth_array = depth_array[:, :, 0]
        finite = np.isfinite(depth_array)
        finite_positive = finite & (depth_array > 0)
        finite_values = depth_array[finite]
        return {
            "available": True,
            "shape": list(depth_array.shape),
            "finite_ratio": float(finite.mean()) if finite.size else 0.0,
            "positive_ratio": float((depth_array > 0).mean()) if depth_array.size else 0.0,
            "finite_positive_ratio": (
                float(finite_positive.mean()) if finite_positive.size else 0.0
            ),
            "min_finite": float(finite_values.min()) if finite_values.size else None,
            "max_finite": float(finite_values.max()) if finite_values.size else None,
        }

    def _gobs_depth_stats(
        self,
        depth: Optional[np.ndarray],
        gobs: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if depth is None:
            return []
        depth_array = np.asarray(depth, dtype=np.float32)
        if depth_array.ndim == 3 and depth_array.shape[-1] == 1:
            depth_array = depth_array[:, :, 0]
        masks = gobs.get("mask", [])
        source_ids = gobs.get("source_object_id", [])
        captions = gobs.get("caption", [])
        stats = []
        for index, mask in enumerate(masks):
            mask_array = np.asarray(mask, dtype=bool)
            if mask_array.shape != depth_array.shape[:2]:
                stats.append(
                    {
                        "source_object_id": source_ids[index] if index < len(source_ids) else None,
                        "caption": captions[index] if index < len(captions) else None,
                        "mask_area": int(mask_array.sum()),
                        "valid_depth_ratio": None,
                        "reason": "mask_depth_shape_mismatch",
                    }
                )
                continue
            masked_depth = depth_array[mask_array]
            valid = np.isfinite(masked_depth) & (masked_depth > 0)
            stats.append(
                {
                    "source_object_id": source_ids[index] if index < len(source_ids) else None,
                    "caption": captions[index] if index < len(captions) else None,
                    "mask_area": int(mask_array.sum()),
                    "valid_depth_ratio": (
                        float(valid.mean()) if masked_depth.size else 0.0
                    ),
                    "valid_depth_count": int(valid.sum()),
                }
            )
        return stats

    def _ensure_graph(self, frame: FrameObservation):
        if self.graph is not None:
            return self.graph

        vendor_root = repo_root() / "og_ego_prim" / "scene_graph" / "vendor" / "unigoal"
        insert_sys_path([vendor_root])
        try:
            import torch
            import src.graph.graph as graph_module
        except ImportError as exc:
            raise ImportError(
                "SAMJAM-UniGoal mapping requires the vendored UniGoal dependencies"
            ) from exc
        Graph = graph_module.Graph
        Edge = graph_module.Edge

        height, width = frame.rgb.shape[:2]
        map_resolution = _env_int("ISBENCH_UNIGOAL_MAP_RESOLUTION", 5)
        map_size_cm = _env_int("ISBENCH_UNIGOAL_MAP_SIZE_CM", 2400)
        args = SimpleNamespace(
            map_resolution=map_resolution,
            map_size_cm=map_size_cm,
            map_size=int(map_size_cm / map_resolution),
            env_frame_height=height,
            env_frame_width=width,
            hfov=_env_float("ISBENCH_SCENE_GRAPH_HFOV", 90.0),
            device=os.environ.get(
                "ISBENCH_SCENE_GRAPH_DEVICE",
                "cuda" if torch.cuda.is_available() else "cpu",
            ),
            base_url=None,
            api_key="EMPTY",
            llm_model="external",
            vlm_model="external",
        )
        graph = self._instantiate_external_graph(graph_module, Graph, args)
        graph.cfg.sim_threshold_spatial = _env_float(
            "ISBENCH_SAMJAM_UNIGOAL_SPATIAL_THRESHOLD", 0.01
        )
        # SAMJAM already filters masks and VLM matches before this adapter.
        graph.cfg.mask_conf_threshold = _env_float(
            "ISBENCH_SAMJAM_UNIGOAL_MASK_CONFIDENCE", 0.0
        )
        graph.cfg.obj_min_detections = _env_int(
            "ISBENCH_SAMJAM_UNIGOAL_MIN_DETECTIONS", 1
        )
        graph.set_room_lookup(self.room_lookup)
        self.graph = graph
        self.edge_type = Edge
        return graph

    def _instantiate_external_graph(self, graph_module: Any, graph_type: Any, args: Any):
        originals = {
            "get_grounded_sam": graph_type.get_grounded_sam,
            "LLM": graph_module.LLM,
            "VLM": graph_module.VLM,
            "GraphBuilder": graph_module.GraphBuilder,
            "GoalGraphDecomposer": graph_module.GoalGraphDecomposer,
            "DISK": graph_module.DISK,
            "LightGlue": graph_module.LightGlue,
        }

        class _NoopCallable:
            def __init__(self, *args, **kwargs):
                pass

            def __call__(self, *args, **kwargs):
                return ""

        class _NoopGraphBuilder(_NoopCallable):
            def build_graph_from_text(self, *_args, **_kwargs):
                return {"nodes": [], "edges": []}

            def get_relations(self, *_args, **_kwargs):
                return []

        class _NoopGoalGraphDecomposer(_NoopCallable):
            def goal_decomposition(self, graph):
                return {"subgraph_1": graph}

        class _NoopModule:
            def __init__(self, *args, **kwargs):
                pass

            def eval(self):
                return self

            def to(self, *_args, **_kwargs):
                return self

        def _noop_get_grounded_sam(_self, _device):
            return None

        try:
            graph_type.get_grounded_sam = _noop_get_grounded_sam
            graph_module.LLM = _NoopCallable
            graph_module.VLM = _NoopCallable
            graph_module.GraphBuilder = _NoopGraphBuilder
            graph_module.GoalGraphDecomposer = _NoopGoalGraphDecomposer
            graph_module.DISK = _NoopModule
            graph_module.LightGlue = _NoopModule
            graph = graph_type(args, is_navigation=False)
        finally:
            graph_type.get_grounded_sam = originals["get_grounded_sam"]
            graph_module.LLM = originals["LLM"]
            graph_module.VLM = originals["VLM"]
            graph_module.GraphBuilder = originals["GraphBuilder"]
            graph_module.GoalGraphDecomposer = originals["GoalGraphDecomposer"]
            graph_module.DISK = originals["DISK"]
            graph_module.LightGlue = originals["LightGlue"]

        self._patch_unigoal_iou_fallback()
        return graph

    def _patch_unigoal_iou_fallback(self) -> None:
        try:
            from src.graph.utils import utils as graph_utils
        except ImportError:
            return
        original = getattr(graph_utils, "compute_3d_iou_accuracte_batch", None)
        if original is None or getattr(original, "_is_isbench_backend_patch", False):
            return

        def _backend_iou_fallback(bbox_map, bbox_new):
            try:
                return original(bbox_map, bbox_new)
            except ImportError:
                return graph_utils.compute_iou_batch(bbox_map, bbox_new)

        _backend_iou_fallback._is_isbench_backend_patch = True
        graph_utils.compute_3d_iou_accuracte_batch = _backend_iou_fallback

    def _run_external_unigoal_mapping(
        self,
        graph: Any,
        gobs: Dict[str, Any],
        image_rgb: np.ndarray,
        image_depth: np.ndarray,
        camera_matrix: np.ndarray,
        pose_matrix: np.ndarray,
        navigate_steps: int,
    ) -> int:
        graph.image_rgb = np.asarray(image_rgb).copy()
        depth = np.asarray(image_depth, dtype=np.float32)
        depth = np.where(np.isfinite(depth), depth, 0.0)
        graph.image_depth = depth[..., None] if depth.ndim == 2 else depth.copy()
        graph.camera_matrix = np.asarray(camera_matrix, dtype=np.float32)
        graph.pose_matrix = np.asarray(pose_matrix, dtype=np.float32)
        graph.navigate_steps = navigate_steps
        graph.segment2d_results.append(gobs)
        graph.scenegraph_update_step += 1
        graph.mapping3d()
        return len(graph.segment2d_results) - 1

    def _to_gobs(
        self,
        frame: FrameObservation,
        frame_objects: List[PerceivedObject],
    ) -> Dict[str, Any]:
        usable = [
            obj
            for obj in frame_objects
            if obj.mask is not None and obj.bbox is not None
        ]
        for obj in usable:
            if obj.name not in self.class_names:
                self.class_names.append(obj.name)

        height, width = frame.rgb.shape[:2]
        masks = (
            np.asarray([np.asarray(obj.mask, dtype=bool) for obj in usable], dtype=bool)
            if usable
            else np.empty((0, height, width), dtype=bool)
        )
        return {
            "xyxy": np.asarray([obj.bbox for obj in usable], dtype=np.float32).reshape(-1, 4),
            "confidence": np.asarray(
                [
                    max(
                        float(obj.attributes.get("predicted_iou", 0.0) or 0.0),
                        float(obj.attributes.get("stability_score", 0.0) or 0.0),
                        float(obj.confidence),
                    )
                    for obj in usable
                ],
                dtype=np.float32,
            ),
            "class_id": np.asarray(
                [self.class_names.index(obj.name) for obj in usable], dtype=np.int64
            ),
            "mask": masks,
            "classes": list(self.class_names),
            "image_appear_efficiency": [""] * len(usable),
            "image_rgb": frame.rgb,
            "caption": [obj.name for obj in usable],
            "source_object_id": [obj.object_id for obj in usable],
        }

    def _source_to_node(self, segment_index: int) -> Dict[str, Any]:
        source_to_node = {}
        segment = self.graph.segment2d_results[segment_index]
        source_ids = segment.get("source_object_id", [])
        for node in self.graph.nodes:
            image_indices = node.object.get("image_idx", [])
            mask_indices = node.object.get("mask_idx", [])
            for image_index, mask_index in zip(image_indices, mask_indices):
                if image_index == segment_index and 0 <= mask_index < len(source_ids):
                    source_to_node[source_ids[mask_index]] = node
        return source_to_node

    def _source_to_map_object(self, graph: Any, segment_index: int) -> Dict[str, Any]:
        source_to_object = {}
        segment = graph.segment2d_results[segment_index]
        source_ids = segment.get("source_object_id", [])
        for map_object in graph.objects:
            image_indices = map_object.get("image_idx", [])
            mask_indices = map_object.get("mask_idx", [])
            for image_index, mask_index in zip(image_indices, mask_indices):
                if image_index == segment_index and 0 <= mask_index < len(source_ids):
                    source_to_object[source_ids[mask_index]] = map_object
        return source_to_object

    def _moving_source_ids(
        self,
        frame_object_by_id: Dict[str, PerceivedObject],
        frame_relations: List[PerceivedRelation],
    ) -> Set[str]:
        moving_ids = {
            object_id
            for object_id, obj in frame_object_by_id.items()
            if self._has_moving_signal(obj)
        }
        for relation in frame_relations:
            source = frame_object_by_id.get(relation.source_id)
            target = frame_object_by_id.get(relation.target_id)
            if source is None or target is None:
                continue
            if self._is_hand_like(source) and not self._is_hand_like(target):
                moving_ids.add(target.object_id)
            elif self._is_hand_like(target) and not self._is_hand_like(source):
                moving_ids.add(source.object_id)
        return moving_ids

    def _has_moving_signal(self, obj: PerceivedObject) -> bool:
        return bool(
            obj.attributes.get("is_moving", False)
            or obj.attributes.get("is_moved", False)
        )

    def _is_hand_like(self, obj: PerceivedObject) -> bool:
        if bool(obj.attributes.get("is_hand", False)):
            return True
        name = _category(obj.name)
        category = _category(obj.category)
        hand_like = {"hand", "robot_hand", "robot_gripper", "gripper"}
        return name in hand_like or category in hand_like

    def _track_key(self, obj: PerceivedObject) -> Optional[str]:
        samjam_id = obj.attributes.get("samjam_id")
        if samjam_id is not None:
            return f"samjam:{samjam_id}"
        source_object_id = obj.attributes.get("source_object_id")
        if source_object_id:
            return str(source_object_id)
        if obj.object_id:
            return str(obj.object_id)
        return None

    def _reassociate_moving_tracks(
        self,
        graph: Any,
        segment_index: int,
        frame_object_by_id: Dict[str, PerceivedObject],
        moving_source_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        source_to_object = self._source_to_map_object(graph, segment_index)
        reassociations: List[Dict[str, Any]] = []
        for source_id, current_object in list(source_to_object.items()):
            frame_object = frame_object_by_id.get(source_id)
            if frame_object is None:
                continue
            track_key = self._track_key(frame_object)
            if track_key is None:
                continue

            previous_object = self.track_to_map_object.get(track_key)
            if previous_object is not None and not self._map_object_in_graph(graph, previous_object):
                previous_object = None

            if (
                source_id in moving_source_ids
                and previous_object is not None
                and previous_object is not current_object
            ):
                self._merge_map_object(graph, target_object=previous_object, source_object=current_object)
                self.track_to_map_object[track_key] = previous_object
                self.track_moved[track_key] = True
                reassociations.append(
                    {
                        "track": track_key,
                        "source_object_id": source_id,
                        "merged_detection_count": int(current_object.get("num_detections", 1)),
                    }
                )
            else:
                self.track_to_map_object[track_key] = current_object
                if source_id in moving_source_ids:
                    self.track_moved[track_key] = True

        if reassociations:
            self._refresh_objects_post(graph)
        return reassociations

    def _prune_invalid_map_objects(self, graph: Any) -> List[Dict[str, Any]]:
        invalid = []
        for map_object in list(graph.objects):
            reason = self._invalid_map_object_reason(map_object)
            if reason is None:
                continue
            invalid.append(
                {
                    "reason": reason,
                    "class_name": list(map_object.get("class_name", [])),
                    "image_idx": list(map_object.get("image_idx", [])),
                    "mask_idx": list(map_object.get("mask_idx", [])),
                }
            )
            self._remove_map_object_with_node(graph, map_object)
        if invalid:
            self._refresh_objects_post(graph)
            self.track_to_map_object = {
                track: map_object
                for track, map_object in self.track_to_map_object.items()
                if self._map_object_in_graph(graph, map_object)
            }
        return invalid

    def _invalid_map_object_reason(self, map_object: Any) -> Optional[str]:
        pcd = map_object.get("pcd")
        if pcd is None:
            return "missing_point_cloud"
        points = np.asarray(pcd.points)
        if points.size == 0:
            return "empty_point_cloud"
        if not np.isfinite(points).all():
            return "nonfinite_point_cloud"
        bbox = map_object.get("bbox")
        if bbox is None:
            return "missing_bbox"
        try:
            center = np.asarray(bbox.get_center(), dtype=np.float64)
        except Exception:
            return "invalid_bbox"
        if center.size == 0 or not np.isfinite(center).all():
            return "nonfinite_bbox"
        return None

    def _map_object_in_graph(self, graph: Any, map_object: Any) -> bool:
        return any(candidate is map_object for candidate in graph.objects)

    def _merge_map_object(self, graph: Any, target_object: Any, source_object: Any) -> None:
        from src.graph.utils.utils import merge_obj2_into_obj1

        source_node = source_object.get("node")
        target_node = target_object.get("node")
        merge_obj2_into_obj1(graph.cfg, target_object, source_object, run_dbscan=False)
        self._remove_map_object(graph, source_object)
        if source_node is not None and source_node is not target_node:
            self._remove_node(graph, source_node)

    def _remove_map_object(self, graph: Any, map_object: Any) -> None:
        for index, candidate in enumerate(list(graph.objects)):
            if candidate is map_object:
                graph.objects.pop(index)
                return

    def _remove_map_object_with_node(self, graph: Any, map_object: Any) -> None:
        node = map_object.get("node")
        self._remove_map_object(graph, map_object)
        if node is not None:
            self._remove_node(graph, node)

    def _remove_node(self, graph: Any, node: Any) -> None:
        for edge in list(node.edges):
            edge.delete()
        for room_node in getattr(graph, "room_nodes", []):
            room_node.nodes.discard(node)
        if node in graph.nodes:
            graph.nodes.remove(node)
        self.node_ids.pop(node, None)
        self.node_uids.pop(node, None)
        self.node_moved.pop(node, None)

    def _refresh_objects_post(self, graph: Any) -> None:
        from src.graph.utils.utils import filter_objects

        graph.objects_post = filter_objects(graph.cfg, graph.objects)

    def _node_uid(self, node: Any) -> int:
        if node not in self.node_uids:
            self.node_uids[node] = self.next_node_uid
            self.next_node_uid += 1
        uid = self.node_uids[node]
        setattr(node, "uid", uid)
        return uid

    def _prepare_lifelong_nodes(
        self,
        segment_index: int,
        source_to_node: Dict[str, Any],
    ) -> None:
        visible_nodes = set(source_to_node.values())
        for node in self.graph.nodes:
            uid = self._node_uid(node)
            normalized = _normalize_caption(getattr(node, "caption", None))
            if normalized and normalized != getattr(node, "caption", None):
                node.caption = normalized
            node.is_vis = node in visible_nodes
            node.is_coarse = _is_coarse_caption(node.caption)
            if node.is_vis:
                node.last_seen_step = segment_index
            elif not hasattr(node, "last_seen_step"):
                node.last_seen_step = -1
            setattr(node, "normalized_caption", normalized)
            setattr(node, "uid", uid)

    def _node_bounds(self, node: Any) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        map_object = getattr(node, "object", None)
        if not map_object:
            return None, None
        bbox = map_object.get("bbox")
        if bbox is None:
            return None, None
        try:
            points = np.asarray(bbox.get_box_points(), dtype=np.float64)
        except Exception:
            return None, None
        if points.size == 0 or not np.isfinite(points).all():
            return None, None
        return points.min(axis=0), points.max(axis=0)

    def _node_position(self, node: Any) -> Optional[List[float]]:
        map_object = getattr(node, "object", None)
        if not map_object:
            return None
        bbox = map_object.get("bbox")
        if bbox is None:
            return None
        try:
            center = np.asarray(bbox.get_center(), dtype=np.float64)
        except Exception:
            return None
        if center.size == 0 or not np.isfinite(center).all():
            return None
        return [float(value) for value in center]

    def _xy_overlap_ratio(
        self,
        fine_min: np.ndarray,
        fine_max: np.ndarray,
        coarse_min: np.ndarray,
        coarse_max: np.ndarray,
    ) -> Tuple[float, float]:
        x_overlap = max(0.0, min(fine_max[0], coarse_max[0]) - max(fine_min[0], coarse_min[0]))
        y_overlap = max(0.0, min(fine_max[1], coarse_max[1]) - max(fine_min[1], coarse_min[1]))
        inter_area = x_overlap * y_overlap
        fine_area = max(float((fine_max[0] - fine_min[0]) * (fine_max[1] - fine_min[1])), 1e-6)
        coarse_area = max(float((coarse_max[0] - coarse_min[0]) * (coarse_max[1] - coarse_min[1])), 1e-6)
        return inter_area / fine_area, inter_area / coarse_area

    def _bbox_surface_distance(self, node1: Any, node2: Any) -> Optional[float]:
        node1_min, node1_max = self._node_bounds(node1)
        node2_min, node2_max = self._node_bounds(node2)
        if node1_min is None or node2_min is None:
            return None
        gap = np.maximum(0.0, np.maximum(node1_min - node2_max, node2_min - node1_max))
        return float(np.linalg.norm(gap))

    def _bbox_xy_center_distance(self, node1: Any, node2: Any) -> Optional[float]:
        node1_min, node1_max = self._node_bounds(node1)
        node2_min, node2_max = self._node_bounds(node2)
        if node1_min is None or node2_min is None:
            return None
        center1 = (node1_min[:2] + node1_max[:2]) / 2.0
        center2 = (node2_min[:2] + node2_max[:2]) / 2.0
        return float(np.linalg.norm(center1 - center2))

    def _infer_fine_coarse_relation(self, fine_node: Any, coarse_node: Any) -> Optional[str]:
        coarse_caption = _normalize_caption(getattr(coarse_node, "caption", None))
        if coarse_caption not in ATTACHABLE_COARSE_VOCAB:
            return None
        fine_min, fine_max = self._node_bounds(fine_node)
        coarse_min, coarse_max = self._node_bounds(coarse_node)
        if fine_min is None or coarse_min is None:
            return None

        fine_cover_ratio, _ = self._xy_overlap_ratio(fine_min, fine_max, coarse_min, coarse_max)
        support_min, support_max = coarse_min.copy(), coarse_max.copy()
        if coarse_caption in SUPPORT_SURFACE_VOCAB:
            margin = _env_float("ISBENCH_LIFELONG_SUPPORT_SURFACE_XY_MARGIN", 0.60)
            support_min[:2] -= margin
            support_max[:2] += margin
        support_cover_ratio, _ = self._xy_overlap_ratio(fine_min, fine_max, support_min, support_max)
        fine_center = self._node_position(fine_node)
        coarse_center = self._node_position(coarse_node)
        map_center_dist = (
            float(np.linalg.norm(np.asarray(fine_center[:2]) - np.asarray(coarse_center[:2])))
            if fine_center is not None and coarse_center is not None
            else float("inf")
        )

        top_contact_gap = min(abs(fine_min[2] - coarse_max[2]), abs(fine_max[2] - coarse_min[2]))
        on_gap = _env_float("ISBENCH_LIFELONG_ON_GAP_THRESH", 0.18)
        xy_contact = _env_float("ISBENCH_LIFELONG_XY_CONTACT_OVERLAP_THRESH", 0.10)
        support_contact = (
            coarse_caption in SUPPORT_SURFACE_VOCAB
            and support_cover_ratio >= xy_contact
            and map_center_dist <= _env_float("ISBENCH_LIFELONG_SUPPORT_MAP_DIST_THRESH", 18.0)
        )
        support_fallback = (
            coarse_caption in SUPPORT_SURFACE_VOCAB
            and map_center_dist <= _env_float("ISBENCH_LIFELONG_SUPPORT_FALLBACK_MAP_DIST_THRESH", 65.0)
        )
        if top_contact_gap <= on_gap and (
            fine_cover_ratio >= xy_contact or support_contact or support_fallback
        ):
            return "on"

        in_gap = _env_float("ISBENCH_LIFELONG_IN_GAP_THRESH", 0.10)
        fine_xy_inside = (
            fine_min[0] >= coarse_min[0] - in_gap
            and fine_max[0] <= coarse_max[0] + in_gap
            and fine_min[1] >= coarse_min[1] - in_gap
            and fine_max[1] <= coarse_max[1] + in_gap
        )
        fine_z_inside = fine_min[2] >= coarse_min[2] - in_gap and fine_max[2] <= coarse_max[2] + in_gap
        if (
            coarse_caption in CONTAINER_COARSE_VOCAB
            and fine_xy_inside
            and fine_z_inside
            and fine_cover_ratio >= _env_float("ISBENCH_LIFELONG_XY_CONTAIN_OVERLAP_THRESH", 0.55)
        ):
            return "in"

        x_gap = min(abs(fine_max[0] - coarse_min[0]), abs(fine_min[0] - coarse_max[0]))
        y_gap = min(abs(fine_max[1] - coarse_min[1]), abs(fine_min[1] - coarse_max[1]))
        side_gap = min(x_gap, y_gap)
        z_overlap = max(0.0, min(fine_max[2], coarse_max[2]) - max(fine_min[2], coarse_min[2]))
        fine_height = max(float(fine_max[2] - fine_min[2]), 1e-6)
        if (
            side_gap <= _env_float("ISBENCH_LIFELONG_ATTACH_GAP_THRESH", 0.14)
            and z_overlap / fine_height >= 0.35
            and top_contact_gap > on_gap
        ):
            return "attach to"

        fine_above_up = fine_min[2] - coarse_max[2]
        fine_above_down = coarse_min[2] - fine_max[2]
        if (
            max(fine_above_up, fine_above_down)
            >= _env_float("ISBENCH_LIFELONG_ABOVE_HEIGHT_THRESH", 0.30)
            and fine_cover_ratio >= xy_contact
        ):
            return "above"
        return None

    def _room_near_threshold(self, room_node: Any) -> float:
        room_nodes = [] if room_node is None else [
            node for node in room_node.nodes if getattr(node, "is_coarse", True)
        ]
        mins, maxs = [], []
        for node in room_nodes:
            node_min, node_max = self._node_bounds(node)
            if node_min is None:
                continue
            mins.append(node_min[:2])
            maxs.append(node_max[:2])
        min_dist = _env_float("ISBENCH_LIFELONG_COARSE_NEAR_DIST_MIN", 0.25)
        cap = _env_float("ISBENCH_LIFELONG_COARSE_NEAR_DIST_CAP", 0.75)
        if len(mins) < 2:
            return min_dist
        room_min = np.min(np.asarray(mins), axis=0)
        room_max = np.max(np.asarray(maxs), axis=0)
        room_span = np.maximum(room_max - room_min, 0.0)
        fraction = _env_float("ISBENCH_LIFELONG_COARSE_NEAR_ROOM_FRACTION", 0.125)
        threshold = float(np.min(room_span) * fraction)
        return float(np.clip(threshold, min_dist, cap))

    def _infer_fine_fine_near(
        self,
        fine_node1: Any,
        fine_node2: Any,
        fine_parent_map: Dict[int, Any],
    ) -> Optional[str]:
        parent1 = fine_parent_map.get(self._node_uid(fine_node1))
        parent2 = fine_parent_map.get(self._node_uid(fine_node2))
        if parent1 is None or parent2 is None or self._node_uid(parent1) != self._node_uid(parent2):
            return None
        surface_dist = self._bbox_surface_distance(fine_node1, fine_node2)
        if surface_dist is not None and surface_dist <= _env_float("ISBENCH_LIFELONG_FINE_NEAR_DIST_THRESH", 0.22):
            return "near"
        return None

    def _build_current_frame_relations(
        self,
        source_to_node: Dict[str, Any],
        frame_relations: List[PerceivedRelation],
    ) -> List[Tuple[Any, Any, str]]:
        relations: List[Tuple[Any, Any, str]] = []
        for relation in frame_relations:
            source_node = source_to_node.get(relation.source_id)
            target_node = source_to_node.get(relation.target_id)
            if source_node is None or target_node is None or source_node is target_node:
                continue
            normalized, reverse = _normalize_relation_with_direction(relation.relation)
            if normalized not in ALLOWED_LIFELONG_RELATIONS:
                continue
            if reverse:
                source_node, target_node = target_node, source_node
            relations.append((source_node, target_node, normalized))

        candidate_coarse = [
            node for node in self.graph.nodes
            if getattr(node, "is_coarse", True)
            and _normalize_caption(getattr(node, "caption", None)) in ATTACHABLE_COARSE_VOCAB
        ]
        visible_fine = [
            node for node in self.graph.nodes
            if not getattr(node, "is_coarse", True) and getattr(node, "is_vis", False)
        ]
        fine_parent_map: Dict[int, Any] = {}
        for fine_node in visible_fine:
            candidates = []
            for coarse_node in candidate_coarse:
                relation = self._infer_fine_coarse_relation(fine_node, coarse_node)
                if relation is None:
                    continue
                surface_dist = self._bbox_surface_distance(fine_node, coarse_node)
                surface_dist = float("inf") if surface_dist is None else surface_dist
                fine_center = self._node_position(fine_node)
                coarse_center = self._node_position(coarse_node)
                map_dist = (
                    float(np.linalg.norm(np.asarray(fine_center[:2]) - np.asarray(coarse_center[:2])))
                    if fine_center is not None and coarse_center is not None
                    else float("inf")
                )
                candidates.append((relation, coarse_node, surface_dist, map_dist))
            if candidates:
                candidates.sort(
                    key=lambda item: (
                        FINE_PARENT_RELATION_PRIORITY.get(item[0], 0),
                        1 if getattr(item[1], "is_vis", False) else 0,
                        -item[3],
                        -item[2],
                    ),
                    reverse=True,
                )
                best_relation, best_coarse, _, _ = candidates[0]
                fine_parent_map[self._node_uid(fine_node)] = best_coarse
                relations.append((fine_node, best_coarse, best_relation))

        for i, node1 in enumerate(visible_fine):
            for node2 in visible_fine[i + 1:]:
                relation = self._infer_fine_fine_near(node1, node2, fine_parent_map)
                if relation is not None:
                    relations.append((node1, node2, relation))
        return relations

    def _sync_moving_state(
        self,
        source_to_node: Dict[str, Any],
        frame_object_by_id: Dict[str, PerceivedObject],
        moving_source_ids: Set[str],
    ) -> None:
        for source_id, node in source_to_node.items():
            frame_object = frame_object_by_id[source_id]
            track_key = self._track_key(frame_object)
            is_moving = source_id in moving_source_ids
            if track_key is not None and is_moving:
                self.track_moved[track_key] = True
            self.node_moved[node] = bool(
                self.node_moved.get(node, False)
                or is_moving
                or (track_key is not None and self.track_moved.get(track_key, False))
            )
            if is_moving:
                for edge in list(node.edges):
                    edge.delete()

    def _sync_relations(
        self,
        source_to_node: Dict[str, Any],
        frame_relations: List[PerceivedRelation],
    ) -> None:
        for relation in frame_relations:
            source_node = source_to_node.get(relation.source_id)
            target_node = source_to_node.get(relation.target_id)
            if source_node is None or target_node is None or source_node is target_node:
                continue
            edge = next(
                (
                    item
                    for item in source_node.edges
                    if item.node1 is source_node and item.node2 is target_node
                ),
                None,
            )
            if edge is None:
                edge = self.edge_type(source_node, target_node)
            edge.set_relation(relation.relation)

    def _upsert_edge(self, node1: Any, node2: Any, relation: str) -> None:
        if node1 is node2 or relation is None:
            return
        edge = next(
            (
                item
                for item in node1.edges
                if item.node1 is node1 and item.node2 is node2
            ),
            None,
        )
        if edge is None:
            edge = self.edge_type(node1, node2)
        edge.set_relation(relation)

    def _refresh_coarse_near_edges(self) -> None:
        for edge in list(self.graph.get_edges()):
            if (
                getattr(edge.node1, "is_coarse", True)
                and getattr(edge.node2, "is_coarse", True)
                and edge.relation == "near"
            ):
                edge.delete()
        coarse_nodes = [
            node for node in self.graph.nodes
            if getattr(node, "is_coarse", True)
            and self._node_position(node) is not None
            and _normalize_caption(getattr(node, "caption", None)) in COARSE_NEAR_OBSTACLE_VOCAB
        ]
        for i, node1 in enumerate(coarse_nodes):
            for node2 in coarse_nodes[i + 1:]:
                if _normalize_caption(node1.caption) == _normalize_caption(node2.caption):
                    continue
                if (
                    node1.room_node is not None
                    and node2.room_node is not None
                    and node1.room_node is not node2.room_node
                ):
                    continue
                surface_dist = self._bbox_surface_distance(node1, node2)
                center_dist = self._bbox_xy_center_distance(node1, node2)
                if surface_dist is None or center_dist is None:
                    continue
                threshold = self._room_near_threshold(node1.room_node)
                if surface_dist <= threshold and center_dist <= threshold:
                    self._upsert_edge(node1, node2, "near")

    def _fine_parent_map_from_edges(self) -> Dict[int, Any]:
        parent_map: Dict[int, Any] = {}
        for edge in self.graph.get_edges():
            if edge.relation not in {"on", "in", "above", "attach to"}:
                continue
            if (not getattr(edge.node1, "is_coarse", True)) and getattr(edge.node2, "is_coarse", True):
                parent_map[self._node_uid(edge.node1)] = edge.node2
            elif getattr(edge.node1, "is_coarse", True) and (not getattr(edge.node2, "is_coarse", True)):
                parent_map[self._node_uid(edge.node2)] = edge.node1
        return parent_map

    def _refresh_fine_fine_near_edges(self) -> None:
        fine_parent_map = self._fine_parent_map_from_edges()
        for edge in list(self.graph.get_edges()):
            if (
                not getattr(edge.node1, "is_coarse", True)
                and not getattr(edge.node2, "is_coarse", True)
                and edge.relation == "near"
            ):
                edge.delete()
        fine_nodes = [
            node for node in self.graph.nodes
            if not getattr(node, "is_coarse", True) and self._node_uid(node) in fine_parent_map
        ]
        for i, node1 in enumerate(fine_nodes):
            for node2 in fine_nodes[i + 1:]:
                relation = self._infer_fine_fine_near(node1, node2, fine_parent_map)
                if relation is not None:
                    self._upsert_edge(node1, node2, relation)

    def _reconcile_manipulated_edges(
        self,
        frame_relation_map: Dict[Tuple[int, int], str],
    ) -> None:
        if not self.manipulated_node_uids:
            return
        uid_to_node = {self._node_uid(node): node for node in self.graph.nodes}
        for uid in list(self.manipulated_node_uids):
            node = uid_to_node.get(uid)
            if node is None:
                continue
            for edge in list(node.edges):
                other = edge.node2 if edge.node1 is node else edge.node1
                if not getattr(other, "is_vis", False):
                    edge.delete()
                    continue
                direct_key = (self._node_uid(edge.node1), self._node_uid(edge.node2))
                reverse_key = (self._node_uid(edge.node2), self._node_uid(edge.node1))
                if direct_key in frame_relation_map:
                    edge.set_relation(frame_relation_map[direct_key])
                elif reverse_key in frame_relation_map:
                    edge.delete()
                else:
                    edge.delete()

    def _update_structured_edges(
        self,
        frame_relations: List[Tuple[Any, Any, str]],
    ) -> None:
        self._refresh_coarse_near_edges()
        frame_relation_map = {
            (self._node_uid(node1), self._node_uid(node2)): relation
            for node1, node2, relation in frame_relations
        }
        self._reconcile_manipulated_edges(frame_relation_map)

        fine_with_new_parent = {
            self._node_uid(node1)
            for node1, node2, _ in frame_relations
            if not getattr(node1, "is_coarse", True) and getattr(node2, "is_coarse", True)
        }
        for fine_node in [
            node for node in self.graph.nodes
            if not getattr(node, "is_coarse", True) and getattr(node, "is_vis", False)
        ]:
            if self._node_uid(fine_node) not in fine_with_new_parent:
                continue
            for edge in list(fine_node.edges):
                other = edge.node2 if edge.node1 is fine_node else edge.node1
                if getattr(other, "is_coarse", True):
                    edge.delete()

        seen = set()
        for node1, node2, relation in frame_relations:
            key = (self._node_uid(node1), self._node_uid(node2), relation)
            if key in seen:
                continue
            seen.add(key)
            self._upsert_edge(node1, node2, relation)
        self._refresh_fine_fine_near_edges()
        self.manipulated_node_uids.clear()

    def _resolve_pending_manipulations(self, source_to_node: Dict[str, Any]) -> None:
        if not self.pending_manipulation_events:
            return
        events = list(self.pending_manipulation_events)
        self.pending_manipulation_events.clear()
        for event in events:
            node, resolution = self._resolve_manipulation_event(event, source_to_node)
            if node is not None:
                self.mark_manipulated_nodes([self._node_uid(node)])
            self.manipulation_resolutions.append(resolution)
        del self.manipulation_resolutions[:-100]

    def _resolve_manipulation_event(
        self,
        event: Dict[str, Any],
        source_to_node: Dict[str, Any],
    ) -> Tuple[Optional[Any], Dict[str, Any]]:
        explicit_uid = event.get("uid")
        if explicit_uid is not None:
            for node in self.graph.nodes:
                if self._node_uid(node) == int(explicit_uid):
                    return node, {"event": event, "status": "resolved", "uid": int(explicit_uid), "method": "explicit_uid"}
            return None, {"event": event, "status": "unresolved", "reason": "explicit_uid_not_found"}

        moved_object = event.get("moved_object")
        if not moved_object:
            return None, {"event": event, "status": "unresolved", "reason": "missing_moved_object"}

        task_obj = self._resolve_env_object(str(moved_object))
        task_position = self._object_position(task_obj)
        normalized_name = _normalize_caption(moved_object)
        current_visible_nodes = set(source_to_node.values())
        candidates = []
        for node in self.graph.nodes:
            score = 0.0
            reasons = []
            semantic_match = False
            label = _normalize_caption(getattr(node, "caption", None))
            history = [
                _normalize_caption(item)
                for item in node.object.get("class_name", [])
            ]
            if label == normalized_name:
                score += 6.0
                reasons.append("label")
                semantic_match = True
            elif normalized_name in history:
                score += 4.0
                reasons.append("caption_history")
                semantic_match = True
            elif normalized_name and (normalized_name in label or label in normalized_name):
                score += 2.0
                reasons.append("partial_label")
                semantic_match = True
            if not semantic_match:
                continue
            if node in current_visible_nodes:
                score += 1.5
                reasons.append("visible")

            node_position = self._node_position(node)
            distance = None
            if task_position is not None and node_position is not None:
                dim = min(len(task_position), len(node_position), 3)
                distance = float(
                    np.linalg.norm(
                        np.asarray(task_position[:dim]) - np.asarray(node_position[:dim])
                    )
                )
                score += max(0.0, 8.0 - distance * 4.0)
                reasons.append("pose_distance")
            if score <= 0.0:
                continue
            candidates.append(
                {
                    "node": node,
                    "uid": self._node_uid(node),
                    "object_id": self._stable_id(node),
                    "label": label,
                    "score": score,
                    "distance": distance,
                    "visible": node in current_visible_nodes,
                    "reasons": reasons,
                }
            )
        candidates.sort(
            key=lambda item: (
                item["score"],
                -9999.0 if item["distance"] is None else -item["distance"],
            ),
            reverse=True,
        )
        compact_candidates = [
            {
                key: value
                for key, value in candidate.items()
                if key != "node"
            }
            for candidate in candidates[:5]
        ]
        if not candidates:
            return None, {
                "event": event,
                "status": "unresolved",
                "reason": "no_candidate",
                "normalized_name": normalized_name,
                "task_object_found": task_obj is not None,
            }
        if len(candidates) > 1:
            best = candidates[0]
            second = candidates[1]
            if task_position is None and best["score"] - second["score"] < 1.0:
                return None, {
                    "event": event,
                    "status": "ambiguous",
                    "reason": "score_tie_without_task_pose",
                    "candidates": compact_candidates,
                }
            if (
                task_position is not None
                and best["distance"] is not None
                and second["distance"] is not None
                and abs(best["distance"] - second["distance"]) < 0.05
                and best["score"] - second["score"] < 1.0
            ):
                return None, {
                    "event": event,
                    "status": "ambiguous",
                    "reason": "pose_distance_tie",
                    "candidates": compact_candidates,
                }
        best = candidates[0]
        return best["node"], {
            "event": event,
            "status": "resolved",
            "method": "task_pose_category_score",
            "uid": best["uid"],
            "object_id": best["object_id"],
            "label": best["label"],
            "score": best["score"],
            "distance": best["distance"],
            "candidates": compact_candidates,
        }

    def _resolve_env_object(self, object_name: str) -> Optional[Any]:
        if self.env is None:
            return None
        candidates = [
            object_name,
            object_name.strip(),
            object_name.replace(" ", "_"),
            object_name.replace("_", " "),
        ]
        object_scope = getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        for candidate in candidates:
            obj_ref = object_scope.get(candidate)
            if obj_ref is not None:
                return getattr(obj_ref, "wrapped_obj", obj_ref)
        normalized = _normalize_caption(object_name)
        for _, obj_ref in object_scope.items():
            obj = getattr(obj_ref, "wrapped_obj", obj_ref)
            names = [
                getattr(obj, "name", None),
                getattr(obj, "category", None),
                getattr(obj_ref, "name", None),
            ]
            if any(_normalize_caption(name) == normalized for name in names if name):
                return obj
        scene = getattr(self.env, "scene", None)
        registry = getattr(scene, "object_registry", None)
        if registry is not None:
            for candidate in candidates:
                try:
                    obj = registry("name", candidate)
                except Exception:
                    obj = None
                if obj is not None:
                    return obj
        return None

    def _object_position(self, obj: Optional[Any]) -> Optional[List[float]]:
        if obj is None:
            return None
        try:
            position = obj.get_position_orientation()[0]
        except Exception:
            return None
        if hasattr(position, "detach"):
            position = position.detach().cpu()
        if hasattr(position, "tolist"):
            position = position.tolist()
        try:
            values = [float(item) for item in position[:3]]
        except Exception:
            return None
        return values

    def _source_history(self, map_object: Any) -> List[str]:
        history = []
        image_indices = map_object.get("image_idx", [])
        mask_indices = map_object.get("mask_idx", [])
        for image_index, mask_index in zip(image_indices, mask_indices):
            if image_index < 0 or image_index >= len(self.graph.segment2d_results):
                continue
            source_ids = self.graph.segment2d_results[image_index].get("source_object_id", [])
            if 0 <= mask_index < len(source_ids):
                history.append(str(source_ids[mask_index]))
        return history

    def _track_key_for_map_object(self, map_object: Any) -> Optional[str]:
        for track_key, tracked_object in self.track_to_map_object.items():
            if tracked_object is map_object:
                return track_key
        return None

    def _stable_id(self, node: Any) -> str:
        if node not in self.node_ids:
            self.node_ids[node] = f"unigoal_object:{self.next_node_id}"
            self.next_node_id += 1
        return self.node_ids[node]

    def _perceived_objects(
        self,
        segment_index: int,
        source_to_node: Dict[str, Any],
        frame_object_by_id: Dict[str, PerceivedObject],
        moving_source_ids: Set[str],
    ) -> List[PerceivedObject]:
        current_node_to_source = {node: source_id for source_id, node in source_to_node.items()}
        objects = []
        for node in self.graph.nodes:
            map_object = node.object
            image_indices = list(map_object.get("image_idx", []))
            if not image_indices:
                continue
            latest_offset = max(range(len(image_indices)), key=image_indices.__getitem__)
            latest_segment = image_indices[latest_offset]
            source_id = current_node_to_source.get(node)
            frame_object = frame_object_by_id.get(source_id)
            visible = source_id is not None
            position = [float(value) for value in map_object["bbox"].get_center()]
            room_id = node.room_node.caption if node.room_node is not None else None

            bbox = to_builtin(map_object.get("xyxy", [None])[latest_offset])
            mask = map_object.get("mask", [None])[latest_offset] if visible else None
            confidence = float(map_object.get("conf", [1.0])[latest_offset])
            bbox_3d = np.asarray(map_object["bbox"].get_box_points()).tolist()
            frame_history = [self.segment_frame_indices[index] for index in image_indices]
            source_history = self._source_history(map_object)
            track_key = (
                self._track_key(frame_object)
                if frame_object is not None
                else self._track_key_for_map_object(map_object)
            )
            stable_object_id = self._stable_id(node)
            uid = self._node_uid(node)
            lifelong_label = _normalize_caption(node.caption or "object")
            attributes = {
                "source": "samjam_unigoal",
                "uid": uid,
                "stable_object_id": stable_object_id,
                "lifelong_label": lifelong_label,
                "normalized_label": lifelong_label,
                "is_coarse": bool(getattr(node, "is_coarse", _is_coarse_caption(lifelong_label))),
                "is_vis": bool(getattr(node, "is_vis", visible)),
                "last_seen_step": int(getattr(node, "last_seen_step", latest_segment)),
                "currently_visible": visible,
                "first_seen_frame": min(frame_history),
                "last_seen_frame": max(frame_history),
                "seen_count": int(map_object.get("num_detections", len(image_indices))),
                "caption_history": list(map_object.get("class_name", [])),
                "source_object_history": source_history,
                "source_object_id": source_id,
                "samjam_track_id": track_key,
                "bbox_3d": bbox_3d,
                "point_count": len(map_object["pcd"].points),
                "is_moving": bool(frame_object and source_id in moving_source_ids),
                "is_moved": bool(
                    self.node_moved.get(node, False)
                    or (track_key is not None and self.track_moved.get(track_key, False))
                ),
                "mask_area": int(np.asarray(mask, dtype=bool).sum()) if mask is not None else 0,
                "latest_mapping_segment": latest_segment,
            }
            if frame_object is not None:
                attributes.update(
                    {
                        "samjam_id": frame_object.attributes.get("samjam_id"),
                        "is_hand": bool(frame_object.attributes.get("is_hand", False)),
                        "vlm_raw_name": frame_object.attributes.get("vlm_raw_name"),
                    }
                )
            objects.append(
                PerceivedObject(
                    object_id=stable_object_id,
                    name=str(lifelong_label or node.caption or "object"),
                    category=_category(lifelong_label or node.caption or "object"),
                    bbox=bbox,
                    mask=mask,
                    position=position,
                    room_id=str(room_id or "unknown_room"),
                    confidence=confidence,
                    attributes=attributes,
                )
            )
        return sorted(objects, key=lambda obj: obj.object_id)

    def _perceived_relations(self) -> List[PerceivedRelation]:
        relations = []
        for edge in self.graph.get_edges():
            if not edge.relation:
                continue
            relation = _normalize_relation_with_direction(edge.relation)[0]
            if relation not in ALLOWED_LIFELONG_RELATIONS:
                continue
            relations.append(
                PerceivedRelation(
                    source_id=self._stable_id(edge.node1),
                    target_id=self._stable_id(edge.node2),
                    relation=str(relation),
                    confidence=1.0,
                    source="samjam:vlm+unigoal:geometry+lifelong",
                )
            )
        return sorted(
            relations,
            key=lambda rel: (rel.source_id, rel.relation, rel.target_id),
        )

    def _lifelong_scene_graph(self) -> Dict[str, Any]:
        caption_count: Dict[str, int] = {}
        node_id_map: Dict[Any, str] = {}
        nodes = []
        for node in self.graph.nodes:
            label = _normalize_caption(getattr(node, "caption", None))
            index = caption_count.get(label, 0)
            caption_count[label] = index + 1
            native_id = f"{_category(label)}_{index}"
            node_id_map[node] = native_id
            nodes.append(
                {
                    "id": native_id,
                    "uid": self._node_uid(node),
                    "object_id": self._stable_id(node),
                    "label": label,
                    "position": self._node_position(node),
                    "is_vis": bool(getattr(node, "is_vis", False)),
                    "is_coarse": bool(getattr(node, "is_coarse", True)),
                    "last_seen_step": int(getattr(node, "last_seen_step", -1)),
                }
            )
        edges = []
        for edge in self.graph.get_edges():
            if not edge.relation or edge.node1 not in node_id_map or edge.node2 not in node_id_map:
                continue
            relation = _normalize_relation_with_direction(edge.relation)[0] or str(edge.relation)
            if relation not in ALLOWED_LIFELONG_RELATIONS:
                continue
            edges.append(
                {
                    "source": node_id_map[edge.node1],
                    "target": node_id_map[edge.node2],
                    "source_uid": self._node_uid(edge.node1),
                    "target_uid": self._node_uid(edge.node2),
                    "source_object_id": self._stable_id(edge.node1),
                    "target_object_id": self._stable_id(edge.node2),
                    "type": relation,
                }
            )
        return {"nodes": nodes, "edges": edges}

    def _lifelong_summary(self, scene_graph: Dict[str, Any]) -> Dict[str, Any]:
        nodes = scene_graph.get("nodes", [])
        edges = scene_graph.get("edges", [])
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
            "manipulation_resolution_count": len(self.manipulation_resolutions),
        }

    def _room_graph(self) -> Dict[str, Any]:
        rooms = []
        for room_node in self.graph.room_nodes:
            object_ids = sorted(self._stable_id(node) for node in room_node.nodes)
            if not object_ids:
                continue
            rooms.append(
                {
                    "id": room_node.caption,
                    "caption": room_node.caption,
                    "objects": object_ids,
                    "object_count": len(object_ids),
                    "group_count": len(room_node.group_nodes),
                }
            )
        return {"rooms": rooms}

    def _group_graph(self) -> Dict[str, Any]:
        groups = []
        for room_node in self.graph.room_nodes:
            for index, group_node in enumerate(room_node.group_nodes):
                object_ids = sorted(self._stable_id(node) for node in group_node.nodes)
                digest = hashlib.sha1("|".join(object_ids).encode("utf-8")).hexdigest()[:10]
                groups.append(
                    {
                        "id": f"unigoal:{room_node.caption}:{digest}",
                        "room": room_node.caption,
                        "caption": group_node.caption,
                        "center": to_builtin(group_node.center),
                        "center_object_id": (
                            self._stable_id(group_node.center_node)
                            if group_node.center_node is not None
                            else None
                        ),
                        "objects": object_ids,
                        "group_index": index,
                    }
                )
        return {"groups": groups}


class SAMJAMUniGoalBackend:
    name = "samjam_unigoal"

    def __init__(self, sensor_name: Optional[str] = None):
        from .samjam_sam2 import SAMJAMSAM2Backend

        self.samjam_backend = SAMJAMSAM2Backend(sensor_name=sensor_name)
        self.unigoal_adapter = SAMJAMUniGoalGraphAdapter()
        self.env = None
        self.last_frame: Optional[FrameObservation] = None
        self.last_samjam_result: Optional[PerceptionResult] = None
        self.last_result: Optional[PerceptionResult] = None

    def reset(self, env: Any) -> None:
        self.env = env
        self.samjam_backend.reset(env)
        self.unigoal_adapter.reset(room_lookup=self.samjam_backend.room_lookup)
        self.unigoal_adapter.set_env(env)
        self.last_frame = None
        self.last_samjam_result = None
        self.last_result = None

    def note_manipulation_event(self, event: Dict[str, Any]) -> None:
        self.unigoal_adapter.note_manipulation_event(event)

    def mark_manipulated_nodes(self, node_uids: List[int]) -> None:
        self.unigoal_adapter.mark_manipulated_nodes(node_uids)

    def observe(self, env: Any) -> FrameObservation:
        return self.samjam_backend.observe(env)

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        result = self.samjam_backend.detect(frame)
        self.last_frame = frame
        self.last_samjam_result = result
        return result

    def update_memory(self, result: PerceptionResult) -> PerceptionResult:
        if self.last_frame is None:
            raise RuntimeError("SAMJAM-UniGoal backend requires a preceding detect call")
        samjam_objects = list(result.objects)
        samjam_relations = list(result.relations)
        mapped = self.unigoal_adapter.update(
            frame=self.last_frame,
            frame_objects=samjam_objects,
            frame_relations=samjam_relations,
        )
        result.backend = self.name
        result.objects = mapped.objects
        result.relations = mapped.relations
        result.scene_graph = mapped.scene_graph
        result.room_graph = mapped.room_graph
        result.group_graph = mapped.group_graph
        result.metadata.update(mapped.metadata)
        samjam_current_count = sum(
            1
            for obj in samjam_objects
            if obj.mask is not None and obj.attributes.get("currently_visible", True)
        )
        result.metadata.update(
            {
                "samjam_backend": self.samjam_backend.name,
                "samjam_metadata": (
                    {} if self.last_samjam_result is None else dict(self.last_samjam_result.metadata)
                ),
                "samjam_object_count": len(samjam_objects),
                "samjam_total_native_object_count": len(samjam_objects),
                "samjam_current_object_count": samjam_current_count,
                "samjam_relation_count": len(samjam_relations),
                "current_frame_object_count": mapped.metadata["visible_object_count"],
                "persistent_object_count": len(result.objects),
                "visible_persistent_object_count": mapped.metadata["visible_object_count"],
                "visible_only_scene_graph": False,
            }
        )
        self._write_samjam_debug_outputs(
            result,
            samjam_objects=samjam_objects,
            samjam_relations=samjam_relations,
        )
        self.last_result = result
        return result

    def _write_samjam_debug_outputs(
        self,
        result: PerceptionResult,
        samjam_objects: List[PerceivedObject],
        samjam_relations: List[PerceivedRelation],
    ) -> None:
        writer = self.samjam_backend.output_writer
        pending_debug = self.samjam_backend.pending_debug
        if writer is None or pending_debug is None:
            return
        try:
            writer.write(
                frame=pending_debug["frame"],
                vlm_scene_graph=pending_debug.get("vlm_scene_graph"),
                candidates=pending_debug.get("candidates", []),
                objects=samjam_objects,
                relations=samjam_relations,
            )
            result.metadata["samjam_output_dir"] = str(writer.output_dir)
        except Exception as exc:
            result.metadata["samjam_output_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
