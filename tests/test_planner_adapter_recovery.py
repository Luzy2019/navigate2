from types import SimpleNamespace

import pytest

from og_ego_prim.config.runtime_config import RuntimeConfig
from og_ego_prim.domain import Action
from og_ego_prim.prompting import PromptContext
from og_ego_prim.task_planner.adapters import (
    AgentPlannerAdapter,
    VLMClosedLoopPlannerAdapter,
    _LOADING_PROMPT,
    _PREFLIGHT_PROMPT,
    _SAFETY_PROMPT,
)


class _Agent:
    def __init__(self):
        self.calls = 0

    def step(self, *, use_obs, max_step):
        del use_obs, max_step
        self.calls += 1
        if self.calls == 1:
            return iter(())
        return iter(({"action": "wait()"},))


def test_agent_planner_adapter_restarts_after_exhausted_iterator():
    adapter = AgentPlannerAdapter(_Agent(), use_obs=False, max_step=1)

    assert adapter.propose(None) is None
    action = adapter.propose(None)

    assert action is not None
    assert action.to_legacy_plan() == "wait()"


def test_safety_prompt_uses_wait_for_pending_cooling_not_wait_for_cool():
    assert "WAIT(the exact heated\nobject)" in _SAFETY_PROMPT
    assert "must begin with exactly WAIT(the exact heated object)" in _SAFETY_PROMPT
    assert "placing it aside does not\nremove the heat risk" in _SAFETY_PROMPT
    assert "WAIT_FOR_COOL is not\nan available action" in _SAFETY_PROMPT


def test_loading_prompt_binds_gripper_state_and_inside_relation():
    assert "first step must be GRASP(pending_objects[0])" in _LOADING_PROMPT
    assert "PLACE_INSIDE, every loading placement must be PLACE_INSIDE" in _LOADING_PROMPT
    assert "After every placement the gripper is empty" in _LOADING_PROMPT


def test_safety_replan_raw_output_is_retained():
    adapter = object.__new__(VLMClosedLoopPlannerAdapter)
    adapter.last_safety_plan_raw_output = "raw response"
    adapter.last_safety_plan_payload = {"status": "SAFETY_PLAN"}

    assert adapter.last_safety_plan_raw_output == "raw response"
    assert adapter.last_safety_plan_payload == {"status": "SAFETY_PLAN"}


def test_preflight_prompt_forbids_observation_layer_entity_ids():
    assert "INPUT.allowed_entities" in _PREFLIGHT_PROMPT
    assert "obj_0001" in _PREFLIGHT_PROMPT
    assert "observation-layer references" in _PREFLIGHT_PROMPT


def test_preflight_rejects_scene_graph_entity_ids():
    adapter = object.__new__(VLMClosedLoopPlannerAdapter)
    adapter._preflight_done = False
    adapter.allowed_entity_ids = (
        "tablespoon.n.02_1",
        "rag.n.01_1",
        "peach.n.03_1",
        "carton.n.02_1",
    )
    adapter.valid_primitives = {"PLACE_INSIDE": 1}
    adapter._request = lambda context, instruction: (
        {
            "status": "MONITOR",
            "ordered_objects": ["obj_0002", "obj_0003", "obj_0001"],
            "destination_role": "carton",
            "destination_relation": "PLACE_INSIDE",
        },
        "raw output",
    )

    with pytest.raises(ValueError, match="allowed task entity IDs"):
        adapter._run_preflight(PromptContext())


def test_loading_preflight_can_be_disabled_without_calling_model(monkeypatch):
    monkeypatch.setattr(
        "og_ego_prim.task_planner.adapters.get_valid_primitives",
        lambda primitive_type: {"NAVIGATE_TO": 1},
    )

    class _ClosedLoopAgent:
        primitive_type = "starter"
        allowed_entity_ids = ("tablespoon.n.02_1",)
        current_step = 0
        runtime_controller = SimpleNamespace(last_outcome=None, last_review=None)

        def step(self, *, use_obs, max_step):
            del use_obs, max_step
            return iter(({"action": "NAVIGATE_TO(tablespoon.n.02_1)"},))

    adapter = VLMClosedLoopPlannerAdapter(
        _ClosedLoopAgent(),
        enable_loading_preflight=False,
    )
    adapter._request = lambda *args, **kwargs: pytest.fail(
        "disabled loading preflight must not call the model"
    )

    action = adapter.propose(PromptContext())

    assert action.to_legacy_plan() == "navigate_to(tablespoon.n.02_1)"


def test_task_config_defaults_loading_preflight_on_and_allows_task_override():
    assert RuntimeConfig.from_mapping({}).task.enable_loading_preflight is True
    configured = RuntimeConfig.from_mapping(
        {"task": {"enable_loading_preflight": False}}
    )
    assert configured.task.enable_loading_preflight is False


