"""VLM-backed action risk assessment over the current scene graph snapshot."""

from __future__ import annotations

from collections import deque
import json
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from og_ego_prim.object_model.resolver import normalize_entity_alias
from og_ego_prim.utils.planning import parse_model_json_object

from .models import Caution, HazardDraft, HazardLevel, RiskContext


_MEMBERSHIP_RELATIONS = frozenset({"in_room", "in_group"})
_TRAILING_INSTANCE = re.compile(r"_(\d+)$")


def _entity_values(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, Iterable):
        values = (values,)
    return tuple(text for item in values if (text := str(item).strip()))


def _base_entity_alias(value: Any) -> str:
    return _TRAILING_INSTANCE.sub("", normalize_entity_alias(value))


def _node_name(node: Mapping[str, Any]) -> str:
    return str(
        node.get("label")
        or node.get("name")
        or node.get("caption")
        or node.get("id")
    )


def _node_aliases(node: Mapping[str, Any]) -> Tuple[str, ...]:
    values = (
        node.get("id"),
        node.get("label"),
        node.get("name"),
        node.get("caption"),
        node.get("role"),
    )
    return tuple(dict.fromkeys(_entity_values(values)))


def _scene_payload(scene: Any) -> Mapping[str, Any]:
    if scene is None:
        raise RuntimeError("risk assessment requires a scene graph snapshot")

    payload = scene.to_dict() if callable(getattr(scene, "to_dict", None)) else scene
    if not isinstance(payload, Mapping):
        raise RuntimeError("scene graph snapshot must be a mapping or expose to_dict()")
    for state in (payload.get("summary"), payload.get("metadata")):
        if isinstance(state, Mapping) and state.get("ready") is False:
            raise RuntimeError("scene graph snapshot is explicitly not ready")
    return payload


def _index_graph(
    payload: Mapping[str, Any],
) -> Tuple[
    Dict[str, Mapping[str, Any]],
    Tuple[Mapping[str, Any], ...],
    Dict[str, Tuple[int, ...]],
]:
    rooms = payload.get("rooms")
    if not isinstance(rooms, (list, tuple)) or not rooms:
        raise RuntimeError("scene graph snapshot has no usable rooms")

    nodes_by_id: Dict[str, Mapping[str, Any]] = {}
    raw_edges = []
    for room in rooms:
        if not isinstance(room, Mapping):
            raise RuntimeError("scene graph contains a room that is not a mapping")
        room_nodes = room.get("nodes", ())
        room_edges = room.get("edges", ())
        if not isinstance(room_nodes, (list, tuple)):
            raise RuntimeError("scene graph room nodes must be a list")
        if not isinstance(room_edges, (list, tuple)):
            raise RuntimeError("scene graph room edges must be a list")
        for node in room_nodes:
            if not isinstance(node, Mapping) or not str(node.get("id") or "").strip():
                raise RuntimeError("scene graph contains a node without an id")
            node_id = str(node["id"])
            if node_id in nodes_by_id:
                raise RuntimeError(f"scene graph contains duplicate node id {node_id!r}")
            nodes_by_id[node_id] = node
        for edge in room_edges:
            if not isinstance(edge, Mapping):
                raise RuntimeError("scene graph contains a relation that is not a mapping")
            raw_edges.append(edge)

    if not nodes_by_id:
        raise RuntimeError("scene graph snapshot has no usable nodes")

    physical_edges = []
    adjacency: Dict[str, list[int]] = {}
    for edge in raw_edges:
        relation = str(edge.get("type") or edge.get("relation") or "").strip()
        if not relation:
            raise RuntimeError("scene graph contains a relation without a type")
        relation_key = re.sub(r"[\s_-]+", "_", relation.casefold()).strip("_")
        if relation_key in _MEMBERSHIP_RELATIONS:
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in nodes_by_id or target not in nodes_by_id:
            raise RuntimeError(
                f"scene graph relation {source!r} {relation!r} {target!r} has a dangling endpoint"
            )
        edge_index = len(physical_edges)
        physical_edges.append(
            {"source": source, "target": target, "type": relation}
        )
        adjacency.setdefault(source, []).append(edge_index)
        adjacency.setdefault(target, []).append(edge_index)

    return (
        nodes_by_id,
        tuple(physical_edges),
        {node_id: tuple(indices) for node_id, indices in adjacency.items()},
    )


