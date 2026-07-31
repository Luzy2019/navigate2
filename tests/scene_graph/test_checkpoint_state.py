from og_ego_prim.domain import StateChange
from og_ego_prim.object_model import ObjectRegistry
from og_ego_prim.scene_graph.global_state import GlobalSceneGraphAccumulator
from og_ego_prim.scene_graph.manual_current_frame import manual_current_frame_perception_result
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater
from og_ego_prim.cli.headless_manual_physical_session import PersistentPhysicalSession
from og_ego_prim.scheduler import ScheduledProcess, build_scheduler
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from og_ego_prim.primitives.starter_primitives import (
    PhysicalStarterSemanticActionPrimitives,
    StarterSemanticActionPrimitiveSet,
)


StarterPrimitiveController = PhysicalStarterSemanticActionPrimitives


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


def test_legacy_place_checkpoint_focuses_previously_grasped_source():
    session = object.__new__(PersistentPhysicalSession)
    payload = {
        "session": {
            "completed_actions": [
                {"action": "navigate_to(lint_screen.n.01_2)"},
                {"action": "grasp(lint_screen.n.01_2)"},
                {"action": "navigate_to(washer.n.03_1)"},
                {"action": "place_on_top(washer.n.03_1)"},
                {"action": "done()"},
            ]
        }
    }

    assert (
        session._infer_restored_first_view_focus_entity(payload)
        == "lint_screen.n.01_2"
    )


def test_native_first_view_angular_error_uses_optical_camera_axes():
    import math
    import torch

    position = torch.zeros(3)
    orientation = torch.tensor([0.0, 0.0, 0.0, 1.0])
    centered = PhysicalStarterSemanticActionPrimitives._first_view_angular_error(
        position,
        orientation,
        torch.tensor([0.0, 0.0, -1.0]),
    )
    right = PhysicalStarterSemanticActionPrimitives._first_view_angular_error(
        position,
        orientation,
        torch.tensor([1.0, 0.0, -1.0]),
    )
    above = PhysicalStarterSemanticActionPrimitives._first_view_angular_error(
        position,
        orientation,
        torch.tensor([0.0, 1.0, -1.0]),
    )

    assert centered[:2] == (0.0, 0.0)
    assert math.isclose(right[0], math.pi / 4.0, abs_tol=1e-6)
    assert math.isclose(above[1], math.pi / 4.0, abs_tol=1e-6)


def _first_view_limit_controller(*, vertical_error, camera_tilt, max_steps=3):
    import torch

    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    controller.first_view_targeting = True
    controller.first_view_targeting_max_steps = max_steps
    controller.first_view_targeting_angular_tolerance = 0.05
    controller.first_view_targeting_max_joint_step = 0.25
    controller.first_view_targeting_settle_steps = 2
    controller.first_view_targeting_align_base = False
    controller.first_view_targeting_roll_tolerance = 0.05
    controller.first_view_targeting_max_base_yaw_change = 0.35
    controller.last_first_view_alignment = None
    controller._first_view_focus = None

    camera_controller = type(
        "CameraController",
        (),
        {
            "dof_idx": [0, 1],
            "use_delta_commands": False,
            "command_input_limits": (
                torch.tensor([-1.0, -1.0]),
                torch.tensor([1.0, 1.0]),
            ),
            "command_output_limits": None,
        },
    )()
    robot = type(
        "Robot",
        (),
        {
            "controllers": {"camera": camera_controller},
            "control_limits": {
                "position": (
                    torch.tensor([-1.0, -1.0]),
                    torch.tensor([1.0, 1.45]),
                ),
                "has_limit": torch.tensor([True, True]),
            },
            "get_joint_positions": lambda self: torch.tensor([0.0, camera_tilt]),
            "get_position_orientation": lambda self: (
                torch.zeros(3),
                torch.tensor([0.0, 0.0, 0.0, 1.0]),
            ),
        },
    )()
    controller.env = type("Environment", (), {"robots": [robot]})()
    sensor = type(
        "Sensor",
        (),
        {
            "intrinsic_matrix": torch.tensor(
                [
                    [256.0, 0.0, 256.0],
                    [0.0, 256.0, 256.0],
                    [0.0, 0.0, 1.0],
                ]
            ),
            "image_width": 512,
            "image_height": 512,
            "clipping_range": torch.tensor([0.01, 10.0]),
            "get_position_orientation": lambda self, frame: (
                torch.zeros(3),
                torch.tensor([0.0, 0.0, 0.0, 1.0]),
            ),
        },
    )()
    controller._native_first_view_sensor = lambda: ("eye", sensor)
    controller._first_view_angular_error = lambda *args: (
        0.0,
        vertical_error,
        1.0,
        1.0,
    )
    controller._first_view_roll_error = lambda *args: 0.0
    controller._camera_target_action = lambda target: target.clone()
    obj = type("Object", (), {"name": "low_target"})()
    return controller, obj, torch.tensor([0.0, 0.0, -1.0])


