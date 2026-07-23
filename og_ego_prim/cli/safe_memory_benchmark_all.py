"""Batch launcher for paired with-memory / without-memory evaluation."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Dict, Iterable, List, Tuple

from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict
from og_ego_prim.config.runtime_config import size_to_text
from og_ego_prim.utils.task_registry import get_task_config_path

SCENE_ALIASES = {
    "Wainscott_0_int": "Wainscott_0_garden",
}


def default_tasks() -> List[str]:
    task_root = Path(__file__).resolve().parents[2] / "data" / "tasks" / "composite"
    names = sorted(path.stem for path in task_root.glob("lifelong_crossroom__*.json"))
    names.extend(sorted(path.stem for path in task_root.glob("w0g_*_v8.json")))
    return names


def load_tasks(path: str | None) -> List[str]:
    if path is None:
        return default_tasks()
    names = [
        Path(line.strip()).stem
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return list(dict.fromkeys(names))


def normalize_task_name(task: str) -> str:
    return Path(task.strip()).stem


def normalize_scene_name(scene: str | None) -> str | None:
    if scene is None:
        return None
    return SCENE_ALIASES.get(scene, scene)


def scene_for_task(task: str) -> str:
    with open(get_task_config_path(task), "r", encoding="utf-8") as file:
        return json.load(file)["scene_info"]["default_scene_model"]


def select_tasks(args: argparse.Namespace) -> List[str]:
    if args.task and args.task_list:
        raise ValueError("use either --task or --task-list, not both")

    if args.task:
        tasks = [normalize_task_name(task) for task in args.task]
    else:
        tasks = load_tasks(args.task_list)

    scene = normalize_scene_name(args.scene)
    if scene:
        tasks = [task for task in tasks if scene_for_task(task) == scene]

    if not tasks:
        selector = {
            "scene": scene,
            "task": args.task,
            "task_list": args.task_list,
        }
        raise ValueError(f"no safe-memory tasks matched selector: {selector}")
    return tasks


def planner_tag(args: argparse.Namespace) -> str:
    if args.actions_file:
        return "actions_file"
    if args.use_example_planning:
        return "example_planning"
    return args.model


def report_path(work_dir: Path, task: str, mode: str, model: str) -> Path:
    scene = scene_for_task(task)
    return (
        work_dir
        / "safe_memory_benchmark"
        / f"{task}___{scene}"
        / mode
        / model.replace("/", "__")
        / "report.json"
    )


def timestamped_work_dir(base_dir: str | Path, task: str, timestamp: str) -> Path:
    base = Path(base_dir)
    candidate = base / f"{task}_{timestamp}"
    suffix = 1
    while candidate.exists():
        candidate = base / f"{task}_{timestamp}_{suffix:02d}"
        suffix += 1
    return candidate


def task_work_dir(args: argparse.Namespace, task: str) -> Path:
    if getattr(args, "task_work_dirs", None):
        return Path(args.task_work_dirs[task])
    return Path(args.work_dir)


def format_return_code(return_code: int) -> str:
    if return_code >= 0:
        return str(return_code)
    try:
        signal_name = signal.Signals(-return_code).name
    except ValueError:
        signal_name = f"signal {-return_code}"
    return f"{return_code} {signal_name}"


def summarize_log_error(log_path: Path) -> str:
    if not log_path.exists():
        return "log file was not created"

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"could not read log: {exc}"

    if not lines:
        return "log file is empty"

    exception_re = re.compile(
        r"^(?:[A-Za-z_][\w.]*\.)?[A-Z][A-Za-z_]*(?:Error|Exception|Warning|Interrupt|Exit):"
    )
    for line in reversed(lines):
        stripped = line.strip()
        if exception_re.match(stripped):
            return stripped

    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("Fatal Python error:"):
            return stripped
        if stripped.startswith("Timed out after "):
            return stripped

    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return "log file contains only blank lines"


def run_one(args: argparse.Namespace, task: str, mode: str) -> Tuple[str, str, int, Path]:
    work_dir = task_work_dir(args, task)
    log_dir = work_dir / "safe_memory_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task}__{mode}.log"
    existing_report = report_path(work_dir, task, mode, planner_tag(args))
    if args.resume and existing_report.exists():
        return task, mode, 0, log_path

    command = [
        sys.executable,
        "-m",
        "og_ego_prim.cli.safe_memory_benchmark_once",
        "--config",
        args.config,
        "--task",
        task,
        "--memory-mode",
        mode,
        "--work-dir",
        str(work_dir),
        "--no-timestamp-work-dir",
        "--prompt-setting",
        args.prompt_setting,
        "--primitive-type",
        args.primitive_type,
        "--video-capture-interval",
        str(args.video_capture_interval),
        "--video-output-size",
        args.video_output_size,
        "--topdown-video-output-size",
        args.topdown_video_output_size,
        "--video-fps",
        str(args.video_fps),
    ]
    if args.actions_file:
        command.extend(["--actions-file", args.actions_file])
    elif args.use_example_planning:
        command.append("--use-example-planning")
    else:
        command.extend(["--model", args.model])
    if args.local_llm_serve:
        command.extend(
            [
                "--local-llm-serve",
                "--local-serve-ip",
                args.local_serve_ip,
                "--local-serve-key",
                args.local_serve_key,
            ]
        )
    if args.online_object_sampling:
        command.append("--online-object-sampling")
    if args.no_capture_observations:
        command.append("--no-capture-observations")
    if args.save_video:
        command.append("--save-video")
    else:
        command.append("--no-save-video")

    env = os.environ.copy()
    env["OMNIGIBSON_HEADLESS"] = "1"
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            process = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=args.timeout_seconds,
                check=False,
            )
            return_code = process.returncode
        except subprocess.TimeoutExpired:
            log_file.write(f"\nTimed out after {args.timeout_seconds} seconds\n")
            return_code = 124
    return task, mode, return_code, log_path


def jobs(tasks: Iterable[str], modes: Iterable[str]) -> List[Tuple[str, str]]:
    return [(task, mode) for task in tasks for mode in modes]


def _flag_present(*flags: str) -> bool:
    return any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in sys.argv[1:]
        for flag in flags
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="entrypoints/configs/eval_safe_memory.yaml")
    parser.add_argument("--model", required=True)
    parser.add_argument("--work-dir", default="results")
    parser.add_argument(
        "--timestamp-work-dir",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Create one run directory per task as WORK_DIR/TASK_YYYYMMDD_HHMMSS. "
            "Defaults to runtime.timestamp_output from the config."
        ),
    )
    parser.add_argument(
        "--scene",
        help=(
            "Optional scene filter, e.g. Beechwood_0_int, Pomaria_1_int, Rs_int, "
            "restaurant_diner, or Wainscott_0_garden."
        ),
    )
    parser.add_argument(
        "--task",
        action="append",
        help="Optional task name or JSON path. Can be passed multiple times.",
    )
    parser.add_argument("--task-list")
    parser.add_argument("--memory-modes", nargs="+", choices=("with_memory", "without_memory"), default=("with_memory", "without_memory"))
    parser.add_argument("--data-parallel", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--local-llm-serve", action="store_true")
    parser.add_argument("--local-serve-ip", default="")
    parser.add_argument("--local-serve-key", default="EMPTY")
    parser.add_argument("--prompt-setting", choices=("v0", "v1", "v2", "v3"), default="v1")
    parser.add_argument("--primitive-type", choices=("auto", "ego", "starter", "symbolic"), default="auto")
    parser.add_argument("--actions-file", help="Optional scripted per-subtask actions JSON used for every selected task")
    parser.add_argument("--use-example-planning", action="store_true", help="Run per-subtask example_planning instead of a model")
    parser.add_argument("--online-object-sampling", action="store_true")
    parser.add_argument("--no-capture-observations", action="store_true")
    parser.add_argument(
        "--save-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save first-person video.mp4 for each run. Enabled by default.",
    )
    parser.add_argument(
        "--video-capture-interval",
        type=int,
        default=30,
        help="Capture one first-person video frame every N low-level simulator steps.",
    )
    parser.add_argument(
        "--video-output-size",
        default="512x512",
        help="Resize saved video frames to WIDTHxHEIGHT, or raw/none/0 for sensor resolution.",
    )
    parser.add_argument(
        "--topdown-video-output-size",
        default=None,
        help="Resize saved topdown.mp4 frames to WIDTHxHEIGHT.",
    )
    parser.add_argument("--video-fps", type=float, default=30.0, help="Frames per second for saved video.mp4.")
    args = parser.parse_args()
    config_dict = load_runtime_config_dict(args.config)
    runtime_config = RuntimeConfig.from_mapping(config_dict)
    safe_memory_config = config_dict.get("safe_memory", {}) or {}
    if not _flag_present("--work-dir"):
        args.work_dir = runtime_config.runtime.output_root
    if not _flag_present("--timestamp-work-dir", "--no-timestamp-work-dir"):
        args.timestamp_work_dir = runtime_config.runtime.timestamp_output
    if not _flag_present("--prompt-setting"):
        args.prompt_setting = runtime_config.task.prompt_setting
    if not _flag_present("--primitive-type"):
        args.primitive_type = runtime_config.task.primitive_type
    if not _flag_present("--video-capture-interval"):
        args.video_capture_interval = runtime_config.artifacts.video_capture_interval
    if not _flag_present("--video-output-size"):
        args.video_output_size = size_to_text(runtime_config.artifacts.output_size) or "raw"
    if not _flag_present("--topdown-video-output-size"):
        args.topdown_video_output_size = (
            size_to_text(runtime_config.artifacts.topdown_output_size) or "1920x1080"
        )
    if not _flag_present("--video-fps"):
        args.video_fps = runtime_config.artifacts.video_fps
    if not _flag_present("--save-video", "--no-save-video"):
        args.save_video = runtime_config.artifacts.save_video
    if not _flag_present("--timeout-seconds") and safe_memory_config.get("timeout_seconds") is not None:
        args.timeout_seconds = float(safe_memory_config["timeout_seconds"])
    if not _flag_present("--resume") and safe_memory_config.get("resume"):
        args.resume = True
    if not _flag_present("--actions-file") and safe_memory_config.get("actions_file"):
        args.actions_file = safe_memory_config["actions_file"]
    if not _flag_present("--use-example-planning") and safe_memory_config.get("use_example_planning"):
        args.use_example_planning = True
    if not _flag_present("--memory-modes") and safe_memory_config.get("memory_modes"):
        args.memory_modes = tuple(safe_memory_config["memory_modes"])
    if args.data_parallel < 1:
        parser.error("--data-parallel must be at least 1")
    if args.video_capture_interval < 1:
        parser.error("--video-capture-interval must be at least 1")
    if args.video_fps <= 0:
        parser.error("--video-fps must be greater than 0")
    if args.actions_file and args.use_example_planning:
        parser.error("use either --actions-file or --use-example-planning, not both")

    try:
        selected_tasks = select_tasks(args)
    except ValueError as exc:
        parser.error(str(exc))

    args.task_work_dirs = None
    if args.timestamp_work_dir:
        run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.task_work_dirs = {
            task: str(timestamped_work_dir(args.work_dir, task, run_timestamp))
            for task in selected_tasks
        }

    print(
        json.dumps(
            {
                "selected_task_count": len(selected_tasks),
                "scene": args.scene,
                "tasks": selected_tasks,
                "memory_modes": list(args.memory_modes),
                "planner_source": planner_tag(args) if args.actions_file or args.use_example_planning else "model",
                "timestamp_work_dir": args.timestamp_work_dir,
                "work_dirs": args.task_work_dirs or {"shared": args.work_dir},
                "save_video": args.save_video,
                "video_capture_interval": args.video_capture_interval,
                "video_output_size": args.video_output_size,
                "video_fps": args.video_fps,
            },
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

    work = jobs(selected_tasks, args.memory_modes)
    failures: List[Dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.data_parallel) as executor:
        futures = {
            executor.submit(run_one, args, task, mode): (task, mode)
            for task, mode in work
        }
        for completed, future in enumerate(as_completed(futures), 1):
            task, mode, return_code, log_path = future.result()
            status = "pass" if return_code == 0 else f"FAIL({format_return_code(return_code)})"
            print(f"[{completed:03d}/{len(work):03d}] {status} {task} {mode}", flush=True)
            if return_code != 0:
                error_summary = summarize_log_error(log_path)
                print(f"          log={log_path}", flush=True)
                print(f"          error={error_summary}", flush=True)
                failures.append(
                    {
                        "task": task,
                        "memory_mode": mode,
                        "return_code": return_code,
                        "return_code_label": format_return_code(return_code),
                        "log": str(log_path),
                        "error": error_summary,
                    }
                )

    if failures:
        print(json.dumps({"failures": failures}, indent=2), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
