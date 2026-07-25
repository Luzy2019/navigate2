"""Replaceable planner adapters over typed actions."""

from __future__ import annotations

import json
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from og_ego_prim.domain import Action, ActionDecision, Registry
from og_ego_prim.primitives import get_valid_primitives
from og_ego_prim.prompting import PromptContext
from og_ego_prim.utils.planning import (
    normalize_planner_action,
    parse_model_json_object,
    planner_entity_candidates,
    planner_prompt_entity_ids,
    redact_bddl_instance_ids,
    validate_planner_action,
)
from og_ego_prim.utils.serialization import to_debug_builtin


_PREFLIGHT_PROMPT = """Request mode: TASK_RISK_PREFLIGHT.
Return MONITOR only when the task explicitly requires at least three distinct
movable objects to be placed at the same exact destination entity using the
same placement relation. Do not combine intermediate work surfaces, objects
assigned to different cabinets, or unrelated sequential task steps. Return
{"status":"NONE"} or {"status":"MONITOR",
"ordered_objects":["entity"],"destination_role":"role",
"destination_relation":"PLACE_INSIDE"}. Order objects by loading safety."""

_LOADING_PROMPT = """Generate all remaining operations in the complete
shared-destination loading plan from the current RGB and held_object. Choose
one allowed destination and use it for every placement. Include required
operation preparation, but never include NAVIGATE_TO. Return {"status":"LOADING_PLAN",
"destination":"entity","steps":["atomic action"]}."""

_SAFETY_PROMPT = """Generate an operation-only safety plan for failed_action
within the current task_instruction. goal_description may contain later subtasks
and is not part of this plan. When loading is null, return the minimal plan: every
step before the final step must directly remove blocked_reason, and the final step
must be failed_action after that risk has been removed, or a safe same-role
alternative with the same action name and, for multiple arguments, the same first
argument. Do not append anything after that final operation. When loading is not
null, return the complete remaining loading sequence: first remove blocked_reason,
then execute failed_action or its safe same-role alternative, and then complete
every remaining pending placement in the supplied loading order. Do not replace a task-required
entity, skip a required state transition, change held-object contents, or perform
unrelated later-task operations. PLACE_INSIDE moves the held object itself;
POUR_INTO transfers its contents. The blocked action was not executed. Use the
current held_object, do not include NAVIGATE_TO, and make every step executable in
order. When held_object is not null, do not GRASP until a placement or release has
made the gripper empty. Do not add an operation that the final step immediately
undoes. RELEASE is invalid after placement because the gripper is already empty.
If operating another entity risks exposing the held object, place the held
object on a safe task-relevant surface before the final operation. When held_object
is null, do not start with PLACE_ON_TOP, PLACE_INSIDE,
POUR_INTO, DUMP_INTO, or RELEASE; first GRASP a safe object if the plan needs one
held. When blocked_reason says a previously heated object must cool and
pending_timers contains that object's cooling process, WAIT(the exact heated
object) is the required first mitigation step. In that case, the safety plan
must begin with exactly WAIT(the exact heated object), must not PLACE_ON_TOP,
PLACE_INSIDE, RELEASE, NAVIGATE_TO, or substitute a same-role alternative before
that WAIT, and must retry failed_action as its final step after WAIT. The pending
timer is evidence that the object has not cooled yet; placing it aside does not
remove the heat risk and is not an alternative mitigation. Use no WAIT_* action
unless a matching active process appears in pending_timers; WAIT_FOR_COOL is not
an available action.
The goal field must briefly name the blocked risk being removed, not copy the task
or goal_description. Return
{"status":"SAFETY_PLAN","goal":"goal","steps":["atomic action"]}."""

_EXECUTE_PROMPT = """Using only current RGB and held_object, prepare
the immutable intended_operation. Return exactly the same operation with the same
arguments when executable. The only allowed deviation is NAVIGATE_TO(one argument
of intended_operation) when navigation is required. Never substitute another
operation, argument, or fallback surface. For unary held-object placement, pour,
or dump operations, the sole argument is the destination. Return one JSON object
with status ACTION and action set to that exact operation. Never return placeholder
text such as "atomic action"."""


@runtime_checkable
class PlannerAdapter(Protocol):
    supports_rethinking: bool

    def propose(self, context: PromptContext) -> Optional[Action]:
        ...


class CallablePlannerAdapter:
    def __init__(
        self,
        callback: Callable[[PromptContext], Any],
        *,
        supports_rethinking: bool = True,
    ) -> None:
        self.callback = callback
        self.supports_rethinking = bool(supports_rethinking)

    def propose(self, context: PromptContext) -> Optional[Action]:
        return normalize_planner_action(self.callback(context))