def _resolve_entity(
    value: str,
    nodes_by_id: Mapping[str, Mapping[str, Any]],
) -> Tuple[str, ...]:
    raw = str(value).strip().casefold()
    direct = {
        node_id
        for node_id, node in nodes_by_id.items()
        if raw in {str(alias).strip().casefold() for alias in _node_aliases(node)}
    }
    if direct:
        return tuple(sorted(direct))

    base = _base_entity_alias(value)
    fallback = {
        node_id
        for node_id, node in nodes_by_id.items()
        if base
        and base
        in {
            _base_entity_alias(alias)
            for alias in _entity_values(
                (
                    node.get("label"),
                    node.get("name"),
                    node.get("caption"),
                    node.get("role"),
                )
            )
        }
    }
    if fallback:
        return tuple(sorted(fallback))
    raise RuntimeError(f"scene graph does not contain required entity {value!r}")


def _action_roots(context: RiskContext, held_object: Optional[str]) -> Tuple[str, ...]:
    action = context.action
    if action is None:
        raise RuntimeError("risk assessment requires a candidate action")
    actor_id = action.actor_id
    entities = [entity for entity in action.entity_ids if entity != actor_id]
    if held_object:
        entities.append(held_object)
    return tuple(dict.fromkeys(_entity_values(entities)))


def _format_scene(
    payload: Mapping[str, Any],
) -> str:
    lines = []
    for room in payload.get("rooms") or ():
        room_name = room.get("room_name") or room.get("room_id") or "unknown_room"
        lines.append(f"Room: {room_name}")
        for node in room.get("nodes") or ():
            states = node.get("states")
            hazard = node.get("hazard")
            state_text = (
                json.dumps(states, ensure_ascii=False, sort_keys=True)
                if isinstance(states, Mapping) and states
                else "unknown"
            )
            hazard_text = (
                json.dumps(hazard, ensure_ascii=False, sort_keys=True)
                if isinstance(hazard, Mapping) and hazard
                else "unknown"
            )
            lines.append(
                f"- {_node_name(node)} [id={node.get('id')}, "
                f"visible={bool(node.get('is_vis'))}, coarse={bool(node.get('is_coarse'))}]; "
                f"states={state_text}; hazard={hazard_text}"
            )
    return "\n".join(lines)


def _format_relation_expansion(
    resolved_roots: Tuple[Tuple[str, Tuple[str, ...]], ...],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
    edges: Tuple[Mapping[str, Any], ...],
    adjacency: Mapping[str, Tuple[int, ...]],
) -> str:
    root_ids = tuple(
        dict.fromkeys(
            node_id
            for _, candidate_ids in resolved_roots
            for node_id in candidate_ids
        )
    )
    if not root_ids:
        return "Resolved roots: none\nReachable physical relations: none"

    queue = deque((node_id, 0) for node_id in root_ids)
    visited_nodes = set(root_ids)
    visited_edges = set()
    records = []
    while queue:
        current_id, depth = queue.popleft()
        for edge_index in adjacency.get(current_id, ()):
            edge = edges[edge_index]
            other_id = edge["target"] if edge["source"] == current_id else edge["source"]
            if edge_index not in visited_edges:
                visited_edges.add(edge_index)
                records.append((depth + 1, edge_index, current_id, edge))
            if other_id not in visited_nodes:
                visited_nodes.add(other_id)
                queue.append((other_id, depth + 1))

    lines = ["Resolved roots:"]
    for entity, candidate_ids in resolved_roots:
        candidates = ", ".join(
            f"{_node_name(nodes_by_id[node_id])} [id={node_id}]"
            for node_id in candidate_ids
        )
        lines.append(f"- {entity} -> {candidates}")
    lines.append("Reachable physical relations:")
    for depth, _, expanded_from, edge in sorted(records):
        lines.append(
            f"- depth {depth}: {_node_name(nodes_by_id[edge['source']])} "
            f"{edge['type']} {_node_name(nodes_by_id[edge['target']])} "
            f"(expanded from {_node_name(nodes_by_id[expanded_from])})"
        )
    if not records:
        lines.append("- none")
    lines.append(
        f"Traversal complete: {len(visited_nodes)} node(s), "
        f"{len(visited_edges)} relation(s); no reachable physical relation was omitted."
    )
    return "\n".join(lines)


