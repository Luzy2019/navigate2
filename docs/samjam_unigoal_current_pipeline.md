# SAMJAM-UniGoal 当前流程说明

本文档说明当前 `samjam_unigoal` backend 从相机观测到最终 v2 scene graph 的完整流程，并总结目前已经暴露的两个核心问题：

- 脏结点：同一物体被识别成多个不同 object，或者错误类别进入长期图。
- 关系混乱：raw relation 有输出，但 endpoint 不稳定、重复 object、历史关系累积导致最终关系不可用。

相关代码入口：

- `og_ego_prim/scene_graph/perception_scene_graph.py`
- `og_ego_prim/scene_graph/backends/samjam_unigoal.py`
- `og_ego_prim/scene_graph/backends/samjam_sam2.py`
- `og_ego_prim/scene_graph/perception.py`
- `og_ego_prim/scene_graph/schema.py`


## 0. 一句话总览

当前 `samjam_unigoal` 不是一个单独模型，而是一条组合链路：

```text
low level step callback
-> PerceptionSceneGraphUpdater.update()
-> observe(env) 得到 FrameObservation
-> SAMJAM: VLM 识别 bbox/name/relation + SAM2 生成 mask/track
-> bbox-mask match
-> adapter 过滤低质量 object/relation，稳定名称
-> UniGoal Graph.mapping3d() 用 depth 反投影到 3D 并做跨帧 object merge
-> 更新 node/edge/group
-> PerceptionSceneGraphUpdater 转成统一 v2 schema
-> scene_graph_report.json / diagnostic video / debug log
```

这里可以把职责拆成三层：

```text
SAMJAM 层：负责当前帧 2D 感知
Adapter 层：负责清洗、归一化、过滤、稳定 ID/name
UniGoal 层：负责长期 3D graph、跨帧同一物体识别、node/edge 更新
```


## 1. SAMJAM 的 VLM + SAM/SAM2

SAMJAM 当前主要做两件事。

第一，VLM 根据 prompt 看当前 RGB 图，输出：

- object name
- bbox
- relation
- 一些辅助描述，例如 moving/hand 等信息

第二，SAM2 对图像或视频帧生成 mask，并通过 video tracking 维护一些跨帧 mask track。

它们的输出不是最终 scene graph，只是 raw perception：

```text
VLM 输出：
  banana bbox=[...]
  tissue_box bbox=[...]
  banana on table

SAM2 输出：
  mask 1034
  mask 956
  mask 1215
  ...

SAMJAM raw object：
  samjam_object:1034
  samjam_object:956
  ...
```

注意：`samjam_object:*` 是 SAMJAM 层的临时 ID，不应该作为最终 scene graph 的 object id。最终 v2 graph 应该输出 `obj_0001`、`obj_0002` 这种 canonical id。

当前会保存的调试图包括：

- `frame_<n>_vlm_bbox.jpg`：VLM bbox 结果。
- `frame_<n>_full_masks.jpg`：SAM/SAM2 mask 候选。
- `frame_<n>_matched_masks.jpg`：bbox 和 mask 匹配后的 mask。
- `frame_<n>_matched_objs_rels.jpg`：SAMJAM raw objects/relations 可视化。
- `frame_<n>_bbox_mask_matches.jpg`：bbox + mask 双重匹配后、最终能进入 graph 的 object。

这些图用于排查 perception，不等价于最终 graph。


## 2. Bbox-Mask 匹配机制

VLM 给的是 bbox，SAM2 给的是 mask。要把两者合成一个 object，需要做 bbox-mask matching。

基本逻辑是：

```text
对每个 VLM object:
  找到和它 bbox 最匹配的 SAM mask
  计算 IoU
  记录 match_detail:
    vlm_name
    canonical_name
    best_native_id
    best_iou
    bbox
    mask bbox
```

然后在 `samjam_unigoal` adapter 中进一步过滤：

```text
必须 currently_visible=True
必须有 bbox
必须有 mask
必须有 match metadata
IoU 必须超过阈值
mask/bbox 不能明显过大
mask 区域 depth 必须足够有效
```

当前代码里 coarse/fine 阈值是分开的：

