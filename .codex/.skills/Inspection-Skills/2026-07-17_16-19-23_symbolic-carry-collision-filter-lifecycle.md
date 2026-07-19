# Symbolic-carry collision filters need explicit ownership and cleanup scope

- Recorded at: 2026-07-17T16:19:23+08:00
- Scope: physical starter symbolic carry, USD filtered-pair lifecycle, repeated grasp/release episodes
- Trigger: difficult-problem and repeat-risk

## Context and symptom

The jar canning task repeatedly grasps the same water-filled mug. Recreating all 50 mug-to-robot filtered pairs on every grasp led to unstable PhysX behavior while the simulator was playing, while broadly suppressing carried-object collisions against scene geometry had previously caused excessive GPU memory use. Articulated containers add another constraint: mutating their root filtered-pair relationships during simulation can invalidate PhysX tensor views.

The task needed repeated mug carry without changing the default behavior used by already working tasks.

## Root cause

Verified: collision filtering is a lifecycle and ownership problem, not a reason to disable arbitrary scene collisions.

- The default `release` path owns only pairs added for the current grasp and removes them on PLACE or RELEASE.
- A repeatedly grasped task object may need an `episode` registry so each new pair is added once and removed only after the simulator stops.
- Pre-existing pairs must not be claimed by the registry.
- An add or remove call can mutate a USD relationship before raising. Forgetting an uncertain or failed pair leaks state into later actions.
- Articulated roots cannot safely use the same playing-simulator pair mutation as rigid children.

## Resolution or current status

- The shared default remains `symbolic_carry_robot_collision_filter_scope: release` with no object allowlist. An empty legacy retain allowlist also normalizes to `release`.
- `eval_safe_memory_jar_seal.yaml` enables `episode` only for `mug.n.04_1`.
- The registry is keyed by object-link and robot-link prim paths. It owns only newly added pairs; uncertain partial adds are conservatively owned for cleanup.
- Episode pairs are retained across mug releases and removed after `og.sim.stop()`. Failed cleanup entries remain registered so `close()` can retry.
- If a normal release-scope removal fails, ownership is transferred to the same lifecycle registry for a stop-boundary retry. Successful release behavior is unchanged.
- Articulated root filtering is skipped while playing. Rigid loaded descendants can still use ordinary release-scope pairs.

The final jar run logged `50` new mug pairs, then three reuses with `already_present=50`, followed by `collision_filter_cleanup requested=50 removed=50 failed=0`. A post-fix knife probe independently logged `scope=release`, `25` new tablespoon pairs, `removed=25 requested=25 deferred_cleanup=0`, and final registry size zero.

## Reusable prevention and checks

- Keep `release` as the public default. Use `episode` only for measured repeated-grasp instability and prefer a task-object allowlist.
- Do not configure `scope: episode` with an empty allowlist unless episode retention for every rigid carried object is intentional.
- Probe whether a pair already exists before adding it. Never remove a pre-existing pair that the controller did not create.
- Treat an add that raised after a possible relation update as owned until cleanup proves otherwise.
- Transfer failed release removals into retryable lifecycle ownership; do not clear the last reference to a possibly live pair.
- Stop the simulator before final registry cleanup. If stop or cleanup fails, retain the entry and report the failure rather than claiming success.
- Skip playing-simulator filtered-pair edits on articulated roots. Test loaded rigid descendants separately.
- Do not filter carried objects against all fixed scene objects. Preserve scene collision geometry and suppress only carried-object-to-robot pairs.
- Verify both scopes: focused fake-API failure tests plus one real non-allowlisted grasp/place run for the default release path.
- On CUDA or OOM reports, inspect host `nvidia-smi` first. Resource contention or sandbox-hidden devices are not collision-filter correctness failures.

## Relevant locations

- `og_ego_prim/primitives/starter_primitives.py`
- `og_ego_prim/benchmark/online_benchmark.py`
- `entrypoints/configs/eval_safe_memory_jar_seal.yaml`
- `tests/test_particle_transactions.py`
- `results/lifelong_crossroom__beechwood__jar_seal_status_after_canning_v1___Beechwood_0_int/20260717_154428_countertop_slot_full_t1_t3_final/`
- `results/lifelong_crossroom__beechwood__knife_hidden_in_hamper_v1___Beechwood_0_int/20260717_161336_release_scope_postfix_minimal/`
