#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""draw_pfd.py —— 规格驱动出图：读 JSON 规格 → 建图 → 自检 → 落图。

用法:
    # 生成 DXF（无需安装 AutoCAD）
    python draw_pfd.py spec.json --out pfd.dxf

    # 只自检、不出图（CI / 交图前把关）
    python draw_pfd.py spec.json --check-only

**交付格式固定为 DWG**（用户规定）：`--out 图.dxf` 也会被改成 `图.dwg`；
需要 DXF 中间件时显式 `--backend dxf`（逃生口，不是默认）。

⚠️ **出 DWG 强制经 AutoCAD COM 端口连 CAD**（用户强制规定）：默认 `dwg` 后端
先纯 Python 写 DXF，再由 `chemcad_dwg` **经 COM 另存为 DWG**；COM 连不上就
**响亮失败**（退出码 3，并给出可执行建议），**绝不静默降级、也绝不静默换成
accoreconsole**。`accoreconsole` 只在显式设 `CHEMCAD_DWG_BACKEND=accore`
时才作为逃生口启用，默认不参与出图。

    # 直接画进运行中的 AutoCAD（需 pyautocad + pywin32）
    python draw_pfd.py spec.json --backend com [--create] [--clear-layers] [--purge]
    python draw_pfd.py --list-docs                       # 先看开了哪几张图纸
    python draw_pfd.py spec.json --backend com --doc "S-2-20-03"   # 指定活动图纸

    # 脚本通道：装不上 pywin32 也能把图落进 AutoCAD（无需任何 Python 依赖）
    python draw_pfd.py spec.json --backend scr  --out pfd.scr    # CAD 里跑 SCRIPT
    python draw_pfd.py spec.json --backend lisp --out pfd.lsp    # APPLOAD 后跑 CHEMCADDRAW

规格字段（JSON）:
    name          图纸名称
    sheet         A0/A1/A2/A3，默认 A1
    title_block   标题栏字段，见 chemcad_symbols.DEFAULT_TITLE_FIELDS
    equipment     [{id, type, at:[x,y]|"引用", tag, name, params:{}, rot, tag_annot}]
    pipes         [{id, from, to, layer, dn, grade, medium, route, via, arrows, tag_at}]
    valves        [{tag, type, on:"管线号", t:0~1, rot?, params:{}}]
    instruments   [{tag, kind, at|on+t, mount, params:{}}]
    loops         [{loop_id, variable, number, controlled_object, measurement_point,
                    controller_tag, final_element, loop_type, sub_measurement,
                    sub_controller_tag, extra_measurements, extra_finals, controller_at}]
    notes         ["说明 1", "说明 2"]
    texts         [{text, at, layer, height, align}]
    offpage       [{at, direction, drawing_no}]

**引用语法**（`from` / `to` / `at` / `measurement_point` 等一律通用）:
    "T-101.top"      设备 id + 锚点（锚点清单见 chemcad_symbols 各 eq_* 的 docstring）
    "T-101"          设备基点
    "PL-101-80@0.35" 管线上按长度比例取点
    "120,300"        字面坐标

退出码:
    0  自检通过且出图成功（或 --check-only 通过）
    1  自检有 error（规格/几何不合规，未出图）
    2  落盘后端（dwg/dxf/scr/lisp）但没给 --out
    3  --backend com 不可用（未开 AutoCAD / 缺 pyautocad+pywin32）——给出可执行建议，不抛栈
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chemcad_engine as E          # noqa: E402
import chemcad_layout as L          # noqa: E402
import chemcad_symbols as S         # noqa: E402
from chemcad_ir import Group, Polyline, Pt, Text, TEXT_H_NOTE, TEXT_H_MAIN   # noqa: E402

#: 工段框角标的臂长（mm）——必须与 `S.add_section_frame(tick=...)` 取同一个值，
#: 否则 A\* 登记的障碍与实际画出的角标对不上，避障就白做了
SECTION_TICK: float = 14.0



# ---------------------------------------------------------------------------
# 规格 → 图纸
# ---------------------------------------------------------------------------

def _shift_rect(r, dx: float, dy: float):
    """内容坐标与图纸坐标间的矩形平移。"""
    return (r[0]+dx,r[1]+dy,r[2]+dx,r[3]+dy)


def _clamp_rect(r, bounds):
    """把矩形夹进可用区域（工段框被家具挤到幅面外时收边，而不是整框丢弃）。"""
    x0, y0, x1, y1 = r
    bx0, by0, bx1, by1 = bounds
    if x0 > bx1 or y0 > by1 or x1 < bx0 or y1 < by0:
        return None
    return (max(x0, bx0), max(y0, by0), min(x1, bx1), min(y1, by1))


def _text_boxes(cd: E.ChemDrawing, skip_layers=()):
    """图上已有文字的包围盒清单（新元素的避让判据）。

    ⚠️ 必须用 `E.text_rect()`（`Text.points()` 只给基点，退化成点 → 判据恒假）。
    """
    skip = set(skip_layers)
    out = []
    for it in cd.items:
        ents = it.ents if isinstance(it, Group) else [it]
        for e in ents:
            if isinstance(e, Text) and e.layer not in skip:
                out.append(E.text_rect(e))
    return out


def _at(cd: E.ChemDrawing, v) -> Pt:
    """把规格里的位置字段（列表坐标或引用字符串）解析为点。"""
    if isinstance(v, str):
        return cd.resolve(v)
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return (float(v[0]), float(v[1]))
    raise ValueError(f"无法解析位置 {v!r}")


def _pipe_dir_rot(cd: E.ChemDrawing, line_no: str, t: float) -> float:
    """按管线在 t 处的走向给出符号旋转角（0=水平，90=竖直），保证阀门朝向随管。"""
    seg = E._segment_containing(cd.pipes[line_no], t)
    if seg is None:
        return 0.0
    (ax, ay), (bx, by) = seg
    return 0.0 if abs(bx - ax) >= abs(by - ay) else 90.0


# ---------------------------------------------------------------------------
# 图纸类型：只有 PFD
# ---------------------------------------------------------------------------
# 依据 `references/drawing-conventions.md` §1：
#   P&ID = 设备 + 管道 + 阀门 + **仪表与控制回路**（HG 20519.2）
#   PFD  = 设备 + **主物料线 + 主要参数**（低于 P&ID，不要求全仪表）
#
# **本技能只出 PFD**（用户决定：取消"一份规格出两张图 PFD + P&ID"）。
# 因此不再有"两图共用幅面/共用版面"的需求，`build_types` / `build_both` /
# `TYPE_SUFFIX` / `-PID|-PFD 后缀` 一并删净；`DRAWING_TYPES` 保留元组形式
# 只为让 `resolve_drawing_type` 的合法值判断仍是单一落点。
DRAWING_TYPES: Tuple[str, ...] = ("pfd",)

#: 版面修正的最大重建轮数。每轮都要整图重建，代价不低；实测 2~3 轮已收敛
#: （三类缺陷数单调下降，见 `build_with_layout` 的"只接受更优"判据）。
MAX_LAYOUT_ROUNDS = 3



#: 图型的中文名（标题栏用）。本技能只出 PFD，故只有一项。
TYPE_TITLE: Dict[str, str] = {"pfd": "工艺流程图（PFD）"}


def resolve_drawing_type(spec: Dict, override: Optional[str] = None) -> str:
    """图型：CLI `--drawing-type` > 规格 `drawing_type` > 默认 **`pfd`**。"""
    t = str(override or spec.get("drawing_type") or "pfd").lower()
    return t if t in DRAWING_TYPES else "pfd"


def spec_for_type(spec: Dict, dtype: str) -> Dict:
    """按图型**裁剪**规格。

    PFD 去掉的是「控制与仪表层」，不是「工艺层」：
      * 去 `valves` / `instruments` / `loops` / `signals`——PFD 不要求全仪表；
      * 管线**保留**（主物料线就是 PFD 的主体），但注记换成**流股号 + 主要参数**；
      * 流向箭头**必标**（HG 20519.2：物料线须示流向）。
    设备与 `layout` 原封不动 —— 这样两种图拿到的是**同一份版面分配**。
    """
    if dtype != "pfd":
        return spec
    streams = spec.get("streams") or {}
    pipes = []
    for p in spec.get("pipes", []):
        q = dict(p)
        # PFD 主物料线**必标流向**（HG 20519.2）：这里不是 `setdefault`，
        # 而是把显式的 0 也提上来——规格若为 P&ID 写死了 arrows:0，PFD 不该照搬
        q["arrows"] = int(q.get("arrows") or 0) or 1
        st = q.get("stream") or streams.get(str(q.get("id", "")))
        if st:
            q["stream"] = st
        pipes.append(q)
    tb = dict(spec.get("title_block") or {})
    tb.update(dict(spec.get("title_block_pfd") or {}))
    if tb.get("名称") and "流程图" not in str(tb["名称"]):
        tb["名称"] = f"{tb['名称']}　{TYPE_TITLE['pfd']}"
    return {**spec, "drawing_type": "pfd", "valves": [], "instruments": [],
            "loops": [], "signals": [], "pipes": pipes,
            "section_frames": False,
            "title_block": tb or spec.get("title_block"),
            "notes": spec.get("notes_pfd") or spec.get("notes")}


def _stream_fields(st: object) -> List[str]:
    """管旁只显示T、P与一个流量；共享符号入口的三行规则。"""
    return S.stream_parameter_fields(st)


