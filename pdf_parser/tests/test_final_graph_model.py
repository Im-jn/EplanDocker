"""Regression checks for the final element/component graph model."""

from __future__ import annotations

from shapely.geometry import box

from pdf_parser.processors.diagram_extractor import assign_remaining_vectors_to_elements
from pdf_parser.processors.diagram_serializer import (
    _remaining_text_with_nearby,
    serialize_diagram,
)
from pdf_parser.processors.relation_composer import handle_relations
from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.tools.vector_box import VectorBoxDetector


def _line(
    start: tuple[float, float],
    end: tuple[float, float],
    dashes: str,
) -> PathBase:
    return PathBase(
        type="line",
        points=[start, end],
        path_meta={"dashes": dashes},
    )


def _boundary(dashes: str) -> VectorBase:
    return VectorBase([
        _line((0, 0), (20, 0), dashes),
        _line((20, 0), (20, 10), dashes),
        _line((20, 10), (0, 10), dashes),
        _line((0, 10), (0, 0), dashes),
    ])


def test_dash_patterns_are_polygonized_into_separate_categories() -> None:
    dashed = VectorBoxDetector(_boundary("[ 5.6693 5.6693 ] 0"))
    dash_dotted = VectorBoxDetector(_boundary("[ 9.0482 1.131 .7087 1.131 ] 0"))

    assert len(dashed.detect_dashed()) == 1
    assert dashed.detect_groups() == []
    assert dash_dotted.detect_dashed() == []
    assert len(dash_dotted.detect_groups()) == 1


def test_final_graph_has_no_element_nodes_or_element_containment_edges() -> None:
    result = {
        "elements": [
            {
                "id": 0,
                "type": "box",
                "shape": [],
                "bbox": (0, 0, 20, 20),
                "attributes": {},
                "title": ["-K1"],
                "descriptions": [],
            },
            {
                "id": 1,
                "type": "symbol",
                "shape": [],
                "bbox": (5, 5, 10, 10),
                "attributes": {},
                "title": [],
                "descriptions": ["Contactor"],
            },
        ],
        "groups": [{
            "id": 0,
            "type": "group",
            "shape": [],
            "bbox": (-1, -1, 21, 21),
            "attributes": {"polygon": box(-1, -1, 21, 21)},
            "title": [],
            "descriptions": [],
        }],
        "wires": [{"id": 7, "vectors": []}],
    }
    intermediate = [
        {"type": "contains", "source": "element:0", "target": "element:1"},
        {"type": "contains", "source": "element:1", "target": "endpoint:3"},
        {"type": "wire_connection", "source": "wire:7", "target": "endpoint:3"},
    ]

    handled = handle_relations(result, intermediate)

    assert handled["components"][0]["element_ids"] == [0, 1]
    assert handled["components"][0]["title"] == ["-K1"]
    assert handled["components"][0]["descriptions"] == ["Contactor"]
    assert handled["relations"] == [
        {"type": "connection", "source": "component:0", "target": "endpoint:3"},
        {"type": "connection", "source": "wire:7", "target": "endpoint:3"},
        {"type": "contains", "source": "group:0", "target": "component:0"},
    ]
    assert handled["nets"] == []
    assert all("element:" not in str(edge) for edge in handled["relations"])
    assert result["groups"][0]["component_ids"] == [0]


def test_net_is_a_first_class_entity_with_common_fields() -> None:
    result = {
        "elements": [],
        "groups": [],
        "wires": [
            {"id": 10, "vectors": [], "titles": ["N1", "SHARED"]},
            {"id": 20, "vectors": [], "titles": ["SHARED", "N2"]},
        ],
    }
    intermediate = [
        {"type": "wire_connection", "source": "wire:10", "target": "endpoint:4"},
        {"type": "wire_connection", "source": "wire:20", "target": "endpoint:4"},
    ]

    net = handle_relations(result, intermediate)["nets"][0]

    assert net == {
        "id": 0,
        "type": "net",
        "shape": [],
        "bbox": None,
        "attributes": {},
        "title": ["SHARED"],
        "descriptions": [],
        "wire_ids": [10, 20],
        "endpoints": [4],
    }


def test_net_title_falls_back_to_a_common_wire_description() -> None:
    result = {
        "elements": [],
        "groups": [],
        "wires": [
            {
                "id": 10,
                "vectors": [],
                "titles": ["LEFT"],
                "descriptions": ["COMMON DESCRIPTION", "left only"],
            },
            {
                "id": 20,
                "vectors": [],
                "titles": ["RIGHT"],
                "descriptions": ["right only", "COMMON DESCRIPTION"],
            },
            {"id": 30, "vectors": [], "titles": ["SOLO"]},
        ],
    }
    intermediate = [
        {"type": "wire_connection", "source": "wire:10", "target": "endpoint:4"},
        {"type": "wire_connection", "source": "wire:20", "target": "endpoint:4"},
    ]

    nets = handle_relations(result, intermediate)["nets"]

    assert len(nets) == 1
    assert nets[0]["wire_ids"] == [10, 20]
    assert nets[0]["title"] == ["COMMON DESCRIPTION"]


def test_net_title_is_empty_without_common_wire_text() -> None:
    result = {
        "elements": [],
        "groups": [],
        "wires": [
            {"id": 10, "vectors": [], "titles": ["LEFT"], "descriptions": ["A"]},
            {"id": 20, "vectors": [], "titles": ["RIGHT"], "descriptions": ["B"]},
        ],
    }
    intermediate = [
        {"type": "wire_connection", "source": "wire:10", "target": "endpoint:4"},
        {"type": "wire_connection", "source": "wire:20", "target": "endpoint:4"},
    ]

    net = handle_relations(result, intermediate)["nets"][0]

    assert net["title"] == []


