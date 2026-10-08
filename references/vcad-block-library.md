# VCAD 图块库：41 类 / 2852 个 DWG

> 本文说明 `assets/vcad-block-library/` 收录的图块**怎么用、边界在哪**。
> 权威元数据是 `assets/vcad-block-library/catalog.json`（**唯一事实来源**，不要在别处另抄一份）；
> 重建走 `scripts/build_vcad_catalog.py`。
>
> 目录
>
> 1. 图块库有什么
> 2. 分类全表（41 类）
> 3. 怎么用
> 4. **边界（必读）**
> 5. 与 `equipment-library/` 的区别
> 6. 查询与重建
> 7. 来源与授权

---

## 1. 图块库有什么

| 资产 | 位置 | 说明 |
|---|---|---|
| 块文件 | `vcad-block-library/<分类>/*.dwg` | **2852 个 DWG**，一律为**可直接 INSERT 的块**，多数按标准件逐一建块 |
| 幻灯片预览 | 同名 `.sld`（796 个） | AutoCAD 图库幻灯片，供 VCAD 面板预览；本技能渲染请走 `scripts/preview_dxf.py` |
| 参数表 | `*.xls` / `*.xlsx` / `*.dat` | 选型与规格数据（如`设备法兰的哑图一览表.xlsx`、各部件参数表） |
| 目录 | `catalog.json`（800 KB） | 46 项分类元数据 + 3882 条块记录（名/扩展名/体量/分类/标准号） |

**总量**：3882 文件 / 2852 DWG / 272.4 MB。

**命名规范（重要）**：VCAD 把**标准号写进文件名**，所以能按标准检索：

```
hg21515-ff-400-常压-F.dwg   → HG/T 21515 人孔  FF 密封面  DN400  常压  F 型
nb47021fm.dwg               → NB/T 47021 设备法兰  FM 密封面
gbt1503appd-d01a.dwg        → GB/T 150.3 附录 D 图 D-01a（焊接结构）
HG21639-2005-01.dwg         → HG/T 21639 吊柱
ear_A1~5.dwg                → 耳式支座 A 型 1~5 号
```

`catalog.json` 里每条块记录都带 `standard` 字段（从文件名解析）。
**解析率 1434/3882 = 36.9%**——未解析的大多是 `tk1`/`tk2`/`company` 里的图框与节点块，
它们**本来就不按标准件命名**（如 `bem_a1`、`$$$vcadA1H`）。这是预期结果，不是漏解析。

## 2. 分类全表（41 类）

| 目录 | 中文名 | DWG | 体量 | 关联标准 |
|---|---|---:|---:|---|
| `tk2` | 节点图库 tk2 | 613 | 49.5 MB | — |
| `tk1` | 图框图库 tk1 | 508 | 40.4 MB | — |
| `company` | 企业图框与容器模板 | 404 | 50.9 MB | 企业标准 |
| `manholeclad` | 衬里人孔 | 313 | 16.9 MB | HG/T 21596 |
| `manhole` | 人孔 | 299 | 13.1 MB | HG/T 21515~21533 |
| `weld` | 焊接节点 | 102 | 7.2 MB | HG/T 20583 · GB/T 150.3 |
| `mhasme` | ASME 人孔 | 66 | 3.9 MB | ASME |
| `BEM` | 固定管板式换热器 | 58 | 3.3 MB | TEMA / GB/T 151 |
| `fldetail` | 法兰连接详图 | 52 | 3.3 MB | HG/T 20592 / ASME B16.5 |
| `detail` | 节点详图 | 51 | 3.1 MB | HG/T 20583 等 |
| `heatex` | 换热器通用部件 | 51 | 2.4 MB | GB/T 151 |
| `设置` | 配置块 | 41 | 1.3 MB | — |
| `davit` | 吊柱 | 40 | 8.0 MB | HG/T 21639 |
| `bdfl` | 设备法兰（NB/T 47021~47023） | 31 | 2.7 MB | NB/T 47021~47023 |
| `BEU` | U 形管式换热器 | 23 | 1.5 MB | TEMA / GB/T 151 |
| `lug2018` | 吊耳（2018 版） | 21 | 1.3 MB | HG/T 21574-2018 |
| `leg` | 耳式支座 | 20 | 1.4 MB | JB/T 4712.3 |
| `standard` | 标准图框与视图 | 20 | 781.9 KB | — |
| `hgtgasket` | 垫片 | 17 | 1.3 MB | HG/T 20606 / ASME B16.20 |
| `BES` | 浮头式换热器 | 15 | 999.7 KB | TEMA / GB/T 151 |
| `yt` | 哑图模板 | 14 | 3.6 MB | — |
| `lug` | 吊耳 | 13 | 699.3 KB | HG/T 21574 |
| `151` | 管壳式换热器零部件 | 9 | 411.0 KB | GB/T 151 |
| `gbhead` | 封头 | 9 | 636.6 KB | GB/T 25198 |
| `other` | 螺栓螺母 | 9 | 385.4 KB | GB/T 5782 等 |
| `(根目录)` | 根目录散装块 | 8 | 316.3 KB | — |
| `nozzle` | 管口与补强 | 8 | 529.5 KB | HG/T 20583 |
| `fgnozzle` | 法兰管口 | 7 | 316.6 KB | HG/T 20583 |
| `innerladder` | 内梯 | 6 | 252.6 KB | HG/T 21514 |
| `jacket` | 夹套 | 4 | 202.8 KB | HG/T 20583 |
| `yt1` | 哑图模板 2 | 4 | 1002.4 KB | — |
| `BKU` | 釜式重沸器 | 2 | 139.6 KB | TEMA / GB/T 151 |
| `expan` | 膨胀节 | 2 | 145.7 KB | GB/T 16749 |
| `eyebolt` | 吊环螺钉 | 2 | 160.4 KB | GB/T 825 |
| `grating` | 格栅与平台板 | 2 | 1.7 MB | — |
| `tower` | 塔器 | 2 | 1.7 MB | NB/T 47041 |
| `zztk` | 增长图块 | 2 | 135.7 KB | — |
| `dcone` | 双锥环密封 | 1 | 127.3 KB | — |
| `hor` | 卧式容器技术特性表 | 1 | 144.2 KB | — |
| `nelson` | Nelson 型接管 | 1 | 88.6 KB | — |
| `sphere` | 球罐 | 1 | 212.4 KB | GB/T 12337 |