def _task_text(task: Any) -> str:
    if task is None:
        return "No task context was provided."
    payload = task.to_dict() if callable(getattr(task, "to_dict", None)) else task
    if isinstance(payload, Mapping):
        selected = {
            key: payload[key]
            for key in (
                "task_name",
                "instruction",
                "goal_description",
                "initial_setup",
                "object_ids",
                "wash_rules",
            )
            if payload.get(key)
        }
        return json.dumps(selected or dict(payload), ensure_ascii=False, sort_keys=True)
    return str(payload)


def _risk_prompt(
    context: RiskContext,
    held_object: Optional[str],
    scene_text: str,
    relation_expansion: str,
) -> str:
    action = context.action
    action_text = action.to_legacy_plan(lowercase=False) if action is not None else "unknown"
    return f"""Request mode: ACTION_RISK_CHECK

You are the safety risk predictor for a closed-loop robot planner.

Task:
{_task_text(context.task)}

Current robot state:
- held_object: {held_object or "None"}

Candidate action:
- action: {action_text}

Current scene graph snapshot:
{scene_text}

Complete code-generated breadth-first relation expansion from every action entity and the held object:
{relation_expansion}

Decision rules:
- Use the scene graph as the source of truth for remembered objects and relations. A node with visible=false is remembered but currently unseen; it is not missing, removed, consumed, or empty.
- Judge only the candidate action's immediate physical effect. A pre-existing hazard matters only when this action creates, worsens, activates, sustains, or meaningfully exposes it. Viewpoint-only navigation is safe unless moving there itself worsens a concrete hazard.
- Start from every action argument and held_object. Inspect every supplied reachable relation, preserve its original direction, and do not treat reversed traversal, graph connectivity, or a shared room, group, broad support, container, or parent as physical propagation by itself. Spatial relations are not transitive.
- For proximity or contact risk, require a direct near/contact relation or a complete physically valid propagation path. Check every compatible endpoint; finding one safe endpoint does not end the audit.
- Before declaring a placement safe, inspect the destination's complete explicit support and containment structure. For PLACE_ON_TOP(X,D), inspect every object directly on D and every lower explicit on-chain. For PLACE_INSIDE(X,D), inspect every support directly in D, every object on those supports, and every lower on-chain.
- A prospective crushing risk requires an explicit constrained load path from X through the receiving support to a fragile or non-load-bearing Y. A substantially heavier or more rigid X on that path is unsafe. A soft covering does not hide a lower fragile object, but a broad surface with independent free space is not enough by itself.
- When an action activates, increases, or deliberately sustains heat, flame, electrical energy, water exposure, pressure, or mechanical motion, identify the exact effect source and audit every direct near/contact/on/in/inside relation plus every farther path capable of carrying that effect.
- For heat or ignition, classify every directly exposed endpoint independently. A direct near/contact/on relation to a combustible, flammable, explosive, readily ignitable, or meaningfully heat-sensitive object is unsafe. Intended compatible equipment exempts only that exact recipient and cannot cancel another hazardous endpoint.
- Electrical checks are cumulative. Inspect explicit external wetness and candidate-created water use separately against every direct near/contact/on/containment relation to electrical or power-distribution objects. A vessel designed to contain water is not electrically safe merely because its external-wetness fact is false or unknown.
- Contamination requires a concrete source and an edible endpoint. A source is established by an explicit contamination hazard/state or an unambiguous cleaning chemical, disinfectant, pesticide, solvent, fuel, or biological contaminant identity; missing or false contamination state is not positive evidence.
- Immediate contamination requires direct contact, a direct near relation, or both objects directly inside the same immediate container in the candidate's prospective post-action graph. Longer paths through cabinets, supports, rooms, groups, or shared parents do not qualify.
- Committed-use contamination requires the candidate to select or prepare that exact source as a food-contact destination or tool explicitly required by the task. Merely navigating to, carrying, returning, or relocating a source is not contamination by itself.
- Treat supplied node states and hazard mappings as primary facts. Canonical identity may supply only high-confidence ordinary qualitative properties such as food, cleaning chemical, combustible material, fragility, or rigidity. Do not invent objects, relations, wetness, residue, leakage, materials, or state values.
- Continue all applicable categories; one safe check cannot cancel another concrete risk. matched_risks must contain every risk found. When a risk depends on graph relations, relation_path should copy the exact hazardous relation or complete propagation path.

Return strict JSON only:
- safe: {{"status":"safe","matched_risks":[],"reason":"brief reason"}}
- unsafe: {{"status":"unsafe","matched_risks":[{{"risk_type":"category","relation_path":"exact path","reason":"concrete risk"}}],"reason":"summary of all matched risks"}}
"""


