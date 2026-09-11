"""Focused regression checks for independent job and cached-PDF deletion."""

from pathlib import Path
from tempfile import TemporaryDirectory

from api_service import database
from api_service import main as api


def _job(job_id: str, document_id: str) -> dict[str, object]:
    return {
        "id": job_id,
        "batch_id": None,
        "document_id": document_id,
        "original_filename": "sample.pdf",
        "source_sha256": "abc",
        "source_size": 10,
        "page_count": 2,
        "pages": [1, 2],
        "strict_pages": False,
        "idempotency_key": None,
    }


def run() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        database.DATABASE_PATH = root / "jobs.sqlite3"
        api.PDF_ROOT = root / "pdfs"
        api.RESULT_ROOT = root / "results"
        api.READER_ROOT = root / "reader"
        for path in (api.PDF_ROOT, api.RESULT_ROOT, api.READER_ROOT):
            path.mkdir(parents=True)
        database.initialize()

        database.create_job(_job("done", "doc_done"))
        database.finish_job("done", status="succeeded", result_path="/result.json")
        assert database.request_incomplete_document_delete("doc_done") == (False, False)
        assert database.get_job("done")["status"] == "succeeded"
        assert database.request_job_delete("done") == (True, False)
        assert database.get_job("done") is None

        database.create_job(_job("queued", "doc_queued"))
        assert database.request_incomplete_document_delete("doc_queued") == (True, False)
        assert database.get_job("queued") is None

        database.create_job(_job("running", "doc_running"))
        with database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'running' WHERE id = 'running'"
            )
        assert database.request_incomplete_document_delete("doc_running") == (True, True)
        running = database.get_job("running")
        assert running is not None
        assert running["status"] == "delete_requested"
        assert running["delete_pdf"] is True

        database.create_job(_job("failed-resume", "doc_failed_resume"))
        with database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'running' WHERE id = 'failed-resume'"
            )
        database.update_progress("failed-resume", 47, "parsing", "Page checkpointed")
        database.finish_job("failed-resume", status="failed", error="Temporary failure")
        failed = database.get_job("failed-resume")
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["progress"] == 47
        resumed = database.resume_job("failed-resume")
        assert resumed is not None
        assert resumed["status"] == "queued"
        assert resumed["progress"] == 47
        assert resumed["error"] is None
        claimed = database.claim_job()
        assert claimed is not None
        assert claimed["id"] == "failed-resume"
        assert claimed["progress"] == 47

        database.create_job(_job("queue-delete", "doc_queue_delete"))
        database.finish_job(
            "queue-delete", status="succeeded", result_path="/result.json"
        )
        queue_pdf = api.PDF_ROOT / "doc_queue_delete" / "source.pdf"
        queue_result = api.RESULT_ROOT / "doc_queue_delete" / "result.json"
        queue_pdf.parent.mkdir()
        queue_result.parent.mkdir()
        queue_pdf.write_bytes(b"%PDF-test")
        queue_result.write_text("{}", encoding="utf-8")
        api.delete_parsing_job("queue-delete")
        assert database.get_job("queue-delete") is None
        assert queue_pdf.is_file()
        assert not queue_result.exists()
        cached = list(api._cached_pdf_index().values())
        assert cached[0]["filename"] == "sample.pdf"

        database.create_job(_job("pdf-delete", "doc_pdf_delete"))
        database.finish_job(
            "pdf-delete", status="succeeded", result_path="/result.json"
        )
        cached_pdf = api.PDF_ROOT / "doc_pdf_delete" / "source.pdf"
        cached_result = api.RESULT_ROOT / "doc_pdf_delete" / "result.json"
        cached_pdf.parent.mkdir()
        cached_result.parent.mkdir()
        cached_pdf.write_bytes(b"%PDF-test")
        cached_result.write_text("{}", encoding="utf-8")
        cache_id = next(
            entry["cache_id"]
            for entry in api._cached_pdf_index().values()
            if entry["document_id"] == "doc_pdf_delete"
        )
        api.delete_cached_pdf(str(cache_id))
        assert not cached_pdf.exists()
        assert database.get_job("pdf-delete")["status"] == "succeeded"
        assert cached_result.is_file()


if __name__ == "__main__":
    run()
    print("delete lifecycle tests passed")