> 5 个目录**无图块**（纯数据挂载点）：`excel`、`fixheat`、`plld`、`tk3`、`tk4`。

## 3. 怎么用

**这是一套"素材"，不是"符号渲染器"。** 两种用法：

1. **直接 INSERT 进目标图**——本技能的出图链路（`draw_pfd.py`）**不认这批块**；
   要插入需在 AutoCAD 里手动 `INSERT`，或由用户在规格里指定块路径后另行处理。
2. **按标准查块**——需要某个标准件（如"HG/T 21515 DN400 FF 常压人孔"）时，
   查 `catalog.json` 定位到文件，再 INSERT。这是它最常用的场景。

**不要**把 `.sld` 当渲染源：`.sld` 是 AutoCAD 幻灯片（位置矢量+颜色索引），
不能可靠转成几何。要预览请对 `.dwg` 跑 `scripts/preview_dxf.py`。

## 4. 边界（必读）

⚠️ **这批图块属于「设备制造详图」域（机械制图）**：

- 内容是人孔、法兰、封头、管口、支座、吊耳、焊接节点、换热器零部件的**零件图/节点大样**，
  带完整尺寸、坡口、材料标注。
- 本技能当前的出图口径是 **只出 PFD**（工艺流程图）——**PFD 用不到这一层深度**。
- 因此：**本图块库是"设备设计参考素材"，不是 PFD 出图素材**。
  不要在 PFD 里插入人孔/法兰/焊接节点详图。

> 需要画设备制造图（机械图）时，这批块才有直接用处——但**那不属于当前技能的出图范围**。

## 5. 与 `equipment-library/` 的区别

本技能里有**两套完全不同的图库**，别混：

| | `assets/equipment-library/` | `assets/vcad-block-library/` |
|---|---|---|
| 是什么 | **PFD 工艺符号**（65 项设备类型，61 个可用块/9 类结点） | **设备制造详图块**（2852 个标准件/节点） |
| 粒度 | 一个块 = 一类设备（离心泵、板式塔…） | 一个块 = 一个具体规格（DN400 人孔…） |
| 有元数据吗 | `catalog.json`（id/族/端口/尺寸/渲染器） | `catalog.json`（分类/体量/标准号；**无端口/尺寸**） |
| 接进出图链路 | ✅ `chemcad_symbols.py` + `chemcad_catalog.py` | ❌ 纯素材 |
| 体量 | 3.6 MB | 272.4 MB |
| 用途 | **画 PFD** | **设备设计查标准件** |

## 6. 查询与重建

```bash
# 重建目录（扫盘 → 写 catalog.json），零依赖
python scripts/build_vcad_catalog.py

# 只看统计、不写文件
python scripts/build_vcad_catalog.py --stats
```

查询直接读 `catalog.json`（`blocks[]` 支持按 `category` / `standard` / `name` 过滤）：

```python
import json
d = json.load(open("assets/vcad-block-library/catalog.json", encoding="utf-8"))
[ b["subpath"] for b in d["blocks"] if b["standard"] == "HG/T 21515" ]
```

## 7. 来源与授权

- **来源**：`VCAD2022 原版程序`（国产压力容器/换热器设计插件）的 `block/` 目录。
- **版权**：VCAD2022 版权归原作者（`yangwting`，微信公众号 `VCAD001`）所有。
- ⚠️ **授权边界**：本目录仅作**本地参考图块库**收录。**不得对外分发、不得商用、不得随技能对外发布**；
  对外分享本技能时应剔除 `assets/vcad-block-library/`。
- 收录时已剔除：`.bak`（编辑器冗余备份）、`.pdf`（标准图册，非图块）、`.bat`（脚本）、
  CAD 锁文件（`.dwl`/`.dwl2`/`.qdr`）。
