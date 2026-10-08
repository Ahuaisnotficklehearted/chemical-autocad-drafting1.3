"""PFD 设备只能取自具名图库块；缺块时等待用户确认，不生成替代形状。"""
from __future__ import annotations

import os
from functools import lru_cache

import chemcad_catalog as C
from chemcad_ir import resolve_layer


class BlockConfirmationRequired(ValueError):
    """需要用户明确选择图块；调用者应报告清单并停止输出。"""


# 兼容旧规格的明确名称。宽泛的 vessel/filter/box 等不在此表中。
ALIASES = {
    "compressor": "compressor.unspecified",
    "centrifuge": "solids.centrifuge", "crystallizer": "solids.crystallizer",
    "cyclone": "separator.cyclone",
}

LAYER_ALIASES = {"设备":"EQUIP", "设备内件":"EQUIP_IN", "内件":"EQUIP_IN",
                 "管口":"NOZZLE", "中心线":"CENTERLINE", "剖面线":"HATCH", "位号":"TAG"}

# 用户圈定的圆形换热器：只接左右两个水平接管，斜箭头不是物流接点。
CIRCULAR_HX_BLOCKS = ('hx.cooling', 'hx.heating')
CIRCULAR_HX_PROCESS_PORTS = ('left', 'right', 'inlet', 'outlet', 'feed', 'side',
                             'tube_in', 'tube_out')


def geometry_layer(name):
    return LAYER_ALIASES.get(name, resolve_layer(name))


def selection(eq):
    """设备描述 -> (文件, 块名)。外部块必须由用户给出/确认文件和块名。"""
    if eq.get("block_file") or eq.get("block_name"):
        path, name = str(eq.get("block_file", "")), str(eq.get("block_name", ""))
        if not path or not name or not os.path.isabs(path):
            raise BlockConfirmationRequired("外部图块需用户确认绝对路径 block_file 和具名 block_name")
        return path, name
    kind = str(eq.get("block") or eq.get("type") or "").strip()
    sym = C.find(kind) or C.find(ALIASES.get(kind.lower(), ""))
    if not sym:
        import chemcad_assets as A
        asset=A.find(kind)
        if asset and asset.get('category')=='equipment' and asset.get('pfd_eligible'):
            return A.source(asset,pfd=True)
        raise BlockConfirmationRequired(f"设备类型 {kind!r} 无明确对应图库块，请用户指定图块")
    if sym.get("status") == "pending_block":
        raise BlockConfirmationRequired(
            f"设备 {sym['label']}（{sym['id']}）尚无已接入的具名块；请确认新版图块及连接点")
    return C.block_source(sym)


@lru_cache(maxsize=64)
def _read_block(path, name, stamp):
    from ezdxf import bbox
    doc = _read_document(path, stamp)
    if name not in doc.blocks:
        raise BlockConfirmationRequired(f"图库 {path} 没有具名块 {name!r}；请用户确认图块")
    block = doc.blocks[name]
    if not len(block):
        raise BlockConfirmationRequired(f"图库块 {name!r} 为空；请用户确认图块")
    bounds = bbox.extents(block)
    if not bounds.has_data:
        raise BlockConfirmationRequired(f"无法识别块 {name!r} 的边界；请用户确认图块")
    return block, bounds


@lru_cache(maxsize=16)
def _read_document(path, stamp):
    # ponytail: 同一分类文件只解析一次；修改时间参与缓存键，文件更新后重新读取。
    import ezdxf
    return ezdxf.readfile(path)


def source(eq):
    params = eq.get("params") or {}
    if params:
        raise BlockConfirmationRequired(
            f"参数 {sorted(params)} 会重新塑造设备；请用户确认相应的图库块，使用 block 指定")
    path, name = selection(eq)
    try:
        stamp = os.stat(path).st_mtime_ns
        block, bounds = _read_block(path, name, stamp)
    except BlockConfirmationRequired:
        raise
    except ImportError as exc:
        raise RuntimeError("读取设备图库需要 ezdxf，请修复 Python 依赖后重试") from exc
    except Exception as exc:
        raise RuntimeError(f"读取图库文件 {path} 失败：{exc}；请先修复文件或运行环境") from exc
    return path, name, block, bounds


