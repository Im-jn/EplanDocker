# pdf_parser 开发说明

## 1. 模块目标

pdf_parser 用于解析工程类 PDF 的单页数据，并在 PyMuPDF 页面坐标系中完成：

- 提取页面矢量图元和文字 span。
- 检测页面主要图纸区域 content_bbox。
- 检测 content_bbox 下方的信息栏区域 info_bbox。
- 按 bbox 提取区域内的矢量和文字。
- 检测闭合框、圆形、虚线区域和单元格。
- 通过平移、旋转和缩放匹配单个矢量或矢量组。
- 为 table_extractor 和 diagram_extractor 提供统一的区域输入。

坐标系约定：

- 内部统一使用 PyMuPDF 页面坐标。
- 原点位于页面左上角。
- x 向右增大，y 向下增大。
- 页码参数使用从 1 开始的页码。


## 2. 目录结构

```text
pdf_parser/
|-- main.py                         命令行调试和算法组合入口
|-- vector_api.py                   JSON 请求形式的独立调用入口
|-- utils.py                        bbox、变换和 anchor 选择等通用函数
|-- pages_manager/
|   |-- page_class.py               PathBase、VectorBase、TextBase 等页面数据容器
|   |-- pdf_page.py                 PDF 页面加载、矢量/文字提取和 PageData 构建
|   `-- split_page.py               页面区域检测和区域内容提取
|-- tools/
|   |-- vector_entity.py            矢量并查集、实体分组和 face 检测
|   |-- vector_box.py               框、圆、虚线区域和 cell 检测
|   |-- vector_matcher.py           bbox 查询和矢量形状匹配
|   `-- vector_visualize.py             VectorBase/TextBase PNG 渲染辅助
`-- processors/
    |-- table_extractor.py           信息栏/表格提取开发入口
    `-- diagram_extractor.py         图纸内容提取开发入口
```


## 3. 核心数据结构

### 3.1 PageData

`PdfPageManager.goto(page_number)` 返回 `PageData` 字典：

```python
{
    "pdf_path": str,
    "page_number": int,
    "page_height_pt": float,
    "page_bbox": (x0, y0, x1, y1),
    "vectors": VectorBase,
    "texts": TextBase
}
```

texts 中每个元素来自 PDF 的文字 span，而不是根据矢量推断：

```python
{
    "index": int,
    "text": str,
    "location": (x0, y0, x1, y1),
    "font": str | None,
    "font_size": float
}
```

### 3.2 PathBase

页面绘图对象被拆成 PathBase。直接公开的数据只有：

```python
{
    "type": "line" | "curve" | "rect" | "quad",
    "points": list[(x, y)],
    "bbox": {"x0": float, "y0": float, "x1": float, "y1": float}
}
```

`code` 和 `path_meta` 作为内部数据保存，通过 `inner_value` 一次性读取：

```python
path.type
path.points
path.bbox
path.inner_value
```

inner_value 返回：

```python
{
    "code": str,
    "path_meta": dict
}
```

op 已删除，因为它可以由 type 一一推导。曲线 bbox、曲线采样、矩形/quad 闭合、
圆形检测和渲染都直接使用公开的 type 判断路径几何类型。bbox 在 PathBase 初始化时
根据 type 和 points 一次性生成。需要 JSON 输出时调用 path.to_dict()。

### 3.3 VectorBase

位置：[`pages_manager/page_class.py`](pages_manager/page_class.py)

VectorBase 是页面矢量的可复用容器：

- vectors：list[PathBase]。
- source_indices：每个局部 vector 对应的原页面 vector index。
- tree：基于每个 PathBase 自动生成的 bbox 构建的 STRtree 空间索引。
- vector_count：矢量数量。

tree 主要供 VectorMatcher.query_bbox() 使用。STRtree 先快速找到与查询框相交
的候选，再直接从 vectors[index].bbox 构造 bbox geometry，判断候选是否被
查询框完整覆盖。

### 3.4 TextBase

位置：[`pages_manager/page_class.py`](pages_manager/page_class.py)

TextBase 是页面文字和区域文字的统一容器：

- texts：标准化后的文字记录列表，每条记录至少包含 text 和 location。
- text_count：文字数量。
- to_list()：返回可 JSON 序列化的文字记录列表。
- in_region(region)：返回中心点位于 region 内的文字记录列表。
- near_region(region, threshold)：返回中心点位于 region 外、但落在 region 外扩
  threshold 范围内的文字记录列表。

region 可以传入 Shapely Polygon/Geometry、bbox tuple/dict，或带 points/bbox 的
dict。当前区域判断使用文字 location 的中心点。


## 4. 页面加载：PdfPageManager

位置：[`pages_manager/pdf_page.py`](pages_manager/pdf_page.py)

PdfPageManager 管理 PDF 文档和当前页，建议作为上下文管理器使用：

```python
with PdfPageManager(pdf_path) as pages:
    page_data = pages.goto(6)
```

