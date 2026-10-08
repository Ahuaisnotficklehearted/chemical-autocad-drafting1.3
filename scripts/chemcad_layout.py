#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_layout.py —— 工段划分 · 设备数量统计 · **ELK layered 版面分配** · 版面缺陷修正。

本模块是**化工语义**与**通用图布局**之间的适配层：
`chemcad_elk.py` 只会摆有向图，不认识设备、管线与工段；这里负责把一张
工艺流程图翻译成 ELK 图，再把 ELK 算出来的坐标翻译回设备基点。

为什么单独一个模块：`chemcad_engine.py` 管的是「几何是否合法」，
本模块管的是「版面是否好看」。两者的判据根本不同——

  * 正交性 / 线宽 / 位号 / 调节阀必注 是**逐实体**的硬规范，单看每个实体就能判；
  * 「设备太挤、符号相撞、文字压线、管线穿设备」是**对象之间**的关系型缺陷，
    必须先有**版面意图**（哪个工段放哪、一个工段怎么排）才好判、才好改。

## 五段式

| 段 | 做什么 | 依据 |
|---|---|---|
| ① `split_sections()` | 工段划分——显式优先，否则**流股连通性 + 层序平衡对切** | 流程图的工程语义就是沿工艺方向分段 |
| ② `count_equipment()` | 每段设备数量与构成（按类型分类计数） | 数量是**间距额度的分摊基数** |
| ③ `plan_layout()` | **ELK layered 版面分配**——每段建图 → 破环/分层/交叉最小化/层内排布/正交布线 | 分层直接来自流股拓扑，不再"猜行数" |
| ④ `detect_collisions()` / `repair_*` / `polish_texts()` | 迭代修正——把三类关系型缺陷收到 0 | 缺陷是关系型的，只能"探测 → 生成提示 → 整图重建 → 复检" |
| ⑤ `apply_plan()` | 按计划平移设备（连带锚点） | 落位 |

## 与旧版（启发式"行数 × 对齐 × 间距档"）的关键差别

旧版的自由度是**几何**（每段几行、行内怎么对齐、间距取哪一档），
代价函数是手写的七项。新版把几何**交给 ELK**：

* **分层来自流股拓扑**——不再靠"行数"猜。哪台设备在第几层，由
  「每条管线必须跨至少一层」这条硬约束解出来，所以流程方向是**推出来的**，
  不是排出来的。回流线由破环阶段统一处理，不会把塔系劈开。
* **交叉数是被最小化的目标**，不是代价函数里的一项估计。ELK 的层扫描
  直接作用在层内次序上，且虚拟节点保证长边也参与计算。
* **走线不穿设备是版面结构保证的**（见 `chemcad_elk` 模块头），
  不再主要依赖布线后处理。
* 剩下的搜索空间只有**ELK 选项档案 × 间距档**（4 × 7），
  代价函数项从"预估"变成"实测口径一致"。

代价函数的分项仍会写进报告，所以"为什么这么排"依然可解释，不是黑箱。
每张工段的 ELK 中间结果（层数、交叉数、折点数、虚拟节点数）也一并落盘。

坐标系与 `chemcad_ir` 一致：1 单位 = 1 mm，y 向上，A1 横幅 841×594。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import chemcad_elk as K
import chemcad_engine as E
import chemcad_symbols as S
from chemcad_ir import (
    SHEET_SIZES, Group, Item, Line, Pt, Polyline, Text, shift_ent, resolve_layer,
    TEXT_H_MAIN,
)

# ---------------------------------------------------------------------------
# 一、常量与默认档
# ---------------------------------------------------------------------------

#: 一个工段最多放几台设备——超过就继续对切。
#: 取 8 是经验值：A1 上一条工段带按 2~3 排折行时，8 台已是"一眼能读完"的上限。
DEFAULT_MAX_PER_SECTION = 8

#: 工段带之间的净距（mm）——要**明显大于**带内的层间距，
#: 否则读图人看不出"这里换工段了"。
BASE_SECTION_GAP = 26.0

# ---- ELK 间距基准（mm）与上限 ---------------------------------------------
#: 同层相邻设备净距基准（ELK `spacing.nodeNode`）
BASE_NODE_NODE = 14.0
#: 相邻层之间净距基准（ELK `spacing.nodeNodeBetweenLayers`）。
#: 必须 ≥ 2 × `spacing.edgeNode` + 走线槽位，否则竖段挤在间隙里贴到设备上。
BASE_LAYER_GAP = 34.0
#: 走线离设备包围盒的净距（ELK `spacing.edgeNode`）
BASE_EDGE_NODE = 8.0
#: 走线之间的净距（ELK `spacing.edgeEdge`）
BASE_EDGE_EDGE = 6.0

#: 间距档的**倍率**——ELK 的间距是输入参数，放大它就是把余量摊进间距。
#: 不设上限会摊成"设备 5 台、间距 200"，读图时视线要横跨半张图才连得上两台设备。
SPACING_SCALE: Tuple[float, ...] = (1.0, 1.35, 1.8, 2.4, 3.2, 4.2, 5.4)

MAX_NODE_NODE = 55.0
MAX_LAYER_GAP = 72.0
MAX_SECTION_GAP = 72.0

#: ELK 选项档案——搜索的**离散自由度**。四档各代表一种"排版性格"：
#: 分层策略（网络单纯形收窄 / 最长路）、层内对齐（四向平均 / 左上），
#: 层内排布（Brandes–Köpf / 简单中位）。由代价函数挑，不拍脑袋定。
ELK_PROFILES: Tuple[Dict[str, object], ...] = (
    {"name": "balanced-simplex", "layering_strategy": "NETWORK_SIMPLEX",
     "node_placement": "BRANDES_KOEPF", "bk_fixed_alignment": "BALANCED"},
    {"name": "balanced-longest", "layering_strategy": "LONGEST_PATH",
     "node_placement": "BRANDES_KOEPF", "bk_fixed_alignment": "BALANCED"},
    {"name": "leftup-simplex", "layering_strategy": "NETWORK_SIMPLEX",
     "node_placement": "BRANDES_KOEPF", "bk_fixed_alignment": "LEFTUP"},
    {"name": "simple-simplex", "layering_strategy": "NETWORK_SIMPLEX",
     "node_placement": "SIMPLE", "bk_fixed_alignment": "BALANCED"},
)

#: 段内折行时，一段最多摊到"可用宽度的几分之一"
BAND_WIDTH_FRACTION = 1.0

#: 目标面积利用率——太低是空，太高是挤。
#: 取 `FILL_RATIO²`：分配步把两轴各填到 FILL_RATIO 时利用率恰好落在这里，
#: 于是"留白失衡"项不会与"空间分配"打架。
TARGET_UTIL = 0.81

#: 代价函数权重（与代价项一一对应，便于报告里解释"为什么这样排"）
W_OVERFLOW = 400.0      # 内容超出可用区（最不可接受）
W_ASPECT = 40.0         # 内容长宽比与可用区不匹配（版面「斜着空」）
W_UTIL = 30.0           # 留白失衡
W_CROWD = 25.0          # 间距小于舒适值
W_THROUGH = 60.0        # 管线穿设备——**不允许出现**（硬约束：校验判 error，见
                        # `validate_pipe_through_equipment`；也是三类版面缺陷之一，修到 0）
W_CROSS = 0.0           # 管线交叉——**允许出现**（用户规定，归零不再参与代价）
                        # ⚠️ 曾为 18.0：那时搜索会为"少几个交叉"去牺牲留白/长宽比，
                        # 等于把一件**允许的事**当缺陷优化。用户明确
                        # 「管线穿设备不允许、管线交叉可以出现」后归零：
                        # 交叉不再是缺陷、不参与候选排序；交叉数仍由 ELK 层间统计
                        # 写进报告（geom.elk["crossings"]）供人工参考。
                        # 注意 W_THROUGH(60) > 旧 W_CROSS(18)：即便当年也是先保"不穿设备"。
W_BEND = 10.0           # 转折点数（每多一次转折多一次读错的机会）
W_LENGTH = 6.0          # 管线总长（越短越清爽）

#: 一次 `plan_layout` 最多真排多少次（每次 = 全部工段各跑一遍 ELK）
MAX_EVALS = 64

#: 分配后内容占可用区的比例。**不能填满**——留白是给仪表圆、引线、回路位号用的：
#: 设备占满以后，回路控制器只能压到设备上（实测：填到 99% 时必然出现符号重叠）。
FILL_RATIO = 0.90

#: 四边预留（mm）：自动生成的控制器气泡/引线/放空管口会落在设备带之外
ANNOT_ALLOWANCE = 16.0

#: 文字挪位搜索半径（mm）与步长档
TEXT_NUDGE_STEPS: Tuple[float, ...] = (2.0, 3.0, 5.0, 7.0, 10.0, 14.0, 20.0, 28.0)

#: 管线绕行让出的富余量（mm）——贴着设备绕行看起来像"黏在上面"
ROUTE_PAD = 10.0

#: 管线可选走线方式（前 5 个是 `chemcad_engine.ortho_path` 的 mode，
#: 后 5 个是**带接管段**的走线——塔顶/塔釜接管必须朝外先走一段，
#: 否则会从设备体内横穿出去，是"管线穿设备"的首选解）
ROUTE_MODES: Tuple[str, ...] = ("auto", "HV", "VH", "H-mid", "V-mid") + E.STUB_ROUTES


# ---------------------------------------------------------------------------
# 二、工段划分
# ---------------------------------------------------------------------------

def ref_owner(ref: object, ids: Set[str]) -> Optional[str]:
    """从引用串里取出它指向的**设备** id。

    ``"T-101.top"`` → ``T-101``；``"T-101"`` → ``T-101``；
    ``"PL-101@0.5"``（管线取点）与 ``"620,430"``（字面坐标）→ None。
    """
    s = str(ref).strip()
    if not s or "@" in s:
        return None
    if "." in s:
        head = s.partition(".")[0]
        return head if head in ids else None
    return s if s in ids else None


def ref_anchor(ref: object) -> str:
    """从引用串里取出**锚点名**（``"T-101.top"`` → ``"top"``；无则空串）。"""
    s = str(ref).strip()
    if "@" in s or "." not in s:
        return ""
    return s.partition(".")[2]


def build_flow_edges(spec: Dict, ids: Set[str]) -> List[Tuple[str, str, str, bool]]:
    """把 `pipes` 转成设备邻接边：`(上游, 下游, 管线号, 是否工段分界)`。

    只保留**两端都是设备**的管线；一端接到跨图接续/字面坐标的管线不参与连通性判断
    （否则"去罐区"这类线会把所有工段粘成一个）。
    """
    out: List[Tuple[str, str, str, bool]] = []
    for p in spec.get("pipes", []):
        a = ref_owner(p.get("from", ""), ids)
        b = ref_owner(p.get("to", ""), ids)
        if not a or not b or a == b:
            continue
        out.append((a, b, str(p.get("id", "")), bool(p.get("section_break"))))
    return out


def flow_order(order_hint: Sequence[str],
               edges: Sequence[Tuple[str, str, str, bool]]) -> List[str]:
    """设备流程序（拓扑序，Kahn）。

    流程方向是"工段按流程从左到右"的依据。有环（如回流/循环物流）时，
    环内节点按 `order_hint`（规格里的出现顺序）兜底，保证结果**可复现**。
    """
    indeg: Dict[str, int] = {i: 0 for i in order_hint}
    adj: Dict[str, List[str]] = {i: [] for i in order_hint}
    for a, b, _pid, _br in edges:
        if a in indeg and b in indeg:
            adj[a].append(b)
            indeg[b] += 1
    ready = [i for i in order_hint if indeg[i] == 0]
    out: List[str] = []
    seen: Set[str] = set()
    while ready:
        n = ready.pop(0)
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        for m in adj.get(n, ()):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    for i in order_hint:                       # 环内节点兜底
        if i not in seen:
            out.append(i)
    return out


def _components(nodes: Sequence[str],
                edges: Sequence[Tuple[str, str, str, bool]]) -> List[List[str]]:
    """按**未标分界**的边做弱连通分量；返回顺序跟随 `nodes` 的流程序。"""
    parent: Dict[str, str] = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b, _pid, brk in edges:
        if brk or a not in parent or b not in parent:
            continue
        union(a, b)
    buckets: Dict[str, List[str]] = {}
    for n in nodes:
        buckets.setdefault(find(n), []).append(n)
    return list(buckets.values())


