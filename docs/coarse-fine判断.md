## Coarse-fine
Optimized tool selection好问题。让我直接从代码中找出答案。

## 答案：两者都有，但核心是**3D 点云计算**

在 samjam_unigoal.py 中，`_build_current_frame_relations()` 的边来源有两个渠道：

### 渠道一：上游 SAMJAM 传来的 `frame_relations`

```python
def _build_current_frame_relations(self, source_to_node, frame_relations):
    relations = []
    # 渠道①：来自 SAMJAM 感知的边（可能是 VLM 生成的）
    for relation in frame_relations:
        source_node = source_to_node.get(relation.source_id)
        target_node = source_to_node.get(relation.target_id)
        normalized, reverse = _normalize_relation_with_direction(relation.relation)
        if normalized in ALLOWED_LIFELONG_RELATIONS:
            relations.append((source_node, target_node, normalized))
```

这部分关系是上游 `samjam_sam2` pipeline 传过来的。SAMJAM 的 `frame_relations` 中包含当前帧可见物体之间的 2D 空间关系，这些确实可以由 VLM 生成。**但这是靠外部输入，不是 samjam_unigoal 自己算的。**

### 渠道二：3D 几何计算（核心，且自包含）

```python
    # 渠道②：3D 几何计算 fine-coarse 关系
    for fine_node in visible_fine:
        for coarse_node in candidate_coarse:
            relation = self._infer_fine_coarse_relation(fine_node, coarse_node)
            # ↑ 完全基于 3D bbox 几何！
```

`_infer_fine_coarse_relation()` 的完整逻辑：

```python
def _infer_fine_coarse_relation(self, fine_node, coarse_node):
    # 1. 获取 3D bbox 的 min/max（从点云计算）
    fine_min, fine_max = self._node_bounds(fine_node)
    coarse_min, coarse_max = self._node_bounds(coarse_node)

    # 2. 计算 XY 投影重叠率
    fine_cover_ratio, _ = self._xy_overlap_ratio(...)

    # 3. 计算 Z 轴接触间隙
    top_contact_gap = min(
        abs(fine_min[2] - coarse_max[2]),  # fine 在 coarse 上面
        abs(fine_max[2] - coarse_min[2])   # fine 在 coarse 下面
    )

    # ⭐ 纯几何判断，没有 LLM/VLM
    if top_contact_gap <= on_gap_thresh and fine_cover_ratio >= ...:
        return 'on'
    if coarse in CONTAINER_COARSE_VOCAB and fine_xy_inside and ...:
        return 'in'
    if side_gap <= attach_gap_thresh and z_overlap / fine_height >= 0.35:
        return 'attach to'
    if z_gap >= above_height_thresh and fine_cover_ratio >= ...:
        return 'above'
    return None
```

**完全基于 3D 点云 bbox 的几何运算，没有调用任何 VLM/LLM。**

### 总结：Relation 来源归属

| 关系类型 | 来源 | 方法 |
|---|---|---|
| **fine-coarse** (apple→table) | **samjam_unigoal 自算** | 3D bbox 几何计算 ✅ |
| **coarse-coarse near** (table→cabinet) | **samjam_unigoal 自算** | 几何距离 ✅ |
| **fine-fine near** (cup→plate) | **samjam_unigoal 自算** | 共享 parent + 几何距离 ✅ |
| **非几何关系**（如 VLM 生成的语义关系） | **上游 SAMJAM 传入** | 来自外部，samjam_unigoal 不做 VLM 🚫 |

**`samjam_unigoal` 自己完全不调用 VLM**。它内部没有 `self.vlm(...)` 调用，所有几何推理都来自 `_node_bounds()`→ 3D 点云的 AABB。

## Unigoal
好问题。让我梳理一下 UniGoal 判断两个物体之间空间关系的完整流程。

核心入口在 graph.py 的 `update_edge()` 方法，以及辅助方法 `_extract_relation_type()`。总共有 **两套机制**：

---

## 机制一：LLM 批量推理（主要路径）

当有**新节点**加入场景图时，`update_edge()` 会触发批量关系推理：

### 步骤 1：创建候选边

