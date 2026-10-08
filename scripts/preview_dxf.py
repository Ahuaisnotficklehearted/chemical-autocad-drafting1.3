#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview_dxf.py —— 把 DXF 渲染成 PNG/SVG，用于**出图后先看一眼**。

自动化校核能查出「斜线、线宽超档、回路不闭合」，但查不出「文字压线、符号撞在一起、
管路穿过设备」这类版面问题——那必须靠眼睛。本脚本就是那个「眼睛」。

依赖（可选，仅本脚本需要）:
    pip install ezdxf matplotlib

用法:
    python preview_dxf.py <file.dxf|目录> [--out out.png] [--dpi 140]
    python preview_dxf.py pfd.dxf --out pfd.png --width 16.54
    python preview_dxf.py pfd.dxf --svg          # 出矢量图
    python preview_dxf.py pfd.dxf --font "Microsoft YaHei"

提示:
    * 中文字体默认尝试 Microsoft YaHei / SimHei / Noto Sans CJK；缺字体时中文会显示为方框，
      但**版面关系（位置、重叠、穿越）仍可判读**——预览的主要目的正是看版面。
    * A1 幅面为 841x594 mm，故默认按 1:1 英寸换算（33.11 x 23.39 in）等比缩放。
"""

from __future__ import annotations

import argparse
import os
import sys

CJK_FONTS = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC",
             "Source Han Sans SC", "WenQuanYi Zen Hei", "Arial Unicode MS"]


def pick_font(preferred: str = "") -> str:
    """挑一个系统里存在的中文字体。"""
    import matplotlib.font_manager as fm
    have = {f.name for f in fm.fontManager.ttflist}
    if preferred and preferred in have:
        return preferred
    for name in CJK_FONTS:
        if name in have:
            return name
    return ""


def render(dxf_path: str, out_path: str, dpi: int = 140, width_in: float = 33.11,
           font: str = "", bg: str = "white") -> str:
    import matplotlib
    matplotlib.use("Agg")
    import ezdxf
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib.pyplot as plt

    fam = pick_font(font)
    if fam:
        matplotlib.rcParams["font.sans-serif"] = [fam] + matplotlib.rcParams["font.sans-serif"]
        matplotlib.rcParams["axes.unicode_minus"] = False

    doc = ezdxf.readfile(dxf_path)
    # 让预览能显示中文：把文字样式指向系统中文字体。
    # **只改内存中的文档，不写回文件**——交付的 DXF 仍保持原样式。
    if fam:
        try:
            import matplotlib.font_manager as _fm
            ttf = os.path.basename(_fm.findfont(_fm.FontProperties(family=fam),
                                                fallback_to_default=False))
            for st in doc.styles:
                try:
                    st.dxf.font = ttf
                except Exception:
                    pass  # 该文字样式无法改字体 → 用默认字体预览
        except Exception:
            pass  # 取中文字体失败 → 沿用原样式预览（只改内存，不写回文件）
    msp = doc.modelspace()
    from ezdxf import bbox as _bbox
    ext = _bbox.extents(msp)
    if ext.has_data:
        xmin, ymin = float(ext.extmin[0]), float(ext.extmin[1])
        xmax, ymax = float(ext.extmax[0]), float(ext.extmax[1])
    else:
        xmin, ymin, xmax, ymax = 0.0, 0.0, 841.0, 594.0
    w = max(1e-6, xmax - xmin)
    h = max(1e-6, ymax - ymin)
    fig = plt.figure(figsize=(width_in, width_in * h / w))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(bg)
    ctx = RenderContext(doc)
    Frontend(ctx, MatplotlibBackend(ax)).draw_layout(msp, finalize=True)
    fig.savefig(out_path, dpi=dpi, facecolor=bg)
    plt.close(fig)
    return fam


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 DWG / DXF 渲染成图片以便目视检查版面")
    ap.add_argument("target", help="DWG / DXF 文件或目录（.dwg 自动先转 DXF）")
    ap.add_argument("--out", default=None, help="输出图片路径（默认同名 .png）")
    ap.add_argument("--dpi", type=int, default=140)
    ap.add_argument("--width", type=float, default=33.11, help="图幅宽度（英寸），A1 按 1:1 为 33.11")
    ap.add_argument("--font", default="", help="指定中文字体名")
    ap.add_argument("--svg", action="store_true", help="输出 SVG 矢量图")
    a = ap.parse_args(argv)

    import chemcad_dwg as DWG

    files = ([os.path.join(r, f) for r, _, fs in os.walk(a.target)
              for f in sorted(fs) if f.lower().endswith((".dxf", ".dwg"))]
             if os.path.isdir(a.target) else [a.target])

    rc = 0
    for p in files:
        ext = ".svg" if a.svg else ".png"
        out = a.out or os.path.splitext(p)[0] + ext
        if len(files) > 1 and a.out:
            out = os.path.splitext(a.out)[0] + "_" + os.path.splitext(os.path.basename(p))[0] + ext
        try:
            with DWG.reading(p) as src:
                fam = render(src, out, dpi=a.dpi, width_in=a.width, font=a.font)
        except ImportError as e:
            print("需要额外依赖：pip install ezdxf matplotlib", file=sys.stderr)
            print(f"  ({e})", file=sys.stderr)
            return 2
        print(f"{os.path.basename(p)} → {out}"
              + (f"   [中文字体: {fam or '未找到，中文可能显示为方框'}]"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
