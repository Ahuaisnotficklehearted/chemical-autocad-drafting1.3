#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 DXF 中的符号块挖掘上下文证据（通用版）。

用途：拿到一套陌生的 DXF 图例/图纸时，**反推每个块的含义**——
     图例图上通常会把块和它的中英文名称放在一起，因此对每个 BLOCK，
     找到它所有 INSERT 的位置，再取最近若干条 TEXT/MTEXT 即得候选名称。

用法:
  python mine_block_context.py <file.dxf|目录> [--top 6] [--min-count 1] [--json out.json]
  python mine_block_context.py 图例.dxf --block BV CHECK OVL      # 只查指定块
  python mine_block_context.py 图例.dxf --layer ICISHT            # 只看某层上的 INSERT
  python mine_block_context.py 图例.dxf --rows [--tol 4]          # 严格同行配对（推荐）

输出: 块名 | 引用次数 | 所在图层 | 邻近文字TOP-N
提示: 若某块旁边是一组并列名称（如"球阀/蝶阀/闸阀"），则相邻文字法一对一**不可靠**——
      改用 `--rows`：以 INSERT 点 y 与文字 y 的**同行关系**唯一定名（|Δy|<=tol 判同行，
      取块右侧最近一列为标签）。此法可复现、不依赖肉眼，能识别**复用块**（一块多义）。
