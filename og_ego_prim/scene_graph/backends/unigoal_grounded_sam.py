import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import numpy as np

from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)

from .samjam_unigoal import (
    ALLOWED_LIFELONG_RELATIONS,
    ATTACHABLE_COARSE_VOCAB,
    COARSE_NEAR_OBSTACLE_VOCAB,
    CONTAINER_COARSE_VOCAB,
    FINE_PARENT_RELATION_PRIORITY,
    SUPPORT_SURFACE_VOCAB,
    _canonical_or_unknown_caption as _lifelong_canonical_or_unknown_caption,
    _is_coarse_caption as _lifelong_is_coarse_caption,
    _normalize_caption as _lifelong_normalize_caption,
)
from .utils import insert_sys_path, repo_root, room_lookup_from_env, to_builtin
from ..unigoal_debug_artifacts import (
    output_dir_from_config,
    write_frame_debug_artifacts,
    write_result_artifacts,
)


DEFAULT_UNIGOAL_EXTRA_NODE_SPACE = (
    "apple",
    "tissue box",
    "banana",
    "cabinet",
    "drawer",
    "counter",
    "sink",
    "stove",
    "refrigerator",
    "door",
    "box",
    "shelf",
    "table",
    "bowl",
    "cup",
    "plate",
    "bottle",
    "trash can",
    "garbage can",
    "bucket",
)

DEFAULT_GROUNDED_SAM_DEVICE = "cpu"

_ACTIVE_SCENE_GRAPH_CONFIG = SceneGraphConfig()


def _set_scene_graph_config(config: Optional[SceneGraphConfig]) -> SceneGraphConfig:
    global _ACTIVE_SCENE_GRAPH_CONFIG
    _ACTIVE_SCENE_GRAPH_CONFIG = config or SceneGraphConfig()
    return _ACTIVE_SCENE_GRAPH_CONFIG


def _cfg() -> SceneGraphConfig:
    return _ACTIVE_SCENE_GRAPH_CONFIG


def _env_bool(name: str, default: bool) -> bool:
    return _cfg().option_bool(name, default)


def _env_int(name: str, default: int) -> int:
    return _cfg().option_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _cfg().option_float(name, default)


def _debug_matching_enabled() -> bool:
    return (
        _env_bool("ISBENCH_SCENE_GRAPH_DEBUG_MATCHING", False)
        or _env_bool("ISBENCH_UNIGOAL_DEBUG_MATCHING", False)
        or _env_bool("ISBENCH_UNIGOAL_GROUNDED_SAM_DEBUG", False)
        or bool(_cfg().debug_log_path)
        or bool(_cfg().output_debug_matching)
    )


def _debug_log_path() -> Path:
    explicit = _cfg().debug_log_path
    if explicit:
        return Path(explicit)
    output_dir = _cfg().output_dir or _cfg().option("ISBENCH_SCENE_GRAPH_OUTPUT_DIR")
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


