from og_ego_prim.domain import StateChange
from og_ego_prim.object_model import ObjectRegistry
from og_ego_prim.scene_graph.global_state import GlobalSceneGraphAccumulator
from og_ego_prim.scene_graph.manual_current_frame import manual_current_frame_perception_result
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater
from og_ego_prim.cli.headless_manual_physical_session import PersistentPhysicalSession
from og_ego_prim.scheduler import ScheduledProcess, build_scheduler
from og_ego_prim.primitives.starter_primitives import StarterPrimitiveController


def test_global_scene_checkpoint_round_trip_preserves_history_and_relations():
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    perception = manual_current_frame_perception_result(
        {
            "objects": [
                {"caption": "bottle", "id": "bottle.n.01_1", "room": "kitchen_0"},
                {"caption": "table", "id": "table.n.01_1", "room": "kitchen_0"},
            ],
            "relations": [
                {"source": "bottle.n.01_1", "target": "table.n.01_1", "relation": "on"}
            ],
        },
        frame_index=3,
    )
    accumulator = GlobalSceneGraphAccumulator()
    accumulator.merge_current_frame(updater._snapshot_from_result(perception, None, False, True))
    accumulator.apply_successful_action("navigate_to(bottle.n.01_1)")

    restored = GlobalSceneGraphAccumulator()
    restored.load_state(accumulator.to_state())
    summary = restored.snapshot().to_dict()["summary"]

    assert summary["objects"] == 2
    assert summary["relations"] == 1
    assert summary["action_history"] == ["navigate_to(bottle.n.01_1)"]


def test_object_registry_checkpoint_round_trip_preserves_aliases_and_states():
    registry = ObjectRegistry()
    registry.upsert(
        "water_bottle.n.01_1",
        canonical_name="water_bottle",
        aliases=("hot_bottle",),
        states={"held_by_robot": True},
        room_id="kitchen_0",
    )

    restored = ObjectRegistry()
    restored.load_dict(registry.to_dict())

    record = restored.require("hot_bottle")
    assert record.entity_id == "water_bottle.n.01_1"
    assert record.states["held_by_robot"] is True


def test_no_current_frame_updates_preserves_existing_visibility():
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    accumulator = GlobalSceneGraphAccumulator()
    initial = manual_current_frame_perception_result(
        {
            "objects": [
                {"caption": "bottle", "id": "bottle.n.01_1", "room": "kitchen_0"},
            ],
        },
        frame_index=1,
    )
    accumulator.merge_current_frame(updater._snapshot_from_result(initial, None, False, True))
    no_updates = manual_current_frame_perception_result(
        {
            "annotation_mode": "no_current_frame_updates",
            "objects": [],
            "relations": [],
        },
        frame_index=2,
    )

    summary = accumulator.merge_current_frame(
        updater._snapshot_from_result(no_updates, None, False, True)
    ).to_dict()

    assert summary["rooms"][0]["nodes"][0]["is_vis"] is True


def test_checkpoint_held_object_falls_back_to_registry_state():
    payload = {
        "object_registry": {
            "objects": [
                {"entity_id": "water_bottle.n.01_1", "states": {"held_by_robot": True}},
            ]
        }
    }

    assert (
        PersistentPhysicalSession._checkpoint_held_object_id(payload)
        == "water_bottle.n.01_1"
    )


def test_llm_log_path_preserves_retries(tmp_path):
    session = object.__new__(PersistentPhysicalSession)
    session.session_dir = tmp_path

    assert session._llm_log_path(24).name == "llm_000024.txt"

    (tmp_path / "llm_000024.txt").touch()
    assert session._llm_log_path(24).name == "llm_000024_retry_01.txt"

    (tmp_path / "llm_000024_retry_01.txt").touch()
    assert session._llm_log_path(24).name == "llm_000024_retry_02.txt"

    (tmp_path / "llm_000024.txt").unlink()
    assert session._llm_log_path(24).name == "llm_000024_retry_02.txt"


def test_symbolic_carry_diagnostics_are_json_serializable():
    controller = object.__new__(StarterPrimitiveController)
    controller._symbolic_carry_active = lambda: True
    controller._symbolic_carry_state = {
        "obj": type(
            "Object",
            (),
            {
                "name": "water_bottle",
                "get_position_orientation": lambda self: (
                    __import__("torch").tensor([1.0, 2.0, 3.0]),
                    __import__("torch").tensor([0.0, 0.0, 0.0, 1.0]),
                ),
            },
        )(),
        "arm": "arm",
        "eef_to_obj_pos": __import__("torch").zeros(3),
        "eef_to_obj_orn": __import__("torch").tensor([0.0, 0.0, 0.0, 1.0]),
    }
    controller.robot = type(
        "Robot",
        (),
        {
            "eef_links": {
                "arm": type(
                    "Link",
                    (),
                    {
                        "get_position_orientation": lambda self: (
                            __import__("torch").tensor([1.0, 2.0, 3.0]),
                            __import__("torch").tensor([0.0, 0.0, 0.0, 1.0]),
                        )
                    },
                )()
            }
        },
    )()

    import json

    json.dumps(controller.symbolic_carry_diagnostics())


