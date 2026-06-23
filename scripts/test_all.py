import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python


if hasattr(argparse, "BooleanOptionalAction"):
    BooleanOptionalAction = argparse.BooleanOptionalAction
else:

    class BooleanOptionalAction(argparse.Action):
        def __init__(
            self,
            option_strings,
            dest,
            default=None,
            required=False,
            help=None,
            metavar=None,
        ):
            expanded_option_strings = []
            for option_string in option_strings:
                expanded_option_strings.append(option_string)
                if option_string.startswith("--"):
                    expanded_option_strings.append("--no-" + option_string[2:])

            super().__init__(
                option_strings=expanded_option_strings,
                dest=dest,
                nargs=0,
                default=default,
                required=required,
                help=help,
                metavar=metavar,
            )

        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, not option_string.startswith("--no-"))


def parse_output_size(value):
    value = str(value).strip().lower()
    if value in {"none", "original", "raw"}:
        return None

    value = value.replace("*", "x")
    if "x" in value:
        width_text, height_text = value.split("x", 1)
    else:
        width_text = value
        height_text = value

    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            "output size must be WIDTHxHEIGHT, a single integer, or original"
        ) from e

    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("output width and height must be positive")

    return width, height


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run a full fixed or PlanningAgent-generated task sequence while "
            "saving the same FPV/video/scene-graph artifacts as scripts/test.py."
        )
    )
    parser.add_argument("--task", default="store_apple_and_tissue_box_in_bottom_cabinet")
    parser.add_argument("--scene", default="Wainscott_0_int")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "PlanningAgent model name. When omitted, execute the task's fixed "
            "example_planning sequence."
        ),
    )
    parser.add_argument(
        "--local-llm-serve",
        action="store_true",
        help="Use the OpenAI-compatible local model server for PlanningAgent.",
    )
    parser.add_argument("--local-serve-ip", default="")
    parser.add_argument("--local-serve-key", default="sk-123456")
    parser.add_argument(
        "--work-dir",
        default=None,
        help=(
            "PlanningAgent work directory. Defaults to OUTPUT_DIR/planner_work_dir. "
            "The agent's multi-view observations are saved below its benchmark directory."
        ),
    )
    parser.add_argument(
        "--primitive-type",
        choices=("auto", "ego", "starter", "symbolic"),
        default="auto",
        help="Primitive set used by both the benchmark and PlanningAgent.",
    )
    parser.add_argument(
        "--prompt-setting",
        choices=("default", "v0", "v1", "v2", "v3"),
        default="default",
        help="PlanningAgent prompt variant.",
    )
    parser.add_argument(
        "--use-initial-setup",
        action=BooleanOptionalAction,
        default=False,
        help="Include the task's initial setup text in PlanningAgent prompts.",
    )
    parser.add_argument(
        "--use-self-caption",
        action=BooleanOptionalAction,
        default=False,
        help="Generate a visual scene caption before PlanningAgent starts planning.",
    )
    parser.add_argument(
        "--planner-use-obs",
        action=BooleanOptionalAction,
        default=True,
        help="Provide saved multi-view observations to PlanningAgent.",
    )
    parser.add_argument(
        "--planner-debug",
        action="store_true",
        help="Pause for confirmation before each PlanningAgent model request.",
    )
    parser.add_argument(
        "--plan-max-steps",
        type=int,
        default=None,
        help="Maximum generated planning steps. Defaults to example plan length plus 10.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/test_store_apple_and_tissue_box_in_bottom_cabinet_full",
        help=(
            "Base directory for saved first-person RGB images, videos, and reports. "
            "A run timestamp is appended by default."
        ),
    )
    parser.add_argument(
        "--timestamp-output",
        action=BooleanOptionalAction,
        default=True,
        help="Append a YYYYMMDD_HHMMSS timestamp to OUTPUT_DIR for each run.",
    )
    parser.add_argument(
        "--output-size",
        type=parse_output_size,
        default=parse_output_size("512x512"),
        help="Resize saved RGB output to WIDTHxHEIGHT, a square size like 512, or original.",
    )
    parser.add_argument(
        "--capture-during-actions",
        action=BooleanOptionalAction,
        default=True,
        help="Capture robot FPV frames periodically during all low-level primitive steps.",
    )
    parser.add_argument(
        "--capture-every",
        type=int,
        default=2,
        help="Capture one mp4 frame every N low-level primitive steps.",
    )
    parser.add_argument(
        "--save-step-images",
        action=BooleanOptionalAction,
        default=True,
        help="Save one robot FPV png after each high-level action.",
    )
    parser.add_argument(
        "--save-video",
        action=BooleanOptionalAction,
        default=True,
        help="Save captured robot RGB frames as an mp4.",
    )
    parser.add_argument(
        "--video-path",
        default=None,
        help="Path for the output mp4. Defaults to OUTPUT_DIR/nav_rgb.mp4.",
    )
    parser.add_argument("--video-fps", type=float, default=10.0)
    parser.add_argument(
        "--headless",
        action=BooleanOptionalAction,
        default=True,
        help="Launch OmniGibson headlessly.",
    )
    parser.add_argument(
        "--clear-on-exit",
        action="store_true",
        help="Call og.clear() before exiting. Disabled by default to avoid Kit teardown crashes.",
    )
    parser.add_argument(
        "--scene-graph-backend",
        default=os.environ.get("ISBENCH_SCENE_GRAPH_BACKEND", "omnigibson_truth"),
        choices=[
            "omnigibson_truth",
            "truth",
            "unigoal_grounded_sam",
            "samjam_sam2",
            "disabled",
        ],
        help="Scene graph backend used by OnlineBenchmark.",
    )
    parser.add_argument(
        "--scene-graph-step-interval",
        type=int,
        default=0,
        help="Low-level step interval for scene graph updates. Use 0 to update after each high-level action.",
    )
    parser.add_argument(
        "--scene-graph-update-every",
        type=int,
        default=int(os.environ.get("ISBENCH_SCENE_GRAPH_UPDATE_EVERY", "1")),
        help=(
            "Advanced perception skip interval. In this full-test script, "
            "samjam_sam2 is forced to 1 so every triggered graph update runs SamJam."
        ),
    )
    parser.add_argument(
        "--scene-graph-history-interval",
        type=int,
        default=int(os.environ.get("ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL", "1")),
        help="Save one scene graph history snapshot every N scene graph updates.",
    )
    parser.add_argument(
        "--scene-graph-image-size",
        type=parse_output_size,
        default=parse_output_size(os.environ.get("ISBENCH_SCENE_GRAPH_IMAGE_SIZE", "256x256")),
        help="Robot vision sensor resolution used by perception scene graph backends.",
    )
    parser.add_argument(
        "--stop-on-error",
        action=BooleanOptionalAction,
        default=False,
        help="Stop executing the plan after the first failed action.",
    )
    parser.add_argument(
        "--nav-stuck-waypoint-tolerance",
        type=float,
        default=None,
        help=(
            "Override ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE. Useful when navigation "
            "gets within a few centimeters of a waypoint but reports stuck."
        ),
    )
    parser.add_argument(
        "--nav-stuck-final-waypoint-tolerance",
        type=float,
        default=None,
        help=(
            "Override ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE. When omitted, "
            "the final waypoint tolerance is at least 0.30 m."
        ),
    )
    parser.add_argument(
        "--nav-goal-clearance-radius",
        type=float,
        default=None,
        help=(
            "Override ISBENCH_NAV_GOAL_CLEARANCE_RADIUS. Candidate navigation goals "
            "must have this extra traversable-map clearance."
        ),
    )
    parser.add_argument(
        "--nav-max-floor-height-delta",
        type=float,
        default=None,
        help=(
            "Override ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA. Navigation aborts if the "
            "robot base height leaves the current floor by more than this amount."
        ),
    )
    parser.add_argument(
        "--save-surrounding-observations",
        action=BooleanOptionalAction,
        default=False,
        help="Also save OnlineBenchmark multi-view observations after each high-level action.",
    )
    parser.add_argument(
        "--show-robot",
        action="store_true",
        help="Keep robot visible in viewer-style captures.",
    )
    args = parser.parse_args()
    if args.local_llm_serve and not args.model:
        parser.error("--local-llm-serve requires --model")
    if args.plan_max_steps is not None and args.plan_max_steps <= 0:
        parser.error("--plan-max-steps must be greater than zero")
    return args


