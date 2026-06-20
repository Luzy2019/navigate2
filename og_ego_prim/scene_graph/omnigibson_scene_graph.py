from typing import Any, Dict, List, Optional, Tuple

from omnigibson import object_states
from omnigibson.envs import Environment
from omnigibson.systems.system_base import BaseSystem

from og_ego_prim.primitives.executor import LowLevelStepContext
from og_ego_prim.primitives.object_states_utils import (
    get_visible_task_related_objects,
    is_target_object_predicate_with_obj,
)

from .base import SceneGraphUpdater
from .schema import SceneGraphEdge, SceneGraphNode, SceneGraphSnapshot


def _to_builtin(value: Any):
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        return [_to_builtin(item) for item in value]
    if isinstance(value, list):
        return [_to_builtin(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_builtin(item) for key, item in value.items()}
    return value


class OmniGibsonSceneGraphUpdater(SceneGraphUpdater):

    def __init__(self):
        self.env: Optional[Environment] = None
        self.snapshot = SceneGraphSnapshot(
            step_index=-1,
            primitive_name=None,
            raw_plan=None,
        )
        self.global_step_index = 0

    def reset(self, env: Environment):
        self.env = env
        self.global_step_index = 0
        self.snapshot = self._build_snapshot(context=None)
        return self.snapshot

    def update(
        self,
        context: Optional[LowLevelStepContext] = None,
    ) -> SceneGraphSnapshot:
        self.snapshot = self._build_snapshot(context=context)
        self.global_step_index += 1
        return self.snapshot

    def get_snapshot(self) -> SceneGraphSnapshot:
        return self.snapshot

    def to_prompt_context(self) -> str:
        return self.snapshot.to_prompt_context()

    def _build_snapshot(
        self,
        context: Optional[LowLevelStepContext],
    ) -> SceneGraphSnapshot:
        if self.env is None:
            return SceneGraphSnapshot(
                step_index=-1,
                primitive_name=None,
                raw_plan=None,
                metadata={"source": "omnigibson", "ready": False},
            )

        object_items = self._get_task_object_items()
        visible_ids = self._get_visible_object_ids()
        nodes = [
            self._build_node(object_id, obj, visible_ids)
            for object_id, obj in object_items
        ]
        edges = self._build_edges(object_items)

        primitive_name = None if context is None else context.primitive_name
        raw_plan = None if context is None else context.raw_plan
        step_index = self.global_step_index if context is None else context.step_index
        return SceneGraphSnapshot(
            step_index=step_index,
            primitive_name=primitive_name,
            raw_plan=raw_plan,
            nodes=nodes,
            edges=edges,
            metadata={
                "source": "omnigibson",
                "global_step_index": self.global_step_index,
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        )

    def _get_task_object_items(self) -> List[Tuple[str, Any]]:
        object_items = []
        for object_id, obj_ref in self.env.task.object_scope.items():
            if object_id.strip().split(".")[0].strip() in {"agent", "floor", "ceiling", "roof"}:
                continue

            obj = obj_ref.wrapped_obj
            if obj is None:
                continue

            if isinstance(obj, BaseSystem) and not self.env.scene.is_visual_particle_system(system_name=obj.name):
                continue

            object_items.append((object_id, obj))
        return object_items

    def _get_visible_object_ids(self) -> set:
        visible_ids = set()
        for visible_obj in get_visible_task_related_objects(self.env):
            if hasattr(visible_obj, "name"):
                visible_ids.add(visible_obj.name)
        return visible_ids

    def _build_node(
        self,
        object_id: str,
        obj: Any,
        visible_ids: set,
    ) -> SceneGraphNode:
        position, orientation = self._get_pose(obj)
        return SceneGraphNode(
            object_id=object_id,
            name=getattr(obj, "name", object_id),
            category=object_id.strip().split(".")[0],
            visible=getattr(obj, "name", None) in visible_ids,
            position=position,
            orientation=orientation,
            states=self._get_states(obj),
        )

    def _get_pose(self, obj: Any):
        if not hasattr(obj, "get_position_orientation"):
            return None, None

        try:
            position, orientation = obj.get_position_orientation()
        except Exception:
            return None, None

        return _to_builtin(position), _to_builtin(orientation)

    def _get_states(self, obj: Any) -> Dict[str, Any]:
        states = {}
        if not hasattr(obj, "states"):
            return states

        state_specs = {
            "open": object_states.Open,
            "toggled_on": object_states.ToggledOn,
        }
        for state_name, state_cls in state_specs.items():
            if state_cls not in obj.states:
                continue
            try:
                states[state_name] = _to_builtin(obj.states[state_cls].get_value())
            except Exception:
                continue

        return states

    def _build_edges(self, object_items: List[Tuple[str, Any]]) -> List[SceneGraphEdge]:
        edges = []
        relation_specs = [
            ("inside", object_states.Inside),
            ("on_top", object_states.OnTop),
        ]

        for source_id, source_obj in object_items:
            for target_id, target_obj in object_items:
                if source_id == target_id:
                    continue
                for relation_name, relation_cls in relation_specs:
                    try:
                        has_relation = is_target_object_predicate_with_obj(
                            source_obj,
                            target_obj,
                            relation_cls,
                        )
                    except Exception:
                        has_relation = False

                    if has_relation:
                        edges.append(
                            SceneGraphEdge(
                                source_id=source_id,
                                target_id=target_id,
                                relation=relation_name,
                            )
                        )

        return edges
