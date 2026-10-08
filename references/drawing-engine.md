# 绘图引擎：把制图规范变成可执行约束

> 本文档描述 `scripts/chemcad_*.py` / `draw_pfd.py` 这套出图引擎的架构与用法。
> **规范条文**见 `drawing-conventions.md`；本文档只讲「怎么把条文变成代码约束」。

---

## 新增模块（2026-10-06）

| 模块 | 职责 |
|---|---|
| `chemcad_process_layout.py` | 用明确塔型、塔顶冷凝/回流或再沸名称及有向闭合连接识别回路；主流程单链向右，回路上下分区 |
| `chemcad_native.py` | 原生实体/递归块导入、来源资源隔离、字体保留、填充关联和扩展对象闭包检查 |
| `chemcad_assets.py` | 全类别目录查询及按明确ID调用；未知项不自动选设备 |
| `chemcad_sections.py` | 有序工段逐行、唯一汇总表、流股身份合并和接续校核；旧book入口兼容 |

寻路为已识别回路优先预留外围通道与完整流股标注矩形，再让后续管线避让；普通拓扑保持ELK，证据不足不猜冷凝/再沸职责。已识别回路无标注位置时尝试外围正交绕行，最终所有实际走线/标注进入硬检查，无解报错。3mm净距、设备水平接管、分支同点同轴、4mm交叉断口和固定文字位置不降低。

COM保存测量设备附近短位号及底部两行文字的真实字形中心，短位号局部中心0、底部位号/名称局部5/0；整体目标仍为轮廓交点下10mm及框底上60/55mm。临时文件仅作ASCII路径转存，打开/保存/测量仍由AutoCAD COM执行；源/目标已在用户实例打开时拒绝覆盖。批量验收使用独立COM恢复实例，未关闭用户原图。

工段规范和退出码见[pfd-sections.md](pfd-sections.md)、[incremental-library.md](incremental-library.md)。本次完整示例和原生回读范围有任务证据；未新增或运行旧测试套件，不用文档计数或语法通过代替运行正确。

## 默认图层配置

默认使用用户指定浙江大学《PFD-2004版.dwg》的实际 32 个图层。`assets/layer-standard.json` 为唯一配置来源，`chemcad_layers.py` 加载图层属性和用途映射；默认输出完整层表。原始线宽码 -3、Defpoints 不打印状态与 HIDDEN 的 6.35 / -3.175 mm 线型段长均保留。完整表与来源 SHA-256 见 `default-layers.md`。


## 平行流股线和换热器外接管（2026-10-05）

`chemcad_engine.PIPE_EDGE_GAP=3mm`按默认图层实际线宽计算边缘净距，主/次线中心距至少3.6mm。`pipe_spacing_bands`将已布路径转为方向禁区，`chemcad_route._Lattice`仅阻挡同方向运动；`PIPE_PREFERRED_GAP=12mm`范围另加方向性软代价，优先给菱形/表框留空间。先预留全部设备端点必经接管，再依次寻路。垂直交叉仍按4mm断口规则处理，已布折点也留净空。搜索留0.01mm舍入余量，最终比较容差0.001mm。与设备净距放宽分开，3mm硬下限永不归零。`detect_pipe_spacing`检查落笔实体，含同一流股折回的平行段；共用设备接点仅豁免18mm同轴短直段，旁边的平行禁区保留。

`heat_exchanger_flow_rect`纳入实际EQUIP/NOZZLE/FLOWARROW几何，保护区外扩3mm；所属接管轴允许水平或竖直连接，其余线段禁止穿入，防止折弯贴住斜箭头。`detect_pipe_over_exchanger_nozzles`与最终校核一致。两项均进入版面计数、preflight、CLI与render出图闸门；显式路线也不能绕过最终检查。

标注先按设备和全部管线筛选可用位置，候选较少者先占位；仍保持固定三行框、引线、菱形整体移动。两组标注相撞时，仅学习框/引线保护区用于重建，菱形自身承载线豁免保留。无解时报错，不直接拉开框格、截断接管或重画设备符号。

补充（2026-10-06）：`shared_port_plan`以设备ID及实际锚点坐标归并接管别名，3条及以上流股共用接点时优先以主物料线作主管。`route_pipes`为支线指定一个公共折点，主管在其外侧继续直行12mm，两条支线分别向上下（竖向主管则左右）正交离开12mm再寻路；每条公共分支通道预先单独保留，另一支线及无关物流也须避让。固定接管前缀作为搜索硬禁区、端面开放，禁止寻路穿回自身接管产生自交。`shared_port_alignment`检查首个实际折点重合且主管覆盖该点，防止相邻错开的T形接合；此规则进入版面计数、CLI和render。

`equipment_flow_rect`将所有设备的本体、NOZZLE、FLOWARROW和VALVE原块实体纳入统一保护范围，外扩3mm后交给寻路。`detect_pipe_over_equipment_nozzles`只豁免该设备所属流股沿明确接管轴的连接，禁止无关物流穿过泵出口长接管等设备线。旧换热器接口保留为其子集，供已有调用方使用。

图层写出映射会把若干本体和接管语义合并到同一标准层；`apply_standard`保留实体的原几何语义，`group_rect`按原语义量尺，避免映射后重新判断出线方向。A*完整路径已经包含接管段，落笔不得再次套内部stub修正模式。设备与自由坐标间的进出料线也走相同避障流程；管线上取点仍由现有接续逻辑处理。

## 一、定位：与 pyautocad 技能的关系

本技能的出图能力**承袭 `pyautocad` 技能的生产模式**（COM 重试、图层管理、图块、
Undo 标记、文字对齐坑位），在此之上补的是**化工域语义**：符号几何、正交布线铁律、
控制回路闭环、图层线宽分档、仪表位号规则。

| 层次 | pyautocad 技能 | 本技能 |
|---|---|---|
| 通用 CAD 操作 | `CADClient`：连 AutoCAD、建图层、画线/圆/多段线/文字、图块、清层 | 复用同一套调用模式（`ComBackend`），并**内建一份零依赖实现**保证可用性 |
| 领域语义 | — | 设备/阀门/仪表符号库、锚点体系、正交布线器、回路闭环校验、图层线宽登记表 |
| 交付形态 | 直接画进运行中的 AutoCAD | ① 画进 AutoCAD（COM）② 落盘 DXF（**无需 AutoCAD**）③ SCR 命令行脚本 ④ AutoLISP `entmake` 脚本（后两条同样**零依赖**） |

关键判据：**能自动校核的规范才真正被遵守**。所以每条硬性条文都有一条对应代码或校验器（见 §6、§8）。
反向也成立：**能提取的数据才算真的可评审**——所以引擎同时备了一条 COM **读回**通道（§10.5）。

---

## 二、分层与文件职责

```
知识层  references/*.md            规范条文、符号对照、控制方案、代号、标准索引、经验库
   ↓
IR 层   chemcad_ir.py             实体模型 + 图层注册表 + 几何工具（无外部依赖）
        chemcad_symbols.py        设备/阀门/仪表/管件/标注/图框 的几何生成 + 锚点体系
        chemcad_dwg.py            DXF→DWG 转换 + **COM 会话管理**（出图通道强制 COM）
   ↓
引擎层  chemcad_engine.py         正交布线、图纸容器、合规校验、三类版面缺陷探测、
                                 接管段走线、四后端渲染、COM 读回
        chemcad_layout.py         工段划分 · 设备数量统计 · **ELK layered 版面分配** · 迭代修正
                                  （化工语义 → ELK 图 → 设备基点；不认识图布局算法）
        chemcad_elk.py            **ELK layered 引擎**（零依赖、不认识化工）：
                                  破环 / 分层 / 折行 / 虚拟节点 / 交叉最小化 /
                                  Brandes–Köpf 层内排布 / 端口放置 / 正交布线
        chemcad_scr.py            SCR 命令序列 / AutoLISP entmake 脚本生成
        draw_pfd.py               规格(JSON) → 建图 → 自检 → 出图（CLI）
提取层  chemcad_locate.py         **文件侧**定位与提取：重点看 §10.6
                                 作用域切分、端口反推尺寸、折线重建、轮廓裁切、拾取、测距、
                                 文字清洗、Aspen `.bkp` 解析（零依赖，CLI + 可导入库）
        inspect_dxf.py            轻量罗列（图层/块/实体计数/文字样例）
        read_dwg.py               读**运行中的 DWG**（COM 提取通道，CLI）
        selftest.py               链路自检（不需要 AutoCAD），[0] 是 meta 契约对账
   ↓
后端    ezdxf/R2010 │ 内置/R12 │ AutoCAD COM │ SCR │ AutoLISP
```

基础 IR 图元可输出到各后端。新版原生设备块仅经 ezdxf 输出 DXF，再经 AutoCAD COM 转换为默认交付 DWG；直接 COM/SCR/LISP/R12 会拒绝这些块，防止丢失曲线和填充。IR 代理只用于排版与关系校核。目录文件与块名见 `equipment-library.md`。

---

## 三、坐标与单位约定（P&ID 不按比例，按最终图纸 1:1 出图）

