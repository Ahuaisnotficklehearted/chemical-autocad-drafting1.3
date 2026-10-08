#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图库目录查询（零依赖、只读）。

读 `assets/equipment-library/templates/pfd/equipment/catalog.json`，
提供三件事：

1. **符号元数据查询** —— `id` / `label`（中文名）/ `family`（设备族）/ `renderer`
   （几何渲染器）/ `size`（名义尺寸）/ `prefix`（位号前缀）/ `port_profile`（端口剖面）/
   `status`（图形状态）/ `reference`（对应参考样本图例）/ `aliases`。
2. **Aspen 模块映射查询** —— `aspen_block_map`：Aspen 块类型 → 中文标签，
   用于把模型模块解析到本技能符号名。
3. **覆盖对账** —— 核对目录中的具名图块实际存在且非空，缺项报告文件或块名错误。
   查询目录为零依赖；读取 DXF 的 coverage 子命令需要 ezdxf。

## 使用边界（硬约束，不要绕）

`catalog.json` 的 `mapping_policy` 原文是
**"Schematic candidates only; not mechanical selection."**——
目录保留设备类型元数据；`pending_block` 表示尚无已接入的具名块，不能出图。
**图形候选状态不等于工程选型批准**。本模块只回答"这个符号画成什么样"，
不回答"这台设备该不该选"。真实尺寸/管口/材料/机械参数必须来自项目依据。

## 命令行

```bash
python scripts/chemcad_catalog.py list                      # 当前设备类型总表
python scripts/chemcad_catalog.py list --family 流体输送      # 按族筛
python scripts/chemcad_catalog.py show pump.vacuum          # 单符号详情
python scripts/chemcad_catalog.py show 真空泵                # 也可用中文名
python scripts/chemcad_catalog.py aspen COMPR               # Aspen 块 → 符号
python scripts/chemcad_catalog.py coverage                  # 与本技能符号表对账
python scripts/chemcad_catalog.py families                  # 8 个族与计数
```

`--json` 可加到任一子命令上，输出机器可读结果。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: 图库根目录（相对本脚本：`scripts/` → `assets/equipment-library/`）
_LIB_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "equipment-library",
)

#: `catalog.json` 路径（唯一权威，不要另存副本以免漂移）
CATALOG_PATH = os.path.join(
    _LIB_ROOT, "templates", "pfd", "equipment", "catalog.json",
)

#: 分类图库 DXF 目录（8 族，每族一个文件）
EQUIPMENT_DXF_DIR = os.path.join(_LIB_ROOT, "templates", "pfd", "equipment")

#: 顶层模板 DXF 目录（图框/标题栏/符号库/设备模板）
TEMPLATE_DXF_DIR = os.path.join(_LIB_ROOT, "templates")

#: 参考样本图例目录（9 个 legend.*.dxf + manifest.json）
REFERENCE_SAMPLE_DIR = os.path.join(_LIB_ROOT, "reference_samples")

#: `catalog.json` 里 `mapping_policy` 的原文（供文档与报告引用，别改写）
SELECTION_CAVEAT = "Schematic candidates only; not mechanical selection."

#: 状态取值的中文解释（写进报告用；值来自 catalog 实测）
STATUS_MEANING: Dict[str, str] = {
    "legacy_geometry": "旧版几何（可画，图形候选）",
    "generic_schematic": "通用示意图（无专属几何）",
    "pending_block": "待接入具名块（不能出图）",
    "user_geometry": "用户新版图形（原始具名块）",
}

#: **已知悬空引用**：`catalog.json` 的 `reference` 指向了图库里**未一并提供**的文件名。
#:
#: 这是**上游仓库自身的缺陷**（实测：`pfd.page30` 在 `cad-pfd-drafting` 里只被
#: `catalog.json` 引用两次、`catalog.py` 提到，但从未提供对应 DXF），
#: 不是本技能搬入时漏拷。**不得据此伪造一个 DXF 冒充**——那会让"参考样本"变成假证据。
#:
#: 处置：在读目录时如实登记为悬空引用（`dangling_references()`），
#: 使 `selftest [24]` 能区分「搬漏了」（必须红）与「上游本来就没有」（如实放行、报告）。
DANGLING_REFERENCES: frozenset = frozenset({"pfd.page30"})

_catalog_cache: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_catalog(path: str = CATALOG_PATH) -> Dict[str, Any]:
    """读 `catalog.json`（带缓存）。文件缺失时抛 `FileNotFoundError`。"""
    global _catalog_cache
    if path == CATALOG_PATH and _catalog_cache is not None:
        return _catalog_cache
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "图库目录不存在：%s\n"
            "  → 图库资产应位于 assets/equipment-library/ 下；"
            "缺失时请从上游仓库重新引入（见 assets/equipment-library/SOURCE.md）。" % path
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if path == CATALOG_PATH:
        _catalog_cache = data
    return data


