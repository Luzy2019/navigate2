
from typing import List, Optional

from omnigibson import object_states
from omnigibson.action_primitives.action_primitive_set_base import ActionPrimitiveError
from omnigibson.envs import Environment
from omnigibson.object_states.object_state_base import BaseObjectState, RelativeObjectState
from omnigibson.object_states.particle_modifier import ParticleModifier
from omnigibson.objects import StatefulObject
from omnigibson.scenes import Scene
from omnigibson.systems.system_base import BaseSystem
from omnigibson.utils.constants import PrimType
import torch

from .attachment import (
    Attachment,
    FluidAttachment,
    StainAttachment,
    PlacementAttachment,
    Placement
)
from .primitive_utils import find_task_related_object
    

def is_visual_or_physical_particle_system(scene: Scene, system: BaseSystem) -> bool:
    """
    Description:
        判断 system 是否为场景中注册的视觉粒子系统或物理粒子系统。
        该函数只进行类型归类，不会创建、删除或修改粒子。

    Example:
        1) is_visual_or_physical_particle_system(scene, dust_system)
        2) is_visual_or_physical_particle_system(scene, water_system)
        3) is_visual_or_physical_particle_system(scene, non_particle_system)

    Output:
        1) True
        2) True
        3) False
    """
    if scene.is_visual_particle_system(system_name=system.name):
        return True
    if scene.is_physical_particle_system(system_name=system.name):
        return True
    return False


def get_obj_with_state(
    obj: StatefulObject | str, 
    state: BaseObjectState, 
    env: Optional[Environment] = None
) -> Optional[StatefulObject]:
    """
    Description:
        获取支持指定 object state 的对象。obj 可以是实际对象，也可以是任务
        object_scope 中的对象名；传入名称时必须同时提供 env。对象不存在、没有
        states 属性或不支持指定 state 时返回 None。

    Example:
        1) get_obj_with_state(cabinet, object_states.Open)
        2) get_obj_with_state("cabinet.n.01_1", object_states.Open, env)
        3) get_obj_with_state(apple, object_states.Open)

    Output:
        1) cabinet
        2) env 中对应且支持 Open 状态的 cabinet 对象
        3) None
    """

    if isinstance(obj, str):
        assert env is not None
        obj = find_task_related_object(env, obj)
    
    if obj is None:
        return None
    if not hasattr(obj, 'states'):
        return None
    if state not in obj.states:
        return None
    return obj

# deprecated 暂时用不到
def get_visible_task_related_objects(env: Environment) -> List[StatefulObject]:
    """
    Description:
        返回当前任务中逻辑上可见的相关对象。函数会排除 agent、建筑结构、
        空引用和非视觉粒子系统；如果对象位于具有 Open 状态且当前关闭的容器
        内，也会将其排除。该判断不检测相机视野、遮挡或光照，并已标记为
        deprecated，但当前仍有调用方。

    Example:
        1) get_visible_task_related_objects(env)

    Output:
        1) [table_obj, cabinet_obj, visible_dust_system]
           # 位于关闭 cabinet 内的对象不会出现在列表中
    """
    visible_task_related_objects = []

    for obj_name, obj_ref in env.task.object_scope.items():
        obj_ref = obj_ref.wrapped_obj
        if obj_name.strip().split('.')[0].strip() in ['agent', 'floor', 'ceiling', 'roof']:
            continue
        if obj_ref is None:
            continue
        if isinstance(obj_ref, BaseSystem) and not env.scene.is_visual_particle_system(system_name=obj_ref.name):
            continue

        is_obj_visible = True
        for placement_obj_name, placement_obj_ref in env.task.object_scope.items():
            placement_obj_ref = placement_obj_ref.wrapped_obj
            if placement_obj_name.strip().split('.')[0].strip() in ['agent', 'floor', 'ceiling', 'roof']:
                continue
            if placement_obj_ref is None:
                continue
            placement_obj = get_obj_with_state(placement_obj_ref, object_states.Open)
            if placement_obj is None:
                continue
            if placement_obj.states[object_states.Open].get_value():
                continue
            if is_target_object_predicate_with_obj(obj_ref, placement_obj_ref, object_states.Inside):
                is_obj_visible = False
                break

        if is_obj_visible:
            visible_task_related_objects.append(obj_ref)
    
    return visible_task_related_objects


