"""Replaceable prompt section renderers."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Protocol, Sequence, Set, Tuple

from og_ego_prim.domain import Registry

from .models import PromptContext


def _as_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return _as_dict(value.to_dict())
    if isinstance(value, Mapping):
        return {
            str(key): _as_dict(item)
            for key, item in value.items()
            if str(key).strip().lower() not in {"extensions", "schema_version"}
        }
    if isinstance(value, (list, tuple)):
        return [_as_dict(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_as_dict(item) for item in sorted(value, key=str)]
    return value


def _json(value: Any) -> str:
    return json.dumps(_as_dict(value), ensure_ascii=False, sort_keys=True, default=str)


def _identity_keys(value: Any) -> Set[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return set()
    normalized = re.sub(r"\.n\.\d+", "", raw)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return {item for item in (raw, normalized) if item}


def _mapping_view(value: Any, allowed: Sequence[str]) -> Dict[str, Any]:
    payload = _as_dict(value)
    if not isinstance(payload, Mapping):
        return {}
    return {
        key: payload[key]
        for key in allowed
        if key in payload and payload[key] is not None
    }


def _mapping_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        return value.values()
    return value or ()


def _edge_endpoint(edge: Mapping[str, Any], role: str) -> Any:
    value = edge.get(role)
    if value is None:
        value = edge.get("subject" if role == "source" else "object")
    if value is None:
        value = edge.get(f"{role}_id")
    if value is None:
        value = edge.get(f"{role}_uid")
    if isinstance(value, Mapping):
        return (
            value.get("task_object_id")
            or value.get("entity_id")
            or value.get("id")
            or value.get("object_id")
            or value.get("uid")
            or value.get("label")
        )
    return value


class PromptSectionProvider(Protocol):
    name: str

    def render(self, context: PromptContext) -> str:
        ...


class BaseSection:
    name = "base"
    title = "Context"

    def content(self, context: PromptContext) -> Any:
        raise NotImplementedError

    def render(self, context: PromptContext) -> str:
        value = self.content(context)
        if value is None or value == () or value == [] or value == {} or value == "":
            return ""
        return f"## {self.title}\n{value if isinstance(value, str) else _json(value)}"


class SceneSection(BaseSection):
    name = "scene"
    title = "Current Relevant Scene"

    def __init__(self, max_items: int = 20) -> None:
        self.max_items = max(int(max_items), 1)

    @staticmethod
    def _node_view(node: Mapping[str, Any]) -> Dict[str, Any]:
        allowed = (
            "id",
            "task_object_id",
            "entity_id",
            "object_id",
            "label",
            "name",
            "role",
            "is_vis",
            "states",
            "hazard",
            "room",
            "room_id",
            "group",
            "aliases",
        )
        return {
            key: _as_dict(node[key])
            for key in allowed
            if key in node and node[key] is not None
        }

    @staticmethod
    def _node_identities(node: Mapping[str, Any]) -> Set[str]:
        uid = node.get("uid")
        values: List[Any] = [
            node.get("id"),
            node.get("task_object_id"),
            node.get("entity_id"),
            node.get("object_id"),
            node.get("label"),
            node.get("name"),
            node.get("simulator_name"),
            uid,
        ]
        if uid is not None:
            try:
                values.append(f"obj_{int(uid):04d}")
            except (TypeError, ValueError):
                pass
        aliases = node.get("aliases") or ()
        values.extend((aliases,) if isinstance(aliases, str) else aliases)
        identities: Set[str] = set()
        for value in values:
            identities.update(_identity_keys(value))
        return identities

    @staticmethod
    def _matches(node: Mapping[str, Any], relevant: Set[str]) -> bool:
        if not relevant:
            return True
        identities = SceneSection._node_identities(node)
        return bool(identities.intersection(relevant))

    @staticmethod
    def _room_nodes(room: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
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

    @staticmethod
    def _room_edges(room: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
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

    @staticmethod
    def _edge_view(edge: Mapping[str, Any]) -> Dict[str, Any]:
        source = _edge_endpoint(edge, "source")
        target = _edge_endpoint(edge, "target")
        relation = edge.get("type") or edge.get("relation") or edge.get("predicate")
        return {
            key: value
            for key, value in {
                "source": source,
                "relation": relation,
                "target": target,
            }.items()
            if value is not None
        }

    def _room_view(
        self,
        rooms: Iterable[Mapping[str, Any]],
        relevant: Set[str],
    ) -> List[Dict[str, Any]]:
        selected_rooms: List[Dict[str, Any]] = []
        remaining = self.max_items
        remaining_edges = self.max_items
        for room in rooms:
            selected_nodes: List[Dict[str, Any]] = []
            selected_identities: Set[str] = set()
            seen_nodes: Set[Tuple[str, ...]] = set()
            for node in self._room_nodes(room):
                if remaining <= 0 or not self._matches(node, relevant):
                    continue
                identities = self._node_identities(node)
                identity_key = tuple(sorted(identities)) or (_json(self._node_view(node)),)
                if identity_key in seen_nodes:
                    continue
                seen_nodes.add(identity_key)
                selected_nodes.append(self._node_view(node))
                selected_identities.update(identities)
                remaining -= 1
            if not selected_nodes:
                continue
            selected_edges: List[Dict[str, Any]] = []
            seen_edges: Set[Tuple[Any, Any, Any]] = set()
            for edge in self._room_edges(room):
                source = _edge_endpoint(edge, "source")
                target = _edge_endpoint(edge, "target")
                relation = edge.get("type") or edge.get("relation") or edge.get("predicate")
                edge_key = (str(source), str(relation), str(target))
                if (
                    _identity_keys(source).intersection(selected_identities)
                    or _identity_keys(target).intersection(selected_identities)
                ) and edge_key not in seen_edges:
                    selected_edges.append(self._edge_view(edge))
                    seen_edges.add(edge_key)
                    remaining_edges -= 1
                if remaining_edges <= 0:
                    break
            selected_rooms.append(
                {
                    key: value
                    for key, value in {
                        "room_id": room.get("room_id"),
                        "room_name": room.get("room_name"),
                        "nodes": selected_nodes,
                        "edges": selected_edges,
                    }.items()
                    if value is not None and value != []
                }
            )
            if remaining <= 0 or remaining_edges <= 0:
                break
        return selected_rooms

    def content(self, context: PromptContext) -> Any:
        scene = _as_dict(context.current_scene)
        if not isinstance(scene, Mapping):
            return scene
        relevant: Set[str] = set()
        for value in context.relevant_entity_ids:
            relevant.update(_identity_keys(value))
        room_values = scene.get("rooms", ()) or ()
        if isinstance(room_values, Mapping):
            room_values = room_values.values()
        rooms = tuple(room for room in room_values if isinstance(room, Mapping))
        if rooms:
            return {
                "step_index": scene.get("step_index"),
                "rooms": self._room_view(rooms, relevant),
            }
        nodes = list(_mapping_values(scene.get("nodes") or scene.get("entities")))
        if nodes:
            selected = []
            selected_ids = set()
            for node in nodes:
                node_dict = _as_dict(node)
                if not isinstance(node_dict, Mapping) or not self._matches(node_dict, relevant):
                    continue
                selected.append(self._node_view(node_dict))
                selected_ids.update(self._node_identities(node_dict))
                if len(selected) >= self.max_items:
                    break
            edges = []
            for edge in _mapping_values(scene.get("edges") or scene.get("relations")):
                edge_dict = _as_dict(edge)
                if not isinstance(edge_dict, Mapping):
                    continue
                source = _edge_endpoint(edge_dict, "source")
                target = _edge_endpoint(edge_dict, "target")
                if (
                    _identity_keys(source).intersection(selected_ids)
                    or _identity_keys(target).intersection(selected_ids)
                ):
                    edges.append(self._edge_view(edge_dict))
                if len(edges) >= self.max_items:
                    break
            return {"nodes": selected, "edges": edges}
        allowed = {
            key: value
            for key, value in scene.items()
            if key in {"step_index", "room", "visible_objects", "held_object"}
        }
        relations = scene.get("relations")
        if isinstance(relations, Mapping):
            relations = tuple(relations.values())
        if isinstance(relations, (list, tuple)):
            allowed["relations"] = [
                self._edge_view(edge)
                for edge in relations[: self.max_items]
                if isinstance(edge, Mapping)
            ]
        return allowed


class ObjectSection(BaseSection):
    name = "objects"
    title = "Relevant Objects"

    def content(self, context: PromptContext) -> Any:
        objects = []
        for value in context.object_views:
            payload = _mapping_view(
                value,
                (
                    "entity_id",
                    "canonical_name",
                    "aliases",
                    "properties",
                    "states",
                    "capabilities",
                    "room_id",
                    "available",
                    "last_seen_step",
                    "manipulations",
                ),
            )
            manipulations = payload.get("manipulations")
            if isinstance(manipulations, (list, tuple)):
                payload["manipulations"] = [
                    _mapping_view(
                        item,
                        (
                            "action_id",
                            "action",
                            "step",
                            "actor_id",
                            "tool_id",
                            "target_id",
                            "role",
                            "count",
                        ),
                    )
                    for item in manipulations
                ]
            if payload:
                objects.append(payload)
        return objects


class MemorySection(BaseSection):
    name = "memory"
    title = "Relevant Task Memory"

    def content(self, context: PromptContext) -> Any:
        recall = context.memory_recall
        if hasattr(recall, "to_prompt_context"):
            return recall.to_prompt_context()
        return recall


class TimerSection(BaseSection):
    name = "timers"
    title = "Pending Temporal Processes"

    def content(self, context: PromptContext) -> Any:
        timers = [
            _mapping_view(
                value,
                (
                    "process_id",
                    "process_type",
                    "entity_ids",
                    "start_step",
                    "ready_step",
                    "readiness_predicate",
                    "blocking_actions",
                    "status",
                ),
            )
            for value in context.pending_timers
        ]
        return [timer for timer in timers if timer]


class ActionSection(BaseSection):
    name = "action"
    title = "Candidate Action"

    def content(self, context: PromptContext) -> Any:
        return _mapping_view(
            context.candidate_action,
            ("name", "actor_id", "object_id", "target_id", "parameters"),
        )


class TaskSection(BaseSection):
    name = "task"
    title = "Task"

    def content(self, context: PromptContext) -> Any:
        values = {
            "instruction": context.task_instruction,
            "goal": _as_dict(context.section_data.get("goal_description")),
            "rules": _as_dict(context.section_data.get("task_rules")),
            "allowed_actions": list(context.allowed_actions),
        }
        return {
            key: value
            for key, value in values.items()
            if value is not None and value != "" and value != () and value != []
        }


class RethinkingSection(BaseSection):
    name = "rethinking"
    title = "Required Rethinking"

    def content(self, context: PromptContext) -> Any:
        return context.rethinking_reason


def default_section_registry(*, max_scene_items: int = 20) -> Registry[PromptSectionProvider]:
    registry: Registry[PromptSectionProvider] = Registry()
    for provider in (
        TaskSection(),
        SceneSection(max_items=max_scene_items),
        ObjectSection(),
        MemorySection(),
        TimerSection(),
        ActionSection(),
        RethinkingSection(),
    ):
        registry.register(provider.name, provider)
    return registry


__all__ = ["PromptSectionProvider", "default_section_registry"]
