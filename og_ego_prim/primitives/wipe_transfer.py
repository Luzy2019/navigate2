from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Tuple


WIPE_PAYLOAD_ATTR = "_isbench_wipe_contamination_payload"


def _normalized_names(names: Iterable[str]) -> FrozenSet[str]:
    return frozenset(str(name) for name in names if str(name))


def get_wipe_payload(cleaning_tool) -> FrozenSet[str]:
    """Return the tool's hidden cross-WIPE contamination payload."""

    return _normalized_names(getattr(cleaning_tool, WIPE_PAYLOAD_ATTR, ()))


def set_wipe_payload(cleaning_tool, system_names: Iterable[str]) -> None:
    """Persist contamination on the object across subtasks in the same scene."""

    setattr(cleaning_tool, WIPE_PAYLOAD_ATTR, tuple(sorted(_normalized_names(system_names))))


def clear_wipe_payload(cleaning_tool) -> None:
    set_wipe_payload(cleaning_tool, ())


def visual_particle_system_names(scene: Any, systems: Iterable[Any]) -> Tuple[str, ...]:
    """Return only visual contaminants; fluids stay in Saturated state handling."""

    names = {
        str(system.name)
        for system in systems
        if scene.is_visual_particle_system(system_name=system.name)
    }
    return tuple(sorted(names))


@dataclass(frozen=True)
class WipeTransferPlan:
    redeposit_systems: Tuple[str, ...]
    acquired_systems: Tuple[str, ...]
    resulting_payload: Tuple[str, ...]


def plan_wipe_transfer(
    *,
    carried_before: Iterable[str],
    removed_visual_contaminants: Iterable[str],
) -> WipeTransferPlan:
    """Plan old-payload deposition before adding contamination from this WIPE."""

    carried = _normalized_names(carried_before)
    acquired = _normalized_names(removed_visual_contaminants)
    return WipeTransferPlan(
        redeposit_systems=tuple(sorted(carried)),
        acquired_systems=tuple(sorted(acquired)),
        resulting_payload=tuple(sorted(carried | acquired)),
    )


__all__ = [
    "WIPE_PAYLOAD_ATTR",
    "WipeTransferPlan",
    "clear_wipe_payload",
    "get_wipe_payload",
    "plan_wipe_transfer",
    "set_wipe_payload",
    "visual_particle_system_names",
]
