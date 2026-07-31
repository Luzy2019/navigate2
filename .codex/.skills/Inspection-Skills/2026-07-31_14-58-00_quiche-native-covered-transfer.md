# Native Covered transfer before fixed-scene removal

- Recorded at: 2026-07-31T14:58:00+08:00
- Scope: Fixed OmniGibson scene repair with `Covered` particle systems and retired task-local supports
- Trigger: difficult-problem

## Context and symptom

Moving the quiche T1 dusty plate and broom from a storage coffee table to a living-room table required moving `dust` with the support. Deleting the old coffee table in the same active PhysX tensor session after `Covered.set_value` invalidated the tensor view, and the scene could not be saved.

## Root cause

Verified: `og.sim.batch_remove_objects()` removed a collision shape while the physics tensor view still referenced it. The failure is not evidence that the native coverage transfer or `OnTop` placement failed.

## Resolution or current status

Load the source cache without task runtime removals, call native `Covered.set_value(dust, False/True)` to transfer coverage, and use native `OnTop` setters for the moved objects. Save the scene before structural deletion. Then remove the retired support only from the saved JSON's `state.object_registry`, `objects_info.init_info`, and its now-empty dust group, while preserving agent metadata and runtime-owned door/chair removals.

## Reusable prevention and checks

- Do not manually offset visual-particle coordinates when changing a covered support; use the native state setter.
- Do not call `batch_remove_objects` on a support immediately before `save_task()` in the same PhysX tensor session.
- Verify the final fixed cache after reload: native `Covered` and `OnTop`, 120-step idle stability, no old support registry entry, and screenshots of the target room.
- Keep runtime door/chair removals in task JSON so their traversability-map footprint release remains coupled to deletion.

## Relevant locations

- `data/scenes/Beechwood_0_int/json/Beechwood_0_int_task_lifelong_crossroom__beechwood__quiche_wrap_identity_v1_0_0_template.json`
- `results/lifelong_crossroom__beechwood__quiche_wrap_identity_v1___Beechwood_0_int/20260731_145400_living_table_canonical_audit/`
