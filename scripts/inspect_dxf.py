#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剖析 DXF：图层表、块(符号)表、实体统计、文字样本。
用法:
    python inspect_dxf.py <file.dxf | dir> [--json out.json] [--texts 30]
说明:
    - 非 ASCII 在 DXF 中常以 \\U+XXXX 转义，本脚本自动还原。
    - 仅做轻量文本解析，不依赖 ezdxf，可在任意 Python 3 环境运行。
"""
import os, re, json, argparse
from chemcad_locate import read_text_robust


def unesc(s):
    if "\\U+" not in s:
        return s
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)


def parse(path):
    # 读文件一律走 chemcad_locate.read_text_robust（逐级降级 utf-8-sig → utf-8 → gbk → latin-1）。
    # 不用 `errors="replace"`：它会把 GBK 中文层名/文字静静换成 U+FFFD，
    # 于是"读到了但全是问号"而不报错 —— 静默失真比报错难查。
    text, _enc = read_text_robust(path)
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]

    section = None
    layers, blocks = [], []
    insert = {}
    ents = {}
    texts = []

    def read_block_head(i):
        """从 code=0/BLOCK|LAYER|INSERT 之后读 name(code=2)"""
        j = i + 2
        nm = None
        while j < len(lines) - 1:
            c = lines[j].strip()
            if c == "2" and nm is None:
                nm = unesc(lines[j + 1])
            if c == "0":
                break
            j += 2
        return nm, j

    i, n = 0, len(lines)
    while i < n - 1:
        code = lines[i].strip()
        val = lines[i + 1]
        if code == "0":
            if val == "SECTION":
                section = lines[i + 3] if i + 3 < n else ""
                i += 4
                continue
            if val == "ENDSEC":
                section = None
                i += 2
                continue
            if val == "LAYER":
                nm, j = read_block_head(i)
                if nm:
                    layers.append(nm)
                i = j
                continue
            if val == "BLOCK":
                nm, j = read_block_head(i)
                if nm and not nm.startswith("*"):
                    blocks.append(nm)
                i = j
                continue
            if val == "INSERT":
                nm, j = read_block_head(i)
                if nm:
                    insert[nm] = insert.get(nm, 0) + 1
                ents["INSERT"] = ents.get("INSERT", 0) + 1
                i = j
                continue
            if val in ("TEXT", "MTEXT"):
                j = i + 2
                s = None
                while j < len(lines) - 1:
                    c = lines[j].strip()
                    if c in ("1", "3") and s is None:
                        s = unesc(lines[j + 1])
                    if c == "0":
                        break
                    j += 2
                if s:
                    texts.append(s)
                ents[val] = ents.get(val, 0) + 1
                i = j
                continue
            if section == "ENTITIES":
                ents[val] = ents.get(val, 0) + 1
        i += 2

    return {
        "file": os.path.basename(path),
        "size": os.path.getsize(path),
        "layers": sorted(set(layers)),
        "blocks": sorted(set(blocks)),
        "insert_top": sorted(insert.items(), key=lambda x: -x[1])[:15],
        "entities": sorted(ents.items(), key=lambda x: -x[1]),
        "text_count": len(texts),
        "texts_head": texts[:40],
    }


def main():
    ap = argparse.ArgumentParser(description="剖析 DWG / DXF 的图层、块、实体、文字")
    ap.add_argument("target", help="DWG / DXF 文件或目录（.dwg 自动先转 DXF）")
    ap.add_argument("--json", default=None, help="结果写入 JSON")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    import chemcad_dwg as DWG

    files = []
    if os.path.isdir(a.target):
        for r, _, fs in os.walk(a.target):
            files += [os.path.join(r, f) for f in sorted(fs)
                      if f.lower().endswith((".dxf", ".dwg"))]
    else:
        files = [a.target]

    out = {}
    for p in files:
        try:
            with DWG.reading(p) as src:
                r = parse(src)
            r["file"] = os.path.basename(p)          # 报告写回**原始**文件名
        except Exception as e:
            print("ERR", p, e)
            continue
        out[r["file"]] = r
        if not a.quiet:
            print("=" * 66)
            print("%s  (%.1f KB)" % (r["file"], r["size"] / 1024))
            print("  图层(%d): %s" % (len(r["layers"]), r["layers"]))
            print("  块定义(%d): %s" % (len(r["blocks"]), r["blocks"][:30]))
            print("  块引用TOP: %s" % r["insert_top"][:10])
            print("  实体: %s" % r["entities"][:12])
            print("  文字(%d)样例: %s" % (r["text_count"], r["texts_head"][:12]))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("\nJSON ->", a.json)


if __name__ == "__main__":
    main()
