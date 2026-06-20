import json
import os
import random
from typing import Dict, Generator, List, Optional
import yaml

import bddl
from numpy.typing import ArrayLike as NumpyArrayLike
import omnigibson as og
from omnigibson import object_states
from omnigibson.tasks import BehaviorTask
from omnigibson.utils.bddl_utils import BEHAVIOR_ACTIVITIES
from PIL import Image
import torch

from .data_utils import (
    CUSTOMIZED_BEHAVIOR_ACTIVITIES, 
    get_customized_definition_filename,
    colorize_bboxes
)
from og_ego_prim.benchmark.base_benchmark import Benchmark
from og_ego_prim.benchmark.evaluator.evaluator import Evaluator
from og_ego_prim.benchmark.tracker.online_tracker import OnlineEvalTracker
from og_ego_prim.primitives import Executor
from og_ego_prim.primitives.executor import LowLevelStepContext
from og_ego_prim.primitives.specs import PrimitiveType, starter_evaluation_action
from og_ego_prim.primitives.object_states_utils import (
    is_target_object_predicate_with_obj, 
    find_task_related_object,
    get_visible_task_related_objects,
)
from og_ego_prim.scene_graph import PerceptionSceneGraphUpdater, SceneGraphUpdater
from og_ego_prim.utils.constants import CAMERAS, SCENES
from og_ego_prim.utils.types import PoseCoord, StepwisePlan


__all__ = ['ONLINE_BENCHMARKS']


