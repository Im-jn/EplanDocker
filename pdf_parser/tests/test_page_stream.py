"""Tests for in-page upstream/downstream arrow search."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.page_stream import PageStreamStartError, page_arrow_distances, trace_page_stream
from pdf_parser.parsed_graph import save_parsed_graph
from pdf_parser.parsed_json import build_parsed_json


def _entity(entity_id: int, page: int = 1, **extra) -> dict:
    return {
        "id": entity_id,
        "page": page,
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
        "title": [],
        "descriptions": [],
        **extra,
    }


def _connect(*chain: str) -> list[dict]:
    """Join ``node, endpoint, node, endpoint, ...`` with connection relations."""
    return [
        {"type": "connection", "source": left, "target": right}
        for left, right in zip(chain, chain[1:])
    ]


def _pdf_info() -> dict:
    # Page 1:
    #   A_in(0) -e0- w0 -e1- C1(1) -e2- w1 -e3- C2(2) -e4- w2 -e5- A_out(3)
    #   A_in(0) -e6- C6(6) -e7- A_in2(7)        arrows end paths
    #   X(5) assembly with page_io "output" on e4 is not a target
    #   group 0 frames C1 and A_out2(4)          groups are not traversed
    return {
        "document": {"filename": "plan.pdf", "page_count": 2},
        "pages": {
            1: {"diagram": {
                "components": [
                    _entity(0, subclass="arrow", page_io="input"),
                    _entity(1, subclass="symbol", title=["-K1"]),
                    _entity(2, subclass="box"),
                    _entity(3, subclass="arrow", page_io="output"),
                    _entity(4, subclass="arrow", page_io="output"),
                    _entity(5, subclass="assembly", page_io="output"),
                    _entity(6, subclass="symbol"),
                    _entity(7, subclass="arrow", page_io="input"),
                ],
                "wires": [_entity(index) for index in range(3)],
                "endpoints": [_entity(index) for index in range(8)],
                "groups": [_entity(0)],
                "relations": [
                    *_connect("component:0", "endpoint:0", "wire:0", "endpoint:1", "component:1"),
                    *_connect("component:1", "endpoint:2", "wire:1", "endpoint:3", "component:2"),
                    *_connect("component:2", "endpoint:4", "wire:2", "endpoint:5", "component:3"),
                    *_connect("component:0", "endpoint:6", "component:6", "endpoint:7", "component:7"),
                    {"type": "connection", "source": "component:5", "target": "endpoint:4"},
                    {"type": "contains", "source": "group:0", "target": "component:1"},
                    {"type": "contains", "source": "group:0", "target": "component:4"},
                ],
                "transfers": [{
                    "source_page": 1, "source_component": 3, "target_page": 2, "target_component": 0,
                }],
            }},
            2: {"diagram": {"components": [_entity(0, page=2, subclass="arrow", page_io="input")]}},
        },
    }


def _trace(component_id: str, direction: str) -> dict:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(build_parsed_json(_pdf_info()), database)
        return trace_page_stream(database, component_id, direction)


def test_upstream_reaches_input_arrows_with_shortest_path() -> None:
    result = _trace("plan/p1/component/2", "upstream")

    assert result["file"] == "plan.pdf"
    assert result["page"] == 1
    assert result["arrow_io"] == "input"
    assert [arrow["id"] for arrow in result["arrows"]] == ["plan/p1/component/0"]
    arrow = result["arrows"][0]
    assert arrow["path"] == [
        "plan/p1/component/2", "plan/p1/endpoint/3", "plan/p1/wire/1", "plan/p1/endpoint/2",
        "plan/p1/component/1", "plan/p1/endpoint/1", "plan/p1/wire/0", "plan/p1/endpoint/0",
        "plan/p1/component/0",
    ]
    assert arrow["distance"] == 8
    assert arrow["components"] == ["plan/p1/component/1"]
    assert result["components"] == [{
        "id": "plan/p1/component/1", "subclass": "symbol", "title": ["-K1"], "descriptions": [],
    }]


def test_arrows_end_paths_so_arrows_behind_them_are_unreachable() -> None:
    result = _trace("plan/p1/component/1", "upstream")

    assert [arrow["id"] for arrow in result["arrows"]] == ["plan/p1/component/0"]
    assert result["unreachable_arrows"] == ["plan/p1/component/7"]
    assert result["components"] == []


def test_downstream_ignores_assemblies_and_does_not_cross_groups() -> None:
    result = _trace("plan/p1/component/1", "downstream")

    assert result["arrow_io"] == "output"
    assert [arrow["id"] for arrow in result["arrows"]] == ["plan/p1/component/3"]
    assert result["arrows"][0]["components"] == ["plan/p1/component/2"]
    assert result["unreachable_arrows"] == ["plan/p1/component/4"]


def test_page_arrow_distances_give_page_local_distances_to_each_arrow() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(build_parsed_json(_pdf_info()), database)
        by_name = page_arrow_distances(database, "plan.pdf", 1)
        by_stem = page_arrow_distances(database, "plan", 1)
        try:
            page_arrow_distances(database, "other.pdf", 1)
        except LookupError:
            unknown_rejected = True
        else:
            unknown_rejected = False

    assert by_name == by_stem
    assert by_name["file"] == "plan.pdf"
    assert by_name["arrows"] == {
        "input": ["component:0", "component:7"],
        "output": ["component:3", "component:4"],  # the assembly with page_io is not an arrow
    }
    to_output = by_name["distances"]["component:3"]
    assert to_output["component:3"] == 0
    assert to_output["component:2"] < to_output["component:1"]
    assert "group:0" not in to_output
    assert "component:4" not in to_output  # only reachable through the group
    assert unknown_rejected


def test_start_must_be_an_existing_component() -> None:
    for component_id in ("plan/p1/wire/0", "plan/p1/component/99"):
        try:
            _trace(component_id, "upstream")
        except PageStreamStartError:
            continue
        raise AssertionError(f"{component_id} should be rejected")
    try:
        _trace("plan/p1/component/1", "sideways")
    except ValueError:
        return
    raise AssertionError("unknown direction should be rejected")
