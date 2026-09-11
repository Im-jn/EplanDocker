from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import fitz
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response

from api_service import database
from api_service.logging_config import (
    bind_request_id,
    configure_logging,
    get_logger,
    reset_request_id,
)
from eplan_runtime import (
    INTERNAL_TOKEN,
    MAX_UPLOAD_BYTES,
    PDF_ROOT,
    READER_ROOT,
    RESULT_ROOT,
    TEMP_ROOT,
    ensure_storage,
)
from api_service.publisher import request_publish
from pdf_parser.query_engine import query as handle_query


logger = get_logger("api")


def _job_response(job: dict[str, Any]) -> dict[str, Any]:
    job = dict(job)
    source_available = (PDF_ROOT / str(job["document_id"]) / "source.pdf").is_file()
    job["status_url"] = f"/api/v1/parsing-jobs/{job['id']}"
    job["result_url"] = f"/api/v1/parsing-jobs/{job['id']}/result"
    job["source_available"] = source_available
    if job.get("status") == "succeeded" and source_available:
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


def _document_storage_paths(document_id: str) -> tuple[Path, Path, Path]:
    if not document_id.startswith("doc_") or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in document_id):
        raise HTTPException(422, "Invalid document id")
    return PDF_ROOT / document_id, RESULT_ROOT / document_id, READER_ROOT / document_id


def _remove_document_artifacts(document_id: str, *, keep_pdf: bool = False) -> None:
    pdf_directory, result_directory, reader_directory = _document_storage_paths(document_id)
    targets = (result_directory, reader_directory) if keep_pdf else (pdf_directory, result_directory, reader_directory)
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)


def _remove_cached_pdf(document_id: str) -> None:
    pdf_directory, _, _ = _document_storage_paths(document_id)
    if pdf_directory.is_dir():
        shutil.rmtree(pdf_directory)