class OnlineBenchmark(Benchmark):
    
    env: og.Environment
    ego_view: bool
    draw_bbox_2d: bool
    surrounding_poses: List[PoseCoord]

    executor: Executor
    evaluator: Evaluator
    tracker: OnlineEvalTracker
    scene_graph_updater: SceneGraphUpdater

    def __init__(
        self,
        task: str, 
        scene: str, 
        config: Dict, 
        debug: bool,
        ego_view: bool, 
        draw_bbox_2d: bool,
        primitive_type: PrimitiveType,
        scene_graph_step_interval: int,
        scene_graph_backend: str,
        use_initial_setup: bool,
        use_self_caption: bool,
        eval_process_safety: bool,
        eval_termination_safety: bool,
        eval_awareness: bool, 
        eval_execution: bool,
    ):
        super().__init__(task, scene, config, debug, False, primitive_type=primitive_type)

        self._configure_scene_graph_sensors(scene_graph_backend)
        self.env = og.Environment(configs=self.env_config)
        self._apply_robot_initial_pose(config)
        self.ego_view = ego_view
        self.draw_bbox_2d = draw_bbox_2d
        self.primitive_type = primitive_type
        self._starter_grasped_object = None
        if scene_graph_step_interval < 0:
            raise ValueError("scene_graph_step_interval must be greater than or equal to zero")
        self.scene_graph_step_interval = scene_graph_step_interval
        self.use_initial_setup = use_initial_setup
        self.use_self_caption = use_self_caption
            
        camera_config = os.path.join(CAMERAS, 'camera.json')
        with open(camera_config, 'r') as f:
            camera_config = json.load(f)
        room = config['scene_info']['room']

        self.surrounding_poses = None
        if camera_config.get(f'{room}__{scene}', None):
            camera_config = camera_config[f'{room}__{scene}']
            
            self.surrounding_poses = []
            for pose_dict in camera_config:
                self.surrounding_poses.append(
                    (torch.tensor(pose_dict['pos']), torch.tensor(pose_dict['quat']))
                )
        self._add_task_specific_surrounding_poses()

        self.tracker = OnlineEvalTracker()
        self.tracker.task = self.task_name
        self.tracker.scene = self.scene_name
        self.tracker.primitive_type = primitive_type

        self.scene_graph_updater = PerceptionSceneGraphUpdater(backend_name=scene_graph_backend)
        initial_scene_graph = self.scene_graph_updater.reset(self.env)
        self.tracker.track_scene_graph(initial_scene_graph, force=True)
        
        self.executor = Executor(
            self.env,
            primitive_type=primitive_type,
            debug=debug,
            step_callback=self._on_low_level_step if self.scene_graph_step_interval > 0 else None,
        )
        self.evaluator = Evaluator(
            self.env, config, self.tracker,
            eval_process_safety, 
            eval_termination_safety, 
            eval_awareness, 
            eval_execution
        )
        
        self.task_instruction = self._get_task_information(config)[0]
        self.initial_setup = self._get_task_information(config)[1]

        self.set_viewer()
        self._add_extra_init_states()
        self._refresh_scene_graph(force=True)

    def _configure_scene_graph_sensors(self, backend_name: str):
        backend_name = backend_name.strip().lower()
        if backend_name in {'none', 'disabled', 'truth', 'omnigibson_truth', 'unigoal_memory'}:
            return

        required_modalities = {'rgb', 'depth', 'depth_linear', 'camera_params'}
        image_height = int(os.environ.get('ISBENCH_SCENE_GRAPH_IMAGE_HEIGHT', '256'))
        image_width = int(os.environ.get('ISBENCH_SCENE_GRAPH_IMAGE_WIDTH', '256'))
        if image_height <= 0 or image_width <= 0:
            raise ValueError('ISBENCH_SCENE_GRAPH_IMAGE_HEIGHT/WIDTH must be greater than zero')

        robot_configs = self.env_config.get('robots', [])
        if isinstance(robot_configs, dict):
            robot_configs = robot_configs.values()

        for robot_config in robot_configs:
            obs_modalities = robot_config.get('obs_modalities', [])
            if obs_modalities != 'all':
                if isinstance(obs_modalities, str):
                    obs_modalities = [obs_modalities]
                robot_config['obs_modalities'] = sorted(set(obs_modalities) | required_modalities)

            sensor_config = robot_config.setdefault('sensor_config', {})
            vision_config = sensor_config.setdefault('VisionSensor', {})
            if vision_config is None:
                vision_config = {}
                sensor_config['VisionSensor'] = vision_config
            if vision_config.get('modalities') != 'all':
                modalities = vision_config.get('modalities')
                if modalities is not None:
                    if isinstance(modalities, str):
                        modalities = [modalities]
                    vision_config['modalities'] = sorted(set(modalities) | required_modalities)

            sensor_kwargs = vision_config.setdefault('sensor_kwargs', {})
            sensor_kwargs['image_height'] = image_height
            sensor_kwargs['image_width'] = image_width

    def _on_low_level_step(self, context: LowLevelStepContext):
        if self.scene_graph_step_interval <= 0:
            return
        if context.step_index % self.scene_graph_step_interval != 0:
            return
        snapshot = self.scene_graph_updater.update(context)
        self.tracker.track_scene_graph(snapshot)

    def _refresh_scene_graph(self, force: bool = False):
        snapshot = self.scene_graph_updater.update()
        self.tracker.track_scene_graph(snapshot, force=force)

    def _sync_starter_grasped_object(self):
        obj_in_hand = self.executor.controller._get_obj_in_hand()
        if obj_in_hand is None:
            self._starter_grasped_object = None
            return

        for object_name, object_ref in self.env.task.object_scope.items():
            if object_ref.wrapped_obj is obj_in_hand:
                self._starter_grasped_object = object_name
                return

        self._starter_grasped_object = obj_in_hand.name

    def _apply_robot_initial_pose(self, config: Dict):
        robot_initial_pose = config.get('scene_info', {}).get('robot_initial_pose')
        if not robot_initial_pose or not self.env.robots:
            return

        position = torch.tensor(robot_initial_pose['position'], dtype=torch.float32)
        orientation = torch.tensor(robot_initial_pose['orientation'], dtype=torch.float32)
        self.env.robots[0].set_position_orientation(position=position, orientation=orientation, frame='scene')
        print(
            'Applied task robot initial pose after env load: '
            f"position={robot_initial_pose['position']} "
            f"orientation={robot_initial_pose['orientation']}"
        )

    def _add_task_specific_surrounding_poses(self):
        extra_poses = {
            ('store_apple_and_tissue_box_in_bottom_cabinet', 'Wainscott_0_int'): [
                {
                    'pos': [5.15, 8.95, 1.55],
                    'quat': [
                        0.6360543966293335,
                        0.0,
                        0.0,
                        0.7716442346572876,
                    ],
                },
            ],
        }.get((self.task_name, self.scene_name), [])

        if not extra_poses:
            return
        if self.surrounding_poses is None:
            self.surrounding_poses = []

        for pose_dict in extra_poses:
            self.surrounding_poses.append(
                (
                    torch.tensor(pose_dict['pos'], dtype=torch.float32),
                    torch.tensor(pose_dict['quat'], dtype=torch.float32),
                )
            )

    def get_example_planning(self) -> Generator[str, None, None]:
        for i, plan in enumerate(self._example_planning):
            self.tracker.track_plan(step=i, plan=plan)
            yield plan
            if plan['action'].lower().startswith('done'):
                return

    def set_viewer(self):
        for robot in self.env.robots:
            robot.visible = not self.ego_view
        if self.ego_view:
            self.executor._simulator_loop(5)
        
        if self.draw_bbox_2d:
            og.sim.viewer_camera.add_modality('bbox_2d_tight')

    def _add_extra_init_states(self):
        # set objects in refrigerator to frozen
        refrigerator = find_task_related_object(self.env, 'refrigerator')
        if refrigerator is None:
            return
        
        for _, obj_ref in self.env.task.object_scope.items():
            obj = obj_ref.wrapped_obj
            if obj is None:
                continue
            if not hasattr(obj, 'states'):
                continue
            if object_states.Frozen not in obj.states:
                continue
            if not is_target_object_predicate_with_obj(obj, refrigerator, object_states.Inside):
                continue

            obj.states[object_states.Frozen].set_value(True)
        
        self.executor._simulator_loop(5)
    
    def _get_task_information(self, config: Dict):
        cond_configs = config["planning_context"]
        if not cond_configs:
            return None

        task_instruction = cond_configs['task_instruction']
        initial_setup = cond_configs['initial_setup']

        return task_instruction, initial_setup

    def execute_plan(self, plan: StepwisePlan | str) -> bool:
        if isinstance(plan, str):
            plan: StepwisePlan = dict(action=plan, caution=None)
        
        evaluation_plan = plan
        if self.primitive_type == "starter":
            evaluation_plan = {
                **plan,
                "action": starter_evaluation_action(
                    plan["action"],
                    self._starter_grasped_object,
                ),
            }

        self.evaluator.record_action(evaluation_plan["action"])
        self.evaluator.evaluate_process_safety_goal_condition(evaluation_plan, 'before')

        if self.debug:
            self.executor.execute_plan(plan['action'])
        else:
            try:
                self.executor.execute_plan(plan['action'])
            except Exception as e:
                self.tracker.track_error(
                    action=plan['action'],
                    err_type=e.__class__.__name__,
                    msg=str(e)
                )

        self.evaluator.evaluate_process_safety_goal_condition(evaluation_plan, 'after')
        if self.primitive_type == "starter":
            self._sync_starter_grasped_object()
        if self.scene_graph_step_interval <= 0:
            self._refresh_scene_graph()
        return True

    def evaluate_awareness(self, awareness: str):
        self.evaluator.evaluate_awareness(
            self.task_instruction,
            self.initial_setup,
            awareness
        )

    def termination_evaluation(self):
        self.evaluator.evaluate_execution_goal_condition()
        self.evaluator.evaluate_non_executed_process_safety_goal_condition()
        self.evaluator.evaluate_termination_safety_goal_condition()
        if self.tracker.termination is None:
            self.tracker.track_termination(
                reason='done'
            )

    def reset_viewer_camera(self, pose: PoseCoord):
        if not isinstance(pose[0], torch.Tensor):
            pos, quat = pose
            pos = torch.Tensor(pos)
            quat = torch.Tensor(quat)
            pose = (pos, quat)

        og.sim.viewer_camera.set_position_orientation(*pose)
        self.executor._simulator_loop(5)

    def _preprocess_obs(self) -> NumpyArrayLike:
        obs, info = og.sim.viewer_camera.get_obs()
        rgb = obs['rgb'].cpu().numpy()
        if not self.draw_bbox_2d:
            return rgb

        bbox_2d_data = obs['bbox_2d_tight']
        bbox_2d_info = info['bbox_2d_tight']
        visible_task_related_objects = get_visible_task_related_objects(self.env)

        visible_task_related_bbox_2d_id = []
        for bbox_2d_id, bbox_name in bbox_2d_info.items():
            for obj in visible_task_related_objects:
                if bbox_name in obj.name:
                    visible_task_related_bbox_2d_id.append(bbox_2d_id)
                    break
        visible_task_related_bbox_2d_data = [
            data for data in bbox_2d_data if data[0] in visible_task_related_bbox_2d_id
        ]
        rgb_with_bbox_2d = colorize_bboxes(visible_task_related_bbox_2d_data, rgb, bbox_2d_info, num_channels=4)
        return rgb_with_bbox_2d

    def get_viewer_obs(
        self, 
        pose: Optional[PoseCoord] = None, 
        save_img: Optional[str] = None
    ) -> NumpyArrayLike:
        if pose is not None:
            self.reset_viewer_camera(pose) 
        
        obs = self._preprocess_obs()
        if save_img is not None:
            if os.path.isdir(save_img):
                save_img = os.path.join(save_img, 'obs.png')
            else:
                os.makedirs(os.path.dirname(save_img), exist_ok=True)

            img = Image.fromarray(obs)
            img.save(save_img)

        return obs

    def get_surrounding_viewer_obs(
        self, save_img: Optional[str] = None
    ) -> Optional[List[NumpyArrayLike]]:
        if self.surrounding_poses is None:
            return None

        if save_img is not None:
            if not os.path.exists(save_img):
                os.makedirs(save_img)
            elif not os.path.isdir(save_img):
                raise ValueError(f'surrounding_obs must be saved in a directory')
            
        surrounding_obs = []
        for i, pose in enumerate(self.surrounding_poses):
            save_img_i = None if save_img is None else os.path.join(save_img, f'obs_{i}.png')
            obs_i = self.get_viewer_obs(pose, save_img_i)
            surrounding_obs.append(obs_i)
        video_label = None if save_img is None else os.path.basename(save_img)
        self.tracker.track_video_observations(surrounding_obs, label=video_label)
        return surrounding_obs


