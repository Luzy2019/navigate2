# Post-sampling whole-scene visual audit is an initialization gate

- Recorded at: 2026-07-18T17:30:42+08:00
- Scope: scene initialization after online object sampling, and first or post-edit acceptance of any sampled/cached scene before runtime execution
- Trigger: user-required gate, difficult-problem, and repeat-risk

## Context and symptom

The repository already required checking sampled objects after scene sampling,
but the earlier note did not define coverage, evidence, or a pass/fail gate. A
scene can exit sampling successfully while a chair is overturned, food is on the
floor, a bottle or cleaner is inverted, a sponge intended for a table is on the
floor, a tabletop object is floating or embedded, a floor sponge or prop is
suspended, a container has the wrong open/closed
state, or an unrelated object is in an implausible location. A camera crop
inferred from the robot start or trajectory can also omit an entire task room.

The oil-rag bring-up exposed both classes of false positive. The first automatic
top-down capture covered only the robot-start area and was rejected as whole-
scene evidence. A later candidate used explicit bounds `[-10.0, -7.0, 2.0,
6.0]`, but the live all-object extent reached approximately
`[-11.96, -7.59, 2.60, 6.97]`; the smaller image therefore remained cropped
and was not acceptable as whole-scene evidence. Keep its artifacts only as a
rejected coverage example:

`results/lifelong_crossroom__beechwood__oil_rag_closed_storage_v1___Beechwood_0_int/oil_sample_storage1_v4_visual_global/`

The later runtime audit found a second false positive that a still image could
not show: the saved sponge had about `0.062 m/s` linear and `0.551 rad/s`
angular velocity and then moved about `0.0504 m` during the first 336 simulator
steps. The runtime displacement was not a pure initialization-idle probe, but it
corroborated that the canonical sample had been saved with nonzero velocity and
therefore required a separate idle-settle check before acceptance.
The same audit found 106 lubricant particles at an old world position while
the task-local bottle pose had been moved elsewhere; the BDDL `filled` claim
therefore did not provide a usable runtime payload for `SPREAD`.

## Root cause

1. `capture_topdown_scene` defaults to bounds derived from scene-graph points
   and robot trajectories. That is useful for a local trajectory view, not proof
   that every room and object is covered.
2. One top-down view cannot establish vertical orientation, support contact,
   penetration, floating gaps, or articulated open/closed state.
3. Scene metadata, BDDL instance names, and rendered identities can drift
   independently. A visual-only review can miss a wrong model, scale, room, or
   `_03` instance typo.
4. Native predicates and action return codes do not prove initialization
   validity. A later `GRASP` can retrieve an object that already fell, and a
   `filled` declaration can coexist with particles that are not inside the
   declared container.
5. Dynamic cloth, sponge, and particle systems can move during refresh frames;
   an immediate screenshot is not a settling or persistence proof.
6. Post-load task adjustments can invalidate the sampled-scene evidence. In the
   oil-rag task, `object_initial_poses` ran before an unconditional task-local
   `OnTop.set_value`; OmniGibson's `OnTop` setter calls `sample_kinematics` and
   moved both cartons to new random points on their floors. The candidate JSON,
   the screenshot, and a later canonical reload can therefore describe three
   different initial states unless the complete post-load sequence is audited.

## Resolution or current status

Treat the following procedure as a mandatory gate immediately after Object
sampling returns, and before any navigation, manipulation, task action, or
canonical scene installation:

1. **Freeze and identify the sample.** Preserve the raw sampled scene, task
   JSON/BDDL revisions, `online_object_sampling`, and a timestamped audit
   directory. Do not overwrite the canonical cache until the audit passes.
   Record the post-load pose/relation override order as part of the manifest.
   Re-run the gate after switching to `online_object_sampling=false`; the fixed
   cache must reload to the same audited state without a relation setter
   randomly resampling an already accepted explicit pose.
2. **Build the expected-state manifest first.** Derive each object's expected
   room, support/receptacle, height class, orientation, open/closed/toggle state,
   and fill/particle state from BDDL `:init`, task JSON `initial_setup` and
   `scene_info`, the sampled scene cache, and asset defaults. Do not infer
   "intended" placement only from what looks plausible in the rendered image.
3. **Establish explicit whole-scene coverage.** Derive bounds from every loaded
   room/floor AABB and every movable or dynamic object AABB, plus a margin.
   Record `world_bounds`, `bounds_source`, camera pose, camera height, output
   size, and floor z. Reject `scene_graph_robot_trajectory` bounds for a
   whole-scene claim. Confirm every loaded room and object lies inside the image
   and that the render is finite, non-empty, and not clipped. Task-room-only
   coverage is insufficient.
