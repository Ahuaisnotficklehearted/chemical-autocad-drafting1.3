#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""chemcad_score.py —— 《"工程图纸质量"专项评审 评分实施细则》(2026) 的可执行化。

**为什么必须单独一个模块，而不是扩进 `chemcad_engine.validate_*`**

引擎原有的全部校验器都是**自证**的：线宽对照自家 `LAYER_REGISTRY` 的档位声明，
图幅对照自家 `SHEET_SIZES`，字高对照自家 `TEXT_H_*`。这类校验有个结构性盲区——
**它永远发现不了"登记表本身写错了"**。实测就撞上过：`EQUIP` 登记为 `coarse`
（0.70 mm），而评分标准要求"设备、机器轮廓线 0.25 mm"。自家校验器一路绿灯，
按标准判则直接扣分。

评分标准是这套技能拿到的**第一个外部基准**：它由评审专家写，不关心我们内部
怎么登记、怎么命名、用什么算法。所以本模块的每条判据都**逐字引用标准原文**
（`basis` 字段），判据与登记表互相独立——登记表改了，这里不会跟着松。

**范围声明（诚实优先）—— 评分表已是纯 PFD 口径**

标准共 20 分，本技能**只出 PFD**（不含 P&ID、示意设备布置图），
故评分表**只保留标准里属于 PFD 的条款**，共 6 分：

    一、格式规范性·PFD 部分 1 分     → 覆盖（8 条）
    二、PFD 内容正确性与完整性 5 分   → 覆盖（13 条）

原标准的 P&ID（一/三，共 6 分）、车间设备布置图与分厂平面布置图（4 分）、
设备布置图（3 分）、总平面布置图（3 分）、以及"P&ID↔PFD 一致"（0.4 分）
——**均已从评分表移除**：此前是照实标 `out_of_scope` 列出，现按用户要求改为
纯 PFD 口径，不再入表。既不假报得分，也不占用分母。

⚠️ 因此本表的"满分"是 **6 分（PFD 口径）**，不是标准的 20 分；与标准总分
对照时要注明口径，别把 6 分满分当成 20 分满分去算得分率。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chemcad_engine as E  # noqa: E402
from chemcad_ir import (  # noqa: E402
    LAYER_REGISTRY, SHEET_SIZES, Polyline, Solid, Text, resolve_layer,
)

Pt = Tuple[float, float]
Rect = Tuple[float, float, float, float]

# ---------------------------------------------------------------------------
# 一、标准里的图线宽度表（表 6.1.3）与文字高度表（表 6.2.2）
# ---------------------------------------------------------------------------
#: P&ID / PFD 的图线宽度（mm）。表 6.1.3 的 备注 列写着"设备、机器轮廓线 0.25mm"，
#: 这一条是**最容易被忽略**的：主物料管道才是粗线，设备轮廓反而要细。
W_PRI_PIPE = (0.6, 0.9)          # 主物料管道
W_OTHER_PIPE = (0.3, 0.5)        # 其他物料管道
W_OTHER = (0.15, 0.25)           # 其他
W_EQUIP_OUTLINE = 0.25           # 设备、机器轮廓线（表 6.1.3 备注）

#: 表 6.2.2 推荐字高（mm）。标题栏内的文字高度标准明确"暂不作要求"。
H_TABLE_TITLE = (5.0, 7.0)       # 图表中的图名及视图符号
H_PROJECT_NAME = 5.0             # 工程名称
H_NOTE = 5.0                     # 图纸中的文字说明及轴线号
H_DIGIT = (2.0, 3.0)             # 图纸中的数字及字母
H_DWG_NAME = 7.0                 # 图名
H_TABLE_TEXT = 5.0               # 表格中的文字
H_TABLE_TEXT_SMALL = 3.0         # 表格中的文字（格高小于 6mm 时）

#: GB/T 14689-2008 边框宽度：留装订边 a=25；不留装订边 e=10(A0/A1) / 5(A2/A3/A4)
BIND_MARGIN = 25.0
EDGE_MARGIN = {"A0": 10.0, "A1": 10.0, "A2": 5.0, "A3": 5.0, "A4": 5.0}


# ---------------------------------------------------------------------------
# 二、条目与结果的数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    """评分标准里的一条。`basis` 必须是**标准原文**，不是我们对它的理解。"""

    code: str
    title: str
    points: float
    scope: str                 # 恒为 "pfd"——评分表已改纯 PFD 口径（保留字段供报告输出）
    basis: str
    checker: str = ""          # 对应的检查函数名；空 = 需人工/图纸外材料判定


@dataclass
class Result:
    item: Item
    got: float = 0.0
    verdict: str = ""          # "得分" / "不得分" / "部分" / "需人工判定" / "超出范围"
    evidence: str = ""
    hint: str = ""

    @property
    def full(self) -> float:
        # 评分表已纯 PFD：不再有 out_of_scope 条目，满分即条目分值
        return self.item.points


