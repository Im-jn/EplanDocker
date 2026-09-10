from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from api_service import database
from eplan_runtime import (
    INTERNAL_TOKEN,
    MAX_UPLOAD_BYTES,
    PDF_ROOT,
    READER_ROOT,
    RESULT_ROOT,
    TEMP_ROOT,
    ensure_storage,
)
from api_service.publisher import publisher_loop
from pdf_parser.query_engine import query as handle_query


def _job_response(job: dict[str, Any]) -> dict[str, Any]:
    job = dict(job)
    job["status_url"] = f"/api/v1/parsing-jobs/{job['id']}"
    job["result_url"] = f"/api/v1/parsing-jobs/{job['id']}/result"
    if job.get("document_ready"):
        job["viewer_url"] = f"/viewer/{job['document_id']}"
    job.pop("result_path", None)
    return job


def _parse_pages(raw: str, page_count: int) -> list[int]:
    if not raw.strip():
        return []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        loaded = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(loaded, list):
        raise HTTPException(422, "pages must be a JSON array or comma-separated page numbers")
    try:
        pages = sorted({int(value) for value in loaded})
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "pages must contain integers") from exc
    invalid = [page for page in pages if page < 1 or page > page_count]
    if invalid:
        raise HTTPException(422, f"pages outside 1..{page_count}: {invalid}")
    return pages


def _inspect_pdf(path: Path) -> int:
    with path.open("rb") as source:
        header = source.read(5)
    if header != b"%PDF-":
        raise HTTPException(415, "Only PDF files are accepted")
    try:
        with fitz.open(path) as document:
            if document.page_count < 1:
                raise HTTPException(422, "PDF has no pages")
            return document.page_count
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Invalid or unreadable PDF: {exc}") from exc


def _store_upload(upload: UploadFile, document_id: str) -> tuple[Path, str, int]:
    target_directory = PDF_ROOT / document_id
    temporary_path = TEMP_ROOT / f"upload-{document_id}.part"
    target_path = target_directory / "source.pdf"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary_path.open("wb") as destination:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"PDF exceeds {MAX_UPLOAD_BYTES} bytes")
                digest.update(chunk)
                destination.write(chunk)
        _inspect_pdf(temporary_path)
        target_directory.mkdir(parents=True, exist_ok=False)
        temporary_path.replace(target_path)
        return target_path, digest.hexdigest(), size
    except Exception:
        temporary_path.unlink(missing_ok=True)
        if target_directory.exists():
            shutil.rmtree(target_directory)
        raise


