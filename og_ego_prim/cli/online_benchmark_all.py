import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed, wait
import datetime
import json
import multiprocessing
import os
import subprocess
import time
import warnings
from pathlib import Path
from typing import List, Optional, Tuple

from tqdm import tqdm

from og_ego_prim.utils.constants import CAMERAS
from og_ego_prim.utils.metric import Metric, read_benchmark_report
from og_ego_prim.utils.task_registry import (
    get_task_config_path,
    iter_task_config_paths,
)
from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, default=None)
parser.add_argument('--model', type=str, default=None)
parser.add_argument('--work_dir', type=str, default='./work_dir')
parser.add_argument('--task_list', type=str, default=None)
parser.add_argument('--not_use_default_scene_model', action='store_true')
parser.add_argument('--data_parallel', type=int, default=1)
parser.add_argument('--online_object_sampling', type=bool, default=None)
parser.add_argument('--num_retry', type=int, default=3)
parser.add_argument('--local_llm_serve', action='store_true')
parser.add_argument('--local_serve_ip', type=str, default=None)
parser.add_argument('--draw_bbox_2d', action='store_true')
parser.add_argument(
    '--primitive_type',
    choices=('auto', 'ego', 'starter', 'symbolic'),
    default='auto',
)
parser.add_argument('--show_robot', action='store_true')
parser.add_argument('--use_initial_setup', action='store_true')
parser.add_argument('--use_self_caption', action='store_true')
parser.add_argument('--prompt_setting', type=str, default='default')

parser.add_argument('--not_eval_process_safety', action='store_true')
parser.add_argument('--not_eval_termination_safety', action='store_true')
parser.add_argument('--not_eval_awareness', action='store_true')
parser.add_argument('--not_eval_execution', action='store_true')


def _flag_present(flag: str) -> bool:
    import sys

    return any(arg == flag or arg.startswith(f"{flag}=") for arg in sys.argv[1:])


def _apply_config(args: argparse.Namespace) -> tuple[argparse.Namespace, RuntimeConfig]:
    config_dict = load_runtime_config_dict(args.config)
    runtime_config = RuntimeConfig.from_mapping(config_dict)
    task_config = runtime_config.task
    if not _flag_present('--model') and args.model is None:
        args.model = task_config.model
    if not _flag_present('--work_dir'):
        args.work_dir = runtime_config.runtime.output_root
    if not _flag_present('--primitive_type'):
        args.primitive_type = task_config.primitive_type
    if not _flag_present('--prompt_setting'):
        args.prompt_setting = task_config.prompt_setting
    if not _flag_present('--use_initial_setup') and task_config.use_initial_setup:
        args.use_initial_setup = True
    if not _flag_present('--use_self_caption') and task_config.use_self_caption:
        args.use_self_caption = True
    if not _flag_present('--show_robot') and runtime_config.runtime.show_robot:
        args.show_robot = True
    return args, runtime_config

def get_time_tag() -> str:
    return datetime.datetime.now().strftime('%Y%m%d-%H:%M:%S')


def get_model_tag(model: str) -> str:
    return model.replace('/', '__') if model is not None else 'example'


def get_all_tasks(
    task_list: Optional[str] = None, 
    use_default_scene_model: bool = True,
) -> List[Tuple[str, str]]:

    if task_list is None:
        task_paths = iter_task_config_paths(include_subdirs=False)
    elif not os.path.exists(task_list):
        raise FileNotFoundError(f'task list does not exist: "{task_list}"')
    else:
        with open(task_list, 'r') as f:
            task_specs = [
                task.strip()
                for task in f.readlines()
                if task.strip() and not task.strip().startswith("#")
            ]
        task_paths = []
        for task_spec in task_specs:
            try:
                task_paths.append(get_task_config_path(task_spec))
            except (FileNotFoundError, ValueError) as exc:
                warnings.warn(str(exc))

    with open(os.path.join(CAMERAS, 'camera.json'), 'r') as f:
        camera_config = json.load(f)
    
    print(f"[INFO]######\n{[str(path) for path in task_paths]}")
    all_tasks = []
    for task_path in task_paths:
        with open(task_path, 'r') as f:
            task_config = json.load(f)
        task_name = task_config['task_info']['task_name']
        scene_info = task_config['scene_info']

        # prepare scene_models
        if use_default_scene_model:
            if 'default_scene_model' not in scene_info:
                warnings.warn(f'task "{task_name}" has not key "default_scene_model", skip')
                continue
            scene_model = scene_info['default_scene_model']

            if scene_model not in scene_info['scene_models']:
                warnings.warn(f'default_scene_model "{scene_model}" not supported in task "{task_name}", skip')
                continue
            scene_models = [scene_model]
        else:
            scene_models = scene_info['scene_models']
        
        # check camera_config
        for scene_model in scene_models:
            room = f'{scene_info["room"]}__{scene_model}'
            if room not in camera_config:
                warnings.warn(f'room "{room}" in scene_model "{scene_model}" has no camera config, skip')
                continue

            all_tasks.append((task_name, scene_model))

    print(f'[{get_time_tag()}][tasks] Totally {len(all_tasks)} requests')
    return all_tasks


