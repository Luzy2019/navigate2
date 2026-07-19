# scripts/test_all.py 参数说明

本文档对 `scripts/test_all.py` 入口脚本的全部命令行参数进行分类汇总，说明每个参数的作用及在代码中的使用位置。

## 1. 任务与场景配置

| 参数 | 默认值 | 作用 | 使用位置 |
|------|--------|------|----------|
| `--task` | `store_apple_and_tissue_box_in_bottom_cabinet` | 指定要运行的任务名称 | `validate_and_normalize_task` 中 `get_task_config_path(args.task)`；`build_benchmark(task=args.task)`；`PlanningAgent(task_name=args.task)`；`task_room_from_args`；`save_scene_graph_report` |
| `--scene` | `None` | 场景模型，未指定时使用任务配置中的 `default_scene_model` | `validate_and_normalize_task` 中赋值与校验；`build_benchmark(scene=args.scene)`；`PlanningAgent(scene_name=args.scene)`；`save_scene_graph_report` |
| `--primitive-type` | `auto` | benchmark 和 PlanningAgent 使用的原语集（auto/ego/starter/symbolic） | `build_benchmark(primitive_type=None if "auto" else args.primitive_type)`；间接影响 `PlanningAgent(primitive_type=benchmark.primitive_type)` 和 `prompt_setting` 默认值 |

## 2. PlanningAgent 模型配置

| 参数 | 默认值 | 作用 | 使用位置 |
|------|--------|------|----------|
| `--model` | `None` | PlanningAgent 模型名称，省略时执行固定 `example_planning` | `parse_args` 中校验 `--local-llm-serve`；`get_planner_paths` 中生成 `model_tag`；`PlanningAgent(agent_name=args.model)`；`main` 中决定是否启用 PlanningAgent 分支 |
| `--local-llm-serve` | `False` | 使用 OpenAI 兼容的本地模型服务器 | `parse_args` 中校验需配合 `--model`；`PlanningAgent(local_llm_serve=...)` |
| `--local-serve-ip` | `""` | 本地模型服务器 IP 地址 | `PlanningAgent(local_serve_ip=...)` |
| `--local-serve-key` | `sk-123456` | 本地模型服务器 API key | `PlanningAgent(local_serve_key=...)` |
| `--work-dir` | `None` | PlanningAgent 工作目录，默认 `OUTPUT_DIR/planner_work_dir` | `get_planner_paths` 中解析路径 |
| `--prompt-setting` | `default` | PlanningAgent 提示词变体（default/v0/v1/v2/v3） | `main` 中：若为 `default` 且 primitive 非 starter 则改为 `v1`；`PlanningAgent(prompt_setting=...)` |
| `--use-initial-setup` | `False` | 在 PlanningAgent 提示词中包含任务初始设置文本 | `build_benchmark(use_initial_setup=...)`；`PlanningAgent(use_initial_setup=...)` |
| `--use-self-caption` | `False` | PlanningAgent 开始规划前生成视觉场景描述 | `build_benchmark(use_self_caption=...)`；`PlanningAgent(use_self_caption=...)`；`main` 中 `agent.generate_caption()` |
| `--planner-use-obs` | `True` | 向 PlanningAgent 提供保存的多视角观测 | `save_planner_observations`；`agent.step(use_obs=...)`；`agent.generate_caption(use_obs=...)`；`agent.generate_awareness(use_obs=...)` |
| `--planner-debug` | `False` | 每次 PlanningAgent 模型请求前暂停确认 | `PlanningAgent(debug=...)` |
| `--plan-max-steps` | `None` | 最大生成规划步数，默认为示例计划长度 +10 | `parse_args` 中校验 >0；`main` 中若为 None 则 `len(benchmark._example_planning) + 10`；`agent.step(max_step=...)` |

## 3. 输出与保存控制

