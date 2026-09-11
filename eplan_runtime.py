"""Shared runtime paths and deployment settings."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
STORAGE_ROOT = Path(os.getenv("EPLAN_STORAGE_ROOT", REPO_ROOT / "storage")).resolve()
PDF_ROOT = STORAGE_ROOT / "data" / "eplan_pdf"
RESULT_ROOT = STORAGE_ROOT / "output" / "pdf_parsing_result"
READER_ROOT = STORAGE_ROOT / "output" / "reader_data"
STATE_ROOT = STORAGE_ROOT / "state"
TEMP_ROOT = STORAGE_ROOT / "tmp"
LOG_ROOT = STORAGE_ROOT / "logs"
DATABASE_PATH = Path(os.getenv("EPLAN_DATABASE_PATH", STATE_ROOT / "jobs.sqlite3")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("EPLAN_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))


def _load_or_create_internal_token() -> str:
    """Return the optional override or a shared token persisted in storage."""
    configured = os.getenv("EPLAN_INTERNAL_TOKEN", "").strip()
    if configured:
        return configured

    token_path = Path(
        os.getenv("EPLAN_INTERNAL_TOKEN_FILE", STATE_ROOT / ".internal-worker-token")
    ).resolve()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if token:
        return token

    generated = secrets.token_urlsafe(48)
    try:
        with token_path.open("x", encoding="utf-8") as token_file:
            token_file.write(generated)
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        return generated
    except FileExistsError:
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"Internal worker token file is empty: {token_path}")
        return token


INTERNAL_TOKEN = _load_or_create_internal_token()


def ensure_storage() -> None:
    for directory in (PDF_ROOT, RESULT_ROOT, READER_ROOT, STATE_ROOT, TEMP_ROOT, LOG_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
