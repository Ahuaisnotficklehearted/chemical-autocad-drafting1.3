#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_engine.py —— 绘图引擎：正交布线、图纸容器、合规校验、双后端渲染。

本文件把「化工制图规范」变成**可执行约束**，而不只是文档里的要求：

  * 布线铁律  → `ortho_path()` 只产生水平/竖直段，斜线在源头不可能生成；
  * 闭环要求  → `validate_control_loops()` 检查五要素齐备 + 测量点**真落在**几何上；
  * 调节阀必注 → `validate_control_valves()` 检查气开/气闭与传动形式；
  * 图层/线宽 → `validate_layers()` / `validate_lineweights()` 对照 HG 20519.1。

后端：
  * `write_dxf()`      —— 优先 ezdxf（R2000，图层带真线宽 370）；无 ezdxf 时退化为
                          自带的 R12 书写器（零依赖，任何 Python 3 环境可用）；
  * `ComBackend`       —— 驱动运行中的 AutoCAD（pyautocad 模式：COM 重试、图层、图块、Undo 标记）。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from chemcad_ir import (
    Arc, Circle, Drawing, Ent, Group, Line, Polyline, Pt, Solid, Text,
    LAYERS, LAYER_REGISTRY, LW_RANGES, SHEET_SIZES,
    TEXT_H_MAIN, TEXT_H_SMALL,
    deviation_deg, is_orthogonal, lerp, resolve_layer,
)
import chemcad_symbols as S

# ---------------------------------------------------------------------------
# 一、正交布线（布线铁律的可执行化）
# ---------------------------------------------------------------------------

def ortho_path(p1: Pt, p2: Pt, mode: str = "auto", via: Optional[Sequence[float]] = None) -> List[Pt]:
    """把两点之间变成**只含水平/竖直段**的折线路径。

    mode:
      auto  —— 按主轴方向自动选 H 先或 V 先（默认，最像人工画法）
      HV    —— 先水平后竖直
      VH    —— 先竖直后水平
      H-mid —— 先水平到中间 x，再竖直，再水平（三点式，适合绕行）
      V-mid —— 先竖直到中间 y，再水平，再竖直
    via:     显式指定中间坐标序列，形如 ``['x', 300]`` 或 ``[300, 'y']``，
             未指定的一维由端点补足；便于沿设备边缘绕行。
    """
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])

    # via 必须先于「共线退化」判断处理：两端点在同一水平/竖直线时，
    # via 往往正是用来把线绕出去的（如循环物流绕行设备），此时不能提前返回直线。
    if via:
        pts: List[Pt] = [(x1, y1)]
        cur = (x1, y1)
        for spec in via:
            if isinstance(spec, str):
                nx, ny = (cur[0], y2) if spec == "y" else (x2, cur[1])
            elif isinstance(spec, (list, tuple)) and len(spec) == 2:
                ax, ay = spec
                nx = cur[0] if ax in ("x", None) else float(ax)
                ny = cur[1] if ay in ("y", None) else float(ay)
            else:
                raise ValueError(f"via 元素无法解析: {spec!r}")
            pts.extend(ortho_path(cur, (nx, ny), mode="auto")[1:])
            cur = (nx, ny)
        pts.extend(ortho_path(cur, (x2, y2), mode="auto")[1:])
        return _dedupe(pts)

    if abs(x1 - x2) < 1e-9 or abs(y1 - y2) < 1e-9:
        return [(x1, y1), (x2, y2)]

    if mode == "HV":
        return [(x1, y1), (x2, y1), (x2, y2)]
    if mode == "VH":
        return [(x1, y1), (x1, y2), (x2, y2)]
    if mode == "H-mid":
        mx = (x1 + x2) / 2.0
        return [(x1, y1), (mx, y1), (mx, y2), (x2, y2)]
    if mode == "V-mid":
        my = (y1 + y2) / 2.0
        return [(x1, y1), (x1, my), (x2, my), (x2, y2)]
    # auto：沿主轴优先（视觉上更自然，也更少占用图面）
    return [(x1, y1), (x2, y1), (x2, y2)] if abs(x2 - x1) >= abs(y2 - y1) \
        else [(x1, y1), (x1, y2), (x2, y2)]


def _dedupe(pts: Sequence[Pt], eps: float = 1e-9) -> List[Pt]:
    out: List[Pt] = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append((float(p[0]), float(p[1])))
    return out


#: 管线离开/进入设备时先沿锚点朝外方向走的"接管段"长度（mm）。
#: 这一段不是装饰——塔顶接管在封头正上方，管线必须先向上再转向；
#: 否则就从塔顶横着穿出去了（`validate_pipe_through_equipment` 会报）。
NOZZLE_STUB: float = 9.0

#: 锚点名关键词 → 出线方向（几何判不出朝向时的兜底）
_ANCHOR_DIR_WORDS: Tuple[Tuple[Tuple[str, ...], Pt], ...] = (
    (("top", "head", "distillate", "vapor", "vent", "discharge", "motor",
      "air_out", "gas"), (0.0, 1.0)),
    (("bottom", "drain", "liquid", "skirt", "air_in", "water_in", "sludge"), (0.0, -1.0)),
    (("left", "suction", "inlet", "tube_in", "feed", "reboiler_return",
      "water_out"), (-1.0, 0.0)),
    (("right", "outlet", "tube_out", "reflux", "reboiler_draw", "shell_in",
      "shell_out", "product"), (1.0, 0.0)),
)


def anchor_direction(owner: Group, name: str) -> Optional[Pt]:
    """锚点的**出线方向**（单位向量）；判不出返回 None。

    管线必须朝这个方向离开设备：塔顶接管在封头正上方，往外走才对；
    若先横向走，等于"从塔顶横着穿出去"——而正交性/线宽/位号三组校验
    对这类错误完全无感（由 `validate_pipe_through_equipment` 兜底）。

    判定顺序：① 锚点落在**设备本体**（EQUIP 层，不含接管）的哪一侧；
    ② 判不出时用锚点名兜底——`top` 的坐标落在接管内部，几何上判不出朝向。
    """
    anchors = owner.meta.get("anchors")
    if not isinstance(anchors, dict) or name not in anchors:
        return None
    p = anchors[name]
    body = group_rect(owner, layers=("EQUIP", "EQUIP_IN")) or group_rect(owner)
    if body is not None:
        tol = 0.8
        if p[1] > body[3] + tol:
            return (0.0, 1.0)
        if p[1] < body[1] - tol:
            return (0.0, -1.0)
        if p[0] < body[0] - tol:
            return (-1.0, 0.0)
        if p[0] > body[2] + tol:
            return (1.0, 0.0)
    low = name.lower()
    for words, d in _ANCHOR_DIR_WORDS:
        if any(low == w or low.startswith(w) for w in words):
            return d
    if body is not None:                       # 最后退化为"离哪条边近就往哪边走"
        cands = [(abs(p[0] - body[0]), (-1.0, 0.0)), (abs(body[2] - p[0]), (1.0, 0.0)),
                 (abs(p[1] - body[1]), (0.0, -1.0)), (abs(body[3] - p[1]), (0.0, 1.0))]
        return min(cands, key=lambda kv: kv[0])[1]
    return None


def stub_path(p1: Pt, d1: Optional[Pt], p2: Pt, d2: Optional[Pt],
              mode: str = "auto", via: Optional[Sequence[float]] = None,
              stub: float = NOZZLE_STUB) -> List[Pt]:
    """**带接管段**的正交路径：两端先沿锚点朝外方向各走一小段，再正交相连。

    这是"管线穿设备"的根治手段：`ortho_path(p1,p2,"auto")` 会按 `|dx|` 与 `|dy|`
    的大小选先横还是先竖——从塔顶接管出发且目标在右下方时它选"先横"，
    于是这条线从封头处**横着穿出塔体**。接管段强制它先朝外走，
    出了设备本体再转弯。两端都用同一条规则，故路径仍然完全正交。
    """
    q1 = (p1[0] + d1[0] * stub, p1[1] + d1[1] * stub) if d1 else (float(p1[0]), float(p1[1]))
    q2 = (p2[0] + d2[0] * stub, p2[1] + d2[1] * stub) if d2 else (float(p2[0]), float(p2[1]))
    span = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if d1 and d2 and span > 1e-9 and stub * 2.4 > span:
        k = max(0.0, span / 2.4)               # 两设备挨得近时缩短接管段，别绕回头
        q1 = (p1[0] + d1[0] * k, p1[1] + d1[1] * k)
        q2 = (p2[0] + d2[0] * k, p2[1] + d2[1] * k)
    mid = ortho_path(q1, q2, mode=mode, via=via)
    return _dedupe([(float(p1[0]), float(p1[1]))] + list(mid)
                   + [(float(p2[0]), float(p2[1]))])


#: 需要 `/` 连接、含接管段的走线方式（供规格与自动修正选用）
STUB_ROUTES: Tuple[str, ...] = ("stub", "stubHV", "stubVH", "stubH-mid", "stubV-mid")


def is_stub_route(mode: str) -> bool:
    return str(mode).startswith("stub")




def _dist_point_seg(p: Pt, a: Pt, b: Pt) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-18:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


# ---------------------------------------------------------------------------
# 二、图纸容器：按「编号」组织设备与管线（规格驱动作图的基础）
# ---------------------------------------------------------------------------

@dataclass
class PipeRec:
    """一段管线的记录：路径、图层、编号与属性。"""

    line_no: str
    path: List[Pt]
    layer: str = "PIPE_PRI"
    dn: str = ""
    grade: str = ""
    medium: str = ""
    #: 绝热代号：H 保温 / P 保冷 / T 伴热（HG 20519.6）。**组合号的末段**。
    insulation: str = ""
    arrows: List[Tuple[Pt, str]] = field(default_factory=list)
    source_ref: str = ""
    target_ref: str = ""

    @property
    def segments(self) -> List[Tuple[Pt, Pt]]:
        return list(zip(self.path, self.path[1:]))

    def point_at(self, t: float) -> Pt:
        """沿路径按累计长度取比例点（0~1）。"""
        if len(self.path) < 2:
            return self.path[0] if self.path else (0.0, 0.0)
        segs = self.segments
        lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
        total = sum(lens)
        if total <= 0:
            return self.path[0]
        want = max(0.0, min(1.0, t)) * total
        acc = 0.0
        for (a, b), L in zip(segs, lens):
            if acc + L >= want:
                return lerp(a, b, (want - acc) / L if L else 0.0)
            acc += L
        return self.path[-1]


    def straight_point_at(self, t: float, margin: float = 4.5) -> Pt:
        """将标注点投到有端部净距的直线段上，优先靠近原比例点。"""
        target = self.point_at(t)
        candidates = []
        for a, b in self.segments:
            length = math.dist(a, b)
            if length < 2.0 * margin or length == 0:
                continue
            u = ((target[0] - a[0]) * (b[0] - a[0]) +
                 (target[1] - a[1]) * (b[1] - a[1])) / (length * length)
            u = max(margin / length, min(1.0 - margin / length, u))
            candidates.append(lerp(a, b, u))
        if not candidates:
            raise ValueError(f"管线 {self.line_no} 没有足够长的直线段放置物流编号，请加长或重排管线")
        return min(candidates, key=lambda p: math.dist(p, target))


class ChemDrawing(Drawing):
    """在通用 `Drawing` 之上增加「按编号寻址」能力：设备/管线都有 id，可互相引用。

    引用语法：``"T-101.top"``（对象 id + 锚点）、``"T-101"``（对象基点）、
    ``"PL-101@0.35"``（管线上某比例点）、``"120,300"``（字面坐标）。
    """

    def __init__(self, name: str = "P&ID", sheet: str = "A1"):
        super().__init__(name=name, sheet=sheet)
        self.objects: Dict[str, Group] = {}
        self.pipes: Dict[str, PipeRec] = {}
        # 拓扑结点（MIXER/FSPLIT/VALVE…）：**不出图元**，但管线要落在它们身上。
        self.nodes: Dict[str, Pt] = {}

    # -- 设备/符号 ---------------------------------------------------------
    def place(self, obj_id: str, kind: str, at: Pt, rot: float = 0.0, tag: str = "",
              **params) -> Group:
        g = S.place_symbol(self, kind, at, rot=rot, tag=tag, **params)
        g.meta["id"] = obj_id
        if tag:
            g.tag = tag
        self.objects[obj_id] = g
        return g

    # -- 引用解析 -----------------------------------------------------------
    def resolve(self, ref: str) -> Pt:
        """把引用字符串解析成世界坐标点。"""
        s = str(ref).strip()
        if "," in s and not s.startswith("@"):
            parts = s.split(",")
            if len(parts) == 2:
                try:
                    return (float(parts[0]), float(parts[1]))
                except ValueError:
                    pass  # 不是纯数字坐标 → 继续尝试管线 / 锚点引用分支
        if s in self.nodes:                    # 拓扑结点：按自由点解析（不出图元）
            return self.nodes[s]
        if "@" in s:
            pid, _, t = s.partition("@")
            if pid in self.pipes:
                return self.pipes[pid].point_at(float(t))
            raise KeyError(f"未定义管线 '{pid}'；已定义：{sorted(self.pipes)}")
        if "." in s:
            oid, _, anc = s.partition(".")
            if oid in self.objects:
                return S.anchor_of(self.objects[oid], anc)
        if s in self.objects:
            return self.objects[s].base
        if s in self.pipes:
            return self.pipes[s].path[0]
        raise KeyError(
            f"无法解析引用 '{ref}'。已定义设备：{sorted(self.objects)}；管线：{sorted(self.pipes)}"
        )

    # -- 管线 ---------------------------------------------------------------
    def anchor_dir_of(self, ref: str) -> Optional[Pt]:
        """引用 `"T-101.top"` 的**出线方向**；非设备锚点引用返回 None。"""
        s = str(ref).strip()
        if "@" in s or "." not in s:
            return None
        oid, _, anc = s.partition(".")
        g = self.objects.get(oid)
        return anchor_direction(g, anc) if g is not None else None

    def translate(self, dx: float, dy: float,
                  skip_layers: Sequence[str] = ("BORDER",)) -> None:
        """平移图纸内容，同时把管线记录（路径与箭头位置）一起挪，避免索引与实际几何脱节。"""
        super().translate(dx, dy, skip_layers)
        if dx == 0 and dy == 0:
            return
        for rec in self.pipes.values():
            rec.path = [(p[0] + dx, p[1] + dy) for p in rec.path]
            rec.arrows = [((p[0] + dx, p[1] + dy), d) for p, d in rec.arrows]

    def pipe(self, line_no: str, frm: str, to: str, layer: str = "PIPE_PRI",
             mode: str = "auto", via: Optional[Sequence[float]] = None,
             dn: str = "", grade: str = "", medium: str = "",
             insulation: str = "", arrows: int = 0) -> PipeRec:
        """按引用布一条正交管线，并登记到图纸。

        `mode` 以 `stub` 开头时走**带接管段**的路径（见 `stub_path`），
        后半段（如 `stubVH` 的 `VH`）是接管段之间那一段的连接方式。
        这是"管线穿设备"的根治手段，也是自动修正的首选候选。
        """
        for ref in (frm,to):
            equipment, _, port = ref.partition('.')
            owner = self.objects.get(equipment)
            allowed = owner.meta.get('process_anchor_names') if owner is not None else None
            if '@' not in ref and allowed and port not in allowed:
                raise ValueError(f"流股{line_no}不能连接{ref}：换热器物流只允许左右水平箭头")
        p1, p2 = self.resolve(frm), self.resolve(to)
        if is_stub_route(mode):
            path = stub_path(p1, self.anchor_dir_of(frm), p2, self.anchor_dir_of(to),
                             mode=str(mode)[4:] or "auto", via=via)
        else:
            path = ortho_path(p1, p2, mode=mode, via=via)
        # 两端各裁掉插进设备本体的那一段（详见 `trim_path_at_body`）：
        # 「管线穿设备」的根因就是端点写在设备中心，大符号一画就有一截走在体内。
        for _ref, _tail in ((frm, False), (to, True)):
            path = trim_path_at_body(path, body_rect_of(self, _ref), tail=_tail)
        rec = PipeRec(line_no=line_no, path=path, layer=layer, dn=dn, grade=grade,
                      medium=medium, insulation=str(insulation or ""),
                      source_ref=frm, target_ref=to)
        for a, b in rec.segments:
            self.line(a, b, layer, note="stream:" + line_no)
        self.pipes[line_no] = rec
        if arrows:
            self.add_arrows(line_no, arrows)
        return rec

    @staticmethod
    def _auto_arrows(rec: PipeRec, n: int, avoid: Sequence[Pt] = (),
                     clearance: float = 8.0) -> List[Tuple[Pt, str]]:
        """箭头在直线段内挑选，避让编号框及已放箭头。"""
        out: List[Tuple[Pt, str]] = []
        for i in range(max(0, n)):
            target = rec.point_at((i + 1) / (n + 1))
            candidates = []
            for a, b in rec.segments:
                length = math.dist(a, b)
                if length < 8.0:
                    continue
                u = ((target[0] - a[0]) * (b[0] - a[0]) +
                     (target[1] - a[1]) * (b[1] - a[1])) / (length * length)
                ratios = [0.25, 0.5, 0.75, 4.0 / length, 1.0 - 4.0 / length,
                          max(4.0 / length, min(1.0 - 4.0 / length, u))]
                # 加入禁放区两端，避免离散采样漏掉实际存在的空位。
                for q, radius in [(q, clearance) for q in avoid] + [(q, 8.0) for q, _ in out]:
                    along = ((q[0] - a[0]) * (b[0] - a[0]) +
                             (q[1] - a[1]) * (b[1] - a[1])) / length
                    normal2 = max(0.0, math.dist(q, a) ** 2 - along ** 2)
                    if normal2 < radius ** 2:
                        half = math.sqrt(radius ** 2 - normal2) + 1e-6
                        ratios.extend(((along - half) / length, (along + half) / length))
                direction = ("right" if b[0] > a[0] else "left") if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else ("up" if b[1] > a[1] else "down")
                for ratio in ratios:
                    if min(ratio, 1.0 - ratio) * length < 4.0:
                        continue
                    pt = lerp(a, b, ratio)
                    if any(math.dist(pt, q) < clearance for q in avoid):
                        continue
                    if any(math.dist(pt, q) < 8.0 for q, _ in out):
                        continue
                    candidates.append((pt, direction))
            if candidates:
                out.append(min(candidates, key=lambda v: math.dist(v[0], target)))
        return out

    def add_arrows(self, line_no: str, n: int = 1, *, avoid: Sequence[Pt] = (),
                   clearance: float = 8.0) -> None:
        rec = self.pipes[line_no]
        rec.arrows = self._auto_arrows(rec, n, avoid, clearance)
        for p, direction in rec.arrows:
            S.add_flow_arrow(self, p, direction)
        if len(rec.arrows) < n:
            self.meta.setdefault("annotation_issues", []).append({
                "level": "error", "rule": "流向箭头净距",
                "msg": f"管线 {line_no} 的直线段不足以放置 {n} 个箭头并避让物流编号，请加长或重排管线"})


    # -- 标注 ---------------------------------------------------------------
    def tag_pipe(self, line_no: str, at: Optional[Pt] = None, t: float = 0.5) -> None:
        rec = self.pipes[line_no]
        p = at or rec.point_at(t)
        S.add_pipe_tag(self, (p[0] + 2.0, p[1] + 3.0), line_no=rec.line_no,
                       dn=rec.dn, grade=rec.grade, medium=rec.medium,
                       insulation=rec.insulation)


def _segment_containing(rec: PipeRec, t: float) -> Optional[Tuple[Pt, Pt]]:
    segs = rec.segments
    if not segs:
        return None
    lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
    total = sum(lens) or 1.0
    want, acc = t * total, 0.0
    for seg, length in zip(segs, lens):
        if acc + length >= want:
            return seg
        acc += length
    return segs[-1]


# ---------------------------------------------------------------------------
# 三、几何索引与合规校验
# ---------------------------------------------------------------------------

class GeometryIndex:
    """把图纸里「真实存在的几何」建成索引：测量点是否落在管线/设备上，据此判定。"""

    def __init__(self, d: ChemDrawing):
        self.pipe_segments: List[Tuple[Pt, Pt, str]] = []
        self.equip_segments: List[Tuple[Pt, Pt, str]] = []
        for it in d.items:
            ents = it.ents if isinstance(it, Group) else [it]
            owner = it.meta.get("id", "") if isinstance(it, Group) else ""
            for e in ents:
                lay = resolve_layer(e.layer)
                if isinstance(e, Line):
                    pair = (e.p1, e.p2)
                elif isinstance(e, Polyline):
                    pair = None
                    for a, b in zip(e.pts, e.pts[1:]):
                        (self.pipe_segments if lay.startswith("PIPE") else self.equip_segments) \
                            .append((a, b, owner))
                    if e.closed and len(e.pts) > 2:
                        (self.pipe_segments if lay.startswith("PIPE") else self.equip_segments) \
                            .append((e.pts[-1], e.pts[0], owner))
                    continue
                else:
                    continue
                if lay.startswith("PIPE"):
                    self.pipe_segments.append((pair[0], pair[1], owner))
                elif lay in ("EQUIP", "NOZZLE", "VALVE"):
                    self.equip_segments.append((pair[0], pair[1], owner))

    def on_pipe(self, pt: Pt, tol: float = 1.5) -> Optional[str]:
        for a, b, owner in self.pipe_segments:
            if _dist_point_seg(pt, a, b) <= tol:
                return owner or "pipe"
        return None

    def on_equipment(self, pt: Pt, tol: float = 1.5) -> Optional[str]:
        for a, b, owner in self.equip_segments:
            if _dist_point_seg(pt, a, b) <= tol:
                return owner or "equip"
        return None

    def on_geometry(self, pt: Pt, tol: float = 1.5) -> Optional[str]:
        return self.on_pipe(pt, tol) or self.on_equipment(pt, tol)


