你现在要“新建 scene”，**最稳的方式不是手写 / 手改 scene JSON**，而是：

> **让 OmniGibson 官方加载 base scene + task，然后由 OmniGibson 自己 dump / save 出 scene state 文件。**

你上传的这个文件就是这种类型的东西：它包含 `metadata.task.inst_to_name`，把 BDDL 里的符号对象映射到具体对象名；也包含 `state.object_registry`、`state.system_registry`、`init_info`、`objects_info` 等运行时状态信息。比如它明确绑定了 `beer_glass.n.01_1 -> beer_glass_179`、`microwave.n.02_1 -> microwave_bfbeeb_0`、`water.n.06_1 -> water`，并且 scene 初始化信息里是 `InteractiveTraversableScene`，`scene_model` 是 `Beechwood_0_int`。 

---

## 结论先说

如果你想要**稳定正确的 scene 文件**，建议按这个顺序：

```text
官方 base scene
    ↓
指定 task / activity
    ↓
使用 OmniGibson / BDDL sampler 生成 task instance
    ↓
reset + 物理稳定若干步
    ↓
dump scene state
    ↓
重新加载验证
    ↓
保存为你的 template / scene json
```

不要直接手写：

```text
object_registry
system_registry
joint_pos
particle_states
inst_to_name
```

这些字段手改很容易出错。

---

## 1. 你要区分两种 scene 文件

### A. Base scene

这是基础环境，例如：

```text
Beechwood_0_int
Merom_0_int
Rs_int
```

它描述的是房子、厨房、客厅、固定家具、可行走地图等。

你的上传文件里 `init_info.args.scene_model = "Beechwood_0_int"`，说明它底层用的是 `Beechwood_0_int` 这个 base scene。

---

### B. Task scene / task instance scene

这是：

```text
某个 task + 某个 scene + 某组具体物体状态
```

比如你这个文件名：

```text
Beechwood_0_int_task_boil_water_in_the_microwave__with_beer_glass_0_0_template.json
```

它不是普通的 `Beechwood_0_int`，而是：

```text
Beechwood_0_int
+
boil_water_in_the_microwave
+
beer_glass 版本的任务实例
+
具体对象位置 / 状态
```

所以它更准确叫：

```text
task-specific scene state dump
```

---

## 2. 最稳定的做法：从官方 scene + 官方 task instance 生成

如果你只是想跑 benchmark / eval，建议优先使用：

```yaml
online_object_sampling: false
```

也就是用官方已经采样好的 task instance。

原因是：

```text
稳定性最高
可复现
更接近官方 benchmark 设置
不容易出现采样失败
不容易出现物体穿模、掉落、初始条件不满足
```

你的 config 思路应该类似：

```yaml
scene:
  type: InteractiveTraversableScene
  scene_model: Beechwood_0_int

task:
  type: BehaviorTask
  activity_name: boil_water_in_the_microwave
  activity_definition_id: 0
  activity_instance_id: 0
  online_object_sampling: false
```

然后让 OmniGibson 正常加载、reset、保存状态。

---

## 3. 如果你真的要“新建”一个 task scene

如果你的意思是：**我要为一个新的 task / 新的 scene 组合生成一个新的 template JSON**，那推荐流程是：

```text
1. 选一个官方 base scene
2. 选 task，比如 boil_water_in_the_microwave
3. 开启一次 online_object_sampling
4. 固定随机种子
5. 让 BDDL sampler 采样对象和初始状态
6. reset 后让物理稳定若干步
7. 检查 initial conditions 是否满足
8. dump scene state
9. 以后 eval 时关闭 online_object_sampling，直接加载这个保存好的文件
```

也就是：

```yaml
task:
  online_object_sampling: true
```

只用于**生成模板**。

生成稳定模板之后，再切回：

```yaml
task:
  online_object_sampling: false
```

用于正式测试。

---

## 4. 为什么不要手动改 JSON？

因为这个 JSON 里不只是物体坐标。

它包含至少几类东西：

```text
metadata.task.inst_to_name
state.system_registry
state.object_registry
init_info
objects_info
```

你上传的文件中 `objects_info` 记录了每个物体的 `category`、`model`、`scale`、`fixed_base`、`in_rooms`、`prim_path`、`uuid` 等信息；例如 `microwave_bfbeeb_0` 是 `microwave` 类别、`bfbeeb` 模型、位于 `kitchen_0`。

这些字段之间必须一致：

```text
BDDL object scope
    ↔ inst_to_name
        ↔ object_registry
            ↔ objects_info
                ↔ scene assets
                    ↔ physics state
```

