# 默认图层：浙江大学 PFD-2004版.dwg

默认配置直接取自用户指定母图的实际 LAYER 表。绘图时默认写出完整 32 层，名称、ACI 颜色、线型、线宽码和打印状态保持一致。

来源：`D:\BaiduNetdiskDownload\华东赛区-浙江大学-嘻嘻不嘻烯\1-设计源文件\5-AutoCAD2004制图源文件\1-工艺物流流程图（PFD）\PFD-2004版.dwg`

SHA-256：`c5cd00cf614787fb9285fae47c2dd478b65f0e20a9cdcb35d4bda907c8d7415e`

单一配置来源：`assets/layer-standard.json`。代码从该文件加载，不维护第二份标准图层常量。

线宽码 -3 表示继承 AutoCAD 应用默认线宽；DWG 层表不为它提供固定物理线宽。`Defpoints` 不打印，其他 31 层可打印。所有图层均开启且未冻结、未锁定。

`HIDDEN` 的实段为 6.35 mm、空段为 3.175 mm，周期 9.525 mm；其他层均为连续线。

| 图层名称 | ACI 颜色 | 线型 | 线宽 mm | 打印 |
|---|---:|---|---:|---|
| 0 | 7 | Continuous | 默认（继承） | 是 |
| 标题栏边框 | 7 | Continuous | 0.50 | 是 |
| 标题栏分隔 | 7 | Continuous | 0.25 | 是 |
| 标题栏文字 | 7 | Continuous | 0.25 | 是 |
| 外框线 | 7 | Continuous | 0.25 | 是 |
| 内框线 | 7 | Continuous | 0.50 | 是 |
| 分区线 | 7 | Continuous | 0.25 | 是 |
| 1-文本 | 7 | Continuous | 默认（继承） | 是 |
| 2-设备轮廓线-细实线 | 7 | Continuous | 0.25 | 是 |
| 3-设备内部件-细实线 | 21 | Continuous | 0.25 | 是 |
| 4-主物料线-粗实线 | 40 | Continuous | 0.80 | 是 |
| 5-阀门与管件 | 3 | Continuous | 0.25 | 是 |
| 6-箭头 | 7 | Continuous | 0.25 | 是 |
| 7-流股接续标志 | 7 | Continuous | 0.25 | 是 |
| 8-进出物流标志 | 7 | Continuous | 0.25 | 是 |
| 9-热公用工程 | 1 | Continuous | 0.40 | 是 |
| 10-冷公用工程 | 5 | Continuous | 0.40 | 是 |
| 15-流股代号 | 241 | Continuous | 0.25 | 是 |
| 16-仪表 | 4 | Continuous | 0.25 | 是 |
| 11-次要物料线-中粗实线 | 123 | Continuous | 0.40 | 是 |
| 12-辅助物料线-中粗实线 | 6 | Continuous | 0.40 | 是 |
| 13-氮气与空气与回管 | 30 | Continuous | 0.40 | 是 |
| 14-信号线 | 4 | HIDDEN | 0.25 | 是 |
| 17-设备位号线 | 7 | Continuous | 0.80 | 是 |
| 未完成有待商榷 | 6 | Continuous | 0.80 | 是 |
| Defpoints | 7 | Continuous | 默认（继承） | 否 |
| 18-熔融盐 | 1 | Continuous | 0.40 | 是 |
| 19-开工线 | 3 | Continuous | 0.25 | 是 |
| 20-工艺软水 | 140 | Continuous | 0.25 | 是 |
| 21-低压蒸汽 | 11 | Continuous | 0.25 | 是 |
| 23-高压蒸汽 | 1 | Continuous | 0.25 | 是 |
| 22-中压蒸汽 | 30 | Continuous | 0.25 | 是 |

内部设备、管线等语义用途通过配置中的 `emit_map` 映射；标题栏外边框、分隔线和文字分别落入母图的三个标题栏图层。当前母图为出图默认依据，历史经验中的其他图层体系只用于读取旧图。