| 项 | 约定 |
|---|---|
| 绘图单位 | **1 单位 = 1 mm**（DXF `$INSUNITS = 4`） |
| 幅面 | **A1 = 841 × 594（本技能标准图幅**，= 浙工大参赛 PFD 实测口径）；A2/A3/A0 见 `SHEET_SIZES`，只在规格里显式指定时用；`auto` 从 A1 起试、装不下才升 A0 |
| 模块网格 | 5 mm（`GRID_MODULE`），关键点吸附网格 → 正交天然成立 |
| 文字高 | 设备位号/名称与管旁参数默认 3mm；仪表 2.5mm；次要 2mm；说明与完整数据表 5mm。设备默认字高来自用户三份 PFD，见 `pfd-reference-study.md`，不以整图表格字号替代设备字号 |
| 典型尺寸 | 仪表圆圈 Ø10；阀门长 10；塔 ø24×h150；卧罐 42×16；换热器 34×13；泵 Ø11 |

> 为什么 P&ID 用「图纸尺寸」作图：P&ID 不按比例，图上就是最终图纸上的毫米，
> 因此文字高必须直接是 2~3 mm，设备符号也就只有几十毫米——这是正确做法，不是画小了。

---

## 四、图层注册表（单一事实来源）

定义在 `chemcad_ir.LAYERS`，绘图、校验、`check_dxf.py` **共用同一份**，避免三方各写一份而漂移。

| 图层 | 用途 | 线宽档 | 实际线宽 | 颜色 | 线型 |
|---|---|---|---|---|---|
| `PIPE_PRI` | 主物料管道 | 粗 | 0.70 | 7 | CONTINUOUS |
| `EQUIP` | 设备外轮廓 | 粗 | 0.70 | 7 | CONTINUOUS |
| `PIPE_INLINE` | 次物料/公用工程 | 中粗 | 0.35 | 4 | CONTINUOUS |
| `AF_PIPE` | 辅助/伴热/夹套管 | 中粗 | 0.35 | 3 | CONTINUOUS |
| `VALVE` | 阀门、管件、附件 | 中粗 | 0.35 | 7 | CONTINUOUS |
| `NOZZLE` | 管口/接管 | 中粗 | 0.35 | 7 | CONTINUOUS |
| `BORDER` | 图框与标题栏 | 中粗 | 0.35 | 7 | CONTINUOUS |
| `EQUIP_IN` | 设备内轮廓/内部构件 | 细 | 0.20 | 8 | CONTINUOUS |
| `SIGNAL` | 电动/气动/毛细管信号线 | 细 | 0.20 | 1 | **DASHED** |
| `仪表线` | 仪表连接线（测量引线） | 细 | 0.20 | 6 | CONTINUOUS |
| `INST` / `仪表` | 仪表圆圈 / 位号文字 | 细 | 0.20 | 6 | CONTINUOUS |
| `FLOWARROW` `TAG` `DIM` `MISC` `_Grid` `HATCH` `DEFPOINTS` | 箭头/文字/标注/说明/网格/剖面/标记点 | 细 | 0.20 | — | — |
| `CENTERLINE` | 中心线 | 细 | 0.20 | 4 | CENTER2 |

线宽区间由 `LW_RANGES` 定义（粗 60~90、中粗 30~50、细 15~25，单位 1/100 mm），
写入 DXF 的 `370` 组码，并由 `check_dxf.py` 反向核对。

**两套命名体系**：`NUMERIC_ALIAS`（数字序号制 → 语义名）与 `EXTRA_ALIAS`（各院习惯命名）
把任意写法归一化。`resolve_layer()` 是唯一入口。**同一项目不得混用**——`validate_layers()` 与
`check_dxf.py` 都会报。

---

## 五、符号库

### 5.1 命名兼容与证据纪律
三种写法都能用：工程口语（`闸阀`/`塔`/`换热器`）、英文规范名（`gate`/`tower`/`exchanger`）、
**图例实测块名**（`BV`/`GLO`/`WL004`/`CENTPUMP`/`FSIA001`…，映射见 `BLOCK_ALIAS`）。

`BLOCK_ALIAS` 的映射**只采信「强证据」**（`symbol-library.md` §3.1：图例块旁直接写了英文全名），
弱证据的块**不凭猜建模**；线型/注释类块（`SHX`/`SHXX`/`PDR`/`WL026`/`JJ5`）收进 `LINETYPE_BLOCKS`，
不当作符号，避免误报。

缺少对应图块时停止绘制，列出设备与模块类型并向用户确认。禁止猜型、占位框和手绘替代。

### 5.2 多义块：一对多别名 + 上下文消歧

图例里存在**同块两义**（`FM`=闸阀&夹套闸阀、`TJF`=薄膜&自力式、`PS`=工艺排污口&平板闸阀）。
`BLOCK_ALIAS` 是 `Dict[str,str]`（一对一），**结构上装不下多义**，故多义块单列
`BLOCK_AMBIGUITY: Dict[str, Tuple[AliasRule, ...]]`，由 `disambiguate_alias()` 判定：

```
params.meaning/hint/intent  >  tag 文本  >  layer 图层名（弱证据，+3 分）
计分：触发词命中 len(键)*2（**最长键优先**，防「夹套闸阀」被「闸阀」抢走）+ default 兜底 0.5
```

`resolve_symbol(kind, context)` / `canonical_name(kind, context)` / `place_symbol(**params)`
均支持上下文；`params` 里的 `meaning`/`hint`/`intent`/`note`/`layer` 会被取出（**不再传给符号函数**）。

判定结果**一律留痕**（`Group.meta.alias_block/alias_symbol/alias_reason/alias_is_default`），
`preflight()` 返回 `alias_choices`，`format_report()` 会把「按默认猜的」标为 `默认·待复核`——
**故意不进 `CHECKS`**：默认含义（如 FM→闸阀）在正常图中遍地都是，报警会把真问题淹掉。

**符号规模**：库内已建模常用设备/阀门/仪表/管件/标注/图框，覆盖 92 个实测块中的绝大多数。

> ⚠️ 原先的**机械图外形符号** `chemcad_symbols_mech.py`（`mech_hex_*` 换热器、
> `mech_vessel_*` 容器、`mech_column` 塔、`mech_tank_roof` 罐顶）已于 2026-09-23
> 随整个机械制图部分**删除**（本技能只出 PFD，用不到设备外形/机械图符号）。

**锚点兜底**：`place_symbol()` 末尾调用 `_fallback_anchors()`——任何符号若未显式给锚点，
自动从包围盒补 `inlet`/`outlet`/`top`/`bottom`，保证布线取锚点**永不因缺锚点而炸**。

### 5.3 锚点体系（管线的两端一律用锚点表达）
`place_symbol()` 返回的 `Group.meta["anchors"]` 是命名连接点。**不允许手写魔数坐标**——
这是正交布线与回路闭环能被自动校验的前提。

| 符号 | 锚点 |
|---|---|
| 塔 `tower` | `bottom` `top` `top_head` `skirt_base` `side_L` `side_R` `feed` `distillate` `reflux` `reboiler_draw` `reboiler_return` |
| 卧式容器 `vessel` | `left` `right` `top` `bottom` `side_L` `side_R` |
| 储罐 `tank` | `top` `bottom` `roof_apex` `side_L` `side_R` `inlet` `outlet` |
| 反应釜 `reactor` | `top` `bottom` `motor_top` `side_L` `side_R` |
| 圆形换热器 `hx.cooling` / `hx.heating` | `left`/`tube_in`/`inlet` 与 `right`/`tube_out`/`outlet` 等水平接管别名；禁止物流接斜向箭头及 `shell_in`/`shell_out`/`top`/`bottom`。釜式再沸器按自身已确认接点 |
| 泵 `pump` | `suction` `discharge` `base` |
| 调节阀 `control` | `actuator`（**信号线接这里**）、`valve_body` |
| 仪表圆圈 `bubble` | `bubble` `left` `right` `top` `bottom` |

> **为什么调节阀要单独给 `actuator` 锚点**：信号线若接到阀体中心（即管线上的点），
> 必然与物料管线重合——这是实际制图中常见的错误。接到执行机构就自然避开。

### 5.4 仪表圆圈安装方式
`mount` 取 `field`（空心圆）/ `panel`（圆+水平线）/ `dcs`（圆+内接方）/ `plc`（圆+内接菱形）。
位号按惯例**分两行**置于圈内：上排功能字母、下排回路序号。

> ⚠️ 这是**通用规则**；具体含义须以 **HG 20505 原件**为准（该标准为扫描件，见 `standards-index.md`）。

### 5.5 位号校验
`validate_tag()`：结构须为「功能字母组 + 回路序号」；首位字母须是被测变量（`MEASURED_VARS`）；
后续字母须在功能码表（`FUNCTIONS`）内。

### 5.6 meta 契约：写 meta 必须配「消费方清单」

符号函数往 `Group.meta` 里写的**每一个键**，都是「画符号」与「校验器」之间的**接缝**。
接缝最典型的失效是：新符号写了个新键（如 `actuator="self"`），老校验器不认，
于是**要么误报**（把合规图判成错），**要么漏报**（真问题查不出来）——两者都只在现场才炸。

因此每条 meta 键**必须**在 `chemcad_engine.META_CONTRACT` 登记三件事：

```python
"actuator": {
    "purpose":  "调节阀传动形式（HG 20519.2 §3.3.3 必注）",
    "values":   set(ACTUATOR_KINDS),        # 值域；None = 自由文本
    "consumer": "validate_control_valves",  # 消费方清单（可给元组，多个）
},
# 暂无校验器执行的键，必须显式申明，否则扫描判为「声明了却无人执行」：
"handwheel": {"purpose": "...", "values": {"True","False"}, "consumer": "", "informational": True},
```

**新增符号 / 新增 meta 键的操作规程**（两条，缺一不可）：