def get_covered_systems(
    obj: StatefulObject | str, 
    env: Optional[Environment] = None
) -> Optional[List[BaseSystem]]:
    """
    Description:
        获取当前覆盖在对象表面的视觉或物理粒子系统。对象不存在或不支持
        Covered 状态时返回 None；支持 Covered 但未被任何粒子覆盖时返回空列表。

    Example:
        1) get_covered_systems(dusty_table)
        2) get_covered_systems(clean_table)
        3) get_covered_systems(non_coverable_obj)

    Output:
        1) [dust_system]
        2) []
        3) None
    """

    covering_systems = set()
    obj = get_obj_with_state(obj, object_states.Covered, env)
    if obj is None:
        return None
    
    for system in obj.scene.system_registry.objects:
        if not is_visual_or_physical_particle_system(obj.scene, system):
            continue
        if obj.states[object_states.Covered].get_value(system):
            covering_systems.add(system)
    
    return list(covering_systems)


def get_contained_systems(
    obj: StatefulObject | str,
    env: Optional[Environment] = None
) -> Optional[List[BaseSystem]]:
    """
    Description:
        获取当前包含在对象内部的视觉或物理粒子系统。对象不存在或不支持
        Contains 状态时返回 None；支持 Contains 但内部没有粒子时返回空列表。

    Example:
        1) get_contained_systems(bowl_with_water)
        2) get_contained_systems(empty_bowl)
        3) get_contained_systems(non_container)

    Output:
        1) [water_system]
        2) []
        3) None
    """

    contained_systems = set()
    obj = get_obj_with_state(obj, object_states.Contains, env) 
    if obj is None:
        return None
    
    for system in obj.scene.system_registry.objects:
        if not is_visual_or_physical_particle_system(obj.scene, system):
            continue
        if obj.states[object_states.Contains].get_value(system):
            contained_systems.add(system)

    return list(contained_systems)


def get_container(
    system: BaseSystem,
    env: Environment,
) -> Optional[StatefulObject]:
    """
    Description:
        在当前任务的 object_scope 中查找包含指定粒子 system 的对象。函数跳过
        agent，并只检查支持 Contains 状态的对象；找不到容器时返回 None。

    Example:
        1) get_container(water_system, env)
        2) get_container(uncontained_system, env)

    Output:
        1) 包含 water_system 的容器对象，例如 bowl_obj
        2) None
    """
    for container_name in env.task.object_scope.keys():
        if 'agent' in container_name:
            continue

        container_obj = get_obj_with_state(container_name, object_states.Contains, env)
        if container_obj is None:
            continue

        if container_obj.states[object_states.Contains].get_value(system):
            return container_obj
    
    return None


def get_produced_systems(
    obj: StatefulObject | str,
    env: Optional[Environment] = None
) -> Optional[List[BaseSystem]]:
    """
    Description:
        获取对象作为 ParticleSource 时，在当前状态条件下能够产生的系统。
        对象不存在或不支持 ParticleSource 状态时返回 None；当前不能产生任何
        系统时返回空列表。

    Example:
        1) get_produced_systems(running_faucet)
        2) get_produced_systems(apple)

    Output:
        1) [water_system]
        2) None
    """

    producing_systems = set()
    obj = get_obj_with_state(obj, object_states.ParticleSource, env)
    if obj is None:
        return None

    for system in obj.scene.system_registry.objects:
        if obj.states[object_states.ParticleSource].check_conditions_for_system(system.name):
            producing_systems.add(system)
    
    return list(producing_systems)


def get_saturated_systems(
    obj: StatefulObject | str,
    env: Optional[Environment] = None
) -> Optional[List[BaseSystem]]:
    """
    Description:
        获取当前使对象处于 Saturated 状态的视觉或物理粒子系统。对象不存在
        或不支持 Saturated 状态时返回 None；支持该状态但未被任何系统浸透时
        返回空列表。

    Example:
        1) get_saturated_systems(wet_rag)
        2) get_saturated_systems(dry_rag)

    Output:
        1) [water_system]
        2) []
    """

    saturated_systems = set()
    obj = get_obj_with_state(obj, object_states.Saturated, env)
    if obj is None:
        return None
    
    for system in obj.scene.system_registry.objects:
        if not is_visual_or_physical_particle_system(obj.scene, system):
            continue
        if obj.states[object_states.Saturated].get_value(system):
            saturated_systems.add(system)
    
    return list(saturated_systems)