"""
import os, re, math, json, argparse
from collections import Counter, defaultdict
from chemcad_locate import read_text_robust


def unesc(s):
    return s if "\\U+" not in s else \
        re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)


def parse(path):
    # 读文件一律走 chemcad_locate.read_text_robust（逐级降级 utf-8-sig → utf-8 → gbk → latin-1）。
    # 不用 `errors="replace"`：它会把 GBK 中文层名/文字静静换成 U+FFFD，
    # 于是"读到了但全是问号"而不报错 —— 静默失真比报错难查。
    text, _enc = read_text_robust(path)
    L = [ln.rstrip("\r\n") for ln in text.splitlines()]
    n = len(L)
    blocks, inserts, texts = [], [], []
    i = 0
    while i < n - 1:
        code, val = L[i].strip(), L[i + 1]
        if code == "0" and val in ("BLOCK", "INSERT", "TEXT", "MTEXT"):
            ent, cur = val, {}
            j = i + 2
            while j < n - 1:
                c = L[j].strip()
                if c == "0":
                    break
                v = L[j + 1]
                if c == "8":
                    cur["layer"] = v
                elif c == "2":
                    cur["name"] = unesc(v)
                elif c == "10":
                    cur["x"] = v
                elif c == "20":
                    cur["y"] = v
                elif c in ("1", "3") and ent in ("TEXT", "MTEXT") and "t" not in cur:
                    cur["t"] = unesc(v)
                j += 2
            try:
                x, y = float(cur.get("x", "nan")), float(cur.get("y", "nan"))
            except ValueError:
                x = y = float("nan")
            if ent == "BLOCK" and cur.get("name") and not cur["name"].startswith(("*", "$")):
                blocks.append(cur["name"])
            elif ent == "INSERT" and cur.get("name") and not math.isnan(x):
                inserts.append((cur["name"], x, y, cur.get("layer", "?")))
            elif ent in ("TEXT", "MTEXT") and cur.get("t", "").strip() and not math.isnan(x):
                texts.append((cur["t"].strip(), x, y, cur.get("layer", "?")))
            i = j
            continue
        i += 2
    return blocks, inserts, texts


def rows_report(all_ins, all_txt, tol, names, want_json):
    """严格同行配对：块 INSERT 点 y ↔ 文字标签 y（|Δy|<=tol 判同行）。

    返回 {块名: [{at, layer, right, left, nearest_right, nearest_left}, ...]}。
    `right`/`left` 是同行内位于块右侧/左侧的文字，按 x 由近到远排序——
    **右侧第一条即该块的图例名**（先按同行过滤、再按 x 取最近，比"看邻近文字"硬）。
    `nearest_*` 是不限同行的最近文字（带 Δy），当同行无标签时的兜底线索。

    一个块若出现多行不同标签，就是**复用块**（一块多义），需在别名表里留默认值 +
    另一种含义走显式符号名。
    """
    def _nearest(px, py, side):
        cand = [(t, tx, ty, abs(ty - py)) for (t, tx, ty, _l) in all_txt
                if (tx > px if side == "r" else tx < px)]
        if not cand:
            return None
        # 兜底线索：**先取 y 最近的行、再取 x 最近**（不能只看 x——同 x 的另一行会误中）
        t, tx, ty, dy = min(cand, key=lambda c: (c[3], abs(c[1] - px)))
        return [tx, t, round(dy, 1)]

    out = {}
    for nm in names:
        occ = [(x, y, l) for (n, x, y, l) in all_ins if n == nm]
        if not occ:
            continue
        ent_list = []
        for x, y, lay in occ:
            same = [(tx, t) for (t, tx, ty, _l) in all_txt if abs(ty - y) <= tol]
            right = sorted([(tx, t) for tx, t in same if tx >= x - tol], key=lambda p: p[0])
            left = sorted([(tx, t) for tx, t in same if tx < x - tol], key=lambda p: -p[0])
            ent_list.append({"at": [x, y], "layer": lay,
                             "right": [[tx, t] for tx, t in right],
                             "left": [[tx, t] for tx, t in left],
                             "nearest_right": _nearest(x, y, "r"),
                             "nearest_left": _nearest(x, y, "l")})
        out[nm] = ent_list
        print("%-10s INSERT %d 处" % (nm, len(ent_list)))
        for e in ent_list:
            if e["right"]:
                lab = e["right"][0][1]
                tags = " ".join("%s@%g" % (t, tx) for tx, t in e["right"][:4])
            else:
                nr = e["nearest_right"]
                lab = "（无同行右侧标签）"
                tags = ("最近右文字 %s@%g(Δy=%g)" % (nr[1], nr[0], nr[2])) if nr else "-"
            print("    at=(%8.1f,%8.1f)  行标签→ %-22s  同行右: %s"
                  % (e["at"][0], e["at"][1], lab, tags))
        if len({e["right"][0][1] for e in ent_list if e["right"]}) > 1:
            print("    ⚠️ 复用块：同行标签不止一种，别名表只能取默认值，其余含义须显式写符号名")
    if want_json:
        open(want_json, "w", encoding="utf-8").write(
            json.dumps(out, ensure_ascii=False, indent=2))
        print("\nJSON ->", want_json)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--top", type=int, default=6, help="每个块取最近 N 条文字")
    ap.add_argument("--min-count", type=int, default=1)
    ap.add_argument("--block", nargs="*", default=None, help="只查这些块名")
    ap.add_argument("--layer", default=None, help="只看该图层上的 INSERT")
    ap.add_argument("--rows", action="store_true",
                    help="严格同行配对模式：以 INSERT 点 y 与文字 y 同行关系唯一定名")
    ap.add_argument("--tol", type=float, default=6.0,
                    help="--rows 判同行的 y 容差（默认 6；图例行距约 10~15，容差过大会串行）")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    files = ([os.path.join(r, f) for r, _, fs in os.walk(a.target)
              for f in sorted(fs) if f.lower().endswith(".dxf")]
             if os.path.isdir(a.target) else [a.target])

    all_ins, all_txt, all_blk = [], [], []
    for p in files:
        try:
            b, ins, txt = parse(p)
        except Exception as e:
            print("ERR", p, e)
            continue
        all_blk += b
        all_ins += ins
        all_txt += txt
        print("解析 %-34s 块定义 %3d / INSERT %4d / TEXT %4d" % (os.path.basename(p), len(b), len(ins), len(txt)))

    if a.rows:
        names = a.block if a.block else sorted({n for n, _x, _y, _l in all_ins})
        rows_report(all_ins, all_txt, a.tol, names, a.json)
        return

    by_block = defaultdict(list)
    for nm, x, y, lay in all_ins:
        if a.layer and lay != a.layer:
            continue
        by_block[nm].append((x, y, lay))

    # 只对有 INSERT 的块报告；--block 时按给定顺序
    names = a.block if a.block else sorted(by_block, key=lambda k: -len(by_block[k]))
    print("\n%-12s %5s %-26s %s" % ("块名", "次数", "图层", "邻近文字 TOP%d" % a.top))
    print("-" * 116)
    out = {}
    for nm in names:
        occ = by_block.get(nm)
        if not occ:
            if a.block:
                print("%-12s %5d  %s" % (nm, 0, "（本图中未引用）"))
            continue
        if len(occ) < a.min_count:
            continue
        layers = Counter(l for _, _, l in occ)
        near = Counter()
        for x, y, _ in occ:
            for t, _, _, _ in sorted(all_txt, key=lambda t: (t[1] - x) ** 2 + (t[2] - y) ** 2)[:a.top]:
                if t and len(t) <= 30:
                    near[t] += 1
        top = near.most_common(a.top)
        out[nm] = {"count": len(occ),
                   "layers": dict(layers),
                   "nearby": [t for t, _ in top]}
        print("%-12s %5d %-26s %s" % (
            nm, len(occ),
            ",".join("%s(%d)" % (k, v) for k, v in layers.most_common(3)),
            " / ".join(t for t, _ in top)))

    if a.json:
        open(a.json, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
        print("\nJSON ->", a.json)


if __name__ == "__main__":
    main()
