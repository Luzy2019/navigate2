# Checkpoint Recover Commands And Checklists

本文档提供可复制的命令模板。执行前替换尖括号参数；不要把 API key 写入命令、shell history、日志或最终报告。

## 目录

- 环境准备
- 任务预检
- off baseline
- save run
- restore branch
- artifact inspection
- restore acceptance
- failure matrix
- final report template

## 环境准备

在项目根目录 /home/lzy/code/IS-Bench 执行：

~~~bash
export ISBENCH_PYTHON=/home/lzy/anaconda3/envs/isbench/bin/python
export PATH=/home/lzy/anaconda3/envs/isbench/bin:$PATH
export ISBENCH_OPENAI_REQUEST_TIMEOUT_SECONDS=180

source entrypoints/env.sh
source entrypoints/launcher.sh
~~~

不要使用 set -x，不要读取 entrypoints/env.local.sh，不要通过 env | sort 打印 credential。source 后只验证非秘密配置是否存在。

检查 GPU 和旧进程：

~~~bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
~~~

只把当前用户明确授权的 PID 视为可清理目标。默认保留其他进程，先等待或按 wait-for-cuda 的 bounded retry 处理。

## 任务预检

先读取 task JSON 的 task_name、activity_definition_id、primitive_type、scene 和 BDDL 引用，再执行：

~~~bash
"$ISBENCH_PYTHON" scripts/test_new_task.py \
  --task <task-id-or-json-path> \
  --scene <scene> \
  --validate-only
~~~

接受条件：

- 输出 task preflight passed；
- primitive 与要求一致，例如 starter；
- object count、BDDLSampler 和 BDDL 路径属于当前 task；
- 没有因为同名 stale cache 载入另一个 task 的物体。

contract preflight 通过只说明任务可解析，不说明 scene graph、导航、抓取、粒子、timer 或 restore 正确。

## Off Baseline

先完成一次不带 checkpoint manager 的真实 runner baseline：

~~~bash
"$ISBENCH_PYTHON" -u -m og_ego_prim.cli.online_benchmark_once \
  --task <task-id-or-json-path> \
  --scene <scene> \
  --model <model> \
  --work_dir results \
  --try_id <unique-off-try-id> \
  --primitive_type starter \
  --scene_graph_backend samjam_unigoal \
  --scene_graph_step_interval 0 \
  --prompt_setting v1 \
  --not_eval_awareness \
  --physical-checkpoint-mode off
~~~

检查：

1. 控制台显示 HEADLESS=1，或 manifest 的 metadata.headless=true。
2. replay_manifest.json.runner 是 online_benchmark_once。
3. report.json.awareness 是 null（当要求禁用 awareness 时）。
4. planner、risk、executor 事件存在于 runtime_timeline.jsonl。
5. report.json 的 action、termination、error_stack 和 goal condition 与 timeline 一致。
6. off 目录没有因为旧 session 误读 checkpoint；off 模式应不创建 checkpoint manager 的 frame artifacts。

运行失败时，保存失败动作前最后一个成功动作的证据，但不要把 off run 伪装成可恢复 save run。

## Save Run

使用新的 try ID：

~~~bash
"$ISBENCH_PYTHON" -u -m og_ego_prim.cli.online_benchmark_once \
  --task <task-id-or-json-path> \
  --scene <scene> \
  --model <model> \
  --work_dir results \
  --try_id <unique-save-try-id> \
  --primitive_type starter \
  --scene_graph_backend samjam_unigoal \
  --scene_graph_step_interval 0 \
  --prompt_setting v1 \
  --not_eval_awareness \
  --physical-checkpoint-mode save \
  --physical-checkpoint-settle-steps 30
~~~

每次成功动作后确认：

~~~bash
ls -lh <run-dir>/checkpoint
ls -lh <run-dir>/checkpoint_scene_graph
~~~

接受条件：

- frame_000001.pt 等 immutable frame 的数量等于已成功保存的高层 actions；
- 每个 immutable frame 有对应 .json manifest；
- session.json.completed_actions 只包含 executed=true、succeeded=true 的 action；
- latest.pt 会更新，旧 rolling pointer 可在 previous.pt 找到；
- checkpoint_scene_graph/frame_NNNNNN.json 同时含 current graph 和 global memory；
- scheduler pending 和 particle payload 摘要与 action 后状态一致；
- failed/blocked action 没有产生新的 immutable frame。

如果需要每个成功动作都可分支，保留 frame_NNNNNN.pt、manifest、scene graph JSON 和对应 media，不要只保留 latest。

## Restore Branch

选择 source immutable frame 后，先确认其 manifest：

~~~bash
"$ISBENCH_PYTHON" -c 'import json, pathlib; p=pathlib.Path("<source-run>/checkpoint/frame_000003.json"); print(json.dumps(json.loads(p.read_text()), indent=2, ensure_ascii=True))'
~~~

使用全新的 output branch 和 try ID：

~~~bash
"$ISBENCH_PYTHON" -u -m og_ego_prim.cli.online_benchmark_once \
  --task <same-task-id-or-json-path> \
  --scene <same-scene> \
  --model <same-model> \
  --work_dir results \
  --try_id <new-restore-branch-try-id> \
  --primitive_type starter \
  --scene_graph_backend samjam_unigoal \
  --scene_graph_step_interval 0 \
  --prompt_setting v1 \
  --not_eval_awareness \
  --physical-checkpoint-mode restore \
  --restore-physical-checkpoint <source-run>/checkpoint/frame_000003.pt \
  --physical-checkpoint-settle-steps 30
~~~

Restore is not ready when only one of these is true：

