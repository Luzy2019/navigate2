import argparse
import json
import os
from pathlib import Path
import shutil
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
            "Run a full task example_planning sequence while saving the same "
            "FPV/video/scene-graph artifacts as scripts/test.py."
        )
    )
    parser.add_argument("--task", default="store_apple_and_tissue_box_in_bottom_cabinet")
    parser.add_argument("--scene", default="Wainscott_0_int")
    parser.add_argument(
        "--output-dir",
        default="outputs/test_store_apple_and_tissue_box_in_bottom_cabinet_full",
        help="Directory for saved first-person RGB images, videos, and reports.",
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
        default=False,
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
        help="Save one robot FPV png after each high-level example_planning action.",
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
        default=int(os.environ.get("ISBENCH_SCENE_GRAPH_UPDATE_EVERY", "5")),
        help="Perception backend update interval when scene graph updates happen during low-level steps.",
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
        help="Stop executing example_planning after the first failed action.",
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
        "--save-surrounding-observations",
        action=BooleanOptionalAction,
        default=True,
        help="Also save OnlineBenchmark multi-view observations after each high-level action.",
    )
    parser.add_argument(
        "--show-robot",
        action="store_true",
        help="Keep robot visible in viewer-style captures.",
    )
    return parser.parse_args()


ARGS = parse_args()

if ARGS.headless:
    os.environ["OMNIGIBSON_HEADLESS"] = "1"

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


def main():
    args = ARGS
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.scene_graph_backend == "samjam_sam2":
        os.environ.setdefault("ISBENCH_SAMJAM_OUTPUT_DIR", str(output_dir / "samjam_outputs"))

    benchmark = build_benchmark(
        task=args.task,
        scene=args.scene,
        ego_view=not args.show_robot,
        draw_bbox_2d=False,
        scene_graph_step_interval=args.scene_graph_step_interval,
        scene_graph_backend=args.scene_graph_backend,
        use_initial_setup=False,
        use_self_caption=False,
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

    original_step_callback = benchmark.executor.step_callback
    capture_every = max(args.capture_every, 1)

    def wrapped_step_callback(context):
        if original_step_callback is not None:
            original_step_callback(context)
        if not args.capture_during_actions or not args.save_video:
            return
        if context.step_index % capture_every != 0:
            return
        benchmark.tracker.track_video_rgb(get_robot_rgb(robot, args.output_size)[1])

    benchmark.executor.step_callback = wrapped_step_callback

    for step_index, plan in enumerate(benchmark.get_example_planning(), start=1):
        action = plan["action"]
        print(f"plan_step_{step_index:02d}: {action}")
        execution_succeeded = benchmark.execute_plan(plan)
        if args.save_video:
            track_robot_rgb_video(robot, benchmark.tracker, args.output_size)
        save_step_image(args, robot, output_dir, step_index, action)
        print_scene_graph_summary(f"after_step_{step_index:02d}", benchmark)
        if args.save_surrounding_observations:
            step_tag = f"{step_index:02d}_" + action.replace("(", "__").replace(")", "__")
            save_surrounding_observations(benchmark, output_dir / step_tag)
        if not execution_succeeded and args.stop_on_error:
            print(f"stopping after failed action: {action}")
            break

    benchmark.termination_evaluation()
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