def _draw_section_overlays(_offpage_fps, cd, dtype, notes_rect, sec_plan, sec_rows, section_frames, spec):
    """画工段成品覆盖层：工段框与段名定稿。"""
    if sec_plan:
            sdx, sdy = L.unfit_shift(cd)      # 内容坐标 + 该位移 = 图纸坐标
            from chemcad_ir import SHEET_SIZES as _SS9
            W9, H9 = _SS9.get(cd.sheet.upper(), _SS9["A1"])
            m9 = float(spec.get("margin", 10.0))
            inner9 = (m9, m9, W9 - m9, H9 - m9)
            _tbw, _tbh = E.TITLE_BLOCK_SIZE
            guards9 = [(W9 - m9 - _tbw, m9, W9 - m9, m9 + _tbh)]
            if notes_rect:
                guards9.append(tuple(float(v) for v in notes_rect))
            for _k in ("tag_index_table", "legend", "stream_table"):
                _t = cd.meta.get(_k)
                if _t and _t.get("w") and _t.get("h"):
                    _ax, _ay = float(_t["at"][0]), float(_t["at"][1])
                    guards9.append((_ax - 2.0, _ay - float(_t["h"]) - 2.0,
                                    _ax + float(_t["w"]) + 2.0, _ay + 2.0))
            skipped9: List[str] = []
            for sid, info in sec_plan.items():
                rect = _clamp_rect(_shift_rect(info["rect"], sdx, sdy), inner9)
                if rect is None or (rect[2] - rect[0]) < 8.0 or (rect[3] - rect[1]) < 8.0:
                    skipped9.append(str(sid))
                    continue
                # 段名避让清单 = 家具 + **全部设备本体**（不只是本工段的）。
                # ⚠️ 早先只收本工段设备（`info["sec"]["members"]`），在小图幅下工段挨得近，
                # 段名压在**邻工段**设备轮廓上照样报"文字压线"（实测 A1 下 sheet 03 命中 2 处：
                # `换热工段（S1）`/`换热工段 2（S4）` 各压一条 EQUIP 线）。段名落笔时并不认识
                # "这是谁家设备"，所以避让就得对着**场上所有设备**来。
                # 角标只避家具，段名两类都避。
                _dev9 = [E.group_rect(g) for g in cd.iter_groups()
                         if str(getattr(g, "kind", "")) == "equipment"]
                # 还要避**已画好的接续标志**：工段框在 step 7 之后落笔，段名压到
                # OFFPAGE 框线是实测报过的一类（"SECT 文字压 OFFPAGE 线"）。
                # 接续标志的足迹在 step 7 已逐件收进 `_offpage_fps`，平移到图纸坐标即可复用。
                _op9 = [_shift_rect(r, sdx, sdy) for r in _offpage_fps]
                _avoid9 = list(guards9) + [r for r in _dev9 if r] + _op9
                # 角标与段名**逐件取舍**：压到家具/设备就只丢那一处，不是整框一起丢
                drawn9 = S.add_section_frame(cd, rect, str(info["sec"]["name"]),
                                             tick=SECTION_TICK, tag=str(sid),
                                             avoid=_avoid9)
                if not drawn9:
                    skipped9.append(str(sid))
                    continue
                section_frames[str(sid)] = rect
            cd.meta["section_frames"] = {k: [round(float(v), 1) for v in r]
                                         for k, r in section_frames.items()}
            if skipped9:
                cd.meta["section_frames_skipped"] = sorted(skipped9)
                print(f"  · 工段标记避让：{sorted(skipped9)} 与说明框/表格/标题栏相交，"
                      f"已跳过（其设备仍在该区，只是不画角标与段名）")

def _draw_std_tables(cd, dtype, notes_rect, sec_rows, spec):
    """物料表独立置于右侧；不生成设备位号表或图例。"""
    if not spec.get("std_tables", True):
        return
    from chemcad_ir import SHEET_SIZES
    W, H = SHEET_SIZES.get(cd.sheet.upper(), SHEET_SIZES["A1"])
    streams = spec.get("streams") or {}
    if dtype != "pfd" or not spec.get("stream_table", True) or not streams:
        return
    datasets = []
    used = set()
    if spec.get("section_stream_boxes", True):
        names = {str(sec["id"]):str(sec["name"]) for sec in L.split_sections(spec)}
        keys = {id(st):key for key, st in streams.items()}
        for sid, rows in sec_rows.items():
            datasets.append((names.get(sid, sid),
                             [dict(st, no=st.get("no") or keys[id(st)]) for st in rows]))
            used.update(id(st) for st in rows)
    remaining = [dict(st, no=st.get("no") or key) for key, st in streams.items()
                 if id(st) not in used]
    if remaining:
        datasets.append(("界区及未分段物流" if datasets else "", remaining))
    # 只用一个用户指定的A1文本框；框间净距150mm、底边Y相同。
    profile = S.text_frame_profile()
    panel_w, panel_h = map(float,profile["size"])
    left, bottom, right, top = map(float,profile["content_rect"])
    x, y = W + 150.0, top
    limit = max(1,min(25,int(spec.get("section_box_max_rows",25))))
    tables = []
    for name, rows in datasets:
        start = 0
        while start < len(rows):
            end = min(len(rows),start+limit)
            title = "物料平衡表" + ("——"+name if name else "")
            while end > start:
                chunk = rows[start:end]
                scratch = E.ChemDrawing(sheet="A1")
                w,h = S.add_balance_table(scratch,(0.0,0.0),chunk,title=title)
                if max(w,S._text_w(title,TEXT_H_NOTE)) <= right-left:
                    break
                end -= 1
            if end == start or y-h < bottom:
                raise ValueError("物料平衡表超出默认A1文本框容量；禁止自动续框或缩小字体")
            tables.append({"title":title,"streams":[str(st.get("no", "")) for st in chunk],
                           "at":[x+left,y],"rect":[x+left,y-h,x+left+w,y],"rows":chunk})
            y -= h+12.0
            start = end
    # 全部量尺完成后落笔，失败不能留下半张物料表。
    S.add_user_text_frame(cd,(x,0.0),fields=spec.get("title_block"),
                          title="物料平衡表",number=int(spec.get('_book_page_number',2)),
                          total=int(spec.get('_book_page_total',2)))
    for table in tables:
        S.add_balance_table(cd,tuple(table["at"]),table.pop("rows"),title=table["title"])
    cd.meta["external_balance_panels"] = [{"title":"物料平衡表","at":[x,0.0],
        "rect":[x,0.0,x+panel_w,panel_h],"gap_x":150.0,
        "streams":[no for table in tables for no in table["streams"]],"tables":tables}]


def _draw_battery_connectors(_avoid_offpage, _ob_bounds, _offpage_fps, cd, spec):
    """画进出界区的接续标志（单端流股），含避让与归属文字。"""
    for _b in spec.get("battery", []):
            # 界区标志落在**配对设备之外**（沿方向让开本体 gap）——不落在设备中心，
            # 否则与本体相撞（实测重叠 12~15 mm，被"符号重叠"判 error 拒绝出图）。
            _eq = next((str(x) for x in (_b.get("pair") or []) if str(x)), "")
            _dir = str(_b.get("direction", "right"))
            _body = E.body_rect_of(cd, _eq) if _eq else None
            if _body:
                _r0 = float(getattr(S, "CONNECTOR_PLANT_R", 5.0))
                _mx, _my = (_body[0] + _body[2]) / 2.0, (_body[1] + _body[3]) / 2.0
                _m = _r0 + 6.0            # 标志大致占位半径（用于碰撞预判）
                # 八向 × 三档距离：从设备本体外沿起算，逐个试，取第一个不撞的落点。
                # 密集张（一张上 6 个界区标志）靠单一方向必然撞，得给足候选。
                _dirs = {"right": (1, 0), "left": (-1, 0), "up": (0, 1), "down": (0, -1),
                         "ur": (1, 1), "ul": (-1, 1), "dr": (1, -1), "dl": (-1, -1)}

                def _cand_pt(d: str, k: int):
                    sx, sy = _dirs[d]
                    gx = _body[2] + _m + (k - 1) * 2.0 * _m if sx > 0 else (
                        _body[0] - _m - (k - 1) * 2.0 * _m if sx < 0 else
                        (_body[0] + _body[2]) / 2.0)
                    gy = _body[3] + _m + (k - 1) * 2.0 * _m if sy > 0 else (
                        _body[1] - _m - (k - 1) * 2.0 * _m if sy < 0 else
                        (_body[1] + _body[3]) / 2.0)
                    return (gx, gy)

                _order = ([_dir] if _dir in _dirs else []) + \
                         [d for d in _dirs if d != _dir]
                _pt = _cand_pt(_dir if _dir in _dirs else "right", 1)
                _found = False
                for _k in (1, 2, 3):
                    for _d in _order:
                        _q = _cand_pt(_d, _k)
                        _r = (_q[0] - _m, _q[1] - _m, _q[0] + _m, _q[1] + _m)
                        if not S._rect_hits_any(_r, _avoid_offpage):
                            _pt, _dir, _found = _q, _d, True
                            break
                    if _found:
                        break
            else:
                _pt = _at(cd, _b.get("at", _eq)) if (_b.get("at") or _eq) else None
            if _pt is None:
                continue
            # 连接器只认四个正方向（对角候选只用于"落点在哪儿"，朝向就近归到正方向）
            _dir_conn = {"ur": "right", "ul": "left", "dr": "right", "dl": "left"}.get(_dir, _dir)
            fp = S.add_offpage_connector(
                cd, _pt, _dir_conn, "",
                kind="plant",
                stream_no=str(_b.get("stream_no", "")),
                stream_data=_b.get("stream_data"),
                dest=str(_b.get("dest", "")),
                body=_body,
                avoid=_avoid_offpage,
                bounds=_ob_bounds)
            if fp is not None:
                _avoid_offpage.append(fp)
                _offpage_fps.append(fp)

def _stream_placement(rec, q):
    return 'right' if any(abs(a[0]-b[0])<1e-6 and abs(q[0]-a[0])<1e-6
                          and min(a[1],b[1])<q[1]<max(a[1],b[1])
                          for a,b in rec.segments) else 'top'


def _stream_candidate_points(rec, pt, half):
    points=[pt]+[rec.straight_point_at(t/24,half+1.0) for t in range(25)]
    for a,b in rec.segments:
        length=math.dist(a,b)
        if length < 2*(half+1.0):
            continue
        count=max(1,math.ceil((length-2*(half+1.0))/2.0))
        for i in range(count+1):
            u=(half+1.0+(length-2*(half+1.0))*i/count)/length
            points.append((a[0]+u*(b[0]-a[0]),a[1]+u*(b[1]-a[1])))
    return list(dict.fromkeys(points))