#: 设备类型 / 位号 / 名称关键词 → **工艺职能**。
#: 工段名按"段内**最上游设备**的工艺职能"取（不是按"数量最多的类型"）——
#: 一个工段之所以成为工段，是因为它从某个职能开始；段首那台设备才是这个段的"名分"。
#: 匹配顺序 = 表序（越具体越靠前），先命中先算。
_FUNCTION_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("进料", "原料", "供料", "feed", "charging", "preheat", "预热"), "进料与预处理"),
    (("反应", "reactor", "reaction"), "反应"),
    (("塔", "精馏", "汽提", "吸收", "解吸", "闪蒸", "分馏", "column", "tower",
      "absorber", "stripper", "flash"), "分离"),
    (("冷凝", "再沸", "冷却", "加热", "换热", "预热器", "condenser", "reboiler",
      "cooler", "heater", "exchanger", "kettle"), "换热"),
    (("泵", "压缩机", "风机", "鼓风", "增压", "pump", "compressor", "blower", "fan"), "输送与加压"),
    (("干燥", "结晶", "离心", "过滤", "脱色", "精制", "dryer", "crystallizer",
      "centrifuge", "filter"), "精制"),
    (("缓冲", "储存", "储罐", "回流罐", "中间罐", "vessel", "drum", "tank"), "储存与缓冲"),
    (("加热炉", "火炬", "旋风", "喷射", "公用", "furnace", "flare", "cyclone", "ejector"),
     "公用与火炬"),
)


def equipment_function(eq: Dict) -> str:
    """一台设备的**工艺职能**（≠ 它的类型）。

    取值优先级：**设备名称 > 类型 > 位号**。
    * 名称信息最多：「原料进料泵」与「回流泵」的 type 都是 `pump`，
      但前者是进料职能、后者属储存与缓冲段的输送；
    * 类型（`tower` / `pump` / `exchanger`…）作次选；
    * 位号（`P-` / `T-` / `E-`…）只在名称与类型都判不出时兜底——它只有类别，没有职能。
    """
    for probe in (str(eq.get("name", "")), str(eq.get("type", "")), str(eq.get("tag", ""))):
        low = probe.lower()
        if not low:
            continue
        for keys, fn in _FUNCTION_RULES:
            if any(k in low for k in keys):
                return fn
    return "工艺"


def upstream_of(members: Sequence[str], pos: Dict[str, int]) -> str:
    """段内**最上游**设备（流程序最小者）。"""
    return min(members, key=lambda m: pos.get(m, 0)) if members else ""


# ---------------------------------------------------------------------------
# 静设备 / 动设备（PFD 版面：静设备在上方、动设备在下方）
# ---------------------------------------------------------------------------
#: **静设备**里"压过一切"的强关键字——用户明确：**带搅拌器的反应釜整体归静设备**
#: （搅拌桨/电机虽动，但主体是静设备）。所以这类名字必须先于"搅拌/混合"判定。
_STATIC_STRONG: Tuple[str, ...] = (
    "反应釜", "反应器", "聚合釜", "合成塔", "固定床", "流化床", "反应炉",
)

#: **动设备**关键字：主要作用部件持续旋转/往复、需驱动机带动。
#: 取自用户给定口径（泵类 / 压缩机·风机 / 搅拌混合 / 固液固气分离 / 旋转窑等）。
_DYNAMIC_KEYS: Tuple[str, ...] = (
    "泵", "压缩机", "风机", "鼓风", "增压", "搅拌器", "搅拌机", "搅拌桨",
    "混合机", "乳化", "离心机", "过滤机", "破碎", "旋转干燥", "旋转窑",
    "成型", "气力输送", "包装机",
    "pump", "compressor", "blower", "fan", "agitator", "mixer", "centrifuge",
    "crusher", "rotary", "conveyor", "packaging",
)

#: **静设备**关键字：提供反应、传热、传质、分离、储存空间（不含"过滤机/旋转干燥"等动设备）。
_STATIC_KEYS: Tuple[str, ...] = (
    "塔", "反应", "换热", "冷凝", "再沸", "蒸发", "冷却", "加热",
    "分离", "过滤器", "吸附", "储罐", "缓冲", "计量槽", "高位槽", "球罐",
    "干燥器", "结晶", "电解", "炉", "离子交换",
    "tower", "column", "reactor", "exchanger", "condenser", "reboiler",
    "evaporator", "cooler", "heater", "vessel", "drum", "tank", "separator",
)


def equipment_class(eq: Dict) -> str:
    """一台设备是 **静设备 `"static"`** 还是 **动设备 `"dynamic"`**。

    判定顺序（先具体后宽泛，避免误判）：
      1. **显式指定** `eq["class"]` / `eq["equipment_class"]`（`static`/`dynamic`/`静`/`动`）；
      2. **静设备强关键字**（反应釜/反应器/聚合釜…）——**压过**"搅拌"，因为
         "带搅拌器的反应釜整体归静设备"是明确口径；
      3. **动设备关键字**（泵/压缩机/风机/搅拌器/离心机/过滤机…）；
      4. **静设备关键字**（塔/换热/储罐/过滤器…）；
      5. 类型兜底：`pump` 系（本技能里泵与压缩机同用 pump 符号）→ 动；
      6. 都没有 → **静设备**（PFD 里绝大多数是静设备）。
    """
    raw = str(eq.get("class", eq.get("equipment_class", ""))).strip().lower()
    if raw in ("static", "静", "静设备", "s"):
        return "static"
    if raw in ("dynamic", "动", "动设备", "d"):
        return "dynamic"
    probes = [str(eq.get("name", "")), str(eq.get("type", "")), str(eq.get("tag", ""))]
    low = [p.lower() for p in probes if p]
    if any(any(k in p for k in _STATIC_STRONG) for p in low):
        return "static"
    if any(any(k in p for k in _DYNAMIC_KEYS) for p in low):
        return "dynamic"
    if any(any(k in p for k in _STATIC_KEYS) for p in low):
        return "static"
    if str(eq.get("type", "")).strip().lower() in ("pump", "compressor", "fan", "blower"):
        return "dynamic"
    return "static"


def classify_spec(spec: Dict) -> Dict[str, str]:
    """整张规格：`{设备 id: "static"/"dynamic"}`。"""
    return {str(e.get("id")): equipment_class(e) for e in (spec.get("equipment") or [])}


def _split_component(members: Sequence[str],
                     edges: Sequence[Tuple[str, str, str, bool]],
                     max_per: int,
                     depth: int = 0,
                     func_of: Optional[Dict[str, str]] = None) -> List[List[str]]:
    """超限分量按**层序 + 职能边界**对切，递归切开。

    为什么是"层序 + 平衡"而不是通用的最小割：流程图的工程语义就是**沿工艺方向分段**
    （反应段 → 分离段 → 精制段）。按层序排好后切，切出来的段既在流程上连续、
    数量上也均衡——这正是工程师手工分段的做法。

    **切点优先落在"职能变化处"**（`func_of` 给出每台设备的工艺职能）：
    纯按数量对半切，会把"塔 + 塔顶冷凝器 + 回流罐"从中间劈开——三段式的装置里
    回流罐跟塔本来是一伙的。工段边界本来就该是**职能边界**，所以先按职能筛候选切点，
    再在这些候选里挑最均衡的；一个职能边界都没有时才退回纯数量对半分。
    """
    if len(members) <= max_per or depth > 8:
        return [list(members)]
    idx = {m: k for k, m in enumerate(members)}
    sub = [(a, b) for a, b, _p, brk in edges
           if not brk and a in idx and b in idx]
    # 层序：从子图内入度为 0 的节点做 BFS 深度（无源则取全图第一个）
    indeg = {m: 0 for m in members}
    for a, b in sub:
        indeg[b] += 1
    frontier = [m for m in members if indeg[m] == 0] or [members[0]]
    rank = {m: -1 for m in members}
    q = list(frontier)
    for m in frontier:
        rank[m] = 0
    while q:
        n = q.pop(0)
        for a, b in sub:
            if a == n and rank[b] < 0:
                rank[b] = rank[n] + 1
                q.append(b)
    for m in members:                          # 环内节点兜底
        if rank[m] < 0:
            rank[m] = 0
    ordered = sorted(members, key=lambda m: (rank[m], idx[m]))

    func_of = func_of or {}
    cuts = range(1, len(ordered))
    if func_of:
        fn_cut = [c for c in cuts if func_of.get(ordered[c - 1]) != func_of.get(ordered[c])]
        pool = fn_cut or list(cuts)
    else:
        pool = list(cuts)
    # 候选里挑最均衡切点（平分时取靠前的一个，保证结果确定）
    best_cut = min(pool, key=lambda c: (abs(2 * c - len(ordered)), c))
    left, right = ordered[:best_cut], ordered[best_cut:]
    return (_split_component(left, edges, max_per, depth + 1, func_of)
            + _split_component(right, edges, max_per, depth + 1, func_of))


def split_sections(spec: Dict, *, max_per_section: Optional[int] = None,
                   sheet: Optional[str] = None) -> List[Dict[str, object]]:
    """工段划分。三级优先：显式 `sections` → 设备 `section` 字段 → 启发式自动划分。

    返回 ``[{"id", "name", "members", "function", "upstream", "source"}]``：
    `name` 是**段内最上游设备的工艺职能**（显式给名时用显式名），
    `upstream` 记下是哪一台定的名；**列表顺序即流程顺序**——
    工段从左到右依次排开，与读图习惯一致。
    """
    eqs = [e for e in spec.get("equipment", []) if e.get("id")]
    ids = {str(e["id"]) for e in eqs}
    if not ids:
        return []
    by_id = {str(e["id"]): e for e in eqs}
    order = [str(e["id"]) for e in eqs]
    edges = build_flow_edges(spec, ids)

    # ---- ① 显式工段表 -----------------------------------------------------
    explicit = spec.get("sections") or []
    if explicit:
        out: List[Dict[str, object]] = []
        used: Set[str] = set()
        for k, s in enumerate(explicit):
            mem = [str(m) for m in (s.get("equipment") or []) if str(m) in ids]
            if not mem:
                mem = [i for i in order if str(by_id[i].get("section")) == str(s.get("name") or s.get("id"))]
            mem = [m for m in mem if m not in used]
            used.update(mem)
            if not mem:
                continue
            fpos0 = {i: q for q, i in enumerate(flow_order(order, edges))}
            head0 = upstream_of(mem, fpos0)
            fn0 = equipment_function(by_id[head0]) if head0 else "工艺"
            out.append({"id": str(s.get("id") or f"S{k + 1}"),
                        "name": str(s.get("name") or f"{fn0}工段"),
                        "members": mem, "function": fn0, "upstream": head0,
                        "source": "显式 sections"})
        rest = [i for i in order if i not in used]
        if rest:
            out.append({"id": "S_rest", "name": "未归段设备", "members": rest,
                        "function": "工艺", "upstream": rest[0],
                        "source": "显式 sections 未覆盖"})
        return out

    # ---- ② 设备自带 section 字段 ------------------------------------------
    if any(by_id[i].get("section") for i in order):
        buckets: Dict[str, List[str]] = {}
        for i in order:
            nm = str(by_id[i].get("section") or "未归段设备")
            buckets.setdefault(nm, []).append(i)
        fpos = {i: k for k, i in enumerate(flow_order(order, edges))}
        seq = sorted(buckets.items(), key=lambda kv: min(fpos.get(x, 0) for x in kv[1]))
        out2: List[Dict[str, object]] = []
        for k, (nm, mem) in enumerate(seq):
            head = upstream_of(mem, fpos)
            fn = equipment_function(by_id[head]) if head else "工艺"
            out2.append({"id": f"S{k + 1}", "name": nm, "members": mem,
                         "function": fn, "upstream": head,
                         "source": "设备 section 字段"})
        return out2

    # ---- ③ 启发式自动划分 -------------------------------------------------
    max_per = int(max_per_section or (spec.get("layout") or {}).get("max_per_section")
                  or DEFAULT_MAX_PER_SECTION)
    fseq = flow_order(order, edges)
    pos = {n: k for k, n in enumerate(fseq)}
    func_of = {n: equipment_function(by_id[n]) for n in order}
    groups: List[List[str]] = []
    for comp in _components(order, edges):
        comp = sorted(comp, key=lambda n: pos.get(n, 0))
        groups += _split_component(comp, edges, max_per, func_of=func_of)
    groups.sort(key=lambda g: min(pos.get(n, 0) for n in g))
    # 自动工段划分不能切散已证实的塔循环；显式工段归属仍由调用方决定。
    import chemcad_process_layout as PL
    for loop in PL.recognize(spec,ids):
        target=next(g for g in groups if loop['column'] in g)
        for member in loop['members']:
            for group in groups:
                if group is not target and member in group:group.remove(member)
            if member not in target:target.append(member)
    groups=[g for g in groups if g]

    # 段名 = **段内最上游设备的工艺职能**：一个工段之所以成为工段，是因为它从某个职能开始；
    # 段首那台设备才是这个段的"名分"。（按"数量最多的类型"取名会撞车，也读不出流程）
    out: List[Dict[str, object]] = []
    fn_seq: Dict[str, int] = {}
    for k, mem in enumerate(groups):
        mem = sorted(mem, key=lambda n: pos.get(n, 0))
        head = upstream_of(mem, pos)
        fn = equipment_function(by_id[head]) if head else "工艺"
        fn_seq[fn] = fn_seq.get(fn, 0) + 1
        name = f"{fn}工段" if fn_seq[fn] == 1 else f"{fn}工段 {fn_seq[fn]}"
        out.append({"id": f"S{k + 1}", "name": name, "members": mem,
                    "function": fn, "upstream": head,
                    "source": "流股连通性 + 层序平衡对切"})
    return out


