import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import numpy as np

from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)

from .utils import insert_sys_path, repo_root, room_lookup_from_env, to_builtin


class UniGoalGroundedSAMBackend:
    name = "unigoal_grounded_sam"

    def __init__(self, sensor_name: Optional[str] = None):
        self.adapter = ISBenchObservationAdapter(sensor_name=sensor_name)
        self.env = None
        self.graph = None
        self.object_goal: Optional[str] = None
        self.last_result: Optional[PerceptionResult] = None
        self.room_lookup = None

    def reset(self, env: Any) -> None:
        self.env = env
        self.graph = None
        self.last_result = None
        self.adapter.reset()
        self.adapter.ensure_robot_sensor_modalities(env)
        self.room_lookup = room_lookup_from_env(env)

    def set_object_goal(self, object_goal: Optional[str]):
        if object_goal:
            object_goal = object_goal.replace("_", " ").strip()
        self.object_goal = object_goal or self.object_goal
        if self.graph is not None and self.object_goal:
            self.graph.set_object_goal(self.object_goal)

    def observe(self, env: Any) -> FrameObservation:
        return self.adapter.observe(env)

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        graph = self._ensure_graph(frame)
        observations = self._frame_to_unigoal_observation(frame)
        if frame.intrinsics is not None:
            graph.camera_matrix = np.asarray(frame.intrinsics, dtype=np.float32)
        graph.set_observations(observations)
        graph.set_navigate_steps(frame.frame_index)
        graph.update_scenegraph()
        graph.update_group()

        result = self._result_from_graph(graph, frame)
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
        map_resolution = int(os.environ.get("ISBENCH_UNIGOAL_MAP_RESOLUTION", "5"))
        map_size_cm = int(os.environ.get("ISBENCH_UNIGOAL_MAP_SIZE_CM", "2400"))
        map_size = int(map_size_cm / map_resolution)
        device = os.environ.get(
            "ISBENCH_SCENE_GRAPH_DEVICE",
            "cuda" if torch.cuda.is_available() else "cpu",
        )
        args = SimpleNamespace(
            map_resolution=map_resolution,
            map_size_cm=map_size_cm,
            map_size=map_size,
            env_frame_height=height,
            env_frame_width=width,
            hfov=float(os.environ.get("ISBENCH_SCENE_GRAPH_HFOV", "90")),
            device=device,
            base_url=os.environ.get("ISBENCH_SCENE_GRAPH_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
            api_key=os.environ.get("ISBENCH_SCENE_GRAPH_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "EMPTY",
            llm_model=os.environ.get("ISBENCH_SCENE_GRAPH_LLM_MODEL", "gpt-4o-mini"),
            vlm_model=os.environ.get("ISBENCH_SCENE_GRAPH_VLM_MODEL", "gpt-4o-mini"),
        )
        graph = Graph(args, is_navigation=True)
        graph.set_room_lookup(self.room_lookup)
        if self.object_goal:
            graph.set_object_goal(self.object_goal)
        self.graph = graph
        return graph

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

    def _result_from_graph(self, graph: Any, frame: FrameObservation) -> PerceptionResult:
        objects = []
        caption_count: Dict[str, int] = {}
        node_id_by_obj = {}
        for node in graph.get_nodes():
            caption = str(getattr(node, "caption", "object") or "object")
            unique_index = caption_count.get(caption, 0)
            caption_count[caption] = unique_index + 1
            object_id = f"{caption.replace(' ', '_')}:{unique_index}"
            node_id_by_obj[node] = object_id
            room_node = getattr(node, "room_node", None)
            objects.append(
                PerceivedObject(
                    object_id=object_id,
                    name=caption,
                    category=caption,
                    position=to_builtin(getattr(node, "center", None)),
                    room_id=getattr(room_node, "caption", None),
                    confidence=float(getattr(node, "score", 1.0) or 1.0),
                    attributes={
                        "source": self.name,
                        "trace_id": f"node@{id(node)}",
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
                "vendor": "UniGoal/src/graph/graph.py",
            },
        )

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