def _route_pipes(astar, cd, dtype, explicit, plan_via, routes, spec, vias):
    """逐条管线定线：优先显式 `via`，否则走 A\\* 避障布线。"""
    for p in spec.get("pipes", []):
            line_no = str(p["id"])
            via = None
            mode = str(p.get("route", "auto"))
            if line_no not in explicit:
                r = astar.get(line_no)
                if r is not None:
                    if not r.ok or len(r.path) < 2:
                        raise ValueError(f"流股 {line_no} 自动寻路失败：{r.note}")
                    via = [list(q) for q in r.path[1:-1]]
                    # A* 已包含实际接管段；再套内部stub提示会折返并改变安全路径。
                    mode = "auto"
                elif line_no in vias:
                    via = vias[line_no]
                    mode = str(routes.get(line_no, mode))
                else:
                    if line_no in plan_via:
                        via = plan_via[line_no]
                    mode = str(routes.get(line_no, mode))
            cd.pipe(line_no, str(p["from"]), str(p["to"]),
                    layer=str(p.get("layer", "PIPE_PRI")),
                    mode=mode,
                    via=via if via is not None else p.get("via"),
                    dn=str(p.get("dn", "")), grade=str(p.get("grade", "")),
                    medium=str(p.get("medium", "")),
                    # 绝热代号（H 保温 / P 保冷 / T 伴热）：评审标准 3.2.6 单列 0.2 分，
                    # 且注明"有此项而没有标注的不得分"——所以规格里写了就必须进组合号。
                    insulation=str(p.get("insulation", "")),
                    arrows=0)
    # 所有管线落笔后再选流股标注位置，固定尺寸，不能只挪框而拉长引线。
    annotation_order=list(spec.get('pipes',[]))
    if dtype=='pfd':
        # 先放净空候选少的标注，避免宽裕流股先占满短连接唯一可用的位置。
        all_segments=[(a,b,E.resolve_layer(e.layer),getattr(e,'note','')) for e in cd.iter_ents()
                      if E.resolve_layer(e.layer) in ('PIPE_PRI','PIPE_INLINE','AF_PIPE')
                      for a,b in E.entity_segments(e)]
        equip_rects=[E.group_rect(g) for g in cd.iter_groups() if g.kind=='equipment']
        def available_slots(p):
            st=p.get('stream')
            if not st and not p.get('stream_no'):
                return float('inf')
            rec=cd.pipes[str(p['id'])]; half=S.STREAMNO_DIAMOND_HALF
            target=rec.straight_point_at(float(p.get('stream_at',p.get('tag_at',.5))),half+1)
            count=0
            for q in _stream_candidate_points(rec,target,half):
                temp=E.ChemDrawing()
                group=S.add_stream_data(temp,q,fields=_stream_fields(st),placement=_stream_placement(rec,q))
                group.ref='stream:'+str(p['id'])
                if E.stream_annotation_pipe_hits(group,all_segments):
                    continue
                if any(r and E.rect_gap(part,r)<E.STREAM_ANNOTATION_GAP-1e-6
                       for _,part in E.stream_annotation_parts(group) for r in equip_rects):
                    continue
                count+=1
            return count
        annotation_order.sort(key=available_slots)
    for p in annotation_order:
            line_no = str(p["id"])
            n_ar = int(p.get("arrows", 0))
            t_tag = float(p.get("stream_at", p.get("tag_at", 0.5)))
            avoid = []
            clearance = 0.0
            if dtype == "pfd":
                st = p.get("stream")
                if st or p.get("stream_no"):
                    rec = cd.pipes[line_no]
                    no = str((st or {}).get("no", p.get("stream_no", ""))
                             if isinstance(st, dict) else p.get("stream_no", ""))
                    if not no.strip():
                        raise ValueError(f"流股 {line_no} 缺少编号，无法按截图绘制菱形流股号")
                    if spec.get("stream_diamond",True) is not True or float(spec.get("stream_diamond_scale",1.0)) != 1.0:
                        raise ValueError("流股信息只能使用用户截图样式，不能关闭菱形或改变比例")
                    diamond = bool(no)
                    scale = float(spec.get("stream_diamond_scale", 1.0))
                    if scale <= 0:
                        raise ValueError("物流编号框比例必须大于零")
                    half = S.STREAMNO_DIAMOND_HALF * scale
                    pt = (cd.meta.get('reserved_stream_points') or {}).get(line_no) or (rec.straight_point_at(t_tag, half + 1.0) if diamond else rec.point_at(t_tag))
                    if diamond:
                        # 中点容不下箭头时，在同一路径两端的直线净空中选位。
                        if n_ar and len(cd._auto_arrows(rec, n_ar, [pt], half + 4.0)) < n_ar:
                            candidates = [rec.straight_point_at(t, half + 1.0) for t in (0.0, 1.0)]
                            free = [q for q in candidates if len(cd._auto_arrows(rec, n_ar, [q], half + 4.0)) == n_ar]
                            if free:
                                pt = min(free, key=lambda q: math.dist(q, pt))
                        points = _stream_candidate_points(rec,pt,half)
                        def placement_at(q):
                            return _stream_placement(rec,q)
                        obstacles = []
                        for g in cd.iter_groups():
                            if g.name == 'STREAM_DATA':
                                obstacles += [r for _,r in E.stream_annotation_parts(g)]
                            elif g.kind in ("equipment","annotation"):
                                r = E.group_rect(g)
                                if r is not None:
                                    obstacles.append(r)
                        segments = [(a,b,E.resolve_layer(e.layer),getattr(e,'note',''))
                                    for e in cd.iter_ents()
                                    if E.resolve_layer(e.layer) not in E._TEXT_LINE_SKIP
                                    for a,b in E.entity_segments(e)]
                        pipe_segments = [s for s in segments if s[2] in ('PIPE_PRI','PIPE_INLINE','AF_PIPE')]
                        def annotation_cost(q):
                            temp = E.ChemDrawing()
                            group = S.add_stream_data(temp,q,fields=_stream_fields(st),placement=placement_at(q))
                            group.ref = 'stream:' + line_no
                            hits = len(E.stream_annotation_pipe_hits(group,pipe_segments))
                            parts = E.stream_annotation_parts(group)
                            hits += sum(E.rect_gap(r,obstacle)<E.STREAM_ANNOTATION_GAP-1e-6
                                        for _,r in parts for obstacle in obstacles)
                            # 非管线实体也不能压参数文字或菱形编号。
                            cells = [E.expand_rect(E.text_rect(e),E.TEXT_LINE_GAP)
                                     for e in group.ents if isinstance(e,Text)]
                            number = E.expand_rect(E.text_rect(Text(layer="TAG",text=no.zfill(4) if no.isdigit() else no,
                                                      pt=q,height=3.0,align="middle_center")),E.TEXT_LINE_GAP)
                            hits += sum(E.seg_inside_rect_len(a,b,r)>1e-9
                                        for r in cells+[number] for a,b,layer,_ in segments
                                        if layer not in ('PIPE_PRI','PIPE_INLINE','AF_PIPE'))
                            if n_ar and len(cd._auto_arrows(rec,n_ar,[q],half+4.0))<n_ar:
                                hits += 1000
                            return (hits,math.dist(q,pt))
                        pt = min(points,key=annotation_cost)
                        S.add_stream_no_diamond(cd, pt, no,
                            digits=int(spec.get("stream_no_digits", 4)), scale=scale)
                        cd.meta.setdefault("stream_diamond_pts", []).append(pt)
                        avoid, clearance = [pt], half + 4.0
                    group = S.add_stream_data(cd, pt, no="" if diamond else no,
                                              fields=_stream_fields(st),placement=placement_at(pt))
                    group.ref = 'stream:' + line_no
            elif p.get("tag_at") is not None or p.get("tag"):
                cd.tag_pipe(line_no, t=float(p.get("tag_at", 0.5)))
            if n_ar:
                cd.add_arrows(line_no, n_ar, avoid=avoid, clearance=clearance)


def _draw_section_frames(_frame_obstacles, _secs, cd, dtype, sec_plan, sec_rows, spec):
    """画工段框与段名（框住工段内的设备，给读图人一个分区）。"""
    if spec.get("section_frames", False) and _secs:
            pad = float(spec.get("section_pad", 14.0))
            member: Dict[str, Dict] = {}
            for _sec in _secs:                                 # type: ignore[union-attr]
                for _m in _sec.get("members") or []:
                    member[str(_m)] = _sec
            bysec: Dict[str, Dict] = {}
            for g in cd.iter_groups():
                if str(getattr(g, "kind", "")) != "equipment":
                    continue
                _s = member.get(E.obj_label(g))
                _r = E.group_rect(g)
                if _s and _r:
                    bysec.setdefault(str(_s["id"]), {"sec": _s, "rs": []})["rs"].append(_r)
            for sid, info in bysec.items():
                rs = info["rs"]                                # type: ignore[assignment]
                rect = (min(r[0] for r in rs) - pad, min(r[1] for r in rs) - pad,
                        max(r[2] for r in rs) + pad, max(r[3] for r in rs) + pad)
                sec_plan[str(sid)] = {"sec": info["sec"], "rect": rect}    # type: ignore[index]
                # 角标与段名的占位 → 交给 A*，让管线绕开（否则会压到框线上）
                for (cx, cy, sx, sy) in ((rect[0], rect[1], 1, 1), (rect[2], rect[1], -1, 1),
                                         (rect[0], rect[3], 1, -1), (rect[2], rect[3], -1, -1)):
                    xs = [cx, cx + sx * SECTION_TICK]
                    ys = [cy, cy + sy * SECTION_TICK]
                    _frame_obstacles.append((min(xs), min(ys), max(xs), max(ys)))
                _lbl = f"{info['sec']['name']}（{sid}）"                     # type: ignore[index]
                _lw = S._text_width(_lbl, TEXT_H_NOTE)
                _lx = (rect[0] + rect[2]) / 2.0
                _ly = rect[3] - TEXT_H_NOTE - 1.0
                _frame_obstacles.append((_lx - _lw / 2.0, _ly - TEXT_H_NOTE / 2.0,
                                         _lx + _lw / 2.0, _ly + TEXT_H_NOTE / 2.0))

            # 各工段流股归属用于独立物料表分组；未归属流股另列，不能遗漏。
            streams = spec.get("streams") or {}
            if streams and dtype == "pfd" and spec.get("section_stream_boxes", True):
                ids = {str(e.get("id")) for e in spec.get("equipment", [])}
                owner_of: Dict[str, str] = {}
                for p in spec.get("pipes", []):
                    head = str(p.get("from", "")).partition(".")[0]
                    if head in ids:
                        owner_of[str(p.get("id"))] = head
                for sid, info in sec_plan.items():
                    mem = {str(m) for m in (info["sec"].get("members") or [])}   # type: ignore[index]
                    rows = [streams[k] for k in streams if owner_of.get(str(k)) in mem]
                    if rows:
                        sec_rows[str(sid)] = rows

