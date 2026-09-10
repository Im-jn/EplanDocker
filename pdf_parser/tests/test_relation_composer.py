"""Regression checks for abstract diagram relationship handling."""

from __future__ import annotations

from pdf_parser.processors.relation_composer import handle_relations


def test_groups_connected_wires_into_nets_and_skips_isolated_wires() -> None:
    result = {
        "wires": [
            {"id": 10},
            {"id": 20},
            {"id": 30},
            {"id": 40},
        ],
    }
    relations = [
        {
            "type": "wire_connection",
            "source": "wire:10",
            "target": "endpoint:0",
        },
        {
            "type": "wire_connection",
            "source": "wire:20",
            "target": "endpoint:0",
        },
        {
            "type": "wire_connection",
            "source": "wire:30",
            "target": "endpoint:0",
        },
    ]

    handled = handle_relations(result, relations)

    assert handled == {
        "nets": [{"id": 0, "wire_ids": [10, 20, 30], "endpoints": [0]}],
        "compounds": [],
    }
    assert all(wire["titles"] == [] for wire in result["wires"])
    assert all(wire["descriptions"] == [] for wire in result["wires"])


def test_moves_wire_mark_text_to_wire_and_removes_mark_relations() -> None:
    crossing_vector = object()
    regular_component = {"id": 6, "type": "box"}
    result = {
        "components": [
            {
                "id": 5,
                "type": "wire_mark",
                "title": ["-W1"],
                "descriptions": ["BK 2.5 mm²", "40R3"],
                "attributes": {"wire_vector": crossing_vector},
            },
            regular_component,
        ],
        "wires": [
            {"id": 10, "vectors": [crossing_vector]},
            {"id": 20, "vectors": [], "titles": ["existing"]},
        ],
    }
    relations = [
        {
            "type": "wire_connection",
            "source": "wire:10",
            "target": "component:5",
        },
        {
            "type": "wire_connection",
            "source": "wire:10",
            "target": "endpoint:0",
        },
        {
            "type": "wire_connection",
            "source": "wire:20",
            "target": "endpoint:0",
        },
        {
            "type": "contains",
            "source": "component:6",
            "target": "component:5",
        },
    ]

    handled = handle_relations(result, relations)

    assert result["components"] == [regular_component]
    assert result["wires"][0]["titles"] == ["-W1"]
    assert result["wires"][0]["descriptions"] == ["BK 2.5 mm²", "40R3"]
    assert result["wires"][1]["titles"] == ["existing"]
    assert result["wires"][1]["descriptions"] == []
    assert relations == [
        {"type": "wire_connection", "source": "wire:10", "target": "endpoint:0"},
        {"type": "wire_connection", "source": "wire:20", "target": "endpoint:0"},
    ]
    assert handled == {
        "nets": [{"id": 0, "wire_ids": [10, 20], "endpoints": [0]}],
        "compounds": [{
            "id": 0,
            "root_component_id": 6,
            "sub_component_ids": [],
            "endpoints": [],
        }],
    }


def test_aggregates_component_hierarchy_and_absorbed_endpoints() -> None:
    result = {
        "components": [{"id": 10}, {"id": 11}, {"id": 12}, {"id": 20}],
        "wires": [],
    }
    relations = [
        {"type": "contains", "source": "component:10", "target": "component:11"},
        {"type": "contains", "source": "component:11", "target": "component:12"},
        {"type": "contains", "source": "component:10", "target": "endpoint:3"},
        {"type": "contains", "source": "component:12", "target": "endpoint:7"},
        {"type": "contains", "source": "component:20", "target": "endpoint:9"},
    ]

    handled = handle_relations(result, relations)

    assert handled == {
        "nets": [],
        "compounds": [
            {
                "id": 0,
                "root_component_id": 10,
                "sub_component_ids": [11, 12],
                "endpoints": [3, 7],
            },
            {
                "id": 1,
                "root_component_id": 20,
                "sub_component_ids": [],
                "endpoints": [9],
            },
        ],
    }
