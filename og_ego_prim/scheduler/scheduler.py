from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Tuple

from .clock import ManualSimulationClock, SimulationClock
from .handlers import (
    ConfiguredProcessHandler,
    HandlerContext,
    NullTemporalStateAdapter,
    ProcessDefinition,
    ProcessHandlerRegistry,
    TemporalStateAdapter,
    builtin_process_definitions,
    register_process_definitions,
)
from .models import (
    ProcessStatus,
    ProcessUpdate,
    ScheduledProcess,
    TemporalEvent,
    TemporalGate,
)
from .utils import _to_bool, normalize_action_name


@dataclass
class SchedulerConfig:
    enabled: bool = True
    include_builtins: bool = True
    process_definitions: Dict[str, ProcessDefinition] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "SchedulerConfig":
        source = dict(value or {})
        if isinstance(source.get("scheduler"), Mapping):
            source = dict(source["scheduler"])
        raw_definitions = source.get("processes", source.get("handlers", {})) or {}
        if not isinstance(raw_definitions, Mapping):
            raise TypeError("scheduler processes must be a mapping")
        include_builtins = _to_bool(source.get("include_builtins"), True)
        definitions = (
            builtin_process_definitions(raw_definitions)
            if include_builtins
            else {
                str(name): ProcessDefinition.from_mapping(str(name), config)
                for name, config in raw_definitions.items()
            }
        )
        return cls(
            enabled=_to_bool(source.get("enabled"), True),
            include_builtins=include_builtins,
            process_definitions=definitions,
            extensions=dict(source.get("extensions") or {}),
        )

    @classmethod
    def defaults(cls) -> "SchedulerConfig":
        return cls(process_definitions=builtin_process_definitions())


class ProcessVisibilityFilter(Protocol):
    def __call__(
        self,
        process: ScheduledProcess,
        visible_entity_ids: frozenset[str],
        context: HandlerContext,
    ) -> bool:
        ...


def entity_visibility_filter(
    process: ScheduledProcess,
    visible_entity_ids: frozenset[str],
    context: HandlerContext,
) -> bool:
    if not process.entity_ids:
        return bool(process.extensions.get("visible_when_global", False))
    return bool(set(process.entity_ids) & visible_entity_ids)


