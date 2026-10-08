#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""read_dwg.py —— 从**运行中的 AutoCAD 图纸**读回结构，并对图层体系做合规核对。

为什么需要它
------------
`inspect_dxf.py` / `check_dxf.py` 只能核**磁盘上的 DXF 文件**。实际会审场景里，
成果往往还开着、未存盘、或是 DWG 原生格式（不是 DXF）——那条路就走不通。
本脚本补上 COM 读图这条腿：**只要 AutoCAD 开着，就能读并核**。

用法:
    # 先看开了哪几张图纸，再按关键词切过去读
    python read_dwg.py --list-docs
    python read_dwg.py --doc "S-2-20-03"

    # 读全部（图层表 + 实体统计 + 文字 + 块引用 + 包围盒）
    python read_dwg.py

    # 只读某图层（走选择集按 DXF 组码 8 过滤，比全图遍历快得多）
    python read_dwg.py --layer INST

    # 落 JSON，供下游脚本消费
    python read_dwg.py --json read_report.json
    python read_dwg.py --layer 0 --json layer0.json

退出码:
    0  读取成功（图层合规问题只提示，不影响退出码）
    3  AutoCAD 不可用（未运行 / 缺 pyautocad+pywin32 / 被对话框阻塞）—— 给可执行建议，不抛栈
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chemcad_engine as E                      # noqa: E402
from chemcad_ir import LAYER_REGISTRY, LW_RANGES, resolve_layer   # noqa: E402


def check_layers(rows: Sequence[Dict[str, object]]) -> List[Dict[str, str]]:
    """把读回的图层表与 HG 20519.1 语义命名注册表对照。

    只报「能确定的问题」：
      * 图层名不在注册表内 → 无法按图层做线宽校核（提示，不判错——各院命名各异）；
      * 图层线宽落在注册表区间之外 → 报错（这是硬性条文）。
    """
    out: List[Dict[str, str]] = []
    for rec in rows:
        name = str(rec.get("name", ""))
        ld = LAYER_REGISTRY.get(resolve_layer(name))
        if ld is None:
            continue
        lw = rec.get("lineweight")
        try:
            lw_i = int(lw) if lw is not None else None
        except Exception:
            lw_i = None
        lo, hi = ld.weight_range
        if lw_i is None or lw_i < 0:
            out.append({"level": "warning", "rule": "线宽未设",
                        "msg": f"图层 {name} 线宽为 {lw}（负值=跟随默认/ByLayer），"
                               f"按 HG 20519.1 应为 {lo/100:.2f}~{hi/100:.2f}mm"})
        elif not (lo <= lw_i <= hi):
            out.append({"level": "error", "rule": "线宽",
                        "msg": f"图层 {name} 线宽 {lw_i/100:.2f}mm 超出 "
                               f"{lo/100:.2f}~{hi/100:.2f}mm（HG 20519.1）"})
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="从运行中的 AutoCAD 读回图纸结构并核对图层")
    ap.add_argument("--doc", default=None, metavar="关键词",
                    help="在已打开的图纸中按名称/路径关键词切换活动图纸")
    ap.add_argument("--layer", default=None, help="只读该图层（按 DXF 组码 8 过滤）")
    ap.add_argument("--list-docs", action="store_true", help="只列出已打开的图纸后退出")
    ap.add_argument("--create", action="store_true", help="AutoCAD 未运行时创建实例")
    ap.add_argument("--invisible", action="store_true", help="不显示 AutoCAD 窗口")
    ap.add_argument("--no-layers", action="store_true", help="跳过图层表读取（大图更快）")
    ap.add_argument("--max-texts", type=int, default=20000, help="文字条数上限（默认 20000）")
    ap.add_argument("--json", default=None, help="把结果写入 JSON")
    a = ap.parse_args(argv)

    try:
        reader = E.ChemComReader(create_if_not_exists=a.create, visible=not a.invisible)
        if a.list_docs:
            docs = reader.list_documents()
            print(f"当前打开 {len(docs)} 张图纸：")
            for name, path in docs:
                print(f"  · {name}    {path}")
            return 0
        if a.doc:
            hit = reader.activate(a.doc)
            if hit is None:
                print(f"错误：找不到与 '{a.doc}' 匹配的图纸；用 --list-docs 看清单。",
                      file=sys.stderr)
                return 3
            print(f"已切换活动图纸 → {hit}")
        rep = reader.read(layer=a.layer, max_texts=a.max_texts)
    except (ImportError, ConnectionError) as e:
        print(f"\n错误：{e}", file=sys.stderr)
        print("提示：若核的是磁盘文件，改用 inspect_dxf.py <file.dxf>；"
              "若装不上 pywin32，本脚本不可用（DXF 通道不受影响）。", file=sys.stderr)
        return 3
    except Exception as e:                          # COM 偶发故障（RPC 忙等）
        print(f"\n错误：读图失败（{type(e).__name__}）：{e}", file=sys.stderr)
        print("提示：确认 AutoCAD 没有弹窗在等输入（按 ESC 关掉），再重试。", file=sys.stderr)
        return 3

    print(E.ChemComReader.format_report(rep))

    layer_rows: List[Dict[str, object]] = []
    issues: List[Dict[str, str]] = []
    if not a.no_layers:
        try:
            client = getattr(reader, "client", None)
            if hasattr(client, "layer_table"):
                layer_rows = client.layer_table()          # type: ignore[union-attr]
                issues = check_layers(layer_rows)
        except Exception as e:
            issues.append({"level": "warning", "rule": "图层表",
                           "msg": f"图层表读取失败（{type(e).__name__}）：{e}"})
        if layer_rows:
            on_reg = sum(1 for r in layer_rows if resolve_layer(str(r.get("name", "")))
                         in LAYER_REGISTRY)
            print("-" * 68)
            print(f"图层表 {len(layer_rows)} 个，其中 {on_reg} 个命中 HG 20519 语义命名注册表")
            for r in layer_rows[:25]:
                print(f"  {str(r.get('name')):<18} 色{r.get('color')} "
                      f"线型{r.get('linetype')} 线宽{r.get('lineweight')}")
            if len(layer_rows) > 25:
                print(f"  … 其余 {len(layer_rows) - 25} 个略")
        errs = [i for i in issues if i["level"] == "error"]
        if issues:
            print(f"\n图层合规：{len(errs)} 错 / {len(issues) - len(errs)} 提示")
            for i in issues[:40]:
                print(f"  [{'✗' if i['level'] == 'error' else '⚠'} {i['rule']}] {i['msg']}")
        else:
            print("\n图层合规：注册表内图层线宽全部落在 HG 20519.1 三档区间 ✓")

    if a.json:
        payload = dict(rep)
        payload["layer_table"] = layer_rows
        payload["layer_issues"] = issues
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n结果 JSON → {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
