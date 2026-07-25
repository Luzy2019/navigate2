"""Current object registry projected from perception and successful actions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from og_ego_prim.domain import ActionRecord, StateChange
from og_ego_prim.utils.serialization import to_builtin

from .lifecycle import (
    EntityLifecyclePolicy,
    LifecycleContext,
    LifecycleDirective,
    LifecycleTransition,
    NullEntityLifecyclePolicy,
)
from .models import ManipulationFact, ObjectRecord, _object_mapping
from .resolver import EntityResolver


def _snapshot_payload(snapshot: Any) -> Mapping[str, Any]:
    if hasattr(snapshot, "to_dict"):
        snapshot = snapshot.to_dict()
    return snapshot if isinstance(snapshot, Mapping) else {}


def _mapping_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        return value.values()
    return value or ()


def _scene_nodes(payload: Mapping[str, Any]) -> Iterable[Tuple[Optional[Any], Mapping[str, Any]]]:
    for room in _mapping_values(payload.get("rooms")):
        if not isinstance(room, Mapping):
            continue
        room_id = room.get("room_id") or room.get("room_name")
        for node in _mapping_values(room.get("nodes")):
            if isinstance(node, Mapping):
                yield room_id, node
        for group in _mapping_values(room.get("groups")):
            if not isinstance(group, Mapping):
                continue
            for node in _mapping_values(group.get("nodes")):
                if isinstance(node, Mapping):
                    yield room_id, node
    flat_nodes = payload.get("nodes") or payload.get("entities") or ()
    for node in _mapping_values(flat_nodes):
        if isinstance(node, Mapping):
            yield node.get("room_id") or node.get("room"), node


class ObjectRegistry:
    def __init__(
        self,
        *,
        resolver: Optional[EntityResolver] = None,
        lifecycle_policy: Optional[EntityLifecyclePolicy] = None,
        manipulation_limit: int = 20,
        extensions: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.schema_version = "isbench.object_registry.v1"
        self.resolver = resolver or EntityResolver()
        self.lifecycle_policy = lifecycle_policy or NullEntityLifecyclePolicy()
        self.manipulation_limit = int(manipulation_limit)
        if self.manipulation_limit <= 0:
            raise ValueError("manipulation_limit must be greater than zero")
        self.extensions = deepcopy(dict(extensions or {}))
        self._records: Dict[str, ObjectRecord] = {}

    def register(
        self,
        record: ObjectRecord,
        *,
        aliases: Iterable[str] = (),
        replace: bool = False,
    ) -> ObjectRecord:
        if record.entity_id in self._records and not replace:
            raise ValueError(f"object {record.entity_id!r} is already registered")
        record.manipulation_limit = self.manipulation_limit
        if len(record.manipulations) > self.manipulation_limit:
            del record.manipulations[: -self.manipulation_limit]
        record.add_aliases(*aliases)
        self._records[record.entity_id] = record
        self.resolver.register(record.entity_id, record.aliases, replace=replace)
        return record

    def upsert(
        self,
        entity_id: str,
        *,
        canonical_name: Optional[str] = None,
        aliases: Iterable[str] = (),
        properties: Optional[Mapping[str, Any]] = None,
        states: Optional[Mapping[str, Any]] = None,
        capabilities: Iterable[str] = (),
        room_id: Optional[str] = None,
        available: Optional[bool] = None,
        last_seen_step: Optional[int] = None,
    ) -> ObjectRecord:
        canonical = str(entity_id or "").strip()
        if not canonical:
            raise ValueError("entity_id must not be empty")
        record = self._records.get(canonical)
        if record is None:
            record = ObjectRecord(
                entity_id=canonical,
                canonical_name=canonical_name or canonical,
                aliases=set(aliases),
                properties=dict(properties or {}),
                states=dict(states or {}),
                capabilities=set(capabilities),
                room_id=room_id,
                available=True if available is None else available,
                last_seen_step=last_seen_step,
                manipulation_limit=self.manipulation_limit,
            )
            return self.register(record)

        if canonical_name:
            normalized_name = str(canonical_name).strip()
            if normalized_name:
                record.canonical_name = normalized_name
        record.add_aliases(*aliases, record.canonical_name)
        record.properties.update(_object_mapping(properties))
        record.states.update(_object_mapping(states))
        record.capabilities.update(
            str(value).strip().lower()
            for value in capabilities
            if str(value).strip()
        )
        if room_id is not None:
            record.room_id = str(room_id)
        if available is not None:
            record.available = bool(available)
        if last_seen_step is not None:
            record.last_seen_step = int(last_seen_step)
        self.resolver.register(record.entity_id, record.aliases)
        return record

    def resolve(self, entity_id_or_alias: object, *, strict: bool = True) -> Optional[str]:
        raw = str(entity_id_or_alias or "").strip()
        if raw in self._records:
            return raw
        return self.resolver.resolve(entity_id_or_alias, strict=strict)

    def get(self, entity_id_or_alias: object) -> Optional[ObjectRecord]:
        entity_id = self.resolve(entity_id_or_alias, strict=False)
        return self._records.get(entity_id) if entity_id is not None else None

    def require(self, entity_id_or_alias: object) -> ObjectRecord:
        entity_id = self.resolve(entity_id_or_alias)
        if entity_id is None:
            raise KeyError(f"unknown object {entity_id_or_alias!r}")
        return self._records[entity_id]

    def snapshot(self, *, actionable_only: bool = False) -> Tuple[ObjectRecord, ...]:
        records = (
            record
            for _, record in sorted(self._records.items())
            if not actionable_only or record.actionable
        )
        return tuple(records)

    def actionable(self) -> Tuple[ObjectRecord, ...]:
        return self.snapshot(actionable_only=True)

    def update_from_scene_graph(self, snapshot: Any) -> Tuple[ObjectRecord, ...]:
        payload = _snapshot_payload(snapshot)
        summary = payload.get("summary")
        summary = summary if isinstance(summary, Mapping) else {}
        step = int(payload.get("step_index") or summary.get("global_step_index") or 0)
        updated: List[ObjectRecord] = []
        for room_id, node in _scene_nodes(payload):
            explicit_entity_id = str(node.get("entity_id") or "").strip()
            entity_id = (
                explicit_entity_id
                or node.get("id")
                or node.get("object_id")
                or node.get("label")
            )
            if not entity_id:
                continue
            aliases = tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in (
                        node.get("entity_id"),
                        node.get("id"),
                        node.get("label"),
                        node.get("name"),
                        node.get("object_id"),
                    )
                    if value is not None and str(value).strip()
                )
            )
            if not explicit_entity_id:
                resolved = None
                for alias in aliases:
                    candidate = self.resolver.resolve(alias, strict=False)
                    if candidate is not None:
                        resolved = candidate
                        break
                entity_id = resolved or str(entity_id)
            last_seen = node.get("last_seen_step")
            if last_seen is None and node.get("is_vis", node.get("visible", True)):
                last_seen = step
            states = node.get("states")
            if not isinstance(states, Mapping):
                states = node.get("attributes")
            states = states if isinstance(states, Mapping) else {}
            properties = node.get("properties")
            properties = properties if isinstance(properties, Mapping) else {}
            capabilities = node.get("capabilities") or node.get("abilities") or ()
            capabilities = (capabilities,) if isinstance(capabilities, str) else capabilities
            if not isinstance(capabilities, (list, tuple, set, frozenset)):
                capabilities = (capabilities,)
            updated.append(
                self.upsert(
                    entity_id,
                    canonical_name=node.get("name") or node.get("label") or str(entity_id),
                    aliases=aliases,
                    properties=properties,
                    states=states,
                    capabilities=capabilities,
                    room_id=str(room_id) if room_id is not None else None,
                    available=node.get("available"),
                    last_seen_step=last_seen,
                )
            )
        updated_ids = tuple(dict.fromkeys(record.entity_id for record in updated))
        return tuple(
            self._records[entity_id]
            for entity_id in updated_ids
        )

    def apply_state_change(self, change: StateChange) -> ObjectRecord:
        record = self.get(change.entity_id)
        if record is None:
            record = self.upsert(change.entity_id, room_id=change.room_id)
        record.update_state(change)
        return record

    def record_action(
        self,
        record: ActionRecord,
        *,
        entity_ids: Optional[Iterable[str]] = None,
        lifecycle_metadata: Optional[Mapping[str, Any]] = None,
        directive_sink: Optional[
            Callable[[LifecycleDirective, LifecycleContext, LifecycleTransition], None]
        ] = None,
    ) -> Tuple[ManipulationFact, ...]:
        if not record.succeeded:
            return ()
        action = record.action
        tool_id = action.parameters.get("tool_id")
        related_ids = action.parameters.get("entity_ids", ())
        if isinstance(related_ids, str):
            related_ids = (related_ids,)
        elif not isinstance(related_ids, (list, tuple, set, frozenset)):
            related_ids = (related_ids,) if related_ids is not None else ()
        identity_values = (action.object_id, action.target_id, tool_id, *related_ids)
        canonical_ids: Dict[str, str] = {}
        for identifier in identity_values:
            if identifier is None:
                continue
            raw = str(identifier).strip()
            if raw:
                matches = self.resolver.resolve_all(raw)
                if len(matches) <= 1:
                    canonical_ids[raw] = matches[0] if matches else raw
        roles = {}
        for raw, role in (
            (action.object_id, "object"),
            (action.target_id, "target"),
            (tool_id, "tool"),
        ):
            if raw is None:
                continue
            canonical = canonical_ids.get(str(raw).strip(), str(raw).strip())
            roles.setdefault(canonical, role)
        selected = tuple(entity_ids) if entity_ids is not None else tuple(
            value for value in identity_values if value is not None
        )
        selected_canonical = []
        for identifier in selected:
            raw = str(identifier).strip()
            if not raw:
                continue
            matches = self.resolver.resolve_all(raw)
            if len(matches) > 1:
                continue
            canonical = matches[0] if matches else canonical_ids.get(raw, raw)
            if canonical not in selected_canonical:
                selected_canonical.append(canonical)
        canonical_target = (
            None
            if action.target_id is None
            else canonical_ids.get(str(action.target_id).strip())
        )
        canonical_tool = (
            None
            if tool_id is None
            else canonical_ids.get(str(tool_id).strip())
        )
        actor_matches = self.resolver.resolve_all(action.actor_id) if action.actor_id else ()
        canonical_actor = (
            actor_matches[0]
            if len(actor_matches) == 1
            else action.actor_id if not actor_matches else None
        )
        facts = []
        for identifier in selected_canonical:
            obj = self.get(identifier)
            if obj is None:
                obj = self.upsert(str(identifier))
            fact = ManipulationFact(
                action_id=record.action_id,
                action=action.name,
                step=record.step,
                actor_id=canonical_actor,
                tool_id=canonical_tool,
                target_id=canonical_target,
                role=roles.get(identifier, "related"),
                count=record.count,
            )
            obj.add_manipulation(fact)
            facts.append(fact)
        self.apply_lifecycle(
            record,
            metadata=lifecycle_metadata,
            directive_sink=directive_sink,
        )
        return tuple(facts)

    def apply_lifecycle(
        self,
        record: ActionRecord,
        *,
        subject_id: Optional[str] = None,
        target_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        directive_sink: Optional[
            Callable[[LifecycleDirective, LifecycleContext, LifecycleTransition], None]
        ] = None,
    ) -> Tuple[LifecycleTransition, ...]:
        subject = self.get(subject_id or record.action.object_id)
        if subject is None:
            return ()
        target = self.get(target_id or record.action.target_id)
        context = LifecycleContext(
            record=record,
            subject=subject,
            target=target,
            metadata=dict(metadata or {}),
        )
        transitions = tuple(self.lifecycle_policy.evaluate(context))
        for transition in transitions:
            if transition.available is not None:
                subject.available = bool(transition.available)
            if transition.clear_location:
                subject.room_id = None
                for key in ("inside", "on_top", "next_to", "location"):
                    subject.states.pop(key, None)
            state_updates = _object_mapping(transition.state_updates)
            subject.states.update(state_updates)
            if directive_sink is not None:
                for directive in transition.directives:
                    directive_sink(directive, context, transition)
        return transitions

    def clear_manipulations(self) -> None:
        for record in self._records.values():
            record.clear_manipulations()

    def clear(self) -> None:
        self._records.clear()
        self.resolver.clear()

    def to_dict(self) -> Dict[str, Any]:
        return to_builtin(
            {
                "schema_version": self.schema_version,
                "objects": [record.to_dict() for record in self.snapshot()],
                "extensions": dict(self.extensions),
            }
        )

    def __len__(self) -> int:
        return len(self._records)


__all__ = ["ObjectRegistry"]