class Scheduler:
    """Owns pending temporal processes and no completed-process history."""

    def __init__(
        self,
        *,
        clock: SimulationClock,
        registry: Optional[ProcessHandlerRegistry] = None,
        state_adapter: Optional[TemporalStateAdapter] = None,
        enabled: bool = True,
        extensions: Optional[Mapping[str, Any]] = None,
    ):
        self.clock = clock
        self.registry = registry if registry is not None else ProcessHandlerRegistry()
        self.state_adapter = (
            state_adapter if state_adapter is not None else NullTemporalStateAdapter()
        )
        self.enabled = bool(enabled)
        self.extensions = dict(extensions or {})
        self._pending: Dict[str, ScheduledProcess] = {}

    def _context(self, data: Optional[Mapping[str, Any]] = None) -> HandlerContext:
        return HandlerContext(
            clock=self.clock,
            state_adapter=self.state_adapter,
            data=dict(data or {}),
        )

    def start_from_event(
        self,
        event: Any,
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[ScheduledProcess, ...]:
        if not self.enabled:
            return ()
        temporal_event = (
            event
            if isinstance(event, TemporalEvent)
            else TemporalEvent.from_action(event, step=self.clock.step)
        )
        if not temporal_event.success:
            return ()

        handler_context = self._context(context)
        self._cancel_from_event(temporal_event)
        started = []
        for handler in self.registry:
            process = handler.start(temporal_event, handler_context)
            if process is None:
                continue
            self._cancel_conflicting_processes(process)
            existing = self._pending.get(process.process_id)
            if existing is not None and not self._should_restart(existing, process):
                # Repeated WAIT actions advance the same timer. They must not
                # move its start forward and make a long configured duration
                # impossible to satisfy.
                continue
            self._pending[process.process_id] = process
            started.append(process)
        return tuple(started)

    def _cancel_from_event(self, event: TemporalEvent) -> None:
        event_entities = set(event.entity_ids)
        for process_id, process in tuple(self._pending.items()):
            raw_cancel_actions = process.extensions.get("cancel_actions", ())
            if isinstance(raw_cancel_actions, str):
                raw_cancel_actions = (raw_cancel_actions,)
            cancel_actions = {
                normalize_action_name(value)
                for value in raw_cancel_actions
            }
            if event.action_name not in cancel_actions:
                continue
            scope = str(process.extensions.get("cancel_scope", "entity")).lower()
            raw_process_entities = process.extensions.get(
                "gate_entity_ids",
                process.entity_ids,
            )
            if isinstance(raw_process_entities, str):
                raw_process_entities = (raw_process_entities,)
            process_entities = set(raw_process_entities)
            if scope != "global" and (
                not event_entities or not event_entities.intersection(process_entities)
            ):
                continue
            self._pending.pop(process_id, None)

    def _cancel_conflicting_processes(self, process: ScheduledProcess) -> None:
        raw_cancelled_types = process.extensions.get("cancels", ())
        if isinstance(raw_cancelled_types, str):
            raw_cancelled_types = (raw_cancelled_types,)
        cancelled_types = {
            str(value).strip().lower()
            for value in raw_cancelled_types
            if str(value).strip()
        }
        if not cancelled_types:
            return
        process_entities = set(process.entity_ids)
        for process_id, pending in tuple(self._pending.items()):
            if pending.process_type not in cancelled_types:
                continue
            if process_entities and not process_entities.intersection(pending.entity_ids):
                continue
            self._pending.pop(process_id, None)

    @staticmethod
    def _should_restart(
        existing: ScheduledProcess,
        replacement: ScheduledProcess,
    ) -> bool:
        raw_restart_actions = replacement.extensions.get("restart_actions", ())
        if isinstance(raw_restart_actions, str):
            raw_restart_actions = (raw_restart_actions,)
        restart_actions = {
            normalize_action_name(value)
            for value in raw_restart_actions
        }
        return bool(
            replacement.extensions.get("restart_on_trigger", False)
            or replacement.extensions.get("trigger_action") in restart_actions
            or replacement.start_step < existing.start_step
        )

    def tick(
        self,
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[ProcessUpdate, ...]:
        if not self.enabled:
            return ()
        handler_context = self._context(context)
        updates = []
        for process in tuple(self._pending.values()):
            handler = self.registry.get(process.process_type)
            if handler is None:
                updates.append(
                    ProcessUpdate(
                        process_id=process.process_id,
                        process_type=process.process_type,
                        status=ProcessStatus.PENDING,
                        step=handler_context.step,
                        entity_ids=process.entity_ids,
                        reason="handler_unavailable",
                    )
                )
                continue
            update = handler.poll(process, handler_context)
            updates.append(update)
            if update.terminal:
                self._pending.pop(process.process_id, None)
        return tuple(updates)

    def check_action(
        self,
        action: Any,
        *,
        context: Optional[Mapping[str, Any]] = None,
        processes: Optional[Iterable[ScheduledProcess]] = None,
    ) -> TemporalGate:
        event = (
            action
            if isinstance(action, TemporalEvent)
            else TemporalEvent.from_action(action, step=self.clock.step)
        )
        if not self.enabled:
            return TemporalGate(
                action_name=event.action_name,
                decision="ALLOW",
                step=self.clock.step,
                extensions={"scheduler_enabled": False},
            )
        action_entities = set(event.entity_ids)
        blocked = []
        reasons = []
        retry_steps = []

        candidates = self._pending.values() if processes is None else tuple(processes)
        for process in candidates:
            if not self._blocks_action(process, event.action_name):
                continue
            scope = str(process.extensions.get("blocking_scope", "entity")).lower()
            raw_gate_entity_ids = process.extensions.get(
                "gate_entity_ids",
                process.entity_ids,
            )
            if isinstance(raw_gate_entity_ids, str):
                raw_gate_entity_ids = (raw_gate_entity_ids,)
            gate_entity_ids = tuple(
                str(value)
                for value in raw_gate_entity_ids
            )
            if scope != "global" and action_entities and not action_entities.intersection(gate_entity_ids):
                continue
            blocked.append(process.process_id)
            reasons.append(
                f"{process.process_type} pending for {', '.join(process.entity_ids) or 'global scope'}"
            )
            if process.ready_step is not None:
                retry_steps.append(process.ready_step)

        return TemporalGate(
            action_name=event.action_name,
            decision="BLOCK" if blocked else "ALLOW",
            step=self.clock.step,
            blocking_process_ids=tuple(blocked),
            reasons=tuple(reasons),
            retry_at_step=max(retry_steps) if retry_steps else None,
            extensions={"scheduler_enabled": self.enabled},
        )

    @staticmethod
    def _blocks_action(process: ScheduledProcess, action_name: str) -> bool:
        action_name = normalize_action_name(action_name)
        return "*" in process.blocking_actions or action_name in process.blocking_actions

    def pending_for(
        self,
        entity_ids: Optional[str | Iterable[str]] = None,
        *,
        process_type: Optional[str] = None,
    ) -> Tuple[ScheduledProcess, ...]:
        if isinstance(entity_ids, str):
            requested = {entity_ids}
        else:
            requested = set(entity_ids or ())
        normalized_type = None if process_type is None else str(process_type).strip().lower()
        return tuple(
            process
            for process in self._pending.values()
            if (normalized_type is None or process.process_type == normalized_type)
            and (not requested or requested.intersection(process.entity_ids))
        )

    def filter_visibility(
        self,
        visible_entity_ids: Iterable[str],
        *,
        processes: Optional[Iterable[ScheduledProcess]] = None,
        visibility_filter: Optional[ProcessVisibilityFilter] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[ScheduledProcess, ...]:
        candidates = tuple(self._pending.values()) if processes is None else tuple(processes)
        visible = frozenset(str(entity_id) for entity_id in visible_entity_ids)
        predicate = visibility_filter or entity_visibility_filter
        handler_context = self._context(context)
        return tuple(
            process
            for process in candidates
            if predicate(process, visible, handler_context)
        )

    def cancel(self, process_id: str, *, reason: str = "cancelled") -> Optional[ProcessUpdate]:
        process = self._pending.pop(str(process_id), None)
        if process is None:
            return None
        return ProcessUpdate(
            process_id=process.process_id,
            process_type=process.process_type,
            status=ProcessStatus.CANCELLED,
            step=self.clock.step,
            entity_ids=process.entity_ids,
            reason=reason,
        )

    def clear(self) -> None:
        self._pending.clear()

    @property
    def pending(self) -> Tuple[ScheduledProcess, ...]:
        return tuple(self._pending.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "pending": [process.to_dict() for process in self._pending.values()],
            "extensions": dict(self.extensions),
        }

    def load_pending(
        self,
        values: Iterable[ScheduledProcess | Mapping[str, Any]],
        *,
        replace_existing: bool = True,
    ) -> None:
        if replace_existing:
            self._pending.clear()
        for value in values:
            process = value if isinstance(value, ScheduledProcess) else ScheduledProcess.from_dict(value)
            handler = self.registry.get(process.process_type)
            if process.status != ProcessStatus.PENDING or handler is None:
                continue
            definition = getattr(handler, "definition", None)
            if definition is not None:
                process.readiness_predicate = definition.readiness_predicate
                process.blocking_actions = definition.blocking_actions
                process.completion_effects = dict(definition.completion_effects)
            duration_steps = getattr(definition, "duration_steps", None)
            if duration_steps is not None:
                process.ready_step = process.start_step + int(duration_steps)
            self._pending[process.process_id] = process


def build_scheduler(
    config: Optional[SchedulerConfig | Mapping[str, Any]] = None,
    *,
    clock: Optional[SimulationClock] = None,
    state_adapter: Optional[TemporalStateAdapter] = None,
    registry: Optional[ProcessHandlerRegistry] = None,
) -> Scheduler:
    scheduler_config = (
        config
        if isinstance(config, SchedulerConfig)
        else SchedulerConfig.from_mapping(config) if config is not None else SchedulerConfig.defaults()
    )
    handler_registry = registry if registry is not None else ProcessHandlerRegistry()
    if registry is None:
        register_process_definitions(
            handler_registry,
            scheduler_config.process_definitions,
            replace_existing=True,
        )
    else:
        # Explicitly injected handlers take precedence over configured
        # defaults, while missing built-ins remain available.
        for process_type, definition in scheduler_config.process_definitions.items():
            if definition.enabled and handler_registry.get(process_type) is None:
                handler_registry.register(ConfiguredProcessHandler(definition))
    return Scheduler(
        clock=clock if clock is not None else ManualSimulationClock(),
        registry=handler_registry,
        state_adapter=state_adapter,
        enabled=scheduler_config.enabled,
        extensions=scheduler_config.extensions,
    )
