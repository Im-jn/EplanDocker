"""
Bbox-only spatial lookup for PDF drawing primitives from page data.

Coordinates are stored in PyMuPDF page space: origin at the top-left, x grows
rightward, and y grows downward. Query helpers can still accept PDF user-space
boxes when ``coord_space="pdf"`` and a page height is available.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from numbers import Integral
from typing import Any, Sequence

import fitz
from shapely.affinity import affine_transform
from shapely.geometry import LineString, MultiLineString, box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.utils import bbox_from_shapes, bbox_to_dict, relative_bbox, select_anchor_shapes, transform_bbox


BBox = tuple[float, float, float, float]
Point = tuple[float, float]
DEFAULT_QUERY_SLACK = PARSER_CONFIG.vector_matcher.query_slack_pt


def _coerce_bbox(bbox: Any) -> BBox:
    if isinstance(bbox, dict):
        return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
    if isinstance(bbox, fitz.Rect):
        return (float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1))
    if len(bbox) != 4:
        raise ValueError("bbox must contain four values")
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def _normalized_bbox(bbox: Any) -> BBox:
    x0, y0, x1, y1 = _coerce_bbox(bbox)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


@dataclass(frozen=True)
class ShapeMatch:
    """A matched vector and the transform from the query vector to it."""

    vector: PathBase
    rotation_degrees: float
    scale: float
    translation: Point
    max_error: float
    mirrored: bool = False


class _ShapeMatchTransformIndex:
    """Conservatively index anchor matches by their induced symbol transform.

    Neighbouring bins are always searched and candidates are still verified by
    ``_translation_equality``. The index therefore removes distant transform
    comparisons without changing the match tolerance at bucket boundaries.
    """

    def __init__(
        self,
        matches: Sequence[ShapeMatch],
        *,
        reference_point: Point,
        translation_tolerance: float,
        scale_tolerance: float,
    ) -> None:
        if translation_tolerance <= 0 or scale_tolerance <= 0:
            raise ValueError("transform index tolerances must be positive")
        self.reference_point = reference_point
        self.translation_tolerance = float(translation_tolerance)
        self.scale_tolerance = float(scale_tolerance)
        self._buckets: dict[tuple[int, bool, int, int, int], list[ShapeMatch]] = defaultdict(list)
        for match in matches:
            self._buckets[self._key(match)].append(match)

    @staticmethod
    def _rotation_quarter(match: ShapeMatch) -> int:
        return int(round(match.rotation_degrees / 90.0)) % 4

    def _reference_position(self, match: ShapeMatch) -> Point:
        return _transform_points(
            [self.reference_point],
            rotation_degrees=match.rotation_degrees,
            scale=match.scale,
            translation=match.translation,
            mirrored=match.mirrored,
        )[0]

    def _key(self, match: ShapeMatch) -> tuple[int, bool, int, int, int]:
        x, y = self._reference_position(match)
        return (
            self._rotation_quarter(match),
            match.mirrored,
            math.floor(match.scale / self.scale_tolerance),
            math.floor(x / self.translation_tolerance),
            math.floor(y / self.translation_tolerance),
        )

    def candidates(self, match: ShapeMatch) -> Sequence[ShapeMatch]:
        rotation, mirrored, scale_bin, x_bin, y_bin = self._key(match)
        candidates: list[ShapeMatch] = []
        for scale_offset in (-1, 0, 1):
            for x_offset in (-1, 0, 1):
                for y_offset in (-1, 0, 1):
                    candidates.extend(self._buckets.get(
                        (
                            rotation,
                            mirrored,
                            scale_bin + scale_offset,
                            x_bin + x_offset,
                            y_bin + y_offset,
                        ),
                        (),
                    ))
        return candidates


def pdf_bbox_to_mupdf_xyxy(x0: float, y0: float, x1: float, y1: float, page_height_pt: float) -> BBox:
    """Convert PDF user-space bbox to PyMuPDF page-space bbox."""
    h = float(page_height_pt)
    return (float(x0), h - float(y1), float(x1), h - float(y0))


def normalize_query_bbox(
    bbox: tuple[float, float, float, float],
    *,
    coord_space: str,
    page_height_pt: float | None,
) -> BBox:
    x0, y0, x1, y1 = _coerce_bbox(bbox)
    if coord_space == "mupdf":
        return _normalized_bbox((x0, y0, x1, y1))
    if coord_space == "pdf":
        if page_height_pt is None:
            raise ValueError("page_height_pt is required when coord_space='pdf'")
        return _normalized_bbox(pdf_bbox_to_mupdf_xyxy(x0, y0, x1, y1, page_height_pt))
    raise ValueError("coord_space must be 'pdf' or 'mupdf'")


def expand_bbox_slack_xyxy(x0: float, y0: float, x1: float, y1: float, slack: float) -> BBox:
    """Symmetrically expand a bbox; slack=0.1 increases width and height by 10%."""
    if slack < 0:
        raise ValueError("slack must be non-negative")
    width = x1 - x0
    height = y1 - y0
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    half_w = width * (1.0 + slack) / 2.0
    half_h = height * (1.0 + slack) / 2.0
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _point_cloud_signature(points: Sequence[Point]) -> list[float]:
    distances: list[float] = []
    for i, p0 in enumerate(points):
        for p1 in points[i + 1 :]:
            distances.append((p0[0] - p1[0]) ** 2 + (p0[1] - p1[1]) ** 2)
    return sorted(distances)


def _signatures_close(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
    if len(a) != len(b):
        return False
    scale = max(max(a, default=0.0), max(b, default=0.0), 1.0)
    return all(abs(x - y) <= tol * scale for x, y in zip(a, b))


def _fit_rigid_transform(source: Sequence[Point], target: Sequence[Point]) -> tuple[float, float, Point, float]:
    if len(source) != len(target) or not source:
        raise ValueError("source and target point counts must match")

    sx = sum(p[0] for p in source) / len(source)
    sy = sum(p[1] for p in source) / len(source)
    tx = sum(p[0] for p in target) / len(target)
    ty = sum(p[1] for p in target) / len(target)

    cross = 0.0
    dot = 0.0
    for sp, tp in zip(source, target):
        x0 = sp[0] - sx
        y0 = sp[1] - sy
        x1 = tp[0] - tx
        y1 = tp[1] - ty
        dot += x0 * x1 + y0 * y1
        cross += x0 * y1 - y0 * x1

    angle = math.atan2(cross, dot)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    offset = (tx - (cos_a * sx - sin_a * sy), ty - (sin_a * sx + cos_a * sy))

    max_error = 0.0
    for sp, tp in zip(source, target):
        px = cos_a * sp[0] - sin_a * sp[1] + offset[0]
        py = sin_a * sp[0] + cos_a * sp[1] + offset[1]
        max_error = max(max_error, math.hypot(px - tp[0], py - tp[1]))

    return angle, 1.0, offset, max_error


def _fit_fixed_similarity_transform(
    source: Sequence[Point],
    target: Sequence[Point],
    *,
    rotation_degrees: float,
    scale: float,
    mirrored: bool = False,
) -> tuple[float, float, Point, float]:
    if len(source) != len(target) or not source:
        raise ValueError("source and target point counts must match")

    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    transformed = [
        (
            scale * (cos_a * (-point[0] if mirrored else point[0]) - sin_a * point[1]),
            scale * (sin_a * (-point[0] if mirrored else point[0]) + cos_a * point[1]),
        )
        for point in source
    ]
    dx = sum(target_point[0] - source_point[0] for source_point, target_point in zip(transformed, target)) / len(source)
    dy = sum(target_point[1] - source_point[1] for source_point, target_point in zip(transformed, target)) / len(source)

    max_error = 0.0
    for source_point, target_point in zip(transformed, target):
        max_error = max(max_error, math.hypot(source_point[0] + dx - target_point[0], source_point[1] + dy - target_point[1]))

    return angle, float(scale), (dx, dy), max_error


def _estimate_scale(source: Sequence[Point], target: Sequence[Point]) -> float:
    source_dist = math.sqrt(max(_point_cloud_signature(source), default=0.0))
    target_dist = math.sqrt(max(_point_cloud_signature(target), default=0.0))
    if source_dist < 1e-12:
        return 1.0
    return target_dist / source_dist


def _transform_points(
    points: Sequence[Point],
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
    mirrored: bool = False,
) -> list[Point]:
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    out: list[Point] = []
    for point in points:
        x = point[0] * scale * (-1.0 if mirrored else 1.0)
        y = point[1] * scale
        out.append((cos_a * x - sin_a * y + translation[0], sin_a * x + cos_a * y + translation[1]))
    return out


def _transform_shape(
    shape: PathBase,
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
    mirrored: bool = False,
) -> PathBase:
    inner_value = shape.inner_value
    return PathBase(
        type=shape.type,
        code=str(inner_value.get("code", "")),
        points=_transform_points(
            shape.points,
            rotation_degrees=rotation_degrees,
            scale=scale,
            translation=translation,
            mirrored=mirrored,
        ),
        path_meta=inner_value.get("path_meta"),
    )


def _transform_shape_group(
    shapes: Sequence[PathBase],
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
    mirrored: bool = False,
) -> list[PathBase]:
    return [
        _transform_shape(
            shape,
            rotation_degrees=rotation_degrees,
            scale=scale,
            translation=translation,
            mirrored=mirrored,
        )
        for shape in shapes
    ]


def _line_parts(points: Sequence[Point]) -> list[LineString]:
    if len(points) < 2:
        return []
    lines: list[LineString] = []
    for left, right in zip(points, points[1:]):
        if math.hypot(left[0] - right[0], left[1] - right[1]) > 1e-9:
            lines.append(LineString([left, right]))
    return lines


def _shape_group_to_multilinestring(shapes: Sequence[PathBase]) -> MultiLineString:
    lines: list[LineString] = []
    for shape in shapes:
        lines.extend(_line_parts(shape.points))
    if not lines:
        return MultiLineString([])
    return MultiLineString(lines)


def _shape_group_line_styles_match_by_geometry(
    target_shapes: Sequence[PathBase],
    candidate_shapes: Sequence[PathBase],
    *,
    tolerance: float,
) -> bool:
    for is_dashed in (False, True):
        target_geometry = _shape_group_to_multilinestring(
            [shape for shape in target_shapes if shape.is_dashed == is_dashed]
        )
        candidate_geometry = _shape_group_to_multilinestring(
            [shape for shape in candidate_shapes if shape.is_dashed == is_dashed]
        )
        if not _shape_groups_match_by_geometry(
            target_geometry,
            candidate_geometry,
            tolerance=tolerance,
        ):
            return False
    return True


def _transform_geometry(
    geometry: BaseGeometry,
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
    mirrored: bool = False,
) -> BaseGeometry:
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle) * scale
    sin_a = math.sin(angle) * scale
    if mirrored:
        matrix = [-cos_a, -sin_a, -sin_a, cos_a, translation[0], translation[1]]
    else:
        matrix = [cos_a, -sin_a, sin_a, cos_a, translation[0], translation[1]]
    return affine_transform(geometry, matrix)


def _buffered_overlap_ratio(left: BaseGeometry, right: BaseGeometry, buffer_distance: float) -> float:
    left_buffer = left.buffer(buffer_distance)
    right_buffer = right.buffer(buffer_distance)
    union_area = left_buffer.union(right_buffer).area
    if union_area <= 1e-12:
        return 1.0 if left_buffer.is_empty and right_buffer.is_empty else 0.0
    return left_buffer.intersection(right_buffer).area / union_area


def _shape_groups_match_by_geometry(
    target_geometry: BaseGeometry,
    candidate_geometry: BaseGeometry,
    *,
    tolerance: float,
) -> bool:
    if target_geometry.is_empty or candidate_geometry.is_empty:
        return target_geometry.is_empty and candidate_geometry.is_empty

    hausdorff_distance = target_geometry.hausdorff_distance(candidate_geometry)
    buffer_distance = max(float(tolerance), 0.01)
    overlap_ratio = _buffered_overlap_ratio(target_geometry, candidate_geometry, buffer_distance)
    return hausdorff_distance <= tolerance and overlap_ratio >= 0.85


def _shape_groups_match_by_sets(
    target_shapes: Sequence[PathBase],
    candidate_shapes: Sequence[PathBase],
    *,
    tolerance: float,
    type_sensitive: bool,
    missing_vector: int = 0,
) -> bool:
    unused_candidate_indexes = set(range(len(candidate_shapes)))
    unmatched_targets = 0

    for target in target_shapes:
        best: tuple[int, float] | None = None
        target_points = target.points
        for candidate_index in list(unused_candidate_indexes):
            candidate = candidate_shapes[candidate_index]
            if type_sensitive and candidate.type != target.type:
                continue
            if candidate.is_dashed != target.is_dashed:
                continue
            candidate_points = candidate.points
            if len(candidate_points) != len(target_points):
                continue
            for ordered_candidate_points in _candidate_point_orders(candidate_points):
                error = max(
                    math.hypot(target_point[0] - candidate_point[0], target_point[1] - candidate_point[1])
                    for target_point, candidate_point in zip(target_points, ordered_candidate_points)
                )
                if best is None or error < best[1]:
                    best = (candidate_index, error)

        if best is not None and best[1] <= tolerance:
            unused_candidate_indexes.remove(best[0])
        else:
            unmatched_targets += 1
            if unmatched_targets > missing_vector:
                return False

    return True


def _allowed_missing_vectors(target_count: int, missing_vector_ratio: float) -> int:
    if missing_vector_ratio < 0 or missing_vector_ratio >= 1:
        raise ValueError("missing_vector_ratio must be in the range [0, 1)")
    return math.floor(target_count * missing_vector_ratio + 1e-12)


def _candidate_point_orders(points: Sequence[Point]) -> list[list[Point]]:
    pts = list(points)
    if len(pts) <= 2:
        return [pts, list(reversed(pts))]
    orders: list[list[Point]] = []
    for start in range(len(pts)):
        rotated = pts[start:] + pts[:start]
        orders.append(rotated)
        orders.append(list(reversed(rotated)))
    return orders


class VectorMatcher:
    """Search and match vectors from one VectorBase package."""

    def __init__(self, vector_base: VectorBase, *, page_height_pt: float):
        self.vector_base: VectorBase | None = None
        self.page_height_pt = float(page_height_pt)
        self.page_vector: list[PathBase] = []
        self._index_by_vector_id: dict[int, int] = {}
        self.tree: STRtree | None = None
        self.set_vector_base(vector_base, page_height_pt=page_height_pt)

    def set_vector_base(self, vector_base: VectorBase, *, page_height_pt: float | None = None) -> None:
        """Switch this matcher to an already-built vector package."""
        self.vector_base = vector_base
        if page_height_pt is not None:
            self.page_height_pt = float(page_height_pt)
        self.page_vector = vector_base.vectors
        self._index_by_vector_id = {
            id(vector): vector_base.source_index(index)
            for index, vector in enumerate(self.page_vector)
        }
        self.tree = vector_base.tree

    def source_index(self, vector: PathBase) -> int:
        """Return the source-page index for a vector owned by this matcher."""
        return self._index_by_vector_id[id(vector)]

    def _require_page(self) -> None:
        if self.tree is None:
            raise RuntimeError("Please provide PdfPageManager page data first")

    def query_bbox(
        self,
        bbox: Any,
        slack: float = 0.0,
        *,
        coord_space: str = "mupdf",
    ) -> list[PathBase]:
        """Return vectors whose stored bbox is fully covered by the query bbox."""
        self._require_page()
        assert self.tree is not None
        query = normalize_query_bbox(
            _coerce_bbox(bbox),
            coord_space=coord_space,
            page_height_pt=self.page_height_pt,
        )
        query = expand_bbox_slack_xyxy(*query, slack)
        region = box(*query)

        hits: list[PathBase] = []
        for candidate in self.tree.query(region, predicate="intersects"):
            index = self._index_from_tree_result(candidate)
            bbox_geom = box(*_coerce_bbox(self.page_vector[index].bbox))
            if region.covers(bbox_geom):
                hits.append(self.page_vector[index])
        return hits

    @staticmethod
    def _index_from_tree_result(candidate: Any) -> int:
        if isinstance(candidate, Integral):
            return int(candidate)
        raise TypeError("STRtree returned geometry objects; shapely>=2.0 index results are required")

    def match_shape(
        self,
        shape: PathBase,
        *,
        tolerance: float = PARSER_CONFIG.vector_matcher.shape_tolerance_pt,
        type_sensitive: bool = True,
        scale_range: tuple[float, float] = (
            PARSER_CONFIG.vector_matcher.scale_min,
            PARSER_CONFIG.vector_matcher.shape_scale_max,
        ),
        rotation_degrees: Sequence[int] = PARSER_CONFIG.vector_matcher.rotations_degrees,
        allow_mirror: bool = True,
    ) -> list[ShapeMatch]:
        """
        Find page shapes equal to the input after rotation, scaling, and translation.

        When rotation_degrees is provided, only those rotations are considered.
        Scaling is constrained by scale_range.
        """
        self._require_page()

        min_scale, max_scale = scale_range
        if min_scale <= 0 or max_scale <= 0 or min_scale > max_scale:
            raise ValueError("scale_range must be positive and ordered")

        matches: list[ShapeMatch] = []

        for candidate in self.page_vector:
            matches.extend(self._match_shape_candidate(
                shape,
                candidate,
                tolerance=tolerance,
                type_sensitive=type_sensitive,
                scale_range=(min_scale, max_scale),
                rotation_degrees=rotation_degrees,
                allow_mirror=allow_mirror,
            ))

        return matches

    @staticmethod
    def _match_shape_candidate(
        shape: PathBase,
        candidate: PathBase,
        *,
        tolerance: float,
        type_sensitive: bool,
        scale_range: tuple[float, float],
        rotation_degrees: Sequence[int],
        allow_mirror: bool,
    ) -> list[ShapeMatch]:
        """Run the exact ``match_shape`` checks for one candidate pair."""
        if type_sensitive and candidate.type != shape.type:
            return []
        if candidate.is_dashed != shape.is_dashed:
            return []
        query_points = shape.points
        candidate_points = [tuple(point) for point in candidate.points]
        if len(candidate_points) != len(query_points):
            return []

        estimated_scale = _estimate_scale(query_points, candidate_points)
        min_scale, max_scale = scale_range
        if estimated_scale < min_scale - 1e-9 or estimated_scale > max_scale + 1e-9:
            return []

        transforms = [
            (
                _fit_fixed_similarity_transform(
                    query_points,
                    ordered_points,
                    rotation_degrees=angle,
                    scale=estimated_scale,
                    mirrored=mirrored,
                ),
                mirrored,
            )
            for ordered_points in _candidate_point_orders(candidate_points)
            for angle in rotation_degrees
            for mirrored in ((False, True) if allow_mirror else (False,))
        ]
        best_error = min((transform[3] for transform, _ in transforms), default=math.inf)
        if best_error > tolerance:
            return []

        matches: list[ShapeMatch] = []
        tie_tolerance = max(1e-9, tolerance * 1e-9)
        seen_transforms: set[tuple[float, float, float, float, bool]] = set()
        for transform, mirrored in transforms:
            if transform[3] > best_error + tie_tolerance:
                continue
            angle_degrees = math.degrees(transform[0]) % 360.0
            key = (angle_degrees, transform[1], transform[2][0], transform[2][1], mirrored)
            if key in seen_transforms:
                continue
            seen_transforms.add(key)
            matches.append(ShapeMatch(
                vector=candidate,
                rotation_degrees=angle_degrees,
                scale=transform[1],
                translation=transform[2],
                max_error=transform[3],
                mirrored=mirrored,
            ))
        return matches

    def compare_shape_groups(
        self,
        target_shapes: Sequence[PathBase],
        candidate_shapes: Sequence[PathBase],
        *,
        rotation_degrees: float,
        scale: float,
        translation: Point,
        mirrored: bool = False,
        tolerance: float = PARSER_CONFIG.vector_matcher.shape_tolerance_pt,
        type_sensitive: bool = True,
        missing_vector_ratio: float = PARSER_CONFIG.vector_matcher.missing_vector_ratio,
    ) -> bool:
        """Return whether a transformed target group matches a candidate group.

        ``missing_vector_ratio`` is converted to a strict integer allowance with
        ``floor(len(target_shapes) * ratio)``.
        """
        allowed_missing = _allowed_missing_vectors(len(target_shapes), missing_vector_ratio)
        if len(candidate_shapes) < len(target_shapes) - allowed_missing:
            return False

        aligned_targets = _transform_shape_group(
            target_shapes,
            rotation_degrees=rotation_degrees,
            scale=scale,
            translation=translation,
            mirrored=mirrored,
        )
        target_geometry = _transform_geometry(
            _shape_group_to_multilinestring(target_shapes),
            rotation_degrees=rotation_degrees,
            scale=scale,
            translation=translation,
            mirrored=mirrored,
        )
        candidate_geometry = _shape_group_to_multilinestring(candidate_shapes)
        geometry_match = _shape_groups_match_by_geometry(
            target_geometry,
            candidate_geometry,
            tolerance=tolerance,
        )
        line_styles_match = _shape_group_line_styles_match_by_geometry(
            aligned_targets,
            candidate_shapes,
            tolerance=tolerance,
        )
        set_match = _shape_groups_match_by_sets(
            aligned_targets,
            candidate_shapes,
            tolerance=tolerance,
            type_sensitive=type_sensitive,
            missing_vector=allowed_missing,
        )
        return (geometry_match and line_styles_match) or set_match

    def match_patterns(
        self,
        symbol_list: Sequence[Sequence[PathBase]],
        *,
        tolerance: float = PARSER_CONFIG.vector_matcher.shape_tolerance_pt,
        scale_range: tuple[float, float] = (
            PARSER_CONFIG.vector_matcher.scale_min,
            PARSER_CONFIG.vector_matcher.pattern_scale_max,
        ),
        rotation_degrees: Sequence[int] = PARSER_CONFIG.vector_matcher.rotations_degrees,
        allow_mirror: bool = True,
        query_slack: float = DEFAULT_QUERY_SLACK,
        missing_vector_ratio: float = PARSER_CONFIG.vector_matcher.missing_vector_ratio,
        include_debug: bool = False,
    ) -> list[list[dict[str, Any]]]:
        """Match a symbol catalog after one indexed pass over page vectors."""
        symbols = [list(symbol) for symbol in symbol_list]
        if any(not symbol for symbol in symbols):
            raise ValueError("symbol_list must not contain empty symbols")
        if not symbols:
            return []

        min_scale, max_scale = scale_range
        if min_scale <= 0 or max_scale <= 0 or min_scale > max_scale:
            raise ValueError("scale_range must be positive and ordered")

        # The line index folds quarter turns together.  Keep the general public
        # API correct for unusual rotation sets by falling back to the existing
        # matcher when rotations are not multiples of 90 degrees.
        if any(
            not math.isclose(float(angle) / 90.0, round(float(angle) / 90.0), abs_tol=1e-12)
            for angle in rotation_degrees
        ):
            return [
                self.match_pattern(
                    symbol,
                    tolerance=tolerance,
                    scale_range=scale_range,
                    rotation_degrees=rotation_degrees,
                    allow_mirror=allow_mirror,
                    query_slack=query_slack,
                    missing_vector_ratio=missing_vector_ratio,
                    include_debug=include_debug,
                )
                for symbol in symbols
            ]

        from pdf_parser.tools.vector_index import SymbolVectorIndex

        vector_index = SymbolVectorIndex(symbols)
        anchor_matches: dict[int, list[ShapeMatch]] = {
            id(anchor.shape): []
            for anchors in vector_index.anchors_by_symbol
            for anchor in anchors
        }
        for candidate in self.page_vector:
            for anchor in vector_index.query(
                candidate,
                tolerance=tolerance,
                scale_range=scale_range,
                allow_mirror=allow_mirror,
            ):
                anchor_matches[id(anchor.shape)].extend(self._match_shape_candidate(
                    anchor.shape,
                    candidate,
                    tolerance=tolerance,
                    type_sensitive=True,
                    scale_range=scale_range,
                    rotation_degrees=rotation_degrees,
                    allow_mirror=allow_mirror,
                ))

        return [
            self.match_pattern(
                symbol,
                tolerance=tolerance,
                scale_range=scale_range,
                rotation_degrees=rotation_degrees,
                allow_mirror=allow_mirror,
                query_slack=query_slack,
                missing_vector_ratio=missing_vector_ratio,
                include_debug=include_debug,
                _anchor_matches_by_shape_id=anchor_matches,
            )
            for symbol in symbols
        ]

    def match_pattern(
        self,
        shapes: Sequence[PathBase],
        *,
        tolerance: float = PARSER_CONFIG.vector_matcher.shape_tolerance_pt,
        scale_range: tuple[float, float] = (
            PARSER_CONFIG.vector_matcher.scale_min,
            PARSER_CONFIG.vector_matcher.pattern_scale_max,
        ),
        rotation_degrees: Sequence[int] = PARSER_CONFIG.vector_matcher.rotations_degrees,
        allow_mirror: bool = True,
        query_slack: float = DEFAULT_QUERY_SLACK,
        missing_vector_ratio: float = PARSER_CONFIG.vector_matcher.missing_vector_ratio,
        include_debug: bool = False,
        _anchor_matches_by_shape_id: dict[int, list[ShapeMatch]] | None = None,
    ) -> list[dict[str, Any]]:
        """Find matching shape groups for the selected shape group."""
        hits = list(shapes)
        if not hits:
            return []
        allowed_missing = _allowed_missing_vectors(len(hits), missing_vector_ratio)

        target_group_bbox = bbox_from_shapes(hits)
        target_group_center = (
            (target_group_bbox[0] + target_group_bbox[2]) / 2.0,
            (target_group_bbox[1] + target_group_bbox[3]) / 2.0,
        )
        anchor_shapes = select_anchor_shapes(hits)
        if len(anchor_shapes) < 2:
            return []

        anchor_matches_by_index: dict[int, list[ShapeMatch]] = {}
        for anchor_shape in anchor_shapes:
            if _anchor_matches_by_shape_id is None:
                matches = self.match_shape(
                    anchor_shape,
                    tolerance=tolerance,
                    scale_range=scale_range,
                    rotation_degrees=rotation_degrees,
                    allow_mirror=allow_mirror,
                )
            else:
                matches = _anchor_matches_by_shape_id.get(id(anchor_shape), [])
            anchor_matches_by_index[id(anchor_shape)] = matches

        translation_tolerance = max(tolerance, 1.0)
        scale_tolerance = 0.002
        anchor_transform_indexes = {
            id(anchor): _ShapeMatchTransformIndex(
                anchor_matches_by_index[id(anchor)],
                reference_point=target_group_center,
                translation_tolerance=translation_tolerance,
                scale_tolerance=scale_tolerance,
            )
            for anchor in anchor_shapes
        }

        matched_groups: list[dict[str, Any]] = []
        candidate_groups: list[dict[str, Any]] = []
        seen_bboxes: set[tuple[int, int, int, int]] = set()
        attempted_transforms: set[tuple[tuple[int, int, int, int], int, bool]] = set()
        for primary_anchor in anchor_shapes:
            # A symmetric anchor can yield equivalent rotation/reflection
            # transforms for the same physical page vector.  Once one form has
            # produced a complete symbol match, the remaining forms for this
            # symbol/anchor/vector cannot add another physical occurrence.
            successful_anchor_vector_ids: set[int] = set()
            other_anchors = [anchor for anchor in anchor_shapes if anchor is not primary_anchor]
            for anchor_match in anchor_matches_by_index[id(primary_anchor)]:
                anchor_vector_id = id(anchor_match.vector)
                if anchor_vector_id in successful_anchor_vector_ids:
                    continue
                secondary_anchor = next(
                    (
                        anchor
                        for anchor in other_anchors
                        if any(
                            self._translation_equality(
                                anchor_match,
                                secondary_match,
                                reference_point=target_group_center,
                                translation_tolerance=translation_tolerance,
                                scale_tolerance=scale_tolerance,
                            )
                            for secondary_match in anchor_transform_indexes[id(anchor)].candidates(
                                anchor_match
                            )
                        )
                    ),
                    None,
                )
                if secondary_anchor is None:
                    continue

                predicted_bbox = transform_bbox(
                    target_group_bbox,
                    rotation_degrees=anchor_match.rotation_degrees,
                    scale=anchor_match.scale,
                    translation=anchor_match.translation,
                    mirrored=anchor_match.mirrored,
                )
                bbox_key = tuple(round(value * 1000) for value in predicted_bbox)
                if bbox_key in seen_bboxes:
                    continue
                transform_key = (
                    bbox_key,
                    int(round(anchor_match.rotation_degrees * 1000)),
                    anchor_match.mirrored,
                )
                if transform_key in attempted_transforms:
                    continue
                attempted_transforms.add(transform_key)
                candidate_shapes = self.query_bbox(
                    bbox=predicted_bbox,
                    slack=query_slack,
                    coord_space="mupdf",
                )
                group_match = self.compare_shape_groups(
                    hits,
                    candidate_shapes,
                    rotation_degrees=anchor_match.rotation_degrees,
                    scale=anchor_match.scale,
                    translation=anchor_match.translation,
                    mirrored=anchor_match.mirrored,
                    tolerance=tolerance,
                    missing_vector_ratio=missing_vector_ratio,
                )
                record = self._shape_group_record(
                    primary_anchor,
                    secondary_anchor,
                    anchor_match,
                    target_group_bbox,
                    predicted_bbox,
                    candidate_shapes,
                    group_match=group_match,
                )
                if include_debug:
                    candidate_groups.append(record)
                if group_match:
                    successful_anchor_vector_ids.add(anchor_vector_id)
                    seen_bboxes.add(bbox_key)
                    matched_groups.append(record)

        if include_debug:
            return [
                {
                    "target_group": {
                        "shape_count": len(hits),
                        "bbox": bbox_to_dict(target_group_bbox),
                        "anchors_selected": [
                            {
                                "index": self._source_index_or_none(anchor),
                                "type": anchor.type,
                                "bbox": anchor.bbox,
                            }
                            for anchor in anchor_shapes
                        ],
                    },
                    "anchor_match_count": sum(len(matches) for matches in anchor_matches_by_index.values()),
                    "candidate_group_count": len(candidate_groups),
                    "matched_group_count": len(matched_groups),
                    "matched_groups": matched_groups,
                    "candidate_groups": candidate_groups,
                }
            ]
        return matched_groups

    @staticmethod
    def _translation_equality(
        left: ShapeMatch,
        right: ShapeMatch,
        *,
        reference_point: Point,
        translation_tolerance: float,
        scale_tolerance: float,
    ) -> bool:
        left_rotation = int(round(left.rotation_degrees / 90.0)) % 4
        right_rotation = int(round(right.rotation_degrees / 90.0)) % 4
        if left_rotation != right_rotation or left.mirrored != right.mirrored:
            return False
        if abs(left.scale - right.scale) > scale_tolerance:
            return False
        left_reference = _transform_points(
            [reference_point],
            rotation_degrees=left.rotation_degrees,
            scale=left.scale,
            translation=left.translation,
            mirrored=left.mirrored,
        )[0]
        right_reference = _transform_points(
            [reference_point],
            rotation_degrees=right.rotation_degrees,
            scale=right.scale,
            translation=right.translation,
            mirrored=right.mirrored,
        )[0]
        return math.hypot(
            left_reference[0] - right_reference[0],
            left_reference[1] - right_reference[1],
        ) <= translation_tolerance

    def _shape_group_record(
        self,
        primary_anchor: PathBase,
        secondary_anchor: PathBase | None,
        anchor_match: ShapeMatch,
        target_group_bbox: BBox,
        predicted_bbox: BBox,
        candidate_shapes: Sequence[PathBase],
        *,
        group_match: bool,
    ) -> dict[str, Any]:
        return {
            "primary_anchor": {
                "source_index": self._source_index_or_none(primary_anchor),
                "relative_bbox": relative_bbox(primary_anchor.bbox, target_group_bbox),
                "matched_index": self.source_index(anchor_match.vector),
            },
            "secondary_anchor": (
                {
                    "source_index": self._source_index_or_none(secondary_anchor),
                    "relative_bbox": relative_bbox(secondary_anchor.bbox, target_group_bbox),
                }
                if secondary_anchor is not None
                else None
            ),
            "transform": {
                "rotation_degrees": anchor_match.rotation_degrees,
                "scale": anchor_match.scale,
                "translation": anchor_match.translation,
                "max_error": anchor_match.max_error,
                "mirrored": anchor_match.mirrored,
            },
            "predicted_bbox": bbox_to_dict(predicted_bbox),
            "bbox_mupdf": bbox_to_dict(predicted_bbox),
            "shape_count": len(candidate_shapes),
            "group_match": group_match,
            "vectors": [
                self._vector_record(vector)
                for vector in candidate_shapes
            ],
            "shapes": [
                self._vector_record(vector)
                for vector in candidate_shapes
            ],
        }

    def _vector_record(self, vector: PathBase) -> dict[str, Any]:
        record = vector.to_dict()
        record["index"] = self.source_index(vector)
        return record

    def _source_index_or_none(self, vector: PathBase) -> int | None:
        return self._index_by_vector_id.get(id(vector))
