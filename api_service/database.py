from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eplan_runtime import DATABASE_PATH, ensure_storage


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT,
    document_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    pages_json TEXT NOT NULL DEFAULT '[]',
    strict_pages INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'queued',
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    result_path TEXT,
    reader_status TEXT NOT NULL DEFAULT 'pending',
    reader_error TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at
ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_reader_status_updated_at
ON jobs(reader_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_batch_id
ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_document_id
ON jobs(document_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    ensure_storage()
    connection = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def initialize() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA optimize")


def serialize_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["pages"] = json.loads(value.pop("pages_json"))
    value["strict_pages"] = bool(value["strict_pages"])
    value["result_available"] = value["status"] == "succeeded" and bool(value["result_path"])
    value["document_ready"] = value["status"] == "succeeded" and value["reader_status"] == "ready"
    return value


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return serialize_job(row)


def get_job_by_idempotency_key(key: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return serialize_job(row)


def create_job(job: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                id, batch_id, document_id, original_filename, source_sha256,
                source_size, page_count, pages_json,
                strict_pages, status, progress, stage, message,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 'queued', 'Waiting for a worker', ?, ?, ?)
            """,
            (
                job["id"], job.get("batch_id"), job["document_id"],
                job["original_filename"], job["source_sha256"], job["source_size"],
                job["page_count"], json.dumps(job.get("pages", [])),
                int(bool(job.get("strict_pages"))), job.get("idempotency_key"), now, now,
            ),
        )
    return get_job(job["id"]) or job


def list_jobs(*, batch_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM jobs"
    params: list[Any] = []
    if batch_id:
        sql += " WHERE batch_id = ?"
        params.append(batch_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with connect() as connection:
        rows = connection.execute(sql, params).fetchall()
    return [serialize_job(row) for row in rows if row is not None]


def claim_job() -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        connection.execute(
            """
            UPDATE jobs SET status = 'running', progress = 1, stage = 'starting',
                message = 'Worker claimed task', started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, row["id"]),
        )
        claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        connection.execute("COMMIT")
    return serialize_job(claimed)


def update_progress(job_id: str, progress: int, stage: str, message: str) -> dict[str, Any] | None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET progress = ?, stage = ?, message = ?, updated_at = ?
            WHERE id = ? AND status IN ('running', 'cancel_requested')
            """,
            (max(0, min(int(progress), 99)), stage, message, utc_now(), job_id),
        )
    return get_job(job_id)


def request_cancel(job_id: str) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancel_requested' END,
                stage = 'cancelling', message = 'Cancellation requested', updated_at = ?,
                finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END
            WHERE id = ? AND status IN ('queued', 'running')
            """,
            (now, now, job_id),
        )
    return get_job(job_id)


def finish_job(job_id: str, *, status: str, result_path: str | None = None, error: str | None = None) -> None:
    now = utc_now()
    progress = 100 if status == "succeeded" else 0
    stage = "parsed" if status == "succeeded" else status
    message = "Parsing result is ready" if status == "succeeded" else (error or status)
    reader_status = "pending" if status == "succeeded" else "skipped"
    with connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET status = ?, progress = ?, stage = ?, message = ?, error = ?,
                result_path = ?, reader_status = ?, updated_at = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, progress, stage, message, error, result_path, reader_status, now, now, job_id),
        )


def claim_publish_job() -> dict[str, Any] | None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id FROM jobs
            WHERE status = 'succeeded' AND reader_status = 'pending'
            ORDER BY finished_at LIMIT 1
            """
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        connection.execute(
            "UPDATE jobs SET reader_status = 'building', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )
        claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        connection.execute("COMMIT")
    return serialize_job(claimed)


def finish_publish(job_id: str, *, ready: bool, error: str | None = None) -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET reader_status = ?, reader_error = ?, updated_at = ? WHERE id = ?
            """,
            ("ready" if ready else "failed", error, utc_now(), job_id),
        )


def get_document(document_id: str, *, ready_only: bool = False) -> dict[str, Any] | None:
    sql = "SELECT * FROM jobs WHERE document_id = ? AND status = 'succeeded'"
    if ready_only:
        sql += " AND reader_status = 'ready'"
    sql += " ORDER BY finished_at DESC LIMIT 1"
    with connect() as connection:
        row = connection.execute(sql, (document_id,)).fetchone()
    return serialize_job(row)


def list_ready_documents(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'succeeded' AND reader_status = 'ready'
            ORDER BY finished_at DESC LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [serialize_job(row) for row in rows if row is not None]