def test_closed_loop_starter_operations_force_navigation_before_remote_place():
    adapter = object.__new__(VLMClosedLoopPlannerAdapter)
    adapter.agent = SimpleNamespace(
        primitive_type="starter",
        starter_manipulation_navigation_guard=True,
        runtime_controller=SimpleNamespace(
            last_outcome=SimpleNamespace(
                executed=True,
                succeeded=True,
                review=SimpleNamespace(action=Action.from_raw("GRASP(peach.n.03_1)")),
            )
        ),
    )
    adapter.allowed_entity_ids = ("peach.n.03_1", "compost_bin.n.01_1")
    adapter.valid_primitives = {"NAVIGATE_TO": 1, "PLACE_INSIDE": 1}
    adapter._steps = [Action.from_raw("PLACE_INSIDE(compost_bin.n.01_1)")]
    adapter._held_object = lambda: "peach.n.03_1"
    adapter._request = lambda *args, **kwargs: pytest.fail(
        "navigation guard must run before operation preparation"
    )
    adapter._issue = lambda action, **kwargs: action

    action = adapter._next_step(PromptContext())

    assert action.to_legacy_plan() == "navigate_to(compost_bin.n.01_1)"


def test_closed_loop_starter_operation_runs_after_successful_navigation():
    adapter = object.__new__(VLMClosedLoopPlannerAdapter)
    adapter.agent = SimpleNamespace(
        primitive_type="starter",
        starter_manipulation_navigation_guard=True,
        runtime_controller=SimpleNamespace(
            last_outcome=SimpleNamespace(
                executed=True,
                succeeded=True,
                review=SimpleNamespace(
                    action=Action.from_raw("NAVIGATE_TO(compost_bin.n.01_1)")
                ),
            )
        ),
    )
    adapter.allowed_entity_ids = ("peach.n.03_1", "compost_bin.n.01_1")
    adapter.valid_primitives = {"NAVIGATE_TO": 1, "PLACE_INSIDE": 1}
    adapter._steps = [Action.from_raw("PLACE_INSIDE(compost_bin.n.01_1)")]
    adapter._held_object = lambda: "peach.n.03_1"
    adapter._request = lambda context, instruction, **extra: (
        {"status": "ACTION", "action": "PLACE_INSIDE(compost_bin.n.01_1)"},
        "raw output",
    )
    adapter._issue = lambda action, **kwargs: action

    action = adapter._next_step(PromptContext())

    assert action.to_legacy_plan() == "place_inside(compost_bin.n.01_1)"


def test_loading_keeps_seed_grasp_until_gripper_state_is_updated():
    held = None
    adapter = object.__new__(VLMClosedLoopPlannerAdapter)
    adapter.agent = SimpleNamespace(
        primitive_type="starter",
        starter_manipulation_navigation_guard=True,
        runtime_controller=SimpleNamespace(last_outcome=None),
    )
    adapter.valid_primitives = {"NAVIGATE_TO": 1, "GRASP": 1, "PLACE_INSIDE": 1}
    adapter.allowed_entity_ids = ("peach.n.03_1", "carton.n.02_1")
    adapter._loading = {
        "destination": None,
        "relation": "PLACE_INSIDE",
        "pending": ["peach.n.03_1"],
    }
    adapter._steps = [Action.from_raw("GRASP(peach.n.03_1)")]
    adapter._held_object = lambda: held
    adapter._issue = lambda action, **kwargs: action
    adapter._request = lambda *args, **kwargs: pytest.fail(
        "seed GRASP must not be reconstructed by loading prompt"
    )

    action = adapter._next_step(PromptContext())

    assert action.to_legacy_plan() == "navigate_to(peach.n.03_1)"


def test_loading_executes_seed_grasp_after_its_guard_navigation():
    adapter = object.__new__(VLMClosedLoopPlannerAdapter)
    adapter.agent = SimpleNamespace(
        primitive_type="starter",
        starter_manipulation_navigation_guard=True,
        runtime_controller=SimpleNamespace(
            last_outcome=SimpleNamespace(
                executed=True,
                succeeded=True,
                review=SimpleNamespace(
                    action=Action.from_raw("NAVIGATE_TO(peach.n.03_1)")
                ),
            )
        ),
    )
    adapter.valid_primitives = {"NAVIGATE_TO": 1, "GRASP": 1}
    adapter.allowed_entity_ids = ("peach.n.03_1",)
    adapter._loading = {"destination": None}
    adapter._steps = [Action.from_raw("GRASP(peach.n.03_1)")]
    adapter._held_object = lambda: None
    adapter._issue = lambda action, **kwargs: action
    adapter._request = lambda *args, **kwargs: pytest.fail(
        "seed GRASP must execute without an action-preparation request"
    )

    action = adapter._next_step(PromptContext())

    assert action.to_legacy_plan() == "grasp(peach.n.03_1)"
