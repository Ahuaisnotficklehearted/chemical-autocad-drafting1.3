"""工段逐行装订：多流程页 + 每工段唯一物料汇总框。"""
from __future__ import annotations
import argparse,copy,hashlib,json,math,os,tempfile
from pathlib import Path

GAP=150.0

def model_identity(spec, path):
    return str(spec.get('model_id') or spec.get('_model_id') or Path(path).resolve())

def merge_streams(pages):
    """原模型身份+原流股ID去重；相同字段冲突必须报错。"""
    merged={}; sources={}
    for path,spec in pages:
        model=model_identity(spec,path)
        records=dict(spec.get('streams') or {})
        for connector in list(spec.get('offpage',[]))+list(spec.get('battery',[])):
            key=connector.get('stream_id') or connector.get('id') or connector.get('stream_no')
            data=connector.get('stream_data')
            if key and isinstance(data,dict):
                current=records.setdefault(str(key),{'no':connector.get('stream_no') or key})
                for field,value in data.items():
                    if value in (None,''):continue
                    if current.get(field) not in (None,'',value):
                        raise ValueError(f'{path} 流股{key}接续参数{field}与流股记录冲突')
                    current[field]=value
        for key,original in records.items():
            if not isinstance(original,dict):raise ValueError(f'{path} 流股{key}参数不是对象')
            row=copy.deepcopy(original)
            identity=(model,str(row.get('source_stream_id') or key))
            target=merged.setdefault(identity,{})
            for field,value in row.items():
                if value in (None,''):continue
                if field in target and target[field]!=value:
                    raise ValueError(f'原模型{model}流股{identity[1]}字段{field}冲突：{target[field]!r} / {value!r}')
                target[field]=value
            sources.setdefault(identity,[]).append(str(path))
    numbers={}
    for identity,row in merged.items():
        row.setdefault('no',identity[1]);numbers.setdefault(str(row['no']),[]).append(identity)
    # 不同原模型的同号物流仍各列一项，列头加模型序号以消除歧义。
    models=list(dict.fromkeys(model for model,stream in merged))
    for no,identities in numbers.items():
        if len(identities)>1:
            for identity in identities:merged[identity]['no']=f'M{models.index(identity[0])+1}:{no}'
    return merged,sources

def validate_continuations(all_pages):
    owners={};records={};locations={}
    for path,spec in all_pages:
        model=model_identity(spec,path)
        for e in spec.get('equipment',[]):
            head=str(e['id']);identity=(model,head)
            if identity in locations:raise ValueError(f'模型{model}设备{head}重复归属流程页')
            locations[identity]=str(Path(path).resolve());owners.setdefault(model,set()).add(head)
        for op in spec.get('offpage',[]):
            pair=op.get('pair')
            if not pair:continue
            identity=(model,str(op.get('stream_id') or op.get('line') or op.get('id') or op.get('stream_no') or ''),tuple(map(str,pair)))
            here=str(op.get('at','')).partition('.')[0]
            if len(pair)!=2 or here not in [str(ref).partition('.')[0] for ref in pair]:
                raise ValueError(f'{path} 接续本地设备{here}不在连接pair中')
            if here not in {str(e['id']) for e in spec.get('equipment',[])}:raise ValueError(f'{path} 接续本地设备未出图')
            page=str(Path(path).resolve())
            if page in records.setdefault(identity,{}):raise ValueError(f'{path} 流股接续重复')
            records[identity][page]=here
    checked=[]
    for (model,stream,pair),pages in records.items():
        heads=[ref.partition('.')[0] for ref in pair]
        internal=all(head in owners.get(model,set()) for head in heads)
        if internal and (len(pages)!=2 or set(pages.values())!=set(heads)):
            raise ValueError(f'跨页流股{stream} {pair}须在两端页面接续，实际{pages}')
        checked.append({'model':model,'stream':stream,'pair':pair,'pages':sorted(pages),'internal':internal})
    return checked