ARGS = parse_args()

if ARGS.headless:
    os.environ["OMNIGIBSON_HEADLESS"] = "1"

if ARGS.scene_graph_backend == "samjam_sam2":
    ARGS.scene_graph_update_every = 1

os.environ["ISBENCH_SCENE_GRAPH_BACKEND"] = ARGS.scene_graph_backend
os.environ["ISBENCH_SCENE_GRAPH_UPDATE_EVERY"] = str(ARGS.scene_graph_update_every)
os.environ["ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL"] = str(
    ARGS.scene_graph_history_interval
)
if ARGS.scene_graph_image_size is not None:
    os.environ["ISBENCH_SCENE_GRAPH_IMAGE_WIDTH"] = str(ARGS.scene_graph_image_size[0])
    os.environ["ISBENCH_SCENE_GRAPH_IMAGE_HEIGHT"] = str(ARGS.scene_graph_image_size[1])
if ARGS.nav_stuck_waypoint_tolerance is not None:
    os.environ["ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE"] = str(
        ARGS.nav_stuck_waypoint_tolerance
    )
if ARGS.nav_stuck_final_waypoint_tolerance is not None:
    os.environ["ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE"] = str(
        ARGS.nav_stuck_final_waypoint_tolerance
    )
if ARGS.nav_goal_clearance_radius is not None:
    os.environ["ISBENCH_NAV_GOAL_CLEARANCE_RADIUS"] = str(
        ARGS.nav_goal_clearance_radius
    )
