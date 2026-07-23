"""Thin orchestration across perception, time, risk, and execution."""

from __future__ import annotations

from dataclasses import replace
import random
import time
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Tuple

from og_ego_prim.domain import (
    Action,
    ActionDecision,
    ActionRecord,
    StateChange,
)
from og_ego_prim.events import RuntimeEvent
from og_ego_prim.object_model import (
    LifecycleContext,
    LifecycleDirective,
    LifecycleTransition,
)
from og_ego_prim.prompting import PromptContext
from og_ego_prim.risk_predictor import RiskEvaluation
from og_ego_prim.task_planner.episode import PlannerEpisode, PlannerEpisodeEntry
from og_ego_prim.utils.planning import (
    normalize_planner_action,
    planner_entity_candidates,
)

from .components import RuntimeComponents
from .models import ActionOutcome, ActionReview


def _as_action(value: Any) -> Action:
    action = normalize_planner_action(value)
    if action is None:
        raise TypeError("candidate action must not be None")
    return action


class AgentRuntimeController:
    def __init__(
        self,
        components: RuntimeComponents,
        *,
        task_id: Optional[str] = None,
        task_view: Any = None,
        max_rethinking_attempts: int = 3,
        expose_cross_subtask_timers: bool = True,
        lifecycle_directive_handlers: Optional[
            Mapping[
                str,
                Callable[
                    [LifecycleDirective, LifecycleContext, LifecycleTransition],
                    None,
                ],
            ]
        ] = None,
    ) -> None:
        
        # 1. 核心依赖
        self.components = components
        self.event_sink = components.event_sink
        self.planner_episode = PlannerEpisode()

        # 2. 任务相关
        # 当前任务ID
        self.task_id = task_id
        # 整个任务的描述、目标、规则等
        self.task_view = task_view
        # 当前子任务对应的任务视图
        self.current_task_view = task_view
        # 当前正在执行的子任务 ID
        self.active_subtask_id: Optional[str] = None

        # 3. 配置属性
        # 动作被阻止后，最多允许重新规划多少次
        self.max_rethinking_attempts = max(int(max_rethinking_attempts), 0)
        # 是否允许当前子任务看到其他子任务的计时器
        self.expose_cross_subtask_timers = bool(expose_cross_subtask_timers)

        # 4. 当前运行状态
        # 最近一次场景快照
        self.latest_scene: Any = None
        # 最近一次感知得到的 StateChange 集合
        self.latest_changes: Tuple[StateChange, ...] = ()
        # 当前场景中可见实体的 ID、别名和规范名称
        self.visible_entity_ids: Tuple[str, ...] = ()
        # 当前动作已经重新思考了多少次
        self.rethinking_attempts = 0
        # 规划器已经提出了多少个动作
        self.proposal_count = 0

        self._emit_planner_proposals = False
        self._scheduler_tick_in_progress = False
        # 最近一次动作的风险审查结果
        self.last_review: Optional[ActionReview] = None
        # 最近一次动作的执行结果
        self.last_outcome: Optional[ActionOutcome] = None
        # 最近一次风险评估耗时；不混入 RiskEvaluation 数据模型。
        self.last_risk_latency: Optional[float] = None
        # 生命周期指令处理器
        self.lifecycle_directive_handlers: Dict[
            str,
            Callable[
                [LifecycleDirective, LifecycleContext, LifecycleTransition],
                None,
            ],
        ] = {}
        for name, handler in (lifecycle_directive_handlers or {}).items():
            self.register_lifecycle_directive_handler(name, handler, replace=True)

    @property
    def step(self) -> int:
        clock = getattr(self.components.scheduler, "clock", None)
        return int(getattr(clock, "step", 0))

    def set_subtask(self, subtask_id: Any) -> None:
        self.active_subtask_id = None if subtask_id is None else str(subtask_id)
        risk_predictor = self.components.risk_predictor
        if risk_predictor is not None:
            set_active_subtask = getattr(risk_predictor, "set_active_subtask", None)
            if callable(set_active_subtask):
                try:
                    set_active_subtask(
                        None
                        if self.active_subtask_id is None
                        else int(self.active_subtask_id)
                    )
                except (TypeError, ValueError):
                    set_active_subtask(self.active_subtask_id)
        subtask_lookup = getattr(self.task_view, "subtask", None)
        if subtask_lookup is not None and self.active_subtask_id and self.active_subtask_id.isdigit():
            self.current_task_view = subtask_lookup(int(self.active_subtask_id)) or self.task_view
        else:
            self.current_task_view = self.task_view
        self.rethinking_attempts = 0
        self.last_review = None
        self.last_outcome = None
        self._emit("subtask_started")

    def observe(self, snapshot: Any) -> Tuple[StateChange, ...]:
        self.latest_scene = snapshot
        state_changes = getattr(self.components.perception, "state_changes", None)
        if not callable(state_changes):
            raise TypeError(
                "PerceptionProvider must implement state_changes(snapshot, subtask_id=...)"
            )
        changes = state_changes(snapshot, subtask_id=self.active_subtask_id)
        self.latest_changes = tuple(changes)
        self.components.objects.update_from_scene_graph(snapshot)
        self.visible_entity_ids = self._visible_scene_entity_ids(snapshot)
        for change in self.latest_changes:
            self.components.objects.apply_state_change(change)
        self._emit(
            "scene_state_changed",
            entity_ids=(change.entity_id for change in self.latest_changes),
            details={"changes": [change.to_dict() for change in self.latest_changes]},
        )
        return self.latest_changes

    def _visible_scene_entity_ids(self, snapshot: Any) -> Tuple[str, ...]:
        payload = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not isinstance(payload, Mapping):
            return ()

        nodes = []
        for room in payload.get("rooms", ()) or ():
            if not isinstance(room, Mapping):
                continue
            nodes.extend(room.get("nodes", ()) or ())
            for group in room.get("groups", ()) or ():
                if isinstance(group, Mapping):
                    nodes.extend(group.get("nodes", ()) or ())
        nodes.extend(payload.get("nodes", ()) or ())

        visible = []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            if not bool(node.get("is_vis", node.get("visible", True))):
                continue
            identities = (
                node.get("id"),
                node.get("object_id"),
                node.get("label"),
                node.get("name"),
            )
            for identity in identities:
                if identity is None:
                    continue
                visible.append(str(identity))
                record = self.components.objects.get(identity)
                if record is not None:
                    visible.append(record.entity_id)
        return tuple(dict.fromkeys(visible))

    def tick_scheduler(self) -> Tuple[Any, ...]:
        if self._scheduler_tick_in_progress:
            return ()
        self._scheduler_tick_in_progress = True
        try:
            updates = tuple(
                self.components.scheduler.tick(
                    context={
                        "scene": self.latest_scene,
                        "objects": self.components.objects,
                        "executor": self.components.executor,
                        "task": self.current_task_view,
                        "active_subtask": self.active_subtask_id,
                    }
                )
            )
        finally:
            self._scheduler_tick_in_progress = False
        for update in updates:
            for entity_id in getattr(update, "entity_ids", ()):
                obj = self.components.objects.get(entity_id)
                for key, value in dict(getattr(update, "state_effects", {}) or {}).items():
                    old = None if obj is None else obj.states.get(key)
                    change = StateChange(
                        step=self.step,
                        subtask_id=self.active_subtask_id,
                        entity_id=entity_id,
                        room_id=None if obj is None else obj.room_id,
                        key=str(key),
                        old=old,
                        new=value,
                        source="scheduler_derived",
                    )
                    self.components.objects.apply_state_change(change)
            self._emit(
                "temporal_process_updated",
                entity_ids=getattr(update, "entity_ids", ()),
                details={"update": update.to_dict()},
            )
        return updates

    def _visible_timers(self) -> Tuple[Any, ...]:
        pending = tuple(self.components.scheduler.pending_for())
        if not self._timer_visibility_restricted:
            return pending
        return tuple(
            self.components.scheduler.filter_visibility(
                self.visible_entity_ids,
                processes=pending,
            )
        )

    @property
    def _timer_visibility_restricted(self) -> bool:
        return not self.expose_cross_subtask_timers

    def build_prompt_context(
        self,
        *,
        candidate_action: Optional[Action] = None,
        rethinking_reason: Optional[str] = None,
    ) -> PromptContext:
        instruction = (
            getattr(self.current_task_view, "instruction", "")
            if self.current_task_view is not None
            else ""
        )
        # Only the planner-facing natural-language projection is allowed here.
        # Evaluator G_task/G_safe BDDL never lives on an AgentTaskView.
        task_goal = getattr(self.current_task_view, "goal_description", None)
        if task_goal is None:
            task_goal = getattr(self.task_view, "goal_description", "")
        allowed_actions = tuple(
            sorted((getattr(self.components.executor, "valid_primitives", {}) or {}).keys())
        )
        return PromptContext(
            task_instruction=instruction,
            pending_timers=tuple(
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in self._visible_timers()
            ),
            candidate_action=candidate_action,
            allowed_actions=allowed_actions,
            rethinking_reason=rethinking_reason,
            section_data={
                "goal_description": task_goal,
                "task_rules": tuple(getattr(self.task_view, "wash_rules", ()) or ()),
            },
        )

    def bind_planner(self, planner: Any, *, emit_proposals: bool = False) -> Any:
        if (
            planner is None
            or not callable(getattr(planner, "propose", None))
            or not hasattr(planner, "supports_rethinking")
        ):
            raise TypeError(
                "planner must implement propose(context) and supports_rethinking"
            )
        self.components.planner = planner
        self._emit_planner_proposals = bool(emit_proposals)
        return planner

    def propose(self) -> Optional[Action]:
        planner = self.components.planner
        if planner is None:
            raise RuntimeError("no PlannerAdapter is bound to the runtime Controller")
        blocked_review = (
            self.last_review
            if self.last_outcome is not None
            and not self.last_outcome.executed
            and self.last_review is not None
            and self.last_review.should_rethink
            else None
        )
        context = self.build_prompt_context(
            candidate_action=None if blocked_review is None else blocked_review.action,
            rethinking_reason=(
                None
                if blocked_review is None
                else blocked_review.reason or "The candidate action was blocked."
            ),
        )
        proposed = planner.propose(context)
        if proposed is None:
            return None
        action = _as_action(proposed)
        self.proposal_count += 1
        if self._emit_planner_proposals:
            self._emit(
                "plan_proposed",
                entity_ids=action.entity_ids,
                details={
                    "step": self.proposal_count,
                    "plan": {
                        "action": action.to_legacy_plan(),
                        "caution": action.extensions.get("caution"),
                    },
                    "history_text": (
                        f"{self.proposal_count}. "
                        f"{action.to_legacy_plan(lowercase=False)}"
                    ),
                },
            )
        return action

    def iter_actions(self) -> Iterator[Action]:
        while True:
            action = self.propose()
            if action is None:
                return
            yield action
            if action.name == "DONE":
                return

    def ground_action(self, value: Any) -> Action:
        """Bind generic planner arguments to exact task entities."""

        action = _as_action(value)
        allowed = tuple(getattr(self.task_view, "object_ids", ()) or ())
        arguments = tuple(action.arguments)
        if not arguments or not allowed:
            return action

        last_navigation = None
        outcome = self.last_outcome
        if outcome is not None and outcome.executed and outcome.succeeded:
            previous = outcome.review.action
            if previous.name == "NAVIGATE_TO" and previous.object_id is not None:
                last_navigation = previous.object_id

        resolved = []
        for argument in arguments:
            candidates = planner_entity_candidates(argument, allowed)
            if not candidates:
                resolved.append(argument)
            elif len(candidates) == 1:
                resolved.append(candidates[0])
            elif (
                action.name != "NAVIGATE_TO"
                and last_navigation is not None
                and last_navigation in candidates
            ):
                resolved.append(last_navigation)
            else:
                resolved.append(random.SystemRandom().choice(candidates))

        if tuple(resolved) == arguments:
            return action
        parameters = dict(action.parameters)
        parameters["arguments"] = resolved
        if action.extensions.get("implicit_held_object"):
            return replace(action, target_id=resolved[0], parameters=parameters)
        return replace(
            action,
            object_id=resolved[0] if resolved else None,
            target_id=resolved[1] if len(resolved) > 1 else None,
            parameters=parameters,
        )

    def review_action(self, value: Any) -> ActionReview:
        action = self.ground_action(value)
        self.tick_scheduler()
        visible_timers = self._visible_timers()
        temporal_gate = self.components.scheduler.check_action(
            action,
            context={
                "objects": self.components.objects,
                "scene": self.latest_scene,
                "executor": self.components.executor,
            },
        )
        gate_payload = temporal_gate.to_dict()
        gate_reasons = list(getattr(temporal_gate, "reasons", ()) or ())
        if self._timer_visibility_restricted and not temporal_gate.allowed:
            visible_process_ids = {
                str(process.process_id)
                for process in visible_timers
                if getattr(process, "process_id", None) is not None
            }
            blocking_ids = list(
                getattr(temporal_gate, "blocking_process_ids", ()) or ()
            )
            visible_pairs = [
                (process_id, reason)
                for process_id, reason in zip(blocking_ids, gate_reasons)
                if str(process_id) in visible_process_ids
            ]
            hidden_count = len(blocking_ids) - len(visible_pairs)
            gate_reasons = [reason for _, reason in visible_pairs]
            if hidden_count:
                gate_reasons.append("temporal precondition not yet satisfied")
            gate_payload["blocking_process_ids"] = [
                process_id for process_id, _ in visible_pairs
            ]
            gate_payload["reasons"] = list(gate_reasons)
            gate_payload["retry_at_step"] = None
            gate_payload.setdefault("extensions", {})["hidden_process_count"] = hidden_count
        self._emit(
            "scheduler_gate_evaluated",
            entity_ids=action.entity_ids,
            details={
                "action": action.to_legacy_plan(),
                "gate": gate_payload,
                "visible_timers": [
                    process.to_dict() if hasattr(process, "to_dict") else process
                    for process in visible_timers
                ],
            },
        )
        risk_started_at = time.perf_counter()
        risk_predictor = self.components.risk_predictor
        if risk_predictor is None:
            risk_evaluation = RiskEvaluation(
                decision=ActionDecision.ALLOW,
                action=action,
            )
        else:
            risk_context = {
                "scene": self.latest_scene,
                "objects": self.components.objects,
                "scheduler": self.components.scheduler,
                "task": self.current_task_view,
                "active_subtask": self.active_subtask_id,
            }
            predict = getattr(risk_predictor, "predict", None)
            if callable(predict):
                risk_evaluation = predict(action, risk_context)
            else:
                evaluate = getattr(risk_predictor, "evaluate", None)
                if not callable(evaluate):
                    raise TypeError(
                        "runtime risk predictor must implement predict(action, context) "
                        "or evaluate(action, context)"
                    )
                risk_evaluation = evaluate(action, risk_context)
            if not isinstance(risk_evaluation, RiskEvaluation):
                if not isinstance(risk_evaluation, Mapping):
                    raise TypeError(
                        "runtime risk predictor must return RiskEvaluation or a mapping"
                    )
                risk_evaluation = RiskEvaluation(**dict(risk_evaluation))
        risk_latency = max(time.perf_counter() - risk_started_at, 0.0)
        self.last_risk_latency = risk_latency
        risk_payload = risk_evaluation.to_dict()
        self._emit(
            "risk_evaluated",
            entity_ids=action.entity_ids,
            details={
                "action": action.to_legacy_plan(),
                "evaluation": risk_payload,
                "latency_seconds": risk_latency,
            },
        )

        risk_reasons = [
            caution.text
            for caution in (
                risk_evaluation.specific_cautions
                + risk_evaluation.common_cautions
            )
        ]
        if risk_evaluation.rethinking_reason:
            risk_reasons.insert(0, risk_evaluation.rethinking_reason)
        reasons = list(gate_reasons)
        if risk_evaluation.decision != ActionDecision.ALLOW:
            reasons.extend(risk_reasons)
        reasons = list(dict.fromkeys(reason for reason in reasons if reason))
        if not temporal_gate.allowed or risk_evaluation.decision == ActionDecision.BLOCK:
            decision = ActionDecision.BLOCK
        elif risk_evaluation.decision == ActionDecision.CAUTION:
            decision = ActionDecision.CAUTION
        else:
            decision = ActionDecision.ALLOW
        reason = "; ".join(reasons) or None
        planner = self.components.planner
        planner_supports_rethinking = (
            True if planner is None else bool(planner.supports_rethinking)
        )
        should_rethink = (
            decision == ActionDecision.BLOCK
            and planner_supports_rethinking
            and self.rethinking_attempts < self.max_rethinking_attempts
        )
        if decision == ActionDecision.BLOCK:
            self.rethinking_attempts += 1

        entry = PlannerEpisodeEntry(
            action=action,
            decision=decision,
            step=self.step,
            attempt=self.rethinking_attempts,
            reason=reason,
            extensions={
                "scheduler_gate": gate_payload,
                "risk_evaluation": risk_payload,
            },
        )
        self.planner_episode.append(entry)
        if should_rethink:
            self.planner_episode.append(
                PlannerEpisodeEntry(
                    action=action,
                    decision=ActionDecision.RETHINKING,
                    step=self.step,
                    attempt=self.rethinking_attempts,
                    reason=reason,
                )
            )
        review = ActionReview(
            action=action,
            decision=decision,
            temporal_gate=temporal_gate,
            risk_evaluation=risk_evaluation,
            should_rethink=should_rethink,
            reason=reason,
            extensions={
                "scheduler_gate": gate_payload,
                "risk_evaluation": risk_payload,
            },
        )
        self.last_review = review
        return review

    def record_execution(
        self,
        review: ActionReview,
        *,
        succeeded: bool,
        room_id: Optional[str] = None,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> ActionOutcome:
        record_manipulation = bool(
            review.action.extensions.get("record_manipulation", True)
        )
        record = ActionRecord(
            action=review.action,
            step=self.step,
            task_id=self.task_id,
            subtask_id=self.active_subtask_id,
            room_id=room_id,
            succeeded=bool(succeeded),
            source="executor",
            extensions={
                "diagnostics": dict(diagnostics or {}),
                "record_manipulation": record_manipulation,
            },
        )
        if succeeded:
            if record_manipulation:
                self.components.objects.record_action(
                    record,
                    directive_sink=self._apply_lifecycle_directive,
                )
            note_manipulation = getattr(
                self.components.perception,
                "note_manipulation_event",
                None,
            )
            if note_manipulation is not None and (
                record_manipulation or review.action.name == "NAVIGATE_TO"
            ):
                note_manipulation(
                    {
                        "raw_plan": review.action.to_legacy_plan(),
                        "primitive": review.action.name,
                        "moved_object": review.action.object_id,
                        "target_object": review.action.target_id,
                        "global_step_index": self.step,
                        "source": "AgentRuntimeController.action_executed",
                    }
                )
            started_processes = self.components.scheduler.start_from_event(
                record,
                context={
                    "objects": self.components.objects,
                    "scene": self.latest_scene,
                    "executor": self.components.executor,
                    "task": self.current_task_view,
                    "active_subtask": self.active_subtask_id,
                },
            )
            for process in started_processes:
                self._emit(
                    "temporal_process_started",
                    entity_ids=getattr(process, "entity_ids", ()),
                    details={
                        "action": review.action.to_legacy_plan(),
                        "process": (
                            process.to_dict() if hasattr(process, "to_dict") else process
                        ),
                    },
                )
            # The action may itself have advanced enough simulator frames to
            # complete a timer (notably WAIT / WAIT_FOR_*). Poll before the
            # evaluator observes the post-action state.
            self.tick_scheduler()
            self.rethinking_attempts = 0
        event_type = "action_executed" if succeeded else "action_failed"
        self.event_sink.emit(
            RuntimeEvent.from_action_record(
                record,
                event_type=event_type,
                details={"diagnostics": dict(diagnostics or {})},
            )
        )
        outcome = ActionOutcome(
            review=review,
            executed=True,
            succeeded=bool(succeeded),
            action_record=record,
            reason=None if succeeded else "execution_failed",
        )
        self.last_outcome = outcome
        return outcome

    def register_lifecycle_directive_handler(
        self,
        directive_type: str,
        handler: Callable[
            [LifecycleDirective, LifecycleContext, LifecycleTransition],
            None,
        ],
        *,
        replace: bool = False,
    ) -> None:
        key = str(directive_type or "").strip().lower()
        if not key:
            raise ValueError("lifecycle directive type must not be empty")
        if not callable(handler):
            raise TypeError("lifecycle directive handler must be callable")
        if key in self.lifecycle_directive_handlers and not replace:
            raise ValueError(f"lifecycle directive handler {key!r} is already registered")
        self.lifecycle_directive_handlers[key] = handler

    def _apply_lifecycle_directive(
        self,
        directive: LifecycleDirective,
        context: LifecycleContext,
        transition: LifecycleTransition,
    ) -> None:
        handler = self.lifecycle_directive_handlers.get(directive.directive_type)
        if handler is None:
            raise KeyError(
                f"no lifecycle directive handler registered for {directive.directive_type!r}"
            )
        handler(directive, context, transition)

    def record_blocked(self, review: ActionReview) -> ActionOutcome:
        blocked_by_scheduler = bool(
            review.temporal_gate is not None
            and not getattr(review.temporal_gate, "allowed", True)
        )
        blocked_by_risk = bool(
            review.risk_evaluation is not None
            and review.risk_evaluation.decision == ActionDecision.BLOCK
        )
        if blocked_by_scheduler and blocked_by_risk:
            reason = "blocked_by_scheduler_and_risk"
        elif blocked_by_scheduler:
            reason = "blocked_by_scheduler"
        elif blocked_by_risk:
            reason = "blocked_by_risk"
        else:
            reason = "blocked"
        outcome = ActionOutcome(
            review=review,
            executed=False,
            succeeded=False,
            reason=reason,
        )
        self.last_outcome = outcome
        self._emit(
            "action_blocked",
            entity_ids=review.action.entity_ids,
            details={
                "action": review.action.to_legacy_plan(),
                "reason": outcome.reason,
                "review": review.to_dict(),
            },
        )
        return outcome

    def rethinking_prompt(self) -> str:
        if self.last_review is None:
            return ""
        context = self.build_prompt_context(
            candidate_action=self.last_review.action,
            rethinking_reason=self.last_review.reason or "The candidate action was blocked.",
        )
        return self.components.prompt_builder.build_rethinking(context)

    def planning_prompt(self) -> str:
        return self.components.prompt_builder.build_planning(self.build_prompt_context())

    def _emit(
        self,
        event_type: str,
        *,
        entity_ids: Iterable[str] = (),
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.event_sink.emit(
            RuntimeEvent(
                event_type=event_type,
                step=self.step,
                task_id=self.task_id,
                subtask_id=self.active_subtask_id,
                entity_ids=tuple(dict.fromkeys(str(value) for value in entity_ids)),
                details=dict(details or {}),
            )
        )


__all__ = ["AgentRuntimeController"]
