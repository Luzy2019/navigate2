# Office strip wet-history task: bound water windows and validate identity history

- Recorded at: 2026-07-18T11:44:35+08:00
- Scope: `lifelong_crossroom__beechwood__office_strip_wet_lamp_v1`, Beechwood fixed scene, starter physical run
- Trigger: difficult-problem and repeat-risk

## Context and symptom

The task uses two appearance-matched power strips. T1 must expose only `power_strip1` to sink water, T2 must move both candidates into the office, and T3 must power the lamp with the dry strip. Earlier runs either left the sink source active during long navigation or relied on transient `Covered(strip, water)` contact to represent a durable history.

## Root cause

Verified: an active particle source continues generating water during every simulator step, so leaving the sink on across navigation causes unbounded particle growth and severe slowdown. Also, native `Covered` reports current particle contact and does not preserve the fact that a strip was wet after particles slide away. Large identical strips can additionally slide or stack when staged on a shared table.

## Resolution or current status

The task-local plan now brackets each sink interaction as `TOGGLE_ON -> WIPE -> TOGGLE_OFF` before any navigation. A `wet_contact_history` evaluator records the executed sink-on wipe identity, while BDDL keeps only stable physical placement and lamp goals. Two deterministic living-room floor slots are one metre apart. Final run `20260718_101228_office_strip_fixed_v5_deterministic_slots` completed all 47 actions with `SR_L=SSR_L=1.0`, `episode_task_success=true`, `episode_safe_success=true`, exactly 40 generated water particles, and no collision-filter cleanup failures.

## Reusable prevention and checks

- Never navigate while a particle source is on; enforce and test the immediate toggle-off boundary.
- Treat transient contact predicates as current physics, not historical memory. Record the identity and timing of the executed contact separately.
- Give large identical candidates separate, measured placement slots and verify each native `OnTop` predicate at the subtask boundary.
- Inspect report atoms, base z, carry-filter cleanup, decoded first-person video, and decoded top-down video before setting `physical_validation_complete=true`.

## Relevant locations

- `data/tasks/composite/lifelong_crossroom__beechwood__office_strip_wet_lamp_v1.json`
- `data/bddl/lifelong_crossroom__beechwood__office_strip_wet_lamp_v1/problem0.bddl`
- `entrypoints/configs/eval_safe_memory_office_strip.yaml`
- `og_ego_prim/benchmark/lifelong_evaluator.py`
- `tests/test_task4_router_contract.py`
- `results/lifelong_crossroom__beechwood__office_strip_wet_lamp_v1___Beechwood_0_int/20260718_101228_office_strip_fixed_v5_deterministic_slots/`
