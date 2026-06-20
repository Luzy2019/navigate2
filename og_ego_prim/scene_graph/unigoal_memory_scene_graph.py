from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Optional, Set, Tuple

from omnigibson.envs import Environment

from og_ego_prim.primitives.executor import LowLevelStepContext

from .base import SceneGraphUpdater
from .omnigibson_scene_graph import OmniGibsonSceneGraphUpdater
from .schema import SceneGraphEdge, SceneGraphNode, SceneGraphSnapshot


def _as_float_list(value):
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def _xy_distance(pos_a, pos_b) -> Optional[float]:
    if pos_a is None or pos_b is None:
        return None
    return math.dist(pos_a[:2], pos_b[:2])


def _room_caption(room_id: str) -> str:
    return room_id.replace("_", " ")


@dataclass
class MemoryObject:
    object_id: str
    name: str
    category: str
    first_seen_step: int
    last_seen_step: int
    seen_count: int = 0
    position: Optional[List[float]] = None
    orientation: Optional[List[float]] = None
    room_id: str = "unknown_room"
    states: Dict[str, Any] = field(default_factory=dict)
    currently_observed: bool = False
    distance_to_robot: Optional[float] = None

    @property
    def caption(self) -> str:
        return self.category or self.name


@dataclass
class MemoryRoom:
    room_id: str
    object_ids: Set[str] = field(default_factory=set)
    group_ids: Set[str] = field(default_factory=set)


@dataclass
class MemoryGroup:
    group_id: str
    room_id: str
    object_ids: List[str]
    center: Optional[List[float]]
    center_object_id: Optional[str]
    caption: str


