from typing import List, Optional

from aenum import IntEnum, auto
from omnigibson import object_states
from omnigibson.action_primitives.action_primitive_set_base import (
    ActionPrimitiveError, 
    ActionPrimitiveErrorGroup
)
from omnigibson.envs import Environment
from omnigibson.objects import StatefulObject
from omnigibson.object_states.object_state_base import RelativeObjectState 
from omnigibson.robots.robot_base import BaseRobot
from omnigibson.systems import BaseSystem
from omnigibson.transition_rules import SlicingRule
from omnigibson.utils.constants import PrimType
import omnigibson.utils.transform_utils as T
import torch

from .object_states_utils import (
    get_covered_systems, 
    get_contained_systems,
    get_produced_systems,
    get_saturated_systems,
    get_supported_systems,
    get_modified_systems,
    capture_attachments,
    recover_attachments,
    check_open_before_grasp,
    check_open_before_placement,
    check_close_before_toggle_on,
    check_toggle_off_before_open,
    is_cloth_place_on_other,
    get_placement_objects,
    is_visual_or_physical_particle_system,
    find_task_related_object,
    get_obj_with_state,
    is_target_object_predicate_with_obj
)
from og_ego_prim.config.runtime_config import NavigationConfig, StarterPrimitivesConfig
from og_ego_prim.navigation import NavigationBackend
from .specs import EGO_VALID_PRIMITIVES
from .starter_primitives import PhysicalStarterSemanticActionPrimitives
from .task_wet_floor import TaskWetFloorRegionController
from .wipe_transfer import (
    get_wipe_payload,
    plan_wipe_transfer,
    set_wipe_payload,
    visual_particle_system_names,
)


class EgoSemanticActionPrimitiveSet(IntEnum):
    _init_ = "value __doc__"
    GRASP = auto(), "Grasp the target_obj and keep it attached while navigating"
    RELEASE = auto(), "Release the currently grasped object"
    PLACE_ON_TOP = auto(), "Place the target_obj on top of placement_obj"
    PLACE_INSIDE = auto(), "Place the target_obj inside placement_obj"
    PLACE_NEXTTO = auto(), "Place the target_obj next to placement_obj"
    OPEN = auto(), "Open an target_obj"
    CLOSE = auto(), "Close an target_obj"
    TOGGLE_ON = auto(), "Toggle an target_obj on"
    TOGGLE_OFF = auto(), "Toggle an target_obj off"
    WIPE = auto(), "Wipe the target_obj with the cleaning_tool"
    CUT = auto(), "Cut (slice or dice) the target_obj with the cutting_tool"
    SOAK_UNDER = auto(), "Soak the target_obj with particles produced by the fluid_source"
    SOAK_INSIDE = auto(), "Soak the target_obj with particles in the fluid_container"
    FILL_WITH = auto(), "Fill the target_obj with particles produced by the fluid source"
    POUR_INTO = auto(), "Pour the particle in the fluid_container into the target_obj (usually a container)"
    WAIT_FOR_COOKED = auto(), "Wait for the cook process of the object to final"
    WAIT_FOR_WASHED = auto(), "Wait for the wash process fo the wash machine to final"
    WAIT = auto(), "Wait for the object to change, such as waiting for the object to rise to room temperature."
    SPREAD = auto(), "Spread some particles onto some object, make object covered with these particles"
    WAIT_FOR_FROZEN = auto(), "Wait something in the refridge to frozen"
    MARK_WET_REGION = auto(), "Mark a configured wet-floor region as a navigation obstacle"
    NAVIGATE_TO = auto(), "Navigate the robot near the target_obj"


VALID_PRIMITIVES = EGO_VALID_PRIMITIVES