主要方法：

- goto(page_number)
  选择页面并重新提取该页的 vectors、texts 和 PageData。

- extract_page_vectors()
  读取 page.get_drawings()，支持 line、curve、rect、quad。

- extract_page_texts()
  读取 page.get_text("dict") 中的文字 span，保留源码文字 bbox、字体和字号。

- add_bboxes(vectors)
  为每个矢量计算紧致 bbox。

- build_page_data(vectors, texts)
  将当前页数据打包，并使用 vectors 构建 VectorBase，使用 texts 构建 TextBase。


## 5. 页面分区：SplitPageDetector

位置：[`pages_manager/split_page.py`](pages_manager/split_page.py)

SplitPageDetector 负责 content_bbox、info_bbox 和区域内容提取。

### 5.1 `detect(page_data)`

返回：

```python
{
    "pdf_path": str,
    "page_number": int,
    "page_bbox": bbox,
    "content_bbox": bbox,
    "info_bbox": bbox | None
}
```

### 5.2 `detect_content_bbox(page_data)`

核心流程：

1. 从 page_data["vectors"] 取得 VectorBase。
2. 创建 VectorDisjointSet。
3. 显式关闭包含合并和临近合并：

   ```python
   merge_contained = False
   merge_nearby = False
   ```

4. 仅根据真实相交/连接关系组成实体。
5. 选择 bbox 面积最大的实体作为外侧框矢量集合。
6. 将集合中每条开放矢量的两端沿自身方向轻微延长，使 T 字连接可靠相交。
7. 对线网执行 unary_union 和 polygonize。
8. 选择面积最大的 face，并返回其 bbox。
9. 无法检测时返回基于 page_bbox 内缩得到的 fallback bbox。

### 5.3 `detect_info_bbox(page_data, content_bbox)`

把 content_bbox 下边缘到页面下边缘之间的区域作为 info_bbox：

```python
(page_x0, content_y1, page_x1, page_y1)
```

若 content_bbox 已经到达页面底部，则返回 None。

### 5.4 `region_content(page_data, bbox, exclude_frame_vectors=True)`

返回指定 bbox 内的矢量和文字：

```python
{
    "vectors": VectorBase,
    "text": TextBase
}
```

文字内容和 location 均来自 PdfPageManager 提取的 PDF 文字 span。

exclude_frame_vectors=True 时，会排除构成区域外框的长边矢量。图纸内容区域
通常设为 True；信息栏或表格区域通常设为 False。


## 6. 矢量实体：VectorDisjointSet

位置：[`tools/vector_entity.py`](tools/vector_entity.py)

VectorDisjointSet 使用并查集把存在真实连接关系的矢量归为同一个 entity，并可
进一步执行可选合并。

构造参数中的重要开关：

- merge_contained
  是否根据闭合面包含关系合并实体。

- merge_nearby
  是否根据 bbox 距离合并临近实体。

主要公开方法：

- find(item)、union(left, right)
  标准并查集操作。

- groups()
  返回 root 到 vector id 列表的映射。

- merge_contained_entities()
  根据闭合 face 与其他实体矢量的关系执行包含合并。

- merge_nearby_entities()
  对距离小于阈值的实体执行临近合并。

- entity_faces(root)
  检测指定实体线网形成的 Polygon faces。

- entity_records(include_faces=True)
  返回每个实体的 bbox、vector 数量、face 等信息。

- result(include_vectors=False, include_faces=True)
  返回完整的结构化并查集结果。

SplitPageDetector 为了寻找真实外框，会关闭包含合并和临近合并。


## 7. 区域检测：VectorBoxDetector

位置：[`tools/vector_box.py`](tools/vector_box.py)

VectorBoxDetector 接受 VectorBase，并从页面矢量中检测几何区域。

主要方法：

- detect_boxes()
  检测矩形闭合区域。

- detect_circles()
  检测近似圆形闭合区域。

- detect_dashed()
  检测由虚线矢量组成的区域。

- detect_cells()
  汇总可作为表格 cell 的闭合区域。

- solid_vectors()、dashed_vectors()
  按 path_meta.dashes 区分实线和虚线。

- linear_vectors()
  返回可转成线段的矢量。

- circle_vector_groups()
  返回构成圆形区域的矢量组。

检测结果通常包含 polygon、bbox、边界 vector index 和内部 vector 数量。


## 8. 形状匹配：VectorMatcher

位置：[`tools/vector_matcher.py`](tools/vector_matcher.py)

VectorMatcher 同时提供 bbox 空间查询、单 shape 匹配和 shape group 验证。

### 8.1 `query_bbox()`

给定查询 bbox，返回 bbox 被查询区域完整覆盖的 vectors：

