# Online Benchmark 完整文本流程图

本图按当前代码静态调用链绘制。所有方框、横线和竖线都是纯文本，不依赖 Mermaid 渲染。

符号说明：

```text
┌──────────────┐
│ 普通过程节点 │
└──────┬───────┘
       │
       ▼
     ◇ 条件 ◇ ──是──▶ 分支
       │否
       ▼

图中的 [A1]、[B3] 等编号用于在总图与展开图之间跳转。
```

## 图 1：从进程入口到任务结束的总流程

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A0] 进程入口                                                               │
│ python -m og_ego_prim.cli.online_benchmark_once                             │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A1] 模块导入阶段                                                           │
│ 1. maybe_reexec_with_omnigibson_python()                                    │
│ 2. add_monkey_patch()                                                       │
│ 3. 导入 OmniGibson、benchmark、planner、replay 等运行模块                   │
│ 4. 创建 argparse parser                                                     │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A2] __main__                                                               │
│ parser.parse_args()                                                         │
│   └─ _apply_config(args)                                                    │
│      ├─ 生成 RuntimeConfig 默认值                                           │
│      ├─ 读取 YAML 及 includes                                               │
│      ├─ 合并 runtime / task / model / scene graph 等配置                    │
│      └─ 显式 CLI 参数覆盖 YAML 默认值                                       │
│ 校验 task 与 scene 已提供                                                   │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A3] online_benchmark_once(...)                                             │
│ 建立 benchmarks 列表，进入 try/finally 生命周期保护                         │
│                                                                              │
│ try:    _online_benchmark_once(..., benchmark_holder=benchmarks)            │
│ finally: 对所有已构造 benchmark                                             │
│          ├─ Replay 未 finalize 时，以 failed 状态补做 _finish_replay()      │
│          └─ 无论 Replay 是否失败，都调用 benchmark.close()                  │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ try
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A4] build_benchmark(...)                                                   │
│ 读取 task JSON → 决定 primitive_type / task_type → 构造 OnlineBenchmark     │
│                                                                              │
│ 注意：这里先加载任务与环境、构造 runtime；此时 planner 尚未创建。            │
│ 详细展开见图 2。                                                            │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A5] benchmark 已经完整存在                                                 │
│ env + task + scene + robot + objects + Executor + scene graph               │
│ + ObjectRegistry + scheduler + 默认 RiskPredictor                           │
│ + Evaluator + AgentRuntimeController                                        │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A6] CLI 后绑定本次 run 的外围组件                                          │
│ 1. 分配 output_dir                                                          │
│ 2. _attach_replay_session()：包装 controller / evaluator / executor / media │
│ 3. model 路径：                                                             │
│    AgentPlanner + TracingModelClient                                        │
│    + VLMClosedLoopPlannerAdapter + TracingPlannerAdapter                     │
│    + graph 非 disabled 且 risk.enabled 时安装 HybridRiskProvider             │
│ 4. example 路径：ExamplePlannerAdapter + TracingPlannerAdapter              │
│ 5. benchmark.bind_planner_adapter()                                         │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A7] 执行前可选工作                                                         │
│ ├─ 保存 0_init 周围视角图                                                   │
│ ├─ 生成 self caption                                                        │
│ └─ 生成 / 评测 awareness                                                    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
                         ◇ 只启用 awareness 评测？ ◇
                          │是                         │否
                          ▼                           ▼
