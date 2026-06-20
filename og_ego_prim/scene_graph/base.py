from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING

from .schema import SceneGraphSnapshot

if TYPE_CHECKING:
    from og_ego_prim.primitives.executor import LowLevelStepContext


class SceneGraphUpdater(ABC):

    @abstractmethod
    def reset(self, env: Any):
        pass

    @abstractmethod
    def update(
        self,
        context: Optional["LowLevelStepContext"] = None,
    ) -> SceneGraphSnapshot:
        pass

    @abstractmethod
    def get_snapshot(self) -> SceneGraphSnapshot:
        pass

    @abstractmethod
    def to_prompt_context(self) -> str:
        pass
