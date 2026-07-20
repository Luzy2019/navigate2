"""Single-task debug runner backed by explicit YAML runtime config."""

from __future__ import annotations

import argparse
import json
from itertools import islice
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Iterable, Optional

from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict, parse_size
from og_ego_prim.utils.cli_parsing import parse_optional_bool


def _flag_present(*flags: str) -> bool:
    return any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in sys.argv[1:]
        for flag in flags
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one IS-Bench task for debugging.")
    parser.add_argument("--config", default="entrypoints/configs/eval_debug.yaml")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--task")
    parser.add_argument("--scene")
    parser.add_argument("--model")
    parser.add_argument("--work-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan-max-steps", type=int)
    parser.add_argument("--stop-on-error", action=argparse.BooleanOptionalAction)
    parser.add_argument("--primitive-type", choices=("auto", "ego", "starter", "symbolic"))
    parser.add_argument("--prompt-setting")
    parser.add_argument("--scene-graph-backend")
    parser.add_argument("--scene-graph-step-interval", type=int)
    parser.add_argument("--scene-graph-history-interval", type=int)
    parser.add_argument("--scene-graph-image-size")
    parser.add_argument("--nav-stuck-waypoint-tolerance", type=float)
    parser.add_argument("--nav-stuck-final-waypoint-tolerance", type=float)
    parser.add_argument("--nav-goal-clearance-radius", type=float)
    parser.add_argument("--nav-max-floor-height-delta", type=float)
    parser.add_argument("--online-object-sampling", type=parse_optional_bool)
    parser.add_argument("--sample-only", action="store_true")
    parser.add_argument("--use-initial-setup", action="store_true")
    parser.add_argument("--use-self-caption", action="store_true")
    parser.add_argument("--planner-use-obs", action=argparse.BooleanOptionalAction)
    parser.add_argument("--draw-bbox-2d", action="store_true")
    parser.add_argument("--show-robot", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--keep-open-after-done", action="store_true")
    parser.add_argument("--save-video", action=argparse.BooleanOptionalAction)
    parser.add_argument("--video-fps", type=float)
    parser.add_argument("--video-output-size")
    parser.add_argument("--save-topdown-scene", action=argparse.BooleanOptionalAction)
    parser.add_argument("--topdown-world-bounds", nargs=4, type=float)
    parser.add_argument("--topdown-output-size")
    parser.add_argument("--no-capture-observations", action="store_true")
    return parser


def _set_nested(config: Dict[str, Any], section: str, key: str, value: Any) -> None:
    if value is not None:
        config.setdefault(section, {})[key] = value


def apply_config(args: argparse.Namespace) -> tuple[argparse.Namespace, RuntimeConfig]:
    config = load_runtime_config_dict(args.config)
    if _flag_present("--task"):
        _set_nested(config, "task", "name", args.task)
    if _flag_present("--scene"):
        _set_nested(config, "task", "scene", args.scene)
    if _flag_present("--model"):
        _set_nested(config, "task", "model", args.model)
    if _flag_present("--work-dir"):
        _set_nested(config, "runtime", "output_root", args.work_dir)
    if _flag_present("--output-dir"):
        _set_nested(config, "artifacts", "output_dir", args.output_dir)
    if _flag_present("--plan-max-steps"):
        _set_nested(config, "task", "plan_max_steps", args.plan_max_steps)
    if _flag_present("--stop-on-error", "--no-stop-on-error"):
        _set_nested(config, "task", "stop_on_error", args.stop_on_error)
    if _flag_present("--primitive-type"):
        _set_nested(config, "task", "primitive_type", args.primitive_type)
    if _flag_present("--prompt-setting"):
        _set_nested(config, "task", "prompt_setting", args.prompt_setting)
    if _flag_present("--scene-graph-backend"):
        _set_nested(config, "scene_graph", "backend", args.scene_graph_backend)
    if _flag_present("--scene-graph-step-interval"):
        _set_nested(config, "scene_graph", "step_interval", args.scene_graph_step_interval)
    if _flag_present("--scene-graph-history-interval"):
        _set_nested(config, "scene_graph", "history_interval", args.scene_graph_history_interval)
    if _flag_present("--scene-graph-image-size"):
        _set_nested(config, "scene_graph", "image_size", args.scene_graph_image_size)
    if _flag_present("--nav-stuck-waypoint-tolerance"):
        _set_nested(config, "navigation", "stuck_waypoint_tolerance", args.nav_stuck_waypoint_tolerance)
    if _flag_present("--nav-stuck-final-waypoint-tolerance"):
        _set_nested(
            config,
            "navigation",
            "stuck_final_waypoint_tolerance",
            args.nav_stuck_final_waypoint_tolerance,
        )
    if _flag_present("--nav-goal-clearance-radius"):
        _set_nested(config, "navigation", "goal_clearance_radius", args.nav_goal_clearance_radius)
    if _flag_present("--nav-max-floor-height-delta"):
        _set_nested(config, "navigation", "max_floor_height_delta", args.nav_max_floor_height_delta)
    if _flag_present("--online-object-sampling"):
        _set_nested(config, "task", "online_object_sampling", args.online_object_sampling)
    if _flag_present("--use-initial-setup"):
        _set_nested(config, "task", "use_initial_setup", True)
    if _flag_present("--use-self-caption"):
        _set_nested(config, "task", "use_self_caption", True)
    if _flag_present("--planner-use-obs", "--no-planner-use-obs"):
        _set_nested(config, "task", "planner_use_obs", args.planner_use_obs)
    if _flag_present("--show-robot"):
        _set_nested(config, "runtime", "show_robot", True)
    if _flag_present("--save-video", "--no-save-video"):
        _set_nested(config, "artifacts", "save_video", args.save_video)
    if _flag_present("--video-fps"):
        _set_nested(config, "artifacts", "video_fps", args.video_fps)
    if _flag_present("--video-output-size"):
        _set_nested(config, "artifacts", "output_size", args.video_output_size)
    if _flag_present("--save-topdown-scene", "--no-save-topdown-scene"):
        _set_nested(config, "artifacts", "save_topdown_scene", args.save_topdown_scene)
    if _flag_present("--topdown-world-bounds"):
        _set_nested(config, "artifacts", "topdown_world_bounds", args.topdown_world_bounds)
    if _flag_present("--topdown-output-size"):
        _set_nested(config, "artifacts", "topdown_output_size", args.topdown_output_size)

    runtime_config = RuntimeConfig.from_mapping(config)
    args.task = runtime_config.task.name
    args.scene = runtime_config.task.scene
    args.model = runtime_config.task.model
    args.work_dir = runtime_config.runtime.output_root
    args.output_dir = runtime_config.artifacts.output_dir
    args.plan_max_steps = runtime_config.task.plan_max_steps
    args.stop_on_error = runtime_config.task.stop_on_error
    args.primitive_type = runtime_config.task.primitive_type
    args.prompt_setting = runtime_config.task.prompt_setting
    args.scene_graph_backend = runtime_config.scene_graph.backend
    args.scene_graph_step_interval = runtime_config.scene_graph.step_interval
    args.online_object_sampling = runtime_config.task.online_object_sampling
    args.use_initial_setup = runtime_config.task.use_initial_setup
    args.use_self_caption = runtime_config.task.use_self_caption
    args.planner_use_obs = runtime_config.task.planner_use_obs
    args.show_robot = runtime_config.runtime.show_robot
    args.save_video = runtime_config.artifacts.save_video
    args.video_fps = runtime_config.artifacts.video_fps
    args.video_output_size = runtime_config.artifacts.output_size
    return args, runtime_config


def _output_dir(args: argparse.Namespace, runtime_config: RuntimeConfig) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    model_tag = args.model.replace("/", "__") if args.model else "example"
    return (
        Path(args.work_dir)
        / "debug"
        / f"{args.task}___{args.scene}"
        / model_tag
    )


def _iter_plans(planner: Iterable[Any], max_steps: Optional[int]) -> Iterable[Any]:
    limited = planner if max_steps is None else islice(planner, max(int(max_steps), 0))
    for index, plan in enumerate(limited, 1):
        yield index, plan


def _safe_step_tag(index: int, action: str) -> str:
    text = str(action).replace("(", "__").replace(")", "__").replace("/", "_")
    return f"{index}_{text}"


def _run(
    args: argparse.Namespace,
    runtime_config: RuntimeConfig,
    *,
    benchmark_holder: list,
) -> Path:
    from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

    if runtime_config.runtime.headless:
        os.environ["OMNIGIBSON_HEADLESS"] = "1"
    maybe_reexec_with_omnigibson_python()
    sys.argv = [sys.argv[0]]

    from og_ego_prim.utils.monkey_patch import add_monkey_patch

    add_monkey_patch()
    import omnigibson as og
    from omnigibson.macros import gm

    gm.USE_GPU_DYNAMICS = True

    from og_ego_prim.benchmark import build_benchmark
    from og_ego_prim.task_planner import (
        AgentPlanner,
        create_planner_adapter,
    )
    from og_ego_prim.cli.safe_memory_benchmark_once import capture_robot_rgb_frame
    from og_ego_prim.utils.metric import track_planning_latency

    benchmark = build_benchmark(
        task=args.task,
        scene=args.scene,
        ego_view=not args.show_robot,
        draw_bbox_2d=args.draw_bbox_2d,
        primitive_type=None if args.primitive_type == "auto" else args.primitive_type,
        scene_graph_step_interval=args.scene_graph_step_interval,
        scene_graph_backend=args.scene_graph_backend,
        use_initial_setup=args.use_initial_setup,
        use_self_caption=args.use_self_caption,
        online_object_sampling=args.online_object_sampling,
        debug=args.debug,
        runtime_config=runtime_config,
    )
    benchmark_holder.append(benchmark)
    output_dir = _output_dir(args, runtime_config)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.online_object_sampling:
        sampled_scene_path = output_dir / f"{args.scene}_task_{args.task}_0_0_template.json"
        benchmark.env.task.save_task(path=str(sampled_scene_path))
        if args.sample_only:
            benchmark.tracker.save_tracking(str(output_dir / "report_sample_only.json"))
            print(f"[sample_only] saved sampled task scene to {sampled_scene_path}", flush=True)
            benchmark.close()
            og.clear()
            return output_dir / "report_sample_only.json"

    agent = None
    if args.model:
        agent = AgentPlanner(
            task_name=args.task,
            scene_name=args.scene,
            model_name=args.model,
            work_dir=str(output_dir),
            debug=args.debug,
            prompt_setting=args.prompt_setting,
            primitive_type=benchmark.primitive_type,
            use_initial_setup=args.use_initial_setup,
            use_self_caption=args.use_self_caption,
        )
        agent.set_tracker(benchmark.tracker)
        agent.set_runtime_controller(benchmark.runtime_controller)
        planner_adapter = create_planner_adapter(
            'model',
            agent,
            use_obs=args.planner_use_obs,
            max_step=args.plan_max_steps or (len(benchmark._example_planning) + 10),
        )
        benchmark.bind_planner_adapter(planner_adapter, source=type(agent).__name__)
    else:
        planner_adapter = create_planner_adapter(
            'example', tuple(dict(plan) for plan in benchmark._example_planning)
        )
        benchmark.bind_planner_adapter(
            planner_adapter,
            source='ExamplePlanner',
            emit_proposals=True,
        )
        benchmark.tracker.model = 'example'
    planner = benchmark.runtime_controller.iter_actions()
    planner = track_planning_latency(planner, benchmark.tracker)

    if runtime_config.artifacts.save_surrounding_observations and not args.no_capture_observations:
        benchmark.get_surrounding_viewer_obs(save_img=str(output_dir / "0_init"))
    if args.save_video and benchmark.env.robots:
        benchmark.tracker.track_video_rgb(
            capture_robot_rgb_frame(benchmark.env.robots[0], args.video_output_size)
        )

    processed_steps = 0
    last_action = None
    for index, plan in _iter_plans(planner, args.plan_max_steps):
        processed_steps = index
        ok = benchmark.execute_plan(plan)
        action = (
            plan.to_legacy_plan()
            if hasattr(plan, 'to_legacy_plan')
            else plan["action"] if isinstance(plan, dict) else str(plan)
        )
        last_action = action
        if args.save_video and benchmark.env.robots:
            benchmark.tracker.track_video_rgb(
                capture_robot_rgb_frame(benchmark.env.robots[0], args.video_output_size)
            )
        if runtime_config.artifacts.save_surrounding_observations and not args.no_capture_observations:
            benchmark.get_surrounding_viewer_obs(
                save_img=str(output_dir / _safe_step_tag(index, action))
            )
        if ok is False:
            review = benchmark.runtime_controller.last_review
            outcome = benchmark.runtime_controller.last_outcome
            if outcome is not None and not outcome.executed:
                if args.model and review is not None and review.should_rethink:
                    agent.note_runtime_review(review)
                    continue
                if benchmark.tracker.termination is None:
                    benchmark.tracker.track_termination(
                        reason=outcome.reason or "blocked_by_scheduler"
                    )
                break
            if not args.model or args.stop_on_error:
                if benchmark.tracker.termination is None:
                    benchmark.tracker.track_termination(reason="execution_error")
                break

    if (
        args.plan_max_steps is not None
        and processed_steps >= args.plan_max_steps
        and not str(last_action or "").strip().lower().startswith("done")
        and benchmark.tracker.termination is None
    ):
        benchmark.tracker.track_termination(
            reason="exceeding_max_steps",
            type="PlannerLimit",
            msg=f"exceeding max steps {args.plan_max_steps}",
        )

    benchmark.termination_evaluation()
    if runtime_config.artifacts.save_topdown_scene:
        try:
            from og_ego_prim.utils.topdown_capture import capture_topdown_scene

            capture_topdown_scene(
                benchmark.env,
                output_dir / "topdown_scene.png",
                world_bounds=runtime_config.artifacts.topdown_world_bounds,
                snapshot=benchmark.tracker.latest_scene_graph,
                execution_diagnostics=benchmark.tracker.execution_diagnostics,
                output_size=runtime_config.artifacts.topdown_output_size,
                metadata_path=output_dir / "topdown_scene.json",
            )
        except Exception as exc:
            benchmark.tracker.track_error(
                action="save_topdown_scene",
                err_type=exc.__class__.__name__,
                msg=str(exc),
            )
    report_path = output_dir / "report.json"
    benchmark.tracker.save_tracking(str(report_path))
    (output_dir / "runtime_config.json").write_text(
        json.dumps(runtime_config.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.keep_open_after_done:
        print("[debug] Viewer is open. Press Ctrl+C to exit.", flush=True)
        try:
            while True:
                benchmark.env.step(benchmark.executor.get_hold_action())
        except KeyboardInterrupt:
            pass
    else:
        time.sleep(1)
        benchmark.close()
        og.clear()
    return report_path


def run(args: argparse.Namespace, runtime_config: RuntimeConfig) -> Path:
    benchmarks = []
    try:
        return _run(
            args,
            runtime_config,
            benchmark_holder=benchmarks,
        )
    finally:
        for benchmark in reversed(benchmarks):
            try:
                benchmark.close()
            except Exception as exc:
                print(
                    "[debug][close] "
                    f"{exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args, runtime_config = apply_config(args)
    if not args.task:
        parser.error("--task is required, or set task.name in the YAML config")
    if not args.scene:
        parser.error("--scene is required, or set task.scene in the YAML config")
    if args.validate_only:
        print(json.dumps(runtime_config.to_dict(), ensure_ascii=False, indent=2))
        return
    report_path = run(args, runtime_config)
    print(f"debug report: {report_path}", flush=True)


if __name__ == "__main__":
    main()
