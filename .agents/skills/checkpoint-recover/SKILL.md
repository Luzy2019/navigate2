---
name: checkpoint-recover
description: Reuse IS-Bench's existing physical checkpoint protocol to run headless OmniGibson benchmarks with off, save, or restore modes. Use when a benchmark must save after successful physical actions, resume from an immutable frame, preserve robot/gripper/camera/particle/timer state, carry scene-graph and planner memory across processes, validate a restored frame, or diagnose checkpoint and recovery failures. Apply to online_benchmark_once.py, PersistentPhysicalSession, OnlinePhysicalCheckpointManager, SAMJAM-UniGoal perception, starter physical primitives, and related output artifacts.
---

# Checkpoint Recover

Use this skill to operate and recover IS-Bench physical benchmark sessions. Keep the existing serializer and restore validator as the source of truth. Do not create a second ad-hoc simulator snapshot format.

## Scope And Guarantees

Treat a checkpoint as a complete executable frame, not as a saved list of planner actions. A valid frame must preserve enough physical and logical state to start a fresh headless process and continue without replaying completed actions.

Preserve these ownership boundaries:

- OmniGibson owns simulator geometry, object transforms, articulations, particle systems, and physics state.
- `PersistentPhysicalSession` owns physical serialization, entity remapping, robot pose, native RGB sensor pose, gripper state, symbolic carry, cooked-particle payloads, and restore validation.
- `OnlinePhysicalCheckpointManager` adds online benchmark state: scene-graph memory, SAMJAM/UniGoal tracking, scheduler state changes, planner episode history, tracker records, agent prompt state, and planner adapter state.
- `online_benchmark_once.py` owns the outer run loop. It saves only after `execute_plan(...)` reports a successful runtime outcome.
- `PerceptionSceneGraphUpdater` and the SAMJAM/UniGoal backends own current-frame perception state and long-term scene-graph identity memory.
- The runtime controller, scheduler, tracker, agent, and planner adapter remain authoritative after restore. Do not reconstruct them from a hand-written JSON annotation.

Use the skill for three distinct operations:

| Mode | Meaning | Checkpoint behavior |
| --- | --- | --- |
| `off` | Baseline uninterrupted run | Do not import or create the checkpoint manager; no checkpoint overhead or recovery |
| `save` | Run with recovery frames | After every successful high-level action, settle the physical scene and save an immutable frame plus a rolling latest pointer |
| `restore` | Continue from a saved frame | Start a fresh process, validate the explicit source frame, restore physical and logical state, then continue and save later successful actions |

Do not call a run complete merely because a process started, a PID exists, a `latest.pt` file exists, or a simulator screenshot was produced. Require the artifacts and validation gates in this skill.

## Non-Negotiable Rules

1. Keep the runner and task semantics requested by the user. For the model-driven online flow, use `og_ego_prim.cli.online_benchmark_once`, a model such as `gpt-4o`, `--primitive_type starter`, the requested real scene-graph backend, and `--prompt_setting v1` when specified. Do not silently replace it with `safe_memory_benchmark_once`, `eval_safe_*`, or an example planner.
2. Pass `--not_eval_awareness` when awareness must be disabled. Awareness is separate from scene graph perception, risk prediction, planner proposals, and physical execution.
3. Use `--physical-checkpoint-mode off|save|restore` explicitly. Never infer the mode from an old output directory.
4. In `save` mode, checkpoint only an action whose runtime outcome is executed and succeeded. Never checkpoint a risk-allowed action that failed in the executor, a blocked action, a proposal, or a partially completed low-level primitive.
5. Restore only from an explicit immutable `checkpoint/frame_NNNNNN.pt` unless the user explicitly requests a rolling pointer. Treat `latest.pt` as mutable and `previous.pt` as a rollback pointer, not as immutable evidence.
6. Restore into a new branch output directory and a new `--try_id`. Do not overwrite the source session, its immutable frame, its media, or its scene-graph evidence.
7. Match the task, scene, BDDL entity IDs, primitive type, scene-graph backend, planner adapter, and relevant runtime configuration before restore. A same-looking filename or stale cache is not identity proof.
8. Require `checkpoint/restore_validation.json` with `status=passed` before issuing the next planner action. A successful process launch is not restore proof.
9. Keep exactly one Isaac/OmniGibson process as the owner of a session socket or GPU allocation. Probe `nvidia-smi` and process state first. Do not kill an unrelated process unless the user explicitly authorizes the exact PID.
10. Never edit a saved timer, held-object field, particle count, or scene graph by hand to make validation pass. Restore the earlier frame before the timer-creating action and replay only the intended later action when a duration or state must change.
11. Treat a failed run as evidence, not as a valid recovery point. Save the last successful immutable frame, preserve its logs and images, and resume from that frame rather than replaying from frame zero or duplicating actions.
12. Keep secrets out of logs and documentation. Source `entrypoints/env.sh`, which loads local credentials as configured; do not print `env.local.sh` or API keys.

