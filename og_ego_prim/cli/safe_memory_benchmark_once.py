"""Run one no-reset safe-memory lifelong episode."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

import numpy as np
from PIL import Image

from og_ego_prim.benchmark.lifelong_evaluator import (
    LifelongEvaluator,
    get_subtask_instruction,
)
from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict
from og_ego_prim.primitives.specs import expand_legacy_plan_for_starter
from og_ego_prim.utils.cli_parsing import parse_optional_size
from og_ego_prim.utils.task_registry import get_task_config_path
from og_ego_prim.utils.topdown_capture import save_topdown_assets


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAFE_MEMORY_CONFIG = REPOSITORY_ROOT / "entrypoints" / "configs" / "eval_safe_memory.yaml"

# These are the canonical, non-experiment safe-memory profiles for the current
# task files. Experimental variants (manual, cooling-timer, and A* trials) stay
# explicit so an ordinary task run cannot select a diagnostic profile by name.
TASK_SAFE_MEMORY_CONFIGS = {
    "lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v3": (
        "eval_safe_memory_cleaner_food.yaml"
    ),
    "lifelong_crossroom__beechwood__dryer_wet_lint_interlock_v3": (
        "eval_safe_memory_dryer_wet_lint.yaml"
    ),
    "lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3": (
        "eval_safe_memory_hot_water.yaml"
    ),
    "lifelong_crossroom__beechwood__jar_seal_status_after_canning_v3": (
        "eval_safe_memory_jar_seal.yaml"
    ),
    "lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3": (
        "eval_safe_memory_knife_hidden_hamper.yaml"
    ),
    "lifelong_crossroom__beechwood__mold_rag_dining_reuse_v3": (
        "eval_safe_memory_mold_rag.yaml"
    ),
    "lifelong_crossroom__beechwood__office_strip_wet_lamp_v3": (
        "eval_safe_memory_office_strip.yaml"
    ),
    "lifelong_crossroom__beechwood__oil_rag_closed_storage_v3": (
        "eval_safe_memory_oil_rag.yaml"
    ),
    "lifelong_crossroom__beechwood__quiche_wrap_identity_v3": (
        "eval_safe_memory_quiche_wrap.yaml"
    ),
    "lifelong_crossroom__beechwood__raw_board_ready_plate_v3": (
        "eval_safe_memory_raw_board_ready_plate.yaml"
    ),
    "lifelong_crossroom__beechwood__thaw_clock_remote_work_v3": (
        "eval_safe_memory_thaw_clock.yaml"
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate one lifelong cross-room task without resetting the environment."
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Runtime YAML. When omitted, select the canonical task-specific "
            "safe-memory profile, falling back to eval_safe_memory.yaml."
        ),
    )
    parser.add_argument("--task")
    parser.add_argument("--scene")
    parser.add_argument("--model")
    parser.add_argument(
        "--enable-scene-graph",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable scene graph perception backend. Disable to skip SAMJAM-UniGoal.",
    )
    parser.add_argument(
        "--enable-risk-predictor",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable runtime risk predictor for action review. Disable to bypass risk checks.",
    )
    parser.add_argument("--work-dir", default="results")

    # 给你的任务运行一个简短的标签，包含在时间戳运行目录和 README 中。
    parser.add_argument(
        "--run-purpose",
        default="benchmark",
        help="Short label included in the timestamped run directory and README.",
    )
    parser.add_argument(
        "--timestamp-work-dir",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Create a run directory named WORK_DIR/TASK_YYYYMMDD_HHMMSS. "
            "Defaults to runtime.timestamp_output from the config."
        ),
    )
    parser.add_argument(
        "--use-example-planning",
        action="store_true",
        help="Run per-subtask example_planning from the task JSON instead of calling a model.",
    )
    parser.add_argument("--local-llm-serve", action="store_true")
    parser.add_argument("--local-serve-ip", default="")
    parser.add_argument("--local-serve-key", default="EMPTY")
    parser.add_argument("--prompt-setting", choices=("v0", "v1", "v2", "v3"), default="v1")
    parser.add_argument("--primitive-type", choices=("auto", "ego", "starter", "symbolic"), default="auto")
    parser.add_argument("--scene-graph-step-interval", type=int, default=30)
    parser.add_argument(
        "--online-object-sampling",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    # 初始化布置开关。启用后，会在任务开始前使用任务 JSON 中预定义的 scene_file_remove_objects 和 object_initial_poses 对场景进行预布置（移除指定物体、覆盖物体初始位姿），而不是使用随机采样或默认场景布局。
    parser.add_argument("--use-initial-setup", action="store_true")
    # 自描述（caption）开关。启用后，在任务执行前会让 VLM 模型对当前场景生成一段自然语言描述（caption），并记录到 tracker 中。这段描述会作为额外上下文信息提供给后续的规划推理，帮助模型更好地理解当前环境状态。
    parser.add_argument("--use-self-caption", action="store_true")
    parser.add_argument("--show-robot", action="store_true")
    parser.add_argument("--draw-bbox-2d", action="store_true")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Launch OmniGibson headlessly. Enabled by default for benchmark runs.",
    )
    parser.add_argument("--no-capture-observations", action="store_true")
    parser.add_argument(
        "--save-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save a first-person video.mp4 from robot RGB observations. Enabled by default.",
    )
    parser.add_argument(
        "--save-topdown-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save a topdown.mp4 robot trace beside video.mp4. Enabled by default.",
    )
    parser.add_argument(
        "--topdown-video-output-size",
        type=parse_optional_size,
        default=None,
        help="Resize topdown.mp4 frames to WIDTHxHEIGHT.",
    )
    parser.add_argument(
        "--topdown-video-fps",
        type=float,
        default=12.0,
        help="Frames per second for saved topdown.mp4.",
    )
    parser.add_argument(
        "--video-capture-interval",
        type=int,
        default=30,
        help="Capture one video frame every N low-level simulator steps.",
    )
    parser.add_argument(
        "--video-output-size",
        type=parse_optional_size,
        default=parse_optional_size("512x512"),
        help="Resize video frames to WIDTHxHEIGHT. Use raw/none/0 to keep sensor resolution.",
    )
    parser.add_argument(
        "--sensor-image-size",
        type=parse_optional_size,
        default=None,
        help="Set robot RGB sensor resolution to WIDTHxHEIGHT. Use raw/none/0 to keep task defaults.",
    )
    parser.add_argument(
        "--video-fps",
        type=float,
        default=30.0,
        help="Frames per second for saved video.mp4.",
    )
    parser.add_argument("--debug", action="store_true")
    return parser


def _flag_present(*flags: str) -> bool:
    return any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in sys.argv[1:]
        for flag in flags
    )


def resolve_safe_memory_config(
    task_spec: Optional[str],
    config_spec: Optional[str],
) -> tuple[Path, dict[str, Any]]:
    """Resolve explicit or canonical task-local runtime settings.

    The task JSON remains the source of scene initialization settings. This
    resolver only selects the runtime profile layered on top of those settings.
    """

    if config_spec:
        path = Path(config_spec).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"safe-memory config not found: {path}")
        return path, {"source": "explicit", "task_config": None}

    task_path = None
    if task_spec:
        task_path = get_task_config_path(task_spec)
        task_stem = task_path.stem
        filename = TASK_SAFE_MEMORY_CONFIGS.get(task_stem)
        if filename:
            path = REPOSITORY_ROOT / "entrypoints" / "configs" / filename
            if path.is_file():
                return path.resolve(), {
                    "source": "task_specific",
                    "task_config": str(task_path),
                }

    return DEFAULT_SAFE_MEMORY_CONFIG.resolve(), {
        "source": "generic_fallback",
        "task_config": None if task_path is None else str(task_path),
    }


def _apply_config(args: argparse.Namespace) -> tuple[argparse.Namespace, RuntimeConfig]:
    config_path, config_resolution = resolve_safe_memory_config(args.task, args.config)
    args.config = str(config_path)
    args.config_resolution = config_resolution
    config_dict = load_runtime_config_dict(config_path)
    runtime_config = RuntimeConfig.from_mapping(config_dict)
    task_config = runtime_config.task
    if not _flag_present("--task") and args.task is None:
        args.task = task_config.name
    if not _flag_present("--scene") and args.scene is None:
        args.scene = task_config.scene
    if not _flag_present("--model") and args.model is None:
        args.model = task_config.model
    if not _flag_present("--work-dir"):
        args.work_dir = runtime_config.runtime.output_root
    if not _flag_present("--timestamp-work-dir", "--no-timestamp-work-dir"):
        args.timestamp_work_dir = runtime_config.runtime.timestamp_output
    if not _flag_present("--prompt-setting"):
        args.prompt_setting = task_config.prompt_setting
    if not _flag_present("--primitive-type"):
        args.primitive_type = task_config.primitive_type
    if not _flag_present("--scene-graph-step-interval"):
        args.scene_graph_step_interval = runtime_config.scene_graph.step_interval
    if (
        not _flag_present("--online-object-sampling", "--no-online-object-sampling")
        and task_config.online_object_sampling is not None
    ):
        args.online_object_sampling = bool(task_config.online_object_sampling)
    if not _flag_present("--use-initial-setup") and task_config.use_initial_setup:
        args.use_initial_setup = True
    if not _flag_present("--use-self-caption") and task_config.use_self_caption:
        args.use_self_caption = True
    if not _flag_present("--show-robot") and runtime_config.runtime.show_robot:
        args.show_robot = True
    if not _flag_present("--save-video", "--no-save-video"):
        args.save_video = runtime_config.artifacts.save_video
    if not _flag_present("--video-capture-interval"):
        args.video_capture_interval = runtime_config.artifacts.video_capture_interval
    if not _flag_present("--video-output-size"):
        args.video_output_size = runtime_config.artifacts.output_size
    if not _flag_present("--topdown-video-output-size"):
        args.topdown_video_output_size = runtime_config.artifacts.topdown_output_size
    if not _flag_present("--sensor-image-size"):
        args.sensor_image_size = runtime_config.artifacts.sensor_image_size
    runtime_config.artifacts.sensor_image_size = args.sensor_image_size
    if not _flag_present("--video-fps"):
        args.video_fps = runtime_config.artifacts.video_fps
    if not _flag_present("--headless", "--no-headless"):
        args.headless = runtime_config.runtime.headless
    args.runtime_config = runtime_config
    return args, runtime_config


class TeeTextStream:
    def __init__(self, terminal_stream: Any, log_stream: Any) -> None:
        self.terminal_stream = terminal_stream
        self.log_stream = log_stream

    def write(self, value: str) -> int:
        self.terminal_stream.write(value)
        self.log_stream.write(value)
        self.flush()
        return len(value)

    def flush(self) -> None:
        self.terminal_stream.flush()
        self.log_stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.terminal_stream, name)


def sanitize_run_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return label or "benchmark"


def timestamped_work_dir(
    base_dir: str | Path,
    task: str,
    scene: str,
    purpose: str,
) -> Path:
    base = Path(base_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_scene_dir = base / f"{task}___{scene}"
    run_name = f"{timestamp}_{sanitize_run_label(purpose)}"
    candidate = task_scene_dir / run_name
    suffix = 1
    while candidate.exists():
        candidate = task_scene_dir / f"{run_name}_{suffix:02d}"
        suffix += 1
    return candidate


def install_run_log(work_dir: Path) -> Path:
    log_path = work_dir / "console.log"
    log_stream = log_path.open("a", encoding="utf-8", buffering=1)
    if os.environ.get("ISBENCH_LOG_FILE_ONLY") == "1":
        # The shell entrypoint redirects native and child-process output here too.
        sys.stdout = log_stream
        sys.stderr = log_stream
    else:
        sys.stdout = TeeTextStream(sys.stdout, log_stream)
        sys.stderr = TeeTextStream(sys.stderr, log_stream)
    return log_path


def _ensure_safe_topdown_assets(
    benchmark: Any,
    output_dir: Path,
    *,
    runtime_config: RuntimeConfig,
    output_size: Optional[tuple[int, int]] = None,
) -> dict[str, Any]:
    cached = getattr(benchmark, "_safe_topdown_assets", None)
    if isinstance(cached, dict):
        occupancy_metadata = cached.get("occupancy_metadata")
        if occupancy_metadata and Path(occupancy_metadata).exists():
            return cached

    tracker = getattr(benchmark, "tracker", None)
    snapshot = getattr(tracker, "latest_scene_graph", None)
    if not isinstance(snapshot, dict):
        snapshot = None
    assets = save_topdown_assets(
        benchmark.env,
        output_dir / "topdown_assets",
        world_bounds=getattr(runtime_config.artifacts, "topdown_world_bounds", None),
        snapshot=snapshot,
        execution_diagnostics=list(
            getattr(tracker, "execution_diagnostics", None) or []
        ),
        output_size=(
            output_size
            or runtime_config.artifacts.topdown_output_size
            or (1920, 1080)
        ),
    )
    benchmark._safe_topdown_assets = assets
    return assets


def load_task_config(task_name: str) -> Dict[str, Any]:
    with open(get_task_config_path(task_name), "r", encoding="utf-8") as file:
        return json.load(file)


def load_example_planning(config: Dict[str, Any]) -> List[List[Any]]:
    subtasks = config.get("subtasks", [])
    scripted = []
    for index, subtask in enumerate(subtasks, 1):
        plans = subtask.get("example_planning")
        if not isinstance(plans, list) or not plans:
            raise ValueError(
                f"subtask {index} is missing non-empty example_planning; "
                "add per-subtask example_planning"
            )
        scripted.append(plans)
    return scripted


def as_plan(action: Any) -> Dict[str, Any]:
    if isinstance(action, str):
        action_text = action.strip()
        if action_text.upper() == "DONE":
            action_text = "done()"
        return {"action": action_text, "caution": None}
    if isinstance(action, dict) and action.get("action"):
        action_text = str(action["action"]).strip()
        if action_text.upper() == "DONE":
            action_text = "done()"
        return {"action": action_text, "caution": action.get("caution")}
    raise ValueError(f"invalid scripted action: {action!r}")


def expand_scripted_actions_for_starter(scripted: List[List[Any]]) -> List[List[Dict[str, Any]]]:
    return [
        [
            expanded
            for raw_action in actions
            for expanded in expand_legacy_plan_for_starter(as_plan(raw_action))
        ]
        for actions in scripted
    ]


def capture_observation(
    benchmark: Any,
    observation_adapter: Any,
    output_dir: Path,
    step_tag: str,
    *,
    track_video: bool = True,
    video_output_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    target = output_dir / step_tag
    target.mkdir(parents=True, exist_ok=True)
    frame = observation_adapter.observe(benchmark.env)
    Image.fromarray(frame.rgb).save(target / "obs_0.png")
    if track_video:
        benchmark.tracker.track_video_rgb(resize_rgb(frame.rgb, video_output_size))
    return {
        "step_tag": step_tag,
        "sensor_name": frame.sensor_name,
        "rgb_shape": list(frame.rgb.shape),
        "frame_index": frame.frame_index,
    }


def _to_numpy_image(frame: Any) -> np.ndarray:
    if hasattr(frame, "detach"):
        frame = frame.detach().cpu().numpy()
    frame = np.asarray(frame)

    if frame.dtype != np.uint8:
        if frame.size and frame.max() <= 1.0:
            frame = frame * 255.0
        frame = np.clip(frame, 0, 255).astype(np.uint8)

    if frame.ndim == 3 and frame.shape[-1] > 3:
        frame = frame[:, :, :3]
    return frame


def resize_rgb(rgb: np.ndarray, output_size: Optional[Tuple[int, int]]) -> np.ndarray:
    if output_size is None:
        return rgb

    width, height = output_size
    if rgb.shape[1] == width and rgb.shape[0] == height:
        return rgb
    return np.asarray(Image.fromarray(rgb).resize((width, height), Image.Resampling.LANCZOS))


def capture_robot_rgb_frame(
    robot: Any,
    output_size: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    obs, _ = robot.get_obs()
    for _sensor_name, sensor_obs in obs.items():
        if isinstance(sensor_obs, dict) and "rgb" in sensor_obs:
            return resize_rgb(_to_numpy_image(sensor_obs["rgb"]), output_size)
    raise RuntimeError(f"No robot RGB observation found. Available keys: {list(obs.keys())}")


def install_video_step_callback(benchmark: Any, args: argparse.Namespace) -> None:
    if not args.save_video or args.no_capture_observations:
        return

    interval = max(args.video_capture_interval, 1)
    robot = benchmark.env.robots[0]
    original_step_callback = benchmark.executor.step_callback

    def wrapped_step_callback(context: Any) -> None:
        if original_step_callback is not None:
            original_step_callback(context)
        if context.step_index % interval != 0:
            return
        try:
            benchmark.tracker.track_video_rgb(capture_robot_rgb_frame(robot, args.video_output_size))
        except Exception as exc:
            benchmark.tracker.track_error(
                action="capture_video_frame",
                err_type=exc.__class__.__name__,
                msg=str(exc),
            )

    benchmark.executor.step_callback = wrapped_step_callback

def plan_report_slice(plans: List[Dict[str, Any]], start: int, end: int) -> List[Dict[str, Any]]:
    return [
        {
            "step": plan["step"],
            "action": plan["plan"]["action"],
            "caution": plan["plan"].get("caution"),
        }
        for plan in plans[start:end]
        if plan.get("executed") is True
    ]


def _run(
    args: argparse.Namespace,
    *,
    benchmark_holder: list,
) -> Path:
    if args.model is None and not args.use_example_planning:
        raise ValueError("provide --model or --use-example-planning")
    if args.video_capture_interval < 1:
        raise ValueError("--video-capture-interval must be at least 1")
    if args.video_fps <= 0:
        raise ValueError("--video-fps must be greater than 0")

    runtime_config = args.runtime_config

    # Apply pre-build ablation settings based on CLI flags.
    if not args.enable_scene_graph:
        args.scene_graph_step_interval = 0
        runtime_config.scene_graph.backend = "disabled"
    if not args.enable_risk_predictor:
        args.prompt_setting = "v1"

    config = load_task_config(args.task)
    canonical_task_name = str(config.get("task_info", {}).get("task_name") or args.task)
    scene = args.scene or config["scene_info"]["default_scene_model"]
        
    if args.online_object_sampling is None:
        args.online_object_sampling = bool(
            config.get("scene_info", {}).get("online_object_sampling", False)
        )

    if not args.online_object_sampling:
        repository_root = Path(__file__).resolve().parents[2]
        sampled_scene = (
            repository_root
            / "data"
            / "scenes"
            / scene
            / "json"
            / f"{scene}_task_{canonical_task_name}_0_0_template.json"
        )
        if not sampled_scene.exists():
            raise FileNotFoundError(
                f"sampled scene not found: {sampled_scene}; generate scenes or pass "
                "--online-object-sampling"
            )
        
    if args.use_example_planning:
        planner_source = "example_planning"
    else:
        planner_source = "model"

    model_tag = (args.model if planner_source == "model" else planner_source).replace("/", "__")
    work_dir = (
        timestamped_work_dir(args.work_dir, args.task, scene, args.run_purpose)
        if args.timestamp_work_dir
        else Path(args.work_dir)
    )
    output_dir = (
        work_dir
        / "safe_memory_benchmark"
        / f"{args.task}___{scene}"
        / model_tag
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "benchmark").mkdir(parents=True, exist_ok=True)
    if args.enable_scene_graph:
        runtime_config.scene_graph.output_dir = str(output_dir / "samjam_outputs")
        runtime_config.scene_graph.debug_log_path = str(
            output_dir / "samjam_outputs" / "scene_graph_debug.log"
        )

    print(
        "safe-memory runtime config: "
        f"{Path(args.config).resolve()} "
        f"source={args.config_resolution.get('source')}",
        flush=True,
    )
    print(
        "safe-memory scene settings: "
        f"removed={len(config.get('scene_info', {}).get('scene_file_remove_objects', []))} "
        f"initial_pose_overrides={len(config.get('scene_info', {}).get('object_initial_poses', {}))} "
        f"online_object_sampling={args.online_object_sampling}",
        flush=True,
    )
    log_path = install_run_log(work_dir)
    print(f"run directory: {work_dir.resolve()}", flush=True)
    print(f"console log: {log_path.resolve()}", flush=True)

    if args.use_example_planning:
        scripted = load_example_planning(config)
    else:
        scripted = None

    from omnigibson.macros import gm

    gm.USE_GPU_DYNAMICS = True

    from og_ego_prim.benchmark import build_benchmark

    benchmark = build_benchmark(
        task=args.task,
        scene=scene,
        ego_view=not args.show_robot,
        draw_bbox_2d=args.draw_bbox_2d,
        primitive_type=args.primitive_type,
        scene_graph_step_interval=args.scene_graph_step_interval,
        scene_graph_backend=runtime_config.scene_graph.backend,
        enable_risk_predictor=args.enable_risk_predictor,
        use_initial_setup=args.use_initial_setup,
        use_self_caption=args.use_self_caption,
        online_object_sampling=args.online_object_sampling,
        debug=args.debug,
        eval_process_safety=False,
        eval_termination_safety=False,
        eval_awareness=False,
        eval_execution=False,
        runtime_config=args.runtime_config,
    )
    benchmark_holder.append(benchmark)
    if scripted is not None and benchmark.primitive_type == "starter":
        scripted = expand_scripted_actions_for_starter(scripted)
    from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter

    observation_adapter = ISBenchObservationAdapter()
    observation_adapter.reset()
    evaluator = LifelongEvaluator(
        benchmark.env,
        benchmark.eval_task_config,
        eval_awareness=scripted is None and args.prompt_setting == "v2",
    )
    benchmark._lifelong_evaluator = evaluator
    benchmark.tracker.runtime_modules["evaluator"] = type(evaluator).__name__

    install_video_step_callback(benchmark, args)
    agent = None
    if scripted is None:
        from og_ego_prim.risk_predictor.utils import install_vlm_risk_provider
        from og_ego_prim.task_planner import AgentPlanner

        agent = AgentPlanner(
            task_name=args.task,
            scene_name=scene,
            model_name=args.model,
            work_dir=str(work_dir),
            local_llm_serve=args.local_llm_serve,
            local_serve_ip=args.local_serve_ip,
            local_serve_key=args.local_serve_key,
            prompt_setting=args.prompt_setting,
            primitive_type=benchmark.primitive_type,
            use_initial_setup=args.use_initial_setup,
            use_self_caption=args.use_self_caption,
            debug=args.debug,
            observation_dir=str(output_dir),
        )
        if args.enable_risk_predictor:
            install_vlm_risk_provider(benchmark, agent.client)
        agent.set_tracker(benchmark.tracker)
        agent.set_runtime_controller(benchmark.runtime_controller)
        benchmark.tracker.runtime_modules["planner"] = type(agent).__name__
        # Evaluator predicates are not planner inputs. Runtime safety decisions
        # arrive through the controller's RiskPredictor review instead.
    else:
        benchmark.tracker.model = "scripted"
        benchmark.tracker.runtime_modules["planner"] = "ScriptedPlanner"

    started_at = time.time()
    subtask_reports = []
    observation_records = []
    if not args.no_capture_observations:
        observation_records.append(
            capture_observation(
                benchmark,
                observation_adapter,
                output_dir,
                "0_init",
                track_video=args.save_video,
                video_output_size=args.video_output_size,
            )
        )

    for index, subtask in enumerate(config["subtasks"], 1):
        benchmark.tracker.termination = None
        instruction = get_subtask_instruction(subtask)
        h_limit = int(subtask.get("H_limit", config["lifelong_config"]["H_per_task"]))
        action_start = len(benchmark.tracker.plans)

        if agent is not None:
            agent.begin_lifelong_subtask(
                task_instruction=instruction,
                subtask_index=index,
            )
            use_obs = not args.no_capture_observations
            if args.use_self_caption:
                benchmark.tracker.track_caption(content=agent.generate_caption(use_obs=use_obs))
            if args.prompt_setting == "v2":
                awareness = agent.generate_awareness(use_obs=use_obs)
                awareness_result = evaluator.evaluate_awareness(
                    instruction,
                    benchmark.initial_setup,
                    awareness,
                    subtask_index=index,
                )
                benchmark.tracker.track_awareness(**awareness_result)
            from og_ego_prim.task_planner import create_planner_adapter

            planner_adapter = create_planner_adapter(
                "vlm_closed_loop",
                agent,
                use_obs=use_obs,
                max_step=h_limit,
                held_object_getter=benchmark._current_grasped_object_id,
                enable_loading_preflight=(
                    runtime_config.task.enable_loading_preflight
                ),
            )
            benchmark.bind_planner_adapter(
                planner_adapter,
                source=type(agent).__name__,
                emit_proposals=False,
            )
        else:
            benchmark.set_active_subtask(index)
            from og_ego_prim.task_planner import create_planner_adapter

            planner_adapter = create_planner_adapter(
                "scripted",
                tuple(
                    as_plan(raw_action)
                    for raw_action in scripted[index - 1][:h_limit]
                ),
            )
            benchmark.bind_planner_adapter(
                planner_adapter,
                source="ScriptedPlanner",
                emit_proposals=True,
            )

        from og_ego_prim.utils.metric import track_planning_latency
        plans = track_planning_latency(
            benchmark.runtime_controller.iter_actions(),
            benchmark.tracker,
        )

        execution_failed = False
        blocked_reason = None
        for plan in plans:
            execution_succeeded = benchmark.execute_plan(plan)
            retry_after_execution_failure = False
            if not execution_succeeded:
                review = benchmark.runtime_controller.last_review
                outcome = benchmark.runtime_controller.last_outcome
                if (
                    agent is not None
                    and outcome is not None
                    and not outcome.executed
                    and review is not None
                    and review.should_rethink
                ):
                    continue
                if (
                    agent is not None
                    and outcome is not None
                    and outcome.executed
                    and not outcome.succeeded
                ):
                    retry_after_execution_failure = True
                else:
                    if outcome is not None and not outcome.executed:
                        blocked_reason = outcome.reason or "blocked_by_scheduler"
                        benchmark.tracker.track_termination(reason=blocked_reason)
                    execution_failed = True
                    break
            step = benchmark.tracker.plans[-1]["step"]
            if not args.no_capture_observations:
                action_text = plan.to_legacy_plan()
                action_tag = action_text.replace("(", "__").replace(")", "__")
                observation_records.append(
                    capture_observation(
                        benchmark,
                        observation_adapter,
                        output_dir,
                        f"{step}_{action_tag}",
                        track_video=args.save_video,
                        video_output_size=args.video_output_size,
                    )
                )
            if retry_after_execution_failure:
                continue

        action_end = len(benchmark.tracker.plans)
        executed_action_count = sum(
            plan.get("executed") is True
            for plan in benchmark.tracker.plans[action_start:action_end]
        )
        if blocked_reason is not None:
            termination_reason = blocked_reason
        elif execution_failed:
            termination_reason = "execution_error"
        elif benchmark.tracker.termination is not None:
            termination_reason = benchmark.tracker.termination["reason"]
        elif action_end > action_start and benchmark.tracker.plans[-1]["plan"]["action"].lower().startswith("done"):
            termination_reason = "done"
        elif executed_action_count >= h_limit:
            termination_reason = "exceeding_max_steps"
        else:
            termination_reason = "planner_stopped"

        result = evaluator.finish_subtask(
            subtask_index=index,
            termination_reason=termination_reason,
            instruction=instruction,
            h_limit=h_limit,
        )
        result_dict = result.to_dict()
        result_dict["actions"] = plan_report_slice(benchmark.tracker.plans, action_start, action_end)
        subtask_reports.append(result_dict)

    benchmark.tracker.finalize_latency()
    report = {
        "schema_version": 1,
        "benchmark": "safe_memory_lifelong",
        "task": args.task,
        "scene": scene,
        "runtime_config_path": str(Path(args.config).resolve()),
        "runtime_config_resolution": args.config_resolution,
        "model": args.model if planner_source == "model" else planner_source,
        "planner_source": planner_source,
        "primitive_type": benchmark.primitive_type,
        "runtime_ablation": {
            "scene_graph_enabled": args.enable_scene_graph,
            "risk_predictor_enabled": args.enable_risk_predictor,
            "use_initial_setup": args.use_initial_setup,
            "use_self_caption": args.use_self_caption,
            "prompt_setting": args.prompt_setting,
        },
        "environment_reset_between_subtasks": False,
        "observation_model": "single_view_egocentric_rgb",
        "observations": observation_records,
        "lifelong_config": config["lifelong_config"],
        "subtask_results": subtask_reports,
        "metrics": evaluator.summary(),
        "runtime_modules": benchmark.tracker.runtime_modules,
        "planner_episode": [
            entry.to_dict()
            for entry in benchmark.runtime_controller.planner_episode.snapshot()
        ],
        "latency": benchmark.tracker.latency_report(),
        "elapsed_wall_seconds": time.time() - started_at,
        "error_stack": benchmark.tracker.error_stack,
        "execution_diagnostics": benchmark.tracker.execution_diagnostics,
    }
    controller = getattr(getattr(benchmark, "executor", None), "controller", None)
    navigation_backend = getattr(controller, "navigation_backend", None)
    save_navigation_debug = getattr(
        navigation_backend,
        "save_debug_artifacts",
        None,
    )
    if save_navigation_debug is not None:
        try:
            report["navigation_debug_artifacts"] = save_navigation_debug(
                controller,
                output_dir,
            )
        except Exception as exc:
            report["navigation_debug_artifacts_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
    if args.save_video:
        try:
            video_info = benchmark.tracker.save_video(str(output_dir))
            if video_info is not None:
                report["video"] = video_info
                if isinstance(video_info, dict) and video_info.get("path"):
                    report["video"]["abs_path"] = str(output_dir / video_info["path"])
        except Exception as exc:
            report["video_error"] = {"type": exc.__class__.__name__, "message": str(exc)}
    if args.save_topdown_video:
        try:
            from og_ego_prim.utils.topdown_trace_video import save_topdown_trace_video

            topdown_output_size = (
                args.topdown_video_output_size
                or runtime_config.artifacts.topdown_output_size
            )
            topdown_assets = _ensure_safe_topdown_assets(
                benchmark,
                output_dir,
                runtime_config=runtime_config,
                output_size=topdown_output_size,
            )
            report["topdown_assets"] = topdown_assets
            topdown_info = save_topdown_trace_video(
                scene=scene,
                execution_diagnostics=benchmark.tracker.execution_diagnostics,
                output_dir=output_dir,
                topdown_assets_dir=topdown_assets["asset_dir"],
                output_name="topdown.mp4",
                output_size=topdown_output_size,
                fps=args.topdown_video_fps,
            )
            if topdown_info is not None:
                report["topdown_video"] = topdown_info
        except Exception as exc:
            report["topdown_video_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }

    report_path = output_dir / "report.json"
    with report_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
    print(json.dumps(report["metrics"], indent=2), flush=True)
    print(f"safe-memory report: {report_path}", flush=True)
    return report_path


def run(args: argparse.Namespace) -> Path:
    benchmarks = []
    try:
        return _run(args, benchmark_holder=benchmarks)
    finally:
        for benchmark in reversed(benchmarks):
            try:
                benchmark.close()
            except Exception as exc:
                print(
                    "[safe-memory][close] "
                    f"{exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


def main() -> None:
    args = build_parser().parse_args()
    args, _runtime_config = _apply_config(args)

    if args.headless:
        os.environ["OMNIGIBSON_HEADLESS"] = "1"

    maybe_reexec_with_omnigibson_python()
    # Isaac Sim should not receive this benchmark CLI's arguments.
    sys.argv = [sys.argv[0]]
    try:
        from og_ego_prim.utils.monkey_patch import add_monkey_patch

        add_monkey_patch()
        run(args)
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        if "omnigibson" in sys.modules:
            # Avoid Isaac/Kit teardown masking the Python exception with a
            # segmentation fault. The traceback above is the actionable error.
            os._exit(1)
        raise SystemExit(1)
    if "omnigibson" in sys.modules:
        # Isaac/Kit teardown can crash after a successful headless run when
        # viewport sensors are destroyed. Match the existing sample-only path:
        # once the report is flushed, terminate the process directly.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
