"""Tests for document-level tracing over the parsed graph database."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.document_trace import (
    TraceDocumentNotFoundError,
    TraceStartNotFoundError,
    trace_document,
)
from pdf_parser.parsed_graph import save_parsed_graph
from pdf_parser.parsed_json import build_parsed_json


def _entity(entity_id: int, entity_type: str, page: int, **extra) -> dict:
    return {
        "id": entity_id,
        "type": entity_type,
        "page": page,
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
        "title": [],
        "descriptions": [],
        "elements": [entity_id + 100],
        **extra,
    }


def _pdf_info(extra_relations: list[dict] | None = None) -> dict:
    # Page 1: C1 -e10- W2 ~net7~ W3 -e11- C4, and group G5 holds C1 and C6.
    # C4 transfers to C9 on page 2.
    return {
        "document": {"filename": "source.pdf", "page_count": 2},
        "pages": {
            1: {"diagram": {
                "components": [
                    _entity(1, "component", 1, page_io="input"),
                    _entity(4, "component", 1),
                    _entity(6, "component", 1),
                ],
                "wires": [_entity(2, "wire", 1), _entity(3, "wire", 1)],
                "endpoints": [_entity(10, "endpoint", 1), _entity(11, "endpoint", 1)],
                "nets": [_entity(7, "net", 1)],
                "groups": [_entity(5, "group", 1)],
                "relations": [
                    {"type": "connection", "source": "component:1", "target": "endpoint:10"},
                    {"type": "connection", "source": "wire:2", "target": "endpoint:10"},
                    {"type": "connection", "source": "wire:3", "target": "endpoint:11"},
                    {"type": "connection", "source": "component:4", "target": "endpoint:11"},
                    {"type": "contains", "source": "net:7", "target": "wire:2"},
                    {"type": "contains", "source": "net:7", "target": "wire:3"},
                    {"type": "contains", "source": "group:5", "target": "component:1"},
                    {"type": "contains", "source": "group:5", "target": "component:6"},
                    *(extra_relations or []),
                ],
                "hyperlinks": [],
                "transfers": [{
                    "source_page": 1,
                    "source_component": 4,
                    "target_page": 2,
                    "target_component": 9,
                    "source_bbox": {"x0": 1, "y0": 1, "x1": 2, "y1": 2},
                    "target_bbox": None,
                }],
            }},
            2: {"diagram": {"components": [_entity(9, "component", 2)]}},
        },
    }


def _trace(directory: str, pdf_info: dict | None = None, **kwargs) -> dict:
    database = Path(directory) / "graph.sqlite3"
    parsed = build_parsed_json(pdf_info or _pdf_info(), filename="plan.pdf")
    save_parsed_graph(parsed, database, document_id="doc_internal")
    kwargs.setdefault("start_id", "plan/p1/component/1")
    return trace_document(database, **kwargs)


def _ids(pages: list[dict], collection: str) -> list[str]:
    return [entity["id"] for page in pages for entity in page[collection]]


def test_default_skip_counts_only_components_and_wires() -> None:
    with TemporaryDirectory() as directory:
        result = _trace(directory, max_hops=4, response_format="hops")

    assert result["skip"] == ["endpoint", "net"]
    assert result["reached_hops"] == 4
    hops = [hop["pages"] for hop in result["hops"]]
    assert _ids(hops[0], "components") == ["plan/p1/component/1"]
    assert _ids(hops[0], "endpoints") == ["plan/p1/endpoint/10"]
    assert all("groups" not in page for pages in hops for page in pages)
    assert _ids(hops[1], "components") == []
    assert _ids(hops[1], "wires") == ["plan/p1/wire/2"]
    assert _ids(hops[1], "nets") == ["plan/p1/net/7"]
    assert _ids(hops[2], "wires") == ["plan/p1/wire/3"]
    assert _ids(hops[3], "components") == ["plan/p1/component/4"]
    assert _ids(hops[4], "components") == ["plan/p2/component/9"]
    assert _ids(hops[4], "transfers") == ["plan/p1/transfer/0"]
    assert hops[4][0]["transfers"][0]["target_component"] == "plan/p2/component/9"
    # C6 shares only a group with the start, and groups are never traversed.
    assert "plan/p1/component/6" not in {
        component for pages in hops for component in _ids(pages, "components")
    }


def test_hop_stops_at_counted_nodes() -> None:
    with TemporaryDirectory() as directory:
        result = _trace(directory, max_hops=1)

    pages = result["result"]["pages"]
    assert _ids(pages, "components") == ["plan/p1/component/1"]
    assert _ids(pages, "wires") == ["plan/p1/wire/2"]
    assert _ids(pages, "nets") == []
    assert pages[0]["components"][0]["page_io"] == "input"
    assert pages[0]["components"][0]["hop"] == 0
    assert "elements" not in pages[0]["components"][0]
    assert {relation["id"] for relation in pages[0]["relations"]} == {
        "plan/p1/relation/0",
        "plan/p1/relation/1",
    }


def test_open_endpoints_mark_where_the_trace_stopped() -> None:
    with TemporaryDirectory() as directory:
        result = _trace(directory, max_hops=2)

    assert result["open_endpoints"] == [{
        "id": "plan/p1/endpoint/11",
        "page": 1,
        "sources": ["plan/p1/wire/3"],
        "candidates": ["plan/p1/component/4"],
    }]


def test_skipping_wires_traces_component_to_component() -> None:
    with TemporaryDirectory() as directory:
        result = _trace(
            directory,
            max_hops=1,
            skip=("endpoint", "net", "wire"),
            response_format="hops",
        )

    assert _ids(result["hops"][1]["pages"], "components") == ["plan/p1/component/4"]


def test_loops_reach_each_node_once() -> None:
    loop = [{"type": "connection", "source": "wire:3", "target": "endpoint:10"}]
    with TemporaryDirectory() as directory:
        result = _trace(directory, _pdf_info(loop), max_hops=10, response_format="hops")

    seen = [
        entity["id"]
        for hop in result["hops"]
        for page in hop["pages"]
        for collection in ("components", "wires", "endpoints", "nets")
        for entity in page[collection]
    ]
    assert len(seen) == len(set(seen))
    assert _ids(result["hops"][1]["pages"], "wires") == ["plan/p1/wire/2", "plan/p1/wire/3"]


def test_parallel_transfers_collapse_per_direction() -> None:
    pdf_info = _pdf_info()
    transfer = pdf_info["pages"][1]["diagram"]["transfers"][0]
    pdf_info["pages"][1]["diagram"]["transfers"].append(dict(transfer))
    pdf_info["pages"][2]["diagram"]["transfers"] = [{
        "source_page": 2, "source_component": 9, "target_page": 1, "target_component": 4,
    }]
    with TemporaryDirectory() as directory:
        result = _trace(directory, pdf_info, max_hops=4)

    assert _ids(result["result"]["pages"], "transfers") == [
        "plan/p1/transfer/0",
        "plan/p2/transfer/0",
    ]


def test_start_accepts_filename_page_and_local_id() -> None:
    for file in ("plan.pdf", "plan"):
        with TemporaryDirectory() as directory:
            result = _trace(
                directory,
                start_id=None,
                file=file,
                start_page=1,
                start_kind="wire",
                start_local_id=2,
                max_hops=0,
            )

        assert result["file"] == "plan.pdf"
        assert result["start"] == {"id": "plan/p1/wire/2", "page": 1, "kind": "wire"}
        assert _ids(result["result"]["pages"], "wires") == ["plan/p1/wire/2"]
        assert _ids(result["result"]["pages"], "components") == []


def test_rejects_missing_start_document_and_bad_skip() -> None:
    with TemporaryDirectory() as directory:
        for kwargs, error in (
            ({"start_id": "plan/p1/component/999"}, TraceStartNotFoundError),
            ({"skip": ("transfer",)}, ValueError),
            ({"skip": ("group",)}, ValueError),
            ({"start_id": "plan/p1/group/5"}, ValueError),
        ):
            try:
                _trace(directory, max_hops=1, **kwargs)
            except error:
                continue
            raise AssertionError(f"{kwargs} should raise {error.__name__}")
        for kwargs in (
            {"start_id": "other/p1/component/1"},
            {"file": "other.pdf", "start_page": 1, "start_kind": "component", "start_local_id": 1},
        ):
            try:
                trace_document(Path(directory) / "graph.sqlite3", max_hops=1, **kwargs)
            except TraceDocumentNotFoundError:
                continue
            raise AssertionError(f"{kwargs} should be rejected as an unknown document")
