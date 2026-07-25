import pytest

from og_ego_prim.primitives import get_valid_primitives
from og_ego_prim.utils.planning import (
    planner_prompt_entity_ids,
    validate_planner_action,
)


def test_planner_prompt_preserves_exact_duplicate_category_ids():
    entity_ids = ("water_bottle.n.01_1", "water_bottle.n.01_2")

    assert planner_prompt_entity_ids(entity_ids) == entity_ids


def test_planner_rejects_ambiguous_generic_entity_action():
    with pytest.raises(ValueError, match="one exact allowed entity"):
        validate_planner_action(
            "GRASP(water_bottle)",
            get_valid_primitives("starter"),
            allowed_entity_ids=("water_bottle.n.01_1", "water_bottle.n.01_2"),
        )


def test_planner_accepts_exact_duplicate_category_entity_action():
    action = validate_planner_action(
        "GRASP(water_bottle.n.01_1)",
        get_valid_primitives("starter"),
        allowed_entity_ids=("water_bottle.n.01_1", "water_bottle.n.01_2"),
    )

    assert action.object_id == "water_bottle.n.01_1"
