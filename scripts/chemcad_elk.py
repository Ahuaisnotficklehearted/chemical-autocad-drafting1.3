#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_elk.py —— 分层版面引擎：Eclipse Layout Kernel **layered** 算法的零依赖实现。

为什么单独一个模块：本模块只回答"给定一张有向图与节点尺寸，怎么摆"，
**完全不认识化工**——设备、管线、工段都是 `chemcad_layout.py` 的事。
把通用图布局与工程语义分开，好处是每个阶段可以**逐个单测**：
破环对不对、分层可不可行、交叉数降没降、层内有没有重叠、走线碰不碰节点。
不需要每次都把图建出来、再靠目视判断。

## 算法谱系

对应 `org.eclipse.elk.alg.layered` 的八阶段流水线，选项名一一对齐：

| 阶段 | ELK 选项 | 本模块 |
|---|---|---|
| ① 破环 | `cycleBreaking.strategy` = GREEDY | `break_cycles()`（Eades–Lin–Smyth） |
| ② 分层 | `layering.strategy` = LONGEST_PATH / NETWORK_SIMPLEX | `assign_layers()` |
| ③ 折行 | `wrapping.strategy` = SINGLE_EDGE，`cutting.strategy` = ARD | `cut_into_bands()` |
| ④ 虚拟节点 | 内部阶段 | `insert_dummy_nodes()` |
| ⑤ 交叉最小化 | `crossingMinimization.strategy` = LAYER_SWEEP | `minimize_crossings()` |
| ⑥ 层内排布 | `nodePlacement.strategy` = BRANDES_KOEPF / SIMPLE | `assign_layer_coordinates()` |
| ⑦ 端口放置 | `portConstraints` = FIXED_SIDE / FIXED_ORDER / FREE | `place_ports()` |
| ⑧ 边布线 | `edgeRouting` = ORTHOGONAL / POLYLINE | `route_edges()` |

## 三条必须写清楚的边界（避免"文档写了、代码没做"）

1. **`NETWORK_SIMPLEX` 只实现"收窄"阶段**。完整网络单纯形要在紧致生成树上
   算割值（cut value）并做基交换；本实现做的是它的前两步——最长路给可行解、
   双向收窄把有自由度的节点收紧（`_tighten_layers`）。结果等价于
   "边总跨度尽可能小"，但**不保证全局最优**。这是刻意的：化工流程图是近链状 DAG，
   收窄已吃掉绝大部分收益，而割值/基交换的实现复杂度与出错面远大于其边际收益。
2. **不做超边（hyperedge）合并布线**。ELK 会把共用源端口的边并成超边再分开引出；
   本实现给每条边独立分配槽位，只保证**同源边的槽位相邻**（视觉上仍然成束）。
   化工图上"同一管口引两路"少见，而超边合并会让槽位分配与折点统计复杂化。
3. **只实现 `direction=RIGHT`**（流程从左到右，与读图习惯一致）。其余方向传入即抛
   `ValueError`——宁可报错，也不要静默按横向画出一张方向不对的图。

## 坐标系（与 `chemcad_engine` 一致，不是 ELK 的屏幕坐标）

ELK 内部用 y 向下的屏幕坐标，`NORTH` 指屏幕上方。本模块**直接在图纸坐标系
（y 向上）里计算**：`NORTH` = `+y`、`SOUTH` = `-y`、`WEST` = `-x`、`EAST` = `+x`，
与 `chemcad_engine.anchor_direction` 的返回值同义。端口朝向、出线方向、
绕行方向三者于是不需要任何坐标系转换——少一层转换，就少一处出错的机会。

## 一条最值钱的性质：**走线不穿设备是版面结构保证的**

* 每个节点在**本层内**的纵向区间互不重叠（`_enforce_separation` 兜底硬保证）；
  所以一条水平段沿 `y = 某节点的 y` 走，绝不会穿过同层的另一个节点；
* 竖段一律落在**层间隙**里，而层间隙按定义不含任何节点；
* 长边已被虚拟节点拆链，中间那几段水平线走在**虚拟节点的 y** 上，
  而虚拟节点在层里占着一格——那一格是空的。

所以"管线穿设备"不是靠布线后处理躲开的，而是版面结构本身排除的；
`_avoid()` 只是**断言式**的兜底（真被触发说明上面某条假设破了）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from chemcad_ir import simplify as _normalize

Pt = Tuple[float, float]

NORTH, EAST, SOUTH, WEST = "NORTH", "EAST", "SOUTH", "WEST"
FIXED_SIDE, FIXED_ORDER, FREE = "FIXED_SIDE", "FIXED_ORDER", "FREE"
SIDES: Tuple[str, ...] = (NORTH, EAST, SOUTH, WEST)
_SIGNS: Dict[str, Pt] = {NORTH: (0.0, 1.0), SOUTH: (0.0, -1.0),
                         EAST: (1.0, 0.0), WEST: (-1.0, 0.0)}


def side_of(direction: Optional[Pt], fallback: str = EAST) -> str:
    """出线方向 → 端口所在边（竖直优先：化工接管多是上下出线）。"""
    if direction is None:
        return fallback
    dx, dy = float(direction[0]), float(direction[1])
    if abs(dy) >= abs(dx):
        return NORTH if dy > 0 else SOUTH
    return EAST if dx > 0 else WEST


def outward(side: str) -> Pt:
    """边的朝外单位向量。"""
    return _SIGNS.get(side, (1.0, 0.0))


# ---------------------------------------------------------------------------
# 一、选项
# ---------------------------------------------------------------------------

@dataclass
class LayoutOptions:
    """ELK layered 选项（字段名下划线化，便于与官方文档对照）。"""

    direction: str = "RIGHT"

    cycle_breaking: str = "GREEDY"
    layering_strategy: str = "NETWORK_SIMPLEX"       # NETWORK_SIMPLEX | LONGEST_PATH

    wrapping: str = "SINGLE_EDGE"                    # OFF | SINGLE_EDGE
    cutting_strategy: str = "ARD"

    crossing_minimization: str = "LAYER_SWEEP"       # LAYER_SWEEP | NONE
    thoroughness: int = 7
    greedy_switch: bool = True

    node_placement: str = "BRANDES_KOEPF"            # BRANDES_KOEPF | SIMPLE
    bk_fixed_alignment: str = "BALANCED"             # NONE|LEFTUP|RIGHTUP|LEFTDOWN|RIGHTDOWN|BALANCED
    bk_edge_straightening: str = "IMPROVE_STRAIGHTNESS"   # NONE | IMPROVE_STRAIGHTNESS

    port_constraints: str = FIXED_SIDE               # FIXED_SIDE | FIXED_ORDER | FREE

    spacing_node_node: float = 14.0
    spacing_node_node_between_layers: float = 34.0
    spacing_edge_node: float = 8.0
    spacing_edge_edge: float = 6.0

    max_layers_per_band: int = 99
    edge_routing: str = "ORTHOGONAL"                 # ORTHOGONAL | POLYLINE

    consider_model_order: bool = True
    separate_connected_components: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def options_from(profile: Optional[Dict[str, object]] = None) -> LayoutOptions:
    """按 dict 覆盖默认选项（未知键忽略）。"""
    o = LayoutOptions()
    for k, v in (profile or {}).items():
        if k in LayoutOptions.__dataclass_fields__:
            setattr(o, k, v)
    return o


# ---------------------------------------------------------------------------
# 二、图模型
# ---------------------------------------------------------------------------

@dataclass
class ElkPort:
    """一个连接点。`offset` 相对**节点中心**（图纸坐标，y 向上）。"""

    id: str
    owner: str
    side: str = EAST
    offset: Optional[Pt] = None
    order: int = 0
    x: float = 0.0
    y: float = 0.0

    def placed_at(self) -> Pt:
        return (self.x, self.y)