```python
# 新节点与旧节点之间创建边
for i, new_node in enumerate(new_nodes):
    for j, old_node in enumerate(old_nodes):
        new_edge = Edge(new_node, old_node)

# 新节点之间也创建边
for i, new_node1 in enumerate(new_nodes):
    for j, new_node2 in enumerate(new_nodes[i + 1:]):
        new_edge = Edge(new_node1, new_node2)
```

所有新边初始 `relation = None`。

### 步骤 2：LLM 批量推理关系

把所有 `(node1.caption, node2.caption)` 对拼成 prompt，一次性请求 LLM：

```python
prompt = self.prompt_edge_proposal  # 预定义模板
prompt += '\n({}, {})' * len(new_edges)
prompt = prompt.format(*node_pairs)  # 填充所有物体对

relation_response = self.llm(prompt=prompt)
```

**prompt 模板** (`prompt_edge_proposal`) 的核心内容：

```
Provide the most possible single spatial relationship for each of the following object pairs. 
Answer with only one relationship per pair, and separate each answer with a newline character.
Choose the relationship from this list only: next to, opposite to, below, behind, in front of, 
above, on, in, under, over, against, near, left of, right of, upstairs, downstairs.
If none of the listed relationships is clear, answer unknown.

Examples:
Input:
Object pair(s):
(cabinet, chair)
Output:
next to

Object pair(s):
(table, lamp)
(bed, nightstand)
Output:
on
next to

Object pair(s):
(cabinet, table)
```

### 步骤 3：解析和归一化关系

LLM 的每一行输出通过 `_extract_relation_type()` 处理：

```python
def _extract_relation_type(self, response_line):
    # 去除噪声前缀
    noise_prefixes = ("given ", "i ", "here ", ...)
    if response_line.startswith(noise_prefixes): return None
    
    # 检查是否是 unknown
    if response_line in ("unknown", "none", ...): return None
    
    # 关系别名归一化
    relation_aliases = {
        "beside": "next to", "close to": "near", "nearby": "near",
        "on top of": "on", "atop": "on", "inside": "in",
        "within": "in", "beneath": "under", "underneath": "under",
        "to the left of": "left of", "to the right of": "right of",
        ...
    }
    # 用正则匹配
```

匹配成功后调用 `edge.set_relation(relation_type)`。

### 步骤 4：丢弃无效关系

如果 LLM 返回 `unknown` / `none`，或者行数不匹配，该边直接被 `edge.delete()` 删除（从两个节点的 `edges` 集合中移除）。

---

## 机制二：VLM 图像验证（次要路径）

`create_new_edge()` 方法会在**有新节点**时，用 VLM 看两个物体的**共同图像**来判断关系：

```python
def create_new_edge(self, new_node):
    for j, old_node in enumerate(self.nodes):
        image = self.get_joint_image(old_node, new_node)
        if image is not None:
            response = self.vlm(
                self.prompt_create_relation.format(
                    obj1=old_node.caption, obj2=new_node.caption
                ),
                image
            )
            if "No clear spatial relationship" not in response:
                # 再用 LLM get_relations 解析
                ...
```

其中 `get_joint_image()` 找两个物体**共同出现过的图像帧**中置信度最高的那张：

```python
def get_joint_image(self, node1, node2):
    # 找两个物体 image_idx 的交集
    image_idx = set(node1.object["image_idx"]) & set(node2.object["image_idx"])
    # 选置信度之和最大的那帧
    for idx in image_idx:
        conf = node1.conf[idx] + node2.conf[idx]
        ...
    return Image.fromarray(image)
```

但注意：这个方法目前**被注释掉了**（`create_new_edge()` 内部虽然调用了，但 `update_edge()` 中并不调它），实际生效的只有 LLM 批量推理路径。

---

## 关系列表（`self.relations`）

预定义的 16 种空间关系：

```
next to, opposite to, below, behind, in front of,
above, on, in, under, over, against, near,
left of, right of, upstairs, downstairs
```

## 完整流程图

```
新节点加入
    │
    ├─► 与所有旧节点创建 Edge(relation=None)
    ├─► 新节点之间创建 Edge(relation=None)
    │
    ▼
收集所有 relation=None 的边
    │
    ▼
构造 LLM prompt：列出所有 (obj1, obj2) 对
    │
    ▼
LLM 返回每对的关系（或 unknown）
    │
    ▼
_extract_relation_type() 解析+别名归一化
    │
    ├─► 有效关系 → edge.set_relation(type)
    ├─► unknown/none → edge.delete()
    └─► 行数不匹配 → 记录诊断
```