if ARGS.nav_max_floor_height_delta is not None:
    os.environ["ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA"] = str(
        ARGS.nav_max_floor_height_delta
    )

maybe_reexec_with_omnigibson_python()

# Isaac Sim will otherwise receive this script's CLI args and try to interpret them.
sys.argv = [sys.argv[0]]

from og_ego_prim.utils.monkey_patch import add_monkey_patch

add_monkey_patch()

import numpy as np
import omnigibson as og
from omnigibson.macros import gm
from PIL import Image

from og_ego_prim.benchmark import build_benchmark
from og_ego_prim.models import PlanningAgent

gm.USE_GPU_DYNAMICS = True


def _to_numpy_image(frame):
    if hasattr(frame, "detach"):
        frame = frame.detach().cpu().numpy()
    frame = np.asarray(frame)

    if frame.dtype != np.uint8:
        if frame.max() <= 1.0:
            frame = frame * 255.0
        frame = np.clip(frame, 0, 255).astype(np.uint8)

    if frame.ndim == 3 and frame.shape[-1] > 3:
        frame = frame[:, :, :3]

    return frame


def _resize_rgb(rgb, output_size):
    if output_size is None:
        return rgb

    width, height = output_size
    if rgb.shape[1] == width and rgb.shape[0] == height:
        return rgb

    return np.asarray(
        Image.fromarray(rgb).resize((width, height), Image.Resampling.LANCZOS)
    )


def get_robot_rgb(robot, output_size=None):
    obs, _ = robot.get_obs()
    for sensor_name, sensor_obs in obs.items():
        if not isinstance(sensor_obs, dict):
            continue
        if "rgb" in sensor_obs:
            raw_rgb = _to_numpy_image(sensor_obs["rgb"])
            return sensor_name, _resize_rgb(raw_rgb, output_size), raw_rgb.shape
    raise RuntimeError(
        f'No robot RGB observation found. Available keys: {list(obs.keys())}'
    )


