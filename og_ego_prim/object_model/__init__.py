"""Object identity, current state, lifecycle, and manipulation projections."""

from .lifecycle import (
    EntityLifecyclePolicy,
    LifecycleContext,
    LifecycleDirective,
    LifecycleRule,
    LifecycleTransition,
    NullEntityLifecyclePolicy,
    RuleBasedLifecyclePolicy,
)
from .models import ManipulationFact, ObjectRecord
from .registry import ObjectRegistry
from .resolver import AmbiguousEntityAliasError, EntityResolver, normalize_entity_alias
from .factory import (
    LIFECYCLE_POLICIES,
    create_lifecycle_policy,
    create_object_registry,
)

__all__ = [
    "AmbiguousEntityAliasError",
    "EntityLifecyclePolicy",
    "EntityResolver",
    "LifecycleContext",
    "LifecycleDirective",
    "LifecycleRule",
    "LifecycleTransition",
    "LIFECYCLE_POLICIES",
    "ManipulationFact",
    "NullEntityLifecyclePolicy",
    "ObjectRecord",
    "ObjectRegistry",
    "RuleBasedLifecyclePolicy",
    "create_lifecycle_policy",
    "create_object_registry",
    "normalize_entity_alias",
]
