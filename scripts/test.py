import argparse
import json
import os
from pathlib import Path
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
        description="Smoke test for store_a_tennis_ball @ Rs_int with robot first-person RGB capture."
    )
    parser.add_argument("--task", default="store_a_tennis_ball")
    parser.add_argument("--scene", default="Rs_int")
    parser.add_argument("--navigate-target", default="bucket.n.01_1")
    parser.add_argument(
        "--run-nav",
        action="store_true",
        help="Execute NAVIGATE_TO(target) after startup.",
    )
    parser.add_argument(
        "--capture-during-nav",
        action=BooleanOptionalAction,
        default=True,
        help="Capture robot FPV frames periodically during NAVIGATE_TO.",
    )
    parser.add_argument(
        "--capture-every",
        type=int,
        default=2,
        help="Default capture interval in low-level navigation steps.",
    )
    parser.add_argument(
        "--video-every",
        type=int,
        default=None,
        help="Capture one mp4 frame every N low-level navigation steps. Defaults to --capture-every.",
    )
    parser.add_argument(
        "--png-every",
        type=int,
        default=None,
        help="Save one NAVIGATE_TO png every N low-level navigation steps. Defaults to --capture-every.",
    )
    parser.add_argument(
        "--output-size",
        type=parse_output_size,
        default=parse_output_size("512x512"),
        help="Resize saved RGB output to WIDTHxHEIGHT, a square size like 512, or original.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/test_store_a_tennis_ball_fpv",
        help="Directory for saved first-person RGB images and videos.",
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
    parser.add_argument(
        "--video-fps",
        type=float,
        default=10.0,
        help="FPS for the saved mp4.",
    )
    parser.add_argument(
        "--save-nav-images",
        action="store_true",
        help="Also save per-step NAVIGATE_TO frames as png files.",
    )
    parser.add_argument(
        "--headless",
        action=BooleanOptionalAction,
        default=True,
        help="Launch OmniGibson headlessly to avoid X11 / GUI crashes.",
    )
    parser.add_argument(
        "--disable-telemetry",
        action="store_true",
        help="Use a temporary OmniGibson kit with omni.kit.telemetry removed.",
    )
    parser.add_argument(
        "--clear-on-exit",
        action="store_true",
        help="Call og.clear() before exiting. Disabled by default to avoid Kit viewport teardown crashes.",
    )
    parser.add_argument(
        "--scene-graph-backend",
        default=os.environ.get("ISBENCH_SCENE_GRAPH_BACKEND", "unigoal_grounded_sam"),
        choices=["unigoal_grounded_sam", "samjam_sam2", "truth", "disabled"],
        help="Scene graph backend used by OnlineBenchmark.",
    )
    parser.add_argument(
        "--scene-graph-update-every",
        type=int,
        default=int(os.environ.get("ISBENCH_SCENE_GRAPH_UPDATE_EVERY", "5")),
        help="Run heavy scene graph perception every N low-level steps.",
    )
    parser.add_argument(
        "--scene-graph-history-interval",
        type=int,
        default=int(os.environ.get("ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL", "1")),
        help="Save one scene graph history snapshot every N low-level steps.",
    )
    parser.add_argument(
        "--scene-graph-image-size",
        type=parse_output_size,
        default=parse_output_size(
            os.environ.get("ISBENCH_SCENE_GRAPH_IMAGE_SIZE", "256x256")
        ),
        help="Robot vision sensor resolution used by the scene graph backend.",
    )
    parser.add_argument(
        "--print-scene-graph-every",
        type=int,
        default=5,
        help="Print scene graph summary every N NAVIGATE_TO low-level steps.",
    )
    parser.add_argument(
        "--force-scene-graph-after-nav",
        action=BooleanOptionalAction,
        default=True,
        help="Force one scene graph update after NAVIGATE_TO finishes.",
    )
    parser.add_argument(
        "--save-scene-graph-json",
        action=BooleanOptionalAction,
        default=True,
        help="Save latest scene graph and history to json.",
    )
    parser.add_argument(
        "--samjam-overlap-relations",
        action=BooleanOptionalAction,
        default=False,
        help="For samjam_sam2, add bbox-overlap placeholder relation edges.",
    )
    parser.add_argument(
        "--scene-graph-json-path",
        default=None,
        help="Path for scene graph json. Defaults to OUTPUT_DIR/scene_graph_report.json.",
    )
    return parser.parse_args()

ARGS = parse_args()

if ARGS.headless:
    os.environ["OMNIGIBSON_HEADLESS"] = "1"

os.environ["ISBENCH_SCENE_GRAPH_BACKEND"] = ARGS.scene_graph_backend
os.environ["ISBENCH_SCENE_GRAPH_UPDATE_EVERY"] = str(ARGS.scene_graph_update_every)
os.environ["ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL"] = str(ARGS.scene_graph_history_interval)
if ARGS.scene_graph_image_size is not None:
    os.environ["ISBENCH_SCENE_GRAPH_IMAGE_WIDTH"] = str(ARGS.scene_graph_image_size[0])
    os.environ["ISBENCH_SCENE_GRAPH_IMAGE_HEIGHT"] = str(ARGS.scene_graph_image_size[1])
os.environ["ISBENCH_SAMJAM_ENABLE_OVERLAP_RELATIONS"] = (
    "1" if ARGS.samjam_overlap_relations else "0"
)

maybe_reexec_with_omnigibson_python()

# Isaac Sim will otherwise receive this script's CLI args and try to interpret them.
sys.argv = [sys.argv[0]]

import numpy as np
import omnigibson as og
from PIL import Image

from og_ego_prim.benchmark import build_benchmark


def _maybe_patch_omnigibson_kit():
    if not ARGS.disable_telemetry:
        return None

    import omnigibson.simulator as og_simulator

    original_copy = og_simulator.shutil.copy

    def patched_copy(src, dst, *args, **kwargs):
        src_path = Path(src)
        dst_path = Path(dst)

        if src_path.name.startswith("omnigibson_") and src_path.suffix == ".kit":
            text = src_path.read_text()
            patched_text = text.replace('"omni.kit.telemetry" = {}\n', "")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            dst_path.write_text(patched_text)
            print(f"patched OmniGibson kit on copy: {dst_path}")
            return str(dst_path)

        return original_copy(src, dst, *args, **kwargs)

    og_simulator.shutil.copy = patched_copy
    return True


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


def save_obs_rgb(robot, save_path: Path, output_size=None):
    return save_robot_rgb(robot, save_path, output_size)


def track_robot_rgb_video(robot, tracker, output_size=None):
    sensor_name, rgb, raw_shape = get_robot_rgb(robot, output_size)
    tracker.track_video_rgb(rgb)
    return sensor_name, raw_shape, rgb.shape, rgb


def save_video_if_requested(args, benchmark, output_dir: Path):
    if not args.save_video:
        return

    video_path = Path(args.video_path) if args.video_path else output_dir / "nav_rgb.mp4"
    try:
        video_info = benchmark.tracker.save_video(str(video_path))
    except Exception as e:
        print(f"saved video: failed with {e.__class__.__name__}: {e}")
        return

    if video_info is None:
        print("saved video: skipped because no frames were captured")
        return

    print(f"saved video: {video_path} ({video_info['frames']} frames, {video_info['fps']} fps)")


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
        object_count = sum(1 for node in nodes if node.get("category") not in {"room", "group"})
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


def save_scene_graph_json_if_requested(args, benchmark, output_dir: Path):
    if not args.save_scene_graph_json:
        return

    save_path = (
        Path(args.scene_graph_json_path)
        if args.scene_graph_json_path
        else output_dir / "scene_graph_report.json"
    )
    save_path.parent.mkdir(parents=True, exist_ok=True)
    latest = benchmark.tracker.latest_scene_graph
    report = {
        "task": args.task,
        "scene": args.scene,
        "navigate_target": args.navigate_target,
        "scene_graph_backend": args.scene_graph_backend,
        "scene_graph_update_every": args.scene_graph_update_every,
        "latest_summary": summarize_scene_graph(latest),
        "latest_scene_graph": latest,
        "scene_graph_history": benchmark.tracker.scene_graph_history,
        "error_stack": benchmark.tracker.error_stack,
    }
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"saved scene graph json: {save_path}")