def save_robot_rgb(robot, save_path: Path, output_size=None):
    sensor_name, rgb, raw_shape = get_robot_rgb(robot, output_size)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(save_path)
    return sensor_name, raw_shape, rgb.shape


def save_robot_rgb_with_frame(robot, save_path: Path, output_size=None):
    sensor_name, rgb, raw_shape = get_robot_rgb(robot, output_size)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(save_path)
    return sensor_name, raw_shape, rgb.shape, rgb


def track_robot_rgb_video(robot, tracker, output_size=None):
    sensor_name, rgb, raw_shape = get_robot_rgb(robot, output_size)
    tracker.track_video_rgb(rgb)
    return sensor_name, raw_shape, rgb.shape, rgb


def summarize_scene_graph(snapshot):
    if snapshot is None:
        return {
            "backend": None,
            "global_step_index": None,
            "frame_index": None,
            "objects": 0,
            "rooms": 0,
            "groups": 0,
            "relations": 0,
            "membership_edges": 0,
            "edges": 0,
            "skipped": None,
        }

    metadata = snapshot.get("metadata", {})
    nodes = snapshot.get("nodes", [])
    object_count = metadata.get("object_count")
    if object_count is None:
        object_count = sum(
            1 for node in nodes if node.get("category") not in {"room", "group"}
        )
    room_graph = metadata.get("room_graph", {})
    group_graph = metadata.get("group_graph", {})
    return {
        "backend": metadata.get("perception_backend"),
        "global_step_index": metadata.get("global_step_index"),
        "frame_index": metadata.get("frame_index"),
        "objects": object_count,
        "rooms": len(room_graph.get("rooms", [])),
        "groups": len(group_graph.get("groups", [])),
        "relations": metadata.get("relation_count", 0),
        "membership_edges": metadata.get("membership_edge_count", 0),
        "edges": metadata.get("total_edge_count", len(snapshot.get("edges", []))),
        "skipped": metadata.get("perception_skipped"),
    }


def print_scene_graph_summary(prefix, benchmark):
    summary = summarize_scene_graph(benchmark.tracker.latest_scene_graph)
    print(
        "{} scene_graph backend={} global_step={} frame={} objects={} rooms={} groups={} relations={} membership_edges={} total_edges={} skipped={}".format(
            prefix,
            summary["backend"],
            summary["global_step_index"],
            summary["frame_index"],
            summary["objects"],
            summary["rooms"],
            summary["groups"],
            summary["relations"],
            summary["membership_edges"],
            summary["edges"],
            summary["skipped"],
        )
    )


def save_scene_graph_report(args, benchmark, output_dir: Path):
    save_path = output_dir / "scene_graph_report.json"
    latest = benchmark.tracker.latest_scene_graph
    report = {
        "task": args.task,
        "scene": args.scene,
        "scene_graph_backend": args.scene_graph_backend,
        "scene_graph_step_interval": args.scene_graph_step_interval,
        "scene_graph_update_every": args.scene_graph_update_every,
        "latest_summary": summarize_scene_graph(latest),
        "latest_scene_graph": latest,
        "scene_graph_history": benchmark.tracker.scene_graph_history,
        "error_stack": benchmark.tracker.error_stack,
        "execution_diagnostics": benchmark.tracker.execution_diagnostics,
        "termination": benchmark.tracker.termination,
        "goal_condition": benchmark.tracker.goal_condition,
    }
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"saved scene graph json: {save_path}")


def safe_step_name(step_index: int, action: str) -> str:
    safe_action = (
        action.lower()
        .replace("(", "__")
        .replace(")", "")
        .replace(",", "_")
        .replace(".", "_")
        .replace(" ", "")
    )
    return f"step_{step_index:02d}_{safe_action}.png"


def safe_step_dir_name(step_index: int, action: str) -> str:
    safe_action = (
        action.lower()
        .replace("(", "__")
        .replace(")", "")
        .replace(",", "_")
        .replace(".", "_")
        .replace(" ", "")
    )
    return f"step_{step_index:02d}_{safe_action}"