def get_supported_systems(
    tool: StatefulObject,
    systems: List[BaseSystem],
    modifier: ParticleModifier,
) -> List[BaseSystem]:
    """
    Description:
        从 systems 中筛选出清洁或粒子修改工具声明支持的系统。modifier 应为
        工具 states 中的 ParticleModifier 状态类型，例如 ParticleRemover。
        该函数只检查支持能力，不检查工具当前是否满足实际修改条件。

    Example:
        1) get_supported_systems(rag, [dust_system, water_system],
                                 object_states.ParticleRemover)

    Output:
        1) [dust_system]
           # 假设 rag 的 ParticleRemover 仅支持移除 dust
    """
    supported_systems = set()

    for system in systems:
        if tool.states[modifier].supports_system(system.name):
            supported_systems.add(system)

    return list(supported_systems)


def get_modified_systems(
    tool: StatefulObject,
    systems: List[BaseSystem],
    modifier: ParticleModifier,
) -> List[BaseSystem]:
    """
    Description:
        从 systems 中筛选出工具在当前状态下实际可以修改的系统。与
        get_supported_systems 不同，本函数还会调用 modifier 的条件检查，
        因而会考虑工具是否已浸湿、开启或满足其他运行条件。

    Example:
        1) get_modified_systems(wet_rag, [dust_system, stain_system],
                                object_states.ParticleRemover)

    Output:
        1) [stain_system]
           # 假设 wet_rag 当前仅满足移除 stain 的条件
    """
    modified_systems = set()

    for system in systems:
        if tool.states[modifier].check_conditions_for_system(system.name):
            modified_systems.add(system)

    return list(modified_systems)


def is_target_object_predicate_with_obj(
    target_obj: StatefulObject, 
    obj: StatefulObject, 
    predicate: RelativeObjectState
) -> bool:
    """
    Description:
        判断 target_obj 是否通过指定的相对状态 predicate 与 obj 建立关系，
        例如 target_obj 是否位于 obj 内部或上方。target_obj 没有 states 属性
        或不支持该 predicate 时返回 False。

    Example:
        1) is_target_object_predicate_with_obj(
               apple, bowl, object_states.Inside
           )
        2) is_target_object_predicate_with_obj(
               water_system, bowl, object_states.Inside
           )

    Output:
        1) True  # 假设 apple 位于 bowl 内
        2) False # system 没有可查询的对象 states
    """
    # Maybe a FluidSystem like water
    if not hasattr(target_obj, 'states'):
        return False
    if not predicate in target_obj.states:
        return False
    return target_obj.states[predicate].get_value(obj)


def get_placement_objects(
    obj: StatefulObject, 
    env: Environment, 
    predicates: Optional[RelativeObjectState | List[RelativeObjectState]] = None,
) -> Optional[List[Placement]]:
    """
    Description:
        查找当前任务中相对于 obj 满足指定放置关系的对象，并将对象及对应
        predicate 封装为 Placement。默认检查 Inside 和 OnTop；obj 为 cloth
        时不进行查询并返回 None，没有匹配关系时返回空列表。

    Example:
        1) get_placement_objects(tray, env)
        2) get_placement_objects(bowl, env, object_states.Inside)
        3) get_placement_objects(cloth_obj, env)

    Output:
        1) [Placement(object=apple, predicate=object_states.OnTop)]
        2) [Placement(object=spoon, predicate=object_states.Inside)]
        3) None
    """
    if predicates is None:
        predicates = [object_states.Inside, object_states.OnTop]
    if not isinstance(predicates, list):
        predicates = [predicates]
    
    if obj.prim_type == PrimType.CLOTH:
        return None
    
    placements = []
    for target_obj_name in env.task.object_scope.keys():
        if 'agent' in target_obj_name:
            continue

        target_obj = find_task_related_object(env, target_obj_name.strip())
        if target_obj is None:
            continue 

        for predicate in predicates:
            if is_target_object_predicate_with_obj(target_obj, obj, predicate):
                placements.append(Placement(target_obj, predicate))
    
    return placements


