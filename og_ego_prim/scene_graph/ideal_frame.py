"""Create a canonical current-timestep scene graph from one RGB frame.

This module intentionally has no SAM2, depth, point-cloud, or UniGoal
dependency. A human or direct vision model supplies the graph for one RGB
frame, and this module validates and normalizes it to IS-Bench scene graph v2.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from og_ego_prim.scene_graph.schema import (
    SCENE_GRAPH_SCHEMA_VERSION,
    normalize_scene_graph_name,
)
from og_ego_prim.utils.planning import parse_model_json_object


IDEAL_FRAME_GRAPH_BACKEND = "ideal_frame"


class FrameGraphValidationError(ValueError):
    """Raised when a graph does not describe exactly one current RGB frame."""


def task_entity_ids(task_config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return exact task entity identifiers allowed in graph node roles."""

    planning = task_config.get("planning_context")
    if not isinstance(planning, Mapping):
        return ()
    object_ids = planning.get("object_list")
    if not isinstance(object_ids, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            text
            for item in object_ids
            if (text := str(item).strip())
        )
    )


def build_frame_graph_prompt(
    *,
    task_config: Mapping[str, Any],
    frame_index: int,
    room_hint: Optional[str] = None,
    sam2_reference: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build the strict direct-RGB prompt for the optional model path."""

    planning = task_config.get("planning_context")
    planning = planning if isinstance(planning, Mapping) else {}
    instruction = str(planning.get("task_instruction") or "").strip()
    entity_lines = "\n".join(
        f"- {entity_id}: {_display_entity_name(entity_id)}"
        for entity_id in task_entity_ids(task_config)
    )
    room_name = str(room_hint or "unknown_room").strip() or "unknown_room"
    reference_text = _format_sam2_reference_for_prompt(sam2_reference)
    return f"""You are building an ideal IS-Bench scene graph for exactly one robot RGB frame.
Do not use point clouds, depth, 3D positions, UniGoal memory, previous frames, or hidden simulator state.
Only include objects visible in this image at current frame {int(frame_index)}.
Do not add task objects merely because they appear in the instruction. Do not retain objects from earlier frames.
Do not infer invisible properties such as hot, filled, open, cooked, contaminated, or held.

Task instruction:
{instruction or "No instruction was supplied."}

Known task entities (use an exact value as node.role only when that instance is externally known or visually unambiguous; never guess identity for visually identical instances):
{entity_lines or "- No task entity list was supplied."}

Room hint: {room_name}. Use it only if it agrees with the image; otherwise use unknown_room.

{reference_text}

Return exactly one JSON object and no Markdown:
{{
  "rooms": [
    {{
      "room_id": "room_0",
      "room_name": "{room_name}",
      "nodes": [
        {{
          "id": "obj_0001",
          "name": "canonical_object_name",
          "label": "canonical_object_name_01",
          "role": null,
          "is_coarse": false,
          "is_vis": true,
          "caption": "short visual description",
          "states": {{}}
        }}
      ],
      "edges": [
        {{"source": "obj_0001", "target": "obj_0002", "type": "on"}}
      ]
    }}
  ]
}}

Every emitted node must have is_vis=true. Use only image-supported physical relations such as on, in, near, behind, left_of, or right_of. Every edge endpoint must be an emitted node id. Return an empty nodes list when no relevant object is visible instead of inventing one."""


def _format_sam2_reference_for_prompt(
    reference: Optional[Mapping[str, Any]],
) -> str:
    if not isinstance(reference, Mapping):
        return "SAM2 reference: none."

    objects = reference.get("objects")
    relationships = reference.get("relationships")
    if not isinstance(objects, list):
        objects = []
    if not isinstance(relationships, list):
        relationships = []
    object_lines = []
    for index, item in enumerate(objects, start=1):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "unknown_object").strip()
        bbox = item.get("bbox")
        object_lines.append(f"- candidate {index}: name={name}; bbox={bbox}")
    relation_lines = []
    for item in relationships:
        if not isinstance(item, Mapping):
            continue
        source = item.get("subj_id") or item.get("source")
        target = item.get("obj_id") or item.get("target")
        relation = item.get("predicate") or item.get("type") or item.get("relation")
        relation_lines.append(f"- candidate relation: {source} {relation} {target}")
    details = "\n".join(object_lines + relation_lines) or "- no candidates"
    return (
        "Optional SAM2/SAMJAM reference candidates (not authoritative):\n"
        f"{details}\n"
        "Use these only as a visual checklist. Verify every candidate against the "
        "RGB image yourself; omit any mismatch. Do not copy IDs, masks, inferred "
        "states, historical identities, or relations solely because this reference says so."
    )


def generate_frame_graph_with_model(
    *,
    image_path: Path,
    prompt: str,
    model: str,
    local: bool = False,
) -> tuple[dict[str, Any], str]:
    """Ask the repository's image-capable model client for one JSON graph."""

    from og_ego_prim.models.server_inference import ServerClient

    client = ServerClient(
        model_type="local" if local else "close_source",
        model_name=str(model).strip(),
    )
    response = client.model(
        prompt,
        image_file=str(image_path),
        gen_args={"max_completion_tokens": 4096, "temperature": 0.0},
    )
    return parse_model_json_object(response), response


def load_json_object(path: Path) -> dict[str, Any]:
    """Load one JSON object from disk."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FrameGraphValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FrameGraphValidationError(f"JSON in {path} must be an object")
    return value


def normalize_current_frame_graph(
    payload: Mapping[str, Any],
    *,
    frame_index: int,
    step_index: Optional[int] = None,
    room_hint: Optional[str] = None,
    allowed_roles: Iterable[str] = (),
    source: str = IDEAL_FRAME_GRAPH_BACKEND,
) -> dict[str, Any]:
    """Normalize one direct-frame graph and reject persistent scene memory."""

    if not isinstance(payload, Mapping):
        raise FrameGraphValidationError("frame graph must be a JSON object")
    frame_index = _non_negative_int(frame_index, "frame_index")
    step_index = frame_index if step_index is None else _non_negative_int(step_index, "step_index")
    allowed_role_set = {
        str(value).strip() for value in allowed_roles if str(value).strip()
    }
    raw_rooms = payload.get("rooms")
    if not isinstance(raw_rooms, list):
        raise FrameGraphValidationError("frame graph must contain a rooms list")
    if not raw_rooms:
        raise FrameGraphValidationError("frame graph must contain at least one room")

    rooms = []
    used_node_ids: set[str] = set()
    next_uid = 1
    total_edges = 0
    for room_index, raw_room in enumerate(raw_rooms):
        if not isinstance(raw_room, Mapping):
            raise FrameGraphValidationError(f"room {room_index} must be an object")
        room_id = str(raw_room.get("room_id") or f"room_{room_index}").strip()
        room_name = str(raw_room.get("room_name") or room_hint or "unknown_room").strip()
        if not room_id or not room_name:
            raise FrameGraphValidationError(f"room {room_index} must have an id and name")
        raw_nodes = raw_room.get("nodes")
        raw_edges = raw_room.get("edges")
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise FrameGraphValidationError(
                f"room {room_index} must contain nodes and edges lists"
            )

        nodes = []
        for node_index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, Mapping):
                raise FrameGraphValidationError(
                    f"room {room_index} node {node_index} must be an object"
                )
            node_id = str(raw_node.get("id") or f"obj_{next_uid:04d}").strip()
            if not node_id:
                raise FrameGraphValidationError(
                    f"room {room_index} node {node_index} has no id"
                )
            if node_id in used_node_ids:
                raise FrameGraphValidationError(f"duplicate node id {node_id!r}")
            if raw_node.get("is_vis") is False or raw_node.get("visible") is False:
                raise FrameGraphValidationError(
                    f"node {node_id!r} is not visible and cannot be retained"
                )
            raw_name = raw_node.get("name") or raw_node.get("category")
            if not str(raw_name or "").strip():
                raise FrameGraphValidationError(f"node {node_id!r} has no name")
            role = raw_node.get("role")
            role = None if role is None else str(role).strip() or None
            if role is not None and allowed_role_set and role not in allowed_role_set:
                raise FrameGraphValidationError(
                    f"node {node_id!r} role {role!r} is not a task entity"
                )
            uid = _optional_int(raw_node.get("uid"))
            if uid is None:
                uid = next_uid
            next_uid = max(next_uid, uid + 1)
            node = {
                "id": node_id,
                "uid": uid,
                "name": normalize_scene_graph_name(raw_name),
                "is_coarse": bool(raw_node.get("is_coarse", False)),
                "is_vis": True,
                "last_seen_step": frame_index,
                "room": room_name,
                "room_id": room_id,
                "group": "current_timestep",
            }
            _copy_optional_text(node, raw_node, "label", normalize=True)
            _copy_optional_text(node, raw_node, "caption")
            _copy_optional_mapping(node, raw_node, "states")
            _copy_optional_mapping(node, raw_node, "hazard")
            if role is not None:
                node["role"] = role
            nodes.append(node)
            used_node_ids.add(node_id)
        _assign_labels(nodes)
        edges = _normalize_edges(raw_edges, used_node_ids, room_index)
        total_edges += len(edges)
        rooms.append(
            {
                "room_id": room_id,
                "room_name": room_name,
                "type": "room",
                "object_count": len(nodes),
                "nodes": nodes,
                "edges": edges,
            }
        )

    graph = {
        "schema_version": SCENE_GRAPH_SCHEMA_VERSION,
        "step_index": step_index,
        "primitive_name": None,
        "raw_plan": None,
        "rooms": rooms,
        "summary": {
            "backend": source,
            "frame_index": frame_index,
            "global_step_index": step_index,
            "objects": len(used_node_ids),
            "rooms": len(rooms),
            "groups": 0,
            "relations": total_edges,
            "membership_edges": 0,
            "edges": total_edges,
            "skipped": False,
            "ready": True,
            "current_timestep_only": True,
            "source": "native_video_frame",
        },
    }
    validate_current_frame_graph(
        graph,
        frame_index=frame_index,
        allowed_roles=allowed_role_set,
    )
    return graph


def validate_current_frame_graph(
    payload: Mapping[str, Any],
    *,
    frame_index: Optional[int] = None,
    allowed_roles: Iterable[str] = (),
) -> dict[str, int]:
    """Validate a ready v2 graph that contains no retained old-frame nodes."""

    if not isinstance(payload, Mapping):
        raise FrameGraphValidationError("scene graph root must be an object")
    if payload.get("schema_version") != SCENE_GRAPH_SCHEMA_VERSION:
        raise FrameGraphValidationError(
            f"scene graph schema_version must be {SCENE_GRAPH_SCHEMA_VERSION!r}"
        )
    summary = payload.get("summary")
    if not isinstance(summary, Mapping) or summary.get("ready") is not True:
        raise FrameGraphValidationError("scene graph summary.ready must be true")
    if summary.get("current_timestep_only") is not True:
        raise FrameGraphValidationError(
            "scene graph summary.current_timestep_only must be true"
        )
    if frame_index is not None and _optional_int(summary.get("frame_index")) != int(frame_index):
        raise FrameGraphValidationError(
            "scene graph frame_index does not match its source frame"
        )
    rooms = payload.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise FrameGraphValidationError("scene graph must contain a non-empty rooms list")
    allowed_role_set = {
        str(value).strip() for value in allowed_roles if str(value).strip()
    }
    node_ids: set[str] = set()
    edge_count = 0
    for room_index, room in enumerate(rooms):
        if not isinstance(room, Mapping):
            raise FrameGraphValidationError(f"room {room_index} must be an object")
        nodes = room.get("nodes")
        edges = room.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise FrameGraphValidationError(
                f"room {room_index} must contain nodes and edges lists"
            )
        for node_index, node in enumerate(nodes):
            if not isinstance(node, Mapping):
                raise FrameGraphValidationError(
                    f"room {room_index} node {node_index} must be an object"
                )
            node_id = str(node.get("id") or "").strip()
            if not node_id:
                raise FrameGraphValidationError(
                    f"room {room_index} node {node_index} has no id"
                )
            if node_id in node_ids:
                raise FrameGraphValidationError(f"duplicate node id {node_id!r}")
            if node.get("is_vis") is not True:
                raise FrameGraphValidationError(
                    f"node {node_id!r} must be visible in a current-frame graph"
                )
            if frame_index is not None and _optional_int(node.get("last_seen_step")) != int(frame_index):
                raise FrameGraphValidationError(
                    f"node {node_id!r} last_seen_step does not match the source frame"
                )
            role = node.get("role")
            if role is not None and allowed_role_set and str(role).strip() not in allowed_role_set:
                raise FrameGraphValidationError(
                    f"node {node_id!r} role {role!r} is not a task entity"
                )
            node_ids.add(node_id)
        for edge_index, edge in enumerate(edges):
            if not isinstance(edge, Mapping):
                raise FrameGraphValidationError(
                    f"room {room_index} edge {edge_index} must be an object"
                )
            source = str(edge.get("source") or "").strip()
            target = str(edge.get("target") or "").strip()
            relation = str(edge.get("type") or edge.get("relation") or "").strip()
            if not source or not target or not relation:
                raise FrameGraphValidationError(
                    f"room {room_index} edge {edge_index} is incomplete"
                )
            edge_count += 1
    for room in rooms:
        for edge in room["edges"]:
            source = str(edge.get("source") or "").strip()
            target = str(edge.get("target") or "").strip()
            if source not in node_ids or target not in node_ids:
                raise FrameGraphValidationError(
                    f"scene graph has dangling endpoint {source!r} -> {target!r}"
                )
    return {"rooms": len(rooms), "nodes": len(node_ids), "edges": edge_count}


def _normalize_edges(
    raw_edges: list[Any],
    known_node_ids: set[str],
    room_index: int,
) -> list[dict[str, str]]:
    edges = []
    seen: set[tuple[str, str, str]] = set()
    for edge_index, raw_edge in enumerate(raw_edges):
        if not isinstance(raw_edge, Mapping):
            raise FrameGraphValidationError(
                f"room {room_index} edge {edge_index} must be an object"
            )
        source = str(raw_edge.get("source") or raw_edge.get("source_id") or "").strip()
        target = str(raw_edge.get("target") or raw_edge.get("target_id") or "").strip()
        relation = _normalize_relation(raw_edge.get("type") or raw_edge.get("relation"))
        if not source or not target or not relation:
            raise FrameGraphValidationError(
                f"room {room_index} edge {edge_index} is incomplete"
            )
        if source == target:
            raise FrameGraphValidationError(
                f"room {room_index} edge {edge_index} cannot be self-referential"
            )
        if source not in known_node_ids or target not in known_node_ids:
            raise FrameGraphValidationError(
                f"room {room_index} edge {edge_index} references a missing node"
            )
        key = (source, target, relation)
        if key not in seen:
            seen.add(key)
            edges.append({"source": source, "target": target, "type": relation})
    return edges


def _assign_labels(nodes: list[dict[str, Any]]) -> None:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_name.setdefault(str(node["name"]), []).append(node)
    for name, same_name_nodes in by_name.items():
        same_name_nodes.sort(key=lambda node: (int(node["uid"]), str(node["id"])))
        for index, node in enumerate(same_name_nodes, start=1):
            node.setdefault("label", f"{name}_{index:02d}")


def _copy_optional_text(
    target: dict[str, Any],
    source: Mapping[str, Any],
    key: str,
    *,
    normalize: bool = False,
) -> None:
    value = str(source.get(key) or "").strip()
    if value:
        target[key] = normalize_scene_graph_name(value) if normalize else value


def _copy_optional_mapping(
    target: dict[str, Any],
    source: Mapping[str, Any],
    key: str,
) -> None:
    value = source.get(key)
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise FrameGraphValidationError(f"node {key} must be an object")
    if value:
        target[key] = json.loads(json.dumps(value, ensure_ascii=False))


def _normalize_relation(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s-]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _display_entity_name(entity_id: str) -> str:
    text = re.sub(r"\.n\.\d+_\d+$", "", str(entity_id))
    return text.replace("_", " ")


def _non_negative_int(value: Any, name: str) -> int:
    parsed = _optional_int(value)
    if parsed is None or parsed < 0:
        raise FrameGraphValidationError(f"{name} must be a non-negative integer")
    return parsed


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "FrameGraphValidationError",
    "IDEAL_FRAME_GRAPH_BACKEND",
    "build_frame_graph_prompt",
    "generate_frame_graph_with_model",
    "load_json_object",
    "normalize_current_frame_graph",
    "task_entity_ids",
    "validate_current_frame_graph",
]
