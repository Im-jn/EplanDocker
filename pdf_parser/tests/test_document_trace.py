"""Tests for document-level component and wire tracing."""

from __future__ import annotations

from pdf_parser.document_trace import TraceStartNotFoundError, trace_document


def _entity(entity_id: int, entity_type: str, page: int) -> dict:
    return {
        "id": entity_id,
        "type": entity_type,
        "page": page,
        "bbox": None,
        "title": [],
        "descriptions": [],
        "elements": [entity_id + 100],
    }


def _document() -> dict:
    transfer = {
        "source_page": 1,
        "source_component": 4,
        "target_page": 2,
        "target_component": 9,
    }
    return {
        "pages": {
            "1": {
                "diagram": {
                    "components": [
                        {**_entity(1, "component", 1), "page_io": "input"},
                        _entity(4, "component", 1),
                    ],
                    "wires": [_entity(2, "wire", 1), _entity(3, "wire", 1)],
                    "endpoints": [_entity(10, "endpoint", 1), _entity(11, "endpoint", 1)],
                    "nets": [_entity(7, "net", 1)],
                    "relations": [
                        {"type": "connection", "source": "component:1", "target": "endpoint:10"},
                        {"type": "connection", "source": "wire:2", "target": "endpoint:10"},
                        {"type": "connection", "source": "wire:3", "target": "endpoint:11"},
                        {"type": "connection", "source": "component:4", "target": "endpoint:11"},
                        {"type": "contains", "source": "net:7", "target": "wire:2"},
                        {"type": "contains", "source": "net:7", "target": "wire:3"},
                    ],
                    "hyperlinks": [],
                    "transfers": [transfer, dict(transfer)],
                },
            },
            "2": {
                "diagram": {
                    "components": [_entity(9, "component", 2)],
                    "wires": [],
                    "endpoints": [],
                    "nets": [],
                    "relations": [],
                    "hyperlinks": [],
                    "transfers": [],
                },
            },
        }
    }


def test_subgraph_traces_reader_hops_and_nests_transfer_on_source_page() -> None:
    result = trace_document(
        _document(),
        start_page=1,
        start_kind="component",
        start_id=1,
        max_hops=3,
    )

    assert result["reached_hops"] == 3
    pages = {page["page_number"]: page for page in result["result"]["pages"]}
    assert [component["id"] for component in pages[1]["components"]] == [1, 4]
    assert [wire["id"] for wire in pages[1]["wires"]] == [2, 3]
    assert [component["id"] for component in pages[2]["components"]] == [9]
    assert pages[1]["transfers"] == [{
        "source_page": 1,
        "source_component": 4,
        "target_page": 2,
        "target_component": 9,
    }]
    assert pages[2]["transfers"] == []
    assert pages[1]["components"][0]["page_io"] == "input"
    for page in pages.values():
        for collection in ("components", "wires", "endpoints", "nets"):
            assert all("elements" not in entity for entity in page[collection])


def test_hops_return_new_entities_and_transfer_evidence_per_layer() -> None:
    result = trace_document(
        _document(),
        start_page=1,
        start_kind="component",
        start_id=1,
        max_hops=3,
        response_format="hops",
    )

    assert len(result["hops"]) == 4
    hop_1_page = result["hops"][1]["pages"][0]
    assert [wire["id"] for wire in hop_1_page["wires"]] == [2, 3]
    assert [net["id"] for net in hop_1_page["nets"]] == [7]
    hop_3_pages = {page["page_number"]: page for page in result["hops"][3]["pages"]}
    assert [component["id"] for component in hop_3_pages[2]["components"]] == [9]
    assert len(hop_3_pages[1]["transfers"]) == 1
    assert hop_3_pages[2]["transfers"] == []


def test_starting_wire_expands_its_net_before_endpoints() -> None:
    result = trace_document(
        _document(),
        start_page=1,
        start_kind="wire",
        start_id=2,
        max_hops=1,
        response_format="hops",
    )

    hop_1 = result["hops"][1]["pages"][0]
    assert [wire["id"] for wire in hop_1["wires"]] == [3]
    assert hop_1["components"] == []


def test_zero_hops_returns_only_the_start_entity() -> None:
    result = trace_document(
        _document(),
        start_page=1,
        start_kind="component",
        start_id=1,
        max_hops=0,
    )

    page = result["result"]["pages"][0]
    assert [component["id"] for component in page["components"]] == [1]
    assert page["wires"] == []
    assert page["endpoints"] == []
    assert page["transfers"] == []


def test_missing_start_entity_is_rejected() -> None:
    try:
        trace_document(
            _document(),
            start_page=1,
            start_kind="component",
            start_id=999,
            max_hops=1,
        )
    except TraceStartNotFoundError:
        return
    raise AssertionError("missing trace start should be rejected")
