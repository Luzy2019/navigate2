from typing import Any, Optional

import numpy as np

from .perception import FrameObservation


_OPTICAL_TO_USD_CAMERA = np.diag((1.0, -1.0, -1.0)).astype(np.float32)


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


# ===================================================
# OmniGibson robot.get_obs() 原始格式
#         ↓
# ISBenchObservationAdapter.observe(env)
#         ↓
# FrameObservation 统一格式
#         ↓
# SAMJAM / UniGoal / scene graph backend
# ===================================================
#
# IS-Bench scene graph 感知的观测适配器，关键作用是通过 observe(env)
# 从 OmniGibson 环境的第一个机器人读取相机观测，并统一封装成 FrameObservation。
#
# observe(env) 会完成以下事情：
# 1. 确保机器人开启 rgb、depth、camera_params 等视觉观测模态。
# 2. 从 robot.get_obs() 中选择一个 RGB 视觉传感器。
# 3. 提取 RGB、depth、相机内参、相机位姿、机器人位置和传感器名称。
# 4. 将这些信息打包成 FrameObservation，供后续 SAMJAM / UniGoal scene graph 后端使用。
#
# 使用方法：
#     adapter = ISBenchObservationAdapter(sensor_name=None)
#     adapter.reset()
#     frame = adapter.observe(env)
#
# 使用位置：
# - UniGoalGroundedSAMBackend 中创建 self.adapter，并在 observe(env) 中调用 self.adapter.observe(env)。
# - SAMJAMSAM2Backend 中创建 self.adapter，并在 observe(env) 中调用 self.adapter.observe(env)。
# - PerceptionSceneGraphUpdater._run_perception() 会调用 backend.observe(self.env)，间接触发这里的 observe(env)

class ISBenchObservationAdapter:
    
    def __init__(self, sensor_name: Optional[str] = None):
        self.sensor_name = sensor_name
        self.frame_index = 0

    def reset(self):
        self.frame_index = 0

    def ensure_robot_sensor_modalities(self, env: Any):
        '''
            确保环境中的第一个机器人开启 scene graph 感知需要的观测模态。
            这里会尝试添加 RGB、线性深度、普通深度和相机参数；如果环境没有机器人则直接返回，
            如果某个模态不被当前机器人/传感器支持，则忽略异常并继续尝试其他模态。
        '''
        if not getattr(env, "robots", None):
            return
        robot = env.robots[0]
        for modality in ("rgb", "depth_linear", "depth", "camera_params"):
            try:
                robot.add_obs_modality(modality)
            except Exception:
                continue

    # env是OmniGibson环境
    # self.env = og.Environment(configs=self.env_config)
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
        '''
        从 robot.get_obs() 返回的观测字典中选择用于 scene graph 感知的视觉传感器。
        如果初始化时指定了 self.sensor_name，则只使用指定传感器；否则自动选择第一个包含 rgb 的传感器。
        使用位置：由 ISBenchObservationAdapter.observe() 调用，用来得到 sensor_name、sensor_obs 和 sensor 对象。
        '''
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
        '''
        读取视觉传感器的相机内参矩阵，并转换成 numpy.float32 格式。
        使用位置：由 ISBenchObservationAdapter.observe() 调用，结果写入 FrameObservation.intrinsics，
        供 UniGoal / SAMJAM 等 scene graph 后端做几何投影或空间关系估计。
        '''
        if sensor is None:
            return None
        try:
            return _to_numpy(sensor.intrinsic_matrix).astype(np.float32)
        except Exception:
            return None

    def _get_camera_pose(self, sensor, sensor_obs):
        '''
        获取相机在场景中的 4x4 位姿矩阵。
        优先使用视觉传感器的 world position 和 orientation；如果传感器位姿不可用，
        再从 sensor_obs["camera_params"]["cameraViewTransform"] 求逆得到相机位姿。
        使用位置：由 ISBenchObservationAdapter.observe() 调用，结果写入 FrameObservation.camera_pose，
        供 scene graph 后端记录相机位置并辅助估计空间关系。
        '''
        if sensor is not None:
            try:
                import omnigibson.utils.transform_utils as T

                position, orientation = sensor.get_position_orientation()
                pose = np.eye(4, dtype=np.float32)
                pose[:3, :3] = (
                    _to_numpy(T.quat2mat(orientation)).astype(np.float32)
                    @ _OPTICAL_TO_USD_CAMERA
                )
                pose[:3, 3] = _to_numpy(position)[:3]
                return pose
            except Exception:
                pass

        camera_params = sensor_obs.get("camera_params") if isinstance(sensor_obs, dict) else None
        if isinstance(camera_params, dict) and "cameraViewTransform" in camera_params:
            view_transform = _to_numpy(camera_params["cameraViewTransform"])
            if view_transform is not None and view_transform.shape == (4, 4):
                try:
                    pose = np.linalg.inv(view_transform).astype(np.float32)
                    pose[:3, :3] = pose[:3, :3] @ _OPTICAL_TO_USD_CAMERA
                    return pose
                except np.linalg.LinAlgError:
                    pass

        return None

    def _get_robot_position(self, robot):
        '''
        读取机器人当前在场景中的位置，并转换成普通 Python list。
        使用位置：由 ISBenchObservationAdapter.observe() 调用，结果写入 FrameObservation.robot_position，
        供 perception backend / scene graph metadata 记录机器人所在位置。
        '''
        try:
            position, _ = robot.get_position_orientation()
        except Exception:
            return None
        return _to_builtin_position(position)
