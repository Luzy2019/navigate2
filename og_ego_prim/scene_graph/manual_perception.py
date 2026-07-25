"""Contracts for approved per-frame manual perception annotations.

The manual-corrected path replaces SAMJAM / SAM2 / UniGoal recognition only.
It deliberately emits ``PerceptionResult`` inputs; canonical scene-graph nodes,
edges, state diffs, and runtime scheduling remain owned by the normal runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np

from og_ego_prim.scene_graph.schema import normalize_scene_graph_name


MANUAL_OBJECT_CATALOG_SCHEMA_VERSION = "isbench.manual_object_catalog.v1"
MANUAL_PERCEPTION_SCHEMA_VERSION = "isbench.manual_corrected_perception.v1"
MANUAL_PERCEPTION_FILE_SUFFIX = ".manual_perception.json"

TASK_ENTITY_VISIBLE = "visible"
TASK_ENTITY_NOT_VISIBLE = "not_visible"
TASK_ENTITY_NOT_VISUAL = "not_visual"
TASK_ENTITY_VISIBILITY_VALUES = frozenset(
    {
        TASK_ENTITY_VISIBLE,
        TASK_ENTITY_NOT_VISIBLE,
        TASK_ENTITY_NOT_VISUAL,
    }
)


class ManualPerceptionValidationError(ValueError):
    """Raised when a manual perception collection is incomplete or inconsistent."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def catalog_sha256(catalog: Mapping[str, Any]) -> str:
    """Hash immutable catalog identities without invalidating review approval."""

    identity_payload = dict(catalog)
    identity_payload.pop("identity_bindings_reviewed", None)
    return hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rgb_sha256(rgb: Any) -> str:
    """Hash the exact RGB array consumed by the live perception backend."""

    array = np.ascontiguousarray(np.asarray(rgb))
    if array.ndim != 3 or array.shape[-1] < 3:
        raise ManualPerceptionValidationError(
            f"manual perception requires HxWxC RGB, got {array.shape}"
        )
    return hashlib.sha256(array[:, :, :3].tobytes()).hexdigest()


def load_json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualPerceptionValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManualPerceptionValidationError(f"JSON in {path} must be an object")
    return value


def task_entity_ids(task_config: Mapping[str, Any]) -> Tuple[str, ...]:
    planning = task_config.get("planning_context")
    planning = planning if isinstance(planning, Mapping) else {}
    values = planning.get("object_list")
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            value
            for item in values
            if (value := str(item).strip())
        )
    )


def task_entity_name(entity_id: Any) -> str:
    text = re.sub(r"\.n\.\d+_\d+$", "", str(entity_id or "").strip())
    return normalize_scene_graph_name(text)


def build_manual_object_catalog(task_config: Mapping[str, Any]) -> Dict[str, Any]:
    """Create the immutable task-object portion of a manual object catalog."""

    entity_ids = task_entity_ids(task_config)
    return {
        "schema_version": MANUAL_OBJECT_CATALOG_SCHEMA_VERSION,
        "task_entity_ids": list(entity_ids),
        "objects": [
            {
                "object_id": f"task:{entity_id}",
                "entity_id": entity_id,
                "uid": 100000 + index,
                "name": task_entity_name(entity_id),
                "category": task_entity_name(entity_id),
                "is_coarse": False,
            }
            for index, entity_id in enumerate(entity_ids, start=1)
        ],
        "identity_bindings_reviewed": False,
    }


