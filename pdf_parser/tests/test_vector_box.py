"""Regression checks for rectangular vector face detection."""

from __future__ import annotations

from shapely.geometry import Polygon

from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.tools.vector_box import VectorBoxDetector


def run_test() -> None:
    detector = VectorBoxDetector(VectorBase([]))
    polygon_with_holes = Polygon(
        [(0, 0), (60, 0), (60, 20), (0, 20)],
        holes=[
            [(3, 3), (18, 3), (18, 15), (3, 15)],
            [(42, 3), (57, 3), (57, 15), (42, 15)],
        ],
    )

    assert polygon_with_holes.area / Polygon(polygon_with_holes.exterior).area < 0.86
    assert detector._is_axis_aligned_rectangle(polygon_with_holes)
    assert detector._looks_like_box(polygon_with_holes)


if __name__ == "__main__":
    run_test()


def _line(x0: float, y0: float, x1: float, y1: float) -> PathBase:
    return PathBase(type="line", points=[(x0, y0), (x1, y1)])


def test_box_boundary_excludes_connectors_that_only_cross_or_touch_it() -> None:
    edges = [
        _line(0, 0, 20, 0),
        _line(20, 0, 20, 10),
        _line(20, 10, 0, 10),
        _line(0, 10, 0, 0),
    ]
    crossing = _line(10, -10, 10, 20)
    touching = _line(20, 5, 30, 5)

    regions = VectorBoxDetector(VectorBase([*edges, crossing, touching])).detect_boxes()
    target = next(region for region in regions if region["bbox"] == {
        "x0": 0.0,
        "y0": 0.0,
        "x1": 20.0,
        "y1": 10.0,
    })

    assert target["vectors"] == edges
