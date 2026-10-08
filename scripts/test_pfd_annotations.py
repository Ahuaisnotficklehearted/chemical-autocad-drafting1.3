"""用户授权的PFD标注回归；真实图库/DXF，无AutoCAD依赖。"""
import json
import io
from contextlib import redirect_stdout
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ezdxf
import chemcad_block_library as BL
import chemcad_engine as E
import chemcad_layout as L
import chemcad_symbols as S
import draw_pfd as P
import chemcad_dwg as DWG
import chemcad_book as BOOK
import chemcad_score as SCORE
from types import SimpleNamespace
from chemcad_ir import Text, Line, Polyline, Solid


class PfdAnnotations(unittest.TestCase):
    def test_equipment_name_uses_center_axis_intersection_not_lowest_bbox(self):
        cd=E.ChemDrawing()
        g=cd.group('斜底设备',(0,0),kind='equipment')
        g.ents=[Polyline(layer='EQUIP',pts=[(0,0),(20,10),(20,20),(0,20)],closed=True)]
        self.assertEqual(E.equipment_label_point(g),(10,-5))
        label=S.add_equipment_labels(cd,{'id':'E','tag':'E','name':'设备'},E.group_rect(g),False,equipment=g)[0]
        self.assertEqual(label.base,(10,-5))
        name=next(t for t in label.ents if isinstance(t,Text) and t.text=='设备')
        self.assertEqual(E.rect_center(E.text_rect(name)),(10,-5))

    def test_equipment_name_position_is_fixed_and_checked(self):
        cd=E.ChemDrawing()
        eq={'id':'E','tag':'E-103','type':'hx.heating','name':'加热器'}
        g=BL.place(cd,eq,(100,100))
        label=S.add_equipment_labels(cd,eq,E.group_rect(g),True,equipment=g)[0]
        target=E.equipment_label_point(g)
        self.assertFalse(E.validate_equipment_label_positions(cd))
        cd.line((target[0]-20,target[1]),(target[0]+20,target[1]),'PIPE_PRI')
        L.polish_texts(cd)
        self.assertEqual(label.base,target)
        self.assertTrue(E.validate_text_over_line(cd))
        label.base=(target[0]+1,target[1])
        self.assertTrue(E.validate_equipment_label_positions(cd))
        label.base=target
        label.ents[-1].pt=(target[0]+1,target[1])
        self.assertTrue(E.validate_equipment_label_positions(cd))

    def test_vertical_side_frame_matches_right_vertex_reference(self):
        cd=E.ChemDrawing()
        g=S.add_stream_data(cd,(100,100),fields=['W 150 kg/h','T 30 ℃','P 3 bar'],placement='right')
        h=S.STREAMNO_DIAMOND_HALF
        r=E.stream_table_rect(g)
        self.assertAlmostEqual(r[0],100+h)
        self.assertAlmostEqual(r[1],100+12.580361429288132-h)
        self.assertEqual((r[2]-r[0],r[3]-r[1]),(25,15))
        leader=next(e for e in g.ents if isinstance(e,Line) and e.note=='annotation_leader')
        self.assertEqual(leader.p1,(100+h,100))
        self.assertEqual(leader.p2,(r[0],r[1]))
        cd.line((100,50),(100,160),'PIPE_PRI')
        self.assertFalse(E.detect_pipe_over_stream_frames(cd))

    def test_frame_check_catches_left_border_and_empty_cell_crossing(self):
        for placement in ('top','right'):
            for border in (True,False):
                cd=E.ChemDrawing()
                g=S.add_stream_data(cd,(100,100),placement=placement)
                x,y,x2,y2=E.stream_table_rect(g)
                if border:
                    cd.line((x,y-10),(x,y2+10),'PIPE_PRI')
                else:
                    cd.line((x-10,y+5),(x2+10,y+5),'PIPE_INLINE')
                self.assertTrue(E.detect_pipe_over_stream_frames(cd))
                self.assertTrue(E.validate_pipe_over_stream_frames(cd))
                self.assertGreater(L.detect_collisions(cd)['counts']['pipe_over_stream_frame'],0)

    def test_frame_check_uses_translated_geometry_and_boundary_clearance(self):
        cd=E.ChemDrawing()
        g=S.add_stream_data(cd,(300,200),placement='right')
        x,y,x2,y2=E.stream_table_rect(g)
        cd.line((x-.1,y-1),(x-.1,y2+1),'PIPE_PRI')
        self.assertTrue(E.detect_pipe_over_stream_frames(cd))

    def test_vertical_pipeline_build_selects_side_frame(self):
        spec={'drawing_type':'pfd','sheet':'A1','equipment':[],
              'pipes':[{'id':'S','from':'100,100','to':'100,240','stream_no':'0124'}]}
        cd,_=P.build(spec)
        groups=[g for g in cd.iter_groups() if g.name=='STREAM_DATA']
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0].meta['stream_placement'],'right')
        self.assertFalse(E.detect_pipe_over_stream_frames(cd))

    def test_main_stream_crossing_keeps_horizontal_and_breaks_vertical(self):
        cd=E.ChemDrawing()
        horizontal=cd.line((-10,0),(10,0),'PIPE_PRI')
        cd.line((0,10),(0,-10),'PIPE_PRI')
        self.assertEqual(len(E.visible_pipe_crossings(cd)),1)
        self.assertTrue(E.validate_pipe_crossings(cd))
        self.assertEqual(E.apply_crossing_breaks(cd),1)
        self.assertIn(horizontal,cd.items)
        vertical=[e for e in cd.items if isinstance(e,Line) and e is not horizontal]
        self.assertEqual([(e.p1,e.p2) for e in vertical], [((0.,10.),(0.,2.)),((0.,-2.),(0.,-10.))])
        self.assertFalse(E.visible_pipe_crossings(cd))
        self.assertFalse(E.validate_pipe_crossings(cd))
        self.assertEqual(E.apply_crossing_breaks(cd),0)
        self.assertEqual(cd.meta['crossing_breaks'],1)

    def test_crossing_orientation_overrides_layer_priority(self):
        cd=E.ChemDrawing()
        horizontal=cd.line((-10,0),(10,0),'PIPE_INLINE')
        cd.line((0,-10),(0,10),'PIPE_PRI')
        self.assertEqual(E.apply_crossing_breaks(cd),1)
        self.assertIn(horizontal,cd.items)
        self.assertFalse(E.visible_pipe_crossings(cd))

    def test_crossing_preserves_declared_junction_and_t_endpoint(self):
        cd=E.ChemDrawing()
        cd.pipe('A','-10,0','10,0')
        cd.pipe('B','A@0.5','0,10')
        cd.line((0,-10),(0,10),'PIPE_PRI')
        self.assertEqual(E.apply_crossing_breaks(cd),0)
        self.assertEqual(cd.resolve('A@0.5'),(0.,0.))
        other=E.ChemDrawing()
        other.line((-10,0),(10,0),'PIPE_PRI')
        other.line((0,0),(0,10),'PIPE_PRI')
        self.assertEqual(E.apply_crossing_breaks(other),0)

    def test_crossing_rejects_invalid_gap_short_stub_and_self_cross(self):
        for gap in (0,-1,float('nan')):
            with self.assertRaises(ValueError):
                E.apply_crossing_breaks(E.ChemDrawing(),gap=gap)
        cd=E.ChemDrawing()
        cd.line((-10,0),(10,0),'PIPE_PRI')
        cd.line((0,-1),(0,10),'PIPE_PRI')
        with self.assertRaises(ValueError):
            E.apply_crossing_breaks(cd)
        cd=E.ChemDrawing()
        cd.pipes['A']=E.PipeRec('A',[(-10,0),(10,0),(10,10),(0,10),(0,-10)])
        with self.assertRaises(ValueError):
            E.apply_crossing_breaks(cd)

    def test_adjacent_crossing_gaps_merge_without_detached_sliver(self):
        cd=E.ChemDrawing()
        for y in (0,1):
            cd.line((-10,y),(10,y),'PIPE_PRI')
        cd.line((0,-10),(0,10),'PIPE_PRI')
        self.assertEqual(E.apply_crossing_breaks(cd),2)
        vertical=[e for e in cd.items if isinstance(e,Line) and e.p1[0]==e.p2[0]]
        self.assertEqual([(e.p1,e.p2) for e in vertical],[((0.,-10.),(0.,-2.)),((0.,3.),(0.,10.))])

    def test_generated_text_bounds_use_written_width_factor(self):
        r=E.text_rect(Text(layer='TAG',text='ABC',pt=(0,0),height=3))
        self.assertAlmostEqual(r[2]-r[0],3*3*.55*.7)
    def test_centrifugal_pump_ports_match_user_connection(self):
        cd = E.ChemDrawing()
        group = BL.place(cd,{'id':'P','type':'pump.centrifugal'},(0,0))
        self.assertAlmostEqual(cd.resolve('P.inlet')[0],-15.628656262921169)
        self.assertAlmostEqual(cd.resolve('P.inlet')[1],-10.198217384732345)
        self.assertAlmostEqual(cd.resolve('P.outlet')[0],12.748656262923731)
        self.assertAlmostEqual(cd.resolve('P.outlet')[1],22.49942943159155)
        self.assertEqual(cd.anchor_dir_of('P.outlet'),(0.,1.))
        cd.pipe('IN','-50,-10.198217384732345','P.inlet')
        cd.pipe('OUT','P.outlet','12.748656262923731,50')
        self.assertFalse(E.validate_pipe_connections(cd))
        cd.pipes['IN'].path[-1] = (-20,0)
        self.assertTrue(E.validate_pipe_connections(cd))

    def test_section_title_over_stream_cells_is_rejected(self):
        cd=E.ChemDrawing()
        group=S.add_stream_data(cd,(100,100),fields=['W 150 kg/h','T 30 ℃','P 3 bar'])
        first=next(e for e in group.ents if isinstance(e,Text))
        cd.text('进料与预处理工段（S1）',first.pt,'SECT',3,align='middle_center')
        self.assertTrue(E.validate_text_overlap(cd))

    def test_pfd_has_no_detached_section_corner_lines(self):
        spec=P.spec_for_type({'section_frames':True},'pfd')
        self.assertFalse(spec['section_frames'])

    def test_all_available_library_blocks_keep_entity_types(self):
        import chemcad_catalog as C
        from collections import Counter
        cd = E.ChemDrawing()
        expected = {}
        for index, symbol in enumerate(C.symbols()):
            if symbol.get('status') == 'pending_block':
                continue
            eq = {'id':str(index),'type':symbol['id']}
            group = BL.place(cd,eq,(index*100,100))
            self.assertTrue(group.meta['native_geometry'])
            _,name,source,_ = BL.source(eq)
            expected[name] = Counter(e.dxftype() for e in source)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'all-library.dxf'
            E.write_dxf(cd,str(path))
            doc = ezdxf.readfile(path)
            for name, counts in expected.items():
                self.assertEqual(Counter(e.dxftype() for e in doc.blocks[name]),counts,name)

    def test_nested_native_block_refuses_missing_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'nested.dxf'
            doc = ezdxf.new('R2010')
            doc.blocks.new('INNER').add_line((0,0),(10,0),dxfattribs={'layer':'设备'})
            doc.blocks.new('OUTER').add_blockref('INNER',(0,0),dxfattribs={'layer':'设备'})
            doc.blocks['OUTER'].add_line((0,1),(10,1),dxfattribs={'layer':'设备'})
            doc.saveas(path)
            cd = E.ChemDrawing()
            BL.place(cd,{'id':'X','block_file':str(path),'block_name':'OUTER'},(0,0))
            with self.assertRaisesRegex(RuntimeError,'嵌套块引用'):
                E.write_dxf(cd,str(Path(tmp)/'out.dxf'))

    def test_retained_library_ellipses_are_native(self):
        cd = E.ChemDrawing()
        group = BL.place(cd, {'id':'R','type':'reactor.stirred'}, (100,100))
        self.assertTrue(group.meta['native_geometry'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'reactor.dxf'
            E.write_dxf(cd,str(path))
            doc = ezdxf.readfile(path)
            self.assertEqual(len(doc.blocks['reactor.stirred'].query('ELLIPSE')), 2)
            self.assertEqual({e.dxf.layer for e in doc.blocks['reactor.stirred']},{'EQUIP','EQUIP_IN'})

    def test_dwg_default_save_format_is_2018(self):
        from unittest.mock import MagicMock
        import types
        doc = MagicMock()
        app = MagicMock()
        app.Documents.Open.return_value = doc
        dynamic = types.SimpleNamespace(Dispatch=lambda obj: doc)
        modules = {'win32com': types.ModuleType('win32com'),
                   'win32com.client': types.ModuleType('win32com.client'),
                   'win32com.client.dynamic': dynamic}
        with patch.dict('sys.modules', modules), patch.object(DWG,'com_session',return_value=(app,'running')), \
                patch.object(DWG.os.path,'isfile',return_value=True), patch.object(DWG.os.path,'getsize',return_value=2048):
            result=DWG.dxf_to_dwg_com('in.dxf','out.dwg')
        self.assertTrue(result['ok'])
        self.assertEqual(doc.SaveAs.call_args.args[1],64)

    def test_screenshot_colors_written_and_distinct_blocks(self):
        cd=E.ChemDrawing()
        S.add_stream_data(cd,(100,100),'0117',['W 150 kg/h','T 30 ℃','P 3 bar'])
        second=S.add_stream_data(cd,(200,100),'0118',['W 150 kg/h','T 30 ℃','P 3 bar'])
        second.ents[0].color=3
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'colors.dxf'
            E.write_dxf(cd,str(file))
            doc=ezdxf.readfile(file)
            blocks=[b for b in doc.blocks if b.name.startswith('STREAM_DATA')]
            self.assertEqual(len(blocks),2)
            self.assertTrue(all(e.dxf.color==7 for e in blocks[0]))
            self.assertEqual(blocks[1][0].dxf.color,3)
            diamonds=list(doc.modelspace().query('LWPOLYLINE'))
            self.assertEqual([e.dxf.color for e in diamonds],[2,2])

    def test_screenshot_geometry_and_units(self):
        cd=E.ChemDrawing()
        group=S.add_stream_data(cd,(100,100),'0117',['W 150 kg/h','T 30 ℃','P 3 bar'])
        self.assertEqual([e.text for e in group.ents if isinstance(e,Text)],
                         ['F kg/h','150.00','T ℃','30.00','P bar','3.00'])
        border=next(e for e in group.ents if isinstance(e,Polyline))
        self.assertEqual(min(p[0] for p in border.pts),100)
        self.assertEqual(max(p[0] for p in border.pts),125)
        leader=next(e for e in group.ents if isinstance(e,Line) and e.note=='annotation_leader')
        self.assertEqual(leader.p1[0],leader.p2[0])
        self.assertEqual(leader.p2[1],min(p[1] for p in border.pts))
        self.assertAlmostEqual(leader.p2[1]-leader.p1[1],5.509293617425101)
        number=next(e for e in cd.iter_ents() if isinstance(e,Text) and e.text=='0117')
        self.assertEqual(number.pt,(100,100))
        self.assertEqual(number.note,'stream_no_fixed')
        self.assertEqual(number.color,7)
        self.assertTrue(all(e.color==7 for e in group.ents))
        dims=json.loads((Path(__file__).resolve().parents[1]/'assets/stream-annotation-dimensions.json').read_text(encoding='utf8'))
        self.assertAlmostEqual(S.STREAMNO_DIAMOND_HALF,dims['diamond_half_diagonal_mm'])
        self.assertAlmostEqual(leader.p2[1]-leader.p1[1],dims['leader_exposed_length_mm'])

    def test_screenshot_no_guessed_units_or_flow_conversion(self):
        self.assertEqual(S.stream_annotation_rows({'F':'3 kmol/h','T':0,'P':'1.2 bar(a)'}),
                         [('F kmol/h','3.00'),('T','0.00'),('P bar(a)','1.20')])
        self.assertEqual(S.stream_annotation_rows({'W':'12 500 kg/h','T':'-2 ℃','P':'1e-2 bar'}),
                         [('F kg/h','12500.00'),('T ℃','-2.00'),('P bar','0.01')])
        with self.assertRaises(ValueError):
            S.add_stream_data(E.ChemDrawing(),(0,0),up=False)
        spec={'std_tables':False,'pipes':[{'id':'L','from':'100,100','to':'200,100','stream_no':'1'}],
              'stream_diamond':False}
        with self.assertRaisesRegex(ValueError,'截图样式'):
            P.build(P.spec_for_type(spec,'pfd'))

    def test_score_recognizes_external_material_frame(self):
        cd=E.ChemDrawing()
        cd.meta['external_balance_panels']=[{'streams':['1','2']}]
        result=SCORE.chk_stream_table(cd,{'item':SimpleNamespace(code='2.2.1',points=0.2)})
        self.assertIn('共 2 个流股号',result.evidence)

    def test_score_does_not_require_legend_for_library_equipment(self):
        cd=E.ChemDrawing()
        BL.place(cd,{'id':'P1','type':'pump.centrifugal'},(100,100))
        result=SCORE.chk_legend(cd,{'item':SimpleNamespace(code='1.3',points=0.2)})
        self.assertIn('不生成图例框',result.evidence)
        self.assertFalse(result.hint)
    def test_both_shipped_examples_build_with_default_frame(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('pfd-spec-example','pfd-spec-autolayout-example'):
            with self.subTest(example=name), redirect_stdout(io.StringIO()):
                spec=json.loads((root/'assets'/f'{name}.json').read_text(encoding='utf8'))
                cd,_=P.build_pfd(spec)
                self.assertEqual(E.preflight(cd)['errors'],[])
                self.assertEqual(len(cd.meta['external_balance_panels']),1)
                self.assertEqual(len(cd.meta['text_frames']),2)
                self.assertEqual([f['at'][1] for f in cd.meta['text_frames']],[0,0])
                self.assertEqual(cd.meta['text_frames'][1]['at'][0]-841,150)
                self.assertEqual(len(cd.meta['external_balance_panels'][0]['streams']),len(spec['streams']))

    def test_first_sheet_gets_full_repair_rounds(self):
        cd = E.ChemDrawing()
        with patch.object(P,'_build_rounds',return_value=(cd,[],{'mode':'auto'})) as build:
            P.build_with_layout({'layout':{'mode':'auto','sheet':'auto'}},max_rounds=4)
        self.assertEqual([call.args[1] for call in build.call_args_list],[0,4])

    def test_external_tables_and_no_legend_even_legacy_flags(self):
        spec = {'sheet':'A1','equipment':[], 'streams':{
            'FEED':{'T':'40 ℃','P':'1 bar','W':'20 kg/h','phase':'液'},
            'OUT':{'no':'2','T':'80 ℃','W':'18 kg/h'}},
            'legend':True,'legend_at':[100,100], 'stream_table_at':[100,100]}
        cd = E.ChemDrawing(sheet='A1')
        P._draw_std_tables(cd, 'pfd', None, {}, spec)
        panels = cd.meta['external_balance_panels']
        self.assertEqual(len(panels),1)
        self.assertGreater(panels[0]['rect'][0],841)
        self.assertEqual(panels[0]['rect'][0]-841,150)
        self.assertEqual(panels[0]['at'][1],0)
        self.assertEqual(panels[0]['rect'][3],594)
        self.assertEqual(panels[0]['streams'],['FEED','2'])
        self.assertNotIn('legend',cd.meta)
        texts = [e for e in cd.iter_ents() if isinstance(e,Text)]
        self.assertTrue(all(t.pt[0] > 841 for t in texts))
        self.assertIn('液',[t.text for t in texts])
        L.polish_texts(cd)
        self.assertTrue(all(t.pt[0] > 841 for t in texts))

    def test_one_external_frame_contains_all_tables(self):
        streams = {str(i):{'no':str(i),'W':i} for i in range(28)}
        spec = {'streams':streams,'section_box_max_rows':10}
        cd = E.ChemDrawing(sheet='A1')
        P._draw_std_tables(cd,'pfd',None,{'S1':[streams[str(i)] for i in range(12)]},spec)
        panels = cd.meta['external_balance_panels']
        self.assertEqual(len(panels),1)
        self.assertEqual([p['at'][1] for p in panels],[0])
        self.assertEqual(len(panels[0]['tables']),4)
        ids = [s for p in panels for s in p['streams']]
        self.assertEqual(set(ids),set(streams))
        self.assertEqual(len(ids),28)
        tables=panels[0]['tables']
        self.assertTrue(all(b['rect'][3]<a['rect'][1] for a,b in zip(tables,tables[1:])))

    def test_frame_capacity_stops_without_continuation(self):
        cd=E.ChemDrawing(sheet='A1')
        spec={'streams':{str(i):{'no':str(i),'W':i} for i in range(250)}}
        with self.assertRaisesRegex(ValueError,'禁止续框'):
            P._draw_std_tables(cd,'pfd',None,{},spec)
        self.assertNotIn('external_balance_panels',cd.meta)
        self.assertFalse(list(cd.iter_groups()))

    def test_default_frame_preserves_native_entities(self):
        cd=E.ChemDrawing(sheet='A1')
        frame=S.add_user_text_frame(cd)
        profile=S.text_frame_profile()
        self.assertEqual(profile['size'],[841,594])
        self.assertEqual(frame.kind,'frame')
        self.assertFalse(E.detect_symbol_overlaps(cd))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'frame.dxf'
            E.write_dxf(cd,str(path),dxf_version='R2010')
            doc=ezdxf.readfile(path)
            actual=doc.blocks[profile['block']]
            source=ezdxf.readfile(frame.meta['library_file']).blocks[profile['block']]
            self.assertEqual([e.dxftype() for e in actual],[e.dxftype() for e in source])
            self.assertEqual(len(actual.query('POINT')),60)
            self.assertEqual(len(actual.query('MTEXT')),55)
            self.assertEqual(doc.styles.get('工程字').dxf.font,'gbenor.shx')
            self.assertFalse(any(e.plain_text()=='图例' for e in actual.query('MTEXT')))

    def test_default_frame_retains_title_field_inputs(self):
        cd=E.ChemDrawing()
        fields={'项目名称':'装置甲','名称':'精馏工段','设计':'张工','制图':'李工',
                '日期':'2026-10-04','图号':'PFD-01','版次':'B','专业名称':'工艺'}
        S.add_user_text_frame(cd,fields=fields,total=2)
        texts=[t.text for t in cd.iter_ents() if isinstance(t,Text)]
        for value in ('装置甲','精馏工段','张工','李工','2026-10-04','PFD-01  B','工艺'):
            self.assertIn(value,texts)
        self.assertEqual(cd.meta['title_block']['共几张'],'2')

    def test_sample_balance_table_orientation(self):
        cd = E.ChemDrawing()
        S.add_balance_table(cd,(100,200),[{'no':'1','T':'40 ℃','P':'1 bar','F':'3 kmol/h'},
                                        {'no':'2','T':'80 ℃','P':'2 bar','F':'4 kmol/h'}])
        texts = [t for t in cd.iter_ents() if isinstance(t,Text)]
        one = next(t for t in texts if t.text=='1')
        two = next(t for t in texts if t.text=='2')
        self.assertEqual(one.pt[1],two.pt[1])
        self.assertLess(one.pt[0],two.pt[0])
        self.assertTrue(all(t.height==3 for t in texts if t.text!='物料平衡表'))
        self.assertIn('3 kmol/h',[t.text for t in texts])

    def test_book_spacing_includes_external_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(2):
                doc=ezdxf.new('R2010')
                doc.modelspace().add_lwpolyline([(0,0),(1400,0),(1400,594),(0,594)],close=True)
                path=Path(tmp)/f'{i}.dxf'
                doc.saveas(path)
                paths.append(str(path))
            result=BOOK.book_dxf(paths,str(Path(tmp)/'book.dxf'),cols=2)
        self.assertGreaterEqual(result['pitch'][0],1550)
        self.assertGreaterEqual(result['placed'][1]['slot'][0],result['placed'][0]['w']+150)

    def test_three_fields_and_mass_flow_priority(self):
        self.assertEqual(P._stream_fields({'T':'40 ℃','P':'1.2 bar','W':'100 kg/h',
            'F':'8 kmol/h','V':'2 m³/h','phase':'液','rho':'850','wf':'0.5','custom':'禁止出现'}),
            ['T 40 ℃','P 1.2 bar','W 100 kg/h'])

    def test_flow_fallback_and_zero(self):
        for fields, expected in [({'F':'3 kmol/h'}, 'F 3 kmol/h'),
                                 ({'V':'2 m³/h'}, 'V 2 m³/h'), ({'W':0}, 'W 0')]:
            self.assertEqual(S.stream_parameter_fields(fields), ['T ', 'P ', expected])

    def test_free_text_filters_and_missing_values(self):
        self.assertEqual(S.stream_parameter_fields(['phase 液','T 40 ℃','P 1 bar','W 20 kg/h','rho 800']),
                         ['T 40 ℃','P 1 bar','W 20 kg/h'])
        self.assertEqual(S.stream_parameter_fields(None), ['T ', 'P ', 'W '])

    def test_frame_is_three_rows(self):
        cd = E.ChemDrawing()
        S.add_stream_data(cd, (100,100), '0001', ['T 40 ℃','P 1 bar','W 20 kg/h','phase 液'])
        group = list(cd.iter_groups())[0]
        texts = [t.text for t in group.ents if isinstance(t, Text)]
        self.assertEqual(texts, ['F kg/h','20.00','T ℃','40.00','P bar','1.00'])
        border = next(e for e in group.ents if isinstance(e, Polyline))
        self.assertAlmostEqual(max(p[1] for p in border.pts) - min(p[1] for p in border.pts), 15)

    def test_fraction_bar_clearance(self):
        cd = E.ChemDrawing()
        group = S.add_equipment_tag(cd, (0,0), 'T-101', '脱轻塔')
        texts = [t for t in group.ents if isinstance(t, Text)]
        bar = next(e for e in group.ents if isinstance(e, Line))
        self.assertEqual([t.height for t in texts], [3,3])
        self.assertTrue(E.text_rect(texts[1])[3] < bar.p1[1] < E.text_rect(texts[0])[1])

    def test_equipment_labels_outside_actual_body(self):
        cd = E.ChemDrawing()
        rect = (-20,-100,20,100)
        groups = S.add_equipment_labels(cd, {'tag':'T-101','name':'长塔','tag_annot':'both'}, rect, True)
        self.assertEqual(len(groups),1)
        self.assertLess(E.group_rect(groups[0])[3], rect[1])
        self.assertEqual(groups[0].base,(0,-110))

    def test_measurement_preserves_geometry_and_anchors(self):
        eq = {'id':'P-101','tag':'P-101','type':'pump.centrifugal'}
        cd = E.ChemDrawing()
        group = BL.place(cd, eq, (0,0), tag='P-101')
        box = L.measure_equipment({'equipment':[eq]})['P-101']
        self.assertEqual(box.body, E.group_rect(group))
        self.assertEqual(box.anchors, group.meta['anchors'])
        self.assertGreater(box.h, box.body[3] - box.body[1])

    def test_vertical_diamond_on_line_and_gap(self):
        spec = {'sheet':'A1','equipment':[], 'std_tables':False, 'auto_fit':False,
                'pipes':[{'id':'L1','from':'100,100','to':'100,200','arrows':1,
                          'stream':{'no':'7','T':'40 ℃','P':'1 bar','W':'20 kg/h','phase':'液'}}]}
        cd, _ = P.build(P.spec_for_type(spec, 'pfd'))
        self.assertEqual(cd.meta['stream_diamond_pts'][0], (100,150))
        self.assertEqual(cd.meta['stream_diamond_breaks'], 1)
        self.assertEqual(len(cd.pipes['L1'].arrows), 1)
        self.assertGreaterEqual(math.dist(cd.pipes['L1'].arrows[0][0], (100,150)), S.STREAMNO_DIAMOND_HALF+4)

    def test_corner_marker_moves_to_straight_segment(self):
        rec = E.PipeRec('L', [(0,0),(10,0),(10,10)])
        pt = rec.straight_point_at(0.5)
        self.assertTrue((pt[1] == 0 and 4.5 <= pt[0] <= 5.5) or
                        (pt[0] == 10 and 4.5 <= pt[1] <= 5.5))

    def test_short_line_can_shift_marker_for_arrow(self):
        spec = {'equipment':[], 'std_tables':False, 'auto_fit':False,
                'pipes':[{'id':'L1','from':'100,100','to':'132,100','arrows':1,'stream_no':'1'}]}
        cd, _ = P.build(P.spec_for_type(spec, 'pfd'))
        self.assertEqual(len(cd.pipes['L1'].arrows), 1)
        self.assertFalse(cd.meta.get('annotation_issues'))

    def test_impossible_marker_rejected(self):
        with self.assertRaisesRegex(ValueError, '足够长'):
            E.PipeRec('tiny', [(0,0),(5,0)]).straight_point_at(0.5)

    def test_multiple_arrows_have_clearance(self):
        rec = E.PipeRec('L', [(0,0),(100,0)])
        arrows = E.ChemDrawing._auto_arrows(rec, 4, [(50,0)], 8)
        self.assertEqual(len(arrows), 4)
        for i, (pt, direction) in enumerate(arrows):
            self.assertEqual(direction, 'right')
            self.assertGreaterEqual(math.dist(pt,(50,0)),8)
            for other, _ in arrows[i+1:]:
                self.assertGreaterEqual(math.dist(pt,other),8)

    def test_direct_pipe_emits_arrow(self):
        cd = E.ChemDrawing()
        cd.pipe('L', '100,100', '200,100', arrows=1)
        self.assertEqual(len(cd.pipes['L'].arrows),1)
        self.assertTrue(any(isinstance(e, (Solid,Polyline)) and e.layer=='FLOWARROW' for e in cd.iter_ents()))

    def test_unit_aware_table_measurement(self):
        rows = [{'no':'1','T':'40 ℃','P':'1.2 bar(a)','W':'20 kg/h','phase':'液'}]
        self.assertEqual(next(h for h,f,w in S.stream_table_columns(rows) if f=='P'), '压力')
        cd = E.ChemDrawing()
        size = S.add_section_stream_box(cd,(100,100),'物流',rows)
        self.assertEqual(size, S.stream_table_size(rows))
        self.assertIn('1.2 bar(a)', [t.text for t in cd.iter_ents() if isinstance(t,Text)])

    def test_polish_keeps_cells_together(self):
        cd = E.ChemDrawing()
        S.add_stream_data(cd,(100,100),fields=['T 40 ℃','P 1 bar','W 20 kg/h'])
        group = list(cd.iter_groups())[0]
        texts = [t for t in group.ents if isinstance(t,Text)]
        before = [(t.pt[0]-texts[0].pt[0], t.pt[1]-texts[0].pt[1]) for t in texts]
        leader = next(e for e in group.ents if isinstance(e,Line) and e.note=='annotation_leader')
        cd.line((100,120.0710678),(160,120.0710678),'PIPE_PRI')
        self.assertTrue(E.detect_text_over_lines(cd))
        L.polish_texts(cd)
        self.assertEqual(before, [(t.pt[0]-texts[0].pt[0], t.pt[1]-texts[0].pt[1]) for t in texts])
        self.assertEqual(leader.p1,(100,100+S.STREAMNO_DIAMOND_HALF))
        self.assertEqual(leader.p2[0],leader.p1[0])
        self.assertGreater(leader.p2[1],leader.p1[1])
        # 固定尺寸后不再靠伸长引线消除碰撞；由完整布线入口重新选位。
        self.assertTrue(E.detect_text_over_lines(cd))

    def test_generated_block_fonts_and_native_curves(self):
        cd = E.ChemDrawing()
        eq = {'id':'T1','type':'column.tray','tag':'T-101'}
        group = BL.place(cd,eq,(200,200),tag='T-101')
        S.add_equipment_tag(cd,(100,100),'T-101','脱轻塔')
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'actual.dxf'
            E.write_dxf(cd,str(file),dxf_version='R2010')
            doc = ezdxf.readfile(file)
            annotation = [b for b in doc.blocks if b.name.startswith('EQUIPMENT_LABEL')][0]
            for text in annotation.query('TEXT'):
                self.assertEqual(text.dxf.style,'3-gbcbig')
                self.assertEqual(text.dxf.height,3)
                self.assertEqual(text.dxf.width,0.7)
            native = doc.blocks[group.meta['library_block']]
            _, _, source, _ = BL.source(eq)
            self.assertEqual([e.dxftype() for e in native], [e.dxftype() for e in source])
            self.assertEqual(doc.styles.get('3-gbcbig').dxf.height,0)

    def test_dwg_report_is_json_serializable(self):
        with patch.object(DWG, '_allow_accore', return_value=False), \
             patch.object(DWG, 'dxf_to_dwg_com', return_value={'ok':True,'backend':'com','bytes':1234}):
            result = DWG.to_dwg('input.dxf','output.dwg')
        self.assertEqual(json.loads(json.dumps(result))['tried'][0]['backend'],'com')

    def test_connectors_share_three_parameter_rule(self):
        for kind in ('cross','same','plant'):
            with self.subTest(kind=kind):
                cd = E.ChemDrawing()
                S.add_offpage_connector(cd,(100,100),'right','PFD-02',kind=kind,
                    dest='去T-101',stream_no='0101',stream_data={'T':'40 ℃','P':'1 bar','W':'20 kg/h','phase':'液','rho':'800'})
                texts = [e.text for e in cd.iter_ents() if isinstance(e,Text)]
                self.assertFalse(any('液' in t or 'rho' in t or '800' in t for t in texts))
                params = next(g for g in cd.iter_groups() if g.name=='STREAM_DATA')
                borders = [e for e in params.ents if isinstance(e,Polyline) and e.closed]
                self.assertEqual(len(borders),1)
                self.assertAlmostEqual(max(p[1] for p in borders[0].pts)-min(p[1] for p in borders[0].pts),15)
                self.assertEqual([e.text for e in params.ents if isinstance(e,Text)],
                                 ['F kg/h','20.00','T ℃','40.00','P bar','1.00'])
                leader=next(e for e in params.ents if isinstance(e,Line) and e.note=='annotation_leader')
                self.assertEqual(leader.p1[0],leader.p2[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