```text
coarse object 默认阈值：
  ISBENCH_SAMJAM_UNIGOAL_COARSE_MIN_MATCH_IOU = 0.25

fine object 默认阈值：
  ISBENCH_SAMJAM_UNIGOAL_FINE_MIN_MATCH_IOU = 0.05
```

coarse object 包括：

```text
table / cabinet / counter / door / stove / refrigerator / sink / bin / trash can ...
```

fine object 包括：

```text
apple / banana / tissue box / bottle / cup / bowl / pillow ...
```

一帧内还有一个重要约束：

```text
同一个 SAM mask 不能被多个 VLM object 同时认领。
如果多个 VLM bbox 都匹配到同一个 native mask，只保留 IoU 最高的那个。
```

这一步是为了避免：

```text
same mask -> banana
same mask -> bottle
same mask -> tissue_box
```

这种一块 mask 被多个名称反复认领的问题。

当前已经观察到的问题是，小物体的 bbox-mask IoU 经常偏低：

```text
banana:    VLM 看见了，但 bbox-mask IoU 经常只有 0.10~0.17
tissue box: IoU 经常只有 0.00~0.08
trash can: 有些帧 0.10~0.28，有些帧能到 0.8+
```

这说明“有没有被 VLM 看见”和“能不能通过 bbox-mask filter”是两件事。很多任务关键物体其实被 VLM 看见了，但没有进入 UniGoal mapping。


## 3. 构建 3D 场景点云

通过 adapter 过滤后的 object 会被送入 UniGoal：

```text
filtered PerceivedObject
-> _to_gobs()
-> UniGoal Graph.mapping3d()
```

输入里最重要的是：

- RGB
- depth
- camera intrinsics
- camera pose
- mask
- bbox
- object name

UniGoal 使用 depth 和相机参数把 2D mask 反投影到 3D 世界坐标：

```text
mask pixels + depth
-> camera space point cloud
-> world space point cloud
-> object 3D center / bbox / point cloud
```

然后用 3D 信息做跨帧匹配：

```text
新 detection
  和历史 map objects 计算 spatial similarity / overlap
  如果足够像，merge 到旧 object
  否则创建 new object
```

当前 `samjam_unigoal` 还在 UniGoal mapping 外面加了两层保护：

第一，semantic merge gate：

```text
如果 detection name 和 existing object name 不兼容，不允许 merge。
例如：
  stove -> coffee maker    禁止
  bottle -> table          禁止
  trash can -> counter     禁止
```

第二，track reassociation：

```text
同一个 samjam track 再次出现时，优先绑定回同一个 UniGoal map object。
```

这两层都是为了减少“同一物体重复建点”和“不同物体错误 merge”。


## 4. Coarse / Fine 结点定义

当前 node 会被分为 coarse 和 fine。

coarse node 表示大物体、支撑面、容器、房间结构或家具：

```text
table
counter
cabinet
drawer
sink
stove
refrigerator
door
trash can / bin
bed
shelf
```

fine node 表示可操作的小物体：

```text
apple
banana
tissue box
bottle
cup
bowl
knife
spoon
lighter
sponge
```

这个区分会影响几个地方。

第一，bbox-mask 匹配阈值：

```text
coarse object 通常更大，默认 IoU 阈值更高。
fine object 通常更小，默认 IoU 阈值更低。
```

第二，关系推断：

```text
fine object 更可能和 coarse object 构成：
  banana on counter
  tissue box in cabinet
  bottle on table

coarse object 之间更多是：
  cabinet near counter
  stove near counter
```

第三，edge 清理：

```text
移动 fine object 后，需要更严格清理它和旧 coarse parent 的关系。
例如 banana 从 table 放进 trash can：
  删除 banana on table
  更新 banana in trash can
```


## 5. Relation 的构建判断和更新机制

当前 relation 有三类来源。

第一类：SAMJAM raw relation。

VLM 直接输出：

```text
banana on table
tissue box on counter
cabinet next_to counter
```

进入最终 graph 前会过滤：

```text
source != target
source 和 target 都必须是通过 object filter 的当前帧 object
relation 必须能归一化到允许集合
重复 relation 删除
```

允许集合目前主要是：

```text
near
on
in
above
attach to
```

