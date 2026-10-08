#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""bkp_to_pfd.py —— 从 Aspen `.bkp` **直接生成 PFD 规格**（不装 Aspen）。

数据来源：`chemcad_locate.py bkp <file> --json out.json`，它给出

  * `blocks`        —— 120 个模块，各带 Aspen **流程图坐标 (x, y)** 与 `category`
  * `streams`       —— 181 条流股，带 `source` / `dest` 拓扑
  * `stream_results`—— 163 条流股结果：temp / pres / mass / vol / mole / vfrac
  * `units`         —— 单位制（本例 METCBAR：°C / bar / m³/h / kg/h / kmol/h）

**为什么用模型自带的坐标，而不是重跑排版**

`.bkp` 里存着 Aspen 流程图的坐标，那是**工程师自己摆的**——它天然顺着工艺方向，
也天然与参赛图纸同源（图纸就是照这个模型画的）。丢开它重排，等于把最可靠的
版面信息扔掉再猜一遍。所以本模块**默认用模型坐标**（`layout.mode="off"`），
只在坐标缺失时才回退到引擎排版。

**先识别模块，再查具名图库块**

按 catalog.json 的显式映射选取块；没有对应块时要求用户确认。
禁止按 category 猜成容器或其他设备。用户确认的设备级选择可写入
输入 JSON 的 equipment_blocks（设备 id -> 图库 id）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chemcad_catalog as catalog
from chemcad_block_library import BlockConfirmationRequired

# 兼容查询接口；真实映射只维护在 catalog.json。
MOD_TO_TYPE = {
    module: str(catalog.find(label)["id"]) if label else ""
    for module, label in catalog.aspen_block_map().items()
}

#: Aspen 模块类型的中文名（比 category 更贴切，用于设备名称标注）
MOD_NAME: Dict[str, str] = {
    "RADFRAC": "精馏塔", "HEATER": "加热器/冷却器", "HEATX": "换热器",
    "MIXER": "混合器", "FSPLIT": "分流器", "PUMP": "泵", "COMPR": "压缩机",
    "MCOMPR": "多级压缩机", "COMPR2": "压缩机", "FLASH2": "闪蒸罐",
    "FLASH3": "三相闪蒸罐", "RPLUG": "管式反应器", "RSTOIC": "化学计量反应器",
    "VALVE": "阀", "DECANTER": "倾析器", "SEP2": "两相分离器", "BLOCK": "用户模块",
    # —— 图库口径新增 ——
    "RATEFRAC": "速率精馏塔", "MULTIFRAC": "多塔精馏", "PETROFRAC": "石油精馏塔",
    "ABSBR": "吸收塔", "EXTRACT": "萃取塔",
    "RBATCH": "间歇反应器", "REQUIL": "平衡反应器", "RGIBBS": "吉布斯反应器",
    "RYIELD": "收率反应器",
    "HXFLUX": "热流率换热器", "MSHEAT": "多股流换热器",
    "SEP": "组分分离器", "DRUM": "缓冲罐",
    "VESSEL": "立式容器", "TANK": "储罐",
    "CRYSTALLIZER": "结晶器", "CENTRIF": "离心机", "CYCLONE": "旋风分离器",
    "FILTER": "烛式过滤器",
}


def _phase(vfrac: Optional[float]) -> str:
    """气相分率 → 相态（标准要求注相态，且本技能的物流表要 phase 列）。"""
    if vfrac is None:
        return ""
    if vfrac >= 0.98:
        return "气"
    if vfrac <= 0.02:
        return "液"
    return "气液"


def _wf_string(r: Dict) -> str:
    """从流股结果里抽取**组成（质量分率 w%）**摘要，填入物流表 `wf` 列。

    评审标准二.2 要求"质量流量和质量分率"成表——`wf` 是"完整物流表"九列之一。
    ⚠️ 当前 `.bkp` 提取链路（`chemcad_locate` 之外的 Aspen COM 导出）**没有**把
    各组分的质量/摩尔分率带出来，`stream_results` 只有 T/P/流量/相态，
    所以这里**默认返回空串**（列头仍保留，表格结构完整）。一旦提取链路补上
    `comp`（{组分: 质量分率}）或 `ycomp`（{组分: 摩尔分率}），本函数即自动填充。
    摩尔分率会显式标 `y:` 前缀，避免与质量分率 w% 混淆。
    """
    comp = r.get("comp") or r.get("composition") or r.get("mass_frac") or r.get("components")
    if isinstance(comp, dict) and comp:
        items = sorted(comp.items(), key=lambda kv: -float(kv[1]))
        return "/".join(f"{k}{float(v) * 100:.0f}" for k, v in items)
    ycomp = r.get("ycomp") or r.get("mole_frac")
    if isinstance(ycomp, dict) and ycomp:
        items = sorted(ycomp.items(), key=lambda kv: -float(kv[1]))
        return "y:" + "/".join(f"{k}{float(v) * 100:.0f}" for k, v in items)
    return ""


