from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()

from og_ego_prim.utils.monkey_patch import add_monkey_patch
add_monkey_patch()

import argparse
import os
import sys

import omnigibson as og
from omnigibson.macros import gm
import shutil
import time
import torch

from og_ego_prim.benchmark import build_benchmark
from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict
from og_ego_prim.risk_predictor.utils import install_vlm_risk_provider
from og_ego_prim.task_planner import (
    AgentPlanner,
    create_planner_adapter,
)
from og_ego_prim.utils.cli_parsing import parse_optional_bool
from og_ego_prim.utils.constants import SCENES
from og_ego_prim.utils.metric import track_planning_latency

# Don't use GPU dynamics and use flatcache for performance boost
gm.USE_GPU_DYNAMICS = True
# gm.ENABLE_FLATCACHE = True

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, default=None)


parser.add_argument('--try_id', type=str, default=None)
# parser.add_argument('--try_id', type=bool, default=True)

parser.add_argument('--task', type=str, default=None)
parser.add_argument('--scene', type=str, default=None)
parser.add_argument('--model', type=str, default=None, help="If not local llm, referece to the model_id, if local_llm, referece to the local model path.")
parser.add_argument('--local_llm_serve', action='store_true')
parser.add_argument('--local_serve_ip', type=str, default="")
parser.add_argument('--local_serve_key', type=str, default="sk-123456")
parser.add_argument('--work_dir', type=str, default=None)

parser.add_argument('--draw_bbox_2d', action='store_true')
parser.add_argument(
    '--primitive_type',
    choices=('auto', 'ego', 'starter', 'symbolic'),
    default=None,
    help='Semantic action implementation. Auto reads task_info.primitive_type and falls back to ego.',
)
parser.add_argument(
    '--scene_graph_step_interval',
    type=int,
    default=None,
    help='Low-level step interval for scene graph updates. Use 0 to update only after each high-level action.',
)
parser.add_argument(
    '--scene_graph_backend',
    choices=('disabled', 'none', 'omnigibson_truth', 'manual_corrected', 'unigoal_grounded_sam', 'samjam_sam2', 'samjam_unigoal'),
    default=None,
    help='Scene graph source. manual_corrected consumes approved native-frame annotations; GroundedSAM and SAM2 require optional perception dependencies.',
)
parser.add_argument('--use_initial_setup', action='store_true', default=None)
parser.add_argument('--use_self_caption', action='store_true', default=None)
parser.add_argument('--online_object_sampling', type=parse_optional_bool, default=None)
parser.add_argument('--sample_only', action='store_true')
parser.add_argument('--debug', action='store_true')
parser.add_argument('--show_robot', action='store_true', default=None, help='Keep the robot visible in the viewer for third-person debugging.')
parser.add_argument(
    '--no_capture_observations',
    action='store_true',
    help='Do not save/reset surrounding viewer observations while executing. Useful for watching the GUI manually.',
)
parser.add_argument(
    '--keep_open_after_done',
    action='store_true',
    help='Keep the viewer open after the run instead of clearing the stage immediately.',
)

parser.add_argument('--not_eval_process_safety', action='store_true')
parser.add_argument('--not_eval_termination_safety', action='store_true')
parser.add_argument('--not_eval_awareness', action='store_true')
parser.add_argument('--not_eval_execution', action='store_true')
parser.add_argument('--prompt_setting', type=str, default=None)
parser.add_argument(
    '--plan-max-steps',
    type=int,
    default=None,
    help=(
        'Maximum number of model-planner high-level actions. If omitted, '
        'retain the legacy example-plan-derived fallback.'
    ),
)