def get_launcher(
    task_name: str, 
    scene_name: str, 
    model: str = None, 
    work_dir: str = None,
    online_object_sampling: bool=None,
    draw_bbox_2d: bool=None,
    primitive_type: str='auto',
    show_robot: bool=False,
    use_initial_setup: bool=None,
    use_self_caption: bool=None,
    local_llm_serve: bool=None,
    local_serve_ip: str=None,
    prompt_setting: str=None,
    not_eval_process_safety: bool=None,
    not_eval_termination_safety: bool=None,
    not_eval_awareness: bool=None,
    not_eval_execution: bool=None,
    config: str=None,
) -> List[str]:
    entrypoint = []

    if 'PARTITION' in os.environ:
        partition = os.environ.get('PARTITION', None)
        assert partition is not None
        num_gpu_per_task = os.environ.get('NUM_GPUS', 1)
        entrypoint.extend([
            'srun', '-p', partition,
            f'--gres=gpu:{num_gpu_per_task}',
            '-N', '1'
        ])

    if 'APPTAINER_IMAGE' in os.environ:
        entrypoint.extend(['apptainer', 'run', '--nv'])
        path_binding = os.environ.get('BINDING')
        if path_binding:
            entrypoint.extend(['--bind', path_binding])
        image = os.environ['APPTAINER_IMAGE']
        entrypoint.append(image)
    
    repo_root = Path(__file__).resolve().parents[2]
    python_launcher = repo_root / "entrypoints" / "omnigibson_python.sh"
    python_command = [str(python_launcher)] if python_launcher.exists() else ["python"]

    entrypoint.extend(python_command + [
        '-m', 'og_ego_prim.cli.online_benchmark_once',
        '--task', task_name, 
        '--scene', scene_name,
    ])
    if config is not None:
        entrypoint.extend(['--config', config])
    if model is not None:
        entrypoint.extend(['--model', model])
    if work_dir is not None:
        entrypoint.extend(['--work_dir', work_dir])
    if online_object_sampling is not None and online_object_sampling:
        entrypoint.extend(['--online_object_sampling', 'True'])
    if draw_bbox_2d is not None and draw_bbox_2d:
        entrypoint.extend(['--draw_bbox_2d'])
    if primitive_type is not None:
        entrypoint.extend(['--primitive_type', primitive_type])
    if show_robot:
        entrypoint.extend(['--show_robot'])
    if use_initial_setup is not None and use_initial_setup:
        entrypoint.extend(['--use_initial_setup'])
    if use_self_caption is not None and use_self_caption:
        entrypoint.extend(['--use_self_caption'])
    if local_llm_serve is not None and local_llm_serve:
        entrypoint.extend(['--local_llm_serve'])
    if local_serve_ip is not None:
        entrypoint.extend(['--local_serve_ip', local_serve_ip])
    if prompt_setting is not None:
        entrypoint.extend(['--prompt_setting', prompt_setting])
    if not_eval_process_safety is not None and not_eval_process_safety:
        entrypoint.extend(['--not_eval_process_safety'])
    if not_eval_termination_safety is not None and not_eval_termination_safety:
        entrypoint.extend(['--not_eval_termination_safety'])
    if not_eval_awareness is not None and not_eval_awareness:
        entrypoint.extend(['--not_eval_awareness'])
    if not_eval_execution is not None and not_eval_execution:
        entrypoint.extend(['--not_eval_execution'])

    return entrypoint


