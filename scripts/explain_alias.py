#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""explain_alias.py —— 多义图例块（`FM`/`TJF`/`PS`）的一对多别名表与判定解释。

背景：图例里**同一个块可能承担两种含义**（实测 3 处，见 `symbol-library.md` §3.3）——
例如 `FM` 既是「闸阀」又是「夹套闸阀」。`BLOCK_ALIAS` 是一对一，装不下多义；
多义走 `BLOCK_AMBIGUITY` + 上下文消歧。本工具把规则摊开、并能对具体输入解释判定，
避免"按字母猜"那类**看不见的错误**（曾把 `FM`/`PS`/`PB` 猜成仪表圆圈）。

用法:
  python explain_alias.py                              # 打印多义块消歧表
  python explain_alias.py --block FM --hint 夹套        # 单条解释（看判定与理由）
  python explain_alias.py --spec 你的规格.json           # 扫描规格里所有多义块（需含 valves）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chemcad_symbols as S  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def print_table() -> None:
    print("多义图例块（一块两义）消歧表 —— 规则：**最长键优先**，default 仅作平局兜底\n")
    for blk, rules in S.BLOCK_AMBIGUITY.items():
        print(f"■ {blk}   （{len(rules)} 种含义）")
        for r in rules:
            mark = "  ← 默认（无命中/平局时）" if r.default else ""
            keys = "、".join((r.label, *r.keys))
            lay = f"   图层≈{r.layer}" if r.layer else ""
            print(f"    {r.symbol:<16} 触发词: {keys}{lay}{mark}")
        print()
    print("上下文来源（强度由高到低）：")
    print("  1. 直接写规范名（type=\"夹套闸阀\" / \"jacketed_gate\"）—— 绕过本表，最优先")
    print("  2. params.meaning / params.hint / params.intent —— 显式消歧词")
    print("  3. tag 文本中的触发词")
    print("  4. 图层名中的触发词（弱证据，加权 3 分）")


def explain(block: str, hint: str = "", tag: str = "", layer: str = "") -> None:
    ctx = {"hint": hint, "tag": tag, "layer": layer}
    sym, reason = S.disambiguate_alias(block, S._alias_words(ctx), layer)
    if sym is None:
        fam, key = S.resolve_symbol(block, ctx)
        print(f"{block}：**非多义块** → {fam}/{key}（未进消歧表，按单义别名解析）")
        return
    fam, key = S.resolve_symbol(block, ctx)
    print(f"{block}  +  上下文[hint={hint!r} tag={tag!r} layer={layer!r}]")
    print(f"   → 含义：{sym}（族 {fam}）    理由：{reason}")
    if reason == "默认":
        print("   ⚠️ 按默认值判定——若实际是另一种含义，请在 params 里加 "
              "{\"meaning\": \"...\"}，或直接写规范名")


def scan_spec(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    rows = []
    for v in spec.get("valves", []):
        rows.append(("valve", str(v.get("tag", "")), str(v.get("type", "")),
                     dict(v.get("params") or {})))
    for i in spec.get("instruments", []):
        rows.append(("instrument", str(i.get("tag", "")), str(i.get("kind", "")),
                     dict(i.get("params") or {})))
    hits = [r for r in rows if r[2].strip() in S.BLOCK_AMBIGUITY]
    if not hits:
        print(f"规格 {os.path.basename(path)}：未使用多义块（{', '.join(S.BLOCK_AMBIGUITY)}）")
        return
    n_def = 0
    print(f"规格 {os.path.basename(path)}：多义块 {len(hits)} 处")
    for kind, tag, blk, params in hits:
        ctx = {"tag": tag}
        for k in ("meaning", "hint", "intent"):
            if params.get(k):
                ctx[k] = params[k]
        sym, reason = S.disambiguate_alias(blk, S._alias_words(ctx),
                                           str(params.get("layer", "") or ""))
        if reason == "默认":
            n_def += 1
        mark = "默认·待复核" if reason == "默认" else reason
        print(f"  {kind:<10} {tag:<12} {blk:<5} → {sym:<16} [{mark}]")
    if n_def:
        print(f"\n⚠️ 其中 {n_def} 处按默认判定——出图前建议人工确认或补 params.meaning")


def main() -> int:
    ap = argparse.ArgumentParser(description="多义图例块消歧表与判定解释")
    ap.add_argument("--block", default=None, help="解释某个多义块的判定（如 FM）")
    ap.add_argument("--hint", default="", help="消歧词（等价 params.meaning/hint）")
    ap.add_argument("--tag", default="", help="位号（其中的触发词也参与判定）")
    ap.add_argument("--layer", default="", help="图层名（弱证据）")
    ap.add_argument("--spec", default=None, help="扫描规格 json 里所有多义块")
    a = ap.parse_args()
    if a.spec:
        scan_spec(a.spec)
    elif a.block:
        explain(a.block, a.hint, a.tag, a.layer)
    else:
        print_table()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