def catalog_object_maps(
    catalog: Mapping[str, Any],
    *,
    expected_task_entity_ids: Iterable[str] = (),
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Tuple[str, ...]]:
    """Validate immutable object identities and return lookup tables.

    A catalog is deliberately closed-world: frame annotations may reference only
    these stable object IDs.  This prevents SAM-style per-frame object creation
    and forces explicit task-instance bindings for visually identical objects.
    """

    if catalog.get("schema_version") != MANUAL_OBJECT_CATALOG_SCHEMA_VERSION:
        raise ManualPerceptionValidationError(
            "manual object catalog has an unsupported schema_version"
        )
    if catalog.get("identity_bindings_reviewed") is not True:
        raise ManualPerceptionValidationError(
            "catalog.identity_bindings_reviewed must be true before runtime use"
        )
    raw_task_ids = catalog.get("task_entity_ids")
    if not isinstance(raw_task_ids, list):
        raise ManualPerceptionValidationError("catalog.task_entity_ids must be a list")
    catalog_task_ids = tuple(str(value).strip() for value in raw_task_ids if str(value).strip())
    if len(set(catalog_task_ids)) != len(catalog_task_ids):
        raise ManualPerceptionValidationError("catalog.task_entity_ids contains duplicates")
    expected = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in expected_task_entity_ids
            if str(value).strip()
        )
    )
    if expected and set(expected) != set(catalog_task_ids):
        raise ManualPerceptionValidationError(
            "manual catalog task entities do not match the active task"
        )

    raw_objects = catalog.get("objects")
    if not isinstance(raw_objects, list):
        raise ManualPerceptionValidationError("catalog.objects must be a list")
    object_by_id: Dict[str, Dict[str, Any]] = {}
    object_by_entity: Dict[str, Dict[str, Any]] = {}
    used_uids = set()
    task_categories = {task_entity_name(entity_id) for entity_id in catalog_task_ids}
    for index, raw_object in enumerate(raw_objects):
        if not isinstance(raw_object, Mapping):
            raise ManualPerceptionValidationError(f"catalog object {index} must be an object")
        object_id = str(raw_object.get("object_id") or "").strip()
        if not object_id:
            raise ManualPerceptionValidationError(f"catalog object {index} has no object_id")
        if object_id in object_by_id:
            raise ManualPerceptionValidationError(f"duplicate catalog object_id {object_id!r}")
        uid = _positive_int(raw_object.get("uid"), f"catalog object {object_id!r} uid")
        if uid in used_uids:
            raise ManualPerceptionValidationError(f"duplicate catalog uid {uid}")
        used_uids.add(uid)
        name = _normalized_name(raw_object.get("name"), f"catalog object {object_id!r}")
        category = _normalized_name(
            raw_object.get("category") or name,
            f"catalog object {object_id!r} category",
        )
        entity_id = raw_object.get("entity_id")
        entity_id = None if entity_id is None else str(entity_id).strip() or None
        if entity_id is not None:
            if entity_id not in catalog_task_ids:
                raise ManualPerceptionValidationError(
                    f"catalog object {object_id!r} binds unknown task entity {entity_id!r}"
                )
            if entity_id in object_by_entity:
                raise ManualPerceptionValidationError(
                    f"task entity {entity_id!r} has multiple catalog object IDs"
                )
        elif name in task_categories or category in task_categories:
            raise ManualPerceptionValidationError(
                f"catalog object {object_id!r} uses task category {name!r} without an exact entity_id"
            )
        identity_mode = str(
            raw_object.get("identity_mode") or MANUAL_IDENTITY_MODE_UNVERIFIED
        ).strip()
        if identity_mode not in MANUAL_IDENTITY_MODES:
            raise ManualPerceptionValidationError(
                f"catalog object {object_id!r} has unsupported identity_mode {identity_mode!r}"
            )
        simulator_name = raw_object.get("simulator_name")
        simulator_name = (
            None if simulator_name is None else str(simulator_name).strip() or None
        )
        if entity_id is not None:
            if identity_mode == MANUAL_IDENTITY_MODE_UNVERIFIED:
                raise ManualPerceptionValidationError(
                    f"catalog object {object_id!r} must declare a reviewed identity_mode"
                )
            if identity_mode == MANUAL_IDENTITY_MODE_SIMULATOR_OBJECT and not simulator_name:
                raise ManualPerceptionValidationError(
                    f"catalog object {object_id!r} needs simulator_name for simulator_object identity"
                )
            if identity_mode == MANUAL_IDENTITY_MODE_NON_VISUAL and simulator_name is not None:
                raise ManualPerceptionValidationError(
                    f"catalog object {object_id!r} non_visual identity cannot declare simulator_name"
                )
        normalized = {
            "object_id": object_id,
            "uid": uid,
            "name": name,
            "category": category,
            "entity_id": entity_id,
            "is_coarse": bool(raw_object.get("is_coarse", False)),
            "identity_mode": identity_mode,
            "simulator_name": simulator_name,
        }
        object_by_id[object_id] = normalized
        if entity_id is not None:
            object_by_entity[entity_id] = normalized

    missing = set(catalog_task_ids) - set(object_by_entity)
    if missing:
        raise ManualPerceptionValidationError(
            "catalog must bind every task entity exactly once: " + ", ".join(sorted(missing))
        )
    return object_by_id, object_by_entity, catalog_task_ids