1. 在 `META_CONTRACT` 里补一条，写明 `purpose` / `values` / `consumer`；
2. 值域**优先引用常量**（`ACTUATOR_KINDS` / `FAIL_ACTIONS`），
   让校验器与契约**共用单一事实来源**，杜绝两份枚举各改各的。

> 这不是靠自觉：见 §11.2，`selftest.py [0]` 会自动对账，漏登记直接失败。

---

## 六、正交布线（布线铁律的可执行化）

`ortho_path(p1, p2, mode, via)` 只产生水平/竖直段，**斜线在源头不可能生成**。

| `mode` | 走法 | 用途 |
|---|---|---|
| `auto` | 沿主轴优先（H 或 V 先） | 默认，最像人工画法 |
| `HV` / `VH` | 强制先水平/先竖直 | 需要固定让线走到某侧时 |
| `H-mid` / `V-mid` | 三点式（走中线） | 绕行设备 |
| `via` | 显式中间点序列，如 `[[258.5,545],[620,545]]`；元素中用 `"x"`/`"y"` 表示「该维取目标值」 | 规避与其他线的重合或穿越 |

`validate_orthogonality()` 对 `PIPE_PRI` `PIPE_INLINE` `AF_PIPE` `SIGNAL` `仪表线`
五类图层逐条验算偏差角，>0.5° 即报错。

> **符号内部的斜线不算违规**（阀门蝶形、剖面线等），`check_dxf.py` 也单列统计，不误报。

### 6.1 A* 避障布线（`chemcad_route`）

矩形设备本体及文字构成硬障碍，阀门/仪表与已占走廊为软偏好。候选坐标取障碍边界、端点、区域边界；四向A*以`(网格点,进入方向)`计长度、折弯和软障碍代价；h加入依方向计算的0–3次剩余转弯下界，同f值优先较小h，保留其它候选，不剪掉暂时远离终点的方向。

先试局部区域、默认260mm半径，再覆盖内容边界；所有保留1.5mm净距的区域失败后才归零净距重试，候选坐标与碰撞净距同步归零。**设备始终不可穿**，禁止`relax=2`。有限网格与预算可能无解，不承诺任何图纸均可求解或全局几何最优。

接管方向用四个正交单位方向，解析计算所属设备出口与邻设备入口，不用点采样跳过薄设备；受阻时缩短接管，仍未出本体则报告失败。主搜索从本体外开始，不挖锚点窗口。固定流股参数框与编号不可独立挪动；上轮记忆坐标可标为过期，重建后重新选位并校核。

`route_pipes`复制选项，避免边界污染后续图纸。无解不生成假直线，调用方停止自动出图；算法异常不静默退回旧走线。`audit_routes`同时检查无解、穿设备、压文字，任一项存在均不能报通过。规格显式`route/via` > A*完整路径 > 迭代提示 > ELK折点，最终仍按完整图纸校核。

```python
res = RT.solve(p1, p2, obstacles, opts, start_dir=d1, end_dir=d2, carve_for=(a, b))
routes = RT.route_pipes(cd, spec, extra_hard=上一轮文字位置)
audit = RT.audit_routes(cd, routes)
```

视频学习、时间点、反例、25项寻路回归和验证边界见 [寻路改进记录](routing-video-study.md)。当前`cost`只含搜索中段，不能冒充两端接管在内的总工程代价。默认净距、权重是技能参数，不是视频给出的尺寸。

---

## 七、版面自动居中（`fit_to_sheet`）

图元全挤在左上角、下半张图空着——自动校核查不出来，但评审一眼就能看出来。
`fit_to_sheet()` 把图面内容整体**平移**到可用区域中央。

**只平移、不缩放**，因为缩放会破坏文字高度（2~3 mm 硬要求）与图层线宽；平移则不受影响，
正交关系也天然保持。内容超出可用区不缩放，只报告 `overflow` 由人决定。

图框/标题栏/说明框属于「图纸家具」，用**图纸坐标**表达，**不参与平移**——所以它们在
`draw_pfd.build()` 的第 9 步（居中之后）才落。

---

## 八、控制回路：定义即布线

这是本引擎与「用脚本把图形堆出来」的本质区别。

`loop` 定义**同时是数据源与绘图源**：`wire_loop()` 依据它自动布置仪表圆圈、画测量引线
（走 `仪表线`）与控制信号线（走 `SIGNAL`），全部正交、全部接到执行机构。
**闭环不是事后检查出来的，而是画不出来不闭合。**

`validate_control_loops()` 逐条核对：

1. 五要素齐备（被控对象 / 测量点 / 控制器 / 最终控制元件）；
2. 位号首字母 = 被测变量，且**同回路共享序号**；
3. 测量点**真落在**管线或设备几何上（用 `GeometryIndex` 按点—线段距离判定）；
4. 最终元件是调节阀符号，且**落在实际管线上**；
5. 串级：`sub_measurement` 必填、必须落在几何上、且不得与主测量点重合；
6. 前馈-反馈：必须给出扰动测量点；
7. 分程：最终元件 ≥ 2；
8. 多冲量：独立测量点 ≥ 2（锅炉三冲量：液位+蒸汽流量+给水流量）。

回路类型：`single` / `cascade` / `feedforward` / `split` / `multi_impulse`。

> 各类方案的**骨架、11 类设备控制要点、以及可直接粘贴的 `loops` 规格片段**，
> 见 `references/control-schemes.md`。（旁路**不是**独立 `loop_type`——它是「真实旁路管 + 其上的单回路」。）

---

## 九、规格（JSON）schema

### 9.1 引用语法（`from` / `to` / `at` / `measurement_point` 等通用）

| 写法 | 含义 |
|---|---|
| `"T-101.top"` | 设备 id + 锚点 |
| `"T-101"` | 设备基点 |
| `"PL-FEED@0.35"` | 管线上按**累计长度比例**取点 |
| `"123.5,300"` | 字面坐标 |

### 9.2 顶层字段

| 字段 | 说明 |
|---|---|
| `name` / `sheet` | 图纸名 / 幅面（A0~A3，默认 A1） |
| `title_block` | 标题栏字段，键见 `DEFAULT_TITLE_FIELDS` |
| `equipment` | `[{id, type, at, tag, name, params, rot, tag_annot, tag_height}]`；默认3mm。近设备短位号中心在外轮廓中心竖轴最低交点下方10mm；底部另列分数式位号/名称，位号中心距流程图文本框底边60mm，名称默认再低5mm，同设备中心X。不生成集中设备位号表。none仅关闭近旁标注；folded不列底部条目。量尺与出图共用轮廓求交，固定标注不可挪动，重叠通过设备X及管线重建解决 |
| `pipes` | `[{id, from, to, layer, dn, grade, medium, route, via, arrows, tag_at}]` |
| `valves` | `[{tag, type, on:"管线号", t, rot?, params}]`——`rot` 省略时**自动随管线走向** |
| `instruments` | `[{tag, kind, at | on+t, mount, rot, params}]` |
| `signals` | `[{from, to, layer, route, via}]`——回路定义之外的自由引线 |
| `loops` | 见 §8，可带 `controller_at` / `sub_controller_at` |
| `offpage` | 跨图接续符 `[{at, direction, drawing_no, kind, dest?, stream_no?, stream_data?}]`——`kind`=cross/same/plant（实测 Drawing1.dwg 三种画法）；`drawing_no` 答"去了哪张图/工段"，`dest`（来去归属文字，如"去T0301"/"自总厂来"）答"去/来自哪台设备"（真实参赛 PFD 的 `5-来去归属文字` 层画法），`stream_no`/`stream_data` 是**分割流股的物流标注**（"流股号 + T/P/流量三行参数框"，数据取自 `.bkp` 的 `streams`，不自造） |

> **`offpage` 的两条落笔纪律（实测血的教训，别省）**
> 1. `at` 通常是设备 id，解析出来是设备**中心**——直接落笔会把整只接续标志
>    画在设备身上。`add_offpage_connector` 接 `body/avoid/bounds` 后会
>    **先算 84 个候选位（4 方向 × 7 档垂直偏移 × 3 档外推）再落笔**，
>    把框+箭头+物流标注一起推到设备本体之外。`direction="auto"` 时方向由版面空闲决定
>    （拆张规格一律写 `auto`——拆张阶段还不知道设备最终落在版面哪一侧）。
> 2. 物流参数框只采用截图中的F/T/P三行两列样式，固定25×15mm、列宽10/15mm、行高5mm、字高3mm；菱形顶点竖直引线长5.5092936mm接框左下角。编号只在管线上菱形内，目的地属于接续标志；相态、密度、分率等字段不进入管旁/接续参数框，保留在独立物料表。
| **`drawing_type`** | 只有 `"pfd"` 合法（写别的也会落到 `"pfd"`；本技能不含 P&ID），见 §9.5 |
| **`streams`** | PFD 的流股数据 `{管线号: {no,T,P,W,phase,…}}`，见 §9.5 |
| **`title_block_pfd`** / **`title_block_pid`** | 按图型覆盖 `title_block` 的同名字段 |
| **`notes_pfd`** | PFD 专用说明（不给则复用 `notes`） |
| `notes` / `notes_at` / `notes_width` / `notes_title` | 说明框（见 §9.3） |
| `texts` | 自由文字 |
| `auto_fit` / `auto_fit_center` / `margin` | 版面居中（默认开启） |
| **`layout`** | **版面分配与三类缺陷修正**，见 §9.4；详述见 `layout-and-collision.md` |
| **`sections`** | 显式工段表 `[{id, name, equipment:[设备 id…]}]`；不给则自动划分（§9.4） |

