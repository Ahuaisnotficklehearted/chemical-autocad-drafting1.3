#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""化工图纸 DXF 合规自检（轻量，无第三方依赖）。

检查项:
  1. 图层清单 + 与约定图层体系的差异；**两套命名体系不得混用**
  2. 图层线宽是否符合 HG 20519.1 的 粗 0.6~0.9 / 中粗 0.3~0.5 / 细 0.15~0.25 mm
     （读 LAYER 表的 370 组码；R12 等无 370 的文件跳过并提示）
  3. 直线正交性 —— **只对工艺线/信号线图层判定**（HG20519 要求正交布线）；
     符号内部斜线（阀门、箭头等）不计入违规，单列供参考
  4. 标题栏关键字（中文版 / 英文版**任一套完整即可**，不再要求两套齐全）
  5. %%c / %%d / %%p 与 \\U+XXXX 转义出现情况（文本比对前须解码）
  6. 实体规模概览

用法:
  python check_dxf.py <file.dxf|dir> [--angle-tol 0.5] [--min-len 0] [--json out.json]
"""
import os
import sys
import re
import math
import json
import argparse
from collections import Counter
from chemcad_locate import read_text_robust
from chemcad_layers import STD_BY_NAME


def unesc(s):
    """还原 DXF 中的 \\U+XXXX 转义（R12 及 R2007 以下非 ASCII 的书写惯例）。"""
    if "\\U+" not in s:
        return s
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 与绘图引擎共用同一份图层/线宽定义（单一事实来源）；不可导入时退化为内置集合
try:
    from chemcad_ir import LAYER_REGISTRY, LW_RANGES, resolve_layer, NUMERIC_ALIAS  # type: ignore
    _HAVE_REGISTRY = True
except ImportError:  # pragma: no cover
    _HAVE_REGISTRY = False

    def resolve_layer(n: str) -> str:  # type: ignore[misc]
        return n

    LAYER_REGISTRY = {}                 # type: ignore[assignment]
    NUMERIC_ALIAS = {}                  # type: ignore[assignment]
    # ⚠️ 这里是 `chemcad_ir.LW_RANGES` 的**副本**（本脚本要能脱离引擎独立跑）。
    # 改档位时两处都得改——`outline` 是《工程图纸质量专项评审》表 6.1.3 备注要求的
    # "设备、机器轮廓线 0.25mm"档，忘了同步这里，独立校核就会漏判。
    LW_RANGES = {"coarse": (60, 90), "medium": (30, 50),
                 "fine": (15, 25), "outline": (15, 25)}  # type: ignore[assignment]

# 应保持正交的图层（工艺线 / 信号线）
ORTHO_LAYERS = {
    "PIPE_PRI", "PIPE_INLINE", "AF_PIPE", "主物料", "次物料", "辅物料",
    "1", "2", "13", "15", "仪表线", "仪表", "INST", "SIGNAL",
}
def _std_layer_names() -> set:
    """本技能**出图时自己会写出的**标准层名（浙工大图层表，见 `chemcad_layers.STANDARD`）。

    ⚠️ 为什么必须并进 `KNOWN`：下面那份白名单是早年**对着外部图纸手抄**的
    （`ICISHT` / `L100_9` / `SHHQ1`…），里面**没有本技能自己的出图层名**。
    于是用它校核自产的图，每张都报"图层未识别 11 个"——
    校核器认不出自己产出的图层，这不是图的问题，是白名单过期了。
    """
    try:
        import chemcad_layers as CL
        return {s.name for s in CL.STANDARD}
    except Exception:                                    # pragma: no cover
        return set()


KNOWN = ORTHO_LAYERS | {
    "0", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "14", "16",
    "18", "19", "20", "21", "22", "62",
    "EQUIP", "EQUIP_IN", "NOZZLE", "VALVE", "FLOWARROW", "TAG",
    "MISC", "BORDER", "OTHERS", "CLOUD", "_Border", "_Cutline", "_Grid",
    "黄色", "Defpoints", "DEFPOINTS", "实线", "粗实线", "细实线", "点划线", "点画线",
    "标注", "DIM", "CENTERLINE", "HATCH", "Draw", "10-DWG-FRAME", "ICISHT",
    "bz", "cen", "contc", "contx", "sd", "1dim", "2中心线", "L100_9", "SHHQ1",
} | _std_layer_names()

# 标题栏：中文版与英文版**任一套完整**即视为合格
TITLE_CN = ["设计", "制图", "审核", "校对", "比例", "图号", "版次", "日期"]
TITLE_EN = ["REV", "DATE", "CHKD", "APPR", "SCALE"]


def analyze(path, angle_tol, min_len):
    # 读文件一律走 chemcad_locate.read_text_robust（逐级降级 utf-8-sig → utf-8 → gbk → latin-1）。
    # 不用 `errors="replace"`：它会把 GBK 中文层名/文字静静换成 U+FFFD，
    # 于是"读到了但全是问号"而不报错 —— 静默失真比报错难查。
    text, _enc = read_text_robust(path)
    L = [ln.rstrip("\r\n") for ln in text.splitlines()]

    layers, texts, ents = [], [], Counter()
    layer_defs = {}          # name -> {color, linetype, lineweight}
    lines_rec = []
    ent, cur = None, {}

    def flush():
        if ent == "LINE" and all(k in cur for k in ("10", "20", "11", "21")):
            try:
                lines_rec.append((cur.get("8", "0"), float(cur["10"]), float(cur["20"]),
                                  float(cur["11"]), float(cur["21"])))
            except ValueError:
                pass  # LINE 坐标非数值（损坏行）→ 跳过该条，不影响其余
        if ent in ("TEXT", "MTEXT") and cur.get("_t"):
            texts.append(cur["_t"])
        if ent == "LAYER" and cur.get("_n"):
            layer_defs[cur["_n"]] = {
                "color": cur.get("62"),
                "linetype": cur.get("6"),
                "lineweight": int(cur["370"]) if str(cur.get("370", "")).lstrip("-").isdigit() else None,
            }

    i, n = 0, len(L)
    while i < n - 1:
        code, val = L[i].strip(), L[i + 1]
        if code == "0":
            flush()
            cur, ent = {}, val
            if val not in ("SECTION", "ENDSEC"):
                ents[val] += 1
        else:
            if ent == "LINE" and code in ("10", "20", "11", "21"):
                cur[code] = val
            elif ent == "LINE" and code == "8":
                cur["8"] = val
            elif ent in ("TEXT", "MTEXT") and code in ("1", "3") and "_t" not in cur:
                cur["_t"] = val
            elif ent == "LAYER":
                if code == "2":
                    nm = unesc(val)
                    layers.append(nm)
                    cur["_n"] = nm
                elif code in ("62", "6", "370"):
                    cur[code] = val
        i += 2
    flush()

    # --- 正交性 ---
    violations, symbol_skew = [], []
    for lay, x1, y1, x2, y2 in lines_rec:
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length == 0 or length < min_len:
            continue
        ang = math.degrees(math.atan2(dy, dx)) % 90.0
        dev = min(ang, 90 - ang)
        if dev > angle_tol:
            item = {"layer": lay, "dev": round(dev, 2), "len": round(length, 1),
                    "p1": [x1, y1], "p2": [x2, y2]}
            (violations if resolve_layer(lay) in ORTHO_LAYERS else symbol_skew).append(item)

    # --- 线宽（HG 20519.1）---
    lw_note = ""
    lw_bad, lw_checked = [], 0
    for lay, d in sorted(layer_defs.items()):
        lw = d.get("lineweight")
        std = STD_BY_NAME.get(lay)
        if std is not None and lw is not None:
            lw_checked += 1
            if lw != std.lineweight:
                lw_bad.append({"layer": lay, "lineweight_mm": lw / 100 if lw >= 0 else None,
                               "expect": f"母图线宽码 {std.lineweight}"})
            continue
        if lw is None or lw < 0:
            continue
        sem = resolve_layer(lay)
        ld = LAYER_REGISTRY.get(sem) if _HAVE_REGISTRY else None
        if ld is None:
            continue
        lw_checked += 1
        lo, hi = ld.weight_range
        if not (lo <= lw <= hi):
            lw_bad.append({"layer": lay, "lineweight_mm": round(lw / 100.0, 2),
                           "expect": f"{lo/100:.2f}~{hi/100:.2f}"})
    if lw_checked == 0 and layers:
        lw_note = "文件无 370 线宽组码（R12 等旧版），线宽需按图层颜色配打印笔宽表"

    # --- 图层体系是否混用 ---
    used_sem = {l for l in set(layers) if resolve_layer(l) in LAYER_REGISTRY} if _HAVE_REGISTRY else set()
    used_num = {l for l in set(layers) if l.strip() in NUMERIC_ALIAS}
    mixed = bool(used_sem) and bool(used_num)

    # --- 文字转义 ---
    esc = Counter()
    for t in texts:
        for k in ("%%c", "%%d", "%%p", "\\U+"):
            if k in t:
                esc[k] += 1
    esc = {k: v for k, v in esc.items() if v}

    # --- 标题栏 ---
    dec = [unesc(t) for t in texts]      # 标题栏比对须先解码

    def has(k):
        return any(k in t for t in dec)

    cn_missing = [k for k in TITLE_CN if not has(k)]
    en_missing = [k for k in TITLE_EN if not has(k)]
    if not cn_missing:
        scheme, title_missing = "中文版", []
    elif not en_missing:
        scheme, title_missing = "英文版", []
    else:
        scheme = "中文版"
        title_missing = cn_missing

    return {
        "file": os.path.basename(path),
        "size_kb": round(os.path.getsize(path) / 1024, 1),
        "layers": sorted(set(layers)),
        "layers_unknown": sorted({l for l in set(layers) if l not in KNOWN}),
        "layer_defs": layer_defs,
        "layers_mixed_scheme": bool(mixed),
        "lineweights": {"checked": lw_checked, "violations": lw_bad, "note": lw_note},
        "entities": ents.most_common(),
        "lines_total": len(lines_rec),
        "violations": len(violations),
        "violations_by_layer": dict(Counter(v["layer"] for v in violations)),
        "violation_sample": violations[:12],
        "symbol_skew_nonortho": len(symbol_skew),
        "text_count": len(texts),
        "escapes": esc,
        "title_scheme": scheme,
        "title_block_missing": title_missing,
    }


def main():
    ap = argparse.ArgumentParser(description="DWG / DXF 合规自检（线宽/正交/图层体系）")
    ap.add_argument("target", help="DWG / DXF 文件或目录（.dwg 自动先转 DXF）")
    ap.add_argument("--angle-tol", type=float, default=0.5)
    ap.add_argument("--min-len", type=float, default=0.0,
                    help="忽略短于此长度的直线（用于剔除符号内部线段）")
    ap.add_argument("--no-ortho", action="store_true",
                    help="跳过正交性检查。用于**非流程类图纸**（剖面线、斜线本就是规范画法，"
                         "不是违规）；PFD 校核不要加")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    global ORTHO_LAYERS
    if a.no_ortho:
        ORTHO_LAYERS = set()

    import chemcad_dwg as DWG

    files = ([os.path.join(r, f) for r, _, fs in os.walk(a.target)
              for f in sorted(fs) if f.lower().endswith((".dxf", ".dwg"))]
             if os.path.isdir(a.target) else [a.target])

    out = {}
    for p in files:
        try:
            with DWG.reading(p) as src:
                r = analyze(src, a.angle_tol, a.min_len)
            r["file"] = os.path.basename(p)          # 报告写回**原始**文件名
        except Exception as e:
            print("ERR", p, e)
            continue
        out[r["file"]] = r
        print("=" * 68)
        print("%s (%.1f KB)" % (r["file"], r["size_kb"]))
        print("  图层 %d%s" % (len(r["layers"]),
              "" if not r["layers_unknown"] else "  ⚠ 未识别: %s" % r["layers_unknown"]))
        if r["layers_mixed_scheme"]:
            print("  ⚠ 同时出现数字序号制与语义命名制图层，同一项目不得混用")
        lw = r["lineweights"]
        if lw["note"]:
            print("  线宽: %s" % lw["note"])
        elif lw["violations"]:
            print("  ⚠ 线宽超档 %d 个：%s" % (
                len(lw["violations"]),
                ", ".join("%s=%smm(应%s)" % (v["layer"], v["lineweight_mm"], v["expect"])
                          for v in lw["violations"][:6])))
        else:
            print("  ✓ 线宽合规（已核 %d 个图层，粗0.6~0.9/中粗0.3~0.5/细0.15~0.25）"
                  % lw["checked"])
        if r["violations"]:
            print("  ⚠ 工艺/信号线非正交 %d 条，按图层: %s" % (r["violations"], r["violations_by_layer"]))
            for v in r["violation_sample"][:4]:
                print("      %-12s 偏 %.2f° len=%.1f %s→%s" % (
                    v["layer"], v["dev"], v["len"], v["p1"], v["p2"]))
        else:
            print("  ✓ 工艺/信号线全部正交（共 %d 条直线）" % r["lines_total"])
        if r["symbol_skew_nonortho"]:
            print("    （另有 %d 条非工艺图层的斜线，属符号内部，不计违规）" % r["symbol_skew_nonortho"])
        print("  文字 %d 条；转义: %s" % (r["text_count"], r["escapes"] or "无"))
        print("  标题栏(%s)缺项: %s" % (r["title_scheme"], r["title_block_missing"] or "无 ✓"))

    if a.json:
        open(a.json, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
        print("\nJSON ->", a.json)


if __name__ == "__main__":
    main()