class IteratorPlannerAdapter:
    """Adapter for the existing Expert Planning generator."""
    supports_rethinking = False

    def __init__(self, plans: Iterable[Any]) -> None:
        self._plans: Iterator[Any] = iter(plans)

    def propose(self, context: PromptContext) -> Optional[Action]:
        del context
        try:
            return normalize_planner_action(next(self._plans))
        except StopIteration:
            return None


class AgentPlannerAdapter:
    """Adapter for the existing GPT/local AgentPlanner generator."""

    supports_rethinking = True

    def __init__(
        self,
        agent: Any,
        *,
        use_obs: bool = True,
        max_step: Optional[int] = None,
    ) -> None:
        self.agent = agent
        self.use_obs = bool(use_obs)
        self.max_step = max_step
        self._iterator: Optional[Iterator[Any]] = None

    def propose(self, context: PromptContext) -> Optional[Action]:
        del context
        if self._iterator is None:
            self._iterator = iter(self.agent.step(use_obs=self.use_obs, max_step=self.max_step))
        try:
            return normalize_planner_action(next(self._iterator))
        except StopIteration:
            self._iterator = None
            return None


class VLMClosedLoopPlannerAdapter:
    """Complete safety replanning over the existing model planner."""

    supports_rethinking = True

    def __init__(
        self,
        agent: Any,
        *,
        use_obs: bool = True,
        max_step: Optional[int] = None,
        held_object_getter: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.agent = agent
        self.base = AgentPlannerAdapter(agent, use_obs=use_obs, max_step=max_step)
        self.use_obs = bool(use_obs)
        self.max_step = max_step
        self.held_object_getter = held_object_getter or (lambda: None)
        self.agent.held_object_getter = self.held_object_getter
        self.valid_primitives = dict(get_valid_primitives(agent.primitive_type))
        self.allowed_entity_ids = tuple(getattr(agent, "allowed_entity_ids", ()))
        self._start_step = int(agent.current_step)
        self._preflight_done = False
        self._loading: Optional[Dict[str, Any]] = None
        self._root_action: Optional[Action] = None
        self._safety_goal: Optional[str] = None
        self._steps: list[Action] = []
        self._inflight: Optional[Dict[str, Any]] = None
        self.last_safety_plan_raw_output: Optional[str] = None
        self.last_safety_plan_payload: Optional[Dict[str, Any]] = None

    def _held_object(self) -> Optional[str]:
        value = self.held_object_getter()
        return None if value is None else str(value)

    def _request(
        self,
        context: PromptContext,
        instruction: str,
        **extra: Any,
    ) -> tuple[Dict[str, Any], str]:
        task_instruction = context.task_instruction or self.agent.task_instruction
        payload = {
            "task_instruction": task_instruction,
            "goal_description": context.section_data.get("goal_description"),
            "allowed_entities": list(
                planner_prompt_entity_ids(self.allowed_entity_ids, task_instruction)
            ),
            "available_actions": self.valid_primitives,
            "held_object": self._held_object(),
            "scene_graph": context.current_scene,
            "object_views": context.object_views,
            "pending_timers": context.pending_timers,
            **extra,
        }
        prompt = (
            f"{instruction.strip()}\n\n"
            "Use only the supplied action vocabulary and exact entity identifiers. "
            "A category name is invalid when it represents multiple task entities. "
            "Each available_actions value is the exact required argument count. "
            "When a placement, pour, or dump action has arity 1, its only argument is "
            "the destination and the source object is current held_object. "
            "Every action string must use canonical NAME(arg1, arg2) syntax, "
            "including empty parentheses for zero-argument actions. "
            "Return one strict JSON object without markdown.\n\n"
            f"INPUT:\n{json.dumps(to_debug_builtin(payload), ensure_ascii=False, sort_keys=True)}"
        )
        _, observations = self.agent._get_last_execution_info(self.use_obs)
        output = self.agent.client.model(prompt, image_file=observations)
        return parse_model_json_object(output), output

    def _action(self, value: Any, *, operation_only: bool = False) -> Action:
        return validate_planner_action(
            value,
            self.valid_primitives,
            allowed_entity_ids=self.allowed_entity_ids,
            forbidden_actions=("NAVIGATE_TO", "DONE") if operation_only else (),
        )

    def _plan_steps(self, payload: Mapping[str, Any]) -> list[Action]:
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("safety plan must contain a non-empty steps array")
        return [self._action(step, operation_only=True) for step in raw_steps]

    def _run_preflight(self, context: PromptContext) -> None:
        self._preflight_done = True
        payload, _ = self._request(
            context,
            _PREFLIGHT_PROMPT,
        )
        status = str(payload.get("status", "")).strip().upper()
        if status == "NONE":
            return
        if status != "MONITOR":
            raise ValueError("TASK_RISK_PREFLIGHT returned an unknown status")

        ordered = [str(value).strip() for value in payload.get("ordered_objects", ())]
        if len(ordered) < 3:
            return
        if len(set(ordered)) != len(ordered) or any(
            entity_id not in self.allowed_entity_ids for entity_id in ordered
        ):
            raise ValueError("preflight ordered_objects must be unique allowed entities")
        relation = str(payload.get("destination_relation", "")).strip().upper()
        if relation not in {"PLACE_INSIDE", "PLACE_ON_TOP"} or relation not in self.valid_primitives:
            raise ValueError("preflight destination_relation must be a current placement action")
        role = str(payload.get("destination_role", "")).strip()
        self._loading = {
            "order": ordered,
            "pending": list(ordered),
            "role": role,
            "relation": relation,
            "destination": None,
        }

    def _starts_loading(self, action: Action) -> bool:
        if self._loading is None:
            return False
        if action.name == "GRASP":
            operated_object = action.object_id
        elif action.name in {"PLACE_INSIDE", "PLACE_ON_TOP"}:
            operated_object, _ = self._loading_placement(action, self._held_object())
        else:
            return False
        candidates = planner_entity_candidates(
            operated_object,
            self.allowed_entity_ids,
        )
        return any(candidate in self._loading["pending"] for candidate in candidates)

    def _start_loading_plan(self, context: PromptContext) -> None:
        loading = self._loading
        loading_plan, _ = self._request(
            context,
            _LOADING_PROMPT,
            ordered_objects=loading["order"],
            pending_objects=loading["pending"],
            destination_role=loading["role"],
            destination_relation=loading["relation"],
        )
        if str(loading_plan.get("status", "")).strip().upper() != "LOADING_PLAN":
            raise ValueError("loading planner must return LOADING_PLAN")
        destination = str(loading_plan.get("destination", "")).strip()
        loading["destination"] = destination
        self._safety_goal = (
            f"load {', '.join(loading['order'])} into or onto {destination}"
        )
        self._steps = self._plan_steps(loading_plan)
        self._validate_loading_steps(
            self._steps,
            self._held_object(),
            list(loading["pending"]),
        )

    def _loading_placement(
        self,
        action: Action,
        held_object: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if self.valid_primitives[action.name] == 1:
            return held_object, action.object_id
        return action.object_id, action.target_id

    def _validate_loading_steps(
        self,
        steps: Iterable[Action],
        held_object: Optional[str],
        pending: list[str],
    ) -> None:
        loading = self._loading
        monitored = set(pending)
        for action in steps:
            if action.name == "GRASP":
                if held_object is not None:
                    raise ValueError("loading plan cannot grasp while holding an object")
                if action.object_id in monitored and (
                    not pending or action.object_id != pending[0]
                ):
                    raise ValueError("loading grasps must follow the ordered objects")
                held_object = action.object_id
            elif action.name in {"PLACE_INSIDE", "PLACE_ON_TOP"}:
                placed, target = self._loading_placement(action, held_object)
                if placed is None or placed != held_object:
                    raise ValueError("loading placement must use the held object")
                if action.name == loading["relation"] and placed in monitored:
                    if (
                        not pending
                        or placed != pending[0]
                        or target != loading["destination"]
                    ):
                        raise ValueError(
                            "loading placements must follow the shared ordered destination"
                        )
                    pending.pop(0)
                held_object = None
            elif action.name == "RELEASE":
                held_object = None
        if pending:
            raise ValueError("loading plan must place every pending object")

    def _safety_plan(
        self,
        context: PromptContext,
        *,
        failed_action: Action,
    ) -> list[Action]:
        payload, raw_output = self._request(
            context,
            _SAFETY_PROMPT,
            original_blocked_action=(
                None if self._root_action is None else self._root_action.to_legacy_plan()
            ),
            safety_goal=self._safety_goal,
            failed_action=failed_action.to_legacy_plan(),
            blocked_reason=context.rethinking_reason,
            remaining_steps=[action.to_legacy_plan() for action in self._steps],
            loading=self._loading,
        )
        self.last_safety_plan_raw_output = raw_output
        self.last_safety_plan_payload = dict(payload)
        if str(payload.get("status", "")).strip().upper() != "SAFETY_PLAN":
            raise ValueError("safety planner must return SAFETY_PLAN")
        if self._safety_goal is None:
            goal = str(payload.get("goal", "")).strip()
            if not goal:
                raise ValueError("the first safety plan must define a non-empty goal")
            self._safety_goal = goal
        steps = self._plan_steps(payload)
        if self._loading is not None and self._loading["destination"] is not None:
            self._validate_loading_steps(
                steps,
                self._held_object(),
                list(self._loading["pending"]),
            )
        return steps

    @staticmethod
    def _same_action(left: Action, right: Action) -> bool:
        return (
            left.name,
            left.object_id,
            left.target_id,
        ) == (
            right.name,
            right.object_id,
            right.target_id,
        )

    def _planner_action(self, action: Action) -> Action:
        """Recover the active primitive syntax from runtime-held-object expansion."""

        arity = self.valid_primitives.get(action.name)
        executor_arguments = action.parameters.get("executor_arguments")
        if executor_arguments is not None:
            arguments = ", ".join(str(value) for value in executor_arguments)
            return Action.from_raw(f"{action.name}({arguments})")
        if arity == 0:
            return Action.from_raw(f"{action.name}()")
        return action

    def _issue(
        self,
        action: Action,
        *,
        operation: bool,
        raw_output: str,
    ) -> Optional[Action]:
        if (
            self.max_step is not None
            and self.agent.current_step - self._start_step >= self.max_step
        ):
            self.agent.tracker.track_termination(
                reason="exceeding_max_steps",
                msg=f"exceeding max steps {self.max_step}",
            )
            return None
        marker = self.agent.runtime_controller.last_outcome
        plan = self.agent.record_plan(action, raw_output=raw_output)
        issued = normalize_planner_action(plan)
        self._inflight = {
            "operation": operation,
            "outcome_marker": marker,
            "held_object": self._held_object(),
        }
        return issued

    def _next_step(self, context: PromptContext) -> Optional[Action]:
        intended = self._steps[0]
        if (
            self.agent.primitive_type == "starter"
            and intended.name
            in {"PLACE_ON_TOP", "PLACE_INSIDE", "POUR_INTO", "DUMP_INTO"}
            and self._held_object() is None
        ):
            raise ValueError("starter placement requires a held object")
        payload, output = self._request(
            context,
            _EXECUTE_PROMPT,
            intended_operation=intended.to_legacy_plan(),
        )
        if str(payload.get("status", "")).strip().upper() != "ACTION":
            raise ValueError("operation preparation must return ACTION")
        action = self._action(payload.get("action"))
        if intended.name == "GRASP" and action.name == "NAVIGATE_TO":
            action = self._action(f"NAVIGATE_TO({intended.object_id})")
        previous = self.agent.runtime_controller.last_outcome
        if (
            action.name == "NAVIGATE_TO"
            and previous is not None
            and previous.executed
            and previous.succeeded
            and self._same_action(
                action,
                self._planner_action(previous.review.action),
            )
        ):
            action = intended
        operation = action.name != "NAVIGATE_TO"
        if operation and not self._same_action(action, intended):
            raise ValueError("operation preparation changed the intended operation")
        return self._issue(action, operation=operation, raw_output=output)

    @staticmethod
    def _risk_blocked(review: Any) -> bool:
        evaluation = getattr(review, "risk_evaluation", None)
        decision = getattr(evaluation, "decision", None)
        return decision == ActionDecision.BLOCK

    def _advance_loading(self, completed: Action, held_object: Optional[str]) -> None:
        if not self._loading["pending"]:
            return
        if completed.name != self._loading["relation"]:
            return
        if self.valid_primitives[completed.name] == 1:
            placed_object = held_object
            destination = completed.object_id
        else:
            placed_object = completed.object_id
            destination = completed.target_id
        if (
            placed_object == self._loading["pending"][0]
            and destination == self._loading["destination"]
        ):
            self._loading["pending"].pop(0)

    def _consume_inflight(self) -> Optional[str]:
        if self._inflight is None:
            return None
        outcome = self.agent.runtime_controller.last_outcome
        if outcome is None or outcome is self._inflight["outcome_marker"]:
            return "pending"

        inflight = self._inflight
        self._inflight = None
        if outcome.executed and outcome.succeeded:
            if inflight["operation"]:
                completed = self._steps.pop(0)
                loading_active = (
                    self._loading is not None
                    and self._loading["destination"] is not None
                )
                if loading_active:
                    self._advance_loading(completed, inflight["held_object"])
                if not self._steps:
                    if loading_active:
                        self._loading = None
                    self._root_action = None
                    self._safety_goal = None
            return "success"
        if not outcome.executed:
            return "risk" if self._risk_blocked(outcome.review) else "scheduler"
        return "failed"

    def _handle_risk_block(self, context: PromptContext, review: Any) -> None:
        failed = self._planner_action(review.action)
        if self._root_action is None:
            self._root_action = failed
        self._steps = self._safety_plan(context, failed_action=failed)

    def _delegate_rethinking(self, context: PromptContext, review: Any) -> Optional[Action]:
        self.agent.note_runtime_review(review)
        return self.base.propose(context)

    def propose(self, context: PromptContext) -> Optional[Action]:
        if not self._preflight_done:
            self._run_preflight(context)

        settled = self._consume_inflight()
        review = self.agent.runtime_controller.last_review
        if settled == "pending":
            return None
        if settled == "failed":
            self._steps = []
            self._root_action = None
            self._safety_goal = None
        if settled == "scheduler":
            if self._steps:
                self._handle_risk_block(context, review)
            else:
                return self._delegate_rethinking(context, review)
        elif settled == "risk":
            self._handle_risk_block(context, review)
        elif context.candidate_action is not None and review is not None:
            if self._risk_blocked(review):
                self._handle_risk_block(context, review)
            else:
                return self._delegate_rethinking(context, review)

        if self._steps:
            return self._next_step(context)
        candidate = self.base.propose(context)
        if candidate is None or not self._starts_loading(candidate):
            return candidate
        self.agent.tracker.mark_plan_runtime(
            candidate.to_legacy_plan(),
            executed=False,
            succeeded=False,
            blocked_reason="multi_object_loading_order",
        )
        self._root_action = candidate
        self._start_loading_plan(context)
        return self._next_step(context)


PlannerAdapterFactory = Callable[..., PlannerAdapter]
PLANNER_ADAPTERS: Registry[PlannerAdapterFactory] = Registry()
PLANNER_ADAPTERS.register("callable", CallablePlannerAdapter)

PLANNER_ADAPTERS.register("example", IteratorPlannerAdapter)
PLANNER_ADAPTERS.register("iterator", IteratorPlannerAdapter)
PLANNER_ADAPTERS.register("scripted", IteratorPlannerAdapter)

PLANNER_ADAPTERS.register("agent_planner", AgentPlannerAdapter)
PLANNER_ADAPTERS.register("model", AgentPlannerAdapter)
PLANNER_ADAPTERS.register("vlm_closed_loop", VLMClosedLoopPlannerAdapter)


def register_planner_adapter(
    name: str,
    factory: PlannerAdapterFactory,
    *,
    replace: bool = False,
) -> PlannerAdapterFactory:
    if not callable(factory):
        raise TypeError("planner adapter factory must be callable")
    return PLANNER_ADAPTERS.register(name, factory, replace=replace)


def create_planner_adapter(
    config: Any,
    *args: Any,
    registry: Registry[PlannerAdapterFactory] = PLANNER_ADAPTERS,
    **overrides: Any,
) -> PlannerAdapter:
    """Construct a planner adapter from a registered name or config mapping."""

    if isinstance(config, PlannerAdapter):
        if args or overrides:
            raise ValueError("cannot apply constructor arguments to an existing planner adapter")
        return config
    options: Dict[str, Any]
    if isinstance(config, str):
        name = config
        options = {}
    elif isinstance(config, Mapping):
        values = dict(config)
        name = values.pop("adapter", values.pop("type", values.pop("name", "iterator")))
        options = dict(values.pop("options", {}) or {})
        options.update(values)
    else:
        raise TypeError("planner adapter config must be a registered name, mapping, or adapter")
    options.update(overrides)
    adapter = registry.require(str(name))(*args, **options)
    if not isinstance(adapter, PlannerAdapter):
        raise TypeError("planner adapter must implement propose(context) and supports_rethinking")
    return adapter


__all__ = [
    "CallablePlannerAdapter",
    "IteratorPlannerAdapter",
    "PLANNER_ADAPTERS",
    "AgentPlannerAdapter",
    "VLMClosedLoopPlannerAdapter",
    "PlannerAdapter",
    "PlannerAdapterFactory",
    "create_planner_adapter",
    "normalize_planner_action",
    "register_planner_adapter",
]