def frame_bounds(doc):
    from ezdxf import bbox
    frames=[e for e in doc.modelspace().query('INSERT') if e.dxf.name.startswith('DEFAULT_TEXT_FRAME_A1')]
    if len(frames)!=1:raise ValueError(f'单页需一个原生文本框，实际{len(frames)}个')
    ins=frames[0]
    # 外框实际几何，不把文字和流程外包范围误作工段行边界。
    geometry=[e for e in ins.virtual_entities() if e.dxftype() not in ('TEXT','MTEXT','ATTRIB','ATTDEF')]
    ext=bbox.extents(geometry)
    return (ext.extmin.x,ext.extmin.y,ext.extmax.x,ext.extmax.y)

def build_manifest(manifest_path,out_dxf,*,work=None):
    import ezdxf
    from chemcad_native import import_block
    import draw_pfd as P
    import chemcad_engine as E
    import chemcad_layers as LY
    base=Path(manifest_path).resolve().parent
    manifest=json.loads(Path(manifest_path).read_text(encoding='utf-8-sig'))
    sections=manifest.get('sections') or []
    if not sections:raise ValueError('工段清单sections不能为空')
    if len({str(s['id']) for s in sections})!=len(sections):raise ValueError('工段编号重复')
    work=Path(work or tempfile.mkdtemp(prefix='chemcad_sections_'));work.mkdir(parents=True,exist_ok=True)
    loaded=[]
    for section in sections:
        paths=section.get('pages') or []
        if not paths:raise ValueError(f"工段{section['id']}没有流程页")
        pages=[]
        for value in paths:
            path=(base/str(value)).resolve()
            spec=json.loads(path.read_text(encoding='utf-8-sig'))
            pages.append((path,spec))
        loaded.append((section,pages))
    continuations=validate_continuations([p for s,pages in loaded for p in pages])
    master=ezdxf.new('R2018');msp=master.modelspace();LY.ensure_linetypes(master)
    for definition in LY.STANDARD:
        attrs={'color':definition.color,'lineweight':definition.lineweight,'plot':int(definition.plot),
               'linetype':definition.linetype,'flags':definition.flags}
        if definition.name in master.layers:
            for key,value in attrs.items():setattr(master.layers.get(definition.name).dxf,key,value)
        else:master.layers.new(definition.name,dxfattribs=attrs)
    placed=[];summaries=[];row_top=None
    for row,(section,pages) in enumerate(loaded):
        documents=[];checks=[]
        for column,(path,spec) in enumerate(pages):
            flow=copy.deepcopy(spec);flow['stream_table']=False
            flow['_book_page_number']=column+1;flow['_book_page_total']=len(pages)+1
            flow['sheet']='A1';flow.setdefault('layout',{})['sheet']='A1'
            cd,loops,layout=P.build_with_layout(flow)
            preflight=E.preflight(cd,loops)
            if preflight['errors']:raise ValueError(f'{path.name}出图检查失败：{preflight["errors"]}')
            output=work/f'{row:03d}_{column:03d}.dxf'
            LY.apply_standard(cd);E.render(cd,str(output),backend='dxf')
            documents.append((ezdxf.readfile(output),'flow',str(path)))
            checks.append({'page':str(path),'preflight':preflight,'layout':P._layout_payload(cd,flow,layout)})
        merged,sources=merge_streams(pages)
        # 没有物流也明确生成空汇总页，维持每工段一框接口。
        cd=E.ChemDrawing(name='物料平衡表',sheet='A1')
        first=copy.deepcopy(pages[0][1]);first['streams']={f'{i}':r for i,r in enumerate(merged.values())}
        first.update(std_tables=True,stream_table=True,section_stream_boxes=False)
        first['_book_page_number']=len(pages)+1;first['_book_page_total']=len(pages)+1
        if first['streams']:P._draw_std_tables(cd,'pfd',None,{},first)
        else:
            import chemcad_symbols as S
            S.add_user_text_frame(cd,(991.,0.),fields=first.get('title_block'),title='物料平衡表',
                                  number=len(pages)+1,total=len(pages)+1)
        cd.translate(-991.,0.,skip_layers=())
        LY.apply_standard(cd)
        output=work/f'{row:03d}_material.dxf';E.render(cd,str(output),backend='dxf')
        documents.append((ezdxf.readfile(output),'material',None))
        bounds=[frame_bounds(d) for d,role,path in documents]
        heights=[b[3]-b[1] for b in bounds]
        if max(heights)-min(heights)>.001:raise ValueError('同工段图框高度不一致')
        height=max(heights)
        bottom=0. if row_top is None else row_top-GAP-height
        x=0.
        for column,((doc,role,path),box) in enumerate(zip(documents,bounds)):
            dx,dy=x-box[0],bottom-box[1]
            for ent in doc.modelspace():ent.translate(dx,dy,0.)
            # 把原实体移入原生整页块，句柄及填充关联保持；来源依赖全部隔离。
            page_name=f'SECTION_PAGE_{row}_{column}'
            page_block=doc.blocks.new(page_name)
            for ent in list(doc.modelspace()):
                doc.modelspace().unlink_entity(ent);page_block.add_entity(ent)
            native_name,dependencies=import_block(doc,master,page_name,f'PAGE{row}_{column}')
            msp.add_blockref(native_name,(0,0,0))
            # 工段与单页继续使用既有标准图层及线宽，样式/嵌套块保持隔离。
            for block_name in dependencies.values():
                for ent in master.blocks[block_name]:
                    standard=ent.dxf.layer.rsplit('$',1)[-1]
                    if standard in {definition.name for definition in LY.STANDARD}:ent.dxf.layer=standard
            placed.append({'section':str(section['id']),'row':row,'column':column,'role':role,'page':path,
                           'rect':[x,bottom,x+box[2]-box[0],bottom+height],'translation':[dx,dy]})
            x+=box[2]-box[0]+GAP
        row_top=bottom
        summaries.append({'section':str(section['id']),'name':section.get('name',section['id']),
                          'streams':[{'model':i[0],'source_stream_id':i[1],'data':r,'pages':sources[i]} for i,r in merged.items()],
                          'checks':checks})
    master.header['$INSUNITS']=4;master.header['$LWDISPLAY']=1;master.header['$CELWEIGHT']=-1
    used_layers={e.dxf.layer for block in master.blocks for e in block}
    for layer in list(master.layers):
        if '$' in layer.dxf.name and layer.dxf.name not in used_layers:master.layers.remove(layer.dxf.name)
    LY.apply_dxf_layer_settings(master)
    master.saveas(out_dxf)
    report={'ok':True,'gap_mm':GAP,'placed':placed,'sections':summaries,'continuations':continuations,'dxf':str(out_dxf)}
    Path(str(out_dxf)+'.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    return report

def main(argv=None):
    ap=argparse.ArgumentParser(description='工段逐行装订，末尾唯一汇总物料框')
    ap.add_argument('--manifest',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--backend',choices=('dwg','dxf'),default='dwg')
    args=ap.parse_args(argv)
    try:
        path=Path(args.out).resolve();path.parent.mkdir(parents=True,exist_ok=True)
        dxf=path if args.backend=='dxf' else path.with_suffix('.dxf')
        report=build_manifest(args.manifest,str(dxf))
        if args.backend=='dwg':
            import chemcad_dwg as D
            saved=D.dxf_to_dwg_com(str(dxf),str(path))
            if not saved.get('ok'):raise RuntimeError(saved)
            report['dwg']=str(path);report['save']=saved
            Path(str(dxf)+'.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'ok':True,'sections':len(report['sections']),'frames':len(report['placed']),'path':str(path)},ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f'工段装订失败：{exc}');return 3

if __name__=='__main__':raise SystemExit(main())
