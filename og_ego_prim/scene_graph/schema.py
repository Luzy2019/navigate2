import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


SCENE_GRAPH_SCHEMA_VERSION = "isbench.scene_graph.v2"


def _to_builtin(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _to_builtin(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_builtin(item) for item in value]
    return value


def _clean_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    keep_empty_lists = {
        "rooms",
        "groups",
        "nodes",
        "edges",
        "scene_graph_history",
        "error_stack",
        "execution_diagnostics",
    }
    cleaned = {}
    for key, value in data.items():
        if value is None:
            continue
        if value == {}:
            continue
        if value == [] and key not in keep_empty_lists:
            continue
        cleaned[key] = _to_builtin(value)
    return cleaned


def normalize_scene_graph_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace(".n.01", "")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "object"


def canonical_object_id(uid: Optional[Any], fallback_uid: Optional[int] = None) -> str:
    parsed_uid = parse_uid(uid)
    if parsed_uid is None:
        parsed_uid = parse_uid(fallback_uid)
    if parsed_uid is None:
        parsed_uid = 0
    return f"obj_{parsed_uid:04d}"


def parse_uid(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d+)$", text)
    if match is None:
        return None
    return int(match.group(1))


@dataclass
class SceneGraphNode:
    id: Optional[str] = None
    uid: Optional[int] = None
    name: str = "object"
    label: Optional[str] = None
    is_coarse: bool = True
    is_vis: bool = True
    position: Optional[List[float]] = None
    states: Dict[str, Any] = field(default_factory=dict)
    hazard: Dict[str, Any] = field(default_factory=dict)
    caption: Optional[str] = None
    last_seen_step: Optional[int] = None
    room: Optional[str] = None
    room_id: Optional[Any] = None
    group: Optional[str] = None
    role: Optional[str] = None

    # Legacy input aliases.  They are accepted so older updaters can keep
    # constructing nodes, but they are intentionally not emitted in v2 output.
    object_id: Optional[str] = None
    category: Optional[str] = None
    visible: Optional[bool] = None
    orientation: Optional[List[float]] = None
    attributes: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or self.name == "object":
            self.name = self.category or self.name or "object"
        self.name = normalize_scene_graph_name(self.name)
        if self.uid is None:
            self.uid = parse_uid(self.attributes.get("uid"))
        if self.uid is None and self.id:
            self.uid = parse_uid(self.id)
        if self.visible is not None:
            self.is_vis = bool(self.visible)
        if self.label is not None:
            self.label = normalize_scene_graph_name(self.label)

    def to_dict(self, fallback_uid: Optional[int] = None) -> Dict[str, Any]:
        uid = parse_uid(self.uid)
        if uid is None:
            uid = parse_uid(fallback_uid)
        node_id = self.id if self.id and self.id.startswith("obj_") else canonical_object_id(uid)
        label = self.label or f"{self.name}_01"
        return _clean_dict(
            {
                "id": node_id,
                "uid": uid,
                "name": self.name,
                "label": label,
                "is_coarse": bool(self.is_coarse),
                "is_vis": bool(self.is_vis),
                "position": self.position,
                "states": self.states,
                "hazard": self.hazard,
                "caption": self.caption,
                "last_seen_step": self.last_seen_step,
                "room": self.room,
                "room_id": self.room_id,
                "group": self.group,
                "role": self.role,
            }
        )


@dataclass
class SceneGraphEdge:
    source: Optional[str] = None
    target: Optional[str] = None
    type: Optional[str] = None
    source_uid: Optional[int] = None
    target_uid: Optional[int] = None

    # Legacy input aliases.  They are accepted but not emitted in v2 output.
    source_id: Optional[str] = None
    target_id: Optional[str] = None
    relation: Optional[str] = None
    source_backend: str = "perception"
    confidence: float = 1.0

    def to_dict(self, id_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        id_map = id_map or {}
        source = id_map.get(str(self.source_id), self.source_id) if self.source_id else self.source
        target = id_map.get(str(self.target_id), self.target_id) if self.target_id else self.target
        relation = self.type or self.relation or "related_to"
        return _clean_dict(
            {
                "source": source,
                "target": target,
                "source_uid": self.source_uid,
                "target_uid": self.target_uid,
                "type": relation,
            }
        )


@dataclass
class SceneGraphGroup:
    group_id: str
    group_name: str
    nodes: List[SceneGraphNode] = field(default_factory=list)
    edges: List[SceneGraphEdge] = field(default_factory=list)
    type: str = "group"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "type": self.type,
            "object_count": len(self.nodes),
            "nodes": [node.to_dict(index + 1) for index, node in enumerate(self.nodes)],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass
class SceneGraphRoom:
    room_id: str
    room_name: str
    groups: List[SceneGraphGroup] = field(default_factory=list)
    nodes: List[SceneGraphNode] = field(default_factory=list)
    edges: List[SceneGraphEdge] = field(default_factory=list)
    type: str = "room"

    def to_dict(self) -> Dict[str, Any]:
        nodes = self._node_dicts()
        edges = self._edge_dicts()
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "type": self.type,
            "object_count": len(nodes),
            "nodes": nodes,
            "edges": edges,
        }

    def _node_dicts(self) -> List[Dict[str, Any]]:
        node_dicts = []
        for node in self.nodes:
            node_dicts.append(node.to_dict(len(node_dicts) + 1))
        for group in self.groups:
            for node in group.nodes:
                node_dicts.append(node.to_dict(len(node_dicts) + 1))
        return _dedupe_node_payloads(node_dicts)

    def _edge_dicts(self) -> List[Dict[str, Any]]:
        edge_dicts = [edge.to_dict() for edge in self.edges]
        for group in self.groups:
            edge_dicts.extend(edge.to_dict() for edge in group.edges)
        return _dedupe_edge_payloads(edge_dicts)


@dataclass
class SceneGraphSnapshot:
    step_index: int
    primitive_name: Optional[str]
    raw_plan: Optional[str]
    rooms: List[SceneGraphRoom] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    # Legacy flat fields.  If an older updater fills these, to_dict() wraps them
    # into room_unknown so downstream code still receives v2.
    nodes: List[SceneGraphNode] = field(default_factory=list)
    edges: List[SceneGraphEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        rooms = self.rooms if self.rooms else self._rooms_from_flat_graph()
        return _clean_dict(
            {
                "rooms": [room.to_dict() for room in rooms],
                "schema_version": SCENE_GRAPH_SCHEMA_VERSION,
                "step_index": self.step_index,
                "primitive_name": self.primitive_name,
                "raw_plan": self.raw_plan,
                "summary": self.summary or self._summary_from_metadata(),
            }
        )

    def to_prompt_context(self) -> str:
        '''
            它主要给 planner / LLM 使用：
            scene graph 本身是结构化对象，但 LLM prompt 更适合读自然语言/文本，
            所以这个函数就是把 scene graph 快速压缩成 prompt context。

            v2 scene graph 已经按 rooms -> nodes/edges 组织；这里把它压平成
            简短文本，便于 prompt 直接引用。
        '''
        lines = []
        snapshot = self.to_dict()
        for room in snapshot.get("rooms", []):
            for node in room.get("nodes", []):
                state_text = ", ".join(
                    f"{key}={value}"
                    for key, value in sorted((node.get("states") or {}).items())
                )
                visible_text = "visible" if node.get("is_vis") else "not_visible"
                label = node.get("label") or node.get("name") or node.get("id")
                if state_text:
                    lines.append(f"- {label}: {visible_text}, {state_text}")
                else:
                    lines.append(f"- {label}: {visible_text}")
            for edge in room.get("edges", []):
                lines.append(
                    f"- {edge.get('source')} {edge.get('type')} {edge.get('target')}"
                )
        return "\n".join(lines)

    def _summary_from_metadata(self) -> Dict[str, Any]:
        return _clean_dict(
            {
                "backend": self.metadata.get("perception_backend"),
                "global_step_index": self.metadata.get("global_step_index"),
                "frame_index": self.metadata.get("frame_index"),
                "objects": self.metadata.get("object_count"),
                "rooms": len((self.metadata.get("room_graph") or {}).get("rooms", [])),
                "groups": len((self.metadata.get("group_graph") or {}).get("groups", [])),
                "relations": self.metadata.get("relation_count", len(self.edges)),
                "membership_edges": self.metadata.get("membership_edge_count", 0),
                "edges": self.metadata.get("total_edge_count", len(self.edges)),
                "skipped": self.metadata.get("perception_skipped"),
            }
        )

    def _rooms_from_flat_graph(self) -> List[SceneGraphRoom]:
        if not self.nodes:
            return []

        object_nodes = [
            node
            for node in self.nodes
            if normalize_scene_graph_name(node.category or node.name) not in {"room", "group"}
        ]
        id_map: Dict[str, str] = {}
        canonical_nodes: List[SceneGraphNode] = []
        for index, node in enumerate(object_nodes, start=1):
            uid = parse_uid(node.uid) or index
            node.uid = uid
            node.id = canonical_object_id(uid)
            node.room = node.room or "unknown_room"
            node.room_id = node.room_id or "room_unknown"
            node.group = node.group or "group_unknown"
            if node.object_id:
                id_map[str(node.object_id)] = node.id
            id_map[str(node.id)] = node.id
            canonical_nodes.append(node)
        for name in sorted({node.name for node in canonical_nodes}):
            same_name_nodes = [node for node in canonical_nodes if node.name == name]
            same_name_nodes.sort(key=lambda node: (node.uid is None, node.uid or 0, node.id or ""))
            for index, node in enumerate(same_name_nodes, start=1):
                node.label = node.label or f"{name}_{index:02d}"

        relation_edges = []
        for edge in self.edges:
            relation = edge.type or edge.relation
            if relation in {"in_room", "in_group"}:
                continue
            relation_edges.append(edge)

        return [
            SceneGraphRoom(
                room_id="room_unknown",
                room_name="unknown_room",
                nodes=canonical_nodes,
                edges=[
                    SceneGraphEdge(
                        source=id_map.get(str(edge.source_id), edge.source),
                        target=id_map.get(str(edge.target_id), edge.target),
                        source_uid=edge.source_uid,
                        target_uid=edge.target_uid,
                        type=edge.type or edge.relation,
                    )
                    for edge in relation_edges
                ],
            )
        ]


def scene_graph_report(
    *,
    task: str,
    scene: str,
    scene_graph_backend: str,
    scene_graph_step_interval: int,
    scene_graph_update_every: int,
    latest_summary: Dict[str, Any],
    latest_scene_graph: Optional[Dict[str, Any]],
    scene_graph_history: List[Dict[str, Any]],
    error_stack: List[Dict[str, Any]],
    execution_diagnostics: List[Dict[str, Any]],
    termination: Optional[Dict[str, Any]] = None,
    goal_condition: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    graph = _scene_graph_payload(latest_scene_graph)
    return _clean_dict(
        {
            "task": task,
            "scene": scene,
            "scene_graph_backend": scene_graph_backend,
            "scene_graph_step_interval": scene_graph_step_interval,
            "scene_graph_update_every": scene_graph_update_every,
            "latest_summary": latest_summary,
            "schema_version": SCENE_GRAPH_SCHEMA_VERSION,
            "latest_scene_graph": graph,
            "scene_graph_history": [
                _scene_graph_payload(snapshot) for snapshot in scene_graph_history
            ],
            "error_stack": error_stack,
            "execution_diagnostics": execution_diagnostics,
            "termination": termination,
            "goal_condition": goal_condition,
        }
    )


def dumps_scene_graph(data: Dict[str, Any], **kwargs: Any) -> str:
    return json.dumps(_to_builtin(data), **kwargs)


def _scene_graph_payload(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"rooms": []}
    rooms = [
        _room_payload(room)
        for room in snapshot.get("rooms", [])
        if isinstance(room, dict)
    ]
    return {"rooms": rooms}


def _room_payload(room: Dict[str, Any]) -> Dict[str, Any]:
    nodes = _dedupe_node_payloads(_room_node_payloads(room))
    edges = _dedupe_edge_payloads(_room_edge_payloads(room))
    return _clean_dict(
        {
            "room_id": room.get("room_id"),
            "room_name": room.get("room_name"),
            "type": room.get("type", "room"),
            "object_count": len(nodes),
            "nodes": nodes,
            "edges": edges,
        }
    )


def _room_node_payloads(room: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = [
        _to_builtin(node)
        for node in room.get("nodes", [])
        if isinstance(node, dict)
    ]
    for group in room.get("groups", []):
        if not isinstance(group, dict):
            continue
        nodes.extend(
            _to_builtin(node)
            for node in group.get("nodes", [])
            if isinstance(node, dict)
        )
    return nodes


def _room_edge_payloads(room: Dict[str, Any]) -> List[Dict[str, Any]]:
    edges = [
        _to_builtin(edge)
        for edge in room.get("edges", [])
        if isinstance(edge, dict)
    ]
    for group in room.get("groups", []):
        if not isinstance(group, dict):
            continue
        edges.extend(
            _to_builtin(edge)
            for edge in group.get("edges", [])
            if isinstance(edge, dict)
        )
    return edges


def _dedupe_node_payloads(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for index, node in enumerate(nodes):
        key = node.get("id") or node.get("uid") or node.get("label")
        if key is None:
            key = f"__node_{index}"
        key = str(key)
        if key in seen:
            continue
        seen.add(key)
        result.append(_to_builtin(node))
    return result


def _dedupe_edge_payloads(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for index, edge in enumerate(edges):
        key = (
            edge.get("source"),
            edge.get("target"),
            edge.get("type"),
            edge.get("source_uid"),
            edge.get("target_uid"),
        )
        if key == (None, None, None, None, None):
            key = (index,)
        if key in seen:
            continue
        seen.add(key)
        result.append(_to_builtin(edge))
    return result