def test_native_first_view_accepts_visible_target_at_camera_joint_limit():
    controller, obj, target_point = _first_view_limit_controller(
        vertical_error=-0.1,
        camera_tilt=1.0,
    )

    actions = list(
        controller.center_first_view_on_object(
            obj,
            phase="post_navigation",
            target_point=target_point,
        )
    )

    assert len(actions) == 1
    assert controller.last_first_view_alignment["status"] == "visible_at_joint_limit"
    assert controller.last_first_view_alignment["joint_limit"]["blocked_axes"] == [
        False,
        True,
    ]
    assert controller.last_first_view_alignment["joint_limit"]["limit_source"] == (
        "camera_command_input_limits"
    )
    assert controller.last_first_view_alignment["joint_limit"]["raw_upper_limits"] == [
        1.0,
        1.45,
    ]
    assert controller.last_first_view_alignment["joint_limit"]["upper_limits"] == [
        1.0,
        1.0,
    ]
    assert controller.last_first_view_alignment["frustum"]["inside_frustum"] is True


def test_native_first_view_restore_accepts_prior_visible_joint_limit_focus():
    controller, obj, target_point = _first_view_limit_controller(
        vertical_error=-0.1,
        camera_tilt=1.0,
    )

    list(
        controller.center_first_view_on_object(
            obj,
            phase="checkpoint_restore",
            target_point=target_point,
        )
    )

    assert controller.last_first_view_alignment["status"] == "visible_at_joint_limit"


def test_native_first_view_rejects_target_outside_frustum_at_joint_limit():
    controller, obj, target_point = _first_view_limit_controller(
        vertical_error=-1.0,
        camera_tilt=1.0,
        max_steps=2,
    )

    try:
        list(
            controller.center_first_view_on_object(
                obj,
                phase="post_navigation",
                target_point=target_point,
            )
        )
    except ActionPrimitiveError:
        pass
    else:
        raise AssertionError("a target outside the native RGB frustum must fail")

    assert controller.last_first_view_alignment["status"] == "failed"
    assert controller.last_first_view_alignment["frustum"]["available"] is True
    assert controller.last_first_view_alignment["frustum"]["inside_image"] is False
    assert controller.last_first_view_alignment["frustum"]["inside_frustum"] is False


def test_native_first_view_does_not_relax_centering_before_joint_limit():
    controller, obj, target_point = _first_view_limit_controller(
        vertical_error=-0.1,
        camera_tilt=0.8,
        max_steps=1,
    )

    try:
        list(
            controller.center_first_view_on_object(
                obj,
                phase="post_navigation",
                target_point=target_point,
            )
        )
    except ActionPrimitiveError:
        pass
    else:
        raise AssertionError("in-frustum visibility must not bypass available centering motion")

    assert controller.last_first_view_alignment["status"] == "failed"
    assert (
        controller.last_first_view_alignment["joint_limit"][
            "all_residual_axes_blocked"
        ]
        is False
    )
    assert controller.last_first_view_alignment["frustum"] is None


def test_native_first_view_keeps_pre_grasp_centering_strict_at_joint_limit():
    controller, obj, target_point = _first_view_limit_controller(
        vertical_error=-0.1,
        camera_tilt=1.0,
        max_steps=1,
    )

    try:
        list(
            controller.center_first_view_on_object(
                obj,
                phase="pre_grasp",
                target_point=target_point,
            )
        )
    except ActionPrimitiveError:
        pass
    else:
        raise AssertionError("pre-grasp targeting must remain strictly centered")

    assert controller.last_first_view_alignment["status"] == "failed"
    assert controller.last_first_view_alignment["joint_limit"] is None
    assert controller.last_first_view_alignment["frustum"] is None


