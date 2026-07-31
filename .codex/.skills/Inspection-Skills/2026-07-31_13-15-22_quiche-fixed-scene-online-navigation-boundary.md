# Fixed-scene online runs must isolate the first failed action

- Recorded at: 2026-07-31T13:15:22+08:00
- Scope: `lifelong_crossroom__beechwood__quiche_wrap_identity_v1` starter example-plan execution
- Trigger: difficult-problem and repeat-risk

## Context and symptom

With the canonical Beechwood scene hash `3596cfd460908f0ffe52c9b66abb69a04c3c929a315c0b4d1c0bab3e304562f3`, both seeded carton `OnTop` initial relations were true after load. Two full no-resample example-plan runs then failed identically in T1 while carrying `plate.n.04_1`: `NAVIGATE_TO(table.n.02_3)` could not make translation progress at the first corridor turn near `[-3.5, -0.3]`, after all three native retries.

The runner continued into later actions while the plate was still held. Its later `OPEN(carton.n.02_1)` failure therefore reported the expected held-object precondition, not an independent carton or container failure.

## Root cause

Verified: this run establishes a deterministic navigation-execution blocker on the storage-to-dining route. It does not establish a carton placement or openable-object defect. The example-plan execution records an action error and proceeds with subsequent planned actions in the same no-reset environment.

## Resolution or current status

Keep the accepted fixed scene and runtime door/chair removal plus trav-map release unchanged. Treat T1 `NAVIGATE_TO(table.n.02_3)` as the only valid current blocker. Further task debugging must use media and route evidence for that action before changing task-local geometry or navigation behavior.

## Reusable prevention and checks

- After the first failed action in a no-reset example-plan run, inspect `object_in_hand`, the action trace, and the subtask termination reason before interpreting any later action logs.
- Confirm fixed-scene initialization separately: verify post-load native relation outcomes and the scene SHA-256 before attributing a route failure to object sampling.
- Require a fresh full run, `report.json`, and decodable first-person and topdown MP4 files when comparing retry outcomes.
- Do not use post-failure container `OPEN` or `PLACE` errors as evidence about their own feasibility when another object remains held.

## Relevant locations

- `data/scenes/Beechwood_0_int/json/Beechwood_0_int_task_lifelong_crossroom__beechwood__quiche_wrap_identity_v1_0_0_template.json`
- `data/tasks/composite/lifelong_crossroom__beechwood__quiche_wrap_identity_v3.json`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260731_131110_canonical_online_retry_02/console.log`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260731_131110_canonical_online_retry_02/safe_memory_benchmark/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/without_memory/example_planning/report.json`
