"""全类别增量图库：查询、来源和明确图块调用。现有设备目录优先。"""
from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]/'assets'/'learned-library'
CATEGORIES=('equipment','valve','piping','instrument','connector','exterior','frame','unidentified')
def catalog():
    path=ROOT/'catalog.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'assets':[]}
def assets(category='',source=''):
    if category and category not in CATEGORIES:raise ValueError(f'未知类别{category}；可选{CATEGORIES}')
    return [r for r in catalog()['assets'] if (not category or r['category']==category) and (not source or any(source in str(s) for s in r['sources']))]
def find(key):
    return next((r for r in assets() if key in (r['id'],r['label'],r['block_name'])),None)
def source(row,*,pfd=False):return str(ROOT/(row.get('pfd_file',row['file']) if pfd else row['file'])),row['block_name']
def selection(key):
    row=find(key)
    if not row or not row.get('pfd_eligible'):return None
    return source(row,pfd=True)

def main(argv=None):
    import argparse,sys
    ap=argparse.ArgumentParser(description='原生图库块按明确来源ID调用；无缩放')
    ap.add_argument('--id',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--x',type=float,default=0);ap.add_argument('--y',type=float,default=0)
    args=ap.parse_args(argv)
    try:
        row=find(args.id)
        if not row:print('没有这个图块');return 4
        import ezdxf
        from chemcad_native import import_block
        path,name=source(row);doc=ezdxf.readfile(path);target=ezdxf.new(doc.dxfversion)
        output,mapping=import_block(doc,target,name,'ASSET')
        target.header['$INSUNITS']=row['units']
        target.modelspace().add_blockref(output,(args.x,args.y))
        destination=Path(args.out).resolve();destination.parent.mkdir(parents=True,exist_ok=True)
        dxf=destination.with_suffix('.dxf');target.saveas(dxf)
        if destination.suffix.lower()=='.dwg':
            import chemcad_dwg as D
            rep=D.dxf_to_dwg_com(str(dxf),str(destination))
            if not rep.get('ok'):raise ValueError(rep)
        elif destination.suffix.lower()!='.dxf':raise ValueError('输出后缀需DXF或DWG')
        print(json.dumps({'ok':True,'id':row['id'],'out':str(destination),'sources':row['sources']},ensure_ascii=False));return 0
    except Exception as e:print('图库调用失败：'+str(e));return 3
if __name__=='__main__':raise SystemExit(main())

