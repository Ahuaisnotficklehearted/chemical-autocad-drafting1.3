# -*- coding: utf-8 -*-
"""**把多张 PFD 装订成一个 DWG** —— 照浙工大 PFD 实测的文件组织方式。

当前用户要求每工段独占一行，图框水平/垂直边缘净距均150mm。
A1为841×594mm，对应基准步距991/744mm；按实际图框边界整体平移。
--manifest入口采用工段清单；旧目录/单张入口保留原有接口。

本模块复现这个组织方式：
  ① 逐张规格 → 临时 DXF（走 `draw_pfd.py`，含图框/标题栏/家具，A1）；
  ② 按网格把每张**整体平移**到主图上的坑位（只平移、不缩放，字高线宽不动）；
  ③ 导入同一张主图的模型空间 → 存 DXF → **经 AutoCAD COM 转成唯一一个 DWG**。

⚠️ 交付格式仍是 DWG、通道仍是 COM（技能两条硬规定）；连不上 COM 就报错退出 3，
   不静默给 DXF——只不过这次出的 DXF 是"装订稿"的中间产物。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

#: 用户指定水平和垂直边缘净距均150mm；新工段清单按实际图框排布。
PITCH_X_DEFAULT = 991.0
PITCH_Y_DEFAULT = 744.0


def build_sheet_dxf(spec_path: str, out_dxf: str, *, python: str,
                    sheet: str = "A1", all_layers: bool = True,
                    timeout: int = 600) -> Dict[str, object]:
    """调 `draw_pfd.py` 出一张**临时 DXF**（含图框；不碰 COM，快且稳）。

    ⚠️ 图幅必须**钉死**（默认 A1），不能让每张按内容自动选——否则装订出来图框大小不一，
    就不成"网格"了。新工段装订入口采用用户指定A1（841×594）。
    图幅是**规格里的字段**（`draw_pfd` 没有 `--sheet` 开关），所以这里改写一份临时规格。
    """
    tmp_spec = os.path.splitext(out_dxf)[0] + ".spec.json"
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            sp = json.load(f)
        if isinstance(sp, dict) and sheet and str(sheet).lower() != "auto":
            sp["sheet"] = str(sheet)
            if isinstance(sp.get("layout"), dict):
                sp["layout"]["sheet"] = str(sheet)
        with open(tmp_spec, "w", encoding="utf-8") as f:
            json.dump(sp, f, ensure_ascii=False)
        use_spec = tmp_spec
    except Exception:                                     # noqa: BLE001
        use_spec = spec_path                              # 读不动就原样用
    cmd = [python, os.path.join(HERE, "draw_pfd.py"), use_spec,
           "--out", out_dxf, "--backend", "dxf", "--layout-force"]
    # **图层表要齐**（默认开）：浙工大 PFD 的层表是完整的一套，只写"用过的层"会缺层。
    if all_layers:
        cmd.append("--all-layers")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"ok": r.returncode == 0, "rc": r.returncode,
            "out": r.stdout, "err": r.stderr, "dxf": out_dxf}


def book_dxf(dxf_paths: Sequence[str], out_dxf: str, *,
             cols: int = 0, pitch_x: float = PITCH_X_DEFAULT,
             pitch_y: float = PITCH_Y_DEFAULT) -> Dict[str, object]:
    """把若干张 DXF 按网格平移后导入**同一张主图**。

    只做**平移**（不缩放）：字高 2~3mm、线宽档位、正交关系全部保持——这是"装订"能做的
    唯一不破坏图面属性的操作。每张的坑位按**自身外接框**对齐（不假设图框原点在 0,0）。
    """
    import ezdxf
    from ezdxf import bbox as ezbbox
    from ezdxf.addons.importer import Importer

    n = len(dxf_paths)
    if not n:
        return {"ok": False, "reason": "没有输入 DXF"}
    ncol = int(cols) if cols else max(1, int(n ** 0.5 + 0.999))
    nrow = (n + ncol - 1) // ncol

    # 独立物料框也占装订空间；先量全部实际外包络，避免盖住相邻流程图。
    sources = [ezdxf.readfile(p) for p in dxf_paths]
    extents = [ezbbox.extents(src.modelspace()) for src in sources]
    pitch_x = max(pitch_x, max(ext.extmax.x-ext.extmin.x for ext in extents) + 150.0)
    pitch_y = max(pitch_y, max(ext.extmax.y-ext.extmin.y for ext in extents) + 150.0)

    master = ezdxf.new("R2010", setup=True)
    # ⚠️ **层表要在主图里显式建全**：`Importer` 只把"有实体的层"搬过来，
    #    各张里没用到的层（02-标题栏文本 / 11-阀门与管件 / 21-标注 …）会被丢掉，
    #    装订稿就与浙工大 PFD 的完整层表不一致（实测 19 → 只剩 12）。先建齐再导入。
    try:
        import chemcad_layers as _LY
        for _s in _LY.STANDARD:
            if _s.name in master.layers:
                continue
            _kw = {"color": _s.color}
            if _s.lineweight >= 0:
                _kw["lineweight"] = _s.lineweight
            if _s.linetype in master.linetypes:
                _kw["linetype"] = _s.linetype
            master.layers.add(_s.name, **_kw)
    except Exception:                                        # noqa: BLE001
        pass
    msp = master.modelspace()
    placed: List[Dict[str, object]] = []
    for i, p in enumerate(dxf_paths):
        src = sources[i]
        smsp = src.modelspace()
        ext = extents[i]
        w, h = ext.extmax.x - ext.extmin.x, ext.extmax.y - ext.extmin.y
        col, row = i % ncol, i // ncol
        slot_x = col * pitch_x
        slot_y = (nrow - 1 - row) * pitch_y          # 第 0 行在最上
        dx = slot_x - ext.extmin.x
        dy = slot_y - ext.extmin.y
        for e in smsp:                               # 先平移到坑位（只平移）
            try:
                e.translate(dx, dy, 0.0)
            except Exception:                        # noqa: BLE001
                pass
        imp = Importer(src, master)
        imp.import_modelspace(msp)
        imp.finalize()
        placed.append({"dxf": os.path.basename(p), "col": col, "row": row,
                       "w": round(w, 1), "h": round(h, 1),
                       "slot": [round(slot_x, 1), round(slot_y, 1)]})
    master.saveas(out_dxf)
    return {"ok": True, "out": out_dxf, "sheets": n,
            "grid": [ncol, nrow], "pitch": [pitch_x, pitch_y], "placed": placed}


def main(argv: Optional[Sequence[str]] = None) -> int:
    # 新工段清单入口；旧单张/扁平列表保持兼容。
    args=list(argv) if argv is not None else sys.argv[1:]
    if '--manifest' in args:
        import chemcad_sections
        return chemcad_sections.main(args)
    ap = argparse.ArgumentParser(
        description="把多张 PFD 装订成一个 DWG（照浙工大 PFD 的 A1 图框网格）")
    ap.add_argument("specs", nargs="+", help="各张图的规格 JSON（或 1 个目录）")
    ap.add_argument("--out", required=True, help="输出 DWG（交付格式固定 DWG）")
    ap.add_argument("--sheet", default="A1",
                    help="每张图的图幅（默认 A1，**钉死**以保证图框大小统一；auto 则按内容各自选）")
    ap.add_argument("--cols", type=int, default=0, help="网格列数（默认按张数自动取，近似方形）")
    ap.add_argument("--pitch-x", type=float, default=PITCH_X_DEFAULT,
                    help=f"横向步距 mm（默认 {PITCH_X_DEFAULT:.0f} = A1 841 + 150；外置物料框按实际外包络扩展）")
    ap.add_argument("--pitch-y", type=float, default=PITCH_Y_DEFAULT,
                    help=f"纵向步距 mm（默认 {PITCH_Y_DEFAULT:.0f} = A1 594 + 150；旧扁平装订按整份内容扩展）")
    ap.add_argument("--no-all-layers", action="store_true",
                    help="只写**用过的**图层（默认写**完整层表**，与浙工大 PFD 的层表一致）")
    ap.add_argument("--create", action="store_true",
                    help="AutoCAD 没在跑时**替你启动一个**（COM 通道不变，只是省得手动开）")
    ap.add_argument("--keep-dxf", action="store_true", help="保留装订稿 DXF（默认删）")
    a = ap.parse_args(list(argv) if argv is not None else None)

    specs: List[str] = []
    for s in a.specs:
        if os.path.isdir(s):
            specs += [os.path.join(s, f) for f in sorted(os.listdir(s))
                      if f.lower().endswith(".json")]
        else:
            specs.append(s)
    if not specs:
        print("错误：没有找到规格文件", file=sys.stderr)
        return 2

    # ⚠️ **每次用带时间戳的独立目录，且不做批量删除**：
    #    ① 上一次的装订稿 DXF 可能被 AutoCAD COM 占着（`PermissionError`）；
    #    ② 直接 `rmtree` 旧目录会触发环境的"批量删除保护"（一次 50+ 文件被拦，
    #       实测整条装订直接跑不起来）。留着旧目录最省事，用完由用户自行清理。
    import time as _time
    work = os.path.join(os.path.dirname(os.path.abspath(a.out)),
                        "_book_tmp_" + _time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(work, exist_ok=True)
    python = sys.executable
    tmp: List[str] = []
    for i, sp in enumerate(specs, 1):
        d = os.path.join(work, f"{i:02d}.dxf")
        r = build_sheet_dxf(sp, d, python=python, sheet=a.sheet,
                            all_layers=not a.no_all_layers)
        if not r["ok"]:
            print(f"错误：第 {i} 张出图失败：{os.path.basename(sp)}", file=sys.stderr)
            print(str(r["err"])[-800:], file=sys.stderr)
            return 3
        tmp.append(d)
        print(f"  [{i}/{len(specs)}] {os.path.basename(sp)} → 临时 DXF")
    book_dxf_path = os.path.join(work, f"book_{os.getpid()}.dxf")
    rep = book_dxf(tmp, book_dxf_path, cols=a.cols,
                   pitch_x=a.pitch_x, pitch_y=a.pitch_y)
    if not rep.get("ok"):
        print(f"错误：装订失败：{rep.get('reason')}", file=sys.stderr)
        return 3
    print(f"装订：{rep['sheets']} 张 → {rep['grid'][0]}×{rep['grid'][1]} 网格，"
          f"步距 {rep['pitch'][0]:.0f}×{rep['pitch'][1]:.0f} mm")

    # 交付：DWG，且**必须**经 AutoCAD COM（技能硬规定；连不上就退 3，不静默给 DXF）
    import chemcad_dwg as DWG
    chk = DWG.require_com(create_if_not_exists=bool(a.create))   # ⚠️ 返回 dict，不抛异常
    if not chk.get("ok"):
        print("错误：要求输出 DWG，但 COM 通道不可用（本技能强制经 AutoCAD COM 出图）：",
              file=sys.stderr)
        print(f"  {chk.get('reason')}", file=sys.stderr)
        print(f"  （装订稿 DXF 已留在 {book_dxf_path}，可自行在 CAD 里另存 DWG）",
              file=sys.stderr)
        return 3
    r = DWG.dxf_to_dwg_com(book_dxf_path, a.out, create_if_not_exists=bool(a.create))
    # AutoCAD 忙会**瞬时**拒收 COM（实测报 `AttributeError: Open.Close`），隔一下就通——
    # 这属环境态不是代码错，故有限重试；仍失败才认输（不静默换成别的通道）。
    import time
    for _try in range(3):
        if r.get("ok"):
            break
        print(f"  · COM 转换失败（第 {_try + 1} 次）：{r.get('reason')}——稍后重试",
              file=sys.stderr)
        time.sleep(3.0)
        r = DWG.dxf_to_dwg_com(book_dxf_path, a.out, create_if_not_exists=bool(a.create))
    if not r.get("ok"):
        print(f"错误：COM 转换 DWG 失败：{r.get('reason') or r}", file=sys.stderr)
        return 3
    if not a.keep_dxf:
        # 只删**本轮自己写的**临时文件（逐个删，不整目录 rmtree——避免触发批量删除保护）
        for _f in tmp + [book_dxf_path]:
            try:
                os.remove(_f)
            except Exception:                             # noqa: BLE001
                pass
    print(f"已出图（**一个 DWG 含 {rep['sheets']} 个 A1 图框**）：{a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