def validate_manual_annotation(
    annotation: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    expected_task_entity_ids: Iterable[str] = (),
    expected_catalog_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a normalized approved annotation or fail closed.

    The validation is intentionally strict.  It makes the human correction
    collection an explicit oracle instead of silently falling back to SAM2,
    semantic name matching, or cross-frame association heuristics.
    """

    object_by_id, object_by_entity, catalog_task_ids = catalog_object_maps(
        catalog,
        expected_task_entity_ids=expected_task_entity_ids,
    )
    if annotation.get("schema_version") != MANUAL_PERCEPTION_SCHEMA_VERSION:
        raise ManualPerceptionValidationError(
            "manual perception annotation has an unsupported schema_version"
        )
    if annotation.get("status") != "approved":
        raise ManualPerceptionValidationError(
            "manual perception annotation status must be 'approved'"
        )
    if expected_catalog_sha256 is not None:
        provided = str(annotation.get("catalog_sha256") or "").strip()
        if provided != expected_catalog_sha256:
            raise ManualPerceptionValidationError(
                "manual perception annotation does not bind the active object catalog"
            )

    source_frame = annotation.get("source_frame")
    if not isinstance(source_frame, Mapping):
        raise ManualPerceptionValidationError("annotation.source_frame must be an object")
    frame_index = _non_negative_int(source_frame.get("frame_index"), "source_frame.frame_index")
    rgb_digest = str(source_frame.get("rgb_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", rgb_digest):
        raise ManualPerceptionValidationError(
            "source_frame.rgb_sha256 must be a SHA256 hex digest"
        )
    rgb_shape = _rgb_shape(source_frame.get("rgb_shape"))
    sensor_name = str(source_frame.get("sensor_name") or "").strip() or None

    coverage = annotation.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ManualPerceptionValidationError("annotation.coverage must be an object")
    for key in (
        "visible_task_entities_complete",
        "objects_human_approved",
        "identity_bindings_reviewed",
        "relations_human_approved",
    ):
        if coverage.get(key) is not True:
            raise ManualPerceptionValidationError(f"coverage.{key} must be true")
    raw_visibility = coverage.get("task_entity_visibility")
    if not isinstance(raw_visibility, Mapping):
        raise ManualPerceptionValidationError(
            "coverage.task_entity_visibility must be an object"
        )
    visibility = {str(key).strip(): str(value).strip() for key, value in raw_visibility.items()}
    if set(visibility) != set(catalog_task_ids):
        raise ManualPerceptionValidationError(
            "coverage.task_entity_visibility must explicitly classify every task entity"
        )
    invalid_visibility = {
        entity_id: value
        for entity_id, value in visibility.items()
        if value not in TASK_ENTITY_VISIBILITY_VALUES
    }
    if invalid_visibility:
        raise ManualPerceptionValidationError(
            "unsupported task entity visibility values: "
            + ", ".join(f"{key}={value}" for key, value in sorted(invalid_visibility.items()))
        )

    raw_objects = annotation.get("objects")
    if not isinstance(raw_objects, list):
        raise ManualPerceptionValidationError("annotation.objects must be a list")
    objects = []
    seen_object_ids = set()
    visible_entity_counts: Dict[str, int] = {}
    for index, raw_object in enumerate(raw_objects):
        if not isinstance(raw_object, Mapping):
            raise ManualPerceptionValidationError(f"annotation object {index} must be an object")
        object_id = str(raw_object.get("object_id") or "").strip()
        if object_id not in object_by_id:
            raise ManualPerceptionValidationError(
                f"annotation object {object_id!r} is not present in the approved catalog"
            )
        if object_id in seen_object_ids:
            raise ManualPerceptionValidationError(
                f"annotation contains duplicate current-frame object {object_id!r}"
            )
        seen_object_ids.add(object_id)
        catalog_object = object_by_id[object_id]
        supplied_entity_id = raw_object.get("entity_id")
        if catalog_object["entity_id"] is not None:
            if str(supplied_entity_id or "").strip() != catalog_object["entity_id"]:
                raise ManualPerceptionValidationError(
                    f"annotation object {object_id!r} changes its immutable entity binding"
                )
        elif supplied_entity_id is not None:
            raise ManualPerceptionValidationError(
                f"annotation object {object_id!r} must not add an entity binding"
            )
        for key in ("name", "category"):
            if raw_object.get(key) is None:
                continue
            value = _normalized_name(raw_object.get(key), f"annotation object {object_id!r} {key}")
            if value != catalog_object[key]:
                raise ManualPerceptionValidationError(
                    f"annotation object {object_id!r} changes its immutable {key}"
                )
        if raw_object.get("is_vis") is False or raw_object.get("visible") is False:
            raise ManualPerceptionValidationError(
                f"annotation object {object_id!r} must be visible in a current frame"
            )
        room_id = str(raw_object.get("room_id") or raw_object.get("room") or "").strip()
        if not room_id:
            raise ManualPerceptionValidationError(
                f"annotation object {object_id!r} must declare a room_id"
            )
        bbox = _bbox(raw_object.get("bbox"), object_id, rgb_shape=rgb_shape)
        if catalog_object["entity_id"] is not None and bbox is None:
            raise ManualPerceptionValidationError(
                f"visible task entity {catalog_object['entity_id']!r} must have a current-frame bbox"
            )
        position = _position(raw_object.get("position"), object_id)
        states = _mapping_copy(raw_object.get("states"), f"annotation object {object_id!r} states")
        hazard = _mapping_copy(raw_object.get("hazard"), f"annotation object {object_id!r} hazard")
        caption = _optional_text(raw_object.get("caption"))
        entity_id = catalog_object["entity_id"]
        if entity_id is not None:
            visible_entity_counts[entity_id] = visible_entity_counts.get(entity_id, 0) + 1
        objects.append(
            {
                **catalog_object,
                "room_id": room_id,
                "bbox": bbox,
                "position": position,
                "states": states,
                "hazard": hazard,
                "caption": caption,
            }
        )

    for entity_id, visibility_value in visibility.items():
        count = visible_entity_counts.get(entity_id, 0)
        if visibility_value == TASK_ENTITY_VISIBLE and count != 1:
            raise ManualPerceptionValidationError(
                f"visible task entity {entity_id!r} must appear exactly once, found {count}"
            )
        if visibility_value != TASK_ENTITY_VISIBLE and count != 0:
            raise ManualPerceptionValidationError(
                f"non-visible task entity {entity_id!r} must not appear in this frame"
            )
    _validate_non_visible_reasons(annotation, visibility)

    raw_relations = annotation.get("relations", annotation.get("edges", []))
    if not isinstance(raw_relations, list):
        raise ManualPerceptionValidationError("annotation.relations must be a list")
    relations = []
    seen_relations = set()
    for index, raw_relation in enumerate(raw_relations):
        if not isinstance(raw_relation, Mapping):
            raise ManualPerceptionValidationError(
                f"annotation relation {index} must be an object"
            )
        source_id = str(raw_relation.get("source_id") or raw_relation.get("source") or "").strip()
        target_id = str(raw_relation.get("target_id") or raw_relation.get("target") or "").strip()
        relation = _relation(raw_relation.get("relation") or raw_relation.get("type"))
        if source_id not in seen_object_ids or target_id not in seen_object_ids:
            raise ManualPerceptionValidationError(
                f"annotation relation {index} references an object absent from this frame"
            )
        if source_id == target_id:
            raise ManualPerceptionValidationError(
                f"annotation relation {index} cannot be self-referential"
            )
        key = (source_id, relation, target_id)
        if key not in seen_relations:
            seen_relations.add(key)
            relations.append(
                {"source_id": source_id, "target_id": target_id, "relation": relation}
            )

    groups = _groups(annotation.get("groups"), seen_object_ids, objects)
    identity_ledger = _validate_identity_ledger(
        annotation,
        catalog_objects=object_by_entity,
        visibility=visibility,
    )
    return {
        "frame_index": frame_index,
        "rgb_sha256": rgb_digest,
        "rgb_shape": rgb_shape,
        "sensor_name": sensor_name,
        "objects": objects,
        "relations": relations,
        "groups": groups,
        "coverage": {
            "task_entity_visibility": visibility,
            "visible_task_entities_complete": True,
            "objects_human_approved": True,
            "identity_bindings_reviewed": True,
            "relations_human_approved": True,
        },
        "identity_ledger": identity_ledger,
        "sam2_reference": _mapping_copy(
            annotation.get("sam2_reference"), "annotation.sam2_reference"
        ),
    }


def build_manual_perception_prompt(
    *,
    task_config: Mapping[str, Any],
    frame_index: int,
    room_hint: Optional[str] = None,
    sam2_reference: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build a draft-only prompt; an approved human review is still mandatory."""

    planning = task_config.get("planning_context")
    planning = planning if isinstance(planning, Mapping) else {}
    instruction = str(planning.get("task_instruction") or "").strip()
    entities = task_entity_ids(task_config)
    entity_lines = "\n".join(f'- "{entity_id}"' for entity_id in entities)
    room_name = str(room_hint or "unknown_room").strip() or "unknown_room"
    reference_text = _sam2_reference_text(sam2_reference)
    return f"""Create a DRAFT manual-corrected perception annotation for exactly one robot RGB frame.
This is not a final scene graph. Do not create obj_XXXX nodes, rooms, groups, or historical memory.
Do not use depth, point clouds, UniGoal, prior frames, hidden simulator state, SAM2 IDs, or automatic tracking.
Only list objects visible in the current RGB image at frame {int(frame_index)}.

Task instruction:
{instruction or 'No task instruction was supplied.'}

Every task entity below must be explicitly classified as \"visible\", \"not_visible\", or \"not_visual\".
If visible, it must occur exactly once in objects with the exact entity_id. Never guess between visually identical instances.
{entity_lines or '- No task entities were supplied.'}

Room hint: {room_name}.
{reference_text}

Return exactly one JSON object and no Markdown:
{{
  "objects": [
    {{
      "object_id": "stable_catalog_object_id",
      "entity_id": null,
      "name": "canonical_object_name",
      "category": "canonical_object_name",
      "room_id": "{room_name}",
      "bbox": [x1, y1, x2, y2],
      "is_vis": true,
      "caption": "short image-grounded description",
      "states": {{}},
      "hazard": {{}}
    }}
  ],
  "relations": [
    {{"source_id": "stable_catalog_object_id", "target_id": "other_stable_catalog_object_id", "relation": "on"}}
  ],
  "coverage": {{
    "task_entity_visibility": {{"exact_task_entity": "visible_or_not_visible_or_not_visual"}},
    "visible_task_entities_complete": true,
    "objects_human_approved": false,
    "identity_bindings_reviewed": false,
    "relations_human_approved": false
  }}
}}

The result is a draft only. A reviewer must bind object_id values to the immutable catalog and change the approval flags before runtime use."""


def build_manual_annotation_template(
    *,
    catalog: Mapping[str, Any],
    frame_index: int,
    rgb: Any,
    sensor_name: Optional[str],
) -> Dict[str, Any]:
    """Create an intentionally invalid draft that a reviewer must complete.

    The draft names every task instance so an approval cannot accidentally omit
    one of two visually identical objects.  It cannot be consumed at runtime
    until every ``unreviewed`` entry is replaced and all approval flags are set.
    """

    raw_task_ids = catalog.get("task_entity_ids")
    task_ids = (
        tuple(str(value).strip() for value in raw_task_ids if str(value).strip())
        if isinstance(raw_task_ids, list)
        else ()
    )
    array = np.ascontiguousarray(np.asarray(rgb))
    if array.ndim != 3 or array.shape[-1] < 3:
        raise ManualPerceptionValidationError(
            f"manual perception requires HxWxC RGB, got {array.shape}"
        )
    return {
        "schema_version": MANUAL_PERCEPTION_SCHEMA_VERSION,
        "status": "draft",
        "catalog_sha256": catalog_sha256(catalog),
        "source_frame": {
            "frame_index": _non_negative_int(frame_index, "frame_index"),
            "rgb_sha256": rgb_sha256(array),
            "rgb_shape": list(array.shape[:3]),
            "sensor_name": str(sensor_name or "").strip() or None,
        },
        "coverage": {
            "task_entity_visibility": {
                entity_id: "unreviewed" for entity_id in task_ids
            },
            "non_visible_reasons": {},
            "visible_task_entities_complete": False,
            "objects_human_approved": False,
            "identity_bindings_reviewed": False,
            "relations_human_approved": False,
        },
        "identity_ledger": {
            "sequence_id": "unreviewed",
            "previous_frame_index": None,
            "previous_annotation_sha256": None,
            "reviewed": False,
            "entities": {
                entity_id: {
                    "object_id": f"task:{entity_id}",
                    "entity_id": entity_id,
                    "simulator_name": None,
                    "visibility": "unreviewed",
                    "identity_evidence": "unreviewed",
                }
                for entity_id in task_ids
            },
        },
        "objects": [],
        "relations": [],
        "groups": [],
        "sam2_reference": {},
    }


def _sam2_reference_text(reference: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(reference, Mapping):
        return "SAM2 reference: none."
    objects = reference.get("objects")
    if not isinstance(objects, list):
        return "SAM2 reference: no object candidates."
    lines = []
    for index, item in enumerate(objects, start=1):
        if not isinstance(item, Mapping):
            continue
        lines.append(
            f"- candidate {index}: name={item.get('name')}; bbox={item.get('bbox')}"
        )
    return (
        "Optional SAM2/SAMJAM candidates are reference-only; do not copy their IDs, "
        "objects, relations, or identities without image review:\n"
        + ("\n".join(lines) if lines else "- no candidates")
    )


def _catalog_identity_binding(
    entity_id: str,
    simulator_bindings: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    value = None if simulator_bindings is None else simulator_bindings.get(entity_id)
    if isinstance(value, Mapping):
        mode = str(value.get("identity_mode") or MANUAL_IDENTITY_MODE_UNVERIFIED).strip()
        simulator_name = value.get("simulator_name")
    elif value is None:
        mode = MANUAL_IDENTITY_MODE_UNVERIFIED
        simulator_name = None
    else:
        mode = MANUAL_IDENTITY_MODE_SIMULATOR_OBJECT
        simulator_name = value
    return {
        "identity_mode": mode,
        "simulator_name": (
            None if simulator_name is None else str(simulator_name).strip() or None
        ),
    }


def _environment_simulator_name(env: Any, entity_id: str) -> Optional[str]:
    task = getattr(env, "task", None)
    object_scope = getattr(task, "object_scope", None)
    if not isinstance(object_scope, Mapping):
        return None
    reference = object_scope.get(entity_id)
    wrapped = getattr(reference, "wrapped_obj", reference)
    name = getattr(wrapped, "name", None)
    return str(name).strip() if name is not None and str(name).strip() else None


def validate_catalog_environment_bindings(
    catalog: Mapping[str, Any],
    env: Any,
    *,
    expected_task_entity_ids: Iterable[str] = (),
) -> None:
    """Fail closed when a reviewed catalog no longer matches the live scene."""

    _, object_by_entity, _ = catalog_object_maps(
        catalog,
        expected_task_entity_ids=expected_task_entity_ids,
    )
    for entity_id, catalog_object in object_by_entity.items():
        expected_name = catalog_object["simulator_name"]
        identity_mode = catalog_object["identity_mode"]
        actual_name = _environment_simulator_name(env, entity_id)
        if identity_mode == MANUAL_IDENTITY_MODE_SIMULATOR_OBJECT:
            if actual_name != expected_name:
                raise ManualPerceptionValidationError(
                    "manual catalog simulator binding mismatch for "
                    f"{entity_id!r}: expected={expected_name!r} actual={actual_name!r}"
                )
        elif identity_mode == MANUAL_IDENTITY_MODE_NON_VISUAL and actual_name is not None:
            raise ManualPerceptionValidationError(
                "manual catalog marks a live task entity non_visual: "
                f"{entity_id!r} -> {actual_name!r}"
            )


def _validate_non_visible_reasons(
    annotation: Mapping[str, Any],
    visibility: Mapping[str, str],
) -> None:
    coverage = annotation.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    raw_reasons = coverage.get("non_visible_reasons")
    if not isinstance(raw_reasons, Mapping):
        raise ManualPerceptionValidationError(
            "coverage.non_visible_reasons must explain every non-visible task entity"
        )
    reasons = {str(key).strip(): str(value).strip() for key, value in raw_reasons.items()}
    expected = {
        entity_id
        for entity_id, value in visibility.items()
        if value != TASK_ENTITY_VISIBLE
    }
    if set(reasons) != expected:
        raise ManualPerceptionValidationError(
            "coverage.non_visible_reasons must match non-visible task entities"
        )
    missing = [entity_id for entity_id, reason in reasons.items() if not reason]
    if missing:
        raise ManualPerceptionValidationError(
            "coverage.non_visible_reasons must be non-empty: " + ", ".join(sorted(missing))
        )


def _validate_identity_ledger(
    annotation: Mapping[str, Any],
    *,
    catalog_objects: Mapping[str, Mapping[str, Any]],
    visibility: Mapping[str, str],
) -> Dict[str, Any]:
    raw_ledger = annotation.get("identity_ledger")
    if not isinstance(raw_ledger, Mapping):
        raise ManualPerceptionValidationError("annotation.identity_ledger must be an object")
    sequence_id = str(raw_ledger.get("sequence_id") or "").strip()
    if not sequence_id:
        raise ManualPerceptionValidationError("identity_ledger.sequence_id must be non-empty")
    reviewed = raw_ledger.get("reviewed")
    if reviewed is not True:
        raise ManualPerceptionValidationError("identity_ledger.reviewed must be true")
    previous_frame_index = raw_ledger.get("previous_frame_index")
    if previous_frame_index is not None:
        previous_frame_index = _non_negative_int(
            previous_frame_index,
            "identity_ledger.previous_frame_index",
        )
    previous_annotation_sha256 = raw_ledger.get("previous_annotation_sha256")
    if previous_annotation_sha256 is not None:
        previous_annotation_sha256 = str(previous_annotation_sha256).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", previous_annotation_sha256):
            raise ManualPerceptionValidationError(
                "identity_ledger.previous_annotation_sha256 must be a SHA256 hex digest"
            )
    raw_entities = raw_ledger.get("entities")
    if not isinstance(raw_entities, Mapping):
        raise ManualPerceptionValidationError("identity_ledger.entities must be an object")
    normalized_entities: Dict[str, Dict[str, Any]] = {}
    if set(str(key).strip() for key in raw_entities) != set(catalog_objects):
        raise ManualPerceptionValidationError(
            "identity_ledger.entities must cover every catalog task entity exactly once"
        )
    for entity_id, catalog_object in catalog_objects.items():
        raw_entry = raw_entities.get(entity_id)
        if not isinstance(raw_entry, Mapping):
            raise ManualPerceptionValidationError(
                f"identity_ledger.entities[{entity_id!r}] must be an object"
            )
        object_id = str(raw_entry.get("object_id") or "").strip()
        entry_entity_id = str(raw_entry.get("entity_id") or "").strip()
        simulator_name = raw_entry.get("simulator_name")
        simulator_name = (
            None if simulator_name is None else str(simulator_name).strip() or None
        )
        entry_visibility = str(raw_entry.get("visibility") or "").strip()
        evidence = str(raw_entry.get("identity_evidence") or "").strip()
        if object_id != catalog_object["object_id"] or entry_entity_id != entity_id:
            raise ManualPerceptionValidationError(
                f"identity_ledger entity {entity_id!r} changes immutable object binding"
            )
        if simulator_name != catalog_object["simulator_name"]:
            raise ManualPerceptionValidationError(
                f"identity_ledger entity {entity_id!r} changes simulator binding"
            )
        if entry_visibility != visibility[entity_id]:
            raise ManualPerceptionValidationError(
                f"identity_ledger entity {entity_id!r} disagrees with frame visibility"
            )
        if not evidence:
            raise ManualPerceptionValidationError(
                f"identity_ledger entity {entity_id!r} needs human identity_evidence"
            )
        normalized_entities[entity_id] = {
            "object_id": object_id,
            "entity_id": entry_entity_id,
            "simulator_name": simulator_name,
            "visibility": entry_visibility,
            "identity_evidence": evidence,
        }
    return {
        "sequence_id": sequence_id,
        "previous_frame_index": previous_frame_index,
        "previous_annotation_sha256": previous_annotation_sha256,
        "reviewed": True,
        "entities": normalized_entities,
    }


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ManualPerceptionValidationError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ManualPerceptionValidationError(f"{label} must be a positive integer")
    return parsed


def _non_negative_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ManualPerceptionValidationError(f"{label} must be a non-negative integer") from exc
    if parsed < 0:
        raise ManualPerceptionValidationError(f"{label} must be a non-negative integer")
    return parsed


def _normalized_name(value: Any, label: str) -> str:
    normalized = normalize_scene_graph_name(value)
    if normalized == "object" and not str(value or "").strip():
        raise ManualPerceptionValidationError(f"{label} must be non-empty")
    return normalized


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _mapping_copy(value: Any, label: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManualPerceptionValidationError(f"{label} must be an object")
    return json.loads(json.dumps(dict(value), ensure_ascii=False))


def _rgb_shape(value: Any) -> Tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ManualPerceptionValidationError("source_frame.rgb_shape must be [height, width, channels]")
    shape = tuple(_positive_int(item, "source_frame.rgb_shape") for item in value)
    if shape[2] < 3:
        raise ManualPerceptionValidationError("source_frame.rgb_shape channels must be at least 3")
    return shape  # type: ignore[return-value]


def _bbox(
    value: Any,
    object_id: str,
    *,
    rgb_shape: Tuple[int, int, int],
) -> Optional[list[float]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} bbox must contain four numbers"
        )
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} bbox must contain four numbers"
        ) from exc
    if not all(math.isfinite(item) for item in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} bbox must have positive finite area"
        )
    height, width, _ = rgb_shape
    if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > width or bbox[3] > height:
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} bbox must lie within current RGB bounds"
        )
    return bbox


