from __future__ import annotations

import shutil
import threading
from pathlib import Path

from api_service import database
from eplan_runtime import PDF_ROOT, READER_ROOT, TEMP_ROOT
from scripts.build_pdf_reader_data import build_document_data


def publish_once() -> bool:
    job = database.claim_publish_job()
    if job is None:
        return False
    document_id = job["document_id"]
    pdf_path = PDF_ROOT / document_id / "source.pdf"
    temporary_root = TEMP_ROOT / f"reader-{document_id}"
    final_root = READER_ROOT / document_id
    try:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        build_document_data(
            pdf_path,
            temporary_root,
            document_id=document_id,
            title=job["original_filename"],
            copy_pdf=False,
            api_base="/api/v1",
        )
        built_root = temporary_root / "documents" / document_id
        if final_root.exists():
            shutil.rmtree(final_root)
        final_root.parent.mkdir(parents=True, exist_ok=True)
        built_root.replace(final_root)
        shutil.rmtree(temporary_root, ignore_errors=True)
        database.finish_publish(job["id"], ready=True)
    except Exception as exc:
        database.finish_publish(job["id"], ready=False, error=str(exc))
    return True


def publisher_loop(stop_event: threading.Event, interval_seconds: float = 2.0) -> None:
    while not stop_event.is_set():
        if not publish_once():
            stop_event.wait(interval_seconds)
