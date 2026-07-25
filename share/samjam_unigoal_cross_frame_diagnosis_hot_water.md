# SAMJAM-UniGoal 跨帧感知诊断报告

**任务**：`lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3`  
**场景**：`Beechwood_0_int`  
**诊断范围**：`samjam_unigoal` 的 RGB-D 感知、SAM2 native track、UniGoal 3D persistent graph。  
**不在本报告的验证范围内**：在线 task planner、risk predictor、replan / rethink、动作执行成功率。  
**证据运行**：`results/perception_gpt4o_shared_hot_water`（13 帧连续观测）。

> 证据目录位于本机 `results/`，且被 Git 忽略；本文中的图片相对链接在保留该目录的工作区中可直接打开，但不会随本 Markdown 一并提交。

---

## 1. 结论摘要

本次最可信的连续观测证据表明，当前问题不是“UniGoal 单独无法建图”，而是从单帧的 `VLM bbox -> SAM2 mask` 对齐开始就已不可靠；随后 native ID 连续性中断，UniGoal 只能在少数帧利用 3D 信息把新 native ID 合并回旧 persistent node，无法阻止长期图持续增生。

```text
VLM 输出两个目标 bbox
|
-> 与 SAM2 candidate mask 的几何匹配不可靠
   |
   -> 错误 mask 被赋予 samjam_object:<native_id>
   |
   -> 后续帧 native track 不连续
      |
      -> 新采样候选分配新 native ID
      |
      -> UniGoal 对一部分对象重新 merge
      |  另一部分对象继续 new_object
      |
      -> persistent scene graph 累积重复节点
```

在 `frame 2`，原始 RGB 图中确实可见两只桌上瓶子，VLM 也输出了两个 `water_bottle` bbox；但历史 matcher 接受的两个 SAM2 mask 都不包含对应 VLM bbox 的中心，右侧瓶子甚至对应一个覆盖大片背景的 mask。因此，问题并非可视化绘制偏移，而是上游 bbox-mask 匹配本身错误。

截至最后的 `frame 12`，SAMJAM native debug artifact 中累计 41 个对象，其中有 5 个 `water_bottle`、3 个 `bottle` 与 7 个 `table`；这不是 UniGoal persistent graph 的对象计数。同帧 UniGoal mapping 日志为 `objects_before=32 -> objects_after=33`，其中仍可见多条 `water bottle` / `bottle` 与 `table` 的重复 object history。两层输出都显示身份或类别未收敛，不能被视为稳定、正确的“双瓶 persistent scene graph”。

需要特别区分两个版本：上述 13 帧产物来自历史代码；当前源码已要求 bbox 中心落在 SAM mask 内才接受匹配。该规则能静态阻止历史 `frame 2` 的两种接受方式，但尚未用同一连续双瓶任务完成有效回归，因此不能宣称问题已经修复。

---

## 2. 范围、术语与证据边界

### 2.1 本报告回答的问题

```text
当前相机 RGB-D
|
-> SAM2 / VLM 是否能形成可信的当前帧 object
|
-> samjam_object:<native_id> 是否能跨帧连续
|
-> unigoal_object:<stable_id> 是否能将同一物体维护为一个 persistent node
|
-> 最终 SceneGraphSnapshot v2 是否会产生重复或错误节点
```

### 2.2 本报告不回答的问题

以下模块在 scene graph snapshot 构造完成之后才消费图，因此不能用本报告的运行结果验证：

```text
SceneGraphSnapshot v2
|
-> AgentRuntimeController.observe(snapshot)
   |
   -> ObjectRegistry / state diff / scheduler
   -> RiskPredictor
   -> Task planner
   -> Executor
```

证据运行的 `planner_source` 为 `actions_file`，runtime planner 为 `ScriptedPlanner`，risk provider 为 `RuleRiskProvider`，最终 `episode_task_success=false`、`num_task_successes=0`。日志只证明 scene-graph VLM 请求使用了 GPT-4o；它不能用于证明在线 VLM planner、VLM risk predictor、replan / rethink 或完整 benchmark 已成功。

### 2.3 重要术语

