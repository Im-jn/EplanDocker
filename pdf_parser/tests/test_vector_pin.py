"""Synthetic regression checks for fixed vector pin detection and persistence."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.tools.vector_pin import VectorPinDetector
from shapely.geometry import Point as ShapelyPoint, Polygon


def _circle(
    path_index: int = 1,
    *,
    scale: float = 1.0,
    translation: tuple[float, float] = (0.0, 0.0),
) -> list[PathBase]:
    k = 1.656854
    meta = {"path_index": path_index}
    circles = [
        PathBase(type="curve", points=[(3, 0), (3 + k, 0), (6, 3 - k), (6, 3)], path_meta=meta),
        PathBase(type="curve", points=[(6, 3), (6, 3 + k), (3 + k, 6), (3, 6)], path_meta=meta),
        PathBase(type="curve", points=[(3, 6), (3 - k, 6), (0, 3 + k), (0, 3)], path_meta=meta),
        PathBase(type="curve", points=[(0, 3), (0, 3 - k), (3 - k, 0), (3, 0)], path_meta=meta),
    ]
    return [
        PathBase(
            type=curve.type,
            points=[
                (x * scale + translation[0], y * scale + translation[1])
                for x, y in curve.points
            ],
            path_meta=curve.inner_value["path_meta"],
        )
        for curve in circles
    ]


def run_test() -> None:
    fill_meta = {"path_index": 2, "path_type": "fs", "fill": (0.0, 0.0, 0.0)}
    arrow = PathBase(type="quad", points=[(20, 0), (26, 0), (23, 6)], path_meta=fill_meta)
    hollow_arrow = PathBase(type="quad", points=[(30, 0), (36, 0), (33, 6)])
    diagonal_arrow = PathBase(
        type="quad",
        points=[(50, 0), (54, 4), (50, 4)],
        path_meta={"path_index": 4, "path_type": "fs", "fill": (0.0, 0.0, 0.0)},
    )
    up_arrow = PathBase(
        type="quad",
        points=[(60, 10), (66, 10), (63, 4)],
        path_meta={"path_index": 5, "path_type": "fs", "fill": (0.0, 0.0, 0.0)},
    )
    right_arrow = PathBase(
        type="quad",
        points=[(70, 0), (70, 6), (76, 3)],
        path_meta={"path_index": 6, "path_type": "fs", "fill": (0.0, 0.0, 0.0)},
    )
    base = VectorBase(
        [
            *_circle(),
            *_circle(7, translation=(10, 0)),
            *_circle(8, scale=0.5, translation=(30, 0)),
            arrow,
            hollow_arrow,
            diagonal_arrow,
            up_arrow,
            right_arrow,
            PathBase(type="curve", points=[(80, 0), (81, 1), (82, 2), (83, 3)],
                     path_meta={"path_index": 9}),
            PathBase(type="curve", points=[(83, 3), (84, 2), (85, 1), (86, 0)],
                     path_meta={"path_index": 9}),
        ]
    )

    with TemporaryDirectory() as directory:
        template_path = Path(directory) / "vector_pin_templates.json"
        detector = VectorPinDetector(base, page_height_pt=100, template_path=template_path)
        circles = detector.detect_circle_pins(use_templates=False)
        arrows = detector.detect_arrow_pins(use_templates=False)

        assert len(circles) == 2
        assert all(abs(circle["bbox"]["x1"] - circle["bbox"]["x0"] - 6.0) < 0.01 for circle in circles)
        assert len(arrows) == 3
        assert circles[0]["category"] == "pin_circle"
        assert arrows[0]["category"] == "pin_arrow"
        assert {arrow["direction"] for arrow in arrows} == {"up", "down", "right"}
        assert all(arrow["source"] == "geometry" for arrow in arrows)
        assert all(isinstance(arrow["polygon"], Polygon) for arrow in arrows)
        assert arrows[0]["polygon"].covers(ShapelyPoint(23, 2))

        detector.save("arrow", arrows[0]["vectors"])
        assert template_path.is_file()
        assert len(detector.load()["arrow"]) == 1
        assert len(detector.detect_arrow_pins()) == 3

        rotated_arrow = PathBase(
            type="quad",
            points=[(40, 0), (40, 6), (34, 3)],
            path_meta={"path_index": 3, "path_type": "fs", "fill": (0.0, 0.0, 0.0)},
        )
        rotated_detector = VectorPinDetector(
            VectorBase([rotated_arrow]),
            page_height_pt=100,
            template_path=template_path,
        )
        rotated_hits = rotated_detector.detect_arrow_pins()
        assert len(rotated_hits) == 1
        assert rotated_hits[0]["direction"] == "left"
        assert rotated_hits[0]["polygon"].covers(ShapelyPoint(38, 3))

    wire_vectors = [
        PathBase(type="line", points=[(0, -10), (0, 10)]),
        PathBase(type="line", points=[(-2.5, -2.5), (2.5, 2.5)]),
        PathBase(type="line", points=[(20, -10), (20, 10)]),
        PathBase(type="line", points=[(17.5, -2.5), (22.5, 2.5)]),
        PathBase(type="line", points=[(-10, 20), (10, 20)]),
        PathBase(type="line", points=[(-2.5, 17.5), (2.5, 22.5)]),
        # A minority model with a different length and angle.
        PathBase(type="line", points=[(40, -10), (40, 10)]),
        PathBase(type="line", points=[(38, 2), (42, -2)]),
        # This otherwise valid mark has another line connected to one endpoint.
        PathBase(type="line", points=[(60, -10), (60, 10)]),
        PathBase(type="line", points=[(57.5, -2.5), (62.5, 2.5)]),
        PathBase(type="line", points=[(62.5, 2.5), (67, 2.5)]),
        # This diagonal crosses the wire away from its own center.
        PathBase(type="line", points=[(80, -10), (80, 10)]),
        PathBase(type="line", points=[(78, -1), (84, 5)]),
    ]
    wire_marks = VectorPinDetector(VectorBase(wire_vectors)).detect_wire_mark()
    assert len(wire_marks) == 3
    assert all(abs(mark["length"] - 7.0710678118654755) < 1e-9 for mark in wire_marks)
    assert all(abs(mark["angle_degrees"] - 45.0) < 1e-9 for mark in wire_marks)
    assert all(mark["crossing_vector"] in wire_vectors for mark in wire_marks)


if __name__ == "__main__":
    run_test()
