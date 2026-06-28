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
from og_ego_prim.utils.task_registry import get_task_config_path


class TeeTextStream:
    def __init__(self, terminal_stream, log_stream):
        self.terminal_stream = terminal_stream
        self.log_stream = log_stream

    def write(self, text):
        self.terminal_stream.write(text)
        self.log_stream.write(text)
        self.flush()
        return len(text)

    def flush(self):
        self.terminal_stream.flush()
        self.log_stream.flush()

    def __getattr__(self, name):
        return getattr(self.terminal_stream, name)


def install_run_log(output_dir):
    log_path = output_dir / "run.log"
    log_stream = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TeeTextStream(sys.stdout, log_stream)
    sys.stderr = TeeTextStream(sys.stderr, log_stream)
    print(f"run log: {log_path.resolve()}")
    return log_stream


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


PARTICLE_LIKE_CATEGORY_KEYWORDS = (
    "water",
    "soap",
    "stain",
    "dust",
    "dirt",
    "oil",
    "disinfectant",
    "detergent",
    "bunchgrass",
)

SYMBOLIC_SMOKE_DIRECT_PRIMITIVES = {
    "OPEN",
    "CLOSE",
    "TOGGLE_ON",
    "TOGGLE_OFF",
}
SYMBOLIC_SMOKE_PLACEMENT_PRIMITIVES = {
    "PLACE_ON_TOP",
    "PLACE_INSIDE",
}
SYMBOLIC_SMOKE_TOOL_PRIMITIVES = {
    "SOAK_UNDER": (0, 1),
    "WIPE": (1, 0),
    "CUT": (1, 0),
}
SYMBOLIC_SMOKE_UNSUPPORTED_PRIMITIVES = {
    "FILL_WITH",
    "POUR_INTO",
    "SPREAD",
    "SOAK_INSIDE",
    "WAIT",
    "WAIT_FOR_COOKED",
    "WAIT_FOR_FROZEN",
    "WAIT_FOR_WASHED",
}
SYMBOLIC_SMOKE_UNSUPPORTED_REASONS = {
    "SOAK_INSIDE": (
        "native OmniGibson symbolic SOAK_INSIDE is incompatible with this "
        "installed FluidSystem API"
    ),
}
SYMBOLIC_SMOKE_ACTION_PATTERN = re.compile(r"^\s*([A-Za-z_]+)\s*\((.*)\)\s*$")

NEW_TASK_INROOM_OBJECT_OVERRIDES = {
    "lifelong__morning_kitchen_routine": {
        # This is the countertop used by the validated Wainscott pour-tea cache.
        "countertop.n.01_1": "countertop_tpuwys_5",
    },
}


def cli_option_present(*option_names):
    for arg in sys.argv[1:]:
        for option_name in option_names:
            if arg == option_name or arg.startswith(f"{option_name}="):
                return True
    return False


def parse_bddl_object_categories(bddl_path):
    object_categories = {}
    in_objects = False
    for raw_line in bddl_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("(:objects"):
            in_objects = True
            continue
        if not in_objects:
            continue
        if line.startswith(")"):
            break
        if " - " not in line:
            continue
        names_text, category = line.split(" - ", 1)
        category = category.strip()
        for object_name in names_text.split():
            object_categories[object_name.strip()] = category
    return object_categories


def get_task_resource_profile(config, bddl_path):
    object_categories = parse_bddl_object_categories(bddl_path)
    bddl_text = bddl_path.read_text(encoding="utf-8")
    substance_objects = set(
        match.group(2)
        for match in re.finditer(
            r"\((covered|filled|insource|saturated)\s+[^\s()]+\s+([^\s()]+)",
            bddl_text,
        )
    )
    particle_like_objects = sorted(
        object_name
        for object_name, category in object_categories.items()
        if object_name in substance_objects
        if any(keyword in category for keyword in PARTICLE_LIKE_CATEGORY_KEYWORDS)
    )
    particle_state_predicates = sorted(
        set(re.findall(r"\((covered|filled|insource|saturated)\s+", bddl_text))
    )
    return {
        "online_object_sampling": config["scene_info"].get("online_object_sampling"),
        "particle_like_objects": particle_like_objects,
        "particle_state_predicates": particle_state_predicates,
    }


def format_task_resource_profile(profile):
    particles = profile["particle_like_objects"]
    predicates = profile["particle_state_predicates"]
    particle_text = ",".join(particles) if particles else "none"
    predicate_text = ",".join(predicates) if predicates else "none"
    return (
        "task_resource_profile: "
        f"online_object_sampling={profile['online_object_sampling']} "
        f"particle_like_objects={particle_text} "
        f"particle_state_predicates={predicate_text}"
    )


