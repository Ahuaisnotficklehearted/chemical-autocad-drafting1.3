# 设备图库

当前目录有 **65 项设备类型、63 个实际设备块、2 项缺块**，按 8 个设备族管理。
其中 33 个块来自用户新版图形；27 个已有专用块及 1 个结晶器示意块保留。
新版图形替换了 9 个同型旧目录项，并增加 24 个类型。4 个带“通用”字样的目录项及 6 个实体块已删除。

## 绘图规则

1. 先识别设备模块与型式，再查目录；使用准确的类型 id、中文名称或明确的文件/块名。
2. `catalog.json` 是唯一目录依据。文件位置读取 `block_file`（缺省为 `{族}_1.dxf`），块名读取 `block_name`（缺省为 id），不能按设备族猜新版块所在文件。
3. 不认识的模块、型式不明确或 `pending_block` 必须汇总缺口并向用户确定图块，禁止改成相近设备或拓扑结点。
4. “泵”“塔”“换热器”“反应器”等宽泛分类没有默认设备块，也没有自动兜底映射。
5. 原图形仅作刚体平移/旋转，不重新描画、比例缩放或增加虚构接管。连接点须落在源几何上，不能取悬空的包围盒边中点；离心泵按用户确认的[连接参考图](../assets/centrifugal-pump-connection-reference.png)左侧吸入、顶部排出。无接管线的图形仅按实际轮廓接触出示意图，不代表机械管口设计批准。

## 新版源块

- `assets/equipment-library/templates/pfd/equipment/新版设备块.dxf`：33 个具名块与图册预览引用；保留原生 ELLIPSE、HATCH、圆弧和多段线等实体。
- `assets/equipment-library/user-block-regions.json`：原图文件 SHA-256、取样区域、实体句柄、原图层与用途映射。
- 原图的局部管线/箭头/阀门示意作为图形的一部分保留；局部管线归入 NOZZLE，箭头归入 FLOWARROW。原始图层记录保存在溯源清单。
- 原图实体按用途映射到默认 32 层配置；清除填充关联的旧实体句柄，保留边界与填充图案。
- DXF 与默认 DWG 路径直接复制全部具名源块，包括保留目录项；不能按“新版”标记决定是否保留曲线。IR 中的曲线/填充边界代理仅供布局与关系校核。直接 COM、SCR、LISP、R12 路径拒绝原生块，避免把代理当交付几何。原有61块文件保留；新增调用递归复制嵌套INSERT和匿名尺寸块，依赖异常拒绝输出。

## 缺块清单

| 类型 | 缺少的块 | 处理 |
|---|---|---|
| `column.spray` | 喷淋塔 | 不能用吸收/汽提塔推定塔内型式 |
| `separator.horizontal` | 卧式分离器 | 新版卧式油水分离器不覆盖任意分离职责 |

缺块项保留为设备识别元数据，调用时停止出图并要求明确图块。

## 示例与 Aspen 模块

两个自带示例的 P-101/P-102/P-103 均使用用户指定的新版离心泵；E-103/E-104 的釜式再沸器已接入同名原图块。
预热、冷却和冷凝按示例中已有设备名称选块，不扩展为任意 Aspen 模块的默认型式。
`aspen_block_map` 当前有 25 个明确映射：16 个设备映射与 9 个拓扑结点映射；19 个原先指向“通用”设备的映射已移除。
例如 HEATER/HEATX 不能仅按模块名推断加热、冷却、管壳或釜式型式，需经项目依据明确 `equipment_blocks`。
真正拓扑结点仍保留空值；未知设备不得降级为结点。

## 查询

```bash
python scripts/chemcad_catalog.py list
python scripts/chemcad_catalog.py show pump.centrifugal
python scripts/chemcad_catalog.py show hx.kettle_reboiler
python scripts/chemcad_catalog.py coverage --json
```

`coverage` 需 ezdxf，实际读取块文件；缺块返回退出码 1，包括 JSON 输出。当前 2 项缺块属于明确保留的缺口，不能宣称全覆盖。
`update-inventory.json` 记录实际块定义、页面引用和源文件哈希；它不证明 AutoCAD 最终出图成功。
默认 `scripts/selftest.py` 运行当前维护回归（含全部61块的DXF实体类型保真）；`--legacy`仅诊断未迁移旧断言，历史343条通过不能作为当前证据。实际DWG/字体/接管连接另行回读与打印核对，不能用DXF回归替代。

## 当前目录全表

