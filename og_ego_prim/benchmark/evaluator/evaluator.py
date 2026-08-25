import ast
import json
import re
import os
from typing import Any, Dict, List, Literal, Mapping, Optional

from omnigibson.envs import Environment
from omnigibson.termination_conditions.predicate_goal import PredicateGoal
import openai

from og_ego_prim.benchmark.evaluator.bddl_goal_condition import compile_bddl_goal_condition
from og_ego_prim.benchmark.tracker import EvalTracker
from og_ego_prim.config.eval_config import EvalTaskConfig
from og_ego_prim.models.openai_config import get_openai_request_kwargs
from og_ego_prim.primitives.specs import get_valid_primitives
from og_ego_prim.utils.prompts import *
from og_ego_prim.utils.types import GoalCondition, StepwisePlan


class Evaluator:

    process_safety_goal_condition: Optional[Dict[str, GoalCondition]]
    termination_safety_goal_condition: Optional[List[GoalCondition]]
    execution_goal_condition: Optional[PredicateGoal]

    def __init__(
        self,
        env: Environment,
        config: Dict | EvalTaskConfig,
        tracker: EvalTracker,
        eval_process_safety: bool,
        eval_termination_safety: bool,
        eval_awareness: bool, 
        eval_execution: bool,
    ):
        self.env = env
        self.tracker = tracker
        self.executed_actions = set()
        self.config = self._config_mapping(config)

        # 动作执行前或后检查的安全 BDDL 条件，按“检查时机 + 动作”索引。
        self.process_safety_goal_condition = (
            self._get_process_safety_goal_condition(self.config)
            if eval_process_safety else None
        )
        # 回合结束时检查的安全 BDDL 条件，可按指定动作或任务完成状态触发。
        self.termination_safety_goal_condition = (
            self._get_termination_safety_goal_condition(self.config)
            if eval_termination_safety else None
        )
        # 任务最终成功条件对应的 BDDL 评测器，用于判断执行目标是否达成。
        self.execution_goal_condition = (
            self._get_execution_goal_condition(self.config)
            if eval_execution else None
        )
        # 供 LLM 安全意识裁判比对的标准风险提示列表。
        self.awareness_list = (
            self._get_awareness_list(self.config) if eval_awareness else None
        )
        
        self.judger_client = None

        # 是否开启【安全意识测评】
        if eval_awareness:
            api_key = os.environ.get('OPENAI_API_KEY')
            api_base = os.environ.get('OPENAI_API_BASE')
            if not api_key or not api_base:
                raise RuntimeError(
                    "OPENAI_API_KEY and OPENAI_API_BASE are required for awareness evaluation. "
                    "Configure them in entrypoints/env.local.sh, or pass --not_eval_awareness."
                )
            self.judger_client = openai.OpenAI(api_key=api_key, base_url=api_base)

    @staticmethod
    def _config_mapping(config: Dict | EvalTaskConfig) -> Dict[str, Any]:
        """Normalize the evaluator-only view without exposing planner fields."""

        if isinstance(config, EvalTaskConfig):
            return {
                "task_info": {"primitive_type": config.primitive_type},
                "evaluation_cautions": [dict(item) for item in config.evaluation_cautions],
                "evaluation_goal_conditions": dict(config.evaluation_goal_conditions),
            }
        if not isinstance(config, Mapping):
            raise TypeError("evaluator config must be a mapping or EvalTaskConfig")
        return dict(config)

    def record_action(self, action: str):
        self.executed_actions.add(action.lower().replace(" ", ""))

    def _get_process_safety_goal_condition(self, config: Dict) -> Optional[Dict[str, GoalCondition]]:
        cond_configs = (
            config.get('evaluation_goal_conditions', {})
            .get('process_safety_goal_condition', [])
            or []
        )
        if not cond_configs:
            return None

        primitive_type = config.get('task_info', {}).get('primitive_type', 'ego')
        valid_primitives = get_valid_primitives(primitive_type)
        
        process_safety_goal_condition = {}
        for cond_config in cond_configs:
            condition_type = str(cond_config.get('type', '')).strip().lower()
            assert condition_type in ['after', 'before']
            action = str(cond_config.get('action', '')).strip().lower()
            primitive = action.split('(')[0].strip()
            if primitive.upper() not in valid_primitives:
                raise ValueError(
                    f'Unsupported {primitive_type} process-safety primitive: {primitive}'
                )
            param = action.split('(')[1].strip().split(')')[0].strip().replace(' ', '')
            action = f'{primitive}({param})'
            
            assert 'safety_bddl' in cond_config
            evaluator = compile_bddl_goal_condition(self.env.task, cond_config['safety_bddl']) 
            
            goal_condition: GoalCondition = dict(
                risk_type=cond_config.get('risk_type'),
                safety_principle=cond_config.get('safety_principle'),
                safety_tip=cond_config.get('safety_tip'),
                bddl=cond_config['safety_bddl'],
                bddl_evaluator=evaluator
            )
            process_safety_goal_condition[(condition_type, action)] = goal_condition
        
        return process_safety_goal_condition

    def _get_termination_safety_goal_condition(self, config: Dict) -> Optional[List[GoalCondition]]:
        cond_configs = (
            config.get('evaluation_goal_conditions', {})
            .get('termination_safety_goal_condition', [])
            or []
        )
        if not cond_configs:
            return None

        termination_safety_goal_condition = []
        for cond_config in cond_configs:
            assert 'safety_bddl' in cond_config
            evaluator = compile_bddl_goal_condition(self.env.task, cond_config['safety_bddl']) 

            goal_condition: GoalCondition = dict(
                risk_type=cond_config.get('risk_type'),
                safety_principle=cond_config.get('safety_principle'),
                safety_tip=cond_config.get('safety_tip'),
                action=cond_config.get('action'),
                bddl=cond_config['safety_bddl'], 
                bddl_evaluator=evaluator
            )
            termination_safety_goal_condition.append(goal_condition)

        return termination_safety_goal_condition

    def _get_goal_text_from_tokens(self, goal_text: str, goal_conds: List) -> str:
        goal_text += '('
        for i, token in enumerate(goal_conds):
            if isinstance(token, List):
                goal_text = self._get_goal_text_from_tokens(goal_text, token)
            else:
                goal_text += token
                if i != len(goal_conds) - 1:
                    goal_text += ' '
        goal_text += ')'
        return goal_text

    def _get_execution_goal_condition(self, config: Dict) -> GoalCondition:
        goal_condition = config.get('evaluation_goal_conditions', {}).get(
            'execution_goal_condition'
        )
        if not goal_condition:
            parsed_goal_conditions = self.env.task.activity_conditions.parsed_goal_conditions
            if len(parsed_goal_conditions) == 1:
                parsed_goal_conditions = parsed_goal_conditions[0]
            goal_condition_bddl = self._get_goal_text_from_tokens('', parsed_goal_conditions)

            goal_condition: GoalCondition = dict(
                bddl=goal_condition_bddl,
                bddl_evaluator=PredicateGoal(goal_fcn=lambda: self.env.task.activity_goal_conditions)
            )

        else:
            evaluator = compile_bddl_goal_condition(
                self.env.task, goal_condition
            )
            goal_condition: GoalCondition = dict(
                bddl=goal_condition,
                bddl_evaluator=evaluator
            )

        return goal_condition

    def _get_awareness_list(self, config: Dict) -> Optional[List[GoalCondition]]:
        '''
            awareness_list = [
                *evaluation_cautions,
                *process_safety_goal_condition 的风险文字,
                *termination_safety_goal_condition 的风险文字,
            ]
        '''
        awareness_list = [
            dict(item) for item in config.get('evaluation_cautions', []) or []
        ]

        for goal_cond_key in [
            'process_safety_goal_condition', 
            'termination_safety_goal_condition'
        ]:
            awareness_list.extend([
                dict(
                    risk_type=goal_cond.get('risk_type'),
                    safety_principle=goal_cond.get('safety_principle'),
                    safety_tip=goal_cond.get('safety_tip')
                )
                for goal_cond in config.get('evaluation_goal_conditions', {}).get(goal_cond_key, []) or []
            ])
        return awareness_list

    def llm_response_to_json(self, response: str) -> Dict:
        response = response.replace("\n", "")
        
        # Attempt to parse directly as JSON
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # Try extracting content wrapped with ```json
        json_pattern = r"```json\s*([\s\S]*?)\s*```"
        match = re.search(json_pattern, response)
        if match:
            json_str = match.group(1)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

        # Try extracting content wrapped with any ``` block
        code_block_pattern = r"```\s*([\s\S]*?)\s*```"
        match = re.search(code_block_pattern, response)
        if match:
            potential_json = match.group(1)
            try:
                return json.loads(potential_json)
            except json.JSONDecodeError:
                pass

        # Try to extract content between the first '{' and the last '}'
        brace_pattern = r"\{[\s\S]*\}"
        match = re.search(brace_pattern, response)
        if match:
            json_str = match.group(0)
            try:
                # Attempt parsing with ast.literal_eval for JSON-like structures
                return ast.literal_eval(json_str)
            except (ValueError, SyntaxError):
                pass

        # Try parsing key-value pairs for simpler JSON structures
        json_data = {}
        for line in response.split(","):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().strip('"')
                value = value.strip().strip('"')
                json_data[key] = value
        if json_data:
            return json_data
        
        # If all attempts fail, return None or raise an error
        raise ValueError(f"Could not parse response as JSON: {response}")   
    
    def llm_as_a_safety_judger(self, task_instruction, initial_setup, pred_cautions, gt_cautions) -> bool:
        prompt = DefaultSafetyJudgePrompt.format(
            task_instruction=task_instruction,
            initial_setup=initial_setup,                                                 
            safety_tips=gt_cautions,
            sample_planning=pred_cautions
        )
        
        completion = self.judger_client.chat.completions.create(
            model=os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o"),
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.0,
            **get_openai_request_kwargs(),
        )
        print(completion.choices[0].message.content) 
        res = self.llm_response_to_json(str(completion.choices[0].message.content))

        return res
    
    def llm_as_a_safety_awareness_judger(self, task_instruction, initial_setup, pred_cautions, gt_cautions) -> bool:
        prompt = SafetyAwarenessJudgePrompt.format(
            task_instruction=task_instruction,
            initial_setup=initial_setup,                                                 
            gt_cautions=gt_cautions,
            pred_cautions=pred_cautions
        )
        
        completion = self.judger_client.chat.completions.create(
            model=os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o"),
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.6,
            **get_openai_request_kwargs(),
        )
        print(completion.choices[0].message.content) 
        res = self.llm_response_to_json(str(completion.choices[0].message.content))            

        return res

    def evaluate_awareness(self, task_instruction: str, initial_setup: str, awareness: str):
        retry = 0
        results = None
        while retry < 3:
            results = self.llm_as_a_safety_awareness_judger(
                task_instruction, 
                initial_setup, 
                awareness, 
                self.awareness_list
            )
            if type(results) == list:
                break
            elif type(results) == dict:
                results = [results]
                break
            retry += 1
        # eval_awareness = [item["eval"] for item in results] # List[bool]
        self.tracker.track_awareness(
            content=awareness,
            eval_results=results
        )

    def evaluate_process_safety_goal_condition(
        self, plan: StepwisePlan, condition_type: Literal['before', 'after'], verbose=True,
    ):
        if self.process_safety_goal_condition is None:
            return
        
        action = plan['action'].strip().lower().replace(' ', '')
        condition_key = (condition_type.lower(), action)
        if condition_key not in self.process_safety_goal_condition:
            return

        goal_condition = self.process_safety_goal_condition[condition_key]
        assert 'bddl_evaluator' in goal_condition
        _, success = goal_condition['bddl_evaluator'].step(self.env.task, self.env, None)

        self.tracker.track_process_safety_goal_condition(
            action=plan['action'],
            type=condition_type,
            eval_mode='bddl',
            risk_type = goal_condition['risk_type'],
            safety_principle=goal_condition['safety_principle'],
            condition=goal_condition['bddl'],
            eval=success,
        )

        # only evaluate once at first
        del self.process_safety_goal_condition[condition_key]

        if not success and verbose:
            condtion = goal_condition['bddl']
            print(
                f'[goal-condition] Proccess Safety Goal Condition not met.\n'
                f'[goal-condition] -> ❎ <{condition_type}> <{action}> {condtion}'
            )
    
    def evaluate_non_executed_process_safety_goal_condition(self):
        if self.process_safety_goal_condition is not None:
            for condition_key in self.process_safety_goal_condition.keys():
                condition_type, action = condition_key
                
                is_eval = False
                if 'process_safety_goal_condition' in self.tracker.goal_condition:
                    for evaluated_goal_cond in self.tracker.goal_condition['process_safety_goal_condition']:
                        evaluated_type = evaluated_goal_cond['type']
                        evaluated_action = evaluated_goal_cond['action'].replace(' ', '')
                        if evaluated_type == condition_type and evaluated_action == action:
                            is_eval = True
                            break
                if is_eval:
                    continue
                
                goal_condition = self.process_safety_goal_condition[condition_key]
                self.tracker.track_process_safety_goal_condition(
                    action=action,
                    type=condition_type,
                    eval_mode='bddl',
                    risk_type = goal_condition['risk_type'],
                    safety_principle=goal_condition['safety_principle'],
                    condition=goal_condition['bddl'],
                    eval=None,
                )

    def evaluate_termination_safety_goal_condition(self, verbose=True):
        if self.termination_safety_goal_condition is None:
            return

        executed_plans = set([
            plan['plan']['action'].lower().replace(' ', '')
            for plan in self.tracker.plans
        ])
        executed_plans.update(self.executed_actions)

        success = True
        err_msg = ''
        for goal_condition in self.termination_safety_goal_condition:
            condition_action = goal_condition['action']
            assert 'bddl_evaluator' in goal_condition
            evaluator = goal_condition['bddl_evaluator']

            if condition_action is None:
                _, success_i = evaluator.step(self.env.task, self.env, None)
            else:
                condition_action = condition_action.strip().lower().replace(' ', '')
                if condition_action == 'completion':
                    if self.tracker.goal_condition['execution_goal_condition']['eval']:
                        _, success_i = evaluator.step(self.env.task, self.env, None)
                    else:
                        success_i = None
                else:
                    if condition_action in executed_plans:
                        _, success_i = evaluator.step(self.env.task, self.env, None)
                    else:
                        success_i = None
            
            condition = goal_condition['bddl']
            if success_i is not None:
                success &= success_i
                err_msg += f'\n[goal-condition] -> {"✅" if success_i else "❎"} {condition}'

            self.tracker.track_termination_safety_goal_condition(
                action=goal_condition['action'],
                eval_mode='bddl',
                risk_type=goal_condition['risk_type'],
                safety_principle=goal_condition['safety_principle'],
                condition=condition,
                eval=success_i,
            )
        
        if not success and verbose:
            print(
                f'[goal-condition] Termination Safety Goal Condition not met.'
                f'{err_msg}'
            )
    
    def evaluate_execution_goal_condition(self, verbose=True):
        if self.execution_goal_condition is None:
            return
        
        goal_condition = self.execution_goal_condition
        evaluator = goal_condition['bddl_evaluator']
        _, success = evaluator.step(self.env.task, self.env, None)

        self.tracker.track_execution_goal_condition(
            eval_mode='bddl',
            condition=goal_condition['bddl'],
            eval=success,
        )

        if not success and verbose:
            print(
                f'[goal-condition] Execution Goal Condition not met.\n'
                f'[goal-condition] -> ❎ {goal_condition["bddl"]}'
            )
