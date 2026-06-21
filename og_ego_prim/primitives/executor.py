import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Generator

import omnigibson as og
from omnigibson.action_primitives.action_primitive_set_base import (
    ActionPrimitiveError,
    ActionPrimitiveErrorGroup,
)
from omnigibson.action_primitives.starter_semantic_action_primitives import (
    StarterSemanticActionPrimitiveSet,
)
from omnigibson.action_primitives.symbolic_semantic_action_primitives import (
    SymbolicSemanticActionPrimitiveSet,
    SymbolicSemanticActionPrimitives,
)
from omnigibson.envs import Environment
from omnigibson.macros import gm
import torch

from .ego_primitives import (
    EgoSemanticActionPrimitiveSet, 
    EgoSemanticActionPrimitives,
)
from og_ego_prim.navigation import NavigationBackend
from .primitive_utils import find_task_related_object
from .specs import PrimitiveType, get_valid_primitives
from .starter_primitives import PhysicalStarterSemanticActionPrimitives


class BadExecutionPlanError(Exception):
    pass


@dataclass
class LowLevelStepContext:
    raw_plan: str
    primitive_name: str
    step_index: int
    action: torch.Tensor


@dataclass
class ParsedActionSequence:
    raw_plan: str
    primitive_name: str
    action_seqs: Generator[torch.Tensor, None, None]


PRIMITIVE_SET = {
    'ego': EgoSemanticActionPrimitiveSet,
    'starter': StarterSemanticActionPrimitiveSet,
    'symbolic': SymbolicSemanticActionPrimitiveSet,
}

PRIMITIVES = {
    'ego': EgoSemanticActionPrimitives,
    'starter': PhysicalStarterSemanticActionPrimitives,
    'symbolic': SymbolicSemanticActionPrimitives,
}


