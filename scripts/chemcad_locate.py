#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_locate.py —— 图纸「对象定位」与「信息提取」的统一模块。

## 这个模块解决什么

出图（chemcad_engine）是**正向**：方案 → 图。本模块是**逆向**：图 / 模型文件 → 结构化信息。
两条腿共用同一套坐标与图层约定（`chemcad_ir`），所以"提取出来的东西"能直接喂回出图流程。

## 能力清单（每条都标了出处，便于回溯）

| 能力 | 函数 | 出处 |
|---|---|---|
| 编码鲁棒读取（utf-8 → gbk → latin-1） | `read_text_robust` | PfdViewer `parse_bkp` 的 latin-1 读法 + 本技能经验库「.bkp 是 GBK」 |
| 多页/多作用域切分（同名不撞车） | `split_dxf_scopes` / `parse_bkp_scopes` | PfdViewer `_pfs_sections`/`_pfs_range`（一个 bkp 存多页 PFD） |
| 同 id 去重（留"更完整"的那份，保序） | `dedupe_keep_best` | PfdViewer `_dedupe` |
| 折线首尾相接合并（一条管被拆成多段） | `merge_paths` | PfdViewer `merge_polys` |
| 依拓扑校正折线方向 | `orient_path` | PfdViewer `orient_poly` |
| 端口反推符号尺寸（中位数抗噪 + 全场基准收敛） | `infer_symbol_sizes` | PfdViewer `port_pts`/`est`/收敛段 |
| 把线端裁到真实轮廓（圆/矩形/多边形） | `clip_to_outline` | PfdViewer `_clip_circle`/`_clip_rect`/`_clip_poly` |
| 点拾取（定位"这个点上是哪个对象"） | `pick_at` | PfdViewer `FlowViewerApp._on_click` |
| 文字列扫描排序（还原图纸阅读顺序） | `order_column_scan` | dwg-text-extractor `format_text_output` |
| 对象间距测量 | `closest_distance` / `point_distance` | cad-viewer `distance` 命令 |
| Aspen .bkp 模块/流股/工艺结果提取 | `parse_bkp_case` | PfdViewer 全套（含 `RES_STR` 结果段） |

## 与既有脚本的关系（分工，别重叠）

- `inspect_dxf.py`：**轻量罗列**（图层 / 块 / 实体计数 / 文字样例），一句话回答"这张图里有什么"。
- 本模块：**定位与提取**——按作用域解析到**实体级**（含坐标、bbo、连通性），
  并能做几何推理（尺寸反推、折线重建、轮廓裁切、拾取、测距）。
- `read_dwg.py`：读**运行中的 AutoCAD**（COM）；本模块读**落盘文件**（DXF / bkp）。

## 设计纪律（沿用本技能既有铁律）

1. **零依赖**：只用标准库，任何 Python 3 环境可跑（ezdxf 可选，只在显著更省事时才用）。
2. **不猜**：解析不出的东西**报出来**（`unresolved`），不静默编造。
3. **可比对**：每个提取结果都带 `source` / `scope`，便于回溯到原文件位置。

## CLI

    python chemcad_locate.py bkp      <case.bkp>  [--json out.json]
    python chemcad_locate.py scopes   <file.dxf>  [--json out.json]
    python chemcad_locate.py locate   <file.dxf>  --tag T-101
    python chemcad_locate.py pick     <file.dxf>  --at 120,85
    python chemcad_locate.py texts    <file.dxf>  [--order column] [--json out.json]
    python chemcad_locate.py distance <file.dxf>  --a T-101 --b T-102
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Pt = Tuple[float, float]

# ===========================================================================
# 一、编码鲁棒读取
# ===========================================================================

#: 逐级降级顺序。latin-1 永不失败（逐字节映射），是**兜底而不是首选**：
#: 它能把 GBK/混合编码的文件"读进来不炸"，但中文会失真，所以只在前两级失败时才用。
_ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk", "latin-1")


def read_text_robust(path: str) -> Tuple[str, str]:
    """逐级降级读文本，返回 `(text, encoding_used)`。

    为什么不用 `errors="replace"` 一把梭：replace 会把**无法解码的字节悄悄换成 U+FFFD**，
    于是"文件是 GBK、按 UTF-8 读"这种最常见的错配不再报错，正则也照跑不误，
    只是所有中文都成了问号——**静默失真**比报错难查得多。

    逐级降级的要点：
    * `utf-8-sig` 放最前：吞掉 BOM，否则第一个字段会带 `\\ufeff` 前缀而匹配不上；
    * `gbk` 在 utf-8 之后：中文工程文件（含 Aspen `.bkp`）大量是 GBK；
    * `latin-1` 放最后：**逐字节映射，永不抛异常**，保证"任何文件都能读进来"，
      且 round-trip 无损（能原样写回），代价是中文显示为乱码——用返回值里的
      `encoding` 告诉调用方"这层是兜底，中文不可信"。
    """
    raw = open(path, "rb").read()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue  # 该档编码解码失败 → 试下一档（utf-8→gbk→latin-1 降级链）
    # 理论上到不了这里（latin-1 对任何字节序列都成立）
    return raw.decode("latin-1", errors="replace"), "latin-1+replace"


def is_lossy(encoding: str) -> bool:
    """这次读取是否只是"兜底读进来"（中文不可信）。"""
    return encoding.startswith("latin-1")


# ===========================================================================
# 二、DXF 作用域切分：为什么必须切
# ===========================================================================
#
# 一个 DXF 里，**同名对象会出现在多个作用域里**：
#   * `ENTITIES` 段 = 模型空间（真正的图面）
#   * `BLOCKS` 段里每个 `BLOCK` = 一块**符号定义**，内部又可能含 INSERT / TEXT
#   * `*Model_Space` / `*Paper_Space` 也是块，承载布局视口内容
#
# 于是"图面上有 T-101"和"块定义内部也有个 T-101"会同时被搜到。
# 不切作用域就把两份混着读，结果就是"图上出现两个、位置还对不上"——
# 这正是 PfdViewer 在 `.bkp` 上踩过的坑（一个 bkp 存多页 PFD：顶层一页 + 层级块自己一页，
# 同一个流股名撞车、图上出现两条线）。**同一个病，同一个解法：先切作用域，再取对象。**


def _dxf_pairs(text: str) -> List[Tuple[str, str]]:
    """把 DXF 拆成 (code, value) 序列。DXF 是严格的"一行 code、一行 value"。"""
    lines = text.splitlines()
    out: List[Tuple[str, str]] = []
    for i in range(0, len(lines) - 1, 2):
        out.append((lines[i].strip(), lines[i + 1].rstrip("\r")))
    return out


_SCOPE_MODEL = "MODEL"
_SCOPE_BLOCK = "BLOCK"
_SCOPE_TABLE = "TABLE"
_SCOPE_OTHER = "OTHER"