**顺序即依赖**：设备 → 管线（按设备锚点接）。示例见 `assets/pfd-spec-example.json`；
**不给坐标**的写法见 `assets/pfd-spec-autolayout-example.json`。

流股标注尺寸来源是 `assets/stream-annotation-dimensions.json`；横向图样见 `assets/stream-annotation-reference.png`，竖向右侧框见补充 `assets/stream-annotation-vertical-reference.png`，复用既有毫米尺寸。旧 `stream_diamond=false` / 改比例会明确拒绝。设备短位号、底部分数式标注、物流参数框及编号/箭头净距见 [三份PFD版面学习](pfd-reference-study.md)。排版量尺含固定标注包络，物理设备与管口保持原块；整框碰撞、固定10mm位置及底部位号60mm位置都是出图硬校验。裸数采用既有表头单位，带单位的值采用无预设单位表头，整表和工段表使用同一量尺规则。

`chemcad_dwg.calibrate_equipment_footers`在COM保存过程中按实际SHX包围盒补偿底部文字，复测至0.001mm公差；位号、名称的原生包围盒中心分别为框底加60mm和默认加55mm。因此DWG回读不能要求TEXT对齐点Y恒为60/55，须结合实际文字包围盒测量核对。源DXF与纯Python版面包络仍为对齐坐标代理。

### 9.4 `layout`（版面分配）与三类版面缺陷

```jsonc
"layout": {
  "mode": "auto",          // off（默认）| report（只诊断不改图）| auto（分配 + 迭代修正）
  "sheet": "auto",         // 或 A3/A2/A1/A0；auto = **从 A1 起试**（本技能标准图幅，
                           //   取自浙工大 PFD 实测：33 个图框清一色 841×594），装不下才升 A0。
                           //   不是"挑最小装得下的"——那会把 9 台设备的图判去 A3，与样本口径不符。
                           //   见 `chemcad_layout.SHEET_AUTO_ORDER`
  "max_per_section": 6,    // 工段设备数上限，超过就继续"层序平衡对切"
  "section_gap": 26,       // 可选：工段间距。**显式给了就照用**（倍率不再乘它）；
                           // 不给则由引擎按 26 × 间距倍率定（上限 72）
  "force": false,          // true = 连写了 at 的设备也一并重排
  "enforce": false,        // true = 即使不自动排版，三类缺陷也记 error
  "area": [16, 16, 578, 359]   // 可选：直接指定排版作业区，绕过幅面推算
}
```

五段式：**① 工段划分**（显式 `sections` → 设备 `section` 字段 → 流股连通性 + 层序平衡对切）
→ **② 设备数量统计** → **③ ELK layered 版面分配**（每段建图 → 破环 → 分层 → 折行 →
交叉最小化 → Brandes–Köpf 层内排布 → 端口 → 正交布线；搜索 **4 档案 × 7 间距档 × 2 段排布
= 56 次真排**，代价 **8 项**）→ **④ 探测三类缺陷并迭代修正** → **⑤ 按计划平移落位**。

> ⚠️ **ELK 参数不在规格里暴露**（间距档/档案/折行）：它们是引擎自有旋钮，由代价函数在搜索里定。
> 规格只表达**工程意图**（分几段、要不要自动排版、用哪张图幅）。
> 两个例外是**显式覆盖**：`layout.section_gap` 给了就照用（倍率不再乘它），
> `pipes[].route` / `pipes[].via` 给了就不被 ELK 折点覆盖——都是"设计者明确表态"的情形。

**为什么流程方向是"解出来"的**：分层阶段只有一条硬约束——**每条管线至少跨一层**；
层号是这张 DAG 的可行解。所以"哪台设备在第几层"来自流股拓扑，不是靠"猜行数"排出来的。
回流线（塔顶回流、再沸器返塔、循环物流）由**破环阶段**统一反转，不会把塔系劈开。

**走线不穿设备为什么是结构保证**：层内每个节点的纵向区间互不重叠 ⇒ 水平段沿某节点的 y 走
绝不会碰到同层另一台设备；竖段一律落在**层间隙**里（那里没有节点）；长边走虚拟节点链，
中间那几段走在**空着的那一格**上。详见 `layout-and-collision.md` §5.3 / §6.1b。
默认走线优先：**规格显式`route/via` > A*完整路径 > 迭代修正提示 > ELK折点**。自动寻路失败会停止，不退回旧走线。

三类版面缺陷各有结构化探测器，`preflight` 按 `layout_enforced` 决定记
`warning`（手工摆位）还是 `error`（引擎承诺排版）：

| 探测器 | 判据要点 |
|---|---|
| `detect_symbol_overlaps` | 设备用**本体**包围盒（不含接管法兰），否则"离接管 1 mm"会被误判成压到设备上 |
| `detect_text_over_lines` | 文字必须有**真面积**（`text_rect()` 含旋转）；封闭框内的文字不判压框线 |
| `detect_pipe_through_equipment` | 首尾各让 `NOZZLE_TOLERANCE=6 mm`（管口本来就插进设备体内） |

**接管段走线**（`mode` 以 `stub` 开头，如 `stubVH`）：两端先沿**锚点出线方向**各走
`NOZZLE_STUB=9 mm` 再转弯。塔顶接管在封头正上方，`auto` 走线却会按 `|dx|` 优先"先横"，
从封头处横着穿出塔体；接管段从源头消除这一类穿透，且全程仍正交。方向由
`anchor_direction()` 判定：先看锚点落在**本体**哪一侧，判不出再用锚点名兜底
（`top` 的坐标落在接管内部，几何上判不出朝向）。

⚠️ **`fit_to_sheet` 平移整张图**：要写进"下一轮重建时复用"的**绝对坐标**，
必须先减掉 `chemcad_layout.unfit_shift(cd)`；否则偏移量正好是两次平移之差
（实测：绕行点算对了，重建后管线跑去了别处，仍然穿塔）。

### 9.5a 工段框、默认文本框与独立物料表

PFD不输出工段角标或工段标题，避免游离L形线与参数框叠字；工段划分仍用于布局和右侧物料表。文字之间的重叠与流股端点断开均作为出图错误检查。接管取源块实际几何，离心泵左侧入口、顶部出口；框内连接点不代表机械选型批准。

流程图不生成图例，物料平衡表不放入流程图区。默认A1文本框采用用户的 `C:/Users/Administrator/Documents/CAD文本框.dwg`，尺寸841×594mm。源DWG在 `assets/default-text-frame-source.dwg` 留存，原几何归一化为 `assets/default-text-frame.dxf` 的 `DEFAULT_TEXT_FRAME_A1` 块；只平移不缩放，保留原生MTEXT、POINT、LINE、LWPOLYLINE。原模板中的图名、图例标题和张数改为按图用途与实际张数填写，源文件不改写。字体依赖仍为gbenor.shx与gbcbig.shx。该用户模板只提供A1；显式其他幅面沿用原有幅面生成路径，不把A1模板放大冒充原框。

独立物料框在流程图右侧，底边Y=0一致，框间净距150mm。A1两框插入点分别为(0,0)、(991,0)。`chemcad_book` 装订时测量流程框与物料框的完整外包络，下一组与当前组保持至少150mm横向净距。

物料表使用 `add_balance_table`：物流沿横向列、参数沿纵向行，表格字高3mm。保留已提供的质量流量、分率、密度、体积流量、T、P、相态和相态分率；有F时增加摩尔流量行。带单位的值使用无预设单位标题；裸数仍依赖规格约定的单位。缺值留空，不捏造数值。

`section_stream_boxes=true` 按工段分组，界区与未归属物流另列；关闭则汇总为整表。`section_box_max_rows` 为兼容字段，在转置表中限制每张表的物流列数，最多25列；文字过宽时按实际列宽拆成框内多表。多表向下排列，但始终只有一个独立框。先量尺全部表，再统一落笔；单框放不下时明确报错，禁止续框或缩字挤入。`legend`、`legend_at`、旧 `stream_table_at` 不再影响流程图中的放置。

### 9.5 只有 PFD 一种图型

`spec_for_type(spec, "pfd")` **裁剪**的是"控制与仪表层"，不是"工艺层"：
去 `valves`/`instruments`/`loops`/`signals`，管线全留但**注记换成流股号 + 主要参数**、
`arrows` 提到 ≥1；设备与 `layout` 原封不动。

| PFD 的口径 | 做法 |
|---|---|
| 管线注记 | `add_stream_data(placement="top"/"right")`：25×15mm两列F/T/P三行框，10/15mm列宽、5mm行高、3mm字高。横向从菱形顶部引线，竖向从右顶点向上接右侧框；10mm边长菱形和5.5092936mm外露引线固定。整框边界/栏线/空白都是管线禁区，框、引线和菱形检查净距3mm。箭头和交叉断口均避让编号；无直线净距或无解时报错 |
| 标题栏 | 自动加「工艺流程图（PFD）」 |

唯一入口 `draw_pfd.build_pfd(spec)`：先按 PFD 裁剪，再走完整出图链路
（排版 → 迭代修正 → 幅面复核），返回 `(cd, loops)`。`build_with_layout()` 内部
**也**硬按 PFD 裁剪（`cur = spec_for_type(spec, resolve_drawing_type(spec))`），
所以老规格里留着 `drawing_type:"pid"` 也不会冒出 P&ID。
需要版面报告时用 `build_with_layout()`（多返回一份 report）。