def _parallel_via(p1: Optional[Tuple[float, float]], p2: Optional[Tuple[float, float]],
                  k: int, step: float = 12.0) -> Optional[Tuple[float, float]]:
    """并联支路的**横向错开点**：第 k 条（k≥1）在同一条直线上加一个垂直偏移 waypoint。

    ⚠️ 一条 bkp 流股 = 一条图面管线（用户硬约束"不得捏造/丢失流股"），所以
    同一对设备之间的多条流股**都要画**；但若照原样画，几条线会完全重合、
    看上去像一条——既丢信息又难看。这里给第 2 条及以后各加一个垂直方向的
    `via` 点，把并联支路岔开，连接关系不变、图面可分辨。
    """
    if not p1 or not p2:
        return None
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = -dy / length, dx / length                 # 垂直单位向量
    mx, my = (p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0
    off = step * k
    return (round(mx + ux * off, 2), round(my + uy * off, 2))


def build_spec(bkp_json: Dict, *, name: str = "", sheet: str = "A1",
               scale: Optional[float] = None, limit: int = 0,
               annotate: bool = True, fold_aux: bool = True,
               fold_prefixes: Sequence[str] = ("P", "S", "M", "VA")
               ) -> Dict:
    """`.bkp` 解析结果 → PFD 规格（可直接喂 `draw_pfd.py`）。

    `fold_aux`：是否启用"辅助折叠"出图模式（默认开，与两校获奖作品口径对齐）。
    折叠对象由 `fold_prefixes` 指定——默认泵 P / 分离器 S / 混合器 M / 放空 VA：
    这些设备**仍画图形符号、仍占版面与避障**，只是**不给设备位号**
    （`tag` 置空、`folded:True`），与评审"PFD 只标主设备"的宽松要求一致。
    核心设备（塔 T / 反应器 R）始终 1:1 带位号。
    """
    blocks = list(bkp_json.get("blocks") or [])
    streams = list(bkp_json.get("streams") or [])
    results = dict(bkp_json.get("stream_results") or {})
    if limit:
        blocks = blocks[:limit]

    eq_ids = {str(b.get("id")) for b in blocks}
    # 流股 → (源设备, 目的设备)；同一对之间只留一条（Aspen 里并联支路会重复）
    w = float(bkp_json.get("_w") or 0) or None

    # ---- 归一化坐标：Aspen 单位（米级）→ 图纸毫米 ----
    xs = [float(b.get("x", 0.0)) for b in blocks] or [0.0]
    ys = [float(b.get("y", 0.0)) for b in blocks] or [0.0]
    span_x = max(1e-6, max(xs) - min(xs))
    span_y = max(1e-6, max(ys) - min(ys))
    # 目标：横向铺开 ~600mm、纵向 ~360mm（A1 可用区里留出家具的位置）
    sx = float(scale) if scale else 600.0 / span_x
    sy = sx if scale else 360.0 / span_y
    if not scale:                       # 保持长宽比，取较小的那个
        s = min(sx, sy)
        sx = sy = s

    def at(b: Dict) -> Tuple[float, float]:
        return (round((float(b.get("x", 0.0)) - min(xs)) * sx, 2),
                round((float(b.get("y", 0.0)) - min(ys)) * sy, 2))

    equip: List[Dict] = []
    bid_xy: Dict[str, Tuple[float, float]] = {}
    folded_n = 0
    node_types: Dict[str, str] = {}
    for b in blocks:
        bid = str(b.get("id"))
        mod = str(b.get("type", "")).upper()
        choice = (bkp_json.get("equipment_blocks") or {}).get(bid)
        symbol = catalog.find(str(choice or MOD_TO_TYPE.get(mod) or ""))
        typ = str(symbol["id"]) if symbol else ""
        # ⚠️ **拓扑结点：记账但不建设备图元**（见 `chemcad_symbols.TOPOLOGY_NODES`）。
        #    catalog 中明确标为 null 的模块才算结点；未知模块必须待用户确认，
        #    不得把“没登记”当成结点，也不得按 category 猜设备类型。
        is_node = mod in MOD_TO_TYPE and MOD_TO_TYPE[mod] == "" and not choice
        # ⚠️ **`bid_xy` 与 `eq_ids` 仍然收录结点**（`eq_ids` 在函数开头由全部块构成）：
        #    结点虽不画设备，但仍是管线的**端点/折点**。若把结点从端点集合里剔除，
        #    "设备—结点—设备"的流股会被判成单端流股，进而被误画成「进出界区标志」——
        #    那是**静默失真**（图上凭空多出界区符号），比不画设备严重得多。
        bid_xy[bid] = at(b)
        if is_node:
            node_types[bid] = mod
            continue
        if not symbol:
            raise BlockConfirmationRequired(
                f"设备 {bid}（模块 {mod or '未知'}）没有对应图库块，请用户确认图块，"
                "在输入 JSON 的 equipment_blocks 中记录设备 id -> 图库 id；尚未生成规格")
        # 折叠判定：**设备位号首字母**（泵 P / 分离 S / 混合 M / 放空 VA）。
        # 取位号（Aspen block id 即最终位号）的前导字母作前缀，
        # 落入 `fold_prefixes` 即折叠——只隐去位号文字，图形照画。
        alpha_pre = "".join(ch for ch in bid if ch.isalpha())
        do_fold = bool(fold_aux) and (alpha_pre in set(fold_prefixes))
        # 位号用 Aspen 模块 id 本身（图纸与模型一致，评审要对得上号）；
        # 折叠设备 tag 置空、并标记 folded，draw_pfd 据此跳过位号标注但仍落图形。
        equip.append({
            "id": bid,
            "tag": "" if do_fold else bid,
            "type": typ,
            "block": typ,
            "module_type": mod,
            "name": MOD_NAME.get(mod) or str(symbol["label"]),
            "at": list(at(b)),
            **({"folded": True} if do_fold else {}),
        })
        if do_fold:
            folded_n += 1

    # ---- 管线 + 流股注记 ----
    # ⚠️ **一条 bkp 流股 = 一条图面管线**（用户硬约束）：**不**对相同 (源,目标) 去重。
    #    并联重复支路（同一对设备间多条流股）全部要画，用**横向 `via` 点岔开**，
    #    既不丢流股、又不让同形线重叠成一条。端点缺失的流股如实记账（见 `_bkp_ref`）。
    pipes: List[Dict] = []
    streams_map: Dict[str, Dict] = {}
    battery: List[Dict] = []
    ref_battery: List[List[str]] = []
    pair_seen: Dict[Tuple[str, str], int] = {}
    skipped_streams: List[List[str]] = []
    ref_pairs: List[List[str]] = []

    def _stream_fields(r: Dict) -> Dict[str, str]:
        """流股关键参数（T/P/W/相态）——设备间管线的注记与界区标志共用同一份口径。"""
        return {
            "T": f"{r.get('temp', 0):.1f} ℃",
            "P": f"{r.get('pres', 0):.2f} bar",
            "W": f"{r.get('mass', 0):.0f} kg/h",
            "phase": _phase(r.get("vfrac")),
        }

    for s in streams:
        sid = str(s.get("id"))
        src, dst = s.get("source"), s.get("dest")
        src_ok = bool(src) and str(src) in eq_ids
        dst_ok = bool(dst) and str(dst) in eq_ids
        if not (src_ok and dst_ok):
            # **单端流股 = 进出界区**（原料进 / 产品出）：另一端不是设备，说明这条物料的
            # 另一头在**界区外**（`.bkp` 里它没有源/目标模块）。按惯例画**进出界区标志**
            # （剖面线圆 + 箭头）+ 文字「自界区来 / 去界区」。
            # ⚠️ 这**不是捏造**——这是模型里真实存在的进出界点（比丢着不画忠实得多）；
            # 两端都不是设备（纯全局流股）才进 `skipped`，并如实记账。
            if src_ok or dst_ok:
                here = str(src) if src_ok else str(dst)
                out = src_ok                       # 源头在本图 → 流出界区
                ref_battery.append([str(src or ""), str(dst or "")])
                # 标志落在**流股的自由端**（= 这条线穿出/穿入界区的真实位置），
                # **不落在设备上**——落在设备中心会与设备本体相撞（实测重叠 12~15 mm，
                # 被"符号重叠"校验判 error 而拒绝出图）。`polys` 已按流向定向。
                at_ref: object = here
                direction = "auto"
                polys = s.get("polys") or []
                if polys and polys[0] and polys[-1]:
                    if out:
                        free = polys[-1][-1]
                        seg = (polys[-1][-2], polys[-1][-1]) if len(polys[-1]) >= 2 else None
                    else:
                        free = polys[0][0]
                        seg = (polys[0][0], polys[0][1]) if len(polys[0]) >= 2 else None
                    if seg:
                        ddx, ddy = seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]
                        direction = ("right" if ddx > 0 else "left") if abs(ddx) >= abs(ddy) \
                            else ("up" if ddy > 0 else "down")
                    at_ref = [round(float(free[0]), 3), round(float(free[1]), 3)]
                ent: Dict = {"at": at_ref, "direction": direction,
                             "dest": "去界区" if out else "自界区来",
                             "stream_no": sid, "stream_id": sid, "pair": [str(src or ""), str(dst or "")]}
                r = results.get(sid) or {}
                if r:
                    ent["stream_data"] = _stream_fields(r)
                    streams_map[sid] = dict(_stream_fields(r),no=sid,source_stream_id=sid)
                battery.append(ent)
            else:
                skipped_streams.append([str(src or ""), str(dst or "")])
            continue
        key = (str(src), str(dst))
        k = pair_seen.get(key, 0)
        pair_seen[key] = k + 1
        ref_pairs.append([str(src), str(dst)])
        line_no = f"L{sid}"
        pipe: Dict = {"id": line_no, "from": str(src), "to": str(dst),
                      "layer": "PIPE_PRI", "arrows": 1}
        if k:                                   # 第 2 条及以后的并联支路：横向错开
            v = _parallel_via(bid_xy.get(str(src)), bid_xy.get(str(dst)), k)
            if v:
                pipe["via"] = [list(v)]
        pipes.append(pipe)
        r = results.get(sid) or {}
        if r:
            streams_map[line_no] = dict(
                _stream_fields(r), no=sid, source_stream_id=sid,
                rho=f"{r.get('mass', 0) / r['vol']:.1f}" if r.get("vol") else "",
                V=f"{r.get('vol', 0):.1f}",
                wf=_wf_string(r),
                pf=f"{r.get('vfrac', 0) * 100:.0f} %" if r.get("vfrac") is not None else "",
            )

    spec: Dict = {
        "name": name or f"{os.path.basename(str(bkp_json.get('source', 'model')))} 工艺物料流程图",
        "drawing_type": "pfd",
        "sheet": sheet,
        "layout": {"mode": "off"},      # **用模型自带坐标**，不重排
        "route_astar": False,           # 模型坐标已是工程师版面，不必再绕障重排
        "title_block": {
            "项目名称": "Aspen 模型导出", "专业名称": "工艺",
            "名称": name or "工艺物料流程图（由 .bkp 生成）",
            "竞赛队名称": "", "设计": "模型导出", "制图": "自动生成",
            "比例": "不按比例",
        },
        "_注释": "本规格由 bkp_to_pfd.py 从 Aspen .bkp 直接生成：设备坐标取模型流程图坐标，"
                 "流股注记取 stream_results（T/P/流量/相态）。",
        "_areas": {"of": assign_areas(blocks, bkp_json.get("areas")),
                   #: 工段顺序 = bkp 里车间标注的**出现顺序**（真实图纸的工段排列就是它）
                   "order": [str(a.get("name")) for a in
                             ((bkp_json.get("areas") or {}).get("areas") or [])]},
        "equipment": equip,
        "pipes": pipes,
        # **进出界区标志**（原料进 / 产品出）：单端流股 → 剖面线圆 + 箭头 + 「自界区来/去界区」。
        "battery": battery,
        "_fold": {
            "enabled": bool(fold_aux),
            "prefixes": list(fold_prefixes),
            "folded": folded_n,
            "带位号": len(equip) - folded_n,
        },
        # **拓扑结点统计**（按图库口径收敛的 9 类块：VALVE/PIPE/MIXER/FSPLIT…）。
        # 这些块在 `.bkp` 里有、在 `_bkp_ref.blocks` 里也在（保真基线不丢），
        # 但**图上不出设备符号**——它们以管线折点/直通点的身份存在。
        # 单独记账是为了让"图上设备少了"这件事**可解释**，而不是看起来像漏画。
        "_nodes": {
            "policy": "按图库 catalog v3.3 口径：这些块保持拓扑结点语义，不画设备符号",
            "count": len(node_types),
            "kinds": sorted(set(node_types.values())),
            "ids": sorted(node_types),
            # 结点坐标：它们不出图元，但是**管线的落点**——出图时按自由点解析。
            "xy": {bid: list(bid_xy[bid]) for bid in node_types},
        },
        # **bkp 溯源 / 连接保真基线**：把"这张图本应有的设备与连接"钉进规格里。
        # 出图前（`draw_pfd`）会拿它核对，规格若被改出 bkp 里没有的设备/连接 → **报错拒绝出图**。
        # 这是用户硬约束"不得捏造与 bkp 不同的连接方案"的可执行判据。
        "_bkp_ref": {
            "scope": "full",
            "source": str(bkp_json.get("source", "")),
            "blocks": sorted(eq_ids),
            "streams": ref_pairs,
            "battery": ref_battery,
            "n_streams_total": len(streams),
            "n_skipped": len(skipped_streams),
            "skipped": skipped_streams[:20],
        },
    }
    if annotate and streams_map:
        spec["streams"] = streams_map
        spec["stream_table"] = True
    return spec