@dataclass
class ElkNode:
    id: str
    w: float
    h: float
    section: str = ""
    model: int = 0
    priority: float = 0.0
    x: float = 0.0
    y: float = 0.0
    layer: int = 0
    order: int = 0
    band: int = 0
    band_layer: int = 0
    is_dummy: bool = False
    dummy_for: str = ""
    ports: Dict[str, ElkPort] = field(default_factory=dict)
    out_edges: List[str] = field(default_factory=list)
    in_edges: List[str] = field(default_factory=list)

    @property
    def left(self) -> float:
        return self.x - self.w / 2.0

    @property
    def right(self) -> float:
        return self.x + self.w / 2.0

    @property
    def bottom(self) -> float:
        return self.y - self.h / 2.0

    @property
    def top(self) -> float:
        return self.y + self.h / 2.0

    def rect(self) -> Tuple[float, float, float, float]:
        return (self.left, self.bottom, self.right, self.top)


@dataclass
class ElkEdge:
    id: str
    source_node: str
    source_port: str
    target_node: str
    target_port: str
    reversed: bool = False
    chain: List[str] = field(default_factory=list)
    route: List[Pt] = field(default_factory=list)
    slot: Dict[int, float] = field(default_factory=dict)

    @property
    def is_self_loop(self) -> bool:
        return self.source_node == self.target_node


class ElkGraph:
    """有向图容器。节点必须带尺寸——**布局不猜尺寸**，尺寸错了版面必然不对。"""

    def __init__(self, options: Optional[LayoutOptions] = None):
        self.options: LayoutOptions = options or LayoutOptions()
        if self.options.direction != "RIGHT":
            raise ValueError("chemcad_elk 只实现 direction=RIGHT（流程从左到右）")
        self.nodes: Dict[str, ElkNode] = {}
        self.edges: Dict[str, ElkEdge] = {}
        self.model_order: List[str] = []
        self.layers: List[List[str]] = []
        self.metrics: Dict[str, object] = {}

    # -- 构图 ---------------------------------------------------------------

    def add_node(self, nid: str, w: float, h: float, *, section: str = "",
                 priority: float = 0.0, model: Optional[int] = None) -> ElkNode:
        n = ElkNode(id=nid, w=max(1e-6, float(w)), h=max(1e-6, float(h)),
                    section=str(section), priority=float(priority),
                    model=len(self.model_order) if model is None else int(model))
        self.nodes[nid] = n
        self.model_order.append(nid)
        return n

    def add_port(self, owner: str, pid: str, side: str = EAST,
                 offset: Optional[Pt] = None, order: int = 0) -> ElkPort:
        if owner not in self.nodes:
            raise KeyError(f"端口 {pid!r} 的宿主节点 {owner!r} 不存在")
        p = ElkPort(id=pid, owner=owner, side=side if side in SIDES else EAST,
                    offset=None if offset is None else (float(offset[0]), float(offset[1])),
                    order=int(order))
        self.nodes[owner].ports[pid] = p
        return p

    def add_edge(self, eid: str, src: Tuple[str, str], dst: Tuple[str, str]) -> ElkEdge:
        for nid, pid in (src, dst):
            if nid not in self.nodes:
                raise KeyError(f"边 {eid!r} 的端点节点 {nid!r} 不存在")
            if pid not in self.nodes[nid].ports:
                self.add_port(nid, pid, EAST)
        e = ElkEdge(id=eid, source_node=src[0], source_port=src[1],
                    target_node=dst[0], target_port=dst[1])
        self.edges[eid] = e
        self.nodes[src[0]].out_edges.append(eid)
        self.nodes[dst[0]].in_edges.append(eid)
        return e

    # -- 便捷 ---------------------------------------------------------------

    def real_nodes(self) -> List[str]:
        return [n for n in self.model_order if not self.nodes[n].is_dummy]

    def port(self, nid: str, pid: str) -> ElkPort:
        return self.nodes[nid].ports[pid]

    def src_port(self, e: ElkEdge) -> ElkPort:
        """边的**实际**出线端口（已处理破环方向）。"""
        if e.reversed:
            return self.port(e.target_node, e.target_port)
        return self.port(e.source_node, e.source_port)

    def dst_port(self, e: ElkEdge) -> ElkPort:
        if e.reversed:
            return self.port(e.source_node, e.source_port)
        return self.port(e.target_node, e.target_port)

    def ordered_endpoints(self, e: ElkEdge) -> Tuple[str, str]:
        """破环后，边的"上游 → 下游"节点 id（跨层方向由它决定）。"""
        if e.reversed:
            return e.target_node, e.source_node
        return e.source_node, e.target_node

    def neighbours(self, nid: str, layer: int) -> List[str]:
        """`nid` 在指定层上的邻居（走全部边，含虚拟链上被拆开的边）。"""
        out: List[str] = []
        for eid in self.nodes[nid].out_edges + self.nodes[nid].in_edges:
            e = self.edges[eid]
            other = e.target_node if e.source_node == nid else e.source_node
            if other in self.nodes and self.nodes[other].layer == layer:
                out.append(other)
        return out


# ---------------------------------------------------------------------------
# 三、阶段 ①：破环
# ---------------------------------------------------------------------------

def break_cycles(g: ElkGraph) -> List[str]:
    """把有向图变成 DAG：**反转尽量少的边**，让某条拓扑序成立。

    回流线（塔顶回流、再沸器返塔、循环物流）在化工图上到处都是，
    这一步不是可选的——分层阶段只对 DAG 有定义。

    Eades–Lin–Smyth：反复摘掉**汇**（放右序）与**源**（放左序）；
    剩下必有环时，摘掉 `出度 − 入度` 最大者。最后落在
    「右序 → 左序」之间的边就是要反转的边。并列一律按模型序取，
    所以结果**可复现**（没有随机数）。
    """
    if g.options.cycle_breaking != "GREEDY":
        raise ValueError(f"未实现的破环策略 {g.options.cycle_breaking!r}")

    outdeg = {n: len(g.nodes[n].out_edges) for n in g.nodes}
    indeg = {n: len(g.nodes[n].in_edges) for n in g.nodes}
    # 邻接快照（撤销节点时按快照扣度，避免误扣已被摘掉的邻居）
    nbr_out: Dict[str, List[str]] = {n: [] for n in g.nodes}
    nbr_in: Dict[str, List[str]] = {n: [] for n in g.nodes}
    for eid in g.edges:
        e = g.edges[eid]
        if e.is_self_loop:
            continue
        nbr_out[e.source_node].append(e.target_node)
        nbr_in[e.target_node].append(e.source_node)

    alive = set(g.nodes)
    left: List[str] = []
    right: List[str] = []
    by_model = sorted(g.nodes, key=lambda n: g.nodes[n].model)

    while alive:
        progressed = True
        while progressed:
            progressed = False
            for n in by_model:
                if n in alive and outdeg[n] == 0:
                    right.insert(0, n)
                    alive.discard(n)
                    _dec_deg(n, nbr_out, nbr_in, outdeg, indeg)
                    progressed = True
                    break
            if progressed:
                continue
            for n in by_model:
                if n in alive and indeg[n] == 0:
                    left.append(n)
                    alive.discard(n)
                    _dec_deg(n, nbr_out, nbr_in, outdeg, indeg)
                    progressed = True
                    break
        if not alive:
            break
        pick = max(by_model, key=lambda n: (outdeg[n] - indeg[n], -g.nodes[n].model)
                   if n in alive else (-10 ** 9, 0))
        left.append(pick)
        alive.discard(pick)
        _dec_deg(pick, nbr_out, nbr_in, outdeg, indeg)

    position = {n: i for i, n in enumerate(left + right)}
    reversed_ids: List[str] = []
    for eid in sorted(g.edges):
        e = g.edges[eid]
        if e.is_self_loop:
            continue
        if position[e.source_node] > position[e.target_node]:
            e.reversed = True
            reversed_ids.append(eid)
    return reversed_ids