class UniGoalMemorySceneGraphUpdater(SceneGraphUpdater):
    """UniGoal-style persistent scene graph memory backed by OmniGibson truth.

    UniGoal's original Graph class combines perception, 3D mapping, object
    merging, room assignment, group construction, and goal reasoning. This
    updater ports the memory shape and incremental graph behavior while keeping
    perception pluggable: the first IS-Bench backend uses OmniGibson object
    state / pose / room metadata instead of GroundingDINO + SAM.
    """

    def __init__(
        self,
        observation_radius: float = 3.0,
        group_distance_threshold: float = 1.5,
        map_cell_size: float = 0.25,
        trajectory_limit: int = 500,
    ):
        self.env: Optional[Environment] = None
        self.truth_updater = OmniGibsonSceneGraphUpdater()
        self.snapshot = SceneGraphSnapshot(
            step_index=-1,
            primitive_name=None,
            raw_plan=None,
        )
        self.global_step_index = 0
        self.observation_radius = observation_radius
        self.group_distance_threshold = group_distance_threshold
        self.map_cell_size = map_cell_size
        self.trajectory_limit = trajectory_limit
        self.objects: Dict[str, MemoryObject] = {}
        self.rooms: Dict[str, MemoryRoom] = {}
        self.groups: Dict[str, MemoryGroup] = {}
        self.visited_cells: Set[Tuple[int, int]] = set()
        self.trajectory: List[List[float]] = []

    def reset(self, env: Environment):
        self.env = env
        self.truth_updater.reset(env)
        self.global_step_index = 0
        self.objects.clear()
        self.rooms.clear()
        self.groups.clear()
        self.visited_cells.clear()
        self.trajectory.clear()
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
                metadata={"source": "unigoal_memory", "ready": False},
            )

        truth_snapshot = self.truth_updater.update(context)
        robot_position = self._get_robot_position()
        self._update_map_memory(robot_position)
        observed_ids = self._update_object_memory(truth_snapshot, robot_position)
        self._rebuild_rooms()
        self._rebuild_groups()

        nodes = self._build_memory_nodes(observed_ids)
        edges = self._build_memory_edges(truth_snapshot)

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
                "source": "unigoal_memory",
                "perception_backend": "omnigibson_truth",
                "global_step_index": self.global_step_index,
                "observed_object_ids": sorted(observed_ids),
                "memory_object_count": len(self.objects),
                "room_count": len(self.rooms),
                "group_count": len(self.groups),
                "rooms": [self._serialize_room(room) for room in self.rooms.values()],
                "groups": [self._serialize_group(group) for group in self.groups.values()],
                "map": self._serialize_map(robot_position),
                "truth_source": truth_snapshot.metadata,
            },
        )

    def _get_robot_position(self) -> Optional[List[float]]:
        if self.env is None or not getattr(self.env, "robots", None):
            return None

        try:
            position, _ = self.env.robots[0].get_position_orientation()
        except Exception:
            return None

        return _as_float_list(position)

    def _update_map_memory(self, robot_position: Optional[List[float]]):
        if robot_position is None:
            return

        self.trajectory.append(robot_position)
        if len(self.trajectory) > self.trajectory_limit:
            self.trajectory = self.trajectory[-self.trajectory_limit:]

        cell = (
            int(round(robot_position[0] / self.map_cell_size)),
            int(round(robot_position[1] / self.map_cell_size)),
        )
        self.visited_cells.add(cell)

    def _update_object_memory(
        self,
        truth_snapshot: SceneGraphSnapshot,
        robot_position: Optional[List[float]],
    ) -> Set[str]:
        for memory_obj in self.objects.values():
            memory_obj.currently_observed = False
            memory_obj.distance_to_robot = None

        observed_ids = set()
        for truth_node in truth_snapshot.nodes:
            position = _as_float_list(truth_node.position)
            distance_to_robot = _xy_distance(robot_position, position)
            is_observed = truth_node.visible and (
                distance_to_robot is None or distance_to_robot <= self.observation_radius
            )
            if not is_observed:
                continue

            room_id = self._get_room_id(truth_node.name)
            memory_obj = self.objects.get(truth_node.object_id)
            if memory_obj is None:
                memory_obj = MemoryObject(
                    object_id=truth_node.object_id,
                    name=truth_node.name,
                    category=truth_node.category,
                    first_seen_step=self.global_step_index,
                    last_seen_step=self.global_step_index,
                )
                self.objects[truth_node.object_id] = memory_obj

            memory_obj.name = truth_node.name
            memory_obj.category = truth_node.category
            memory_obj.last_seen_step = self.global_step_index
            memory_obj.seen_count += 1
            memory_obj.position = position
            memory_obj.orientation = _as_float_list(truth_node.orientation)
            memory_obj.room_id = room_id
            memory_obj.states = dict(truth_node.states)
            memory_obj.currently_observed = True
            memory_obj.distance_to_robot = distance_to_robot
            observed_ids.add(truth_node.object_id)

        return observed_ids

    def _get_room_id(self, object_name: str) -> str:
        if self.env is None:
            return "unknown_room"

        try:
            obj = self.env.scene.object_registry("name", object_name, None)
        except Exception:
            obj = None

        rooms = getattr(obj, "in_rooms", None)
        if isinstance(rooms, str):
            rooms = [rooms]
        if rooms:
            rooms = [room for room in rooms if room]
        if rooms:
            return str(rooms[0])
        return "unknown_room"

    def _rebuild_rooms(self):
        rooms: Dict[str, MemoryRoom] = {}
        for memory_obj in self.objects.values():
            room = rooms.setdefault(memory_obj.room_id, MemoryRoom(room_id=memory_obj.room_id))
            room.object_ids.add(memory_obj.object_id)
        self.rooms = rooms

    def _rebuild_groups(self):
        groups = {}
        for room in self.rooms.values():
            object_ids = sorted(room.object_ids)
            components = self._cluster_room_objects(object_ids)
            for index, component in enumerate(components):
                group_id = f"group:{room.room_id}:{index}"
                center = self._compute_center(component)
                center_object_id = self._nearest_object_id(component, center)
                group = MemoryGroup(
                    group_id=group_id,
                    room_id=room.room_id,
                    object_ids=component,
                    center=center,
                    center_object_id=center_object_id,
                    caption=self._group_caption(component),
                )
                groups[group_id] = group
                room.group_ids.add(group_id)
        self.groups = groups

    def _cluster_room_objects(self, object_ids: List[str]) -> List[List[str]]:
        remaining = set(object_ids)
        components = []
        while remaining:
            seed = remaining.pop()
            component = [seed]
            queue = [seed]
            while queue:
                current_id = queue.pop()
                current_pos = self.objects[current_id].position
                for candidate_id in list(remaining):
                    candidate_pos = self.objects[candidate_id].position
                    distance = _xy_distance(current_pos, candidate_pos)
                    if distance is not None and distance <= self.group_distance_threshold:
                        remaining.remove(candidate_id)
                        component.append(candidate_id)
                        queue.append(candidate_id)
            components.append(sorted(component))
        return sorted(components, key=lambda ids: ids[0])

    def _compute_center(self, object_ids: List[str]) -> Optional[List[float]]:
        positions = [self.objects[object_id].position for object_id in object_ids]
        positions = [position for position in positions if position is not None]
        if not positions:
            return None

        dims = len(positions[0])
        return [
            sum(position[index] for position in positions) / len(positions)
            for index in range(dims)
        ]

    def _nearest_object_id(
        self,
        object_ids: List[str],
        center: Optional[List[float]],
    ) -> Optional[str]:
        if center is None:
            return object_ids[0] if object_ids else None

        nearest_id = None
        nearest_distance = float("inf")
        for object_id in object_ids:
            distance = _xy_distance(center, self.objects[object_id].position)
            if distance is not None and distance < nearest_distance:
                nearest_id = object_id
                nearest_distance = distance
        return nearest_id

    def _group_caption(self, object_ids: List[str]) -> str:
        captions = [self.objects[object_id].caption for object_id in object_ids]
        return "Nodes: {}.".format(", ".join(captions))

    def _build_memory_nodes(self, observed_ids: Set[str]) -> List[SceneGraphNode]:
        nodes = []
        for memory_obj in sorted(self.objects.values(), key=lambda obj: obj.object_id):
            states = dict(memory_obj.states)
            states.update(
                {
                    "room_id": memory_obj.room_id,
                    "first_seen_step": memory_obj.first_seen_step,
                    "last_seen_step": memory_obj.last_seen_step,
                    "seen_count": memory_obj.seen_count,
                    "currently_observed": memory_obj.object_id in observed_ids,
                    "distance_to_robot": memory_obj.distance_to_robot,
                }
            )
            nodes.append(
                SceneGraphNode(
                    object_id=memory_obj.object_id,
                    name=memory_obj.name,
                    category=memory_obj.category,
                    visible=memory_obj.object_id in observed_ids,
                    position=memory_obj.position,
                    orientation=memory_obj.orientation,
                    states=states,
                )
            )

        for room in sorted(self.rooms.values(), key=lambda item: item.room_id):
            nodes.append(
                SceneGraphNode(
                    object_id=f"room:{room.room_id}",
                    name=_room_caption(room.room_id),
                    category="room",
                    visible=True,
                    position=None,
                    orientation=None,
                    states={
                        "object_ids": sorted(room.object_ids),
                        "group_ids": sorted(room.group_ids),
                        "exploration_level": len(room.object_ids),
                    },
                )
            )

        for group in sorted(self.groups.values(), key=lambda item: item.group_id):
            nodes.append(
                SceneGraphNode(
                    object_id=group.group_id,
                    name=group.caption,
                    category="group",
                    visible=True,
                    position=group.center,
                    orientation=None,
                    states={
                        "room_id": group.room_id,
                        "object_ids": group.object_ids,
                        "center_object_id": group.center_object_id,
                    },
                )
            )

        return nodes

    def _build_memory_edges(self, truth_snapshot: SceneGraphSnapshot) -> List[SceneGraphEdge]:
        edges = []
        memory_ids = set(self.objects)

        for truth_edge in truth_snapshot.edges:
            if truth_edge.source_id in memory_ids and truth_edge.target_id in memory_ids:
                edges.append(
                    SceneGraphEdge(
                        source_id=truth_edge.source_id,
                        target_id=truth_edge.target_id,
                        relation=truth_edge.relation,
                        source=truth_edge.source,
                        confidence=truth_edge.confidence,
                    )
                )

        for memory_obj in self.objects.values():
            edges.append(
                SceneGraphEdge(
                    source_id=memory_obj.object_id,
                    target_id=f"room:{memory_obj.room_id}",
                    relation="in_room",
                    source="omnigibson_room_metadata",
                    confidence=1.0,
                )
            )

        for group in self.groups.values():
            for object_id in group.object_ids:
                edges.append(
                    SceneGraphEdge(
                        source_id=object_id,
                        target_id=group.group_id,
                        relation="in_group",
                        source="unigoal_memory_grouping",
                        confidence=1.0,
                    )
                )
            edges.extend(self._build_near_edges(group.object_ids))

        return self._dedupe_edges(edges)

    def _build_near_edges(self, object_ids: List[str]) -> List[SceneGraphEdge]:
        edges = []
        for i, source_id in enumerate(object_ids):
            for target_id in object_ids[i + 1:]:
                distance = _xy_distance(
                    self.objects[source_id].position,
                    self.objects[target_id].position,
                )
                if distance is None or distance > self.group_distance_threshold:
                    continue
                edges.append(
                    SceneGraphEdge(
                        source_id=source_id,
                        target_id=target_id,
                        relation="near",
                        source="unigoal_memory_grouping",
                        confidence=max(0.1, 1.0 - distance / self.group_distance_threshold),
                    )
                )
        return edges

    def _dedupe_edges(self, edges: List[SceneGraphEdge]) -> List[SceneGraphEdge]:
        deduped = {}
        for edge in edges:
            key = (edge.source_id, edge.target_id, edge.relation)
            if key not in deduped or edge.confidence > deduped[key].confidence:
                deduped[key] = edge
        return sorted(deduped.values(), key=lambda edge: (edge.source_id, edge.relation, edge.target_id))

    def _serialize_room(self, room: MemoryRoom) -> Dict[str, Any]:
        return {
            "id": room.room_id,
            "caption": _room_caption(room.room_id),
            "object_ids": sorted(room.object_ids),
            "group_ids": sorted(room.group_ids),
            "exploration_level": len(room.object_ids),
        }

    def _serialize_group(self, group: MemoryGroup) -> Dict[str, Any]:
        return {
            "id": group.group_id,
            "room_id": group.room_id,
            "caption": group.caption,
            "center": group.center,
            "center_object_id": group.center_object_id,
            "object_ids": group.object_ids,
        }

    def _serialize_map(self, robot_position: Optional[List[float]]) -> Dict[str, Any]:
        return {
            "robot_position": robot_position,
            "visited_cell_count": len(self.visited_cells),
            "trajectory_length": len(self.trajectory),
            "trajectory_tail": self.trajectory[-10:],
            "map_cell_size": self.map_cell_size,
        }