def _proc_code(bid: str) -> str:
    """从设备位号取**工段代号**（主项代号）——**bkp 里的一级划分依据**。

    ⚠️ **用户指定的权威口径**：`.bkp` 里 `01` 就是**第一工段**、`02` 就是**第二工段**，
    以此类推。位号形如 `T0101` / `P0102` / `R0201`（类别字母 + 数字），
    其中**前 2 位是工段（主项）代号**、后 2 位是工段内顺序号
    （与 HG 20559.7 §3.0.3 的"前 1~2 位表工序、后 2 位表顺序"一致）。
    所以 `T0101` 属**第一工段（01）**。

    早先这里只当"二级细拆用的工序代号"，而一级工段由引擎按**流股连通性**猜——
    那是猜的，而位号里的工段号是**模型自带的权威划分**，不猜更可靠。

    非标准位号（物料名如 `C6H6`/`CH3OH`）返回 `"--"`（待定），不凭空造工段号
    （见 `_is_std_tag` 的踩坑说明）。
    """
    if not _is_std_tag(bid):
        return "--"
    digits = "".join(ch for ch in str(bid) if ch.isdigit())
    return digits[:2] if len(digits) >= 2 else "--"


def _seq_code(bid: str) -> str:
    """从设备位号取**工段内顺序号**（后 2 位数字）——供大工段细拆用。

    位号 `T0101` → 工段 `01`、顺序号 `01`；`E0327` → 工段 `03`、顺序号 `27`。
    顺序号在工段内按流程排（Aspen 建模时就是顺着流程编号的），
    所以按它切出来的子组在流程上**连续**，不会把塔和它的回流罐劈开。
    """
    digits = "".join(ch for ch in str(bid) if ch.isdigit())
    return digits[2:4] if len(digits) >= 4 else (digits[2:] if len(digits) > 2 else "00")


#: 标准位号模式：**类别字母 + 4 位数字**（`T0101` / `P0102` / `R0201`）
_STD_TAG_RE = re.compile(r"^[A-Za-z]+\d{3,}$")


def _is_std_tag(bid: str) -> bool:
    """是否为**标准位号**（类别字母 + 数字）。

    ⚠️ 为什么要判这个（实测踩到）：`.bkp` 里有一批**按物料命名的设备**
    ——`C6H6`（苯）、`CH3OH`（甲醇）、`H2O`（水）、`DS-1`、`C-1`。
    它们不是位号而是**化学式/代号**，若照样"取数字当工段号"，就会
    `C6H6` → 工段 **66**、`H2O` → 工段 2、`CH3OH` → 工段 30——
    凭空多出几个"工段"，每个还只有 1~2 台设备。
    **先判是不是位号，再取号**；不是位号的归入待定工段 `"--"`，
    由第二步按"每图 ≥6 台"合并处理。
    """
    return bool(_STD_TAG_RE.match(str(bid).strip()))


def _owner_of(ref: str, ids: set) -> str:
    head = str(ref).partition(".")[0]
    return head if head in ids else ""


def assign_areas(blocks: Sequence[Dict], areas_info: Optional[Dict]) -> Dict[str, str]:
    """把每台设备归到 `.bkp` 自带的**车间/工段**——依"区域名标注 + 分界线围出的矩形"。

    算法：每个区域名标注落在由**分界线**围成的矩形里（上/下/左/右各取最近的、覆盖该点的
    一条分界线）；再把设备按坐标落进哪个矩形来归属。

    ⚠️ 两条实测教训：
      * 不用"取最近标注"——标注摆在区域角落，最近法实测错 35/120 台，大面积串区；
      * 分界线判定要留容差——样本竖墙写作 `Dim: 25.4 21.3 25.3 -10.6`（x 差 0.1 不是 0），
        按"严格相等"会把这条墙漏掉，导致两个车间并成一个。
    无标注 / 无分界线时返回空 dict —— 调用方**回退**到别的判据，绝不凭空造工段。
    """
    if not areas_info:
        return {}
    names = areas_info.get("areas") or []
    wh = areas_info.get("walls_h") or []
    wv = areas_info.get("walls_v") or []
    if not names:
        return {}
    xs = [float(b.get("x", 0.0)) for b in blocks] or [0.0]
    ys = [float(b.get("y", 0.0)) for b in blocks] or [0.0]
    bnd = (min(xs) - 5.0, min(ys) - 5.0, max(xs) + 5.0, max(ys) + 5.0)

    def region(a: Dict) -> Tuple[float, float, float, float]:
        x, y = float(a["x"]), float(a["y"])
        left = max([w["x"] for w in wv if w["x"] < x and w["y1"] <= y <= w["y2"]],
                   default=bnd[0])
        right = min([w["x"] for w in wv if w["x"] > x and w["y1"] <= y <= w["y2"]],
                    default=bnd[2])
        down = max([w["y"] for w in wh if w["y"] < y and w["x1"] <= x <= w["x2"]],
                   default=bnd[1])
        up = min([w["y"] for w in wh if w["y"] > y and w["x1"] <= x <= w["x2"]],
                 default=bnd[3])
        return (left, down, right, up)

    regs = [(str(a["name"]), region(a)) for a in names]
    out: Dict[str, str] = {}
    for b in blocks:
        x, y = float(b.get("x", 0.0)), float(b.get("y", 0.0))
        hit = [n for n, (L, D, R, U) in regs
               if L - 1e-6 <= x <= R + 1e-6 and D - 1e-6 <= y <= U + 1e-6]
        if hit:
            out[str(b.get("id"))] = hit[0]
    return out


