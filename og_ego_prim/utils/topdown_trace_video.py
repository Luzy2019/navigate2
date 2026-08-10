"""Generate top-down robot trace videos from execution diagnostics."""

from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
from typing import Any, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw


Pose = dict[str, Any]


def save_replay_topdown_video(
    *,
    scene: str,
    frame_records: Sequence[dict[str, Any]],
    output_dir: Path | str,
    repository_root: Optional[Path | str] = None,
    topdown_assets_dir: Optional[Path | str] = None,
    output_name: str = "replay_topdown.mp4",
    output_size: Optional[tuple[int, int]] = (1920, 1080),
    fps: float = 10.0,
) -> Optional[dict[str, Any]]:
    """Render one top-down frame for every recorded camera replay frame."""

    poses = _replay_frame_poses(frame_records)
    if not poses:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name
    repo = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[2]
    topdown_image_path, occupancy_metadata_path = _resolve_topdown_assets(
        repo,
        scene,
        topdown_assets_dir=topdown_assets_dir,
    )
    metadata = json.loads(occupancy_metadata_path.read_text(encoding="utf-8"))
    # Keep replay generation usable with legacy/partial runtime configs that
    # leave the optional artifact size unset.
    output_size = output_size or (1920, 1080)
    width, height = int(output_size[0]), int(output_size[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"topdown output size must be positive, got {output_size}")
    if float(fps) <= 0:
        raise ValueError(f"topdown fps must be positive, got {fps}")

    base_image = Image.open(topdown_image_path).convert("RGB").resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )
    projector = _WorldProjector(metadata, (width, height))
    _write_video(
        output_path=output_path,
        frames=(
            _draw_frame(
                base_image=base_image,
                projector=projector,
                poses=poses,
                frames=poses,
                frame_index=index,
                frame=pose,
                scene=scene,
            )
            for index, pose in enumerate(poses)
        ),
        output_size=(width, height),
        fps=float(fps),
    )
    return {
        "path": output_path.name,
        "kind": "replay_topdown",
        "fps": float(fps),
        "frames": len(poses),
        "width": width,
        "height": height,
        "abs_path": str(output_path),
        "source_topdown": _resource_identifier(topdown_image_path, repo),
        "source_occupancy_metadata": _resource_identifier(occupancy_metadata_path, repo),
        "synchronized_to": "camera_frame_index",
    }


def save_topdown_trace_video(
    *,
    scene: str,
    execution_diagnostics: Sequence[dict[str, Any]],
    output_dir: Path | str,
    repository_root: Optional[Path | str] = None,
    topdown_assets_dir: Optional[Path | str] = None,
    output_name: str = "topdown.mp4",
    output_size: tuple[int, int] = (1920, 1080),
    fps: float = 12.0,
) -> Optional[dict[str, Any]]:
    """Save a top-down mp4 with a robot position dot and heading cone."""
    poses = _extract_robot_poses(execution_diagnostics)
    if not poses:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name
    repo = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[2]

    topdown_image_path, occupancy_metadata_path = _resolve_topdown_assets(
        repo,
        scene,
        topdown_assets_dir=topdown_assets_dir,
    )
    metadata = json.loads(occupancy_metadata_path.read_text(encoding="utf-8"))
    width, height = int(output_size[0]), int(output_size[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"topdown output size must be positive, got {output_size}")
    base_image = Image.open(topdown_image_path).convert("RGB").resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )

    projector = _WorldProjector(metadata, (width, height))
    frames = _interpolate_poses(poses, fps=float(fps))
    _write_video(
        output_path=output_path,
        frames=(
            _draw_frame(
                base_image=base_image,
                projector=projector,
                poses=poses,
                frames=frames,
                frame_index=index,
                frame=frame,
                scene=scene,
            )
            for index, frame in enumerate(frames)
        ),
        output_size=(width, height),
        fps=float(fps),
    )

    return {
        "path": output_path.name,
        "kind": "topdown_trace",
        "fps": float(fps),
        "frames": len(frames),
        "width": width,
        "height": height,
        "abs_path": str(output_path),
        "source_topdown": _resource_identifier(topdown_image_path, repo),
        "source_occupancy_metadata": _resource_identifier(occupancy_metadata_path, repo),
    }


def _replay_frame_poses(frame_records: Sequence[dict[str, Any]]) -> list[Pose]:
    """Resolve exactly one pose per recorded camera frame when possible.

    A camera observation can occasionally arrive before the simulator exposes
    a valid robot pose.  Dropping that record would shift every subsequent
    top-down frame by one index, so unresolved prefix records are backfilled
    with the first valid pose observed later in the stream.
    """

    resolved: list[Optional[Pose]] = []
    last_pose: Optional[Pose] = None
    for record in frame_records or ():
        robot_pose = record.get("robot_pose")
        if not isinstance(robot_pose, dict):
            robot_pose = {}
        position = robot_pose.get("position")
        if position is None:
            position = robot_pose.get("base_position")
        orientation = robot_pose.get("orientation")
        if orientation is None:
            orientation = robot_pose.get("base_orientation")
        try:
            has_position = position is not None and len(position) >= 2
        except (TypeError, ValueError):
            has_position = False
        if has_position:
            try:
                label = record.get("action") or record.get("action_id") or "idle"
                last_pose = {
                    "x": float(position[0]),
                    "y": float(position[1]),
                    "yaw": _yaw_from_xyzw(orientation),
                    "label": str(label),
                }
            except (TypeError, ValueError, IndexError):
                # Keep the frame slot and use the previous pose below.
                pass
        resolved.append(None if last_pose is None else dict(last_pose))

    first_valid = next((pose for pose in resolved if pose is not None), None)
    if first_valid is None:
        # Without any pose there is no honest top-down location to render.
        return []
    return [dict(pose if pose is not None else first_valid) for pose in resolved]


