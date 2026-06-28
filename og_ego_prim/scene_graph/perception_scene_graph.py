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
    '''
    从原始 high-level plan 中提取 NAVIGATE_TO 的目标对象名。

    这个函数只处理导航动作；如果 raw_plan 不是 NAVIGATE_TO(...)，则返回 None。
    提取后会去掉描述符、WordNet 后缀和下划线，方便传给 perception backend 作为当前导航目标。

    使用位置：
        PerceptionSceneGraphUpdater.update() 中调用，用于在 NAVIGATE_TO 期间设置 backend 的 object_goal。

    示例：
        _target_from_raw_plan("navigate_to(half__banana.n.01_1)") -> "half  banana 1"
        _target_from_raw_plan("NAVIGATE_TO(apple@on the table)") -> "apple"
        _target_from_raw_plan("grasp(apple)") -> None
    '''
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


def _strip_plan_object(value: str) -> str:
    '''
    清理 primitive 参数中的对象名，去掉 @ 后面的自然语言描述。

    planner 可能生成 "apple@on the table" 这种带描述符的参数；scene graph 事件记录
    只需要对象名本身，因此这里保留 @ 前面的部分。

    使用位置：
        _parse_raw_action() 解析 raw_plan 参数时调用。

    示例：
        _strip_plan_object("apple@on the table") -> "apple"
        _strip_plan_object(" cabinet ") -> "cabinet"
    '''
    value = str(value).strip()
    if "@" in value:
        value = value.split("@", 1)[0].strip()
    return value


def _parse_raw_action(raw_plan: Optional[str]) -> tuple[Optional[str], List[str]]:
    '''
    解析原始 high-level plan，得到 primitive 名称和参数对象列表。

    primitive 会被转成大写；参数会按逗号拆分，并通过 _strip_plan_object(...)
    去掉 @ 后面的描述符。解析失败或 raw_plan 为空时返回 (None, [])。

    使用位置：
        PerceptionSceneGraphUpdater._manipulation_event_from_context() 中调用，
        用于根据 GRASP / PLACE_ON_TOP / PLACE_INSIDE / RELEASE 等动作记录 manipulation event。

    示例：
        _parse_raw_action("grasp(apple@on table)") -> ("GRASP", ["apple"])
        _parse_raw_action("place_inside(apple, cabinet)") -> ("PLACE_INSIDE", ["apple", "cabinet"])
        _parse_raw_action(None) -> (None, [])
    '''
    if not raw_plan:
        return None, []
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*", raw_plan)
    if match is None:
        return None, []
    primitive = match.group(1).strip().upper()
    raw_params = match.group(2).strip()
    params = (
        []
        if not raw_params
        else [_strip_plan_object(item) for item in raw_params.split(",")]
    )
    return primitive, [param for param in params if param]


class PerceptionSceneGraphUpdater(SceneGraphUpdater):
    """Scene graph updater backed by first-person RGB-D perception."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
        update_every: Optional[int] = None,
        sensor_name: Optional[str] = None,
    ):
        self.backend_name = backend_name or os.environ.get("ISBENCH_SCENE_GRAPH_BACKEND", "samjam_unigoal")

        '''
            若：
            scene_graph_step_interval = 100
            update_every = 5

            每 100 个 low-level step 调一次 update()
            每 5 次 update() 真正跑一次 perception
            所以大约每 100 * 5 = 500 个 low-level step 真正重新感知一次
        '''
        # self.update_every = update_every or _env_int("ISBENCH_SCENE_GRAPH_UPDATE_EVERY", 1) # 
        self.update_every = 1
        self.sensor_name = sensor_name or os.environ.get("ISBENCH_SCENE_GRAPH_SENSOR_NAME", "auto")
        
        self.env = None
        self.global_step_index = 0
        self.latest_result: Optional[PerceptionResult] = None
        self.snapshot = SceneGraphSnapshot(
            step_index=-1, primitive_name=None, raw_plan=None
        )
        self.perception_errors: List[Dict[str, Any]] = []
        self.held_object_name: Optional[str] = None
        self.manipulation_event_history: List[Dict[str, Any]] = []
        self._last_manipulation_key: Optional[tuple[str, int]] = None

        if self.backend_name.lower() in {
            "truth",
            "omnigibson_truth",
            "unigoal_memory",
            "disabled",
            "none",
        }:
            self.truth_updater: Optional[SceneGraphUpdater] = (
                UniGoalMemorySceneGraphUpdater()
            )
            self.backend = None
        else:
            self.truth_updater = None
            self.backend = build_perception_backend(
                self.backend_name, sensor_name=self.sensor_name
            )

    def reset(self, env: Any):
        self.env = env
        self.global_step_index = 0
        self.latest_result = None
        self.perception_errors.clear()
        self.held_object_name = None
        self.manipulation_event_history.clear()
        self._last_manipulation_key = None

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

        target = _target_from_raw_plan(
            context.raw_plan if context is not None else None
        )
        if target and hasattr(self.backend, "set_object_goal"):
            self.backend.set_object_goal(target)

        manipulation_event = self._manipulation_event_from_context(context)
        if manipulation_event is not None and hasattr(
            self.backend, "note_manipulation_event"
        ):
            self.backend.note_manipulation_event(manipulation_event)

        force = context is None
        should_update = (
            force
            or self.latest_result is None
            or self.global_step_index % self.update_every == 0
        )
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

    def mark_manipulated_nodes(self, node_uids: List[int]) -> None:
        if self.truth_updater is not None:
            return
        if self.backend is not None and hasattr(self.backend, "mark_manipulated_nodes"):
            self.backend.mark_manipulated_nodes(node_uids)

    def _manipulation_event_from_context(
        self,
        context: Optional[LowLevelStepContext],
    ) -> Optional[Dict[str, Any]]:
        if context is None:
            return None
        primitive, params = _parse_raw_action(context.raw_plan)
        if primitive is None:
            return None

        moved_object = None
        target_object = None
        relation = None
        if primitive == "GRASP" and params:
            moved_object = params[0]
            self.held_object_name = moved_object
            relation = "grasp"
        elif primitive in {"PLACE_ON_TOP", "PLACE_INSIDE"}:
            relation = "on" if primitive == "PLACE_ON_TOP" else "in"
            if len(params) >= 2:
                moved_object = params[0]
                target_object = params[1]
            elif len(params) == 1:
                moved_object = self.held_object_name
                target_object = params[0]
        elif primitive == "RELEASE":
            moved_object = self.held_object_name
            relation = "release"
            self.held_object_name = None
        else:
            return None

        key = (context.raw_plan, context.step_index)
        if key == self._last_manipulation_key:
            return None
        self._last_manipulation_key = key
        event = {
            "raw_plan": context.raw_plan,
            "primitive": primitive,
            "moved_object": moved_object,
            "target_object": target_object,
            "relation": relation,
            "global_step_index": self.global_step_index,
            "low_level_step_index": context.step_index,
            "source": "PerceptionSceneGraphUpdater.raw_plan",
        }
        self.manipulation_event_history.append(event)
        del self.manipulation_event_history[:-100]
        return event

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

        return self._snapshot_from_result(
            result, context=context, skipped=False, force=force
        )

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
                    "manipulation_event_history": list(self.manipulation_event_history),
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
            "manipulation_event_history": list(self.manipulation_event_history),
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
