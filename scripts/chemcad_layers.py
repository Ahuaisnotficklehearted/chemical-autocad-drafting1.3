#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""用户指定浙江大学 PFD-2004版.dwg 的默认图层。

assets/layer-standard.json 是唯一配置来源：32 个原始图层、ACI 颜色、
线型、原始线宽码、打印状态与内部语义映射。来源路径和 SHA-256 均在配置内。
负线宽 -3 表示继承 AutoCAD 应用的默认线宽，不是负的物理线宽。
标准写出完整图层表；原图的辅助层保留，设备和管线只映射到对应用途层。
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple

Pt = Tuple[float, float]


@dataclass(frozen=True)
class StdLayer:
    """标准里的一层：名字、ACI 颜色、线型、线宽（0.01mm，DXF code 370）。"""

    name: str
    color: int
    linetype: str = "Continuous"
    lineweight: int = 25           # 0.25mm
    plot: bool = True
    flags: int = 0

    @property
    def mm(self):
        return self.lineweight / 100.0 if self.lineweight >= 0 else None


PROFILE_PATH = Path(__file__).resolve().parents[1] / "assets" / "layer-standard.json"
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
STANDARD: Tuple[StdLayer, ...] = tuple(
    StdLayer(**{k: row[k] for k in ("name", "color", "linetype", "lineweight", "plot", "flags")})
    for row in PROFILE["layers"])
STD_BY_NAME: Dict[str, StdLayer] = {s.name: s for s in STANDARD}
EMIT_MAP: Dict[str, str] = dict(PROFILE["emit_map"])
assert len(STD_BY_NAME) == len(STANDARD), "默认图层名称重复"
assert set(EMIT_MAP.values()) <= set(STD_BY_NAME), "语义映射指向默认表外的图层"


def std_name(sem: str) -> str:
    """内部语义层名 → 标准层名（未登记的原样返回，便于暴露"漏登记"）。"""
    return EMIT_MAP.get(sem, sem)


NEED_LINETYPES = tuple(
    (row["name"], row["description"], row["pattern"]) for row in PROFILE["linetypes"])


def ensure_linetypes(doc) -> List[str]:
    """按母图的实际线型定义设置新建输出文件，返回已设置的线型名。"""
    added = []
    for name, description, pattern in NEED_LINETYPES:
        if name in doc.linetypes:
            doc.linetypes.remove(name)
        doc.linetypes.add(name, pattern=pattern, description=description)
        added.append(name)
    return added


def layer_definition(name):
    """输出层的属性优先取母图；内部语义层取语义注册表。"""
    from chemcad_ir import LAYER_REGISTRY, resolve_layer
    return STD_BY_NAME.get(str(name)) or LAYER_REGISTRY.get(resolve_layer(str(name)))


def output_layer_names(d):
    """启用默认图层时写全表，否则仅输出所用语义层。"""
    return list(STD_BY_NAME) if d.meta.get("layer_standard") else sorted(d.used_layers())


def apply_dxf_layer_settings(doc) -> Dict[str, object]:
    """导入块后重新写全母图线宽，包含预建的0和Defpoints层。"""
    for layer in STANDARD:
        if layer.name not in doc.layers:
            doc.layers.new(layer.name, dxfattribs={
                'color': layer.color, 'linetype': layer.linetype,
                'lineweight': layer.lineweight, 'plot': int(layer.plot), 'flags': layer.flags})
        else:
            doc.layers.get(layer.name).dxf.lineweight = layer.lineweight
    settings = PROFILE['drawing_settings']
    doc.header['$LWDISPLAY'] = int(settings['LWDISPLAY'])
    doc.header['$CELWEIGHT'] = int(settings['CELWEIGHT'])
    return {'layers': len(STANDARD), 'lineweight_display': bool(settings['LWDISPLAY'])}


def unmapped(d) -> List[str]:
    """列出**用了但没有登记映射**的语义层 —— 换标准时最怕静默漏掉几个层。

    ⚠️ 必须排除**已经是标准层名**的图层：`apply_standard()` 之后就全是标准名了，
    若不过滤，它会把"已换好的标准层"当成"漏登记的语义层"报出来（自相矛盾的假警报）。
    所以判据要么在换名**之前**看，要么认得出标准名——这里选后者（更抗误用顺序）。
    """
    known_std = set(STD_BY_NAME) | {"0", "Defpoints"}
    out = []
    for sem in d.used_layers():
        s = str(sem)
        if s in known_std:
            continue
        if s not in EMIT_MAP:
            out.append(s)
    return sorted(set(out))


