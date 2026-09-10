"""Synthetic regression tests for diagram component extraction."""

from __future__ import annotations

from pdf_parser.processors.diagram_extractor import (
    assign_remaining_vectors_to_boxes,
    extract_diagram,
    extract_vector_components,
    extract_wires,
    merge_diagram_results,
)
from pdf_parser.processors.relation_extractor import organize_relation
from pdf_parser.pages_manager import PathBase, TextBase, VectorBase
from shapely.geometry import Point as ShapelyPoint, Polygon, box as shapely_box


def _line(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    dashed: bool = False,
) -> PathBase:
    meta = {"dashes": "[3 2] 0"} if dashed else None
    return PathBase(type="line", points=[(x0, y0), (x1, y1)], path_meta=meta)


def _rect(x0: float, y0: float, x1: float, y1: float) -> PathBase:
    return PathBase(
        type="rect",
        points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
    )


def _circle(path_index: int, x: float, y: float) -> list[PathBase]:
    k = 1.656854
    meta = {"path_index": path_index}
    points = [
        [(3, 0), (3 + k, 0), (6, 3 - k), (6, 3)],
        [(6, 3), (6, 3 + k), (3 + k, 6), (3, 6)],
        [(3, 6), (3 - k, 6), (0, 3 + k), (0, 3)],
        [(0, 3), (0, 3 - k), (3 - k, 0), (3, 0)],
    ]
    return [
        PathBase(
            type="curve",
            points=[(px + x, py + y) for px, py in curve],
            path_meta=meta,
        )
        for curve in points
    ]


def _arrow(x: float, y: float) -> PathBase:
    return PathBase(
        type="quad",
        points=[(x, y), (x + 6, y), (x + 3, y + 6)],
        path_meta={"path_index": 100, "path_type": "fs", "fill": (0.0, 0.0, 0.0)},
    )


def _dashed_rect(x0: float, y0: float, x1: float, y1: float) -> list[PathBase]:
    return [
        _line(x0, y0, x1, y0, dashed=True),
        _line(x1, y0, x1, y1, dashed=True),
        _line(x1, y1, x0, y1, dashed=True),
        _line(x0, y1, x0, y0, dashed=True),
    ]


def run_test() -> None:
    test_symbol_conflict_resolution()
    test_vector_component_detection_pipeline()
    test_wire_extraction_uses_seeded_endpoint_groups()


def test_symbol_conflict_resolution() -> None:
    first_rect = _rect(0, 0, 10, 10)
    first_extension = _line(10, 5, 20, 5)
    second_rect = _rect(50, 20, 60, 30)
    second_extension = _line(60, 25, 70, 25)
    unrelated = _line(100, 0, 110, 0)
    vectors = VectorBase([
        first_rect,
        first_extension,
        second_rect,
        second_extension,
        unrelated,
    ])
    partial_symbol = [_rect(0, 0, 10, 10)]
    complete_symbol = [_rect(0, 0, 10, 10), _line(10, 5, 20, 5)]

    result, remaining = extract_diagram(
        vectors,
        TextBase([]),
        [partial_symbol, complete_symbol],
    )
    components = result["components"]

    assert len(components) == 2
    assert [component["id"] for component in components] == [0, 1]
    assert [component["attributes"]["symbol_id"] for component in components] == [1, 1]
    assert components[0]["shape"] == [first_rect, first_extension]
    assert components[1]["shape"] == [second_rect, second_extension]
    assert components[0]["bbox"] == {"x0": 0.0, "y0": 0.0, "x1": 20.0, "y1": 10.0}
    assert components[1]["bbox"] == {"x0": 50.0, "y0": 20.0, "x1": 70.0, "y1": 30.0}
    assert components[0]["title"] == []
    assert components[0]["descriptions"] == []
    assert remaining.vectors == []


def test_vector_component_detection_pipeline() -> None:
    circle = _circle(10, 0, 0)
    arrow = _arrow(20, 0)
    box = _rect(40, 0, 55, 10)
    dashed = _dashed_rect(70, 0, 85, 10)
    leftover = _line(100, 0, 110, 0)
    vector_base = VectorBase([*circle, arrow, box, *dashed, leftover])

    components, remaining = extract_vector_components(vector_base, [])
    component_types = [component["type"] for component in components]

    assert component_types == ["endpoint_circle", "arrow", "box", "dashed"]
    assert components[1]["attributes"]["direction"] == "down"
    assert [len(component["shape"]) for component in components] == [4, 1, 1, 4]
    dashed_polygon_coords = list(components[3]["attributes"]["polygon"].exterior.coords)
    assert dashed_polygon_coords[0] == dashed_polygon_coords[-1]
    assert all(component["title"] == [] for component in components)
    assert all(component["descriptions"] == [] for component in components)
    assert remaining.vectors == [leftover]


