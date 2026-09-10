from __future__ import annotations

import json
import shutil
import threading

from api_service import database
from eplan_runtime import PDF_ROOT, READER_ROOT, TEMP_ROOT
from scripts.build_pdf_reader_data import (
    READER_SCHEMA_VERSION,
    build_document_data,
)


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
    try:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        build_document_data(
            pdf_path,
            temporary_root,
            progress_callback=lambda current, total: database.update_publish_progress(
                job_id, current, total
            ),
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
    except Exception as exc:
        database.finish_publish(job_id, ready=False, error=str(exc))


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
