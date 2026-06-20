from abc import ABC, abstractmethod
from typing import Generator

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
    ) -> Generator[torch.Tensor, None, None]:
        raise NotImplementedError