def count_equipment(spec: Dict,
                    sections: Sequence[Dict[str, object]]) -> Dict[str, object]:
    """每个工段的**设备数量**与构成（按 type 分类计数）。"""
    by_id = {str(e["id"]): e for e in spec.get("equipment", []) if e.get("id")}
    total = 0
    rows: List[Dict[str, object]] = []
    for s in sections:
        types: Dict[str, int] = {}
        for m in s["members"]:                                   # type: ignore[union-attr]
            t = str(by_id.get(str(m), {}).get("type", "?"))
            types[t] = types.get(t, 0) + 1
        n = len(s["members"])                                    # type: ignore[arg-type]
        total += n
        rows.append({"id": s["id"], "name": s["name"], "count": n,
                     # 段名是谁定的、段首是哪台——报告里要能一眼核对
                     "function": s.get("function", ""),
                     "upstream": s.get("upstream", ""),
                     "types": dict(sorted(types.items(), key=lambda kv: (-kv[1], kv[0])))})
    return {"total": total, "sections": rows,
            "sheet_total": len(spec.get("equipment", []))}


# ---------------------------------------------------------------------------
# 三、设备量尺（尺寸 + 锚点，全部相对基点）
# ---------------------------------------------------------------------------

class EqBox:
    """一台设备的量尺：含默认标注的占位、原块物理包络、命名锚点偏移。

    锚点偏移是"预估管线"与"端口放置"的关键——用锚点而不是包围盒中心估管线，
    估出来的走向才与实际出图一致（塔顶接管在正上方，不是几何中心）。
    """

    __slots__ = ("eid", "spec", "w", "h", "cx", "cy", "anchors", "dirs", "body")

    def __init__(self, eid: str, spec: Dict, rect: E.Rect, anchors: Dict[str, Pt],
                 dirs: Optional[Dict[str, Pt]] = None):
        self.eid = eid
        self.spec = spec
        self.body = rect
        self.w = rect[2] - rect[0]
        self.h = rect[3] - rect[1]
        self.cx = (rect[0] + rect[2]) / 2.0
        self.cy = (rect[1] + rect[3]) / 2.0
        self.anchors = anchors
        self.dirs = dirs or {}

    def anchor_offset(self, name: str) -> Optional[Pt]:
        """锚点相对**节点中心**的偏移（ELK 端口坐标）。"""
        p = self.anchors.get(name)
        if p is None:
            return None
        return (float(p[0]) - self.cx, float(p[1]) - self.cy)


def measure_equipment(spec: Dict, sheet: str = "A1") -> Dict[str, EqBox]:
    """把所有设备在一张**草稿图**上量一遍尺寸与锚点（不产生任何输出）。

    用真实符号量尺而不是"猜一个 30×20 的格子"——塔器是 24×120、泵是 11×11，
    差别一个数量级；按猜的尺寸排版，出来必然不是压成一团就是散得读不出关系。
    """
    scratch = E.ChemDrawing(name="measure", sheet=sheet)
    out: Dict[str, EqBox] = {}
    for e in spec.get("equipment", []):
        eid = str(e.get("id", ""))
        if not eid:
            continue
        import chemcad_block_library as BL
        g = BL.place(scratch, e, (0.0, 0.0),
                     rot=float(e.get("rot", 0.0)), tag=str(e.get("tag", "")))
        r = E.group_rect(g)
        anc = dict(g.meta.get("anchors", {}) or {})              # type: ignore[arg-type]
        dirs = {nm: d for nm in anc
                if (d := E.anchor_direction(g, nm)) is not None}
        if r is None:
            r = (-10.0, -7.0, 10.0, 7.0)
        w = max(4.0, r[2] - r[0])
        h = max(4.0, r[3] - r[1])
        out[eid] = EqBox(eid, e, (r[0], r[1], r[2], r[3]), anc, dirs)
        # 节点占位含默认标注；设备物理边界、锚点及出线方向仍用原块。
        labels = S.add_equipment_labels(scratch, e, r,
            True, explicit=False,equipment=g)
        rs = [r] + [E.group_rect(label) for label in labels]
        rs = [rr for rr in rs if rr is not None]
        occupied = (min(rr[0] for rr in rs), min(rr[1] for rr in rs),
                    max(rr[2] for rr in rs), max(rr[3] for rr in rs))
        b = out[eid]
        b.w, b.h = E.rect_size(occupied)
        b.cx, b.cy = E.rect_center(occupied)
        if out[eid].w < 4.0 or out[eid].h < 4.0:                     # 退化尺寸兜底
            out[eid].w, out[eid].h = w, h
    return out


# ---------------------------------------------------------------------------
# 四、可用区域
# ---------------------------------------------------------------------------

def available_area(spec: Dict, sheet: str,
                   n_notes: int = 0) -> E.Rect:
    """图纸可用区 = 幅面 − 装订边 − 标题栏 − 说明框。

    排版必须**自己**避开标题栏与说明框：`fit_to_sheet` 只负责居中和平移，
    它发现内容压标题栏也只是报一句 `overlaps_title_block`，不会帮你躲开。
    """
    lay = spec.get("layout") or {}
    if lay.get("area"):
        a = list(lay["area"])
        return (float(a[0]), float(a[1]), float(a[2]), float(a[3]))
    W, H = SHEET_SIZES.get(sheet.upper(), SHEET_SIZES["A1"])
    m = float(spec.get("margin", 10.0)) + 6.0
    x0, y0, x1, y1 = m, m, W - m, H - m
    if n_notes:                                   # 说明框在左上角，预留出它的高度
        est_h = 10.0 + 5.0 * (n_notes + 3)
        na = spec.get("notes_at") or [30.0, H - 16.0]
        if float(na[0]) < x0 + 160.0 and float(na[1]) > H * 0.5:
            y1 = min(y1, float(na[1]) - est_h)
    return (x0, y0, x1, y1)


def planning_area(area: E.Rect, allow: float = ANNOT_ALLOWANCE) -> E.Rect:
    """扣掉四边预留后的**排版作业区**。

    设备带之外还有仪器圆、测量引线、回路位号要占地方，排版必须替它们留出来；
    否则自动生成的控制器会正好落在设备轮廓上（实测：把可用区填到 99% 时必然如此）。
    """
    r = E.expand_rect(area, -allow)
    return r if (r[2] - r[0]) > 40.0 and (r[3] - r[1]) > 40.0 else area


#: 图幅由小到大（显式指定图幅时按此顺序做"升幅"用）
SHEET_ASCENDING: Tuple[str, ...] = ("A3", "A2", "A1", "A0")

#: **本技能的标准图幅 = A1（841×594）**——取自**浙工大参赛 PFD 实测**：
#: 那份 `工艺物流流程图（PFD）-2004.dwg` 里 33 个图框**清一色 841×594**（A1 横放）。
#: 所以 `layout.sheet="auto"` **从 A1 起试**（不再"挑最小装得下的"——那样会把
#: 9 台设备的图判去 A3，与浙工大"一律 A1"的口径不符）；A1 真装不下才升 A0。
SHEET_AUTO_ORDER: Tuple[str, ...] = ("A1", "A0")

#: 版面利用率低于这个值就认为"内容撑不起这张图幅"
SHEET_MIN_UTIL = 0.38


def next_sheet(sheet: str) -> Optional[str]:
    """比给定幅面**大一号**的标准幅面（已是最大则 None）。

    用途：`layout.sheet="auto"` 选完幅面后，真实图纸里还有仪器圆/引线/位号要占地方，
    排完发现装不下时往上退一档——"排版时看不见的占用"只能靠出图后实测补上。
    """
    order = list(SHEET_ASCENDING)
    sh = sheet.upper()
    return order[order.index(sh) + 1] if sh in order[:-1] else None


def plan_utilization(plan: Dict[str, object]) -> float:
    """一次排版的版面利用率（内容面积 / 可用区面积）。"""
    a = plan.get("area") or [0.0, 0.0, 0.0, 0.0]
    c = plan.get("content_size") or [0.0, 0.0]
    aw, ah = float(a[2]) - float(a[0]), float(a[3]) - float(a[1])   # type: ignore[index]
    cw, ch = float(c[0]), float(c[1])                              # type: ignore[index]
    return (cw * ch) / max(aw * ah, 1.0)


# ---------------------------------------------------------------------------
# 五、ELK 图构建：设备 → 节点，锚点 → 端口，管线 → 边
# ---------------------------------------------------------------------------

def _scaled_options(profile: Dict[str, object], scale: float) -> K.LayoutOptions:
    """把"选项档案 + 间距倍率"折成一份 ELK 选项。"""
    o = K.options_from(profile)
    o.spacing_node_node = min(MAX_NODE_NODE, BASE_NODE_NODE * scale)
    o.spacing_node_node_between_layers = min(MAX_LAYER_GAP, BASE_LAYER_GAP * scale)
    o.spacing_edge_node = BASE_EDGE_NODE
    o.spacing_edge_edge = BASE_EDGE_EDGE
    return o


def build_elk_graph(section: Dict[str, object], boxes: Dict[str, EqBox],
                    spec: Dict, opts: K.LayoutOptions) -> Tuple[K.ElkGraph, Dict[str, str]]:
    """把一个工段翻成 ELK 图。返回 `(图, 边 id → 管线号)`。

    翻译规则（三条，都是"不得不这样"的）：

    * **设备 = 节点**，尺寸用真实块包络加默认标注占位——布局不猜尺寸；
      端口 = 该设备被引用到的**命名锚点**，位置用真锚点偏移，
      于是管线从法兰出去，而不是从设备壁的空白处出去。
    * **管线 = 边**，方向 = 流程方向。回流线在这里仍然是"反着指"的边，
      由破环阶段统一处理，不在建模时就把它掰正。
    * 只建**两端都是本工段设备**的管线。跨工段管线不在段内建图：
      段是独立排版的（段与段之间只按带宽铺排），把跨段管线塞进某一侧会
      让那一侧的层序被别段设备牵着走。
    """
    g = K.ElkGraph(opts)
    members = [str(m) for m in section["members"]]                        # type: ignore[union-attr]
    mset = set(members)
    for m in members:
        b = boxes.get(m)
        if b is None:
            continue
        g.add_node(m, b.w, b.h, section=str(section["id"]))
    edge_lines: Dict[str, str] = {}
    for p in spec.get("pipes", []):
        line = str(p.get("id", ""))
        frm, to = str(p.get("from", "")), str(p.get("to", ""))
        a = ref_owner(frm, mset)
        b2 = ref_owner(to, mset)
        if not a or not b2 or a == b2 or a == b2:
            continue
        if a not in g.nodes or b2 not in g.nodes:
            continue
        pa, da = ref_anchor(frm), ref_anchor(to)
        if not pa or not da:
            continue                            # 引用没给锚点 → 无法定端口，不建边
        bx, by = boxes[a], boxes[b2]
        if pa not in bx.anchors or da not in by.anchors:
            continue
        _ensure_port(g, boxes, a, pa)
        _ensure_port(g, boxes, b2, da)
        g.add_edge(line, (a, pa), (b2, da))
        edge_lines[line] = line
    return g, edge_lines


def _ensure_port(g: K.ElkGraph, boxes: Dict[str, EqBox], eid: str, anch: str) -> None:
    """按真锚点建端口：偏移相对节点中心，所在边由出线方向决定。"""
    if anch in g.nodes[eid].ports:
        return
    b = boxes[eid]
    off = b.anchor_offset(anch)
    if off is None:
        return
    side = K.side_of(b.dirs.get(anch), K.EAST)
    g.add_port(eid, anch, side, off)


# ---------------------------------------------------------------------------
# 六、版面分配：每段跑 ELK，段与段铺排
# ---------------------------------------------------------------------------