┌───────────────────────────────────────┐  ┌───────────────────────────────────┐
│ 保存 report_awareness.json            │  │ [A8] 创建 action generator        │
│ _finish_replay()                      │  │ runtime_controller.iter_actions() │
│ finish_run()                          │  │ + track_planning_latency()        │
└───────────────────┬───────────────────┘  └─────────────────┬─────────────────┘
                    │                                        │
                    │                                        ▼
                    │                      ┌───────────────────────────────────┐
                    │                      │ [A9] 逐 Action 闭环               │◀──────────────┐
                    │                      │ planner 提议一个高层 Action       │               │
                    │                      │ → runtime review                  │               │
                    │                      │ → BLOCK 或 Executor 执行          │               │
                    │                      │ → runtime / scene / evaluator反馈 │               │
                    │                      │ 详细展开见图 4～图 8              │               │
                    │                      └─────────────────┬─────────────────┘               │
                    │                                        │                                 │
                    │                            ◇ 本轮是否继续？ ◇                             │
                    │                         是 │                 │ 否                          │
                    │                            └─────────────────┘                             │
                    │                                        │                                 │
                    │                                        └─────────────────────────────────┘
                    │                                        │ 否
                    │                                        ▼
                    │                      ┌───────────────────────────────────┐
                    │                      │ [A10] termination_evaluation()    │
                    │                      │ 1. execution goal                 │
                    │                      │ 2. 未执行的 process safety        │
                    │                      │ 3. termination safety             │
                    │                      │ 4. 若无既有终止原因，记为 done    │
                    │                      └─────────────────┬─────────────────┘
                    │                                        │
                    │                                        ▼
                    │                      ┌───────────────────────────────────┐
                    │                      │ [A11] 结果落盘                    │
                    │                      │ tracker.save_tracking(report.json)│
                    │                      │ _finish_replay()                  │
                    │                      │ ├─ replay_camera.mp4              │
                    │                      │ ├─ video.mp4                      │
                    │                      │ ├─ replay_topdown.mp4             │
                    │                      │ └─ events / frames / manifest     │
                    │                      └─────────────────┬─────────────────┘
                    │                                        │
                    └──────────────────────┬─────────────────┘
                                           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [A12] finish_run() 与最终清理                                                │
│ keep_open_after_done=false：等待 3 秒 → benchmark.close() → og.clear()      │
│ keep_open_after_done=true ：持续 env.step(hold action)，Ctrl+C 后返回        │
│ 最外层 finally 再保证 Replay finalize 与 benchmark.close()                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 图 2：Benchmark、任务和环境的加载顺序

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B1] build_benchmark(task, scene, ...)                                      │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ get_task_config_path(task) → 读取 task JSON                                 │
│ resolve_primitive_type(task JSON, CLI override)                             │
│ 将 scene graph backend / interval 写入 RuntimeConfig                        │
│ 根据 task_info.task_type 从 ONLINE_BENCHMARKS 选择具体类                    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B2] OnlineBehaviorBenchmark.init_env_config()                              │
│ ├─ 读取任务引用的 OmniGibson base YAML                                     │
│ ├─ 确定 BDDL activity / definition / instance                              │
│ ├─ 如 task JSON 提供 execution_goal_condition，则注入内存中的 BDDL goal    │
│ ├─ 选择 BehaviorTask 或 CustomBehaviorTask                                 │
│ ├─ 校验 scene 是否属于任务允许场景                                         │
│ ├─ 选择 fixed scene cache / online sampled scene                           │
│ └─ 形成 env_config：task + scene + robot + objects + sensors               │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B3] OnlineBenchmark.__init__()                                             │
│                                                                              │
│ _configure_video_sensors()                                                  │
│ _configure_scene_graph_sensors()                                            │
│          │                                                                   │
│          ▼                                                                   │
│ og.Environment(configs=env_config)                                          │
│          │                                                                   │
│          ├─ 加载 BDDL task                                                  │
│          ├─ 加载 scene / fixed scene JSON                                   │
│          ├─ 创建 robot、task objects、sensors                               │
│          └─ 初始化 simulator-owned state                                    │
│          │                                                                   │
│          ▼                                                                   │
│ 应用 task-specific removals                                                 │
│ → robot 初始 pose                                                           │
│ → object 初始 poses                                                         │
│ → object 初始 relations                                                     │
│ → task traversability-map obstacles                                         │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B4] 构造 simulator-facing 组件                                             │
│ ├─ 周围视角 camera poses                                                    │
│ ├─ OnlineEvalTracker                                                       │
│ ├─ PerceptionSceneGraphUpdater                                             │
│ │   └─ reset(env) → initial SceneGraphSnapshot → tracker                   │
│ └─ Executor(env, primitive_type, step_callback=_on_low_level_step)          │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B5] _initialize_agent_runtime()，详细见图 3                                │
│ TaskDefinition → ObjectRegistry / scheduler / prompt / risk                 │
│ → RuntimeComponents → AgentRuntimeController                                │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B6] 完成 benchmark 自身初始化                                              │
│ ├─ 配置 starter placement slots                                             │
│ ├─ 构造 Evaluator，并注入 RuntimeComponents                                 │
│ ├─ 保存 task instruction / initial setup                                   │
│ ├─ set_viewer()                                                             │
│ ├─ _add_extra_init_states()                                                 │
│ └─ _refresh_scene_graph(force=True) → runtime_controller.observe(snapshot)  │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [B7] build_benchmark() 返回已经可执行的 OnlineBenchmark                     │
│ 之后 CLI 才创建并绑定 AgentPlanner / VLM risk provider / planner adapter。   │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 图 3：TaskDefinition 投影与 Runtime 所有权

