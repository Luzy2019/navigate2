"""Generate an Excalidraw flowchart (.excalidraw JSON) for the samjam_unigoal backend workflow.

Usage:
    python scripts/generate_samjam_unigoal_flow.py
    # -> writes share/samjam_unigoal_flow.excalidraw
    # Open https://excalidraw.com and drag the file in to view/edit.
"""
import json
import os

elements = []
_counter = [0]


def nid():
    _counter[0] += 1
    return f"el{_counter[0]}"


def _base(x, y, w, h, **kw):
    d = {
        "id": nid(),
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "boundElements": [],
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "seed": 1,
        "updated": 1,
        "link": None,
        "locked": False,
    }
    d.update(kw)
    return d


def box(x, y, w, h, text, fill="#ffffff", stroke="#1e1e1e", fs=18, tc=None, rounded=True):
    """Rectangle with centered text. Returns element id."""
    eid = nid()
    rect = _base(
        x, y, w, h,
        type="rectangle",
        backgroundColor=fill,
        strokeColor=stroke,
        roundness=({"type": 3} if rounded else None),
    )
    rect["id"] = eid
    elements.append(rect)

    lines = text.split("\n")
    lh = fs * 1.25
    th = lh * len(lines)
    tid = nid()
    txt = _base(
        x + 8, y + (h - th) / 2, w - 16, th,
        type="text",
        strokeColor=(tc or stroke),
        backgroundColor="transparent",
    )
    txt["id"] = tid
    txt.update(
        fontSize=fs,
        fontFamily=1,
        text=text,
        textAlign="center",
        verticalAlign="middle",
        containerId=eid,
        originalText=text,
        lineHeight=1.25,
        baseline=fs,
    )
    elements.append(txt)
    rect["boundElements"] = [{"id": tid, "type": "text"}]
    return eid


def group_box(x, y, w, h, text, fill="#f1f5f9", stroke="#94a3b8", fs=20):
    """Dashed container with a title in the top-left corner."""
    eid = nid()
    rect = _base(
        x, y, w, h,
        type="rectangle",
        backgroundColor=fill,
        strokeColor=stroke,
        strokeWidth=1,
        strokeStyle="dashed",
        roundness={"type": 3},
    )
    rect["id"] = eid
    elements.append(rect)

    tid = nid()
    txt = _base(x + 14, y + 10, w - 28, fs * 1.25, type="text",
                strokeColor=stroke, backgroundColor="transparent")
    txt["id"] = tid
    txt.update(
        fontSize=fs,
        fontFamily=1,
        text=text,
        textAlign="left",
        verticalAlign="top",
        containerId=None,
        originalText=text,
        lineHeight=1.25,
        baseline=fs,
    )
    elements.append(txt)
    return eid


def text_label(x, y, text, fs=16, color="#1e1e1e", w=None, align="center"):
    tid = nid()
    if w is None:
        # rough width estimate
        w = max(20, len(text) * fs * 0.6)
    txt = _base(x, y, w, fs * 1.25, type="text",
                strokeColor=color, backgroundColor="transparent")
    txt["id"] = tid
    txt.update(
        fontSize=fs,
        fontFamily=1,
        text=text,
        textAlign=align,
        verticalAlign="top",
        containerId=None,
        originalText=text,
        lineHeight=1.25,
        baseline=fs,
    )
    elements.append(txt)
    return tid


def arrow(x1, y1, x2, y2, label_text=None, color="#1e1e1e", start_arrow=False, end_arrow=True, dashed=False):
    """Arrow with optional label near the midpoint."""
    eid = nid()
    a = _base(0, 0, 0, 0, type="arrow", strokeColor=color,
              strokeWidth=2, strokeStyle=("dashed" if dashed else "solid"),
              roughness=1, opacity=100,
              startArrowhead=(None if not start_arrow else "arrow"),
              endArrowhead=("arrow" if end_arrow else None))
    a["id"] = eid
    a["points"] = [[x1 - x1, y1 - y1], [x2 - x1, y2 - y1]]
    a["lastCommittedPoint"] = None
    # Excalidraw expects points relative to x,y
    a["x"] = x1
    a["y"] = y1
    a["width"] = abs(x2 - x1)
    a["height"] = abs(y2 - y1)
    elements.append(a)
    if label_text:
        mx = (x1 + x2) / 2
        my = (y1 + y2) / 2 - 14
        text_label(mx - len(label_text) * 3, my, label_text, fs=14, color=color, w=len(label_text) * 7 + 10)
    return eid


