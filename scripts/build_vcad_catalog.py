#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 VCAD 图块库目录 -- 扫 assets/vcad-block-library/ 输出 catalog.json

用法:
    python scripts/build_vcad_catalog.py            # 重建 catalog.json
    python scripts/build_vcad_catalog.py --stats    # 只打印统计

catalog.json 是图块库的**机器可读唯一事实来源**：
    version / source / license / generated / totals / categories[] / blocks[]

设计约束（与 chemcad_catalog.py 一致）：
  * 零依赖（只用标准库），任何环境下都能重跑；
  * 分类语义（中文名 / 说明 / 关联标准）写在 CATEGORY_META 里，**不靠猜**；
  * 标准号从**文件名**解析（VCAD 命名高度规范化），解析不出的留空，不编造。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB = os.path.join(ROOT, "assets", "vcad-block-library")
OUT = os.path.join(LIB, "catalog.json")

# ---------------------------------------------------------------- 分类语义表
# key = 目录名；值 = (中文名, 说明, 关联标准)
# 说明里的"用途类"判断依据：目录名 + 抽样文件名 + VCAD.mnu 菜单。
CATEGORY_META = {
    "151":         ("管壳式换热器零部件", "GB/T 151 系列：换热管、管板、折流板等部件详图", "GB/T 151"),
    "BEM":         ("固定管板式换热器", "TEMA 型式 BEM：固定管板换热器总图与部件", "TEMA / GB/T 151"),
    "BES":         ("浮头式换热器", "TEMA 型式 BES：浮头式换热器总图与部件", "TEMA / GB/T 151"),
    "BEU":         ("U 形管式换热器", "TEMA 型式 BEU：U 形管换热器总图与部件", "TEMA / GB/T 151"),
    "BKU":         ("釜式重沸器", "TEMA 型式 BKU：釜式重沸器，含技术特性表", "TEMA / GB/T 151"),
    "bdfl":        ("设备法兰（NB/T 47021~47023）", "甲型/乙型/长颈法兰，含衬里版（clad）与螺栓孔（hole）派生", "NB/T 47021~47023"),
    "company":     ("企业图框与容器模板", "企业级图框、容器/球罐模板（含 sphere_mix 等组合块）", "企业标准"),
    "davit":       ("吊柱", "HG/T 21639 系列吊柱（旋臂式）", "HG/T 21639"),
    "dcone":       ("双锥环密封", "高压容器双锥环密封结构", "—"),
    "detail":      ("节点详图", "通用节点大样（含容器划类 cate2016 系列）", "HG/T 20583 等"),
    "expan":       ("膨胀节", "膨胀节（波形补偿器）详图", "GB/T 16749"),
    "eyebolt":     ("吊环螺钉", "吊环螺钉 TPA/TPB 型", "GB/T 825"),
    "fgnozzle":    ("法兰管口", "带法兰接管（管口）详图与补强", "HG/T 20583"),
    "fldetail":    ("法兰连接详图", "法兰连接节点，含 ASME 系列（ASME_BL_FF/FM/G）", "HG/T 20592 / ASME B16.5"),
    "gbhead":      ("封头", "GB/T 25198 封头：椭圆 EHA、碟形 THA、锥形 CHA、球冠等", "GB/T 25198"),
    "grating":     ("格栅与平台板", "钢格栅、踏步板", "—"),
    "heatex":      ("换热器通用部件", "折流板/拉杆（baffle_tierod）、旁路密封（sldway_*）等", "GB/T 151"),
    "hgtgasket":   ("垫片", "HG/T 20606 非金属垫片，含 ASME B16.20 系列", "HG/T 20606 / ASME B16.20"),
    "hor":         ("卧式容器技术特性表", "卧式容器 GB 技术特性表模板", "—"),
    "innerladder": ("内梯", "容器内梯（直梯/斜梯）详图", "HG/T 21514"),
    "jacket":      ("夹套", "夹套筒体与夹套接管详图", "HG/T 20583"),
    "leg":         ("耳式支座", "耳式支座 A/B 型（ear_A1~5 等），按号选型", "JB/T 4712.3"),
    "lug":         ("吊耳", "容器吊耳详图（hplug 系列）", "HG/T 21574"),
    "lug2018":     ("吊耳（2018 版）", "HG/T 21574-2018 版吊耳详图", "HG/T 21574-2018"),
    "manhole":     ("人孔", "HG/T 21515~21533 系列人孔：FF/RF/MFM 密封面、常压/耐压、按 DN 与材料分文件", "HG/T 21515~21533"),
    "manholeclad": ("衬里人孔", "HG/T 21596 系列衬里人孔（plrf 等），按衬里层数/DN/密封面分文件", "HG/T 21596"),
    "mhasme":      ("ASME 人孔", "ASME 制人孔（mhaa 系列）", "ASME"),
    "nelson":      ("Nelson 型接管", "Nelson 型（高颈）接管结构", "—"),
    "nozzle":      ("管口与补强", "接管补强圈、筋板（rib/stiff）详图", "HG/T 20583"),
    "other":       ("螺栓螺母", "标准紧固件外形块（M12/M16/M20 双头/双螺母）", "GB/T 5782 等"),
    "sphere":      ("球罐", "球罐支柱（sphere_spt）等", "GB/T 12337"),
    "standard":    ("标准图框与视图", "各图幅横竖视图框（$$$vcadA1H/V 等）、视口与访问符号", "—"),
    "tk1":         ("图框图库 tk1", "VCAD 图框图库（tk1 命令）：各图幅/标题栏/图签组合", "—"),
    "tk2":         ("节点图库 tk2", "VCAD 节点图库（tk2 命令）：通用节点大样集合", "—"),
    "tk3":         ("参考资料 tk3", "VCAD 参考资料库（tk3 命令）", "—"),
    "tk4":         ("计算表格 tk4", "VCAD 计算表格库（tk4 命令）", "—"),
    "tower":       ("塔器", "NB/T 47041 塔式容器：塔盘、加强圈（strake）等", "NB/T 47041"),
    "weld":        ("焊接节点", "焊接节点详图库：HG/T 20583 焊缝坡口形式（不开坡口 / 单面 V 坡口…）"
                                "与 GB/T 150.3 附录 D 焊接结构", "HG/T 20583 · GB/T 150.3"),
    "yt":          ("哑图模板", "VCAD 哑图生成器（yt 命令）模板", "—"),
    "yt1":         ("哑图模板 2", "哑图模板第二组", "—"),
    "PDF":         ("（空）", "原为空目录", "—"),
    "boltnut":     ("（空）", "原为空目录", "—"),
    "excel":       ("（数据）", "Excel 模板挂载点，无图块", "—"),
    "fixheat":     ("（数据）", "固定换热器数据挂载点，无图块", "—"),
    "plld":        ("（数据）", "布置数据挂载点，无图块", "—"),
    "设置":        ("配置块", "图框/明细表初始化用块（gk_mxb_* 等）", "—"),
    "zztk":        ("增长图块", "VCAD 增长图块（zztk 命令）模板，随明细表行数伸缩", "—"),
    "(根目录)":    ("根目录散装块", "未归入子库的散装块：U 形管详图（utube_*）、管口"
                                    "（gkh_nozzle / npt3-4）、法兰管口详图（fgpipe_detail）、"
                                    "标准件（vcad_bgbz / vcad_KUAI）", "—"),
}

