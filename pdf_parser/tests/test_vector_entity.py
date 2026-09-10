"""Regression checks for vector entity union-find state."""

from __future__ import annotations

from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.tools.vector_entity import VectorDisjointSet


def _line(*points: tuple[float, float]) -> PathBase:
    return PathBase(type="line", points=list(points))


def test_connected_vectors_refresh_points_after_all_unions() -> None:
    vectors = VectorBase([
        _line((0, 0), (10, 0)),
        _line((5, -5), (5, 5)),
        _line((10, 0), (20, 0)),
    ])

    entities = VectorDisjointSet(
        vectors,
        merge_contained=False,
        merge_nearby=False,
    )
    records = entities.entity_records()

    assert len(records) == 1
    assert len(records[0]["vectors"]) == 3
    assert set(records[0]["points"]) == {
        (0.0, 0.0),
        (5.0, -5.0),
        (5.0, 0.0),
        (5.0, 5.0),
        (10.0, 0.0),
        (20.0, 0.0),
    }
    assert records[0]["bbox"] == {"x0": 0.0, "y0": -5.0, "x1": 20.0, "y1": 5.0}


def test_containment_absorption_keeps_incremental_entity_state() -> None:
    vectors = VectorBase([
        PathBase(type="rect", points=[(0, 0), (20, 0), (20, 20), (0, 20)]),
        _line((5, 5), (8, 5)),
    ])

    entities = VectorDisjointSet(
        vectors,
        merge_contained=False,
        merge_nearby=False,
    )
    containment_merge_count = entities.merge_contained_entities()
    records = entities.entity_records()

    assert entities.raw_entity_count == 2
    assert containment_merge_count == 1
    assert len(records) == 1
    assert len(records[0]["vectors"]) == 2
    assert records[0]["bbox"] == {"x0": 0.0, "y0": 0.0, "x1": 20.0, "y1": 20.0}
