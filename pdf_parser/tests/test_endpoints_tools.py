from __future__ import annotations

from pdf_parser.pages_manager import PathBase
from pdf_parser.tools.endpoints_tools import EndpointTools


def test_builds_endpoints_from_all_wire_free_endpoints():
    wires = [
        {"id": 3, "free_endpoints": [(0, 0), (10, 0)]},
        {"id": 7, "free_endpoints": [(20, 5)]},
    ]

    tools = EndpointTools(wires)
    endpoints = tools.build()

    assert [endpoint["id"] for endpoint in endpoints] == [0, 1, 2]
    assert [endpoint["shape"][0].points[0] for endpoint in endpoints] == [
        (0.0, 0.0),
        (10.0, 0.0),
        (20.0, 5.0),
    ]
    assert wires == [{"id": 3}, {"id": 7}]
    assert tools.relations == [
        {"type": "wire_connection", "source": "wire:3", "target": "endpoint:0"},
        {"type": "wire_connection", "source": "wire:3", "target": "endpoint:1"},
        {"type": "wire_connection", "source": "wire:7", "target": "endpoint:2"},
    ]
    assert endpoints[2]["bbox"] == {
        "x0": 20.0,
        "y0": 5.0,
        "x1": 20.0,
        "y1": 5.0,
    }
    assert endpoints[2]["title"] == []
    assert endpoints[2]["descriptions"] == []


def test_merges_coincident_endpoints_and_collects_wire_ids():
    wires = [
        {"id": 1, "free_endpoints": [(10, 10)]},
        {"id": 2, "free_endpoints": [(10.005, 10.004)]},
    ]

    endpoints = EndpointTools(wires, merge_tolerance=0.01).build()

    assert len(endpoints) == 1
    assert endpoints[0]["type"] == "point"
    assert endpoints[0]["shape"][0].points == [(10.0, 10.0)]
    assert "wire_ids" not in endpoints[0]
    assert wires == [{"id": 1}, {"id": 2}]


def test_absorbs_nearby_wire_point_into_circle_endpoint():
    wires = [{"id": 4, "free_endpoints": [(12.5, 10)]}]
    circle_shape = [PathBase(type="line", points=[(10, 9), (10, 11)])]
    components = [
        {
            "id": 0,
            "type": "symbol",
            "shape": [],
            "bbox": (0, 0, 5, 5),
            "attributes": {},
        },
        {
            "id": 1,
            "type": "endpoint_circle",
            "shape": circle_shape,
            "bbox": (10, 9, 10, 11),
            "attributes": {},
            "title": [],
            "descriptions": [],
        },
    ]

    endpoints = EndpointTools(
        wires,
        components,
        component_match_tolerance=10.0,
    ).build()

    assert [component["type"] for component in components] == ["symbol"]
    assert components[0]["id"] == 0
    assert len(endpoints) == 1
    assert endpoints[0]["type"] == "circle"
    assert endpoints[0]["shape"] == circle_shape
    assert endpoints[0]["title"] == []
    assert endpoints[0]["descriptions"] == []
    assert wires == [{"id": 4}]


def test_rejects_invalid_free_endpoint_coordinates():
    wires = [{"id": 1, "free_endpoints": [(10,)]}]

    try:
        EndpointTools(wires).build()
    except ValueError as exc:
        assert "two coordinates" in str(exc)
    else:
        raise AssertionError("invalid endpoint should raise ValueError")
