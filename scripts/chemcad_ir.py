#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chemcad_ir.py —— 化工图纸绘制的中间表示（IR）与图层体系注册表。

分层思想（承自 pyautocad 技能的「库 / 工具 / CLI」分离）：
    知识层 references/*.md  →  IR 层（本文件）  →  后端层（chemcad_engine.py）
IR 与后端解耦：同一份 IR 既可由纯 Python 的 DXF 后端落盘（**无需安装 AutoCAD**），
也可由 COM 后端直接画进运行中的 AutoCAD。

坐标与单位约定（P&ID 不按比例，按最终图纸 1:1 出图）：
    1 绘图单位 = 1 mm；A1 横幅 = 841 x 594。
    文字高 2.5 mm（标注/位号）、2.0 mm（次要）、3.0 mm（说明/表格）——符合 HG 20519.1「图纸数字字母 2~3 mm」。
    模块网格 5 mm，所有关键点吸附网格 → 正交布线天然成立。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# 一、常量与线宽分档（HG 20519.1 第 6 章）
# ---------------------------------------------------------------------------

#: 图纸幅面（mm），P&ID 常用 A1 横幅、简单者 A2
SHEET_SIZES: Dict[str, Tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
}

#: 模块网格步距（mm）——关键点一律吸附此网格
GRID_MODULE: float = 5.0

#: GB/T 14689-2008 图纸边框宽度（mm）：
#:   留装订边 a = 25（装订边固定在左侧）；不留装订边 e = 10（A0/A1）/ 5（A2/A3/A4）
BIND_MARGIN: float = 25.0
EDGE_MARGIN: Dict[str, float] = {"A0": 10.0, "A1": 10.0, "A2": 5.0, "A3": 5.0, "A4": 5.0}

#: 文字高（mm）。**以 HG/T 20519-2009 表 6.2.2 为准**（标准原文列在右边）：
#:   图名 7 / 图表中的图名及视图符号 5~7 / 工程名称 5 /
#:   图纸中的文字说明及轴线号 5 / 图纸中的数字及字母 2~3 /
#:   表格中的文字 5 / 表格中的文字（格高<6mm）3
#: 标题栏内的文字高度标准明确「暂不作要求」，故单列一个常量。
TEXT_H_MAIN: float = 2.5      # 位号、管号、仪表号（属"数字及字母 2~3"）
TEXT_H_SMALL: float = 2.0     # 次要说明、小尺寸标注
TEXT_H_NOTE: float = 5.0      # 说明框等**图纸中的文字说明**（表 6.2.2：5）
TEXT_H_TITLE: float = 7.0     # **图名**（表 6.2.2：图名 7）
TEXT_H_TABLE: float = 5.0     # 表格中的文字（表 6.2.2：5）
TEXT_H_TABLE_SMALL: float = 3.0   # 表格中的文字，格高 < 6mm 时（表 6.2.2：3）
TEXT_H_TB: float = 3.0        # 标题栏内文字（标准：标题栏内文字高度暂不作要求）

#: 线宽分档（DXF code 370，单位 1/100 mm）。HG 20519.1 表 6.1.3：
#:   粗 0.6~0.9（主物料管道）/ 中粗 0.3~0.5（其他物料管道）/ 细 0.15~0.25（其他，一般取 0.25）
LW_COARSE: int = 70   # 0.70 mm
LW_MEDIUM: int = 35   # 0.35 mm
LW_FINE: int = 20     # 0.20 mm
#: **设备、机器轮廓线**：表 6.1.3 的「备注」列点名 0.25 mm。
#: ⚠️ 这一档最容易被想当然地放进"粗"——毕竟设备轮廓看着像主线。实测评审标准
#: 恰恰相反：P&ID/PFD 里**主物料管道才是粗线**，设备轮廓要细（0.25）。
LW_OUTLINE: int = 25  # 0.25 mm

#: 各档允许区间（1/100 mm），供校验器判定合规
LW_RANGES: Dict[str, Tuple[int, int]] = {
    "coarse": (60, 90),
    "medium": (30, 50),
    "fine": (15, 25),
    "outline": (15, 25),
}

#: ACI 颜色索引常用值
ACI_RED, ACI_YELLOW, ACI_GREEN, ACI_CYAN = 1, 2, 3, 4
ACI_BLUE, ACI_MAGENTA, ACI_WHITE, ACI_GRAY = 5, 6, 7, 8


# ---------------------------------------------------------------------------
# 二、图层体系注册表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LayerDef:
    """一个图层的完整定义：语义名、用途、线宽分档、ACI 颜色、线型。"""

    name: str
    purpose: str
    weight: str          # coarse / medium / fine
    color: int
    linetype: str = "CONTINUOUS"

    @property
    def lineweight(self) -> int:
        """线宽（1/100 mm）。"""
        return {"coarse": LW_COARSE, "medium": LW_MEDIUM,
                "fine": LW_FINE, "outline": LW_OUTLINE}[self.weight]

    @property
    def weight_range(self) -> Tuple[int, int]:
        return LW_RANGES[self.weight]


def _L(name, purpose, weight, color, linetype="CONTINUOUS") -> LayerDef:
    return LayerDef(name, purpose, weight, color, linetype)


#: 语义命名制图层（推荐；方案 B）。用于按图层做自动校核（线宽、线型、正交性）。
LAYERS: Tuple[LayerDef, ...] = (
    # 当前默认母图的内框线为 0.50 mm：语义层按中粗档校核，写出精确取母图值。
    _L("BORDER",     "图框与标题栏",            "medium", ACI_WHITE),
    _L("EQUIP",      "设备外轮廓",              "outline", ACI_WHITE),
    _L("EQUIP_IN",   "设备内轮廓/内部构件",      "fine",   ACI_GRAY),
    # ⚠️ 早先这里写 `medium`（0.35），但 `EMIT_MAP` 把 NOZZLE 映到标准层
    # "15-设备内部管道"（实测 0.25）——**语义层声明 0.35、实际落到 0.25 的层**，
    # 于是 `check_dxf` 按语义档位一判就"超档"。接管属**设备侧**（见 EMIT_MAP 注释），
    # 与设备外轮廓同一档才是对的：读图时不会出现"设备内部件比外轮廓还粗"。
    _L("NOZZLE",     "管口/接管（含法兰端）",    "outline", ACI_WHITE),
    _L("PIPE_PRI",   "主物料管道",              "coarse", ACI_WHITE),
    _L("PIPE_INLINE","次物料/公用工程管道",      "medium", ACI_CYAN),
    _L("AF_PIPE",    "辅助/伴热/夹套管",         "medium", ACI_GREEN),
    _L("VALVE",      "阀门、管件与附件",         "fine", ACI_WHITE),
    _L("FLOWARROW",  "流向箭头",                "fine",   ACI_WHITE),
    _L("SIGNAL",     "电动/气动/毛细管信号线",   "fine",   ACI_RED,   "DASHED"),
    _L("仪表线",      "仪表连接线（测量引线）",   "fine",   ACI_MAGENTA),
    _L("INST",       "仪表圆圈与符号",           "fine",   ACI_MAGENTA),
    _L("仪表",        "仪表位号文字",            "fine",   ACI_MAGENTA),
    _L("TAG",        "设备位号/管道号文字",      "fine",   ACI_WHITE),
    #: 设备位号分数式的**分数线**。HG 20559.7-1993 §2.0.2 要求"分数线以**粗实线**绘制"，
    #: 所以它不能跟位号文字同用细线档——单列一层，走粗档。
    _L("TAGBAR",     "设备位号分数式分数线（粗实线）", "coarse", ACI_WHITE),
    _L("DIM",        "尺寸标注",                "fine",   ACI_GREEN),
    _L("CENTERLINE", "中心线",                  "fine",   ACI_CYAN,  "CENTER2"),
    _L("MISC",       "其他/说明/注释框",         "fine",   ACI_WHITE),
    #: 标准表格（物流表 / 设备位号表 / 图例）。**必须与 MISC 分开**：
    #: "说明框"是靠 `_largest_closed_box(cd, "MISC")` 认出来的（要算它占掉哪块版面），
    #: 表格若也画在 MISC 上，那个函数会认到最大的一张表 → 说明框的编号检查全盘错位。
    _L("TABLE",      "标准表格（物流表/位号表/图例）", "fine",   ACI_WHITE),
    #: **工段框**（分区线）。浙江大学 PFD/P&ID 的图层表里就声明了
    #: `分区线` / `内框线` / `外框线` —— 各院做法一致：工段/分区要成框，
    #: 读图人才能一眼看出"这几台设备属同一工段"。
    # ⚠️ 原 "medium"：工段框与图框同层（PFD 无专用分区线层，见 EMIT_MAP），
    #    图框层照浙工大实测降到 fine 后，SECT 必须跟着降，否则两套映射互检会红。
    _L("SECT", "工段框/分区线", "fine", ACI_WHITE),
    #: **图纸接续标志**（跨张/跨工段的流股接续，画在流股端头）。
    #: 单列一层的理由与 `TABLE` 同：**不能画在 MISC 上**——说明框是靠
    #: `_largest_closed_box(cd, "MISC")` 认的，接续标志是一串带框的小矩形，
    #: 画在 MISC 上同样会把"最大的框"认错。
    _L("OFFPAGE",    "流股接续标志（跨张/跨工段）", "fine",  ACI_WHITE),
    #: **进出工厂界区**的物流标志（剖线圆 + 箭头），与"跨工段接续"是两种东西：
    #: 前者是物料**进出厂**，后者是流股**跨图纸**。
    _L("BATTERY",    "进出工厂界区物流标志",      "fine",   ACI_WHITE),
    _L("_Grid",      "定位辅助网格（出图前可冻结）", "fine", ACI_GRAY),
    _L("HATCH",      "剖面线",                  "fine",   ACI_GRAY),
    _L("DEFPOINTS",  "非出图标记点",            "fine",   ACI_WHITE),
)

LAYER_REGISTRY: Dict[str, LayerDef] = {ld.name: ld for ld in LAYERS}

#: 方案 A（数字序号命名）→ 方案 B（语义命名）的映射。
#: symbols-library.md §5 对照表。**同一项目内不得混用两套**。
NUMERIC_ALIAS: Dict[str, str] = {
    "1": "PIPE_PRI", "2": "PIPE_INLINE", "3": "EQUIP", "4": "EQUIP_IN",
    "5": "BORDER", "6": "FLOWARROW", "7": "MISC", "8": "TAG", "9": "MISC",
    "10": "VALVE", "11": "PIPE_INLINE", "12": "PIPE_INLINE", "13": "SIGNAL",
    "14": "INST", "15": "仪表线", "16": "TAG", "18": "AF_PIPE",
    "19": "AF_PIPE", "20": "AF_PIPE", "21": "AF_PIPE", "22": "TAG",
}

#: **出图图层标准**（`NN-用途-线型`，见 `chemcad_layers.STANDARD`）→ 语义名。
#:
#: 为什么需要这张"反查表"：出图时会把语义层名换成本标准的层名
#: （`chemcad_layers.apply_standard`）。若校验器只认语义名，
#: 那么**"按标准出图"反而会让自己的线宽/图元校验全部报错**——
#: 输出合规、自检崩溃，方向正好搞反。所以标准层名必须在 `resolve_layer()`
#: 里能回落到对应语义层，校验才能继续工作。
#: ⚠️ 这张表与 `chemcad_layers.EMIT_MAP` 必须**互为逆映射**，
#: 由 `chemcad_layers` 的自检比对（改一处忘另一处会红）。
STD_ALIAS: Dict[str, str] = {
    "01-主物流线-粗实线": "PIPE_PRI",
    "02-辅助物流线-中粗线": "PIPE_INLINE",
    "03-设备外轮廓线-细实线": "EQUIP",
    "03-辅助物流线-中粗线": "PIPE_INLINE",
    "04-次要物流线-中粗线": "AF_PIPE",
    "04-设备内部件-细实线": "EQUIP_IN",
    "05-冷公用工程": "PIPE_INLINE",
    "05-热公用工程": "AF_PIPE",
    "05-次物流线-中粗线": "AF_PIPE",
    "06-箭头": "FLOWARROW",
    "06-说明文本": "MISC",
    "07-流股接续标志": "OFFPAGE",
    "07-设备外轮廓线-细实线": "EQUIP",
    "08-设备内部件-细实线": "EQUIP_IN",
    "08-进出物流标志": "BATTERY",
    "09-流股接续标志": "MISC",
    "09-进出物流标志": "MISC",
    "09-流股代号": "TAG",
    "10-公用工程-氮气或空气与回管": "PIPE_INLINE",
    "10-文本": "TAG",
    "11-文本": "TAG",
    "11-阀门与管件": "VALVE",
    "12-信号线": "SIGNAL",
    "12-流股代号": "TAG",
    "12-设备位号线": "TAGBAR",
    "13-箭头": "FLOWARROW",
    "13-物流表及物流信息标识": "TABLE",
    "14-信号线": "SIGNAL",
    "14-图框与标题栏": "BORDER",
    "14-阀门与管件": "VALVE",
    "15-仪表": "INST",
    "15-设备内部管道": "NOZZLE",
    "15-阀门与管件": "VALVE",
    "17-仪表": "INST",
    "18-图框与标题栏": "BORDER",
    "21-标注": "DIM",
    "22-仪表": "INST",
    "23系统内信号线": "仪表线",
    "分区线": "SECT",
    "内框线": "SECT",
    "外框线": "SECT",
    #: **浙工大 PFD 实测补充**（`工艺物流流程图（PFD）-2004.dwg`，20 层）：标准里
    #: 另有 `02-标题栏文本` / `09-文本` / `10-文本` / `03-设备轮廓线`（与
    #: `03-设备外轮廓线-细实线` 同义异名，样本里两套写法并存）。补进来读图才归一化得到。
    "02-标题栏文本": "TAG",
    "09-文本": "MISC",
    "03-设备轮廓线": "EQUIP",
}

# 当前默认母图的用途归一化（同名项覆盖历史读图别名）。
STD_ALIAS.update({
    "标题栏边框": "BORDER", "标题栏分隔": "BORDER", "标题栏文字": "BORDER",
    "外框线": "BORDER", "内框线": "BORDER", "分区线": "TABLE", "1-文本": "MISC",
    "2-设备轮廓线-细实线": "EQUIP", "3-设备内部件-细实线": "EQUIP_IN",
    "4-主物料线-粗实线": "PIPE_PRI", "5-阀门与管件": "VALVE", "6-箭头": "FLOWARROW",
    "7-流股接续标志": "OFFPAGE", "8-进出物流标志": "BATTERY",
    "9-热公用工程": "AF_PIPE", "10-冷公用工程": "PIPE_INLINE",
    "11-次要物料线-中粗实线": "PIPE_INLINE", "12-辅助物料线-中粗实线": "AF_PIPE",
    "13-氮气与空气与回管": "AF_PIPE", "14-信号线": "SIGNAL", "15-流股代号": "TAG",
    "16-仪表": "INST", "17-设备位号线": "TAGBAR", "未完成有待商榷": "PIPE_PRI",
    "18-熔融盐": "AF_PIPE", "19-开工线": "AF_PIPE", "20-工艺软水": "AF_PIPE",
    "21-低压蒸汽": "AF_PIPE", "22-中压蒸汽": "AF_PIPE", "23-高压蒸汽": "AF_PIPE",
})

#: 常见等价写法（机械图/其他院的习惯命名）→ 语义名
EXTRA_ALIAS: Dict[str, str] = {
    "标注": "DIM", "标注dim": "DIM", "dim": "DIM", "1dim": "DIM",
    "点划线": "CENTERLINE", "点画线": "CENTERLINE", "cen": "CENTERLINE",
    "2中心线": "CENTERLINE", "centerline": "CENTERLINE",
    "细实线": "MISC", "粗实线": "EQUIP", "实线": "MISC",
    "文本": "TAG", "文字": "TAG", "OTHERS": "MISC",
    "注释": "MISC", "说明": "MISC", "INST_SIGNAL": "SIGNAL",
    "_Cutline": "BORDER", "_Border": "BORDER",
}


def resolve_layer(name: str) -> str:
    """把任意命名习惯的图层名归一化为语义名；不认识的返回原名。

    认三类：语义名本身 / 数字序号（方案 A）/ 常见别名 / **出图图层标准名**。
    最后一类尤其重要——出图默认按标准换名，若这里认不出标准名，
    自己的线宽与图元校验会全部报"图层未注册"。
    """
    if name in LAYER_REGISTRY:
        return name
    if name in NUMERIC_ALIAS:
        return NUMERIC_ALIAS[name]
    if name in STD_ALIAS:
        return STD_ALIAS[name]
    low = name.strip().lower()
    for k, v in EXTRA_ALIAS.items():
        if k.lower() == low:
            return v
    for k, v in STD_ALIAS.items():
        if k.strip().lower() == low:
            return v
    return name




# ---------------------------------------------------------------------------
# 三、绘图实体 IR
# ---------------------------------------------------------------------------

Pt = Tuple[float, float]


def simplify(path: Sequence[Pt]) -> List[Pt]:
    """去重复点、合并共线中间点，并把"斜跳"补成两个正交段。

    正交布线的公共后处理：ELK 分段布线与 A* 避障布线共用这一份
    （原先两个模块各有一份同实现，改一处忘一处）。
    """
    out: List[Pt] = []
    for p in path:
        p = (float(p[0]), float(p[1]))
        if out and abs(p[0] - out[-1][0]) < 1e-9 and abs(p[1] - out[-1][1]) < 1e-9:
            continue
        if out and abs(p[0] - out[-1][0]) > 1e-9 and abs(p[1] - out[-1][1]) > 1e-9:
            out.append((p[0], out[-1][1]))
        out.append(p)
    res: List[Pt] = []
    for p in out:
        if len(res) >= 2:
            if abs(res[-1][0] - res[-2][0]) < 1e-9 and abs(res[-1][0] - p[0]) < 1e-9:
                res[-1] = p
                continue
            if abs(res[-1][1] - res[-2][1]) < 1e-9 and abs(res[-1][1] - p[1]) < 1e-9:
                res[-1] = p
                continue
        res.append(p)
    return res


@dataclass
class Ent:
    """实体基类。所有实体必须落在某个图层上（化工图按图层校核线宽与线型）。"""

    layer: str = "MISC"
    color: Optional[int] = None      # None → ByLayer
    linetype: Optional[str] = None   # None → ByLayer
    note: str = ""                   # 语义备注（不落入 DXF，供校验/调试）

    def points(self) -> List[Pt]:
        """返回实体参与正交性/包围盒计算的关键点。"""
        return []


@dataclass
class Line(Ent):
    p1: Pt = (0.0, 0.0)
    p2: Pt = (0.0, 0.0)

    def points(self) -> List[Pt]:
        return [self.p1, self.p2]

    @property
    def length(self) -> float:
        return math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1])


@dataclass
class Polyline(Ent):
    pts: List[Pt] = field(default_factory=list)
    closed: bool = False
    width: float = 0.0   # 线宽（图形宽度，mm）；0 = 按图层

    def points(self) -> List[Pt]:
        return list(self.pts)


@dataclass
class Circle(Ent):
    center: Pt = (0.0, 0.0)
    radius: float = 1.0

    def points(self) -> List[Pt]:
        r = self.radius
        return [(self.center[0] - r, self.center[1] - r), (self.center[0] + r, self.center[1] + r)]


@dataclass
class Arc(Ent):
    center: Pt = (0.0, 0.0)
    radius: float = 1.0
    start_deg: float = 0.0
    end_deg: float = 90.0

    def points(self) -> List[Pt]:
        r = self.radius
        return [(self.center[0] - r, self.center[1] - r), (self.center[0] + r, self.center[1] + r)]


@dataclass
class Solid(Ent):
    """实心填充三角形/四边形（AutoCAD SOLID）。用于流向箭头等实心符号。

    3 点时第 4 点按 DXF 约定重复第 3 点。三角形顶点顺序为 1-2-3，
    四边形需按「Z 字序」给出：(p1,p2,p3,p4) = 左下、右下、左上、右上。
    """

    pts: List[Pt] = field(default_factory=list)

    def points(self) -> List[Pt]:
        return list(self.pts)


@dataclass
class Text(Ent):
    """单行文字（AcDbText）。

    注意（承自 pyautocad 技能）：COM 下若设置了对齐方式，**必须同时写 TextAlignmentPoint**，
    否则文字掉到 (0,0,0)。COM 后端已统一处理，此处仅声明对齐语义。
    """

    text: str = ""
    pt: Pt = (0.0, 0.0)
    height: float = TEXT_H_MAIN
    align: str = "left"         # left / center / middle_center / right
    rotation: float = 0.0       # 度

    def points(self) -> List[Pt]:
        return [self.pt]


@dataclass
class Group:
    """符号实例：一组实体的集合，可整体作为图块（BLOCK/INSERT）落图。

    `name` 即块名（COM 后端据此建块；DXF 后端据此写 BLOCKS 段）。
    `tag`  是该实例的位号（若有），供位号校验与反查。
    """

    name: str
    base: Pt
    ents: List[Ent] = field(default_factory=list)
    tag: str = ""
    kind: str = ""              # equipment / valve / instrument / fitting ...
    ref: str = ""               # 关联的管线号或设备号
    meta: Dict[str, object] = field(default_factory=dict)

    def points(self) -> List[Pt]:
        out: List[Pt] = []
        for e in self.ents:
            out.extend(e.points())
        return out


Item = Union[Group, Ent]


# ---------------------------------------------------------------------------
# 四、图纸容器
# ---------------------------------------------------------------------------

@dataclass
class Drawing:
    """一份图纸 = 有序的图元序列 + 使用到的图层集合。"""

    name: str = "P&ID"
    sheet: str = "A1"
    items: List[Item] = field(default_factory=list)
    meta: Dict[str, object] = field(default_factory=dict)

    # -- 添加 ---------------------------------------------------------------
    def add(self, item: Item) -> Item:
        self.items.append(item)
        return item

    def add_all(self, items: Sequence[Item]) -> None:
        self.items.extend(items)

    def line(self, p1: Pt, p2: Pt, layer: str, **kw) -> Line:
        return self.add(Line(layer=layer, p1=(float(p1[0]), float(p1[1])),
                             p2=(float(p2[0]), float(p2[1])), **kw))  # type: ignore[return-value]

    def polyline(self, pts: Sequence[Pt], layer: str, closed: bool = False, **kw) -> Polyline:
        return self.add(Polyline(layer=layer, pts=[(float(p[0]), float(p[1])) for p in pts],
                                 closed=closed, **kw))  # type: ignore[return-value]

    def circle(self, center: Pt, radius: float, layer: str, **kw) -> Circle:
        return self.add(Circle(layer=layer, center=(float(center[0]), float(center[1])),
                               radius=float(radius), **kw))  # type: ignore[return-value]

    def arc(self, center: Pt, radius: float, a1: float, a2: float, layer: str, **kw) -> Arc:
        return self.add(Arc(layer=layer, center=(float(center[0]), float(center[1])),
                            radius=float(radius), start_deg=a1, end_deg=a2, **kw))  # type: ignore[return-value]

    def solid(self, pts: Sequence[Pt], layer: str, **kw) -> Solid:
        return self.add(Solid(layer=layer, pts=[(float(p[0]), float(p[1])) for p in pts], **kw))  # type: ignore[return-value]

    def text(self, text: str, pt: Pt, layer: str = "TAG", height: float = TEXT_H_MAIN,
             align: str = "left", rotation: float = 0.0, **kw) -> Text:
        return self.add(Text(layer=layer, text=text, pt=(float(pt[0]), float(pt[1])),
                             height=height, align=align, rotation=rotation, **kw))  # type: ignore[return-value]

    def group(self, name: str, base: Pt, kind: str = "", tag: str = "", ref: str = "",
              **meta) -> Group:
        g = Group(name=name, base=(float(base[0]), float(base[1])), kind=kind, tag=tag,
                  ref=ref, meta=dict(meta))
        self.add(g)
        return g

    # -- 查询 ---------------------------------------------------------------
    def iter_ents(self) -> Iterator[Ent]:
        """展平遍历所有实体（展开 Group 内部）。"""
        for it in self.items:
            if isinstance(it, Group):
                yield from it.ents
            else:
                yield it

    def iter_groups(self) -> Iterator[Group]:
        for it in self.items:
            if isinstance(it, Group):
                yield it

    def used_layers(self) -> List[str]:
        return sorted({e.layer for e in self.iter_ents()})

    def entities_on(self, layer: str) -> List[Ent]:
        want = resolve_layer(layer)
        return [e for e in self.iter_ents() if resolve_layer(e.layer) == want]

    def bbox(self) -> Tuple[float, float, float, float]:
        xs: List[float] = []
        ys: List[float] = []
        for e in self.iter_ents():
            for x, y in e.points():
                xs.append(x)
                ys.append(y)
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def content_bbox(self, skip_layers: Sequence[str] = ("BORDER",)) -> Tuple[float, float, float, float]:
        """内容包围盒：跳过图框/标题栏等「图纸家具」，只统计真正的图面内容。"""
        skip = {resolve_layer(l) for l in skip_layers}
        xs: List[float] = []
        ys: List[float] = []
        for it in self.items:
            ents = it.ents if isinstance(it, Group) else [it]
            if ents and all(resolve_layer(e.layer) in skip for e in ents):
                continue
            for e in ents:
                if resolve_layer(e.layer) in skip:
                    continue
                for x, y in e.points():
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def translate(self, dx: float, dy: float,
                  skip_layers: Sequence[str] = ("BORDER",)) -> None:
        """整体平移图元。

        **只平移、不缩放**——这样文字高度（2~3 mm）与图层线宽都不会被破坏，
        正交关系也天然保持。
        """
        if dx == 0 and dy == 0:
            return
        skip = {resolve_layer(l) for l in skip_layers}
        for it in self.items:
            if isinstance(it, Group):
                if it.ents and all(resolve_layer(e.layer) in skip for e in it.ents):
                    continue
                for e in it.ents:
                    shift_ent(e, dx, dy)
                it.base = (it.base[0] + dx, it.base[1] + dy)
                anc = it.meta.get("anchors")
                if isinstance(anc, dict):
                    it.meta["anchors"] = {k: (v[0] + dx, v[1] + dy) for k, v in anc.items()}
            else:
                if resolve_layer(it.layer) in skip:
                    continue
                shift_ent(it, dx, dy)

    def stats(self) -> Dict[str, object]:
        from collections import Counter
        kinds: Counter = Counter()
        for e in self.iter_ents():
            kinds[type(e).__name__] += 1
        return {
            "name": self.name,
            "sheet": self.sheet,
            "items": len(self.items),
            "groups": sum(1 for _ in self.iter_groups()),
            "entities": sum(kinds.values()),
            "entity_kinds": dict(sorted(kinds.items())),
            "layers": self.used_layers(),
            "bbox": [round(v, 1) for v in self.bbox()],
            "texts": sum(1 for e in self.iter_ents() if isinstance(e, Text)),
        }


# ---------------------------------------------------------------------------
# 五、几何工具
# ---------------------------------------------------------------------------

def snap(v: float, module: float = GRID_MODULE) -> float:
    """把一个坐标吸附到模块网格。"""
    return round(v / module) * module


def snap_pt(p: Sequence[float], module: float = GRID_MODULE) -> Pt:
    return (snap(float(p[0]), module), snap(float(p[1]), module))


def is_orthogonal(p1: Pt, p2: Pt, tol: float = 1e-6) -> bool:
    """两点连线是否水平或竖直。"""
    return abs(p2[0] - p1[0]) <= tol or abs(p2[1] - p1[1]) <= tol


def deviation_deg(p1: Pt, p2: Pt) -> float:
    """与最近的正交轴的最小夹角偏差（度）。正交为 0。"""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if dx == 0 and dy == 0:
        return 0.0
    ang = math.degrees(math.atan2(dy, dx)) % 90.0
    return min(ang, 90.0 - ang)






def lerp(p1: Pt, p2: Pt, t: float) -> Pt:
    return (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)


def box_pts(cx: float, cy: float, w: float, h: float, anchor: str = "center") -> List[Pt]:
    """按锚点生成矩形四角点。anchor: center / bottom_left / bottom_center / top_center。"""
    if anchor == "bottom_left":
        x0, y0 = cx, cy
    elif anchor == "bottom_center":
        x0, y0 = cx - w / 2.0, cy
    elif anchor == "top_center":
        x0, y0 = cx - w / 2.0, cy - h
    else:
        x0, y0 = cx - w / 2.0, cy - h / 2.0
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def shift_ent(e: Ent, dx: float, dy: float) -> Ent:
    """把一个实体整体平移（原地修改）。"""
    def s(p: Pt) -> Pt:
        return (p[0] + dx, p[1] + dy)

    if isinstance(e, Line):
        e.p1, e.p2 = s(e.p1), s(e.p2)
    elif isinstance(e, Polyline):
        e.pts = [s(p) for p in e.pts]
    elif isinstance(e, Solid):
        e.pts = [s(p) for p in e.pts]
    elif isinstance(e, Circle):
        e.center = s(e.center)
    elif isinstance(e, Arc):
        e.center = s(e.center)
    elif isinstance(e, Text):
        e.pt = s(e.pt)
    return e