def test_native_first_view_settling_holds_current_camera_joints():
    import torch

    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    controller.first_view_targeting = True
    controller.first_view_targeting_max_steps = 8
    controller.first_view_targeting_angular_tolerance = 0.05
    controller.first_view_targeting_max_joint_step = 0.25
    controller.first_view_targeting_settle_steps = 2
    controller.first_view_targeting_align_base = False
    controller.first_view_targeting_roll_tolerance = 0.05
    controller.first_view_targeting_max_base_yaw_change = 0.35
    controller.last_first_view_alignment = None
    controller._first_view_focus = None

    camera_controller = type("CameraController", (), {"dof_idx": [0, 1]})()
    joint_positions = iter(
        (
            torch.tensor([0.0, 0.0]),
            torch.tensor([0.0, 0.0]),
            torch.tensor([0.2, 0.1]),
            torch.tensor([0.2, 0.1]),
            torch.tensor([0.2, 0.1]),
        )
    )
    robot = type(
        "Robot",
        (),
        {
            "controllers": {"camera": camera_controller},
            "get_joint_positions": lambda self: next(joint_positions),
            "get_position_orientation": lambda self: (
                torch.zeros(3),
                torch.tensor([0.0, 0.0, 0.0, 1.0]),
            ),
        },
    )()
    controller.env = type("Environment", (), {"robots": [robot]})()
    sensor = type(
        "Sensor",
        (),
        {
            "get_position_orientation": lambda self, frame: (
                torch.zeros(3),
                torch.tensor([0.0, 0.0, 0.0, 1.0]),
            )
        },
    )()
    errors = iter(
        (
            (0.2, 0.1, 1.0, 1.0),
            (0.01, -0.01, 1.0, 1.0),
            (0.01, -0.01, 1.0, 1.0),
        )
    )
    roll_errors = iter((0.2, 0.01, 0.01))
    controller._native_first_view_sensor = lambda: ("eye", sensor)
    controller._first_view_angular_error = lambda *args: next(errors)
    controller._first_view_roll_error = lambda *args: next(roll_errors)
    controller._camera_target_action = lambda target: target.clone()
    obj = type("Object", (), {"name": "lint_screen_179"})()

    actions = list(
        controller.center_first_view_on_object(
            obj,
            phase="checkpoint_restore",
            target_point=torch.tensor([0.0, 0.0, -1.0]),
        )
    )

    assert torch.allclose(actions[0], torch.tensor([-0.2, -0.1]))
    assert torch.allclose(actions[1], torch.tensor([0.2, 0.1]))
    assert controller.last_first_view_alignment["status"] == "centered"
    assert controller.last_first_view_alignment["stable_frames"] == 2
    assert controller.last_first_view_alignment["roll_error_rad"] == 0.01


def test_native_first_view_skips_implicit_floor_bbox_targeting():
    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    controller.first_view_targeting = True

    def fail_bbox_target(*args, **kwargs):
        raise AssertionError("floor bbox targeting must be skipped")

    controller._first_view_bbox_target = fail_bbox_target
    floor = type("Floor", (), {"name": "floors_mknpoc_0", "category": "floors"})()

    assert list(
        controller.center_first_view_on_object(floor, phase="post_navigation")
    ) == []
    assert list(
        controller.center_first_view_on_object(
            floor,
            phase="pre_place_on_top",
            surface=True,
        )
    ) == []


def test_action_first_view_alignment_gate_modes():
    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    target = type("Target", (), {"name": "target"})()
    held = type("Held", (), {"name": "held"})()
    calls = []
    alignment_targets = []

    def no_actions(*args):
        if False:
            yield None

    def record_alignment(
        obj,
        *,
        phase,
        target_point=None,
        surface=False,
        require_success=True,
    ):
        calls.append((obj, phase, surface, require_success))
        alignment_targets.append(target_point)
        if False:
            yield None

    controller._apply_grasp_without_default_reset = no_actions
    controller.center_first_view_on_object = record_alignment
    list(controller.apply_ref(StarterSemanticActionPrimitiveSet.GRASP, target))
    assert calls == [(target, "post_grasp", False, False)]

    calls.clear()
    controller._get_obj_in_hand = lambda: held
    controller._primitive_uses_symbolic_shortcut = lambda primitive: True
    controller.controller_functions = {
        StarterSemanticActionPrimitiveSet.PLACE_INSIDE: no_actions,
    }
    list(controller.apply_ref(StarterSemanticActionPrimitiveSet.PLACE_INSIDE, target))
    assert calls == [
        (target, "pre_place_inside", False, True),
        (held, "post_place_inside", False, False),
    ]

    calls.clear()
    alignment_targets.clear()
    controller._settle_robot = no_actions
    controller._with_navigation_hand_actions_suppressed = lambda actions: actions
    focus = object()

    def navigate_with_focus(*args):
        if False:
            yield None
        return focus

    controller._navigate_to_explicit_target = navigate_with_focus
    list(controller.apply_ref(StarterSemanticActionPrimitiveSet.NAVIGATE_TO, target))
    assert calls == [(target, "post_navigation", False, True)]
    assert alignment_targets == [focus]


