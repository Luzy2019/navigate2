from __future__ import annotations

import json
import math
import os
import random
import time
from typing import Any, Dict, Generator, List, Optional
import yaml

import bddl
import numpy as np
from bddl.config import get_definition_filename as get_bddl_definition_filename
from numpy.typing import ArrayLike as NumpyArrayLike
from og_ego_prim.benchmark.custom_behavior_task import CustomBehaviorTask
import omnigibson as og
from omnigibson import object_states
from omnigibson.tasks import BehaviorTask
import omnigibson.utils.transform_utils as T
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
from og_ego_prim.config.runtime_config import RuntimeConfig
from og_ego_prim.domain import Action
from og_ego_prim.config.task_definition import (
    inject_execution_goal_into_bddl_problem,
    load_task_definition,
)
from og_ego_prim.agent_runtime import AgentRuntimeController, RuntimeComponents
from og_ego_prim.events import create_event_sink
from og_ego_prim.object_model import create_object_registry
from og_ego_prim.prompting import create_prompt_builder
from og_ego_prim.risk_predictor import create_risk_predictor
from og_ego_prim.primitives import Executor
from og_ego_prim.primitives.executor import LowLevelStepContext
from og_ego_prim.primitives.specs import (
    PrimitiveType,
    get_valid_primitives,
    starter_evaluation_action,
)
from og_ego_prim.primitives.object_states_utils import (
    is_target_object_predicate_with_obj, 
    find_task_related_object,
    get_visible_task_related_objects,
)
from og_ego_prim.scene_graph import PerceptionSceneGraphUpdater, SceneGraphUpdater
from og_ego_prim.scheduler import (
    CallbackSimulationClock,
    ContextTemporalStateAdapter,
    build_scheduler,
)
from og_ego_prim.utils.constants import CAMERAS, SCENES
from og_ego_prim.utils.planning import planner_prompt_entity_ids
from og_ego_prim.utils.types import PoseCoord, StepwisePlan


__all__ = ['ONLINE_BENCHMARKS']