class _WorldProjector:
    def __init__(self, metadata: dict[str, Any], output_size: tuple[int, int]):
        self.min_x, self.min_y, self.max_x, self.max_y = [
            float(value) for value in metadata["world_bounds"]
        ]
        source_width, source_height = [float(value) for value in metadata["output_size"]]
        scale_x = output_size[0] / source_width
        scale_y = output_size[1] / source_height
        left, top, _right, _bottom = [
            float(value) for value in metadata["padding_left_top_right_bottom"]
        ]
        content_width, content_height = [float(value) for value in metadata["content_size"]]
        self.left = left * scale_x
        self.top = top * scale_y
        self.content_width = content_width * scale_x
        self.content_height = content_height * scale_y
        self.pixels_per_meter = self.content_width / max(self.max_x - self.min_x, 1e-6)

    def world_to_px(self, x: float, y: float) -> tuple[float, float]:
        px = self.left + (float(x) - self.min_x) / (self.max_x - self.min_x) * self.content_width
        py = self.top + (self.max_y - float(y)) / (self.max_y - self.min_y) * self.content_height
        return px, py


def _resolve_topdown_assets(
    repository_root: Path,
    scene: str,
    *,
    topdown_assets_dir: Optional[Path | str] = None,
) -> tuple[Path, Path]:
    scene_topdown_dir = (
        Path(topdown_assets_dir)
        if topdown_assets_dir is not None
        else repository_root / "outputs" / "all_scene_topdowns" / scene
    )
    scene_metadata = scene_topdown_dir / "topdown_scene.json"
    occupancy_metadata = scene_topdown_dir / "occupancy_map.json"

    candidates: list[Path] = []
    if scene_metadata.exists():
        try:
            scene_payload = json.loads(scene_metadata.read_text(encoding="utf-8"))
            image_path = scene_payload.get("image")
            if image_path:
                image_path = Path(image_path)
                candidates.append(
                    image_path
                    if image_path.is_absolute()
                    else scene_topdown_dir / image_path
                )
            occupancy_payload = scene_payload.get("occupancy_metadata")
            if occupancy_payload:
                occupancy_path = Path(occupancy_payload)
                occupancy_metadata = (
                    occupancy_path
                    if occupancy_path.is_absolute()
                    else scene_topdown_dir / occupancy_path
                )
        except Exception:
            pass

    if not occupancy_metadata.exists():
        raise FileNotFoundError(f"topdown occupancy metadata not found: {occupancy_metadata}")

    candidates.extend(
        [
            scene_topdown_dir / f"{scene}_topdown_scene.png",
            scene_topdown_dir / "topdown_scene.png",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate, occupancy_metadata
    raise FileNotFoundError(
        f"topdown image not found for {scene}; checked: {', '.join(str(path) for path in candidates)}"
    )


def _resource_identifier(path: Path, repository_root: Path) -> str:
    """Return a portable resource label instead of leaking host filesystem paths."""

    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _extract_robot_poses(execution_diagnostics: Sequence[dict[str, Any]]) -> list[Pose]:
    poses: list[Pose] = []
    for diagnostic in execution_diagnostics or []:
        label = diagnostic.get("plan") or diagnostic.get("primitive_name") or "action"
        _append_state_pose(poses, diagnostic.get("start_state") or {}, label)

        navigation = diagnostic.get("navigation") or {}
        for waypoint in navigation.get("executed_waypoints_2d") or []:
            if len(waypoint) >= 3:
                _append_pose(poses, waypoint[0], waypoint[1], waypoint[2], label)

        end_state = diagnostic.get("end_state") or {}
        if end_state.get("base_position"):
            _append_state_pose(poses, end_state, label)
        elif navigation.get("end_base_pos"):
            position = navigation["end_base_pos"]
            yaw = poses[-1]["yaw"] if poses else 0.0
            _append_pose(poses, position[0], position[1], yaw, label)

    clean: list[Pose] = []
    for pose in poses:
        if (
            clean
            and abs(clean[-1]["x"] - pose["x"]) < 1e-4
            and abs(clean[-1]["y"] - pose["y"]) < 1e-4
            and abs(_normalize_angle(clean[-1]["yaw"] - pose["yaw"])) < 1e-4
        ):
            clean[-1]["label"] = pose["label"]
        else:
            clean.append(pose)
    return clean


def _append_state_pose(poses: list[Pose], state: dict[str, Any], label: str) -> None:
    position = state.get("base_position")
    if not position:
        return
    _append_pose(poses, position[0], position[1], _yaw_from_xyzw(state.get("base_orientation")), label)


def _append_pose(poses: list[Pose], x: Any, y: Any, yaw: Any, label: str) -> None:
    poses.append({"x": float(x), "y": float(y), "yaw": float(yaw), "label": str(label)})


def _interpolate_poses(poses: list[Pose], *, fps: float) -> list[Pose]:
    if len(poses) == 1:
        return poses * max(1, int(round(fps * 2)))

    frames: list[Pose] = []
    for current, following in zip(poses, poses[1:]):
        distance = math.hypot(following["x"] - current["x"], following["y"] - current["y"])
        steps = max(6, min(48, int(distance * 14) + 6))
        yaw_delta = _normalize_angle(following["yaw"] - current["yaw"])
        for index in range(steps):
            t = index / steps
            frames.append(
                {
                    "x": current["x"] * (1.0 - t) + following["x"] * t,
                    "y": current["y"] * (1.0 - t) + following["y"] * t,
                    "yaw": current["yaw"] + yaw_delta * t,
                    "label": following["label"],
                }
            )
    frames.append(poses[-1])
    frames.extend([poses[-1]] * max(1, int(round(fps * 2))))
    return frames


def _draw_frame(
    *,
    base_image: Image.Image,
    projector: _WorldProjector,
    poses: list[Pose],
    frames: list[Pose],
    frame_index: int,
    frame: Pose,
    scene: str,
) -> np.ndarray:
    image = base_image.copy().convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    trail = [projector.world_to_px(pose["x"], pose["y"]) for pose in frames[: frame_index + 1]]
    if len(trail) >= 2:
        draw.line(trail, fill=(0, 100, 255, 210), width=5, joint="curve")

    px, py = projector.world_to_px(frame["x"], frame["y"])
    yaw = frame["yaw"]
    cone_length = 2.4 * projector.pixels_per_meter
    cone_half_angle = math.radians(28.0)
    cone_points = [(px, py)]
    for index in range(25):
        angle = yaw - cone_half_angle + 2.0 * cone_half_angle * index / 24.0
        cone_points.append(
            (
                px + math.cos(angle) * cone_length,
                py - math.sin(angle) * cone_length,
            )
        )
    draw.polygon(cone_points, fill=(255, 180, 0, 70), outline=(255, 120, 0, 170))

    radius = 13
    draw.ellipse(
        (px - radius, py - radius, px + radius, py + radius),
        fill=(230, 0, 0, 255),
        outline=(255, 255, 255, 255),
        width=4,
    )
    heading_x = px + math.cos(yaw) * radius * 1.7
    heading_y = py - math.sin(yaw) * radius * 1.7
    draw.line((px, py, heading_x, heading_y), fill=(255, 255, 255, 255), width=4)

    start_x, start_y = projector.world_to_px(poses[0]["x"], poses[0]["y"])
    final_x, final_y = projector.world_to_px(poses[-1]["x"], poses[-1]["y"])
    draw.ellipse(
        (start_x - 9, start_y - 9, start_x + 9, start_y + 9),
        fill=(0, 180, 80, 255),
        outline=(255, 255, 255, 255),
        width=3,
    )
    draw.ellipse(
        (final_x - 17, final_y - 17, final_x + 17, final_y + 17),
        outline=(255, 0, 0, 255),
        width=5,
    )

    image = Image.alpha_composite(image, overlay)
    text_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_overlay, "RGBA")
    text_draw.rectangle((20, 18, 980, 112), fill=(0, 0, 0, 155))
    text_draw.text(
        (34, 30),
        f"{scene} | topdown robot trace | frame {frame_index + 1}/{len(frames)}",
        fill=(255, 255, 255, 255),
    )
    text_draw.text(
        (34, 58),
        f"pose=({frame['x']:.2f}, {frame['y']:.2f}) yaw={math.degrees(frame['yaw']):.1f} deg",
        fill=(255, 255, 255, 255),
    )
    text_draw.text((34, 86), f"action={frame['label']}", fill=(255, 230, 180, 255))
    image = Image.alpha_composite(image, text_overlay).convert("RGB")
    return np.ascontiguousarray(np.asarray(image, dtype=np.uint8))


def _write_video(
    *,
    output_path: Path,
    frames: Any,
    output_size: tuple[int, int],
    fps: float,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to save topdown videos")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = int(output_size[0]), int(output_size[1])
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
        str(float(fps)),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        for frame in frames:
            if frame.shape[:2] != (height, width):
                raise ValueError(f"topdown frame shape mismatch: {frame.shape}")
            process.stdin.write(frame.tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise
    if return_code != 0:
        raise RuntimeError(f"ffmpeg failed to save topdown video: {stderr.strip()}")


def _yaw_from_xyzw(quaternion: Optional[Sequence[Any]]) -> float:
    if quaternion is None:
        return 0.0
    try:
        if len(quaternion) != 4:
            return 0.0
    except (TypeError, ValueError):
        return 0.0
    x, y, z, w = [float(value) for value in quaternion]
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _normalize_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle
