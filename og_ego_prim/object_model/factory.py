"""Registry-backed construction for the Object Module."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Tuple

from og_ego_prim.domain import Registry

from .lifecycle import (
    EntityLifecyclePolicy,
    LifecycleRule,
    LifecycleTransition,
    NullEntityLifecyclePolicy,
    RuleBasedLifecyclePolicy,
)
from .registry import ObjectRegistry


def _config_values(config: Any) -> Dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, Mapping):
        return dict(config)
    fields = (
        "max_manipulations",
        "lifecycle_policy",
        "lifecycle_rules",
        "options",
    )
    return {field: getattr(config, field) for field in fields if hasattr(config, field)}


def _lifecycle_rule(value: Any, ordinal: int) -> LifecycleRule:
    if isinstance(value, LifecycleRule):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("object lifecycle rules must be mappings or LifecycleRule instances")
    transition = value.get("transition") or {}
    if isinstance(transition, LifecycleTransition):
        parsed_transition = transition
    elif isinstance(transition, Mapping):
        directives = transition.get("directives") or ()
        parsed_transition = LifecycleTransition(
            available=transition.get("available"),
            clear_location=bool(transition.get("clear_location", False)),
            state_updates=dict(transition.get("state_updates") or {}),
            reason=transition.get("reason"),
            directives=directives,
            extensions=dict(transition.get("extensions") or {}),
        )
    else:
        raise TypeError("object lifecycle transition must be a mapping")
    return LifecycleRule(
        rule_id=str(value.get("rule_id") or f"lifecycle_{ordinal}"),
        conditions=dict(value.get("conditions") or {}),
        transition=parsed_transition,
        priority=int(value.get("priority", 0)),
        extensions=dict(value.get("extensions") or {}),
    )


def _rule_policy_factory(
    *,
    rules: Iterable[Any] = (),
    **_: Any,
) -> EntityLifecyclePolicy:
    return RuleBasedLifecyclePolicy(
        _lifecycle_rule(value, ordinal)
        for ordinal, value in enumerate(rules, start=1)
    )


LIFECYCLE_POLICIES: Registry[Any] = Registry()
LIFECYCLE_POLICIES.register("rules", _rule_policy_factory)
LIFECYCLE_POLICIES.register("none", lambda **_: NullEntityLifecyclePolicy())
LIFECYCLE_POLICIES.register("disabled", lambda **_: NullEntityLifecyclePolicy())


def create_lifecycle_policy(
    name: str = "rules",
    *,
    rules: Iterable[Any] = (),
    registry: Registry[Any] = LIFECYCLE_POLICIES,
    **options: Any,
) -> EntityLifecyclePolicy:
    factory = registry.require(name)
    policy = factory(rules=rules, **options)
    if not hasattr(policy, "evaluate"):
        raise TypeError("entity lifecycle policy must implement evaluate(context)")
    return policy


def create_object_registry(
    config: Any = None,
    *,
    task_view: Any = None,
    lifecycle_registry: Registry[Any] = LIFECYCLE_POLICIES,
    **overrides: Any,
) -> ObjectRegistry:
    """Build current object state from module config and an agent-visible task view."""

    values = _config_values(config)
    options = dict(values.get("options") or {})
    options.update(overrides)
    max_manipulations = int(
        options.pop("max_manipulations", values.get("max_manipulations", 20))
    )
    policy_name = str(
        options.pop("lifecycle_policy", values.get("lifecycle_policy", "rules"))
    )
    rules = options.pop("lifecycle_rules", values.get("lifecycle_rules", ()))
    lifecycle_options = options.pop("lifecycle_options", {})
    if not isinstance(lifecycle_options, Mapping):
        raise TypeError("object_model.options.lifecycle_options must be a mapping")
    policy = create_lifecycle_policy(
        policy_name,
        rules=rules,
        registry=lifecycle_registry,
        **dict(lifecycle_options),
    )
    object_registry = ObjectRegistry(
        lifecycle_policy=policy,
        manipulation_limit=max_manipulations,
        extensions=options,
    )
    object_ids = getattr(task_view, "object_ids", ()) if task_view is not None else ()
    abilities = getattr(task_view, "object_abilities", {}) if task_view is not None else {}
    for entity_id in object_ids:
        capabilities: Tuple[str, ...] = tuple(abilities.get(entity_id, ()) or ())
        object_registry.upsert(
            str(entity_id),
            capabilities=capabilities,
            properties={str(capability): True for capability in capabilities},
        )
    return object_registry


__all__ = [
    "LIFECYCLE_POLICIES",
    "create_lifecycle_policy",
    "create_object_registry",
]