| 名称 | 含义 | 是否是真值任务实例 ID |
|---|---|---|
| `water_bottle.n.01_1` / `water_bottle.n.01_2` | BDDL 中的 ground-truth 实例 | 是；不能泄露给 VLM 感知或 planner |
| `samjam_object:<id>` | SAMJAM/SAM2 层的 native tracking ID | 否 |
| `unigoal_object:<id>` | UniGoal persistent 3D object 的稳定 ID | 否 |
| `obj_####` | SceneGraphSnapshot v2 的 canonical node ID | 否 |
| `bottle_01` / `bottle_02` | 根据 canonical uid 排序生成的显示 label | 否，不是 tracking 主键 |

当前 `SAMJAMUniGoalBackend` 没有实现 `set_task_entities()`；因此 updater 即使收到 task JSON 中的 exact entity ID，也不会把 `rag.n.01_2`、`water_bottle.n.01_1` 等真值实例 ID 注入该后端。这符合“不能让 VLM 预先知道应该选哪一个外观相同物体”的设计要求。

### 2.4 证据层级：native debug 与 persistent map 不能混用

```text
native_video_frames/ + vis_output/
|
-> 当前 RGB、VLM bbox、SAM2 masks、native matching 的可视化证据
|
-> scene_graph_output/<frame>_objs.json
   |
   -> SAMJAMOutputWriter 写出的 native debug artifact
   -> 累积 samjam_object:<native_id>
   -> 不能当作最终 persistent UniGoal scene graph
|
-> scene_graph_debug.log 的 [UniGoalMapping]
   |
   -> Graph.mapping3d() 的 objects_before / objects_after
   -> source samjam_object 与 unigoal_object 的 merge / new_object 决策
   -> 本报告中 persistent map 的主要证据
|
-> report.json
   |
   -> runner、planner、risk provider 与 benchmark outcome 的证据
```

因此，`frame 12` 的 41 个对象只证明 native tracking history 已大量累积；同帧 UniGoal mapping 的 33 个 map object 才是持久 3D map 的数量。二者均需单独报告，不能相互替代。

---

## 3. 入口与配置：什么情况下真正使用 SAMJAM-UniGoal

### 3.1 `eval_close.sh` 默认不是 SAMJAM-UniGoal

```text
entrypoints/eval_close.sh
|
-> python -m og_ego_prim.cli.online_benchmark_all
   |
   -> entrypoints/configs/eval_close.yaml
      |
      -> scene_graph.backend = omnigibson_truth
```

因此，直接运行 `eval_close.sh` 默认得到的是 truth scene graph，而不是 `samjam_unigoal`。如需用该入口测试感知 backend，配置中必须显式指定：

```yaml
scene_graph:
  backend: samjam_unigoal
```

### 3.2 `safe_memory` 的实际语义

```text
entrypoints/eval_safe_memory.sh
|
-> safe_memory_benchmark_all
   |
   -> safe_memory_benchmark_once --memory-mode with_memory
      |
      -> scene_graph_backend = samjam_unigoal
   |
   -> safe_memory_benchmark_once --memory-mode without_memory
      |
      -> scene_graph_backend = disabled
```

这里的 `with_memory` 实际上是“构建 persistent scene graph”；`without_memory` 是“不构建 scene graph，返回标记为 `perception_skipped=True` 的空 snapshot”。

还需注意：在在线 `model` 路径中，`with_memory` 还会安装 VLM risk provider。因此当前 `with_memory / without_memory` 不是严格只改变 scene graph 的纯消融；它同时改变了风险审查路径。`actions_file` 与 `example_planning` 模式不会创建在线 `AgentPlanner`。

---

## 4. 完整调用链

### 4.1 从 benchmark 入口到 persistent scene graph

```text
task JSON + scene name
|
-> build_benchmark(..., scene_graph_backend=samjam_unigoal)
|
-> OnlineBenchmark.__init__
   |
   -> 创建 PerceptionSceneGraphUpdater
   |  |
   |  -> 保存 task instruction / task categories
   |  -> task entity IDs 仅在 backend 支持 set_task_entities 时转发
   |
   -> PerceptionSceneGraphUpdater.reset(env)
      |
      -> SAMJAMUniGoalBackend.reset(env)
      |  |
      |  -> SAMJAMSAM2Backend.reset(env)
      |  |  |
      |  |  -> observation adapter
      |  |  -> SAM2 automatic-mask generator
      |  |  -> SAM2 video predictor
      |  |  -> VLM scene-graph adapter
      |  |
      |  -> SAMJAMUniGoalGraphAdapter.reset(...)
      |     |
      |     -> persistent UniGoal graph / object nodes / room / group
      |
      -> _run_perception(force=True)
         |
         -> observe(env)
         -> detect(frame)
         -> update_memory(result)
         -> SceneGraphSnapshot v2
```

