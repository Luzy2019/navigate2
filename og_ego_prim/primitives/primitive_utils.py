from typing import Optional

from omnigibson.envs import Environment
from omnigibson.objects import BaseObject


def find_task_related_object(
    env: Environment, 
    target_name: str, 
    retain_wrapper: bool = False,
) -> Optional[BaseObject]:
    """
    Description:
        在当前任务的 object_scope 中查找与 target_name 对应的对象。
        函数会先使用完整名称进行精确匹配；如果未命中，则按名称长度从长到短
        检查非 agent 对象，并使用候选名称中 ``.`` 之前的部分进行包含匹配。
        默认返回 scope wrapper 中的实际对象；retain_wrapper 为 True 时返回
        wrapper 本身。没有找到匹配对象时返回 None。

    Example:
        1) find_task_related_object(env, "apple.n.01_1")
        2) find_task_related_object(env, "apple")
        3) find_task_related_object(env, "unknown_object")

    Output:
        1) env.task.object_scope["apple.n.01_1"].wrapped_obj
        2) 与 "apple" 匹配的 task-related object
        3) None
    """
    task_related_objects = sorted(
        list(env.task.object_scope.keys()), 
        key=lambda name: len(name), 
        reverse=True
    )

    if target_name in task_related_objects:
        ref = env.task.object_scope[target_name]
        target_obj = ref if retain_wrapper else ref.wrapped_obj
        return target_obj

    for name in task_related_objects:
        ref = env.task.object_scope[name]
        if 'agent' in name:
            continue

        target_name = target_name.strip()
        candidate_name = name.split('.')[0].strip()
        if target_name in candidate_name:
            target_obj = ref if retain_wrapper else ref.wrapped_obj
            return target_obj
    
    return None
