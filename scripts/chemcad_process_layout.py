"""从明确设备名称、型式和有向连接组织塔循环；未知用途保持拓扑排版。"""
from collections import deque
import re

def recognize(spec,members):
    eq={str(e['id']):e for e in spec.get('equipment',[]) if str(e['id']) in members}
    edges={};adj={k:[] for k in eq}
    for pipe in spec.get('pipes',[]):
        a=str(pipe.get('from','')).split('.')[0];b=str(pipe.get('to','')).split('.')[0]
        if a in eq and b in eq:adj[a].append(b);edges.setdefault((a,b),[]).append(str(pipe.get('id','')))
    def path(a,b,allowed):
        todo=deque([(a,[a])]);visited={a}
        while todo:
            node,route=todo.popleft()
            if node==b:return route
            for nxt in adj[node]:
                if nxt in allowed and nxt not in visited:visited.add(nxt);todo.append((nxt,route+[nxt]))
        return []
    loops=[]
    for column,e in eq.items():
        if not str(e.get('block') or e.get('type','')).startswith('column.'):continue
        eligible={k for k,v in eq.items() if re.search('塔顶.*(?:冷凝|冷却)|回流',str(v.get('name','')))}|{column}
        for nxt in adj[column]:
            first=eq[nxt]
            if nxt in eligible and re.search('塔顶.*(?:冷凝|冷却)',str(first.get('name',''))) and str(first.get('block') or first.get('type','')).startswith('hx.'):
                cycle=path(nxt,column,eligible)
                if len(cycle)>=3:
                    nodes=cycle[:-1]
                    loops.append({'kind':'overhead_reflux','column':column,'members':nodes,
                                  'return_lines':edges.get((nodes[-1],column),[]),'evidence':'明确塔顶冷凝/回流名称+设备型式+闭合有向连接'})
        for nxt in adj[column]:
            first=eq[nxt]
            if column in adj[nxt] and re.search('再沸|重沸',str(first.get('name',''))) and str(first.get('block') or first.get('type','')).startswith('hx.'):
                loops.append({'kind':'bottom_reboiler','column':column,'members':[nxt],
                              'return_lines':edges.get((nxt,column),[]),'evidence':'明确再沸名称+换热器型式+塔底返回连接'})
    return loops

def arrange(spec,rel,boxes):
    loops=recognize(spec,set(rel));clear=60.0 # 完整标注、箭头及两端接管的预留直段
    def rect(k):
        x,y=rel[k];b=boxes[k].body
        return (x+b[0],y+b[1],x+b[2],y+b[3])
    all_loops=recognize(spec,{str(e['id']) for e in spec.get('equipment',[])})
    if not all_loops:return rel,None,[]
    main=[k for k in rel if not any(k in l['members'] for l in loops)]
    # 已识别回路的同页主流程按实际单链排列，给标注预留直段；多支路继续用ELK。
    adjacency={k:[] for k in main};degree={k:0 for k in main}
    for p in spec.get('pipes',[]):
        if p.get('layer','PIPE_PRI')!='PIPE_PRI':continue
        a=str(p.get('from','')).split('.')[0];b=str(p.get('to','')).split('.')[0]
        if a in adjacency and b in adjacency and b not in adjacency[a]:adjacency[a].append(b);degree[b]+=1
    if main and all(len(v)<=1 for v in adjacency.values()) and all(v<=1 for v in degree.values()):
        starts=[k for k in main if degree[k]==0];ordered=[]
        if len(starts)==1:
            node=starts[0]
            while node not in ordered:
                ordered.append(node)
                if not adjacency[node]:break
                node=adjacency[node][0]
        if len(ordered)==len(main):
            x=0.
            for k in ordered:
                box=boxes[k];anchor=box.anchors.get('inlet') or box.anchors.get('left') or (0.,0.)
                # 普通主链40mm容纳25mm参数框、两侧净距和流向箭头；
                # 塔回路接管通道仍按下面的60mm预留。
                rel[k]=(x-box.body[0],-anchor[1]);x+=box.w+40.0
    top=max(rect(k)[3] for k in main);bottom=min(rect(k)[1] for k in main)
    for loop in loops:
        c=rect(loop['column']);x=c[2]+clear
        if loop['kind']=='bottom_reboiler':x+=80.0 # 与塔顶设备保留独立底部文字列
        for k in loop['members']:
            box=boxes[k];y=top+clear if loop['kind']=='overhead_reflux' else bottom-clear-box.h
            rel[k]=(x-box.body[0],y-box.body[1]);x+=box.w+clear
    bounds=[rect(k) for k in rel]
    # 整段外缘留完整25mm标注框及3mm净距所需28mm。
    # 把直段长度再用于两侧空白会累计放大多工段总宽，挤出底部文字列。
    padding=28.0
    x0=min(r[0] for r in bounds)-padding;y0=min(r[1] for r in bounds)-padding
    width=max(r[2] for r in bounds)-x0+padding;height=max(r[3] for r in bounds)-y0+padding
    rel={k:(x-x0,y-y0) for k,(x,y) in rel.items()}
    return rel,(width,height),loops

