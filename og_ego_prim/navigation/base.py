from abc import ABC, abstractmethod
from typing import Generator, Optional, Tuple

import torch
from omnigibson.objects import StatefulObject


class NavigationBackend(ABC):
    """Interface for object-level navigation used by semantic primitives."""

    def reset(self, env):
        self.env = env

    @abstractmethod
    def navigate_to_object(
        self,
        controller,
        target_obj: StatefulObject,
        target_pose: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        prefer_target_reachable: bool = False,
        preferred_goal_direction: Optional[torch.Tensor] = None,
        minimum_goal_radius_override: Optional[float] = None,
    ) -> Generator[torch.Tensor, None, None]:
        raise NotImplementedError
