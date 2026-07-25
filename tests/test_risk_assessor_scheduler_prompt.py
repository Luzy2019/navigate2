from og_ego_prim.domain import Action
from og_ego_prim.risk_predictor.models import RiskContext
from og_ego_prim.risk_predictor.risk_assessor import RiskAssessor


class _SafeClient:
    def __init__(self):
        self.prompt = ""

    def model(self, prompt):
        self.prompt = prompt
        return '{"status":"safe","matched_risks":[],"reason":"No immediate risk."}'


def test_risk_prompt_exposes_pending_cooling_timer_with_remaining_steps():
    client = _SafeClient()
    assessor = RiskAssessor(client)
    context = RiskContext(
        action=Action.from_raw("POUR_INTO(vase.n.01_1)"),
        scene={
            "rooms": [
                {
                    "room_name": "kitchen_0",
                    "nodes": [
                        {"id": "bottle", "entity_id": "water_bottle.n.01_1"},
                        {"id": "vase", "entity_id": "vase.n.01_1"},
                    ],
                    "edges": [],
                }
            ]
        },
        scheduler={
            "clock": {"step": 5650},
            "pending": [
                {
                    "process_id": "cooling:bottle",
                    "process_type": "cooling",
                    "entity_ids": ["water_bottle.n.01_1"],
                    "status": "pending",
                    "start_step": 2423,
                    "ready_step": 9623,
                    "readiness_predicate": "cooling_timer_elapsed",
                    "blocking_actions": [],
                }
            ],
        },
    )

    assessor(context)

    assert "Time scheduler (authoritative temporal state):" in client.prompt
    assert "type=cooling; entities=water_bottle.n.01_1" in client.prompt
    assert "ready_step=9623; remaining_steps=3973" in client.prompt
    assert "state `WAIT(object)` as the applicable mitigation" in client.prompt