def test_explicit_pickup_navigation_reuses_grasp_pose_for_first_view():
    import torch

    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    target = type("Target", (), {"name": "hoodie_183"})()
    controller._should_use_open_pose_navigation = lambda obj: False
    controller._should_use_grasp_ready_navigation = lambda obj: True
    controller.explicit_grasp_use_object_navigation = True
    controller.explicit_grasp_navigation_max_goal_radius = 1.2
    controller.explicit_navigation_max_goal_radius = None
    calls = []

    def navigate_to_obj(*args, **kwargs):
        calls.append(("navigate", kwargs["navigation_reason"]))
        if False:
            yield None

    grasp_pose = (
        torch.tensor([0.45, 3.3, 0.44]),
        torch.tensor([0.0, 0.0, 0.0, 1.0]),
    )

    def navigate_to_grasp_ready_pose(obj, navigation_reason):
        calls.append(("grasp_ready", navigation_reason))
        if False:
            yield None
        return grasp_pose, torch.tensor([1.0, 0.0])

    controller._navigate_to_obj = navigate_to_obj
    controller._navigate_to_grasp_ready_pose = navigate_to_grasp_ready_pose
    result = []

    def collect_result():
        result.append((yield from controller._navigate_to_explicit_target(target)))

    list(collect_result())

    assert calls == [
        ("navigate", "explicit_grasp_object"),
        ("grasp_ready", "explicit_grasp_object"),
    ]
    assert torch.equal(result[0], grasp_pose[0])


def test_first_view_failure_does_not_retry_completed_navigation():
    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    target = type("Target", (), {"name": "target"})()
    navigation_calls = []

    def navigate(*args):
        navigation_calls.append(args)
        if False:
            yield None
        return None

    def no_actions(*args):
        if False:
            yield None

    def fail_alignment(*args, **kwargs):
        raise RuntimeError("first-view failure")
        yield None

    controller._navigate_to_explicit_target = navigate
    controller._settle_robot = no_actions
    controller._with_navigation_hand_actions_suppressed = lambda actions: actions
    controller.center_first_view_on_object = fail_alignment

    try:
        list(
            controller.apply_ref(
                StarterSemanticActionPrimitiveSet.NAVIGATE_TO,
                target,
                attempts=3,
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "first-view failure"
    else:
        raise AssertionError("first-view failure should propagate")

    assert navigation_calls == [(target,)]


def test_navigation_hand_suppression_forwards_generator_return_value():
    controller = object.__new__(PhysicalStarterSemanticActionPrimitives)
    robot = type("Robot", (), {"_disable_grasp_handling": False})()
    controller.env = type("Environment", (), {"robots": [robot]})()
    controller._suppress_navigation_hand_actions = False
    controller._pin_current_arm_targets_for_navigation = lambda: None
    controller.navigation_backend = type(
        "NavigationBackend",
        (),
        {"last_navigation_result": {}},
    )()
    returned = object()

    def source():
        yield "navigation_action"
        return returned

    result = []

    def collect_result():
        result.append(
            (
                yield from controller._with_navigation_hand_actions_suppressed(
                    source()
                )
            )
        )

    assert list(collect_result()) == ["navigation_action"]
    assert result == [returned]
    assert robot._disable_grasp_handling is False
    assert controller._suppress_navigation_hand_actions is False


def test_restore_sensor_pose_uses_native_parent_mount():
    import torch

    session = object.__new__(PersistentPhysicalSession)
    calls = []
    sensor = type(
        "Sensor",
        (),
        {
            "set_position_orientation": lambda self, **kwargs: calls.append(kwargs),
        },
    )()
    session._native_rgb_sensor = lambda: ("eye", sensor)
    session._initial_native_sensor_parent_pose = {
        "position": torch.tensor([0.0, 0.0, 0.1]),
        "orientation": torch.tensor([0.0, 0.0, 0.0, 1.0]),
    }

    session._restore_sensor_pose(
        {
            "sensor_pose": {
                "position": [100.0, 100.0, 100.0],
                "orientation": [1.0, 0.0, 0.0, 0.0],
            }
        }
    )

    assert len(calls) == 1
    assert calls[0]["frame"] == "parent"
    assert torch.equal(calls[0]["position"], torch.tensor([0.0, 0.0, 0.1]))
    assert torch.equal(calls[0]["orientation"], torch.tensor([0.0, 0.0, 0.0, 1.0]))


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
    robot = type(
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
    controller.env = type("Environment", (), {"robots": [robot]})()

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