def parse_plan_action(action):
    action = action.strip()
    if action.upper() == "DONE":
        return "DONE", []

    match = SYMBOLIC_SMOKE_ACTION_PATTERN.fullmatch(action)
    if match is None:
        raise ValueError(f"invalid action syntax: {action!r}")
    operator, raw_params = match.groups()
    params = [] if not raw_params.strip() else [part.strip() for part in raw_params.split(",")]
    return operator.upper(), params


def build_symbolic_smoke_plan(plans):
    """Convert ego syntax to the installed OmniGibson symbolic contract."""
    converted = []
    skipped = []
    inserted = []
    held_object = None
    tools_with_skipped_soak = set()

    def append_action(action, caution=None):
        converted.append({"action": action.lower(), "caution": caution})

    def release_if_holding(reason):
        nonlocal held_object
        if held_object is None:
            return
        append_action("release()")
        inserted.append(
            {
                "action": "RELEASE()",
                "reason": reason,
                "object": held_object,
            }
        )
        held_object = None

    def grasp_if_needed(object_name, reason):
        nonlocal held_object
        if held_object == object_name:
            return
        release_if_holding(f"switching held object before {reason}")
        append_action(f"grasp({object_name})")
        inserted.append(
            {
                "action": f"GRASP({object_name})",
                "reason": reason,
            }
        )
        held_object = object_name

    for index, plan in enumerate(plans, start=1):
        action = plan["action"]
        caution = plan.get("caution")
        operator, params = parse_plan_action(action)

        if operator == "DONE":
            append_action("done()", caution)
            continue

        if operator == "NAVIGATE_TO":
            skipped.append(
                {
                    "index": index,
                    "action": action,
                    "reason": "navigation disabled for symbolic smoke",
                }
            )
            continue

        if operator in SYMBOLIC_SMOKE_UNSUPPORTED_PRIMITIVES:
            if operator == "SOAK_INSIDE" and params:
                tools_with_skipped_soak.add(params[0])
            skipped.append(
                {
                    "index": index,
                    "action": action,
                    "reason": SYMBOLIC_SMOKE_UNSUPPORTED_REASONS.get(
                        operator,
                        "primitive is unavailable in OmniGibson symbolic mode",
                    ),
                }
            )
            continue

        if operator in SYMBOLIC_SMOKE_PLACEMENT_PRIMITIVES:
            if len(params) != 2:
                raise ValueError(f"{operator} expects source and target: {action!r}")
            source, target = params
            grasp_if_needed(source, f"preparing {operator}")
            append_action(f"{operator}({target})", caution)
            held_object = None
            continue

        if operator in SYMBOLIC_SMOKE_TOOL_PRIMITIVES:
            if len(params) != 2:
                raise ValueError(f"{operator} expects tool and target objects: {action!r}")
            tool_index, target_index = SYMBOLIC_SMOKE_TOOL_PRIMITIVES[operator]
            tool = params[tool_index]
            target = params[target_index]
            if operator == "WIPE" and tool in tools_with_skipped_soak:
                skipped.append(
                    {
                        "index": index,
                        "action": action,
                        "reason": (
                            "dependent WIPE skipped because the required "
                            f"SOAK_INSIDE setup for {tool} was skipped"
                        ),
                    }
                )
                continue
            grasp_if_needed(tool, f"preparing {operator}")
            append_action(f"{operator}({target})", caution)
            continue

        if operator == "GRASP":
            if len(params) != 1:
                raise ValueError(f"GRASP expects one object: {action!r}")
            grasp_if_needed(params[0], "preserving explicit GRASP")
            continue

        if operator == "RELEASE":
            release_if_holding("preserving explicit RELEASE")
            continue

        if operator in SYMBOLIC_SMOKE_DIRECT_PRIMITIVES:
            if len(params) != 1:
                raise ValueError(f"{operator} expects one object: {action!r}")
            release_if_holding(f"{operator} requires an empty hand")
            append_action(f"{operator}({params[0]})", caution)
            continue

        skipped.append(
            {
                "index": index,
                "action": action,
                "reason": "no safe symbolic smoke conversion is defined",
            }
        )

    return converted, skipped, inserted


