"""Regression checks for diagram relation extraction."""

from __future__ import annotations

from pdf_parser.processors.relation_extractor import (
    merge_adjacent_box_elements,
    organize_relation,
)
from pdf_parser.pages_manager import PathBase
from shapely.geometry import box as shapely_box


def _line(x0: float, y0: float, x1: float, y1: float) -> PathBase:
    return PathBase(type="line", points=[(x0, y0), (x1, y1)])


def test_connects_wire_free_endpoints_to_component_points() -> None:
    result = {
        "components": [
            {"id": 4, "shape": [_line(0, 0, 2, 0)], "bbox": (0, 0, 2, 1), "attributes": {}},
            {"id": 5, "shape": [_line(20, 0, 22, 0)], "bbox": (20, 0, 22, 1), "attributes": {}},
        ],
        "wires": [
            {"id": 7, "free_endpoints": [(2.005, 0), (20, 0)]},
        ],
    }

    relations = organize_relation(result)
    connections = [edge for edge in relations if edge["type"] == "wire_connection"]

    assert [(edge["source"], edge["target"]) for edge in connections] == [
        ("wire:7", "endpoint:0"),
        ("wire:7", "endpoint:1"),
    ]
    assert "free_endpoints" not in result["wires"][0]
    assert all("endpoints" not in item for item in result["components"] + result["wires"])
    assert [edge for edge in relations if edge["type"] == "contains"] == [
        {"type": "contains", "source": "component:0", "target": "endpoint:0"},
        {"type": "contains", "source": "component:1", "target": "endpoint:1"},
    ]


def test_associates_all_global_endpoints_before_deriving_wire_connections() -> None:
    result = {
        "components": [
            {
                "id": 4,
                "shape": [_line(0, 0, 20, 0)],
                "bbox": (0, 0, 20, 0),
                "attributes": {},
            },
        ],
        "endpoints": [
            {
                "id": 10,
                "shape": [PathBase(type="point", points=[(10, 0.005)])],
                "bbox": (10, 0.005, 10, 0.005),
            },
            {
                "id": 11,
                "shape": [PathBase(type="point", points=[(15, 0.005)])],
                "bbox": (15, 0.005, 15, 0.005),
            },
        ],
        "wires": [
            {"id": 7, "vectors": []},
        ],
        "relations": [{"type": "wire_connection", "source": "wire:7", "target": "endpoint:10"}],
    }

    relations = organize_relation(result)

    assert [
        edge
        for edge in relations
        if edge["type"] == "contains"
    ] == [
        {"type": "contains", "source": "component:4", "target": "endpoint:10"},
        {"type": "contains", "source": "component:4", "target": "endpoint:11"},
    ]


def test_connects_wire_endpoint_to_component_vector_interior() -> None:
    result = {
        "components": [
            {"id": 4, "shape": [_line(0, 0, 20, 0)], "bbox": (0, 0, 20, 0), "attributes": {}},
        ],
        "wires": [
            {"id": 7, "vectors": [], "free_endpoints": [(10, 0.005)]},
        ],
    }

    relations = organize_relation(result)

    assert [
        (edge["source"], edge["target"])
        for edge in relations
        if edge["type"] == "wire_connection"
    ] == [("wire:7", "endpoint:0")]


def test_connects_endpoint_across_small_pdf_coordinate_rounding_gap() -> None:
    result = {
        "components": [
            {
                "id": 4,
                "shape": [_line(536.015015, 425.529, 536.015015, 430.682007)],
                "bbox": (536.015015, 425.529, 536.015015, 430.682007),
                "attributes": {},
            },
        ],
        "wires": [
            {
                "id": 27,
                "vectors": [],
                "free_endpoints": [(536.005981, 430.687988)],
            },
        ],
    }

    relations = organize_relation(result)

    assert [
        (edge["source"], edge["target"])
        for edge in relations
        if edge["type"] == "wire_connection"
    ] == [("wire:27", "endpoint:0")]
    assert {"type": "contains", "source": "component:0", "target": "endpoint:0"} in relations


def test_connects_wire_endpoint_inside_closed_component_box_only() -> None:
    result = {
        "components": [
            {
                "id": 4,
                "shape": [],
                "bbox": (0, 0, 20, 20),
                "attributes": {"polygon": shapely_box(0, 0, 20, 20)},
            },
            {
                "id": 5,
                "shape": [],
                "bbox": (30, 0, 50, 20),
                "attributes": {},
            },
        ],
        "wires": [
            {"id": 7, "vectors": [], "free_endpoints": [(10, 10), (40, 10)]},
        ],
    }

    relations = organize_relation(result)

    assert [
        edge
        for edge in relations
        if edge["type"] == "contains" and edge["target"].startswith("endpoint:")
    ] == [{"type": "contains", "source": "component:0", "target": "endpoint:0"}]


