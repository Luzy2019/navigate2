# Dynamic Scene Graph Update — Design

> 目标：在 IS-Bench 在线 benchmark 执行过程中，以 SAMJAM 的 video SGG（VLM + SAM2 跨帧 mask 跟踪）为主感知链路，结合 UniGoal 的层次化（房间→分组→物体）持久记忆思想，构建一个**每 N 步动态更新**的统一场景图，对外只暴露一个 `SceneGraphSnapshot` 供 LLM 规划与评估消费。
>
> 本文是 `docs/unigoal_samjam_integration_sketch.md` Phase 2 的具体落地设计，不重复其已确立的三层分离 / 数据所有权 / 规范 ID 原则，只聚焦"动态更新"本身。

## 1. 需求与约束（来自对齐）

| 维度 | 决策 |
|------|------|
| 触发策略 | 步长超参节流：设 `N`（如 20），每 `N` 个底层仿真步触发一次更新（复用现有 `scene_graph_step_interval`） |
| 主感知后端 | **SAMJAM video SGG 在线化**（当前视觉构建/更新 graph）。不主要用 UniGoal（其依赖先验知识 / Grounded-SAM 重型链路） |
| 保留思想 | UniGoal 的 **group / hierarchical** 思想（房间→分组→物体）必须保留 |
| 记忆融合 | **统一持久记忆**：感知结果合并进单一记忆层，对外只暴露一个 Snapshot |
| 评估真值 | 评估器仍以 OmniGibson 仿真器状态为准；感知图只用于规划上下文，不污染评估（沿用 sketch §6） |
| 规范 ID | 节点 ID 优先映射到 `env.task.object_scope` 的任务对象名；SAMJAM track id 仅作感知别名（沿用 sketch §6.1） |
| 首要交付 | 先出本设计方案，评审后再写代码 |

## 2. 整体架构

```text
                         每 N 步触发 (scene_graph_step_interval)
                                     │
                                     ▼
        ┌────────────────────────────────────────────────────────┐
        │            DynamicSceneGraphUpdater                     │
        │  (og_ego_prim/scene_graph/dynamic_scene_graph.py)       │
        │                                                          │
        │  update(context) ──► backend.observe(env)  ──┐          │
        │                    backend.detect(frame)    ──┤          │
        │                                                 ▼          │
        │                          ┌─────────────────────────────┐ │
        │                          │ HierarchicalSceneGraphMemory │ │
        │                          │ (统一持久记忆)               │ │
        │                          │  objects / rooms / groups    │ │
        │                          │  trajectory / visited_cells  │ │
        │                          │  merge() + cluster()         │ │
        │                          └──────────────┬──────────────┘ │
        │                                         ▼                 │
        │                                 SceneGraphSnapshot        │
        └─────────────────────────────────────────┬────────────────┘
                                                  ▼
                                    tracker.track_scene_graph()
                                  snapshot.to_prompt_context()  → LLM
```

数据流：

```
env.step(action)
  → Executor._execute: step_callback(LowLevelStepContext)  [每 N 步]
    → OnlineBenchmark._on_low_level_step(context)
      → DynamicSceneGraphUpdater.update(context)
        → OnlineSAMJAMBackend.observe(env)        # RGB-D + pose (ISBenchObservationAdapter)
        → OnlineSAMJAMBackend.detect(frame)       # VLM 检测 + SAM2 跨帧跟踪 + IoU 匹配
        → HierarchicalSceneGraphMemory.update(result, robot_pos, context)
              # 合并持久物体 / 分配房间 / 聚类分组 / 构建 near,in_room,in_group + 关系边
        → SceneGraphSnapshot
      → tracker.track_scene_graph(snapshot)
```

## 3. 组件设计

### 3.1 在线 SAMJAM 感知后端 `OnlineSAMJAMBackend`

**文件**：`og_ego_prim/scene_graph/backends/online_samjam.py`（重写 `samjam_sam2.py` 为在线模式，旧文件保留作离线参考）

**实现 `perception.py` 的 `PerceptionBackend` Protocol**：`reset / observe / detect / update_memory / set_object_goal`。

**核心改造：SAMJAM 从"离线视频目录"→"在线逐帧"**