# ------------------------------------------------------- 文件名 → 标准号解析
# VCAD 命名高度规范化：标准号写在文件名里，只是写法五花八门。
STD_PATTERNS = [
    # GB/T 150.x《压力容器》分部：命名形如 gbt1503appd-d01a = 150.3 附录 D 图 D-01a。
    # ⚠ 必须排在通用 GB 规则**之前**，否则会被读成不存在的「GB/T 1503」（已实测踩到）。
    (re.compile(r"^gb\s*/?\s*t?\s*150[._-]?(\d)", re.I), lambda m: f"GB/T 150.{m.group(1)}"),
    (re.compile(r"^hg\s*t?\s*(\d{5})", re.I),    lambda m: f"HG/T {m.group(1)}"),
    (re.compile(r"^nb\s*t?\s*(\d{5})", re.I),    lambda m: f"NB/T {m.group(1)}"),
    (re.compile(r"^head\s*(\d{5})", re.I),       lambda m: f"GB/T {m.group(1)}"),
    (re.compile(r"^asme", re.I),                 lambda m: "ASME"),
    (re.compile(r"^gb\s*/?\s*t?\s*(\d{4,5})", re.I), lambda m: f"GB/T {m.group(1)}"),
    (re.compile(r"^jb\s*/?\s*t?\s*(\d+)", re.I), lambda m: f"JB/T {m.group(1)}"),
    (re.compile(r"^mh(aa|a)", re.I),             lambda m: "ASME"),
]


