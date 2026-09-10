from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.vector_api import (
    _document_extract_info,
    _document_symbol_result,
    _document_trace_index,
)


def _diagram() -> dict[str, list[object]]:
    return {
        "elements": [],
        "components": [],
        "endpoints": [],
        "wires": [],
        "nets": [],
        "groups": [],
        "relations": [],
        "remaining_vectors": [],
        "remaining_text": [],
    }


def test_document_result_uses_pdf_stem_and_returns_requested_diagram_page() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        pdf_path = root / "plant.v2.pdf"
        pdf_path.write_bytes(b"%PDF")
        result_root = root / "results"
        result_root.mkdir()
        (result_root / "plant.v2.json").write_text(
            json.dumps({
                "pages": {
                    "2": {
                        "page_type": "multi",
                        "diagram": _diagram(),
                        "info_table": {"html": "page 2"},
                        "crosspage_relations": {"hyperlinks": [], "transfers": []},
                    },
                },
            }),
            encoding="utf-8",
        )

        result, filename = _document_extract_info(
            pdf_path,
            2,
            result_root=result_root,
        )

        assert filename == "plant.v2.json"
        assert result is not None
        assert result["category"] == "extract_info"
        assert result["page_number"] == 2
        assert result["diagram"] == _diagram()


def test_document_result_skips_missing_and_non_diagram_pages() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        pdf_path = root / "example.pdf"
        result_root = root / "results"
        result_root.mkdir()
        (result_root / "example.json").write_text(
            json.dumps({"pages": {"1": {"diagram": {}}}}),
            encoding="utf-8",
        )

        missing, filename = _document_extract_info(
            pdf_path,
            2,
            result_root=result_root,
        )
        non_diagram, _ = _document_extract_info(
            pdf_path,
            1,
            result_root=result_root,
        )

        assert filename == "example.json"
        assert missing is None
        assert non_diagram is None


def test_document_result_supports_document_scoped_api_layout() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        pdf_path = root / "data" / "doc_123" / "source.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"%PDF")
        result_root = root / "results"
        result_path = result_root / "doc_123" / "result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps({
                "pages": {
                    "1": {
                        "page_type": "multi",
                        "diagram": _diagram(),
                        "info_table": {},
                        "crosspage_relations": {"hyperlinks": [], "transfers": []},
                    },
                },
            }),
            encoding="utf-8",
        )

        result, filename = _document_extract_info(pdf_path, 1, result_root=result_root)

        assert filename == "result.json"
        assert result is not None
        assert result["page_number"] == 1


def test_document_symbol_result_projects_parser_output_without_reextracting() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        pdf_path = root / "data" / "doc_123" / "source.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"%PDF")
        result_root = root / "results"
        result_path = result_root / "doc_123" / "result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps({
                "pages": {
                    "5": {"page_type": "symbol_overview"},
                    "6": {"page_type": "symbol_overview"},
                    "7": {"page_type": "multi"},
                },
                "symbol_overview": {
                    "symbols": [[{
                        "type": "line",
                        "points": [[10, 20], [14, 25]],
                        "path_meta": {"dashes": "[3 2] 0"},
                    }]],
                    "records": [{
                        "symbol": 0,
                        "type": "contactor",
                        "descriptions": ["K1", "main contactor"],
                    }],
                },
            }),
            encoding="utf-8",
        )

        result, filename = _document_symbol_result(pdf_path, result_root=result_root)

        assert filename == "result.json"
        assert result is not None
        assert result["pages"] == [5, 6]
        assert result["records"] == [{
            "symbol": 0,
            "name": "contactor",
            "description": "K1; main contactor",
        }]
        assert result["symbols"] == [{
            "shapes": [{
                "type": "line",
                "points": [[0.0, 0.0], [4.0, 5.0]],
                "dashed": True,
            }],
            "width": 4.0,
            "height": 5.0,
        }]


def test_document_trace_index_keeps_one_transfer_record_for_bidirectional_client_use() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        pdf_path = root / "example.pdf"
        result_root = root / "results"
        result_root.mkdir()
        page_1_diagram = _diagram()
        page_1_diagram["components"] = [{"id": 4, "type": "component", "page": 1}]
        page_1_diagram["wires"] = [{"id": 2, "type": "wire", "page": 1}]
        page_1_diagram["nets"] = [{"id": 7, "type": "net", "page": 1}]
        page_1_diagram["relations"] = [
            {"type": "contains", "source": "net:7", "target": "wire:2"},
            {"type": "contains", "source": "group:3", "target": "component:4"},
        ]
        page_2_diagram = _diagram()
        page_2_diagram["components"] = [{"id": 9, "type": "component", "page": 2}]
        transfer = {
            "source_page": 1,
            "source_component": 4,
            "target_page": 2,
            "target_component": 9,
        }
        (result_root / "example.json").write_text(
            json.dumps({
                "pages": {
                    "1": {
                        "diagram": page_1_diagram,
                        "crosspage_relations": {"hyperlinks": [], "transfers": [transfer]},
                    },
                    "2": {
                        "diagram": page_2_diagram,
                        "crosspage_relations": {"hyperlinks": [], "transfers": []},
                    },
                },
            }),
            encoding="utf-8",
        )

        result, filename = _document_trace_index(pdf_path, result_root=result_root)

        assert filename == "example.json"
        assert result is not None
        assert result["transfers"] == [transfer]
        assert [page["page_number"] for page in result["pages"]] == [1, 2]
        assert result["pages"][0]["nets"] == [{"id": 7, "type": "net", "page": 1}]
        assert result["pages"][0]["relations"] == [
            {"type": "contains", "source": "net:7", "target": "wire:2"},
        ]
