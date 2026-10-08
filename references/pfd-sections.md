# 工段逐行装订与汇总物料表

2026-10-06。用户指定图框边缘净距150mm，文字/线宽/设备块不缩放。

## 有序工段入口

`chemcad_book.py --manifest`读取UTF-8 JSON，`pages`路径相对清单文件解析，工段编号唯一，页面不可重复归属。旧目录/逐张接口仍可用。

```json
{"version":1,"sections":[
  {"id":"S001","name":"第一工段","pages":["01.json"]},
  {"id":"S002","name":"第二工段","pages":["02.json","03.json"]}
]}
```

```bash
python scripts/chemcad_book.py --manifest sheets/book-manifest.json --out 全套PFD.dwg
python scripts/chemcad_sections.py --manifest sheets/book-manifest.json --out 全套PFD.dxf --backend dxf
```

每工段独占一行，左边界X=0，各行按清单顺序向下。流程页向右续排，末尾仅一张本工段汇总物料框；流程页不重复物料框。默认A1为841×594mm，横向基准步距991mm、纵向744mm。坐标由实际原生图框边界计算，纯平移；第二行增加页数不会移动第一行。流程图超过A1容量、页面图框不唯一/高度不一致、汇总表超容量均报错，不缩字或自动增加物料续框。

## 物流身份与接续

同一工段用`model_id`或`_model_id`加`source_stream_id`合并流股；同一原模型跨页须传相同模型身份。缺省模型身份为规格绝对路径，避免误合并不同模型。Aspen拆张会写入原模型哈希和原流股ID，并同步生成`book-manifest.json`。

不同模型同号流股各保留一列，列头添加模型序号；同一身份任一非空参数冲突报错。汇总同时计入管线、界区进出料和跨页接续提供的数据。接续的`pair`必须对应真实源/目标，两页各有一个本地端点；工段清单检查跨页配对与归属，不生成模型中没有的连接。

## 验收边界

任务目录的两份原生DWG分别为第一工段2框/第二工段3框，以及第一工段2框/第二工段4框。回读核对实际边界、150mm净距、第一行坐标不变、汇总列头唯一。跨工段/跨页与参数冲突、容量错误另有实际CLI证据。Aspen拆张接口使用人工构造的解析结果；本次没有运行Aspen模型。