class Geom:
    """一次候选排版的几何结果。"""

    __slots__ = ("place", "bands", "size", "origin", "sections", "elk", "spacing")

    def __init__(self, place: Dict[str, Pt], bands: Dict[str, E.Rect],
                 size: Tuple[float, float], origin: Pt,
                 sections: List[Dict[str, object]],
                 elk: Dict[str, object], spacing: Tuple[float, float, float]):
        self.place = place
        self.bands = bands
        self.size = size
        self.origin = origin
        self.sections = sections
        self.elk = elk
        self.spacing = spacing


def _layout_one_section(section: Dict[str, object], boxes: Dict[str, EqBox],
                        spec: Dict, opts: K.LayoutOptions,
                        wrap_width: float) -> Optional[Dict[str, object]]:
    """一个工段跑一遍完整 ELK 流水线，返回它的块（宽高 + 段内相对落位 + 中间结果）。"""
    g, _lines = build_elk_graph(section, boxes, spec, opts)
    if not g.nodes:
        return None
    before = {n: (g.nodes[n].x, g.nodes[n].y) for n in g.nodes}
    del before
    metrics = K.layout(g, wrap_target_width=wrap_width,
                       max_layers_per_band=int(opts.max_layers_per_band))
    rel: Dict[str, Pt] = {}
    for eid in g.real_nodes():
        b = boxes[eid]
        n = g.nodes[eid]
        rel[eid] = (n.x - b.cx, n.y - b.cy)          # 节点中心 → 设备基点
    r = K.content_rect(g)
    import chemcad_process_layout as PL
    rel,process_size,process_loops=PL.arrange(spec,rel,boxes)
    layers: List[List[str]] = []
    layer_bands: List[int] = []
    for layer in g.layers:
        real = [n for n in layer if not g.nodes[n].is_dummy]
        if not real:
            continue
        layers.append(real)
        # 该层属于哪一排（折行后一个工段会分成若干排，排与排的 x 起点相同）
        layer_bands.append(int(g.nodes[real[0]].band))
    return {"sid": str(section["id"]), "name": str(section["name"]),
            "w": process_size[0] if process_size else max(1.0, r[2] - r[0]),
            "h": process_size[1] if process_size else max(1.0, r[3] - r[1]),
            "place": rel, "layers": layers, "layer_bands": layer_bands,
            "elk": dict(metrics),"process_loops":process_loops,
            "routes": {eid: [(x - r[0], y - r[1]) for x, y in e.route]
                       for eid, e in g.edges.items() if e.route and not process_loops}}


def layout_sections(spec: Dict, sections: Sequence[Dict[str, object]],
                    boxes: Dict[str, EqBox], area: E.Rect, *,
                    profile: Dict[str, object], scale: float,
                    pack: str = "row") -> Geom:
    """**版面分配主函数**：每个工段跑一遍 ELK，再把各段的块铺排进可用区。

    为什么"每段单独排版"而不是"整图一次排版"：工段是流程图上真实存在的
    工程分界（反应段 / 分离段 / 精制段），段与段之间允许有一道明显的空档。
    把整图丢给 ELK 一次排，段与段之间只会拿到"层间距"，
    读图人就看不出"这里换工段了"。分开排、再用 `section_gap` 叠，
    分段意图才保得住；代价是跨段管线改由出图阶段的布线处理。

    段内折行的目标带宽取"可用宽度 ÷ 段数"——一段排得太宽会把别的段挤到下一排，
    一段排得太窄又会折出很多排，两者都由代价函数兜住。
    """
    opts = _scaled_options(profile, scale)
    # 工段间距：规格**显式给了**就照用（那是设计者给的意图，倍率不该再乘它一遍）；
    # 没给才用基准 × 倍率，交给搜索去调。这条与"规格写了 route 就不覆盖"同一个道理。
    explicit_gap = (spec.get("layout") or {}).get("section_gap")
    section_gap = float(explicit_gap) if explicit_gap is not None \
        else min(MAX_SECTION_GAP, BASE_SECTION_GAP * scale)
    aw = max(1.0, area[2] - area[0])
    ser: List[Dict[str, object]] = [s for s in sections if s.get("members")]
    n = max(1, len(ser))
    wrap_width = aw * FILL_RATIO / n

    blocks: List[Dict[str, object]] = []
    for s in ser:
        blk = _layout_one_section(s, boxes, spec, opts, wrap_width)
        if blk:
            blocks.append(blk)
    if not blocks:
        return Geom({}, {}, (0.0, 0.0), (0.0, 0.0), [], {}, (0.0, 0.0, section_gap))

    place, bands, size = _pack_blocks(blocks, aw, pack, section_gap)
    elk: Dict[str, object] = {
        "layers": sum(len(b["layers"]) for b in blocks),          # type: ignore[arg-type]
        "crossings": sum(int((b["elk"] or {}).get("crossings", 0)) for b in blocks),
        "bendpoints": sum(int((b["elk"] or {}).get("bendpoints", 0)) for b in blocks),
        "dummy_nodes": sum(int((b["elk"] or {}).get("dummy_nodes", 0)) for b in blocks),
        "bands": sum(int((b["elk"] or {}).get("bands", 1)) for b in blocks),
        "reversed_edges": sum(int((b["elk"] or {}).get("reversed_edges", 0))
                              for b in blocks),
        "edge_node_hits": sum(int((b["elk"] or {}).get("edge_node_hits", 0))
                              for b in blocks),
        "per_section": [{"id": b["sid"], "name": b["name"],
                         "layers": len(b["layers"]),          # type: ignore[arg-type]
                         "bands": (b["elk"] or {}).get("bands", 1),
                         "crossings": (b["elk"] or {}).get("crossings", 0),
                         "bendpoints": (b["elk"] or {}).get("bendpoints", 0),
                         "block": [round(b["w"], 1), round(b["h"], 1)]}   # type: ignore[arg-type]
                        for b in blocks],
    }
    routes: Dict[str, List[Pt]] = {}
    off_of = {b["sid"]: bands.get(str(b["sid"])) for b in blocks}
    for b in blocks:
        br = off_of.get(str(b["sid"]))
        if br is None:
            continue
        ox, oy = br[0], br[1]
        for eid, path in (b["routes"] or {}).items():             # type: ignore[union-attr]
            routes[eid] = [(x + ox, y + oy) for x, y in path]
    elk["routes"] = routes
    elk['process_loops']=[loop for b in blocks for loop in b.get('process_loops',[])]
    return Geom(place, bands, size, (0.0, 0.0), blocks, elk,
                (opts.spacing_node_node, opts.spacing_node_node_between_layers,
                 section_gap))


def _pack_blocks(blocks: Sequence[Dict[str, object]], area_w: float,
                 pack: str, section_gap: float
                 ) -> Tuple[Dict[str, Pt], Dict[str, E.Rect], Tuple[float, float]]:
    """把各段的块铺进可用区：`row` 全部并排；`grid` 超宽换行。

    为什么需要 `grid`：一个工段排成"6 层 1 排"很扁、另一个折成"2 层 3 排"很高，
    若一律并排，高的那条会往下伸出一大片——图面左下角就空出一块废墟。
    "设备不冗杂"不只是不挤，也包括不留大块空白；两种摆法各试一次，由代价函数挑。
    """
    wrap = area_w * FILL_RATIO
    rows: List[List[int]] = [[]]
    if pack == "grid" and wrap > 0:
        used = 0.0
        for k, b in enumerate(blocks):
            add = float(b["w"]) if not rows[-1] else float(b["w"]) + section_gap
            if rows[-1] and used + add > wrap:
                rows.append([])
                used, add = 0.0, float(b["w"])
            rows[-1].append(k)
            used += add
    else:
        rows = [list(range(len(blocks)))]

    place: Dict[str, Pt] = {}
    bands: Dict[str, E.Rect] = {}
    ytop = 0.0
    max_w = 0.0
    for row in rows:
        rh = max(float(blocks[k]["h"]) for k in row)
        x = 0.0
        for k in row:
            b = blocks[k]
            dy = ytop - float(b["h"])
            for eid, rel in (b["place"] or {}).items():           # type: ignore[union-attr]
                place[str(eid)] = (float(rel[0]) + x, float(rel[1]) + dy)
            bands[str(b["sid"])] = (x, dy, x + float(b["w"]), dy + float(b["h"]))
            x += float(b["w"]) + section_gap
        max_w = max(max_w, x - section_gap)
        ytop -= rh + section_gap
    sizew, sizeh = max_w, -ytop - section_gap
    if place:
        dy = (0.0 - (-sizeh))                            # 把内容左下角挪到原点
        for k in place:
            place[k] = (place[k][0], place[k][1] + dy)
        bands = {k: (r[0], r[1] + dy, r[2], r[3] + dy) for k, r in bands.items()}
    return place, bands, (sizew, sizeh)


# ---------------------------------------------------------------------------
# 七、代价函数：把"好看"拆成可算的项
# ---------------------------------------------------------------------------

def _seg_cross(p1: Pt, p2: Pt, p3: Pt, p4: Pt) -> bool:
    """两条**轴对齐**线段的"真交叉"（交点严格落在两条线段内部，共端不算）。"""
    if abs(p1[1] - p2[1]) < 1e-9 and abs(p3[0] - p4[0]) < 1e-9:
        hy, vx = p1[1], p3[0]
        return (min(p1[0], p2[0]) + 1e-6 < vx < max(p1[0], p2[0]) - 1e-6
                and min(p3[1], p4[1]) + 1e-6 < hy < max(p3[1], p4[1]) - 1e-6)
    if abs(p1[0] - p2[0]) < 1e-9 and abs(p3[1] - p4[1]) < 1e-9:
        vx, hy = p1[0], p3[1]
        return (min(p3[0], p4[0]) + 1e-6 < vx < max(p3[0], p4[0]) - 1e-6
                and min(p1[1], p2[1]) + 1e-6 < hy < max(p1[1], p2[1]) - 1e-6)
    return False


def _endpoint(boxes: Dict[str, EqBox], place: Dict[str, Pt], eid: str, ref: str
              ) -> Tuple[Pt, Optional[Pt]]:
    """走线端点与**出线方向**：能取到命名锚点就取锚点，否则退化为包围盒中心。"""
    b = boxes[eid]
    base = place[eid]
    anc = ref_anchor(ref)
    off = b.anchors.get(anc) if anc else None
    d = b.dirs.get(anc) if anc else None
    if off is None:
        off = E.rect_center(b.body)
    return ((base[0] + off[0], base[1] + off[1]), d)


def _trim_ends(path, rects):
    """裁掉规划路径两端插进设备本体的那一段（两端各裁一次，见 `trim_path_at_body`）。"""
    out = list(path)
    for r in rects:
        out = E.trim_path_at_body(out, r, tail=False)
    for r in rects:
        out = E.trim_path_at_body(out, r, tail=True)
    return out


