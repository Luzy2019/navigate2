from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import yaml

from og_ego_prim.utils.serialization import as_versioned_dict, to_builtin


Size = Tuple[int, int]
RUNTIME_CONFIG_SCHEMA_VERSION = "isbench.runtime_config.v1"
RUNTIME_TASK_CONFIG_SCHEMA_VERSION = "isbench.runtime_task_config.v1"


@dataclass(frozen=True)
class RuntimeSafetyCue:
    """Runtime-only hazard/caution context projected from one safety item.

    Evaluator predicates and action decisions are intentionally absent.  The
    risk provider decides how this context affects the candidate action.
    """

    risk_type: str
    safety_tip: str
    action: str
    checkpoint_type: str
    safety_principle: str = ""
    hazard_id: Optional[str] = None
    subtask_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "risk_type": self.risk_type,
            "safety_principle": self.safety_principle,
            "safety_tip": self.safety_tip,
            "action": self.action,
            "type": self.checkpoint_type,
        }
        if self.hazard_id is not None:
            payload["hazard_id"] = self.hazard_id
        if self.subtask_index is not None:
            payload["triggered_during_subtask"] = self.subtask_index
        return payload


# 从任务 JSON 投影出的只读运行时安全上下文，供风险预测器按动作和子任务查询。
@dataclass(frozen=True)
class RuntimeTaskConfig:
    """Read-only runtime safety projection of the task JSON authoring source."""

    task_name: str
    safety_cues: Tuple[RuntimeSafetyCue, ...] = ()
    schema_version: str = RUNTIME_TASK_CONFIG_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = as_versioned_dict(self)
        payload["safety_cues"] = [item.to_dict() for item in self.safety_cues]
        return payload


def _runtime_safety_cues(
    value: Any,
    *,
    subtask_index: Optional[int] = None,
) -> Tuple[RuntimeSafetyCue, ...]:
    values = (value,) if isinstance(value, Mapping) else value
    if not isinstance(values, (list, tuple)):
        return ()

    result = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        has_runtime_context = any(
            item.get(key)
            for key in (
                "risk_type",
                "hazard_type",
                "safety_principle",
                "safety_tip",
                "caution",
                "message",
            )
        )
        if not has_runtime_context:
            continue
        forbidden = {"decision"} & set(item)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(
                f"runtime safety items must not define {names}; "
                "the risk predictor owns action decisions"
            )
        action = str(item.get("action") or "").strip()
        checkpoint_type = str(item.get("type") or "").strip().lower()
        if not action or not checkpoint_type:
            continue
        risk_type = str(
            item.get("risk_type")
            or item.get("hazard_type")
            or "Safety Hazard"
        ).strip()
        safety_principle = str(item.get("safety_principle") or "").strip()
        safety_tip = str(
            item.get("safety_tip")
            or item.get("caution")
            or item.get("message")
            or ""
        ).strip()
        hazard_id = item.get("hazard_id")
        result.append(
            RuntimeSafetyCue(
                risk_type=risk_type,
                safety_principle=safety_principle,
                safety_tip=safety_tip,
                action=action,
                checkpoint_type=checkpoint_type,
                hazard_id=(
                    None if hazard_id is None else str(hazard_id).strip() or None
                ),
                subtask_index=subtask_index,
            )
        )
    return tuple(result)


def build_runtime_task_config(config: Mapping[str, Any]) -> RuntimeTaskConfig:
    """Project only action-scoped runtime safety context from a task JSON."""

    task_info = config.get("task_info")
    task_info = dict(task_info) if isinstance(task_info, Mapping) else {}
    task_name = str(task_info.get("task_name") or config.get("task_name") or "")
    if not task_name:
        raise ValueError("task definition is missing task_info.task_name")

    goal_conditions = config.get("evaluation_goal_conditions")
    goal_conditions = (
        dict(goal_conditions) if isinstance(goal_conditions, Mapping) else {}
    )
    safety_cues = list(
        _runtime_safety_cues(
            goal_conditions.get("process_safety_goal_condition")
        )
    )
    for ordinal, subtask in enumerate(config.get("subtasks") or (), start=1):
        if not isinstance(subtask, Mapping):
            continue
        subtask_index = int(subtask.get("subtask_index", ordinal))
        safety_cues.extend(
            _runtime_safety_cues(
                subtask.get("G_safe"),
                subtask_index=subtask_index,
            )
        )

    source_version = config.get("_version", config.get("schema_version"))
    extensions = (
        {"source_version": str(source_version)}
        if source_version is not None
        else {}
    )
    return RuntimeTaskConfig(
        task_name=task_name,
        safety_cues=tuple(safety_cues),
        extensions=extensions,
    )


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _to_string_tuple(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    return tuple(
        text
        for item in values
        if (text := str(item).strip().lower())
    )


def parse_size(value: Any, *, allow_none: bool = False) -> Optional[Size]:
    if value is None:
        if allow_none:
            return None
        raise ValueError("size cannot be null")
    if isinstance(value, (tuple, list)) and len(value) == 2:
        width, height = int(value[0]), int(value[1])
    else:
        text = str(value).strip().lower()
        if allow_none and text in {"raw", "none", "0", "null"}:
            return None
        if "x" not in text:
            raise ValueError(f"expected WIDTHxHEIGHT, got {value!r}")
        width_text, height_text = text.split("x", 1)
        width, height = int(width_text), int(height_text)
    if width <= 0 or height <= 0:
        raise ValueError("size dimensions must be positive")
    return width, height


def size_to_text(value: Optional[Size]) -> Optional[str]:
    if value is None:
        return None
    return f"{int(value[0])}x{int(value[1])}"


def _section(mapping: Mapping[str, Any], name: str) -> Dict[str, Any]:
    value = mapping.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"config section {name!r} must be a mapping")
    return dict(value)


