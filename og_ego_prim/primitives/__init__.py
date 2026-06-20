from typing import TYPE_CHECKING

from .specs import (
    EGO_VALID_PRIMITIVES,
    STARTER_VALID_PRIMITIVES,
    SYMBOLIC_VALID_PRIMITIVES,
    get_valid_primitives,
)

if TYPE_CHECKING:
    from .ego_primitives import (
        EgoSemanticActionPrimitiveSet,
        EgoSemanticActionPrimitives,
    )
    from .executor import Executor
    from .primitive_utils import find_task_related_object


def __getattr__(name):
    if name in {
        "EgoSemanticActionPrimitiveSet",
        "EgoSemanticActionPrimitives",
        "VALID_PRIMITIVES",
    }:
        from .ego_primitives import (
            EgoSemanticActionPrimitiveSet,
            EgoSemanticActionPrimitives,
            VALID_PRIMITIVES,
        )

        return {
            "EgoSemanticActionPrimitiveSet": EgoSemanticActionPrimitiveSet,
            "EgoSemanticActionPrimitives": EgoSemanticActionPrimitives,
            "VALID_PRIMITIVES": VALID_PRIMITIVES,
        }[name]
    if name == "Executor":
        from .executor import Executor

        return Executor
    if name == "find_task_related_object":
        from .primitive_utils import find_task_related_object

        return find_task_related_object
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EGO_VALID_PRIMITIVES",
    "EgoSemanticActionPrimitiveSet",
    "EgoSemanticActionPrimitives",
    "Executor",
    "STARTER_VALID_PRIMITIVES",
    "SYMBOLIC_VALID_PRIMITIVES",
    "VALID_PRIMITIVES",
    "find_task_related_object",
    "get_valid_primitives",
]
