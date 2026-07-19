from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class SimulationClock(Protocol):
    """Global monotonic simulator-step source."""

    @property
    def step(self) -> int:
        ...


class ManualSimulationClock:
    def __init__(self, start_step: int = 0):
        self._step = self._validate(start_step)

    @staticmethod
    def _validate(value: int) -> int:
        result = int(value)
        if result < 0:
            raise ValueError("simulation step cannot be negative")
        return result

    @property
    def step(self) -> int:
        return self._step

    def advance(self, steps: int = 1) -> int:
        increment = int(steps)
        if increment < 0:
            raise ValueError("simulation clock cannot move backwards")
        self._step += increment
        return self._step

    def set_step(self, step: int) -> int:
        value = self._validate(step)
        if value < self._step:
            raise ValueError("simulation clock cannot move backwards")
        self._step = value
        return self._step


class CallbackSimulationClock:
    """Clock backed by the benchmark's global low-level-step callback."""

    def __init__(self, callback: Callable[[], int]):
        self._callback = callback
        self._last_step = -1

    @property
    def step(self) -> int:
        value = int(self._callback())
        if value < 0:
            raise ValueError("simulation step callback returned a negative value")
        if value < self._last_step:
            raise ValueError("simulation step callback moved backwards")
        self._last_step = value
        return value