def _dec_deg(n: str, nbr_out: Dict[str, List[str]], nbr_in: Dict[str, List[str]],
             outdeg: Dict[str, int], indeg: Dict[str, int]) -> None:
    """摘掉 `n` 时更新邻居的度。"""
    for t in nbr_out[n]:
        if t in indeg:
            indeg[t] = max(0, indeg[t] - 1)
    for s in nbr_in[n]:
        if s in outdeg:
            outdeg[s] = max(0, outdeg[s] - 1)
    outdeg[n] = 0
    indeg[n] = 0


# ---------------------------------------------------------------------------
# 四、阶段 ②：分层
# ---------------------------------------------------------------------------

def assign_layers(g: ElkGraph) -> List[List[str]]:
    """把节点分到层（写 `node.layer`），层号 = 沿流程方向的深度。

    * `LONGEST_PATH`：`layer[v] = max(layer[pred]) + 1`。保证每条边跨 ≥1 层。
    * `NETWORK_SIMPLEX`：先取最长路作可行解，再**双向收窄**（`_tighten_layers`）。

    空层会被压掉并重新编号——空的层带会在版面上留出一条无用的缝。
    """
    strat = g.options.layering_strategy
    if strat not in ("LONGEST_PATH", "NETWORK_SIMPLEX"):
        raise ValueError(f"未实现的分层策略 {strat!r}")

    adj: Dict[str, List[Tuple[str, str]]] = {n: [] for n in g.nodes}
    indeg: Dict[str, int] = {n: 0 for n in g.nodes}
    for eid in sorted(g.edges):
        e = g.edges[eid]
        if e.is_self_loop:
            continue
        s, t = g.ordered_endpoints(e)
        adj[s].append((t, "out"))
        adj[t].append((s, "in"))
        indeg[t] += 1

    ready = sorted([n for n in g.nodes if indeg[n] == 0], key=lambda n: g.nodes[n].model)
    order: List[str] = []
    deg = dict(indeg)
    while ready:
        n = ready.pop(0)
        order.append(n)
        for other, kind in adj[n]:
            if kind == "out":
                deg[other] -= 1
                if deg[other] == 0:
                    ready.append(other)
                    ready.sort(key=lambda x: g.nodes[x].model)
    if len(order) < len(g.nodes):                    # 破环后不该发生；兜底不静默丢点
        order += [n for n in sorted(g.nodes, key=lambda x: g.nodes[x].model)
                  if n not in set(order)]

    layer = {n: 0 for n in g.nodes}
    for n in order:
        for other, kind in adj[n]:
            if kind == "out":
                layer[other] = max(layer[other], layer[n] + 1)

    if strat == "NETWORK_SIMPLEX":
        layer = _tighten_layers(layer, order, adj)

    for n in g.nodes:
        g.nodes[n].layer = layer[n]
    _refresh_layers(g, compact=True)
    return g.layers


def _tighten_layers(layer: Dict[str, int], order: Sequence[str],
                    adj: Dict[str, List[Tuple[str, str]]]) -> Dict[str, int]:
    """收窄：把"离后继还远"的节点往上提，直到贴着后继。

    每次上提的上界是 `min(后继层) − 1`，它必然 ≥ `max(前驱层) + 1`
    （否则原分层就不可行），所以**只缩边、不动可行性**，迭代单调收敛。

    与最长路的差别：最长路只把节点往**前驱**方向压，短分支会被留在后面；
    双向收窄把短分支也往**后继**方向拉回来，于是层数更少、边更短。
    """
    changed = True
    guard = 0
    while changed and guard < 64:
        guard += 1
        changed = False
        for n in reversed(order):
            succ = [layer[o] for o, k in adj[n] if k == "out"]
            if not succ:
                continue
            hi = min(succ) - 1
            if hi > layer[n]:
                layer[n] = hi
                changed = True
        for n in order:
            pred = [layer[o] for o, k in adj[n] if k == "in"]
            lo = (max(pred) + 1) if pred else 0
            if lo > layer[n]:
                layer[n] = lo
                changed = True
    base = min(layer.values()) if layer else 0
    if base:
        for n in layer:
            layer[n] -= base
    return layer


def _refresh_layers(g: ElkGraph, *, compact: bool = True) -> None:
    """按 `layer` 重建 `g.layers`（层内次序暂按模型序；`compact=True` 时压掉空层）。"""
    if not g.nodes:
        g.layers = []
        return
    max_l = max(g.nodes[n].layer for n in g.nodes)
    buckets: List[List[str]] = [[] for _ in range(max_l + 1)]
    for n in sorted(g.nodes, key=lambda x: (g.nodes[x].layer, g.nodes[x].model)):
        buckets[g.nodes[n].layer].append(n)
    if compact:
        keep = [b for b in buckets if b]
        for new_i, b in enumerate(keep):
            for n in b:
                g.nodes[n].layer = new_i
        buckets = keep
    for li, layer in enumerate(buckets):
        for k, n in enumerate(layer):
            g.nodes[n].order = k
    g.layers = buckets


# ---------------------------------------------------------------------------
# 五、阶段 ③：折行（层序列切段）
# ---------------------------------------------------------------------------

def cut_into_bands(n_layers: int, target_band_width: float,
                   layer_widths: Sequence[float], gap: float,
                   *, max_layers_per_band: int = 99) -> List[List[int]]:
    """把连续层序列切成若干**段（band）**，控制单段总宽。

    对应 ELK 的 `wrapping.strategy`。不分段时，一条 12 层的近链状流程会
    12 层一字排开——图面成一条极扁的长条，读图人得横着扫完整张图；
    分 3 段叠起来，就是工程上常见的「流程图分排绘制」。

    切法（`cutting.strategy = ARD`）：自左向右累加层宽，**超过目标带宽就切**；
    目标带宽由调用方按图幅长宽比算。切点只落在层边界，
    所以段内层序完整，段与段的衔接（跨段管线）交给布线阶段。
    """
    if n_layers <= 0:
        return []
    limit = max(1, int(max_layers_per_band))
    bands: List[List[int]] = []
    cur: List[int] = []
    used = 0.0
    for i in range(n_layers):
        w = float(layer_widths[i]) if i < len(layer_widths) else 0.0
        add = w if not cur else w + gap
        over = target_band_width > 0 and cur and (used + add > target_band_width)
        too_many = len(cur) >= limit
        if cur and (over or too_many):
            bands.append(cur)
            cur, used = [i], w
        else:
            cur.append(i)
            used += add
    if cur:
        bands.append(cur)
    return bands


# ---------------------------------------------------------------------------
# 六、阶段 ④：虚拟节点
# ---------------------------------------------------------------------------

def insert_dummy_nodes(g: ElkGraph) -> Dict[str, List[str]]:
    """跨多层的边在每一中间层插一个**虚拟节点**；返回 `边 id → 节点链`。

    为什么必须插：交叉最小化与层内排布都是"层内相邻次序"上的操作。
    一条从第 0 层直奔第 5 层的边若不拆，它在这两层之间"占哪条缝"是没有定义的——
    交叉数会算少，BK 对齐也会把它当成不存在的邻居。

    虚拟节点的**横向尺寸取 `spacing_edge_edge`**：它在层里占的那一道宽，
    正是两条走线之间应有的净距——所以这一步顺便把"走线间距"也定了。
    """
    chains: Dict[str, List[str]] = {}
    for eid in sorted(g.edges):
        e = g.edges[eid]
        if e.is_self_loop:
            e.chain = [e.source_node]
            chains[eid] = e.chain
            continue
        s, t = g.ordered_endpoints(e)
        ls, lt = g.nodes[s].layer, g.nodes[t].layer
        lo, hi = min(ls, lt), max(ls, lt)
        chain = [s]
        for k in range(lo + 1, hi):
            did = f"\x00d{eid}L{k}"
            d = g.add_node(did, g.options.spacing_edge_edge, 1e-6)
            d.is_dummy, d.dummy_for, d.layer = True, eid, k
            chain.append(did)
        chain.append(t)
        if ls > lt:
            chain.reverse()
        e.chain = chain
        chains[eid] = chain
    if any(g.nodes[n].is_dummy for n in g.nodes):
        max_l = max(g.nodes[n].layer for n in g.nodes)
        buckets: List[List[str]] = [[] for _ in range(max_l + 1)]
        for n in sorted(g.nodes, key=lambda x: (g.nodes[x].layer, g.nodes[x].model)):
            buckets[g.nodes[n].layer].append(n)
        for li, layer in enumerate(buckets):
            for k, n in enumerate(layer):
                g.nodes[n].order = k
        g.layers = buckets
    return chains