## Standard Workflow

### 1. Inspect Before Running

Read the live implementation and task files before changing or recovering anything:

- `og_ego_prim/cli/online_benchmark_once.py`
- `og_ego_prim/cli/online_physical_checkpoint.py`
- `og_ego_prim/cli/headless_manual_physical_session.py`
- `og_ego_prim/scene_graph/perception_scene_graph.py`
- `og_ego_prim/scene_graph/backends/samjam_sam2.py`
- `og_ego_prim/scene_graph/backends/samjam_unigoal.py`
- `data/tasks/composite/<task>.json`
- `data/bddl/<task_name>/problem<activity_definition_id>.bddl`

Confirm the live task metadata before trusting paths. A v3 task JSON may intentionally report an internal v1 `task_name`, so the output directory can contain `v1` while the invocation and manifest identify v3.

Resolve these questions before launch:

- Is this an uninterrupted `off` baseline, a new `save` run, or a restore branch?
- Which exact immutable frame is the source of the restore?
- Does the source frame belong to the same task and scene?
- Is the source process finished, and is the source checkpoint no longer being written?
- Is the task object/entity set identical to the current BDDL?
- Is the requested perception backend capable of checkpoint state restore?
- Is a physical grasp, symbolic carry, particle payload, or pending scheduler process present at the source frame?

Use scoped project searches only. Never run `find`, `rg`, `grep -r`, `fd`, or `tree` from `/home`, `/home/lzy`, `$HOME`, or `/`.

### 2. Preflight The Task And Environment

Use the IS-Bench interpreter and validate the task contract:

```bash
export ISBENCH_PYTHON=/home/lzy/anaconda3/envs/isbench/bin/python
export PATH="/home/lzy/anaconda3/envs/isbench/bin:${PATH}"

"$ISBENCH_PYTHON" scripts/test_new_task.py \
  --task <task-id-or-task-json> \
  --scene <scene> \
  --validate-only
```

Require `task preflight passed`. Treat this as contract readiness only; it does not prove physical success or checkpoint restore compatibility.