`build_both` / `build_types` / `TYPE_SUFFIX` / `-PID|-PFD 后缀` 已删净：
只有一种图型时，"两图共用幅面、坐标对不上"这个问题根本不存在，
那套机制连同它的测试一起退场。

`layout.sheet="auto"` 另有**幅面实测**一层：`plan_layout` 挑幅面只能在排版期估计，
而仪器圆、引线、回路位号、流股数据块当时还没生成；整图收敛后再复核
"内容压没压到说明框 / 标题栏"（`_needs_bigger_sheet`），压到了就换大一号重排。
`fit_to_sheet` 的 `overflow` **看不到说明框占掉的那一块**——内容塞得进幅面却压在说明框上时，
它的 `overflow` 仍是 False（实测踩过），所以三条判据都要看。

### 9.3 说明框的两条排版约定（都踩过坑）

1. **编号按"逻辑条目"给，不按"折行后的物理行"给。**
   `notes` 里一条就是一条，无论折成几行。早期实现按物理行编号，结果 4 条说明被画成 5 条
   编号，其中一条是上一条的残句（`3. 00）。`）——图能出、`preflight` 也 0 错误，
   但图面是错的。**校验器不查这个，只能靠 `selftest.py [6]` 钉住。**
2. **续行悬挂缩进**到首行正文起点，一眼能看出是上一条的延续。

如果条目**自带**编号（`1.` / `（1）` / `1）`），则全部按自带编号渲染、引擎不再加号
（混着用会撞号）；要交给引擎编号，就先把自带编号删掉。
折行**不切断 ASCII 词**：`150x100`、`HG 20519` 不会被劈成两半（按单元折行，单元超长才退化为字符硬切）。

---

## 十、后端与通道（出图 / 读回 / 命令路由 / 文件侧提取）

> 本节不止讲"后端"。四条**方向不同**的通道都在这：
>
> | 小节 | 方向 | 一句话 |
> |---|---|---|
> | §10.1–10.4 | 出 → 图 | 四个**出图后端**（DXF / COM / SCR / LISP），吃同一份 IR |
> | §10.5 | 图 → 信息 | COM **读回**：读**运行中的 DWG**（需 AutoCAD 开着） |
> | §10.6 | 图 → 信息 | **文件侧**提取：读**落盘 DXF / `.bkp`**（零依赖、离线） |
> | §10.7 | 兜底 | 引擎没建模时，**驱动 AutoCAD 原生命令**的路由规则 |

### 10.0 两条硬规定：交付格式 = DWG，出图通道 = AutoCAD COM

> 用户规定（2026-09-23）并写进技能：**PFD 一律以 DWG 交付**，且**必须经 COM 连到 AutoCAD 出图**。
> 落点是 `draw_pfd.resolve_backend()`（默认 `dwg`，不看扩展名）与
> `chemcad_dwg.com_session()`（默认只认已在运行的实例）；自检 `[7] ⑦a / ⑦b` 钉住。

```bash
python scripts/draw_pfd.py --check-tools          # 体检：COM 状态 / AutoCAD 版本 / 逃生口
python scripts/draw_pfd.py spec.json --out pfd.dwg            # COM → DWG（默认）
python scripts/draw_pfd.py spec.json --out pfd.dwg --create   # CAD 没开，让技能起一个
python scripts/draw_pfd.py spec.json --out pfd.dxf --backend dxf   # 显式逃生口：要 DXF
```

| 规定 | 落点 | 违反时 |
|---|---|---|
| **格式 = DWG** | `resolve_backend()` 默认 `dwg`；`--out 图.dxf` 由 `_as_dwg_path()` 改写成 `图.dwg` | 不会静默出 DXF |
| **通道 = COM** | 出图前 `require_com()` 体检，再由 `dxf_to_dwg_com()` 打开另存 DWG | 连不上 → 退出码 3 + 可执行建议 |

**两条"不许静默"**（这是本节最要紧的一条经验）：

1. *不许静默给 DXF*——要求 DWG 就必须得到 DWG。"交付格式不对但没人知道"比"出不来图"更糟：
   下游拿 DWG 的人会直接崩，而且是在**他那边**崩，你这边一切正常。
2. *不许静默换通道*——COM 与 accoreconsole 的**产物不等价**：COM 画进用户眼前那张图纸
   （图层/图块/字体与他的样板一致），accoreconsole 是无界面核心环境、用默认样板，
   线型 `HIDDEN`、字体、图层标准常常对不上。用户按 COM 的心智模型去用图，
   实际拿到另一条通道的产物——这种错要到下游才爆。

所以 `accoreconsole` 从"默认通道"降为**显式逃生口**：只有 `CHEMCAD_DWG_BACKEND=accore`
时才走；`converter_report()["default_backend"]` 恒为 `"com"`。

**出图后端总表**（`dwg` 是默认，其余都要显式指定）：

| 后端 | 何时用 | 特点 |
|---|---|---|
| `--backend dwg`（**默认**） | PFD 交付 | 纯 Python 写 DXF → **COM 连 AutoCAD** 另存 DWG；连不上就退出码 3 |
| `--backend dxf`（逃生口） | 中间件 / 明确要 DXF | **R2010**，UTF-8，中文不经转义；图层带**真线宽 370**；图块按几何指纹去重 |
| `--no-ezdxf`（内置 R12 书写器） | 没装 ezdxf / 需要最老版本兼容 | 零依赖，AC1009，任何 CAD 都能读；**无 370 线宽**，粗/中粗/细改由**图层颜色**承载（配打印笔宽表） |
| `--backend com` | 要直接画进运行中的 AutoCAD | 复用 pyautocad 调用模式 + 图块 + Undo 标记 + 只在最后 Regen 一次；支持 `--clear-layers`/`--purge`/`--doc` |
| `--backend scr` | 生成脚本，在 CAD 里 `SCRIPT` 跑 | 零依赖；**展平**为 `-LAYER S`/`LINE`/`CIRCLE`/`ARC`/`PLINE`/`-TEXT` 命令序列（命令行 `-BLOCK` 需交互选择，故不建块）；默认 GBK 编码 |
| `--backend lisp` | 生成脚本，`APPLOAD` 后输命令名 | 零依赖；`(entmake …)` **含真 BLOCK/ENDBLK/INSERT**（entmake 绕过命令提示符）；图块按几何去重 |

`render(d, path, backend, **kw)` 是统一派发入口：`dxf`/`scr`/`lisp` 走 `chemcad_scr`，
`com` 走 `ComBackend`，`dwg` 走 `write_dxf` + `chemcad_dwg.to_dwg`；
各通道吃的是**同一份 IR**，因此几何一致。`dwg` 额外认 `create_if_not_exists`
（透传给 `to_dwg`，控制 COM 连不上时是否替用户启动 AutoCAD）。

### 10.1 DXF 编码陷阱（实测踩到过）
`ezdxf.new("R2000")` 会把非 ASCII 写成 `\U+XXXX` 转义，**且 ezdxf 自己不还原**——
往返一圈图层名就变成字面量 `\U+4eea\U+8868`，第三方工具链会失真。
**结论：默认 R2010（UTF-8，中文原样写）**；仅在需要兼容老版本时降到 R2007 以下，
此时引擎会主动给出提示。

内置 R12 书写器仍按 R12 惯例用 `\U+XXXX`，并且**图层名、图块名、文字全部转义**
（漏了图层名会直接 `UnicodeEncodeError`，这是实测报错点）；`inspect_dxf.py` / `check_dxf.py`
会自动还原。

### 10.2 COM 惰性连接陷阱
`pyautocad.Autocad()` **构造时不连接**，直到访问 `.app` 才连。因此「构造成功」不代表
AutoCAD 可用。`ComBackend.connect()` 显式摸一次 `doc`，把问题在动手画之前暴露，
并给出三条可执行处理（先开 AutoCAD / 加 `--create` / 改用 `--backend dxf`）。

`create_if_not_exists=False` 时**不会**擅自启动 AutoCAD（`GetActiveObject` 失败即抛错）。

### 10.3 重试范围：只重试"环境噪声"，不重试自己的 bug

`com_retry` / `is_com_busy` 是 COM 调用的护栏。关键设计：**只对 COM busy 类错误重试**——
按 `hresult ∈ COM_BUSY_HRESULTS`（如 `0x80010001` RPC_E_CALL_REJECTED）或消息关键字判定；
其余异常**立即抛出**。

> **踩过的坑**：早期版本对任意 `Exception` 重试 10 次，把调用方自己的 `TypeError`/`KeyError`
> 也吞成"偶发 COM 故障"。护栏一旦过宽，真 bug 就被掩成玄学问题。

### 10.3.1 COM 被拒还有一种真因：AutoCAD 停在「开始」选项卡（永远不静默）

现象：`app.Name` / `app.Version` / `app.HWND` 都取得到，但 `Documents.Count` / `.Open` /
`.Add` 一律失败——`com_error(-2147418111, '被呼叫方拒绝接收呼叫。')` 或
`AttributeError: <unknown>.Open`。窗口表里**没有** `#32770` 模态对话框，
`GetAcadState().IsQuiescent` **恒为 False**，而且**等再久也不会自己好**
（不是 §10.3 那种"忙一会儿"的环境噪声）。

