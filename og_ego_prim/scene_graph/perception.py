from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import numpy as np


@dataclass
class FrameObservation:
    frame_index: int
    rgb: np.ndarray
    depth: Optional[np.ndarray]
    intrinsics: Optional[np.ndarray]
    camera_pose: Optional[np.ndarray]
    robot_position: Optional[List[float]]
    sensor_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerceivedObject:
    object_id: str
    name: str
    category: str
    bbox: Optional[List[float]] = None
    mask: Optional[np.ndarray] = None
    position: Optional[List[float]] = None
    room_id: Optional[str] = None
    confidence: float = 1.0
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerceivedRelation:
    source_id: str
    target_id: str
    relation: str
    confidence: float = 1.0
    source: str = "perception"


@dataclass
class PerceptionResult:
    backend: str
    frame_index: int
    objects: List[PerceivedObject] = field(default_factory=list)
    relations: List[PerceivedRelation] = field(default_factory=list)
    scene_graph: Dict[str, Any] = field(default_factory=dict)
    room_graph: Dict[str, Any] = field(default_factory=dict)
    group_graph: Dict[str, Any] = field(default_factory=dict)
    goal_graph: Dict[str, Any] = field(default_factory=dict)
    scene_goal_matches: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


class PerceptionBackend(Protocol):
    name: str

    def reset(self, env: Any) -> None:
        ...

    def observe(self, env: Any) -> FrameObservation:
        ...

    def detect(self, frame: FrameObservation) -> PerceptionResult:
        ...

    def update_memory(self, result: PerceptionResult) -> PerceptionResult:
        ...
