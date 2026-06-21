import unittest
import ast
import json
from pathlib import Path

from og_ego_prim.benchmark import resolve_primitive_type
from og_ego_prim.primitives.specs import (
    STARTER_VALID_PRIMITIVES,
    expand_legacy_plan_for_starter,
    starter_evaluation_action,
)
from og_ego_prim.utils.prompts import build_starter_step_prompt
from og_ego_prim.cli.online_benchmark_all import get_launcher


class StarterPrimitiveModeTest(unittest.TestCase):
    @staticmethod
    def _class_method_source(path, class_name, method_name):
        source = Path(path).read_text()
        module = ast.parse(source)
        class_node = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef)
            and node.name == class_name
        )
        method_node = next(
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        )
        return ast.get_source_segment(source, method_node)

    @classmethod
    def _physical_starter_method_source(cls, method_name):
        return cls._class_method_source(
            "og_ego_prim/primitives/starter_primitives.py",
            "PhysicalStarterSemanticActionPrimitives",
            method_name,
        )

    def test_physical_starter_disables_implicit_hand_reset(self):
        apply_source = self._physical_starter_method_source("apply_ref")
        reset_source = self._physical_starter_method_source("_reset_hand")

        self.assertNotIn("super().apply_ref", apply_source)
        self.assertNotIn("super()._reset_hand", reset_source)
        self.assertIn("yield from ()", reset_source)

    def test_carry_lift_does_not_send_gripper_release_command(self):
        lift_source = self._physical_starter_method_source("_move_hand_upward")

        self.assertNotIn("gripper_", lift_source)
        self.assertNotIn("= 1.0", lift_source)
        self.assertIn("_move_hand_linearly_cartesian", lift_source)

    def test_observation_simulation_steps_hold_absolute_joint_positions(self):
        loop_source = self._class_method_source(
            "og_ego_prim/primitives/executor.py",
            "Executor",
            "_simulator_loop",
        )
        hold_source = self._class_method_source(
            "og_ego_prim/primitives/executor.py",
            "Executor",
            "get_hold_action",
        )
        diagnostic_source = self._class_method_source(
            "og_ego_prim/primitives/executor.py",
            "Executor",
            "log_passive_motion_diagnostic",
        )

        self.assertNotIn("torch.zeros", loop_source)
        self.assertIn("get_hold_action", loop_source)
        self.assertIn("compute_no_op_action", hold_source)
        self.assertIn("[executor][between-actions]", diagnostic_source)
        self.assertIn("unexpected_motion", diagnostic_source)
        self.assertIn("task_action_generated", diagnostic_source)

    def test_trash_container_navigation_uses_extra_standoff(self):
        init_source = self._class_method_source(
            "og_ego_prim/navigation/omnigibson_nav.py",
            "OmniGibsonNavigationBackend",
            "__init__",
        )
        navigate_source = self._class_method_source(
            "og_ego_prim/navigation/omnigibson_nav.py",
            "OmniGibsonNavigationBackend",
            "navigate_to_object",
        )
        drive_source = self._class_method_source(
            "og_ego_prim/navigation/omnigibson_nav.py",
            "OmniGibsonNavigationBackend",
            "_drive_towards_waypoint",
        )
        radius_source = self._class_method_source(
            "og_ego_prim/navigation/omnigibson_nav.py",
            "OmniGibsonNavigationBackend",
            "_minimum_goal_radius",
        )

        self.assertIn('"0.80"', init_source)
        self.assertIn('"0.70"', init_source)
        self.assertIn('"0.45"', init_source)
        self.assertIn("ISBENCH_NAV_MAX_IK_GOAL_CHECKS", init_source)
        self.assertIn("ISBENCH_NAV_TACTQN_MIN_GOAL_RADIUS", init_source)
        self.assertIn("ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE", init_source)
        self.assertIn('"0.10"', init_source)
        self.assertIn('"trash_can"', radius_source)
        self.assertIn('"ashcan"', radius_source)
        self.assertIn('model == "tactqn"', radius_source)
        self.assertIn("self.tactqn_min_goal_radius", radius_source)
        self.assertIn('"cabinet" in category', radius_source)
        self.assertIn("return 0.45", radius_source)
        self.assertIn("minimum_goal_radius_override", navigate_source)
        self.assertIn("start_radius_satisfied", navigate_source)
        self.assertIn("and start_radius_satisfied", navigate_source)
        self.assertIn("distance < m.DEFAULT_DIST_THRESHOLD", drive_source)
        self.assertIn("distance <= self.stuck_waypoint_tolerance", drive_source)
        self.assertIn("accepted_after_stuck", drive_source)

    def test_open_close_uses_local_omnigibson_compatibility_sampler(self):
        starter_init_source = self._physical_starter_method_source("__init__")
        open_close_source = self._physical_starter_method_source("_open_or_close")
        sampler_source = self._physical_starter_method_source(
            "_sample_open_close_grasp_data"
        )
        prismatic_source = self._physical_starter_method_source(
            "_prismatic_open_close_grasp_data"
        )
        revolute_source = self._physical_starter_method_source(
            "_revolute_open_close_grasp_data"
        )

        self.assertIn("_sample_open_close_grasp_data", open_close_source)
        self.assertIn("open_close_object_precheck", open_close_source)
        self.assertNotIn("_navigate_if_needed", open_close_source)
        self.assertIn("list(_get_relevant_joints(obj)[1])", sampler_source)
        self.assertNotIn(".size(", sampler_source)
        self.assertIn("torch.stack", prismatic_source)
        self.assertIn("_interpolate_open_close_waypoints", prismatic_source)
        self.assertNotIn("native_link_bboxes", revolute_source)
        self.assertIn('physics:localPos0', revolute_source)
        self.assertIn("T.axisangle2quat", revolute_source)
        self.assertIn('getattr(obj, "model", "") == "tactqn"', revolute_source)
        self.assertIn("tactqn_lower_handle_candidate_", revolute_source)
        self.assertIn("vertical_axis_index", revolute_source)
        self.assertIn("surface_position", revolute_source)
        self.assertIn("preferred_goal_direction", open_close_source)
        self.assertIn("preferred_side=door_front", open_close_source)
        self.assertIn("grasp_candidate_index=attempt", open_close_source)
        self.assertIn("phase=close_gripper", open_close_source)
        self.assertIn("phase=gripper_closed", open_close_source)
        self.assertIn("_move_tactqn_open_pose", open_close_source)
        self.assertIn("ISBENCH_STARTER_TACTQN_OPEN_GOAL_RADIUS", starter_init_source)
        self.assertIn('"0.58"', starter_init_source)
        self.assertIn(
            "ISBENCH_STARTER_TACTQN_SYMBOLIC_OPEN_CLOSE_FALLBACK",
            starter_init_source,
        )
        self.assertIn("self.tactqn_open_goal_radius", open_close_source)
        self.assertIn("minimum_goal_radius_override", open_close_source)
        self.assertIn("_symbolic_open_or_close_fallback", open_close_source)
        self.assertIn("candidate_fractions", revolute_source)
        self.assertIn("tactqn_lower_handle_candidate_", revolute_source)
        self.assertIn(
            "exc.reason == ActionPrimitiveError.Reason.SAMPLING_ERROR",
            open_close_source,
        )

    def test_tactqn_symbolic_fallback_is_scoped_to_open_close(self):
        open_close_source = self._physical_starter_method_source("_open_or_close")
        should_fallback_source = self._physical_starter_method_source(
            "_should_symbolically_fallback_open_close"
        )
        fallback_source = self._physical_starter_method_source(
            "_symbolic_open_or_close_fallback"
        )
        grasp_source = self._physical_starter_method_source("_grasp")

        self.assertIn("self.tactqn_symbolic_open_close_fallback", should_fallback_source)
        self.assertIn('getattr(obj, "category", "") == "top_cabinet"', should_fallback_source)
        self.assertIn('getattr(obj, "model", "") == "tactqn"', should_fallback_source)
        self.assertIn("obj.states[object_states.Open].set_value", fallback_source)
        self.assertIn("fully=True", fallback_source)
        self.assertIn("[symbolic_fallback]", fallback_source)
        self.assertIn("yield from self._settle_robot()", fallback_source)
        self.assertIn("[symbolic_direct]", open_close_source)
        self.assertIn("physical_attempts=0", open_close_source)
        self.assertIn("physical_attempted=False", open_close_source)
        self.assertLess(
            open_close_source.index("_should_symbolically_fallback_open_close"),
            open_close_source.index("object-level pre-navigation"),
        )
        self.assertLess(
            open_close_source.index("[symbolic_direct]"),
            open_close_source.index("for attempt in range"),
        )
        self.assertIn("if physical_attempted:", fallback_source)
        self.assertNotIn("_symbolic_open_or_close_fallback", grasp_source)

    def test_open_retry_changes_do_not_modify_grasp_pipeline(self):
        grasp_source = self._physical_starter_method_source("_grasp")
        grasp_apply_source = self._physical_starter_method_source(
            "_apply_grasp_without_default_reset"
        )
        lift_source = self._physical_starter_method_source(
            "_lift_held_object_for_navigation"
        )

        self.assertIn("yield from super()._grasp(obj)", grasp_source)
        self.assertIn("_repair_sticky_grasp_if_contacted", grasp_source)
        self.assertIn("_lift_held_object_for_navigation", grasp_apply_source)
        self.assertIn("_move_hand_upward", lift_source)
        for source in (grasp_source, grasp_apply_source, lift_source):
            self.assertNotIn("grasp_candidate_index", source)
            self.assertNotIn("tactqn", source)
            self.assertNotIn("_open_or_close", source)
            self.assertNotIn("_move_tactqn_open_pose", source)

    def test_tactqn_direct_joint_motion_is_open_only(self):
        helper_source = self._physical_starter_method_source(
            "_move_tactqn_open_pose"
        )
        open_close_source = self._physical_starter_method_source("_open_or_close")

        self.assertIn("self._convert_cartesian_to_joint_space", helper_source)
        self.assertIn("self._move_hand_direct_joint", helper_source)
        self.assertIn("should_open", open_close_source)
        self.assertIn('getattr(obj, "model", "") == "tactqn"', open_close_source)
        self.assertIn("motion=direct_joint", helper_source)

    def test_starter_primitive_arities_match_omnigibson_contract(self):
        self.assertEqual(STARTER_VALID_PRIMITIVES["GRASP"], 1)
        self.assertEqual(STARTER_VALID_PRIMITIVES["PLACE_INSIDE"], 1)
        self.assertEqual(STARTER_VALID_PRIMITIVES["PLACE_ON_TOP"], 1)
        self.assertEqual(STARTER_VALID_PRIMITIVES["RELEASE"], 0)

    def test_legacy_place_inside_expands_to_grasp_and_physical_place(self):
        plans = expand_legacy_plan_for_starter(
            {
                "action": "place_inside(apple.n.01_1, cabinet.n.01_1)",
                "caution": "Keep food separate.",
            }
        )

        self.assertEqual(
            plans,
            [
                {"action": "navigate_to(apple.n.01_1)", "caution": None},
                {"action": "grasp(apple.n.01_1)", "caution": None},
                {
                    "action": "place_inside(cabinet.n.01_1)",
                    "caution": "Keep food separate.",
                },
            ],
        )

    def test_non_placement_plan_is_unchanged(self):
        plan = {"action": "open(cabinet.n.01_1)", "caution": None}
        self.assertEqual(expand_legacy_plan_for_starter(plan), [plan])

    def test_physical_place_maps_back_to_legacy_evaluation_action(self):
        self.assertEqual(
            starter_evaluation_action(
                "PLACE_INSIDE(cabinet.n.01_1)",
                "apple.n.01_1",
            ),
            "place_inside(apple.n.01_1, cabinet.n.01_1)",
        )

    def test_starter_prompt_requires_grasp_and_single_argument_place(self):
        prompt = build_starter_step_prompt(
            objects_str="1. apple.n.01_1\n2. cabinet.n.01_1",
            task_instruction="Store the apple.",
            object_abilities_str="cabinet.n.01_1: ['openable']",
            task_goals="(inside apple cabinet)",
            wash_rules_str="",
            history_actions="None",
            prompt_setting="v1",
        )

        self.assertIn("GRASP(object)", prompt)
        self.assertIn("Before each GRASP, first call NAVIGATE_TO", prompt)
        self.assertIn("PLACE_INSIDE(container)", prompt)
        self.assertIn("take only the destination as their single argument", prompt)

    def test_batch_launcher_forwards_starter_and_show_robot_flags(self):
        launcher = get_launcher(
            task_name="store_a_tennis_ball",
            scene_name="Rs_int",
            primitive_type="starter",
            show_robot=True,
        )

        self.assertIn("--primitive_type", launcher)
        self.assertIn("starter", launcher)
        self.assertIn("--show_robot", launcher)

    def test_custom_task_defaults_to_starter_but_can_be_overridden(self):
        task_config = json.loads(
            Path("data/tasks/store_apple_and_tissue_box_in_bottom_cabinet.json").read_text()
        )

        self.assertEqual(resolve_primitive_type(task_config), "starter")
        self.assertEqual(resolve_primitive_type(task_config, "ego"), "ego")

    def test_custom_task_spreads_grasp_targets_across_table_edges(self):
        task_config = json.loads(
            Path("data/tasks/store_apple_and_tissue_box_in_bottom_cabinet.json").read_text()
        )
        object_initial_poses = task_config["scene_info"]["object_initial_poses"]
        object_names = (
            "half__banana.n.01_1",
            "detergent__bottle.n.01_1",
            "dishtowel.n.01_1",
        )

        for object_name in object_names:
            self.assertIn(object_name, object_initial_poses)
        self.assertIn("ashcan.n.01_1", object_initial_poses)

        positions = [
            object_initial_poses[object_name]["position"]
            for object_name in object_names
        ]
        for i, pos_i in enumerate(positions):
            for pos_j in positions[i + 1:]:
                xy_distance = sum(
                    (pos_i[axis] - pos_j[axis]) ** 2
                    for axis in (0, 1)
                ) ** 0.5
                self.assertGreaterEqual(xy_distance, 0.6)

        ashcan_position = object_initial_poses["ashcan.n.01_1"]["position"]
        self.assertEqual(object_initial_poses["ashcan.n.01_1"]["model"], "zotrbg")
        self.assertGreaterEqual(ashcan_position[0], 4.1)
        self.assertGreater(ashcan_position[1], 11.0)
        self.assertGreater(ashcan_position[2], 0.18)

        ashcan_orientation = object_initial_poses["ashcan.n.01_1"]["orientation"]
        orientation_norm = sum(value ** 2 for value in ashcan_orientation) ** 0.5
        self.assertAlmostEqual(orientation_norm, 1.0, places=5)

        detergent_cabinet_position = object_initial_poses["cabinet.n.01_2"][
            "position"
        ]
        self.assertEqual(detergent_cabinet_position, [5.15, 10.35, 0.69])

    def test_scene_template_maps_red_drawer_to_detergent_cabinet(self):
        scene_template = json.loads(
            Path(
                "data/scenes/Wainscott_0_int/json/"
                "Wainscott_0_int_task_store_apple_and_tissue_box_in_bottom_cabinet_0_0_template.json"
            ).read_text()
        )
        removed_back_wall_objects = {
            "bottom_cabinet_bamfsz_0",
            "bottom_cabinet_no_top_bmsclc_0",
            "countertop_tpuwys_4",
            "fridge_dszchb_0",
            "microwave_bfbeeb_0",
            "oven_fexqbj_0",
            "oven_fexqbj_1",
            "top_cabinet_eobsmt_0",
            "top_cabinet_jvdbxh_0",
            "top_cabinet_lsyzkh_1",
            "top_cabinet_lsyzkh_2",
        }
        object_registry = scene_template["state"]["object_registry"]
        object_init_info = scene_template["objects_info"]["init_info"]
        for object_name in removed_back_wall_objects:
            self.assertNotIn(object_name, object_registry)
            self.assertNotIn(object_name, object_init_info)

        target_cabinet = "top_cabinet_tactqn_0"
        self.assertEqual(
            object_registry[target_cabinet]["root_link"]["pos"],
            [5.15, 10.35, 0.69],
        )
        object_mapping = scene_template["metadata"]["task"]["inst_to_name"]
        object_registry = scene_template["state"]["object_registry"]

        self.assertEqual(
            object_mapping["cabinet.n.01_2"],
            "top_cabinet_tactqn_0",
        )
        self.assertEqual(
            object_mapping["cabinet.n.01_1"],
            "bottom_cabinet_no_top_qohxjq_0",
        )
        self.assertEqual(
            object_registry["top_cabinet_tactqn_0"]["root_link"]["pos"],
            [5.15, 10.35, 0.69],
        )
        self.assertEqual(
            object_registry["apple_47"]["root_link"]["pos"],
            [6.564576625823975, 12.528535842895508, 0.74],
        )

    def test_custom_task_legacy_places_expand_into_grasp_sequences(self):
        task_config = json.loads(
            Path("data/tasks/store_apple_and_tissue_box_in_bottom_cabinet.json").read_text()
        )
        expanded_actions = []
        for plan in task_config["example_planning"]:
            action = "done()" if plan["action"].endswith("DONE") else plan["action"].lower()
            expanded_actions.extend(
                item["action"]
                for item in expand_legacy_plan_for_starter(
                    {"action": action, "caution": plan["caution"]}
                )
            )

        self.assertEqual(
            expanded_actions[:3],
            [
                "navigate_to(half__banana.n.01_1)",
                "grasp(half__banana.n.01_1)",
                "place_inside(ashcan.n.01_1)",
            ],
        )
        self.assertEqual(
            expanded_actions.count("navigate_to(detergent__bottle.n.01_1)"),
            1,
        )
        self.assertEqual(expanded_actions.count("grasp(detergent__bottle.n.01_1)"), 1)
        self.assertIn("place_inside(cabinet.n.01_2)", expanded_actions)
        self.assertEqual(expanded_actions.count("grasp(dishtowel.n.01_1)"), 1)
        self.assertEqual(expanded_actions.count("navigate_to(dishtowel.n.01_1)"), 1)
        self.assertIn("place_on_top(dish_rack.n.01_1)", expanded_actions)
        for index, action in enumerate(expanded_actions):
            if action.startswith("grasp("):
                self.assertGreater(index, 0)
                self.assertEqual(
                    expanded_actions[index - 1],
                    action.replace("grasp(", "navigate_to(", 1),
                )


if __name__ == "__main__":
    unittest.main()