def split_spec(spec: Dict) -> List[Dict]:
    # ⚠️ 早先这里有个 `plan=` 参数（引擎的工段划分结果），**已删**：工段划分改为
    # **位号前两位**的权威口径后，不再需要引擎按连通性猜的那份划分。
    # 留着这个参数会误导调用方以为"工段还是引擎定的"。
    r"""把一份全流程规格**按用户指定的三步工作流拆张**。

    第一步 **按工段划分**：`.bkp` 位号的**前 2 位数字就是工段号**
    （`01`=第一工段、`02`=第二工段……）。`T0101` 属第一工段、`R0201` 属第二工段。
    这是**模型自带的权威划分**，不靠引擎按流股连通性猜。

    第二步 **识别各工段设备数量并分配**：统计每个工段有几台设备，再决定
    "整段一张"还是"段内按顺序号细拆"——**每张图至少 6 台设备**（用户硬约束），
    一张最多 12 台。细拆按位号后两位（工段内顺序号）切，流程连续。

    第三步 **开始做图**：逐组生成一张图的规格（设备 / 管线 / 流股 / 接续标志）。

    跨张流股**不能丢**：它标进 `offpage`，出图时会画成接续标志并**附带流股物流
    信息**（流股号 + T/P/W/相态，数据取自 `.bkp` 的 `streams`），否则读图人
    不知道这条线去哪儿了、也不知道跨过去多少料。
    """
    # ---- 第一步：按 bkp 位号的工段代号（前 2 位数字）划分工段 ------------
    # ⚠️ **用户指定的权威口径**：`.bkp` 里 `01`=第一工段、`02`=第二工段……位号
    # 前两位就是工段号。这是**模型自带的划分**，比"引擎按流股连通性猜"可靠——
    # 早先一级工段用引擎连通性划分（位号前两位只当二级"工序代号"），
    # 结果把储罐这类互无直连管线的设备切成一串"每个 1 台"的小段
    # （实测 7 台储罐各自成「储存与缓冲工段 1~7」）。改回位号口径后，
    # 工段号与 Aspen 主项、与流程图主项编号天然对得上。
    equip = {str(e.get("id")): e for e in (spec.get("equipment") or [])}
    pipes = list(spec.get("pipes") or [])
    # 端点集合 = 设备 ∪ 拓扑结点：结点**不出设备图元，却是管线的合法端点**
    # （MIXER / FSPLIT / VALVE 直连的流股很常见）。漏掉它们，这些流股在拆张时
    # 既进不了"本张管线"、也进不了接续标志 —— 覆盖性校验会判「拆张丢流股」。
    _NSET = {str(x) for x in ((spec.get("_nodes") or {}).get("ids") or [])}
    _NXY = (spec.get("_nodes") or {}).get("xy") or {}
    ids = set(equip) | _NSET
    # **工段划分与命名：优先用 `.bkp` 自带的车间标注**（`_areas`），无则回退位号前两位。
    _area_of: Dict[str, str] = dict((spec.get("_areas") or {}).get("of") or {})
    _AREA_NAMES = set(_area_of.values())

    def _sec_key(m: str) -> str:
        return _area_of.get(m) or _proc_code(m)


    # ---- 无编号设备（物料名如 C6H6 / CH3OH / H2O）的工段判定 --------------
    # ⚠️ 这类设备**位号里没有工段号**（见 `_is_std_tag`），但它们在流程里
    # **确实属于某个工段**。判定用**确定性规则**，不用投票/聚类：
    #
    #   ① `.bkp` 自带区域矩形命中 → 直接用（见上 `assign_areas`）；
    #   ② 否则沿**流股图** BFS：取跳数最少的有编号设备所在工段；
    #      同跳并列取**位号字典序最小**的那台 → 结果唯一、可复现；
    #   ③ 图上孤立（无任何连通路径）→ 留 `--` 并**显式登记**，不猜。
    #
    # 为什么弃用"邻居投票 + 坐标网格聚类"：两者都带**魔数**（最近 6 台、网格步长 12 mm），
    # 且结论随设备坐标浮动——同一张图换个 Aspen 版本可能归到不同工段。
    # BFS 只看**连接关系**（模型自带的拓扑），跨版本不变。
    def _adjacency() -> Dict[str, set]:
        adj: Dict[str, set] = {}
        for p in pipes:
            a = _owner_of(str(p.get("from", "")), ids)
            b = _owner_of(str(p.get("to", "")), ids)
            if a and b and a != b:
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)
        return adj

    _ADJ = _adjacency()

    def _nearest_section(bid: str) -> str:
        """沿流股图 BFS 到**最近的有编号设备**，取其工段；同跳取位号字典序最小。"""
        seen, level = {bid}, [bid]
        while level:
            nxt: List[str] = []
            for cur in level:
                for nb in sorted(_ADJ.get(cur, ())):
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            hits = sorted(x for x in nxt if _sec_key(x) != "--")
            if hits:
                return _sec_key(hits[0])
            level = nxt
        return "--"

    unknown = [m for m in sorted(ids) if _sec_key(m) == "--"]
    sec_of_unknown: Dict[str, str] = {}
    evidence: Dict[str, Dict[str, object]] = {}
    for m in unknown:
        pc = _nearest_section(m)
        sec_of_unknown[m] = pc
        evidence[m] = {
            "归段依据": "流股图 BFS 最近有编号设备" if pc != "--" else "孤立：图上无连通路径",
            "归入工段": pc,
        }

    by_sec: Dict[str, List[str]] = {}
    for m in sorted(ids):
        pc = _sec_key(m)
        if pc == "--":
            pc = sec_of_unknown.get(m, "--")
        by_sec.setdefault(pc, []).append(m)

    # **段序 = 流程顺序**：bkp 里车间标注的先后就是工艺走向（实测与真实 PFD 的工段排列一致）。
    # 不排序的话键序会跟着 `sorted(ids)` 的字母序跑（物料名 CH3OH 之类排最前），段序就乱了。
    _order = [str(x) for x in ((spec.get("_areas") or {}).get("order") or [])]
    if _order:
        _ord = {n: k for k, n in enumerate(_order)}
        by_sec = dict(sorted(by_sec.items(),
                             key=lambda kv: (_ord.get(kv[0], len(_ord)), kv[0])))
    # 工段名带中文序号，读图人一眼知道是第几工段
    _ZH = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
           7: "七", 8: "八", 9: "九", 10: "十"}
    # 小工段合并后的显示名（键 = 合并后的合成键，值 = "第X工段 + 第Y工段"）
    _MERGED_LABEL: Dict[str, str] = {}

    def _sec_name(pc: str) -> str:
        if pc in _MERGED_LABEL:
            return _MERGED_LABEL[pc]
        if pc in _AREA_NAMES:                     # bkp 自带的车间名（权威划分）
            return pc
        if pc.isdigit():
            n = int(pc)
            return f"第{_ZH.get(n, str(n))}工段（{pc}）"
        return f"未归段（{pc}）"

    # ---- 第二步：分别识别各个工段的设备数量，进行设备分配 ----------------
    # 统计每工段设备数 → 决定"整段一张"还是"段内细拆"。
    # ⚠️ **用户硬约束：每张图至少 6 台设备**。所以：
    #   · **装得下** → 整段一张；
    #   · **装不下** → 按**工段内顺序号**（位号后两位）切成若干子组，
    #     顺序号在工段内顺着流程编，切出来的子组流程连续（不会把塔和它的回流罐劈开）；
    #   · 切完仍有 < MIN_SHEET 的尾组 → 并回上一组（杜绝碎片张）；
    #   · 工段本身就 < MIN_SHEET（如物料命名的储罐只有 5 台、位号 66 只有 2 台）
    #     → **几个小工段合并成一张**，凑够 ≥6 台再成图（见下 `_merge_small`）。
    #
    # ⚠️ **"装得下"不再是常数**。原先写死 `MAX_SHEET = 12`（"一张图最多 12 台"）是**猜的**：
    # 一台塔 24×120、一台泵 11×11，幅面相同却差一个数量级，用**台数**当容量必然切错。
    # 现在由 `_fit_count` 按**真实符号包络 + 图幅可用区**折行装填**算出来**。
    MIN_SHEET = 6          # 一张图至少 6 台设备（用户定）
    #: 折行装填的设备间净距（mm），与 ELK 的 `spacing_node_node` 同量级。
    #: ponytail: 版面净距先写常量；要按项目调时再外提成 spec 字段。
    CELL_GAP = 12.0

    # **小工段合并**：工段号是权威划分，但"每图 ≥6 台"是硬约束。
    # 单个工段不够 6 台时，把几个小工段**并成一张**（名写"第X工段+第Y工段"），
    # 而不是出一张 2 台的碎片图。实测：待定工段 5 台 + 第66工段 2 台 → 并成 7 台一张。
    small = [pc for pc in by_sec if len(by_sec[pc]) < MIN_SHEET]
    if len(small) > 1:
        merged_mem: List[str] = []
        names: List[str] = []
        for pc in small:
            merged_mem += by_sec[pc]
            names.append(_sec_name(pc))
            del by_sec[pc]
        by_sec["+".join(pc for pc in small)] = merged_mem
        _MERGED_LABEL["+".join(pc for pc in small)] = " + ".join(names)

    sec_counts = {pc: len(mem) for pc, mem in by_sec.items()}
    spec_meta = {
        "第一步·工段划分": {_sec_name(pc): n for pc, n in sorted(sec_counts.items())},
        "第二步·设备分配": {},
        # 无编号设备的归段依据（逐台可核对；孤立设备显式登记，不猜）
        "无编号设备·归段依据": evidence,
    }

    #: 算容量用的图幅：spec 里是 `auto`（交给出图阶段升幅）时，按 A1 保守估。
    cap_sheet = str(spec.get("sheet") or "A1").upper()
    if cap_sheet in ("", "AUTO"):
        cap_sheet = "A1"

    def _fit_count(mem: List[str]) -> int:
        """**这一组设备在一张图上放得下几台**——量真包络，不数台数。

        折行装填：按工段内顺序号逐台放进当前行（列宽 = 设备包络宽 + 净距），
        行满换行（行高 = 本行最高设备 + 净距），纵向放不下即止。
        量尺与可用区**全部复用布局层**（`measure_equipment` / `available_area` /
        `planning_area`），不再有第二份尺寸表。

        返回 0 = 量尺不可用（缺绘图层）→ 调用方按"整段一张"处理。
        """
        try:
            from chemcad_layout import (measure_equipment, available_area,
                                        planning_area)
        except Exception:                       # 纯 .bkp 解析场景下允许没有绘图层
            return 0
        sub = {"equipment": [equip[m] for m in mem if m in equip],
               "streams": spec.get("streams") or {}}
        boxes = measure_equipment(sub, cap_sheet)
        x0, y0, x1, y1 = planning_area(available_area(sub, cap_sheet))
        W, H = x1 - x0, y1 - y0
        x = y = row_h = 0.0
        n = 0
        for m in sorted(mem, key=lambda m: (_seq_code(m), m)):
            b = boxes.get(m)
            w, h = (b.w, b.h) if b is not None else (20.0, 20.0)
            if x and x + CELL_GAP + w > W:                # 行满 → 换行
                y += row_h + CELL_GAP
                x, row_h = 0.0, 0.0
            if y + max(row_h, h) > H:                     # 纵向放不下 → 本张满
                break
            x += (CELL_GAP if x else 0.0) + w
            row_h = max(row_h, h)
            n += 1
        return n

    def _split_big(mem: List[str]) -> List[List[str]]:
        """一个工段的设备按**顺序号**切成若干子组，每组都塞得进一张图。"""
        cap = _fit_count(mem)
        if cap <= 0 or len(mem) <= cap:
            return [mem]
        ordered = sorted(mem, key=lambda m: (_seq_code(m), m))
        chunks = [ordered[i:i + cap] for i in range(0, len(ordered), cap)]
        # 尾组过小 → 并回上一组（宁可上一组稍大，也不留碎片张）
        # ponytail: 合并后上一组会**略微超出容量**（上限 = cap + MIN_SHEET - 1）。
        # "≥6 台"是用户硬约束、"放得下"是版面约束，尾数 <6 时两者无解，取前者。
        # 要消掉它得让出图阶段回吐（升幅 / 缩小符号），收益不抵复杂度。
        while len(chunks) > 1 and len(chunks[-1]) < MIN_SHEET:
            chunks[-2] = chunks[-2] + chunks.pop()
        return chunks

    # ---- 第三步：开始做图（逐组生成一张图的规格） ------------------------
    groups: List[Tuple[str, str, List[str]]] = []
    for pc in by_sec:            # 按 by_sec 的既有顺序（= 段序 = 流程序），不再字母序重排
        mem = by_sec[pc]
        chunks = _split_big(mem)
        spec_meta["第二步·设备分配"][_sec_name(pc)] = {
            "设备数": len(mem), "分成几张": len(chunks),
            "每张设备数": [len(c) for c in chunks],
        }
        if len(chunks) == 1:
            groups.append((_sec_name(pc), "", chunks[0]))
        else:
            for k, c in enumerate(chunks, 1):
                lo = _seq_code(c[0])
                groups.append((_sec_name(pc), f"{k}({lo}起)", c))

    # 设备守恒自检：拆张最典型的静默 bug 就是丢设备
    _all = [m for _s, _p, g in groups for m in g]
    assert sorted(_all) == sorted(ids), \
        f"拆张丢设备/重复：{len(_all)} vs {len(ids)}"

    out: List[Dict] = []
    total = len(groups)
    # 每张图归属哪个工段 → 后面判"跨张流股是同段还是跨段"要用
    sec_of_id: Dict[str, str] = {}
    for sec_name, _proc, mem in groups:
        for m in mem:
            sec_of_id[m] = sec_name
    for i, (sec_name, proc, mem) in enumerate(groups, 1):
        ms = set(mem)
        keep, cross = [], []
        for p in pipes:
            a = _owner_of(p.get("from", ""), ids)
            b = _owner_of(p.get("to", ""), ids)
            if a in ms and b in ms:
                keep.append(p)
            elif a in ms or b in ms:
                cross.append(p)

        def _flags(p: Dict) -> Tuple[str, str, str, str]:
            """一条跨张流股 → (本张一端, 另一端设备, 另一端工段名, 接续标志种类)。"""
            la = _owner_of(p.get("from", ""), ids)
            lb = _owner_of(p.get("to", ""), ids)
            here, there = (la, lb) if la in ms else (lb, la)
            other_sec = sec_of_id.get(there, "")
            # Drawing1.dwg 的图例把接续分成两种：进出**相同工段**用短框、
            # 进出**不同工段**用长框。这正是拆张后跨张流股的两种情形。
            kind = "same" if other_sec == sec_name else "cross"
            return here, there, other_sec, kind

        # ⚠️ **跨张流股的物流信息不能丢**（用户硬约束）：分割流股时必须在接续标志上
        # 标明流股号 + 关键物流参数，且数据必须与 .bkp 一致（直接取 `streams`，
        # 不自造）。跨张流股是"这条物料线去了/来自另一张图"，若不标流量温度压力，
        # 读图人就不知道跨出去的是多少料、什么状态——那是"这条线去哪儿了"之外的
        # 另一半信息。
        _st = spec.get("streams") or {}

        def _offpage(p: Dict) -> Dict:
            here, there, other_sec, kind = _flags(p)
            line = str(p.get("id", ""))
            sd = _st.get(line) if isinstance(_st.get(line), dict) else {}
            # 接续标志旁的「来去归属」文字——真实参赛 PFD（南工大 PFD-2018.dwg 的
            # `5-来去归属文字` 层）在框旁写「物料/来源 + 去/自 + 目标(设备位号/界区)」，
            # 例：「脱轻烃塔塔顶产物去T0301」「原料苯自总厂来」。框里只答"去了哪张图"，
            # 这条文字答"跨过去去/来自哪台设备"——是真实图纸的画法，补上（不替掉
            # 流股号+T/P/W 那条用户硬约束的物流标注，二者并列）。
            teq = equip.get(there, {})
            # ⚠️ 目标一律用 **bkp 位号（= 设备 id）**，不用显示位号 `tag`：
            #   折叠改动后辅助设备 `tag` 为空，若取 tag 会退化成中文名（"去泵/压缩机"），
            #   读图人无从对照 bkp。"去到哪里"必须答得出**模型里的那台设备**，
            #   而 id 与折叠与否无关，恒等于 bkp 位号。
            tname = teq.get("id") or teq.get("tag") or there
            out = (str(p.get("from", "")) == str(here))   # 本张是源头 → 流出去
            if there not in equip:
                dest = "去界区" if out else "自界区来"
            else:
                dest = (f"去{tname}" if out else f"自{tname}来")
            # direction="auto"：拆张阶段还不知道设备最终落在版面的哪一侧，
            # 写死 "right" 会让 15 只接续标志全挤在设备右侧互相压、还压设备本体。
            # 交给出图阶段按版面空闲自动选（先右后左再上下，逐件避让）。
            d = {"at": here, "direction": "auto", "kind": kind, "stream_id": line,
                 "drawing_no": f"→{other_sec or '外'}", "dest": dest,
                 # 原始有向连接一并记下：拆张覆盖性校验（不丢流股）要用，
                 # 也让接续标志可回溯到 bkp 的哪条流股。
                 "pair": [str(p.get("from", "")), str(p.get("to", ""))]}
            # 流股号 + 物流参数一并带进接续标志（draw_pfd 出图时画在标志旁）
            if sd:
                d["stream_no"] = str(sd.get("no") or line)
                d["stream_data"] = {k: str(sd[k]) for k in ("T", "P", "W", "phase")
                                    if sd.get(k) not in (None, "")}
            return d

        sub = {
            "name": f"{sec_name}·工序{proc} 工艺物料流程图",
            "drawing_type": "pfd",
            "sheet": "auto",
            "layout": {"mode": "auto", "sheet": "auto"},
            "title_block": {**(spec.get("title_block") or {}),
                            # 标题栏本来就渲染"第 X 张 共 N 张"，把总张数写进去即可
                            "共几张": str(total),
                            "图号": f"PFD-{i:02d}"},
            # 结点不建设备图元（图库口径），但它在 `mem` 里以**管线端点**身份存在
            "equipment": [equip[m] for m in mem if m in equip],
            "_nodes": {"ids": sorted(m for m in mem if m in _NSET),
                       "xy": {m: _NXY[m] for m in sorted(mem) if m in _NSET and m in _NXY}},
            "pipes": keep,
            # 进出界区的接续标志**必须挂在本张的那一端**。
            # 先是写成"取 from 端，取不到就 to 端"——结果挂到了**另一张**的设备上，
            # 出图时 `resolve('V0101')` 直接 KeyError（那台根本不在这张图上）。
            # 跨张流股的两端必有一端在本张，取**在本张**的那一端才对。
            "offpage": [_offpage(p) for p in cross],
            # 进出界区标志（单端流股）：落到**配对设备所在**的那张。
            # ⚠️ 不能拿 `at` 比设备集合——`at` 是流股的自由端坐标（界区穿出点），
            #    要按 `pair` 里的设备端判归属。
            "battery": [b for b in (spec.get("battery") or [])
                        if any(str(x) in ms for x in (b.get("pair") or []))],
            "_拆张": {"工段": sec_name, "分工段": proc,
                      "本张设备": sum(1 for m in mem if m in equip), "本张管线": len(keep),
                      "跨张流股": len(cross), "第几张": f"{i}/{total}",
                      # 后继工具（自检/评分）可据此断言"一张图 = 一个工段"
                      "工段唯一": len({sec_of_id.get(m, "") for m in mem}) == 1,
                      "跨工段接续": sum(1 for p in cross if _flags(p)[3] == "cross"),
                      "同工段接续": sum(1 for p in cross if _flags(p)[3] == "same")},
            # 三步工作流的统计（供自检/评分核对"每图 ≥6 台""工段划分对不对"）
            "_三步": spec_meta if i == 1 else None,
            # **本张的连接保真基线**（scope=part）：本张该有的设备与连接。
            # 出图前核对，本张若被改出基线里没有的设备/连接 → 报错拒绝出图。
            "_bkp_ref": {
                "scope": "part",
                "source": str((spec.get("_bkp_ref") or {}).get("source", "")),
                "blocks": sorted(m for m in ms if m in equip),
                "streams": [[str(p.get("from", "")), str(p.get("to", ""))] for p in keep],
                "battery": [[str(b.get("pair", ["", ""])[0]), str(b.get("pair", ["", ""])[1])]
                            for b in (spec.get("battery") or [])
                            if any(str(x) in ms for x in (b.get("pair") or []))],
                "n_streams_total": len(cross) + len(keep),
                "n_skipped": 0,
                "skipped": [],
            },
        }
        if spec.get("streams"):
            snames = {str(p.get("id")) for p in keep + cross}
            snames.update(str(b.get("stream_id") or b.get("id") or b.get("stream_no") or "")
                          for b in sub["battery"])
            sub["streams"] = {k: v for k, v in spec["streams"].items() if k in snames}
            sub["stream_table"] = True
        out.append(sub)
    return out


