# EplanMaster

`EplanMaster` 是一个面向 Eplan / 工程类 PDF 的解析与可视化工具仓库，当前包含三条主要能力：

- Python 侧 PDF 预处理与结构解析
- Python 侧 `pdf_parser`，用于按页提取矢量对象、推断图纸内容区域、识别闭合实体/矩形/圆/虚线区域，并支持矢量形状匹配
- 前端 `pdf_reader`，用于查看 PDF 页面、叠加矢量/文字/链接对象，并回看对应的 PDF 源片段

项目目前的 PDF 解析核心逻辑是自定义实现的，主要位于：

- [scripts/inspect_eplan_pdfs.py](scripts/inspect_eplan_pdfs.py)
- [scripts/build_pdf_reader_data.py](scripts/build_pdf_reader_data.py)
- [scripts/parse_pdf_to_json.py](scripts/parse_pdf_to_json.py)
- [pdf_parser/main.py](pdf_parser/main.py)
- [pdf_parser/vector_api.py](pdf_parser/vector_api.py)

前端 PDF 渲染使用：

- `pdfjs-dist`

## Frontend Preview

下面这张图用于展示 `pdf_reader` 的前端界面效果：

![pdf_reader frontend preview](docs/images/pdf-reader-overview.png)

## 仓库结构

```text
EplanMaster/
├─ docs/                     # README 用到的图片与附加文档
├─ scripts/                  # PDF 解析、检查、前端数据生成脚本
├─ pdf_parser/               # 单页 PDF 矢量解析、区域检测、实体识别与形状匹配
├─ pdf_reader/               # Vite + TypeScript 前端阅读器
├─ storage/                  # Docker volume：原始数据与运行输出
│  ├─ data/eplan_pdf/       # 放原始 PDF，默认不提交
│  └─ output/               # 解析与调试输出，默认不提交
├─ environment.yml           # Python 环境定义
└─ README.md
```

## 环境要求

建议使用下面这套环境：

- Python 3.13
- Node.js 20+
- npm 10+

Python 侧依赖见 [requirements.txt](requirements.txt)，目前主要包括：

- `pymupdf`：读取 PDF 页面、绘图对象和页面尺寸
- `shapely`：空间索引、矢量相交、闭合面 polygonize 与区域判断
- `py7zr`：处理 Eplan 相关归档数据时使用

## 1. 搭建 Python 环境

### 使用 conda

在仓库根目录运行：

```powershell
conda env create -f environment.yml
conda activate Eplan
pip install -r requirements.txt
```

如果环境已经存在：

```powershell
conda activate Eplan
pip install -r requirements.txt
```

### 不使用 conda

也可以直接使用本机 Python 3.13：

```powershell
python --version
```

只要版本兼容，脚本通常也可以直接运行。

## 2. 搭建前端环境

进入前端目录并安装依赖：

```powershell
cd pdf_reader
npm install
cd ..
```

## 3. 准备原始 PDF

默认输入目录是：

```text
storage/data/eplan_pdf
```

如果目录不存在，先创建：

```powershell
New-Item -ItemType Directory -Force storage\data\eplan_pdf
```

然后把待处理的 PDF 放进去，例如：

```text
storage/data/eplan_pdf/demo.pdf
```

## 4. 生成前端所需的预处理数据

预处理脚本依赖 **PyMuPDF**（`pymupdf`）。若尚未安装，可先执行 `pip install -r requirements.txt`。

在仓库根目录运行：

```powershell
python scripts/build_pdf_reader_data.py
```

默认行为：

- 输入目录：`storage/data/eplan_pdf`
- 输出目录：`pdf_reader/public/reader-data`

这个脚本会：

- 扫描 `storage/data/eplan_pdf` 下所有 `*.pdf`
- 复制 PDF 到前端静态目录
- 解析页面中的矢量路径、文字、图片、链接等对象
- 生成 `manifest.json`
- 为每个 PDF 的每一页生成结构化 JSON

如果你新增、替换或删除了 PDF，需要重新执行这一步。

## 5. 启动前端阅读器

进入前端目录后运行：

```powershell
cd pdf_reader
npm run dev
```

启动后按终端输出打开本地地址，通常是：

```text
http://localhost:5173
```

## 从零开始的完整运行流程

如果是第一次在新机器上运行，推荐直接按下面的顺序执行：

```powershell
git clone <your-repo-url>
cd EplanMaster

conda env create -f environment.yml
conda activate Eplan

cd pdf_reader
npm install
cd ..

New-Item -ItemType Directory -Force storage\data\eplan_pdf
# 然后把 PDF 放到 storage/data/eplan_pdf/

python scripts/build_pdf_reader_data.py

cd pdf_reader
npm run dev
```