# ---------------------------------------------------------------------------
# 七、阶段 ⑤：交叉最小化
# ---------------------------------------------------------------------------

def bilayer_crossings(upper: Sequence[str], lower: Sequence[str],
                      pairs: Sequence[Tuple[str, str]]) -> int:
    """两层之间的交叉数（Barth–Jünger–Mutzel：化为逆序对，O(E log V)）。

    交叉数只在**相邻两层**上有定义；长边已由虚拟节点拆成逐层边，
    所以逐对层求和就是总交叉数——这也是上一步必须插虚拟节点的第二个理由。
    """
    pos_u = {n: i for i, n in enumerate(upper)}
    pos_l = {n: i for i, n in enumerate(lower)}
    seq = sorted((pos_u[a], pos_l[b]) for a, b in pairs
                 if a in pos_u and b in pos_l)
    if len(seq) < 2:
        return 0
    n_max = max(p[1] for p in seq) + 2
    bit = [0] * (n_max + 2)

    total = 0
    for k, (_i, j) in enumerate(seq):
        idx = j + 1
        s = 0
        t = idx
        while t > 0:
            s += bit[t]
            t -= t & (-t)
        total += k - s                       # 已插入中 j 更大者 = 逆序对
        t = idx
        while t <= n_max:
            bit[t] += 1
            t += t & (-t)
    return total


def _layer_pairs(g: ElkGraph, i: int) -> List[Tuple[str, str]]:
    """第 i 层与第 i+1 层之间的近邻层边（上游在前）。"""
    out: List[Tuple[str, str]] = []
    for eid in g.edges:
        e = g.edges[eid]
        if e.is_self_loop:
            continue
        s, t = g.ordered_endpoints(e)
        ls, lt = g.nodes[s].layer, g.nodes[t].layer
        if ls == i and lt == i + 1:
            out.append((s, t))
    return out


def count_crossings(g: ElkGraph) -> int:
    total = 0
    for i in range(max(0, len(g.layers) - 1)):
        total += bilayer_crossings(g.layers[i], g.layers[i + 1], _layer_pairs(g, i))
    return total