class OnlineBehaviorBenchmark(OnlineBenchmark):
    
    def init_env_config(self, task: str, scene: str, config: Dict):
        env_config = os.path.join(og.example_config_path, config['_base_config'])
        with open(env_config, 'r') as f:
            env_config = yaml.load(f, Loader=yaml.FullLoader)

        task_info = config['task_info']
        scene_info = config['scene_info']                
        
        # task customization
        task_name = task_info['task_name']
        assert task_name in BEHAVIOR_ACTIVITIES or task_name in CUSTOMIZED_BEHAVIOR_ACTIVITIES
        if task_name not in BEHAVIOR_ACTIVITIES:
            og.tasks.behavior_task.BEHAVIOR_ACTIVITIES.append(task_name)
            bddl.parsing.get_definition_filename = get_customized_definition_filename

        task_type = task_info['task_type'] if not scene_info['online_object_sampling'] \
            else 'CustomBehaviorTask'
        print(f'Using task type: {task_type}')

        env_config['task'] = {
            'type': task_type,
            'activity_name': task_name,
            'activity_definition_id':  task_info['activity_definition_id'],
            'activity_instance_id':  task_info['activity_instance_id'],
            'predefined_problem': None,
            'online_object_sampling': scene_info['online_object_sampling'],
        }

        if scene is None:
            if 'default_scene_model' in scene_info and scene_info['default_scene_model']:
                scene = scene_info['default_scene_model']
            else:
                scene = random.choice(scene_info['scene_models'])
        assert scene in scene_info['scene_models'], f'task "{task}" is not supported in scene "{scene}"'

        env_config['scene'].update({
            'scene_model': scene,
            'load_task_relevant_only': True if self.debug else False,
            'not_load_object_categories': ['ceilings', 'roof']
        })

        # scene customization
        activity_definition_id = task_info['activity_definition_id']
        activity_instance_id = task_info['activity_instance_id']
        scene_file = BehaviorTask.get_cached_activity_scene_filename(
            scene_model=scene,
            activity_name=task_name,
            activity_definition_id=activity_definition_id,
            activity_instance_id=activity_instance_id,
        )
        # use customized scene if scene_file exists
        scene_file = os.path.join(SCENES, scene, 'json', f'{scene_file}.json')
        if not scene_info['online_object_sampling'] and os.path.exists(scene_file):
            env_config['scene']['scene_file'] = scene_file

        robot_initial_pose = scene_info.get('robot_initial_pose')
        if robot_initial_pose and env_config.get('robots'):
            env_config['robots'][0]['position'] = robot_initial_pose['position']
            env_config['robots'][0]['orientation'] = robot_initial_pose['orientation']
            print(
                'Using task robot initial pose: '
                f"position={robot_initial_pose['position']} "
                f"orientation={robot_initial_pose['orientation']}"
            )

        return env_config


ONLINE_BENCHMARKS = {
    'BehaviorTask': OnlineBehaviorBenchmark,
}
