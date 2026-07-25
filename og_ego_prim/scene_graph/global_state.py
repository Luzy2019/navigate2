"""Accumulate canonical current-frame snapshots into remembered global state."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .schema import SceneGraphEdge, SceneGraphNode, SceneGraphRoom, SceneGraphSnapshot


_RELATIONS_REPLACED_BY_MOVE = frozenset({"on", "in", "inside", "held_by"})


def _snapshot_payload(snapshot: Any) -> Mapping[str, Any]:
    payload = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    if not isinstance(payload, Mapping):
        raise TypeError("scene graph snapshot must be a mapping or expose to_dict()")
    return payload


def _annotation_mode(snapshot: Any) -> str:
    metadata = getattr(snapshot, "metadata", None)
    if not isinstance(metadata, Mapping):
        return "complete_current_frame"
    backend_metadata = metadata.get("backend_metadata")
    if not isinstance(backend_metadata, Mapping):
        return "complete_current_frame"
    return str(
        backend_metadata.get("manual_annotation_mode") or "complete_current_frame"
    ).strip().lower()


def _iter_room_nodes(payload: Mapping[str, Any]) -> Iterable[Tuple[Mapping[str, Any], Mapping[str, Any]]]:
    for room in payload.get("rooms", ()) or ():
        if not isinstance(room, Mapping):
            continue
        for node in room.get("nodes", ()) or ():
            if isinstance(node, Mapping):
                yield room, node
        for group in room.get("groups", ()) or ():
            if not isinstance(group, Mapping):
                continue
            for node in group.get("nodes", ()) or ():
                if isinstance(node, Mapping):
                    yield room, node


def _iter_room_edges(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for room in payload.get("rooms", ()) or ():
        if not isinstance(room, Mapping):
            continue
        for edge in room.get("edges", ()) or ():
            if isinstance(edge, Mapping):
                yield edge
        for group in room.get("groups", ()) or ():
            if not isinstance(group, Mapping):
                continue
            for edge in group.get("edges", ()) or ():
                if isinstance(edge, Mapping):
                    yield edge


def _identity_key(node: Mapping[str, Any]) -> str:
    entity_id = str(node.get("entity_id") or "").strip()
    if entity_id:
        return f"entity:{entity_id}"
    source_object_id = str(node.get("source_object_id") or "").strip()
    if source_object_id:
        return f"source:{source_object_id}"
    raise ValueError(
        "global scene graph merge requires entity_id or source_object_id for every node"
    )


def _edge_key(source: str, relation: str, target: str) -> Tuple[str, str, str]:
    return (str(source), str(relation).strip().lower(), str(target))


class GlobalSceneGraphAccumulator:
    """Remember every confirmed object while retaining current-frame visibility."""

    def __init__(self) -> None:
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._global_id_by_identity: Dict[str, str] = {}
        self._room_id_by_name: Dict[str, str] = {}
        self._next_uid = 1
        self._frame_index = -1
        self._action_history: list[str] = []
        self._last_action: Optional[str] = None

    def merge_current_frame(self, snapshot: Any) -> SceneGraphSnapshot:
        """Merge one complete manual frame without treating absent nodes as deleted."""

        payload = _snapshot_payload(snapshot)
        summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
        frame_index = int(summary.get("frame_index", payload.get("step_index", 0)) or 0)
        annotation_mode = _annotation_mode(snapshot)
        frame_is_empty_update = annotation_mode == "no_new_nodes"
        if not frame_is_empty_update:
            for node in self._nodes.values():
                node["is_vis"] = False

        local_to_global: Dict[str, str] = {}
        for room, raw_node in _iter_room_nodes(payload):
            node = deepcopy(dict(raw_node))
            identity = _identity_key(node)
            global_id = self._global_id_by_identity.get(identity)
            if global_id is None:
                global_id = f"obj_{self._next_uid:04d}"
                self._next_uid += 1
                self._global_id_by_identity[identity] = global_id
            local_id = str(node.get("id") or "").strip()
            if local_id:
                local_to_global[local_id] = global_id

            room_name = str(
                node.get("room") or room.get("room_name") or room.get("room_id") or "unknown_room"
            )
            room_id = self._room_id_by_name.setdefault(
                room_name, f"room_{len(self._room_id_by_name)}"
            )
            previous = self._nodes.get(global_id, {})
            merged = dict(previous)
            merged.update(node)
            merged["id"] = global_id
            merged["uid"] = int(global_id.rsplit("_", 1)[-1])
            merged["room"] = room_name
            merged["room_id"] = room_id
            merged["is_vis"] = bool(node.get("is_vis", True))
            merged["last_seen_step"] = int(node.get("last_seen_step", frame_index) or frame_index)
            merged["states"] = {
                **dict(previous.get("states") or {}),
                **dict(node.get("states") or {}),
            }
            merged["hazard"] = {
                **dict(previous.get("hazard") or {}),
                **dict(node.get("hazard") or {}),
            }
            self._nodes[global_id] = merged

        for raw_edge in _iter_room_edges(payload):
            source = local_to_global.get(str(raw_edge.get("source") or "").strip())
            target = local_to_global.get(str(raw_edge.get("target") or "").strip())
            relation = str(raw_edge.get("type") or raw_edge.get("relation") or "").strip()
            if source is None or target is None or source == target or not relation:
                continue
            edge = {
                "source": source,
                "target": target,
                "source_uid": self._nodes[source]["uid"],
                "target_uid": self._nodes[target]["uid"],
                "type": relation,
            }
            self._edges[_edge_key(source, relation, target)] = edge

        self._frame_index = frame_index
        return self.snapshot()

    def apply_successful_action(self, action: Any) -> SceneGraphSnapshot:
        """Project deterministic action effects onto the remembered graph state."""

        action_name = str(getattr(action, "name", "")).strip().upper()
        action_text = (
            action.to_legacy_plan()
            if callable(getattr(action, "to_legacy_plan", None))
            else str(action)
        )
        object_id = self._resolve_action_entity(getattr(action, "object_id", None))
        target_id = self._resolve_action_entity(getattr(action, "target_id", None))

        if object_id is not None:
            states = self._nodes[object_id].setdefault("states", {})
            if action_name == "OPEN":
                states["open"] = True
            elif action_name == "CLOSE":
                states["open"] = False
            elif action_name == "TOGGLE_ON":
                states["toggled_on"] = True
            elif action_name == "TOGGLE_OFF":
                states["toggled_on"] = False
            elif action_name == "GRASP":
                states["held_by_robot"] = True
                self._remove_move_relations(object_id)
            elif action_name in {"RELEASE", "PLACE_ON_TOP", "PLACE_INSIDE"}:
                states["held_by_robot"] = False

        relation = {
            "PLACE_ON_TOP": "on",
            "PLACE_INSIDE": "in",
        }.get(action_name)
        if relation and object_id is not None and target_id is not None:
            self._remove_move_relations(object_id)
            self._edges[_edge_key(object_id, relation, target_id)] = {
                "source": object_id,
                "target": target_id,
                "source_uid": self._nodes[object_id]["uid"],
                "target_uid": self._nodes[target_id]["uid"],
                "type": relation,
            }

        self._last_action = action_text
        self._action_history.append(action_text)
        return self.snapshot()

    def snapshot(self) -> SceneGraphSnapshot:
        rooms: Dict[str, SceneGraphRoom] = {}
        for node_id, node_payload in self._nodes.items():
            room_name = str(node_payload.get("room") or "unknown_room")
            room_id = str(node_payload.get("room_id") or "room_unknown")
            room = rooms.setdefault(
                room_name,
                SceneGraphRoom(room_id=room_id, room_name=room_name),
            )
            room.nodes.append(SceneGraphNode(**deepcopy(node_payload)))

        for edge_payload in self._edges.values():
            source = str(edge_payload["source"])
            source_node = self._nodes.get(source)
            room_name = str((source_node or {}).get("room") or "unknown_room")
            room_id = str((source_node or {}).get("room_id") or "room_unknown")
            room = rooms.setdefault(
                room_name,
                SceneGraphRoom(room_id=room_id, room_name=room_name),
            )
            room.edges.append(SceneGraphEdge(**deepcopy(edge_payload)))

        ordered_rooms = [rooms[name] for name in self._room_id_by_name if name in rooms]
        if "unknown_room" in rooms and "unknown_room" not in self._room_id_by_name:
            ordered_rooms.append(rooms["unknown_room"])
        return SceneGraphSnapshot(
            step_index=max(self._frame_index, 0),
            primitive_name=None,
            raw_plan=self._last_action,
            rooms=ordered_rooms,
            summary={
                "backend": "manual_current_frame_global_state",
                "frame_index": self._frame_index,
                "objects": len(self._nodes),
                "rooms": len(ordered_rooms),
                "groups": 0,
                "relations": len(self._edges),
                "membership_edges": 0,
                "edges": len(self._edges),
                "ready": True,
                "scope": "current_global_state",
                "last_action": self._last_action,
                "action_history": list(self._action_history),
            },
        )

    def _resolve_action_entity(self, value: Any) -> Optional[str]:
        raw = str(value or "").strip()
        if not raw:
            return None
        for node_id, node in self._nodes.items():
            if raw in {
                str(node.get("entity_id") or "").strip(),
                str(node.get("source_object_id") or "").strip(),
                str(node.get("id") or "").strip(),
            }:
                return node_id
        return None

    def _remove_move_relations(self, source: str) -> None:
        for key in tuple(self._edges):
            edge = self._edges[key]
            if edge["source"] != source:
                continue
            if str(edge["type"]).strip().lower() in _RELATIONS_REPLACED_BY_MOVE:
                self._edges.pop(key)


__all__ = ["GlobalSceneGraphAccumulator"]
