# pdf_reader

这是 `EplanMaster` 的前端阅读器，基于 Vite + TypeScript 构建。

它的主要用途是：

- 显示预处理后的 PDF 页面
- 叠加矢量路径、文字、图片、链接等对象区域
- 点击对象后查看对应的 PDF 源片段与引用链
- 在下方 `PDF Play Ground` 中预览和测试矢量路径代码

## 运行前提

前端依赖预处理生成的数据目录：

```text
pdf_reader/public/reader-data
```

请先在仓库根目录执行：

```powershell
python scripts/build_pdf_reader_data.py
```

默认输入目录是：

```text
storage/data/eplan_pdf
```

## 安装与运行

在当前目录执行：

```powershell
npm install
npm run dev
```

## 生产构建

```powershell
npm run build
```

## 技术栈

- Vite
- TypeScript
- `pdfjs-dist`

## 说明

如果你需要完整的从环境搭建到预处理再到运行前端的全流程说明，请查看仓库根目录的 [README.md](../README.md)。