def validate_symbolic_smoke_plan(plans):
    valid_primitives = get_valid_primitives("symbolic")
    for index, plan in enumerate(plans, start=1):
        operator, params = parse_plan_action(plan["action"])
        if operator == "DONE":
            continue
        if operator == "NAVIGATE_TO":
            raise ValueError("symbolic smoke plan must not contain NAVIGATE_TO")
        if operator not in valid_primitives:
            raise ValueError(
                f"symbolic smoke action #{index} uses unsupported primitive {operator}"
            )
        expected = valid_primitives[operator]
        if len(params) != expected:
            raise ValueError(
                f"symbolic smoke action #{index} {operator} expects {expected} params, "
                f"got {len(params)}"
            )


def print_symbolic_smoke_summary(original_count, converted, skipped, inserted):
    print(
        "symbolic_smoke_plan: "
        f"original_actions={original_count} "
        f"executable_actions={len(converted)} "
        f"inserted_actions={len(inserted)} "
        f"skipped_actions={len(skipped)} navigation_actions=0"
    )
    for item in skipped:
        print(
            "symbolic_smoke_skip: "
            f"source_step={item['index']} action={item['action']!r} "
            f"reason={item['reason']}"
        )


def get_available_ram_gb():
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.is_file():
        return None
    for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) / (1024 * 1024)
    return None


def get_gpu_memory_rows():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return None, "nvidia-smi not found"
    except subprocess.TimeoutExpired:
        return None, "nvidia-smi timed out"
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return None, error

    rows = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            rows.append(
                {
                    "index": int(parts[0]),
                    "free_mb": int(parts[1]),
                    "total_mb": int(parts[2]),
                }
            )
        except ValueError:
            continue
    if not rows:
        return None, "no parseable nvidia-smi rows"
    return rows, None


