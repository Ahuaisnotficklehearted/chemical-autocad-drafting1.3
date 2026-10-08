#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_scr.py —— 出图后端 C/D：SCR 脚本 与 AutoLISP。

为什么还需要这两条后端
----------------------
已有三条出图通道，各有边界：

| 通道 | 前提 | 边界 |
|---|---|---|
| `--backend dxf` | 无（纯 Python） | 落盘文件；要另开 AutoCAD 载入 |
| `--backend com` | AutoCAD 运行中 + pyautocad + pywin32 | 装不上 pywin32 就用不了 |
| **`--backend scr`** | AutoCAD 运行中，**啥都不用装** | 用户手动跑 `SCRIPT` 一条命令 |
| **`--backend lisp`** | AutoCAD 运行中，**啥都不用装** | `APPLOAD` 载入后调用命令 |

后两条正是 `autocad-drawing` 技能的做法，价值在于：**把"依赖 COM 环境"这个
前置条件去掉**。现场机器常常装不上 pywin32（权限/版本），但只要 AutoCAD 能开，
就能把脚本拖进去跑。

两条通道的实现差异（都很重要）
------------------------------
* **SCR**：走 `LINE` / `CIRCLE` / `ARC` / `PLINE` / `SOLID` / `-TEXT` 命令序列。
  命令行的 `-BLOCK` 需要"选择对象"，在脚本模式下不可靠，所以 SCR **展平为基本图元**
  （等价于 `--no-blocks`）。
* **AutoLISP**：走 `(entmake ...)` 直接写实体数据库——**绕开命令行提示**，
  因此可以正确生成 BLOCK / INSERT（图块去重、符号复用照旧），
  且不受 AutoCAD 界面语言（中文版/英文版）影响，也不会因为 `FILEDIA`、`OSNAP`、
  对话框状态而中断。
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Tuple

from chemcad_ir import (
    Arc, Circle, Drawing, Ent, Group, Line, Polyline, Pt, Solid, Text,
    LAYER_REGISTRY, resolve_layer,
)

from chemcad_layers import layer_definition, output_layer_names

# ---------------------------------------------------------------------------
# 一、数值与转义
# ---------------------------------------------------------------------------

def n(v: float) -> str:
    """紧凑数字：最多 4 位小数，去掉尾零（`120.0000` → `120`）。"""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def xy(p: Pt) -> str:
    return f"{n(p[0])},{n(p[1])}"


def _arc_points(c: Pt, r: float, a1: float, a2: float) -> Tuple[Pt, Pt]:
    """弧的起点、终点坐标（SCR 的 ARC 命令要给出端点）。"""
    p1 = (c[0] + r * math.cos(math.radians(a1)), c[1] + r * math.sin(math.radians(a1)))
    p2 = (c[0] + r * math.cos(math.radians(a2)), c[1] + r * math.sin(math.radians(a2)))
    return p1, p2