### 4.2 单次真实 perception refresh

```text
PerceptionSceneGraphUpdater._run_perception()
|
-> backend.observe(env)
|  |
|  -> robot.get_obs()
|  -> 选择有 RGB 的 VisionSensor
|  -> RGB + depth / depth_linear + intrinsics + camera pose
|  -> FrameObservation(frame_index, rgb, depth, pose)
|
-> backend.detect(frame)
|  |
|  -> SAMJAMSAM2Backend.detect(frame)
|     |
|     -> 保存 native_video_frames/<frame>.jpg
|     -> SAM2 automatic masks -> mask candidates
|     -> VLM 当前 RGB -> objects(name, bbox, attributes) + relationships
|     -> VLM bbox <-> SAM2 mask matching
|     -> samjam_object:<native_id> + raw relations
|
-> backend.update_memory(raw_samjam_result)
|  |
|  -> SAMJAM-UniGoal input filter
|  -> mask + depth + intrinsics + pose -> Graph.mapping3d()
|  -> 3D spatial merge / new_object
|  -> unigoal_object:<stable_id>
|  -> persistent objects + rooms + groups + is_coarse
|
-> PerceptionSceneGraphUpdater._snapshot_from_result()
   |
   -> canonical uid
   -> obj_0001 / obj_0002 / ...
   -> SceneGraphNode + SceneGraphRoom + SceneGraphGroup
   -> SceneGraphSnapshot v2
```

当 `update_every` 没有命中时，updater 不会再次调用 SAM2 或 UniGoal，而是由 `latest_result` 生成一个 `skipped=True` 的 snapshot。高层动作完成后，`OnlineBenchmark._refresh_scene_graph()` 也可以触发新的 refresh；其动作参数只会通过 `set_object_goal()` 成为下一轮 VLM 感知的 object goal，不参与 native ID 分配或 UniGoal 3D merge。

### 4.3 native tracking 的传播与重采样

```text
frame 0
|
-> SAM2 candidates
-> _samjam_objects_from_candidates()
-> _match_samjam_objects()
-> matched objects = samjam_cur_objs

frame t (t > 0)
|
-> 仅从上一帧 samjam_cur_objs 做 SAM2 video propagation
|  -> propagated_objs
|
-> 从本帧 automatic masks 新采样候选
|  -> sampled_objs（每个候选赋新 native ID）
|
-> next_objs = propagated_objs + sampled_objs
|
-> VLM bbox 与 next_objs 竞争匹配
|  -> 同等可接受时优先 propagated native ID
|
-> 仅本帧成功匹配的 matched_objs 进入下一帧 samjam_cur_objs
```

因此，某个对象一旦在当前帧没有成功 bbox-mask match，即使它仍保留在 `samjam_total_objs` 历史中，也不会再作为下一帧 video propagation seed。新的 sampled candidate 随后会获得新的 native ID，这正是“native ID 增生”的关键机制。

### 4.4 UniGoal 的输入过滤与 3D 合并

```text
raw SAMJAM objects
|
-> _prepare_samjam_mapping_inputs()
   |
   -> 当前可见
   -> mask / bbox 存在
   -> match metadata 存在
   -> mask、bbox、depth 质量通过
   -> 名称 canonicalization
|
-> _to_gobs()
   |
   -> source_object_id = samjam_object:<native_id>
|
-> UniGoal Graph.mapping3d()
   |
   -> 2D mask + depth -> 3D point cloud / center / bbox
   -> semantic gate + spatial similarity
   -> merge 到已有 map object，或 new_object
|
-> stable ID: unigoal_object:<id>
```

UniGoal 的 persistent graph 本身是“场景记忆”：对象即使暂时不可见，仍可作为历史 node 保留。这个设计在输入稳定时是长期任务所需要的记忆；但当上游输入已经碎裂时，也会把重复和错误 node 持久化。

---

## 5. 历史连续运行与可视化证据

### 5.1 运行身份

