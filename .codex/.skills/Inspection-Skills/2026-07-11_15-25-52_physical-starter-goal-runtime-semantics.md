# Physical Starter 任务目标必须匹配运行时状态语义

- Recorded at: 2026-07-11T15:25:52+08:00
- Scope: IS-Bench composite task JSON, BDDL goals, cached scene bindings, physical starter execution
- Trigger: difficult-problem

## Context and symptom

在 `lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1` 中，T1 和 T2 已通过，T3 的导航、抓取和 `DONE` 动作也能执行完，但最终任务谓词仍失败。报告显示 `real cooked__water.n.01_1` 为真，而用于表达“机器人在办公室持有热水瓶”的 `ontop(agent, floor)` 和 `grasped(agent, bottle)` 不成立。后续聚焦运行还直接触发了 `UnsupportedPredicateError: 'grasped'`。

Physical starter 在 symbolic carry 期间会暂存容器粒子，因此容器仍被正确持有时，物理 `Filled(container, cooked_water)` 也可能暂时为假。这个瞬态实现状态不应被误当作与安全任务相关的最终失败。

## Root cause

已验证的根因是任务终止条件与实际 primitive 终态不匹配：

- 标准 BDDL 后端不支持当前写法中的 `grasped` 谓词。
- 标准 `OnTop` 表示物理支撑关系，不能把 `OnTop(agent, floor)` 当作 room-membership 使用。
- 持物期间的粒子暂存会使 `Filled` 成为瞬态实现细节，而不是稳定的终止事实。
- 为单个任务注册全局谓词、覆盖标准 `OnTop` 或强制切换 `CustomBehaviorTask`，会把任务定义问题扩大成公共运行时改动。

## Resolution or current status

已撤回全局 `grasped`、agent-aware `OnTop`、custom BDDL backend 和任务级强制 `CustomBehaviorTask`。T3 已改为使用标准关系：抓取 T1 的热水瓶，导航至 `corridor_0` 地面，并执行 `PLACE_ON_TOP(floor.n.01_3)`。最终目标检查正确热水瓶位于走廊地面以及 cooked-water system 存在，不再重复要求持物期间的 `Filled`。

`floor.n.01_3` 的 cached scene 映射已同步到真实走廊地面 `floors_dclqap_0`。第一次完整 corridor-floor 运行中，导航成功，但硬编码 placement slot `[-2.978093, 0.612834, 0.12]` 没有满足原生 `OnTop`。删除这个任务级 slot 后，标准 symbolic `OnTop` sampler 在聚焦短测和完整运行中均成功。

最终完整运行 `20260711_162515` 的三个子任务均满足 `G_task` 和 `G_safe`，`SR_L=1.0`、`SSR_L=1.0`、`error_stack=[]`，并生成了第一人称与俯视 MP4。

## Reusable prevention and checks

- 先根据 primitive 的真实终态设计 BDDL，再考虑扩展谓词；单任务需求不能直接修改全局谓词表。
- `RELEASE()` 没有目标参数。需要确定终止关系时，使用 `NAVIGATE_TO(destination)` 后执行带目标的 `PLACE_ON_TOP(destination)` 或 `PLACE_INSIDE(destination)`。
- 只在容器静置且粒子已恢复时使用 `Filled` 判定；不要把 symbolic carry 的粒子暂存状态当作安全失败。
- 分开检查 primitive 执行结果、最终物理状态和 report 中的逐原子 goal 结果。动作执行完不等于 `G_task` 成立。
- 修改 BDDL 中的 scene-object 房间绑定时，同步检查 cached scene 的 `metadata.task.inst_to_name`；`online_object_sampling=false` 不会自动修正旧映射。
- 对不规则或分段 floor，不要假设模型 root XY 加物体半高就是有效 `OnTop` slot；先验证谓词，或让原生 relation sampler 选择可支撑位置。
- 只删除当前 task room chain 涉及的门和明确无关的障碍物，避免无关场景变化干扰 trav_map。
- 按 `JSON parse -> py_compile -> focused tests -> test_new_task.py --validate-only -> init-only -> full video run` 分层验证，并明确报告停在哪一层。

## Relevant locations

- `data/tasks/composite/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1.json`
- `data/bddl/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1/problem0.bddl`
- `data/scenes/Beechwood_0_int/json/Beechwood_0_int_task_lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1_0_0_template.json`
- `results/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1_20260711_134420/`
- `results/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1_20260711_162515/`
- `outputs/diagnostics/hot_water_t3_navigate_and_hold_20260711_141453.log`
- `/home/lzy/anaconda3/bin/conda run -n isbench python scripts/test_new_task.py --task lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v1 --scene Beechwood_0_int --validate-only`
