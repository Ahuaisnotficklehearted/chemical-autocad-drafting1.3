#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""compare_bkp_dwg.py —— **bkp 模型 ↔ 手绘 DWG（PFD）对照比对**，带"核心吻合 / 零星容差"。

**为什么需要这个工具（两校样本的教训）**
========================================

手工做的 PFD 与 Aspen `.bkp` 模型**不会是 100% 闭环计数**。对照过浙工大（ZJUT）
与南工大（NJTech）两份获奖真实样本后，规律高度一致：

| 行为       | 类别            | NJTech 实测 | 含义 |
|------------|-----------------|------------|------|
| 核心 1:1   | 塔 T / 反应器 R | 11→11 / 5→5 | 全流程主设备一台不缺、不增 |
| 亮点展开   | 换热器 E / 阀 V | 22→35 / 9→17 | 换热网络、控制阀展开，DWG 反而**更多** |
| 辅助折叠   | 泵 P / 分离 S / 混合 M / 放空 VA | 24→0 / 5→0 / 7→2 / 3→0 | 画了图形符号但**不给设备位号** |
| 零星差异   | 界区阀 V0503/V0505、bkp 散号 C-2/C-3 | — | 图纸侧补画的公用工程接口 / 模型侧全局对象 |

所以**"简单地两边取集合差"必然误报**——会把 V0503/V0505 这种界区阀报成"多画了设备"、
把折叠掉的 24 台泵报成"丢了 24 台设备"。本脚本按上面的真实规律分**容差等级**：

* **core（核心）**：`T` / `R` —— **严格 1:1**。缺一台或多一台都判 `error`（这是唯一硬判据）。
* **fold（折叠类）**：`P` / `S` / `M` / `VA` —— bkp 有而 DWG 无 **属预期**（`ok`），
  DWG 有而 bkp 无 也只作 `note`（少量保留位号是常见做法，如 NJTech 的 M 留了 2 台）。
* **expand（展开类）**：`E` / `V` —— DWG 多于 bkp **属预期**（`ok`，亮点展开）；
  DWG 少于 bkp 才 `warn`（可能是漏画）。
* **boundary（界区/零星）**：DWG 里有、但所属工段在 bkp 中**压根不存在**
  （如 bkp 无 05 工段却有 V0503/V0505）→ 判为界区/公用工程接口，**不误报**（`note`）。
* **loose（散号/物料名）**：bkp 里的非标准位号（`C-2`/`C-3`/`C6H6`/`H2O`）→
  模型侧全局/界区对象，DWG 未现属正常（`note`）。
* **other**：以上都不属于的差异 → `warn`（需要人工确认）。

**用法**
======

    # ① DWG 先经 AutoCAD SaveAs(DXF,61) 导出，再直接抽位号
    python compare_bkp_dwg.py --bkp nj_bkp.json --dwg-dxf nj.dxf

    # ② DWG 的位号已由别的环节抽成 JSON（list，或 {"tags":[...]}）
    python compare_bkp_dwg.py --bkp nj_bkp.json --dwg-tags nj_tags.json

    # ③ 只比对若干图层上的文字（推荐：南工大样本次/csv 在 6-流股号、7-位号线）
    python compare_bkp_dwg.py --bkp nj_bkp.json --dwg-dxf nj.dxf --tag-layers "6-流股号,7-位号线"

    # ④ 输出 markdown 报告与 JSON 明细
    python compare_bkp_dwg.py --bkp nj_bkp.json --dwg-dxf nj.dxf --report out.md --json out.json

退出码：`0`=核心设备严格吻合（无 error）；`1`=核心设备不符（有 error）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:                                    # 复用 bkp_to_pfd 的位号口径，避免两处各写一遍
    from bkp_to_pfd import _is_std_tag, _proc_code
except Exception:                       # noqa: BLE001
    _is_std_tag = None                  # type: ignore[assignment]
    _proc_code = None                   # type: ignore[assignment]

# 位号口径的**本地兜底**（与 bkp_to_pfd._STD_TAG_RE 一致：类别字母 + 至少 3 位数字）
_STD_TAG_RE = re.compile(r"^[A-Za-z]+\d{3,}$")

# 容差等级 → 默认行为分类
CORE: Tuple[str, ...] = ("T", "R")            # 核心设备：严格 1:1
FOLD: Tuple[str, ...] = ("P", "S", "M", "VA")  # 辅助折叠：画图形不给位号
EXPAND: Tuple[str, ...] = ("E", "V")          # 亮点展开：DWG 常常更多