只改一个地方，很容易导致：

```text
找不到对象
谓词检查失败
初始条件不成立
物体穿模
机器人导航失败
粒子系统丢失
关节状态异常
```

---

## 5. 稳定 scene 文件应该满足什么标准？

保存之前建议做这些检查：

```text
1. env.reset() 能正常完成
2. 重新加载保存的 JSON 后不报错
3. BDDL initial conditions 全部满足
4. task object scope 里的对象都能在 scene 中找到
5. inst_to_name 里的对象名都存在于 object_registry
6. 机器人初始位姿可达、没有卡在家具里
7. 关键物体没有掉落、穿模、悬空
8. 关节状态没有 NaN / 异常大数值
9. 速度最好接近 0，至少不是还在剧烈运动
10. goal condition 检查器能正常运行
```

特别是你这个文件里很多对象有 `lin_vel` / `ang_vel`，说明它保存的是一个仿真状态快照，而不是纯静态布局。如果想更稳定，最好在保存前让物理多 step 一段时间，等物体 settle。

---

## 6. 推荐生成方式

你可以写一个脚本，核心逻辑是：

```python
import omnigibson as og

cfg = {
    "scene": {
        "type": "InteractiveTraversableScene",
        "scene_model": "Beechwood_0_int",
    },
    "robots": [
        {
            "type": "Fetch",
            "obs_modalities": ["rgb"],
        }
    ],
    "task": {
        "type": "BehaviorTask",
        "activity_name": "boil_water_in_the_microwave",
        "activity_definition_id": 0,
        "activity_instance_id": 0,

        # 生成新实例时用 True
        # 正式 eval / 稳定复现时用 False
        "online_object_sampling": True,

        # online_object_sampling=True 时通常不要用 presampled robot pose
        "use_presampled_robot_pose": False,
    },
}

env = og.Environment(configs=cfg)
env.reset()

# 让物体稳定
for _ in range(200):
    og.sim.step()

# 然后用你当前 OmniGibson 版本提供的 save / dump scene state API 保存
# 具体函数名以你安装版本为准：
# scene_state = env.scene.dump_state(...)
# init_info = env.scene.dump_init_info(...)
# objects_info = ...
```

重点不是这段 API 名字，而是流程：

```text
加载 → 采样 → reset → settle → dump → reload verify
```

---

## 7. 推荐的实际策略

如果你是为了 **IS-Bench / BEHAVIOR eval**，我建议：

### 最稳策略

```text
使用官方预采样 task instance
online_object_sampling = false
不要自己新采样
```

### 如果必须新增 scene

```text
用 online_object_sampling=true 只生成一次
固定 seed
生成多个候选
逐个 reload 验证
保留能通过 initial condition 的那个
保存为 template
正式测试时 online_object_sampling=false
```

### 不推荐

```text
直接复制一个 JSON
手动改 scene_model
手动改 object_registry 里的物体名
手动改 inst_to_name
手动替换 microwave / sink / glass 等对象
```

---

## 8. 一句话总结

你要的“稳定正确 scene 文件”，本质上应该是：

> **由 OmniGibson 在官方 BEHAVIOR assets 上生成并验证过的 scene state dump，而不是人工拼出来的 JSON。**

你现在这个文件可以当模板参考，但新建 scene 时最好走：

```text
官方 base scene + BDDL task + OmniGibson sampler + dump/save + reload 验证
```

这样得到的 scene 文件才稳定。
=========================
是的，**这个页面就是“创建 BEHAVIOR task scene / task instance”的官方流程**。但要注意一个关键区别：

> 它不是从零创建一个新的房子 / base scene，而是基于已有或已注册的 scene，给某个 BEHAVIOR task 采样出稳定的 **task-specific scene JSON**。

也就是你现在上传的这种文件：

```text
Beechwood_0_int_task_boil_water_in_the_microwave__with_beer_glass_0_0_template.json
```

它属于：

```text
base scene + task + task-related objects + 初始状态采样结果
```

不是单纯的 base scene。你上传的 JSON 里也能看到它保存了 `metadata.task.inst_to_name`、`system_registry`、`object_registry`、`init_info`、`objects_info` 等信息，说明它是一个已经实例化过的 task scene state dump。

---

# 1. 先判断：你要的是哪种“自定义 scene”？

这里有两种情况。

## 情况 A：基于官方已有 scene 新建 task scene

比如：

```text
Beechwood_0_int
+
boil_water_in_the_microwave
+
指定 beer_glass / microwave / sink / cabinet
```

这种最推荐，也最稳定。