4. **Run a pure initialization-idle settle gate.** Keep the robot in hold and do
   not navigate, manipulate, toggle, open, close, or emit particles. Record pose,
   quaternion, AABB, support relation, linear velocity, and angular velocity at
   sampling return and over a declared continuous idle window. Use at least 30
   simulator steps for rigid scenes and at least 120 when cloth, sponge, fluid,
   or other particle systems are present, unless a documented domain check
   requires longer. Declare displacement, rotation, and velocity thresholds in
   the audit metadata before inspecting the result. Any threshold violation,
   support change, continued oscillation, or drift is `settle_unstable`.
5. **Capture complementary views after the idle gate.** Save one global
   top-down image, a crop with recorded bounds for every loaded room, and
   low-angle/oblique views for every orientation-, height-, containment-, or
   articulation-sensitive object. Save a readable object overlay; split crowded
   labels into room/object views when necessary. A file existing is not evidence
   that its subject is inspectable or that a human opened and reviewed it.
6. **Cross-check metadata.** For every BDDL object, every movable/dynamic object,
   and all loaded furniture/appliances, record rendered instance name/path,
   model/category, scale, room/floor, position, quaternion, AABB/extent, velocity,
   and support/receptacle where available. Compare the overlay with
   `metadata.task.inst_to_name` and scene JSON. Distinguish cloth mesh vertices
   from system particle counts.
7. **Run the physical checklist.** Require both images and
   machine-readable checks for:
   - intended room/floor/support, with no task object in another room;
   - full AABB support, native `OnTop`/`Inside`/`Touching` where applicable, no
     floating gap, floor embedding, penetration, or unintended overhang;
   - upright/stable furniture and appliances, base-down bottles/cleaners,
     support-aligned boards/trays, finite normalized quaternions;
   - intended open/closed and toggle states, correct fill/empty state, and no
     orphan or misplaced particle pile;
   - grounded finite robot pose, reachable object placement, and removal of
     both collision geometry and trav-map footprints for removed doors/chairs;
   - unrelated food, cookware, bottles, cleaners, sponges, chairs, and floor
     props in plausible locations and orientations.
8. **Require an explicit human review record.** The reviewer must open every
   global, per-room, oblique, and overlay image; mark each loaded room and each
   expected-state item `PASS` or `FAIL`; and record reviewer identity, review
   time, image list, and findings. Missing `human_visual_review_pass=true` is an
   automatic rejection. Image generation alone is never a completed review.
9. **Classify findings.** `PASS` requires full coverage, the explicit human
   visual review record, metadata consistency, and idle-settle checks. `REPAIR`
   permits task-local
   position/orientation/scale/particle-count edits that preserve semantics. A
   missing support/asset, wrong room/object role, or semantic drift is an
   approval blocker, not a pose tweak. Keep capture success, human review, and
   full runtime success as separate states.
10. **Retain evidence.** Keep global/room/oblique images, per-view metadata,
   overlay, sampled JSON, hashes, and the short checklist under one timestamped
   `results/` directory. Renderer/UI warnings are non-blocking only when the
   process exits cleanly, `error_stack=[]`, and the visual/metadata gates pass.
   Init-only success must never set `physical_validation_complete=true`.

## Reusable prevention and checks

- Require `bounds_source=explicit_world_bounds` (or a verified all-floor scene
  extent) for whole-scene claims; reject trajectory-only captures.
- Make `capture_success`, `coverage_pass`, `human_visual_review_pass`, and
  `runtime_pass` explicit, independent statuses.
- Require every loaded room to be opened and reviewed, and every BDDL object to
  be readable in at least one room/object view, not merely present as a tiny
  global overlay point.
- Check finite pose/quaternion/AABB/velocity values, native support and state,
  room mapping, particle counts and attachment, and post-refresh drift.
- Record a pure idle window before any task action; runtime motion cannot replace
  initialization-idle evidence.
- Preserve evidence before canonical installation and recapture after every
  task-local scene repair. Keep `online_object_sampling=false` for an accepted
  cache unless a new sample is necessary.
- Treat relation setters as mutating samplers, not assertions. Compare the pose
  before and after every task-local `OnTop`/`Inside` setter, and remove a
  redundant setter or replace it with a validated deterministic pose when it
  relocates an audited object. Verify the native relation again after idle.
- Audit unrelated objects as well as task objects; an implausible roast or
  cleaner on the floor is an initialization failure even when task atoms pass.

## Relevant locations

- `.codex/.skills/Inspection-Skills/notion01.md`
- `og_ego_prim/utils/topdown_capture.py` (`capture_topdown_scene`, bounds resolution)
- `og_ego_prim/benchmark/online_benchmark.py` (post-load scene adjustments)
- `scripts/test_all.py` (initial top-down capture path)
- `data/tasks/composite/<task>.json`
- `data/bddl/<task>/problem0.bddl`
- `data/scenes/<scene>/json/<task-scene>.json`
- `results/<task>___<scene>/<timestamp>_<purpose>/`
