# Fixed-scene installation must preserve runtime removal ownership

- Recorded at: 2026-07-31T11:52:30+08:00
- Scope: Beechwood fixed-scene refreshes that use `scene_file_remove_objects` and traversability-map footprint release
- Trigger: difficult-problem and repeat-risk

## Context and symptom

For `lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3`, an initialization audit successfully saved a settled candidate after applying task-local chair and door removals. Reloading that candidate logged that all seven removal targets were already absent. The physical scene then lacked the obstacles, but the normal removal path no longer had their AABBs available to release the corresponding static trav-map cells.

## Root cause

Verified: `save_task()` serializes the post-load physical scene, while the static traversability map is not made equivalent by that save. The runtime removal hook owns both physical deletion and map-footprint release. Permanently baking only the deletion into the cache splits those coupled layers.

## Resolution or current status

Installed only the four audited task-object root poses (`carton_180`, `tablespoon_185`, `rag_182`, and `compost_bin_178`) into the canonical scene. Kept the original doors and chairs in that file so the task JSON continues to remove them and release their exact map footprints at load time. The installed scene passed JSON diff verification, task preflight, and a fresh 300-step whole-scene audit with `machine_pass=true`.

## Reusable prevention and checks

- Do not install a post-removal settled scene as canonical when removal hooks also update the trav map.
- Persist only state whose ownership is entirely in the scene JSON, such as settled target-object poses and orientations.
- Keep physical removals and their map-footprint updates together in the task-local runtime configuration unless map state is explicitly serialized and verified.
- After any scene installation, load the installed file with the task configuration and verify the removal log contains both physical removal and trav-map update entries.

## Relevant locations

- `og_ego_prim/benchmark/online_benchmark.py`
- `scripts/scene_initialization_audit.py`
- `data/tasks/composite/lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3.json`
- `results/lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3___Beechwood_0_int/20260731_113610_installed_scene_audit/`
