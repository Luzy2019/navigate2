from typing import Optional

from og_ego_prim.scene_graph.perception import PerceptionBackend

from .samjam_sam2 import SAMJAMSAM2Backend
from .unigoal_grounded_sam import UniGoalGroundedSAMBackend


def build_perception_backend(
    backend_name: str,
    sensor_name: Optional[str] = None,
) -> PerceptionBackend:
    normalized = backend_name.strip().lower()
    if normalized == "unigoal_grounded_sam":
        return UniGoalGroundedSAMBackend(sensor_name=sensor_name)
    if normalized == "samjam_sam2":
        return SAMJAMSAM2Backend(sensor_name=sensor_name)
    raise ValueError(
        f"unknown scene graph perception backend {backend_name!r}; "
        "expected unigoal_grounded_sam or samjam_sam2"
    )
