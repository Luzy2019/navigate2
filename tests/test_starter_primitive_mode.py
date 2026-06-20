import unittest

from og_ego_prim.primitives.specs import (
    STARTER_VALID_PRIMITIVES,
    expand_legacy_plan_for_starter,
    starter_evaluation_action,
)
from og_ego_prim.utils.prompts import build_starter_step_prompt
from og_ego_prim.cli.online_benchmark_all import get_launcher


class StarterPrimitiveModeTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