class EgoSemanticActionPrimitives(PhysicalStarterSemanticActionPrimitives):

    def __init__(
        self,
        env: Environment,
        navigation_backend: Optional[NavigationBackend] = None,
        navigation_config: Optional[NavigationConfig] = None,
        starter_config: Optional[StarterPrimitivesConfig] = None,
    ):
        super().__init__(
            env,
            navigation_backend=navigation_backend,
            navigation_config=navigation_config,
            starter_config=starter_config,
        )
        self.controller_functions = {
            EgoSemanticActionPrimitiveSet.NAVIGATE_TO: self._navigate_to,
            EgoSemanticActionPrimitiveSet.GRASP: self._apply_grasp_without_default_reset,
            EgoSemanticActionPrimitiveSet.RELEASE: self._execute_release,
            EgoSemanticActionPrimitiveSet.PLACE_ON_TOP: self._place_on_top,
            EgoSemanticActionPrimitiveSet.PLACE_INSIDE: self._place_inside,
            EgoSemanticActionPrimitiveSet.PLACE_NEXTTO: self._place_nextto,
            EgoSemanticActionPrimitiveSet.OPEN: self._open,  # done
            EgoSemanticActionPrimitiveSet.CLOSE: self._close,  # done
            EgoSemanticActionPrimitiveSet.TOGGLE_ON: self._toggle_on,  # done
            EgoSemanticActionPrimitiveSet.TOGGLE_OFF: self._toggle_off,  # done
            EgoSemanticActionPrimitiveSet.WIPE: self._wipe,
            EgoSemanticActionPrimitiveSet.CUT: self._cut,
            EgoSemanticActionPrimitiveSet.SOAK_INSIDE: self._soak_inside,
            EgoSemanticActionPrimitiveSet.SOAK_UNDER: self._soak_under,
            EgoSemanticActionPrimitiveSet.FILL_WITH: self._fill_with,
            EgoSemanticActionPrimitiveSet.POUR_INTO: self._pour_into,
            EgoSemanticActionPrimitiveSet.WAIT_FOR_COOKED: self._wait_for_cooked,
            EgoSemanticActionPrimitiveSet.WAIT_FOR_WASHED: self._wait_for_washed,
            EgoSemanticActionPrimitiveSet.WAIT: self._wait,
            EgoSemanticActionPrimitiveSet.SPREAD: self._spread,
            EgoSemanticActionPrimitiveSet.WAIT_FOR_FROZEN: self._wait_for_frozen,
            EgoSemanticActionPrimitiveSet.MARK_WET_REGION: self._mark_wet_region,
        }
        self.env = env
        self.attachments = []
        self.task_wet_floor_regions = TaskWetFloorRegionController(env)

    def apply_ref(self, primitive, *args):
        if any(isinstance(arg, BaseRobot) for arg in args):
            raise ActionPrimitiveErrorGroup(
                [
                    ActionPrimitiveError(
                        ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                        "Cannot call a symbolic semantic action primitive with a robot as an argument.",
                    )
                ]
            )
        
        try:
            yield from self.controller_functions[primitive](*args)
        except ActionPrimitiveError as e:
            raise ActionPrimitiveErrorGroup([e])

        # Settle before returning.
        try:
            yield from self._settle_robot()
        except ActionPrimitiveError:
            pass

    def _navigate_to(self, target_obj: StatefulObject):
        yield from self._with_navigation_hand_actions_suppressed(
            self.navigation_backend.navigate_to_object(self, target_obj)
        )

    def _open_or_close(self, target_obj: StatefulObject, should_open: bool):
        if object_states.Open not in target_obj.states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not openable.",
                {"target object": target_obj.name},
            )
        
        # Don't do anything if the object is already closed and we're trying to close.
        if should_open == target_obj.states[object_states.Open].get_value():
            return
        
        if should_open is True:
            check_toggle_off_before_open(target_obj)

        inside_placements = get_placement_objects(target_obj, self.env, object_states.Inside)
        
        # Set the value
        target_obj.states[object_states.Open].set_value(should_open, fully=True)
        yield from self._settle_robot()

        if inside_placements:
            for placement in inside_placements:
                obj = placement.object
                if not is_target_object_predicate_with_obj(obj, target_obj, object_states.Inside):
                    yield from self._place_inside(obj, target_obj, skip_check=True)

        if target_obj.states[object_states.Open].get_value() != should_open:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "The object did not open or close as expected. Maybe try again",
                {"target object": target_obj.name, "is it currently open": target_obj.states[object_states.Open].get_value()},
            )

    def _place_on_top(self, target_obj: StatefulObject, placement_obj: StatefulObject, **kwargs):
        yield from self._place_with_predicate(target_obj, placement_obj, object_states.OnTop, **kwargs)

    def _place_inside(self, target_obj: StatefulObject, placement_obj: StatefulObject, **kwargs):
        yield from self._place_with_predicate(target_obj, placement_obj, object_states.Inside, **kwargs)

    def _place_nextto(self, target_obj: StatefulObject, placement_obj: StatefulObject, **kwargs):
        yield from self._place_with_predicate(target_obj, placement_obj, object_states.NextTo, **kwargs)

    def _sample_on_top_heat_source(self, target_obj: StatefulObject, heat_source: StatefulObject):
        heating_element = heat_source.states[object_states.HeatSourceOrSink].link
        heating_element_positions = heating_element.get_position_orientation()[0] + torch.tensor([0, 0, 0.1])
        target_obj.set_position_orientation(position=heating_element_positions)
        yield from self._settle_robot()

    def _sample_placement_with_predicate(
        self, 
        target_obj: StatefulObject, 
        placement_obj: StatefulObject, 
        predicate: RelativeObjectState,
    ):
        placement_obj_pose = placement_obj.get_position_orientation()

        attempts = 0
        while attempts < 5:
            attempts += 1
            try:
                # Find a spot to put it
                predicated_pose = self._sample_pose_with_object_and_predicate(
                    predicate, target_obj, placement_obj
                )
            except Exception as e:
                print(f'Attempt {attempts}: {e}')
                continue

            # Actually move the target object to the spot and step a bit to settle it.
            target_obj.set_position_orientation(*predicated_pose)
            yield from self._settle_robot()

            if target_obj.states[predicate].get_value(placement_obj):
                break
            else:
                # recover if failed
                placement_obj.set_position_orientation(*placement_obj_pose)
                placement_obj.keep_still()
                yield from self._settle_robot()

    def _place_with_predicate(
        self, 
        target_obj: StatefulObject | BaseSystem, 
        placement_obj: StatefulObject, 
        predicate: RelativeObjectState,
        skip_check: bool = False,
    ):
        if isinstance(target_obj, BaseSystem) and is_visual_or_physical_particle_system(target_obj.scene, target_obj):
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Cannot place system to desired position, perhaps place its container like bottle to the position",
                {"system (target object)": target_obj.name},
            )

        held_obj = self._get_obj_in_hand()
        if (
            held_obj is None
            and predicate == object_states.OnTop
            and is_cloth_place_on_other(target_obj, placement_obj)
        ):
            predicate = object_states.Overlaid
        if predicate not in [object_states.OnTop, object_states.Inside, object_states.Overlaid, object_states.NextTo]:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "Only support place the target object OnTop, OverLaid, Inside, or NextTo the placement object.",
                {"provided_predicate": predicate.__class__.__name__},
            )

        if held_obj is None and not skip_check:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The requested source must be grasped before placement.",
                {
                    "requested source": target_obj.name,
                    "placement object": placement_obj.name,
                },
            )
        if held_obj is not None:
            if held_obj is not target_obj:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                    "The requested source is not the object currently being carried.",
                    {
                        "requested source": target_obj.name,
                        "carried object": held_obj.name,
                    },
                )
            if not skip_check and predicate == object_states.Inside:
                check_open_before_placement(placement_obj)

            if predicate == object_states.Inside:
                yield from PhysicalStarterSemanticActionPrimitives._place_inside(
                    self,
                    placement_obj,
                )
            elif predicate == object_states.OnTop:
                yield from PhysicalStarterSemanticActionPrimitives._place_on_top(
                    self,
                    placement_obj,
                )
            else:
                yield from self._symbolic_place_with_predicate(
                    placement_obj,
                    predicate,
                )
            return

        # Only internal relation repair, such as OPEN restoring displaced
        # contents, may place an object without first establishing a grasp.
        attachments = capture_attachments(target_obj, self.env)

        placed_with_custom_pose = yield from self._try_task_specific_placement(
            target_obj,
            placement_obj,
        )

        if placed_with_custom_pose:
            pass
        elif object_states.HeatSourceOrSink in placement_obj.states and predicate == object_states.OnTop and \
              not placement_obj.states[object_states.HeatSourceOrSink].requires_inside:
            yield from self._sample_on_top_heat_source(target_obj, placement_obj)

        elif predicate == object_states.Overlaid:
            placement_pose = placement_obj.get_position_orientation()[0]
            position = placement_pose + torch.tensor([0, 0, 0.1])
            target_obj.set_position_orientation(position=position)
            yield from self._settle_robot()

        elif predicate == object_states.NextTo:
            yield from self._sample_placement_with_predicate(target_obj, placement_obj, predicate)

            if not target_obj.states[predicate].get_value(placement_obj):
                yield from self._place_nextto_by_bbox(target_obj, placement_obj)

        else:
            yield from self._sample_placement_with_predicate(target_obj, placement_obj, predicate)

            # Last attempt to directly place the target object with predicate
            if not target_obj.states[predicate].get_value(placement_obj):
                if predicate == object_states.OnTop:  # ontop 
                    placement_pose = placement_obj.get_position_orientation()[0]
                    position = placement_pose + torch.tensor([0, 0, 0.1])
                else: # others, inside
                    placement_obj_center = placement_obj.get_base_aligned_bbox()[0]
                    position = placement_obj_center

                target_obj.set_position_orientation(position=position)
                yield from self._settle_robot()

            if predicate == object_states.Inside:
                yield from self._try_visible_cabinet_inside_placement(target_obj, placement_obj)

        if attachments:
            recover_attachments(attachments)
            yield from self._settle_robot()

        if placed_with_custom_pose:
            return

        # check
        error = ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Failed to place target object at the desired place (probably dropped).",
            {"dropped object": target_obj.name, "placement object": placement_obj.name, "predicate": predicate.__class__.__name__},
        )
        if predicate == object_states.OnTop:
            adjacency = target_obj.states[object_states.VerticalAdjacency].get_value()
            if not placement_obj in adjacency.negative_neighbors or placement_obj in adjacency.positive_neighbors:
                raise error
        elif predicate == object_states.Inside and not target_obj.states[predicate].get_value(placement_obj):
                raise error
        elif predicate == object_states.NextTo and not target_obj.states[predicate].get_value(placement_obj):
                raise error
        elif predicate == object_states.Overlaid and not target_obj.states[object_states.Touching].get_value(placement_obj):
                raise error

    def _place_nextto_by_bbox(self, target_obj: StatefulObject, placement_obj: StatefulObject):
        placement_center, _, placement_extent, _ = placement_obj.get_base_aligned_bbox()
        _, _, target_extent, _ = target_obj.get_base_aligned_bbox()
        px, py, pz = [float(value) for value in placement_center]
        pw, pd, ph = [float(value) for value in placement_extent]
        tw, td, th = [float(value) for value in target_extent]
        z = pz - ph / 2.0 + th / 2.0 + 0.01
        offsets = [
            (pw / 2.0 + tw / 2.0 + 0.015, 0.0),
            (-(pw / 2.0 + tw / 2.0 + 0.015), 0.0),
            (0.0, pd / 2.0 + td / 2.0 + 0.015),
            (0.0, -(pd / 2.0 + td / 2.0 + 0.015)),
        ]
        orientation = target_obj.get_position_orientation()[1]
        for dx, dy in offsets:
            target_obj.set_position_orientation(
                position=torch.tensor([px + dx, py + dy, z], dtype=torch.float32),
                orientation=orientation,
            )
            target_obj.keep_still()
            yield from self._settle_robot()
            if target_obj.states[object_states.NextTo].get_value(placement_obj):
                return

    def _try_task_specific_placement(self, target_obj: StatefulObject, placement_obj: StatefulObject):
        """Place objects on sparse or narrow custom models using bbox-relative poses."""
        target_key = (getattr(target_obj, "category", ""), getattr(target_obj, "model", ""))
        placement_key = (getattr(placement_obj, "category", ""), getattr(placement_obj, "model", ""))

        if target_key == ("half_banana", "xytkre") and placement_key == ("trash_can", "gvnfgj"):
            relation = object_states.Inside
            placement_kind = "inside"
        elif target_key == ("dishtowel", "ltydgg") and placement_key == ("dish_rack", "kxuutl"):
            relation = object_states.OnTop
            placement_kind = "rack_top"
        else:
            return False

        original_pose = target_obj.get_position_orientation()
        bbox_center, bbox_orn, bbox_extent, _ = placement_obj.get_base_aligned_bbox()
        _, _, target_extent, _ = target_obj.get_base_aligned_bbox()
        width, depth, height = [float(value) for value in bbox_extent]
        target_width, target_depth, target_height = [float(value) for value in target_extent]
        bbox_pose = T.pose2mat((bbox_center, bbox_orn))

        if placement_kind == "inside":
            x_limit = max(0.0, (width - target_width) / 2.0 - 0.03)
            y_limit = max(0.0, (depth - target_depth) / 2.0 - 0.03)
            z_low = -height / 2.0 + target_height / 2.0 + 0.025
            z_high = height / 2.0 - target_height / 2.0 - 0.025
            z_candidates = [
                z_low + height * 0.06,
                z_low + height * 0.14,
                -height * 0.12,
                0.0,
            ]
            local_offsets = [
                (x, y, min(max(z, z_low), z_high))
                for z in z_candidates
                for x, y in [
                    (0.0, 0.0),
                    (-x_limit * 0.25, 0.0),
                    (x_limit * 0.25, 0.0),
                    (0.0, -y_limit * 0.25),
                    (0.0, y_limit * 0.25),
                ]
            ]
            orientation = original_pose[1]
        else:
            top_z = height / 2.0 + target_height / 2.0
            local_offsets = [
                (x, y, top_z + z_margin)
                for z_margin in (0.012, 0.025, 0.04)
                for x, y in [
                    (0.0, 0.0),
                    (-width * 0.22, 0.0),
                    (width * 0.22, 0.0),
                    (0.0, -depth * 0.18),
                    (0.0, depth * 0.18),
                ]
            ]
            orientation = bbox_orn

        for offset in local_offsets:
            local_offset = torch.tensor(offset, dtype=torch.float32)
            position = T.transform_points(local_offset.reshape(1, 3), bbox_pose)[0]
            target_obj.set_position_orientation(position=position, orientation=orientation)
            target_obj.keep_still()
            yield from self._settle_robot()

            if target_obj.states[relation].get_value(placement_obj):
                print(
                    f"[executor] adjusted {placement_kind} placement "
                    f"for {target_obj.name} relative to {placement_obj.name}"
                )
                return True

        target_obj.set_position_orientation(*original_pose)
        target_obj.keep_still()
        yield from self._settle_robot()
        return False

    def _try_visible_cabinet_inside_placement(self, target_obj: StatefulObject, placement_obj: StatefulObject):
        """Prefer visible shelf/front placements for small objects stored in selected cabinets.

        OmniGibson's Inside sampler may choose a logically valid point deep inside
        the fillable volume. For the custom cabinet-storage scene, that can make
        the object invisible even while the cabinet is open. Keep this scoped to
        the cabinet models we intentionally use for that task.
        """
        target_category = getattr(target_obj, "category", "")
        placement_category = getattr(placement_obj, "category", "")
        placement_model = getattr(placement_obj, "model", "")

        if target_category not in {"apple", "box_of_tissues", "box__of__tissue"}:
            return False
        if (placement_category, placement_model) not in {
            ("bottom_cabinet_no_top", "spojpj"),
            ("top_cabinet", "tactqn"),
        }:
            return False

        original_pose = target_obj.get_position_orientation()
        bbox_center, bbox_orn, bbox_extent, _ = placement_obj.get_base_aligned_bbox()
        _, _, target_extent, _ = target_obj.get_base_aligned_bbox()

        width, depth, height = [float(v) for v in bbox_extent]
        target_width, target_depth, target_height = [float(v) for v in target_extent]
        x_low = -width / 2.0 + target_width / 2.0 + 0.03
        x_high = width / 2.0 - target_width / 2.0 - 0.03
        y_low = -depth / 2.0 + target_depth / 2.0 + 0.03
        y_high = depth / 2.0 - target_depth / 2.0 - 0.03
        z_low = -height / 2.0 + target_height / 2.0 + 0.03
        z_high = height / 2.0 - target_height / 2.0 - 0.03

        if z_low > z_high:
            return False

        def clamp_axis(value, low, high):
            if low > high:
                return value
            return min(max(value, low), high)

        def clamp_z(z):
            return min(max(z, z_low), z_high)

        def unique_offsets(offsets):
            seen = set()
            unique = []
            for x, y, z in offsets:
                key = (round(float(x), 4), round(float(y), 4), round(float(z), 4))
                if key in seen:
                    continue
                seen.add(key)
                unique.append(torch.tensor([x, y, z], dtype=torch.float32))
            return unique

        bbox_pose = T.pose2mat((bbox_center, bbox_orn))
        local_offsets = []
        if target_category in {"box_of_tissues", "box__of__tissue"}:
            apple_obj = find_task_related_object(self.env, "apple")
            if apple_obj is not None and apple_obj is not target_obj:
                apple_pos = apple_obj.get_position_orientation()[0]
                apple_local = T.transform_points(apple_pos.reshape(1, 3), T.pose_inv(bbox_pose))[0]
                apple_x = float(apple_local[0])
                apple_y = float(apple_local[1])
                apple_z = float(apple_local[2])

                x_candidates = [
                    apple_x,
                    apple_x - width * 0.04,
                    apple_x + width * 0.04,
                    width * 0.18,
                    width * 0.10,
                    0.0,
                ]
                y_candidates = [
                    apple_y - depth * 0.18,
                    apple_y - depth * 0.10,
                    apple_y - depth * 0.06,
                    apple_y,
                    apple_y + depth * 0.06,
                    apple_y + depth * 0.10,
                    depth * 0.34,
                    depth * 0.20,
                    0.0,
                ]
                z_candidates = [
                    clamp_z(apple_z),
                    clamp_z(apple_z + height * 0.04),
                    clamp_z(height * 0.12),
                    clamp_z(0.0),
                ]

                for z in z_candidates:
                    for y in y_candidates:
                        for x in x_candidates:
                            local_offsets.append(
                                (
                                    clamp_axis(x, x_low, x_high),
                                    clamp_axis(y, y_low, y_high),
                                    z,
                                )
                            )

        for z in [clamp_z(height * 0.12), clamp_z(0.0), clamp_z(-height * 0.18)]:
            for y in [depth * 0.34, depth * 0.20, 0.0, -depth * 0.20]:
                for x in [0.0, -width * 0.18, width * 0.18]:
                    local_offsets.append((x, y, z))

        local_offsets = unique_offsets(local_offsets)
        for local_offset in local_offsets:
            position = T.transform_points(local_offset.reshape(1, 3), bbox_pose)[0]
            target_obj.set_position_orientation(position=position)
            target_obj.keep_still()
            yield from self._settle_robot()

            if target_obj.states[object_states.Inside].get_value(placement_obj):
                print(
                    "[executor] adjusted visible inside placement "
                    f"for {target_obj.name} in {placement_obj.name}"
                )
                return True

        target_obj.set_position_orientation(*original_pose)
        target_obj.keep_still()
        yield from self._settle_robot()
        return False

    def _toggle(self, target_obj: StatefulObject, value: bool):
        if object_states.ToggledOn not in target_obj.states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not toggleable.",
                {"target object": target_obj.name},
            )
        
        if target_obj.states[object_states.ToggledOn].get_value() == value:
            return
        
        if value is True:
            check_close_before_toggle_on(target_obj)

        # Call the setter
        target_obj.states[object_states.ToggledOn].set_value(value)
        yield from self._settle_robot()

        # Check that it actually happened
        if target_obj.states[object_states.ToggledOn].get_value() != value:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "The object did not toggle as expected - maybe try again",
                {
                    "target object": target_obj.name,
                    "is it currently toggled on": target_obj.states[object_states.ToggledOn].get_value(),
                },
            )
        
    def _wipe(self, target_obj: StatefulObject, cleaning_tool: StatefulObject):
        payload_before = get_wipe_payload(cleaning_tool)
        saturated_before = tuple(get_saturated_systems(cleaning_tool) or ())

        # Check that the cleaning tool can remove those particles
        if object_states.ParticleRemover not in cleaning_tool.states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The cleaning tool is not a particle remover.",
                {"cleaning tool": cleaning_tool.name},
            )
        
        covered_systems = get_covered_systems(target_obj)
        # Check that the target object is coverable
        if covered_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not coverable by any particles, so there is no need to wipe it.",
                {"target object": target_obj.name},
            )
        
        check_open_before_grasp(target_obj, self.env)
        check_open_before_grasp(cleaning_tool, self.env)

        # Check if the target object has any particles on it
        if not covered_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not covered by any particles.",
                {"target object": target_obj.name},
            )
        
        supported_systems = get_supported_systems(
            cleaning_tool, covered_systems, object_states.ParticleRemover
        )
        if not supported_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is covered only by particles that this cleaning tool cannot remove.",
                {
                    "target object": target_obj.name,
                    "cleaning tool": cleaning_tool.name,
                    "particles the target object is covered by": sorted(x.name for x in covered_systems),
                    "particles the cleaning tool can remove": sorted(
                        [x for x in cleaning_tool.states[object_states.ParticleRemover].conditions.keys()]
                    ),
                },
            )
        
        removed_systems = get_modified_systems(
            cleaning_tool, supported_systems, object_states.ParticleRemover
        )
        if not removed_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is covered by some particles that this cleaning tool can normally remove, but needs to be in a different state to do so (e.g. toggled on, soaked by another fluid first, etc.).",
                {
                    "target object": target_obj.name,
                    "cleaning tool": cleaning_tool.name,
                    "particles the target object is covered by": sorted(x.name for x in covered_systems),
                },
            )

        transfer = plan_wipe_transfer(
            carried_before=payload_before,
            removed_visual_contaminants=visual_particle_system_names(
                target_obj.scene,
                removed_systems,
            ),
        )

        systems_by_name = {
            system.name: system for system in target_obj.scene.system_registry.objects
        }
        missing_payload = [
            name for name in transfer.redeposit_systems if name not in systems_by_name
        ]
        if missing_payload:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                "The cleaning tool carries contamination systems that are unavailable in the scene.",
                {
                    "cleaning tool": cleaning_tool.name,
                    "missing systems": missing_payload,
                },
            )

        reserved_saturated = self.task_wet_floor_regions.reserved_saturated_system_names(
            target_obj,
            cleaning_tool,
        )
        saturation_to_transfer = {
            system.name: system
            for system in saturated_before
            if system.name == "water" and system.name not in reserved_saturated
        }
        systems_to_deposit = {
            name: systems_by_name[name] for name in transfer.redeposit_systems
        }
        systems_to_deposit.update(saturation_to_transfer)
        affected_systems = {
            system.name: system for system in removed_systems
        }
        affected_systems.update(systems_to_deposit)
        affected_systems.update(
            {
                system.name: system
                for system in saturated_before
                if system.name in reserved_saturated
            }
        )
        covered_state = target_obj.states[object_states.Covered]
        coverage_before = {
            name: bool(covered_state.get_value(system))
            for name, system in affected_systems.items()
        }

        try:
            MAX_WIPE_NUMS = 3
            for i in range(MAX_WIPE_NUMS):  # 最多wipe 三次
                print(f"######[INFO] Try to Wipe at times {i}")
                for system in removed_systems:
                    covered_state.set_value(system, False)
                    yield from self._settle_robot()
                remaining_removed = [
                    system
                    for system in removed_systems
                    if covered_state.get_value(system)
                ]
                if not remaining_removed:
                    break

            if remaining_removed:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                    "The WIPE action did not remove all supported contaminants.",
                    {
                        "target object": target_obj.name,
                        "cleaning tool": cleaning_tool.name,
                        "remaining systems": sorted(
                            system.name for system in remaining_removed
                        ),
                    },
                )

            for system in systems_to_deposit.values():
                covered_state.set_value(system, True)
            if systems_to_deposit:
                yield from self._settle_robot()

            failed_deposits = [
                name
                for name, system in systems_to_deposit.items()
                if not covered_state.get_value(system)
            ]
            if failed_deposits:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.POST_CONDITION_ERROR,
                    "The WIPE action did not transfer the tool's prior payload or saturation.",
                    {
                        "target object": target_obj.name,
                        "cleaning tool": cleaning_tool.name,
                        "missing systems": sorted(failed_deposits),
                    },
                )

            set_wipe_payload(cleaning_tool, transfer.resulting_payload)
            yield from self.task_wet_floor_regions.after_wipe(
                self,
                target_obj,
                cleaning_tool,
            )
        except Exception:
            set_wipe_payload(cleaning_tool, payload_before)
            for name, system in affected_systems.items():
                try:
                    covered_state.set_value(system, coverage_before[name])
                except Exception as restore_error:
                    print(
                        "[wipe_transfer] failed to restore "
                        f"target={target_obj.name} system={name}: {restore_error}",
                        flush=True,
                    )
            try:
                yield from self._settle_robot()
            except Exception as restore_error:
                print(
                    "[wipe_transfer] failed to settle restored WIPE state: "
                    f"{restore_error}",
                    flush=True,
                )
            raise

    def _mark_wet_region(self, target_obj: StatefulObject):
        was_marked = self.task_wet_floor_regions.is_marked(target_obj)
        self.task_wet_floor_regions.mark_wet_region(target_obj)
        try:
            yield from self._settle_robot()
        except Exception:
            if not was_marked:
                self.task_wet_floor_regions.restore_marked_region(target_obj)
            raise
                    

    def _cut(self, target_obj: StatefulObject, cutting_tool: StatefulObject):
        # Check that cutting tool is a slicer
        if "slicer" not in cutting_tool._abilities:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The cutting tool cannot slice object.",
                {"cutting tool": cutting_tool.name},
            )
        
        # Check that the target object is sliceable
        if "sliceable" not in target_obj._abilities and "diceable" not in target_obj._abilities:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not sliceable or diceable.",
                {"target object": target_obj.name},
            )

        check_open_before_grasp(target_obj, self.env)
        check_open_before_grasp(cutting_tool, self.env)

        added_obj_attrs, removed_objs = [], []
        (slicing_rule,) = [
            rule 
            for rule in target_obj.scene.transition_rule_api.active_rules 
            if isinstance(rule, SlicingRule)
        ]
        output = slicing_rule.transition({"sliceable": [target_obj]})

        added_obj_attrs += output.add
        removed_objs += output.remove
        target_obj.scene.transition_rule_api.execute_transition(
            added_obj_attrs=added_obj_attrs, removed_objs=removed_objs
        )
        yield from self._settle_robot()
    
    def _soak_with_fluid_systems(self, target_obj: StatefulObject, systems: List[BaseSystem]) -> Optional[List[BaseSystem]]:
        # Check that the target object can saturated (remove) with particles in container or producer
        supported_systems = get_supported_systems(
            target_obj, systems, object_states.ParticleRemover
        )
        if not supported_systems:
            return None
        
        removed_systems = get_modified_systems(
            target_obj, supported_systems, object_states.ParticleRemover
        )
        if not removed_systems:
            return None
        
        return removed_systems

    def _soak_under(self, target_obj: StatefulObject, fluid_source: StatefulObject):
        # Check that the target object can saturated (remove) with particles
        if object_states.Saturated not in target_obj.states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The currently grasped object cannot soak particles.",
                {"object in hand": target_obj.name},
            )

        check_open_before_grasp(target_obj, self.env)

        # Check that the fluid source should either be a particle producer or a particle container
        produced_systems = get_produced_systems(fluid_source)
        if produced_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid source is not a particle producer, so you can not soak target object.",
                {"fluid source": fluid_source.name},
            )
        if not produced_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid source currently does not produce particles, may be some conditions for producing particles not met, e.g., the fluid source should be toggled on.",
                {"fluid source": fluid_source.name}
            )
        
        removed_produced_systems = self._soak_with_fluid_systems(target_obj, produced_systems)
        if removed_produced_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object cannot soak with particles from fluid source, maybe target object cannot support the particles or saturation reaches upper limit",
                {
                    "target object": target_obj.name,
                    "fluid source": fluid_source.name,
                    "particles the target object can soak:": sorted(
                        [x for x in target_obj.states[object_states.ParticleRemover].conditions.keys()]
                    ),
                    "particles the fluid source produces": sorted(x.name for x in produced_systems) if produced_systems else None,
                },
            )
        
        # Remove the particles.
        for system in removed_produced_systems:
            target_obj.states[object_states.Saturated].set_value(system, True)
        
        yield from self._settle_robot()
    
    def _soak_inside(self, target_obj: StatefulObject, fluid_container: StatefulObject):
        # Check that the target object can saturated (remove) with particles
        if object_states.Saturated not in target_obj.states:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The currently grasped object cannot soak particles.",
                {"target object": target_obj.name},
            )

        check_open_before_grasp(target_obj, self.env)
        check_open_before_grasp(fluid_container, self.env)

        contained_systems = get_contained_systems(fluid_container)
        if contained_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid container is not a particle container, so you can not soak target object.",
                {"fluid container": fluid_container.name},
            )
        
        if not contained_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid source currently does not contain any particles.",
                {"fluid container": fluid_container.name}
            )
        
        removed_contained_systems = self._soak_with_fluid_systems(target_obj, contained_systems)
        if removed_contained_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object cannot soak with particles from fluid container, maybe target object cannot support the particles or saturation reaches upper limit",
                {
                    "target object": target_obj.name,
                    "fluid container": fluid_container.name,
                    "particles the target object can soak:": sorted(
                        [x for x in target_obj.states[object_states.ParticleRemover].conditions.keys()]
                    ),
                    "particles in the fluid container": sorted(x.name for x in contained_systems) if contained_systems else None,
                },
            )
        
        # Remove the particles.
        for system in removed_contained_systems:
            target_obj.states[object_states.Saturated].set_value(system, True)
        
        yield from self._settle_robot()

    def _fill_with(self, target_obj: StatefulObject, fluid_source: StatefulObject):
        # Check that target object is fillable
        contained_systems = get_contained_systems(target_obj)
        if contained_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not fillable by particles, so you can not fill anything in it.",
                {"target object": target_obj.name},
            )

        check_open_before_grasp(target_obj, self.env)

        # Check that the fluid source should be a particle producer
        produced_systems = get_produced_systems(fluid_source)
        if produced_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid source is not a particle producer, so you can not fill target object.",
                {"fluid source": fluid_source.name},
            )
        if not produced_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid source currently does not produce any particles, may be some conditions for producing particles not met, e.g., the fluid source should be toggled on.",
                {"fluid source": fluid_source.name}
            )
        
        # If so, fill the target object with all the particles from fluid source
        for system in produced_systems:
            target_obj.states[object_states.Filled].set_value(system, True)
            yield from self._settle_robot()

        # for system in produced_systems:
        #     if not target_obj.states[object_states.Contains].get_value(system):
        #         raise ActionPrimitiveError(
        #             ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
        #             "The container does not contain target particle, maybe the container fall over and the particles are scattered.",
        #             {"container": target_obj.name, "particle": system.name}
        #         )

    def _pour_into(self, fluid_container: StatefulObject, target_obj: StatefulObject):
        # Check that target object is fillable
        contained_systems = get_contained_systems(target_obj)
        if contained_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The target object is not fillable by particles, so you can not fill anything in it.",
                {"target object": target_obj.name},
            )

        check_open_before_grasp(target_obj, self.env)
        check_open_before_grasp(fluid_container, self.env)

        # Check that the fluid container contains particles
        contained_systems = get_contained_systems(fluid_container)
        if contained_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid container is not a particle container, so you can not fill target object.",
                {"fluid container": fluid_container.name},
            )
        if not contained_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The fluid container currently does not contain any particles.",
                {"fluid container": fluid_container.name}
            )

        # If so, fill the target object with all the particles from fluid source
        for system in contained_systems:
            target_obj.states[object_states.Filled].set_value(system, True)
            yield from self._settle_robot()

        # for system in contained_systems:
        #     if not target_obj.states[object_states.Contains].get_value(system):
        #         raise ActionPrimitiveError(
        #             ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
        #             "The container does not contain target particle, maybe the container fall over and the particles are scattered.",
        #             {"container": target_obj.name, "particle": system.name}
        #         )
    
    def _spread(self, fluid_container: StatefulObject, target_obj: StatefulObject):
        contained_systems = get_contained_systems(fluid_container)
        # check current object is a particle container
        if contained_systems is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The current container object is not fillable by particles, so you can not use it to spread",
                {"target object": fluid_container.name},
            )
        # Check if the current object has any particles in it
        if not contained_systems:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                "The current container object does not contain any particles.",
                {"target object": fluid_container.name},
            )

        check_open_before_grasp(fluid_container, self.env)
        check_open_before_grasp(target_obj, self.env)

        for system in contained_systems:
            if target_obj.prim_type != PrimType.CLOTH:
                target_obj.states[object_states.Covered].set_value(system, True)
            else: 
                target_obj.states[object_states.Saturated].set_value(system, True)
            yield from self._settle_robot()

    def _wait_for_cooked(self, target_obj: StatefulObject | BaseSystem):
        del target_obj
        yield from self._settle_robot()

    def _wait_for_washed(self, wash_machine: StatefulObject):
        del wash_machine
        yield from self._settle_robot()

    def _wait(self, target_obj: StatefulObject):
        del target_obj
        yield from self._settle_robot()
        
    def _wait_for_frozen(self, target_obj: StatefulObject, refrigerator_obj: StatefulObject):
        del target_obj, refrigerator_obj
        yield from self._settle_robot()
                        