```text
                            ┌────────────────────────┐
                            │ task JSON              │
                            └───────────┬────────────┘
                                        │ load_task_definition()
                                        ▼
                            ┌────────────────────────┐
                            │ TaskDefinition         │
                            └───────────┬────────────┘
                                        │
                  ┌─────────────────────┼─────────────────────┐
                  │                     │                     │
                  ▼                     ▼                     ▼
┌────────────────────────┐ ┌────────────────────────┐ ┌────────────────────────┐
│ AgentTaskView          │ │ RuntimeTaskConfig      │ │ EvalTaskConfig         │
│ 只进入 planner         │ │ 进入 runtime risk     │ │ 只进入 Evaluator       │
│ ├─ instruction        │ │ └─ action-scoped      │ │ ├─ execution goal     │
│ ├─ plain-text goal    │ │    safety cues        │ │ ├─ process safety     │
│ ├─ initial setup      │ └───────────┬────────────┘ │ ├─ termination safety │
│ ├─ objects/abilities  │             │              │ └─ eval cautions      │
│ └─ rules/subtasks     │             │              └───────────┬────────────┘
└───────────┬────────────┘             │                          │
            │                          │                          │
            ▼                          ▼                          ▼
planner prompt + Controller     enabled：task provider    Evaluator
的 task_view                    disabled：Null provider   → RuntimeComponents
                                → RiskPredictor
            │                          │                          │
            │                          └──────────────┬───────────┘
            │                                         ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ RuntimeComponents                                                           │
│ ├─ perception      → PerceptionSceneGraphUpdater                            │
│ ├─ objects         → ObjectRegistry                                         │
│ ├─ scheduler       → Scheduler(clock = Executor.global_step_index)          │
│ ├─ prompt_builder  → PromptBuilder                                          │
│ ├─ executor        → OnlineBenchmark.executor                               │
│ ├─ evaluator       → OnlineBenchmark.evaluator                              │
│ ├─ event_sink      → TrackerEventSink + ReplaySession                       │
│ ├─ risk_predictor  → RiskPredictor                                          │
│ │                    └─ Null / task / Hybrid provider                       │
│ │                       └─ Hybrid = task + RiskAssessor/ServerClient        │
│ └─ planner         → TracingPlannerAdapter                                  │
│                      └─ VLMClosedLoopPlannerAdapter                         │
│                         └─ AgentPlannerAdapter → AgentPlanner               │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ 被持有和编排
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ AgentRuntimeController                                                      │
│ ├─ task_view → 上方 AgentTaskView                                           │
│ ├─ latest_scene / latest_changes / visible_entity_ids                       │
│ ├─ current task/subtask                                                     │
│ ├─ last_review / last_outcome / rethinking_attempts                         │
│ └─ PlannerEpisode                                                          │
└──────────────────────────────────────────────────────────────────────────────┘

注意：
1. AgentRuntimeController 负责 planner、scheduler、risk、scene 和 ObjectRegistry 的编排。
2. Executor.controller 是 primitive controller，只负责生成低层 robot action。
3. 二者不是同一个 controller。
```

## 图 4：每一个高层 Action 的完整闭环

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [C1] runtime_controller.iter_actions()                                      │
│ while True: action = propose(); yield action; DONE 后停止                    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [C2] runtime_controller.propose()                                           │
│ 构造 PromptContext：                                                        │
│ task instruction + goal + pending timers + allowed actions                  │
│ + 上一被挡 action / rethinking reason（仅 should_rethink 时）               │
│ planner 不接收 latest scene、ObjectRegistry view 或 memory recall            │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [C3] planner_adapter.propose(context)                                       │
│ example：读取下一条 scripted action                                         │
│ model  ：VLM closed-loop propose，详细见图 5                                │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                         ◇ 是否得到 Action？ ◇
                          │否                         │是
                          ▼                           ▼
