"""Import an existing PDF and parsing result without parsing the PDF again."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import fitz

from api_service import database
from eplan_runtime import PDF_ROOT, RESULT_ROOT


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_completed_document(pdf_path: Path, result_path: Path) -> dict[str, object]:
    pdf_path = pdf_path.resolve()
    result_path = result_path.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if not result_path.is_file():
        raise FileNotFoundError(f"Parsing result not found: {result_path}")
    if pdf_path.suffix.lower() != ".pdf" or result_path.suffix.lower() != ".json":
        raise ValueError("Expected one .pdf file and one .json parsing result")

    source_sha256 = sha256_file(pdf_path)
    document_id = f"doc_{source_sha256[:24]}"
    job_id = f"import_{source_sha256[:24]}"
    original_filename = pdf_path.name
    source_size = pdf_path.stat().st_size
    with fitz.open(pdf_path) as document:
        page_count = document.page_count

    target_pdf = PDF_ROOT / document_id / "source.pdf"
    target_result = RESULT_ROOT / document_id / "result.json"
    target_pdf.parent.mkdir(parents=True, exist_ok=True)
    target_result.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path != target_pdf:
        if target_pdf.exists():
            raise FileExistsError(f"Target PDF already exists: {target_pdf}")
        pdf_path.replace(target_pdf)
    if result_path != target_result:
        if target_result.exists():
            raise FileExistsError(f"Target result already exists: {target_result}")
        result_path.replace(target_result)

    database.initialize()
    job = database.get_job(job_id)
    if job is None:
        database.create_job(
            {
                "id": job_id,
                "document_id": document_id,
                "original_filename": original_filename,
                "source_sha256": source_sha256,
                "source_size": source_size,
                "page_count": page_count,
                "pages": [],
                "strict_pages": False,
            }
        )
        database.finish_job(job_id, status="succeeded", result_path=str(target_result))
    return database.get_job(job_id) or {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register an existing PDF/result pair as already parsed."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    job = import_completed_document(args.pdf, args.result)
    print(
        f"Imported {job['original_filename']} as {job['document_id']} "
        f"({job['status']}, reader={job['reader_status']})"
    )


if __name__ == "__main__":
    main()
