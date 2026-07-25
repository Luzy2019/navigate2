import importlib.util
import math
import sys
from typing import Any, Dict, Mapping, Optional

import omnigibson as og
import torch
import omnigibson.lazy as lazy
from omnigibson import object_states
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from omnigibson.action_primitives.starter_semantic_action_primitives import (
    PlanningContext,
    StarterSemanticActionPrimitiveSet,
    StarterSemanticActionPrimitives,
    m,
)
from omnigibson.envs import Environment
from omnigibson.object_states.open_state import _get_relevant_joints
from omnigibson.utils.control_utils import IKSolver
from omnigibson.utils.constants import JointType, PrimType
from omnigibson.utils.grasping_planning_utils import get_grasp_poses_for_object_sticky
from omnigibson.utils.motion_planning_utils import plan_base_motion
import omnigibson.utils.transform_utils as T

from og_ego_prim.config.runtime_config import NavigationConfig, StarterPrimitivesConfig
from og_ego_prim.navigation import NavigationBackend, OmniGibsonNavigationBackend
from og_ego_prim.primitives.object_states_utils import get_placement_objects
from og_ego_prim.primitives.primitive_utils import compute_cloth_drop_pose


def _interpolate_open_close_waypoints(start_pose, end_pose, num_waypoints="default"):
    """Tensor-compatible replacement for the broken OG waypoint helper."""
    start_pos, start_orn = start_pose
    end_pos, end_orn = end_pose
    travel_distance = float(torch.norm(end_pos - start_pos).item())
    if num_waypoints == "default":
        num_waypoints = max(2, int(travel_distance / 0.05) + 1)
    else:
        num_waypoints = max(2, int(num_waypoints))

    fractions = torch.linspace(0.0, 1.0, num_waypoints)
    return [
        (
            start_pos + fraction * (end_pos - start_pos),
            T.quat_slerp(start_orn, end_orn, fraction),
        )
        for fraction in fractions
    ]


def _orientation_facing_vector(vector):
    """Return a stable quaternion whose +X axis faces ``vector``."""
    forward = torch.as_tensor(vector, dtype=torch.float32)
    forward = forward / torch.norm(forward)
    reference = torch.tensor([0.0, 0.0, 1.0])
    if abs(float(torch.dot(forward, reference).item())) > 0.95:
        reference = torch.tensor([0.0, 1.0, 0.0])
    side = torch.linalg.cross(reference, forward)
    side = side / torch.norm(side)
    up = torch.linalg.cross(forward, side)
    up = up / torch.norm(up)
    return T.mat2quat(torch.stack((forward, side, up), dim=1))