真因（2026-09-24 实测，AutoCAD 2026 / `25.1s`）：**活动页是「开始」选项卡时 AutoCAD 不静默**。
人工解法是在 AutoCAD 里点一下任一图纸页标；代码解法见 `chemcad_dwg._wake_autocad(app)`：
纯 Win32 取 `app.HWND` → 找 `MDIClient` → 枚举 MDI 子窗，跳过「开始 / Start / 起步」，
用 `WM_MDIACTIVATE` 激活第一张真图纸。**只切焦点，不关图纸、不点按钮、不敲键**；
拿不到窗口就安静返回 `False`，交给原来的报错文案兜底（并追加"在 AutoCAD 里点一下图纸页标"）。

重试顺序：`_is_com_rejected` 认出是拒收 → 先 `_wake_autocad` → 再按 `COM_RETRY` 重试；
自救成功会在报告里留一条 `waked`，免得"图出来了但不知道为什么刚才不行"。

### 10.4 脚本通道的两条约束（都实测踩到）

1. **SCR 不建块**：命令行 `-BLOCK` 需要交互选择对象，脚本里无法可靠选中，故 `to_scr()` **展平**为
   逐实体命令；要真图块用 LISP 通道（`entmake` 不需要交互）。
2. **`_plan()` 形参勿与局部变量同名**：早期形参 `blocks` 与局部图块字典 `blocks` 同名，
   局部被遮蔽 → `and blocks` 恒假 → 图块去重静默失效、全部展平。改名 `use_blocks` 后修复，
   并由 `selftest.py [8]` 断言「LISP 的 BLOCK/ENDBLK 计数 == 结构数、括号平衡 = 0」。

### 10.5 COM 读回通道（提取，与出图互为逆过程）

| 能力 | 位置 |
|---|---|
| 列文档 / 切文档 / 按名找文档 | `_MinimalCom.list_documents` / `activate_document` / `find_document` |
| 按层取实体 / 选择集 / 计数 / 清层 / 清空图层 | `entities_on_layer` / `select_by_layer` / `count_on_layer` / `clear_layer` / `purge_empty_layers` |
| 动态块**有效名** | `effective_block_name`（取 `EffectiveName`，不是块表 `Name`） |
| 结构化读回报告 | `ChemComReader.read()` / `format_report()` |
| CLI | `scripts/read_dwg.py --list-docs` / `--doc <关键词>` / `--layer <名> [--json]` |

