"""Tests for the simplified parsed JSON exposed to other work packages."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.diagram_pdf_parser import load_pdf_info
from pdf_parser.parsed_json import build_parsed_json, save_parsed_json


def _pdf_info() -> dict:
    bbox = {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}
    return {
        "schema_version": "1.0",
        "document": {"filename": "source.pdf", "page_count": 1, "target_pages": None},
        "symbol_overview": {
            "symbols": [[{"type": "line"}]],
            "records": [{"symbol": 0, "type": "A", "descriptions": "PLC box"}],
        },
        "pages": {
            1: {
                "page_type": "multi",
                "info_table": {
                    "html": "<table></table>",
                    "cells": [{
                        "bbox": (0.0, 1.0, 2.0, 3.0),
                        "text": "SYMBOL",
                        "texts": [{"text": "SYMBOL"}],
                        "source": {"category": "cell"},
                        "row": 0,
                        "col": 1,
                        "rowspan": 1,
                        "colspan": 2,
                        "subcell_boundaries": [],
                        "subcells": [],
                    }],
                    "rows": [{"index": 0}],
                    "columns": [{"index": 0}],
                    "raw_cells": [{"category": "cell"}],
                },
                "diagram": {
                    "elements": [{"id": 0}],
                    "components": [{"id": 3, "type": "component", "bbox": bbox, "elements": [0]}],
                    "wires": [{"id": 4, "type": "wire", "bbox": bbox, "elements": [1]}],
                    "relations": [{"type": "connection", "source": "wire:4", "target": "endpoint:0"}],
                    "remaining_vectors": [{"type": "line"}],
                    "remaining_text": [{"text": "note", "text_indices": [7], "nearby": [3]}],
                    "hyperlinks": [{"source_page": 1, "source_component": 3}],
                    "transfers": [],
                },
            },
        },
    }


def test_drops_elements_references_and_table_internals() -> None:
    page = build_parsed_json(_pdf_info())["pages"][1]

    assert page["info_table"] == {
        "html": "<table></table>",
        "cells": [{
            "bbox": [0.0, 1.0, 2.0, 3.0],
            "text": "SYMBOL",
            "row": 0,
            "col": 1,
            "rowspan": 1,
            "colspan": 2,
        }],
    }
    diagram = page["diagram"]
    assert "elements" not in diagram
    assert "remaining_vectors" not in diagram
    assert diagram["components"] == [{
        "id": "source/p1/component/3",
        "type": "component",
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
    }]
    assert "elements" not in diagram["wires"][0]
    assert diagram["remaining_text"] == [{
        "id": "source/p1/text/0",
        "text": "note",
        "nearby": ["source/p1/component/3"],
    }]


def test_replaces_local_ids_and_references_with_global_ids() -> None:
    diagram = build_parsed_json(_pdf_info(), filename="Plan A.pdf")["pages"][1]["diagram"]

    assert diagram["wires"][0]["id"] == "Plan A/p1/wire/4"
    assert diagram["relations"] == [{
        "id": "Plan A/p1/relation/0",
        "type": "connection",
        "source": "Plan A/p1/wire/4",
        "target": "Plan A/p1/endpoint/0",
    }]
    assert diagram["hyperlinks"] == [{
        "id": "Plan A/p1/hyperlink/0",
        "source_page": 1,
        "source_component": "Plan A/p1/component/3",
        "target_component": None,
    }]


def test_keeps_only_symbol_records() -> None:
    pdf_info = _pdf_info()
    pdf_info["pages"][2] = {
        "page_type": "symbol_overview",
        "diagram": {"symbols": [[{"type": "line"}]], "records": [{"symbol": 0}]},
    }
    parsed = build_parsed_json(pdf_info)

    assert parsed["symbol_overview"] == {
        "records": [{"symbol": 0, "type": "A", "descriptions": "PLC box"}],
    }
    assert parsed["pages"][2]["diagram"] == {"records": [{"symbol": 0}]}


def test_does_not_mutate_full_parser_output() -> None:
    pdf_info = _pdf_info()
    build_parsed_json(pdf_info, filename="plan.pdf")

    assert "elements" in pdf_info["pages"][1]["diagram"]
    assert "elements" in pdf_info["pages"][1]["diagram"]["components"][0]
    assert "raw_cells" in pdf_info["pages"][1]["info_table"]
    assert "symbols" in pdf_info["symbol_overview"]
    assert "text_indices" in pdf_info["pages"][1]["diagram"]["remaining_text"][0]
    assert pdf_info["document"]["filename"] == "source.pdf"


def test_saves_under_original_pdf_name() -> None:
    with TemporaryDirectory() as directory:
        parsed = build_parsed_json(_pdf_info(), filename="Plan A.pdf")
        output_path = save_parsed_json(parsed, Path(directory))

        assert output_path == Path(directory).resolve() / "Plan A.json"
        saved = load_pdf_info(output_path)
        assert saved["document"]["filename"] == "Plan A.pdf"
        assert set(saved["pages"]) == {"1"}