#: 评分标准·**纯 PFD 口径**全表：逐条照抄标准里属于 PFD 的条款。
#:
#: ⚠️ 评分表已按用户要求改为**纯 PFD 口径**。原标准的 P&ID（一/三，共 6 分）、
#: 车间设备布置图与分厂平面布置图（4 分）、设备布置图（3 分）、总平面布置图（3 分）、
#: 以及"P&ID↔PFD 一致"（0.4 分）**已从评分表移除**——此前是照实标 `out_of_scope`
#: 列出，现按纯 PFD 口径不再入表。本技能只出 PFD：这些条款既不假报得分，也不占分母。
def rubric() -> List[Item]:
    it: List[Item] = []

    # ---- 一、格式规范性·PFD 部分（1 分）--------------------------------
    it += [
        Item("1.1.1", "PFD·有图框", 0.1, "pfd", "有图框，+0.1 分", "chk_border"),
        Item("1.1.2", "PFD·图框符合 GB/T 14689-2008", 0.1, "pfd",
             "图框尺寸的控制按照以下标准进行（GB/T 14689-2008 技术制图 图纸幅面和格式）",
             "chk_border_std"),
        Item("1.2.1", "PFD·有标题栏", 0.1, "pfd", "有标题栏，+0.1 分", "chk_titleblock"),
        Item("1.2.2", "PFD·有项目名称", 0.1, "pfd", "有项目名称，+0.1 分", "chk_tb_project"),
        Item("1.2.3", "PFD·有竞赛队名称，且设计/审核非同一人", 0.1, "pfd",
             "有竞赛队名称，项目设计的设计、审核由不同人员承担，+0.1 分",
             "chk_tb_team_and_review"),
        Item("1.3", "PFD·图标（设备图例等）正确", 0.2, "pfd",
             "图标（设备图例等）正确，+0.2 分", "chk_legend"),
        Item("1.4", "PFD·图线宽度", 0.1, "pfd",
             "图线宽度参照 HG/T 20519-2009 表 6.1.3；备注：设备、机器轮廓线 0.25mm",
             "chk_lineweights"),
        Item("1.5", "PFD·文字高度", 0.2, "pfd",
             "文字高度参照 HG/T 20519-2009 表 6.2.2 执行", "chk_text_heights"),
    ]
    # ---- 二、PFD 内容正确性与完整性（5 分）------------------------------
    it += [
        Item("2.1.1", "主项内及主项间物料进出口完整、标识正确", 0.5, "pfd",
             "主项内及主项间物料进出口完整、标识正确，+0.5 分", "chk_pfd_io"),
        Item("2.1.2", "设备进出口物料完整", 0.5, "pfd", "设备进出口物料完整，+0.5 分",
             "chk_pfd_equip_io"),
        Item("2.1.3", "物料流向标识正确", 0.5, "pfd", "物料流向标识正确，+0.5 分",
             "chk_pfd_arrows"),
        Item("2.1.4", "物流压力变化合理，表示有压力变化处的阀门", 0.5, "pfd",
             "物流压力变化合理，表示有压力变化处的阀门，+0.5 分", "chk_pfd_pressure"),
        Item("2.1.5", "有物流编号及操作条件（温度、压力、流量）", 0.5, "pfd",
             "有物流编号及操作条件（温度、压力、流量），+0.5 分", "chk_pfd_stream_ann"),
        Item("2.2.1", "物流表·有物流号", 0.2, "pfd", "有物流号，+0.2 分", "chk_stream_table"),
        Item("2.2.2", "物流表·物流号完整", 0.5, "pfd", "物流号完整，+0.5 分",
             "chk_stream_table"),
        Item("2.2.3", "物流表·质量流量和质量分率", 0.2, "pfd",
             "有各物料的质量流量和质量分率，+0.2 分", "chk_stream_table"),
        Item("2.2.4", "物流表·密度和体积流量", 0.2, "pfd", "有密度和体积流量，+0.2 分",
             "chk_stream_table"),
        Item("2.2.5", "物流表·操作参数（温度、压力等）", 0.2, "pfd",
             "有操作参数（温度、压力等），+0.2 分", "chk_stream_table"),
        Item("2.2.6", "物流表·相态及相态分率", 0.2, "pfd",
             "有相态（气、液、固）及相态分率，+0.2 分", "chk_stream_table"),
        Item("2.3.1", "设备图标处标示有设备位号", 0.5, "pfd",
             "设备图标处标示有设备位号，+0.5 分", "chk_tag_at_symbol"),
        Item("2.3.2", "图中顶部或底部标示设备位号和对应设备名称", 0.5, "pfd",
             "图中顶部或底部标示有设备位号和对应的设备名称，+0.5 分",
             "chk_tag_index_table"),
    ]

    return it


# ---------------------------------------------------------------------------
# 三、几何小工具
# ---------------------------------------------------------------------------

def _rects_of(cd: E.ChemDrawing, layer: str) -> List[Rect]:
    """该图层上的所有包围盒。

    ⚠️ 必须**同时**扫 group 与散实体：图框/说明框是散实体，设备/阀门才是 group。
    第一版只扫 group 并读 `g.layer`——而 `Group` 根本没有 `layer` 属性
    （图层在各 `g.ents[i].layer` 上），于是"图上明明有图框"被判成没有。
    """
    want = resolve_layer(layer)
    out: List[Rect] = []
    pts: List[Pt] = []
    for e in cd.iter_ents():
        if resolve_layer(str(e.layer)) != want:
            continue
        try:
            pts.extend(e.points())
        except Exception:
            pass  # 该实体无点集 / 取点失败 → 跳过，不影响其余
    if pts:
        r = E.rect_of_points(pts)
        if r:
            out.append(r)
    for g in cd.iter_groups():
        r = E.group_rect(g, layers=(want,))
        if r:
            out.append(r)
    return out


