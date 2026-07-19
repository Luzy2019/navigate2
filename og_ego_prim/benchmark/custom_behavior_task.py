from copy import deepcopy
import inspect
import os

import omnigibson as og
from omnigibson.macros import gm
from omnigibson.tasks import BehaviorTask
from omnigibson.transition_rules import ChangeConditionWrapper
from omnigibson.utils.bddl_utils import *
from omnigibson.utils.asset_utils import get_all_object_category_models, decrypted

if os.getenv("OMNIGIBSON_NO_OMNIVERSE", default=0) != "1":
    import omnigibson.lazy as lazy


# fix bug, NotImplementError of is_compatible_asset function in ParticleRequirement class (execute when online sampling)
def custom__get_all_object_category_models_with_abilities(category, abilities):
    # Avoid circular imports
    from omnigibson.object_states.factory import get_requirements_for_ability, get_states_for_ability
    from omnigibson.objects.dataset_object import DatasetObject
    
    # Get all valid models
    all_models = get_all_object_category_models(category=category)

    # Generate all object states required per object given the requested set of abilities
    abilities_info = {
        ability: [(state_type, params) for state_type in get_states_for_ability(ability)]
        for ability, params in abilities.items()
    }

    # Get mapping for class init kwargs
    state_init_default_kwargs = dict()

    for ability, state_types_and_params in abilities_info.items():
        for state_type, _ in state_types_and_params:
            # Add each state's dependencies, too. Note that only required dependencies are added.
            for dependency in state_type.get_dependencies():
                if all(other_state != dependency for other_state, _ in state_types_and_params):
                    state_types_and_params.append((dependency, dict()))

        for state_type, _ in state_types_and_params:
            default_kwargs = inspect.signature(state_type.__init__).parameters
            state_init_default_kwargs[state_type] = {
                kwarg: val.default
                for kwarg, val in default_kwargs.items()
                if kwarg != "self" and val.default != inspect._empty
            }

    # Iterate over all models and sanity check each one, making sure they satisfy all the requested @abilities
    valid_models = []

    def supports_abilities(info, obj_prim):
        for ability, states_and_params in info.items():
            # Check ability requirements
            for requirement in get_requirements_for_ability(ability):
                if requirement.__name__ == 'ParticleRequirement':
                    return True
                elif not requirement.is_compatible_asset(prim=obj_prim)[0]:
                    return False

            # Check all link states
            for state_type, params in states_and_params:
                kwargs = deepcopy(state_init_default_kwargs[state_type])
                kwargs.update(params)
                if not state_type.is_compatible_asset(prim=obj_prim, **kwargs)[0]:
                    return False
        return True

    for model in all_models:
        usd_path = DatasetObject.get_usd_path(category=category, model=model)
        usd_path = usd_path.replace(".usd", ".encrypted.usd")
        with decrypted(usd_path) as fpath:
            stage = lazy.pxr.Usd.Stage.Open(fpath)
            prim = stage.GetDefaultPrim()
            if supports_abilities(abilities_info, prim):
                valid_models.append(model)

    return valid_models