def cost_of(spec: Dict, sections: Sequence[Dict[str, object]], boxes: Dict[str, EqBox],
            geom: Geom, area: E.Rect) -> Tuple[float, Dict[str, float]]:
    """一次排版的代价与分项明细（分项会写进报告，好解释"为什么这样排"）。

    ⚠️ **走线类分项按"实际会画的线"算，不按默认正交**。切换 ELK 之后这两者不再是一回事：
    出图时会把 ELK 折点当默认走线（`plan["pipe_via"]`），而 ELK 的走线**结构上不穿设备**。
    若代价仍按 `ortho_path` 的默认走线去量，就会**给一个即将被修掉的问题打分**——
    搜索会围着"默认走线碰了几台设备"优化，而那个量对最终图面没有影响。
    所以：**能取到 ELK 折点就用折点**；只有规格明确写了 `route`/`via` 的管线
    （引擎不覆盖它）才退回默认走线。
    """
    aw, ah = area[2] - area[0], area[3] - area[1]
    cw, ch = geom.size
    parts: Dict[str, float] = {}

    parts["溢出版面"] = W_OVERFLOW * (max(0.0, cw - aw) / max(aw, 1.0)
                                    + max(0.0, ch - ah) / max(ah, 1.0))
    if cw > 1e-6 and ch > 1e-6 and aw > 1e-6 and ah > 1e-6:
        parts["长宽比失衡"] = W_ASPECT * abs(math.log((cw / ch) / (aw / ah)))
    else:
        parts["长宽比失衡"] = W_ASPECT
    util = (cw * ch) / max(aw * ah, 1.0)
    parts["留白失衡"] = W_UTIL * ((util - TARGET_UTIL) / TARGET_UTIL) ** 2

    mean_size = (sum(b.w + b.h for b in boxes.values())
                 / max(1, 2 * len(boxes)))
    comfort = max(4.0, 0.22 * mean_size)
    min_gap = min(geom.spacing[0], geom.spacing[2]) if geom.spacing else comfort
    parts["净距不足"] = W_CROWD * (max(0.0, comfort - min_gap) / comfort) ** 2

    rects = [(b.eid, (geom.place[b.eid][0] + b.body[0], geom.place[b.eid][1] + b.body[1],
                      geom.place[b.eid][0] + b.body[2], geom.place[b.eid][1] + b.body[3]))
             for b in boxes.values() if b.eid in geom.place]
    paths = _cost_paths(spec, boxes, geom.place, geom.elk.get("routes") or {})
    # ⚠️ 与出图同口径：**先把端点插进设备本体的那一段裁掉**再量。
    # 规划路径的端点同样是设备中心，不裁的话每台大设备都贡献十几毫米"穿设备"，
    # 实测这一项能占到总代价的 68%（73.9 里占 50.1），搜索会围着这个**出图时
    # 根本不会存在的假问题**反复调整，把真正的净距/留白指标挤到一边。
    paths = [_trim_ends(p, [r for _, r in rects]) for p in paths]
    through = 0.0
    for path in paths:
        # 与正式校验同口径：先让开管口段，再量"穿设备"长度
        for sub in E.shrink_path(path, E.NOZZLE_TOLERANCE, E.NOZZLE_TOLERANCE):
            for _eid, r in rects:
                through += E.seg_inside_rect_len(sub[0], sub[1], r)
    cross = 0
    bends = 0
    for path in paths:
        bends += max(0, len(path) - 2)
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            for a1, a2 in zip(paths[i], paths[i][1:]):
                for b1, b2 in zip(paths[j], paths[j][1:]):
                    if _seg_cross(a1, a2, b1, b2):
                        cross += 1
    total_len = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                    for path in paths for a, b in zip(path, path[1:]))
    np_ = max(1, len(paths))
    # 穿设备：**不允许出现**（硬约束；校验里判 error，出图前修到 0）
    parts["穿设备"] = W_THROUGH * (through / (np_ * 60.0))
    # 管线交叉：**允许出现**（用户规定）→ W_CROSS=0，此项恒为 0、不参与候选排序；
    # 交叉数仍可在报告里查 `geom.elk["crossings"]`（各工段层间交叉数）。
    parts["管线交叉"] = W_CROSS * (cross / (np_ * 3.0))
    parts["转折点数"] = W_BEND * (bends / (np_ * 3.0))
    parts["管线总长"] = W_LENGTH * (total_len / max(1.0, aw + ah))
    return sum(parts.values()), parts


def _cost_paths(spec: Dict, boxes: Dict[str, EqBox], place: Dict[str, Pt],
                elk_routes: Dict[str, List[Pt]]) -> List[List[Pt]]:
    """代价用的路径集合：**优先用 ELK 折点**（那才是实际会画的线）。

    规格明确写了 `route` / `via` 的管线不在此列——引擎不覆盖设计者的意图，
    所以它们仍按默认走线估（`_elk_via` 用的是同一套过滤规则，两处必须一致，
    否则代价评估的就不是真正会画的那张图）。
    """
    explicit = {str(p.get("id", "")) for p in spec.get("pipes", [])
                if p.get("route") or p.get("via")}
    ids = {str(e.get("id")) for e in spec.get("equipment", []) if e.get("id")}
    paths: List[List[Pt]] = []
    for p in spec.get("pipes", []):
        line = str(p.get("id", ""))
        frm, to = str(p.get("from", "")), str(p.get("to", ""))
        a, b = ref_owner(frm, ids), ref_owner(to, ids)
        if not a or not b or a not in place or b not in boxes or b not in boxes:
            continue
        r = elk_routes.get(line)
        if r and len(r) >= 2 and line not in explicit:
            paths.append([(float(x), float(y)) for x, y in r])
            continue
        p1, d1 = _endpoint(boxes, place, a, frm)
        p2, d2 = _endpoint(boxes, place, b, to)
        mode = str(p.get("route", "auto"))
        if E.is_stub_route(mode):
            paths.append(E.stub_path(p1, d1, p2, d2, mode=mode[4:] or "auto"))
        else:
            paths.append(E.ortho_path(p1, p2, mode=mode))
    return paths


# ---------------------------------------------------------------------------
# 八、搜索：ELK 选项档案 × 间距档 × 段排布
# ---------------------------------------------------------------------------

def plan_layout(spec: Dict, *, sheet: Optional[str] = None,
                max_evals: int = MAX_EVALS) -> Dict[str, object]:
    """**ELK layered 版面分配主入口**。

    搜索空间只有三样，且每一样都写在 ELK 的术语里：
      * 选项档案（4）——分层策略 / 层内对齐 / 层内排布；
      * 间距倍率（7）——把 ELK 的间距参数整体放大，用不完的余量摊进间距；
      * 段排布（2）——工段并排 / 分行。

    代价函数是**确定性**的（无随机数），所以同一份规格两次排版结果必然一致。

    `layout.sheet == "auto"` 时先选**最小装得下的幅面**再正式排——
    小流程塞进大图幅不是"大气"，是缩在正中一小块（HG 20519.2 允许简单者用 A2）。
    """
    lay = dict(spec.get("layout") or {})
    sheet = str(sheet or lay.get("sheet") or spec.get("sheet", "A1"))
    auto_sheet = sheet.lower() == "auto"

    if auto_sheet:
        chosen, plan, tried = choose_sheet(spec, max_evals)
        plan["sheet_advice"] = chosen
        plan["sheet_tried"] = {sh: round(plan_utilization(p), 3) for sh, p in tried.items()}
        plan["notes"].append(                                   # type: ignore[union-attr]
            f"图幅 auto 选定 {chosen}（各幅面利用率 "
            + "、".join(f"{sh} {100 * plan_utilization(p):.0f}%"
                        for sh, p in tried.items()) + "）")
        return plan

    plan = _plan_on_sheet(spec, sheet, max_evals)
    plan["sheet_advice"] = sheet
    if plan_utilization(plan) < SHEET_MIN_UTIL:
        # 只在"这张图幅确实太空"时才去算推荐幅面（最多 4 次真排，别白花）
        advice, best, tried = choose_sheet(spec, max_evals)
        plan["sheet_advice"] = advice
        plan["sheet_tried"] = {sh: round(plan_utilization(p), 3) for sh, p in tried.items()}
        if advice != sheet:
            plan["notes"].append(                                 # type: ignore[union-attr]
                f"提示：内容只占 {sheet} 的 {100 * plan_utilization(plan):.0f}%，"
                f"用 **{advice}** 更合适（利用率 {100 * plan_utilization(best):.0f}%）"
                f"；或设 layout.sheet=\"auto\" 让引擎自动选幅面")
    return plan


def _plan_on_sheet(spec: Dict, sheet: str, max_evals: int,
                   *, force_scale: Optional[float] = None) -> Dict[str, object]:
    """在**指定图幅**上做一次完整的"ELK 选项 × 间距档 × 段排布"搜索。"""
    lay = dict(spec.get("layout") or {})
    sections = split_sections(spec, max_per_section=lay.get("max_per_section"), sheet=sheet)
    boxes = measure_equipment(spec, sheet)
    area = available_area(spec, sheet, n_notes=len(spec.get("notes") or []))
    work = planning_area(area)

    if not sections or not boxes:
        return {"sections": sections, "counts": count_equipment(spec, sections),
                "placements": {}, "bands": {}, "layers": [], "cost": 0.0, "cost_parts": {},
                "evals": 0, "sheet": sheet, "area": list(area), "work_area": list(work),
                "content_size": [0.0, 0.0], "pack": "row", "spacing": [0.0, 0.0, 0.0],
                "profile": "", "section_gap": BASE_SECTION_GAP, "elk": {},
                "pipe_via": {},
                "literal_coords": scan_literal_coords(spec),
                "notes": ["规格里没有设备，版面分配无事可做"]}

    scales = (float(force_scale),) if force_scale is not None else SPACING_SCALE
    evals = 0
    best: Optional[Tuple[float, Dict[str, float], Geom, str, str, float]] = None
    for profile in ELK_PROFILES:
        for scale in scales:
            if evals >= max_evals:
                break
            import chemcad_process_layout as PL
            packs=('row',) if PL.recognize(spec,set(boxes)) else ('row','grid')
            for pack in packs:
                if evals >= max_evals:
                    break
                geom = layout_sections(spec, sections, boxes, work,
                                       profile=profile, scale=scale, pack=pack)
                if not geom.place:
                    continue
                cost, parts = cost_of(spec, sections, boxes, geom, work)
                evals += 1
                if best is None or cost < best[0] - 1e-9:
                    best = (cost, parts, geom, str(profile["name"]), pack, scale)

    if best is None:                                       # 理论上不会发生
        return _empty_plan(spec, sheet, sections, boxes, area, work)
    cost, parts, geom, pname, pack, scale = best
    # 同一条直链使用原始接点精度，避免毫米三位舍入生成0.0002mm伪折线。
    place = {k: [float(v[0]),float(v[1])] for k, v in geom.place.items()}
    return {
        "sections": sections,
        "counts": count_equipment(spec, sections),
        "placements": place,
        "bands": {k: [round(x, 1) for x in v] for k, v in geom.bands.items()},
        "layers": [b["layers"] for b in geom.sections],
        "layer_bands": [b["layer_bands"] for b in geom.sections],
        "max_layers": max((len(b["layers"]) for b in geom.sections), default=0),  # type: ignore[arg-type]
        "cost": round(cost, 3),
        "cost_parts": {k: round(v, 3) for k, v in sorted(parts.items(), key=lambda kv: -kv[1])},
        "evals": evals,
        "sheet": sheet,
        "area": [round(v, 1) for v in area],
        "work_area": [round(v, 1) for v in work],
        "content_size": [round(geom.size[0], 1), round(geom.size[1], 1)],
        "pack": pack,
        "profile": pname,
        "spacing_scale": scale,
        "spacing": [round(v, 1) for v in geom.spacing],
        "section_gap": round(geom.spacing[2], 1),
        "elk": geom.elk,
        "pipe_via": _elk_via(spec, geom.elk, place),
        "literal_coords": scan_literal_coords(spec),
        "notes": _layout_notes(sections, geom, area, parts, pname, pack),
    }


def _empty_plan(spec: Dict, sheet: str, sections: Sequence[Dict[str, object]],
                boxes: Dict[str, EqBox], area: E.Rect, work: E.Rect) -> Dict[str, object]:
    return {"sections": list(sections), "counts": count_equipment(spec, sections),
            "placements": {}, "bands": {}, "layers": [], "max_layers": 0,
            "cost": 0.0, "cost_parts": {}, "evals": 0, "sheet": sheet,
            "area": [round(v, 1) for v in area], "work_area": [round(v, 1) for v in work],
            "content_size": [0.0, 0.0], "pack": "row", "profile": "", "spacing_scale": 1.0,
            "spacing": [0.0, 0.0, 0.0], "section_gap": BASE_SECTION_GAP, "elk": {},
            "pipe_via": {}, "literal_coords": scan_literal_coords(spec),
            "notes": ["未能在可用区里完成分配"]}


def _elk_via(spec: Dict, elk: Dict[str, object], place: Dict[str, object]
             ) -> Dict[str, List[List[float]]]:
    """把 ELK 算出的走线折点变成出图用的 `via`（去掉两端锚点，只留中间点）。

    只在**规格没有明确表态**的管线上用：规格若写了 `route` 或 `via`，
    那是设计者给的意图（例如"塔顶这根必须 VH 出"），引擎不该覆盖它。
    """
    routes = elk.get("routes") or {}
    explicit: Set[str] = set()
    for p in spec.get("pipes", []):
        if p.get("route") or p.get("via"):
            explicit.add(str(p.get("id", "")))
    out: Dict[str, List[List[float]]] = {}
    for line, path in routes.items():                          # type: ignore[union-attr]
        if line in explicit or len(path) < 3:
            continue
        mid = [[round(x, 3), round(y, 3)] for x, y in path[1:-1]]
        if mid:
            out[line] = mid
    return out