def _border_rect(cd: E.ChemDrawing) -> Optional[Rect]:
    """**图框本体**的矩形＝BORDER 图层上面积最大的**闭合**多段线。

    ⚠️ 不能拿 BORDER 图层的全局包围盒顶上：分区代号（1..n / A..H）是按标准画在
    图框**外面**的，把它们的点算进来，得到的"图框"会比真实边框大一圈
    （实测左 20 / 右 0 / 下 0 / 上 3，而真实内缩是 25/5/5/5），
    于是"装订边 25mm"这条判据被自己的量法搞错。
    """
    best: Optional[Rect] = None
    best_area = -1.0
    for e in cd.iter_ents():
        if resolve_layer(str(e.layer)) != "BORDER":
            continue
        if not isinstance(e, Polyline) or not bool(getattr(e, "closed", False)):
            continue
        r = E.rect_of_points(list(e.pts))
        if not r:
            continue
        area = (r[2] - r[0]) * (r[3] - r[1])
        if area > best_area:
            best, best_area = r, area
    if best is not None:
        return best
    rs = _rects_of(cd, "BORDER")
    return max(rs, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])) if rs else None


def _texts(cd: E.ChemDrawing) -> List[Text]:
    return [t for t in cd.iter_ents()
            if isinstance(t, Text) and str(t.text).strip()]


def _sheets() -> Dict[str, Tuple[float, float]]:
    return dict(SHEET_SIZES)


def _bounds(cd: E.ChemDrawing) -> Optional[Rect]:
    """**工艺内容**的包围盒（用于"内容够不够少 → 该用哪张幅面"这类判断）。

    两条口径都必须写清，因为这里踩过两次：

    1. **不能只扫 Group**。布置图（`chemcad_plotplan`）的设备占位是**普通多段线**，
       不构成 Group；旧版只扫 `iter_groups()`，于是 `_bounds()` 返回 None，
       把"图框存在""图幅合规"**误判成不得分**。判据看不见的几何会被说成不存在。
    2. **但也不能把所有实体都算进来**。图框/标题栏（BORDER）、标准表格（TABLE）、
       说明框（MISC）都是**跟着幅面走的家具**——它们按幅面摆好，不是"内容撑破幅面"。
       把它们算进去会量出 0.86~0.96 的利用率，反过来把本该在 A2 的图判成"该用 A1"。
       排掉这三层后是 0.33 —— 这才是"内容比较少"的真实读数。
    """
    furniture = {"BORDER", "TABLE", "MISC"}
    xs: List[float] = []
    ys: List[float] = []
    for g in cd.iter_groups():
        r = E.group_rect(g)
        if r:
            xs += [r[0], r[2]]
            ys += [r[1], r[3]]
    for e in cd.iter_ents():
        if resolve_layer(str(e.layer)) in furniture:
            continue
        try:
            for x, y in e.points():
                xs.append(float(x))
                ys.append(float(y))
        except Exception:
            continue  # 点坐标非数值 → 跳过该点
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _title_block(cd: E.ChemDrawing, ctx: Optional[Dict] = None) -> Dict[str, str]:
    """标题栏字段。优先 meta，其次调用方从**规格**递进来。

    规格里明明写着 `title_block`，但引擎没把它登记进 meta——第一版只看 meta，
    于是"有项目名称"被判成没有。判据不该因为"字段放在哪儿"而误判：
    只要图纸的来源材料里有，就算有。
    """
    tb: Dict[str, str] = {}
    src = (ctx or {}).get("title_block")
    if isinstance(src, dict):
        tb.update({str(k): str(v) for k, v in src.items()})
    try:
        meta_tb = (cd.meta or {}).get("title_block") or {}
        if isinstance(meta_tb, dict):
            tb.update({str(k): str(v) for k, v in meta_tb.items()})
    except Exception:
        pass  # 未提供 title_block 元数据 → 用已收集条目
    return tb




def _used_layers(cd: E.ChemDrawing) -> List[str]:
    try:
        return [resolve_layer(str(x)) for x in cd.used_layers()]
    except Exception:
        return sorted({resolve_layer(str(e.layer)) for e in cd.iter_ents()})


# ---------------------------------------------------------------------------
# 四、各条检查器：签名统一 (cd, ctx) -> Result
# ---------------------------------------------------------------------------

def _mk(item: Item, got: float, evidence: str, hint: str = "") -> Result:
    if got >= item.points - 1e-9:
        v = "得分"
    elif got > 0:
        v = "部分"
    else:
        v = "不得分"
    return Result(item, round(got, 2), v, evidence, hint)