def preflight(spec):
    """建图之前一次列全设备缺口，避免绘制一半才发现无块。"""
    missing = []
    blocks = {}
    for eq in spec.get("equipment", []):
        try:
            blocks[str(eq['id'])] = source(eq)[1]
        except BlockConfirmationRequired as exc:
            missing.append(f"{eq.get('id', '?')}（模块 {eq.get('module_type') or eq.get('type', '?')}）：{exc}")
    if missing:
        raise BlockConfirmationRequired("以下设备需要用户确认图块，尚未出图：\n" + "\n".join(missing))
    for pipe in spec.get('pipes', []):
        for ref in (str(pipe.get('from', '')), str(pipe.get('to', ''))):
            equipment, _, port = ref.partition('.')
            if '@' not in ref and blocks.get(equipment) in CIRCULAR_HX_BLOCKS and port not in CIRCULAR_HX_PROCESS_PORTS:
                raise ValueError(f"流股{pipe.get('id','?')}的{ref}不是允许的换热器物流接点；只允许左右水平箭头，禁止top/bottom/shell_in/shell_out")


def place(drawing, eq, at, rot=0.0, tag=""):
    """读取源 BLOCK 的实体，刚体变换后以 Group 输出为图块引用。"""
    from chemcad_symbols import LocalDraw
    path, name, block, bounds = source(eq)
    # 所有已确认的具名源块均原生输出；目录的新版标志不应决定曲线是否保真。
    native = True
    ld = LocalDraw(name, "equipment", tag=tag, ref=path)
    # 图库图册块以左下角为基点；统一把真实包围盒中心作为排版插入点。
    cx = (bounds.extmin.x + bounds.extmax.x) / 2
    cy = (bounds.extmin.y + bounds.extmax.y) / 2
    def pt(value):
        return float(value[0]) - cx, float(value[1]) - cy

    def emit(ent):
        typ = ent.dxftype()
        layer = geometry_layer(ent.dxf.layer)
        if layer not in ("EQUIP", "EQUIP_IN", "NOZZLE", "HATCH", "CENTERLINE", "TAG", "FLOWARROW", "VALVE"):
            raise BlockConfirmationRequired(f"块 {name} 含未登记图层 {ent.dxf.layer!r}，请确认图层用途")
        if typ == "LINE":
            ld.line(pt(ent.dxf.start), pt(ent.dxf.end), layer)
        elif typ == "CIRCLE":
            ld.circle(pt(ent.dxf.center), ent.dxf.radius, layer)
        elif typ == "ARC":
            ld.arc(pt(ent.dxf.center), ent.dxf.radius, ent.dxf.start_angle, ent.dxf.end_angle, layer)
        elif typ == "LWPOLYLINE" and not ent.has_arc:
            ld.poly([pt(p) for p in ent.get_points("xy")], layer, closed=ent.closed)
        elif typ in ("ELLIPSE", "SPLINE"):
            # 新版原生块在输出时直接复制；此折线仅用于排版和关系校核。
            ld.poly([pt(p) for p in ent.flattening(0.01)], layer)
        elif typ == "HATCH" and native:
            from ezdxf.render.hatching import hatch_boundary_paths
            for boundary in hatch_boundary_paths(ent):
                ld.poly([pt(p) for p in boundary.flattening(0.01)], "HATCH", closed=True)
        elif typ == "SOLID":
            ld.solid([pt(p) for p in ent.wcs_vertices()], layer)
        elif typ in ("INSERT", "LWPOLYLINE", "POLYLINE"):
            for child in ent.virtual_entities():
                emit(child)
        elif typ == "TEXT":
            ld.text(ent.dxf.text, pt(ent.dxf.insert), layer, height=ent.dxf.height,
                    rotation=ent.dxf.rotation)
        elif typ == 'MTEXT':
            ld.text(ent.plain_text(),pt(ent.dxf.insert),layer,height=ent.dxf.char_height,
                    rotation=ent.dxf.get('rotation',0))
        elif typ == 'ATTDEF':
            ld.text(ent.dxf.text,pt(ent.dxf.insert),layer,height=ent.dxf.height,
                    rotation=ent.dxf.rotation)
        else:
            raise BlockConfirmationRequired(f"块 {name} 含未支持实体 {typ}，请用户确认可用图块；禁止替代")
    for ent in block:
        emit(ent)
    x0, y0, x1, y1 = bounds.extmin.x-cx, bounds.extmin.y-cy, bounds.extmax.x-cx, bounds.extmax.y-cy
    # 取源图形实际极值点，不能把包围盒边中点当作接管。
    from ezdxf.path import make_path
    geometry, nozzles = [], []
    def leaves(entities):
        for entity in entities:
            if entity.dxftype()=='INSERT':yield from leaves(entity.virtual_entities())
            else:yield entity
    for entity in leaves(block):
        layer = geometry_layer(entity.dxf.layer)
        if layer not in ("EQUIP", "EQUIP_IN", "NOZZLE"):
            continue
        try:
            vertices = [pt(v) for v in make_path(entity).flattening(.001)]
        except (TypeError, ValueError):
            continue  # 无可用路径的实体不提供连接候选，后续无候选会明确报错。
        if entity.dxftype() == "LINE":
            a,b=vertices[0],vertices[-1]
            if abs(a[0]-b[0])<1e-7 and min(a[1],b[1])<=0<=max(a[1],b[1]):
                vertices.append((a[0],0.0))
            if abs(a[1]-b[1])<1e-7 and min(a[0],b[0])<=0<=max(a[0],b[0]):
                vertices.append((0.0,a[1]))
        geometry.extend(vertices)
        if layer == "NOZZLE":
            nozzles.extend(vertices)
    if not geometry:
        raise BlockConfirmationRequired(f"块 {name} 无可识别的连接轮廓，请用户指定连接点")
    points = {}
    for key,axis,sign in (("left",0,-1),("right",0,1),("top",1,1),("bottom",1,-1)):
        edge = max(sign*q[axis] for q in geometry)
        candidates = [q for q in nozzles if abs(sign*q[axis]-edge)<1e-6]
        candidates = candidates or [q for q in geometry if abs(sign*q[axis]-edge)<1e-6]
        points[key] = min(candidates,key=lambda q:abs(q[1-axis]))
    if name == "pump.centrifugal":
        # 用户确认的离心泵连接：左侧吸入口、顶部竖向出口。
        points['right'] = points['top']
    if name in CIRCULAR_HX_BLOCKS:
        # 从真实NOZZLE直线取外端点，不再用图块极值挑选斜向箭头。
        horizontal = [entity for entity in block if entity.dxftype() == 'LINE'
                      and geometry_layer(entity.dxf.layer) == 'NOZZLE'
                      and abs(entity.dxf.start.y-entity.dxf.end.y)<1e-6]
        if len(horizontal) != 2:
            raise BlockConfirmationRequired(f"块{name}的左右水平接管已改变，请用户确认接点")
        ends = [pt(p) for entity in horizontal for p in (entity.dxf.start,entity.dxf.end)]
        points['left'] = min(ends, key=lambda p:p[0])
        points['right'] = max(ends, key=lambda p:p[0])
        ld.meta['process_anchor_names'] = CIRCULAR_HX_PROCESS_PORTS
    for key, value in points.items():
        ld.anchor(key, value)
    for key, target in {"inlet":"left", "outlet":"right", "feed":"left", "side":"right",
                        "tube_in":"left", "tube_out":"right", "shell_in":"top", "shell_out":"bottom"}.items():
        ld.anchor(key, points[target])
    if name == "hx.kettle_reboiler":
        ld.anchor("vapor", points["top"])
    ld.meta.update({"library_file": path, "library_block": name, "geometry_source": "library_block",
                    "curve_tolerance_mm": 0.01, "anchor_basis": "source_geometry",
                    "native_geometry": native, "source_center": (cx, cy)})
    group = ld.commit(drawing, at, rot)
    group.meta["id"] = str(eq["id"])
    if hasattr(drawing, "objects"):
        drawing.objects[str(eq["id"])] = group
    return group
