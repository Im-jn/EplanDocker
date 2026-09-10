from __future__ import annotations

from shapely.geometry import box

from pdf_parser.pages_manager import Text, TextBase
from pdf_parser.tools.endpoints_tools import EndpointTools
from pdf_parser.tools.text_matcher import TextMatcher


def _component(
    component_id: int,
    component_type: str,
    bbox: tuple[float, float, float, float],
) -> dict[str, object]:
    attributes = (
        {"polygon": box(*bbox), "content": []}
        if component_type in {"box", "dashed", "arrow"}
        else {}
    )
    return {
        "id": component_id,
        "type": component_type,
        "bbox": bbox,
        "attributes": attributes,
        "title": [],
        "descriptions": [],
    }


def test_proximity_assigns_each_text_to_the_nearest_component_edge():
    components = [
        _component(0, "symbol", (0, 0, 10, 10)),
        _component(1, "symbol", (30, 0, 40, 10)),
    ]
    texts = TextBase([
        {"index": 4, "text": "-K1", "location": (11, 0, 18, 5), "font_size": 9},
        {"index": 5, "text": "Contactor", "location": (11, 7, 25, 12), "font_size": 7},
        {"index": 6, "text": "unmatched", "location": (80, 80, 95, 85), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components(components)

    assert components[0]["title"] == ["-K1"]
    assert components[0]["descriptions"] == ["-K1: Contactor"]
    assert components[1]["title"] == []
    assert [match["semantic_role"] for match in result["matches"]] == [
        "device_tag",
        "description",
    ]
    assert "text_matches" not in components[0]
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["unmatched"]


def test_wire_mark_receives_clustered_text_before_container_matching():
    wire_mark = _component(7, "wire_mark", (10, 10, 12, 12))
    container = _component(8, "box", (0, 0, 100, 100))
    texts = TextBase([
        {"index": 0, "text": "BK 10 mm2", "location": (13, 7, 28, 11), "font_size": 5},
        {"index": 1, "text": "40R002", "location": (13, 12, 23, 16), "font_size": 5},
    ])

    result = TextMatcher(texts).match_components([wire_mark, container])

    assert wire_mark["title"] == []
    assert wire_mark["descriptions"] == ["BK 10 mm2 40R002"]
    assert container["title"] == []
    assert container["descriptions"] == []
    assert [match["component_id"] for match in result["matches"]] == [7]
    assert len(result["remaining_text"]) == 0


def test_containment_uses_polygon_for_unmatched_box_and_dashed_text():
    outer = _component(0, "box", (0, 0, 100, 100))
    inner = _component(1, "dashed", (20, 20, 80, 80))
    texts = TextBase([
        {"index": 8, "text": "Panel", "location": (40, 40, 60, 50), "font_size": 10},
    ])

    result = TextMatcher(texts, proximity_threshold=4).match_components([outer, inner])

    assert outer["title"] == []
    assert inner["title"] == []
    assert inner["descriptions"] == ["Panel"]
    assert result["matches"][0]["component_id"] == 1
    assert len(result["remaining_text"]) == 0


def test_arrow_participates_in_component_text_matching():
    arrow = _component(3, "arrow", (10, 10, 20, 20))
    texts = TextBase([
        {"index": 0, "text": "X1", "location": (12, 12, 17, 17), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=1).match_components([arrow])

    assert arrow["title"] == []
    assert arrow["descriptions"] == ["X1"]
    assert result["matches"][0]["component_id"] == 3
    assert len(result["remaining_text"]) == 0


def test_contained_arrow_receives_text_instead_of_outer_box():
    outer_box = _component(1, "box", (0, 0, 100, 100))
    arrow = _component(2, "arrow", (40, 40, 60, 60))
    texts = TextBase([
        {"index": 0, "text": "A1", "location": (45, 45, 55, 55), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=1).match_components([outer_box, arrow])

    assert outer_box["descriptions"] == []
    assert arrow["descriptions"] == ["A1"]
    assert [match["component_id"] for match in result["matches"]] == [2]
    assert len(result["remaining_text"]) == 0


def test_arrow_matching_runs_before_boxes_with_a_wider_threshold():
    box_component = _component(1, "box", (6, 0, 10, 10))
    arrow = _component(2, "arrow", (11.5, 0, 16.5, 10))
    texts = TextBase([
        {"index": 0, "text": "23R0", "location": (0, 0, 5, 5), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components([box_component, arrow])

    assert box_component["descriptions"] == []
    assert arrow["descriptions"] == ["23R0"]
    assert [match["component_id"] for match in result["matches"]] == [2]
    assert len(result["remaining_text"]) == 0


def test_arrow_matching_does_not_use_containment_as_a_fallback():
    arrow = _component(2, "arrow", (0, 0, 10, 10))
    texts = TextBase([
        {"index": 0, "text": "inside", "location": (2, 2, 8, 8), "font_size": 7},
    ])

    result = TextMatcher(texts, arrow_proximity_threshold=1).match_components([arrow])

    assert arrow["descriptions"] == []
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["inside"]


def test_component_text_is_clustered_then_merged_by_font_size():
    component = _component(0, "symbol", (0, 0, 10, 30))
    texts = TextBase([
        {"index": 0, "text": "Main", "location": (11, 0, 20, 5), "font_size": 10},
        {"index": 1, "text": "switch", "location": (21, 0, 32, 5), "font_size": 10},
        {"index": 2, "text": "24V", "location": (11, 7, 18, 12), "font_size": 7},
        {"index": 3, "text": "DC", "location": (19, 7, 24, 12), "font_size": 7},
        {"index": 4, "text": "remote", "location": (11, 35, 22, 40), "font_size": 6},
    ])

    result = TextMatcher(
        texts,
        proximity_threshold=12,
        cluster_distance=12,
        merge_distance=6,
    ).match_components([component])

    assert component["title"] == ["Main switch"]
    assert component["descriptions"] == ["Main switch: 24V DC", "remote"]
    assert [match["role"] for match in result["matches"]] == [
        "title",
        "description",
        "description",
    ]
    assert result["matches"][0]["text_indices"] == [0, 1]


def test_containment_requires_the_complete_text_bbox():
    component = _component(0, "box", (0, 0, 50, 50))
    texts = TextBase([
        {"index": 1, "text": "partial", "location": (45, 20, 60, 30), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=0).match_components([component])

    assert component["title"] == []
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["partial"]


def test_text_fully_inside_symbol_bbox_is_directly_an_element_description():
    symbol = _component(7, "symbol", (0, 0, 30, 20))
    texts = TextBase([
        {"index": 4, "text": "-K1", "location": (5, 5, 15, 10), "font_size": 10},
    ])

    result = TextMatcher(
        texts,
        symbol_proximity_threshold=0,
    ).match_elements([symbol], symbol_records=[])

    assert symbol["title"] == []
    assert symbol["descriptions"] == ["-K1"]
    assert result["matches"] == [{
        "text": "-K1",
        "text_indices": [4],
        "role": "description",
        "semantic_role": "device_tag",
        "element_id": 7,
    }]
    assert result["remaining_text"] == []


def test_symbol_bbox_containment_requires_the_complete_text_bbox():
    symbol = _component(7, "symbol", (0, 0, 30, 20))
    texts = TextBase([
        {"index": 4, "text": "partial", "location": (25, 5, 35, 10), "font_size": 7},
    ])

    result = TextMatcher(
        texts,
        symbol_proximity_threshold=0,
    ).match_elements([symbol], symbol_records=[])

    assert symbol["descriptions"] == []
    assert result["matches"] == []
    assert [record["text"] for record in result["remaining_text"]] == ["partial"]


def test_endpoint_matching_precedes_symbol_bbox_containment():
    symbol = _component(7, "symbol", (0, 0, 30, 20))
    endpoint = EndpointTools([
        {"id": 8, "free_endpoints": [(15, 10)]},
    ]).build()[0]
    texts = TextBase([
        {"index": 4, "text": "A1", "location": (13, 8, 17, 12), "font_size": 7},
    ])

    result = TextMatcher(texts).match_elements(
        [symbol],
        endpoints=[endpoint],
        symbol_records=[],
    )

    assert symbol["descriptions"] == []
    assert endpoint["descriptions"] == ["A1"]
    assert result["matches"][0]["endpoint_id"] == endpoint["id"]


def test_container_matching_precedes_symbol_bbox_containment():
    symbol = _component(7, "symbol", (0, 0, 30, 20))
    container = _component(8, "box", (5, 5, 20, 15))
    texts = TextBase([
        {"index": 4, "text": "note", "location": (8, 7, 17, 12), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=0).match_elements(
        [symbol, container],
        symbol_records=[],
    )

    assert symbol["descriptions"] == []
    assert container["descriptions"] == ["note"]
    assert result["matches"][0]["element_id"] == 8


def test_semantic_role_is_independent_from_title_and_description_role():
    component = _component(0, "symbol", (0, 0, 10, 30))
    texts = TextBase([
        {"index": 0, "text": "-K1", "location": (11, 0, 20, 5), "font_size": 10},
        {"index": 1, "text": "24V DC", "location": (11, 7, 25, 12), "font_size": 7},
        {"index": 2, "text": "A1", "location": (11, 14, 16, 19), "font_size": 6},
        {"index": 3, "text": "N04/2C", "location": (11, 27, 25, 32), "font_size": 6},
    ])

    result = TextMatcher(texts, proximity_threshold=12).match_components([component])

    assert [(match["role"], match["semantic_role"]) for match in result["matches"]] == [
        ("title", "device_tag"),
        ("description", "parameter"),
        ("description", "pin_label"),
        ("description", "cross_reference"),
    ]


def test_proximity_leaves_a_text_unmatched_when_two_components_are_ambiguous():
    components = [
        _component(0, "symbol", (0, 0, 10, 10)),
        _component(1, "symbol", (20, 0, 30, 10)),
    ]
    texts = TextBase([
        {"index": 0, "text": "center", "location": (12, 2, 18, 7), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components(components)

    assert result["matches"] == []
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["center"]


def test_proximity_penalizes_a_diagonal_component_relationship():
    component = _component(0, "symbol", (0, 0, 10, 10))
    texts = TextBase([
        {"index": 0, "text": "diagonal", "location": (13, 13, 18, 18), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=6).match_components([component])

    assert result["matches"] == []
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["diagonal"]


def test_same_pdf_line_uses_span_index_before_geometry_for_content_order():
    component = _component(0, "symbol", (0, 0, 10, 10))
    texts = TextBase([
        {"index": 0, "text": "second", "location": (11, 2, 15, 7), "font_size": 7,
         "block_index": 1, "line_index": 2, "span_index": 1},
        {"index": 1, "text": "first", "location": (16, 2, 20, 7), "font_size": 7,
         "block_index": 1, "line_index": 2, "span_index": 0},
    ])

    result = TextMatcher(texts, proximity_threshold=12).match_components([component])

    assert result["matches"][0]["text"] == "first second"
    assert result["matches"][0]["text_indices"] == [1, 0]


def test_vertical_text_order_follows_its_direction():
    component = _component(0, "symbol", (0, 0, 10, 40))
    texts = TextBase([
        {"index": 0, "text": "top", "location": (11, 10, 15, 15), "font_size": 7,
         "line_direction": (0, -1)},
        {"index": 1, "text": "bottom", "location": (11, 20, 15, 25), "font_size": 7,
         "line_direction": (0, -1)},
    ])

    result = TextMatcher(texts).match_components([component])

    assert result["matches"][0]["text"] == "bottom top"
    assert result["matches"][0]["text_indices"] == [1, 0]


def test_containment_accepts_nested_bbox_polygon_records():
    component = _component(0, "box", (0, 0, 100, 100))
    component["attributes"]["polygon"] = {"polygon": {"bbox": (0, 0, 100, 100)}}
    texts = TextBase([
        {"index": 0, "text": "inside", "location": (40, 40, 60, 50), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=1).match_components([component])

    assert result["matches"][0]["component_id"] == 0
    assert len(result["remaining_text"]) == 0


def test_global_cluster_descriptions_follow_the_title_component_vote():
    left = _component(0, "symbol", (0, 0, 10, 10))
    right = _component(1, "symbol", (28, 0, 38, 10))
    texts = TextBase([
        {"index": 0, "text": "Title", "location": (11, 2, 18, 7), "font_size": 10},
        {"index": 1, "text": "description", "location": (20, 2, 27, 7), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components([left, right])

    assert left["title"] == ["Title"]
    assert left["descriptions"] == ["Title: description"]
    assert right["title"] == []
    assert right["descriptions"] == []
    assert {match["component_id"] for match in result["matches"]} == {0}
    assert all("vote_counts" not in match for match in result["matches"])


def test_separate_close_clusters_do_not_vote_as_one_component():
    left = _component(0, "symbol", (0, 0, 10, 20))
    right = _component(1, "symbol", (30, 0, 40, 20))
    texts = TextBase([
        {"index": 0, "text": "left", "location": (11, 5, 15, 10), "font_size": 7},
        {"index": 1, "text": "right", "location": (24, 2, 29, 7), "font_size": 7},
        {"index": 2, "text": "side", "location": (24, 9, 29, 14), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components([left, right])

    assert left["title"] == []
    assert left["descriptions"] == ["left"]
    assert right["title"] == []
    assert right["descriptions"] == ["right side"]
    assert {match["component_id"] for match in result["matches"]} == {0, 1}
    assert all("vote_basis" not in match for match in result["matches"])


def test_unmatched_text_follows_a_voted_title_in_the_same_global_cluster():
    component = _component(0, "symbol", (0, 0, 10, 10))
    texts = TextBase([
        {"index": 0, "text": "Title", "location": (11, 2, 15, 7), "font_size": 10},
        {"index": 1, "text": "detail", "location": (16, 2, 22, 7), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=4).match_components([component])

    assert component["title"] == ["Title"]
    assert component["descriptions"] == ["Title: detail"]
    assert len(result["remaining_text"]) == 0
    assert result["matches"][1]["component_id"] == 0


def test_text_clustering_requires_edge_or_center_alignment():
    matcher = TextMatcher(TextBase([]), cluster_distance=5, alignment_tolerance=4)
    left = Text("left", (0, 0, 5, 5), font_size=7)

    assert matcher._are_spatially_related(
        left,
        Text("top-aligned", (7, 3.5, 12, 8.5), font_size=7),
        5,
    )
    assert not matcher._are_spatially_related(
        left,
        Text("unaligned", (7, 4.5, 12, 9.5), font_size=7),
        5,
    )


def test_text_clustering_supports_vertical_layout_alignment():
    matcher = TextMatcher(TextBase([]), cluster_distance=5, alignment_tolerance=4)
    top = Text("top", (0, 0, 5, 5), font_size=7)

    assert matcher._are_spatially_related(
        top,
        Text("center-aligned", (3.5, 7, 8.5, 12), font_size=7),
        5,
    )
    assert not matcher._are_spatially_related(
        top,
        Text("too-far", (3.5, 11, 8.5, 16), font_size=7),
        5,
    )


def test_text_clustering_does_not_mix_horizontal_and_vertical_text():
    matcher = TextMatcher(TextBase([]), cluster_distance=5, alignment_tolerance=4)
    horizontal = Text("horizontal", (0, 0, 5, 5), direction=(1, 0))
    vertical = Text("vertical", (7, 0, 12, 5), direction=(0, 1))

    assert not matcher._are_spatially_related(horizontal, vertical, 5)


def test_endpoint_participates_in_text_absorption_and_receives_content():
    components = [_component(0, "symbol", (0, 0, 10, 10))]
    endpoints = EndpointTools([
        {"id": 4, "free_endpoints": [(20, 5)]},
    ]).build()
    texts = TextBase([
        {"index": 0, "text": "X1", "location": (21, 2, 25, 7), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components(
        components,
        endpoints=endpoints,
    )

    assert components[0]["descriptions"] == []
    assert endpoints[0]["title"] == []
    assert endpoints[0]["descriptions"] == ["X1"]
    assert result["matches"] == [{
        "text": "X1",
        "text_indices": [0],
        "role": "description",
        "semantic_role": "pin_label",
        "endpoint_id": 0,
    }]
    assert len(result["remaining_text"]) == 0


def test_endpoint_accepts_multiple_font_sizes_and_uses_largest_as_title():
    endpoint = EndpointTools([
        {"id": 8, "free_endpoints": [(20, 5)]},
    ]).build()[0]
    texts = TextBase([
        {"index": 0, "text": "Terminal", "location": (21, 2, 27, 7), "font_size": 10},
        {"index": 1, "text": "24V", "location": (28, 2, 33, 7), "font_size": 7},
    ])

    result = TextMatcher(texts, proximity_threshold=8).match_components(
        [],
        endpoints=[endpoint],
    )

    assert endpoint["title"] == ["Terminal"]
    assert endpoint["descriptions"] == ["Terminal: 24V"]
    assert [match["role"] for match in result["matches"]] == ["title", "description"]
    assert all(match["endpoint_id"] == endpoint["id"] for match in result["matches"])
    assert len(result["remaining_text"]) == 0


def test_endpoint_rejects_a_multi_font_cluster_with_an_oversized_group():
    endpoint = EndpointTools([
        {"id": 8, "free_endpoints": [(20, 5)]},
    ]).build()[0]
    texts = TextBase([
        {"index": 0, "text": "Long title", "location": (21, 2, 27, 7), "font_size": 10},
        {"index": 1, "text": "24V", "location": (28, 2, 33, 7), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components([], endpoints=[endpoint])

    assert endpoint["title"] == []
    assert endpoint["descriptions"] == []
    assert result["matches"] == []
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["Long title: 24V"]
    assert result["remaining_text"][0] == {
        "title": ["Long title"],
        "descriptions": ["Long title: 24V"],
        "text": "Long title: 24V",
        "text_indices": [0, 1],
        "bbox": {"x0": 21.0, "y0": 2.0, "x1": 33.0, "y1": 7.0},
    }


def test_staged_endpoint_matching_is_strict_and_precedes_container_matching():
    endpoint = EndpointTools([
        {"id": 8, "free_endpoints": [(20, 5)]},
    ]).build()[0]
    container = _component(0, "box", (0, 0, 30, 20))
    texts = TextBase([
        {"index": 0, "text": "X1", "location": (21, 2, 25, 7), "font_size": 7},
    ])

    result = TextMatcher(texts, endpoint_proximity_threshold=4).match_components(
        [container],
        endpoints=[endpoint],
        symbol_records=[],
    )

    assert endpoint["descriptions"] == ["X1"]
    assert container["descriptions"] == []
    assert result["matches"][0]["endpoint_id"] == 0


def test_staged_container_uses_wider_threshold_than_endpoint():
    endpoint = EndpointTools([
        {"id": 8, "free_endpoints": [(0, 5)]},
    ]).build()[0]
    container = _component(0, "box", (10, 0, 20, 10))
    texts = TextBase([
        {"index": 0, "text": "note", "location": (5, 2, 9, 7), "font_size": 7},
    ])

    result = TextMatcher(texts, endpoint_proximity_threshold=4).match_components(
        [container],
        endpoints=[endpoint],
        symbol_records=[],
    )

    assert endpoint["descriptions"] == []
    assert container["descriptions"] == ["note"]
    assert result["matches"][0]["component_id"] == 0


def test_symbol_catalog_marker_forces_title_even_with_equal_font_size():
    symbol = _component(0, "symbol", (0, 0, 10, 20))
    symbol["attributes"]["symbol_id"] = 3
    texts = TextBase([
        {"index": 0, "text": "-CT1", "location": (11, 1, 20, 6), "font_size": 7},
        {"index": 1, "text": "transformer", "location": (11, 14, 28, 19), "font_size": 7},
    ])

    result = TextMatcher(texts).match_components(
        [symbol],
        symbol_records=[{"symbol": 3, "type": "CT", "descriptions": ""}],
    )

    assert symbol["title"] == ["-CT1"]
    assert symbol["descriptions"] == ["-CT1"]
    assert [(match["text"], match["role"]) for match in result["matches"]] == [
        ("-CT1", "title"),
    ]
    assert [cluster["text"] for cluster in result["remaining_text"]] == ["transformer"]


def test_same_symbol_components_receive_distinct_nearest_marker_clusters():
    left = _component(0, "symbol", (0, 0, 10, 10))
    right = _component(1, "symbol", (50, 0, 60, 10))
    left["attributes"]["symbol_id"] = 2
    right["attributes"]["symbol_id"] = 2
    texts = TextBase([
        {"index": 0, "text": "PL1", "location": (11, 2, 18, 7), "font_size": 7},
        {"index": 1, "text": "PL2", "location": (41, 2, 49, 7), "font_size": 7},
    ])

    result = TextMatcher(
        texts,
        cluster_distance=6,
    ).match_components(
        [left, right],
        symbol_records=[{"symbol": 2, "type": "PL", "descriptions": ""}],
    )

    assert left["title"] == ["PL1"]
    assert right["title"] == ["PL2"]
    assert {match["component_id"] for match in result["matches"]} == {0, 1}


def test_symbol_bipartite_matching_minimizes_global_distance_not_greedy_distance():
    matcher = TextMatcher(TextBase([]))
    symbols = [{"id": 0}, {"id": 1}]
    candidates = [
        (0, 2.0, 0, 0, 0, symbols[0]),
        (0, 4.0, 1, 0, 0, symbols[0]),
        (0, 3.0, 0, 1, 0, symbols[1]),
        (0, 20.0, 1, 1, 0, symbols[1]),
    ]

    selected = matcher._minimum_cost_maximum_symbol_matching(
        candidates,
        symbol_count=2,
        cluster_count=2,
    )

    assert {
        (symbol_order, cluster_index)
        for _, _, cluster_index, symbol_order, _, _ in selected
    } == {(0, 1), (1, 0)}
    assert sum(distance for _, distance, *_ in selected) == 7.0
