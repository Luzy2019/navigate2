"""Print the scheduler-relevant contents of a physical session checkpoint."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


CHECKPOINT = Path(
    "outputs/headless_manual_hot_water_particle_frame16_coldrestore/"
    "checkpoint/frame_000024.pt"
)
OUTPUT = Path(
    "outputs/headless_manual_hot_water_particle_frame24_cooling_test/"
    "frame_000024_cooling_backfill.pt"
)


def main() -> None:
    payload = torch.load(CHECKPOINT, map_location="cpu", weights_only=False)
    scheduler = payload["scheduler"]
    pending = list(scheduler.get("pending") or ())
    if any(item.get("process_type") == "cooling" for item in pending):
        raise RuntimeError("source checkpoint unexpectedly already has cooling")
    pending.append(
        {
            "process_id": "cooling:73dce88b6824d676",
            "process_type": "cooling",
            "entity_ids": ["water_bottle.n.01_1"],
            "source_action_id": "manual_backfill:toggle_off:frame_000010",
            "start_step": 2423,
            "ready_step": 9623,
            "readiness_predicate": "not_heated",
            "blocking_actions": [],
            "completion_effects": {"heated": False},
            "status": "pending",
            "extensions": {
                "description": "hot object cooling",
                "entity_selectors": {"TOGGLE_OFF": "placements_of_object"},
                "conditions_by_action": {
                    "TOGGLE_OFF": {"source_supports_any": ["heat_source"]},
                    "WAIT": {"entity_states": {"heated": True}},
                },
                "duration_actions": ["WAIT"],
                "include_action_duration": True,
                "cancels": ["heating"],
                "cancel_actions": ["TOGGLE_ON"],
                "gate_entity_ids": ["water_bottle.n.01_1"],
                "trigger_action": "TOGGLE_OFF",
                "backfilled_from": "frame_000010 toggle_off(microwave.n.02_1)",
            },
        }
    )
    payload["scheduler"] = {**scheduler, "pending": pending}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    torch.save(payload, OUTPUT)
    print(OUTPUT)
    print(payload["scheduler"])


if __name__ == "__main__":
    main()