def test_net_title_finds_an_identifier_embedded_in_descriptions() -> None:
    result = {
        "elements": [],
        "groups": [],
        "wires": [
            {
                "id": 0,
                "vectors": [],
                "descriptions": ["BK 25 mm²", "40R0"],
            },
            {
                "id": 4,
                "vectors": [],
                "descriptions": ["OG 4 mm² 40R0"],
            },
        ],
    }
    intermediate = [
        {"type": "wire_connection", "source": "wire:0", "target": "endpoint:4"},
        {"type": "wire_connection", "source": "wire:4", "target": "endpoint:4"},
    ]

    net = handle_relations(result, intermediate)["nets"][0]

    assert net["title"] == ["40R0"]


def test_page_level_unowned_vector_becomes_part_of_deepest_element_and_component() -> None:
    boundary = _boundary("[ 5.6693 5.6693 ] 0").vectors
    inner_vector = PathBase(type="line", points=[(4, 5), (16, 5)])
    outer = {
        "id": 0,
        "type": "box",
        "shape": boundary,
        "bbox": (0, 0, 20, 10),
        "attributes": {"polygon": box(0, 0, 20, 10), "content": []},
        "title": [],
        "descriptions": [],
    }

    remaining = assign_remaining_vectors_to_elements(VectorBase([inner_vector]), [outer])
    handled = handle_relations(
        {"elements": [outer], "groups": [], "wires": []},
        [],
    )

    assert remaining.vectors == []
    assert outer["attributes"]["content"] == [inner_vector]
    assert inner_vector in outer["shape"]
    assert inner_vector in handled["components"][0]["shape"]


def test_diagram_serializer_centralizes_shapes_and_slims_graph_entities() -> None:
    element_shape = [_line((0, 0), (4, 0), "[] 0")]
    wire_shape = [_line((4, 0), (8, 0), "[] 0")]
    endpoint_shape = [PathBase(type="point", points=[(4, 0)])]
    group_shape = _boundary("[ 9.0482 1.131 .7087 1.131 ] 0").vectors
    remaining_shape = _line((30, 30), (31, 30), "[] 0")
    rich_result = {
        "elements": [{
            "id": 42,
            "type": "symbol",
            "shape": element_shape,
            "bbox": (0, 0, 4, 0),
            "attributes": {"name": "K1"},
            "title": ["-K1"],
            "descriptions": [],
        }],
        "components": [{
            "id": 3,
            "type": "component",
            "bbox": (0, 0, 4, 0),
            "title": ["-K1"],
            "descriptions": ["relay"],
            "element_ids": [42],
            "shape": element_shape,
            "attributes": {"internal": True},
        }],
        "wires": [{"id": 7, "vectors": wire_shape, "bbox": (4, 0, 8, 0)}],
        "endpoints": [{"id": 9, "type": "point", "shape": endpoint_shape, "bbox": (4, 0, 4, 0)}],
        "groups": [{
            "id": 2,
            "type": "group",
            "shape": group_shape,
            "bbox": (0, 0, 20, 10),
            "component_ids": [3],
        }],
        "nets": [{"id": 1, "type": "net", "wire_ids": [7]}],
        "relations": [{"type": "contains", "source": "net:1", "target": "wire:7"}],
        "remaining_vector": VectorBase([remaining_shape]),
        "remaining_text": [{"text": "unused", "bbox": (40, 40, 45, 42)}],
    }

    diagram = serialize_diagram(rich_result, page=12)

    expected_entity_fields = {
        "id", "type", "page", "bbox", "title", "descriptions", "elements",
    }
    for collection in ("components", "endpoints", "wires", "nets", "groups"):
        assert all(set(entity) == expected_entity_fields for entity in diagram[collection])
        assert all(entity["page"] == 12 for entity in diagram[collection])
    assert [element["id"] for element in diagram["elements"]] == list(range(4))
    assert diagram["components"][0]["elements"] == [0]
    assert diagram["wires"][0]["elements"] == [1]
    assert diagram["endpoints"][0]["elements"] == [2]
    assert diagram["groups"][0]["elements"] == [3]
    assert diagram["nets"][0]["elements"] == [1]
    assert diagram["elements"][3]["attributes"]["component_ids"] == [3]
    assert diagram["remaining_vectors"] == [remaining_shape]
    assert all(element["type"] != "unassigned" for element in diagram["elements"])
    assert diagram["remaining_text"][0]["nearby"] == [3]
    assert all("shape" not in entity for collection in ("components", "endpoints", "wires", "nets", "groups") for entity in diagram[collection])


def test_remaining_text_nearby_uses_top_five_and_nearest_distance_band() -> None:
    text = [{"text": "unmatched", "bbox": (0, 0, 10, 10)}]
    ratio_components = [
        {"id": 10, "bbox": (20, 0, 21, 1)},
        {"id": 11, "bbox": (20.5, 0, 21.5, 1)},
        {"id": 12, "bbox": (21, 0, 22, 1)},
        {"id": 13, "bbox": (22, 0, 23, 1)},
        {"id": 14, "bbox": (22.01, 0, 23.01, 1)},
    ]

    ratio_result = _remaining_text_with_nearby(text, ratio_components)

    assert ratio_result[0]["nearby"] == [10, 11, 12, 13]

    overlapping_components = [
        {"id": component_id, "bbox": (2, 2, 4, 4)}
        for component_id in range(6)
    ]

    limited_result = _remaining_text_with_nearby(text, overlapping_components)

    assert limited_result[0]["nearby"] == [0, 1, 2, 3, 4]
