# Cleaner-food task: keep semantics, scene geometry, maps, and validation aligned

- Recorded at: 2026-07-16T15:42:26+08:00
- Scope: `lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1`, Beechwood fixed scene, starter primitives, cross-room navigation and placement
- Trigger: difficult-problem and repeat-risk

## Context and symptom

The task must preserve this semantic chain: store cereal and remember the dirty floor sponge in `kitchen_0`, fetch and stage the dirty plate from `dining_room_0`, then fetch cleaner from `storage_room_0`, clean the plate with the rinsed sponge, and store cleaner away from the cereal.

The task initially failed across several independent layers:

- `storage_room_0` had no suitable shelf, but changing to another room would have changed the task definition and route.
- A map-only `no_door` variant could make A* think a route existed while physical door panels still blocked Fetch.
- Removed kitchen and dining chairs still occupied cells in the traversability map.
- Dining and storage routes ran too close to walls; the extended arm contacted walls even when A* returned a path.
- The bottom-cabinet approach was too close, so the open door could lift the robot base.
- Random or poorly chosen table placements made later navigation and grasping difficult, and visually plausible placements repeatedly failed native `OnTop`.
- The original cleaner model could not fit inside the top cabinet according to OmniGibson's intrinsic placement geometry.
- Early example planning grasped cereal before opening the destination cabinet, then continued after the precondition failure and produced unrelated falls and NaN state.

This record supersedes the final-solution status in `2026-07-13_16-52-16_cleaner-storage-route-and-plan-blockers.md`. That earlier file remains useful history, but its map-only `no_door` resolution must not be restored.

## Root cause

Verified causes were separated by layer:

1. **Semantic and scene layer:** the requested storage-room support object did not exist. Replacing the room would have hidden the scene defect by changing task semantics.
2. **Physical scene versus map:** editing only the traversability map does not remove collision geometry. Conversely, removing an object without releasing its exact map footprint leaves a false obstacle.
3. **Runtime-added furniture:** a cloned shelf is absent from the dataset map and must be added as an obstacle from its runtime AABB.
4. **Navigation geometry:** increasing stuck tolerance only delays a collision failure. Extra erosion can disconnect the approximately `1.185 m` storage passage, while aggressive path simplification cuts unsafe doorway corners.
5. **Robot configuration:** an extended navigation arm enlarges the real collision footprint beyond what a base-only path suggests. A cabinet stance that is too close can also destabilize the base when the door opens.
6. **Relation sampling:** a configured XYZ is not correct until the object settles, leaves the hand, and the native relation is true. Blindly changing z did not fix sponge `OnTop`.
7. **Container fit:** the old cleaner was about `0.039 x 0.124 x 0.296 m`, while the cabinet shelf clearance was only about `0.177-0.193 m`. Rotating it on the table did not change the base-aligned geometry used by the `Inside` sampler.
8. **Particle and process state:** automatic sponge particle removal could satisfy or destroy task state at the wrong time. Goal-score relaxation cannot replace actual dirt, stain, water, carry, release, support, or containment updates.
9. **Validation interpretation:** failures after a deterministic precondition error can be cascading environment corruption. A focused route or T3 fixture also cannot be judged by aggregate SR/SSR.

## Resolution or current status

- Kept `storage_room_0`. Cloned the small `shelf_owvfik_0` as fixed `shelf_owvfik_3` at the back of storage without moving the source shelf. The cleaner remained `OnTop` and `Touching`; after 20 hold steps its displacement was about `3e-5 m`.
- Added only `shelf_owvfik_3` to `trav_map_obstacle_objects` with `0.1 m` padding.
- Removed `door_ohagsq_1` and `door_lvgliq_4` from the physical scene during task initialization and released only their corresponding map regions. Removed the obsolete task `no_door` / map-variant configuration.
- Physically removed four kitchen-table chairs and six dining-table chairs, and released all ten exact footprints through `trav_map_free_objects` with zero padding.
- Kept navigation changes task-local: `goal_clearance_radius: 0.20`, soft clearance `0.40`, clearance weight `4.0`, extra erosion `0.0`, path simplification disabled, `cabinet_min_goal_radius: 0.85`, and navigation arm pose `tucked_high`.
- Corrected initialization and planning: cereal starts on the kitchen table, dirty sponge on the kitchen floor, dirty/stained plate on the dining table, and cleaner on the storage shelf. T1 opens the bottom cabinet before grasping cereal. Route doors are not operated by the plan after physical removal. There is no `WAIT`.
- Disabled the sponge's automatic `ParticleRemover` ability. Kept 20 dirt particles and 20 stain particles total; the executed wash/wipe flow still updates dirt, stain, and water state.
- Added deterministic near-edge table slots. Cleaner uses `[-3.05, -5.34, 0.779]` with horizontal quaternion `[0, 0.7071068, 0, 0.7071068]`. The initially tried `z=0.783` left the smaller cleaner suspended by about `4-5 mm`; `z=0.779` settled to about `0.775432` and supported a second grasp.
- With user approval, replaced `svzbeq / scale 1.0` with `ykzonz / scale 0.6`. The new measured extent is about `0.027 x 0.027 x 0.073 m`. The final focused T3 run completed table release, second grasp, open, `PLACE_INSIDE`, close, and `DONE`; the hand was empty after placement, final `Inside(top_cabinet)=true`, and `error_stack=[]`.
- The preceding full example-plan run had already completed T1, T2, and the T3 cleaning sequence, failing only at old-cleaner containment. Therefore current acceptance is combined evidence from that full run plus the final focused T3 blocker run, not a new single uninterrupted run of the final files.