| 设备族 | 类型 id | 图块名称 | 状态 | 所在文件 |
|---|---|---|---|---|
| 塔器 | `column.tray` | 板式塔 | 新版原生块 | `新版设备块.dxf` |
| 塔器 | `column.packed` | 填料塔 | 新版原生块 | `新版设备块.dxf` |
| 塔器 | `column.spray` | 喷淋塔 | 缺块，停止出图 | `塔器_1.dxf` |
| 容器储罐 | `vessel.vertical` | 立式容器 | 新增源实体提取 | 来源图例138，路径见catalog.json |
| 容器储罐 | `vessel.horizontal` | 卧式容器 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `tank.fixed_roof` | 立式圆筒固定顶储罐 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `tank.external_floating` | 外浮顶罐 | 新增源实体提取 | 来源图例138，路径见catalog.json |
| 容器储罐 | `tank.internal_floating` | 立式圆筒内浮顶储罐 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `tank.sphere` | 球罐 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `separator.horizontal` | 分离器 | 缺块，停止出图 | `容器储罐_1.dxf` |
| 换热设备 | `hx.shell_tube` | 管壳式换热器 | 已有专用块 | `换热设备_1.dxf` |
| 换热设备 | `hx.plate` | 板式换热器 | 已有专用块 | `换热设备_1.dxf` |
| 换热设备 | `hx.double_pipe` | 套管式换热器 | 已有专用块 | `换热设备_1.dxf` |
| 换热设备 | `hx.air` | 空冷器 | 已有专用块 | `换热设备_1.dxf` |
| 换热设备 | `hx.cooling_tower` | 冷却塔 | 已有专用块 | `换热设备_1.dxf` |
| 反应设备 | `reactor.stirred` | 搅拌釜 | 已有专用块 | `反应设备_1.dxf` |
| 反应设备 | `reactor.fixed_bed` | 固定床反应器 | 新版原生块 | `新版设备块.dxf` |
| 反应设备 | `reactor.fluid_bed` | 流化床反应器 | 已有专用块 | `反应设备_1.dxf` |
| 反应设备 | `reactor.tubular` | 列管式固定床反应器 | 新版原生块 | `新版设备块.dxf` |
| 其他设备 | `thermal.furnace` | 加热炉 | 已有专用块 | `其他设备_1.dxf` |
| 流体输送 | `pump.centrifugal` | 离心泵 | 新版原生块 | `新版设备块.dxf` |
| 流体输送 | `pump.positive` | 容积泵 | 已有专用块 | `流体输送_1.dxf` |
| 流体输送 | `pump.vacuum` | 真空泵 | 已有专用块 | `流体输送_1.dxf` |
| 流体输送 | `compressor.unspecified` | 压缩机 | 已有专用块 | `流体输送_1.dxf` |
| 流体输送 | `compressor.reciprocating` | 往复压缩机 | 已有专用块 | `流体输送_1.dxf` |
| 流体输送 | `compressor.roots` | 罗茨风机 | 已有专用块 | `流体输送_1.dxf` |
| 流体输送 | `compressor.screw` | 螺杆压缩机 | 已有专用块 | `流体输送_1.dxf` |
| 流体输送 | `fan.general` | 通风机 | 已有专用块 | `流体输送_1.dxf` |
| 固体分离 | `solids.centrifuge` | 离心机 | 已有专用块 | `固体分离_1.dxf` |
| 固体分离 | `solids.crystallizer` | 结晶器 | 已有示意块 | `固体分离_1.dxf` |
| 固体分离 | `filter.bag` | 袋式除尘器 | 已有专用块 | `固体分离_1.dxf` |
| 固体分离 | `filter.plate` | 板框压滤机 | 已有专用块 | `固体分离_1.dxf` |
| 固体分离 | `filter.drum` | 转筒过滤机 | 已有专用块 | `固体分离_1.dxf` |
| 固体分离 | `filter.candle` | 烛式过滤器 | 已有专用块 | `固体分离_1.dxf` |
| 固体分离 | `separator.cyclone` | 旋风分离器 | 已有专用块 | `固体分离_1.dxf` |
| 固体分离 | `dryer.spray` | 喷雾干燥塔 | 已有专用块 | `固体分离_1.dxf` |
| 其他设备 | `utility.ejector` | 喷射器 | 已有专用块 | `其他设备_1.dxf` |
| 固体输送 | `conveyor.screw` | 螺旋输送机 | 已有专用块 | `固体输送_1.dxf` |
| 固体输送 | `conveyor.bucket` | 斗式提升机 | 已有专用块 | `固体输送_1.dxf` |
| 固体输送 | `conveyor.belt` | 带式输送机 | 已有专用块 | `固体输送_1.dxf` |
| 固体输送 | `mill.ball` | 球磨机 | 已有专用块 | `固体输送_1.dxf` |
| 塔器 | `column.absorber_stripper` | 吸收塔(或汽提塔) | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `separator.gas_liquid` | 气液分离罐 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `vessel.buffer` | 缓冲罐 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `separator.gas_liquid_liquid` | 气液液分离罐 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `separator.wire_mesh` | 立式丝网气液分离器 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `separator.vane` | 立式叶片式气液分离器 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `separator.oil_water` | 卧式油水分离器 | 新版原生块 | `新版设备块.dxf` |
| 容器储罐 | `vessel.steam_drum` | 汽包 | 新版原生块 | `新版设备块.dxf` |
| 反应设备 | `reactor.radial_fixed_bed` | 径向固定床反应器 | 新版原生块 | `新版设备块.dxf` |
| 反应设备 | `reactor.quench_fixed_bed` | 多段绝热冷激式固定床反应器 | 新版原生块 | `新版设备块.dxf` |
| 反应设备 | `reactor.axial_radial` | 轴径向反应器 | 新版原生块 | `新版设备块.dxf` |
| 反应设备 | `reactor.multistage_bubble` | 多级鼓泡塔反应器 | 新版原生块 | `新版设备块.dxf` |
| 换热设备 | `hx.cooling` | 冷却/冷凝 | 新版原生块 | `新版设备块.dxf` |
| 换热设备 | `hx.heating` | 加热 | 新版原生块 | `新版设备块.dxf` |
| 换热设备 | `hx.interstream` | 流股间换热 | 新版原生块 | `新版设备块.dxf` |
| 换热设备 | `hx.kettle_reboiler` | 釜式再沸器 | 新版原生块 | `新版设备块.dxf` |
| 流体输送 | `pump.diaphragm` | 隔膜泵 | 新版原生块 | `新版设备块.dxf` |
| 流体输送 | `compressor.centrifugal` | 离心式压缩机 | 新版原生块 | `新版设备块.dxf` |
| 流体输送 | `compressor.multistage_centrifugal` | 多级离心式压缩机 | 新版原生块 | `新版设备块.dxf` |
| 流体输送 | `compressor.axial` | 轴流压缩机 | 新版原生块 | `新版设备块.dxf` |
| 其他设备 | `thermal.waste_heat_boiler` | 余热蒸汽锅炉 | 新版原生块 | `新版设备块.dxf` |
| 其他设备 | `separator.membrane` | 膜分离器 | 新版原生块 | `新版设备块.dxf` |
| 其他设备 | `utility.static_mixer` | 混合器 | 新版原生块 | `新版设备块.dxf` |
| 其他设备 | `thermal.steam_generator` | 蒸汽发生器 | 新版原生块 | `新版设备块.dxf` |