┌───────────────────────────────────────┐  ┌───────────────────────────────────┐
│ planner exhausted                    │  │ [C4] yield 一个高层 Action        │
│ iter_actions() return                │  │ 记录 planning latency             │
│ → 跳到图 9 终止评测                  │  └─────────────────┬─────────────────┘
└───────────────────────────────────────┘                    │
                                                            ▼
                                          ┌───────────────────────────────────┐
                                          │ ReplaySession.execute_plan()      │
                                          │ → OnlineBenchmark.execute_plan() │
                                          └─────────────────┬─────────────────┘
                                                            │
                                                            ▼
                                          ┌───────────────────────────────────┐
                                          │ [C5] _runtime_action()            │
                                          │ 规范化为 review 使用的 Action     │
                                          │ starter 隐式 held object 只在这里 │
                                          │ 显式化，不新增公共 held 字段       │
                                          └─────────────────┬─────────────────┘
                                                            │
                                                            ▼
                                          ┌───────────────────────────────────┐
                                          │ [C6] review_action()              │
                                          │ scheduler gate + risk predictor  │
                                          │ 详细见图 6                        │
                                          └─────────────────┬─────────────────┘
                                                            │
                                                 ◇ 最终 decision？ ◇
                                      ┌───────────────┼──────────────────┐
                                      │ BLOCK         │ CAUTION          │ ALLOW
                                      ▼               └────────┬─────────┘
                         ┌──────────────────────────┐           │
                         │ [C7] record_blocked()    │           ▼
                         │ executed=false          │  ┌─────────────────────────┐
                         │ 不调用 Evaluator        │  │ [C8] 允许执行           │
                         │ 不调用 Executor         │  │ Evaluator(before)       │
                         │ 不调用 env.step()       │  │ → Executor → env.step   │
                         └────────────┬─────────────┘  │ → runtime/eval/scene反馈│
                                      │                │ 详细见图 8               │
                           ◇ should_rethink？ ◇       └────────────┬────────────┘
                             │是             │否                    │
                             ▼               ▼                      │
                 ┌───────────────────┐ ┌──────────────────────┐     │
                 │ 主循环 continue   │ │ 记录 blocked 终止原因│     │
                 │ 下一次 propose    │ │ 主循环 break         │     │
                 │ 详细见图 7        │ └──────────┬───────────┘     │
                 └─────────┬─────────┘            │                 │
                           │                      │                 │
                           └──────▶ 回到 [C2]     │                 │
                                                  │                 │
                                                  │       ◇ execution_ok？ ◇
                                                  │          │否         │是
                                                  │          ▼           ▼
                                                  │  ┌─────────────┐ ┌──────────────┐
                                                  │  │ execution   │ │ action 后媒体│
                                                  │  │ error/break │ │ /观察图保存  │
                                                  │  └──────┬──────┘ └──────┬───────┘
                                                  │         │               │
                                                  └─────────┴───────┬───────┘
                                                                    │
                                                         ◇ Action == DONE？ ◇
                                                           │是            │否
                                                           ▼              ▼
                                                   iter_actions 停止   回到 [C2]
                                                           │
                                                           ▼
                                                     图 9 终止评测