def test_connects_wire_endpoint_to_other_wire_segment_interior() -> None:
    result = {
        "components": [],
        "wires": [
            {
                "id": 7,
                "vectors": [_line(0, 0, 20, 0)],
                "free_endpoints": [(0, 0), (20, 0)],
            },
            {
                "id": 8,
                "vectors": [_line(10, -10, 10, 0.005)],
                "free_endpoints": [(10, -10), (10, 0.005)],
            },
        ],
    }

    relations = organize_relation(result)
    connections = [
        edge
        for edge in relations
        if edge["type"] == "wire_connection"
        and edge["source"].startswith("wire:")
        and edge["target"].startswith("endpoint:")
    ]
    endpoints_by_wire = {
        source: {edge["target"] for edge in connections if edge["source"] == source}
        for source in ("wire:7", "wire:8")
    }
    assert endpoints_by_wire["wire:7"] & endpoints_by_wire["wire:8"]


def test_merges_only_adjacent_boxes_into_one_element() -> None:
    outer_shape = _line(0, 0, 20, 0)
    top_shape = _line(5, -4, 8, -4)
    bottom_shape = _line(12, 20.75, 15, 20.75)
    inner_shape = _line(10, 10, 12, 10)
    elements = [
        {
            "id": 10,
            "type": "box",
            "shape": [outer_shape],
            "bbox": (0, 0, 20, 20),
            "attributes": {"polygon": shapely_box(0, 0, 20, 20), "content": []},
            "title": ["outer"],
            "descriptions": [],
        },
        {
            "id": 11,
            "type": "box",
            "shape": [top_shape],
            "bbox": (5, -4, 8, 0),
            "attributes": {"polygon": shapely_box(5, -4, 8, 0), "content": []},
            "title": [],
            "descriptions": ["top"],
        },
        {
            "id": 12,
            "type": "box",
            "shape": [bottom_shape],
            "bbox": (12, 20.75, 15, 24),
            "attributes": {"polygon": shapely_box(12, 20.75, 15, 24), "content": []},
            "title": [],
            "descriptions": ["bottom"],
        },
        {
            "id": 13,
            "type": "box",
            "shape": [inner_shape],
            "bbox": (10, 10, 12, 12),
            "attributes": {"polygon": shapely_box(10, 10, 12, 12), "content": []},
            "title": [],
            "descriptions": ["inner"],
        },
    ]

    aliases = merge_adjacent_box_elements(elements)

    assert aliases == {11: 10, 12: 10}
    assert [element["id"] for element in elements] == [10, 13]
    merged = elements[0]
    assert merged["shape"] == [outer_shape, top_shape, bottom_shape]
    assert merged["bbox"] == {"x0": 0.0, "y0": -4.0, "x1": 20.0, "y1": 24.0}
    assert merged["title"] == ["outer"]
    assert merged["descriptions"] == ["top", "bottom"]


def test_adjacent_boxes_merge_across_line_styles_but_non_box_stays_separate() -> None:
    elements = [
        {
            "id": 1,
            "type": "box",
            "shape": [],
            "bbox": (0, 0, 10, 10),
            "attributes": {"polygon": shapely_box(0, 0, 10, 10)},
        },
        {
            "id": 2,
            "type": "box",
            "shape": [],
            "bbox": (10, 0, 20, 10),
            "attributes": {
                "polygon": shapely_box(10, 0, 20, 10),
                "line_style": "dashed",
            },
        },
        {
            "id": 3,
            "type": "symbol",
            "shape": [],
            "bbox": (-10, 0, 0, 10),
            "attributes": {"polygon": shapely_box(-10, 0, 0, 10)},
        },
    ]

    assert merge_adjacent_box_elements(elements) == {2: 1}
    assert [element["id"] for element in elements] == [1, 3]


def test_relations_from_an_adjacent_box_are_remapped_to_merged_element() -> None:
    result = {
        "elements": [
            {
                "id": 4,
                "type": "box",
                "shape": [],
                "bbox": (0, 0, 20, 20),
                "attributes": {"polygon": shapely_box(0, 0, 20, 20)},
            },
            {
                "id": 5,
                "type": "box",
                "shape": [],
                "bbox": (5, -4, 8, 0),
                "attributes": {"polygon": shapely_box(5, -4, 8, 0)},
            },
        ],
        "endpoints": [],
        "wires": [],
        "relations": [
            {"type": "contains", "source": "element:5", "target": "endpoint:9"},
        ],
    }

    relations = organize_relation(result)

    assert [element["id"] for element in result["elements"]] == [4]
    assert {"type": "contains", "source": "element:4", "target": "endpoint:9"} in relations