**总结**：UniGoal 判断物体关系的核心思路是 **LLM 常识推理**——不给图像，只给物体名称对，让 LLM 基于语义常识判断它们之间可能的空间关系。这是一种高效但依赖 LLM 先验知识的方法，不依赖几何计算或视觉分析。

这是一个非常好的问题，直击了 UniGoal 这套方案的核心局限。

## 答案：它**无法**区分

对于 `(apple, table)` 这样的裸物体名对，LLM **没有足够信息**来区分"apple on table"、"apple above table"和"apple below table"。LLM 只能基于**训练语料中的统计先验**给出一个答案：

| 物体对 | LLM 大概率输出 | 原因 |
|---|---|---|
| (apple, table) | `on` | 语料中最常见的搭配 |
| (book, table) | `on` | 同上 |
| (cup, table) | `on` | 同上 |
| (plate, table) | `on` | 同上 |

如果有另一组物体对：

| 物体对 | LLM 可能输出 | 原因 |
|---|---|---|
| (lamp, table) | `on` 或 `above` | 台灯在桌上（on）或吊灯在桌上（above）——但 LLM 不知道具体是哪种灯 |
| (chair, table) | `next to` / `near` | 椅子在桌子旁边是常识 |
| (rug, table) | `under` / `below` | 地毯可能在桌子下面 |

**核心问题**：LLM 的答案是**常识驱动**的，而不是**场景观察驱动**的。它输出的是"苹果通常放在桌子上"这个先验知识，而不是"在这个具体场景中，这个苹果相对于这张桌子的实际空间位置"。

## 代码中本应解决这个问题的机制（但未启用）

代码里其实预留了用 VLM 做视觉验证的路径：

```python
def create_new_edge(self, new_node):
    for j, old_node in enumerate(self.nodes):
        image = self.get_joint_image(old_node, new_node)  # 找两者共同出现的图像帧
        if image is not None:
            response = self.vlm(
                self.prompt_create_relation.format(
                    obj1=old_node.caption, obj2=new_node.caption
                ),
                image  # ⭐ 传入实际图像！
            )
```

`get_joint_image()` 能找到两个物体**在同一帧中同时出现**的 RGB 图像，传入 VLM 后，VLM 确实能看到苹果是在桌子上、桌子下、还是桌子上方。

但这条路径在 `update_edge()` 中**没有被调用**——实际的边推理走的全是纯 LLM 路径。

## 为什么这是个问题

考虑一个 IS-Bench 中的场景：

```
初始时：apple in refrigerator（苹果在冰箱里）
任务：将苹果放到桌子上
```

执行过程中：
1. 机器人打开冰箱 → LLM 看到 `(apple, refrigerator)` → 输出 `in` ✅ 正确
2. 机器人拿出苹果 → LLM 可能仍认为 `(apple, table)` 关系是 `on`，但此时苹果还在机器人手里，离桌子很远
3. 实际上场景中苹果可能在**机器人手中**、在**台面上**、在**推车里**——LLM 光靠 `(apple, table)` 对名永远猜不到真实位置

## 对比：真正有区分力的方案

| 方案 | 能否区分 on/above/below | 依赖 |
|---|---|---|
| UniGoal LLM 路径 | ❌ 不能 | LLM 常识先验 |
| UniGoal VLM 路径（未启用） | ✅ 能（看图像判断） | VLM + 多视角图像 |
| **几何计算**（3D bbox overlap） | ✅ 能（算包围盒相交） | 3D 点云 + 空间坐标 |
| **相对位置编码** | ✅ 能（算中心点相对方位） | 3D 位置 |

UniGoal 已经采集了 **3D 点云和 bbox**（通过 `mapping3d()`），理论上完全可以用**包围盒相交检测**或**点云相对位置**来几何地判断 `on`（上面接触）、`above`（上方有距离）、`below`（下方）、`next to`（侧面相邻）等关系——但它**没有用**这些几何信息，全部交给了 LLM。

**总结**：UniGoal 的关系推理是一个纯语义的"常识猜测"，而不是基于场景几何或视觉观测的真实判断。这是它最明显的设计取舍，也是在实际任务中可能产生错误边关系的主要原因。