def parse_standard(basename: str) -> str:
    """从文件名解析标准号；解析不出返回空串（不编造）。"""
    stem = os.path.splitext(basename)[0]
    for rx, fn in STD_PATTERNS:
        m = rx.match(stem)
        if m:
            try:
                return fn(m)
            except Exception:
                return ""
    return ""


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def scan():
    if not os.path.isdir(LIB):
        print(f"[ERROR] 图块库目录不存在: {LIB}", file=sys.stderr)
        sys.exit(2)

    cats: dict[str, dict] = {}
    blocks: list[dict] = []

    for dirpath, _dirnames, filenames in os.walk(LIB):
        rel_dir = os.path.relpath(dirpath, LIB)
        if rel_dir == ".":
            cat = "(根目录)"
        else:
            cat = rel_dir.replace("\\", "/").split("/")[0]

        for fn in filenames:
            if fn == "catalog.json":
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue  # 样本文件不可读（被删/无权限）→ 跳过该文件，不中断编目
            ext = os.path.splitext(fn)[1].lower().lstrip(".")
            blocks.append({
                "name": fn,
                "ext": ext,
                "size": size,
                "size_h": human_size(size),
                "category": cat,
                "subpath": os.path.relpath(full, LIB).replace("\\", "/"),
                "standard": parse_standard(fn),
            })

    # 汇总分类
    for b in blocks:
        c = cats.setdefault(b["category"], {"dwg": 0, "sld": 0, "other": 0, "bytes": 0})
        if b["ext"] == "dwg":
            c["dwg"] += 1
        elif b["ext"] == "sld":
            c["sld"] += 1
        else:
            c["other"] += 1
        c["bytes"] += b["size"]

    categories = []
    for name in sorted(cats):
        c = cats[name]
        zh, note, std = CATEGORY_META.get(name, ("（未标注）", "", ""))
        categories.append({
            "dir": name, "zh": zh, "note": note, "standard": std,
            "dwg": c["dwg"], "sld": c["sld"], "other": c["other"],
            "bytes": c["bytes"], "size_h": human_size(c["bytes"]),
        })

    dwg = sum(1 for b in blocks if b["ext"] == "dwg")
    total = sum(b["size"] for b in blocks)

    # 未在 CATEGORY_META 里登记的分类 —— 显式报出来，不静默
    unlabeled = [c["dir"] for c in categories if c["zh"] == "（未标注）"]

    return {
        "version": "1.0",
        "source": "VCAD2022 原版程序（国产压力容器/换热器设计插件）",
        "source_path_original": r"D:\QQ\VCAD2022原版程序\block",
        "copyright": "VCAD2022 版权归原作者（yangwting / 公众号 VCAD001）所有；"
                     "本目录仅作**本地参考图块库**收录，**不得对外分发或商用**。",
        "generated": str(date.today()),
        "totals": {
            "files": len(blocks), "dwg": dwg,
            "categories": len(categories),
            "bytes": total, "size_h": human_size(total),
            "unlabeled_categories": unlabeled,
        },
        "categories": categories,
        "blocks": sorted(blocks, key=lambda b: (b["category"], b["name"])),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 VCAD 图块库目录")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args()

    cat = scan()
    t = cat["totals"]

    if args.stats:
        print(f"图块库: {LIB}")
        print(f"  {t['files']} 文件 / {t['dwg']} DWG / {t['categories']} 分类 / {t['size_h']}")
        if t["unlabeled_categories"]:
            print(f"  ⚠ 未登记分类: {t['unlabeled_categories']}")
        for c in cat["categories"]:
            if c["dwg"]:
                print(f"    {c['dir']:<14} {c['dwg']:>4} dwg  {c['size_h']:>9}  {c['zh']}")
        return 0

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
    print(f"[OK] {os.path.relpath(OUT, ROOT)}")
    print(f"     {t['files']} 文件 / {t['dwg']} DWG / {t['categories']} 分类 / {t['size_h']}")
    if t["unlabeled_categories"]:
        print(f"     ⚠ 未登记分类（已标「未标注」）: {t['unlabeled_categories']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