第二类：3D 几何推断的 fine-coarse relation。

当 fine object 和 coarse object 都是新鲜可见节点时，系统会根据 3D bbox/center/surface distance 推断：

```text
fine on coarse
fine in coarse
fine above coarse
fine attach to coarse
```

例如：

```text
banana + counter -> banana on counter
tissue box + cabinet -> tissue box in cabinet
```

第三类：near relation。

系统会对一些 coarse object 或 fine object 做几何距离判断，推断：

```text
cabinet near counter
bottle near tissue box
```

但是这类关系最容易噪声较大，所以需要限制：

```text
必须当前新鲜可见
最好同 room / group
不要对同名重复 node 大量生成 near
```

移动物体的关系更新机制如下：

```text
Executor / raw_plan 中出现：
  GRASP(x)
  PLACE_ON_TOP(x, y)
  PLACE_INSIDE(x, y)
  RELEASE(x)

PerceptionSceneGraphUpdater 解析 raw_plan
-> note_manipulation_event()
-> backend 尝试把 moved object 解析成 graph node uid
-> mark_manipulated_nodes([uid])
-> 下一次 update_structured_edges() 时严格更新
```

严格更新规则：

```text
如果 moved node 的旧 edge 另一端当前不可见：
  删除旧 edge

如果另一端可见，但当前帧没有这个 relation：
  删除旧 edge

如果当前帧有新的 relation：
  更新 edge relation
```

例子：

```text
原来：
  banana on table

执行：
  place_inside(banana, trash_can)

期望下一次图更新：
  删除 banana on table
  新增 banana in trash_can
```

目前这个机制本身是合理的，但它依赖一个前提：

```text
moved object 必须已经是 graph node，并且能解析到 uid。
```

如果 banana 一直没有进入最终 graph，那么移动物体 edge update 就无从触发。


## 6. 同一物体有多个名称，但属于同类，如何解决

这是 VLM 输出不稳定问题。例如：

```text
tissue box
tissue_box
tissue box box

banana
half_banana
half banana

stove
black stove
black slove
```

这类情况应该用两层处理。

第一层：prompt 约束。

在 SAMJAM 的 VLM prompt 里明确要求：

```text
优先从 canonical vocabulary 里选择名称。
不要创造新名字。
遇到别名时输出 canonical name。
```

例如 prompt 中写：

```text
Use "tissue box", not "tissue_box" or "tissue box box".
Use "banana", not "half_banana" or "half banana".
Use "stove", not "black slove" or "black stove".
Use "trash can" or "bin" consistently according to the vocabulary.
```

第二层：adapter 兜底归一化。

代码里维护 alias dictionary：

```text
half_banana        -> banana
half banana        -> banana
tissue_box         -> tissue box
tissue box box     -> tissue box
black slove        -> stove
black stove        -> stove
kitchen counter    -> counter
trash bin          -> bin
```

这种“prompt + adapter”的组合是必要的：

```text
prompt 让 VLM 尽量少犯错
adapter 保证即使 VLM 犯错，最终 schema 仍然统一
```

最终 graph 中应该只出现 canonical name：

```text
name: banana
label: banana_01
```

不要出现：

```text
half_banana_01
banana_01
half banana_01
```

同时，原始名称应该保留到 debug/caption/source_ids：

```text
vlm_raw_name: half_banana
canonical_name: banana
```


## 7. 同一物体被识别成不同种类名称，如何解决

这是更严重的问题。例如：

```text
同一个位置：
  第一次识别为 tissue box
  第二次识别为 coffee cabinet

同一个 track：
  trash_can -> counter
  bottle -> table
  sink -> stove
  stove -> counter
```

这不能只靠 alias dictionary 解决，因为它们不是同义词，而是类别冲突。

当前已经加入或需要强化的机制有三类。

### 7.1 Name vote

对同一个 SAMJAM track 或同一个 UniGoal object，累计多次名称投票：

```text
track A:
  banana: 0.42
  banana: 0.38
  bottle: 0.09

最终稳定名：
  banana
```

投票权重可以来自：

```text
bbox-mask IoU
detection confidence
mask quality
是否来自当前 visible object
是否属于 task vocabulary
```