def validate_layers(d: ChemDrawing) -> List[Dict[str, str]]:
    """图层必须在注册表内（两套命名体系不得混用）。"""
    issues: List[Dict[str, str]] = []
    for lay in d.used_layers():
        if resolve_layer(lay) not in LAYER_REGISTRY:
            issues.append({"level": "error", "rule": "图层体系",
                           "msg": f"图层 '{lay}' 不在 HG 20519 语义命名注册表内，无法按图层校核线宽"})
    numeric = [l for l in d.used_layers() if l.strip().isdigit()]
    semantic = [l for l in d.used_layers() if l in LAYER_REGISTRY]
    if numeric and semantic:
        issues.append({"level": "error", "rule": "图层体系",
                       "msg": f"同时使用了数字序号制 {numeric} 与语义命名制，同一项目不得混用"})
    return issues


def validate_lineweights(d: ChemDrawing) -> List[Dict[str, str]]:
    """每个图层的线宽必须落在 HG 20519.1 的粗/中粗/细三档区间内。"""
    issues: List[Dict[str, str]] = []
    for lay in d.used_layers():
        ld = LAYER_REGISTRY.get(resolve_layer(lay))
        if ld is None:
            continue
        lo, hi = ld.weight_range
        lw = ld.lineweight
        if not (lo <= lw <= hi):
            issues.append({"level": "error", "rule": "线宽",
                           "msg": f"图层 {lay} 线宽 {lw/100:.2f}mm 超出 {lo/100:.2f}~{hi/100:.2f}mm"})
    return issues


def validate_orthogonality(d: ChemDrawing, tol: float = 0.5) -> List[Dict[str, str]]:
    """工艺线/仪表线/信号线必须水平或竖直（布线铁律）。"""
    ortho_layers = {"PIPE_PRI", "PIPE_INLINE", "AF_PIPE", "SIGNAL", "仪表线"}
    issues: List[Dict[str, str]] = []
    for e in d.iter_ents():
        if not isinstance(e, Line):
            continue
        if resolve_layer(e.layer) not in ortho_layers:
            continue
        dev = deviation_deg(e.p1, e.p2)
        if dev > tol:
            issues.append({"level": "error", "rule": "正交布线",
                           "msg": f"图层 {e.layer} 上存在斜线，偏差 {dev:.2f}°，"
                                  f"{e.p1} → {e.p2}（物料线/信号线禁止斜线）"})
    return issues


#: 调节阀传动形式的合法取值（HG 20519.2 §3.3.3）。
#: ⚠️ 这是**单一事实来源**：`validate_control_valves` 与 `META_CONTRACT` 都引用它，
#: 二者因此**不可能漂移**——新增一种传动形式只改这里。
#: `self` = 自力式：靠被调介质自身压力驱动，不接外部能源，本身就是合法传动形式。
ACTUATOR_KINDS: Tuple[str, ...] = ("pneumatic", "electric", "hydraulic", "self")

#: 失效位置的合法取值（FC 气开/失气关 · FO 气闭/失气开 · FL 保位）。
FAIL_ACTIONS: Tuple[str, ...] = ("FC", "FO", "FL")


def validate_control_valves(d: ChemDrawing) -> List[Dict[str, str]]:
    """调节阀必注项：传动形式 + 气开/气闭（HG 20519.2 §3.3.3）。"""
    issues: List[Dict[str, str]] = []
    for g in d.iter_groups():
        if g.kind != "valve" or not g.meta.get("is_final_element"):
            continue
        tag = g.tag or g.meta.get("id", "?")
        act = str(g.meta.get("actuator", ""))
        fail = str(g.meta.get("fail", ""))
        if act not in ACTUATOR_KINDS:
            issues.append({"level": "error", "rule": "调节阀必注",
                           "msg": f"调节阀 {tag} 未注传动形式"
                                  f"（气动/电动/液动/自力式），当前='{act}'"})
        if fail not in FAIL_ACTIONS:
            issues.append({"level": "error", "rule": "调节阀必注",
                           "msg": f"调节阀 {tag} 未注气开/气闭（FC 气开失气关 / FO 气闭失气开 / FL 保位），当前='{fail}'"})
    return issues


def validate_text_heights(d: ChemDrawing) -> List[Dict[str, str]]:
    """图纸数字字母高度 2~3 mm（HG 20519.1）。"""
    issues: List[Dict[str, str]] = []
    for e in d.iter_ents():
        if not isinstance(e, Text):
            continue
        if resolve_layer(e.layer) not in ("TAG", "仪表", "INST", "DIM"):
            continue
        if not (1.9 <= e.height <= 3.01):
            issues.append({"level": "warning", "rule": "文字高度",
                           "msg": f"文字 '{e.text[:12]}' 高度 {e.height}mm 超出 2~3mm"})
    return issues


def validate_tags(d: ChemDrawing) -> List[Dict[str, str]]:
    """仪表位号构成（功能字母组 + 回路序号；首位字母=被测变量）。"""
    issues: List[Dict[str, str]] = []
    for g in d.iter_groups():
        if g.kind != "instrument" or not g.tag:
            continue
        for p in S.validate_tag(g.tag):
            issues.append({"level": "warning", "rule": "仪表位号", "msg": p})
    return issues


def report_alias_choices(d: ChemDrawing) -> List[Dict[str, str]]:
    """汇总多义块（`FM`/`TJF`/`PS`，一块两义）的消歧结果——**这些块最容易被画错**。

    刻意**不并入 `CHECKS`**：默认含义（如 `FM`→闸阀）在正常图里遍地都是，
    若每次都报警会把真问题淹掉。改为每条都留痕（`Group.meta.alias_*`），
    由本函数把「**按默认猜的**」挑出来，供出图前人工复核。
    """
    out: List[Dict[str, str]] = []
    for g in d.iter_groups():
        blk = g.meta.get("alias_block")
        if not blk:
            continue
        out.append({
            "id": str(g.meta.get("id", g.name)),
            "block": str(blk),
            "symbol": str(g.meta.get("alias_symbol", "")),
            "reason": str(g.meta.get("alias_reason", "")),
            "by_default": "是" if g.meta.get("alias_is_default") else "否",
        })
    return out


def validate_flow_arrows(d: ChemDrawing) -> List[Dict[str, str]]:
    """主物料管道必须有流向箭头（HG 20519.2：P&ID 须表示物料流向）。

    这是「规范列了、但早期实现没兜底」的一条：`arrows` 缺省为 0，于是
    "没写箭头"能一路通过自检，而图面上管道是没有流向的。本校验器把它变成
    可执行约束——**只对主物料管道（PIPE_PRI）报**，公用工程/仪表线不强制
    （管径小、走向靠设备位置即可判读，行业惯例如此）。
    """
    out: List[Dict[str, str]] = []
    for rec in d.pipes.values():
        if resolve_layer(rec.layer) != "PIPE_PRI":
            continue
        if not rec.arrows:
            out.append({"level": "warning", "rule": "流向箭头",
                        "msg": f"主物料管道 {rec.line_no} 未画流向箭头"
                               f"（HG 20519.2 要求 P&ID 表示物料流向）"})
    return out


#: 需要「入口压降受限注记」的阀型（HG 20519.2 §3.3.4）
_RELIEF_KINDS = ("relief", "安全阀", "泄压阀", "vacuum_relief", "真空安全阀", "VRL", "VRR")


def validate_relief_valves(d: ChemDrawing) -> List[Dict[str, str]]:
    """安全阀入口压降受限时须注明入口管段长度及弯头数量（HG 20519.2 §3.3.4）。

    早期这条只写在文档里，没有代码兜底——「文档要求 / 代码不管」的条文
    等于没要求。现在对每只安全阀核对 `inlet_note`（规格里 `params.inlet_note`）。
    """
    out: List[Dict[str, str]] = []
    for g in d.iter_groups():
        if g.kind != "valve":
            continue
        if not (g.meta.get("is_relief") or g.ref in _RELIEF_KINDS or g.name.endswith("RELIEF")):
            continue
        if not str(g.meta.get("inlet_note", "")).strip():
            tag = g.tag or g.meta.get("id", "?")
            out.append({"level": "warning", "rule": "安全阀注记",
                        "msg": f"安全阀 {tag} 未注入口管段长度及弯头数量"
                               f"（HG 20519.2 §3.3.4；规格里用 params.inlet_note 给）"})
    return out


# ---------------------------------------------------------------------------
# 三之一、版面几何原语 + 三类「丑」的可计算化
# ---------------------------------------------------------------------------
# 背景：正交性 / 线宽 / 位号三组校验都是**逐实体**的——单看每个实体都合法，
# 于是「文字压线、符号相撞、管线穿设备」这三类**关系型**缺陷一条都查不出。
# 技能文档过去只能承认「自动校核查不出丑，靠出图目视」。
#
# 关系型判定的共同前提是**每个对象都要有真正的面积**：
#   * `Text.points()` 只返回基点 → 直接拿它算包围盒会**退化成一个点**，压线永远查不出；
#   * `Group.points()` 返回内部实体关键点，可以并成包围盒；
#   * 圆心/弧心要用 `points()` 给的 ±r 角点，不能只用圆心。
# 所以下面先补「矩形 + 线段-矩形裁剪」几个原语，再让三组校验器共用。

#: 版面矩形，形如 (x0, y0, x1, y1)
Rect = Tuple[float, float, float, float]

#: 文字包围盒向外收缩量（mm）——小于这个净距才算「压线」，
#: 免得把「紧贴但不重叠」误报成压线
TEXT_LINE_GAP: float = 0.2
# 用户截图修正：整套标注与流股线/其他标注的最小净距。
STREAM_ANNOTATION_GAP: float = 3.0
PIPE_EDGE_GAP: float = 3.0
PIPE_PREFERRED_GAP: float = 12.0
SHARED_PORT_STEM: float = 18.0

#: 符号之间允许的最小净距（mm）——小于它算「过近」（提示级）
SYMBOL_CLEARANCE: float = 2.0

#: 管线穿设备：首尾各让出这么长不算「穿」（管口/接管段本身就是插进设备的）
NOZZLE_TOLERANCE: float = 6.0

#: 管线在设备内部累计超过这么长才报（mm）——避免切角擦边误报
PIPE_INSIDE_MIN: float = 2.0


def rect_of_points(pts: Sequence[Pt]) -> Optional[Rect]:
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def expand_rect(r: Rect, m: float) -> Rect:
    return (r[0] - m, r[1] - m, r[2] + m, r[3] + m)


def rect_size(r: Rect) -> Tuple[float, float]:
    return (r[2] - r[0], r[3] - r[1])


def rect_center(r: Rect) -> Pt:
    return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0)


def rect_contains(r: Rect, p: Pt, tol: float = 0.0) -> bool:
    return (r[0] - tol <= p[0] <= r[2] + tol) and (r[1] - tol <= p[1] <= r[3] + tol)


def rect_gap(a: Rect, b: Rect) -> float:
    """两矩形净距；**负值 = 重叠深度**（取"最小分离距离"，即分离它们所需的最短位移）。

    用「最小分离距离」而不是"重叠面积"，是因为后续修正动作要的正是它——
    沿穿透最浅的一轴推开这么多就能分开。
    """
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx < 0.0 and dy < 0.0:
        return max(dx, dy)                     # 两轴都穿透 → 取绝对值小者（负）
    return math.hypot(max(dx, 0.0), max(dy, 0.0))


def seg_rect_clip(a: Pt, b: Pt, r: Rect) -> Optional[Tuple[float, float]]:
    """线段被矩形裁剪后的参数区间 (t0, t1) ∈ [0,1]；不相交返回 None（Liang–Barsky）。"""
    x0, y0, x1, y1 = r
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0.0:
                return None
            continue
        t = q / p
        if p < 0.0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (t0, t1) if t1 > t0 else None


def seg_inside_rect_len(a: Pt, b: Pt, r: Rect) -> float:
    """线段落在矩形内部的长度（mm）。"""
    t = seg_rect_clip(a, b, r)
    if t is None:
        return 0.0
    return (t[1] - t[0]) * math.hypot(b[0] - a[0], b[1] - a[1])


def body_rect_of(d: "ChemDrawing", ref: str, min_side: float = 4.0) -> Optional[Rect]:
    """引用所指设备的**本体包围盒**（只取 EQUIP 层；管口、位号不算"体内"）。

    与 `detect_pipe_through_equipment` **同口径**（同一 `min_side`、同一内缩 0.5），
    这样"裁掉插入本体的那一段"和"判定是否穿设备"用的是同一个矩形——
    不会出现裁完仍被判穿、或该裁没裁的错位。
    """
    s = str(ref).strip()
    oid = s.partition("@")[0].partition(".")[0]
    g = d.objects.get(oid)
    if g is None or getattr(g, "kind", None) != "equipment":
        return None
    r = group_rect(g, layers=("EQUIP",))
    if r is None:
        return None
    w, h = rect_size(r)
    if w < min_side or h < min_side:
        return None
    return expand_rect(r, -0.5)