def get_cooked_system(cooked_system: str, env: Environment) -> Optional[BaseSystem]:
    """
    Description:
        根据系统名称在当前任务作用域和场景可用系统中查找烹饪产物系统。
        任务对象名需要包含 cooked_system，且该系统必须存在于场景的
        available_systems 中；不满足条件时返回 None。

    Example:
        1) get_cooked_system("cooked_rice", env)
        2) get_cooked_system("unknown_food", env)

    Output:
        1) env._scene.get_system("cooked_rice")
        2) None
    """
    for system_name in env.task.object_scope.keys():
        if system_name.startswith('agent'):
            continue
        
        if cooked_system in system_name and cooked_system in env._scene.available_systems:
            return env._scene.get_system(cooked_system)
    
    return None


def is_cloth_place_on_other(target_obj: StatefulObject, placement_obj: StatefulObject) -> bool:
    """
    Description:
        判断是否属于“cloth 对象放置到 rigid 对象上”的组合。任一对象缺少
        prim_type 属性时返回 False。

    Example:
        1) is_cloth_place_on_other(rag, table)
        2) is_cloth_place_on_other(apple, table)

    Output:
        1) True  # rag 为 CLOTH，table 为 RIGID
        2) False
    """
    if not hasattr(target_obj, 'prim_type') or not hasattr(placement_obj, 'prim_type'):
        return False

    return target_obj.prim_type == PrimType.CLOTH \
        and placement_obj.prim_type == PrimType.RIGID


def check_open_before_grasp(
    obj: StatefulObject, 
    env: Environment
):
    """
    Description:
        检查 obj 是否位于关闭的可开启容器内。如果是，则抛出
        ActionPrimitiveError，要求先打开容器；否则正常返回，不修改任何状态。

    Example:
        1) check_open_before_grasp(apple_in_closed_cabinet, env)
        2) check_open_before_grasp(apple_on_table, env)

    Output:
        1) ActionPrimitiveError(PRE_CONDITION_ERROR)
        2) None
    """
    for parent_obj_name in env.task.object_scope.keys():
        parent_obj = get_obj_with_state(parent_obj_name, object_states.Open, env)
        if parent_obj is None:
            continue

        if is_target_object_predicate_with_obj(obj, parent_obj, object_states.Inside) \
              and parent_obj.states[object_states.Open].get_value() is False:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                f"You should open {parent_obj.name} first, because currently the operated object is place inside {parent_obj.name}.",
                {"operated object": obj.name, "parent object should be opened first": parent_obj.name},
            )


def check_open_before_placement(
    obj: StatefulObject,
    env: Optional[Environment] = None,
):
    """
    Description:
        检查作为放置目标的对象是否需要先打开。对象支持 Open 状态且当前关闭
        时抛出 ActionPrimitiveError；对象不支持 Open 或已经打开时正常返回。

    Example:
        1) check_open_before_placement(closed_cabinet)
        2) check_open_before_placement(open_cabinet)
        3) check_open_before_placement(table)

    Output:
        1) ActionPrimitiveError(PRE_CONDITION_ERROR)
        2) None
        3) None
    """
    obj = get_obj_with_state(obj, object_states.Open, env)
    if obj is None:
        return
    
    if obj.states[object_states.Open].get_value() is False:
        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
            f"You should open {obj.name} first before placing other object in {obj.name}.",
            {"object": obj.name},
        )
    

def check_close_before_toggle_on(
    obj: StatefulObject,
    env: Optional[Environment] = None,
):
    """
    Description:
        检查可开启设备在启动前是否已经关闭。设备支持 Open 状态且仍处于打开
        状态时抛出 ActionPrimitiveError；不支持 Open 或已经关闭时正常返回。

    Example:
        1) check_close_before_toggle_on(open_microwave)
        2) check_close_before_toggle_on(closed_microwave)

    Output:
        1) ActionPrimitiveError(PRE_CONDITION_ERROR)
        2) None
    """
    obj = get_obj_with_state(obj, object_states.Open, env)
    if obj is None:
        return
    
    if obj.states[object_states.Open].get_value():
        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
            "The machine must be closed and then toggled on",
            {"target object": obj.name},
        )


def check_toggle_off_before_open(
    obj: StatefulObject,
    env: Optional[Environment] = None,
):
    """
    Description:
        检查设备在打开前是否已经关闭电源。设备支持 ToggledOn 状态且仍处于
        开启状态时抛出 ActionPrimitiveError；不支持该状态或已经关闭时正常返回。

    Example:
        1) check_toggle_off_before_open(running_microwave)
        2) check_toggle_off_before_open(stopped_microwave)

    Output:
        1) ActionPrimitiveError(PRE_CONDITION_ERROR)
        2) None
    """
    obj = get_obj_with_state(obj, object_states.ToggledOn, env)
    if obj is None:
        return
    
    if obj.states[object_states.ToggledOn].get_value():
        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
            "The machine must be toggled off before open",
            {"target object": obj.name},
        )