class OnlineBenchmark(Benchmark):
    
    env: og.Environment
    ego_view: bool
    draw_bbox_2d: bool

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
        enable_risk_predictor: bool,
        use_initial_setup: bool,
        use_self_caption: bool,
        eval_process_safety: bool,
        eval_termination_safety: bool,
        eval_awareness: bool, 
        eval_execution: bool,
        runtime_config: Optional[RuntimeConfig] = None,
    ):
        super().__init__(task, scene, config, debug, False, primitive_type=primitive_type)
        self.runtime_config = runtime_config or RuntimeConfig.defaults()
        self._closed = False
        self._close_report = None

        self._configure_video_sensors()
        self._configure_scene_graph_sensors(self.runtime_config.scene_graph.backend)
        self.env = og.Environment(configs=self.env_config)
        self._apply_scene_runtime_removals(config)
        self._apply_robot_initial_pose(config)
        self._apply_object_initial_poses(config)
        self._apply_object_initial_relations(config)
        self._apply_task_trav_map_obstacles(config)
        self.ego_view = ego_view
        self.draw_bbox_2d = draw_bbox_2d
        self.primitive_type = primitive_type
        self._starter_grasped_object = None
        if scene_graph_step_interval < 0:
            raise ValueError("scene_graph_step_interval must be greater than or equal to zero")
        self.scene_graph_step_interval = scene_graph_step_interval
        self.use_initial_setup = use_initial_setup
        self.use_self_caption = use_self_caption
        self._enable_risk_predictor = enable_risk_predictor
            
        camera_config = os.path.join(CAMERAS, 'camera.json')
        with open(camera_config, 'r') as f:
            camera_config = json.load(f)
        room = config['scene_info']['room']

        self.tracker = OnlineEvalTracker(
            scene_graph_history_interval=self.runtime_config.scene_graph.history_interval,
            video_fps=self.runtime_config.artifacts.video_fps,
        )
        self.tracker.task = self.task_name
        self.tracker.scene = self.scene_name
        self.tracker.primitive_type = primitive_type

        self.scene_graph_updater = PerceptionSceneGraphUpdater(
            scene_graph_config=self.runtime_config.scene_graph,
        )
        self.scene_graph_updater.set_task_entities(
            (config.get("planning_context") or {}).get("object_list") or ()
        )
        self.scene_graph_updater.set_task_categories(
            planner_prompt_entity_ids(
                (config.get("planning_context") or {}).get("object_list") or ()
            )
        )
        self._feed_task_scene_context(config)
        first_subtask = next(iter(config.get("subtasks") or ()), {})
        initial_instruction = str(
            first_subtask.get("L")
            or config.get("planning_context", {}).get("task_instruction")
            or ""
        )
        self.scene_graph_updater.set_task_instruction(initial_instruction)
        self.executor = Executor(
            self.env,
            primitive_type=primitive_type,
            debug=debug,
            runtime_config=self.runtime_config,
            step_callback=self._on_low_level_step,
        )
        self._initialize_agent_runtime(task, config)
        self._configure_starter_task_placement_slots(config)
        self.evaluator = Evaluator(
            self.env, self.eval_task_config, self.tracker,
            eval_process_safety, 
            eval_termination_safety, 
            eval_awareness, 
            eval_execution
        )
        self.runtime_controller.components.evaluator = self.evaluator
        self.tracker.runtime_modules['evaluator'] = type(self.evaluator).__name__
        
        self.task_instruction = self.agent_task_view.instruction
        self.initial_setup = list(self.agent_task_view.initial_setup)

        self.set_viewer()
        self._add_extra_init_states()
        # [frame0-fix] Build the first scene graph AFTER the Executor exists so
        # the navigation arm pose (e.g. tucked_high) is already applied. The old
        # order perceived the pre-pose frame (arm spread over the table), whose
        # masks/depth differ from every later frame at the same camera pose; the
        # same object then got two inconsistent 3D centroids and split into two
        # nodes. reset() is the first perception; the forced refresh below emits
        # the second frame with the same arm pose so frame0/frame1 align.
        graph_started_at = time.perf_counter()
        initial_scene_graph = self.scene_graph_updater.reset(self.env)
        self.tracker.track_scene_graph(initial_scene_graph, force=True)
        if self._is_graph_construction(initial_scene_graph):
            self.tracker.track_latency('graph_construction', time.perf_counter() - graph_started_at)
        self._refresh_scene_graph(force=True)

    def close(self):
        """Stop simulation, then release episode-owned runtime relationships."""
        if self._closed:
            return dict(self._close_report or {})

        stop_error = None
        cleanup_error = None
        cleanup_report = None
        simulator_stopped = False
        try:
            simulator_stopped = bool(og.sim.is_stopped())
            if not simulator_stopped:
                og.sim.stop()
                simulator_stopped = bool(og.sim.is_stopped())
        except Exception as exc:
            stop_error = f"{exc.__class__.__name__}: {exc}"

        if simulator_stopped:
            try:
                cleanup_report = self.executor.close()
                if (
                    isinstance(cleanup_report, dict)
                    and int(cleanup_report.get("failed", 0)) > 0
                ):
                    cleanup_error = (
                        "collision-filter cleanup retained "
                        f"{cleanup_report['failed']} failed pair(s) for retry"
                    )
            except Exception as exc:
                cleanup_error = f"{exc.__class__.__name__}: {exc}"
        else:
            cleanup_error = (
                "collision-filter cleanup skipped because the simulator "
                "could not be confirmed stopped"
            )

        self._close_report = {
            "simulator_stopped": simulator_stopped,
            "stop_error": stop_error,
            "cleanup_error": cleanup_error,
            "cleanup": cleanup_report,
        }
        self._closed = bool(
            simulator_stopped
            and stop_error is None
            and cleanup_error is None
        )
        if stop_error or cleanup_error:
            print(
                "[benchmark][close] "
                f"stop_error={stop_error!r} cleanup_error={cleanup_error!r}"
            )
        return dict(self._close_report)

    def _initialize_agent_runtime(self, task: str, config: Dict) -> None:
        definition = load_task_definition(config)
        self.task_definition = definition
        self.agent_task_view = definition.agent
        self.runtime_task_config = definition.runtime
        self.eval_task_config = definition.evaluation
        objects = create_object_registry(
            self.runtime_config.object_model,
            task_view=definition.agent,
        )
        scheduler_config = {
            'enabled': self.runtime_config.scheduler.enabled,
            'include_builtins': self.runtime_config.scheduler.handler_options.get(
                'include_builtins', True
            ),
            'processes': {
                str(item.get('process_type') or item.get('name')): item
                for item in self.runtime_config.scheduler.process_definitions
                if isinstance(item, dict) and (item.get('process_type') or item.get('name'))
            },
        }
        scheduler = build_scheduler(
            scheduler_config,
            clock=CallbackSimulationClock(lambda: self.executor.global_step_index),
            state_adapter=ContextTemporalStateAdapter(),
        )
        prompt_builder = create_prompt_builder(self.runtime_config.prompting)
        risk_predictor = None
        if self._enable_risk_predictor:
            risk_config = self.runtime_config.risk.to_dict()
            risk_predictor = create_risk_predictor(
                risk_config,
                task=definition.runtime,
            )
        components = RuntimeComponents(
            perception=self.scene_graph_updater,
            objects=objects,
            scheduler=scheduler,
            prompt_builder=prompt_builder,
            executor=self.executor,
            evaluator=None,
            event_sink=create_event_sink("tracker", self.tracker),
            risk_predictor=risk_predictor,
        )
        self.runtime_controller = AgentRuntimeController(
            components,
            task_id=task,
            task_view=definition.agent,
            expose_cross_subtask_timers=(
                self.runtime_config.scheduler.expose_cross_subtask_timers
            ),
        )
        self.executor.set_pending_heating_process_lookup(
            self._has_pending_heating_process
        )
        self.tracker.planner_episode = self.runtime_controller.planner_episode
        self.tracker.runtime_modules = {
            'schema_version': 'isbench.runtime_modules.v2',
            'perception': type(self.scene_graph_updater).__name__,
            'objects': type(objects).__name__,
            'object_lifecycle_policy': type(objects.lifecycle_policy).__name__,
            'scheduler': type(scheduler).__name__,
            'risk_predictor': type(risk_predictor).__name__ if risk_predictor is not None else None,
            'risk_provider': type(risk_predictor.provider).__name__ if risk_predictor is not None else None,
            'prompt_builder': type(prompt_builder).__name__,
            'executor': type(self.executor).__name__,
            'event_sink': type(components.event_sink).__name__,
            'planner': None,
            'planner_adapter': None,
            'evaluator': None,
            'environment_goal_source': (
                'task_json.evaluation_goal_conditions.execution_goal_condition'
                if config.get('evaluation_goal_conditions', {}).get(
                    'execution_goal_condition'
                )
                else 'bddl.goal'
            ),
            'scheduler_enabled': scheduler.enabled,
            'cross_subtask_timers_configured': (
                self.runtime_config.scheduler.expose_cross_subtask_timers
            ),
            'cross_subtask_timers_exposed': (
                self.runtime_config.scheduler.expose_cross_subtask_timers
            ),
        }
        if risk_predictor is not None:
            self.tracker.risk_predictor = {
                'type': type(risk_predictor).__name__,
                'provider': type(risk_predictor.provider).__name__,
                'task_json_rule_count': len(
                    getattr(getattr(risk_predictor.provider, 'catalog', None), 'rules', ())
                ),
            }
        else:
            self.tracker.risk_predictor = {
                'type': None,
                'provider': None,
                'task_json_rule_count': 0,
            }

    def _has_pending_heating_process(self, target_obj: Any) -> bool:
        """Return whether the scheduler already owns this object's heating wait."""

        scope = getattr(getattr(self.env, "task", None), "object_scope", {}) or {}
        entity_id = next(
            (
                str(candidate_id)
                for candidate_id, reference in scope.items()
                if getattr(reference, "wrapped_obj", None) is target_obj
            ),
            None,
        )
        if entity_id is None:
            return False
        return bool(
            self.runtime_controller.components.scheduler.pending_for(
                entity_id,
                process_type="heating",
            )
        )

    def _configure_video_sensors(self):
        image_size = self.runtime_config.artifacts.sensor_image_size
        if image_size is None:
            return
        image_width, image_height = image_size
        if image_height <= 0 or image_width <= 0:
            raise ValueError("artifacts.sensor_image_size must contain positive dimensions")

        robot_configs = self.env_config.get("robots", [])
        if isinstance(robot_configs, dict):
            robot_configs = robot_configs.values()

        for robot_config in robot_configs:
            sensor_config = robot_config.setdefault("sensor_config", {})
            vision_config = sensor_config.setdefault("VisionSensor", {})
            if vision_config is None:
                vision_config = {}
                sensor_config["VisionSensor"] = vision_config
            sensor_kwargs = vision_config.setdefault("sensor_kwargs", {})
            sensor_kwargs["image_height"] = image_height
            sensor_kwargs["image_width"] = image_width

    def _configure_scene_graph_sensors(self, backend_name: str):
        backend_name = backend_name.strip().lower()
        if backend_name in {'none', 'disabled', 'truth', 'omnigibson_truth', 'unigoal_memory'}:
            return

        required_modalities = {'rgb', 'depth', 'depth_linear', 'camera_params'}
        image_width, image_height = self.runtime_config.scene_graph.image_size
        if image_height <= 0 or image_width <= 0:
            raise ValueError('scene_graph.image_size must contain positive dimensions')

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

    def _feed_task_scene_context(self, config: Dict) -> None:
        """[L3] Feed location/category priors from task JSON to the VLM adapter.

        Uses only ``planning_context.object_list`` + ``initial_setup`` (position
        priors). Goals, safety rules, and abilities are deliberately excluded so
        the VLM does not hallucinate objects/states from task intent.
        """

        object_list = (config.get("planning_context") or {}).get("object_list") or ()
        initial_setup = (config.get("planning_context") or {}).get("initial_setup") or []
        setup_text = " ".join(str(item) for item in initial_setup)
        task_context = [
            (entity_id, setup_text)
            for entity_id in object_list
            if str(entity_id).strip() and not str(entity_id).startswith("agent.")
        ]
        set_task_context = getattr(self.scene_graph_updater, "set_task_context", None)
        if callable(set_task_context):
            set_task_context(task_context)

    def _on_low_level_step(self, context: LowLevelStepContext):
        # Capture this simulator frame before polling timers. A completed
        # process may apply a simulator state and advance one nested frame;
        # doing that first would associate the newer state with this stale
        # LowLevelStepContext and could duplicate interval-based graph builds.
        if (
            self.scene_graph_step_interval > 0
            and context.global_step_index % self.scene_graph_step_interval == 0
        ):
            self._safe_scene_graph_update(context)
        if hasattr(self, 'runtime_controller'):
            self.runtime_controller.tick_scheduler()

    def _refresh_scene_graph(
        self,
        force: bool = False,
        raw_plan: Optional[str] = None,
    ):
        if raw_plan is not None:
            self.scene_graph_updater.set_object_goal_from_action(raw_plan)
        self._safe_scene_graph_update(None, force=force)

    def _safe_scene_graph_update(
        self,
        context: Optional[LowLevelStepContext],
        force: bool = False,
    ):
        """Run one perception update; perception failures must not kill the
        executing primitive. On error, record the error and reuse the last
        snapshot instead of raising into execute_plan. Both interval-based
        (step) and post-primitive refreshes feed the runtime controller."""

        started_at = time.perf_counter()
        try:
            if context is None:
                snapshot = self.scene_graph_updater.update()
            else:
                snapshot = self.scene_graph_updater.update(context)
        except Exception as exc:
            self.tracker.track_error(
                action='scene_graph_update',
                err_type=exc.__class__.__name__,
                msg=str(exc),
            )
            print(
                f"[scene_graph][update_error] {exc.__class__.__name__}: {exc}",
                flush=True,
            )
            return None
        if self._is_graph_construction(snapshot):
            self.tracker.track_latency('graph_construction', time.perf_counter() - started_at)
        self.tracker.track_scene_graph(snapshot, force=force)
        if hasattr(self, 'runtime_controller'):
            self.runtime_controller.observe(snapshot)
        return snapshot

    @staticmethod
    def _is_graph_construction(snapshot) -> bool:
        metadata = getattr(snapshot, 'metadata', None)
        if metadata is None and isinstance(snapshot, dict):
            metadata = snapshot.get('metadata', {})
        return not bool((metadata or {}).get('perception_skipped', False))

    def set_active_subtask(
        self,
        subtask_index: Optional[int],
    ) -> None:
        if hasattr(self, 'runtime_controller'):
            self.runtime_controller.set_subtask(subtask_index)

    def _sync_starter_grasped_object(self):
        self._starter_grasped_object = self._current_grasped_object_id()

    def _current_grasped_object_id(self) -> Optional[str]:
        get_obj_in_hand = getattr(self.executor.controller, '_get_obj_in_hand', None)
        if get_obj_in_hand is None:
            return None
        try:
            obj_in_hand = get_obj_in_hand()
        except Exception:
            return None
        if obj_in_hand is None:
            return None

        for object_name, object_ref in self.env.task.object_scope.items():
            if object_ref.wrapped_obj is obj_in_hand:
                return object_name

        return obj_in_hand.name

    def _configure_starter_task_placement_slots(self, config: Dict):
        scene_info = config.get('scene_info', {})
        placement_slots = scene_info.get('object_placement_slots')
        if not placement_slots:
            return

        controller = getattr(self.executor, 'controller', None)
        configure = getattr(controller, 'configure_task_placement_slots', None)
        if configure is None:
            return

        configure(placement_slots)

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

    def _apply_task_trav_map_obstacles(self, config: Dict):
        scene_info = config.get('scene_info', {})
        object_names = scene_info.get('trav_map_obstacle_objects', [])
        if not object_names:
            return

        padding = float(scene_info.get('trav_map_obstacle_padding', 0.0))
        if padding < 0.0:
            raise ValueError('scene_info.trav_map_obstacle_padding must be non-negative')

        scene = getattr(self.env, 'scene', None)
        trav_map = getattr(scene, 'trav_map', None)
        floor_maps = getattr(trav_map, 'floor_map', None)
        floor_heights = getattr(trav_map, 'floor_heights', None)
        if scene is None or trav_map is None or floor_maps is None or not floor_heights:
            raise RuntimeError(
                'Could not add task traversability-map obstacles: the loaded '
                'scene does not expose trav_map floor data'
            )

        for object_name in object_names:
            obj = scene.object_registry('name', object_name, default_val=None)
            if obj is None:
                raise KeyError(
                    f'Task traversability-map obstacle not found: {object_name}'
                )

            lower, upper = (torch.as_tensor(bound, dtype=torch.float32) for bound in obj.aabb)
            center_z = float(((lower[2] + upper[2]) * 0.5).item())
            floor = min(
                range(len(floor_heights)),
                key=lambda idx: abs(center_z - float(floor_heights[idx])),
            )
            floor_map = floor_maps[floor]

            world_lower = lower[:2] - padding
            world_upper = upper[:2] + padding
            map_corners = torch.stack(
                [
                    torch.as_tensor(
                        trav_map.world_to_map(torch.stack([x, y])),
                        dtype=torch.int64,
                    )
                    for x in (world_lower[0], world_upper[0])
                    for y in (world_lower[1], world_upper[1])
                ]
            )
            height, width = floor_map.shape
            row_min = max(0, int(map_corners[:, 0].min().item()) - 1)
            row_max = min(height - 1, int(map_corners[:, 0].max().item()) + 1)
            col_min = max(0, int(map_corners[:, 1].min().item()) - 1)
            col_max = min(width - 1, int(map_corners[:, 1].max().item()) + 1)

            changed_cells = 0
            for row in range(row_min, row_max + 1):
                for col in range(col_min, col_max + 1):
                    world_xy = torch.as_tensor(
                        trav_map.map_to_world(torch.tensor([row, col], dtype=torch.int64)),
                        dtype=torch.float32,
                    )
                    if not bool(
                        torch.all(world_xy >= world_lower).item()
                        and torch.all(world_xy <= world_upper).item()
                    ):
                        continue
                    if int(floor_map[row, col]) != 0:
                        changed_cells += 1
                    floor_map[row, col] = 0

            print(
                'Applied task traversability-map obstacle: '
                f'object={object_name} floor={floor} padding={padding:.3f} '
                f'changed_cells={changed_cells}'
            )

    def _apply_scene_runtime_removals(self, config: Dict):
        scene_info = config.get('scene_info', {})
        object_names = scene_info.get('scene_file_remove_objects', [])
        categories = scene_info.get('scene_file_remove_object_categories', [])
        if not object_names and not categories:
            return

        scene = getattr(self.env, 'scene', None)
        if scene is None:
            print('[benchmark][warning] Could not apply task runtime scene removals: missing scene')
            return

        objects_to_remove = []
        seen_names = set()
        for category in categories:
            try:
                category_objects = list(
                    scene.object_registry('category', category, default_val=[])
                )
            except Exception as exc:
                print(
                    '[benchmark][warning] Could not query runtime scene removal category: '
                    f'category={category!r} error={type(exc).__name__}: {exc}'
                )
                continue
            for obj in category_objects:
                if obj.name not in seen_names:
                    seen_names.add(obj.name)
                    objects_to_remove.append(obj)

        for object_name in object_names:
            obj = None
            try:
                obj = scene.object_registry('name', object_name, default_val=None)
            except Exception:
                obj = None
            if obj is None:
                task_obj = getattr(getattr(self.env, 'task', None), 'object_scope', {}).get(object_name)
                obj = getattr(task_obj, 'wrapped_obj', None) if task_obj is not None else None
            if obj is None:
                print(
                    '[benchmark][warning] Runtime scene removal target not found: '
                    f'object={object_name}'
                )
                continue
            if obj.name not in seen_names:
                seen_names.add(obj.name)
                objects_to_remove.append(obj)

        if not objects_to_remove:
            return

        free_object_names = set(scene_info.get('trav_map_free_objects', []))
        free_object_padding = float(scene_info.get('trav_map_free_object_padding', 0.0))
        if free_object_padding < 0.0:
            raise ValueError('scene_info.trav_map_free_object_padding must be non-negative')
        missing_free_objects = free_object_names - seen_names
        if missing_free_objects:
            raise KeyError(
                'Task traversability-map free objects must also be runtime removal targets: '
                f'{sorted(missing_free_objects)}'
            )

        removed_object_markers = self._removed_object_markers(
            objects_to_remove,
            object_names=free_object_names,
        )
        if removed_object_markers:
            self._apply_removed_object_trav_map_updates(
                removed_object_markers,
                padding=free_object_padding,
            )

        removed_door_markers = self._removed_door_markers(objects_to_remove)
        if removed_door_markers:
            self._apply_removed_door_trav_map_updates(removed_door_markers)

        og.sim.batch_remove_objects(objects_to_remove)
        print(
            'Applied task runtime scene removals: '
            f"objects={[obj.name for obj in objects_to_remove]}"
        )

    def _removed_door_markers(self, objects_to_remove: List) -> List[Dict]:
        return self._removed_object_markers(
            objects_to_remove,
            categories={'door'},
        )

    def _removed_object_markers(
        self,
        objects_to_remove: List,
        object_names=None,
        categories=None,
    ) -> List[Dict]:
        markers = []
        for obj in objects_to_remove:
            if object_names is not None and obj.name not in object_names:
                continue
            category = str(getattr(obj, 'category', '')).lower()
            if categories is not None and category not in categories:
                continue

            try:
                center, orientation, extent, _ = obj.get_base_aligned_bbox(visual=False)
            except Exception as exc:
                print(
                    '[benchmark][warning] Could not read removed object bbox: '
                    f'object={getattr(obj, "name", None)} '
                    f'error={type(exc).__name__}: {exc}'
                )
                continue

            center = torch.as_tensor(center, dtype=torch.float32)
            orientation = torch.as_tensor(orientation, dtype=torch.float32)
            extent = torch.as_tensor(extent, dtype=torch.float32)
            axes_world = [
                torch.as_tensor(
                    T.quat_apply(orientation, torch.eye(3, dtype=torch.float32)[axis]),
                    dtype=torch.float32,
                )
                for axis in range(3)
            ]
            horizontal_axes = [
                axis for axis, axis_world in enumerate(axes_world)
                if float(torch.norm(axis_world[:2]).item()) >= 0.35
            ]
            if len(horizontal_axes) < 2:
                print(
                    '[benchmark][warning] Could not identify removed object horizontal axes: '
                    f'object={obj.name} extent={self._to_float_list(extent)}'
                )
                continue

            long_axis_index = max(horizontal_axes, key=lambda axis: float(extent[axis].item()))
            normal_axis_index = min(horizontal_axes, key=lambda axis: float(extent[axis].item()))
            long_axis_xy = self._normalized_xy_axis(axes_world[long_axis_index])
            normal_axis_xy = self._normalized_xy_axis(axes_world[normal_axis_index])
            if long_axis_xy is None or normal_axis_xy is None:
                continue

            marker = {
                'name': obj.name,
                'center': self._to_float_list(center),
                'orientation': self._to_float_list(orientation),
                'extent': self._to_float_list(extent),
                'long_axis_xy': self._to_float_list(long_axis_xy),
                'normal_axis_xy': self._to_float_list(normal_axis_xy),
                'length': float(extent[long_axis_index].item()),
                'thickness': float(extent[normal_axis_index].item()),
            }
            markers.append(marker)

        return markers

    def _apply_removed_object_trav_map_updates(
        self,
        object_markers: List[Dict],
        padding: float = 0.0,
    ):
        scene = getattr(self.env, 'scene', None)
        trav_map = getattr(scene, 'trav_map', None)
        floor_maps = getattr(trav_map, 'floor_map', None)
        if trav_map is None or floor_maps is None:
            raise RuntimeError(
                'Could not update trav_map for removed objects: missing trav_map.floor_map'
            )

        map_resolution = float(getattr(trav_map, 'map_resolution', 0.1) or 0.1)
        floor_heights = getattr(trav_map, 'floor_heights', None) or [0.0]
        minimum_half_extent = map_resolution * 0.5

        for marker in object_markers:
            center = torch.tensor(marker['center'], dtype=torch.float32)
            long_axis = torch.tensor(marker['long_axis_xy'], dtype=torch.float32)
            normal_axis = torch.tensor(marker['normal_axis_xy'], dtype=torch.float32)
            floor = int(
                min(
                    range(len(floor_heights)),
                    key=lambda idx: abs(float(center[2]) - float(floor_heights[idx])),
                )
            )
            if floor < 0 or floor >= len(floor_maps):
                continue

            long_half_extent = max(
                float(marker['length']) * 0.5 + padding,
                minimum_half_extent,
            )
            normal_half_extent = max(
                float(marker['thickness']) * 0.5 + padding,
                minimum_half_extent,
            )
            changed_cells = self._free_oriented_trav_map_region(
                trav_map,
                floor_maps[floor],
                center[:2],
                long_axis,
                normal_axis,
                long_half_extent,
                normal_half_extent,
            )
            print(
                'Applied removed-object trav_map update: '
                f"object={marker['name']} floor={floor} "
                f"center={marker['center'][:2]} "
                f"long_half_extent={long_half_extent:.3f} "
                f"normal_half_extent={normal_half_extent:.3f} "
                f"padding={padding:.3f} map_resolution={map_resolution:.3f} "
                f"changed_cells={changed_cells}"
            )

    def _apply_removed_door_trav_map_updates(self, door_markers: List[Dict]):
        scene = getattr(self.env, 'scene', None)
        trav_map = getattr(scene, 'trav_map', None)
        floor_maps = getattr(trav_map, 'floor_map', None)
        if trav_map is None or floor_maps is None:
            print('[benchmark][warning] Could not update trav_map for removed doors: missing trav_map.floor_map')
            return

        map_resolution = float(getattr(trav_map, 'map_resolution', 0.1) or 0.1)
        floor_heights = getattr(trav_map, 'floor_heights', None) or [0.0]
        robot_clearance = self._robot_trav_map_clearance()

        for marker in door_markers:
            center = torch.tensor(marker['center'], dtype=torch.float32)
            long_axis = torch.tensor(marker['long_axis_xy'], dtype=torch.float32)
            normal_axis = torch.tensor(marker['normal_axis_xy'], dtype=torch.float32)
            floor = int(
                min(
                    range(len(floor_heights)),
                    key=lambda idx: abs(float(center[2]) - float(floor_heights[idx])),
                )
            )
            if floor < 0 or floor >= len(floor_maps):
                continue

            long_half_extent = max(float(marker['length']) * 0.5 + 0.20, 0.45)
            normal_half_extent = max(
                float(marker['thickness']) * 0.5 + robot_clearance + 0.08,
                0.45,
            )
            changed_cells = self._free_oriented_trav_map_region(
                trav_map,
                floor_maps[floor],
                center[:2],
                long_axis,
                normal_axis,
                long_half_extent,
                normal_half_extent,
            )
            print(
                'Applied removed-door trav_map update: '
                f"door={marker['name']} floor={floor} "
                f"center={marker['center'][:2]} "
                f"long_half_extent={long_half_extent:.3f} "
                f"normal_half_extent={normal_half_extent:.3f} "
                f"map_resolution={map_resolution:.3f} "
                f"changed_cells={changed_cells}"
            )

    def _free_oriented_trav_map_region(
        self,
        trav_map,
        floor_map,
        center_xy: torch.Tensor,
        long_axis: torch.Tensor,
        normal_axis: torch.Tensor,
        long_half_extent: float,
        normal_half_extent: float,
    ) -> int:
        corners = [
            center_xy
            + long_axis * long_sign * long_half_extent
            + normal_axis * normal_sign * normal_half_extent
            for long_sign in (-1.0, 1.0)
            for normal_sign in (-1.0, 1.0)
        ]
        map_corners = [
            torch.as_tensor(trav_map.world_to_map(corner), dtype=torch.float32)
            for corner in corners
        ]
        height, width = floor_map.shape
        row_min = max(0, int(math.floor(min(corner[0] for corner in map_corners).item())) - 1)
        row_max = min(height - 1, int(math.ceil(max(corner[0] for corner in map_corners).item())) + 1)
        col_min = max(0, int(math.floor(min(corner[1] for corner in map_corners).item())) - 1)
        col_max = min(width - 1, int(math.ceil(max(corner[1] for corner in map_corners).item())) + 1)

        changed_cells = 0
        for row in range(row_min, row_max + 1):
            for col in range(col_min, col_max + 1):
                world_xy = torch.as_tensor(
                    trav_map.map_to_world(torch.tensor([row, col], dtype=torch.int64)),
                    dtype=torch.float32,
                )
                rel_xy = world_xy - center_xy
                long_offset = float(torch.dot(rel_xy, long_axis).item())
                normal_offset = float(torch.dot(rel_xy, normal_axis).item())
                if (
                    abs(long_offset) <= long_half_extent
                    and abs(normal_offset) <= normal_half_extent
                ):
                    if int(floor_map[row, col]) != 255:
                        changed_cells += 1
                    floor_map[row, col] = 255
        return changed_cells

    def _robot_trav_map_clearance(self) -> float:
        if not self.env.robots:
            return 0.35
        extent = getattr(self.env.robots[0], 'reset_joint_pos_aabb_extent', None)
        if extent is None:
            return 0.35
        extent = torch.as_tensor(extent, dtype=torch.float32)
        if extent.numel() < 2:
            return 0.35
        scale = float(self.runtime_config.navigation.doorway_clearance_radius_scale)
        if not 0.0 < scale <= 1.0:
            raise ValueError(
                'navigation.doorway_clearance_radius_scale must be in (0, 1]'
            )
        return float(torch.norm(extent[:2]).item()) * 0.5 * scale

    @staticmethod
    def _normalized_xy_axis(axis_world):
        axis_xy = torch.as_tensor(axis_world, dtype=torch.float32)[:2]
        norm = torch.norm(axis_xy)
        if float(norm.item()) < 1e-6:
            return None
        return axis_xy / norm

    @staticmethod
    def _to_float_list(value):
        if value is None:
            return None
        if hasattr(value, 'detach'):
            value = value.detach().cpu()
        if hasattr(value, 'tolist'):
            value = value.tolist()
        return [round(float(item), 6) for item in value]

    def _apply_object_initial_poses(self, config: Dict):
        object_initial_poses = config.get('scene_info', {}).get('object_initial_poses', {})
        if not object_initial_poses:
            return

        for object_name, pose_config in object_initial_poses.items():
            obj = self._resolve_initial_pose_object(object_name)
            if obj is None:
                print(
                    '[benchmark][warning] Could not apply task object initial pose: '
                    f'object={object_name!r} was not found'
                )
                continue

            position = pose_config.get('position')
            orientation = pose_config.get('orientation')
            if position is None:
                print(
                    '[benchmark][warning] Could not apply task object initial pose: '
                    f'object={object_name!r} has no position'
                )
                continue

            position = torch.tensor(position, dtype=torch.float32)
            orientation = (
                None
                if orientation is None
                else torch.tensor(orientation, dtype=torch.float32)
            )
            if orientation is None:
                obj.set_position_orientation(position=position)
            else:
                obj.set_position_orientation(position=position, orientation=orientation)
            obj.keep_still()

            print(
                'Applied task object initial pose after env load: '
                f"object={object_name} simulator_object={obj.name} "
                f"category={getattr(obj, 'category', None)} "
                f"model={getattr(obj, 'model', None)} "
                f"position={pose_config['position']} "
                f"orientation={pose_config.get('orientation')}"
            )

    def _apply_object_initial_relations(self, config: Dict):
        object_initial_relations = config.get('scene_info', {}).get('object_initial_relations', [])
        if not object_initial_relations:
            return

        predicate_map = {
            'inside': object_states.Inside,
            'ontop': object_states.OnTop,
            'on_top': object_states.OnTop,
            'nextto': object_states.NextTo,
            'next_to': object_states.NextTo,
        }

        for relation in object_initial_relations:
            if not isinstance(relation, dict):
                print(
                    '[benchmark][warning] Could not apply task object initial relation: '
                    f'invalid relation entry {relation!r}'
                )
                continue

            predicate_name = str(relation.get('predicate', '')).strip().lower()
            predicate = predicate_map.get(predicate_name)
            object_name = relation.get('object')
            target_name = relation.get('target')

            if predicate is None or not object_name or not target_name:
                print(
                    '[benchmark][warning] Could not apply task object initial relation: '
                    f'invalid relation entry {relation!r}'
                )
                continue

            obj = self._resolve_initial_pose_object(object_name)
            target_obj = self._resolve_initial_pose_object(target_name)
            if obj is None or target_obj is None:
                print(
                    '[benchmark][warning] Could not apply task object initial relation: '
                    f'object={object_name!r} resolved={obj is not None} '
                    f'target={target_name!r} resolved={target_obj is not None}'
                )
                continue
            if predicate not in getattr(obj, 'states', {}):
                print(
                    '[benchmark][warning] Could not apply task object initial relation: '
                    f'object={object_name!r} has no state {predicate.__name__}'
                )
                continue

            # Initial poses are applied immediately before this pass.  Calling
            # OnTop.set_value unconditionally can resample a valid pose and
            # move dynamic objects to a different random point on the support.
            # Preserve an already-satisfied relation so scene initialization is
            # deterministic; only invoke the setter when the pose needs repair.
            try:
                already_satisfied = bool(obj.states[predicate].get_value(target_obj))
            except Exception:
                already_satisfied = False
            if already_satisfied:
                print(
                    'Task object initial relation already satisfied; '
                    f'skipping setter: object={object_name} target={target_name} '
                    f'predicate={predicate_name}'
                )
                continue

            try:
                sampling_seed = relation.get('sampling_seed')
                rng_state = None
                if sampling_seed is not None:
                    try:
                        sampling_seed = int(sampling_seed)
                    except (TypeError, ValueError):
                        print(
                            '[benchmark][warning] Could not apply task object initial relation: '
                            f'object={object_name!r} target={target_name!r} '
                            f'invalid sampling_seed={sampling_seed!r}'
                        )
                        continue
                    rng_state = (
                        random.getstate(),
                        np.random.get_state(),
                        torch.random.get_rng_state(),
                    )
                    random.seed(sampling_seed)
                    np.random.seed(sampling_seed)
                    torch.manual_seed(sampling_seed)
                try:
                    success = obj.states[predicate].set_value(
                        target_obj,
                        True,
                        reset_before_sampling=False,
                    )
                except TypeError:
                    success = obj.states[predicate].set_value(target_obj, True)
            except Exception as exc:
                print(
                    '[benchmark][warning] Could not apply task object initial relation: '
                    f'object={object_name} target={target_name} '
                    f'predicate={predicate_name} error={type(exc).__name__}: {exc}'
                )
                continue
            finally:
                if rng_state is not None:
                    random.setstate(rng_state[0])
                    np.random.set_state(rng_state[1])
                    torch.random.set_rng_state(rng_state[2])

            obj.keep_still()
            target_obj.keep_still()
            satisfied = bool(obj.states[predicate].get_value(target_obj))
            print(
                'Applied task object initial relation after env load: '
                f"object={object_name} simulator_object={obj.name} "
                f"target={target_name} simulator_target={target_obj.name} "
                f"predicate={predicate_name} setter_success={success} "
                f"satisfied={satisfied}"
            )

    def _resolve_initial_pose_object(self, object_name: str):
        '''
            把任务对象名解析为 simulator 中的对象引用，优先使用 task.object_scope 中的对象。
            如果 task.object_scope 中没有，则尝试在 simulator 的 object_registry 中查找
            如果 simulator 中也没有，则尝试在 task.object_scope 中查找与任务相关的对象
            （例如 task.object_scope 中的 "refrigerator" 对象可能对应 simulator 中的 "refrigerator_0" 对象）。
            如果以上都没有找到，则返回 None。
        '''
        object_ref = self.env.task.object_scope.get(object_name)
        if object_ref is not None:
            return object_ref.wrapped_obj

        obj = self.env.scene.object_registry('name', object_name)
        if obj is not None:
            return obj

        return find_task_related_object(self.env, object_name)

    def get_example_planning(self) -> Generator[str, None, None]:
        for i, plan in enumerate(self._example_planning):
            self.tracker.track_plan(step=i, plan=plan)
            yield plan
            if plan['action'].lower().startswith('done'):
                return

    def set_viewer(self):
        '''
            ego：第一人称视角
        '''
        for robot in self.env.robots:
            robot.visible = not self.ego_view
        # 切到 ego / 第一人称视角后，让仿真先空跑 5 步稳定一下画面和机器人状态。
        if self.ego_view:
            self.executor._simulator_loop(5)
        
        if self.draw_bbox_2d:
            og.sim.viewer_camera.add_modality('bbox_2d_tight')

    # 在仿真中添加额外的初始状态
    # 例如：在任务初始化后，把冰箱里的、支持 Frozen 状态的任务对象设成“冷冻”状态。
    def _add_extra_init_states(self):
        '''
            这个功能主要服务于一些烹饪/加热任务：
            比如从冰箱拿出来的食材需要先 WAIT(...) 解冻，或者安全条件要求 not frozen 后才能烹饪。
            它确保“冰箱里的食物一开始是冷冻的”这个物理/状态前提真的写入 OmniGibson 状态里。
        '''
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
    
    # 解析配置文件中的 task_instruction 和 initial_setup 信息
    # 返回 (task_instruction, initial_setup)。
    def _get_task_information(self, config: Dict):
        cond_configs = config["planning_context"]
        if not cond_configs:
            return None

        task_instruction = cond_configs['task_instruction']
        initial_setup = cond_configs['initial_setup']

        return task_instruction, initial_setup

    def bind_planner_adapter(
        self,
        planner,
        *,
        source: Optional[str] = None,
        emit_proposals: bool = False,
    ):
        bound = self.runtime_controller.bind_planner(
            planner,
            emit_proposals=emit_proposals,
        )
        self.tracker.runtime_modules['planner'] = source or type(bound).__name__
        self.tracker.runtime_modules['planner_adapter'] = type(bound).__name__
        return bound

    def execute_plan(self, plan: StepwisePlan | Action | str) -> bool:
        execution_started_at = time.perf_counter()
        planning_latency = self.tracker.consume_planning_latency()
        try:
            return self._execute_plan(plan)
        finally:
            action_execution_latency = time.perf_counter() - execution_started_at
            self.tracker.track_latency('action_execution', action_execution_latency)
            self.tracker.track_latency('total', planning_latency + action_execution_latency)

    def _execute_plan(self, plan: StepwisePlan | Action | str) -> bool:
        if isinstance(plan, Action):
            plan = {
                'action': plan.to_legacy_plan(),
                'caution': plan.extensions.get('caution'),
            }
        if isinstance(plan, str):
            plan: StepwisePlan = dict(action=plan, caution=None)

        proposal_action = plan['action']
        runtime_review = self.runtime_controller.review_action(
            self._runtime_action(proposal_action)
        )
        if runtime_review.risk_evaluation is not None:
            self.tracker.track_risk_evaluation(
                proposal_action,
                runtime_review.risk_evaluation,
            )
            risk_latency = getattr(
                self.runtime_controller,
                'last_risk_latency',
                None,
            )
            if risk_latency is not None:
                self.tracker.track_latency('risk_prediction', risk_latency)
        plan = dict(plan)
        plan['runtime_decision'] = runtime_review.decision.value
        if not runtime_review.allowed:
            outcome = self.runtime_controller.record_blocked(runtime_review)
            self.tracker.mark_plan_runtime(
                proposal_action,
                executed=False,
                succeeded=False,
                runtime_decision=runtime_review.decision.value,
                runtime_reason=outcome.reason,
            )
            return False

        execution_plan = {
            **plan,
            'action': runtime_review.action.to_legacy_plan(),
        }
        evaluation_plan = execution_plan
        if self.primitive_type == "starter":
            evaluation_plan = {
                **execution_plan,
                # action wrapper
                "action": starter_evaluation_action(
                    execution_plan["action"],
                    self._starter_grasped_object,
                ),
            }

        self._evaluate_lifelong_process_safety(evaluation_plan, "before")
        self.evaluator.evaluate_process_safety_goal_condition(evaluation_plan, 'before')

        execution_succeeded = True
        if self.debug:
            self.executor.execute_plan(execution_plan['action'])
        else:
            try:
                self.executor.execute_plan(execution_plan['action'])
            except Exception as e:
                execution_succeeded = False
                print(
                    f"[benchmark][execution_error] action={execution_plan['action']!r} "
                    f"type={e.__class__.__name__} message={e}"
                )
                self.tracker.track_error(
                    action=execution_plan['action'],
                    err_type=e.__class__.__name__,
                    msg=str(e)
                )
        self.tracker.track_execution_diagnostic(
            self.executor.last_execution_diagnostics
        )
        outcome = self.runtime_controller.record_execution(
            runtime_review,
            succeeded=execution_succeeded,
            diagnostics=self.executor.last_execution_diagnostics,
        )
        self.tracker.mark_plan_runtime(
            proposal_action,
            executed=True,
            succeeded=execution_succeeded,
            runtime_decision=runtime_review.decision.value,
            runtime_reason=outcome.reason,
        )

        if execution_succeeded:
            self.evaluator.record_action(evaluation_plan["action"])
            self._record_lifelong_action(evaluation_plan["action"])
            self._evaluate_lifelong_process_safety(evaluation_plan, "after")
            self.evaluator.evaluate_process_safety_goal_condition(evaluation_plan, 'after')
        if self.primitive_type == "starter" and execution_succeeded:
            self._sync_starter_grasped_object()
        if self.scene_graph_step_interval <= 0:
            self._refresh_scene_graph(raw_plan=execution_plan['action'])
        return execution_succeeded

    def _record_lifelong_action(self, action: str) -> None:
        """Record one successfully executed action for lifelong termination gating."""

        evaluator = getattr(self, "_lifelong_evaluator", None)
        if evaluator is None:
            return
        record = getattr(evaluator, "record_action", None)
        if callable(record):
            record(action)

    def _evaluate_lifelong_process_safety(
        self,
        plan: StepwisePlan,
        phase: str,
    ) -> None:
        """Record lifelong ``G_safe`` checkpoints without affecting execution."""

        evaluator = getattr(self, "_lifelong_evaluator", None)
        if evaluator is None:
            return
        callback = getattr(
            evaluator,
            "evaluate_process_safety_goal_condition",
            None,
        )
        if callable(callback):
            callback(plan, phase)

    def _runtime_action(self, raw_action: str) -> Action:
        action = self.runtime_controller.ground_action(
            Action.from_raw(raw_action, actor_id='Root')
        )
        action.extensions['record_manipulation'] = not (
            action.name in {'NAVIGATE_TO', 'DONE'} or action.name.startswith('WAIT')
        )
        primitive_arity = get_valid_primitives(self.primitive_type).get(action.name)
        implicit_held_action = (
            primitive_arity == 1
            and (action.name.startswith('PLACE_') or action.name == 'POUR_INTO')
        ) or (action.name == 'RELEASE' and primitive_arity == 0)
        held_object = (
            self._current_grasped_object_id() if implicit_held_action else None
        )
        if primitive_arity == 1 and (
            action.name.startswith('PLACE_') or action.name == 'POUR_INTO'
        ):
            destination = action.object_id
            if held_object is not None:
                return Action(
                    name=action.name,
                    actor_id=action.actor_id,
                    object_id=held_object,
                    target_id=destination,
                    parameters={
                        **action.parameters,
                        'executor_arguments': action.parameters.get('arguments', ()),
                    },
                    raw=raw_action,
                    extensions={
                        **action.extensions,
                        'implicit_held_object': True,
                    },
                )
        if action.name == 'RELEASE' and primitive_arity == 0 and held_object is not None:
            return Action(
                name=action.name,
                actor_id=action.actor_id,
                object_id=held_object,
                parameters=action.parameters,
                raw=raw_action,
                extensions={
                    **action.extensions,
                    'implicit_held_object': True,
                },
            )
        return action

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
        self.tracker.finalize_latency()

    def reset_viewer_camera(self, pose: PoseCoord):
        if not isinstance(pose[0], torch.Tensor):
            pos, quat = pose
            pos = torch.Tensor(pos)
            quat = torch.Tensor(quat)
            pose = (pos, quat)

        og.sim.viewer_camera.set_position_orientation(*pose)
        self.executor._simulator_loop(5)

    def _preprocess_obs(self) -> NumpyArrayLike:
        '''
            获取当前仿真环境的观察结果，并根据 draw_bbox_2d 配置决定是否在 rgb 图像上绘制 2D 边界框。
            
            Returns:
                rgb: numpy.ndarray, shape (H, W, 3), dtype uint8

            注：在当前任务中用的是sam2的bbox，所以这里应该设置为：
                self.draw_bbox_2d = False
        '''
        obs, info = og.sim.viewer_camera.get_obs()
        rgb = obs['rgb'].cpu().numpy()
        if not self.draw_bbox_2d:
            return rgb

        # ============= 以下访问不到 ===================
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


