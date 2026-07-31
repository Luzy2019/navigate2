# Quiche navigation retries must preserve a physical checkpoint

- Recorded at: 2026-07-31T18:26:54+08:00
- Scope: `lifelong_crossroom__beechwood__quiche_wrap_identity_v1`, fixed-scene safe-memory runner
- Trigger: difficult-problem and repeat-risk

## Context and symptom

With the fixed scene, `online_object_sampling=false`, and
`eval_safe_memory_quiche_wrap.yaml`, one full-run attempt failed while carrying
`plate_186` at dining-room waypoint `[-8.1, -0.5]` after all three native
navigation retries. A second attempt with the same files reached the dining
table successfully (`end_base_target_xy_distance=1.023613`, base z about
`0.006142 m`).

## Root cause

The first T1 result is a sampled navigation-execution failure, not proof of a
static scene blocker. Continuing the no-reset example plan after that error
keeps `plate_186` held, so later actions are cascading evidence. The ordinary
`safe_memory_benchmark_once` runner does not expose a physical checkpoint after
an action; its report and partial video cannot be used to resume T2.

## Resolution or current status

Keep the task scene and YAML unchanged. Future refrigerator diagnosis must use
the existing headless physical-session checkpoint/restore path: create an
immutable checkpoint after the desired T2 refrigerator placement, then restore
that checkpoint for removal and transport retries. Do not restart T1 for every
T2 trial. No valid T2 checkpoint was created by the interrupted runner.

## Reusable prevention and checks

- Retry a navigation failure at least once with identical files before editing geometry or parameters.
- After the first failed action in a no-reset run, ignore later action failures unless the environment is restored.
- Treat `report.json`, a partial scene, or a partial video as non-restorable unless the checkpoint manifest and socket restore validation agree.
- Preserve both decoded videos for a completed checkpoint-backed run.

## Relevant locations

- `entrypoints/configs/eval_safe_memory_quiche_wrap.yaml`
- `og_ego_prim/cli/safe_memory_benchmark_once.py`
- `og_ego_prim/cli/headless_manual_physical_session.py`
- `og_ego_prim/cli/headless_manual_physical_session_control.py`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260731_181131_quiche_fridge_navigation_full_retry/`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260731_181852_quiche_fridge_navigation_full_retry02/`
