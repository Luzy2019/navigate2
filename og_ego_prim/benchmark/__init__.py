import json
import os
from typing import Optional, TYPE_CHECKING

from og_ego_prim.utils.constants import TASKS
from og_ego_prim.primitives.specs import PrimitiveType

if TYPE_CHECKING:
    from .base_benchmark import Benchmark


def resolve_primitive_type(
    task_config: dict,
    override: Optional[PrimitiveType] = None,
) -> PrimitiveType:
    configured = task_config.get('task_info', {}).get('primitive_type', 'ego')
    if configured not in {'ego', 'starter', 'symbolic'}:
        raise ValueError(f"invalid primitive_type {configured!r} in task config")
    return configured if override is None else override


def build_benchmark(
    task: str, 
    scene: str = None, 
    ego_view: bool = False,
    draw_bbox_2d: bool = False,
    primitive_type: Optional[PrimitiveType] = None,
    scene_graph_step_interval: int = 0,
    scene_graph_backend: str = 'omnigibson_truth',
    use_initial_setup: bool = False,
    use_self_caption: bool = False,
    online_object_sampling: bool = None,
    offline_mode: bool = False,
    debug: bool = False, 
    eval_process_safety: bool = True,
    eval_termination_safety: bool = True,
    eval_awareness: bool = True,
    eval_execution: bool = True,
) -> 'Benchmark':
    from .custom_behavior_task import CustomBehaviorTask  # register customized BehaviorTask
    
    task_config = os.path.join(TASKS, f'{task}.json')
    assert os.path.exists(task_config), f'invalid task config "{task}"'
    with open(task_config, 'r') as f:
        task_config = json.load(f)

    primitive_type = resolve_primitive_type(task_config, primitive_type)
    print(f'primitive_type: {primitive_type}')

    if online_object_sampling is not None:
        task_config['scene_info']['online_object_sampling'] = online_object_sampling
    print(f'online_object_sampling: {task_config["scene_info"]["online_object_sampling"]}')

    task_kwargs = {
        'task': task,
        'scene': scene,
        'config': task_config,
        'debug': debug,
    }

    task_type = task_config['task_info']['task_type']
    if offline_mode:
        raise NotImplemented

    else:
        from og_ego_prim.benchmark.online_benchmark import ONLINE_BENCHMARKS
        from og_ego_prim.benchmark.tracker.online_tracker import OnlineEvalTracker

        task_kwargs.update({
            'ego_view': ego_view,
            'draw_bbox_2d': draw_bbox_2d,
            'primitive_type': primitive_type,
            'scene_graph_step_interval': scene_graph_step_interval,
            'scene_graph_backend': scene_graph_backend,
            'use_initial_setup': use_initial_setup,
            'use_self_caption': use_self_caption,
            'eval_process_safety': eval_process_safety,
            'eval_termination_safety': eval_termination_safety,
            'eval_execution': eval_execution,
            'eval_awareness': eval_awareness,
        })

        assert task_type in ONLINE_BENCHMARKS, \
            f'task_type {task_type} not supported in online mode'
        benchmark = ONLINE_BENCHMARKS[task_type](**task_kwargs)

    return benchmark