def symbols(path: str = CATALOG_PATH) -> List[Dict[str, Any]]:
    """返回当前符号元数据列表，包括待接入类型。"""
    return list(load_catalog(path).get("symbols") or [])


def block_source(symbol: Dict[str, Any], path: str = CATALOG_PATH) -> Tuple[str, str]:
    """目录条目 -> 实际块文件和名称；新版整理库不再按族猜文件名。"""
    filename = str(symbol.get("block_file") or (str(symbol["family"]) + "_1.dxf"))
    return os.path.join(os.path.dirname(os.path.abspath(path)), filename), str(symbol.get("block_name") or symbol["id"])




def aspen_block_map(path: str = CATALOG_PATH) -> Dict[str, Optional[str]]:
    """返回 Aspen 块类型 → 中文标签 的映射。

    值为 `None` 表示该块**是拓扑结点而非设备**
    （`MIXER` / `FSPLIT` / `SSPLIT` / `VALVE` / `PIPE` / `MULT` / `USER` 等），
    应保持结点语义、不画成设备符号。
    """
    return dict(load_catalog(path).get("aspen_block_map") or {})


def families(path: str = CATALOG_PATH) -> Dict[str, int]:
    """返回设备族 → 符号数。"""
    out: Dict[str, int] = {}
    for s in symbols(path):
        fam = str(s.get("family", ""))
        out[fam] = out.get(fam, 0) + 1
    return out




# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def find(key: str, path: str = CATALOG_PATH) -> Optional[Dict[str, Any]]:
    """按 `id`、`label`（中文名）或 `aliases` 查一个符号；查不到返回 `None`。

    匹配顺序：**精确 id → 精确 label → 精确 alias**，
    再退化为**大小写不敏感**的同一顺序。
    """
    syms = symbols(path)
    k = (key or "").strip()
    if not k:
        return None

    def _exact(s: Dict[str, Any]) -> bool:
        return k == str(s.get("id")) or k == str(s.get("label")) \
            or k in (s.get("aliases") or [])

    for s in syms:
        if _exact(s):
            return s
    low = k.lower()
    for s in syms:
        names = [str(s.get("id", "")), str(s.get("label", ""))] + [
            str(a) for a in (s.get("aliases") or [])
        ]
        if any(n.lower() == low for n in names):
            return s
    return None


def by_family(family: str, path: str = CATALOG_PATH) -> List[Dict[str, Any]]:
    """按设备族筛符号（族名支持子串匹配）。"""
    f = (family or "").strip()
    return [s for s in symbols(path) if f and f in str(s.get("family", ""))]




def resolve_aspen(module: str, path: str = CATALOG_PATH) -> Optional[str]:
    """Aspen 模块类型 → 图库中文标签；拓扑结点与未登记块返回 `None`。

    模块名大小写不敏感（`.bkp` 里大小写不稳定）。
    """
    m = (module or "").strip().upper()
    if not m:
        return None
    m = m.replace(" ", "").rstrip("-")
    for k, v in aspen_block_map(path).items():
        if str(k).upper() == m:
            return v
    return None


def is_topology_node(module: str, path: str = CATALOG_PATH) -> bool:
    """该 Aspen 块是否为**拓扑结点**（映射值为 `None`，应保持结点语义不画设备）。"""
    m = (module or "").strip().upper()
    if not m:
        return False
    for k, v in aspen_block_map(path).items():
        if str(k).upper() == m:
            return v is None
    return False


def reference_file(sym_or_id: Any, path: str = CATALOG_PATH) -> str:
    """符号 → 参考样本图例文件名（如 `legend.tray` → 绝对路径）；无参考返回 `""`。

    ⚠️ 若 `catalog.json` 指向的是**已知悬空引用**（见 `DANGLING_REFERENCES`），
    也返回 `""`——避免调用方拿到一个不存在的路径。
    """
    s = find(sym_or_id, path) if isinstance(sym_or_id, str) else sym_or_id
    if not s:
        return ""
    ref = str(s.get("reference") or "").strip()
    if not ref or ref in DANGLING_REFERENCES:
        return ""
    return os.path.join(REFERENCE_SAMPLE_DIR, ref + ".dxf")


