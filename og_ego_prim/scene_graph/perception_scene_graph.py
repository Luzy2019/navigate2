import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from og_ego_prim.primitives.executor import LowLevelStepContext
else:
    LowLevelStepContext = Any

from .backends import build_perception_backend
from .base import SceneGraphUpdater
from .perception import PerceptionResult
from .schema import (
    SceneGraphEdge,
    SceneGraphGroup,
    SceneGraphNode,
    SceneGraphRoom,
    SceneGraphSnapshot,
    canonical_object_id,
    normalize_scene_graph_name,
    parse_uid,
)
from .state_diff import SceneGraphDiffer, SceneGraphStateTracker
from og_ego_prim.config.runtime_config import SceneGraphConfig


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
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ):
        self.scene_graph_config = scene_graph_config or SceneGraphConfig()
        self.backend_name = backend_name or self.scene_graph_config.backend
        self.name = self.backend_name

        '''
            若：
            scene_graph_step_interval = 100
            update_every = 5

            每 100 个 low-level step 调一次 update()
            每 5 次 update() 真正跑一次 perception
            所以大约每 100 * 5 = 500 个 low-level step 真正重新感知一次
        '''
        self.update_every = int(update_every or self.scene_graph_config.update_every)
        if self.update_every <= 0:
            raise ValueError("scene_graph.update_every must be greater than zero")
        self.sensor_name = sensor_name or self.scene_graph_config.sensor_name
        
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
        self.state_tracker = SceneGraphStateTracker(
            SceneGraphDiffer(include_visibility=False, infer_relation_removals=False)
        )
        self.disabled = self.backend_name.lower() in {"disabled", "none"}

        # backend 暂时只有 samjam_unigoal
        if self.disabled:
            self.truth_updater = None
            self.backend = None
        elif self.backend_name.lower() in {
            "truth",
            "omnigibson_truth",
            "unigoal_memory",
        }:
            from .unigoal_memory_scene_graph import UniGoalMemorySceneGraphUpdater

            self.truth_updater: Optional[SceneGraphUpdater] = (
                UniGoalMemorySceneGraphUpdater()
            )
            self.backend = None
        else:
            self.truth_updater = None
            self.backend = build_perception_backend(
                self.backend_name,
                sensor_name=self.sensor_name,
                scene_graph_config=self.scene_graph_config,
            )

    def reset(self, env: Any):
        self.env = env
        self.global_step_index = 0
        self.latest_result = None
        self.perception_errors.clear()
        self.held_object_name = None
        self.manipulation_event_history.clear()
        self._last_manipulation_key = None
        self.state_tracker.reset()

        if self.disabled:
            self.snapshot = SceneGraphSnapshot(
                step_index=0,
                primitive_name=None,
                raw_plan=None,
                metadata={"perception_backend": "disabled", "perception_skipped": True},
            )
            return self.snapshot

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
        if self.disabled:
            self.global_step_index += 1
            self.snapshot = SceneGraphSnapshot(
                step_index=self.global_step_index,
                primitive_name=context.primitive_name if context is not None else None,
                raw_plan=context.raw_plan if context is not None else None,
                metadata={"perception_backend": "disabled", "perception_skipped": True},
            )
            return self.snapshot
        if self.truth_updater is not None:
            return self.truth_updater.update(context)

        target = _target_from_raw_plan(
            context.raw_plan if context is not None else None
        )
        if target and hasattr(self.backend, "set_object_goal"):
            self.backend.set_object_goal(target)

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

    def observe(self, context: Optional[LowLevelStepContext] = None) -> SceneGraphSnapshot:
        return self.update(context)

    def state_changes(
        self,
        snapshot: Any,
        *,
        subtask_id: Optional[str] = None,
    ):
        return self.state_tracker.update(snapshot, subtask_id=subtask_id)

    def get_snapshot(self) -> SceneGraphSnapshot:
        if self.disabled:
            return self.snapshot
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

    def note_manipulation_event(self, event: Dict[str, Any]) -> None:
        """Forward one confirmed action to perception's tracking backend."""
        if self.disabled or self.truth_updater is not None or self.backend is None:
            return
        if hasattr(self.backend, "note_manipulation_event"):
            self.backend.note_manipulation_event(dict(event))

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
        step_index = (
            self.global_step_index
            if context is None
            else getattr(context, "global_step_index", context.step_index)
        )
        if result is None:
            return SceneGraphSnapshot(
                step_index=step_index,
                primitive_name=primitive_name,
                raw_plan=raw_plan,
                summary={
                    "backend": self.backend_name,
                    "global_step_index": self.global_step_index,
                    "frame_index": None,
                    "objects": 0,
                    "rooms": 0,
                    "groups": 0,
                    "relations": 0,
                    "membership_edges": 0,
                    "edges": 0,
                    "skipped": skipped,
                    "ready": False,
                },
                metadata={
                    "source": "perception",
                    "ready": False,
                    "perception_backend": self.backend_name,
                    "global_step_index": self.global_step_index,
                    "perception_errors": self.perception_errors,
                },
            )

        rooms, relation_edge_count = self._canonical_rooms_from_result(result)

        object_count = len(
            {
                node.id
                for room in rooms
                for group in room.groups
                for node in group.nodes
                if node.id is not None
            }
        )
        summary = {
            "backend": result.backend,
            "global_step_index": self.global_step_index,
            "frame_index": result.frame_index,
            "objects": object_count,
            "rooms": len(rooms),
            "groups": sum(len(room.groups) for room in rooms),
            "relations": len(result.relations),
            "membership_edges": 0,
            "edges": relation_edge_count,
            "skipped": skipped,
            "ready": True,
            "perception_forced": force,
        }
        metadata = {
            "source": "perception",
            "ready": True,
            "perception_backend": result.backend,
            "global_step_index": self.global_step_index,
            "frame_index": result.frame_index,
            "perception_skipped": skipped,
            "perception_forced": force,
            "object_count": object_count,
            "relation_count": len(result.relations),
            "membership_edge_count": 0,
            "total_edge_count": relation_edge_count,
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
            rooms=rooms,
            summary=summary,
            metadata=metadata,
        )

    def _canonical_rooms_from_result(
        self,
        result: PerceptionResult,
    ) -> tuple[List[SceneGraphRoom], int]:
        raw_objects = list(result.objects)
        raw_to_uid = self._canonical_uids(raw_objects)
        raw_to_id = {
            str(obj.object_id): canonical_object_id(raw_to_uid[str(obj.object_id)])
            for obj in raw_objects
        }
        id_alias_to_raw = self._object_id_aliases(raw_objects, raw_to_uid, raw_to_id)

        room_lookup = self._room_lookup(result, id_alias_to_raw)
        group_specs = self._group_specs(result, raw_to_id, room_lookup, id_alias_to_raw)
        group_by_id = {spec["group_id"]: spec for spec in group_specs}
        object_to_group = {
            object_id: spec["group_id"]
            for spec in group_specs
            for object_id in spec["object_ids"]
        }

        room_names = []
        for obj in raw_objects:
            room_name = room_lookup.get(str(obj.object_id)) or self._object_room_name(obj)
            if room_name not in room_names:
                room_names.append(room_name)
        for spec in group_specs:
            if spec["room_name"] not in room_names:
                room_names.append(spec["room_name"])
        if not room_names:
            room_names = ["unknown_room"]
        room_ids = {
            room_name: ("room_unknown" if room_name == "unknown_room" else f"room_{index}")
            for index, room_name in enumerate(room_names)
        }

        canonical_nodes = []
        for obj in raw_objects:
            raw_id = str(obj.object_id)
            uid = raw_to_uid[raw_id]
            room_name = room_lookup.get(raw_id) or self._object_room_name(obj)
            group_id = object_to_group.get(raw_id)
            if group_id is None:
                group_id = f"group_unknown:{room_ids[room_name]}"
                if group_id not in group_by_id:
                    group_by_id[group_id] = {
                        "group_id": group_id,
                        "group_name": "unknown_group",
                        "room_name": room_name,
                        "object_ids": [],
                    }
                group_by_id[group_id]["object_ids"].append(raw_id)
                object_to_group[raw_id] = group_id

            node = self._canonical_node(
                obj=obj,
                uid=uid,
                canonical_id=raw_to_id[raw_id],
                room_name=room_name,
                room_id=room_ids[room_name],
                group_name=group_by_id[group_id]["group_name"],
            )
            canonical_nodes.append(node)

        self._assign_stable_labels(canonical_nodes)
        node_by_id = {node.id: node for node in canonical_nodes if node.id is not None}
        raw_to_node = {
            raw_id: node_by_id[canonical_id]
            for raw_id, canonical_id in raw_to_id.items()
            if canonical_id in node_by_id
        }

        canonical_edges = []
        seen_edges = set()
        for relation in result.relations:
            source_raw_id = id_alias_to_raw.get(str(relation.source_id), str(relation.source_id))
            target_raw_id = id_alias_to_raw.get(str(relation.target_id), str(relation.target_id))
            source_id = raw_to_id.get(source_raw_id)
            target_id = raw_to_id.get(target_raw_id)
            if source_id is None or target_id is None or source_id == target_id:
                continue
            source_node = node_by_id.get(source_id)
            target_node = node_by_id.get(target_id)
            relation_type = self._normalize_relation(relation.relation)
            edge_key = (source_id, target_id, relation_type)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            canonical_edges.append(
                SceneGraphEdge(
                    source=source_id,
                    target=target_id,
                    source_uid=None if source_node is None else source_node.uid,
                    target_uid=None if target_node is None else target_node.uid,
                    type=relation_type,
                )
            )

        canonical_id_to_group = {
            raw_to_id[raw_id]: group_id
            for raw_id, group_id in object_to_group.items()
            if raw_id in raw_to_id
        }
        group_edges_by_id: Dict[str, List[SceneGraphEdge]] = {}
        for edge in canonical_edges:
            group_id = canonical_id_to_group.get(str(edge.source))
            if group_id is None:
                group_id = canonical_id_to_group.get(str(edge.target))
            if group_id is not None:
                group_edges_by_id.setdefault(group_id, []).append(edge)

        rooms: List[SceneGraphRoom] = []
        edge_count = sum(len(edges) for edges in group_edges_by_id.values())
        room_to_groups: Dict[str, List[SceneGraphGroup]] = {room_name: [] for room_name in room_names}
        for group_id, spec in sorted(group_by_id.items(), key=lambda item: item[0]):
            group_nodes = []
            for raw_id in spec["object_ids"]:
                node = raw_to_node.get(raw_id)
                if node is not None:
                    group_nodes.append(node)
            group_nodes = sorted(group_nodes, key=lambda node: (node.uid is None, node.uid or 0, node.id or ""))
            group_edges = group_edges_by_id.get(group_id, [])
            room_to_groups.setdefault(spec["room_name"], []).append(
                SceneGraphGroup(
                    group_id=self._display_group_id(group_id),
                    group_name=str(spec["group_name"]),
                    nodes=group_nodes,
                    edges=group_edges,
                )
            )

        for room_name in room_names:
            groups = room_to_groups.get(room_name, [])
            if not groups:
                groups = [
                    SceneGraphGroup(
                        group_id="group_unknown",
                        group_name="unknown_group",
                        nodes=[],
                        edges=[],
                    )
                ]
            rooms.append(
                SceneGraphRoom(
                    room_id=room_ids[room_name],
                    room_name=str(room_name),
                    groups=groups,
                )
            )
        return rooms, edge_count

    def _object_name(self, obj: Any) -> str:
        attrs = obj.attributes or {}
        return normalize_scene_graph_name(
            attrs.get("normalized_label")
            or attrs.get("lifelong_label")
            or obj.name
            or obj.category
            or "object"
        )

    def _canonical_uids(self, objects: List[Any]) -> Dict[str, int]:
        used = set()
        next_uid = 1
        raw_to_uid: Dict[str, int] = {}
        for obj in objects:
            raw_id = str(obj.object_id)
            attrs = obj.attributes or {}
            candidate = parse_uid(attrs.get("uid"))
            if candidate is None and re.match(r"^(obj_|samjam_object:|unigoal_object:)\d+$", raw_id):
                candidate = parse_uid(raw_id)
            while next_uid in used:
                next_uid += 1
            if candidate is None or candidate in used:
                candidate = next_uid
                next_uid += 1
            used.add(candidate)
            raw_to_uid[raw_id] = candidate
        return raw_to_uid

    def _object_id_aliases(
        self,
        objects: List[Any],
        raw_to_uid: Dict[str, int],
        raw_to_id: Dict[str, str],
    ) -> Dict[str, str]:
        aliases = {}
        for obj in objects:
            raw_id = str(obj.object_id)
            uid = raw_to_uid[raw_id]
            aliases[raw_id] = raw_id
            aliases[raw_to_id[raw_id]] = raw_id
            aliases[str(uid)] = raw_id
            for source_id in (obj.attributes or {}).get("source_ids", {}).values():
                if source_id is not None:
                    aliases[str(source_id)] = raw_id
        return aliases

    def _room_lookup(
        self,
        result: PerceptionResult,
        id_alias_to_raw: Dict[str, str],
    ) -> Dict[str, str]:
        lookup: Dict[str, str] = {}
        for index, room in enumerate((result.room_graph or {}).get("rooms", [])):
            room_name = str(
                room.get("caption")
                or room.get("name")
                or room.get("id")
                or f"room_{index}"
            )
            for object_id in room.get("objects", []):
                raw_id = id_alias_to_raw.get(str(object_id))
                if raw_id is not None:
                    lookup[raw_id] = room_name
        return lookup

    def _group_specs(
        self,
        result: PerceptionResult,
        raw_to_id: Dict[str, str],
        room_lookup: Dict[str, str],
        id_alias_to_raw: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        specs = []
        for index, group in enumerate((result.group_graph or {}).get("groups", [])):
            object_ids = []
            seen_object_ids = set()
            for object_id in group.get("objects", []):
                raw_id = id_alias_to_raw.get(str(object_id))
                if raw_id is not None and raw_id in raw_to_id and raw_id not in seen_object_ids:
                    object_ids.append(raw_id)
                    seen_object_ids.add(raw_id)
            if not object_ids:
                continue
            room_name = str(group.get("room") or room_lookup.get(object_ids[0]) or "unknown_room")
            group_name = str(
                group.get("group_name")
                or group.get("name")
                or group.get("label")
                or f"group_{index}"
            )
            specs.append(
                {
                    "group_id": f"group_{index}",
                    "group_name": group_name,
                    "room_name": room_name,
                    "object_ids": object_ids,
                }
            )
        return specs

    def _canonical_node(
        self,
        obj: Any,
        uid: int,
        canonical_id: str,
        room_name: str,
        room_id: str,
        group_name: str,
    ) -> SceneGraphNode:
        attrs = obj.attributes or {}
        name = self._object_name(obj)
        states = dict(attrs.get("states") or {})
        hazard = dict(attrs.get("hazard") or {})
        caption = attrs.get("caption") or attrs.get("description") or attrs.get("vlm_raw_name")
        last_seen_step = attrs.get("last_seen_step")
        if last_seen_step is None:
            last_seen_step = attrs.get("last_seen_frame")
        if last_seen_step is None and bool(attrs.get("currently_visible", True)):
            last_seen_step = getattr(obj, "frame_index", None)
        is_vis = bool(attrs.get("is_vis", attrs.get("currently_visible", True)))
        is_coarse = bool(attrs.get("is_coarse", True))
        return SceneGraphNode(
            id=canonical_id,
            uid=uid,
            name=name,
            label=None,
            is_coarse=is_coarse,
            is_vis=is_vis,
            position=obj.position or attrs.get("position"),
            states=states,
            hazard=hazard,
            caption=caption,
            last_seen_step=last_seen_step,
            room=room_name,
            room_id=room_id,
            group=group_name,
            role=attrs.get("role"),
        )

    def _assign_stable_labels(self, nodes: List[SceneGraphNode]) -> None:
        grouped: Dict[str, List[SceneGraphNode]] = {}
        for node in nodes:
            grouped.setdefault(node.name, []).append(node)
        for name, same_name_nodes in grouped.items():
            same_name_nodes.sort(key=lambda node: (node.uid is None, node.uid or 0, node.id or ""))
            for index, node in enumerate(same_name_nodes, start=1):
                node.label = f"{name}_{index:02d}"

    def _object_room_name(self, obj: Any) -> str:
        attrs = obj.attributes or {}
        room = attrs.get("room") or obj.room_id or "unknown_room"
        return str(room or "unknown_room")

    def _display_group_id(self, group_id: str) -> str:
        if group_id.startswith("group_unknown:"):
            return "group_unknown"
        return group_id

    def _normalize_relation(self, relation: Any) -> str:
        text = str(relation or "related_to").strip().lower()
        text = re.sub(r"[_\-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or "related_to"

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
