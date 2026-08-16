import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Generator, Sequence, Tuple

import omnigibson as og
from omnigibson import object_states
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
from omnigibson.systems import BaseSystem
import omnigibson.utils.transform_utils as T
import torch

from og_ego_prim.config.runtime_config import RuntimeConfig
from og_ego_prim.scheduler import ProcessStart
from .ego_primitives import (
    EgoSemanticActionPrimitiveSet, 
    EgoSemanticActionPrimitives,
)
from og_ego_prim.navigation import NavigationBackend
from .primitive_utils import find_task_related_object
from .object_states_utils import (
    check_heat_source_before_cook,
    get_container,
    get_contained_systems,
    get_cooked_system,
    get_covered_systems,
    get_placement_objects,
    get_produced_systems,
)
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
    global_step_index: int = 0


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


TEMPORAL_WAIT_PRIMITIVES = frozenset(
    {"WAIT", "WAIT_FOR_COOKED", "WAIT_FOR_WASHED", "WAIT_FOR_FROZEN"}
)


class Executor:
    '''
    IS-Bench 的高层动作执行器。

    关键职责：
    1. 接收形如 "navigate_to(apple)" 的高层 primitive plan。
    2. 将 plan 解析成 OmniGibson / IS-Bench 的 primitive enum 和 simulator object 引用。
    3. 调用 self.controller.apply_ref(...) 生成低层 action tensor 序列。
    4. 逐步执行 env.step(action)，并在每个 low-level step 后触发 step_callback。

    使用示例：
        executor = Executor(env, primitive_type="ego")
        executor.execute_plan("navigate_to(apple)")
        executor.execute_plan("grasp(apple)")
    '''

    def __init__(
        self, 
        env: Environment, 
        primitive_type: PrimitiveType = 'ego',
        verbose: bool = True,
        debug: bool = False,
        navigation_backend: Optional[NavigationBackend] = None,
        step_callback: Optional[Callable[[LowLevelStepContext], None]] = None,
        runtime_config: Optional[RuntimeConfig] = None,
    ):
        '''
        初始化 Executor，并根据 primitive_type 创建对应的 primitive controller。

        controller 的作用是把高层语义动作转换成低层 action 序列：
        - primitive_type="ego" 时使用 EgoSemanticActionPrimitives。
        - primitive_type="starter" 时使用 PhysicalStarterSemanticActionPrimitives。
        - primitive_type="symbolic" 时使用 OmniGibson 的 SymbolicSemanticActionPrimitives。

        使用示例：
            executor = Executor(
                env,
                primitive_type="ego",
                step_callback=on_low_level_step,
            )
        '''
        self.env = env
        self.verbose = verbose
        self.debug = debug
        self.step_callback = step_callback
        self.primitive_type = primitive_type
        self.runtime_config = runtime_config or RuntimeConfig.defaults()
        self.valid_primitives = get_valid_primitives(primitive_type)
        self.last_execution_diagnostics: Optional[Dict[str, Any]] = None
        # The runtime clock origin is Executor construction, after environment
        # creation. It never resets at a primitive or subtask boundary; viewer,
        # camera, initialization, action, and temporal-settle frames all share
        # this counter once the Executor exists.
        self.global_step_index = 0
        self.temporal_wait_steps = int(
            self.runtime_config.scheduler.handler_options.get("wait_action_steps", 60)
        )
        if self.temporal_wait_steps <= 0:
            raise ValueError("scheduler.handler_options.wait_action_steps must be positive")
        self.semantic_wait_completes_matching_timer = bool(
            self.runtime_config.scheduler.handler_options.get(
                "semantic_wait_completes_matching_timer",
                False,
            )
        )
        self._active_cooked_particle_expectations: Dict[
            Tuple[int, str], Dict[str, Any]
        ] = {}
        self._cooked_particle_payloads: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._has_pending_heating_process: Optional[Callable[[Any], bool]] = None

        self.primitive_set = PRIMITIVE_SET[primitive_type]

        controller_kwargs = {}
        if primitive_type == 'starter':
            controller_kwargs.update(
                dict(
                    enable_head_tracking=False,
                    navigation_backend=navigation_backend,
                    starter_config=self.runtime_config.starter_primitives,
                    navigation_config=self.runtime_config.navigation,
                )
            )
        elif primitive_type == 'ego':
            controller_kwargs.update(
                dict(
                    navigation_backend=navigation_backend,
                    navigation_config=self.runtime_config.navigation,
                    starter_config=self.runtime_config.starter_primitives,
                )
            )
        self.controller = PRIMITIVES[primitive_type](env, **controller_kwargs)

    def end_episode(self):
        """Delegate controller-owned runtime cleanup at an episode boundary."""
        cleanup = getattr(self.controller, "end_episode", None)
        if cleanup is None:
            return {"supported": False}
        report = cleanup()
        if isinstance(report, Mapping):
            return dict(report)
        return {"supported": True, "result": report}

    def set_pending_heating_process_lookup(
        self,
        lookup: Optional[Callable[[Any], bool]],
    ) -> None:
        """Bind runtime scheduler state for checkpoint-resilient cook waits."""

        self._has_pending_heating_process = lookup

    def close(self):
        return self.end_episode()

    def execute_plans(self, plans: List[str]):
        '''
        顺序执行多个高层 plan。

        每个 plan 都会交给 execute_plan(...) 单独解析和执行；如果中途某个 plan 抛出异常，
        后续 plan 不会继续执行，异常会向上传递给调用者。

        使用示例：
            executor.execute_plans([
                "navigate_to(apple)",
                "grasp(apple)",
                "navigate_to(cabinet)",
                "place_inside(apple, cabinet)",
            ])
        '''
        for plan in plans:
            self.execute_plan(plan)

    def execute(self, action: Any):
        """Typed ActionExecutor compatibility for the modular runtime."""
        plan = action.to_legacy_plan() if hasattr(action, 'to_legacy_plan') else str(action)
        self.execute_plan(plan)
        return self.last_execution_diagnostics

    @staticmethod
    def _exception_metadata(exc: Exception) -> dict:
        """Extract structured metadata from an exception, when available.

        Starter primitives raise ``ActionPrimitiveError(reason, message,
        metadata)``; the metadata dict carries actionable signals such as
        ``target_distance``, ``base_alignment_steps``, ``base_yaw_change``,
        ``phase``, etc.  Keeping it as a first-class diagnostics field lets the
        planner distill a corrective hint without re-parsing the flattened
        ``error_message`` string.
        """
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, dict):
            return dict(metadata)
        return {}

    def execute_plan(self, plan: str):
        '''
        执行单条高层 primitive plan。

        这里会先把字符串 plan 解析成 primitive 和目标对象引用，再调用 _execute(...)
        执行 controller 生成的低层 action 序列。执行完成或失败后，会把诊断信息写入
        self.last_execution_diagnostics。

        plan 格式：
            OPERATOR(OBJ@DESCRIPTOR, ...)

        使用示例：
            executor.execute_plan("grasp(vegetables@inside the refrigerator)")
            executor.execute_plan("close(refrigerator)")
            executor.execute_plan("done()")
        '''
        if self.verbose:
            print(f'[executor] -> executing {plan}')
            sys.stdout.flush()

        self.last_execution_diagnostics = {
            "plan": plan,
            "primitive_type": self.primitive_type, # ego, starter, symbolic
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
                metadata=self._exception_metadata(exc),
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
        '''
        执行已经解析好的低层 action 序列。

        parsed_action_seqs.action_seqs 是一个 generator，会不断 yield action tensor。
        本函数逐个调用 env.step(action)，并在每个 low-level step 后触发 step_callback，
        因此 scene graph 的 low-level step 更新也是从这里被触发的。

        内部调用示例：
            parsed = self._parse_plan_to_action_seqs("navigate_to(apple)")
            self._execute(parsed)
        '''
        start_state = self._snapshot_robot_state()
        global_step_start = self.global_step_index
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
                self._step_environment(
                    action,
                    raw_plan=parsed_action_seqs.raw_plan,
                    primitive_name=parsed_action_seqs.primitive_name,
                    step_index=step_index,
                )
            self._synchronize_cooked_particle_payloads()
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
                "global_step_end": self.global_step_index,
                "global_step_start": global_step_start,
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
                    metadata=self._exception_metadata(error),
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

    def _step_environment(
        self,
        action: torch.Tensor,
        *,
        raw_plan: str,
        primitive_name: str,
        step_index: int,
    ) -> None:
        """Execute one simulator frame and update the single global clock."""
        self.env.step(action)
        self.global_step_index += 1
        if self.step_callback is not None:
            self.step_callback(
                LowLevelStepContext(
                    raw_plan=raw_plan,
                    primitive_name=primitive_name,
                    step_index=step_index,
                    action=action,
                    global_step_index=self.global_step_index,
                )
            )

    def _snapshot_robot_state(self) -> Dict[str, Any]:
        '''
        截取当前机器人状态，用于执行前后诊断。

        当前记录的信息包括机器人 base 位置/朝向、末端执行器位置，以及当前手中物体。
        这些信息会被 _execute(...) 用来计算 base/eef 位移和执行诊断。

        内部调用示例：
            start_state = self._snapshot_robot_state()
        '''
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
            if obj_in_hand is not None:
                obj_pos, obj_orn = obj_in_hand.get_position_orientation()
                state["object_in_hand_position"] = self._to_float_list(obj_pos)
                state["object_in_hand_orientation"] = self._to_float_list(obj_orn)
        except Exception:
            state["object_in_hand"] = None
        return state

    def _snapshot_navigation_result(self) -> Optional[Dict[str, Any]]:
        '''
        读取 navigation backend 最近一次导航结果。

        如果当前 controller 没有 navigation_backend，或者 backend 还没有产生导航结果，
        则返回 None。该信息会被 _execute(...) 追加到 last_execution_diagnostics 中。

        内部调用示例：
            navigation_result = self._snapshot_navigation_result()
        '''
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
        '''
        统计当前低层 action 中各个 robot controller 的活跃情况。

        它会根据 robot.controller_action_idx 把 action 切分到不同 controller，
        记录每个 controller 有多少 step 发出了非零命令，以及最大命令幅度。
        这些统计会写入 _execute(...) 的 action_groups 诊断字段。

        内部调用示例：
            action_stats = {}
            self._update_action_stats(action_stats, action)
        '''
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
        '''
        将 tensor / numpy array / list 等数值序列转换成普通 float list。

        该函数主要用于把机器人位置、朝向、关节值等转成可 JSON 序列化的诊断信息，
        并统一保留 6 位小数。

        使用示例：
            position = Executor._to_float_list(robot.get_position_orientation()[0])
        '''
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
        '''
        计算两个等长向量之间的欧氏距离。

        如果输入为空或长度不一致，则返回 None。主要用于执行诊断中的 base_displacement
        和 eef_displacement。

        使用示例：
            distance = Executor._distance([0, 0, 0], [1, 0, 0])
        '''
        if start is None or end is None or len(start) != len(end):
            return None
        return round(sum((a - b) ** 2 for a, b in zip(start, end)) ** 0.5, 6)

    @staticmethod
    def _log_execution_error(plan: str, error: Exception):
        '''
        打印 plan 执行或解析失败时的错误信息。

        该函数只负责日志输出，不会吞掉异常；调用方仍然会继续 raise 原始异常。

        内部调用示例：
            self._log_execution_error(plan, exc)
        '''
        print(
            f"[executor][error] plan={plan!r} "
            f"type={error.__class__.__name__} message={error}"
        )
        sys.stdout.flush()

    def _parse_plan_to_action_seqs(self, plan: str) -> Optional[ParsedActionSequence]:
        '''
        将字符串形式的高层 plan 解析成可执行的低层 action generator。

        主要步骤：
        1. 解析 OPERATOR(...) 格式，得到 operator 和参数。
        2. 检查 operator 是否属于当前 primitive_type 支持的 primitive。
        3. 根据任务 object_scope 将对象名解析成 simulator object 引用。
        4. 调用 self.controller.apply_ref(...) 生成低层 action 序列。

        返回值：
        - ParsedActionSequence：可交给 _execute(...) 执行。
        - None：表示 plan 是 done()，无需执行动作。

        内部调用示例：
            parsed = self._parse_plan_to_action_seqs("navigate_to(apple)")
            if parsed is not None:
                self._execute(parsed)
        '''
        pattern = r'([\w\W_]+)\((.*)\)'
        result = re.search(pattern, plan.strip())
        if result is None:
            raise BadExecutionPlanError(f'invalid plan "{plan}", expected "OPERATOR(OBJ@DESCRIPTOR)"')        
        operator, params = result.group(1).lower(), result.group(2).lower()

        if operator == 'done':
            return None

        if (
            self.primitive_type in {'starter', 'symbolic'}
            and operator.upper() in TEMPORAL_WAIT_PRIMITIVES
        ):
            primitive_params = [] if not params.strip() else [param.strip() for param in params.split(',')]
            expected_params = {
                "WAIT": 1,
                "WAIT_FOR_COOKED": 1,
                "WAIT_FOR_WASHED": 1,
                "WAIT_FOR_FROZEN": 2,
            }[operator.upper()]
            if len(primitive_params) != expected_params:
                raise BadExecutionPlanError(f'invalid params "{params}" for operator "{operator}"')
            object_refs = []
            for primitive_param in primitive_params:
                target_obj = find_task_related_object(self.env, primitive_param.strip())
                if target_obj is None:
                    raise BadExecutionPlanError(
                        f'cannot resolve task object "{primitive_param.strip()}" in plan "{plan}"'
                    )
                object_refs.append(target_obj)
            return ParsedActionSequence(
                raw_plan=plan,
                primitive_name=operator.upper(),
                action_seqs=self._temporal_wait_action_seq(
                    operator.upper(),
                    tuple(object_refs),
                ),
            )

        if self.primitive_type == 'starter' and operator in {
            'wipe',
            'toggle_on',
            'toggle_off',
            'pour_into',
            'dump_into',
        }:
            primitive_params = [] if not params.strip() else [param.strip() for param in params.split(',')]
            expected_params = self.valid_primitives[operator.upper()]
            if len(primitive_params) != expected_params:
                raise BadExecutionPlanError(f'invalid params "{params}" for operator "{operator}"')
            target_obj = find_task_related_object(self.env, primitive_params[0].strip())
            if target_obj is None:
                raise BadExecutionPlanError(
                    f'cannot resolve task object "{primitive_params[0].strip()}" in plan "{plan}"'
                )
            if operator == 'wipe':
                action_seqs = self._starter_wipe_action_seq(target_obj)
            elif operator == 'pour_into':
                action_seqs = self._starter_pour_into_action_seq(target_obj)
            elif operator == 'dump_into':
                action_seqs = self._starter_dump_into_action_seq(target_obj)
            else:
                action_seqs = self._starter_toggle_action_seq(
                    target_obj,
                    value=(operator == 'toggle_on'),
                )
            return ParsedActionSequence(
                raw_plan=plan,
                primitive_name=operator.upper(),
                action_seqs=action_seqs,
            )

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

        if operator.upper() in TEMPORAL_WAIT_PRIMITIVES:
            action_seqs = self._temporal_wait_action_seq(
                operator.upper(),
                tuple(object_refs),
            )
        else:
            try:
                action_seqs = self.controller.apply_ref(primitive, *object_refs)
            except TypeError:
                raise BadExecutionPlanError(f'invalid params "{params}" for operator "{operator}"')

        return ParsedActionSequence(
            raw_plan=plan,
            primitive_name=operator.upper(),
            action_seqs=action_seqs,
        )

    def _temporal_wait_action_seq(
        self,
        primitive_name: str,
        object_refs: Sequence[Any],
    ):
        """Advance simulator time without applying symbolic state side effects."""
        self._validate_temporal_wait(primitive_name, object_refs)
        primitive_name = str(primitive_name).upper()
        previous_expectations = self._active_cooked_particle_expectations
        self._active_cooked_particle_expectations = (
            self._capture_cooked_particle_expectations(object_refs[0])
            if primitive_name == "WAIT_FOR_COOKED"
            else {}
        )
        try:
            wait_steps = (
                1
                if primitive_name == "WAIT" and self.semantic_wait_completes_matching_timer
                else self.temporal_wait_steps
            )
            for _ in range(wait_steps):
                yield self.get_hold_action()
            if primitive_name == "WAIT_FOR_COOKED":
                readiness = self._cooked_or_heated_readiness(object_refs[0])
                particles_ready = self._cooked_particle_expectations_met(
                    self._active_cooked_particle_expectations
                )
                if readiness is not True or particles_ready is False:
                    # The scheduler sets Heated after the final duration frame.
                    # Native transition rules consume that state on the next
                    # env.step, so allow exactly one transition-settle frame.
                    yield self.get_hold_action()
                    readiness = self._cooked_or_heated_readiness(object_refs[0])
                    particles_ready = self._cooked_particle_expectations_met(
                        self._active_cooked_particle_expectations
                    )
                if readiness is not True or particles_ready is False:
                    raise BadExecutionPlanError(
                        "WAIT_FOR_COOKED elapsed without a complete verified "
                        "cooked or heated state"
                    )
                self._capture_cooked_particle_payloads(
                    self._active_cooked_particle_expectations
                )
        finally:
            self._active_cooked_particle_expectations = previous_expectations

    def _capture_cooked_particle_expectations(
        self,
        obj: Any,
    ) -> Dict[Tuple[int, str], Dict[str, Any]]:
        """Snapshot the complete convertible payload present when cooking starts."""
        container = (
            get_container(obj, self.env) if isinstance(obj, BaseSystem) else obj
        )
        contained_state = getattr(container, "states", {}).get(
            object_states.ContainedParticles
        )
        if container is None or contained_state is None:
            return {}

        if isinstance(obj, BaseSystem):
            systems = (obj,)
        else:
            registry = getattr(
                getattr(container, "scene", None),
                "system_registry",
                None,
            )
            systems = tuple(
                system
                for system in getattr(registry, "objects", ())
                if getattr(system, "is_fluid", False)
                and self._cooked_system_available(system.name)
            )

        expectations = {}
        for system in systems:
            cooked_system = get_cooked_system(f"cooked__{system.name}", self.env)
            if cooked_system is None:
                continue
            contained_state.clear_cache()
            raw_count = int(contained_state.get_value(system).n_in_volume)
            contained_state.clear_cache()
            cooked_count = int(
                contained_state.get_value(cooked_system).n_in_volume
            )
            if raw_count <= 0:
                continue
            expectations[(id(container), system.name)] = {
                "container": container,
                "system": system,
                "cooked_system": cooked_system,
                "raw_count": raw_count,
                "initial_cooked_count": cooked_count,
            }
        return expectations

    @staticmethod
    def _cooked_particle_expectations_met(
        expectations: Mapping[Tuple[int, str], Mapping[str, Any]],
    ) -> Optional[bool]:
        if not expectations:
            return None

        complete = True
        for expectation in expectations.values():
            container = expectation["container"]
            system = expectation["system"]
            cooked_system = expectation["cooked_system"]
            contained_state = container.states[object_states.ContainedParticles]
            contained_state.clear_cache()
            raw_count = int(contained_state.get_value(system).n_in_volume)
            contained_state.clear_cache()
            cooked_count = int(
                contained_state.get_value(cooked_system).n_in_volume
            )
            expected_cooked_count = int(expectation["raw_count"]) + int(
                expectation["initial_cooked_count"]
            )
            item_complete = (
                raw_count == 0 and cooked_count == expected_cooked_count
            )
            complete = complete and item_complete
            print(
                "[executor][temporal][particle_postcondition] "
                f"source={system.name} target={cooked_system.name} "
                f"container={getattr(container, 'name', None)} "
                f"raw_in_container={raw_count} cooked_in_container={cooked_count} "
                f"expected_cooked_in_container={expected_cooked_count} "
                f"complete={item_complete}"
            )
            sys.stdout.flush()
        return complete

    def _task_entity_id_for_object(self, obj: Any) -> Optional[str]:
        for entity_id, reference in (
            getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        ).items():
            if getattr(reference, "wrapped_obj", None) is obj:
                return str(entity_id)
        return None

    @staticmethod
    def _world_points_to_local_frame(world_points, frame_pos, frame_orn):
        points = torch.as_tensor(world_points)
        rotation = T.quat2mat(torch.as_tensor(frame_orn, dtype=points.dtype))
        position = torch.as_tensor(frame_pos, dtype=points.dtype, device=points.device)
        return ((points - position) @ rotation).clone()

    @staticmethod
    def _local_points_to_world_frame(local_points, frame_pos, frame_orn):
        points = torch.as_tensor(local_points)
        rotation = T.quat2mat(torch.as_tensor(frame_orn, dtype=points.dtype))
        position = torch.as_tensor(frame_pos, dtype=points.dtype, device=points.device)
        return points @ rotation.T + position

    def _capture_cooked_particle_payloads(
        self,
        expectations: Mapping[Tuple[int, str], Mapping[str, Any]],
    ) -> None:
        for expectation in expectations.values():
            container = expectation["container"]
            entity_id = self._task_entity_id_for_object(container)
            if entity_id is None:
                raise BadExecutionPlanError(
                    "cooked particle payload container is not a task entity"
                )
            system = expectation["cooked_system"]
            contained_state = container.states[object_states.ContainedParticles]
            contained_state.clear_cache()
            contained = contained_state.get_value(system)
            indices = torch.nonzero(contained.in_volume, as_tuple=False).flatten()
            expected_count = int(expectation["raw_count"]) + int(
                expectation["initial_cooked_count"]
            )
            if int(indices.numel()) != expected_count:
                raise BadExecutionPlanError(
                    "cooked particle payload was incomplete after native conversion: "
                    f"container={container.name} system={system.name} "
                    f"expected={expected_count} actual={int(indices.numel())}"
                )
            instancer = getattr(system, "default_particle_instancer", None)
            if instancer is None:
                raise BadExecutionPlanError(
                    f"cooked particle system {system.name} has no particle instancer"
                )
            container_pos, container_orn = container.get_position_orientation()
            self._cooked_particle_payloads[(entity_id, system.name)] = {
                "entity_id": entity_id,
                "system_name": system.name,
                "local_positions": self._world_points_to_local_frame(
                    contained.positions[indices], container_pos, container_orn
                ),
                "velocities": instancer.particle_velocities[indices].clone(),
                "orientations": instancer.particle_orientations[indices].clone(),
                "scales": instancer.particle_scales[indices].clone(),
                "prototype_indices": instancer.particle_prototype_ids[indices].clone(),
                "instancer_idn": int(getattr(instancer, "idn", 0)),
                "particle_group": int(getattr(instancer, "particle_group", 0)),
            }
            print(
                "[executor][temporal][particle_payload] "
                f"captured container={container.name} system={system.name} "
                f"count={expected_count}"
            )
            sys.stdout.flush()

    def cooked_particle_payload_checkpoint(self) -> list[Dict[str, Any]]:
        return [
            {
                **{
                    key: value.detach().cpu().clone()
                    if isinstance(value, torch.Tensor)
                    else value
                    for key, value in payload.items()
                }
            }
            for _, payload in sorted(self._cooked_particle_payloads.items())
        ]

    def restore_cooked_particle_payloads(self, payloads: Sequence[Mapping[str, Any]]) -> None:
        restored = {}
        scope = getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        for payload in payloads:
            entity_id = str(payload.get("entity_id") or "").strip()
            system_name = str(payload.get("system_name") or "").strip()
            if not entity_id or not system_name:
                raise ValueError("cooked particle payload checkpoint is missing identity")
            if entity_id not in scope:
                raise ValueError(
                    f"cooked particle payload entity is absent from task scope: {entity_id}"
                )
            restored[(entity_id, system_name)] = {
                "entity_id": entity_id,
                "system_name": system_name,
                "local_positions": torch.as_tensor(
                    payload["local_positions"], dtype=torch.float32
                ).clone(),
                "velocities": torch.as_tensor(payload["velocities"], dtype=torch.float32).clone(),
                "orientations": torch.as_tensor(
                    payload["orientations"], dtype=torch.float32
                ).clone(),
                "scales": torch.as_tensor(payload["scales"], dtype=torch.float32).clone(),
                "prototype_indices": torch.as_tensor(
                    payload["prototype_indices"], dtype=torch.long
                ).clone(),
                "instancer_idn": int(payload.get("instancer_idn", 0)),
                "particle_group": int(payload.get("particle_group", 0)),
            }
        self._cooked_particle_payloads = restored

    def recover_cooked_particle_payloads_from_live_state(self) -> list[Dict[str, Any]]:
        expectations = {}
        for entity_id, reference in (
            getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        ).items():
            container = getattr(reference, "wrapped_obj", None)
            contained_state = getattr(container, "states", {}).get(
                object_states.ContainedParticles
            )
            if contained_state is None:
                continue
            for system in getattr(container.scene.system_registry, "objects", ()):
                if not str(system.name).startswith("cooked__"):
                    continue
                contained_state.clear_cache()
                cooked_count = int(contained_state.get_value(system).n_in_volume)
                if cooked_count <= 0:
                    continue
                expectations[(id(container), system.name)] = {
                    "container": container,
                    "system": system,
                    "cooked_system": system,
                    "raw_count": cooked_count,
                    "initial_cooked_count": 0,
                }
        self._capture_cooked_particle_payloads(expectations)
        return self.cooked_particle_payload_checkpoint()

    def _payload_is_suspended_by_symbolic_carry(self, container: Any, system_name: str) -> bool:
        getter = getattr(self.controller, "_symbolic_carried_particle_states", None)
        if not callable(getter):
            return False
        return any(
            str(state["system"].name) == system_name and bool(state.get("suspended"))
            for state in getter(container)
        )

    def _synchronize_cooked_particle_payloads(self) -> None:
        if not self._cooked_particle_payloads:
            return
        scope = getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        payloads_by_system = {}
        for payload in self._cooked_particle_payloads.values():
            payloads_by_system.setdefault(payload["system_name"], []).append(payload)
        changed = False
        for payload in self._cooked_particle_payloads.values():
            container = getattr(scope.get(payload["entity_id"]), "wrapped_obj", None)
            if container is None:
                raise RuntimeError(
                    f"cooked particle payload lost task entity {payload['entity_id']}"
                )
            if self._payload_is_suspended_by_symbolic_carry(
                container, payload["system_name"]
            ):
                continue
            system = container.scene.get_system(payload["system_name"], force_init=True)
            contained_state = container.states[object_states.ContainedParticles]
            contained_state.clear_cache()
            contained = contained_state.get_value(system)
            actual_count = int(contained.n_in_volume)
            expected_count = int(payload["local_positions"].shape[0])
            if actual_count == expected_count and int(system.n_particles) == expected_count:
                continue
            if len(payloads_by_system[payload["system_name"]]) == 1:
                remove_indices = torch.arange(int(system.n_particles), dtype=torch.long)
            else:
                remove_indices = torch.nonzero(
                    contained.in_volume, as_tuple=False
                ).flatten()
            if remove_indices.numel():
                system.remove_particles(idxs=remove_indices)
            container_pos, container_orn = container.get_position_orientation()
            positions = self._local_points_to_world_frame(
                payload["local_positions"], container_pos, container_orn
            )
            system.generate_particles(
                positions=positions,
                instancer_idn=payload["instancer_idn"],
                velocities=torch.zeros_like(positions),
                orientations=payload["orientations"],
                scales=payload["scales"],
                prototype_indices=payload["prototype_indices"].tolist(),
                particle_group=payload["particle_group"],
            )
            changed = True
            print(
                "[executor][temporal][particle_payload] "
                f"restored container={container.name} system={system.name} "
                f"expected={expected_count} previous={actual_count}"
            )
            sys.stdout.flush()
        if changed:
            og.sim.update_handles()

    def cooked_particle_payload_diagnostics(self) -> list[Dict[str, Any]]:
        self._synchronize_cooked_particle_payloads()
        scope = getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        diagnostics = []
        for payload in self._cooked_particle_payloads.values():
            container = getattr(scope.get(payload["entity_id"]), "wrapped_obj", None)
            system = container.scene.get_system(payload["system_name"], force_init=True)
            suspended = self._payload_is_suspended_by_symbolic_carry(
                container, payload["system_name"]
            )
            contained_state = container.states[object_states.ContainedParticles]
            contained_state.clear_cache()
            contained_count = int(contained_state.get_value(system).n_in_volume)
            diagnostics.append(
                {
                    "entity_id": payload["entity_id"],
                    "system_name": payload["system_name"],
                    "expected_count": int(payload["local_positions"].shape[0]),
                    "contained_count": contained_count,
                    "system_count": int(system.n_particles),
                    "suspended_by_symbolic_carry": suspended,
                    "contained": suspended or contained_count
                    == int(payload["local_positions"].shape[0]),
                }
            )
        return diagnostics

    def _validate_temporal_wait(
        self,
        primitive_name: str,
        object_refs: Sequence[Any],
    ) -> None:
        primitive_name = str(primitive_name).upper()
        if primitive_name == "WAIT_FOR_COOKED":
            if len(object_refs) != 1:
                raise BadExecutionPlanError("WAIT_FOR_COOKED requires one target")
            target = object_refs[0]
            cook_target = get_container(target, self.env) if isinstance(target, BaseSystem) else target
            if cook_target is None or not self._supports_cooked_effect(target):
                raise BadExecutionPlanError(
                    f'target object "{getattr(target, "name", target)}" is not cookable or heatable'
                )
            has_pending_heating = bool(
                self._has_pending_heating_process
                and self._has_pending_heating_process(target)
            )
            if not has_pending_heating:
                check_heat_source_before_cook(cook_target, self.env)

        if primitive_name == "WAIT_FOR_WASHED":
            if len(object_refs) != 1:
                raise BadExecutionPlanError("WAIT_FOR_WASHED requires one wash machine")
            wash_machine = object_refs[0]
            wash_name = self._temporal_name_text(None, wash_machine)
            if not any(marker in wash_name for marker in ("washer", "dishwasher")):
                raise BadExecutionPlanError(
                    "WAIT_FOR_WASHED requires a washer or dishwasher"
                )
            states = getattr(wash_machine, "states", {})
            if (
                object_states.Open in states
                and states[object_states.Open].get_value()
            ):
                raise BadExecutionPlanError("wash machine must be closed before waiting")
            if (
                object_states.ToggledOn not in states
                or not states[object_states.ToggledOn].get_value()
            ):
                raise BadExecutionPlanError("wash machine must be toggled on before waiting")
            if not self._temporal_placements(wash_machine, inside_only=True):
                raise BadExecutionPlanError("wash machine has no objects to wash")

        if primitive_name == "WAIT_FOR_FROZEN":
            if len(object_refs) != 2:
                raise BadExecutionPlanError(
                    "WAIT_FOR_FROZEN requires an object and cold-storage target"
                )
            target_obj, cold_storage = object_refs
            cold_storage_name = self._temporal_name_text(None, cold_storage)
            if not any(
                marker in cold_storage_name
                for marker in ("fridge", "refrigerator", "freezer")
            ):
                raise BadExecutionPlanError(
                    "WAIT_FOR_FROZEN requires a refrigerator or freezer"
                )
            states = getattr(target_obj, "states", {})
            if object_states.Frozen not in states:
                raise BadExecutionPlanError(
                    f'target object "{getattr(target_obj, "name", target_obj)}" is not freezable'
                )
            if (
                object_states.Inside not in states
                or not states[object_states.Inside].get_value(cold_storage)
            ):
                raise BadExecutionPlanError("target object is not inside cold storage")

    def prepare_temporal_process(
        self,
        event: Any,
        definition: Any,
        entity_ids: Tuple[str, ...],
        _context: Any,
    ) -> Optional[ProcessStart]:
        """Resolve a data-driven process definition against simulator state."""
        extensions = dict(getattr(definition, "extensions", {}) or {})
        selectors = extensions.get("entity_selectors", {})
        selector = (
            selectors.get(event.action_name, "action")
            if isinstance(selectors, Mapping)
            else "action"
        )
        selected = self._select_temporal_entities(
            event,
            str(selector),
            tuple(entity_ids),
        )
        conditions_by_action = extensions.get("conditions_by_action", {})
        conditions = (
            conditions_by_action.get(event.action_name, {})
            if isinstance(conditions_by_action, Mapping)
            else {}
        )
        selected = self._filter_temporal_entities(event, selected, conditions)
        if not selected and not extensions.get("allow_global", False):
            return None

        actor_id = getattr(event, "actor_id", None)
        action_entity_ids = tuple(
            entity_id
            for entity_id in getattr(event, "entity_ids", ())
            if entity_id != actor_id
        )
        gate_entity_ids = tuple(
            dict.fromkeys((*action_entity_ids, *selected))
        )
        return ProcessStart(
            entity_ids=selected,
            start_step=int(event.step),
            extensions={"gate_entity_ids": gate_entity_ids},
        )

    def _select_temporal_entities(
        self,
        event: Any,
        selector: str,
        defaults: Tuple[str, ...],
    ) -> Tuple[str, ...]:
        selector = selector.strip().lower()
        if selector == "action":
            return tuple(dict.fromkeys(defaults))
        if selector == "object":
            return (event.object_id,) if event.object_id else ()
        if selector == "target":
            return (event.target_id,) if event.target_id else ()
        if selector == "target_or_object":
            entity_id = event.target_id or event.object_id
            return (entity_id,) if entity_id else ()

        source_id = event.target_id if selector.endswith("_of_target") else event.object_id
        source_obj = self.resolve_temporal_entity(source_id)
        if source_obj is None:
            return ()
        if selector in {"contents_of_object", "contents_of_target"}:
            placements = self._temporal_placements(source_obj, inside_only=True)
        elif selector in {"placements_of_object", "placements_of_target"}:
            placements = self._temporal_placements(source_obj, inside_only=False)
        else:
            return ()
        return tuple(
            dict.fromkeys(
                identifier
                for identifier in (
                    self._task_entity_id(placement.object)
                    for placement in placements
                )
                if identifier
            )
        )

    def _filter_temporal_entities(
        self,
        event: Any,
        entity_ids: Tuple[str, ...],
        conditions: Any,
    ) -> Tuple[str, ...]:
        if not isinstance(conditions, Mapping):
            return entity_ids
        source_obj = self.resolve_temporal_entity(event.object_id)
        target_obj = self.resolve_temporal_entity(event.target_id)

        if not self._temporal_object_matches(
            source_obj,
            event.object_id,
            name_contains=conditions.get("source_name_contains"),
            supports_any=conditions.get("source_supports_any"),
            states=conditions.get("source_states"),
        ):
            return ()
        if not self._temporal_object_matches(
            target_obj,
            event.target_id,
            name_contains=conditions.get("target_name_contains"),
            supports_any=conditions.get("target_supports_any"),
            states=conditions.get("target_states"),
        ):
            return ()

        selected = []
        for entity_id in entity_ids:
            obj = self.resolve_temporal_entity(entity_id)
            if not self._temporal_object_matches(
                obj,
                entity_id,
                name_contains=conditions.get("entity_name_contains"),
                supports_any=conditions.get("entities_support_any"),
                states=conditions.get("entity_states"),
            ):
                continue
            if conditions.get("entity_inside_target", False):
                states = getattr(obj, "states", {})
                try:
                    if (
                        target_obj is None
                        or object_states.Inside not in states
                        or not states[object_states.Inside].get_value(target_obj)
                    ):
                        continue
                except Exception:
                    continue
            selected.append(entity_id)
        return tuple(dict.fromkeys(selected))

    def _temporal_object_matches(
        self,
        obj: Any,
        entity_id: Optional[str],
        *,
        name_contains: Any = None,
        supports_any: Any = None,
        states: Any = None,
    ) -> bool:
        has_constraints = any(
            value not in (None, (), [], {})
            for value in (name_contains, supports_any, states)
        )
        if obj is None:
            return not has_constraints
        if name_contains:
            markers = (name_contains,) if isinstance(name_contains, str) else name_contains
            name_text = self._temporal_name_text(entity_id, obj)
            if not any(str(marker).lower() in name_text for marker in markers):
                return False
        if supports_any:
            semantics = (supports_any,) if isinstance(supports_any, str) else supports_any
            if not any(self._supports_temporal_state(obj, value) for value in semantics):
                return False
        if isinstance(states, Mapping):
            for semantic, expected in states.items():
                actual = self._read_temporal_state(obj, str(semantic))
                if actual is None or bool(actual) != bool(expected):
                    return False
        return True

    def resolve_temporal_entity(self, entity_id: Optional[str]) -> Any:
        if not entity_id:
            return None
        return find_task_related_object(self.env, str(entity_id))

    def _task_entity_id(self, obj: Any) -> Optional[str]:
        if obj is None:
            return None
        for entity_id, reference in getattr(self.env.task, "object_scope", {}).items():
            if getattr(reference, "wrapped_obj", None) is obj:
                return str(entity_id)
        name = getattr(obj, "name", None)
        return None if name is None else str(name)

    def _temporal_placements(self, source_obj: Any, *, inside_only: bool) -> Tuple[Any, ...]:
        if source_obj is None or not hasattr(source_obj, "states"):
            return ()
        try:
            placements = get_placement_objects(
                source_obj,
                self.env,
                object_states.Inside if inside_only else None,
            )
        except Exception:
            return ()
        return tuple(placements or ())

    @staticmethod
    def _temporal_name_text(entity_id: Optional[str], obj: Any) -> str:
        return " ".join(
            str(value).lower()
            for value in (
                entity_id,
                getattr(obj, "name", None),
                getattr(obj, "category", None),
                getattr(obj, "model", None),
            )
            if value
        )

    @staticmethod
    def _temporal_state_class(semantic: Any) -> Any:
        key = str(semantic).strip().lower()
        names = {
            "cooked": "Cooked",
            "covered": "Covered",
            "frozen": "Frozen",
            "heat_source": "HeatSourceOrSink",
            "heated": "Heated",
            "open": "Open",
            "spoiled": "Spoiled",
            "toggled_on": "ToggledOn",
        }
        class_name = names.get(key)
        return getattr(object_states, class_name, None) if class_name else None

    def _supports_temporal_state(self, obj: Any, semantic: Any) -> bool:
        if str(semantic).strip().lower() == "cooked":
            return self._supports_cooked_effect(obj)
        state_class = self._temporal_state_class(semantic)
        return state_class is not None and state_class in getattr(obj, "states", {})

    def _cooked_system_available(self, raw_system_name: str) -> bool:
        cooked_name = f"cooked__{raw_system_name}"
        if cooked_name not in getattr(self.env._scene, "available_systems", set()):
            return False
        return any(
            cooked_name in str(entity_name)
            for entity_name in getattr(self.env.task, "object_scope", {})
        )

    def _supports_cooked_effect(self, obj: Any) -> bool:
        if obj is None:
            return False
        if isinstance(obj, BaseSystem):
            return (
                get_container(obj, self.env) is not None
                and self._cooked_system_available(obj.name)
            )
        states = getattr(obj, "states", {})
        if object_states.Cooked in states or object_states.Heated in states:
            return True
        for system in get_contained_systems(obj) or ():
            if self._cooked_system_available(system.name):
                return True
        return any(
            object_states.Cooked in getattr(placement.object, "states", {})
            for placement in self._temporal_placements(obj, inside_only=False)
        )

    def _read_temporal_state(self, obj: Any, semantic: str) -> Optional[bool]:
        semantic = str(semantic).strip().lower()
        if semantic == "covered":
            systems = get_covered_systems(obj)
            return None if systems is None else bool(systems)
        state_class = self._temporal_state_class(semantic)
        states = getattr(obj, "states", {})
        if state_class is None or state_class not in states:
            return None
        if semantic == "heat_source":
            return True
        try:
            return bool(states[state_class].get_value())
        except Exception:
            return None

    def temporal_readiness(
        self,
        process: Any,
        predicate: str,
        _context: Any,
    ) -> Optional[bool]:
        predicate = str(predicate).strip().lower()
        # These semantics intentionally live only in the Object Module unless
        # a dedicated simulator adapter is registered by the caller.
        if predicate in {"dry", "spoiled"}:
            return None

        values = []
        for entity_id in process.entity_ids:
            obj = self.resolve_temporal_entity(entity_id)
            if obj is None:
                continue
            if predicate == "washed":
                covered = self._read_temporal_state(obj, "covered")
                if covered is not None:
                    values.append(not covered)
                continue
            if predicate == "cooked_or_heated":
                readiness = self._cooked_or_heated_readiness(obj)
                if readiness is not None:
                    values.append(readiness)
                continue
            invert = predicate.startswith("not_")
            semantic = predicate[4:] if invert else predicate
            value = self._read_temporal_state(obj, semantic)
            if value is not None:
                values.append(not value if invert else value)
        return all(values) if values else None

    def _cooked_or_heated_readiness(self, obj: Any) -> Optional[bool]:
        if isinstance(obj, BaseSystem):
            return False if self._supports_cooked_effect(obj) else None

        states = getattr(obj, "states", {})
        if object_states.Cooked in states:
            return self._read_temporal_state(obj, "cooked")

        for state_class in (
            object_states.Contains,
            object_states.ContainedParticles,
        ):
            state = states.get(state_class)
            if state is not None:
                state.clear_cache()

        contained_systems = list(get_contained_systems(obj) or ())
        contained_raw_systems = [
            system
            for system in {system.name: system for system in contained_systems}.values()
            if self._cooked_system_available(system.name)
        ]
        placed_cookable = [
            placement.object
            for placement in self._temporal_placements(obj, inside_only=False)
            if object_states.Cooked in getattr(placement.object, "states", {})
        ]
        if contained_raw_systems:
            return False
        if placed_cookable:
            cooked_values = [
                self._read_temporal_state(placed_obj, "cooked")
                for placed_obj in placed_cookable
            ]
            known_values = [value for value in cooked_values if value is not None]
            return all(known_values) if known_values else None
        return self._read_temporal_state(obj, "heated")

    def apply_temporal_effects(
        self,
        process: Any,
        effects: Mapping[str, Any],
        _context: Any,
    ) -> Optional[bool]:
        results = []
        for entity_id in process.entity_ids:
            obj = self.resolve_temporal_entity(entity_id)
            if obj is None:
                continue
            for semantic, value in effects.items():
                result = self._apply_temporal_state(obj, str(semantic), value)
                if result is not None:
                    results.append(result)
        if any(result is False for result in results):
            return False
        return True if results else None

    def _apply_temporal_state(
        self,
        obj: Any,
        semantic: str,
        value: Any,
    ) -> Optional[bool]:
        semantic = semantic.strip().lower()
        if semantic in {"spoiled", "wet"}:
            return None
        if semantic == "covered":
            systems = get_covered_systems(obj)
            if systems is None:
                return None
            if bool(value):
                return None
            try:
                for system in systems:
                    obj.states[object_states.Covered].set_value(system, False)
                return True
            except Exception:
                return False
        if semantic == "cooked":
            if not bool(value):
                return None
            return self._apply_cooked_effect(obj)

        state_class = self._temporal_state_class(semantic)
        states = getattr(obj, "states", {})
        if state_class is None or state_class not in states:
            return None
        try:
            states[state_class].set_value(bool(value))
            return True
        except Exception:
            return False

    @staticmethod
    def _apply_cooked_temperature(obj: Any) -> Optional[bool]:
        states = getattr(obj, "states", {})
        cooked_state = states.get(getattr(object_states, "Cooked", None))
        if cooked_state is None:
            return None
        temperature_state = states.get(getattr(object_states, "Temperature", None))
        max_temperature_state = states.get(getattr(object_states, "MaxTemperature", None))
        if temperature_state is None or max_temperature_state is None:
            return None
        thresholds = [float(getattr(cooked_state, "cook_temperature", 0.0))]
        heated_state = states.get(getattr(object_states, "Heated", None))
        if heated_state is not None:
            thresholds.append(float(getattr(heated_state, "heat_temperature", 0.0)))
        target_temperature = max(thresholds)
        try:
            temperature_state.set_value(target_temperature)
            max_temperature_state.set_value(target_temperature)
            return True
        except Exception:
            return False

    def _apply_cooked_effect(self, obj: Any) -> Optional[bool]:
        if isinstance(obj, BaseSystem):
            return self._convert_cooked_particle_system(
                obj,
                container=get_container(obj, self.env),
            )

        states = getattr(obj, "states", {})
        if object_states.Cooked in states:
            return self._apply_cooked_temperature(obj)

        results = []
        registry = getattr(getattr(obj, "scene", None), "system_registry", None)
        systems = {
            system.name: system
            for system in getattr(registry, "objects", ())
            if getattr(system, "is_fluid", False)
            and self._cooked_system_available(system.name)
        }
        for system in systems.values():
            result = self._convert_cooked_particle_system(
                system,
                container=obj,
            )
            if result is not None:
                results.append(result)
        for placement in self._temporal_placements(obj, inside_only=False):
            placed_obj = placement.object
            if object_states.Cooked in getattr(placed_obj, "states", {}):
                result = self._apply_cooked_temperature(placed_obj)
                if result is not None:
                    results.append(result)
        if any(result is False for result in results):
            return False
        if any(result is True for result in results):
            return True
        return None

    def _convert_cooked_particle_system(
        self,
        system: BaseSystem,
        *,
        container: Any,
    ) -> Optional[bool]:
        if container is None:
            return False
        cooked_system = get_cooked_system(f"cooked__{system.name}", self.env)
        if cooked_system is None:
            return None
        contained_state = getattr(container, "states", {}).get(
            object_states.ContainedParticles
        )
        if contained_state is None:
            return None

        # OmniGibson's native CookingPhysicalParticleRule mutates the raw and
        # cooked systems during env.step. Relative object states cache within a
        # timestep, so the scheduler callback must discard any pre-transition
        # raw mask before it inspects the live instancers.
        contained_state.clear_cache()
        raw_contained = contained_state.get_value(system)
        contained_state.clear_cache()
        cooked_contained = contained_state.get_value(cooked_system)
        raw_count = int(raw_contained.n_in_volume)
        cooked_count = int(cooked_contained.n_in_volume)
        expectation = getattr(
            self,
            "_active_cooked_particle_expectations",
            {},
        ).get((id(container), system.name))
        expected_cooked_count = (
            None
            if expectation is None
            else int(expectation["raw_count"])
            + int(expectation["initial_cooked_count"])
        )

        print(
            "[executor][temporal][particle_conversion] "
            f"source={system.name} target={cooked_system.name} "
            f"container={getattr(container, 'name', None)} "
            f"raw_in_container={raw_count} cooked_in_container={cooked_count} "
            f"raw_total={int(system.n_particles)} "
            f"cooked_total={int(cooked_system.n_particles)} "
            f"expected_cooked_in_container={expected_cooked_count} "
            "mode=native_rule_verification"
        )
        sys.stdout.flush()

        if raw_count == 0 and cooked_count > 0:
            return expected_cooked_count is None or cooked_count == expected_cooked_count
        if raw_count == 0 and cooked_count == 0:
            return None
        return False

    def _starter_toggle_action_seq(self, target_obj, value: bool):
        """Apply a toggle; temporal effects are owned by the Scheduler."""
        if not hasattr(target_obj, 'states') or object_states.ToggledOn not in target_obj.states:
            raise BadExecutionPlanError(f'target object "{target_obj.name}" is not toggleable')
        if target_obj.states[object_states.ToggledOn].get_value() != value:
            target_obj.states[object_states.ToggledOn].set_value(value)
        for _ in range(5):
            yield self.get_hold_action()

    def _starter_pour_into_action_seq(self, target_obj):
        """Fill ``target_obj`` from the currently physically held container.

        The source is implicit in physical starter syntax: the robot must first
        GRASP the source container and NAVIGATE_TO the target.  This mirrors the
        one-argument placement primitives while preserving the BDDL Filled
        effect used by OmniGibson's legacy POUR_INTO action.
        """
        source_obj = self.controller._get_obj_in_hand()
        if source_obj is None:
            raise BadExecutionPlanError('POUR_INTO requires a container currently held by the robot')

        target_systems = get_contained_systems(target_obj)
        if target_systems is None or object_states.Filled not in getattr(target_obj, 'states', {}):
            raise BadExecutionPlanError(f'target object "{target_obj.name}" cannot be filled')
        if (
            object_states.Open in getattr(target_obj, 'states', {})
            and not target_obj.states[object_states.Open].get_value()
        ):
            raise BadExecutionPlanError(
                f'target object "{target_obj.name}" must be open before POUR_INTO'
            )

        source_systems = get_contained_systems(source_obj)
        if not source_systems and hasattr(
            self.controller,
            'symbolic_carried_particle_systems',
        ):
            source_systems = self.controller.symbolic_carried_particle_systems(
                source_obj
            )
        if not source_systems:
            raise BadExecutionPlanError(
                f'held source container "{source_obj.name}" does not contain any particles'
            )

        transfer_results = None
        if hasattr(
            self.controller,
            'transfer_symbolic_carried_particles_to_target',
        ):
            transfer_results = (
                self.controller.transfer_symbolic_carried_particles_to_target(
                    source_obj,
                    target_obj,
                )
            )

        if transfer_results is not None:
            commit_transfer = getattr(
                self.controller,
                'commit_symbolic_particle_transfer',
                None,
            )
            rollback_transfer = getattr(
                self.controller,
                'rollback_symbolic_particle_transfer',
                None,
            )
            committed = False
            try:
                # Object-state values are cached within a simulator frame.
                # Advance once, but retain the source snapshot until Filled is
                # verified so a failed transfer remains retryable.
                yield self.get_hold_action()
                failed_systems = []
                for result in transfer_results:
                    system = result['system']
                    contained_state = target_obj.states[
                        object_states.ContainedParticles
                    ]
                    contained_state.clear_cache()
                    contained = contained_state.get_value(system)
                    particle_volume = float((system.particle_radius * 2.0) ** 3)
                    container_volume = float(
                        target_obj.states[object_states.ContainedParticles].volume
                    )
                    volume_fraction = (
                        particle_volume * int(contained.n_in_volume) / container_volume
                        if container_volume > 0.0
                        else 0.0
                    )
                    print(
                        '[starter][pour][filled_volume] '
                        f'target={target_obj.name} system={system.name} '
                        f'particles={int(contained.n_in_volume)} '
                        f'particle_volume={particle_volume:.9g} '
                        f'container_volume={container_volume:.9g} '
                        f'volume_fraction={volume_fraction:.6f}'
                    )
                    sys.stdout.flush()
                    filled_state = target_obj.states[object_states.Filled]
                    filled_state.clear_cache()
                    if not filled_state.get_value(system):
                        failed_systems.append(
                            {
                                'system': system.name,
                                'available': result['available_count'],
                                'transferred': result['transferred_count'],
                                'remaining': result['remaining_count'],
                                'target_particles': int(contained.n_in_volume),
                                'particle_volume': particle_volume,
                                'container_volume': container_volume,
                                'volume_fraction': volume_fraction,
                            }
                        )
                if failed_systems:
                    raise BadExecutionPlanError(
                        f'physical particle transfer did not fill target "{target_obj.name}": '
                        f'{failed_systems}'
                    )
                if commit_transfer is None or not commit_transfer(
                    source_obj, target_obj
                ):
                    raise BadExecutionPlanError(
                        'physical particle transfer verification had no pending commit'
                    )
                committed = True
            finally:
                if not committed and rollback_transfer is not None:
                    rollback_transfer(source_obj, target_obj)
            print(
                '[starter][pour] '
                f'source={source_obj.name} target={target_obj.name} '
                f'systems={[result["system"].name for result in transfer_results]} '
                'mode=physical_transfer'
            )
            sys.stdout.flush()
            return

        physical_source_systems = []
        for system in source_systems:
            try:
                if source_obj.scene.is_physical_particle_system(
                    system_name=system.name
                ):
                    physical_source_systems.append(system.name)
            except Exception:
                continue
        if physical_source_systems:
            raise BadExecutionPlanError(
                "POUR_INTO has no physical transfer for systems "
                f"{physical_source_systems}; refusing to duplicate source particles"
            )

        for system in source_systems:
            target_obj.states[object_states.Filled].set_value(system, True)
        yield self.get_hold_action()
        for system in source_systems:
            if not target_obj.states[object_states.Filled].get_value(system):
                raise BadExecutionPlanError(
                    f'failed to pour system "{system.name}" into "{target_obj.name}"'
                )

        print(
            '[starter][pour] '
            f'source={source_obj.name} target={target_obj.name} '
            f'systems={[system.name for system in source_systems]}'
        )
        sys.stdout.flush()

    def _starter_dump_into_action_seq(self, target_obj):
        """Empty rigid contents from the held source container as one action."""
        source_obj = self.controller._get_obj_in_hand()
        if source_obj is None:
            raise BadExecutionPlanError(
                'DUMP_INTO requires a container currently held by the robot'
            )

        try:
            dumped_objects = yield from self.controller.dump_carried_contents_into(
                source_obj,
                target_obj,
            )
        except ActionPrimitiveError as exc:
            raise BadExecutionPlanError(
                f'failed to dump held container "{source_obj.name}" into '
                f'"{target_obj.name}": {exc}'
            ) from exc

        print(
            '[starter][dump] '
            f'source={source_obj.name} target={target_obj.name} '
            f'objects={[obj.name for obj in dumped_objects]}'
        )
        sys.stdout.flush()

    def _starter_active_rinse_systems(self, target_obj, max_xy_distance=1.5):
        """Return fluids produced by nearby task sources that are currently on.

        Starter ``WIPE`` has a one-object signature, so a preceding
        ``TOGGLE_ON(sink)`` is the executable indication that the held object is
        being rinsed under that source.  Restricting the lookup to nearby
        sources prevents an unrelated active sink elsewhere in the scene from
        making the wiped object wet.
        """
        try:
            if self.controller._get_obj_in_hand() is not target_obj:
                return [], []
            target_position = target_obj.get_position_orientation()[0]
        except Exception:
            return [], []

        systems_by_name = {}
        source_names = []
        for object_name in self.env.task.object_scope:
            source_obj = find_task_related_object(self.env, object_name)
            if source_obj is None or source_obj is target_obj:
                continue
            source_states = getattr(source_obj, "states", {})
            if (
                object_states.ToggledOn not in source_states
                or not source_states[object_states.ToggledOn].get_value()
            ):
                continue
            try:
                source_position = source_obj.get_position_orientation()[0]
                xy_distance = float(
                    torch.linalg.vector_norm(
                        source_position[:2] - target_position[:2]
                    ).item()
                )
            except Exception:
                continue
            if xy_distance > max_xy_distance:
                continue

            produced_systems = get_produced_systems(source_obj) or []
            if not produced_systems:
                continue
            source_names.append(source_obj.name)
            for system in produced_systems:
                systems_by_name[system.name] = system

        return list(systems_by_name.values()), sorted(set(source_names))

    def _starter_wipe_action_seq(self, target_obj):
        """Remove grime and retain nearby active-source fluid as wetness."""
        covered_systems = get_covered_systems(target_obj)
        if covered_systems is None:
            raise BadExecutionPlanError(f'target object "{target_obj.name}" cannot be wiped')

        rinse_systems, rinse_sources = self._starter_active_rinse_systems(target_obj)
        rinse_names = {system.name for system in rinse_systems}
        for system in covered_systems:
            target_obj.states[object_states.Covered].set_value(system, False)
        # Covered is tensorized and cached for one simulator frame.  Refresh it
        # after clearing grime before applying the rinse fluid.
        yield self.get_hold_action()

        deferred_rinse_names = set()
        defer_coverage = getattr(
            self.controller,
            "defer_symbolic_carried_coverage",
            None,
        )
        if callable(defer_coverage):
            deferred_rinse_names = set(
                defer_coverage(target_obj, rinse_systems)
            )
        for system in rinse_systems:
            if system.name in deferred_rinse_names:
                continue
            target_obj.states[object_states.Covered].set_value(system, True)
        yield self.get_hold_action()

        failed_to_remove = [
            system.name
            for system in covered_systems
            if system.name not in rinse_names
            and target_obj.states[object_states.Covered].get_value(system)
        ]
        failed_to_wet = [
            system.name
            for system in rinse_systems
            if system.name not in deferred_rinse_names
            if not target_obj.states[object_states.Covered].get_value(system)
        ]
        if failed_to_remove or failed_to_wet:
            raise BadExecutionPlanError(
                f'failed to apply starter wipe effects to "{target_obj.name}": '
                f'remaining={failed_to_remove} missing_rinse={failed_to_wet}'
            )

        print(
            '[starter][wipe] '
            f'target={target_obj.name} '
            f'removed={[system.name for system in covered_systems if system.name not in rinse_names]} '
            f'rinse_sources={rinse_sources} wet={sorted(rinse_names)} '
            f'deferred={sorted(deferred_rinse_names)}'
        )
        sys.stdout.flush()
        for _ in range(8):
            yield self.get_hold_action()

    def _simulator_loop(self, interval=None):
        '''
        让仿真在 no-op action 下继续运行一段时间，常用于 debug 或等待物理状态稳定。

        如果 interval 是正整数，则运行固定步数；否则进入无限循环，需要用户手动中断。
        no-op action 由 get_hold_action() 生成，避免绝对位置控制器被错误地命令到 0。

        内部调用示例：
            self._simulator_loop(interval=5)
        '''
        if interval is not None and isinstance(interval, int) and interval > 0:
            for step_index in range(interval):
                self._step_environment(
                    self.get_hold_action(),
                    raw_plan="simulator_wait()",
                    primitive_name="SIMULATOR_WAIT",
                    step_index=step_index,
                )
        else:
            step_index = 0
            while True:
                self._step_environment(
                    self.get_hold_action(),
                    raw_plan="simulator_wait()",
                    primitive_name="SIMULATOR_WAIT",
                    step_index=step_index,
                )
                step_index += 1

    def get_hold_action(self) -> torch.Tensor:
        '''
        生成真正的 no-op / hold action，让机器人保持当前状态。

        注意：全零 action 不一定是真正的 no-op。对于绝对位置 JointController，
        全零可能表示“把关节移动到 0 位置”。因此这里会逐个调用 robot controller 的
        compute_no_op_action(control_dict)，生成各 controller 自己认可的保持动作。

        使用示例：
            hold_action = executor.get_hold_action()
            env.step(hold_action)
        '''
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
        '''
        截取 passive/no-op 仿真阶段的机器人状态。

        相比 _snapshot_robot_state()，这里还会额外记录非 base 控制器对应的关节位置，
        用于判断观察、等待、相机切换等非任务动作阶段是否让机器人意外移动。

        使用示例：
            start_state = executor.snapshot_passive_motion_state()
            for _ in range(5):
                env.step(executor.get_hold_action())
            executor.log_passive_motion_diagnostic("camera_capture", start_state, 5)
        '''
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
        '''
        输出 passive/no-op 仿真阶段的运动诊断信息。

        它会比较 start_state 和当前状态，计算 base 位移、末端执行器位移、
        非 base 关节最大位移，以及手中物体是否变化，用来判断 no-op 阶段是否发生了
        unexpected_motion。

        使用示例：
            start_state = executor.snapshot_passive_motion_state()
            for _ in range(5):
                env.step(executor.get_hold_action())
            diagnostic = executor.log_passive_motion_diagnostic(
                phase="between_actions",
                start_state=start_state,
                simulation_steps=5,
            )
        '''
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
        '''
        计算两个等长列表逐元素差值的最大绝对值。

        主要用于 passive motion 诊断中的 max_joint_displacement。
        如果输入为空或长度不一致，则返回 None；如果列表为空，则返回 0.0。

        使用示例：
            max_delta = Executor._max_abs_difference([0.1, 0.2], [0.1, 0.5])
        '''
        if start is None or end is None or len(start) != len(end):
            return None
        if not start:
            return 0.0
        return round(max(abs(a - b) for a, b in zip(start, end)), 6)
