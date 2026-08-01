import pytest

from og_ego_prim.prompting import PromptContext
from og_ego_prim.task_planner.adapters import (
    AgentPlannerAdapter,
    VLMClosedLoopPlannerAdapter,
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
