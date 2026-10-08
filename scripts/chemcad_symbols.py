#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_symbols.py —— 化工图纸符号几何库（设备 / 阀门 / 仪表 / 管件 / 标注 / 图框）。

设计要点
--------
1. **局部坐标作图 + 统一变换落图**：每个符号先在以「连接中心」为原点的局部坐标系里画好，
   再由 `place()` 按 (插入点, 旋转角) 变换。这样同一个符号天然支持水平/竖直两种管道走向。
2. **每个符号返回 `Group`，并携带命名锚点（anchors）**：`equipment.anchors["top"]` 就是塔顶管口坐标。
   管线布线的两端一律用锚点表达，**不允许手写魔数坐标**——这是正交布线与回路闭环可校验的前提。
3. **符号名双向兼容**：既接受工程口语（`闸阀`/`塔`/`换热器`），也接受 `symbol-library.md`
   实测提取到的图例块名（`BV`/`GLO`/`WL004`/`CENTPUMP`…），映射见 `SYMBOLS` 与 `BLOCK_ALIAS`。

标准依据：HG 20559.2（设备图例）、HG 20559.3（管道/阀门/管件图例）、HG 20505（仪表符号，**扫描件待核**）。
"""

from __future__ import annotations

import math
import re
import json
from pathlib import Path
from functools import lru_cache

import chemcad_catalog as C
from chemcad_block_library import ALIASES as EQUIPMENT_ALIASES
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from chemcad_ir import (
    Arc, Circle, Drawing, Ent, Group, Line,     Polyline, Pt, Solid, Text,
    TEXT_H_MAIN, TEXT_H_NOTE, TEXT_H_SMALL, TEXT_H_TB, TEXT_H_TABLE, TEXT_H_TABLE_SMALL,
    box_pts,
)

#: 矩形 (x0, y0, x1, y1)，与 `chemcad_engine.Rect` 同一口径
Rect = Tuple[float, float, float, float]

# ---------------------------------------------------------------------------
# 一、变换与锚点工具
# ---------------------------------------------------------------------------

def _xf_pt(p: Pt, at: Pt, rot_deg: float) -> Pt:
    """局部点 → 世界点（先旋转，后平移）。"""
    r = math.radians(rot_deg)
    c, s = math.cos(r), math.sin(r)
    return (at[0] + p[0] * c - p[1] * s, at[1] + p[0] * s + p[1] * c)


def _xf_ent(e: Ent, at: Pt, rot_deg: float) -> Ent:
    """把实体从局部坐标变换到世界坐标（原地修改并返回）。"""
    if isinstance(e, Line):
        e.p1, e.p2 = _xf_pt(e.p1, at, rot_deg), _xf_pt(e.p2, at, rot_deg)
    elif isinstance(e, Polyline):
        e.pts = [_xf_pt(p, at, rot_deg) for p in e.pts]
    elif isinstance(e, Solid):
        e.pts = [_xf_pt(p, at, rot_deg) for p in e.pts]
    elif isinstance(e, Circle):
        e.center = _xf_pt(e.center, at, rot_deg)
    elif isinstance(e, Arc):
        e.center = _xf_pt(e.center, at, rot_deg)
        e.start_deg += rot_deg
        e.end_deg += rot_deg
    elif isinstance(e, Text):
        e.pt = _xf_pt(e.pt, at, rot_deg)
        e.rotation += rot_deg
    return e


class LocalDraw:
    """局部坐标系下的作图板：把画好的实体暂存，最后一次性变换并塞进 Group。"""

    def __init__(self, name: str, kind: str, tag: str = "", ref: str = ""):
        self.name = name
        self.kind = kind
        self.tag = tag
        self.ref = ref
        self.ents: List[Ent] = []
        self.anchors: Dict[str, Pt] = {}
        self.meta: Dict[str, object] = {}

    # 局部作图原语 -------------------------------------------------------
    def line(self, p1: Pt, p2: Pt, layer: str, **kw) -> Line:
        e = Line(layer=layer, p1=p1, p2=p2, **kw)
        self.ents.append(e)
        return e

    def poly(self, pts: Sequence[Pt], layer: str, closed: bool = False, **kw) -> Polyline:
        e = Polyline(layer=layer, pts=list(pts), closed=closed, **kw)
        self.ents.append(e)
        return e

    def circle(self, c: Pt, r: float, layer: str, **kw):
        e = Circle(layer=layer, center=c, radius=r, **kw)
        self.ents.append(e)
        return e

    def arc(self, c: Pt, r: float, a1: float, a2: float, layer: str, **kw):
        e = Arc(layer=layer, center=c, radius=r, start_deg=a1, end_deg=a2, **kw)
        self.ents.append(e)
        return e

    def solid(self, pts: Sequence[Pt], layer: str, **kw) -> Solid:
        e = Solid(layer=layer, pts=list(pts), **kw)
        self.ents.append(e)
        return e

    def text(self, s: str, pt: Pt, layer: str = "TAG", height: float = TEXT_H_MAIN,
             align: str = "left", rotation: float = 0.0, **kw) -> Text:
        e = Text(layer=layer, text=s, pt=pt, height=height, align=align, rotation=rotation, **kw)
        self.ents.append(e)
        return e

    def anchor(self, name: str, p: Pt) -> None:
        self.anchors[name] = p

    # 落图 ---------------------------------------------------------------
    def commit(self, d: Drawing, at: Pt, rot_deg: float = 0.0) -> Group:
        at = (float(at[0]), float(at[1]))
        for e in self.ents:
            _xf_ent(e, at, rot_deg)
        g = Group(name=self.name, base=at, ents=self.ents, tag=self.tag,
                  kind=self.kind, ref=self.ref, meta=dict(self.meta))
        g.meta["anchors"] = {k: _xf_pt(v, at, rot_deg) for k, v in self.anchors.items()}
        g.meta["rotation"] = rot_deg
        return d.add(g)  # type: ignore[return-value]


def anchor_of(g: Group, name: str) -> Pt:
    """取符号实例的命名锚点；缺失时抛出带可选锚点清单的错误（便于自查规格）。"""
    anchors: Dict[str, Pt] = g.meta.get("anchors", {})  # type: ignore[assignment]
    if name in anchors:
        return anchors[name]
    raise KeyError(
        f"符号 {g.name}（kind={g.kind}, tag={g.tag}）没有锚点 '{name}'；"
        f"可用锚点：{sorted(anchors)}"
    )


def has_anchor(g: Group, name: str) -> bool:
    return name in g.meta.get("anchors", {})  # type: ignore[operator]


# ---------------------------------------------------------------------------
# 四、阀门与管件符号
# ---------------------------------------------------------------------------

def _bowtie(ld: LocalDraw, half: float = 5.0, wing: float = 2.4, layer: str = "VALVE") -> None:
    """阀门通用「蝶形」本体：两个对顶三角形，沿 ±x 方向。

    同时登记 `inlet` / `outlet` 两个锚点——这样阀门既能按「管线上取点」定位
    （规格里的 `on` + `t`），也能在需要精确对位时被引用（`"FV001.inlet"`）。
    """
    ld.poly([(-half, wing), (-half, -wing), (0.0, 0.0)], layer, closed=True)
    ld.poly([(half, wing), (half, -wing), (0.0, 0.0)], layer, closed=True)
    ld.anchor("inlet", (-half, 0.0))
    ld.anchor("outlet", (half, 0.0))


def vl_gate(ld: LocalDraw, **kw) -> None:
    """闸阀。"""
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, 8.0), "VALVE")
    ld.line((-3.0, 8.0), (3.0, 8.0), "VALVE")


def vl_globe(ld: LocalDraw, **kw) -> None:
    """截止阀（GLO / GLOBE）：蝶形 + 阀体上的圆。"""
    _bowtie(ld)
    ld.circle((0.0, 0.0), 2.2, "VALVE")


def vl_ball(ld: LocalDraw, **kw) -> None:
    """球阀（BV）：蝶形 + 球心圆。"""
    _bowtie(ld)
    ld.circle((0.0, 0.0), 2.0, "VALVE")


def vl_butterfly(ld: LocalDraw, **kw) -> None:
    """蝶阀（BFLY）：蝶形 + 中轴斜线。"""
    _bowtie(ld)
    ld.line((0.0, -2.6), (0.0, 2.6), "VALVE")


def vl_check(ld: LocalDraw, **kw) -> None:
    """止回阀（CHECK / NRS 升降式）：蝶形 + 流向箭头（单向）。"""
    _bowtie(ld)
    ld.line((-2.2, 0.0), (1.6, 0.0), "VALVE")


def vl_needle(ld: LocalDraw, **kw) -> None:
    """针型阀（NEEDLE）。"""
    _bowtie(ld, wing=2.0)
    ld.line((0.0, 0.0), (0.0, 7.0), "VALVE")
    ld.poly([(-1.6, 7.0), (1.6, 7.0), (0.0, 9.6)], "VALVE", closed=True)


def vl_plug(ld: LocalDraw, **kw) -> None:
    """旋塞阀（PLUG）。"""
    _bowtie(ld)
    ld.poly([(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)], "VALVE", closed=True)


def vl_control(ld: LocalDraw, actuator: str = "pneumatic", fail: str = "", size: float = 5.0,
               handwheel: bool = False) -> None:
    """调节阀（最终控制元件）。

    HG 20519.2 §3.3.3 必注项：传动形式（气动/电动/液动）+ 气开/气闭 + 有无手动机构 + 有无排放阀。
    `fail` 取 'FC'(气开/失气关) / 'FO'(气闭/失气开) / 'FL'(保位)，会作为文字随阀输出。
    """
    _bowtie(ld, half=size, wing=2.4)
    stem_top = 7.0
    ld.line((0.0, 0.0), (0.0, stem_top), "VALVE")
    top_y = stem_top
    if actuator == "pneumatic":
        # 气动薄膜头：上凸弧 + 弦线
        R = 4.0
        ld.arc((0.0, stem_top), R, 0.0, 180.0, "VALVE")
        ld.line((-R, stem_top), (R, stem_top), "VALVE")
        top_y = stem_top + R
    elif actuator == "electric":
        ld.poly(box_pts(0.0, stem_top + 2.4, 9.0, 4.8), "VALVE", closed=True)
        top_y = stem_top + 4.8
    elif actuator == "hydraulic":
        ld.poly(box_pts(0.0, stem_top + 2.0, 8.0, 3.6), "VALVE", closed=True)
        ld.line((-2.0, stem_top + 2.0), (2.0, stem_top + 2.0), "VALVE")
        top_y = stem_top + 3.8
    # 信号线应接到执行机构（而非阀体中心）——否则信号线会与物料管线重合
    ld.anchor("actuator", (0.0, top_y))
    ld.anchor("valve_body", (0.0, 0.0))
    if handwheel:
        ld.line((-2.6, stem_top - 1.2), (2.6, stem_top - 1.2), "VALVE")
    if fail:
        # 注记须**让开管线**：原先落在 (size+1.5, -1.0)，文字框正好跨过阀体中心线，
        # 管线从字里穿过——即典型「文字压线」。下移到阀体下缘之外。
        ld.text(fail, (size + 1.5, -4.2), "TAG", TEXT_H_SMALL, align="left")
    # 记录必注项，供校验器核对（HG 20519.2 §3.3.3）
    ld.meta["fail"] = fail
    ld.meta["actuator"] = actuator
    ld.meta["handwheel"] = handwheel
    ld.meta["is_final_element"] = True


def vl_relief(ld: LocalDraw, inlet_note: str = "", set_pressure: str = "", **kw) -> None:
    """安全阀/泄压阀（VRL）：蝶形 + 弹簧 + 出口。

    HG 20519.2 §3.3.4：**入口压降受限制的安全阀，须注明入口管段长度及弯头数量**。
    `inlet_note` 就是这条注记（如 `'入口管长 2.5m / 3 个弯头'`），会随阀输出到图上，
    并记录进 meta 供 `validate_relief_valves()` 核对——没注就报出来。
    """
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, 6.0), "VALVE")
    ld.line((-2.2, 6.0), (2.2, 6.0), "VALVE")
    for i in range(3):
        y = 7.0 + i * 1.6
        ld.line((-2.0, y), (2.0, y + 1.0), "VALVE")
    if set_pressure:
        ld.text(set_pressure, (3.0, 4.0), "TAG", TEXT_H_SMALL, align="left")
    if inlet_note:
        ld.text(inlet_note, (3.0, -2.0), "TAG", TEXT_H_SMALL, align="left")
    ld.meta["inlet_note"] = inlet_note
    ld.meta["set_pressure"] = set_pressure
    ld.meta["is_relief"] = True


def vl_rupture(ld: LocalDraw, **kw) -> None:
    """爆破片。"""
    ld.line((-5.0, 0.0), (-1.5, 0.0), "VALVE")
    ld.line((1.5, 0.0), (5.0, 0.0), "VALVE")
    ld.arc((0.0, -2.0), 2.6, 20.0, 160.0, "VALVE")


def vl_three_way(ld: LocalDraw, **kw) -> None:
    """三通阀（3WAY）：三蝶形。"""
    for (px, py) in ((-5.0, 0.0), (0.0, 5.0), (5.0, 0.0)):
        ld.poly([(px - 2.4, py - 2.4), (px + 2.4, py - 2.4), (px, py)], "VALVE", closed=True)
    ld.circle((0.0, 0.0), 1.8, "VALVE")


def vl_spectacle_blind(ld: LocalDraw, **kw) -> None:
    """8 字盲板（DSLIPP，检修隔离）。"""
    ld.circle((0.0, 0.0), 3.0, "VALVE")
    ld.line((0.0, 0.0), (0.0, 7.0), "VALVE")
    ld.circle((0.0, 7.0), 3.0, "VALVE")
    ld.line((-4.5, 0.0), (4.5, 0.0), "VALVE")


def vl_strainer(ld: LocalDraw, shape: str = "Y", **kw) -> None:
    """过滤器：Y 型（YSTL）/ T 型（TSTR）/ 在线（ILSTR）。"""
    _bowtie(ld, half=4.5)
    if shape == "Y":
        ld.line((0.0, 0.0), (5.0, -5.0), "VALVE")
        ld.line((4.0, -6.5), (6.5, -4.0), "VALVE")
    elif shape == "T":
        ld.line((0.0, 0.0), (0.0, -6.0), "VALVE")
        ld.line((-2.0, -6.0), (2.0, -6.0), "VALVE")
    else:
        ld.poly([(-2.0, -3.0), (2.0, -3.0), (2.0, 0.0), (-2.0, 0.0)], "VALVE", closed=True)


def vl_steam_trap(ld: LocalDraw, bypass: bool = False, **kw) -> None:
    """疏水阀（TRAP / ATRAP 带旁通）。"""
    ld.poly([(-4.5, -3.0), (4.5, -3.0), (4.5, 3.0), (-4.5, 3.0)], "VALVE", closed=True)
    ld.arc((-4.5, 0.0), 2.5, 90.0, 270.0, "VALVE")
    ld.arc((4.5, 0.0), 2.5, -90.0, 90.0, "VALVE")
    if bypass:
        ld.line((-4.5, 5.0), (4.5, 5.0), "VALVE")
        ld.line((-4.5, 3.0), (-4.5, 5.0), "VALVE")
        ld.line((4.5, 3.0), (4.5, 5.0), "VALVE")


def vl_flange(ld: LocalDraw, **kw) -> None:
    """法兰（FLGS）。"""
    ld.line((-1.5, -4.0), (-1.5, 4.0), "VALVE")
    ld.line((1.5, -4.0), (1.5, 4.0), "VALVE")




def vl_flame_arrestor(ld: LocalDraw, **kw) -> None:
    """阻火器（ARREST）。"""
    ld.poly([(-4.0, -3.5), (4.0, -3.5), (4.0, 3.5), (-4.0, 3.5)], "VALVE", closed=True)
    for i in range(4):
        x = -2.4 + i * 1.6
        ld.line((x, -3.5), (x, 3.5), "VALVE")


def vl_sight_glass(ld: LocalDraw, **kw) -> None:
    """视镜（SG）。"""
    ld.circle((0.0, 0.0), 3.2, "VALVE")
    ld.line((-6.0, 0.0), (-3.2, 0.0), "VALVE")
    ld.line((3.2, 0.0), (6.0, 0.0), "VALVE")


def vl_orifice(ld: LocalDraw, **kw) -> None:
    """孔板（IOP / PTI2）。"""
    ld.line((-6.0, 0.0), (-1.5, 0.0), "VALVE")
    ld.line((1.5, 0.0), (6.0, 0.0), "VALVE")
    ld.line((0.0, -3.0), (0.0, 3.0), "VALVE")


def vl_reducer(ld: LocalDraw, big: float = 5.0, small: float = 2.5, **kw) -> None:
    """异径管：大端 x 小端（HG 20519.2 要求以大端×小端表示，如 150x100）。"""
    ld.poly([(-4.0, big), (-4.0, -big), (4.0, -small), (4.0, small)], "VALVE", closed=True)


def vl_expansion_joint(ld: LocalDraw, **kw) -> None:
    """膨胀节/波纹管（OBEL）。"""
    zig = [(x, 3.0 if i % 2 == 0 else -3.0) for i, x in enumerate([-4.0 + j * 1.6 for j in range(6)])]
    ld.poly(zig, "VALVE")


def vl_blind(ld: LocalDraw, **kw) -> None:
    """盲板/堵头（CAP / PLUG）。"""
    ld.line((0.0, -4.0), (0.0, 4.0), "VALVE")
    ld.line((-2.5, 0.0), (2.5, 0.0), "VALVE")


# ---------------------------------------------------------------------------
# 五、仪表
# ---------------------------------------------------------------------------

#: 被测变量（首位字母）——通用规则，具体含义以 HG 20505 原件为准
MEASURED_VARS: Dict[str, str] = {
    "A": "分析", "B": "火焰", "C": "电导率", "D": "密度", "E": "电压",
    "F": "流量", "G": "尺寸", "H": "手动", "I": "电流", "J": "功率",
    "K": "时间", "L": "液位", "M": "湿度", "N": "特性", "O": "密度(备)",
    "P": "压力", "Q": "数量", "R": "放射性", "S": "速度/频率", "T": "温度",
    "U": "多变量", "V": "振动", "W": "重量", "X": "未分类", "Y": "事件", "Z": "位置",
}

#: 后续字母的功能——通用规则
FUNCTIONS: Dict[str, str] = {
    "I": "指示", "R": "记录", "C": "控制", "V": "阀/执行", "T": "变送",
    "A": "报警", "S": "开关/联锁", "Z": "驱动/最终元件", "G": "视镜/观察",
    "H": "高", "L": "低", "Q": "累计", "E": "检测元件", "Y": "计算/转换",
    "K": "自动/手动切换", "O": "节流孔", "W": "套管", "U": "多功能",
    # 下面几个是**常用但原先漏收**的，缺了就会把合法位号误报成"未在功能码表中"
    # （实测：`PDI101` 差压指示被报错，而 D=差压/密度 是 ISA-5.1 与 HG 20505 的通用字母）
    "D": "差压/密度", "B": "火焰/烧嘴", "F": "流量比", "J": "扫描",
    "M": "瞬时", "N": "用户自定义", "P": "测试点", "X": "未分类",
}

#: 安装方式 → 仪表圆圈内符号（通用规则，**待核 HG 20505**）
MOUNT_STYLES = {
    "field": "就地安装（空心圆）",
    "panel": "集中盘面安装（圆 + 水平线）",
    "dcs": "DCS/共享显示（圆 + 内接方）",
    "plc": "PLC/计算机（圆 + 内接菱形）",
    "local_panel": "就地盘装（圆 + 水平线，上半为盘前）",
}


def split_tag(tag: str) -> Tuple[str, str]:
    """把仪表位号拆成「功能字母组」与「回路序号」：FSIA001 -> ('FSIA','001')。"""
    t = tag.strip()
    i = len(t)
    while i > 0 and t[i - 1].isdigit():
        i -= 1
    return t[:i], t[i:]


def validate_tag(tag: str) -> List[str]:
    """校验仪表位号构成，返回问题清单（空列表 = 合规）。

    规则（symbol-library.md §4）：
      1. 结构必须是「功能字母组 + 回路序号」；
      2. 首位字母必须是被测变量（M-... 除外）；
      3. 后续字母必须是已知功能码；
      4. 序号建议 3 位（各院惯例，不强制）。
    """
    problems: List[str] = []
    letters, number = split_tag(tag)
    if not letters:
        return [f"位号 '{tag}' 缺少功能字母组"]
    if not number:
        problems.append(f"位号 '{tag}' 缺少回路序号")
    if letters[0].upper() not in MEASURED_VARS:
        problems.append(f"位号 '{tag}' 首位字母 '{letters[0]}' 不是合法被测变量")
    for ch in letters[1:].upper():
        if ch not in FUNCTIONS:
            problems.append(f"位号 '{tag}' 中功能字母 '{ch}' 未在通用功能码表中")
    if number and len(number) < 3:
        problems.append(f"位号 '{tag}' 序号 '{number}' 位数偏少（惯例 3 位）")
    return problems


def inst_bubble(ld: LocalDraw, tag: str = "", mount: str = "field", r: float = 5.0) -> None:
    """仪表圆圈（气泡）。位号按惯例分两行置于圈内：上排功能字母、下排回路序号。"""
    ld.circle((0.0, 0.0), r, "INST")
    if mount in ("panel", "local_panel"):
        ld.line((-r, 0.0), (r, 0.0), "INST")
    elif mount == "dcs":
        k = r / math.sqrt(2.0)
        ld.poly([(-k, -k), (k, -k), (k, k), (-k, k)], "INST", closed=True)
    elif mount == "plc":
        ld.poly([(0.0, r), (r, 0.0), (0.0, -r), (-r, 0.0)], "INST", closed=True)
    letters, number = split_tag(tag) if tag else ("", "")
    h = TEXT_H_SMALL if len(letters) > 2 else TEXT_H_MAIN
    if letters:
        ld.text(letters, (0.0, 1.1), "仪表", h, align="middle_center")
    if number:
        ld.text(number, (0.0, -1.6), "仪表", h, align="middle_center")
    ld.anchor("bubble", (0.0, 0.0))
    ld.anchor("left", (-r, 0.0))
    ld.anchor("right", (r, 0.0))
    ld.anchor("top", (0.0, r))
    ld.anchor("bottom", (0.0, -r))


def inst_logic(ld: LocalDraw, tag: str = "") -> None:
    """逻辑/联锁功能块（方框）。锚点：left / right / top / bottom。"""
    ld.poly(box_pts(0.0, 0.0, 12.0, 8.0), "INST", closed=True)
    letters, number = split_tag(tag) if tag else ("", "")
    if letters:
        ld.text(letters, (0.0, 1.2), "仪表", TEXT_H_SMALL, align="middle_center")
    if number:
        ld.text(number, (0.0, -1.8), "仪表", TEXT_H_SMALL, align="middle_center")
    ld.anchor("left", (-6.0, 0.0))
    ld.anchor("right", (6.0, 0.0))
    ld.anchor("top", (0.0, 4.0))
    ld.anchor("bottom", (0.0, -4.0))


def inst_sensor(ld: LocalDraw, kind: str = "rtd", tag: str = "") -> None:
    """现场检测元件：带套管测温元件（DKX3）、孔板、测压点等。锚点：tip / conn。"""
    if kind in ("rtd", "thermowell", "tw"):
        ld.line((0.0, 0.0), (0.0, -9.0), "INST")
        ld.poly([(-1.6, -9.0), (1.6, -9.0), (0.0, -13.0)], "INST", closed=True)
        ld.anchor("tip", (0.0, -13.0))
    elif kind == "orifice":
        ld.line((-4.0, 0.0), (4.0, 0.0), "INST")
        ld.line((0.0, 0.0), (0.0, -6.0), "仪表线")
        ld.anchor("tip", (0.0, -6.0))
    else:
        ld.circle((0.0, 0.0), 2.0, "INST")
        ld.anchor("tip", (0.0, -2.0))
    ld.anchor("conn", (0.0, 0.0))


# ---------------------------------------------------------------------------
# 六、管线附注：流向箭头、跨图接续、标高、尺寸
# ---------------------------------------------------------------------------

_DIRS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "up": (0.0, 1.0), "down": (0.0, -1.0)}


def add_flow_arrow(d: Drawing, at: Pt, direction: str = "right", size: float = 4.0) -> None:
    """流向箭头（实心三角，SOLID）。P&ID 每根管道都应有明确流向。"""
    dx, dy = _DIRS[direction]
    hx, hy = -dy, dx  # 垂直方向
    tip = (at[0] + dx * size * 0.5, at[1] + dy * size * 0.5)
    b1 = (at[0] - dx * size * 0.5 + hx * size * 0.30, at[1] - dy * size * 0.5 + hy * size * 0.30)
    b2 = (at[0] - dx * size * 0.5 - hx * size * 0.30, at[1] - dy * size * 0.5 - hy * size * 0.30)
    d.solid([tip, b1, b2], "FLOWARROW")


#: **图纸接续标志的真实几何**——实测自 `Drawing1.dwg`
#: （华东赛区·浙江工业大学 PFD 的 AutoCAD 源文件，图层 `07-流股接续标志` /
#: `08-进出物流标志`；同图 `10-文本` 层里的三行 MTEXT 就是这三种标志的图例说明）。
#:
#: 那份图本身就是一张**图例**，三条说明原文（字高 7.0）：
#:   · 为进、出**不同工段**的物流线接续  → 箭头 + **长框**（实测框长 40）
#:   · 为进、出**相同工段**的物流线接续  → 箭头 + **短框**（实测框长 10）
#:   · 为进、出**工厂**的物流线接续      → **剖面线圆**（φ10）+ 箭头
#:
#: 量出来的尺寸（以**箭头尖**为原点、指向 `+x`，单位 mm）：
#:   箭头：长 3.0，高 6.0（底边在 x=-3，上下 ±3）
#:   接续框：高 6.0，从 x=-3 往回延伸；**框长随其中的文字长度变**（实测 40 与 10 两例）
#:   界区圆：半径 5.0，圆心在 x=-5.0；箭头长 5.0、高 10.0
#:
#: ⚠️ 两种"接续"不是一回事，别混用：
#:   `cross` / `same` = 流股**跨图纸**接续（画在流股端头，框里写去/自哪张图）；
#:   `plant`          = 物料**进出厂界区**（画在界区边，与拆不拆张无关）。
CONNECTOR_ARROW_LEN = 3.0
CONNECTOR_BOX_H = 6.0
CONNECTOR_BOX_MIN = 10.0
CONNECTOR_PLANT_R = 5.0
CONNECTOR_PLANT_ARROW = 5.0

#: 三种接续标志的**图块名**（落 DXF 时即 BLOCK 名；多实例同形共用一块）。
#: 与南工大参赛图 `5-接续符号` 层、浙工大 `07-流股接续标志` 层同源几何。
CONNECTOR_BLOCK_NAMES: Dict[str, str] = {
    "cross": "OFFPAGE_CROSS",    # 进/出不同工段（箭头 + 长框）
    "same":  "OFFPAGE_SAME",     # 进/出相同工段（箭头 + 短框）
    "plant": "BATTERY_PLANT",    # 进/出工厂界区（剖面线圆 + 箭头）
}


def _dir_angle(direction: str) -> float:
    """把方向词翻成旋转角（度），供图块 INSERT 用。canonical 朝 +x（right=0）。"""
    return {"right": 0.0, "left": 180.0, "up": 90.0, "down": 270.0}.get(direction, 0.0)


def connector_symbol(kind: str, box_len: Optional[float] = None) -> "LocalDraw":
    """构造「流股接续标志」图块——**canonical：箭头尖在原点、指向 +x**。

    只含**图形**（箭头 / 接续框 / 界区圆 + 剖线），**不含随实例变化的文字**
    （目标图号、来去归属、物流参数）。调用方用 `LocalDraw.commit(d, at, rot_deg)`
    落为可复用 BLOCK/INSERT：`add_offpage_connector` 内部即如此。

    把接续标志做成「图块」而非 inline 几何的动机（承用户要求 + 实测教训）：
    一份拆张 PFD 里跨张/跨工段标志会出现 **几十上百次**（南工大这份实测 106 处），
    若每次都 inline 画箭头 + 框，文件膨胀且无法整体改样式；落成 BLOCK 后同形共用
    一块，既能瘦身，又能在 AutoCAD 里 `BEDIT` 一次改全图。南工大源图 `图纸接续标志.dwg`
    本身虽未建真块（全是 inline），但其几何正是我们这版图库的母本。
    """
    ld = LocalDraw(name=CONNECTOR_BLOCK_NAMES.get(kind, "OFFPAGE_CROSS"),
                  kind="offpage", tag="")
    a = CONNECTOR_ARROW_LEN
    half = CONNECTOR_BOX_H / 2.0
    if kind == "plant":
        r = CONNECTOR_PLANT_R
        c = (-r, 0.0)
        ld.circle(c, r, "BATTERY")
        ld.poly([(-r - 4.4, -2.5), (-r, -2.5), (-r, 2.5), (-r - 4.4, 2.5)],
                "BATTERY", closed=True)
        # 45° 平行线近似 HATCH（ANSI31）：IR 无 HATCH 实体，明确为近似。
        u0, u1 = -r - 4.4, -r
        v0, v1 = -2.5, 2.5
        cc0, cc1 = v0 - u1, v1 - u0
        cc = cc0
        while cc <= cc1 + 1e-9:
            a0 = max(u0, v0 - cc)
            a1 = min(u1, v1 - cc)
            if a1 - a0 > 1e-6:
                ld.line((a0, a0 + cc), (a1, a1 + cc), "BATTERY")
            cc += 1.6
        al = CONNECTOR_PLANT_ARROW
        ld.poly([(0.0, 0.0), (-al, al), (-al, -al)], "BATTERY", closed=True)
        ld.line((-al, al), (-al, -al), "BATTERY")
        return ld
    # cross / same
    if kind == "same":
        box_len = CONNECTOR_BOX_MIN
    else:
        box_len = max(CONNECTOR_BOX_MIN, box_len if box_len is not None else CONNECTOR_BOX_MIN)
    x0 = -a
    x1 = -a - box_len
    ld.poly([(0.0, 0.0), (-a, half), (-a, -half)], "OFFPAGE", closed=True)
    ld.poly([(x0, half), (x1, half), (x1, -half), (x0, -half)], "OFFPAGE", closed=True)
    return ld


def add_offpage_connector(d: Drawing, at: Pt, direction: str = "right",
                          drawing_no: str = "", size: float = 6.0,
                          kind: str = "cross",
                          stream_no: str = "",
                          stream_data: Optional[Dict[str, str]] = None,
                          dest: str = "",
                          body: Optional[Rect] = None,
                          avoid: Optional[Sequence[Rect]] = None,
                          bounds: Optional[Rect] = None) -> Optional[Rect]:
    r"""图纸接续标志——**按真实参赛图纸的三种画法**（见 `CONNECTOR_*` 的实测出处）。

    `kind`：
      * `"cross"` **进/出不同工段**（默认）：箭头 + **长框**，框里写目标图号/工段名；
      * `"same"`  **进/出相同工段**：箭头 + **短框**（框长只够写一个短编号）；
      * `"plant"` **进/出工厂界区**：**剖面线圆** + 箭头（不写编号）。

    为什么不再用"空心三角 + 图号"那个自造的符号：真图上接续标志是
    **带框的**（框里写接续到哪儿），而自造的三角形没有地方写编号，
    画出来读图人不知道这条线去了哪张图——**有框才接得住"去 PFD-03"这种信息**。

    `stream_no` / `stream_data`：**分割流股的物流信息**（用户硬约束）。跨张流股
    在接续标志旁标流股号和T/P/流量三行框——接续框回答"去了哪儿"，
    这条标注回答"跨过去的是多少料、什么状态"。数据直接取自 `.bkp` 解析的
    `streams`，不自造。

    `at` 是**箭头尖**的位置；标志整体朝 `direction` 的反方向延伸（框在箭头后面）。
    """
    # ⚠️ `direction` 可能是 "auto"：候选搜索阶段（下面 4 方向 × 7 档 × 3 档）才定
    # 具体朝向，这里先给一个合法初值供几何映射。外层 `dx,dy` 只被 plant 分支用到；
    # cross/same 的 auto 路径会在候选循环后把 `direction` 重赋成具体方向并重新定义 P。
    dx, dy = _DIRS.get(direction, _DIRS["right"])
    # 沿/垂直两个基向量：把"局部 x 沿指向、局部 y 垂直"的形映射到任意朝向
    hx, hy = -dy, dx

    def P(u: float, v: float) -> Pt:
        """局部坐标 → 世界坐标（u 沿指向，v 垂直）。"""
        return (at[0] + dx * u + hx * v, at[1] + dy * u + hy * v)

    if kind == "plant":
        r = CONNECTOR_PLANT_R
        # 剖面线圆 + 矩形 + 箭头：进出厂界区——整体作为图库块 BATTERY_PLANT 落图
        # （见 connector_symbol），不再 inline 画；同形多实例共用一块，可整体编辑。
        # ⚠️ 真图用 HATCH（ANSI31）填充，IR 无 HATCH 实体，改 45° 平行细线近似
        # （明确为近似，不叫"图案填充"）。
        connector_symbol("plant").commit(d, at, _dir_angle(direction))
        # 来去归属文字：真实参赛 PFD（南工大）的界区连接（"原料苯自总厂来"/
        # "焦油去总厂"）正是画在界区圆旁的——plant 也要标，不能丢（原先直接
        # `return` 把 dest 吞了）。标在箭头尖右侧，不与剖面线圆重叠。
        lab_w = _text_w(dest, TEXT_H_SMALL) if dest else 0.0
        if dest:
            d.text(dest, (at[0],at[1]-STREAMNO_DIAMOND_HALF-5.0), "OFFPAGE", TEXT_H_SMALL,
                   align="middle_center")
        # 足迹（圆 + 箭头 + 文字）供调用方避让；原先返回 None 会导致不被避让
        cx0, cy0 = P(-r - 5.0, -5.0)
        cx1, cy1 = P(lab_w + 3.0, 5.0)
        footprint = (min(cx0, cx1), min(cy0, cy1), max(cx0, cx1), max(cy0, cy1))
        if stream_data is not None:
            marker=P(STREAMNO_DIAMOND_HALF+2.0,0.0)
            owner = f"stream:connector:{marker[0]:.6f},{marker[1]:.6f}"
            d.line(at,P(2*STREAMNO_DIAMOND_HALF+4.0,0.0),"PIPE_PRI",note=owner)
            from chemcad_engine import break_lines_at
            group = add_stream_data(d, marker, no=stream_no,
                                    fields=stream_parameter_fields(stream_data),
                                    placement='right' if abs(dx)<1e-6 else 'top')
            group.ref = owner
            break_lines_at(d,[marker],gap=2*STREAMNO_DIAMOND_HALF)
            points = [pt for entity in group.ents for pt in entity.points()]
            rr = _pts_bbox(points)
            footprint = (min(footprint[0],rr[0]-2.0), min(footprint[1],rr[1]-2.0),
                         max(footprint[2],rr[2]+2.0), max(footprint[3],rr[3]+2.0))
        return footprint

    half = CONNECTOR_BOX_H / 2.0
    if kind == "same":
        box_len = CONNECTOR_BOX_MIN
    else:
        # 框长按内容定：实测长框 40（写"工段/图号"）、短框 10（写短编号）。
        # 一个汉字约 1.0 字高宽，数字/字母约 0.55；两端各留 1.5 余量。
        w = _text_width(drawing_no, TEXT_H_SMALL) if drawing_no else 0.0
        box_len = max(CONNECTOR_BOX_MIN, w + 3.0)
    a = CONNECTOR_ARROW_LEN

    # 归属和编号独立；参数框只含T、P、一个流量，框占位参与接续标志选址。
    lines: List[str] = []
    if dest:
        lines.append(dest)
    params = stream_parameter_fields(stream_data) if stream_data is not None else []
    param_w = sum(stream_annotation_size(params)) if params else 0.0
    lab_w = max(param_w, max([_text_width(s,3.0)+4.0 for s in lines],default=0.0))
    lab_h = (2*STREAMNO_DIAMOND_HALF+20.5 if params else 0.0) + len(lines)*5.0

    # ---- 选址：先算出"放哪儿才不压设备、不压已有文字"，再落笔 --------------
    # 与工段框同一条纪律：**先选址后落笔，逐件避让**。
    # 规格里 offpage 的 `at` 写的是设备 id（解析成设备**中心**），
    # 直接在中心落笔会把整个接续框压在设备身上——所以必须按 `body` 推到体外。
    dirs: Tuple[str, ...] = ("right", "left", "up", "down") if direction == "auto" \
        else (direction,)
    step = CONNECTOR_BOX_H + 4.0 + lab_h

    def _cand(dir_i: str, k: int, extra: float = 0.0):
        """第 dir_i 方向、垂直偏移第 k 档、再朝外多推 `extra` 的候选位。

        `extra` 是给"四周都挤"的情形留的后手：同一台设备挂了 3~4 只接续标志时，
        光靠垂直挪档可能每一档都蹭到别人，再多往外推一点往往就干净了。
        """
        ddx, ddy = _DIRS[dir_i]
        hhx, hhy = -ddy, ddx
        base = (at[0] + hhx * step * k + ddx * extra,
                at[1] + hhy * step * k + ddy * extra)

        def Q(u: float, v: float) -> Pt:
            return (base[0] + ddx * u + hhx * v, base[1] + ddy * u + hhy * v)

        _c = [Q(-a - box_len, -half), Q(0.0, -half), Q(0.0, half), Q(-a - box_len, half)]
        box = (min(p[0] for p in _c), min(p[1] for p in _c),
               max(p[0] for p in _c), max(p[1] for p in _c))
        if lines or params:
            ax, ay = Q(-a, half + 1.5)
            ext_x = ddx if abs(ddx) > 0.5 else hhx
            align_i = "left" if ext_x >= 0 else "right"
            lx0 = ax if align_i == "left" else ax - lab_w
            lab = (lx0, ay, lx0 + lab_w, ay + lab_h)
            fp = (min(box[0], lab[0]), min(box[1], lab[1]),
                  max(box[2], lab[2]), max(box[3], lab[3]))
        else:
            align_i = "left"
            fp = box
        # 推出设备本体：整只标志（框+箭头+标注）都要在 `body` 之外
        if body is not None:
            gp = 2.0
            sx = sy = 0.0
            if ddx > 0.5:
                sx = max(0.0, body[2] + gp - fp[0])
            elif ddx < -0.5:
                sx = min(0.0, body[0] - gp - fp[2])
            elif ddy > 0.5:
                sy = max(0.0, body[3] + gp - fp[1])
            elif ddy < -0.5:
                sy = min(0.0, body[1] - gp - fp[3])
            if sx or sy:
                base = (base[0] + sx, base[1] + sy)
                fp = (fp[0] + sx, fp[1] + sy, fp[2] + sx, fp[3] + sy)
        return base, fp, align_i

    def _ov(a_: Rect, b_: Rect) -> float:
        gx = min(a_[2], b_[2]) - max(a_[0], b_[0])
        gy = min(a_[3], b_[3]) - max(a_[1], b_[1])
        return gx * gy if gx > 0.0 and gy > 0.0 else 0.0

    best = None
    for _i, dir_i in enumerate(dirs):
        for _j, k in enumerate((0, 1, -1, 2, -2, 3, -3)):
            for _m, extra in enumerate((0.0, 8.0, 16.0)):
                base, fp, align_i = _cand(dir_i, k, extra)
                cost = sum(_ov(fp, r) for r in (avoid or ()))
                if bounds is not None:                   # 出界按越界深度重罚
                    cost += 40.0 * (max(0.0, bounds[0] - fp[0]) + max(0.0, fp[2] - bounds[2])
                                    + max(0.0, bounds[1] - fp[1]) + max(0.0, fp[3] - bounds[3]))
                # 同分时守"先右后左 → 先近后远 → 先不外加推距"
                cost += 0.6 * _i + 0.3 * _j + 0.2 * _m
                if best is None or cost < best[0]:
                    best = (cost, dir_i, base, fp, align_i)
    assert best is not None
    _, direction, base, fp, align_i = best

    dx, dy = _DIRS[direction]
    hx, hy = -dy, dx

    def P(u: float, v: float) -> Pt:
        """局部坐标 → 世界坐标（u 沿指向，v 垂直）。"""
        return (base[0] + dx * u + hx * v, base[1] + dy * u + hy * v)

    # 箭头 + 接续框整体作为图库块 OFFPAGE_CROSS / OFFPAGE_SAME 落图（见
    # connector_symbol）：同形标志多实例共用一块，便于整体编辑与文件瘦身。
    x1 = -a - box_len
    connector_symbol(kind, box_len).commit(d, base, _dir_angle(direction))
    # 框内目标图号/工段名（随实例变化，不入块，单独 TEXT）
    if drawing_no:
        d.text(drawing_no, P(x1 + box_len / 2.0, 0.0), "OFFPAGE", TEXT_H_SMALL,
               align="middle_center")

    if dest:
        d.text(dest, P(-a-box_len/2.0,half+3.0), "OFFPAGE",3.0,align="middle_center")
    if params:
        # 菱形放在接续箭头前方的管线延伸段；参数框始终世界坐标上方。
        marker=P(STREAMNO_DIAMOND_HALF+2.0,0.0)
        end=P(2*STREAMNO_DIAMOND_HALF+4.0,0.0)
        owner = f"stream:connector:{marker[0]:.6f},{marker[1]:.6f}"
        d.line(base,end,"PIPE_PRI",note=owner)
        group=add_stream_data(d,marker,no=stream_no,fields=params,
                              placement='right' if abs(dx)<1e-6 else 'top')
        group.ref = owner
        from chemcad_engine import break_lines_at, group_rect
        break_lines_at(d,[marker],gap=2*STREAMNO_DIAMOND_HALF)
        rr=group_rect(list(d.iter_groups())[-1])
        fp=(min(fp[0],marker[0]-STREAMNO_DIAMOND_HALF,rr[0]),
            min(fp[1],marker[1]-STREAMNO_DIAMOND_HALF,rr[1]),
            max(fp[2],marker[0]+STREAMNO_DIAMOND_HALF,rr[2]),max(fp[3],rr[3]))
    return fp


def _text_width(s: str, h: float) -> float:
    """粗略字宽估算（续接框定长、标注占位用）。

    ⚠️ 必须与 `text_rect()` 内部用的 `_text_w` **同一个口径**，否则"我算的占位"
    比"实际画出来的字"窄——避让按窄的去算，压线判定按宽的来判，
    于是明明避让过了仍被判「文字压线」（实测 `℃` U+2103 就踩过这个坑：
    旧口径按 0.55 算，`_text_w` 按 1.0 算）。所以这里直接转调，只留一个真值来源。
    """
    return _text_w(str(s), h)


def add_level_mark(d: Drawing, at: Pt, elev: str = "+0.000", side: str = "left") -> None:
    """标高符号（EL 基准）。设备/管道布置图用，基准统一为 EL±0.000。"""
    w = 6.0
    x = at[0] - w if side == "left" else at[0]
    d.line((x, at[1]), (x + w, at[1]), "TAG")
    d.line((x, at[1]), (x + w * 0.4, at[1] + 2.4), "TAG")
    d.text(f"EL{elev}", (x + w + 1.0, at[1] - 1.0), "TAG", TEXT_H_SMALL, align="left")




# ---------------------------------------------------------------------------
# 七、文字标注：设备位号 / 管道号 / 说明框
# ---------------------------------------------------------------------------

EQUIPMENT_TAG_H: float = 3.0


def add_equipment_tag(d: Drawing, at: Pt, tag: str, name: str = "",
                      mode: str = "full", height: float = EQUIPMENT_TAG_H) -> Group:
    """近设备短位号；完整标注为上位号、下名称，分数线处于两行净空。

    默认 3mm 来自三份用户 PFD 的设备位号样本，见 pfd-reference-study.md。
    at 是名称行中心；整组保持对齐，不由文字抛光逐字拆散。
    """
    h = float(height)
    if h <= 0:
        raise ValueError("设备位号字高必须大于零")
    ld = LocalDraw("EQUIPMENT_LABEL", "annotation")
    if mode == "short" or not name:
        ld.text(tag, (0.0, 0.0), "TAG", h, align="middle_center")
    else:
        spacing = h + 2.0
        ld.text(tag, (0.0, spacing), "TAG", h, align="middle_center")
        ld.text(name, (0.0, 0.0), "TAG", h, align="middle_center")
        w = max(_text_w(str(tag), h), _text_w(str(name), h)) + 2.0
        ld.line((-w / 2.0, spacing / 2.0), (w / 2.0, spacing / 2.0), "TAGBAR")
    return ld.commit(d, at)


def add_equipment_labels(d: Drawing, eq: Dict, rect: Rect, index_enabled: bool,
                         explicit: bool = True, equipment: Optional[Group] = None) -> List[Group]:
    """量尺与出图共用固定位置；有集中名称表时显示短位号。"""
    ann = eq.get("tag_annot", "beside" if index_enabled else "below")
    if ann not in ("none", "below", "beside", "both"):
        raise ValueError(f"未知设备标注模式: {ann}")
    tag, name = str(eq.get("tag", "")), str(eq.get("name", ""))
    if not tag or ann == "none":
        return []
    h = float(eq.get("tag_height", EQUIPMENT_TAG_H))
    if equipment is not None:
        import chemcad_engine as E
        pt = E.equipment_label_point(equipment)
    else:
        pt = ((rect[0]+rect[2])/2.0,rect[1]-10.0)
    label=add_equipment_tag(d,pt,tag,name,mode='short' if index_enabled else 'full',height=h)
    if equipment is not None:
        label.ref='equipment:'+str(eq['id'])
    return [label]


def add_equipment_footer(d: Drawing, equipment: Group, eq: Dict) -> Optional[Group]:
    """截图底部分数式标注；位号中心距首个文本框底边60mm。"""
    tag = str(eq.get('tag') or eq.get('id', ''))
    if not tag or eq.get('folded'):
        return None
    import chemcad_engine as E
    h = float(eq.get('tag_height', EQUIPMENT_TAG_H))
    frames = d.meta.get('text_frames') or [{'at': [0., 0.]}]
    bottom = float(frames[0]['at'][1])
    x = E.equipment_label_point(equipment)[0]
    name = str(eq.get('name', ''))
    group = add_equipment_tag(d, (x, bottom+60.-(h+2. if name else 0.)),
                              tag, name, height=h)
    group.name = 'EQUIPMENT_FOOTER'
    group.ref = 'equipment_footer:'+str(eq['id'])
    return group


#: 用户截图唯一样式，尺寸取浙工大原图0117（assets/stream-annotation-dimensions.json）。
#: 边长10mm，外接14.1421mm；编号3mm居中。
STREAMNO_DIAMOND_SIDE: float = 10.0
STREAMNO_DIAMOND_HALF: float = STREAMNO_DIAMOND_SIDE * math.sqrt(2.0) / 2.0
STREAMNO_TEXT_H: float = 3.0


def add_stream_no_diamond(d: Drawing, at: Pt, no: object, *,
                          digits: int = 4, scale: float = 1.0,
                          text_h: float = STREAMNO_TEXT_H,
                          layer: str = "TAG") -> None:
    """在 `at` 处画**流股编号菱形框**（菱形 + 框心编号）。

    ⚠️ 位数口径 = **4 位**（用户确认）：与浙大图纸实测一致——工段 2 位 + 序号 2 位，
    如 `0101`。左侧补零到 4 位；**已够长则原样不截断**（位数更长也照画）。
    """
    if digits != 4 or scale != 1.0 or text_h != 3.0:
        raise ValueError("流股编号只能采用原图尺寸：四位编号、10mm边长菱形、3mm字高")
    h = STREAMNO_DIAMOND_HALF * float(scale)
    cx, cy = float(at[0]), float(at[1])
    d.polyline([(cx - h, cy), (cx, cy - h), (cx + h, cy), (cx, cy + h)],
               layer, closed=True, color=2)
    s = str(no).strip()
    if not s:
        return
    if s.isdigit():                       # 纯数字才补零；`S-1` 之类保持原样
        s = s.zfill(max(1, int(digits)))
    # 框心居中：水平 center + 垂直 middle（与浙大"文本插入点=框心"一致）
    d.text(s, (cx, cy), layer, float(text_h) * float(scale),
           align="middle_center", note="stream_no_fixed", color=7)


def stream_parameter_fields(data: object) -> List[str]:
    """用户指定的三行参数：T、P、一个流量，其他字段不作管旁注记。"""
    if isinstance(data, dict):
        values = data
    else:
        lines = data if isinstance(data, (list, tuple)) else [data] if data else []
        values = {}
        for line in lines:
            parts = str(line).strip().split(maxsplit=1)
            if parts and parts[0] in ("T", "P", "W", "F", "V"):
                values[parts[0]] = parts[1] if len(parts) > 1 else ""
    flow = next((key for key in ("W", "F", "V") if values.get(key) not in (None, "")), "W")
    return [f"{key} {values[key] if values.get(key) is not None else ''}" for key in ("T", "P", flow)]


def stream_annotation_rows(data: object) -> List[Tuple[str, str]]:
    """截图唯一样式：F/T/P顺序，单位在左、数值在右；不猜单位或换算物性。"""
    fields = stream_parameter_fields(data)
    rows = []
    for field in (fields[2], fields[0], fields[1]):
        key, _, raw = field.partition(" ")
        raw = re.sub(r"(?<=\d)[ ,](?=\d{3}(?:\D|$))", "", raw)
        match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*", raw)
        if match:
            from decimal import Decimal
            value, unit = format(Decimal(match[1]), ".2f"), match[2]
        else:
            value, unit = raw.strip(), ""
        label = "F" if key in ("W", "F", "V") else key
        rows.append((f"{label} {unit}".rstrip(), value))
    return rows


def stream_annotation_size(data: object) -> Tuple[float, float]:
    rows = stream_annotation_rows(data)
    # 浙工大0117实体：10/15mm两栏；实际SHX字宽因子0.7。
    if any(_text_w(k,3.0)*0.7>9.5 or _text_w(v,3.0)*0.7>14.5 for k,v in rows):
        raise ValueError("流股参数超出原图25mm框容量，不能扩框或缩字；请检查数值与单位")
    return (10.0,15.0)


def add_stream_data(d: Drawing, at: Pt, no: str = "",
                    fields: Sequence[str] = (), up: bool = True,
                    offset: Optional[float] = None, height: float = 3.0,
                    line_h: float = 5.0, placement: str = "top") -> Group:
    """三行参数框；竖向流股按用户补充样本从菱形右顶点引线。"""
    if placement not in ("top", "right"):
        raise ValueError("流股参数框只能采用上方或右侧截图样式")
    if not up or height != 3.0 or line_h != 5.0:
        raise ValueError("流股标注必须采用截图样式：上方框、3mm字高、5mm行距")
    bottom = 12.580361429288132
    if offset is not None and abs(float(offset)-bottom)>1e-6:
        raise ValueError("流股标注引线尺寸必须采用浙工大原图0117实测值")
    if bottom <= STREAMNO_DIAMOND_HALF:
        raise ValueError("参数框必须在流股号菱形上方")
    if no:
        add_stream_no_diamond(d, at, no)
    rows = stream_annotation_rows(fields)
    key_w, val_w = stream_annotation_size(fields)
    left, leader_y = 0.0, STREAMNO_DIAMOND_HALF
    if placement == "right":
        left, leader_y = STREAMNO_DIAMOND_HALF, 0.0
        bottom -= STREAMNO_DIAMOND_HALF
    width, top = key_w + val_w, bottom + 15.0
    ld = LocalDraw("STREAM_DATA", "annotation")
    ld.poly([(left,bottom),(left+width,bottom),(left+width,top),(left,top)], "TABLE", closed=True)
    ld.line((left+key_w,bottom),(left+key_w,top), "TABLE")
    ld.anchor("leader", (left,leader_y))
    ld.line((left,leader_y),(left,bottom), "MISC", note="annotation_leader")
    for i,(key,value) in enumerate(rows):
        cy = top - (i+0.5)*5.0
        ld.text(key,(left+key_w/2,cy),"TAG",3.0,align="middle_center")
        ld.text(value,(left+key_w+val_w/2,cy),"TAG",3.0,align="middle_center")
        if i:
            ld.line((left,top-i*5.0),(left+width,top-i*5.0),"TABLE")
    group=ld.commit(d,at)
    for entity in group.ents:
        entity.color=7
    group.meta['stream_marker_at']=list(at)
    group.meta['stream_placement']=placement
    return group


def add_pipe_tag(d: Drawing, at: Pt, line_no: str = "", dn: str = "", grade: str = "",
                 medium: str = "", insulation: str = "", align: str = "left") -> None:
    r"""管道组合号标注，**写在管线之上**。

    组合号按 HG/T 20519 §12.2 拼成**一个完整字符串**：

        物料代号－主项编号－管道序号－管道规格－管道等级[－绝热代号]

    等级不同处须画分界线并注等级（HG 20519.2）；异径管以**大端x小端**表示，如 `150x100`。

    ⚠️ 两处踩过的坑，都留着当戒：

    1. 早先把等级行放在 `at[1] - 3.6`（即**跨过管线**）——文字框正好被管线穿过，
       典型的「文字压线」。这类缺陷正交性/线宽/位号三组校验一条都报不出，
       现由 `chemcad_engine.validate_text_over_line()` 兜底。
    2. 后来又改成「上行管道号、下行等级」两行——看着清楚，但**评审标准按组合号
       逐段给分**（物料代号/主项编号/管道序号/规格/等级/绝热代号，共 1.4 分），
       把等级拆到另一行，等于组合号里那一段"不存在"，3.2.5 与 3.2.6 直接归零。
       标准把组合号定义成**一个字符串**，那就得真拼成一个字符串。
    """
    head = "-".join([p for p in (medium, line_no, dn) if p])
    # 介质代号已含在管线号里时不再重复拼（"PL-PL-SUCT-150" 这种一眼就看出是机器拼的）
    if medium and line_no.upper().startswith(str(medium).upper() + "-"):
        head = "-".join([p for p in (line_no, dn) if p])
    # ---- 组合号要**完整**：物料代号－主项编号－管道序号－规格－**等级**（－绝热代号）----
    # HG/T 20519 §12.2 把管道组合号定义成**一个字符串**，等级是其中一段。
    # 旧实现把等级拆到下一行单独写，于是"组合号里没有等级"——评审标准 3.2.5
    # （0.2 分）按组合号逐段给分，拆行就等于这一段不存在。
    # 绝热代号（3.2.6，0.2 分）同属末段，且标准注明"有此项而没有标注的不得分"。
    full = "-".join([p for p in (head, grade, insulation) if p])
    x, ha = at[0], ("left" if align == "left" else align)
    d.text(full, (x, at[1] + 3.6), "TAG", TEXT_H_MAIN, align=ha)



def add_data_table(d: Drawing, at: Pt, title: str = "",
                   columns: Sequence[Tuple[str, float]] = (),
                   rows: Sequence[Sequence[str]] = (),
                   layer: str = "TABLE", pad: float = 2.0,
                   row_h: float = 8.0, title_h: float = 0.0) -> Tuple[float, float]:
    """通用**数据表**：标题行 + 表头行 + 数据行。左上角定位在 `at`，向下生长。

    `columns` 是 `(表头, 列宽)` 序列；列宽按内容取，不做自适应（表格要能被同事手工改）。

    字高按 HG/T 20519-2009 表 6.2.2 取：**表格中的文字 5mm，格高 < 6mm 时 3mm**。
    所以行高 ≥ 8mm 用 5mm 字，压到 6mm 以下自动降为 3mm —— 这条联动是刻意的：
    评审标准同时管"表格文字 5"和"格高<6 用 3"，两者必须一起满足。

    返回 `(总宽, 总高)`，方便调用方接着摆下一张表。
    """
    colw_decl = [float(w) for _, w in columns]
    n_row = len(rows) + (1 if columns else 0)
    th = float(title_h) if title_h else TEXT_H_NOTE
    title_band = th + 3.0 if title else 0.0
    height = title_band + row_h * n_row + pad * 2
    x0, y0 = float(at[0]), float(at[1])
    text_h = TEXT_H_TABLE if row_h >= 7.5 else TEXT_H_TABLE_SMALL

    # ---- 列宽**按内容定**，声明值只当最小宽度 -------------------------------
    # ⚠️ 这里踩过一次：表头按声明宽度（如"质量流量 kg/h"=26mm）排，
    # 而 5mm 字高的表头实际要 ~34mm，于是**表头文字越过自己的竖分格线**——
    # 一次出图报出 30 处"文字压线"，全是这么来的。
    # 表格的行列线是它自己画的，那就必须由它自己保证文字装得下。
    colw: List[float] = []
    for i, (_head, w) in enumerate(columns):
        cells = [str(_head)] + [str(r[i]) if i < len(r) else "" for r in rows]
        need = max((_text_w(c, text_h) for c in cells), default=0.0) + 4.0
        colw.append(max(float(w), need))
    width = sum(colw)

    d.polyline([(x0, y0), (x0 + width, y0), (x0 + width, y0 - height),
                (x0, y0 - height)], layer, closed=True)
    yy = y0
    if title:
        yy -= title_band
        d.line((x0, yy), (x0 + width, yy), layer)
        d.text(title, (x0 + width / 2.0, yy + th * 0.5 + 0.6), layer, th,
               align="middle_center")
    # 表头
    if columns:
        yy -= row_h
        d.line((x0, yy), (x0 + width, yy), layer)
        cx = x0
        # ⚠️ 必须用**实际列宽 `colw`**（= max(声明宽, 内容需要宽)）来定位表头文字。
        # 先前写成 `for (head, w) in columns`（声明宽），而竖线与整表宽 `width` 用的是 `colw`——
        # 一旦某列被内容撑宽（如图例里 "exchanger   加热"），表头就与自己的竖线**错位**，
        # 表现为"文字压线"（实测 A1 出图时命中 1 处）。
        for _head, _cw in zip((c[0] for c in columns), colw):
            d.text(str(_head), (cx + _cw / 2.0, yy + row_h / 2.0), layer, text_h,
                   align="middle_center")
            cx += _cw
        # 竖线从表头行往下拉，贯穿数据区
        cx = x0
        for w in colw[:-1]:
            cx += w
            d.line((cx, yy), (cx, yy - row_h * len(rows)), layer)
    # 数据行
    for row in rows:
        yy -= row_h
        d.line((x0, yy), (x0 + width, yy), layer)
        cx = x0
        for w, cell in zip(colw, row):
            d.text(str(cell), (cx + w / 2.0, yy + row_h / 2.0), layer, text_h,
                   align="middle_center")
            cx += w
    return width, height


#: 物流表列定义（表头，`streams[]` 字段，列宽 mm）。
#: 列集**直接对着评审标准"完整物流表"的六项**（评审标准 二.2.1~2.6）：
#:   物流号 / 质量流量与质量分率 / 密度与体积流量 / 操作参数(T、P) / 相态与相态分率
STREAM_TABLE_COLS: Tuple[Tuple[str, str, float], ...] = (
    ("物流号", "no", 16.0),
    ("质量流量 kg/h", "W", 26.0),
    ("质量分率 w%", "wf", 22.0),
    ("密度 kg/m³", "rho", 22.0),
    ("体积流量 m³/h", "V", 24.0),
    ("温度 ℃", "T", 18.0),
    ("压力 MPa(G)", "P", 26.0),
    ("相态", "phase", 14.0),
    ("相态分率 %", "pf", 22.0),
)


def stream_table_columns(streams: Sequence[Dict[str, object]]) -> Tuple[Tuple[str, str, float], ...]:
    """值自带单位时采用无预设单位的表头，避免把bar/绝压标成MPa(G)。

    裸数沿用既有默认单位；项目单位须在进入规格前明确。
    """
    cols = []
    numeric_fields = {"T", "P", "W", "wf", "rho", "V", "pf"}
    number = r"[+-]?(?:[\d, ]+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    for head, field, width in STREAM_TABLE_COLS:
        values = [str(st[field]).strip() for st in streams
                  if isinstance(st, dict) and st.get(field) not in (None, "")]
        embedded = field in numeric_fields and any(not re.fullmatch(number, v) for v in values)
        cols.append((head.split()[0] if embedded else head, field, width))
    return tuple(cols)


def add_balance_table(d: Drawing, at: Pt, streams: Sequence[Dict[str, object]],
                      title: str = "物料平衡表") -> Tuple[float, float]:
    """样本式物料表：物流横向列出，参数纵向列出，3mm表格文字。"""
    definitions = list(stream_table_columns(streams))
    if any(st.get("F") is not None for st in streams):
        definitions.append(("摩尔流量", "F", 24.0))
    columns = [("参数 / 物流号", 40.0)] + [(str(st.get("no", "")), 24.0) for st in streams]
    rows = [[head] + [str(st.get(field, "")) if st.get(field) is not None else ""
                      for st in streams] for head, field, _w in definitions if field != "no"]
    return add_data_table(d, at, title=title, columns=columns, rows=rows, row_h=6.0)


def add_stream_table(d: Drawing, at: Pt, streams: Dict[str, Dict[str, object]],
                     title: str = "物料平衡表", order: Sequence[str] = ()
                     ) -> Tuple[float, float]:
    """**物料平衡表（物流表）**——PFD 的必备件，评审标准单列 1.5 分。

    ⚠️ 这东西以前**没有**：引擎只在每条管线旁注一份流股数据块（`add_stream_data`）。
    逐条注记读起来方便，但评审标准要的是**一张表**——"有物流号(0.2) / 物流号完整(0.5)
    / 质量流量和质量分率(0.2) / 密度和体积流量(0.2) / 操作参数(0.2) / 相态及相态分率(0.2)"，
    逐条注记一条都对不上号。**载体不对，内容再全也拿不到分。**

    行的顺序默认按 `streams` 的插入序（规格里就是从上游到下游写的），
    可用 `order` 显式指定流股号顺序。
    """
    keys = list(order) if order else list(streams.keys())
    definitions = stream_table_columns([streams[k] for k in keys if k in streams])
    cols = [(head, w) for head, _f, w in definitions]
    rows: List[List[str]] = []
    for k in keys:
        st = streams.get(k)
        if not isinstance(st, dict):
            continue
        rows.append([str(st.get(f, "")) for _h, f, _w in definitions])
    if not rows:
        return 0.0, 0.0
    return add_data_table(d, at, title=title, columns=cols, rows=rows)


def stream_table_size(streams: Sequence[Dict[str, object]],
                      cols: Optional[Sequence[Tuple[str, str, float]]] = None
                      ) -> Tuple[float, float]:
    """**预估**一张物流表的实际 (宽, 高)，**不画**。

    供 `draw_pfd` 的"先选址、后落笔"用：`add_data_table` 的列宽会按单元格文本
    **自适应膨胀**（`max(声明宽, 该列最长文本 + 4)`），而估位时若只按声明列宽
    （`STREAM_TABLE_COLS` 的 `w` 加起来 = 190）算，会**估窄**——实测流股数据里的
    密度/分率长文本把表撑到 261 mm，估 190 mm 的占位没盖住实际表，表就压到了
    旁边的图例上（报 4 处"文字压线"，全是这么来的）。

    所以这里**逐字复用 `add_data_table` 的列宽算法**算实际宽度，宁可估宽——
    估宽只会多让一点空间，估窄才会压到别人。
    """
    cols_ = cols or stream_table_columns(streams)
    text_h = TEXT_H_TABLE
    colw: List[float] = []
    for i, (_head, _f, w) in enumerate(cols_):
        cells = [str(_head)] + [str(st.get(_f, "")) if isinstance(st, dict) else ""
                                for st in streams]
        need = max((_text_w(c, text_h) for c in cells), default=0.0) + 4.0
        colw.append(max(float(w), need))
    width = sum(colw)
    n_row = len(streams) + (1 if cols_ else 0)
    height = (TEXT_H_NOTE + 3.0) + 8.0 * n_row + 4.0
    return width, height


def add_legend(d: Drawing, at: Pt, entries: Sequence[Tuple[str, str]],
               title: str = "图例", col_w: float = 150.0,
               per_row: int = 3) -> Tuple[float, float]:
    """**图例**（设备图形符号 + 名称）。

    评审标准 1.3 / 2.4「图标（设备图例等）正确」各 0.2 分。
    图例解决的是"评审者认不出你那个符号是什么"——本技能的符号是自绘的
    （`CHEM_TOWER` / `CHEM_PUMP` …），不列图例，外部读者无从对照 HG 20559.2。
    """
    items = [(str(a), str(b)) for a, b in entries if str(a).strip()]
    if not items:
        return 0.0, 0.0
    cols = [(f"符号 / 名称 {i + 1}", col_w) for i in range(max(1, per_row))]
    rows: List[List[str]] = []
    for i in range(0, len(items), len(cols)):
        chunk = items[i:i + len(cols)]
        row = [f"{a}   {b}" for a, b in chunk]
        row += [""] * (len(cols) - len(row))
        rows.append(row)
    return add_data_table(d, at, title=title, columns=cols, rows=rows)


def _rect_hits_any(r: Tuple[float, float, float, float],
                   avoid: Optional[Sequence[Tuple[float, float, float, float]]],
                   pad: float = 0.0) -> bool:
    """矩形是否与任一避让区相交（`pad` = 额外外扩的净距要求）。"""
    if not avoid:
        return False
    x0, y0, x1, y1 = r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad
    for a in avoid:
        if not (x1 <= a[0] or a[2] <= x0 or y1 <= a[1] or a[3] <= y0):
            return True
    return False


def _pts_bbox(pts: Sequence[Pt]) -> Tuple[float, float, float, float]:
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def add_section_frame(d: Drawing, rect: Tuple[float, float, float, float],
                      title: str, *, style: str = "corners",
                      tick: float = 14.0, tag: str = "",
                      avoid: Optional[Sequence[Tuple[float, float, float, float]]] = None
                      ) -> List[str]:
    r"""**工段框**：把某个工段的设备圈出来，并标上工段名。

    `style="corners"`（默认）画**四角标记**，`style="full"` 画整框。

    ⚠️ 为什么默认用角标而不是整框——这是实测逼出来的结论：

    第一版画整框，出图立刻报 **20 处"文字压线"**：框线横穿管号注记、
    流股注记。整框的边框**必然**横穿版面里的文字，而"文字压线"是评审要扣分的
    缺陷（本项目已为此做了三类探测器）。用角标则**构造上不可能压字**：
    四角的短折线只占角落，那里本来就没有文字。

    这也解释了参考图纸里一个怪现象：浙江大学 PFD 的图层表**声明了**
    `分区线` / `内框线` / `外框线` 三个层，但**实体数都是 0** ——
    框线根本没画成整框。各院的通行做法就是**角标定分区 + 工段名**，
    整框只用在内容确实不与外界交织的图（如布置图）上。

    `rect` 是 (x0, y0, x1, y1)，由调用方按已落位设备算好——摆版靠计算，不靠估。

    **`avoid` = 避让区清单**（图纸坐标，如说明框 / 标题栏 / 表格）。
    逐个角标、逐个段名单独判断，命中就**只丢这一处**，不是整框一起丢：

    早先是"整框与家具相交就跳过整个工段标记"，代价太大——一个角压到说明框，
    整个工段就没有任何标记了。而工段框的价值正是**让人一眼看出哪几台设备同属一段**，
    少一段就少一段信息。改成逐件取舍后，最坏也只是少了某一个角或一个段名。

    ⚠️ 这个参数**只有在图纸坐标系里才有意义**：调用方必须在 `fit_to_sheet`
    **之后**（那时设备与家具同为图纸坐标）才传它。`fit_to_sheet` 之前算出的
    工段框是**内容坐标**，与家具的图纸坐标做相交判断，结果是错的
    （实测漏判，报 4 处"SECT 线压说明文字"）。

    返回实际画出的角标序号（0=左下 1=右下 2=左上 3=右上），供调用方统计。
    """
    x0, y0, x1, y1 = rect
    t = max(2.0, float(tick))
    drawn: List[str] = []
    if style == "full":
        if not _rect_hits_any((x0, y0, x1, y1), avoid, pad=1.0):
            d.polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], "SECT", closed=True)
            drawn.append("full")
    else:
        # 四角各一个 L 形折线（开口朝内）——逐个判断是否落在避让区里
        for i, (cx, cy, sx, sy) in enumerate(((x0, y0, 1, 1), (x1, y0, -1, 1),
                                              (x0, y1, 1, -1), (x1, y1, -1, -1))):
            pts = [(cx + sx * t, cy), (cx, cy), (cx, cy + sy * t)]
            if _rect_hits_any(_pts_bbox(pts), avoid, pad=1.0):
                continue
            d.polyline(pts, "SECT")
            drawn.append(str(i))
    # 工段名写在框内上沿（角标样式下也没有框线可压，标题自成一格）
    tx = (x0 + x1) / 2.0
    label = f"{title}" + (f"（{tag}）" if tag else "")
    lp = (tx, y1 - TEXT_H_NOTE - 1.0)
    lr = _pts_bbox([(lp[0] - _text_width(label, TEXT_H_NOTE) / 2.0, lp[1]),
                    (lp[0] + _text_width(label, TEXT_H_NOTE) / 2.0, lp[1] + TEXT_H_NOTE)])
    if not _rect_hits_any(lr, avoid, pad=1.5):
        d.text(label, lp, "SECT", TEXT_H_NOTE, align="middle_center")
        drawn.append("label")
    return drawn


def add_section_stream_box(d: Drawing, at: Pt, title: str,
                           streams: Sequence[Dict[str, object]],
                           width: float = 150.0,
                           cols: Optional[Sequence[Tuple[str, str, float]]] = None
                           ) -> Tuple[float, float]:
    """**工段流股信息框**：把某个工段的流股汇总成一张小表，紧贴该工段的框摆放。

    为什么"设备画完再加一次框"：设备框回答"**这些设备属于哪个工段**"，
    流股框回答"**这个工段的进出料是多少**"——两者是**不同的读图问题**，
    硬塞进同一个框会让框里既挤设备又挤数字。参考图纸（浙大 PFD）也是分开的：
    主图区走设备与 `进出工段物流标志`，流股数据另成一表。

    `at` 是左上角，向下生长（与 `add_data_table` 一致）。

    ⚠️ 列集**必须与整表一致**（默认取 `STREAM_TABLE_COLS`）。第一版只取了
    温度/压力/流量/相态四列，看着"够用"，但评审标准「完整物流表」是**按列给分**的
    （物流号 / 质量流量与质量分率 / 密度与体积流量 / 操作参数 / 相态与相态分率，
    共 1.5 分）——少一列就少一项分。**按工段分框不等于可以少列。**
    """
    definitions = cols or stream_table_columns(streams)
    cols_ = [(h, w) for h, _f, w in definitions]
    fs = [f for _h, f, _w in definitions]
    rows: List[List[str]] = []
    for st in streams:
        if not isinstance(st, dict):
            continue
        rows.append([str(st.get(f, "")) for f in fs])
    if not rows:
        return 0.0, 0.0
    return add_data_table(d, at, title=title, columns=cols_, rows=rows)


def _text_w(s: str, height: float) -> float:
    """估算一串文字的绘制宽度（中文按 1 字高、ASCII 按 0.55 字高）。"""
    return sum(height * 0.55 if ord(ch) < 128 else height for ch in s)


#: 连续的 ASCII 可见字符（含内部空格）算一个折行单元
_ASCII_RUN_RE = re.compile(r"[ -~]+")


def _wrap_tokens(text: str) -> List[str]:
    """切成折行单元：ASCII 连续段整体作一个单元，其余按单字。

    逐字折行会把 `150x100` 劈成 `150x` / `100`，把 `HG 20519` 劈成 `HG` / `20519`——
    图上读起来像两个不同的东西。按单元折行可以避免。
    """
    out: List[str] = []
    i = 0
    while i < len(text):
        m = _ASCII_RUN_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            out.append(text[i])
            i += 1
    return out


def _wrap_cjk(text: str, width: float, height: float, pad: float,
              extra: float = 0.0) -> List[str]:
    """按可用宽度折行（中文按 1 字宽≈1 字高估算，ASCII 按 0.55 计）。

    * `extra`：悬挂缩进宽度——续行要缩进这么多，故每行可用宽度须扣掉它。
    * 折行单元优先取整（见 `_wrap_tokens`），单元超长时才退化为按字符硬切。
    """
    avail = max(1.0, width - 2 * pad - extra)
    lines: List[str] = []
    cur, cur_w = "", 0.0
    for tok in _wrap_tokens(text):
        w = _text_w(tok, height)
        if cur and cur_w + w > avail:
            lines.append(cur.rstrip())
            cur, cur_w, tok = "", 0.0, tok.lstrip()
            w = _text_w(tok, height)
            if not tok:
                continue
        if not cur and w > avail:        # 单个单元比整行还长：按字符硬切
            for ch in tok:
                cw = _text_w(ch, height)
                if cur and cur_w + cw > avail:
                    lines.append(cur.rstrip())
                    cur, cur_w = "", 0.0
                cur += ch
                cur_w += cw
            continue
        cur += tok
        cur_w += w
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


#: 条目自带编号的写法：`1.` `1、` `1)` `（1）` `(1)`
_LEADING_NUM_RE = re.compile(r"^\s*(?:[（(]\s*\d+\s*[)）]|\d+\s*[.、)）])\s*")


def add_notes_box(d: Drawing, at: Pt, title: str = "说明", lines: Sequence[str] = (),
                  width: float = 120.0, line_h: float = 7.0, pad: float = 3.0,
                  auto_number: bool = True) -> None:
    """说明/注释框（左上角锚点）。按框宽自动折行，避免溢出。

    **编号按"逻辑条目"给，不按折行后的物理行给。**
    否则一条说明折成两行就会多出一个编号，图上读起来像多了一条、且续行被编成
    独立条目（实测踩到过：4 条说明画成 5 条，其中一条是残句「00）。」）。
    续行改为**悬挂缩进**——缩进到首行正文起点，一眼能看出是上一条的延续。

    条目若**自带**编号（`1.` / `（1）` / `1）` 等），则全部按自带编号渲染，
    不再自动加号（混用会撞号）；要强制自动编号就先把自带编号删掉。
    """
    raws = [str(s).rstrip() for s in lines]
    explicit = any(_LEADING_NUM_RE.match(s) for s in raws if s)

    items: List[Tuple[str, float, List[str]]] = []      # (前缀, 前缀宽, 折行后的正文)
    n = 0
    for s in raws:
        if not s:
            items.append(("", 0.0, [""]))
            continue
        m = _LEADING_NUM_RE.match(s) if explicit else None
        if m:
            prefix, body = m.group(0).strip() + " ", s[m.end():]
        elif auto_number:
            n += 1
            prefix, body = f"{n}. ", s
        else:
            prefix, body = "", s
        pw = _text_w(prefix, TEXT_H_NOTE)
        items.append((prefix, pw, _wrap_cjk(body, width, TEXT_H_NOTE, pad, extra=pw)))

    n_phys = sum(len(p) for _, _, p in items)
    h = pad * 2 + line_h * (n_phys + 1)
    x, y = at
    d.polyline([(x, y), (x + width, y), (x + width, y - h), (x, y - h)], "MISC", closed=True)
    d.line((x, y - line_h - pad), (x + width, y - line_h - pad), "MISC")
    # 标题中线上移，让出下方那条分隔线——否则分隔线从字身中间穿过（文字压线）。
    # 上移量取**字高的一半**：字高按表 6.2.2 提到 5mm 后，原来的固定 2.6mm 就不够了。
    d.text(title, (x + width / 2.0, y - line_h - pad + TEXT_H_NOTE * 0.5 + 0.2), "MISC",
           TEXT_H_NOTE, align="middle_center")
    i = 0
    for prefix, pw, phys in items:
        for j, ln in enumerate(phys):
            ty = y - line_h * (i + 2) - pad + 1.0
            if j == 0 and prefix:
                d.text(prefix + ln, (x + pad, ty), "MISC", TEXT_H_NOTE, align="left")
            else:
                # 续行缩进到首行正文起点（悬挂缩进）
                d.text(ln, (x + pad + pw, ty), "MISC", TEXT_H_NOTE, align="left")
            i += 1


# ---------------------------------------------------------------------------
# 八、图框与标题栏（HG 20519.1）
# ---------------------------------------------------------------------------

#: 标题栏字段默认值（中文版，drawing-conventions.md §4）
#: `竞赛队名称` 是《工程图纸质量专项评审》1.2.3 明确要求的一项（+0.1 分），
#: 且同一条还要求**设计与审核由不同人员承担**——这两点都不是"图上好看"的问题，
#: 是直接把分扣掉的问题。
DEFAULT_TITLE_FIELDS: Dict[str, str] = {
    "项目名称": "", "专业名称": "工艺", "名称": "",
    "竞赛队名称": "",
    "设计": "", "制图": "", "校对": "", "审核": "", "审定": "",
    "日期": "", "比例": "不按比例", "图号": "", "版次": "A", "共几张": "1",
}


@lru_cache(maxsize=1)
def text_frame_profile():
    """用户指定的默认A1框；资产损坏时明确报错，不能另造替代框。"""
    path = Path(__file__).resolve().parents[1] / "assets/default-text-frame.json"
    return json.loads(path.read_text(encoding="utf-8"))


def add_user_text_frame(d: Drawing, at: Pt = (0.0,0.0), *, fields=None,
                        title="工艺物料流程图(PFD)", subtitle="", number=1, total=1):
    """平移复制用户原框，原生MTEXT/POINT/线条保留，不缩放。"""
    import chemcad_block_library as BL
    profile = text_frame_profile()
    path = Path(__file__).resolve().parents[1] / "assets" / profile["asset"]
    _, name, block, _ = BL.source({"block_file":str(path),"block_name":profile["block"]})
    ld = LocalDraw(name,"frame",ref=profile["source"])
    for ent in block:
        typ = ent.dxftype()
        if typ == "LINE":
            ld.line(tuple(ent.dxf.start)[:2],tuple(ent.dxf.end)[:2],"BORDER")
        elif typ == "LWPOLYLINE":
            ld.poly([tuple(p) for p in ent.get_points("xy")],"BORDER",closed=ent.closed)
        elif typ == "MTEXT":
            ld.text(ent.plain_text(),tuple(ent.dxf.insert)[:2],"BORDER",
                    height=ent.dxf.char_height,align="middle_center")
    ld.meta.update({"library_file":str(path),"library_block":name,"native_geometry":True,
                    "source_center":(0.0,0.0),"geometry_source":"user_frame"})
    group = ld.commit(d,at,0.0)
    f = {**DEFAULT_TITLE_FIELDS, **(fields or {})}
    f["共几张"] = str(total)
    if not subtitle and title != "物料平衡表":
        subtitle = str(f.get("名称", ""))
    values = {"名称":title,"内容":subtitle,"张号":f"第 {number} 张",
              "总张数":f"共 {total} 张","项目名称":str(f.get("项目名称", ""))}
    if f.get("竞赛队名称"):
        values["项目名称"] += "  " + str(f["竞赛队名称"])
    for key, value in values.items():
        slot = profile["slots"][key]
        if value:
            d.text(value,(at[0]+slot["at"][0],at[1]+slot["at"][1]),"BORDER",
                   height=3.0 if key in ("项目名称","内容") else slot["height"],align="middle_center")
    # 复用模板中的签字、日期与专业会签空格，保留既有规格调用方的字段。
    for key, row_y in (("设计",31.36),("制图",25.0),("校对",19.0),("审核",13.0)):
        if f.get(key):
            d.text(str(f[key]),(at[0]+681.0,at[1]+row_y),"BORDER",height=3.0,align="middle_center")
        if f.get("日期"):
            d.text(str(f["日期"]),(at[0]+701.0,at[1]+row_y),"BORDER",height=3.0,align="middle_center")
    if f.get("专业名称"):
        d.text(str(f["专业名称"]),(at[0]+4.17,at[1]+67.0),"BORDER",height=3.0,align="middle_center")
    if f.get("审定"):
        d.text(str(f["审定"]),(at[0]+12.5,at[1]+67.0),"BORDER",height=3.0,align="middle_center")
    identifiers = "  ".join(str(f.get(k,"")) for k in ("图号","版次") if f.get(k))
    if identifiers:
        d.text(identifiers,(at[0]+741.0,at[1]+47.0),"BORDER",height=3.0,align="middle_center")
    d.meta.setdefault("text_frames",[]).append({"at":list(at),"size":profile["size"],
        "source":profile["source"],"title":title,"number":number,"total":total})
    if number == 1:
        d.meta["title_block"] = dict(f)
    return group


def add_sheet_frame(d: Drawing, sheet: str = "A1", margin: float = 10.0,
                    zone: bool = True, fields: Optional[Dict[str, str]] = None) -> None:
    """图纸幅面、图框、分区代号与标题栏。

    标题栏字段覆盖 drawing-conventions.md §4 的中文版要求（含**比例、版次、张数**）——
    这既是交付要求，也是 `check_dxf.py` 标题栏关键字的来源。
    """
    if sheet.upper() == "A1":
        add_user_text_frame(d,fields=fields,total=int((fields or {}).get("共几张",1)),
                            number=int((fields or {}).get('第几张',1)))
        return
    from chemcad_ir import SHEET_SIZES, BIND_MARGIN, EDGE_MARGIN, TEXT_H_TITLE
    W, H = SHEET_SIZES.get(sheet.upper(), SHEET_SIZES["A1"])
    f = dict(DEFAULT_TITLE_FIELDS)
    if fields:
        f.update({k: str(v) for k, v in fields.items()})

    # 边框宽度按 GB/T 14689-2008：**左侧留装订边 25**，其余三边取该幅面的 e 值。
    # 旧版四边一律用同一个 margin，核下来左边只有 5mm——装订边不足，
    # 评审标准 1.1.2「图框尺寸的控制按照 GB/T 14689-2008」这一项就挂不上。
    edge = float(EDGE_MARGIN.get(sheet.upper(), margin))
    ml, mr, mb, mt = float(BIND_MARGIN), edge, edge, edge

    # 内图框
    d.polyline([(ml, mb), (W - mr, mb), (W - mr, H - mt), (ml, H - mt)],
               "BORDER", closed=True)

    # 分区代号（横向 1..n，纵向 A..）
    if zone:
        n = max(1, int((W - ml - mr) // 50))
        for i in range(n):
            x = ml + (W - ml - mr) * (i + 0.5) / n
            d.text(str(i + 1), (x, mb - 5.0), "BORDER", TEXT_H_SMALL, align="middle_center")
            d.text(str(n - i), (x, H - mt + 2.0), "BORDER", TEXT_H_SMALL, align="middle_center")
        m = max(1, int((H - mb - mt) // 50))
        letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        for i in range(m):
            y = mb + (H - mb - mt) * (i + 0.5) / m
            d.text(letters[i % len(letters)], (ml - 5.0, y - 1.0), "BORDER",
                   TEXT_H_SMALL, align="middle_center")
            d.text(letters[i % len(letters)], (W - mr + 5.0, y - 1.0), "BORDER",
                   TEXT_H_SMALL, align="middle_center")

    # ---- 标题栏（右下角，180 x 60）----
    tw, th = 180.0, 60.0
    x0, y0 = W - mr - tw, mb
    rh = th / 6.0
    d.polyline([(x0, y0), (x0 + tw, y0), (x0 + tw, y0 + th), (x0, y0 + th)], "BORDER", closed=True)
    for i in range(1, 6):
        d.line((x0, y0 + rh * i), (x0 + tw, y0 + rh * i), "BORDER")

    def cell(row: int, label: str, value: str, col_split: float = 26.0) -> None:
        """在标题栏第 row 行（自下往上，0 起）写「标签 | 值」。"""
        yy = y0 + rh * row
        d.line((x0 + col_split, yy), (x0 + col_split, yy + rh), "BORDER")
        d.text(label, (x0 + 2.0, yy + rh * 0.35), "BORDER", TEXT_H_TB, align="left")
        d.text(value, (x0 + col_split + 2.0, yy + rh * 0.35), "BORDER",
               TEXT_H_TB, align="left")

    cell(5, "设计项目", f["项目名称"])
    cell(4, "专业名称", f"{f['专业名称']}    比例 {f['比例']}")
    cell(3, "设计 / 制图", f"{f['设计']} / {f['制图']}")
    cell(2, "校对 / 审核", f"{f['校对']} / {f['审核']}")
    cell(1, "审定 / 日期", f"{f['审定']} / {f['日期']}")
    # 参赛队名称：标准 1.2.3 点名要求，靠右放在「设计项目」行（该行右侧空着）
    if str(f.get("竞赛队名称", "")).strip():
        d.text(f"参赛队：{f['竞赛队名称']}",
               (x0 + tw - 2.0, y0 + rh * 5 + rh * 0.35), "BORDER",
               TEXT_H_TABLE_SMALL, align="right")
    # 末行：图号 / 版次 / 第X张 共Y张
    yy = y0
    d.line((x0 + 90.0, yy), (x0 + 90.0, yy + rh), "BORDER")
    d.line((x0 + 140.0, yy), (x0 + 140.0, yy + rh), "BORDER")
    d.text("图号", (x0 + 2.0, yy + rh * 0.35), "BORDER", TEXT_H_TB, align="left")
    d.text(f["图号"], (x0 + 28.0, yy + rh * 0.35), "BORDER", TEXT_H_TB, align="left")
    d.text("版次", (x0 + 92.0, yy + rh * 0.35), "BORDER", TEXT_H_TB, align="left")
    d.text(f["版次"], (x0 + 112.0, yy + rh * 0.35), "BORDER", TEXT_H_TB, align="left")
    d.text("第 1 张 共 %s 张" % f["共几张"], (x0 + 142.0, yy + rh * 0.35), "BORDER",
           TEXT_H_TB, align="left")
    # 图名：表 6.2.2 规定**图名 7mm**（旧版按"说明字高 + 2"凑成 5，偏低）
    d.text(f["名称"], (x0 + tw - 2.0, y0 + rh * 3 + rh * 0.3), "BORDER",
           TEXT_H_TITLE, align="right")
    # 把**实际生效的字段**登记进 meta：绘制期会补默认值 / 覆盖比例栏
    # （布置图会把比例强制写成"不按比例"），判据若只读规格就会读到旧值。
    d.meta["title_block"] = dict(f)


# ---------------------------------------------------------------------------
# 八之三、阀门与管件补遗（HG 20559.3 + 图例实测块名）
# ---------------------------------------------------------------------------

def vl_reducing(ld: LocalDraw, **kw) -> None:
    """减压阀（`VRED`，PRESSURE REDUCING）。蝶形 + 膜头 + 弹簧的调压表示。"""
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, 6.5), "VALVE")
    ld.line((-4.0, 6.5), (4.0, 6.5), "VALVE")
    for i in range(4):                             # 弹簧
        y = 6.5 + i * 1.4
        ld.line((-3.0, y), (3.0, y + 1.0), "VALVE")


def vl_angle(ld: LocalDraw, **kw) -> None:
    """角阀（`VA`，ANGLE）。蝶形 + 直角出口，流向折 90°。"""
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, 7.0), "VALVE")


def vl_vacuum_relief(ld: LocalDraw, **kw) -> None:
    """真空安全阀（`VRR`，RELIEF (VACUUM)）。与泄压安全阀反向安装。"""
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, -6.0), "VALVE")
    ld.line((-2.2, -6.0), (2.2, -6.0), "VALVE")
    for i in range(3):
        y = -7.0 - i * 1.6
        ld.line((-2.0, y), (2.0, y - 1.0), "VALVE")


def vl_slide(ld: LocalDraw, **kw) -> None:
    """刀阀/闸板阀（`TF`，SLIDE / GATE VALVE）。蝶形 + 刀板。"""
    _bowtie(ld)
    ld.line((0.0, -4.5), (0.0, 4.5), "VALVE")
    ld.line((-1.8, 4.5), (1.8, 4.5), "VALVE")
    ld.poly([(-1.8, -4.5), (1.8, -4.5), (0.0, -6.5)], "VALVE", closed=True)


def vl_flush_bottom(ld: LocalDraw, **kw) -> None:
    """冲洗底阀（`TRA`，FLUSH BOTTOM VALVE）。罐底排净用。"""
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, -5.0), "VALVE")
    ld.poly([(-2.6, -5.0), (2.6, -5.0), (2.6, -7.0), (-2.6, -7.0)], "VALVE", closed=True)


def vl_sample(ld: LocalDraw, **kw) -> None:
    """取样阀/取样连接（`AN`，SAMPLE / LABORATORY CONNECTION）。"""
    _bowtie(ld, half=3.8)
    ld.circle((0.0, 0.0), 1.6, "VALVE")
    ld.line((3.8, 0.0), (7.5, 0.0), "VALVE")
    ld.circle((7.5, 0.0), 1.4, "VALVE")


def vl_parallel_slide(ld: LocalDraw, **kw) -> None:
    """带旁通的压力平衡平板闸阀（`OVL`，VALVE WITH PRESSURE EQUALISING）。

    图例(二)中被引用 14 次，是实测最常用的检修隔离阀型——**带旁通**是其要点。
    """
    _bowtie(ld)
    ld.line((0.0, -4.0), (0.0, 4.0), "VALVE")
    ld.line((0.0, 5.6), (0.0, 9.6), "VALVE")        # 旁通段
    ld.line((-4.2, 5.6), (0.0, 5.6), "VALVE")
    ld.line((0.0, 9.6), (4.2, 9.6), "VALVE")
    for x, y in ((-4.2, 5.6), (4.2, 9.6)):          # 旁通上的小阀
        ld.poly([(x - 1.0, y + 1.2), (x - 1.0, y - 1.2), (x + 1.0, y)], "VALVE", closed=True)


def vl_four_way(ld: LocalDraw, **kw) -> None:
    """四通阀（`4WAY`）：四蝶形 + 中心。"""
    for (px, py) in ((-5.0, 0.0), (5.0, 0.0), (0.0, 5.0), (0.0, -5.0)):
        ld.poly([(px - 2.2, py - 2.2), (px + 2.2, py - 2.2), (px, py)], "VALVE", closed=True)
    ld.circle((0.0, 0.0), 1.6, "VALVE")


def vl_funnel(ld: LocalDraw, w: float = 8.0, up: bool = False, **kw) -> None:
    """漏斗/放空斗/排液斗（常压排放口）。up=True 为受料漏斗。"""
    s = 1.0 if up else -1.0
    ld.poly([(-w / 2.0, 3.0 * s), (w / 2.0, 3.0 * s), (w * 0.16, -3.0 * s),
             (-w * 0.16, -3.0 * s)], "VALVE", closed=True)


def vl_rotameter(ld: LocalDraw, **kw) -> None:
    """转子流量计（就地指示流量，装于竖直管段）。锥管 + 浮子。"""
    ld.poly([(-2.4, 6.0), (2.4, 6.0), (1.4, -6.0), (-1.4, -6.0)], "VALVE", closed=True)
    ld.line((-2.0, 1.2), (2.0, 1.2), "VALVE")
    ld.circle((0.0, 1.2), 1.2, "VALVE")


def vl_hose(ld: LocalDraw, **kw) -> None:
    """软管接头/金属软管（`HC`，HOSE CONNECTION）。直段 + 波纹 + 两端接头。"""
    ld.line((-7.0, 0.0), (-4.0, 0.0), "VALVE")
    ld.line((4.0, 0.0), (7.0, 0.0), "VALVE")
    zig = [(x, 1.6 if i % 2 == 0 else -1.6)
           for i, x in enumerate([-4.0 + j * 1.6 for j in range(6)])]
    ld.poly(zig, "VALVE")
    ld.line((-7.0, -1.8), (-7.0, 1.8), "VALVE")
    ld.line((7.0, -1.8), (7.0, 1.8), "VALVE")


def vl_thread(ld: LocalDraw, **kw) -> None:
    """螺纹连接件（`TSO`，SCREWED CONNECTION）。"""
    ld.line((-6.0, 0.0), (-2.0, 0.0), "VALVE")
    ld.line((2.0, 0.0), (6.0, 0.0), "VALVE")
    for x in (-2.0, 0.0, 2.0):
        ld.line((x, -2.2), (x, 2.2), "VALVE")


def vl_silencer(ld: LocalDraw, w: float = 12.0, h: float = 5.0,
                reducing: bool = False, **kw) -> None:
    """消音器（`SILNCR1` 直筒 / `SILNCR2` 异径 / `SHH` 小型在线）。

    锚点：inlet / outlet。
    """
    x0, x1 = -w / 2.0, w / 2.0
    if reducing:
        ld.poly([(x0, -h / 2.0), (x1, -h * 0.16), (x1, h * 0.16), (x0, h / 2.0)],
                "VALVE", closed=True)
    else:
        ld.poly(box_pts(0.0, 0.0, w, h), "VALVE", closed=True)
    for i in range(3):                             # 吸声层
        x = x0 + w * (i + 1) / 4.0
        ld.line((x, -h / 2.0 + 0.8), (x, h / 2.0 - 0.8), "VALVE")
    ld.anchor("inlet", (x0, 0.0))
    ld.anchor("outlet", (x1, 0.0))


# ---------------------------------------------------------------------------
# 八之三之二、通用件补遗（`mine_block_context.py` 对图例 DXF 逐行坐实的块）
# ---------------------------------------------------------------------------
# 这批块此前只有**弱证据**（只知道落在某个并列名称组里，靠字母猜名），
# 现按「图例 INSERT 点 y」与「文字标签 y」同行对齐逐一坐实——块名、含义、
# 行内 y 值、是否被复用都写进 docstring，便于日后回溯。
# 证据表见 `references/symbol-library.md` §3.3；挖掘脚本见 `scripts/mine_block_context.py`。


def vl_jacketed_gate(ld: LocalDraw, **kw) -> None:
    """夹套闸阀（`FM`，图例一 y=992.8 行）。闸阀本体 + 外夹套壁。

    ⚠️ `FM` 是**复用块**：同一块在图例一 y=1131.2 行还画「闸阀」。故 `FM` 的
    默认别名取更基础的「闸阀」；要夹套闸阀请在图例/规格里**显式**写 `夹套闸阀`。
    """
    _bowtie(ld)
    ld.line((0.0, 0.0), (0.0, 8.0), "VALVE")
    ld.line((-3.0, 8.0), (3.0, 8.0), "VALVE")
    # 夹套外壁：包住阀体的一圈双壁（蒸汽/热载体走夹套）
    ld.poly([(-6.6, 3.4), (6.6, 3.4), (6.6, -3.4), (-6.6, -3.4)], "VALVE", closed=True)


def vl_self_actuated(ld: LocalDraw, fail: str = "", size: float = 5.0, **kw) -> None:
    """自力式调节阀（`TJF`，图例一 y=1068.9 行）。弹簧筒膜头，**无外部信号线**。

    ⚠️ `TJF` 是**复用块**：图例一 y=1130.1 行同块画「薄膜调节阀」。薄膜阀用
    `control`（气动膜头 + 仪表信号线）；自力式靠介质自身压力驱动、不接信号线，
    故单列。`TJF` 默认别名取更常用的 `control`；要自力式请显式写 `自力式调节阀`。

    `fail` 与用于 `is_final_element` 的元信息沿 `vl_control` 的口径：
    自力式阀虽无外部能源，其失效位置仍由弹簧决定，仍须注明（HG 20519.2 §3.3.3）。
    """
    _bowtie(ld, half=size)
    stem = 6.0
    ld.line((0.0, 0.0), (0.0, stem), "VALVE")
    ld.poly(box_pts(0.0, stem + 3.0, 8.0, 6.0), "VALVE", closed=True)   # 弹簧筒
    for i in range(3):                                                  # 调压弹簧
        y = stem + 1.2 + i * 1.6
        ld.line((-2.4, y), (2.4, y + 0.8), "VALVE")
    ld.anchor("actuator", (0.0, stem + 6.0))
    ld.anchor("valve_body", (0.0, 0.0))
    if fail:
        # 同 `vl_control`：注记让开管线，别让管线从字里穿过
        ld.text(fail, (size + 1.5, -4.2), "TAG", TEXT_H_SMALL, align="left")
    ld.meta["actuator"] = "self"
    ld.meta["fail"] = fail
    ld.meta["is_final_element"] = True


def vl_drain(ld: LocalDraw, **kw) -> None:
    """排污口（`S`，图例一 y=870.7 行）。管端敞口排污斗，流向自左向右。

    锚点：inlet（接管侧）/ outlet（敞口侧）。
    """
    ld.line((-7.0, 0.0), (-1.2, 0.0), "VALVE")
    # 敞口斗：左侧接短管，右侧外扩开口（右端加一道竖线 = 斗口，非封闭端盖）
    ld.poly([(-1.2, 2.4), (-1.2, -2.4), (3.4, -4.0)], "VALVE")
    ld.line((3.4, -4.0), (3.4, 4.0), "VALVE")
    ld.line((3.4, 4.0), (-1.2, 2.4), "VALVE")
    ld.anchor("inlet", (-7.0, 0.0))
    ld.anchor("outlet", (3.4, 0.0))


def vl_process_drain(ld: LocalDraw, **kw) -> None:
    """工艺排污口（`PS`，图例一 y=854.4 行）。带隔离阀的排污接口。

    ⚠️ `PS` 是**复用块**：图例二 y=1462.6 行同块画「平板闸阀 PARALLEL SLIDE」
    （那里已由 `OVL`→`parallel_slide` 覆盖）。故 `PS` 默认别名取工艺排污口。
    工艺侧排污须可隔离，故比普通「排污口」多一个手动排污阀。锚点：inlet / outlet。
    """
    # 手动排污阀（蝶形 + 手轮）
    ld.poly([(-6.0, 2.3), (-6.0, -2.3), (-3.4, 0.0)], "VALVE", closed=True)
    ld.poly([(-0.8, 2.3), (-0.8, -2.3), (-3.4, 0.0)], "VALVE", closed=True)
    ld.line((-3.4, 0.0), (-3.4, 5.2), "VALVE")
    ld.line((-4.8, 5.2), (-2.0, 5.2), "VALVE")
    ld.line((-0.8, 0.0), (0.4, 0.0), "VALVE")
    # 敞口斗
    ld.poly([(0.4, 2.4), (0.4, -2.4), (4.2, -4.0)], "VALVE")
    ld.line((4.2, -4.0), (4.2, 4.0), "VALVE")
    ld.line((4.2, 4.0), (0.4, 2.4), "VALVE")
    ld.anchor("inlet", (-6.0, 0.0))
    ld.anchor("outlet", (4.2, 0.0))


def inst_push_button(ld: LocalDraw, selector: bool = False, tag: str = "",
                     **kw) -> None:
    """按钮（`PB`，图例一 y=839.7）/ 选择开关和按钮（`SSPB`，y=824.4）。

    `selector=True` 时加一支斜置手柄，表示「选择开关 + 按钮」复合件。
    锚点：signal（接信号/联锁线）/ plunger（操作端）。
    """
    ld.poly(box_pts(0.0, 0.0, 8.0, 8.0), "INST", closed=True)
    ld.line((4.0, 0.0), (7.2, 0.0), "INST")            # 顶杆
    ld.line((7.2, -1.8), (7.2, 1.8), "INST")
    if selector:
        ld.line((-3.0, -3.0), (3.0, -7.0), "INST")     # 选择开关手柄
    if tag:
        ld.text(tag, (0.0, 7.4), "TAG", TEXT_H_SMALL, align="middle_center")
    ld.anchor("signal", (-4.0, 0.0))
    ld.anchor("plunger", (7.2, 0.0))


def inst_selector_button(ld: LocalDraw, **kw) -> None:
    """选择开关和按钮（`SSPB`，图例一 y=824.4 行）。"""
    inst_push_button(ld, selector=True, **kw)


# ---------------------------------------------------------------------------
# 八之四、仪表补遗（就地直读仪表与取样点）
# ---------------------------------------------------------------------------

def inst_gauge(ld: LocalDraw, tag: str = "", style: str = "pressure",
               r: float = 5.0) -> None:
    """就地直读仪表。

    style: pressure 压力表 / temp 双金属温度计 / level 玻璃板液位计 /
           analyzer 取样点。
    锚点：conn（接工艺侧的取源点）/ tip / left / right / top / bottom。
    """
    if style in ("pressure", "pg", "gauge"):
        ld.circle((0.0, 0.0), r, "INST")
        ld.line((0.0, 0.0), (r * 0.60, r * 0.60), "INST")
        ld.line((0.0, 0.0), (0.0, -r - 4.0), "仪表线")
        ld.anchor("conn", (0.0, -r - 4.0))
    elif style in ("temp", "tg", "bimetal"):
        ld.circle((0.0, 0.0), r, "INST")
        ld.line((0.0, -r), (0.0, -r - 3.0), "INST")
        ld.poly([(-1.3, -r - 3.0), (1.3, -r - 3.0), (0.0, -r - 6.5)], "INST", closed=True)
        ld.anchor("conn", (0.0, -r - 6.5))
    elif style in ("level", "lg"):
        ld.line((-1.6, 7.0), (1.6, 7.0), "INST")
        ld.line((-1.6, -7.0), (1.6, -7.0), "INST")
        ld.line((-1.6, -7.0), (-1.6, 7.0), "INST")
        ld.line((1.6, -7.0), (1.6, 7.0), "INST")
        for y in (-5.0, 0.0, 5.0):
            ld.line((-3.0, y), (3.0, y), "INST")
        ld.anchor("conn", (0.0, -7.0))
    else:
        ld.circle((0.0, 0.0), r, "INST")
        ld.text("S", (0.0, 0.0), "仪表", TEXT_H_SMALL, align="middle_center")
        ld.anchor("conn", (0.0, -r))
    if tag:
        ld.text(tag, (0.0, r + 1.2), "仪表", TEXT_H_SMALL, align="middle_center")
    ld.anchor("tip", (0.0, -r - 6.5))
    ld.anchor("left", (-r, 0.0))
    ld.anchor("right", (r, 0.0))
    ld.anchor("top", (0.0, r))
    ld.anchor("bottom", (0.0, -r))


def inst_analyzer(ld: LocalDraw, tag: str = "") -> None:
    """在线分析仪取样点（分析小屋接口）。

    锚点：sample_in / sample_out / conn / left / right。
    知识库图纸 `硝酸尾气处理流程示意图` 的排放监测点（AI/AT）即此类。
    """
    ld.poly(box_pts(0.0, 0.0, 9.0, 7.0), "INST", closed=True)
    ld.line((0.0, 0.0), (0.0, -7.0), "仪表线")
    letters, number = split_tag(tag) if tag else ("", "")
    if letters:
        ld.text(letters, (0.0, 1.0), "仪表", TEXT_H_SMALL, align="middle_center")
    if number:
        ld.text(number, (0.0, -1.6), "仪表", TEXT_H_SMALL, align="middle_center")
    ld.anchor("conn", (0.0, -7.0))
    ld.anchor("sample_in", (-4.5, 0.0))
    ld.anchor("sample_out", (4.5, 0.0))
    ld.anchor("left", (-4.5, 0.0))
    ld.anchor("right", (4.5, 0.0))


# ---------------------------------------------------------------------------
# 九、符号目录与统一入口
# ---------------------------------------------------------------------------

# ponytail: 设备名称与图块直接取自目录，避免再维护一份手绘函数映射。
EQUIPMENT: Dict[str, str] = {
    key: sym["id"]
    for sym in C.symbols()
    for key in (sym["id"], sym["label"], *(sym.get("aliases") or []))
}
EQUIPMENT.update(EQUIPMENT_ALIASES)
TOPOLOGY_NODES = frozenset(k for k, value in C.aspen_block_map().items() if value is None)

VALVES: Dict[str, Callable[..., None]] = {
    "gate": vl_gate, "闸阀": vl_gate,
    "globe": vl_globe, "截止阀": vl_globe,
    "ball": vl_ball, "球阀": vl_ball,
    "butterfly": vl_butterfly, "蝶阀": vl_butterfly, "蝶阀(对夹)": vl_butterfly,
    "check": vl_check, "止回阀": vl_check, "止逆阀": vl_check,
    "needle": vl_needle, "针型阀": vl_needle,
    "plug": vl_plug, "旋塞阀": vl_plug,
    "control": vl_control, "调节阀": vl_control, "控制阀": vl_control,
    "relief": vl_relief, "safety": vl_relief, "安全阀": vl_relief, "泄压阀": vl_relief,
    "rupture": vl_rupture, "爆破片": vl_rupture,
    "three_way": vl_three_way, "三通阀": vl_three_way,
    "spectacle_blind": vl_spectacle_blind, "8字盲板": vl_spectacle_blind, "盲板": vl_spectacle_blind,
    "strainer": vl_strainer, "过滤器(管道)": vl_strainer,
    "steam_trap": vl_steam_trap, "疏水阀": vl_steam_trap,
    "flange": vl_flange, "法兰": vl_flange,
    "flame_arrestor": vl_flame_arrestor, "阻火器": vl_flame_arrestor,
    "sight_glass": vl_sight_glass, "视镜": vl_sight_glass,
    "orifice": vl_orifice, "孔板": vl_orifice,
    "reducer": vl_reducer, "异径管": vl_reducer,
    "expansion_joint": vl_expansion_joint, "膨胀节": vl_expansion_joint,
    "blind": vl_blind, "管帽": vl_blind, "堵头": vl_blind,
    # —— 图例实测块名对应的阀型（HG 20559.3 + 图例(二) 邻近文字挖掘）——
    "reducing": vl_reducing, "减压阀": vl_reducing,
    "angle": vl_angle, "角阀": vl_angle,
    "vacuum_relief": vl_vacuum_relief, "真空安全阀": vl_vacuum_relief,
    "slide": vl_slide, "刀阀": vl_slide, "闸板阀": vl_slide,
    "flush_bottom": vl_flush_bottom, "冲洗底阀": vl_flush_bottom,
    "sample": vl_sample, "取样阀": vl_sample, "取样口": vl_sample,
    "parallel_slide": vl_parallel_slide, "带旁通平板闸阀": vl_parallel_slide,
    "four_way": vl_four_way, "四通阀": vl_four_way,
    "funnel": vl_funnel, "漏斗": vl_funnel, "放空漏斗": vl_funnel,
    "rotameter": vl_rotameter, "转子流量计": vl_rotameter,
    "hose": vl_hose, "软管接头": vl_hose,
    "thread": vl_thread, "螺纹连接": vl_thread,
    "silencer": vl_silencer, "消音器": vl_silencer,
    # —— 图例实测块名坐实（INSERT 点与标签同行对齐得到，见 §八之三之二）——
    "jacketed_gate": vl_jacketed_gate, "夹套闸阀": vl_jacketed_gate,
    "self_actuated": vl_self_actuated, "自力式调节阀": vl_self_actuated,
    "drain": vl_drain, "排污口": vl_drain,
    "process_drain": vl_process_drain, "工艺排污口": vl_process_drain,
}

INSTRUMENTS: Dict[str, Callable[..., None]] = {
    "bubble": inst_bubble, "仪表": inst_bubble, "圆圈": inst_bubble,
    "logic": inst_logic, "逻辑": inst_logic, "联锁": inst_logic,
    "sensor": inst_sensor, "检测元件": inst_sensor,
    "rtd": inst_sensor, "测温元件": inst_sensor, "thermowell": inst_sensor,
    # —— 就地直读仪表与取样点（HG 20505 就地安装类）——
    "gauge": inst_gauge, "压力表": inst_gauge,
    "thermometer": inst_gauge, "双金属温度计": inst_gauge,
    "level_gauge": inst_gauge, "玻璃板液位计": inst_gauge,
    "analyzer": inst_analyzer, "分析仪": inst_analyzer, "取样点": inst_analyzer,
    # —— 盘装操作件（图例一 y=839.7 / 824.4 行）——
    "push_button": inst_push_button, "按钮": inst_push_button,
    "selector_button": inst_selector_button, "选择开关和按钮": inst_selector_button,
}

#: `symbol-library.md` 实测块名（chem.9think.cn 图例）→ 本库符号名。
#: 便于直接照抄既有图例块名作图，也便于与 DXF 反查结果对接。
#: ⚠️ 命名以 `symbol-library.md` §3 的**邻近文字挖掘证据**为准——
#: 上一版有几处是"按字母猜"的错映射，已按强证据修正（VA/VRED/VRR/TF/TRA/SB），
#: 以及 FM/PS/PB 三处**曾错映射为 `bubble`**（会把阀门/排污/按钮画成仪表圆圈！）。
BLOCK_ALIAS: Dict[str, str] = {
    # 阀门（强证据：块旁直接给出英文全名）
    "BV": "ball", "GLO": "globe", "BFLY": "butterfly", "CHECK": "check", "NRS": "check",
    "NEEDLE": "needle", "PLUG": "plug", "3WAY": "three_way", "3WAYRV": "three_way",
    "4WAY": "four_way",
    "VA": "angle",              # ANGLE（角阀）——原误映射为 globe
    "VRED": "reducing",         # PRESSURE REDUCING（减压阀）——原误映射为 globe
    "VRL": "relief", "VRR": "vacuum_relief",   # RELIEF (VACUUM)（真空安全阀）
    "TF": "slide",              # SLIDE / GATE VALVE（刀阀）——原误映射为 gate
    "TRA": "flush_bottom",      # FLUSH BOTTOM VALVE（冲洗底阀）——原误映射为 check
    "DSLIPP": "spectacle_blind",
    "OVL": "parallel_slide",    # 带旁通压力平衡平板闸阀（图例二引用 14 次，最常用）
    "AN": "sample",             # SAMPLE / LABORATORY CONNECTION
    # 管件
    "FLGS": "flange", "CAP": "blind", "PLUGNOZ": "blind", "FLEX": "expansion_joint",
    "OBEL": "expansion_joint", "ILSTR": "strainer", "YSTL": "strainer", "TSTR": "strainer",
    "STRAIN": "strainer", "FLT": "strainer", "TRAP": "steam_trap", "ATRAP": "steam_trap",
    "ARREST": "flame_arrestor", "SG": "sight_glass", "IOP": "orifice", "PTI2": "orifice",
    "HC": "hose",               # HOSE CONNECTION（软管接头）
    "TSO": "thread",            # 螺纹连接件
    "SHH": "silencer", "SILNCR1": "silencer", "SILNCR2": "silencer",
    # 设备
    "CENTPUMP": "pump", "NPUMPC": "pump", "MOTOR": "pump", "DRIER": "filter",
    "WL004": "exchanger", "WL005": "exchanger", "WL006": "exchanger", "WL007": "kettle",
    "SB": "ejector",            # DESUPERHEATER / 多级喷射器——原误映射为 kettle
    "PROP": "reactor",          # PROPELLER TYPE（推进式搅拌器）
    "DTUND": "box",
    "NOZL": "nozzle", "NOZR": "nozzle", "OWIG": "manhole",
    # 仪表（真正的仪表类块）
    "DKX3": "rtd", "XLS": "logic", "AS": "bubble",
    # —— 图例块名逐行坐实（INSERT 点 y 与文字标签 y 同行对齐；见 §八之三之二）——
    #    ⚠️ 下列 FM / TJF / PS 是**复用块**（一块多义），别名只取最常用的一种含义，
    #    另一种含义须在规格里**显式写符号名**：
    "FM": "gate",               # 闸阀(y=1131.2)；同块亦作「夹套闸阀」(y=992.8)→ 显式写 夹套闸阀
    "AQF": "relief",            # 安全阀(y=1028.1)
    "CHBF": "slide",            # 插板阀(y=1081.2)
    "TJF": "control",           # 薄膜调节阀(y=1130.1)；同块亦作「自力式调节阀」→ 显式写 自力式调节阀
    "KB": "orifice",            # 孔板(y=851.1)
    "ZHQ1": "flame_arrestor",   # 阻火器(y=709.2)
    "PS": "process_drain",      # 工艺排污口(y=854.4)；图例二同块作「平板闸阀」→ 用 OVL/parallel_slide
    "PB": "push_button",        # 按钮(y=839.7)
    "SSPB": "selector_button",  # 选择开关和按钮(y=824.4)
    "S": "drain",               # 排污口(y=870.7)
    "SILNCR": "silencer",       # 消音器（SILNCR1/SILNCR2 的基名）
}

# ---------------------------------------------------------------------------
# 多义块：一对多别名 + 上下文消歧
# ---------------------------------------------------------------------------
# 图例里存在**同一个块承担两种含义**的情况（实测 3 处，见 `symbol-library.md` §3.3）。
# `BLOCK_ALIAS` 是 `Dict[str, str]`（一对一），**结构上装不下多义**——所以单列下表：
#   * 每种含义给一组 `keys` 触发词与可选 `layer`（典型图层）；
#   * 判定用**最长键优先**：长键更具体，"夹套闸阀"(4 字) 压过 "闸阀"(2 字)；
#   * 全部落空时取 `default=True` 那条，**保证永远有确定结果**，绝不静默画错。
#
# 上下文来源（强度由高到低）：
#   1. 规格里直接写规范名（`"type": "夹套闸阀"` / `"jacketed_gate"`）→ **绕过本表**，最优先；
#   2. `params.meaning` / `params.hint` / `params.intent`：显式消歧词；
#   3. `tag` 文本中的触发词（如 `JGV-1`）；
#   4. 图层名中的触发词（弱证据，仅加权 3 分）。
#
# ⚠️ 判定结果一律记进 `Group.meta`（`alias_block` / `alias_symbol` / `alias_reason` /
#    `alias_is_default`），可用 `chemcad_engine.report_alias_choices()` 汇总复核。


class AliasRule(NamedTuple):
    """多义块的一种含义。"""
    symbol: str                     # 本库符号名（VALVES / INSTRUMENTS 里的键）
    label: str                      # 图例里的中文名（自动作为触发词）
    keys: Tuple[str, ...] = ()      # 额外触发词（中英文皆可）
    layer: str = ""                 # 该含义的典型图层关键词（弱证据）
    default: bool = False           # 无命中/平局时的兜底项


BLOCK_AMBIGUITY: Dict[str, Tuple[AliasRule, ...]] = {
    # 图例一：y=1131.2「闸阀」/ y=992.8「夹套闸阀」——同块 FM
    "FM": (
        AliasRule("gate", "闸阀", ("闸阀", "gate"), default=True),
        AliasRule("jacketed_gate", "夹套闸阀", ("夹套", "jacket", "伴热", "夹套闸阀"),
                  layer="夹套"),
    ),
    # 图例一：y=1130.1「薄膜调节阀」/ y=1068.9「自力式调节阀」——同块 TJF
    "TJF": (
        AliasRule("control", "薄膜调节阀", ("薄膜", "膜头", "气动", "diaphragm"),
                  default=True),
        AliasRule("self_actuated", "自力式调节阀", ("自力式", "self", "调压", "减压")),
    ),
    # 图例一 y=854.4「工艺排污口」/ 图例二 y=1462.6「平板闸阀 PARALLEL SLIDE」——同块 PS
    "PS": (
        AliasRule("process_drain", "工艺排污口", ("排污", "排放", "drain"), default=True),
        AliasRule("parallel_slide", "平板闸阀",
                  ("平板闸阀", "平板", "parallel", "平衡", "隔离", "闸阀")),
    ),
}


def _alias_words(context: Optional[Dict[str, object]] = None) -> str:
    """把上下文里所有可能带语义的字段拼成一段小写文本，供触发词匹配。"""
    if not context:
        return ""
    parts: List[str] = []
    for k in ("meaning", "hint", "intent", "note", "label", "tag", "layer", "name"):
        v = context.get(k)
        if v:
            parts.append(str(v))
    return " ".join(parts).lower()


def disambiguate_alias(block: str, words: str = "", layer: str = ""
                       ) -> Tuple[Optional[str], str]:
    """多义块消歧 → `(符号名, 判定理由)`；非多义块返回 `(None, "")`。

    计分：触发词命中 `len(键)*2`（**长键更具体、权重更高**），图层命中 +3，
    `default` 仅 +0.5 作平局兜底。取最高分；理由形如 `命中[夹套]` / `默认`。
    """
    rules = BLOCK_AMBIGUITY.get((block or "").strip())
    if not rules:
        return None, ""
    low, lay = (words or "").lower(), (layer or "").lower()
    best: Optional[AliasRule] = None
    best_score, best_hit = -1.0, ""
    for r in rules:
        score, hits = 0.0, []
        for key in (r.label, *r.keys):
            kk = str(key).lower()
            if kk and kk in low:
                score += len(kk) * 2.0          # 长键更具体 → 权重更高
                hits.append(key)
        if r.layer and r.layer.lower() in lay:
            score += 3.0
            hits.append("layer:" + r.layer)
        if r.default:
            score += 0.5                        # 仅作平局兜底，不掩盖真命中
        if score > best_score:
            best_score, best, best_hit = score, r, "+".join(hits)
    if best is None:                            # 理论上不会发生（default 保底）
        return rules[0].symbol, "默认"
    return best.symbol, ("命中[" + best_hit + "]" if best_hit else "默认")


#: 图例里属**线型/注释/水印类**而非符号块的条目——`resolve_symbol` 应把它们
#: 视为「线型约定」而不是未知符号，避免误画占位框。
LINETYPE_BLOCKS: Dict[str, str] = {
    "SHX": "仪表信号线线型（气动/毛细管/模拟/数字）——按线型绘制，非符号",
    "SHXX": "执行机构线型（单作用/双作用气缸）——按线型绘制，非符号",
    "PDR": "管道坡度 / 伴热-夹套物料线线型——按线型绘制，非符号",
    "JJ5": "注释/说明框——按文字框绘制，非符号",
    "WL026": "驱动型式符号（气/柴油/液压/蒸汽透平）——按文字标注绘制",
    "zw$DDF3": "站点水印/图号戳（INSERT 点在图框外 -841,594）——非图面符号，忽略",
}


def _family_of(k: str) -> Tuple[str, str]:
    """已归一化的符号名 → (族, 规范名)。族 ∈ equipment/valve/instrument/unknown。"""
    if k in EQUIPMENT:
        return "equipment", k
    if k in VALVES:
        return "valve", k
    if k in INSTRUMENTS:
        return "instrument", k
    low = k.lower()
    for table, fam in ((EQUIPMENT, "equipment"), (VALVES, "valve"), (INSTRUMENTS, "instrument")):
        for key in table:
            if key.lower() == low:
                return fam, key
    # 形如 FSIA001 / LT002 的仪表位号（symbol-library.md 图例(一)的做法）→ 直接作仪表圆圈
    letters, number = split_tag(k)
    if number and letters and letters[0].isalpha() and letters[0].upper() in MEASURED_VARS:
        return "instrument", "bubble"
    return "unknown", k


def resolve_symbol(kind: str, context: Optional[Dict[str, object]] = None
                   ) -> Tuple[str, str]:
    """把任意符号写法归一化为 (族, 规范名)。族 ∈ equipment/valve/instrument/unknown。

    **多义块先消歧**：`FM` / `TJF` / `PS` 一个块有两种含义（见 `BLOCK_AMBIGUITY`），
    会按上下文判定——`context` 可含 `meaning` / `hint` / `intent`（显式消歧词）、
    `tag`、`layer`。例：

        resolve_symbol("FM", {"hint": "夹套"})     -> ("valve", "jacketed_gate")
        resolve_symbol("FM")                       -> ("valve", "gate")        # 默认闸阀
        resolve_symbol("TJF", {"hint": "自力式"})  -> ("valve", "self_actuated")

    直接写规范名（`"夹套闸阀"` / `"jacketed_gate"`）**绕过本表**，优先级最高。
    """
    k = (kind or "").strip()
    sym, _reason = disambiguate_alias(k, _alias_words(context),
                                      str((context or {}).get("layer", "") or ""))
    if sym:
        k = sym
    elif k in BLOCK_ALIAS:
        k = BLOCK_ALIAS[k]
    return _family_of(k)


def _ascii_name_for(fam: str, key: str) -> str:
    """设备取图库 id；阀门与仪表取函数对应的规范 ASCII 名。"""
    if fam == "equipment":
        return EQUIPMENT.get(key, "SYM").replace(".", "_")
    table = {"valve": VALVES, "instrument": INSTRUMENTS}.get(fam)
    if not table or key not in table:
        return "SYM"
    fn = table[key]
    for k, v in table.items():
        if k.isascii() and v is fn:
            return k
    return "SYM"


def canonical_name(kind: str, context: Optional[Dict[str, object]] = None) -> str:
    """符号的规范 ASCII 名（用于图块名）：中文别名也会归到同一 ASCII 名。

    图块名进 DXF 后须为 ASCII，否则 R12 等旧版需转义、跨工具易失真。
    多义块会按 `context` 消歧后再取名（见 `resolve_symbol`）。
    """
    fam, key = resolve_symbol(kind, context)
    return _ascii_name_for(fam, key)


def place_symbol(d: Drawing, kind: str, at: Pt, rot: float = 0.0, tag: str = "",
                 name: Optional[str] = None, **params) -> Group:
    """统一落图入口。设备仅读取具名图库块，缺块时要求用户确认；禁止占位。

    返回 Group（含 anchors），供管线布线取锚点。

    `rot` 单位为度：0 = 管道水平（左→右），90 = 管道竖直（下→上）。

    **锚点兜底**：若某符号没显式登记锚点（管件类符号通常如此，因为它们多数
    按「管线上取点」定位），则按其几何包围盒自动补 `inlet` / `outlet` /
    `top` / `bottom` 四个锚点——保证任何符号都能被规格精确引用，
    不会因为「忘了写锚点」而让规格报 `没有锚点` 的错。

    **多义块消歧**：`params` 里的 `meaning` / `hint` / `intent`（显式消歧词）会被取出
    作为上下文（不再传给符号函数），连同 `tag`、`layer` 一起交给 `resolve_symbol`，
    用于判定 `FM`/`TJF`/`PS` 这类一块两义的块。判定结果记进 `meta`。
    """
    ctx: Dict[str, object] = {"tag": tag}
    for ck in ("meaning", "hint", "intent", "note", "layer"):
        if ck in params:
            ctx[ck] = params.pop(ck)
    fam, key = resolve_symbol(kind, ctx)
    if fam == "equipment":
        import chemcad_block_library as BL
        eq = {"id": tag or name or key, "type": key, "params": params}
        BL.preflight({"equipment": [eq]})
        return BL.place(d, eq, at, rot=rot, tag=tag)
    sym_name = name or f"CHEM_{_ascii_name_for(fam, key).upper()}"
    ld = LocalDraw(sym_name, fam or "unknown", tag=tag)

    # 多义块一律留痕，便于 report_alias_choices() 汇总复核
    _sym, _reason = disambiguate_alias((kind or "").strip(),
                                       _alias_words(ctx), str(ctx.get("layer", "") or ""))
    if _sym:
        ld.meta["alias_block"] = (kind or "").strip()
        ld.meta["alias_symbol"] = _sym
        ld.meta["alias_reason"] = _reason
        ld.meta["alias_is_default"] = (_reason == "默认")

    if fam == "valve":
        VALVES[key](ld, **params)
    elif fam == "instrument":
        if key in ("bubble", "logic", "sensor", "rtd", "thermowell", "检测元件", "测温元件",
                   "gauge", "压力表", "thermometer", "双金属温度计", "level_gauge",
                   "玻璃板液位计", "analyzer", "分析仪", "取样点",
                   "push_button", "按钮", "selector_button", "选择开关和按钮"):
            params["tag"] = tag
            INSTRUMENTS[key](ld, **params)
        else:
            INSTRUMENTS[key](ld, **params)
    else:
        from chemcad_block_library import BlockConfirmationRequired
        raise BlockConfirmationRequired(
            f"未识别符号 {kind!r}，请用户确认对应图库块；禁止使用占位方框代替")
    _fallback_anchors(ld)
    return ld.commit(d, at, rot)


def _fallback_anchors(ld: LocalDraw) -> None:
    """给没有显式锚点的符号按包围盒补 4 个通用锚点（只补缺，不覆盖）。"""
    if ld.anchors:
        return
    xs = [p[0] for e in ld.ents for p in e.points()]
    ys = [p[1] for e in ld.ents for p in e.points()]
    if not xs:
        return
    ld.anchor("inlet", (min(xs), (min(ys) + max(ys)) / 2.0))
    ld.anchor("outlet", (max(xs), (min(ys) + max(ys)) / 2.0))
    ld.anchor("top", ((min(xs) + max(xs)) / 2.0, max(ys)))
    ld.anchor("bottom", ((min(xs) + max(xs)) / 2.0, min(ys)))

