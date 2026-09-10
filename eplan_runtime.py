"""Shared runtime paths and deployment settings."""

from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
STORAGE_ROOT = Path(os.getenv("EPLAN_STORAGE_ROOT", REPO_ROOT / "storage")).resolve()
PDF_ROOT = STORAGE_ROOT / "data" / "eplan_pdf"
RESULT_ROOT = STORAGE_ROOT / "output" / "pdf_parsing_result"
READER_ROOT = STORAGE_ROOT / "output" / "reader_data"
STATE_ROOT = STORAGE_ROOT / "state"
TEMP_ROOT = STORAGE_ROOT / "tmp"
DATABASE_PATH = Path(os.getenv("EPLAN_DATABASE_PATH", STATE_ROOT / "jobs.sqlite3")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("EPLAN_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
INTERNAL_TOKEN = os.getenv("EPLAN_INTERNAL_TOKEN", "development-worker-token")


def ensure_storage() -> None:
    for directory in (PDF_ROOT, RESULT_ROOT, READER_ROOT, STATE_ROOT, TEMP_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
