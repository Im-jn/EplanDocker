from __future__ import annotations

import json
import shutil
import threading

from api_service import database
from api_service.logging_config import get_logger
from eplan_runtime import PDF_ROOT, READER_ROOT, TEMP_ROOT
from scripts.build_pdf_reader_data import (
    READER_SCHEMA_VERSION,
    build_document_data,
)


logger = get_logger("reader")


def _reader_data_is_current(document_id: str) -> bool:
    manifest_path = READER_ROOT / document_id / "document.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("reader_schema_version") == READER_SCHEMA_VERSION


def publish_document(job: dict[str, object]) -> None:
    document_id = str(job["document_id"])
    job_id = str(job["id"])
    pdf_path = PDF_ROOT / document_id / "source.pdf"
    temporary_root = TEMP_ROOT / f"reader-{document_id}"
    final_root = READER_ROOT / document_id
    logger.info(
        "Reader preprocessing started",
        extra={"action": "reader_started", "job_id": job_id, "document_id": document_id, "document_name": job["original_filename"]},
    )

    def report_progress(current: int, total: int) -> None:
        database.update_publish_progress(job_id, current, total)
        logger.info(
            "Reader page prepared",
            extra={"action": "reader_progress", "job_id": job_id, "document_id": document_id, "current": current, "total": total},
        )

    try:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        build_document_data(
            pdf_path,
            temporary_root,
            progress_callback=report_progress,
            document_id=document_id,
            title=str(job["original_filename"]),
            copy_pdf=False,
            api_base="/api/v1",
        )
        built_root = temporary_root / "documents" / document_id
        if final_root.exists():
            shutil.rmtree(final_root)
        final_root.parent.mkdir(parents=True, exist_ok=True)
        built_root.replace(final_root)
        shutil.rmtree(temporary_root, ignore_errors=True)
        database.finish_publish(job_id, ready=True)
        logger.info(
            "Reader preprocessing completed",
            extra={"action": "reader_completed", "job_id": job_id, "document_id": document_id},
        )
    except Exception as exc:
        database.finish_publish(job_id, ready=False, error=str(exc))
        logger.exception(
            "Reader preprocessing failed",
            extra={"action": "reader_failed", "job_id": job_id, "document_id": document_id, "error_type": type(exc).__name__},
        )


def request_publish(document_id: str) -> dict[str, object] | None:
    """Start whole-document reader preprocessing and return its current state."""
    job = database.get_document(document_id)
    if job is None:
        return None
    if job["reader_status"] == "ready" and not _reader_data_is_current(document_id):
        database.invalidate_document_publish(document_id)
        job = database.get_document(document_id)
        if job is None:
            return None
    if job["reader_status"] in {"pending", "failed"}:
        claimed = database.begin_document_publish(document_id)
        if claimed is not None:
            threading.Thread(
                target=publish_document,
                args=(claimed,),
                daemon=True,
                name=f"reader-{document_id}",
            ).start()
    return database.get_document(document_id)
