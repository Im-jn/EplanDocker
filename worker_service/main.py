from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from eplan_runtime import INTERNAL_TOKEN, PDF_ROOT, RESULT_ROOT
from pdf_parser.diagram_pdf_parser import parse_diagram_pdf, save_pdf_info


API_URL = os.getenv("EPLAN_API_URL", "http://api:8000").rstrip("/")
POLL_SECONDS = float(os.getenv("EPLAN_WORKER_POLL_SECONDS", "2"))
PAGE_PROGRESS_RE = re.compile(r"Diagram page (\d+)/(\d+)")


class JobCancelled(RuntimeError):
    pass


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{API_URL}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": INTERNAL_TOKEN,
        },
    )
    with urlopen(request, timeout=30) as response:
        if response.status == 204:
            return None
        return json.loads(response.read().decode("utf-8"))


def progress_for_message(message: str) -> tuple[int, str]:
    if "Scanning page information" in message:
        return 5, "classifying"
    match = PAGE_PROGRESS_RE.search(message)
    if match:
        current, total = (int(value) for value in match.groups())
        return 10 + int(80 * max(current - 1, 0) / max(total, 1)), "parsing"
    if "Resolving hyperlink" in message:
        return 92, "linking"
    if "Finished" in message:
        return 98, "saving"
    return 3, "preparing"


def process_job(job: dict[str, Any]) -> None:
    job_id = job["id"]
    document_id = job["document_id"]
    pdf_path = PDF_ROOT / document_id / "source.pdf"
    result_directory = RESULT_ROOT / document_id
    result_path = result_directory / "result.json"
    checkpoint_path = result_directory / "result.json.resume.json"

    def report(message: str) -> None:
        progress, stage = progress_for_message(message)
        response = api_request(
            "POST",
            f"/internal/v1/worker/jobs/{job_id}/progress",
            {"progress": progress, "stage": stage, "message": message},
        )
        if response.get("cancel_requested"):
            raise JobCancelled("Cancellation requested")

    try:
        pages = job.get("pages") or None
        pdf_info = parse_diagram_pdf(
            pdf_path,
            target_pages=pages,
            progress_callback=report,
            checkpoint_file=checkpoint_path,
            resume=True,
            show_page_progress=False,
        )
        saved_path = save_pdf_info(pdf_info, result_path)
        checkpoint_path.unlink(missing_ok=True)
        api_request(
            "POST",
            f"/internal/v1/worker/jobs/{job_id}/complete",
            {"result_path": str(saved_path)},
        )
    except JobCancelled as exc:
        api_request(
            "POST",
            f"/internal/v1/worker/jobs/{job_id}/fail",
            {"cancelled": True, "error": str(exc)},
        )
    except Exception as exc:
        api_request(
            "POST",
            f"/internal/v1/worker/jobs/{job_id}/fail",
            {"cancelled": False, "error": f"{type(exc).__name__}: {exc}"},
        )


def main() -> None:
    print(f"[worker] Polling {API_URL}", flush=True)
    while True:
        try:
            job = api_request("POST", "/internal/v1/worker/jobs/claim", {})
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            print(f"[worker] Claimed {job['id']} ({job['original_filename']})", flush=True)
            process_job(job)
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            print(f"[worker] API unavailable: {exc}", flush=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