`unformat_text()` 负责剥 MText 格式码（`\f…\`、`\C3;`、`\P`、`%%d`→°、`%%c`→Ø、`\U+XXXX`）——
不剥就看不清真文字。`check_layers()` 把读回的图层线宽与 `LW_RANGES` 对照，用于**评审别人的图**。

> ⚠️ `clear_layer` 曾是**静默 no-op**（`_MinimalCom` 没实现，被 `hasattr` 守卫跳过）——
> 现在 `ComBackend.draw(clear_layers=True)` 走三档回退（`client.clear_layer` → `entities_on_layer`
> → `ModelSpace`），失败**显式报告 `clear_failed`**，不再假装成功。`selftest.py [7]` 钉住该回归。

### 10.6 定位与提取（**文件侧**，与 §10.5 互补）

§10.5 读的是**运行中的 DWG**（走 COM，需要 AutoCAD 开着）；
本节的 `scripts/chemcad_locate.py` 读**落盘文件**（DXF / Aspen `.bkp`），**零依赖**、离线可跑。
完整用法见 `references/locate-and-extract.md`。

| 能力 | 函数 |
|---|---|
| 编码逐级降级读取（`utf-8-sig → utf-8 → gbk → latin-1`） | `read_text_robust` / `is_lossy` |
| DXF 作用域切分（模型空间 / 各块定义 / 各表） | `split_dxf_scopes` / `parse_scope_entities` / `load_dxf` |
| 同 id 去重（保序留"更完整"者） | `dedupe_keep_best` |
| 折线首尾相接合并 / 依拓扑定向 | `merge_paths` / `orient_path` / `path_length` |
| 端口反推符号半尺寸（中位数抗噪 + 全场基准收敛） | `infer_symbol_sizes` |
| 线端裁到真实轮廓（圆 / 矩形 / 多边形二分） | `clip_to_outline` / `clip_circle` / `clip_rect` / `clip_poly` / `point_in_poly` |
| 点拾取（定位"这个点上是哪个对象"） | `pick_at` |
| 文字列扫描排序 / 两层伪装剥净 / 估宽 bbox | `order_column_scan` / `strip_mtext_codes` / `unescape_dxf` / `text_bbox` |
| 间距测量（中心距 + 包围盒净距） | `closest_distance` / `point_distance` |
| Aspen `.bkp`：模块定位 + 流股拓扑 + 工艺结果 + 单位集 | `parse_bkp_case` 及其子函数 |

**三条设计纪律**（都是"提取不报错但内容是错的"那类静默失真的解药）：

1. **先切作用域再取对象**。一个 `.bkp` 可能存多页 PFD（顶层 + 层级块各一页，内部模块/流股
   再存一份且**不带前缀**）；一个 DXF 的 `BLOCKS` 段里也会出现与图面同名的对象。混读 →
   对象出现两次、位置还对不上。DXF 侧段名必须取 `SECTION` 的**下一对** `2/<段名>`——
   写成 `pairs[i+2]` 会把下一个实体类型当段名，`BLOCKS`/`ENTITIES` 全认不出、作用域数静默变 0。
2. **不用 `errors="replace"`**。它把编码错配变成看不见的 `U+FFFD`，正则照跑、中文全废。
   逐级降级读，兜底层（latin-1）用 `is_lossy()` 显式标记"中文不可信"。
3. **不静默**。解析不出来的一律进 `unresolved` 列出；文字必须给**有面积的 bbox**
   （按 `中文=1 字高 / ASCII=0.55 字高` 估宽，**与 `chemcad_symbols._text_w` 同一条规则**），
   否则拾取/排序/测距在文字上全退化成"一个点"且不报错。

`selftest.py [13]` 覆盖本组全部断言（含上面那个 off-by-one 的回归钉，以及
「`unresolved` 不得夹带管径/设备规格」的范围约定钉）。
历史自检记录为 **25 组 / 343 条**断言；本次旧整套在直接COM简化原生块的测试处停止，不作为当前通过证明。当前针对性运行命令：`python scripts/test_chemcad_route.py`（25项）与 `python scripts/test_pfd_annotations.py`（32项），含两个自带示例完整建图；本轮合计57项通过。原断言范围（`[0]`~`[24]`；`[14]` 把组数与条数回写进文档对账，见 §11.3）。

<!-- 历史 SELFTEST-CLAIM: 25 组 / 343 条；旧整套未通过 -->  <!-- 与 `selftest.py [14]` 组自动对账：改代码而不同步这里，自检会红 -->

CLI 退出码：`0` 成功 / `2` 文件不存在 / `3` 其他异常 / **`4` 查无此物**（未命中≠程序出错）。

### 10.7 驱动 AutoCAD 原生命令的路由（引擎覆盖不到时的兜底）

本引擎只生成「线/圆/弧/多段线/实心三角/文字/图块」。一旦需要**引擎没建模的命令**
（`TRIM`/`EXTEND`/`FILLET`/`CHAMFER`/`HATCH`/`SPLINE`/`COPY`/`MIRROR`…），走 COM 时有两条**硬规则**：

| 规则 | 说明 |
|---|---|
| **有 `ModelSpace.AddXxx` 或实体方法 → 走纯 COM** | `LINE/XLINE/RAY/PLINE/MLINE/POLYGON/RECTANG/ARC/CIRCLE/SPLINE/ELLIPSE/POINT/MTEXT/TEXT/HATCH/REGION/BLOCK/INSERT/ERASE/COPY/MIRROR/OFFSET/ARRAY/MOVE/ROTATE/SCALE/EXPLODE/DIMANGULAR/DIMSTYLE/LAYER/ZOOM/REGEN/QSAVE` 共 33 条，零挂起 |
| **只有交互/查询类才走 `SendCommand`，且必须先程序化预选** | `STRETCH/TRIM/EXTEND/BREAK/JOIN/CHAMFER/FILLET/BOUNDARY/DIMLINEAR/DIMALIGNED/DIMRADIUS/DIMDIAMETER/DIMCENTER/QDIM/PROPERTIES/MATCHPROP/LIST/PAN/DIST/AREA` 共 20 条。**绝不可裸发 `_CMD`** 让 CAD 等交互输入——会卡死；必须先 `SelectionSets` 预选对象并设 `PICKFIRST` |

几条实测坑（都是"看起来该成功、实际不成功"的）：

* **`DIST`/`AREA` 不要用命令**：自动化下直接用 Python 算坐标/属性更快更稳
  （文件侧用 `chemcad_locate.closest_distance`）。
* **`HATCH` 必须两件事**：`AppendOuterLoop` 传**边界实体对象数组**（不是点坐标），
  且之后**必须调 `Evaluate()`**——否则 `AddHatch` 成功但图上什么都看不到。
* **`COPY` 用 `entity.Copy()`**，不要用 `Document.CopyObjects`（后者常报"对象数组无效"）。
* **`SPLINE` 读坐标用 `FitPoints`/`ControlPoints`**，`ent.Coordinates` 会抛 `AttributeError`。
* **`REVCLOUD` 无 `AddRevCloud`**，原生 `_REVCLOUD` 直发会卡死 → 用闭合多段线逐段 bulge 复刻。
* **删除实体**：先把待删对象收进 Python 列表，**再**删——边枚举 `ModelSpace` 边删会
  破坏 COM 枚举器并崩脚本（同 §10.3 的纪律）。

---

## 十一、校验器清单（`preflight`）

| 校验器 | 对应规范 |
|---|---|
| `validate_layers` | 图层须在注册表内；两套命名体系不得混用 |
| `validate_lineweights` | 线宽落在粗/中粗/细三档区间（HG 20519.1） |
| `validate_orthogonality` | 物料线/信号线一律正交（布线铁律） |
| `validate_control_valves` | 调节阀必注传动形式 + 气开/气闭（HG 20519.2 §3.3.3） |
| `validate_text_heights` | 图纸数字字母 2~3 mm（HG 20519.1） |
| `validate_tags` | 仪表位号构成与功能字母合法性 |
| `validate_flow_arrows` | 主物料管道须标流向箭头（HG 20519.2） |
| `validate_relief_valves` | 安全阀须注入口管段长度及弯头数量（HG 20519.2 §3.3.4） |
| `validate_control_loops` | 闭环可追踪（§8 的 8 条） |

**三类版面缺陷（关系型）**——`COLLISION_CHECKS` 单独一组，因为它们的严重级别
由「引擎是否承诺过自动排版」决定（见 §9.4）：手工摆位记 `warning`，
`layout.mode=auto` / `layout.enforce=true` 记 `error`。

| 校验器 | 对应判据 |
|---|---|
| `validate_symbol_overlap` | 符号包围盒相交即报；`SYMBOL_CLEARANCE=2 mm` 内另记"过近" |
| `validate_text_over_line` | 文字框与线相交；`TEXT_LINE_GAP=0.2 mm` 内不算压线 |
| `validate_pipe_through_equipment` | 让开 `NOZZLE_TOLERANCE=6 mm` 后仍在本体内走 > `PIPE_INSIDE_MIN=2 mm` |

> 三个 `detect_*` 是**结构化探测**（返回"谁和谁、差多少、沿哪一轴"），
> 上面的 `validate_*` 只是把明细压成人读的一条。分成两层是为了**单一事实来源**：
> 报告与自动修正读同一份明细、共用同一组阈值常量——否则"校核说没问题、
> 修正却按另一个阈值改"这类漂移迟早出现。

`error` 会让 `draw_pfd.py` 以退出码 1 结束（可用于 CI 把关）；`warning` 只提示。

### 11.1 退出码契约（供 CI / 批处理调用）

| 码 | 含义 |
|---|---|
| 0 | 自检通过且出图成功（`--check-only` 通过同此） |
| 1 | 自检有 `error`——规格或几何不合规，**未出图** |
| 2 | 落盘后端（dwg/dxf/scr/lisp）但没给 `--out` |
| 3 | **COM 通道不可用**（AutoCAD 没开 / 缺 pywin32 / COM 偶发故障）——默认后端 `dwg` 也走这条，见 §10.0 |

**异常路径一律不抛栈**：缺依赖与连不上 CAD 对用户是同一件事——这条通道现在走不通。
两者都归到退出码 3，并给出可执行建议（先开 AutoCAD / 加 `--create` / 逃生口 accore /
改用 `--backend dxf`）。`--check-tools` 同理：COM 可用返回 0，不可用返回 3。
`selftest.py` 的 `[5]` 会逐条钉住这组契约，防止后续改动把 traceback 又放出来。

### 11.2 meta 契约自动对账（`scan_meta_coverage`，启动时最先跑）

契约表（§5.6）是**文档约束**，光靠自觉一定会漂移。所以引擎自带扫描，
用**两个独立事实源**跟契约表三方对账：

| 事实源 | 怎么拿 | 为什么可信 |
|---|---|---|
| **产出方**（符号实际写了哪些键、写了什么值） | `sweep_symbol_meta()`：把库里**每个符号真的落一次图**，走 `cd.place()` 真实路径，收集实际 `meta` | 不是读源码猜，是运行时事实，契约表**没法替符号撒谎** |
| **消费方**（校验器实际读了哪些键） | `_ast_meta_keys(fn)`：**AST 遍历**校验器源码，抽出 `.meta.get("x")` / `.meta["x"]` | 不是手抄清单，消费方清单**永不与代码脱节** |

对账结果 `scan_meta_coverage()` 返回 9 类问题，`selftest.py [0]` **全部断言为空**：

| 字段 | 含义 |
|---|---|
| `unreadable` | 校验器源码取不到（AST 失败）——**按失败处理，不静默通过**，否则扫描形同虚设 |
| `undeclared` | 符号产出了键，但契约表里没登记（新增符号忘了配消费方清单） |
| `violations` | 产出值**不在契约声明的值域内**（如 `actuator='self'` 而枚举只写了三种） |
| `foreign` | 校验器读了键，但契约表没声明（消费方清单过时） |
| `unenforced` | 契约里声明了强约束的键，却没有校验器真的执行它 |
| `mismatch` | 契约写的消费方 ≠ AST 实际读到的消费方 |
| `stale` | 契约声明的键，实际没有任何符号产出（死条目） |

```bash
python -c "import chemcad_engine as E; print(E.format_meta_coverage(E.scan_meta_coverage()))"
```

> **为什么 `[0]` 要最先跑**：它守的是接缝。接缝一旦脱节，后面 17 组测的
> 很可能是「建立在错误前提上的正确」——先证契约，再证行为。

### 11.3 文档规模自动对账（`selftest.py [14]`，最后跑）

`[14]` 是 §11.2 思路的第二次应用：**把留在文档里的数字与声明也变成受管对象**。
（**组号 14 最小，但执行顺序在最末**——对账必须在跑完全部各组之后才有"实测组数"可对。）

| 对账项 | 事实源（怎么拿） | 文档声明处 |
|---|---|---|
| 回归**组数** | 本文件里 `print("\n[N] …")` 的组号个数，并要求 `0..N-1` **连续、无重号/断号** | `SKILL.md`、本文件各一行 `<!-- SELFTEST-CLAIM: N 组 / M 条 -->` |
| 断言**条数** | `check()` 实际累加出的 `CHECKS_RUN`（跑到 `[15]` 时的收口值） | 同上 |
| CLI **子命令**集合 | `chemcad_locate.build_parser()` 的 choices | 三份文档里出现的 `chemcad_locate.py <子命令>` |
| 子命令**都接了处理函数** | choices 里 `func` 是否为空 | ——（防「声明了却没人接」） |
| **作者声明**是否在 | `references/author-letter.md` 存在 + 关键条款子串齐备 + `SKILL.md` 仍引用它 | 本文件与 `SKILL.md` 的引用行 |

三条硬规矩：

1. **锚点只写一处不够**。正文里重复写的 `N 组 / M 条` **同样受管**（`_SIZE_RE` 匹配全部出现处）——
   否则"改了锚点没改正文"立刻变成新的漂移源。**要复述这个数字，就必须进扫描面。**
2. **`[15]` 必须最后跑**：只有跑完全部各组，才知道实际多少组、多少条。
3. **改完要反证**。把锚点数字改掉 / 把正文数字改掉 / 把锚点整行删掉，三种情况都必须让自检**变红**；
   只改一处却不报错，说明扫描面有缺口。

> **HTML 注释锚点在渲染后不可见**：`<!-- SELFTEST-CLAIM: … -->` 夹在文档里不打扰阅读，
> 但它才是"机器可读的那一份"；人手改数字时必须一起改。
> 同理，**作者声明属技能组成部分**——重构/拆分/翻译时只能补充，删条款或删引用即自检变红。

---

## 十二、标准工作流

```bash
# 1) 写规格（可先只写骨架，逐步补）
# 2) 只自检，不出图 —— 改规格时的快速回环
python scripts/draw_pfd.py spec.json --check-only

# 3) 出图（设备若不写坐标，加 --layout auto 走"工段划分 → 数量统计 → ELK layered 分配"）
python scripts/draw_pfd.py spec.json --out pfd.dwg --report report.json
python scripts/draw_pfd.py spec.json --out pfd.dwg --layout auto --layout-report lay.json

# 4) 交付前双重把关：图面自检（引擎） + DXF 复核（独立脚本）
#    三类版面缺陷（符号重叠 / 文字压线 / 管线穿设备）已进 preflight：
#    手工摆位记提示、layout.mode=auto 记错误
python scripts/check_dxf.py pfd.dxf

# 5) 目视检查版面（"丑"不止那三类：疏密不均、层次混乱仍只能目视）
python scripts/preview_dxf.py pfd.dxf --out pfd.png

# 6) 改动引擎后回归
python scripts/selftest.py

# 7) 评审别人的图（读运行中的 DWG）
python scripts/read_dwg.py --list-docs

# 8) 从落盘文件取信息（DXF / Aspen .bkp）——先切作用域，再取对象
python scripts/chemcad_locate.py scopes  图.dxf
python scripts/chemcad_locate.py locate  图.dxf --tag T-101
python scripts/chemcad_locate.py bkp     案例.bkp --json case.json