def _layout_notes(sections: Sequence[Dict[str, object]], geom: Geom, area: E.Rect,
                  parts: Dict[str, float], profile: str, pack: str) -> List[str]:
    """把 ELK 的中间结果与代价分项翻译成人话，说明"为什么这么排"。"""
    out: List[str] = [f"共 {len(sections)} 个工段；各工段 "
                      + "、".join(f"{s['name']} {len(s['members'])} 台"   # type: ignore[arg-type]
                                 for s in sections)]
    for b in geom.sections:
        el = b["elk"] or {}
        out.append(f"  · {b['name']}：{len(b['layers'])} 层"            # type: ignore[arg-type]
                   f"、{el.get('crossings', 0)} 处交叉"
                   f"、{el.get('bendpoints', 0)} 个转折"
                   f"、{el.get('dummy_nodes', 0)} 个虚拟节点"
                   f"、{el.get('reversed_edges', 0)} 条回流线（破环时反转）"
                   + ("，折成 %s 排" % el.get("bands") if int(el.get("bands", 1)) > 1 else ""))
    cw, ch = geom.size
    aw, ah = area[2] - area[0], area[3] - area[1]
    out.append(f"ELK 档案 {profile}，工段{('并排' if pack == 'row' else '分行')}；"
               f"同层净距 {geom.spacing[0]:.0f} mm、层间距 {geom.spacing[1]:.0f} mm、"
               f"工段间距 {geom.spacing[2]:.0f} mm")
    out.append(f"内容 {cw:.0f}×{ch:.0f} mm，可用区 {aw:.0f}×{ah:.0f} mm，"
               f"利用率 {100 * cw * ch / max(1.0, aw * ah):.0f}%")
    if parts.get("溢出版面", 0.0) > 0.5:
        out.append("⚠ 内容超出可用区：请改大图幅、减少单段设备数，或缩小间距档/工段间距")
    return out


def choose_sheet(spec: Dict, max_evals: int = MAX_EVALS
                 ) -> Tuple[str, Dict[str, object], Dict[str, Dict[str, object]]]:
    """挑图幅：**从 A1 起**（本技能标准图幅 = 浙工大 PFD 实测 841×594），装不下才升 A0。

    判据只有一条：**内容不超出可用区**。

    为什么不再"挑最小装得下的"（旧版按 A3→A2→A1→A0 升）：那样 9 台设备的图会被判去 A3，
    而浙工大那份 PFD **33 个图框清一色 A1**——"标准图幅"是交付口径的一部分，不是省纸。
    为什么不再拿"利用率下限"当判据（更早的旧版是这么做的）：ELK 版的间距是**输入参数**，
    分配步会主动把余量摊进间距把版面填到 `FILL_RATIO`；于是"能不能填满"
    取决于间距上限而不是图纸大小，拿它当判据会把该选小图的情形选成大图。
    利用率只用来提示"这张确实偏空"。A3/A2 只在规格里**显式**指定时才用。
    """
    tried: Dict[str, Dict[str, object]] = {}
    chosen: Optional[str] = None
    for sh in SHEET_AUTO_ORDER:
        p = _plan_on_sheet(spec, sh, max_evals, force_scale=1.0)   # 最小间距先试装得下不
        tried[sh] = p
        if not p["placements"]:
            continue
        if p["cost_parts"].get("溢出版面", 0.0) <= 0.5:
            chosen = sh
            break
    if chosen is None:
        chosen = SHEET_AUTO_ORDER[-1]
    full = _plan_on_sheet(spec, chosen, max_evals)
    if full["placements"]:
        tried[chosen] = full
    return chosen, (full if full["placements"] else tried[chosen]), tried


# ---------------------------------------------------------------------------
# 九、字面坐标体检（自动排版前的"魔数"预警）
# ---------------------------------------------------------------------------

#: 规格里**位置型**字段：自动排版一旦移动设备，写死的坐标就可能失效
_LITERAL_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("pipes", "from"), ("pipes", "to"), ("pipes", "via"),
    ("signals", "from"), ("signals", "to"), ("signals", "via"),
    ("instruments", "at"),
    ("loops", "controller_at"), ("loops", "sub_controller_at"),
    ("offpage", "at"), ("texts", "at"),
    ("equipment", "beside"), ("equipment", "tag_at"),
)


def scan_literal_coords(spec: Dict) -> List[str]:
    """列出规格里的**字面坐标**（魔数）。

    自动排版会移动设备，而 `"620,430"` 这类坐标不会跟着走——
    结果是"线还连着、但接到空气里"。技能一贯要求**用锚点或管线取点，禁写魔数**，
    这里把违反项显式列出来，供出图前改引用。
    """
    out: List[str] = []
    for section, field in _LITERAL_FIELDS:
        for i, item in enumerate(spec.get(section) or []):
            if field not in item:
                continue
            v = item[field]
            label = str(item.get("id") or item.get("tag") or item.get("loop_id") or f"#{i}")
            if isinstance(v, str) and "," in v and "@" not in v:
                try:
                    float(v.split(",")[0]); float(v.split(",")[1])
                    out.append(f"{section}[{label}].{field} = {v!r}")
                    continue
                except (ValueError, IndexError):
                    pass  # 不是字面坐标（含引用串）→ 交给下方列表 / 引用分支处理
            if isinstance(v, (list, tuple)) and len(v) == 2 and all(
                    isinstance(x, (int, float)) for x in v):
                out.append(f"{section}[{label}].{field} = {list(v)}")
            elif isinstance(v, (list, tuple)) and v and all(
                    isinstance(x, (list, tuple)) and len(x) == 2 for x in v):
                out.append(f"{section}[{label}].{field} = {[list(x) for x in v]}")
    return out


# ---------------------------------------------------------------------------
# 十、落位：把计划真正写进图纸
# ---------------------------------------------------------------------------

def shift_group(g: Group, dx: float, dy: float) -> None:
    """平移一个符号实例——实体、基点与**锚点**一起挪。

    锚点必须跟着走：管线是 `cd.pipe()` 用引用解析锚点得到的，
    漏挪锚点会出现"设备动了、管子还接在原来那点"（而且不会报错）。
    """
    if dx == 0.0 and dy == 0.0:
        return
    for e in g.ents:
        shift_ent(e, dx, dy)
    g.base = (g.base[0] + dx, g.base[1] + dy)
    anc = g.meta.get("anchors")
    if isinstance(anc, dict):
        g.meta["anchors"] = {k: (float(v[0]) + dx, float(v[1]) + dy)
                             for k, v in anc.items()}          # type: ignore[union-attr]


def apply_plan(cd: E.ChemDrawing, spec: Dict, plan: Dict[str, object],
               *, force: bool = False) -> Dict[str, object]:
    """按计划平移设备。返回搬迁摘要。

    钉住规则：**写了 `at` 的设备默认不动**（作者给了坐标就是给了意图），
    `layout.force=true` 或被 `pin` 显式说明时才由引擎接管。
    """
    place = plan.get("placements") or {}
    pinned: List[str] = []
    moved: List[str] = []
    for e in spec.get("equipment", []):
        eid = str(e.get("id", ""))
        g = cd.objects.get(eid)
        if g is None:
            continue
        has_at = e.get("at") is not None
        is_pin = bool(e.get("pin", False))
        if (has_at and not force) or (is_pin and not force):
            if eid in place:
                pinned.append(eid)
            continue
        tgt = place.get(eid)                                     # type: ignore[union-attr]
        if not tgt:
            continue
        dx = float(tgt[0]) - g.base[0]
        dy = float(tgt[1]) - g.base[1]
        shift_group(g, dx, dy)
        moved.append(eid)
    return {"moved": moved, "pinned": pinned,
            "note": ("混用「给了坐标」与「不给坐标」的工段不利于版面统一："
                     "给了坐标的一律视为钉住。想整体重排用 layout.force=true"
                     if pinned and moved else "")}


# ---------------------------------------------------------------------------
# 十一、版面缺陷修正：结构化探测 → 修补提示 → 重建
# ---------------------------------------------------------------------------

def detect_collisions(cd: E.ChemDrawing,
                      clearance: float = E.SYMBOL_CLEARANCE) -> Dict[str, object]:
    """一次性取出三类关系型缺陷的**明细**（引擎结构化探测器的薄封装）。"""
    sym = E.detect_symbol_overlaps(cd, clearance)
    txt = E.detect_text_over_lines(cd)
    text_pairs = E.detect_text_over_texts(cd)
    thr = E.detect_pipe_through_equipment(cd)
    frames = E.detect_pipe_over_stream_frames(cd)
    pipe_spacing = E.detect_pipe_spacing(cd)
    nozzle_clearance = E.detect_pipe_over_exchanger_nozzles(cd)
    alignment=E.shared_port_alignment(cd)[1]
    equipment_nozzles=E.detect_pipe_over_equipment_nozzles(cd)
    label_positions = E.validate_equipment_label_positions(cd)
    return {
        "symbol_overlap": [h for h in sym if h["overlap"]],
        "symbol_near": [h for h in sym if not h["overlap"]],
        "text_over_line": txt,
        "text_overlap": text_pairs,
        "pipe_through": thr,
        "pipe_over_stream_frame": frames,
        "equipment_label_position": label_positions,
        "pipe_spacing": pipe_spacing,
        "exchanger_nozzle_clearance": nozzle_clearance,
        "shared_port_alignment": alignment,
        "equipment_nozzle_clearance": equipment_nozzles,
        "counts": {"symbol_overlap": len([h for h in sym if h["overlap"]]),
                   "symbol_near": len([h for h in sym if not h["overlap"]]),
                   "text_over_line": len(txt),
                   "text_overlap": len(text_pairs),
                   "pipe_through": len(thr),
                   "pipe_over_stream_frame": len(frames),
                   "equipment_label_position": len(label_positions),
                   "pipe_spacing": len(pipe_spacing),
                   "exchanger_nozzle_clearance": len(nozzle_clearance),
                   "shared_port_alignment": len(alignment),
                   "equipment_nozzle_clearance": len(equipment_nozzles)},
    }


def _pipe_dirs(cd: E.ChemDrawing, spec: Dict,
               line_no: str) -> Tuple[Optional[Pt], Optional[Pt]]:
    """取一条管线两端的**出线方向**（从规格的 from/to 引用反查锚点）。"""
    for p in spec.get("pipes", []):
        if str(p.get("id")) == line_no:
            return (cd.anchor_dir_of(str(p.get("from", ""))),
                    cd.anchor_dir_of(str(p.get("to", ""))))
    return None, None


def _equip_rects(cd: E.ChemDrawing) -> Dict[str, E.Rect]:
    out: Dict[str, E.Rect] = {}
    for g in cd.iter_groups():
        if g.kind != "equipment":
            continue
        r = E.group_rect(g, layers=("EQUIP",))
        if r is not None:
            out[E._obj_label(g)] = r
    return out


def unfit_shift(cd: E.ChemDrawing) -> Pt:
    """出图最后一步「整体居中平移」的位移量。

    ⚠️ **修补提示必须在"平移前"的坐标系里表达**。`fit_to_sheet` 是在建图末尾
    把整张图平移居中，而 `repair_*` 读的是**平移后**的几何；直接把平移后的绝对坐标
    写进 `pipe_via`，下一轮重建时它会被当作平移前的坐标用——偏移量正好等于两次
    平移之差。实测表现为"绕行点明明算对了，重建后管线跑去了别处"
    （`PL-RET` 的绕行点 x 一样、y 差了 50 mm，于是仍然穿塔）。
    所以：凡是写进提示的**绝对坐标**，先减掉这里的位移。
    """
    fit = cd.meta.get("fit") or {}
    sh = fit.get("shift") or [0.0, 0.0]        # type: ignore[union-attr]
    return (float(sh[0]), float(sh[1]))        # type: ignore[index]


def _unshift(p: Pt, s: Pt) -> Pt:
    return (p[0] - s[0], p[1] - s[1])


def _unshift_rect(r: E.Rect, s: Pt) -> E.Rect:
    return (r[0] - s[0], r[1] - s[1], r[2] - s[0], r[3] - s[1])