def check_heat_source_before_cook(
    obj: StatefulObject, 
    env: Environment
):
    """
    Description:
        检查待烹饪对象是否正确放置在任务中的热源内或热源上，并在热源具有
        ToggledOn 状态时确认其已开启。对象未正确放置或热源未开启时抛出
        ActionPrimitiveError；满足烹饪前置条件时正常返回。

    Example:
        1) check_heat_source_before_cook(pot_on_running_stove, env)
        2) check_heat_source_before_cook(pot_on_stopped_stove, env)
        3) check_heat_source_before_cook(pot_on_table, env)

    Output:
        1) None
        2) ActionPrimitiveError(PRE_CONDITION_ERROR)
        3) ActionPrimitiveError(PRE_CONDITION_ERROR)
    """
    adjacency = obj.states[object_states.VerticalAdjacency].get_value()
    
    placed_heat_source = None
    for heat_source_name in env.task.object_scope.keys():
        heat_source = get_obj_with_state(heat_source_name, object_states.HeatSourceOrSink, env)
        if heat_source is None:
            continue

        if heat_source.states[object_states.HeatSourceOrSink].requires_inside:
            if not is_target_object_predicate_with_obj(obj, heat_source, object_states.Inside):
                continue
        else:
            if heat_source not in adjacency.negative_neighbors or heat_source in adjacency.positive_neighbors:
                continue
        
        placed_heat_source = heat_source
        if object_states.ToggledOn in heat_source.states \
              and heat_source.states[object_states.ToggledOn].get_value() is False:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                f"You should toggle the heat source on when cooking.",
                {"target object": obj.name, "heat source": heat_source.name},
            )

    if placed_heat_source is None:
        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
            f"You should first place the target inside or on top of a heat source before cooking.",
            {"target object": obj.name},
        )


def capture_attachments(obj: StatefulObject, env: Environment) -> List[Attachment]:
    """
    Description:
        捕获移动 obj 前需要保留的附着关系，并立即从场景中临时移除附着内容。
        当前会保存并移除容器内的流体粒子，以及隐藏位于 obj 内部或上方的普通
        对象；污渍对应的 StainAttachment 分支目前未启用。返回的 attachments
        应在 obj 移动完成后交给 recover_attachments 恢复。

    Example:
        1) attachments = capture_attachments(bowl_with_water, env)
        2) attachments = capture_attachments(tray_with_apple, env)
        3) attachments = capture_attachments(empty_table, env)

    Output:
        1) [FluidAttachment(...)]
        2) [PlacementAttachment(...)]
        3) []
    """
    attachments: List[Attachment] = []

    contained_systems = get_contained_systems(obj)
    if contained_systems is not None:
        for system in contained_systems:
            if hasattr(system, 'is_fluid') and system.is_fluid:  # 
                attachment = FluidAttachment(obj, system)
                attachment.remove_attachment()
                attachments.append(attachment)
    
    # covering_systems = get_covered_systems(obj)
    # if covering_systems is not None:
    #     for system in covering_systems:
    #         if not system.is_fluid:
    #             attachment = StainAttachment(obj, system)
    #             attachment.remove_attachment()
    #             attachments.append(attachment)

    placement_objects = get_placement_objects(obj, env)
    if placement_objects is not None:
        for placement in placement_objects:
            attachment = PlacementAttachment(obj, placement)
            attachment.remove_attachment()
            attachments.append(attachment)

    return attachments


def recover_attachments(attachments: List[Attachment]):
    """
    Description:
        逐个恢复 capture_attachments 返回的附着关系。每个 attachment 会根据
        主对象的新位姿重新生成流体粒子，或重新定位并显示附属对象。函数没有
        返回值；末尾的 del 只删除局部参数引用，不会清空调用方持有的列表。

    Example:
        1) attachments = capture_attachments(tray_with_apple, env)
           tray_with_apple.set_position_orientation(position=new_position)
           recover_attachments(attachments)
        2) recover_attachments([])

    Output:
        1) None  # apple 按相对位姿恢复到 tray 的新位置
        2) None
    """
    for attachment in attachments:
        attachment.recover_attachment()

    del attachments
