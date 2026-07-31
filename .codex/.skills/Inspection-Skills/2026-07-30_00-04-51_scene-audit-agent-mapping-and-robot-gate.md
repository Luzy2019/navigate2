# Preserve agent bindings across chained fixed-scene audits

- Recorded at: 2026-07-30T00:04:51+08:00
- Scope: `scripts/scene_initialization_audit.py`, fixed-scene candidate chaining, Fetch idle validation
- Trigger: difficult-problem and repeat-risk

## Context and symptom

A quiche task candidate completed a 120-step fixed-scene audit, but its saved `settled_scene` failed when used as the next `--scene-file`. OmniGibson raised `AssertionError: BDDL object instance agent.n.01_1 should exist in cached metadata`, even though the BDDL still declared the agent and the source candidate had loaded successfully.

The same audit also reported the Fetch robot as `settle_unstable` from large instantaneous linear and angular velocity values while its base moved only about `0.00023 m` and rotated about `0.00075 rad` over the complete idle window.

## Root cause

Verified: `BehaviorTask.save_task()` omitted the cached `agent.n.01_1 -> robot_*` entry from `metadata.task.inst_to_name`. A saved audit candidate therefore could not be chained into another fixed-scene audit unless that binding was inherited from the input scene.

Verified: articulated Fetch velocity readings can reflect active wheel or joint motion without corresponding base-pose drift. Applying rigid-object velocity thresholds directly to the agent produced a false unstable result.

## Resolution or current status

The audit now copies `agent.*` task mappings from the input candidate into both raw and settled audit scene files. It retains the BDDL agent initialization atom required by the loader, excludes agent instances from the ordinary scene-object mapping check, and validates the robot through the dedicated finite quaternion plus base displacement and rotation gate. Velocity thresholds remain active for non-agent objects.

The oblique camera was also raised above wall height after low in-room camera poses produced unreadable wall-only storage-room images.

## Reusable prevention and checks

- Before chaining a saved candidate, verify `metadata.task.inst_to_name` still contains every `agent.*` binding required by the BDDL.
- Do not delete the BDDL agent declaration or its loader-required initialization atom merely because native post-load `OnTop(agent, floor)` is false.
- Keep robot base displacement and rotation checks independent from ordinary rigid-object velocity checks; a stable pose must still pass finite and normalized quaternion gates.
- Reject a visual audit when any oblique image is wall-only or otherwise unreadable, even if image-file and coverage checks pass.
- Confirm a repaired settled scene can itself be loaded as `--scene-file`; successful generation alone is not chainability proof.

## Relevant locations

- `scripts/scene_initialization_audit.py`
- `data/bddl/lifelong_crossroom__beechwood__quiche_wrap_identity_v1/problem0.bddl`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260729_232100_fixed_scene_candidate_carton_v2_retry1/`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260730_001000_fixed_scene_candidate_carton_v6_visual/`
