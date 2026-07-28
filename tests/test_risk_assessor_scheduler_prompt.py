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


def test_risk_prompt_explains_matching_cooking_wait_semantics():
    client = _SafeClient()
    assessor = RiskAssessor(client)
    context = RiskContext(
        action=Action.from_raw("WAIT_FOR_COOKED(mug.n.04_1)"),
        scene={
            "rooms": [
                {
                    "room_name": "kitchen_0",
                    "nodes": [
                        {"id": "mug", "entity_id": "mug.n.04_1"},
                    ],
                    "edges": [],
                }
            ]
        },
        scheduler={
            "clock": {"step": 100},
            "pending": [
                {
                    "process_id": "heating:mug",
                    "process_type": "heating",
                    "entity_ids": ["mug.n.04_1"],
                    "status": "pending",
                    "start_step": 40,
                    "ready_step": 160,
                    "readiness_predicate": "cooked_or_heated",
                    "blocking_actions": [],
                }
            ],
        },
    )

    assessor(context)

    assert "`WAIT_FOR_COOKED(X)` only advances the scheduler's already-started" in client.prompt
    assert "pending heating process for the exact X" in client.prompt
