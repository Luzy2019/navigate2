import contextlib
import hashlib
import io
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)

from .utils import insert_sys_path, repo_root, to_builtin

_ACTIVE_SCENE_GRAPH_CONFIG = SceneGraphConfig()


def _set_scene_graph_config(config: Optional[SceneGraphConfig]) -> SceneGraphConfig:
    global _ACTIVE_SCENE_GRAPH_CONFIG
    _ACTIVE_SCENE_GRAPH_CONFIG = config or SceneGraphConfig()
    return _ACTIVE_SCENE_GRAPH_CONFIG


def _cfg() -> SceneGraphConfig:
    return _ACTIVE_SCENE_GRAPH_CONFIG


def _env_float(name: str, default: float) -> float:
    return _cfg().option_float(name, default)


def _env_int(name: str, default: int) -> int:
    return _cfg().option_int(name, default)


def _env_bool(name: str, default: bool) -> bool:
    return _cfg().option_bool(name, default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if np.isfinite(value) else default


def _match_detail_effective_score(match_detail: Optional[Dict[str, Any]]) -> float:
    if match_detail is None:
        return 0.0
    match_iou = _safe_float(match_detail.get("best_iou"), 0.0)
    if match_detail.get("accepted") is False:
        return match_iou
    return max(match_iou, _safe_float(match_detail.get("best_score"), match_iou))


def _debug_log_path() -> Path:
    explicit = _cfg().debug_log_path
    if explicit:
        return Path(explicit)
    output_dir = _cfg().output_dir or _cfg().option("ISBENCH_SAMJAM_OUTPUT_DIR")
    if output_dir:
        return Path(output_dir) / "scene_graph_debug.log"
    return Path("scene_graph_debug.log")


def _append_debug_log(lines: List[str]) -> None:
    if not lines:
        return
    path = _debug_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(f"{line}\n")


def _suppress_vendor_output_enabled() -> bool:
    return bool(_cfg().suppress_vendor_output) and _env_bool(
        "ISBENCH_UNIGOAL_SUPPRESS_VENDOR_OUTPUT", True
    )


@contextlib.contextmanager
def _maybe_suppress_vendor_output():
    if not _suppress_vendor_output_enabled():
        yield
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield


def _category(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return value or "object"


COARSE_VOCAB = {
    "table", "chair", "cabinet", "sofa", "bed", "window", "mirror",
    "curtain", "painting", "picture", "plant", "tv", "shelf", "desk",
    "dresser", "nightstand", "counter", "stool", "toilet", "sink",
    "bathtub", "shower", "refrigerator", "oven", "stove", "microwave",
    "trash can", "bin", "door", "top compartment of the cabinet",
    "rack over the sink",
}
FINE_VOCAB = {
    "apple", "banana", "book", "bottle", "bowl", "box", "clock", "cloth",
    "cup", "fork", "knife", "lamp", "laptop", "lighter", "mug", "paper",
    "phone", "pillow", "plate", "potato", "rag", "remote", "soap", "sponge",
    "spoon", "tennis ball", "tissue box", "tofu", "tomato", "towel", "vase",
    "vegetable", "vegetables", "ring shaped bread", "crumpled white plastic waste",
    "cleaning spray bottle", "folded towel", "long bread loaf", "pan",
}
UNKNOWN_OBJECT_NAME = "unknown_object"
CAPTION_ALIAS = {
    "black slove": "stove",
    "black stove": "stove",
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
    "half banana": "banana",
    "half_banana": "banana",
    "mugs": "mug",
    "night stand": "nightstand",
    "pillows": "pillow",
    "plates": "plate",
    "potted plant": "plant",
    "tennis_ball": "tennis ball",
    "tissue box box": "tissue box",
    "tissue_box": "tissue box",
    "trash bin": "bin",
    "unknown object": UNKNOWN_OBJECT_NAME,
    "unknown_object": UNKNOWN_OBJECT_NAME,
    "tv monitor": "tv",
    "tv_monitor": "tv",
    "windows": "window",
}
CANONICAL_OBJECT_VOCAB = COARSE_VOCAB | FINE_VOCAB
NODE_SPACE_TERMS = sorted(CANONICAL_OBJECT_VOCAB, key=len, reverse=True)
ATTACHABLE_COARSE_VOCAB = {
    "bed", "sofa", "table", "desk", "nightstand", "shelf", "counter",
    "sink", "bathtub", "shower", "cabinet", "dresser", "refrigerator",
    "stove", "oven", "microwave", "trash can", "bin",
    "top compartment of the cabinet", "rack over the sink",
}
SUPPORT_SURFACE_VOCAB = {
    "bed", "sofa", "table", "desk", "nightstand", "shelf", "counter",
    "stove", "oven", "microwave", "rack over the sink",
}
CONTAINER_COARSE_VOCAB = {
    "cabinet", "dresser", "refrigerator", "sink", "bathtub", "shower",
    "trash can", "bin", "top compartment of the cabinet",
}
COARSE_NEAR_OBSTACLE_VOCAB = {
    "table", "chair", "cabinet", "sofa", "bed", "shelf", "desk",
    "dresser", "nightstand", "counter", "stool", "toilet", "sink",
    "bathtub", "shower", "refrigerator", "oven", "stove", "microwave",
    "plant", "trash can", "bin", "top compartment of the cabinet",
    "rack over the sink",
}
LARGE_MASK_COARSE_FILTER_VOCAB = {
    "cabinet", "counter", "door", "dresser", "refrigerator", "sink",
    "stove", "oven", "microwave", "table", "shelf",
    "top compartment of the cabinet", "rack over the sink",
}
FINE_PARENT_RELATION_PRIORITY = {"on": 4, "in": 3, "attach to": 2, "above": 1}
ALLOWED_FINE_FINE_RELATIONS = {"on", "in", "above", "near", "attach to"}
ALLOWED_FINE_COARSE_RELATIONS = {"on", "in", "above", "attach to"}
ALLOWED_COARSE_COARSE_RELATIONS = {"near"}
ALLOWED_LIFELONG_RELATIONS = (
    ALLOWED_FINE_FINE_RELATIONS
    | ALLOWED_FINE_COARSE_RELATIONS
    | ALLOWED_COARSE_COARSE_RELATIONS
)


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


def _canonical_or_unknown_caption(caption: Any) -> str:
    normalized = _normalize_caption(caption)
    if normalized in CANONICAL_OBJECT_VOCAB or normalized == UNKNOWN_OBJECT_NAME:
        return normalized
    return UNKNOWN_OBJECT_NAME


def _is_unknown_caption(caption: Any) -> bool:
    return _canonical_or_unknown_caption(caption) == UNKNOWN_OBJECT_NAME


def _is_coarse_caption(caption: Any) -> bool:
    return _canonical_or_unknown_caption(caption) not in FINE_VOCAB


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


def _normalize_relation_for_type_pair(
    source_is_coarse: bool,
    target_is_coarse: bool,
    relation: Any,
) -> Optional[Tuple[str, bool]]:
    normalized, reverse = _normalize_relation_with_direction(relation)
    if normalized is None:
        return None
    if reverse:
        source_is_coarse, target_is_coarse = target_is_coarse, source_is_coarse

    if source_is_coarse and target_is_coarse:
        if normalized not in ALLOWED_COARSE_COARSE_RELATIONS:
            return None
    elif (not source_is_coarse) and (not target_is_coarse):
        if normalized not in ALLOWED_FINE_FINE_RELATIONS:
            return None
    else:
        if normalized not in ALLOWED_FINE_COARSE_RELATIONS:
            return None
        if source_is_coarse:
            return None
    return normalized, reverse


def _normalize_relation_for_node_pair(
    source_node: Any,
    target_node: Any,
    relation: Any,
) -> Optional[Tuple[Any, Any, str]]:
    result = _normalize_relation_for_type_pair(
        source_is_coarse=bool(getattr(source_node, "is_coarse", True)),
        target_is_coarse=bool(getattr(target_node, "is_coarse", True)),
        relation=relation,
    )
    if result is None:
        return None
    normalized, reverse = result
    if reverse:
        source_node, target_node = target_node, source_node
    return source_node, target_node, normalized


@dataclass
class UniGoalMappedGraph:
    objects: List[PerceivedObject]
    relations: List[PerceivedRelation]
    scene_graph: Dict[str, Any]
    room_graph: Dict[str, Any]
    group_graph: Dict[str, Any]
    metadata: Dict[str, Any]


@dataclass
class SAMJAMFilterResult:
    objects: List[PerceivedObject]
    relations: List[PerceivedRelation]
    report: Dict[str, Any]


class SAMJAMUniGoalGraphAdapter:
    """Feed SAMJAM masks and labels into UniGoal's native 3D graph pipeline."""

    def __init__(
        self,
        room_lookup: Optional[Callable[[List[float]], str]] = None,
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ):
        self.scene_graph_config = _set_scene_graph_config(scene_graph_config)
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
        # Canonical scene-graph ids are obj_0001, obj_0002, ...; uid=0 is
        # reserved for malformed/unknown legacy inputs by schema.py.
        self.next_node_uid = 1
        self.manipulated_node_uids: Set[int] = set()
        self.pending_manipulation_events: List[Dict[str, Any]] = []
        self.manipulation_event_history: List[Dict[str, Any]] = []
        self.manipulation_resolutions: List[Dict[str, Any]] = []
        self.edge_update_events: List[Dict[str, Any]] = []
        self.last_mapping_debug: Optional[Dict[str, Any]] = None
        self.name_vote_scores: Dict[str, Dict[str, float]] = {}
        self.name_vote_current: Dict[str, str] = {}

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
        self.next_node_uid = 1
        self.manipulated_node_uids.clear()
        self.pending_manipulation_events.clear()
        self.manipulation_event_history.clear()
        self.manipulation_resolutions.clear()
        self.edge_update_events.clear()
        self.last_mapping_debug = None
        self.name_vote_scores.clear()
        self.name_vote_current.clear()

    def set_env(self, env: Any) -> None:
        self.env = env

    def stable_name_for_samjam_object(
        self,
        obj: PerceivedObject,
        match_detail: Optional[Dict[str, Any]],
    ) -> Tuple[str, Dict[str, Any]]:
        track_key = self._track_key(obj) or str(obj.object_id)
        raw_frame_name = _normalize_caption(
            (match_detail or {}).get("canonical_name")
            or (match_detail or {}).get("vlm_name")
            or obj.name
        )
        frame_name = _canonical_or_unknown_caption(raw_frame_name)
        weight = self._name_vote_weight(obj, match_detail)
        scores = self.name_vote_scores.setdefault(track_key, {})
        scores[frame_name] = scores.get(frame_name, 0.0) + weight

        current = self.name_vote_current.get(track_key)
        best_score = max(scores.values())
        best_names = {
            name for name, score in scores.items() if score == best_score
        }
        if frame_name in best_names and frame_name != UNKNOWN_OBJECT_NAME:
            best_name = frame_name
        elif current in best_names:
            best_name = current
        else:
            best_name = sorted(best_names)[0]
        margin = _env_float("ISBENCH_SAMJAM_UNIGOAL_NAME_SWITCH_MARGIN", 1.25)
        current_score = scores.get(current, 0.0) if current else 0.0
        if current is None:
            current = best_name
            self.name_vote_current[track_key] = current
        elif current != UNKNOWN_OBJECT_NAME and best_name == UNKNOWN_OBJECT_NAME:
            pass
        elif current == UNKNOWN_OBJECT_NAME and best_name != UNKNOWN_OBJECT_NAME:
            current = best_name
            self.name_vote_current[track_key] = current
        elif best_score >= current_score * margin:
            current = best_name
            self.name_vote_current[track_key] = current
        elif (
            best_score == current_score
            and frame_name == best_name
            and best_name != UNKNOWN_OBJECT_NAME
        ):
            current = best_name
            self.name_vote_current[track_key] = current

        return current, {
            "track_key": track_key,
            "raw_frame_name": raw_frame_name,
            "frame_name": frame_name,
            "stable_name": current,
            "weight": float(weight),
            "scores": {name: round(score, 4) for name, score in sorted(scores.items())},
        }

    def _name_vote_weight(
        self,
        obj: PerceivedObject,
        match_detail: Optional[Dict[str, Any]],
    ) -> float:
        match_iou = _safe_float((match_detail or {}).get("best_iou"), 0.0)
        match_score = self._match_detail_effective_score(match_detail)
        visual_confidence = max(
            _safe_float(obj.attributes.get("predicted_iou"), 0.0),
            _safe_float(obj.attributes.get("stability_score"), 0.0),
            _safe_float(obj.confidence, 0.0),
            0.01,
        )
        return max(match_iou, match_score, 0.01) * visual_confidence

    def _match_detail_effective_score(self, match_detail: Optional[Dict[str, Any]]) -> float:
        return _match_detail_effective_score(match_detail)

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
        with _maybe_suppress_vendor_output():
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
        with _maybe_suppress_vendor_output():
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
                    "+samjam_track_reassociation"
                    "+semantic_merge_gate"
                ),
                "semantic_identity_matching": True,
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
                "edge_update_events": list(self.edge_update_events[-50:]),
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
                "unigoal_mapping_debug": self.last_mapping_debug
                if self._debug_matching_enabled()
                else None,
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
            device=_cfg().device or ("cuda" if torch.cuda.is_available() else "cpu"),
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

    def _debug_matching_enabled(self) -> bool:
        return (
            _env_bool("ISBENCH_SCENE_GRAPH_DEBUG_MATCHING", False)
            or _env_bool("ISBENCH_SAMJAM_DEBUG_MATCHING", False)
            or _env_bool("ISBENCH_UNIGOAL_MAPPING_DEBUG", False)
            or _env_bool("ISBENCH_SAMJAM_UNIGOAL_DEBUG_MAPPING", False)
            or bool(_cfg().debug_log_path)
            or bool(_cfg().output_debug_matching)
        )

    def _debug_max_items(self) -> int:
        return _env_int("ISBENCH_SCENE_GRAPH_DEBUG_MAX_ITEMS", 40)

    def _debug_first(self, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    def _debug_float_list(self, value: Any, ndigits: int = 4) -> Optional[List[float]]:
        if value is None:
            return None
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "tolist"):
            value = value.tolist()
        try:
            return [round(float(item), ndigits) for item in value]
        except Exception:
            return None

    def _debug_matrix(self, matrix: Any) -> List[List[float]]:
        if matrix is None:
            return []
        if hasattr(matrix, "detach"):
            matrix = matrix.detach().cpu()
        if hasattr(matrix, "numpy"):
            matrix = matrix.numpy()
        matrix = np.asarray(matrix)
        rows = []
        for row in matrix:
            rows.append([float(value) for value in row])
        return rows

    def _source_track_key(self, source_id: Optional[Any]) -> Optional[str]:
        if source_id is None:
            return None
        text = str(source_id)
        match = re.match(r"samjam_object:(\d+)$", text)
        if match:
            return f"samjam:{match.group(1)}"
        return text or None

    def _source_id_for_detection(
        self,
        detection: Any,
        gobs: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if gobs is None:
            return None
        mask_idx = self._debug_first(detection.get("mask_idx"))
        source_ids = gobs.get("source_object_id", [])
        try:
            if 0 <= int(mask_idx) < len(source_ids):
                return str(source_ids[int(mask_idx)])
        except Exception:
            return None
        return None

    def _canonical_detection_name(self, detection: Any) -> str:
        return _canonical_or_unknown_caption(self._debug_first(detection.get("class_name")))

    def _map_object_canonical_names(self, map_object: Any) -> Set[str]:
        names = {
            _canonical_or_unknown_caption(item)
            for item in map_object.get("class_name", [])
            if item is not None
        }
        node = map_object.get("node")
        if node is not None:
            names.add(_canonical_or_unknown_caption(getattr(node, "caption", None)))
        names.discard("")
        return names or {UNKNOWN_OBJECT_NAME}

    def _map_object_track_keys(self, map_object: Any) -> Set[str]:
        keys = set()
        direct = self._track_key_for_map_object(map_object)
        if direct:
            keys.add(direct)
        for source_id in self._source_history(map_object):
            key = self._source_track_key(source_id)
            if key:
                keys.add(key)
        return keys

    def _map_object_center_distance(self, first: Any, second: Any) -> Optional[float]:
        try:
            first_center = np.asarray(first["bbox"].get_center(), dtype=np.float64)
            second_center = np.asarray(second["bbox"].get_center(), dtype=np.float64)
        except Exception:
            return None
        if first_center.size == 0 or second_center.size == 0:
            return None
        if not np.isfinite(first_center).all() or not np.isfinite(second_center).all():
            return None
        dim = min(first_center.size, second_center.size, 3)
        return float(np.linalg.norm(first_center[:dim] - second_center[:dim]))

    def _names_semantically_compatible(
        self,
        detection_name: str,
        object_names: Set[str],
        *,
        same_track: bool = False,
    ) -> Tuple[bool, str]:
        if detection_name in object_names:
            return True, "same_canonical_name"
        if detection_name == UNKNOWN_OBJECT_NAME and object_names == {UNKNOWN_OBJECT_NAME}:
            return True, "both_unknown"
        if same_track:
            if detection_name == UNKNOWN_OBJECT_NAME or UNKNOWN_OBJECT_NAME in object_names:
                return True, "same_track_unknown_alias"
            return False, "same_track_incompatible_known_names"
        return False, "incompatible_names"

    def _semantic_gate_similarity(
        self,
        spatial_sim: Any,
        detection_list: Any,
        objects: Any,
        gobs: Optional[Dict[str, Any]],
    ) -> Tuple[Any, List[Dict[str, Any]]]:
        gated = spatial_sim.clone() if hasattr(spatial_sim, "clone") else np.array(spatial_sim, copy=True)
        rejects: List[Dict[str, Any]] = []
        same_track_min_sim = _env_float("ISBENCH_SAMJAM_UNIGOAL_SAME_TRACK_MIN_SIM", 0.02)
        for det_index, detection in enumerate(detection_list):
            detection_name = self._canonical_detection_name(detection)
            source_id = self._source_id_for_detection(detection, gobs)
            source_track = self._source_track_key(source_id)
            for obj_index, map_object in enumerate(objects):
                try:
                    score = float(gated[det_index, obj_index])
                except Exception:
                    continue
                object_names = self._map_object_canonical_names(map_object)
                object_tracks = self._map_object_track_keys(map_object)
                same_track = source_track is not None and source_track in object_tracks
                compatible, reason = self._names_semantically_compatible(
                    detection_name,
                    object_names,
                    same_track=same_track,
                )
                if same_track and compatible and score < same_track_min_sim:
                    compatible = False
                    reason = "same_track_low_spatial_similarity"
                if compatible:
                    continue
                gated[det_index, obj_index] = float("-inf")
                rejects.append(
                    {
                        "detection_index": det_index,
                        "object_index": obj_index,
                        "source_object_id": source_id,
                        "source_track": source_track,
                        "detection_name": detection_name,
                        "object_names": sorted(object_names),
                        "object_tracks": sorted(object_tracks),
                        "score": score,
                        "reason": reason,
                    }
                )
        return gated, rejects

    def _map_object_debug_summary(self, map_object: Any, index: Optional[int] = None) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "index": index,
            "class_name": list(map_object.get("class_name", [])),
            "image_idx": list(map_object.get("image_idx", [])),
            "mask_idx": list(map_object.get("mask_idx", [])),
            "num_detections": int(map_object.get("num_detections", 0) or 0),
            "source_history": self._source_history(map_object) if self.graph is not None else [],
            "track_key": self._track_key_for_map_object(map_object),
        }
        node = map_object.get("node")
        if node is not None:
            summary.update(
                {
                    "node_caption": getattr(node, "caption", None),
                    "node_uid": getattr(node, "uid", self.node_uids.get(node)),
                    "stable_object_id": self.node_ids.get(node),
                }
            )
        pcd = map_object.get("pcd")
        if pcd is not None:
            try:
                summary["point_count"] = int(len(pcd.points))
            except Exception:
                summary["point_count"] = None
        bbox = map_object.get("bbox")
        if bbox is not None:
            try:
                summary["bbox_center"] = self._debug_float_list(bbox.get_center())
            except Exception:
                summary["bbox_center"] = None
            try:
                summary["bbox_extent"] = self._debug_float_list(bbox.get_extent())
            except Exception:
                summary["bbox_extent"] = None
            try:
                summary["bbox_volume"] = float(bbox.volume())
            except Exception:
                summary["bbox_volume"] = None
        return summary

    def _detection_debug_summary(
        self,
        detection: Any,
        gobs: Optional[Dict[str, Any]] = None,
        index: Optional[int] = None,
    ) -> Dict[str, Any]:
        mask_idx = self._debug_first(detection.get("mask_idx"))
        source_object_id = None
        if gobs is not None and mask_idx is not None:
            source_ids = gobs.get("source_object_id", [])
            try:
                if 0 <= int(mask_idx) < len(source_ids):
                    source_object_id = source_ids[int(mask_idx)]
            except Exception:
                source_object_id = None
        summary = {
            "index": index,
            "source_object_id": source_object_id,
            "class_name": self._debug_first(detection.get("class_name")),
            "mask_idx": mask_idx,
            "image_idx": self._debug_first(detection.get("image_idx")),
            "confidence": self._debug_first(detection.get("conf")),
            "pixel_area": self._debug_first(detection.get("pixel_area")),
            "point_count": self._debug_first(detection.get("n_points")),
            "xyxy": to_builtin(self._debug_first(detection.get("xyxy"))),
        }
        bbox = detection.get("bbox")
        if bbox is not None:
            try:
                summary["bbox_center"] = self._debug_float_list(bbox.get_center())
            except Exception:
                summary["bbox_center"] = None
            try:
                summary["bbox_extent"] = self._debug_float_list(bbox.get_extent())
            except Exception:
                summary["bbox_extent"] = None
        return summary

    def _build_mapping_debug_decisions(
        self,
        detections: Any,
        objects: Any,
        thresholded_sim: Any,
        raw_sim: List[List[float]],
        gobs: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        thresholded_rows = self._debug_matrix(thresholded_sim)
        decisions = []
        for det_index, detection in enumerate(detections):
            row = thresholded_rows[det_index] if det_index < len(thresholded_rows) else []
            raw_row = raw_sim[det_index] if det_index < len(raw_sim) else []
            finite_scores = [
                (obj_index, score)
                for obj_index, score in enumerate(row)
                if np.isfinite(score)
            ]
            raw_top_scores = sorted(
                [
                    {
                        "object_index": obj_index,
                        "score": float(score),
                    }
                    for obj_index, score in enumerate(raw_row)
                    if np.isfinite(score)
                ],
                key=lambda item: item["score"],
                reverse=True,
            )[: self._debug_max_items()]
            if finite_scores:
                matched_index, matched_score = max(finite_scores, key=lambda item: item[1])
                action = "merge"
                matched_object = (
                    self._map_object_debug_summary(objects[matched_index], matched_index)
                    if matched_index < len(objects)
                    else None
                )
            else:
                matched_index = None
                matched_score = None
                action = "new_object"
                matched_object = None
            decisions.append(
                {
                    "detection": self._detection_debug_summary(detection, gobs, det_index),
                    "action": action,
                    "matched_object_index": matched_index,
                    "matched_score_after_threshold": None
                    if matched_score is None
                    else float(matched_score),
                    "matched_object": matched_object,
                    "raw_top_scores": raw_top_scores,
                }
            )
        return decisions

    def _write_unigoal_mapping_log(self, payload: Dict[str, Any]) -> None:
        if not self._debug_matching_enabled():
            return
        max_items = self._debug_max_items()
        lines = [
            "[ISBench][UniGoalMapping] "
            f"frame={payload.get('frame_index')} segment={payload.get('segment_index')} "
            f"detections={len(payload.get('detections', []))} "
            f"objects_before={len(payload.get('objects_before', []))} "
            f"objects_after={len(payload.get('objects_after', []))} "
            f"threshold={payload.get('spatial_threshold')} "
            f"semantic_rejects={len(payload.get('semantic_gate_rejects', []))}"
        ]
        for detection in payload.get("detections", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalMapping][detection] "
                f"idx={detection.get('index')} source={detection.get('source_object_id')} "
                f"class={detection.get('class_name')} xyxy={detection.get('xyxy')} "
                f"points={detection.get('point_count')} center={detection.get('bbox_center')}"
            )
        for obj in payload.get("objects_before", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalMapping][object_before] "
                f"idx={obj.get('index')} caption={obj.get('node_caption')} "
                f"stable={obj.get('stable_object_id')} uid={obj.get('node_uid')} "
                f"class_history={obj.get('class_name')} sources={obj.get('source_history')} "
                f"detections={obj.get('num_detections')} center={obj.get('bbox_center')}"
            )
        for decision in payload.get("merge_decisions", [])[:max_items]:
            detection = decision.get("detection", {})
            matched = decision.get("matched_object") or {}
            lines.append(
                "[ISBench][UniGoalMapping][decision] "
                f"det_idx={detection.get('index')} source={detection.get('source_object_id')} "
                f"class={detection.get('class_name')} action={decision.get('action')} "
                f"matched_idx={decision.get('matched_object_index')} "
                f"score={decision.get('matched_score_after_threshold')} "
                f"matched_caption={matched.get('node_caption')} "
                f"matched_sources={matched.get('source_history')} "
                f"raw_top={decision.get('raw_top_scores')}"
            )
        for reject in payload.get("semantic_gate_rejects", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalMapping][semantic_gate_reject] "
                f"det_idx={reject.get('detection_index')} "
                f"obj_idx={reject.get('object_index')} "
                f"source={reject.get('source_object_id')} "
                f"class={reject.get('detection_name')} "
                f"object_names={reject.get('object_names')} "
                f"score={reject.get('score')} "
                f"reason={reject.get('reason')}"
            )
        for obj in payload.get("objects_after", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalMapping][object_after] "
                f"idx={obj.get('index')} caption={obj.get('node_caption')} "
                f"stable={obj.get('stable_object_id')} uid={obj.get('node_uid')} "
                f"class_history={obj.get('class_name')} sources={obj.get('source_history')} "
                f"detections={obj.get('num_detections')} center={obj.get('bbox_center')}"
            )
        _append_debug_log(lines)

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
        self.last_mapping_debug = None
        graph.image_rgb = np.asarray(image_rgb).copy()
        depth = np.asarray(image_depth, dtype=np.float32)
        depth = np.where(np.isfinite(depth), depth, 0.0)
        graph.image_depth = depth[..., None] if depth.ndim == 2 else depth.copy()
        graph.camera_matrix = np.asarray(camera_matrix, dtype=np.float32)
        graph.pose_matrix = np.asarray(pose_matrix, dtype=np.float32)
        graph.navigate_steps = navigate_steps
        graph.segment2d_results.append(gobs)
        graph.scenegraph_update_step += 1
        segment_index = len(graph.segment2d_results) - 1

        debug_enabled = self._debug_matching_enabled()
        debug_payload: Optional[Dict[str, Any]] = None
        graph_module = None
        originals: Dict[str, Any] = {}
        try:
            import src.graph.graph as graph_module
        except ImportError:
            graph_module = None
        if debug_enabled:
            debug_payload = {
                "frame_index": int(navigate_steps),
                "segment_index": int(segment_index),
                "spatial_threshold": float(graph.cfg.sim_threshold_spatial),
                "spatial_similarity": str(graph.cfg.spatial_sim_type),
                "objects_before": [
                    self._map_object_debug_summary(obj, index)
                    for index, obj in enumerate(list(graph.objects))
                ],
                "detections": [],
                "spatial_sim_raw": [],
                "spatial_sim_after_semantic_gate": [],
                "spatial_sim_after_threshold": [],
                "semantic_gate_rejects": [],
                "merge_decisions": [],
            }

        if graph_module is not None:
            originals = {
                "compute_spatial_similarities": graph_module.compute_spatial_similarities,
            }
            if debug_enabled:
                originals.update(
                    {
                        "gobs_to_detection_list": graph_module.gobs_to_detection_list,
                        "merge_detections_to_objects": graph_module.merge_detections_to_objects,
                    }
                )

            def _debug_gobs_to_detection_list(*args, **kwargs):
                fg_detection_list, bg_detection_list = originals["gobs_to_detection_list"](
                    *args, **kwargs
                )
                if debug_payload is not None:
                    passed_gobs = kwargs.get("gobs")
                    if passed_gobs is None and len(args) >= 6:
                        passed_gobs = args[5]
                    debug_payload["detections"] = [
                        self._detection_debug_summary(det, passed_gobs, index)
                        for index, det in enumerate(fg_detection_list)
                    ]
                return fg_detection_list, bg_detection_list

            def _debug_compute_spatial_similarities(cfg, detection_list, objects):
                spatial_sim = originals["compute_spatial_similarities"](
                    cfg, detection_list, objects
                )
                gated_sim, semantic_rejects = self._semantic_gate_similarity(
                    spatial_sim,
                    detection_list,
                    objects,
                    gobs,
                )
                if debug_payload is not None:
                    debug_payload["spatial_sim_raw"] = self._debug_matrix(spatial_sim)
                    debug_payload["spatial_sim_after_semantic_gate"] = self._debug_matrix(gated_sim)
                    debug_payload["semantic_gate_rejects"] = semantic_rejects
                    debug_payload["objects_before_similarity"] = [
                        self._map_object_debug_summary(obj, index)
                        for index, obj in enumerate(objects)
                    ]
                return gated_sim

            def _debug_merge_detections_to_objects(cfg, detection_list, objects, agg_sim):
                if debug_payload is not None:
                    debug_payload["spatial_sim_after_threshold"] = self._debug_matrix(agg_sim)
                    debug_payload["merge_decisions"] = self._build_mapping_debug_decisions(
                        detection_list,
                        objects,
                        agg_sim,
                        debug_payload.get("spatial_sim_raw", []),
                        gobs,
                    )
                merged = originals["merge_detections_to_objects"](
                    cfg, detection_list, objects, agg_sim
                )
                if debug_payload is not None:
                    debug_payload["objects_after_merge"] = [
                        self._map_object_debug_summary(obj, index)
                        for index, obj in enumerate(merged)
                    ]
                return merged

            if debug_enabled:
                graph_module.gobs_to_detection_list = _debug_gobs_to_detection_list
                graph_module.merge_detections_to_objects = _debug_merge_detections_to_objects
            graph_module.compute_spatial_similarities = _debug_compute_spatial_similarities

        try:
            with _maybe_suppress_vendor_output():
                graph.mapping3d()
        finally:
            if originals and graph_module is not None:
                graph_module.compute_spatial_similarities = originals[
                    "compute_spatial_similarities"
                ]
                if "gobs_to_detection_list" in originals:
                    graph_module.gobs_to_detection_list = originals["gobs_to_detection_list"]
                if "merge_detections_to_objects" in originals:
                    graph_module.merge_detections_to_objects = originals[
                        "merge_detections_to_objects"
                    ]

        if debug_payload is not None:
            if not debug_payload["merge_decisions"] and not debug_payload["objects_before"]:
                debug_payload["merge_decisions"] = [
                    {
                        "detection": detection,
                        "action": "initial_add",
                        "matched_object_index": None,
                        "matched_score_after_threshold": None,
                        "matched_object": None,
                        "raw_top_scores": [],
                    }
                    for detection in debug_payload["detections"]
                ]
            debug_payload["objects_after"] = [
                self._map_object_debug_summary(obj, index)
                for index, obj in enumerate(list(graph.objects))
            ]
            debug_payload["objects_post"] = [
                self._map_object_debug_summary(obj, index)
                for index, obj in enumerate(list(graph.objects_post))
            ]
            self.last_mapping_debug = to_builtin(debug_payload)
            self._write_unigoal_mapping_log(self.last_mapping_debug)

        return segment_index

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
        canonical_names = [_canonical_or_unknown_caption(obj.name) for obj in usable]
        for name in canonical_names:
            if name not in self.class_names:
                self.class_names.append(name)

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
                [self.class_names.index(name) for name in canonical_names], dtype=np.int64
            ),
            "mask": masks,
            "classes": list(self.class_names),
            "image_appear_efficiency": [""] * len(usable),
            "image_rgb": frame.rgb,
            "caption": canonical_names,
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

    def _same_track_reassociation_allowed(
        self,
        previous_object: Any,
        current_object: Any,
        *,
        moving: bool,
    ) -> Tuple[bool, Dict[str, Any]]:
        previous_names = self._map_object_canonical_names(previous_object)
        current_names = self._map_object_canonical_names(current_object)
        distance = self._map_object_center_distance(previous_object, current_object)
        report = {
            "previous_names": sorted(previous_names),
            "current_names": sorted(current_names),
            "center_distance": distance,
            "moving": moving,
        }
        if moving:
            report["reason"] = "moving_track"
            return True, report
        if distance is None:
            report["reason"] = "missing_center_distance"
            return False, report
        max_distance = _env_float("ISBENCH_SAMJAM_UNIGOAL_SAME_TRACK_MAX_DISTANCE_M", 0.75)
        if distance > max_distance:
            report["reason"] = "track_distance_too_large"
            report["max_distance"] = max_distance
            return False, report
        if previous_names & current_names:
            report["reason"] = "same_track_same_name"
            return True, report
        if UNKNOWN_OBJECT_NAME in previous_names or UNKNOWN_OBJECT_NAME in current_names:
            report["reason"] = "same_track_unknown_alias"
            return True, report
        report["reason"] = "same_track_incompatible_names"
        return False, report

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

            moving = source_id in moving_source_ids
            allowed, reassociation_report = (
                self._same_track_reassociation_allowed(
                    previous_object,
                    current_object,
                    moving=moving,
                )
                if previous_object is not None and previous_object is not current_object
                else (False, {})
            )
            if previous_object is not None and previous_object is not current_object and allowed:
                self._merge_map_object(graph, target_object=previous_object, source_object=current_object)
                self.track_to_map_object[track_key] = previous_object
                if moving:
                    self.track_moved[track_key] = True
                reassociations.append(
                    {
                        "track": track_key,
                        "source_object_id": source_id,
                        "merged_detection_count": int(current_object.get("num_detections", 1)),
                        **reassociation_report,
                    }
                )
            else:
                self.track_to_map_object[track_key] = current_object
                if moving:
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
        existing = getattr(node, "uid", None)
        if isinstance(existing, (int, np.integer)) and int(existing) > 0:
            uid = int(existing)
            self.node_uids[node] = uid
            self.next_node_uid = max(self.next_node_uid, uid + 1)
            return uid
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
        frame_index = (
            self.segment_frame_indices[segment_index]
            if 0 <= segment_index < len(self.segment_frame_indices)
            else segment_index
        )
        for node in self.graph.nodes:
            uid = self._node_uid(node)
            normalized = _canonical_or_unknown_caption(getattr(node, "caption", None))
            if normalized and normalized != getattr(node, "caption", None):
                node.caption = normalized
            node.is_vis = node in visible_nodes
            node.is_coarse = _is_coarse_caption(node.caption)
            if node.is_vis:
                node.last_seen_step = frame_index
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

    def _current_segment_index(self) -> int:
        return len(self.segment_frame_indices) - 1

    def _node_seen_segment_age(self, node: Any) -> Optional[int]:
        map_object = getattr(node, "object", None)
        if not map_object:
            return None
        image_indices = [
            int(index)
            for index in map_object.get("image_idx", [])
            if isinstance(index, (int, np.integer)) or str(index).isdigit()
        ]
        if not image_indices:
            return None
        return max(0, self._current_segment_index() - max(image_indices))

    def _node_relation_fresh(self, node: Any) -> bool:
        if getattr(node, "is_vis", False):
            return True
        max_age = _env_int("ISBENCH_SAMJAM_UNIGOAL_RELATION_NODE_MAX_SEGMENT_AGE", 0)
        if max_age < 0:
            return True
        age = self._node_seen_segment_age(node)
        return age is not None and age <= max_age

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
            oriented = _normalize_relation_for_node_pair(source_node, target_node, relation.relation)
            if oriented is None:
                continue
            source_node, target_node, normalized = oriented
            if not self._node_relation_fresh(source_node) or not self._node_relation_fresh(target_node):
                continue
            relations.append((source_node, target_node, normalized))

        candidate_coarse = [
            node for node in self.graph.nodes
            if getattr(node, "is_coarse", True)
            and self._node_relation_fresh(node)
            and _normalize_caption(getattr(node, "caption", None)) in ATTACHABLE_COARSE_VOCAB
        ]
        visible_fine = [
            node for node in self.graph.nodes
            if not getattr(node, "is_coarse", True)
            and getattr(node, "is_vis", False)
            and self._node_relation_fresh(node)
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
            oriented = _normalize_relation_for_node_pair(source_node, target_node, relation.relation)
            if oriented is None:
                continue
            source_node, target_node, normalized = oriented
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
            edge.set_relation(normalized)

    def _upsert_edge(self, node1: Any, node2: Any, relation: str) -> None:
        if node1 is node2 or relation is None:
            return
        oriented = _normalize_relation_for_node_pair(node1, node2, relation)
        if oriented is None:
            return
        node1, node2, relation = oriented
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
            and self._node_relation_fresh(node)
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
            if edge.relation not in ALLOWED_FINE_COARSE_RELATIONS:
                continue
            if not self._node_relation_fresh(edge.node1) or not self._node_relation_fresh(edge.node2):
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
            if not getattr(node, "is_coarse", True)
            and self._node_relation_fresh(node)
            and self._node_uid(node) in fine_parent_map
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
                self._record_edge_update_event(
                    action="unresolved",
                    node_uid=uid,
                    reason="manipulated_uid_not_found",
                )
                continue
            for edge in list(node.edges):
                other = edge.node2 if edge.node1 is node else edge.node1
                direct_key = (self._node_uid(edge.node1), self._node_uid(edge.node2))
                reverse_key = (self._node_uid(edge.node2), self._node_uid(edge.node1))
                event_base = {
                    "node_uid": uid,
                    "node_object_id": self._stable_id(node),
                    "node_name": _canonical_or_unknown_caption(getattr(node, "caption", None)),
                    "other_uid": self._node_uid(other),
                    "other_object_id": self._stable_id(other),
                    "other_name": _canonical_or_unknown_caption(getattr(other, "caption", None)),
                    "edge_source_uid": direct_key[0],
                    "edge_target_uid": direct_key[1],
                    "relation_before": edge.relation,
                }
                if not getattr(other, "is_vis", False):
                    self._record_edge_update_event(
                        action="delete",
                        reason="other_endpoint_not_visible",
                        **event_base,
                    )
                    edge.delete()
                    continue
                if direct_key in frame_relation_map:
                    relation_after = frame_relation_map[direct_key]
                    self._record_edge_update_event(
                        action=(
                            "confirm"
                            if relation_after == edge.relation
                            else "update"
                        ),
                        reason="current_frame_relation",
                        relation_after=relation_after,
                        **event_base,
                    )
                    edge.set_relation(frame_relation_map[direct_key])
                elif reverse_key in frame_relation_map:
                    self._record_edge_update_event(
                        action="delete",
                        reason="current_frame_reverse_relation",
                        reverse_relation=frame_relation_map[reverse_key],
                        **event_base,
                    )
                    edge.delete()
                else:
                    self._record_edge_update_event(
                        action="delete",
                        reason="no_current_frame_relation",
                        **event_base,
                    )
                    edge.delete()

    def _record_edge_update_event(self, **event: Any) -> None:
        event = to_builtin(event)
        self.edge_update_events.append(event)
        del self.edge_update_events[:-100]
        if not self._debug_matching_enabled():
            return
        _append_debug_log(
            [
                "[ISBench][SAMJAM-UniGoalEdgeUpdate] "
                f"action={event.get('action')} reason={event.get('reason')} "
                f"node_uid={event.get('node_uid')} other_uid={event.get('other_uid')} "
                f"before={event.get('relation_before')} after={event.get('relation_after')}"
            ]
        )

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
        normalized_name = _canonical_or_unknown_caption(moved_object)
        current_visible_nodes = set(source_to_node.values())
        candidates = []
        for node in self.graph.nodes:
            score = 0.0
            reasons = []
            semantic_match = False
            label = _canonical_or_unknown_caption(getattr(node, "caption", None))
            history = [
                _canonical_or_unknown_caption(item)
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
        normalized = _canonical_or_unknown_caption(object_name)
        for _, obj_ref in object_scope.items():
            obj = getattr(obj_ref, "wrapped_obj", obj_ref)
            names = [
                getattr(obj, "name", None),
                getattr(obj, "category", None),
                getattr(obj_ref, "name", None),
            ]
            if any(_canonical_or_unknown_caption(name) == normalized for name in names if name):
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
        if self.graph is None:
            return []
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
            lifelong_label = _canonical_or_unknown_caption(node.caption or "object")
            samjam_object_id = source_id or (source_history[-1] if source_history else None)
            frame_attributes = frame_object.attributes if frame_object is not None else {}
            states = dict(frame_attributes.get("states") or {})
            is_moving = bool(frame_object and source_id in moving_source_ids)
            is_moved = bool(
                self.node_moved.get(node, False)
                or (track_key is not None and self.track_moved.get(track_key, False))
            )
            if is_moving:
                states["is_moving"] = True
            if is_moved:
                states["is_moved"] = True
            source_ids = {
                "unigoal_node": f"node@{uid}",
                "unigoal_object": stable_object_id,
                "legacy_unigoal_object_id": stable_object_id,
            }
            if samjam_object_id is not None:
                source_ids.update(
                    {
                        "samjam_object": str(samjam_object_id),
                        "samjam_object_id": str(samjam_object_id),
                    }
                )
            attributes = {
                "source": "samjam_unigoal",
                "uid": uid,
                "trace_id": f"node@{id(node)}",
                "source_ids": source_ids,
                "stable_object_id": stable_object_id,
                "lifelong_label": lifelong_label,
                "normalized_label": lifelong_label,
                "caption": (
                    frame_attributes.get("caption")
                    or frame_attributes.get("description")
                    or frame_attributes.get("vlm_raw_name")
                    or lifelong_label
                ),
                "states": states,
                "hazard": dict(frame_attributes.get("hazard") or {}),
                "role": frame_attributes.get("role"),
                "room": str(room_id or "unknown_room"),
                "is_coarse": bool(getattr(node, "is_coarse", _is_coarse_caption(lifelong_label))),
                "is_vis": bool(getattr(node, "is_vis", visible)),
                "last_seen_step": int(
                    getattr(node, "last_seen_step", max(frame_history))
                ),
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
                "is_moving": is_moving,
                "is_moved": is_moved,
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
            if not self._node_relation_fresh(edge.node1) or not self._node_relation_fresh(edge.node2):
                continue
            oriented = _normalize_relation_for_node_pair(edge.node1, edge.node2, edge.relation)
            if oriented is None:
                continue
            source_node, target_node, relation = oriented
            relations.append(
                PerceivedRelation(
                    source_id=self._stable_id(source_node),
                    target_id=self._stable_id(target_node),
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
            label = _canonical_or_unknown_caption(getattr(node, "caption", None))
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
            if not self._node_relation_fresh(edge.node1) or not self._node_relation_fresh(edge.node2):
                continue
            oriented = _normalize_relation_for_node_pair(edge.node1, edge.node2, edge.relation)
            if oriented is None:
                continue
            source_node, target_node, relation = oriented
            edges.append(
                {
                    "source": node_id_map[source_node],
                    "target": node_id_map[target_node],
                    "source_uid": self._node_uid(source_node),
                    "target_uid": self._node_uid(target_node),
                    "source_object_id": self._stable_id(source_node),
                    "target_object_id": self._stable_id(target_node),
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

    def __init__(
        self,
        sensor_name: Optional[str] = None,
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ):
        from .samjam_sam2 import SAMJAMSAM2Backend

        self.scene_graph_config = _set_scene_graph_config(scene_graph_config)
        self.samjam_backend = SAMJAMSAM2Backend(
            sensor_name=sensor_name,
            scene_graph_config=self.scene_graph_config,
        )
        self.unigoal_adapter = SAMJAMUniGoalGraphAdapter(
            scene_graph_config=self.scene_graph_config,
        )
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
        filter_result = self._prepare_samjam_mapping_inputs(
            frame=self.last_frame,
            samjam_objects=samjam_objects,
            samjam_relations=samjam_relations,
            metadata=result.metadata,
        )
        mapped = self.unigoal_adapter.update(
            frame=self.last_frame,
            frame_objects=filter_result.objects,
            frame_relations=filter_result.relations,
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
                "samjam_mapping_input_object_count": len(filter_result.objects),
                "samjam_mapping_input_relation_count": len(filter_result.relations),
                "raw_samjam_object_count": len(samjam_objects),
                "filtered_samjam_object_count": len(filter_result.objects),
                "rejected_samjam_object_count": len(filter_result.report.get("rejected_objects", [])),
                "samjam_unigoal_filter": filter_result.report,
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

    def _prepare_samjam_mapping_inputs(
        self,
        frame: FrameObservation,
        samjam_objects: List[PerceivedObject],
        samjam_relations: List[PerceivedRelation],
        metadata: Dict[str, Any],
    ) -> SAMJAMFilterResult:
        coarse_min_match_iou = _env_float("ISBENCH_SAMJAM_UNIGOAL_COARSE_MIN_MATCH_IOU", 0.25)
        fine_min_match_iou = _env_float("ISBENCH_SAMJAM_UNIGOAL_FINE_MIN_MATCH_IOU", 0.05)
        thresholds = {
            "coarse_min_match_iou": coarse_min_match_iou,
            "fine_min_match_iou": fine_min_match_iou,
            "min_match_iou": coarse_min_match_iou,
            "require_match_metadata": _env_bool(
                "ISBENCH_SAMJAM_UNIGOAL_REQUIRE_MATCH_METADATA", True
            ),
            "max_mask_area_ratio": _env_float(
                "ISBENCH_SAMJAM_UNIGOAL_MAX_MASK_AREA_RATIO", 0.80
            ),
            "max_large_coarse_mask_area_ratio": _env_float(
                "ISBENCH_SAMJAM_UNIGOAL_MAX_LARGE_COARSE_MASK_AREA_RATIO", 0.65
            ),
            "max_bbox_area_ratio": _env_float(
                "ISBENCH_SAMJAM_UNIGOAL_MAX_BBOX_AREA_RATIO", 0.90
            ),
            "min_valid_depth_ratio": _env_float(
                "ISBENCH_SAMJAM_UNIGOAL_MIN_VALID_DEPTH_RATIO", 0.03
            ),
        }
        match_details = list(metadata.get("samjam_match_details") or [])
        best_detail_by_native, duplicate_claims = self._best_samjam_match_details(match_details)

        kept_objects: List[PerceivedObject] = []
        kept_object_rows: List[Dict[str, Any]] = []
        rejected_objects: List[Dict[str, Any]] = []
        kept_source_ids: Set[str] = set()
        for obj in samjam_objects:
            native_key = self._native_key_from_object(obj)
            match_detail = best_detail_by_native.get(native_key)
            reason, metrics = self._samjam_object_rejection_reason(
                frame=frame,
                obj=obj,
                match_detail=match_detail,
                thresholds=thresholds,
            )
            object_row = self._samjam_object_report_row(
                obj=obj,
                native_key=native_key,
                match_detail=match_detail,
                metrics=metrics,
            )
            if reason is not None:
                object_row["reason"] = reason
                rejected_objects.append(object_row)
                continue

            stable_name, vote_info = self.unigoal_adapter.stable_name_for_samjam_object(
                obj, match_detail
            )
            filtered_obj = self._clone_samjam_object_for_mapping(
                obj=obj,
                stable_name=stable_name,
                match_detail=match_detail,
                metrics=metrics,
                vote_info=vote_info,
            )
            kept_objects.append(filtered_obj)
            kept_source_ids.add(filtered_obj.object_id)
            object_row["stable_name"] = stable_name
            object_row["name_vote"] = vote_info
            kept_object_rows.append(object_row)

        filtered_relations, relation_report = self._filter_samjam_relations(
            relations=samjam_relations,
            kept_source_ids=kept_source_ids,
            kept_object_by_id={obj.object_id: obj for obj in kept_objects},
        )
        report = {
            "enabled": True,
            "thresholds": thresholds,
            "raw_object_count": len(samjam_objects),
            "kept_object_count": len(kept_objects),
            "rejected_object_count": len(rejected_objects),
            "raw_relation_count": len(samjam_relations),
            "kept_relation_count": len(filtered_relations),
            "rejected_relation_count": len(relation_report["rejected_relations"]),
            "duplicate_mask_claims": duplicate_claims,
            "kept_objects": kept_object_rows,
            "rejected_objects": rejected_objects,
            "relations": relation_report,
        }
        self._write_samjam_filter_log(report)
        return SAMJAMFilterResult(
            objects=kept_objects,
            relations=filtered_relations,
            report=to_builtin(report),
        )

    def _match_detail_effective_score(self, match_detail: Optional[Dict[str, Any]]) -> float:
        return _match_detail_effective_score(match_detail)

    def _best_samjam_match_details(
        self,
        match_details: List[Dict[str, Any]],
    ) -> Tuple[Dict[Optional[str], Dict[str, Any]], List[Dict[str, Any]]]:
        best_by_native: Dict[Optional[str], Dict[str, Any]] = {}
        duplicate_claims: List[Dict[str, Any]] = []
        for detail in match_details:
            native_key = self._native_key_from_match_detail(detail)
            if native_key is None:
                continue
            current = best_by_native.get(native_key)
            detail_iou = _safe_float(detail.get("best_iou"), 0.0)
            detail_score = self._match_detail_effective_score(detail)
            if current is None:
                best_by_native[native_key] = detail
                continue
            current_iou = _safe_float(current.get("best_iou"), 0.0)
            current_score = self._match_detail_effective_score(current)
            if detail_score > current_score or (
                detail_score == current_score and detail_iou > current_iou
            ):
                winner, loser = detail, current
                best_by_native[native_key] = detail
            else:
                winner, loser = current, detail
            duplicate_claims.append(
                {
                    "native_id": native_key,
                    "winner_vlm_id": winner.get("vlm_id"),
                    "winner_name": winner.get("canonical_name") or winner.get("vlm_name"),
                    "winner_iou": _safe_float(winner.get("best_iou"), 0.0),
                    "winner_score": self._match_detail_effective_score(winner),
                    "loser_vlm_id": loser.get("vlm_id"),
                    "loser_name": loser.get("canonical_name") or loser.get("vlm_name"),
                    "loser_iou": _safe_float(loser.get("best_iou"), 0.0),
                    "loser_score": self._match_detail_effective_score(loser),
                    "reason": "duplicate_mask_claim",
                }
            )
        return best_by_native, duplicate_claims

    def _samjam_object_rejection_reason(
        self,
        frame: FrameObservation,
        obj: PerceivedObject,
        match_detail: Optional[Dict[str, Any]],
        thresholds: Dict[str, Any],
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        metrics = self._samjam_object_quality_metrics(frame, obj)
        if not obj.attributes.get("currently_visible", True):
            return "not_currently_visible", metrics
        if obj.mask is None or obj.bbox is None:
            return "missing_mask_or_bbox", metrics
        if metrics.get("bbox_area") is None:
            return "invalid_bbox", metrics
        normalized_name = _canonical_or_unknown_caption(
            (match_detail or {}).get("canonical_name")
            or (match_detail or {}).get("vlm_name")
            or obj.name
        )
        min_match_iou = (
            thresholds["coarse_min_match_iou"]
            if _is_coarse_caption(normalized_name)
            else thresholds["fine_min_match_iou"]
        )
        metrics["canonical_name"] = normalized_name
        metrics["match_iou_threshold"] = min_match_iou
        if match_detail is None and thresholds["require_match_metadata"]:
            return "missing_match_metadata", metrics
        if match_detail is not None:
            match_iou = _safe_float(match_detail.get("best_iou"), 0.0)
            match_score = self._match_detail_effective_score(match_detail)
            metrics["match_iou"] = match_iou
            metrics["match_score"] = match_score
            metrics["match_accept_reason"] = match_detail.get("accept_reason")
            if match_score < min_match_iou:
                return "low_match_iou", metrics

        mask_area_ratio = metrics.get("mask_area_ratio")
        if (
            mask_area_ratio is not None
            and mask_area_ratio >= thresholds["max_mask_area_ratio"]
        ):
            return "huge_mask_area", metrics

        if (
            normalized_name in LARGE_MASK_COARSE_FILTER_VOCAB
            and mask_area_ratio is not None
            and mask_area_ratio >= thresholds["max_large_coarse_mask_area_ratio"]
        ):
            return "large_coarse_mask_area", metrics

        bbox_area_ratio = metrics.get("bbox_area_ratio")
        if (
            bbox_area_ratio is not None
            and bbox_area_ratio >= thresholds["max_bbox_area_ratio"]
        ):
            return "huge_bbox_area", metrics

        valid_depth_ratio = metrics.get("valid_depth_ratio")
        if (
            valid_depth_ratio is not None
            and thresholds["min_valid_depth_ratio"] > 0.0
            and valid_depth_ratio < thresholds["min_valid_depth_ratio"]
        ):
            return "low_valid_depth_ratio", metrics
        return None, metrics

    def _samjam_object_quality_metrics(
        self,
        frame: FrameObservation,
        obj: PerceivedObject,
    ) -> Dict[str, Any]:
        height, width = frame.rgb.shape[:2]
        image_area = max(int(height) * int(width), 1)
        mask = None if obj.mask is None else np.asarray(obj.mask, dtype=bool)
        mask_area = int(mask.sum()) if mask is not None else int(obj.attributes.get("mask_area", 0) or 0)
        bbox_area = self._bbox_area(obj.bbox)
        return {
            "mask_area": mask_area,
            "mask_area_ratio": float(mask_area / image_area),
            "bbox_area": bbox_area,
            "bbox_area_ratio": None if bbox_area is None else float(bbox_area / image_area),
            "valid_depth_ratio": self._mask_valid_depth_ratio(frame.depth, mask),
            "predicted_iou": _safe_float(obj.attributes.get("predicted_iou"), 0.0),
            "stability_score": _safe_float(obj.attributes.get("stability_score"), 0.0),
            "confidence": _safe_float(obj.confidence, 0.0),
        }

    def _bbox_area(self, bbox: Optional[List[float]]) -> Optional[float]:
        if bbox is None or len(bbox) != 4:
            return None
        try:
            x1, y1, x2, y2 = [float(value) for value in bbox]
        except (TypeError, ValueError):
            return None
        if not all(np.isfinite([x1, y1, x2, y2])):
            return None
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def _mask_valid_depth_ratio(
        self,
        depth: Optional[np.ndarray],
        mask: Optional[np.ndarray],
    ) -> Optional[float]:
        if depth is None or mask is None:
            return None
        depth_array = np.asarray(depth, dtype=np.float32)
        if depth_array.ndim == 3 and depth_array.shape[-1] == 1:
            depth_array = depth_array[:, :, 0]
        if mask.shape != depth_array.shape[:2]:
            return None
        masked_depth = depth_array[mask]
        if masked_depth.size == 0:
            return 0.0
        valid = np.isfinite(masked_depth) & (masked_depth > 0)
        return float(valid.mean())

    def _clone_samjam_object_for_mapping(
        self,
        obj: PerceivedObject,
        stable_name: str,
        match_detail: Optional[Dict[str, Any]],
        metrics: Dict[str, Any],
        vote_info: Dict[str, Any],
    ) -> PerceivedObject:
        attributes = dict(obj.attributes)
        attributes.update(
            {
                "vlm_raw_frame_name": vote_info.get("raw_frame_name"),
                "vlm_raw_name": attributes.get("vlm_raw_name") or vote_info.get("raw_frame_name"),
                "vlm_frame_name": vote_info.get("frame_name"),
                "stable_name": stable_name,
                "canonical_name": stable_name,
                "name_vote_scores": vote_info.get("scores", {}),
                "name_vote_weight": vote_info.get("weight"),
                "name_vote_track_key": vote_info.get("track_key"),
                "samjam_unigoal_filter": dict(metrics),
                "match_iou": (
                    None
                    if match_detail is None
                    else _safe_float(match_detail.get("best_iou"), 0.0)
                ),
                "match_score": None
                if match_detail is None
                else self._match_detail_effective_score(match_detail),
                "match_accept_reason": None
                if match_detail is None
                else match_detail.get("accept_reason"),
                "filter_source": "samjam_unigoal_adapter",
            }
        )
        return PerceivedObject(
            object_id=obj.object_id,
            name=stable_name,
            category=_category(stable_name),
            bbox=None if obj.bbox is None else [float(value) for value in obj.bbox],
            mask=obj.mask,
            position=obj.position,
            room_id=obj.room_id,
            confidence=obj.confidence,
            attributes=attributes,
        )

    def _filter_samjam_relations(
        self,
        relations: List[PerceivedRelation],
        kept_source_ids: Set[str],
        kept_object_by_id: Dict[str, PerceivedObject],
    ) -> Tuple[List[PerceivedRelation], Dict[str, Any]]:
        filtered: List[PerceivedRelation] = []
        rejected: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str, str]] = set()
        for relation in relations:
            source_id = relation.source_id
            target_id = relation.target_id
            if source_id == target_id:
                rejected.append(self._relation_reject_row(relation, "self_relation"))
                continue
            if source_id not in kept_source_ids or target_id not in kept_source_ids:
                rejected.append(self._relation_reject_row(relation, "endpoint_filtered"))
                continue
            source_obj = kept_object_by_id.get(source_id)
            target_obj = kept_object_by_id.get(target_id)
            if source_obj is None or target_obj is None:
                rejected.append(self._relation_reject_row(relation, "endpoint_filtered_after_lookup"))
                continue
            normalized_result = _normalize_relation_for_type_pair(
                source_is_coarse=_is_coarse_caption(source_obj.name),
                target_is_coarse=_is_coarse_caption(target_obj.name),
                relation=relation.relation,
            )
            if normalized_result is None:
                rejected.append(self._relation_reject_row(relation, "unsupported_relation_for_node_types"))
                continue
            normalized, reverse = normalized_result
            if reverse:
                source_id, target_id = target_id, source_id
            if source_id == target_id:
                rejected.append(self._relation_reject_row(relation, "self_relation_after_reverse"))
                continue
            key = (source_id, target_id, normalized)
            if key in seen:
                rejected.append(self._relation_reject_row(relation, "duplicate_relation"))
                continue
            seen.add(key)
            filtered.append(
                PerceivedRelation(
                    source_id=source_id,
                    target_id=target_id,
                    relation=normalized,
                    confidence=relation.confidence,
                    source=relation.source,
                )
            )
        return filtered, {
            "kept_relation_count": len(filtered),
            "rejected_relations": rejected,
        }

    def _relation_reject_row(self, relation: PerceivedRelation, reason: str) -> Dict[str, Any]:
        return {
            "source_id": relation.source_id,
            "target_id": relation.target_id,
            "relation": relation.relation,
            "reason": reason,
        }

    def _native_key_from_object(self, obj: PerceivedObject) -> Optional[str]:
        samjam_id = obj.attributes.get("samjam_id")
        if samjam_id is not None:
            return self._native_key(samjam_id)
        match = re.match(r"samjam_object:(\d+)$", str(obj.object_id))
        if match:
            return self._native_key(match.group(1))
        return None

    def _native_key_from_match_detail(self, detail: Dict[str, Any]) -> Optional[str]:
        native_id = detail.get("best_native_id")
        if native_id is None:
            return None
        return self._native_key(native_id)

    def _native_key(self, value: Any) -> Optional[str]:
        try:
            return str(int(value))
        except (TypeError, ValueError):
            text = str(value or "").strip()
            return text or None

    def _samjam_object_report_row(
        self,
        obj: PerceivedObject,
        native_key: Optional[str],
        match_detail: Optional[Dict[str, Any]],
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "object_id": obj.object_id,
            "native_id": native_key,
            "name": obj.name,
            "canonical_name": _canonical_or_unknown_caption(
                (match_detail or {}).get("canonical_name")
                or (match_detail or {}).get("vlm_name")
                or obj.name
            ),
            "visible": bool(obj.attributes.get("currently_visible", True)),
            "match_iou": (
                None
                if match_detail is None
                else _safe_float(match_detail.get("best_iou"), 0.0)
            ),
            "match_score": None
            if match_detail is None
            else self._match_detail_effective_score(match_detail),
            "match_accept_reason": None
            if match_detail is None
            else match_detail.get("accept_reason"),
            "match_vlm_id": None if match_detail is None else match_detail.get("vlm_id"),
            "match_name": None
            if match_detail is None
            else match_detail.get("canonical_name") or match_detail.get("vlm_name"),
            "mask_area_ratio": metrics.get("mask_area_ratio"),
            "bbox_area_ratio": metrics.get("bbox_area_ratio"),
            "valid_depth_ratio": metrics.get("valid_depth_ratio"),
        }

    def _write_samjam_filter_log(self, report: Dict[str, Any]) -> None:
        if not (
            self.unigoal_adapter._debug_matching_enabled()
            or _env_bool("ISBENCH_SAMJAM_UNIGOAL_DEBUG_FILTER", False)
        ):
            return
        max_items = _env_int("ISBENCH_SCENE_GRAPH_DEBUG_MAX_ITEMS", 40)
        lines = [
            "[ISBench][SAMJAM-UniGoalFilter] "
            f"raw_objects={report.get('raw_object_count')} "
            f"kept_objects={report.get('kept_object_count')} "
            f"rejected_objects={report.get('rejected_object_count')} "
            f"raw_relations={report.get('raw_relation_count')} "
            f"kept_relations={report.get('kept_relation_count')} "
            f"rejected_relations={report.get('rejected_relation_count')}"
        ]
        for item in report.get("kept_objects", [])[:max_items]:
            vote = item.get("name_vote") or {}
            lines.append(
                "[ISBench][SAMJAM-UniGoalFilter][keep] "
                f"id={item.get('object_id')} native={item.get('native_id')} "
                f"name={item.get('name')} stable={item.get('stable_name')} "
                f"match_iou={item.get('match_iou')} "
                f"mask_ratio={item.get('mask_area_ratio')} "
                f"depth_ratio={item.get('valid_depth_ratio')} "
                f"vote={vote.get('scores')}"
            )
        for item in report.get("rejected_objects", [])[:max_items]:
            lines.append(
                "[ISBench][SAMJAM-UniGoalFilter][reject] "
                f"id={item.get('object_id')} native={item.get('native_id')} "
                f"name={item.get('name')} reason={item.get('reason')} "
                f"match_iou={item.get('match_iou')} "
                f"mask_ratio={item.get('mask_area_ratio')} "
                f"bbox_ratio={item.get('bbox_area_ratio')} "
                f"depth_ratio={item.get('valid_depth_ratio')}"
            )
        for item in report.get("duplicate_mask_claims", [])[:max_items]:
            lines.append(
                "[ISBench][SAMJAM-UniGoalFilter][duplicate_claim] "
                f"native={item.get('native_id')} "
                f"winner={item.get('winner_name')}:{item.get('winner_iou')} "
                f"loser={item.get('loser_name')}:{item.get('loser_iou')}"
            )
        for item in report.get("relations", {}).get("rejected_relations", [])[:max_items]:
            lines.append(
                "[ISBench][SAMJAM-UniGoalFilter][relation_reject] "
                f"{item.get('source_id')} -[{item.get('relation')}]-> "
                f"{item.get('target_id')} reason={item.get('reason')}"
            )
        _append_debug_log(lines)

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
                match_summary=pending_debug.get("match_summary", {}),
                filter_report=result.metadata.get("samjam_unigoal_filter", {}),
                graph_objects=result.objects,
            )
            result.metadata["samjam_output_dir"] = str(writer.output_dir)
        except Exception as exc:
            result.metadata["samjam_output_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
