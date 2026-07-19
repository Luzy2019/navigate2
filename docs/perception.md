Optimized tool selection

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/configs)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/third_party)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/agent)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/envs)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/map)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/utils)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/utils)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/agent/unigoal)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/__init__.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/graph.py#1-1), lines 1 to 200

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/graphbuilder.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/graph.py#200-200), lines 200 to 500

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/overlap.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/scenegraphcorrector.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/goalgraphdecomposer.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/graph.py#500-500), lines 500 to 850

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/agent/unigoal/agent.py#1-1), lines 1 to 200

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/utils/llm.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/map/bev_mapping.py#1-1), lines 1 to 100

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/graph.py#850-850), lines 850 to 1100

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/utils/utils.py#1-1), lines 1 to 200

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/utils/mapping.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/utils/slam_classes.py)

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/utils/utils.py#200-200), lines 200 to 450

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/graph.py#1100-1100), lines 1100 to 1250

Read [](file:///home/lzy/code/IS-Bench/og_ego_prim/scene_graph/vendor/unigoal/src/graph/utils/iou.py#1-1), lines 1 to 100

以下是 `vendor/unigoal` 中 UniGoal 场景图实现方案的整体分析：

---

## UniGoal 场景图核心架构

### 1. 目录结构

```
vendor/unigoal/
├── configs/              # 配置文件
│   ├── config_habitat.yaml
│   ├── categories.py
│   └── tasks/
├── src/
│   ├── agent/unigoal/    # 主 agent：导航+探索决策
│   ├── envs/             # Habitat 环境封装
│   ├── graph/            # ⭐ 场景图核心模块
│   │   ├── graph.py           # Graph 主类 + 节点/边/子图定义
│   │   ├── graphbuilder.py    # LLM 从文本构建目标图
│   │   ├── goalgraphdecomposer.py # 目标图分解
│   │   ├── overlap.py         # 图匹配/重叠计算
│   │   ├── scenegraphcorrector.py # 场景图校错
│   │   └── utils/
│   │       ├── slam_classes.py # MapObjectList, DetectionList
│   │       ├── utils.py       # 点云处理、过滤、融合
│   │       ├── mapping.py     # 空间相似度、merge 检测到物体
│   │       └── iou.py         # 3D IoU 计算
│   ├── map/               # BEV 语义地图
│   │   └── bev_mapping.py
│   └── utils/             # LLM/VLM 调用、相机矩阵
└── third_party/
    └── Grounded-Segment-Anything/  # GSAM 模型
```

### 2. 工作流程（从传感器到场景图）

整个流程由 `Graph.update_scenegraph()` 驱动，每帧依次执行：

```
update_scenegraph()
  ├── segment2d()        # ① 2D 语义分割 (Grounded-SAM)
  ├── mapping3d()        # ② 2D→3D 投影 + 点云建图
  ├── get_caption()      # ③ 获取物体类别标签
  ├── update_node()      # ④ 更新/新增节点
  ├── update_edge()      # ⑤ 更新/新增边
  └── get_scenegraph()   # ⑥ 导出最终 scene graph
```

### 3. 各步骤详解

#### ① segment2d() — 2D 分割

- 使用 **Grounded-SAM**（GroundingDINO + SAM）
- 对当前 RGB 帧检测 `self.node_space` 中的物体类别
- 输出：`sv.Detections(xyxy, confidence, mask)`，存入 `self.segment2d_results`

#### ② mapping3d() — 3D 投影与建图

- 将深度图 + 相机内参 + 机器人位姿结合，把 2D mask 反投影成 3D 点云
- 用 **FAISS** 计算新检测与已有物体点云之间的重叠度（spatial similarity）
- 超过阈值则与现有物体 **merge**，否则作为新物体加入 `self.objects`
- DBSCAN 降噪、voxel down sample
- 关键数据结构：`MapObjectList`（继承 `DetectionList`）

#### ③ get_caption() — 类别标注

- 对每个 3D 物体的多次检测结果做 **mode voting**，选取出现次数最多的 caption

#### ④ update_node() — 节点管理

- 每个 `self.objects_post` 中的物体对应一个 `ObjectNode`
- 节点属性：`caption`、`center`（地图坐标）、`room_node`（所属房间）、`exploration_level`、`score`、`edges`
- 新物体创建新 `ObjectNode`，已有物体 caption 变化时调用 `set_caption()` 重置边
- 房间划分通过 `room_lookup` 或 `room_map`（BEV 语义地图）

#### ⑤ update_edge() — 边关系推理

- 新节点与所有旧节点之间创建 `Edge`
- 使用 **LLM** 批量推理边的关系：
  - Prompt 包含所有 `(node1, node2)` 对
  - LLM 从预定义关系列表（`next to`, `on`, `in`, `under` 等）中选一个
  - 关系别名归一化（`beside`→`next to`）
- 无效边（LLM 返回 `unknown`/`none`）自动删除

#### ⑥ get_scenegraph() — 导出

- 将 `ObjectNode` + `Edge` 转换为标准 dict 格式：
  ```python
  {'nodes': [{'id': 'chair_0', 'position': [x, y]}],
   'edges': [{'source': 'chair_0', 'target': 'table_0', 'type': 'next to'}]}
  ```

### 4. 核心数据结构

| 类 | 用途 |
|---|---|
| `ObjectNode` | 场景图中的节点，包含 caption、center、room、edges |
| `Edge` | 连接两个 ObjectNode，包含 relation 类型 |
| `RoomNode` | 房间节点，包含所属 object nodes 列表 |
| `GroupNode` | 房间内的物体聚类组（DBSCAN 聚类），用于中间目标选择 |
| `Graph` | 主类，管理所有节点/边 + 建图流程 |
| `MapObjectList` | 3D 物体列表（含点云、bbox、特征） |
| `DetectionList` | 单帧检测结果列表 |

### 5. 目标图与匹配

- **目标图构建**：`GraphBuilder` 用 LLM 从文本描述提取物体和关系
- **目标图分解**：`GoalGraphDecomposer` 用 LLM 将目标图拆成子图（逐步探索）
- **图匹配**：`GraphMatcher` 用 NetworkX + grakel 计算场景图与目标图的重叠度（node overlap + edge similarity + GED heuristic）
- **场景图校正**：`SceneGraphCorrector` 用 LLM 检查节点和边的合理性

### 6. 关键依赖

- **Grounded-SAM**（GroundingDINO + SAM）— 2D 分割
- **OpenAI API**（LLM + VLM）— 关系推理、目标理解
- **LightGlue + DISK** — 图像特征匹配（instance goal）
- **FAISS** — 高效点云最近邻搜索
- **Open3D** — 点云处理
- **pytorch3d** — 3D IoU 计算

### 7. 与 IS-Bench 场景图的对比思路

UniGoal 的方案是 **online、incremental** 的建图方式——每帧都从传感器数据增量更新场景图，并维护一个与目标图逐步匹配的过程。这与你目前正在看的 IS-Bench 场景图代码（scene_graph）有相似的 pipeline，但 IS-Bench 可能在此基础上做了场景图历史管理、多步间隔更新、不同 backends（unigoal_grounded_sam 等）的抽象。需要我进一步对比分析 IS-Bench 的 scene graph 封装层吗？