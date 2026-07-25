"""Capture native egocentric RGB frames without SAM2 or UniGoal mapping."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from og_ego_prim.config import RuntimeConfig, load_runtime_config_dict, parse_size
from og_ego_prim.utils.task_registry import get_task_config_path


ARTIFACT_SCHEMA_VERSION = "isbench.native_frame_collect.v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize one task and save one native robot RGB frame without "
            "SAM2, UniGoal mapping, planner, or task actions."
        )
    )
    parser.add_argument("--task", required=True)
    parser.add_argument("--scene")
    parser.add_argument("--config")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--frame-image-size",
        "--scene-graph-image-size",
        dest="frame_image_size",
        help="Robot RGB sensor resolution, for example 512x512.",
    )
    headless = parser.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true", default=None)
    headless.add_argument("--no-headless", dest="headless", action="store_false")
    return parser


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _rgb_uint8(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    rgb = np.asarray(value)
    if rgb.ndim != 3 or rgb.shape[-1] < 3:
        raise RuntimeError(f"native frame is not HxWxC RGB: {rgb.shape}")
    rgb = rgb[:, :, :3]
    if rgb.dtype != np.uint8:
        if rgb.size and float(rgb.max()) <= 1.0:
            rgb = rgb * 255.0
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb)


def _write_native_frame(path: Path, rgb: np.ndarray) -> None:
    """Write lossless RGB with OpenCV, which remains usable inside Isaac Sim."""

    import cv2

    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    if not cv2.imwrite(str(path), bgr):
        raise RuntimeError(f"cv2.imwrite returned False for {path}")


def _task_context(
    task: str,
    requested_scene: Optional[str],
) -> tuple[Path, dict[str, Any], str]:
    task_path = get_task_config_path(task)
    task_config = json.loads(task_path.read_text(encoding="utf-8"))
    if not isinstance(task_config, dict):
        raise ValueError(f"task configuration is not an object: {task_path}")
    scene_info = task_config.get("scene_info")
    scene_info = scene_info if isinstance(scene_info, Mapping) else {}
    scene = requested_scene or scene_info.get("default_scene_model")
    if not scene:
        raise ValueError("task scene_info.default_scene_model is missing")
    return task_path, task_config, str(scene)


def _default_output_dir(task_path: Path, scene: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        Path("outputs")
        / "scene_graph_collect"
        / task_path.stem
        / f"{scene}__{timestamp}"
    )


def _prepare_output_dir(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"output directory already contains files: {output_dir}. "
            "Choose a new --output-dir."
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def _runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    runtime_config = RuntimeConfig.from_mapping(load_runtime_config_dict(args.config))
    runtime_config.scene_graph.backend = "disabled"
    runtime_config.scene_graph.step_interval = 0
    runtime_config.scene_graph.update_every = 1
    runtime_config.scene_graph.output_dir = None
    runtime_config.scene_graph.debug_log_path = None
    if args.frame_image_size:
        image_size = parse_size(args.frame_image_size)
        runtime_config.scene_graph.image_size = image_size
        runtime_config.artifacts.sensor_image_size = image_size
    elif runtime_config.artifacts.sensor_image_size is None:
        runtime_config.artifacts.sensor_image_size = runtime_config.scene_graph.image_size
    if args.headless is not None:
        runtime_config.runtime.headless = bool(args.headless)
    runtime_config.artifacts.save_video = False
    runtime_config.artifacts.save_step_images = False
    runtime_config.artifacts.save_surrounding_observations = False
    runtime_config.artifacts.save_topdown_scene = False
    return runtime_config


def _capture_native_frame(benchmark: Any, runtime_config: RuntimeConfig) -> Any:
    """Read the shared observation adapter only; never start perception detection."""

    from og_ego_prim.scene_graph.observation_adapter import ISBenchObservationAdapter

    observer = ISBenchObservationAdapter(
        sensor_name=runtime_config.scene_graph.sensor_name,
    )
    observer.reset()
    return observer.observe(benchmark.env)


def _capture_artifacts(
    *,
    benchmark: Any,
    task_path: Path,
    task_config: Mapping[str, Any],
    task: str,
    scene: str,
    output_dir: Path,
    runtime_config: RuntimeConfig,
) -> dict[str, Any]:
    frame = _capture_native_frame(benchmark, runtime_config)
    frame_index = int(getattr(frame, "frame_index", -1))
    if frame_index < 0:
        raise RuntimeError("native frame has no non-negative frame_index")
    rgb = _rgb_uint8(getattr(frame, "rgb", None))

    frames_dir = output_dir / "native_video_frames"
    frames_dir.mkdir(exist_ok=True)
    image_path = frames_dir / f"{frame_index:06d}.png"
    _write_native_frame(image_path, rgb)
    frame_record = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "frame_index": frame_index,
        "sensor_name": str(getattr(frame, "sensor_name", "")),
        "rgb_shape": list(rgb.shape),
        "robot_position": _json_safe(getattr(frame, "robot_position", None)),
        "capture_backend": "samjam_unigoal.observation_adapter_only",
        "native_video_frame": {
            "path": str(image_path.relative_to(output_dir)),
            "sha256": _sha256_file(image_path),
        },
    }
    frame_record_path = frames_dir / f"{frame_index:06d}.json"
    frame_record_path.write_bytes(_canonical_json(frame_record) + b"\n")

    runtime_config_path = output_dir / "runtime_config.json"
    runtime_config_path.write_bytes(
        _canonical_json(_json_safe(runtime_config.to_dict())) + b"\n"
    )
    scene_info = task_config.get("scene_info")
    scene_info = scene_info if isinstance(scene_info, Mapping) else {}
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "collected",
        "output_dir": str(output_dir.resolve()),
        "task": {
            "requested": task,
            "config_path": str(task_path),
            "scene": scene,
            "room_hint": scene_info.get("room"),
            "primitive_contract": benchmark.primitive_type,
            "online_object_sampling": False,
        },
        "collection_boundary": {
            "capture_backend": "samjam_unigoal.observation_adapter_only",
            "sam2_detect_started": False,
            "sam2_tracking_started": False,
            "unigoal_mapping_started": False,
            "point_cloud_started": False,
            "planner_started": False,
            "risk_review_started": False,
            "task_action_execution_started": False,
            "note": (
                "Environment initialization may settle simulator state. This command "
                "does not call detect(), update_memory(), controller.propose(), "
                "controller.review_action(), benchmark.execute_plan(), "
                "executor.execute_plan(), or an evaluator action."
            ),
        },
        "source_frame": frame_record,
        "artifacts": {
            "native_video_frame_record": {
                "path": str(frame_record_path.relative_to(output_dir)),
                "sha256": _sha256_file(frame_record_path),
            },
            "runtime_config": {
                "path": str(runtime_config_path.relative_to(output_dir)),
                "sha256": _sha256_file(runtime_config_path),
            },
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(_canonical_json(manifest) + b"\n")
    return manifest


def collect(args: argparse.Namespace) -> dict[str, Any]:
    task_path, task_config, scene = _task_context(args.task, args.scene)
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(task_path, scene)
    _prepare_output_dir(output_dir)
    runtime_config = _runtime_config(args)
    if runtime_config.runtime.headless:
        os.environ["OMNIGIBSON_HEADLESS"] = "1"

    from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

    maybe_reexec_with_omnigibson_python()
    from og_ego_prim.utils.monkey_patch import add_monkey_patch

    add_monkey_patch()
    from omnigibson.macros import gm
    from og_ego_prim.benchmark import build_benchmark

    gm.USE_GPU_DYNAMICS = True
    benchmark = None
    try:
        benchmark = build_benchmark(
            task=args.task,
            scene=scene,
            ego_view=True,
            draw_bbox_2d=False,
            primitive_type=None,
            scene_graph_step_interval=0,
            scene_graph_backend="disabled",
            use_initial_setup=False,
            use_self_caption=False,
            online_object_sampling=False,
            debug=False,
            eval_process_safety=False,
            eval_termination_safety=False,
            eval_awareness=False,
            eval_execution=False,
            runtime_config=runtime_config,
        )
        return _capture_artifacts(
            benchmark=benchmark,
            task_path=task_path,
            task_config=task_config,
            task=args.task,
            scene=scene,
            output_dir=output_dir,
            runtime_config=runtime_config,
        )
    except Exception as exc:
        (output_dir / "failure.json").write_bytes(
            _canonical_json(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "status": "failed",
                    "error": {"type": exc.__class__.__name__, "message": str(exc)},
                }
            )
            + b"\n"
        )
        raise
    finally:
        if benchmark is not None:
            benchmark.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    manifest = collect(build_parser().parse_args(argv))
    frame = manifest["source_frame"]
    print(
        "scene_graph_collect: "
        f"frame={frame['frame_index']} rgb={frame['rgb_shape']} "
        f"artifact={frame['native_video_frame']['path']}"
    )
    print(f"artifacts: {manifest['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