def _write_pdf_metadata(document_id: str, job: dict[str, Any]) -> None:
    pdf_directory, _, _ = _document_storage_paths(document_id)
    if not pdf_directory.is_dir():
        return
    (pdf_directory / "metadata.json").write_text(
        json.dumps(
            {
                "filename": str(job["original_filename"]),
                "page_count": int(job["page_count"]),
                "source_sha256": str(job["source_sha256"]),
                "source_size": int(job["source_size"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _read_pdf_metadata(path: Path) -> dict[str, Any]:
    metadata_path = path.parent / "metadata.json"
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _finish_deferred_delete(job: dict[str, Any]) -> None:
    """Finalize a worker-acknowledged delete without conflating job and PDF data."""
    job_id = str(job["id"])
    document_id = str(job["document_id"])
    delete_pdf = bool(job.get("delete_pdf"))
    database.delete_job_record(job_id)
    if delete_pdf:
        if not database.has_incomplete_document(document_id):
            _remove_cached_pdf(document_id)
            if not database.has_completed_document(document_id):
                _remove_document_artifacts(document_id, keep_pdf=True)
    else:
        _remove_document_artifacts(document_id, keep_pdf=True)


def _cached_pdf_index() -> dict[str, dict[str, Any]]:
    jobs_by_document: dict[str, dict[str, Any]] = {}
    for job in database.list_jobs(limit=500):
        jobs_by_document.setdefault(str(job["document_id"]), job)
    entries: dict[str, dict[str, Any]] = {}
    for path in PDF_ROOT.rglob("*.pdf"):
        relative_path = path.relative_to(PDF_ROOT).as_posix()
        cache_id = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]
        document_id = path.parent.name if path.name == "source.pdf" and path.parent.parent == PDF_ROOT else None
        job = jobs_by_document.get(document_id or "")
        metadata = _read_pdf_metadata(path)
        stat = path.stat()
        entries[cache_id] = {
            "cache_id": cache_id,
            "document_id": document_id,
            "filename": job["original_filename"] if job else metadata.get("filename", path.name),
            "relative_path": relative_path,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "page_count": job["page_count"] if job else metadata.get("page_count"),
            "job_id": job["id"] if job else None,
            "status": job["status"] if job else "cached",
            "reader_status": job["reader_status"] if job else None,
        }
    return entries


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
    _write_pdf_metadata(document_id, job)
    logger.info(
        "Parsing job submitted",
        extra={
            "action": "job_submitted",
            "job_id": job_id,
            "document_id": document_id,
            "batch_id": batch_id,
            "document_name": job["original_filename"],
            "total": page_count,
        },
    )
    return job


def _require_internal_token(token: str | None) -> None:
    if not token or token != INTERNAL_TOKEN:
        logger.warning("Rejected worker request with invalid internal token")
        raise HTTPException(401, "Invalid worker token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_storage()
    log_path = configure_logging()
    database.initialize()
    database.recover_interrupted_publish_jobs()
    logger.info("API service started", extra={"action": "service_started", "path": str(log_path)})
    try:
        yield
    finally:
        logger.info("API service stopped", extra={"action": "service_stopped"})


app = FastAPI(
    title="Eplan Processing API",
    version="1.0.0",
    description="Submit PDF parsing jobs and consume completed parsing results.",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def log_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    log_method = logger.error if exc.status_code >= 500 else logger.warning
    log_method(
        f"API request rejected: {exc.detail}",
        extra={
            "action": "request_rejected",
            "method": request.method,
            "path": request.url.path,
            "status_code": exc.status_code,
            "client": request.client.host if request.client else None,
        },
    )
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def log_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    summary = [
        {"location": ".".join(str(part) for part in error["loc"]), "type": error["type"]}
        for error in exc.errors()
    ]
    logger.warning(
        f"API request validation failed: {summary}",
        extra={
            "action": "validation_failed",
            "method": request.method,
            "path": request.url.path,
            "status_code": 422,
            "client": request.client.host if request.client else None,
        },
    )
    return JSONResponse({"detail": exc.errors()}, status_code=422)


@app.middleware("http")
async def log_request(request: Request, call_next: Any) -> Response:
    request_id = request.headers.get("x-request-id", "").strip()[:128] or uuid.uuid4().hex
    request_token = bind_request_id(request_id)
    started_at = perf_counter()
    details = {
        "request_id": request_id,
        "method": request.method,
        "path": request.url.path,
        "client": request.client.host if request.client else None,
    }
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception(
                "Unhandled API request failure",
                extra={**details, "duration_ms": round((perf_counter() - started_at) * 1000, 2), "error_type": type(exc).__name__},
            )
            raise
        response.headers["X-Request-ID"] = request_id
        quiet_internal_request = (
            request.url.path.endswith("/progress")
            or (request.url.path.endswith("/claim") and response.status_code == 204)
        )
        quiet_dashboard_poll = request.method == "GET" and request.url.path in {
            "/api/v1/parsing-jobs",
            "/api/v1/documents",
            "/api/v1/cached-pdfs",
        }
        log_method = (
            logger.debug
            if request.url.path == "/health"
            or quiet_internal_request
            or quiet_dashboard_poll
            else logger.info
        )
        log_method(
            "API request completed",
            extra={
                **details,
                "status_code": response.status_code,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            },
        )
        return response
    finally:
        reset_request_id(request_token)


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


@app.post("/api/v1/parsing-jobs/{job_id}/cancel")
def cancel_parsing_job(job_id: str) -> dict[str, Any]:
    job = database.request_cancel(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or no longer cancellable")
    logger.info(
        "Job cancellation requested",
        extra={"action": "job_cancel", "job_id": job_id, "document_id": job["document_id"]},
    )
    return _job_response(job)


@app.post("/api/v1/parsing-jobs/{job_id}/pause")
def pause_parsing_job(job_id: str) -> dict[str, Any]:
    before = database.get_job(job_id)
    if before is None:
        raise HTTPException(404, "Job not found")
    if before["status"] not in {"queued", "running"}:
        raise HTTPException(409, f"Job cannot be paused from status {before['status']}")
    job = database.request_pause(job_id) or before
    logger.info(
        "Job pause requested",
        extra={"action": "job_pause", "job_id": job_id, "document_id": job["document_id"]},
    )
    return _job_response(job)


@app.post("/api/v1/parsing-jobs/{job_id}/resume")
def resume_parsing_job(job_id: str) -> dict[str, Any]:
    before = database.get_job(job_id)
    if before is None:
        raise HTTPException(404, "Job not found")
    if before["status"] not in {"paused", "failed"}:
        raise HTTPException(409, f"Job cannot be resumed from status {before['status']}")
    job = database.resume_job(job_id) or before
    logger.info(
        "Job resumed from checkpoint",
        extra={"action": "job_resume", "job_id": job_id, "document_id": job["document_id"], "progress": job["progress"]},
    )
    return _job_response(job)


@app.delete("/api/v1/parsing-jobs/{job_id}")
def delete_parsing_job(job_id: str) -> JSONResponse:
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job["reader_status"] == "building":
        raise HTTPException(409, "Reader preprocessing is active; wait for it to finish before deletion")
    _write_pdf_metadata(str(job["document_id"]), job)
    found, deferred = database.request_job_delete(job_id)
    if not found:
        raise HTTPException(404, "Job not found")
    if not deferred:
        _remove_document_artifacts(str(job["document_id"]), keep_pdf=True)
    logger.info(
        "Job deletion requested" if deferred else "Job deleted",
        extra={"action": "job_delete", "job_id": job_id, "document_id": job["document_id"], "stage": "deferred" if deferred else "complete"},
    )
    return JSONResponse({"deleted": not deferred, "deferred": deferred}, status_code=202 if deferred else 200)


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
    return {"documents": [_job_response(job) for job in database.list_completed_documents()]}


@app.get("/api/v1/cached-pdfs")
def list_cached_pdfs() -> dict[str, Any]:
    return {"pdfs": list(_cached_pdf_index().values())}


@app.post("/api/v1/cached-pdfs/{cache_id}/reprocess", status_code=202)
def reprocess_cached_pdf(cache_id: str) -> dict[str, Any]:
    entry = _cached_pdf_index().get(cache_id)
    if entry is None:
        raise HTTPException(404, "Cached PDF not found")
    source_path = PDF_ROOT / str(entry["relative_path"])
    document_id = entry.get("document_id")
    if document_id:
        current = database.get_job(str(entry["job_id"])) if entry.get("job_id") else None
        if current and current["status"] in {"running", "pause_requested", "delete_requested", "cancel_requested"}:
            raise HTTPException(409, f"Document is currently {current['status']}")
        if current is not None:
            _remove_document_artifacts(str(document_id), keep_pdf=True)
            job = database.reset_document_job(str(document_id))
            if job is None:
                raise HTTPException(404, "Cached document record not found")
            logger.info(
                "Cached PDF reprocessing queued",
                extra={"action": "pdf_reprocess", "job_id": job["id"], "document_id": document_id, "document_name": entry["filename"]},
            )
            return _job_response(job)

        page_count = _inspect_pdf(source_path)
        digest = hashlib.sha256()
        with source_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        job = database.create_job({
            "id": f"job_{uuid.uuid4().hex}",
            "batch_id": None,
            "document_id": str(document_id),
            "original_filename": str(entry["filename"]),
            "source_sha256": digest.hexdigest(),
            "source_size": source_path.stat().st_size,
            "page_count": page_count,
            "pages": list(range(1, page_count + 1)),
            "strict_pages": False,
            "idempotency_key": None,
        })
        _write_pdf_metadata(str(document_id), job)
        logger.info(
            "Cached PDF reprocessing queued",
            extra={"action": "pdf_reprocess", "job_id": job["id"], "document_id": document_id, "document_name": entry["filename"]},
        )
        return _job_response(job)

    document_id = f"doc_{uuid.uuid4().hex}"
    target_directory, _, _ = _document_storage_paths(document_id)
    target_directory.mkdir(parents=True, exist_ok=False)
    target_path = target_directory / "source.pdf"
    source_path.replace(target_path)
    page_count = _inspect_pdf(target_path)
    digest = hashlib.sha256()
    with target_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    job = database.create_job({
        "id": f"job_{uuid.uuid4().hex}",
        "batch_id": None,
        "document_id": document_id,
        "original_filename": str(entry["filename"]),
        "source_sha256": digest.hexdigest(),
        "source_size": target_path.stat().st_size,
        "page_count": page_count,
        "pages": list(range(1, page_count + 1)),
        "strict_pages": False,
        "idempotency_key": None,
    })
    _write_pdf_metadata(document_id, job)
    logger.info(
        "Cached PDF reprocessing queued",
        extra={"action": "pdf_reprocess", "job_id": job["id"], "document_id": document_id, "document_name": entry["filename"]},
    )
    return _job_response(job)


@app.delete("/api/v1/cached-pdfs/{cache_id}")
def delete_cached_pdf(cache_id: str) -> JSONResponse:
    entry = _cached_pdf_index().get(cache_id)
    if entry is None:
        raise HTTPException(404, "Cached PDF not found")
    document_id = entry.get("document_id")
    if document_id:
        job = database.get_job(str(entry["job_id"])) if entry.get("job_id") else None
        if job and job["reader_status"] == "building":
            raise HTTPException(409, "Reader preprocessing is active; wait for it to finish before deletion")
        _, deferred = database.request_incomplete_document_delete(str(document_id))
        if deferred:
            logger.info(
                "Cached PDF deletion waiting for active worker",
                extra={"action": "pdf_delete", "document_id": document_id, "document_name": entry["filename"], "stage": "deferred"},
            )
            return JSONResponse({"deleted": False, "deferred": True}, status_code=202)
        _remove_cached_pdf(str(document_id))
        if not database.has_completed_document(str(document_id)):
            _remove_document_artifacts(str(document_id), keep_pdf=True)
        logger.info(
            "Cached PDF deleted",
            extra={"action": "pdf_delete", "document_id": document_id, "document_name": entry["filename"], "stage": "complete"},
        )
        return JSONResponse({"deleted": True, "deferred": False})

    source_path = (PDF_ROOT / str(entry["relative_path"])).resolve()
    if source_path.parent != PDF_ROOT.resolve():
        raise HTTPException(422, "Unsupported cached PDF location")
    source_path.unlink(missing_ok=True)
    logger.info(
        "Cached PDF deleted",
        extra={"action": "pdf_delete", "document_name": entry["filename"], "stage": "complete"},
    )
    return JSONResponse({"deleted": True, "deferred": False})


@app.get("/api/v1/documents/manifest")
def reader_manifest() -> dict[str, Any]:
    documents = []
    for job in database.list_ready_documents():
        if not (PDF_ROOT / str(job["document_id"]) / "source.pdf").is_file():
            continue
        manifest_path = READER_ROOT / job["document_id"] / "document.json"
        try:
            documents.append(json.loads(manifest_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return {"documents": documents}


@app.get("/api/v1/documents/{document_id}")
def read_document(document_id: str) -> dict[str, Any]:
    job = database.get_document(document_id)
    if job is None:
        raise HTTPException(404, "Completed document not found")
    return _job_response(job)


@app.post("/api/v1/documents/{document_id}/prepare", status_code=202)
def prepare_document(document_id: str) -> dict[str, Any]:
    if not (PDF_ROOT / document_id / "source.pdf").is_file():
        raise HTTPException(409, "Source PDF has been deleted; parsing result is still available")
    job = request_publish(document_id)
    if job is None:
        raise HTTPException(404, "Completed document not found")
    return _job_response(job)


@app.get("/api/v1/documents/{document_id}/file")
def read_document_file(document_id: str) -> FileResponse:
    job = database.get_document(document_id, ready_only=True)
    if job is None:
        raise HTTPException(404, "Completed document not found")
    source_path = PDF_ROOT / document_id / "source.pdf"
    if not source_path.is_file():
        raise HTTPException(404, "Source PDF has been deleted; parsing result is still available")
    return FileResponse(
        source_path,
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
        raise HTTPException(404, "Preprocessed page data not found")
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
        logger.exception(
            "Document query failed",
            extra={"action": "query_failed", "document_id": document_id, "error_type": type(exc).__name__},
        )
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "result": result})


@app.post("/internal/v1/worker/jobs/claim")
def worker_claim(x_internal_token: str | None = Header(None)) -> Response:
    _require_internal_token(x_internal_token)
    job = database.claim_job()
    if job is None:
        return Response(status_code=204)
    logger.info(
        "Worker claimed parsing job",
        extra={"action": "worker_claim", "job_id": job["id"], "document_id": job["document_id"], "document_name": job["original_filename"], "progress": job["progress"]},
    )
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
    logger.info(
        str(payload.get("message", "Parsing progress")),
        extra={"action": "worker_progress", "job_id": job_id, "document_id": job["document_id"], "progress": job["progress"], "stage": job["stage"]},
    )
    return {
        "cancel_requested": job["status"] == "cancel_requested",
        "pause_requested": job["status"] == "pause_requested",
        "delete_requested": job["status"] == "delete_requested",
    }


@app.post("/internal/v1/worker/jobs/{job_id}/complete")
async def worker_complete(
    job_id: str,
    request: Request,
    x_internal_token: str | None = Header(None),
) -> dict[str, bool]:
    _require_internal_token(x_internal_token)
    payload = await request.json()
    current = database.get_job(job_id)
    if current and current["status"] == "delete_requested":
        _finish_deferred_delete(current)
        logger.info(
            "Deferred job deletion completed",
            extra={"action": "job_delete_completed", "job_id": job_id, "document_id": current["document_id"]},
        )
        return {"ok": True}
    database.finish_job(job_id, status="succeeded", result_path=str(payload.get("result_path") or ""))
    logger.info(
        "Parsing job completed",
        extra={"action": "worker_completed", "job_id": job_id, "document_id": current["document_id"] if current else None, "progress": 100, "stage": "parsed"},
    )
    return {"ok": True}


@app.post("/internal/v1/worker/jobs/{job_id}/fail")
async def worker_fail(
    job_id: str,
    request: Request,
    x_internal_token: str | None = Header(None),
) -> dict[str, bool]:
    _require_internal_token(x_internal_token)
    payload = await request.json()
    current = database.get_job(job_id)
    if current and current["status"] == "delete_requested":
        _finish_deferred_delete(current)
        logger.info(
            "Deferred job deletion completed",
            extra={"action": "job_delete_completed", "job_id": job_id, "document_id": current["document_id"]},
        )
        return {"ok": True}
    if payload.get("paused"):
        database.finish_pause(job_id)
        logger.info(
            "Parsing job paused at checkpoint",
            extra={"action": "worker_paused", "job_id": job_id, "document_id": current["document_id"] if current else None, "progress": current["progress"] if current else None},
        )
        return {"ok": True}
    status = "cancelled" if payload.get("cancelled") else "failed"
    error = str(payload.get("error") or status)
    database.finish_job(job_id, status=status, error=error)
    log_method = logger.warning if status == "cancelled" else logger.error
    log_method(
        f"Parsing job {status}: {error}",
        extra={"action": f"worker_{status}", "job_id": job_id, "document_id": current["document_id"] if current else None, "progress": current["progress"] if current else None, "stage": status},
    )
    return {"ok": True}