SAMJAM 原流水线（`vidsgg.py`）吃一个帧目录，首帧 `generate_first_frame()`（VLM 检测 + SAM2 自动分割 + IoU 匹配），后续帧 `generate_next_frames()`（SAM2 video predictor `propagate_in_video` 全量前向传播 + 新物体检测 + 再匹配）。难点是 SAM2 video predictor 的 `init_state` 设计为一次性载入整段视频。

**在线化策略：滚动窗口 + 增量传播**

- 维护 SAM2 video predictor 的 `inference_state` 与一个**滚动帧缓冲**（长度上限 `W`，默认 16）。
- **首帧更新**：VLM 在当前 rgb 上出场景图 → `SAM2AutomaticMaskGenerator` 生成候选 mask → IoU 匹配 VLM bbox↔mask → 初始化 `objs: Dict[int, Object]`（复用 SAMJAM `Object`，含 `frames={fid:{seg,bbox}}`、`is_hand`、`is_moved`）→ 用匹配到的 mask `add_new_mask` 进 inference_state。
- **后续更新**（每次 `detect` 被调用）：
  1. 把新 rgb 追加进滚动缓冲；若超出 `W`，则**滑窗重置**：`reset_state` → 用最近 `W` 帧 re-init → 把上一轮已知物体的 mask 作为锚点 `add_new_mask` 在窗口起点。
  2. `propagate_in_video` 前向到当前帧，得到已跟踪物体的新 mask。
  3. VLM 在当前 rgb 出场景图（objects + relationships）。
  4. IoU 匹配 VLM bbox ↔ 已跟踪 mask；低重叠的新检测 → `AutomaticMaskGenerator` 补 mask → 新建 `Object`。
  5. 更新 `objs`：追加 `frames[fid]`；若 VLM 标 `is_moving` 则置 `is_moved=True` 并清旧关系。
  6. 更新 `rels: Dict["src,dst", predicate]`。
- **3D 定位**：用 `backends/utils.py:mask_center_world(mask, depth, intrinsics, camera_pose)` 把每个物体 mask 反投影到世界坐标 → `position`。
- **房间归属**：`room_lookup_from_env(env)`（基于 `scene.seg_map`）按物体世界坐标查房间 → `room_id`。

**VLM 客户端**：复用 `vendor/samjam/vlms/`（gpt/qwen/gemini），但**必须删除 `samjam_sam2.py` 顶部硬编码的 `OPENAI_BASE_KEY`**，改为从环境变量读取（`ISBENCH_SCENE_GRAPH_LLM_API_KEY` / `_BASE_URL` / `_VLM_MODEL`，与 unigoal 后端一致）。VLM 输出 schema 不变：

```json
{"objects":[{"id":1,"name":"apple","bbox":[ymin,xmin,ymax,xmax],"is_hand":false,"is_moving":false}],
 "relationships":[{"subj_id":1,"obj_id":2,"predicate":"on_top_of"}]}
```

**节流内的二次节流（可选）**：当 SAM2 传播已稳定跟踪所有已知物体、且本轮新 mask 占比低于阈值时，可跳过 VLM 调用以降本（环境变量 `ISBENCH_SAMJAM_SKIP_VLM_NEW_MASK_RATIO`，默认关闭）。

**输出**：`PerceptionResult`，其中 `objects: List[PerceivedObject]`（含 `object_id`=SAMJAM track id 字符串、`name`、`bbox`、`mask`、`position`、`room_id`、`confidence`、`attributes={is_hand,is_moved,is_moving,seen_count}`），`relations: List[PerceivedRelation]`。

### 3.2 统一持久记忆 `HierarchicalSceneGraphMemory`

**文件**：`og_ego_prim/scene_graph/hierarchical_memory.py`

这是 UniGoal 层次化思想的落地，但**由感知结果驱动**（而非 GT）。实际上就是把 `unigoal_memory_scene_graph.py` 里 `UniGoalMemorySceneGraphUpdater` 的记忆核心抽出，改成吃 `PerceptionResult` 而非 `OmniGibsonSceneGraphUpdater` 的真值快照。

**数据结构**（沿用现有命名，便于复用）：

