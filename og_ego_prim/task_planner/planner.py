"""Example and model-backed task planners."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional, Tuple, Union

from og_ego_prim.domain import Action
from og_ego_prim.primitives import get_valid_primitives
from og_ego_prim.primitives.specs import PrimitiveType
from og_ego_prim.benchmark.tracker import EvalTracker
from og_ego_prim.utils.constants import WORK_DIR
from og_ego_prim.utils.planning import (
    list_observation_images,
    parse_json_code_block,
    planner_entity_candidates,
    planner_prompt_entity_ids,
    redact_bddl_instance_ids,
)
from og_ego_prim.utils.prompts import *
from og_ego_prim.utils.task_registry import get_task_config_path
from .context import TaskPlanContext
from .model_agent import AgentModelConfig, resolve_agent_model_config

if TYPE_CHECKING:
    from og_ego_prim.utils.types import StepwisePlan


get_obs_from_dir = list_observation_images
parse_output = parse_json_code_block


class ExamplePlanner:
    """Parse per-task ``example_planning`` entries into executable plans."""

    _ACTION = re.compile(r"(?:\d+\.\s+)?([a-zA-Z_]+)\(([^)]*)\)")

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        plans = []
        for item in config.get("example_planning", []) or []:
            action = str(item.get("action", "")).strip()
            if action.upper() == "DONE" or action.upper().startswith("DONE("):
                normalized = "done()"
            else:
                match = cls._ACTION.search(action)
                if match is None:
                    raise ValueError(f"invalid example planning action: {action!r}")
                normalized = f"{match.group(1).lower()}({match.group(2).strip().lower()})"
            plans.append({"action": normalized, "caution": item.get("caution")})
        return plans

    @classmethod
    def from_task(cls, task: str) -> List[Dict[str, Any]]:
        with get_task_config_path(task).open("r", encoding="utf-8") as file:
            return cls.from_config(json.load(file))


class AgentPlanner:

    def __init__(
        self,
        task_name: str,
        scene_name: str,
        model_name: Union[str, AgentModelConfig],
        work_dir: str,
        local_llm_serve: bool = False,
        local_serve_ip: str = "",
        local_serve_key: str = "",
        prompt_setting: str = "default",
        primitive_type: PrimitiveType = "ego",
        use_initial_setup: bool = False,
        use_self_caption: bool = False,
        retry: int = 3,
        verbose: bool = True,
        debug: bool = False,
        observation_dir: Optional[str] = None,
    ) -> None:
        if work_dir is None:
            work_dir = WORK_DIR
        self.working_dir = os.path.join(work_dir, "benchmark")
        assert os.path.exists(self.working_dir)

        self.task_name = task_name
        self.scene_name = scene_name
        self.model_config = resolve_agent_model_config(
            model_name,
            local=local_llm_serve,
            api_key=local_serve_key,
            api_base=local_serve_ip,
        )
        self.model_name = self.model_config.model_name
        self.current_step = 0
        self.observation_dir = observation_dir
        self.runtime_controller = None
        self._pending_rethinking_prompt = None
        self._pending_manipulation = None
        self._subtask_plan_start = 0
        self.held_object_getter = None

        self.retry = retry
        self.verbose = verbose
        self.debug = debug

        self.prompt_setting = prompt_setting
        self.primitive_type = primitive_type
        self.valid_primitives = get_valid_primitives(primitive_type)
        self.use_initial_setup = use_initial_setup
        self.use_self_caption = use_self_caption

        # initialize data
        (
            self.task_instruction,
            self.objects_str,
            self.initial_setup_str,
            self.object_abilities_str,
            self.wash_rules_str,
            self.goal_description,
        ) = self.load_info_data()
        if self.verbose:
            print(f'[agent] instruction: {self.task_instruction}')
            print(f'[agent] objects:\n{self.objects_str}')
            print(f'[agent] initial setup:\n{self.initial_setup_str}')
            print(f'[agent] object abilities:\n{self.object_abilities_str}')
            print(f'[agent] wash rules:\n{self.wash_rules_str}')
            print(f'[agent] goal description:\n{self.goal_description}')
            sys.stdout.flush()

        self.client = self._create_model_client()

    def set_tracker(self, tracker: EvalTracker):
        self.tracker = tracker
        model_name = self.model_name.split("/")[-1]
        self.tracker.model = model_name

    def set_runtime_controller(self, controller: Any) -> None:
        """Attach the canonical runtime context."""
        self.runtime_controller = controller

    def note_runtime_review(self, review: Any) -> None:
        if review is None or not getattr(review, "should_rethink", False):
            self._pending_rethinking_prompt = None
            return
        self._pending_rethinking_prompt = self.runtime_controller.rethinking_prompt()

    def _held_object(self) -> Optional[str]:
        getter = self.held_object_getter
        if not callable(getter):
            return None
        value = getter()
        return None if value is None else str(value)

    def _create_model_client(self) -> Any:
        from og_ego_prim.models.server_inference import ServerClient

        return ServerClient(
            model_type=self.model_config.model_type,
            model_name=self.model_config.model_name,
            api_key=self.model_config.api_key,
            api_base=self.model_config.api_base,
        )

    def _get_last_execution_info(self, use_obs=True):
        last_step, last_plan = 0, 'init'
        executed_plans = [
            record
            for record in self.tracker.plans
            if record.get('executed') is not False
        ]
        if executed_plans:
            last_record = executed_plans[-1]
            last_step = last_record['step']
            last_plan = last_record['plan']['action']

        if not use_obs:
            observations = None
        else:
            step_tag = f'{last_step}_' + last_plan.replace('(', '__').replace(')', '__')
            if self.observation_dir is not None:
                obs_dir = os.path.join(self.observation_dir, step_tag)
            else:
                benchmark_tag = f'{self.task_name}___{self.scene_name}'
                model_tag = self.model_name.replace('/', '__')
                obs_dir = os.path.join(self.working_dir, benchmark_tag, model_tag, step_tag)
            observations = list_observation_images(obs_dir)

            print(f'read obs from {obs_dir}')
            sys.stdout.flush()

        return last_plan, observations

    def _prepare_prompt(self) -> str:
        history_sections = []
        executed_actions = [
            record.get("history_text")
            or f"{record['step']}. {record['plan']['action'].upper()}"
            for record in self.tracker.plans[self._subtask_plan_start:]
            if record.get("executed") is True and record.get("succeeded") is True
        ]
        if executed_actions:
            history_sections.append("Executed actions:\n" + "\n".join(executed_actions))

        if self.runtime_controller is not None:
            runtime_prompt = (
                self._pending_rethinking_prompt
                or self.runtime_controller.planning_prompt()
            )
            if runtime_prompt:
                history_sections.append(f"Modular runtime context:\n{runtime_prompt}")
            outcome = self.runtime_controller.last_outcome
            if outcome is not None and outcome.executed and not outcome.succeeded:
                diagnostics = dict(
                    getattr(outcome.action_record, "extensions", {}).get(
                        "diagnostics", {}
                    )
                    or {}
                )
                reason = (
                    diagnostics.get("error_message")
                    or outcome.reason
                    or "execution failed"
                )
                history_sections.append(
                    "Last execution failed:\n"
                    f"- Action: {outcome.review.action.to_legacy_plan()}\n"
                    f"- Reason: {reason}\n"
                    "The action did not complete. Do not assume its intended "
                    "postcondition; use the current observation and held-object "
                    "state before choosing the correction."
                )
        if callable(self.held_object_getter):
            history_sections.append(
                f"Current held object: {self._held_object() or 'None'}"
            )
        history_plans = "\n".join(history_sections) if history_sections else "None"

        if self.primitive_type == "starter":
            scene_description = None
            if self.use_initial_setup:
                scene_description = self.initial_setup_str
            elif self.use_self_caption:
                assert self.tracker.caption is not None and 'content' in self.tracker.caption
                scene_description = self.tracker.caption['content']

            awareness = None
            if self.prompt_setting == "v2":
                assert self.tracker.awareness is not None and 'content' in self.tracker.awareness
                awareness = self.tracker.awareness['content']

            return build_starter_step_prompt(
                objects_str=self.objects_str,
                task_instruction=self.task_instruction,
                object_abilities_str=self.object_abilities_str,
                task_goal=self.goal_description,
                wash_rules_str=self.wash_rules_str,
                history_actions=history_plans,
                prompt_setting=self.prompt_setting,
                scene_description=scene_description,
                awareness=awareness,
            )

        if not self.use_initial_setup and not self.use_self_caption:
            if self.prompt_setting == 'v0': # v0: no safety reminder
                prompt = V0StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans
                )
            elif self.prompt_setting == 'v1': # v0 + implicit safety reminder
                prompt = V1StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans
                )
            elif self.prompt_setting == 'v2': # v0 + cot safety reminder
                assert self.tracker.awareness is not None and 'content' in self.tracker.awareness
                awareness = self.tracker.awareness['content']
                prompt = V2StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans,
                    awareness=awareness
                )
            elif self.prompt_setting == 'v3':
                # Task-authored evaluator cautions are not planner inputs.
                prompt = V1StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans
                )
            else:
                raise Exception('Wrong prompt setting.')
        else:
            if self.use_initial_setup:
                scene_description = self.initial_setup_str
            else:
                assert self.tracker.caption is not None and 'content' in self.tracker.caption
                scene_description = self.tracker.caption['content']

            if self.prompt_setting == 'v0':
                prompt = T0StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans,
                    scene_description=scene_description,
                )
            elif self.prompt_setting == 'v1':
                prompt = T1StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans,
                    scene_description=scene_description,
                )
            elif self.prompt_setting == 'v2':
                assert self.tracker.awareness is not None and 'content' in self.tracker.awareness
                awareness = self.tracker.awareness['content']
                prompt = T2StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans,
                    scene_description=scene_description,
                    awareness=awareness
                )
            elif self.prompt_setting == 'v3':
                # Preserve the legacy option without injecting safety oracles.
                prompt = T1StepPlanningPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                    history_actions=history_plans,
                    scene_description=scene_description
                )
            else:
                raise Exception('Wrong prompt setting.')

        return prompt

    def begin_lifelong_subtask(
        self,
        task_instruction: str,
        subtask_index: int,
    ) -> None:
        """Switch the active instruction without resetting the simulator."""
        self.task_instruction = task_instruction
        self.goal_description = task_instruction
        prompt_objects = planner_prompt_entity_ids(
            self.allowed_entity_ids,
            task_instruction,
        )
        self.objects_str = '\n'.join(
            f"{index}. {entity_id}"
            for index, entity_id in enumerate(prompt_objects, start=1)
        )
        self._pending_rethinking_prompt = None
        self._pending_manipulation = None
        self._subtask_plan_start = len(self.tracker.plans)
        if self.runtime_controller is not None:
            self.runtime_controller.set_subtask(subtask_index)


    def _verify_plan(self, plan: Optional[StepwisePlan]) -> Optional[Tuple[str, str, str]]:
        if plan is None:
            return None
        if 'action' not in plan:
            return None

        action = plan['action'].strip()
        if action.upper().startswith('DONE'):
            caution = plan.get('caution', None)
            return 'done', '', caution

        pattern = r'(?:\d+\.\s+)?([a-zA-Z_]+)\(([^)]*)\)'
        matches = re.findall(pattern, action)
        if len(matches) >= 1:
            operator, params = matches[-1]
        else:
            return None

        operator = operator.strip()
        if operator.upper() not in self.valid_primitives:
            return None

        params = params.strip().lower()
        objects = [] if not params else [obj.strip() for obj in params.split(',')]
        if len(objects) != self.valid_primitives[operator.upper()]:
            return None
        for obj in objects:
            if not planner_entity_candidates(obj, self.allowed_entity_ids):
                return None

        if 'caution' not in plan:
            caution = None
        else:
            caution = plan['caution']

        if (
            self.primitive_type == "starter"
            and operator.upper() in {"OPEN", "CLOSE"}
            and callable(self.held_object_getter)
            and self._held_object() is not None
        ):
            print(
                f"[agent][planner_guard] rejecting {operator.upper()} while "
                "the gripper is occupied"
            )
            sys.stdout.flush()
            return None

        last_outcome = (
            None
            if self.runtime_controller is None
            else self.runtime_controller.last_outcome
        )
        if (
            self.primitive_type == "starter"
            and last_outcome is not None
            and last_outcome.executed
            and not last_outcome.succeeded
            and last_outcome.review.action.to_legacy_plan().strip().lower()
            == f"{operator}({params})".lower()
        ):
            print(
                "[agent][planner_guard] rejecting unchanged retry of failed "
                f"{operator.upper()}({params})"
            )
            sys.stdout.flush()
            return None

        placement_actions = {
            "PLACE_ON_TOP", "PLACE_INSIDE", "POUR_INTO", "DUMP_INTO"
        }
        if (
            self.primitive_type == "starter"
            and operator.upper() in placement_actions
            and callable(self.held_object_getter)
            and self._held_object() is None
        ):
            print(
                "[agent][planner_guard] rejecting starter placement while "
                "the gripper is empty"
            )
            sys.stdout.flush()
            return None

        if (
            self.primitive_type == "starter"
            and operator.upper() == "NAVIGATE_TO"
            and objects
            and self._last_plan_is_navigation_to(objects[0])
        ):
            print(
                "[agent][planner_guard] rejecting repeated successful "
                f"NAVIGATE_TO({objects[0]})"
            )
            sys.stdout.flush()
            return None

        if (
            self.primitive_type == "starter"
            and operator.upper() in {
                "GRASP", "PLACE_ON_TOP", "PLACE_INSIDE", "POUR_INTO", "DUMP_INTO"
            }
            and objects
            and not self._last_plan_is_navigation_to(objects[0])
        ):
            destination = objects[0]
            print(
                "[agent][planner_guard] rewriting starter manipulation to "
                f"NAVIGATE_TO({destination}) before {operator.upper()}({destination})"
            )
            sys.stdout.flush()
            self._pending_manipulation = (
                operator.lower(),
                params,
                caution,
                destination,
            )
            return "navigate_to", destination, None

        return operator.lower(), params, caution

    def _last_plan_is_navigation_to(self, target: str) -> bool:
        if not getattr(self, "tracker", None) or not self.tracker.plans:
            return False

        current_subtask_plans = self.tracker.plans[self._subtask_plan_start:]
        if not current_subtask_plans:
            return False
        last_record = current_subtask_plans[-1]
        if not (
            last_record.get("executed") is True
            and last_record.get("succeeded") is True
        ):
            return False
        try:
            last_action = Action.from_raw(last_record["plan"]["action"])
        except (TypeError, ValueError):
            return False
        if last_action.name != "NAVIGATE_TO" or last_action.object_id is None:
            return False

        previous_candidates = set(
            planner_entity_candidates(last_action.object_id, self.allowed_entity_ids)
        )
        target_candidates = set(
            planner_entity_candidates(target, self.allowed_entity_ids)
        )
        return len(previous_candidates & target_candidates) == 1

    def generate_caption(self, use_obs=True) -> str:
        _, obs = self._get_last_execution_info(use_obs)
        prompt_cp = GenerateCaptionPrompt.format(
                objects_str=self.objects_str,
                task_instruction=self.task_instruction,
                object_abilities_str=self.object_abilities_str,
                task_goal=self.goal_description,
                wash_rules_str=self.wash_rules_str,
            )
        output_caption = self.client.model(prompt_cp, image_file=obs)
        return output_caption

    def generate_awareness(self, use_obs=True) -> str:
        _, obs = self._get_last_execution_info(use_obs)
        if self.use_initial_setup or self.use_self_caption:
            if self.use_initial_setup:
                scene_description = self.initial_setup_str
            else:
                assert self.tracker.caption is not None and 'content' in self.tracker.caption
                scene_description = self.tracker.caption['content']
            prompt_sa = T2GenerateAwarenessPrompt.format(
                objects_str=self.objects_str,
                task_instruction=self.task_instruction,
                object_abilities_str=self.object_abilities_str,
                task_goal=self.goal_description,
                wash_rules_str=self.wash_rules_str,
                scene_description=scene_description,
            )
        else:
            prompt_sa = GenerateAwarenessPrompt.format(
                    objects_str=self.objects_str,
                    task_instruction=self.task_instruction,
                    object_abilities_str=self.object_abilities_str,
                    task_goal=self.goal_description,
                    wash_rules_str=self.wash_rules_str,
                )
        output = self.client.model(prompt_sa, image_file=obs)
        return output

    def record_plan(
        self,
        action: Action | str,
        *,
        caution: Optional[str] = None,
        raw_output: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record one model-produced action for the legacy tracker and executor."""

        parsed = action if isinstance(action, Action) else Action.from_raw(action)
        action_text = parsed.to_legacy_plan()
        self.current_step += 1
        plan = {"action": action_text, "caution": caution}
        self.tracker.track_plan(
            step=self.current_step,
            plan=plan,
            history_text=f"{self.current_step}. {parsed.to_legacy_plan(lowercase=False)}",
        )
        if raw_output is not None:
            self.tracker.track_raw_output(step=self.current_step, content=raw_output)
        self._pending_rethinking_prompt = None
        return plan

    def step(self, use_obs=True, max_step=None) -> Generator[str, None, None]:
        retry = 0
        retry_feedback = None
        start_step = self.current_step
        while True:
            if self._pending_manipulation is not None:
                operator, params, caution, navigation_target = self._pending_manipulation
                self._pending_manipulation = None
                if self._last_plan_is_navigation_to(navigation_target):
                    next_plan = self.record_plan(
                        f'{operator}({params})',
                        caution=caution,
                    )
                    yield next_plan
                    if max_step is not None and self.current_step - start_step >= max_step:
                        self.tracker.track_termination(
                            reason='exceeding_max_steps',
                            msg=f'exceeding max steps {max_step}'
                        )
                        return
                    continue

            # get obs after last execution
            last_plan, obs = self._get_last_execution_info(use_obs)
            prompt = self._prepare_prompt()
            if retry_feedback is not None:
                prompt += f"\n\nPlanner correction:\n{retry_feedback}"

            if self.debug:
                print(f'[agent] last_step: {last_plan}, Continue (y/Y): ')
                sys.stdout.flush()

                while cmd := input().upper() != 'Y':
                    print(f'[agent] last_step: {last_plan}, Continue (y/Y): ')
                    sys.stdout.flush()

            output = self.client.model(prompt, image_file=obs)
            next_plan = parse_json_code_block(output)
            self.tracker.track_raw_output(
                step=self.current_step + 1,
                content=output,
            )
            if self.verbose:
                print(f"[agent] raw output:\n{output}")
                print(f"[agent] next plan:\n{next_plan}")
                sys.stdout.flush()

            # verification the next step of generated plan is correct
            results = self._verify_plan(next_plan)
            if results is None:
                retry += 1
                retry_feedback = (
                    "The previous proposal was invalid, repeated a completed "
                    "action, or repeated a failed action without correcting "
                    "its precondition. Re-read Current held object and active "
                    "processes, then choose a different applicable action."
                )
                if retry < self.retry:
                    print(f"[agent] retry...")
                    sys.stdout.flush()
                    continue
                else:
                    self.tracker.track_termination(
                        reason='plan_error',
                        msg=f'plan ``{next_plan if next_plan else "None"}`` not applicable'
                    )
                    return
            else:
                retry = 0
                retry_feedback = None

                operator, params, caution = results
                next_plan: StepwisePlan = self.record_plan(
                    f'{operator}({params})',
                    caution=caution,
                )
                yield next_plan
                if operator == 'done':
                    return
                if max_step is not None and self.current_step - start_step >= max_step:
                    self.tracker.track_termination(
                        reason='exceeding_max_steps',
                        msg=f'exceeding max steps {max_step}'
                    )
                    return

    def load_info_data(self):
        with open(get_task_config_path(self.task_name), 'r', encoding='utf-8') as f:
            task_json_data = json.load(f)
        context = TaskPlanContext(task_json_data)
        task_instruction = context.task_instruction
        objects_list = context.object_list
        prompt_objects = planner_prompt_entity_ids(objects_list, task_instruction)
        objects_str = '\n'.join(
            f"{i+1}. {item}" for i, item in enumerate(prompt_objects)
        )
        intial_setup_list = context.initial_setup
        initial_setup_str = '\n'.join(f"{item.strip()}" for i, item in enumerate(intial_setup_list))

        object_abilities = context.object_abilities
        if object_abilities is None:
            object_abilities_str = ""
        else:
            object_abilities_str = '\n'.join([f"{key}: " + str(value) for key, value in object_abilities.items()])

        wash_rules = context.wash_rules
        if wash_rules is None:
            wash_rules_str = ""
        else:
            wash_rules_str = json.dumps(wash_rules, indent=4, ensure_ascii=False)

        self.allowed_entity_ids = tuple(context.object_list)
        return (
            task_instruction,
            objects_str,
            initial_setup_str,
            object_abilities_str,
            wash_rules_str,
            context.goal_description,
        )


__all__ = [
    "AgentPlanner",
    "ExamplePlanner",
    "TaskPlanContext",
    "get_obs_from_dir",
    "parse_output",
]