def connect(src_box, dst_box, src_side="bottom", dst_side="top", label_text=None, color="#1e1e1e",
            src_offset=0, dst_offset=0, dashed=False):
    """Connect two boxes (centered on the named side)."""
    sx, sy = _side(src_box, src_side, src_offset)
    dx, dy = _side(dst_box, dst_side, dst_offset)
    return arrow(sx, sy, dx, dy, label_text=label_text, color=color, dashed=dashed)


# Track box rectangles by id so we can connect them
_box_rects = {}


def _side(box_id, side, offset=0):
    x, y, w, h = _box_rects[box_id]
    if side == "top":
        return x + w / 2 + offset, y
    if side == "bottom":
        return x + w / 2 + offset, y + h
    if side == "left":
        return x, y + h / 2 + offset
    if side == "right":
        return x + w, y + h / 2 + offset
    raise ValueError(side)


def B(x, y, w, h, text, **kw):
    bid = box(x, y, w, h, text, **kw)
    _box_rects[bid] = (x, y, w, h)
    return bid


# ---------- Build the diagram ----------

# Colors
C_INPUT = "#dbeafe"      # light blue - inputs
C_BACKEND = "#bfdbfe"    # blue - backend entry
C_SAMJAM = "#cffafe"     # cyan - SAMJAM 2D perception
C_FILTER = "#fef3c7"     # amber - filtering
C_UNIGOAL = "#e0e7ff"    # indigo - UniGoal 3D
C_ISBENCH = "#fae8ff"    # fuchsia - ISBench enhancement
C_OUTPUT = "#dcfce7"     # green - output
C_TITLE = "#1e3a8a"

# Layout constants
LEFT = 60
COL1 = 60
COL2 = 360
COL3 = 700
COL4 = 1060
BW = 280  # box width
BH = 70   # box height tall
BH2 = 56  # box height short
GAP_Y = 40

# Title
text_label(LEFT, 20, "samjam_unigoal backend 工作流程", fs=32, color=C_TITLE, w=900)
text_label(LEFT, 64, "SAMJAM/SAM2 (2D 感知) + UniGoal (3D 场景图) 融合方案", fs=18, color="#475569", w=900)

# ===== Layer 0: Entry =====
entry = B(LEFT, 120, BW, BH,
          "env.reset() / observe() / detect()\nSAMJAMUniGoalBackend 四段式入口",
          fill=C_BACKEND, stroke="#1d4ed8", fs=16)

# ===== Layer 1: SAMJAM 2D perception group =====
g1 = group_box(COL1 - 20, 220, BW + 40, 360, "① SAMJAM/SAM2 单帧 2D 感知",
               fill="#ecfeff", stroke="#0891b2")

obs = B(COL1, 270, BW, BH2,
        "observe(env)\n获取 rgb / depth / intrinsics / camera_pose\n（校验 depth+pose 必须存在）",
        fill=C_SAMJAM, stroke="#0e7490", fs=14)

sam2 = B(COL1, 350, BW, BH2,
         "SAM2 视频分割\n输出 mask / bbox / track id",
         fill=C_SAMJAM, stroke="#0e7490", fs=14)

vlm = B(COL1, 430, BW, BH2,
        "VLM 识别\nPerceivedObject(name, mask, samjam_id)\n+ PerceivedRelation",
        fill=C_SAMJAM, stroke="#0e7490", fs=14)

detect_out = B(COL1, 510, BW, BH2,
               "PerceptionResult\n（缓存 last_frame / last_samjam_result）",
               fill=C_SAMJAM, stroke="#0e7490", fs=14)

