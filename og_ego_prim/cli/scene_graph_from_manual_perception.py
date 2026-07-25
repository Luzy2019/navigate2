"""Build a v2 scene graph from human-confirmed current-frame perception."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from og_ego_prim.scene_graph.manual_current_frame import (
    MANUAL_CURRENT_FRAME_PERCEPTION_SCHEMA_VERSION,
    load_manual_current_frame_perception,
)
from og_ego_prim.scene_graph.perception_scene_graph import PerceptionSceneGraphUpdater


ARTIFACT_SCHEMA_VERSION = "isbench.manual_current_frame_scene_graph.v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert human-confirmed current-frame objects and relations through "
            "the normal PerceptionResult-to-v2-scene-graph post-processing path."
        )
    )
    parser.add_argument("--capture-dir", required=True)
    parser.add_argument("--perception-json", required=True)
    parser.add_argument("--output-dir")
    return parser


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON in {path} must be an object")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture_frame(capture_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load_json_object(capture_dir / "manifest.json")
    if manifest.get("status") != "collected":
        raise ValueError("capture manifest status must be collected")
    artifact = (manifest.get("artifacts") or {}).get("native_video_frame_record")
    if not isinstance(artifact, Mapping):
        raise ValueError("capture manifest has no native frame record")
    record_path = capture_dir / str(artifact.get("path") or "")
    if _sha256_file(record_path) != str(artifact.get("sha256") or ""):
        raise ValueError("native frame record hash does not match capture manifest")
    record = _load_json_object(record_path)
    image = record.get("native_video_frame")
    if not isinstance(image, Mapping):
        raise ValueError("native frame record has no image")
    image_path = capture_dir / str(image.get("path") or "")
    if _sha256_file(image_path) != str(image.get("sha256") or ""):
        raise ValueError("native image hash does not match frame record")
    return manifest, record


def generate(args: argparse.Namespace) -> Path:
    capture_dir = Path(args.capture_dir).resolve()
    manifest, frame_record = _capture_frame(capture_dir)
    result = load_manual_current_frame_perception(
        Path(args.perception_json).resolve(),
        frame_index=int(frame_record["frame_index"]),
    )
    updater = PerceptionSceneGraphUpdater(backend_name="disabled")
    snapshot = updater._snapshot_from_result(
        result,
        context=None,
        skipped=False,
        force=True,
    )
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else capture_dir / "manual_current_frame_scene_graph"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    graph_path = output_dir / f"frame_{result.frame_index:06d}.scene_graph.json"
    if graph_path.exists():
        raise FileExistsError(f"refusing to overwrite existing graph: {graph_path}")
    graph_payload = snapshot.to_dict()
    graph_path.write_text(
        json.dumps(graph_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    record = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "generated",
        "capture_dir": str(capture_dir),
        "task": (manifest.get("task") or {}).get("requested"),
        "frame_index": result.frame_index,
        "perception_input": {
            "schema_version": MANUAL_CURRENT_FRAME_PERCEPTION_SCHEMA_VERSION,
            "path": str(Path(args.perception_json).resolve()),
            "sha256": _sha256_file(Path(args.perception_json).resolve()),
            "object_count": len(result.objects),
            "relation_count": len(result.relations),
        },
        "scene_graph": {"path": str(graph_path), "sha256": _sha256_file(graph_path)},
        "post_processing": {
            "perception_result_to_canonical_snapshot": True,
            "sam2_started": False,
            "unigoal_mapping_started": False,
            "reason": "manual input contains no masks or depth annotation",
        },
    }
    (output_dir / f"frame_{result.frame_index:06d}.manifest.json").write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return graph_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    graph_path = generate(build_parser().parse_args(argv))
    print(f"manual current-frame scene graph: {graph_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