# 格式化输出 output_dir
def _allocate_output_dir(
    work_dir: str,
    benchmark_tag: str,
    model_tag: str,
    try_id: str | None,
) -> str:
    '''
        Usage:
            path = _allocate_output_dir(
                work_dir="results",
                benchmark_tag="store_apple___Wainscott_0_int",
                model_tag="example",
                try_id=None,
            )
        Results:
            results/benchmark/store_apple___Wainscott_0_int/20260720_153012_4821_example
    '''

    root = os.path.join(work_dir, "benchmark", benchmark_tag)
    if try_id:
        return os.path.join(root, f"{try_id}_{model_tag}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = os.path.join(root, f"{stamp}_{os.getpid()}_{model_tag}")
    suffix = 1
    while os.path.exists(candidate):
        candidate = os.path.join(root, f"{stamp}_{os.getpid()}_{suffix}_{model_tag}")
        suffix += 1
    return candidate


# 从 args.config 配置文件中提取参数，并将其应用到 args 中
# 返回更新后的 args 和 RuntimeConfig 对象
def _apply_config(args: argparse.Namespace) -> tuple[argparse.Namespace, RuntimeConfig]:
    config_dict = load_runtime_config_dict(args.config)
    runtime_config = RuntimeConfig.from_mapping(config_dict)
    task_config = runtime_config.task

    config_defaults = {
        "task": task_config.name,
        "scene": task_config.scene,
        "model": task_config.model,
        "work_dir": runtime_config.runtime.output_root,
        "primitive_type": task_config.primitive_type,
        "prompt_setting": task_config.prompt_setting,
        "plan_max_steps": task_config.plan_max_steps,
        "scene_graph_step_interval": runtime_config.scene_graph.step_interval,
        "scene_graph_backend": runtime_config.scene_graph.backend,
        "online_object_sampling": task_config.online_object_sampling,
        "use_initial_setup": task_config.use_initial_setup,
        "use_self_caption": task_config.use_self_caption,
        "show_robot": runtime_config.runtime.show_robot,
    }
    for name, default in config_defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, default)

    args.capture_observations = (
        False
        if args.no_capture_observations
        else bool(runtime_config.artifacts.save_surrounding_observations)
    )
    return args, runtime_config


