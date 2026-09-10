# EplanDocker

[简体中文](README.md) | [English](README_EN.md)

EplanDocker is an asynchronous parsing and query service for Eplan and engineering PDFs. It follows an API-first design: the frontend is only one client of the public API, while other work packages can submit PDFs, track jobs, and download parsing results directly.

## Architecture

The deployment consists of three containers:

| Service | Responsibility |
| --- | --- |
| `api` | Accepts uploads, maintains the job queue, exposes result and query APIs, and generates reader data |
| `pdf-parser-worker` | Claims jobs, runs `pdf_parser`, and writes canonical parsing results |
| `frontend` | Serves the task dashboard and read-only PDF viewer, and proxies API requests through Nginx |

Processing flow:

```text
Client uploads a PDF
    ↓
API stores the file and creates a queued job
    ↓
Worker claims the job and continuously reports progress
    ↓
Parsing result is written atomically to storage
(external clients can download it at this point)
    ↓
API generates reader data in the background
    ↓
Document becomes ready and appears in the frontend viewer
```

Parsing status and frontend publication status are independent. External clients do not need to wait for reader-data generation to finish.

## Project Structure

```text
EplanDocker/
├─ api_service/             # FastAPI, SQLite job queue, and reader-data publisher
├─ worker_service/          # Long-running parser worker
├─ pdf_parser/              # PDF parsing and query core
├─ pdf_reader/              # Task dashboard and PDF viewer
├─ scripts/                 # Reader-data builder and existing-result importer
├─ docker/                  # Dockerfiles for all services and Nginx configuration
├─ storage/                 # All persistent data
├─ compose.yaml
└─ requirements.txt
```

## Storage Layout

The API and worker share the same `storage` bind mount. The frontend container does not mount it directly.

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

Clients cannot submit server-side file paths and must upload PDFs. The server locates files through generated `document_id` values, preventing filename collisions and path traversal.

## Start with Docker

Docker Engine or Docker Desktop with Docker Compose v2 is required.

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

At minimum, replace `EPLAN_INTERNAL_TOKEN` in `.env` with a long random string. Set `GROQ_API_KEY` as well when using hosted LLM classification.

Start all three services:

```powershell
docker compose up --build
```

After startup, open:

- Task dashboard: `http://localhost:8080/tasks`
- PDF viewer: `http://localhost:8080/viewer/<document_id>`
- API: `http://localhost:8000`
- OpenAPI documentation: `http://localhost:8000/docs`

Stop the services:

```powershell
docker compose down
```

`storage` is a host directory. Running `docker compose down` does not remove PDFs, results, job records, or caches stored there.

## Call the API Directly

### Submit One PDF

```powershell
curl.exe -X POST http://localhost:8000/api/v1/parsing-jobs `
  -H "Idempotency-Key: wp-42-drawing-7" `
  -F "file=@drawing.pdf;type=application/pdf" `
  -F "pages=[1,2,8]"
```

The endpoint returns `202 Accepted`:

```json
{
  "id": "job_...",
  "document_id": "doc_...",
  "status": "queued",
  "status_url": "/api/v1/parsing-jobs/job_...",
  "result_url": "/api/v1/parsing-jobs/job_.../result"
}
```

Use `Idempotency-Key` to prevent duplicate jobs when a client retries a request.

### Page Parameter Semantics

- `pages=[]`: parse all automatically detected diagram pages.
- `pages=[1,2,8]`: run expensive diagram parsing only on the specified target pages.
- The parser may still read other pages lightly to identify page types, symbol tables, and cross-page relationships.
- `strict_pages=true` currently returns `422`, preventing clients from incorrectly assuming that pages outside the list will never be read.

### Check a Job and Download Its Result

```powershell
curl.exe http://localhost:8000/api/v1/parsing-jobs/JOB_ID
curl.exe http://localhost:8000/api/v1/parsing-jobs/JOB_ID/result
curl.exe -OJ http://localhost:8000/api/v1/parsing-jobs/JOB_ID/result/download
```

The parsing result can be downloaded as soon as the job reaches `succeeded`; `reader_status` may still be `pending` or `building`.

Request cancellation for an unfinished job:

```powershell
curl.exe -X DELETE http://localhost:8000/api/v1/parsing-jobs/JOB_ID
```

### Submit a Batch

`page_specs` must contain one entry for each uploaded file:

```powershell
curl.exe -X POST http://localhost:8000/api/v1/parsing-batches `
  -F "files=@first.pdf;type=application/pdf" `
  -F "files=@second.pdf;type=application/pdf" `
  -F 'page_specs=[{"pages":[1,2]},{"pages":[]}]'