def _rect_exit_point(a: Pt, b: Pt, r: Rect) -> Pt:
    """线段 a→b 从矩形 r **内部**走到外部时的出界点（要求 a 在 r 内）。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = 1.0
    if dx > 1e-12:
        t = min(t, (r[2] - a[0]) / dx)
    elif dx < -1e-12:
        t = min(t, (r[0] - a[0]) / dx)
    if dy > 1e-12:
        t = min(t, (r[3] - a[1]) / dy)
    elif dy < -1e-12:
        t = min(t, (r[1] - a[1]) / dy)
    t = max(0.0, min(1.0, t))
    return (a[0] + dx * t, a[1] + dy * t)


def trim_path_at_body(path: Sequence[Pt], body: Optional[Rect],
                      tail: bool = False) -> List[Pt]:
    """把折线**插入设备本体的那一端裁掉**，让管线止于设备轮廓（管口）。

    为什么必须裁：规格里管线端点写的是设备**中心**，塔/罐这类大符号一画，
    管线就有一大截走在设备体内——让开 6mm 管口余量后仍剩 8~15mm，
    被判「管线穿设备」（拆张实测 L0118 在 T0101 体内走 8.5 mm）。
    真实图纸里管线是**接到设备轮廓上的**，不是插到中心；裁掉这一段，
    视觉上管子仍然贴着设备，但不再有"从设备体内横穿"的几何事实。

    `tail=True` 时从**末端**往回裁（处理 `to` 端）。
    整条路径都在体内时（同一台设备的两个锚点互连）保持原样不动。
    """
    pts = [(float(p[0]), float(p[1])) for p in path]
    if body is None or len(pts) < 2 or not rect_contains(body, pts[-1] if tail else pts[0]):
        return pts
    if tail:
        pts.reverse()
    cut: Optional[List[Pt]] = None
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if rect_contains(body, b):
            continue
        cut = [_rect_exit_point(a, b, body)] + pts[i + 1:]
        break
    if cut is None or len(cut) < 2:
        return [(float(p[0]), float(p[1])) for p in path]
    if tail:
        cut.reverse()
    return cut


def text_rect(t: Text, shrink: float = 0.0) -> Rect:
    """文字包围盒（**含旋转**）。

    注意：`Text.points()` 只给基点，拿它算包围盒会退化成点，
    「压线」这类判定就永远为假——本函数是那三组校验器能成立的前提。
    """
    # 写出器的生成注记宽度因子固定0.7；测量须使用同一口径。
    w = S._text_w(t.text, t.height) * 0.7
    h = t.height
    if t.align in ("center", "middle_center"):
        lo = (-w / 2.0, -h / 2.0)
    elif t.align == "right":
        lo = (-w, 0.0)
    else:
        lo = (0.0, 0.0)                        # left：基点即左端基线
    corners = [(lo[0], lo[1]), (lo[0] + w, lo[1]), (lo[0] + w, lo[1] + h), (lo[0], lo[1] + h)]
    rad = math.radians(float(t.rotation or 0.0))
    ca, sa = math.cos(rad), math.sin(rad)
    xs, ys = [], []
    for cx, cy in corners:
        xs.append(t.pt[0] + cx * ca - cy * sa)
        ys.append(t.pt[1] + cx * sa + cy * ca)
    r = (min(xs), min(ys), max(xs), max(ys))
    return expand_rect(r, -shrink) if shrink else r




def group_rect(g: Group, layers: Optional[Sequence[str]] = None) -> Optional[Rect]:
    """符号实例的包围盒；`layers` 给出时只统计这些图层上的实体。"""
    want = {resolve_layer(x) for x in layers} if layers else None
    pts: List[Pt] = []
    for e in g.ents:
        if layers is None and g.kind == "annotation" and e.note == "annotation_leader":
            continue
        if want is not None and getattr(e, '_semantic_layer', resolve_layer(e.layer)) not in want:
            continue
        if isinstance(e, Text):
            r = text_rect(e)
            pts.extend(((r[0], r[1]), (r[2], r[3])))
        else:
            pts.extend(e.points())
    return rect_of_points(pts)


def equipment_label_point(group: Group) -> Pt:
    """设备外轮廓中心竖轴的最低交点下方10mm；不计管口、箭头或位号。"""
    rect = group_rect(group, ('EQUIP',))
    if rect is None:
        raise ValueError(f'设备 {group.name} 没有可测量的外轮廓')
    cx,cy = rect_center(rect)
    ys = []
    for ent in group.ents:
        if resolve_layer(ent.layer) != 'EQUIP':
            continue
        if isinstance(ent,(Circle,Arc)):
            dx=cx-ent.center[0]
            if abs(dx)<=ent.radius+1e-9:
                dy=math.sqrt(max(0.,ent.radius**2-dx**2))
                for y in (ent.center[1]-dy,ent.center[1]+dy):
                    angle=math.degrees(math.atan2(y-ent.center[1],dx))%360
                    if isinstance(ent,Circle) or (angle-ent.start_deg)%360 <= (ent.end_deg-ent.start_deg)%360+1e-9:
                        ys.append(y)
            continue
        for a,b in entity_segments(ent):
            if min(a[0],b[0])-1e-9<=cx<=max(a[0],b[0])+1e-9:
                if abs(a[0]-b[0])<1e-9:
                    ys.extend((a[1],b[1]))
                else:
                    ys.append(a[1]+(cx-a[0])*(b[1]-a[1])/(b[0]-a[0]))
    if not ys:
        raise ValueError(f'设备 {group.name} 的中心竖轴与轮廓无交点，须确认标注基准')
    return cx,min(ys)-10.0


def entity_segments(e: Ent) -> List[Tuple[Pt, Pt]]:
    """把实体拆成线段；圆/弧按 24 边形近似（用于压线与穿设备的相交判定）。"""
    if isinstance(e, Line):
        return [(e.p1, e.p2)]
    if isinstance(e, Polyline):
        pts = list(e.pts)
        out = list(zip(pts, pts[1:]))
        if e.closed and len(pts) > 2:
            out.append((pts[-1], pts[0]))
        return out
    if isinstance(e, Solid):
        pts = list(e.pts)
        return list(zip(pts, pts[1:] + pts[:1])) if len(pts) >= 3 else []
    if isinstance(e, (Circle, Arc)):
        n = 24
        a0 = 0.0 if isinstance(e, Circle) else float(e.start_deg)
        a1 = 360.0 if isinstance(e, Circle) else float(e.end_deg)
        if a1 <= a0:
            a1 += 360.0
        ring = []
        for k in range(n + 1):
            th = math.radians(a0 + (a1 - a0) * k / n)
            ring.append((e.center[0] + e.radius * math.cos(th),
                         e.center[1] + e.radius * math.sin(th)))
        return list(zip(ring, ring[1:]))
    return []


def shrink_path(path: Sequence[Pt], head: float, tail: float) -> List[List[Pt]]:
    """把折线首尾各截掉 head / tail 长度，返回剩余的子折线段列表。

    用于「管线穿设备」：管口/接管段本身就插进设备体内，不算穿设备；
    只有**让开端口之后**仍然钻在设备里的部分才是真的画错。
    """
    pts = [(float(p[0]), float(p[1])) for p in path]
    if len(pts) < 2:
        return []
    segs = list(zip(pts, pts[1:]))
    lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
    total = sum(lens)
    if total <= head + tail + 1e-9:
        return []
    lo, hi = head, total - tail
    out: List[List[Pt]] = []
    acc = 0.0
    for (a, b), L in zip(segs, lens):
        s, e = acc, acc + L
        acc = e
        if e <= lo or s >= hi or L <= 1e-12:
            continue
        u0 = max(0.0, (lo - s) / L)
        u1 = min(1.0, (hi - s) / L)
        if u1 - u0 <= 1e-9:
            continue
        p0 = (a[0] + (b[0] - a[0]) * u0, a[1] + (b[1] - a[1]) * u0)
        p1 = (a[0] + (b[0] - a[0]) * u1, a[1] + (b[1] - a[1]) * u1)
        out.append([p0, p1])
    return out


def _obj_label(g: Group) -> str:
    return str(g.tag or g.meta.get("id") or g.name)


#: 公开别名——`chemcad_layout` 的自动修正要按**同一口径**称呼对象、跳过同样的图层，
#: 两边各抄一份必然漂移（"校核跳过图框、修正却去挪图框里的字"）
obj_label = _obj_label


def _agg_issue(level: str, rule: str, head: str, items: Sequence[str],
               max_show: int = 6) -> List[Dict[str, str]]:
    """把同类问题聚成一条（报告里逐条刷屏会淹掉其他错误）。"""
    if not items:
        return []
    shown = "；".join(items[:max_show])
    more = f"；…共 {len(items)} 处" if len(items) > max_show else ""
    return [{"level": level, "rule": rule, "msg": f"{head}{len(items)} 处：{shown}{more}"}]


# -- 三类关系型缺陷 ---------------------------------------------------------

#: 参与「文字压线」判定的图层（图框/辅助网格属图纸家具，文字压在其上是正常的）
#: ⚠️ **`TABLE` / `SECT` 是照浙工大 PFD 实测加进去的**（冲突处以浙工大为主）：
#: 拿本判据去量他们那份 `工艺物流流程图（PFD）-2004.dwg`，一个图框里就有 **119 处**
#: "文字压线"，而其中 **113 处是"框 / 表格 / 标题栏里的文字压自己的框线"**——
#:   · `13-物流表及物流信息标识` 文字压 `13-物流表` 的框线 …… 96 处
#:   · `02-标题栏文本` 压 `14-图框与标题栏` 的框线 ………………… 17 处
#:   · `14-图框与标题栏` 压自己 ………………………………… 3 处
#: 真正压到**工艺内容**（管线 / 设备 / 接续 / 界区标志）的只有 **2 处**。
#: 结论：人家**不把"家具内文字压家具线"当缺陷**——表格/标题栏/图框/工段框都是家具。
#: 所以判据收敛为：**只判「文字压工艺内容」**（管线 / 设备轮廓 / 接续 / 界区 / 箭头 /
#: 仪表信号），家具层的线一律不参与。
_TEXT_LINE_SKIP = ("TABLE", "SECT", "BORDER", "_Grid", "DEFPOINTS", "HATCH")
text_line_skip_layers = _TEXT_LINE_SKIP        # 公开别名（供 chemcad_layout 复用口径）

#: 可作为「容器」的封闭矩形：文字在其**内部**时，不判它压了容器的框线
_CONTAINER_MIN = (30.0, 15.0)
container_min_size = _CONTAINER_MIN            # 公开别名


# -- 三类关系型缺陷：**结构化探测** + 聚合报告 ------------------------------
# 探测函数返回逐项明细（谁和谁、差多少、沿哪一轴），聚合函数把它压成人读的一条。
# 分成两层是为了**单一事实来源**：报告（`validate_*`）与自动修正
# （`chemcad_layout.repair_hints`）读的是同一份明细，阈值也共用同一组常量——
# 否则"校核说没问题、修正却按另一个阈值改"这类漂移迟早出现。

def _rect_axis(a: Rect, b: Rect) -> Tuple[str, float]:
    """两矩形的「最小分离方向」与所需位移（正=分开，负=已重叠的深度）。"""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx < 0.0 and dy < 0.0:
        return ("x" if dx >= dy else "y", max(dx, dy))
    if dx < 0.0:
        return ("y", dy)
    if dy < 0.0:
        return ("x", dx)
    return ("x" if dx <= dy else "y", math.hypot(max(dx, 0.0), max(dy, 0.0)))


def detect_symbol_overlaps(d: ChemDrawing,
                           clearance: float = SYMBOL_CLEARANCE) -> List[Dict[str, object]]:
    """逐项列出符号包围盒**相交**（净距 < 0）与**过近**（0 ≤ 净距 < clearance）。

    设备用**本体**包围盒（EQUIP/EQUIP_IN 层），不含接管与法兰——
    接管本来就是伸出体外的短段，拿它当"设备边界"会把"离接管 1 mm"的阀门
    误判成"压到设备上"（实测：`塔↔调节阀 0.20 mm` 就是接管法兰造成的假相撞）。
    其余符号仍用完整包围盒（执行机构、位号都在里面）。
    """
    items: List[Tuple[Group, E.Rect]] = []
    for g in d.iter_groups():
        if not g.ents or g.kind == "frame":
            continue
        r = group_rect(g, layers=("EQUIP", "EQUIP_IN")) if g.kind == "equipment" \
            else group_rect(g)
        if r is None:
            r = group_rect(g)
        if r is not None:
            items.append((g, r))
    out: List[Dict[str, object]] = []
    for i, (ga, ra) in enumerate(items):
        for gb, rb in items[i + 1:]:
            gap = rect_gap(ra, rb)
            if gap >= clearance:
                continue
            axis, need = _rect_axis(ra, rb)
            out.append({"a": _obj_label(ga), "b": _obj_label(gb),
                        "a_kind": ga.kind, "b_kind": gb.kind,
                        "rect_a": ra, "rect_b": rb,
                        "overlap": gap < 0.0, "gap": gap,
                        "axis": axis, "need": need})
    return out


def symbol_overlap_axis(a: Rect, b: Rect) -> Tuple[str, float]:
    """公开的「最小分离方向 / 位移」——供自动修正按同一口径推开符号。"""
    return _rect_axis(a, b)


def detect_text_over_lines(d: ChemDrawing, gap: float = TEXT_LINE_GAP) -> List[Dict[str, object]]:
    """逐项列出**被线穿过**的文字（含它压的是哪个图层、压在哪个位置）。"""
    containers: List[Rect] = []
    segs: List[Tuple[Pt, Pt, str, int]] = []
    for it in d.items:
        ents = it.ents if isinstance(it, Group) else [it]
        for e in ents:
            lay = resolve_layer(e.layer)
            if lay in _TEXT_LINE_SKIP:
                continue
            cid = -1
            if isinstance(e, Polyline) and e.closed:
                r = rect_of_points(e.pts)
                if r:
                    w, h = rect_size(r)
                    if w >= _CONTAINER_MIN[0] and h >= _CONTAINER_MIN[1]:
                        cid = len(containers)
                        containers.append(r)
            for a, b in entity_segments(e):
                segs.append((a, b, lay, cid))

    out: List[Dict[str, object]] = []
    for t in d.iter_ents():
        if not isinstance(t, Text) or not t.text.strip():
            continue
        if resolve_layer(t.layer) in _TEXT_LINE_SKIP:
            continue
        r = text_rect(t, shrink=gap)
        w, h = rect_size(r)
        if w <= 0.0 and h <= 0.0:
            continue
        c = rect_center(r)
        skip_ids = {cid for cid, box in enumerate(containers) if rect_contains(box, c)}
        for a, b, lay, cid in segs:
            if cid >= 0 and cid in skip_ids:
                continue
            if seg_inside_rect_len(a, b, r) > 1e-9:
                out.append({"text": t, "rect": r, "layer": t.layer,
                            "seg_layer": lay, "seg": (a, b)})
                break
    return out


def detect_pipe_through_equipment(d: ChemDrawing, min_len: float = PIPE_INSIDE_MIN,
                                  nozzle_tol: float = NOZZLE_TOLERANCE
                                  ) -> List[Dict[str, object]]:
    """逐项列出「管线让开管口之后仍从设备本体内穿过」的管线。"""
    equip: List[Tuple[str, Rect]] = []
    for g in d.iter_groups():
        if g.kind != "equipment":
            continue
        r = group_rect(g, layers=("EQUIP",))     # 只取本体轮廓，管口不算"体内"
        if r is None:
            continue
        w, h = rect_size(r)
        if w < 4.0 or h < 4.0:
            continue
        equip.append((_obj_label(g), expand_rect(r, -0.5)))
    if not equip:
        return []

    out: List[Dict[str, object]] = []
    for line_no, rec in d.pipes.items():
        for sub in shrink_path(rec.path, nozzle_tol, nozzle_tol):
            for name, r in equip:
                got = seg_inside_rect_len(sub[0], sub[1], r)
                if got > min_len:
                    out.append({"line": line_no, "equip": name, "length": got,
                                "rect": r, "path": list(rec.path)})
                    break
    return out


def detect_text_over_texts(d: ChemDrawing) -> List[Dict[str, object]]:
    """文字之间也须检查，包含表格和工段标题，不能仅检查文字压管线。"""
    # 用户原框的MTEXT仅在IR里用简化中心点表示，不能把它当真实排版包络。
    # 原框保持原生输出；此处检查新生成文字，不对原框的近似字框作判定。
    native_frame = {id(e) for g in d.iter_groups() if g.kind == 'frame' and g.meta.get('native_geometry')
                    for e in g.ents}
    texts = [(t,text_rect(t)) for t in d.iter_ents()
             if isinstance(t,Text) and t.text.strip() and id(t) not in native_frame]
    hits = []
    for i,(a,ra) in enumerate(texts):
        for b,rb in texts[i+1:]:
            if min(ra[2],rb[2])-max(ra[0],rb[0])>.05 and min(ra[3],rb[3])-max(ra[1],rb[1])>.05:
                hits.append({'a':a,'b':b})
    return hits


def validate_text_overlap(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    hits = [f"「{h['a'].text}」与「{h['b'].text}」重叠" for h in detect_text_over_texts(d)]
    return _agg_issue('error','文字重叠','文字包围盒相交',hits)


def validate_pipe_connections(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    hits = []
    for rec in d.pipes.values():
        for ref,point in ((rec.source_ref,rec.path[0]),(rec.target_ref,rec.path[-1])):
            if '@' in ref or '.' not in ref:
                continue
            owner = d.objects.get(ref.partition('.')[0])
            if owner is None or owner.kind != 'equipment':
                continue
            allowed = owner.meta.get('process_anchor_names')
            if allowed and ref.partition('.')[2] not in allowed:
                hits.append(f"{rec.line_no}的{ref}接在非物流箭头；换热器只允许左右水平接管")
                continue
            if math.dist(point,d.resolve(ref)) > .03:
                hits.append(f"{rec.line_no} 端点未落在 {ref}（管线裁切后断开）")
                continue
            segments = [s for e in owner.ents if resolve_layer(e.layer) in ('EQUIP','EQUIP_IN','NOZZLE')
                        for s in entity_segments(e)]
            def distance(seg):
                a,b=seg
                v=(b[0]-a[0],b[1]-a[1]); length=v[0]**2+v[1]**2
                t=max(0.,min(1.,((point[0]-a[0])*v[0]+(point[1]-a[1])*v[1])/length)) if length else 0.
                return math.dist(point,(a[0]+t*v[0],a[1]+t*v[1]))
            if not segments or min(map(distance,segments)) > .03:
                hits.append(f"{rec.line_no} 的 {ref} 悬空，未接触源图块几何")
    return _agg_issue('error','流股设备连接','流股端点必须接触对应设备图形',hits)


def validate_symbol_overlap(d: ChemDrawing, clearance: float = SYMBOL_CLEARANCE,
                            strict: bool = False) -> List[Dict[str, str]]:
    """符号相撞 / 过近：两个符号实例的包围盒相交（净距 < 0）即「相撞」。

    这是三类里最不能被接受的：两只泵叠在一起，读图人根本分不清哪台是哪台，
    而正交性、线宽、位号三组校验**一条都不会报**。
    `strict=True`（引擎已承诺自动排版）时相撞记 error，否则记 warning。
    """
    hit = detect_symbol_overlaps(d, clearance)
    overlaps = [f"{h['a']}↔{h['b']} 相交 {abs(h['gap']):.1f} mm"          # type: ignore[arg-type]
                for h in hit if h["overlap"]]
    near = [f"{h['a']}↔{h['b']} {h['gap']:.1f} mm"                          # type: ignore[arg-type]
            for h in hit if not h["overlap"]]
    out = _agg_issue("error" if strict else "warning", "符号重叠",
                     "符号包围盒相交（相撞，必须分开）", overlaps)
    out += _agg_issue("warning", "符号过近",
                      f"符号净距 < {clearance:.0f} mm（读图易误认为一体）", near)
    return out


def validate_text_over_line(d: ChemDrawing, gap: float = TEXT_LINE_GAP,
                            strict: bool = False) -> List[Dict[str, str]]:
    """文字压线：文字包围盒与线（管线/设备轮廓/信号线…）相交。

    实现要点（两条都不能省，否则要么查不出、要么满天误报）：
      * 文字必须有**真面积**——用 `text_rect()`（含旋转），不能用 `Text.points()`；
      * 文字位于某个封闭框**内部**时，不判它压了这个框自己的框线
        （说明框、标题栏里的字贴着框线是正常的）。
    """
    hits = [f"「{str(h['text'].text)[:14]}」({h['layer']}) 压 {h['seg_layer']} 线 @"
            f"({h['text'].pt[0]:.0f},{h['text'].pt[1]:.0f})"                  # type: ignore[union-attr]
            for h in detect_text_over_lines(d, gap)]
    return _agg_issue("error" if strict else "warning", "文字压线",
                      "文字框与线相交（字被线穿过）", hits)


def validate_pipe_through_equipment(d: ChemDrawing, min_len: float = PIPE_INSIDE_MIN,
                                    nozzle_tol: float = NOZZLE_TOLERANCE,
                                    strict: bool = False) -> List[Dict[str, str]]:
    """管线穿设备：管线**让开管口之后**仍从设备体内穿过。

    端口附近让出 `nozzle_tol`——接管/管口本来就插进设备体内，不算画错；
    只有让开端口后还在设备里走的部分才报（正交性校验对这类错误完全无感）。

    ⚠️ **恒判 error**，不看 `strict`：用户强制规定「**管线穿设备不允许出现**」。
    符号重叠 / 文字压线的级别随"引擎是否承诺自动排版"在 error/warning 之间浮动
    （手工摆位时只是 warning）；但**穿设备是画错，不是排版偏好**——管子从设备
    本体里穿过去在任何情况下都不允许，所以这里与是否自动排版无关，恒为 error。
    `strict` 参数仅为与 `COLLISION_CHECKS` 同签名而保留。
    """
    hits = [f"{h['line']} 在 {h['equip']} 体内走 {h['length']:.1f} mm"       # type: ignore[arg-type]
            for h in detect_pipe_through_equipment(d, min_len, nozzle_tol)]
    return _agg_issue("error", "管线穿设备",
                      "管线穿过设备本体（须绕行或改接其他管口）", hits)


def stream_table_rect(group: Group) -> Optional[Rect]:
    """读取参数框实际几何，随整体平移自然更新。"""
    if group.name != 'STREAM_DATA':
        return None
    for ent in group.ents:
        if isinstance(ent, Polyline) and ent.closed and resolve_layer(ent.layer) == 'TABLE':
            return rect_of_points(ent.pts)
    return None


def stream_annotation_parts(group: Group) -> List[Tuple[str, Rect]]:
    """框、外露引线和菱形的世界坐标禁区；菱形保守采用外包矩形。"""
    rect = stream_table_rect(group)
    if rect is None:
        return []
    parts = [('frame', rect)]
    parts += [('leader', rect_of_points((ent.p1, ent.p2))) for ent in group.ents
              if isinstance(ent, Line) and ent.note == 'annotation_leader']
    q = group.base
    half = 10.0 * math.sqrt(2.0) / 2.0
    parts.append(('diamond', (q[0]-half, q[1]-half, q[0]+half, q[1]+half)))
    return parts


def stream_host_segment(group: Group, a: Pt, b: Pt, owner: str) -> bool:
    """只豁免同一流股承载菱形的直线，折回段与其他流股不豁免。"""
    if not group.ref or group.ref != owner:
        return False
    q = group.base
    if group.meta.get('stream_placement') == 'right':
        return abs(a[0]-b[0]) < 1e-3 and abs(a[0]-q[0]) < 1e-3
    return abs(a[1]-b[1]) < 1e-3 and abs(a[1]-q[1]) < 1e-3


def stream_annotation_pipe_hits(group: Group, segments,
                                gap: float = STREAM_ANNOTATION_GAP) -> List[Dict[str, object]]:
    hits = []
    for kind, rect in stream_annotation_parts(group):
        protected = expand_rect(rect, gap)
        for a,b,layer,owner in segments:
            if kind == 'diamond' and stream_host_segment(group,a,b,owner):
                continue
            if seg_inside_rect_len(a,b,protected) > 1e-9:
                hits.append({'rect': rect, 'protected_rect': protected, 'part': kind,
                             'stream': group.ref, 'seg': (a,b), 'seg_layer': layer,
                             'pipe': owner})
    return hits


def detect_pipe_over_stream_frames(d: ChemDrawing,
                                  gap: float = STREAM_ANNOTATION_GAP) -> List[Dict[str, object]]:
    """兼容原入口名：同时检查框、引线、菱形及不同标注之间的净距。"""
    segments = [(a,b,resolve_layer(ent.layer), getattr(ent,'note','')) for ent in d.iter_ents()
                if resolve_layer(ent.layer) in ('PIPE_PRI','PIPE_INLINE','AF_PIPE')
                for a,b in entity_segments(ent)]
    groups = [g for g in d.iter_groups() if g.name == 'STREAM_DATA']
    hits = [h for g in groups for h in stream_annotation_pipe_hits(g,segments,gap)]
    for i, group in enumerate(groups):
        for other in groups[i+1:]:
            for kind, rect in stream_annotation_parts(group):
                for other_kind, other_rect in stream_annotation_parts(other):
                    if rect_gap(rect,other_rect) < gap-1e-6:
                        hits.append({'rect': rect, 'part': kind, 'stream': group.ref,
                                     'other_rect': other_rect, 'other_part': other_kind,
                                     'other_stream': other.ref})
    return hits


def pipe_width_mm(layer: str) -> float:
    """取实际默认图层线宽，不使用旧语义层的近似线宽。"""
    import chemcad_layers as LY
    definition = LY.layer_definition(LY.std_name(resolve_layer(layer)))
    if definition is None or definition.lineweight < 0:
        raise ValueError(f'流股层{layer}没有明确物理线宽，不能计算线间净距')
    return definition.lineweight / 100.0


def shared_port_stems(path, refs, other_path, other_refs):
    """仅同一设备、同一几何接点的短直段可共用；不豁免外围长走廊。"""
    stems = []
    for ref, p, q in ((refs[0],path[0],path[1]), (refs[1],path[-1],path[-2])):
        for other_ref, op in ((other_refs[0],other_path[0]),(other_refs[1],other_path[-1])):
            if '@' in ref or ref.partition('.')[0] != other_ref.partition('.')[0] or math.dist(p,op) > .001:
                continue
            axis = 0 if abs(p[1]-q[1]) < .001 else 1
            sign = 1 if q[axis] > p[axis] else -1
            end = list(p)
            end[axis] += sign * SHARED_PORT_STEM
            stems.append((axis, p[1-axis], min(p[axis],end[axis]), max(p[axis],end[axis])))
    return stems


def pipe_spacing_bands(path, layer, other_path, other_layer, stems=(), *, search=False, spacing=PIPE_EDGE_GAP):
    """将已布平行段变成单方向禁区，保留垂直交叉的合法寻路。"""
    gap = spacing + (pipe_width_mm(layer)+pipe_width_mm(other_layer))/2 + (.01 if search else -.001)
    bands = []
    for a,b in zip(other_path,other_path[1:]):
        axis = 0 if abs(a[1]-b[1]) < .001 else 1
        normal = (a[1-axis]+b[1-axis])/2
        intervals = [(min(a[axis],b[axis]),max(a[axis],b[axis]))]
        for sa,sn,sl,sh in stems:
            if sa != axis or abs(sn-normal) > .001:
                continue
            if search:
                # 共用接管只允许同轴，不能连同其旁边的平行禁区一起挖掉。
                for lo,hi in intervals:
                    u,v=max(lo,sl),min(hi,sh)
                    if v-u<=1e-6:
                        continue
                    # 同轴世界坐标浮点误差不能把共用接管误判为旁边平行线。
                    for low,high in ((normal-gap,normal-.001),(normal+.001,normal+gap)):
                        rect=(u,low,v,high) if axis==0 else (low,u,high,v)
                        bands.append((axis,rect))
            intervals = [(u,v) for lo,hi in intervals
                         for u,v in ((lo,min(hi,sl)),(max(lo,sh),hi)) if v-u > 1e-6]
        for lo,hi in intervals:
            rect = (lo,normal-gap,hi,normal+gap) if axis==0 else (normal-gap,lo,normal+gap,hi)
            bands.append((axis,rect))
    if search:
        # 折点周围也留净空，防止垂直交叉落在断口无法容纳的转角上。
        for p in other_path:
            if any(abs(p[1-axis]-normal)<.001 and lo-.001<=p[axis]<=hi+.001
                   for axis,normal,lo,hi in stems):
                continue
            r=(p[0]-gap,p[1]-gap,p[0]+gap,p[1]+gap)
            bands.extend(((0,r),(1,r)))
    return bands


def equipment_flow_rect(group: Group) -> Optional[Rect]:
    """所有设备本体及原块外露接管、箭头、阀件的共同保护范围。"""
    return group_rect(group,layers=('EQUIP','NOZZLE','FLOWARROW','VALVE')) if group.kind=='equipment' else None


def heat_exchanger_flow_rect(group: Group) -> Optional[Rect]:
    return equipment_flow_rect(group) if str(group.meta.get('library_block','')).startswith('hx.') else None


def shared_port_plan(d: ChemDrawing, rows):
    """同一实际设备接点的主管与支线；别名由世界坐标归并。"""
    grouped={}
    for line,layer,refs in rows:
        for ref in refs:
            equipment=ref.partition('.')[0]
            if '@' in ref or equipment not in d.objects:
                continue
            point=d.resolve(ref); direction=d.anchor_dir_of(ref)
            if not direction:
                continue
            key=(equipment,round(point[0],4),round(point[1],4))
            entry=grouped.setdefault(key,{'point':point,'direction':direction,'connections':[]})
            entry['connections'].append((line,ref,resolve_layer(layer)))
    out=[]
    for entry in grouped.values():
        connections=entry.pop('connections')
        if len(connections)<3:
            continue
        connections.sort(key=lambda row:row[2]!='PIPE_PRI')
        entry.update({'main':connections[0][:2],'branches':[row[:2] for row in connections[1:]]})
        out.append(entry)
    return out


def shared_port_alignment(d: ChemDrawing):
    """支线在一个公共折点接入主管，不得形成相邻错开的T形连接。"""
    rows=[]; hits=[]
    plan=shared_port_plan(d,((rec.line_no,rec.layer,(rec.source_ref,rec.target_ref)) for rec in d.pipes.values()))
    for entry in plan:
        p=entry['point']; direction=entry['direction']; axis=0 if direction[0] else 1
        points=[]
        for line,ref in entry['branches']:
            rec=d.pipes[line]
            path=rec.path if ref==rec.source_ref else list(reversed(rec.path))
            bend=next((a for a,b in zip(path,path[1:]) if abs(a[1-axis]-b[1-axis])>.001),None)
            points.append({'pipe':line,'point':bend})
        bends=[row['point'] for row in points]
        main=d.pipes[entry['main'][0]]
        valid=all(q is not None and abs(q[1-axis]-p[1-axis])<.001
                  and .001<(q[axis]-p[axis])*direction[axis]<=SHARED_PORT_STEM+.001 for q in bends)
        if valid:
            valid=all(math.dist(q,bends[0])<=.001 for q in bends)
            valid=valid and any(_dist_point_seg(bends[0],a,b)<=.001 for a,b in main.segments)
        row={'port':list(p),'main':entry['main'][0],'branches':points,'aligned':valid}
        rows.append(row)
        if not valid:
            hits.append(row)
    return rows,hits


def validate_shared_port_alignment(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    return _agg_issue('error','共用接管分支错位','同一设备接点的支线须在一个公共折点与主管对齐',
                      [str(hit) for hit in shared_port_alignment(d)[1]])


def detect_pipe_spacing(d: ChemDrawing) -> List[Dict[str,object]]:
    hits=[]
    segments=[(a,b,str(ent.layer),getattr(ent,'note','')) for ent in d.iter_ents()
              if resolve_layer(ent.layer) in ('PIPE_PRI','PIPE_INLINE','AF_PIPE')
              for a,b in entity_segments(ent)]
    for i,(a,b,layer,owner) in enumerate(segments):
        rec=d.pipes.get(owner.removeprefix('stream:'))
        for c,e,other_layer,other_owner in segments[i+1:]:
            other=d.pipes.get(other_owner.removeprefix('stream:'))
            stems=shared_port_stems(rec.path,(rec.source_ref,rec.target_ref),other.path,
                                    (other.source_ref,other.target_ref)) if rec and other and rec is not other else ()
            for axis,rect in pipe_spacing_bands((a,b),layer,(c,e),other_layer,stems):
                if abs(a[1-axis]-b[1-axis]) > .001:
                    continue
                if seg_inside_rect_len(a,b,rect) > .001:
                    distance=abs((a[1-axis]+b[1-axis]-c[1-axis]-e[1-axis])/2)
                    hits.append({'pipe':owner,'other_pipe':other_owner,'seg':(a,b),'other_seg':(c,e),
                                 'edge_gap':distance-(pipe_width_mm(layer)+pipe_width_mm(other_layer))/2})
    return hits


def detect_pipe_over_equipment_nozzles(d: ChemDrawing) -> List[Dict[str,object]]:
    hits=[]
    for group in d.iter_groups():
        rect=equipment_flow_rect(group)
        if rect is None:
            continue
        protected=expand_rect(rect,PIPE_EDGE_GAP)
        for ent in d.iter_ents():
            if resolve_layer(ent.layer) not in ('PIPE_PRI','PIPE_INLINE','AF_PIPE'):
                continue
            rec=d.pipes.get(getattr(ent,'note','').removeprefix('stream:'))
            for a,b in entity_segments(ent):
                allowed=False
                if rec:
                    for ref in (rec.source_ref,rec.target_ref):
                        if ref.partition('.')[0] != group.meta.get('id'):
                            continue
                        p=d.resolve(ref); direction=d.anchor_dir_of(ref)
                        axis=0 if direction and direction[0] else 1
                        if abs(a[1-axis]-b[1-axis]) < .001 and abs(a[1-axis]-p[1-axis]) < .001:
                            allowed=True
                if not allowed and seg_inside_rect_len(a,b,protected) > 1e-6:
                    hits.append({'equipment':group.meta.get('id'),'pipe':getattr(ent,'note',''),'seg':(a,b)})
    return hits


def detect_pipe_over_exchanger_nozzles(d: ChemDrawing) -> List[Dict[str,object]]:
    ids={g.meta.get('id') for g in d.iter_groups() if str(g.meta.get('library_block','')).startswith('hx.')}
    return [hit for hit in detect_pipe_over_equipment_nozzles(d) if hit['equipment'] in ids]


def validate_equipment_nozzle_clearance(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    return _agg_issue('error','物流穿设备接管','物流线只能沿所属接管轴连接，其他段须避开设备接管、箭头和阀件',
                      [str(h) for h in detect_pipe_over_equipment_nozzles(d)])


def validate_pipe_spacing(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    return _agg_issue('error','流股线净距不足',f'平行流股线考虑图层线宽后须留{PIPE_EDGE_GAP:g}mm净距',
                      [str(h) for h in detect_pipe_spacing(d)])


def validate_exchanger_nozzle_clearance(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    return _agg_issue('error','换热器接管避让','管线须沿设备接管轴延伸后折弯，避开原块斜箭头3mm',
                      [str(h) for h in detect_pipe_over_exchanger_nozzles(d)])


def validate_pipe_over_stream_frames(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    return _agg_issue('error','流股标注净距不足',
                      f'参数框、引线及菱形与管线和其他标注须留{STREAM_ANNOTATION_GAP:g}mm净距',
                      [str(hit) for hit in detect_pipe_over_stream_frames(d)])


def validate_equipment_label_positions(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    hits=[]
    for label in d.iter_groups():
        if label.name not in ('EQUIPMENT_LABEL', 'EQUIPMENT_FOOTER') or not label.ref.startswith(('equipment:', 'equipment_footer:')):
            continue
        owner=d.objects.get(label.ref.partition(':')[2])
        texts=[ent for ent in label.ents if isinstance(ent,Text)]
        desired=equipment_label_point(owner) if owner is not None else (0., 0.)
        if label.name == 'EQUIPMENT_FOOTER':
            frames=d.meta.get('text_frames') or [{'at': [0., 0.]}]
            desired=(desired[0], float(frames[0]['at'][1])+60.)
            centers=[rect_center(text_rect(text)) for text in texts]
            invalid=(not centers or math.dist(centers[0], desired)>.03
                     or any(abs(center[0]-desired[0])>.03 for center in centers))
        else:
            invalid=(not texts or math.dist(label.base,desired)>.03
                     or math.dist(rect_center(text_rect(texts[-1])),desired)>.03)
        if owner is None or invalid:
            hits.append(label.ref)
    return _agg_issue('error','设备标注位置','近设备短位号在最低轮廓交点下方10mm；底部位号、名称同X，位号中心距框底60mm',hits)


def validate_pipe_crossings(d: ChemDrawing, strict: bool = False) -> List[Dict[str,str]]:
    return _agg_issue('error', '流股交叉断口', '未声明连接的正十字交叉必须留断口',
                      [str(p) for _, _, p in visible_pipe_crossings(d)])


#: 版面/碰撞类检查（需要 `strict` 开关，故与 `CHECKS` 分开；
#: `preflight` 按"引擎是否承诺自动排版"决定其严重级别）
COLLISION_CHECKS = (
    validate_equipment_nozzle_clearance,
    validate_shared_port_alignment,
    validate_pipe_spacing,
    validate_exchanger_nozzle_clearance,
    validate_equipment_label_positions,
    validate_pipe_over_stream_frames,
    validate_text_overlap,
    validate_pipe_connections,
    validate_pipe_crossings,
    validate_symbol_overlap,
    validate_text_over_line,
    validate_pipe_through_equipment,
)


# ---------------------------------------------------------------------------
# 四、控制回路：定义即布线（闭环不可能被忘掉）
# ---------------------------------------------------------------------------

@dataclass
class ControlLoop:
    """一条控制回路。**它同时是数据源与绘图源**——信号线由它生成，
    因此「闭环可追踪」不是事后检查出来的，而是画不出来不闭合。

    loop_type: single 单回路 / cascade 串级 / feedforward 前馈-反馈 /
               split 分程 / multi_impulse 多冲量
    """

    loop_id: str
    variable: str                 # F / L / T / P ...
    number: str                   # 001
    controlled_object: str        # 被控对象（设备/管线 id）
    measurement_point: str        # 测量点引用，须落在对象几何上
    controller_tag: str = ""      # 控制器位号，如 FIC001
    final_element: str = ""       # 最终控制元件（调节阀）位号，如 FV001
    loop_type: str = "single"
    sub_measurement: str = ""     # 串级：副测量点（须落在副变量管线/设备上）
    sub_controller_tag: str = ""
    extra_measurements: List[str] = field(default_factory=list)   # 多冲量/前馈
    extra_finals: List[str] = field(default_factory=list)         # 分程：第 2 只阀

    @property
    def expected_controller(self) -> str:
        return self.controller_tag or f"{self.variable}IC{self.number}"

    @property
    def expected_final(self) -> str:
        return self.final_element or f"{self.variable}V{self.number}"


#: 仪表气泡半径（与 `chemcad_symbols.inst_bubble` 默认值保持一致）
BUBBLE_RADIUS: float = 5.0


def approach_edge(center: Pt, frm: Pt, r: float = BUBBLE_RADIUS) -> Pt:
    """引线应止于气泡**边缘**，而不是圆心。

    画到圆心会让引线钻进圆圈，并与圈内位号文字（上排功能字母、下排回路序号）
    叠在一起——正交性 / 线宽 / 位号三组校验**一条都不报**，
    是典型的关系型缺陷（由 `validate_text_over_line()` 兜底）。
    """
    dx, dy = center[0] - frm[0], center[1] - frm[1]
    if abs(dx) >= abs(dy):
        return (center[0] - (r if dx >= 0 else -r), center[1])
    return (center[0], center[1] - (r if dy >= 0 else -r))


def wire_loop(cd: ChemDrawing, loop: ControlLoop,
              controller_at: Optional[Pt] = None,
              sub_controller_at: Optional[Pt] = None,
              bubble_mount: str = "field") -> Dict[str, object]:
    """按回路定义自动布置仪表与信号线（全部正交）。

    过程量引线走 `仪表线` 图层，控制信号走 `SIGNAL` 图层——与 HG 20519 一致。
    **引线止于气泡边缘**（`approach_edge`），不画到圆心：否则引线与圈内位号叠在一起。
    """
    mp = cd.resolve(loop.measurement_point)
    ctrl_at = tuple(controller_at) if controller_at else (mp[0] + 30.0, mp[1] + 34.0)
    nodes: Dict[str, Pt] = {}

    # 1) 测量引线：测量点 → 控制器气泡边缘
    m_to = approach_edge(ctrl_at, mp)
    for a, b in zip(ortho_path(mp, m_to), ortho_path(mp, m_to)[1:]):
        cd.line(a, b, "仪表线")
    # 2) 控制器气泡
    g_ctrl = S.place_symbol(cd, "bubble", ctrl_at, tag=loop.expected_controller,
                            mount=bubble_mount)
    g_ctrl.kind = "instrument"
    g_ctrl.meta["id"] = f"CTRL_{loop.loop_id}"
    cd.objects[f"CTRL_{loop.loop_id}"] = g_ctrl
    nodes["controller"] = ctrl_at
    nodes["measurement"] = mp

    # 3) 串级：副测量点 → 副控制器 → 主控制器（两端都止于气泡边缘）
    if loop.loop_type == "cascade":
        if not loop.sub_measurement:
            raise ValueError(f"回路 {loop.loop_id} 是串级，必须给 sub_measurement")
        smp = cd.resolve(loop.sub_measurement)
        sc_at = tuple(sub_controller_at) if sub_controller_at else (smp[0] + 30.0, smp[1] - 34.0)
        s_path = ortho_path(smp, approach_edge(sc_at, smp))
        for a, b in zip(s_path[:-1], s_path[1:]):
            cd.line(a, b, "仪表线")
        sub_tag = loop.sub_controller_tag or f"{loop.variable}C{loop.number}B"
        g_sub = S.place_symbol(cd, "bubble", sc_at, tag=sub_tag, mount=bubble_mount)
        g_sub.meta["id"] = f"SUBCTRL_{loop.loop_id}"
        cd.objects[f"SUBCTRL_{loop.loop_id}"] = g_sub
        c_path = ortho_path(approach_edge(sc_at, ctrl_at), approach_edge(ctrl_at, sc_at))
        for a, b in zip(c_path[:-1], c_path[1:]):
            cd.line(a, b, "SIGNAL")
        nodes["sub_controller"] = sc_at
        nodes["sub_measurement"] = smp

    # 4) 前馈/多冲量：额外测量点各自取信号
    for i, ref in enumerate(loop.extra_measurements):
        ep = cd.resolve(ref)
        e_path = ortho_path(ep, approach_edge(ctrl_at, ep))
        for a, b in zip(e_path[:-1], e_path[1:]):
            cd.line(a, b, "SIGNAL")
        nodes[f"extra_{i}"] = ep

    # 5) 控制器输出 → 最终控制元件（分程则分别到每只阀）
    finals = [loop.expected_final] + list(loop.extra_finals)
    for tag in finals:
        fe = cd.objects.get(tag)
        if fe is None:
            continue
        # 接到执行机构而非阀体中心：否则信号线会与物料管线重叠（典型画错）
        fp = S.anchor_of(fe, "actuator") if S.has_anchor(fe, "actuator") else fe.base
        seg = ortho_path(approach_edge(ctrl_at, fp), fp)
        for a, b in zip(seg[:-1], seg[1:]):
            cd.line(a, b, "SIGNAL")
        nodes[f"final_{tag}"] = fp
    return nodes


def validate_control_loops(cd: ChemDrawing, loops: Sequence[ControlLoop],
                           tol: float = 1.5) -> List[Dict[str, str]]:
    """闭环可追踪性校验：五要素齐备 + 测量点**真落在**几何上 + 副测量点独立取信号。"""
    issues: List[Dict[str, str]] = []
    idx = GeometryIndex(cd)

    for lp in loops:
        tag = lp.loop_id
        # (1) 五要素齐备
        missing = [n for n, v in (("被控对象", lp.controlled_object),
                                  ("测量点", lp.measurement_point),
                                  ("控制器", lp.expected_controller),
                                  ("最终控制元件", lp.expected_final)) if not v]
        if missing:
            issues.append({"level": "error", "rule": "闭环",
                           "msg": f"回路 {tag} 缺少：{'、'.join(missing)}；"
                                  f"只有设备外形+仪表圆+装饰线属于工艺示意，不是控制方案附图"})
            continue

        # (2) 位号与回路序号一致，且首字母=被测变量
        for t, role in ((lp.expected_controller, "控制器"), (lp.expected_final, "最终元件")):
            letters, num = S.split_tag(t)
            if letters[:1].upper() != lp.variable.upper():
                issues.append({"level": "error", "rule": "位号一致性",
                               "msg": f"回路 {tag} 的{role}位号 '{t}' 首字母与变量 "
                                      f"'{lp.variable}' 不一致"})
            if num and num != lp.number:
                issues.append({"level": "error", "rule": "位号一致性",
                               "msg": f"回路 {tag} 的{role}位号 '{t}' 序号 '{num}' "
                                      f"与回路序号 '{lp.number}' 不一致（同回路须共享序号）"})

        # (3) 测量点必须落在对象几何上
        try:
            mp = cd.resolve(lp.measurement_point)
        except KeyError as e:
            issues.append({"level": "error", "rule": "闭环", "msg": str(e)})
            continue
        where = idx.on_geometry(mp, tol)
        if where is None:
            issues.append({"level": "error", "rule": "闭环",
                           "msg": f"回路 {tag} 的测量点 {mp} **不在任何管线或设备上**——"
                                  f"测量点必须实际落在被测量对象上"})

        # (4) 最终控制元件必须是调节阀且落在管线上
        fe = cd.objects.get(lp.expected_final)
        if fe is None:
            issues.append({"level": "error", "rule": "闭环",
                           "msg": f"回路 {tag} 的最终控制元件 '{lp.expected_final}' 未在图中布置"})
        else:
            if not fe.meta.get("is_final_element"):
                issues.append({"level": "error", "rule": "闭环",
                               "msg": f"回路 {tag} 的最终元件 '{lp.expected_final}' 不是调节阀符号"})
            if idx.on_pipe(fe.base, tol) is None:
                issues.append({"level": "error", "rule": "闭环",
                               "msg": f"回路 {tag} 的最终元件 '{lp.expected_final}' "
                                      f"未落在任何管线上（执行端必须落在实际管线）"})

        # (5) 串级：副测量点必须实际落在副变量管线/设备上，不能只画两个控制框
        if lp.loop_type == "cascade":
            if not lp.sub_measurement:
                issues.append({"level": "error", "rule": "闭环",
                               "msg": f"回路 {tag} 为串级，但未给副测量点"})
            else:
                smp = cd.resolve(lp.sub_measurement)
                if idx.on_geometry(smp, tol) is None:
                    issues.append({"level": "error", "rule": "闭环",
                                   "msg": f"回路 {tag} 的副测量点 {smp} 不在任何几何上——"
                                          f"串级副测量点必须实际落在副变量管线/设备上"})
                if cd.resolve(lp.measurement_point) == smp:
                    issues.append({"level": "error", "rule": "闭环",
                                   "msg": f"回路 {tag} 的主/副测量点重合，副回路形同虚设"})

        # (6) 分程：至少两只阀，且执行端分别落在实际管线
        if lp.loop_type == "split" and not lp.extra_finals:
            issues.append({"level": "error", "rule": "闭环",
                           "msg": f"回路 {tag} 为分程，但只有一只最终元件（应 ≥ 2）"})

        # (7) 前馈-反馈：扰动测量点与被控变量反馈点必须分别取信号
        if lp.loop_type == "feedforward" and not lp.extra_measurements:
            issues.append({"level": "error", "rule": "闭环",
                           "msg": f"回路 {tag} 为前馈-反馈，但未指定扰动测量点"})

        # (8) 多冲量：每个冲量都要有独立测量点
        if lp.loop_type == "multi_impulse" and len(lp.extra_measurements) < 2:
            issues.append({"level": "error", "rule": "闭环",
                           "msg": f"回路 {tag} 为多冲量，独立测量点不足"
                                  f"（当前 {len(lp.extra_measurements)} 个，需 ≥ 2）"})

    return issues


# ---------------------------------------------------------------------------
# 五、总自检
# ---------------------------------------------------------------------------

CHECKS = (
    validate_layers,
    validate_lineweights,
    validate_orthogonality,
    validate_control_valves,
    validate_text_heights,
    validate_tags,
    validate_flow_arrows,
    validate_relief_valves,
)


# ---------------------------------------------------------------------------
# 五之一、meta 契约（消费方清单）与覆盖扫描
# ---------------------------------------------------------------------------
# 背景：符号往 `Group.meta` 里写语义（传动形式、是否最终控制元件、消歧理由…），
# 校验器再消费这些键来落规范。**两端中间的接缝没人管**——
# 曾出现新增符号写 `actuator="self"`、而既有校验器只认三种传动形式，
# 单测碰不到，直到写进示例规格跑完整 preflight 才暴露。
#
# 对策两件一起做：
#   ① **契约表**（本表）：每个 meta 键必须登记 用途 / 值域 / 消费方；
#   ② **自动扫描**（`scan_meta_coverage`）：用**运行时事实**收集"谁真的产出了什么"，
#      用 **AST** 收集"哪个校验器真的读了哪个键"，再与契约表对账。
# 于是"新增符号忘了登记"或"校验器读了没声明的键"都会在 `selftest.py` 里直接失败。
#
# ⚠️ 值域**优先引用上面的常量**（`ACTUATOR_KINDS` / `FAIL_ACTIONS`），
#    让校验器与契约共用单一事实来源，杜绝两份枚举各改各的。

META_CONTRACT: Dict[str, Dict[str, object]] = {
    "id": {
        "purpose": "对象标识（`ChemDrawing.place` 写入），供校验器报错定位到具体符号",
        "values": None,
        "consumer": ("validate_control_valves", "validate_relief_valves",
                     "report_alias_choices"),
    },
    "actuator": {
        "purpose": "调节阀传动形式（HG 20519.2 §3.3.3 必注）",
        "values": set(ACTUATOR_KINDS),
        "consumer": "validate_control_valves",
    },
    "fail": {
        "purpose": "失效位置 FC 气开/FO 气闭/FL 保位；空串=尚未标注（校验器须报）",
        "values": set(FAIL_ACTIONS) | {""},
        "consumer": "validate_control_valves",
    },
    "is_final_element": {
        "purpose": "标记最终控制元件，供「调节阀必注」校验器识别",
        "values": {"True"},
        "consumer": "validate_control_valves",
    },
    "handwheel": {
        "purpose": "有无手动机构（HG 20519.2 列为必注项，当前尚无强制条文）",
        "values": {"True", "False"},
        "consumer": "",            # 暂无校验器执行
        "informational": True,     # 显式申明"仅记录"，否则扫描会判为「声明了却无人执行」
    },
    "inlet_note": {
        "purpose": "安全阀入口管段长度及弯头数（HG 20519.2 §3.3.4）；空串=未注，须报",
        "values": None,            # 自由文本
        "consumer": "validate_relief_valves",
    },
    "set_pressure": {
        "purpose": "安全阀整定压力",
        "values": None,
        "consumer": "",
        "informational": True,
    },
    "is_relief": {
        "purpose": "标记安全阀，供入口注记校验器识别",
        "values": {"True"},
        "consumer": "validate_relief_valves",
    },
    "library_file": {"purpose": "设备块源文件", "values": None, "consumer": "", "informational": True},
    "library_block": {"purpose": "设备块源名称", "values": None, "consumer": "", "informational": True},
    "native_geometry": {"purpose": "新版块原生输出；IR 几何仅供排版", "values": None, "consumer": "_emit", "informational": True},
    "source_center": {"purpose": "原生块排版中心", "values": None, "consumer": "_emit", "informational": True},
    "geometry_source": {"purpose": "设备几何来源", "values": None, "consumer": "", "informational": True},
    "curve_tolerance_mm": {"purpose": "源曲线离散弦高", "values": None, "consumer": "", "informational": True},
    "anchor_basis": {"purpose": "图面连接点口径", "values": None, "consumer": "", "informational": True},
    "alias_block": {
        "purpose": "多义块（FM/TJF/PS）的原始块名——消歧留痕",
        "values": set(S.BLOCK_AMBIGUITY.keys()),
        "consumer": "report_alias_choices",
    },
    "alias_symbol": {
        "purpose": "多义块判定后的符号名",
        "values": None,
        "consumer": "report_alias_choices",
    },
    "alias_reason": {
        "purpose": "判定理由（命中[…] / 默认）",
        "values": None,
        "consumer": "report_alias_choices",
    },
    "alias_is_default": {
        "purpose": "是否按默认猜（True→报告标「默认·待复核」）",
        "values": {"True", "False"},
        "consumer": "report_alias_choices",
    },
}

#: 被视为"消费方"的函数：`CHECKS` 里的校验器 + 闭环校验 + 多义块留痕汇总。
META_CONSUMERS = CHECKS + (validate_control_loops, report_alias_choices)


def _ast_meta_keys(fn) -> Optional[set]:
    """从消费方源码里 **AST 提取** 它实际读取的 meta 键（`.meta.get("x")` / `.meta["x"]`）。

    用 AST 而不是手抄一份清单，是为了让"消费方清单"**永不与代码脱节**：
    校验器读了个新键却没登记，扫描立刻能发现。取不到源码返回 None（由调用方当失败处理，
    **不能静默当通过**——否则扫描形同虚设）。
    """
    import ast
    import inspect
    import textwrap
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except Exception:
        return None
    keys: set = set()
    for node in ast.walk(tree):
        # g.meta.get("key", default)
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "get"
                    and isinstance(f.value, ast.Attribute) and f.value.attr == "meta"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
        # g.meta["key"]
        elif isinstance(node, ast.Subscript):
            v, sl = node.value, node.slice
            if (isinstance(v, ast.Attribute) and v.attr == "meta"
                    and isinstance(sl, ast.Constant) and isinstance(sl.value, str)):
                keys.add(sl.value)
    return keys


def sweep_symbol_meta() -> Dict[str, set]:
    """落地**每一个**已注册符号（外加未知块/线型块/多义块各一），收集实际写出的 meta 键与取值。

    用**运行时事实**而非源码猜测——这是"契约表不说谎"的前提：
    `ld.meta[...] = 变量` 这种写法，光看源码根本不知道值域。
    """
    out: Dict[str, set] = {}

    def _collect(g) -> None:
        for k, v in g.meta.items():
            if k in ("anchors", "rotation"):   # 通用落图信息，不属语义契约
                continue
            out.setdefault(k, set()).add(str(v))

    # 走 `ChemDrawing.place`（而非 `place_symbol`）才是**真实使用路径**：
    # `id` 就是 place 写入的，绕开它会让契约表凭空多出一条"冗余"。
    def _place(kind: str, tag: str = "T-1", obj_id: str = "X", **params):
        cd = ChemDrawing(name="sweep", sheet="A1")
        return cd.place(obj_id, kind, (0.0, 0.0), tag=tag, **params)

    for table in (S.EQUIPMENT, S.VALVES, S.INSTRUMENTS):
        for name in list(table.keys()):
            if not isinstance(name, str):
                continue
            try:
                _collect(_place(name))
            except Exception:
                continue                       # 个别符号可能因默认参数抛错，不阻断整体扫描
    _collect(_place("FM", tag="GV", meaning="夹套"))   # → alias_* 四件套
    return out


def scan_meta_coverage() -> Dict[str, object]:
    """对账 meta 契约：产出方 × 消费方 × 契约表，三者任何一处脱节都报出来。

    返回 dict 含 `produced` / `consumed` 明细与七类问题：
      * `unreadable`  —— 校验器源码取不到（AST 失败）：**按失败处理**，否则扫描形同虚设
      * `undeclared`  —— 有符号在写、但契约表没登记（**新增符号忘了配消费方清单**）
      * `violations`  —— 产出值不在契约值域内（防 actuator="self" 那类枚举错配）
      * `foreign`     —— 校验器读了契约表没声明的键（消费方清单过时）
      * `unenforced`  —— 声明了强约束却没有任何校验器执行（且未标 informational）
      * `mismatch`    —— 契约写的消费方 ≠ AST 实际读到的消费方
      * `stale`       —— 契约表登记了、当前无符号产出（提示，不算错）
    """
    produced = sweep_symbol_meta()

    consumed: Dict[str, set] = {}
    unreadable: List[str] = []
    for fn in META_CONSUMERS:
        keys = _ast_meta_keys(fn)
        nm = getattr(fn, "__name__", str(fn))
        if keys is None:
            unreadable.append(nm)
            continue
        for k in keys:
            consumed.setdefault(k, set()).add(nm)

    undeclared: List[str] = []
    violations: List[str] = []
    for k in sorted(produced):
        spec = META_CONTRACT.get(k)
        if spec is None:
            undeclared.append(f"{k}（值例 {sorted(produced[k])[:3]}）")
            continue
        allowed = spec.get("values")
        if allowed is not None:
            bad = sorted(v for v in produced[k] if v not in allowed)
            if bad:
                violations.append(f"{k}: 产出值 {bad} 不在契约值域 {sorted(allowed)}")

    foreign: List[str] = []
    for k in sorted(consumed):
        if k not in META_CONTRACT:
            foreign.append(f"{k}（被 {sorted(consumed[k])} 读取，契约表未声明）")

    unenforced: List[str] = []
    mismatch: List[str] = []
    for k in sorted(META_CONTRACT):
        spec = META_CONTRACT[k]
        want = spec.get("consumer") or ""
        wants = [want] if isinstance(want, str) else list(want)
        if k in consumed:
            for w in wants:
                if w and w not in consumed[k]:
                    mismatch.append(f"{k}: 契约写消费方={w!r}，"
                                    f"实际读到的是 {sorted(consumed[k])}")
        elif not spec.get("informational"):
            unenforced.append(k)

    stale = [k for k in sorted(META_CONTRACT) if k not in produced]
    return {"produced": {k: sorted(v) for k, v in sorted(produced.items())},
            "consumed": {k: sorted(v) for k, v in sorted(consumed.items())},
            "undeclared": undeclared, "violations": violations, "foreign": foreign,
            "unenforced": unenforced, "mismatch": mismatch, "stale": stale,
            "unreadable": unreadable}


def format_meta_coverage(cov: Dict[str, object]) -> str:
    """把 meta 覆盖对账结果渲染成人可读文本。"""
    lines = ["meta 契约对账（产出 × 消费方 × 契约表）"]
    prod, cons = cov["produced"], cov["consumed"]       # type: ignore[index]
    for k in sorted(set(prod) | set(cons)):             # type: ignore[arg-type]
        spec = META_CONTRACT.get(k, {})
        vals = prod.get(k, [])                          # type: ignore[union-attr]
        cs = cons.get(k, [])                            # type: ignore[union-attr]
        who = ",".join(cs) or ("仅记录" if spec.get("informational") else "**无**")
        val = ",".join(vals[:3]) + ("…" if len(vals) > 3 else "")
        lines.append(f"    {k:<18} 值={val:<26} 消费方={who}")
    for label, key, mark in (
            ("产出未登记", "undeclared", "✗"), ("值域越界", "violations", "✗"),
            ("消费方未声明", "foreign", "✗"), ("声明却无人执行", "unenforced", "✗"),
            ("消费方名不符", "mismatch", "✗"), ("源码不可读", "unreadable", "✗"),
            ("契约冗余", "stale", "·")):
        items = cov.get(key) or []                      # type: ignore[union-attr]
        if items:
            lines.append(f"  {mark} {label} {len(items)} 项：")
            lines += [f"      - {i}" for i in items]
    return "\n".join(lines)


TITLE_BLOCK_SIZE = (180.0, 60.0)


def fit_to_sheet(cd: ChemDrawing, sheet: Optional[str] = None, margin: float = 10.0,
                 center: bool = True, reserve_title_block: bool = True) -> Dict[str, object]:
    """把图面内容**整体平移**到图纸可用区域的中央。

    只平移不缩放：文字高度与图层线宽因此不会被破坏，正交关系也保持。
    这解决的是「图元全挤在左上角、下半张图空着」这类版面问题——
    自动校核查不出来，但评审一眼就能看出来。

    内容超出可用区域时不缩放，只报告 `overflow`，由人决定改规格还是改图幅。
    """
    W, H = SHEET_SIZES.get((sheet or cd.sheet).upper(), SHEET_SIZES["A1"])
    x0, y0 = margin, margin
    x1, y1 = W - margin, H - margin
    bx0, by0, bx1, by1 = cd.content_bbox()
    cw, ch = bx1 - bx0, by1 - by0
    aw, ah = x1 - x0, y1 - y0

    info: Dict[str, object] = {
        "sheet": sheet or cd.sheet, "area": [x0, y0, x1, y1],
        "content_before": [round(bx0, 1), round(by0, 1), round(bx1, 1), round(by1, 1)],
        "content_size": [round(cw, 1), round(ch, 1)],
        "overflow": bool(cw > aw or ch > ah),
    }
    if cw <= 0 or ch <= 0:
        return info

    dx = (x0 + x1) / 2.0 - (bx0 + bx1) / 2.0 if center else x0 - bx0
    dy = (y0 + y1) / 2.0 - (by0 + by1) / 2.0 if center else y1 - by1
    dx, dy = round(dx, 3), round(dy, 3)
    cd.translate(dx, dy)

    nbx0, nby0, nbx1, nby1 = cd.content_bbox()
    info.update({
        "shift": [dx, dy],
        "content_after": [round(v, 1) for v in (nbx0, nby0, nbx1, nby1)],
    })
    # 与标题栏区域是否打架
    tw, th = TITLE_BLOCK_SIZE
    tb = (W - margin - tw, margin, W - margin, margin + th)
    info["overlaps_title_block"] = bool(
        nbx1 > tb[0] and nby1 > tb[1] and nbx0 < tb[2] and nby0 < tb[3]
    ) if reserve_title_block else False
    return info


def layout_enforced(cd: ChemDrawing) -> bool:
    """引擎是否**承诺过**自动排版。

    严重级别跟着承诺走：
      * 手工摆位 → 三类碰撞记 warning（引擎无权替人改坐标，只提示复核）；
      * 自动排版（`layout.mode=auto`）或显式 `enforce` → 记 error
        （既然排版是引擎做的，就没有理由留下碰撞——"承诺过就要兑现"）。
    """
    return bool(cd.meta.get("layout_enforced"))


#: 管线语义层；交叉处理按方向，不再按主次决定断哪条线。
AUX_BREAK_LAYERS: Tuple[str, ...] = ("PIPE_INLINE", "AF_PIPE", "SIGNAL", "仪表线", "仪表")


def _proper_cross(a1: Pt, a2: Pt, b1: Pt, b2: Pt, eps: float = .001) -> Optional[Pt]:
    """两条**正交线段**的"正十字交叉"点；不交 / 端点相接 / 共线重叠都返回 None。

    只认"一横一竖、交点严格落在两条线段内部"——这正是交叉断口要处理的
    那一种：线在路上十字穿过。端点相接是**连接**（不该断），共线重叠是重合走线
    （另有收敛逻辑管，不该在这一步动手）。
    """
    ax1, ay1 = a1; ax2, ay2 = a2
    bx1, by1 = b1; bx2, by2 = b2
    a_h, a_v = abs(ay1 - ay2) <= eps, abs(ax1 - ax2) <= eps
    b_h, b_v = abs(by1 - by2) <= eps, abs(bx1 - bx2) <= eps
    if a_h and b_v:
        x, y = bx1, ay1
        if (min(ax1, ax2) + eps < x < max(ax1, ax2) - eps
                and min(by1, by2) + eps < y < max(by1, by2) - eps):
            return (x, y)
    elif a_v and b_h:
        x, y = ax1, by1
        if (min(bx1, bx2) + eps < x < max(bx1, bx2) - eps
                and min(ay1, ay2) + eps < y < max(ay1, ay2) - eps):
            return (x, y)
    return None


def visible_pipe_crossings(d: ChemDrawing, layers: Sequence[str] =
        ("PIPE_PRI",) + AUX_BREAK_LAYERS) -> List[Tuple[Line, Line, Pt]]:
    """实际可见管线的正十字交叉；明确 pipe@ 接点不属于跨越。"""
    lines = [e for e in d.items if isinstance(e, Line) and resolve_layer(e.layer) in layers]
    junctions = [d.resolve(ref) for rec in d.pipes.values()
                 for ref in (rec.source_ref, rec.target_ref) if '@' in ref]
    hits = []
    for i, a in enumerate(lines):
        for b in lines[i+1:]:
            point = _proper_cross(a.p1, a.p2, b.p1, b.p2)
            if point is not None and not any(math.dist(point, q) < 1e-6 for q in junctions):
                hits.append((a, b, point))
    return hits


def apply_crossing_breaks(d: ChemDrawing, *, gap: float = 4.0,
                          main_sem: str = "PIPE_PRI",
                          aux_sem: Sequence[str] = AUX_BREAK_LAYERS) -> int:
    """按用户截图：横线连续、竖线留断口，包含主物料线之间的交叉。

    只改可见管线 LINE；保留设备、表格、端点及明确 pipe@ 接点。
    PipeRec 的工艺拓扑不变。断口会合并，重复调用不再切割。
    """
    if not math.isfinite(gap) or gap <= 0:
        raise ValueError('交叉断口宽度必须为有限正数')
    cuts: Dict[int, Tuple[Line, List[float]]] = {}
    for rec in d.pipes.values():
        for i, (a1, a2) in enumerate(rec.segments):
            if any(_proper_cross(a1, a2, b1, b2) is not None
                   for b1, b2 in rec.segments[i+1:]):
                raise ValueError(f'流股 {rec.line_no} 自交，须重新寻路')
    crossings = visible_pipe_crossings(d, (main_sem,) + tuple(aux_sem))
    for a, b, point in crossings:
        vertical = a if abs(a.p1[0]-a.p2[0]) < 1e-6 else b
        length = math.dist(vertical.p1, vertical.p2)
        t = math.dist(vertical.p1, point)
        if min(t, length-t) <= gap/2 + .03:
            raise ValueError(f'交叉点 {point} 离竖线端点过近，须重排管线以保留连接')
        cuts.setdefault(id(vertical), (vertical, []))[1].append(t)
    breaks = 0
    for aux, hits in cuts.values():
        a1, a2 = aux.p1, aux.p2
        seg_len = math.hypot(a2[0] - a1[0], a2[1] - a1[1])
        hits = sorted(set(round(t, 4) for t in hits))
        # 沿段把 [t-gap/2, t+gap/2] 挖掉，剩下的是要保留的实线段
        keep: List[Tuple[float, float]] = []
        cur = 0.0
        for t in hits:
            lo, hi = t - gap / 2.0, t + gap / 2.0
            if lo > cur + 1e-6:
                keep.append((cur, min(lo, seg_len)))
            cur = max(cur, hi)
            breaks += 1
        if cur < seg_len - 1e-6:
            keep.append((cur, seg_len))
        if not keep:
            continue
        if len(keep) == 1 and abs(keep[0][0]) < 1e-6 and abs(keep[0][1] - seg_len) < 1e-6:
            continue                                    # 没真正断开
        ux, uy = (a2[0] - a1[0]) / seg_len, (a2[1] - a1[1]) / seg_len

        def at(s: float) -> Pt:
            return (round(a1[0] + ux * s, 4), round(a1[1] + uy * s, 4))

        d.items.remove(aux)
        for s0, s1 in keep:
            if s1 - s0 <= 1e-6:
                continue
            d.line(at(s0), at(s1), aux.layer, color=aux.color,
                   linetype=aux.linetype, note=aux.note)
    d.meta['crossing_breaks'] = int(d.meta.get('crossing_breaks', 0)) + breaks
    d.meta.setdefault('crossing_break_points', []).extend(
        {'point': list(p), 'gap': gap} for _, _, p in crossings)
    return breaks


def break_lines_at(d: ChemDrawing, points: Sequence[Pt], *, gap: float,
                   layers: Sequence[str] = ("PIPE_PRI", "PIPE_INLINE", "AF_PIPE",
                                            "SIGNAL", "仪表线")) -> int:
    """在若干**给定点**处把管线断开：以每个点为中心挖掉 `gap` 宽的实线段。

    用途：**流股编号菱形框**。浙大 PFD 实测——主线正好在菱形**左右顶点**处断开
    （菱形外接 7.07 ↔ 缺口 7.07，线段端点与顶点重合）。所以画了菱形之后，要把
    它盖住的那一段管子挖掉，编号才不被线压。

    判据与 `apply_crossing_breaks` 同源：只动**管线层**；点必须落在某条线段的
    **内部**（两端各留 `eps`）才切——点正好在端点/接头上时不切，避免把接头锯开。
    返回切开的次数。
    """
    if not points:
        return 0
    want = tuple(layers)
    ents = [it for it in d.items if isinstance(it, Line)]
    cuts = 0
    for ln in ents:
        if resolve_layer(ln.layer) not in want:
            continue
        a1, a2 = ln.p1, ln.p2
        seg_len = math.hypot(a2[0] - a1[0], a2[1] - a1[1])
        if seg_len <= gap:
            continue
        ux, uy = (a2[0] - a1[0]) / seg_len, (a2[1] - a1[1]) / seg_len
        # 落在这条线段内部（离两端各留 0.5 mm）的给定点 → 沿线里程
        ts: List[float] = []
        for p in points:
            vx, vy = p[0] - a1[0], p[1] - a1[1]
            t = vx * ux + vy * uy                       # 投影里程
            if t <= 0.5 or t >= seg_len - 0.5:
                continue
            off = abs(vx * (-uy) + vy * ux)             # 到直线的垂距
            if off <= 0.6:                              # 落在线上（容差 0.6mm）
                ts.append(t)
        if not ts:
            continue
        ts = sorted(set(round(t, 4) for t in ts))
        keep: List[Tuple[float, float]] = []
        cur = 0.0
        for t in ts:
            lo, hi = t - gap / 2.0, t + gap / 2.0
            if lo > cur + 1e-6:
                keep.append((cur, min(lo, seg_len)))
            cur = max(cur, hi)
            cuts += 1
        if cur < seg_len - 1e-6:
            keep.append((cur, seg_len))
        if not keep or (len(keep) == 1 and abs(keep[0][0]) < 1e-6
                        and abs(keep[0][1] - seg_len) < 1e-6):
            continue
        try:
            d.items.remove(ln)
        except ValueError:
            pass  # 元素已不在列表 → 幂等移除
        for s0, s1 in keep:
            if s1 - s0 <= 1e-6:
                continue
            d.line((round(a1[0] + ux * s0, 4), round(a1[1] + uy * s0, 4)),
                   (round(a1[0] + ux * s1, 4), round(a1[1] + uy * s1, 4)),
                   ln.layer, color=ln.color, linetype=ln.linetype, note=ln.note)
    return cuts


def preflight(cd: ChemDrawing, loops: Sequence[ControlLoop] = ()) -> Dict[str, object]:
    """出图前总自检，返回结构化报告。"""
    issues: List[Dict[str, str]] = []
    for fn in CHECKS:
        issues.extend(fn(cd))  # type: ignore[operator]
    strict = layout_enforced(cd)
    for fn in COLLISION_CHECKS:
        issues.extend(fn(cd, strict=strict))  # type: ignore[call-arg]
    if loops:
        issues.extend(validate_control_loops(cd, loops))
    issues.extend(cd.meta.get("annotation_issues", []))
    fit=cd.meta.get('fit') or {}
    if strict and fit.get('overflow'):
        issues.append({'level':'error','rule':'sheet_capacity','msg':'流程或设备底部文字超出图框可用区；须扩大幅面或重新排布，不能平移到图框外'})
    errors = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warning"]
    return {
        "stats": cd.stats(),
        "errors": errors,
        "warnings": warns,
        # 多义块消歧留痕：不计入 errors/warnings（默认含义遍地都是，报警会淹掉真问题），
        # 但显式列出「按默认猜的」供人工复核
        "alias_choices": report_alias_choices(cd),
        "ok": not errors,
        "loops": len(loops),
        "layout_guarantee": strict,
    }


def format_report(rep: Dict[str, object]) -> str:
    """把自检报告渲染成人可读文本。"""
    st = rep["stats"]  # type: ignore[index]
    lines = [
        "=" * 68,
        f"图纸自检：{st['name']}  幅面 {st['sheet']}  图元 {st['entities']} 个 / 符号 {st['groups']} 个",
        f"图层({len(st['layers'])})：{', '.join(st['layers'])}",
        f"包围盒：{st['bbox']}    文字：{st['texts']} 条",
    ]
    errs, warns = rep["errors"], rep["warnings"]
    if errs:  # type: ignore[truthy-bool]
        lines.append(f"✗ 错误 {len(errs)} 项：")  # type: ignore[arg-type]
        for e in errs:  # type: ignore[union-attr]
            lines.append(f"    [{e['rule']}] {e['msg']}")
    else:
        lines.append("✓ 无错误：正交布线 / 图层线宽 / 调节阀必注 / 文字高度 / 闭环 全部通过")
    if rep.get("layout_guarantee"):
        lines.append("· 版面：引擎已自动排版，三类版面缺陷（符号重叠/文字压线/管线穿设备）"
                     "按硬约束判定")
    else:
        lines.append("· 版面：手工摆位，三类版面缺陷按提示判定"
                     "（要转硬约束用 layout.mode=auto 或 layout.enforce=true）")
    if warns:  # type: ignore[truthy-bool]
        lines.append(f"⚠ 提示 {len(warns)} 项：")  # type: ignore[arg-type]
        for w in warns:  # type: ignore[union-attr]
            lines.append(f"    [{w['rule']}] {w['msg']}")
    choices = rep.get("alias_choices") or []
    by_def = [c for c in choices if c["by_default"] == "是"]  # type: ignore[index]
    if choices:
        lines.append(f"· 多义块消歧 {len(choices)} 个"
                     f"（其中按默认猜 {len(by_def)} 个，建议复核）：")
        for c in choices:  # type: ignore[union-attr]
            mark = "默认·待复核" if c["by_default"] == "是" else c["reason"]
            lines.append(f"    {c['id']}: {c['block']} → {c['symbol']}  [{mark}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 六、出图后端 A：DXF 书写器
# ---------------------------------------------------------------------------

#: IR 对齐名 → (DXF 组码 72, 组码 73, 是否使用第二对齐点)
_DXF_ALIGN = {
    "left": (0, 0, False),
    "center": (1, 0, True),
    "right": (2, 0, True),
    "middle_center": (1, 2, True),
    "middle_left": (0, 2, True),
    "middle_right": (2, 2, True),
}


def _esc(s: str) -> str:
    """非 ASCII 按 DXF 惯例转义为 \\U+XXXX（与 references/drawing-conventions.md §6 一致）。"""
    out: List[str] = []
    for ch in str(s):
        out.append(ch if ord(ch) < 128 else "\\U+%04X" % ord(ch))
    return "".join(out)


def _rel_pt(p: Pt, base: Pt) -> Tuple[float, float]:
    return (float(p[0]) - base[0], float(p[1]) - base[1])


def _geom_key(g: Group) -> Tuple:
    """符号几何指纹（**相对基点**），用于图块去重：同形符号共用一块。"""

    def r(v: float) -> float:
        return round(float(v), 2)

    parts: List[Tuple] = []
    for e in g.ents:
        if isinstance(e, Line):
            a, b = _rel_pt(e.p1, g.base), _rel_pt(e.p2, g.base)
            parts.append(("L", e.layer, r(a[0]), r(a[1]), r(b[0]), r(b[1])))
        elif isinstance(e, Polyline):
            parts.append(("P", e.layer, e.closed,
                          tuple((r(p[0]), r(p[1])) for p in
                                (_rel_pt(q, g.base) for q in e.pts))))
        elif isinstance(e, Circle):
            c = _rel_pt(e.center, g.base)
            parts.append(("C", e.layer, r(c[0]), r(c[1]), r(e.radius)))
        elif isinstance(e, Arc):
            c = _rel_pt(e.center, g.base)
            parts.append(("A", e.layer, r(c[0]), r(c[1]), r(e.radius),
                          r(e.start_deg), r(e.end_deg)))
        elif isinstance(e, Solid):
            parts.append(("S", e.layer,
                          tuple((r(p[0]), r(p[1])) for p in
                                (_rel_pt(q, g.base) for q in e.pts))))
        elif isinstance(e, Text):
            t = _rel_pt(e.pt, g.base)
            parts.append(("T", e.layer, e.text, r(t[0]), r(t[1]), r(e.height), e.align))
    return tuple(parts) + (("colors",tuple(e.color for e in g.ents)),)


class _R12Builder:
    """零依赖 DXF 书写器（AC1009 / R12）。

    选 R12 的理由：**结构最宽容、任何 CAD 都能读**。代价是 R12 无「线宽(370)」概念——
    粗/中粗/细三档改由**图层颜色**承载（配合打印笔宽表），这恰是 R12 时代的通行做法。
    若需在文件里携带真线宽，请让 ezdxf 可用（自动走 R2000 后端）。
    """

    def __init__(self) -> None:
        self.entities: List[Tuple[int, object]] = []
        self.blocks: List[Tuple[str, List[Tuple[int, object]]]] = []
        self.layers: Dict[str, object] = {}
        self._cur: Optional[List[Tuple[int, object]]] = None
        self._offset: Pt = (0.0, 0.0)

    # -- 写指令 -----------------------------------------------------------
    def _w(self, code: int, val: object) -> None:
        (self._cur if self._cur is not None else self.entities).append((code, val))

    def _p(self, code_base: int, p: Pt) -> None:
        x, y = _rel_pt(p, self._offset)
        self._w(code_base, f"{x:.6f}")
        self._w(code_base + 10, f"{y:.6f}")
        self._w(code_base + 20, "0.0")

    def _head(self, kind: str, layer: str) -> None:
        self._w(0, kind)
        self._w(8, _esc(layer))          # 图层名可能含中文（仪表/仪表线），R12 须转义
        self.layers.setdefault(resolve_layer(layer), layer)

    def open_block(self, name: str, base: Pt) -> None:
        self._cur = []
        self._offset = (float(base[0]), float(base[1]))

    def close_block(self, name: str) -> None:
        assert self._cur is not None
        self.blocks.append((name, self._cur))
        self._cur = None
        self._offset = (0.0, 0.0)

    # -- 图元 -------------------------------------------------------------
    def add_line(self, layer: str, p1: Pt, p2: Pt, **kw) -> None:
        self._head("LINE", layer)
        self._p(10, p1)
        self._p(11, p2)

    def add_circle(self, layer: str, c: Pt, r: float, **kw) -> None:
        self._head("CIRCLE", layer)
        self._p(10, c)
        self._w(40, f"{r:.6f}")

    def add_arc(self, layer: str, c: Pt, r: float, a1: float, a2: float, **kw) -> None:
        self._head("ARC", layer)
        self._p(10, c)
        self._w(40, f"{r:.6f}")
        self._w(50, f"{a1:.4f}")
        self._w(51, f"{a2:.4f}")

    def add_polyline(self, layer: str, pts: Sequence[Pt], closed: bool = False,
                     width: float = 0.0, **kw) -> None:
        self._head("POLYLINE", layer)
        self._w(66, 1)
        self._w(70, 1 if closed else 0)
        self._w(10, "0.0"), self._w(20, "0.0"), self._w(30, "0.0")
        for p in pts:
            self._w(0, "VERTEX")
            self._w(8, _esc(layer))
            self._p(10, p)
        self._w(0, "SEQEND")
        self._w(8, _esc(layer))

    def add_solid(self, layer: str, pts: Sequence[Pt], **kw) -> None:
        q = list(pts)
        if len(q) == 3:
            q = [q[0], q[1], q[2], q[2]]
        self._head("SOLID", layer)
        for i, p in enumerate(q[:4]):
            self._p(10 + i, p)

    def add_text(self, layer: str, text: str, pt: Pt, height: float,
                 align: str = "left", rotation: float = 0.0, **kw) -> None:
        h, v, second = _DXF_ALIGN.get(align, (0, 0, False))
        self._head("TEXT", layer)
        self._w(10, f"{pt[0] - self._offset[0]:.6f}")
        self._w(20, f"{pt[1] - self._offset[1]:.6f}")
        self._w(30, "0.0")
        self._w(40, f"{height:.4f}")
        self._w(1, _esc(text))
        if rotation:
            self._w(50, f"{rotation:.4f}")
        self._w(7, "STANDARD")
        if h:
            self._w(72, h)
        if second:
            self._w(11, f"{pt[0] - self._offset[0]:.6f}")
            self._w(21, f"{pt[1] - self._offset[1]:.6f}")
            self._w(31, "0.0")
        if v:
            self._w(73, v)

    def add_insert(self, layer: str, block: str, at: Pt, **kw) -> None:
        self._head("INSERT", layer)
        self._w(2, _esc(block))
        self._p(10, at)

    # -- 落盘 -------------------------------------------------------------
    def _ltype_defs(self) -> List[Tuple[int, object]]:
        import chemcad_layers as LY
        t: List[Tuple[int, object]] = []
        definitions = [
            ("CONTINUOUS", "Solid line", []),
            ("DASHED", "Dashed __ __ __", [0.5, -0.25]),
            ("CENTER2", "Center ____ _ ____", [1.25, -0.25, 0.25, -0.25]),
        ] + [(name, desc, pattern[1:]) for name, desc, pattern in LY.NEED_LINETYPES]
        for name, desc, pattern in definitions:
            t += [(0, "LTYPE"), (2, name), (70, 0), (3, desc), (72, 65),
                  (73, len(pattern)), (40, float(sum(abs(v) for v in pattern)))]
            for v in pattern:
                t.append((49, v))
        return t

    def to_dxf(self, extents: Tuple[float, float, float, float], all_layers: bool) -> str:
        from chemcad_ir import LAYERS as ALL_LAYERS
        out: List[Tuple[int, object]] = []
        xmin, ymin, xmax, ymax = extents

        # HEADER
        out += [(0, "SECTION"), (2, "HEADER")]
        out += [(9, "$ACADVER"), (1, "AC1009")]
        out += [(9, "$INSBASE"), (10, "0.0"), (20, "0.0"), (30, "0.0")]
        out += [(9, "$EXTMIN"), (10, f"{xmin:.4f}"), (20, f"{ymin:.4f}"), (30, "0.0")]
        out += [(9, "$EXTMAX"), (10, f"{xmax:.4f}"), (20, f"{ymax:.4f}"), (30, "0.0")]
        out += [(9, "$LTSCALE"), (40, "1.0")]
        out += [(0, "ENDSEC")]

        # TABLES
        used = {raw: raw for raw in self.layers.values()}
        import chemcad_layers as LY
        standard_mode = any(name in LY.STD_BY_NAME and name not in ("0", "Defpoints") for name in used)
        if all_layers:
            for ld in (LY.STANDARD if standard_mode else ALL_LAYERS):
                used.setdefault(ld.name, ld.name)
        used.setdefault("0", "0")
        out += [(0, "SECTION"), (2, "TABLES")]
        out += [(0, "TABLE"), (2, "LTYPE"), (70, 3 + len(LY.NEED_LINETYPES))]
        out += self._ltype_defs()
        out += [(0, "ENDTAB")]
        out += [(0, "TABLE"), (2, "LAYER"), (70, len(used))]
        for sem in sorted(used):
            ld = LY.layer_definition(sem)
            color = ld.color if ld else 7
            ltype = ld.linetype if ld else "CONTINUOUS"
            out += [(0, "LAYER"), (2, _esc(sem)), (70, 0), (62, color), (6, ltype)]
        out += [(0, "ENDTAB")]
        out += [(0, "TABLE"), (2, "STYLE"), (70, 1)]
        out += [(0, "STYLE"), (2, "STANDARD"), (70, 0), (40, "0.0"), (41, "1.0"),
                (50, "0.0"), (71, 0), (42, "2.5"), (3, "txt"), (4, "")]
        out += [(0, "ENDTAB")]
        out += [(0, "ENDSEC")]

        # BLOCKS
        out += [(0, "SECTION"), (2, "BLOCKS")]
        for name, tags in self.blocks:
            out += [(0, "BLOCK"), (8, "0"), (2, _esc(name)), (70, 0),
                    (10, "0.0"), (20, "0.0"), (30, "0.0"), (3, _esc(name)), (1, "")]
            out += tags
            out += [(0, "ENDBLK"), (8, "0")]
        out += [(0, "ENDSEC")]

        # ENTITIES
        out += [(0, "SECTION"), (2, "ENTITIES")]
        out += self.entities
        out += [(0, "ENDSEC"), (0, "EOF")]

        return "".join(f"{c}\n{v}\n" for c, v in out)


class _EzBuilder:
    """ezdxf 书写器（AC1015 / R2000）：图层携带**真线宽(370)**，可直接被 AutoCAD 读取。"""

    ALIGN = None  # 延迟绑定 ezdxf 枚举

    def __init__(self, doc, layout) -> None:
        self.doc = doc
        self.layout = layout
        self.target = layout
        self.offset: Pt = (0.0, 0.0)
        self.layers: Dict[str, object] = {}
        self.native_blocks = {}

    def add_native_group(self, group, standard, blocks=True):
        """新版源块按原生实体输出，保留椭圆、样条和填充。"""
        import chemcad_block_library as BL
        import chemcad_layers as LY
        from ezdxf.math import Matrix44
        path, name = str(group.meta["library_file"]), str(group.meta["library_block"])
        _, _, source, _ = BL.source({"block_file": path, "block_name": name})
        key = (path, name, os.stat(path).st_mtime_ns, bool(standard))
        output_name = self.native_blocks.get(key)
        created = output_name is None
        if created:
            import hashlib
            from chemcad_native import import_block,dependencies as native_dependencies
            # 既有技能图框用登记的语义层，但旧资产未附层表；仅在内存补齐这些已知定义。
            closure,missing=native_dependencies(source.doc,name)
            if missing:raise ValueError(missing)
            for native_block in closure:
                for entity in source.doc.blocks[native_block]:
                    semantic=entity.dxf.layer
                    if semantic in source.doc.layers:continue
                    definition=LY.STD_BY_NAME.get(LY.std_name(semantic)) if standard else LY.layer_definition(semantic)
                    if definition is None:raise ValueError(f'原生源块缺少未登记图层：{semantic}')
                    if definition.linetype not in source.doc.linetypes:
                        row=next((r for r in LY.PROFILE['linetypes'] if r['name'].upper()==definition.linetype.upper()),None)
                        if row is None:raise ValueError(f'语义源层缺少线型：{definition.linetype}')
                        source.doc.linetypes.add(row['name'],pattern=row['pattern'],description=row['description'])
                    source.doc.layers.new(semantic,dxfattribs={'color':definition.color,'linetype':definition.linetype,
                        'lineweight':definition.lineweight,'plot':int(getattr(definition,'plot',True)),'flags':getattr(definition,'flags',0)})
            before_layers=set(self.doc.layers.entries)
            prefix='SRC_'+hashlib.sha256((path+'|'+name).encode('utf-8')).hexdigest()[:12]
            output_name,dependencies=import_block(source.doc,self.doc,name,prefix)
            if name not in self.doc.blocks:
                previous=output_name
                self.doc.blocks.rename_block(previous,name)
                for imported in dependencies.values():
                    for entity in self.doc.blocks[name if imported==previous else imported]:
                        if entity.dxftype()=='INSERT' and entity.dxf.name==previous:entity.dxf.name=name
                dependencies[name]=name;output_name=name
            target=self.doc.blocks[output_name]
            cx, cy = group.meta["source_center"]
            for entity in target:entity.transform(Matrix44.translate(-cx,-cy,0))
            # 只改输出的语义层；归档资产仍完整保留原图层、线宽和依赖。
            for old,new in dependencies.items():
                for clone in self.doc.blocks[new]:
                    layer=clone.dxf.layer.split('$',2)[-1]
                    clone.dxf.layer=BL.geometry_layer(layer)
                    if standard:
                        clone.dxf.layer=LY.std_name(clone.dxf.layer)
                        clone.dxf.color=256;clone.dxf.lineweight=-1;clone.dxf.linetype='BYLAYER'
            if standard:
                used={e.dxf.layer.casefold() for b in self.doc.blocks for e in b}
                for layer in list(self.doc.layers):
                    if layer.dxf.name.casefold() not in before_layers and layer.dxf.name.casefold() not in used:
                        self.doc.layers.remove(layer.dxf.name)
            self.native_blocks[key] = output_name
        insert = self.layout.add_blockref(output_name, group.base,
                    dxfattribs={"rotation": float(group.meta.get("rotation", 0))})
        if not blocks:
            insert.explode()
        return int(created), len(source)

    def _dxf(self, layer: str) -> Dict[str, object]:
        self.layers.setdefault(resolve_layer(layer), layer)
        return {"layer": layer}

    def open_block(self, name: str, base: Pt) -> None:
        self.target = self.doc.blocks.new(name=name)
        self.offset = (float(base[0]), float(base[1]))

    def close_block(self, name: str) -> None:
        self.target = self.layout
        self.offset = (0.0, 0.0)

    def _pt(self, p: Pt) -> Tuple[float, float, float]:
        return (p[0] - self.offset[0], p[1] - self.offset[1], 0.0)

    def add_line(self, layer: str, p1: Pt, p2: Pt, **kw) -> None:
        self.target.add_line(self._pt(p1), self._pt(p2), dxfattribs=self._dxf(layer))

    def add_circle(self, layer: str, c: Pt, r: float, **kw) -> None:
        self.target.add_circle(self._pt(c), r, dxfattribs=self._dxf(layer))

    def add_arc(self, layer: str, c: Pt, r: float, a1: float, a2: float, **kw) -> None:
        self.target.add_arc(self._pt(c), r, a1, a2, dxfattribs=self._dxf(layer))

    def add_polyline(self, layer: str, pts: Sequence[Pt], closed: bool = False,
                     width: float = 0.0, **kw) -> None:
        self.target.add_lwpolyline([self._pt(p) for p in pts], close=closed,
                                   dxfattribs=self._dxf(layer))

    def add_solid(self, layer: str, pts: Sequence[Pt], **kw) -> None:
        q = list(pts)
        if len(q) == 3:
            q = [q[0], q[1], q[2], q[2]]
        self.target.add_solid([self._pt(p) for p in q], dxfattribs=self._dxf(layer))

    def add_text(self, layer: str, text: str, pt: Pt, height: float,
                 align: str = "left", rotation: float = 0.0, **kw) -> None:
        import ezdxf.enums as ez_enum
        if _EzBuilder.ALIGN is None:
            _EzBuilder.ALIGN = {
                "left": ez_enum.TextEntityAlignment.LEFT,
                "center": ez_enum.TextEntityAlignment.CENTER,
                "right": ez_enum.TextEntityAlignment.RIGHT,
                "middle_center": ez_enum.TextEntityAlignment.MIDDLE_CENTER,
                "middle_left": ez_enum.TextEntityAlignment.MIDDLE_LEFT,
                "middle_right": ez_enum.TextEntityAlignment.MIDDLE_RIGHT,
            }
        t = self.target.add_text(text, height=height,
                                 dxfattribs=dict(self._dxf(layer), rotation=rotation))
        t.set_placement(self._pt(pt), align=_EzBuilder.ALIGN.get(  # type: ignore[arg-type]
            align, ez_enum.TextEntityAlignment.LEFT))

    def add_insert(self, layer: str, block: str, at: Pt, **kw) -> None:
        self.target.add_blockref(block, insert=self._pt(at), dxfattribs=self._dxf(layer))


def _emit(d: ChemDrawing, b, blocks: bool, all_layers: bool = False) -> Dict[str, int]:
    """把 IR 统一灌进某个 builder。blocks=True 时符号落为图块 + INSERT。"""
    blocks_created: Dict[Tuple, str] = {}
    names_used: Dict[str, int] = {}
    n_blocks = n_ents = 0

    def emit_ent(e: Ent) -> None:
        nonlocal n_ents
        n_ents += 1
        if isinstance(e, Line):
            b.add_line(e.layer, e.p1, e.p2)
        elif isinstance(e, Polyline):
            b.add_polyline(e.layer, e.pts, e.closed, e.width)
        elif isinstance(e, Circle):
            b.add_circle(e.layer, e.center, e.radius)
        elif isinstance(e, Arc):
            b.add_arc(e.layer, e.center, e.radius, e.start_deg, e.end_deg)
        elif isinstance(e, Solid):
            b.add_solid(e.layer, e.pts)
        elif isinstance(e, Text):
            b.add_text(e.layer, e.text, e.pt, e.height, e.align, e.rotation)
        if isinstance(b, _EzBuilder) and e.color is not None:
            b.target[-1].dxf.color = int(e.color)

    for item in d.items:
        if isinstance(item, Group) and item.meta.get("native_geometry"):
            if not isinstance(b, _EzBuilder):
                raise ValueError("新版原生设备块需使用 ezdxf 的 DXF 或默认 DWG 通道，不能用 R12 简化曲线/填充")
            added, count = b.add_native_group(item, bool(d.meta.get("layer_standard")), blocks)
            n_blocks += added
            n_ents += count
        elif isinstance(item, Group) and blocks:
            key = (item.name, _geom_key(item))
            name = blocks_created.get(key)
            if name is None:
                base_name = item.name
                n = names_used.get(base_name, 0)
                name = base_name if n == 0 else f"{base_name}_{n + 1}"
                while isinstance(b, _EzBuilder) and name in b.doc.blocks:
                    n += 1
                    name = f"{base_name}_{n + 1}"
                names_used[base_name] = n + 1
                blocks_created[key] = name
                b.open_block(name, item.base)
                for e in item.ents:
                    emit_ent(e)
                b.close_block(name)
                n_blocks += 1
            b.add_insert("0", name, item.base)
        elif isinstance(item, Group):
            for e in item.ents:
                emit_ent(e)
        else:
            emit_ent(item)
    return {"blocks": n_blocks, "entities": n_ents}


def write_dxf(d: ChemDrawing, path: str, prefer_ezdxf: bool = True,
              blocks: bool = True, all_layers: bool = False,
              dxf_version: str = "R2010") -> Dict[str, object]:
    """把图纸写成 DXF 文件。

    优先 ezdxf（默认 R2010：**DXF 为 UTF-8，中文图层名/文字不经转义**，且图层带真线宽 370）；
    不可用时退回零依赖的 R12 书写器（非 ASCII 按 \\U+XXXX 转义，符合 R12 惯例，
    且本技能自带的 inspect_dxf.py / check_dxf.py 能自动还原）。

    `dxf_version` 可指定 R2000/R2004/R2007/R2010/R2013/R2018；**R2007 以下中文会退化为
    \\U+XXXX 转义**（AutoCAD 能读，但第三方工具往返易失真），仅在需要兼容老版本时使用。
    """
    used = d.used_layers()
    all_layers = all_layers or bool(d.meta.get("layer_standard"))
    extents = d.bbox()
    info: Dict[str, object] = {"path": os.path.abspath(path), "layers": used}

    if prefer_ezdxf:
        try:
            import ezdxf
        except ImportError:
            info["note"] = "未安装 ezdxf，已退回零依赖 R12 书写器（线宽由图层颜色承载）"
        else:
            doc = ezdxf.new(dxf_version, setup=True)
            doc.header["$INSUNITS"] = 4          # 毫米
            doc.header["$LTSCALE"] = 1.0
            # 图层表：先认**出图图层标准**（chemcad_layers.STANDARD），
            # 再退回本技能的语义注册表。标准优先，因为"输出必须按标准设置"
            # 是硬要求：名字换了、颜色/线型/线宽也得跟着换，
            # 否则会出现"层名写着粗实线、实际线宽仍是旧的 0.25"这种假合规。
            try:
                import chemcad_layers as _LY
                _std = _LY.STD_BY_NAME
                # 母图 HIDDEN 使用实际段长定义，避免同名线型的参数不一致。
                # 不补就会**静默退化成 Continuous**；补了才谈得上"按标准设置"。
                _added = _LY.ensure_linetypes(doc)
                if _added:
                    info["linetypes_added"] = _added
            except Exception:                                 # noqa: BLE001
                if d.meta.get("layer_standard"):
                    raise
                _std = {}
            for sem in used + ["0"]:
                if sem in doc.layers:
                    continue
                s = _std.get(str(sem))
                if s is not None:
                    kwargs: Dict[str, object] = {"color": s.color}
                    kwargs["lineweight"] = s.lineweight
                    kwargs["plot"] = int(s.plot)
                    kwargs["flags"] = s.flags
                    if s.linetype in doc.linetypes:
                        kwargs["linetype"] = s.linetype
                    doc.layers.new(sem, dxfattribs=kwargs)
                    continue
                ld = LAYER_REGISTRY.get(resolve_layer(sem))
                if ld is None:
                    doc.layers.add(sem, color=7)
                    continue
                kwargs = {"color": ld.color, "lineweight": ld.lineweight}
                if ld.linetype in doc.linetypes:
                    kwargs["linetype"] = ld.linetype
                doc.layers.add(sem, **kwargs)
            b = _EzBuilder(doc, doc.modelspace())
            stats = _emit(d, b, blocks, all_layers)
            if d.meta.get('layer_standard'):
                # 原生块落笔后再落实32层线宽，不依赖--all-layers。
                info['layer_settings'] = _LY.apply_dxf_layer_settings(doc)
            # 三份样本均使用 gbenor/gbcbig；统一生成注记，跳过原生设备块定义。
            for name, height in (("3-gbcbig", 3.0), ("5-gbcbig", 5.0), ("7-GBCBIG", 7.0)):
                style = doc.styles.get(name) if name in doc.styles else doc.styles.add(name, font="gbenor.shx")
                style.dxf.font, style.dxf.bigfont = "gbenor.shx", "gbcbig.shx"
                style.dxf.width = 0.7
                style.dxf.height = 0.0  # 可变字高：实体的 2.5/3/5/7mm 才是实际字号
                style.dxf.last_height = height
            native_names = set(b.native_blocks.values())
            for layout in doc.blocks:
                if layout.name in native_names:
                    continue
                for entity in layout:
                    kind = entity.dxftype()
                    if kind not in ("TEXT", "MTEXT"):
                        continue
                    hh = float(entity.dxf.height if kind == "TEXT" else entity.dxf.char_height)
                    entity.dxf.style = "7-GBCBIG" if hh >= 6.5 else "3-gbcbig" if hh <= 3.5 else "5-gbcbig"
                    if kind == "TEXT":
                        entity.dxf.width = 0.7
            doc.saveas(path)
            info.update({"backend": f"ezdxf/{dxf_version}", "lineweight_in_file": True, **stats})
            if int(dxf_version.upper().lstrip("R")) < 2007:
                info["note"] = (f"{dxf_version} 早于 R2007：非 ASCII 已按 \\U+XXXX 转义，"
                                f"AutoCAD 可读，但第三方工具往返建议改用 R2010")
            return info

    b = _R12Builder()
    stats = _emit(d, b, blocks, all_layers)
    with open(path, "w", encoding="ascii", errors="strict", newline="\r\n") as f:
        f.write(b.to_dxf(extents, all_layers))
    info.update({"backend": "builtin/R12", "lineweight_in_file": False, **stats})
    return info


# ---------------------------------------------------------------------------
# 七、出图后端 B：AutoCAD COM（承自 pyautocad 技能的生产模式）
# ---------------------------------------------------------------------------

#: COM「被调用方忙」类错误的 HRESULT（与 pyautocad 技能 `com_stability_and_errors.md` 一致）
COM_BUSY_HRESULTS = frozenset({
    -2147418111,   # 0x80010001 RPC_E_CALL_REJECTED  —— AutoCAD 正在处理命令
    -2147417846,   # 0x8001010A RPC_E_SERVERCALL_RETRYLATER
    -2147417848,   # 0x80010108 RPC_E_DISCONNECTED
    -2147418105,   # 0x80010007 RPC_E_CALL_CANCELED
    -2147417851,   # 0x80010105 RPC_E_SERVERFAULT
})

#: 消息层面的兜底特征（拿不到 HRESULT 时用）
_COM_BUSY_HINTS = (
    "rejected by callee", "call was rejected", "retry later", "server busy",
    "message filter", "被调用方拒绝", "0x80010001", "0x8001010a",
)


def is_com_busy(err: BaseException) -> bool:
    """判断异常是否为「AutoCAD 忙」——只有这类才值得重试。

    **这是本技能相对早期实现的关键修正**：早期 `com_retry` 捕获 `Exception`
    并重试 8~10 次，会把**我们自己的代码 bug**（TypeError、KeyError…）也当成
    COM 偶发故障反复重试，既掩盖真实错误、又把耗时放大十倍。现在只重试
    COM 忙类错误，其余立即抛出。
    """
    hr = getattr(err, "hresult", None)
    if hr is None and getattr(err, "args", None):
        first = err.args[0]
        if isinstance(first, int):
            hr = first
    if hr is not None and hr in COM_BUSY_HRESULTS:
        return True
    msg = str(err).lower()
    return any(h in msg for h in _COM_BUSY_HINTS)


def com_retry(max_attempts: int = 10, delay: float = 0.5, backoff: float = 1.2):
    """COM 重试装饰器：**只对「AutoCAD 忙」重试**（`is_com_busy`）。

    与 pyautocad 技能的 `com_retry` 同构；内建一份以保证本技能零外部依赖可用。
    `retry_all=True` 可恢复旧的「无差别重试」行为——仅在调用方明确知道
    这条后端只会抛 COM 异常时才用。
    """
    import time

    def deco(fn):
        def wrapper(*a, **kw):
            retry_all = bool(kw.pop("_retry_all", False))
            last: Optional[BaseException] = None
            d = delay
            for attempt in range(max_attempts):
                try:
                    return fn(*a, **kw)
                except Exception as err:  # noqa: BLE001 —— COM 异常类型由 win32com 决定
                    if not retry_all and not is_com_busy(err):
                        raise                 # 不是「忙」——立即冒泡，不掩盖真 bug
                    last, d = err, d * backoff
                    if attempt < max_attempts - 1:
                        time.sleep(min(d, 3.0))
            assert last is not None
            raise last
        return wrapper
    return deco


class _MinimalCom:
    """最小 COM 客户端（无 pyautocad 时的自备实现）。仅覆盖本引擎所需操作。"""

    def __init__(self, create_if_not_exists: bool = False, visible: bool = True) -> None:
        try:
            from pyautocad import APoint, Autocad, aDouble  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "驱动 AutoCAD 需要 pyautocad + pywin32：pip install pyautocad pywin32"
            ) from e
        self._APoint = APoint
        self._aDouble = aDouble
        self.acad = Autocad(create_if_not_exists=create_if_not_exists, visible=visible)

    @property
    def doc(self):
        return self.acad.doc

    @property
    def model(self):
        return self.acad.model

    @com_retry(max_attempts=8, delay=0.5)
    def ensure_layer(self, name: str, color: int = 7, linetype: str = "CONTINUOUS"):
        layers = self.doc.Layers
        for lay in layers:
            if str(lay.Name).lower() == name.lower():
                lay.color = int(color)
                return lay
        new = layers.Add(name)
        new.color = int(color)
        try:
            new.Linetype = linetype
        except Exception:
            pass  # 图层线型设置失败 → 用默认线型，图仍可出
        return new

    @com_retry(max_attempts=8, delay=0.5)
    def blocks(self):
        return self.doc.Blocks

    @com_retry(max_attempts=8, delay=0.5)
    def add_block(self, name: str, base):
        return self.doc.Blocks.Add(self._APoint(*base, 0.0), name)

    @com_retry(max_attempts=5, delay=0.3)
    def insert_block(self, space, name: str, at):
        return space.InsertBlock(self._APoint(at[0], at[1], 0.0), name, 1.0, 1.0, 1.0, 0.0)

    @com_retry(max_attempts=5, delay=0.3)
    def add_line(self, space, p1, p2, layer: str):
        e = space.AddLine(self._APoint(p1[0], p1[1], 0.0), self._APoint(p2[0], p2[1], 0.0))
        e.Layer = layer
        return e

    @com_retry(max_attempts=5, delay=0.3)
    def add_polyline(self, space, pts, closed: bool, layer: str):
        flat: List[float] = []
        for p in pts:
            flat += [float(p[0]), float(p[1])]
        e = space.AddLightWeightPolyline(self._aDouble(*flat))
        if closed:
            e.Closed = True
        e.Layer = layer
        return e

    @com_retry(max_attempts=5, delay=0.3)
    def add_circle(self, space, c, r: float, layer: str):
        e = space.AddCircle(self._APoint(c[0], c[1], 0.0), float(r))
        e.Layer = layer
        return e

    @com_retry(max_attempts=5, delay=0.3)
    def add_arc(self, space, c, r: float, a1: float, a2: float, layer: str):
        # COM 用弧度，IR 用度
        e = space.AddArc(self._APoint(c[0], c[1], 0.0), float(r),
                         math.radians(a1), math.radians(a2))
        e.Layer = layer
        return e

    @com_retry(max_attempts=5, delay=0.3)
    def add_solid(self, space, pts, layer: str):
        q = list(pts)
        if len(q) == 3:
            q = [q[0], q[1], q[2], q[2]]
        p = [self._APoint(x[0], x[1], 0.0) for x in q[:4]]
        e = space.AddSolid(p[0], p[1], p[2], p[3])
        e.Layer = layer
        return e

    @com_retry(max_attempts=5, delay=0.3)
    def add_text(self, space, text: str, pt, height: float, align: str, rotation: float, layer: str):
        txt = space.AddText(str(text), self._APoint(pt[0], pt[1], 0.0), float(height))
        txt.Layer = layer
        if rotation:
            txt.Rotation = math.radians(rotation)
        # 关键坑（见 pyautocad 技能）：设了 Alignment 必须同时写 TextAlignmentPoint，
        # 否则文字掉到 (0,0,0)。
        code = {"left": 0, "center": 1, "right": 2,
                "middle_left": 9, "middle_center": 10, "middle_right": 11}.get(align, 0)
        if code != 0:
            txt.Alignment = code
            txt.TextAlignmentPoint = self._APoint(pt[0], pt[1], 0.0)
        return txt

    @com_retry(max_attempts=6, delay=0.5)
    def regen(self):
        self.doc.Regen(0)

    @com_retry(max_attempts=6, delay=0.5)
    def start_undo(self):
        self.doc.StartUndoMark()

    @com_retry(max_attempts=6, delay=0.5)
    def end_undo(self):
        self.doc.EndUndoMark()

    # -- 多文档（吸收 pyautocad 技能的 CADClient 多文档管理）-----------------
    @com_retry(max_attempts=5, delay=0.4)
    def document_name(self) -> str:
        return str(getattr(self.doc, "Name", "<未命名>"))

    @com_retry(max_attempts=5, delay=0.4)
    def list_documents(self) -> List[Tuple[str, str]]:
        """列出全部已打开图纸：[(Name, FullName)]。

        一个 AutoCAD 里同时开着主体平面图、计算导出图、备份图是常态
        （pyautocad 技能 `04_multi_doc_management.py` 的场景）；
        **写图前必须确认当前图纸是哪一张**，否则会画到错的图上。
        """
        out: List[Tuple[str, str]] = []
        for d in self.acad.app.Documents:
            name = str(getattr(d, "Name", ""))
            path = ""
            for attr in ("FullName", "Path"):
                try:
                    path = str(getattr(d, attr, "") or "")
                except Exception:
                    path = ""
                if path:
                    break
            out.append((name, path))
        return out

    @com_retry(max_attempts=5, delay=0.4)
    def _doc_by_name(self, name: str):
        want = str(name).lower()
        for d in self.acad.app.Documents:
            if str(getattr(d, "Name", "")).lower() == want:
                return d
        return None

    @com_retry(max_attempts=5, delay=0.4)
    def activate_document(self, doc) -> str:
        """把指定文档设为活动文档。返回其名称。"""
        self.acad.app.ActiveDocument = doc
        return str(getattr(doc, "Name", ""))

    def find_document(self, keyword: str):
        """按关键字（图纸名或路径）查文档并激活；找不到返回 None。"""
        kw = str(keyword).lower()
        for name, path in self.list_documents():
            if kw in name.lower() or kw in (path or "").lower():
                doc = self._doc_by_name(name)
                if doc is not None:
                    self.activate_document(doc)
                return str(name)
        return None

    @com_retry(max_attempts=5, delay=0.4)
    def save_as(self, path: str) -> str:
        self.doc.SaveAs(str(path))
        return str(path)

    @com_retry(max_attempts=5, delay=0.4)
    def zoom_extents(self):
        self.acad.app.ZoomExtents()

    # -- 查询与清理（**先收集、后删除**：边遍历边删会打坏 COM 枚举器）-------
    def iter_entities(self, fast: bool = True):
        """遍历模型空间实体。

        `fast=True` 走 pyautocad 的 `iter_objects_fast()`——它按类型批量取，
        比逐实体做 COM 动态接口转换快一个数量级（10 万实体的图差异极大）。
        """
        if fast and hasattr(self.acad, "iter_objects_fast"):
            try:
                for e in self.acad.iter_objects_fast():
                    yield e
                return
            except Exception:
                pass  # 快速迭代不可用 → 回退逐实体迭代（降级，不是吞）
        for e in self.model:
            yield e

    def entities_on_layer(self, layer: str) -> List[object]:
        """取某图层上的全部实体（**只收集，不删除**）。

        优先用选择集按 DXF 组码 8 过滤（快）；选择集不可用时退回逐实体扫描。
        """
        got = self.select_by_layer(layer)
        if got is not None:
            return got
        out: List[object] = []
        for e in self.iter_entities():
            try:
                if str(e.Layer) == layer:
                    out.append(e)
            except Exception:
                continue  # 单个实体属性读取失败（COM 偶发）→ 跳过该实体，不中断整图
        return out

    def select_by_layer(self, layer: str, ss_name: str = "CHEMCAD_SEL") -> Optional[List[object]]:
        """用选择集 + DXF 组码过滤取实体；不可用时返回 None（由调用方回退扫描）。

        组码 8 = 图层名。这正是 pyautocad 技能 `dxf_codes_and_filters.md`
        推荐的快速筛选方式，避免把整张图的实体都拉进 Python。
        """
        try:
            sets = self.doc.SelectionSets
        except Exception:
            return None
        ss = None
        try:
            try:
                ss = sets.Item(ss_name)
                ss.Clear()
            except Exception:
                ss = sets.Add(ss_name)
            ss.Select(5, None, None, [8], [str(layer)])   # 5 = acSelectionSetAll
            return [e for e in ss]
        except Exception:
            if ss is not None:
                try:
                    ss.Delete()
                except Exception:
                    pass  # 选择集清理失败 → 忽略（已决定回退逐实体扫描）
            return None

    def count_on_layer(self, layer: str) -> int:
        return len(self.entities_on_layer(layer))

    @com_retry(max_attempts=4, delay=0.4)
    def clear_layer(self, layer: str) -> int:
        """删除某图层上的全部实体，返回删除条数。

        ⚠️ 早期版本的 `ComBackend.draw(clear_layers=True)` 会调用本方法，但
        `_MinimalCom` 里**根本没有这个方法**——`hasattr` 判定为假后静默跳过，
        于是「清层重画」变成了「在旧图元上叠加再画一遍」。这是一个静默空转缺陷，
        现补齐实现；`selftest.py [7]` 钉住它不再回归。
        """
        doomed = self.entities_on_layer(layer)
        n = 0
        for e in doomed:
            try:
                e.Delete()
                n += 1
            except Exception:
                pass  # 单个实体删除失败 → 继续删其余（返回计数已反映实际删除数）
        return n

    def purge_empty_layers(self, keep: Iterable[str] = ("0", "Defpoints")) -> List[str]:
        """清除**空图层**（无实体的图层）。返回被删除的图层名。

        与 AutoCAD 的 PURGE 同义：只删空层，绝不删有图元的层。
        """
        keep_set = {str(k) for k in keep}
        kept_used = set()
        for e in self.iter_entities():
            try:
                kept_used.add(str(e.Layer))
            except Exception:
                continue  # 单个实体图层读取失败 → 跳过，不误判为「未使用」
        removed: List[str] = []
        for lay in list(self.doc.Layers):
            try:
                nm = str(lay.Name)
            except Exception:
                continue  # 图层名读取失败 → 保留该层（保守，不删）
            if nm in keep_set or nm in kept_used:
                continue
            try:
                lay.Delete()
                removed.append(nm)
            except Exception:
                pass  # 删除空图层失败 → 跳过该层，继续其余
        return removed

    @staticmethod
    def effective_block_name(blk) -> str:
        """取块名。**动态块**的 `Name` 是匿名名（`*U123`），必须用 `EffectiveName`。"""
        return str(getattr(blk, "EffectiveName", None) or getattr(blk, "Name", ""))

    @com_retry(max_attempts=5, delay=0.4)
    def layer_table(self) -> List[Dict[str, object]]:
        """读图层表：[{name, color, linetype, lineweight, on, frozen}]。

        `lineweight` 单位 1/100 mm（-3 = 默认，-1 = ByLayer…），与 DXF 组码 370 同口径，
        因此可以直接与 `chemcad_ir.LW_RANGES` 对照做线宽校核。
        """
        out: List[Dict[str, object]] = []
        for lay in self.doc.Layers:
            rec: Dict[str, object] = {"name": str(getattr(lay, "Name", ""))}
            for attr, key in (("color", "color"), ("Linetype", "linetype"),
                              ("Lineweight", "lineweight"), ("LayerOn", "on"),
                              ("Freeze", "frozen")):
                try:
                    rec[key] = getattr(lay, attr, None)
                except Exception:
                    rec[key] = None
            # 个别版本用 IsFrozen / Freeze 命名不同，做一次兜底
            if rec.get("frozen") is None:
                try:
                    rec["frozen"] = bool(getattr(lay, "IsFrozen", False))
                except Exception:
                    pass  # 该 CAD 版本无 IsFrozen → 冻结态记为未知
            out.append(rec)
        return out


# ---------------------------------------------------------------------------
# 七之二、文本/图元的读回工具（供 COM 读图与 DXF 复核共用）
# ---------------------------------------------------------------------------

#: MText 内嵌格式串：`\fSimSun|b0|i0;` `\C3;` `\H2.5x;` `\S1/2;` `\A1;` …
_MTEXT_CTRL_RE = re.compile(r"\\[A-Za-z][^;\\]*;")
#: R12 的非 ASCII 转义 `\U+XXXX`（本技能 R12 书写器与许多老 DXF 都用）
_UNI_ESC_RE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def unformat_text(s: str) -> str:
    """剥离 MText/Text 的内嵌格式串，得到可读文本。

    AutoCAD 的 MText 里常带 `\\P`（换行）、`\\fSimSun|b0;`（字体）、`\\C3;`（颜色）、
    `\\H2.5x;`（字高）等控制串；直接把 MText 原文当位号用，会得到一串乱码。
    pyautocad 技能的 `CADClient.extract_texts()` 干了这件事，本技能内建一份，
    保证「零外部依赖也能读图」。
    """
    if s is None:
        return ""
    t = str(s)
    t = t.replace("\\P", "\n").replace("\\p", "\n")
    t = _MTEXT_CTRL_RE.sub("", t)
    t = t.replace("\\~", " ")
    t = t.replace("%%d", "°").replace("%%D", "°")
    t = t.replace("%%c", "Ø").replace("%%C", "Ø")
    t = t.replace("%%p", "±").replace("%%P", "±")
    t = t.replace("{", "").replace("}", "")
    t = _UNI_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), t)
    return t.replace("\r", "").strip()


def _pt3(v) -> Optional[Pt]:
    """把 COM 返回的点（VARIANT 数组 / APoint / 元组）归一成 (x, y)。"""
    try:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return (float(v[0]), float(v[1])) if len(v) >= 2 else None
        # 有些 COM 返回的是索引访问的安全数组
        return (float(v[0]), float(v[1]))
    except Exception:
        return None


class ChemComReader:
    """从**运行中的 AutoCAD 图纸**读回实体，产出结构化报告。

    这是「校核腿」的 COM 通道：此前校核只能对**磁盘上的 DXF 文件**做
    （`inspect_dxf.py` / `check_dxf.py`），若用户手里的成果还开着、尚未存盘，
    就没法核。本类补齐这条路。

    产出与 `inspect_dxf.py` **同构**的字典，便于下游共用同一套统计与判定逻辑：

        {"document": 名字, "path": 路径,
         "layers": {层名: 实体数}, "blocks": {块名: 引用次数},
         "entities": {类型: 条数}, "texts": [{"text","raw","layer","ok":.., "point","height"}],
         "polylines": [{"layer","points","count"}],
         "inserts": [{"name","layer","point"}], "bbox": [x0,y0,x1,y1]}
    """

    #: 用于估算包围盒的实体类型（只取最常见的三类，够用且快）
    _POINT_KEYS = ("InsertionPoint", "Center", "StartPoint", "EndPoint", "Coordinates")

    def __init__(self, client=None, create_if_not_exists: bool = False,
                 visible: bool = True) -> None:
        self.client = client or _MinimalCom(create_if_not_exists=create_if_not_exists,
                                            visible=visible)
        self._external = client is not None

    # -- 文档 ---------------------------------------------------------------
    def connect(self) -> str:
        try:
            return str(getattr(self.client.doc, "Name", "<未命名图纸>"))
        except Exception as e:
            raise ConnectionError(
                "无法连接到 AutoCAD（未运行、无打开图纸，或被对话框阻塞）。\n"
                "  处理：① 先打开 AutoCAD 并载入要核的图纸；或 ② 加 --create 自动启动；"
                "或 ③ 改用 inspect_dxf.py / check_dxf.py 核磁盘上的 DXF 文件。\n"
                f"  原始错误：{type(e).__name__}: {e}"
            ) from e

    def list_documents(self) -> List[Tuple[str, str]]:
        if hasattr(self.client, "list_documents"):
            return self.client.list_documents()  # type: ignore[attr-defined]
        return []

    def activate(self, keyword: str) -> Optional[str]:
        if hasattr(self.client, "find_document"):
            return self.client.find_document(keyword)  # type: ignore[attr-defined]
        return None

    # -- 读图 ---------------------------------------------------------------
    def _iter(self, layer: Optional[str] = None):
        """取实体迭代器；兼容内建 `_MinimalCom` 与 pyautocad 技能的 `CADClient`。"""
        c = self.client
        if layer and hasattr(c, "entities_on_layer"):
            return iter(c.entities_on_layer(layer))
        if hasattr(c, "iter_entities"):
            return iter(c.iter_entities())
        if hasattr(c, "iter_objects_fast"):
            try:
                return iter(c.iter_objects_fast())
            except Exception:
                pass  # 快速迭代不可用 → 回退 ModelSpace（降级链）
        try:
            return iter(c.doc.ModelSpace)
        except Exception:
            return iter(())

    def read(self, layer: Optional[str] = None, max_texts: int = 20000) -> Dict[str, object]:
        """读回实体，产出结构化报告。`layer` 非空时只读该图层。"""
        name = self.connect()
        path = ""
        try:
            path = str(getattr(self.client.doc, "FullName", "") or "")
        except Exception:
            path = ""

        rep: Dict[str, object] = {
            "document": name, "path": path,
            "layers": {}, "blocks": {}, "entities": {},
            "texts": [], "polylines": [], "inserts": [],
            "bbox": None, "truncated": False,
        }
        layers: Dict[str, int] = rep["layers"]  # type: ignore[assignment]
        blocks: Dict[str, int] = rep["blocks"]  # type: ignore[assignment]
        kinds: Dict[str, int] = rep["entities"]  # type: ignore[assignment]
        texts: List[Dict[str, object]] = rep["texts"]  # type: ignore[assignment]
        polys: List[Dict[str, object]] = rep["polylines"]  # type: ignore[assignment]
        inserts: List[Dict[str, object]] = rep["inserts"]  # type: ignore[assignment]

        xs: List[float] = []
        ys: List[float] = []
        it = self._iter(layer)

        for e in it:
            try:
                lay = str(getattr(e, "Layer", ""))
                typ = str(getattr(e, "EntityName", "") or type(e).__name__)
            except Exception:
                continue  # 单个实体属性读取失败（COM 偶发）→ 跳过该实体
            if layer and lay != layer:
                continue
            layers[lay] = layers.get(lay, 0) + 1
            kinds[typ] = kinds.get(typ, 0) + 1

            if typ in ("AcDbText", "AcDbMText") or hasattr(e, "TextString"):
                if len(texts) < max_texts:
                    raw = str(getattr(e, "TextString", "") or "")
                    p = _pt3(getattr(e, "InsertionPoint", None))
                    try:
                        h = float(getattr(e, "Height", 0.0) or 0.0)
                    except Exception:
                        h = 0.0
                    texts.append({"text": unformat_text(raw), "raw": raw,
                                  "layer": lay, "point": p, "height": round(h, 3)})
                    if p:
                        xs.append(p[0]); ys.append(p[1])
                else:
                    rep["truncated"] = True
            elif typ in ("AcDbPolyline", "AcDb2dPolyline") or hasattr(e, "Coordinates"):
                try:
                    coords = list(getattr(e, "Coordinates", []) or [])
                except Exception:
                    coords = []
                pts = [(float(coords[i]), float(coords[i + 1]))
                       for i in range(0, len(coords) - 1, 2)]
                polys.append({"layer": lay, "points": pts, "count": len(pts)})
                xs.extend(p[0] for p in pts); ys.extend(p[1] for p in pts)
            elif typ == "AcDbBlockReference" or hasattr(e, "EffectiveName"):
                bname = _MinimalCom.effective_block_name(e)
                blocks[bname] = blocks.get(bname, 0) + 1
                p = _pt3(getattr(e, "InsertionPoint", None))
                inserts.append({"name": bname, "layer": lay, "point": p})
                if p:
                    xs.append(p[0]); ys.append(p[1])
            else:
                for key in self._POINT_KEYS:
                    p = _pt3(getattr(e, key, None))
                    if p:
                        xs.append(p[0]); ys.append(p[1])
                        break

        if xs and ys:
            rep["bbox"] = [round(min(xs), 1), round(min(ys), 1),
                           round(max(xs), 1), round(max(ys), 1)]
        return rep

    @staticmethod
    def format_report(rep: Dict[str, object]) -> str:
        """把读图结果渲染成人可读文本。"""
        layers = rep.get("layers") or {}
        blocks = rep.get("blocks") or {}
        kinds = rep.get("entities") or {}
        texts = rep.get("texts") or []
        polys = rep.get("polylines") or []
        lines = [
            "=" * 68,
            f"COM 读图：{rep.get('document')}",
            f"路径：{rep.get('path') or '（未保存）'}",
            f"实体 {sum(kinds.values())} 个 / 图层 {len(layers)} 个 / 块 {len(blocks)} 种"
            f" / 文字 {len(texts)} 条 / 多段线 {len(polys)} 条",
            f"包围盒：{rep.get('bbox')}",
        ]
        if rep.get("truncated"):
            lines.append("⚠ 文字数已达上限，报告被截断")
        if kinds:
            top = sorted(kinds.items(), key=lambda kv: -kv[1])[:8]
            lines.append("实体类型：" + ", ".join(f"{k}×{v}" for k, v in top))
        if layers:
            top = sorted(layers.items(), key=lambda kv: -kv[1])[:12]
            lines.append("图层占用：" + ", ".join(f"{k}({v})" for k, v in top))
        if blocks:
            top = sorted(blocks.items(), key=lambda kv: -kv[1])[:12]
            lines.append("块引用：" + ", ".join(f"{k}×{v}" for k, v in top))
        return "\n".join(lines)


class ComBackend:
    """把图纸画进**运行中的 AutoCAD**。

    传入 `client` 可复用 pyautocad 技能里的 `CADClient`（推荐，带上其多文档管理与清层能力）；
    不传则使用内建的 `_MinimalCom`。
    """

    def __init__(self, client=None, create_if_not_exists: bool = False,
                 visible: bool = True, container=None) -> None:
        self.client = client or _MinimalCom(create_if_not_exists=create_if_not_exists,
                                            visible=visible)
        self._container = container
        self._external = client is not None

    @property
    def space(self):
        if self._container is not None:
            return self._container
        return getattr(self.client, "model", None) or self.client.doc.ModelSpace

    def connect(self) -> str:
        """强制建立连接并返回当前图纸名。

        注意：pyautocad 的 `Autocad()` 是**惰性连接**——构造成功并不代表已经连上 AutoCAD。
        所以这里显式摸一次 `doc`，把「AutoCAD 没开」这种事在**动手画之前**就暴露出来，
        并给出可执行的提示，而不是画到一半抛 COM 异常。
        """
        try:
            doc = self.client.doc
            return str(getattr(doc, "Name", "<未命名图纸>"))
        except Exception as e:  # COM 异常类型由 win32com/comtypes 决定
            raise ConnectionError(
                "无法连接到 AutoCAD（未运行、无打开图纸，或被对话框阻塞）。\n"
                "  处理：① 先打开 AutoCAD 并载入一张图纸；或 ② 加 --create 自动启动；"
                "或 ③ 改用 --backend dxf 直接出 DXF 文件（无需 AutoCAD）。\n"
                f"  原始错误：{type(e).__name__}: {e}"
            ) from e

    def _ensure_layers(self, d: ChemDrawing) -> None:
        import chemcad_layers as LY
        for sem in LY.output_layer_names(d):
            ld = LY.layer_definition(sem)
            if ld is None:
                continue
            if d.meta.get("layer_standard") and ld.linetype.upper() != "CONTINUOUS":
                # 母图 HIDDEN 是 acad.lin 的英制定义（6.35 / -3.175）。
                if not any(str(t.Name).upper() == ld.linetype.upper() for t in self.client.doc.Linetypes):
                    self.client.doc.Linetypes.Load(ld.linetype, "acad.lin")
            try:
                layer = self.client.ensure_layer(sem, ld.color, ld.linetype)
            except TypeError:
                layer = self.client.ensure_layer(sem, color_index=ld.color, linetype=ld.linetype)
            if layer is not None and d.meta.get("layer_standard"):
                layer.Linetype = ld.linetype
                layer.Lineweight = ld.lineweight
                layer.Plottable = ld.plot
                if int(layer.Lineweight) != ld.lineweight:
                    raise RuntimeError(f"图层{sem}线宽写入失败")
        if d.meta.get('layer_standard'):
            import pythoncom
            from win32com.client import VARIANT
            settings = LY.PROFILE['drawing_settings']
            # 两项为AutoCAD短整数系统变量，显式类型避免Boolean/Long被拒。
            for name in ('LWDISPLAY', 'CELWEIGHT'):
                self.client.doc.SetVariable(name, VARIANT(pythoncom.VT_I2, int(settings[name])))

    def clear_layer(self, layer: str) -> int:
        """清空一个图层上的旧图元，返回删除条数。

        三级回退，保证「清层重画」不会像早期版本那样静默空转：
          ① 客户端自带 `clear_layer`（本技能 `_MinimalCom` 已实现）；
          ② 客户端能枚举实体（`entities_on_layer` / ModelSpace）→ 先收集再删除；
          ③ 都不行 → 返回 -1，由调用方据此**明确告知用户**「未能清层」。
        """
        c = self.client
        if hasattr(c, "clear_layer"):
            try:
                return int(c.clear_layer(layer))
            except Exception:
                pass  # 该客户端无可用 clear_layer → 回退逐实体删除
        doomed: List[object] = []
        if hasattr(c, "entities_on_layer"):
            try:
                doomed = list(c.entities_on_layer(layer))
            except Exception:
                doomed = []
        if not doomed:
            try:
                space = getattr(c, "model", None) or c.doc.ModelSpace
                for e in space:
                    try:
                        if str(getattr(e, "Layer", "")) == layer:
                            doomed.append(e)
                    except Exception:
                        continue  # 单个实体图层读取失败 → 跳过，不加入待删集合
            except Exception:
                return -1
        n = 0
        for e in doomed:
            try:
                e.Delete()
                n += 1
            except Exception:
                pass  # 单个实体删除失败 → 继续其余（返回实际删除数）
        return n

    def draw(self, d: ChemDrawing, blocks: bool = True, clear_layers: bool = False,
             undo_mark: bool = True, connect_first: bool = True,
             purge_layers: bool = False) -> Dict[str, object]:
        """执行落图。返回统计信息。

        `clear_layers=True`：先删掉同名图层上的旧图元再画（避免叠加）。
        `purge_layers=True`：落图后清除**空图层**（等价于 AutoCAD 的 PURGE）。
        """
        if any(isinstance(item, Group) and item.meta.get("native_geometry") for item in d.items):
            raise ValueError("新版原生设备块请使用默认 DWG 通道（DXF → AutoCAD COM），不能用直接 COM 简化图元")
        doc_name = self.connect() if connect_first else ""
        self._ensure_layers(d)
        info: Dict[str, object] = {"backend": "autocad-com", "blocks": 0, "entities": 0,
                                   "document": doc_name}

        if clear_layers:
            cleared: Dict[str, int] = {}
            failed: List[str] = []
            for sem in d.used_layers():
                n = self.clear_layer(sem)
                if n < 0:
                    failed.append(sem)
                else:
                    cleared[sem] = n
            info["cleared"] = cleared
            if failed:
                # 不静默：清层失败必须让人知道，否则会以为画干净了
                info["clear_failed"] = failed
                print("⚠ 未能清空这些图层的旧图元（客户端不支持枚举）：" + ", ".join(failed))

        if undo_mark and hasattr(self.client, "start_undo"):
            try:
                self.client.start_undo()
            except Exception:
                undo_mark = False

        created: Dict[Tuple, str] = {}
        names_used: Dict[str, int] = {}
        try:
            for item in d.items:
                if isinstance(item, Group) and blocks:
                    key = (item.name, _geom_key(item))
                    name = created.get(key)
                    if name is None:
                        base_name = item.name
                        n = names_used.get(base_name, 0)
                        name = base_name if n == 0 else f"{base_name}_{n + 1}"
                        names_used[base_name] = n + 1
                        blk = self.client.add_block(name, (0.0, 0.0))
                        self._emit_ents(item.ents, blk, offset=item.base)
                        created[key] = name
                        info["blocks"] = int(info["blocks"]) + 1  # type: ignore[arg-type]
                    self.client.insert_block(self.space, name, item.base)
                    info["entities"] = int(info["entities"]) + 1  # type: ignore[arg-type]
                elif isinstance(item, Group):
                    self._emit_ents(item.ents, self.space, offset=(0.0, 0.0))
                else:
                    self._emit_ents([item], self.space, offset=(0.0, 0.0))
        finally:
            if undo_mark:
                try:
                    self.client.end_undo()
                except Exception:
                    pass  # 撤销标记收尾失败 → 不影响已落图元

        if hasattr(self.client, "regen"):
            try:
                self.client.regen()
            except Exception:
                pass  # regen 仅刷新视图，失败不影响已落图元
        if purge_layers and hasattr(self.client, "purge_empty_layers"):
            try:
                info["purged_layers"] = self.client.purge_empty_layers()
            except Exception:
                pass  # purge 为空层清理，失败不影响图元内容
        if hasattr(self.client, "zoom_extents"):
            try:
                self.client.zoom_extents()
            except Exception:
                pass  # zoom_extents 仅视图缩放，失败不影响图元
        info["layers"] = d.used_layers()
        return info

    def _emit_ents(self, ents: Iterable[Ent], space, offset: Pt) -> None:
        c = self.client
        for e in ents:
            if isinstance(e, Line):
                c.add_line(space, _rel_pt(e.p1, offset), _rel_pt(e.p2, offset), e.layer)
            elif isinstance(e, Polyline):
                c.add_polyline(space, [_rel_pt(p, offset) for p in e.pts], e.closed, e.layer)
            elif isinstance(e, Circle):
                c.add_circle(space, _rel_pt(e.center, offset), e.radius, e.layer)
            elif isinstance(e, Arc):
                c.add_arc(space, _rel_pt(e.center, offset), e.radius,
                          e.start_deg, e.end_deg, e.layer)
            elif isinstance(e, Solid):
                c.add_solid(space, [_rel_pt(p, offset) for p in e.pts], e.layer)
            elif isinstance(e, Text):
                c.add_text(space, e.text, _rel_pt(e.pt, offset), e.height,
                           e.align, e.rotation, e.layer)


def render(d: ChemDrawing, path: Optional[str] = None, backend: str = "dxf",
           **kw) -> Dict[str, object]:
    """统一出图入口。

    backend:
      * `dwg`  —— **落盘 DWG（交付默认）**：先纯 Python 写 DXF，再由 `chemcad_dwg`
        **经 AutoCAD COM** 另存为 DWG（本技能强制走 COM；连不上就抛错，不静默换通道）
      * `dxf`  —— 落盘 DXF（ezdxf/R2010，图层带真线宽）
      * `com`  —— 直接画进**运行中的 AutoCAD**（需 pyautocad + pywin32）
      * `scr`  —— 生成 AutoCAD 脚本 `.scr`（**无需任何 Python 依赖**，CAD 里跑 SCRIPT）
      * `lisp` —— 生成 AutoLISP `.lsp`（走 entmake，可用真图块，APPLOAD 后执行命令）

    `dwg` 后端额外认 `create_if_not_exists`（COM 连不上时是否替用户启动 AutoCAD）。
    """
    if (d.meta.get('fit') or {}).get('overflow'):
        raise ValueError('流程或设备底部文字超出图框可用区，拒绝落盘；须扩大幅面或重新排布')
    annotation_hits = detect_pipe_over_stream_frames(d)
    if annotation_hits:
        raise ValueError(f"流股标注净距不足{len(annotation_hits)}处，拒绝出图")
    connection_issues = validate_pipe_connections(d)
    flow_clearance_issues = validate_pipe_spacing(d) + validate_equipment_nozzle_clearance(d) + validate_shared_port_alignment(d)
    if flow_clearance_issues:
        raise ValueError(f'流股线净距或换热器接管避让不合格，拒绝出图：{flow_clearance_issues}')
    if connection_issues:
        raise ValueError(f"流股设备连接不合格，拒绝出图：{connection_issues}")
    if backend not in ("dwg", "dxf") and any(
            isinstance(item, Group) and item.meta.get("native_geometry") for item in d.items):
        raise ValueError("新版原生设备块只支持 DXF 或默认 DWG 通道，以保留原曲线和填充")
    if backend == "dwg":
        if not path:
            raise ValueError("dwg 后端需要 --out 指定输出文件")
        import tempfile

        import chemcad_dwg as DWG
        create = bool(kw.pop("create_if_not_exists", False))
        tmp = tempfile.mkdtemp(prefix="chemcad_out_")
        try:
            dxf = os.path.join(tmp, "tmp.dxf")
            info = write_dxf(d, dxf, **kw)
            # 无 COM 会话时**抛错**，既不静默给 DXF 也不静默换 accoreconsole
            rep = DWG.to_dwg(dxf, path, create_if_not_exists=create)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        return {**info, "backend": "dwg", "path": path,
                "dwg": rep, "converted_by": rep.get("backend"), "bytes": rep.get("bytes")}
    if backend in ("dxf", "scr", "lisp"):
        if not path:
            raise ValueError(f"{backend} 后端需要 --out 指定输出文件")
        if backend == "dxf":
            return write_dxf(d, path, **kw)
        import chemcad_scr as SCR
        if backend == "scr":
            return SCR.write_scr(d, path, **kw)
        return SCR.write_lisp(d, path, **kw)
    if backend == "com":
        return ComBackend(**kw).draw(d)
    raise ValueError(f"未知后端 '{backend}'，可选 dwg / dxf / com / scr / lisp")


