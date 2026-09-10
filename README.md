# EplanDocker

[简体中文](README.md) | [English](README_EN.md)

EplanDocker 是一个面向 Eplan / 工程类 PDF 的异步解析与查询服务。系统采用 API-first 设计：前端只是公开 API 的一个客户端，其他 work package 也可以直接提交 PDF、查询任务状态并下载 parsing result。

## 架构

部署由三个容器组成：

| 服务 | 责任 |
| --- | --- |
| `api` | 接收上传、维护任务队列、提供结果和查询 API，并生成阅读器所需数据 |
| `pdf-parser-worker` | 领取任务、运行 `pdf_parser`、写入规范 parsing result |
| `frontend` | 提供任务看板和只读 PDF 阅读器，通过 Nginx 代理 API |

处理流程：

```text
客户端上传 PDF
    ↓
API 保存文件并创建 queued 任务
    ↓
worker 领取任务并持续上报进度
    ↓
parsing result 原子写入 storage（此时外部调用方即可下载）
    ↓
API 后台生成 reader-data
    ↓
文档进入 ready 状态并出现在前端阅读器
```

解析状态和前端发布状态相互独立。外部调用方不需要等待 reader-data 生成完成。

## 项目结构

```text
EplanDocker/
├─ api_service/             # FastAPI、SQLite 任务队列和 reader-data 发布器
├─ worker_service/          # 长期运行的 parser worker
├─ pdf_parser/              # PDF 解析和查询核心
├─ pdf_reader/              # 任务看板与 PDF 阅读器
├─ scripts/                 # reader-data 构建和已有结果导入工具
├─ docker/                  # 三个镜像的 Dockerfile 与 Nginx 配置
├─ storage/                 # 所有需要持久化的数据
├─ compose.yaml
└─ requirements.txt
```

## Storage 布局

三个服务使用同一个 `storage` bind mount，但前端容器不直接挂载它。

```text
storage/
├─ data/eplan_pdf/
│  └─ <document_id>/source.pdf
├─ output/
│  ├─ pdf_parsing_result/<document_id>/result.json
│  └─ reader_data/<document_id>/
├─ cache/
│  ├─ entity/
│  └─ frontend/
├─ state/jobs.sqlite3
└─ tmp/
```

客户端不能提交服务器文件路径，只能上传 PDF；服务端使用生成的 `document_id` 定位文件，避免文件名冲突和路径穿越。

## Docker 启动

要求 Docker Engine 或 Docker Desktop，并支持 Docker Compose v2。

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

至少应将 `.env` 中的 `EPLAN_INTERNAL_TOKEN` 替换为随机长字符串。使用托管 LLM 分类时还需要设置 `GROQ_API_KEY`。

启动三个服务：

```powershell
docker compose up --build
```

启动后可访问：

- 任务看板：`http://localhost:8080/tasks`
- PDF 阅读器：`http://localhost:8080/viewer/<document_id>`
- API：`http://localhost:8000`
- OpenAPI：`http://localhost:8000/docs`

停止服务：

```powershell
docker compose down
```

`storage` 是宿主机目录，执行 `docker compose down` 不会删除其中的 PDF、结果、任务记录或缓存。

## 直接调用 API

### 提交单个 PDF

```powershell
curl.exe -X POST http://localhost:8000/api/v1/parsing-jobs `
  -H "Idempotency-Key: wp-42-drawing-7" `
  -F "file=@drawing.pdf;type=application/pdf" `
  -F "pages=[1,2,8]"