def chk_border(cd, ctx) -> Result:
    it = ctx["item"]
    r = _border_rect(cd)
    b = _bounds(cd)
    if r is None or not b:
        return _mk(it, 0.0, "未找到 BORDER 图层上的闭合图框",
                  "先画图框（draw_pfd 应自动生成）")
    return _mk(it, it.points,
               f"图框 {r[0]:.0f},{r[1]:.0f}~{r[2]:.0f},{r[3]:.0f}；"
               f"图面范围 {b[0]:.0f},{b[1]:.0f}~{b[2]:.0f},{b[3]:.0f}")


def chk_border_std(cd, ctx) -> Result:
    it = ctx["item"]
    name = str(cd.sheet).upper()
    size = _sheets().get(name)
    if size is None:
        return _mk(it, 0.0, f"图幅 {name} 不在 GB/T 14689 常用幅面内",
                  f"改用 {sorted(_sheets())}")
    outer = _border_rect(cd)
    if outer is None:
        return _mk(it, 0.0, "无图框，无法核边框内缩")
    left = outer[0]
    right = size[0] - outer[2]
    bottom = outer[1]
    top = size[1] - outer[3]
    need = EDGE_MARGIN.get(name, 5.0)
    has_bind = any(abs(v - BIND_MARGIN) <= 3.0 for v in (left, right, bottom, top))
    ok_others = all(v >= need - 1.0 for v in (left, right, bottom, top))
    ev = (f"幅面 {name}({size[0]:.0f}×{size[1]:.0f}) 内缩 "
          f"左{left:.1f} 右{right:.1f} 下{bottom:.1f} 上{top:.1f}；"
          f"GB/T 14689：装订边 25 或无装订边 {need:.0f}")
    if ok_others and has_bind:
        return _mk(it, it.points, ev)
    return _mk(it, 0.0, ev, "图框需留装订边 25mm（或按幅面无装订边 10/5mm）")


def chk_titleblock(cd, ctx) -> Result:
    it = ctx["item"]
    tb = _title_block(cd, ctx)
    if tb:
        return _mk(it, it.points, f"标题栏字段 {len(tb)} 项")
    if _rects_of(cd, "BORDER"):
        return _mk(it, it.points, "有图框（标题栏未登记字段，按图框存在计）")
    return _mk(it, 0.0, "无标题栏")


def chk_tb_project(cd, ctx) -> Result:
    it = ctx["item"]
    v = str(_title_block(cd, ctx).get("项目名称", "")).strip()
    return (_mk(it, it.points, f"项目名称={v!r}") if v
            else _mk(it, 0.0, "无项目名称", "title_block.项目名称"))


def chk_tb_team_and_review(cd, ctx) -> Result:
    it = ctx["item"]
    tb = _title_block(cd, ctx)
    team = str(tb.get("竞赛队名称", tb.get("设计单位", ""))).strip()
    d = str(tb.get("设计", "")).strip()
    r = str(tb.get("审核", "")).strip()
    if not d:
        d = str(tb.get("设计 / 制图", "")).split("/")[0].strip()
    if not r:
        chk = str(tb.get("校对 / 审核", ""))
        if "/" in chk:
            parts = [p.strip() for p in chk.split("/")]
            r = parts[-1] if len(parts) > 1 else ""
        else:
            r = chk.strip()
    if not d:
        d = str(tb.get("设计 / 制图", "")).split("/")[0].strip()
    problems = []
    if not team:
        problems.append("缺竞赛队名称")
    if not d or not r:
        problems.append(f"设计/审核未署名（设计={d!r} 审核={r!r}）")
    elif d == r:
        problems.append(f"设计与审核是同一人（{d}）")
    if problems:
        return _mk(it, 0.0, "；".join(problems),
                  "标题栏补『竞赛队名称』，且设计、审核必须由不同人员承担")
    return _mk(it, it.points, f"竞赛队={team!r} 设计={d!r} 审核={r!r}")


def chk_legend(cd, ctx) -> Result:
    it = ctx["item"]
    txts = [str(t.text) for t in _texts(cd)]
    joined = " ".join(txts).lower()
    has = any(k in joined for k in ("图例", "legend", "symbol", "符号说明", "图形符号"))
    equipment = [g for g in cd.iter_groups() if g.kind == "equipment"]
    if not has and equipment:
        import chemcad_block_library as BL
        try:
            for g in equipment:
                if g.meta.get("geometry_source") != "library_block":
                    raise ValueError("设备符号没有图库来源")
                BL.source({"block_file":g.meta["library_file"],"block_name":g.meta["library_block"]})
        except (ValueError, KeyError, RuntimeError) as exc:
            return _mk(it,0.0,f"设备图形来源未通过核对：{exc}","核对设备类型与已确认的图库块")
        return _mk(it,it.points,f"{len(equipment)} 个设备符号调用已登记图库块；按用户规则不生成图例框")
    if not has:
        return _mk(it, 0.0, "未找到可核对的设备图形来源", "核对设备与已确认的图库块")
    n = sum(1 for g in cd.iter_groups()
            if str(getattr(g, "kind", "")) == "equipment")
    return _mk(it, it.points, f"有图例；设备符号 {n} 类")