class CustomBDDLSampler(BDDLSampler):

    def __init__(self, *args, required_instance_models=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._required_instance_models = dict(required_instance_models or {})

    def _room_semantic_instance_mapping(self):
        """Restore exact room labels dropped by OmniGibson's tensor-key grouping."""
        mapping = {
            semantic_name: list(dict.fromkeys(instance_names))
            for semantic_name, instance_names in
            self._env.scene.seg_map.room_sem_name_to_ins_name.items()
        }
        for obj in self._env.scene.objects:
            in_rooms = getattr(obj, "in_rooms", ()) or ()
            if isinstance(in_rooms, str):
                in_rooms = (in_rooms,)
            for room_instance in in_rooms:
                semantic_name, separator, instance_index = str(room_instance).rpartition("_")
                if not separator or not instance_index.isdigit():
                    continue
                instances = mapping.setdefault(semantic_name, [])
                if room_instance not in instances:
                    instances.append(room_instance)
        for instances in mapping.values():
            instances.sort(
                key=lambda name: (
                    int(name.rpartition("_")[2])
                    if name.rpartition("_")[2].isdigit()
                    else float("inf"),
                    name,
                )
            )
        return mapping

    def _apply_required_instance_model(self, obj_inst, category, model_choices):
        requirement = self._required_instance_models.get(obj_inst)
        if requirement is None:
            return model_choices

        required_synset = requirement.get("synset")
        if (
            required_synset is not None
            and required_synset != self._object_instance_to_synset.get(obj_inst)
        ):
            return set()
        required_category = requirement.get("dataset_category")
        if required_category is not None and required_category != category:
            return set()
        required_model = requirement.get("model")
        if required_model is None:
            return model_choices
        return set(model_choices).intersection({required_model})

    def _apply_good_model_filter(self, obj_inst, category, model_choices):
        requirement = self._required_instance_models.get(obj_inst) or {}
        if requirement.get("model") is not None:
            return model_choices
        if category not in GOOD_MODELS:
            return model_choices
        return set(model_choices).intersection(GOOD_MODELS[category])

    def _allow_required_scene_model(self, obj_inst, category, model_choices):
        """Opt in to a known fixed-scene fixture when ability metadata is stale."""
        requirement = self._required_instance_models.get(obj_inst) or {}
        if not requirement.get("allow_unvalidated_scene_model"):
            return model_choices
        if requirement.get("dataset_category") != category:
            return model_choices
        required_model = requirement.get("model")
        if required_model is None or required_model not in get_all_object_category_models(category):
            return model_choices
        return set(model_choices).union({required_model})

    def _sample_initial_conditions_final(self):
        """Sample the initial state without interpreting it as an executed transition."""
        if not gm.ENABLE_TRANSITION_RULES:
            return super()._sample_initial_conditions_final()

        transition_api = self._env.scene.transition_rule_api
        transition_api.active_rules = set()
        try:
            result = super()._sample_initial_conditions_final()
        finally:
            transition_api.refresh_all_rules()
            # Change-triggered recipes should compare against the completed initial
            # state, not the default state that existed while BDDL was sampling.
            for rule in transition_api.active_rules:
                if rule.conditions is None:
                    rule.conditions = rule._generate_conditions()
                for condition in rule.conditions:
                    if isinstance(condition, ChangeConditionWrapper):
                        candidates = {
                            name: values.copy() for name, values in rule.candidates.items()
                        }
                        condition(object_candidates=candidates)
        return result

    def _parse_inroom_object_room_assignment(self):
        """Accept either a semantic room type or an exact room instance."""
        self._room_type_to_object_instance = {}
        self._inroom_object_instances = set()
        room_sem_to_instances = self._room_semantic_instance_mapping()
        room_instances = {
            room_instance
            for instances in room_sem_to_instances.values()
            for room_instance in instances
        }
        valid_room_tokens = set(room_sem_to_instances) | room_instances

        for cond in self._activity_conditions.parsed_initial_conditions:
            if cond[0] != "inroom":
                continue
            obj_inst, room_token = cond[1], cond[2]
            obj_synset = self._object_instance_to_synset[obj_inst]
            if "sceneObject" not in OBJECT_TAXONOMY.get_abilities(obj_synset):
                return (
                    f"You have assigned room [{room_token}] for [{obj_synset}], but [{obj_synset}] is sampleable. "
                    "Only non-sampleable (scene) objects can have room assignment."
                )
            if self._scene_model is not None and room_token not in valid_room_tokens:
                return f"Room [{room_token}] missing in scene [{self._scene_model}]."
            self._room_type_to_object_instance.setdefault(room_token, []).append(obj_inst)
            if obj_inst in self._inroom_object_instances:
                return f"Object [{obj_inst}] has more than one room assignment"
            self._inroom_object_instances.add(obj_inst)

    def _import_sampleable_objects(self):
        assert og.sim.is_stopped(), "Simulator should be stopped when importing sampleable objects"

        # Move the robot object frame to a far away location, similar to other newly imported objects below
        self._agent.set_position_orientation(
            position=th.tensor([300, 300, 300], dtype=th.float32), orientation=th.tensor([0, 0, 0, 1], dtype=th.float32)
        )

        self._sampled_objects = set()
        num_new_obj = 0
        # Only populate self.object_scope for sampleable objects
        available_categories = set(get_all_object_categories())

        # Attached states introduce dependencies among objects during import time.
        # For example, when importing a child object instance, we need to make sure the imported model can be attached
        # to the parent object instance. We sort the object instances such that parent object instances are imported
        # before child object instances.
        dependencies = {key: self._attached_objects.get(key, {}) for key in self._object_instance_to_synset.keys()}
        for obj_inst in list(reversed(list(nx.algorithms.topological_sort(nx.DiGraph(dependencies))))):
            obj_synset = self._object_instance_to_synset[obj_inst]

            # Don't populate agent
            if obj_synset == "agent.n.01":
                continue

            # Populate based on whether it's a substance or not
            if is_substance_synset(obj_synset):
                assert len(self._activity_conditions.parsed_objects[obj_synset]) == 1, "Systems are singletons"
                obj_inst = self._activity_conditions.parsed_objects[obj_synset][0]
                system_name = OBJECT_TAXONOMY.get_subtree_substances(obj_synset)[0]
                self._object_scope[obj_inst] = BDDLEntity(
                    bddl_inst=obj_inst,
                    entity=(
                        None if obj_inst in self._future_obj_instances else self._env.scene.get_system(system_name)
                    ),
                )
            else:
                valid_categories = set(OBJECT_TAXONOMY.get_subtree_categories(obj_synset))
                categories = list(valid_categories.intersection(available_categories))
                if len(categories) == 0:
                    return (
                        f"None of the following categories could be found in the dataset for synset {obj_synset}: "
                        f"{valid_categories}"
                    )

                # Don't explicitly sample if future
                if obj_inst in self._future_obj_instances:
                    self._object_scope[obj_inst] = BDDLEntity(bddl_inst=obj_inst)
                    continue
                # Don't sample if already in room
                if obj_inst in self._inroom_object_instances:
                    continue

                # Shuffle categories and sample to find a valid model
                random.shuffle(categories)
                model_choices = set()
                for category in categories:
                    # Get all available models that support all of its synset abilities
                    model_choices = set(
                        custom__get_all_object_category_models_with_abilities(
                            category=category,
                            abilities=OBJECT_TAXONOMY.get_abilities(OBJECT_TAXONOMY.get_synset_from_category(category)),
                        )
                    )
                    model_choices = self._apply_good_model_filter(
                        obj_inst,
                        category,
                        model_choices,
                    )
                    model_choices -= BAD_CLOTH_MODELS.get(category, set())
                    model_choices = self._filter_model_choices_by_attached_states(model_choices, category, obj_inst)
                    model_choices = self._apply_required_instance_model(
                        obj_inst,
                        category,
                        model_choices,
                    )
                    if len(model_choices) > 0:
                        break

                if len(model_choices) == 0:
                    # We failed to find ANY valid model across ALL valid categories
                    return f"Missing valid object models for all categories: {categories}"

                # Randomly select an object model
                model = random.choice(list(model_choices))

                # Potentially add additional kwargs
                obj_kwargs = dict()

                required_scale = (self._required_instance_models.get(obj_inst) or {}).get("scale")
                if required_scale is not None:
                    if isinstance(required_scale, (int, float)):
                        required_scale = [float(required_scale)] * 3
                    else:
                        required_scale = [float(value) for value in required_scale]
                    if len(required_scale) != 3 or any(value <= 0 for value in required_scale):
                        raise ValueError(
                            f"required scale for {obj_inst} must contain three positive values"
                        )
                    obj_kwargs["scale"] = required_scale
                else:
                    obj_kwargs["bounding_box"] = GOOD_BBOXES.get(category, dict()).get(model, None)

                # create the object
                simulator_obj = DatasetObject(
                    name=f"{category}_{len(self._env.scene.objects)}",
                    category=category,
                    model=model,
                    prim_type=(
                        PrimType.CLOTH
                        if "cloth" in OBJECT_TAXONOMY.get_abilities(obj_synset)
                        else PrimType.RIGID
                    ),
                    **obj_kwargs,
                )
                num_new_obj += 1

                # Load the object into the simulator
                self._env.scene.add_object(simulator_obj)

                # Set these objects to be far-away locations
                simulator_obj.set_position_orientation(
                    position=th.tensor([100.0, 100.0, -100.0]) + th.ones(3) * num_new_obj * 5.0
                )

                self._sampled_objects.add(simulator_obj)
                self._object_scope[obj_inst] = BDDLEntity(bddl_inst=obj_inst, entity=simulator_obj)

        og.sim.play()
        og.sim.stop()

    def _build_inroom_object_scope(self):
        room_type_to_scene_objs = {}
        room_sem_to_instances = self._room_semantic_instance_mapping()
        room_instance_to_sem = {
            room_instance: room_sem
            for room_sem, room_instances in room_sem_to_instances.items()
            for room_instance in room_instances
        }
        for room_token in self._room_type_to_object_instance:
            room_type_to_scene_objs[room_token] = {}
            for obj_inst in self._room_type_to_object_instance[room_token]:
                room_type_to_scene_objs[room_token][obj_inst] = {}
                obj_synset = self._object_instance_to_synset[obj_inst]

                # We allow burners to be used as if they are stoves
                # No need to safeguard check for subtree_substances because inroom objects will never be substances
                categories = OBJECT_TAXONOMY.get_subtree_categories(obj_synset)

                # Grab all models that fully support all abilities for the corresponding category
                valid_models = {
                    cat: set(
                        custom__get_all_object_category_models_with_abilities(
                            cat, OBJECT_TAXONOMY.get_abilities(OBJECT_TAXONOMY.get_synset_from_category(cat))
                        )
                    )
                    for cat in categories
                }
                valid_models = {
                    cat: self._apply_good_model_filter(obj_inst, cat, models)
                    - BAD_CLOTH_MODELS.get(cat, set())
                    for cat, models in valid_models.items()
                }
                valid_models = {
                    cat: self._filter_model_choices_by_attached_states(models, cat, obj_inst)
                    for cat, models in valid_models.items()
                }
                valid_models = {
                    cat: self._apply_required_instance_model(
                        obj_inst,
                        cat,
                        self._allow_required_scene_model(obj_inst, cat, models),
                    )
                    for cat, models in valid_models.items()
                }
                if self._scene_model is None:
                    room_insts = [None]
                elif room_token in room_instance_to_sem:
                    room_insts = [room_token]
                else:
                    room_insts = room_sem_to_instances[room_token]
                for room_inst in room_insts:
                    # A list of scene objects that satisfy the requested categories
                    room_objs = self._env.scene.object_registry("in_rooms", room_inst, default_val=[])
                    scene_objs = [
                        obj
                        for obj in room_objs
                        if obj.category in categories and obj.model in valid_models[obj.category]
                    ]

                    if len(scene_objs) != 0:
                        room_type_to_scene_objs[room_token][obj_inst][room_inst] = scene_objs

        error_msg = self._consolidate_room_instance(room_type_to_scene_objs, "initial_pre-sampling")
        if error_msg:
            return error_msg
        self._inroom_object_scope = room_type_to_scene_objs


class CustomBehaviorTask(BehaviorTask):

    def __init__(
        self,
        activity_name=None,
        activity_definition_id=0,
        activity_instance_id=0,
        predefined_problem=None,
        online_object_sampling=False,
        highlight_task_relevant_objects=False,
        termination_config=None,
        reward_config=None,
        required_instance_models=None,
    ):
        self.required_instance_models = dict(required_instance_models or {})
        super().__init__(
            activity_name=activity_name,
            activity_definition_id=activity_definition_id,
            activity_instance_id=activity_instance_id,
            predefined_problem=predefined_problem,
            online_object_sampling=online_object_sampling,
            highlight_task_relevant_objects=highlight_task_relevant_objects,
            termination_config=termination_config,
            reward_config=reward_config,
        )

    def initialize_activity(self, env):
        accept_scene = True
        feedback = None

        # Generate sampler
        self.sampler = CustomBDDLSampler(
            env=env,
            activity_conditions=self.activity_conditions,
            object_scope=self.object_scope,
            backend=self.backend,
            required_instance_models=self.required_instance_models,
        )

        # Compose future objects
        self.future_obj_instances = {
            init_cond.body[1] for init_cond in self.activity_initial_conditions if init_cond.body[0] == "future"
        }

        if self.online_object_sampling:
            # Sample online
            accept_scene, feedback = self.sampler.sample()
            if not accept_scene:
                raise RuntimeError(f"Failed to sample Behavior activity: {feedback}")
        else:
            # Load existing scene cache and assign object scope accordingly
            self.assign_object_scope_with_cache(env)

        # Generate goal condition with the fully populated self.object_scope
        self.activity_goal_conditions = get_goal_conditions(self.activity_conditions, self.backend, self.object_scope)
        self.ground_goal_state_options = get_ground_goal_state_options(
            self.activity_conditions, self.backend, self.object_scope, self.activity_goal_conditions
        )
        return accept_scene, feedback