def alpha_prefix(tag: str) -> str:
    """取位号的**类别字母前缀**（`T0101` → `T`；`VA0501` → `VA`；`C6H6` → `C`）。"""
    return "".join(ch for ch in str(tag) if ch.isalpha())


def is_std(tag: str) -> bool:
    """是否为标准位号（类别字母 + 数字）。非标准的（`C6H6`/`DS-1`）判 False。"""
    if _is_std_tag is not None:
        return bool(_is_std_tag(tag))
    return bool(_STD_TAG_RE.match(str(tag)))


def section_of(tag: str) -> str:
    """位号前两位 = 工段号（两校样本共同验证的权威锚点）；非标准号返回 `--`。"""
    if _proc_code is not None:
        return str(_proc_code(tag))
    if not is_std(tag):
        return "--"
    digits = "".join(ch for ch in str(tag) if ch.isdigit())
    return digits[:2] if len(digits) >= 2 else "--"


def class_of(tag: str) -> str:
    """按类别前缀判容差等级：core / fold / expand / other。"""
    p = alpha_prefix(tag)
    if p in CORE:
        return "core"
    if p in FOLD:
        return "fold"
    if p in EXPAND:
        return "expand"
    return "other"


def is_loose(tag: str) -> bool:
    """是否为 bkp 侧"散号/按物料命名"的对象（`C-2`/`C-3`/`C6H6`/`H2O`）→ 容差处理。

    判据：**不是标准位号**，或带连字符的零散代号（`DS-1`/`C-1`）。
    这类对象通常是模型里的全局/界区对象或物料名，DWG 不现属正常。
    """
    t = str(tag)
    return (not is_std(t)) or ("-" in t)


# ---------------------------------------------------------------------------
# DWG 侧：位号抽取
# ---------------------------------------------------------------------------
def tags_from_dxf(path: str, tag_layers: Optional[Sequence[str]] = None) -> List[str]:
    """从 DXF 里抽设备位号（TEXT / MTEXT，逐个实体而非 COM 遍历 —— COM 会挂死）。

    ⚠️ 为什么要"离线快读"：参考样本 4 MB / 3.2 万实体，用 AutoCAD COM 逐实体
    遍历会卡死；`ezdxf` 离线读是秒级——这是上一轮踩过的坑，别退回 COM 遍历。

    `tag_layers` 为空 → 扫所有层；给了 → 只扫这些层（推荐给 `6-流股号`/`7-位号线`）。
    """
    import ezdxf                                          # type: ignore[import-untyped]
    want = {str(x).strip() for x in tag_layers} if tag_layers else None
    doc = ezdxf.readfile(path)
    out: List[str] = []
    for ent in doc.modelspace():
        try:
            dxftype = ent.dxftype()
        except Exception:                                 # noqa: BLE001
            continue
        if dxftype not in ("TEXT", "MTEXT"):
            continue
        try:
            lay = str(ent.dxf.layer)
        except Exception:                                 # noqa: BLE001
            lay = ""
        if want is not None and lay not in want:
            continue
        try:
            s = ent.text if dxftype == "TEXT" else ent.text
        except Exception:                                 # noqa: BLE001
            continue
        s = str(s).strip()
        if (s.startswith("(") and s.endswith(")")) or len(s) < 4:
            continue
        out.append(s)
    return out