def chk_lineweights(cd, ctx) -> Result:
    it = ctx["item"]
    used = set(_used_layers(cd))
    bad: List[str] = []
    detail: List[str] = []

    def mm(lay: str) -> Optional[float]:
        ld = LAYER_REGISTRY.get(lay)
        return None if ld is None else ld.lineweight / 100.0

    def check(lay: str, lo: float, hi: float, label: str) -> None:
        if lay not in used:
            return
        v = mm(lay)
        if v is None:
            return
        detail.append(f"{lay}={v:.2f}")
        if not (lo - 1e-9 <= v <= hi + 1e-9):
            bad.append(f"{label} {lay}={v:.2f} 应在 {lo}~{hi}")

    check("PIPE_PRI", *W_PRI_PIPE, "主物料管道")
    check("PIPE_INLINE", *W_OTHER_PIPE, "其他物料管道")
    # 其他（细）：这些都在 0.15~0.25 内即可
    for lay in ("FLOWARROW", "SIGNAL", "仪表线", "INST", "TAG", "仪表",
                "DIM", "CENTERLINE", "MISC", "HATCH", "EQUIP_IN"):
        check(lay, *W_OTHER, "其他")
    # 设备、机器轮廓线：标准备注点名 0.25
    for lay in ("EQUIP",):
        if lay in used:
            v = mm(lay)
            detail.append(f"{lay}={v:.2f}" if v is not None else f"{lay}=?")
            if v is None or abs(v - W_EQUIP_OUTLINE) > 1e-9:
                bad.append(
                    f"设备轮廓线 {lay}={v:.2f}mm，标准（表 6.1.3 备注）要求 0.25mm")
    if bad:
        return _mk(it, 0.0, "；".join(bad[:3]), "按 HG/T 20519-2009 表 6.1.3 调整线宽")
    return _mk(it, it.points, "；".join(detail[:6]) or "无相关图层")


def chk_text_heights(cd, ctx) -> Result:
    it = ctx["item"]
    txts = _texts(cd)
    if not txts:
        return _mk(it, 0.0, "图上无文字")
    tb = _title_block(cd, ctx)
    dwname = str(tb.get("名称", "")).strip()
    bad: List[str] = []
    seen: List[str] = []

    # 1) 图名 == 7mm（表 6.2.2「图名 7」）
    if dwname:
        hit = [t for t in txts if str(t.text).strip() == dwname]
        if hit:
            h = max(float(t.height) for t in hit)
            seen.append(f"图名={h:.1f}")
            if abs(h - H_DWG_NAME) > 0.6:
                bad.append(f"图名 {h:.1f}mm，标准 7mm")
        else:
            bad.append("图名文字未找到")

    # 2) 数字及字母（位号/管号）落 2~3mm
    tagh = {round(float(t.height), 2) for t in txts
            if resolve_layer(str(t.layer)) == "TAG"}
    if tagh:
        lo, hi = min(tagh), max(tagh)
        seen.append(f"位号字高 {sorted(tagh)}")
        if lo < H_DIGIT[0] - 0.6 or hi > H_DIGIT[1] + 0.6:
            bad.append(f"位号/管号字高 {sorted(tagh)} 超出 2~3mm")

    # 3) 文字说明（说明框）落 5mm
    note_layers = {"MISC"}
    noteh = {round(float(t.height), 2) for t in txts
             if resolve_layer(str(t.layer)) in note_layers}
    body = [h for h in noteh if h >= 2.5]
    if body:
        seen.append(f"说明字高 {sorted(body)}")
        worst = min(body)
        if abs(worst - H_NOTE) > 0.6:
            bad.append(f"图中文字说明 {worst:.1f}mm，标准 5mm"
                       "（若确为表格且格高<6mm 才可用 3mm）")
    if bad:
        return _mk(it, 0.0, "；".join(bad[:3]), "按 HG/T 20519-2009 表 6.2.2 调整字高")
    return _mk(it, it.points, "；".join(seen[:3]) or "字高未发现冲突")


# ---- PFD 内容 --------------------------------------------------------------

def _pfd_ann(cd) -> List[str]:
    """把图上所有注记文字摊平，供 PFD 内容类判据查关键词。"""
    return [str(t.text) for t in _texts(cd)]


def chk_pfd_io(cd, ctx) -> Result:
    it = ctx["item"]
    txt = " ".join(_pfd_ann(cd))
    has_off = any(k in txt for k in ("界区", "进料", "产品", "出料", "原料"))
    return (_mk(it, it.points, "进出口有注记（进料/产品/界区）") if has_off
            else _mk(it, 0.0, "未找到物料进出口注记", "在界区处标进料/产品"))


def chk_pfd_equip_io(cd, ctx) -> Result:
    it = ctx["item"]
    n_eq = sum(1 for g in cd.iter_groups()
               if str(getattr(g, "kind", "")) == "equipment")
    # 设备进出口物料：由管线条数相对设备数体现；每个设备至少 1 条接管
    n_pipe = 0
    try:
        n_pipe = len(getattr(cd, "pipes", {}) or {})
    except Exception:
        pass  # 无 pipes 集合 → 记 0（评分不因缺字段崩）
    if n_eq and n_pipe >= n_eq:
        return _mk(it, it.points, f"{n_eq} 台设备 / {n_pipe} 条管线")
    return _mk(it, 0.0, f"设备 {n_eq} 台但管线仅 {n_pipe} 条",
               "确认每台设备进出口都有物料线")