```python
@dataclass
class MemoryObject:
    object_id: str          # 规范 ID（见 §4 ID 解析）
    track_id: Optional[str] # SAMJAM track id（感知别名）
    name: str
    category: str
    first_seen_step: int
    last_seen_step: int
    seen_count: int
    position: Optional[List[float]]
    orientation: Optional[List[float]]
    room_id: str
    states: Dict[str, Any]  # is_hand,is_moved,is_moving,confidence,...
    currently_observed: bool
    distance_to_robot: Optional[float]
    bbox: Optional[List[float]]
    # 当前的 2D mask 引用（供可视化/调试）

@dataclass
class MemoryRoom:
    room_id: str
    object_ids: Set[str]
    group_ids: Set[str]

@dataclass
class MemoryGroup:
    group_id: str
    room_id: str
    object_ids: List[str]
    center: Optional[List[float]]
    center_object_id: Optional[str]
    caption: str
```

并维护 `visited_cells: Set[Tuple[int,int]]`、`trajectory: List[List[float]]`（机器人位姿轨迹）。

**核心方法 `update(result, robot_position, context) -> SceneGraphSnapshot`**：

1. **合并物体**（感知别名 → 规范 ID）：
   - 先把所有 `MemoryObject.currently_observed=False`、`distance_to_robot=None`。
   - 对每个 `PerceivedObject`，按 §4 解析规范 ID；在 `objects` 中查找同规范 ID（或同 track_id）的记忆项：
     - 命中 → 更新 `position/room_id/states/last_seen_step/seen_count++/currently_observed=True/distance`。
     - 未命中 → 新建 `MemoryObject(first_seen_step=...)`。
   - **空间去重兜底**：若感知未给出规范 ID（纯感知新物体），则按 `(name, 与已有同 name 物体距离 < merge_distance)` 合并，避免同一物体被反复新建。
2. **更新地图记忆**：`trajectory.append(robot_position)`、`visited_cells.add(cell)`（cell = `robot_pos / map_cell_size`）。
3. **重建房间**：按 `MemoryObject.room_id` 聚合 → `MemoryRoom`。
4. **重建分组**：每间房内按 `group_distance_threshold`（默认 1.5m）连通分量聚类（复用 `UniGoalMemorySceneGraphUpdater._cluster_room_objects` 逻辑）→ `MemoryGroup`，算中心 + 中心物体 + caption。
5. **构建节点**：物体节点（带 room_id/first_seen/last_seen/seen_count/currently_observed/distance 等状态）+ 房间节点 + 分组节点。
6. **构建边**：
   - `near`：同组内两两距离 ≤ `group_distance_threshold`（confidence 随距离衰减）。
   - `in_room`：物体→房间。
   - `in_group`：物体→分组。
   - 感知关系：`result.relations` 映射到规范 ID 后加入（`source="samjam_vlm"`）。
7. **去重边**并返回 `SceneGraphSnapshot`（`metadata` 含 `source="dynamic_perception"`、`perception_backend="samjam_online"`、room/group/map 序列化、感知错误等）。

**与真值记忆的关系**：把 `UniGoalMemorySceneGraphUpdater` 重构为"GT 感知器 + 同一套 `HierarchicalSceneGraphMemory`"的薄包装，确保两条链路共享同一记忆/聚类/序列化实现，仅输入来源不同。这样 `omnigibson_truth` 与 `samjam_online` 行为一致、可对比。

### 3.3 动态更新器 `DynamicSceneGraphUpdater`

**文件**：`og_ego_prim/scene_graph/dynamic_scene_graph.py`

```python
class DynamicSceneGraphUpdater(SceneGraphUpdater):
    def __init__(self, backend_name="samjam_online", sensor_name=None,
                 group_distance_threshold=1.5, map_cell_size=0.25, ...):
        self.backend = build_perception_backend(backend_name, sensor_name)  # OnlineSAMJAMBackend
        self.memory = HierarchicalSceneGraphMemory(group_distance_threshold, map_cell_size, ...)
        ...

    def reset(self, env):
        self.backend.reset(env)
        self.memory.reset()
        frame = self.backend.observe(env)
        result = self.backend.detect(frame)
        return self.memory.update(result, robot_position=..., context=None)

    def update(self, context=None) -> SceneGraphSnapshot:
        target = _target_from_raw_plan(context.raw_plan if context else None)
        if target: self.backend.set_object_goal(target)
        frame = self.backend.observe(env)
        result = self.backend.detect(frame)        # 内部已做跨帧跟踪与持久化
        result = self.backend.update_memory(result)
        robot_pos = _robot_position(env)
        return self.memory.update(result, robot_pos, context)
```