def test_fallback_attaches_unowned_wire_endpoint_to_nearest_element() -> None:
    result = {
        "components": [
            {"id": 10, "shape": [_line(0, 6, 10, 6)], "bbox": (0, 6, 10, 6), "attributes": {}},
            {"id": 20, "shape": [_line(0, 3, 10, 3)], "bbox": (0, 3, 10, 3), "attributes": {}},
        ],
        "wires": [
            {"id": 7, "vectors": [], "free_endpoints": [(5, 0)]},
        ],
    }

    relations = organize_relation(result)

    assert {"type": "wire_connection", "source": "wire:7", "target": "endpoint:0"} in relations
    assert [
        edge for edge in relations
        if edge["type"] == "contains" and edge["target"] == "endpoint:0"
    ] == [{"type": "contains", "source": "component:1", "target": "endpoint:0"}]


def test_fallback_leaves_wire_endpoint_unowned_outside_margin() -> None:
    result = {
        "components": [
            {"id": 10, "shape": [_line(0, 10.1, 10, 10.1)], "bbox": (0, 10.1, 10, 10.1), "attributes": {}},
        ],
        "wires": [
            {"id": 7, "vectors": [], "free_endpoints": [(5, 0)]},
        ],
    }

    relations = organize_relation(result)

    assert {"type": "wire_connection", "source": "wire:7", "target": "endpoint:0"} in relations
    assert not any(
        edge["type"] == "contains" and edge["target"] == "endpoint:0"
        for edge in relations
    )


def test_fallback_does_not_replace_an_existing_element_owner() -> None:
    result = {
        "components": [
            {"id": 4, "shape": [_line(0, 0, 10, 0)], "bbox": (0, 0, 10, 0), "attributes": {}},
            {"id": 5, "shape": [_line(0, 2, 10, 2)], "bbox": (0, 2, 10, 2), "attributes": {}},
        ],
        "endpoints": [
            {
                "id": 10,
                "shape": [PathBase(type="point", points=[(5, 0)])],
                "bbox": (5, 0, 5, 0),
            },
        ],
        "wires": [{"id": 7, "vectors": []}],
        "relations": [
            {"type": "wire_connection", "source": "wire:7", "target": "endpoint:10"},
            {"type": "contains", "source": "component:4", "target": "endpoint:10"},
        ],
    }

    relations = organize_relation(result)

    assert [
        edge for edge in relations
        if edge["type"] == "contains" and edge["target"] == "endpoint:10"
    ] == [{"type": "contains", "source": "component:4", "target": "endpoint:10"}]


def test_reports_endpoint_to_endpoint_as_wire_connection() -> None:
    result = {
        "components": [],
        "wires": [
            {"id": 1, "vectors": [_line(0, 0, 10, 0)], "free_endpoints": [(0, 0), (10, 0)]},
            {"id": 2, "vectors": [_line(10, 0, 10, 10)], "free_endpoints": [(10, 0), (10, 10)]},
        ],
    }

    relations = organize_relation(result)

    connections = [
        edge for edge in relations
        if edge["type"] == "wire_connection"
        and edge["source"].startswith("wire:")
        and edge["target"].startswith("endpoint:")
    ]
    wire_1_endpoints = {edge["target"] for edge in connections if edge["source"] == "wire:1"}
    wire_2_endpoints = {edge["target"] for edge in connections if edge["source"] == "wire:2"}
    assert wire_1_endpoints & wire_2_endpoints


def test_uses_only_nearest_containing_parent() -> None:
    result = {
        "components": [
            {"id": 10, "shape": [], "bbox": (0, 0, 100, 100), "attributes": {}},
            {"id": 11, "shape": [], "bbox": (10, 10, 80, 80), "attributes": {}},
            {"id": 12, "shape": [], "bbox": (20, 20, 30, 30), "attributes": {}},
            {"id": 13, "shape": [], "bbox": (120, 0, 130, 10), "attributes": {}},
        ],
        "wires": [],
    }

    relations = organize_relation(result)
    hierarchy = [edge for edge in relations if edge["type"] == "contains"]

    assert [
        (edge["source"], edge["target"])
        for edge in hierarchy
    ] == [
        ("component:10", "component:11"),
        ("component:11", "component:12"),
    ]


def test_component_parent_accepts_sixty_percent_coverage() -> None:
    result = {
        "components": [
            {"id": 10, "shape": [], "bbox": (4, -5, 15, 15), "attributes": {}},
            {"id": 11, "shape": [], "bbox": (0, 0, 10, 10), "attributes": {}},
        ],
        "wires": [],
    }

    relations = organize_relation(result)

    assert [edge for edge in relations if edge["type"] == "contains"] == [{
        "type": "contains",
        "source": "component:10",
        "target": "component:11",
    }]


def test_component_parent_rejects_coverage_below_sixty_percent() -> None:
    result = {
        "components": [
            {"id": 10, "shape": [], "bbox": (4.1, -5, 15, 15), "attributes": {}},
            {"id": 11, "shape": [], "bbox": (0, 0, 10, 10), "attributes": {}},
        ],
        "wires": [],
    }

    relations = organize_relation(result)

    assert [edge for edge in relations if edge["type"] == "contains"] == []
