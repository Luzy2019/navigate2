import math
import os
import sys
from typing import Generator, Iterable, List, Optional, Set, Tuple

import torch
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from omnigibson.objects import StatefulObject
import omnigibson.utils.transform_utils as T
from omnigibson.action_primitives.starter_semantic_action_primitives import m

from .base import NavigationBackend


class OmniGibsonNavigationBackend(NavigationBackend):
    """Navigation backend that uses OmniGibson traversability maps and stepwise base control."""

    def __init__(self, allow_native_fallback: bool = True):
        # 自定义导航在采样目标点、规划路径或跟踪路径时失败后，是否退回
        # OmniGibson 原生的 _navigate_if_needed。ego 模式默认允许回退，
        # starter 模式会关闭回退，以免原生站位采样再次在拥挤场景中失败。
        self.allow_native_fallback = allow_native_fallback

        # 写入差速底盘 action 的归一化前进/旋转指令。这里控制的是每个仿真步
        # 的动作强度，而不是直接以 m/s、rad/s 表示的物理速度。
        self.linear_command = float(os.environ.get("ISBENCH_NAV_LINEAR_COMMAND", "0.5"))
        self.angular_command = float(os.environ.get("ISBENCH_NAV_ANGULAR_COMMAND", "0.5"))

        # 连续多少步没有明显缩短距离或角度误差，就认为底盘可能被碰撞体卡住。
        # 后两个容差允许机器人已经非常接近目标时结束跟踪，避免物理抖动造成假失败。
        self.stuck_window = int(os.environ.get("ISBENCH_NAV_STUCK_WINDOW", "60"))
        self.stuck_angle_tolerance = float(
            os.environ.get("ISBENCH_NAV_STUCK_ANGLE_TOLERANCE", "0.25")
        )
        self.stuck_waypoint_tolerance = float(
            os.environ.get("ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE", "0.10")
        )
        self.stuck_final_waypoint_tolerance = float(
            os.environ.get(
                "ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE",
                str(max(self.stuck_waypoint_tolerance, 0.30)),
            )
        )

        # 机器人不能把底盘中心直接开到物体中心，因此按物体类型设置环形候选站位
        # 的最小半径。容器和普通柜体需要较大间距来避免底盘碰撞；tactqn 柜体的
        # 把手较低，需要允许 Fetch 站得更近，机械臂才能够到。
        self.container_min_goal_radius = float(
            os.environ.get("ISBENCH_NAV_CONTAINER_MIN_GOAL_RADIUS", "0.80")
        )
        self.cabinet_min_goal_radius = float(
            os.environ.get("ISBENCH_NAV_CABINET_MIN_GOAL_RADIUS", "0.70")
        )
        self.tactqn_min_goal_radius = float(
            os.environ.get("ISBENCH_NAV_TACTQN_MIN_GOAL_RADIUS", "0.45")
        )
        self.goal_clearance_radius = float(
            os.environ.get("ISBENCH_NAV_GOAL_CLEARANCE_RADIUS", "0.25")
        )
        self.max_floor_height_delta = float(
            os.environ.get("ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA", "0.35")
        )

        # prefer_target_reachable=True 时，会对路径可达的候选站位进一步做机械臂
        # 可达性检查；限制检查数量可以避免 IK 计算拖慢每次导航。
        self.max_ik_goal_checks = int(
            os.environ.get("ISBENCH_NAV_MAX_IK_GOAL_CHECKS", "8")
        )

        # verbose 输出候选点、路径和跟踪过程；last_navigation_result 则保留最近
        # 一次导航的结构化诊断，供 Executor / tracker 写入评测报告。
        self.verbose = os.environ.get("ISBENCH_NAV_VERBOSE", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.last_navigation_result = None

        # 在环境初始化阶段尽早拒绝不合理配置。卡死容差不能比 OmniGibson 正常
        # 到达阈值更严格，否则会出现“已停止进步、却永远达不到退出条件”的状态。
        if not 0.0 < self.linear_command <= 1.0:
            raise ValueError("ISBENCH_NAV_LINEAR_COMMAND must be in (0, 1]")
        if not 0.0 < self.angular_command <= 1.0:
            raise ValueError("ISBENCH_NAV_ANGULAR_COMMAND must be in (0, 1]")
        if self.stuck_window <= 0:
            raise ValueError("ISBENCH_NAV_STUCK_WINDOW must be greater than zero")
        if self.stuck_angle_tolerance < m.DEFAULT_ANGLE_THRESHOLD:
            raise ValueError(
                "ISBENCH_NAV_STUCK_ANGLE_TOLERANCE must be greater than or equal to "
                "the default navigation angle threshold"
            )
        if self.stuck_waypoint_tolerance < m.DEFAULT_DIST_THRESHOLD:
            raise ValueError(
                "ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE must be greater than or equal to "
                "the default navigation distance threshold"
            )
        if self.stuck_final_waypoint_tolerance < self.stuck_waypoint_tolerance:
            raise ValueError(
                "ISBENCH_NAV_STUCK_FINAL_WAYPOINT_TOLERANCE must be greater than or "
                "equal to ISBENCH_NAV_STUCK_WAYPOINT_TOLERANCE"
            )

        # 0.45 m 是当前 Fetch 底盘和后续操作共同采用的安全下限；小于该值的
        # 候选站位容易让底盘贴进目标物体或家具碰撞体。
        if self.container_min_goal_radius < 0.45:
            raise ValueError(
                "ISBENCH_NAV_CONTAINER_MIN_GOAL_RADIUS must be at least 0.45"
            )
        if self.cabinet_min_goal_radius < 0.45:
            raise ValueError(
                "ISBENCH_NAV_CABINET_MIN_GOAL_RADIUS must be at least 0.45"
            )
        if self.tactqn_min_goal_radius < 0.45:
            raise ValueError(
                "ISBENCH_NAV_TACTQN_MIN_GOAL_RADIUS must be at least 0.45"
            )
        if self.goal_clearance_radius < 0.0:
            raise ValueError("ISBENCH_NAV_GOAL_CLEARANCE_RADIUS must be non-negative")
        if self.max_floor_height_delta <= 0.0:
            raise ValueError("ISBENCH_NAV_MAX_FLOOR_HEIGHT_DELTA must be positive")
        if self.max_ik_goal_checks <= 0:
            raise ValueError("ISBENCH_NAV_MAX_IK_GOAL_CHECKS must be positive")

    def navigate_to_object(
        self,
        controller,
        target_obj: StatefulObject,
        target_pose=None,
        prefer_target_reachable: bool = False,
        preferred_goal_direction=None,
        minimum_goal_radius_override=None,
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
        if minimum_goal_radius < 0.45:
            raise ValueError("minimum_goal_radius_override must be at least 0.45")
        start_radius_satisfied = (
            minimum_goal_radius_override is None
            or start_distance >= minimum_goal_radius - 0.03
        )
        self._log(
            "start "
            f"target={target_obj.name} "
            f"target_pos={self._to_float_list(target_pos)} "
            f"base_pos={self._to_float_list(start_pos)} "
            f"base_target_xy_distance={start_distance:.3f} "
            f"already_reachable={start_reachable} "
            f"direction_aligned={start_direction_aligned} "
            f"radius_satisfied={start_radius_satisfied} "
            f"preferred_goal_direction={self._to_float_list(preferred_goal_direction)} "
            f"prefer_target_reachable={prefer_target_reachable}"
        )
        self.last_navigation_result = {
            "target_object": target_obj.name,
            "target_pos": self._to_float_list(target_pos),
            "start_base_pos": self._to_float_list(start_pos),
            "start_base_target_xy_distance": round(start_distance, 6),
            "start_reachable": start_reachable,
            "start_direction_aligned": start_direction_aligned,
            "start_radius_satisfied": start_radius_satisfied,
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
            "status": "started",
        }
        if start_reachable and start_direction_aligned and start_radius_satisfied:
            self.last_navigation_result.update(status="skipped_already_reachable")
            self._log(f"skip target={target_obj.name} reason=already_reachable")
            return

        try:
            goal_pose_2d = self._sample_goal_pose_near_object(
                controller,
                target_obj,
                target_pose=target_pose,
                prefer_target_reachable=prefer_target_reachable,
                minimum_goal_radius=minimum_goal_radius,
                preferred_goal_direction=preferred_goal_direction,
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
        preferred_goal_direction=None,
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
            preferred_goal_direction=preferred_goal_direction,
        )
        return goal_pose_2d

    def _find_reachable_goal_pose_near_target(
        self,
        controller,
        target_pos: torch.Tensor,
        target_pose=None,
        minimum_goal_radius: float = 0.45,
        preferred_goal_direction=None,
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
            preferred_goal_direction=candidate_goal_direction,
        )
        first_traversable_pose = None
        first_traversable_path_length = None
        num_path_reachable = 0
        num_ik_reachable = 0

        for candidate_xy in candidates:
            path_world, _ = trav_map.get_shortest_path(
                floor=floor,
                source_world=source_world,
                target_world=candidate_xy,
                entire_path=True,
                robot=controller.robot,
            )
            if path_world is not None:
                num_path_reachable += 1
                goal_pose_2d = self._goal_pose_2d_from_candidate(
                    candidate_xy,
                    target_pos,
                )
                path_length = self._path_length(torch.as_tensor(path_world, dtype=torch.float32))

                if first_traversable_pose is None:
                    first_traversable_pose = goal_pose_2d
                    first_traversable_path_length = path_length

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
                        "using_first_traversable_goal=True"
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
                        f"ik_reachable_candidates={num_ik_reachable} "
                        f"path_length={path_length:.3f}"
                    )
                    return goal_pose_2d

        if first_traversable_pose is not None:
            self._log(
                "candidate "
                f"selected_goal={self._to_float_list(first_traversable_pose)} "
                f"path_reachable_candidates={num_path_reachable} "
                "ik_reachable_candidates=0 "
                "reason=no_ik_reachable_goal_found_using_first_traversable "
                f"path_length={first_traversable_path_length:.3f}"
            )
            return first_traversable_pose

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PLANNING_ERROR,
            "Could not find a reachable traversable goal near the target object.",
            {"target pos": target_pos.tolist()},
        )

    def _candidate_goal_positions_near_target(
        self,
        controller,
        floor: int,
        target_xy: torch.Tensor,
        minimum_goal_radius: float = 0.45,
        preferred_goal_direction=None,
    ) -> List[torch.Tensor]:
        trav_map = self.env.scene.trav_map
        traversable = self._eroded_floor_map(controller, floor)
        candidates: List[torch.Tensor] = []
        seen_map_cells: Set[Tuple[int, int]] = set()

        min_radius = minimum_goal_radius
        max_radius = 2.25
        radius_step = 0.15
        num_angles = 32
        angle_offsets = self._ordered_angle_offsets(
            num_angles,
            preferred_goal_direction,
        )
        num_radii = int(math.ceil((max_radius - min_radius) / radius_step)) + 1
        for radius_idx in range(num_radii):
            radius = min_radius + radius_idx * radius_step
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
        return trav_map._erode_trav_map(floor_map, robot=controller.robot)

    def _valid_traversable_map_cell(
        self,
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
        if not self._has_traversable_clearance(
            traversable,
            row,
            col,
            float(trav_map.map_resolution),
        ):
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

    def _plan_path_to_goal(
        self,
        controller,
        goal_pose_2d: Iterable[float],
    ) -> torch.Tensor:
        floor = self._get_current_floor(controller)
        robot_pos = controller.robot.get_position_orientation()[0]
        source_world = robot_pos[:2]
        target_world = torch.as_tensor(goal_pose_2d[:2], dtype=torch.float32)
        path_world, _ = self.env.scene.trav_map.get_shortest_path(
            floor=floor,
            source_world=source_world,
            target_world=target_world,
            entire_path=True,
            robot=controller.robot,
        )
        if path_world is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Could not find a traversable path to the target position.",
                {"target pose": list(goal_pose_2d)},
            )

        return torch.as_tensor(path_world, dtype=torch.float32)

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
        for index, waypoint in enumerate(filtered_waypoints):
            is_final_waypoint = index == len(filtered_waypoints) - 1
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

        yield from self._rotate_to_yaw(controller, float(waypoints[-1][2]))

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
                yield from self._rotate_to_yaw(controller, target_yaw)
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
    ) -> Generator[torch.Tensor, None, None]:
        best_error = float("inf")
        steps_without_progress = 0
        for _ in range(m.MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            self._raise_if_robot_left_floor(controller, phase="rotate")
            _, robot_quat = controller.robot.get_position_orientation()
            current_yaw = T.quat2euler(robot_quat)[2].item()
            yaw_error = self._normalize_angle(target_yaw - current_yaw)
            if abs(yaw_error) < m.DEFAULT_ANGLE_THRESHOLD:
                empty_action = controller._empty_action()
                yield controller._postprocess_action(empty_action)
                return

            abs_error = abs(yaw_error)
            if abs_error < best_error - 0.01:
                best_error = abs_error
                steps_without_progress = 0
            else:
                steps_without_progress += 1
            if steps_without_progress >= self.stuck_window:
                if abs_error <= self.stuck_angle_tolerance:
                    empty_action = controller._empty_action()
                    yield controller._postprocess_action(empty_action)
                    return
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Robot base is not making rotational progress toward the waypoint.",
                    {
                        "target yaw": target_yaw,
                        "current yaw": current_yaw,
                        "yaw error": yaw_error,
                        "angular command": self.angular_command,
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
        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Robot base left the traversable floor during navigation.",
            details,
        )

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
            sys.stdout.flush()
