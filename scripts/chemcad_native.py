"""原生块依赖校核和来源隔离导入；不展开曲线或重画几何。"""
from __future__ import annotations
import hashlib,json
from collections import Counter

def extension_signature(entity):
    """完整检查扩展字典及其对象树；悬空句柄属于缺失依赖。"""
    from ezdxf.lldxf.tagwriter import TagCollector
    if not entity.has_extension_dict:return None
    visiting=set()
    def walk(obj):
        if isinstance(obj,str):
            handle=obj;obj=entity.doc.entitydb.get(handle)
            if obj is None:raise ValueError(f'扩展对象句柄缺失：{handle}')
        handle=obj.dxf.handle
        if handle in visiting:return ('cycle',obj.dxftype())
        visiting.add(handle)
        if obj.dxftype()=='DICTIONARY':
            result=('DICTIONARY',tuple(sorted((key,walk(value)) for key,value in obj.items())))
        else:
            tags=TagCollector.dxftags(obj,dxfversion='AC1032')
            data=[]
            for tag in tags:
                if tag.code in (5,105,330,360):continue
                if 340<=tag.code<=369 and isinstance(tag.value,str) and tag.value!='0':
                    linked=entity.doc.entitydb.get(tag.value)
                    if linked is None:raise ValueError(f'扩展对象引用缺失：{tag.value}')
                    data.append((tag.code,linked.dxftype()))
                else:data.append((tag.code,str(tag.value)))
            result=(obj.dxftype(),tuple(data))
        visiting.remove(handle)
        return result
    return walk(entity.get_extension_dict().dictionary)

def dependencies(doc,name):
    found=[];missing=[];visiting=set()
    def walk(key):
        if key in visiting:return
        visiting.add(key)
        if key not in doc.blocks:missing.append({'type':'block','name':key});return
        block=doc.blocks[key];found.append(key)
        if block.block.dxf.flags & 12:missing.append({'type':'xref','name':key})
        for ent in block:
            typ=ent.dxftype()
            if typ=='INSERT':walk(ent.dxf.name)
            elif typ=='DIMENSION' and ent.dxf.get('geometry'):walk(ent.dxf.geometry)
            if typ in ('DIMENSION','LEADER') and ent.dxf.dimstyle not in doc.dimstyles:
                missing.append({'type':'dimstyle','name':ent.dxf.dimstyle})
            if typ in ('TEXT','MTEXT','ATTDEF','ATTRIB') and ent.dxf.style not in doc.styles:
                missing.append({'type':'style','name':ent.dxf.style})
            if typ in ('IMAGE','PDFUNDERLAY','DWFUNDERLAY','DGNUNDERLAY'):
                missing.append({'type':'external_media','handle':ent.dxf.handle})
    walk(name)
    return found,missing

