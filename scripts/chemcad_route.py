#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""设备与文字避障：障碍边界构成稀疏正交网格，四向 A* 搜索。

长度、折弯、软符号和已有走廊共同计价。搜索限定区域与展开预算，
返回的是候选网格内的解，不能承诺任意图纸都有解或全局几何最优。
设备始终是硬障碍；先扩大区域，再归零设备余量，仍失败则不生成假路径。
平行流股3mm边缘硬净距始终保留，12mm方向性软代价用于优先拉开。
接管允许从所属设备内出线，但整段必须避开其它设备。

学习来源、视频时间点、改动及验证边界见 references/routing-video-study.md。
坐标单位为 mm，y 向上。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import chemcad_engine as E
from chemcad_ir import simplify   # 正交后处理：与 ELK 共用一份实现

Pt = Tuple[float, float]
Rect = Tuple[float, float, float, float]           # (x0, y0, x1, y1)

KIND_EQUIP = "equip"
KIND_TEXT = "text"
KIND_SYMBOL = "symbol"

_EAST, _WEST, _NORTH, _SOUTH = 0, 1, 2, 3
_DIRS: Tuple[Tuple[int, int, int], ...] = (
    (_EAST, 1, 0), (_WEST, -1, 0), (_NORTH, 0, 1), (_SOUTH, 0, -1),
)


@dataclass
class RouteOptions:
    """布线选项。代价单位统一是"等效毫米"（mm-equivalent），便于解释权重。"""

    #: 硬障碍外扩量（mm）。校验器把设备本体缩 0.5 后仍容 2 mm 才算穿设备，
    #: 所以外扩 1.5 mm 的绕行在判据上必然安全。
    hard_clear: float = 1.5
    #: "舒服"净距：网格里额外生成这一档候选线，让 A* 有机会走得更松
    pref_clear: float = 8.0
    #: 出线接管段长度（mm）
    stub: float = E.NOZZLE_STUB
    #: 接管段**上限**（mm）——锚点嵌在本体内时可以一直外移到这个长度。
    #: 见 `_stub_point`：走到本体之外是接管段的定义，不是可选项。
    stub_max: float = 40.0
    #: 旧版选项兼容保留；搜索从本体外开始，不再使用锚点窗口
    carve: float = 7.0

    #: 每个折弯的等效代价（mm）：14 ≈ "宁可多绕 14 mm，也不多一个弯"
    bend_penalty: float = 14.0
    #: 穿过障碍的**每毫米**代价
    text_penalty: float = 80.0        # 文字：最不愿压
    equip_penalty: float = 800.0      # 仅调用方显式声明为软对象时适用
    symbol_penalty: float = 9.0       # 其它符号（阀门/仪表）
    #: 与**已布管线**共用走廊的每毫米代价——降图面噪声（作用同 ELK 的交叉最小化）
    corridor_penalty: float = 30.0

    corridor_cell: float = 8.0
    max_nodes: int = 60000
    #: 单条管线**至少**展开多少状态。真正生效的预算是
    #: `max(max_expand, expand_factor × 网格节点数)`，再被 `expand_cap` 封顶。
    #:
    #: ⚠️ 为什么预算必须随网格伸缩：固定 24000 时，两条跨设备管线死在
    #: 「展开超预算（节点 194×241）」上，随后退化成直线穿过设备本体 66 mm。
    #: 网格节点数是量纲正确的预算标尺——一次搜索的最坏工作量本就是节点数的量级。
    max_expand: int = 24000
    expand_factor: float = 4.0
    expand_cap: int = 150000
    #: 搜索区域相对"起终点包围盒"的外扩量（mm）；区域外的**软**障碍不参与。
    #: 初始档取 `clamp(端点距离 × region_pad_ratio)`，失败再扩到默认档和内容边界，
    #: 因为对一段 120 mm 的短跳搜 ±260 mm 会把整幅面的障碍全卷进来（网格爆炸）。
    region_pad: float = 260.0
    region_pad_ratio: float = 0.55
    region_pad_min: float = 60.0
    hard_text: bool = True
    #: 画幅边界（硬）。**默认 None，别乱开**——见 `route_pipes` 的说明：
    #: 布线跑在 `fit_to_sheet` **之前**，此刻内容坐标还不是图纸坐标，
    #: 拿图纸边界去卡内容，会把"内容左下角恰好为负"的设备整个封死。
    sheet_bounds: Optional[Rect] = None
    sheet_margin: float = 4.0


@dataclass
class Obstacle:
    rect: Rect
    kind: str
    owner: str = ""
    hard: bool = True
    movable: bool = False  # 仅已知可由文字排版移动的文字可让出接管走廊

    def penalty(self, o: "RouteOptions") -> float:
        if self.kind == KIND_EQUIP:
            return o.equip_penalty
        if self.kind == KIND_TEXT:
            return o.text_penalty
        return o.symbol_penalty


@dataclass
class RouteResult:
    line: str
    path: List[Pt] = field(default_factory=list)
    cost: float = 0.0
    expanded: int = 0
    #: 0 保留净距 / 1 净距归零；设备仍不可穿越
    relaxed: int = 0
    ok: bool = True
    note: str = ""
    #: 因压在必经接管走廊上而被剔除的文字（交给迭代修正去挪）
    evicted: List[str] = field(default_factory=list)
    #: `audit_routes` 填：穿设备长度 / 压到的文字数
    through: float = 0.0
    text_hits: int = 0


# ---------------------------------------------------------------------------
# 一、轴对齐几何（解析求解，不用采样——采样会漏"细长矩形被短段横穿"）
# ---------------------------------------------------------------------------

def _expand(r: Rect, m: float) -> Rect:
    return (r[0] - m, r[1] - m, r[2] + m, r[3] + m)


def _inside(r: Rect, p: Pt, eps: float = 1e-9) -> bool:
    return r[0] + eps < p[0] < r[2] - eps and r[1] + eps < p[1] < r[3] - eps


def _seg_len_in(a: Pt, b: Pt, r: Rect) -> float:
    """轴对齐线段落在矩形内部的长度。"""
    if abs(a[1] - b[1]) < 1e-9:
        y = a[1]
        if not (r[1] < y < r[3]):
            return 0.0
        lo, hi = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
        return max(0.0, min(hi, r[2]) - max(lo, r[0]))
    if abs(a[0] - b[0]) < 1e-9:
        x = a[0]
        if not (r[0] < x < r[2]):
            return 0.0
        lo, hi = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
        return max(0.0, min(hi, r[3]) - max(lo, r[1]))
    return 0.0


# ---------------------------------------------------------------------------
# 二、障碍收集：**与校验器同口径**
# ---------------------------------------------------------------------------