Current limitations must remain explicit:

- The final sponge slot `[-3.7, -5.34, 0.775]` was added after the last full T1/T2 run. It has static test coverage but has not been exercised in a final runtime; prior successful sponge placements used the native `OnTop` fallback.
- First-person left/right visual sway and cabinet-facing composition remain unresolved by user decision. Top-down evidence showed no base snake motion or fall; do not claim the camera issue is fixed.
- The task-specific scene is under the repository's ignored `data/scenes/` tree, and the focused test file is currently untracked. Local success does not by itself guarantee those artifacts will be delivered through Git.

## Reusable prevention and checks

- Apply the semantic-change approval policy before editing. Keep room and object roles unchanged unless the user explicitly approves a target-object, target-room, example-plan, or task-definition change. Explain scope, risk, and alternatives before shared-code or new runtime-interface work.
- When a required fixture is missing, clone a suitable object without moving the source. Verify source and clone independently, make fixed furniture fixed-base, and add the clone's runtime footprint to the map.
- A removed door or chair must disappear from both collision geometry and its exact traversability footprint. Never treat a map-only `no_door` image as proof that the physical doorway is open.
- Diagnose `no path` separately from `path exists but robot cannot progress`. Inspect first-person frames, top-down video, contact pairs, base height, planned path, and trav-map overlays before changing erosion or tolerance.
- If a small target on a table repeatedly remains outside the `1.2 m` manipulation envelope, inspect the chairs around that table before widening a shared grasp radius. In Beechwood, the kitchen table commonly has four chairs and the dining table six. When those chairs are confirmed route or stance blockers, remove the physical chair objects and release each exact chair footprint from the traversability map; doing only one side leaves either collision geometry or false map obstacles behind.
- Prefer task-local soft-clearance cost, arm pose, and destination standoff over widening shared defaults. Do not raise stuck tolerance to hide collision, add hard erosion that disconnects narrow rooms, or simplify paths that cut doorway corners without a focused rerun.
- For small task objects, inspect initial position and orientation after scene load. Put later-stage table placements near a reachable edge while keeping their full AABB safely supported and separated from other task objects.
- For `OnTop` or `Inside`, verify the native predicate after settling. Also verify `object_in_hand` transitions, AABB/extent, contact or support, and successful later re-grasp; a plausible image or action return is not enough.
- Before repeatedly tuning pose or orientation for `Inside`, compare the asset's intrinsic/base-aligned bbox with the receptacle's usable interior. Treat a proven fit mismatch as a model/scale blocker and request approval for replacement.
- Goal atoms and SR/SSR may be relaxed only when the intended navigation, grasp, carry, wash, wipe, release, support, containment, particles, and no-fall physics are independently verified. Never use score relaxation to excuse a dropped object, stale particles, broken carry constraint, or fall.
- After a deterministic primitive precondition failure, stop or reset before interpreting later subtasks. Extreme coordinates or NaN transforms after continuation are usually cascading evidence, not new root causes.
- If the latest full run failed only in T3, rerun the smallest faithful T3 segment that covers the changed state. Report the result as combined validation and name any final-file changes that were not exercised.
- Every run, successful or failed, must retain decoded `video.mp4` and `topdown.mp4`, observations, `README.md`, `console.log`, and `report.json` under one timestamped results tree.
- Keep bootstrap and geometry-probe scripts under a clearly named results directory and state that they are diagnostic artifacts, not runtime dependencies. Avoid accumulating new scripts when existing reports, images, or focused actions can answer the question.

## Relevant locations

- `data/bddl/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1/problem0.bddl`
- `data/tasks/composite/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1.json`
- `data/scenes/Beechwood_0_int/json/Beechwood_0_int_task_lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1_0_0_template.json`
- `entrypoints/configs/eval_safe_memory_cleaner_food.yaml`
- `tests/test_cleaner_food_cabinet_location_task.py`
- `results/cleaner_food_storage_shelf_clone_20260713/`
- `results/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1___Beechwood_0_int/20260715_220255_cleaner_food_full_t1_t3_chair_travmap_tucked_high_r085/`
- `results/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1___Beechwood_0_int/20260716_123830_cleaner_food_t3_ykzonz_z0779_place_inside_targeted/`
- `.codex/.skills/Inspection-Skills/2026-07-13_13-26-28_require-approval-for-semantic-task-changes.md`
- `.codex/.skills/Inspection-Skills/2026-07-13_16-52-16_cleaner-storage-route-and-plan-blockers.md`