def minimize_crossings(g: ElkGraph) -> int:
    """层扫描：按**重心**排序每层，再用**双侧相邻交换**收尾。

    与 ELK 的 `LAYER_SWEEP` 同构：反复做前向（用上一层算重心）与
    后向（用下一层算重心）扫描，全程保留交叉数最小的那一版；
    `thoroughness` 决定轮数，`greedy_switch` 打开时每层再扫一遍相邻对交换。
    """
    if g.options.crossing_minimization == "NONE" or len(g.layers) < 2:
        return count_crossings(g)

    rounds = max(1, int(g.options.thoroughness) // 2)
    best = [list(x) for x in g.layers]
    best_c = count_crossings(g)

    for _ in range(rounds):
        for direction in ("forward", "backward"):
            idxs = range(1, len(g.layers)) if direction == "forward" \
                else range(len(g.layers) - 2, -1, -1)
            for i in idxs:
                adj_i = i - 1 if direction == "forward" else i + 1
                if not (0 <= adj_i < len(g.layers)):
                    continue
                _sort_layer_by_barycenter(g, i, adj_i)
                if g.options.greedy_switch:
                    _greedy_switch(g, i)
            if direction == "forward":
                c = count_crossings(g)
                if c < best_c:
                    best_c, best = c, [list(x) for x in g.layers]
        if best_c == 0:
            break

    g.layers = best
    for li, layer in enumerate(g.layers):
        for k, n in enumerate(layer):
            g.nodes[n].order = k
    return best_c


def _sort_layer_by_barycenter(g: ElkGraph, i: int, adj_i: int) -> None:
    """把第 i 层按"邻居在 adj_i 层中的位置重心"排序。

    平局先比中位数、再比模型序；**无邻居的节点保持原位**
    （用它在层内的当前下标当重心）——否则孤立设备会被重心排序吸到一头。
    """
    pos = {n: k for k, n in enumerate(g.layers[adj_i])}
    here = {n: k for k, n in enumerate(g.layers[i])}
    key: List[Tuple[float, float, int, str]] = []
    for n in g.layers[i]:
        nbrs = sorted(pos[m] for m in g.neighbours(n, adj_i) if m in pos)
        if nbrs:
            bary = sum(nbrs) / len(nbrs)
            med = float(nbrs[len(nbrs) // 2])
        else:
            bary = med = float(here[n])
        key.append((bary, med, g.nodes[n].model, n))
    key.sort(key=lambda k: (k[0], k[1], k[2], k[3]) if g.options.consider_model_order
             else (k[0], k[1], k[3]))
    g.layers[i] = [k[3] for k in key]


def _greedy_switch(g: ElkGraph, i: int) -> None:
    """相邻交换：只交换**真正降低交叉数**的相邻对，反复到不再改善。

    这是 ELK `greedySwitch.type = TWO_SIDED`：交换时**上一层与下一层两侧都看**。
    只看一侧会把交叉"从左边赶到右边"，总数不降反升。
    """
    layer = g.layers[i]
    if len(layer) < 2:
        return
    improved = True
    guard = 0
    while improved and guard < 8 * len(layer) + 8:
        guard += 1
        improved = False
        for k in range(len(layer) - 1):
            before = _local_crossings(g, i, k, k + 1)
            layer[k], layer[k + 1] = layer[k + 1], layer[k]
            after = _local_crossings(g, i, k, k + 1)
            if after < before:
                improved = True
            else:
                layer[k], layer[k + 1] = layer[k + 1], layer[k]


def _local_crossings(g: ElkGraph, i: int, k1: int, k2: int) -> int:
    """只与第 i 层第 k1 / k2 两个位置有关的交叉数（两侧都算）。

    两节点（k1 在 k2 之前）的邻居对 (p, q) 相交当且仅当 p 排在 q **之后**，
    即 `(p − q) × (k2 − k1) > 0`（k2 > k1 时退化为 `p > q`）。
    """
    n1, n2 = g.layers[i][k1], g.layers[i][k2]
    total = 0
    for adj_i in (i - 1, i + 1):
        if not (0 <= adj_i < len(g.layers)):
            continue
        pos = {n: t for t, n in enumerate(g.layers[adj_i])}
        a1 = [pos[m] for m in g.neighbours(n1, adj_i) if m in pos]
        a2 = [pos[m] for m in g.neighbours(n2, adj_i) if m in pos]
        for p in a1:
            for q in a2:
                if (p - q) * (k2 - k1) > 0:
                    total += 1
    return total


# ---------------------------------------------------------------------------
# 八、阶段 ⑥：层内排布（Brandes–Köpf）
# ---------------------------------------------------------------------------

def _sep(g: ElkGraph, upper: str, lower: str) -> float:
    """同层上下相邻两节点中心的最小距离 = 半高之和 + 净距。"""
    return g.nodes[upper].h / 2.0 + g.options.spacing_node_node + g.nodes[lower].h / 2.0


def _nbrs_in_layer(g: ElkGraph, nid: str, adj: int) -> List[str]:
    nbrs = g.neighbours(nid, adj)
    nbrs.sort(key=lambda m: g.nodes[m].order)
    return nbrs


def _type1_conflict(g: ElkGraph, i: int, v: str, m: str, adj_i: int) -> bool:
    """类型 1 冲突：把 (m, v) 对齐会**与一条内段交叉**。

    内段 = 邻居唯一的那条边（它本来就"直"）。Brandes–Köpf 只允许对齐
    不产生交叉的邻居——否则"拉直这条边"会"折断另一条"，净效果更差。
    """
    pos = {n: k for k, n in enumerate(g.layers[i])}
    pos_adj = {n: k for k, n in enumerate(g.layers[adj_i])}
    if m not in pos_adj or v not in pos:
        return False
    for u in g.layers[adj_i]:
        nbrs = _nbrs_in_layer(g, u, i)
        if len(nbrs) != 1:                       # 只看内段
            continue
        w = nbrs[0]
        if w not in pos:
            continue
        if (pos_adj[u] - pos_adj[m]) * (pos[w] - pos[v]) < 0:
            return True
    return False


def _bk_align_blocks(g: ElkGraph, up: bool, left: bool
                     ) -> Tuple[Dict[str, str], Dict[str, Dict[int, str]]]:
    """垂直对齐：把节点并进**块**（一个块在每层至多一个节点）。

    返回 `(block_of, block_layers)`：`block_of[node]` 是块代表，
    `block_layers[块代表][层号]` 是该块在那层的节点。

    关键在"并块"而不是"逐个连边"：处理第 i 层时，节点 v 与它在相邻层的
    中位邻居 m **所在的整块**合并——于是 v 自动接到一条已经向下延伸了若干层的
    竖直块上。块内节点共用同一个纵向坐标，对齐出来就是**直线段**。
    """
    block_of: Dict[str, str] = {n: n for n in g.nodes}
    blayers: Dict[str, Dict[int, str]] = {n: {g.nodes[n].layer: n} for n in g.nodes}
    members: Dict[str, List[str]] = {n: [n] for n in g.nodes}

    idxs = range(1, len(g.layers)) if up else range(len(g.layers) - 2, -1, -1)
    for i in idxs:
        adj = i - 1 if up else i + 1
        if not (0 <= adj < len(g.layers)):
            continue
        far = -1 if left else 10 ** 9
        seq = list(g.layers[i]) if left else list(reversed(g.layers[i]))
        for v in seq:
            if len(members[block_of[v]]) > 1:
                continue                                  # v 已并入某块
            nbrs = _nbrs_in_layer(g, v, adj)
            if not nbrs:
                continue
            mp = (len(nbrs) - 1) / 2.0
            picks = nbrs[int(math.floor(mp)):int(math.ceil(mp)) + 1]
            for m in picks:
                rb = block_of[m]
                if g.nodes[rb].layer > g.nodes[v].layer and up:
                    pass
                if i in blayers[rb]:                      # 该块在这层已有人 → 合并会撞车
                    continue
                if _type1_conflict(g, i, v, m, adj):
                    continue
                pm = g.nodes[m].order
                if not ((pm > far) if left else (pm < far)):
                    continue
                # 并块：v 的块 v + m 的块 rb
                rv = block_of[v]
                if rv == rb:
                    continue
                for n in members[rb]:
                    block_of[n] = rv
                    blayers[rv][g.nodes[n].layer] = n
                members[rv] += members[rb]
                del blayers[rb]
                del members[rb]
                far = pm
                break
    return block_of, blayers


def _bk_compaction(g: ElkGraph, block_of: Dict[str, str],
                   members: Dict[str, List[str]], left: bool) -> Dict[str, float]:
    """水平压实：块内取紧、块间用 `shift` 让开。

    `sink[块]` 记"该块被压向哪一边"；两块争同一位置时用 `shift`
    记下需要整体平移的量——这正是 BK 里 `sink`/`shift` 的用途。
    """
    x: Dict[str, float] = {}
    sink = {r: r for r in members}
    shift: Dict[str, float] = {r: float("inf") for r in members}
    calls = [0]

    def place_block(r: str) -> float:
        calls[0] += 1
        if calls[0] > 200000:
            return x.get(r, 0.0)
        if r in x:
            return x[r]
        x[r] = 0.0
        for w in members[r]:
            li = g.nodes[w].layer
            k = g.layers[li].index(w)
            uk = k - 1 if left else k + 1
            if not (0 <= uk < len(g.layers[li])):
                continue
            u = g.layers[li][uk]
            ru = block_of[u]
            place_block(ru)
            sep = _sep(g, u, w) if left else _sep(g, w, u)
            if sink[r] == r:
                sink[r] = sink[ru]
            if sink[r] != sink[ru]:
                if left:
                    shift[sink[ru]] = min(shift[sink[ru]], x[r] - x[ru] - sep)
                else:
                    shift[sink[ru]] = min(shift[sink[ru]], x[ru] - x[r] - sep)
            else:
                if left:
                    x[r] = max(x[r], x[ru] + sep)
                else:
                    x[r] = min(x[r], x[ru] - sep)
        return x[r]

    for r in sorted(members, key=lambda r: (g.nodes[r].layer, g.nodes[r].model)):
        place_block(r)

    out: Dict[str, float] = {}
    for n in g.nodes:
        r = block_of[n]
        s = sink[r]
        out[n] = x[r] + (0.0 if not math.isfinite(shift[s]) else shift[s])
    return out


def _bk_straighten(g: ElkGraph) -> None:
    """拉直：同层相邻且**等高**的一对节点，若交换位置能减少折弯就交换。

    对应 ELK `nodePlacement.bk.edgeStraightening = IMPROVE_STRAIGHTNESS`。
    等高是必需的——不等高时交换会改变层内净距，破坏"不重叠"这条硬约束；
    等高时 `sep` 对称，交换保持两节点间距不变，所以是安全的。
    """
    for li, layer in enumerate(g.layers):
        for k in range(len(layer) - 1):
            u, v = layer[k], layer[k + 1]
            if abs(g.nodes[u].h - g.nodes[v].h) > 1e-9:
                continue
            before = _bends(g, u, li) + _bends(g, v, li)
            g.nodes[u].y, g.nodes[v].y = g.nodes[v].y, g.nodes[u].y
            after = _bends(g, u, li) + _bends(g, v, li)
            if after > before:
                g.nodes[u].y, g.nodes[v].y = g.nodes[v].y, g.nodes[u].y


def _bends(g: ElkGraph, nid: str, li: int) -> float:
    """节点相对其邻居中位的偏移——越小说明越"对得上"，折弯越少。"""
    vals: List[float] = []
    for adj in (li - 1, li + 1):
        if not (0 <= adj < len(g.layers)):
            continue
        for m in g.neighbours(nid, adj):
            vals.append(g.nodes[m].y)
    if not vals:
        return 0.0
    vals.sort()
    return abs(g.nodes[nid].y - vals[len(vals) // 2])


def assign_layer_coordinates(g: ElkGraph) -> None:
    """层内排布：给定层序，算每个节点的**纵向**坐标（`y`）。

    `BRANDES_KOEPF`：四向（上/下 × 左/右）各跑一次对齐 + 压实，再取平均。
    四次都是**保序**的（层内先后次序不被破坏），而"同层净距 ≥ 要求值"
    是一组线性不等式——线性不等式的可行点取平均仍可行，
    所以取平均**不会**引入重叠。这正是"平衡对齐"（BALANCED）
    既好看又安全的原因，不是"多试几次碰运气"。

    `SIMPLE`：中位松弛 + 按序解冲突，可预测但折弯更多。
    """
    strat = g.options.node_placement
    if strat not in ("BRANDES_KOEPF", "SIMPLE"):
        raise ValueError(f"未实现的层内排布策略 {strat!r}")

    if strat == "SIMPLE":
        _place_simple(g)
        _center_layers_secondary(g)
        _enforce_separation(g)
        return

    fixed = {"LEFTUP": (True, True), "RIGHTUP": (True, False),
             "LEFTDOWN": (False, True), "RIGHTDOWN": (False, False)}
    combos = [fixed[g.options.bk_fixed_alignment]] \
        if g.options.bk_fixed_alignment in fixed \
        else [(True, True), (True, False), (False, True), (False, False)]

    acc: Dict[str, float] = {n: 0.0 for n in g.nodes}
    for up, left in combos:
        block_of, blayers = _bk_align_blocks(g, up, left)
        members: Dict[str, List[str]] = {}
        for n in g.nodes:
            members.setdefault(block_of[n], []).append(n)
        sec = _bk_compaction(g, block_of, members, left)
        for n in g.nodes:
            acc[n] += sec[n]
    for n in g.nodes:
        g.nodes[n].y = acc[n] / len(combos)

    if g.options.bk_edge_straightening == "IMPROVE_STRAIGHTNESS":
        _bk_straighten(g)
    _center_layers_secondary(g)
    _enforce_separation(g)


def _place_simple(g: ElkGraph) -> None:
    """`SIMPLE`：中位松弛 + 按序解冲突。比 BK 差，但每一步都能一眼看懂。"""
    for li, layer in enumerate(g.layers):
        for n in layer:
            vals: List[float] = []
            for adj in (li - 1, li + 1):
                if not (0 <= adj < len(g.layers)):
                    continue
                vals += [g.nodes[m].y for m in g.neighbours(n, adj)]
            g.nodes[n].y = (sum(vals) / len(vals)) if vals else 0.0
        for k in range(1, len(layer)):
            u, v = layer[k - 1], layer[k]
            need = _sep(g, u, v)
            if g.nodes[v].y > g.nodes[u].y - need:
                g.nodes[v].y = g.nodes[u].y - need


def _center_layers_secondary(g: ElkGraph) -> None:
    """每层纵向重新居中（消掉四向平均带来的整体错位）。

    只加常数、不改层内间距，所以不破坏不重叠。
    """
    means = [sum(g.nodes[n].y for n in layer) / len(layer)
             for layer in g.layers if layer]
    if not means:
        return
    target = sum(means) / len(means)
    for layer, mean in zip([l for l in g.layers if l], means):
        d = target - mean
        for n in layer:
            g.nodes[n].y += d


def _enforce_separation(g: ElkGraph) -> None:
    """层内净距的**硬保证**：按层序从上往下推，直到满足 `sep`。

    这是兜底，不是主算法——BK 正常工作时它是空操作（改动量为 0）。
    有了它，"同层不重叠"就不依赖 BK 的实现细节，
    于是后面"走线不穿设备"的论证才站得住。
    """
    for layer in g.layers:
        for k in range(1, len(layer)):
            u, v = layer[k - 1], layer[k]
            need = _sep(g, u, v)
            gap = g.nodes[u].y - g.nodes[v].y
            if gap < need - 1e-9:
                g.nodes[v].y = g.nodes[u].y - need


def assign_layer_positions(g: ElkGraph) -> None:
    """层坐标：主轴上每层占一条带，带宽 = 该层最宽节点的宽度。

    带之间留 `spacing_node_node_between_layers`。这一步**没有自由度**——
    层间距只由带宽与间距决定，所以"版面为什么这么宽"完全可解释。
    """
    x = 0.0
    for layer in g.layers:
        thick = max([g.nodes[n].w for n in layer], default=0.0)
        cx = x + thick / 2.0
        for n in layer:
            g.nodes[n].x = cx
        x += thick + g.options.spacing_node_node_between_layers


def layer_bands(g: ElkGraph) -> List[Tuple[float, float]]:
    """每层在主轴上的占据区间 `(x_left, x_right)`。"""
    out: List[Tuple[float, float]] = []
    for layer in g.layers:
        if not layer:
            out.append((0.0, 0.0))
            continue
        thick = max(g.nodes[n].w for n in layer)
        cx = g.nodes[layer[0]].x
        out.append((cx - thick / 2.0, cx + thick / 2.0))
    return out


def content_rect(g: ElkGraph) -> Tuple[float, float, float, float]:
    """内容包围盒（只算真实节点，虚拟节点不占版面）。"""
    pts = [g.nodes[n].rect() for n in g.model_order if not g.nodes[n].is_dummy]
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[2] for p in pts), max(p[3] for p in pts))


def content_size(g: ElkGraph) -> Tuple[float, float]:
    r = content_rect(g)
    return (r[2] - r[0], r[3] - r[1])


def shift_content(g: ElkGraph, dx: float, dy: float) -> None:
    """整体平移（含端口与已算出的路径）。只平移不缩放，故字高与线宽不受影响。"""
    for n in g.nodes.values():
        n.x += dx
        n.y += dy
        for p in n.ports.values():
            p.x += dx
            p.y += dy
    for e in g.edges.values():
        e.route = [(x + dx, y + dy) for x, y in e.route]


# ---------------------------------------------------------------------------
# 九、阶段 ⑦：端口放置
# ---------------------------------------------------------------------------

def place_ports(g: ElkGraph) -> None:
    """把端口放到绝对坐标上。

    两种情况：
      * 端口带**真锚点偏移**（设备符号的命名连接点）→ 直接用。接管法兰在哪儿，
        管线就只能在哪儿出——按边均分会把管子接到设备壁的空白处；
      * 端口没有锚点（`FREE` 的合成端口）→ 落在该边中点；
        同边多端口时按连接对象排序后均分。

    端口只落在边界上、**不撑大节点尺寸**：设备包围盒已经量好了，
    为端口再加一圈留白会让设备之间凭空多出净距。
    """
    for nid in g.nodes:
        n = g.nodes[nid]
        by_side: Dict[str, List[ElkPort]] = {s: [] for s in SIDES}
        for p in n.ports.values():
            if p.offset is not None:
                p.x = n.x + p.offset[0]
                p.y = n.y + p.offset[1]
            else:
                by_side[p.side].append(p)
        if g.options.port_constraints == FREE:
            for plist in by_side.values():
                for p in plist:                      # FREE：全都落边中点
                    _synthetic_port_pos(n, p, p.side, 0, 1)
            continue
        for side, plist in by_side.items():
            if not plist:
                continue
            plist.sort(key=lambda p: (p.order, p.id))
            for k, p in enumerate(plist):
                _synthetic_port_pos(n, p, side, k, len(plist))


def _synthetic_port_pos(n: ElkNode, p: ElkPort, side: str, k: int, total: int) -> None:
    t = (k + 1) / (total + 1)
    if side in (NORTH, SOUTH):
        p.x = n.left + t * n.w
        p.y = n.top if side == NORTH else n.bottom
    else:
        p.x = n.right if side == EAST else n.left
        p.y = n.bottom + t * n.h


# ---------------------------------------------------------------------------
# 十、阶段 ⑧：边布线
# ---------------------------------------------------------------------------

def _gap_slots(n_edges: int, gap_w: float, sp_edge_node: float,
               sp_edge_edge: float) -> List[float]:
    """一条层间隙里 n 个竖段的**偏心**（相对间隙左边界）。"""
    if n_edges <= 0:
        return []
    usable = max(0.0, gap_w - 2.0 * sp_edge_node)
    if n_edges == 1:
        return [gap_w / 2.0]
    step = max(sp_edge_edge, usable / (n_edges - 1))
    span = step * (n_edges - 1)
    start = sp_edge_node + max(0.0, (usable - span) / 2.0)
    return [start + k * step for k in range(n_edges)]


def _gap_index(g: ElkGraph, a: str, b: str) -> Optional[int]:
    la, lb = g.nodes[a].layer, g.nodes[b].layer
    lo, hi = min(la, lb), max(la, lb)
    return lo if hi - lo == 1 else None


def route_edges(g: ElkGraph) -> Dict[str, List[Pt]]:
    """正交布线：每条边走「出线接管段 → 逐层间隙竖段 → 入线接管段」。

    为什么这样走就不会穿设备（本引擎最值钱的一条性质）：

      * 每个节点在**本层内**的纵向区间互不重叠（`_enforce_separation` 兜底保证），
        所以水平段沿 `y = 某节点的 y` 走时，绝不会穿过同层的另一个节点；
      * 竖段一律落在**层间隙**里，而层间隙按定义不含任何节点；
      * 长边已被虚拟节点拆链，中间那几段水平线走在**虚拟节点的 y** 上，
        而虚拟节点在层里占着一格——那一格是空的。

    于是"管线穿设备"是**版面结构排除的**，不是布线后处理躲开的。
    `_avoid()` 只是断言式的兜底：真被触发，说明上面某条假设破了。
    """
    if g.options.edge_routing not in ("ORTHOGONAL", "POLYLINE"):
        raise ValueError(f"未实现的布线方式 {g.options.edge_routing!r}")
    bands = layer_bands(g)
    stub = max(2.0, min(g.options.spacing_edge_node, 9.0))
    orth = g.options.edge_routing == "ORTHOGONAL"

    need: Dict[int, List[str]] = {}
    for eid, e in g.edges.items():
        if e.is_self_loop or len(e.chain) < 2:
            continue
        for k in range(len(e.chain) - 1):
            gap = _gap_index(g, e.chain[k], e.chain[k + 1])
            if gap is not None:
                need.setdefault(gap, []).append(eid)
    for gap, eids in need.items():
        eids.sort(key=lambda x: (g.nodes[g.edges[x].chain[0]].y, x))
        if gap + 1 >= len(bands):
            continue
        gap_w = max(1.0, bands[gap + 1][0] - bands[gap][1])
        offs = _gap_slots(len(eids), gap_w, g.options.spacing_edge_node,
                          g.options.spacing_edge_edge)
        for eid, off in zip(eids, offs):
            g.edges[eid].slot[gap] = bands[gap][1] + off

    routes: Dict[str, List[Pt]] = {}
    for eid, e in g.edges.items():
        if e.is_self_loop or len(e.chain) < 2:
            continue
        p_src, p_dst = g.src_port(e), g.dst_port(e)
        d_src, d_dst = outward(p_src.side), outward(p_dst.side)
        a0, a1 = (p_src.x, p_src.y), (p_dst.x, p_dst.y)
        q0 = (a0[0] + d_src[0] * stub, a0[1] + d_src[1] * stub)
        q1 = (a1[0] + d_dst[0] * stub, a1[1] + d_dst[1] * stub)

        if not orth and _clear(g, a0, a1):
            e.route = [a0, a1]
            routes[eid] = e.route
            continue

        pts: List[Pt] = [a0, q0]
        cur = q0
        for k in range(1, len(e.chain)):
            gap = _gap_index(g, e.chain[k - 1], e.chain[k])
            y_next = q1[1] if k == len(e.chain) - 1 else g.nodes[e.chain[k]].y
            gx = e.slot.get(gap, cur[0]) if gap is not None else cur[0]
            if abs(gx - cur[0]) > 1e-9:
                pts.append((gx, cur[1]))
            if abs(y_next - cur[1]) > 1e-9:
                pts.append((gx, y_next))
            cur = (gx, y_next)
        if abs(q1[0] - cur[0]) > 1e-9:
            pts.append((q1[0], cur[1]))
        pts.append(q1)
        pts.append(a1)
        e.route = _avoid(g, _normalize(pts), e)
        routes[eid] = e.route
    return routes




def _clear(g: ElkGraph, a: Pt, b: Pt) -> bool:
    """线段是否不碰任何真实节点包围盒。"""
    return not any(_seg_hits_rect(a, b, g.nodes[nid].rect())
                   for nid in g.nodes if not g.nodes[nid].is_dummy)


def _seg_hits_rect(a: Pt, b: Pt, r: Tuple[float, float, float, float]) -> bool:
    """轴对齐线段与矩形是否有**正面积**交集（贴边不算）。"""
    if abs(a[1] - b[1]) < 1e-9:                       # 水平段
        y = a[1]
        if not (r[1] + 1e-9 < y < r[3] - 1e-9):
            return False
        lo, hi = min(a[0], b[0]), max(a[0], b[0])
        return min(hi, r[2]) - max(lo, r[0]) > 1e-9
    if abs(a[0] - b[0]) < 1e-9:                       # 竖直段
        x = a[0]
        if not (r[0] + 1e-9 < x < r[2] - 1e-9):
            return False
        lo, hi = min(a[1], b[1]), max(a[1], b[1])
        return min(hi, r[3]) - max(lo, r[1]) > 1e-9
    return False                                       # 非轴对齐：交给 _normalize 处理


def _avoid(g: ElkGraph, path: List[Pt], e: ElkEdge) -> List[Pt]:
    """兜底后处理：逐段检查碰没碰节点，碰了就沿该节点边界绕行。

    结构上不该发生（见 `route_edges` 的说明），所以这里是**断言式**的：
    真被触发说明"层内不重叠"或"层间隙无节点"的假设破了。
    宁可多绕一下，也不能把管线画进设备里。
    """
    if len(path) < 2:
        return path
    others = [g.nodes[nid] for nid in g.nodes
              if not g.nodes[nid].is_dummy
              and nid not in (e.source_node, e.target_node)]
    out: List[Pt] = [path[0]]
    for a, b in zip(path, path[1:]):
        hit = next((n for n in others if _seg_hits_rect(a, b, n.rect())), None)
        if hit is None:
            out.append(b)
            continue
        pad = max(6.0, g.options.spacing_edge_node)
        replaced = False
        for cand in ([(a[0], hit.top + pad), (b[0], hit.top + pad)],
                     [(a[0], hit.bottom - pad), (b[0], hit.bottom - pad)],
                     [(hit.left - pad, a[1]), (hit.left - pad, b[1])],
                     [(hit.right + pad, a[1]), (hit.right + pad, b[1])]):
            chain = [a] + cand + [b]
            if not any(_seg_hits_rect(u, v, hit.rect())
                       for u, v in zip(chain, chain[1:])):
                out.extend(_normalize(chain)[1:])
                replaced = True
                break
        if not replaced:
            out.append(b)
    return _normalize(out)


def edge_metrics(g: ElkGraph) -> Dict[str, object]:
    """布线与拓扑的体检指标（写进报告，用来解释"为什么这么排"）。"""
    bends = 0
    length = 0.0
    hits = 0
    for e in g.edges.values():
        if e.is_self_loop or not e.route:
            continue
        bends += max(0, len(e.route) - 2)
        length += sum(math.hypot(b[0] - a[0], b[1] - a[1])
                      for a, b in zip(e.route, e.route[1:]))
        for nid in g.nodes:
            n = g.nodes[nid]
            if n.is_dummy or nid in (e.source_node, e.target_node):
                continue
            for a, b in zip(e.route, e.route[1:]):
                if _seg_hits_rect(a, b, n.rect()):
                    hits += 1
    return {"crossings": count_crossings(g),
            "bendpoints": bends,
            "edge_length": round(length, 3),
            "edge_node_hits": hits,
            "dummy_nodes": sum(1 for n in g.nodes if g.nodes[n].is_dummy),
            "layers": len(g.layers),
            "reversed_edges": sum(1 for e in g.edges.values() if e.reversed)}


# ---------------------------------------------------------------------------
# 十一、驱动
# ---------------------------------------------------------------------------

def layout(g: ElkGraph, *, wrap_target_width: float = 0.0,
           max_layers_per_band: int = 99) -> Dict[str, object]:
    """跑完整条流水线。

    返回指标；节点坐标在 `g.nodes[*].x/y`，路径在 `g.edges[*].route`。
    `wrap_target_width > 0` 且层数过多时启用**折行**（见 `_layout_banded`）。
    结束时内容包围盒左下角被移到原点——版面坐标只表示**相对关系**，
    绝对落位由调用方（图幅与可用区）决定。
    """
    if g.options.direction != "RIGHT":
        raise ValueError("chemcad_elk 只实现 direction=RIGHT")

    break_cycles(g)
    assign_layers(g)
    insert_dummy_nodes(g)
    c0 = count_crossings(g)
    c1 = minimize_crossings(g)

    n_layers = len(g.layers)
    widths = [max([g.nodes[n].w for n in layer], default=0.0) for layer in g.layers]
    bands: List[List[int]] = []
    if n_layers > 1 and (wrap_target_width > 0
                         or n_layers > max(1, int(max_layers_per_band))):
        bands = cut_into_bands(n_layers, wrap_target_width, widths,
                               g.options.spacing_node_node_between_layers,
                               max_layers_per_band=max_layers_per_band)
    wrapped = len(bands) > 1
    if wrapped:
        _layout_banded(g, bands)
    else:
        assign_layer_positions(g)
        assign_layer_coordinates(g)
        _arrange_components(g)
        place_ports(g)
        route_edges(g)

    _center_content(g)
    m = edge_metrics(g)
    m["crossings_before_sweep"] = c0
    m["crossings_after_sweep"] = c1
    m["bands"] = len(bands) if wrapped else 1
    m["wrapped"] = wrapped
    g.metrics = m
    return m


def _center_content(g: ElkGraph) -> None:
    r = content_rect(g)
    shift_content(g, -r[0], -r[1])


def _weak_components(g: ElkGraph) -> List[List[str]]:
    parent = {n: n for n in g.nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in g.edges.values():
        if e.is_self_loop:
            continue
        a, b = find(e.source_node), find(e.target_node)
        if a != b:
            parent[b] = a
    buckets: Dict[str, List[str]] = {}
    for n in g.model_order:
        buckets.setdefault(find(n), []).append(n)
    return list(buckets.values())


def _arrange_components(g: ElkGraph) -> None:
    """`separate_connected_components`：弱连通分量各自成块、纵向叠放。

    分量之间本来就没有边；硬排在一层里只会让两堆不相干的东西互相挤。
    分开排、中间留一道净距，读图时才能一眼看出"这是两摊"。

    堆叠净距取 `max(层间距, 2 × 同层净距)`——**不能只用层间距**：
    两个分量可能在同一层里各有一台设备，用层间距去堆会把它们压得比
    「同层净距」还近（这正是本函数第一版踩的坑）。叠完再跑一遍
    `_enforce_separation` 兜底，于是"层内不重叠"这条硬约束无论如何都成立。
    """
    if not g.options.separate_connected_components:
        return
    comps = _weak_components(g)
    if len(comps) <= 1:
        return
    gap = max(g.options.spacing_node_node_between_layers,
              2.0 * g.options.spacing_node_node)
    y_top = 0.0
    for nodes in comps:
        ys = [g.nodes[n].y for n in nodes]
        h = max(ys) - min(ys)
        dy = y_top + h / 2.0 - (sum(ys) / len(ys))
        for n in nodes:
            g.nodes[n].y += dy
        y_top += h + gap
    _enforce_separation(g)


def _layout_banded(g: ElkGraph, bands: List[List[int]]) -> None:
    """折行排布：每段独立跑流水线，再自上而下叠起来。

    段内层号重排到 0 起；各段**左对齐、纵向依次下移**——
    这正是工程上「流程图分排绘制」的画法。跨段管线由 `_route_cross_band` 处理。
    """
    band_gap = g.options.spacing_node_node_between_layers
    y_top = 0.0
    for bi, layer_ids in enumerate(bands):
        sub_nodes: List[str] = []
        for li in layer_ids:
            sub_nodes += list(g.layers[li])
        sub_set = set(sub_nodes)
        keep = [eid for eid, e in g.edges.items()
                if e.source_node in sub_set and e.target_node in sub_set]
        sub = ElkGraph(g.options)
        for nid in sub_nodes:
            n = g.nodes[nid]
            sn = sub.add_node(nid, n.w, n.h, section=n.section,
                              priority=n.priority, model=n.model)
            sn.is_dummy, sn.dummy_for = n.is_dummy, n.dummy_for
            for pid, p in n.ports.items():
                sub.add_port(nid, pid, p.side, p.offset, p.order)
        for eid in keep:
            e = g.edges[eid]
            se = sub.add_edge(eid, (e.source_node, e.source_port),
                              (e.target_node, e.target_port))
            se.reversed = e.reversed
        assign_layers(sub)
        insert_dummy_nodes(sub)
        minimize_crossings(sub)
        assign_layer_positions(sub)
        assign_layer_coordinates(sub)
        _arrange_components(sub)
        place_ports(sub)
        route_edges(sub)
        if not sub.layers:
            continue
        r = content_rect(sub)
        dx, dy = -r[0], y_top - r[1]
        for nid in sub_nodes:
            g.nodes[nid].x = sub.nodes[nid].x + dx
            g.nodes[nid].y = sub.nodes[nid].y + dy
            g.nodes[nid].band = bi
            g.nodes[nid].band_layer = sub.nodes[nid].layer
        for eid in keep:
            g.edges[eid].route = [(x + dx, y + dy) for x, y in sub.edges[eid].route]
        y_top += (r[3] - r[1]) + band_gap
    # ⚠ 段内布局只摆平了各段自己的端口（那些端口属于 sub，不是 g）。
    # 段节点被搬到全局坐标后，`g` 上的端口还停在初始位置——跨段边会以
    # 一对 (0,0) 为端点画出一条退化的零长线。所以必须**按最终节点位置重放端口**，
    # 再布跨段边。（段内边不受影响：它们的路径已随节点一起平移过。）
    place_ports(g)
    _route_cross_band(g, bands)
    # ⚠ `g.layers` 必须**按段内实际排出的层**重建。段是独立排版的，
    # 段内丢掉跨段边后重算分层，结果可能与全局分层不同——于是"全局第 k 层"的
    # 两台设备可能落在段内两个不同的层上。此时若还拿全局分层去解释几何，
    # 「同一层的设备 x 相同」这条就不成立（实测差 1 mm），
    # 而错的是**记录**、不是几何。重建之后层号与坐标重新一一对应。
    _relayer_from_bands(g, bands)


def _relayer_from_bands(g: ElkGraph, bands: List[List[int]]) -> None:
    """按各段实际排出的局部层号，重建全局 `layers` / `layer` / `order`。"""
    new_layers: List[List[str]] = []
    for layer_ids in bands:
        local: Dict[int, List[str]] = {}
        for li in layer_ids:
            for nid in g.layers[li]:
                local.setdefault(g.nodes[nid].band_layer, []).append(nid)
        for k in sorted(local):
            new_layers.append(sorted(local[k], key=lambda n: g.nodes[n].model))
    if not new_layers:
        return
    for li, layer in enumerate(new_layers):
        for k, nid in enumerate(layer):
            g.nodes[nid].layer = li
            g.nodes[nid].order = k
    g.layers = new_layers


def _route_cross_band(g: ElkGraph, bands: List[List[int]]) -> None:
    """跨段边的布线：出段右侧 → 沿右侧走廊下行 → 从目标段右侧折回。

    走廊取"内容最右边界 + 净距"，不穿过任何段内的设备。
    """
    r = content_rect(g)
    corridor = r[2] + max(12.0, 2.0 * g.options.spacing_edge_node)
    stub = max(2.0, min(g.options.spacing_edge_node, 9.0))
    for eid, e in g.edges.items():
        if e.is_self_loop or len(e.chain) < 2:
            continue
        if g.nodes[e.source_node].band == g.nodes[e.target_node].band:
            continue
        p_src, p_dst = g.src_port(e), g.dst_port(e)
        d_src, d_dst = outward(p_src.side), outward(p_dst.side)
        a0, a1 = (p_src.x, p_src.y), (p_dst.x, p_dst.y)
        q0 = (a0[0] + d_src[0] * stub, a0[1] + d_src[1] * stub)
        q1 = (a1[0] + d_dst[0] * stub, a1[1] + d_dst[1] * stub)
        e.route = _avoid(g, _normalize(
            [a0, q0, (corridor, q0[1]), (corridor, q1[1]), q1, a1]), e)


def describe(g: ElkGraph) -> str:
    """人读的分层结果（调试与报告用）。"""
    lines = [f"分层 {len(g.layers)} 层，节点 {len(g.nodes)} 个"
             f"（虚拟 {sum(1 for n in g.nodes if g.nodes[n].is_dummy)} 个）"]
    for i, layer in enumerate(g.layers):
        names = ["·" if g.nodes[n].is_dummy else n for n in layer]
        lines.append(f"  L{i}: " + " | ".join(names))
    if g.metrics:
        lines.append("  指标：" + "，".join(f"{k} {v}" for k, v in g.metrics.items()))
    return "\n".join(lines)
