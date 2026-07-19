from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from .clock import SimulationClock
from .models import (
    ProcessStatus,
    ProcessUpdate,
    SCHEMA_VERSION,
    ScheduledProcess,
    TemporalEvent,
    make_process_id,
    normalize_action_name,
)


@dataclass
class HandlerContext:
    clock: SimulationClock
    state_adapter: "TemporalStateAdapter"
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def step(self) -> int:
        return self.clock.step


@runtime_checkable
class TemporalStateAdapter(Protocol):
    """Bridge to simulator state or a derived semantic-state store.

    ``readiness`` returns ``None`` when the predicate is unsupported. Effects
    are never written to a scene graph by this interface.
    """

    def prepare_start(
        self,
        event: TemporalEvent,
        definition: "ProcessDefinition",
        entity_ids: Tuple[str, ...],
        context: HandlerContext,
    ) -> Optional["ProcessStart"]:
        ...

    def readiness(
        self,
        process: ScheduledProcess,
        predicate: str,
        context: HandlerContext,
    ) -> Optional[bool]:
        ...

    def apply_effects(
        self,
        process: ScheduledProcess,
        effects: Mapping[str, Any],
        context: HandlerContext,
    ) -> Optional[bool]:
        ...


class NullTemporalStateAdapter:
    def prepare_start(
        self,
        event: TemporalEvent,
        definition: "ProcessDefinition",
        entity_ids: Tuple[str, ...],
        context: HandlerContext,
    ) -> Optional["ProcessStart"]:
        conditions = definition.extensions.get("conditions_by_action", {})
        selectors = definition.extensions.get("entity_selectors", {})
        if (
            isinstance(conditions, Mapping) and event.action_name in conditions
        ) or (
            isinstance(selectors, Mapping) and event.action_name in selectors
        ):
            return None
        return ProcessStart(entity_ids=entity_ids, start_step=event.step)

    def readiness(
        self,
        process: ScheduledProcess,
        predicate: str,
        context: HandlerContext,
    ) -> Optional[bool]:
        return None

    def apply_effects(
        self,
        process: ScheduledProcess,
        effects: Mapping[str, Any],
        context: HandlerContext,
    ) -> Optional[bool]:
        return None


ReadinessCallback = Callable[[ScheduledProcess, str, HandlerContext], Optional[bool]]
StateEffectCallback = Callable[
    [ScheduledProcess, Mapping[str, Any], HandlerContext], Optional[bool]
]


@dataclass(frozen=True)
class ProcessStart:
    """Adapter-prepared start data for a configured temporal process."""

    entity_ids: Tuple[str, ...]
    start_step: int
    extensions: Mapping[str, Any] = field(default_factory=dict)


ProcessStartCallback = Callable[
    [TemporalEvent, "ProcessDefinition", Tuple[str, ...], HandlerContext],
    Optional[ProcessStart],
]


class ContextTemporalStateAdapter:
    """Delegate simulator-specific temporal semantics to the injected executor.

    The scheduler remains usable without an executor: start preparation then
    falls back to the action entities, while readiness and state writes remain
    unsupported and are represented as derived Object Module state.
    """

    @staticmethod
    def _executor(context: HandlerContext) -> Any:
        return context.data.get("executor")

    def prepare_start(
        self,
        event: TemporalEvent,
        definition: "ProcessDefinition",
        entity_ids: Tuple[str, ...],
        context: HandlerContext,
    ) -> Optional[ProcessStart]:
        executor = self._executor(context)
        callback = getattr(executor, "prepare_temporal_process", None)
        if callable(callback):
            return callback(event, definition, entity_ids, context)
        return NullTemporalStateAdapter().prepare_start(
            event,
            definition,
            entity_ids,
            context,
        )

    def readiness(
        self,
        process: ScheduledProcess,
        predicate: str,
        context: HandlerContext,
    ) -> Optional[bool]:
        executor = self._executor(context)
        callback = getattr(executor, "temporal_readiness", None)
        return callback(process, predicate, context) if callable(callback) else None

    def apply_effects(
        self,
        process: ScheduledProcess,
        effects: Mapping[str, Any],
        context: HandlerContext,
    ) -> Optional[bool]:
        executor = self._executor(context)
        callback = getattr(executor, "apply_temporal_effects", None)
        return callback(process, effects, context) if callable(callback) else None