def build(spec: Dict, hints: Optional[Dict] = None) -> Tuple[E.ChemDrawing, List[E.ControlLoop]]:
    """按规格建图。返回 (图纸, 回路列表)。

    `hints` 是**版面修正提示**（`chemcad_layout` 生成的）：设备微调位移、
    管线走线方式/绕行点、阀门沿管线的 `t`。空/None 即"按规格原样建"。
    迭代修正靠它把三类版面缺陷逐轮压下去（见 `build_with_layout`）。
    """
    import chemcad_block_library as BL
    BL.preflight(spec)
    hints = hints or {}
    lay = dict(spec.get("layout") or {})
    mode = str(hints.get("mode") or lay.get("mode", "off")).lower()
    want_plan = mode in ("auto", "report")
    plan = L.plan_layout(spec) if want_plan else None
    apply_layout = mode == "auto"

    # 工段划分**独立于排版模式**：用户工作流是「工段设备分配完 → 画框图」，
    # 分不分工段跟"要不要 auto 排版"是两件事。`layout.mode=off`（用 bkp 自带坐标）
    # 也得能画工段框，所以这里单独算一份 `sections`（复用 `L.split_sections`，
    # 三级优先：显式 `sections` → 设备 `section` 字段 → 启发式划分）。
    _sections = None
    if spec.get("section_frames", False) or spec.get("sections") or spec.get("section_stream_boxes", True):
        _sections = L.split_sections(spec)

    cd = E.ChemDrawing(name=str(spec.get("name", "PFD")), sheet=str(spec.get("sheet", "A1")))
    # 拓扑结点：不出图元，但管线要落在它们身上——登记为自由点（见 `ChemDrawing.resolve`）。
    cd.nodes = {str(k): (float(v[0]), float(v[1]))
                for k, v in (((spec.get("_nodes") or {}).get("xy")) or {}).items()}
    dtype0 = resolve_drawing_type(spec)
    if "流程图" not in cd.name:
        cd.name = f"{cd.name}　{TYPE_TITLE[dtype0]}"
    if plan and plan.get("sheet"):
        cd.sheet = str(plan["sheet"])                 # layout.sheet=auto 时图幅由引擎定
    cd.meta = {"spec": spec.get("name", ""), "layout_mode": mode,
               "drawing_type": dtype0,
               "layout_enforced": bool(mode == "auto" or lay.get("enforce")),
               "layout_plan": plan, "layout_hints": hints}

    # 1) 图框与标题栏在**最后**添加（见步骤 9）：它们是「图纸家具」，不参与内容居中平移。
    #    内容先建好，再由 E.fit_to_sheet 整体平移居中（见步骤 8）。

    # 2) 设备
    #    坐标来源三级：① `layout.mode=auto` 的排版计划（不给 at 的设备）
    #                 ② 规格里写死的 `at`（视为钉住，作者给了坐标就是给了意图）
    #                 ③ 都没有 → 落在原点，由计划接管
    placements = (plan or {}).get("placements") or {}
    eq_off = hints.get("equipment_offset") or {}
    eq_entries: List[Tuple[Dict, object]] = []
    for eq in spec.get("equipment", []):
        eid = str(eq["id"])
        off = eq_off.get(eid) or [0.0, 0.0]
        pinned = eq.get("at") is not None and not lay.get("force")
        if apply_layout and eid in placements and not pinned:
            base = (float(placements[eid][0]), float(placements[eid][1]))
        elif eid in placements and not pinned:
            # report 模式：只统计不动图，仍用规格坐标（没有就落在原点）
            base = _at(cd, eq["at"]) if eq.get("at") is not None else \
                (float(placements[eid][0]), float(placements[eid][1]))
        else:
            base = _at(cd, eq["at"]) if eq.get("at") is not None else (0.0, 0.0)
        at = (base[0] + float(off[0]), base[1] + float(off[1]))
        g = BL.place(cd, eq, at, rot=float(eq.get("rot", 0.0)),
                     tag=str(eq.get("tag", "")))
        eq_entries.append((eq, g))

    # 2b) 标注按真实块边界落位；有集中名称表时，设备近旁仅保留短位号。
    index_enabled = True  # 设备附近保留短位号，完整名称移到底部。
    for eq, g in eq_entries:
        r = E.group_rect(g)
        if r is None:
            raise ValueError(f"设备 {eq['id']} 缺少可测量的图块边界")
        S.add_equipment_labels(cd, eq, r, index_enabled,equipment=g)
        if eq.get("level"):
            S.add_level_mark(cd, (g.base[0], g.base[1]), str(eq["level"]))

    # 3) 管线：设计者显式路线优先；自动路径由A*统一执行设备与净距约束。
    #    只出 PFD，故管线一律注**流股号 + 主要参数**（不再有管道号+等级那套）
    dtype = resolve_drawing_type(spec)
    routes = dict(hints.get("pipe_route") or {})

    # 3a0) **工段框的"选址"**（画框动作在步骤 9c）
    #      本处只做两件事：① 算出每个工段的设备包围盒；② 把框的角标与段名
    #      **登记成 A\* 的硬障碍**，让管线绕开它们。真正落笔在步骤 9c。
    #      ⚠️ **为什么必须拆成两段**：工段框在 `fit_to_sheet` **之前**算出来的是
    #      **内容坐标**，而说明框/表格/图框是 `fit_to_sheet` **之后**才画的
    #      **图纸坐标**——两边不同坐标系，相交判断没有意义。早先在这里直接画，
    #      实测漏判，另一份规格仍报 4 处"SECT 线压说明文字"。
    #      拆开之后：选址在内容坐标（管线避障需要），落笔在图纸坐标（家具避让需要），
    #      靠 `fit.shift` 这个纯平移把两者对齐。
    section_frames: Dict[str, object] = {}
    sec_plan: Dict[str, Dict] = {}
    sec_rows: Dict[str, object] = {}
    _frame_obstacles: List[object] = []
    _secs = (plan or {}).get("sections") or _sections
    _draw_section_frames(_frame_obstacles, _secs, cd, dtype, sec_plan, sec_rows, spec)

    #     必须在画管线**之前**跑——此刻图上的障碍（设备本体 + 设备位号文字）
    #     才是干净的；管线自己带来的那批文字由迭代修正逐轮"学"回来（route_avoid）。
    astar: Dict[str, object] = {}
    if spec.get("route_astar", True) and not hints.get("no_astar"):
        try:
            import chemcad_route as RT
            astar = RT.route_pipes(cd, spec,
                                   extra_hard=(list(hints.get("route_avoid") or [])
                                               + _frame_obstacles))
        except Exception as exc:
            raise RuntimeError(f"自动避障寻路未完成：{type(exc).__name__}: {exc}") from exc

    # 走线优先级：用户显式route/via → 已通过A*的完整路径 → 内部修正提示 → ELK。
    # A*路径含接管，不能再套stub模式；迭代障碍用route_avoid交给A*重算。
    plan_via: Dict[str, object] = dict((plan or {}).get("pipe_via") or {}) if apply_layout else {}
    vias: Dict[str, object] = dict(hints.get("pipe_via") or {})
    for line_no in list(vias):          # `via` 会压过 `route`，两者不能同时留
        if line_no in routes:
            vias.pop(line_no, None)
    explicit = {str(p.get("id")) for p in spec.get("pipes", [])
                if p.get("route") or p.get("via")}
    _route_pipes(astar, cd, dtype, explicit, plan_via, routes, spec, vias)
    # **菱形框处把管线断开**（浙大实测：主线在菱形左右顶点断开，缺口=当前菱形宽 14.14）。
    # 放在画完管线之后：此刻管子都在，才找得到"被菱形盖住的那一段"。
    _dpts = cd.meta.get("stream_diamond_pts") or []
    if spec.get("stream_diamond", True) and _dpts:
        try:
            _gap = 2.0 * S.STREAMNO_DIAMOND_HALF * float(spec.get("stream_diamond_scale", 1.0))
            nb = E.break_lines_at(cd, [tuple(q) for q in _dpts], gap=_gap)
            cd.meta["stream_diamond_breaks"] = nb
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠ 菱形框处断线未执行：{type(exc).__name__}: {exc}", file=sys.stderr)
    cd.meta["astar"] = _astar_summary(astar)

    # 4) 阀门（沿管线定位，朝向随管；`t` 可由版面修正提示微调以避开相撞）
    slide = hints.get("valve_t") or {}
    for v in spec.get("valves", []):
        tag = str(v.get("tag", ""))
        if v.get("on"):
            t = float(slide.get(tag, v.get("t", 0.5)))
            at = cd.pipes[str(v["on"])].point_at(t)
            rot = float(v.get("rot", _pipe_dir_rot(cd, str(v["on"]), t)))
        else:
            at = _at(cd, v["at"])
            rot = float(v.get("rot", 0.0))
        g = cd.place(tag or f"V{len(cd.objects)}", str(v.get("type", "gate")), at,
                     rot=rot, tag=tag, **dict(v.get("params", {})))
        g.meta["on"] = str(v.get("on", ""))
        g.meta["t"] = t if v.get("on") else None
        if tag:
            cd.objects[tag] = g

    # 5) 仪表与检测元件
    #    `instrument_off` 是**法向让位**：气泡骑在管线上画时，管子贴着设备走就必然与设备相交，
    #    沿管线滑多远都没用——真实图纸里气泡本来就带引线挂在管侧。
    ins_off = hints.get("instrument_off") or {}
    for ins in spec.get("instruments", []):
        tag = str(ins.get("tag", ""))
        if ins.get("on"):
            t = float(slide.get(tag, ins.get("t", 0.5)))
            at = cd.pipes[str(ins["on"])].point_at(t)
            rot = float(ins.get("rot", _pipe_dir_rot(cd, str(ins["on"]), t)))
        else:
            at = _at(cd, ins["at"])
            rot = float(ins.get("rot", 0.0))
        o = ins_off.get(tag)
        if o:
            at = (at[0] + float(o[0]), at[1] + float(o[1]))
        params = dict(ins.get("params", {}))
        if "mount" in ins:
            params.setdefault("mount", str(ins["mount"]))
        g = cd.place(tag or f"I{len(cd.objects)}", str(ins.get("kind", "bubble")), at,
                     rot=rot, tag=tag, **params)
        g.meta["on"] = str(ins.get("on", ""))
        g.meta["t"] = t if ins.get("on") else None
        if tag:
            cd.objects[tag] = g

    # 6) 控制回路（定义即布线 —— 信号线由回路生成，闭环不可能被漏掉）
    #    `controller_at` 可以是**引用**（如 "PL-FEED@0.70"），配合 `controller_offset`
    #    做相对偏移——这样自动排版移动设备后，控制器位置会跟着走，不会变成魔数。
    loops: List[E.ControlLoop] = []
    for lp in spec.get("loops", []):
        d = {k: lp[k] for k in lp if k not in ("controller_at", "sub_controller_at",
                                               "controller_offset", "sub_controller_offset")}
        loop = E.ControlLoop(**d)  # type: ignore[arg-type]
        loops.append(loop)
        ctrl = _at(cd, lp["controller_at"]) if lp.get("controller_at") else None
        o = (hints.get("controller_offset") or {}).get(str(lp.get("loop_id", ""))) \
            or lp.get("controller_offset")
        if ctrl and o:
            ctrl = (ctrl[0] + float(o[0]), ctrl[1] + float(o[1]))
        sub = _at(cd, lp["sub_controller_at"]) if lp.get("sub_controller_at") else None
        so = (hints.get("controller_offset") or {}).get(str(lp.get("loop_id", "")) + "_sub") \
            or lp.get("sub_controller_offset")
        if sub and so:
            sub = (sub[0] + float(so[0]), sub[1] + float(so[1]))
        E.wire_loop(cd, loop, controller_at=ctrl, sub_controller_at=sub,
                    bubble_mount=str(lp.get("mount", "field")))

    # 6b) 自由信号线/引线（不在回路定义内的仪表连接线）
    for s in spec.get("signals", []):
        layer = str(s.get("layer", "仪表线"))
        p1, p2 = cd.resolve(str(s["from"])), cd.resolve(str(s["to"]))
        path = E.ortho_path(p1, p2, mode=str(s.get("route", "auto")), via=s.get("via"))
        for a, b in zip(path[:-1], path[1:]):
            cd.line(a, b, layer)

    # 7) 跨图接续 / 自由文字 / 说明框
    #    `kind`：cross=进/出**不同工段**（长框）、same=进/出**相同工段**（短框）、
    #    plant=进/出**工厂界区**（剖线圆）。三种画法实测自 Drawing1.dwg 的图例。
    #    offpage 的 `at` 在拆张规格里写的是**设备 id**（解析成设备中心），
    #    直接落笔会把接续框压在设备身上——所以逐件算出设备本体 `body` 交给符号库，
    #    由它把整只标志（框+箭头+物流标注）推到本体之外，并避开已画过的东西。
    _avoid_offpage: List = []
    for _e in spec.get("equipment", []):
        _b = E.body_rect_of(cd, str(_e.get("id") or _e.get("tag") or ""))
        if _b is not None:
            _avoid_offpage.append(_b)
    _avoid_offpage.extend(_text_boxes(cd))
    _cb = cd.content_bbox()
    _ob_bounds = (_cb[0] - 60.0, _cb[1] - 60.0, _cb[2] + 60.0, _cb[3] + 60.0) \
        if _cb else None
    _offpage_fps: List = []          # 只收**接续标志自己的足迹**（给工段框避让用）
    for op in spec.get("offpage", []):
        _aid = str(op["at"])
        fp = S.add_offpage_connector(
            cd, _at(cd, op["at"]), str(op.get("direction", "auto")),
            str(op.get("drawing_no", "")),
            kind=str(op.get("kind", "cross")),
            stream_no=str(op.get("stream_no", "")),
            stream_data=op.get("stream_data"),
            dest=str(op.get("dest", "")),
            body=E.body_rect_of(cd, _aid),
            avoid=_avoid_offpage,
            bounds=_ob_bounds)
        if fp is not None:
            _avoid_offpage.append(fp)
            _offpage_fps.append(fp)
    # **进出界区标志**（单端流股：原料进 / 产品出）——剖面线圆 + 箭头 + 「自界区来/去界区」。
    # 与跨张接续分开画：跨张是"去另一张图"，界区是"去工厂界区"，两者语义不同（见 BATTERY 层）。
    _draw_battery_connectors(_avoid_offpage, _ob_bounds, _offpage_fps, cd, spec)
    for t in spec.get("texts", []):
        cd.text(str(t["text"]), _at(cd, t["at"]), layer=str(t.get("layer", "MISC")),
                height=float(t.get("height", TEXT_H_MAIN)),
                align=str(t.get("align", "left")),
                rotation=float(t.get("rotation", 0.0)))

    # 8) 版面：把图面内容整体平移居中 —— 不缩放，故文字高度/线宽/正交关系均不受影响
    fit = {}
    if spec.get("auto_fit", True):
        fit = E.fit_to_sheet(cd, sheet=cd.sheet,
                             margin=float(spec.get("margin", 10.0)),
                             center=bool(spec.get("auto_fit_center", True)))
        # 底部名称行与标题栏处于同一高度；流程内容整体左移，保持设备对应X。
        from chemcad_ir import SHEET_SIZES
        W = SHEET_SIZES.get(cd.sheet.upper(), SHEET_SIZES['A1'])[0]
        spans = []
        for eq in spec.get('equipment', []):
            if eq.get('folded'):
                continue
            h = float(eq.get('tag_height', S.EQUIPMENT_TAG_H))
            width = max(S._text_width(str(eq.get('tag') or eq['id']), h),
                        S._text_width(str(eq.get('name', '')), h))+2.
            x = E.equipment_label_point(cd.objects[str(eq['id'])])[0]
            spans.append((x-width/2., x+width/2.))
        if spans:
            right = W-float(spec.get('margin', 10.))-E.TITLE_BLOCK_SIZE[0]-2.
            dx = min(0., right-max(span[1] for span in spans))
            if dx:
                cd.translate(dx, 0.)
                fit['shift'][0] += dx
                fit['content_after'] = list(cd.content_bbox())
            fit['overflow'] = bool(fit.get('overflow') or min(span[0]+dx for span in spans)<25.)
    cd.meta["fit"] = fit

    # 9) 图框、标题栏、说明框（图纸家具，用图纸坐标，最后落）
    frame_fields = dict(spec.get("title_block") or {})
    frame_fields["共几张"] = "2" if (spec.get("std_tables",True) and spec.get("stream_table",True)
                                   and spec.get("streams")) else "1"
    if spec.get('_book_page_total'):
        frame_fields['共几张']=str(spec['_book_page_total'])
        frame_fields['第几张']=str(spec['_book_page_number'])
    S.add_sheet_frame(cd,sheet=cd.sheet,fields=frame_fields)
    for eq in spec.get('equipment', []):
        S.add_equipment_footer(cd, cd.objects[str(eq['id'])], eq)
    notes_rect = None
    if spec.get("notes"):
        from chemcad_ir import SHEET_SIZES
        H = SHEET_SIZES.get(cd.sheet.upper(), SHEET_SIZES["A1"])[1]
        pos = spec.get("notes_at", [30.0, H - 16.0])
        S.add_notes_box(cd, (float(pos[0]), float(pos[1])),
                        title=str(spec.get("notes_title", "说明")),
                        lines=list(spec["notes"]),
                        width=float(spec.get("notes_width", 130.0)))
        notes_rect = _largest_closed_box(cd, "MISC")

    # 9b) 名称清单与右侧独立物料表（不生成图例）。
    _draw_std_tables(cd, dtype, notes_rect, sec_rows, spec)

    # 9c) 工段框与段名定稿；物料表不进入流程图区。
    #     顺序即语义：先框（这几台设备属同一工段）→ 框里放设备 → 再另加框写流股。
    #     ⚠️ **必须落在 `fit_to_sheet` 之后**：此刻设备与"图纸家具"（说明框 / 表格 /
    #     标题栏）同为**图纸坐标**，避让判据才成立。选址在步骤 3a0（内容坐标，给 A\*
    #     避障用），落笔在这里，用 `fit.shift` 这个纯平移把两者对齐——
    #     早先两步合成一步、在内容坐标里跟家具比相交，实测漏判，报 4 处
    #     "SECT 线压说明文字"。**同一坐标系是这条判据的前提，省不得。**
    _draw_section_overlays(_offpage_fps, cd, dtype, notes_rect, sec_plan, sec_rows, section_frames, spec)

    # 10) 版面复核：内容与**图纸家具**是否打架。
    #     `fit_to_sheet` 只看"内容尺寸 vs 幅面"，它**看不到说明框占掉的那一块**——
    #     内容刚好塞进幅面、却压在说明框上时，它的 `overflow` 仍是 False（实测踩过）。
    #     所以这里拿"居中平移后的内容包围盒"与说明框/标题栏**实算**一次。
    fit = cd.meta.get("fit") or {}
    ca = fit.get("content_after")
    if ca:
        from chemcad_ir import SHEET_SIZES
        W2, H2 = SHEET_SIZES.get(cd.sheet.upper(), SHEET_SIZES["A1"])
        m2 = float(spec.get("margin", 10.0))
        tw, th = E.TITLE_BLOCK_SIZE
        crect = (float(ca[0]), float(ca[1]), float(ca[2]), float(ca[3]))
        title_rect = (W2 - m2 - tw, m2, W2 - m2, m2 + th)
        fit["overlaps_notes"] = bool(notes_rect) and E.rect_gap(crect, notes_rect) < 0.0
        fit["overlaps_title_block"] = E.rect_gap(crect, title_rect) < 0.0
        cd.meta["fit"] = fit

    if dtype == 'pfd':
        E.apply_crossing_breaks(cd, gap=float(spec.get('crossing_gap', 4.0)))
    return cd, loops