class PhysicalStarterSemanticActionPrimitives(StarterSemanticActionPrimitives):
    """Starter primitives with deterministic traversability-map pre-navigation.

    OmniGibson's original Starter implementation samples a collision-free base
    pose near each manipulation pose. In cluttered custom scenes that sampler
    can reject all candidates based on room membership before the robot moves.
    This subclass keeps Starter's physical arm and gripper implementation while
    routing navigation through IS-Bench's traversability-map backend.
    """

    def __init__(
        self,
        env: Environment,
        navigation_backend: Optional[NavigationBackend] = None,
        starter_config: Optional[StarterPrimitivesConfig] = None,
        navigation_config: Optional[NavigationConfig] = None,
        **kwargs,
    ):
        super().__init__(env, **kwargs)
        config = starter_config or StarterPrimitivesConfig()
        if navigation_backend is None:
            self.navigation_backend = OmniGibsonNavigationBackend(
                allow_native_fallback=False,
                navigation_config=navigation_config,
            )
        else:
            self.navigation_backend = navigation_backend
            if hasattr(self.navigation_backend, "allow_native_fallback"):
                self.navigation_backend.allow_native_fallback = False
        self.navigation_backend.reset(env)
        self.verbose = bool(config.verbose)
        self.symbolic_manipulation = bool(config.symbolic_manipulation)
        self.symbolic_grasp = self.symbolic_manipulation and bool(config.symbolic_grasp)
        self.symbolic_place = self.symbolic_manipulation and bool(config.symbolic_place)
        self.symbolic_cloth_inside_drop = bool(config.symbolic_cloth_inside_drop)
        self.symbolic_cloth_inside_drop_container_categories = frozenset(
            config.symbolic_cloth_inside_drop_container_categories
        )
        self.symbolic_cloth_inside_drop_height = float(
            config.symbolic_cloth_inside_drop_height
        )
        self.symbolic_cloth_inside_pre_settle_steps = int(
            config.symbolic_cloth_inside_pre_settle_steps
        )
        self.symbolic_cloth_inside_settle_steps = int(
            config.symbolic_cloth_inside_settle_steps
        )
        self.symbolic_cloth_inside_fit_shape = bool(
            config.symbolic_cloth_inside_fit_shape
        )
        self.symbolic_cloth_inside_fit_container_scale = float(
            config.symbolic_cloth_inside_fit_container_scale
        )
        self.symbolic_cloth_carry_preserve_shape = bool(
            config.symbolic_cloth_carry_preserve_shape
        )
        self.symbolic_cloth_carry_preserve_shape_categories = frozenset(
            config.symbolic_cloth_carry_preserve_shape_categories
        )
        self.symbolic_loaded_cloth_carry_preserve_shape = bool(
            config.symbolic_loaded_cloth_carry_preserve_shape
        )
        self.symbolic_loaded_cloth_carry_preserve_shape_categories = frozenset(
            config.symbolic_loaded_cloth_carry_preserve_shape_categories
        )
        self.symbolic_loaded_cloth_release_stabilization_steps = int(
            config.symbolic_loaded_cloth_release_stabilization_steps
        )
        if not math.isfinite(self.symbolic_cloth_inside_drop_height):
            raise ValueError(
                "starter_primitives.symbolic_cloth_inside_drop_height must be finite"
            )
        if (
            self.symbolic_cloth_inside_drop
            and not self.symbolic_cloth_inside_drop_container_categories
        ):
            raise ValueError(
                "starter_primitives.symbolic_cloth_inside_drop_container_categories "
                "must be non-empty when cloth drop is enabled"
            )
        if self.symbolic_cloth_inside_settle_steps < 1:
            raise ValueError(
                "starter_primitives.symbolic_cloth_inside_settle_steps must be positive"
            )
        if not 0.0 < self.symbolic_cloth_inside_fit_container_scale <= 1.0:
            raise ValueError(
                "starter_primitives.symbolic_cloth_inside_fit_container_scale "
                "must be in (0, 1]"
            )
        if self.symbolic_cloth_inside_pre_settle_steps < 0:
            raise ValueError(
                "starter_primitives.symbolic_cloth_inside_pre_settle_steps "
                "must be non-negative"
            )
        if self.symbolic_loaded_cloth_release_stabilization_steps < 0:
            raise ValueError(
                "starter_primitives.symbolic_loaded_cloth_release_stabilization_steps "
                "must be non-negative"
            )
        self.symbolic_open_close = self.symbolic_manipulation and bool(config.symbolic_open_close)
        self.symbolic_release = self.symbolic_manipulation and bool(config.symbolic_release)
        self._symbolic_grasp_previous_disable_grasp_handling = None
        self._symbolic_carry_state = None
        self._released_loaded_cloth_stabilization = None
        self._pending_symbolic_particle_transfer = None
        self._last_grasp_ready_navigation = None
        self._open_ready_stance_cache = {}
        self.task_placement_slots: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.tactqn_open_goal_radius = float(config.tactqn_open_goal_radius)
        if self.tactqn_open_goal_radius < 0.45:
            raise ValueError(
                "starter_primitives.tactqn_open_goal_radius must be at least 0.45"
            )
        self.symbolic_grasp_max_goal_radius = float(config.symbolic_grasp_max_goal_radius)
        if self.symbolic_grasp_max_goal_radius < 0.45:
            raise ValueError(
                "starter_primitives.symbolic_grasp_max_goal_radius must be at least 0.45"
            )
        self.symbolic_grasp_max_yaw_error = float(config.symbolic_grasp_max_yaw_error)
        if self.symbolic_grasp_max_yaw_error <= 0.0:
            raise ValueError(
                "starter_primitives.symbolic_grasp_max_yaw_error must be positive"
            )
        self.symbolic_carry_radial_clearance = float(
            config.symbolic_carry_radial_clearance
        )
        self.symbolic_carry_vertical_clearance = float(
            config.symbolic_carry_vertical_clearance
        )
        self.deferred_coverage_max_samples = int(
            config.deferred_coverage_max_samples
        )
        if self.deferred_coverage_max_samples < 1:
            raise ValueError(
                "starter_primitives.deferred_coverage_max_samples must be positive"
            )
        self.symbolic_carry_robot_collision_filter_scope = str(
            config.symbolic_carry_robot_collision_filter_scope
        ).strip().lower()
        if self.symbolic_carry_robot_collision_filter_scope not in {
            "release",
            "episode",
        }:
            raise ValueError(
                "starter_primitives.symbolic_carry_robot_collision_filter_scope "
                "must be 'release' or 'episode'"
            )
        self.symbolic_carry_robot_collision_filter_objects = frozenset(
            config.symbolic_carry_robot_collision_filter_objects
        )
        self._symbolic_carry_robot_filter_registry = {}
        if self.symbolic_carry_radial_clearance < 0.0:
            raise ValueError(
                "starter_primitives.symbolic_carry_radial_clearance must be non-negative"
            )
        if self.symbolic_carry_vertical_clearance < 0.0:
            raise ValueError(
                "starter_primitives.symbolic_carry_vertical_clearance must be non-negative"
            )
        self.tactqn_symbolic_open_close_fallback = bool(
            config.tactqn_symbolic_open_close_fallback
        )
        self._ompl_warning_logged = False
        self._suppress_navigation_hand_actions = False
        self.fixed_navigation_arm_pose = bool(config.fixed_navigation_arm_pose)
        self.fixed_navigation_arm_pose_name = config.fixed_navigation_arm_pose_name
        self._fixed_navigation_joint_indices = None
        self._fixed_navigation_joint_positions = None
        self.native_stance_sample_attempts = int(config.native_stance_sample_attempts)
        if self.native_stance_sample_attempts < 1:
            raise ValueError(
                "starter_primitives.native_stance_sample_attempts must be at least 1"
            )
        self.native_waypoint_max_linear_step = float(config.native_waypoint_max_linear_step)
        if self.native_waypoint_max_linear_step <= 0.0:
            raise ValueError(
                "starter_primitives.native_waypoint_max_linear_step must be positive"
            )
        self.explicit_navigation_max_goal_radius = config.explicit_navigation_max_goal_radius
        if (
            self.explicit_navigation_max_goal_radius is not None
            and self.explicit_navigation_max_goal_radius < 0.45
        ):
            raise ValueError(
                "starter_primitives.explicit_navigation_max_goal_radius must be at least 0.45"
            )
        self.explicit_grasp_use_object_navigation = bool(
            config.explicit_grasp_use_object_navigation
        )
        self.explicit_grasp_navigation_max_goal_radius = (
            config.explicit_grasp_navigation_max_goal_radius
        )
        if (
            self.explicit_grasp_navigation_max_goal_radius is not None
            and self.explicit_grasp_navigation_max_goal_radius < 0.45
        ):
            raise ValueError(
                "starter_primitives.explicit_grasp_navigation_max_goal_radius must be at least 0.45"
            )
        self._cached_ik_solver = None
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.NAVIGATE_TO
        ] = self._navigate_to_obj
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.GRASP
        ] = self._grasp
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.PLACE_ON_TOP
        ] = self._place_on_top
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.PLACE_INSIDE
        ] = self._place_inside
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.RELEASE
        ] = self._execute_release
        self._initialize_fixed_navigation_arm_pose()

    def apply_ref(self, prim, *args, attempts=3):
        """Execute one semantic primitive without Starter's implicit arm reset.

        Upstream Starter retries every primitive and calls ``_reset_hand`` after
        every attempt.  That reset is not part of the requested task action and,
        without OMPL, can become a large direct joint motion.  In particular it
        is unsafe between GRASP and PLACE while an object is constrained to the
        gripper. Navigation retains Starter's native retry count, but retries
        only the native sample / OMPL / base execution and never resets the arm.
        """
        assert attempts > 0, "Must make at least one attempt"

        if prim == StarterSemanticActionPrimitiveSet.NAVIGATE_TO:
            errors = []
            for attempt in range(attempts):
                try:
                    yield from self._with_navigation_hand_actions_suppressed(
                        self._navigate_to_explicit_target(*args)
                    )
                    yield from self._with_navigation_hand_actions_suppressed(
                        self._settle_robot()
                    )
                    return
                except ActionPrimitiveError as exc:
                    errors.append(exc)
                    print(
                        "[starter][navigation][native_retry] "
                        f"attempt={attempt + 1}/{attempts} error={self._short_error(exc)}"
                    )
                    sys.stdout.flush()
                    if (
                        self._is_exhausted_native_stance_error(exc)
                        or self._is_robot_left_floor_error(exc)
                    ):
                        raise
                    try:
                        yield from self._with_navigation_hand_actions_suppressed(
                            self._settle_robot()
                        )
                    except ActionPrimitiveError:
                        pass
            raise errors[-1]
        if prim == StarterSemanticActionPrimitiveSet.GRASP:
            yield from self._apply_grasp_without_default_reset(*args)
            return

        ctrl = self.controller_functions[prim]
        try:
            yield from ctrl(*args)
        except ActionPrimitiveError:
            if not self._primitive_uses_symbolic_shortcut(prim):
                try:
                    yield from self._settle_robot()
                except ActionPrimitiveError:
                    pass
            raise

        if self._primitive_uses_symbolic_shortcut(prim):
            return

        try:
            yield from self._settle_robot()
        except ActionPrimitiveError:
            pass

    def _primitive_uses_symbolic_shortcut(self, prim) -> bool:
        return (
            (prim == StarterSemanticActionPrimitiveSet.GRASP and self.symbolic_grasp)
            or (
                prim
                in {
                    StarterSemanticActionPrimitiveSet.PLACE_INSIDE,
                    StarterSemanticActionPrimitiveSet.PLACE_ON_TOP,
                }
                and self.symbolic_place
            )
            or (
                prim == StarterSemanticActionPrimitiveSet.RELEASE
                and self.symbolic_release
            )
            or (
                prim
                in {
                    StarterSemanticActionPrimitiveSet.OPEN,
                    StarterSemanticActionPrimitiveSet.CLOSE,
                }
                and self.symbolic_open_close
            )
        )

    def _is_exhausted_native_stance_error(self, exc) -> bool:
        metadata = getattr(exc, "metadata", {}) or {}
        return bool(metadata.get("native_stance_exhausted"))

    def _is_robot_left_floor_error(self, exc) -> bool:
        metadata = getattr(exc, "metadata", {}) or {}
        if "height delta" in metadata and "max floor height delta" in metadata:
            return True
        return "Robot base left the traversable floor" in str(exc)

    def _initialize_fixed_navigation_arm_pose(self):
        """Put the arm in one configured navigation pose and hold it there.

        Symbolic manipulation does not require an extended arm during base
        motion.  In addition to Fetch's named untucked poses, ``tucked`` uses
        OmniGibson's native compact reset pose. ``tucked_high`` keeps the same
        compact arm joints but restores the normal raised navigation trunk.
        """
        if not self.fixed_navigation_arm_pose:
            return

        try:
            requested_pose_name = self.fixed_navigation_arm_pose_name
            tucked_pose_names = {"tucked", "tucked_high"}
            if requested_pose_name in tucked_pose_names and hasattr(
                self.robot,
                "tucked_default_joint_pos",
            ):
                target_joint_positions = torch.as_tensor(
                    self.robot.tucked_default_joint_pos,
                    dtype=torch.float32,
                )
                pose_name = requested_pose_name
                if (
                    pose_name == "tucked_high"
                    and hasattr(self.robot, "untucked_default_joint_pos")
                ):
                    raised_joint_positions = torch.as_tensor(
                        self.robot.untucked_default_joint_pos,
                        dtype=torch.float32,
                    )
                    target_joint_positions[self.robot.trunk_control_idx] = (
                        raised_joint_positions[self.robot.trunk_control_idx]
                    )
            elif hasattr(self.robot, "untucked_default_joint_pos"):
                target_joint_positions = torch.as_tensor(
                    self.robot.untucked_default_joint_pos,
                    dtype=torch.float32,
                )
                pose_name = requested_pose_name
            else:
                target_joint_positions = torch.as_tensor(
                    self._get_reset_joint_pos(),
                    dtype=torch.float32,
                )
                pose_name = "reset"

            if pose_name not in tucked_pose_names and hasattr(
                self.robot,
                "default_arm_poses",
            ):
                arm_poses = self.robot.default_arm_poses
                if pose_name not in arm_poses:
                    pose_name = "vertical"
                if pose_name in arm_poses:
                    target_joint_positions[
                        self.robot.arm_control_idx[self.arm]
                    ] = torch.as_tensor(
                        arm_poses[pose_name],
                        dtype=torch.float32,
                    )
            elif pose_name not in tucked_pose_names:
                pose_name = "reset"

            indices = [
                torch.as_tensor(self.robot.trunk_control_idx, dtype=torch.long),
                torch.as_tensor(self.robot.arm_control_idx[self.arm], dtype=torch.long),
                torch.as_tensor(self.robot.gripper_control_idx[self.arm], dtype=torch.long),
            ]
            indices = torch.cat([idx.flatten() for idx in indices])
            indices = torch.unique(indices, sorted=True)
            positions = target_joint_positions[indices].clone()

            self.robot.set_joint_positions(
                positions=positions,
                indices=indices,
                drive=False,
            )
            self._fixed_navigation_joint_indices = indices
            self._fixed_navigation_joint_positions = positions
            self._set_arm_targets_to_fixed_navigation_pose()
            print(
                "[starter][navigation][fixed_arm_pose] initialized "
                f"pose={pose_name} "
                f"indices={self._to_float_list(indices)} "
                f"positions={self._to_float_list(positions)}"
            )
            sys.stdout.flush()
        except Exception as exc:
            print(
                "[starter][navigation][fixed_arm_pose] failed_to_initialize "
                f"error={exc.__class__.__name__}: {exc}"
            )
            sys.stdout.flush()
            self._fixed_navigation_joint_indices = None
            self._fixed_navigation_joint_positions = None

    def _set_arm_targets_to_fixed_navigation_pose(self):
        if (
            self._fixed_navigation_joint_indices is None
            or self._fixed_navigation_joint_positions is None
        ):
            return False

        try:
            joint_positions = self.robot.get_joint_positions().clone()
            joint_positions[self._fixed_navigation_joint_indices] = (
                self._fixed_navigation_joint_positions
            )
            control_dict = self.robot.get_control_dict()
        except Exception:
            return False

        changed = False
        for arm_name in getattr(self.robot, "arm_names", []):
            arm_key = f"arm_{arm_name}"
            if arm_key not in self._arm_targets or arm_key not in self.robot.controllers:
                continue
            arm_ctrl = self.robot.controllers[arm_key]
            try:
                if isinstance(self._arm_targets[arm_key], tuple):
                    eef_key = f"eef_{arm_name}"
                    pos_relative = control_dict[f"{eef_key}_pos_relative"]
                    quat_relative = control_dict[f"{eef_key}_quat_relative"]
                    self._arm_targets[arm_key] = (
                        pos_relative.clone(),
                        T.quat2axisangle(quat_relative).clone(),
                    )
                    changed = True
                    continue
                self._arm_targets[arm_key] = joint_positions[
                    arm_ctrl.dof_idx
                ].clone()
                changed = True
            except Exception:
                continue
        return changed

    def _apply_grasp_without_default_reset(self, obj):
        if self.symbolic_grasp:
            grasp_pose, preferred_goal_direction = yield from (
                self._navigate_to_grasp_ready_pose(obj)
            )
            yield from self._symbolic_grasp(
                obj,
                grasp_pose=grasp_pose,
                preferred_goal_direction=preferred_goal_direction,
            )
            return

        try:
            yield from self._grasp(obj)
        except ActionPrimitiveError:
            if self._get_obj_in_hand() is None:
                try:
                    yield from self._execute_release()
                except ActionPrimitiveError:
                    pass
            try:
                yield from self._settle_robot()
            except ActionPrimitiveError:
                pass
            raise

        if self._get_obj_in_hand() == obj and self._should_lift_after_grasp(obj):
            print(
                "[starter][grasp] performing controlled carry lift (not reset) "
                f"target={obj.name}"
            )
            sys.stdout.flush()
            yield from self._lift_held_object_for_navigation()

        try:
            yield from self._settle_robot()
        except ActionPrimitiveError:
            pass

        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand != obj:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "The grasped object was lost while preparing it for transport.",
                {
                    "expected object": obj.name,
                    "actual object": None if obj_in_hand is None else obj_in_hand.name,
                },
            )

    def _grasp(self, obj):
        yield from self._navigate_to_grasp_ready_pose(obj)
        print(f"[starter][grasp] starting physical grasp target={obj.name}")
        sys.stdout.flush()
        try:
            yield from super()._grasp(obj)
        except ActionPrimitiveError as exc:
            repaired = yield from self._repair_sticky_grasp_if_contacted(obj, exc)
            if repaired:
                return
            raise

    def _navigate_to_grasp_ready_pose(
        self,
        obj,
        navigation_reason="grasp_pose_native_stance",
    ):
        print(f"[starter][grasp] object-level pre-navigation target={obj.name}")
        sys.stdout.flush()

        cached_ready_pose = self._get_cached_grasp_ready_navigation(obj)
        if cached_ready_pose is not None:
            print(
                "[starter][grasp] reusing cached ready pose "
                f"target={obj.name} reason={navigation_reason}"
            )
            sys.stdout.flush()
            return cached_ready_pose

        grasp_poses = list(get_grasp_poses_for_object_sticky(obj))
        if not grasp_poses:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.SAMPLING_ERROR,
                "Could not sample a physical grasp pose for target object.",
                {"target object": obj.name},
            )

        errors = []
        for grasp_candidate_index in torch.randperm(len(grasp_poses)).tolist():
            grasp_pose, object_direction = grasp_poses[grasp_candidate_index]
            preferred_goal_direction = self._preferred_goal_direction_from_current_base(
                grasp_pose
            )
            print(
                "[starter][grasp] sampled ready pose "
                f"target={obj.name} candidate={grasp_candidate_index} "
                f"grasp_pos={self._to_float_list(grasp_pose[0])} "
                f"object_direction={self._to_float_list(object_direction)} "
                f"preferred_goal_direction={self._to_float_list(preferred_goal_direction)}"
            )
            sys.stdout.flush()
            if navigation_reason != "explicit_grasp_ready_stance" and (
                self._safe_target_in_reach(grasp_pose)
                or (
                    self._symbolic_grasp_pose_near_enough(grasp_pose)
                    and self._symbolic_grasp_pose_direction_aligned(
                        grasp_pose,
                        preferred_goal_direction,
                    )
                    and self._symbolic_grasp_pose_facing_target(grasp_pose)
                )
            ):
                print(
                    "[starter][grasp] accepting current ready pose "
                    f"target={obj.name} reason={navigation_reason} "
                    f"base_target_xy_distance={self._base_target_xy_distance(grasp_pose)}"
                )
                sys.stdout.flush()
                self._set_cached_grasp_ready_navigation(
                    obj,
                    grasp_pose,
                    preferred_goal_direction,
                    navigation_reason,
                )
                return grasp_pose, preferred_goal_direction
            try:
                yield from self._navigate_to_native_stance_pose(
                    obj,
                    pose_on_obj=grasp_pose,
                    navigation_reason=navigation_reason,
                    preferred_goal_direction=preferred_goal_direction,
                )
                if (
                    self._safe_target_in_reach(grasp_pose)
                    or (
                        self._symbolic_grasp_pose_near_enough(grasp_pose)
                        and self._symbolic_grasp_pose_direction_aligned(
                            grasp_pose,
                            preferred_goal_direction,
                        )
                        and self._symbolic_grasp_pose_facing_target(grasp_pose)
                    )
                ):
                    self._set_cached_grasp_ready_navigation(
                        obj,
                        grasp_pose,
                        preferred_goal_direction,
                        navigation_reason,
                    )
                    return grasp_pose, preferred_goal_direction
            except ActionPrimitiveError as exc:
                errors.append(self._short_error(exc))
                if exc.reason not in {
                    ActionPrimitiveError.Reason.PLANNING_ERROR,
                    ActionPrimitiveError.Reason.SAMPLING_ERROR,
                }:
                    raise
                continue

            errors.append(
                "Navigation completed, but sampled grasp pose is still outside "
                "the symbolic grasp-ready radius / side / yaw constraints."
            )

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PLANNING_ERROR,
            "Could not navigate to an OmniGibson-native sampled grasp stance.",
            {
                "target object": obj.name,
                "num grasp candidates": len(grasp_poses),
                "maximum goal radius": self.symbolic_grasp_max_goal_radius,
                "maximum yaw error": self.symbolic_grasp_max_yaw_error,
                "attempt errors": errors,
            },
        )

    def _open_or_close(self, obj, should_open):
        """Open / close an object, using symbolic state updates by default."""
        self._tracking_object = self.robot
        action_name = "open" if should_open else "close"

        if self._get_obj_in_hand():
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot open or close an object while holding an object",
                {"object in hand": self._get_obj_in_hand().name},
            )

        if bool(obj.states[object_states.Open].get_value()) == should_open:
            print(
                f"[starter][open_close] already complete action={action_name} "
                f"target={obj.name}"
            )
            sys.stdout.flush()
            return

        if self.symbolic_open_close:
            yield from self._navigate_to_open_close_ready_pose(obj, action_name)
            print(
                f"[starter][open_close][symbolic_shortcut] action={action_name} "
                f"target={obj.name} ready_pose=True physical_attempts=0"
            )
            sys.stdout.flush()
            yield from self._symbolic_open_or_close_fallback(
                obj,
                should_open,
                attempt_errors=[],
                physical_attempted=False,
            )
            return

        # If global symbolic open / close is disabled, still keep the known
        # hard tactqn cabinet symbolic: its WIP physical trajectory can leave
        # Fetch's arm extended at countertop height and block base navigation.
        if self._should_symbolically_fallback_open_close(obj):
            yield from self._navigate_to_open_close_ready_pose(obj, action_name)
            print(
                f"[starter][open_close][symbolic_direct] action={action_name} "
                f"target={obj.name} ready_pose=True physical_attempts=0"
            )
            sys.stdout.flush()
            yield from self._symbolic_open_or_close_fallback(
                obj,
                should_open,
                attempt_errors=[],
                physical_attempted=False,
            )
            return

        print(
            f"[starter][open_close] object-level pre-navigation "
            f"action={action_name} target={obj.name}"
        )
        sys.stdout.flush()
        yield from self._navigate_to_obj(
            obj,
            navigation_reason="open_close_object_precheck",
            require_target_reachable=False,
        )

        yield from self._execute_release()

        errors = []
        for attempt in range(min(m.MAX_ATTEMPTS_FOR_OPEN_CLOSE, 3)):
            try:
                try:
                    grasp_data = self._sample_open_close_grasp_data(
                        obj,
                        should_open,
                        num_waypoints=8 if should_open else 3,
                        grasp_candidate_index=attempt,
                    )
                except ActionPrimitiveError:
                    raise
                except Exception as exc:
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.SAMPLING_ERROR,
                        "Open/close grasp sampling failed.",
                        {
                            "target object": obj.name,
                            "error type": exc.__class__.__name__,
                            "error": str(exc),
                        },
                    ) from exc
                if grasp_data is None:
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.SAMPLING_ERROR,
                        "Could not sample grasp position for target object",
                        {"target object": obj.name},
                    )

                (
                    relevant_joint,
                    grasp_pose,
                    target_poses,
                    object_direction,
                    _,
                    pos_change,
                ) = grasp_data
                print(
                    f"[starter][open_close] action={action_name} target={obj.name} "
                    f"attempt={attempt} joint={relevant_joint.joint_name} "
                    f"joint_type={relevant_joint.joint_type} "
                    f"position_change={float(pos_change):.4f}"
                )
                sys.stdout.flush()

                if abs(float(pos_change)) < 0.1:
                    return

                approach_pose = (
                    grasp_pose[0] + object_direction * m.OPEN_GRASP_APPROACH_DISTANCE,
                    grasp_pose[1],
                )
                use_tactqn_joint_motion = (
                    should_open
                    and getattr(obj, "category", "") == "top_cabinet"
                    and getattr(obj, "model", "") == "tactqn"
                )
                preferred_goal_direction = None
                minimum_goal_radius_override = None
                if use_tactqn_joint_motion:
                    # Stand in front of this door. An unconstrained circular
                    # sample can put Fetch behind the adjacent sink cabinet,
                    # making its arm sweep down across the countertop.
                    preferred_goal_direction = -object_direction[:2]
                    # Keep Fetch a little farther from the door than the
                    # low-handle reach minimum. Standing too close makes the
                    # shoulder approach from above and collide with the
                    # countertop before the gripper reaches the handle.
                    minimum_goal_radius_override = self.tactqn_open_goal_radius

                if (
                    not self._safe_target_in_reach(grasp_pose)
                    or preferred_goal_direction is not None
                ):
                    print(
                        f"[starter][open_close] local base adjustment "
                        f"action={action_name} target={obj.name} "
                        "preferred_side=door_front"
                    )
                    sys.stdout.flush()
                    yield from self._navigate_to_obj(
                        obj,
                        pose_on_obj=grasp_pose,
                        navigation_reason="open_close_grasp_pose",
                        require_target_reachable=False,
                        preferred_goal_direction=preferred_goal_direction,
                        minimum_goal_radius_override=minimum_goal_radius_override,
                    )
                print(
                    f"[starter][open_close] phase=move_to_pregrasp "
                    f"action={action_name} target={obj.name} attempt={attempt}"
                )
                sys.stdout.flush()
                yield from self._move_hand(grasp_pose, stop_if_stuck=True)

                if should_open:
                    print(
                        f"[starter][open_close] phase=close_gripper "
                        f"action={action_name} target={obj.name} attempt={attempt}"
                    )
                    sys.stdout.flush()
                    yield from self._execute_grasp()
                    print(
                        f"[starter][open_close] phase=gripper_closed "
                        f"action={action_name} target={obj.name} attempt={attempt}"
                    )
                    sys.stdout.flush()

                if use_tactqn_joint_motion:
                    yield from self._move_tactqn_open_pose(
                        approach_pose,
                        phase="contact_handle",
                        stop_on_contact=True,
                    )
                else:
                    yield from self._move_hand_linearly_cartesian(
                        approach_pose,
                        ignore_failure=False,
                        stop_on_contact=should_open,
                        stop_if_stuck=True,
                    )

                empty_action = self._empty_action()
                yield self._postprocess_action(empty_action)

                for waypoint_index, target_pose in enumerate(target_poses):
                    if use_tactqn_joint_motion:
                        yield from self._move_tactqn_open_pose(
                            target_pose,
                            phase=f"pull_arc_{waypoint_index}",
                        )
                    else:
                        yield from self._move_hand_linearly_cartesian(
                            target_pose,
                            ignore_failure=False,
                            stop_if_stuck=True,
                        )

                yield from self._move_hand_linearly_cartesian(
                    self.robot.eef_links[self.arm].get_position_orientation(),
                    ignore_failure=True,
                    stop_if_stuck=True,
                )

                if should_open:
                    yield from self._execute_release()
                    yield from self._move_base_backward()

                is_open = bool(obj.states[object_states.Open].get_value())
                if is_open == should_open:
                    print(
                        f"[starter][open_close] succeeded action={action_name} "
                        f"target={obj.name}"
                    )
                    sys.stdout.flush()
                    return
            except ActionPrimitiveError as exc:
                errors.append(self._short_error(exc))
                print(
                    f"[starter][open_close] attempt failed action={action_name} "
                    f"target={obj.name} attempt={attempt} "
                    f"error={self._short_error(exc)}"
                )
                sys.stdout.flush()
                if exc.reason == ActionPrimitiveError.Reason.SAMPLING_ERROR:
                    raise
                if should_open:
                    try:
                        yield from self._execute_release()
                    except ActionPrimitiveError:
                        pass
                    yield from self._move_base_backward()
                else:
                    yield from self._move_hand_backward()

        if self._should_symbolically_fallback_open_close(obj):
            yield from self._symbolic_open_or_close_fallback(
                obj,
                should_open,
                errors,
            )
            return

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
            "Despite executing the physical trajectory, the object did not open or close as expected.",
            {
                "target object": obj.name,
                "requested open state": should_open,
                "actual open state": bool(obj.states[object_states.Open].get_value()),
                "attempt errors": errors,
            },
        )

    def _navigate_to_open_close_ready_pose(self, obj, action_name):
        """Reach the physical-open ready pose before a symbolic open/close.

        Symbolic mode should only replace the brittle handle manipulation
        itself. The base still needs to move to the same side / stance family
        that physical OPEN would use; otherwise a cabinet can appear to open
        while Fetch is nowhere near it.
        """
        print(
            f"[starter][open_close] ready-pose pre-navigation "
            f"action={action_name} target={obj.name}"
        )
        sys.stdout.flush()

        if self._should_use_open_pose_navigation(obj):
            yield from self._navigate_to_open_pose_preview(obj)
            return

        yield from self._navigate_to_obj(
            obj,
            navigation_reason="open_close_object_precheck",
            require_target_reachable=False,
        )

    def _navigate_to_native_stance_pose(
        self,
        obj,
        pose_on_obj,
        navigation_reason,
        preferred_goal_direction=None,
        sampled_pose_2d=None,
    ):
        """Use OmniGibson Starter's native manipulation-stance sampler.

        This keeps symbolic manipulation scoped to the action result.  The base
        stance itself is sampled by OG's ``_sample_pose_near_object``.  Before
        executing, each sampled stance is checked with the same OG
        ``PlanningContext`` + ``plan_base_motion`` path that native
        ``_navigate_to_pose`` uses.  That keeps sampling / room / collision /
        planning decisions in OmniGibson while letting us reject bad random
        candidates before the robot starts moving.
        """
        target_pose = self._normalize_target_pose(obj, pose_on_obj)
        target_kind = "manipulation_pose"
        start_distance = self._base_target_xy_distance(target_pose)
        print(
            f"[starter][navigation][native_stance] target={obj.name} "
            f"target_kind={target_kind} "
            f"reason={navigation_reason} "
            f"preferred_goal_direction={self._to_float_list(preferred_goal_direction)} "
            f"base_target_xy_distance={start_distance}"
        )
        sys.stdout.flush()
        self.navigation_backend.last_navigation_result = {
            "target_object": obj.name,
            "target_pos": self._to_float_list(target_pose[0]),
            "start_base_pos": self._to_float_list(
                self.robot.get_position_orientation()[0]
            ),
            "start_base_target_xy_distance": start_distance,
            "preferred_goal_direction": self._to_float_list(preferred_goal_direction),
            "navigation_reason": navigation_reason,
            "sampler": "omnigibson_native_starter",
            "status": "sampling_native_stance",
        }

        try:
            pose_2d, plan, sample_source, planning_errors = (
                self._sample_og_plannable_native_stance(
                    obj,
                    target_pose,
                    sampled_pose_2d=sampled_pose_2d,
                )
            )
        except ActionPrimitiveError as exc:
            self.navigation_backend.last_navigation_result.update(
                status="failed",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        self.navigation_backend.last_navigation_result.update(
            status="planned_native_stance",
            goal_pose_2d=self._to_float_list(pose_2d),
            sample_source=sample_source,
            native_plan_waypoints_2d=[
                self._to_float_list(waypoint) for waypoint in plan
            ],
            native_plan_steps=len(plan),
            native_planning_filter_errors=planning_errors,
        )

        try:
            yield from self._with_navigation_hand_actions_suppressed(
                self._execute_native_navigation_plan(plan)
            )
        except Exception as exc:
            self.navigation_backend.last_navigation_result.update(
                status="failed",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        end_distance = self._base_target_xy_distance(target_pose)
        reachable = self._safe_target_in_reach(target_pose)
        print(
            f"[starter][navigation][native_stance] completed target={obj.name} "
            f"reason={navigation_reason} "
            f"reachable={reachable} "
            f"base_target_xy_distance={end_distance}"
        )
        sys.stdout.flush()
        self.navigation_backend.last_navigation_result.update(
            status="completed_native_stance",
            end_base_pos=self._to_float_list(self.robot.get_position_orientation()[0]),
            end_base_target_xy_distance=end_distance,
            end_reachable=reachable,
        )
        return pose_2d

    def _sample_og_plannable_native_stance(
        self,
        obj,
        target_pose,
        sampled_pose_2d=None,
    ):
        """Sample OG native stances, keeping only poses OG/OMPL can plan to."""
        self._prepare_native_room_instance_lookup()
        errors = []
        sampled_candidates = 0

        if sampled_pose_2d is not None:
            pose_2d = torch.as_tensor(sampled_pose_2d, dtype=torch.float32).clone()
            plan = self._plan_native_base_motion(pose_2d)
            if plan is not None:
                print(
                    "[starter][navigation][native_stance] sampled target="
                    f"{obj.name} source=saved_native_sample "
                    f"pose_2d={self._to_float_list(pose_2d)} "
                    f"plan_steps={len(plan)}"
                )
                sys.stdout.flush()
                return pose_2d, plan, "saved_native_sample", errors

            errors.append(
                "saved_native_sample rejected by OG/OMPL base planner "
                f"pose_2d={self._to_float_list(pose_2d)}"
            )
            print(
                "[starter][navigation][native_plan_filter] "
                f"rejected_saved target={obj.name} "
                f"pose_2d={self._to_float_list(pose_2d)}"
            )
            sys.stdout.flush()

        for sample_attempt in range(self.native_stance_sample_attempts):
            try:
                pose_2d = super()._sample_pose_near_object(
                    obj,
                    pose_on_obj=target_pose,
                )
            except ActionPrimitiveError as exc:
                errors.append(
                    f"sample_attempt={sample_attempt + 1} "
                    f"sampling_error={self._short_error(exc)}"
                )
                print(
                    "[starter][navigation][native_plan_filter] "
                    f"sample_failed target={obj.name} "
                    f"attempt={sample_attempt + 1}/{self.native_stance_sample_attempts} "
                    f"error={self._short_error(exc)}"
                )
                sys.stdout.flush()
                continue

            sampled_candidates += 1
            self.navigation_backend.last_navigation_result.update(
                status="sampled_native_stance",
                goal_pose_2d=self._to_float_list(pose_2d),
                sample_source="new_native_sample",
                native_sample_attempt=sample_attempt + 1,
            )

            plan = self._plan_native_base_motion(pose_2d)
            if plan is None:
                errors.append(
                    f"sample_attempt={sample_attempt + 1} "
                    "rejected_by_og_ompl_base_planner "
                    f"pose_2d={self._to_float_list(pose_2d)}"
                )
                print(
                    "[starter][navigation][native_plan_filter] "
                    f"rejected target={obj.name} "
                    f"attempt={sample_attempt + 1}/{self.native_stance_sample_attempts} "
                    f"pose_2d={self._to_float_list(pose_2d)}"
                )
                sys.stdout.flush()
                continue

            print(
                "[starter][navigation][native_stance] sampled target="
                f"{obj.name} source=new_native_sample "
                f"attempt={sample_attempt + 1}/{self.native_stance_sample_attempts} "
                f"pose_2d={self._to_float_list(pose_2d)} "
                f"plan_steps={len(plan)}"
            )
            sys.stdout.flush()
            return pose_2d, plan, "new_native_sample", errors

        reason = (
            ActionPrimitiveError.Reason.SAMPLING_ERROR
            if sampled_candidates == 0
            else ActionPrimitiveError.Reason.PLANNING_ERROR
        )
        raise ActionPrimitiveError(
            reason,
            "Could not find an OmniGibson-native sampled stance with a valid OG/OMPL base plan.",
            {
                "target object": obj.name,
                "sample attempts": self.native_stance_sample_attempts,
                "sampled candidates": sampled_candidates,
                "attempt errors": errors,
                "native_stance_exhausted": True,
            },
        )

    def _plan_native_base_motion(self, pose_2d):
        """Ask OmniGibson's native OMPL planner whether a sampled pose is usable."""
        with PlanningContext(self.env, self.robot, self.robot_copy, "simplified") as context:
            return plan_base_motion(
                robot=self.robot,
                end_conf=pose_2d,
                context=context,
            )

    def _execute_native_navigation_plan(self, plan):
        dense_plan = self._densify_native_navigation_plan(plan)
        if len(dense_plan) > 1:
            robot_pos = self.robot.get_position_orientation()[0]
            first_waypoint = torch.as_tensor(dense_plan[0], dtype=torch.float32)
            if (
                torch.norm(first_waypoint[:2] - robot_pos[:2])
                < m.LOW_PRECISION_DIST_THRESHOLD
            ):
                dense_plan = dense_plan[1:]

        self.navigation_backend.last_navigation_result.update(
            densified_plan_waypoints_2d=[
                self._to_float_list(waypoint) for waypoint in dense_plan
            ],
            densified_plan_steps=len(dense_plan),
        )
        print(
            "[starter][navigation][native_plan] "
            f"executing native_steps={len(plan)} "
            f"densified_steps={len(dense_plan)}"
        )
        sys.stdout.flush()

        for waypoint_index, waypoint in enumerate(dense_plan):
            low_precision = waypoint_index < len(dense_plan) - 1
            print(
                "[starter][navigation][native_plan] "
                f"waypoint={waypoint_index + 1}/{len(dense_plan)} "
                f"low_precision={low_precision} "
                f"pose_2d={self._to_float_list(waypoint)}"
            )
            sys.stdout.flush()
            yield from self._navigate_to_pose_direct(
                waypoint,
                low_precision=low_precision,
            )

    def _densify_native_navigation_plan(self, plan):
        waypoints = [torch.as_tensor(waypoint, dtype=torch.float32) for waypoint in plan]
        if len(waypoints) <= 1:
            return [waypoint.clone() for waypoint in waypoints]

        dense_plan = [waypoints[0].clone()]
        min_executable_step = float(m.LOW_PRECISION_DIST_THRESHOLD) * 1.25
        for start, end in zip(waypoints, waypoints[1:]):
            delta_xy = end[:2] - start[:2]
            distance = float(torch.norm(delta_xy).item())
            delta_yaw = self._wrap_navigation_yaw(float(end[2] - start[2]))
            if distance <= self.native_waypoint_max_linear_step:
                num_segments = 1
            else:
                desired_segments = max(
                    1,
                    math.ceil(distance / self.native_waypoint_max_linear_step),
                )
                max_executable_segments = max(
                    1,
                    math.floor(distance / min_executable_step),
                )
                num_segments = min(desired_segments, max_executable_segments)
            for segment_index in range(1, num_segments + 1):
                fraction = segment_index / num_segments
                waypoint = start.clone()
                waypoint[:2] = start[:2] + fraction * delta_xy
                waypoint[2] = self._wrap_navigation_yaw(
                    float(start[2]) + fraction * delta_yaw
                )
                if not dense_plan or not torch.allclose(waypoint, dense_plan[-1]):
                    dense_plan.append(waypoint)
        return dense_plan

    @staticmethod
    def _wrap_navigation_yaw(yaw):
        return (yaw + math.pi) % (2.0 * math.pi) - math.pi

    def _navigate_to_pose_direct(self, pose_2d, low_precision=False):
        """Execute OG waypoint navigation without enforcing intermediate yaw.

        OmniGibson's native ``_navigate_to_pose`` uses ``low_precision=True`` for
        all intermediate OMPL waypoints, but its direct executor still performs a
        final rotate to each intermediate waypoint's sampled yaw.  Those yaw
        values are not the manipulation stance; they are just path states.  For
        intermediate waypoints, move position-only and reserve strict yaw
        alignment for the final waypoint where ``low_precision=False``.
        """
        self._record_native_navigation_waypoint(pose_2d, low_precision)
        if not low_precision:
            yield from super()._navigate_to_pose_direct(
                pose_2d,
                low_precision=low_precision,
            )
            return

        yield from self._navigate_to_pose_direct_position_only(pose_2d)

    def _record_native_navigation_waypoint(self, pose_2d, low_precision):
        result = self.navigation_backend.last_navigation_result
        if not isinstance(result, dict):
            return
        waypoint = self._to_float_list(pose_2d)
        waypoints = result.setdefault("executed_waypoints_2d", [])
        if not waypoints or waypoint != waypoints[-1]:
            waypoints.append(waypoint)
        result["current_waypoint_pose_2d"] = waypoint
        result["current_waypoint_index"] = len(waypoints) - 1
        result["current_waypoint_is_final"] = not low_precision

    def _navigate_to_pose_direct_position_only(self, pose_2d):
        dist_threshold = m.LOW_PRECISION_DIST_THRESHOLD
        end_pose = self._get_robot_pose_from_2d_pose(pose_2d)
        body_target_pose = self._get_pose_in_robot_frame(end_pose)

        for _ in range(m.MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            if torch.norm(body_target_pose[0][:2]) < dist_threshold:
                break

            diff_pos = end_pose[0] - self.robot.get_position_orientation()[0]
            intermediate_pose = (
                end_pose[0],
                T.euler2quat(
                    torch.tensor(
                        [0, 0, math.atan2(diff_pos[1], diff_pos[0])],
                        dtype=torch.float32,
                    )
                ),
            )
            body_intermediate_pose = self._get_pose_in_robot_frame(intermediate_pose)
            diff_yaw = T.quat2euler(body_intermediate_pose[1])[2].item()
            if abs(diff_yaw) > m.DEFAULT_ANGLE_THRESHOLD:
                yield from self._rotate_in_place(
                    intermediate_pose,
                    angle_threshold=m.DEFAULT_ANGLE_THRESHOLD,
                )
            else:
                action = self._empty_action()
                if self._base_controller_is_joint:
                    base_action_size = self.robot.controller_action_idx["base"].numel()
                    assert (
                        base_action_size == 3
                    ), "Currently, the action primitives only support [x, y, theta] joint controller"
                    direction_vec = (
                        body_target_pose[0][:2]
                        / torch.norm(body_target_pose[0][:2])
                        * m.KP_LIN_VEL[type(self.robot)]
                    )
                    base_action = torch.tensor(
                        [direction_vec[0], direction_vec[1], 0.0],
                        dtype=torch.float32,
                    )
                    action[self.robot.controller_action_idx["base"]] = base_action
                else:
                    base_action = torch.tensor(
                        [m.KP_LIN_VEL[type(self.robot)], 0.0],
                        dtype=torch.float32,
                    )
                    action[self.robot.controller_action_idx["base"]] = base_action
                yield self._postprocess_action(action)

            body_target_pose = self._get_pose_in_robot_frame(end_pose)
        else:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Could not navigate to the intermediate target position",
                {"target pose": end_pose},
            )

        yield self._postprocess_action(self._empty_action())

    def _prepare_native_room_instance_lookup(self):
        """Make OG's room lookup total without choosing or validating poses.

        The cropped Wainscott room-instance raster contains a few nonzero ids
        that are absent from ``room_ins_id_to_ins_name``.  OG's native lookup
        indexes that dictionary directly and otherwise raises ``KeyError``
        before its own sampler can reject the point as outside the target room.
        Mapping only those unmapped ids to ``None`` preserves OG's native
        sampling, room comparison, collision test, and IK reachability test.
        """
        seg_map = self.env.scene._seg_map
        known_ids = seg_map.room_ins_id_to_ins_name
        for ins_id in torch.unique(seg_map.room_ins_map).tolist():
            ins_id = int(ins_id)
            if ins_id != 0 and ins_id not in known_ids:
                known_ids[ins_id] = None

    def _should_symbolically_fallback_open_close(self, obj):
        """Use symbolic open/close only for the known hard tactqn cabinet."""
        return (
            self.tactqn_symbolic_open_close_fallback
            and getattr(obj, "category", "") == "top_cabinet"
            and getattr(obj, "model", "") == "tactqn"
        )

    def _symbolic_open_or_close_fallback(
        self,
        obj,
        should_open,
        attempt_errors,
        physical_attempted=True,
    ):
        """Set the Open state for the scoped tactqn symbolic path.

        This keeps the starter mode hybrid: navigation / grasp / placement
        remain physical, while the one WIP cabinet handle interaction can fall
        back to the same state update used by OmniGibson's symbolic primitive.
        When selected before physical execution, no robot control is generated.
        """
        action_name = "open" if should_open else "close"
        print(
            "[starter][open_close][symbolic_fallback] "
            f"action={action_name} target={obj.name} "
            f"attempt_errors={attempt_errors}"
        )
        sys.stdout.flush()

        if object_states.Open not in obj.states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not openable.",
                {"target object": obj.name},
            )

        if physical_attempted:
            try:
                yield from self._execute_release()
            except ActionPrimitiveError:
                pass

        try:
            obj.states[object_states.Open].set_value(should_open, fully=True)
        except TypeError:
            obj.states[object_states.Open].set_value(should_open)

        if physical_attempted:
            yield from self._settle_robot()

        is_open = bool(obj.states[object_states.Open].get_value())
        if is_open != should_open:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "Symbolic fallback did not open or close the object as expected.",
                {
                    "target object": obj.name,
                    "requested open state": should_open,
                    "actual open state": is_open,
                    "physical attempt errors": attempt_errors,
                },
            )

        print(
            "[starter][open_close][symbolic_fallback] succeeded "
            f"action={action_name} target={obj.name}"
        )
        sys.stdout.flush()

    def _symbolic_grasp(self, obj, grasp_pose=None, preferred_goal_direction=None):
        """Enter a navigation-safe symbolic holding state.

        A live PhysX FixedJoint is too brittle for our all-symbolic shortcut:
        the object may be teleported from a table into the hand in a single
        frame, and the stale arm target left by a previous primitive can then
        pull the constrained object / gripper hard enough to destabilize Fetch.

        Instead we mirror the assisted-grasp bookkeeping that downstream
        primitives use (``_ag_obj_in_hand``, gripper freeze, etc.) and carry the
        object kinematically at the end-effector pose before each no-op /
        navigation action.  This preserves the same high-level "hand is full"
        state as a physical grasp while avoiding a dynamic constraint that can
        drag the robot off the floor.
        """
        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand is not None:
            if obj_in_hand == obj:
                self._disable_assisted_grasp_auto_handling_for_symbolic_hold()
                yield from self._yield_symbolic_refresh_step()
                return
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot grasp when the gripper is already holding an object.",
                {
                    "target object": obj.name,
                    "object currently in hand": obj_in_hand.name,
                },
            )

        self._remove_released_loaded_cloth_object(obj)

        print(f"[starter][grasp][symbolic_shortcut] target={obj.name}")
        sys.stdout.flush()
        reach_pose = grasp_pose if grasp_pose is not None else obj.get_position_orientation()
        if not (
            self._safe_target_in_reach(reach_pose)
            or (
                self._symbolic_grasp_pose_near_enough(reach_pose)
                and self._symbolic_grasp_pose_direction_aligned(
                    reach_pose,
                    preferred_goal_direction,
                )
                and self._symbolic_grasp_pose_facing_target(reach_pose)
            )
        ):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot symbolically grasp an object outside the grasp-ready stance.",
                {
                    "target object": obj.name,
                    "maximum goal radius": self.symbolic_grasp_max_goal_radius,
                    "maximum yaw error": self.symbolic_grasp_max_yaw_error,
                    "base target xy distance": self._base_target_xy_distance(reach_pose),
                    "physical reachability": self._safe_target_in_reach(reach_pose),
                    "symbolic near enough": self._symbolic_grasp_pose_near_enough(reach_pose),
                    "symbolic direction aligned": self._symbolic_grasp_pose_direction_aligned(
                        reach_pose,
                        preferred_goal_direction,
                    ),
                    "symbolic facing target": self._symbolic_grasp_pose_facing_target(
                        reach_pose
                    ),
                },
            )
        obj_pos, obj_orn = obj.get_position_orientation()
        # Capture deformable geometry before the symbolic teleport.  Sampling
        # after the root move can already include the stretched state caused by
        # moving PhysX particles with the object pose.
        capture_cloth_shape = getattr(
            self,
            "_capture_symbolic_carried_cloth_shape",
            None,
        )
        cloth_shape = (
            capture_cloth_shape(obj)
            if callable(capture_cloth_shape)
            else None
        )
        particle_states = self._capture_symbolic_carried_particles(obj)
        rigid_descendant_states = self._capture_symbolic_carried_rigid_descendants(
            obj
        )
        try:
            self._suspend_symbolic_carried_particles(particle_states)
            self._suspend_symbolic_carried_rigid_descendants(
                rigid_descendant_states
            )
            carried_obj_pos, carried_obj_orn, eef_pos = (
                self._symbolic_carry_pose_away_from_robot(
                    obj,
                    obj_orn,
                )
            )
            obj.set_position_orientation(
                position=carried_obj_pos,
                orientation=carried_obj_orn,
            )
            obj.keep_still()
            self._sync_symbolic_rigid_descendants_to_root_pose(
                rigid_descendant_states,
                carried_obj_pos,
                carried_obj_orn,
            )
            self._force_symbolic_grasp_constraint(
                obj,
                eef_pos,
                cloth_shape=cloth_shape,
                particle_states=particle_states,
                rigid_descendant_states=rigid_descendant_states,
            )
            # Particle-count and filtered-pair edits invalidate PhysX tensor
            # views until OmniGibson refreshes its runtime handles.
            og.sim.update_handles()
        except Exception:
            if (
                self._symbolic_carry_state is not None
                and self._symbolic_carry_state.get("obj") is obj
            ):
                self._clear_symbolic_grasp_state(
                    self.arm,
                    restore_payload=False,
                )
            obj.set_position_orientation(position=obj_pos, orientation=obj_orn)
            obj.keep_still()
            self._restore_symbolic_rigid_descendant_snapshot(
                rigid_descendant_states
            )
            self._restore_symbolic_carried_rigid_descendant_collisions(
                rigid_descendant_states
            )
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj,
                preserve_snapshot_velocities=True,
            )
            raise

        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand is None:
            if self._symbolic_carry_state is not None:
                self._clear_symbolic_grasp_state(
                    self.arm,
                    restore_payload=False,
                )
            obj.set_position_orientation(position=obj_pos, orientation=obj_orn)
            obj.keep_still()
            self._restore_symbolic_rigid_descendant_snapshot(
                rigid_descendant_states
            )
            self._restore_symbolic_carried_rigid_descendant_collisions(
                rigid_descendant_states
            )
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj,
                preserve_snapshot_velocities=True,
            )
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "Symbolic grasp completed, but no object was detected in hand.",
                {"target object": obj.name},
            )
        if obj_in_hand != obj:
            if self._symbolic_carry_state is not None:
                self._clear_symbolic_grasp_state(
                    self.arm,
                    restore_payload=False,
                )
            obj.set_position_orientation(position=obj_pos, orientation=obj_orn)
            obj.keep_still()
            self._restore_symbolic_rigid_descendant_snapshot(
                rigid_descendant_states
            )
            self._restore_symbolic_carried_rigid_descendant_collisions(
                rigid_descendant_states
            )
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj,
                preserve_snapshot_velocities=True,
            )
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "Symbolic grasp attached an unexpected object.",
                {
                    "expected object": obj.name,
                    "actual object": obj_in_hand.name,
                },
            )
        self._disable_assisted_grasp_auto_handling_for_symbolic_hold()
        yield from self._yield_symbolic_refresh_step()

    def _symbolic_carry_pose_away_from_robot(self, obj, obj_orn):
        """Place the carried AABB below the EEF and outside the robot body."""
        robot_pos, robot_orn = self.robot.get_position_orientation()
        eef_pos, _ = self.robot.eef_links[self.arm].get_position_orientation()
        robot_pos = torch.as_tensor(robot_pos, dtype=torch.float32)
        eef_pos = torch.as_tensor(eef_pos, dtype=torch.float32)
        extent = torch.as_tensor(obj.aabb_extent, dtype=torch.float32)

        outward_xy = eef_pos[:2] - robot_pos[:2]
        outward_norm = torch.linalg.norm(outward_xy)
        if float(outward_norm.item()) < 1e-4:
            robot_yaw = T.quat2euler(robot_orn)[2]
            outward_xy = torch.stack((torch.cos(robot_yaw), torch.sin(robot_yaw)))
        else:
            outward_xy = outward_xy / outward_norm

        carried_obj_pos = eef_pos.clone()
        carried_obj_pos[:2] += outward_xy * (
            0.5 * torch.linalg.norm(extent[:2])
            + self.symbolic_carry_radial_clearance
        )
        carried_obj_pos[2] -= (
            0.5 * extent[2] + self.symbolic_carry_vertical_clearance
        )
        print(
            "[starter][grasp][symbolic_carry_pose] "
            f"target={obj.name} extent={self._to_float_list(extent)} "
            f"eef={self._to_float_list(eef_pos)} "
            f"object={self._to_float_list(carried_obj_pos)}"
        )
        sys.stdout.flush()
        return carried_obj_pos, obj_orn, eef_pos

    def _empty_action(self):
        if self._symbolic_carry_active():
            if not self._set_arm_targets_to_fixed_navigation_pose():
                self._pin_current_arm_targets_for_symbolic_carry()
            self._sync_symbolic_carried_object_to_eef()
        action = super()._empty_action()
        if self._suppress_navigation_hand_actions:
            action = self._mask_navigation_hand_action(action)
        return action

    def _with_navigation_hand_actions_suppressed(self, generator):
        """Run a navigation generator without letting OG drive arm/gripper.

        OG native navigation builds each base command from ``_empty_action()``.
        Starter's ``_empty_action()`` also tracks stale arm targets, so a pure
        NAVIGATE_TO can accidentally keep sending arm commands for hundreds of
        frames.  During navigation we keep symbolic carry bookkeeping/rendering,
        but replace gripper command slices with no-op.  The arm itself is not
        reset or tucked; its cached target is pinned to the current pose before
        navigation starts, so ``_empty_action()`` actively holds that pose
        instead of chasing a stale manipulation target.
        """
        previous = self._suppress_navigation_hand_actions
        previous_disable_grasp_handling = self.robot._disable_grasp_handling
        self._pin_current_arm_targets_for_navigation()
        self._suppress_navigation_hand_actions = True
        # Holding the gripper command still allows AssistedGrasp to attach an
        # object when closed fingers brush nearby furniture.  Navigation must
        # never acquire a new object implicitly.
        self.robot._disable_grasp_handling = True
        if isinstance(self.navigation_backend.last_navigation_result, dict):
            self.navigation_backend.last_navigation_result.update(
                navigation_hand_action_mode="pinned_arm_gripper_no_op",
                assisted_grasp_handling_disabled=True,
            )
        try:
            yield from generator
        finally:
            self.robot._disable_grasp_handling = previous_disable_grasp_handling
            self._suppress_navigation_hand_actions = previous

    def _mask_navigation_hand_action(self, action):
        control_dict = self.robot.get_control_dict()
        for name, controller in self.robot._controllers.items():
            if not name.startswith("gripper_"):
                continue
            action_idx = self.robot.controller_action_idx[name]
            action[action_idx] = controller.compute_no_op_action(control_dict)
        return action

    def _pin_current_arm_targets_for_navigation(self):
        """Make navigation no-op actions hold the current arm pose.

        This does not reset or tuck the arm.  It only overwrites Starter's
        cached arm targets with the robot's current arm state so subsequent
        ``_empty_action()`` calls cannot chase a stale manipulation target while
        the base is navigating.
        """
        if self._set_arm_targets_to_fixed_navigation_pose():
            return

        try:
            control_dict = self.robot.get_control_dict()
        except Exception:
            return

        for arm_name in getattr(self.robot, "arm_names", []):
            arm_key = f"arm_{arm_name}"
            if arm_key not in self._arm_targets or arm_key not in self.robot.controllers:
                continue
            arm_ctrl = self.robot.controllers[arm_key]
            current_target = self._arm_targets[arm_key]
            try:
                if isinstance(current_target, tuple):
                    eef_key = f"eef_{arm_name}"
                    pos_relative = control_dict[f"{eef_key}_pos_relative"]
                    quat_relative = control_dict[f"{eef_key}_quat_relative"]
                    self._arm_targets[arm_key] = (
                        pos_relative.clone(),
                        T.quat2axisangle(quat_relative).clone(),
                    )
                else:
                    self._arm_targets[arm_key] = control_dict["joint_position"][
                        arm_ctrl.dof_idx
                    ].clone()
            except Exception:
                continue

    def _postprocess_action(self, action):
        """Refresh task-scoped released cloth before each simulator step."""
        self._stabilize_released_loaded_cloth_payload()
        return super()._postprocess_action(action)

    def _stabilize_released_loaded_cloth_payload(self):
        """Keep captured cloth descendants inside a released carrier briefly.

        Symbolic loaded-container carry restores a cloth pose when the carrier
        is released, but a subsequent navigation step can otherwise let the
        cloth slide out before the next manipulation.  This bounded repair is
        opt-in and only applies to the captured cloth descendants of one
        released root; the state is removed as soon as a listed child is
        grasped or the step budget expires.
        """
        stabilization = getattr(
            self,
            "_released_loaded_cloth_stabilization",
            None,
        )
        if not stabilization:
            return
        remaining = int(stabilization.get("remaining_steps", 0))
        if remaining <= 0:
            self._released_loaded_cloth_stabilization = None
            return
        root_obj = stabilization.get("root")
        descendant_states = stabilization.get("states", [])
        if root_obj is None or not descendant_states:
            self._released_loaded_cloth_stabilization = None
            return
        try:
            root_pos, root_orn = root_obj.get_position_orientation()
            self._sync_symbolic_rigid_descendants_to_root_pose(
                descendant_states,
                root_pos,
                root_orn,
            )
            self._restore_symbolic_rigid_descendant_cloth_shapes(
                descendant_states
            )
        except Exception as exc:
            print(
                "[starter][release][loaded_cloth] clearing stale "
                f"stabilization root={getattr(root_obj, 'name', root_obj)} "
                f"error={type(exc).__name__}: {exc}"
            )
            sys.stdout.flush()
            self._released_loaded_cloth_stabilization = None
            return
        stabilization["remaining_steps"] = remaining - 1

    @staticmethod
    def _same_runtime_object(left, right):
        if left is right:
            return True
        if left is None or right is None:
            return False
        for attribute in ("prim_path", "name"):
            left_value = getattr(left, attribute, None)
            right_value = getattr(right, attribute, None)
            if left_value is not None and left_value == right_value:
                return True
        return False

    def _register_released_loaded_cloth_stabilization(
        self,
        root_obj,
        descendant_states,
    ):
        """Register only after a released loaded relation has been checked."""
        if self.symbolic_loaded_cloth_release_stabilization_steps <= 0:
            return
        if self._get_obj_in_hand() is not None:
            return
        if getattr(root_obj, "prim_type", None) == PrimType.CLOTH:
            return
        cloth_states = [
            state
            for state in descendant_states
            if state.get("cloth_shape")
        ]
        if not cloth_states:
            return
        self._released_loaded_cloth_stabilization = {
            "root": root_obj,
            "states": cloth_states,
            "remaining_steps": self.symbolic_loaded_cloth_release_stabilization_steps,
        }
        print(
            "[starter][release][loaded_cloth] "
            f"stabilization root={root_obj.name} "
            f"children={[state['obj'].name for state in cloth_states]} "
            f"steps={self.symbolic_loaded_cloth_release_stabilization_steps}"
        )
        sys.stdout.flush()

    def _remove_released_loaded_cloth_object(self, obj):
        """Stop stabilizing one child when its next manipulation begins."""
        stabilization = getattr(
            self,
            "_released_loaded_cloth_stabilization",
            None,
        )
        if not stabilization:
            return
        if self._same_runtime_object(stabilization.get("root"), obj):
            self._released_loaded_cloth_stabilization = None
            return
        remaining_states = [
            state
            for state in stabilization.get("states", [])
            if not self._same_runtime_object(state.get("obj"), obj)
        ]
        if remaining_states:
            stabilization["states"] = remaining_states
        else:
            self._released_loaded_cloth_stabilization = None

    def _yield_symbolic_refresh_step(self):
        """Advance one safe no-op frame after symbolic state changes.

        Symbolic GRASP / PLACE can complete without yielding any low-level
        action.  The executor then saves ``obs_after.png`` from a stale render
        buffer.  One no-op action is enough to refresh physics / rendering while
        avoiding the long settle sequence that can destabilize Fetch.
        """
        self._sync_symbolic_carried_object_to_eef()
        yield self._postprocess_action(self._empty_action())

    def _disable_assisted_grasp_auto_handling_for_symbolic_hold(self):
        """Keep OG from auto-releasing a symbolic grasp during no-op steps."""
        if self._symbolic_grasp_previous_disable_grasp_handling is None:
            self._symbolic_grasp_previous_disable_grasp_handling = (
                self.robot._disable_grasp_handling
            )
        self.robot._disable_grasp_handling = True

    def _restore_assisted_grasp_auto_handling_after_symbolic_hold(self):
        """Restore OG assisted-grasp handling after PLACE / RELEASE."""
        if self._symbolic_grasp_previous_disable_grasp_handling is None:
            return
        self.robot._disable_grasp_handling = (
            self._symbolic_grasp_previous_disable_grasp_handling
        )
        self._symbolic_grasp_previous_disable_grasp_handling = None

    def _force_symbolic_grasp_constraint(
        self,
        obj,
        contact_pos,
        particle_states=None,
        rigid_descendant_states=None,
        *,
        cloth_shape=None,
    ):
        """Create assisted-grasp state records without a live physics joint.

        Downstream Starter / executor code only needs the same bookkeeping that
        a physical assisted grasp exposes: an object in hand, frozen gripper,
        and release metadata.  A live FixedJoint is intentionally avoided here
        because symbolic grasp teleports the object to the hand; immediately
        solving that hard constraint during base navigation can inject large
        impulses into the robot.  The visual / logical carry is instead kept in
        sync by ``_sync_symbolic_carried_object_to_eef`` before each no-op /
        navigation action.
        """
        arm = self.arm
        obj_link = obj.root_link
        contact_pos = torch.as_tensor(contact_pos, dtype=torch.float32)

        if (
            self.robot._ag_obj_constraints[arm] is not None
            or self.robot._ag_obj_in_hand[arm] is not None
            or self._symbolic_carry_state is not None
        ):
            self._clear_symbolic_grasp_state(arm)

        eef_pos, eef_orn = self.robot.eef_links[arm].get_position_orientation()
        obj_pos, obj_orn = obj.get_position_orientation()
        eef_to_obj_pos, eef_to_obj_orn = T.relative_pose_transform(
            obj_pos,
            obj_orn,
            eef_pos,
            eef_orn,
        )

        self.robot._ag_obj_constraints[arm] = None
        self.robot._ag_obj_constraint_params[arm] = {
            "ag_obj_prim_path": obj.prim_path,
            "ag_link_prim_path": obj_link.prim_path,
            "ag_joint_prim_path": None,
            "joint_type": "KinematicCarry",
            "gripper_pos": self.robot.get_joint_positions()[
                self.robot.gripper_control_idx[arm]
            ],
            "max_force": None,
            "contact_pos": contact_pos,
            "symbolic_kinematic_carry": True,
        }
        self.robot._ag_obj_in_hand[arm] = obj
        self.robot._ag_freeze_gripper[arm] = True
        self.robot._ag_freeze_joint_pos[arm] = {}
        self.robot._ag_release_counter[arm] = None
        for joint in self.robot.finger_joints[arm]:
            self.robot._ag_freeze_joint_pos[arm][joint.joint_name] = joint.get_state()[
                0
            ][0]

        # Keep a compatibility fallback for direct callers that do not provide
        # the pre-teleport snapshot.  The normal symbolic grasp path always
        # passes the snapshot captured before moving the object.
        if cloth_shape is None:
            cloth_shape = self._capture_symbolic_carried_cloth_shape(obj)
        self._symbolic_carry_state = {
            "arm": arm,
            "obj": obj,
            "eef_to_obj_pos": eef_to_obj_pos.clone(),
            "eef_to_obj_orn": eef_to_obj_orn.clone(),
            "collision_states": self._suppress_symbolic_carried_object_collisions(
                obj
            ),
            "particle_states": list(particle_states or []),
            "rigid_descendant_states": list(rigid_descendant_states or []),
            "pending_covered_systems": [],
            "cloth_shape": cloth_shape,
        }
        self._pin_current_arm_targets_for_symbolic_carry()
        self._sync_symbolic_carried_object_to_eef()

        print(
            "[starter][grasp][symbolic_carry] "
            f"kinematic_state target={obj.name}"
        )
        sys.stdout.flush()

    def _symbolic_carry_active(self):
        if self._symbolic_carry_state is None:
            return False
        obj = self._symbolic_carry_state.get("obj")
        arm = self._symbolic_carry_state.get("arm")
        return arm == self.arm and obj is not None and self._get_obj_in_hand() == obj

    def _pin_current_arm_targets_for_symbolic_carry(self):
        """Make future no-op actions hold the current arm pose.

        Upstream ``_empty_action`` drives the arm toward ``self._arm_targets``.
        After a symbolic grasp those targets may still describe a previous
        navigation / manipulation pose, causing large arm commands while the
        robot is supposed to be only navigating.  Pinning them to the current
        joint / EEF state makes NAVIGATE_TO behave like the physical-grasp case:
        base moves, arm holds its carry pose.
        """
        try:
            control_dict = self.robot.get_control_dict()
        except Exception:
            return

        for arm_name in getattr(self.robot, "arm_names", []):
            arm_key = f"arm_{arm_name}"
            if arm_key not in self._arm_targets or arm_key not in self.robot.controllers:
                continue
            arm_ctrl = self.robot.controllers[arm_key]
            current_target = self._arm_targets[arm_key]
            try:
                if isinstance(current_target, tuple):
                    eef_key = f"eef_{arm_name}"
                    pos_relative = control_dict[f"{eef_key}_pos_relative"]
                    quat_relative = control_dict[f"{eef_key}_quat_relative"]
                    self._arm_targets[arm_key] = (
                        pos_relative.clone(),
                        T.quat2axisangle(quat_relative).clone(),
                    )
                else:
                    self._arm_targets[arm_key] = control_dict["joint_position"][
                        arm_ctrl.dof_idx
                    ].clone()
            except Exception:
                continue

    def _sync_symbolic_carried_object_to_eef(self):
        if not self._symbolic_carry_active():
            return

        obj = self._symbolic_carry_state["obj"]
        arm = self._symbolic_carry_state["arm"]
        eef_pos, eef_orn = self.robot.eef_links[arm].get_position_orientation()
        obj_pos, obj_orn = T.pose_transform(
            eef_pos,
            eef_orn,
            self._symbolic_carry_state["eef_to_obj_pos"],
            self._symbolic_carry_state["eef_to_obj_orn"],
        )
        obj.set_position_orientation(position=obj_pos, orientation=obj_orn)
        obj.keep_still()
        self._restore_symbolic_carried_cloth_shape(
            obj,
            obj_pos,
            obj_orn,
            self._symbolic_carry_state.get("cloth_shape"),
        )
        self._sync_symbolic_rigid_descendants_to_root_pose(
            self._symbolic_carry_state.get("rigid_descendant_states", []),
            obj_pos,
            obj_orn,
        )

    @staticmethod
    def _capture_symbolic_cloth_shape(obj):
        obj_pos, obj_orn = obj.get_position_orientation()
        world_positions = obj.root_link.compute_particle_positions()
        rotation = T.quat2mat(obj_orn)
        return {
            "root_relative_positions": (
                rotation.T @ (world_positions - obj_pos).T
            ).T.clone()
        }

    def _capture_symbolic_carried_cloth_shape(self, obj):
        if (
            not self.symbolic_cloth_carry_preserve_shape
            or getattr(obj, "prim_type", None) != PrimType.CLOTH
            or (
                self.symbolic_cloth_carry_preserve_shape_categories
                and getattr(obj, "category", "")
                not in self.symbolic_cloth_carry_preserve_shape_categories
            )
        ):
            return None
        return self._capture_symbolic_cloth_shape(obj)

    def _capture_symbolic_loaded_cloth_shape(self, obj):
        if (
            not self.symbolic_loaded_cloth_carry_preserve_shape
            or getattr(obj, "prim_type", None) != PrimType.CLOTH
            or (
                self.symbolic_loaded_cloth_carry_preserve_shape_categories
                and getattr(obj, "category", "")
                not in self.symbolic_loaded_cloth_carry_preserve_shape_categories
            )
        ):
            return None
        return self._capture_symbolic_cloth_shape(obj)

    @staticmethod
    def _restore_symbolic_carried_cloth_shape(obj, obj_pos, obj_orn, shape):
        if not shape:
            return
        relative_positions = shape["root_relative_positions"]
        rotation = T.quat2mat(obj_orn)
        world_positions = (rotation @ relative_positions.T).T + obj_pos
        obj.root_link.set_particle_positions(world_positions)
        obj.root_link.particle_velocities = torch.zeros_like(
            obj.root_link.particle_velocities
        )

    @staticmethod
    def _fit_symbolic_cloth_shape_to_extent(shape, root_orn, max_extent):
        """Compress a captured fold only when its release container is smaller."""
        if not shape:
            return shape
        relative_positions = shape.get("root_relative_positions")
        if relative_positions is None or relative_positions.ndim != 2:
            return shape
        rotation = T.quat2mat(root_orn)
        world_relative = (rotation @ relative_positions.T).T
        low = world_relative.min(dim=0).values
        high = world_relative.max(dim=0).values
        extent = high - low
        max_extent = torch.as_tensor(
            max_extent,
            dtype=world_relative.dtype,
            device=world_relative.device,
        )
        scale = torch.minimum(
            torch.ones_like(extent),
            max_extent / extent.clamp_min(1e-6),
        )
        if bool(torch.all(scale >= 0.999999).item()):
            return shape
        center = (low + high) / 2.0
        fitted_world_relative = (world_relative - center) * scale + center
        fitted_local = (rotation.T @ fitted_world_relative.T).T
        return {
            **shape,
            "root_relative_positions": fitted_local.clone(),
            "fit_scale": scale.clone(),
        }

    def _capture_symbolic_carried_rigid_descendants(self, root_obj):
        """Capture task objects recursively supported by ``root_obj``.

        Each descendant keeps both a root-relative pose for whole-container
        carry and a parent-relative pose for preserving nested Inside / OnTop
        relations. DUMP_INTO later selects only direct Inside children.
        """
        root_pos, root_orn = root_obj.get_position_orientation()
        captured = []
        seen = {id(root_obj)}

        def capture_children(parent_obj, top_level_obj=None, depth=1):
            for predicate in (object_states.Inside, object_states.OnTop):
                try:
                    placements = get_placement_objects(
                        parent_obj,
                        self.env,
                        predicate,
                    ) or []
                except Exception as exc:
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.EXECUTION_ERROR,
                        "Failed to enumerate complete loaded-object contents.",
                        {
                            "parent": parent_obj.name,
                            "predicate": predicate.__name__,
                            "error": str(exc),
                        },
                    ) from exc

                for placement in placements:
                    child_obj = placement.object
                    if child_obj is None or id(child_obj) in seen:
                        continue
                    if not hasattr(child_obj, "get_position_orientation"):
                        continue
                    seen.add(id(child_obj))
                    child_pos, child_orn = child_obj.get_position_orientation()
                    root_to_child_pos, root_to_child_orn = T.relative_pose_transform(
                        child_pos,
                        child_orn,
                        root_pos,
                        root_orn,
                    )
                    parent_pos, parent_orn = parent_obj.get_position_orientation()
                    parent_to_child_pos, parent_to_child_orn = T.relative_pose_transform(
                        child_pos,
                        child_orn,
                        parent_pos,
                        parent_orn,
                    )
                    top_level = child_obj if top_level_obj is None else top_level_obj
                    captured.append(
                        {
                            "obj": child_obj,
                            "parent": parent_obj,
                            "predicate": predicate,
                            "top_level": top_level,
                            "depth": depth,
                            "root_to_obj_pos": root_to_child_pos.clone(),
                            "root_to_obj_orn": root_to_child_orn.clone(),
                            "parent_to_obj_pos": parent_to_child_pos.clone(),
                            "parent_to_obj_orn": parent_to_child_orn.clone(),
                            "cloth_shape": self._capture_symbolic_loaded_cloth_shape(
                                child_obj
                            ),
                            "original_pos": child_pos.clone(),
                            "original_orn": child_orn.clone(),
                            "original_visible": bool(
                                getattr(child_obj, "visible", True)
                            ),
                            "collision_states": None,
                        }
                    )
                    capture_children(child_obj, top_level, depth + 1)

        capture_children(root_obj)
        if captured:
            print(
                "[starter][grasp][loaded_carry] "
                f"root={root_obj.name} "
                f"descendants={[state['obj'].name for state in captured]}"
            )
            sys.stdout.flush()
        return captured

    def _restore_symbolic_rigid_descendant_cloth_shapes(
        self,
        descendant_states,
    ):
        for state in descendant_states:
            cloth_shape = state.get("cloth_shape")
            if not cloth_shape:
                continue
            obj = state["obj"]
            obj_pos, obj_orn = obj.get_position_orientation()
            self._restore_symbolic_carried_cloth_shape(
                obj,
                obj_pos,
                obj_orn,
                cloth_shape,
            )

    def _suspend_symbolic_carried_rigid_descendants(self, descendant_states):
        """Suppress bundle-to-robot collisions while preserving render state."""
        for state in descendant_states:
            obj = state["obj"]
            state["collision_states"] = (
                self._suppress_symbolic_carried_object_collisions(obj)
            )
            try:
                obj.visible = state["original_visible"]
            except Exception:
                pass
            obj.keep_still()

    def _sync_symbolic_rigid_descendants_to_root_pose(
        self,
        descendant_states,
        root_pos,
        root_orn,
    ):
        for state in descendant_states:
            child_pos, child_orn = T.pose_transform(
                root_pos,
                root_orn,
                state["root_to_obj_pos"],
                state["root_to_obj_orn"],
            )
            child_obj = state["obj"]
            child_obj.set_position_orientation(
                position=child_pos,
                orientation=child_orn,
            )
            child_obj.keep_still()
            self._restore_symbolic_carried_cloth_shape(
                child_obj,
                child_pos,
                child_orn,
                state.get("cloth_shape"),
            )

    def _sync_symbolic_rigid_descendant_subtrees(self, descendant_states):
        """Rebuild nested descendants after a direct child has been resampled."""
        for state in sorted(descendant_states, key=lambda item: item["depth"]):
            if state["depth"] <= 1:
                continue
            parent_pos, parent_orn = state["parent"].get_position_orientation()
            child_pos, child_orn = T.pose_transform(
                parent_pos,
                parent_orn,
                state["parent_to_obj_pos"],
                state["parent_to_obj_orn"],
            )
            child_obj = state["obj"]
            child_obj.set_position_orientation(
                position=child_pos,
                orientation=child_orn,
            )
            child_obj.keep_still()
            self._restore_symbolic_carried_cloth_shape(
                child_obj,
                child_pos,
                child_orn,
                state.get("cloth_shape"),
            )

    def _restore_symbolic_rigid_descendant_snapshot(self, descendant_states):
        for state in descendant_states:
            obj = state["obj"]
            obj.set_position_orientation(
                position=state["original_pos"],
                orientation=state["original_orn"],
            )
            self._restore_symbolic_carried_cloth_shape(
                obj,
                state["original_pos"],
                state["original_orn"],
                state.get("cloth_shape"),
            )
            try:
                obj.visible = state["original_visible"]
            except Exception:
                pass
            obj.keep_still()

    def _restore_symbolic_carried_rigid_descendant_collisions(
        self,
        descendant_states,
    ):
        for state in descendant_states:
            collision_states = state.get("collision_states")
            if collision_states is not None:
                self._restore_symbolic_carried_object_collisions(
                    {
                        "obj": state["obj"],
                        "collision_states": collision_states,
                    }
                )
                state["collision_states"] = None
            try:
                state["obj"].visible = state["original_visible"]
            except Exception:
                pass

    def _symbolic_carried_rigid_descendant_states(self, obj):
        carry_state = self._symbolic_carry_state
        if not carry_state or carry_state.get("obj") != obj:
            return []
        return list(carry_state.get("rigid_descendant_states", []))

    @staticmethod
    def _rigid_descendant_postcondition_failures(descendant_states):
        failures = []
        for state in descendant_states:
            obj = state["obj"]
            parent = state["parent"]
            predicate = state.get("predicate", object_states.Inside)
            try:
                relation_holds = (
                    predicate in getattr(obj, "states", {})
                    and obj.states[predicate].get_value(parent)
                )
            except Exception:
                relation_holds = False
            if not relation_holds:
                failures.append((obj, parent, predicate))
        return failures

    def _ensure_symbolic_rigid_descendant_postconditions(
        self,
        descendant_states,
    ):
        """Repair once, then fail if a released loaded container lost contents."""
        failures = self._rigid_descendant_postcondition_failures(descendant_states)
        if not failures:
            return

        for obj, parent, predicate in failures:
            try:
                obj.states[predicate].set_value(
                    parent,
                    True,
                    reset_before_sampling=False,
                )
            except TypeError:
                obj.states[predicate].set_value(parent, True)
            except Exception:
                pass
        self._sync_symbolic_rigid_descendant_subtrees(descendant_states)
        self._restore_symbolic_rigid_descendant_cloth_shapes(descendant_states)
        yield from self._yield_symbolic_refresh_step()
        failures = self._rigid_descendant_postcondition_failures(descendant_states)
        if failures:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "Loaded-object carry did not preserve all Inside relations or OnTop relations.",
                {
                    "failed relations": [
                        {"object": obj.name, "parent": parent.name}
                        for obj, parent, _predicate in failures
                    ]
                },
            )

    def _sync_released_symbolic_payload_to_object(
        self,
        particle_states,
        rigid_descendant_states,
        obj,
        *,
        preserve_snapshot_velocities=False,
    ):
        self._sync_released_symbolic_particles_to_object(
            particle_states,
            obj,
            preserve_snapshot_velocities=preserve_snapshot_velocities,
        )
        if rigid_descendant_states:
            obj_pos, obj_orn = obj.get_position_orientation()
            self._sync_symbolic_rigid_descendants_to_root_pose(
                rigid_descendant_states,
                obj_pos,
                obj_orn,
            )
        carry_state = self._symbolic_carry_state
        if (
            carry_state is not None
            and carry_state.get("obj") is obj
            and carry_state.get("released_pending_payload", False)
        ):
            self._symbolic_carry_state = None

    def dump_carried_contents_into(self, source_obj, target_obj):
        """Atomically move all direct rigid contents into ``target_obj``.

        The source is implicit and remains in hand. No simulator step occurs
        between direct-child placements. A failed placement or postcondition
        restores every captured object to its pre-dump pose and, for symbolic
        carry, reattaches the complete payload to the held source.
        """
        if self._get_obj_in_hand() != source_obj:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "DUMP_INTO requires the source container to be held.",
                {"source": source_obj.name, "target": target_obj.name},
            )
        if source_obj is target_obj:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot dump a container into itself.",
                {"source": source_obj.name},
            )
        if (
            object_states.Open in getattr(target_obj, "states", {})
            and not target_obj.states[object_states.Open].get_value()
        ):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Open the destination before dumping into it.",
                {"target": target_obj.name},
            )

        carry_state = self._symbolic_carry_state
        symbolic_payload = bool(
            carry_state and carry_state.get("obj") == source_obj
        )
        descendant_states = (
            list(carry_state.get("rigid_descendant_states", []))
            if symbolic_payload
            else self._capture_symbolic_carried_rigid_descendants(source_obj)
        )
        direct_states = [
            state
            for state in descendant_states
            if state["parent"] is source_obj
            and state.get("predicate", object_states.Inside)
            is object_states.Inside
        ]
        if not direct_states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The held source container has no rigid contents to dump.",
                {"source": source_obj.name, "target": target_obj.name},
            )

        top_level_ids = {id(state["obj"]) for state in direct_states}
        detached_states = [
            state
            for state in descendant_states
            if id(state["top_level"]) in top_level_ids
        ]
        if any(state["obj"] is target_obj for state in detached_states):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot dump into an object contained by the source.",
                {"source": source_obj.name, "target": target_obj.name},
            )
        for state in direct_states:
            if object_states.Inside not in getattr(state["obj"], "states", {}):
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                    "A source content object does not support Inside.",
                    {"object": state["obj"].name, "target": target_obj.name},
                )

        dump_snapshot = []
        for state in detached_states:
            pos, orn = state["obj"].get_position_orientation()
            dump_snapshot.append((state, pos.clone(), orn.clone()))
        dump_cloth_shapes = {
            id(state): state.get("cloth_shape") for state in detached_states
        }
        cloth_drop_states = []

        def rollback_to_source():
            if symbolic_payload:
                carry_state["rigid_descendant_states"] = descendant_states
            for state, pos, orn in dump_snapshot:
                state["cloth_shape"] = dump_cloth_shapes[id(state)]
                state["obj"].set_position_orientation(
                    position=pos,
                    orientation=orn,
                )
                state["obj"].keep_still()
            self._sync_symbolic_rigid_descendant_subtrees(detached_states)
            self._restore_symbolic_rigid_descendant_cloth_shapes(detached_states)
            return self._rigid_descendant_postcondition_failures(detached_states)

        def place_inside_target(state):
            obj = state["obj"]
            source_pos, source_orn = obj.get_position_orientation()
            try:
                placed = obj.states[object_states.Inside].set_value(
                    target_obj,
                    True,
                    reset_before_sampling=False,
                )
            except TypeError:
                placed = obj.states[object_states.Inside].set_value(
                    target_obj,
                    True,
                )
            if placed is not False or not self._should_use_symbolic_dump_cloth_drop(
                state,
                target_obj,
            ):
                return placed
            state["cloth_shape"] = self._prepare_symbolic_dump_cloth_drop(
                state,
                target_obj,
                source_pos,
                source_orn,
            )
            cloth_drop_states.append(state)
            return True

        try:
            for state in direct_states:
                placed = place_inside_target(state)
                if placed is False:
                    raise RuntimeError(
                        f'Inside sampler rejected object "{state["obj"].name}"'
                    )
            self._sync_symbolic_rigid_descendant_subtrees(detached_states)
            self._restore_symbolic_rigid_descendant_cloth_shapes(detached_states)
        except Exception as exc:
            rollback_failures = rollback_to_source()
            rollback_complete = not rollback_failures
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                (
                    "DUMP_INTO failed; all contents were rolled back."
                    if rollback_complete
                    else "DUMP_INTO failed and rollback did not restore all source relations."
                ),
                {
                    "source": source_obj.name,
                    "target": target_obj.name,
                    "error": str(exc),
                    "rollback_failed_relations": [
                        {"object": obj.name, "parent": parent.name}
                        for obj, parent, _predicate in rollback_failures
                    ],
                },
            ) from exc

        detached_object_ids = {id(state["obj"]) for state in detached_states}
        remaining_states = [
            state
            for state in descendant_states
            if id(state["obj"]) not in detached_object_ids
        ]
        stabilization = getattr(
            self,
            "_released_loaded_cloth_stabilization",
            None,
        )
        if stabilization and self._same_runtime_object(
            stabilization.get("root"),
            source_obj,
        ):
            kept_states = [
                state
                for state in stabilization.get("states", [])
                if id(state.get("obj")) not in detached_object_ids
            ]
            if kept_states:
                stabilization["states"] = kept_states
            else:
                self._released_loaded_cloth_stabilization = None
        if symbolic_payload:
            carry_state["rigid_descendant_states"] = remaining_states
        self._restore_symbolic_carried_rigid_descendant_collisions(
            detached_states
        )
        settle_steps = (
            self.symbolic_cloth_inside_settle_steps if cloth_drop_states else 1
        )
        for _ in range(settle_steps):
            yield from self._yield_symbolic_refresh_step()

        failures = []
        for state in direct_states:
            try:
                if not state["obj"].states[object_states.Inside].get_value(
                    target_obj
                ):
                    failures.append(
                        (state["obj"], target_obj, object_states.Inside)
                    )
            except Exception:
                failures.append((state["obj"], target_obj, object_states.Inside))
        nested_states = [state for state in detached_states if state["depth"] > 1]
        failures.extend(self._rigid_descendant_postcondition_failures(nested_states))
        if failures:
            rollback_failures = rollback_to_source()
            if symbolic_payload:
                self._suspend_symbolic_carried_rigid_descendants(
                    detached_states
                )
            yield from self._yield_symbolic_refresh_step()
            rollback_failures = self._rigid_descendant_postcondition_failures(
                detached_states
            )
            rollback_complete = not rollback_failures
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                (
                    "DUMP_INTO postcondition failed; all contents were rolled back."
                    if rollback_complete
                    else "DUMP_INTO postcondition failed and rollback did not restore all source relations."
                ),
                {
                    "source": source_obj.name,
                    "target": target_obj.name,
                    "failed relations": [
                        {"object": obj.name, "parent": parent.name}
                        for obj, parent, _predicate in failures
                    ],
                    "rollback_failed_relations": [
                        {"object": obj.name, "parent": parent.name}
                        for obj, parent, _predicate in rollback_failures
                    ],
                },
            )

        for state in cloth_drop_states:
            obj = state["obj"]
            cloth_center, _, cloth_extent, _ = obj.get_base_aligned_bbox()
            print(
                "[starter][dump][cloth_drop] succeeded "
                f"object={obj.name} target={target_obj.name} "
                f"cloth_center={self._to_float_list(cloth_center)} "
                f"cloth_extent={self._to_float_list(cloth_extent)} "
                f"settle_steps={settle_steps}"
            )
        if cloth_drop_states:
            sys.stdout.flush()

        return [state["obj"] for state in direct_states]

    def _capture_symbolic_carried_particles(self, obj):
        """Capture physical particles currently inside a symbolic carry object."""
        if object_states.ContainedParticles not in getattr(obj, "states", {}):
            return []

        obj_pos, obj_orn = obj.get_position_orientation()
        particle_states = []
        for system in obj.scene.system_registry.objects:
            try:
                if not obj.scene.is_physical_particle_system(system_name=system.name):
                    continue
                contained_state = obj.states[object_states.ContainedParticles]
                contained_state.clear_cache()
                contained = contained_state.get_value(system)
                particle_indices = torch.nonzero(
                    contained.in_volume,
                    as_tuple=False,
                ).flatten()
                if particle_indices.numel() == 0:
                    continue
                world_positions = contained.positions[particle_indices]
                instancer = getattr(system, "default_particle_instancer", None)
                if instancer is None:
                    raise RuntimeError("contained physical particles have no instancer")
                particle_states.append(
                    {
                        "system": system,
                        "instancer": instancer,
                        "instancer_particle_count": int(instancer.n_particles),
                        "instancer_idn": int(getattr(instancer, "idn", 0)),
                        "indices": particle_indices.clone(),
                        "local_positions": self._world_points_to_local_frame(
                            world_positions,
                            obj_pos,
                            obj_orn,
                        ),
                        "velocities": instancer.particle_velocities[
                            particle_indices
                        ].clone(),
                        "orientations": instancer.particle_orientations[
                            particle_indices
                        ].clone(),
                        "scales": instancer.particle_scales[
                            particle_indices
                        ].clone(),
                        "prototype_indices": instancer.particle_prototype_ids[
                            particle_indices
                        ].clone(),
                        "particle_group": int(
                            getattr(instancer, "particle_group", 0)
                        ),
                        "suspended": False,
                    }
                )
            except Exception as exc:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Failed to capture complete loaded-container particles.",
                    {
                        "object": obj.name,
                        "system": getattr(system, "name", None),
                        "error": str(exc),
                    },
                ) from exc

        if particle_states:
            particle_count = sum(
                int(state["indices"].numel()) for state in particle_states
            )
            print(
                "[starter][grasp][symbolic_carry] "
                f"captured_contained_particles object={obj.name} "
                f"systems={[state['system'].name for state in particle_states]} "
                f"count={particle_count}"
            )
            sys.stdout.flush()
        return particle_states

    def _suspend_symbolic_carried_particles(self, particle_states):
        """Remove carried rows from PhysX while retaining a complete payload."""
        if not particle_states:
            return
        for particle_state in particle_states:
            system = particle_state["system"]
            instancer = particle_state["instancer"]
            if getattr(system, "default_particle_instancer", None) is not instancer:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Cannot suspend particles from a replaced instancer.",
                    {"system": system.name},
                )
            if int(instancer.n_particles) != int(
                particle_state["instancer_particle_count"]
            ):
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Cannot suspend particles after the instancer count changed.",
                    {"system": system.name},
                )
            indices = particle_state["indices"].to(dtype=torch.long)
            expected_count = int(particle_state["instancer_particle_count"]) - int(
                indices.numel()
            )
            instancer_snapshot = {
                "positions": instancer.particle_positions.clone(),
                "velocities": instancer.particle_velocities.clone(),
                "orientations": instancer.particle_orientations.clone(),
                "scales": instancer.particle_scales.clone(),
                "prototype_indices": instancer.particle_prototype_ids.clone(),
                "instancer_idn": int(getattr(instancer, "idn", 0)),
                "particle_group": int(getattr(instancer, "particle_group", 0)),
                "particle_count": int(instancer.n_particles),
            }
            try:
                system.remove_particles(idxs=indices)
                if int(system.n_particles) != expected_count:
                    raise RuntimeError(
                        "particle suspension removed an incomplete payload: "
                        f"expected {expected_count}, got {int(system.n_particles)}"
                    )
            except Exception as exc:
                rollback_error = None
                if int(system.n_particles) != instancer_snapshot["particle_count"]:
                    try:
                        current_count = int(system.n_particles)
                        if current_count:
                            system.remove_particles(
                                idxs=torch.arange(
                                    0,
                                    current_count,
                                    dtype=torch.long,
                                )
                            )
                        if int(system.n_particles) != 0:
                            raise RuntimeError(
                                "failed to clear the partially modified instancer"
                            )
                        system.generate_particles(
                            positions=instancer_snapshot["positions"],
                            instancer_idn=instancer_snapshot["instancer_idn"],
                            velocities=instancer_snapshot["velocities"],
                            orientations=instancer_snapshot["orientations"],
                            scales=instancer_snapshot["scales"],
                            prototype_indices=instancer_snapshot[
                                "prototype_indices"
                            ].tolist(),
                            particle_group=instancer_snapshot["particle_group"],
                        )
                        if int(system.n_particles) != instancer_snapshot[
                            "particle_count"
                        ]:
                            raise RuntimeError(
                                "full instancer rollback restored the wrong count"
                            )
                        restored_instancer = system.default_particle_instancer
                        if restored_instancer is None:
                            raise RuntimeError(
                                "full instancer rollback produced no instancer"
                            )
                        particle_state["instancer"] = restored_instancer
                    except Exception as rollback_exc:
                        rollback_error = (
                            f"{type(rollback_exc).__name__}: {rollback_exc}"
                        )
                particle_state["suspended"] = False
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Particle suspension failed before the payload was committed.",
                    {
                        "system": system.name,
                        "expected remaining": expected_count,
                        "actual remaining": int(system.n_particles),
                        "error": f"{type(exc).__name__}: {exc}",
                        "rollback_error": rollback_error,
                    },
                ) from exc
            particle_state["suspended"] = True
            print(
                "[starter][grasp][particle_suspension] "
                f"system={system.name} count={int(indices.numel())} "
                "mode=removed_payload_snapshot"
            )
            sys.stdout.flush()

    @staticmethod
    def _world_points_to_local_frame(world_points, frame_pos, frame_orn):
        world_points = torch.as_tensor(world_points)
        frame_pos = torch.as_tensor(
            frame_pos,
            dtype=world_points.dtype,
            device=world_points.device,
        )
        frame_orn = torch.as_tensor(
            frame_orn,
            dtype=world_points.dtype,
            device=world_points.device,
        )
        rotation = T.quat2mat(frame_orn)
        return ((world_points - frame_pos) @ rotation).clone()

    @staticmethod
    def _local_points_to_world_frame(local_points, frame_pos, frame_orn):
        local_points = torch.as_tensor(local_points)
        frame_pos = torch.as_tensor(
            frame_pos,
            dtype=local_points.dtype,
            device=local_points.device,
        )
        frame_orn = torch.as_tensor(
            frame_orn,
            dtype=local_points.dtype,
            device=local_points.device,
        )
        rotation = T.quat2mat(frame_orn)
        return local_points @ rotation.T + frame_pos

    def _sync_symbolic_carried_particles_to_pose(
        self,
        particle_states,
        obj_pos,
        obj_orn,
        *,
        zero_velocities,
    ):
        """Materialize a suspended payload at a stationary container pose."""
        suspended_states = [
            state for state in particle_states if state.get("suspended", False)
        ]
        if not suspended_states:
            return
        world_positions = [
            self._local_points_to_world_frame(
                state["local_positions"],
                obj_pos,
                obj_orn,
            )
            for state in suspended_states
        ]
        try:
            self._generate_suspended_particle_states(
                suspended_states,
                world_positions,
                zero_velocities=zero_velocities,
            )
        except Exception as exc:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Failed to restore the complete loaded-container payload.",
                {
                    "systems": [state["system"].name for state in suspended_states],
                    "error": str(exc),
                },
            ) from exc

    def _generate_suspended_particle_states(
        self,
        particle_states,
        world_positions,
        *,
        zero_velocities,
    ):
        if len(particle_states) != len(world_positions):
            raise ValueError("particle states and destination positions must align")

        generated_plans = []
        try:
            for state, positions in zip(particle_states, world_positions):
                if not state.get("suspended", False):
                    raise RuntimeError("particle payload is not suspended")
                system = state["system"]
                positions = torch.as_tensor(positions)
                expected_count = int(state["local_positions"].shape[0])
                if int(positions.shape[0]) != expected_count:
                    raise RuntimeError("particle destination count changed")

                start_count = int(system.n_particles)
                snapshot = {
                    "instancer": state.get("instancer"),
                    "instancer_particle_count": state.get(
                        "instancer_particle_count"
                    ),
                    "indices": state.get("indices"),
                    "suspended": state.get("suspended", False),
                }
                try:
                    system.generate_particles(
                        positions=positions,
                        instancer_idn=state["instancer_idn"],
                        velocities=(
                            torch.zeros_like(positions)
                            if zero_velocities
                            else state["velocities"]
                        ),
                        orientations=state["orientations"],
                        scales=state["scales"],
                        prototype_indices=state["prototype_indices"].tolist(),
                        particle_group=state["particle_group"],
                    )
                except Exception:
                    generated_count = max(0, int(system.n_particles) - start_count)
                    if generated_count:
                        generated_plans.append(
                            {
                                "state": state,
                                "system": system,
                                "start_count": start_count,
                                "generated_count": generated_count,
                                "post_count": int(system.n_particles),
                                "snapshot": snapshot,
                            }
                        )
                    raise

                generated_count = int(system.n_particles) - start_count
                generated_plans.append(
                    {
                        "state": state,
                        "system": system,
                        "start_count": start_count,
                        "generated_count": generated_count,
                        "post_count": int(system.n_particles),
                        "snapshot": snapshot,
                    }
                )
                if generated_count != expected_count:
                    raise RuntimeError(
                        f"generated {generated_count} of {expected_count} particles"
                    )

            for plan in generated_plans:
                state = plan["state"]
                instancer = plan["system"].default_particle_instancer
                if instancer is None:
                    raise RuntimeError("particle generation produced no instancer")
                start = plan["start_count"]
                stop = start + plan["generated_count"]
                state["instancer"] = instancer
                state["instancer_particle_count"] = plan["post_count"]
                state["indices"] = torch.arange(start, stop, dtype=torch.long)
                state["suspended"] = False
            if generated_plans:
                og.sim.update_handles()
        except Exception:
            rollback_errors = self._remove_generated_particle_plans(
                generated_plans
            )
            for plan in generated_plans:
                plan["state"].update(plan["snapshot"])
            if rollback_errors:
                raise RuntimeError(
                    "particle generation failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                )
            raise
        return generated_plans

    @staticmethod
    def _remove_generated_particle_plans(generated_plans):
        errors = []
        for plan in reversed(generated_plans):
            system = plan["system"]
            if int(system.n_particles) != int(plan["post_count"]):
                errors.append(
                    f"{system.name}: count changed before rollback "
                    f"({int(system.n_particles)} != {int(plan['post_count'])})"
                )
                continue
            start = int(plan["start_count"])
            stop = start + int(plan["generated_count"])
            try:
                system.remove_particles(
                    idxs=torch.arange(start, stop, dtype=torch.long)
                )
                if int(system.n_particles) != start:
                    raise RuntimeError(
                        f"remaining count {int(system.n_particles)} != {start}"
                    )
            except Exception as exc:
                errors.append(f"{system.name}: {type(exc).__name__}: {exc}")
        return errors

    def _sample_symbolic_transfer_positions(self, target_obj, system, count):
        """Sample existing particle destinations inside a target container."""
        contained_state = target_obj.states[object_states.ContainedParticles]
        link = contained_state.link
        try:
            low, high = link.visual_aabb
        except ValueError:
            low, high = target_obj.aabb

        radius = float(system.particle_radius)
        spacing = float(system.particle_particle_rest_distance)
        axes = [
            torch.arange(
                float(axis_low) + radius,
                float(axis_high) - radius + 1e-10,
                spacing,
            )
            for axis_low, axis_high in zip(low, high)
        ]
        if any(axis.numel() == 0 for axis in axes):
            candidates = torch.empty((0, 3), dtype=torch.float32)
        else:
            candidates = torch.cartesian_prod(*axes).reshape(-1, 3)
            candidates = candidates[contained_state.check_in_volume(candidates)]

        contact_free_count = 0
        if len(candidates) >= count and hasattr(system, "check_in_contact"):
            try:
                contact_free = candidates[
                    ~system.check_in_contact(candidates).to(dtype=torch.bool)
                ]
                contact_free_count = len(contact_free)
                if contact_free_count >= count:
                    candidates = contact_free
            except Exception:
                contact_free_count = 0

        if len(candidates) < count:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Target container volume cannot hold the complete particle payload.",
                {
                    "target": target_obj.name,
                    "system": system.name,
                    "required": int(count),
                    "sampled inside": int(len(candidates)),
                    "contact free": int(contact_free_count),
                },
            )

        center = torch.tensor(
            [
                (float(axis_low) + float(axis_high)) / 2.0
                for axis_low, axis_high in zip(low, high)
            ],
            dtype=candidates.dtype,
            device=candidates.device,
        )
        order = torch.argsort(torch.sum((candidates - center) ** 2, dim=1))
        selected = candidates[order[:count]]
        print(
            "[starter][pour][target_volume] "
            f"target={target_obj.name} system={system.name} "
            f"selected={len(selected)} candidates={len(candidates)} "
            f"contact_free={contact_free_count}"
        )
        sys.stdout.flush()
        return selected

    def _symbolic_carried_particle_states(self, obj):
        carry_state = self._symbolic_carry_state
        if not carry_state or carry_state.get("obj") != obj:
            return []
        return list(carry_state.get("particle_states", []))

    def defer_symbolic_carried_coverage(self, obj, systems):
        """Defer physical rinse particles until the carried object is placed."""
        carry_state = self._symbolic_carry_state
        if not carry_state or carry_state.get("obj") is not obj:
            return set()

        pending = {
            system.name: system
            for system in carry_state.get("pending_covered_systems", [])
        }
        deferred = set()
        for system in systems:
            try:
                is_physical = obj.scene.is_physical_particle_system(
                    system_name=system.name
                )
            except Exception:
                is_physical = False
            if not is_physical:
                continue
            pending[system.name] = system
            deferred.add(system.name)

        carry_state["pending_covered_systems"] = list(pending.values())
        if deferred:
            print(
                "[starter][wipe][symbolic_carry] "
                f"deferred_coverage object={obj.name} systems={sorted(deferred)}"
            )
            sys.stdout.flush()
        return deferred

    def _pending_symbolic_carried_coverage(self, obj):
        carry_state = self._symbolic_carry_state
        if not carry_state or carry_state.get("obj") is not obj:
            return []
        return list(carry_state.get("pending_covered_systems", []))

    def _finalize_pending_symbolic_coverage(
        self,
        obj,
        systems,
        max_samples=200,
    ):
        """Materialize deferred rinse particles after collisions are restored."""
        systems = list(systems or [])
        if not systems:
            return
        max_samples = min(
            int(max_samples),
            self.deferred_coverage_max_samples,
        )
        if max_samples < 1:
            raise ValueError("max_samples must be at least 1")

        generated = {}
        failed = []
        covered_state = getattr(obj, "states", {}).get(object_states.Covered)
        if covered_state is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Cannot materialize deferred coverage on an object without Covered state.",
                {"object": obj.name},
            )

        for system in systems:
            try:
                if covered_state.get_value(system):
                    generated[system.name] = 0
                    continue
                if obj.scene.is_physical_particle_system(system_name=system.name):
                    before_count = int(system.n_particles)
                    success = system.generate_particles_on_object(
                        obj=obj,
                        max_samples=int(max_samples),
                        min_samples_for_success=1,
                    )
                    generated_count = int(system.n_particles) - before_count
                    generated[system.name] = generated_count
                    if (
                        not success
                        or generated_count < 1
                        or generated_count > int(max_samples)
                    ):
                        failed.append(system.name)
                elif not covered_state.set_value(system, True):
                    generated[system.name] = 0
                    failed.append(system.name)
                else:
                    generated[system.name] = 0
            except Exception as exc:
                generated[system.name] = 0
                failed.append(system.name)
                self._log_verbose(
                    "[starter][wipe][symbolic_carry] "
                    f"failed_to_materialize object={obj.name} system={system.name} "
                    f"error={type(exc).__name__}: {exc}"
                )

        for _ in range(2):
            yield from self._yield_symbolic_refresh_step()

        missing = [
            system.name
            for system in systems
            if not covered_state.get_value(system)
        ]
        failed = sorted(set(failed + missing))
        if failed:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Deferred symbolic rinse coverage did not persist after placement.",
                {
                    "object": obj.name,
                    "failed systems": failed,
                    "generated particles": generated,
                    "max samples per system": int(max_samples),
                },
            )

        print(
            "[starter][wipe][symbolic_carry] "
            f"materialized_coverage object={obj.name} "
            f"systems={sorted(generated)} generated={generated}"
        )
        sys.stdout.flush()

    def symbolic_carried_particle_systems(self, obj=None):
        """Return logically contained systems while symbolic particles are suspended."""
        carry_state = self._symbolic_carry_state
        if not carry_state or (obj is not None and carry_state.get("obj") != obj):
            return []
        return [
            particle_state["system"]
            for particle_state in carry_state.get("particle_states", [])
        ]

    def transfer_symbolic_carried_particles_to_target(self, source_obj, target_obj):
        """Atomically stage carried particle rows inside a target container."""
        carry_state = self._symbolic_carry_state
        if not carry_state or carry_state.get("obj") != source_obj:
            return None
        if object_states.ContainedParticles not in getattr(target_obj, "states", {}):
            return None
        if self._pending_symbolic_particle_transfer is not None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "A symbolic particle transfer is already awaiting verification.",
            )

        particle_states = list(carry_state.get("particle_states", []))
        if not particle_states or not all(
            state.get("suspended", False) for state in particle_states
        ):
            return None

        target_positions = []
        transfer_results = []
        for particle_state in particle_states:
            system = particle_state["system"]
            available_count = int(particle_state["local_positions"].shape[0])
            target_positions.append(
                self._sample_symbolic_transfer_positions(
                    target_obj,
                    system,
                    available_count,
                )
            )
            transfer_results.append(
                {
                    "system": system,
                    "available_count": available_count,
                    "transferred_count": available_count,
                    "remaining_count": 0,
                }
            )

        try:
            transfer_plans = self._generate_suspended_particle_states(
                particle_states,
                target_positions,
                zero_velocities=True,
            )
        except Exception as exc:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Failed to stage the complete particle payload in the target.",
                {
                    "source": source_obj.name,
                    "target": target_obj.name,
                    "error": str(exc),
                },
            ) from exc

        carry_state["particle_states"] = []
        self._pending_symbolic_particle_transfer = {
            "source_obj": source_obj,
            "target_obj": target_obj,
            "carry_state": carry_state,
            "particle_states": particle_states,
            "plans": transfer_plans,
        }
        print(
            "[starter][pour][physical_transfer] "
            f"source={source_obj.name} target={target_obj.name} "
            "mode=generated_payload_pending_verification "
            f"systems={[(result['system'].name, result['transferred_count'], result['remaining_count']) for result in transfer_results]}"
        )
        sys.stdout.flush()
        return transfer_results

    def commit_symbolic_particle_transfer(self, source_obj, target_obj):
        pending = self._pending_symbolic_particle_transfer
        if pending is None:
            return False
        if (
            pending["source_obj"] is not source_obj
            or pending["target_obj"] is not target_obj
        ):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Pending symbolic particle transfer does not match commit request.",
            )
        self._pending_symbolic_particle_transfer = None
        print(
            "[starter][pour][physical_transfer] "
            f"committed source={source_obj.name} target={target_obj.name}"
        )
        sys.stdout.flush()
        return True

    def rollback_symbolic_particle_transfer(self, source_obj, target_obj):
        pending = self._pending_symbolic_particle_transfer
        if pending is None:
            return False
        if (
            pending["source_obj"] is not source_obj
            or pending["target_obj"] is not target_obj
        ):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Pending symbolic particle transfer does not match rollback request.",
            )

        try:
            rollback_errors = self._remove_generated_particle_plans(
                pending["plans"]
            )
            if rollback_errors:
                raise RuntimeError("; ".join(rollback_errors))
            for plan in pending["plans"]:
                plan["state"].update(plan["snapshot"])
            pending["carry_state"]["particle_states"] = list(
                pending["particle_states"]
            )
            self._pending_symbolic_particle_transfer = None
        except Exception as exc:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Failed to roll back symbolic particle transfer.",
                {
                    "source": source_obj.name,
                    "target": target_obj.name,
                    "error": str(exc),
                },
            ) from exc

        print(
            "[starter][pour][physical_transfer] "
            f"rolled_back source={source_obj.name} target={target_obj.name}"
        )
        sys.stdout.flush()
        return True

    def _sync_released_symbolic_particles_to_object(
        self,
        particle_states,
        obj,
        *,
        preserve_snapshot_velocities=False,
    ):
        if not particle_states:
            return
        obj_pos, obj_orn = obj.get_position_orientation()
        self._sync_symbolic_carried_particles_to_pose(
            particle_states,
            obj_pos,
            obj_orn,
            zero_velocities=not preserve_snapshot_velocities,
        )

    @staticmethod
    def _symbolic_carry_robot_filter_key(obj_link, robot_link):
        return str(obj_link.prim_path), str(robot_link.prim_path)

    @staticmethod
    def _robot_collision_pair_is_filtered(obj_link, robot_link):
        obj_targets = (
            obj_link._collision_filter_api.GetFilteredPairsRel().GetTargets()
        )
        robot_targets = (
            robot_link._collision_filter_api.GetFilteredPairsRel().GetTargets()
        )
        return any(
            str(target) == str(robot_link.prim_path) for target in obj_targets
        ) or any(
            str(target) == str(obj_link.prim_path) for target in robot_targets
        )

    def _uses_episode_symbolic_carry_robot_filters(self, obj) -> bool:
        if self.symbolic_carry_robot_collision_filter_scope != "episode":
            return False
        configured_names = self.symbolic_carry_robot_collision_filter_objects
        if not configured_names:
            return True
        if obj is None:
            return False
        object_names = {
            str(name).strip().lower()
            for name in self._name_variants_for_object(obj)
            if str(name).strip()
        }
        return bool(object_names.intersection(configured_names))

    def _register_episode_symbolic_carry_robot_filter(
        self,
        obj,
        obj_link,
        robot_link,
    ):
        key = self._symbolic_carry_robot_filter_key(obj_link, robot_link)
        self._symbolic_carry_robot_filter_registry.setdefault(
            key,
            {
                "obj": obj,
                "obj_link": obj_link,
                "robot_link": robot_link,
            },
        )
        return key

    def _suppress_symbolic_carried_object_collisions(self, obj):
        """Disable carried-object collisions while preserving visual carry.

        Symbolic carry teleports the held object to the end-effector before
        navigation actions.  Keeping collisions enabled makes larger objects
        such as the detergent bottle push against the table, cabinets, or the
        robot body and can lift Fetch off the traversable floor.  During carry
        the object is already logically held via ``_ag_obj_in_hand``, so its
        collision meshes can be disabled and restored on PLACE / RELEASE.

        GPU dynamics does not allow changing ``collision_enabled`` while the
        simulator is playing.  In that mode, use USD filtered-pair
        relationships against every robot link instead, preserving collisions
        with scene geometry while preventing the held object from pushing the
        robot.
        """
        collision_states = []
        link_visual_only_states = []
        filtered_pairs = []
        robot_filter_scope = (
            "episode"
            if self._uses_episode_symbolic_carry_robot_filters(obj)
            else "release"
        )

        try:
            sim_requires_filtered_pairs = not bool(og.sim.is_stopped())
        except Exception:
            # Collision attributes are only safe to mutate when stopped. If
            # simulator state cannot be queried, keep the conservative runtime
            # path instead of risking an ineffective collision update.
            sim_requires_filtered_pairs = True

        if sim_requires_filtered_pairs:
            articulation_joints = getattr(obj, "joints", {})
            if articulation_joints:
                # Applying filtered-pair USD relationships while PhysX is
                # running invalidates tensor views for articulated containers.
                # The kinematic carry pose already keeps the root outside the
                # robot body; descendants can still use ordinary pair filters.
                print(
                    "[starter][grasp][symbolic_carry] "
                    "skipped_articulated_robot_collision_filter "
                    f"object={obj.name} joints={len(articulation_joints)}"
                )
                sys.stdout.flush()
                return {
                    "collision_meshes": collision_states,
                    "link_visual_only": link_visual_only_states,
                    "filtered_pairs": filtered_pairs,
                    "robot_filter_scope": robot_filter_scope,
                }
            robot_links = list(getattr(self.robot, "links", {}).values())
            requested_pairs = 0
            already_filtered_pairs = 0
            for obj_link in getattr(obj, "links", {}).values():
                for robot_link in robot_links:
                    requested_pairs += 1
                    try:
                        already_filtered = (
                            self._robot_collision_pair_is_filtered(
                                obj_link,
                                robot_link,
                            )
                        )
                    except Exception as exc:
                        self._log_verbose(
                            "[starter][grasp][symbolic_carry] "
                            "failed_to_read_robot_collision_filter "
                            f"object={obj.name} robot_link={robot_link.name} "
                            f"error={exc}"
                        )
                        continue
                    if already_filtered:
                        already_filtered_pairs += 1
                        continue
                    try:
                        obj_link.add_filtered_collision_pair(robot_link)
                    except Exception as exc:
                        relation_probe_error = None
                        try:
                            pair_was_added = (
                                self._robot_collision_pair_is_filtered(
                                    obj_link,
                                    robot_link,
                                )
                            )
                        except Exception as probe_exc:
                            # The pre-add read proved this relationship was not
                            # already present. If the mutating call and the
                            # follow-up read both fail, retain ownership so a
                            # later release / episode cleanup can remove any
                            # partial USD mutation instead of leaking it.
                            pair_was_added = True
                            relation_probe_error = probe_exc
                        if not pair_was_added:
                            self._log_verbose(
                                "[starter][grasp][symbolic_carry] "
                                "failed_to_add_robot_collision_filter "
                                f"object={obj.name} robot_link={robot_link.name} "
                                f"error={exc}"
                            )
                            continue
                        if relation_probe_error is None:
                            self._log_verbose(
                                "[starter][grasp][symbolic_carry] "
                                "collision_filter_add_raised_after_relation_update "
                                f"object={obj.name} robot_link={robot_link.name} "
                                f"error={exc}"
                            )
                        else:
                            self._log_verbose(
                                "[starter][grasp][symbolic_carry] "
                                "collision_filter_add_state_unknown_owned_for_cleanup "
                                f"object={obj.name} robot_link={robot_link.name} "
                                f"add_error={exc} probe_error={relation_probe_error}"
                            )
                    filtered_pairs.append((obj_link, robot_link))
                    if robot_filter_scope == "episode":
                        self._register_episode_symbolic_carry_robot_filter(
                            obj,
                            obj_link,
                            robot_link,
                        )
            print(
                "[starter][grasp][symbolic_carry] "
                f"filtered_robot_collisions object={obj.name} "
                f"pairs={len(filtered_pairs)} requested={requested_pairs} "
                f"already_present={already_filtered_pairs} "
                f"scope={robot_filter_scope} "
                f"owned={len(self._symbolic_carry_robot_filter_registry)}"
            )
            sys.stdout.flush()
            return {
                "collision_meshes": collision_states,
                "link_visual_only": link_visual_only_states,
                "filtered_pairs": filtered_pairs,
                "robot_filter_scope": robot_filter_scope,
            }

        for link in getattr(obj, "links", {}).values():
            for collision_mesh in getattr(link, "collision_meshes", {}).values():
                try:
                    was_enabled = bool(collision_mesh.collision_enabled)
                    collision_states.append((collision_mesh, was_enabled))
                    if was_enabled:
                        collision_mesh.collision_enabled = False
                except Exception as exc:
                    self._log_verbose(
                        "[starter][grasp][symbolic_carry] "
                        "failed_to_disable_collision "
                        f"object={obj.name} error={exc}"
                    )
            try:
                was_visual_only = bool(link.visual_only)
                link_visual_only_states.append((link, was_visual_only))
                if not was_visual_only:
                    link.visual_only = True
            except Exception as exc:
                self._log_verbose(
                    "[starter][grasp][symbolic_carry] "
                    "failed_to_disable_link_physics "
                    f"object={obj.name} error={exc}"
                )
        return {
            "collision_meshes": collision_states,
            "link_visual_only": link_visual_only_states,
            "filtered_pairs": filtered_pairs,
            "robot_filter_scope": robot_filter_scope,
        }

    def _restore_symbolic_carried_object_collisions(
        self,
        carry_state=None,
    ):
        if carry_state is None:
            carry_state = self._symbolic_carry_state
        if not carry_state:
            return

        obj = carry_state.get("obj")
        physics_states = carry_state.get("collision_states", {})
        for link, was_visual_only in physics_states.get("link_visual_only", []):
            try:
                link.visual_only = was_visual_only
            except Exception as exc:
                self._log_verbose(
                    "[starter][grasp][symbolic_carry] "
                    "failed_to_restore_link_physics "
                    f"object={None if obj is None else obj.name} error={exc}"
                )
        for collision_mesh, was_enabled in physics_states.get(
            "collision_meshes", []
        ):
            try:
                collision_mesh.collision_enabled = was_enabled
            except Exception as exc:
                self._log_verbose(
                    "[starter][grasp][symbolic_carry] "
                    "failed_to_restore_collision "
                    f"object={None if obj is None else obj.name} error={exc}"
                )
        filtered_pairs = list(physics_states.get("filtered_pairs", []))
        if physics_states.get("robot_filter_scope") == "episode":
            print(
                "[starter][grasp][symbolic_carry] "
                f"retained_robot_collision_filters object={obj.name} "
                f"pairs={len(filtered_pairs)} "
                f"owned={len(self._symbolic_carry_robot_filter_registry)}"
            )
            sys.stdout.flush()
        else:
            removed_pairs = 0
            deferred_cleanup_pairs = 0
            for obj_link, robot_link in filtered_pairs:
                try:
                    obj_link.remove_filtered_collision_pair(robot_link)
                    removed_pairs += 1
                except Exception as exc:
                    # A failed release-time removal must not be forgotten. The
                    # simulator may need to stop before USD accepts the edit,
                    # so transfer ownership to the lifecycle registry and let
                    # end_episode() retry it after the stop boundary.
                    self._register_episode_symbolic_carry_robot_filter(
                        obj,
                        obj_link,
                        robot_link,
                    )
                    deferred_cleanup_pairs += 1
                    self._log_verbose(
                        "[starter][grasp][symbolic_carry] "
                        "failed_to_remove_robot_collision_filter "
                        f"object={None if obj is None else obj.name} "
                        f"robot_link={getattr(robot_link, 'name', None)} error={exc}"
                    )
            if filtered_pairs:
                print(
                    "[starter][grasp][symbolic_carry] "
                    f"removed_robot_collision_filters "
                    f"object={None if obj is None else obj.name} "
                    f"removed={removed_pairs} requested={len(filtered_pairs)} "
                    f"deferred_cleanup={deferred_cleanup_pairs}"
                )
                sys.stdout.flush()
        physics_states["filtered_pairs"] = []
        physics_states["collision_meshes"] = []
        physics_states["link_visual_only"] = []

    def end_episode(self):
        """Remove controller-owned episode filters after simulation stops."""
        try:
            simulator_stopped = bool(og.sim.is_stopped())
        except Exception as exc:
            raise RuntimeError(
                "Cannot verify simulator state before collision-filter cleanup"
            ) from exc
        if not simulator_stopped:
            raise RuntimeError(
                "Episode collision filters may only be removed while the simulator is stopped"
            )

        owned_pairs = list(self._symbolic_carry_robot_filter_registry.items())
        removed_pairs = 0
        failures = []
        for key, entry in owned_pairs:
            obj_link = entry["obj_link"]
            robot_link = entry["robot_link"]
            try:
                obj_link.remove_filtered_collision_pair(robot_link)
                removed_pairs += 1
                self._symbolic_carry_robot_filter_registry.pop(key, None)
            except Exception as exc:
                failures.append(
                    {
                        "object_link": key[0],
                        "robot_link": key[1],
                        "error": str(exc),
                    }
                )
                self._log_verbose(
                    "[starter][episode][symbolic_carry] "
                    "failed_to_remove_owned_robot_collision_filter "
                    f"object_link={key[0]} robot_link={key[1]} error={exc}"
                )

        report = {
            "requested": len(owned_pairs),
            "removed": removed_pairs,
            "failed": len(failures),
            "failures": failures,
        }
        print(
            "[starter][episode][symbolic_carry] "
            f"collision_filter_cleanup requested={report['requested']} "
            f"removed={report['removed']} failed={report['failed']}"
        )
        sys.stdout.flush()
        return report

    def _clear_symbolic_grasp_state(
        self,
        arm,
        restore_payload=True,
    ):
        params = self.robot._ag_obj_constraint_params.get(arm, {})
        joint_prim_path = params.get("ag_joint_prim_path")
        if joint_prim_path:
            og.sim.stage.RemovePrim(joint_prim_path)
        if (
            self._symbolic_carry_state is not None
            and self._symbolic_carry_state.get("arm") == arm
        ):
            carry_state = self._symbolic_carry_state
            carry_obj = carry_state.get("obj")
            if carry_obj is not None and restore_payload:
                self._sync_released_symbolic_payload_to_object(
                    carry_state.get("particle_states", []),
                    carry_state.get("rigid_descendant_states", []),
                    carry_obj,
                )
            self._restore_symbolic_carried_rigid_descendant_collisions(
                carry_state.get("rigid_descendant_states", [])
            )
            self._restore_symbolic_carried_object_collisions(carry_state)
            if restore_payload:
                self._symbolic_carry_state = None
            else:
                carry_state["released_pending_payload"] = True
        self.robot._ag_obj_constraints[arm] = None
        self.robot._ag_obj_constraint_params[arm] = {}
        self.robot._ag_obj_in_hand[arm] = None
        self.robot._ag_freeze_gripper[arm] = False
        self.robot._ag_freeze_joint_pos[arm] = {}
        self.robot._ag_release_counter[arm] = None

    def _symbolic_grasp_pose_near_enough(self, target_pose) -> bool:
        distance = self._base_target_xy_distance(target_pose)
        return (
            distance is not None
            and distance <= self.symbolic_grasp_max_goal_radius + 0.15
        )

    def _set_cached_grasp_ready_navigation(
        self,
        obj,
        grasp_pose,
        preferred_goal_direction,
        navigation_reason,
    ):
        self._last_grasp_ready_navigation = {
            "obj": obj,
            "obj_name": getattr(obj, "name", None),
            "grasp_pose": grasp_pose,
            "preferred_goal_direction": preferred_goal_direction,
            "navigation_reason": navigation_reason,
            "base_pos": self.robot.get_position_orientation()[0].clone(),
        }

    def _get_cached_grasp_ready_navigation(self, obj):
        cache = self._last_grasp_ready_navigation
        if not cache:
            return None
        if cache.get("obj") is not obj and cache.get("obj_name") != getattr(
            obj,
            "name",
            None,
        ):
            return None

        grasp_pose = cache.get("grasp_pose")
        preferred_goal_direction = cache.get("preferred_goal_direction")
        if grasp_pose is None:
            self._last_grasp_ready_navigation = None
            return None

        if self._safe_target_in_reach(grasp_pose) or (
            self._symbolic_grasp_pose_near_enough(grasp_pose)
            and self._symbolic_grasp_pose_direction_aligned(
                grasp_pose,
                preferred_goal_direction,
            )
            and self._symbolic_grasp_pose_facing_target(grasp_pose)
        ):
            return grasp_pose, preferred_goal_direction

        self._last_grasp_ready_navigation = None
        return None

    def _preferred_goal_direction_from_current_base(self, target_pose):
        try:
            robot_pos = self.robot.get_position_orientation()[0]
            target_pos = torch.as_tensor(target_pose[0], dtype=torch.float32)
            direction = torch.as_tensor(robot_pos[:2], dtype=torch.float32) - target_pos[:2]
            norm = torch.norm(direction)
            if float(norm.item()) < 1e-6:
                return None
            return direction / norm
        except Exception:
            return None

    def _symbolic_grasp_pose_direction_aligned(
        self,
        target_pose,
        preferred_goal_direction,
    ) -> bool:
        if preferred_goal_direction is None:
            return True
        try:
            robot_pos = self.robot.get_position_orientation()[0]
            target_pos = torch.as_tensor(target_pose[0], dtype=torch.float32)
            actual_direction = (
                torch.as_tensor(robot_pos[:2], dtype=torch.float32) - target_pos[:2]
            )
            norm = torch.norm(actual_direction)
            if float(norm.item()) < 1e-6:
                return False
            actual_direction = actual_direction / norm
            return float(torch.dot(actual_direction, preferred_goal_direction).item()) >= 0.85
        except Exception:
            return False

    def _symbolic_grasp_pose_facing_target(self, target_pose) -> bool:
        try:
            robot_pos, robot_orn = self.robot.get_position_orientation()
            target_pos = torch.as_tensor(target_pose[0], dtype=torch.float32)
            desired_yaw = math.atan2(
                float(target_pos[1] - robot_pos[1]),
                float(target_pos[0] - robot_pos[0]),
            )
            current_yaw = float(T.quat2euler(robot_orn)[2])
            yaw_error = abs(self._angle_diff(current_yaw, desired_yaw))
            return yaw_error <= self.symbolic_grasp_max_yaw_error
        except Exception:
            return False

    @staticmethod
    def _angle_diff(current, target):
        return math.atan2(math.sin(target - current), math.cos(target - current))

    def _symbolic_release(self, restore_payload=True):
        """Release any assisted-grasp constraint immediately."""
        if self._get_obj_in_hand() is None:
            self._restore_assisted_grasp_auto_handling_after_symbolic_hold()
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot release an object if the gripper is empty.",
            )

        try:
            release_fallback_used = False
            try:
                self.robot.release_grasp_immediately()
            except Exception:
                self._clear_symbolic_grasp_state(
                    self.arm,
                    restore_payload=restore_payload,
                )
                release_fallback_used = True
            if (
                not release_fallback_used
                and self._symbolic_carry_state is not None
            ):
                self._clear_symbolic_grasp_state(
                    self.arm,
                    restore_payload=restore_payload,
                )
            elif (
                not release_fallback_used
                and self._get_obj_in_hand() is not None
            ):
                self._clear_symbolic_grasp_state(
                    self.arm,
                    restore_payload=restore_payload,
                )

            if self._get_obj_in_hand() is not None:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Symbolic release completed, but an object is still detected in hand.",
                    {"object in hand": self._get_obj_in_hand().name},
            )
        finally:
            if self._get_obj_in_hand() is None:
                self._restore_assisted_grasp_auto_handling_after_symbolic_hold()
        yield from ()

    def _move_tactqn_open_pose(
        self,
        target_pose,
        phase,
        stop_on_contact=False,
    ):
        """Follow a nearby door pose without Starter's strict 45-degree filter.

        The Fetch arm uses absolute joint control in this task. Starter's
        Cartesian helper rejects a reachable 5 cm handle approach whenever any
        wrist joint needs to rotate by more than 45 degrees. For this specific
        cabinet only, solve the target pose and let the existing direct joint
        controller follow it. The banana GRASP path never calls this method.
        """
        joint_pos = self._convert_cartesian_to_joint_space(target_pose)
        current_joint_pos = self.robot.get_joint_positions()[
            self._manipulation_control_idx
        ]
        max_joint_delta = float(
            torch.max(torch.abs(joint_pos - current_joint_pos)).item()
        )
        print(
            "[starter][open_close] "
            f"phase={phase} motion=direct_joint "
            f"max_joint_delta={max_joint_delta:.4f}"
        )
        sys.stdout.flush()
        yield from self._move_hand_direct_joint(
            joint_pos,
            stop_on_contact=stop_on_contact,
            ignore_failure=False,
        )

    def _sample_open_close_grasp_data(
        self,
        obj,
        should_open,
        relevant_joint=None,
        num_waypoints="default",
        grasp_candidate_index=0,
    ):
        """Correct OG Starter's list / macro / waypoint compatibility bugs."""
        joints = (
            [relevant_joint]
            if relevant_joint is not None
            else list(_get_relevant_joints(obj)[1])
        )
        if not joints:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.SAMPLING_ERROR,
                "Cannot open or close an object without relevant joints.",
                {"target object": obj.name},
            )

        ordering = torch.randperm(len(joints)).tolist()
        selected_joint = None
        for index in ordering:
            joint = joints[index]
            current_position = joint.get_state()[0][0]
            joint_range = joint.upper_limit - joint.lower_limit
            if abs(float(joint_range)) < 1e-6:
                continue
            openness_fraction = float(
                (current_position - joint.lower_limit) / joint_range
            )
            if (
                should_open and openness_fraction < 0.8
            ) or (
                not should_open and openness_fraction > 0.05
            ):
                selected_joint = joint
                break

        if selected_joint is None:
            return None

        if selected_joint.joint_type == JointType.JOINT_REVOLUTE:
            joint_data = self._revolute_open_close_grasp_data(
                obj,
                selected_joint,
                should_open,
                num_waypoints=num_waypoints,
                grasp_candidate_index=grasp_candidate_index,
            )
        elif selected_joint.joint_type == JointType.JOINT_PRISMATIC:
            joint_data = self._prismatic_open_close_grasp_data(
                obj,
                selected_joint,
                should_open,
                num_waypoints=num_waypoints,
            )
        else:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.SAMPLING_ERROR,
                "Unsupported openable joint type.",
                {
                    "target object": obj.name,
                    "joint": selected_joint.joint_name,
                    "joint type": str(selected_joint.joint_type),
                },
            )

        return (selected_joint,) + joint_data

    def _revolute_open_close_grasp_data(
        self,
        obj,
        joint,
        should_open,
        num_waypoints,
        grasp_candidate_index=0,
    ):
        """Sample a door arc using live link bounds and USD joint frames."""
        child_link_name = joint.body1.split("/")[-1]
        parent_link_name = joint.body0.split("/")[-1]
        parent_link = obj.links[parent_link_name]
        (
            bbox_center,
            bbox_orientation,
            bbox_extent,
            _,
        ) = obj.get_base_aligned_bbox(link_name=child_link_name, visual=False)

        parent_position, parent_orientation = parent_link.get_position_orientation()
        local_hinge_position = torch.as_tensor(
            list(joint.get_attribute("physics:localPos0")),
            dtype=torch.float32,
        )
        local_hinge_orientation = torch.as_tensor(
            lazy.omni.isaac.core.utils.rotations.gf_quat_to_np_array(
                joint.get_attribute("physics:localRot0")
            )[[1, 2, 3, 0]],
            dtype=torch.float32,
        )
        hinge_position, hinge_orientation = T.pose_transform(
            parent_position,
            parent_orientation,
            local_hinge_position,
            local_hinge_orientation,
        )

        axis_name = str(joint.axis).upper()
        axis_index = {"X": 0, "Y": 1, "Z": 2}.get(axis_name[-1])
        if axis_index is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.SAMPLING_ERROR,
                "Could not determine revolute joint axis.",
                {"joint": joint.joint_name, "axis": axis_name},
            )
        joint_axis_local = torch.eye(3)[axis_index]
        joint_axis_world = T.quat_apply(hinge_orientation, joint_axis_local)
        joint_axis_world = joint_axis_world / torch.norm(joint_axis_world)

        bbox_axes_world = [
            T.quat_apply(bbox_orientation, torch.eye(3)[index])
            for index in range(3)
        ]
        perpendicular_axes = [
            index
            for index, axis in enumerate(bbox_axes_world)
            if abs(float(torch.dot(axis, joint_axis_world).item())) < 0.8
        ]
        if not perpendicular_axes:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.SAMPLING_ERROR,
                "Could not identify the door face perpendicular to the hinge.",
                {"joint": joint.joint_name},
            )

        # A door's thinnest non-hinge dimension is its front / back normal.
        normal_axis_index = min(
            perpendicular_axes,
            key=lambda index: float(bbox_extent[index]),
        )
        normal_axis_world = bbox_axes_world[normal_axis_index]
        robot_position = self.robot.get_position_orientation()[0]
        normal_sign = (
            1.0
            if float(torch.dot(robot_position - bbox_center, normal_axis_world)) >= 0.0
            else -1.0
        )
        front_normal_world = normal_axis_world * normal_sign
        surface_position = (
            bbox_center
            + front_normal_world * (bbox_extent[normal_axis_index] / 2.0)
        )

        handle_sampling_mode = "opposite_hinge"
        if (
            getattr(obj, "category", "") == "top_cabinet"
            and getattr(obj, "model", "") == "tactqn"
        ):
            # tactqn has one long horizontal handle close to the lower edge of
            # its door.  The USD joint frame makes the generic opposite-hinge
            # heuristic select the left middle edge instead, which aims the
            # gripper through the neighbouring sink cabinet.  Use the visible
            # handle layout for this known task asset: centered horizontally,
            # low on the front face, and slightly proud of the wooden panel.
            vertical_axis_index = max(
                range(3),
                key=lambda index: abs(
                    float(
                        torch.dot(
                            bbox_axes_world[index],
                            torch.tensor([0.0, 0.0, 1.0]),
                        ).item()
                    )
                ),
            )
            vertical_axis_world = bbox_axes_world[vertical_axis_index]
            downward_sign = (
                -1.0 if float(vertical_axis_world[2].item()) >= 0.0 else 1.0
            )
            surface_position = surface_position + vertical_axis_world * (
                downward_sign * bbox_extent[vertical_axis_index] * 0.38
            )
            surface_position = surface_position + front_normal_world * 0.025

            # Make retries physically meaningful by sampling three positions
            # along the long horizontal handle. The first uses the center, the
            # second moves away from the hinge for more leverage, and the third
            # moves toward the hinge in case the arm cannot reach the far side.
            horizontal_axis_index = next(
                index
                for index in range(3)
                if index not in {normal_axis_index, vertical_axis_index}
            )
            horizontal_axis_world = bbox_axes_world[horizontal_axis_index]
            away_from_hinge = bbox_center - hinge_position
            away_from_hinge = away_from_hinge - torch.dot(
                away_from_hinge, front_normal_world
            ) * front_normal_world
            away_from_hinge = away_from_hinge - torch.dot(
                away_from_hinge, vertical_axis_world
            ) * vertical_axis_world
            if float(torch.norm(away_from_hinge).item()) < 1e-4:
                away_from_hinge = horizontal_axis_world
            else:
                away_from_hinge = away_from_hinge / torch.norm(away_from_hinge)

            candidate_fractions = (-0.18, -0.10, 0.0)
            candidate_index = int(grasp_candidate_index) % len(candidate_fractions)
            candidate_fraction = candidate_fractions[candidate_index]
            handle_offset = (
                away_from_hinge
                * bbox_extent[horizontal_axis_index]
                * candidate_fraction
            )
            surface_position = surface_position + handle_offset
            handle_sampling_mode = (
                f"tactqn_lower_handle_candidate_{candidate_index}"
            )
        else:
            # Move the contact away from the hinge to increase opening torque.
            hinge_to_center = bbox_center - hinge_position
            hinge_to_center = hinge_to_center - torch.dot(
                hinge_to_center, joint_axis_world
            ) * joint_axis_world
            hinge_to_center = hinge_to_center - torch.dot(
                hinge_to_center, front_normal_world
            ) * front_normal_world
            if float(torch.norm(hinge_to_center).item()) > 1e-4:
                handle_direction = hinge_to_center / torch.norm(hinge_to_center)
                handle_axis_index = max(
                    range(3),
                    key=lambda index: abs(
                        float(torch.dot(bbox_axes_world[index], handle_direction))
                    ),
                )
                surface_position = surface_position + handle_direction * (
                    bbox_extent[handle_axis_index] * 0.35
                )

        print(
            "[starter][open_close][geometry] "
            f"target={obj.name} model={getattr(obj, 'model', None)} "
            f"mode={handle_sampling_mode} "
            f"candidate_index={int(grasp_candidate_index)} "
            f"bbox_center={self._to_float_list(bbox_center)} "
            f"bbox_extent={self._to_float_list(bbox_extent)} "
            f"hinge_position={self._to_float_list(hinge_position)} "
            f"joint_axis={self._to_float_list(joint_axis_world)} "
            f"surface_position={self._to_float_list(surface_position)}"
        )
        sys.stdout.flush()

        approach_direction = -front_normal_world
        hand_orientation = _orientation_facing_vector(approach_direction)
        finger_length = float(self.robot.finger_lengths[self.arm])
        contact_hand_position = surface_position - approach_direction * finger_length
        pregrasp_position = contact_hand_position - approach_direction * 0.05

        current_joint_position = joint.get_state()[0][0]
        target_joint_position = (
            joint.upper_limit if should_open else joint.lower_limit
        )
        required_angle_change = target_joint_position - current_joint_position
        num_waypoints = max(2, int(num_waypoints))
        fractions = torch.linspace(0.0, 1.0, num_waypoints)
        relative_contact_position = contact_hand_position - hinge_position
        target_poses = []
        for fraction in fractions:
            rotation = T.axisangle2quat(
                joint_axis_world * (required_angle_change * fraction)
            )
            rotated_position = hinge_position + T.quat_apply(
                rotation,
                relative_contact_position,
            )
            rotated_orientation = T.pose_transform(
                torch.zeros(3),
                rotation,
                torch.zeros(3),
                hand_orientation,
            )[1]
            target_poses.append((rotated_position, rotated_orientation))

        return (
            (pregrasp_position, hand_orientation),
            target_poses,
            approach_direction,
            should_open,
            required_angle_change,
        )

    def _prismatic_open_close_grasp_data(
        self,
        obj,
        joint,
        should_open,
        num_waypoints,
    ):
        """Sample a physical pull / push trajectory for a drawer joint."""
        link_name = joint.body1.split("/")[-1]
        (
            bbox_center,
            bbox_orientation,
            bbox_extent,
            _,
        ) = obj.get_base_aligned_bbox(link_name=link_name, visual=False)

        joint_orientation = torch.as_tensor(
            lazy.omni.isaac.core.utils.rotations.gf_quat_to_np_array(
                joint.get_attribute("physics:localRot0")
            )[[1, 2, 3, 0]],
            dtype=torch.float32,
        )
        push_axis = T.quat_apply(
            joint_orientation,
            torch.tensor([1.0, 0.0, 0.0]),
        )
        push_axis_index = int(torch.argmax(torch.abs(push_axis)).item())
        canonical_axis = torch.eye(3)[push_axis_index]
        axis_sign = float(torch.sign(push_axis[push_axis_index]).item())
        if axis_sign == 0.0:
            axis_sign = 1.0

        surface_points = torch.stack(
            (canonical_axis, -canonical_axis),
            dim=0,
        ) * (bbox_extent[push_axis_index] / 2.0)
        robot_position = self.robot.get_position_orientation()[0]
        surface_world_positions = [
            T.pose_transform(
                bbox_center,
                bbox_orientation,
                point,
                torch.tensor([0.0, 0.0, 0.0, 1.0]),
            )[0]
            for point in surface_points
        ]
        surface_index = min(
            range(len(surface_world_positions)),
            key=lambda index: float(
                torch.norm(surface_world_positions[index] - robot_position).item()
            ),
        )
        surface_sign = 1.0 if surface_index == 0 else -1.0
        surface_position = surface_world_positions[surface_index]

        approach_direction = T.quat_apply(
            bbox_orientation,
            canonical_axis * -surface_sign,
        )
        approach_direction = approach_direction / torch.norm(approach_direction)
        hand_orientation = _orientation_facing_vector(approach_direction)
        finger_length = float(self.robot.finger_lengths[self.arm])
        pregrasp_position = surface_position - approach_direction * (
            finger_length + 0.05
        )

        current_joint_position = joint.get_state()[0][0]
        target_joint_position = (
            joint.upper_limit if should_open else joint.lower_limit
        )
        required_position_change = target_joint_position - current_joint_position
        local_motion_direction = canonical_axis * (
            axis_sign if should_open else -axis_sign
        )
        world_motion = T.quat_apply(
            bbox_orientation,
            local_motion_direction * abs(float(required_position_change)),
        )

        contact_hand_position = surface_position - approach_direction * finger_length
        target_hand_position = contact_hand_position + world_motion
        target_poses = _interpolate_open_close_waypoints(
            (contact_hand_position, hand_orientation),
            (target_hand_position, hand_orientation),
            num_waypoints=num_waypoints,
        )
        grasp_required = bool(torch.dot(world_motion, approach_direction) < 0)
        return (
            (pregrasp_position, hand_orientation),
            target_poses,
            approach_direction,
            grasp_required,
            required_position_change,
        )

    def _ik_solver_cartesian_to_joint_space(self, relative_target_pose):
        """Reuse Lula IK instead of rebuilding it for every navigation candidate."""
        if self._cached_ik_solver is None:
            self._cached_ik_solver = IKSolver(
                robot_description_path=self._manipulation_descriptor_path,
                robot_urdf_path=self.robot.urdf_path,
                reset_joint_pos=self.robot.reset_joint_pos[
                    self._manipulation_control_idx
                ],
                eef_name=self.robot.eef_link_names[self.arm],
            )
        return self._cached_ik_solver.solve(
            target_pos=relative_target_pose[0],
            target_quat=relative_target_pose[1],
            max_iterations=50,
        )

    def _place_inside(self, obj):
        if self.symbolic_place:
            obj_in_hand = self._get_obj_in_hand()
            if self._should_use_symbolic_cloth_inside_drop(obj_in_hand, obj):
                yield from self._symbolic_drop_cloth_inside(obj_in_hand, obj)
                return
            yield from self._symbolic_place_with_predicate(obj, object_states.Inside)
            return

        obj_in_hand = self._get_obj_in_hand()
        if self._should_use_drop_inside_fallback(obj_in_hand, obj):
            yield from self._drop_inside_open_container(obj_in_hand, obj)
            return

        yield from super()._place_inside(obj)

    def _place_on_top(self, obj):
        if self.symbolic_place:
            yield from self._symbolic_place_with_predicate(obj, object_states.OnTop)
            return

        yield from super()._place_on_top(obj)

    def _should_use_symbolic_cloth_inside_drop(self, obj_in_hand, container) -> bool:
        return (
            self.symbolic_cloth_inside_drop
            and obj_in_hand is not None
            and getattr(obj_in_hand, "prim_type", None) == PrimType.CLOTH
            and getattr(container, "category", "")
            in self.symbolic_cloth_inside_drop_container_categories
        )

    def _should_use_symbolic_dump_cloth_drop(self, state, container) -> bool:
        obj = state["obj"]
        return (
            self.symbolic_cloth_inside_drop
            and state.get("cloth_shape") is not None
            and getattr(obj, "prim_type", None) == PrimType.CLOTH
            and getattr(container, "category", "")
            in self.symbolic_cloth_inside_drop_container_categories
        )

    def _prepare_symbolic_dump_cloth_drop(
        self,
        state,
        container,
        source_pos,
        source_orn,
    ):
        obj = state["obj"]
        cloth_shape = state["cloth_shape"]
        target_center, _, target_extent, _ = container.get_base_aligned_bbox()
        if self.symbolic_cloth_inside_fit_shape:
            cloth_shape = self._fit_symbolic_cloth_shape_to_extent(
                cloth_shape,
                source_orn,
                torch.as_tensor(target_extent, dtype=torch.float32)
                * self.symbolic_cloth_inside_fit_container_scale,
            )

        obj.set_position_orientation(position=source_pos, orientation=source_orn)
        obj.keep_still()
        self._restore_symbolic_carried_cloth_shape(
            obj,
            source_pos,
            source_orn,
            cloth_shape,
        )
        _, cloth_orientation, cloth_extent, cloth_center_in_base = (
            obj.get_base_aligned_bbox()
        )
        cloth_position, cloth_orientation = compute_cloth_drop_pose(
            target_center,
            target_extent,
            cloth_orientation,
            cloth_extent,
            cloth_center_in_base,
            self.symbolic_cloth_inside_drop_height,
        )
        obj.set_position_orientation(
            position=cloth_position,
            orientation=cloth_orientation,
        )
        obj.keep_still()
        self._restore_symbolic_carried_cloth_shape(
            obj,
            cloth_position,
            cloth_orientation,
            cloth_shape,
        )
        print(
            "[starter][dump][cloth_drop] "
            f"object={obj.name} target={container.name} "
            f"position={self._to_float_list(cloth_position)} "
            f"target_center={self._to_float_list(target_center)} "
            f"target_extent={self._to_float_list(target_extent)} "
            f"cloth_extent={self._to_float_list(cloth_extent)}"
        )
        sys.stdout.flush()
        return cloth_shape

    def _symbolic_drop_cloth_inside(self, obj_in_hand, container):
        """Drop a cloth above a container and require native Inside after settling."""
        particle_states = self._symbolic_carried_particle_states(obj_in_hand)
        rigid_descendant_states = self._symbolic_carried_rigid_descendant_states(
            obj_in_hand
        )
        pending_coverage = self._pending_symbolic_carried_coverage(obj_in_hand)
        carry_state = self._symbolic_carry_state
        cloth_shape = (
            carry_state.get("cloth_shape")
            if carry_state is not None and carry_state.get("obj") is obj_in_hand
            else None
        )

        if self.symbolic_cloth_inside_pre_settle_steps:
            print(
                "[starter][place_inside][cloth_drop] pre_settle "
                f"object={obj_in_hand.name} container={container.name} "
                f"steps={self.symbolic_cloth_inside_pre_settle_steps}"
            )
            sys.stdout.flush()
            for _ in range(self.symbolic_cloth_inside_pre_settle_steps):
                yield from self._yield_symbolic_refresh_step()
            self._sync_symbolic_carried_object_to_eef()

        target_center, _, target_extent, _ = container.get_base_aligned_bbox()
        if (
            self.symbolic_cloth_inside_fit_shape
            and cloth_shape is not None
        ):
            cloth_shape = self._fit_symbolic_cloth_shape_to_extent(
                cloth_shape,
                obj_in_hand.get_position_orientation()[1],
                torch.as_tensor(target_extent, dtype=torch.float32)
                * self.symbolic_cloth_inside_fit_container_scale,
            )
            if carry_state is not None and carry_state.get("obj") is obj_in_hand:
                carry_state["cloth_shape"] = cloth_shape
            current_pos, current_orn = obj_in_hand.get_position_orientation()
            self._restore_symbolic_carried_cloth_shape(
                obj_in_hand,
                current_pos,
                current_orn,
                cloth_shape,
            )
        _, cloth_orientation, cloth_extent, cloth_center_in_base = (
            obj_in_hand.get_base_aligned_bbox()
        )
        cloth_position, cloth_orientation = compute_cloth_drop_pose(
            target_center,
            target_extent,
            cloth_orientation,
            cloth_extent,
            cloth_center_in_base,
            self.symbolic_cloth_inside_drop_height,
        )
        print(
            "[starter][place_inside][cloth_drop] "
            f"object={obj_in_hand.name} container={container.name} "
            f"position={self._to_float_list(cloth_position)} "
            f"container_center={self._to_float_list(target_center)} "
            f"container_extent={self._to_float_list(target_extent)} "
            f"cloth_extent={self._to_float_list(cloth_extent)} "
            f"settle_steps={self.symbolic_cloth_inside_settle_steps}"
        )
        sys.stdout.flush()

        yield from self._symbolic_release(restore_payload=False)
        try:
            obj_in_hand.set_position_orientation(
                position=cloth_position,
                orientation=cloth_orientation,
            )
        except Exception:
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            raise
        obj_in_hand.keep_still()
        self._restore_symbolic_carried_cloth_shape(
            obj_in_hand,
            cloth_position,
            cloth_orientation,
            cloth_shape,
        )
        self._sync_released_symbolic_payload_to_object(
            particle_states,
            rigid_descendant_states,
            obj_in_hand,
        )
        for _ in range(self.symbolic_cloth_inside_settle_steps):
            yield from self._yield_symbolic_refresh_step()
        yield from self._ensure_symbolic_rigid_descendant_postconditions(
            rigid_descendant_states
        )

        final_cloth_center, _, final_cloth_extent, _ = (
            obj_in_hand.get_base_aligned_bbox()
        )
        final_container_center, _, final_container_extent, _ = (
            container.get_base_aligned_bbox()
        )
        inside_satisfied = obj_in_hand.states[object_states.Inside].get_value(
            container
        )
        if not inside_satisfied:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Cloth settled above the container without satisfying Inside.",
                {
                    "dropped object": obj_in_hand.name,
                    "target object": container.name,
                    "drop position": self._to_float_list(cloth_position),
                    "final cloth center": self._to_float_list(final_cloth_center),
                    "final cloth extent": self._to_float_list(final_cloth_extent),
                    "final container center": self._to_float_list(
                        final_container_center
                    ),
                    "final container extent": self._to_float_list(
                        final_container_extent
                    ),
                    "pre-settle steps": self.symbolic_cloth_inside_pre_settle_steps,
                    "settle steps": self.symbolic_cloth_inside_settle_steps,
                },
            )

        print(
            "[starter][place_inside][cloth_drop] succeeded "
            f"object={obj_in_hand.name} container={container.name} "
            f"cloth_center={self._to_float_list(final_cloth_center)} "
            f"container_center={self._to_float_list(final_container_center)}"
        )
        sys.stdout.flush()
        self._register_released_loaded_cloth_stabilization(
            obj_in_hand,
            rigid_descendant_states,
        )
        yield from self._finalize_pending_symbolic_coverage(
            obj_in_hand,
            pending_coverage,
        )

    def _execute_release(self):
        if self.symbolic_release:
            obj_in_hand = self._get_obj_in_hand()
            pending_coverage = self._pending_symbolic_carried_coverage(obj_in_hand)
            rigid_descendant_states = (
                self._symbolic_carried_rigid_descendant_states(obj_in_hand)
            )
            particle_states = self._symbolic_carried_particle_states(obj_in_hand)
            yield from self._symbolic_release(restore_payload=False)
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            if rigid_descendant_states:
                yield from self._yield_symbolic_refresh_step()
                yield from self._ensure_symbolic_rigid_descendant_postconditions(
                    rigid_descendant_states
                )
                self._register_released_loaded_cloth_stabilization(
                    obj_in_hand,
                    rigid_descendant_states,
                )
            yield from self._finalize_pending_symbolic_coverage(
                obj_in_hand,
                pending_coverage,
            )
            return

        yield from super()._execute_release()

    def _symbolic_place_with_predicate(self, obj, predicate):
        """Place the currently held object by sampling a valid predicate pose."""
        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "You need to be grasping an object first to place it somewhere.",
            )
        particle_states = self._symbolic_carried_particle_states(obj_in_hand)
        rigid_descendant_states = (
            self._symbolic_carried_rigid_descendant_states(obj_in_hand)
        )
        pending_coverage = self._pending_symbolic_carried_coverage(obj_in_hand)

        print(
            "[starter][place][symbolic_shortcut] "
            f"object={obj_in_hand.name} target={obj.name} "
            f"predicate={predicate.__name__}"
        )
        sys.stdout.flush()

        slot_pose = self._task_placement_slot_pose(obj_in_hand, obj, predicate)
        if slot_pose is not None:
            yield from self._symbolic_place_at_configured_slot(
                obj_in_hand,
                obj,
                predicate,
                slot_pose,
            )
            return

        def try_state_setter():
            try:
                try:
                    return obj_in_hand.states[predicate].set_value(
                        obj,
                        True,
                        reset_before_sampling=False,
                    )
                except TypeError:
                    return obj_in_hand.states[predicate].set_value(obj, True)
            except Exception as exc:
                print(
                    "[starter][place][symbolic_fallback] "
                    f"state setter failed object={obj_in_hand.name} "
                    f"target={obj.name} predicate={predicate.__name__} "
                    f"error={type(exc).__name__}: {exc}"
                )
                sys.stdout.flush()
                return False

        try:
            obj_pose = self._sample_pose_with_object_and_predicate(
                predicate,
                obj_in_hand,
                obj,
            )
        except ActionPrimitiveError as exc:
            if exc.reason != ActionPrimitiveError.Reason.SAMPLING_ERROR:
                raise

            yield from self._symbolic_release(restore_payload=False)
            if try_state_setter():
                obj_in_hand.keep_still()
                self._sync_released_symbolic_payload_to_object(
                    particle_states,
                    rigid_descendant_states,
                    obj_in_hand,
                )
                yield from self._yield_symbolic_refresh_step()
                yield from self._ensure_symbolic_rigid_descendant_postconditions(
                    rigid_descendant_states
                )
                if obj_in_hand.states[predicate].get_value(obj):
                    print(
                        "[starter][place][symbolic_fallback] "
                        "state setter succeeded after sampling failure "
                        f"object={obj_in_hand.name} target={obj.name} "
                        f"predicate={predicate.__name__}"
                    )
                    sys.stdout.flush()
                    yield from self._finalize_pending_symbolic_coverage(
                        obj_in_hand,
                        pending_coverage,
                    )
                    return

            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            raise

        yield from self._symbolic_release(restore_payload=False)
        try:
            obj_in_hand.set_position_orientation(*obj_pose)
        except Exception:
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            raise
        obj_in_hand.keep_still()
        self._sync_released_symbolic_payload_to_object(
            particle_states,
            rigid_descendant_states,
            obj_in_hand,
        )
        yield from self._yield_symbolic_refresh_step()
        yield from self._ensure_symbolic_rigid_descendant_postconditions(
            rigid_descendant_states
        )

        if not obj_in_hand.states[predicate].get_value(obj):
            if try_state_setter():
                obj_in_hand.keep_still()
                self._sync_released_symbolic_payload_to_object(
                    particle_states,
                    rigid_descendant_states,
                    obj_in_hand,
                )
                yield from self._yield_symbolic_refresh_step()
                yield from self._ensure_symbolic_rigid_descendant_postconditions(
                    rigid_descendant_states
                )
                if obj_in_hand.states[predicate].get_value(obj):
                    self._register_released_loaded_cloth_stabilization(
                        obj_in_hand,
                        rigid_descendant_states,
                    )
                    print(
                        "[starter][place][symbolic_fallback] "
                        f"state setter succeeded object={obj_in_hand.name} "
                        f"target={obj.name} predicate={predicate.__name__}"
                    )
                    sys.stdout.flush()
                    yield from self._finalize_pending_symbolic_coverage(
                        obj_in_hand,
                        pending_coverage,
                    )
                    return

            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Symbolic placement sampled a pose, but the desired relation is not satisfied.",
                {
                    "dropped object": obj_in_hand.name,
                    "target object": obj.name,
                    "predicate": predicate.__name__,
                },
            )

        self._register_released_loaded_cloth_stabilization(
            obj_in_hand,
            rigid_descendant_states,
        )
        yield from self._finalize_pending_symbolic_coverage(
            obj_in_hand,
            pending_coverage,
        )

    def _task_placement_slot_pose(self, obj_in_hand, target_obj, predicate):
        if predicate is not object_states.OnTop:
            return None
        object_variants = self._name_variants_for_object(obj_in_hand)
        target_variants = self._name_variants_for_object(target_obj)
        for object_key in object_variants:
            for target_key in target_variants:
                slot_pose = self.task_placement_slots.get((object_key, target_key))
                if slot_pose is not None:
                    return slot_pose
                slot_pose = self.task_placement_slots.get(
                    (object_key.lower(), target_key.lower())
                )
                if slot_pose is not None:
                    return slot_pose
        return None

    def _symbolic_place_at_configured_slot(
        self,
        obj_in_hand,
        target_obj,
        predicate,
        slot_pose,
    ):
        particle_states = self._symbolic_carried_particle_states(obj_in_hand)
        rigid_descendant_states = (
            self._symbolic_carried_rigid_descendant_states(obj_in_hand)
        )
        pending_coverage = self._pending_symbolic_carried_coverage(obj_in_hand)
        position, orientation = self._build_task_placement_pose(obj_in_hand, slot_pose)
        print(
            "[starter][place][slot] applying "
            f"object={obj_in_hand.name} target={target_obj.name} "
            f"predicate={predicate.__name__} "
            f"position={self._to_float_list(position)}"
        )
        sys.stdout.flush()

        yield from self._symbolic_release(restore_payload=False)
        try:
            obj_in_hand.set_position_orientation(
                position=position,
                orientation=orientation,
            )
        except Exception:
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            raise
        obj_in_hand.keep_still()
        self._sync_released_symbolic_payload_to_object(
            particle_states,
            rigid_descendant_states,
            obj_in_hand,
        )
        yield from self._yield_symbolic_refresh_step()
        yield from self._ensure_symbolic_rigid_descendant_postconditions(
            rigid_descendant_states
        )

        if obj_in_hand.states[predicate].get_value(target_obj):
            self._register_released_loaded_cloth_stabilization(
                obj_in_hand,
                rigid_descendant_states,
            )
            yield from self._finalize_pending_symbolic_coverage(
                obj_in_hand,
                pending_coverage,
            )
            return

        try:
            obj_in_hand.states[predicate].set_value(
                target_obj,
                True,
                reset_before_sampling=False,
            )
        except TypeError:
            obj_in_hand.states[predicate].set_value(target_obj, True)
        except Exception as exc:
            print(
                "[starter][place][slot][warning] "
                f"state setter failed object={obj_in_hand.name} "
                f"target={target_obj.name} predicate={predicate.__name__} "
                f"error={type(exc).__name__}: {exc}"
            )
            sys.stdout.flush()

        try:
            obj_in_hand.set_position_orientation(
                position=position,
                orientation=orientation,
            )
        except Exception:
            self._sync_released_symbolic_payload_to_object(
                particle_states,
                rigid_descendant_states,
                obj_in_hand,
            )
            raise
        obj_in_hand.keep_still()
        self._sync_released_symbolic_payload_to_object(
            particle_states,
            rigid_descendant_states,
            obj_in_hand,
        )
        yield from self._yield_symbolic_refresh_step()
        yield from self._ensure_symbolic_rigid_descendant_postconditions(
            rigid_descendant_states
        )
        if obj_in_hand.states[predicate].get_value(target_obj):
            self._register_released_loaded_cloth_stabilization(
                obj_in_hand,
                rigid_descendant_states,
            )
            yield from self._finalize_pending_symbolic_coverage(
                obj_in_hand,
                pending_coverage,
            )
            return

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Configured placement slot did not satisfy the desired relation.",
            {
                "dropped object": obj_in_hand.name,
                "target object": target_obj.name,
                "predicate": predicate.__name__,
                "slot pose": dict(slot_pose),
            },
        )

    def _build_task_placement_pose(self, obj, slot_pose):
        raw_position = slot_pose.get("position") or slot_pose.get("pos")
        if raw_position is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Configured placement slot has no position.",
                {"object": obj.name, "slot pose": dict(slot_pose)},
            )
        position = torch.tensor(list(raw_position)[:3], dtype=torch.float32)
        raw_orientation = slot_pose.get("orientation")
        if raw_orientation is None:
            orientation = obj.get_position_orientation()[1]
        else:
            orientation = torch.tensor(raw_orientation, dtype=torch.float32)
        return position, orientation

    def _should_use_drop_inside_fallback(self, obj_in_hand, container) -> bool:
        if obj_in_hand is None or container is None:
            return False

        return (
            getattr(obj_in_hand, "category", "") == "half_banana"
            and getattr(obj_in_hand, "model", "") == "xytkre"
            and getattr(container, "category", "") in {"trash_can", "ashcan"}
        )

    def _drop_inside_open_container(self, obj_in_hand, container):
        print(
            "[starter][place_inside] using drop-in fallback "
            f"object={obj_in_hand.name} container={container.name}"
        )
        self._log_container_pose(container)
        sys.stdout.flush()
        self._tracking_object = container
        container_pos_before_navigation = container.get_position_orientation()[0].clone()

        try:
            yield from self._navigate_to_obj(
                container,
                navigation_reason="place_inside_container_precheck",
                require_target_reachable=False,
            )
        except ActionPrimitiveError as exc:
            error = self._short_error(exc)
            print(
                "[starter][place_inside] failed to navigate near container "
                f"before drop error={error}"
            )
            sys.stdout.flush()
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Could not navigate near the open container before dropping.",
                {
                    "object": obj_in_hand.name,
                    "container": container.name,
                    "navigation error": error,
                },
            )

        container_pos_after_navigation = container.get_position_orientation()[0]
        container_navigation_displacement = float(
            torch.norm(
                container_pos_after_navigation - container_pos_before_navigation
            ).item()
        )
        print(
            "[starter][place_inside] container motion during navigation "
            f"container={container.name} "
            f"displacement={container_navigation_displacement:.4f} "
            f"before={self._to_float_list(container_pos_before_navigation)} "
            f"after={self._to_float_list(container_pos_after_navigation)} "
            f"pushed={container_navigation_displacement > 0.03}"
        )
        sys.stdout.flush()

        candidate_errors = []
        for candidate_idx, hand_pose in enumerate(self._drop_poses_for_container(container)):
            if self._get_obj_in_hand() is None:
                break

            self._log_verbose(
                "[starter][place_inside] drop candidate "
                f"index={candidate_idx} "
                f"hand_pos={self._to_float_list(hand_pose[0])}"
            )

            if not self._safe_target_in_reach(hand_pose):
                try:
                    yield from self._navigate_to_obj(
                        container,
                        pose_on_obj=hand_pose,
                        navigation_reason="place_inside_drop",
                        require_target_reachable=True,
                    )
                except ActionPrimitiveError as exc:
                    error = self._short_error(exc)
                    print(
                        "[starter][place_inside] navigation to drop pose failed; "
                        "aborting drop candidates "
                        f"index={candidate_idx} error={error}"
                    )
                    sys.stdout.flush()
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.PLANNING_ERROR,
                        "Could not navigate to a stable drop pose for the open container.",
                        {
                            "object": obj_in_hand.name,
                            "container": container.name,
                            "navigation error": error,
                        },
                    )

            try:
                yield from self._move_hand(hand_pose)
                yield from self._execute_release()
                yield from self._settle_robot()

                if obj_in_hand.states[object_states.Inside].get_value(container):
                    print(
                        "[starter][place_inside] drop-in fallback succeeded "
                        f"object={obj_in_hand.name} container={container.name}"
                    )
                    sys.stdout.flush()
                    yield from self._move_hand_upward()
                    return

                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.EXECUTION_ERROR,
                    "Released object above the container, but the Inside relation is not satisfied.",
                    {
                        "object": obj_in_hand.name,
                        "container": container.name,
                    },
                )
            except ActionPrimitiveError as exc:
                error = self._short_error(exc)
                candidate_errors.append(error)
                print(
                    "[starter][place_inside] drop candidate failed "
                    f"index={candidate_idx} error={error}"
                )
                sys.stdout.flush()
                if self._get_obj_in_hand() is None:
                    raise

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PLANNING_ERROR,
            "Could not place the held object inside the open container using drop-in fallback.",
            {
                "object": obj_in_hand.name,
                "container": container.name,
                "candidate errors": candidate_errors,
            },
        )

    def _drop_poses_for_container(self, container):
        bbox_center, _, bbox_extent, _ = container.get_base_aligned_bbox()
        _, eef_orientation = self.robot.eef_links[self.arm].get_position_orientation()

        top_z = float(bbox_center[2] + bbox_extent[2] / 2.0)
        xy_offsets = [
            (0.0, 0.0),
            (0.04, 0.0),
            (-0.04, 0.0),
            (0.0, 0.04),
            (0.0, -0.04),
        ]
        z_margins = [0.34, 0.26, 0.42]
        for z_margin in z_margins:
            for x_offset, y_offset in xy_offsets:
                position = torch.as_tensor(bbox_center, dtype=torch.float32).clone()
                position[0] += x_offset
                position[1] += y_offset
                position[2] = top_z + z_margin
                yield position, eef_orientation

    def _log_container_pose(self, container):
        try:
            container_pos, container_ori = container.get_position_orientation()
            bbox_center, _, bbox_extent, _ = container.get_base_aligned_bbox()
        except Exception as exc:
            self._log_verbose(
                "[starter][place_inside] failed to inspect container pose "
                f"container={container.name} error={exc}"
            )
            return

        self._log_verbose(
            "[starter][place_inside] container pose "
            f"container={container.name} "
            f"pos={self._to_float_list(container_pos)} "
            f"ori={self._to_float_list(container_ori)} "
            f"bbox_center={self._to_float_list(bbox_center)} "
            f"bbox_extent={self._to_float_list(bbox_extent)}"
        )

    def _repair_sticky_grasp_if_contacted(self, obj, error):
        if not self._should_repair_sticky_grasp(obj):
            return False
        if self._get_obj_in_hand() is not None:
            return False

        eef_pos = self.robot.eef_links[self.arm].get_position_orientation()[0]
        obj_center = obj.get_base_aligned_bbox(visual=False)[0]
        eef_obj_distance = float(torch.norm(eef_pos - obj_center).item())
        touching_object = self._gripper_touching_object(obj)
        print(
            "[starter][grasp][repair] checking sticky repair "
            f"target={obj.name} "
            f"eef_obj_distance={eef_obj_distance:.4f} "
            f"touching_object={touching_object} "
            f"original_error={error}"
        )
        sys.stdout.flush()

        if not touching_object and eef_obj_distance > 0.18:
            return False

        contact_pos = (eef_pos + obj_center) / 2.0
        try:
            self.robot._establish_grasp(
                arm=self.arm,
                ag_data=(obj, obj.root_link),
                contact_pos=contact_pos,
            )
        except Exception as repair_exc:
            print(
                "[starter][grasp][repair] failed to establish sticky constraint "
                f"target={obj.name} error={repair_exc}"
            )
            sys.stdout.flush()
            return False

        for _ in range(5):
            empty_action = self._empty_action()
            yield self._postprocess_action(empty_action)

        repaired = self._get_obj_in_hand() == obj
        print(
            "[starter][grasp][repair] "
            f"{'succeeded' if repaired else 'failed'} "
            f"target={obj.name} object_in_hand="
            f"{None if self._get_obj_in_hand() is None else self._get_obj_in_hand().name}"
        )
        sys.stdout.flush()
        return repaired

    def _should_repair_sticky_grasp(self, obj) -> bool:
        return (
            getattr(obj, "category", "") == "half_banana"
            and getattr(obj, "model", "") == "xytkre"
        )

    def _should_lift_after_grasp(self, obj) -> bool:
        return obj is not None

    def _lift_held_object_for_navigation(self):
        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand is None:
            return

        yield from self._move_hand_upward()

        if self._get_obj_in_hand() != obj_in_hand:
            actual = self._get_obj_in_hand()
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "The object was lost during the controlled carry lift.",
                {
                    "expected object": obj_in_hand.name,
                    "actual object": None if actual is None else actual.name,
                },
            )

    def _move_hand_upward(self, steps=5, speed=0.1):
        """Move the EEF a short Cartesian distance without opening the gripper.

        Starter's implementation writes ``+1`` to the gripper on every upward
        step.  For Fetch that is a release command, which detached the banana.
        A Cartesian target also avoids treating one arbitrary joint axis as
        world-space upward when JointControllers are active.
        """
        del steps, speed
        start_pos, start_orn = self.robot.eef_links[self.arm].get_position_orientation()
        target_pos = start_pos.clone()
        target_pos[2] += 0.06
        yield from self._move_hand_linearly_cartesian(
            (target_pos, start_orn),
            ignore_failure=True,
            stop_if_stuck=False,
        )

    def _gripper_touching_object(self, obj) -> bool:
        try:
            contact_paths, _ = self.robot._find_gripper_contacts(arm=self.arm)
        except Exception:
            return False

        obj_prim_path = getattr(obj, "prim_path", "")
        return any(
            path == obj_prim_path or path.startswith(f"{obj_prim_path}/")
            for path in contact_paths
        )

    def _move_hand_joint(self, joint_pos):
        if importlib.util.find_spec("ompl") is not None:
            yield from super()._move_hand_joint(joint_pos)
            return

        if not self._ompl_warning_logged:
            print(
                "[starter][arm] OMPL is unavailable; using direct joint-target "
                "motion without global collision-path planning"
            )
            sys.stdout.flush()
            self._ompl_warning_logged = True
        yield from self._move_hand_direct_joint(joint_pos)

    def _reset_hand(self):
        """Disable Starter's implicit reset; it is not a task-level action."""
        obj_in_hand = self._get_obj_in_hand()
        self._log_verbose(
            "[starter][arm] skipped implicit hand reset"
            + (
                ""
                if obj_in_hand is None
                else f" while holding object={obj_in_hand.name}"
            )
        )
        yield from ()

    def _navigate_if_needed(self, obj, pose_on_obj=None, **kwargs):
        """Navigate for internal manipulation targets, with explicit diagnostics.

        Starter calls this before GRASP / PLACE / OPEN / CLOSE with a concrete
        hand target pose. Unlike explicit NAVIGATE_TO(object), these internal
        calls should prefer a base pose that makes the target pose IK-reachable.
        """
        target_pose = self._normalize_target_pose(obj, pose_on_obj)
        target_kind = "manipulation_pose" if pose_on_obj is not None else "object"
        if self._safe_target_in_reach(target_pose):
            print(
                f"[starter][navigation] skip target={obj.name} "
                f"target_kind={target_kind} already_reachable=True"
            )
            sys.stdout.flush()
            return

        print(
            f"[starter][navigation] adjustment_required target={obj.name} "
            f"target_kind={target_kind}"
        )
        sys.stdout.flush()
        yield from self._navigate_to_obj(
            obj,
            pose_on_obj=target_pose,
            navigation_reason="manipulation",
            require_target_reachable=(target_kind == "manipulation_pose"),
            **kwargs,
        )

    def _navigate_to_explicit_target(self, obj):
        if self._should_use_open_pose_navigation(obj):
            yield from self._navigate_to_open_pose_preview(obj)
            return

        if self._should_use_grasp_ready_navigation(obj):
            if self.explicit_grasp_use_object_navigation:
                max_radius = (
                    self.explicit_grasp_navigation_max_goal_radius
                    if self.explicit_grasp_navigation_max_goal_radius is not None
                    else self.explicit_navigation_max_goal_radius
                )
                yield from self._navigate_to_obj(
                    obj,
                    navigation_reason="explicit_grasp_object",
                    require_target_reachable=False,
                    maximum_goal_radius_override=max_radius,
                    enforce_navigation_postcondition=max_radius is not None,
                )
                return

            yield from self._navigate_to_grasp_ready_pose(
                obj,
                navigation_reason="explicit_grasp_ready_stance",
            )
            return

        yield from self._navigate_to_obj(
            obj,
            navigation_reason="explicit",
            require_target_reachable=False,
            maximum_goal_radius_override=self.explicit_navigation_max_goal_radius,
            enforce_navigation_postcondition=(
                self.explicit_navigation_max_goal_radius is not None
            ),
        )

    def _should_use_grasp_ready_navigation(self, obj) -> bool:
        """Route explicit NAVIGATE_TO(pickup) to the physical GRASP stance.

        Starter plans often contain ``NAVIGATE_TO(object)`` immediately before
        ``GRASP(object)``.  If that explicit navigation only optimizes for
        seeing / approaching the object, the following symbolic GRASP can look
        physically implausible.  When the robot is empty-handed and the target
        is a small pickup-like object, use the same native grasp stance sampler
        that physical GRASP would use.  Receptacles / fixtures still use normal
        navigation because they are usually placement or opening destinations.
        """
        if obj is None or self._get_obj_in_hand() is not None:
            return False
        if self._should_use_open_pose_navigation(obj):
            return False

        category = getattr(obj, "category", "") or ""
        model = getattr(obj, "model", "") or ""
        non_pickup_markers = {
            "ashcan",
            "bin",
            "trash_can",
            "cabinet",
            "desk",
            "dish_rack",
            "dryer",
            "hamper",
            "rack",
            "shelf",
            "table",
            "counter",
            "sink",
            "vase",
            "door",
            "floor",
            "washer",
        }
        category_text = f"{category} {model}".lower()
        return not any(marker in category_text for marker in non_pickup_markers)

    def _should_use_open_pose_navigation(self, obj) -> bool:
        return (
            obj is not None
            and getattr(obj, "category", "") == "top_cabinet"
            and getattr(obj, "model", "") == "tactqn"
        )

    def _navigate_to_open_pose_preview(self, obj):
        """Navigate to the same front-side pose family used by physical OPEN.

        A plain explicit ``NAVIGATE_TO(cabinet)`` samples around the cabinet
        center and only requires a traversable base pose.  For the tactqn
        cabinet that can stop almost two meters away, which is fine for symbolic
        state changes but wrong for first-person observations.  Use the handle
        pose sampled by the physical OPEN code so the robot stands on the door
        front side and faces the cabinet / camera target before the symbolic
        OPEN shortcut runs.

        Symbolic manipulation must not change this stance choice: symbolic
        OPEN / PLACE only replaces the fragile arm-level state transition after
        the robot has reached the same physical-open ready pose.
        """
        cached_stance = self._get_cached_open_ready_stance(obj)
        if cached_stance is not None:
            print(
                "[starter][navigation][open_pose_stance_preview] "
                f"reusing_saved_stance target={obj.name} "
                f"pose_2d={self._to_float_list(cached_stance['pose_2d'])}"
            )
            sys.stdout.flush()
            return (yield from self._navigate_to_native_stance_pose(
                obj,
                pose_on_obj=cached_stance["target_pose"],
                navigation_reason="explicit_open_pose_stance_preview",
                preferred_goal_direction=cached_stance["preferred_goal_direction"],
                sampled_pose_2d=cached_stance["pose_2d"],
            ))

        grasp_data = self._sample_tactqn_navigation_grasp_data(obj)
        if grasp_data is None:
            print(
                "[starter][navigation][open_pose_stance_preview] "
                f"failed target={obj.name} reason=no_physical_open_grasp_pose"
            )
            sys.stdout.flush()
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.SAMPLING_ERROR,
                "Could not sample a physical-open stance pose for explicit cabinet navigation.",
                {"target object": obj.name},
            )

        _, grasp_pose, _, object_direction, _, _ = grasp_data
        preferred_goal_direction = -torch.as_tensor(
            object_direction[:2],
            dtype=torch.float32,
        )
        print(
            "[starter][navigation][open_pose_stance_preview] "
            f"target={obj.name} "
            f"grasp_pos={self._to_float_list(grasp_pose[0])} "
            f"preferred_goal_direction={self._to_float_list(preferred_goal_direction)} "
            "sampler=omnigibson_native_starter"
        )
        sys.stdout.flush()
        pose_2d = yield from self._navigate_to_native_stance_pose(
            obj,
            pose_on_obj=grasp_pose,
            navigation_reason="explicit_open_pose_stance_preview",
            preferred_goal_direction=preferred_goal_direction,
        )
        self._save_open_ready_stance(
            obj,
            grasp_pose,
            preferred_goal_direction,
            pose_2d,
        )
        return pose_2d

    def _save_open_ready_stance(
        self,
        obj,
        target_pose,
        preferred_goal_direction,
        pose_2d,
    ):
        key = getattr(obj, "name", str(id(obj)))
        self._open_ready_stance_cache[key] = {
            "obj": obj,
            "target_pose": (
                torch.as_tensor(target_pose[0], dtype=torch.float32).clone(),
                torch.as_tensor(target_pose[1], dtype=torch.float32).clone(),
            ),
            "preferred_goal_direction": torch.as_tensor(
                preferred_goal_direction,
                dtype=torch.float32,
            ).clone(),
            "pose_2d": torch.as_tensor(pose_2d, dtype=torch.float32).clone(),
        }
        print(
            "[starter][navigation][open_pose_stance_preview] "
            f"saved_stance target={key} "
            f"pose_2d={self._to_float_list(pose_2d)}"
        )
        sys.stdout.flush()

    def _get_cached_open_ready_stance(self, obj):
        key = getattr(obj, "name", str(id(obj)))
        cached = self._open_ready_stance_cache.get(key)
        if cached is None:
            return None
        if cached.get("obj") is not obj and key != getattr(obj, "name", None):
            return None
        return cached

    def _sample_tactqn_navigation_grasp_data(self, obj):
        for should_open in (True, False):
            for grasp_candidate_index in range(min(m.MAX_ATTEMPTS_FOR_OPEN_CLOSE, 3)):
                try:
                    grasp_data = self._sample_open_close_grasp_data(
                        obj,
                        should_open=should_open,
                        num_waypoints=8 if should_open else 3,
                        grasp_candidate_index=grasp_candidate_index,
                    )
                except ActionPrimitiveError:
                    continue
                if grasp_data is not None:
                    return grasp_data
        return None

    def configure_task_placement_slots(
        self,
        placement_slots: Optional[Mapping[str, Any]],
    ):
        """Configure deterministic task placement slots for symbolic PLACE."""
        self.task_placement_slots = {}
        if not placement_slots:
            return
        if not isinstance(placement_slots, Mapping):
            print(
                "[starter][place][slot][warning] "
                f"ignored non-mapping config={placement_slots!r}"
            )
            sys.stdout.flush()
            return

        for target_name, object_slots in dict(placement_slots).items():
            if not isinstance(object_slots, Mapping):
                continue
            target_variants = self._name_variants_for_config_name(target_name)
            for object_name, slot_pose in dict(object_slots).items():
                if not isinstance(slot_pose, Mapping):
                    continue
                object_variants = self._name_variants_for_config_name(object_name)
                for object_key in object_variants:
                    for target_key in target_variants:
                        self.task_placement_slots[(object_key, target_key)] = dict(slot_pose)

        print(
            "[starter][place][slot] configured "
            f"slot_keys={len(self.task_placement_slots)}"
        )
        sys.stdout.flush()

    def _name_variants_for_config_name(self, name: Any) -> set[str]:
        variants = {str(name), str(name).lower()}
        object_ref = getattr(self.env.task, "object_scope", {}).get(str(name))
        if object_ref is None:
            for task_name, candidate_ref in getattr(self.env.task, "object_scope", {}).items():
                if str(task_name).lower() == str(name).lower():
                    object_ref = candidate_ref
                    break
        obj = getattr(object_ref, "wrapped_obj", None) if object_ref is not None else None
        if obj is not None:
            variants.add(str(obj.name))
            variants.add(str(obj.name).lower())
        return variants

    def _name_variants_for_object(self, obj) -> set[str]:
        variants = {str(getattr(obj, "name", "")), str(getattr(obj, "name", "")).lower()}
        for task_name, object_ref in getattr(self.env.task, "object_scope", {}).items():
            if getattr(object_ref, "wrapped_obj", None) is obj:
                variants.add(str(task_name))
                variants.add(str(task_name).lower())
        return {variant for variant in variants if variant}

    def _navigate_to_obj(
        self,
        obj,
        pose_on_obj=None,
        navigation_reason="internal",
        require_target_reachable=False,
        preferred_goal_direction=None,
        minimum_goal_radius_override=None,
        maximum_goal_radius_override=None,
        require_goal_direction_aligned=False,
        allow_unreachable_goal_fallback=True,
        enforce_navigation_postcondition=False,
        skip_if_already_satisfied=True,
        **kwargs,
    ):
        del kwargs
        target_pose = self._normalize_target_pose(obj, pose_on_obj)
        target_kind = "manipulation_pose" if pose_on_obj is not None else "object"
        start_distance = self._base_target_xy_distance(target_pose)
        print(
            f"[starter][navigation] target={obj.name} "
            f"target_kind={target_kind} "
            f"reason={navigation_reason} "
            f"require_target_reachable={require_target_reachable} "
            f"minimum_goal_radius_override={minimum_goal_radius_override} "
            f"maximum_goal_radius_override={maximum_goal_radius_override} "
            f"require_goal_direction_aligned={require_goal_direction_aligned} "
            f"allow_unreachable_goal_fallback={allow_unreachable_goal_fallback} "
            f"enforce_navigation_postcondition={enforce_navigation_postcondition} "
            f"skip_if_already_satisfied={skip_if_already_satisfied} "
            f"base_target_xy_distance={start_distance}"
        )
        sys.stdout.flush()
        yield from self._with_navigation_hand_actions_suppressed(
            self.navigation_backend.navigate_to_object(
                self,
                obj,
                target_pose=target_pose,
                prefer_target_reachable=require_target_reachable,
                preferred_goal_direction=preferred_goal_direction,
                minimum_goal_radius_override=minimum_goal_radius_override,
                maximum_goal_radius_override=maximum_goal_radius_override,
                require_goal_direction_aligned=require_goal_direction_aligned,
                allow_unreachable_goal_fallback=allow_unreachable_goal_fallback,
                skip_if_already_satisfied=skip_if_already_satisfied,
            )
        )

        reachable = self._safe_target_in_reach(target_pose)
        end_distance = self._base_target_xy_distance(target_pose)
        print(
            f"[starter][navigation] completed target={obj.name} "
            f"target_kind={target_kind} "
            f"reachable={reachable} "
            f"base_target_xy_distance={end_distance}"
        )
        sys.stdout.flush()
        if require_target_reachable and not reachable:
            if enforce_navigation_postcondition:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.PLANNING_ERROR,
                    "Navigation finished outside the required manipulation reach.",
                    {
                        "target": obj.name,
                        "target kind": target_kind,
                        "reason": navigation_reason,
                        "base target xy distance": end_distance,
                        "navigation result": getattr(
                            self.navigation_backend,
                            "last_navigation_result",
                            None,
                        ),
                    },
                )
            print(
                f"[starter][navigation][warning] target={obj.name} "
                f"target_kind={target_kind} "
                "is still outside manipulation reach after base navigation; "
                "continuing so the following physical primitive can attempt "
                "or report the arm-level failure"
            )
            sys.stdout.flush()

        navigation_result = getattr(
            self.navigation_backend,
            "last_navigation_result",
            None,
        )
        if (
            enforce_navigation_postcondition
            and require_goal_direction_aligned
            and isinstance(navigation_result, dict)
            and not navigation_result.get("end_direction_aligned", False)
        ):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Navigation finished on the wrong side of the target.",
                {
                    "target": obj.name,
                    "target kind": target_kind,
                    "reason": navigation_reason,
                    "navigation result": navigation_result,
                },
            )
        if (
            enforce_navigation_postcondition
            and maximum_goal_radius_override is not None
            and end_distance > float(maximum_goal_radius_override) + 0.15
        ):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Navigation finished farther than the allowed goal radius.",
                {
                    "target": obj.name,
                    "target kind": target_kind,
                    "reason": navigation_reason,
                    "base target xy distance": end_distance,
                    "maximum goal radius": float(maximum_goal_radius_override),
                    "navigation result": navigation_result,
                },
            )

    def _normalize_target_pose(self, obj, pose_on_obj=None):
        if pose_on_obj is None:
            return obj.get_position_orientation()

        if self._looks_like_pose(pose_on_obj):
            return pose_on_obj

        _, obj_orientation = obj.get_position_orientation()
        return torch.as_tensor(pose_on_obj, dtype=torch.float32), obj_orientation

    @staticmethod
    def _looks_like_pose(value) -> bool:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            return False
        pos, orn = value
        try:
            return len(pos) >= 3 and len(orn) >= 4
        except TypeError:
            return False

    def _safe_target_in_reach(self, target_pose) -> bool:
        try:
            return bool(self._target_in_reach_of_robot(target_pose))
        except Exception:
            return False

    def _base_target_xy_distance(self, target_pose):
        try:
            robot_pos = self.robot.get_position_orientation()[0]
            target_pos = torch.as_tensor(target_pose[0], dtype=torch.float32)
            return round(float(torch.norm(robot_pos[:2] - target_pos[:2]).item()), 6)
        except Exception:
            return None

    @staticmethod
    def _to_float_list(value):
        if value is None:
            return None
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "tolist"):
            value = value.tolist()
        return [round(float(item), 6) for item in value]

    def _log_verbose(self, message: str):
        if self.verbose:
            print(message)
            sys.stdout.flush()

    @staticmethod
    def _short_error(exc, limit: int = 500) -> str:
        text = str(exc)
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        return f"{exc.__class__.__name__}: {text}"