def worker(task_name: str, scene_name: str, model: str, work_dir: str, online_object_sampling: bool, retry: int, draw_bbox_2d: bool, primitive_type: str, show_robot: bool, use_initial_setup: bool, use_self_caption: bool, local_llm_serve: bool, local_serve_ip: str, prompt_setting: str, not_eval_process_safety: bool, not_eval_termination_safety: bool, not_eval_awareness: bool, not_eval_execution: bool, stream_logs: bool, config: str):
    worker_id = multiprocessing.current_process()._identity[0]
    time.sleep(worker_id * 0.5)
    print(f'[{get_time_tag()}][worker_{worker_id}] Processing "{task_name}___{scene_name}"')

    log_dir = os.path.join(os.path.dirname(work_dir), 'logs')
    os.makedirs(log_dir, exist_ok=True)

    benchmark_tag = f'{task_name}___{scene_name}'
    model_tag = get_model_tag(model)
    time_tag = get_time_tag()
    log_file = os.path.join(log_dir, f'benchmark_{benchmark_tag}_{model_tag}_{time_tag}.log')

    launcher = get_launcher(
        task_name,
        scene_name,
        model,
        work_dir,
        online_object_sampling,
        draw_bbox_2d,
        primitive_type,
        show_robot,
        use_initial_setup,
        use_self_caption,
        local_llm_serve,
        local_serve_ip,
        prompt_setting,
        not_eval_process_safety,
        not_eval_termination_safety,
        not_eval_awareness,
        not_eval_execution,
        config,
    )
    envs = os.environ.copy()
    envs['OMNIGIBSON_HEADLESS'] = '1'

    with open(log_file, 'w') as outfile:
        if stream_logs:
            process = subprocess.Popen(
                launcher,
                env=envs,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in process.stdout:
                outfile.write(line)
                outfile.flush()
                print(line, end='', flush=True)
            return_code = process.wait()
        else:
            result = subprocess.run(
                launcher,
                env=envs,
                stdout=outfile,
                stderr=outfile,
                text=True,
                check=False,
            )
            return_code = result.returncode

    return task_name, scene_name, return_code, retry, log_file


def benchmark_all(
    model: str, 
    work_dir: str, 
    task_list: str,
    use_default_scene_model: bool,
    data_parallel: int,
    online_object_sampling: bool,
    num_retry: int,
    draw_bbox_2d: bool,
    primitive_type: str,
    show_robot: bool,
    use_initial_setup: bool,
    use_self_caption: bool, 
    local_llm_serve: bool, 
    local_serve_ip: str,
    prompt_setting: str,
    not_eval_process_safety: bool, 
    not_eval_termination_safety: bool, 
    not_eval_awareness: bool, 
    not_eval_execution: bool,
    config: str,
):
    if data_parallel < 1:
        warnings.warn(f'data_parallel can not be set < 1, set to 1 by default.')
        data_parallel = 1

    all_tasks = get_all_tasks(task_list, use_default_scene_model)
    metric = Metric()

    task_queue = deque([(task, 0) for task in all_tasks])
    pbar = tqdm(total=len(task_queue), desc='IS-Bench')

    with ProcessPoolExecutor(max_workers=data_parallel) as executor:
        while len(task_queue) > 0:
            dispatched = []
            for i in range(len(task_queue)):
                task, retry = task_queue.popleft()
                task_name, scene_name = task
                dispatched.append(executor.submit(
                    worker, 
                    task_name, 
                    scene_name, 
                    model, 
                    work_dir, 
                    online_object_sampling, 
                    retry, 
                    draw_bbox_2d,
                    primitive_type,
                    show_robot,
                    use_initial_setup,
                    use_self_caption, 
                    local_llm_serve, 
                    local_serve_ip, 
                    prompt_setting,
                    not_eval_process_safety, 
                    not_eval_termination_safety, 
                    not_eval_awareness,
                    not_eval_execution,
                    data_parallel == 1,
                    config,
                ))

            for future in as_completed(dispatched):
                task_name, scene_name, return_code, retry, log_file = future.result()
                if return_code != 0:
                    tqdm.write(
                        f'[{get_time_tag()}][error] "{task_name}___{scene_name}" '
                        f'exited with code {return_code}. See {log_file}'
                    )
                do_retry = read_benchmark_report(task_name, scene_name, model, work_dir, metric)

                if do_retry and retry < num_retry:
                    task_queue.append(((task_name, scene_name), retry + 1))
                    continue
                
                if do_retry:
                    metric.failure_placement.append((task_name, scene_name))

                metric.num_tasks += 1
                pbar.update(1)
                latency_summary = metric.summary()['latency']
                pbar.set_postfix(dict(
                    TR=f'{metric.termination_rate:.2f} ({metric.num_success_terminations}/{metric.num_tasks})',
                    SR=f'{metric.success_rate:.2f} ({metric.num_success_completions}/{metric.num_tasks})',
                    SSR=f'{metric.safe_success_rate:.2f}',
                    SRec=f'{metric.overall_safety_recall:.2f}',
                    GraphLat=f'{latency_summary["graph_construction_latency"]:.4f}s',
                    TotalLat=f'{latency_summary["total_latency"]:.4f}s',
                ))

            wait(dispatched)

    model_tag = get_model_tag(model)
    with open(os.path.join(work_dir, 'benchmark', f'report_{model_tag}_all.json'), 'w', encoding='utf-8') as f:
        json.dump(metric.summary(), f, indent=4, ensure_ascii=False)


if __name__ == '__main__':
    args = parser.parse_args()
    args, runtime_config = _apply_config(args)
    benchmark_all(
        model=args.model, 
        work_dir=args.work_dir, 
        task_list=args.task_list, 
        use_default_scene_model=(not args.not_use_default_scene_model),
        data_parallel=args.data_parallel,
        online_object_sampling=args.online_object_sampling,
        num_retry = args.num_retry,
        draw_bbox_2d = args.draw_bbox_2d,
        primitive_type = args.primitive_type,
        show_robot = args.show_robot,
        use_initial_setup = args.use_initial_setup, 
        use_self_caption = args.use_self_caption,
        local_llm_serve = args.local_llm_serve,
        local_serve_ip = args.local_serve_ip,
        prompt_setting = args.prompt_setting,
        not_eval_process_safety = args.not_eval_process_safety, 
        not_eval_termination_safety = args.not_eval_termination_safety, 
        not_eval_awareness = args.not_eval_awareness, 
        not_eval_execution = args.not_eval_execution,
        config=args.config,
    )
