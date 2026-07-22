from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Tuple


WIPE_PAYLOAD_ATTR = "_isbench_wipe_contamination_payload"

'''
wipe transfer 可以理解为“擦拭过程中的污染物转移”。
它模拟的不是“用抹布一擦，污染物直接消失”，而是：

目标表面的污染物 -> 转移到清洁工具
清洁工具原有的污染物 -> 重新沾到目标表面


redeposit_systems
清洁工具在擦拭前已经携带的污染物。
本次擦拭时，这些污染物会重新沾到目标表面。

acquired_systems
第 42 行对应的字段。
本次擦拭从目标表面新获得的视觉污染物。

resulting_payload
擦拭完成后，清洁工具携带的全部污染物。
等于原有污染物和新获得污染物的并集。
'''

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