def test_arrow_polygon_connects_wire_endpoint_inside_triangle() -> None:
    arrow = _arrow(20, 0)
    wire = _line(23, 2, 23, 12)

    result, remaining = extract_diagram(
        VectorBase([arrow, wire]),
        TextBase([]),
        [],
    )
    arrow_component = result["components"][0]
    arrow_polygon = arrow_component["attributes"]["polygon"]
    relations = organize_relation(result)

    assert arrow_polygon.covers(ShapelyPoint(23, 2))
    assert [component["type"] for component in result["components"]] == ["arrow"]
    assert {endpoint["type"] for endpoint in result["endpoints"]} == {"point"}
    assert "endpoints" not in result["wires"][0]
    assert [
        (edge["source"], edge["target"])
        for edge in relations
        if edge["type"] == "contains"
        and edge["target"].startswith("endpoint:")
    ] == [("component:0", "endpoint:0")]
    assert remaining.vectors == []


def test_wire_extraction_uses_seeded_endpoint_groups() -> None:
    first = _line(0, 0, 10, 0)
    second = _line(10, 0, 20, 0)
    short_branch = _line(20, 0, 20, 4)
    crossing = _line(5, -4, 5, 4)
    short_separate = _line(30, 0, 34, 0)
    diagonal = _line(40, 0, 50, 10)

    wires, remaining = extract_wires(VectorBase([
        first,
        second,
        short_branch,
        crossing,
        short_separate,
        diagonal,
    ]))

    assert [len(wire["vectors"]) for wire in wires] == [3, 1]
    assert wires[0]["vectors"] == [first, second, short_branch]
    assert wires[0]["free_endpoints"] == [(0.0, 0.0), (20.0, 4.0)]
    assert wires[1]["vectors"] == [crossing]
    assert wires[1]["free_endpoints"] == [(5.0, -4.0), (5.0, 4.0)]
    assert remaining.vectors == [short_separate, diagonal]


def test_wire_recovery_attaches_axis_lines_at_segment_interior_and_updates_endpoints() -> None:
    seed = _line(0, 0, 10, 0)
    interior_branch = _line(5, 0, 5, 4)
    chained_remainder = _line(5, 4, 7, 4)
    unrelated = _line(20, 0, 24, 0)

    wires, remaining = extract_wires(VectorBase([
        seed,
        interior_branch,
        chained_remainder,
        unrelated,
    ]))

    assert len(wires) == 1
    assert wires[0]["vectors"] == [seed, interior_branch, chained_remainder]
    assert wires[0]["free_endpoints"] == [(0.0, 0.0), (10.0, 0.0), (7.0, 4.0)]
    assert remaining.vectors == [unrelated]


def test_wire_recovery_detects_existing_endpoint_on_remaining_vector_and_merges_nets() -> None:
    left = _line(0, 0, 10, 0)
    right = _line(10, 5, 20, 5)
    bridge = _line(10, -1, 10, 6)

    wires, remaining = extract_wires(VectorBase([left, right, bridge]))

    assert len(wires) == 1
    assert wires[0]["vectors"] == [left, right, bridge]
    assert wires[0]["free_endpoints"] == [
        (10.0, -1.0),
        (0.0, 0.0),
        (20.0, 5.0),
        (10.0, 6.0),
    ]
    assert remaining.vectors == []


def test_merges_seeded_collinear_wires_when_endpoint_lies_on_segment() -> None:
    long_seed = _line(0, 0, 20, 0)
    overlapping_seed = _line(15, 0, 25, 0)

    wires, remaining = extract_wires(VectorBase([long_seed, overlapping_seed]))

    assert len(wires) == 1
    assert wires[0]["vectors"] == [long_seed, overlapping_seed]
    assert wires[0]["free_endpoints"] == [(0.0, 0.0), (25.0, 0.0)]
    assert remaining.vectors == []