def _online_benchmark_once(
    try_id,
    task: str,
    scene: str,
    model: str,
    local_llm_serve: str, 
    local_serve_ip: str,
    local_serve_key: str,
    work_dir: str,
    draw_bbox_2d: bool,
    primitive_type: str,
    scene_graph_step_interval: int,
    scene_graph_backend: str,
    use_initial_setup: bool,
    use_self_caption: bool,
    online_object_sampling: bool,
    debug: bool,
    eval_process_safety: bool,
    eval_termination_safety: bool,
    eval_awareness: bool,
    eval_execution: bool,
    sample_only: bool,
    prompt_setting: str,
    show_robot: bool,
    capture_observations: bool,
    keep_open_after_done: bool,
    runtime_config: RuntimeConfig,
    plan_max_steps: int | None = None,
    *,
    benchmark_holder: list,
):
    if local_llm_serve and not model:
        raise ValueError("model is required when local_llm_serve is enabled")
    benchmark = build_benchmark(
        task=task, 
        scene=scene, 
        ego_view=not show_robot,
        draw_bbox_2d=draw_bbox_2d,
        primitive_type=None if primitive_type == 'auto' else primitive_type,
        scene_graph_step_interval=scene_graph_step_interval,
        scene_graph_backend=scene_graph_backend,
        use_initial_setup=use_initial_setup,
        use_self_caption=use_self_caption,
        online_object_sampling=online_object_sampling, 
        debug=debug,
        eval_process_safety=eval_process_safety,
        eval_termination_safety=eval_termination_safety,
        eval_awareness=eval_awareness,
        eval_execution=eval_execution,
        runtime_config=runtime_config,
    )
    benchmark_holder.append(benchmark)
    if debug and gm.HEADLESS is False:
        og.sim.enable_viewer_camera_teleoperation()
    primitive_type = benchmark.primitive_type

    benchmark_tag = f'{benchmark.task_name}___{benchmark.scene_name}'
    model_tag = model.replace('/', '__') if model is not None else 'example'
    
    output_dir = _allocate_output_dir(work_dir, benchmark_tag, model_tag, try_id)
    os.makedirs(output_dir, exist_ok=True)

    def finish_run():
        if keep_open_after_done:
            print('[keep_open_after_done] Viewer is open. Press Ctrl+C in this terminal to exit.')
            if gm.HEADLESS is False:
                og.sim.enable_viewer_camera_teleoperation()
            try:
                while True:
                    action = benchmark.executor.get_hold_action()
                    benchmark.env.step(action)
            except KeyboardInterrupt:
                print('[keep_open_after_done] Exiting without clearing the viewer stage.')
            return

        time.sleep(3)
        benchmark.close()
        og.clear()

    if online_object_sampling:
        fname = f'{scene}_task_{task}_0_0_template'
        sampled_scene_file = os.path.join(output_dir, f'{fname}.json')
        benchmark.env.task.save_task(path=sampled_scene_file)
        if sample_only:
            normal_scene_dir = os.path.join(SCENES, scene, "json")
            os.makedirs(normal_scene_dir, exist_ok=True)
            normal_scene_file = os.path.join(normal_scene_dir, f'{fname}.json')
            shutil.copyfile(sampled_scene_file, normal_scene_file)
            sample_report_path = os.path.join(output_dir, 'report_sample_only.json')
            benchmark.tracker.save_tracking(sample_report_path)
            print(f'[sample_only] saved sampled task scene to {normal_scene_file}', flush=True)
            if not keep_open_after_done:
                benchmark.close()
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(0)
            finish_run()
            return

    agent = None
    if model or local_llm_serve:
        agent = AgentPlanner(
            task_name=task, 
            scene_name=scene, 
            model_name=model,
            work_dir=work_dir,
            local_llm_serve=local_llm_serve, 
            local_serve_ip=local_serve_ip,  
            local_serve_key=local_serve_key, 
            debug=debug,
            prompt_setting=prompt_setting,
            primitive_type=primitive_type,
            use_initial_setup=use_initial_setup,
            use_self_caption=use_self_caption,
            observation_dir=output_dir,
        )
        if not benchmark.scene_graph_updater.disabled:
            install_vlm_risk_provider(benchmark, agent.client)
        agent.set_tracker(benchmark.tracker)
        agent.set_runtime_controller(benchmark.runtime_controller)
        base_planner_adapter = create_planner_adapter(
            'vlm_closed_loop',
            agent,
            use_obs=capture_observations,
            max_step=(
                plan_max_steps
                if plan_max_steps is not None
                else (len(benchmark._example_planning) + 10)
            ),
            held_object_getter=benchmark._current_grasped_object_id,
            enable_loading_preflight=(
                runtime_config.task.enable_loading_preflight
            ),
        )
        benchmark.bind_planner_adapter(
            base_planner_adapter,
            source=type(agent).__name__,
        )
    else:
        base_planner_adapter = create_planner_adapter(
            'example', tuple(dict(plan) for plan in benchmark._example_planning)
        )
        benchmark.bind_planner_adapter(
            base_planner_adapter,
            source='ExamplePlanner',
            emit_proposals=True,
        )
        benchmark.tracker.model = 'example'

    planner = benchmark.runtime_controller.iter_actions()
    planner = track_planning_latency(planner, benchmark.tracker)

    if capture_observations:
        benchmark.get_surrounding_viewer_obs(save_img=os.path.join(output_dir, '0_init'))
    if use_self_caption and agent is not None:
        caption = agent.generate_caption(use_obs=True)
        benchmark.tracker.track_caption(
            content=caption
        )
    if eval_awareness and (model or local_llm_serve):
        awareness = agent.generate_awareness(use_obs=True)
        benchmark.evaluate_awareness(awareness)
    elif prompt_setting == 'v2' and agent is not None:
        awareness = agent.generate_awareness(use_obs=True)
        benchmark.tracker.track_awareness(
            content=awareness,
            eval_results=None
        )
    if not (eval_process_safety or eval_termination_safety or eval_execution):
        benchmark.tracker.save_tracking(os.path.join(output_dir, 'report_awareness.json'))
        finish_run()
        return

    for plan in planner:
        execution_ok = benchmark.execute_plan(plan)
        action_text = (
            plan.to_legacy_plan() if hasattr(plan, 'to_legacy_plan') else plan['action']
        )
        if execution_ok is False:
            review = benchmark.runtime_controller.last_review
            outcome = benchmark.runtime_controller.last_outcome
            if (
                (model or local_llm_serve)
                and review is not None
                and review.should_rethink
            ):
                continue
            if benchmark.tracker.termination is None:
                reason = (
                    outcome.reason or 'blocked_by_scheduler'
                    if outcome is not None and not outcome.executed
                    else 'execution_error'
                )
                benchmark.tracker.track_termination(reason=reason)
            break
        step = benchmark.tracker.plans[-1]['step']
        step_tag = f'{step}_' + action_text.replace('(', '__').replace(')', '__')
        if capture_observations:
            benchmark.get_surrounding_viewer_obs(save_img=os.path.join(output_dir, step_tag))

    benchmark.termination_evaluation()
    benchmark.tracker.save_tracking(os.path.join(output_dir, 'report.json'))
    
    if online_object_sampling:
        if benchmark.tracker.termination['reason'] == 'done' and benchmark.tracker.goal_condition['execution_goal_condition']['eval']: 
            normal_scene_file = os.path.join(work_dir, "..", "data", "scenes", scene, "json", f'{fname}.json')
            shutil.copyfile(sampled_scene_file, normal_scene_file)
        else:
            os.remove(sampled_scene_file)

    finish_run()