class Executor:

    def __init__(
        self, 
        env: Environment, 
        primitive_type: PrimitiveType = 'ego',
        verbose: bool = True,
        debug: bool = False,
        navigation_backend: Optional[NavigationBackend] = None,
        step_callback: Optional[Callable[[LowLevelStepContext], None]] = None,
    ):
        self.env = env
        self.verbose = verbose
        self.debug = debug
        self.step_callback = step_callback
        self.primitive_type = primitive_type
        self.valid_primitives = get_valid_primitives(primitive_type)
        self.last_execution_diagnostics: Optional[Dict[str, Any]] = None

        self.primitive_set = PRIMITIVE_SET[primitive_type]

        controller_kwargs = {}
        if primitive_type == 'starter':
            controller_kwargs.update(
                dict(
                    enable_head_tracking=False,
                    navigation_backend=navigation_backend,
                )
            )
        elif primitive_type == 'ego':
            controller_kwargs.update(dict(navigation_backend=navigation_backend))
        self.controller = PRIMITIVES[primitive_type](env, **controller_kwargs)

    def execute_plans(self, plans: List[str]):
        for plan in plans:
            self.execute_plan(plan)
        
    def execute_plan(self, plan: str):
        """
            plan format: OPERATOR(OBJ@DESCRIPTOR, ...)
            e.g., 
                grasp(vegetables@inside the refrigerator)
                close(regrigerator)
        """
        if self.verbose:
            print(f'[executor] -> executing {plan}')
            sys.stdout.flush()

        self.last_execution_diagnostics = {
            "plan": plan,
            "primitive_type": self.primitive_type,
            "status": "parsing",
        }

        if self.debug:
            debug_prompt = '[executor] Continue (y/Y)'
            if not gm.HEADLESS:
                debug_prompt += ' or Simulator (s/S)'
            print(f'{debug_prompt}: ')
            sys.stdout.flush()
             
            while cmd := input().upper() != "Y":
                if cmd == 'S':
                    if gm.HEADLESS:
                        print('[executor] Simulator (s/S) is not supported in HEADLESS mode.')
                        sys.stdout.flush()
                    else:   
                        self._simulator_loop()
                else:
                    print(f'{debug_prompt}: ')
                    sys.stdout.flush()

        try:
            parsed_action_seqs = self._parse_plan_to_action_seqs(plan)
        except Exception as exc:
            self.last_execution_diagnostics.update(
                status="parse_error",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            self._log_execution_error(plan, exc)
            raise
        if parsed_action_seqs is None:  # Done
            self.last_execution_diagnostics.update(status="done", low_level_steps=0)
            return
        
        try:
            self._execute(parsed_action_seqs)
        except Exception as e:
            self._log_execution_error(plan, e)
            if self.debug and gm.HEADLESS is False:
                print(f'[executor] catch error: {e}')
                sys.stdout.flush()
                self._simulator_loop()
            else:
                raise e
        
    def _execute(self, parsed_action_seqs: ParsedActionSequence):
        start_state = self._snapshot_robot_state()
        action_stats: Dict[str, Dict[str, float | int]] = {}
        low_level_steps = 0
        error = None

        print(
            f"[executor][diagnostic] begin primitive={parsed_action_seqs.primitive_name} "
            f"robot={start_state}"
        )
        sys.stdout.flush()

        try:
            for step_index, action in enumerate(parsed_action_seqs.action_seqs):
                low_level_steps = step_index + 1
                self._update_action_stats(action_stats, action)
                self.env.step(action)
                if self.step_callback is not None:
                    self.step_callback(
                        LowLevelStepContext(
                            raw_plan=parsed_action_seqs.raw_plan,
                            primitive_name=parsed_action_seqs.primitive_name,
                            step_index=step_index,
                            action=action,
                        )
                    )
        except Exception as exc:
            error = exc
            raise
        finally:
            end_state = self._snapshot_robot_state()
            diagnostics = {
                "plan": parsed_action_seqs.raw_plan,
                "primitive_type": self.primitive_type,
                "primitive_name": parsed_action_seqs.primitive_name,
                "status": "failed" if error is not None else "succeeded",
                "low_level_steps": low_level_steps,
                "start_state": start_state,
                "end_state": end_state,
                "base_displacement": self._distance(
                    start_state.get("base_position"),
                    end_state.get("base_position"),
                ),
                "eef_displacement": self._distance(
                    start_state.get("eef_position"),
                    end_state.get("eef_position"),
                ),
                "action_groups": action_stats,
            }
            navigation_result = self._snapshot_navigation_result()
            if navigation_result is not None:
                diagnostics["navigation"] = navigation_result
            if error is not None:
                diagnostics.update(
                    error_type=error.__class__.__name__,
                    error_message=str(error),
                )
            self.last_execution_diagnostics = diagnostics
            print(
                "[executor][diagnostic] end "
                f"status={diagnostics['status']} steps={low_level_steps} "
                f"base_displacement={diagnostics['base_displacement']} "
                f"eef_displacement={diagnostics['eef_displacement']} "
                f"object_in_hand={end_state.get('object_in_hand')} "
                f"navigation={navigation_result} "
                f"action_groups={action_stats}"
            )
            if error is not None:
                print(
                    "[executor][diagnostic] generator failed before completing "
                    f"{parsed_action_seqs.primitive_name}: "
                    f"{error.__class__.__name__}: {error}"
                )
            sys.stdout.flush()

    def _snapshot_robot_state(self) -> Dict[str, Any]:
        if not self.env.robots:
            return {}

        robot = self.env.robots[0]
        state: Dict[str, Any] = {}
        try:
            state["base_position"] = self._to_float_list(
                robot.get_position_orientation()[0]
            )
            state["base_orientation"] = self._to_float_list(
                robot.get_position_orientation()[1]
            )
        except Exception:
            state["base_position"] = None
            state["base_orientation"] = None

        arm = getattr(self.controller, "arm", getattr(robot, "default_arm", None))
        try:
            if arm is not None:
                state["eef_position"] = self._to_float_list(
                    robot.eef_links[arm].get_position_orientation()[0]
                )
        except Exception:
            state["eef_position"] = None

        try:
            obj_in_hand = self.controller._get_obj_in_hand()
            state["object_in_hand"] = None if obj_in_hand is None else obj_in_hand.name
        except Exception:
            state["object_in_hand"] = None
        return state

    def _snapshot_navigation_result(self) -> Optional[Dict[str, Any]]:
        navigation_backend = getattr(self.controller, "navigation_backend", None)
        result = getattr(navigation_backend, "last_navigation_result", None)
        if result is None:
            return None
        return dict(result)

    def _update_action_stats(
        self,
        action_stats: Dict[str, Dict[str, float | int]],
        action: torch.Tensor,
    ):
        if not self.env.robots:
            return

        robot = self.env.robots[0]
        for controller_name, action_idx in robot.controller_action_idx.items():
            try:
                values = torch.as_tensor(action[action_idx]).detach()
                max_abs = float(values.abs().max().item()) if values.numel() else 0.0
            except Exception:
                continue

            stats = action_stats.setdefault(
                controller_name,
                {"active_steps": 0, "max_abs_command": 0.0},
            )
            if max_abs > 1e-6:
                stats["active_steps"] += 1
            stats["max_abs_command"] = max(stats["max_abs_command"], max_abs)

    @staticmethod
    def _to_float_list(value) -> Optional[List[float]]:
        if value is None:
            return None
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "tolist"):
            value = value.tolist()
        return [round(float(item), 6) for item in value]

    @staticmethod
    def _distance(
        start: Optional[List[float]],
        end: Optional[List[float]],
    ) -> Optional[float]:
        if start is None or end is None or len(start) != len(end):
            return None
        return round(sum((a - b) ** 2 for a, b in zip(start, end)) ** 0.5, 6)

    @staticmethod
    def _log_execution_error(plan: str, error: Exception):
        print(
            f"[executor][error] plan={plan!r} "
            f"type={error.__class__.__name__} message={error}"
        )
        sys.stdout.flush()

    def _parse_plan_to_action_seqs(self, plan: str) -> Optional[ParsedActionSequence]:
        pattern = r'([\w\W_]+)\((.*)\)'
        result = re.search(pattern, plan.strip())
        if result is None:
            raise BadExecutionPlanError(f'invalid plan "{plan}", expected "OPERATOR(OBJ@DESCRIPTOR)"')        
        operator, params = result.group(1).lower(), result.group(2).lower()

        if operator == 'done':
            return None
        
        if operator.upper() not in self.primitive_set._member_names_:
            raise BadExecutionPlanError(f'invalid operator "{operator}", expected {self.primitive_set._member_names_}')
        primitive = self.primitive_set._member_map_[operator.upper()]

        primitive_params = [] if not params.strip() else [param.strip() for param in params.split(',')]
        if len(primitive_params) != self.valid_primitives[operator.upper()]:
            raise BadExecutionPlanError(f'invalid params "{params}" for operator "{operator}"')

        object_refs = []
        for prim_param in primitive_params:
            if '@' in prim_param:
                obj, _ = prim_param.strip().split('@')
            else:
                obj = prim_param
            
            obj_ref = find_task_related_object(self.env, obj.strip())
            if obj_ref is None:
                raise BadExecutionPlanError(
                    f'cannot resolve task object "{obj.strip()}" in plan "{plan}"'
                )
            object_refs.append(obj_ref)

        if self.verbose:
            resolved_objects = [
                {
                    "name": obj.name,
                    "position": self._to_float_list(obj.get_position_orientation()[0]),
                    "in_rooms": list(getattr(obj, "in_rooms", []) or []),
                }
                for obj in object_refs
            ]
            print(f"[executor][diagnostic] resolved_objects={resolved_objects}")
            sys.stdout.flush()

        try:
            action_seqs = self.controller.apply_ref(primitive, *object_refs)
        except TypeError:
            raise BadExecutionPlanError(f'invalid params "{params}" for operator "{operator}"')

        return ParsedActionSequence(
            raw_plan=plan,
            primitive_name=operator.upper(),
            action_seqs=action_seqs,
        )
            
    def _simulator_loop(self, interval=None):
        if interval is not None and isinstance(interval, int) and interval > 0:
            for _ in range(interval):
                self.env.step(self.get_hold_action())
        else:
            while True:
                self.env.step(self.get_hold_action())

    def get_hold_action(self) -> torch.Tensor:
        """Return a real no-op action that holds the robot's current state.

        A zero vector is only a no-op for delta / velocity controllers.  Fetch's
        primitive config uses absolute position JointControllers, where zeros
        command every controlled joint toward position zero.  Observation
        capture advances physics between camera poses, so it must ask each
        controller for its own no-op command instead of sending raw zeros.
        """
        if not self.env.robots:
            return torch.empty(0)

        robot = self.env.robots[0]
        action = torch.zeros(robot.action_dim, dtype=torch.float32)
        control_dict = robot.get_control_dict()
        controllers = getattr(robot, "controllers", None)
        if controllers is None:
            controllers = robot._controllers

        for controller_name, controller in controllers.items():
            action_idx = robot.controller_action_idx[controller_name]
            action[action_idx] = controller.compute_no_op_action(control_dict)

        return action

    def snapshot_passive_motion_state(self) -> Dict[str, Any]:
        """Capture robot state used to audit non-task simulation phases."""
        state = self._snapshot_robot_state()
        if not self.env.robots:
            state["joint_positions"] = None
            return state

        try:
            robot = self.env.robots[0]
            non_base_dof_indices = []
            for controller_name, controller in robot.controllers.items():
                if controller_name == "base":
                    continue
                controller_indices = controller.dof_idx
                if hasattr(controller_indices, "tolist"):
                    controller_indices = controller_indices.tolist()
                non_base_dof_indices.extend(
                    int(index) for index in controller_indices
                )
            state["joint_positions"] = self._to_float_list(
                robot.get_joint_positions()[non_base_dof_indices]
            )
        except Exception:
            state["joint_positions"] = None
        return state

    def log_passive_motion_diagnostic(
        self,
        phase: str,
        start_state: Dict[str, Any],
        simulation_steps: int,
    ) -> Dict[str, Any]:
        """Report whether a no-op phase moved the robot unexpectedly."""
        end_state = self.snapshot_passive_motion_state()
        base_displacement = self._distance(
            start_state.get("base_position"), end_state.get("base_position")
        )
        eef_displacement = self._distance(
            start_state.get("eef_position"), end_state.get("eef_position")
        )
        max_joint_displacement = self._max_abs_difference(
            start_state.get("joint_positions"), end_state.get("joint_positions")
        )
        object_before = start_state.get("object_in_hand")
        object_after = end_state.get("object_in_hand")
        unexpected_motion = any(
            (
                base_displacement is not None and base_displacement > 0.005,
                eef_displacement is not None and eef_displacement > 0.01,
                max_joint_displacement is not None and max_joint_displacement > 0.02,
                object_before != object_after,
            )
        )
        diagnostic = {
            "phase": phase,
            "action_source": "controller_no_op",
            "task_action_generated": False,
            "simulation_steps": simulation_steps,
            "base_displacement": base_displacement,
            "eef_displacement": eef_displacement,
            "max_joint_displacement": max_joint_displacement,
            "object_in_hand_before": object_before,
            "object_in_hand_after": object_after,
            "unexpected_motion": unexpected_motion,
        }
        print(
            "[executor][between-actions] "
            f"phase={phase} action_source=controller_no_op "
            f"task_action_generated=False simulation_steps={simulation_steps} "
            f"base_displacement={base_displacement} "
            f"eef_displacement={eef_displacement} "
            f"max_joint_displacement={max_joint_displacement} "
            f"object_in_hand={object_before}->{object_after} "
            f"unexpected_motion={unexpected_motion}"
        )
        sys.stdout.flush()
        return diagnostic

    @staticmethod
    def _max_abs_difference(
        start: Optional[List[float]],
        end: Optional[List[float]],
    ) -> Optional[float]:
        if start is None or end is None or len(start) != len(end):
            return None
        if not start:
            return 0.0
        return round(max(abs(a - b) for a, b in zip(start, end)), 6)
