"""Detect rectangular and circular closed vector regions from a VectorBase."""

from __future__ import annotations

import math
from numbers import Integral
from typing import Any, Iterable

from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.utils import bbox_to_dict


Point = tuple[float, float]
AXIS_TOLERANCE_PT = PARSER_CONFIG.vector_geometry.closed_edge_tolerance_pt
BOUNDARY_TOLERANCE_PT = PARSER_CONFIG.vector_geometry.closed_edge_tolerance_pt
CURVE_SEGMENTS = PARSER_CONFIG.geometry.curve_segments
MIN_REGION_AREA_PT2 = PARSER_CONFIG.vector_geometry.min_region_area_pt2
CIRCLE_MIN_CIRCULARITY = PARSER_CONFIG.vector_geometry.circle_min_circularity
CIRCLE_MAX_ASPECT_ERROR = PARSER_CONFIG.vector_geometry.circle_max_aspect_error


class VectorBoxDetector:
    """Find closed rectangular and circular regions from one VectorBase."""

    def __init__(self, vector_base: VectorBase):
        self.vector_base = vector_base
        self.vectors = vector_base.vectors
        self._source_index_by_id = {
            id(vector): vector_base.source_index(index)
            for index, vector in enumerate(self.vectors)
        }

    def detect_boxes(self) -> list[dict[str, Any]]:
        """Return closed rectangular regions detected from linear vectors."""
        return self._dedup_regions(self._detect_rectangular_faces())

    def detect_circles(self) -> list[dict[str, Any]]:
        """Return closed circular regions detected from cubic curve groups."""
        return self._dedup_regions(self._detect_circular_faces())

    def detect_dashed(self, dash_type: str = "dashed") -> list[dict[str, Any]]:
        """Return closed polygonal regions for one dashed stroke category."""
        if dash_type not in {"dashed", "dash_dotted"}:
            raise ValueError("dash_type must be 'dashed' or 'dash_dotted'")
        return self._dedup_regions(self._detect_dashed_faces(dash_type))

    def detect_groups(self) -> list[dict[str, Any]]:
        """Return group polygons drawn with EPLAN dash-dot boundaries."""
        return self.detect_dashed("dash_dotted")

    def detect_cells(self) -> list[dict[str, Any]]:
        """Node all vector intersections and return every closed face."""
        return self._detect_all_faces()

    def solid_vectors(self) -> list[PathBase]:
        """Return vectors whose path style is not dashed."""
        return [vector for vector in self.vectors if not self._is_dashed_vector(vector)]

    def dashed_vectors(self, dash_type: str | None = None) -> list[PathBase]:
        """Return dashed vectors, optionally restricted to one dash category."""
        return [
            vector for vector in self.vectors
            if self._is_dashed_vector(vector)
            and (dash_type is None or vector.dash_type == dash_type)
        ]

    def linear_vectors(self) -> list[PathBase]:
        """Return solid vectors that contribute only horizontal/vertical line segments."""
        linear: list[PathBase] = []
        for vector in self.solid_vectors():
            segments = self._linear_segments(vector)
            if segments and all(self._is_axis_aligned_segment(segment) for segment in segments):
                linear.append(vector)
        return linear

    def dashed_linear_vectors(self, dash_type: str | None = None) -> list[PathBase]:
        """Return dashed vectors that contribute linear polygon edges."""
        return [
            vector
            for vector in self.dashed_vectors(dash_type)
            if self._linear_segments(vector)
        ]

    def circle_vector_groups(self) -> list[list[PathBase]]:
        """Return same-path solid cubic curve groups that can be tested as circles."""
        grouped: dict[Any, list[PathBase]] = {}
        for vector in self.solid_vectors():
            if vector.type != "curve":
                continue
            path_key = self._path_key(vector)
            grouped.setdefault(path_key, []).append(vector)
        return [group for group in grouped.values() if len(group) >= 2]

    def box_boundary_vectors(self) -> list[PathBase]:
        """Return axis-aligned vectors plus cubic corner curves used by rounded boxes."""
        boundary = list(self.linear_vectors())
        boundary.extend(
            vector
            for vector in self.solid_vectors()
            if vector.type == "curve" and len(vector.points) == 4
        )
        return boundary

    def _detect_rectangular_faces(self) -> list[dict[str, Any]]:
        vectors = self.box_boundary_vectors()
        if not vectors:
            return []

        lines = [self._box_boundary_geometry(vector) for vector in vectors]
        if not lines:
            return []
        geometry_tree = STRtree(lines)
        geometry_index_by_id = {
            id(geometry): index for index, geometry in enumerate(lines)
        }

        faces = [
            polygon
            for polygon in polygonize(MultiLineString(lines))
            if isinstance(polygon, Polygon)
            and polygon.area >= MIN_REGION_AREA_PT2
            and self._looks_like_box(polygon)
        ]

        return [
            self._region_record(
                "rectangle",
                face,
                self._box_boundary_vector_indices(
                    face,
                    vectors,
                    geometry_tree,
                    geometry_index_by_id,
                ),
            )
            for face in faces
        ]

    def _detect_circular_faces(self) -> list[dict[str, Any]]:
        regions: list[dict[str, Any]] = []
        for group in self.circle_vector_groups():
            lines = [self._curve_line(vector) for vector in group if len(vector.points) == 4]
            if not lines:
                continue
            for polygon in polygonize(MultiLineString(lines)):
                if not isinstance(polygon, Polygon):
                    continue
                if self._looks_like_circle(polygon):
                    regions.append(
                        self._region_record(
                            "circle",
                            polygon,
                            [self._vector_index(vector) for vector in group],
                        )
                )
        return regions

    def _detect_dashed_faces(self, dash_type: str) -> list[dict[str, Any]]:
        vectors = self.dashed_linear_vectors(dash_type)
        if not vectors:
            return []

        lines: list[LineString] = []
        line_vectors: list[PathBase] = []
        for vector in vectors:
            for segment in self._linear_segments(vector):
                lines.append(LineString(segment))
                line_vectors.append(vector)
        if not lines:
            return []
        geometry_tree = STRtree(lines)
        geometry_index_by_id = {
            id(geometry): index for index, geometry in enumerate(lines)
        }

        faces = [
            polygon
            for polygon in polygonize(MultiLineString(lines))
            if isinstance(polygon, Polygon) and polygon.area >= MIN_REGION_AREA_PT2
        ]

        return [
            self._region_record(
                dash_type,
                face,
                self._boundary_vector_indices(
                    face,
                    line_vectors,
                    geometry_tree,
                    geometry_index_by_id,
                ),
            )
            for face in faces
        ]

    def _detect_all_faces(self) -> list[dict[str, Any]]:
        vectors: list[PathBase] = []
        geometries: list[LineString] = []
        for vector in self.vectors:
            geometry = self._vector_geometry(vector)
            if geometry is None or geometry.is_empty:
                continue
            vectors.append(vector)
            geometries.append(geometry)
        if not geometries:
            return []
        geometry_tree = STRtree(geometries)
        geometry_index_by_id = {
            id(geometry): index for index, geometry in enumerate(geometries)
        }

        noded = unary_union(MultiLineString(geometries))
        faces = [
            polygon
            for polygon in polygonize(noded)
            if isinstance(polygon, Polygon) and polygon.area >= MIN_REGION_AREA_PT2
        ]
        faces.sort(key=lambda polygon: (polygon.bounds[1], polygon.bounds[0], -polygon.area))

        return [
            self._region_record(
                "cell",
                face,
                self._boundary_vector_indices(
                    face,
                    vectors,
                    geometry_tree,
                    geometry_index_by_id,
                ),
            )
            for face in faces
        ]

    def _region_record(
        self,
        category: str,
        polygon: Polygon,
        vector_indices: Iterable[int],
    ) -> dict[str, Any]:
        indices = sorted({int(index) for index in vector_indices if int(index) >= 0})
        index_set = set(indices)
        return {
            "category": category,
            "bbox": bbox_to_dict(polygon.bounds),
            "vectors": [
                vector
                for vector in self.vectors
                if self._vector_index(vector) in index_set
            ],
            "points": [(float(x), float(y)) for x, y in polygon.exterior.coords],
            "polygon": polygon,
        }

    def _linear_segments(self, vector: PathBase) -> list[tuple[Point, Point]]:
        points = self._points(vector)
        if vector.type == "line" and len(points) == 2:
            return [(points[0], points[1])]
        if vector.type in {"rect", "quad"} and len(points) == 4:
            closed = [*points, points[0]]
            return list(zip(closed, closed[1:]))
        return []

    def _vector_geometry(self, vector: PathBase) -> LineString | None:
        points = self._points(vector)
        if vector.type == "curve" and len(points) == 4:
            return self._curve_line(vector)
        if vector.type in {"rect", "quad"} and len(points) == 4:
            return LineString([*points, points[0]])
        if len(points) >= 2:
            return LineString(points)
        return None

    def _box_boundary_geometry(self, vector: PathBase) -> LineString:
        if vector.type == "curve" and len(vector.points) == 4:
            return self._curve_line(vector)
        segments = self._linear_segments(vector)
        if len(segments) == 1:
            return LineString(segments[0])
        coords: list[Point] = []
        for left, right in segments:
            if not coords:
                coords.append(left)
            coords.append(right)
        return LineString(coords)

    def _curve_line(self, vector: PathBase) -> LineString:
        points = self._points(vector)
        p0, p1, p2, p3 = points
        samples: list[Point] = []
        for step in range(CURVE_SEGMENTS + 1):
            t = step / CURVE_SEGMENTS
            u = 1.0 - t
            samples.append(
                (
                    (u**3 * p0[0]) + (3 * u**2 * t * p1[0]) + (3 * u * t**2 * p2[0]) + (t**3 * p3[0]),
                    (u**3 * p0[1]) + (3 * u**2 * t * p1[1]) + (3 * u * t**2 * p2[1]) + (t**3 * p3[1]),
                )
            )
        return LineString(samples)

    def _boundary_vector_indices(
        self,
        polygon: Polygon,
        vectors: list[PathBase],
        geometry_tree: STRtree,
        geometry_index_by_id: dict[int, int],
    ) -> list[int]:
        boundary = polygon.boundary.buffer(BOUNDARY_TOLERANCE_PT)
        return sorted({
            self._vector_index(
                vectors[self._index_from_tree_result(candidate, geometry_index_by_id)]
            )
            for candidate in geometry_tree.query(boundary, predicate="intersects")
        })

    def _box_boundary_vector_indices(
        self,
        polygon: Polygon,
        vectors: list[PathBase],
        geometry_tree: STRtree,
        geometry_index_by_id: dict[int, int],
    ) -> list[int]:
        """Return vectors fully contained by the box's exterior boundary band."""
        boundary = polygon.exterior.buffer(BOUNDARY_TOLERANCE_PT)
        vector_ids = {
            self._vector_index(
                vectors[self._index_from_tree_result(candidate, geometry_index_by_id)]
            )
            for candidate in geometry_tree.query(boundary, predicate="covers")
        }
        return sorted(vector_ids)

    def _is_axis_aligned_rectangle(self, polygon: Polygon) -> bool:
        coords = self._dedup_points((float(x), float(y)) for x, y in polygon.exterior.coords)
        if len(coords) != 4:
            return False
        for left, right in zip(coords, [*coords[1:], coords[0]]):
            if not self._is_axis_aligned_segment((left, right)):
                return False
        bbox = polygon.bounds
        bbox_area = max(float(bbox[2] - bbox[0]), 0.0) * max(float(bbox[3] - bbox[1]), 0.0)
        if bbox_area <= 0:
            return False
        exterior_area = float(Polygon(polygon.exterior).area)
        return abs(exterior_area - bbox_area) <= max(bbox_area * 0.01, MIN_REGION_AREA_PT2)

    def _looks_like_box(self, polygon: Polygon) -> bool:
        if self._is_axis_aligned_rectangle(polygon):
            return True
        x0, y0, x1, y1 = polygon.bounds
        width = float(x1 - x0)
        height = float(y1 - y0)
        bbox_area = width * height
        if width <= 0 or height <= 0 or bbox_area <= 0:
            return False
        exterior_area = float(Polygon(polygon.exterior).area)
        if exterior_area / bbox_area < 0.86:
            return False
        return self._has_box_axis_spans(polygon, width, height)

    def _has_box_axis_spans(self, polygon: Polygon, width: float, height: float) -> bool:
        coords = [(float(x), float(y)) for x, y in polygon.exterior.coords]
        horizontal = 0.0
        vertical = 0.0
        for left, right in zip(coords, coords[1:]):
            dx = abs(right[0] - left[0])
            dy = abs(right[1] - left[1])
            if dy <= AXIS_TOLERANCE_PT:
                horizontal += dx
            if dx <= AXIS_TOLERANCE_PT:
                vertical += dy
        return horizontal >= width * 1.2 and vertical >= height * 1.2

    def _looks_like_circle(self, polygon: Polygon) -> bool:
        if polygon.area < MIN_REGION_AREA_PT2 or polygon.length <= 0:
            return False
        x0, y0, x1, y1 = polygon.bounds
        width = float(x1 - x0)
        height = float(y1 - y0)
        if width <= 0 or height <= 0:
            return False
        aspect_error = abs(width - height) / max(width, height)
        circularity = (4.0 * math.pi * float(polygon.area)) / (float(polygon.length) ** 2)
        return aspect_error <= CIRCLE_MAX_ASPECT_ERROR and circularity >= CIRCLE_MIN_CIRCULARITY

    @staticmethod
    def _is_axis_aligned_segment(segment: tuple[Point, Point]) -> bool:
        left, right = segment
        return (
            abs(left[0] - right[0]) <= AXIS_TOLERANCE_PT
            or abs(left[1] - right[1]) <= AXIS_TOLERANCE_PT
        )

    @staticmethod
    def _dedup_points(points: Iterable[Point]) -> list[Point]:
        deduped: list[Point] = []
        for point in points:
            if deduped and math.dist(deduped[-1], point) <= BOUNDARY_TOLERANCE_PT:
                continue
            deduped.append(point)
        if len(deduped) > 1 and math.dist(deduped[0], deduped[-1]) <= BOUNDARY_TOLERANCE_PT:
            deduped.pop()
        return deduped

    def _dedup_regions(self, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int, int, int]] = set()
        for region in sorted(
            regions,
            key=lambda item: (
                item["category"],
                item["bbox"]["y0"],
                item["bbox"]["x0"],
                item["bbox"]["y1"],
                item["bbox"]["x1"],
            ),
        ):
            bbox = region["bbox"]
            key = (
                str(region["category"]),
                round(float(bbox["x0"]) / BOUNDARY_TOLERANCE_PT),
                round(float(bbox["y0"]) / BOUNDARY_TOLERANCE_PT),
                round(float(bbox["x1"]) / BOUNDARY_TOLERANCE_PT),
                round(float(bbox["y1"]) / BOUNDARY_TOLERANCE_PT),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(region)
        return deduped

    @staticmethod
    def _path_key(vector: PathBase) -> Any:
        return (
            vector.path_meta_value("path_index"),
            vector.path_meta_value("seqno"),
            vector.path_meta_value("path_type"),
        )

    @staticmethod
    def _points(vector: PathBase) -> list[Point]:
        return [(float(point[0]), float(point[1])) for point in vector.points]

    def _vector_index(self, vector: PathBase) -> int:
        return self._source_index_by_id[id(vector)]

    @staticmethod
    def _is_dashed_vector(vector: PathBase) -> bool:
        return vector.is_dashed

    @staticmethod
    def _index_from_tree_result(candidate: Any, geom_to_index: dict[int, int]) -> int:
        if isinstance(candidate, Integral):
            return int(candidate)
        index = geom_to_index.get(id(candidate))
        if index is None:
            raise KeyError("STRtree returned an unknown geometry")
        return index
