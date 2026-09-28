"""Write parsed JSON and graph rows for documents parsed before they existed."""

from __future__ import annotations

import argparse

from api_service import database
from eplan_runtime import (
    PARSED_GRAPH_PATH,
    PARSED_JSON_ROOT,
    RESULT_ROOT,
)
from pdf_parser.diagram_pdf_parser import load_pdf_info
from pdf_parser.parsed_graph import export_parsed_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "document_ids",
        nargs="*",
        help="Documents to export; defaults to every completed document.",
    )
    args = parser.parse_args()

    database.initialize()
    # Oldest first, so the newest document wins when two share a filename.
    jobs = list(reversed(database.list_completed_documents(limit=500)))
    if args.document_ids:
        jobs = [job for job in jobs if job["document_id"] in set(args.document_ids)]
    for job in jobs:
        result_path = RESULT_ROOT / job["document_id"] / "result.json"
        if not result_path.is_file():
            print(f"Skipped {job['document_id']}: {result_path} is missing")
            continue
        json_path, counts = export_parsed_outputs(
            load_pdf_info(result_path),
            filename=job["original_filename"],
            parsed_json_directory=PARSED_JSON_ROOT,
            graph_path=PARSED_GRAPH_PATH,
            document_id=job["document_id"],
        )
        print(
            f"Exported {job['original_filename']} ({job['document_id']}): "
            f"{json_path.name}, {counts['nodes']} nodes, {counts['edges']} edges"
        )


if __name__ == "__main__":
    main()
