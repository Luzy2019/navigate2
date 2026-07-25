"""Convert human-confirmed current-frame objects into a perception result."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .perception import PerceivedObject, PerceivedRelation, PerceptionResult
from .schema import normalize_scene_graph_name


MANUAL_CURRENT_FRAME_PERCEPTION_SCHEMA_VERSION = "isbench.manual_current_frame_perception.v1"


class ManualCurrentFrameValidationError(ValueError):
    """Raised when a human-confirmed current-frame payload is invalid."""


def load_manual_current_frame_perception(
    path: Path,
    *,
    frame_index: int,
) -> PerceptionResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualCurrentFrameValidationError(
            f"invalid JSON in {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ManualCurrentFrameValidationError("manual perception must be a JSON object")
    return manual_current_frame_perception_result(payload, frame_index=frame_index)


def manual_current_frame_perception_result(
    payload: Mapping[str, Any],
    *,
    frame_index: int,
) -> PerceptionResult:
    schema_version = payload.get("schema_version")
    if schema_version not in {None, MANUAL_CURRENT_FRAME_PERCEPTION_SCHEMA_VERSION}:
        raise ManualCurrentFrameValidationError(
            f"unsupported manual perception schema_version: {schema_version!r}"
        )
    raw_objects = payload.get("objects")
    if not isinstance(raw_objects, Sequence) or isinstance(raw_objects, (str, bytes)):
        raise ManualCurrentFrameValidationError("objects must be a list")

    objects = []
    object_ids = set()
    room_objects: Dict[str, list[str]] = {}
    for index, raw_object in enumerate(raw_objects):
        if not isinstance(raw_object, Mapping):
            raise ManualCurrentFrameValidationError(
                f"objects[{index}] must be an object"
            )
        object_id = str(raw_object.get("id") or raw_object.get("object_id") or "").strip()
        if not object_id:
            raise ManualCurrentFrameValidationError(f"objects[{index}] has no id")
        if object_id in object_ids:
            raise ManualCurrentFrameValidationError(f"duplicate object id: {object_id!r}")
        caption = str(raw_object.get("caption") or raw_object.get("name") or "").strip()
        if not caption:
            raise ManualCurrentFrameValidationError(
                f"objects[{index}] has no caption or name"
            )
        room_id = str(raw_object.get("room") or raw_object.get("room_id") or "").strip()
        if not room_id:
            raise ManualCurrentFrameValidationError(f"objects[{index}] has no room")
        object_ids.add(object_id)
        room_objects.setdefault(room_id, []).append(object_id)
        name = _object_name(raw_object, object_id)
        states = raw_object.get("states") or {}
        if not isinstance(states, Mapping):
            raise ManualCurrentFrameValidationError(
                f"objects[{index}].states must be an object when provided"
            )
        hazard = raw_object.get("hazard") or {}
        if not isinstance(hazard, Mapping):
            raise ManualCurrentFrameValidationError(
                f"objects[{index}].hazard must be an object when provided"
            )
        position = raw_object.get("position")
        if position is not None:
            if not isinstance(position, Sequence) or isinstance(position, (str, bytes)):
                raise ManualCurrentFrameValidationError(
                    f"objects[{index}].position must be a numeric list when provided"
                )
            try:
                position = [float(value) for value in position]
            except (TypeError, ValueError) as exc:
                raise ManualCurrentFrameValidationError(
                    f"objects[{index}].position must be numeric"
                ) from exc
        objects.append(
            PerceivedObject(
                object_id=object_id,
                name=name,
                category=name,
                position=position,
                room_id=room_id,
                confidence=1.0,
                attributes={
                    "caption": caption,
                    "currently_visible": True,
                    "is_vis": True,
                    "is_coarse": bool(raw_object.get("is_coarse", True)),
                    "manual_object_id": object_id,
                    "manual_confirmation": True,
                    "states": dict(states),
                    "hazard": dict(hazard),
                    "last_seen_frame": int(frame_index),
                    **_entity_id_attribute(raw_object, index),
                },
            )
        )

    raw_relations = payload.get("relations", [])
    if not isinstance(raw_relations, Sequence) or isinstance(raw_relations, (str, bytes)):
        raise ManualCurrentFrameValidationError("relations must be a list")
    relations = []
    relation_keys = set()
    for index, raw_relation in enumerate(raw_relations):
        if not isinstance(raw_relation, Mapping):
            raise ManualCurrentFrameValidationError(
                f"relations[{index}] must be an object"
            )
        source_id = str(raw_relation.get("source_id") or raw_relation.get("source") or "").strip()
        target_id = str(raw_relation.get("target_id") or raw_relation.get("target") or "").strip()
        relation = str(raw_relation.get("relation") or raw_relation.get("type") or "").strip()
        if source_id not in object_ids or target_id not in object_ids:
            raise ManualCurrentFrameValidationError(
                f"relations[{index}] references an object outside the current frame"
            )
        if source_id == target_id or not relation:
            raise ManualCurrentFrameValidationError(f"relations[{index}] is invalid")
        key = (source_id, relation, target_id)
        if key not in relation_keys:
            relation_keys.add(key)
            relations.append(
                PerceivedRelation(
                    source_id=source_id,
                    target_id=target_id,
                    relation=relation,
                    confidence=1.0,
                    source="manual_current_frame",
                )
            )

    room_graph = {
        "rooms": [
            {"id": room_id, "caption": room_id, "objects": room_object_ids}
            for room_id, room_object_ids in room_objects.items()
        ]
    }
    group_graph = {
        "groups": [
            {
                "id": f"manual_current_frame:{room_id}",
                "name": "manual_current_frame",
                "room": room_id,
                "objects": room_object_ids,
            }
            for room_id, room_object_ids in room_objects.items()
        ]
    }
    return PerceptionResult(
        backend="manual_current_frame",
        frame_index=int(frame_index),
        objects=objects,
        relations=relations,
        room_graph=room_graph,
        group_graph=group_graph,
        metadata={
            "manual_confirmation": True,
            "manual_input_object_count": len(objects),
            "manual_input_relation_count": len(relations),
            "manual_annotation_mode": str(
                payload.get("annotation_mode") or "complete_current_frame"
            ),
            "mapping_mode": "not_run_no_mask_or_depth_annotation",
        },
    )


def _object_name(raw_object: Mapping[str, Any], object_id: str) -> str:
    """Prefer an explicit ASCII name, then derive one from the stable ID."""
    explicit_name = normalize_scene_graph_name(raw_object.get("name"))
    if explicit_name != "object":
        return explicit_name
    identifier_name = re.sub(r"(?:[_-]?\d+)+$", "", object_id)
    identifier_name = normalize_scene_graph_name(identifier_name)
    if identifier_name == "object":
        raise ManualCurrentFrameValidationError(
            f"object {object_id!r} needs an ASCII semantic name"
        )
    return identifier_name


def _entity_id_attribute(raw_object: Mapping[str, Any], index: int) -> Dict[str, str]:
    """Keep an optional task-instance binding through normal graph canonicalization."""
    entity_id = str(raw_object.get("entity_id") or "").strip()
    if not entity_id:
        return {}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", entity_id):
        raise ManualCurrentFrameValidationError(
            f"objects[{index}].entity_id is not a valid task entity id"
        )
    return {"entity_id": entity_id}
