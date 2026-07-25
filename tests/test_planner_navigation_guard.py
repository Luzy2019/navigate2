from og_ego_prim.benchmark.tracker.online_tracker import OnlineEvalTracker
from og_ego_prim.task_planner.planner import AgentPlanner


def _agent_with_last_navigation(action: str) -> AgentPlanner:
    agent = AgentPlanner.__new__(AgentPlanner)
    agent.allowed_entity_ids = (
        "water_bottle.n.01_1",
        "water_bottle.n.01_2",
        "microwave.n.02_1",
    )
    agent._subtask_plan_start = 0
    tracker = OnlineEvalTracker()
    tracker.track_plan(
        step=1,
        plan={"action": action, "caution": None},
    )
    tracker.mark_plan_runtime(action, executed=True, succeeded=True)
    agent.tracker = tracker
    return agent


def test_navigation_guard_matches_generic_planner_name_to_exact_instance():
    agent = _agent_with_last_navigation("navigate_to(water_bottle.n.01_1)")

    assert agent._last_plan_is_navigation_to("water_bottle")


def test_navigation_guard_rejects_ambiguous_or_different_exact_instance():
    agent = _agent_with_last_navigation("navigate_to(water_bottle.n.01_1)")

    assert not agent._last_plan_is_navigation_to("water_bottle.n.01_2")
    assert not agent._last_plan_is_navigation_to("microwave")