def collect_obstacles(cd: E.ChemDrawing, *,
                      opts: Optional[RouteOptions] = None) -> List[Obstacle]:
    """把图纸上的"不能碰"与"尽量别碰"收成障碍清单。

    三处口径刻意与校验器**逐字对齐**（否则就会出现"布线以为躲开了、校核说压了"）：

    * 设备 → `group_rect(g, layers=("EQUIP",))`：**本体**，不含接管法兰。
      这与 `detect_pipe_through_equipment` 用的是同一个矩形——
      拿含接管的包围盒当障碍，会把"贴着接管绕行"误判成违规，白白绕远。
    * 文字 → `text_rect(t)`：**含旋转、有真面积**。
      用 `Text.points()`（只有基点）会让文字退化成点，既避不开也查不出。
    * 图层跳过表复用 `E.text_line_skip_layers`：图框/网格/剖面线上的文字
      压在框线上是正常的，不该逼着管线绕它们。
    """
    o = opts or RouteOptions()
    out: List[Obstacle] = []
    for g in cd.iter_groups():
        lab = E.obj_label(g)
        if g.kind == "equipment":
            nozzle_rect = E.equipment_flow_rect(g)
            r = E.expand_rect(nozzle_rect,E.PIPE_EDGE_GAP) if nozzle_rect else (E.group_rect(g, layers=("EQUIP",)) or E.group_rect(g))
            if r is None:
                continue
            w, h = E.rect_size(r)
            if w >= 4.0 and h >= 4.0:
                out.append(Obstacle(r, KIND_EQUIP, lab, hard=True))
        else:
            r = E.stream_table_rect(g) or E.group_rect(g)
            if r is not None and r[2] - r[0] >= 1.0 and r[3] - r[1] >= 1.0:
                out.append(Obstacle(r, KIND_SYMBOL, lab, hard=g.name=='STREAM_DATA'))
    protected = {id(e) for g in cd.iter_groups()
                 if g.name == "STREAM_DATA" or g.ref.startswith('equipment:') for e in g.ents}
    for t in cd.iter_ents():
        if not isinstance(t, E.Text) or not t.text.strip():
            continue
        if str(t.layer) in E.text_line_skip_layers:
            continue
        r = E.text_rect(t)
        if r[2] - r[0] <= 0.2 and r[3] - r[1] <= 0.2:
            continue
        out.append(Obstacle(r, KIND_TEXT, repr(str(t.text))[:20], hard=o.hard_text,
                            movable=id(t) not in protected and t.note != "stream_no_fixed"))
    return out


# ---------------------------------------------------------------------------
# 三、A*
# ---------------------------------------------------------------------------

class _Lattice:
    """候选坐标（Hanán 网格）+ **逐线预计算**的阻挡与代价查询。

    预计算是关键：对每条 x 候选线记下"哪些 y 区间被硬障碍挡住""哪些 y 区间被
    软障碍覆盖、每毫米多少代价"，于是"竖直移动的合法性与代价"是沿一条有序区间表
    走一遍，而不是把全部障碍扫一轮。实测差别是**两个数量级**：
    60 个软障碍 × 25 万次状态展开 × 4 邻域的朴素写法要跑一分半钟。
    """

    __slots__ = ("xs", "ys", "nx", "ny", "hb_x", "hb_y", "sc_x", "sc_y")

    def __init__(self, xs: Sequence[float], ys: Sequence[float],
                 hards: Sequence[Rect], softs: Sequence[Obstacle],
                 opts: RouteOptions, pipe_bands=(), pipe_soft_bands=()):
        self.xs = list(xs)
        self.ys = list(ys)
        self.nx = len(self.xs)
        self.ny = len(self.ys)

        # ⚠️ **预排序 + 二分定位**，替代早先"每条候选线全扫 hards 再 sort"。
        # profile 实测 120 台设备下单条 solve 里 `_Lattice.__init__` 占 0.2s、上万次 sort。
        # 关键：要 `r[0] < x < r[2]` 的障碍，按 x0 排序后它们全在**第一个 r[0]>=x 之前**，
        # 用 `bisect_left` 定位这个分界点，只扫前段即可；提取出的区间**仍需 sort**
        # （`_blocked` 依赖按起始坐标升序），但扫描量已从 O(候选线×障碍) 降到 O(候选线×命中)。
        import bisect as _bisect

        hards_x0 = sorted(hards, key=lambda r: r[0])
        hards_y0 = sorted(hards, key=lambda r: r[1])
        softs_x0 = sorted(softs, key=lambda o: o.rect[0])
        softs_y0 = sorted(softs, key=lambda o: o.rect[1])
        hards_x0_keys = [r[0] for r in hards_x0]
        hards_y0_keys = [r[1] for r in hards_y0]
        softs_x0_keys = [o.rect[0] for o in softs_x0]
        softs_y0_keys = [o.rect[1] for o in softs_y0]

        # 竖直方向（x 固定）：被挡/有代价的 y 区间
        self.hb_x: List[List[Tuple[float, float]]] = []
        self.sc_x: List[List[Tuple[float, float, float]]] = []
        for x in self.xs:
            i = _bisect.bisect_left(hards_x0_keys, x)
            hb = [(r[1], r[3]) for r in hards_x0[:i] if r[2] > x]
            hb += [(r[1],r[3]) for axis,r in pipe_bands if axis==1 and r[0]+1e-6 < x < r[2]-1e-6]
            hb.sort()
            self.hb_x.append(hb)
            j = _bisect.bisect_left(softs_x0_keys, x)
            sc = [(o.rect[1], o.rect[3], o.penalty(opts))
                  for o in softs_x0[:j] if o.rect[2] > x]
            sc += [(r[1],r[3],opts.corridor_penalty) for axis,r in pipe_soft_bands
                   if axis==1 and r[0]+1e-6<x<r[2]-1e-6]
            sc.sort()
            self.sc_x.append(sc)

        # 水平方向（y 固定）
        self.hb_y: List[List[Tuple[float, float]]] = []
        self.sc_y: List[List[Tuple[float, float, float]]] = []
        for y in self.ys:
            i = _bisect.bisect_left(hards_y0_keys, y)
            hb = [(r[0], r[2]) for r in hards_y0[:i] if r[3] > y]
            hb += [(r[0],r[2]) for axis,r in pipe_bands if axis==0 and r[1]+1e-6 < y < r[3]-1e-6]
            hb.sort()
            self.hb_y.append(hb)
            j = _bisect.bisect_left(softs_y0_keys, y)
            sc = [(o.rect[0], o.rect[2], o.penalty(opts))
                  for o in softs_y0[:j] if o.rect[3] > y]
            sc += [(r[0],r[2],opts.corridor_penalty) for axis,r in pipe_soft_bands
                   if axis==0 and r[1]+1e-6<y<r[3]-1e-6]
            sc.sort()
            self.sc_y.append(sc)

    @staticmethod
    def _blocked(tab: Sequence[Tuple[float, float]], lo: float, hi: float) -> bool:
        for a, b in tab:
            if a >= hi:
                break
            if b > lo:
                return True
        return False

    @staticmethod
    def _penalty(tab: Sequence[Tuple[float, float, float]],
                 lo: float, hi: float) -> float:
        cost = 0.0
        for a, b, pen in tab:
            if a >= hi:
                break
            cost += max(0.0, min(hi, b) - max(lo, a)) * pen
        return cost

    def blocked_y(self, ix: int, y0: float, y1: float) -> bool:
        return self._blocked(self.hb_x[ix], min(y0, y1), max(y0, y1))

    def blocked_x(self, iy: int, x0: float, x1: float) -> bool:
        return self._blocked(self.hb_y[iy], min(x0, x1), max(x0, x1))

    def soft_y(self, ix: int, y0: float, y1: float) -> float:
        return self._penalty(self.sc_x[ix], min(y0, y1), max(y0, y1))

    def soft_x(self, iy: int, x0: float, x1: float) -> float:
        return self._penalty(self.sc_y[iy], min(x0, x1), max(x0, x1))