| 项目 | 值 |
|---|---|
| 结果目录 | `results/perception_gpt4o_shared_hot_water` |
| 任务 | `data/tasks/composite/lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3.json` |
| 场景 | `Beechwood_0_int` |
| memory mode | `with_memory` |
| scene-graph VLM | GPT-4o（仅由 `run/console.log` 的 SAMJAM VLM request 证明） |
| 感知帧 | `0..12`，共 13 帧 |
| 调试日志 | `scene_graph_debug.log`，共 3222 行 |
| planner source | `actions_file`（ScriptedPlanner） |
| risk provider | `RuleRiskProvider` |
| benchmark 结果 | `episode_task_success=false`，`num_task_successes=0` |

每帧都保存了下列五类调试图：

```text
frame_<n>_vlm_bbox.jpg
frame_<n>_full_masks.jpg
frame_<n>_bbox_mask_matches.jpg
frame_<n>_matched_masks.jpg
frame_<n>_matched_objs_rels.jpg
```

### 5.2 关键图片索引

| 证据 | 文件 | 说明 |
|---|---|---|
| 原始双瓶观测 | [000002.jpg](../results/perception_gpt4o_shared_hot_water/native_video_frames/000002.jpg) | 图中可见桌上两只瓶子 |
| VLM 双瓶 bbox | [frame_2_vlm_bbox.jpg](../results/perception_gpt4o_shared_hot_water/vis_output/frame_2_vlm_bbox.jpg) | VLM 确实输出两个 `water_bottle` |
| bbox-mask overlay | [frame_2_bbox_mask_matches.jpg](../results/perception_gpt4o_shared_hot_water/vis_output/frame_2_bbox_mask_matches.jpg) | 两个 bbox 对应的 mask 明显不可信 |
| 最终 accepted masks | [frame_2_matched_masks.jpg](../results/perception_gpt4o_shared_hot_water/vis_output/frame_2_matched_masks.jpg) | 已进入 native graph 的 mask |
| 碎裂开始 | [frame_3_bbox_mask_matches.jpg](../results/perception_gpt4o_shared_hot_water/vis_output/frame_3_bbox_mask_matches.jpg) | 新 native object 出现 |
| 类别漂移 | [frame_5_bbox_mask_matches.jpg](../results/perception_gpt4o_shared_hot_water/vis_output/frame_5_bbox_mask_matches.jpg) | `water_bottle` 漂移为 `bottle` |
| 局部 merge | [frame_6_bbox_mask_matches.jpg](../results/perception_gpt4o_shared_hot_water/vis_output/frame_6_bbox_mask_matches.jpg) | 一部分对象能重新 merge，另一部分继续新建 |

这些 overlay 是直接用实际 VLM bbox 和实际 SAM2 mask 绘制，不能解释为“绘图函数把正确框画偏了”。

---

## 6. 帧级证据：双瓶如何从首帧失真到 native / persistent map 增生

### 6.1 `frame 2`：原始图和 VLM 都有双瓶，但 SAM2 mask 不正确

该帧的 VLM 输出为一个 `table` 与两个 `water_bottle`：

```text
table:
  bbox = [25.6, 64.0, 230.4, 128.0]

water_bottle A:
  VLM bbox = [76.8, 76.8, 102.4, 102.4]
  native ID = 208
  SAM bbox = [98.0, 62.0, 107.0, 100.0]
  mask area = 286
  IoU = 0.1140
  center_in_mask = false

water_bottle B:
  VLM bbox = [153.6, 76.8, 179.2, 102.4]
  native ID = 227
  SAM bbox = [122.0, 0.0, 255.0, 90.0]
  mask area = 6325
  IoU = 0.0275
  center_in_mask = false
  历史接受原因 = vlm_coverage (0.5156)
```

`208` 对应的 mask 很小且偏离 VLM 左瓶区域；`227` 对应的 mask 覆盖了很大一片区域，并不是右侧单个瓶子的实例 mask。尽管如此，历史 matcher 仍让它们进入 native graph：

```text
samjam_object:208 -[on]-> samjam_object:187(table)
samjam_object:227 -[on]-> samjam_object:187(table)
samjam_object:208 -[beside]-> samjam_object:227

samjam_object:208 -> unigoal_object:8  (new_object)
samjam_object:227 -> unigoal_object:9  (new_object)
```

这一步已经说明：首要问题不是 3D mapping，而是 “VLM 检测到两个类别” 没有转化为 “两个几何上可信的实例”。

### 6.2 `frame 3`：native ID 切换，UniGoal 创建新的 map object

在后一帧中：