```

## 图 5：VLMClosedLoopPlannerAdapter 如何产生下一条 Action

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [D1] VLMClosedLoopPlannerAdapter.propose(context)                           │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                         ◇ 首次 propose，尚未 preflight？ ◇
                          │是                         │否
                          ▼                           │
┌──────────────────────────────────────────────┐      │
│ TASK_RISK_PREFLIGHT                          │      │
│ VLM 判断是否存在 multi_object_loading_order │      │
└───────────────────────┬──────────────────────┘      │
                        │                             │
           ┌────────────┴─────────────┐               │
           │ NONE                     │ MONITOR       │
           │                          ▼               │
           │       ┌────────────────────────────────┐ │
           │       │ 校验 risk type、对象顺序(>=3)、│ │
           │       │ destination role、relation     │ │
           │       └───────────────┬────────────────┘ │
           │                       │                  │
           │                       ▼                  │
           │       ┌────────────────────────────────┐ │
           │       │ 保存 order/pending/role/relation││
           │       │ 等普通 planner 首次操作其中对象│ │
           │       │ 再生成完整 loading plan       │ │
           │       └───────────────┬────────────────┘ │
           │                       │                  │
           └───────────────────────┴──────────────────┘
                                    │
                                    ▼
                         ◇ 是否存在 inflight action？ ◇
                          │否                         │是
                          │                           ▼
                          │             ┌────────────────────────────────────┐
                          │             │ _consume_inflight(last_outcome)    │
                          │             ├────────────────────────────────────┤
                          │             │ pending：等待 runtime              │
                          │             │ navigation success：不移动 cursor │
                          │             │ operation success：pop steps[0]   │
                          │             │ risk BLOCK：进入 risk rethink      │
                          │             │ scheduler BLOCK：活动计划则重写； │
                          │             │ 普通动作委托 base rethink         │
                          │             │ executed failure：停止 planner     │
                          │             └──────────────────┬─────────────────┘
                          │                                │
                          └────────────────────────────────┘
                                    │
                         ◇ 上一 action 是否 risk BLOCK？ ◇
                          │是                         │否
                          ▼                           │
                       进入图 7 safety rethink        │
                          │                           │
                          └──────────────┬────────────┘
                                         ▼
                         ◇ 当前是否有 safety/loading steps？ ◇
                          │是                              │否
                          ▼                                ▼
┌────────────────────────────────────────────┐  ┌──────────────────────────────┐
│ [D2] _next_step()                         │  │ [D3] base AgentPlanner       │
│ intended operation = steps[0]             │  │ 读取最新 RGB/上一执行信息    │
│ VLM 根据 held object + 最新 RGB           │  │ 构造 planning/rethinking提示 │
│ 决定先 NAVIGATE_TO 还是执行 intended op   │  │ ServerClient.model()         │
│                                            │  │ parse / verify / record_plan │
│ VLM 不得用其他 operation 改写当前 op       │  │ 得到 candidate 或 None       │
└──────────────────────┬─────────────────────┘  └──────────────┬───────────────┘
                       │                                       │
                       ▼                            ◇ candidate 是否首次对 monitored
┌────────────────────────────────────────────┐       object 提出 GRASP 或 placement？ ◇
│ [D4] _issue(managed action)               │        │否                    │是
│ AgentPlanner.record_plan()                │        ▼                      ▼
│ → 增加 current_step / action budget       │  直接返回 candidate   标记 candidate 未执行
│ → 保存 inflight / outcome marker          │                       _start_loading_plan()
│ → 保存动作发出前的 held object            │                       → 回到 [D2]
└──────────────────────┬─────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 返回一个 Action 给 runtime_controller                                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 图 6：Scheduler 与 Risk Predictor 的审查流程

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [E1] review_action(candidate Action)                                        │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ tick_scheduler()                                                            │
│ scheduler.check_action(action, objects + latest_scene + executor)            │
│ → TemporalGate(ALLOW / BLOCK, reasons, blocking process ids)                │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 构造 RiskContext                                                           │
│ latest scene + ObjectRegistry + Scheduler                                   │
│ + current AgentTaskView + active subtask                                    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ RiskPredictor.predict(action, RiskContext)                                  │
│                                                                              │
│ risk.enabled=false：工厂安装 NullRiskProvider                               │
│ risk.enabled=true：调用当前 RiskProvider.assess()                           │
│ ├─ graph disabled：仅调用 task provider，保留 IS-Bench hazard/caution       │
│ └─ graph enabled：Hybrid(task provider, ModelRiskProvider)                  │
│                                      │                                       │
│                                      ▼                                       │
│                                  RiskAssessor                               │
│                                  → 仅消费 RiskContext.scene 当前快照       │
│                                  → 图缺失/not-ready/empty/畸形物理边或      │
│                                    action entity 缺失时显式报错             │
│                                  → 排除 in_room / in_group membership edge  │
│                                  → 同类多实例全部作为保守 BFS roots         │
│                                  → 显示 task entity → candidate nodes       │
│                                  → depth 1 优先，再完整展开所有可达物理边   │
│                                  → 每条边标 depth / expanded from           │
│                                  → states/hazard 原值序列化，缺失为 unknown │
│                                  → ServerClient.model(ACTION_RISK_CHECK)    │
│                                           │                                  │
│                              ┌────────────┴────────────┐                     │
│                              │ safe                    │ unsafe              │
│                              ▼                         ▼                     │
│                         空 model hazards       每个 matched_risk            │
│                                               → HIGH HazardDraft             │
│                                               无有效项时 reason fallback      │
│                                      │                                       │
│                                      ▼                                       │
│ graph enabled：合并 task hazards + model hazards                            │
│ graph disabled：直接使用 task hazards                                       │
│ → 计算 ALLOW / CAUTION / BLOCK                                              │
│                                                                              │
│ safe-memory：with_memory 固定启用 samjam；without_memory 固定禁用 graph。   │
│ 普通 online 入口按实际 backend 是否 disabled 决定是否安装 model provider。  │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [E2] 合成最终 ActionReview                                                  │
│ scheduler BLOCK 或 risk BLOCK  → BLOCK                                      │
│ 否则 risk CAUTION                → CAUTION                                  │
│ 否则                             → ALLOW                                    │
│                                                                              │
│ BLOCK 且 planner.supports_rethinking 且未超过次数 → should_rethink=true     │
│ 写 PlannerEpisodeEntry、last_review、risk latency 和 replay/tracker events  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 图 7：BLOCK 后的 rethink、safety 与 loading

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [F1] ActionReview.decision == BLOCK                                         │
│ OnlineBenchmark._execute_plan() 调 AgentRuntimeController.record_blocked()  │
│ → executed=false → 返回 execution_ok=false                                  │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                         ◇ should_rethink == true？ ◇
                          │否                         │是
                          ▼                           ▼
┌──────────────────────────────────────┐   ◇ risk BLOCK？ ◇
│ 主循环记录 blocked 终止原因          │    │否                │是
│ break → 图 9                        │    ▼                  │
└──────────────────────────────────────┘   ◇ 已有 managed steps？ ◇
                                           │否                │是
                                           ▼                  │
                              ┌──────────────────────────┐     │
                              │ scheduler-only 普通动作 │     │
                              │ 委托 base AgentPlanner  │     │
                              │ 使用 rethinking_prompt  │     │
                              │ 新 candidate → 图 6     │     │
                              └──────────────────────────┘     │
                                                             ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [F2] 生成或重写完整 operation-only SAFETY_PLAN                             │
│ risk BLOCK：保存 root blocked action，优先同角色安全替代                    │
│ managed scheduler BLOCK：保留 root / goal / loading context / remaining    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [F4] 下一次 propose 选择 managed steps[0]                                  │
│ _next_step() 动态返回 NAVIGATE_TO 或 intended operation                    │
│ 每一条都重新进入图 6 scheduler + risk review                               │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                         ◇ managed action 的 outcome？ ◇
                ┌───────────────────┼──────────────────┬────────────────────┐
                │ 成功              │ risk BLOCK       │ scheduler BLOCK    │ executed failure
                ▼                   └────────┬─────────┘                    ▼
┌──────────────────────────────┐             │                  ┌────────────────────┐
│ navigation：cursor 不动      │             └──────▶ [F2]     │ 停止 planner       │
│ operation：pop steps[0]      │                                │ 记录 execution_error│
│ loading placement：推进顺序  │                                │ → 图 9             │
└──────────────┬───────────────┘                                └────────────────────┘
               ▼
      ◇ managed steps 是否清空？ ◇
        │否                    │是
        ▼                      ▼
      回到 [F4]        清 root/goal，回普通 AgentPlanner
```

