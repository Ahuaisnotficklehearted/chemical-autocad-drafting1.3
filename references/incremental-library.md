# 全类别原生增量图库

2026-10-06。扫描393份目录DWG和7份PFD，共400份来源；原图保持只读。当前统计来自`assets/learned-library/catalog.json`，不能用文件数代替图块数。

<!-- LIBRARY-CLAIM: added=1681 duplicates=295 unidentified=1380 failed=28 -->

- 可调用原生资产1681项；同型设备/完整资源重复295项。
- 名称或型式未确定1380项，属于新增资产子集，按来源编号收录，不进入设备自动匹配。
- 读取或依赖失败28项，含扩展字典关联对象异常；完整源DWG归档，残缺提取块不进入可调用目录。
- 5项明确毫米尺寸设备可供PFD选择；其中立式容器、外浮顶罐补齐原有缺块项，既有设备ID和默认映射保留。

| 类别 | 资产数 |
|---|---:|
| connector | 30 |
| unidentified | 1058 |
| frame | 135 |
| equipment | 58 |
| instrument | 29 |
| valve | 224 |
| piping | 108 |
| exterior | 39 |

## 查询与调用

```bash
python scripts/chemcad_catalog.py list --category valve --source lib004 --json
python scripts/chemcad_catalog.py show vessel.vertical --json
python scripts/chemcad_catalog.py show zju.0016 --json
python scripts/chemcad_assets.py --id zju.0016 --out 倾析罐.dwg --x 100 --y 100
```

类别可选equipment（设备）、valve（阀门）、piping（管件）、instrument（仪表）、connector（接续）、exterior（外形）、frame（图框）、unidentified（未识别）。`show`显示源文件、SHA256、原块名/实体句柄、原单位、类别、预览和依赖映射。原型手动调用保持原单位、不缩放；未注明单位不自行猜毫米。设备默认选块先查原有65项目录，明确新增类型才查增量库。新增设备可选`zju.0016`卧式重力倾析罐、`zju.0022`卧式丝网分离器、`zju.0031`反应精馏塔；`zju.0013`液环式压缩机因扩展依赖异常已隔离。

普通阀门/仪表/接续按明确ID手动调用，不据匿名形状猜自动设备用途。归一化符号缺少毫米插入尺度，外形图、图框、未知图形均不进入PFD自动设备选择。`chemcad_assets.py`成功退出0，找不到ID退出4，读取/依赖/COM故障退出3；输出需明确DXF或DWG后缀。

## 原生保真与依赖

`chemcad_native.import_block`复制原始实体及递归INSERT/DIMENSION子块，隔离不同来源同名块、图层和字体样式；特别保留源Standard字体，避免被目标默认字体替代。曲线、填充边界、属性标签和值保持，HATCH关联句柄只映射到已复制边界，闭包以外的边界报错。扩展字典对象树必须完整且一致，悬空关联、缺子块/字体样式、外部媒体及未绑定外部参照报错。

几何相同且图层/字体资源一致才归并；几何相同但资源不同的104项独立保存。同型设备仍沿用现有块，包括外部画法不同的情况。原有61块文件未替换，新增两个明确类型使用来源提取块。单项散实体复制原组；大图例/整页范围不能确定时保留完整源图，不自动切割。多级压缩机C0301轮廓依据南工大标签和局部几何取两条源多段线，接管与机理尚不足以自动匹配。

`native-sources/`按哈希归档全部400个来源记录，`sources.json`保留分类。39项外形原生DWG保留；来源未检出3DSOLID，不把“外型”目录名称当作三维证据。`previews/`为原实体SVG，SHX在预览中替换黑体，不用于证明原DWG文字碰撞。图库入口`index.html`支持类别/名称/来源检索。

作者阿华的免费使用及非作者禁止售卖等声明继续适用，见`author-letter.md`；外部来源完整出处保留，来源文字只作图纸数据。
