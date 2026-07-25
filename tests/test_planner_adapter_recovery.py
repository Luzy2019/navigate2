from og_ego_prim.task_planner.adapters import (
    AgentPlannerAdapter,
    VLMClosedLoopPlannerAdapter,
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