def _pop(mapping: Mapping[str, Any], key: str, default: Any) -> Any:
    value = mapping.get(key, default)
    return default if value is None else value


def _option_key(name: str) -> str:
    key = str(name).strip().lower()
    if key.startswith("isbench_"):
        key = key[len("isbench_") :]
    return key


# 控制实验进程的基础运行方式，例如无界面模式、输出根目录和机器人显示。
@dataclass
class RuntimeSectionConfig:
    headless: bool = True
    num_gpus: int = 1
    output_root: str = "outputs"
    timestamp_output: bool = True
    show_robot: bool = False

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "RuntimeSectionConfig":
        return cls(
            headless=_to_bool(_pop(mapping, "headless", cls.headless)),
            num_gpus=int(_pop(mapping, "num_gpus", cls.num_gpus)),
            output_root=str(_pop(mapping, "output_root", cls.output_root)),
            timestamp_output=_to_bool(_pop(mapping, "timestamp_output", cls.timestamp_output)),
            show_robot=_to_bool(_pop(mapping, "show_robot", cls.show_robot)),
        )


# 配置感知场景图的后端、更新频率、相机参数及后端专属选项。
@dataclass
class SceneGraphConfig:
    backend: str = "samjam_unigoal"
    step_interval: int = 30
    update_every: int = 1
    history_interval: int = 30
    image_size: Size = (512, 512)
    sensor_name: Optional[str] = None
    hfov: float = 90.0
    output_debug_matching: bool = True
    output_dir: Optional[str] = None
    debug_log_path: Optional[str] = None
    model_dir: Optional[str] = None
    device: Optional[str] = None
    suppress_vendor_output: bool = True
    backend_options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "SceneGraphConfig":
        image_size = parse_size(_pop(mapping, "image_size", "512x512"))
        backend_options = dict(mapping.get("backend_options") or {})
        return cls(
            backend=str(_pop(mapping, "backend", cls.backend)),
            step_interval=int(_pop(mapping, "step_interval", cls.step_interval)),
            update_every=int(_pop(mapping, "update_every", cls.update_every)),
            history_interval=int(_pop(mapping, "history_interval", cls.history_interval)),
            image_size=image_size,  # type: ignore[arg-type]
            sensor_name=mapping.get("sensor_name"),
            hfov=float(_pop(mapping, "hfov", cls.hfov)),
            output_debug_matching=_to_bool(
                _pop(mapping, "output_debug_matching", cls.output_debug_matching)
            ),
            output_dir=mapping.get("output_dir"),
            debug_log_path=mapping.get("debug_log_path"),
            model_dir=mapping.get("model_dir"),
            device=mapping.get("device"),
            suppress_vendor_output=_to_bool(
                _pop(mapping, "suppress_vendor_output", cls.suppress_vendor_output)
            ),
            backend_options=backend_options,
        )

    def option(self, name: str, default: Any = None) -> Any:
        candidates = [name, _option_key(name)]
        if str(name).startswith("ISBENCH_"):
            candidates.append(str(name).lower())
        for key in candidates:
            if key in self.backend_options:
                value = self.backend_options[key]
                return default if value is None else value
        return default

    def option_bool(self, name: str, default: bool = False) -> bool:
        return _to_bool(self.option(name, default))

    def option_int(self, name: str, default: int) -> int:
        return int(self.option(name, default))

    def option_float(self, name: str, default: float) -> float:
        return float(self.option(name, default))