### 7.2 Semantic compatibility gate

在 UniGoal merge 阶段，不能只看 3D overlap。

必须同时判断：

```text
detection canonical name
existing object canonical name
是否语义兼容
```

允许：

```text
half_banana -> banana
tissue_box -> tissue box
trash can -> bin
```

禁止：

```text
stove -> coffee maker
trash_can -> counter
bottle -> table
tissue box -> cabinet
```

如果不加这个 gate，即使两个点云位置很接近，也可能把不同类别错误 merge。

### 7.3 Track name protection

同一个 track 的稳定名不能被单帧异常结果轻易覆盖。

建议规则：

```text
如果 track 已经稳定为 trash can：
  单帧 counter 不能直接改成 counter
  除非连续多帧都是 counter，并且 bbox/mask/depth 质量明显更高

如果 track 已经稳定为 bottle：
  单帧 table 不能直接改成 table
```

也就是说，name vote 不应该只做“谁分数高谁赢”，还要做“语义类别是否允许切换”。

当前日志里已经出现过：

```text
trash_can stable=counter
bottle stable=table
sink stable=stove
stove stable=counter
```

这说明 name vote 还需要加更强的语义保护。


## 8. 已讨论问题一：脏结点

脏结点来源主要有四类。

第一，SAMJAM raw object 是累积式、视频式的。

它会保留很多历史 object：

```text
samjam_object:43 trash_can
samjam_object:225 trash_can
samjam_object:357 trash_can
samjam_object:398 trash_can
...
```

这些不应该全部进入最终 graph。

第二，VLM 命名不稳定。

同一个物体可能被命名为：

```text
banana / half_banana / bottle
tissue box / tissue_box / cabinet
trash can / bin / counter
```

第三，bbox-mask match 不稳定。

尤其是小物体：

```text
banana bbox 很小，SAM mask 可能偏移
tissue box 被大面积 table/counter mask 吞掉
```

第四，3D mapping merge 过松或过严都会出问题。

过松：

```text
不同类别因为位置接近被错误 merge。
```

过严：

```text
同一个物体跨帧重复建 object。
```

当前 adapter 的保守过滤已经减少了“最终 graph 爆炸”，但副作用是：

```text
任务关键物体经常进不了最终 graph。
```

例如某些 run 中最终 graph 只剩：

```text
counter
door
cabinet
```

而没有：

```text
banana
tissue box
trash can
```


## 9. 已讨论问题二：关系识别混乱

relation 混乱有两个层次。

### 9.1 Raw relation 混乱

SAMJAM raw relation 里会出现：

```text
A next_to A
A right_of A
counter on box
banana on table
old_object on old_surface
```

其中有些明显是 self relation，有些方向可能反了，有些 endpoint 是历史旧 object。

这些 raw relation 不能直接写入最终 graph。

### 9.2 Final relation 缺失

当前 adapter 已经严格过滤 relation：

```text
source 和 target 必须都通过 object filter
```

所以很多 raw relation 最后被拒绝：

```text
banana on table
-> banana 没通过 object filter
-> endpoint_filtered
-> final graph 没有这条 edge
```

这避免了脏关系污染 graph，但也导致最终 graph 经常 `edges=0`。

所以现在不是“relation 完全没识别”，而是：

```text
raw relation 有
但 endpoint 不稳定
最终 graph 留不住
```


## 10. 当前最需要改进的地方

### 10.1 小物体匹配不要只看 IoU

对 banana、tissue box、bottle 这类小物体，bbox-mask IoU 很容易低。

建议增加辅助判断：

```text
mask center 是否落在 bbox 附近
mask bbox 和 VLM bbox 是否有 containment
mask 是否主要位于 bbox 内部或附近
depth 是否合理
同一 task target 是否连续出现
```

不要全局降低 coarse/fine 阈值，否则脏 mask 会大量进入 graph。

### 10.2 task object rescue

如果当前 subtask 目标是：

```text
navigate_to(half_banana)
grasp(half_banana)
navigate_to(ashcan)
place_inside(ashcan)
```

那么 `banana` / `trash can` 应该获得更宽松但有条件的候选机制：

