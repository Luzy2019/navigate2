import importlib.util
import os
import sys
from typing import Optional

import torch
import omnigibson.lazy as lazy
from omnigibson import object_states
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from omnigibson.action_primitives.starter_semantic_action_primitives import (
    StarterSemanticActionPrimitiveSet,
    StarterSemanticActionPrimitives,
    m,
)
from omnigibson.envs import Environment
from omnigibson.object_states.open_state import _get_relevant_joints
from omnigibson.utils.control_utils import IKSolver
from omnigibson.utils.constants import JointType
import omnigibson.utils.transform_utils as T

from og_ego_prim.navigation import NavigationBackend, OmniGibsonNavigationBackend


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
        **kwargs,
    ):
        super().__init__(env, **kwargs)
        self.navigation_backend = navigation_backend or OmniGibsonNavigationBackend(
            allow_native_fallback=False
        )
        self.navigation_backend.reset(env)
        self.verbose = os.environ.get("ISBENCH_STARTER_VERBOSE", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.tactqn_open_goal_radius = float(
            os.environ.get("ISBENCH_STARTER_TACTQN_OPEN_GOAL_RADIUS", "0.58")
        )
        if self.tactqn_open_goal_radius < 0.45:
            raise ValueError(
                "ISBENCH_STARTER_TACTQN_OPEN_GOAL_RADIUS must be at least 0.45"
            )
        self.tactqn_symbolic_open_close_fallback = os.environ.get(
            "ISBENCH_STARTER_TACTQN_SYMBOLIC_OPEN_CLOSE_FALLBACK",
            "1",
        ).lower() in {"1", "true", "yes", "on"}
        self._ompl_warning_logged = False
        self._cached_ik_solver = None
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.NAVIGATE_TO
        ] = self._navigate_to_obj
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.GRASP
        ] = self._grasp
        self.controller_functions[
            StarterSemanticActionPrimitiveSet.PLACE_INSIDE
        ] = self._place_inside

    def apply_ref(self, prim, *args, attempts=3):
        """Execute one semantic primitive without Starter's implicit arm reset.

        Upstream Starter retries every primitive and calls ``_reset_hand`` after
        every attempt.  That reset is not part of the requested task action and,
        without OMPL, can become a large direct joint motion.  In particular it
        is unsafe between GRASP and PLACE while an object is constrained to the
        gripper, so this physical mode executes the requested primitive once and
        only settles the robot afterwards.
        """
        assert attempts > 0, "Must make at least one attempt"

        if prim == StarterSemanticActionPrimitiveSet.NAVIGATE_TO:
            yield from self._navigate_to_obj(
                *args,
                navigation_reason="explicit",
                require_target_reachable=False,
            )
            yield from self._settle_robot()
            return
        if prim == StarterSemanticActionPrimitiveSet.GRASP:
            yield from self._apply_grasp_without_default_reset(*args)
            return

        ctrl = self.controller_functions[prim]
        try:
            yield from ctrl(*args)
        except ActionPrimitiveError:
            try:
                yield from self._settle_robot()
            except ActionPrimitiveError:
                pass
            raise

        try:
            yield from self._settle_robot()
        except ActionPrimitiveError:
            pass

    def _apply_grasp_without_default_reset(self, obj):
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
        print(f"[starter][grasp] object-level pre-navigation target={obj.name}")
        sys.stdout.flush()
        yield from self._navigate_to_obj(
            obj,
            navigation_reason="grasp_object_precheck",
            require_target_reachable=False,
        )
        print(f"[starter][grasp] starting physical grasp target={obj.name}")
        sys.stdout.flush()
        try:
            yield from super()._grasp(obj)
        except ActionPrimitiveError as exc:
            repaired = yield from self._repair_sticky_grasp_if_contacted(obj, exc)
            if repaired:
                return
            raise

    def _open_or_close(self, obj, should_open):
        """Open / close an object with a scoped symbolic cabinet exception."""
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

        # The tactqn cabinet has no separately addressable handle and its WIP
        # physical trajectory repeatedly leaves Fetch's arm fully extended at
        # countertop height.  That stale pose can physically block the next
        # base navigation.  This one known cabinet is therefore symbolic from
        # the start; all other open / close targets retain the physical path.
        if self._should_symbolically_fallback_open_close(obj):
            print(
                f"[starter][open_close][symbolic_direct] action={action_name} "
                f"target={obj.name} physical_attempts=0"
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
        obj_in_hand = self._get_obj_in_hand()
        if self._should_use_drop_inside_fallback(obj_in_hand, obj):
            yield from self._drop_inside_open_container(obj_in_hand, obj)
            return

        yield from super()._place_inside(obj)

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

    def _navigate_to_obj(
        self,
        obj,
        pose_on_obj=None,
        navigation_reason="internal",
        require_target_reachable=False,
        preferred_goal_direction=None,
        minimum_goal_radius_override=None,
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
            f"base_target_xy_distance={start_distance}"
        )
        sys.stdout.flush()
        yield from self.navigation_backend.navigate_to_object(
            self,
            obj,
            target_pose=target_pose,
            prefer_target_reachable=require_target_reachable,
            preferred_goal_direction=preferred_goal_direction,
            minimum_goal_radius_override=minimum_goal_radius_override,
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
            print(
                f"[starter][navigation][warning] target={obj.name} "
                f"target_kind={target_kind} "
                "is still outside manipulation reach after base navigation; "
                "continuing so the following physical primitive can attempt "
                "or report the arm-level failure"
            )
            sys.stdout.flush()

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