> 节流（每 N 步）由 `OnlineBenchmark._on_low_level_step` 的 `scene_graph_step_interval` 负责，updater 本身每次被调用都执行——保持单一节流来源，避免双重节流歧义。`PerceptionSceneGraphUpdater` 内的 `update_every` 在此模式下设为 1（不二次节流）或直接由 `DynamicSceneGraphUpdater` 取代。

### 3.4 触发与装配（benchmark / factory）

- **factory**：`build_perception_backend` 新增 `"samjam_online"` → `OnlineSAMJAMBackend`；保留 `"samjam_sam2"`（离线）与 `"unigoal_grounded_sam"`。
- **PerceptionSceneGraphUpdater**：当 `backend_name in {"samjam_online"}` 时，改用 `DynamicSceneGraphUpdater` 路径（或在 `perception_scene_graph.py` 里把 `samjam_online` 路由到 `DynamicSceneGraphUpdater`，复用其 `_target_from_raw_plan`）。最简洁做法：`PerceptionSceneGraphUpdater.__init__` 增加分支——`samjam_online` → 持有 `OnlineSAMJAMBackend` + `HierarchicalSceneGraphMemory`，`update()` 走 §3.3 流程。
- **OnlineBenchmark**：`scene_graph_step_interval` 即"N"超参（CLI `--scene_graph_step_interval 20`）。默认后端可保持 `omnigibson_truth`，用 `--scene_graph_backend samjam_online` 切到动态感知模式。装配点不变（`_on_low_level_step` / `_refresh_scene_graph`）。
- **导航衔接（可选增强）**：当前触发仅靠步长节流。若后续要在导航航点边界也触发，给 `NavigationBackend.navigate_to_object` 加可选 `waypoint_callback`，在 `_execute` 的 step 循环里由 Executor 转发——但本期不做，先用步长节流验证。

## 4. 物体 ID 解析（感知别名 → 规范 ID）

这是 sketch §6.1 的落地，决定记忆能否正确持久化。

- **规范 ID 来源**：`env.task.object_scope` 的 key（如 `apple.n.01_1`）。
- **解析流程**（在 `HierarchicalSceneGraphMemory.update` 内，或独立 `resolver`）：
  1. 对每个 `PerceivedObject`，取其 VLM `name`（如 "apple"）。
  2. 在任务对象名中按类别词匹配（`apple.n.01_1` 的类别 = `apple.n.01` 去掉 `.n.01` 后缀 / `_idx`），候选可能有多个实例。
  3. 多候选时，用**空间距离**择近（感知 3D position vs 任务对象 GT position——此处允许读 GT 仅用于 ID 解析，不影响评估真值归属，因为评估仍看仿真器状态）。
  4. 无匹配 → 规范 ID 退化为感知别名 `samjam:<track_id>`，作为"非任务相关物体"保留在图里（供规划感知环境，但不参与任务对象状态评估）。
- **映射表持久化**：`track_id ↔ canonical_id` 跨更新保留，保证同一物体身份稳定。SAM2 滑窗重置后，靠 `track_id`（SAMJAM Object 字典 key）延续身份。

> 注：ID 解析读取 GT 位置仅为"对齐感知与任务对象"，不把 GT 状态写进感知图；评估器独立读仿真器，二者解耦。

## 5. 关键技术风险与对策