# 配置机器人导航的速度、可达目标筛选、避障净空和卡死判定参数。
@dataclass
class NavigationConfig:
    linear_command: float = 0.5
    angular_command: float = 0.5
    stuck_window: int = 60
    stuck_angle_tolerance: float = 0.25
    stuck_waypoint_tolerance: float = 0.25
    stuck_final_waypoint_tolerance: float = 0.30
    stuck_angle_progress_threshold: float = 0.005
    final_approach_distance: float = 0.35
    container_min_goal_radius: float = 0.80
    cabinet_min_goal_radius: float = 0.70
    tactqn_min_goal_radius: float = 0.45
    goal_clearance_radius: float = 0.25
    trav_map_robot_radius_scale: float = 1.0
    trav_map_extra_erosion_margin: float = 0.0
    doorway_clearance_radius_scale: float = 1.0
    clearance_aware_path: bool = True
    clearance_aware_desired_clearance: float = 0.30
    clearance_aware_weight: float = 1.25
    clearance_aware_simplify: bool = True
    rotate_when_already_in_navigation_region: bool = True
    already_region_yaw_tolerance: float = 0.85
    already_reachable_max_goal_radius: float = 1.30
    max_floor_height_delta: float = 0.35
    max_ik_goal_checks: int = 8
    verbose: bool = False

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "NavigationConfig":
        return cls(
            linear_command=float(_pop(mapping, "linear_command", cls.linear_command)),
            angular_command=float(_pop(mapping, "angular_command", cls.angular_command)),
            stuck_window=int(_pop(mapping, "stuck_window", cls.stuck_window)),
            stuck_angle_tolerance=float(
                _pop(mapping, "stuck_angle_tolerance", cls.stuck_angle_tolerance)
            ),
            stuck_waypoint_tolerance=float(
                _pop(mapping, "stuck_waypoint_tolerance", cls.stuck_waypoint_tolerance)
            ),
            stuck_final_waypoint_tolerance=float(
                _pop(mapping, "stuck_final_waypoint_tolerance", cls.stuck_final_waypoint_tolerance)
            ),
            stuck_angle_progress_threshold=float(
                _pop(
                    mapping,
                    "stuck_angle_progress_threshold",
                    cls.stuck_angle_progress_threshold,
                )
            ),
            final_approach_distance=float(
                _pop(mapping, "final_approach_distance", cls.final_approach_distance)
            ),
            container_min_goal_radius=float(
                _pop(mapping, "container_min_goal_radius", cls.container_min_goal_radius)
            ),
            cabinet_min_goal_radius=float(
                _pop(mapping, "cabinet_min_goal_radius", cls.cabinet_min_goal_radius)
            ),
            tactqn_min_goal_radius=float(
                _pop(mapping, "tactqn_min_goal_radius", cls.tactqn_min_goal_radius)
            ),
            goal_clearance_radius=float(
                _pop(mapping, "goal_clearance_radius", cls.goal_clearance_radius)
            ),
            trav_map_robot_radius_scale=float(
                _pop(
                    mapping,
                    "trav_map_robot_radius_scale",
                    cls.trav_map_robot_radius_scale,
                )
            ),
            trav_map_extra_erosion_margin=float(
                _pop(
                    mapping,
                    "trav_map_extra_erosion_margin",
                    cls.trav_map_extra_erosion_margin,
                )
            ),
            doorway_clearance_radius_scale=float(
                _pop(
                    mapping,
                    "doorway_clearance_radius_scale",
                    cls.doorway_clearance_radius_scale,
                )
            ),
            clearance_aware_path=_to_bool(
                _pop(mapping, "clearance_aware_path", cls.clearance_aware_path)
            ),
            clearance_aware_desired_clearance=float(
                _pop(
                    mapping,
                    "clearance_aware_desired_clearance",
                    cls.clearance_aware_desired_clearance,
                )
            ),
            clearance_aware_weight=float(
                _pop(mapping, "clearance_aware_weight", cls.clearance_aware_weight)
            ),
            clearance_aware_simplify=_to_bool(
                _pop(
                    mapping,
                    "clearance_aware_simplify",
                    cls.clearance_aware_simplify,
                )
            ),
            rotate_when_already_in_navigation_region=_to_bool(
                _pop(
                    mapping,
                    "rotate_when_already_in_navigation_region",
                    cls.rotate_when_already_in_navigation_region,
                )
            ),
            already_region_yaw_tolerance=float(
                _pop(
                    mapping,
                    "already_region_yaw_tolerance",
                    cls.already_region_yaw_tolerance,
                )
            ),
            already_reachable_max_goal_radius=float(
                _pop(
                    mapping,
                    "already_reachable_max_goal_radius",
                    cls.already_reachable_max_goal_radius,
                )
            ),
            max_floor_height_delta=float(
                _pop(mapping, "max_floor_height_delta", cls.max_floor_height_delta)
            ),
            max_ik_goal_checks=int(_pop(mapping, "max_ik_goal_checks", cls.max_ik_goal_checks)),
            verbose=_to_bool(_pop(mapping, "verbose", cls.verbose)),
        )


