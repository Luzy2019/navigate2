from abc import ABC, abstractmethod
import re
from typing import Dict, List, Optional

from og_ego_prim.utils.types import StepwisePlan
from og_ego_prim.primitives.specs import PrimitiveType, expand_legacy_plan_for_starter
from og_ego_prim.task_planner import ExamplePlanner


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
        return ExamplePlanner.from_config(config)

    @abstractmethod
    def execute_plan(self, plan: StepwisePlan):
        pass

    @abstractmethod
    def termination_evaluation(self):
        pass
