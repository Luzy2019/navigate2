"""Fail-closed manual perception backend for current native RGB frames.

This backend replaces SAMJAM / SAM2 / UniGoal recognition only.  It emits the
same ``PerceptionResult`` contract consumed by ``PerceptionSceneGraphUpdater``
so canonical node construction, edge construction, state diffs, object
registry updates, and scheduler integration stay in the normal runtime path.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.scene_graph.manual_perception import (
    MANUAL_PERCEPTION_FILE_SUFFIX,
    ManualPerceptionValidationError,
    catalog_sha256,
    catalog_object_maps,
    load_json_object,
    rgb_sha256,
    sha256_file,
    validate_catalog_environment_bindings,
    validate_manual_annotation,
)
from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter
from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)


MANUAL_CORRECTED_BACKEND = "manual_corrected"


class ManualCorrectedPerceptionError(RuntimeError):
    """Raised when current-frame manual perception evidence is unavailable."""


class ManualCorrectedBackend:
    """Load approved exact-frame annotations without SAM2 or UniGoal mapping."""

    name = MANUAL_CORRECTED_BACKEND

    def __init__(
        self,
        sensor_name: Optional[str] = None,
        scene_graph_config: Optional[SceneGraphConfig] = None,
    ) -> None:
        self.scene_graph_config = scene_graph_config or SceneGraphConfig()
        self.sensor_name = sensor_name
        self.adapter = ISBenchObservationAdapter(sensor_name=sensor_name)
        self.task_instruction: Optional[str] = None
        self.task_categories: Tuple[str, ...] = ()
        self.task_entity_ids: Tuple[str, ...] = ()
        self._catalog: Optional[Dict[str, Any]] = None
        self._catalog_digest: Optional[str] = None
        self._annotation_paths: Dict[int, Path] = {}
        self._approved_annotations: Dict[int, Dict[str, Any]] = {}
        self._last_result: Optional[PerceptionResult] = None

    def reset(self, env: Any) -> None:
        if not self.task_entity_ids:
            raise ManualCorrectedPerceptionError(
                "manual_corrected requires exact active task entity IDs before reset"
            )
        self.adapter.reset()
        self._last_result = None
        self._catalog = self._load_catalog()
        self._catalog_digest = catalog_sha256(self._catalog)
        try:
            validate_catalog_environment_bindings(
                self._catalog,
                env,
                expected_task_entity_ids=self.task_entity_ids,
            )
        except ManualPerceptionValidationError as exc:
            raise ManualCorrectedPerceptionError(
                f"manual_corrected catalog does not match the live simulator: {exc}"
            ) from exc
        self._approved_annotations, self._annotation_paths = self._load_annotations(
            catalog=self._catalog,
            catalog_digest=self._catalog_digest,
        )

    def observe(self, env: Any) -> FrameObservation:
        return self.adapter.observe(env)

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        annotation = self._annotation_for_frame(frame)
        result = self._result_from_annotation(frame, annotation)
        self._last_result = result
        return result

    def update_memory(self, result: PerceptionResult) -> PerceptionResult:
        if result.backend != self.name or result is not self._last_result:
            raise ManualCorrectedPerceptionError(
                "manual_corrected.update_memory accepts only its current detect result"
            )
        result.metadata["memory_update"] = "identity_no_unigoal_mapping"
        result.metadata["unigoal_mapping_used"] = False
        result.metadata["sam2_tracking_used"] = False
        return result

    def set_task_instruction(self, instruction: Optional[str]) -> None:
        self.task_instruction = str(instruction or "").strip() or None

    def set_task_categories(self, categories: Iterable[str]) -> None:
        self.task_categories = tuple(
            dict.fromkeys(str(value).strip() for value in categories if str(value).strip())
        )

    def set_task_entities(self, entity_ids: Iterable[str]) -> None:
        self.task_entity_ids = tuple(
            dict.fromkeys(str(value).strip() for value in entity_ids if str(value).strip())
        )

    def _load_catalog(self) -> Dict[str, Any]:
        catalog_path = self._catalog_path()
        if not catalog_path.is_file():
            raise ManualCorrectedPerceptionError(
                f"manual_corrected catalog is missing: {catalog_path}"
            )
        try:
            catalog = load_json_object(catalog_path)
            object_by_id, _, _ = catalog_object_maps(
                catalog,
                expected_task_entity_ids=self.task_entity_ids,
            )
        except ManualPerceptionValidationError as exc:
            raise ManualCorrectedPerceptionError(
                f"invalid manual_corrected catalog {catalog_path}: {exc}"
            ) from exc
        unbound_object_ids = sorted(
            object_id
            for object_id, item in object_by_id.items()
            if item["entity_id"] is None
        )
        if unbound_object_ids:
            raise ManualCorrectedPerceptionError(
                "manual_corrected catalog must not contain unbound non-task objects: "
                + ", ".join(unbound_object_ids)
            )
        return catalog

    def _load_annotations(
        self,
        *,
        catalog: Mapping[str, Any],
        catalog_digest: str,
    ) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Path]]:
        annotations_dir = self._annotations_dir()
        if not annotations_dir.is_dir():
            raise ManualCorrectedPerceptionError(
                f"manual_corrected annotation directory is missing: {annotations_dir}"
            )
        paths = sorted(
            path
            for path in annotations_dir.glob(f"*{MANUAL_PERCEPTION_FILE_SUFFIX}")
            if path.is_file()
        )
        if not paths:
            raise ManualCorrectedPerceptionError(
                "manual_corrected annotation directory has no approved frame annotations: "
                f"{annotations_dir}"
            )
        annotations: Dict[int, Dict[str, Any]] = {}
        annotation_paths: Dict[int, Path] = {}
        for path in paths:
            try:
                annotation = validate_manual_annotation(
                    load_json_object(path),
                    catalog=catalog,
                    expected_task_entity_ids=self.task_entity_ids,
                    expected_catalog_sha256=catalog_digest,
                )
            except ManualPerceptionValidationError as exc:
                raise ManualCorrectedPerceptionError(
                    f"invalid manual_corrected annotation {path}: {exc}"
                ) from exc
            frame_index = int(annotation["frame_index"])
            if frame_index in annotations:
                raise ManualCorrectedPerceptionError(
                    "manual_corrected has multiple approved annotations for "
                    f"frame {frame_index}: {annotation_paths[frame_index]} and {path}"
                )
            annotations[frame_index] = annotation
            annotation_paths[frame_index] = path
        self._validate_annotation_ledger(annotations, annotation_paths)
        return annotations, annotation_paths

    @staticmethod
    def _validate_annotation_ledger(
        annotations: Mapping[int, Mapping[str, Any]],
        annotation_paths: Mapping[int, Path],
    ) -> None:
        """Require one reviewed, hash-linked identity sequence for all frames."""

        previous_index: Optional[int] = None
        previous_path: Optional[Path] = None
        sequence_id: Optional[str] = None
        for frame_index in sorted(annotations):
            annotation = annotations[frame_index]
            ledger = annotation["identity_ledger"]
            current_sequence = str(ledger["sequence_id"])
            if sequence_id is None:
                sequence_id = current_sequence
                if ledger["previous_frame_index"] is not None:
                    raise ManualCorrectedPerceptionError(
                        "first approved manual frame must not reference a previous frame"
                    )
                if ledger["previous_annotation_sha256"] is not None:
                    raise ManualCorrectedPerceptionError(
                        "first approved manual frame must not reference a previous annotation hash"
                    )
            else:
                if current_sequence != sequence_id:
                    raise ManualCorrectedPerceptionError(
                        "approved manual annotations must use one identity_ledger.sequence_id"
                    )
                if previous_index is None or previous_path is None:
                    raise AssertionError("manual annotation ledger has no previous frame")
                if frame_index != previous_index + 1:
                    raise ManualCorrectedPerceptionError(
                        "approved manual annotation frame indices must be consecutive"
                    )
                if ledger["previous_frame_index"] != previous_index:
                    raise ManualCorrectedPerceptionError(
                        f"manual frame {frame_index} does not link to previous frame {previous_index}"
                    )
                if ledger["previous_annotation_sha256"] != sha256_file(previous_path):
                    raise ManualCorrectedPerceptionError(
                        f"manual frame {frame_index} previous annotation hash does not match frame {previous_index}"
                    )
            previous_index = frame_index
            previous_path = annotation_paths[frame_index]

    def _annotation_for_frame(self, frame: FrameObservation) -> Dict[str, Any]:
        frame_index = int(frame.frame_index)
        annotation = self._approved_annotations.get(frame_index)
        if annotation is None:
            raise ManualCorrectedPerceptionError(
                "manual_corrected has no approved annotation for current "
                f"frame_index={frame_index}; it will not reuse another frame"
            )
        expected_digest = annotation["rgb_sha256"]
        actual_digest = rgb_sha256(frame.rgb)
        if actual_digest != expected_digest:
            raise ManualCorrectedPerceptionError(
                "manual_corrected RGB hash mismatch for current "
                f"frame_index={frame_index}; annotation={expected_digest} "
                f"current={actual_digest}"
            )
        actual_shape = tuple(int(value) for value in frame.rgb.shape[:3])
        if actual_shape != tuple(annotation["rgb_shape"]):
            raise ManualCorrectedPerceptionError(
                "manual_corrected RGB shape mismatch for current "
                f"frame_index={frame_index}; annotation={annotation['rgb_shape']} "
                f"current={list(actual_shape)}"
            )
        expected_sensor = annotation.get("sensor_name")
        if expected_sensor and str(frame.sensor_name) != expected_sensor:
            raise ManualCorrectedPerceptionError(
                "manual_corrected sensor mismatch for current "
                f"frame_index={frame_index}; annotation={expected_sensor!r} "
                f"current={frame.sensor_name!r}"
            )
        return annotation

    def _result_from_annotation(
        self,
        frame: FrameObservation,
        annotation: Mapping[str, Any],
    ) -> PerceptionResult:
        objects = [self._perceived_object(item, frame.frame_index) for item in annotation["objects"]]
        relations = [
            PerceivedRelation(
                source_id=str(item["source_id"]),
                target_id=str(item["target_id"]),
                relation=str(item["relation"]),
                confidence=1.0,
                source="manual_corrected",
            )
            for item in annotation["relations"]
        ]
        rooms = self._room_graph(annotation["objects"])
        groups = [
            {
                "id": str(item["id"]),
                "name": str(item["name"]),
                "room": str(item["room"]),
                "objects": list(item["objects"]),
            }
            for item in annotation["groups"]
        ]
        annotation_path = self._annotation_paths[int(frame.frame_index)]
        return PerceptionResult(
            backend=self.name,
            frame_index=int(frame.frame_index),
            objects=objects,
            relations=relations,
            scene_graph={
                "source": "manual_corrected_perception",
                "current_timestep_only": True,
                "sam2_merged_into_graph": False,
                "unigoal_mapping_used": False,
            },
            room_graph={"rooms": rooms},
            group_graph={"groups": groups},
            metadata={
                "manual_corrected": True,
                "current_timestep_only": True,
                "manual_annotation_path": str(annotation_path),
                "manual_annotation_sha256": sha256_file(annotation_path),
                "manual_catalog_path": str(self._catalog_path()),
                "manual_catalog_sha256": self._catalog_digest,
                "source_frame": {
                    "frame_index": int(frame.frame_index),
                    "rgb_sha256": annotation["rgb_sha256"],
                    "rgb_shape": list(annotation["rgb_shape"]),
                    "sensor_name": annotation.get("sensor_name"),
                },
                "coverage": deepcopy(dict(annotation["coverage"])),
                "identity_ledger": deepcopy(dict(annotation["identity_ledger"])),
                "sam2_reference": deepcopy(dict(annotation["sam2_reference"])),
                "sam2_reference_only": bool(annotation["sam2_reference"]),
                "sam2_merged_into_graph": False,
                "sam2_tracking_used": False,
                "unigoal_mapping_used": False,
                "point_cloud_used": False,
            },
        )

    @staticmethod
    def _perceived_object(item: Mapping[str, Any], frame_index: int) -> PerceivedObject:
        entity_id = item.get("entity_id")
        attributes = {
            "uid": int(item["uid"]),
            "entity_id": entity_id,
            "normalized_label": str(item["name"]),
            "lifelong_label": str(item["name"]),
            "is_vis": True,
            "currently_visible": True,
            "is_coarse": bool(item["is_coarse"]),
            "states": deepcopy(dict(item["states"])),
            "hazard": deepcopy(dict(item["hazard"])),
            "caption": item.get("caption"),
            "room": str(item["room_id"]),
            "last_seen_frame": int(frame_index),
            "source_ids": {
                "manual_catalog_object_id": str(item["object_id"]),
                "task_entity_id": entity_id,
                "simulator_name": item["simulator_name"],
            },
            "simulator_name": item["simulator_name"],
        }
        return PerceivedObject(
            object_id=str(item["object_id"]),
            name=str(item["name"]),
            category=str(item["category"]),
            bbox=None if item["bbox"] is None else list(item["bbox"]),
            position=None if item["position"] is None else list(item["position"]),
            room_id=str(item["room_id"]),
            confidence=1.0,
            attributes=attributes,
        )

    @staticmethod
    def _room_graph(objects: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
        room_objects: Dict[str, list[str]] = {}
        for item in objects:
            room_objects.setdefault(str(item["room_id"]), []).append(str(item["object_id"]))
        return [
            {"id": room_id, "name": room_id, "caption": room_id, "objects": object_ids}
            for room_id, object_ids in sorted(room_objects.items())
        ]

    def _annotations_dir(self) -> Path:
        value = self.scene_graph_config.option("manual_annotation_dir")
        if not value:
            value = self.scene_graph_config.option("manual_perception_dir")
        if not value:
            raise ManualCorrectedPerceptionError(
                "scene_graph.backend_options.manual_annotation_dir is required "
                "for manual_corrected"
            )
        return Path(str(value)).expanduser().resolve()

    def _catalog_path(self) -> Path:
        value = self.scene_graph_config.option("manual_catalog_path")
        if not value:
            value = self.scene_graph_config.option("manual_object_catalog")
        if value:
            return Path(str(value)).expanduser().resolve()
        return self._annotations_dir().parent / "manual_object_catalog.json"


__all__ = [
    "MANUAL_CORRECTED_BACKEND",
    "ManualCorrectedBackend",
    "ManualCorrectedPerceptionError",
]