# 配置 starter primitives 的符号化抓取、放置、开关和布料搬运执行策略。
@dataclass
class StarterPrimitivesConfig:
    verbose: bool = False
    symbolic_manipulation: bool = True
    symbolic_grasp: bool = True
    symbolic_place: bool = True
    symbolic_cloth_inside_drop: bool = False
    symbolic_cloth_inside_drop_container_categories: Tuple[str, ...] = ()
    symbolic_cloth_inside_drop_height: float = 0.05
    symbolic_cloth_inside_pre_settle_steps: int = 0
    symbolic_cloth_inside_settle_steps: int = 90
    symbolic_cloth_inside_fit_shape: bool = False
    symbolic_cloth_inside_fit_container_scale: float = 0.82
    symbolic_cloth_carry_preserve_shape: bool = False
    symbolic_cloth_carry_preserve_shape_categories: Tuple[str, ...] = ()
    symbolic_loaded_cloth_carry_preserve_shape: bool = False
    symbolic_loaded_cloth_carry_preserve_shape_categories: Tuple[str, ...] = ()
    # Keep released cloth descendants in their captured container-relative pose
    # for a bounded task-local window.  The default is off so ordinary starter
    # tasks retain the native release dynamics.
    symbolic_loaded_cloth_release_stabilization_steps: int = 0
    symbolic_open_close: bool = True
    symbolic_release: bool = True
    tactqn_open_goal_radius: float = 0.45
    symbolic_grasp_max_goal_radius: float = 0.85
    symbolic_grasp_max_yaw_error: float = 0.85
    symbolic_carry_radial_clearance: float = 0.08
    symbolic_carry_vertical_clearance: float = 0.04
    deferred_coverage_max_samples: int = 200
    symbolic_carry_robot_collision_filter_scope: str = "release"
    symbolic_carry_robot_collision_filter_objects: Tuple[str, ...] = ()
    tactqn_symbolic_open_close_fallback: bool = True
    fixed_navigation_arm_pose: bool = True
    fixed_navigation_arm_pose_name: str = "vertical"
    native_stance_sample_attempts: int = 1
    native_waypoint_max_linear_step: float = 0.18
    explicit_navigation_max_goal_radius: Optional[float] = None
    explicit_grasp_use_object_navigation: bool = False
    explicit_grasp_navigation_max_goal_radius: Optional[float] = None
    first_view_targeting: bool = True
    first_view_targeting_max_steps: int = 120
    first_view_targeting_angular_tolerance: float = 0.045
    first_view_targeting_max_joint_step: float = 0.16
    first_view_targeting_settle_steps: int = 6
    first_view_targeting_align_base: bool = True
    first_view_targeting_roll_tolerance: float = 0.045
    first_view_targeting_max_base_yaw_change: float = 0.35

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "StarterPrimitivesConfig":
        filter_objects = _to_string_tuple(
            _pop(
                mapping,
                "symbolic_carry_robot_collision_filter_objects",
                cls.symbolic_carry_robot_collision_filter_objects,
            )
        )
        filter_scope = str(
            _pop(
                mapping,
                "symbolic_carry_robot_collision_filter_scope",
                cls.symbolic_carry_robot_collision_filter_scope,
            )
        ).strip().lower()
        if filter_scope not in {"release", "episode"}:
            raise ValueError(
                "starter_primitives.symbolic_carry_robot_collision_filter_scope "
                "must be 'release' or 'episode'"
            )
        return cls(
            verbose=_to_bool(_pop(mapping, "verbose", cls.verbose)),
            symbolic_manipulation=_to_bool(
                _pop(mapping, "symbolic_manipulation", cls.symbolic_manipulation)
            ),
            symbolic_grasp=_to_bool(_pop(mapping, "symbolic_grasp", cls.symbolic_grasp)),
            symbolic_place=_to_bool(_pop(mapping, "symbolic_place", cls.symbolic_place)),
            symbolic_cloth_inside_drop=_to_bool(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_drop",
                    cls.symbolic_cloth_inside_drop,
                )
            ),
            symbolic_cloth_inside_drop_container_categories=_to_string_tuple(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_drop_container_categories",
                    cls.symbolic_cloth_inside_drop_container_categories,
                )
            ),
            symbolic_cloth_inside_drop_height=float(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_drop_height",
                    cls.symbolic_cloth_inside_drop_height,
                )
            ),
            symbolic_cloth_inside_pre_settle_steps=int(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_pre_settle_steps",
                    cls.symbolic_cloth_inside_pre_settle_steps,
                )
            ),
            symbolic_cloth_inside_settle_steps=int(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_settle_steps",
                    cls.symbolic_cloth_inside_settle_steps,
                )
            ),
            symbolic_cloth_inside_fit_shape=_to_bool(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_fit_shape",
                    cls.symbolic_cloth_inside_fit_shape,
                )
            ),
            symbolic_cloth_inside_fit_container_scale=float(
                _pop(
                    mapping,
                    "symbolic_cloth_inside_fit_container_scale",
                    cls.symbolic_cloth_inside_fit_container_scale,
                )
            ),
            symbolic_cloth_carry_preserve_shape=_to_bool(
                _pop(
                    mapping,
                    "symbolic_cloth_carry_preserve_shape",
                    cls.symbolic_cloth_carry_preserve_shape,
                )
            ),
            symbolic_cloth_carry_preserve_shape_categories=_to_string_tuple(
                _pop(
                    mapping,
                    "symbolic_cloth_carry_preserve_shape_categories",
                    cls.symbolic_cloth_carry_preserve_shape_categories,
                )
            ),
            symbolic_loaded_cloth_carry_preserve_shape=_to_bool(
                _pop(
                    mapping,
                    "symbolic_loaded_cloth_carry_preserve_shape",
                    cls.symbolic_loaded_cloth_carry_preserve_shape,
                )
            ),
            symbolic_loaded_cloth_carry_preserve_shape_categories=_to_string_tuple(
                _pop(
                    mapping,
                    "symbolic_loaded_cloth_carry_preserve_shape_categories",
                    cls.symbolic_loaded_cloth_carry_preserve_shape_categories,
                )
            ),
            symbolic_loaded_cloth_release_stabilization_steps=int(
                _pop(
                    mapping,
                    "symbolic_loaded_cloth_release_stabilization_steps",
                    cls.symbolic_loaded_cloth_release_stabilization_steps,
                )
            ),
            symbolic_open_close=_to_bool(
                _pop(mapping, "symbolic_open_close", cls.symbolic_open_close)
            ),
            symbolic_release=_to_bool(
                _pop(mapping, "symbolic_release", cls.symbolic_release)
            ),
            tactqn_open_goal_radius=float(
                _pop(mapping, "tactqn_open_goal_radius", cls.tactqn_open_goal_radius)
            ),
            symbolic_grasp_max_goal_radius=float(
                _pop(
                    mapping,
                    "symbolic_grasp_max_goal_radius",
                    cls.symbolic_grasp_max_goal_radius,
                )
            ),
            symbolic_grasp_max_yaw_error=float(
                _pop(
                    mapping,
                    "symbolic_grasp_max_yaw_error",
                    cls.symbolic_grasp_max_yaw_error,
                )
            ),
            symbolic_carry_radial_clearance=float(
                _pop(
                    mapping,
                    "symbolic_carry_radial_clearance",
                    cls.symbolic_carry_radial_clearance,
                )
            ),
            symbolic_carry_vertical_clearance=float(
                _pop(
                    mapping,
                    "symbolic_carry_vertical_clearance",
                    cls.symbolic_carry_vertical_clearance,
                )
            ),
            deferred_coverage_max_samples=int(
                _pop(
                    mapping,
                    "deferred_coverage_max_samples",
                    cls.deferred_coverage_max_samples,
                )
            ),
            symbolic_carry_robot_collision_filter_scope=filter_scope,
            symbolic_carry_robot_collision_filter_objects=filter_objects,
            tactqn_symbolic_open_close_fallback=_to_bool(
                _pop(
                    mapping,
                    "tactqn_symbolic_open_close_fallback",
                    cls.tactqn_symbolic_open_close_fallback,
                )
            ),
            fixed_navigation_arm_pose=_to_bool(
                _pop(mapping, "fixed_navigation_arm_pose", cls.fixed_navigation_arm_pose)
            ),
            fixed_navigation_arm_pose_name=str(
                _pop(mapping, "fixed_navigation_arm_pose_name", cls.fixed_navigation_arm_pose_name)
            ),
            native_stance_sample_attempts=int(
                _pop(
                    mapping,
                    "native_stance_sample_attempts",
                    cls.native_stance_sample_attempts,
                )
            ),
            native_waypoint_max_linear_step=float(
                _pop(
                    mapping,
                    "native_waypoint_max_linear_step",
                    cls.native_waypoint_max_linear_step,
                )
            ),
            explicit_navigation_max_goal_radius=(
                None
                if mapping.get("explicit_navigation_max_goal_radius") is None
                else float(mapping["explicit_navigation_max_goal_radius"])
            ),
            explicit_grasp_use_object_navigation=_to_bool(
                _pop(
                    mapping,
                    "explicit_grasp_use_object_navigation",
                    cls.explicit_grasp_use_object_navigation,
                )
            ),
            explicit_grasp_navigation_max_goal_radius=(
                None
                if mapping.get("explicit_grasp_navigation_max_goal_radius") is None
                else float(mapping["explicit_grasp_navigation_max_goal_radius"])
            ),
            first_view_targeting=_to_bool(
                _pop(mapping, "first_view_targeting", cls.first_view_targeting)
            ),
            first_view_targeting_max_steps=int(
                _pop(
                    mapping,
                    "first_view_targeting_max_steps",
                    cls.first_view_targeting_max_steps,
                )
            ),
            first_view_targeting_angular_tolerance=float(
                _pop(
                    mapping,
                    "first_view_targeting_angular_tolerance",
                    cls.first_view_targeting_angular_tolerance,
                )
            ),
            first_view_targeting_max_joint_step=float(
                _pop(
                    mapping,
                    "first_view_targeting_max_joint_step",
                    cls.first_view_targeting_max_joint_step,
                )
            ),
            first_view_targeting_settle_steps=int(
                _pop(
                    mapping,
                    "first_view_targeting_settle_steps",
                    cls.first_view_targeting_settle_steps,
                )
            ),
            first_view_targeting_align_base=bool(
                _pop(
                    mapping,
                    "first_view_targeting_align_base",
                    cls.first_view_targeting_align_base,
                )
            ),
            first_view_targeting_roll_tolerance=float(
                _pop(
                    mapping,
                    "first_view_targeting_roll_tolerance",
                    cls.first_view_targeting_roll_tolerance,
                )
            ),
            first_view_targeting_max_base_yaw_change=float(
                _pop(
                    mapping,
                    "first_view_targeting_max_base_yaw_change",
                    cls.first_view_targeting_max_base_yaw_change,
                )
            ),
        )