## 图 8：ALLOW/CAUTION 后从高层 primitive 到 env.step() 的流程

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G1] ActionReview.allowed == true                                           │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ starter：为 Evaluator 构造带当前 held object 的显式 action 文本             │
│ evaluator.record_action(action)                                             │
│ lifelong process safety before（如已挂载）                                  │
│ evaluator.evaluate_process_safety_goal_condition(action, "before")          │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G2] Executor.execute_plan("OPERATOR(arguments)")                           │
│ ├─ 解析 operator 与 arguments                                               │
│ ├─ 校验 operator 属于当前 primitive_type 词表                              │
│ ├─ 校验参数数量                                                             │
│ ├─ 将 task object id 解析为 simulator object reference                     │
│ ├─ Executor.controller.apply_ref(...)                                       │
│ │   └─ primitive controller 生成 low-level action generator                │
│ └─ 逐个取得 low-level robot action tensor                                   │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G3] 对每一个 low-level action                                              │◀──────────────┐
│ env.step(action tensor)                                                     │               │
│ → Executor.global_step_index += 1                                           │               │
│ → step_callback(LowLevelStepContext)                                        │               │
│    └─ ReplayMediaRecorder 安装的 wrapper                                    │               │
│       ├─ 先调用原 OnlineBenchmark._on_low_level_step()                      │               │
│       │  ├─ 若 scene_graph_step_interval > 0 且命中 interval：              │               │
│       │  │  scene_graph_updater.update(context)                             │               │
│       │  │  → tracker.track_scene_graph(snapshot)                           │               │
│       │  │  → runtime_controller.observe(snapshot)                         │               │
│       │  │     ├─ latest_scene = snapshot                                   │               │
│       │  │     ├─ 计算 StateChange                                          │               │
│       │  │     └─ 更新 ObjectRegistry / visible ids                         │               │
│       │  └─ runtime_controller.tick_scheduler()                             │               │
│       └─ 再按 replay capture_interval 采集 camera frame                     │               │
└───────────────────────────────────┬──────────────────────────────────────────┘               │
                                    │                                                          │
                         ◇ generator 还有 action？ ◇                                            │
                          │是                         │否                                         │
                          └───────────────────────────┼─────────────────────────────────────────┘
                                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G4] Executor 完成或抛错                                                    │