def _position(value: Any, object_id: str) -> Optional[list[float]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} position must contain at least two numbers"
        )
    try:
        position = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} position must contain numbers"
        ) from exc
    if not all(math.isfinite(item) for item in position):
        raise ManualPerceptionValidationError(
            f"annotation object {object_id!r} position must be finite"
        )
    return position


def _relation(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ManualPerceptionValidationError("annotation relation must be non-empty")
    return text


def _groups(
    value: Any,
    visible_object_ids: set[str],
    objects: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    if value is None:
        by_room: Dict[str, list[str]] = {}
        for object_value in objects:
            by_room.setdefault(str(object_value["room_id"]), []).append(
                str(object_value["object_id"])
            )
        return [
            {
                "id": f"current:{room_id}",
                "name": "current_timestep",
                "room": room_id,
                "objects": object_ids,
            }
            for room_id, object_ids in sorted(by_room.items())
        ]
    if not isinstance(value, list):
        raise ManualPerceptionValidationError("annotation.groups must be a list")
    groups = []
    seen_group_ids = set()
    claimed_objects = set()
    for index, raw_group in enumerate(value):
        if not isinstance(raw_group, Mapping):
            raise ManualPerceptionValidationError(f"annotation group {index} must be an object")
        group_id = str(raw_group.get("id") or raw_group.get("group_id") or "").strip()
        if not group_id or group_id in seen_group_ids:
            raise ManualPerceptionValidationError(f"annotation group {index} has a duplicate or empty id")
        seen_group_ids.add(group_id)
        room = str(raw_group.get("room") or raw_group.get("room_id") or "").strip()
        if not room:
            raise ManualPerceptionValidationError(f"annotation group {group_id!r} has no room")
        raw_ids = raw_group.get("objects")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ManualPerceptionValidationError(
                f"annotation group {group_id!r} must contain visible objects"
            )
        object_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        if len(set(object_ids)) != len(object_ids):
            raise ManualPerceptionValidationError(
                f"annotation group {group_id!r} contains duplicate object IDs"
            )
        if any(object_id not in visible_object_ids for object_id in object_ids):
            raise ManualPerceptionValidationError(
                f"annotation group {group_id!r} references an object absent from this frame"
            )
        overlap = claimed_objects.intersection(object_ids)
        if overlap:
            raise ManualPerceptionValidationError(
                f"annotation object(s) assigned to multiple groups: {', '.join(sorted(overlap))}"
            )
        claimed_objects.update(object_ids)
        groups.append(
            {
                "id": group_id,
                "name": str(
                    raw_group.get("name")
                    or raw_group.get("group_name")
                    or raw_group.get("caption")
                    or "current_timestep"
                ).strip(),
                "room": room,
                "objects": object_ids,
            }
        )
    if claimed_objects != visible_object_ids:
        missing = visible_object_ids - claimed_objects
        raise ManualPerceptionValidationError(
            "every visible object must belong to exactly one current-frame group: "
            + ", ".join(sorted(missing))
        )
    return groups


__all__ = [
    "MANUAL_OBJECT_CATALOG_SCHEMA_VERSION",
    "MANUAL_PERCEPTION_FILE_SUFFIX",
    "MANUAL_PERCEPTION_SCHEMA_VERSION",
    "MANUAL_IDENTITY_MODE_NON_VISUAL",
    "MANUAL_IDENTITY_MODE_SIMULATOR_OBJECT",
    "MANUAL_IDENTITY_MODE_UNVERIFIED",
    "ManualPerceptionValidationError",
    "TASK_ENTITY_NOT_VISIBLE",
    "TASK_ENTITY_NOT_VISUAL",
    "TASK_ENTITY_VISIBLE",
    "TASK_ENTITY_VISIBILITY_VALUES",
    "build_manual_object_catalog",
    "build_manual_annotation_template",
    "bind_manual_catalog_to_environment",
    "build_manual_perception_prompt",
    "catalog_sha256",
    "canonical_json_bytes",
    "catalog_object_maps",
    "load_json_object",
    "rgb_sha256",
    "sha256_file",
    "task_entity_ids",
    "task_entity_name",
    "validate_catalog_environment_bindings",
    "validate_manual_annotation",
]