# 配置评测产物的保存位置、视频采样、图像尺寸和调试证据输出。
@dataclass
class ArtifactConfig:
    output_dir: Optional[str] = None
    save_video: bool = True
    video_fps: float = 30.0
    video_capture_interval: int = 30
    output_size: Optional[Size] = (512, 512)
    sensor_image_size: Optional[Size] = None
    save_step_images: bool = True
    save_surrounding_observations: bool = False
    save_topdown_scene: bool = False
    topdown_world_bounds: Optional[Any] = None
    topdown_output_size: Size = (1920, 1080)
    save_sampled_scene: bool = False

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "ArtifactConfig":
        return cls(
            output_dir=mapping.get("output_dir"),
            save_video=_to_bool(_pop(mapping, "save_video", cls.save_video)),
            video_fps=float(_pop(mapping, "video_fps", cls.video_fps)),
            video_capture_interval=int(
                _pop(mapping, "video_capture_interval", cls.video_capture_interval)
            ),
            output_size=parse_size(
                _pop(mapping, "output_size", size_to_text(cls.output_size)),
                allow_none=True,
            ),
            sensor_image_size=parse_size(
                _pop(mapping, "sensor_image_size", size_to_text(cls.sensor_image_size)),
                allow_none=True,
            ),
            save_step_images=_to_bool(_pop(mapping, "save_step_images", cls.save_step_images)),
            save_surrounding_observations=_to_bool(
                _pop(
                    mapping,
                    "save_surrounding_observations",
                    cls.save_surrounding_observations,
                )
            ),
            save_topdown_scene=_to_bool(
                _pop(mapping, "save_topdown_scene", cls.save_topdown_scene)
            ),
            topdown_world_bounds=mapping.get("topdown_world_bounds"),
            topdown_output_size=parse_size(
                _pop(mapping, "topdown_output_size", size_to_text(cls.topdown_output_size))
            ),  # type: ignore[arg-type]
            save_sampled_scene=_to_bool(
                _pop(mapping, "save_sampled_scene", cls.save_sampled_scene)
            ),
        )


