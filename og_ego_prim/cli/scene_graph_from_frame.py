"""Generate one current-timestep scene graph from a captured native RGB frame.

This command never starts OmniGibson, SAM2, point-cloud reconstruction, or
UniGoal. It consumes a directory written by scene_graph_collect and either
normalizes human JSON or asks a direct image model to author the graph.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from og_ego_prim.scene_graph.ideal_frame import (
    FrameGraphValidationError,
    build_frame_graph_prompt,
    generate_frame_graph_with_model,
    load_json_object,
    normalize_current_frame_graph,
    task_entity_ids,
)


ARTIFACT_SCHEMA_VERSION = "isbench.ideal_frame_scene_graph.v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one validated current-timestep v2 scene graph directly from "
            "a captured native RGB frame."
        )
    )
    parser.add_argument("--capture-dir", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--graph-json", help="Human-authored raw graph JSON.")
    source.add_argument("--model", help="Image-capable model for direct graph generation.")
    parser.add_argument("--local-model", action="store_true")
    parser.add_argument(
        "--sam2-reference",
        help=(
            "Optional SAM2/SAMJAM reference JSON for the author only. It is "
            "recorded but never merged into the final current-frame graph."
        ),
    )
    parser.add_argument("--output-dir")
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


def _load_manifest(capture_dir: Path) -> dict[str, Any]:
    manifest_path = capture_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"capture manifest not found: {manifest_path}")
    manifest = load_json_object(manifest_path)
    if manifest.get("status") != "collected":
        raise FrameGraphValidationError("capture manifest status must be collected")
    boundary = manifest.get("collection_boundary")
    if not isinstance(boundary, Mapping):
        raise FrameGraphValidationError("capture manifest has no collection_boundary")
    forbidden = (
        "sam2_detect_started",
        "sam2_tracking_started",
        "unigoal_mapping_started",
        "point_cloud_started",
        "planner_started",
        "risk_review_started",
        "task_action_execution_started",
    )
    if any(boundary.get(key) is not False for key in forbidden):
        raise FrameGraphValidationError(
            "capture manifest does not prove the required A/B-only boundary"
        )
    return manifest


def _frame_record(
    capture_dir: Path,
    manifest: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    artifact = (manifest.get("artifacts") or {}).get("native_video_frame_record")
    if not isinstance(artifact, Mapping):
        raise FrameGraphValidationError("capture manifest has no native frame record")
    record_path = capture_dir / str(artifact.get("path") or "")
    if not record_path.is_file():
        raise FileNotFoundError(f"native frame record not found: {record_path}")
    if _sha256_file(record_path) != str(artifact.get("sha256") or ""):
        raise FrameGraphValidationError("native frame record hash does not match manifest")
    record = load_json_object(record_path)
    frame_artifact = record.get("native_video_frame")
    if not isinstance(frame_artifact, Mapping):
        raise FrameGraphValidationError("native frame record has no image artifact")
    image_path = capture_dir / str(frame_artifact.get("path") or "")
    if not image_path.is_file():
        raise FileNotFoundError(f"native frame image not found: {image_path}")
    if _sha256_file(image_path) != str(frame_artifact.get("sha256") or ""):
        raise FrameGraphValidationError("native frame image hash does not match record")
    return image_path, record


def _load_task_config(manifest: Mapping[str, Any]) -> dict[str, Any]:
    task = manifest.get("task")
    if not isinstance(task, Mapping):
        raise FrameGraphValidationError("capture manifest has no task descriptor")
    task_path = Path(str(task.get("config_path") or ""))
    if not task_path.is_file():
        raise FileNotFoundError(f"task config not found: {task_path}")
    return load_json_object(task_path)


def _load_sam2_reference(path_value: Optional[str]) -> Optional[dict[str, Any]]:
    if not path_value:
        return None
    path = Path(path_value).resolve()
    reference = load_json_object(path)
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "used_for_authoring_only": True,
        "merged_into_final_graph": False,
        "note": (
            "SAM2/SAMJAM is optional reference evidence. The final graph is "
            "still authored directly for this RGB frame and is not a SAM2, "
            "point-cloud, or UniGoal memory output."
        ),
        "reference_object_count": len(reference.get("objects") or []),
        "reference_relation_count": len(reference.get("relationships") or []),
    }


def generate(args: argparse.Namespace) -> Path:
    capture_dir = Path(args.capture_dir).resolve()
    manifest = _load_manifest(capture_dir)
    image_path, frame_record = _frame_record(capture_dir, manifest)
    task_config = _load_task_config(manifest)
    frame_index = int(frame_record["frame_index"])
    task = manifest["task"]
    room_hint = task.get("room_hint") if isinstance(task, Mapping) else None
    sam2_reference = _load_sam2_reference(args.sam2_reference)
    if args.graph_json:
        raw_graph = load_json_object(Path(args.graph_json))
        source = "manual_json"
    else:
        prompt = build_frame_graph_prompt(
            task_config=task_config,
            frame_index=frame_index,
            room_hint=None if room_hint is None else str(room_hint),
            sam2_reference=(
                load_json_object(Path(str(sam2_reference["path"])))
                if sam2_reference is not None
                else None
            ),
        )
        raw_graph, _ = generate_frame_graph_with_model(
            image_path=image_path,
            prompt=prompt,
            model=args.model,
            local=bool(args.local_model),
        )
        source = f"direct_vlm:{args.model}"
    graph = normalize_current_frame_graph(
        raw_graph,
        frame_index=frame_index,
        room_hint=None if room_hint is None else str(room_hint),
        allowed_roles=task_entity_ids(task_config),
        source=source,
    )
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else capture_dir / "ideal_scene_graph"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    graph_path = output_dir / f"frame_{frame_index:06d}.scene_graph.json"
    if graph_path.exists():
        raise FileExistsError(f"refusing to overwrite existing graph: {graph_path}")
    graph_path.write_bytes(_canonical_json(graph))
    record = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "generated",
        "source": source,
        "capture_dir": str(capture_dir),
        "frame_index": frame_index,
        "native_video_frame": {
            "path": str(image_path),
            "sha256": _sha256_file(image_path),
        },
        "scene_graph": {
            "path": str(graph_path),
            "sha256": _sha256_file(graph_path),
            "counts": {
                "rooms": graph["summary"]["rooms"],
                "nodes": graph["summary"]["objects"],
                "edges": graph["summary"]["edges"],
            },
        },
        "current_timestep_only": True,
        "sam2_reference": sam2_reference,
        "sam2_used": sam2_reference is not None,
        "sam2_merged_into_final_graph": False,
        "point_cloud_used": False,
        "unigoal_mapping_used": False,
    }
    record_path = output_dir / f"frame_{frame_index:06d}.manifest.json"
    record_path.write_bytes(_canonical_json(record))
    return graph_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    graph_path = generate(build_parser().parse_args(argv))
    print(f"ideal current-frame scene graph: {graph_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
