# Dryer wet-lint task: separate scene, navigation, grasp, and placement failures

- Recorded at: 2026-07-12T23:35:21+08:00
- Scope: `lifelong_crossroom__beechwood__dryer_wet_lint_interlock_v1`, physical starter, Beechwood cross-room navigation
- Trigger: difficult-problem and repeat-risk

## Context and symptom

The dryer wet-lint task repeatedly reached different failure points even though the task objects were no longer resampled. T1 and T2 became repeatable, while T3 alternately failed when grasping the old dryer screen, rotating while carrying it near the washer, or placing the washed screen on the office desk.

Verified observations from the debugging runs:

- `online_object_sampling=false` fixed scene objects, but navigation goal poses and relation poses were still sampled at runtime.
- Removing `door_lvgliq_1` and `door_lvgliq_2` and updating the traversability map made the office-corridor-utility room chain navigable.
- A route-only run carried a lint screen through `desk -> corridor -> washer -> sink -> corridor floor -> office floor -> desk` successfully.
- Fixing lint-screen orientations to identity broke a native grasp that had previously succeeded; fixed positions were retained and the orientation overrides were removed.
- With the correct dryer config, T1 and T2 repeatedly succeeded. Omitting that config restored `clearance_aware_simplify=true`, collapsed narrow-doorway A* corners into long segments, and caused an unrelated held-object navigation fall.
- One T3 run grasped the old dryer screen successfully, then executed a redundant `NAVIGATE_TO(washer)` while already inside the accepted radius. The resulting in-place rotation raised the base from about `0.018 m` to `0.267 m`; the subsequent fixed washer placement failed because the robot had already fallen.
- Another T3 run stopped about `1.376 m` from the old dryer screen. The inherited `1.2 m` symbolic grasp envelope forced eight native stance samples, all rejected by room or collision checks.
- A user-authorized task-local `symbolic_grasp_max_goal_radius: 1.4` trial accepted stable distances of about `1.229 m` and `1.366 m`. Both old-screen grasps succeeded, direct placement on the washer succeeded, the dry spare was installed, the dryer started, and the old screen was washed and carried back to the office.
- That `1.4` run still did not complete the whole task: the final sampled `PLACE_ON_TOP(desk)` pose did not satisfy native `OnTop`. T1/T2 passed, T3 failed, and `SR_L=SSR_L=0.6667`.

## Root cause

Several independent sampling and execution layers were initially conflated:

1. Fixed scene sampling controls object initialization only. It does not fix navigation target poses, native grasp stances, or symbolic relation poses.
2. `NAVIGATE_TO(object)` may finish with `reachable=false` because explicit object navigation can accept a visible radius without guaranteeing manipulation reach. A later `GRASP` can therefore need a second target sample.
3. Repeating navigation is useful only when the first result is outside the required manipulation envelope. Repeating it while already accepted and carrying an object can trigger a hazardous in-place rotation.
4. A configured placement coordinate is not valid merely because its XYZ looks plausible; the native `OnTop` predicate must be true after settling.
5. Fixed object orientation can invalidate the asset's native grasp pose even when its position is correct.
6. Scripted example planning does not require the SAM/UniGoal scene graph. Accidentally enabling `samjam_unigoal` consumed the remaining GPU memory and failed during initialization, before any task action.

## Resolution or current status

- Keep `online_object_sampling=false` and reuse the cached scene.
- Keep the three validated washer positions and do not restore the failed identity orientation overrides.
- Keep the task room-chain door removals and their traversability-map updates.
- Run with `entrypoints/configs/eval_safe_memory_dryer_wet_lint.yaml`, which preserves raw A* corner points and uses the `tucked_high` navigation arm pose.
- Keep the task-local `symbolic_grasp_max_goal_radius: 1.4`; do not change the shared starter baseline.
- Keep the T3 removal of the redundant washer navigation immediately after the first old-screen grasp; direct fixed-slot placement succeeded while the base remained level.
- Keep the return relays through `floor.n.01_2` and `floor.n.01_3`; the full held-screen return route succeeded.
- Remaining unresolved issue: make the final old-screen desk placement deterministically satisfy `OnTop`; the latest complete test failed only at this final relation.

## Reusable prevention and checks

- First classify a failure as scene initialization, navigation goal sampling, manipulation reach, carried-object physics, relation sampling, or goal evaluation. Do not use one broad patch for different layers.
- After `NAVIGATE_TO(target)`, inspect `end_reachable`, base-target distance, base height, and heading. If manipulation reach is still false, allow one additional same-target navigation sample before changing a radius. Do not add an unconditional duplicate navigation.
- If the robot already lies inside the accepted target radius while carrying an object, inspect whether navigation would only rotate in place; remove or skip that redundant action when the next symbolic placement can execute directly.
- Prefer a previously successful trajectory and relay coordinates over repeatedly changing global navigation parameters.
- Keep task-specific parameter changes in the task-specific YAML and require measured evidence. Modify shared defaults only after cross-task regression evidence.
- For placement, record the successful settled pose and verify the actual predicate. If a sampled `OnTop` fails, inspect video and relation state before hard-coding XYZ.
- Do not add held-object collision suppression against all fixed scene objects. A trial added 247 pairs, requested roughly 5.7 GB more PhysX GPU memory, and crashed the CUDA context. Keep only the paired carried-object-to-robot collision filtering already used by symbolic carry.
- For scripted example-planning regressions, pass `--scene-graph-backend disabled` to avoid irrelevant model memory use.
- Treat CUDA initialization/OOM as a separate resource failure: probe, wait, re-probe, and retry in a fresh process without killing other users' GPU processes.
- Every environment run, successful or failed, must retain decodable `video.mp4` and `topdown.mp4`, plus `README.md`, `console.log`, and `report.json` under one timestamped `results/` tree.
- Do not judge route-probe runs by SR/SSR when their purpose is only to establish physical reachability and coordinates.

## Relevant locations

- `data/tasks/composite/lifelong_crossroom__beechwood__dryer_wet_lint_interlock_v1.json`
- `entrypoints/configs/eval_safe_memory_dryer_wet_lint.yaml`
- `og_ego_prim/primitives/starter_primitives.py`
- `results/dryer_wet_lint_route_loop_v4_20260712/`
- `results/dryer_wet_lint_final_fixed_positions_relays_20260712/`
- `results/lifelong_crossroom__beechwood__dryer_wet_lint_interlock_v1___Beechwood_0_int/20260712_224108_t3_direct_old_filter_placement_final/`
- `results/lifelong_crossroom__beechwood__dryer_wet_lint_interlock_v1___Beechwood_0_int/20260712_230531_grasp_radius_1p4/`
- `--config entrypoints/configs/eval_safe_memory_dryer_wet_lint.yaml --scene-graph-backend disabled --save-video --save-topdown-video`