```

```powershell
curl.exe http://localhost:8000/api/v1/parsing-batches/BATCH_ID
```

## Main Public Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/parsing-jobs` | Upload one PDF and create a job |
| `POST` | `/api/v1/parsing-batches` | Upload multiple PDFs and create a batch |
| `GET` | `/api/v1/parsing-jobs/{job_id}` | Get job status and progress |
| `DELETE` | `/api/v1/parsing-jobs/{job_id}` | Request job cancellation |
| `GET` | `/api/v1/parsing-jobs/{job_id}/result` | Get the canonical parsing result |
| `GET` | `/api/v1/documents` | List completed, readable documents |
| `POST` | `/api/v1/queries` | Query a completed document |

`/internal/v1/worker/*` is reserved for the worker on the internal Compose network and should not be exposed through the reverse proxy.

## Import an Existing Parsing Result

If a PDF already has a complete parsing result, register it as a successful job to avoid parsing it again:

```powershell
python -m scripts.import_completed_document `
  path\to\drawing.pdf `
  path\to\drawing.json
```

The script derives a stable `document_id` from the PDF SHA-256 hash, moves both files into the canonical storage layout, and registers the job as `succeeded / reader pending`. The API builds the missing reader data after startup.

The existing `TE2_Sealer.pdf` and its parsing result have already been registered this way and do not need to be resubmitted for parsing.

## Local Development

Install the Python dependencies:

```powershell
pip install -r requirements.txt
```

Start the API:

```powershell
uvicorn api_service.main:app --reload --port 8000
```

Start the worker in another terminal:

```powershell
$env:EPLAN_API_URL = "http://127.0.0.1:8000"
python -m worker_service.main
```

Start the frontend:

```powershell
cd pdf_reader
npm install
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000`. Override the target with `EPLAN_API_URL`.

## Configuration

| Environment Variable | Default | Description |
| --- | --- | --- |
| `EPLAN_STORAGE_ROOT` | Project `storage` directory | Persistent storage root |
| `EPLAN_INTERNAL_TOKEN` | Fixed development value | Internal API/worker authentication token; must be changed for deployment |
| `EPLAN_MAX_UPLOAD_BYTES` | `1073741824` | Maximum size of one uploaded file in bytes |
| `EPLAN_API_URL` | `http://api:8000` | API address used by the worker or Vite |
| `EPLAN_WORKER_POLL_SECONDS` | `2` | Worker polling interval while no job is available |
| `GROQ_API_KEY` | Empty | Hosted LLM API key |

For Linux bind mounts, set `EPLAN_UID` and `EPLAN_GID` in `.env` so container processes use the UID/GID that owns the storage directory.

## Current Deployment Boundaries

- The SQLite queue supports one API instance. Workers can be scaled horizontally, but do not run multiple API replicas yet.
- Worker cancellation is cooperative: parsing stops at a progress callback rather than forcibly interrupting an active low-level call.
- Internal worker endpoints require a token. The public API does not yet implement user authentication; add authentication and authorization at the API gateway or service layer before exposing it publicly.
- `pdf_parser/vector_api.py` still contains parts of the legacy query implementation, but the frontend no longer starts its stdio server. The HTTP boundary now lives in `api_service`.