def load_tags(path: str) -> List[str]:
    """读位号列表 JSON：支持 `[...]` 或 `{"tags":[...]}`。"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, dict):
        d = d.get("tags") or d.get("equipment") or []
    out: List[str] = []
    for x in d:
        if isinstance(x, dict):
            t = x.get("tag") or x.get("id")
            if t:
                out.append(str(t))
        elif x:
            out.append(str(x))
    return out


def normalize_tagset(tags: Iterable[str]) -> Set[str]:
    """位号集合归一：去掉 TAG 后可能附带的空格/句号，保留标准位号与散号原样。

    ⚠️ **去重但不去"非标准号"**——散号（C-2/C-3）要单独进 `loose` 类做容差，
    若在这里被洗掉，就会漏掉"模型侧全局对象"这一类正常差异。
    """
    out: Set[str] = set()
    for t in tags:
        s = str(t).strip().rstrip(".").replace(" ", "")
        if s:
            out.add(s)
    return out


# ---------------------------------------------------------------------------
# 比对主体
# ---------------------------------------------------------------------------
class Finding:
    """一条差异记录：`level`（error/warn/note/ok）+ 分类 + 说明。"""

    __slots__ = ("level", "kind", "tag", "msg")

    def __init__(self, level: str, kind: str, tag: str, msg: str) -> None:
        self.level = level
        self.kind = kind
        self.tag = tag
        self.msg = msg

    def as_dict(self) -> Dict[str, str]:
        return {"level": self.level, "kind": self.kind, "tag": self.tag, "msg": self.msg}


def compare(bkp_tags: Set[str], dwg_tags: Set[str]) -> Dict[str, object]:
    """核心比对：返回明细 / 计数 / 工段矩阵 / 判定。

    **容差设计的两条硬要求**（来自 NJTech 复盘）：
      1. 核心设备 T/R **严格 1:1**——这是唯一能出 `error` 的地方；
      2. 界区阀（V0503/V0505）与 bkp 散号（C-2/C-3）**不得误报为丢/多设备**。
    """
    bkp_secs = {section_of(t) for t in bkp_tags if not is_loose(t)}
    findings: List[Finding] = []
    matrix: Dict[str, Dict[str, Tuple[int, int]]] = defaultdict(dict)

    def bump(sec: str, pre: str, b: int = 0, d: int = 0) -> None:
        cur = matrix.get(sec, {}).get(pre, (0, 0))
        matrix[sec][pre] = (cur[0] + b, cur[1] + d)

    # —— 矩阵：先把两边各自的数量铺进去 ——
    for t in bkp_tags:
        if is_loose(t):
            continue
        bump(section_of(t), alpha_prefix(t), b=1)
    for t in dwg_tags:
        if is_loose(t):
            continue
        bump(section_of(t), alpha_prefix(t), d=1)

    # —— ① bkp 有 / DWG 无（"丢设备"候选）——
    for t in sorted(bkp_tags - dwg_tags):
        pre, cls = alpha_prefix(t), class_of(t)
        sec = section_of(t)
        if is_loose(t):
            findings.append(Finding("note", "loose", t,
                                    f"bkp 散号/物料名对象（{pre}），DWG 未单列属正常"
                                    f"（模型侧全局/界区对象）"))
        elif cls == "core":
            findings.append(Finding("error", "core_missing", t,
                                    f"核心设备（{pre}）在 DWG 中缺失——核心设备要求 1:1，须补画"))
        elif cls == "fold":
            findings.append(Finding("ok", "fold_expected", t,
                                    f"辅助折叠类（{pre}）DWG 未给位号，符合获奖作品口径"
                                    f"（画图形不给位号）"))
        elif cls == "expand":
            findings.append(Finding("warn", "expand_missing", t,
                                    f"展开类（{pre}）DWG 少于 bkp，请确认是否真的漏画"))
        else:
            findings.append(Finding("warn", "other_missing", t,
                                    f"其余类别（{pre}）DWG 缺失，需人工确认（工段 {sec}）"))

    # —— ② DWG 有 / bkp 无（"多设备"候选）——
    for t in sorted(dwg_tags - bkp_tags):
        pre, cls, sec = alpha_prefix(t), class_of(t), section_of(t)
        if is_loose(t):
            findings.append(Finding("note", "loose", t,
                                    f"DWG 侧非标准位号（{pre}），可能为编号写法差异，人工确认"))
            continue
        # ⚠️ **顺序是刻意的：核心设备（T/R）的严格性必须压过一切容差规则。**
        #    否则一颗多画的核心塔只要落在 bkp 不存在的工段里（如 T0999 / 工段 09），
        #    就会被下面那条"界区接口"规则悄悄吃掉，变成一条 note——
        #    实测负例即如此：漏画的 T0103 报了 error，多画的 T0999 却被判成界区。
        #    这条规则本是为 V0503/V0505 那类**非核心**的界区阀设的。
        if cls == "core":
            findings.append(Finding("error", "core_extra", t,
                                    f"核心设备（{pre}）在 DWG 中多出——bkp 模型里没有对应模块"))
            continue
        # ★ 界区/零星：DWG 里出现的位号，其**工段**在 bkp 中根本不存在
        #   （如 bkp 无 05 工段却有 V0503/V0505）→ 判为界区/公用工程接口，不误报。
        if sec not in bkp_secs:
            findings.append(Finding("note", "boundary", t,
                                    f"DWG 工段 {sec} 在 bkp 中不存在 → 判为界区/公用工程"
                                    f"接口件（如 V0503/V0505），不算多设备"))
        elif cls == "expand":
            findings.append(Finding("ok", "expand_expected", t,
                                    f"展开类（{pre}）DWG 多于 bkp，符合亮点展开做法"
                                    f"（换热网络/控制阀展开）"))
        elif cls == "fold":
            findings.append(Finding("note", "fold_kept", t,
                                    f"折叠类（{pre}）DWG 仍给了位号，少量保留属常见做法"))
        else:
            findings.append(Finding("warn", "other_extra", t,
                                    f"其余类别（{pre}）DWG 多出，需人工确认（工段 {sec}）"))

    # —— ③ 核心设备的**逐秘书长 faults** 汇总 ——
    bkp_core = {t for t in bkp_tags if class_of(t) == "core"}
    dwg_core = {t for t in dwg_tags if class_of(t) == "core"}
    counts = Counter(f.level for f in findings)
    return {
        "findings": [f.as_dict() for f in findings],
        "counts": dict(counts),
        "core": {
            "bkp": sorted(bkp_core), "dwg": sorted(dwg_core),
            "matched": sorted(bkp_core & dwg_core),
            "missing": sorted(bkp_core - dwg_core),
            "extra": sorted(dwg_core - bkp_core),
            "strict_ok": bkp_core == dwg_core,
        },
        "matrix": {s: {p: {"bkp": v[0], "dwg": v[1]} for p, v in ps.items()}
                   for s, ps in matrix.items()},
        "totals": {"bkp": len(bkp_tags), "dwg": len(dwg_tags),
                   "bkp_std": len([t for t in bkp_tags if not is_loose(t)]),
                   "dwg_std": len([t for t in dwg_tags if not is_loose(t)])},
        "verdict": "PASS" if counts.get("error", 0) == 0 else "FAIL",
    }


def render_md(res: Dict[str, object], bkp_path: str, dwg_path: str) -> str:
    """把比对结果渲染成 markdown 报告。"""
    L: List[str] = []
    core = res["core"]                                   # type: ignore[index]
    tot = res["totals"]                                  # type: ignore[index]
    cnt = res["counts"]                                  # type: ignore[index]
    L.append("# bkp ↔ DWG 对照比对报告（带容差）\n")
    L.append(f"- bkp 模型：`{bkp_path}`\n- DWG/PFD：`{dwg_path}`\n")
    L.append(f"**判定：{res['verdict']}**（核心设备 T/R 严格 1:1"
             f"{' 已吻合' if core['strict_ok'] else ' 不吻合'}）\n")
    L.append(f"> 位号总数 bkp {tot['bkp_std']} / DWG {tot['dwg_std']}"
             f"（含散号则 {tot['bkp']} / {tot['dwg']}）\n")

    L.append("## 1. 差异分级统计\n")
    L.append("| 级别 | 条数 | 含义 |\n|---|---|---|")
    for lv, name in (("error", "必须修（核心设备不符）"),
                     ("warn", "需人工确认"),
                     ("note", "容差内的正常差异（界区/散号/折叠保留）"),
                     ("ok", "符合预期（折叠/展开）")):
        L.append(f"| {lv} | {cnt.get(lv, 0)} | {name} |")
    L.append("")

    L.append("## 2. 核心设备 T/R 严格核对\n")
    L.append(f"- bkp 核心 {len(core['bkp'])} 台 / DWG 核心 {len(core['dwg'])} 台 / "
             f"吻合 {len(core['matched'])} 台")
    if core["missing"]:
        L.append(f"- ⚠ DWG 缺失（须补画）：{', '.join(core['missing'])}")
    if core["extra"]:
        L.append(f"- ⚠ DWG 多出（bkp 无对应模块）：{', '.join(core['extra'])}")
    if core["strict_ok"]:
        L.append("- ✓ 核心设备逐台吻合")
    L.append("")

    L.append("## 3. 工段 × 类别矩阵（bkp ⇒ dwg）\n")
    mtx = res["matrix"]                                  # type: ignore[index]
    pres = sorted({p for ps in mtx.values() for p in ps})
    L.append("| 工段 | " + " | ".join(pres) + " |")
    L.append("|---|" + "---|" * len(pres))
    for sec in sorted(mtx):
        row = []
        for p in pres:
            v = mtx[sec].get(p)
            row.append(f"{v['bkp']}→{v['dwg']}" if v else "–")
        L.append(f"| **{sec}** | " + " | ".join(row) + " |")
    L.append("")

    L.append("## 4. 差异明细\n")
    order = {"error": 0, "warn": 1, "note": 2, "ok": 3}
    fs = sorted(res["findings"], key=lambda x: (order.get(x["level"], 9), x["tag"]))  # type: ignore[union-attr]
    if not fs:
        L.append("（两边位号集合完全一致）")
    else:
        L.append("| 级别 | 分类 | 位号 | 说明 |\n|---|---|---|---|")
        for f in fs:
            L.append(f"| {f['level']} | {f['kind']} | `{f['tag']}` | {f['msg']} |")
    L.append("")
    L.append("> 容差规则：`T/R` 核心严格 1:1；`P/S/M/VA` 折叠类缺位号属预期；"
             "`E/V` 展开类 DWG 更多属预期；DWG 中**工段不存在于 bkp** 的位号判为界区/"
             "公用工程接口；bkp 非标准位号（散号/物料名）判为模型侧全局对象。")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="bkp 模型 ↔ 手绘 PFD(DWG) 对照比对（核心严格、零星容差）")
    ap.add_argument("--bkp", required=True, help="Aspen .bkp 提取结果 JSON（含 blocks）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dwg-dxf", help="DWG 经 SaveAs(DXF) 导出的 DXF 文件")
    g.add_argument("--dwg-tags", help="已抽取的 DWG 位号 JSON（[...] 或 {\"tags\":[...]}）")
    ap.add_argument("--tag-layers", default="",
                    help="只在这些图层上抽位号（逗号分隔），推荐 6-流股号,7-位号线")
    ap.add_argument("--report", help="markdown 报告输出路径")
    ap.add_argument("--json", dest="json_out", help="JSON 明细输出路径")
    a = ap.parse_args(list(argv) if argv is not None else None)

    with open(a.bkp, "r", encoding="utf-8") as f:
        bj = json.load(f)
    bkp_tags = normalize_tagset(str(b.get("id")) for b in (bj.get("blocks") or []))
    # bkp 侧的 device 也可能用 tag 字段
    for b in (bj.get("blocks") or []):
        if b.get("tag") and b.get("tag") != b.get("id"):
            pass                                         # id 即最终位号，保守只取 id
    if not bkp_tags:
        print("错误：bkp 里没有 blocks / id", file=sys.stderr)
        return 2

    if a.dwg_dxf:
        layers = [x.strip() for x in a.tag_layers.split(",") if x.strip()] or None
        try:
            dwg_tags = normalize_tagset(tags_from_dxf(a.dwg_dxf, layers))
        except ImportError:
            print("错误：需要 ezdxf 才能读 DXF（pip install ezdxf）", file=sys.stderr)
            return 2
        dwg_src = a.dwg_dxf
    else:
        dwg_tags = normalize_tagset(load_tags(a.dwg_tags))
        dwg_src = a.dwg_tags

    res = compare(bkp_tags, dwg_tags)
    core = res["core"]                                   # type: ignore[index]

    print(f"bkp {len(bkp_tags)} 个位号 ↔ DWG {len(dwg_tags)} 个位号  "
          f"（来源 {os.path.basename(dwg_src)}）")
    print(f"差异分级：{res['counts']}")                    # type: ignore[index]
    print(f"核心 T/R：bkp {len(core['bkp'])} / DWG {len(core['dwg'])} / "
          f"吻合 {len(core['matched'])} → "
          f"{'✓ 严格 1:1 吻合' if core['strict_ok'] else '✗ 不吻合'}")
    if core["missing"]:
        print(f"  ⚠ DWG 缺失核心：{', '.join(core['missing'])}")
    if core["extra"]:
        print(f"  ⚠ DWG 多出核心：{', '.join(core['extra'])}")
    print(f"判定：{res['verdict']}")

    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(render_md(res, a.bkp, dwg_src))
        print(f"markdown 报告 → {a.report}")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"JSON 明细 → {a.json_out}")

    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