def repair_pipes(cd: E.ChemDrawing, spec: Dict, hints: Dict[str, object],
                 det: Dict[str, object]) -> int:
    """给穿设备的管线换走线方式，或沿冲突设备四条边各绕一次。

    候选集很小但命中率高：① 换 5 种正交走线方式；② 沿**每台冲突设备**的四条边各绕一次。
    评分口径 = 「所有设备合计穿行长度」优先、管线总长次之。

    ⚠️ **按管线聚合后再决策**（同一条管线可能同时穿两台设备）：
    早先是"每处冲突各自挑最优"，同一条管线的两次决策互相覆盖（后一处决定前一处），
    于是每一轮都在两个解之间来回跳——实测 `pipe_through` 在 5 / 8 之间反复。
    现在一条管线一轮只出一个决策，且候选与评分都只依赖当前几何，
    所以同一张图重跑得到同一结果（幂等），迭代才收得住。
    """
    thr = det.get("pipe_through") or []
    if not thr:
        return 0
    by_line: Dict[str, List[Dict[str, object]]] = {}
    for hit in thr:                                              # type: ignore[union-attr]
        by_line.setdefault(str(hit["line"]), []).append(hit)     # type: ignore[arg-type]
    routes = dict(hints.get("pipe_route") or {})                 # type: ignore[arg-type]
    vias = dict(hints.get("pipe_via") or {})                     # type: ignore[arg-type]
    sh = unfit_shift(cd)                         # 提示写的是"平移前"的坐标
    erec = {k: _unshift_rect(r, sh) for k, r in _equip_rects(cd).items()}
    n = 0
    for line_no, hs in by_line.items():
        rec = cd.pipes.get(line_no)
        if rec is None or len(rec.path) < 2:
            continue
        p1, p2 = _unshift(rec.path[0], sh), _unshift(rec.path[-1], sh)
        cands: List[Tuple[str, Optional[list]]] = [(m, None) for m in ROUTE_MODES]
        seen: Set[Tuple[str, str]] = set()
        for hit in hs:
            r = _unshift_rect(hit["rect"], sh)                   # type: ignore[arg-type]
            # 三档让距：贴着绕常撞到隔壁那台，让远一点才真的空出来
            for pad in (ROUTE_PAD, ROUTE_PAD + 16.0, ROUTE_PAD + 34.0):
                for via in ([[p1[0], r[3] + pad], [p2[0], r[3] + pad]],
                            [[p1[0], r[1] - pad], [p2[0], r[1] - pad]],
                            [[r[0] - pad, p1[1]], [r[0] - pad, p2[1]]],
                            [[r[2] + pad, p1[1]], [r[2] + pad, p2[1]]]):
                    key = ("auto", repr(via))
                    if key not in seen:
                        seen.add(key)
                        cands.append(("auto", via))
        d1, d2 = _pipe_dirs(cd, spec, line_no)
        best: Optional[Tuple[float, float, str, Optional[list]]] = None
        for mode, via in cands:
            try:
                if E.is_stub_route(mode):
                    path = E.stub_path(p1, d1, p2, d2, mode=mode[4:] or "auto", via=via)
                else:
                    path = E.ortho_path(p1, p2, mode=mode, via=via)
            except (ValueError, TypeError):
                continue  # 端点非法或布线无解 → 跳过该条管线，不中断整图
            # 对**所有**设备开口算穿行量：只盯着当前冲突那台，改完常会穿到隔壁
            inside = 0.0
            for sub in E.shrink_path(path, E.NOZZLE_TOLERANCE, E.NOZZLE_TOLERANCE):
                for _name, er in erec.items():
                    inside += E.seg_inside_rect_len(sub[0], sub[1], er)
            length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                         for a, b in zip(path, path[1:]))
            key2 = (inside, length)
            if best is None or key2 < (best[0], best[1]):
                best = (inside, length, mode, via)
        if best is None:
            continue
        if best[3] is not None:
            vias[line_no] = best[3]
            routes.pop(line_no, None)
        else:
            routes[line_no] = best[2]
            vias.pop(line_no, None)
        n += 1
    hints["pipe_route"], hints["pipe_via"] = routes, vias
    return n


def repair_equipment_overlaps(cd: E.ChemDrawing, spec: Dict,
                              hints: Dict[str, object], det: Dict[str, object]) -> int:
    """设备**之间**相撞时，沿穿透最浅的一轴各推开一半。

    ⚠️ 只在两端都是设备时才动——「设备 vs 阀门/仪表」不该搬设备：
    排版阶段已经保证设备格位互不相交，此时报出来的相撞几乎都是
    "阀门/仪表压到设备上"，该由 `repair_symbol_slides` 滑动那只阀/表。
    早先不分对象一律推设备，结果是**每轮都在推、每轮又撞出新的**，
    缺陷总数来回震荡不收敛（实测 44→37→32→33→34 就是这么来的）。
    """
    hits = det.get("symbol_overlap") or []
    if not hits:
        return 0
    kinds = {E.obj_label(g): g.kind for g in cd.iter_groups()}
    off = {k: list(v) for k, v in dict(hints.get("equipment_offset") or {}).items()}  # type: ignore[arg-type]
    n = 0
    for h in hits:                                               # type: ignore[union-attr]
        a, b = str(h["a"]), str(h["b"])
        if kinds.get(a) != "equipment" or kinds.get(b) != "equipment":
            continue
        axis, need = E.symbol_overlap_axis(h["rect_a"], h["rect_b"])   # type: ignore[arg-type]
        push = (abs(need) + E.SYMBOL_CLEARANCE) / 2.0
        idx = 0 if axis == "x" else 1
        sign = 1.0 if h["rect_a"][idx] >= h["rect_b"][idx] else -1.0   # type: ignore[index]
        for name, sgn in ((a, sign), (b, -sign)):
            cur = off.get(name, [0.0, 0.0])
            cur[idx] += push * sgn
            off[name] = cur
        n += 1
    hints["equipment_offset"] = off
    return n


def _slide_candidates(t0: float) -> List[float]:
    """沿管线滑动的候选 `t`——由近及远，先试"挪一点就够"的。"""
    out: List[float] = []
    for dt in (0.03, -0.03, 0.06, -0.06, 0.10, -0.10, 0.15, -0.15,
               0.22, -0.22, 0.30, -0.30, 0.40, -0.40, 0.50, -0.50):
        nt = round(min(0.95, max(0.05, t0 + dt)), 3)
        if abs(nt - t0) > 1e-9 and nt not in out:
            out.append(nt)
    return out


def _rect_swapped(r: E.Rect) -> E.Rect:
    """把矩形绕自身中心转 90°——符号在竖管上时包围盒长宽互换。"""
    cx, cy = E.rect_center(r)
    w, h = E.rect_size(r)
    return (cx - h / 2.0, cy - w / 2.0, cx + h / 2.0, cy + w / 2.0)


def repair_symbol_slides(cd: E.ChemDrawing, spec: Dict, hints: Dict[str, object],
                         det: Dict[str, object]) -> int:
    """把与别的符号相撞的**阀门/仪表/控制器**挪开。

    三类对象各有"最小代价的挪法"：
      * 阀门/仪表挂在管线上 → 改 `t` 沿管线滑一段（不动拓扑，闭环也不受影响）；
      * 控制器由回路自动布置 → 改 `controller_offset`（相对测量点的偏移）；
      * 设备会牵动全局，最后一手才搬（由 `repair_equipment_overlaps` 处理）。

    **候选要就地评过再选**，不能"先挪 0.06 再看"：盲目步进会把一只阀挪到另一只阀
    身上——实测出现过"挪一步 → 撞上邻居 → 再挪 → 又撞回来"的来回震荡，
    缺陷总数在 36~44 之间反复。这里直接算出**挪过去之后的包围盒**
    （沿管线平移 + 竖管上长宽互换），跟图纸上所有其他符号比一遍，
    取"净距最大"的那个位置——一次到位。
    """
    hits = (det.get("symbol_overlap") or []) + (det.get("symbol_near") or [])
    if not hits:
        return 0
    slide: Dict[str, Tuple[str, float]] = {}
    for v in spec.get("valves", []):
        tag = str(v.get("tag", ""))
        if tag and v.get("on"):
            slide[tag] = (str(v["on"]), float(v.get("t", 0.5)))
    for ins in spec.get("instruments", []):
        tag = str(ins.get("tag", ""))
        if tag and ins.get("on"):
            slide[tag] = (str(ins["on"]), float(ins.get("t", 0.5)))
    # 控制器：`cd.objects` 里以 CTRL_<loop_id> 登记，但**标签是位号**
    # （`_obj_label` 先取 tag），所以不能凭名字前缀找，要反查登记表
    ctrl_of: Dict[str, str] = {}
    for key, g in cd.objects.items():
        if key.startswith("CTRL_"):
            ctrl_of[E.obj_label(g)] = key[len("CTRL_"):]
    loops_of = {str(lp.get("loop_id", "")): lp for lp in (spec.get("loops") or [])}

    rects: Dict[str, E.Rect] = {}
    for g in cd.iter_groups():
        r = E.group_rect(g)
        if r is not None:
            rects[E.obj_label(g)] = r

    vt = dict(hints.get("valve_t") or {})                        # type: ignore[arg-type]
    io = {k: list(v) for k, v in dict(hints.get("instrument_off") or {}).items()}  # type: ignore[arg-type]
    co = {k: list(v) for k, v in dict(hints.get("controller_offset") or {}).items()}  # type: ignore[arg-type]
    n = 0
    for h in hits:                                               # type: ignore[union-attr]
        for name in (str(h["a"]), str(h["b"])):
            if name in slide:
                line, t0 = slide[name]
                rec = cd.pipes.get(line)
                g = next((x for x in cd.iter_groups() if E.obj_label(x) == name), None)
                if rec is None or g is None:
                    continue
                r0 = rects.get(name)
                if r0 is None:
                    continue
                base0 = g.base
                best_t, best_gap = None, E.rect_gap(r0, r0)
                # 候选**以规格给出的 t0 为中心**，不以上一轮结果为中心——
                # 否则每轮都会"从当前位置再往外挪一点"，符号一路漂走且结果不可复现
                for nt in _slide_candidates(t0):
                    p = rec.point_at(nt)
                    dx, dy = p[0] - base0[0], p[1] - base0[1]
                    same_dir = (abs(dx) < 1e-9) == (abs(dy) < 1e-9)
                    r2 = E.expand_rect(r0, 0.0)
                    r2 = (r2[0] + dx, r2[1] + dy, r2[2] + dx, r2[3] + dy)
                    if not same_dir:                             # 竖管上长宽互换
                        r2 = _rect_swapped(r2)
                    gap = min((E.rect_gap(r2, rr) for lab, rr in rects.items()
                               if lab != name), default=1e9)
                    if gap > best_gap + 1e-9:
                        best_gap, best_t = gap, nt
                # 仪表（气泡）是**骑在管线上**画的，管子在设备旁边时必然与设备相交——
                # 沿管滑动救不了，要沿**法向**让开（真实图纸里气泡本来就带引线挂在管侧）。
                # 阀门不能这么做：阀体离开管线就不是阀了。
                kind = next((g.kind for g in cd.iter_groups()
                             if E.obj_label(g) == name), "")
                if kind == "instrument":
                    seg = E._segment_containing(rec, min(max(best_t or 0.5, 0.0), 1.0))
                    perps = [(0.0, 1.0), (0.0, -1.0)] if seg is None or \
                        abs(seg[1][0] - seg[0][0]) >= abs(seg[1][1] - seg[0][1]) else \
                        [(1.0, 0.0), (-1.0, 0.0)]
                    cur_off = io.get(name) or [0.0, 0.0]
                    for mag in (9.0, 13.0, 18.0, 24.0, 30.0):
                        for px, py in perps:
                            cand = [cur_off[0] + px * mag, cur_off[1] + py * mag]
                            r2 = (r0[0] + cand[0], r0[1] + cand[1],
                                  r0[2] + cand[0], r0[3] + cand[1])
                            gap2 = min((E.rect_gap(r2, rr) for lab, rr in rects.items()
                                        if lab != name), default=1e9)
                            if gap2 > best_gap + 1e-9:
                                best_gap, best_t, io[name] = gap2, None, cand
                if best_t is not None and best_gap > 0.0:
                    vt[name] = round(best_t, 3)
                    n += 1
                    break
                if io.get(name) is not None and best_gap > 0.0 and name not in vt:
                    n += 1
                    break
            elif name in ctrl_of:
                lid = ctrl_of[name]
                base = (loops_of.get(lid) or {}).get("controller_offset") or [0.0, 0.0]
                r0 = rects.get(name)
                if r0 is None:
                    continue
                best_off, best_gap = None, -1e9
                for dy in (0.0, 24.0, -24.0, 40.0, -40.0, 56.0, -56.0):
                    for dx in (0.0, 30.0, -30.0, 50.0, -50.0):
                        if dx == 0.0 and dy == 0.0:
                            continue
                        r2 = (r0[0] + dx, r0[1] + dy, r0[2] + dx, r0[3] + dy)
                        gap = min((E.rect_gap(r2, rr) for lab, rr in rects.items()
                                   if lab != name), default=1e9)
                        if gap > best_gap + 1e-9:
                            best_gap, best_off = gap, [float(base[0]) + dx,
                                                       float(base[1]) + dy]
                if best_off is not None and best_gap > 0.0:
                    co[lid] = best_off
                    n += 1
                    break
    hints["valve_t"], hints["instrument_off"], hints["controller_offset"] = vt, io, co
    return n


