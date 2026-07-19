from __future__ import annotations

import re
from typing import Any, Dict, List, Literal


PrimitiveType = Literal["ego", "starter", "symbolic"]


EGO_VALID_PRIMITIVES: Dict[str, int] = {
    "NAVIGATE_TO": 1,
    "GRASP": 1,
    "RELEASE": 0,
    "PLACE_ON_TOP": 2,
    "PLACE_INSIDE": 2,
    "PLACE_NEXTTO": 2,
    "OPEN": 1,
    "CLOSE": 1,
    "TOGGLE_ON": 1,
    "TOGGLE_OFF": 1,
    "WIPE": 2,
    "CUT": 2,
    "SOAK_INSIDE": 2,
    "SOAK_UNDER": 2,
    "FILL_WITH": 2,
    "POUR_INTO": 2,
    "SPREAD": 2,
    "WAIT": 1,
    "WAIT_FOR_COOKED": 1,
    "WAIT_FOR_WASHED": 1,
    "WAIT_FOR_FROZEN": 2,
    "MARK_WET_REGION": 1,
}


STARTER_VALID_PRIMITIVES: Dict[str, int] = {
    "GRASP": 1,
    "PLACE_ON_TOP": 1,
    "PLACE_INSIDE": 1,
    "POUR_INTO": 1,
    "DUMP_INTO": 1,
    "OPEN": 1,
    "CLOSE": 1,
    "NAVIGATE_TO": 1,
    "RELEASE": 0,
    "TOGGLE_ON": 1,
    "TOGGLE_OFF": 1,
    "WIPE": 1,
    "WAIT": 1,
    "WAIT_FOR_COOKED": 1,
    "WAIT_FOR_WASHED": 1,
    "WAIT_FOR_FROZEN": 2,
}


SYMBOLIC_VALID_PRIMITIVES: Dict[str, int] = {
    "GRASP": 1,
    "PLACE_ON_TOP": 1,
    "PLACE_INSIDE": 1,
    "OPEN": 1,
    "CLOSE": 1,
    "TOGGLE_ON": 1,
    "TOGGLE_OFF": 1,
    "SOAK_UNDER": 1,
    "SOAK_INSIDE": 1,
    "WIPE": 1,
    "CUT": 1,
    "PLACE_NEAR_HEATING_ELEMENT": 1,
    "NAVIGATE_TO": 1,
    "RELEASE": 0,
    "WAIT": 1,
    "WAIT_FOR_COOKED": 1,
    "WAIT_FOR_WASHED": 1,
    "WAIT_FOR_FROZEN": 2,
}


VALID_PRIMITIVES_BY_TYPE = {
    "ego": EGO_VALID_PRIMITIVES,
    "starter": STARTER_VALID_PRIMITIVES,
    "symbolic": SYMBOLIC_VALID_PRIMITIVES,
}


def get_valid_primitives(primitive_type: PrimitiveType) -> Dict[str, int]:
    try:
        return VALID_PRIMITIVES_BY_TYPE[primitive_type]
    except KeyError as exc:
        raise ValueError(
            f"Unknown primitive type {primitive_type!r}; "
            f"expected one of {tuple(VALID_PRIMITIVES_BY_TYPE)}"
        ) from exc


def expand_legacy_plan_for_starter(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert legacy two-object actions into grasp + physical starter actions."""
    action = plan["action"].strip()
    match = re.fullmatch(
        r"(PLACE_ON_TOP|PLACE_INSIDE|PLACE_NEXTTO|POUR_INTO|DUMP_INTO)\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
        action,
        flags=re.IGNORECASE,
    )
    if match is None:
        return [plan]

    primitive, target_obj, placement_obj = match.groups()
    expanded = [
        {
            "action": f"navigate_to({target_obj.strip()})",
            "caution": None,
        },
        {
            "action": f"grasp({target_obj.strip()})",
            "caution": None,
        },
    ]
    if primitive.upper() == "PLACE_NEXTTO":
        expanded.extend(
            [
                {
                    "action": f"navigate_to({placement_obj.strip()})",
                    "caution": None,
                },
                {
                    "action": "release()",
                    "caution": plan.get("caution"),
                },
            ]
        )
        return expanded

    expanded.extend(
        [
            {
                "action": f"navigate_to({placement_obj.strip()})",
                "caution": None,
            },
            {
                "action": f"{primitive.lower()}({placement_obj.strip()})",
                "caution": plan.get("caution"),
            },
        ]
    )
    return expanded


def starter_evaluation_action(
    action: str,
    grasped_object: str | None,
) -> str:
    """将 starter 原语集的持物操作格式转回 benchmark 的 legacy 格式。

    Starter 原语集使用"先抓取、再放置/倒入"的流程，操作物体是隐式的
    （即机器人当前夹持的物体）。而评估框架期望的是 legacy ego 格式的动作，
    要求显式包含该物体和目标对象两个参数。

    Args:
        action: Planner 输出的动作字符串，例如 "PLACE_ON_TOP(bottom_cabinet)"。
        grasped_object: 机器人当前夹持的物体名称，例如 "apple"。
            如果为 None，则直接返回原动作。

    Returns:
        Legacy 格式的动作字符串，例如 "place_on_top(apple, bottom_cabinet)"。
        如果动作不是 PLACE_ON_TOP/PLACE_INSIDE/POUR_INTO，或者没有夹持物体，
        则原样返回原始动作。

    Examples:
        >>> starter_evaluation_action("PLACE_ON_TOP(bottom_cabinet)", "apple")
        'place_on_top(apple, bottom_cabinet)'

        >>> starter_evaluation_action("PLACE_INSIDE( drawer )", "tissue_box")
        'place_inside(tissue_box, drawer)'

        >>> starter_evaluation_action("POUR_INTO(vase)", "tupperware")
        'pour_into(tupperware, vase)'

        >>> starter_evaluation_action("NAVIGATE_TO(table)", None)
        'NAVIGATE_TO(table)'           # 非放置动作，原样返回

        >>> starter_evaluation_action("PLACE_ON_TOP(table)", None)
        'PLACE_ON_TOP(table)'           # 无夹持物体，原样返回
    """
    # DUMP_INTO intentionally stays in starter's one-target form because its
    # safety conditions use the held source implicitly as part of the action.
    # 匹配物理持物语法: PRIMITIVE(destination)
    # 例如 "PLACE_ON_TOP(bottom_cabinet)" -> primitive="PLACE_ON_TOP", destination="bottom_cabinet"
    match = re.fullmatch(
        r"\s*(PLACE_ON_TOP|PLACE_INSIDE|POUR_INTO)\s*\(\s*([^,()]+?)\s*\)\s*",
        action,
        re.IGNORECASE,
    )
    if match is None or grasped_object is None:
        return action

    primitive, destination = match.groups()
    return f"{primitive.lower()}({grasped_object}, {destination.strip()})"