│ debug=false：异常被 benchmark 捕获并记 execution_succeeded=false            │
│ 收集 last_execution_diagnostics                                             │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G5] runtime_controller.record_execution(review, succeeded, diagnostics)     │
│                                                                              │
│ succeeded=true：                                                            │
│ 1. ObjectRegistry.record_action()                                           │
│ 2. 应用可能的 lifecycle directive                                          │
│ 3. perception.note_manipulation_event()                                     │
│ 4. Scheduler.start_from_event()                                             │
│ 5. tick_scheduler()                                                         │
│ 6. rethinking_attempts = 0                                                  │
│ 7. 写 last_outcome / runtime events                                         │
│                                                                              │
│ succeeded=false：不更新 manipulation，不启动新 temporal process             │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G6] action 后评测与高层刷新                                                │
│ tracker.mark_plan_runtime(executed=true, succeeded=...)                     │
│ succeeded 时：lifelong process safety after                                 │
│ evaluator.evaluate_process_safety_goal_condition(action, "after")           │
│ starter 且 succeeded：同步 _starter_grasped_object                          │
│ scene_graph_step_interval <= 0：_refresh_scene_graph()                      │
│   └─ updater.update → tracker → runtime_controller.observe                  │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [G7] 返回 CLI 主循环                                                       │
│ ├─ Replay 在 action 后采集媒体 frame                                        │
│ ├─ execution_ok=false：rethink 则 continue，否则记录错误并 break            │
│ ├─ execution_ok=true：可保存周围视角 observation                            │
│ └─ 下一次 generator.next()：planner 读取 task/timers/blocked outcome       │
│    adapter 专用请求另读取 held object 和可选最新 RGB                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 图 9：循环退出、最终评测、报告、Replay 与清理