def _create_uploaded_job(
    upload: UploadFile,
    *,
    raw_pages: str,
    strict_pages: bool,
    batch_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    if strict_pages:
        raise HTTPException(
            422,
            "strict_pages is not supported yet; pages currently select expensive diagram parsing targets",
        )
    if idempotency_key:
        existing = database.get_job_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
    job_id = f"job_{uuid.uuid4().hex}"
    document_id = f"doc_{uuid.uuid4().hex}"
    path, sha256, size = _store_upload(upload, document_id)
    page_count = _inspect_pdf(path)
    pages = _parse_pages(raw_pages, page_count)
    job = database.create_job(
        {
            "id": job_id,
            "batch_id": batch_id,
            "document_id": document_id,
            "original_filename": Path(upload.filename or "document.pdf").name,
            "source_sha256": sha256,
            "source_size": size,
            "page_count": page_count,
            "pages": pages,
            "strict_pages": False,
            "idempotency_key": idempotency_key,
        }
    )
    return job


def _require_internal_token(token: str | None) -> None:
    if not token or token != INTERNAL_TOKEN:
        raise HTTPException(401, "Invalid worker token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_storage()
    database.initialize()
    stop_event = threading.Event()
    publisher = threading.Thread(target=publisher_loop, args=(stop_event,), daemon=True)
    publisher.start()
    yield
    stop_event.set()
    publisher.join(timeout=10)


app = FastAPI(
    title="Eplan Processing API",
    version="1.0.0",
    description="Submit PDF parsing jobs and consume completed parsing results.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/parsing-jobs", status_code=202)
def create_parsing_job(
    file: UploadFile = File(...),
    pages: str = Form("[]"),
    strict_pages: bool = Form(False),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return _job_response(
        _create_uploaded_job(
            file,
            raw_pages=pages,
            strict_pages=strict_pages,
            batch_id=None,
            idempotency_key=idempotency_key,
        )
    )


@app.post("/api/v1/parsing-batches", status_code=202)
def create_parsing_batch(
    files: list[UploadFile] = File(...),
    page_specs: str = Form("[]"),
) -> dict[str, Any]:
    try:
        specs = json.loads(page_specs)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "page_specs must be a JSON array") from exc
    if not isinstance(specs, list):
        raise HTTPException(422, "page_specs must be a JSON array")
    if specs and len(specs) != len(files):
        raise HTTPException(422, "page_specs must contain one entry per uploaded PDF")
    batch_id = f"batch_{uuid.uuid4().hex}"
    jobs = []
    for index, upload in enumerate(files):
        spec = specs[index] if index < len(specs) and isinstance(specs[index], dict) else {}
        jobs.append(
            _job_response(
                _create_uploaded_job(
                    upload,
                    raw_pages=json.dumps(spec.get("pages", [])),
                    strict_pages=bool(spec.get("strict_pages", False)),
                    batch_id=batch_id,
                    idempotency_key=None,
                )
            )
        )
    return {"batch_id": batch_id, "jobs": jobs}


@app.get("/api/v1/parsing-batches/{batch_id}")
def read_parsing_batch(batch_id: str) -> dict[str, Any]:
    jobs = database.list_jobs(batch_id=batch_id, limit=500)
    if not jobs:
        raise HTTPException(404, "Batch not found")
    statuses = {job["status"] for job in jobs}
    if statuses <= {"succeeded", "failed", "cancelled"}:
        status = "completed"
    elif "running" in statuses or "cancel_requested" in statuses:
        status = "running"
    else:
        status = "queued"
    return {
        "batch_id": batch_id,
        "status": status,
        "jobs": [_job_response(job) for job in jobs],
    }


@app.get("/api/v1/parsing-jobs")
def list_parsing_jobs(batch_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    return {"jobs": [_job_response(job) for job in database.list_jobs(batch_id=batch_id, limit=limit)]}


@app.get("/api/v1/parsing-jobs/{job_id}")
def read_parsing_job(job_id: str) -> dict[str, Any]:
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return _job_response(job)


@app.delete("/api/v1/parsing-jobs/{job_id}")
def cancel_parsing_job(job_id: str) -> dict[str, Any]:
    job = database.request_cancel(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or no longer cancellable")
    return _job_response(job)


@app.get("/api/v1/parsing-jobs/{job_id}/result")
def read_parsing_result(job_id: str, download: bool = False) -> FileResponse:
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if not job["result_available"]:
        raise HTTPException(409, f"Result is not available; job status is {job['status']}")
    result_path = RESULT_ROOT / job["document_id"] / "result.json"
    if not result_path.is_file():
        raise HTTPException(500, "Recorded parsing result is missing")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        result_path,
        media_type="application/json",
        filename=f"{Path(job['original_filename']).stem}.json" if download else None,
        content_disposition_type=disposition,
    )


@app.get("/api/v1/parsing-jobs/{job_id}/result/download")
def download_parsing_result(job_id: str) -> FileResponse:
    return read_parsing_result(job_id, download=True)


@app.get("/api/v1/documents")
def list_documents() -> dict[str, Any]:
    return {"documents": [_job_response(job) for job in database.list_ready_documents()]}


@app.get("/api/v1/documents/manifest")
def reader_manifest() -> dict[str, Any]:
    documents = []
    for job in database.list_ready_documents():
        manifest_path = READER_ROOT / job["document_id"] / "document.json"
        try:
            documents.append(json.loads(manifest_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return {"documents": documents}


@app.get("/api/v1/documents/{document_id}")
def read_document(document_id: str) -> dict[str, Any]:
    job = database.get_document(document_id, ready_only=True)
    if job is None:
        raise HTTPException(404, "Completed document not found")
    return _job_response(job)


@app.get("/api/v1/documents/{document_id}/file")
def read_document_file(document_id: str) -> FileResponse:
    job = database.get_document(document_id, ready_only=True)
    if job is None:
        raise HTTPException(404, "Completed document not found")
    return FileResponse(
        PDF_ROOT / document_id / "source.pdf",
        media_type="application/pdf",
        filename=job["original_filename"],
        content_disposition_type="inline",
    )


@app.get("/api/v1/documents/{document_id}/manifest")
def read_document_manifest(document_id: str) -> FileResponse:
    if database.get_document(document_id, ready_only=True) is None:
        raise HTTPException(404, "Completed document not found")
    return FileResponse(READER_ROOT / document_id / "document.json", media_type="application/json")


@app.get("/api/v1/documents/{document_id}/pages/{page_number}")
def read_document_page(document_id: str, page_number: int) -> FileResponse:
    if database.get_document(document_id, ready_only=True) is None:
        raise HTTPException(404, "Completed document not found")
    page_path = READER_ROOT / document_id / "pages" / f"page-{page_number:04d}.json"
    if not page_path.is_file():
        raise HTTPException(404, "Page data not found")
    return FileResponse(page_path, media_type="application/json")


def _document_id_from_payload(payload: dict[str, Any]) -> str | None:
    if payload.get("document_id"):
        return str(payload["document_id"])
    pdf_url = str(payload.get("pdf_url") or "")
    prefix = "/api/v1/documents/"
    if pdf_url.startswith(prefix):
        return pdf_url[len(prefix):].split("/", 1)[0]
    return None


@app.post("/api/v1/queries")
@app.post("/api/vector-matcher")
async def query_document(request: Request) -> JSONResponse:
    payload = await request.json()
    document_id = _document_id_from_payload(payload)
    if not document_id:
        raise HTTPException(422, "document_id is required")
    job = database.get_document(document_id, ready_only=True)
    if job is None:
        raise HTTPException(404, "Completed document not found")
    mode = str(payload.get("mode", "match"))
    if mode in {"extract_info", "symbols", "symbol_search", "cancel_extract_info"}:
        raise HTTPException(409, "Interactive parsing is disabled; submit a parsing job instead")
    safe_payload = dict(payload)
    safe_payload["pdf_path"] = str(PDF_ROOT / document_id / "source.pdf")
    safe_payload.pop("llm_api_key", None)
    try:
        result = await run_in_threadpool(handle_query, safe_payload)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "result": result})


@app.post("/internal/v1/worker/jobs/claim")
def worker_claim(x_internal_token: str | None = Header(None)) -> JSONResponse:
    _require_internal_token(x_internal_token)
    job = database.claim_job()
    if job is None:
        return JSONResponse(status_code=204, content=None)
    return JSONResponse(job)


@app.post("/internal/v1/worker/jobs/{job_id}/progress")
async def worker_progress(
    job_id: str,
    request: Request,
    x_internal_token: str | None = Header(None),
) -> dict[str, Any]:
    _require_internal_token(x_internal_token)
    payload = await request.json()
    job = database.update_progress(
        job_id,
        int(payload.get("progress", 0)),
        str(payload.get("stage", "parsing")),
        str(payload.get("message", "")),
    )
    if job is None:
        raise HTTPException(404, "Job not found")
    return {"cancel_requested": job["status"] == "cancel_requested"}


@app.post("/internal/v1/worker/jobs/{job_id}/complete")
async def worker_complete(
    job_id: str,
    request: Request,
    x_internal_token: str | None = Header(None),
) -> dict[str, bool]:
    _require_internal_token(x_internal_token)
    payload = await request.json()
    database.finish_job(job_id, status="succeeded", result_path=str(payload.get("result_path") or ""))
    return {"ok": True}


@app.post("/internal/v1/worker/jobs/{job_id}/fail")
async def worker_fail(
    job_id: str,
    request: Request,
    x_internal_token: str | None = Header(None),
) -> dict[str, bool]:
    _require_internal_token(x_internal_token)
    payload = await request.json()
    status = "cancelled" if payload.get("cancelled") else "failed"
    database.finish_job(job_id, status=status, error=str(payload.get("error") or status))
    return {"ok": True}