# 配置本次任务运行的任务名、场景、模型、规划器和执行行为默认值。
@dataclass
class TaskConfig:
    name: Optional[str] = None
    scene: Optional[str] = None
    model: Optional[str] = None
    primitive_type: str = "auto"
    prompt_setting: str = "v1"
    plan_max_steps: Optional[int] = None
    stop_on_error: bool = True
    use_initial_setup: bool = False
    use_self_caption: bool = False
    planner_use_obs: bool = True
    online_object_sampling: Optional[bool] = None

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "TaskConfig":
        plan_max_steps = mapping.get("plan_max_steps")
        return cls(
            name=mapping.get("name"),
            scene=mapping.get("scene"),
            model=mapping.get("model"),
            primitive_type=str(_pop(mapping, "primitive_type", cls.primitive_type)),
            prompt_setting=str(_pop(mapping, "prompt_setting", cls.prompt_setting)),
            plan_max_steps=None if plan_max_steps is None else int(plan_max_steps),
            stop_on_error=_to_bool(_pop(mapping, "stop_on_error", cls.stop_on_error)),
            use_initial_setup=_to_bool(
                _pop(mapping, "use_initial_setup", cls.use_initial_setup)
            ),
            use_self_caption=_to_bool(
                _pop(mapping, "use_self_caption", cls.use_self_caption)
            ),
            planner_use_obs=_to_bool(_pop(mapping, "planner_use_obs", cls.planner_use_obs)),
            online_object_sampling=mapping.get("online_object_sampling"),
        )


# 保留旧 TaskMemory 的配置契约；当前 runtime 不构造该模块。
@dataclass
class MemoryConfig:
    enabled: bool = False
    max_actions: int = 50
    max_states_per_object: int = 20
    max_object_manipulations: int = 20
    retriever: str = "exact"
    consolidation: str = "deduplicate"
    options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "MemoryConfig":
        return cls(
            enabled=_to_bool(_pop(mapping, "enabled", cls.enabled)),
            max_actions=int(_pop(mapping, "max_actions", cls.max_actions)),
            max_states_per_object=int(
                _pop(mapping, "max_states_per_object", cls.max_states_per_object)
            ),
            max_object_manipulations=int(
                _pop(mapping, "max_object_manipulations", cls.max_object_manipulations)
            ),
            retriever=str(_pop(mapping, "retriever", cls.retriever)),
            consolidation=str(_pop(mapping, "consolidation", cls.consolidation)),
            options=dict(mapping.get("options") or {}),
        )