| 参数 | 默认值 | 作用 | 使用位置 |
|------|--------|------|----------|
| `--output-dir` | `None` | 保存 RGB 图像、视频、报告的基础目录，默认 `outputs/test_TASK_full` | `validate_and_normalize_task` 中赋值；`get_run_output_dir(args.output_dir, ...)` |
| `--timestamp-output` | `True` | 为每次运行在 OUTPUT_DIR 后追加 `YYYYMMDD_HHMMSS` 时间戳 | `get_run_output_dir(args.output_dir, args.timestamp_output)` |
| `--output-size` | `512x512` | 调整保存的 RGB 输出尺寸（WIDTHxHEIGHT / 单整数 / original） | 多处 `save_robot_rgb(..., args.output_size)`；`capture_robot_rgb_frame(robot, args.output_size)`；`track_robot_rgb_video(..., args.output_size)` |
| `--save-step-images` | `True` | 每个高层动作后保存一张机器人 FPV png | `save_step_image` 函数中判断（注：该函数在 `main` 中未被直接调用，实际由内联的 `save_robot_rgb_with_frame` 替代） |
| `--save-video` | `True` | 将捕获的机器人 RGB 帧保存为 mp4 | `wrapped_step_callback` 中；`main` 中多处 `if args.save_video:`；`save_report_and_video` |
| `--video-path` | `None` | 输出 mp4 路径，默认 `OUTPUT_DIR/nav_rgb.mp4` | `save_report_and_video` 中 |
| `--video-fps` | `30.0` | 视频帧率 | `benchmark.tracker.video_fps = args.video_fps`；`save_rgb_video(..., args.video_fps)` |
| `--capture-during-actions` | `True` | 在所有低层原语步骤中定期捕获机器人 FPV 帧 | `wrapped_step_callback` 中 `if not args.capture_during_actions or not args.save_video: return` |
| `--capture-every` | `2` | 每 N 个低层原语步骤捕获一帧 | `capture_every = max(args.capture_every, 1)`；`if context.step_index % capture_every != 0: return` |
| `--save-surrounding-observations` | `False` | 每个高层动作后也保存 OnlineBenchmark 多视角观测 | `main` 中多处 `if args.save_surrounding_observations:` 调用 `save_surrounding_observations` |
| `--show-robot` | `False` | 在 viewer 风格捕获中显示机器人（非 ego 视角） | `build_benchmark(ego_view=not args.show_robot)` |

## 4. 运行模式控制

| 参数 | 默认值 | 作用 | 使用位置 |
|------|--------|------|----------|
| `--validate-only` | `False` | 仅验证任务 JSON、BDDL、场景、对象和示例动作，不启动 OmniGibson | `validate_and_normalize_task` 后 `if ARGS.validate_only: raise SystemExit(0)` |
| `--init-only` | `False` | 初始化 OmniGibson 和任务后退出，不执行计划 | `main` 中 `if args.init_only:` 打印信息后退出 |
| `--headless` | `True` | 无头模式启动 OmniGibson | `if args.headless: os.environ["OMNIGIBSON_HEADLESS"] = "1"` |
| `--clear-on-exit` | `False` | 退出前调用 `og.clear()`，默认禁用以避免 Kit 拆解崩溃 | `main` 末尾 `if args.clear_on_exit: og.clear()` |
| `--stop-on-error` | `False` | 第一个失败动作后停止执行计划 | `main` 中 `if not execution_succeeded and args.stop_on_error: break` |

## 5. 场景图配置

| 参数 | 默认值 | 作用 | 使用位置 |
|------|--------|------|----------|
| `--scene-graph-backend` | 环境变量或 `samjam_unigoal` | OnlineBenchmark 使用的场景图后端 | 强制 samjam 后端 `update_every=1`；写入 `ISBENCH_SCENE_GRAPH_BACKEND` 环境变量；`build_benchmark(scene_graph_backend=...)`；`main` 中多处分支判断；`save_scene_graph_report` |
| `--scene-graph-step-interval` | `0` | 场景图更新的低层步长间隔，0 表示每个高层动作后更新 | `build_benchmark(scene_graph_step_interval=...)`；`save_scene_graph_report`；打印信息 |
| `--scene-graph-update-every` | 环境变量或 `1` | 高级感知跳过间隔，samjam 后端强制为 1 | 强制赋值；写入 `ISBENCH_SCENE_GRAPH_UPDATE_EVERY` 环境变量；`save_scene_graph_report`；打印信息 |
| `--scene-graph-history-interval` | 环境变量或 `1` | 每 N 次场景图更新保存一次历史快照 | 写入 `ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL` 环境变量；`save_scene_graph_report`；打印信息 |
| `--scene-graph-image-size` | 环境变量或 `512x512` | 感知场景图后端使用的机器人视觉传感器分辨率 | 写入 `ISBENCH_SCENE_GRAPH_IMAGE_WIDTH/HEIGHT` 环境变量；打印信息 |

## 6. 导航参数覆盖

| 参数 | 默认值 | 作用 | 使用位置 |
|------|--------|------|----------|
| `--nav-stuck-waypoint-tolerance` | `None` | 覆盖 `ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE`，导航接近但未到达航点时调整卡住判定 | 写入 `os.environ["ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE"]` |
| `--nav-stuck-final-waypoint-tolerance` | `None` | 覆盖 `ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE`，最终航点容差至少 0.30m | 写入 `os.environ["ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE"]` |
| `--nav-goal-clearance-radius` | `None` | 覆盖 `ISBENCH_NAV_GOAL_CLEARANCE_RADIUS`，候选导航目标需有额外可通行地图间隙 | 写入 `os.environ["ISBENCH_NAV_GOAL_CLEARANCE_RADIUS"]` |
| `--nav-max-floor-height-delta` | `None` | 覆盖 `ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA`，机器人基座高度偏离当前楼层超过此值时中止导航 | 写入 `os.environ["ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA"]` |

---

**补充说明**：`--save-step-images` 参数虽然在 `save_step_image` 函数中被检查，但该函数在 `main()` 中实际未被调用（被内联的 `save_robot_rgb_with_frame` 替代），因此该参数目前是一个**遗留参数**，不产生实际效果。