## 可选：生成 PDF 检查报告

如果你想单独查看 PDF 的原始对象结构和检查输出，可以运行：

```powershell
python scripts/inspect_eplan_pdfs.py
```

默认行为：

- 输入目录：`storage/data/eplan_pdf`
- 输出目录：`storage/output/pdf_inspection`

主要输出包括：

- `storage/output/pdf_inspection/README.md`
- `storage/output/pdf_inspection/index.json`
- `storage/output/pdf_inspection/<pdf-name>/overview.md`
- `storage/output/pdf_inspection/<pdf-name>/summary.json`
- `storage/output/pdf_inspection/<pdf-name>/pages_readable.json`

这个步骤不是前端运行的必需步骤，但对调试 PDF 结构很有帮助。

## 可选：生成 PDF 原始分段 JSON

如果你想把单个 PDF 拆成粗粒度源码片段，可以运行原始 PDF 分段脚本：

```powershell
python scripts/parse_pdf_to_json.py storage/data/eplan_pdf/demo.pdf
```

默认输出到：

```text
storage/output/pdf_parser_step1/demo.json
```

也可以显式指定输入和输出：

```powershell
python scripts/parse_pdf_to_json.py --input storage/data/eplan_pdf/demo.pdf --output storage/output/pdf_parser_step1/demo.json
```

## 可选：使用 pdf_parser 分析单页矢量结构

`pdf_parser` 目录里的工具面向单页矢量图形分析，适合调试 Eplan 图纸中的框、圆、虚线区域、封闭单元和重复形状。

主要模块：

- [pdf_parser/pages_manager/pdf_page.py](pdf_parser/pages_manager/pdf_page.py)：用 PyMuPDF 抽取页面 drawing primitives，并封装为 `VectorBase`
- [pdf_parser/pages_manager/split_page.py](pdf_parser/pages_manager/split_page.py)：根据图纸外框推断有效内容区域，并过滤页框矢量
- [pdf_parser/tools/vector_entity.py](pdf_parser/tools/vector_entity.py)：用 union-find 和 Shapely 将相交/相邻/包含的矢量合并为实体，并计算闭合面
- [pdf_parser/tools/vector_box.py](pdf_parser/tools/vector_box.py)：检测矩形、圆形、虚线闭合区域和网格单元
- [pdf_parser/tools/vector_matcher.py](pdf_parser/tools/vector_matcher.py)：按 bbox 选中形状，并在页面或文档中查找旋转、缩放、平移后的重复形状
- [pdf_parser/vector_api.py](pdf_parser/vector_api.py)：stdin/stdout JSON API，供前端或其他进程调用

命令行入口是 [pdf_parser/main.py](pdf_parser/main.py)。示例：

```powershell
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool split-area
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool entities
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool boxes
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool circles
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool dashed
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool cells
```

常用参数：

- `--include-vectors`：在 `entities` 输出中附带原始矢量记录
- `--include-faces`：在 `entities` 输出中附带实体闭合面
- `--bbox X0 Y0 X1 Y1`：给 `matcher` 指定待选择区域
- `--coord-space pdf|mupdf`：指定 bbox 坐标系，默认 `pdf`
- `--use-content-vectors` / `--no-use-content-vectors`：是否只在推断出的内容区域内检索

使用 `matcher` 查找某个 bbox 内的形状及其重复出现位置：

```powershell
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool matcher --bbox 551.29 500.398 572.291 521.399
```

如果需要从其他进程调用，可以向 `vector_api.py` 写入 JSON：

```powershell
'{"pdf_path":"storage/data/eplan_pdf/demo.pdf","page":1,"mode":"entities"}' | python pdf_parser/vector_api.py
'{"pdf_path":"storage/data/eplan_pdf/demo.pdf","page":1,"mode":"boxes"}' | python pdf_parser/vector_api.py
'{"pdf_path":"storage/data/eplan_pdf/demo.pdf","page":1,"mode":"match","bbox":{"x0":551.29,"y0":500.398,"x1":572.291,"y1":521.399},"coord_space":"pdf"}' | python pdf_parser/vector_api.py
```

`vector_api.py` 支持的主要 `mode` 包括：

- `entities` / `entity_region`
- `boxes` / `circles` / `dashed` / `groups` / `cells`
- `select` / `match`

### Final diagram graph model

Diagram extraction uses two layers. `elements` are intermediate recognized
shapes; ordinary dashed polygons remain polygonal box elements.
`components` are final entities built by merging every element containment
tree. Element-to-element containment is therefore not exposed in the final
graph.