# 配置对象状态模型的操作次数上限、生命周期策略和生命周期规则。
@dataclass
class ObjectModelConfig:
    max_manipulations: int = 20
    lifecycle_policy: str = "rules"
    lifecycle_rules: Tuple[Dict[str, Any], ...] = ()
    options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "ObjectModelConfig":
        rules = mapping.get("lifecycle_rules") or ()
        if isinstance(rules, Mapping):
            normalized_rules = []
            for rule_id, value in rules.items():
                if value is not None and not isinstance(value, Mapping):
                    raise TypeError(
                        f"object lifecycle rule {rule_id!r} must contain a mapping"
                    )
                normalized_rules.append(
                    {"rule_id": str(rule_id), **dict(value or {})}
                )
            rules = tuple(normalized_rules)
        elif isinstance(rules, (str, bytes)):
            raise TypeError("object_model.lifecycle_rules must be a sequence of mappings")
        else:
            rules = tuple(rules)
        if any(not isinstance(rule, Mapping) for rule in rules):
            raise TypeError("object_model.lifecycle_rules entries must be mappings")
        max_manipulations = int(
            _pop(mapping, "max_manipulations", cls.max_manipulations)
        )
        if max_manipulations <= 0:
            raise ValueError("object_model.max_manipulations must be greater than zero")
        lifecycle_policy = str(
            _pop(mapping, "lifecycle_policy", cls.lifecycle_policy)
        ).strip().lower()
        if not lifecycle_policy:
            raise ValueError("object_model.lifecycle_policy must not be empty")
        return cls(
            max_manipulations=max_manipulations,
            lifecycle_policy=lifecycle_policy,
            lifecycle_rules=tuple(dict(rule) for rule in rules),
            options=dict(mapping.get("options") or {}),
        )


# 配置模拟时钟驱动的过程调度器，以及跨子任务计时器的暴露方式。
@dataclass
class SchedulerConfig:
    enabled: bool = True
    clock: str = "simulator_step"
    expose_cross_subtask_timers: bool = True
    process_definitions: Tuple[Dict[str, Any], ...] = ()
    handler_options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "SchedulerConfig":
        definitions = mapping.get("process_definitions") or mapping.get("processes") or ()
        if isinstance(definitions, Mapping):
            normalized_definitions = []
            for name, value in definitions.items():
                if value is not None and not isinstance(value, Mapping):
                    raise TypeError(
                        f"scheduler process {name!r} must contain a mapping"
                    )
                normalized_definitions.append(
                    {"process_type": str(name), **dict(value or {})}
                )
            definitions = tuple(normalized_definitions)
        elif isinstance(definitions, (str, bytes)):
            raise TypeError("scheduler process_definitions must be a sequence of mappings")
        else:
            normalized_definitions = []
            for item in definitions:
                if not isinstance(item, Mapping):
                    raise TypeError(
                        "scheduler process_definitions entries must be mappings"
                    )
                normalized_definitions.append(dict(item))
            definitions = tuple(normalized_definitions)
        return cls(
            enabled=_to_bool(_pop(mapping, "enabled", cls.enabled)),
            clock=str(_pop(mapping, "clock", cls.clock)),
            expose_cross_subtask_timers=_to_bool(
                _pop(
                    mapping,
                    "expose_cross_subtask_timers",
                    cls.expose_cross_subtask_timers,
                )
            ),
            process_definitions=tuple(dict(item) for item in definitions),
            handler_options=dict(mapping.get("handler_options") or {}),
        )


# 配置运行时风险预测器是否启用及其风险上下文提供者。
@dataclass
class RiskPredictorConfig:
    enabled: bool = True
    provider: str = "task_json"
    provider_options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "RiskPredictorConfig":
        options = dict(mapping)
        if "rules" in options:
            raise ValueError(
                "risk.rules is not supported; author runtime safety context "
                "in the task JSON"
            )
        enabled = _to_bool(options.pop("enabled", cls.enabled))
        provider = str(
            options.pop("provider", options.pop("type", cls.provider))
        ).strip().lower()
        if not provider:
            raise ValueError("risk.provider must not be empty")
        return cls(
            enabled=enabled,
            provider=provider,
            provider_options=options,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            **copy.deepcopy(self.provider_options),
        }


