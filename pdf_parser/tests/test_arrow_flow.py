"""Tests for page input/output interpretation of arrow components."""

from __future__ import annotations

from pdf_parser.pages_manager import PathBase
from pdf_parser.processors.arrow_flow import classify_arrow_page_io
from pdf_parser.processors.diagram_serializer import serialize_diagram


def _result(
    direction: str,
    wire_points: list[tuple[float, float]],
    *,
    connected: bool = True,
) -> dict:
    relations = []
    if connected:
        relations = [
            {"type": "connection", "source": "component:3", "target": "endpoint:5"},
            {"type": "connection", "source": "wire:7", "target": "endpoint:5"},
        ]
    return {
        "elements": [{
            "id": 11,
            "type": "arrow",
            "attributes": {"direction": direction},
        }],
        "components": [{"id": 3, "element_ids": [11]}],
        "endpoints": [{"id": 5, "bbox": (0, 0, 0, 0)}],
        "wires": [{
            "id": 7,
            "vectors": [PathBase(type="line", points=wire_points)],
        }],
        "relations": relations,
    }


def test_classifies_arrow_from_local_wire_direction() -> None:
    cases = [
        ("right", (10, 0), "input"),
        ("right", (-10, 0), "output"),
        ("left", (-10, 0), "input"),
        ("left", (10, 0), "output"),
        ("down", (0, 10), "input"),
        ("down", (0, -10), "output"),
        ("up", (0, -10), "input"),
        ("up", (0, 10), "output"),
    ]
    for direction, wire_end, expected in cases:
        result = _result(direction, [(0, 0), wire_end])

        classify_arrow_page_io(result)

        assert result["elements"][0]["attributes"]["page_io"] == expected
        assert result["components"][0]["page_io"] == expected


def test_ignores_vector_storage_order() -> None:
    result = _result("right", [(10, 0), (0, 0)])

    classify_arrow_page_io(result)

    assert result["components"][0]["page_io"] == "input"


def test_uses_first_wire_segment_instead_of_distant_wire_position() -> None:
    result = _result("right", [(0, 0), (10, 0), (-20, 20)])

    classify_arrow_page_io(result)

    assert result["components"][0]["page_io"] == "input"


def test_returns_unknown_without_a_connected_wire() -> None:
    result = _result("right", [(0, 0), (10, 0)], connected=False)

    classify_arrow_page_io(result)

    assert result["components"][0]["page_io"] == "unknown"


def test_returns_unknown_for_conflicting_connected_wires() -> None:
    result = _result("right", [(0, 0), (10, 0)])
    result["wires"].append({
        "id": 8,
        "vectors": [PathBase(type="line", points=[(0, 0), (-10, 0)])],
    })
    result["relations"].append({
        "type": "connection",
        "source": "wire:8",
        "target": "endpoint:5",
    })

    classify_arrow_page_io(result)

    assert result["components"][0]["page_io"] == "unknown"


def test_serializer_exposes_page_io_on_arrow_component() -> None:
    result = _result("right", [(0, 0), (10, 0)])
    result["elements"][0]["shape"] = []
    result["elements"][0]["bbox"] = (0, 0, 1, 1)
    result["elements"][0]["title"] = []
    result["elements"][0]["descriptions"] = []
    result["components"][0].update({
        "type": "component",
        "shape": [],
        "bbox": (0, 0, 1, 1),
        "title": [],
        "descriptions": [],
    })

    classify_arrow_page_io(result)
    diagram = serialize_diagram(result, page=2)

    assert diagram["components"][0]["page_io"] == "input"
    assert diagram["elements"][0]["attributes"]["page_io"] == "input"