def run_resource_guard(args):
    if args.skip_resource_check:
        print("resource_guard: skipped by --skip-resource-check")
        return

    available_ram_gb = get_available_ram_gb()
    if available_ram_gb is None:
        print("resource_guard: RAM availability check unavailable")
    else:
        print(
            "resource_guard: available_ram={:.1f}GB min_required={:.1f}GB".format(
                available_ram_gb, args.min_free_ram_gb
            )
        )
        if args.min_free_ram_gb > 0 and available_ram_gb < args.min_free_ram_gb:
            raise SystemExit(
                "resource preflight failed: available RAM is below "
                f"{args.min_free_ram_gb:.1f}GB; refusing to launch OmniGibson"
            )

    gpu_rows, gpu_error = get_gpu_memory_rows()
    if gpu_rows is None:
        print(f"resource_guard: GPU memory check unavailable ({gpu_error})")
        if args.min_free_gpu_mb > 0:
            raise SystemExit(
                "resource preflight failed: GPU memory is unavailable; refusing to "
                "launch OmniGibson. Use --min-free-gpu-mb 0 only if you explicitly "
                "want to bypass this guard."
            )
        return

    gpu_text = ", ".join(
        f"gpu{row['index']}={row['free_mb']}/{row['total_mb']}MB free"
        for row in gpu_rows
    )
    print(f"resource_guard: {gpu_text} min_required={args.min_free_gpu_mb}MB")
    if args.min_free_gpu_mb > 0 and not any(
        row["free_mb"] >= args.min_free_gpu_mb for row in gpu_rows
    ):
        raise SystemExit(
            "resource preflight failed: no GPU has at least "
            f"{args.min_free_gpu_mb}MB free; refusing to launch OmniGibson"
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run a full fixed or PlanningAgent-generated task sequence while "
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
        "--symbolic-smoke",
        action="store_true",
        help=(
            "Run the fixed example plan with the installed OmniGibson symbolic "
            "primitive contract while skipping navigation. Legacy two-object "
            "actions are converted to GRASP plus a one-object symbolic action."
        ),
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
        "--low-resource",
        action="store_true",
        help=(
            "Use conservative defaults for new-task smoke tests: disable scene graph "
            "unless explicitly requested, disable video/action capture, and lower saved image size."
        ),
    )
    parser.add_argument(
        "--online-object-sampling",
        action=BooleanOptionalAction,
        default=None,
        help=(
            "Override scene_info.online_object_sampling. Leaving this unset uses "
            "the task JSON value."
        ),
    )
    parser.add_argument(
        "--task-relevant-only",
        action=BooleanOptionalAction,
        default=None,
        help=(
            "Load only task-relevant scene objects. This is off by default because "
            "online BDDL sampling still needs fixed scene candidates such as kitchen appliances."
        ),
    )
    parser.add_argument(
        "--exclude-object-categories",
        default=None,
        help=(
            "Comma-separated scene object categories to add to not_load_object_categories. "
            "Defaults to table_lamp for --init-only / --low-resource to avoid a known "
            "Wainscott table_lamp NaN transform during online sampling. Use 'none' to disable."
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
        help="Save robot FPV images before and after each high-level action.",
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
        "--disable-gpu-dynamics",
        action="store_true",
        help=(
            "Set gm.USE_GPU_DYNAMICS=False before creating the OmniGibson environment. "
            "This can reduce GPU pressure but may break particle-heavy tasks."
        ),
    )
    parser.add_argument(
        "--min-free-gpu-mb",
        type=int,
        default=int(os.environ.get("ISBENCH_MIN_FREE_GPU_MB", "4096")),
        help=(
            "Refuse to launch OmniGibson unless at least one GPU has this much free "
            "memory. Use 0 to disable this guard."
        ),
    )
    parser.add_argument(
        "--min-free-ram-gb",
        type=float,
        default=float(os.environ.get("ISBENCH_MIN_FREE_RAM_GB", "8")),
        help=(
            "Refuse to launch OmniGibson unless system MemAvailable is at least "
            "this many GB. Use 0 to disable this guard."
        ),
    )
    parser.add_argument(
        "--skip-resource-check",
        action="store_true",
        help="Skip RAM/GPU resource checks before launching OmniGibson.",
    )
    parser.add_argument(
        "--resource-check-only",
        action="store_true",
        help=(
            "Validate the task and run RAM/GPU resource checks, then exit before "
            "launching OmniGibson."
        ),
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
        action="store_true",
        help="Keep robot visible in viewer-style captures.",
    )
    args = parser.parse_args()
    if args.symbolic_smoke:
        if args.model:
            parser.error("--symbolic-smoke only supports the fixed example plan; omit --model")
        if args.primitive_type not in {"auto", "symbolic"}:
            parser.error(
                "--symbolic-smoke cannot be combined with "
                f"--primitive-type {args.primitive_type}"
            )
        args.primitive_type = "symbolic"
        args.low_resource = True
        if not cli_option_present("--stop-on-error", "--no-stop-on-error"):
            args.stop_on_error = True
    scene_graph_explicit = cli_option_present("--scene-graph-backend")
    if args.init_only and not scene_graph_explicit:
        args.scene_graph_backend = "disabled"
    if args.low_resource:
        if not scene_graph_explicit:
            args.scene_graph_backend = "disabled"
        if not cli_option_present("--save-video", "--no-save-video"):
            args.save_video = args.symbolic_smoke
        if not cli_option_present("--capture-during-actions", "--no-capture-during-actions"):
            args.capture_during_actions = args.symbolic_smoke
        if not cli_option_present("--save-step-images", "--no-save-step-images"):
            args.save_step_images = args.symbolic_smoke
        if not cli_option_present("--output-size"):
            args.output_size = parse_output_size("256x256")
    if args.task_relevant_only is None:
        args.task_relevant_only = False
    if args.exclude_object_categories is None:
        args.exclude_object_categories = (
            "table_lamp" if args.init_only or args.low_resource else ""
        )
    if args.exclude_object_categories.strip().lower() in {"", "none"}:
        args.exclude_object_categories = []
    else:
        args.exclude_object_categories = [
            category.strip()
            for category in args.exclude_object_categories.split(",")
            if category.strip()
        ]
    if args.local_llm_serve and not args.model:
        parser.error("--local-llm-serve requires --model")
    if args.plan_max_steps is not None and args.plan_max_steps <= 0:
        parser.error("--plan-max-steps must be greater than zero")
    if args.min_free_gpu_mb < 0:
        parser.error("--min-free-gpu-mb must be zero or greater")
    if args.min_free_ram_gb < 0:
        parser.error("--min-free-ram-gb must be zero or greater")
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
    primitive_type = task_info.get("primitive_type", "ego")
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

    return (
        task_config_path,
        bddl_path,
        primitive_type,
        len(bddl_objects),
        get_task_resource_profile(config, bddl_path),
    )


try:
    (
        TASK_CONFIG_PATH,
        BDDL_PATH,
        TASK_PRIMITIVE_TYPE,
        TASK_OBJECT_COUNT,
        TASK_RESOURCE_PROFILE,
    ) = validate_and_normalize_task(ARGS)
except Exception as exc:
    raise SystemExit(f"task preflight failed: {exc}") from exc

print(
    f"task preflight passed: task={ARGS.task} scene={ARGS.scene} "
    f"primitive={TASK_PRIMITIVE_TYPE} objects={TASK_OBJECT_COUNT} "
    f"config={TASK_CONFIG_PATH} bddl={BDDL_PATH}"
)
print(format_task_resource_profile(TASK_RESOURCE_PROFILE))
if ARGS.init_only and TASK_RESOURCE_PROFILE["particle_like_objects"]:
    print(
        "resource_warning: this task has particle-like BDDL systems; "
        "--init-only still launches OmniGibson and may be slow."
    )
if ARGS.low_resource:
    print(
        "low_resource: enabled "
        f"scene_graph_backend={ARGS.scene_graph_backend} "
        f"save_video={ARGS.save_video} "
            f"capture_during_actions={ARGS.capture_during_actions} "
            f"save_step_images={ARGS.save_step_images} "
            f"output_size={ARGS.output_size} "
            f"task_relevant_only={ARGS.task_relevant_only} "
            f"exclude_object_categories={ARGS.exclude_object_categories}"
        )
else:
    print(
        "new_task_options: "
        f"task_relevant_only={ARGS.task_relevant_only} "
        f"exclude_object_categories={ARGS.exclude_object_categories}"
    )

if ARGS.symbolic_smoke:
    with TASK_CONFIG_PATH.open("r", encoding="utf-8") as f:
        symbolic_smoke_config = json.load(f)
    symbolic_preview_source = symbolic_smoke_config.get("example_planning", [])
    symbolic_preview_plan, symbolic_preview_skipped, symbolic_preview_inserted = (
        build_symbolic_smoke_plan(symbolic_preview_source)
    )
    validate_symbolic_smoke_plan(symbolic_preview_plan)
    print_symbolic_smoke_summary(
        len(symbolic_preview_source),
        symbolic_preview_plan,
        symbolic_preview_skipped,
        symbolic_preview_inserted,
    )
    print(
        "symbolic_smoke_notice: OmniGibson still loads the scene and settles state "
        "changes, but physical navigation and manipulation trajectories are disabled."
    )
    if symbolic_preview_skipped:
        print(
            "symbolic_smoke_notice: final BDDL task success is not expected because "
            "unsupported actions are intentionally skipped."
        )

if ARGS.validate_only:
    raise SystemExit(0)

if ARGS.resource_check_only:
    run_resource_guard(ARGS)
    raise SystemExit(0)

if ARGS.headless:
    os.environ["OMNIGIBSON_HEADLESS"] = "1"

if ARGS.scene_graph_backend in {"samjam_sam2", "samjam_unigoal"}:
    ARGS.scene_graph_update_every = 1

os.environ["ISBENCH_SCENE_GRAPH_BACKEND"] = ARGS.scene_graph_backend
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

if os.environ.get("ISBENCH_RESOURCE_GUARD_DONE") != "1":
    run_resource_guard(ARGS)
    os.environ["ISBENCH_RESOURCE_GUARD_DONE"] = "1"

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
from og_ego_prim.models import PlanningAgent

gm.USE_GPU_DYNAMICS = not ARGS.disable_gpu_dynamics
print(f"omnigibson_runtime: USE_GPU_DYNAMICS={gm.USE_GPU_DYNAMICS}")


def configure_native_symbolic_grasp(benchmark):
    controller = benchmark.executor.controller
    controller_type = type(controller)
    if (
        controller_type.__name__ != "SymbolicSemanticActionPrimitives"
        or controller_type.__module__
        != "omnigibson.action_primitives.symbolic_semantic_action_primitives"
    ):
        raise RuntimeError(
            "symbolic smoke expected OmniGibson's native "
            "SymbolicSemanticActionPrimitives, got "
            f"{controller_type.__module__}.{controller_type.__name__}"
        )

    robot = controller.robot
    previous = robot._disable_grasp_handling
    robot._disable_grasp_handling = True
    print(
        "native_symbolic_grasp_config: "
        f"controller={controller_type.__module__}.{controller_type.__name__} "
        f"grasping_mode={robot.grasping_mode} "
        f"disable_grasp_handling={previous}->True"
    )


def install_disabled_scene_graph_patch():
    if ARGS.scene_graph_backend not in {"disabled", "none"}:
        return

    import og_ego_prim.scene_graph as scene_graph_module
    from og_ego_prim.scene_graph.perception_scene_graph import (
        PerceptionSceneGraphUpdater as OriginalPerceptionSceneGraphUpdater,
    )
    from og_ego_prim.scene_graph.schema import SceneGraphSnapshot

    class DisabledSceneGraphUpdater:
        def __init__(self, backend_name=None, update_every=None, sensor_name=None):
            self.backend_name = backend_name or "disabled"
            self.global_step_index = 0
            self.snapshot = self._snapshot(context=None)
            self.backend = None

        def _snapshot(self, context=None):
            primitive_name = None if context is None else context.primitive_name
            raw_plan = None if context is None else context.raw_plan
            step_index = self.global_step_index if context is None else context.step_index
            return SceneGraphSnapshot(
                step_index=step_index,
                primitive_name=primitive_name,
                raw_plan=raw_plan,
                metadata={
                    "source": "disabled",
                    "ready": False,
                    "perception_backend": self.backend_name,
                    "global_step_index": self.global_step_index,
                    "perception_skipped": True,
                    "object_count": 0,
                    "relation_count": 0,
                    "membership_edge_count": 0,
                    "total_edge_count": 0,
                    "room_graph": {"rooms": []},
                    "group_graph": {"groups": []},
                    "perception_errors": [],
                },
            )

        def reset(self, env):
            self.global_step_index = 0
            self.snapshot = self._snapshot(context=None)
            return self.snapshot

        def update(self, context=None):
            self.snapshot = self._snapshot(context=context)
            self.global_step_index += 1
            return self.snapshot

        def get_snapshot(self):
            return self.snapshot

        def to_prompt_context(self):
            return ""

    class NewTaskPerceptionSceneGraphUpdater:
        def __new__(cls, backend_name=None, *args, **kwargs):
            backend = backend_name or os.environ.get(
                "ISBENCH_SCENE_GRAPH_BACKEND",
                "omnigibson_truth",
            )
            if backend.lower() in {"disabled", "none"}:
                return DisabledSceneGraphUpdater(
                    backend_name=backend_name,
                    update_every=kwargs.get("update_every"),
                    sensor_name=kwargs.get("sensor_name"),
                )
            return OriginalPerceptionSceneGraphUpdater(
                backend_name,
                *args,
                **kwargs,
            )

    scene_graph_module.PerceptionSceneGraphUpdater = NewTaskPerceptionSceneGraphUpdater


def install_new_task_scene_patch():
    if not ARGS.task_relevant_only and not ARGS.exclude_object_categories:
        return

    import og_ego_prim.benchmark.online_benchmark as online_benchmark_module
    import og_ego_prim.benchmark.custom_behavior_task as custom_behavior_task_module

    benchmark_cls = online_benchmark_module.OnlineBehaviorBenchmark
    original_init_env_config = benchmark_cls.init_env_config

    def new_task_init_env_config(self, task, scene, config):
        env_config = original_init_env_config(self, task, scene, config)
        scene_config = env_config.setdefault("scene", {})
        if ARGS.task_relevant_only:
            scene_config["load_task_relevant_only"] = True
        not_load_categories = env_config["scene"].setdefault(
            "not_load_object_categories",
            [],
        )
        for category in [*ARGS.exclude_object_categories, "ceilings", "roof"]:
            if category not in not_load_categories:
                not_load_categories.append(category)
        print(
            "new_task_scene_config: "
            f"load_task_relevant_only={scene_config.get('load_task_relevant_only')} "
            f"not_load_object_categories={not_load_categories}"
        )
        return env_config

    benchmark_cls.init_env_config = new_task_init_env_config

    inroom_overrides = NEW_TASK_INROOM_OBJECT_OVERRIDES.get(ARGS.task, {})
    if not inroom_overrides:
        return

    sampler_cls = custom_behavior_task_module.CustomBDDLSampler
    original_build_inroom_object_scope = sampler_cls._build_inroom_object_scope

    def new_task_build_inroom_object_scope(self):
        result = original_build_inroom_object_scope(self)
        if result:
            return result

        inroom_scope = getattr(self, "_inroom_object_scope", {})
        for object_instance, simulator_name in inroom_overrides.items():
            matched = False
            available_names = []
            for object_scopes in inroom_scope.values():
                room_candidates = object_scopes.get(object_instance, {})
                for room_instance, candidates in room_candidates.items():
                    available_names.extend(obj.name for obj in candidates)
                    selected = [obj for obj in candidates if obj.name == simulator_name]
                    if selected:
                        room_candidates[room_instance] = selected
                        matched = True
            if not matched:
                raise RuntimeError(
                    "new-task in-room override could not bind "
                    f"{object_instance} to {simulator_name}; "
                    f"available={sorted(set(available_names))}"
                )
            print(
                "new_task_inroom_binding: "
                f"object={object_instance} simulator_object={simulator_name}"
            )
        return result

    sampler_cls._build_inroom_object_scope = new_task_build_inroom_object_scope


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


def track_robot_rgb_video(robot, tracker, output_size=None, frame_annotator=None):
    sensor_name, rgb, raw_shape = get_robot_rgb(robot, output_size)
    if frame_annotator is not None:
        rgb = frame_annotator(rgb)
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


def _text_pixel_width(draw, text):
    try:
        bbox = draw.textbbox((0, 0), text)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * 6


def _fit_text(draw, text, max_width):
    text = str(text)
    if _text_pixel_width(draw, text) <= max_width:
        return text

    ellipsis = "..."
    if _text_pixel_width(draw, ellipsis) > max_width:
        return ""

    for keep in range(len(text) - 1, 0, -1):
        head = (keep + 1) // 2
        tail = keep // 2
        candidate = f"{text[:head]}{ellipsis}{text[-tail:]}" if tail else f"{text[:head]}{ellipsis}"
        if _text_pixel_width(draw, candidate) <= max_width:
            return candidate
    return ellipsis


def annotate_task_action_hud(
    rgb,
    task_name,
    step_index=None,
    total_steps=None,
    action=None,
    phase=None,
    status=None,
):
    """Overlay task/action context on video frames."""
    frame = np.asarray(rgb)
    image = Image.fromarray(frame[:, :, :3].copy()).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size

    step_text = None
    if step_index is not None:
        step_text = f"Step: {step_index}"
        if total_steps is not None:
            step_text += f"/{total_steps}"

    phase_parts = []
    if phase:
        phase_parts.append(str(phase))
    if status:
        phase_parts.append(str(status))

    raw_lines = [f"Task: {task_name}"]
    if step_text:
        raw_lines.append(step_text)
    if action:
        raw_lines.append(f"Action: {action}")
    if phase_parts:
        raw_lines.append(f"Phase: {' | '.join(phase_parts)}")

    margin = max(6, int(min(width, height) * 0.03))
    pad_x = max(8, int(width * 0.025))
    pad_y = max(6, int(height * 0.02))
    max_text_width = max(32, width - margin * 2 - pad_x * 2)
    lines = [_fit_text(draw, line, max_text_width) for line in raw_lines]
    try:
        line_bbox = draw.textbbox((0, 0), "Ag")
        line_height = max(11, line_bbox[3] - line_bbox[1] + 3)
    except Exception:
        line_height = 13

    panel_height = pad_y * 2 + line_height * len(lines)
    left = margin
    top = max(margin, height - margin - panel_height)
    right = width - margin
    bottom = height - margin
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=6,
        fill=(0, 0, 0, 165),
        outline=(255, 255, 255, 95),
        width=1,
    )

    y = top + pad_y
    for line in lines:
        draw.text((left + pad_x, y), line, fill=(255, 255, 255, 245))
        y += line_height

    return np.asarray(Image.alpha_composite(image, overlay).convert("RGB"))


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
    run_log_stream = install_run_log(output_dir)
    print(f"run output directory: {output_dir.resolve()}")
    install_disabled_scene_graph_patch()
    install_new_task_scene_patch()
    if args.scene_graph_backend in {"samjam_sam2", "samjam_unigoal"}:
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
        online_object_sampling=args.online_object_sampling,
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

    if args.init_only:
        print(
            f"task initialization passed: task={benchmark.task_name} "
            f"scene={benchmark.scene_name} primitive={benchmark.primitive_type}"
        )
        sys.stdout.flush()
        sys.stderr.flush()
        if args.clear_on_exit:
            og.clear()
            return
        os._exit(0)

    if args.symbolic_smoke:
        configure_native_symbolic_grasp(benchmark)

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
        track_robot_rgb_video(
            robot,
            benchmark.tracker,
            args.output_size,
            frame_annotator=lambda rgb: annotate_task_action_hud(
                rgb,
                args.task,
                action="initialization",
                phase="before planning",
            ),
        )
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
        planner = list(benchmark.get_example_planning())
        print("planning_source: fixed example_planning")

    symbolic_smoke_skipped = []
    if args.symbolic_smoke:
        original_plan_count = len(planner)
        planner, symbolic_smoke_skipped, symbolic_smoke_inserted = (
            build_symbolic_smoke_plan(planner)
        )
        validate_symbolic_smoke_plan(planner)
        print_symbolic_smoke_summary(
            original_plan_count,
            planner,
            symbolic_smoke_skipped,
            symbolic_smoke_inserted,
        )
        print("planning_source: converted symbolic smoke plan (navigation disabled)")

    original_step_callback = benchmark.executor.step_callback
    capture_every = max(args.capture_every, 1)
    total_plan_steps = len(planner)
    current_step_frames = None
    current_step_action = None
    current_step_index = None

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

    def annotate_current_video_frame(rgb, phase, status=None):
        rgb = annotate_current_navigation_frame(rgb)
        return annotate_task_action_hud(
            rgb,
            args.task,
            step_index=current_step_index,
            total_steps=total_plan_steps,
            action=current_step_action,
            phase=phase,
            status=status,
        )

    def wrapped_step_callback(context):
        if original_step_callback is not None:
            original_step_callback(context)
        if not args.capture_during_actions or not args.save_video:
            return
        if context.step_index % capture_every != 0:
            return
        rgb = capture_robot_rgb_frame(robot, args.output_size)
        rgb = annotate_current_video_frame(rgb, phase="running")
        benchmark.tracker.track_video_rgb(rgb)
        if current_step_frames is not None:
            current_step_frames.append(rgb)

    benchmark.executor.step_callback = wrapped_step_callback

    attempted_actions = 0
    succeeded_actions = 0
    failed_actions = 0
    for step_index, plan in enumerate(planner, start=1):
        action = plan["action"]
        current_step_action = action
        current_step_index = step_index
        step_dir = output_dir / safe_step_dir_name(step_index, action)
        step_dir.mkdir(parents=True, exist_ok=True)
        if args.scene_graph_backend in {"samjam_sam2", "samjam_unigoal"}:
            set_samjam_output_dir(benchmark, step_dir / "samjam_outputs")

        print(f"plan_step_{step_index:02d}: {action}")
        current_step_frames = []
        capture_step_rgb = args.save_step_images or args.save_video
        if capture_step_rgb:
            sensor_name, raw_shape, shape, before_rgb = save_robot_rgb_with_frame(
                robot, step_dir / "obs_before.png", args.output_size
            )
            current_step_frames.append(
                annotate_current_video_frame(before_rgb, phase="before")
            )
            print(
                f"saved {step_dir.name}/obs_before.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
            )

        attempted_actions += 1
        execution_succeeded = benchmark.execute_plan(plan)
        if execution_succeeded:
            succeeded_actions += 1
        else:
            failed_actions += 1
        video_after_rgb = None
        if capture_step_rgb:
            sensor_name, raw_shape, shape, after_rgb = save_robot_rgb_with_frame(
                robot, step_dir / "obs_after.png", args.output_size
            )
            video_after_rgb = annotate_current_video_frame(
                after_rgb,
                phase="after",
                status="succeeded" if execution_succeeded else "failed",
            )
            current_step_frames.append(video_after_rgb)
            print(
                f"saved {step_dir.name}/obs_after.png from sensor={sensor_name}, raw_shape={raw_shape}, saved_shape={shape}"
            )
        if args.save_video:
            benchmark.tracker.track_video_rgb(video_after_rgb)
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
        current_step_action = None
        current_step_index = None
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

    if args.symbolic_smoke:
        print(
            "symbolic_smoke_execution: "
            f"attempted={attempted_actions} succeeded={succeeded_actions} "
            f"failed={failed_actions} skipped={len(symbolic_smoke_skipped)}"
        )
        if failed_actions:
            print(
                "symbolic_smoke_result: FAILED because one or more executable "
                "symbolic actions failed."
            )
        elif symbolic_smoke_skipped:
            print(
                "symbolic_smoke_result: executable actions passed; full task success "
                "is not asserted because the source plan contains unsupported actions."
            )
        else:
            print("symbolic_smoke_result: PASSED")

    benchmark.termination_evaluation()
    if args.scene_graph_backend in {"samjam_sam2", "samjam_unigoal"}:
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

    try:
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
    except Exception as e:
        benchmark.tracker.track_error(
            action="save_final_robot_observation",
            err_type=e.__class__.__name__,
            msg=str(e),
        )
        print(
            "final robot observation failed: "
            f"{e.__class__.__name__}: {e}"
        )

    save_report_and_video(args, benchmark, output_dir)
    save_scene_graph_report(args, benchmark, output_dir)
    print("scene_graph_history:", len(benchmark.tracker.scene_graph_history))

    sys.stdout.flush()
    sys.stderr.flush()
    run_log_stream.flush()
    if args.clear_on_exit:
        og.clear()
        return
    os._exit(0)


if __name__ == "__main__":
    main()
