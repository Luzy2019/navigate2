import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python
from og_ego_prim.primitives.specs import get_valid_primitives
from og_ego_prim.scene_graph.schema import (
    SCENE_GRAPH_SCHEMA_VERSION,
    scene_graph_report,
)
from og_ego_prim.utils.task_registry import get_task_config_path


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
            "Run a full fixed or AgentPlanner-generated task sequence while "
            "saving the same FPV/video/scene-graph artifacts as scripts/test.py."
        )
    )
    parser.add_argument("--task", default="store_apple_and_tissue_box_in_bottom_cabinet")
    parser.add_argument(
        "--scene",
        default=None,
        help="Scene model. Defaults to scene_info.default_scene_model for the task.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "AgentPlanner model name. When omitted, execute the task's fixed "
            "example_planning sequence."
        ),
    )
    parser.add_argument(
        "--local-llm-serve",
        action="store_true",
        help="Use the OpenAI-compatible local model server for AgentPlanner.",
    )
    parser.add_argument("--local-serve-ip", default="")
    parser.add_argument("--local-serve-key", default="sk-123456")
    parser.add_argument(
        "--work-dir",
        default=None,
        help=(
            "AgentPlanner work directory. Defaults to OUTPUT_DIR/planner_work_dir. "
            "The agent's multi-view observations are saved below its benchmark directory."
        ),
    )
    parser.add_argument(
        "--primitive-type",
        choices=("auto", "ego", "starter", "symbolic"),
        default="auto",
        help="Primitive set used by both the benchmark and AgentPlanner.",
    )
    parser.add_argument(
        "--prompt-setting",
        choices=("default", "v0", "v1", "v2", "v3"),
        default="default",
        help="AgentPlanner prompt variant.",
    )
    parser.add_argument(
        "--use-initial-setup",
        action=BooleanOptionalAction,
        default=False,
        help="Include the task's initial setup text in AgentPlanner prompts.",
    )
    parser.add_argument(
        "--use-self-caption",
        action=BooleanOptionalAction,
        default=False,
        help="Generate a visual scene caption before AgentPlanner starts planning.",
    )
    parser.add_argument(
        "--planner-use-obs",
        action=BooleanOptionalAction,
        default=True,
        help="Provide saved multi-view observations to AgentPlanner.",
    )
    parser.add_argument(
        "--planner-debug",
        action="store_true",
        help="Pause for confirmation before each AgentPlanner model request.",
    )
    parser.add_argument(
        "--plan-max-steps",
        type=int,
        default=None,
        help="Maximum generated planning steps. Defaults to example plan length plus 10.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Base directory for saved first-person RGB images, videos, and reports. "
            "Defaults to outputs/test_TASK_full. A run timestamp is appended by default."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate task JSON, BDDL, scene, objects, and example actions without launching OmniGibson.",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize OmniGibson and the task, then exit before executing the plan.",
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
    parser.add_argument("--video-fps", type=float, default=30.0)
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
        default=os.environ.get("ISBENCH_SCENE_GRAPH_BACKEND", "samjam_unigoal"),
        choices=[
            "omnigibson_truth",
            "truth",
            "unigoal_grounded_sam",
            "samjam_sam2",
            "samjam_unigoal",
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
            "Run perception every N scene graph update calls. The default 1 updates "
            "perception on every scheduled scene graph refresh."
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
        default=parse_output_size(os.environ.get("ISBENCH_SCENE_GRAPH_IMAGE_SIZE", "512x512")),
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
        action=BooleanOptionalAction,
        default=False,
        help="Show robot in viewer-style captures.",
    )
    parser.add_argument(
        "--save-topdown-scene",
        action=BooleanOptionalAction,
        default=False,
        help="Save a clean top-down RGB scene render at the end of the simulation.",
    )
    parser.add_argument(
        "--save-sampled-scene",
        action=BooleanOptionalAction,
        default=False,
        help="Save the sampled OmniGibson task scene JSON after initialization.",
    )
    parser.add_argument(
        "--sampled-scene-dir",
        default=None,
        help=(
            "Directory for --save-sampled-scene. Defaults to "
            "data/scenes/<scene>/json."
        ),
    )
    parser.add_argument(
        "--topdown-only",
        action="store_true",
        help=(
            "Initialize the task, save one clean top-down RGB scene render, then exit "
            "before planning/execution. Implies --save-topdown-scene and disables the "
            "scene graph backend."
        ),
    )
    parser.add_argument(
        "--topdown-capture-stage",
        choices=["initial", "final", "both"],
        default="final",
        help=(
            "When --save-topdown-scene is enabled, choose whether to capture before "
            "planning, after execution, or at both points."
        ),
    )
    parser.add_argument(
        "--topdown-world-bounds",
        nargs=4,
        type=float,
        metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y"),
        default=None,
        help="World-space ROI for the top-down scene render.",
    )
    parser.add_argument(
        "--topdown-output-size",
        type=parse_output_size,
        default=parse_output_size("1920x1080"),
        help="Top-down RGB output size.",
    )
    parser.add_argument(
        "--topdown-camera-height",
        type=float,
        default=None,
        help="Camera height above the current floor. Auto-scales from ROI when omitted.",
    )
    parser.add_argument(
        "--topdown-yaw-deg",
        type=float,
        default=0.0,
        help="Rotate the top-down camera around the vertical axis.",
    )
    parser.add_argument(
        "--topdown-camera-quat",
        nargs=4,
        type=float,
        metavar=("X", "Y", "Z", "W"),
        default=None,
        help="Override the top-down camera orientation quaternion.",
    )
    parser.add_argument(
        "--topdown-focal-length",
        type=float,
        default=17.0,
        help="Viewer camera focal length for top-down scene render.",
    )
    parser.add_argument(
        "--topdown-margin",
        type=float,
        default=1.0,
        help="Margin in meters when top-down bounds are inferred.",
    )
    parser.add_argument(
        "--topdown-settle-steps",
        type=int,
        default=5,
        help="Simulator steps after moving the top-down camera before capturing.",
    )
    parser.add_argument(
        "--topdown-show-robot",
        action=BooleanOptionalAction,
        default=False,
        help="Show the robot in the clean top-down scene render.",
    )
    args = parser.parse_args()
    if args.local_llm_serve and not args.model:
        parser.error("--local-llm-serve requires --model")
    if args.plan_max_steps is not None and args.plan_max_steps <= 0:
        parser.error("--plan-max-steps must be greater than zero")
    return args


ARGS = parse_args()


def validate_and_normalize_task(args):
    task_config_path = get_task_config_path(args.task)
    with task_config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    for key in ("_base_config", "task_info", "scene_info", "planning_context", "evaluation_goal_conditions"):
        if key not in config:
            raise ValueError(f"missing required task config key: {key}")

    task_info = config["task_info"]
    task_name = task_info["task_name"]
    primitive_type = task_info.get("primitive_type", "starter") # { ego, starter, symbolic }
    valid_primitives = get_valid_primitives(primitive_type)

    scene_info = config["scene_info"]
    if args.scene is None:
        args.scene = scene_info.get("default_scene_model")
    if not args.scene:
        raise ValueError("--scene is required because the task has no default_scene_model")
    if args.scene not in scene_info["scene_models"]:
        raise ValueError(
            f'scene "{args.scene}" is not supported; expected one of {scene_info["scene_models"]}'
        )

    planning_context = config["planning_context"]
    for key in ("task_instruction", "initial_setup", "goal_condition", "object_list", "object_abilities", "wash_rules"):
        if key not in planning_context:
            raise ValueError(f"missing required planning_context key: {key}")

    goal_conditions = config["evaluation_goal_conditions"]
    for key in (
        "process_safety_goal_condition",
        "termination_safety_goal_condition",
        "execution_goal_condition",
    ):
        if key not in goal_conditions:
            raise ValueError(f"missing required evaluation_goal_conditions key: {key}")

    action_pattern = re.compile(r"([A-Za-z_]+)\((.*)\)")
    for index, plan in enumerate(config.get("example_planning", []), start=1):
        action = plan.get("action", "").strip()
        if action.upper() == "DONE":
            continue
        match = action_pattern.fullmatch(action)
        if match is None:
            raise ValueError(f"invalid example_planning action #{index}: {action!r}")
        operator, raw_params = match.groups()
        operator = operator.upper()
        if operator not in valid_primitives:
            raise ValueError(
                f"example_planning action #{index} uses {operator}, which is not available for {primitive_type}"
            )
        params = [] if not raw_params.strip() else [item.strip() for item in raw_params.split(",")]
        expected = valid_primitives[operator]
        if len(params) != expected:
            raise ValueError(
                f"example_planning action #{index} {operator} expects {expected} params, got {len(params)}"
            )

    bddl_path = REPO_ROOT / "data" / "bddl" / task_name / f'problem{task_info["activity_definition_id"]}.bddl'
    if not bddl_path.is_file():
        raise FileNotFoundError(f"missing BDDL problem file: {bddl_path}")

    from bddl.activity import Conditions, get_goal_conditions, get_initial_conditions, get_object_scope
    from bddl.bddl_verification import TrivialBackend
    from bddl.condition_evaluation import compile_state
    from bddl.parsing import package_predicates, scan_tokens

    conditions = Conditions(
        task_name,
        task_info["activity_definition_id"],
        "omnigibson",
        predefined_problem=bddl_path.read_text(encoding="utf-8"),
    )
    scope = get_object_scope(conditions)
    backend = TrivialBackend()
    get_initial_conditions(conditions, backend, scope, generate_ground_options=False)
    get_goal_conditions(conditions, backend, scope, generate_ground_options=False)

    embedded_goals = [
        (f"process_safety_goal_condition[{index}]", item["safety_bddl"])
        for index, item in enumerate(goal_conditions["process_safety_goal_condition"])
    ]
    embedded_goals.extend(
        (f"termination_safety_goal_condition[{index}]", item["safety_bddl"])
        for index, item in enumerate(goal_conditions["termination_safety_goal_condition"])
    )
    if goal_conditions["execution_goal_condition"]:
        embedded_goals.append(
            ("execution_goal_condition", goal_conditions["execution_goal_condition"])
        )

    for label, goal_bddl in embedded_goals:
        tokens = scan_tokens(string=goal_bddl)
        if not tokens or tokens[0] != ":goal":
            raise ValueError(f"{label} is not a BDDL :goal expression")
        parsed_goals = []
        package_predicates(tokens[1], parsed_goals, "", "goals")
        try:
            compile_state(
                parsed_goals,
                backend,
                scope=scope,
                object_map=conditions.parsed_objects,
                generate_ground_options=False,
            )
        except Exception as exc:
            raise ValueError(f"failed to compile {label}: {exc}") from exc

    bddl_objects = set(scope)
    configured_objects = set(planning_context["object_list"])
    if bddl_objects != configured_objects:
        missing = sorted(bddl_objects - configured_objects)
        extra = sorted(configured_objects - bddl_objects)
        raise ValueError(
            f"planning_context.object_list differs from BDDL (missing={missing}, extra={extra})"
        )

    args.task = task_name
    if args.output_dir is None:
        args.output_dir = f"outputs/test_{task_name}_full"

    return task_config_path, bddl_path, primitive_type, len(bddl_objects)


try:
    TASK_CONFIG_PATH, BDDL_PATH, TASK_PRIMITIVE_TYPE, TASK_OBJECT_COUNT = validate_and_normalize_task(ARGS)
except Exception as exc:
    raise SystemExit(f"task preflight failed: {exc}") from exc

if ARGS.validate_only or os.environ.get("ISBENCH_OMNIGIBSON_X11_FIX") == "1":
    print(
        f"task preflight passed: task={ARGS.task} scene={ARGS.scene} "
        f"primitive={TASK_PRIMITIVE_TYPE} objects={TASK_OBJECT_COUNT} "
        f"config={TASK_CONFIG_PATH} bddl={BDDL_PATH}"
    )

if ARGS.validate_only:
    raise SystemExit(0)

if ARGS.topdown_only:
    ARGS.save_topdown_scene = True
    ARGS.topdown_capture_stage = "initial"
    ARGS.scene_graph_backend = "disabled"
    ARGS.save_video = False
    ARGS.capture_during_actions = False
    ARGS.save_step_images = False
    ARGS.save_surrounding_observations = False
    ARGS.planner_use_obs = False

if ARGS.headless:
    os.environ["OMNIGIBSON_HEADLESS"] = "1"

if not hasattr(ARGS, "scene_graph_update_every"):
    ARGS.scene_graph_update_every = 1
if ARGS.scene_graph_update_every <= 0:
    raise SystemExit("--scene-graph-update-every must be greater than zero")

os.environ["ISBENCH_SCENE_GRAPH_BACKEND"] = ARGS.scene_graph_backend
os.environ["ISBENCH_SCENE_GRAPH_UPDATE_EVERY"] = str(ARGS.scene_graph_update_every)
os.environ["ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL"] = str(ARGS.scene_graph_history_interval)

if ARGS.scene_graph_image_size is not None:
    os.environ["ISBENCH_SCENE_GRAPH_IMAGE_WIDTH"] = str(ARGS.scene_graph_image_size[0])
    os.environ["ISBENCH_SCENE_GRAPH_IMAGE_HEIGHT"] = str(ARGS.scene_graph_image_size[1])
if ARGS.nav_stuck_waypoint_tolerance is not None:
    os.environ["ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE"] = str(ARGS.nav_stuck_waypoint_tolerance)
if ARGS.nav_stuck_final_waypoint_tolerance is not None:
    os.environ["ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE"] = str(ARGS.nav_stuck_final_waypoint_tolerance)
if ARGS.nav_goal_clearance_radius is not None:
    os.environ["ISBENCH_NAV_GOAL_CLEARANCE_RADIUS"] = str(ARGS.nav_goal_clearance_radius)
if ARGS.nav_max_floor_height_delta is not None:
    os.environ["ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA"] = str(ARGS.nav_max_floor_height_delta)

maybe_reexec_with_omnigibson_python()

# Isaac Sim will otherwise receive this script's CLI args and try to interpret them.
sys.argv = [sys.argv[0]]

from og_ego_prim.utils.monkey_patch import add_monkey_patch

add_monkey_patch()

import numpy as np
import omnigibson as og
from omnigibson.macros import gm
from PIL import Image, ImageDraw

from og_ego_prim.benchmark import build_benchmark
from og_ego_prim.task_planner import AgentPlanner

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

    rooms = snapshot.get("rooms", [])
    if rooms:
        summary = snapshot.get("summary", {})
        groups = [
            group
            for room in rooms
            for group in room.get("groups", [])
            if isinstance(group, dict)
        ]
        nodes = [
            node
            for room in rooms
            for node in room.get("nodes", [])
            if isinstance(node, dict)
        ]
        nodes.extend(
            node
            for group in groups
            for node in group.get("nodes", [])
            if isinstance(node, dict)
        )
        edges = [
            edge
            for room in rooms
            for edge in room.get("edges", [])
            if isinstance(edge, dict)
        ]
        edges.extend(
            edge
            for group in groups
            for edge in group.get("edges", [])
            if isinstance(edge, dict)
        )
        object_ids = {node.get("id") for node in nodes if node.get("id") is not None}
        edge_keys = {
            (
                edge.get("source"),
                edge.get("target"),
                edge.get("type"),
                edge.get("source_uid"),
                edge.get("target_uid"),
            )
            for edge in edges
        }
        return {
            "backend": summary.get("backend") or summary.get("perception_backend"),
            "global_step_index": summary.get("global_step_index"),
            "frame_index": summary.get("frame_index"),
            "objects": summary.get("objects", len(object_ids)),
            "rooms": summary.get("rooms", len(rooms)),
            "groups": summary.get("groups", len(groups)),
            "relations": summary.get("relations", len(edge_keys)),
            "membership_edges": summary.get("membership_edges", 0),
            "edges": summary.get("edges", len(edge_keys)),
            "skipped": summary.get("skipped"),
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
    if latest is None:
        latest = {"schema_version": SCENE_GRAPH_SCHEMA_VERSION, "rooms": []}
    report = scene_graph_report(
        task=args.task,
        scene=args.scene,
        scene_graph_backend=args.scene_graph_backend,
        scene_graph_step_interval=args.scene_graph_step_interval,
        scene_graph_update_every=args.scene_graph_update_every,
        latest_summary=summarize_scene_graph(latest),
        latest_scene_graph=latest,
        scene_graph_history=benchmark.tracker.scene_graph_history,
        error_stack=benchmark.tracker.error_stack,
        execution_diagnostics=benchmark.tracker.execution_diagnostics,
        termination=benchmark.tracker.termination,
        goal_condition=benchmark.tracker.goal_condition,
    )
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"saved scene graph json: {save_path}")


def save_scene_graph_visualization(args, benchmark, output_dir: Path):
    latest = benchmark.tracker.latest_scene_graph
    if latest is None:
        print("saved scene graph diagnostic BEV video: skipped because latest scene graph is empty")
        return
    try:
        history = benchmark.tracker.scene_graph_history
        task_room = task_room_from_args(args)
        scene_graph_debug_metadata = None
        diagnostic_video_metadata = None
        diagnostic_backend = str(args.scene_graph_backend).lower()
        if diagnostic_backend in {"unigoal_grounded_sam", "samjam_unigoal"}:
            from og_ego_prim.scene_graph.unigoal_debug_artifacts import (
                render_unigoal_debug_artifacts,
                save_scene_graph_diagnostic_videos,
            )

            if diagnostic_backend == "unigoal_grounded_sam":
                scene_graph_debug_metadata = render_unigoal_debug_artifacts(output_dir)
            diagnostic_video_metadata = save_scene_graph_diagnostic_videos(
                history,
                output_dir,
                latest_snapshot=latest,
                env=benchmark.env,
                execution_diagnostics=benchmark.tracker.execution_diagnostics,
                task_room=task_room,
            )
        else:
            print(
                "saved scene graph diagnostic BEV video: "
                "skipped (backend does not provide detection overlays)"
            )
        if diagnostic_video_metadata is not None:
            diagnostic_global = diagnostic_video_metadata.get("global") or {}
            diagnostic_task = diagnostic_video_metadata.get("task_scene") or {}
            if diagnostic_global.get("saved"):
                print(
                    "saved scene graph diagnostic BEV video: "
                    f"{output_dir / 'scene_graph_bev_diagnostic_history.mp4'}"
                )
            elif diagnostic_global:
                print(
                    "saved scene graph diagnostic BEV video: "
                    f"skipped ({diagnostic_global.get('reason')})"
                )
            if diagnostic_task.get("saved"):
                print(
                    "saved scene graph diagnostic task-scene BEV video: "
                    f"{output_dir / 'scene_graph_bev_task_scene_diagnostic_history.mp4'}"
                )
            elif diagnostic_task:
                print(
                    "saved scene graph diagnostic task-scene BEV video: "
                    f"skipped ({diagnostic_task.get('reason')})"
                )
        if scene_graph_debug_metadata is not None:
            print(
                "saved UniGoal debug artifacts: "
                f"frames={scene_graph_debug_metadata.get('rendered_frames')} "
                f"errors={len(scene_graph_debug_metadata.get('errors', []))}"
            )
    except Exception as e:
        benchmark.tracker.track_error(
            action="save_scene_graph_visualization",
            err_type=e.__class__.__name__,
            msg=str(e),
        )
        print(f"save_scene_graph_visualization failed: {e.__class__.__name__}: {e}")


def should_save_topdown_scene(args, stage: str) -> bool:
    if args.topdown_only:
        return stage == "initial"
    if not args.save_topdown_scene:
        return False
    capture_stage = getattr(args, "topdown_capture_stage", "final")
    return capture_stage == stage or capture_stage == "both"


def topdown_scene_stem(args, stage: str) -> str:
    if args.topdown_only:
        return "topdown_scene"
    capture_stage = getattr(args, "topdown_capture_stage", "final")
    if stage == "final" and capture_stage == "final":
        return "topdown_scene"
    return f"topdown_scene_{stage}"


def save_topdown_scene_if_requested(args, benchmark, output_dir: Path, stage: str = "final"):
    if not args.save_topdown_scene:
        return
    if not should_save_topdown_scene(args, stage):
        return
    output_stem = topdown_scene_stem(args, stage)
    image_path = output_dir / f"{output_stem}.png"
    metadata_path = output_dir / f"{output_stem}.json"
    output_size = args.topdown_output_size or parse_output_size("1920x1080")
    try:
        from og_ego_prim.utils.topdown_capture import capture_topdown_scene

        metadata = capture_topdown_scene(
            benchmark.env,
            image_path,
            world_bounds=args.topdown_world_bounds,
            snapshot=benchmark.tracker.latest_scene_graph,
            execution_diagnostics=benchmark.tracker.execution_diagnostics,
            output_size=output_size,
            camera_height=args.topdown_camera_height,
            yaw_degrees=args.topdown_yaw_deg,
            camera_quat=args.topdown_camera_quat,
            focal_length=args.topdown_focal_length,
            margin=args.topdown_margin,
            settle_steps=args.topdown_settle_steps,
            show_robot=args.topdown_show_robot,
            metadata_path=metadata_path,
        )
        print(
            f"saved clean top-down scene ({stage}): "
            f"{image_path} "
            f"bounds_source={metadata.get('bounds_source')}"
        )
    except Exception as e:
        benchmark.tracker.track_error(
            action=f"save_topdown_scene_{stage}",
            err_type=e.__class__.__name__,
            msg=str(e),
        )
        print(f"save_topdown_scene ({stage}) failed: {e.__class__.__name__}: {e}")


def save_sampled_scene_if_requested(args, benchmark):
    if not getattr(args, "save_sampled_scene", False):
        return
    try:
        scene_name = benchmark.scene_name
        task_name = benchmark.task_name
        fname = f"{scene_name}_task_{task_name}_0_0_template.json"
        if args.sampled_scene_dir:
            scene_dir = Path(args.sampled_scene_dir)
        else:
            scene_dir = REPO_ROOT / "data" / "scenes" / scene_name / "json"
        scene_dir.mkdir(parents=True, exist_ok=True)
        scene_path = scene_dir / fname
        benchmark.env.task.save_task(path=str(scene_path), override=True)
        print(f"saved sampled task scene: {scene_path}", flush=True)
    except Exception as e:
        benchmark.tracker.track_error(
            action="save_sampled_scene",
            err_type=e.__class__.__name__,
            msg=str(e),
        )
        print(f"save_sampled_scene failed: {e.__class__.__name__}: {e}")


def task_room_from_args(args):
    try:
        with get_task_config_path(args.task).open("r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return None
    return config.get("scene_info", {}).get("room")


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
    print(f"saved AgentPlanner observations: {save_path}")


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


PERCEPTION_SCENE_GRAPH_BACKENDS = {
    "unigoal_grounded_sam",
    "samjam_sam2",
    "samjam_unigoal",
}


def is_perception_scene_graph_backend(backend_name: str) -> bool:
    return str(backend_name or "").lower() in PERCEPTION_SCENE_GRAPH_BACKENDS


def scene_graph_output_dir_for_backend(
    backend_name: str,
    base_dir: Path,
    *,
    final: bool = False,
) -> Path:
    if final:
        base_dir = base_dir / "final_scene_graph"
    backend_name = str(backend_name or "").lower()
    if backend_name in {"samjam_sam2", "samjam_unigoal"}:
        return base_dir / "samjam_outputs"
    return base_dir / "scene_graph_outputs"


def configure_scene_graph_output_env(backend_name: str, output_dir: Path) -> bool:
    if not is_perception_scene_graph_backend(backend_name):
        return False

    os.environ["ISBENCH_SCENE_GRAPH_OUTPUT_DIR"] = str(output_dir)
    if str(backend_name).lower() in {"samjam_sam2", "samjam_unigoal"}:
        os.environ["ISBENCH_SAMJAM_OUTPUT_DIR"] = str(output_dir)

    debug_flag_names = (
        "ISBENCH_SCENE_GRAPH_DEBUG_MATCHING",
        "ISBENCH_SAMJAM_DEBUG_MATCHING",
        "ISBENCH_UNIGOAL_MAPPING_DEBUG",
        "ISBENCH_SAMJAM_UNIGOAL_DEBUG_MAPPING",
        "ISBENCH_UNIGOAL_DEBUG_MATCHING",
        "ISBENCH_UNIGOAL_GROUNDED_SAM_DEBUG",
    )
    if not os.environ.get("ISBENCH_SCENE_GRAPH_DEBUG_LOG_PATH") and all(
        os.environ.get(name) is None for name in debug_flag_names
    ):
        os.environ["ISBENCH_SCENE_GRAPH_DEBUG_MATCHING"] = "1"

    debug_enabled = bool(os.environ.get("ISBENCH_SCENE_GRAPH_DEBUG_LOG_PATH")) or any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in debug_flag_names
    )
    if debug_enabled:
        debug_log_path = Path(
            os.environ.get("ISBENCH_SCENE_GRAPH_DEBUG_LOG_PATH")
            or output_dir / "scene_graph_debug.log"
        )
        debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "[ISBench][SceneGraphDebug] "
                f"enabled backend={backend_name} output_dir={output_dir}\n"
            )
    return True