def save_step_image(args, robot, output_dir: Path, step_index: int, action: str):
    if not args.save_step_images:
        return
    save_path = output_dir / safe_step_name(step_index, action)
    sensor_name, raw_shape, shape = save_robot_rgb(robot, save_path, args.output_size)
    print(
        f"saved {save_path.name} from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )


def save_surrounding_observations(benchmark, save_path: Path):
    video_cache_size = len(benchmark.tracker.video_cache)
    benchmark.get_surrounding_viewer_obs(save_img=str(save_path))
    del benchmark.tracker.video_cache[video_cache_size:]


def get_planner_paths(args, output_dir: Path):
    work_dir = (
        Path(args.work_dir).expanduser()
        if args.work_dir is not None
        else output_dir / "planner_work_dir"
    )
    work_dir = work_dir.resolve()
    model_tag = args.model.replace("/", "__")
    benchmark_tag = f"{args.task}___{args.scene}"
    observation_root = work_dir / "benchmark" / benchmark_tag / model_tag
    observation_root.mkdir(parents=True, exist_ok=True)
    return work_dir, observation_root


def planner_step_tag(step_index: int, action: str):
    return f"{step_index}_" + action.replace("(", "__").replace(")", "__")


def save_planner_observations(benchmark, observation_root: Path, step_tag: str):
    save_path = observation_root / step_tag
    save_path.mkdir(parents=True, exist_ok=True)
    for stale_image in save_path.glob("*.png"):
        stale_image.unlink()
    save_surrounding_observations(benchmark, save_path)
    print(f"saved PlanningAgent observations: {save_path}")


def save_rgb_video(frames, save_path: Path, fps: float):
    if not frames:
        return None

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to save videos")

    save_path.parent.mkdir(parents=True, exist_ok=True)
    first = np.asarray(frames[0])
    height, width = first.shape[:2]
    if any(np.asarray(frame).shape[:2] != (height, width) for frame in frames):
        raise ValueError("all video frames must have the same dimensions")

    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(save_path),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for frame in frames:
            rgb = np.asarray(frame)
            if rgb.dtype != np.uint8:
                rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            process.stdin.write(np.ascontiguousarray(rgb[:, :, :3]).tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise

    if return_code != 0:
        raise RuntimeError(f"ffmpeg failed to save video: {stderr.strip()}")

    return {
        "path": str(save_path),
        "fps": fps,
        "frames": len(frames),
        "width": width,
        "height": height,
    }


def set_samjam_output_dir(benchmark, output_dir: Path):
    updater = getattr(benchmark, "scene_graph_updater", None)
    backend = getattr(updater, "backend", None)
    if backend is None or not hasattr(backend, "output_writer"):
        return False

    from og_ego_prim.scene_graph.backends.samjam_sam2 import SAMJAMOutputWriter

    backend.output_writer = SAMJAMOutputWriter(output_dir)
    return True


def capture_robot_rgb_frame(robot, output_size):
    return get_robot_rgb(robot, output_size)[1]


def save_report_and_video(args, benchmark, output_dir: Path):
    report_path = output_dir / "report.json"
    benchmark.tracker.save_tracking(str(report_path))
    print(f"saved report json: {report_path}")

    default_video = output_dir / "video.mp4"
    target_video = Path(args.video_path) if args.video_path else output_dir / "nav_rgb.mp4"
    if args.save_video and default_video.exists():
        target_video.parent.mkdir(parents=True, exist_ok=True)
        if default_video.resolve() != target_video.resolve():
            shutil.copyfile(default_video, target_video)
        print(f"saved video: {target_video}")


def get_run_output_dir(base_output_dir: str, add_timestamp: bool) -> Path:
    output_dir = Path(base_output_dir).expanduser()
    if not add_timestamp:
        return output_dir

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = output_dir.with_name(f"{output_dir.name}_{timestamp}")
    suffix = 1
    while candidate.exists():
        candidate = output_dir.with_name(
            f"{output_dir.name}_{timestamp}_{suffix:02d}"
        )
        suffix += 1
    return candidate


def main():
    args = ARGS
    output_dir = get_run_output_dir(args.output_dir, args.timestamp_output)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"run output directory: {output_dir.resolve()}")
    if args.scene_graph_backend == "samjam_sam2":
        os.environ["ISBENCH_SAMJAM_OUTPUT_DIR"] = str(output_dir / "samjam_outputs")

    benchmark = build_benchmark(
        task=args.task,
        scene=args.scene,
        ego_view=not args.show_robot,
        draw_bbox_2d=False,
        primitive_type=None if args.primitive_type == "auto" else args.primitive_type,
        scene_graph_step_interval=args.scene_graph_step_interval,
        scene_graph_backend=args.scene_graph_backend,
        use_initial_setup=args.use_initial_setup,
        use_self_caption=args.use_self_caption,
        online_object_sampling=False,
        debug=False,
        eval_process_safety=True,
        eval_termination_safety=True,
        eval_awareness=False,
        eval_execution=True,
    )
    benchmark.tracker.video_fps = args.video_fps
    print(
        "scene_graph_config: backend={} step_interval={} update_every={} history_interval={} sensor_size={}".format(
            args.scene_graph_backend,
            args.scene_graph_step_interval,
            args.scene_graph_update_every,
            args.scene_graph_history_interval,
            args.scene_graph_image_size,
        )
    )
    print_scene_graph_summary("initial", benchmark)

    robot = benchmark.env.robots[0]
    print("before:", robot.get_position_orientation()[0].tolist())
    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "fpv_before.png", args.output_size
    )
    print(
        f"saved fpv_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "obs_rgb_before.png", args.output_size
    )
    print(
        f"saved obs_rgb_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    if args.save_video:
        track_robot_rgb_video(robot, benchmark.tracker, args.output_size)
    if args.save_surrounding_observations:
        save_surrounding_observations(benchmark, output_dir / "0_init")

    planner_observation_root = None
    if args.model:
        planner_work_dir, planner_observation_root = get_planner_paths(args, output_dir)
        if args.planner_use_obs:
            save_planner_observations(
                benchmark,
                planner_observation_root,
                "0_init",
            )

        prompt_setting = args.prompt_setting
        if prompt_setting == "default" and benchmark.primitive_type != "starter":
            prompt_setting = "v1"

        agent = PlanningAgent(
            task_name=args.task,
            scene_name=args.scene,
            agent_name=args.model,
            work_dir=str(planner_work_dir),
            local_llm_serve=args.local_llm_serve,
            local_serve_ip=args.local_serve_ip,
            local_serve_key=args.local_serve_key,
            debug=args.planner_debug,
            prompt_setting=prompt_setting,
            primitive_type=benchmark.primitive_type,
            use_initial_setup=args.use_initial_setup,
            use_self_caption=args.use_self_caption,
        )
        agent.set_tracker(benchmark.tracker)

        if args.use_self_caption:
            caption = agent.generate_caption(use_obs=args.planner_use_obs)
            benchmark.tracker.track_caption(content=caption)
        if prompt_setting == "v2":
            awareness = agent.generate_awareness(use_obs=args.planner_use_obs)
            benchmark.tracker.track_awareness(content=awareness, eval_results=None)

        max_steps = args.plan_max_steps
        if max_steps is None:
            max_steps = len(benchmark._example_planning) + 10
        planner = agent.step(
            use_obs=args.planner_use_obs,
            max_step=max_steps,
        )
        print(
            f"planning_source: PlanningAgent model={args.model} "
            f"max_steps={max_steps} obs={args.planner_use_obs}"
        )
    else:
        planner = benchmark.get_example_planning()
        print("planning_source: fixed example_planning")

    original_step_callback = benchmark.executor.step_callback
    capture_every = max(args.capture_every, 1)
    current_step_frames = None

    def wrapped_step_callback(context):
        if original_step_callback is not None:
            original_step_callback(context)
        if not args.capture_during_actions or not args.save_video:
            return
        if context.step_index % capture_every != 0:
            return
        rgb = capture_robot_rgb_frame(robot, args.output_size)
        benchmark.tracker.track_video_rgb(rgb)
        if current_step_frames is not None:
            current_step_frames.append(rgb)

    benchmark.executor.step_callback = wrapped_step_callback

    for step_index, plan in enumerate(planner, start=1):
        action = plan["action"]
        step_dir = output_dir / safe_step_dir_name(step_index, action)
        step_dir.mkdir(parents=True, exist_ok=True)
        if args.scene_graph_backend == "samjam_sam2":
            set_samjam_output_dir(benchmark, step_dir / "samjam_outputs")

        print(f"plan_step_{step_index:02d}: {action}")
        current_step_frames = []
        sensor_name, raw_shape, shape, before_rgb = save_robot_rgb_with_frame(
            robot, step_dir / "obs_before.png", args.output_size
        )
        current_step_frames.append(before_rgb)
        print(
            f"saved {step_dir.name}/obs_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
        )

        execution_succeeded = benchmark.execute_plan(plan)
        sensor_name, raw_shape, shape, after_rgb = save_robot_rgb_with_frame(
            robot, step_dir / "obs_after.png", args.output_size
        )
        current_step_frames.append(after_rgb)
        print(
            f"saved {step_dir.name}/obs_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
        )
        if args.save_video:
            benchmark.tracker.track_video_rgb(after_rgb)
            video_info = save_rgb_video(
                current_step_frames,
                step_dir / "nav_rgb.mp4",
                args.video_fps,
            )
            if video_info is not None:
                shutil.copyfile(step_dir / "nav_rgb.mp4", step_dir / "video.mp4")
                print(
                    f"saved {step_dir.name}/nav_rgb.mp4 and {step_dir.name}/video.mp4 "
                    f"({video_info['frames']} frames, {video_info['fps']} fps)"
                )
        current_step_frames = None
        save_scene_graph_report(args, benchmark, step_dir)
        print_scene_graph_summary(f"after_step_{step_index:02d}", benchmark)
        if args.save_surrounding_observations:
            step_tag = f"{step_index:02d}_" + action.replace("(", "__").replace(")", "__")
            save_surrounding_observations(benchmark, step_dir / "surrounding_obs" / step_tag)
        if planner_observation_root is not None and args.planner_use_obs:
            save_planner_observations(
                benchmark,
                planner_observation_root,
                planner_step_tag(step_index, action),
            )
        if not execution_succeeded and args.stop_on_error:
            print(f"stopping after failed action: {action}")
            break

    benchmark.termination_evaluation()
    if args.scene_graph_backend == "samjam_sam2":
        set_samjam_output_dir(benchmark, output_dir / "samjam_outputs_final")
    try:
        benchmark._refresh_scene_graph(force=True)
        print_scene_graph_summary("final_forced", benchmark)
    except Exception as e:
        benchmark.tracker.track_error(
            action="force_scene_graph_final",
            err_type=e.__class__.__name__,
            msg=str(e),
        )
        print(f"force_scene_graph_final failed: {e.__class__.__name__}: {e}")

    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "fpv_after.png", args.output_size
    )
    print("after:", robot.get_position_orientation()[0].tolist())
    print(
        f"saved fpv_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "obs_rgb_after.png", args.output_size
    )
    print(
        f"saved obs_rgb_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )

    save_report_and_video(args, benchmark, output_dir)
    save_scene_graph_report(args, benchmark, output_dir)
    print("scene_graph_history:", len(benchmark.tracker.scene_graph_history))

    sys.stdout.flush()
    sys.stderr.flush()
    if args.clear_on_exit:
        og.clear()
        return
    os._exit(0)


if __name__ == "__main__":
    main()