def test_symbolic_carry_state_presence_tracks_retained_controller_state():
    controller = object.__new__(StarterPrimitiveController)
    controller._symbolic_carry_state = None

    assert controller.has_symbolic_carry_state() is False

    controller._symbolic_carry_state = {"obj": object()}

    assert controller.has_symbolic_carry_state() is True


def test_global_scene_projects_explicit_scheduler_state_changes():
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    perception = manual_current_frame_perception_result(
        {
            "objects": [
                {
                    "caption": "water_bottle",
                    "id": "water_bottle.n.01_1",
                    "room": "kitchen_0",
                },
            ],
        },
        frame_index=1,
    )
    accumulator = GlobalSceneGraphAccumulator()
    accumulator.merge_current_frame(updater._snapshot_from_result(perception, None, False, True))
    snapshot = accumulator.apply_state_changes(
        (
            StateChange(
                step=2,
                entity_id="water_bottle.n.01_1",
                key="heated",
                old=None,
                new=True,
                source="scheduler_derived",
            ),
            StateChange(
                step=2,
                entity_id="water_bottle.n.01_1",
                key="cooked",
                old=None,
                new=True,
                source="scheduler_derived",
            ),
        )
    ).to_dict()
    node = snapshot["rooms"][0]["nodes"][0]

    assert node["states"]["heated"] is True
    assert node["states"]["cooked"] is True


def test_legacy_registry_backfill_does_not_override_action_state():
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    perception = manual_current_frame_perception_result(
        {
            "objects": [
                {"caption": "microwave", "id": "microwave.n.02_1", "room": "kitchen_0"},
            ],
        },
        frame_index=1,
    )
    accumulator = GlobalSceneGraphAccumulator()
    accumulator.merge_current_frame(updater._snapshot_from_result(perception, None, False, True))
    accumulator._nodes["obj_0001"]["states"] = {"open": True, "toggled_on": False}
    registry = ObjectRegistry()
    registry.upsert("microwave.n.02_1", states={"open": False, "toggled_on": True})

    node = accumulator.apply_missing_object_registry_states(registry).to_dict()["rooms"][0]["nodes"][0]

    assert node["states"] == {"open": True, "toggled_on": False}


def test_grasp_removes_stale_move_relation_states():
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    accumulator = GlobalSceneGraphAccumulator()
    perception = manual_current_frame_perception_result(
        {
            "objects": [
                {"caption": "bottle", "id": "bottle.n.01_1", "room": "kitchen_0"},
                {"caption": "table", "id": "table.n.01_1", "room": "kitchen_0"},
            ],
            "relations": [
                {"source": "bottle.n.01_1", "target": "table.n.01_1", "relation": "on"}
            ],
        },
        frame_index=1,
    )
    accumulator.merge_current_frame(updater._snapshot_from_result(perception, None, False, True))
    bottle_id = accumulator._resolve_action_entity("bottle.n.01_1")
    accumulator._nodes[bottle_id]["states"]["relation:on:table.n.01_1"] = True

    accumulator.apply_successful_action(type("Action", (), {
        "name": "GRASP",
        "object_id": "bottle.n.01_1",
        "target_id": None,
        "to_legacy_plan": lambda self: "grasp(bottle.n.01_1)",
    })())

    states = accumulator._nodes[bottle_id]["states"]
    assert states["held_by_robot"] is True
    assert "relation:on:table.n.01_1" not in states


def test_scheduler_restore_skips_processes_disabled_by_current_configuration():
    scheduler = build_scheduler(
        {"include_builtins": True, "processes": {"cooling": {"enabled": False}}}
    )
    scheduler.load_pending(
        (
            ScheduledProcess(
                process_id="cooling:hot-bottle",
                process_type="cooling",
                entity_ids=("water_bottle.n.01_1",),
                start_step=1,
                ready_step=61,
            ),
        )
    )

    assert scheduler.pending == ()


def test_scheduler_restore_reconciles_pending_duration_with_current_config():
    scheduler = build_scheduler(
        {
            "include_builtins": True,
            "processes": {"cooling": {"duration_steps": 7200}},
        }
    )
    scheduler.load_pending(
        (
            ScheduledProcess(
                process_id="cooling:hot-bottle",
                process_type="cooling",
                entity_ids=("water_bottle.n.01_1",),
                start_step=2423,
                ready_step=2483,
            ),
        )
    )

    assert scheduler.pending[0].ready_step == 9623


def test_scheduler_restore_reconciles_pending_blocking_actions_with_current_config():
    scheduler = build_scheduler(
        {
            "include_builtins": True,
            "processes": {"cooling": {"blocking_actions": []}},
        }
    )
    scheduler.load_pending(
        (
            ScheduledProcess(
                process_id="cooling:hot-bottle",
                process_type="cooling",
                entity_ids=("water_bottle.n.01_1",),
                start_step=1,
                ready_step=61,
                blocking_actions=("GRASP",),
            ),
        )
    )

    assert scheduler.pending[0].blocking_actions == ()
