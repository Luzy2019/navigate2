#!/usr/bin/env python3
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent

import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from og_ego_prim.scene_graph.backends import build_perception_backend
from og_ego_prim.scene_graph.backends.samjam_sam2 import SAMJAMOutputWriter
from og_ego_prim.scene_graph.backends.utils import to_builtin
from og_ego_prim.scene_graph.perception import FrameObservation
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater
from og_ego_prim.scene_graph.schema import (
    SCENE_GRAPH_SCHEMA_VERSION,
    scene_graph_report,
)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
PERCEPTION_BACKENDS = {"unigoal_grounded_sam", "samjam_sam2", "samjam_unigoal"}


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


def parse_output_size(value: Any) -> Optional[Tuple[int, int]]:
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
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "output size must be WIDTHxHEIGHT, one integer, or original"
        ) from exc

    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("output width and height must be positive")
    return width, height


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the perception scene-graph backend over an offline physical-world "
            "image sequence and save scene graph history records."
        )
    )
    parser.add_argument(
        "--image-dir",
        default=None,
        help="Directory containing ordered RGB images. Defaults to this script's directory.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory for scene_graph_report.json and SAMJAM debug artifacts. "
            "Defaults to IMAGE_DIR/scene_graph_history_outputs."
        ),
    )
    parser.add_argument(
        "--timestamp-output",
        action=BooleanOptionalAction,
        default=True,
        help="Append a YYYYMMDD_HHMMSS timestamp to OUTPUT_DIR.",
    )
    parser.add_argument(
        "--scene-graph-backend",
        default=os.environ.get("ISBENCH_SCENE_GRAPH_BACKEND", "samjam_unigoal"),
        choices=sorted(PERCEPTION_BACKENDS),
        help="Perception backend to run over the image sequence.",
    )
    parser.add_argument(
        "--scene-graph-history-interval",
        type=int,
        default=int(os.environ.get("ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL", "1")),
        help="Keep one report history snapshot every N processed frames.",
    )
    parser.add_argument(
        "--scene-graph-image-size",
        type=parse_output_size,
        default=parse_output_size(os.environ.get("ISBENCH_SCENE_GRAPH_IMAGE_SIZE", "512x512")),
        help="Resize input images to WIDTHxHEIGHT before perception, or use original.",
    )
    parser.add_argument(
        "--task",
        default="physical_world",
        help="Task label written to scene_graph_report.json.",
    )
    parser.add_argument(
        "--scene",
        default="physical_world",
        help="Scene label written to scene_graph_report.json.",
    )
    parser.add_argument(
        "--sensor-name",
        default="physical_world_rgb",
        help="Sensor name stored in offline FrameObservation records.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Optional maximum number of ordered images to process.",
    )
    parser.add_argument(
        "--start-frame",
        type=int,
        default=0,
        help="Zero-based index into the sorted image sequence.",
    )
    parser.add_argument(
        "--synthetic-depth",
        type=float,
        default=float(os.environ.get("ISBENCH_PHYSICAL_WORLD_SYNTHETIC_DEPTH", "2.0")),
        help="Constant depth in meters used when no depth image is available.",
    )
    parser.add_argument(
        "--hfov",
        type=float,
        default=float(os.environ.get("ISBENCH_SCENE_GRAPH_HFOV", "90.0")),
        help="Horizontal field of view used to synthesize camera intrinsics.",
    )
    parser.add_argument("--camera-origin-x", type=float, default=0.0)
    parser.add_argument("--camera-origin-y", type=float, default=0.0)
    parser.add_argument("--camera-origin-z", type=float, default=0.0)
    parser.add_argument(
        "--camera-step-x",
        type=float,
        default=0.0,
        help="Synthetic camera translation added per processed frame.",
    )
    parser.add_argument("--camera-step-y", type=float, default=0.0)
    parser.add_argument("--camera-step-z", type=float, default=0.0)
    parser.add_argument(
        "--save-per-frame-results",
        action=BooleanOptionalAction,
        default=True,
        help="Save one compact snapshot JSON per processed image.",
    )
    parser.add_argument(
        "--stop-on-error",
        action=BooleanOptionalAction,
        default=True,
        help="Stop after the first failed frame.",
    )
    parser.add_argument(
        "--disable-vlm",
        action=BooleanOptionalAction,
        default=False,
        help=(
            "Disable SAMJAM VLM calls and keep unmatched SAM2 masks. Object names "
            "will usually be unknown_object."
        ),
    )
    return parser.parse_args()