def _lisp_str(s: str) -> str:
    """AutoLISP 字符串字面量转义（反斜杠与双引号）。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dxf_align(align: str) -> Tuple[int, int, bool]:
    """IR 对齐名 → (组码 72, 组码 73, 是否需要第二对齐点 11)。"""
    return {
        "left": (0, 0, False),
        "center": (1, 0, True),
        "right": (2, 0, True),
        "middle_center": (1, 2, True),
        "middle_left": (0, 2, True),
        "middle_right": (2, 2, True),
    }.get(align, (0, 0, False))


#: SCR 的 `-TEXT` 对齐关键字
_SCR_JUSTIFY = {
    "center": "C", "right": "R",
    "middle_center": "MC", "middle_left": "ML", "middle_right": "MR",
}


# ---------------------------------------------------------------------------
# 二、图块去重（与 chemcad_engine._geom_key 同一口径）
# ---------------------------------------------------------------------------

def _rel(p: Pt, base: Pt) -> Tuple[float, float]:
    return (p[0] - base[0], p[1] - base[1])


def _geom_key(g: Group) -> Tuple:
    def r(v: float) -> float:
        return round(float(v), 2)

    parts: List[Tuple] = []
    for e in g.ents:
        if isinstance(e, Line):
            a, b = _rel(e.p1, g.base), _rel(e.p2, g.base)
            parts.append(("L", e.layer, r(a[0]), r(a[1]), r(b[0]), r(b[1])))
        elif isinstance(e, Polyline):
            parts.append(("P", e.layer, e.closed,
                          tuple((r(p[0]), r(p[1])) for p in
                                (_rel(q, g.base) for q in e.pts))))
        elif isinstance(e, Circle):
            c = _rel(e.center, g.base)
            parts.append(("C", e.layer, r(c[0]), r(c[1]), r(e.radius)))
        elif isinstance(e, Arc):
            c = _rel(e.center, g.base)
            parts.append(("A", e.layer, r(c[0]), r(c[1]), r(e.radius),
                          r(e.start_deg), r(e.end_deg)))
        elif isinstance(e, Solid):
            parts.append(("S", e.layer,
                          tuple((r(p[0]), r(p[1])) for p in
                                (_rel(q, g.base) for q in e.pts))))
        elif isinstance(e, Text):
            t = _rel(e.pt, g.base)
            parts.append(("T", e.layer, e.text, r(t[0]), r(t[1]), r(e.height), e.align))
    return tuple(parts)


def _plan(d: Drawing, use_blocks: bool) -> Tuple[List[Tuple[str, List[Ent]]],
                                                List[Tuple[str, Pt, Tuple]]]:
    """把图纸拆成 (图块定义列表, 平铺项列表)。

    返回 `(blk_defs, flats)`：
      * `blk_defs` = [(块名, [已按基点平移到局部坐标的实体])]，同名同形只留一份；
      * `flats`    = [(块名, 插入点, 几何指纹)] 或 [("", (0,0), (ent,))]，按图纸顺序，
                     供渲染端放 INSERT 或直接画实体。

    ⚠️ 参数名不要叫 `blocks`——局部变量也称 `blocks` 会把它遮蔽成空字典，
    于是 `and blocks` 恒为假、所有符号都被展平、图块去重静默失效。
    """
    if any(isinstance(item, Group) and item.meta.get("native_geometry") for item in d.items):
        raise ValueError("新版原生设备块请使用 DXF 或默认 DWG 通道；SCR/LISP 不支持原曲线和填充")
    blk_defs: Dict[Tuple, Tuple[str, List[Ent]]] = {}
    order: List[Tuple[str, List[Ent]]] = []
    flats: List[Tuple[str, Pt, Tuple]] = []
    names_used: Dict[str, int] = {}

    for item in d.items:
        if isinstance(item, Group) and use_blocks:
            key = (item.name, _geom_key(item))
            got = blk_defs.get(key)
            if got is None:
                base_name = item.name
                k = names_used.get(base_name, 0)
                nm = base_name if k == 0 else f"{base_name}_{k + 1}"
                names_used[base_name] = k + 1
                local = [_shift_copy(e, -item.base[0], -item.base[1]) for e in item.ents]
                got = (nm, local)
                blk_defs[key] = got
                order.append(got)
            flats.append((got[0], item.base, key))
        elif isinstance(item, Group):
            for e in item.ents:
                flats.append(("", (0.0, 0.0), (e,)))
        else:
            flats.append(("", (0.0, 0.0), (item,)))
    return order, flats


def _shift_copy(e: Ent, dx: float, dy: float) -> Ent:
    """复制一个实体并平移（不改动原 IR）。"""
    import copy
    c = copy.deepcopy(e)
    if isinstance(c, Line):
        c.p1, c.p2 = (c.p1[0] + dx, c.p1[1] + dy), (c.p2[0] + dx, c.p2[1] + dy)
    elif isinstance(c, (Polyline, Solid)):
        c.pts = [(p[0] + dx, p[1] + dy) for p in c.pts]
    elif isinstance(c, (Circle, Arc)):
        c.center = (c.center[0] + dx, c.center[1] + dy)
    elif isinstance(c, Text):
        c.pt = (c.pt[0] + dx, c.pt[1] + dy)
    return c


# ---------------------------------------------------------------------------
# 三、SCR 脚本（命令行序列，展平为基本图元）
# ---------------------------------------------------------------------------

def _scr_line(e: Line) -> List[str]:
    return ["LINE", xy(e.p1), xy(e.p2), ""]


def _scr_circle(e: Circle) -> List[str]:
    return ["CIRCLE", xy(e.center), n(e.radius)]


def _scr_arc(e: Arc) -> List[str]:
    p1, p2 = _arc_points(e.center, e.radius, e.start_deg, e.end_deg)
    # ARC 命令：起点 → 圆心(C) → 圆心坐标 → 终点
    return ["ARC", xy(p1), "C", xy(e.center), xy(p2)]


def _scr_polyline(e: Polyline) -> List[str]:
    out = ["PLINE", xy(e.pts[0])] if e.pts else ["PLINE"]
    for p in e.pts[1:]:
        out.append(xy(p))
    out.append("C" if e.closed else "")
    return out


def _scr_solid(e: Solid) -> List[str]:
    q = list(e.pts)
    if len(q) == 3:
        q = [q[0], q[1], q[2], q[2]]
    out = ["SOLID"]
    out += [xy(p) for p in q[:4]]
    return out


def _scr_text(e: Text) -> List[str]:
    j = _SCR_JUSTIFY.get(e.align)
    out = ["-TEXT"]
    if j:
        out += ["J", j]
    out.append(xy(e.pt))
    out.append(n(e.height))
    out.append(n(e.rotation))
    out.append(str(e.text).replace("\n", " "))
    return out


def _scr_ents(ents: Iterable[Ent], state: Dict[str, object]) -> List[str]:
    """把一组实体转成 SCR 行；仅在图层变化时插入 `-LAYER S`。

    `state` 是跨调用共享的 `{"layer": 当前图层}`——图元顺序不变，
    但把 305 次图层切换压到「用到几个图层就切几次」。
    """
    out: List[str] = []
    for e in ents:
        lay = str(e.layer)
        if lay != state.get("layer"):
            out += ["-LAYER", "S", lay, ""]
            state["layer"] = lay
        if isinstance(e, Line):
            out += _scr_line(e)
        elif isinstance(e, Circle):
            out += _scr_circle(e)
        elif isinstance(e, Arc):
            out += _scr_arc(e)
        elif isinstance(e, Polyline):
            out += _scr_polyline(e)
        elif isinstance(e, Solid):
            out += _scr_solid(e)
        elif isinstance(e, Text):
            out += _scr_text(e)
    return out


def to_scr(d: Drawing, layer_linetypes: bool = True,
           header: bool = True) -> str:  # noqa: D417
    """把图纸渲染成 AutoCAD 脚本（`.scr`），在 AutoCAD 里用 `SCRIPT` 运行。

    **SCR 通道一律展平为基本图元，不生成图块**：命令行版 `-BLOCK` 需要交互
    选择对象，脚本模式不可靠（DXF / LISP 通道仍可用真图块）。
    """
    order, flats = _plan(d, use_blocks=False)      # SCR 不使用图块
    lay = ["; ============================================================",
           f"; {d.name}  —— 化工 AutoCAD 制图引擎生成（SCR 通道）",
           "; 用法：AutoCAD 中执行 SCRIPT 命令 → 选择本文件",
           "; ============================================================",
           ""]
    if header:
        lay += ["; —— 先把系统变量调到可脚本化状态 ——",
                "FILEDIA", "0",
                "CMDDIA", "0",
                "ATTREQ", "0",
                "OSMODE", "0",
                "CMDECHO", "1",
                ""]
        # 建图层：M(ake) + C(olor) + L(type)
        lay.append("; —— 建立图层体系（颜色按 HG 20519.1 分档，配打印笔宽表）——")
        for sem in output_layer_names(d):
            ld = layer_definition(sem)
            lay += ["-LAYER", "M", str(sem)]
            if ld:
                lay += ["C", str(ld.color), str(sem)]
                width = "_Default" if ld.lineweight < 0 else n(ld.lineweight / 100.0)
                lay += ["_LWeight", width, str(sem)]
                if d.meta.get("layer_standard"):
                    lay += ["_Plot", "_Plot" if ld.plot else "_NoPlot", str(sem)]
                if layer_linetypes and ld.linetype.upper() != "CONTINUOUS":
                    # 线型文件按版本可能是 acad.lin / acadiso.lin；失败不影响后续
                    lay += ["", "-LINETYPE", "L", str(ld.linetype), "acad.lin", "",
                            "-LAYER", "L", str(ld.linetype), str(sem)]
            lay += ["", ]
        lay += ["", "0", ""]
    lay.append("; —— 图元 ——")
    state: Dict[str, object] = {"layer": None}
    for _nm, _at, payload in flats:
        lay += _scr_ents(payload, state)
    lay += ["", "ZOOM", "E", "", "; —— 完成 ——", ""]
    return "\n".join(lay)


# ---------------------------------------------------------------------------
# 四、AutoLISP（entmake，可生成真图块）
# ---------------------------------------------------------------------------

def _lisp_pt(p: Pt) -> str:
    return f"(list {n(p[0])} {n(p[1])} 0.0)"


def _lisp_pair(code: int, value: str, dotted: bool = True) -> str:
    return f"({code} . {value})" if dotted else f"({code} {value})"


def _lisp_ent(e: Ent) -> str:
    """把一个实体渲染成 (entmake (list ...)) 表达式。"""
    lay = str(e.layer)
    L: List[str] = [f"'(0 . \"{_dxftype(e)}\")", f"'(8 . {_lisp_str(lay)})"]
    if isinstance(e, Line):
        L += [_lisp_pair(10, _lisp_pt(e.p1)), _lisp_pair(11, _lisp_pt(e.p2))]
    elif isinstance(e, Circle):
        L += [_lisp_pair(10, _lisp_pt(e.center)), _lisp_pair(40, n(e.radius))]
    elif isinstance(e, Arc):
        L += [_lisp_pair(10, _lisp_pt(e.center)), _lisp_pair(40, n(e.radius)),
              _lisp_pair(50, n(e.start_deg)), _lisp_pair(51, n(e.end_deg))]
    elif isinstance(e, Polyline):
        L += [f"'(90 . {len(e.pts)})", f"'(70 . {1 if e.closed else 0})"]
        L += [f"(cons 10 {_lisp_pt(p)})" for p in e.pts]
    elif isinstance(e, Solid):
        q = list(e.pts)
        if len(q) == 3:
            q = [q[0], q[1], q[2], q[2]]
        for i, p in enumerate(q[:4]):
            L.append(_lisp_pair(10 + i, _lisp_pt(p)))
    elif isinstance(e, Text):
        h, v, second = _dxf_align(e.align)
        L += [_lisp_pair(10, _lisp_pt(e.pt)), _lisp_pair(40, n(e.height)),
              _lisp_pair(1, _lisp_str(e.text)), _lisp_pair(7, _lisp_str("STANDARD"))]
        if e.rotation:
            L.append(_lisp_pair(50, n(e.rotation)))
        if h:
            L.append(f"'(72 . {h})")
        if v:
            L.append(f"'(73 . {v})")
        if second:
            L.append(_lisp_pair(11, _lisp_pt(e.pt)))
    return "(entmake (list " + "\n               ".join(L) + "))"


def _dxftype(e: Ent) -> str:
    return {Line: "LINE", Circle: "CIRCLE", Arc: "ARC", Polyline: "LWPOLYLINE",
            Solid: "SOLID", Text: "TEXT"}[type(e)]


def to_lisp(d: Drawing, command_name: str = "CHEMCADDRAW", blocks: bool = True,
            layer_linetypes: bool = True) -> str:
    """把图纸渲染成 AutoLISP（`.lsp`）。

    用 `(entmake ...)` 直接写实体数据库，因此：
      * **可以生成真图块**（BLOCK/ENDBLK + INSERT），符号复用与 DXF 通道一致；
      * 不依赖命令行提示，不受界面语言、FILEDIA/OSMODE/对话框状态影响。
    载入方式：`APPLOAD` 选本文件 → 命令 `CHEMCADDRAW`。
    """
    order, flats = _plan(d, use_blocks=blocks)
    out: List[str] = [
        ";;; " + "=" * 58,
        f";;; {d.name} —— 化工 AutoCAD 制图引擎生成（AutoLISP 通道）",
        ";;; 用法：APPLOAD 载入本文件 → 执行命令 " + command_name,
        ";;; " + "=" * 58,
        "(defun c:%s ( / old-osmode old-filedia old-cmdecho)"
        % command_name.upper(),
        "  (setq old-osmode (getvar \"OSMODE\") old-filedia (getvar \"FILEDIA\")",
        "        old-cmdecho (getvar \"CMDECHO\"))",
        "  (setvar \"OSMODE\" 0) (setvar \"CMDECHO\" 0)",
        "  (setvar \"FILEDIA\" 0)",
        "  ;; —— 建立图层 ——",
    ]
    for sem in output_layer_names(d):
        ld = layer_definition(sem)
        out.append("  (if (not (tblsearch \"LAYER\" %s))" % _lisp_str(sem))
        out.append("    (command \"-LAYER\" \"M\" %s)" % _lisp_str(sem))
        out.append("  )")
        if ld:
            out.append("  (command \"-LAYER\" \"C\" \"%d\" %s \"\")" % (ld.color, _lisp_str(sem)))
            width = "_Default" if ld.lineweight < 0 else n(ld.lineweight / 100.0)
            out.append("  (command \"-LAYER\" \"_LWeight\" %s %s \"\")" % (_lisp_str(width), _lisp_str(sem)))
            if d.meta.get("layer_standard"):
                out.append("  (command \"-LAYER\" \"_Plot\" %s %s \"\")" % (_lisp_str("_Plot" if ld.plot else "_NoPlot"), _lisp_str(sem)))
            if layer_linetypes and ld.linetype.upper() != "CONTINUOUS":
                out.append("  ;; 线型按需加载：若报错说明该线型文件未加载，可在图层特性里手工指定")
                out.append("  (command \"-LINETYPE\" \"L\" %s \"acad.lin\" \"\")"
                           % _lisp_str(ld.linetype))
                out.append("  (command \"-LAYER\" \"L\" %s %s \"\")"
                           % (_lisp_str(ld.linetype), _lisp_str(sem)))
    out.append("  ;; —— 图块定义 ——")
    for nm, entlist in order:
        base = " '(0 . \"BLOCK\")"
        out.append("  (entmake (list %s '(8 . \"0\") (cons 2 %s)"
                   % (base, _lisp_str(nm)))
        out.append("                 (cons 10 (list 0.0 0.0 0.0)) '(70 . 0)"
                   " (cons 3 %s)))" % _lisp_str(nm))
        for e in entlist:
            out.append("  " + _lisp_ent(e).replace("\n", "\n  "))
        out.append("  (entmake '((0 . \"ENDBLK\") (8 . \"0\")))")
    out.append("  ;; —— 落图 ——")
    for nm, at, payload in flats:
        if nm:
            out.append("  (entmake (list '(0 . \"INSERT\") (cons 2 %s)" % _lisp_str(nm))
            out.append("                 (cons 10 %s) '(41 . 1.0) '(42 . 1.0)"
                       " '(43 . 1.0) '(50 . 0.0)))" % _lisp_pt(at))
        else:
            for e in payload:      # 平铺项：payload 就是 (ent,)
                out.append("  " + _lisp_ent(e).replace("\n", "\n  "))
    out += [
        "  ;; —— 复位与视图 ——",
        "  (setvar \"CMDECHO\" old-cmdecho) (setvar \"OSMODE\" old-osmode)",
        "  (setvar \"FILEDIA\" old-filedia)",
        "  (command \"ZOOM\" \"E\")",
        "  (princ (strcat \"\\n化工制图引擎：已绘制 \" %s \"。\"))"
        % _lisp_str(d.name),
        "  (princ)",
        ")",
        "(princ \"\\n已载入。执行命令 " + command_name.upper() + " 开始绘制。\")",
        "(princ)",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 五、落盘
# ---------------------------------------------------------------------------

def write_scr(d: Drawing, path: str, encoding: str = "gbk", **kw) -> Dict[str, object]:
    """落盘 `.scr`。默认 GBK 编码——中文版 AutoCAD 的 SCRIPT 按 ANSI/GBK 读脚本，
    用 UTF-8 会让中文图层名与文字变成乱码（图层名含"仪表/仪表线"）。"""
    txt = to_scr(d, **kw)
    with open(path, "w", encoding=encoding, errors="replace", newline="\r\n") as f:
        f.write(txt)
    return {"path": os.path.abspath(path), "backend": "autocad-scr",
            "encoding": encoding, "bytes": os.path.getsize(path),
            "lines": txt.count("\n") + 1,
            "note": "在 AutoCAD 中执行 SCRIPT 命令选择本文件即可；脚本内已建图层"}


def write_lisp(d: Drawing, path: str, encoding: str = "utf-8",
               **kw) -> Dict[str, object]:
    """落盘 `.lsp`。默认 UTF-8——AutoLISP 源文件由 CAD 按本机代码页解析，
    若中文乱码，改 `encoding="gbk"` 再生成一次。"""
    txt = to_lisp(d, **kw)
    with open(path, "w", encoding=encoding, errors="replace", newline="\r\n") as f:
        f.write(txt)
    return {"path": os.path.abspath(path), "backend": "autolisp-entmake",
            "encoding": encoding, "bytes": os.path.getsize(path),
            "lines": txt.count("\n") + 1,
            "note": "APPLOAD 载入后在命令行执行 CHEMCADDRAW"}