def test_overlapping_stub_does_not_hide_wire_component_endpoint() -> None:
    component_line = _line(0, 0, 5, 0)
    long_wire = _line(0, 0, 0, 20)
    overlapping_stub = _line(0, 0, 0, 5)

    wires, remaining = extract_wires(VectorBase([long_wire, overlapping_stub]))
    relations = organize_relation({
        "components": [{
            "id": 6,
            "type": "symbol",
            "shape": [component_line],
            "bbox": {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 0.0},
        }],
        "wires": wires,
    })

    assert "free_endpoints" not in wires[0]
    assert "endpoints" not in wires[0]
    assert [
        (edge["source"], edge["target"])
        for edge in relations
        if edge["type"] == "contains"
        and edge["target"].startswith("endpoint:")
    ] == [("component:0", "endpoint:0")]
    assert remaining.vectors == []


def test_terminal_diagonal_does_not_propagate_into_another_wire() -> None:
    left = _line(0, 0, 10, 0)
    terminal_diagonal = _line(10, 0, 14, 4)
    right = _line(14, 4, 24, 4)

    wires, remaining = extract_wires(VectorBase([left, terminal_diagonal, right]))

    assert [wire["vectors"] for wire in wires] == [[left, terminal_diagonal], [right]]
    assert wires[0]["free_endpoints"] == [(0.0, 0.0), (14.0, 4.0)]
    assert wires[1]["free_endpoints"] == [(14.0, 4.0), (24.0, 4.0)]
    assert remaining.vectors == []

    relations = organize_relation({"components": [], "wires": wires})
    connections = [edge for edge in relations if edge["type"] == "wire_connection"]
    targets_by_wire = {
        source: {edge["target"] for edge in connections if edge["source"] == source}
        for source in ("wire:0", "wire:1")
    }
    assert targets_by_wire["wire:0"] & targets_by_wire["wire:1"]


def test_eplan_break_mark_at_nominal_eight_points_does_not_merge_wires() -> None:
    left = _line(0, 0, 10, 0)
    stub = _line(10, 0, 10, 5.669)
    break_mark = _line(10, 5.669, 15.669, 0)
    right = _line(15.669, 0, 30, 0)

    wires, remaining = extract_wires(VectorBase([left, stub, break_mark, right]))

    assert len(wires) == 2
    assert [left, stub, break_mark] in [wire["vectors"] for wire in wires]
    assert [right] in [wire["vectors"] for wire in wires]
    assert remaining.vectors == []


def test_wire_recovery_does_not_continue_from_terminal_diagonal() -> None:
    seed = _line(0, 0, 10, 0)
    terminal_diagonal = _line(10, 0, 14, 4)
    short_axis_line = _line(14, 4, 18, 4)

    wires, remaining = extract_wires(VectorBase([seed, terminal_diagonal, short_axis_line]))

    assert [wire["vectors"] for wire in wires] == [[seed, terminal_diagonal]]
    assert wires[0]["free_endpoints"] == [(0.0, 0.0), (14.0, 4.0)]
    assert remaining.vectors == [short_axis_line]


def test_merge_diagram_results_reassigns_page_ids_and_keeps_entity_ids() -> None:
    first_remain = _line(0, 0, 1, 0)
    second_remain = _line(2, 0, 3, 0)
    results = [
        {
            "components": [{"id": 0, "bbox": (0, 0, 1, 1), "attributes": {}}],
            "wires": [{"id": 0, "vectors": []}],
            "remains": VectorBase([first_remain]),
        },
        {
            "components": [{"id": 0, "bbox": (2, 0, 3, 1), "attributes": {}}],
            "wires": [{"id": 0, "vectors": []}],
            "remains": VectorBase([second_remain]),
        },
    ]

    merged = merge_diagram_results(results)

    assert [component["id"] for component in merged["components"]] == [0, 1]
    assert [component["attributes"]["entity_id"] for component in merged["components"]] == [0, 1]
    assert [wire["id"] for wire in merged["wires"]] == [0, 1]
    assert [wire["entity_id"] for wire in merged["wires"]] == [0, 1]
    assert merged["remains"].vectors == [first_remain, second_remain]


def test_arrow_detection_consumes_duplicate_stroke_outline() -> None:
    arrow = _arrow(20, 0)
    outline = [
        _line(20, 0, 26, 0),
        _line(26, 0, 23, 6),
        _line(23, 6, 20, 0),
    ]
    unrelated = _line(40, 0, 44, 0)

    components, remaining = extract_vector_components(
        VectorBase([arrow, *outline, unrelated]),
        [],
    )

    assert [component["type"] for component in components] == ["arrow"]
    assert remaining.vectors == [unrelated]