def ref_from_bkp(bkp_json: Dict) -> Dict:
    """从 `.bkp` 提取结果导出**连接保真基线**：设备集合 + 有向连接多重集 + 进出界区流股。

    这就是"这张图本应有的东西"。出图前拿它核对规格——**不得捏造、不得丢失**。

    流股分三类（与 `build_spec` 同一口径，两处必须一致，否则保真门会自相矛盾地报"捏造"）：
      * 两端都是设备 → `streams`（要画成管线）；
      * **恰好一端是设备** → `battery`（进出界区：原料进 / 产品出，画界区标志）；
      * 两端都不是设备 → `skipped`（纯全局对象，如实记账、不画）。
    """
    blocks = list(bkp_json.get("blocks") or [])
    eq_ids = {str(b.get("id")) for b in blocks}
    pairs: List[List[str]] = []
    battery: List[List[str]] = []
    skipped: List[List[str]] = []
    for s in (bkp_json.get("streams") or []):
        a, b = s.get("source"), s.get("dest")
        a_ok = bool(a) and str(a) in eq_ids
        b_ok = bool(b) and str(b) in eq_ids
        if a_ok and b_ok:
            pairs.append([str(a), str(b)])
        elif a_ok or b_ok:
            battery.append([str(a or ""), str(b or "")])
        else:
            skipped.append([str(a or ""), str(b or "")])
    return {"scope": "full", "source": str(bkp_json.get("source", "")),
            "blocks": sorted(eq_ids), "streams": pairs, "battery": battery,
            "n_streams_total": len(bkp_json.get("streams") or []),
            "n_skipped": len(skipped), "skipped": skipped[:20]}