官方 BEHAVIOR 本身提供 50 个 fully interactive house-scale scenes，包括 apartment、house、office、restaurant、school 等多种环境，`Beechwood_0_int`、`Rs_int`、`Merom_0_int` 都属于这类已有 scene。([行为][1])

## 情况 B：真的导入一个全新的 base scene

比如你自己做一个 `my_kitchen_int`。

这种难度高很多，因为你不只是要有 USD 模型，还要有：

```text
房间划分
物体语义标注
可行走地图 traversability map
segmentation map
object metadata
room instance 信息
物理碰撞
可交互物体
```

官方 task sampling 页面允许在生成模板时“输入 custom scene name”，但前提是这个 custom scene 已经能被 OmniGibson 正确识别和加载。页面里明确说，生成 template 时会提示选择 scene，可以从几个内置 house scene 中选，也可以输入自定义 scene name。([行为][2])

所以你现在更现实的方案应该是：

> **先不要从零造 base scene，而是在官方 base scene 上生成自己的 task-specific scene JSON。**

---

# 2. 推荐方案：基于官方流程生成自定义 task scene

下面我按你当前目标设计一个落地流程。

假设你的目标是：

```bash
TASK_NAME=boil_water_in_the_microwave
SCENE_NAME=Beechwood_0_int
```

生成类似：

```text
Beechwood_0_int_task_boil_water_in_the_microwave_0_0_template.json
Beechwood_0_int_task_boil_water_in_the_microwave_instances/
```

---

## Step 0：准备 task instance 数据仓库

官方文档要求把 `2026-challenge-task-instances` clone 到 `gm.DATA_PATH` 下。([行为][2])

先看你的 `gm.DATA_PATH` 在哪：

```bash
python - <<'PY'
from omnigibson.macros import gm
print(gm.DATA_PATH)
PY
```

然后进入这个目录：

```bash
cd $(python - <<'PY'
from omnigibson.macros import gm
print(gm.DATA_PATH)
PY
)
```

clone 数据仓库：

```bash
git clone https://github.com/wensi-ai/2026-challenge-task-instances
```

目录大概会变成：

```text
$DATA_PATH/
├── og_dataset/
├── assets/
└── 2026-challenge-task-instances/
    ├── metadata/
    └── scenes/
```

---

## Step 1：确认 BDDL task 是否合理

官方流程第一步是检查：

```text
bddl3/bddl/activity_definitions/TASK_NAME/problem_0.bddl
```

文档特别提醒要检查 BDDL 定义是否合理，尤其要注意 wildcard expansions。([行为][2])

例如：

```bash
less bddl3/bddl/activity_definitions/boil_water_in_the_microwave/problem_0.bddl
```

你重点看三块：

```lisp
:objects
:init
:goal
```

你要确认：

```text
1. 任务需要哪些 synset
2. 哪些物体必须在 kitchen
3. 初始状态是否依赖 microwave / sink / cabinet / container / water
4. goal 是否需要 heated / cooked / filled / inside 之类状态
```

---

## Step 2：生成 task_custom_lists.json 模板

官方命令是：

```bash
python OmniGibson/scripts/sampling/autogenerate_task_custom_list_template.py -t TASK_NAME
```

页面说明这个脚本会交互式提示你选择 scene，并为每个 required synset / category 选择可用 model ID，最终写入：

```text
datasets/2026-challenge-task-instances/metadata/task_custom_lists.json
```

([行为][2])

你的命令可以写成：

```bash
TASK_NAME=boil_water_in_the_microwave

python OmniGibson/scripts/sampling/autogenerate_task_custom_list_template.py \
  -t ${TASK_NAME}
```

交互时：

```text
Scene:
  输入 Beechwood_0_int

room_types:
  选 kitchen

beer_glass / mug / microwave / sink / cabinet:
  选择你本地已有的 model ID
```

你最终希望 `task_custom_lists.json` 里出现类似结构：

```json
{
  "boil_water_in_the_microwave": {
    "room_types": [
      "kitchen"
    ],
    "Beechwood_0_int": {
      "whitelist": {
        "beer_glass.n.01": {
          "beer_glass": {
            "179": null
          }
        },
        "microwave.n.02": {
          "microwave": {
            "bfbeeb": null
          }
        },
        "sink.n.01": {
          "sink": {
            "czyfhq": null
          }
        },
        "cabinet.n.01": {
          "top_cabinet": {
            "dmwxyl": null
          }
        }
      },
      "blacklist": {}
    }
  }
}
```

上面只是结构示例，**model ID 要以你本地 dataset 里实际存在的为准**。你上传的 JSON 里已经出现了 `microwave_bfbeeb_0`、`sink_czyfhq_0`、`top_cabinet_dmwxyl_2`、`beer_glass_179`，所以这些可以作为优先候选。

