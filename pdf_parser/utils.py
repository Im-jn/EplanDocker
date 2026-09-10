from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from pdf_parser.config import PARSER_CONFIG

BBox = tuple[float, float, float, float]
Point = tuple[float, float]

REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_repo_relative(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if path.is_absolute():
        return path.resolve()
    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (REPO_ROOT / path).resolve()


def coerce_bbox(bbox: Any) -> BBox:
    if isinstance(bbox, dict):
        return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def bbox_to_dict(bbox: BBox) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    return {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}


def _bbox_center(bbox: Any) -> Point:
    x0, y0, x1, y1 = coerce_bbox(bbox)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _bbox_gap(a: Any, b: Any) -> float:
    ax0, ay0, ax1, ay1 = coerce_bbox(a)
    bx0, by0, bx1, by1 = coerce_bbox(b)
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return math.hypot(dx, dy)


def _bbox_center_distance(a: Any, b: Any) -> float:
    ac = _bbox_center(a)
    bc = _bbox_center(b)
    return math.hypot(ac[0] - bc[0], ac[1] - bc[1])


def _shape_bbox(shape: Any) -> Any:
    return shape["bbox"] if isinstance(shape, dict) else shape.bbox


def select_anchor_shapes(
    shapes: list[Any],
    *,
    adjacency_gap: float = PARSER_CONFIG.vector_matcher.anchor_adjacency_gap_pt,
    max_anchors: int = 3,
) -> list[Any]:
    """Select distinct, well-spread anchors, preferring non-adjacent shapes."""
    if max_anchors < 1 or not shapes:
        return []
    if len(shapes) <= max_anchors:
        return list(shapes)

    pairs: list[tuple[float, Any, Any]] = []

    for i, left in enumerate(shapes):
        for right in shapes[i + 1 :]:
            distance = _bbox_center_distance(_shape_bbox(left), _shape_bbox(right))
            pairs.append((distance, left, right))

    non_adjacent_pairs = [
        pair
        for pair in pairs
        if _bbox_gap(_shape_bbox(pair[1]), _shape_bbox(pair[2])) > adjacency_gap
    ]
    _, left, right = max(non_adjacent_pairs or pairs, key=lambda pair: pair[0])
    selected = [left, right]

    while len(selected) < min(max_anchors, len(shapes)):
        remaining = [shape for shape in shapes if all(shape is not item for item in selected)]
        non_adjacent = [
            shape
            for shape in remaining
            if all(
                _bbox_gap(_shape_bbox(shape), _shape_bbox(item)) > adjacency_gap
                for item in selected
            )
        ]
        candidates = non_adjacent or remaining
        selected.append(max(
            candidates,
            key=lambda shape: min(
                _bbox_center_distance(_shape_bbox(shape), _shape_bbox(item))
                for item in selected
            ),
        ))

    return selected


def bbox_from_shapes(shapes: Iterable[Any]) -> BBox:
    xs: list[float] = []
    ys: list[float] = []
    for shape in shapes:
        x0, y0, x1, y1 = coerce_bbox(_shape_bbox(shape))
        xs.extend([x0, x1])
        ys.extend([y0, y1])
    if not xs:
        raise ValueError("shape group is empty")
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_corners(bbox: BBox) -> list[Point]:
    x0, y0, x1, y1 = bbox
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _bbox_from_points(points: Iterable[Point]) -> BBox:
    pts = list(points)
    if not pts:
        raise ValueError("point list is empty")
    xs = [point[0] for point in pts]
    ys = [point[1] for point in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def relative_bbox(inner: Any, outer: Any) -> dict[str, float]:
    ix0, iy0, ix1, iy1 = coerce_bbox(inner)
    ox0, oy0, ox1, oy1 = coerce_bbox(outer)
    width = ox1 - ox0
    height = oy1 - oy0
    if abs(width) < 1e-12 or abs(height) < 1e-12:
        raise ValueError("outer bbox must have non-zero width and height")
    return {
        "x0": (ix0 - ox0) / width,
        "y0": (iy0 - oy0) / height,
        "x1": (ix1 - ox0) / width,
        "y1": (iy1 - oy0) / height,
    }


def _transform_point(
    point: Point,
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
    mirrored: bool = False,
) -> Point:
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    x = point[0] * scale * (-1.0 if mirrored else 1.0)
    y = point[1] * scale
    return (
        cos_a * x - sin_a * y + translation[0],
        sin_a * x + cos_a * y + translation[1],
    )


def transform_bbox(
    bbox: Any,
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
    mirrored: bool = False,
) -> BBox:
    return _bbox_from_points(
        _transform_point(
            point,
            rotation_degrees=rotation_degrees,
            scale=scale,
            translation=translation,
            mirrored=mirrored,
        )
        for point in _bbox_corners(coerce_bbox(bbox))
    )