1. 将 PDF 坐标按需转换成 PyMuPDF 坐标。
2. 根据 slack 对查询 bbox 做对称扩张。
3. 使用 VectorBase.tree 查询相交候选。
4. 使用 vectors[index]["bbox"] 和 region.covers() 做完整包含精筛。
5. 根据 index 返回原始 vector 的副本。

### 8.2 `match_shape()`

在页面中寻找经过旋转、缩放和平移后与目标 shape 相同的 vector：

- 可选择是否要求 type 相同。
- 要求点数量一致。
- 通过点云距离估算 scale。
- 尝试循环点序和反向点序。
- 自动拟合旋转，或限制在指定 rotation_degrees 中。
- max_error 不超过 tolerance 时返回匹配结果。

每个匹配结果包含 rotation_degrees、scale、translation 和 max_error。

### 8.3 `compare_shape_groups()`

使用已知 rotation、scale 和 translation 变换目标 shape group，再与候选组进行
一对一匹配。结果包含：

- matched：是否不存在 missing 和 extra。
- pairings：成功配对记录。
- missing：未匹配的目标 shape。
- extra：未使用的候选 shape。


## 9. 区域渲染：vector_visualize

位置：[`tools/vector_visualize.py`](tools/vector_visualize.py)

`vector_visualize` 当前只提供一个渲染函数：

```python
render_vector_text_png(
    vector_base,
    text_base,
    output_dir="storage/output/images",
    filename="vector_text.png",
) -> Path
```

该函数接收 VectorBase 和 TextBase，将其中的矢量和文字绘制到一张 PNG 中，把
图片写入 output_dir/filename（output_dir 不存在时会自动创建），并返回写入的
文件路径 Path。output_dir 和 filename 均可省略，默认输出到 storage/output/images/
vector_text.png。

渲染前会先计算 vector/text 的合并最小 bbox，再用该 bbox 的左上角做零坐标
对齐，避免直接按原始 PDF 页面坐标绘制时出现大面积空白。坐标系沿用 PyMuPDF
页面坐标，即左上角为原点、x 向右、y 向下。TextBase 可以为空，VectorBase
必须至少包含一个 vector。


## 10. Extractor 开发入口

### 10.1 `table_extractor.py`

面向页面底部 information 区域：

1. PdfPageManager.goto() 生成 page_data。
2. SplitPageDetector.detect() 生成 info_bbox。
3. region_content(info_bbox, exclude_frame_vectors=False)。
4. 将返回的 vectors 和 text 传给 extract_table()。

目前 extract_table() 仍是开发中的 dummy 流程，但会调用 VectorBoxDetector 的
detect_cells()。

运行方式（从仓库根目录）：

```powershell
python -m pdf_parser.processors.table_extractor `
    --pdf-file-path "./storage/data/eplan_pdf/example.pdf" `
    --page 6
```

### 10.2 `diagram_extractor.py`

面向主要图纸 content 区域：

1. PdfPageManager.goto() 生成 page_data。
2. SplitPageDetector.detect() 生成 content_bbox。
3. region_content(content_bbox, exclude_frame_vectors=True)。
4. 将返回的 vectors 和 text 传给 extract_diagram()。

extract_diagram() 当前为 dummy 流程。

运行方式：

```powershell
python -m pdf_parser.processors.diagram_extractor `
    --pdf-file-path "./storage/data/eplan_pdf/example.pdf" `
    --page 6
```


## 11. `main.py` 与 `vector_api.py`

main.py 是本地命令行组合入口，用于调试 bbox 查询、区域检测、box 检测和匹配
流程。它负责在进入具体 run_* 流程前加载 page_data、生成 split_result，并生成
真实 content region/VectorBase。

vector_api.py 是独立的 JSON 调用入口：

- 从标准输入读取 JSON payload。
- 根据 action 调用页面解析、区域检测或矢量工具。
- 将 JSON 结果写到标准输出。
- 不依赖 main.py 的命令行流程。

二者共享 pages_manager、tools 和 utils，但入口逻辑相互独立。


## 12. 推荐的组合流程

```python
from pdf_parser.pages_manager import PdfPageManager, SplitPageDetector

with PdfPageManager(pdf_path) as pages:
    page_data = pages.goto(page_number)

    detector = SplitPageDetector()
    split_result = detector.detect(page_data)

    diagram_content = detector.region_content(
        page_data,
        split_result["content_bbox"],
        exclude_frame_vectors=True,
    )

    info_content = None
    if split_result["info_bbox"] is not None:
        info_content = detector.region_content(
            page_data,
            split_result["info_bbox"],
            exclude_frame_vectors=False,
        )
```

diagram_content 和 info_content 的格式一致：

```python
{
    "vectors": VectorBase,
    "text": TextBase
}
```


## 13. 依赖

主要依赖：

- PyMuPDF / fitz：PDF 页面、drawing 和文字 span 提取。
- Shapely：STRtree、空间关系、线网合并、polygonize 和 Polygon 运算。

安装仓库依赖：

```powershell
pip install -r requirements.txt
```