def _largest_closed_box(cd: E.ChemDrawing, layer: str):
    """找出某图层上面积最大的封闭多段线（用于量说明框的占位）。"""
    best = None
    best_a = 0.0
    for it in cd.items:
        ents = it.ents if isinstance(it, Group) else [it]
        for e in ents:
            if isinstance(e, Polyline) and e.closed and e.layer == layer:
                r = E.rect_of_points(e.pts)
                if r is None:
                    continue
                a = (r[2] - r[0]) * (r[3] - r[1])
                if a > best_a:
                    best, best_a = r, a
    return best


def _needs_bigger_sheet(cd: E.ChemDrawing) -> bool:
    """流程图内容溢出或压到说明框/标题栏时扩大流程幅面。"""
    fit = cd.meta.get("fit") or {}
    return bool(fit.get("overflow") or fit.get("overlaps_notes")
                or fit.get("overlaps_title_block")
)


# ---------------------------------------------------------------------------
# 带版面分配的建图：工段划分 → 数量统计 → ELK layered 分配 → 迭代修正
# ---------------------------------------------------------------------------

def _violation_count(det: Dict) -> int:
    """三类关系型缺陷的总数（相撞 + 压线 + 穿设备）——迭代收敛的判据。"""
    c = det.get("counts") or {}
    return int(c.get("symbol_overlap", 0)) + int(c.get("text_over_line", 0)) \
        + int(c.get("pipe_through", 0)) + int(c.get("text_overlap", 0)) \
        + int(c.get("pipe_over_stream_frame", 0)) \
        + int(c.get("equipment_label_position", 0)) \
        + int(c.get("pipe_spacing", 0)) + int(c.get("exchanger_nozzle_clearance", 0)) \
        + int(c.get('shared_port_alignment',0)) + int(c.get('equipment_nozzle_clearance',0))