```text
旧对象：
  208 -> currently_visible，但因 missing_match_metadata 被 UniGoal filter 拒绝
  227 -> not_currently_visible，被 UniGoal filter 拒绝

进入本帧 mapping 的新 source：
  243 -> unigoal_object:10 (new_object)
  298 -> unigoal_object:11 (new_object)
```

`243`、`298` 都通过了该帧 filter，并被日志直接记录为 `new_object`；后续 mapping history 中保留了 `unigoal_object:8`、`:9`、`:10`、`:11` 四条 `water bottle` 记录。这里证明的是新 native source ID 进入 mapping、旧 source 未更新、persistent map history 均发生增生；由于日志没有候选来源，不能进一步断言 `243` / `298` 一定来自 propagation 失败或重新采样替代。也不将 `scene_graph_output/*.json` 的 native 数量误写成 persistent map object 数量。

### 6.3 `frame 5` / `frame 6`：类别漂移进一步削弱合并

```text
frame 5
|
-> VLM 类别从 water_bottle 漂移为 bottle
-> samjam_object:430 -> unigoal_object:16 (new_object)
-> samjam_object:481 -> unigoal_object:17 (new_object)

frame 6
|
-> samjam_object:430 -> merge 到 unigoal_object:16
-> samjam_object:593 -> unigoal_object:18 (new_object)
```

`430` 的 merge 证明 UniGoal 的 3D 合并在局部连续视角能够工作。历史产物中类别由 `water bottle` 变为 `bottle`，会造成当时 semantic gate 的不一致；当前源码已将 `water_bottle` / `water bottle` 归一化为 `bottle`，但尚未在该连续双瓶任务完成回归验证。

### 6.4 后续帧：UniGoal 能偶发恢复，但无法收敛全局图

在后续映射中，新 native ID `890` 被合并回早期的 `unigoal_object:9`，其 source history 同时包含 `samjam_object:227` 和 `samjam_object:890`：

```text
samjam_object:890
|
-> UniGoal action=merge
-> matched unigoal_object:9
-> historical source: samjam_object:227
```

这证明 UniGoal 并非完全失效：它有时能跨 native ID 将同一类、相近 3D 位置的对象重新关联。但这种偶发恢复不足以删除已经新建的重复 map object；到 `frame 12`，mapping 日志仍为 `32 -> 33`，并保留 4 条 `water bottle` history、3 条 `bottle` history 及多条 `table` history，因此 persistent map 仍未收敛。

---

## 7. 诊断结论：问题分别出现在哪一层

### 7.1 单帧实例几何问题：已确认

```text
原始 RGB 中有双瓶
|
-> VLM 输出双 bottle bbox
|
-> 选中的 SAM2 mask 与 bbox 不对齐
|
-> 错误 mask 进入 native object
```

这不是“VLM 完全没有识别物体”，也不是“绘图错误”。VLM 的类别识别和数量识别可以成功，但缺少可用于 3D mapping 的可靠实例 mask。

### 7.2 native track 连续性问题：已确认

`samjam_cur_objs` 只保留当前帧成功匹配的对象，并作为下一帧 SAM2 video propagation 的 seed。匹配失败、不可见、或没有必要 metadata 的历史 object 不再进入下一帧 seed 集；新采样 mask 则生成新 native ID。历史证据中的：

```text
208 / 227
|
-> 243 / 298
|
-> 430 / 481 / 593
|
-> 890
```

与此代码路径和现象一致。但历史日志没有记录每个候选来自 propagation 还是 re-sampling，因此不能将每次 ID 切换严格归因于“propagation 失败”或“sampled candidate 竞争胜出”。

### 7.3 UniGoal input filter：放大了小物体断裂，但不是原始根因

filter 会拒绝：

```text
not_currently_visible
missing_match_metadata
缺少 mask / bbox
低质量 mask、bbox、depth
```

这本来是防止错误 object 污染长期图的必要保护；但在小而相邻的瓶子已经不稳定时，会造成“旧 node 不更新、新 node 又产生”的结果。它不是 `frame 2` 错误 mask 的原因，而是随后碎裂被持久化的放大因素。

### 7.4 UniGoal 3D merge：部分有效，但不足以修复上游错误

已观察到：

```text
430 -> unigoal_object:16  (同 native ID 的连续 merge)
890 -> unigoal_object:9   (跨 native ID 的恢复 merge)
```