def geometry_digest(entities, *, resource_neutral=False, detach_hatch=False):
    """除实体身份/所有者外保留原DXF属性和扩展数据；依赖名称另由闭包核对。"""
    from ezdxf.lldxf.tagwriter import TagCollector
    records=[]
    for e in entities:
        # 写出再读会省略这些显式默认值；缺省与原值语义完全一致。
        e=e.copy()
        children=([*e.attribs] if e.dxftype()=='INSERT' else [*e.vertices] if e.dxftype()=='POLYLINE' else [])
        for entity in [e]+children:
            for key in list(entity.dxf.all_existing_dxf_attribs()):
                attr=entity.DXFATTRIBS.get(key)
                if attr is not None and attr.default is not None and entity.dxf.get(key)==attr.default:entity.dxf.discard(key)
        if detach_hatch and e.dxftype()=='HATCH':
            e=e.copy()
            for path in e.paths:path.source_boundary_objects=[]
        tags=TagCollector.dxftags(e,dxfversion='AC1032')
        skip={5,105,330,360}
        if resource_neutral:
            skip.update({6,7,8,102,1005});skip.update(range(320,370))
            if e.dxftype() in ('INSERT','DIMENSION'):skip.add(2)
            if e.dxftype() in ('DIMENSION','LEADER'):skip.add(3)
        records.append([(t.code,str(t.value)) for t in tags if t.code not in skip])
    raw=json.dumps(records,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def preserve_styles(source,target,groups,prefix):
    """Loader把Standard视为特殊项；显式复制源样式，避免改用默认字体。"""
    styles={}
    for original,loaded in groups:
        pairs=list(zip(original,loaded))
        for a,b in list(pairs):
            if a.dxftype()=='INSERT':pairs.extend(zip(a.attribs,b.attribs))
        for a,b in pairs:
            if a.dxftype() not in ('TEXT','MTEXT','ATTDEF','ATTRIB'):continue
            old=a.dxf.style
            if old not in styles:
                entry=source.styles.get(old);attrs=entry.dxf.all_existing_dxf_attribs()
                attrs={key:value for key,value in attrs.items() if key not in ('name','handle','owner')}
                name=f'{prefix}$FONT${old}'
                if name not in target.styles:
                    style=target.styles.new(name,dxfattribs=attrs)
                    if entry.has_extended_font_data:
                        family,italic,bold=entry.get_extended_font_data()
                        style.set_extended_font_data(family,italic=italic,bold=bold)
                styles[old]=name
            b.dxf.style=styles[old]
    return styles

def preserve_layer_linetypes(source,target,groups,prefix):
    """Loader可能保留来源图层上的旧线型名；按已导入资源重新绑定。"""
    # 同一个源层可能在多个实体中重复使用；写目标前冻结源值。
    layer_types={entry.dxf.name:entry.dxf.linetype for entry in source.layers}
    def resolve(name):
        if name.upper() in ('BYLAYER','BYBLOCK','CONTINUOUS'):return name
        candidates=[entry.dxf.name for entry in target.linetypes
                    if entry.dxf.name.casefold().startswith(prefix.casefold()+'$')
                    and entry.dxf.name.casefold().endswith('$'+name.casefold())]
        if len(candidates)==1:return candidates[0]
        if name in target.linetypes:return name
        raise ValueError(f'线型依赖未完整导入：{name}')
    for original,loaded in groups:
        for a,b in zip(original,loaded):
            if a.dxf.layer not in source.layers:raise ValueError(f'缺少源图层：{a.dxf.layer}')
            original_type=layer_types[a.dxf.layer];after=target.layers.get(b.dxf.layer)
            if original_type not in source.linetypes:raise ValueError(f'源线型缺失：{original_type}')
            after.dxf.linetype=resolve(original_type)
            if a.dxf.linetype.upper() not in ('BYLAYER','BYBLOCK'):
                b.dxf.linetype=resolve(a.dxf.linetype)

def import_block(source,target,name,prefix):
    """递归导入，全部来源样式/图层/块名加前缀，不与其他文件同名资源串用。"""
    from ezdxf.xref import Loader,ConflictPolicy
    closure,missing=dependencies(source,name)
    if missing:raise ValueError(f'图块{name}依赖缺失：{missing}')
    before=set(target.blocks.block_names())
    source_layer_types={entry.dxf.name:entry.dxf.linetype for entry in source.layers}
    loader=Loader(source,target,ConflictPolicy.XREF_PREFIX)
    loader.load_block_layout(source.blocks[name]);loader.execute(prefix)
    # ezdxf Loader实测改写了来源Layer的线型名，却未改目标Layer；
    # 冻结值还原来源内存，再显式重绑定目标，不能把被改写的源值当基线。
    for layer_name,linetype in source_layer_types.items():source.layers.get(layer_name).dxf.linetype=linetype
    created=set(target.blocks.block_names())-before
    names={n:next((k for k in created if k.casefold().startswith(prefix.casefold()+'$') and k.casefold().endswith('$'+n.casefold())),None) for n in closure}
    used=set(n for n in names.values() if n)
    for old,new in list(names.items()):
        if new or not old.startswith('*'):continue
        digest=geometry_digest(source.blocks[old],resource_neutral=True,detach_hatch=True)
        matches=[n for n in created-used if geometry_digest(target.blocks[n],resource_neutral=True,detach_hatch=True)==digest]
        if matches:names[old]=sorted(matches)[0];used.add(names[old])
    handle_map={}
    for old,new in names.items():
        if new:
            for a,b in zip(source.blocks[old],target.blocks[new]):handle_map[a.dxf.handle]=b.dxf.handle
    for old,new in names.items():
        if not new:continue
        for a,b in zip(source.blocks[old],target.blocks[new]):
            if a.dxftype()=='HATCH':
                for original,clone in zip(a.paths,b.paths):
                    handles=original.source_boundary_objects
                    if any(h not in handle_map for h in handles):
                        raise ValueError(f'填充关联边界在块依赖闭包之外：{old}')
                    clone.source_boundary_objects=[handle_map[h] for h in handles]
                b.dxf.associative=a.dxf.associative
    preserve_styles(source,target,[(source.blocks[old],target.blocks[new]) for old,new in names.items() if new],prefix)
    preserve_layer_linetypes(source,target,[(source.blocks[old],target.blocks[new]) for old,new in names.items() if new],prefix)
    for old,new in names.items():
        if not new:continue
        for a,b in zip(source.blocks[old],target.blocks[new]):
            if a.dxftype() not in ('DIMENSION','LEADER'):continue
            style=a.dxf.dimstyle
            matches=[s.dxf.name for s in target.dimstyles if s.dxf.name.casefold().startswith(prefix.casefold()+'$') and s.dxf.name.casefold().endswith('$'+style.casefold())]
            if len(matches)==1:b.dxf.dimstyle=matches[0]
            elif style in target.dimstyles:b.dxf.dimstyle=style
            else:raise ValueError(f'标注样式依赖未完整导入：{style}')
    for old,new in names.items():
        if not new:raise ValueError(f'递归导入未产生{old}')
        src,dst=source.blocks[old],target.blocks[new]
        if Counter(e.dxftype() for e in src)!=Counter(e.dxftype() for e in dst):
            raise ValueError(f'导入实体类型/数量变化：{old}')
        if geometry_digest(src,resource_neutral=True)!=geometry_digest(dst,resource_neutral=True):
            raise ValueError(f'导入原生实体属性变化：{old}')
        # 曲线控制点、填充路径、属性及文本内容由原生copy流程保存；资源名合法改写。
        for a,b in zip(src,dst):
            if a.has_extension_dict and extension_signature(a)!=extension_signature(b):
                raise ValueError(f'扩展依赖丢失或改变：{old}/{a.dxf.handle}')
            if a.dxftype() in ('TEXT','MTEXT','ATTDEF','ATTRIB'):
                field='text'
                if a.dxf.get(field)!=b.dxf.get(field):raise ValueError(f'文本失真：{old}')
    return names[name],names