---

## Step 3：采样 Task-Related Objects，也就是 TRO

官方第二步命令是：

```bash
python OmniGibson/scripts/sampling/sample_b1k_tasks.py -t TASK_NAME
```

官方也建议用 `-m pdb` 跑，这样采样失败时可以直接停在错误位置调试。采样成功后，会在：

```text
datasets/2026-challenge-task-instances/scenes/SCENE_NAME/json/
```

下面生成两个文件：

```text
SCENE_NAME_task_TASK_NAME_0_0_template-partial_rooms.json
SCENE_NAME_task_TASK_NAME_0_0_template.json
```

其中 `partial_rooms` 是中间结果，完整的 `template.json` 是后处理后、合并了完整 scene objects 的版本。([行为][2])

建议这样跑：

```bash
TASK_NAME=boil_water_in_the_microwave

python -m pdb OmniGibson/scripts/sampling/sample_b1k_tasks.py \
  -t ${TASK_NAME}
```

成功后你应该看到类似：

```text
datasets/2026-challenge-task-instances/scenes/Beechwood_0_int/json/
├── Beechwood_0_int_task_boil_water_in_the_microwave_0_0_template-partial_rooms.json
└── Beechwood_0_int_task_boil_water_in_the_microwave_0_0_template.json
```

这一步才是你真正要的：

```text
稳定 task scene template
```

---

## Step 4：生成多个随机实例

官方第三步是用 `multiply_b1k_tasks.py` 生成实例，例如先生成 1 个实例。([行为][2])

我建议你一开始不要直接生成 300 个，先生成 5 个调试：

```bash
TASK_NAME=boil_water_in_the_microwave
SCENE_NAME=Beechwood_0_int

python OmniGibson/scripts/sampling/multiply_b1k_tasks.py \
  --partial_save \
  --start_idx 1 \
  --end_idx 5 \
  -t ${TASK_NAME} \
  -s ${SCENE_NAME}
```

生成后目录大概是：

```text
datasets/2026-challenge-task-instances/scenes/Beechwood_0_int/json/
└── Beechwood_0_int_task_boil_water_in_the_microwave_instances/
    ├── Beechwood_0_int_task_boil_water_in_the_microwave_0_1_template-tro_state.json
    ├── Beechwood_0_int_task_boil_water_in_the_microwave_0_2_template-tro_state.json
    └── ...
```

---

## Step 5：预采样机器人初始位姿

官方第四步是：

```bash
python OmniGibson/scripts/sampling/sample_robot_pose.py -t TASK_NAME
```

([行为][2])

你跑：

```bash
TASK_NAME=boil_water_in_the_microwave

python OmniGibson/scripts/sampling/sample_robot_pose.py \
  -t ${TASK_NAME}
```

这一步很重要。否则 task object 采样好了，但机器人可能：

```text
出生点太远
卡在家具里
无法导航到厨房
离 task-relevant object 太近或太远
```

---

## Step 6：注册新 task / 更新 metadata

官方第五步是：

```bash
python OmniGibson/scripts/sampling/extract_task_information.py
```

第六步是更新 `B100_task_misc.csv`，并填写 task relevant rooms。官方提醒，room 不只包括机器人和 TRO 所在房间，还要包括中间连接房间、走廊，以及机器人视野中可能出现的房间。([行为][2])

建议你对 `boil_water_in_the_microwave` 设置：

```text
task relevant rooms:
  kitchen_0
  dining_room_0
  corridor_0
```

具体要看 `Beechwood_0_int` 的房间布局。保守一点可以多包含连接房间，少包含可能导致加载时地板或墙缺失。

---

## Step 7：验证 task viability

官方第七步要求用 teleoperation 验证任务是否真的能完成，并检查是否存在不可导航、视觉 artifact、物体过高、机器人必须碰撞环境才能完成等问题。([行为][2])

你可以先不接 joylo，先做自动化 smoke test：

```bash
TASK_NAME=boil_water_in_the_microwave

python - <<'PY'
import omnigibson as og

cfg = {
    "scene": {
        "type": "InteractiveTraversableScene",
        "scene_model": "Beechwood_0_int",
    },
    "robots": [
        {
            "type": "Fetch",
            "obs_modalities": ["rgb"],
        }
    ],
    "task": {
        "type": "BehaviorTask",
        "activity_name": "boil_water_in_the_microwave",
        "activity_definition_id": 0,
        "activity_instance_id": 1,
        "online_object_sampling": False,
        "use_presampled_robot_pose": True,
    },
}

env = og.Environment(configs=cfg)
obs = env.reset()

for _ in range(100):
    env.step(env.action_space.sample())

print("Loaded and stepped successfully.")
og.shutdown()
PY
```