def _astar_summary(astar: Dict[str, object]) -> Dict[str, object]:
    """把 A* 的产出压成报告用的小结（含**放宽分布**与**被迫让路的文字**）。

    放宽分布必须报出来：`relaxed > 0` 的那些路是"放宽容忍后得到的"，
    很可能已经违规——不写清楚就成了"布线说没问题、校核说穿了"的静默失真。
    """
    if not astar:
        return {}
    rel = {0: 0, 1: 0, 2: 0}
    evicted: List[str] = []
    ok = 0
    for r in astar.values():
        rel[int(getattr(r, "relaxed", 0))] = rel.get(int(getattr(r, "relaxed", 0)), 0) + 1
        if getattr(r, "ok", False):
            ok += 1
        evicted += list(getattr(r, "evicted", []) or [])
    return {"lines": len(astar), "ok": ok, "relaxed": rel,
            "evicted_text": sorted(set(evicted)),
            "bends": sum(max(0, len(getattr(r, "path", [])) - 2) for r in astar.values()),
            "expanded": sum(int(getattr(r, "expanded", 0)) for r in astar.values())}


def build_pfd(spec: Dict, *, max_rounds: int = MAX_LAYOUT_ROUNDS
              ) -> Tuple[E.ChemDrawing, List[E.ControlLoop]]:
    """**本技能唯一的对外建图入口**（取代已删除的 `build_both`）：出一张 PFD。

    走的是完整出图链路（排版 → 迭代修正 → 幅面复核），不是只建一次就交差；
    并按 PFD 口径裁剪规格。调用方手里即使是一份写着 `drawing_type:"pid"`
    的老规格，走这里也只会出 PFD——"只出 PFD"在库层是强制的，不是默认项。

    底层原语仍在：`build()`（按规格原样建一次）/ `build_with_layout()`（多返回
    一份版面报告）。需要报告的走后者。
    """
    cd, loops, _report = build_with_layout(spec_for_type(spec, "pfd"),
                                           max_rounds=max_rounds)
    return cd, loops


def build_with_layout(spec: Dict, *, max_rounds: int = MAX_LAYOUT_ROUNDS
                      ) -> Tuple[E.ChemDrawing, List[E.ControlLoop], Dict]:
    """出图主入口：先划分工段、统计数量、按启发式分配版面，再逐轮修掉版面缺陷。

    为什么要"重建"而不是"就地改"：三类缺陷里只有**文字挪位**能就地修
    （文字是叶子标注），其余两类（设备相撞、管线穿设备）都牵动管线重算——
    管线是用锚点解出来的，设备一动管子就得跟着重画，改半张图比重建更容易留下错。
    所以修正走"生成提示 → 整图重建 → 复检"的闭环：

      ① 排版（启发式）→ 建图 → 探测三类缺陷
      ② 有缺陷且还有预算 → 生成提示（换走线 / 绕行 / 滑阀 / 推开设备）→ 回 ①
      ③ **只接受缺陷更少的方案**（严格单调，不会来回震荡）
      ④ 收敛后做文字挪位（唯一无需重建的一类），再复检一次

    `layout.mode`：`off`（默认，完全不动）｜`report`（只统计与探测，不动图）｜
                  `auto`（排版 + 迭代修正，并把三类缺陷升级为**硬约束**）

    `layout.sheet="auto"` 时外面还有一层**幅面实测**：`plan_layout` 挑幅面只能在
    排版期估计，而仪器圆、测量引线、回路位号、流股数据块都还没生成；
    整图收敛之后再复核一次"内容压没压到说明框/标题栏"，压到了就换大一号重来。
    """
    lay = dict(spec.get("layout") or {})
    auto_sheet = str(lay.get("sheet", "")).lower() == "auto"
    # **只出 PFD**：这一步是硬性的——不管规格里写的是 `pid` 还是别的，
    # 走到这个入口一律按 PFD 口径裁剪（去阀门/仪表/回路，管线注流股号+参数）。
    cur = spec_for_type(spec, resolve_drawing_type(spec))
    out: Optional[Tuple[E.ChemDrawing, List[E.ControlLoop], Dict]] = None
    # ⚠️ **换幅面的"试探"与"定稿"要分开给预算**（性能关键，实测踩到）：
    # 早先每次换幅面都跑 `max_rounds` 轮完整迭代修正——`sheet="auto"` 时最多
    # 5 个幅面 × 4 轮 = 20 次完整建图。而"这个幅面装不装得下"**一次建图就能
    # 判断**（`_needs_bigger_sheet` 只看 overflow / 压家具 / 流股表放不下），
    # 迭代修正对"选幅面"毫无帮助——它改的是设备微移与走线，不是幅面。
    # 实测：8 台设备的一张拆图，改前 **53.5s**，改后见下。
    #
    # 所以：试探轮 `probe_rounds=0`（只建一次），只有**确定不再换幅面**的那次
    # 才用完整 `max_rounds` 做迭代修正。
    probe_rounds = 0
    for _i in range(len(L.SHEET_ASCENDING) + 1):
        cd, loops, report = _build_rounds(cur, probe_rounds)
        out = (cd, loops, report)
        if not (auto_sheet and str(report.get("mode")) == "auto"
                and _needs_bigger_sheet(cd)):
            # **定稿这一张**：用完整预算重跑一次迭代修正（前面只是试探）
            if probe_rounds < max_rounds:
                cd, loops, report = _build_rounds(cur, max_rounds)
                out = (cd, loops, report)
            return out
        bigger = L.next_sheet(cd.sheet)
        if bigger is None:
            # 已到最大幅面：同样要用完整预算定稿
            if probe_rounds < max_rounds:
                cd, loops, report = _build_rounds(cur, max_rounds)
                out = (cd, loops, report)
            return out
        report.setdefault("sheet_bumps", []).append(
            f"{cd.sheet} → {bigger}（内容压到说明框/标题栏或溢出，换大一号重排）")
        cur = {**cur, "layout": {**dict(cur.get("layout") or {}), "sheet": bigger}}
    assert out is not None
    return out


def _build_rounds(spec: Dict, max_rounds: int
                  ) -> Tuple[E.ChemDrawing, List[E.ControlLoop], Dict]:
    """在一张**已定幅面**的规格上跑完整的"排版 → 迭代修正"。"""
    lay = dict(spec.get("layout") or {})
    mode = str(lay.get("mode", "off")).lower()
    report: Dict[str, object] = {"mode": mode, "rounds": 0, "plan": None,
                                 "collisions": None, "polish": None, "accepted": None}
    if mode not in ("auto", "report"):
        cd, loops = build(spec)
        report["collisions"] = L.detect_collisions(cd)
        return cd, loops, report

    hints = L.empty_hints()
    hints["mode"] = mode

    best: Optional[Tuple[int, E.ChemDrawing, List[E.ControlLoop], Dict, Dict]] = None
    tried = 0
    for rnd in range(max_rounds + 1):
        cd, loops = build(spec, hints)
        # 文字挪位随每轮一起做：它是**唯一不用重建就能修**的一类
        # （文字是叶子标注）。放进轮内而不是只在最后做，才能让"缺陷总数"
        # 这个收敛判据反映真实版面——否则 20 多处文字压线会把判据一直顶在上面，
        # 迭代永远看不到 0，白白跑满轮数。
        pol = L.polish_texts(cd) if mode == "auto" else None
        det = L.detect_collisions(cd)
        n = _violation_count(det)
        tried = rnd
        if best is None or n < best[0]:
            best = (n, cd, loops, det, dict(hints))
        if n == 0 or mode != "auto":
            break
        trial = dict(hints)
        L.repair_equipment_footer_overlaps(cd, spec, trial)
        L.repair_equipment_overlaps(cd, spec, trial, det)
        L.repair_pipes(cd, spec, trial, det)
        L.repair_symbol_slides(cd, spec, trial, det)
        # 把"这一轮被压到的文字框"学成下一轮的硬障碍，供 A* 绕开。
        # A* 跑在管线画出**之前**，所以管线自己带来的那批文字（管道号、流股数据块、
        # 仪表气泡）在布线时还不存在——只能靠这条通路一轮一轮学回来。
        L.learn_route_avoid(cd, spec, trial, det)
        if trial == hints:                       # 提不出新建议 → 再重也是同一张图
            break
        hints = trial

    assert best is not None
    n, cd, loops, det, best_hints = best
    report["rounds"] = tried
    report["plan"] = cd.meta.get("layout_plan")
    report["astar"] = cd.meta.get("astar") or {}
    report["collisions"] = det
    report["accepted"] = {"violations": n, "hints": best_hints}
    report["polish"] = L.polish_texts(cd) if mode == "auto" else None
    report["collisions"] = L.detect_collisions(cd)
    report["after"] = {"violations": _violation_count(report["collisions"])}   # type: ignore[arg-type]
    return cd, loops, report


# ---------------------------------------------------------------------------
# 版面报告
# ---------------------------------------------------------------------------

def _moved_of(cd: E.ChemDrawing, spec: Dict) -> Dict[str, object]:
    """落位摘要：哪几台是引擎排的、哪几台被钉在规格坐标上。"""
    lay = dict(spec.get("layout") or {})
    forced = bool(lay.get("force"))
    pinned = [str(e["id"]) for e in spec.get("equipment", [])
              if e.get("at") is not None and not forced]
    plan = cd.meta.get("layout_plan") or {}
    placed = list((plan.get("placements") or {}).keys())
    moved = [e for e in placed if e not in pinned]
    note = ("混用「给了坐标」与「不给坐标」会削弱版面统一性：给了坐标的一律视为钉住；"
            "想整体重排加 --layout-force" if pinned and moved else "")
    return {"moved": moved, "pinned": pinned, "note": note}


