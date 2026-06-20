from .base import SceneGraphUpdater
from .schema import SceneGraphEdge, SceneGraphNode, SceneGraphSnapshot


def __getattr__(name):
    if name == "OmniGibsonSceneGraphUpdater":
        from .omnigibson_scene_graph import OmniGibsonSceneGraphUpdater

        return OmniGibsonSceneGraphUpdater
    if name == "PerceptionSceneGraphUpdater":
        from .perception_scene_graph import PerceptionSceneGraphUpdater

        return PerceptionSceneGraphUpdater
    if name == "UniGoalMemorySceneGraphUpdater":
        from .unigoal_memory_scene_graph import UniGoalMemorySceneGraphUpdater

        return UniGoalMemorySceneGraphUpdater
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "OmniGibsonSceneGraphUpdater",
    "PerceptionSceneGraphUpdater",
    "SceneGraphEdge",
    "SceneGraphNode",
    "SceneGraphSnapshot",
    "SceneGraphUpdater",
    "UniGoalMemorySceneGraphUpdater",
]
