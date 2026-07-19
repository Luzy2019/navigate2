# Require approval for semantic task changes and separate physical validation from scores

- Recorded at: 2026-07-13T13:26:28+08:00
- Scope: IS-Bench BDDL, task JSON, scene-cache repair, example planning, and runtime acceptance
- Trigger: repeat-risk and user-defined workflow policy

## Context and symptom

While repairing a cross-room task, replacing `storage_room_0` with `utility_room_0` made the scene easier to populate but also changed the room chain, support object, scene sampling, task wording, and route assumptions. This creates review drift: a task may run while no longer matching the original definition the user intends to audit.

The user also clarified that exact BDDL goal satisfaction and aggregate `G_task`, `G_safe`, `SR`, or `SSR` scores are not mandatory for task-build validation when the intended action skeleton and physical behavior complete correctly.

## Root cause

Verified process issue: runtime convenience fixes at the target-object, target-room, planning, or task-definition layers can silently broaden into semantic changes. Predicate metrics also conflate symbolic relation tolerances with whether navigation and manipulation were physically successful.

## Resolution or current status

Use this modification priority, from lowest to highest semantic risk:

1. Object initial position, orientation, or goal position.
2. Object quantity, including particle count.
3. Target object.
4. Target room.
5. Example planning.
6. Task definition.

Position/orientation and particle-count repairs may be attempted task-locally when they preserve semantics. Before changing a target object, target room, example plan, or task definition, present the evidence and proposed change to the user and obtain explicit approval. Do not infer approval from a request to make the task runnable.

Runtime acceptance prioritizes completion of the intended navigation, grasp, placement, wipe, wash, and storage skeleton plus physical consistency. A goal predicate or score may remain false without blocking acceptance when the full workflow is visibly and physically correct, but the mismatch must still be reported.

## Reusable prevention and checks

- Before editing, classify each proposed change against the six-level priority and identify every downstream BDDL, task JSON, scene-cache, route, and plan consequence.
- Treat target-object, target-room, example-planning, and task-definition changes as approval gates. Pause that edit until the user chooses.
- Preserve the original room chain and object roles while testing lower-risk pose, orientation, and particle-count changes first.
- Validate the action skeleton directly: navigation completes without falls, grasp constraints remain valid during transport, placements preserve support or containment, and wipe/wash actions execute in the intended order.
- Validate physical state propagation: carried-object constraints remain coherent, water or other particles move/update with their container or target, and task objects do not teleport, fall through supports, or destabilize the robot.
- Record `G_task`, `G_safe`, `SR`, and `SSR`, but do not require perfect values when a false atom is only a strict relation-tolerance mismatch and the physical workflow completed correctly.
- Do not dismiss a metric failure automatically. Distinguish harmless predicate tolerance from a real missed action, broken constraint, stale particle state, wrong room, or physically invalid final state using reports and both videos.

## Relevant locations

- `data/bddl/<task>/problem0.bddl`
- `data/tasks/composite/<task>.json`
- `data/scenes/<scene>/json/<task-scene>.json`
- `results/<task>___<scene>/<timestamp>_<purpose>/report.json`
- `results/<task>___<scene>/<timestamp>_<purpose>/video.mp4`
- `results/<task>___<scene>/<timestamp>_<purpose>/topdown.mp4`