# 9) 从 .bkp 拆张出 PFD（每图一个工段、每图 ≥6 台、跨张流股带物流标注）
python scripts/bkp_to_pfd.py case.json --out spec.json --split sheets/
```

**从 `.bkp` 拆张的三步工作流**（`bkp_to_pfd.split_spec`，用户指定）：

1. **第一步 —— 按工段划分**：**`.bkp` 位号的前 2 位数字就是工段号**
   （`01`=第一工段、`02`=第二工段……）。`T0101` 属第一工段、`R0201` 属第二工段。
   这是**模型自带的权威划分**，不靠引擎按流股连通性猜——早先一级工段用连通性划分
   （位号前两位只当二级"工序代号"），把储罐这类互无直连管线的设备切成一串
   "每个 1 台"的小段（实测 7 台储罐各自成「储存与缓冲工段 1~7」）。
   ⚠️ 非标准位号（**物料名**如 `C6H6` 苯 / `CH3OH` 甲醇 / `H2O` 水）**不是位号**，
   若照样取数字当工段号会凭空造出"第66工段"（`C6H6`→66）。

   **无编号设备的工段判定——沿流股图 BFS**（确定性规则，不用投票/聚类）：
   * **依据**：`.bkp` 自带区域矩形（`assign_areas`）优先；没命中就沿**流股图**做 BFS，
     取**跳数最少的有编号设备**所在工段；同跳并列取**位号字典序最小**的那台。
     结果唯一、可复现——重跑、换 Aspen 版本都相同。
   * **图上孤立**（无任何连通路径）留 `--`，并逐台记进 `_三步.无编号设备·归段依据`，
     **不猜**；仍受"每图 ≥6 台"约束，由小工段合并兜底。
   * 为什么弃用"邻居投票 + 坐标网格聚类"：两者都带**魔数**（最近 6 台、网格步长 12 mm），
     结论随设备坐标浮动。BFS 只看**连接关系**（模型自带拓扑），跨版本不变。
2. **第二步 —— 按版面容量分配设备**：统计每工段台数 →
   由 `_fit_count` 按**真实符号包络 + 图幅可用区**折行装填，算出**这一张放得下几台**；
   超了就按**位号后两位（工段内顺序号）**切若干子组
   （顺序号顺着流程编，切出来流程连续，不会把塔和它的回流罐劈开）。
   **每张图至少 6 台**（硬约束）：尾组不足 6 台并回上一组；
   单个工段本身不足 6 台则几个小工段**并成一张**。
   ⚠️ **上限不是常数**：一台塔 24×120、一台泵 11×11，同一幅面差一个数量级，
   用**台数**当容量必然切错——所以量真包络，不数台数。
3. **第三步 —— 开始做图**：逐组生成一张图的规格（设备 / 管线 / 流股 / 接续标志）。

配套两条约束：
* **设备进出流股与 `.bkp` 一致**：流股数据直接取 `.bkp` 解析出的 `streams`，不自造、不丢——
  设备守恒（拆张前后设备数不变）由自检 `[18]` 钉住。
* **分割流股标物流信息**：跨张流股在接续标志上额外标"流股号 + T/P/流量三行参数框"，
  接续框答"去了哪儿"、这条标注答"跨过去多少料"。


`selftest.py` 默认运行meta契约及当前维护的寻路、标注/图库回归，可接规格路径额外校核。实际AutoCAD保存、回读、打印须另行执行。`--legacy`才调用未迁移的历史 **25 组**（`[0]`~`[24]`）；这些旧断言不作为新版通过证明，历史覆盖范围如下：**meta 契约对账（最先跑，§11.2）**、
**规格 → 自检 0 错误**、
**COM 映射与 IR 几何逐条一致**（记录式假客户端，不需要 AutoCAD）、**DXF 回读与 IR 一致**且线宽在三档内、
**SCR/LISP 几何等价与括号平衡**、**COM 清层三档回退**、**新符号有几何+有锚点**、
**文档级校验器（流向箭头 / 安全阀注记）**、**退出码契约**、
**图例块坐实（`--rows` 同行配对 + 复用块识别）**、**多义块上下文消歧（含最长键优先）**、
**定位与提取（作用域切分 / 端口反推尺寸 / 折线重建 / 文字清洗 / CLI 退出码，§10.6）**、
**版面（工段划分三级优先 / ELK 中间结果落盘 / 版面结构可断言 / 三类关系型缺陷该报才报 / auto 端到端归零）**、
**ELK layered 引擎（破环 / 分层 / 交叉最小化 / 虚拟节点 / 净距硬约束 / 端口真锚点 / 正交布线 / 折行）**、
**A\* 布线器（逐段正交 / 硬障碍严格不侵入 / 曼哈顿最优 / 接管段必出本体 / 无解如实报告，§6.1）**、
**拆张（一张图 = 一个工段 + 图纸接续标志）**、
**两校样本反哺（辅助折叠 / 当前母图图层保真 / 比对容差 / 完整物流表）**、
**连接保真硬门（与 .bkp 一致 / 不捏造 / 不丢失 / 拓扑严格交替，含负例）**、
**用户交叉样式（横线连续、竖线留断口，同为主物料线也适用；明确连接保留）**、
**流股编号菱形框（实测自浙大）+ 静/动设备分类**、
**文档规模与作者声明对账（组数 / 条数 / CLI 子命令 / 声明是否还在，§11.3，最后跑）**。

---

## 十三、已知边界（不要越界使用）

1. **缺少对应图块或含义不明确时停止绘制并向用户确认**；不生成占位框，不按类别猜形。
2. **HG 20505（仪表符号）为扫描件**：仪表圆圈的安装方式符号、四字母位号含义
   **未经原件核对**，交付前须翻原件（见 `standards-reference.md` 的注意事项）。
3. **尺寸标注**用「引出线+尺寸线+箭头+文字」组合表示，**未生成真正的 DIMENSION 实体**
   （其需要标注样式与关联块，跨后端一致性差）。
4. **剖面线（HATCH）**未生成；`HATCH` 图层已预留。（机械图的形位公差、剖视图符号
   随机械制图部分一并移出本技能范围。）
5. **比例**：PFD 不按比例；设备布置图/管道布置图（1:50）尚未提供比例换算层。
6. 参考图纸（chem.9think.cn）为示意图/图例级，深度**低于** HG 20519 施工图要求，
   本引擎产出同样**不可直接等同施工图交付**。
7. **定位与提取（§10.6）的边界**：
   * **不解析 XREF（外部参照）**——文字/图元在外部参照里时提取不到，表现为"结果为空"；
   * **`POLYLINE` 只收顶点，凸度（`42`）不还原为圆弧**——圆弧段会被"拉直"；
   * **文字宽度是估算**（中文 1 字高 / ASCII 0.55 字高），用于拾取/排序/溢出预判足够，
     不可用于精确排版核算；
   * **`.bkp` 只读主 PFD 段**（对象数最多的顶层页），层级页内的细节流程图不提取——
     要看必须先明确"看哪一页"。
8. **AutoCAD 原生命令兜底（§10.7）**：交互/查询类命令必须预选对象后走 `SendCommand`，
   **裸发即卡死**；`DIST`/`AREA` 建议改用坐标计算而非命令。


流股标注净距修正（2026-10-05）：选位与最终校核共用3mm净距，保护25×15mm整框、固定引线和菱形；不同流股标注之间也检查。只豁免同一流股在菱形处的承载直线，参数框和引线无管线豁免。尺寸和截图样式保持，不能靠拉长引线或遮挡管线掩盖冲突。该3mm是本项目默认选择，不作为原样本毫米测量值。


### 原生资源导入修正（2026-10-06）

`ezdxf.xref.Loader` 的实际运行会改写源文件内存中图层的线型名称，却留下目标图层的旧引用。导入前冻结源线型引用，导入后恢复源内存，再绑定目标文件已隔离命名的线型。LEADER（引线）和 DIMENSION（尺寸）也必须检查并重新绑定隔离后的标注样式，不能只核对文字样式与块引用。

旧默认图框保留原文件；其已登记的语义层只在导入用内存副本中按母图定义补齐，未知层仍报错。新增资源的图层、线型及标注样式引用必须完整，导出后以原生 DWG 回读核对。一个 AutoCAD COM 实例中的打开、保存、文字包围盒读取必须串行；独立验收实例只关闭自身创建的实例。

### 默认图层线宽（2026-10-05重新核对）

以用户最新指定的浙江大学`华东赛区-浙江大学-嘻嘻不嘻烯/PFD-2004版.dwg`为母图，唯一配置在`assets/layer-standard.json`。32层逐层保存原线宽，单位1/100mm：主物料线及设备位号线0.80mm，次要/辅助物料线、热/冷公用工程、氮气空气回管及熔融盐0.40mm，设备轮廓/内件/箭头/流股代号等0.25mm，标题栏边框和内框0.50mm；0、1-文本和Defpoints保留默认值-3。默认DXF/DWG启用线宽显示，当前实体线宽采用随层。线宽数值与显示开关分别核对；不能用颜色/层名一致替代线宽核对。原生块导入后重写32层线宽，不依赖`--all-layers`，直接COM出图也写入并读回图层线宽。


### 圆形换热器物流接点（用户截图，2026-10-05）

图库`hx.cooling`与`hx.heating`的流股只能接左右两个水平NOZZLE外端点，对应left/inlet/feed/tube_in及right/outlet/side/tube_out；禁止物流使用top、bottom、shell_in、shell_out，斜向箭头不作为物流接点。左右位置读取源块两条水平接管实体，刚体变换后与图块一起定位。规格预检、直接pipe调用及最终连接校核共用该限制。多个流股可按用户指定共用同一水平口，不能擅自改接斜向箭头。用户最新示例中PL-LIGHT终点为E-103.tube_out，PL-OUT由E-105.tube_out到E-103.tube_out。此修正不替换已确认设备块；釜式再沸器等其它类型按各自已确认接点。