def set_scene_graph_output_dir(benchmark, output_dir: Path):
    updater = getattr(benchmark, "scene_graph_updater", None)
    backend_name = getattr(
        updater,
        "backend_name",
        os.environ.get("ISBENCH_SCENE_GRAPH_BACKEND", ""),
    )
    configured = configure_scene_graph_output_env(backend_name, output_dir)
    backend = getattr(updater, "backend", None)
    if backend is None:
        return configured

    target_backend = getattr(backend, "samjam_backend", backend)
    if not hasattr(target_backend, "output_writer"):
        return configured

    from og_ego_prim.scene_graph.backends.samjam_sam2 import SAMJAMOutputWriter

    target_backend.output_writer = SAMJAMOutputWriter(output_dir)
    return True


def disable_scene_graph_artifact_output(benchmark):
    updater = getattr(benchmark, "scene_graph_updater", None)
    backend = getattr(updater, "backend", None)
    if backend is None:
        return False

    target_backend = getattr(backend, "samjam_backend", backend)
    if hasattr(target_backend, "output_writer"):
        target_backend.output_writer = None
        return True
    return False


def capture_robot_rgb_frame(robot, output_size):
    return get_robot_rgb(robot, output_size)[1]


def annotate_navigation_landmarks(rgb, robot, navigation_result):
    """Overlay a robot-centric waypoint / target HUD on recorded video frames."""
    if not isinstance(navigation_result, dict):
        return rgb
    target = navigation_result.get("goal_pose_2d")
    if not isinstance(target, (list, tuple)) or len(target) < 2:
        return rgb

    robot_pos, robot_orn = robot.get_position_orientation()
    robot_xy = np.asarray(robot_pos[:2].detach().cpu(), dtype=float)
    quat = np.asarray(robot_orn.detach().cpu(), dtype=float)
    x, y, z, w = quat
    robot_yaw = float(
        np.arctan2(
            2.0 * (w * z + x * y),
            1.0 - 2.0 * (y * y + z * z),
        )
    )

    target_xy = np.asarray(target[:2], dtype=float)
    waypoint = navigation_result.get("current_waypoint_pose_2d")
    waypoint_xy = (
        None
        if not isinstance(waypoint, (list, tuple)) or len(waypoint) < 2
        else np.asarray(waypoint[:2], dtype=float)
    )
    executed_waypoints = [
        np.asarray(item[:2], dtype=float)
        for item in navigation_result.get("executed_waypoints_2d", [])
        if isinstance(item, (list, tuple)) and len(item) >= 2
    ]

    frame = np.asarray(rgb)
    image = Image.fromarray(frame[:, :, :3].copy()).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    panel_size = max(108, min(180, int(min(width, height) * 0.36)))
    left, top = 8, 8
    right, bottom = left + panel_size, top + panel_size
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=8,
        fill=(0, 0, 0, 150),
        outline=(255, 255, 255, 180),
        width=1,
    )
    center = np.asarray(
        [(left + right) / 2.0, (top + bottom) / 2.0 + 8.0],
        dtype=float,
    )

    points = [target_xy, *executed_waypoints]
    if waypoint_xy is not None:
        points.append(waypoint_xy)
    max_distance = max(
        [float(np.linalg.norm(point - robot_xy)) for point in points] + [1.0]
    )
    scale = (panel_size * 0.38) / max_distance
    cos_yaw, sin_yaw = np.cos(robot_yaw), np.sin(robot_yaw)

    def project(world_xy):
        delta = world_xy - robot_xy
        forward = cos_yaw * delta[0] + sin_yaw * delta[1]
        leftward = -sin_yaw * delta[0] + cos_yaw * delta[1]
        return (
            float(center[0] - leftward * scale),
            float(center[1] - forward * scale),
        )

    route_points = [project(point) for point in executed_waypoints]
    if len(route_points) >= 2:
        draw.line(route_points, fill=(120, 255, 120, 170), width=2)
    for point in route_points:
        draw.ellipse(
            (point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2),
            fill=(120, 255, 120, 190),
        )

    target_px = project(target_xy)
    draw.ellipse(
        (
            target_px[0] - 7,
            target_px[1] - 7,
            target_px[0] + 7,
            target_px[1] + 7,
        ),
        outline=(255, 70, 70, 255),
        width=3,
    )
    draw.line(
        (target_px[0] - 5, target_px[1], target_px[0] + 5, target_px[1]),
        fill=(255, 70, 70, 255),
        width=2,
    )
    draw.line(
        (target_px[0], target_px[1] - 5, target_px[0], target_px[1] + 5),
        fill=(255, 70, 70, 255),
        width=2,
    )

    waypoint_distance = None
    if waypoint_xy is not None:
        waypoint_px = project(waypoint_xy)
        draw.ellipse(
            (
                waypoint_px[0] - 5,
                waypoint_px[1] - 5,
                waypoint_px[0] + 5,
                waypoint_px[1] + 5,
            ),
            fill=(80, 255, 100, 255),
            outline=(255, 255, 255, 230),
            width=1,
        )
        waypoint_distance = float(np.linalg.norm(waypoint_xy - robot_xy))

    robot_triangle = [
        (center[0], center[1] - 8),
        (center[0] - 6, center[1] + 6),
        (center[0] + 6, center[1] + 6),
    ]
    draw.polygon(robot_triangle, fill=(60, 170, 255, 255))
    target_distance = float(np.linalg.norm(target_xy - robot_xy))
    waypoint_text = "--" if waypoint_distance is None else f"{waypoint_distance:.2f}m"
    draw.text(
        (left + 6, top + 5),
        f"TARGET {target_distance:.2f}m  WP {waypoint_text}",
        fill=(255, 255, 255, 255),
    )
    draw.text(
        (left + 6, bottom - 16),
        "red=target  green=waypoint",
        fill=(220, 220, 220, 230),
    )
    return np.asarray(Image.alpha_composite(image, overlay).convert("RGB"))


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
        online_object_sampling=None,
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

    save_topdown_scene_if_requested(args, benchmark, output_dir, stage="initial")
    save_sampled_scene_if_requested(args, benchmark)
    if args.topdown_only:
        print(
            f"topdown-only complete: task={benchmark.task_name} "
            f"scene={benchmark.scene_name}"
        )
        sys.stdout.flush()
        sys.stderr.flush()
        benchmark.close()
        if args.clear_on_exit:
            og.clear()
            return
        os._exit(0)

    if args.init_only:
        print(
            f"task initialization passed: task={benchmark.task_name} "
            f"scene={benchmark.scene_name} primitive={benchmark.primitive_type}"
        )
        sys.stdout.flush()
        sys.stderr.flush()
        benchmark.close()
        if args.clear_on_exit:
            og.clear()
            return
        os._exit(0)

    robot = benchmark.env.robots[0]
    print("before:", robot.get_position_orientation()[0].tolist())
    
    # First Person View (FPV) image before executing any plan
    sensor_name, raw_shape, shape = save_robot_rgb(
        robot, output_dir / "fpv_before.png", args.output_size
    )
    print(
        f"saved fpv_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    )
    # sensor_name, raw_shape, shape = save_robot_rgb(
    #     robot, output_dir / "obs_rgb_before.png", args.output_size
    # )
    # print(
    #     f"saved obs_rgb_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    # )
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

        agent = AgentPlanner(
            task_name=args.task,
            scene_name=args.scene,
            model_name=args.model,
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
            f"planning_source: AgentPlanner model={args.model} "
            f"max_steps={max_steps} obs={args.planner_use_obs}"
        )
    else:
        planner = benchmark.get_example_planning()
        print("planning_source: fixed example_planning")

    original_step_callback = benchmark.executor.step_callback
    capture_every = max(args.capture_every, 1)
    current_step_frames = None
    current_step_action = None

    def current_navigation_result():
        controller = getattr(benchmark.executor, "controller", None)
        navigation_backend = getattr(controller, "navigation_backend", None)
        return getattr(navigation_backend, "last_navigation_result", None)

    def annotate_current_navigation_frame(rgb):
        if not current_step_action or not current_step_action.lower().startswith(
            "navigate_to("
        ):
            return rgb
        return annotate_navigation_landmarks(
            rgb,
            robot,
            current_navigation_result(),
        )

    def wrapped_step_callback(context):
        if original_step_callback is not None:
            original_step_callback(context)
        if not args.capture_during_actions or not args.save_video:
            return
        if context.step_index % capture_every != 0:
            return
        rgb = capture_robot_rgb_frame(robot, args.output_size)
        rgb = annotate_current_navigation_frame(rgb)
        benchmark.tracker.track_video_rgb(rgb)
        if current_step_frames is not None:
            current_step_frames.append(rgb)

    benchmark.executor.step_callback = wrapped_step_callback

    for step_index, plan in enumerate(planner, start=1):
        action = plan["action"]
        current_step_action = action

        # ========================= log start =========================
        step_dir = output_dir / safe_step_dir_name(step_index, action)
        step_dir.mkdir(parents=True, exist_ok=True)
        if is_perception_scene_graph_backend(args.scene_graph_backend):
            set_scene_graph_output_dir(
                benchmark,
                scene_graph_output_dir_for_backend(args.scene_graph_backend, step_dir),
            )
        print(f"plan_step_{step_index:02d}: {action}")

        current_step_frames = []
        sensor_name, raw_shape, shape, before_rgb = save_robot_rgb_with_frame(
            robot, step_dir / "obs_before.png", args.output_size
        )
        current_step_frames.append(before_rgb)
        print(
            f"saved {step_dir.name}/obs_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
        )

        # ========================= plan start =========================
        execution_succeeded = benchmark.execute_plan(plan)


        sensor_name, raw_shape, shape, after_rgb = save_robot_rgb_with_frame(
            robot, step_dir / "obs_after.png", args.output_size
        )
        video_after_rgb = annotate_current_navigation_frame(after_rgb)
        current_step_frames.append(video_after_rgb)
        print(
            f"saved {step_dir.name}/obs_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
        )
        if args.save_video:
            benchmark.tracker.track_video_rgb(video_after_rgb)
            # video_info = save_rgb_video(
            #     current_step_frames,
            #     step_dir / "nav_rgb.mp4",
            #     args.video_fps,
            # )
            # if video_info is not None:
            #     shutil.copyfile(step_dir / "nav_rgb.mp4", step_dir / "video.mp4")
            #     print(
            #         f"saved {step_dir.name}/nav_rgb.mp4 and {step_dir.name}/video.mp4 "
            #         f"({video_info['frames']} frames, {video_info['fps']} fps)"
            #     )
        current_step_frames = None
        current_step_action = None

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
    if is_perception_scene_graph_backend(args.scene_graph_backend):
        disable_scene_graph_artifact_output(benchmark)
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
    # sensor_name, raw_shape, shape = save_robot_rgb(
    #     robot, output_dir / "obs_rgb_after.png", args.output_size
    # )
    # print(
    #     f"saved obs_rgb_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
    # )

    save_topdown_scene_if_requested(args, benchmark, output_dir, stage="final")
    save_scene_graph_visualization(args, benchmark, output_dir)
    save_report_and_video(args, benchmark, output_dir)
    save_scene_graph_report(args, benchmark, output_dir)
    print("scene_graph_history:", len(benchmark.tracker.scene_graph_history))

    sys.stdout.flush()
    sys.stderr.flush()
    benchmark.close()
    if args.clear_on_exit:
        og.clear()
        return
    os._exit(0)


if __name__ == "__main__":
    main()