class CallbackTemporalStateAdapter:
    def __init__(
        self,
        *,
        readiness: Optional[ReadinessCallback] = None,
        apply_effects: Optional[StateEffectCallback] = None,
        prepare_start: Optional[ProcessStartCallback] = None,
        fallback: Optional[TemporalStateAdapter] = None,
    ):
        self._readiness = readiness
        self._apply_effects = apply_effects
        self._prepare_start = prepare_start
        self._fallback = fallback or ContextTemporalStateAdapter()

    def prepare_start(
        self,
        event: TemporalEvent,
        definition: "ProcessDefinition",
        entity_ids: Tuple[str, ...],
        context: HandlerContext,
    ) -> Optional[ProcessStart]:
        if self._prepare_start is not None:
            return self._prepare_start(event, definition, entity_ids, context)
        fallback = getattr(self._fallback, "prepare_start", None)
        if callable(fallback):
            return fallback(event, definition, entity_ids, context)
        return NullTemporalStateAdapter().prepare_start(
            event,
            definition,
            entity_ids,
            context,
        )

    def readiness(
        self,
        process: ScheduledProcess,
        predicate: str,
        context: HandlerContext,
    ) -> Optional[bool]:
        result = (
            self._readiness(process, predicate, context)
            if self._readiness is not None
            else None
        )
        if result is not None:
            return result
        return self._fallback.readiness(process, predicate, context)

    def apply_effects(
        self,
        process: ScheduledProcess,
        effects: Mapping[str, Any],
        context: HandlerContext,
    ) -> Optional[bool]:
        callback_result = (
            self._apply_effects(process, effects, context)
            if self._apply_effects is not None
            else None
        )
        if callback_result is not None:
            return callback_result
        return self._fallback.apply_effects(process, effects, context)


@runtime_checkable
class ProcessHandler(Protocol):
    @property
    def process_type(self) -> str:
        ...

    def start(
        self,
        event: TemporalEvent,
        context: HandlerContext,
    ) -> Optional[ScheduledProcess]:
        ...

    def poll(
        self,
        process: ScheduledProcess,
        context: HandlerContext,
    ) -> ProcessUpdate:
        ...


class ProcessHandlerRegistry:
    def __init__(self, handlers: Iterable[ProcessHandler] = ()):
        self._handlers: Dict[str, ProcessHandler] = {}
        for handler in handlers:
            self.register(handler)

    @staticmethod
    def _key(process_type: str) -> str:
        key = str(process_type).strip().lower()
        if not key:
            raise ValueError("process_type cannot be empty")
        return key

    def register(self, handler: ProcessHandler, *, replace_existing: bool = False) -> None:
        key = self._key(handler.process_type)
        if key in self._handlers and not replace_existing:
            raise ValueError(f"process handler {key!r} is already registered")
        self._handlers[key] = handler

    def unregister(self, process_type: str) -> Optional[ProcessHandler]:
        return self._handlers.pop(self._key(process_type), None)

    def get(self, process_type: str) -> Optional[ProcessHandler]:
        return self._handlers.get(self._key(process_type))

    def require(self, process_type: str) -> ProcessHandler:
        handler = self.get(process_type)
        if handler is None:
            raise KeyError(f"no process handler registered for {process_type!r}")
        return handler

    def __iter__(self) -> Iterator[ProcessHandler]:
        return iter(tuple(self._handlers.values()))

    def __len__(self) -> int:
        return len(self._handlers)