def _layout_payload(cd: E.ChemDrawing, spec: Dict, lrep: Dict) -> Dict:
    """版面报告的落盘结构（工段 / 数量 / 计划 / 缺陷明细）。"""
    plan = lrep.get("plan") or {}
    det = lrep.get("collisions") or {}
    return {
        "mode": lrep.get("mode"),
        "sheet": cd.sheet,
        "rounds": lrep.get("rounds"),
        "sections": plan.get("sections"),
        "equipment_counts": plan.get("counts"),
        "plan": {k: v for k, v in plan.items() if k != "sections"},
        "placement": _moved_of(cd, spec),
        "crossings": {"break_count": cd.meta.get('crossing_breaks', 0),
                      "gaps": cd.meta.get('crossing_break_points', []),
                      "unbroken": len(E.visible_pipe_crossings(cd))},
        "violations": {
            "counts": det.get("counts"),
            "pipe_over_stream_frame": det.get("pipe_over_stream_frame", []),
            "pipe_spacing": det.get("pipe_spacing", []),
            "exchanger_nozzle_clearance": det.get("exchanger_nozzle_clearance", []),
            "shared_port_alignment": det.get('shared_port_alignment',[]),
            "equipment_nozzle_clearance": det.get('equipment_nozzle_clearance',[]),
            "equipment_label_position": det.get("equipment_label_position", []),
            "text_overlap": [
                {"a": h['a'].text, "b": h['b'].text}
                for h in (det.get('text_overlap') or [])],
            "symbol_overlap": [
                {"a": h["a"], "b": h["b"], "gap": round(h["gap"], 2), "axis": h["axis"]}
                for h in (det.get("symbol_overlap") or [])],
            "text_over_line": [
                {"text": str(h["text"].text), "layer": h["layer"],
                 "seg_layer": h["seg_layer"], "at": [round(h["text"].pt[0], 1),
                                                     round(h["text"].pt[1], 1)]}
                for h in (det.get("text_over_line") or [])],
            "pipe_through": [
                {"line": h["line"], "equipment": h["equip"],
                 "inside_mm": round(h["length"], 1)}
                for h in (det.get("pipe_through") or [])],
        },
        "polish": lrep.get("polish"),
        "final_ok": not (det.get("counts") or {}).get("symbol_overlap")
                    and not (det.get("counts") or {}).get("text_overlap")
                    and not (det.get("counts") or {}).get("text_over_line")
                    and not (det.get("counts") or {}).get("pipe_through")
                    and not (det.get("counts") or {}).get("pipe_over_stream_frame")
                    and not (det.get("counts") or {}).get("equipment_label_position")
                    and not (det.get("counts") or {}).get("pipe_spacing")
                    and not (det.get("counts") or {}).get("exchanger_nozzle_clearance")
                    and not (det.get('counts') or {}).get('shared_port_alignment')
                    and not (det.get('counts') or {}).get('equipment_nozzle_clearance')
                    and not E.visible_pipe_crossings(cd),
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="规格驱动生成化工工艺流程图（PFD）图纸")
    ap.add_argument("spec", nargs="?", help="JSON 规格文件（--list-docs 时可省略）")
    ap.add_argument("--out", default=None,
                    help="输出文件路径（.dwg / .dxf / .scr / .lsp）")
    ap.add_argument("--backend", default="dwg",
                    choices=["dwg", "dxf", "com", "scr", "lisp", "auto"],
                    help="**默认 dwg**（交付格式固定 DWG；**强制经 AutoCAD COM 端口连 CAD**："
                         "先纯 Python 写 DXF，再由 chemcad_dwg 经 COM 另存为 DWG；"
                         "COM 连不上即响亮失败，绝不静默降级/换 accoreconsole）；"
                         "auto=旧默认值，等价于 dwg（不再按 --out 扩展名猜）；"
                         "dxf=显式落盘 DXF（中间件逃生口）；com=画进运行中的 AutoCAD；"
                         "scr=生成 AutoCAD 脚本；lisp=生成 AutoLISP（entmake，可用图块）")
    ap.add_argument("--check-only", action="store_true", help="只自检，不出图")
    ap.add_argument("--no-layer-standard", action="store_true",
                    help="不套用出图图层标准（默认套用：图层名/颜色/线型/线宽按标准设置）")
    ap.add_argument("--no-blocks", action="store_true", help="符号展平为基本图元（不使用图块）")
    ap.add_argument("--no-ezdxf", action="store_true",
                    help="强制使用零依赖 R12 书写器（线宽由图层颜色承载）")
    ap.add_argument("--dxf-version", default="R2010",
                    choices=["R2000", "R2004", "R2007", "R2010", "R2013", "R2018"],
                    help="DXF 版本；R2007 以下中文会退化为 \\U+XXXX 转义")
    ap.add_argument("--all-layers", action="store_true", help="图层表中写入全部标准图层")
    ap.add_argument("--report", default=None, help="自检报告写入 JSON")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    # 版面（工段划分 / 数量统计 / 启发式分配 / 迭代修正）
    ap.add_argument("--layout", default=None, choices=["off", "report", "auto"],
                    help="版面模式：off=只按规格坐标（默认）；report=划分工段+统计数量+"
                         "探测三类版面缺陷，但不改图；auto=ELK layered 分配版面并迭代修掉"
                         "符号重叠/文字压线/管线穿设备（把它们升级为硬约束）")
    ap.add_argument("--layout-force", action="store_true",
                    help="连规格里写了坐标（at）的设备也一并重排")
    ap.add_argument("--layout-report", default=None, metavar="文件.json",
                    help="把工段划分、数量统计、版面计划与缺陷明细写入 JSON")
    # 图纸类型：本技能只出 PFD（见 DRAWING_TYPES 的说明）
    ap.add_argument("--drawing-type", default=None, choices=["pfd"],
                    help="pfd=工艺流程图（PFD，唯一图型；去阀门/仪表/回路，"
                         "管线注流股号+主要参数）。保留此开关仅为兼容旧命令行")
    # COM 参数（**本技能强制经 AutoCAD COM 出图**，所以这些是主通道的参数，不是旁支）
    ap.add_argument("--create", action="store_true",
                    help="AutoCAD 未运行时由本技能启动一个（默认只认**已在运行**的实例："
                         "要画进用户眼前那张图纸，不偷偷另起一个）")
    ap.add_argument("--check-tools", action="store_true",
                    help="只体检出图通道（COM 是否可连 / AutoCAD 版本 / 逃生口状态），不出图")
    ap.add_argument("--invisible", action="store_true", help="COM 后端不显示 AutoCAD 窗口")
    ap.add_argument("--clear-layers", action="store_true",
                    help="COM 后端先删掉同名图层上的旧图元（避免在旧图元上叠加）")
    ap.add_argument("--purge", action="store_true",
                    help="COM 后端落图后清除空图层（等价 AutoCAD PURGE）")
    ap.add_argument("--doc", default=None, metavar="关键词",
                    help="COM 后端：在已打开的多张图纸中按名称/路径关键词切换活动图纸")
    ap.add_argument("--list-docs", action="store_true",
                    help="COM 后端：只列出当前打开的全部图纸后退出（不画图）")
    a = ap.parse_args(argv)

    if a.check_tools:
        import chemcad_dwg as DWG
        r = DWG.converter_report()
        com = r.get("com") or {}
        print("出图通道体检（本技能**强制经 AutoCAD COM** 出图）")
        print(f"  COM          {'可用' if com.get('ok') else '不可用'}"
              + (f"：{com.get('session')}" if com.get("ok") else "")
              + (f" AutoCAD {com.get('version')}" if com.get("version") else ""))
        if not com.get("ok"):
            print(f"    {str(com.get('reason', '')).strip()}")
        print(f"  pywin32       {'在' if r.get('pywin32') else '缺'}")
        print(f"  accoreconsole {r.get('accoreconsole') or '未找到'}"
              "（**逃生口**，默认不走；要它得设 CHEMCAD_DWG_BACKEND=accore）")
        print(f"  默认后端     {r.get('default_backend')}")
        return 0 if r.get("ok") else 3

    if a.list_docs:
        try:
            client = E._MinimalCom(create_if_not_exists=a.create, visible=not a.invisible)
            docs = client.list_documents()
        except (ImportError, ConnectionError) as e:
            print(f"\n错误：{e}", file=sys.stderr)
            return 3
        except Exception as e:                      # COM 偶发故障
            print(f"\n错误：读取图纸列表失败（{type(e).__name__}）：{e}", file=sys.stderr)
            return 3
        print(f"当前打开 {len(docs)} 张图纸：")
        for name, path in docs:
            print(f"  · {name}    {path}")
        return 0

    if not a.spec:
        print("错误：需要给出 JSON 规格文件（或用 --list-docs 只列出已打开的图纸）",
              file=sys.stderr)
        return 2

    with open(a.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)
    lay = dict(spec.get("layout") or {})
    if a.layout:
        lay["mode"] = a.layout
    if a.layout_force:
        lay["force"] = True
    spec = {**spec, "layout": lay}

    # **只出 PFD**（用户决定取消"一份规格出两张图"）：
    # 没有 `types` 循环、没有 `-PID/-PFD` 后缀、也没有"两图共用幅面"这一说了。
    dtype = resolve_drawing_type(spec, a.drawing_type)
    spec = spec_for_type(spec, dtype)
    import chemcad_block_library as BL
    try:
        cd, loops, lrep = build_with_layout(spec)
    except BL.BlockConfirmationRequired as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (ValueError, KeyError) as exc:
        print(f"规格或标注无法绘制：{exc}", file=sys.stderr)
        return 1
    code, payload = _emit(a, spec, dtype, cd, loops, lrep)
    if a.report and not a.json and payload.get("report_path"):
        print(f"自检报告 → {payload['report_path']}")
    if a.json:
        print(json.dumps(payload["report"], ensure_ascii=False, indent=2))
    return code


def resolve_backend(backend: Optional[str], out_path: Optional[str] = None) -> str:
    """定落盘后端。**默认 `dwg`**——交付格式是硬性规定，不看 `--out` 扩展名。

    `auto` 是旧默认值（当年按扩展名猜：`.dwg`→DWG 其余→DXF），现在等价 `dwg`。
    抽成函数是为了让"默认就是 DWG"有一条**不依赖 AutoCAD 是否装了**的落点可测；
    `out_path` 留着只为签名兼容（不再参与判定）。
    """
    b = str(backend or "dwg").lower()
    return "dwg" if b in ("", "auto") else b


def _as_dwg_path(p: str) -> str:
    """把输出路径规范成 **.dwg**——交付格式是硬性规定，不是"看扩展名猜"。

    写 `--out 图.dxf` 也给你 `图.dwg`；真要 DXF 必须显式 `--backend dxf`。
    """
    return os.path.splitext(p)[0] + ".dwg"