def chk_pfd_arrows(cd, ctx) -> Result:
    it = ctx["item"]
    n = sum(1 for g in cd.iter_groups()
            if resolve_layer(str(getattr(g, "layer", ""))) == "FLOWARROW")
    ent = sum(1 for e in cd.iter_ents()
              if resolve_layer(str(e.layer)) == "FLOWARROW")
    tot = n + ent
    return (_mk(it, it.points, f"流向箭头 {tot} 处") if tot
            else _mk(it, 0.0, "无流向箭头", "管线上加箭头（规格 pipes[].arrows）"))


def chk_pfd_pressure(cd, ctx) -> Result:
    it = ctx["item"]
    txt = " ".join(_pfd_ann(cd))
    has_valve = any(k in txt for k in ("阀", "HV", "FV", "PV", "TV"))
    n_valve = 0
    try:
        n_valve = len(getattr(cd, "valves", {}) or {})
    except Exception:
        pass  # 无 valves 集合 → 记 0（同上）
    if has_valve or n_valve:
        return _mk(it, it.points, f"阀门注记存在（{n_valve} 处阀门对象）")
    return _mk(it, 0.0, "未找到表示压力变化的阀门", "在压力变化处画调节阀并注记")


def chk_pfd_stream_ann(cd, ctx) -> Result:
    """物流编号 + 操作条件（温度、压力、流量）是否逐条注出。

    注记用**流股号本身**核对（PFD 的流股号是裸数字），而不是去文字里搜"物流"二字——
    第一版搜关键词，图上明明注着 `1 / T 40 ℃ / P 0.45 MPa(G) / W 12 500 kg/h`，
    却因为没写"物流"两个字被判成没有。
    """
    it = ctx["item"]
    txts = {str(t.text).strip() for t in _texts(cd)}
    joined = " ".join(txts)
    streams = ctx.get("streams") or {}
    nos = [str(v.get("no", "")).strip() for v in streams.values()
           if isinstance(v, dict) and str(v.get("no", "")).strip()]
    hit_no = [n for n in nos if n in txts] if nos else []
    pats = {"温度": ("℃", "°C"), "压力": ("MPa", "kPa"),
            "流量": ("kg/h", "t/h", "kmol/h", "m3/h")}
    hit = [k for k, ps in pats.items() if any(p in joined for p in ps)]
    ok_no = bool(hit_no) or (not nos)
    if ok_no and len(hit) == len(pats):
        return _mk(it, it.points,
                   f"物流号 {len(hit_no) or '—'}/{len(nos)} 条在图上；"
                   f"温度/压力/流量均有注记")
    miss = [k for k, ps in pats.items() if not any(p in joined for p in ps)]
    if not ok_no:
        miss.append(f"物流编号（规格 {len(nos)} 个流股号，图上只找到 {len(hit_no)}）")
    return _mk(it, 0.0, f"缺 {miss}", "管线旁注出流股号与 T/P/流量")


def chk_stream_table(cd, ctx) -> Result:
    """完整物流表（1.5 分，拆成 6 条）。

    ⚠️ 判据是**独立的物流表**，不是"管线旁逐条注记"。标准把"完整物流表"
    单列 1.5 分，要求一张表里逐列给出：物流号 / 质量流量与质量分率 /
    密度与体积流量 / 操作参数 / 相态与相态分率。
    """
    it = ctx["item"]
    txts = _pfd_ann(cd)
    joined = " ".join(txts)
    meta = cd.meta or {}
    tbl = bool(meta.get("stream_table"))
    external = meta.get("external_balance_panels") or []
    if external:
        tbl = True
    # 按工段分框的流股表同样算"物流表"——标准要的是**内容成表且齐全**，
    # 不是"必须只有一张"。分框反而更好读（每段的进出料自成一组）。
    # 判据认不出这一形态的话，"按工段分流股"就会被误判成"没有物流表"（实测踩过：
    # PFD 因此从 6.0 掉到 4.5）。
    sec = meta.get("section_stream_boxes") or {}
    if not tbl and sec:
        tbl = True
    code = it.code
    if code in ("2.2.1", "2.2.2"):
        if tbl:
            if external:
                n = sum(len(panel.get("streams",[])) for panel in external)
                return _mk(it,it.points,f"独立物料表存在（外置单框），共 {n} 个流股号")
            n = int((meta.get("stream_table") or {}).get("rows", 0)) \
                or sum(int(v.get("rows", 0)) for v in sec.values())
            kind = "整表" if meta.get("stream_table") else f"按工段分 {len(sec)} 框"
            return _mk(it, it.points, f"物流表存在（{kind}），共 {n} 个流股号")
        return _mk(it, 0.0, "未找到物流表（整表与按工段分框都没有）",
                   "补一张物流表：首列为物流号，逐列给出各参数")
    field_map = {
        "2.2.3": (("质量流量", ("kg/h", "t/h")), ("质量分率", ("%", "w%", "wt"))),
        "2.2.4": (("密度", ("kg/m3", "kg/m³")), ("体积流量", ("m3/h", "m³/h"))),
        "2.2.5": (("温度", ("℃",)), ("压力", ("MPa", "kPa"))),
        "2.2.6": (("相态", ("气", "液", "固")), ("相态分率", ("%", "分率"))),
    }
    need = field_map.get(code)
    if not need:
        return _mk(it, 0.0, "未知条目")
    if not tbl:
        return _mk(it, 0.0, "无物流表，无从取该项",
                   "先建物流表，再逐列补该参数")
    miss = [name for name, keys in need if not any(k in joined for k in keys)]
    if miss:
        return _mk(it, 0.0, f"物流表缺 {miss}", f"物流表补列：{miss}")
    return _mk(it, it.points, f"{'、'.join(n for n, _ in need)} 均列出")