def split_dxf_scopes(pairs: Sequence[Tuple[str, str]]) -> List[Dict[str, object]]:
    """把 DXF 的 (code,value) 序列切成作用域。

    返回列表，每项：
        {"scope": "MODEL" | "BLOCK:<块名>" | "TABLE:<表名>" | "OTHER:<段名>",
         "kind":  "MODEL" | "BLOCK" | "TABLE" | "OTHER",
         "name":  块名/表名/段名,
         "start": 起 idx（含）, "end": 止 idx（不含）}

    说明：`MODEL` 取 `ENTITIES` 段；`BLOCK:*` 取其定义体（不含 BLOCK/ENDBLK 头尾）。
    块定义**全收**（含 `*Model_Space` 等匿名块）——匿名块里也有真几何，
    只是它们**不参与按名定位**（见 `is_anonymous_block`）。
    """
    scopes: List[Dict[str, object]] = []
    section: Optional[str] = None
    i, n = 0, len(pairs)
    blk_open: Optional[int] = None
    blk_name: Optional[str] = None
    tbl_open: Optional[int] = None
    tbl_name: Optional[str] = None

    while i < n:
        code, val = pairs[i]
        if code == "0":
            if val == "SECTION":
                # ⚠️ 段名紧随 SECTION：`0/SECTION` 的下**一对**就是 `2/<段名>`。
                # 写成 `pairs[i+2]` 会读到下一个实体的类型名（`BLOCK`/`INSERT`…），
                # 于是 `BLOCKS`/`ENTITIES` 一律认不出——作用域数静默变成 0，
                # 且不报错，只是"提取不到任何东西"。这是本模块自己踩过并钉住的坑。
                section = (pairs[i + 1][1].strip()
                           if i + 1 < n and pairs[i + 1][0] == "2" else "")
                # ENTITIES 段整体就是一个作用域；起点跳过 `0/SECTION` 与 `2/ENTITIES`
                if section == "ENTITIES":
                    end = _find_endsection(pairs, i)
                    scopes.append({"scope": _SCOPE_MODEL, "kind": _SCOPE_MODEL,
                                   "name": "ENTITIES", "start": i + 2, "end": end})
                    i = end
                    continue
                i += 1
                continue
            if val == "ENDSEC":
                section = None
                i += 1
                continue
            if val == "BLOCK" and section == "BLOCKS":
                blk_open, blk_name = i, None
                j = i + 1
                while j < n and pairs[j][0] != "0":
                    if pairs[j][0] == "2" and blk_name is None:
                        blk_name = pairs[j][1].strip()
                    j += 1
                i = j
                continue
            if val == "ENDBLK" and blk_open is not None:
                scopes.append({"scope": "%s:%s" % (_SCOPE_BLOCK, blk_name or ""),
                               "kind": _SCOPE_BLOCK, "name": blk_name or "",
                               "start": blk_open, "end": i})
                blk_open = blk_name = None
                i += 1
                continue
            if val == "TABLE" and section == "TABLES":
                tbl_open, tbl_name = i, None
                j = i + 1
                while j < n and pairs[j][0] != "0":
                    if pairs[j][0] == "2" and tbl_name is None:
                        tbl_name = pairs[j][1].strip()
                    j += 1
                i = j
                continue
            if val == "ENDTAB" and tbl_open is not None:
                scopes.append({"scope": "%s:%s" % (_SCOPE_TABLE, tbl_name or ""),
                               "kind": _SCOPE_TABLE, "name": tbl_name or "",
                               "start": tbl_open, "end": i})
                tbl_open = tbl_name = None
                i += 1
                continue
        i += 1

    return scopes


def _find_endsection(pairs: Sequence[Tuple[str, str]], start: int) -> int:
    for j in range(start + 1, len(pairs)):
        if pairs[j][0] == "0" and pairs[j][1].strip() == "ENDSEC":
            return j
    return len(pairs)


def is_anonymous_block(name: str) -> bool:
    """匿名块（`*Model_Space` / `*Paper_Space` / `*U123` 动态块 / `*D…` 标注块）。

    匿名块**不参与按名定位**：用户嘴里说的 "T-101" 绝不可能是 `*U123`。
    但它们的几何要参与"全图范围"统计——否则 bbox 会漏掉布局页里的内容。
    """
    return name.startswith("*")


# ---- DXF 实体解析 ---------------------------------------------------------

#: 与实体类型无关的通用码。**`40` 刻意不在这里**：它随实体类型变义
#: （TEXT/MTEXT 是字高、CIRCLE/ARC 是半径、LWPOLYLINE 是线宽），
#: 一刀切会把「半径」读成「字高」，于是圆的 bbox 全错、还查不出来。
_HEAD_CODES = {"5": "handle", "8": "layer", "2": "name", "1": "text",
               "70": "flags", "62": "color"}


def parse_scope_entities(pairs: Sequence[Tuple[str, str]],
                        sc) -> List[Dict[str, object]]:
    """解析一个作用域内的实体，返回结构化列表。

    每个实体：
        {"type","handle","layer","name","text","height","pts":[(x,y)…],"closed":bool,
         "center":(x,y)?"radius":float?,"bbox":(x0,y0,x1,y1)}

    * `pts` 只收**真实几何点**：LINE 收 2 点；LWPOLYLINE/POLYLINE 收全部顶点；
      CIRCLE/ARC 收"圆心"并另给 `center`/`radius`（bbox 按外接方给出）；
      TEXT/MTEXT/INSERT 收插入点 1 个。
    * 老式 `POLYLINE` 的顶点是**后续独立的 VERTEX 实体**（以 `SEQEND` 结束），
      这里一并收进来——不收的话老图（R12 及以前、不少院的老底图）全是空折线。
    """
    s, e = int(sc["start"]), int(sc["end"])  # type: ignore[arg-type]
    ents: List[Dict[str, object]] = []
    i = s
    while i < e:
        code, val = pairs[i]
        if code == "0" and val.strip() not in ("BLOCK", "ENDBLK", "TABLE", "ENDTAB", ""):
            etype = val.strip()
            j = i + 1
            while j < e and pairs[j][0] != "0":
                j += 1
            ent = _build_entity(etype, pairs[i:j])
            # 老式 POLYLINE：把后续 VERTEX 实体收成顶点，到 SEQEND 为止
            if etype == "POLYLINE":
                k = j
                while k < e:
                    if pairs[k][0] != "0":
                        k += 1
                        continue
                    sub = pairs[k][1].strip()
                    if sub == "VERTEX":
                        m = k + 1
                        while m < e and pairs[m][0] != "0":
                            m += 1
                        v = _build_entity("POINT", pairs[k + 1:m])
                        if v["pts"]:
                            ent["pts"].append(v["pts"][0])   # type: ignore[union-attr]
                        k = m
                        continue
                    break
                ent["bbox"] = _bbox_of(ent["pts"])          # type: ignore[arg-type]
            ents.append(ent)
            i = j
            continue
        i += 1
    return ents


def _bbox_of(pts: Sequence[Pt]) -> Tuple[float, float, float, float]:
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _build_entity(etype: str, body: Sequence[Tuple[str, str]]) -> Dict[str, object]:
    """按实体类型解释码值。**同一码值在不同实体上含义不同，必须分类型读。**"""
    ent: Dict[str, object] = {"type": etype, "handle": "", "layer": "0",
                              "name": "", "text": "", "height": 0.0,
                              "pts": [], "closed": False}
    verts: List[Pt] = []
    cur: Optional[List[float]] = None
    marker: Optional[str] = None      # 当前在收哪一个点：None/"P1"/"P2"
    radius = 0.0

    def flush() -> None:
        nonlocal cur, marker
        if cur and len(cur) >= 2:
            if marker == "P2":
                verts.append((cur[0], cur[1]))
            else:
                verts.append((cur[0], cur[1]))
        cur, marker = None, None

    for code, val in body:
        if code in _HEAD_CODES:
            key = _HEAD_CODES[code]
            if key == "flags":
                try:
                    if int(val) & 1:
                        ent["closed"] = True
                except ValueError:
                    pass  # 组码 70 非数值 → 按「未闭合」处理
            else:
                ent[key] = val.strip() if key != "text" else val
            continue
        if code in ("10", "11"):
            flush()
            cur = [float(val)] if _is_num(val) else None
            marker = "P1" if code == "10" else "P2"
            continue
        if code in ("20", "21") and cur is not None and len(cur) == 1:
            if _is_num(val):
                cur.append(float(val))
            else:
                cur = None
            continue
        if code == "40":
            if etype in ("CIRCLE", "ARC", "ELLIPSE"):
                if _is_num(val):
                    radius = float(val)
            elif etype in ("TEXT", "MTEXT"):
                if _is_num(val):
                    ent["height"] = float(val)
            # LWPOLYLINE 的 40 是线宽，与几何无关，忽略
            continue
        if code == "42":          # LWPOLYLINE 凸度：有凸度说明是圆弧段，顶点仍按折点收
            continue
    flush()

    if etype in ("LINE", "LWPOLYLINE", "POLYLINE", "SOLID", "3DFACE"):
        ent["pts"] = verts
    elif etype in ("CIRCLE", "ARC", "ELLIPSE"):
        c = verts[0] if verts else (0.0, 0.0)
        ent["center"] = c
        ent["radius"] = radius
        ent["pts"] = [(c[0] - radius, c[1] - radius), (c[0] + radius, c[1] + radius)]
    else:                          # TEXT / MTEXT / INSERT / POINT / VERTEX
        ent["pts"] = verts[:1]

    # 文字清洗：两层伪装都要剥（`\U+XXXX` 与 MTEXT 格式码），否则取到的是码不是字
    ent["text"] = strip_mtext_codes(unescape_dxf(str(ent["text"])))
    ent["name"] = unescape_dxf(str(ent["name"])).strip()

    if etype in ("TEXT", "MTEXT") and ent["pts"]:
        # 文字给**有面积**的 bbox（估宽），否则拾取/排序/测距在文字上全部退化
        ent["bbox"] = text_bbox(ent["pts"][0], str(ent["text"]),
                                float(ent["height"]))
    else:
        ent["bbox"] = _bbox_of(ent["pts"])   # type: ignore[arg-type]
    return ent