def verify_spec_fidelity(spec: Dict, ref: Optional[Dict] = None) -> List[Dict[str, str]]:
    """**连接保真硬校验**：规格是否忠实于 `.bkp`（不捏造 / 不丢失 / 拓扑合法 / 接续正确）。

    四条用户硬约束，逐条落成判据：

      * **C1 连接一致**：图上的每条管线都对应 bkp 的一条流股；bkp 的每条流股都画了（scope=full）。
      * **C2 接续标志**：挂点必在本图设备上；文字必含「自X来 / 去Y」；目标用 bkp 位号。
      * **C3 拓扑严格交替**：任何连接都是「设备—流股—设备」——起点终点都必须是设备，
        不许设备直连设备、不许流股接流股、不许自环。**不限制左右方向**（回流/循环照画）。
      * **C4 不捏造**：图上不得出现 bkp 里没有的设备或连接。

    `ref` 缺省取 `spec["_bkp_ref"]`（由 `build_spec`/`split_spec` 写入）。
    手工规格没有溯源时**只做 C3 拓扑校验**（拓扑与 bkp 无关，永远可判）。

    返回 `problems`（每项含 level/rule/msg）；**空列表 = 通过**。
    """
    out: List[Dict[str, str]] = []

    def err(rule: str, msg: str) -> None:
        out.append({"level": "error", "rule": rule, "msg": msg})

    def warn(rule: str, msg: str) -> None:
        out.append({"level": "warning", "rule": rule, "msg": msg})

    def head(ref: object) -> str:
        """取端点引用的**设备 id**：手工规格的端点常带管口后缀（`T-101.top`、
        `E-101.tube_in`），判连接要按 id 比，不能拿带管口的整串去比设备集合
        （否则每个正常接管都会被误报成"不是设备"）。"""
        return str(ref).partition(".")[0]

    def is_point(x: object) -> bool:
        """端点是否为**自由坐标**（`"620,430"` / `[620,430]`）——界区/接续的落点。

        自由坐标是"这条线在图纸边界进出"，**不是设备引用**，不适用"设备—流股—设备"
        拓扑判据（手工规格里大量使用，误判会把正常规格全判违规）。
        """
        if isinstance(x, (list, tuple)) and len(x) == 2:
            try:
                float(x[0]); float(x[1])
                return True
            except (TypeError, ValueError):
                return False
        parts = str(x).replace(" ", "").split(",")
        if len(parts) != 2:
            return False
        try:
            float(parts[0]); float(parts[1])
            return True
        except ValueError:
            return False

    eq_set = {str(e.get("id")) for e in (spec.get("equipment") or [])}
    # 拓扑结点**不是"没画"**，是按图库口径**不出设备符号**——它们以管线折点/直通点
    # 的身份存在（见 `spec["_nodes"]`）。所以拓扑校验的**合法端点 = 设备 ∪ 结点**，
    # 保真比对的"丢失设备"也要把它们排除。漏了这一步，凡是经过 MIXER/FSPLIT/VALVE
    # 的流股都会被判成"设备直连设备"，整张图拒绝落盘。
    node_ids = {str(x) for x in ((spec.get("_nodes") or {}).get("ids") or [])}
    eq_ext = eq_set | node_ids
    pipes = spec.get("pipes") or []
    pipe_ids = {str(p.get("id")) for p in pipes}
    ref = ref if ref is not None else spec.get("_bkp_ref")
    has_ref = bool(ref)                     # bkp 派生的规格才做"保真"强校验

    # ---- C3 拓扑严格交替（永远校验；坐标端点跳过，因为它不是设备引用）----
    for p in pipes:
        ra, rb = p.get("from", ""), p.get("to", "")
        a, b = head(ra), head(rb)
        if not is_point(ra) and a not in eq_ext:
            err("连接拓扑", f"管线 {p.get('id')} 的起点 {a!r} 不是设备"
                            "（不许设备直连设备 / 流股接流股）")
        if not is_point(rb) and b not in eq_ext:
            err("连接拓扑", f"管线 {p.get('id')} 的终点 {b!r} 不是设备"
                            "（不许设备直连设备 / 流股接流股）")
        if not is_point(ra) and not is_point(rb) and a and a == b:
            err("连接拓扑", f"管线 {p.get('id')} 自环（{a} → {a}）")
        if (a in pipe_ids) or (b in pipe_ids):
            err("连接拓扑", f"管线 {p.get('id')} 接到了另一条管线上（流股接流股）")

    # ---- C2 接续标志 ----
    for op in (spec.get("offpage") or []):
        dest = str(op.get("dest", ""))
        at = op.get("at", "")
        if not is_point(at) and head(at) not in eq_ext:
            err("接续标志", f"接续标志挂点 {at!r} 不在本图设备上")
        if (not dest) or ("去" not in dest and "自" not in dest):
            # bkp 派生：跨工段接续**必须**标「自X来/去Y」→ 硬错误；
            # 手工规格：可能是有意的裸接续符号 → 只提示，不拦（不能拿它判别人的规格违规）
            (err if has_ref else warn)(
                "接续标志", f"接续标志缺「自X来 / 去Y」归属文字：{dest!r}")

    # ---- C2b 进出界区标志（单端流股：原料进 / 产品出）----
    for _b in (spec.get("battery") or []):
        _dest = str(_b.get("dest", ""))
        _at = _b.get("at", "")
        # 界区标志的落点**可以是设备 id，也可以是自由坐标**（流股穿出界区的真实位置）——
        # 只有"看着像设备引用、却不在本图设备里"才算错。
        if (not is_point(_at)) and head(_at) not in eq_ext:
            err("界区标志", f"进/出界区标志挂点 {_at!r} 不在本图设备上")
        if (not _dest) or ("界区" not in _dest):
            (err if has_ref else warn)(
                "界区标志", f"进/出界区标志缺「自界区来 / 去界区」文字：{_dest!r}")

    # ---- C1 / C4 与 bkp 基线比对（仅 bkp 派生规格）----
    if not has_ref:
        return out                                  # 无溯源（手工规格）→ 只保拓扑
    scope = str(ref.get("scope", "full"))
    ref_blocks = {str(x) for x in (ref.get("blocks") or [])}
    ref_pairs = [(head(a), head(b)) for a, b in (ref.get("streams") or [])]
    spec_pairs = [(head(p.get("from", "")), head(p.get("to", ""))) for p in pipes]
    spec_pairs = [pr for pr in spec_pairs if not is_point(pr[0]) and not is_point(pr[1])]

    extra_eq = sorted(eq_set - ref_blocks)
    if extra_eq:
        err("捏造设备", f"图上有 bkp 里没有的设备 {len(extra_eq)} 台：{extra_eq[:8]}")
    if scope == "full":
        miss_eq = sorted(ref_blocks - eq_set - node_ids)
        if miss_eq:
            err("丢失设备", f"bkp 有 {len(miss_eq)} 台设备没画：{miss_eq[:8]}")

    cs, cr = Counter(spec_pairs), Counter(ref_pairs)
    fab = list((cs - cr).elements())
    if fab:
        err("捏造连接", f"图上有 bkp 里没有的连接 {len(fab)} 条：{fab[:6]}")
    if scope == "full":
        miss = list((cr - cs).elements())
        if miss:
            err("丢失连接", f"bkp 有 {len(miss)} 条连接没画：{miss[:6]}")

    # 进出界区标志同样纳入保真比对（单端流股：一端是设备、另一端在界区外）
    def _batt_pairs(seq) -> List[Tuple[str, str]]:
        out2: List[Tuple[str, str]] = []
        for x in seq or []:
            pr = x.get("pair") if isinstance(x, dict) else x
            if not pr:
                continue
            out2.append((head(pr[0]), head(pr[1])))
        return out2

    rb = Counter(_batt_pairs(ref.get("battery")))
    sb = Counter(_batt_pairs(spec.get("battery")))
    fab_b = list((sb - rb).elements())
    if fab_b:
        err("捏造界区流股", f"图上多出 bkp 里没有的界区流股 {len(fab_b)} 条：{fab_b[:6]}")
    if scope == "full":
        miss_b = list((rb - sb).elements())
        if miss_b:
            err("丢失界区流股", f"bkp 有 {len(miss_b)} 条界区流股没画：{miss_b[:6]}")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="从 Aspen .bkp 生成 PFD 规格")
    ap.add_argument("bkp_json", help="chemcad_locate.py bkp ... --json 的产物")
    ap.add_argument("--out", required=True, help="输出规格 JSON")
    ap.add_argument("--name", default="")
    ap.add_argument("--sheet", default="A1")
    ap.add_argument("--scale", type=float, default=None)
    ap.add_argument("--no-fold-aux", action="store_true",
                    help="关闭辅助折叠：给所有设备（含泵/分离/混合/放空）都打位号")
    ap.add_argument("--fold-prefixes", default="P,S,M,VA",
                    help="辅助折叠的位号首字母集合（逗号分隔，默认 P,S,M,VA）")
    ap.add_argument("--split", metavar="DIR",
                    help="按三步工作流拆张（① 工段划分 → "
                         "② 识别各工段设备数并分配，每图 ≥6 台 → ③ 逐张做图）")
    a = ap.parse_args(list(argv) if argv is not None else None)
    with open(a.bkp_json, "r", encoding="utf-8") as f:
        d = json.load(f)
    try:
        spec = build_spec(d, name=a.name, sheet=a.sheet, scale=a.scale,
                          fold_aux=not a.no_fold_aux,
                          fold_prefixes=[p.strip() for p in a.fold_prefixes.split(",") if p.strip()])
    except BlockConfirmationRequired as exc:
        print(str(exc), file=sys.stderr)
        return 4

    # ===== 出图前硬门 ①：整份规格必须与 .bkp 连接一致（不得捏造 / 丢失）=====
    problems = [x for x in verify_spec_fidelity(spec, ref_from_bkp(d))
                if x.get("level") == "error"]
    if problems:
        for x in problems:
            print(f"  ✗ [{x['rule']}] {x['msg']}", file=sys.stderr)
        print(f"错误：规格与 .bkp 的连接不一致（{len(problems)} 项）——"
              "拒绝落盘（用户硬约束：不得捏造与 bkp 不同的流股/设备连接）", file=sys.stderr)
        return 3

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    _f = spec.get("_fold", {})
    print(f"设备 {len(spec['equipment'])} 台（折叠 {_f.get('folded', 0)} / 带位号 {_f.get('带位号', 0)}）"
          f" / 管线 {len(spec['pipes'])} 条 "
          f"/ 流股注记 {len(spec.get('streams') or {})} 条 "
          f"/ 界区标志 {len(spec.get('battery') or [])} 个 → {a.out}")

    if a.split:
        os.makedirs(a.split, exist_ok=True)
        subs = split_spec(spec)

        # ===== 出图前硬门 ②：拆张逐张 ⊆ 原 bkp，且合起来**覆盖**原全部流股（不许丢）=====
        split_bad: List[str] = []
        for i, s in enumerate(subs, 1):
            for x in verify_spec_fidelity(s):       # 用各张自带 _bkp_ref(part)
                if x.get("level") == "error":
                    split_bad.append(f"第 {i} 张【{x['rule']}】{x['msg']}")
        # 覆盖性：每条原始连接都要在某张里以"本张管线"或"接续标志"出现
        orig = Counter((str(p.get("from", "")), str(p.get("to", ""))) for p in spec["pipes"])
        got: Counter = Counter()
        for s in subs:
            for p in s["pipes"]:
                got[(str(p.get("from", "")), str(p.get("to", "")))] += 1
            for op in (s.get("offpage") or []):
                pr = op.get("pair")
                if pr:
                    got[(str(pr[0]), str(pr[1]))] += 1
        for pr, n in orig.items():
            if got.get(pr, 0) == 0:
                split_bad.append(f"【拆张丢流股】{pr[0]} → {pr[1]} 既未在本张画出、也无接续标志")
        for pr in got:
            if pr not in orig:
                split_bad.append(f"【拆张造流股】{pr[0]} → {pr[1]} 不在 .bkp 里")
        if split_bad:
            for m in split_bad[:12]:
                print(f"  ✗ {m}", file=sys.stderr)
            print(f"错误：拆张与 .bkp 不一致（{len(split_bad)} 项）——拒绝落盘", file=sys.stderr)
            return 3

        import hashlib
        identity=str(spec.get('_model_id') or 'bkp:'+hashlib.sha256(
            json.dumps(d,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest())
        manifest={'version':1,'sections':[]}
        grouped={}
        print(f"\n按「工段 → 工序代号」拆成 {len(subs)} 张：")
        for i, s in enumerate(subs, 1):
            info = s["_拆张"]
            # ⚠️ `_拆张` 存的是 `分工段`（单张整段时为 ""）；之前误写成 `工序代号`
            # 会 KeyError 崩在写文件前（verify_split_regression 走 split_spec 直调没踩到，
            # 这条 `--split` 落盘路径一直没人实跑过）。空工序号标「整段」更直观。
            _proc = info.get("分工段") or ""
            sub_label = f"工序{_proc}" if _proc else "整段"
            fn = os.path.join(a.split, f"{i:02d}_{info['工段']}_{sub_label}.json")
            s['_model_id']=identity
            section_name=str(info['工段'])
            if section_name not in grouped:
                item={'id':f'S{len(grouped)+1:03d}','name':section_name,'pages':[]}
                grouped[section_name]=item;manifest['sections'].append(item)
            grouped[section_name]['pages'].append(os.path.basename(fn))
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
            print(f"  {i:2d}. {info['工段']:<14} {sub_label:<8}  "
                  f"设备{info['本张设备']:>3} 管线{info['本张管线']:>3} "
                  f"跨张{info['跨张流股']:>3}  → {os.path.basename(fn)}")
        manifest_path=os.path.join(a.split,'book-manifest.json')
        with open(manifest_path,'w',encoding='utf-8') as f:
            json.dump(manifest,f,ensure_ascii=False,indent=2)
        print(f'工段装订清单 → {manifest_path}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