因此不能把全部问题归结为 UniGoal 的 3D mapping。它能工作，但它接收的是已经出现几何错误、tracking 断裂和语义漂移的输入。

### 7.5 persistent graph retention：正确机制在错误输入下成为累积器

```text
正确输入
|
-> 不可见对象保留为历史节点
-> long-life memory 可用

错误 / 碎裂输入
|
-> 重复对象也被长期保留
-> 多个 table / bottle / water_bottle 持续累积
```

因此，“persistent scene graph 作为 memory”本身仍是合理架构；需要修复的是进入图的观测质量和身份连续性，而不是简单删除 persistence。

---

## 8. 已经尝试过的方向及其结论

| 尝试 | 观察到的结果 | 结论 |
|---|---|---|
| 使用 256x256 感知图像 | 降低显存和调用成本；双瓶在图中占很小面积 | 对小而相邻实例更敏感，不能单独解决匹配问题 |
| 覆盖率 fallback | 历史 `frame 2` 右瓶以 `vlm_coverage=0.5156` 接受 | 会让大背景 mask 冒充瓶子，不适合作为唯一接受依据 |
| bbox 中心必须落在 mask | 当前源码已有该条件 | 静态阻止历史两种错误接受；尚未在连续双瓶任务回归验证 |
| 单帧 box-prompt 检查 | VLM 有双 bbox，但仅一瓶匹配，且 mask area 仅 13 | box prompt 本身不能稳定得到两个可靠实例 mask |
| UniGoal 3D merge | 有 `430 -> 16`、`890 -> 9` 的成功例子 | 只能部分恢复，不足以消除已建立的重复 map object |
| 调整单个 object 的阈值 | 可能改善某一帧、某一物体 | 不应针对一个瓶子无限微调，容易损害其他 task-relevant object |

此外，历史过程中曾有 CUDA/NVML 可见性不一致以及 PhysX CUDA 内存分配失败。它们会中断连续仿真运行，但不是上述已经落盘的 bbox-mask 错误和 native/persistent node 增生的解释。

`robot_gripper` 曾作为视觉 object 出现在历史输出中，会增加候选与语义噪声；但双瓶问题在它之外仍然存在，不能将其视为根因。

---

## 9. 历史产物与当前代码的版本差异

历史运行中，两个 `center_in_mask=false` 的 bottle match 仍然被接受。当前 `_bbox_match_decision()` 的逻辑不同：

```text
当前源码：
|
-> center_in_sam_mask 为 false
   |
   -> 不允许通过 IoU / mask_coverage / vlm_coverage / center fallback
```

该中心点约束是在 2026-07-25 的提交 `bf8ba64` 中加入；用于本报告的连续双瓶运行早于此修改。因此应按以下方式解读：

```text
历史产物
|
-> 证明旧 matcher 能把错误大 mask 接受为 bottle

当前源码
|
-> 静态上已排除那种 center 不在 mask 的接受
|
-> 但尚无同条件连续 hot-water v3 回归
   |
   -> 不能声称“当前版本已修复 native tracking”
```

这也是当前最重要的验证缺口。

---

## 10. 已确认、尚未确认与不能推断的结论

### 10.1 已确认

```text
1. 正确的 hot-water v3 场景中，原始图可见两只桌上瓶子。
2. VLM 可以在同一帧输出两个 water_bottle bbox。
3. 历史 SAM2 bbox-mask matching 对这两个 bottle 均发生错误接受。
4. native ID 在连续帧中出现明显增生；`frame 12` native debug artifact 为 41 个累计对象。
5. UniGoal persistent map 同样增生；`frame 12` 的 mapping 日志为 32 个 map object 增至 33 个，且保留多条 bottle / table history。
6. UniGoal 有时能够对新 native ID 做 3D merge。
7. 当前历史 persistent graph 不能视为稳定、正确的双瓶图。
8. planner / risk 不参与 SAM2 native ID 分配和 UniGoal mapping。
9. SAMJAM-UniGoal backend 未接收 BDDL exact instance ID，因此不会借真值区分两个瓶子。
```

### 10.2 尚未确认