Recognized geometric payloads are stored once in `diagram.elements`, with one
ID sequence shared by original elements plus the raw shapes of wires,
endpoints, and groups. Unowned vectors remain separately in
`diagram.remaining_vectors` and do not receive element IDs. Each `component`,
`endpoint`, `wire`, `net`, and `group` record contains only `id`, `type`, `page`, `bbox`, `title`,
`descriptions`, and an `elements` ID list. A net's list references its wire
elements. Page extraction output stores `diagram`, `info_table`, and
`crosspage_relations` as sibling fields. `crosspage_relations` contains the
`hyperlinks` and `transfers` lists. Links owned by components containing an
arrow element are stored as transfers; neither link collection persists
`action_chain`. Text ownership is transient frontend state and is not persisted
in extraction JSON.

After all pages are processed, hyperlinks receive `target_component` only when
their target region matches a diagram component. Transfers are resolved only
against components containing arrow elements; a missing or non-arrow target
omits `target_component` and emits a runtime warning.

The PDF reader first looks for a completed document result at
`storage/output/pdf_parsing_result/<PDF filename without extension>.json`. Diagram
pages under its `pages` field take precedence over per-page extraction cache;
missing files and non-diagram pages keep the existing cache fallback.

Each `diagram.remaining_text` record includes `nearby`, containing up to five
component IDs. Candidates are ordered by bbox distance and retained only when
their distance is at most 1.2 times the nearest component distance.

After strict wire/endpoint relation extraction, a wire endpoint with no element
owner is attached to the nearest element within
`wire_endpoint_fallback_margin_pt` (5 pt by default). Existing endpoint owners
are never replaced by this fallback.

Box elements that touch, or whose boundaries are within
`box_adjacency_margin_pt` (1 pt by default), are merged into one element before
containment relations are composed. Fully contained boxes stay independent.

A net is created only for a connected group containing at least two wires. Its
`title` uses the ordered intersection of member wire titles, falling back to
their common descriptions when no title is shared. Identifier-like fragments
embedded in longer descriptions (for example `40R0` in `OG 4 mm² 40R0`) also
count as common values; the title stays empty when no common name is found.

Dash-dot polygons are independent `groups`, never component members. `nets`
are first-class abstract entities with common entity fields and their wire and
endpoint IDs. Final relations are limited to:

- `connection`: `component -> endpoint` and `wire -> endpoint` (electrically
  undirected).
- `contains`: `net -> wire` and `group -> component`.

A single PDF dash on/off pair is an ordinary dashed boundary. A multi-pair
dash-dot pattern is a group boundary; `example.pdf` pages 12 and 13 are the
reference samples.

注意：`pdf_parser` 的矢量坐标内部主要使用 PyMuPDF 页面坐标系，即原点在左上角，`y` 轴向下；API 会在输出中尽量同时给出 `bbox_mupdf` 和 `bbox_pdf`，方便和前端选区或 PDF 用户坐标互相转换。

## 常用命令

### 重新生成前端数据

```powershell
python scripts/build_pdf_reader_data.py
```

### 启动前端开发环境

```powershell
cd pdf_reader
npm run dev
```

### 前端生产构建

```powershell
cd pdf_reader
npm run build
```

### 单独生成 PDF 检查输出

```powershell
python scripts/inspect_eplan_pdfs.py
```

### 单独生成 PDF 原始分段 JSON

```powershell
python scripts/parse_pdf_to_json.py storage/data/eplan_pdf/demo.pdf
```

### 分析单页矢量实体

```powershell
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool entities --include-faces
```

### 检测单页闭合框/圆/单元格

```powershell
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool boxes
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool circles
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool cells
```

### 在单页或文档中查找重复矢量形状

```powershell
python pdf_parser/main.py --pdf-file-path storage/data/eplan_pdf/demo.pdf --page 1 --tool matcher --bbox 551.29 500.398 572.291 521.399
```

## 开源前建议

如果你准备把这个仓库公开，建议在发布前确认下面几件事：

- 确认 `storage/data/` 下没有不方便公开的原始 PDF
- 确认 `storage/output/` 下没有调试产物需要提交
- 确认 `pdf_reader/public/reader-data` 中的示例数据允许公开
- 选择并添加一个明确的开源许可证

当前仓库里还没有替你擅自添加许可证文件，因为这属于有法律后果的选择，建议你根据发布目标自行决定，例如 MIT、Apache-2.0 或 GPL 系列。

## 说明

- Python 预处理和 `pdf_parser` 依赖 PyMuPDF、Shapely 等库，建议先执行 `pip install -r requirements.txt`
- 前端是 Vite + TypeScript
- PDF 页面渲染由 `pdfjs-dist` 完成
- `pdf_parser` 当前更偏向工程图纸矢量结构调试，不是通用 OCR 或语义解析器
- 某些 PDF 的文字编码、字体映射和对象流格式比较特殊，解析结果可能需要继续迭代
