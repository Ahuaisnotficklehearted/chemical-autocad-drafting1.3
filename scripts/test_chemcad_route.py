"""流股避障回归：几何碰撞、失败传播与现有出图入口。"""
import unittest
import heapq
import math
import random
from types import SimpleNamespace
from unittest.mock import patch
import chemcad_route as R
import chemcad_engine as E
import draw_pfd as P

def device(rect, name="W"):
    return R.Obstacle(rect,R.KIND_EQUIP,name,hard=True)

def drawing(start=(0.,0.),end=(100.,0.)):
    return SimpleNamespace(resolve=lambda ref: start if ref.startswith("A.") else end,
                           anchor_dir_of=lambda ref: None)

SPEC={"equipment":[{"id":"A"},{"id":"B"}],
      "pipes":[{"id":"L","from":"A.out","to":"B.in"}]}

def routed(obs, opts=None):
    with patch.object(R,"collect_obstacles",return_value=obs), patch.object(R,"_content_box",return_value=None):
        return R.route_pipes(drawing(),SPEC,opts=opts)["L"]

class RoutingTests(unittest.TestCase):
    def assert_safe(self,res,obstacles):
        self.assertTrue(res.ok,res.note)
        self.assertTrue(res.path)
        for a,b in zip(res.path,res.path[1:]):
            self.assertTrue(a[0]==b[0] or a[1]==b[1],(a,b))
            for ob in obstacles:
                self.assertLessEqual(R._seg_len_in(a,b,ob.rect),1e-9,(res.path,ob.rect))

    def test_expand_region_before_clearance_relaxation(self):
        obs=[device((40.,-180.,60.,180.))]
        res=routed(obs)
        self.assert_safe(res,obs)
        self.assertEqual(res.relaxed,0)

    def test_mandatory_stub_cannot_jump_thin_equipment(self):
        obs=[device((2.,-2.,3.,2.))]
        res=R.solve((0.,0.),(100.,0.),obs,start_dir=(1.,0.))
        self.assert_safe(res,obs)
        self.assertGreater(res.path[1][0],0)

    def test_blocked_owner_exit_is_failure(self):
        obs=[device((0.,-10.,20.,10.),"A"),device((21.,-10.,24.,10.),"W")]
        res=R.solve((10.,0.),(100.,0.),obs,start_dir=(1.,0.),carve_for=("A","B"))
        self.assertFalse(res.ok)
        self.assertFalse(res.path)

    def test_sealed_goal_not_downgraded_to_soft(self):
        walls=[(60.,-30.,140.,-20.),(60.,20.,140.,30.),(60.,-30.,70.,30.),(130.,-30.,140.,30.)]
        res=routed([device(r,str(i)) for i,r in enumerate(walls)])
        self.assertFalse(res.ok)
        self.assertEqual(res.path,[])
        self.assertTrue(res.note)

    def test_unsolved_audit_cannot_be_clean(self):
        cd=E.ChemDrawing()
        audit=R.audit_routes(cd,{"L":R.RouteResult("L",ok=False)})
        self.assertFalse(audit["clean"])
        self.assertNotIn("✓",R.format_audit(audit))

    def test_options_not_mutated_across_drawings(self):
        opts=R.RouteOptions()
        with patch.object(R,"collect_obstacles",return_value=[]), patch.object(R,"_content_box",return_value=(0,0,100,100)):
            R.route_pipes(drawing(),SPEC,opts=opts)
        self.assertIsNone(opts.sheet_bounds)

    def test_drawer_rejects_failed_automatic_route(self):
        cd=SimpleNamespace(pipe=lambda *args,**kw: None)
        with self.assertRaisesRegex(ValueError,"L"):
            P._route_pipes({"L":R.RouteResult("L",ok=False,note="封闭")},cd,"pfd",set(),{}, {},SPEC,{})

    def test_full_content_region_beyond_fixed_pad(self):
        obs=[device((40.,-350.,60.,350.))]
        opts=R.RouteOptions(sheet_bounds=(-300,-450,400,450))
        res=routed(obs,opts)
        self.assert_safe(res,obs)
        self.assertEqual(res.relaxed,0)

    def test_zero_length_inside_equipment_is_not_success(self):
        res=R.solve((0.,0.),(0.,0.),[device((-5.,-5.,5.,5.))])
        self.assertFalse(res.ok)

    def test_hard_soft_downgrade_is_rejected(self):
        with self.assertRaises(ValueError):
            R.solve((0.,0.),(100.,0.),[],relax=2)

    def test_four_stub_directions_avoid_thin_obstacles(self):
        for d,wall,goal in [((1.,0.),(2.,-2.,3.,2.),(100.,0.)),
                            ((-1.,0.),(-3.,-2.,-2.,2.),(-100.,0.)),
                            ((0.,1.),(-2.,2.,2.,3.),(0.,100.)),
                            ((0.,-1.),(-2.,-3.,2.,-2.),(0.,-100.))]:
            with self.subTest(direction=d):
                obs=[device(wall)]
                self.assert_safe(R.solve((0.,0.),goal,obs,start_dir=d),obs)

    def test_embedded_owner_anchor_exits_before_search(self):
        opts=R.RouteOptions()
        body=device((91.4,224.6,140.7,281.9),"A")
        q=R._stub_point((99.,254.3),(-1.,0.),[body],opts,9.,owner="A")
        self.assertLess(q[0],body.rect[0]-opts.hard_clear)

    def test_fixed_text_not_evicted_from_stub(self):
        ob=R.Obstacle((3.,-1.,5.,1.),R.KIND_TEXT,"learned",hard=True)
        res=R.solve((0.,0.),(100.,0.),[ob],start_dir=(1.,0.))
        self.assertTrue(res.ok,res.note)
        self.assertFalse(res.evicted)
        self.assertTrue(all(R._seg_len_in(a,b,ob.rect)<1e-9 for a,b in zip(res.path,res.path[1:])))

    def test_movable_text_reports_stub_eviction(self):
        ob=R.Obstacle((3.,-1.,5.,1.),R.KIND_TEXT,"label",hard=True,movable=True)
        res=R.solve((0.,0.),(100.,0.),[ob],start_dir=(1.,0.))
        self.assertTrue(res.ok,res.note)
        self.assertEqual(res.evicted,["label"])

    def test_zero_clearance_grid_can_find_offset_narrow_passage(self):
        obs=[device((40,-10,60,.2)),device((40,.5,60,10))]
        opts=R.RouteOptions(sheet_bounds=(-10,-2,110,2),sheet_margin=0)
        strict=R.solve((0.,0.),(100.,0.),obs,opts)
        relaxed=R.solve((0.,0.),(100.,0.),obs,opts,relax=1)
        self.assertFalse(strict.ok)
        self.assert_safe(relaxed,obs)
        self.assertEqual(relaxed.relaxed,1)

    def test_search_after_stub_never_reenters_owner(self):
        opts=R.RouteOptions(stub=1.)
        body=device((-20.,-20.,20.,20.),"A")
        res=R.solve((19.,0.),(0.,50.),[body],opts,start_dir=(1.,0.),carve_for=("A","B"))
        self.assertTrue(res.ok,res.note)
        for a,b in zip(res.path[1:],res.path[2:]):
            self.assertLessEqual(R._seg_len_in(a,b,body.rect),1e-9)

    def test_actual_parameter_frame_and_number_are_not_movable(self):
        import chemcad_symbols as S
        cd=E.ChemDrawing()
        S.add_stream_data(cd,(0,0),"0117",["W 150 kg/h","T 30 ℃","P 3 bar"])
        texts=[ob for ob in R.collect_obstacles(cd) if ob.kind==R.KIND_TEXT]
        self.assertEqual(len(texts),7)
        self.assertTrue(all(not ob.movable for ob in texts))

    def test_previous_annotation_position_can_expire_at_stub(self):
        fake=drawing()
        fake.anchor_dir_of=lambda ref:(1.,0.) if ref.startswith("A.") else None
        with patch.object(R,"collect_obstacles",return_value=[]),patch.object(R,"_content_box",return_value=None):
            res=R.route_pipes(fake,SPEC,extra_hard=[(3.,-1.,5.,1.)])["L"]
        self.assertTrue(res.ok,res.note)
        self.assertEqual(res.evicted,["learned"])

    def test_unexpected_router_exception_is_not_swallowed(self):
        with patch.object(R,"route_pipes",side_effect=RuntimeError("算法异常")):
            with self.assertRaisesRegex(RuntimeError,"算法异常"):
                P.build(P.spec_for_type({"std_tables":False,"pipes":[]},"pfd"))

    def test_directional_bend_lower_bound_cases(self):
        g=(0.,0.)
        for p,d,expected in [((1.,0.),R._EAST,3),((1.,0.),R._WEST,0),
                             ((1.,0.),R._NORTH,1),((1.,1.),R._EAST,2),
                             ((1.,1.),R._WEST,1),((1.,1.),-1,1),
                             ((0.,0.),R._EAST,0)]:
            with self.subTest(point=p,direction=d):
                self.assertEqual(R._bend_lower_bound(p,g,d),expected)

    def test_heuristic_consistency_on_10000_transitions(self):
        bend=14.
        points=[(float(x),float(y)) for x in range(-2,3) for y in range(-2,3)]
        count=0
        for p in points:
            for goal in points:
                for d in (-1,0,1,2,3):
                    current=abs(p[0]-goal[0])+abs(p[1]-goal[1])+R._bend_lower_bound(p,goal,d)*bend
                    for nd,dx,dy in R._DIRS:
                        if d>=0 and nd==(d^1): continue
                        q=(p[0]+dx,p[1]+dy)
                        step=1+(bend if d>=0 and d!=nd else 0.)
                        remaining=abs(q[0]-goal[0])+abs(q[1]-goal[1])+R._bend_lower_bound(q,goal,nd)*bend
                        self.assertLessEqual(current,step+remaining+1e-9,(p,goal,d,nd))
                        count+=1
        self.assertEqual(count,10000)

    def test_u_trap_requires_initial_move_away_from_goal(self):
        obs=[device((-5.,-5.,5.,-4.)),device((-5.,4.,5.,5.)),device((4.,-5.,5.,5.))]
        res=R.solve((0.,0.),(10.,0.),obs)
        self.assert_safe(res,obs)
        self.assertLess(res.path[1][0],0.)

    def test_nonuniform_grid_minimizes_cost_not_number_of_edges(self):
        opts=R.RouteOptions()
        lat=R._Lattice([0,39,61,100],[-100]+list(range(12)),[(40,-9,60,10)],[],opts)
        res=R._astar(lat,(0,0),(100,0),opts,{})
        self.assertTrue(res.ok)
        self.assertEqual(res.cost,148.)
        self.assertLess(max(abs(p[1]) for p in res.path),100.)

    def test_astar_matches_independent_dijkstra_on_40_maps(self):
        # 使用相同网格的独立无启发式搜索；允许四方向全部后继，校验代价和可达性。
        for seed in range(40):
            rng=random.Random(seed)
            hard=[]
            for _ in range(5):
                x,y=rng.randint(2,9),rng.randint(2,9)
                hard.append((x-.2,y-.2,x+1.2,y+1.2))
            opts=R.RouteOptions(bend_penalty=rng.choice([0.,3.,14.]))
            soft=[R.Obstacle((3,3,8,8),R.KIND_SYMBOL,"soft",hard=False)]
            lat=R._Lattice(list(range(13)),list(range(13)),hard,soft,opts)
            queue=[(0.,0,0,-1)];best={(0,0,-1):0.};expected=math.inf
            while queue:
                cost,x,y,d=heapq.heappop(queue)
                if cost>best[(x,y,d)]+1e-9: continue
                if (x,y)==(12,12): expected=cost;break
                for nd,dx,dy in R._DIRS:
                    nx,ny=x+dx,y+dy
                    if not(0<=nx<13 and 0<=ny<13): continue
                    if dx:
                        if lat.blocked_x(y,x,nx): continue
                        step=1+lat.soft_x(y,x,nx)
                    else:
                        if lat.blocked_y(x,y,ny): continue
                        step=1+lat.soft_y(x,y,ny)
                    if d>=0 and d!=nd: step+=opts.bend_penalty
                    st=(nx,ny,nd);total=cost+step
                    if total<best.get(st,math.inf)-1e-9:
                        best[st]=total;heapq.heappush(queue,(total,nx,ny,nd))
            res=R._astar(lat,(0.,0.),(12.,12.),opts,{})
            with self.subTest(seed=seed):
                self.assertEqual(res.ok,math.isfinite(expected))
                if res.ok: self.assertAlmostEqual(res.cost,expected)

    def test_straight_astar_route_is_consumed(self):
        calls=[]
        cd=SimpleNamespace(pipe=lambda *args,**kw: calls.append(kw))
        spec={**SPEC,"pipes":[{**SPEC["pipes"][0],"via":[[50,50]]}]}
        P._route_pipes({"L":R.RouteResult("L",path=[(0,0),(100,0)])},cd,"pfd",set(),{}, {},spec,{})
        self.assertEqual(calls[0]["via"],[])

if __name__=="__main__": unittest.main(verbosity=2)