```text
                 ┌─────────────────────────────────────────────────┐
                 │ [H1] action loop 退出                           │
                 └────────────────────────┬────────────────────────┘
                                          │
        ┌─────────────────────────────────┼──────────────────────────────────┐
        │                                 │                                  │
        ▼                                 ▼                                  ▼
┌──────────────────┐          ┌────────────────────────┐          ┌────────────────────────┐
│ DONE 成功执行    │          │ planner 返回 None     │          │ 主循环主动 break       │
│ low-level steps=0│          │ exhausted / plan_error│          │ blocked / execution_err│
└────────┬─────────┘          └────────────┬───────────┘          └────────────┬───────────┘
         └────────────────────────────────┼──────────────────────────────────┘
                                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [H2] benchmark.termination_evaluation()                                     │
│ ├─ evaluator.evaluate_execution_goal_condition()                            │
│ ├─ evaluator.evaluate_non_executed_process_safety_goal_condition()          │
│ ├─ evaluator.evaluate_termination_safety_goal_condition()                   │
│ ├─ 已存在 blocked / plan_error / execution_error 时保留原终止原因           │
│ └─ 否则 tracker.track_termination(reason="done")                            │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [H3] 写最终产物                                                            │
│ replay event：termination_evaluated                                         │
│ tracker.save_tracking(output_dir/report.json)                               │
│ _finish_replay(status="completed")                                          │
│ ├─ replay_camera.mp4 / video.mp4 / replay_topdown.mp4                       │
│ ├─ 恢复 media callback 与 Executor wrapper                                  │
│ └─ ReplaySession.finalize(events + frames + media + report + manifest)      │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                         ◇ online_object_sampling？ ◇
                          │否                         │是
                          │                           ▼
                          │          ◇ done 且 execution goal 成功？ ◇
                          │              │是                    │否
                          │              ▼                      ▼
                          │       复制 sampled scene      删除 sampled scene
                          │       到正式 scene 目录       临时文件
                          │              │                      │
                          └──────────────┴──────────┬───────────┘
                                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [H4] finish_run()                                                           │
│ ├─ 默认：sleep(3) → benchmark.close() → og.clear()                          │
│ └─ keep_open_after_done：循环 env.step(hold action)，直到 Ctrl+C            │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [H5] online_benchmark_once() finally                                        │
│ ├─ Replay 尚未 finalize：补做 failed finalize                              │
│ └─ benchmark.close()：停止 simulator → Executor 清理 → 释放 episode 资源   │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 图 10：三个特殊提前结束分支

```text
正常入口
   │
   ├─ online_object_sampling && sample_only
   │    ├─ 保存 sampled task scene JSON
   │    ├─ 复制到正式 scene 目录
   │    ├─ 保存 report_sample_only.json
   │    ├─ _finish_replay()
   │    └─ 非 keep-open：benchmark.close() → os._exit(0)
   │       （os._exit 会绕过 finally，所以必须提前 finalize/close）
   │
   ├─ process / termination / execution 三类评测全部关闭
   │    ├─ 可完成 awareness 生成或评测
   │    ├─ 保存 report_awareness.json
   │    ├─ _finish_replay()
   │    └─ finish_run() → return，不进入 action loop
   │
   └─ 任意未捕获异常
        └─ online_benchmark_once() finally
             ├─ Replay 未完成则 status=failed finalize
             └─ 始终尝试 benchmark.close()
```

## 图 11：最容易产生歧义的时序约束

```text
正确：

构造 OnlineBenchmark
   │
   ├─ env / task / scene / robot / objects
   ├─ Executor / scene graph
   ├─ ObjectRegistry / scheduler / default risk predictor
   ├─ Evaluator / AgentRuntimeController
   │
   ▼
绑定 AgentPlanner + VLMClosedLoopPlannerAdapter
   │
   ├─ graph 非 disabled 且 risk.enabled 时安装 ModelRiskProvider
   │
   ▼
planner 提议 Action
   │
   ▼
已存在的 OnlineBenchmark 审查并执行该 Action


错误理解（当前代码不是这样）：

planner 先产生 Action ──X──▶ 再创建或调用一个新的 OnlineBenchmark
```

## 源码锚点

- 入口和主循环：`og_ego_prim/cli/online_benchmark_once.py::__main__`、`::_online_benchmark_once()`、`::online_benchmark_once()`
- benchmark 工厂：`og_ego_prim/benchmark/__init__.py::build_benchmark()`
- 环境与 runtime 初始化：`og_ego_prim/benchmark/online_benchmark.py::OnlineBenchmark.__init__()`、`::_initialize_agent_runtime()`
- 高层动作执行：`og_ego_prim/benchmark/online_benchmark.py::OnlineBenchmark.execute_plan()`、`::_execute_plan()`
- runtime 闭环：`og_ego_prim/agent_runtime/controller.py::AgentRuntimeController.propose()`、`::iter_actions()`、`::review_action()`、`::record_execution()`
- VLM 闭环 adapter：`og_ego_prim/task_planner/adapters.py::VLMClosedLoopPlannerAdapter`
- 风险评估：`og_ego_prim/risk_predictor/risk_assessor.py::RiskAssessor`
- primitive 执行：`og_ego_prim/primitives/executor.py::Executor.execute_plan()`
