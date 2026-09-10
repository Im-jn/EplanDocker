"""Detect fixed vector ports and repeated wire marks."""

from __future__ import annotations

import json
import math
from numbers import Integral
from pathlib import Path
from typing import Any, Iterable, Sequence

from shapely.geometry import LineString, MultiLineString, Point as ShapelyPoint, Polygon
from shapely.ops import polygonize
from shapely.strtree import STRtree

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.utils import bbox_from_shapes, bbox_to_dict

from .vector_box import VectorBoxDetector
from .vector_matcher import VectorMatcher


Point = tuple[float, float]
PIN_TEMPLATE_VERSION = 1
DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "storage"
    / "data"
    / "vector_pin_templates.json"
)
MIN_PIN_SIZE_PT = PARSER_CONFIG.vector_pin.min_pin_size_pt
MAX_CIRCLE_DIAMETER_PT = PARSER_CONFIG.vector_pin.max_circle_diameter_pt
CIRCLE_SIZE_ABSOLUTE_TOLERANCE_PT = PARSER_CONFIG.vector_pin.circle_size_absolute_tolerance_pt
CIRCLE_SIZE_RELATIVE_TOLERANCE = PARSER_CONFIG.vector_pin.circle_size_relative_tolerance
MAX_ARROW_SIZE_PT = PARSER_CONFIG.vector_pin.max_arrow_size_pt
MIN_ARROW_AREA_PT2 = PARSER_CONFIG.vector_pin.min_arrow_area_pt2
POINT_TOLERANCE_PT = PARSER_CONFIG.vector_pin.point_tolerance_pt
ISOSCELES_LEG_ERROR_RATIO = PARSER_CONFIG.vector_pin.isosceles_leg_error_ratio
EQUILATERAL_SIDE_ERROR_RATIO = PARSER_CONFIG.vector_pin.equilateral_side_error_ratio
DIRECTION_AXIS_ERROR_RATIO = PARSER_CONFIG.vector_pin.direction_axis_error_ratio
MAX_WIRE_MARK_LENGTH_PT = PARSER_CONFIG.vector_pin.max_wire_mark_length_pt
WIRE_MARK_ENDPOINT_TOLERANCE_PT = PARSER_CONFIG.vector_pin.wire_mark_endpoint_tolerance_pt
WIRE_MARK_CENTER_TOLERANCE_PT = PARSER_CONFIG.vector_pin.wire_mark_center_tolerance_pt
WIRE_MARK_AXIS_TOLERANCE_DEGREES = PARSER_CONFIG.vector_pin.wire_mark_axis_tolerance_degrees
WIRE_MARK_ANGLE_CLUSTER_TOLERANCE_DEGREES = (
    PARSER_CONFIG.vector_pin.wire_mark_angle_cluster_tolerance_degrees
)
WIRE_MARK_LENGTH_RELATIVE_TOLERANCE = (
    PARSER_CONFIG.vector_pin.wire_mark_length_relative_tolerance
)