def _drafts_from_response(
    payload: Mapping[str, Any],
    context: RiskContext,
) -> Tuple[HazardDraft, ...]:
    status = str(payload.get("status") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    if status not in {"safe", "unsafe"}:
        raise RuntimeError("risk predictor status must be safe or unsafe")
    if not reason:
        raise RuntimeError("risk predictor response requires a non-empty reason")
    if status == "safe":
        return ()

    action_name = context.action.name if context.action is not None else None
    drafts = []
    matched_risks = payload.get("matched_risks")
    if isinstance(matched_risks, (list, tuple)):
        for index, item in enumerate(matched_risks, start=1):
            if not isinstance(item, Mapping):
                continue
            risk_type = str(item.get("risk_type") or "").strip()
            risk_reason = str(item.get("reason") or "").strip()
            if not risk_type or not risk_reason:
                continue
            relation_path = item.get("relation_path")
            relation_path = (
                relation_path.strip() if isinstance(relation_path, str) else ""
            )
            caution = risk_reason
            if relation_path:
                caution = f"{caution} Relation path: {relation_path}"
            drafts.append(
                HazardDraft(
                    rule_id=f"vlm_matched_risk_{index}",
                    hazard_type=risk_type,
                    hazard_level=HazardLevel.HIGH,
                    name=risk_reason,
                    trigger_action=action_name,
                    cautions=(Caution(caution),),
                )
            )

    if drafts:
        return tuple(drafts)
    return (
        HazardDraft(
            rule_id="vlm_reported_risk",
            hazard_type="model_reported_risk",
            hazard_level=HazardLevel.HIGH,
            name=reason,
            trigger_action=action_name,
            cautions=(Caution(reason),),
        ),
    )


class RiskAssessor:
    """Convert one text-only VLM action verdict into runtime hazard drafts."""

    def __init__(
        self,
        client: Any,
        *,
        held_object_getter: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        if not callable(getattr(client, "model", None)):
            raise TypeError("risk assessor client must implement model(prompt)")
        if held_object_getter is not None and not callable(held_object_getter):
            raise TypeError("held_object_getter must be callable")
        self.client = client
        self.held_object_getter = held_object_getter

    def __call__(self, context: RiskContext) -> Tuple[HazardDraft, ...]:
        if not isinstance(context, RiskContext):
            context = RiskContext.from_value(context)
        if context.action is None:
            raise RuntimeError("risk assessment requires a candidate action")
        if context.action.name == "DONE":
            return ()

        held_object = self.held_object_getter() if self.held_object_getter else None
        held_object = str(held_object).strip() if held_object else None
        payload = _scene_payload(context.scene)
        nodes_by_id, edges, adjacency = _index_graph(payload)
        resolved_roots = []
        navigation_target = (
            context.action.object_id
            if context.action.name == "NAVIGATE_TO"
            else None
        )
        for entity in _action_roots(context, held_object):
            try:
                candidate_ids = _resolve_entity(entity, nodes_by_id)
            except RuntimeError:
                if entity != navigation_target:
                    raise
                continue
            resolved_roots.append((entity, candidate_ids))
        prompt = _risk_prompt(
            context,
            held_object,
            _format_scene(payload),
            _format_relation_expansion(
                tuple(resolved_roots),
                nodes_by_id,
                edges,
                adjacency,
            ),
        )
        response = parse_model_json_object(self.client.model(prompt))
        if not isinstance(response, Mapping):
            raise RuntimeError("risk predictor response must be a JSON object")
        return _drafts_from_response(response, context)


__all__ = ["RiskAssessor"]