def _candidates(obstacles: Sequence[Obstacle], opts: RouteOptions,
                use_pref: bool) -> Tuple[Set[float], Set[float]]:
    """候选 x / y 坐标集合：障碍边界 ± 余量。"""
    xs: Set[float] = set()
    ys: Set[float] = set()
    for ob in obstacles:
        r = ob.rect
        margins = (opts.hard_clear, opts.pref_clear) if use_pref else (opts.hard_clear,)
        for m in margins:
            xs.add(r[0] - m)
            xs.add(r[2] + m)
            ys.add(r[1] - m)
            ys.add(r[3] + m)
    return xs, ys


def _corridor_hits(corridor: Dict[Tuple[int, int], int], a: Pt, b: Pt,
                   cell: float) -> float:
    """沿线段取样统计被占用的走廊格数（返回格数，调用方再折算代价）。"""
    if not corridor:
        return 0.0
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    steps = max(1, int(length / (cell * 0.6)))
    hits = 0
    for k in range(steps + 1):
        t = k / steps
        key = (int((a[0] + (b[0] - a[0]) * t) // cell),
               int((a[1] + (b[1] - a[1]) * t) // cell))
        hits += corridor.get(key, 0)
    return hits * cell / (steps + 1)


def _mark_corridor(corridor: Dict[Tuple[int, int], int], path: Sequence[Pt],
                   cell: float) -> None:
    c = max(1.0, cell)
    for a, b in zip(path, path[1:]):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, int(length / (c * 0.6)))
        for k in range(steps + 1):
            t = k / steps
            key = (int((a[0] + (b[0] - a[0]) * t) // c),
                   int((a[1] + (b[1] - a[1]) * t) // c))
            corridor[key] = corridor.get(key, 0) + 1


def solve(start: Pt, goal: Pt, obstacles: Sequence[Obstacle],
          opts: Optional[RouteOptions] = None, *,
          start_dir: Optional[Pt] = None, end_dir: Optional[Pt] = None,
          corridor: Optional[Dict[Tuple[int, int], int]] = None,
          line: str = "", relax: int = 0,
          carve_for: Tuple[str, ...] = (),
          pipe_bands=(),
          pipe_soft_bands=(),
          start_junction=None, end_junction=None,
          start_branch_dir=None, end_branch_dir=None,
          start_straight_end=None,end_straight_end=None,
          pad: Optional[float] = None) -> RouteResult:
    """在有限候选网格与预算内求一条正交避障路径。

    `start_dir` / `end_dir` 是锚点出线方向。给了就先沿该方向走 `stub` 接管段——
    塔顶接管在封头正上方，必须先朝外走；否则"先横"会从封头**横着穿出塔体**，
    而正交性校验对这类错误完全无感。

    `carve_for` 指定接管所属设备，仅对应的必经接管段允许从本体内出线。
    `relax` 仅允许 0 正常 / 1 余量归零；禁止硬障碍降为软障碍。
    """
    o = opts or RouteOptions()
    if relax not in (0, 1):
        raise ValueError("禁止把硬障碍降级为软障碍；relax 只能为 0 或 1")
    if relax == 1:
        o = replace(o, hard_clear=0.0)  # 候选坐标与碰撞净距同时归零
    corner = corridor or {}
    margin = o.hard_clear if relax == 0 else 0.0

    # ---- 接管段 -----------------------------------------------------------
    span = math.hypot(goal[0] - start[0], goal[1] - start[1])
    stub = o.stub
    if start_dir and end_dir and span > 1e-9 and stub * 2.4 > span:
        stub = max(0.0, span / 2.4)                  # 两设备挨得近时别绕回头
    src_owner = carve_for[0] if carve_for else None
    dst_owner = carve_for[1] if len(carve_for) > 1 else None
    s_exit = _stub_point(start, start_dir, obstacles, o, stub, owner=src_owner, margin=margin) if start_dir else start
    g_exit = _stub_point(goal, end_dir, obstacles, o, stub, owner=dst_owner, margin=margin) if end_dir else goal
    s_exit = start_junction or start_straight_end or s_exit
    g_exit = end_junction or end_straight_end or g_exit
    source_prefix=[start,s_exit]
    goal_prefix=[goal,g_exit]
    for prefix,direction in ((source_prefix,start_branch_dir),(goal_prefix,end_branch_dir)):
        if direction:
            p=prefix[-1]
            prefix.append((p[0]+direction[0]*E.PIPE_PREFERRED_GAP,p[1]+direction[1]*E.PIPE_PREFERRED_GAP))
    s_exit,g_exit=source_prefix[-1],goal_prefix[-1]
    mandatory=[(p,q,owner) for prefix,owner in ((source_prefix,src_owner),(goal_prefix,dst_owner))
               for p,q in zip(prefix,prefix[1:]) if math.dist(p,q)>1e-6]
    for p, q, owner in mandatory:
        for axis, rect in pipe_bands:
            if abs(p[1-axis]-q[1-axis]) < 1e-6 and _seg_len_in(p,q,rect) > 1e-6:
                return RouteResult(line=line,ok=False,relaxed=relax,note=f'接管段{p}→{q}与已有流股禁区{rect}（方向{axis}）净距不足')
        for ob in obstacles:
            if not ob.hard or ob.kind == KIND_TEXT:
                continue
            rect = _expand(ob.rect, margin)
            own = ob.kind == KIND_EQUIP and ob.owner == owner
            if (own and _inside(rect, q)) or (not own and
                    (_inside(rect, q) or _seg_len_in(p, q, rect) > 1e-9)):
                return RouteResult(line=line, ok=False, relaxed=relax,
                                   note=f"接管段被设备 {ob.owner} 阻挡或未出本体")

    # ---- 必备走廊上的文字：该让路的是文字，不是管线 -----------------------
    evicted: List[str] = []
    work: List[Obstacle] = []
    corridor_segs = [(p,q) for p,q,_ in mandatory]
    for ob in obstacles:
        if ob.kind == KIND_TEXT and ob.hard and _on_mandatory(ob.rect, corridor_segs):
            if not ob.movable:
                return RouteResult(line=line, ok=False, relaxed=relax,
                                   note=f"固定文字 {ob.owner} 阻挡接管段")
            evicted.append(ob.owner)
            continue                                  # 仅交给排版器确实可移动的文字
        work.append(ob)

    # ---- 硬 / 软分档 ------------------------------------------------------
    hard_rects: List[Rect] = []
    softs: List[Obstacle] = []
    # 搜索在本体外的接管端点间进行，不再挖窗，避免搜索重新进入端点设备。
    for ob in work:
        if ob.hard:
            hard_rects.append(_expand(ob.rect, margin))
        else:
            softs.append(ob)
    # 固定接管和公共分支前缀也不能被同一流股的后续搜索穿回；端面保持开放。
    for p,q,_ in mandatory:
        m=E.PIPE_EDGE_GAP
        if abs(p[1]-q[1])<1e-6:
            hard_rects.append((min(p[0],q[0]),p[1]-m,max(p[0],q[0]),p[1]+m))
        else:
            hard_rects.append((p[0]-m,min(p[1],q[1]),p[0]+m,max(p[1],q[1])))
    if o.sheet_bounds is not None:
        x0, y0, x1, y1 = o.sheet_bounds
        m, far = o.sheet_margin, 1e5
        hard_rects += [(-far, -far, x0 + m, far), (x1 - m, -far, far, far),
                       (-far, -far, far, y0 + m), (-far, y1 - m, far, far)]

    if any(_inside(r, q) for r in hard_rects for q in (s_exit, g_exit)):
        return RouteResult(line=line, ok=False, relaxed=relax,
                           note="搜索端点位于硬障碍内部")

    # ---- 搜索区域 --------------------------------------------------------
    # 半径随**端点距离**伸缩：固定 260 mm 对一段 120 mm 的短跳是荒谬的，
    # 会把整幅面 100+ 个障碍全卷进来，网格直接涨到 194×241。
    pad = float(o.region_pad if pad is None else pad)
    rx0 = min(start[0], goal[0], s_exit[0], g_exit[0]) - pad
    ry0 = min(start[1], goal[1], s_exit[1], g_exit[1]) - pad
    rx1 = max(start[0], goal[0], s_exit[0], g_exit[0]) + pad
    ry1 = max(start[1], goal[1], s_exit[1], g_exit[1]) + pad
    region = (rx0, ry0, rx1, ry1)
    softs = [ob for ob in softs if E.rect_gap(ob.rect, region) <= 0.0]
    # ⚠️ **候选线只从区域内的障碍生成**（性能关键）：`_candidates` 早先扫
    # **全部**障碍再按 region 过滤——120 台设备时全量候选线 1946×1493，
    # 每条管线重建一次、每条候选线又扫 613 个障碍，136 条管线累加 30s+。
    # 先按 region 裁剪（区域外的障碍边界坐标对本条管线毫无意义），
    # 候选线与 lattice 的构建都只在"有用的那几百个障碍"上跑。
    region_obs = [ob for ob in work if E.rect_gap(ob.rect, region) <= 0.0]

    def build(use_pref: bool) -> _Lattice:
        xs, ys = _candidates(region_obs, o, use_pref)
        for axis, rect in (*pipe_bands,*pipe_soft_bands):
            xs.update((rect[0],rect[2]))
            ys.update((rect[1],rect[3]))
        xs.update((rx0, rx1))
        ys.update((ry0, ry1))
        for v in (start[0], goal[0], s_exit[0], g_exit[0],
                  (s_exit[0] + g_exit[0]) / 2.0):
            xs.add(float(v))
        for v in (start[1], goal[1], s_exit[1], g_exit[1],
                  (s_exit[1] + g_exit[1]) / 2.0):
            ys.add(float(v))
        xs2 = sorted(v for v in xs if rx0 - 1e-6 <= v <= rx1 + 1e-6)
        ys2 = sorted(v for v in ys if ry0 - 1e-6 <= v <= ry1 + 1e-6)
        if not xs2:
            xs2 = sorted({start[0], goal[0]})
        if not ys2:
            ys2 = sorted({start[1], goal[1]})
        return _Lattice(xs2, ys2, hard_rects, softs, o, pipe_bands,pipe_soft_bands)

    # ⚠️ **先估算候选线规模再决定要不要 pref 档**：`build(True)` 的 `_Lattice.__init__`
    # 对每条候选线跑 4 次 sort（hb_x/sc_x/hb_y/sc_y），障碍一多就是上万次 sort
    # （profile 实测 10150 次），这是 120 台设备下单条 solve 0.2s 的主因。
    # pref 档（hard_clear + pref_clear 两套余量）让候选线翻倍，而它只是"走得更松"
    # 的次优偏好——候选线规模一超 `max_nodes` 反正要退化成 build(False)，
    # 那不如先估一下，能省掉一次昂贵的 True 构建。
    _xs_est, _ys_est = _candidates(region_obs, o, True)
    for _v in (start[0], goal[0], s_exit[0], g_exit[0], (s_exit[0] + g_exit[0]) / 2.0):
        _xs_est.add(float(_v))
    for _v in (start[1], goal[1], s_exit[1], g_exit[1], (s_exit[1] + g_exit[1]) / 2.0):
        _ys_est.add(float(_v))
    _nx_est = sum(1 for _v in _xs_est if rx0 - 1e-6 <= _v <= rx1 + 1e-6)
    _ny_est = sum(1 for _v in _ys_est if ry0 - 1e-6 <= _v <= ry1 + 1e-6)
    lat = build(False) if _nx_est * _ny_est > o.max_nodes else build(True)
    if lat.nx * lat.ny > o.max_nodes:
        lat = build(False)                            # 退化：丢掉 pref 档候选线

    direction_code=lambda p: _EAST if p[0]>0 else _WEST if p[0]<0 else _NORTH if p[1]>0 else _SOUTH
    res = _astar(lat, s_exit, g_exit, o, corner,
                 start_forbidden_dir=(direction_code(start_branch_dir)^1) if start_branch_dir else None,
                 end_forbidden_dir=direction_code(end_branch_dir) if end_branch_dir else None)
    res.line = line
    res.relaxed = relax
    res.evicted = evicted
    if not res.ok:
        return res

    path: List[Pt] = source_prefix
    path += res.path
    path += list(reversed(goal_prefix))
    res.path = simplify(path)
    return res


def _far(a: Pt, b: Pt) -> bool:
    return abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9


def _on_mandatory(r: Rect, segs: Sequence[Tuple[Pt, Pt]]) -> bool:
    """文字框是否压在**必经接管段**上（含 1 mm 缓冲）。"""
    slack = _expand(r, 1.0)
    for a, b in segs:
        if _far(a, b) and _seg_len_in(a, b, slack) > 0.0:
            return True
    return False


def _stub_point(p: Pt, d: Pt, obstacles: Sequence[Obstacle],
                o: RouteOptions, stub: float, *, owner: Optional[str] = None,
                margin: Optional[float] = None) -> Pt:
    """计算所属设备出口；遇邻设备或固定文字缩短直段并提前转弯。"""
    if not d or stub <= 0.0:
        return p
    if d not in ((1., 0.), (-1., 0.), (0., 1.), (0., -1.)):
        raise ValueError("接管方向必须为四个正交单位方向")
    clearance = o.hard_clear if margin is None else margin
    axis = 0 if d[0] else 1
    sign = d[axis]
    own, foreign = [], []
    for ob in obstacles:
        if not ob.hard or (ob.kind == KIND_TEXT and ob.movable):
            continue
        r = _expand(ob.rect, clearance)
        is_own = ob.kind == KIND_EQUIP and (
            ob.owner == owner if owner is not None else _inside(r, p))
        (own if is_own else foreign).append(r)
    k = stub
    for r in own:
        if _inside(r, p):
            edge = r[axis + 2] if sign > 0 else r[axis]
            k = max(k, (edge - p[axis]) * sign + 1e-5)
    k = min(k, max(stub, o.stub_max))
    for r in foreign:
        q = (p[0] + d[0]*k, p[1] + d[1]*k)
        if _seg_len_in(p, q, r) > 1e-9:
            edge = r[axis] if sign > 0 else r[axis + 2]
            k = min(k, max(0., (edge-p[axis])*sign - 1e-5))
    return (p[0] + d[0]*k, p[1] + d[1]*k)


def _bend_lower_bound(p: Pt, goal: Pt, direction: int) -> int:
    """忽略障碍、禁止立即反向时的剩余弯数下界，目标不限制入线朝向。

    朝目标前进需0/1弯；目标在后方且不共线需2弯；在后方且共线需3弯。
    障碍、软代价只能增加费用。这是方向下界，不删除远离目标的合法后继。
    """
    dx, dy = goal[0]-p[0], goal[1]-p[1]
    if direction < 0:
        return int(abs(dx)>1e-9 and abs(dy)>1e-9)
    axis = 0 if direction in (_EAST, _WEST) else 1
    delta = (dx, dy)
    forward = delta[axis] * (1 if direction in (_EAST, _NORTH) else -1)
    sideways = abs(delta[1-axis]) > 1e-9
    if forward < -1e-9:
        return 2 if sideways else 3
    return int(sideways)


def _astar(lat: _Lattice, s: Pt, g: Pt, o: RouteOptions,
           corridor: Dict[Tuple[int, int], int], *, start_forbidden_dir=None,end_forbidden_dir=None) -> RouteResult:
    """A* 主体。状态 = (网格点, 进入方向)——带方向才能给折弯计价。

    启发式 = 曼哈顿距离 + 剩余最少弯数 × 折弯代价。
    剩余弯数根据进入方向和目标所在方向取0–3，不限制必须接近目标；
    非负边代价下为下界。一致性与独立Dijkstra对照见针对性回归。
    """
    xs, ys = lat.xs, lat.ys

    def nearest(v: float, arr: Sequence[float]) -> int:
        lo, hi = 0, len(arr) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        if lo > 0 and abs(arr[lo - 1] - v) <= abs(arr[lo] - v):
            return lo - 1
        return lo

    sx, sy = nearest(s[0], xs), nearest(s[1], ys)
    gx, gy = nearest(g[0], xs), nearest(g[1], ys)
    gxs, gys = xs[gx], ys[gy]

    def h(ix: int, iy: int, d: int) -> float:
        p, goal = (xs[ix], ys[iy]), (gxs, gys)
        return abs(p[0]-goal[0]) + abs(p[1]-goal[1]) + \
            _bend_lower_bound(p, goal, d) * o.bend_penalty

    start_state = (sx, sy, -1)
    gscore: Dict[Tuple[int, int, int], float] = {start_state: 0.0}
    came: Dict[Tuple[int, int, int], Tuple[int, int, int]] = {}
    heap: List[Tuple[float, int, int, int, int, int]] = [
        (h(sx, sy, -1), h(sx, sy, -1), sx, sy, -1, 0)]
    seen: Set[Tuple[int, int, int]] = set()
    expanded = 0
    goal_state: Optional[Tuple[int, int, int]] = None
    budget = min(int(o.expand_cap),
                 max(int(o.max_expand),
                     int(o.expand_factor * lat.nx * lat.ny)))
    cc = o.corridor_cell

    while heap:
        _f, _gc, ix, iy, d, tie = heapq.heappop(heap)
        st = (ix, iy, d)
        if st in seen:
            continue
        seen.add(st)
        expanded += 1
        if (ix, iy) == (gx, gy) and (end_forbidden_dir is None or d!=end_forbidden_dir):
            goal_state = st
            break
        if expanded > budget:
            return RouteResult(line="", ok=False, expanded=expanded,
                               note=f"展开超预算 {budget}（节点 {lat.nx}×{lat.ny}）")
        px, py = xs[ix], ys[iy]
        gc = gscore[st]
        for nd, dx, dy in _DIRS:
            if d==-1 and start_forbidden_dir is not None and nd==start_forbidden_dir:
                continue
            if d >= 0 and nd == (d ^ 1):
                continue  # 非负代价下立即原路返回不会改进路线
            jx, jy = ix + dx, iy + dy
            if not (0 <= jx < lat.nx and 0 <= jy < lat.ny):
                continue
            qx, qy = xs[jx], ys[jy]
            if dx:
                if lat.blocked_x(iy, px, qx):
                    continue
                cost = abs(qx - px) + lat.soft_x(iy, px, qx)
            else:
                if lat.blocked_y(ix, py, qy):
                    continue
                cost = abs(qy - py) + lat.soft_y(ix, py, qy)
            cost += _corridor_hits(corridor, (px, py), (qx, qy), cc) \
                * o.corridor_penalty / 100.0
            if d != -1 and d != nd:
                cost += o.bend_penalty
            ns = (jx, jy, nd)
            ng = gc + cost
            if ng < gscore.get(ns, float("inf")) - 1e-9:
                gscore[ns] = ng
                came[ns] = st
                # f 相同时优先剩余估价较小者；保留其它候选，避免退化为贪心。
                heuristic = h(jx, jy, nd)
                heapq.heappush(heap, (ng + heuristic, heuristic, jx, jy, nd, jx + jy))

    if goal_state is None:
        return RouteResult(line="", ok=False, expanded=expanded,
                           note=f"网格上无可行路径（节点 {lat.nx}×{lat.ny}）")

    pts: List[Pt] = []
    st = goal_state
    while st in came:
        pts.append((xs[st[0]], ys[st[1]]))
        st = came[st]
    pts.append((xs[st[0]], ys[st[1]]))
    pts.reverse()
    return RouteResult(line="", path=simplify(pts),
                       cost=gscore[goal_state], expanded=expanded, ok=True)




# ---------------------------------------------------------------------------
# 四、对外入口
# ---------------------------------------------------------------------------

#: 放宽级别 → 说明（写进报告，说明"这条路是怎么来的"）
RELAX_NOTE: Dict[int, str] = {
    0: "正常（硬障碍 + 余量）",
    1: "放宽：硬余量归零（设备挨得太近）",

}


def route_pipes(cd: E.ChemDrawing, spec: Dict, *,
                opts: Optional[RouteOptions] = None,
                extra_hard: Sequence[Rect] = (),
                line_filter: Optional[Iterable[str]] = None
                ) -> Dict[str, RouteResult]:
    """给设备连接及设备↔自由坐标管线逐条布线，返回 `管线号 → RouteResult`。

    三条工程约束：

    * 先预留显式路线，再自动布主管与次管。全部端点的必经接管提前保留；
      后布平行线须满足3mm硬净距，12mm范围用方向性代价优先绕开。
    * `extra_hard` 是**上一轮学到的障碍**（例如上一轮被压到的文字框）：
      迭代修正据此逐轮收敛，而不是每轮从零开始犯同一个错。
    * 接管从所属设备本体内出线；搜索从本体外开始，不挖窗穿回设备。

    所有区域保留净距失败后，才归零净距重试；设备始终不可穿。
    """
    o = replace(opts) if opts is not None else RouteOptions()
    if o.sheet_bounds is None:
        # ⚠️ **不要用图纸幅面当边界**。布线跑在 `fit_to_sheet` **之前**，此刻
        # 内容坐标是"版面自身坐标系"（原点常常就在内容左下角），跟图纸坐标系
        # 是两回事。实测吃过这个亏：某座反应器本体落在 x∈[0, 49.3]，而按幅面
        # 生成的左边界墙是 x<4，于是它贴左边的整片区域连带接管走廊全被判非法，
        # 向西出线的管线 **走不出墙** → 无解 → 退化成直线穿设备。
        #
        # frame 正确的做法是拿**内容自身**求包络，再留一圈余量：同一坐标系、
        # 会随内容长大，也不再假设任何图纸尺寸。
        box = _content_box(cd)
        if box is not None:
            p = float(o.region_pad)
            o.sheet_bounds = (box[0] - p, box[1] - p, box[2] + p, box[3] + p)

    ids = {str(e.get("id")) for e in spec.get("equipment", []) if e.get("id")}
    want = {str(x) for x in line_filter} if line_filter is not None else None
    import chemcad_process_layout as PL
    returns={line for loop in PL.recognize(spec,ids) for line in loop['return_lines']}
    # 有向连接本身证明的次物流循环可走外围，不据此猜设备用途。
    adjacency={key:[] for key in ids}
    for item in spec.get('pipes',[]):
        src=str(item.get('from','')).split('.')[0];dst=str(item.get('to','')).split('.')[0]
        if src in ids and dst in ids:adjacency[src].append(dst)
    for item in spec.get('pipes',[]):
        if item.get('layer','PIPE_PRI')!='PIPE_INLINE':continue
        src=str(item.get('from','')).split('.')[0];dst=str(item.get('to','')).split('.')[0]
        pending=[dst];visited=set()
        while pending:
            node=pending.pop()
            if node==src:returns.add(str(item.get('id','')));break
            if node in visited:continue
            visited.add(node);pending.extend(adjacency.get(node,[]))
    pipes = sorted(spec.get("pipes", []),
                   key=lambda p: (not bool(p.get('route') or p.get('via')),
                                  str(p.get('id','')) in returns,
                                  str(p.get("layer", "PIPE_PRI")) != "PIPE_PRI"))
    base = collect_obstacles(cd, opts=o)
    for r in extra_hard:
        # 上轮文字坐标不是本轮实体；接管冲突时标记过期并重新选标注位置。
        # 本轮实际存在的固定参数框由 collect_obstacles 保持不可挪动。
        base.append(Obstacle((float(r[0]), float(r[1]), float(r[2]), float(r[3])),
                             KIND_TEXT, "learned", hard=True, movable=True))

    corridor: Dict[Tuple[int, int], int] = {}
    occupied = []
    reserved_stubs=[]
    # 后布流股的固定接管不能被早布线占住；按全部端点预留，再进行逐条搜索。
    for item in pipes:
        refs=(str(item.get('from','')),str(item.get('to','')))
        try:
            starts=[cd.resolve(ref) for ref in refs]
            directions=[cd.anchor_dir_of(ref) for ref in refs]
        except (KeyError,ValueError):
            continue
        span=math.dist(*starts)
        stub=o.stub
        if all(directions) and span>1e-9 and stub*2.4>span:
            stub=span/2.4
        for ref,point,direction in zip(refs,starts,directions):
            own=_owner(ref,ids)
            if not own:
                continue
            end=_stub_point(point,direction,base,o,stub,owner=own)
            if math.dist(point,end)>1e-6:
                reserved_stubs.append((str(item.get('id','')),ref,[point,end],str(item.get('layer','PIPE_PRI'))))
    branch_junctions={}
    branch_directions={}
    trunk_exits={}
    reserved_branches=[]
    for entry in E.shared_port_plan(cd,((str(p.get('id','')),str(p.get('layer','PIPE_PRI')),
                                        (str(p.get('from','')),str(p.get('to','')))) for p in pipes)):
        p=entry['point']; direction=entry['direction']
        distance=max((math.dist(p,path[-1]) for line,ref,path,layer in reserved_stubs
                      if math.dist(p,path[0])<.001),default=o.stub)
        junction=(p[0]+direction[0]*distance,p[1]+direction[1]*distance)
        perpendicular=(0.,1.) if direction[0] else (1.,0.)
        leg=E.PIPE_PREFERRED_GAP
        trunk_exits[tuple(entry['main'])]=(junction[0]+direction[0]*E.PIPE_PREFERRED_GAP,
                                          junction[1]+direction[1]*E.PIPE_PREFERRED_GAP)
        for index,pair in enumerate(entry['branches']):
            branch_junctions[tuple(pair)]=junction
            sign=1 if index%2==0 else -1
            turn=(perpendicular[0]*sign,perpendicular[1]*sign)
            branch_directions[tuple(pair)]=turn
            branch_layer=next(str(item.get('layer','PIPE_PRI')) for item in pipes if str(item.get('id',''))==pair[0])
            reserved_branches.append((pair[0],[junction,(junction[0]+turn[0]*leg,junction[1]+turn[1]*leg)],branch_layer))
    out: Dict[str, RouteResult] = {}
    annotation_parts=[]
    def reserve_annotation(item,path):
        if not PL.recognize(spec,ids):return True
        if not item.get('stream') and not item.get('stream_no'):return True
        import draw_pfd as P
        import chemcad_symbols as S
        line=str(item['id']);rec=E.PipeRec(line,path)
        half=S.STREAMNO_DIAMOND_HALF
        desired=rec.straight_point_at(float(item.get('stream_at',item.get('tag_at',.5))),half+1)
        segments=[(a,b,lay,'stream:'+lid) for old,lay,refs in occupied
                  for lid in [next((key for key,result in out.items() if result.path is old),line)]
                  for a,b in zip(old,old[1:])]
        segments.extend((a,b,lay,'reserved:'+lid) for lid,ref,stub,lay in reserved_stubs
                        if lid!=line for a,b in zip(stub,stub[1:]))
        equipment=[E.group_rect(g) for g in cd.iter_groups() if g.kind=='equipment']
        for q in sorted(P._stream_candidate_points(rec,desired,half),key=lambda q:math.dist(q,desired)):
            scratch=E.ChemDrawing();group=S.add_stream_data(scratch,q,fields=P._stream_fields(item.get('stream')),
                                                            placement=P._stream_placement(rec,q))
            group.ref='stream:'+line;parts=[r for _,r in E.stream_annotation_parts(group)]
            if E.stream_annotation_pipe_hits(group,segments):continue
            if any(E.rect_gap(r,old)<E.STREAM_ANNOTATION_GAP+.01 for r in parts for old in annotation_parts+equipment if old):continue
            annotation_parts.extend(parts)
            base.extend(Obstacle(E.expand_rect(part,E.STREAM_ANNOTATION_GAP+.01),KIND_SYMBOL,
                                 'annotation:'+line,hard=True) for part in parts)
            cd.meta.setdefault('reserved_stream_points',{})[line]=q
            return True
        return False
    for p in pipes:
        line = str(p.get("id", ""))
        if want is not None and line not in want:
            continue
        frm, to = str(p.get("from", "")), str(p.get("to", ""))
        a, b = _owner(frm, ids), _owner(to, ids)
        # 自由坐标的进/出料也须避让设备接管；管线上取点另由调用方处理。
        def literal(ref):
            try:
                x,y=ref.split(',')
                return math.isfinite(float(x)) and math.isfinite(float(y))
            except (ValueError,TypeError):
                return False
        if (not a and not literal(frm)) or (not b and not literal(to)) or (a and a == b):
            continue
        try:
            p1, p2 = cd.resolve(frm), cd.resolve(to)
        except (KeyError, ValueError):
            continue  # 管线端点无法解析（不在设备/管线表内）→ 跳过该条
        d1, d2 = cd.anchor_dir_of(frm), cd.anchor_dir_of(to)
        layer=str(p.get('layer','PIPE_PRI'))
        if p.get('route') or p.get('via'):
            # 显式路线会覆盖 A*：必须预留真实路径，不能预留最终不会落笔的候选。
            mode=str(p.get('route','auto'))
            path=(E.stub_path(p1,d1,p2,d2,mode=mode[4:] or 'auto',via=p.get('via'))
                  if E.is_stub_route(mode) else E.ortho_path(p1,p2,mode=mode,via=p.get('via')))
            for ref,tail in ((frm,False),(to,True)):
                path=E.trim_path_at_body(path,E.body_rect_of(cd,ref),tail=tail)
            r=RouteResult(line=line,path=path,note='显式路线，最终净距校核仍生效')
            occupied.append((path,layer,(frm,to)))
            _mark_corridor(corridor,path,o.corridor_cell)
            out[line]=r
            continue
        bands=[]
        soft_bands=[]
        # 只豁免已声明的同一设备接点短直段，长距离共线仍禁止。
        endpoints=[p1,(p1[0]+(d1 or (1,0))[0],p1[1]+(d1 or (1,0))[1]),
                   (p2[0]+(d2 or (1,0))[0],p2[1]+(d2 or (1,0))[1]),p2]
        for old_path,old_layer,old_refs in occupied:
            stems=E.shared_port_stems(endpoints,(frm,to),old_path,old_refs)
            bands += E.pipe_spacing_bands(endpoints,layer,old_path,old_layer,stems,search=True)
            soft_bands += E.pipe_spacing_bands(endpoints,layer,old_path,old_layer,stems,
                                              search=True,spacing=E.PIPE_PREFERRED_GAP)
        for future_line,ref,stub_path,stub_layer in reserved_stubs:
            if future_line==line:
                continue
            stems=E.shared_port_stems(endpoints,(frm,to),stub_path,(ref,ref))
            bands += E.pipe_spacing_bands(endpoints,layer,stub_path,stub_layer,stems,search=True)
        for branch_line,branch_path,branch_layer in reserved_branches:
            if line != branch_line:
                bands += E.pipe_spacing_bands(endpoints,layer,branch_path,branch_layer,spacing=E.PIPE_EDGE_GAP+.011)
        carve = (a, b)
        # 区域半径先"够用就好"，失败才放宽到全幅：搜索量对半径是平方级的，
        # 一上来就用最大半径等于把每一条管线都当成全局最短路来解。
        span = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        near = max(float(o.region_pad_min),
                   min(float(o.region_pad), span * float(o.region_pad_ratio)))
        pads: List[float] = [near]
        if float(o.region_pad) > near + 1e-9:
            pads.append(float(o.region_pad))
        # 最后一档覆盖内容边界；局部地图无解不等于整张图无解。
        if o.sheet_bounds is not None:
            box = o.sheet_bounds
            full = max(min(p1[0],p2[0])-box[0], min(p1[1],p2[1])-box[1],
                       box[2]-max(p1[0],p2[0]), box[3]-max(p1[1],p2[1]))
            if full > pads[-1] + 1e-9:
                pads.append(full)
        r: Optional[RouteResult] = None
        expanded = 0
        start_joint=branch_junctions.get((line,frm)); end_joint=branch_junctions.get((line,to))
        obstacles=base
        interior=_content_box(cd)
        if line in returns and interior:
            obstacles=base+[Obstacle(interior,KIND_SYMBOL,'塔循环外围优先',hard=False)]
        for relax in (0, 1):
            for pd in pads:
                r = solve(p1, p2, obstacles, o, start_dir=d1, end_dir=d2,
                          corridor=corridor, line=line, relax=relax,
                          carve_for=carve, pipe_bands=bands, pipe_soft_bands=soft_bands,
                          start_junction=start_joint,end_junction=end_joint,
                          start_straight_end=trunk_exits.get((line,frm)),
                          end_straight_end=trunk_exits.get((line,to)),
                          start_branch_dir=branch_directions.get((line,frm)),
                          end_branch_dir=branch_directions.get((line,to)),pad=pd)
                expanded += r.expanded
                if r is not None and r.ok:
                    break
            if r is not None and r.ok:
                break
        if r is None or not r.ok:
            out[line] = RouteResult(line=line, ok=False, expanded=expanded,
                                    note=f"保留/归零净距 × {len(pads)} 档区域后未找到路径；"
                                         + (r.note if r is not None else "未搜索"))
            continue
        r.expanded = expanded
        _mark_corridor(corridor, r.path, o.corridor_cell)
        occupied.append((r.path,layer,(frm,to)))
        out[line] = r
        if not reserve_annotation(p,r.path):
            # 短连接没有完整标注净空时试外围折线；不拆框或降低3mm约束。
            box=interior or (min(p1[0],p2[0]),min(p1[1],p2[1]),max(p1[0],p2[0]),max(p1[1],p2[1]))
            middle=((p1[0]+p2[0])/2,(p1[1]+p2[1])/2)
            for waypoint in ((middle[0],box[3]+50),(middle[0],box[1]-50),
                             (box[0]-50,middle[1]),(box[2]+50,middle[1])):
                first=solve(p1,waypoint,base,o,start_dir=d1,carve_for=carve,
                            pipe_bands=bands,pipe_soft_bands=soft_bands,pad=o.region_pad,
                            start_junction=start_joint,start_branch_dir=branch_directions.get((line,frm)),
                            start_straight_end=trunk_exits.get((line,frm)))
                last=solve(waypoint,p2,base,o,end_dir=d2,carve_for=carve,
                           pipe_bands=bands,pipe_soft_bands=soft_bands,pad=o.region_pad,
                           end_junction=end_joint,end_branch_dir=branch_directions.get((line,to)),
                           end_straight_end=trunk_exits.get((line,to)))
                if not first.ok or not last.ok:continue
                proposed=simplify(first.path+last.path[1:])
                old_path=r.path;r.path=proposed;occupied[-1]=(proposed,layer,(frm,to))
                if reserve_annotation(p,proposed):
                    r.note+='；外围绕行预留完整流股标注';break
                r.path=old_path;occupied[-1]=(old_path,layer,(frm,to))
    return out


def _content_box(cd: E.ChemDrawing) -> Optional[Rect]:
    """图上**所有几何**的包围盒（版面自身坐标系）。

    只看有意義的图层主体——文字记注若全算进来，一个飘远的标签就能把边界撑到
    装得下一张桌子。设备本体 + 已有符号足够代表"内容在哪儿"。
    """
    xs: List[float] = []
    ys: List[float] = []
    for g in cd.iter_groups():
        r = E.group_rect(g)
        if r:
            xs.append(r[0])
            ys.append(r[1])
            xs.append(r[2])
            ys.append(r[3])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _owner(ref: str, ids: Set[str]) -> Optional[str]:
    """引用串 → 设备 id（`"T-101.top"` → `T-101`；管线取点/字面坐标 → None）。"""
    s = str(ref).strip()
    if not s or "@" in s:
        return None
    head = s.partition(".")[0] if "." in s else s
    return head if head in ids else None


# ---------------------------------------------------------------------------
# 五、自检：用**同一套判据**复核自己
# ---------------------------------------------------------------------------

def audit_routes(cd: E.ChemDrawing, routes: Dict[str, RouteResult], *,
                 nozzle_tol: float = E.NOZZLE_TOLERANCE,
                 min_len: float = E.PIPE_INSIDE_MIN,
                 text_gap: float = E.TEXT_LINE_GAP) -> Dict[str, object]:
    """复核 A* 的产出：**穿设备长度**与**压到的文字**，判据与校验器逐字一致。

    为什么必须自检：`relaxed > 0` 那条路是"放宽后得到的"，很可能已经违规。
    报告里不写清楚，就会变成"布线说没问题、校核说穿了"的静默失真——
    那正是本技能最忌讳的一类缺陷。
    """
    equip: List[Tuple[str, Rect]] = []
    for g in cd.iter_groups():
        if g.kind != "equipment":
            continue
        r = E.group_rect(g, layers=("EQUIP",))
        if r is None:
            continue
        w, h = E.rect_size(r)
        if w >= 4.0 and h >= 4.0:
            equip.append((E.obj_label(g), E.expand_rect(r, -0.5)))
    texts: List[Tuple[str, Rect]] = []
    for t in cd.iter_ents():
        if isinstance(t, E.Text) and t.text.strip() \
                and str(t.layer) not in E.text_line_skip_layers:
            texts.append((repr(str(t.text))[:20], E.text_rect(t, shrink=text_gap)))

    bad_through: List[Dict[str, object]] = []
    bad_text: List[Dict[str, object]] = []
    for line, res in routes.items():
        if not res.path:
            continue
        subs = E.shrink_path(res.path, nozzle_tol, nozzle_tol)
        res.through = 0.0
        res.text_hits = 0
        for sub in subs:
            for name, r in equip:
                got = E.seg_inside_rect_len(sub[0], sub[1], r)
                if got > 0.0:
                    res.through += got
                    if got > min_len:
                        bad_through.append({"line": line, "equip": name,
                                            "length": round(got, 2),
                                            "relaxed": res.relaxed})
            for label, r in texts:
                if E.seg_inside_rect_len(sub[0], sub[1], r) > 1e-9:
                    res.text_hits += 1
                    bad_text.append({"line": line, "text": label,
                                     "relaxed": res.relaxed})
    return {
        "lines": len(routes),
        "unsolved": [ln for ln, r in routes.items() if not r.ok],
        "relaxed": {k: sum(1 for r in routes.values() if r.relaxed == k)
                    for k in (0, 1, 2)},
        "evicted_text": sorted({t for r in routes.values() for t in r.evicted}),
        "pipe_through": bad_through,
        "text_over_line": bad_text,
        "bends": sum(max(0, len(r.path) - 2) for r in routes.values()),
        "length": round(sum(math.hypot(b[0] - a[0], b[1] - a[1])
                            for r in routes.values()
                            for a, b in zip(r.path, r.path[1:])), 2),
        "expanded": sum(r.expanded for r in routes.values()),
        "clean": all(r.ok and bool(r.path) for r in routes.values()) and not bad_through and not bad_text,
    }


def format_audit(a: Dict[str, object]) -> str:
    """把复核结果渲染成人读摘要（与 `preflight` 口径一致，便于对照）。"""
    rel = a.get("relaxed") or {}
    lines = [f"A* 布线复核：{a['lines']} 条管线，{a['bends']} 个转折，"
             f"总长 {a['length']} mm，搜索展开 {a['expanded']} 个状态",
             f"  放宽分布：正常 {rel.get(0, rel.get('0', 0))} / "
             f"放宽余量 {rel.get(1, rel.get('1', 0))} / "
             f"降级 {rel.get(2, rel.get('2', 0))}"]
    if a.get("evicted_text"):
        lines.append(f"  被迫让路的文字 {len(a['evicted_text'])} 处"
                     f"（压在必经接管走廊上，交由文字挪位处理）："
                     + "；".join(str(x) for x in (a["evicted_text"] or [])[:5]))  # type: ignore[union-attr]
    for k, label in (("unsolved", "无解"), ("pipe_through", "穿设备"),
                     ("text_over_line", "压文字")):
        hits = a.get(k) or []
        if hits:
            lines.append(f"  ⚠ {label} {len(hits)} 处：" + "；".join(
                str(h) if isinstance(h, str) else
                f"{h.get('line')}→{h.get('equip') or h.get('text')}"
                f"({h.get('length', '')})" for h in hits[:6]))                # type: ignore[union-attr]
    if a.get("clean"):
        lines.append("  ✓ 复核通过：不穿设备、不压文字")
    return "\n".join(lines)