如果这一步都过不了，先不要进 eval。

---

# 3. 我建议你的目录组织方式

你可以把自定义 scene instance 当成一个独立数据包管理：

```text
custom_behavior_instances/
├── metadata/
│   ├── task_custom_lists.json
│   ├── available_tasks.yaml
│   └── B100_task_misc.csv
└── scenes/
    └── Beechwood_0_int/
        └── json/
            ├── Beechwood_0_int_task_boil_water_in_the_microwave_0_0_template.json
            ├── Beechwood_0_int_task_boil_water_in_the_microwave_0_0_template-partial_rooms.json
            └── Beechwood_0_int_task_boil_water_in_the_microwave_instances/
                ├── Beechwood_0_int_task_boil_water_in_the_microwave_0_1_template-tro_state.json
                ├── Beechwood_0_int_task_boil_water_in_the_microwave_0_2_template-tro_state.json
                └── ...
```

官方最后提交时需要包含 seed instance JSON、多个 task instance files，以及更新后的 `task_custom_lists.json`、`available_tasks.yaml`、`B100_task_misc.csv` 等文件。([行为][2])

你本地调试时，也应该保留这些文件，方便复现。

---

# 4. 推荐的“稳定生成策略”

不要一次只采一个，然后直接用。

建议这样做：

```bash
python OmniGibson/scripts/sampling/multiply_b1k_tasks.py \
  --partial_save \
  --start_idx 1 \
  --end_idx 20 \
  -t boil_water_in_the_microwave \
  -s Beechwood_0_int
```

然后筛选：

```text
1. 能 env.reset()
2. 能 step 100~300 步
3. object 没有掉落 / 穿模 / 爆炸
4. robot pose 合理
5. initial conditions 满足
6. goal checker 不报错
7. 关键物体在合理高度
8. microwave / sink / container 可交互
```

最后只保留稳定的几个 instance。

正式 eval 时用：

```yaml
online_object_sampling: false
use_presampled_robot_pose: true
activity_instance_id: 你筛选出的稳定 instance id
```

---

# 5. 不推荐的做法

不要这样：

```text
复制一个 Beechwood_0_int_task_xxx_template.json
手动把 task 名改掉
手动把 scene_model 改成另一个
手动改 object_registry 坐标
手动改 inst_to_name
```

原因是你上传的 JSON 里 `inst_to_name`、`system_registry`、`object_registry`、`init_info`、`objects_info` 是联动的。比如 `inst_to_name` 把 BDDL 符号对象绑定到具体对象名，`object_registry` 保存这些对象的实时位姿 / 速度 / 关节 / 状态，`init_info` 又指定了 `InteractiveTraversableScene` 和 `scene_model=Beechwood_0_int`。这些字段只改一部分，极容易出现对象找不到、谓词检查失败或物理状态不一致。

---

# 6. 最终方案总结

你的自定义 scene 生成流程应该是：

```text
选 task
  ↓
检查 BDDL
  ↓
选择 base scene，例如 Beechwood_0_int
  ↓
生成 task_custom_lists.json
  ↓
指定 whitelist / blacklist / room_types
  ↓
sample_b1k_tasks.py 生成 0_0_template
  ↓
multiply_b1k_tasks.py 生成多个 instance
  ↓
sample_robot_pose.py 预采样机器人位姿
  ↓
extract_task_information.py 注册任务信息
  ↓
加载 + reset + step + teleop / QA 验证
  ↓
筛选稳定 instance
  ↓
正式 eval 时关闭 online_object_sampling
```

最适合你现在的配置是：

```yaml
scene:
  type: InteractiveTraversableScene
  scene_model: Beechwood_0_int

task:
  type: BehaviorTask
  activity_name: boil_water_in_the_microwave
  activity_definition_id: 0
  activity_instance_id: 1
  online_object_sampling: false
  use_presampled_robot_pose: true
```

一句话：

> 这个官方页面是“创建稳定 task scene instance”的方法，不是从零创建 base scene 的完整资产管线。你现在应该先基于官方 base scene，用 task sampling 生成自己的 `template.json` 和 `tro_state.json`，通过验证后再用于 eval。

[1]: https://behavior.stanford.edu/behavior_components/scenes.html "Scenes - BEHAVIOR"
[2]: https://behavior.stanford.edu/behavior_components/task_sampling.html "Task Sampling - BEHAVIOR"