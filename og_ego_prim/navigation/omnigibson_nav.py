import heapq
import math
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Generator, Iterable, List, Optional, Set, Tuple

import cv2
import torch
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from omnigibson.objects import StatefulObject
import omnigibson.utils.transform_utils as T
from omnigibson.action_primitives.starter_semantic_action_primitives import m

from og_ego_prim.config.runtime_config import NavigationConfig
from .base import NavigationBackend


class OmniGibsonNavigationBackend(NavigationBackend):
    """Navigation backend that uses OmniGibson traversability maps and stepwise base control."""

    def __init__(
        self,
        allow_native_fallback: bool = True,
        navigation_config: Optional[NavigationConfig] = None,
    ):
        # 自定义导航在采样目标点、规划路径或跟踪路径时失败后，是否退回
        # OmniGibson 原生的 _navigate_if_needed。ego 模式默认允许回退，
        # starter 模式会关闭回退，以免原生站位采样再次在拥挤场景中失败。
        self.allow_native_fallback = allow_native_fallback
        config = navigation_config or NavigationConfig()

        # 写入差速底盘 action 的归一化前进/旋转指令。这里控制的是每个仿真步
        # 的动作强度，而不是直接以 m/s、rad/s 表示的物理速度。
        self.linear_command = float(config.linear_command)
        self.angular_command = float(config.angular_command)

        # 连续多少步没有明显缩短距离或角度误差，就认为底盘可能被碰撞体卡住。
        # 后两个容差允许机器人已经非常接近目标时结束跟踪，避免物理抖动造成假失败。
        self.stuck_window = int(config.stuck_window)
        self.stuck_angle_tolerance = float(config.stuck_angle_tolerance)
        self.stuck_waypoint_tolerance = float(config.stuck_waypoint_tolerance)
        self.stuck_final_waypoint_tolerance = float(config.stuck_final_waypoint_tolerance)

        # 旋转过程中，连续停滞检测的每步最小进度阈值（弧度）。
        # 取值过大时正常慢速旋转可能被误判为"卡死"。
        self.stuck_angle_progress_threshold = float(config.stuck_angle_progress_threshold)
        self.final_approach_distance = float(config.final_approach_distance)

        # 机器人不能把底盘中心直接开到物体中心，因此按物体类型设置环形候选站位
        # 的最小半径。容器和普通柜体需要较大间距来避免底盘碰撞；tactqn 柜体的
        # 把手较低，需要允许 Fetch 站得更近，机械臂才能够到。
        self.container_min_goal_radius = float(config.container_min_goal_radius)
        self.cabinet_min_goal_radius = float(config.cabinet_min_goal_radius)
        self.tactqn_min_goal_radius = float(config.tactqn_min_goal_radius)
        self.goal_clearance_radius = float(config.goal_clearance_radius)
        self.trav_map_robot_radius_scale = float(config.trav_map_robot_radius_scale)
        self.trav_map_extra_erosion_margin = float(
            config.trav_map_extra_erosion_margin
        )
        self.clearance_aware_path = bool(config.clearance_aware_path)
        self.clearance_aware_desired_clearance = float(
            config.clearance_aware_desired_clearance
        )
        self.clearance_aware_weight = float(config.clearance_aware_weight)
        self.clearance_aware_simplify = bool(config.clearance_aware_simplify)
        self.rotate_when_already_in_navigation_region = bool(
            config.rotate_when_already_in_navigation_region
        )
        self.already_region_yaw_tolerance = float(
            config.already_region_yaw_tolerance
        )
        self.already_reachable_max_goal_radius = float(
            config.already_reachable_max_goal_radius
        )
        self.max_floor_height_delta = float(config.max_floor_height_delta)

        # prefer_target_reachable=True 时，会对路径可达的候选站位进一步做机械臂
        # 可达性检查；限制检查数量可以避免 IK 计算拖慢每次导航。
        self.max_ik_goal_checks = int(config.max_ik_goal_checks)

        # verbose 输出候选点、路径和跟踪过程；last_navigation_result 则保留最近
        # 一次导航的结构化诊断，供 Executor / tracker 写入评测报告。
        self.verbose = bool(config.verbose)
        self.last_navigation_result = None
        self._last_clearance_path_diagnostics = None

        # 在环境初始化阶段尽早拒绝不合理配置。卡死容差不能比 OmniGibson 正常
        # 到达阈值更严格，否则会出现“已停止进步、却永远达不到退出条件”的状态。
        if not 0.0 < self.linear_command <= 1.0:
            raise ValueError("navigation.linear_command must be in (0, 1]")
        if not 0.0 < self.angular_command <= 1.0:
            raise ValueError("navigation.angular_command must be in (0, 1]")
        if self.stuck_window <= 0:
            raise ValueError("navigation.stuck_window must be greater than zero")
        if self.stuck_angle_tolerance < m.DEFAULT_ANGLE_THRESHOLD:
            raise ValueError(
                "navigation.stuck_angle_tolerance must be greater than or equal to "
                "the default navigation angle threshold"
            )
        if self.stuck_waypoint_tolerance < m.DEFAULT_DIST_THRESHOLD:
            raise ValueError(
                "navigation.stuck_waypoint_tolerance must be greater than or equal to "
                "the default navigation distance threshold"
            )
        if self.stuck_final_waypoint_tolerance < self.stuck_waypoint_tolerance:
            raise ValueError(
                "navigation.stuck_final_waypoint_tolerance must be greater than or "
                "equal to navigation.stuck_waypoint_tolerance"
            )
        # 0.45 m 是当前 Fetch 底盘和后续操作共同采用的安全下限；小于该值的
        # 候选站位容易让底盘贴进目标物体或家具碰撞体。
        if self.container_min_goal_radius < 0.45:
            raise ValueError(
                "navigation.container_min_goal_radius must be at least 0.45"
            )
        if self.cabinet_min_goal_radius < 0.45:
            raise ValueError(
                "navigation.cabinet_min_goal_radius must be at least 0.45"
            )
        if self.tactqn_min_goal_radius < 0.45:
            raise ValueError(
                "navigation.tactqn_min_goal_radius must be at least 0.45"
            )
        if self.goal_clearance_radius < 0.0:
            raise ValueError("navigation.goal_clearance_radius must be non-negative")
        if not 0.0 < self.trav_map_robot_radius_scale <= 1.0:
            raise ValueError(
                "navigation.trav_map_robot_radius_scale must be in (0, 1]"
            )
        if self.trav_map_extra_erosion_margin < 0.0:
            raise ValueError(
                "navigation.trav_map_extra_erosion_margin must be non-negative"
            )
        if self.clearance_aware_desired_clearance < 0.0:
            raise ValueError(
                "navigation.clearance_aware_desired_clearance must be non-negative"
            )
        if self.clearance_aware_weight < 0.0:
            raise ValueError("navigation.clearance_aware_weight must be non-negative")
        if self.already_region_yaw_tolerance < m.DEFAULT_ANGLE_THRESHOLD:
            raise ValueError(
                "navigation.already_region_yaw_tolerance must be greater than "
                "or equal to the default navigation angle threshold"
            )
        if self.already_reachable_max_goal_radius < 0.45:
            raise ValueError(
                "navigation.already_reachable_max_goal_radius must be at least 0.45"
            )
        if self.max_floor_height_delta <= 0.0:
            raise ValueError("navigation.max_floor_height_delta must be positive")
        if self.max_ik_goal_checks <= 0:
            raise ValueError("navigation.max_ik_goal_checks must be positive")
        if self.final_approach_distance < 0.0:
            raise ValueError("navigation.final_approach_distance must be non-negative")

    def navigate_to_object(
        self,
        controller,
        target_obj: StatefulObject,
        target_pose=None,
        prefer_target_reachable: bool = False,
        preferred_goal_direction=None,
        minimum_goal_radius_override=None,
        maximum_goal_radius_override=None,
        require_goal_direction_aligned: bool = False,
        allow_unreachable_goal_fallback: bool = True,
        skip_if_already_satisfied: bool = True,
    ) -> Generator[torch.Tensor, None, None]:
        if target_obj is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot navigate to an unknown object.",
            )

        target_pose = target_pose or target_obj.get_position_orientation()
        target_pos = torch.as_tensor(target_pose[0], dtype=torch.float32)
        preferred_goal_direction = self._normalize_goal_direction(
            preferred_goal_direction
        )
        start_pos = controller.robot.get_position_orientation()[0]
        start_distance = torch.norm(start_pos[:2] - target_pos[:2]).item()
        start_reachable = self._safe_target_in_reach(controller, target_pose)
        start_reach_satisfied = (not prefer_target_reachable) or start_reachable
        start_direction_aligned = self._goal_direction_aligned(
            start_pos[:2],
            target_pos[:2],
            preferred_goal_direction,
        )
        minimum_goal_radius = (
            float(minimum_goal_radius_override)
            if minimum_goal_radius_override is not None
            else self._minimum_goal_radius(target_obj)
        )
        is_floor_target = self._is_floor_target(target_obj)
        if minimum_goal_radius < 0.45 and not is_floor_target:
            raise ValueError("minimum_goal_radius_override must be at least 0.45")
        maximum_goal_radius = (
            float(maximum_goal_radius_override)
            if maximum_goal_radius_override is not None
            else None
        )
        if (
            maximum_goal_radius is not None
            and maximum_goal_radius < minimum_goal_radius
        ):
            raise ValueError(
                "maximum_goal_radius_override must be greater than or equal to "
                "the effective minimum goal radius"
            )
        start_radius_satisfied = start_distance >= minimum_goal_radius - 0.03
        effective_start_max_goal_radius = (
            maximum_goal_radius
            if maximum_goal_radius is not None
            else self.already_reachable_max_goal_radius
        )
        start_max_radius_satisfied = (
            start_distance <= effective_start_max_goal_radius + 0.03
        )
        self._log(
            "start "
            f"target={target_obj.name} "
            f"target_pos={self._to_float_list(target_pos)} "
            f"base_pos={self._to_float_list(start_pos)} "
            f"base_target_xy_distance={start_distance:.3f} "
            f"already_reachable={start_reachable} "
            f"reach_satisfied={start_reach_satisfied} "
            f"direction_aligned={start_direction_aligned} "
            f"radius_satisfied={start_radius_satisfied} "
            f"max_radius_satisfied={start_max_radius_satisfied} "
            f"preferred_goal_direction={self._to_float_list(preferred_goal_direction)} "
            f"prefer_target_reachable={prefer_target_reachable} "
            f"maximum_goal_radius={maximum_goal_radius} "
            f"effective_start_max_goal_radius={effective_start_max_goal_radius} "
            f"already_reachable_max_goal_radius={self.already_reachable_max_goal_radius} "
            f"require_goal_direction_aligned={require_goal_direction_aligned} "
            f"allow_unreachable_goal_fallback={allow_unreachable_goal_fallback} "
            f"skip_if_already_satisfied={skip_if_already_satisfied}"
        )
        self.last_navigation_result = {
            "target_object": target_obj.name,
            "target_pos": self._to_float_list(target_pos),
            "start_base_pos": self._to_float_list(start_pos),
            "start_base_target_xy_distance": round(start_distance, 6),
            "start_reachable": start_reachable,
            "start_reach_satisfied": start_reach_satisfied,
            "start_direction_aligned": start_direction_aligned,
            "start_radius_satisfied": start_radius_satisfied,
            "start_max_radius_satisfied": start_max_radius_satisfied,
            "preferred_goal_direction": self._to_float_list(
                preferred_goal_direction
            ),
            "prefer_target_reachable": prefer_target_reachable,
            "minimum_goal_radius": round(minimum_goal_radius, 6),
            "minimum_goal_radius_override": (
                None
                if minimum_goal_radius_override is None
                else round(float(minimum_goal_radius_override), 6)
            ),
            "maximum_goal_radius": (
                None if maximum_goal_radius is None else round(maximum_goal_radius, 6)
            ),
            "effective_start_max_goal_radius": round(
                effective_start_max_goal_radius, 6
            ),
            "already_reachable_max_goal_radius": round(
                self.already_reachable_max_goal_radius, 6
            ),
            "maximum_goal_radius_override": (
                None
                if maximum_goal_radius_override is None
                else round(float(maximum_goal_radius_override), 6)
            ),
            "require_goal_direction_aligned": require_goal_direction_aligned,
            "allow_unreachable_goal_fallback": allow_unreachable_goal_fallback,
            "skip_if_already_satisfied": skip_if_already_satisfied,
            "status": "started",
        }
        if (
            skip_if_already_satisfied
            and start_reach_satisfied
            and start_direction_aligned
            and start_radius_satisfied
            and start_max_radius_satisfied
        ):
            goal_pose_2d = self._goal_pose_2d_from_candidate(
                start_pos[:2],
                target_pos,
            )
            should_rotate = self.rotate_when_already_in_navigation_region
            relaxed_rotation_error = None
            self.last_navigation_result.update(
                status=(
                    "rotating_already_reachable"
                    if should_rotate
                    else "skipped_already_reachable"
                ),
                goal_pose_2d=self._to_float_list(goal_pose_2d),
                sampled_goal_radius=round(start_distance, 6),
                path_points=0,
                path_length=0.0,
                waypoints=0,
                skipped_rotation=not should_rotate,
            )
            self._log(
                f"skip target={target_obj.name} "
                "reason=already_in_allowed_navigation_region "
                f"action={'rotate_to_face_target' if should_rotate else 'accept_current_pose'}"
            )
            if should_rotate:
                try:
                    yield from self._rotate_to_yaw(
                        controller,
                        float(goal_pose_2d[2]),
                        rotation_kind="already_reachable",
                    )
                    yield from controller._settle_robot()
                except ActionPrimitiveError as exc:
                    current_quat = controller.robot.get_position_orientation()[1]
                    current_yaw = T.quat2euler(current_quat)[2].item()
                    yaw_error = abs(
                        self._normalize_angle(float(goal_pose_2d[2]) - current_yaw)
                    )
                    if yaw_error > self.already_region_yaw_tolerance:
                        raise
                    relaxed_rotation_error = str(exc)
                    self._log(
                        "skip "
                        f"accepted_after_relaxed_rotation target={target_obj.name} "
                        f"yaw_error={yaw_error:.3f} "
                        f"tolerance={self.already_region_yaw_tolerance:.3f}"
                    )
                    empty_action = controller._empty_action()
                    yield controller._postprocess_action(empty_action)
                    yield from controller._settle_robot()
            end_pos = controller.robot.get_position_orientation()[0]
            end_reachable = self._safe_target_in_reach(controller, target_pose)
            end_distance = torch.norm(end_pos[:2] - target_pos[:2]).item()
            end_direction_aligned = self._goal_direction_aligned(
                end_pos[:2],
                target_pos[:2],
                preferred_goal_direction,
            )
            self.last_navigation_result.update(
                status="skipped_already_reachable",
                end_base_pos=self._to_float_list(end_pos),
                end_base_target_xy_distance=round(end_distance, 6),
                end_reachable=end_reachable,
                end_direction_aligned=end_direction_aligned,
                relaxed_rotation_error=relaxed_rotation_error,
            )
            return

        try:
            self._last_clearance_path_diagnostics = None
            goal_pose_2d = self._sample_goal_pose_near_object(
                controller,
                target_obj,
                target_pose=target_pose,
                prefer_target_reachable=prefer_target_reachable,
                minimum_goal_radius=minimum_goal_radius,
                maximum_goal_radius=maximum_goal_radius,
                preferred_goal_direction=preferred_goal_direction,
                require_goal_direction_aligned=require_goal_direction_aligned,
                allow_unreachable_goal_fallback=allow_unreachable_goal_fallback,
            )
            path_world = self._plan_path_to_goal(controller, goal_pose_2d)
            self._log(
                "plan "
                f"target={target_obj.name} "
                f"goal_pose_2d={self._to_float_list(goal_pose_2d)} "
                f"path_points={int(path_world.shape[0]) if path_world.ndim > 1 else 1} "
                f"path_length={self._path_length(path_world):.3f}"
            )
            waypoints = self._path_world_to_waypoints(
                path_world,
                goal_pose_2d,
                target_pose[0],
            )
            self.last_navigation_result.update(
                status="following_path",
                goal_pose_2d=self._to_float_list(goal_pose_2d),
                sampled_goal_radius=round(
                    torch.norm(goal_pose_2d[:2] - target_pos[:2]).item(), 6
                ),
                path_points=int(path_world.shape[0]) if path_world.ndim > 1 else 1,
                path_length=round(self._path_length(path_world), 6),
                waypoints=len(waypoints),
                planned_path_world=[
                    self._to_float_list(point[:2]) for point in path_world
                ],
                planned_waypoints_2d=[
                    self._to_float_list(waypoint) for waypoint in waypoints
                ],
                clearance_plan=self._last_clearance_path_diagnostics,
            )
            yield from self._follow_waypoints(controller, waypoints)

            end_pos = controller.robot.get_position_orientation()[0]
            end_reachable = self._safe_target_in_reach(controller, target_pose)
            end_distance = torch.norm(end_pos[:2] - target_pos[:2]).item()
            end_direction_aligned = self._goal_direction_aligned(
                end_pos[:2],
                target_pos[:2],
                preferred_goal_direction,
            )
            self.last_navigation_result.update(
                status="completed",
                end_base_pos=self._to_float_list(end_pos),
                end_base_target_xy_distance=round(end_distance, 6),
                end_reachable=end_reachable,
                end_direction_aligned=end_direction_aligned,
            )
            self._log(
                "finish "
                f"target={target_obj.name} "
                f"base_pos={self._to_float_list(end_pos)} "
                f"base_target_xy_distance={end_distance:.3f} "
                f"reachable={end_reachable} "
                f"direction_aligned={end_direction_aligned}"
            )
        except Exception as exc:
            self.last_navigation_result.update(
                status="failed",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            if not self.allow_native_fallback:
                raise
            self._log(
                f"fallback target={target_obj.name} "
                "reason=custom_navigation_failed"
            )
            yield from controller._navigate_if_needed(target_obj)

    def _sample_goal_pose_near_object(
        self,
        controller,
        target_obj: StatefulObject,
        target_pose=None,
        prefer_target_reachable: bool = False,
        minimum_goal_radius: float = 0.45,
        maximum_goal_radius: Optional[float] = None,
        preferred_goal_direction=None,
        require_goal_direction_aligned: bool = False,
        allow_unreachable_goal_fallback: bool = True,
    ) -> torch.Tensor:
        target_pos = torch.as_tensor(
            (target_pose or target_obj.get_position_orientation())[0],
            dtype=torch.float32,
        )
        goal_pose_2d = self._find_reachable_goal_pose_near_target(
            controller,
            target_pos,
            target_pose=target_pose if prefer_target_reachable else None,
            minimum_goal_radius=minimum_goal_radius,
            maximum_goal_radius=maximum_goal_radius,
            preferred_goal_direction=preferred_goal_direction,
            require_goal_direction_aligned=require_goal_direction_aligned,
            allow_unreachable_goal_fallback=allow_unreachable_goal_fallback,
        )
        return goal_pose_2d

    def _find_reachable_goal_pose_near_target(
        self,
        controller,
        target_pos: torch.Tensor,
        target_pose=None,
        minimum_goal_radius: float = 0.45,
        maximum_goal_radius: Optional[float] = None,
        preferred_goal_direction=None,
        require_goal_direction_aligned: bool = False,
        allow_unreachable_goal_fallback: bool = True,
    ) -> torch.Tensor:
        trav_map = self.env.scene.trav_map
        floor = self._get_current_floor(controller)
        robot_pos = controller.robot.get_position_orientation()[0]
        source_world = robot_pos[:2]
        candidate_goal_direction = preferred_goal_direction
        if candidate_goal_direction is None:
            candidate_goal_direction = self._normalize_goal_direction(
                source_world - target_pos[:2]
            )

        candidates = self._candidate_goal_positions_near_target(
            controller,
            floor,
            target_pos[:2],
            minimum_goal_radius=minimum_goal_radius,
            maximum_goal_radius=maximum_goal_radius,
            preferred_goal_direction=candidate_goal_direction,
        )
        first_traversable_pose = None
        num_path_reachable = 0
        num_ik_reachable = 0
        num_direction_rejected = 0
        traversable = None
        reachable_map_cells = None
        if self.clearance_aware_path:
            traversable = self._eroded_floor_map(controller, floor)
            source_map = self._nearest_free_map_cell(
                traversable,
                trav_map.world_to_map(source_world),
            )
            reachable_map_cells = self._reachable_free_map_cells(
                traversable,
                source_map,
            )

        for candidate_xy in candidates:
            if require_goal_direction_aligned and not self._goal_direction_aligned(
                candidate_xy,
                target_pos[:2],
                candidate_goal_direction,
            ):
                num_direction_rejected += 1
                continue

            if reachable_map_cells is not None:
                candidate_map = self._valid_traversable_map_cell(
                    trav_map,
                    traversable,
                    candidate_xy,
                )
                path_reachable = candidate_map in reachable_map_cells
            else:
                path_reachable = (
                    self._plan_path_between(
                        controller,
                        floor,
                        source_world,
                        candidate_xy,
                    )
                    is not None
                )

            if path_reachable:
                num_path_reachable += 1
                goal_pose_2d = self._goal_pose_2d_from_candidate(
                    candidate_xy,
                    target_pos,
                )

                if first_traversable_pose is None:
                    first_traversable_pose = goal_pose_2d

                if target_pose is None:
                    self._log(
                        "candidate "
                        f"selected_goal={self._to_float_list(goal_pose_2d)} "
                        f"path_reachable_candidates={num_path_reachable} "
                        "ik_reachable=not_required"
                    )
                    return goal_pose_2d

                if num_path_reachable > self.max_ik_goal_checks:
                    self._log(
                        "candidate "
                        f"ik_check_limit_reached={self.max_ik_goal_checks} "
                        f"using_first_traversable_goal={allow_unreachable_goal_fallback}"
                    )
                    break

                if self._target_reachable_from_goal_pose(
                    controller,
                    target_pose,
                    goal_pose_2d,
                ):
                    num_ik_reachable += 1
                    self._log(
                        "candidate "
                        f"selected_goal={self._to_float_list(goal_pose_2d)} "
                        f"path_reachable_candidates={num_path_reachable} "
                        f"ik_reachable_candidates={num_ik_reachable}"
                    )
                    return goal_pose_2d

        if first_traversable_pose is not None and allow_unreachable_goal_fallback:
            self._log(
                "candidate "
                f"selected_goal={self._to_float_list(first_traversable_pose)} "
                f"path_reachable_candidates={num_path_reachable} "
                "ik_reachable_candidates=0 "
                "reason=no_ik_reachable_goal_found_using_first_traversable"
            )
            return first_traversable_pose

        if first_traversable_pose is not None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Could not find an IK-reachable traversable goal near the target "
                "object, and unreachable-goal fallback is disabled.",
                {
                    "target pos": target_pos.tolist(),
                    "minimum goal radius": minimum_goal_radius,
                    "maximum goal radius": maximum_goal_radius,
                    "path reachable candidates": num_path_reachable,
                    "direction rejected candidates": num_direction_rejected,
                },
            )

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PLANNING_ERROR,
            "Could not find a reachable traversable goal near the target object.",
            {
                "target pos": target_pos.tolist(),
                "minimum goal radius": minimum_goal_radius,
                "maximum goal radius": maximum_goal_radius,
                "direction rejected candidates": num_direction_rejected,
            },
        )

    @staticmethod
    def _reachable_free_map_cells(
        traversable: torch.Tensor,
        source_map: Optional[Tuple[int, int]],
    ) -> Set[Tuple[int, int]]:
        if source_map is None:
            return set()

        height, width = traversable.shape
        reachable = {source_map}
        frontier = deque([source_map])
        while frontier:
            row, col = frontier.popleft()
            for row_delta, col_delta in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = row + row_delta, col + col_delta
                if (
                    neighbor in reachable
                    or neighbor[0] < 0
                    or neighbor[1] < 0
                    or neighbor[0] >= height
                    or neighbor[1] >= width
                    or int(traversable[neighbor]) != 255
                ):
                    continue
                reachable.add(neighbor)
                frontier.append(neighbor)
        return reachable

    def _candidate_goal_positions_near_target(
        self,
        controller,
        floor: int,
        target_xy: torch.Tensor,
        minimum_goal_radius: float = 0.45,
        maximum_goal_radius: Optional[float] = None,
        preferred_goal_direction=None,
    ) -> List[torch.Tensor]:
        trav_map = self.env.scene.trav_map
        traversable = self._eroded_floor_map(controller, floor)
        candidates: List[torch.Tensor] = []
        seen_map_cells: Set[Tuple[int, int]] = set()

        min_radius = minimum_goal_radius
        max_radius = 2.25 if maximum_goal_radius is None else maximum_goal_radius
        radius_step = 0.15
        num_angles = 32
        angle_offsets = self._ordered_angle_offsets(
            num_angles,
            preferred_goal_direction,
        )
        num_full_steps = int(
            math.floor((max_radius - min_radius) / radius_step + 1e-9)
        )
        radii = [
            min_radius + radius_idx * radius_step
            for radius_idx in range(num_full_steps + 1)
        ]
        if max_radius - radii[-1] > 1e-6:
            radii.append(max_radius)

        for radius in radii:
            for angle in angle_offsets:
                candidate_xy = target_xy + torch.tensor(
                    [math.cos(angle) * radius, math.sin(angle) * radius],
                    dtype=torch.float32,
                )
                map_cell = self._valid_traversable_map_cell(
                    trav_map,
                    traversable,
                    candidate_xy,
                )
                if map_cell is None or map_cell in seen_map_cells:
                    continue

                seen_map_cells.add(map_cell)
                candidates.append(torch.as_tensor(candidate_xy, dtype=torch.float32))

        if candidates:
            return candidates

        return self._nearest_traversable_positions(
            trav_map,
            traversable,
            target_xy,
            max_candidates=96,
            min_radius=min_radius,
            max_radius=max_radius,
        )

    @staticmethod
    def _normalize_goal_direction(direction):
        if direction is None:
            return None
        direction = torch.as_tensor(direction, dtype=torch.float32)[:2]
        norm = torch.norm(direction)
        if float(norm.item()) < 1e-6:
            return None
        return direction / norm

    @staticmethod
    def _goal_direction_aligned(base_xy, target_xy, preferred_direction) -> bool:
        if preferred_direction is None:
            return True
        actual_direction = torch.as_tensor(
            base_xy, dtype=torch.float32
        ) - torch.as_tensor(target_xy, dtype=torch.float32)
        norm = torch.norm(actual_direction)
        if float(norm.item()) < 1e-6:
            return False
        actual_direction = actual_direction / norm
        return float(torch.dot(actual_direction, preferred_direction).item()) >= 0.85

    @staticmethod
    def _ordered_angle_offsets(num_angles: int, preferred_direction):
        if preferred_direction is None:
            return [2.0 * math.pi * index / num_angles for index in range(num_angles)]

        preferred_angle = math.atan2(
            float(preferred_direction[1]),
            float(preferred_direction[0]),
        )
        index_offsets = [0]
        for step in range(1, num_angles // 2):
            index_offsets.extend((step, -step))
        index_offsets.append(num_angles // 2)
        return [
            preferred_angle + 2.0 * math.pi * offset / num_angles
            for offset in index_offsets
        ]

    def _eroded_floor_map(
        self,
        controller,
        floor: int,
    ) -> torch.Tensor:
        trav_map = self.env.scene.trav_map
        floor_map = torch.clone(trav_map.floor_map[floor])
        return trav_map._erode_trav_map(
            floor_map,
            robot=self._navigation_footprint_robot(controller),
        )

    def _navigation_footprint_robot(self, controller):
        if (
            self.trav_map_robot_radius_scale >= 0.999
            and self.trav_map_extra_erosion_margin <= 0.0
        ):
            return controller.robot

        extent = getattr(controller.robot, "reset_joint_pos_aabb_extent", None)
        if extent is None:
            return controller.robot
        extent = torch.as_tensor(extent, dtype=torch.float32).clone()
        if extent.numel() < 2:
            return controller.robot

        extent[:2] *= self.trav_map_robot_radius_scale
        radius = float(torch.norm(extent[:2]).item()) * 0.5
        if radius > 1e-6 and self.trav_map_extra_erosion_margin > 0.0:
            extent[:2] *= (
                radius + self.trav_map_extra_erosion_margin
            ) / radius
        return SimpleNamespace(reset_joint_pos_aabb_extent=extent)

    def save_debug_artifacts(self, controller, output_dir) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        trav_map = self.env.scene.trav_map
        footprint_robot = self._navigation_footprint_robot(controller)
        extent = torch.as_tensor(
            footprint_robot.reset_joint_pos_aabb_extent,
            dtype=torch.float32,
        )
        effective_radius = float(torch.norm(extent[:2]).item()) * 0.5
        map_resolution = float(trav_map.map_resolution)
        kernel_size = int(math.ceil(effective_radius / map_resolution))
        floors = []

        for floor, raw_floor_map in enumerate(trav_map.floor_map):
            raw_path = output_dir / f"trav_map_floor{floor}_raw.png"
            eroded_path = output_dir / f"trav_map_floor{floor}_eroded.png"
            eroded_floor_map = self._eroded_floor_map(controller, floor)
            raw_array = raw_floor_map.detach().cpu().numpy().astype("uint8")
            eroded_array = eroded_floor_map.detach().cpu().numpy().astype("uint8")
            if not cv2.imwrite(str(raw_path), raw_array):
                raise RuntimeError(f"Could not save raw traversability map: {raw_path}")
            if not cv2.imwrite(str(eroded_path), eroded_array):
                raise RuntimeError(
                    f"Could not save eroded traversability map: {eroded_path}"
                )
            floors.append(
                {
                    "floor": floor,
                    "raw_path": raw_path.name,
                    "eroded_path": eroded_path.name,
                    "shape": list(raw_array.shape),
                    "raw_free_cells": int((raw_array == 255).sum()),
                    "eroded_free_cells": int((eroded_array == 255).sum()),
                }
            )

        return {
            "trav_map_robot_radius_scale": self.trav_map_robot_radius_scale,
            "trav_map_extra_erosion_margin": self.trav_map_extra_erosion_margin,
            "effective_erosion_radius": round(effective_radius, 6),
            "erosion_kernel_size_pixels": kernel_size,
            "map_resolution": map_resolution,
            "clearance_aware_desired_clearance": self.clearance_aware_desired_clearance,
            "clearance_aware_weight": self.clearance_aware_weight,
            "clearance_aware_simplify": self.clearance_aware_simplify,
            "floors": floors,
        }

    def _valid_traversable_map_cell(
        self,
        trav_map,
        traversable: torch.Tensor,
        xy_world: torch.Tensor,
    ) -> Optional[Tuple[int, int]]:
        cell = self._traversable_map_cell(trav_map, traversable, xy_world)
        if cell is None:
            return None
        row, col = cell
        if not self._has_traversable_clearance(
            traversable,
            row,
            col,
            float(trav_map.map_resolution),
        ):
            return None
        return row, col

    @staticmethod
    def _traversable_map_cell(
        trav_map,
        traversable: torch.Tensor,
        xy_world: torch.Tensor,
    ) -> Optional[Tuple[int, int]]:
        map_xy = trav_map.world_to_map(xy_world)
        row = int(map_xy[0])
        col = int(map_xy[1])
        height, width = traversable.shape
        if row < 0 or col < 0 or row >= height or col >= width:
            return None
        if int(traversable[row, col]) != 255:
            return None
        return row, col

    def _has_traversable_clearance(
        self,
        traversable: torch.Tensor,
        row: int,
        col: int,
        map_resolution: float,
    ) -> bool:
        if self.goal_clearance_radius <= 0.0:
            return True

        radius_px = int(math.ceil(self.goal_clearance_radius / map_resolution))
        height, width = traversable.shape
        row_min = row - radius_px
        row_max = row + radius_px + 1
        col_min = col - radius_px
        col_max = col + radius_px + 1
        if row_min < 0 or col_min < 0 or row_max > height or col_max > width:
            return False

        window = traversable[row_min:row_max, col_min:col_max]
        return bool(torch.all(window == 255).item())

    def _nearest_traversable_positions(
        self,
        trav_map,
        traversable: torch.Tensor,
        target_xy: torch.Tensor,
        max_candidates: int,
        min_radius: float = 0.0,
        max_radius: float = 2.5,
    ) -> List[torch.Tensor]:
        target_map = trav_map.world_to_map(target_xy)
        target_row = int(target_map[0])
        target_col = int(target_map[1])
        max_radius_px = int(math.ceil(2.5 / trav_map.map_resolution))
        height, width = traversable.shape
        row_min = max(0, target_row - max_radius_px)
        row_max = min(height, target_row + max_radius_px + 1)
        col_min = max(0, target_col - max_radius_px)
        col_max = min(width, target_col + max_radius_px + 1)

        window = traversable[row_min:row_max, col_min:col_max]
        free_cells = torch.nonzero(window == 255, as_tuple=False)
        if free_cells.numel() == 0:
            return []

        free_cells = free_cells + torch.tensor([row_min, col_min])
        distances = torch.norm(
            (free_cells - target_map).to(dtype=torch.float32),
            dim=1,
        )
        distances_m = distances * float(trav_map.map_resolution)
        valid_distance = (distances_m >= min_radius) & (distances_m <= max_radius)
        free_cells = free_cells[valid_distance]
        distances = distances[valid_distance]
        if free_cells.numel() == 0:
            return []
        if self.goal_clearance_radius > 0.0:
            clearance_mask = torch.tensor(
                [
                    self._has_traversable_clearance(
                        traversable,
                        int(cell[0]),
                        int(cell[1]),
                        float(trav_map.map_resolution),
                    )
                    for cell in free_cells
                ],
                dtype=torch.bool,
            )
            free_cells = free_cells[clearance_mask]
            distances = distances[clearance_mask]
            if free_cells.numel() == 0:
                return []
        sorted_indices = torch.argsort(distances)[:max_candidates]
        return [
            torch.as_tensor(trav_map.map_to_world(free_cells[idx]), dtype=torch.float32)
            for idx in sorted_indices
        ]

    def _goal_pose_2d_from_candidate(
        self,
        candidate_xy: torch.Tensor,
        target_pos: torch.Tensor,
    ) -> torch.Tensor:
        target_yaw = math.atan2(
            float(target_pos[1] - candidate_xy[1]),
            float(target_pos[0] - candidate_xy[0]),
        )
        return torch.tensor(
            [candidate_xy[0], candidate_xy[1], target_yaw],
            dtype=torch.float32,
        )

    def _target_reachable_from_goal_pose(
        self,
        controller,
        target_pose,
        goal_pose_2d: torch.Tensor,
    ) -> bool:
        try:
            robot_pose = controller._get_robot_pose_from_2d_pose(goal_pose_2d)
            relative_target_pose = T.relative_pose_transform(
                *target_pose,
                *robot_pose,
            )
            return bool(
                controller._target_in_reach_of_robot_relative(relative_target_pose)
            )
        except Exception:
            return False

    def _get_current_floor(self, controller) -> int:
        robot_pos = controller.robot.get_position_orientation()[0]
        floor_heights = getattr(self.env.scene.trav_map, "floor_heights", None)
        if not floor_heights:
            return 0

        floor_diffs = [abs(float(robot_pos[2]) - float(height)) for height in floor_heights]
        return int(min(range(len(floor_diffs)), key=floor_diffs.__getitem__))

    def _minimum_goal_radius(self, target_obj: StatefulObject) -> float:
        category = getattr(target_obj, "category", "")
        model = getattr(target_obj, "model", "")
        if self._is_floor_target(target_obj):
            return 0.0
        if category in {"trash_can", "ashcan"}:
            return self.container_min_goal_radius
        if category == "top_cabinet" and model == "tactqn":
            # This fixed cabinet's visible handle is only about 0.31 m above
            # the floor. Fetch needs to stand closer than the generic cabinet
            # clearance in order for its arm to reach that low target.
            return self.tactqn_min_goal_radius
        if "cabinet" in category:
            return self.cabinet_min_goal_radius
        return 0.45

    @staticmethod
    def _is_floor_target(target_obj: StatefulObject) -> bool:
        category = (getattr(target_obj, "category", "") or "").lower()
        return category in {"floor", "floors"} or category.startswith("floor")

    def _plan_path_to_goal(
        self,
        controller,
        goal_pose_2d: Iterable[float],
    ) -> torch.Tensor:
        floor = self._get_current_floor(controller)
        robot_pos = controller.robot.get_position_orientation()[0]
        source_world = robot_pos[:2]
        goal_pose_2d = torch.as_tensor(goal_pose_2d, dtype=torch.float32)
        target_world = goal_pose_2d[:2]

        final_approach_path = self._plan_path_with_final_approach(
            controller,
            floor,
            source_world,
            goal_pose_2d,
        )
        if final_approach_path is not None:
            return final_approach_path

        path_world = self._plan_path_between(
            controller,
            floor,
            source_world,
            target_world,
        )
        if path_world is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Could not find a traversable path to the target position.",
                {"target pose": list(goal_pose_2d)},
            )

        return path_world

    def _plan_path_between(
        self,
        controller,
        floor: int,
        source_world: torch.Tensor,
        target_world: torch.Tensor,
    ) -> Optional[torch.Tensor]:
        if self.clearance_aware_path:
            return self._clearance_aware_path(
                controller,
                floor,
                source_world,
                target_world,
            )

        path_world, _ = self.env.scene.trav_map.get_shortest_path(
            floor=floor,
            source_world=source_world,
            target_world=target_world,
            entire_path=True,
            robot=self._navigation_footprint_robot(controller),
        )
        if path_world is None:
            return None
        return torch.as_tensor(path_world, dtype=torch.float32)

    def _plan_path_with_final_approach(
        self,
        controller,
        floor: int,
        source_world: torch.Tensor,
        goal_pose_2d: torch.Tensor,
    ) -> Optional[torch.Tensor]:
        """Plan to an approach point, then enter the final pose facing the target.

        The traversability planner reasons mostly about base-center xy.  If the
        shortest path reaches the final xy from the target side, Fetch can arrive
        almost exactly 180 degrees away from the desired manipulation/perception
        yaw and then fail while trying to spin in place in a narrow spot.  This
        method inserts a short final segment aligned with the requested final yaw
        so the robot turns before entering the tight goal pose.
        """
        if self.final_approach_distance <= 0.0:
            return None

        trav_map = self.env.scene.trav_map
        goal_xy = goal_pose_2d[:2]
        final_yaw = float(goal_pose_2d[2])
        final_direction = torch.tensor(
            [math.cos(final_yaw), math.sin(final_yaw)],
            dtype=torch.float32,
        )
        approach_xy = goal_xy - final_direction * self.final_approach_distance
        traversable = self._eroded_floor_map(controller, floor)

        if (
            self._valid_traversable_map_cell(
                trav_map,
                traversable,
                approach_xy,
            )
            is None
        ):
            self._log(
                "plan final_approach_unavailable "
                "reason=approach_cell_not_traversable "
                f"approach_xy={self._to_float_list(approach_xy)}"
            )
            return None

        if not self._line_segment_traversable(
            trav_map,
            traversable,
            approach_xy,
            goal_xy,
        ):
            self._log(
                "plan final_approach_unavailable "
                "reason=approach_segment_not_traversable "
                f"approach_xy={self._to_float_list(approach_xy)} "
                f"goal_xy={self._to_float_list(goal_xy)}"
            )
            return None

        path_world = self._plan_path_between(
            controller,
            floor,
            source_world,
            approach_xy,
        )
        if path_world is None:
            self._log(
                "plan final_approach_unavailable "
                "reason=no_path_to_approach "
                f"approach_xy={self._to_float_list(approach_xy)}"
            )
            return None

        if path_world.ndim == 1:
            path_world = path_world.unsqueeze(0)

        if (
            torch.norm(path_world[-1, :2] - approach_xy).item()
            > m.DEFAULT_DIST_THRESHOLD
        ):
            path_world = torch.cat((path_world, approach_xy.unsqueeze(0)), dim=0)
        if (
            torch.norm(path_world[-1, :2] - goal_xy).item()
            > m.DEFAULT_DIST_THRESHOLD
        ):
            path_world = torch.cat((path_world, goal_xy.unsqueeze(0)), dim=0)

        self._log(
            "plan final_approach_selected "
            f"approach_xy={self._to_float_list(approach_xy)} "
            f"goal_xy={self._to_float_list(goal_xy)} "
            f"final_yaw={final_yaw:.3f} "
            f"approach_distance={self.final_approach_distance:.3f}"
        )
        return path_world

    def _clearance_aware_path(
        self,
        controller,
        floor: int,
        source_world: torch.Tensor,
        target_world: torch.Tensor,
    ) -> Optional[torch.Tensor]:
        diagnostics = {
            "planner": "clearance_aware_astar",
            "floor": int(floor),
            "source_world": self._to_float_list(source_world),
            "target_world": self._to_float_list(target_world),
            "desired_clearance": self.clearance_aware_desired_clearance,
            "clearance_weight": self.clearance_aware_weight,
            "simplify": self.clearance_aware_simplify,
            "extra_erosion_margin": self.trav_map_extra_erosion_margin,
        }
        self._last_clearance_path_diagnostics = diagnostics
        if not self.clearance_aware_path:
            diagnostics["status"] = "disabled"
            return None

        trav_map = self.env.scene.trav_map
        traversable = self._eroded_floor_map(controller, floor)
        source_map = self._nearest_free_map_cell(
            traversable,
            trav_map.world_to_map(source_world),
        )
        target_map = self._nearest_free_map_cell(
            traversable,
            trav_map.world_to_map(target_world),
        )
        if source_map is None or target_map is None:
            diagnostics.update(
                status="missing_free_endpoint",
                source_map=None if source_map is None else list(source_map),
                target_map=None if target_map is None else list(target_map),
            )
            return None
        diagnostics.update(
            source_map=list(source_map),
            target_map=list(target_map),
            eroded_free_cells=int((traversable == 255).sum().item()),
        )

        path_map = self._clearance_aware_astar(
            traversable,
            source_map,
            target_map,
            map_resolution=float(trav_map.map_resolution),
        )
        if path_map is None:
            diagnostics["status"] = "no_path"
            return None

        free_map = (traversable.detach().cpu().numpy() == 255).astype("uint8")
        clearance_px = cv2.distanceTransform(free_map, cv2.DIST_L2, 3)
        path_clearances = [
            float(clearance_px[row, col]) * float(trav_map.map_resolution)
            for row, col in path_map
        ]
        diagnostics.update(
            status="planned",
            astar_grid_points=len(path_map),
            astar_path_map=[list(cell) for cell in path_map],
            astar_min_clearance=round(min(path_clearances), 6),
            astar_mean_clearance=round(
                sum(path_clearances) / len(path_clearances),
                6,
            ),
        )

        path_world = torch.as_tensor(
            trav_map.map_to_world(torch.tensor(path_map, dtype=torch.int64)),
            dtype=torch.float32,
        )
        waypoint_interval = max(int(getattr(trav_map, "waypoint_interval", 1)), 1)
        path_world = path_world[::waypoint_interval]
        if path_world.shape[0] == 0:
            return None
        if torch.norm(path_world[-1, :2] - target_world[:2]).item() > m.DEFAULT_DIST_THRESHOLD:
            path_world = torch.cat(
                (path_world, torch.as_tensor(target_world[:2], dtype=torch.float32).unsqueeze(0)),
                dim=0,
            )
        diagnostics["sampled_path_world"] = [
            self._to_float_list(point[:2]) for point in path_world
        ]
        if self.clearance_aware_simplify:
            path_world = self._simplify_path_world(
                trav_map,
                traversable,
                path_world,
            )
        diagnostics["planned_path_world"] = [
            self._to_float_list(point[:2]) for point in path_world
        ]
        return path_world

    def _clearance_aware_astar(
        self,
        traversable: torch.Tensor,
        source_map,
        target_map,
        map_resolution: float,
    ) -> Optional[List[Tuple[int, int]]]:
        free_map = (traversable.detach().cpu().numpy() == 255).astype("uint8")
        clearance_px = cv2.distanceTransform(free_map, cv2.DIST_L2, 3)
        rows, cols = free_map.shape
        start = (int(source_map[0]), int(source_map[1]))
        goal = (int(target_map[0]), int(target_map[1]))
        if start == goal:
            return [start]

        desired_clearance_px = min(
            8.0,
            self.clearance_aware_desired_clearance / max(map_resolution, 1e-6),
        )
        clearance_weight = self.clearance_aware_weight
        neighbors = (
            (1, 0, 1.0),
            (-1, 0, 1.0),
            (0, 1, 1.0),
            (0, -1, 1.0),
            (1, 1, math.sqrt(2.0)),
            (-1, -1, math.sqrt(2.0)),
            (1, -1, math.sqrt(2.0)),
            (-1, 1, math.sqrt(2.0)),
        )

        def heuristic(cell: Tuple[int, int]) -> float:
            return math.hypot(cell[0] - goal[0], cell[1] - goal[1])

        def clearance_penalty(cell: Tuple[int, int]) -> float:
            if desired_clearance_px <= 0.0 or clearance_weight <= 0.0:
                return 0.0
            clearance = float(clearance_px[cell[0], cell[1]])
            shortfall = max(0.0, desired_clearance_px - clearance)
            return clearance_weight * (shortfall / desired_clearance_px) ** 2

        open_set = [(heuristic(start), 0.0, start)]
        came_from = {}
        best_cost = {start: 0.0}
        closed = set()

        while open_set:
            _, current_cost, current = heapq.heappop(open_set)
            if current_cost > best_cost.get(current, float("inf")) + 1e-6:
                continue
            if current in closed:
                continue
            if current == goal:
                path = [current]
                while current in came_from:
                    current = came_from[current]
                    path.insert(0, current)
                return path
            closed.add(current)

            for row_delta, col_delta, step_cost in neighbors:
                neighbor = (current[0] + row_delta, current[1] + col_delta)
                if (
                    neighbor[0] < 0
                    or neighbor[1] < 0
                    or neighbor[0] >= rows
                    or neighbor[1] >= cols
                    or free_map[neighbor] == 0
                    or neighbor in closed
                ):
                    continue
                if (
                    row_delta != 0
                    and col_delta != 0
                    and (
                        free_map[current[0] + row_delta, current[1]] == 0
                        or free_map[current[0], current[1] + col_delta] == 0
                    )
                ):
                    continue
                tentative_cost = (
                    current_cost
                    + step_cost
                    + clearance_penalty(neighbor)
                )
                if tentative_cost >= best_cost.get(neighbor, float("inf")):
                    continue
                best_cost[neighbor] = tentative_cost
                came_from[neighbor] = current
                heapq.heappush(
                    open_set,
                    (tentative_cost + heuristic(neighbor), tentative_cost, neighbor),
                )

        return None

    def _simplify_path_world(
        self,
        trav_map,
        traversable: torch.Tensor,
        path_world: torch.Tensor,
    ) -> torch.Tensor:
        path_world = torch.as_tensor(path_world, dtype=torch.float32)
        if path_world.ndim == 1:
            return path_world.unsqueeze(0)
        if path_world.shape[0] <= 2:
            return path_world

        simplified = [path_world[0]]
        anchor_index = 0
        last_index = path_world.shape[0] - 1
        while anchor_index < last_index:
            best_index = anchor_index + 1
            for candidate_index in range(last_index, anchor_index, -1):
                if self._line_segment_traversable(
                    trav_map,
                    traversable,
                    path_world[anchor_index],
                    path_world[candidate_index],
                    require_goal_clearance=False,
                ):
                    best_index = candidate_index
                    break
            simplified.append(path_world[best_index])
            anchor_index = best_index

        return torch.stack(simplified, dim=0)

    @staticmethod
    def _nearest_free_map_cell(
        traversable: torch.Tensor,
        map_xy,
        max_radius_px: int = 12,
    ) -> Optional[Tuple[int, int]]:
        row = int(map_xy[0])
        col = int(map_xy[1])
        height, width = traversable.shape
        if (
            0 <= row < height
            and 0 <= col < width
            and int(traversable[row, col]) == 255
        ):
            return row, col

        best_cell = None
        best_distance = float("inf")
        for radius in range(1, max_radius_px + 1):
            row_min = max(0, row - radius)
            row_max = min(height - 1, row + radius)
            col_min = max(0, col - radius)
            col_max = min(width - 1, col + radius)
            for candidate_row in range(row_min, row_max + 1):
                for candidate_col in range(col_min, col_max + 1):
                    if int(traversable[candidate_row, candidate_col]) != 255:
                        continue
                    distance = math.hypot(candidate_row - row, candidate_col - col)
                    if distance < best_distance:
                        best_distance = distance
                        best_cell = (candidate_row, candidate_col)
            if best_cell is not None:
                return best_cell

        return None

    def _line_segment_traversable(
        self,
        trav_map,
        traversable: torch.Tensor,
        start_xy: torch.Tensor,
        end_xy: torch.Tensor,
        require_goal_clearance: bool = True,
    ) -> bool:
        start_xy = torch.as_tensor(start_xy, dtype=torch.float32)[:2]
        end_xy = torch.as_tensor(end_xy, dtype=torch.float32)[:2]
        delta_xy = end_xy - start_xy
        distance = torch.norm(delta_xy).item()
        if distance < 1e-6:
            return True

        map_resolution = max(float(trav_map.map_resolution), 1e-6)
        step = max(map_resolution * 0.5, 0.025)
        num_segments = max(1, int(math.ceil(distance / step)))
        for index in range(num_segments + 1):
            alpha = float(index) / float(num_segments)
            sample_xy = start_xy + delta_xy * alpha
            if (
                (
                    self._valid_traversable_map_cell(
                        trav_map,
                        traversable,
                        sample_xy,
                    )
                    if require_goal_clearance
                    else self._traversable_map_cell(
                        trav_map,
                        traversable,
                        sample_xy,
                    )
                )
                is None
            ):
                return False
        return True

    def _path_world_to_waypoints(
        self,
        path_world: torch.Tensor,
        goal_pose_2d: Iterable[float],
        target_obj_pos: torch.Tensor,
    ) -> list[torch.Tensor]:
        if path_world.ndim == 1:
            path_world = path_world.unsqueeze(0)

        goal_pose_2d = torch.as_tensor(goal_pose_2d, dtype=torch.float32)
        target_obj_pos = torch.as_tensor(target_obj_pos, dtype=torch.float32)
        if torch.norm(path_world[-1, :2] - goal_pose_2d[:2]) > m.DEFAULT_DIST_THRESHOLD:
            path_world = torch.cat((path_world, goal_pose_2d[:2].unsqueeze(0)), dim=0)

        waypoints = []

        for i, waypoint_xy in enumerate(path_world):
            if i < path_world.shape[0] - 1:
                next_xy = path_world[i + 1]
                yaw = math.atan2(
                    float(next_xy[1] - waypoint_xy[1]),
                    float(next_xy[0] - waypoint_xy[0]),
                )
            else:
                yaw = math.atan2(
                    float(target_obj_pos[1] - waypoint_xy[1]),
                    float(target_obj_pos[0] - waypoint_xy[0]),
                )
                if torch.norm(target_obj_pos[:2] - waypoint_xy[:2]) < 1e-6:
                    yaw = float(goal_pose_2d[2])

            waypoints.append(
                torch.tensor(
                    [waypoint_xy[0], waypoint_xy[1], yaw],
                    dtype=torch.float32,
                )
            )

        if not waypoints:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "The traversability planner returned an empty path.",
                {"target pose": list(goal_pose_2d)},
            )

        return waypoints

    def _follow_waypoints(
        self,
        controller,
        waypoints: list[torch.Tensor],
    ) -> Generator[torch.Tensor, None, None]:
        current_xy = controller.robot.get_position_orientation()[0][:2]
        filtered_waypoints = [
            waypoint
            for index, waypoint in enumerate(waypoints)
            if index == len(waypoints) - 1
            or torch.norm(waypoint[:2] - current_xy).item() >= 0.2
        ]
        self._log(
            "follow "
            f"raw_waypoints={len(waypoints)} "
            f"filtered_waypoints={len(filtered_waypoints)} "
            f"final_waypoint={self._to_float_list(waypoints[-1])}"
        )
        for filtered_index, waypoint in enumerate(filtered_waypoints):
            is_final_waypoint = filtered_index == len(filtered_waypoints) - 1
            yield from self._drive_towards_waypoint(
                controller,
                waypoint[:2],
                stuck_tolerance=(
                    self.stuck_final_waypoint_tolerance
                    if is_final_waypoint
                    else self.stuck_waypoint_tolerance
                ),
                waypoint_kind="final" if is_final_waypoint else "intermediate",
            )

        yield from self._rotate_to_yaw(
            controller,
            float(waypoints[-1][2]),
            rotation_kind="final",
        )

        yield from controller._settle_robot()

    def _drive_towards_waypoint(
        self,
        controller,
        waypoint_xy: torch.Tensor,
        stuck_tolerance: Optional[float] = None,
        waypoint_kind: str = "intermediate",
    ) -> Generator[torch.Tensor, None, None]:
        if stuck_tolerance is None:
            stuck_tolerance = self.stuck_waypoint_tolerance

        best_distance = float("inf")
        steps_without_progress = 0
        for _ in range(m.MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            self._raise_if_robot_left_floor(
                controller,
                phase="drive",
                waypoint=waypoint_xy,
            )
            robot_pos, robot_quat = controller.robot.get_position_orientation()
            delta_xy = waypoint_xy - robot_pos[:2]
            distance = torch.norm(delta_xy).item()
            if distance < m.DEFAULT_DIST_THRESHOLD:
                empty_action = controller._empty_action()
                yield controller._postprocess_action(empty_action)
                return

            if distance < best_distance - 0.01:
                best_distance = distance
                steps_without_progress = 0
            else:
                steps_without_progress += 1
            if steps_without_progress >= self.stuck_window:
                if distance <= stuck_tolerance:
                    self._log(
                        "waypoint "
                        f"accepted_after_stuck distance={distance:.3f} "
                        f"tolerance={stuck_tolerance:.3f} "
                        f"kind={waypoint_kind} "
                        f"waypoint={self._to_float_list(waypoint_xy)}"
                    )
                    empty_action = controller._empty_action()
                    yield controller._postprocess_action(empty_action)
                    return
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Robot base is not making translation progress toward the waypoint.",
                    {
                        "waypoint": waypoint_xy.tolist(),
                        "remaining distance": distance,
                        "linear command": self.linear_command,
                        "waypoint kind": waypoint_kind,
                        "stuck waypoint tolerance": stuck_tolerance,
                    },
                )

            target_yaw = math.atan2(float(delta_xy[1]), float(delta_xy[0]))
            current_yaw = T.quat2euler(robot_quat)[2].item()
            yaw_error = self._normalize_angle(target_yaw - current_yaw)
            if abs(yaw_error) > m.DEFAULT_ANGLE_THRESHOLD:
                yield from self._rotate_to_yaw(
                    controller,
                    target_yaw,
                    rotation_kind=f"{waypoint_kind}_drive_alignment",
                )
                continue

            action = controller._empty_action()
            base_action = action[controller.robot.controller_action_idx["base"]]
            if base_action.numel() != 2:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "This navigation backend currently only supports 2D differential-drive base actions.",
                    {"base action size": int(base_action.numel())},
                )

            base_action[0] = self.linear_command
            base_action[1] = 0.0
            action[controller.robot.controller_action_idx["base"]] = base_action
            yield controller._postprocess_action(action)

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Could not drive to the navigation waypoint.",
            {"waypoint": waypoint_xy.tolist()},
        )

    def _rotate_to_yaw(
        self,
        controller,
        target_yaw: float,
        rotation_kind: str = "intermediate",
    ) -> Generator[torch.Tensor, None, None]:
        """Rotate the robot base in-place to @target_yaw.

        If the shorter rotation direction gets stuck (no angular progress for
        ``stuck_window`` consecutive steps), the method automatically tries the
        opposite (longer) direction before raising an error.  This avoids
        false-positive failures when one side of the robot is blocked by
        furniture.
        """
        first_direction = None  # recorded on first attempt
        for direction_attempt in range(2):
            best_error = float("inf")
            steps_without_progress = 0
            reversed_direction = direction_attempt > 0
            if reversed_direction:
                self._log(
                    "rotation "
                    f"reversing_direction kind={rotation_kind} "
                    f"reason=stuck_in_original_direction"
                )
            for _ in range(m.MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
                self._raise_if_robot_left_floor(controller, phase="rotate")
                _, robot_quat = controller.robot.get_position_orientation()
                current_yaw = T.quat2euler(robot_quat)[2].item()
                yaw_error = self._normalize_angle(target_yaw - current_yaw)
                if abs(yaw_error) < m.DEFAULT_ANGLE_THRESHOLD:
                    empty_action = controller._empty_action()
                    yield controller._postprocess_action(empty_action)
                    if reversed_direction:
                        self._log(
                            "rotation "
                            f"succeeded_after_reverse kind={rotation_kind}"
                        )
                    return

                abs_error = abs(yaw_error)
                if abs_error < best_error - self.stuck_angle_progress_threshold:
                    best_error = abs_error
                    steps_without_progress = 0
                else:
                    steps_without_progress += 1
                if steps_without_progress >= self.stuck_window:
                    if abs_error <= self.stuck_angle_tolerance:
                        empty_action = controller._empty_action()
                        yield controller._postprocess_action(empty_action)
                        return
                    # If we haven't tried the reverse direction yet, break out
                    # of this inner loop and try the opposite.
                    if not reversed_direction:
                        break
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.EXECUTION_ERROR,
                        "Robot base is not making rotational progress toward the waypoint.",
                        {
                            "target yaw": target_yaw,
                            "current yaw": current_yaw,
                            "yaw error": yaw_error,
                            "angular command": self.angular_command,
                            "robot contact pairs": self._robot_contact_pairs(
                                controller
                            ),
                        },
                    )

                action = controller._empty_action()
                base_action = action[controller.robot.controller_action_idx["base"]]
                if base_action.numel() != 2:
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.EXECUTION_ERROR,
                        "This navigation backend currently only supports 2D differential-drive base actions.",
                        {"base action size": int(base_action.numel())},
                    )

                # First attempt: choose the shorter arc direction.  When the
                # error is near +/-pi, the normalized sign can flip due to tiny
                # yaw changes, so keep the initial direction until we move away
                # from that ambiguous boundary.  Second attempt forces the
                # opposite direction in case the first side is physically blocked.
                if first_direction is None:
                    first_direction = -1.0 if yaw_error < 0.0 else 1.0
                if reversed_direction:
                    direction = -first_direction
                elif abs_error > math.pi - 0.05:
                    direction = first_direction
                else:
                    direction = -1.0 if yaw_error < 0.0 else 1.0
                ang_vel = self.angular_command * direction
                base_action[0] = 0.0
                base_action[1] = ang_vel
                action[controller.robot.controller_action_idx["base"]] = base_action
                yield controller._postprocess_action(action)

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Could not rotate to the desired yaw during navigation.",
            {"target yaw": target_yaw},
        )

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        return math.atan2(math.sin(angle), math.cos(angle))

    def _raise_if_robot_left_floor(self, controller, phase: str, waypoint=None):
        robot_pos = controller.robot.get_position_orientation()[0]
        robot_z = float(robot_pos[2])
        if not math.isfinite(robot_z):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Robot base height became non-finite during navigation.",
                {"phase": phase, "base position": self._to_float_list(robot_pos)},
            )

        floor_heights = getattr(self.env.scene.trav_map, "floor_heights", None)
        expected_z = 0.0
        floor = 0
        if floor_heights:
            floor = self._get_current_floor(controller)
            expected_z = float(floor_heights[floor])

        height_delta = robot_z - expected_z
        if (
            abs(height_delta) > 0.05
            and isinstance(self.last_navigation_result, dict)
            and "first_elevated_base_contact" not in self.last_navigation_result
        ):
            elevated_contact = {
                "phase": phase,
                "base_position": self._to_float_list(robot_pos),
                "height_delta": height_delta,
                "robot_contact_pairs": self._robot_contact_pairs(controller),
            }
            self.last_navigation_result["first_elevated_base_contact"] = (
                elevated_contact
            )
            print(
                "[navigation][contact_diagnostic] first_elevated_base_contact "
                f"details={elevated_contact}"
            )
            sys.stdout.flush()
        if abs(height_delta) <= self.max_floor_height_delta:
            return

        details = {
            "phase": phase,
            "floor": floor,
            "base position": self._to_float_list(robot_pos),
            "expected floor height": expected_z,
            "height delta": height_delta,
            "max floor height delta": self.max_floor_height_delta,
        }
        if waypoint is not None:
            details["waypoint"] = self._to_float_list(waypoint)
        details["robot contact pairs"] = self._robot_contact_pairs(controller)
        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Robot base left the traversable floor during navigation.",
            details,
        )

    @staticmethod
    def _robot_contact_pairs(controller, max_pairs: int = 24):
        """Return compact raw PhysX contact paths for navigation failures."""
        try:
            robot_prim_path = str(controller.robot.prim_path)
            pairs = []
            seen = set()
            for contact in controller.robot.contact_list():
                body0 = str(contact.body0)
                body1 = str(contact.body1)
                if robot_prim_path not in body0 and robot_prim_path not in body1:
                    continue
                pair = (body0, body1)
                if pair in seen:
                    continue
                seen.add(pair)
                pairs.append({"body0": body0, "body1": body1})
                if len(pairs) >= max_pairs:
                    break
            return pairs
        except Exception as exc:
            return [{"contact_diagnostic_error": f"{type(exc).__name__}: {exc}"}]

    @staticmethod
    def _safe_target_in_reach(controller, target_pose) -> bool:
        try:
            return bool(controller._target_in_reach_of_robot(target_pose))
        except Exception:
            return False

    @staticmethod
    def _path_length(path_world: torch.Tensor) -> float:
        if path_world.ndim == 1 or path_world.shape[0] < 2:
            return 0.0
        deltas = path_world[1:, :2] - path_world[:-1, :2]
        return float(torch.norm(deltas, dim=1).sum().item())

    @staticmethod
    def _to_float_list(value):
        if value is None:
            return None
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "tolist"):
            value = value.tolist()
        return [round(float(item), 6) for item in value]

    def _log(self, message: str):
        if self.verbose:
            print(f"[starter][navigation_backend] {message}")
