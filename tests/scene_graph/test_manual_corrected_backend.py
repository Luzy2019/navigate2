"""CPU-only contracts for approved per-frame manual perception."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from og_ego_prim.agent_runtime import AgentRuntimeController, RuntimeComponents
from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.object_model import ObjectRegistry
from og_ego_prim.scene_graph.manual_perception import (
    build_manual_object_catalog,
    catalog_sha256,
    rgb_sha256,
)
from og_ego_prim.scene_graph.backends.manual_corrected import (
    ManualCorrectedBackend,
    ManualCorrectedPerceptionError,
)
from og_ego_prim.scene_graph.perception import FrameObservation
from og_ego_prim.scene_graph import perception_scene_graph
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater
from og_ego_prim.events import NullEventSink
from og_ego_prim.scheduler import (
    ManualSimulationClock,
    ScheduledProcess,
    Scheduler,
)


TASK_ENTITY_IDS = (
    "water_bottle.n.01_1",
    "water_bottle.n.01_2",
)


def _task_config() -> dict[str, object]:
    return {"planning_context": {"object_list": list(TASK_ENTITY_IDS)}}


def _frame(*, frame_index: int = 0, rgb: np.ndarray | None = None) -> FrameObservation:
    rgb = (
        np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3) + frame_index
        if rgb is None
        else rgb
    )
    return FrameObservation(
        frame_index=frame_index,
        rgb=rgb,
        depth=None,
        intrinsics=None,
        camera_pose=None,
        robot_position=None,
        sensor_name="robot_rgb",
    )


def _write_approved_annotation_set(
    tmp_path: Path,
    *,
    frames: tuple[FrameObservation, ...],
) -> tuple[Path, Path]:
    annotations_dir = tmp_path / "annotations"
    annotations_dir.mkdir()
    catalog = build_manual_object_catalog(_task_config())
    catalog["identity_bindings_reviewed"] = True
    catalog_path = tmp_path / "manual_object_catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    object_ids = {
        entity_id: f"task:{entity_id}" for entity_id in TASK_ENTITY_IDS
    }
    for frame in frames:
        annotation = {
            "schema_version": "isbench.manual_corrected_perception.v1",
            "status": "approved",
            "catalog_sha256": catalog_sha256(catalog),
            "source_frame": {
                "frame_index": frame.frame_index,
                "rgb_sha256": rgb_sha256(frame.rgb),
                "rgb_shape": list(frame.rgb.shape),
                "sensor_name": frame.sensor_name,
            },
            "coverage": {
                "task_entity_visibility": {
                    entity_id: "visible" for entity_id in TASK_ENTITY_IDS
                },
                "visible_task_entities_complete": True,
                "objects_human_approved": True,
                "identity_bindings_reviewed": True,
                "relations_human_approved": True,
            },
            "objects": [
                {
                    "object_id": object_ids["water_bottle.n.01_1"],
                    "entity_id": "water_bottle.n.01_1",
                    "room_id": "kitchen",
                    "bbox": [frame.frame_index, 0, frame.frame_index + 2, 3],
                    "caption": "left water bottle",
                    "states": {},
                    "hazard": {},
                },
                {
                    "object_id": object_ids["water_bottle.n.01_2"],
                    "entity_id": "water_bottle.n.01_2",
                    "room_id": "kitchen",
                    "bbox": [3, frame.frame_index, 6, frame.frame_index + 3],
                    "caption": "right water bottle",
                    "states": {},
                    "hazard": {},
                },
            ],
            "relations": [
                {
                    "source_id": object_ids["water_bottle.n.01_1"],
                    "target_id": object_ids["water_bottle.n.01_2"],
                    "relation": "near",
                }
            ],
            "sam2_reference": {},
        }
        annotation_path = annotations_dir / (
            f"frame_{frame.frame_index:06d}.manual_perception.json"
        )
        annotation_path.write_text(json.dumps(annotation), encoding="utf-8")
    return annotations_dir, catalog_path


def _backend(annotations_dir: Path, catalog_path: Path) -> ManualCorrectedBackend:
    config = SceneGraphConfig(
        backend="manual_corrected",
        backend_options={
            "manual_annotation_dir": str(annotations_dir),
            "manual_catalog_path": str(catalog_path),
        },
    )
    backend = ManualCorrectedBackend(
        sensor_name="robot_rgb",
        scene_graph_config=config,
    )
    backend.set_task_entities(TASK_ENTITY_IDS)
    backend.reset(env=object())
    return backend


def _snapshot_from_current_frame(
    backend: ManualCorrectedBackend,
    frame: FrameObservation,
):
    result = backend.update_memory(backend.detect(frame))
    updater = PerceptionSceneGraphUpdater(
        backend_name="disabled",
        scene_graph_config=SceneGraphConfig(backend="disabled"),
    )
    return result, updater._snapshot_from_result(
        result,
        context=None,
        skipped=False,
        force=True,
    )


def _annotation_path(annotations_dir: Path, frame_index: int = 0) -> Path:
    return annotations_dir / f"frame_{frame_index:06d}.manual_perception.json"


def test_approved_current_frame_builds_v2_nodes_and_edge(tmp_path: Path) -> None:
    frame = _frame()
    annotations_dir, catalog_path = _write_approved_annotation_set(
        tmp_path, frames=(frame,)
    )

    result, snapshot = _snapshot_from_current_frame(
        _backend(annotations_dir, catalog_path), frame
    )
    payload = snapshot.to_dict()
    nodes = payload["rooms"][0]["nodes"]
    edges = payload["rooms"][0]["edges"]

    assert result.backend == "manual_corrected"
    assert result.metadata["unigoal_mapping_used"] is False
    assert payload["schema_version"] == "isbench.scene_graph.v2"
    assert {node["entity_id"] for node in nodes} == set(TASK_ENTITY_IDS)
    assert len(edges) == 1
    assert edges[0]["type"] == "near"
    assert {edges[0]["source"], edges[0]["target"]} == {
        node["id"] for node in nodes
    }


@pytest.mark.parametrize(
    ("frame_index", "rgb", "message"),
    [
        (1, None, "no approved annotation"),
        (0, np.zeros((4, 6, 3), dtype=np.uint8), "RGB hash mismatch"),
    ],
)
def test_wrong_frame_or_rgb_hash_fails_closed(
    tmp_path: Path,
    frame_index: int,
    rgb: np.ndarray | None,
    message: str,
) -> None:
    approved_frame = _frame()
    annotations_dir, catalog_path = _write_approved_annotation_set(
        tmp_path, frames=(approved_frame,)
    )
    backend = _backend(annotations_dir, catalog_path)

    with pytest.raises(ManualCorrectedPerceptionError, match=message):
        backend.detect(_frame(frame_index=frame_index, rgb=rgb))


def test_two_same_category_instances_remain_distinct_across_frames(
    tmp_path: Path,
) -> None:
    frames = (_frame(frame_index=0), _frame(frame_index=1))
    annotations_dir, catalog_path = _write_approved_annotation_set(
        tmp_path, frames=frames
    )
    backend = _backend(annotations_dir, catalog_path)
    registry = ObjectRegistry()
    canonical_ids_by_entity: dict[str, str] = {}

    for frame in frames:
        _, snapshot = _snapshot_from_current_frame(backend, frame)
        nodes = snapshot.to_dict()["rooms"][0]["nodes"]
        entity_to_node_id = {
            node["entity_id"]: node["id"]
            for node in nodes
        }

        assert set(entity_to_node_id) == set(TASK_ENTITY_IDS)
        assert len(set(entity_to_node_id.values())) == 2
        assert all(node_id.startswith("obj_") for node_id in entity_to_node_id.values())
        if canonical_ids_by_entity:
            assert entity_to_node_id == canonical_ids_by_entity
        else:
            canonical_ids_by_entity = entity_to_node_id

        registry.update_from_scene_graph(snapshot)
        assert {record.entity_id for record in registry.snapshot()} == set(TASK_ENTITY_IDS)
        assert registry.resolve("water_bottle", strict=False) is None

    assert not any(
        record.entity_id.startswith("obj_") for record in registry.snapshot()
    )


def test_wrong_explicit_task_binding_fails_closed(tmp_path: Path) -> None:
    frame = _frame()
    annotations_dir, catalog_path = _write_approved_annotation_set(
        tmp_path, frames=(frame,)
    )
    annotation_path = _annotation_path(annotations_dir)
    annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
    annotation["objects"][0]["entity_id"] = "water_bottle.n.01_2"
    annotation_path.write_text(json.dumps(annotation), encoding="utf-8")

    backend = ManualCorrectedBackend(
        sensor_name="robot_rgb",
        scene_graph_config=SceneGraphConfig(
            backend="manual_corrected",
            backend_options={
                "manual_annotation_dir": str(annotations_dir),
                "manual_catalog_path": str(catalog_path),
            },
        ),
    )
    backend.set_task_entities(TASK_ENTITY_IDS)

    with pytest.raises(ManualCorrectedPerceptionError, match="immutable entity binding"):
        backend.reset(env=object())


def test_current_frame_result_uses_updater_and_scheduler_exact_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = (_frame(frame_index=0), _frame(frame_index=1))
    annotations_dir, catalog_path = _write_approved_annotation_set(
        tmp_path, frames=frames
    )
    config = SceneGraphConfig(
        backend="manual_corrected",
        update_every=1,
        backend_options={
            "manual_annotation_dir": str(annotations_dir),
            "manual_catalog_path": str(catalog_path),
        },
    )
    backend = ManualCorrectedBackend(sensor_name="robot_rgb", scene_graph_config=config)
    observed_frames = iter(frames)
    monkeypatch.setattr(backend, "observe", lambda env: next(observed_frames))
    monkeypatch.setattr(
        perception_scene_graph,
        "build_perception_backend",
        lambda backend_name, sensor_name, scene_graph_config: backend,
    )

    updater = PerceptionSceneGraphUpdater(scene_graph_config=config)
    updater.set_task_entities(TASK_ENTITY_IDS)
    initial_snapshot = updater.reset(env=object())
    current_snapshot = updater.update()

    assert initial_snapshot.metadata["perception_backend"] == "manual_corrected"
    assert current_snapshot.metadata["frame_index"] == 1
    assert current_snapshot.metadata["backend_metadata"]["memory_update"] == "identity_no_unigoal_mapping"

    scheduler = Scheduler(clock=ManualSimulationClock())
    process = ScheduledProcess(
        process_id="test:cool-bottle",
        process_type="test",
        entity_ids=("water_bottle.n.01_2",),
        start_step=0,
        ready_step=None,
    )
    scheduler.load_pending((process,))
    controller = AgentRuntimeController(
        RuntimeComponents(
            perception=updater,
            objects=ObjectRegistry(),
            scheduler=scheduler,
            event_sink=NullEventSink(),
        ),
        expose_cross_subtask_timers=False,
    )
    controller.observe(current_snapshot)

    assert "water_bottle.n.01_2" in controller.visible_entity_ids
    assert scheduler.filter_visibility(controller.visible_entity_ids) == (process,)


def test_manual_corrected_rejects_stale_update_frequency() -> None:
    with pytest.raises(ValueError, match="update_every=1"):
        PerceptionSceneGraphUpdater(
            scene_graph_config=SceneGraphConfig(
                backend="manual_corrected",
                update_every=2,
            )
        )