def online_benchmark_once(*args, **kwargs):
    benchmarks = []
    try:
        return _online_benchmark_once(
            *args,
            **kwargs,
            benchmark_holder=benchmarks,
        )
    finally:
        for benchmark in reversed(benchmarks):
            try:
                benchmark.close()
            except Exception as exc:
                print(
                    "[online-benchmark][close] "
                    f"{exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


if __name__ == "__main__":
    args = parser.parse_args()
    args, runtime_config = _apply_config(args)
    if not args.task:
        parser.error("--task is required, or set task.name in the YAML config")
    if not args.scene:
        parser.error("--scene is required, or set task.scene in the YAML config")
    if args.plan_max_steps is not None and args.plan_max_steps < 1:
        parser.error('--plan-max-steps must be positive')
    print(f'args: {args}')
    sys.stdout.flush()

    online_benchmark_once(
        try_id=args.try_id,
        task=args.task,
        scene=args.scene,
        model=args.model,
        local_llm_serve=args.local_llm_serve,
        local_serve_ip=args.local_serve_ip,
        local_serve_key=args.local_serve_key,
        prompt_setting=args.prompt_setting,
        work_dir=args.work_dir,
        draw_bbox_2d=args.draw_bbox_2d,
        primitive_type=args.primitive_type,
        scene_graph_step_interval=args.scene_graph_step_interval,
        scene_graph_backend=args.scene_graph_backend,
        use_initial_setup=args.use_initial_setup,
        use_self_caption=args.use_self_caption,
        online_object_sampling=args.online_object_sampling,
        debug=args.debug,
        eval_process_safety=(not args.not_eval_process_safety),
        eval_termination_safety=(not args.not_eval_termination_safety),
        eval_awareness=(not args.not_eval_awareness),
        eval_execution=(not args.not_eval_execution),
        sample_only=args.sample_only,
        show_robot=args.show_robot,
        capture_observations=args.capture_observations,
        keep_open_after_done=args.keep_open_after_done,
        plan_max_steps=args.plan_max_steps,
        runtime_config=runtime_config,
    )