```

接口返回 `202 Accepted`：

```json
{
  "id": "job_...",
  "document_id": "doc_...",
  "status": "queued",
  "status_url": "/api/v1/parsing-jobs/job_...",
  "result_url": "/api/v1/parsing-jobs/job_.../result"
}
```

`Idempotency-Key` 用于防止调用方重试请求时重复创建任务。

### 页面参数语义

- `pages=[]`：解析自动识别出的全部 diagram 页面。
- `pages=[1,2,8]`：只对这些目标页执行昂贵的 diagram parsing。
- 为了识别页面类型、符号表和跨页关系，解析器仍可能轻量读取目标列表之外的页面。
- `strict_pages=true` 当前会返回 `422`，避免调用方误以为列表外页面完全不会被读取。

### 查询任务与下载结果

```powershell
curl.exe http://localhost:8000/api/v1/parsing-jobs/JOB_ID
curl.exe http://localhost:8000/api/v1/parsing-jobs/JOB_ID/result
curl.exe -OJ http://localhost:8000/api/v1/parsing-jobs/JOB_ID/result/download
```

只要任务状态成为 `succeeded`，parsing result 就可以下载；`reader_status` 可能仍为 `pending` 或 `building`。

取消尚未完成的任务：

```powershell
curl.exe -X DELETE http://localhost:8000/api/v1/parsing-jobs/JOB_ID
```

### 提交批量任务

`page_specs` 必须与上传文件一一对应：

```powershell
curl.exe -X POST http://localhost:8000/api/v1/parsing-batches `
  -F "files=@first.pdf;type=application/pdf" `
  -F "files=@second.pdf;type=application/pdf" `
  -F 'page_specs=[{"pages":[1,2]},{"pages":[]}]'
```

```powershell
curl.exe http://localhost:8000/api/v1/parsing-batches/BATCH_ID
```

## 主要公开接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/parsing-jobs` | 上传单个 PDF 并创建任务 |
| `POST` | `/api/v1/parsing-batches` | 上传多个 PDF 并创建批次 |
| `GET` | `/api/v1/parsing-jobs/{job_id}` | 查询任务和进度 |
| `DELETE` | `/api/v1/parsing-jobs/{job_id}` | 请求取消任务 |
| `GET` | `/api/v1/parsing-jobs/{job_id}/result` | 获取规范 parsing result |
| `GET` | `/api/v1/documents` | 列出已完成且可阅读的文档 |
| `POST` | `/api/v1/queries` | 查询已完成文档 |

`/internal/v1/worker/*` 只供 Compose 内部网络中的 worker 使用，不应通过反向代理暴露。

## 导入已有解析结果

如果 PDF 已经具有完整的 parsing result，可以登记为成功任务，避免重新解析：

```powershell
python -m scripts.import_completed_document `
  path\to\drawing.pdf `
  path\to\drawing.json
```

脚本按 PDF SHA-256 生成稳定的 `document_id`，把文件移动到规范 storage 目录，并将任务登记为 `succeeded / reader pending`。API 启动后会补建 reader-data。

当前已有的 `TE2_Sealer.pdf` 及其 parsing result 已按此方式登记，无需再次提交解析任务。

## 本地开发

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

启动 API：

```powershell
uvicorn api_service.main:app --reload --port 8000
```

在另一个终端启动 worker：

```powershell
$env:EPLAN_API_URL = "http://127.0.0.1:8000"
python -m worker_service.main
```

启动前端：

```powershell
cd pdf_reader
npm install
npm run dev
```

Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。可通过 `EPLAN_API_URL` 覆盖代理目标。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EPLAN_STORAGE_ROOT` | 项目下的 `storage` | 持久化根目录 |
| `EPLAN_INTERNAL_TOKEN` | 开发用固定值 | API 与 worker 的内部认证 token，部署时必须更换 |
| `EPLAN_MAX_UPLOAD_BYTES` | `1073741824` | 单个上传文件最大字节数 |
| `EPLAN_API_URL` | `http://api:8000` | worker 或 Vite 使用的 API 地址 |
| `EPLAN_WORKER_POLL_SECONDS` | `2` | worker 无任务时的轮询间隔 |
| `GROQ_API_KEY` | 空 | 托管 LLM API key |

在 Linux bind mount 场景中，可通过 `.env` 的 `EPLAN_UID` 和 `EPLAN_GID` 让容器进程使用 storage 目录所有者的 UID/GID。

## 当前部署边界

- SQLite 队列适合单个 API 实例；可以横向增加 worker，但暂时不要同时运行多个 API 副本。
- worker 采用协作式取消，会在解析进度回调处停止，而不是强制终止正在执行的底层调用。
- 内部 worker 接口已有 token；公开 API 尚未提供用户级鉴权。对公网部署前应在 API 网关或服务层增加认证与权限控制。
- `pdf_parser/vector_api.py` 仍保留部分旧查询实现，但前端不再启动它的 stdio server；HTTP 边界已经迁入 `api_service`。