def _emit(a, s: Dict, dtype: str,
          cd: E.ChemDrawing, loops: List[E.ControlLoop],
          lrep: Dict) -> Tuple[int, Dict]:
    """把**已建好**的一张图落盘并打印报告。返回 (退出码, 汇总)。

    建图交给 `build_with_layout`，本函数只管"报告 + 写文件"。
    """
    rep = E.preflight(cd, loops)
    rep["drawing_type"] = dtype
    rep["layout"] = cd.meta.get("fit", {})
    rep["layout_plan"] = lrep.get("plan")
    rep["layout_collisions"] = (lrep.get("collisions") or {}).get("counts")

    # **连接保真硬门**（用户硬约束）：规格若带 bkp 溯源（`_bkp_ref`），
    # 出图前核对"设备/连接/拓扑/接续标志"是否忠实于 .bkp——捏造或丢失即报 error、
    # 拒绝出图（与"管线穿设备"等错误同级别，不复核就等于没约束）。
    try:
        import bkp_to_pfd as _B
        fp = _B.verify_spec_fidelity(s)
        if fp:
            for x in fp:
                tgt = rep["errors"] if x.get("level") == "error" else rep["warnings"]
                tgt.append({"rule": x["rule"], "msg": x["msg"]})
            if any(x.get("level") == "error" for x in fp):
                rep["ok"] = False
            rep["fidelity"] = fp
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠ 连接保真校验未执行：{type(exc).__name__}: {exc}", file=sys.stderr)

    if not a.json:
        print(E.format_report(rep))
        if lrep.get("mode") in ("auto", "report") and lrep.get("plan"):
            print()
            print(L.format_plan(lrep["plan"], _moved_of(cd, s)))
            cnt = rep.get("layout_collisions") or {}
            print(f"版面缺陷：符号重叠 {cnt.get('symbol_overlap', 0)} 处"
                  f"、文字压线 {cnt.get('text_over_line', 0)} 处"
                  f"、管线穿设备 {cnt.get('pipe_through', 0)} 处"
                  f"（修正 {lrep.get('rounds', 0)} 轮）")
            pol = lrep.get("polish") or {}
            if pol:
                print(f"文字挪位：修好 {pol.get('fixed', 0)} 处"
                      f"，仍有 {pol.get('left', 0)} 处待人工处理")
                for x in (pol.get("unresolved") or [])[:6]:
                    print(f"    ! {x}")
            if lrep.get("mode") == "report":
                print("（report 模式只诊断不改图；要自动修用 --layout auto）")
        lay = rep.get("layout") or {}
        if lay:
            print(f"版面：内容尺寸 {lay.get('content_size')}，平移 {lay.get('shift')} 后居中于"
                  f" {lay.get('sheet')} 可用区 {lay.get('area')}")
            if lay.get("overflow"):
                print("  ⚠ 内容尺寸超出可用区域（未缩放，请改图幅或精简内容）")
            if lay.get("overlaps_title_block"):
                print("  ⚠ 内容与标题栏区域重叠")
            if lay.get("overlaps_notes"):
                print("  ⚠ 内容与说明框区域重叠（请改图幅或把说明框挪开）")

    rep_path = None
    if a.layout_report:
        rep_path = a.layout_report
        with open(rep_path, "w", encoding="utf-8") as f:
            json.dump(_layout_payload(cd, s, lrep), f, ensure_ascii=False, indent=2)
        if not a.json:
            print(f"版面报告 → {rep_path}")

    if not a.check_only and any(x.get('rule') in ('流股标注净距不足','流股设备连接','流股线净距不足','换热器接管避让','共用接管分支错位','物流穿设备接管') for x in rep['errors']):
        if a.report:
            with open(a.report, 'w', encoding='utf-8') as f:
                json.dump(rep, f, ensure_ascii=False, indent=2)
        print('流股设备连接或标注净距不合格，已保留检查报告，拒绝出图', file=sys.stderr)
        return 1, {'type': dtype, 'report': rep, 'layout': lrep}

    out_info: Dict[str, object] = {}
    out_path = a.out
    # **出图图层标准**：把内部语义层名换成标准层名（NN-用途-线型），
    # 并让写出器按标准给颜色/线型/线宽。默认开启——"输出必须按标准设置"。
    if not a.check_only and not a.no_layer_standard:
        try:
            import chemcad_layers as LY
            rep_ls = LY.apply_standard(cd)
            miss = LY.unmapped(cd)
            if miss:
                print(f"  ⚠ 有语义层未登记标准映射：{miss}（将原样输出，评审无法对照）")
            bad = LY.check_standard(cd)
            for b in bad:
                print(f"  · 图层标准 {b['level']}：{b['msg'][:110]}")
            print(f"  · 图层标准已套用：重命名 {len(rep_ls.get('renamed') or {})} 组"
                  f"（标准共 {rep_ls.get('layers')} 层）")
        except Exception as exc:                              # noqa: BLE001
            print(f"图层标准套用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    # **交付格式硬性规定为 DWG**（不再"按 --out 扩展名猜"）：
    #   · 默认 `--backend dwg`；`auto` 是旧默认值，等价 dwg（兼容老命令行）
    #   · 走 dwg 后端时 `--out 图.dxf` 会被改成 `图.dwg` 并说一声，不静默出 DXF
    #   · 真要 DXF 请**显式** `--backend dxf`（逃生口，不是默认路径）
    #   · scr / lisp / com 不受影响（它们本来就不是"落盘成图"的通道）
    backend = resolve_backend(a.backend, out_path)
    if backend == "dwg" and out_path and not out_path.lower().endswith(".dwg"):
        fixed = _as_dwg_path(out_path)
        if not a.json:
            print(f"  · 交付格式固定 DWG：输出名 {os.path.basename(out_path)} → "
                  f"{os.path.basename(fixed)}（要 DXF 请显式 --backend dxf）")
        out_path = fixed
    if not a.check_only:
        if backend in ("dwg", "dxf", "scr", "lisp"):
            if not out_path:
                print(f"错误：--backend {backend} 需要 --out 指定输出文件", file=sys.stderr)
                return 2, {"type": dtype, "report": rep}
            if backend == "dwg":
                # **前置体检**：本技能强制经 AutoCAD COM 出图。先把"连不连得上"
                # 问清楚——画完再失败，用户白等一轮；而且报错里要能直接照着修。
                try:
                    import chemcad_dwg as DWG
                    pre = DWG.require_com(a.create)
                except Exception as exc:                       # noqa: BLE001
                    print(f"错误：COM 通道体检失败：{type(exc).__name__}: {exc}",
                          file=sys.stderr)
                    return 3, {"type": dtype, "report": rep}
                if not pre.get("ok"):
                    print(f"错误：{pre.get('reason')}", file=sys.stderr)
                    return 3, {"type": dtype, "report": rep}
                if not a.json:
                    print(f"  · COM 通道：已连 AutoCAD"
                          + (f" {pre.get('version')}" if pre.get("version") else "")
                          + f"（{pre.get('session')}）")
                try:
                    out_info = E.render(cd, out_path, backend="dwg",
                                        prefer_ezdxf=not a.no_ezdxf,
                                        blocks=not a.no_blocks,
                                        all_layers=a.all_layers,
                                        dxf_version=a.dxf_version,
                                        create_if_not_exists=a.create)
                except RuntimeError as exc:          # 连不上 AutoCAD：**响亮地失败**
                    print(f"错误：{exc}", file=sys.stderr)
                    return 3, {"type": dtype, "report": rep}
            elif backend == "dxf":
                out_info = E.write_dxf(cd, out_path, prefer_ezdxf=not a.no_ezdxf,
                                       blocks=not a.no_blocks, all_layers=a.all_layers,
                                       dxf_version=a.dxf_version)
            else:
                import chemcad_scr as SCR
                if backend == "scr":
                    # SCR 一律展平（命令行 -BLOCK 要交互选对象，脚本模式不可靠）
                    out_info = SCR.write_scr(cd, out_path)
                else:
                    out_info = SCR.write_lisp(cd, out_path, blocks=not a.no_blocks)
        else:
            # ---- COM：先切活动图纸（多文档场景容易画错图），再落图 ----
            try:
                client = E._MinimalCom(create_if_not_exists=a.create,
                                       visible=not a.invisible)
                if a.doc:
                    hit = client.find_document(a.doc)
                    if hit is None:
                        print(f"错误：在已打开的图纸中找不到与 '{a.doc}' 匹配的图纸。"
                              f"用 --list-docs 看清单。", file=sys.stderr)
                        return 3, {"type": dtype, "report": rep}
                    print(f"已切换活动图纸 → {hit}")
                out_info = E.ComBackend(client=client).draw(
                    cd, blocks=not a.no_blocks, clear_layers=a.clear_layers,
                    purge_layers=a.purge)
            except (ConnectionError, ImportError) as e:
                # 缺 pyautocad/pywin32 与「AutoCAD 没开」对用户是同一件事：
                # 这条后端现在走不通。两者都必须给可执行建议，而不是抛栈。
                print(f"\n错误：{e}", file=sys.stderr)
                print("提示：① 不想开 AutoCAD → 改用 --backend dxf 直接落盘；"
                      "② 装不上 pywin32 → 改用 --backend scr / lisp"
                      "（同样零 Python 依赖，但可脚本化落图）。", file=sys.stderr)
                return 3, {"type": dtype, "report": rep}
            except Exception as e:          # COM 的 RPC 繁忙等偶发故障
                print(f"\n错误：驱动 AutoCAD 失败（{type(e).__name__}）：{e}",
                      file=sys.stderr)
                print("提示：可重试一次；仍失败则改用 --backend dxf。", file=sys.stderr)
                return 3, {"type": dtype, "report": rep}
        if not a.json:
            print("-" * 68)
            print(f"已出图：{out_info.get('path', 'AutoCAD 模型空间')}")
            print(f"  后端 {out_info.get('backend')}  图块 {out_info.get('blocks', 0)} 个"
                  f"  图元 {out_info.get('entities', 0)} 个")
            if out_info.get("note"):
                print(f"  注意：{out_info['note']}")

    rep_file = a.report
    if rep_file:
        payload = dict(rep)
        payload["output"] = out_info
        with open(rep_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return (0 if rep["ok"] else 1), {"type": dtype, "report": rep, "out": out_info,
                                     "layout": lrep, "path": out_path, "sheet": cd.sheet,
                                     "report_path": rep_path or rep_file}


if __name__ == "__main__":
    raise SystemExit(main())