class VectorPinDetector:
    """Find circle/arrow ports and repeated wire marks in a VectorBase."""

    KINDS = ("circle", "arrow")

    def __init__(
        self,
        vector_base: VectorBase,
        *,
        page_height_pt: float = 1.0,
        template_path: str | Path | None = None,
    ):
        self.vector_base = vector_base
        self.vectors = vector_base.vectors
        self.page_height_pt = float(page_height_pt)
        self.template_path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
        self._source_index_by_id = {
            id(vector): vector_base.source_index(index)
            for index, vector in enumerate(self.vectors)
        }

    def detect(self, kind: str, *, use_templates: bool = True) -> list[dict[str, Any]]:
        """Detect one pin kind, preferring persisted models when available."""
        normalized_kind = self._normalize_kind(kind)
        templates = self.load().get(normalized_kind, []) if use_templates else []
        if templates:
            regions = self._detect_from_templates(normalized_kind, templates)
            return self._vote_circle_size(regions) if normalized_kind == "circle" else regions
        if normalized_kind == "circle":
            return self.detect_circle_pins(use_templates=False)
        return self.detect_arrow_pins(use_templates=False)

    def detect_circle_pins(self, *, use_templates: bool = True) -> list[dict[str, Any]]:
        """Return small closed circular ports."""
        if use_templates:
            templates = self.load().get("circle", [])
            if templates:
                return self._vote_circle_size(self._detect_from_templates("circle", templates))

        regions: list[dict[str, Any]] = []
        for region in VectorBoxDetector(self.vector_base).detect_circles():
            bbox = region["bbox"]
            width = float(bbox["x1"] - bbox["x0"])
            height = float(bbox["y1"] - bbox["y0"])
            if min(width, height) < MIN_PIN_SIZE_PT or max(width, height) > MAX_CIRCLE_DIAMETER_PT:
                continue
            regions.append(self._pin_record("circle", region["vectors"], region["points"], source="geometry"))
        return self._vote_circle_size(self._dedup(regions))

    def detect_arrow_pins(self, *, use_templates: bool = True) -> list[dict[str, Any]]:
        """Return small filled arrows whose vector boundary is closed."""
        if use_templates:
            templates = self.load().get("arrow", [])
            if templates:
                return self._detect_from_templates("arrow", templates)

        grouped: dict[Any, list[PathBase]] = {}
        for vector in self.vectors:
            if vector.is_dashed or not self._is_filled(vector):
                continue
            grouped.setdefault(self._path_key(vector), []).append(vector)

        regions: list[dict[str, Any]] = []
        for vectors in grouped.values():
            vector_geometries = [
                (vector, geometry)
                for vector in vectors
                if (geometry := self._linear_geometry(vector)) is not None and not geometry.is_empty
            ]
            if not vector_geometries:
                continue
            for polygon in polygonize(
                MultiLineString([geometry for _, geometry in vector_geometries])
            ):
                if not isinstance(polygon, Polygon):
                    continue
                direction = self._arrow_direction(polygon)
                if direction is None:
                    continue
                boundary = polygon.boundary.buffer(POINT_TOLERANCE_PT)
                boundary_vectors = [
                    vector
                    for vector, geometry in vector_geometries
                    if boundary.covers(geometry)
                ]
                if not boundary_vectors:
                    continue
                regions.append(
                    self._pin_record(
                        "arrow",
                        boundary_vectors,
                        [(float(x), float(y)) for x, y in polygon.exterior.coords],
                        source="geometry",
                        direction=direction,
                        polygon=polygon,
                    )
                )
        return self._dedup(regions)

    def detect_wire_mark(self) -> list[dict[str, Any]]:
        """Return the dominant short diagonal mark crossing otherwise unconnected wires."""
        indexed_lines = [
            (vector, LineString(vector.points))
            for vector in self.vectors
            if vector.type == "line"
            and len(vector.points) == 2
            and not vector.is_dashed
            and math.dist(vector.points[0], vector.points[1]) > 1e-9
        ]
        if not indexed_lines:
            return []

        geometries = [geometry for _, geometry in indexed_lines]
        tree = STRtree(geometries)
        candidates: list[dict[str, Any]] = []
        for vector_index, (vector, geometry) in enumerate(indexed_lines):
            length = float(geometry.length)
            if length > MAX_WIRE_MARK_LENGTH_PT:
                continue
            angle = self._undirected_line_angle(vector.points[0], vector.points[1])
            if self._is_axis_angle(angle):
                continue
            if self._endpoint_touches_other_line(vector_index, geometry, geometries, tree):
                continue
            crossing_index = self._center_crossing_line(vector_index, geometry, indexed_lines, tree)
            if crossing_index is None:
                continue
            crossing_vector = indexed_lines[crossing_index][0]
            candidates.append(
                {
                    "category": "wire_mark",
                    "bbox": bbox_to_dict(bbox_from_shapes([vector])),
                    "vectors": [vector],
                    "points": list(vector.points),
                    "polygon": None,
                    "source": "geometry",
                    "length": length,
                    "angle_degrees": angle,
                    "crossing_vector": crossing_vector,
                    "crossing_vector_index": self._source_index_by_id[id(crossing_vector)],
                }
            )
        return self._vote_wire_mark_model(candidates)

    def _endpoint_touches_other_line(
        self,
        vector_index: int,
        geometry: LineString,
        geometries: Sequence[LineString],
        tree: STRtree,
    ) -> bool:
        for x, y in geometry.coords:
            endpoint = ShapelyPoint(float(x), float(y))
            query = endpoint.buffer(WIRE_MARK_ENDPOINT_TOLERANCE_PT)
            for result in tree.query(query, predicate="intersects"):
                other_index = self._tree_result_index(result)
                if other_index == vector_index:
                    continue
                if geometries[other_index].distance(endpoint) <= WIRE_MARK_ENDPOINT_TOLERANCE_PT:
                    return True
        return False

    def _center_crossing_line(
        self,
        vector_index: int,
        geometry: LineString,
        indexed_lines: Sequence[tuple[PathBase, LineString]],
        tree: STRtree,
    ) -> int | None:
        midpoint = geometry.interpolate(0.5, normalized=True)
        query = midpoint.buffer(WIRE_MARK_CENTER_TOLERANCE_PT)
        for result in tree.query(query, predicate="intersects"):
            other_index = self._tree_result_index(result)
            if other_index == vector_index:
                continue
            _, other = indexed_lines[other_index]
            if other.length <= geometry.length:
                continue
            other_points = list(other.coords)
            other_angle = self._undirected_line_angle(other_points[0], other_points[-1])
            if not self._is_axis_angle(other_angle):
                continue
            if other.distance(midpoint) <= WIRE_MARK_CENTER_TOLERANCE_PT:
                return other_index
        return None

    @staticmethod
    def _undirected_line_angle(left: Point, right: Point) -> float:
        return math.degrees(math.atan2(right[1] - left[1], right[0] - left[0])) % 180.0

    @staticmethod
    def _is_axis_angle(angle: float) -> bool:
        return min(abs(angle), abs(angle - 90.0), abs(angle - 180.0)) <= WIRE_MARK_AXIS_TOLERANCE_DEGREES

    @staticmethod
    def _tree_result_index(result: Any) -> int:
        if isinstance(result, Integral):
            return int(result)
        raise TypeError("STRtree index results require shapely>=2.0")

    @staticmethod
    def _vote_wire_mark_model(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(regions) <= 1:
            return regions
        clusters: list[list[dict[str, Any]]] = []
        for region in sorted(regions, key=lambda item: (item["angle_degrees"], item["length"])):
            best_cluster: list[dict[str, Any]] | None = None
            best_distance = math.inf
            for cluster in clusters:
                mean_length = sum(item["length"] for item in cluster) / len(cluster)
                mean_angle = sum(item["angle_degrees"] for item in cluster) / len(cluster)
                length_tolerance = max(POINT_TOLERANCE_PT, mean_length * WIRE_MARK_LENGTH_RELATIVE_TOLERANCE)
                length_distance = abs(region["length"] - mean_length)
                angle_distance = abs(region["angle_degrees"] - mean_angle)
                if length_distance <= length_tolerance and angle_distance <= WIRE_MARK_ANGLE_CLUSTER_TOLERANCE_DEGREES:
                    distance = length_distance / length_tolerance + angle_distance / WIRE_MARK_ANGLE_CLUSTER_TOLERANCE_DEGREES
                    if distance < best_distance:
                        best_cluster = cluster
                        best_distance = distance
            if best_cluster is None:
                clusters.append([region])
            else:
                best_cluster.append(region)
        winner = max(
            clusters,
            key=lambda cluster: (
                len(cluster),
                -max(item["length"] for item in cluster) + min(item["length"] for item in cluster),
            ),
        )
        return winner

    def load(self) -> dict[str, list[list[PathBase]]]:
        """Load normalized pin models from the configured JSON file."""
        empty: dict[str, list[list[PathBase]]] = {kind: [] for kind in self.KINDS}
        if not self.template_path.is_file():
            return empty
        payload = json.loads(self.template_path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) != PIN_TEMPLATE_VERSION:
            raise ValueError(f"Unsupported vector pin template version: {payload.get('version')}")
        raw_templates = payload.get("templates", {})
        for kind in self.KINDS:
            empty[kind] = [
                [PathBase.from_record(record) for record in group]
                for group in raw_templates.get(kind, [])
                if isinstance(group, list) and group
            ]
        return empty

    def save(
        self,
        kind: str,
        shapes: Sequence[PathBase] | Sequence[Sequence[PathBase]],
        *,
        replace: bool = False,
    ) -> Path:
        """Persist one or more pin models after translating them to a local origin."""
        normalized_kind = self._normalize_kind(kind)
        groups = self._coerce_shape_groups(shapes)
        if not groups:
            raise ValueError("At least one pin shape is required")
        templates = self.load()
        records = [[shape.to_dict() for shape in self._normalize_group(group)] for group in groups]
        if replace:
            templates[normalized_kind] = []
        existing_keys = {
            json.dumps([shape.to_dict() for shape in group], sort_keys=True)
            for group in templates[normalized_kind]
        }
        for record in records:
            key = json.dumps(record, sort_keys=True)
            if key not in existing_keys:
                templates[normalized_kind].append([PathBase.from_record(item) for item in record])
                existing_keys.add(key)
        payload = {
            "version": PIN_TEMPLATE_VERSION,
            "templates": {
                name: [[shape.to_dict() for shape in group] for group in templates[name]]
                for name in self.KINDS
            },
        }
        self.template_path.parent.mkdir(parents=True, exist_ok=True)
        self.template_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return self.template_path

    def save_detected(self, kind: str, *, replace: bool = False) -> Path:
        """Run geometry detection and persist every distinct detected model."""
        pins = self.detect(kind, use_templates=False)
        return self.save(kind, [pin["vectors"] for pin in pins], replace=replace)

    def _detect_from_templates(
        self,
        kind: str,
        templates: Sequence[Sequence[PathBase]],
    ) -> list[dict[str, Any]]:
        matcher = VectorMatcher(self.vector_base, page_height_pt=self.page_height_pt)
        regions: list[dict[str, Any]] = []
        for template in templates:
            for match in matcher.match_pattern(
                template,
                tolerance=0.5,
                scale_range=(0.98, 1.02),
                rotation_degrees=(0, 90, 180, 270),
                query_slack=0.02,
            ):
                indices = {
                    int(record["index"])
                    for record in match.get("vectors", [])
                    if record.get("index") is not None
                }
                vectors = [
                    vector
                    for vector in self.vectors
                    if self._source_index_by_id[id(vector)] in indices
                ]
                if not vectors:
                    continue
                if kind == "arrow" and not all(self._is_filled(vector) for vector in vectors):
                    continue
                polygon = None
                direction = None
                if kind == "arrow":
                    arrow_geometry = self._arrow_geometry_from_vectors(vectors)
                    if arrow_geometry is None:
                        continue
                    polygon, direction = arrow_geometry
                bbox = match["bbox_mupdf"]
                points = (
                    [(float(x), float(y)) for x, y in polygon.exterior.coords]
                    if polygon is not None
                    else [
                        (float(bbox["x0"]), float(bbox["y0"])),
                        (float(bbox["x1"]), float(bbox["y0"])),
                        (float(bbox["x1"]), float(bbox["y1"])),
                        (float(bbox["x0"]), float(bbox["y1"])),
                        (float(bbox["x0"]), float(bbox["y0"])),
                    ]
                )
                regions.append(
                    self._pin_record(
                        kind,
                        vectors,
                        points,
                        source="template",
                        direction=direction,
                        polygon=polygon,
                    )
                )
        return self._dedup(regions)

    def _pin_record(
        self,
        kind: str,
        vectors: Sequence[PathBase],
        points: Iterable[Point],
        *,
        source: str,
        direction: str | None = None,
        polygon: Polygon | None = None,
    ) -> dict[str, Any]:
        record = {
            "category": f"pin_{kind}",
            "bbox": bbox_to_dict(bbox_from_shapes(vectors)),
            "vectors": list(vectors),
            "points": list(points),
            "polygon": polygon,
            "source": source,
        }
        if direction is not None:
            record["direction"] = direction
        return record

    @staticmethod
    def _linear_geometry(vector: PathBase) -> LineString | None:
        points = [(float(x), float(y)) for x, y in vector.points]
        if vector.type == "line" and len(points) == 2:
            return LineString(points)
        if vector.type in {"rect", "quad"} and len(points) >= 3:
            return LineString([*points, points[0]])
        if len(points) >= 3 and math.dist(points[0], points[-1]) <= POINT_TOLERANCE_PT:
            return LineString(points)
        return None

    @staticmethod
    def _is_filled(vector: PathBase) -> bool:
        return vector.path_meta_value("fill") is not None

    @staticmethod
    def _path_key(vector: PathBase) -> Any:
        path_index = vector.path_meta_value("path_index")
        if path_index is not None:
            return ("path_index", path_index)
        return (
            "fallback",
            vector.path_meta_value("seqno"),
            vector.path_meta_value("path_type"),
        )

    @staticmethod
    def _arrow_direction(polygon: Polygon) -> str | None:
        coords: list[Point] = []
        for x, y in polygon.exterior.coords:
            point = (float(x), float(y))
            if not coords or math.dist(coords[-1], point) > POINT_TOLERANCE_PT:
                coords.append(point)
        if len(coords) > 1 and math.dist(coords[0], coords[-1]) <= POINT_TOLERANCE_PT:
            coords.pop()
        if len(coords) != 3 or polygon.area < MIN_ARROW_AREA_PT2:
            return None
        x0, y0, x1, y1 = polygon.bounds
        if min(x1 - x0, y1 - y0) < MIN_PIN_SIZE_PT or max(x1 - x0, y1 - y0) > MAX_ARROW_SIZE_PT:
            return None

        candidates: list[tuple[float, int, float, float]] = []
        for apex_index, apex in enumerate(coords):
            base = [point for index, point in enumerate(coords) if index != apex_index]
            left_leg = math.dist(apex, base[0])
            right_leg = math.dist(apex, base[1])
            mean_leg = (left_leg + right_leg) / 2.0
            if mean_leg <= 1e-9:
                continue
            leg_error = abs(left_leg - right_leg) / mean_leg
            base_length = math.dist(base[0], base[1])
            candidates.append((leg_error, apex_index, mean_leg, base_length))
        if not candidates:
            return None

        leg_error, apex_index, mean_leg, base_length = min(candidates)
        if leg_error > ISOSCELES_LEG_ERROR_RATIO:
            return None
        if abs(mean_leg - base_length) / max(mean_leg, base_length, 1e-9) <= EQUILATERAL_SIDE_ERROR_RATIO:
            return None

        apex = coords[apex_index]
        base = [point for index, point in enumerate(coords) if index != apex_index]
        base_midpoint = ((base[0][0] + base[1][0]) / 2.0, (base[0][1] + base[1][1]) / 2.0)
        dx = apex[0] - base_midpoint[0]
        dy = apex[1] - base_midpoint[1]
        dominant = max(abs(dx), abs(dy))
        if dominant <= 1e-9 or min(abs(dx), abs(dy)) / dominant > DIRECTION_AXIS_ERROR_RATIO:
            return None
        if abs(dx) > abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    @classmethod
    def _arrow_geometry_from_vectors(
        cls,
        vectors: Sequence[PathBase],
    ) -> tuple[Polygon, str] | None:
        geometries = [
            geometry
            for vector in vectors
            if (geometry := cls._linear_geometry(vector)) is not None and not geometry.is_empty
        ]
        if not geometries:
            return None
        candidates = [
            (polygon, direction)
            for polygon in polygonize(MultiLineString(geometries))
            if isinstance(polygon, Polygon)
            if (direction := cls._arrow_direction(polygon)) is not None
        ]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _normalize_group(shapes: Sequence[PathBase]) -> list[PathBase]:
        x0, y0, _, _ = bbox_from_shapes(shapes)
        normalized: list[PathBase] = []
        for shape in shapes:
            inner = shape.inner_value
            normalized.append(
                PathBase(
                    type=shape.type,
                    code=str(inner.get("code", "")),
                    points=[(float(x) - x0, float(y) - y0) for x, y in shape.points],
                    path_meta=inner.get("path_meta"),
                )
            )
        return normalized

    @staticmethod
    def _coerce_shape_groups(
        shapes: Sequence[PathBase] | Sequence[Sequence[PathBase]],
    ) -> list[list[PathBase]]:
        values = list(shapes)
        if not values:
            return []
        if isinstance(values[0], PathBase):
            return [[shape for shape in values if isinstance(shape, PathBase)]]
        return [list(group) for group in values if group]

    @classmethod
    def _normalize_kind(cls, kind: str) -> str:
        normalized = str(kind).strip().lower()
        aliases = {"circles": "circle", "triangle": "arrow", "triangles": "arrow", "arrows": "arrow"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in cls.KINDS:
            raise ValueError(f"Unsupported vector pin kind: {kind}")
        return normalized

    @staticmethod
    def _dedup(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[int, int, int, int]] = set()
        result: list[dict[str, Any]] = []
        for region in sorted(regions, key=lambda item: (item["bbox"]["y0"], item["bbox"]["x0"])):
            bbox = region["bbox"]
            key = tuple(round(float(bbox[name]) / POINT_TOLERANCE_PT) for name in ("x0", "y0", "x1", "y1"))
            if key in seen:
                continue
            seen.add(key)
            result.append(region)
        return result

    @staticmethod
    def _vote_circle_size(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(regions) <= 1:
            return regions

        sized_regions = []
        for region in regions:
            bbox = region["bbox"]
            diameter = (
                float(bbox["x1"] - bbox["x0"])
                + float(bbox["y1"] - bbox["y0"])
            ) / 2.0
            sized_regions.append((diameter, region))
        sized_regions.sort(key=lambda item: item[0])

        clusters: list[list[tuple[float, dict[str, Any]]]] = []
        for diameter, region in sized_regions:
            best_cluster: list[tuple[float, dict[str, Any]]] | None = None
            best_distance = math.inf
            for cluster in clusters:
                mean_diameter = sum(item[0] for item in cluster) / len(cluster)
                tolerance = max(
                    CIRCLE_SIZE_ABSOLUTE_TOLERANCE_PT,
                    mean_diameter * CIRCLE_SIZE_RELATIVE_TOLERANCE,
                )
                distance = abs(diameter - mean_diameter)
                if distance <= tolerance and distance < best_distance:
                    best_cluster = cluster
                    best_distance = distance
            if best_cluster is None:
                clusters.append([(diameter, region)])
            else:
                best_cluster.append((diameter, region))

        def cluster_rank(cluster: list[tuple[float, dict[str, Any]]]) -> tuple[int, float, float]:
            diameters = [item[0] for item in cluster]
            mean_diameter = sum(diameters) / len(diameters)
            dispersion = max(diameters) - min(diameters)
            return (len(cluster), -dispersion, -mean_diameter)

        winner = max(clusters, key=cluster_rank)
        return [region for _, region in winner]