def main():
    args = ARGS
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.scene_graph_backend == "samjam_sam2":
        os.environ.setdefault("ISBENCH_SAMJAM_OUTPUT_DIR", str(output_dir / "samjam_outputs"))
    _maybe_patch_omnigibson_kit()
    default_capture_every = max(args.capture_every, 1)
    video_every = max(args.video_every or default_capture_every, 1)
    png_every = max(args.png_every or default_capture_every, 1)

    benchmark = build_benchmark(
        task=args.task,
        scene=args.scene,
        ego_view=False,
        draw_bbox_2d=False,
        use_initial_setup=False,
        use_self_caption=False,
        online_object_sampling=False,
        debug=False,
        eval_process_safety=False,
        eval_termination_safety=False,
        eval_awareness=False,
        eval_execution=False,
    )
    benchmark.tracker.video_fps = args.video_fps
    print(
        "scene_graph_config: backend={} update_every={} history_interval={} sensor_size={}".format(
            args.scene_graph_backend,
            args.scene_graph_update_every,
            args.scene_graph_history_interval,
            args.scene_graph_image_size,
        )
    )
    print_scene_graph_summary("initial", benchmark)

    robot = benchmark.env.robots[0]
    start_pos = robot.get_position_orientation()[0].tolist()
    print("before:", start_pos)

    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "fpv_before.png", args.output_size
    )
    print(
        f"saved fpv_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    sensor_name, raw_shape, shape = save_obs_rgb(
        robot, output_dir / "obs_rgb_before.png", args.output_size
    )
    print(
        f"saved obs_rgb_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    if args.save_video:
        track_robot_rgb_video(robot, benchmark.tracker, args.output_size)

    if args.run_nav:
        original_step_callback = benchmark.executor.step_callback
        print_every = max(args.print_scene_graph_every, 0)

        def wrapped_step_callback(context):
            if original_step_callback is not None:
                original_step_callback(context)

            if context.primitive_name == "NAVIGATE_TO" and print_every > 0:
                if context.step_index % print_every == 0:
                    print_scene_graph_summary(f"nav_step_{context.step_index:04d}", benchmark)

            if not args.capture_during_nav:
                return
            if context.primitive_name != "NAVIGATE_TO":
                return

            should_track_video = args.save_video and context.step_index % video_every == 0
            should_save_png = args.save_nav_images and context.step_index % png_every == 0
            if not should_track_video and not should_save_png:
                return

            sensor_name, rgb, raw_shape = get_robot_rgb(robot, args.output_size)
            shape = rgb.shape
            if should_track_video:
                benchmark.tracker.track_video_rgb(rgb)
            if should_save_png:
                save_path = output_dir / f"nav_step_{context.step_index:04d}.png"
                Image.fromarray(rgb).save(save_path)
                print(
                    f"saved {save_path.name} from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
                )

        benchmark.executor.step_callback = wrapped_step_callback

        plan = f"NAVIGATE_TO({args.navigate_target})"
        print("plan:", plan)
        benchmark.execute_plan(plan)
        if benchmark.tracker.error_stack:
            print("latest_error:", benchmark.tracker.error_stack[-1])

        if args.force_scene_graph_after_nav:
            try:
                benchmark._refresh_scene_graph(force=True)
                print_scene_graph_summary("after_nav_forced", benchmark)
            except Exception as e:
                benchmark.tracker.track_error(
                    action="force_scene_graph_after_nav",
                    err_type=e.__class__.__name__,
                    msg=str(e),
                )
                print(f"force_scene_graph_after_nav failed: {e.__class__.__name__}: {e}")

    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "fpv_after.png", args.output_size
    )
    end_pos = robot.get_position_orientation()[0].tolist()
    print("after:", end_pos)
    print(
        f"saved fpv_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    sensor_name, raw_shape, shape = save_obs_rgb(
        robot, output_dir / "obs_rgb_after.png", args.output_size
    )
    print(
        f"saved obs_rgb_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    if args.save_video:
        track_robot_rgb_video(robot, benchmark.tracker, args.output_size)
        save_video_if_requested(args, benchmark, output_dir)
    print_scene_graph_summary("final", benchmark)
    save_scene_graph_json_if_requested(args, benchmark, output_dir)
    print("scene_graph_history:", len(benchmark.tracker.scene_graph_history))

    sys.stdout.flush()
    sys.stderr.flush()
    if args.clear_on_exit:
        og.clear()
        return

    # This is a one-shot smoke test. On some Isaac Sim / OmniGibson builds,
    # og.clear() tears down the USD stage while viewport callbacks still point
    # at /World/viewer_camera, causing a native segfault after the test already
    # succeeded. Skip Python / Kit teardown for the stable smoke-test path.
    os._exit(0)


if __name__ == "__main__":
    main()