def apply_standard(d) -> Dict[str, object]:
    """把图纸里所有实体的图层名换成标准层名（出图前调用）。

    ⚠️ 只改**图层归属**，不动几何——所以"换了图层标准"不会改变图形，
    也不会影响任何几何校验（正交性/穿设备/压线都只看几何）。
    同时把标准层的颜色/线型/线宽写进 `d.meta`，供写出器建表时取用。
    """
    from chemcad_ir import Line, Polyline, Text
    from chemcad_ir import resolve_layer
    changed: Dict[str, int] = {}
    for e in d.iter_ents():
        old = str(e.layer)
        # 标准层可能合并本体与接管；保留原几何语义供出图前后的量尺一致使用。
        if not hasattr(e, '_semantic_layer'):
            e._semantic_layer = resolve_layer(old)
        new = std_name(old)
        # 家具在内部都用 BORDER；写出时按母图分开标题栏边框、分隔与文字。
        if old == "BORDER":
            if isinstance(e, Text):
                new = "标题栏文字"
            elif isinstance(e, Line):
                new = "标题栏分隔"
            elif isinstance(e, Polyline) and e.pts:
                xs, ys = [p[0] for p in e.pts], [p[1] for p in e.pts]
                if abs(max(xs) - min(xs) - 180.0) < 1e-6 and abs(max(ys) - min(ys) - 60.0) < 1e-6:
                    new = "标题栏边框"
        elif old == "TABLE" and isinstance(e, Text):
            new = "1-文本"
        if new != old:
            changed[f"{old} -> {new}"] = changed.get(f"{old} -> {new}", 0) + 1
            try:
                e.layer = new            # 实体层（IR 里是可写字段）
            except Exception:
                pass  # 该 IR 实体不支持改层 → 保持原层名，不中断整批映射
    # 组的 meta 里若记了 layer 也要跟着换
    for g in d.iter_groups():
        try:
            lay = g.meta.get("layer")
            if lay:
                g.meta["layer"] = std_name(str(lay))
        except Exception:
            pass  # 该分组无 layer 元数据 → 跳过（无可映射项）
    d.meta.setdefault("layer_standard", {})
    d.meta["layer_standard"] = {
        "source": PROFILE["source"],
        "source_sha256": PROFILE["source_sha256"],
        "layers": len(STANDARD),
        "renamed": changed,
    }
    return d.meta["layer_standard"]


def check_standard(d) -> List[Dict[str, str]]:
    """检查 IR 的图层名；颜色、线型、线宽与打印状态在输出回读时对账。"""
    out: List[Dict[str, str]] = []
    for raw in d.used_layers():
        sem = str(raw)
        if sem in ("0", "Defpoints"):
            continue
        std = STD_BY_NAME.get(sem)
        if std is None:
            out.append({"level": "error", "rule": "图层不在标准表内",
                        "msg": f"{sem} 不在出图图层标准里（标准共 {len(STANDARD)} 层）；"
                               f"要么改用标准层，要么在 EMIT_MAP 里登记映射"})
    return out


def to_dict() -> Dict[str, object]:
    """标准表 → 可序列化 dict（供文档/外部工具消费）。"""
    return json.loads(json.dumps(PROFILE, ensure_ascii=False))


def check_maps_consistent() -> List[str]:
    """核对 `EMIT_MAP`（语义→标准）与 `chemcad_ir.STD_ALIAS`（标准→语义）互为逆映射。

    这两张表分居两个模块，改一处忘另一处**不会报错**，只会让"按标准出图后
    自己的校验全部认不出图层"——症状离病因很远。所以直接写成一条自检。
    """
    import chemcad_ir as IR
    bad: List[str] = []
    for sem, std in EMIT_MAP.items():
        if std in ("0", "Defpoints"):
            continue                       # 归到 DXF 默认层，标准表里本就没有
        back = IR.STD_ALIAS.get(std)
        if back is None:
            bad.append(f"EMIT_MAP[{sem}]={std}，但 STD_ALIAS 里没有 {std}"
                       "（出图后自己的校验认不出这层）")
            continue
        # ⚠️ **多对一是合法的**：INST 与 仪表 都归标准层 `15-仪表`，
        # 反查只能挑一个代表（上面挑的是 INST）。所以这里不比"名字相等"，
        # 只比**线宽档位**——只要档位一致，图面实际效果就没有差别。
        a = IR.LAYER_REGISTRY.get(IR.resolve_layer(sem))
        b = IR.LAYER_REGISTRY.get(IR.resolve_layer(back))
        if a and b and a.weight != b.weight:
            bad.append(f"{sem}({a.weight}) → {std} → {back}({b.weight})："
                       "线宽档位不一致，图面会变")
    for std, sem in IR.STD_ALIAS.items():
        if sem not in EMIT_MAP:
            bad.append(f"STD_ALIAS[{std}]={sem}，该语义层没在 EMIT_MAP 里"
                       "（正查不出去，等于白登记）")
    return bad


if __name__ == "__main__":
    print(f"出图图层标准：{len(STANDARD)} 层（来源：浙江大学 PFD-2004版.dwg）")
    print(f"  {'图层名':<26} {'色':>4} {'线型':<12} {'mm':>5}")
    for s in STANDARD:
        width = f"{s.mm:.2f}" if s.mm is not None else "默认"
        print(f"  {s.name:<26} {s.color:>4} {s.linetype:<12} {width:>5}")
    print(f"\n内部语义层映射：{len(EMIT_MAP)} 条")
    for k, v in EMIT_MAP.items():
        if k in ("0", "Defpoints", "_Grid", "DEFPOINTS"):
            continue
        print(f"  {k:<14} -> {v}")
    problems = check_maps_consistent()
    print(f"\n两侧映射一致性：{'✓ 互为逆映射' if not problems else '✗ ' + str(len(problems)) + ' 处'}")
    for p in problems[:8]:
        print("   ", p)