def dangling_references(path: str = CATALOG_PATH) -> List[Dict[str, str]]:
    """列出 `catalog.json` 里**指向不存在文件**的 `reference`（含已知悬空项）。

    返回 `[{"id", "reference", "known"}]`；`known=True` 表示已在
    `DANGLING_REFERENCES` 里登记（上游缺陷），`known=False` 表示**可能是搬漏了**
    ——后者应当视为错误处理（`selftest [24]` 就是这么判的）。
    """
    out: List[Dict[str, str]] = []
    for s in symbols(path):
        ref = str(s.get("reference") or "").strip()
        if not ref:
            continue
        if os.path.isfile(os.path.join(REFERENCE_SAMPLE_DIR, ref + ".dxf")):
            continue
        out.append({"id": str(s.get("id")), "reference": ref,
                    "known": ref in DANGLING_REFERENCES})
    return out


# ---------------------------------------------------------------------------
# 覆盖对账：图库 ↔ 本技能符号表
# ---------------------------------------------------------------------------

def coverage(path: str = CATALOG_PATH) -> Dict[str, Any]:
    """核对目录中的具名块实际存在且非空；读取 DXF 需 ezdxf。"""
    import chemcad_block_library as BL
    syms = symbols(path)
    hits: List[Dict[str, str]] = []
    misses: List[Dict[str, str]] = []
    for s in syms:
        row = {
            "id": str(s.get("id")), "label": str(s.get("label")),
            "family": str(s.get("family")),
        }
        filename, block_name = block_source(s, path)
        try:
            BL.source({"block_file": filename, "block_name": block_name})
        except (BL.BlockConfirmationRequired, RuntimeError) as exc:
            row["error"] = str(exc)
            misses.append(row)
        else:
            row["keys"] = ", ".join((row["id"], row["label"], *(s.get("aliases") or [])))
            row["block"] = row["id"]
            hits.append(row)

    return {
        "total": len(syms),
        "direct": len(hits),
        "missing": len(misses),
        "hits": hits,
        "misses": misses,
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _p(sym: Dict[str, Any]) -> str:
    """一行式符号摘要。"""
    ref = sym.get("reference") or "-"
    sz = sym.get("size") or []
    sz_s = "×".join(str(round(float(v), 1)) for v in sz) if sz else "-"
    return "%-24s %-10s %-10s %-6s %-10s %-12s %s  %s" % (
        sym.get("id"), sym.get("label"), sym.get("family"),
        sym.get("prefix", ""), sz_s, sym.get("port_profile", ""), ref,
        STATUS_MEANING.get(str(sym.get("status")), str(sym.get("status", ""))),
    )


def _cmd_list(args: argparse.Namespace) -> int:
    syms = by_family(args.family) if args.family else symbols()
    if args.status:
        syms = [s for s in syms if str(s.get("status")) == args.status]
    if args.json:
        print(json.dumps(syms, ensure_ascii=False, indent=2))
        return 0
    print("图库 %d 个符号（mapping_policy: %s）" % (len(syms), SELECTION_CAVEAT))
    print("%-24s %-10s %-10s %-6s %-10s %-12s %s" % (
        "id", "中文名", "族", "前缀", "名义尺寸", "端口剖面", "参考样本 / 状态"))
    print("-" * 100)
    for s in syms:
        print(_p(s))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    s = find(args.key)
    if not s:
        import chemcad_assets as A
        asset=A.find(args.key)
        if asset:
            print(json.dumps({**asset,'absolute_file':A.source(asset)[0],
                              'absolute_preview':str(A.ROOT/asset['preview'])},ensure_ascii=False,indent=2));return 0
        print("未找到符号：%s" % args.key, file=sys.stderr);return 2
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    for k in ("id", "label", "family", "renderer", "size", "prefix",
              "port_profile", "defaults", "reference", "status", "aliases", "block_file", "block_name"):
        v = s.get(k, "")
        if k == "status":
            v = "%s（%s）" % (v, STATUS_MEANING.get(str(v), "?"))
        print("%-14s %s" % (k + ":", json.dumps(v, ensure_ascii=False)))
    ref = reference_file(s)
    if ref:
        print("%-14s %s" % ("参考样本文件:", ref))
    print("%-14s %s" % ("选型边界:", SELECTION_CAVEAT))
    return 0


def _cmd_aspen(args: argparse.Namespace) -> int:
    m = aspen_block_map()
    if args.module:
        got = resolve_aspen(args.module)
        if got is None:
            kind = "拓扑结点（保持结点语义，不画设备）" if is_topology_node(args.module) \
                else "未登记（映射缺失）"
            if args.json:
                print(json.dumps({"module": args.module.upper(), "symbol": None,
                                  "kind": kind}, ensure_ascii=False))
            else:
                print("%-12s → %s" % (args.module.upper(), kind))
            return 0
        if args.json:
            print(json.dumps({"module": args.module.upper(), "symbol": got},
                             ensure_ascii=False))
        else:
            print("%-12s → %s" % (args.module.upper(), got))
        return 0
    if args.json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return 0
    dev = [(k, v) for k, v in m.items() if v]
    nod = [k for k, v in m.items() if not v]
    print("Aspen 块映射 %d 项：设备 %d / 拓扑结点 %d" % (len(m), len(dev), len(nod)))
    print("-" * 60)
    for k, v in dev:
        print("  %-12s → %s" % (k, v))
    print("\n拓扑结点（不画设备符号，保持结点语义）：")
    print("  " + ", ".join(nod))
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    cov = coverage()
    if args.json:
        print(json.dumps(cov, ensure_ascii=False, indent=2))
        return 0 if cov["missing"] == 0 else 1
    print("图库符号覆盖对账：共 %d / 可直接命中 %d / 缺失 %d"
          % (cov["total"], cov["direct"], cov["missing"]))
    print("-" * 84)
    print("【可直接命中】%d" % cov["direct"])
    print("  %-24s %-10s %-10s %s" % ("id", "中文名", "族", "命中键 → 图块"))
    for r in cov["hits"]:
        print("  %-24s %-10s %-10s %s → %s"
              % (r["id"], r["label"], r["family"], r.get("keys"), r.get("block")))
    print()
    print("【缺失】%d（需确认图块或补明确别名）" % cov["missing"])
    print("  %-24s %-12s %-10s %s" % ("id", "中文名", "族", "错误"))
    for r in cov["misses"]:
        print("  %-24s %-12s %-10s %s"
              % (r["id"], r["label"], r["family"], r.get("error")))
    return 0 if cov["missing"] == 0 else 1


def _cmd_families(args: argparse.Namespace) -> int:
    fam = families()
    if args.json:
        print(json.dumps(fam, ensure_ascii=False, indent=2))
        return 0
    print("设备族 %d 个：" % len(fam))
    for k, v in sorted(fam.items(), key=lambda kv: -kv[1]):
        print("  %-12s %d 个" % (k, v))
    print()
    print("分类图库 DXF（每族一个）：")
    for k in fam:
        p = os.path.join(EQUIPMENT_DXF_DIR, "%s_1.dxf" % k)
        print("  %-12s %s" % (k, "有" if os.path.isfile(p) else "缺"))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="图库目录查询（当前设备类型 / Aspen 映射 / 实际块覆盖）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("list", help="列出符号")
    p1.add_argument("--family", default="", help="按设备族筛（子串匹配）")
    p1.add_argument("--status", default="", help="按状态筛")
    p1.add_argument("--json", action="store_true")
    p1.add_argument('--category',default='',help='equipment/valve/piping/instrument/connector/exterior/frame/unidentified')
    p1.add_argument('--source',default='',help='来源路径或来源编号')
    p1.set_defaults(func=_cmd_list)

    p2 = sub.add_parser("show", help="单符号详情（id / 中文名 / 别名）")
    p2.add_argument("key")
    p2.add_argument("--json", action="store_true")
    p2.set_defaults(func=_cmd_show)

    p3 = sub.add_parser("aspen", help="Aspen 块映射查询")
    p3.add_argument("module", nargs="?", default="", help="如 COMPR / MCOMPR / RADFRAC")
    p3.add_argument("--json", action="store_true")
    p3.set_defaults(func=_cmd_aspen)

    p4 = sub.add_parser("coverage", help="与本技能符号表对账")
    p4.add_argument("--json", action="store_true")
    p4.set_defaults(func=_cmd_coverage)

    p5 = sub.add_parser("families", help="设备族与分类图库文件")
    p5.add_argument("--json", action="store_true")
    p5.set_defaults(func=_cmd_families)

    args = ap.parse_args(argv)
    if args.cmd=='list' and (args.category or args.source):
        import chemcad_assets as A
        try:
            rows=A.assets(args.category,args.source)
            if args.category=='equipment' and not args.source:
                rows=[dict(s,category='equipment',origin='existing') for s in symbols()]+[r for r in rows if r['id'] not in {s['id'] for s in symbols()}]
            if args.json:print(json.dumps(rows,ensure_ascii=False,indent=2))
            else:
                for row in rows:print(row['id'],row['label'],row['category'],row.get('file',row.get('block_file','')))
            return 0
        except Exception as e:print(str(e),file=sys.stderr);return 3
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
