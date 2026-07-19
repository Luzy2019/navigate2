# Cleaner storage route: model cloned obstacles, verify door angle, and open before grasp

- Recorded at: 2026-07-13T16:52:16+08:00
- Scope: Beechwood fixed-scene physical starter navigation and example planning
- Trigger: difficult-problem and repeat-risk

## Context and symptom

For `lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1`, an independent compact shelf clone was added at the back of `storage_room_0` while the source kitchen shelf remained unchanged. The cleaner was stable and `OnTop` the clone, but the first storage route could not sample any reachable navigation goal.

The default traversability map disconnected the storage room. Replacing it task-locally with `floor_trav_no_door_0.png` restored graph connectivity, but the default `goal_clearance_radius=0.25` still rejected every endpoint after the full Fetch footprint erosion. Lowering only that clearance admitted an unsafe 0.45 m stance because the cloned shelf did not exist in the static map.

After the clone AABB plus 0.10 m padding was rasterized as a task-local map obstacle and endpoint clearance was set to the equivalent two-pixel band, navigation sampled a safe 0.90 m stance. The robot then stopped at the storage doorway. Although the door's `Open` predicate was true, its joint angle was only `0.10035 rad`, so the physical panel still blocked the passage.

Setting the dining and storage route doors to the known valid `lvgliq` open angle `1.39853 rad` made the focused storage navigation and cleaner grasp succeed without a fall. The full example plan then failed independently in T1 because it grasped the cereal before `OPEN(bottom_cabinet)`. The runner continued into T2 while still carrying cereal, after which the robot left the floor at approximately `[24.88, 595.53, 501.38]` and produced a NaN gripper quaternion.

## Root cause

Four separate layers were involved:

1. The default static map encoded closed door barriers and disconnected storage.
2. A runtime-cloned object was absent from every dataset map, so endpoint selection did not account for its collision footprint.
3. The symbolic `Open` threshold accepted a small angle that was not physically wide enough for Fetch.
4. The example plan violated the starter precondition that an openable destination must be opened before the robot grasps the object to place inside it. Continuing after that error preserved an assisted-grasp constraint into unrelated navigation and caused physical divergence.

## Resolution or current status

- The focused storage route is fixed task-locally with `trav_map_variant: no_door`, the cloned shelf as a padded map obstacle, and `goal_clearance_radius: 0.20` while retaining the full `0.587 m` Fetch erosion.
- Both route doors load at `1.39853 rad`; the successful probe ended at base height `0.00146 m`, sampled a `0.90 m` goal, and recorded `object_in_hand=bottle_of_cleaner_181`.
- The full task remains blocked at an example-planning approval boundary. No plan reordering was applied. The minimum proposed T1 repair is to open the bottom cabinet before grasping the cereal, then grasp, navigate back, and place inside.

## Reusable prevention and checks

- When adding or cloning fixed furniture after dataset map generation, add its real runtime footprint to a task-local traversability obstacle layer before eroding for the robot.
- Account for grid quantization: at `0.1 m/cell`, `goal_clearance_radius=0.20` and `0.15` both require a two-pixel radius, while any value above `0.20` jumps to three pixels.
- Do not accept `Open=true` as doorway proof. Record the actual joint angle and inspect first-person and top-down video at the passage.
- For `PLACE_INSIDE(openable)`, ensure the destination is navigated to and opened before grasping the object when the primitive cannot open while holding.
- After a deterministic manipulation precondition error, do not interpret later navigation as independent evidence. A still-active grasp constraint can turn continuation into a fall, extreme coordinates, or NaN state.
- Validate route repairs with base height, endpoint radius, `object_in_hand`, and both decoded videos; do not use SR/SSR from a focused partial-action probe.

## Relevant locations

- `data/scenes/Beechwood_0_int/json/Beechwood_0_int_task_lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1_0_0_template.json`
- `data/tasks/composite/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1.json`
- `entrypoints/configs/eval_safe_memory_cleaner_food.yaml`
- `og_ego_prim/benchmark/online_benchmark.py`
- `results/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1___Beechwood_0_int/20260713_163313_cleaner_food_storage_shelf_route_full_open/`
- `results/lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v1___Beechwood_0_int/20260713_164420_cleaner_food_full_example_storage_shelf/`
