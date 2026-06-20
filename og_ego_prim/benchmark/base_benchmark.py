from abc import ABC, abstractmethod
import re
from typing import Dict, List, Optional

from og_ego_prim.utils.types import StepwisePlan
from og_ego_prim.primitives.specs import PrimitiveType, expand_legacy_plan_for_starter


class Benchmark(ABC):

    env_config: Dict
    offline_mode: bool
    example_instructions: List[StepwisePlan]

    def __init__(
        self, 
        task: str, 
        scene: str, 
        config: Dict, 
        debug: bool,
        offline_mode: bool,
        primitive_type: PrimitiveType = "ego",
    ):
        self.offline_mode = offline_mode
        self.debug = debug
        self.primitive_type = primitive_type

        self.env_config = self.init_env_config(task, scene, config)
        self._example_planning = self._get_example_planning(config)
        if primitive_type == "starter":
            self._example_planning = [
                expanded_plan
                for plan in self._example_planning
                for expanded_plan in expand_legacy_plan_for_starter(plan)
            ]

    @abstractmethod
    def init_env_config(self, task: str, scene: str, config: Dict) -> Dict:
        pass

    @property
    def task_name(self) -> str:
        return self.env_config['task']['activity_name']
    
    @property
    def scene_name(self) -> str:
        return self.env_config['scene']['scene_model']

    def _get_example_planning(self, config: Dict) -> Optional[List[StepwisePlan]]:
        if 'example_planning' not in config:    
            return []

        example_planning: List[StepwisePlan] = []
        pattern = r'(?:\d+\.\s+)?([a-zA-Z_]+)\(([^)]+)\)'

        for plan in config['example_planning']:
            action = plan['action']
            if action.endswith('DONE'):
                example_planning.append(dict(action='done()', caution=plan['caution']))
                continue

            matches = re.findall(pattern, action)
            if len(matches) >= 1:
                operator, params = matches[-1]
            else:
                return []

            operator = operator.strip().lower()
            params = params.strip().lower()
            action = f'{operator}({params})'
            example_planning.append(dict(action=action, caution=plan['caution']))

        return example_planning

    @abstractmethod
    def execute_plan(self, plan: StepwisePlan):
        pass

    @abstractmethod
    def termination_evaluation(self):
        pass