def chk_tag_at_symbol(cd, ctx) -> Result:
    it = ctx["item"]
    eq = [g for g in cd.iter_groups()
          if str(getattr(g, "kind", "")) == "equipment"]
    if not eq:
        return _mk(it, 0.0, "图上无设备符号")
    labels = set()
    for t in _texts(cd):
        labels.add(str(t.text).strip())
    ok = 0
    for g in eq:
        tag = None
        try:
            tag = E.obj_label(g)
        except Exception:
            pass  # 取不到位号 → 记为无位号（评分据此扣分，不崩）
        if tag and str(tag) in labels:
            ok += 1
    if ok == len(eq):
        return _mk(it, it.points, f"{ok}/{len(eq)} 台设备符号处均有位号")
    return _mk(it, 0.0, f"仅 {ok}/{len(eq)} 台设备处找到位号",
               "每台设备旁标设备位号")


def chk_tag_index_table(cd, ctx) -> Result:
    """顶部或底部的对应位号/名称标注；无需集中设备位号表。"""
    it = ctx["item"]
    footers = [g for g in cd.iter_groups() if g.name == 'EQUIPMENT_FOOTER']
    if footers:
        from collections import Counter
        wanted = Counter(str(n) for n in (ctx.get('equip_names') or []) if str(n).strip())
        actual = Counter(str(t.text) for g in footers for t in g.ents
                         if isinstance(t, Text) and str(t.text) in wanted)
        if wanted and not (wanted-actual) and not E.validate_equipment_label_positions(cd):
            return _mk(it, it.points, f'底部{len(footers)}组位号/名称同X，位号中心距框底60mm（版面坐标校核）')
        return _mk(it, 0., '底部设备标注缺项或位置不符', '补齐底部分数式位号/名称并复核对应关系')
    tbl = (cd.meta or {}).get("tag_index_table")
    if tbl:
        return _mk(it, it.points,
                   f"位号表 {tbl.get('rows', '?')} 行（含位号与设备名称）")
    names = [str(n) for n in (ctx.get("equip_names") or []) if str(n).strip()]
    if not names:
        return _mk(it, 0.0, "没有设备名称清单可比对（规格 equipment[].name 为空）",
                   "在图底部标注对应设备位号与设备名称")
    size = _sheets().get(str(cd.sheet).upper())
    if not size:
        return _mk(it, 0.0, "幅面未知，无法判断顶部/底部")
    band = size[1] * 0.14                      # 顶部/底部各留 14% 带
    top = 0
    bot = 0
    for t in _texts(cd):
        s = str(t.text).strip()
        if s not in names:
            continue
        y = float(t.pt[1])
        if y >= size[1] - band:
            top += 1
        elif y <= band:
            bot += 1
    # 汇总表要有"多台设备"成群出现才成立
    if max(top, bot) >= 3:
        return _mk(it, it.points,
                   f"{'顶部' if top >= bot else '底部'}成组出现 {max(top, bot)} 条设备名称")
    return _mk(it, 0.0,
               f"设备名称共 {len(names)} 项，但图顶部 {top} / 底部 {bot} 条"
               "（未成表）",
               "在图底部标注对应设备位号与设备名称")


# ---- P&ID 内容 —— 已整段移除（评分表改纯 PFD 口径）-------------------------
# 原 3.1.1~3.1.4（控制方案）、3.2.1~3.2.6（管道组合号）均为 P&ID 条款；
# 连同其判据 `_loop_check` / `chk_loop_*` / `_pipe_parts` / `_pipe_no_check` /
# `chk_pipe_no` / `chk_pipe_insulation` 及 helper `_loops` / `_equip_type_of`
# 一并删除——它们只服务 P&ID 条款，评分表已不含这些条目。


#: 判据注册表。**纯 PFD 口径**：只留 PFD 条款用到的判据（P&ID / 布置图判据已删）。
CHECKERS: Dict[str, Callable] = {
    "chk_border": chk_border,
    "chk_border_std": chk_border_std,
    "chk_titleblock": chk_titleblock,
    "chk_tb_project": chk_tb_project,
    "chk_tb_team_and_review": chk_tb_team_and_review,
    "chk_legend": chk_legend,
    "chk_lineweights": chk_lineweights,
    "chk_text_heights": chk_text_heights,
    "chk_pfd_io": chk_pfd_io,
    "chk_pfd_equip_io": chk_pfd_equip_io,
    "chk_pfd_arrows": chk_pfd_arrows,
    "chk_pfd_pressure": chk_pfd_pressure,
    "chk_pfd_stream_ann": chk_pfd_stream_ann,
    "chk_stream_table": chk_stream_table,
    "chk_tag_at_symbol": chk_tag_at_symbol,
    "chk_tag_index_table": chk_tag_index_table,
}