def numeric_sort_key(path: Path) -> Tuple[Any, ...]:
    parts: List[Any] = []
    for part in re.split(r"(\d+)", path.stem):
        if part.isdigit():
            parts.append(int(part))
        elif part:
            parts.append(part.lower())
    return tuple(parts + [path.suffix.lower()])


def iter_image_paths(image_dir: Path) -> List[Path]:
    if not image_dir.is_dir():
        raise FileNotFoundError(f"image directory not found: {image_dir}")
    paths = [
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    paths.sort(key=numeric_sort_key)
    if not paths:
        raise FileNotFoundError(f"no RGB images found in {image_dir}")
    return paths


def output_dir_from_args(args: argparse.Namespace) -> Path:
    output_dir = (
        Path(args.output_dir)
        if args.output_dir is not None
        else Path(args.image_dir) / "scene_graph_history_outputs"
    )
    if args.timestamp_output:
        output_dir = output_dir.parent / f"{output_dir.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def configure_backend_output(backend_name: str, output_dir: Path, disable_vlm: bool) -> None:
    os.environ["ISBENCH_SCENE_GRAPH_BACKEND"] = backend_name
    os.environ["ISBENCH_SCENE_GRAPH_OUTPUT_DIR"] = str(output_dir)
    os.environ["ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL"] = os.environ.get(
        "ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL", "1"
    )
    if backend_name in {"samjam_sam2", "samjam_unigoal"}:
        samjam_dir = output_dir / "samjam_outputs"
        os.environ["ISBENCH_SAMJAM_OUTPUT_DIR"] = str(samjam_dir)
        if disable_vlm:
            os.environ["ISBENCH_SAMJAM_VLM_ENABLED"] = "0"
            os.environ["ISBENCH_SAMJAM_KEEP_UNMATCHED_MASKS"] = "1"
            os.environ["ISBENCH_SAMJAM_UNIGOAL_REQUIRE_MATCH_METADATA"] = "0"

    debug_names = (
        "ISBENCH_SCENE_GRAPH_DEBUG_MATCHING",
        "ISBENCH_SAMJAM_DEBUG_MATCHING",
        "ISBENCH_UNIGOAL_MAPPING_DEBUG",
        "ISBENCH_SAMJAM_UNIGOAL_DEBUG_MAPPING",
        "ISBENCH_UNIGOAL_DEBUG_MATCHING",
        "ISBENCH_UNIGOAL_GROUNDED_SAM_DEBUG",
    )
    if not os.environ.get("ISBENCH_SCENE_GRAPH_DEBUG_LOG_PATH") and all(
        os.environ.get(name) is None for name in debug_names
    ):
        os.environ["ISBENCH_SCENE_GRAPH_DEBUG_MATCHING"] = "1"


def load_rgb(path: Path, output_size: Optional[Tuple[int, int]]) -> np.ndarray:
    image = Image.open(path).convert("RGB")
    if output_size is not None and image.size != output_size:
        image = image.resize(output_size, Image.Resampling.LANCZOS)
    return np.ascontiguousarray(np.asarray(image, dtype=np.uint8))


def synthetic_intrinsics(width: int, height: int, hfov: float) -> np.ndarray:
    hfov_radians = math.radians(float(hfov))
    if not 0.0 < hfov_radians < math.pi:
        raise ValueError("--hfov must be in the open interval (0, 180)")
    fx = width / (2.0 * math.tan(hfov_radians / 2.0))
    fy = fx
    cx = (width - 1.0) / 2.0
    cy = (height - 1.0) / 2.0
    return np.asarray(
        [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
        dtype=np.float32,
    )


def synthetic_pose(args: argparse.Namespace, sequence_index: int) -> np.ndarray:
    pose = np.eye(4, dtype=np.float32)
    pose[:3, 3] = np.asarray(
        [
            args.camera_origin_x + args.camera_step_x * sequence_index,
            args.camera_origin_y + args.camera_step_y * sequence_index,
            args.camera_origin_z + args.camera_step_z * sequence_index,
        ],
        dtype=np.float32,
    )
    return pose


def build_frame(
    *,
    args: argparse.Namespace,
    image_path: Path,
    frame_index: int,
    sequence_index: int,
) -> FrameObservation:
    rgb = load_rgb(image_path, args.scene_graph_image_size)
    height, width = rgb.shape[:2]
    depth = np.full((height, width), float(args.synthetic_depth), dtype=np.float32)
    intrinsics = synthetic_intrinsics(width, height, args.hfov)
    camera_pose = synthetic_pose(args, sequence_index)
    robot_position = [float(value) for value in camera_pose[:3, 3].tolist()]
    return FrameObservation(
        frame_index=frame_index,
        rgb=rgb,
        depth=depth,
        intrinsics=intrinsics,
        camera_pose=camera_pose,
        robot_position=robot_position,
        sensor_name=args.sensor_name,
        metadata={
            "source_image": str(image_path),
            "rgb_shape": list(rgb.shape),
            "depth_shape": list(depth.shape),
            "offline_physical_world": True,
            "synthetic_depth_m": float(args.synthetic_depth),
            "synthetic_hfov_deg": float(args.hfov),
            "synthetic_camera_pose": to_builtin(camera_pose),
        },
    )


def attach_output_writer(backend: Any, output_dir: Path) -> None:
    target_backend = getattr(backend, "samjam_backend", backend)
    if hasattr(target_backend, "output_writer"):
        target_backend.output_writer = SAMJAMOutputWriter(output_dir / "samjam_outputs")


def should_keep_history(processed_index: int, total_count: int, interval: int) -> bool:
    if interval <= 1:
        return True
    return processed_index == 0 or processed_index == total_count - 1 or processed_index % interval == 0


def compact_snapshot_record(
    snapshot: Dict[str, Any],
    *,
    source_image: Path,
    frame_index: int,
) -> Dict[str, Any]:
    record = dict(snapshot)
    record["source_image"] = str(source_image)
    record["frame_index"] = frame_index
    return record


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_builtin(payload), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def latest_summary(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not snapshot:
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
    return dict(snapshot.get("summary") or {})


def run_sequence(args: argparse.Namespace, image_paths: Iterable[Path], output_dir: Path) -> Dict[str, Any]:
    image_paths = list(image_paths)
    backend = build_perception_backend(args.scene_graph_backend, sensor_name=args.sensor_name)
    env = SimpleNamespace(robots=[], scene=None)
    backend.reset(env)
    attach_output_writer(backend, output_dir)

    converter = PerceptionSceneGraphUpdater(
        backend_name=args.scene_graph_backend,
        sensor_name=args.sensor_name,
    )
    converter.backend = backend
    converter.env = env
    converter.global_step_index = 0

    full_history: List[Dict[str, Any]] = []
    report_history: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    frames_dir = output_dir / "frames"
    if args.save_per_frame_results:
        frames_dir.mkdir(parents=True, exist_ok=True)

    for processed_index, image_path in enumerate(image_paths):
        frame_index = args.start_frame + processed_index
        print(f"[physical_world] frame {processed_index + 1}/{len(image_paths)}: {image_path}")
        try:
            frame = build_frame(
                args=args,
                image_path=image_path,
                frame_index=frame_index,
                sequence_index=processed_index,
            )
            samjam_result = backend.detect(frame)
            result = backend.update_memory(samjam_result)
            converter.latest_result = result
            converter.global_step_index = processed_index
            snapshot = converter._snapshot_from_result(
                result,
                context=None,
                skipped=False,
                force=(processed_index == 0),
            ).to_dict()
            record = compact_snapshot_record(
                snapshot,
                source_image=image_path,
                frame_index=frame_index,
            )
            full_history.append(record)
            if should_keep_history(
                processed_index,
                len(image_paths),
                args.scene_graph_history_interval,
            ):
                report_history.append(snapshot)
            if args.save_per_frame_results:
                save_json(frames_dir / f"frame_{frame_index:06d}_scene_graph.json", record)
        except Exception as exc:
            error = {
                "frame_index": frame_index,
                "source_image": str(image_path),
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
            errors.append(error)
            print(
                "[physical_world] ERROR "
                f"frame={frame_index} type={error['type']} message={error['message']}"
            )
            if args.stop_on_error:
                break

    latest = full_history[-1] if full_history else None
    latest_snapshot = None if latest is None else {
        key: value
        for key, value in latest.items()
        if key not in {"source_image", "frame_index"}
    }
    report = scene_graph_report(
        task=args.task,
        scene=args.scene,
        scene_graph_backend=args.scene_graph_backend,
        scene_graph_step_interval=1,
        scene_graph_update_every=1,
        latest_summary=latest_summary(latest_snapshot),
        latest_scene_graph=latest_snapshot,
        scene_graph_history=report_history,
        error_stack=errors,
        execution_diagnostics=[
            {
                "source": "physical_world/generate_scene_graph_history.py",
                "schema_version": SCENE_GRAPH_SCHEMA_VERSION,
                "image_dir": str(Path(args.image_dir)),
                "processed_frames": len(full_history),
                "requested_frames": len(image_paths),
                "history_interval": args.scene_graph_history_interval,
                "synthetic_depth_m": float(args.synthetic_depth),
                "synthetic_hfov_deg": float(args.hfov),
                "image_size": (
                    "original"
                    if args.scene_graph_image_size is None
                    else list(args.scene_graph_image_size)
                ),
            }
        ],
    )
    save_json(output_dir / "scene_graph_report.json", report)
    save_json(output_dir / "scene_graph_history_full.json", full_history)
    save_json(
        output_dir / "manifest.json",
        {
            "script": "physical_world/generate_scene_graph_history.py",
            "backend": args.scene_graph_backend,
            "image_count": len(image_paths),
            "processed_frames": len(full_history),
            "errors": errors,
            "output_dir": str(output_dir),
            "image_paths": [str(path) for path in image_paths],
        },
    )
    return report

# MPLCONFIGDIR=/tmp/matplotlib \
# ISBENCH_SAMJAM_POINTS_PER_SIDE=8 \
# ISBENCH_SAMJAM_POINTS_PER_BATCH=16 \
# ISBENCH_SAMJAM_CROP_N_LAYERS=0 \
# ISBENCH_SAMJAM_MAX_MASKS=10 \
# /home/lzy/anaconda3/envs/isbench/bin/python -u physical_world/generate_scene_graph_history.py \
#   --scene-graph-backend samjam_unigoal \
#   --output-dir physical_world/scene_graph_history_outputs_vlm \
#   --image-dir physical_world/data/raw_images

def main() -> int:
    args = parse_args()
    if args.start_frame < 0:
        raise SystemExit("--start-frame must be non-negative")
    if args.max_frames is not None and args.max_frames <= 0:
        raise SystemExit("--max-frames must be greater than zero")
    if args.scene_graph_history_interval <= 0:
        raise SystemExit("--scene-graph-history-interval must be greater than zero")
    if args.synthetic_depth <= 0:
        raise SystemExit("--synthetic-depth must be greater than zero")

    image_dir = Path(args.image_dir) if args.image_dir is not None else Path(__file__).resolve().parent
    args.image_dir = str(image_dir)
    image_paths = iter_image_paths(image_dir)
    image_paths = image_paths[args.start_frame :]
    if args.max_frames is not None:
        image_paths = image_paths[: args.max_frames]
    if not image_paths:
        raise SystemExit("no images left after applying --start-frame/--max-frames")

    output_dir = output_dir_from_args(args)
    configure_backend_output(args.scene_graph_backend, output_dir, args.disable_vlm)
    report = run_sequence(args, image_paths, output_dir)
    summary = report.get("latest_summary", {})
    print(
        "[physical_world] wrote "
        f"{output_dir / 'scene_graph_report.json'} "
        f"frames={len(image_paths)} objects={summary.get('objects', 0)} "
        f"errors={len(report.get('error_stack', []))}"
    )
    return 0 if not report.get("error_stack") else 1


if __name__ == "__main__":
    raise SystemExit(main())