- command returned or a process exists；
- GPU memory increased；
- latest.pt was created in the branch；
- a restored image was rendered；
- a control socket accepted a request。

Require all of the following：

~~~bash
test -f <branch-run>/checkpoint/restore_validation.json
test -f <branch-run>/checkpoint/session.json
test -f <branch-run>/checkpoint/latest.pt
test -f <branch-run>/checkpoint_scene_graph/frame_000003.json
~~~

Then inspect validation with structured JSON parsing：

~~~bash
"$ISBENCH_PYTHON" -c 'import json, pathlib; p=pathlib.Path("<branch-run>/checkpoint/restore_validation.json"); d=json.loads(p.read_text()); print(json.dumps({"status":d.get("status"),"physical_state":d.get("physical_state"),"frame_index":d.get("frame_index"),"held_object":d.get("held_object"),"online_runtime":d.get("online_runtime")}, indent=2, ensure_ascii=True))'
~~~

Do not submit the next action unless status is passed, frame_index matches the source frame, task IDs match, held-object state is expected, particle counts are valid, scene graph backend matches, and scheduler/planner diagnostics are present.

## Artifact Inspection

List only the known run directory, not the whole home directory：

~~~bash
ls -lah <run-dir>
ls -lah <run-dir>/checkpoint
ls -lah <run-dir>/checkpoint_scene_graph
~~~

Summarize the run report：

~~~bash
"$ISBENCH_PYTHON" -c 'import json, pathlib; p=pathlib.Path("<run-dir>/report.json"); d=json.loads(p.read_text()); print(json.dumps({"task":d.get("task"),"scene":d.get("scene"),"primitive_type":d.get("primitive_type"),"awareness":d.get("awareness"),"plans":d.get("plans"),"termination":d.get("termination"),"error_stack":d.get("error_stack"),"goal_condition":d.get("goal_condition")}, indent=2, ensure_ascii=True))'
~~~

Summarize event ownership without printing giant prompts：

~~~bash
"$ISBENCH_PYTHON" -c 'import collections, json, pathlib; p=pathlib.Path("<run-dir>/runtime_timeline.jsonl"); e=[json.loads(x) for x in p.read_text().splitlines() if x.strip()]; c=collections.Counter((x.get("component"),x.get("event_type")) for x in e); print("\\n".join(f"{a}:{b}={n}" for (a,b),n in sorted(c.items())))'
~~~

Check media separately：

~~~bash
ffprobe -v error -show_entries format=duration,size \
  -of default=noprint_wrappers=1 <run-dir>/replay_camera.mp4
~~~

A top-down media error caused by missing occupancy metadata is a media/evidence failure unless it also blocks runtime actions. Keep it separate from physical task failure.

## Restore Acceptance

Use these gates in order：

| Gate | Evidence | What it proves |
| --- | --- | --- |
| 0. Static | py_compile, git diff --check, task preflight | code/config is parseable |
| 1. Init | headless manifest, initialized scene, finite robot state | simulator started |
| 2. Save | immutable frame + manifest + session entry | one successful action was checkpointed |
| 3. Physical restore | restore_validation.json.status=passed | robot/camera/objects/gripper/particles/timers match |
| 4. Logical restore | scene graph, object registry, planner episode, tracker and scheduler diagnostics | Python runtime memory matches source frame |
| 5. Continuation | one new post-restore successful action and new immutable frame | recovery did not replay or duplicate source actions |
| 6. Full acceptance | uninterrupted report, decoded media, goal condition | task completed physically with evidence |

Never jump from Gate 1 to Gate 6. Report the highest verified gate and the exact next blocker.

## Failure Matrix

| Symptom | Likely class | Action |
| --- | --- | --- |
| task preflight fails | contract | inspect live JSON/BDDL/schema; do not restore |
| CUDA unavailable or device busy | transient resource | probe GPU, wait, retry smallest failed command in a fresh process |
| checkpoint manager says model planner required | mode/config | pass --model; do not enable example planner for save/restore |
| task entity IDs do not match | wrong task/scene/cache | compare BDDL entities and source manifest; create a fresh branch |
| scene graph backend mismatch | wrong backend/config | rerun with source backend; do not bypass validation |
| RGB sensor or robot pose mismatch | scene initialization drift | verify scene/task/config and restore in a clean process |
| held object mismatch | gripper/carry loss | stop; inspect physical gripper and symbolic carry diagnostics |
| particle-system count mismatch | missing system or stale scene | initialize matching systems and verify scene resources; do not edit counts |
| cooked payload not contained | carry restore failure | stop before action; inspect payload and object transform |
| scheduler timer mismatch | restored after timer creation or hand-edited state | restore before timer-creating action and replay it in a new branch |
| restore_validation.json absent | launch is not restore proof | inspect process/log/socket and wait for durable artifacts |
| executor fails after restore | physical action failure | resume from last successful immutable frame, not failed action |
| topdown/video cleanup fails | evidence/cleanup | inspect report and replay camera; do not call it task success automatically |

## Final Report Template

Use this compact structure when handing results to the user：

~~~text
Mode and command:
- runner:
- task / scene:
- primitive:
- perception backend:
- model planner / risk:
- awareness:
- checkpoint mode:

Verified:
- contract preflight:
- headless initialization:
- successful actions:
- immutable checkpoint frames:
- restore validation:
- post-restore actions:
- final goal condition:
- media decode:

Not verified or failed:
- exact action/error:
- physical/runtime evidence:
- next recovery boundary:

Artifacts:
- report.json:
- runtime_timeline.jsonl:
- checkpoint directory:
- restore_validation.json:
- media:
~~~

Always distinguish passed, not_run, failed, and inferred. Never convert an inference from a screenshot or PID into a restore or physical-success claim.

