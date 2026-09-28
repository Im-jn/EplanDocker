"""Tests for upstream/downstream tracing that keeps every hop heading one way."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.document_trace import trace_document
from pdf_parser.parsed_graph import save_parsed_graph
from pdf_parser.parsed_json import build_parsed_json


COMPONENT_HOPS = ("endpoint", "net", "group", "wire")


def _entity(entity_id: int, page: int, **extra) -> dict:
    return {
        "id": entity_id,
        "page": page,
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
        "title": [],
        "descriptions": [],
        **extra,
    }


def _connect(*chain: str) -> list[dict]:
    return [
        {"type": "connection", "source": left, "target": right}
        for left, right in zip(chain, chain[1:])
    ]


def _transfer(source_page: int, source: int, target_page: int, target: int) -> dict:
    return {
        "source_page": source_page,
        "source_component": source,
        "target_page": target_page,
        "target_component": target,
    }


def _pdf_info() -> dict:
    # p3 (upstream):  C1 -- A_out(0)                  A_out -> p1 A_in
    # p1 (middle):    A_in(0) -- B(1) -- C2(2) -- A_out(3),  B -- M(4)   A_out -> p2 A_in
    # p2 (end page):  A_in(0) -- C1(1) -- C2(2)       no output arrows; A_in also links back to p1
    return {
        "document": {"filename": "plan.pdf", "page_count": 3},
        "pages": {
            1: {"diagram": {
                "components": [
                    _entity(0, 1, subclass="arrow", page_io="input"),
                    _entity(1, 1, subclass="symbol", title=["-B"]),
                    _entity(2, 1, subclass="symbol"),
                    _entity(3, 1, subclass="arrow", page_io="output"),
                    _entity(4, 1, subclass="symbol", title=["-M"]),
                ],
                "wires": [_entity(index, 1) for index in range(4)],
                "endpoints": [_entity(index, 1) for index in range(8)],
                "relations": [
                    *_connect("component:0", "endpoint:0", "wire:0", "endpoint:1", "component:1"),
                    *_connect("component:1", "endpoint:2", "wire:1", "endpoint:3", "component:2"),
                    *_connect("component:2", "endpoint:4", "wire:2", "endpoint:5", "component:3"),
                    *_connect("component:1", "endpoint:6", "wire:3", "endpoint:7", "component:4"),
                ],
                "transfers": [_transfer(1, 3, 2, 0)],
            }},
            2: {"diagram": {
                "components": [
                    _entity(0, 2, subclass="arrow", page_io="input"),
                    _entity(1, 2, subclass="symbol"),
                    _entity(2, 2, subclass="symbol"),
                ],
                "wires": [_entity(index, 2) for index in range(2)],
                "endpoints": [_entity(index, 2) for index in range(4)],
                "relations": [
                    *_connect("component:0", "endpoint:0", "wire:0", "endpoint:1", "component:1"),
                    *_connect("component:1", "endpoint:2", "wire:1", "endpoint:3", "component:2"),
                ],
                "transfers": [_transfer(2, 0, 1, 3)],
            }},
            3: {"diagram": {
                "components": [
                    _entity(0, 3, subclass="arrow", page_io="output"),
                    _entity(1, 3, subclass="symbol"),
                ],
                "wires": [_entity(0, 3)],
                "endpoints": [_entity(index, 3) for index in range(2)],
                "relations": _connect("component:1", "endpoint:0", "wire:0", "endpoint:1", "component:0"),
                "transfers": [_transfer(3, 0, 1, 0)],
            }},
        },
    }


def _trace(start: str, direction: str, **kwargs) -> dict:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(build_parsed_json(_pdf_info()), database)
        return trace_document(database, start_id=start, direction=direction, **kwargs)


def _hops(result: dict) -> list[list[str]]:
    return [
        [
            component["id"]
            for page in hop["pages"]
            for component in page["components"]
        ]
        for hop in result["hops"]
    ]


def test_downstream_heads_to_output_arrows_then_spreads_on_the_end_page() -> None:
    result = _trace(
        "plan/p1/component/1", "downstream",
        max_hops=10, skip=COMPONENT_HOPS, response_format="hops",
    )

    assert result["direction"] == "downstream"
    assert _hops(result) == [
        ["plan/p1/component/1"],
        ["plan/p1/component/2"],
        ["plan/p1/component/3"],
        ["plan/p2/component/0"],
        ["plan/p2/component/1"],
        ["plan/p2/component/2"],
    ]


def test_downstream_returns_only_the_edges_it_walked() -> None:
    result = _trace("plan/p1/component/1", "downstream", max_hops=2, skip=COMPONENT_HOPS)

    page = result["result"]["pages"][0]
    assert [component["id"] for component in page["components"]] == [
        "plan/p1/component/1", "plan/p1/component/2", "plan/p1/component/3",
    ]
    assert [wire["id"] for wire in page["wires"]] == ["plan/p1/wire/1", "plan/p1/wire/2"]
    walked = {(relation["source"], relation["target"]) for relation in page["relations"]}
    assert ("plan/p1/component/1", "plan/p1/endpoint/6") not in walked
    assert ("plan/p1/component/0", "plan/p1/endpoint/0") not in walked
    assert ("plan/p1/component/2", "plan/p1/endpoint/4") in walked


def test_upstream_crosses_through_input_arrows() -> None:
    result = _trace(
        "plan/p1/component/2", "upstream",
        max_hops=10, skip=COMPONENT_HOPS, response_format="hops",
    )

    assert _hops(result) == [
        ["plan/p1/component/2"],
        ["plan/p1/component/1"],
        ["plan/p1/component/0"],
        ["plan/p3/component/0"],
        ["plan/p3/component/1"],
    ]
    transfers = [transfer for hop in result["hops"] for page in hop["pages"] for transfer in page["transfers"]]
    assert [transfer["id"] for transfer in transfers] == ["plan/p3/transfer/0"]


def test_opposite_arrows_never_cross_to_another_page() -> None:
    result = _trace("plan/p2/component/1", "downstream", max_hops=10, skip=COMPONENT_HOPS)

    pages = {page["page_number"] for page in result["result"]["pages"]}
    assert pages == {2}
    assert result["reached_hops"] == 1


def test_default_skip_counts_wires_along_the_directed_route() -> None:
    result = _trace("plan/p1/component/1", "downstream", max_hops=2, response_format="hops")

    assert [[wire["id"] for page in hop["pages"] for wire in page["wires"]] for hop in result["hops"]] == [
        [], ["plan/p1/wire/1"], [],
    ]
    assert _hops(result)[2] == ["plan/p1/component/2"]