```text
1. native ID 每次断裂究竟是：
   - SAM2 video propagation 没有产出目标 mask；
   - propagation mask 存在，但新 sampled candidate 被匹配器选中；
   - 两者组合。

2. 当前加入 center-in-mask 约束后：
   - 两瓶是否都能稳定进入 native graph；
   - 是否会因更严格拒绝而完全漏检；
   - UniGoal 是否能在连续帧稳定维护两个 persistent node。

3. 208 / 227 各自对应 BDDL 中哪一个 water_bottle.n.01_x。
   本系统不应该从 scene graph 结果推断或泄露这一 ground truth 身份。

4. 哪一组 SAM2 或 matcher 参数可同时泛化到所有 task-relevant object。

5. 在正确稳定 scene graph 的前提下，risk predictor 与在线 task planner 是否能完整完成任务。
```

---

## 11. 下一步应采用的最小回归实验

在继续调 planner、risk 或 benchmark action sequence 之前，应先完成独立的“连续观测 -> native track -> UniGoal mapping”回归。实验不应启动 planner、risk review 或动作执行循环。

```text
固定 hot_water_container_fragile_vase_v3 + Beechwood_0_int
|
-> 使用当前 matcher（含 center-in-mask 约束）
|
-> 连续采集相同观测路径的 RGB-D
|
-> 每帧运行：SAM2 -> VLM bbox -> native matching -> UniGoal mapping
|
-> 仅审计以下字段：
   |
   -> VLM bbox
   -> selected mask / match metrics
   -> native ID
   -> candidate origin（propagated 或 sampled）
   -> source samjam_object ID
   -> stable unigoal_object ID
   -> filter rejection reason
|
-> 判定两个视觉 bottle 是否在连续 N 帧内保持：
   |
   -> 不泄露 BDDL instance ID
   -> 2 个可信 native object
   -> 至多 2 个对应 persistent bottle node
   -> 无背景大 mask 被接受
```

这里最有价值、但历史日志缺失的信息是：

```text
propagated_native_ids
sampled_native_ids
每个 VLM bbox 最终选中候选的 origin
未选中的传播候选及其 match metrics
```

只有拿到这些数据，才能将根因从“native 层已碎裂”进一步区分为“propagation 丢失”或“重新采样替换”。在此之前，继续针对某一个物体反复调 IoU、mask 面积或阈值会陷入局部优化循环，不能证明对其他任务相关 object 有泛化效果。

---

## 12. 代码与产物索引

| 目的 | 位置 |
|---|---|
| entrypoint | `entrypoints/eval_close.sh`、`entrypoints/eval_safe_memory.sh` |
| 默认 truth backend 配置 | `entrypoints/configs/eval_close.yaml` |
| `with_memory / without_memory` backend 选择 | `og_ego_prim/cli/safe_memory_benchmark_once.py` |
| 上层刷新与 snapshot 构造 | `og_ego_prim/scene_graph/perception_scene_graph.py` |
| VLM bbox、SAM2 mask、native ID、video propagation | `og_ego_prim/scene_graph/backends/samjam_sam2.py` |
| SAMJAM filter、UniGoal 3D mapping、persistent graph | `og_ego_prim/scene_graph/backends/samjam_unigoal.py` |
| 原始连续运行日志 | `results/perception_gpt4o_shared_hot_water/scene_graph_debug.log` |
| native debug JSON（不是 persistent graph） | `results/perception_gpt4o_shared_hot_water/scene_graph_output/` |
| 每帧 VLM / mask / relation 图 | `results/perception_gpt4o_shared_hot_water/vis_output/` |
| 原始 RGB 帧 | `results/perception_gpt4o_shared_hot_water/native_video_frames/` |
| benchmark report | `results/perception_gpt4o_shared_hot_water/run/safe_memory_benchmark/.../report.json` |

---

## 13. 最终判断

目前可以明确说：`samjam_unigoal` 已能形成 persistent graph，也能在部分情况下完成 3D merge；但在两个相邻、小尺寸、外观相似瓶子的连续观测中，输入侧的 bbox-mask 对齐和 native tracking 还不稳定，导致 persistent scene graph 增生。

因此，后续工作优先级应为：

```text
先证明当前 matcher 下的连续 native track 是否稳定
|
-> 再证明 UniGoal 对两个视觉实例的 persistent mapping 是否稳定
|
-> 再让 risk predictor 消费可信 scene graph
|
-> 最后评估在线 planner / replan / rethink 的端到端成功率
```

在前两步完成前，不应把 task planner 的 action sequence 错误、risk predictor 判断或 benchmark 失败归因给 scene graph 以外的模块，也不应将当前历史产物描述为“完整流程已经运行成功”。