connect(entry, obs, "bottom", "top")
connect(obs, sam2, "bottom", "top")
connect(sam2, vlm, "bottom", "top")
connect(vlm, detect_out, "bottom", "top")

# ===== Layer 2: ISBench filtering & name voting =====
g2 = group_box(COL2 - 20, 220, BW + 40, 460, "② ISBench 过滤 + 名称投票 (update_memory a)",
               fill="#fffbeb", stroke="#d97706")

dedup = B(COL2, 270, BW, BH2,
          "去重 _best_samjam_match_details\n同 native mask 保留 score 最高\n(duplicate_mask_claims)",
          fill=C_FILTER, stroke="#b45309", fs=13)

gate = B(COL2, 350, BW, BH2 + 8,
         "质量门控 _samjam_object_rejection_reason\nvisible / mask / bbox / match accepted\nmask 比例 / bbox 比例 / 有效深度比例",
         fill=C_FILTER, stroke="#b45309", fs=12)

vote = B(COL2, 440, BW, BH2 + 8,
         "名称稳定化 stable_name_for_samjam_object\n按 samjam_id 加权投票 (iou×conf)\n滞回 margin=1.25, unknown 不覆盖已知",
         fill=C_FILTER, stroke="#b45309", fs=12)

relf = B(COL2, 530, BW, BH2,
         "关系过滤 _filter_samjam_relations\n端点必须存活 / 类型对归一化\n去自环 / 去重",
         fill=C_FILTER, stroke="#b45309", fs=13)

filt_out = B(COL2, 610, BW, BH2,
             "SAMJAMFilterResult\n(objects, relations, report)",
             fill=C_FILTER, stroke="#b45309", fs=14)

connect(detect_out, dedup, "right", "left", label_text="喂入")
connect(dedup, gate, "bottom", "top")
connect(gate, vote, "bottom", "top")
connect(vote, relf, "bottom", "top")
connect(relf, filt_out, "bottom", "top")

# ===== Layer 3: UniGoal 3D mapping group (the big one) =====
g3 = group_box(COL3 - 20, 220, BW + 40, 760,
               "③ UniGoal 3D 图构建 + ISBench 增强 (adapter.update)",
               fill="#eef2ff", stroke="#4f46e5")

# step markers
steps = [
    ("s1", 260, "① 懒加载 Graph\nmonkey-patch 屏蔽 UniGoal 自带感知/LLM\n设置阈值 sim_threshold/obj_min_detections", C_UNIGOAL, "#4338ca"),
    ("s2", 340, "② _to_gobs 转换\nPerceivedObject -> gobs dict\n(xyxy/mask/class_id/caption)", C_UNIGOAL, "#4338ca"),
    ("s3", 420, "③ mapping3d() + 语义门控\npatch compute_spatial_similarities\n同名/unknown 放行, 同track冲突拒绝\n(避免跨物体误合并)", C_ISBENCH, "#a21caf"),
    ("s4", 520, "④ _reassociate_moving_tracks\n运动/被操作物体 track 重关联\n合并回旧 map_object 保持终身身份", C_ISBENCH, "#a21caf"),
    ("s5", 610, "⑤ _prune_invalid_map_objects\n清理空/NaN 点云、无效 bbox", C_ISBENCH, "#a21caf"),
    ("s6", 680, "⑥ graph.get_caption / update_node\n（魔改后基本 noop，保留流程）", C_UNIGOAL, "#4338ca"),
    ("s7", 750, "⑦ _prepare_lifelong_nodes\n分配 uid / normalized_caption\nis_vis / is_coarse / last_seen_step", C_ISBENCH, "#a21caf"),
    ("s8", 830, "⑧ _sync_moving_state\n同步 is_moved, 删除移动节点旧边", C_ISBENCH, "#a21caf"),
    ("s9", 900, "⑨ _build_current_frame_relations\n几何关系推断 (on/in/attach/above)\nfine->coarse + fine-fine near\n(VLM 关系仅作补充)", C_ISBENCH, "#a21caf"),
]