def _normalize_term(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _split_node_space(value: Any) -> List[str]:
    if value is None:
        return []
    terms = []
    for part in re.split(r"[\n,;.]+", str(value)):
        term = _normalize_term(part)
        if term:
            terms.append(term)
    return terms


def _format_node_space(terms: List[str]) -> str:
    ordered_terms = []
    seen = set()
    for term in terms:
        term = _normalize_term(term)
        if not term or term in seen:
            continue
        seen.add(term)
        ordered_terms.append(term)
    return ". ".join(ordered_terms)


class UniGoalGroundedSAMBackend:
    name = "unigoal_grounded_sam"

    def __init__(
        self,
        sensor_name: Optional[str] = None,
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ):
        self.scene_graph_config = _set_scene_graph_config(scene_graph_config)
        self.adapter = ISBenchObservationAdapter(sensor_name=sensor_name)
        self.env = None
        self.graph = None
        self.object_goal: Optional[str] = None
        self.last_result: Optional[PerceptionResult] = None
        self.room_lookup = None
        self.node_uids: Dict[Any, int] = {}
        self.next_node_uid = 1
        self.last_mapping_debug: Optional[Dict[str, Any]] = None
        self.last_relation_debug: Optional[Dict[str, Any]] = None
        self.iou_fallback_patch_installed = False
        self.iou_fallback_used = False

    def reset(self, env: Any) -> None:
        self.env = env
        self.graph = None
        self.last_result = None
        self.adapter.reset()
        self.adapter.ensure_robot_sensor_modalities(env)
        self.room_lookup = room_lookup_from_env(env)
        self.node_uids.clear()
        self.next_node_uid = 1
        self.last_mapping_debug = None
        self.last_relation_debug = None
        self.iou_fallback_patch_installed = False
        self.iou_fallback_used = False

    def set_object_goal(self, object_goal: Optional[str]):
        if object_goal:
            object_goal = object_goal.replace("_", " ").strip()
        self.object_goal = object_goal or self.object_goal
        if self.graph is not None and self.object_goal:
            self.graph.set_object_goal(self.object_goal)
            self._refresh_node_space(self.graph)

    def observe(self, env: Any) -> FrameObservation:
        return self.adapter.observe(env)

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        graph = self._ensure_graph(frame)
        observations = self._frame_to_unigoal_observation(frame)
        camera_matrix = self._unigoal_navigation_camera_matrix(frame)
        if camera_matrix is not None:
            graph.camera_matrix = camera_matrix
        graph.set_observations(observations)
        graph.set_navigate_steps(frame.frame_index)
        self._refresh_node_space(graph)
        self._run_debugged_update_scenegraph(graph, frame)
        graph.update_group()

        result = self._result_from_graph(graph, frame)
        self._write_lightweight_artifacts(frame, result)
        self.last_result = result
        return result

    def update_memory(self, result: PerceptionResult) -> PerceptionResult:
        return result

    def _ensure_graph(self, frame: FrameObservation):
        if self.graph is not None:
            return self.graph

        vendor_root = repo_root() / "og_ego_prim" / "scene_graph" / "vendor" / "unigoal"
        gsa_root = vendor_root / "third_party" / "Grounded-Segment-Anything"
        groundingdino_root = gsa_root / "GroundingDINO"
        segment_anything_root = gsa_root / "segment_anything"
        insert_sys_path([segment_anything_root, groundingdino_root, gsa_root, vendor_root])

        try:
            import torch
        except ImportError as exc:
            raise ImportError("UniGoal backend requires torch") from exc

        try:
            from src.graph.graph import Graph
        except ImportError as exc:
            raise ImportError(
                "Failed to import vendored UniGoal Graph. Install scene graph dependencies "
                "from requirements-scene-graph.txt and local GroundingDINO / segment-anything packages."
            ) from exc

        height, width = frame.rgb.shape[:2]
        map_resolution = _env_int("ISBENCH_UNIGOAL_MAP_RESOLUTION", 5)
        map_size_cm = _env_int("ISBENCH_UNIGOAL_MAP_SIZE_CM", 2400)
        map_size = int(map_size_cm / map_resolution)
        device = (
            self.scene_graph_config.device
            or self.scene_graph_config.option("ISBENCH_UNIGOAL_GROUNDED_SAM_DEVICE")
            or self.scene_graph_config.option("ISBENCH_UNIGOAL_DEVICE")
            or DEFAULT_GROUNDED_SAM_DEVICE
        )
        args = SimpleNamespace(
            map_resolution=map_resolution,
            map_size_cm=map_size_cm,
            map_size=map_size,
            env_frame_height=height,
            env_frame_width=width,
            hfov=float(self.scene_graph_config.hfov),
            device=device,
            base_url=os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE"),
            api_key=os.environ.get("OPENAI_API_KEY") or "EMPTY",
            llm_model=self.scene_graph_config.option("ISBENCH_SCENE_GRAPH_LLM_MODEL", "gpt-4o"),
            vlm_model=self.scene_graph_config.option("ISBENCH_SCENE_GRAPH_VLM_MODEL", "gpt-4o"),
        )
        graph = Graph(args, is_navigation=True)
        self._patch_unigoal_iou_fallback()
        graph.set_room_lookup(self.room_lookup)
        if self.object_goal:
            graph.set_object_goal(self.object_goal)
        self._refresh_node_space(graph)
        self.graph = graph
        return graph

    def _patch_unigoal_iou_fallback(self) -> None:
        try:
            from src.graph.utils import utils as graph_utils
        except ImportError:
            return
        original = getattr(graph_utils, "compute_3d_iou_accuracte_batch", None)
        if original is None:
            return
        if getattr(original, "_is_isbench_backend_patch", False):
            self.iou_fallback_patch_installed = True
            return

        def _backend_iou_fallback(bbox_map, bbox_new):
            try:
                return original(bbox_map, bbox_new)
            except ImportError:
                self.iou_fallback_used = True
                return graph_utils.compute_iou_batch(bbox_map, bbox_new)

        _backend_iou_fallback._is_isbench_backend_patch = True
        graph_utils.compute_3d_iou_accuracte_batch = _backend_iou_fallback
        self.iou_fallback_patch_installed = True

    def _refresh_node_space(self, graph: Any) -> None:
        override = self.scene_graph_config.option("ISBENCH_UNIGOAL_NODE_SPACE")
        if override:
            terms = _split_node_space(override)
        else:
            terms = _split_node_space(getattr(graph, "node_space", ""))
            terms.extend(DEFAULT_UNIGOAL_EXTRA_NODE_SPACE)
            terms.extend(
                _split_node_space(
                    self.scene_graph_config.option("ISBENCH_UNIGOAL_EXTRA_NODE_SPACE")
                )
            )
        if self.object_goal:
            terms.extend(_split_node_space(self.object_goal))
        graph.node_space = _format_node_space(terms)

    def _relation_backend(self) -> str:
        value = (
            self.scene_graph_config.option("ISBENCH_UNIGOAL_GROUNDED_SAM_RELATION_BACKEND")
            or self.scene_graph_config.option("ISBENCH_UNIGOAL_RELATION_BACKEND")
            or "geometry"
        )
        normalized = str(value).strip().lower().replace("-", "_")
        aliases = {
            "3d": "geometry",
            "3d_mapping": "geometry",
            "geometry": "geometry",
            "lifelong": "geometry",
            "mapping3d": "geometry",
            "spatial": "geometry",
            "caption_llm": "llm",
            "llm": "llm",
            "native": "llm",
            "unigoal": "llm",
        }
        return aliases.get(normalized, "geometry")

    def _run_unigoal_scenegraph_update(self, graph: Any, frame: FrameObservation) -> None:
        relation_backend = self._relation_backend()
        segment_count_before = len(getattr(graph, "segment2d_results", []))
        self.last_relation_debug = {
            "backend": relation_backend,
            "frame_index": int(frame.frame_index),
        }
        if relation_backend != "geometry":
            graph.update_scenegraph()
            self.last_relation_debug.update(
                {
                    "mode": "native_unigoal_update_edge",
                    "edge_count": len(graph.get_edges()),
                }
            )
            return

        original_update_edge = graph.update_edge
        skipped_native_new_nodes = 0

        def _skip_native_relation_update() -> None:
            nonlocal skipped_native_new_nodes
            new_nodes = [
                node
                for node in getattr(graph, "nodes", [])
                if getattr(node, "is_new_node", False)
            ]
            skipped_native_new_nodes = len(new_nodes)
            for node in new_nodes:
                node.is_new_node = False
            clear_line = getattr(graph, "clear_line", None)
            if callable(clear_line):
                clear_line()

        graph.update_edge = _skip_native_relation_update
        try:
            graph.update_scenegraph()
        finally:
            graph.update_edge = original_update_edge

        segment_count_after = len(getattr(graph, "segment2d_results", []))
        segment_index = (
            segment_count_after - 1
            if segment_count_after > segment_count_before
            else -1
        )
        self.last_relation_debug.update(
            {
                "mode": "geometry_from_mapping3d",
                "segment_index": segment_index,
                "skipped_native_new_nodes": skipped_native_new_nodes,
            }
        )
        if segment_index < 0:
            self.last_relation_debug["skipped_reason"] = "no_new_segment"
            return
        detection_count = self._segment_detection_count(graph, segment_index)
        self.last_relation_debug["segment_detection_count"] = detection_count
        if detection_count <= 0:
            self.last_relation_debug["skipped_reason"] = "empty_segment"
            return

        self._prepare_geometry_relation_nodes(graph, frame)
        geometry_debug = self._update_geometry_edges(graph)
        self.last_relation_debug.update(geometry_debug)
        graph.get_scenegraph()
        self._write_geometry_relation_debug_log(self.last_relation_debug)

    def _segment_detection_count(self, graph: Any, segment_index: int) -> int:
        segments = getattr(graph, "segment2d_results", [])
        if segment_index < 0 or segment_index >= len(segments):
            return 0
        segment = segments[segment_index] or {}
        return max(
            len(segment.get("xyxy", []) or []),
            len(segment.get("mask", []) or []),
            len(segment.get("caption", []) or []),
            len(segment.get("class_id", []) or []),
        )

    def _prepare_geometry_relation_nodes(self, graph: Any, frame: FrameObservation) -> None:
        for node in getattr(graph, "nodes", []):
            uid = self._node_uid(node)
            normalized = _lifelong_canonical_or_unknown_caption(
                getattr(node, "caption", None)
            )
            is_vis = self._node_visible_in_latest_segment(graph, node)
            setattr(node, "uid", uid)
            setattr(node, "normalized_caption", normalized)
            setattr(node, "is_coarse", _lifelong_is_coarse_caption(normalized))
            setattr(node, "is_vis", bool(is_vis))
            if is_vis:
                setattr(node, "last_seen_step", int(frame.frame_index))
            elif not hasattr(node, "last_seen_step"):
                setattr(node, "last_seen_step", -1)

    def _node_bounds(self, node: Any) -> Any:
        map_object = getattr(node, "object", None)
        if not isinstance(map_object, dict):
            return None, None
        bbox = map_object.get("bbox")
        if bbox is None:
            return None, None
        points = self._bbox_points(bbox)
        if points is None or points.size == 0 or not np.isfinite(points).all():
            return None, None
        return points.min(axis=0), points.max(axis=0)

    def _current_segment_index(self, graph: Any) -> int:
        return len(getattr(graph, "segment2d_results", [])) - 1

    def _node_seen_segment_age(self, graph: Any, node: Any) -> Optional[int]:
        map_object = getattr(node, "object", None)
        if not isinstance(map_object, dict):
            return None
        image_indices = [
            int(index)
            for index in map_object.get("image_idx", [])
            if isinstance(index, (int, np.integer)) or str(index).isdigit()
        ]
        if not image_indices:
            return None
        return max(0, self._current_segment_index(graph) - max(image_indices))

    def _node_relation_fresh(self, graph: Any, node: Any) -> bool:
        if getattr(node, "is_vis", False):
            return True
        max_age = _env_int("ISBENCH_UNIGOAL_RELATION_NODE_MAX_SEGMENT_AGE", 0)
        if max_age < 0:
            return True
        age = self._node_seen_segment_age(graph, node)
        return age is not None and age <= max_age

    def _xy_overlap_ratio(
        self,
        fine_min: np.ndarray,
        fine_max: np.ndarray,
        coarse_min: np.ndarray,
        coarse_max: np.ndarray,
    ) -> Any:
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
        coarse_caption = _lifelong_normalize_caption(getattr(coarse_node, "caption", None))
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

    def _build_current_frame_geometry_relations(self, graph: Any) -> Any:
        relations = []
        relation_rows = []
        candidate_coarse = [
            node for node in getattr(graph, "nodes", [])
            if getattr(node, "is_coarse", True)
            and self._node_relation_fresh(graph, node)
            and _lifelong_normalize_caption(getattr(node, "caption", None)) in ATTACHABLE_COARSE_VOCAB
        ]
        visible_fine = [
            node for node in getattr(graph, "nodes", [])
            if not getattr(node, "is_coarse", True)
            and getattr(node, "is_vis", False)
            and self._node_relation_fresh(graph, node)
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
                best_relation, best_coarse, surface_dist, map_dist = candidates[0]
                fine_parent_map[self._node_uid(fine_node)] = best_coarse
                relations.append((fine_node, best_coarse, best_relation))
                relation_rows.append(
                    {
                        "source_uid": self._node_uid(fine_node),
                        "source_caption": getattr(fine_node, "caption", None),
                        "target_uid": self._node_uid(best_coarse),
                        "target_caption": getattr(best_coarse, "caption", None),
                        "relation": best_relation,
                        "surface_dist": None if not np.isfinite(surface_dist) else float(surface_dist),
                        "map_dist": None if not np.isfinite(map_dist) else float(map_dist),
                    }
                )

        for i, node1 in enumerate(visible_fine):
            for node2 in visible_fine[i + 1:]:
                relation = self._infer_fine_fine_near(node1, node2, fine_parent_map)
                if relation is None:
                    continue
                relations.append((node1, node2, relation))
                relation_rows.append(
                    {
                        "source_uid": self._node_uid(node1),
                        "source_caption": getattr(node1, "caption", None),
                        "target_uid": self._node_uid(node2),
                        "target_caption": getattr(node2, "caption", None),
                        "relation": relation,
                    }
                )
        return relations, relation_rows

    def _upsert_edge(self, node1: Any, node2: Any, relation: str) -> None:
        if node1 is node2 or relation is None:
            return
        edge = None
        for item in list(node1.edges):
            if item.node1 is node1 and item.node2 is node2:
                edge = item
                break
            if relation == "near" and item.node1 is node2 and item.node2 is node1:
                edge = item
                break
        if edge is None:
            from src.graph.graph import Edge

            edge = Edge(node1, node2)
        edge.set_relation(relation)

    def _refresh_coarse_near_edges(self, graph: Any) -> int:
        deleted = 0
        for edge in list(graph.get_edges()):
            if (
                getattr(edge.node1, "is_coarse", True)
                and getattr(edge.node2, "is_coarse", True)
                and edge.relation == "near"
            ):
                edge.delete()
                deleted += 1
        coarse_nodes = [
            node for node in getattr(graph, "nodes", [])
            if getattr(node, "is_coarse", True)
            and self._node_relation_fresh(graph, node)
            and self._node_position(node) is not None
            and _lifelong_normalize_caption(getattr(node, "caption", None)) in COARSE_NEAR_OBSTACLE_VOCAB
        ]
        created = 0
        for i, node1 in enumerate(coarse_nodes):
            for node2 in coarse_nodes[i + 1:]:
                if _lifelong_normalize_caption(node1.caption) == _lifelong_normalize_caption(node2.caption):
                    continue
                room1 = getattr(node1, "room_node", None)
                room2 = getattr(node2, "room_node", None)
                if room1 is not None and room2 is not None and room1 is not room2:
                    continue
                surface_dist = self._bbox_surface_distance(node1, node2)
                center_dist = self._bbox_xy_center_distance(node1, node2)
                if surface_dist is None or center_dist is None:
                    continue
                threshold = self._room_near_threshold(room1)
                if surface_dist <= threshold and center_dist <= threshold:
                    self._upsert_edge(node1, node2, "near")
                    created += 1
        return created - deleted

    def _fine_parent_map_from_edges(self, graph: Any) -> Dict[int, Any]:
        parent_map: Dict[int, Any] = {}
        for edge in graph.get_edges():
            if edge.relation not in {"on", "in", "above", "attach to"}:
                continue
            if not self._node_relation_fresh(graph, edge.node1) or not self._node_relation_fresh(graph, edge.node2):
                continue
            if (not getattr(edge.node1, "is_coarse", True)) and getattr(edge.node2, "is_coarse", True):
                parent_map[self._node_uid(edge.node1)] = edge.node2
            elif getattr(edge.node1, "is_coarse", True) and (not getattr(edge.node2, "is_coarse", True)):
                parent_map[self._node_uid(edge.node2)] = edge.node1
        return parent_map

    def _refresh_fine_fine_near_edges(self, graph: Any) -> int:
        fine_parent_map = self._fine_parent_map_from_edges(graph)
        deleted = 0
        for edge in list(graph.get_edges()):
            if (
                not getattr(edge.node1, "is_coarse", True)
                and not getattr(edge.node2, "is_coarse", True)
                and edge.relation == "near"
            ):
                edge.delete()
                deleted += 1
        fine_nodes = [
            node for node in getattr(graph, "nodes", [])
            if not getattr(node, "is_coarse", True)
            and self._node_relation_fresh(graph, node)
            and self._node_uid(node) in fine_parent_map
        ]
        created = 0
        for i, node1 in enumerate(fine_nodes):
            for node2 in fine_nodes[i + 1:]:
                relation = self._infer_fine_fine_near(node1, node2, fine_parent_map)
                if relation is not None:
                    self._upsert_edge(node1, node2, relation)
                    created += 1
        return created - deleted

    def _delete_invalid_geometry_edges(self, graph: Any) -> int:
        deleted = 0
        for edge in list(graph.get_edges()):
            if edge.relation not in ALLOWED_LIFELONG_RELATIONS:
                edge.delete()
                deleted += 1
        return deleted

    def _update_geometry_edges(self, graph: Any) -> Dict[str, Any]:
        before_edges = len(graph.get_edges())
        invalid_deleted = self._delete_invalid_geometry_edges(graph)
        coarse_near_delta = self._refresh_coarse_near_edges(graph)
        frame_relations, relation_rows = self._build_current_frame_geometry_relations(graph)
        fine_with_new_parent = {
            self._node_uid(node1)
            for node1, node2, _ in frame_relations
            if not getattr(node1, "is_coarse", True) and getattr(node2, "is_coarse", True)
        }
        parent_relation_types = {"on", "in", "above", "attach to"}
        parent_deleted = 0
        for fine_node in [
            node for node in getattr(graph, "nodes", [])
            if not getattr(node, "is_coarse", True) and getattr(node, "is_vis", False)
        ]:
            if self._node_uid(fine_node) not in fine_with_new_parent:
                continue
            for edge in list(fine_node.edges):
                other = edge.node2 if edge.node1 is fine_node else edge.node1
                if getattr(other, "is_coarse", True) and edge.relation in parent_relation_types:
                    edge.delete()
                    parent_deleted += 1

        seen = set()
        upserted = 0
        for node1, node2, relation in frame_relations:
            key = (self._node_uid(node1), self._node_uid(node2), relation)
            if key in seen:
                continue
            seen.add(key)
            self._upsert_edge(node1, node2, relation)
            upserted += 1
        fine_near_delta = self._refresh_fine_fine_near_edges(graph)
        after_edges = len(graph.get_edges())
        return {
            "allowed_relations": sorted(ALLOWED_LIFELONG_RELATIONS),
            "invalid_deleted": invalid_deleted,
            "parent_deleted": parent_deleted,
            "coarse_near_delta": coarse_near_delta,
            "fine_near_delta": fine_near_delta,
            "geometry_relation_count": len(frame_relations),
            "geometry_relations": relation_rows[: self._debug_max_items()],
            "edge_count_before_geometry": before_edges,
            "edge_count_after_geometry": after_edges,
            "upserted_geometry_edges": upserted,
        }

    def _write_geometry_relation_debug_log(self, payload: Dict[str, Any]) -> None:
        if not _debug_matching_enabled():
            return
        lines = [
            "[ISBench][UniGoalGroundedSAM][geometry_relation] "
            f"frame={payload.get('frame_index')} segment={payload.get('segment_index')} "
            f"relations={payload.get('geometry_relation_count')} "
            f"edges_before={payload.get('edge_count_before_geometry')} "
            f"edges_after={payload.get('edge_count_after_geometry')} "
            f"invalid_deleted={payload.get('invalid_deleted')}"
        ]
        for item in payload.get("geometry_relations", [])[: self._debug_max_items()]:
            lines.append(
                "[ISBench][UniGoalGroundedSAM][geometry_relation][edge] "
                f"{item.get('source_uid')}:{item.get('source_caption')} "
                f"-[{item.get('relation')}]-> "
                f"{item.get('target_uid')}:{item.get('target_caption')}"
            )
        _append_debug_log(lines)

    def _run_debugged_update_scenegraph(self, graph: Any, frame: FrameObservation) -> None:
        if not _debug_matching_enabled():
            self._run_unigoal_scenegraph_update(graph, frame)
            return

        segment_count_before = len(getattr(graph, "segment2d_results", []))
        debug_payload: Dict[str, Any] = {
            "frame_index": int(frame.frame_index),
            "node_space": getattr(graph, "node_space", None),
            "spatial_threshold": float(getattr(graph.cfg, "sim_threshold_spatial", 0.0)),
            "filter_config": self._filter_config(graph),
            "objects_before": [
                self._map_object_debug_summary(obj, index)
                for index, obj in enumerate(list(getattr(graph, "objects", [])))
            ],
            "detections": [],
            "spatial_sim_raw": [],
            "spatial_sim_after_threshold": [],
            "merge_decisions": [],
        }

        graph_module = None
        originals: Dict[str, Any] = {}
        try:
            import src.graph.graph as graph_module
        except ImportError:
            graph_module = None

        if graph_module is not None:
            originals = {
                "compute_spatial_similarities": graph_module.compute_spatial_similarities,
                "merge_detections_to_objects": graph_module.merge_detections_to_objects,
            }

            def _debug_compute_spatial_similarities(cfg, detection_list, objects):
                spatial_sim = originals["compute_spatial_similarities"](
                    cfg, detection_list, objects
                )
                debug_payload["detections"] = [
                    self._detection_debug_summary(det, index=index)
                    for index, det in enumerate(detection_list)
                ]
                debug_payload["spatial_sim_raw"] = self._debug_matrix(spatial_sim)
                debug_payload["objects_before_similarity"] = [
                    self._map_object_debug_summary(obj, index)
                    for index, obj in enumerate(objects)
                ]
                return spatial_sim

            def _debug_merge_detections_to_objects(cfg, detection_list, objects, agg_sim):
                debug_payload["spatial_sim_after_threshold"] = self._debug_matrix(agg_sim)
                raw_sim = debug_payload.get("spatial_sim_raw", [])
                debug_payload["merge_decisions"] = self._build_mapping_debug_decisions(
                    detection_list,
                    objects,
                    agg_sim,
                    raw_sim,
                )
                merged = originals["merge_detections_to_objects"](
                    cfg, detection_list, objects, agg_sim
                )
                debug_payload["objects_after_merge"] = [
                    self._map_object_debug_summary(obj, index)
                    for index, obj in enumerate(merged)
                ]
                return merged

            graph_module.compute_spatial_similarities = _debug_compute_spatial_similarities
            graph_module.merge_detections_to_objects = _debug_merge_detections_to_objects

        try:
            self._run_unigoal_scenegraph_update(graph, frame)
        finally:
            if originals and graph_module is not None:
                graph_module.compute_spatial_similarities = originals[
                    "compute_spatial_similarities"
                ]
                graph_module.merge_detections_to_objects = originals[
                    "merge_detections_to_objects"
                ]

        segment_count_after = len(getattr(graph, "segment2d_results", []))
        segment_index = segment_count_after - 1 if segment_count_after > segment_count_before else -1
        debug_payload["segment_index"] = segment_index
        debug_payload["grounded_sam"] = (
            self._segment_debug_summary(graph.segment2d_results[segment_index])
            if segment_index >= 0
            else []
        )
        if not debug_payload["detections"] and not debug_payload["objects_before"]:
            debug_payload["merge_decisions"] = [
                {
                    "detection": detection,
                    "action": "initial_add",
                    "matched_object_index": None,
                    "matched_score_after_threshold": None,
                    "matched_object": None,
                    "raw_top_scores": [],
                }
                for detection in debug_payload["grounded_sam"]
            ]
        debug_payload["objects_after"] = [
            self._map_object_debug_summary(obj, index)
            for index, obj in enumerate(list(getattr(graph, "objects", [])))
        ]
        debug_payload["objects_post"] = [
            self._map_object_debug_summary(obj, index)
            for index, obj in enumerate(list(getattr(graph, "objects_post", [])))
        ]
        self.last_mapping_debug = to_builtin(debug_payload)
        try:
            write_frame_debug_artifacts(
                output_dir_from_config(self.scene_graph_config),
                frame,
                self.last_mapping_debug,
                (
                    graph.segment2d_results[segment_index]
                    if segment_index >= 0
                    else None
                ),
            )
        except Exception as exc:
            _append_debug_log(
                [
                    "[ISBench][UniGoalGroundedSAM][artifact_error] "
                    f"frame={frame.frame_index} type={exc.__class__.__name__} message={exc}"
                ]
            )
        self._write_grounded_sam_debug_log(self.last_mapping_debug)

    def _filter_config(self, graph: Any) -> Dict[str, Any]:
        cfg = getattr(graph, "cfg", None)
        return {
            "obj_min_points": getattr(cfg, "obj_min_points", None),
            "obj_min_detections": getattr(cfg, "obj_min_detections", None),
            "sim_threshold_spatial": getattr(cfg, "sim_threshold_spatial", None),
        }

    def _write_lightweight_artifacts(
        self,
        frame: FrameObservation,
        result: PerceptionResult,
    ) -> None:
        try:
            write_result_artifacts(output_dir_from_config(self.scene_graph_config), frame, result)
        except Exception as exc:
            result.metadata["unigoal_artifact_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }

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

    def _map_object_debug_summary(self, map_object: Any, index: Optional[int] = None) -> Dict[str, Any]:
        if not hasattr(map_object, "get"):
            return {"index": index, "type": type(map_object).__name__}
        summary: Dict[str, Any] = {
            "index": index,
            "class_name": list(map_object.get("class_name", [])),
            "image_idx": list(map_object.get("image_idx", [])),
            "mask_idx": list(map_object.get("mask_idx", [])),
            "num_detections": int(map_object.get("num_detections", 0) or 0),
        }
        node = map_object.get("node")
        if node is not None:
            summary.update(
                {
                    "node_caption": getattr(node, "caption", None),
                    "node_uid": getattr(node, "uid", self.node_uids.get(node)),
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
            summary["bbox_center"] = self._bbox_center(bbox)
            summary["bbox_extent"] = self._bbox_extent(bbox)
            summary["bbox_volume"] = self._bbox_volume(bbox)
        return summary

    def _detection_debug_summary(
        self,
        detection: Any,
        index: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not hasattr(detection, "get"):
            return {"index": index, "type": type(detection).__name__}
        summary = {
            "index": index,
            "class_name": self._debug_first(detection.get("class_name")),
            "mask_idx": self._debug_first(detection.get("mask_idx")),
            "image_idx": self._debug_first(detection.get("image_idx")),
            "confidence": self._debug_first(detection.get("conf")),
            "pixel_area": self._debug_first(detection.get("pixel_area")),
            "point_count": self._debug_first(detection.get("n_points")),
            "xyxy": to_builtin(self._debug_first(detection.get("xyxy"))),
        }
        bbox = detection.get("bbox")
        if bbox is not None:
            summary["bbox_center"] = self._bbox_center(bbox)
            summary["bbox_extent"] = self._bbox_extent(bbox)
        return summary

    def _segment_debug_summary(self, segment: Dict[str, Any]) -> List[Dict[str, Any]]:
        xyxy = segment.get("xyxy", [])
        confidence = segment.get("confidence", [])
        masks = segment.get("mask", [])
        captions = segment.get("caption", [])
        class_ids = segment.get("class_id", [])
        count = max(len(xyxy), len(confidence), len(masks), len(captions), len(class_ids))
        rows = []
        for index in range(count):
            mask = masks[index] if index < len(masks) else None
            rows.append(
                {
                    "index": index,
                    "caption": captions[index] if index < len(captions) else None,
                    "class_name": captions[index] if index < len(captions) else None,
                    "class_id": int(class_ids[index]) if index < len(class_ids) else None,
                    "xyxy": to_builtin(xyxy[index]) if index < len(xyxy) else None,
                    "confidence": float(confidence[index]) if index < len(confidence) else None,
                    "mask_area": int(np.asarray(mask).sum()) if mask is not None else None,
                }
            )
        return rows

    def _build_mapping_debug_decisions(
        self,
        detections: Any,
        objects: Any,
        thresholded_sim: Any,
        raw_sim: List[List[float]],
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
                    "detection": self._detection_debug_summary(detection, det_index),
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

    def _write_grounded_sam_debug_log(self, payload: Dict[str, Any]) -> None:
        if not _debug_matching_enabled():
            return
        max_items = self._debug_max_items()
        lines = [
            "[ISBench][UniGoalGroundedSAM] "
            f"frame={payload.get('frame_index')} segment={payload.get('segment_index')} "
            f"grounded_sam={len(payload.get('grounded_sam', []))} "
            f"detections={len(payload.get('detections', []))} "
            f"objects_before={len(payload.get('objects_before', []))} "
            f"objects_after={len(payload.get('objects_after', []))} "
            f"threshold={payload.get('spatial_threshold')} "
            f"node_space={payload.get('node_space')}"
        ]
        for item in payload.get("grounded_sam", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalGroundedSAM][grounded_dino_sam1] "
                f"idx={item.get('index')} caption={item.get('caption')} "
                f"bbox={item.get('xyxy')} confidence={item.get('confidence')} "
                f"mask_area={item.get('mask_area')}"
            )
        for detection in payload.get("detections", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalGroundedSAM][mapping_detection] "
                f"idx={detection.get('index')} class={detection.get('class_name')} "
                f"xyxy={detection.get('xyxy')} points={detection.get('point_count')} "
                f"center={detection.get('bbox_center')}"
            )
        for obj in payload.get("objects_before", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalGroundedSAM][object_before] "
                f"idx={obj.get('index')} caption={obj.get('node_caption')} "
                f"uid={obj.get('node_uid')} class_history={obj.get('class_name')} "
                f"detections={obj.get('num_detections')} center={obj.get('bbox_center')}"
            )
        for decision in payload.get("merge_decisions", [])[:max_items]:
            detection = decision.get("detection", {})
            matched = decision.get("matched_object") or {}
            lines.append(
                "[ISBench][UniGoalGroundedSAM][decision] "
                f"det_idx={detection.get('index')} class={detection.get('class_name')} "
                f"action={decision.get('action')} "
                f"matched_idx={decision.get('matched_object_index')} "
                f"score={decision.get('matched_score_after_threshold')} "
                f"matched_caption={matched.get('node_caption')} "
                f"raw_top={decision.get('raw_top_scores')}"
            )
        for obj in payload.get("objects_after", [])[:max_items]:
            lines.append(
                "[ISBench][UniGoalGroundedSAM][object_after] "
                f"idx={obj.get('index')} caption={obj.get('node_caption')} "
                f"uid={obj.get('node_uid')} class_history={obj.get('class_name')} "
                f"detections={obj.get('num_detections')} center={obj.get('bbox_center')}"
            )
        _append_debug_log(lines)

    def _frame_to_unigoal_observation(self, frame: FrameObservation) -> Dict[str, Any]:
        depth = frame.depth
        if depth is None:
            raise RuntimeError("UniGoal backend requires depth or depth_linear observation")
        if depth.ndim == 2:
            depth = depth[..., None]

        pose_matrix = frame.camera_pose
        if pose_matrix is None:
            pose_matrix = np.eye(4, dtype=np.float32)

        robot_position = frame.robot_position or [0.0, 0.0, 0.0]
        return {
            "rgb": frame.rgb,
            "depth": np.asarray(depth, dtype=np.float32),
            "pose_matrix": np.asarray(pose_matrix, dtype=np.float32),
            "gps": np.asarray(robot_position[:2], dtype=np.float32),
            "compass": np.asarray([0.0], dtype=np.float32),
            "camera_intrinsics": None if frame.intrinsics is None else np.asarray(frame.intrinsics, dtype=np.float32),
        }

    def _unigoal_navigation_camera_matrix(self, frame: FrameObservation) -> Optional[Any]:
        """Return the camera object expected by UniGoal navigation mapping3d."""
        intrinsics = frame.intrinsics
        if intrinsics is None:
            return None
        if all(hasattr(intrinsics, attr) for attr in ("f", "xc", "zc")):
            return intrinsics
        if hasattr(intrinsics, "detach"):
            intrinsics = intrinsics.detach().cpu().numpy()
        matrix = np.asarray(intrinsics, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] < 3 or matrix.shape[1] < 3:
            return None

        fx = float(matrix[0, 0])
        fy = float(matrix[1, 1])
        valid_focal = [value for value in (fx, fy) if np.isfinite(value) and value > 0.0]
        if not valid_focal:
            return None
        focal = sum(valid_focal) / len(valid_focal)
        return SimpleNamespace(
            f=float(focal),
            xc=float(matrix[0, 2]),
            zc=float(matrix[1, 2]),
        )

    def _camera_matrix_metadata(self, graph: Any) -> Dict[str, Any]:
        camera_matrix = getattr(graph, "camera_matrix", None)
        data: Dict[str, Any] = {
            "type": type(camera_matrix).__name__ if camera_matrix is not None else None,
        }
        if camera_matrix is None:
            return data
        if all(hasattr(camera_matrix, attr) for attr in ("f", "xc", "zc")):
            data.update(
                {
                    "f": float(camera_matrix.f),
                    "xc": float(camera_matrix.xc),
                    "zc": float(camera_matrix.zc),
                }
            )
            return data
        try:
            data["matrix"] = to_builtin(np.asarray(camera_matrix, dtype=np.float32))
        except Exception:
            data["repr"] = repr(camera_matrix)
        return data

    def _result_from_graph(self, graph: Any, frame: FrameObservation) -> PerceptionResult:
        objects = []
        node_id_by_obj = {}
        for node in graph.get_nodes():
            caption = str(getattr(node, "caption", "object") or "object")
            uid = self._node_uid(node)
            object_id = f"unigoal_object:{uid}"
            node_id_by_obj[node] = object_id
            room_node = getattr(node, "room_node", None)
            position = self._node_position(node)
            is_vis = self._node_visible_in_latest_segment(graph, node)
            latest_observation = self._node_latest_segment_observation(graph, node)
            objects.append(
                PerceivedObject(
                    object_id=object_id,
                    name=caption,
                    category=caption,
                    bbox=latest_observation.get("bbox"),
                    mask=latest_observation.get("mask"),
                    position=to_builtin(position),
                    room_id=getattr(room_node, "caption", None),
                    confidence=float(getattr(node, "score", 1.0) or 1.0),
                    attributes={
                        "source": self.name,
                        "uid": uid,
                        "trace_id": f"node@{id(node)}",
                        "source_ids": {
                            "unigoal_node": f"node@{uid}",
                            "unigoal_object": object_id,
                        },
                        "normalized_label": _normalized_key(caption),
                        "caption": caption,
                        "is_coarse": self._is_coarse_caption(caption),
                        "is_vis": is_vis,
                        "currently_visible": is_vis,
                        "last_seen_step": frame.frame_index if is_vis else None,
                        "latest_segment_index": latest_observation.get("segment_index"),
                        "latest_mask_index": latest_observation.get("mask_index"),
                        "mask_area": latest_observation.get("mask_area"),
                        "bbox_3d": self._node_bbox_3d(node),
                        "exploration_level": int(getattr(node, "exploration_level", 0)),
                        "distance": float(getattr(node, "distance", 0.0) or 0.0),
                    },
                )
            )

        relations = []
        for edge in graph.get_edges():
            source_id = node_id_by_obj.get(edge.node1)
            target_id = node_id_by_obj.get(edge.node2)
            if source_id is None or target_id is None:
                continue
            relation = str(getattr(edge, "relation", "") or "")
            if not relation:
                continue
            relations.append(
                PerceivedRelation(
                    source_id=source_id,
                    target_id=target_id,
                    relation=relation,
                    confidence=1.0,
                    source=self.name,
                )
            )

        scene_graph = graph.get_scenegraph()
        scene_goal_matches = self._compute_scene_goal_matches(graph)
        return PerceptionResult(
            backend=self.name,
            frame_index=frame.frame_index,
            objects=objects,
            relations=relations,
            scene_graph=to_builtin(scene_graph),
            room_graph=self._room_graph_from_graph(graph, node_id_by_obj),
            group_graph=self._group_graph_from_graph(graph, node_id_by_obj),
            goal_graph=to_builtin(getattr(graph, "goalgraph", {})),
            scene_goal_matches=scene_goal_matches,
            metadata={
                "sensor_name": frame.sensor_name,
                "rgb_shape": list(frame.rgb.shape),
                "depth_shape": None if frame.depth is None else list(frame.depth.shape),
                "object_goal": self.object_goal,
                "node_space": getattr(graph, "node_space", None),
                "device": getattr(graph, "device", None),
                "filter_config": self._filter_config(graph),
                "camera_matrix": self._camera_matrix_metadata(graph),
                "iou_fallback": {
                    "installed": self.iou_fallback_patch_installed,
                    "used": self.iou_fallback_used,
                    "mode": "axis_aligned_without_pytorch3d"
                    if self.iou_fallback_used
                    else "pytorch3d_or_unused",
                },
                "vendor": "UniGoal/src/graph/graph.py",
                "mapping_debug": self.last_mapping_debug,
                "relation_backend": self._relation_backend(),
                "relation_debug": self.last_relation_debug,
            },
        )

    def _node_uid(self, node: Any) -> int:
        existing = getattr(node, "uid", None)
        if isinstance(existing, int):
            self.node_uids[node] = existing
            self.next_node_uid = max(self.next_node_uid, existing + 1)
            return existing
        if node not in self.node_uids:
            self.node_uids[node] = self.next_node_uid
            self.next_node_uid += 1
        uid = self.node_uids[node]
        setattr(node, "uid", uid)
        return uid

    def _node_position(self, node: Any) -> Optional[List[float]]:
        map_object = getattr(node, "object", None)
        if isinstance(map_object, dict):
            pcd = map_object.get("pcd")
            if pcd is not None:
                try:
                    points = np.asarray(pcd.points, dtype=np.float32)
                    if points.size:
                        return [round(float(item), 4) for item in points.mean(axis=0)[:3]]
                except Exception:
                    pass
            bbox = map_object.get("bbox")
            if bbox is not None:
                center = self._bbox_center(bbox)
                if center is not None:
                    return center
        return to_builtin(getattr(node, "center", None))

    def _node_bbox_3d(self, node: Any) -> Optional[Dict[str, Any]]:
        map_object = getattr(node, "object", None)
        if not isinstance(map_object, dict):
            return None
        bbox = map_object.get("bbox")
        if bbox is None:
            return None
        data = {
            "center": self._bbox_center(bbox),
            "extent": self._bbox_extent(bbox),
        }
        volume = self._bbox_volume(bbox)
        if volume is not None:
            data["volume"] = volume
        return data

    def _bbox_center(self, bbox: Any) -> Optional[List[float]]:
        try:
            if hasattr(bbox, "get_center"):
                return self._debug_float_list(bbox.get_center())
            center = getattr(bbox, "center", None)
            if center is not None:
                return self._debug_float_list(center)
        except Exception:
            return None
        return None

    def _bbox_extent(self, bbox: Any) -> Optional[List[float]]:
        try:
            if hasattr(bbox, "get_extent"):
                return self._debug_float_list(bbox.get_extent())
            extent = getattr(bbox, "extent", None)
            if extent is not None:
                return self._debug_float_list(extent)
            points = self._bbox_points(bbox)
            if points is not None and points.size:
                return self._debug_float_list(points.max(axis=0) - points.min(axis=0))
        except Exception:
            return None
        return None

    def _bbox_volume(self, bbox: Any) -> Optional[float]:
        try:
            if hasattr(bbox, "volume"):
                return float(bbox.volume())
        except Exception:
            pass
        extent = self._bbox_extent(bbox)
        if extent is None:
            return None
        try:
            return float(np.prod(np.asarray(extent, dtype=np.float64)))
        except Exception:
            return None

    def _bbox_points(self, bbox: Any) -> Optional[np.ndarray]:
        try:
            if hasattr(bbox, "get_box_points"):
                points = bbox.get_box_points()
            else:
                points = getattr(bbox, "box_points", None)
            if points is None:
                return None
            points = np.asarray(points, dtype=np.float64)
            if points.ndim != 2 or points.shape[1] < 3:
                return None
            return points[:, :3]
        except Exception:
            return None

    def _node_visible_in_latest_segment(self, graph: Any, node: Any) -> bool:
        latest_segment = len(getattr(graph, "segment2d_results", [])) - 1
        if latest_segment < 0:
            return True
        map_object = getattr(node, "object", None)
        if not isinstance(map_object, dict):
            return True
        return latest_segment in list(map_object.get("image_idx", []))

    def _node_latest_segment_observation(self, graph: Any, node: Any) -> Dict[str, Any]:
        latest_segment = len(getattr(graph, "segment2d_results", [])) - 1
        if latest_segment < 0:
            return {}
        map_object = getattr(node, "object", None)
        if not isinstance(map_object, dict):
            return {}
        image_indices = list(map_object.get("image_idx", []))
        mask_indices = list(map_object.get("mask_idx", []))
        try:
            history_index = len(image_indices) - 1 - image_indices[::-1].index(latest_segment)
        except ValueError:
            return {}
        if history_index < 0 or history_index >= len(mask_indices):
            return {}
        try:
            mask_index = int(mask_indices[history_index])
        except Exception:
            return {}
        segment = graph.segment2d_results[latest_segment]
        masks = segment.get("mask", [])
        xyxy = segment.get("xyxy", [])
        mask = None
        bbox = None
        if 0 <= mask_index < len(masks):
            try:
                mask = np.asarray(masks[mask_index], dtype=bool)
            except Exception:
                mask = None
        if 0 <= mask_index < len(xyxy):
            try:
                bbox = [float(value) for value in to_builtin(xyxy[mask_index])[:4]]
            except Exception:
                bbox = None
        return {
            "segment_index": latest_segment,
            "mask_index": mask_index,
            "bbox": bbox,
            "mask": mask,
            "mask_area": None if mask is None else int(mask.sum()),
        }

    def _is_coarse_caption(self, caption: str) -> bool:
        return _lifelong_is_coarse_caption(caption)

    def _room_graph_from_graph(self, graph: Any, node_id_by_obj: Dict[Any, str]) -> Dict[str, Any]:
        rooms = []
        for room_node in getattr(graph, "room_nodes", []):
            object_ids = [
                node_id_by_obj[node]
                for node in sorted(room_node.nodes, key=lambda item: getattr(item, "caption", ""))
                if node in node_id_by_obj
            ]
            rooms.append(
                {
                    "id": room_node.caption,
                    "caption": room_node.caption,
                    "object_count": len(object_ids),
                    "objects": object_ids,
                    "group_count": len(room_node.group_nodes),
                }
            )
        return {"rooms": rooms}

    def _group_graph_from_graph(self, graph: Any, node_id_by_obj: Dict[Any, str]) -> Dict[str, Any]:
        groups = []
        for room_node in getattr(graph, "room_nodes", []):
            for group_index, group_node in enumerate(room_node.group_nodes):
                object_ids = [
                    node_id_by_obj[node]
                    for node in group_node.nodes
                    if node in node_id_by_obj
                ]
                groups.append(
                    {
                        "id": f"{room_node.caption}:{group_index}",
                        "room": room_node.caption,
                        "caption": group_node.caption,
                        "center": to_builtin(group_node.center),
                        "center_object": (
                            node_id_by_obj.get(group_node.center_node)
                            if group_node.center_node is not None
                            else None
                        ),
                        "objects": object_ids,
                        "edge_count": len(group_node.edges),
                        "corr_score": float(getattr(group_node, "corr_score", 0.0)),
                    }
                )
        return {"groups": groups}

    def _compute_scene_goal_matches(self, graph: Any) -> Dict[str, Any]:
        goal_graph = getattr(graph, "goalgraph", None)
        if not goal_graph or not goal_graph.get("nodes"):
            return {"enabled": True, "overlap_score": None, "reason": "empty_goal_graph"}
        if len(getattr(graph, "nodes", [])) == 0:
            return {"enabled": True, "overlap_score": None, "reason": "empty_scene_graph"}
        try:
            overlap_score = graph.overlap()
        except Exception as exc:
            return {
                "enabled": True,
                "overlap_score": None,
                "error": exc.__class__.__name__,
                "message": str(exc),
            }
        common_nodes = []
        matcher = getattr(graph, "matcher", None)
        if matcher is not None:
            common_nodes = to_builtin(getattr(matcher, "common_nodes", [])) or []
        return {
            "enabled": True,
            "overlap_score": float(overlap_score),
            "common_nodes": common_nodes,
        }
