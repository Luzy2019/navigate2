from og_ego_prim.domain import Action
from og_ego_prim.scheduler import ProcessStatus, ScheduledProcess, Scheduler
from og_ego_prim.scheduler.clock import ManualSimulationClock


def test_semantic_wait_clears_only_matching_cooling_timer():
    scheduler = Scheduler(clock=ManualSimulationClock(start_step=5650))
    cooling = ScheduledProcess(
        process_id="cooling:bottle",
        process_type="cooling",
        entity_ids=("water_bottle.n.01_1",),
        start_step=2423,
        ready_step=9623,
        status=ProcessStatus.PENDING,
    )
    spoilage = ScheduledProcess(
        process_id="spoilage:bottle",
        process_type="spoilage",
        entity_ids=("water_bottle.n.01_1",),
        start_step=3529,
        ready_step=10729,
        status=ProcessStatus.PENDING,
    )
    scheduler._pending = {
        cooling.process_id: cooling,
        spoilage.process_id: spoilage,
    }

    action = Action.from_raw("WAIT(water_bottle.n.01_1)")
    for process in scheduler.pending_for(action.entity_ids, process_type="cooling"):
        scheduler.cancel(process.process_id, reason="semantic_wait_completed")

    assert scheduler.pending_for("water_bottle.n.01_1", process_type="cooling") == ()
    assert scheduler.pending_for("water_bottle.n.01_1", process_type="spoilage") == (spoilage,)