# 配置供规划模型使用的提示词构建器和提示词组成部分。
@dataclass
class PromptingConfig:
    builder: str = "semantic"
    sections: Tuple[str, ...] = (
        "task",
        "scene",
        "objects",
        "timers",
        "action",
    )
    options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "PromptingConfig":
        sections = mapping.get("sections", cls.sections)
        if sections is None:
            sections = cls.sections
        if isinstance(sections, str):
            sections = tuple(item.strip() for item in sections.split(",") if item.strip())
        return cls(
            builder=str(_pop(mapping, "builder", cls.builder)),
            sections=tuple(str(item).strip() for item in sections if str(item).strip()),
            options=dict(mapping.get("options") or {}),
        )


# 聚合整份运行 YAML 的所有配置段，并保留模式版本、扩展字段和原始输入。
@dataclass
class RuntimeConfig:
    runtime: RuntimeSectionConfig = field(default_factory=RuntimeSectionConfig)
    scene_graph: SceneGraphConfig = field(default_factory=SceneGraphConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    starter_primitives: StarterPrimitivesConfig = field(default_factory=StarterPrimitivesConfig)
    artifacts: ArtifactConfig = field(default_factory=ArtifactConfig)
    task: TaskConfig = field(default_factory=TaskConfig)
    object_model: ObjectModelConfig = field(default_factory=ObjectModelConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    risk: RiskPredictorConfig = field(default_factory=RiskPredictorConfig)
    prompting: PromptingConfig = field(default_factory=PromptingConfig)
    schema_version: str = RUNTIME_CONFIG_SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "RuntimeConfig":
        return cls()

    @classmethod
    def from_mapping(cls, mapping: Optional[Mapping[str, Any]]) -> "RuntimeConfig":
        raw_mapping = dict(mapping or {})
        mapping = dict(raw_mapping)
        memory_mapping = _section(mapping, "memory")
        object_model_mapping = _section(mapping, "object_model")
        return cls(
            runtime=RuntimeSectionConfig.from_mapping(_section(mapping, "runtime")),
            scene_graph=SceneGraphConfig.from_mapping(_section(mapping, "scene_graph")),
            navigation=NavigationConfig.from_mapping(_section(mapping, "navigation")),
            starter_primitives=StarterPrimitivesConfig.from_mapping(
                _section(mapping, "starter_primitives")
            ),
            artifacts=ArtifactConfig.from_mapping(_section(mapping, "artifacts")),
            task=TaskConfig.from_mapping(_section(mapping, "task")),
            object_model=ObjectModelConfig.from_mapping(object_model_mapping),
            memory=MemoryConfig.from_mapping(memory_mapping),
            scheduler=SchedulerConfig.from_mapping(_section(mapping, "scheduler")),
            risk=RiskPredictorConfig.from_mapping(_section(mapping, "risk")),
            prompting=PromptingConfig.from_mapping(_section(mapping, "prompting")),
            schema_version=str(
                _pop(
                    mapping,
                    "schema_version",
                    RUNTIME_CONFIG_SCHEMA_VERSION,
                )
            ),
            extensions=dict(mapping.get("extensions") or {}),
            raw=raw_mapping,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        # Parse the old Memory-owned name, but emit only the canonical Object
        # Module field so fresh configs do not perpetuate the old ownership.
        data["memory"].pop("max_object_manipulations", None)
        data["scene_graph"]["image_size"] = size_to_text(self.scene_graph.image_size)
        data["artifacts"]["output_size"] = size_to_text(self.artifacts.output_size)
        data["artifacts"]["sensor_image_size"] = size_to_text(self.artifacts.sensor_image_size)
        data["artifacts"]["topdown_output_size"] = size_to_text(
            self.artifacts.topdown_output_size
        )
        data["risk"] = self.risk.to_dict()
        return to_builtin(data)


def _deep_merge_runtime_config(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> Dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in dict(override).items():
        if key == "includes":
            continue
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge_runtime_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_runtime_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"config file must contain a mapping: {path}")
    return dict(payload)


def _load_runtime_yaml_with_includes(
    path: Path,
    seen: Optional[set[Path]] = None,
) -> Dict[str, Any]:
    path = path.resolve()
    seen = set() if seen is None else seen
    if path in seen:
        raise ValueError(f"recursive config include detected: {path}")
    seen.add(path)
    payload = _read_runtime_yaml(path)
    merged: Dict[str, Any] = {}
    for include in payload.get("includes", []) or []:
        include_path = Path(include)
        if not include_path.is_absolute():
            include_path = path.parent / include_path
        merged = _deep_merge_runtime_config(
            merged,
            _load_runtime_yaml_with_includes(include_path, seen),
        )
    return _deep_merge_runtime_config(merged, payload)


def load_runtime_config_dict(
    config_path: Optional[str | Path],
) -> Dict[str, Any]:
    """Load runtime/experiment YAML without importing evaluator contracts."""

    merged = RuntimeConfig.defaults().to_dict()
    if config_path:
        overrides = _load_runtime_yaml_with_includes(Path(config_path))
        merged = _deep_merge_runtime_config(merged, overrides)
    return merged
