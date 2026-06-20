from typing import Any, Optional

import numpy as np

from .perception import FrameObservation


def _to_numpy(value):
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def _to_rgb_uint8(rgb):
    rgb = _to_numpy(rgb)
    if rgb is None:
        return None
    if rgb.ndim == 3 and rgb.shape[-1] > 3:
        rgb = rgb[:, :, :3]
    if rgb.dtype != np.uint8:
        if rgb.max(initial=0) <= 1.0:
            rgb = rgb * 255.0
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb)


def _to_depth_float(depth):
    depth = _to_numpy(depth)
    if depth is None:
        return None
    if depth.ndim == 3 and depth.shape[-1] == 1:
        depth = depth[:, :, 0]
    return np.asarray(depth, dtype=np.float32)


def _to_builtin_position(position):
    if position is None:
        return None
    position = _to_numpy(position)
    return [float(x) for x in position.tolist()]


class ISBenchObservationAdapter:
    def __init__(self, sensor_name: Optional[str] = None):
        self.sensor_name = sensor_name
        self.frame_index = 0

    def reset(self):
        self.frame_index = 0

    def ensure_robot_sensor_modalities(self, env: Any):
        if not getattr(env, "robots", None):
            return
        robot = env.robots[0]
        for modality in ("rgb", "depth_linear", "depth", "camera_params"):
            try:
                robot.add_obs_modality(modality)
            except Exception:
                continue

    def observe(self, env: Any) -> FrameObservation:
        if not getattr(env, "robots", None):
            raise RuntimeError("ISBenchObservationAdapter requires at least one robot")

        robot = env.robots[0]
        self.ensure_robot_sensor_modalities(env)
        obs, _ = robot.get_obs()
        sensor_name, sensor_obs, sensor = self._select_vision_sensor(robot, obs)

        rgb = _to_rgb_uint8(sensor_obs.get("rgb"))
        if rgb is None:
            raise RuntimeError(f"sensor {sensor_name} did not return rgb")

        depth = _to_depth_float(sensor_obs.get("depth_linear"))
        if depth is None:
            depth = _to_depth_float(sensor_obs.get("depth"))

        intrinsics = self._get_intrinsics(sensor)
        camera_pose = self._get_camera_pose(sensor, sensor_obs)
        robot_position = self._get_robot_position(robot)

        frame = FrameObservation(
            frame_index=self.frame_index,
            rgb=rgb,
            depth=depth,
            intrinsics=intrinsics,
            camera_pose=camera_pose,
            robot_position=robot_position,
            sensor_name=sensor_name,
            metadata={
                "rgb_shape": list(rgb.shape),
                "depth_shape": None if depth is None else list(depth.shape),
            },
        )
        self.frame_index += 1
        return frame

    def _select_vision_sensor(self, robot, obs):
        sensors = getattr(robot, "sensors", {})
        if self.sensor_name is not None:
            if self.sensor_name not in obs:
                raise RuntimeError(f"robot observation has no sensor named {self.sensor_name}")
            return self.sensor_name, obs[self.sensor_name], sensors.get(self.sensor_name)

        for sensor_name, sensor_obs in obs.items():
            if isinstance(sensor_obs, dict) and "rgb" in sensor_obs:
                return sensor_name, sensor_obs, sensors.get(sensor_name)

        raise RuntimeError(f"no rgb vision sensor found in robot obs keys: {list(obs.keys())}")

    def _get_intrinsics(self, sensor):
        if sensor is None:
            return None
        try:
            return _to_numpy(sensor.intrinsic_matrix).astype(np.float32)
        except Exception:
            return None

    def _get_camera_pose(self, sensor, sensor_obs):
        camera_params = sensor_obs.get("camera_params") if isinstance(sensor_obs, dict) else None
        if isinstance(camera_params, dict) and "cameraViewTransform" in camera_params:
            view_transform = _to_numpy(camera_params["cameraViewTransform"])
            if view_transform is not None and view_transform.shape == (4, 4):
                try:
                    return np.linalg.inv(view_transform).astype(np.float32)
                except np.linalg.LinAlgError:
                    pass

        if sensor is None:
            return None
        try:
            position, _ = sensor.get_position_orientation()
        except Exception:
            return None

        pose = np.eye(4, dtype=np.float32)
        pose[:3, 3] = _to_numpy(position)[:3]
        return pose

    def _get_robot_position(self, robot):
        try:
            position, _ = robot.get_position_orientation()
        except Exception:
            return None
        return _to_builtin_position(position)
