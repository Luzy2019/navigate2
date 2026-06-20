import os
import re
from typing import Any, Dict, List, Optional

from og_ego_prim.primitives.executor import LowLevelStepContext

from .backends import build_perception_backend
from .base import SceneGraphUpdater
from .perception import PerceptionResult
from .schema import SceneGraphEdge, SceneGraphNode, SceneGraphSnapshot
from .unigoal_memory_scene_graph import UniGoalMemorySceneGraphUpdater


def _env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _target_from_raw_plan(raw_plan: Optional[str]) -> Optional[str]:
    if not raw_plan:
        return None
    match = re.match(r"\s*NAVIGATE_TO\s*\((.*)\)\s*", raw_plan, flags=re.IGNORECASE)
    if match is None:
        return None
    target = match.group(1).split(",")[0].strip()
    if "@" in target:
        target = target.split("@", 1)[0].strip()
    target = target.replace(".n.01", "").replace("_", " ").strip()
    return target or None


class PerceptionSceneGraphUpdater(SceneGraphUpdater):
    """Scene graph updater backed by first-person RGB-D perception."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
        update_every: Optional[int] = None,
        sensor_name: Optional[str] = None,
    ):
        self.backend_name = backend_name or os.environ.get(
            "ISBENCH_SCENE_GRAPH_BACKEND",
            "omnigibson_truth",
        )
        self.update_every = update_every or _env_int("ISBENCH_SCENE_GRAPH_UPDATE_EVERY", 5)
        self.sensor_name = sensor_name or os.environ.get("ISBENCH_SCENE_GRAPH_SENSOR_NAME")
        self.env = None
        self.global_step_index = 0
        self.latest_result: Optional[PerceptionResult] = None
        self.snapshot = SceneGraphSnapshot(step_index=-1, primitive_name=None, raw_plan=None)
        self.perception_errors: List[Dict[str, Any]] = []

        if self.backend_name.lower() in {"truth", "omnigibson_truth", "unigoal_memory", "disabled", "none"}:
            self.truth_updater: Optional[SceneGraphUpdater] = UniGoalMemorySceneGraphUpdater()
            self.backend = None
        else:
            self.truth_updater = None
            self.backend = build_perception_backend(self.backend_name, sensor_name=self.sensor_name)

    def reset(self, env: Any):
        self.env = env
        self.global_step_index = 0
        self.latest_result = None
        self.perception_errors.clear()

        if self.truth_updater is not None:
            self.snapshot = self.truth_updater.reset(env)
            return self.snapshot

        self.backend.reset(env)
        self.snapshot = self._run_perception(context=None, force=True)
        return self.snapshot

    def update(
        self,
        context: Optional[LowLevelStepContext] = None,
    ) -> SceneGraphSnapshot:
        if self.truth_updater is not None:
            return self.truth_updater.update(context)

        target = _target_from_raw_plan(context.raw_plan if context is not None else None)
        if target and hasattr(self.backend, "set_object_goal"):
            self.backend.set_object_goal(target)

        force = context is None
        should_update = force or self.latest_result is None or self.global_step_index % self.update_every == 0
        if should_update:
            self.snapshot = self._run_perception(context=context, force=force)
        else:
            self.snapshot = self._snapshot_from_result(
                self.latest_result,
                context=context,
                skipped=True,
            )
        self.global_step_index += 1
        return self.snapshot

    def get_snapshot(self) -> SceneGraphSnapshot:
        if self.truth_updater is not None:
            return self.truth_updater.get_snapshot()
        return self.snapshot

    def to_prompt_context(self) -> str:
        return self.get_snapshot().to_prompt_context()

    def _run_perception(
        self,
        context: Optional[LowLevelStepContext],
        force: bool,
    ) -> SceneGraphSnapshot:
        try:
            frame = self.backend.observe(self.env)
            result = self.backend.detect(frame)
            result = self.backend.update_memory(result)
            self.latest_result = result
        except (ImportError, FileNotFoundError) as exc:
            raise RuntimeError(
                f"scene graph backend {self.backend_name!r} is not ready: {exc}"
            ) from exc
        except Exception as exc:
            error = {
                "backend": self.backend_name,
                "type": exc.__class__.__name__,
                "message": str(exc),
                "global_step_index": self.global_step_index,
            }
            self.perception_errors.append(error)
            raise RuntimeError(
                f"scene graph backend {self.backend_name!r} failed during perception: {exc}"
            ) from exc

        return self._snapshot_from_result(result, context=context, skipped=False, force=force)

    def _snapshot_from_result(
        self,
        result: Optional[PerceptionResult],
        context: Optional[LowLevelStepContext],
        skipped: bool,
        force: bool = False,
    ) -> SceneGraphSnapshot:
        primitive_name = None if context is None else context.primitive_name
        raw_plan = None if context is None else context.raw_plan
        step_index = self.global_step_index if context is None else context.step_index
        if result is None:
            return SceneGraphSnapshot(
                step_index=step_index,
                primitive_name=primitive_name,
                raw_plan=raw_plan,
                metadata={
                    "source": "perception",
                    "ready": False,
                    "perception_backend": self.backend_name,
                    "global_step_index": self.global_step_index,
                    "perception_errors": self.perception_errors,
                },
            )

        nodes, membership_edges = self._nodes_and_membership_edges(result)
        relation_edges = [
            SceneGraphEdge(
                source_id=relation.source_id,
                target_id=relation.target_id,
                relation=relation.relation,
                source=relation.source,
                confidence=relation.confidence,
            )
            for relation in result.relations
        ]

        metadata = {
            "source": "perception",
            "ready": True,
            "perception_backend": result.backend,
            "global_step_index": self.global_step_index,
            "frame_index": result.frame_index,
            "perception_skipped": skipped,
            "perception_forced": force,
            "object_count": len(result.objects),
            "relation_count": len(result.relations),
            "membership_edge_count": len(membership_edges),
            "total_edge_count": len(relation_edges) + len(membership_edges),
            "goal_graph": result.goal_graph,
            "room_graph": result.room_graph,
            "group_graph": result.group_graph,
            "scene_goal_matches": result.scene_goal_matches,
            "perception_errors": list(self.perception_errors),
            "backend_metadata": result.metadata,
            "scene_graph": result.scene_graph,
        }
        return SceneGraphSnapshot(
            step_index=step_index,
            primitive_name=primitive_name,
            raw_plan=raw_plan,
            nodes=nodes,
            edges=relation_edges + membership_edges,
            metadata=metadata,
        )

    def _nodes_and_membership_edges(
        self,
        result: PerceptionResult,
    ) -> tuple[List[SceneGraphNode], List[SceneGraphEdge]]:
        nodes: List[SceneGraphNode] = []
        edges: List[SceneGraphEdge] = []
        room_ids = set()
        group_ids = set()

        object_to_group = {}
        for group in result.group_graph.get("groups", []):
            group_id = f"group:{group.get('id', len(group_ids))}"
            group_ids.add(group_id)
            for object_id in group.get("objects", []):
                object_to_group[str(object_id)] = group_id

        for obj in result.objects:
            room_id = f"room:{obj.room_id or 'unknown_room'}"
            room_ids.add(room_id)
            nodes.append(
                SceneGraphNode(
                    object_id=obj.object_id,
                    name=obj.name,
                    category=obj.category,
                    visible=bool(obj.attributes.get("currently_visible", True)),
                    position=obj.position,
                    states={
                        "bbox": obj.bbox,
                        "room_id": obj.room_id,
                        "confidence": obj.confidence,
                        "attributes": obj.attributes,
                    },
                )
            )
            edges.append(
                SceneGraphEdge(
                    source_id=obj.object_id,
                    target_id=room_id,
                    relation="in_room",
                    source=result.backend,
                    confidence=1.0,
                )
            )
            group_id = object_to_group.get(obj.object_id)
            if group_id is not None:
                edges.append(
                    SceneGraphEdge(
                        source_id=obj.object_id,
                        target_id=group_id,
                        relation="in_group",
                        source=result.backend,
                        confidence=1.0,
                    )
                )

        for room in result.room_graph.get("rooms", []):
            room_id = f"room:{room.get('id', room.get('caption', 'unknown_room'))}"
            room_ids.add(room_id)
        for room_id in sorted(room_ids):
            nodes.append(
                SceneGraphNode(
                    object_id=room_id,
                    name=room_id.removeprefix("room:"),
                    category="room",
                    visible=True,
                    states={"source": result.backend},
                )
            )

        for group in result.group_graph.get("groups", []):
            group_id = f"group:{group.get('id', len(group_ids))}"
            group_ids.add(group_id)
            nodes.append(
                SceneGraphNode(
                    object_id=group_id,
                    name=str(group.get("caption") or group_id),
                    category="group",
                    visible=True,
                    position=group.get("center"),
                    states={
                        "room": group.get("room"),
                        "object_count": len(group.get("objects", [])),
                        "source": result.backend,
                    },
                )
            )

        return nodes, edges