def _as_names(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(name for name in (normalize_action_name(item) for item in value) if name)


def _as_fields(value: Any, default: Sequence[str]) -> Tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class ProcessDefinition:
    process_type: str
    trigger_actions: Tuple[str, ...]
    duration_steps: Optional[int]
    entity_fields: Tuple[str, ...] = ("object_id",)
    parameter_indices: Tuple[int, ...] = (0,)
    readiness_predicate: Optional[str] = None
    blocking_actions: Tuple[str, ...] = ()
    completion_effects: Dict[str, Any] = field(default_factory=dict)
    required_attributes: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    schema_version: str = SCHEMA_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.process_type = str(self.process_type).strip().lower()
        if not self.process_type:
            raise ValueError("process_type cannot be empty")
        self.trigger_actions = _as_names(self.trigger_actions)
        self.duration_steps = None if self.duration_steps is None else int(self.duration_steps)
        if self.duration_steps is not None and self.duration_steps < 0:
            raise ValueError("duration_steps cannot be negative")
        if self.duration_steps is None and not self.readiness_predicate:
            raise ValueError("a process requires duration_steps or readiness_predicate")
        self.entity_fields = _as_fields(self.entity_fields, ("object_id",))
        self.parameter_indices = tuple(int(index) for index in self.parameter_indices)
        self.blocking_actions = _as_names(self.blocking_actions)
        self.completion_effects = dict(self.completion_effects or {})
        self.required_attributes = dict(self.required_attributes or {})
        self.extensions = dict(self.extensions or {})
        for key in ("conditions_by_action", "entity_selectors"):
            values = self.extensions.get(key)
            if isinstance(values, Mapping):
                self.extensions[key] = {
                    normalize_action_name(action_name): value
                    for action_name, value in values.items()
                }

    @classmethod
    def from_mapping(
        cls,
        process_type: str,
        value: Mapping[str, Any],
        *,
        base: Optional["ProcessDefinition"] = None,
    ) -> "ProcessDefinition":
        source: Dict[str, Any] = {}
        base_extensions: Dict[str, Any] = {}
        if base is not None:
            source = base.to_dict()
            base_extensions = dict(source.get("extensions") or {})
        source.update(dict(value or {}))
        extensions = {
            **base_extensions,
            **dict(source.get("extensions") or {}),
        }
        return cls(
            process_type=process_type,
            trigger_actions=_as_names(source.get("trigger_actions")),
            duration_steps=(
                None if source.get("duration_steps") is None else int(source["duration_steps"])
            ),
            entity_fields=_as_fields(source.get("entity_fields"), ("object_id",)),
            parameter_indices=tuple(int(index) for index in source.get("parameter_indices", (0,))),
            readiness_predicate=source.get("readiness_predicate"),
            blocking_actions=_as_names(source.get("blocking_actions")),
            completion_effects=dict(source.get("completion_effects") or {}),
            required_attributes=dict(source.get("required_attributes") or {}),
            enabled=_to_bool(source.get("enabled"), True),
            schema_version=str(source.get("schema_version", SCHEMA_VERSION)),
            extensions=extensions,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "process_type": self.process_type,
            "trigger_actions": list(self.trigger_actions),
            "duration_steps": self.duration_steps,
            "entity_fields": list(self.entity_fields),
            "parameter_indices": list(self.parameter_indices),
            "readiness_predicate": self.readiness_predicate,
            "blocking_actions": list(self.blocking_actions),
            "completion_effects": dict(self.completion_effects),
            "required_attributes": dict(self.required_attributes),
            "enabled": self.enabled,
            "extensions": dict(self.extensions),
        }


EventFilter = Callable[[TemporalEvent, HandlerContext], bool]


class ConfiguredProcessHandler:
    """Data-driven handler shared by all built-in temporal processes."""

    def __init__(
        self,
        definition: ProcessDefinition,
        *,
        event_filter: Optional[EventFilter] = None,
    ):
        self.definition = definition
        self._event_filter = event_filter

    @property
    def process_type(self) -> str:
        return self.definition.process_type

    def _matches(self, event: TemporalEvent, context: HandlerContext) -> bool:
        if not self.definition.enabled or not event.success:
            return False
        if event.action_name not in self.definition.trigger_actions:
            return False
        if any(event.attributes.get(key) != value for key, value in self.definition.required_attributes.items()):
            return False
        return self._event_filter(event, context) if self._event_filter is not None else True

    def _entities(self, event: TemporalEvent) -> Tuple[str, ...]:
        entities = []
        for field_name in self.definition.entity_fields:
            value = getattr(event, field_name, None)
            if value and value not in entities:
                entities.append(str(value))

        if not entities:
            parameters = event.parameters
            if isinstance(parameters, Mapping):
                arguments = parameters.get("arguments")
                positional: Sequence[Any] = (
                    arguments
                    if isinstance(arguments, Sequence)
                    and not isinstance(arguments, (str, bytes))
                    else tuple(parameters.values())
                )
            elif isinstance(parameters, Sequence) and not isinstance(parameters, (str, bytes)):
                positional = parameters
            else:
                positional = ()
            for index in self.definition.parameter_indices:
                if -len(positional) <= index < len(positional):
                    value = positional[index]
                    if value is not None and str(value) not in entities:
                        entities.append(str(value))

        if not entities:
            entities.extend(
                entity_id
                for entity_id in event.entity_ids
                if entity_id != event.actor_id
            )
        return tuple(entities)

    def start(
        self,
        event: TemporalEvent,
        context: HandlerContext,
    ) -> Optional[ScheduledProcess]:
        if not self._matches(event, context):
            return None
        entities = self._entities(event)
        prepare_start = getattr(context.state_adapter, "prepare_start", None)
        conditions = self.definition.extensions.get("conditions_by_action", {})
        selectors = self.definition.extensions.get("entity_selectors", {})
        requires_adapter = (
            isinstance(conditions, Mapping) and event.action_name in conditions
        ) or (
            isinstance(selectors, Mapping) and event.action_name in selectors
        )
        if requires_adapter and not callable(prepare_start):
            return None
        prepared = (
            prepare_start(event, self.definition, entities, context)
            if callable(prepare_start)
            else ProcessStart(entity_ids=entities, start_step=event.step)
        )
        if prepared is None:
            return None
        entities = tuple(prepared.entity_ids)
        if not entities and not self.definition.extensions.get("allow_global", False):
            return None

        start_step = int(prepared.start_step)
        duration_actions = _as_names(
            self.definition.extensions.get("duration_actions", ())
        )
        include_action_duration = _to_bool(
            self.definition.extensions.get("include_action_duration"),
            False,
        )
        if include_action_duration and (
            not duration_actions or event.action_name in duration_actions
        ):
            diagnostics = event.extensions.get("diagnostics", {})
            elapsed_steps = (
                diagnostics.get("low_level_steps", 0)
                if isinstance(diagnostics, Mapping)
                else 0
            )
            start_step = max(0, start_step - max(0, int(elapsed_steps or 0)))
        duration_attribute = str(
            self.definition.extensions.get("duration_attribute", "duration_steps")
        )
        duration_steps = event.attributes.get(
            duration_attribute,
            self.definition.duration_steps,
        )
        duration_steps = None if duration_steps is None else int(duration_steps)
        if duration_steps is not None and duration_steps < 0:
            raise ValueError("event duration_steps cannot be negative")
        ready_step = (
            None
            if duration_steps is None
            else start_step + duration_steps
        )
        instance_attribute = self.definition.extensions.get("instance_attribute")
        instance_key = (
            str(event.attributes.get(instance_attribute))
            if instance_attribute and event.attributes.get(instance_attribute) is not None
            else None
        )
        return ScheduledProcess(
            process_id=make_process_id(self.process_type, entities, instance_key=instance_key),
            process_type=self.process_type,
            entity_ids=entities,
            source_action_id=event.action_id or event.event_id,
            start_step=start_step,
            ready_step=ready_step,
            readiness_predicate=self.definition.readiness_predicate,
            blocking_actions=self.definition.blocking_actions,
            completion_effects=self.definition.completion_effects,
            schema_version=self.definition.schema_version,
            extensions={
                **self.definition.extensions,
                **dict(prepared.extensions or {}),
                "trigger_action": event.action_name,
            },
        )

    def poll(
        self,
        process: ScheduledProcess,
        context: HandlerContext,
    ) -> ProcessUpdate:
        now = context.step
        predicate_result: Optional[bool] = None
        if process.readiness_predicate:
            predicate_result = context.state_adapter.readiness(
                process,
                process.readiness_predicate,
                context,
            )
            if predicate_result is True:
                return self._ready_update(
                    process,
                    now,
                    "readiness_predicate_satisfied",
                    None,
                    state_effects={},
                )

        if process.ready_step is None or now < process.ready_step:
            return self._pending_update(process, now, "waiting_for_time_or_state")

        applied: Optional[bool] = None
        if process.completion_effects:
            applied = context.state_adapter.apply_effects(
                process,
                process.completion_effects,
                context,
            )
            if applied is False:
                return self._pending_update(process, now, "completion_effects_not_applied")

        # A known false predicate cannot be completed by elapsed time alone.
        # A successful adapter write is authoritative for this tick; simulator
        # perception verifies the state again on the following observation.
        allow_derived_completion = _to_bool(
            process.extensions.get("allow_derived_completion"),
            False,
        )
        if (
            predicate_result is False
            and applied is not True
            and not allow_derived_completion
        ):
            return self._pending_update(process, now, "readiness_predicate_not_satisfied")

        return self._ready_update(
            process,
            now,
            "deadline_reached",
            applied,
            state_effects=process.completion_effects,
        )

    @staticmethod
    def _pending_update(
        process: ScheduledProcess,
        step: int,
        reason: str,
    ) -> ProcessUpdate:
        return ProcessUpdate(
            process_id=process.process_id,
            process_type=process.process_type,
            status=ProcessStatus.PENDING,
            step=step,
            entity_ids=process.entity_ids,
            reason=reason,
        )

    @staticmethod
    def _ready_update(
        process: ScheduledProcess,
        step: int,
        reason: str,
        effects_applied: Optional[bool],
        *,
        state_effects: Mapping[str, Any],
    ) -> ProcessUpdate:
        return ProcessUpdate(
            process_id=process.process_id,
            process_type=process.process_type,
            status=ProcessStatus.READY,
            step=step,
            entity_ids=process.entity_ids,
            reason=reason,
            state_effects=dict(state_effects),
            extensions={"state_effects_applied": effects_applied},
        )


BUILTIN_PROCESS_DEFINITIONS: Dict[str, ProcessDefinition] = {
    "spoilage": ProcessDefinition(
        process_type="spoilage",
        trigger_actions=(
            "START_SPOILAGE",
            "REMOVE_FROM_COLD_STORAGE",
            "PLACE_ON_TOP",
            "PLACE_NEXTTO",
        ),
        duration_steps=7200,
        readiness_predicate="spoiled",
        completion_effects={"spoiled": True},
        extensions={
            "description": "food shelf-life or cold-chain deadline",
            "conditions_by_action": {
                "PLACE_ON_TOP": {
                    "entities_support_any": ["spoiled", "cooked", "frozen"]
                },
                "PLACE_NEXTTO": {
                    "entities_support_any": ["spoiled", "cooked", "frozen"]
                },
            },
            "allow_derived_completion": True,
            "cancels": ["freezing"],
        },
    ),
    "mop_drying": ProcessDefinition(
        process_type="mop_drying",
        trigger_actions=("START_MOP_DRYING", "WIPE_FLOOR", "WIPE"),
        duration_steps=60,
        readiness_predicate="dry",
        blocking_actions=("TOGGLE_ON", "PLACE_ON_TOP", "PLACE_INSIDE"),
        completion_effects={"wet": False},
        extensions={
            "description": "wet floor or mop drying",
            "entity_selectors": {"WIPE": "target_or_object"},
            "conditions_by_action": {
                "WIPE": {"entity_name_contains": ["floor"]},
            },
            "allow_derived_completion": True,
        },
    ),
    "thawing": ProcessDefinition(
        process_type="thawing",
        trigger_actions=(
            "START_THAWING",
            "REMOVE_FROM_FREEZER",
            "PLACE_ON_TOP",
            "PLACE_NEXTTO",
            "WAIT",
        ),
        duration_steps=60,
        readiness_predicate="not_frozen",
        blocking_actions=("CUT", "COOK", "PLACE_NEAR_HEATING_ELEMENT"),
        completion_effects={"frozen": False},
        extensions={
            "description": "frozen object thawing",
            "conditions_by_action": {
                "PLACE_ON_TOP": {"entity_states": {"frozen": True}},
                "PLACE_NEXTTO": {"entity_states": {"frozen": True}},
                "WAIT": {"entity_states": {"frozen": True}},
            },
            "duration_actions": ["WAIT"],
            "include_action_duration": True,
            "cancels": ["freezing"],
            "cancel_actions": ["PLACE_INSIDE"],
        },
    ),
    "heating": ProcessDefinition(
        process_type="heating",
        trigger_actions=("START_HEATING", "TOGGLE_ON", "WAIT_FOR_COOKED"),
        duration_steps=60,
        readiness_predicate="cooked_or_heated",
        blocking_actions=("POUR_INTO", "GRASP"),
        completion_effects={"heated": True, "cooked": True},
        extensions={
            "description": "object heating",
            "entity_selectors": {"TOGGLE_ON": "placements_of_object"},
            "conditions_by_action": {
                "TOGGLE_ON": {"source_supports_any": ["heat_source"]},
                "WAIT_FOR_COOKED": {
                    "entities_support_any": ["cooked", "heated"]
                },
            },
            "duration_actions": ["WAIT_FOR_COOKED"],
            "include_action_duration": True,
            "cancels": ["cooling"],
            "cancel_actions": ["TOGGLE_OFF"],
        },
    ),
    "cooling": ProcessDefinition(
        process_type="cooling",
        trigger_actions=("START_COOLING", "REMOVE_FROM_HEAT", "TOGGLE_OFF", "WAIT"),
        duration_steps=60,
        readiness_predicate="not_heated",
        blocking_actions=("GRASP", "WIPE", "PLACE_INSIDE", "CLOSE"),
        completion_effects={"heated": False},
        extensions={
            "description": "hot object cooling",
            "entity_selectors": {"TOGGLE_OFF": "placements_of_object"},
            "conditions_by_action": {
                "TOGGLE_OFF": {"source_supports_any": ["heat_source"]},
                "WAIT": {"entity_states": {"heated": True}},
            },
            "duration_actions": ["WAIT"],
            "include_action_duration": True,
            "cancels": ["heating"],
            "cancel_actions": ["TOGGLE_ON"],
        },
    ),
    # Compatibility processes keep legacy benchmark actions declarative. They
    # use the same ConfiguredProcessHandler as every other process and can be
    # disabled or replaced through scheduler configuration.
    "washing": ProcessDefinition(
        process_type="washing",
        trigger_actions=("TOGGLE_ON", "WAIT_FOR_WASHED"),
        duration_steps=60,
        readiness_predicate="washed",
        blocking_actions=("OPEN", "GRASP"),
        completion_effects={"covered": False},
        extensions={
            "description": "washer or dishwasher cleaning cycle",
            "entity_selectors": {
                "TOGGLE_ON": "contents_of_object",
                "WAIT_FOR_WASHED": "contents_of_object",
            },
            "conditions_by_action": {
                "TOGGLE_ON": {
                    "source_name_contains": ["washer", "dishwasher"]
                },
                "WAIT_FOR_WASHED": {
                    "source_name_contains": ["washer", "dishwasher"],
                    "source_states": {"open": False, "toggled_on": True},
                },
            },
            "duration_actions": ["WAIT_FOR_WASHED"],
            "include_action_duration": True,
            "cancel_actions": ["TOGGLE_OFF", "OPEN"],
        },
    ),
    "freezing": ProcessDefinition(
        process_type="freezing",
        trigger_actions=("PLACE_INSIDE", "WAIT_FOR_FROZEN"),
        duration_steps=60,
        readiness_predicate="frozen",
        blocking_actions=("GRASP", "CUT", "COOK"),
        completion_effects={"frozen": True},
        extensions={
            "description": "object freezing inside cold storage",
            "conditions_by_action": {
                "PLACE_INSIDE": {
                    "target_name_contains": ["fridge", "refrigerator", "freezer"],
                    "entities_support_any": ["frozen"],
                    "entity_inside_target": True,
                },
                "WAIT_FOR_FROZEN": {
                    "target_name_contains": ["fridge", "refrigerator", "freezer"],
                    "entities_support_any": ["frozen"],
                    "entity_inside_target": True,
                },
            },
            "duration_actions": ["WAIT_FOR_FROZEN"],
            "include_action_duration": True,
            "cancels": ["thawing", "spoilage"],
            "cancel_actions": ["GRASP", "PLACE_ON_TOP", "PLACE_NEXTTO"],
        },
    ),
}


def builtin_process_definitions(
    overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, ProcessDefinition]:
    overrides = dict(overrides or {})
    definitions: Dict[str, ProcessDefinition] = {}
    for name, base in BUILTIN_PROCESS_DEFINITIONS.items():
        definitions[name] = ProcessDefinition.from_mapping(
            name,
            overrides.get(name, {}),
            base=base,
        )
    for name, value in overrides.items():
        if name not in definitions:
            definitions[name] = ProcessDefinition.from_mapping(name, value)
    return definitions


def register_process_definitions(
    registry: ProcessHandlerRegistry,
    definitions: Mapping[str, ProcessDefinition],
    *,
    replace_existing: bool = False,
) -> ProcessHandlerRegistry:
    for definition in definitions.values():
        if definition.enabled:
            registry.register(
                ConfiguredProcessHandler(definition),
                replace_existing=replace_existing,
            )
    return registry
