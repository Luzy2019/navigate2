# IS-Bench Checkpoint Implementation Reference

本文档记录 checkpoint-recover skill 对应的当前 IS-Bench 实现。使用时优先阅读当前源码；行号可能随修改变化，符号名和 schema version 更可靠。

## 目录

- 文件与所有权
- 磁盘目录
- 物理 payload
- 在线 runtime payload
- 保存时序
- 恢复时序
- scene graph 状态
- timer 与粒子
- planner 与生成器
- 现有手动 session 的关系

## 文件与所有权

| 文件 | 关键符号 | 责任 |
| --- | --- | --- |
| og_ego_prim/cli/online_benchmark_once.py | _online_benchmark_once, CLI args, action loop | 创建 benchmark、AgentPlanner、VLM risk provider、planner adapter，并在成功动作之后调用 checkpoint manager |
| og_ego_prim/cli/online_physical_checkpoint.py | OnlinePhysicalCheckpointManager | 把在线 runner 的 Python runtime state 叠加到既有物理 session 协议 |
| og_ego_prim/cli/headless_manual_physical_session.py | PersistentPhysicalSession | 复用 simulator dump/load、实体 remap、机器人/相机/夹爪/粒子/物理验证和 immutable frame 保存 |
| og_ego_prim/scene_graph/perception_scene_graph.py | checkpoint_state, restore_checkpoint_state | 保存 updater snapshot、state diff、任务 entity、manipulation history 和 backend state |
| og_ego_prim/scene_graph/backends/samjam_sam2.py | checkpoint_state, restore_checkpoint_state | 保存 SAMJAM native tracking identity、关系、frame indices、native JPEG frame 和 task categories；不保存模型权重 |
| og_ego_prim/scene_graph/backends/samjam_unigoal.py | checkpoint_state, restore_checkpoint_state | 同时保存 SAMJAM 与 UniGoal lifelong mapping state |
| og_ego_prim/task_planner/episode.py | PlannerEpisode | 保存历史 planner episode，供恢复后重新生成下一 proposal |
| og_ego_prim/observability/* | ReplaySession 和 tracing wrappers | 记录 action、risk、planner、executor、媒体和 checkpoint 事件；不是物理恢复的 source of truth |

online_physical_checkpoint.py 继承 PersistentPhysicalSession。修改 checkpoint 时先检查父类已有能力，再决定是否必须扩展 online runtime；不要把父类逻辑复制到 online runner。

## 磁盘目录

在线 run 的输出通常形如：

~~~text
results/benchmark/<task_metadata_name>___<scene>/<try_id>_<model>/
├── report.json
├── runtime_timeline.jsonl
├── replay_manifest.json
├── replay_camera.mp4
├── video.mp4
├── checkpoint/
│   ├── session.json
│   ├── latest.pt
│   ├── latest.json
│   ├── previous.pt
│   ├── frame_000001.pt
│   ├── frame_000001.json
│   ├── frame_000002.pt
│   └── ...
└── checkpoint_scene_graph/
    ├── frame_000001.json
    ├── frame_000002.json
    └── ...
~~~

目录名由任务 metadata 生成，不能单独作为 task identity。使用 report.json 的 task、replay_manifest.json 的 task_id、CLI 参数和 BDDL 一起确认任务。

文件语义：

- frame_NNNNNN.pt：immutable frame。成功 action 后只写一次，存在时拒绝覆盖。
- frame_NNNNNN.json：immutable frame 的轻量 manifest，包含 frame index、完成 action 数、scheduler、scene summary、task entity IDs、held object 和粒子 payload 摘要。
- latest.pt：当前最新状态的 rolling pointer，每次保存会替换。
- latest.json：latest.pt 的 manifest。
- previous.pt：被替换前的 rolling pointer，只用于近邻回滚，不是 immutable history。
- session.json：session 状态、完成 action 列表、active subtask、restore source 和 branch metadata。
- checkpoint_scene_graph/frame_NNNNNN.json：current scene graph、global accumulated memory 和 scheduler state-change 数量。
- restore_validation.json：restore 是否通过的硬证据；没有 status=passed 就不能继续。

## 物理 payload

父类 _physical_checkpoint_payload() 生成 schema isbench.physical_checkpoint.v1，主要字段如下：

| 字段 | 用途 |
| --- | --- |
| sim_state | OmniGibson simulator-owned state，包括 scene registry 和系统状态 |
| robot_pose | robot base position/orientation、joint positions |
| sensor_pose | native RGB sensor position/orientation |
| first_view_focus | 最近一次需要 first-view 对齐的 entity 和目标点 |
| task_object_states | 每个 BDDL entity 的 position、orientation、open state |
| task_object_names | BDDL entity ID 到当前 simulator object name 的映射 |
| robot_name | source process 的随机 robot name，用于 remap 后排除旧 articulation references |
| object_registry | runtime object state registry |
| physical_held_object_id | 当前 gripper 实际持有的 task entity 或空值 |
| symbolic_carry_checkpoint | symbolic carry 的局部坐标粒子/载荷状态 |
| symbolic_carry | carry diagnostics |
| cooked_particle_payloads | cooked/contained particle 的可恢复 local-frame payload |
| scheduler | scheduler 的 pending process 序列化状态 |
| executor_global_step | 物理执行步计数 |
| updater_global_step | scene graph updater 步计数 |
| active_subtask_index | 当前 subtask index |
| active_subtask_action_start | 当前 subtask 的 action 起点 |
| global_scene | global scene accumulator 的 state |

保存前会记录真实 hold frames，不是简单 sleep。hold 期间同步 symbolic carry，使抓取物体与其 local-frame particle payload 不脱节。

恢复时会先初始化 payload 中出现但新场景尚未初始化的 particle systems，再调用 og.sim.load_state。对 symbolic carry 标记为空的 physical instancer，会跳过不安全的 empty-instancer load，并从单独 payload 恢复；不要手动把 particle count 改成零。

## 在线 runtime payload

OnlinePhysicalCheckpointManager._runtime_checkpoint() 在物理 payload 上增加 schema isbench.online_physical_checkpoint.v1。

### online_runtime.perception

来自 PerceptionSceneGraphUpdater.checkpoint_state()：

- backend name 和 disabled 状态；
- updater/global frame index；
- latest result、snapshot、perception errors；
- task instruction、task entity IDs、held object name；
- manipulation-event history 和 last manipulation key；
- state tracker 的 previous/current；
- backend-specific SAMJAM/UniGoal state。

### online_runtime.controller

保存 runtime controller 的：

- active subtask；
- latest scene、latest changes、visible entity IDs；
- rethinking/proposal counters；
- pending scheduler state changes；
- last review、last outcome、last risk latency；
- PlannerEpisode.to_dict()。

### online_runtime.tracker

保存 tracker 的 plans、raw outputs、risk evaluations、risk predictions、execution diagnostics、termination/goal 相关状态和其他 tracker fields。planner_episode 单独由 controller 保存并在恢复时重建。

### online_runtime.agent

保存 AgentPlanner 的 current step、task instruction、objects/goal text、safety text、pending rethinking/manipulation、validation error、subtask plan start、last prompt、prompt sequence 和 prompt records。

### online_runtime.planner_adapter

保存 active adapter type、start/preflight/loading/root action/safety goal/steps/inflight 和 safety-plan output。恢复时要求 adapter type 与当前 adapter 完全一致。

## 保存时序

在线 runner 的 action loop 逻辑是：

~~~text
planner proposal
    -> runtime review / risk evaluation
    -> executor executes physical primitive
    -> execution_ok == True ?
         yes: save_after_success(action_text)
         no:  no checkpoint; terminate/rethink according to runner policy
~~~

save_after_success() 的具体顺序：

1. 检查 last_outcome.executed、last_outcome.succeeded 和 last_review。
2. 运行配置数量的 real post-action settle steps。
3. 读取 updater current snapshot。
4. 合并 global scene memory 并应用成功 action。
5. drain scheduler state changes 并应用到 accumulator。
6. 写 scene graph frame JSON。
7. 更新 session.completed_actions 和 current frame index。
8. 先写临时 .tmp.pt，再 atomic rename 为 immutable frame。
9. 更新 latest.pt；如果旧 latest 存在，先复制为 previous.pt。
10. 写 immutable 和 rolling manifests。

如果 execute_plan() 返回 false，runner 不会调用 save_after_success()。即使 risk 返回 ALLOW，也不能把 executor failure 当作成功 checkpoint。

## 恢复时序

OnlinePhysicalCheckpointManager.restore() 的顺序是：

1. 检查 source path 是文件。
2. 用 torch.load(..., map_location="cpu", weights_only=False) 读取。
3. 检查 checkpoint_kind == isbench.online_physical_checkpoint.v1。
4. 通过父类恢复并验证 simulator-owned physical state。
5. 恢复 online runtime state。
6. 恢复并渲染 observation，不推进 task action。
7. 写 session.json 的 restored source 和状态。
8. 写 checkpoint/restore_validation.json，包含 physical validation、online diagnostics 和 source path。
9. 更新新 branch 的 latest.pt，但不修改 source immutable frame。

父类验证至少包括：

- checkpoint schema；
- source/current task entity ID 集合和 simulator name 一一映射；
- robot position、orientation、joint positions；
- RGB sensor position/orientation；
- 每个 pose-bearing task object 的 position/orientation/open state；
- held object 与实际 gripper state；
- symbolic carry 和 carried-object position error；
- 所有 particle system count；
- cooked particle payload containment；
- empty hands 与残留 symbolic carry/particle suspension 不矛盾。

## Scene Graph 状态

物理 sim_state 不包含 Python perception identity memory，因此不能只恢复 simulator state。PerceptionSceneGraphUpdater 额外保存：

- 当前和历史 snapshot；
- current-frame result；
- is_vis 生命周期相关 state tracker；
- object registry 之外的 scene graph diff；
- SAMJAM native object IDs、关系、next ID、frame indices；
- native tracking frame JPEG bytes；
- UniGoal stable nodes、source mappings、last frame 和 last results；
- task entity category binding、object goal 和 task instruction。

SAM2/SAMJAM model weights 不放进 checkpoint。恢复后 video_predictor 会重新构建；保存的是用于重建 tracking input 的逻辑状态和 native frames。不要把模型对象重新初始化误判为 scene graph 丢失，也不要因此自己实现另一个 graph cache。

恢复后区分：

- current scene graph：当前 frame 的可见事实和当前关系；
- global scene memory：长期身份、历史节点和状态变化；
- TaskMemory/PlannerEpisode：action/planner history；
- scheduler：时间过程的 pending state。

这四者不能互相替代。风险 predictor 需要 current scene snapshot 和相关历史，而不是只读取一份手写 global JSON。

## Timer 与粒子

Scheduler timer 属于 runtime state，不是普通文本 memory。保存 scheduler.to_dict()、pending processes 和 controller pending state changes；恢复后用 scheduler.load_pending(...)，并检查 manifest 与 restore diagnostics。

当需要修改某个 timer duration 时：

1. 找到 timer 被创建前的 immutable frame。
2. 在新的 branch restore 该 frame。
3. 只删除或移走该 frame 之后的 branch-derived artifacts，并保留 source frame 和 earlier evidence。
4. 重新执行创建 timer 的 action。
5. 检查 ready_step - start_step == requested_duration。

不要从 timer 已经存在的 frame 直接改 JSON；这样会让 physical state、scheduler state、scene memory 和 action history 不一致。

粒子需要同时检查物理 system count、cooked payload count、containment 和 symbolic carry suspension。视频里看见粒子不代表 checkpoint restore 已经恢复粒子；必须以 restore_validation.json 为准。

## Planner 与生成器

checkpoint 保存 planner 的历史和部分 adapter state，但 Python generator 不能序列化。恢复时会清空 base adapter iterator，并使用 restored tracker history、planner episode、agent prompt state 和 adapter fields 重新生成下一 proposal。

因此：

- 不要重复已经成功的 actions；
- 不要把恢复后的第一条 proposal 当作 source 进程中断点的 byte-for-byte continuation；
- 不要恢复一个 ExamplePlanner checkpoint 到 vlm_closed_loop adapter，或反过来；
- save/restore 模式必须使用 model planner agent，当前 online manager 会拒绝 example planning；
- risk 通过只是 action review 结果，仍需 executor 成功后才保存。

## 现有手动 session 的关系

headless_manual_physical_session.py 是物理 checkpoint 协议的原始长期 session 入口，提供 immutable frame、restore validation、first-view focus restore、manual annotation 以及 persistent service 状态。在线 runner 不应重新实现这些能力；OnlinePhysicalCheckpointManager 继承它并增加 online runtime state。

如果任务是人工 native-frame 审查流程，使用手动 session 的 frame_NNNNNN.pt、restore_frame(frame_index)、native PNG、annotation 和 control socket 约定。不要把 manual annotation JSON 当作 online benchmark 的 scene graph checkpoint，也不要绕过 PerceptionSceneGraphUpdater 直接注入最终 graph。

