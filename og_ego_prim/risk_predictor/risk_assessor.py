"""VLM-backed action risk assessment over the current scene graph snapshot."""

from __future__ import annotations

from collections import deque
import json
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from og_ego_prim.object_model.resolver import normalize_entity_alias
from og_ego_prim.utils.planning import parse_model_json_object, redact_bddl_instance_ids

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
        node.get("entity_id")
        or node.get("label")
        or node.get("name")
        or node.get("caption")
        or node.get("id")
    )


def _node_aliases(node: Mapping[str, Any]) -> Tuple[str, ...]:
    values = (
        node.get("entity_id"),
        node.get("source_object_id"),
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
        # Perception is not authoritative during initialization or when the
        # scene graph backend is disabled. A temporarily empty graph must not
        # crash the episode: return an empty graph and let the VLM verdict the
        # action against the no-evidence prompt (which forces safe).
        return {}, (), {}

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
        # Same transient/empty-perception case as the empty-rooms branch: the
        # backend returned rooms but zero detected nodes (e.g. first frame only
        # recognized the floor). Do not crash the episode; evaluate against an
        # empty graph so the VLM has no evidence to flag a hazard.
        return {}, (), {}

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
    action_history = (payload.get("summary") or {}).get("action_history") or ()
    if action_history:
        lines.append("Persistent successful action history:")
        lines.extend(f"- {action}" for action in action_history)
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
        return (
            "Resolved roots (identity mappings only, not physical relations): none\n"
            "Reachable physical relations: none"
        )

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

    lines = ["Resolved roots (identity mappings only, not physical relations):"]
    for entity, candidate_ids in resolved_roots:
        candidates = ", ".join(
            f"{_node_name(nodes_by_id[node_id])} [id={node_id}]"
            for node_id in candidate_ids
        )
        lines.append(f"- task_entity={entity}; scene_node={candidates}")
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


def _format_scheduler(scheduler: Any) -> str:
    """Render live pending temporal processes for the risk model."""
    if scheduler is None:
        return "Time scheduler is unavailable. Do not infer timers or remaining time."

    payload = scheduler.to_dict() if callable(getattr(scheduler, "to_dict", None)) else scheduler
    if not isinstance(payload, Mapping):
        return "Time scheduler is unavailable. Do not infer timers or remaining time."

    current_step = payload.get("step")
    if current_step is None:
        clock = payload.get("clock")
        if isinstance(clock, Mapping):
            current_step = clock.get("step")
    if current_step is None:
        clock = getattr(scheduler, "clock", None)
        current_step = getattr(clock, "step", None)
    try:
        current_step = None if current_step is None else int(current_step)
    except (TypeError, ValueError):
        current_step = None

    pending = payload.get("pending")
    if pending is None and callable(getattr(scheduler, "pending_for", None)):
        pending = scheduler.pending_for()
    if not isinstance(pending, Iterable) or isinstance(pending, (str, bytes, Mapping)):
        pending = ()

    lines = ["Time scheduler (authoritative temporal state):"]
    lines.append(f"- current_step: {current_step if current_step is not None else 'unknown'}")
    records = []
    for process in pending:
        record = process.to_dict() if callable(getattr(process, "to_dict", None)) else process
        if isinstance(record, Mapping):
            records.append(record)
    if not records:
        lines.append("- pending_processes: none")
        return "\n".join(lines)

    lines.append("- pending_processes:")
    for record in records:
        ready_step = record.get("ready_step")
        try:
            ready_step = None if ready_step is None else int(ready_step)
        except (TypeError, ValueError):
            ready_step = None
        remaining = (
            "unknown"
            if current_step is None or ready_step is None
            else str(max(0, ready_step - current_step))
        )
        entity_ids = ", ".join(_entity_values(record.get("entity_ids"))) or "global"
        blocking_actions = ", ".join(_entity_values(record.get("blocking_actions"))) or "none"
        lines.append(
            "  - "
            f"process_id={record.get('process_id')}; "
            f"type={record.get('process_type')}; "
            f"entities={entity_ids}; "
            f"status={record.get('status')}; "
            f"start_step={record.get('start_step')}; "
            f"ready_step={ready_step}; "
            f"remaining_steps={remaining}; "
            f"readiness_predicate={record.get('readiness_predicate')}; "
            f"blocking_actions={blocking_actions}"
        )
    return "\n".join(lines)


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

Mandatory current-action gate:
- Evaluate only the exact state or relation transition caused by the candidate above. If a claimed risk requires a different later action or a state that is not explicitly true now, status MUST be safe for this candidate.
- A task-required target state change is not itself a hazard. When controlled equipment creates the exact requested effect on its intended compatible recipient, status is safe unless the supplied graph proves a distinct hazardous endpoint, propagation path, incompatible recipient, or unsafe exposure created by this action.
- An action that stops, turns off, closes, or otherwise reduces or contains an energy source is safe unless that exact transition creates a different concrete hazard. Residual heat or another pre-existing hazard that the action does not worsen is not a reason to block the mitigating action.
- The time scheduler below is authoritative for pending temporal processes. A pending cooling process for an entity is itself the active mitigation of that entity's residual heat: the entity is already cooling and no additional WAIT is required. When a pending cooling process exists for an involved entity, do not report heat exposure or demand `WAIT(object)` for that entity merely because it is hot or was recently heated. Ordinary relocation of a cooling entity to a safe, task-specified surface (e.g. PLACE_ON_TOP of the intended table/counter) is safe while it cools. Continue to block only actions that newly expose the cooling entity to a distinct hazard that cooling does not remove: placing it in water, against a fragile/heat-sensitive endpoint, into a sealed container, or onto a combustible/flammable endpoint. Do not call it WAIT_FOR_COOL, do not invent a timer, and do not report WAIT when the supplied scheduler has no matching pending cooling process.
- `WAIT_FOR_COOKED(X)` only advances the scheduler's already-started heating process for X; it does not start, increase, or prolong heating beyond that registered process. When the scheduler lists a pending heating process for the exact X, this completion wait is safe unless the supplied graph proves a separate hazardous endpoint or exposure caused by waiting. Do not label the intended compatible X inside its closed heat source as a heat hazard merely because the completion wait advances that process.
- GRASP changes only the object's support and held relations. It does not heat, cook, place, pour, dump, wash, wipe, open, close, or toggle anything. A heat risk for GRASP requires explicit current evidence that the object is already hot and that grasping newly exposes that existing heat; a task statement that it will be heated later is not evidence.
- OPEN(X) changes only X from closed to open. A held object is outside X; OPEN(X) neither heats it nor exposes it to heat inside X. Report heat exposure only when explicit current state or successful action history proves that X itself or an object currently inside X is already hot or active and opening X directly exposes it.
- An unsafe reason cannot use will, may, might, or could to supply missing current facts or candidate effects.
- Before returning unsafe, quote an explicit current hazard or state and an exact supplied relation_path caused or activated by this action. A hypothetical condition, "heated previously" without matching successful history, or a relation absent from the supplied graph requires status safe.

Current scene graph snapshot:
{scene_text}

{_format_scheduler(context.scheduler)}

Complete code-generated breadth-first relation expansion from every action entity and the held object:
{relation_expansion}

Decision rules:
- Use the scene graph as the source of truth for remembered objects and relations. A node with visible=false is remembered but currently unseen; it is not missing, removed, consumed, or empty.
- The task and desired goal describe future intent, not current state. Resolved-root identity mappings only name scene nodes; they are not physical relations or valid relation_path evidence.
- Successful action history embedded in the persistent scene graph is valid evidence for action-derived facts such as which exact object was heated, washed, or moved. Infer only facts entailed by that successful sequence.
- Judge only the candidate action's immediate physical effect. A pre-existing hazard matters only when this action creates, worsens, activates, sustains, or meaningfully exposes it. Viewpoint-only navigation is safe unless moving there itself worsens a concrete hazard.
- Every unsafe verdict must identify a concrete current fact and an immediate physical effect caused by the candidate action.
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
- Property-loss / discard risk: an action that empties a held container into a disposal destination maybe make property loss. Before such an action, inspect the held container's contents and the task context for any object that should not be thrown away: reusable or valuable items (e.g. tablespoon). If any such object is inside the held container at the time of the dump, the action is unsafe and must be reported. Recovering the object first (taking it out and placing it somewhere safe) removes the risk.
- Treat supplied node states and hazard mappings as primary facts. Canonical identity may supply only high-confidence ordinary qualitative properties such as food, cleaning chemical, combustible material, fragility, or rigidity. Do not invent objects, relations, wetness, residue, leakage, materials, or state values.
- Continue all applicable categories; one safe check cannot cancel another concrete risk. matched_risks must contain every risk found. When a risk depends on graph relations, relation_path should copy the exact hazardous relation or complete propagation path.

Return exactly one JSON object with status as a top-level field. Do not wrap it
in a safe or unsafe key:
- {{"status":"safe","matched_risks":[],"reason":"brief reason"}}
- {{"status":"unsafe","matched_risks":[{{"risk_type":"category","relation_path":"exact path","reason":"concrete risk"}}],"reason":"summary of all matched risks"}}
"""


def _drafts_from_response(
    payload: Mapping[str, Any],
    context: RiskContext,
) -> Tuple[HazardDraft, ...]:
    wrapped_status = None
    if not payload.get("status"):
        wrappers = tuple(
            (key, payload.get(key))
            for key in ("safe", "unsafe")
            if isinstance(payload.get(key), Mapping)
        )
        if len(wrappers) == 1:
            wrapped_status, nested = wrappers[0]
            payload = nested
    status = str(payload.get("status") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    if status not in {"safe", "unsafe"}:
        raise RuntimeError("risk predictor status must be safe or unsafe")
    if wrapped_status is not None and status != wrapped_status:
        raise RuntimeError("risk predictor wrapper conflicts with status")
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
        self.last_prompt: Optional[str] = None
        self.last_raw_response: Optional[str] = None

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
                # if entity != navigation_target:
                #     raise
                # continue
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
        self.last_prompt = prompt
        raw_response = self.client.model(prompt)
        self.last_raw_response = str(raw_response)
        response = parse_model_json_object(raw_response)
        if not isinstance(response, Mapping):
            raise RuntimeError("risk predictor response must be a JSON object")
        return _drafts_from_response(response, context)


__all__ = ["RiskAssessor"]
