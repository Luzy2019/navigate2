import math
from typing import Generator, Iterable, List, Optional, Set, Tuple

import torch
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from omnigibson.objects import StatefulObject
from omnigibson.robots.fetch import Fetch
import omnigibson.utils.transform_utils as T
from omnigibson.action_primitives.starter_semantic_action_primitives import m

from .base import NavigationBackend


class OmniGibsonNavigationBackend(NavigationBackend):
    """Navigation backend that uses OmniGibson traversability maps and stepwise base control."""

    def navigate_to_object(
        self,
        controller,
        target_obj: StatefulObject,
    ) -> Generator[torch.Tensor, None, None]:
        if target_obj is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot navigate to an unknown object.",
            )

        if controller._target_in_reach_of_robot(target_obj.get_position_orientation()):
            return

        try:
            goal_pose_2d = self._sample_goal_pose_near_object(controller, target_obj)
            path_world = self._plan_path_to_goal(controller, goal_pose_2d)
            waypoints = self._path_world_to_waypoints(
                path_world,
                goal_pose_2d,
                target_obj.get_position_orientation()[0],
            )
            yield from self._follow_waypoints(controller, waypoints)
        except Exception:
            yield from controller._navigate_if_needed(target_obj)

    def _sample_goal_pose_near_object(
        self,
        controller,
        target_obj: StatefulObject,
    ) -> torch.Tensor:
        target_pos = torch.as_tensor(
            target_obj.get_position_orientation()[0],
            dtype=torch.float32,
        )
        goal_xy = self._find_reachable_goal_xy_near_target(controller, target_pos)
        target_yaw = math.atan2(
            float(target_pos[1] - goal_xy[1]),
            float(target_pos[0] - goal_xy[0]),
        )
        return torch.tensor(
            [goal_xy[0], goal_xy[1], target_yaw],
            dtype=torch.float32,
        )

    def _find_reachable_goal_xy_near_target(
        self,
        controller,
        target_pos: torch.Tensor,
    ) -> torch.Tensor:
        trav_map = self.env.scene.trav_map
        floor = self._get_current_floor(controller)
        robot_pos = controller.robot.get_position_orientation()[0]
        source_world = robot_pos[:2]

        candidates = self._candidate_goal_positions_near_target(
            controller,
            floor,
            target_pos[:2],
        )
        for candidate_xy in candidates:
            path_world, _ = trav_map.get_shortest_path(
                floor=floor,
                source_world=source_world,
                target_world=candidate_xy,
                entire_path=True,
                robot=controller.robot,
            )
            if path_world is not None:
                return torch.as_tensor(candidate_xy, dtype=torch.float32)

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
    ) -> List[torch.Tensor]:
        trav_map = self.env.scene.trav_map
        traversable = self._eroded_floor_map(controller, floor)
        candidates: List[torch.Tensor] = []
        seen_map_cells: Set[Tuple[int, int]] = set()

        min_radius = 0.45
        max_radius = 2.25
        radius_step = 0.15
        num_angles = 32
        num_radii = int(math.ceil((max_radius - min_radius) / radius_step)) + 1
        for radius_idx in range(num_radii):
            radius = min_radius + radius_idx * radius_step
            for angle_idx in range(num_angles):
                angle = 2.0 * math.pi * angle_idx / num_angles
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
        )

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
        return row, col

    def _nearest_traversable_positions(
        self,
        trav_map,
        traversable: torch.Tensor,
        target_xy: torch.Tensor,
        max_candidates: int,
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
        sorted_indices = torch.argsort(distances)[:max_candidates]
        return [
            torch.as_tensor(trav_map.map_to_world(free_cells[idx]), dtype=torch.float32)
            for idx in sorted_indices
        ]

    def _get_current_floor(self, controller) -> int:
        robot_pos = controller.robot.get_position_orientation()[0]
        floor_heights = getattr(self.env.scene.trav_map, "floor_heights", None)
        if not floor_heights:
            return 0

        floor_diffs = [abs(float(robot_pos[2]) - float(height)) for height in floor_heights]
        return int(min(range(len(floor_diffs)), key=floor_diffs.__getitem__))

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
        for waypoint in waypoints:
            yield from self._drive_towards_waypoint(controller, waypoint[:2])
            yield from self._rotate_to_yaw(controller, float(waypoint[2]))

        yield from controller._settle_robot()

    def _drive_towards_waypoint(
        self,
        controller,
        waypoint_xy: torch.Tensor,
    ) -> Generator[torch.Tensor, None, None]:
        for _ in range(m.MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            robot_pos, robot_quat = controller.robot.get_position_orientation()
            delta_xy = waypoint_xy - robot_pos[:2]
            distance = torch.norm(delta_xy).item()
            if distance < m.DEFAULT_DIST_THRESHOLD:
                empty_action = controller._empty_action()
                yield controller._postprocess_action(empty_action)
                return

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

            lin_vel = m.KP_LIN_VEL.get(type(controller.robot), m.KP_LIN_VEL[Fetch])
            base_action[0] = lin_vel
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
        for _ in range(m.MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            _, robot_quat = controller.robot.get_position_orientation()
            current_yaw = T.quat2euler(robot_quat)[2].item()
            yaw_error = self._normalize_angle(target_yaw - current_yaw)
            if abs(yaw_error) < m.DEFAULT_ANGLE_THRESHOLD:
                empty_action = controller._empty_action()
                yield controller._postprocess_action(empty_action)
                return

            action = controller._empty_action()
            base_action = action[controller.robot.controller_action_idx["base"]]
            if base_action.numel() != 2:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "This navigation backend currently only supports 2D differential-drive base actions.",
                    {"base action size": int(base_action.numel())},
                )

            direction = -1.0 if yaw_error < 0.0 else 1.0
            ang_vel = m.KP_ANGLE_VEL.get(type(controller.robot), m.KP_ANGLE_VEL[Fetch]) * direction
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