prev = None
for sid, y, text, fill, stroke in steps:
    b = B(COL3, y, BW, BH2 + 10, text, fill=fill, stroke=stroke, fs=12)
    if prev is not None:
        connect(prev, b, "bottom", "top")
    prev = b

s9_id = prev

# ===== Layer 4: Manipulation + edges (right column) =====
g4 = group_box(COL4 - 20, 220, BW + 40, 360,
               "④ 操作事件协调 + 边更新", fill="#fdf4ff", stroke="#a21caf")

manip = B(COL4, 270, BW, BH2 + 10,
          "⑩ _resolve_pending_manipulations\nnote_manipulation_event 事件打分定位\n(label/caption/track/位姿)\nresolved / ambiguous / unresolved",
          fill=C_ISBENCH, stroke="#a21caf", fs=12)

edges = B(COL4, 370, BW, BH2 + 16,
          "⑪ _update_structured_edges\n_refresh_coarse_near_edges\n_reconcile_manipulated_edges\n(可见? 有同向? 反向? 否则删)\nupsert 当前帧关系 + refresh fine-fine near",
          fill=C_ISBENCH, stroke="#a21caf", fs=11)

export = B(COL4, 470, BW, BH2,
           "⑫ graph.scenegraph = lifelong_graph\ngraph.update_group() (room->group)",
           fill=C_UNIGOAL, stroke="#4338ca", fs=13)

# Connect UniGoal column s9 -> manip (cross column)
connect(s9_id, manip, "right", "left", label_text="被操作节点", color="#a21caf")
connect(manip, edges, "bottom", "top")
connect(edges, export, "bottom", "top")

# ===== Output =====
out = B(COL4, 600, BW, BH + 20,
        "⑬ 导出 UniGoalMappedGraph\nPerceivedObject(uid/source_ids/stable_id/\nlifelong_label/states/hazard/bbox_3d/...)\nPerceivedRelation + scene_graph/room_graph/group_graph",
        fill=C_OUTPUT, stroke="#15803d", fs=12)
connect(export, out, "bottom", "top")

# Write back into result.objects/relations/scene_graph
writeback = B(COL4, 740, BW, BH2,
              "回写 result.* + 调试日志 + 清 SAM2 显存",
              fill=C_OUTPUT, stroke="#15803d", fs=13)
connect(out, writeback, "bottom", "top")

# A note box for key design points
note = B(LEFT, 740, BW * 2 + 60, 150,
         "关键设计：\n"
         "• 语义门控: 空间相似度高但类别不兼容的合并直接拒绝\n"
         "• 三重身份: 3D点云 + SAM2 track + 语义合并门控\n"
         "• 几何关系优先于 VLM 关系 (on/in/attach/above 阈值集)\n"
         "• 操作事件延迟协调: 动作 -> 下一帧打分定位 -> 逐条裁决旧边",
         fill="#fff7ed", stroke="#c2410c", fs=14)

# A legend box
legend_x = LEFT
legend_y = 920
text_label(legend_x, legend_y, "图例：", fs=16, color="#1e1e1e", w=80, align="left")
legend_items = [
    (C_SAMJAM, "#0e7490", "SAMJAM 2D 感知"),
    (C_FILTER, "#b45309", "ISBench 过滤/投票"),
    (C_UNIGOAL, "#4338ca", "UniGoal 原生 3D"),
    (C_ISBENCH, "#a21caf", "ISBench 增强"),
    (C_OUTPUT, "#15803d", "导出/回写"),
]
lx = legend_x + 80
for fill, stroke, name in legend_items:
    bid = B(lx, legend_y - 4, 20, 20, "", fill=fill, stroke=stroke, fs=10)
    text_label(lx + 28, legend_y, name, fs=14, color="#1e1e1e", w=160, align="left")
    lx += 200

# ---------- Assemble and write ----------
doc = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": elements,
    "appState": {
        "gridSize": None,
        "viewBackgroundColor": "#ffffff",
    },
    "files": {},
}

out_dir = "share"
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, "samjam_unigoal_flow.excalidraw")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)

print(f"Wrote {out_path} with {len(elements)} elements.")