class OnlineBehaviorBenchmark(OnlineBenchmark):
    
    def init_env_config(self, task: str, scene: str, config: Dict):
        env_config = os.path.join(og.example_config_path, config['_base_config'])
        with open(env_config, 'r') as f:
            env_config = yaml.load(f, Loader=yaml.FullLoader)

        task_info = config['task_info']
        scene_info = config['scene_info']                
        
        # Get Task BDDL Definition
        task_name = task_info['task_name']
        assert task_name in BEHAVIOR_ACTIVITIES or task_name in CUSTOMIZED_BEHAVIOR_ACTIVITIES
        if task_name not in BEHAVIOR_ACTIVITIES:
            og.tasks.behavior_task.BEHAVIOR_ACTIVITIES.append(task_name)
            bddl.parsing.get_definition_filename = get_customized_definition_filename
        definition_path = (
            get_customized_definition_filename(
                task_name,
                task_info['activity_definition_id'],
            )
            if task_name in CUSTOMIZED_BEHAVIOR_ACTIVITIES
            else get_bddl_definition_filename(
                task_name,
                task_info['activity_definition_id'],
            )
        )
        with open(definition_path, 'r', encoding='utf-8') as file:
            problem_text = file.read() # BDDL Definition

        # 将json文件中的execution_goal_condition 动态替换 BDDL Definition (:goal
        execution_goal = config['evaluation_goal_conditions']['execution_goal_condition']
        predefined_problem = (
            inject_execution_goal_into_bddl_problem(
                problem_text,
                execution_goal,
            )
            if execution_goal
            else problem_text
        )

        if scene_info['online_object_sampling']:
            task_type = 'CustomBehaviorTask'
        else:
            task_type = task_info['task_type'] # default: BehaviorTask
        print(f'Using task type: {task_type}')

        env_config['task'] = {
            'type': task_type, # BehaviorTask / CustomBehaviorTask
            'activity_name': task_name,
            'activity_definition_id':  task_info['activity_definition_id'], # 0
            'activity_instance_id':  task_info['activity_instance_id'], # 0
            # bddl definition with manual execution_goal_condition
            'predefined_problem': predefined_problem,
            # online_object_sampling: True / False
            'online_object_sampling': scene_info['online_object_sampling'],
        }
        required_models = scene_info.get('scene_asset_requirements', {}).get(
            'required_instance_models',
            {},
        )
        if required_models and scene_info['online_object_sampling']:
            env_config['task']['required_instance_models'] = required_models

        if scene is None:
            if 'default_scene_model' in scene_info and scene_info['default_scene_model']:
                scene = scene_info['default_scene_model']
            else:
                scene = random.choice(scene_info['scene_models'])
        assert scene in scene_info['scene_models'], f'task "{task}" is not supported in scene "{scene}"'

        env_config['scene'].update({
            'scene_model': scene, # Beechwood_0_int
            'load_task_relevant_only': True if self.debug else False, # 控制是否只加载任务相关物体，还是加载场景中的全部物体。
            'not_load_object_categories': ['ceilings', 'roof'] # 无论是否调试模式，始终不加载 ceilings（天花板）和 roof（屋顶）这两个类别。
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
            self._validate_cached_scene_requirements(scene_file, required_models)
            scene_file = self._prepare_task_scene_file(scene_file, scene_info)
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

    @staticmethod
    def _validate_cached_scene_requirements(scene_file: str, required_models: Dict) -> None:
        """Reject a fixed cache whose task-relevant asset bindings drifted."""
        if not required_models:
            return
        with open(scene_file, 'r') as f:
            scene_data = json.load(f)
        inst_to_name = scene_data.get('metadata', {}).get('task', {}).get('inst_to_name', {})
        object_init_info = scene_data.get('objects_info', {}).get('init_info', {})
        mismatches = []
        for instance, requirement in required_models.items():
            simulator_name = inst_to_name.get(instance)
            info = object_init_info.get(simulator_name, {}) if simulator_name else {}
            args = info.get('args', {})
            expected_category = requirement.get('dataset_category')
            expected_model = requirement.get('model')
            if simulator_name is None:
                mismatches.append(f'{instance}: missing metadata binding')
                continue
            if expected_category is not None and args.get('category') != expected_category:
                mismatches.append(
                    f'{instance}: category={args.get("category")!r}, expected={expected_category!r}'
                )
            if expected_model is not None and args.get('model') != expected_model:
                mismatches.append(
                    f'{instance}: model={args.get("model")!r}, expected={expected_model!r}'
                )
            expected_scale = requirement.get('scale')
            if expected_scale is not None:
                expected_values = (
                    [float(expected_scale)] * 3
                    if isinstance(expected_scale, (int, float))
                    else [float(value) for value in expected_scale]
                )
                actual_scale = args.get('scale')
                try:
                    actual_values = [float(value) for value in actual_scale]
                except (TypeError, ValueError):
                    actual_values = []
                if len(actual_values) != 3 or any(
                    abs(actual - expected) > 1e-5
                    for actual, expected in zip(actual_values, expected_values)
                ):
                    mismatches.append(
                        f'{instance}: scale={actual_scale!r}, expected={expected_values!r}'
                    )
        if mismatches:
            raise RuntimeError(
                'cached scene asset requirements failed: '
                + '; '.join(mismatches)
                + f"; regenerate {scene_file} with online_object_sampling=True"
            )

    def _prepare_task_scene_file(self, scene_file: str, scene_info: Dict) -> str:
        # Keep cached scene state byte-for-byte intact for cloth objects. Editing
        # the saved scene JSON can desynchronize cloth particle buffers during
        # OmniGibson load; task-specific removals and pose tweaks are applied
        # after the environment is constructed instead.
        if (
            scene_info.get('object_initial_poses')
            or scene_info.get('scene_file_remove_objects')
            or scene_info.get('scene_file_remove_object_categories')
        ):
            print(
                'Using original scene file; task scene removals and object pose '
                'overrides will be applied after environment load'
            )
        return scene_file


ONLINE_BENCHMARKS = {
    'BehaviorTask': OnlineBehaviorBenchmark,
}
