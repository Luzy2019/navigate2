"""Semantic state diffs over versioned scene-graph snapshots."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from og_ego_prim.domain import StateChange
from og_ego_prim.utils.serialization import ExtensionMap, as_versioned_dict, to_builtin


def _payload(snapshot: Any) -> Dict[str, Any]:
    if snapshot is None:
        return {}
    if hasattr(snapshot, "to_dict"):
        snapshot = snapshot.to_dict()
    if not isinstance(snapshot, Mapping):
        raise TypeError("scene graph snapshot must be a mapping or expose to_dict()")
    return deepcopy(to_builtin(snapshot))


def _step(snapshot: Mapping[str, Any]) -> int:
    summary = snapshot.get("summary") or {}
    return int(
        snapshot.get("step_index")
        or (summary.get("global_step_index") if isinstance(summary, Mapping) else 0)
        or 0
    )


def _mapping_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        return value.values()
    return value or ()


def _identity_keys(value: Any) -> Tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return ()
    normalized = re.sub(r"\.n\.\d+", "", raw.lower())
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return tuple(dict.fromkeys(item for item in (raw, raw.lower(), normalized) if item))


def _node_id(node: Mapping[str, Any]) -> Optional[str]:
    value = (
        node.get("entity_id")
        or node.get("id")
        or node.get("object_id")
        or node.get("label")
    )
    if value is None and node.get("uid") is not None:
        try:
            value = f"obj_{int(node['uid']):04d}"
        except (TypeError, ValueError):
            value = None
    return str(value).strip() if value is not None and str(value).strip() else None


def _node_identity_values(
    canonical: str,
    node: Mapping[str, Any],
    *,
    include_semantic: bool = True,
) -> Tuple[Any, ...]:
    uid = node.get("uid")
    uid_id = None
    if uid is not None:
        try:
            uid_id = f"obj_{int(uid):04d}"
        except (TypeError, ValueError):
            uid_id = None
    stable = (
        canonical,
        node.get("entity_id"),
        node.get("id"),
        node.get("object_id"),
        uid,
        uid_id,
    )
    if not include_semantic:
        return stable
    return stable + (node.get("label"), node.get("name"))


def _iter_room_nodes(room: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield from (
        node for node in _mapping_values(room.get("nodes")) if isinstance(node, Mapping)
    )
    for group in _mapping_values(room.get("groups")):
        if isinstance(group, Mapping):
            yield from (
                node
                for node in _mapping_values(group.get("nodes"))
                if isinstance(node, Mapping)
            )


def _nodes(snapshot: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for room in _mapping_values(snapshot.get("rooms")):
        if not isinstance(room, Mapping):
            continue
        room_id = room.get("room_id") or room.get("room_name")
        for node in _iter_room_nodes(room):
            entity_id = _node_id(node)
            if entity_id is None:
                continue
            payload = dict(node)
            payload["__room_id"] = room_id or node.get("room_id") or node.get("room")
            result[entity_id] = payload
    flat_nodes = snapshot.get("nodes") or snapshot.get("entities") or ()
    for node in _mapping_values(flat_nodes):
        if not isinstance(node, Mapping):
            continue
        entity_id = _node_id(node)
        if entity_id is None or entity_id in result:
            continue
        payload = dict(node)
        payload["__room_id"] = node.get("room_id") or node.get("room")
        result[entity_id] = payload
    return result


def _edge_endpoint_aliases(snapshot: Mapping[str, Any]) -> Dict[str, str]:
    """Map scene/backend node IDs to the task-stable entity ID when available."""

    aliases: Dict[str, str] = {}
    ambiguous: Set[str] = set()
    for canonical, node in _nodes(snapshot).items():
        for value in _node_identity_values(canonical, node):
            if value is None:
                continue
            for key in _identity_keys(value):
                existing = aliases.get(key)
                if existing is not None and existing != canonical:
                    ambiguous.add(key)
                    continue
                aliases[key] = canonical
    for key in ambiguous:
        aliases.pop(key, None)
    return aliases


def _iter_room_edges(room: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield from (
        edge for edge in _mapping_values(room.get("edges")) if isinstance(edge, Mapping)
    )
    for group in _mapping_values(room.get("groups")):
        if isinstance(group, Mapping):
            yield from (
                edge
                for edge in _mapping_values(group.get("edges"))
                if isinstance(edge, Mapping)
            )


def _edge_endpoint(edge: Mapping[str, Any], role: str) -> Optional[Any]:
    value = edge.get(role)
    if value is None:
        value = edge.get("subject" if role == "source" else "object")
    if value is None:
        value = edge.get(f"{role}_id")
    if value is None:
        value = edge.get(f"{role}_uid")
    if isinstance(value, Mapping):
        value = (
            value.get("entity_id")
            or value.get("id")
            or value.get("object_id")
            or value.get("uid")
            or value.get("label")
        )
    return value


def _resolve_edge_endpoint(value: Any, aliases: Mapping[str, str]) -> str:
    for key in _identity_keys(value):
        canonical = aliases.get(key)
        if canonical is not None:
            return canonical
    return str(value).strip()


def _edges(snapshot: Mapping[str, Any]) -> Set[Tuple[str, str, str]]:
    result: Set[Tuple[str, str, str]] = set()
    aliases = _edge_endpoint_aliases(snapshot)
    edge_groups: List[Iterable[Mapping[str, Any]]] = []
    for room in _mapping_values(snapshot.get("rooms")):
        if isinstance(room, Mapping):
            edge_groups.append(_iter_room_edges(room))
    flat_edges = snapshot.get("edges") or snapshot.get("relations") or ()
    edge_groups.append(
        edge for edge in _mapping_values(flat_edges) if isinstance(edge, Mapping)
    )
    for edges in edge_groups:
        for edge in edges:
            source = _edge_endpoint(edge, "source")
            target = _edge_endpoint(edge, "target")
            relation = edge.get("type") or edge.get("relation") or edge.get("predicate")
            if source is None or target is None or relation is None:
                continue
            source_id = _resolve_edge_endpoint(source, aliases)
            target_id = _resolve_edge_endpoint(target, aliases)
            relation_id = str(relation).strip()
            if not source_id or not target_id or not relation_id:
                continue
            result.add((source_id, relation_id, target_id))
    return result


def _reconcile_node_ids(
    previous_nodes: Mapping[str, Mapping[str, Any]],
    current_nodes: Mapping[str, Mapping[str, Any]],
) -> Dict[str, str]:
    """Map prior backend IDs onto current task-stable IDs using unambiguous aliases."""

    aliases: Dict[str, str] = {}
    ambiguous: Set[str] = set()
    for canonical, node in current_nodes.items():
        for value in _node_identity_values(canonical, node, include_semantic=False):
            for key in _identity_keys(value):
                existing = aliases.get(key)
                if existing is not None and existing != canonical:
                    ambiguous.add(key)
                else:
                    aliases[key] = canonical
    for key in ambiguous:
        aliases.pop(key, None)

    result: Dict[str, str] = {}
    claimed: Set[str] = set()
    for previous_id, node in previous_nodes.items():
        candidates = {
            aliases[key]
            for value in _node_identity_values(previous_id, node, include_semantic=False)
            for key in _identity_keys(value)
            if key in aliases
        }
        current_id = next(iter(candidates)) if len(candidates) == 1 else previous_id
        if current_id in claimed and current_id != previous_id:
            current_id = previous_id
        result[previous_id] = current_id
        claimed.add(current_id)
    return result


def _equal(left: Any, right: Any) -> bool:
    return to_builtin(left) == to_builtin(right)


def _semantic_states(node: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(node, Mapping):
        return {}
    values = node.get("states")
    if not isinstance(values, Mapping):
        values = node.get("attributes")
    if not isinstance(values, Mapping):
        return {}
    copied = deepcopy(dict(values))
    return {str(key): value for key, value in copied.items()}


@dataclass
class SceneGraphDiffResult:
    previous_step: Optional[int]
    current_step: int
    changes: Tuple[StateChange, ...] = ()
    schema_version: str = "isbench.scene_graph_diff.v1"
    extensions: ExtensionMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.previous_step = (
            None if self.previous_step is None else int(self.previous_step)
        )
        self.current_step = int(self.current_step)
        self.changes = tuple(
            value if isinstance(value, StateChange) else StateChange(**dict(value))
            for value in self.changes or ()
        )
        self.schema_version = str(self.schema_version).strip()
        self.extensions = deepcopy(dict(self.extensions or {}))

    def to_dict(self) -> Dict[str, Any]:
        return as_versioned_dict(self)


class SceneGraphDiffer:
    """Compare semantic state while treating missing observations as unknown."""

    def __init__(
        self,
        *,
        source: str = "perception",
        emit_new_entities: bool = True,
        infer_state_removals: bool = False,
        include_visibility: bool = True,
        include_relations: bool = True,
        infer_relation_removals: bool = False,
    ) -> None:
        self.source = str(source or "perception")
        self.emit_new_entities = bool(emit_new_entities)
        self.infer_state_removals = bool(infer_state_removals)
        self.include_visibility = bool(include_visibility)
        self.include_relations = bool(include_relations)
        self.infer_relation_removals = bool(infer_relation_removals)

    def compare(
        self,
        previous: Any,
        current: Any,
        *,
        subtask_id: Optional[str] = None,
    ) -> SceneGraphDiffResult:
        previous_payload = _payload(previous)
        current_payload = _payload(current)
        current_step = _step(current_payload)
        previous_nodes = _nodes(previous_payload)
        current_nodes = _nodes(current_payload)
        previous_to_current = _reconcile_node_ids(previous_nodes, current_nodes)
        if any(previous_id != current_id for previous_id, current_id in previous_to_current.items()):
            previous_nodes = {
                previous_to_current[previous_id]: node
                for previous_id, node in previous_nodes.items()
            }
        changes: List[StateChange] = []

        for entity_id, node in sorted(current_nodes.items()):
            old_node = previous_nodes.get(entity_id)
            room_id = node.get("__room_id")
            states = _semantic_states(node)
            old_states = _semantic_states(old_node)
            if old_node is None and not self.emit_new_entities:
                continue
            state_keys = set(states)
            if old_node is not None and self.infer_state_removals:
                state_keys.update(old_states)
            for key in sorted(state_keys, key=str):
                old_value = old_states.get(key)
                new_value = states.get(key)
                if not _equal(old_value, new_value):
                    changes.append(
                        StateChange(
                            step=current_step,
                            subtask_id=subtask_id,
                            entity_id=entity_id,
                            room_id=str(room_id) if room_id is not None else None,
                            key=str(key),
                            old=old_value,
                            new=new_value,
                            source=self.source,
                        )
                    )
            if old_node is not None:
                old_room = old_node.get("__room_id")
                if not _equal(old_room, room_id):
                    changes.append(
                        StateChange(
                            step=current_step,
                            subtask_id=subtask_id,
                            entity_id=entity_id,
                            room_id=str(room_id) if room_id is not None else None,
                            key="room_id",
                            old=old_room,
                            new=room_id,
                            source=self.source,
                        )
                    )
            if self.include_visibility:
                old_visibility = (
                    old_node.get("is_vis", old_node.get("visible"))
                    if old_node is not None
                    else None
                )
                new_visibility = node.get("is_vis", node.get("visible"))
                if new_visibility is not None and not _equal(old_visibility, new_visibility):
                    changes.append(
                        StateChange(
                            step=current_step,
                            subtask_id=subtask_id,
                            entity_id=entity_id,
                            room_id=str(room_id) if room_id is not None else None,
                            key="visible",
                            old=old_visibility,
                            new=bool(new_visibility),
                            source=self.source,
                        )
                    )

        if self.include_relations:
            previous_edges = {
                (
                    previous_to_current.get(source, source),
                    relation,
                    previous_to_current.get(target, target),
                )
                for source, relation, target in _edges(previous_payload)
            }
            current_edges = _edges(current_payload)
            for source, relation, target in sorted(current_edges - previous_edges):
                source_node = current_nodes.get(source, {})
                changes.append(
                    StateChange(
                        step=current_step,
                        subtask_id=subtask_id,
                        entity_id=source,
                        room_id=source_node.get("__room_id"),
                        key=f"relation:{relation}:{target}",
                        old=False,
                        new=True,
                        source=self.source,
                    )
                )
            if self.infer_relation_removals:
                current_ids = set(current_nodes)
                for source, relation, target in sorted(previous_edges - current_edges):
                    if source not in current_ids or target not in current_ids:
                        continue
                    source_node = current_nodes.get(source, {})
                    changes.append(
                        StateChange(
                            step=current_step,
                            subtask_id=subtask_id,
                            entity_id=source,
                            room_id=source_node.get("__room_id"),
                            key=f"relation:{relation}:{target}",
                            old=True,
                            new=False,
                            source=self.source,
                        )
                    )

        return SceneGraphDiffResult(
            previous_step=_step(previous_payload) if previous_payload else None,
            current_step=current_step,
            changes=tuple(changes),
        )

    def diff(
        self,
        previous: Any,
        current: Any,
        *,
        subtask_id: Optional[str] = None,
    ) -> Tuple[StateChange, ...]:
        return self.compare(previous, current, subtask_id=subtask_id).changes


class SceneGraphStateTracker:
    """Own only current/previous snapshots and delegate comparison policy."""

    def __init__(self, differ: Optional[SceneGraphDiffer] = None) -> None:
        self.differ = differ or SceneGraphDiffer()
        self.previous: Optional[Dict[str, Any]] = None
        self.current: Optional[Dict[str, Any]] = None

    def update(
        self,
        snapshot: Any,
        *,
        subtask_id: Optional[str] = None,
    ) -> Tuple[StateChange, ...]:
        next_snapshot = _payload(snapshot)
        self.previous = self.current
        self.current = next_snapshot
        if self.previous is None:
            return ()
        return self.differ.diff(self.previous, self.current, subtask_id=subtask_id)

    def reset(self) -> None:
        self.previous = None
        self.current = None


__all__ = [
    "SceneGraphDiffResult",
    "SceneGraphDiffer",
    "SceneGraphStateTracker",
]