def test_box_absorbs_only_still_unmatched_inner_vectors() -> None:
    box = _rect(0, 0, 30, 30)
    arrow = _arrow(10, 10)
    dashed = _dashed_rect(15, 15, 25, 25)
    inner_remaining = _line(5, 20, 9, 24)
    dashed_inner_remaining = _line(17, 20, 21, 24)
    outside_remaining = _line(40, 20, 44, 24)

    components, remaining = extract_vector_components(
        VectorBase([
            box,
            arrow,
            *dashed,
            inner_remaining,
            dashed_inner_remaining,
            outside_remaining,
        ]),
        [],
    )

    arrow_component = next(component for component in components if component["type"] == "arrow")
    box_component = next(component for component in components if component["type"] == "box")
    dashed_component = next(component for component in components if component["type"] == "dashed")
    assert arrow in arrow_component["shape"]
    assert box_component["attributes"]["content"] == [inner_remaining]
    assert dashed_component["attributes"]["content"] == [dashed_inner_remaining]
    assert arrow not in box_component["attributes"]["content"]
    assert all(vector not in box_component["attributes"]["content"] for vector in dashed_component["shape"])
    assert remaining.vectors == [outside_remaining]


def test_box_absorbs_long_axis_line_before_wire_extraction() -> None:
    box = _rect(0, 0, 30, 30)
    inner_wire_like_line = _line(5, 15, 25, 15)

    result, remaining = extract_diagram(
        VectorBase([box, inner_wire_like_line]),
        TextBase([]),
        [],
    )

    box_component = next(
        component for component in result["components"] if component["type"] == "box"
    )
    assert box_component["attributes"]["content"] == [inner_wire_like_line]
    assert result["wires"] == []
    assert remaining.vectors == []


def test_dashed_absorbs_only_fully_contained_lines_before_wire_extraction() -> None:
    dashed = _dashed_rect(0, 0, 30, 30)
    inner_wire_like_line = _line(5, 15, 25, 15)
    crossing_line = _line(20, 20, 40, 20)

    result, remaining = extract_diagram(
        VectorBase([*dashed, inner_wire_like_line, crossing_line]),
        TextBase([]),
        [],
    )

    dashed_component = next(
        component for component in result["components"] if component["type"] == "dashed"
    )
    assert dashed_component["attributes"]["content"] == [inner_wire_like_line]
    assert result["wires"] == [
        {
            "id": 0,
            "vectors": [crossing_line],
            "free_endpoints": [(20.0, 20.0), (40.0, 20.0)],
            "titles": [],
            "descriptions": [],
        }
    ]
    assert remaining.vectors == []


def test_remaining_vector_uses_smallest_containing_box() -> None:
    vector = _line(4, 4, 6, 6)
    outer = {"attributes": {"content": []}}
    inner = {"attributes": {"content": []}}

    remaining = assign_remaining_vectors_to_boxes(
        VectorBase([vector]),
        [
            (outer, shapely_box(0, 0, 20, 20)),
            (inner, shapely_box(2, 2, 8, 8)),
        ],
    )

    assert inner["attributes"]["content"] == [vector]
    assert outer["attributes"]["content"] == []
    assert remaining.vectors == []


def test_box_ownership_ignores_polygonize_holes_and_keeps_nested_priority() -> None:
    inner_vector = _line(4, 4, 6, 6)
    outer_only_vector = _line(12, 4, 14, 6)
    outer = {"attributes": {"content": []}}
    inner = {"attributes": {"content": []}}
    polygon_with_hole = Polygon(
        [(0, 0), (20, 0), (20, 20), (0, 20)],
        holes=[[(2, 2), (8, 2), (8, 8), (2, 8)]],
    )
    outer_exterior = Polygon(polygon_with_hole.exterior)

    remaining = assign_remaining_vectors_to_boxes(
        VectorBase([inner_vector, outer_only_vector]),
        [
            (outer, outer_exterior),
            (inner, shapely_box(2, 2, 8, 8)),
        ],
    )

    assert inner["attributes"]["content"] == [inner_vector]
    assert outer["attributes"]["content"] == [outer_only_vector]
    assert remaining.vectors == []


if __name__ == "__main__":
    run_test()
