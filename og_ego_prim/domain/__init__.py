"""Public contracts for the modular IS-Bench agent runtime."""

from og_ego_prim.utils.serialization import (
    ExtensionMap,
    VersionedPayload,
    as_versioned_dict,
    to_builtin,
)

from .contracts import (
    Action,
    ActionDecision,
    ActionRecord,
    PlannerEpisodeEntry,
    StateChange,
)
from .protocols import ActionExecutor, PerceptionProvider
from .registry import (
    DuplicateRegistrationError,
    Registry,
    UnknownRegistrationError,
    normalize_registration_name,
)

__all__ = [
    "Action",
    "ActionDecision",
    "ActionExecutor",
    "ActionRecord",
    "DuplicateRegistrationError",
    "ExtensionMap",
    "PerceptionProvider",
    "PlannerEpisodeEntry",
    "Registry",
    "StateChange",
    "UnknownRegistrationError",
    "VersionedPayload",
    "as_versioned_dict",
    "normalize_registration_name",
    "to_builtin",
]