def _is_num(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


# ---- 文字清洗（与既有两个脚本共用同一套规则，别各写一套） -------------------
#
# DXF 里的文字有两层"伪装"，不剥掉就取不到真文字：
#   ① DXF 自身的转义：非 ASCII 常写成 `\U+XXXX`（`inspect_dxf.unesc` 的规则）；
#   ② MTEXT 内联格式码：`\fSimSun|b0|i0;`、`\C3;`、`\H2.5x;`、`\P`、`{}`，
#      以及 `%%d`/`%%c`/`%%p` 这三个老式特殊字符（`chemcad_engine.unformat_text` 的规则）。
# **同一个事实只在一处定义**：这里复用引擎的字宽约定，避免"提取出来的宽度"和
# "画出来的宽度"两套标准。

_UE_RE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_MT_CODE_RE = re.compile(r"\\[fFhHcCwWqQtTaAlLoOkKpP][^;\\]*;")
_SP_ESC = {"%%d": "°", "%%c": "Ø", "%%p": "±", "%%%": "%"}


def unescape_dxf(s: str) -> str:
    """还原 DXF 的 `\\U+XXXX` 转义（不还原就看不清真文字）。"""
    return _UE_RE.sub(lambda m: chr(int(m.group(1), 16)), s) if "\\U+" in s else s


def strip_mtext_codes(s: str) -> str:
    """剥掉 MTEXT 内联格式码与老式特殊字符，得到**人能读的真文字**。"""
    if not s:
        return ""
    out = _MT_CODE_RE.sub("", s)
    out = out.replace("\\P", " ").replace("\\~", " ")
    out = out.replace("{", "").replace("}", "")
    for k, v in _SP_ESC.items():
        out = out.replace(k, v)
    return out.strip()


def _text_width(s: str, height: float) -> float:
    """估宽：中文/全角按 1 字高，ASCII 按 0.55 字高。

    ⚠️ 与 `chemcad_symbols._text_w` 是**同一条约定**（出图与提取必须一致，
    否则"提取出来量到的宽"和"画出来的宽"对不上）。
    """
    return sum(height * 0.55 if ord(ch) < 128 else height for ch in s)


def text_bbox(pt: Pt, s: str, height: float) -> Tuple[float, float, float, float]:
    """给文字一个**有面积的**包围盒。

    为什么必须估宽：TEXT 实体在 DXF 里只有**一个插入点**，若原样当 bbox，
    它对拾取、列扫描、测距全都退化成"一个点"——点拾取点不中、
    "文字压线/超框"这类校核也无从谈起，而**提取结果看起来一切正常**。
    """
    h = height if height > 0 else 2.5
    w = _text_width(s, h)
    x, y = pt
    return (x, y, x + w, y + h)




def entity_bbox(ents: Iterable[Dict[str, object]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for en in ents:
        x0, y0, x1, y1 = en.get("bbox", (0.0, 0.0, 0.0, 0.0))  # type: ignore[misc]
        xs += [x0, x1]
        ys += [y0, y1]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


# ===========================================================================
# 三、去重：同 id 撞车时留谁
# ===========================================================================

def dedupe_keep_best(items: Sequence[Dict[str, object]], key: str = "id",
                     prefer: str = "pts") -> List[Dict[str, object]]:
    """同一 `key` 出现多次时只留一份，**保序**（按首次出现）留"更完整"的那份。

    判据：`prefer` 字段非空者胜（默认 `pts`，即"有真实几何/折线的那份"）。
    为什么不是"后出现者覆盖前者"：文件里的顺序是**工具写的顺序**，不是**可信度顺序**；
    而"有几何"是客观的完整度信号。PfdViewer 就是这么定的（优先留带 `polys` 的那份）。
    """
    best: Dict[object, Dict[str, object]] = {}
    order: List[object] = []
    for it in items:
        k = it.get(key)
        if k not in best:
            best[k] = it
            order.append(k)
        elif not best[k].get(prefer) and it.get(prefer):
            best[k] = it
    return [best[k] for k in order]


# ===========================================================================
# 四、折线：合并 + 定向
# ===========================================================================

def merge_paths(polys: Sequence[Sequence[Pt]], tol: float = 0.02) -> List[List[Pt]]:
    """把首尾相接的折线段合并成连续路径。

    为什么需要：CAD / 模拟器常把**一条**管线或流股拆成多段 `LWPOLYLINE` / `ROUTE`
    （换图层、绕设备、跨图框都会断）。不合并的话，"这条管有多少折点"、
    "它拐了几个弯"这类校核全都不准。

    合并规则：后一段的**起点**接上前一段的**终点** → 直连；
    后一段的**终点**接上前一段的**终点** → 反向接（说明这段是被反着存的）。
    容差内视为相接。结果保持输入顺序，不重排。
    """
    clean = [[(float(x), float(y)) for x, y in p] for p in polys if len(p) >= 1]
    if not clean:
        return []
    merged: List[List[Pt]] = [list(clean[0])]
    for p in clean[1:]:
        last = merged[-1]
        if last and p:
            if math.hypot(last[-1][0] - p[0][0], last[-1][1] - p[0][1]) < tol:
                last.extend(p[1:])
                continue
            if math.hypot(last[-1][0] - p[-1][0], last[-1][1] - p[-1][1]) < tol:
                last.extend(reversed(p[:-1]))
                continue
        merged.append(list(p))
    return merged


def orient_path(poly: Sequence[Pt], src: Optional[Pt],
                dst: Optional[Pt]) -> List[Pt]:
    """按已知的进/出对象位置校正折线方向：**起点靠近源、终点靠近目的**。

    为什么必要：无论是 Aspen 的 `ROUTE` 段还是 CAD 里手画的线，**都没有保证方向**。
    方向错了，流向箭头就指反了——这是"图能出、流程读反"的一类硬伤。

    判定优先级（信息越全越严）：
      1. 两端位置都知道 → 要求"起点近源 **且** 终点近目的"才算对，否则整条反转；
      2. 只知道目的 → 要求终点更近目的；
      3. 只知道源   → 要求起点更近源；
      4. 都不知道   → **原样返回，不猜**。
    """
    pts = [(float(x), float(y)) for x, y in poly]
    if len(pts) < 2:
        return pts

    def d2(a: Pt, b: Optional[Pt]) -> Optional[float]:
        if b is None:
            return None
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    d0s, d1s = d2(pts[0], src), d2(pts[-1], src)
    d0t, d1t = d2(pts[0], dst), d2(pts[-1], dst)

    if src is not None and dst is not None:
        ok = (d0s <= d1s) and (d1t <= d0t)         # type: ignore[operator]
        return pts if ok else pts[::-1]
    if dst is not None:
        return pts if d1t <= d0t else pts[::-1]     # type: ignore[operator]
    if src is not None:
        return pts if d0s <= d1s else pts[::-1]     # type: ignore[operator]
    return pts


def path_length(poly: Sequence[Pt]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(poly, poly[1:]))


# ===========================================================================
# 五、端口反推符号尺寸（中位数抗噪 + 全场基准收敛）
# ===========================================================================

def infer_symbol_sizes(port_pts: Dict[str, List[Pt]],
                       centers: Dict[str, Pt],
                       lo: float = 0.4, hi: float = 2.5,
                       band: Tuple[float, float] = (0.65, 1.4)) -> Dict[str, float]:
    """由**接到符号上的线端位置**反推符号的半尺寸。

    为什么用端口反推：符号尺寸决定了"线该画到哪儿"。若线端是按符号真实轮廓画的，
    那么"线端到符号中心的距离"就是**该符号的半尺寸的实测值**——比任何约定都硬。

    三步，每步都在处理一类噪声：
      1. **逐符号取中位数**（不是平均）：有人把线画到圆心、有人画到很远，
         极端值必须在**符号内**就被剔掉，否则一个离群点就能把整张图带偏；
      2. **全场取基准**：所有符号的估计值再取一次中位数，作为"本图统一尺寸"；
      3. **收敛到基准附近**（`band` = 0.65~1.4 倍）：同一张图上符号**不该一大一小**，
         差得离谱的多半是估计噪声，拉回带内；基准本身再夹到 `[lo, hi]`。

    返回 `{符号 id: 半尺寸}`；`centers` 里没有端口信息的符号取全场基准。
    """
    est: Dict[str, float] = {}
    for sid, pts in port_pts.items():
        c = centers.get(sid)
        if c is None or not pts:
            continue
        dists = sorted(math.hypot(x - c[0], y - c[1]) for x, y in pts)
        if dists:
            est[sid] = dists[len(dists) // 2]        # 中位数

    base = 0.6
    if est:
        vals = sorted(est.values())
        base = vals[len(vals) // 2]
        base = max(lo, min(hi, base))

    out: Dict[str, float] = {}
    for sid in centers:
        v = est.get(sid, base)
        low, high = band[0] * base, band[1] * base
        out[sid] = max(lo, min(hi, max(low, min(high, v))))
    return out


# ===========================================================================
# 六、把线端裁到真实轮廓
# ===========================================================================
#
# 只按"圆心/中心"定位是不够的：线会**画进符号里面**。视觉上就是"管线穿过设备"，
# 而自动校核（正交性、线宽、位号）一条都查不出来——只能靠目视发现。
# 正确做法：按符号**真实轮廓**（圆 / 矩形 / 三角形这类多边形）把线端裁到边界上。


def point_in_poly(pt: Pt, verts: Sequence[Pt]) -> bool:
    """射线法判点在多边形内。"""
    x, y = pt
    inside = False
    n = len(verts)
    j = n - 1
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def clip_circle(p: Pt, center: Pt, radius: float) -> Pt:
    """沿 center→p 方向，把 p 拉回圆周上（已在圆内则原样返回）。"""
    dx, dy = p[0] - center[0], p[1] - center[1]
    d = math.hypot(dx, dy)
    if d <= 1e-9 or d <= radius:
        return p
    return (center[0] + dx / d * radius, center[1] + dy / d * radius)


def clip_rect(p: Pt, center: Pt, hw: float, hh: float) -> Pt:
    """沿 center→p 方向，把 p 裁到宽 2hw、高 2hh 的矩形边界上。"""
    dx, dy = p[0] - center[0], p[1] - center[1]
    if dx == 0 and dy == 0:
        return p
    tx = abs(hw / dx) if dx else 1e18
    ty = abs(hh / dy) if dy else 1e18
    t = min(tx, ty, 1.0)
    return (center[0] + dx * t, center[1] + dy * t)


def clip_poly(p: Pt, center: Pt, verts: Sequence[Pt], iters: int = 40) -> Pt:
    """把 p 裁到任意多边形边界上：**二分逼近**。

    没有闭式解，用二分（40 次 ≈ 精度 1e-12 相对量）：在 center 与 p 之间找
    "在内 / 在外"的分界点。比"取包围盒近似"准得多，代价只有 40 次点在多边形测试。
    """
    dx, dy = p[0] - center[0], p[1] - center[1]
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return p
    if point_in_poly(p, verts):
        return p
    ux, uy = dx / d, dy / d
    lo, hi = 0.0, d
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if point_in_poly((center[0] + ux * mid, center[1] + uy * mid), verts):
            lo = mid
        else:
            hi = mid
    return (center[0] + ux * lo, center[1] + uy * lo)


#: 轮廓形状表。`shape` 取 "circle" / "rect" / "triangle_up" / "triangle_down"
#: 或 `("poly", [顶点…])`。
def outline_verts(shape: str, center: Pt, half: float) -> Optional[List[Pt]]:
    cx, cy = center
    if shape == "triangle_up":
        return [(cx, cy - half), (cx + 0.9 * half, cy + half), (cx - 0.9 * half, cy + half)]
    if shape == "triangle_down":
        return [(cx, cy + half), (cx - 0.8 * half, cy - half), (cx + 0.8 * half, cy - half)]
    return None


def clip_to_outline(p: Pt, center: Pt, shape: str = "rect",
                    half: float = 1.0, aspect: float = 1.0) -> Pt:
    """把线端 `p` 裁到符号真实轮廓边界上。

    `aspect` 是矩形/多边形的**宽高比**（半宽 = aspect·half），让"方箱子"和"瘦塔"都能对上。
    认不出的 `shape` 一律按矩形——**保守但不静默**：调用方应把未识别形状记进报告。
    """
    s = shape or "rect"
    if s == "circle":
        return clip_circle(p, center, half)
    if isinstance(s, tuple) and s and s[0] == "poly":
        return clip_poly(p, center, s[1])
    verts = outline_verts(s, center, half)
    if verts is not None:
        return clip_poly(p, center, verts)
    return clip_rect(p, center, half * aspect, half)


# ===========================================================================
# 七、拾取（定位"这个点上是哪个对象"）
# ===========================================================================

def pick_at(pt: Pt, items: Sequence[Dict[str, object]],
            key: str = "tag", tol: float = 0.0) -> Optional[Dict[str, object]]:
    """给出一个点，找出"点中的对象"。

    命中判定：先用**包围盒**筛（快、稳），多个候选时取**中心最近**的那个
    ——与 PfdViewer 的点击命中一致：先按矩形框住，再比中心距离。
    `tol` 为正时把包围盒外扩 `tol`（容错点击）。

    为什么按"最近中心"而不是"命中最多个"：符号是**离散**的，
    重叠区域里离中心近的那个几乎总是用户真正想点的。
    """
    x, y = pt
    best: Optional[Dict[str, object]] = None
    best_d = float("inf")
    for it in items:
        bb = it.get("bbox")
        if not bb:
            continue
        x0, y0, x1, y1 = bb  # type: ignore[misc]
        if not (x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol):
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        d = (x - cx) ** 2 + (y - cy) ** 2
        if d < best_d:
            best_d, best = d, it
    return best


# ===========================================================================
# 八、文字列扫描排序（还原图纸阅读顺序）
# ===========================================================================

def order_column_scan(items: Sequence[Dict[str, object]],
                      col_factor: float = 3.0) -> List[Dict[str, object]]:
    """按"列扫描"顺序排文字：**从左到右逐列，列内从上到下**。

    这是 `dwg-text-extractor` 的做法：图纸不是"一行行"排的，而是**分栏**的
    （说明框、明细表、图例栏各占一列）。单纯按 y 排会把不同栏的文字串在一起，
    按 x 排又会把同一栏的顺序打乱。列扫描两者兼顾。

    列边界：把所有文字的 x 升序后，间隔 > `col_factor × 平均字高` 处断开——
    **阈值随字号自适应**，小字图纸不会误并栏。
    """
    pts = [it for it in items if it.get("bbox")]
    if not pts:
        return list(items)
    heights = [float(it.get("height") or 0.0) for it in pts]
    avg_h = (sum(h for h in heights if h > 0) / max(1, sum(1 for h in heights if h > 0))) or 2.5
    gap = avg_h * col_factor

    xs = sorted({round(float(it["bbox"][0]), 6) for it in pts})  # type: ignore[index]
    col_of: Dict[float, int] = {}
    col = 0
    prev: Optional[float] = None
    for x in xs:
        if prev is not None and (x - prev) > gap:
            col += 1
        col_of[x] = col
        prev = x

    def sort_key(it: Dict[str, object]):
        bb = it["bbox"]
        c = col_of.get(round(float(bb[0]), 6), 0)   # type: ignore[index]
        return (c, -round(float(bb[3]), 6), round(float(bb[0]), 6))  # type: ignore[index]

    return sorted(items, key=sort_key)


# ===========================================================================
# 九、测距（对象间距 / 净空核查）
# ===========================================================================

def point_distance(a: Pt, b: Pt) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _bbox_gap(a: Tuple[float, float, float, float],
              b: Tuple[float, float, float, float]) -> float:
    """两包围盒的**最近距离**（相交为 0）。"""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def closest_distance(a: Dict[str, object], b: Dict[str, object]) -> Dict[str, float]:
    """两个对象的间距：中心距 + 包围盒净距。

    两个数都要，因为它们是**两个不同的问题**：
      * 中心距 → "摆得均匀吗"（布置评审常用）；
      * 净距   → "够不够净空"（HG 20519 通道/检修间距核查用，相交时为 0 就是**撞上了**）。
    """
    ba = a.get("bbox")
    bb = b.get("bbox")
    if not ba or not bb:
        raise ValueError("对象缺少 bbox，无法测距")
    ca = ((ba[0] + ba[2]) / 2.0, (ba[1] + ba[3]) / 2.0)  # type: ignore[index]
    cb = ((bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0)  # type: ignore[index]
    return {"center": round(point_distance(ca, cb), 4),
            "clearance": round(_bbox_gap(ba, bb), 4)}   # type: ignore[arg-type]


# ===========================================================================
# 十、Aspen .bkp 解析（模块定位 + 流股拓扑 + 工艺结果）
# ===========================================================================
#
# 由来：本机不装 Aspen 也要能看流程。`.bkp` 是**文本**，流程图数据存在
# `PFSVData` / `#HIERPFSVDATA` 段里，拓扑存在 `FLOWSHEET` 段的
# `BLOCK BLKID=… BLKTYPE=… IN=(…) OUT=(…)` 里，工艺结果存在
# `DSET STREAM MATERIAL <名> RES_STR @L_n (…)` 里。
#
# ⚠️ 一处**经验订正**：本技能经验库旧条目写"流股结果（T/P/F）不在 bkp 里"——
# 那是不完整的。`DSET STREAM … RES_STR` 段**确实带 T/P/质量流量/体积流量/摩尔流量/气相分率**，
# 本模块的 `parse_bkp_stream_results` 把它们提出来（`selftest [13]` 钉住）。
#
# 而**管径 / 设备规格**不在 bkp 里属**预期之内，不算缺失**：PFD 的图面内容本就
# 不含这两项（管径属「管道布置图 / 管道特性表」，设备规格属「设备数据表 / 设备一览表」）。
# 因此本模块**不去识别它们、也不把它们列进 `unresolved`**；仅当用户明确要那几类图/表时才另取。
# （机械制图部分已于 2026-09-23 移出本技能范围，不再有"机械图仍需设备规格"这个例外。）

_PFS_MARKERS = ("PFSVData", "#HIERPFSVDATA")
_AT_RE = re.compile(r"^At\s+(-?[\d.]+)\s+(-?[\d.]+)\s*$")
_ID_RE = re.compile(r"^ID:\s*(.*?)\s*$")
_ICON_RE = re.compile(r'^ICON:\s*"([^"]*)"\s*$')
_ROUTE_RE = re.compile(r"^ROUTE\s+\d+\s+\d+\s*$")
_POLY_RE = re.compile(r"^[rtxy]\s+[rtxy]\s+(-?[\d.]+)\s+(-?[\d.]+)\s+0\s*$")
_PFS_SEC_RE = re.compile(r"^(\S+)\s+PFSVData$")
_RES_STR_RE = re.compile(r"DSET STREAM MATERIAL (\S+) RES_STR @L_\d+ \(([^)]*)\)")
_UNITS_SET_RE = re.compile(
    r'SETUP "UNITS-SET" (\S+).*?DESCRIPTION DESCRIPTION = "([^"]+)"', re.S)
_BLK_RE = re.compile(r'BLOCK\s+BLKID\s*=\s*(\S+)\s+BLKTYPE\s*=\s*"([^"]+)"')
_CONNECT_RE = re.compile(r'CONNECT\s+BLKID\s*=\s*"?([^"\s]+)"?')
_LIST_RE = re.compile(r"\b(IN|OUT)\s*=\s*\(([^)]*)\)", re.S)
_SUB_RE = re.compile(r"^M\d+-\d+$")
_KNOWN_SUB = {"MIXED", "CISOLID", "NC", "NCPSD", "CIPSD", "SOLIDS",
              "CONVEN", "HEAT", "WORK"}


def _is_substream(tok: str) -> bool:
    """子流股标记（`M1-2`、`MIXED`、`CISOLID`…）必须剔除。

    不剔的话 `IN=(FEED MIXED-NC …)` 会把 `MIXED` 当成一条真流股，
    凭空多出一条"物料线"，且**不报错**——又是一类静默失真。
    """
    return bool(_SUB_RE.match(tok)) or tok.upper() in _KNOWN_SUB


def parse_bkp_scopes(lines: Sequence[str]) -> List[Tuple[int, int, str]]:
    """列出文件里所有 PFS 图段：`[(start, end, owner)]`。

    `owner == ""` → 顶层流程图（坐标即主画布坐标）；
    `owner == "B1"` → 层级块 B1 自己那一页（**局部坐标**）。

    ⚠️ Aspen 会把层级块内部的模块/流股**再存一份、名字还不带前缀**。
    所以在整个文件里按名字找流股一定撞车 —— **必须按段取**。
    这是本模块最重要的一个"先切作用域"的实例。
    """
    heads: List[Tuple[int, str]] = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s == "PFSVData":
            heads.append((i, ""))
            continue
        m = _PFS_SEC_RE.match(s)
        if m and not s.startswith("#"):
            heads.append((i, m.group(1)))

    out: List[Tuple[int, int, str]] = []
    for idx, (i, owner) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        for j in range(i + 1, end):
            s = lines[j].strip()
            if s.startswith("$_") or s.startswith("_$"):
                end = j
                break
        out.append((i, end, owner))
    return out


def _scope_obj_count(lines: Sequence[str], sec: Tuple[int, int, str]) -> int:
    a, b, _ = sec
    return sum(1 for j in range(a, b) if lines[j].strip() in ("BLOCK", "STREAM"))


def pick_main_bkp_scope(lines: Sequence[str]) -> Tuple[Optional[int], Optional[int], str]:
    """挑出**主 PFD 段**：优先顶层 `PFSVData`，只有层级页有图时才退回它。

    返回 `(start, end, why)`，`why` 说明这次是靠什么挑的（可追溯）。
    判据：**对象数最多**的那段 = 真正的主流程图。混读两页就是"图上有两条同样的流股"的成因。
    """
    secs = parse_bkp_scopes(lines)
    if not secs:
        return None, None, "文件里没有 PFSVData 段"
    top = [s for s in secs if s[2] == ""]
    pool = top if top and any(_scope_obj_count(lines, s) for s in top) else secs
    best = max(pool, key=lambda s: _scope_obj_count(lines, s))
    why = "顶层 PFSVData（对象最多：%d）" % _scope_obj_count(lines, best)
    if best[2]:
        why = "退回层级页 %s（顶层无对象）" % best[2]
    elif not top:
        why = "无顶层 PFSVData，取对象最多的段"
    return best[0], best[1], why


def parse_bkp_objects(lines: Sequence[str]) -> Tuple[List[Dict], List[Dict]]:
    """解析 PFS 段里的 BLOCK / STREAM 记录（含 ROUTE 路由折线）。

    返回 `(blocks, streams)`；每条记录只取**主坐标**（`At x y` 后即 `cur_id=None`），
    因为同一 id 后面还会跟一串子记录坐标，取多了会把中心点拖偏。
    """
    start, end, _why = pick_main_bkp_scope(lines)
    blocks: List[Dict] = []
    streams: List[Dict] = []
    if start is None:
        return blocks, streams

    cur: Optional[str] = None
    cur_id: Optional[str] = None
    cur_icon = ""
    cur_poly: Optional[List[Pt]] = None
    cur_stream: Optional[Dict] = None

    def flush_stream() -> None:
        nonlocal cur_stream, cur_poly
        if cur_poly is not None and len(cur_poly) >= 2:
            cur_stream["polys"].append(cur_poly)      # type: ignore[union-attr]
        cur_poly = None
        if cur_stream is not None and cur_stream.get("id"):
            streams.append(cur_stream)

    for i in range(int(start), int(end)):
        s = lines[i].strip()
        if s == "BLOCK":
            if cur == "stream":
                flush_stream()
            cur, cur_id, cur_icon, cur_stream = "block", None, "", None
            continue
        if s == "STREAM":
            if cur == "stream":
                flush_stream()
            cur, cur_id = "stream", None
            cur_stream = {"id": None, "x": 0.0, "y": 0.0, "polys": []}
            continue
        if cur is None:
            continue
        m = _ID_RE.match(s)
        if m:
            cur_id = m.group(1).strip()
            if cur_stream is not None:
                cur_stream["id"] = cur_id
            continue
        if cur == "block":
            m = _ICON_RE.match(s)
            if m:
                cur_icon = m.group(1)
                continue
        if cur == "stream":
            if _ROUTE_RE.match(s):
                if cur_poly is not None and len(cur_poly) >= 2:
                    cur_stream["polys"].append(cur_poly)   # type: ignore[union-attr]
                cur_poly = []
                continue
            m = _POLY_RE.match(s)
            if m and cur_poly is not None:
                cur_poly.append((float(m.group(1)), float(m.group(2))))
                continue
        m = _AT_RE.match(s)
        if m and cur_id:
            x, y = float(m.group(1)), float(m.group(2))
            if cur == "block":
                blocks.append({"id": cur_id, "icon": cur_icon, "x": x, "y": y})
            elif cur_stream is not None:
                cur_stream["x"], cur_stream["y"] = x, y
            cur_id = None          # 每条记录只取主坐标
    if cur == "stream":
        flush_stream()
    return dedupe_keep_best(blocks, "id", "icon"), dedupe_keep_best(streams, "id", "polys")


def parse_bkp_flowsheet(text: str) -> Dict[str, Dict]:
    """解析 `FLOWSHEET` 段：块的类型 + IN/OUT 流股（连通性的**权威来源**）。

    层级块的 `CONNECT BLKID=` 单独收进 `connect`，**只在主 IN/OUT 缺连时兜底**——
    反过来会让层级端口覆盖真实连接，把拓扑改错。
    """
    hits = [(m.start(), m) for m in _BLK_RE.finditer(text)]
    result: Dict[str, Dict] = {}
    connect_io: Dict[str, Dict] = {}
    for idx, (pos, m) in enumerate(hits):
        end = hits[idx + 1][0] if idx + 1 < len(hits) else min(len(text), pos + 6000)
        seg = text[pos:end]
        bid = m.group(1).strip('"')
        entry: Dict = {"type": m.group(2), "in": [], "out": []}
        for lm in _LIST_RE.finditer(seg):
            toks = [t.strip('"') for t in lm.group(2).split()
                    if t and not _is_substream(t)]
            entry[lm.group(1).lower()].extend(toks)
        result[bid] = entry
        for cm in _CONNECT_RE.finditer(seg):
            cstart = cm.end()
            cut = seg.find("BLOCK BLKID", cstart)
            if cut == -1:
                cut = seg.find("CONNECT BLKID", cstart)
            cseg = seg[cstart: cut if cut != -1 else len(seg)]
            cio = connect_io.setdefault(bid, {"type": "HIERARCHY", "in": [], "out": []})
            for lm in _LIST_RE.finditer(cseg):
                toks = [t.strip('"') for t in lm.group(2).split()
                        if t and not _is_substream(t)]
                cio[lm.group(1).lower()].extend(toks)
    return {"blocks": result, "connect": connect_io}


def build_stream_connections(flow: Dict[str, Dict]) -> Dict[str, Dict]:
    """由 FLOWSHEET 的 IN/OUT 推出每条流股的 `{source, dest}`（谁产、谁用）。"""
    conn: Dict[str, Dict] = {}
    for bid, e in flow.items():
        for sid in e["in"]:
            conn.setdefault(sid, {})["dest"] = bid
        for sid in e["out"]:
            conn.setdefault(sid, {})["source"] = bid
    return conn


def _dset_numbers(raw: str) -> List[float]:
    """`RES_STR` 里混着单位标签 `<3>` 与占位符 `*`，只留真实数值。

    另处理 Fortran 指数写法（`1.5D+02`）——这是模拟器导出文本的常见形态，
    不处理就会被当成字符串丢掉。
    """
    cleaned = re.sub(r"<[^>]*>", " ", raw).replace("*", " ")
    out: List[float] = []
    for t in cleaned.split():
        try:
            out.append(float(t.replace("D", "E").replace("d", "e")))
        except ValueError:
            continue  # 非数值 token（DSET 行混入标签）→ 跳过
    return out


def parse_bkp_stream_results(text: str) -> Dict[str, Dict[str, float]]:
    """提取每条流股的工艺结果：温度 / 压力 / 质量流量 / 体积流量 / 摩尔流量 / 气相分率。

    字段序（`RES_STR` 约定）：`TEMP PRES MASS VOL MOLE VFRAC`，少于 6 个值即判为不完整、不取。
    """
    out: Dict[str, Dict[str, float]] = {}
    for m in _RES_STR_RE.finditer(text):
        vals = _dset_numbers(m.group(2))
        if len(vals) >= 6:
            out[m.group(1)] = {"temp": vals[0], "pres": vals[1], "mass": vals[2],
                               "vol": vals[3], "mole": vals[4], "vfrac": vals[5]}
    return out


def parse_bkp_areas(text: str) -> Dict[str, object]:
    """提取 Aspen 流程图里的**车间/工段标注**与**分界线** —— 模型的工段划分。

    实测（浙工大 V15 bkp）：流程图用 `楷体` 文本框写「干气精制车间」等区域名，并用
    `Type: l` 的分界线把图面切成若干区域。**这些就是模型的工段划分**——比
    "按位号前两位猜"权威得多（位号只是习惯编号，区域名才是工艺划分）。

    返回 `{"areas": [{"name", "x", "y"}], "walls_h": [...], "walls_v": [...]}`；
    没有标注时返回空（调用方应回退到别的判据，不要凭空造工段）。
    """
    areas: List[Dict[str, object]] = []
    walls_h: List[Dict[str, float]] = []
    walls_v: List[Dict[str, float]] = []
    base: Optional[Tuple[float, float]] = None
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("Base:"):
            try:
                a = s[5:].split()
                base = (float(a[0]), float(a[1]))
            except (ValueError, IndexError):
                base = None
        elif base is not None and s.endswith(("车间", "工段", "装置区")) and 1 < len(s) <= 24:
            areas.append({"name": s, "x": base[0], "y": base[1]})
            base = None
        elif s.startswith("Dim:"):
            try:
                v = [float(t) for t in s[4:].split()]
            except ValueError:
                continue  # Dim: 行含非数值 → 跳过该行
            if len(v) != 4 or (v[0], v[1]) == (v[2], v[3]):
                continue
            dx, dy = abs(v[0] - v[2]), abs(v[1] - v[3])
            # ⚠️ 容差要松：样本里竖墙写作 `Dim: 25.4 21.3 25.3 -10.6`（x 差 0.1，不是 0）
            if dx <= 0.5 and dy > 15:                       # 竖直分界
                walls_v.append({"x": round((v[0] + v[2]) / 2.0, 4),
                                "y1": min(v[1], v[3]), "y2": max(v[1], v[3])})
            elif dy <= 0.5 and dx > 15:                     # 水平分界
                walls_h.append({"y": round((v[1] + v[3]) / 2.0, 4),
                                "x1": min(v[0], v[2]), "x2": max(v[0], v[2])})
    return {"areas": areas, "walls_h": walls_h, "walls_v": walls_v}


def parse_bkp_units(text: str) -> Dict[str, str]:
    """从单位集描述推断 T/P/流量单位（如 `METCBAR` ≈ ℃ / bar / m³·h⁻¹）。

    这一步不显眼但关键：`RES_STR` 只给**裸数字**。不把单位顶上去，
    "温度 = 45" 到底 45 ℃ 还是 45 K 就无法判断，工艺数据表会整体失真。
    """
    m = _UNITS_SET_RE.search(text)
    if not m:
        return {"set": "", "desc": "", "temp": "?", "pres": "?",
                "mass": "kg/h", "mole": "kmol/h", "vol": "m3/h", "recognized": "no"}
    desc = m.group(2)
    d = desc.lower()
    temp = "°C" if (" c," in d or " c " in d) else ("K" if (" k," in d or " k " in d) else "?")
    pres = ("bar" if "bar" in d else
            ("kg/cm²" if "kg/sqcm" in d else ("Pa" if " pa" in d else "?")))
    is_si = ("international" in d) or ("si" in d and " " in d)
    return {"set": m.group(1), "desc": desc, "temp": temp, "pres": pres,
            "vol": "m³/h" if ("cum" in d or is_si) else "?",
            "mass": "kg/h" if ("kg" in d or is_si) else ("lb/h" if "lb" in d else "?"),
            "mole": "kmol/h", "recognized": "yes"}


#: 模拟器块类型 → 化工符号大类（PfdViewer 的分类，用于"画成什么形状"与"算哪一类对象"）。
BKP_BLOCK_CATEGORY: Dict[str, str] = {}
for _cat, _names in (
    ("tower", {"RADFRAC", "DSTWU", "DISTL", "EXTRACT", "ABSBR", "MULTIFRAC",
               "SCNFRAC", "PETROFRAC", "TOWERRAD", "RADFRC", "RADFRAG"}),
    ("hx", {"HEATER", "HEATX", "MHEATX", "HXFLUX", "SHORTCUT", "HEATR", "HOTX", "HX"}),
    ("pressure", {"PUMP", "COMPR", "MCOMPR", "VALVE", "PIPE"}),
    ("reactor", {"RPLUG", "RSTOIC", "RCSTR", "RYIELD", "REQUIL", "RGIBBS",
                 "RBATCH", "RATEB"}),
    ("mix", {"MIXER", "FSPLIT", "SSPLIT", "TEE", "FSPLIT1"}),
    ("sep", {"FLASH2", "FLASH3", "DECANTER", "SEP", "SEP2", "FABFL", "FLASH"}),
):
    for _n in _names:
        BKP_BLOCK_CATEGORY[_n] = _cat


def bkp_category(block_type: str) -> str:
    return BKP_BLOCK_CATEGORY.get((block_type or "").upper(), "default")


#: 化工符号大类 → 轮廓形状（喂给 `clip_to_outline`，保证线端不画进符号里）。
CATEGORY_OUTLINE: Dict[str, str] = {
    "tower": "rect", "hx": "circle", "pressure": "triangle_up",
    "reactor": "rect", "mix": "circle", "sep": "rect", "default": "rect",
}


def parse_bkp_case(path: str) -> Dict[str, object]:
    """解析一个 `.bkp`，返回完整的**模块定位 + 流股拓扑 + 工艺结果**。

    返回结构：
        {
          "source": 路径, "encoding": 实际编码, "main_scope": 主段选取理由,
          "blocks":  [{id,type,icon,category,x,y,outline,half}],     # 模块定位
          "streams": [{id,x,y,source,dest,polys,length}],           # 拓扑 + 折线
          "stream_results": {流股号: {temp,pres,mass,vol,mole,vfrac}},  # 工艺结果
          "units": {…}, "unresolved": [未识别项说明]
        }

    `unresolved` 是本模块的纪律体现：解析不出来的，**列出来**而不是留空。
    """
    text, enc = read_text_robust(path)
    lines = text.splitlines(keepends=True)

    blocks_raw, streams = parse_bkp_objects(lines)
    flow_data = parse_bkp_flowsheet(text)
    flow = flow_data["blocks"]
    conn = build_stream_connections(flow)

    # 层级 CONNECT 仅在主连接缺失时兜底（不可反过来覆盖真实拓扑）
    for bid, cio in flow_data["connect"].items():
        for sid in cio["in"]:
            conn.setdefault(sid, {}).setdefault("dest", bid)
        for sid in cio["out"]:
            conn.setdefault(sid, {}).setdefault("source", bid)

    blocks: List[Dict[str, object]] = []
    by_id: Dict[str, Dict[str, object]] = {}
    unresolved: List[str] = []
    for b in blocks_raw:
        f = flow.get(b["id"], {})
        btype = f.get("type") or b.get("icon") or ""
        cat = bkp_category(btype)
        rec = {"id": b["id"], "type": btype, "icon": b.get("icon", ""),
               "category": cat, "x": b["x"], "y": b["y"],
               "outline": CATEGORY_OUTLINE.get(cat, "rect"), "half": 0.6}
        blocks.append(rec)
        by_id[b["id"]] = rec
        if not f.get("type") and not b.get("icon"):
            unresolved.append("模块 %s：FLOWSHEET 与 ICON 都取不到类型" % b["id"])

    centers = {b["id"]: (float(b["x"]), float(b["y"])) for b in blocks}

    # 折线：先按拓扑定向，再首尾相接合并
    for st in streams:
        c = conn.get(st["id"], {})
        st["source"] = c.get("source")
        st["dest"] = c.get("dest")
        sp = centers.get(st["source"]) if st.get("source") else None
        dp = centers.get(st["dest"]) if st.get("dest") else None
        st["polys"] = merge_paths([orient_path(po, sp, dp)
                                   for po in (st.get("polys") or [])])
        st["length"] = round(sum(path_length(po) for po in st["polys"]), 3)
        if not st["polys"]:
            unresolved.append("流股 %s：无 ROUTE 折线（只有端点坐标）" % st["id"])
        if st.get("source") is None and st.get("dest") is None:
            unresolved.append("流股 %s：拓扑缺失（既无 source 也无 dest）" % st["id"])

    # 端口反推模块半尺寸：把每条流股接模块的那一端的位置交给尺寸估计
    port_pts: Dict[str, List[Pt]] = {}
    for st in streams:
        for po in (st.get("polys") or []):
            if len(po) >= 2:
                if st.get("source") in by_id:
                    port_pts.setdefault(str(st["source"]), []).append(po[0])
                if st.get("dest") in by_id:
                    port_pts.setdefault(str(st["dest"]), []).append(po[-1])
    sizes = infer_symbol_sizes(port_pts, centers)
    for bid, half in sizes.items():
        by_id[bid]["half"] = round(half, 4)

    return {"source": path, "encoding": enc,
            "main_scope": pick_main_bkp_scope(lines)[2],
            "scope_count": len(parse_bkp_scopes(lines)),
            "blocks": blocks, "streams": streams,
            "stream_results": parse_bkp_stream_results(text),
            "units": parse_bkp_units(text),
            "areas": parse_bkp_areas(text),
            "unresolved": sorted(set(unresolved))}


# ===========================================================================
# 十一、DXF 侧的高层封装
# ===========================================================================

def load_dxf(path: str) -> Dict[str, object]:
    """读一个 DXF，返回切好作用域的结构化信息。

    `items` = **主作用域（模型空间）**的 INSERT / TEXT / MTEXT，带 `tag` 与 bbox，
    可直接喂给 `pick_at` / `order_column_scan` / `closest_distance`。
    `scopes` 给出全部作用域清单（块定义也在内），供"同名撞车"排查。
    """
    text, enc = read_text_robust(path)
    pairs = _dxf_pairs(text)
    scopes = split_dxf_scopes(pairs)

    per_scope: Dict[str, List[Dict[str, object]]] = {}
    for sc in scopes:
        if sc["kind"] in (_SCOPE_MODEL, _SCOPE_BLOCK):
            per_scope[str(sc["scope"])] = parse_scope_entities(pairs, sc)

    model = per_scope.get(_SCOPE_MODEL, [])
    items: List[Dict[str, object]] = []
    for en in model:
        if en["type"] not in ("INSERT", "TEXT", "MTEXT"):
            continue
        items.append({"kind": "insert" if en["type"] == "INSERT" else "text",
                      "name": en.get("name") or "",
                      "tag": (en.get("name") or en.get("text") or "").strip(),
                      "layer": en.get("layer", ""),
                      "text": en.get("text", ""),
                      "handle": en.get("handle", ""),
                      "pts": en.get("pts", []),
                      "height": en.get("height", 0.0),
                      "bbox": en["bbox"], "scope": _SCOPE_MODEL})

    # 同名撞车自查：同一 tag 在多个作用域出现 → 报出来（不要静默混读）
    seen: Dict[str, List[str]] = {}
    for scname, ents in per_scope.items():
        if scname == _SCOPE_MODEL:
            continue
        for en in ents:
            nm = (en.get("name") or en.get("text") or "").strip()
            if nm:
                seen.setdefault(nm, []).append(scname)
    model_tags = {str(it["tag"]) for it in items if it["tag"]}
    collisions = sorted(t for t in model_tags if t in seen)

    return {"file": os.path.basename(path), "encoding": enc,
            "scope_count": len(scopes),
            "scopes": [{"scope": s["scope"], "kind": s["kind"], "name": s["name"],
                        "entities": len(parse_scope_entities(pairs, s))
                        if s["kind"] in (_SCOPE_MODEL, _SCOPE_BLOCK) else 0}
                       for s in scopes],
            "items": items,
            "model_bbox": entity_bbox([e for e in model]),
            "tag_collisions": collisions,
            "layers": sorted({e.get("layer", "0") for e in model})}


def locate_tags(dxf: Dict[str, object], tag: str) -> List[Dict[str, object]]:
    """按位号（或块名/文字）定位对象，返回全部命中（含所属作用域）。"""
    want = tag.strip().lower()
    return [it for it in dxf["items"]                      # type: ignore[index]
            if want in str(it.get("tag", "")).lower()
            or want in str(it.get("name", "")).lower()]


# ===========================================================================
# 十二、CLI
# ===========================================================================

def _print_json(obj: object, path: Optional[str]) -> None:
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
        print("JSON ->", path)
    else:
        print(s)


def _cmd_bkp(a) -> int:
    case = parse_bkp_case(a.file)
    if a.json:
        _print_json(case, a.json)
        return 0
    u = case["units"]
    print("=" * 70)
    print("Aspen .bkp 解析：%s" % case["source"])
    print("  编码：%s   PFS 段数：%d   主段取法：%s"
          % (case["encoding"], case["scope_count"], case["main_scope"]))
    if is_lossy(str(case["encoding"])):
        print("  ⚠️ 走的是 latin-1 兜底，中文注释不可信；拓扑/坐标仍可靠（ASCII 段）")
    print("  单位集：%s（%s）→ %s / %s / %s"
          % (u.get("set") or "未识别", u.get("desc") or "-",
             u.get("temp"), u.get("pres"), u.get("mass")))
    print("-" * 70)
    print("模块（%d）——定位 + 类型 + 轮廓 + 反推半尺寸：" % len(case["blocks"]))
    for b in case["blocks"][:40]:
        print("  %-14s %-16s %-9s (%8.2f, %8.2f)  half=%.2f"
              % (b["id"], b["type"], b["category"], b["x"], b["y"], b["half"]))
    if len(case["blocks"]) > 40:
        print("  … 其余 %d 个见 --json" % (len(case["blocks"]) - 40))
    print("-" * 70)
    res = case["stream_results"]
    print("流股（%d，其中带工艺结果的 %d 条）：" % (len(case["streams"]), len(res)))
    for s in case["streams"][:25]:
        r = res.get(str(s["id"]))
        rtxt = ("T=%.4g%s P=%.4g%s F=%.4g%s"
                % (r["temp"], u.get("temp", ""), r["pres"], u.get("pres", ""),
                   r["mass"], u.get("mass", ""))) if r else "（无结果数据）"
        print("  %-14s %-10s→%-10s len=%-8.1f %s"
              % (s["id"], s.get("source") or "进料", s.get("dest") or "产品",
                 s.get("length", 0.0), rtxt))
    if len(case["streams"]) > 25:
        print("  … 其余 %d 条见 --json" % (len(case["streams"]) - 25))
    if case["unresolved"]:
        print("-" * 70)
        print("未解析项（%d）——不猜，报出来：" % len(case["unresolved"]))
        for x in case["unresolved"][:15]:
            print("  · %s" % x)
    return 0


def _cmd_scopes(a) -> int:
    dxf = load_dxf(a.file)
    if a.json:
        _print_json(dxf, a.json)
        return 0
    print("=" * 70)
    print("DXF 作用域切分：%s（编码 %s，共 %d 个作用域）"
          % (dxf["file"], dxf["encoding"], dxf["scope_count"]))
    for s in dxf["scopes"]:
        print("  %-28s [%-7s] 实体 %d" % (s["scope"], s["kind"], s["entities"]))
    print("-" * 70)
    print("模型空间：图层 %d 个，命名对象 %d 个" % (len(dxf["layers"]), len(dxf["items"])))
    print("  图层：%s" % dxf["layers"][:20])
    if dxf["tag_collisions"]:
        print("  ⚠️ 同名撞车（模型空间与块定义内同名，按名定位会歧义）：%s"
              % dxf["tag_collisions"][:10])
    else:
        print("  ✓ 无同名撞车（模型空间与块定义不重名）")
    return 0


def _cmd_locate(a) -> int:
    dxf = load_dxf(a.file)
    hits = locate_tags(dxf, a.tag)
    if a.json:
        _print_json(hits, a.json)
        return 0
    print("定位 %r：命中 %d 个" % (a.tag, len(hits)))
    for it in hits:
        bb = it["bbox"]
        print("  [%s] %-8s layer=%-10s bbox=(%.2f, %.2f, %.2f, %.2f) scope=%s"
              % (it["kind"], it["tag"], it["layer"], bb[0], bb[1], bb[2], bb[3],
                 it["scope"]))
    if not hits:
        print("  （未命中——检查位号写法，或该对象只在块定义内，用 scopes 子命令看）")
    return 0 if hits else 4


def _cmd_pick(a) -> int:
    x, y = (float(v) for v in a.at.split(","))
    dxf = load_dxf(a.file)
    hit = pick_at((x, y), dxf["items"], tol=a.tol)          # type: ignore[arg-type]
    if a.json:
        _print_json(hit, a.json)
    elif hit:
        bb = hit["bbox"]
        print("点 (%.2f, %.2f) 命中：%s [%s] layer=%s bbox=(%.2f, %.2f, %.2f, %.2f)"
              % (x, y, hit["tag"], hit["kind"], hit["layer"],
                 bb[0], bb[1], bb[2], bb[3]))
    else:
        print("点 (%.2f, %.2f) 未命中任何对象（可用 --tol 放宽）" % (x, y))
    return 0 if hit else 4


def _cmd_texts(a) -> int:
    dxf = load_dxf(a.file)
    items = [it for it in dxf["items"] if it["kind"] == "text"]   # type: ignore[index]
    if a.order == "column":
        items = order_column_scan(items)
    if a.json:
        _print_json(items, a.json)
        return 0
    print("文字（%d 条，顺序：%s）" % (len(items), a.order))
    for it in items:
        print("  %-10s %s" % (it["layer"], it["text"]))
    return 0


def _cmd_distance(a) -> int:
    dxf = load_dxf(a.file)
    ia = locate_tags(dxf, a.a)
    ib = locate_tags(dxf, a.b)
    if not ia or not ib:
        print("ERR 未找到对象：%s=%d 个, %s=%d 个" % (a.a, len(ia), a.b, len(ib)))
        return 4
    d = closest_distance(ia[0], ib[0])
    print("%s ↔ %s  中心距=%.3f  净距=%.3f（0 = 相交/相撞）"
          % (a.a, a.b, d["center"], d["clearance"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="图纸对象定位与信息提取（DXF / Aspen .bkp）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bkp", help="解析 Aspen .bkp：模块定位 + 流股拓扑 + 工艺结果")
    p.add_argument("file")
    p.add_argument("--json", default=None)
    p.set_defaults(func=_cmd_bkp)

    p = sub.add_parser("scopes", help="DXF 作用域切分（含同名撞车自查）")
    p.add_argument("file")
    p.add_argument("--json", default=None)
    p.set_defaults(func=_cmd_scopes)

    p = sub.add_parser("locate", help="按位号/块名定位对象")
    p.add_argument("file")
    p.add_argument("--tag", required=True)
    p.add_argument("--json", default=None)
    p.set_defaults(func=_cmd_locate)

    p = sub.add_parser("pick", help="点拾取：这个坐标上是哪个对象")
    p.add_argument("file")
    p.add_argument("--at", required=True, help="x,y")
    p.add_argument("--tol", type=float, default=0.0)
    p.add_argument("--json", default=None)
    p.set_defaults(func=_cmd_pick)

    p = sub.add_parser("texts", help="提取文字（可还原图纸阅读顺序）")
    p.add_argument("file")
    p.add_argument("--order", choices=("file", "column"), default="file")
    p.add_argument("--json", default=None)
    p.set_defaults(func=_cmd_texts)

    p = sub.add_parser("distance", help="两个对象的中心距与净距")
    p.add_argument("file")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.set_defaults(func=_cmd_distance)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as e:
        print("ERR 文件不存在：%s" % e, file=sys.stderr)
        return 2
    except Exception as e:                       # 异常路径不抛栈（CLI 契约）
        print("ERR %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
