"""Persistence checks for complete diagram PDF extraction results."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pdf_parser.diagram_pdf_parser import (
    _restore_checkpoint,
    _save_checkpoint,
    load_pdf_info,
    save_pdf_info,
)
from pdf_parser.main import main
from pdf_parser.pages_manager import PathBase, TextBase, VectorBase
from shapely.geometry import Polygon


def test_pdf_info_json_round_trip_uses_plain_data() -> None:
    vector = PathBase(type="line", points=[(0, 0), (10, 0)])
    pdf_info = {
        "pages": {
            12: {
                "page_type": "multi",
                "info_table": {"html": "<table></table>"},
                "crosspage_relations": {"hyperlinks": [], "transfers": []},
                "diagram": {
                    "elements": [],
                    "remaining_vectors": [vector],
                    "remaining_text": TextBase([
                        {"text": "N", "location": (0, 0, 2, 2)},
                    ]),
                    "components": [{
                        "attributes": {
                            "polygon": Polygon([(0, 0), (10, 0), (5, 5)]),
                        },
                    }],
                    "relations": [{
                        "type": "wire_connection",
                        "source": "wire:0",
                        "target": "component:0",
                        "endpoint": (0.0, 0.0),
                    }],
                },
            },
        },
    }

    with TemporaryDirectory() as directory:
        output_path = save_pdf_info(pdf_info, Path(directory) / "pdf_info.json")
        loaded = load_pdf_info(output_path)

        page = loaded["pages"]["12"]["diagram"]
        assert page["remaining_vectors"][0]["type"] == "line"
        assert page["remaining_vectors"][0]["points"] == [[0.0, 0.0], [10.0, 0.0]]
        assert page["remaining_text"][0]["text"] == "N"
        assert page["components"][0]["attributes"]["polygon"]["type"] == "Polygon"
        assert page["relations"] == [{
            "type": "wire_connection",
            "source": "wire:0",
            "target": "component:0",
            "endpoint": [0.0, 0.0],
        }]
        assert list(Path(directory).iterdir()) == [output_path]


def test_main_saves_parser_result_to_requested_output() -> None:
    pdf_info = {"pages": {12: {
        "diagram": {"components": [], "wires": []},
        "crosspage_relations": {"hyperlinks": [], "transfers": []},
    }}}

    with TemporaryDirectory() as directory:
        output_path = Path(directory) / "main_result.json"
        with (
            patch.object(
                sys,
                "argv",
                ["pdf_parser.main", "--output-file", str(output_path)],
            ),
            patch("pdf_parser.main.parse_diagram_pdf", return_value=pdf_info),
        ):
            main()

        assert load_pdf_info(output_path) == {
            "pages": {"12": {
                "diagram": {"components": [], "wires": []},
                "crosspage_relations": {"hyperlinks": [], "transfers": []},
            }},
        }


def test_checkpoint_restores_only_completed_page_results() -> None:
    pdf_info = {
        "pages": {
            1: {"diagram": {}, "crosspage_relations": {"hyperlinks": [], "transfers": []}},
            2: {
                "diagram": {"components": [{"id": "K1"}]},
                "crosspage_relations": {"hyperlinks": [], "transfers": []},
            },
        },
    }

    with TemporaryDirectory() as directory:
        checkpoint_path = Path(directory) / "result.resume.json"
        _save_checkpoint(
            checkpoint_path,
            document_id="document-sha",
            page_count=2,
            completed_pages={2},
            pdf_info=pdf_info,
        )
        fresh_pdf_info = {
            "pages": {
                1: {"diagram": {}, "crosspage_relations": {"hyperlinks": [], "transfers": []}},
                2: {"diagram": {}, "crosspage_relations": {"hyperlinks": [], "transfers": []}},
            },
        }

        restored = _restore_checkpoint(
            checkpoint_path,
            document_id="document-sha",
            page_count=2,
            pdf_info=fresh_pdf_info,
        )

        assert restored == {2}
        assert fresh_pdf_info["pages"][2]["diagram"] == {
            "components": [{"id": "K1"}],
        }
        assert fresh_pdf_info["pages"][2]["crosspage_relations"] == {
            "hyperlinks": [],
            "transfers": [],
        }
        assert fresh_pdf_info["pages"][1]["diagram"] == {}