```text
低 IoU 但 VLM 连续多帧看见
depth 有效
3D 位置稳定
则进入 pending candidate

pending candidate 连续确认后再 promotion 成 node
```

### 10.3 Name vote 加语义保护

不能让单帧异常把稳定名改掉：

```text
trash_can -> counter
bottle -> table
```

建议增加：

```text
track 已稳定类别后，不允许跨 coarse/fine 或跨互斥类别直接改名
除非连续多帧强证据
```

### 10.4 Relation pending buffer

对于：

```text
banana on table
```

如果 banana 暂时没有 promoted，但已经是 pending candidate，可以先把 relation 放进 pending buffer。

当 banana 后续 promoted 后，再尝试落入 graph。

这比直接永久丢弃更稳。

### 10.5 Manipulation resolution 日志继续加强

移动物体关系更新依赖 uid：

```text
mark_manipulated_nodes([uid])
```

如果 `banana` 没有 node，就解析不到 uid。

需要在 report/debug 中明确记录：

```text
raw action: place_inside(half_banana, ashcan)
moved object: half_banana
resolved uid: None
reason: moved object not in graph
```

否则很难判断是 relation update 失败，还是 node 本身没构建出来。


## 11. 推荐的最终稳定策略

最终比较稳的策略应该是：

```text
1. VLM prompt 限制 canonical vocabulary
2. Adapter 做 alias 归一化
3. bbox-mask match 做质量过滤
4. 小物体增加非 IoU 辅助匹配
5. task target 增加 pending/rescue 机制
6. 同 track 名称投票，但加语义保护
7. UniGoal mapping3d 做 3D 跨帧 merge
8. semantic merge gate 防止不同类别错误 merge
9. relation 只在 endpoint 可靠后进入 graph
10. manipulation event 用 uid 严格更新移动物体旧关系
```

最终 graph 中应该满足：

```text
node id 统一：
  obj_0001
  obj_0002

name 统一：
  banana
  tissue box
  trash can

label 可区分同类实例：
  banana_01
  banana_02

source_ids 保存外部来源：
  samjam_object:1034
  unigoal object uid
  omnigibson object name

relation 只连接可靠 endpoint：
  banana in trash_can
  tissue_box in cabinet
```


## 12. 判断一次 run 是否健康

检查一次输出时，建议按这个顺序看。

第一，看 `scene_graph_report.json`：

```text
latest_summary.objects 是否合理
latest_summary.edges 是否合理
latest_scene_graph.rooms[].groups[].nodes 是否有任务物体
latest_scene_graph.rooms[].groups[].edges 是否有任务关系
```

第二，看 `scene_graph_debug.log`：

```text
[SAMJAM][bbox_mask_match]
  VLM 是否看见任务物体
  IoU 是多少

[SAMJAM-UniGoalFilter]
  kept_objects / rejected_objects
  任务物体被拒绝的 reason

[UniGoalMapping]
  mapping 是 merge 还是 new_object
  semantic_gate 是否挡掉错误 merge

[SAMJAM-UniGoalEdgeUpdate]
  manipulated edge 是否被删除/更新
```

第三，看可视化图：

```text
frame_<n>_vlm_bbox.jpg
  VLM 有没有框到物体

frame_<n>_full_masks.jpg
  SAM2 有没有生成合理 mask

frame_<n>_bbox_mask_matches.jpg
  最终进入 graph 的 bbox+mask 是否正确
```

第四，看 diagnostic video：

```text
当前帧 detection
前一帧 detection
BEV graph node
edge 是否和当前操作一致
```


## 13. 当前结论

当前 `samjam_unigoal` 的整体框架是对的：

```text
SAMJAM 做 2D perception
UniGoal 做 3D mapping 和长期 graph
IS-Bench schema 做最终统一输出
```

但目前最薄弱的地方在两个关口：

```text
VLM bbox 与 SAM2 mask 的匹配质量
名称稳定与 relation endpoint 稳定
```

所以后续优化优先级应该是：

```text
先解决 node identity 和 task object promotion
再解决 relation pending/update
最后再调更复杂的 room/group/BEV 可视化
```

如果 node 不稳定，relation 一定会混乱；如果任务物体进不了 graph，移动物体关系更新也无法生效。