## 资产位置

| 资产 | 路径 |
|---|---|
| 权威目录 | `assets/equipment-library/templates/pfd/equipment/catalog.json` |
| 新版块包 | `assets/equipment-library/templates/pfd/equipment/新版设备块.dxf` |
| 原分类源图与已有块 | `assets/equipment-library/templates/pfd/equipment/{族}_1.dxf` |
| 来源与授权 | `assets/equipment-library/SOURCE.md`、`LICENSE` |
| 查询 / 调用 | `scripts/chemcad_catalog.py`、`scripts/chemcad_block_library.py` |


### 圆形换热器物流接点（用户截图，2026-10-05）

图库`hx.cooling`与`hx.heating`的流股只能接左右两个水平NOZZLE外端点，对应left/inlet/feed/tube_in及right/outlet/side/tube_out；禁止物流使用top、bottom、shell_in、shell_out，斜向箭头不作为物流接点。左右位置读取源块两条水平接管实体，刚体变换后与图块一起定位。规格预检、直接pipe调用及最终连接校核共用该限制。多个流股可按用户指定共用同一水平口，不能擅自改接斜向箭头。用户最新示例中PL-LIGHT终点为E-103.tube_out，PL-OUT由E-105.tube_out到E-103.tube_out。此修正不替换已确认设备块；釜式再沸器等其它类型按各自已确认接点。

## 全类别增量补库（2026-10-06）

立式容器和外浮顶罐按可靠图例名称及原实体句柄补齐；原有65项ID、默认映射和61块文件保留。完整分类数量、原单位、来源、嵌套/扩展依赖与查询命令见[incremental-library.md](incremental-library.md)。未知资产不进入设备自动匹配。图库保真回读与原生DWG调用证据见任务目录，不声称旧测试套件已通过新版本。