def polish_texts(cd: E.ChemDrawing) -> Dict[str, object]:
    """把**压线**的文字挪到最近的空位。

    独立文字可以移位；annotation 组内的文字保持相对位置，
    避免把分数式设备标注和物流参数框拆散。
    说明框/标题栏内的文字（位于封闭框内）不参与，避免破坏悬挂缩进与栏格对齐。
    """
    W, H = SHEET_SIZES.get(cd.sheet.upper(), SHEET_SIZES["A1"])
    m = 4.0
    group_moves, group_unresolved = [], []
    for group in cd.iter_groups():
        if group.kind != "annotation":
            continue
        if group.name in ("STREAM_DATA", "EQUIPMENT_FOOTER") or group.ref.startswith('equipment:'):
            # 菱形和参数框的相对几何固定，选位在布线入口完成。
            continue
        own = {id(e) for e in group.ents}
        base = E.group_rect(group)
        if base is None:
            continue
        # 参数框/分数式位号整体平移；引出点固定在线上。
        others = [E.group_rect(g, layers=("EQUIP", "EQUIP_IN")) if g.kind == "equipment"
                  else E.group_rect(g) for g in cd.iter_groups() if g is not group and g.kind != "frame"]
        others = [r for r in others if r is not None]
        external = []
        for e in cd.iter_ents():
            if id(e) in own:
                continue
            if isinstance(e, Text) and e.text.strip():
                others.append(E.text_rect(e))
            if isinstance(e, Polyline) and e.closed and resolve_layer(e.layer) == "TABLE":
                others.append(E.rect_of_points(e.pts))
            if resolve_layer(e.layer) not in E._TEXT_LINE_SKIP:
                external.extend(E.entity_segments(e))
        cells = [E.text_rect(t) for t in group.ents if isinstance(t, Text) and t.text.strip()]

        def shifted(r, dx, dy):
            return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)

        def group_clear(dx, dy):
            rect = shifted(base, dx, dy)
            if rect[0] < m or rect[1] < m or rect[2] > W - m or rect[3] > H - m:
                return False
            if any(r is not None and E.rect_gap(rect, r) < 0.6 for r in others):
                return False
            return not any(E.seg_inside_rect_len(a, b, shifted(r, dx, dy)) > 1e-9
                           for r in cells for a, b in external)

        if group_clear(0.0, 0.0):
            continue
        offsets = [(dx, dy) for step in TEXT_NUDGE_STEPS + (40.0, 56.0, 80.0)
                   for dx, dy in ((0,-step),(0,step),(step,0),(-step,0),
                                  (step,-step),(-step,-step),(step,step),(-step,step))]
        chosen = next(((dx,dy) for dx,dy in offsets if group_clear(dx,dy)), None)
        if chosen is None:
            group_unresolved.append(f"标注组 {group.name} 未找到整体净空")
            continue
        dx, dy = chosen
        for e in group.ents:
            anchor = e.p1 if isinstance(e, Line) and e.note == "annotation_leader" else None
            shift_ent(e, dx, dy)
            if anchor is not None:
                e.p1 = anchor
        group.base = (group.base[0] + dx, group.base[1] + dy)
        group_moves.append(f"标注组 {group.name} 整体挪 ({dx:+.0f},{dy:+.0f})")
    segs: List[Tuple[Pt, Pt, str]] = []
    boxes: List[E.Rect] = []
    for it in cd.items:
        ents = it.ents if isinstance(it, Group) else [it]
        for e in ents:
            lay = resolve_layer(e.layer)
            if lay in E._TEXT_LINE_SKIP:
                continue
            if isinstance(e, Polyline) and e.closed:
                r = E.rect_of_points(e.pts)
                if r and (r[2] - r[0]) >= E._CONTAINER_MIN[0] and (r[3] - r[1]) >= E._CONTAINER_MIN[1]:
                    boxes.append(r)
            for a, b in E.entity_segments(e):
                segs.append((a, b, lay))

    texts = [t for t in cd.iter_ents()
             if isinstance(t, Text) and t.text.strip()
             and resolve_layer(t.layer) not in E._TEXT_LINE_SKIP]
    grouped = {id(t) for g in cd.iter_groups() if g.kind == "annotation"
               for t in g.ents if isinstance(t, Text)}
    inside = {id(t) for t in texts
              if t.note=="stream_no_fixed" or id(t) in grouped or any(E.rect_contains(b, E.rect_center(E.text_rect(t))) for b in boxes)}
    movable = [t for t in texts if id(t) not in inside]
    other_rects = [E.text_rect(t) for t in texts if id(t) in inside]

    def clear(rect: E.Rect) -> bool:
        if rect[0] < m or rect[1] < m or rect[2] > W - m or rect[3] > H - m:
            return False
        for a, b, _lay in segs:
            if E.seg_inside_rect_len(a, b, rect) > 1e-9:
                return False
        for o in other_rects:
            if E.rect_gap(rect, o) < 0.6:
                return False
        return True

    moved: List[str] = group_moves
    unresolved: List[str] = group_unresolved
    for t in movable:
        base = E.text_rect(t)
        if clear(base):
            other_rects.append(base)
            continue
        hit = False
        for step in TEXT_NUDGE_STEPS:
            cands = [(0.0, -step), (step, 0.0), (-step, 0.0), (0.0, step),
                     (step * 0.7, -step * 0.7), (-step * 0.7, -step * 0.7),
                     (step * 0.7, step * 0.7), (-step * 0.7, step * 0.7)]
            for dx, dy in cands:
                trial = E.expand_rect(base, 0.0)
                trial = (trial[0] + dx, trial[1] + dy, trial[2] + dx, trial[3] + dy)
                if clear(trial):
                    shift_ent(t, dx, dy)
                    other_rects.append(trial)
                    moved.append(f"「{t.text[:14]}」({t.layer}) 挪 ({dx:+.0f},{dy:+.0f})")
                    hit = True
                    break
            if hit:
                break
        if not hit:
            other_rects.append(base)
            unresolved.append(f"「{t.text[:14]}」({t.layer}) @({t.pt[0]:.0f},{t.pt[1]:.0f})")
    return {"moved": moved, "unresolved": unresolved,
            "fixed": len(moved), "left": len(unresolved)}




def repair_equipment_footer_overlaps(cd: E.ChemDrawing, spec: Dict, hints: Dict[str, object]) -> int:
    """底部标注共用Y；与设备一起调整X，保持对应关系。"""
    rows = sorted((g for g in cd.iter_groups() if g.name == 'EQUIPMENT_FOOTER'),
                  key=lambda g: g.base[0])
    entries = {str(eq['id']): eq for eq in spec.get('equipment', [])}
    offsets = dict(hints.get('equipment_offset') or {})
    right, changed = -float('inf'), 0
    for group in rows:
        rect = E.group_rect(group)
        owner = group.ref.partition(':')[2]
        eq = entries[owner]
        delta = max(0., right+.7-rect[0])
        if delta > 0 and (eq.get('at') is None or (spec.get('layout') or {}).get('force')):
            previous = offsets.get(owner) or [0., 0.]
            offsets[owner] = [float(previous[0])+delta, float(previous[1])]
            changed += 1
        else:
            delta = 0.
        right = max(right, rect[2]+delta)
    hints['equipment_offset'] = offsets
    return changed


def empty_hints() -> Dict[str, object]:
    return {"equipment_offset": {}, "pipe_route": {}, "pipe_via": {},
            "valve_t": {}, "pipe_tag_t": {}, "instrument_off": {},
            "controller_offset": {}, "route_avoid": []}


def learn_route_avoid(cd: E.ChemDrawing, spec: Dict, hints: Dict[str, object],
                      det: Dict[str, object]) -> int:
    """把**上一轮被压到的文字框**记成下一轮的硬障碍，供 A* 绕开。

    为什么需要这条"学习"通路：A* 布线跑在**设备落位之后、管线画出之前**，
    它看得见的是那一刻图上的障碍。而管线自己会带来一批**新文字**
    ——管道号/等级、流股数据块、仪表气泡、以及最后由图纸家具加上去的说明框。
    这些字在布线时还不存在，所以 A* 无从避让；等它们出现时才发现压线。

    于是走"探测 → 记障碍 → 重建"：这一轮压到谁，下一轮就把谁的框当硬障碍。
    每轮只学**增量**，且只学**文字**——设备本体本来就在 A* 的硬障碍里，
    再记一遍没有意义。

    ⚠️ 与 `repair_pipes` 同一坑：`repair_*` 读的是**平移后**的几何，
    而提示要在**平移前**的坐标系里表达（下一轮重建时用）。所以先减 `unfit_shift`。
    """
    hits = list(det.get("text_over_line") or []) + list(det.get("pipe_over_stream_frame") or [])
    if not hits:
        return 0
    sh = unfit_shift(cd)
    seen = {tuple(round(v, 3) for v in r)                    # type: ignore[union-attr]
            for r in (hints.get("route_avoid") or [])}       # type: ignore[union-attr]
    out = list(hints.get("route_avoid") or [])               # type: ignore[arg-type]
    n = 0
    for h in hits:                                           # type: ignore[union-attr]
        if h.get("part") == "diamond":
            continue
        r = h.get("protected_rect") or h.get("rect")
        if r and h.get('other_rect'):
            # 两组标注相撞也须重建承载管线；只学习框/引线，菱形仍允许自身承载线。
            r = E.expand_rect(tuple(r),E.STREAM_ANNOTATION_GAP)
        if not r:
            continue
        rr = _unshift_rect(tuple(r), sh)                     # type: ignore[arg-type]
        key = tuple(round(v, 3) for v in rr)
        if key in seen:
            continue
        seen.add(key)
        out.append([round(v, 3) for v in rr])
        n += 1
    hints["route_avoid"] = out
    return n


def format_plan(plan: Dict[str, object], moved: Optional[Dict[str, object]] = None) -> str:
    """把版面计划渲染成人可读文本（出图时打印，也便于写进报告）。"""
    lines = ["=" * 68, "工段划分 · 设备数量统计 · ELK layered 版面分配"]
    counts = plan.get("counts") or {}
    lines.append(f"设备合计 {counts.get('total', 0)} 台，分 {len(plan.get('sections') or [])} 个工段"
                 f"（幅面 {plan.get('sheet')}，可用区 {plan.get('area')}）")
    for s in (counts.get("sections") or []):                     # type: ignore[union-attr]
        types = "、".join(f"{k}×{v}" for k, v in (s["types"] or {}).items())   # type: ignore[union-attr]
        lines.append(f"  · {s['name']}：{s['count']} 台（{types}）")
    for n in (plan.get("notes") or []):                          # type: ignore[union-attr]
        lines.append(f"  {n}")
    cp = plan.get("cost_parts") or {}
    if cp:
        lines.append("代价分项（越小越好）："
                     + "，".join(f"{k} {v:.2f}" for k, v in cp.items()))     # type: ignore[union-attr]
    if plan.get("profile"):
        lines.append(f"ELK 档案 {plan.get('profile')}，间距倍率 {plan.get('spacing_scale')}"
                     f"，段排布 {plan.get('pack')}")
    lines.append(f"搜索评估 {plan.get('evals', 0)} 次，代价 {plan.get('cost')}")
    if moved:
        lines.append(f"落位：移动 {len(moved.get('moved') or [])} 台"
                     f"，钉住 {len(moved.get('pinned') or [])} 台")
        if moved.get("note"):
            lines.append(f"  ⚠ {moved['note']}")
    lits = plan.get("literal_coords") or []
    if lits:
        lines.append(f"⚠ 规格里有 {len(lits)} 处**字面坐标**（自动排版后会失效，建议改引用）：")
        for x in lits[:8]:                                       # type: ignore[union-attr]
            lines.append(f"    {x}")
        if len(lits) > 8:
            lines.append(f"    …共 {len(lits)} 处")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import json
    from collections import Counter

    _ap = argparse.ArgumentParser(description="版面工具：静/动设备分类自检（PFD 版面原则用）")
    _ap.add_argument("--classify", metavar="spec.json",
                     help="打印规格里每台设备的静/动分类（静设备在上、动设备在下）")
    _a = _ap.parse_args()
    if _a.classify:
        with open(_a.classify, "r", encoding="utf-8") as _f:
            _sp = json.load(_f)
        _cls = classify_spec(_sp)
        for _e in _sp.get("equipment", []):
            _id = str(_e.get("id"))
            print(f"  {_id:<12} {str(_e.get('name', '')):<14} "
                  f"{str(_e.get('type', '')):<10} -> {_cls.get(_id)}")
        print("  合计:", dict(Counter(_cls.values())))
    else:
        _ap.print_help()