Probe the environment without exposing credentials:

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
```

Source the project launch environment in the same shell used for the run:

```bash
source entrypoints/env.sh
source entrypoints/launcher.sh
```

Use the project interpreter, not `/usr/bin/python3`. If CUDA is temporarily unavailable, apply the bounded fresh-process retry procedure from the project `wait-for-cuda` skill; do not silently switch devices or kill another user's process.

### 3. Run The Baseline Or Create Checkpoints

Use the command templates in [commands-and-checklists.md](references/commands-and-checklists.md). The model-driven physical baseline must include:

```text
--model <model>
--primitive_type starter
--scene_graph_backend samjam_unigoal
--scene_graph_step_interval 0
--prompt_setting v1
--not_eval_awareness
```

Set `--physical-checkpoint-mode off` for the first uninterrupted baseline. Set `save` only after the requested off configuration is understood, or when recovery is explicitly needed. Keep a unique `--try_id` for every branch.

Do not use an example plan as a fallback when checkpoints are requested. The online checkpoint manager requires a model planner agent and rejects example planning in `save` or `restore` mode.

### 4. Save After A Verified Successful Action

The online runner calls `save_after_success(...)` only after the outer execution result is successful. The manager then:

1. Advance real hold frames controlled by `--physical-checkpoint-settle-steps` (default `30`).
2. Synchronize symbolic carry before and after every settle step.
3. Read the current scene graph and merge it into `GlobalSceneGraphAccumulator`.
4. Apply the successful action and drain scheduler state changes.
5. Write `checkpoint_scene_graph/frame_NNNNNN.json` with current and global scene memory.
6. Append the action, simulator step, physical settle diagnostics, and scene-memory summary to `checkpoint/session.json`.
7. Atomically write `frame_NNNNNN.pt` and `frame_NNNNNN.json` without overwriting an existing immutable frame.
8. Atomically update `latest.pt` and `latest.json`; copy the previous rolling pointer to `previous.pt` first.

A successful risk decision is not enough. A successful planner proposal is not enough. If the executor fails, no new action frame is valid and the previous immutable frame is the recovery boundary.

### 5. Restore Into A Fresh Process And Branch

Start the same task, scene, model, primitive, scene-graph backend, and prompt settings in a new output branch. Pass an explicit source frame:

```text
--physical-checkpoint-mode restore
--restore-physical-checkpoint /absolute/path/to/source/checkpoint/frame_000003.pt
```

The restore sequence is:

1. Rebuild the task and simulator in a fresh process.
2. Reject a non-online checkpoint or an unsupported schema.
3. Initialize any checkpointed particle systems before loading simulator state.
4. Remap saved transient simulator object names to the current task entities and reject mismatched BDDL IDs, missing objects, duplicate names, or stale robot state.
5. Load OmniGibson simulator state.
6. Restore robot base pose, orientation, joint positions, native RGB sensor pose, and first-view focus information.
7. Synchronize the executor/controller state.
8. Clear unexpected grasp records and restore the expected physical gripper attachment.
9. Restore symbolic carry and cooked-particle payloads, including local-frame payload data that cannot safely be loaded as an empty instancer.
10. Validate robot pose, camera pose, every task-object pose/orientation/open state, held object, symbolic carry, particle-system counts, and cooked-particle containment.
11. Restore the scene-graph updater, SAM2 native tracking frames, SAMJAM identity memory, UniGoal mapping memory, global scene accumulator, object registry, scheduler pending processes, planner episode, tracker, agent state, and planner adapter state.
12. Render the restored observation without treating rendering as an action.
13. Write `checkpoint/restore_validation.json` and stop if its status is not `passed`.

Do not replay completed actions after restore. Resume the planner from the restored history. Python generators and in-flight LLM iterator objects are deliberately rebuilt from restored tracker and planner state; do not claim that an interrupted network request resumed byte-for-byte.

### 6. Verify Before Continuing

Inspect all of the following before the next action:

- `checkpoint/restore_validation.json`: physical validation passed, expected frame index, held object, particle counts, controller synchronization, and online runtime diagnostics.
- `checkpoint/session.json`: restored source path, completed action count, active subtask, and branch metadata.
- `checkpoint/frame_NNNNNN.json`: source frame manifest and task entity IDs.
- `checkpoint_scene_graph/frame_NNNNNN.json`: current scene graph versus accumulated global memory.
- Native current-frame PNG and replay media, if produced.
- Runtime/control socket or status response, if using the persistent manual-session service.
- GPU/process ownership: exactly one active session owner.

Only after these agree may the next planner action be submitted. Distinguish “restore acknowledged”, “restore validation passed”, and “one post-restore action succeeded”; they are separate milestones.

### 7. Finish And Report Honestly

At the end, retain `report.json`, `runtime_timeline.jsonl`, `replay_manifest.json`, decoded replay media, checkpoint manifests, restore validation, and relevant scene-graph artifacts. Report separately:

- contract validation;
- simulator initialization;
- perception/backend initialization;
- checkpoint save success;
- checkpoint restore validation;
- post-restore action progress;
- final task goal evaluation;
- media decode and visual evidence;
- process exit or headless cleanup errors.

Do not label an init-only run, a failed executor run, a process launch, or a checkpoint write as full physical validation.

## State Coverage And Limits

Expect these logical groups in an online checkpoint:

### Physical State

`sim_state`, robot pose and joint positions, native RGB sensor pose, first-view target focus, task-object poses/orientations/open state, simulator object registry, physical held-object ID, gripper state, symbolic carry diagnostics/checkpoint, cooked particle payloads, particle-system counts, executor global step, and scheduler serialization.

### Scene Graph And Perception Memory

`PerceptionSceneGraphUpdater` state includes backend identity, current/latest result, current snapshot, state-tracker previous/current values, task entities, manipulation-event history, perception errors, held-object name, and backend checkpoint state. SAMJAM preserves logical IDs, relations, task categories, frame indices, and the native JPEG frames needed to rebuild tracking input. It deliberately does not serialize model weights or stale GPU predictor objects. SAMJAM-UniGoal wraps SAMJAM state together with UniGoal mapping state and the last frame/results.

### Planner And Runtime Memory

Save and restore controller latest scene/changes, visible entity IDs, rethinking/proposal counters, pending scheduler state changes, last review/outcome/risk latency, planner episode history, tracker records and plan counts, agent prompt records and pending manipulation, planner adapter state, active subtask, and session action history.

Do not expect a checkpoint to preserve an external API server's hidden state, an in-flight HTTP request, Python generator execution position, video encoder process, or arbitrary files outside the session. Rebuild generators from restored planner history and rerun only the next action.

## Failure Boundaries

Classify failures before changing anything:

- **Contract failure**: task preflight, BDDL, entity IDs, or primitive arity mismatch. Fix the task/config or stop; do not restore.
- **Init failure**: OmniGibson, CUDA, scene resource, or stale cache problem. Preserve logs and diagnose initialization; no physical checkpoint exists yet.
- **Perception failure**: missing/duplicated/stale nodes or mismatched backend state. Inspect current/global graph and SAMJAM/UniGoal logs; do not hand-edit a final graph into a checkpoint.
- **Risk/planner failure**: blocked/unsafe proposal or planner parse issue. A risk-allowed proposal still needs executor success before save.
- **Executor failure**: gripper, first-view alignment, navigation, placement, particle, or physics failure. Resume from the last successful immutable frame, not from the failed action.
- **Restore validation failure**: task mapping, pose, camera, gripper, carry, particle, timer, scene graph, or planner state mismatch. Do not continue; compare source and rebuilt task/config, then create a new branch after the cause is corrected.
- **Cleanup/media failure**: top-down occupancy metadata, video encoding, or headless viewport cleanup failed after runtime evidence was written. Report it separately from task execution and inspect `report.json`/timeline before rerunning.

For timer changes, restore a frame before the action that creates the timer, remove only later derived artifacts in an authorized branch, submit the timer-creating action again, and verify `ready_step - start_step` against the requested duration. Never alter a pending timer in a later checkpoint by hand.

## References

- Read [isbench-checkpoint-reference.md](references/isbench-checkpoint-reference.md) for file/symbol ownership and payload details.
- Read [commands-and-checklists.md](references/commands-and-checklists.md) for copyable headless commands, preflight checks, save/restore templates, artifact checks, and a recovery decision matrix.
- Reuse the project `scoped-file-search` skill for every recursive search.
- Use the project `wait-for-cuda` skill only for bounded transient CUDA recovery.