| 风险 | 对策 |
|------|------|
| SAM2 video predictor 在线流式支持弱（`init_state` 倾向整段视频） | 滚动窗口 `W` 帧 re-init + 锚点 mask 重注入；窗口外靠 `track_id` 续身份。先小 `W`（如 8）验证稳定性 |
| VLM 每次更新成本高（延迟 + 费用） | 步长节流 `N=20`；可选"新 mask 占比低则跳过 VLM"二次节流；VLM 用 `gpt-4o-mini` 降本 |
| 滑窗重置导致 mask 断链 | 重置时把上一窗口末帧的已知 mask 作为新窗口起点 `add_new_mask`，`propagate` 仅前向一步 |
| 感知物体 ID 漂移（VLM 名称不一致、遮挡后再现） | `track_id` 为主键 + 规范 ID 映射表；`merge_distance` 空间去重；`seen_count`/`last_seen` 记录便于 stale 检测 |
| 深度/位姿缺失或噪声 | `mask_center_world` 已做有限点采样与中位数；缺深度时 position=None，节点仍保留但标记 `position=None` |
| 硬编码密钥（`samjam_sam2.py` 顶部 `OPENAI_BASE_KEY`） | **必须删除**，统一走环境变量（`ISBENCH_SCENE_GRAPH_LLM_API_KEY` 等） |
| 感知图污染评估 | 评估器只读仿真器状态（沿用 sketch §6.2）；感知 Snapshot 仅进 `tracker` 与 LLM prompt，不进 `Evaluator` |

## 6. 文件级落地计划

**新增**
- `og_ego_prim/scene_graph/backends/online_samjam.py` — `OnlineSAMJAMBackend`（在线 SAMJAM，复用 `vendor/samjam` 的 Object/mask/vlms）
- `og_ego_prim/scene_graph/hierarchical_memory.py` — `HierarchicalSceneGraphMemory`（统一持久记忆 + 层次化聚类）
- `og_ego_prim/scene_graph/id_resolver.py` — 感知别名→规范 ID 解析（任务对象匹配 + track 映射表）

**修改**
- `og_ego_prim/scene_graph/backends/factory.py` — 注册 `samjam_online`
- `og_ego_prim/scene_graph/perception_scene_graph.py` — `samjam_online` 路由到在线后端 + 统一记忆；去掉/弱化 `update_every` 双重节流
- `og_ego_prim/scene_graph/unigoal_memory_scene_graph.py` — 抽取记忆核心复用 `HierarchicalSceneGraphMemory`（GT 作为输入源之一），减少重复
- `og_ego_prim/scene_graph/backends/samjam_sam2.py` — 删除硬编码密钥；保留作离线参考或标记 deprecated
- `og_ego_prim/benchmark/online_benchmark.py` — 默认/文档化 `--scene_graph_backend samjam_online` + `--scene_graph_step_interval 20`

**不动**
- `og_ego_prim/primitives/executor.py`（`step_callback` 机制已够用）
- `og_ego_prim/navigation/*`（本期不引入航点回调）
- 评估器（真值独立）

## 7. 验证阶梯

1. **记忆单元**：喂合成 `PerceptionResult` 序列，验证物体合并/房间/分组/near 边正确、`seen_count`/`last_seen` 递增、滑窗重置后身份延续。
2. **ID 解析**：在真实任务上验证感知物体能映射到 `object_scope` 任务对象名，多实例按距离择近。
3. **端到端（真值对照）**：`--scene_graph_backend omnigibson_truth` vs `samjam_online`，同一任务同一计划，对比 Snapshot 节点/边召回，确认感知图能捕捉 GT 图的关键物体与关系。
4. **动态性**：导航过程中（`NAVIGATE_TO`）每 20 步取 Snapshot，确认 `currently_observed`/`distance_to_robot`/新物体出现随机器人移动而变化。
5. **成本**：单次 `detect` 端到端延迟与 VLM 调用次数，确认 `N=20` 下可接受。

## 8. 待定问题

1. 滚动窗口 `W` 与节流 `N` 的关系：`W` 应 ≥ `N` 对应的真实帧数吗？建议 `W` 独立设（默认 16），`N` 只决定调用频率。
2. 感知新物体（非任务对象）是否进 LLM prompt？建议进，但标注 `non_task`，避免规划器误用。
3. 是否需要把 SAMJAM 的 `is_moving/is_moved` 作为安全信号进评估？本期只进 prompt，不进评估器。
4. VLM 选型默认 gpt-4o-mini 还是本地 qwen？建议环境变量切换，默认 gpt-4o-mini。