# ---------------------------------------------------------------------------
# 五、对外接口
# ---------------------------------------------------------------------------

def score_drawing(cd: E.ChemDrawing, dtype: str = "pfd",
                  ctx_extra: Optional[Dict] = None) -> Dict[str, object]:
    """按标准给一张图打分（**纯 PFD 口径**：`rubric()` 只含 PFD 条款）。

    需要调用方递进来的"图纸外材料"（判据不能只看几何就能判准的）：
      * `title_block` —— 标题栏字段（规格里写的，可能没进 meta）
      * `equip_names` —— 设备名称清单
      * `streams`     —— 流股数据（物流表 / 流股注记判据用）
    """
    dtype = str(dtype).lower()
    ctx_base: Dict[str, object] = dict(ctx_extra or {})
    results: List[Result] = []
    for item in rubric():
        if not item.checker:
            results.append(Result(item, 0.0, "需人工判定", item.basis, ""))
            continue
        fn = CHECKERS.get(item.checker)
        if fn is None:
            results.append(Result(item, 0.0, "需人工判定", item.basis,
                                  f"未实现 {item.checker}"))
            continue
        ctx = dict(ctx_base)
        ctx["item"] = item
        try:
            results.append(fn(cd, ctx))
        except Exception as exc:                     # 判据不该因异常静默归零
            results.append(Result(item, 0.0, "判定失败",
                                  f"{type(exc).__name__}: {exc}",
                                  "修检查器，别把异常当不得分"))
    got = sum(r.got for r in results)
    full = sum(r.item.points for r in results)
    return {
        "drawing_type": dtype,
        "sheet": str(getattr(cd, "sheet", "")),
        "items": results,
        "got": round(got, 2),
        "full": round(full, 2),
        "rate": round(got / full, 3) if full else 0.0,
    }


def format_score(sc: Dict[str, object]) -> str:
    lines = [f"《工程图纸质量》专项评审 自评分（{sc['drawing_type']} / {sc['sheet']}）",
             f"  PFD 口径：{sc['got']} / {sc['full']} 分"
             f"（得分率 {float(sc['rate'])*100:.0f}%）",
             "  评分表为**纯 PFD 口径**（标准 20 分中属 PFD 的 6 分）；"
             "P&ID / 车间与设备布置 / 总平面等条款不在范围内，既不假报也不占分母",
             ""]
    fails = [r for r in sc["items"] if r.verdict in ("不得分", "部分")]  # type: ignore[union-attr]
    for r in sc["items"]:                                               # type: ignore[union-attr]
        ri: Result = r                                                   # type: ignore[assignment]
        mark = {"得分": "✓", "部分": "△", "不得分": "✗",
                "需人工判定": "?", "判定失败": "!"}.get(ri.verdict, "?")
        lines.append(f"  {mark} [{ri.item.code}] {ri.item.title}"
                     f"  {ri.got}/{ri.item.points}")
        if ri.verdict != "得分":
            if ri.evidence:
                lines.append(f"        实测：{ri.evidence[:150]}")
            if ri.hint:
                lines.append(f"        建议：{ri.hint[:150]}")
    if fails:
        lines.append("")
        lines.append(f"  共 {len(fails)} 项未拿满："
                     + "；".join(f"{r.item.code} {r.item.title}" for r in fails[:8]))  # type: ignore[union-attr]
    else:
        lines.append("")
        lines.append("  ✓ 范围内条目全部拿满")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="按《工程图纸质量》专项评审评分标准给图纸自评分")
    ap.add_argument("spec", help="规格 JSON")
    ap.add_argument("--drawing-type", default="pfd", choices=("pfd",),
                    help="pfd=工艺物料流程图（唯一口径）。评分表已改纯 PFD："
                         "P&ID / 车间与设备布置 / 总平面等条款均不在评分表内")
    ap.add_argument("--json", help="把评分明细写入该文件")
    a = ap.parse_args(list(argv) if argv is not None else None)

    import draw_pfd as D
    with open(a.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)

    types = (a.drawing_type,)
    payload = {}
    rc = 0
    for t in types:
        sp = D.spec_for_type(spec, t)
        cd, _loops = D.build(sp)          # loops 只服务 P&ID 判据（已删），不再入 ctx
        ctx = {
            "title_block": sp.get("title_block") or {},
            "equip_names": [str(e.get("name", "")) for e in (sp.get("equipment") or [])],
            "streams": sp.get("streams") or {},
        }
        sc = score_drawing(cd, t, ctx)
        print(format_score(sc))
        print()
        payload[t] = {k: v for k, v in sc.items() if k != "items"}
        payload[t]["items"] = [
            {"code": r.item.code, "title": r.item.title, "points": r.item.points,
             "scope": r.item.scope, "basis": r.item.basis,
             "got": r.got, "verdict": r.verdict,
             "evidence": r.evidence, "hint": r.hint}
            for r in sc["items"]]                                        # type: ignore[union-attr]
        if float(sc["got"]) < float(sc["full"]) - 1e-9:
            rc = 1
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"明细已写入 {a.json}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
