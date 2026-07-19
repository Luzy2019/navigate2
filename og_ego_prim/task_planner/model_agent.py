import json
import os
import re
import sys
from typing import Any, Generator, List, Tuple, Optional

from og_ego_prim.primitives import get_valid_primitives
from og_ego_prim.primitives.specs import PrimitiveType
from og_ego_prim.benchmark.tracker import EvalTracker
from og_ego_prim.utils.constants import WORK_DIR
from og_ego_prim.utils.planning import list_observation_images, parse_json_code_block
from og_ego_prim.utils.prompts import *
from og_ego_prim.utils.types import StepwisePlan

from og_ego_prim.utils.task_registry import get_task_config_path
from og_ego_prim.memory import TaskMemory
from .context import TaskPlanContext


class BadAgentPlanError(Exception):
    """Raised when a model response cannot be converted into a valid plan."""


class PlanningAgent: 
    
    def __init__(
        self, 
        task_name: str, 
        scene_name: str, 
        agent_name: str, 
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
        self.agent_name = agent_name
        self.current_step = 0
        self.history_start_index = 0
        self.observation_dir = observation_dir
        self.lifelong_instruction_history = []
        self.active_lifelong_instruction = None
        self.preserve_lifelong_history = True
        self.runtime_controller = None
        self._pending_rethinking_prompt = None

        self.retry = retry
        self.verbose = verbose
        self.debug = debug
        
        self.local_llm_serve = local_llm_serve
        self.local_serve_ip = local_serve_ip
        self.local_serve_key = local_serve_key
        self.prompt_setting = prompt_setting
        self.primitive_type = primitive_type
        self.valid_primitives = get_valid_primitives(primitive_type)
        self.use_initial_setup = use_initial_setup
        self.use_self_caption = use_self_caption
        # Semantic history is separate from the scene graph so it survives
        # room changes and can be shared with future online planners.
        self.memory = TaskMemory(task_id=task_name)

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
        
        self.client = self._get_agent(agent_name)
    
    def set_tracker(self, tracker: EvalTracker):
        self.tracker = tracker
        model_name = self.agent_name.split("/")[-1]
        self.tracker.model = model_name

    def set_runtime_controller(self, controller: Any) -> None:
        """Attach the canonical runtime context without changing legacy construction."""
        self.runtime_controller = controller
        if controller is not None and getattr(controller.components, "memory", None) is not None:
            self.memory = controller.components.memory

    def note_runtime_review(self, review: Any) -> None:
        if review is None or not getattr(review, "should_rethink", False):
            self._pending_rethinking_prompt = None
            return
        self._pending_rethinking_prompt = self.runtime_controller.rethinking_prompt()

    def _get_agent(self, agent_name: str) -> Any:
        from og_ego_prim.models.server_inference import ServerClient

        if self.local_llm_serve: 
            return ServerClient(
                model_type="local", 
                model_name=agent_name,
                api_key=self.local_serve_key, 
                api_base=self.local_serve_ip
            ) 
        else: 
            return ServerClient(
                model_type="close_source",
                model_name=agent_name, 
                api_key=os.environ['OPENAI_API_KEY'], 
                api_base=os.environ['OPENAI_API_BASE']
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
                model_tag = self.agent_name.replace('/', '__')
                obs_dir = os.path.join(self.working_dir, benchmark_tag, model_tag, step_tag)
            observations = list_observation_images(obs_dir)

            print(f'read obs from {obs_dir}')
            sys.stdout.flush()
        
        return last_plan, observations

    def _prepare_prompt(self) -> str:
        history_sections = []
        if self.memory.records:
            history_sections.append(
                f"Task and action memory:\n{self.memory.to_prompt_context()}"
            )
        if self.runtime_controller is not None:
            runtime_prompt = (
                self._pending_rethinking_prompt
                or self.runtime_controller.planning_prompt()
            )
            if runtime_prompt:
                history_sections.append(f"Modular runtime context:\n{runtime_prompt}")
        history_plans = '\n'.join(history_sections) if history_sections else "None"

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
        preserve_history: bool,
    ) -> None:
        """Switch the active instruction without resetting the simulator."""
        if self.active_lifelong_instruction is not None:
            self.lifelong_instruction_history.append(self.active_lifelong_instruction)
        if not preserve_history and self.runtime_controller is None:
            self.memory.clear()
        self.active_lifelong_instruction = task_instruction
        self.preserve_lifelong_history = preserve_history
        self.task_instruction = task_instruction
        if not preserve_history:
            self.history_start_index = len(self.tracker.plans)
        if self.runtime_controller is not None:
            self.runtime_controller.set_subtask(
                len(self.lifelong_instruction_history) + 1,
                preserve_memory=preserve_history,
            )
    

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
            if obj not in self.objects_str:
                return None

        if 'caution' not in plan:
            caution = None
        else:
            caution = plan['caution']

        if (
            self.primitive_type == "starter"
            and operator.upper() in {
                "PLACE_ON_TOP", "PLACE_INSIDE", "POUR_INTO", "DUMP_INTO"
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
            return "navigate_to", destination, None

        return operator.lower(), params, caution

    def _last_plan_is_navigation_to(self, target: str) -> bool:
        if not getattr(self, "tracker", None) or not self.tracker.plans:
            return False

        executed_plans = [
            record
            for record in self.tracker.plans
            if record.get('executed') is not False
        ]
        if not executed_plans:
            return False
        last_action = executed_plans[-1]["plan"]["action"].strip().lower()
        expected_action = f"navigate_to({target.strip().lower()})"
        return last_action == expected_action
    
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
        
        

    def step(self, use_obs=True, max_step=None) -> Generator[str, None, None]:
        retry = 0
        start_step = self.current_step
        while True:
            # get obs after last execution
            last_plan, obs = self._get_last_execution_info(use_obs)
            prompt = self._prepare_prompt()

            if self.debug:
                print(f'[agent] last_step: {last_plan}, Continue (y/Y): ')
                sys.stdout.flush()

                while cmd := input().upper() != 'Y':
                    print(f'[agent] last_step: {last_plan}, Continue (y/Y): ')
                    sys.stdout.flush()
            
            output = self.client.model(prompt, image_file=obs)
            next_plan = parse_json_code_block(output)
            if self.verbose:
                print(f"[agent] raw output:\n{output}")
                print(f"[agent] next plan:\n{next_plan}")
                sys.stdout.flush()

            # verification the next step of generated plan is correct
            results = self._verify_plan(next_plan)
            if results is None:
                retry += 1
                if retry < self.retry:
                    print(f"[agent] retry...")
                    sys.stdout.flush()
                    continue
                else:
                    self.tracker.track_termination(
                        reason='plan_error',
                        type='BadAgentPlanError',
                        msg=f'plan ``{next_plan if next_plan else "None"}`` not applicable'
                    )
                    return
            else:
                retry = 0
            
                operator, params, caution = results
                self.current_step += 1
                next_plan: StepwisePlan = dict(
                    action=f'{operator}({params})',
                    caution=caution
                )
                self.tracker.track_plan(
                    step=self.current_step,
                    plan=next_plan,
                    history_text=f'{self.current_step}. {operator.upper()}({params.lower()})'
                )
                self.tracker.track_raw_output(
                    step=self.current_step,
                    content=output,
                )
                self._pending_rethinking_prompt = None
                yield next_plan
                if operator == 'done':
                    return
                if max_step is not None and self.current_step - start_step >= max_step:
                    self.tracker.track_termination(
                        reason='exceeding_max_steps',
                        type='BadAgentPlanError',
                        msg=f'exceeding max steps {max_step}'
                    )
                    return
        
    def load_info_data(self):
        with open(get_task_config_path(self.task_name), 'r', encoding='utf-8') as f:
            task_json_data = json.load(f)
        context = TaskPlanContext(task_json_data)
        task_instruction = context.task_instruction
        objects_list = context.object_list
        objects_str = '\n'.join(f"{i+1}. {item.strip()}" for i, item in enumerate(objects_list))
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

        return (
            task_instruction,
            objects_str,
            initial_setup_str,
            object_abilities_str,
            wash_rules_str,
            context.goal_description,
        )


__all__ = ["PlanningAgent"]
