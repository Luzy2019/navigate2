from typing import Optional

from og_ego_prim.config.runtime_config import SceneGraphConfig
from og_ego_prim.scene_graph.perception import PerceptionBackend


def build_perception_backend(
    backend_name: str,
    sensor_name: Optional[str] = None,
    scene_graph_config: Optional[SceneGraphConfig] = None,
) -> PerceptionBackend:
    normalized = backend_name.strip().lower()
    if normalized == "unigoal_grounded_sam":
        from .unigoal_grounded_sam import UniGoalGroundedSAMBackend

        return UniGoalGroundedSAMBackend(sensor_name=sensor_name, scene_graph_config=scene_graph_config)
    if normalized == "samjam_sam2":
        from .samjam_sam2 import SAMJAMSAM2Backend

        return SAMJAMSAM2Backend(sensor_name=sensor_name, scene_graph_config=scene_graph_config)
    if normalized == "samjam_unigoal":
        from .samjam_unigoal import SAMJAMUniGoalBackend

        return SAMJAMUniGoalBackend(sensor_name=sensor_name, scene_graph_config=scene_graph_config)
    if normalized == "manual_corrected":
        from .manual_corrected import ManualCorrectedBackend

        return ManualCorrectedBackend(sensor_name=sensor_name, scene_graph_config=scene_graph_config)
    raise ValueError(
        f"unknown scene graph perception backend {backend_name!r}; "
        "expected manual_corrected, unigoal_grounded_sam, samjam_sam2, or samjam_unigoal"
    )
