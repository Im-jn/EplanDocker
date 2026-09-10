"""Vector entity graph and union-find state for a VectorBase."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Integral
from typing import Any, Iterable

from shapely.geometry import LineString, MultiLineString, Point as ShapelyPoint, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.utils import bbox_to_dict, coerce_bbox


BBox = tuple[float, float, float, float]
Point = tuple[float, float]
POINT_TOLERANCE_PT = PARSER_CONFIG.geometry.topology_tolerance_pt
CURVE_SEGMENTS = PARSER_CONFIG.geometry.curve_segments
CONTAINMENT_TOLERANCE_PT = (
    PARSER_CONFIG.vector_geometry.entity_containment_tolerance_pt
)
NEARBY_MERGE_GAP_PT = PARSER_CONFIG.vector_geometry.entity_nearby_merge_gap_pt
MIN_CONTAINMENT_FACE_AREA_PT2 = (
    PARSER_CONFIG.vector_geometry.entity_min_containment_face_area_pt2
)


@dataclass
class VectorNode:
    """A vector registered in the entity graph."""

    vector: PathBase
    point_ids: list[int]
    edge_ids: set[tuple[int, int]] = field(default_factory=set)


@dataclass
class EntityRecord:
    """Per-root cached entity state."""

    vector_ids: set[int] = field(default_factory=set)
    point_ids: set[int] = field(default_factory=set)
    bbox: BBox | None = None


class VectorDisjointSet:
    """Union-find backed vector entity graph.

    This class owns a vector set, deduplicated point set, graph edges,
    and entity bboxes. Vectors are noded with Shapely before graph
    registration, and closed faces are computed from the current graph when
    requested.
    """

    def __init__(
        self,
        vector_base: VectorBase,
        *,
        merge_contained: bool = True,
        merge_nearby: bool = True,
    ):
        self.vector_base = vector_base
        self.page_vectors = vector_base.vectors
        self.raw_entity_count = 0
        self.containment_merge_count = 0
        self.nearby_merge_count = 0

        self._reset_state()
        self._build_from_vectors()
        self.raw_entity_count = len(self.groups())
        if merge_contained:
            self.containment_merge_count = self.merge_contained_entities()
        if merge_nearby:
            self.nearby_merge_count = self.merge_nearby_entities()

    def _reset_state(self) -> None:
        self.parent: list[int] = []
        self.rank: list[int] = []
        self.vectors: list[VectorNode] = []
        self.points: list[Point] = []
        self.point_to_vectors: dict[int, set[int]] = {}
        self.point_key_to_id: dict[tuple[int, int], int] = {}
        self.graph: dict[int, set[int]] = {}
        self.edge_to_vectors: dict[tuple[int, int], set[int]] = {}
        self.entities: dict[int, EntityRecord] = {}

    def _build_from_vectors(self) -> None:
        vectors = list(self.page_vectors)
        if not vectors:
            return

        geometries = [self._vector_geometry(vector) for vector in vectors]
        self._initialize_vector_nodes(vectors)
        noded_geometry = unary_union(MultiLineString(geometries))
        geom_to_index = {id(geom): index for index, geom in enumerate(geometries)}
        tree = STRtree(geometries)

        for left, right in self._segments_from_geometry(noded_geometry):
            segment = LineString([left, right])
            source_ids = self._source_vectors_for_segment(segment, geometries, tree, geom_to_index)
            if not source_ids:
                continue
            self._register_noded_segment(left, right, source_ids)

        for root in list(self.groups()):
            self._refresh_root(root)

    def _initialize_vector_nodes(self, vectors: list[PathBase]) -> None:
        for vector in vectors:
            vector_id = self._add_parent()
            self.vectors.append(VectorNode(vector=vector, point_ids=[]))
            self.entities[vector_id] = EntityRecord(
                vector_ids={vector_id},
                point_ids=set(),
                bbox=coerce_bbox(vector.bbox),
            )

    def _register_noded_segment(self, left: Point, right: Point, source_ids: set[int]) -> None:
        if self._same_point(left, right):
            return

        left_id = self._get_or_add_point(left)
        right_id = self._get_or_add_point(right)
        if left_id == right_id:
            return

        touched_vectors: set[int] = set(source_ids)
        touched_vectors.update(self.point_to_vectors.get(left_id, set()))
        touched_vectors.update(self.point_to_vectors.get(right_id, set()))

        edge_id = self._edge_id(left_id, right_id)
        touched_vectors.update(self.edge_to_vectors.get(edge_id, set()))
        self.edge_to_vectors.setdefault(edge_id, set()).update(source_ids)
        self.graph.setdefault(left_id, set()).add(right_id)
        self.graph.setdefault(right_id, set()).add(left_id)

        for vector_id in source_ids:
            node = self.vectors[vector_id]
            node.point_ids.extend([left_id, right_id])
            node.edge_ids.add(edge_id)
            self.point_to_vectors.setdefault(left_id, set()).add(vector_id)
            self.point_to_vectors.setdefault(right_id, set()).add(vector_id)

        source_id = min(source_ids)
        for touched_id in touched_vectors:
            if touched_id != source_id:
                self.union(source_id, touched_id)

    def find(self, item: int) -> int:
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: int, right: int) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False

        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1

        kept = self.entities.setdefault(left_root, EntityRecord())
        dropped = self.entities.pop(right_root, EntityRecord())
        kept.vector_ids.update(dropped.vector_ids)
        kept.point_ids.update(dropped.point_ids)
        kept.bbox = self._merge_bboxes(kept.bbox, dropped.bbox)
        return True

    def compressed_parents(self) -> list[int]:
        return [self.find(index) for index in range(len(self.parent))]

    def groups(self) -> dict[int, list[int]]:
        grouped: dict[int, list[int]] = {}
        for index in range(len(self.parent)):
            grouped.setdefault(self.find(index), []).append(index)
        return grouped

    def merge_contained_entities(self) -> int:
        """Merge entities whose vector geometry is touched by another entity's closed faces."""
        merge_count = 0
        vector_geometries: dict[int, Any] = {}
        vector_bboxes = [coerce_bbox(node.vector.bbox) for node in self.vectors]
        roots = sorted(
            self.groups(),
            key=lambda root: self._bbox_area(self.entities[root].bbox or (0.0, 0.0, 0.0, 0.0)),
            reverse=True,
        )

        for original_root in roots:
            outer_root = self.find(original_root)
            if outer_root != original_root or outer_root not in self.entities:
                continue
            outer = self.entities[outer_root]
            if outer.bbox is None:
                continue
            if not self._entity_bbox_has_foreign_vectors(outer_root, outer.bbox, vector_bboxes):
                continue

            active_polygons: list[Polygon] = []
            pending_polygons: list[Polygon] = []
            for polygon in self._merged_face_polygons(
                outer_root,
            ):
                if self._add_uncovered_polygon(active_polygons, polygon):
                    pending_polygons.append(polygon)

            while pending_polygons:
                polygon = pending_polygons.pop(0)
                cover_geometry = (
                    polygon.buffer(CONTAINMENT_TOLERANCE_PT)
                    if CONTAINMENT_TOLERANCE_PT
                    else polygon
                )
                absorbed_this_polygon = False
                for inner_root in list(self.groups()):
                    outer_root = self.find(outer_root)
                    inner_root = self.find(inner_root)
                    if inner_root == outer_root or inner_root not in self.entities:
                        continue
                    if self._entity_has_vector_in_polygon(
                        inner_root,
                        cover_geometry,
                        vector_bboxes,
                        vector_geometries,
                    ):
                        absorbed_polygons = self._merged_face_polygons(
                            inner_root,
                        )
                        if self._absorb_entity(outer_root, inner_root):
                            merge_count += 1
                            absorbed_this_polygon = True
                            for absorbed_polygon in absorbed_polygons:
                                if self._add_uncovered_polygon(active_polygons, absorbed_polygon):
                                    pending_polygons.append(absorbed_polygon)
                if absorbed_this_polygon:
                    outer_root = self.find(outer_root)
        return merge_count

    def merge_nearby_entities(self) -> int:
        """Merge entities when member vector bboxes are nearby."""
        if NEARBY_MERGE_GAP_PT == 0 or len(self.vectors) <= 1:
            return 0

        geometries = [
            box(*self._expand_bbox(coerce_bbox(node.vector.bbox), NEARBY_MERGE_GAP_PT))
            for node in self.vectors
        ]
        geom_to_index = {id(geom): index for index, geom in enumerate(geometries)}
        tree = STRtree(geometries)

        merge_count = 0
        for left_id, left_geometry in enumerate(geometries):
            left_root = self.find(left_id)
            for candidate in tree.query(left_geometry, predicate="intersects"):
                right_id = self._index_from_tree_result(candidate, geom_to_index)
                if right_id <= left_id:
                    continue
                right_root = self.find(right_id)
                if left_root == right_root:
                    continue
                if self._bbox_gap(
                    coerce_bbox(self.vectors[left_id].vector.bbox),
                    coerce_bbox(self.vectors[right_id].vector.bbox),
                ) <= NEARBY_MERGE_GAP_PT:
                    if self.union(left_root, right_root):
                        merge_count += 1
                        left_root = self.find(left_root)

        return merge_count

    def entity_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for root, vector_ids in sorted(self.groups().items(), key=lambda item: item[1][0]):
            root = self.find(root)
            self._refresh_root(root)
            entity = self.entities[root]
            polygon = self.entity_merged_face(root)
            records.append({
                "category": "entity",
                "bbox": bbox_to_dict(entity.bbox or (0.0, 0.0, 0.0, 0.0)),
                "vectors": [
                    self.vectors[vector_id].vector
                    for vector_id in sorted(vector_ids)
                ],
                "points": [
                    self.points[point_id]
                    for point_id in sorted(entity.point_ids)
                ],
                "polygon": polygon,
            })
        return records

    def result(self) -> list[dict[str, Any]]:
        """Return compact entity geometry records."""
        return self.entity_records()

    def entity_faces(self, root: int) -> list[Polygon]:
        """Compute the current closed faces for one entity."""
        root = self.find(root)
        entity = self.entities.get(root)
        if entity is None:
            return []
        return self._detect_faces(entity.vector_ids)

    def entity_merged_face(self, root: int) -> Any | None:
        """Compute the current merged face geometry for one entity."""
        faces = self.entity_faces(root)
        if not faces:
            return None
        merged = unary_union(faces)
        return merged if not merged.is_empty else None

    def _add_parent(self) -> int:
        index = len(self.parent)
        self.parent.append(index)
        self.rank.append(0)
        return index

    def _absorb_entity(self, target_root: int, source_root: int) -> bool:
        target_root = self.find(target_root)
        source_root = self.find(source_root)
        if target_root == source_root:
            return False

        self.parent[source_root] = target_root
        self.rank[target_root] = max(self.rank[target_root], self.rank[source_root] + 1)
        kept = self.entities.setdefault(target_root, EntityRecord())
        dropped = self.entities.pop(source_root, EntityRecord())
        kept.vector_ids.update(dropped.vector_ids)
        kept.point_ids.update(dropped.point_ids)
        kept.bbox = self._merge_bboxes(kept.bbox, dropped.bbox)
        return True

    def _refresh_root(self, root: int) -> None:
        root = self.find(root)
        vector_ids = set(self.groups().get(root, []))
        point_ids: set[int] = set()
        bbox_value: BBox | None = None
        for vector_id in vector_ids:
            node = self.vectors[vector_id]
            point_ids.update(node.point_ids)
            node_bbox = node.vector.bbox
            bbox_value = self._merge_bboxes(bbox_value, coerce_bbox(node_bbox))

        self.entities[root] = EntityRecord(
            vector_ids=vector_ids,
            point_ids=point_ids,
            bbox=bbox_value,
        )

    def _detect_faces(self, vector_ids: set[int]) -> list[Polygon]:
        lines: list[LineString] = []
        for edge_id, edge_vectors in self.edge_to_vectors.items():
            if not edge_vectors.intersection(vector_ids):
                continue
            left_id, right_id = edge_id
            left = self.points[left_id]
            right = self.points[right_id]
            if left != right:
                lines.append(LineString([left, right]))

        if not lines:
            return []

        noded = unary_union(MultiLineString(lines))
        faces = [
            polygon
            for polygon in polygonize(noded)
            if isinstance(polygon, Polygon)
        ]
        faces.sort(key=lambda polygon: (polygon.bounds[0], polygon.bounds[1], -polygon.area))
        return faces

    def _closed_entity_geometry(self, root: int) -> Any | None:
        root = self.find(root)
        return self.entity_merged_face(root)

    def _merged_face_polygons(
        self,
        root: int,
    ) -> list[Polygon]:
        geometry = self._closed_entity_geometry(root)
        polygons = [
            polygon
            for polygon in self._polygonal_parts(geometry)
            if polygon.area >= MIN_CONTAINMENT_FACE_AREA_PT2
        ]
        polygons.sort(key=lambda polygon: polygon.area, reverse=True)
        return polygons

    def _polygonal_parts(self, geometry: Any | None) -> list[Polygon]:
        if geometry is None or geometry.is_empty:
            return []
        if isinstance(geometry, Polygon):
            return [geometry]
        return [
            part
            for part in getattr(geometry, "geoms", [])
            if isinstance(part, Polygon) and not part.is_empty
        ]

    def _entity_bbox_has_foreign_vectors(
        self,
        root: int,
        bbox: BBox,
        vector_bboxes: list[BBox],
    ) -> bool:
        root = self.find(root)
        for vector_id, vector_bbox in enumerate(vector_bboxes):
            if self.find(vector_id) == root:
                continue
            if self._bbox_intersects(bbox, vector_bbox):
                return True
        return False

    def _entity_has_vector_in_polygon(
        self,
        root: int,
        polygon: Any,
        vector_bboxes: list[BBox],
        vector_geometries: dict[int, Any],
    ) -> bool:
        root = self.find(root)
        polygon_bbox = coerce_bbox(polygon.bounds)
        entity = self.entities.get(root)
        if entity is None:
            return False
        for vector_id in entity.vector_ids:
            if not self._bbox_intersects(polygon_bbox, vector_bboxes[vector_id]):
                continue
            geometry = vector_geometries.get(vector_id)
            if geometry is None:
                geometry = self._vector_geometry(self.vectors[vector_id].vector)
                vector_geometries[vector_id] = geometry
            if polygon.intersects(geometry):
                return True
        return False

    @staticmethod
    def _add_uncovered_polygon(polygons: list[Polygon], candidate: Polygon) -> bool:
        if any(existing.covers(candidate) for existing in polygons):
            return False
        polygons[:] = [existing for existing in polygons if not candidate.covers(existing)]
        polygons.append(candidate)
        return True

    def _vector_geometry(self, vector: PathBase) -> Any:
        points = self._vector_path_points(vector)
        if len(points) >= 2:
            return LineString(points)
        return box(*coerce_bbox(vector.bbox)).boundary

    @classmethod
    def _segments_from_geometry(cls, geometry: Any) -> list[tuple[Point, Point]]:
        if geometry.is_empty:
            return []
        geom_type = geometry.geom_type
        if geom_type in {"LineString", "LinearRing"}:
            coords = list(geometry.coords)
            return [
                (
                    (float(left[0]), float(left[1])),
                    (float(right[0]), float(right[1])),
                )
                for left, right in zip(coords, coords[1:])
            ]
        if geom_type.startswith("Multi") or geom_type == "GeometryCollection":
            segments: list[tuple[Point, Point]] = []
            for part in geometry.geoms:
                segments.extend(cls._segments_from_geometry(part))
            return segments
        return []

    def _source_vectors_for_segment(
        self,
        segment: LineString,
        geometries: list[Any],
        tree: STRtree,
        geom_to_index: dict[int, int],
    ) -> set[int]:
        source_ids: set[int] = set()
        for candidate in tree.query(segment, predicate="intersects"):
            vector_id = self._index_from_tree_result(candidate, geom_to_index)
            geometry = geometries[vector_id]
            if self._segment_lies_on_geometry(segment, geometry):
                source_ids.add(vector_id)
        return source_ids

    def _segment_lies_on_geometry(self, segment: LineString, geometry: Any) -> bool:
        coords = list(segment.coords)
        if len(coords) < 2:
            return False
        probes = [
            ShapelyPoint(coords[0]),
            segment.interpolate(0.5, normalized=True),
            ShapelyPoint(coords[-1]),
        ]
        return all(geometry.distance(point) <= POINT_TOLERANCE_PT for point in probes)

    @staticmethod
    def _index_from_tree_result(candidate: Any, geom_to_index: dict[int, int]) -> int:
        if isinstance(candidate, Integral):
            return int(candidate)
        index = geom_to_index.get(id(candidate))
        if index is None:
            raise KeyError("STRtree returned an unknown geometry")
        return index

    def _get_or_add_point(self, point: Point) -> int:
        key = self._point_key(point)
        existing = self.point_key_to_id.get(key)
        if existing is not None:
            return existing

        point_id = len(self.points)
        self.points.append(point)
        self.point_key_to_id[key] = point_id
        self.graph.setdefault(point_id, set())
        self.point_to_vectors.setdefault(point_id, set())
        return point_id

    def _vector_path_points(self, vector: PathBase) -> list[Point]:
        points = [self._coerce_point(point) for point in vector.points]
        if vector.type == "curve" and len(points) == 4:
            return self._sample_cubic(points)
        if vector.type in {"rect", "quad"} and points:
            return [*points, points[0]]
        if len(points) >= 2:
            return points
        bbox_value = coerce_bbox(vector.bbox)
        return [(bbox_value[0], bbox_value[1]), (bbox_value[2], bbox_value[3])]

    def _sample_cubic(self, points: list[Point]) -> list[Point]:
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
        return samples

    def _same_point(self, left: Point, right: Point) -> bool:
        return math.dist(left, right) <= POINT_TOLERANCE_PT

    def _point_key(self, point: Point) -> tuple[int, int]:
        return (
            int(round(point[0] / POINT_TOLERANCE_PT)),
            int(round(point[1] / POINT_TOLERANCE_PT)),
        )

    @staticmethod
    def _coerce_point(point: Any) -> Point:
        return (float(point[0]), float(point[1]))

    @staticmethod
    def _edge_id(left_id: int, right_id: int) -> tuple[int, int]:
        return (left_id, right_id) if left_id < right_id else (right_id, left_id)

    @staticmethod
    def _bbox_from_points(points: Iterable[Point]) -> BBox:
        pts = list(points)
        xs = [point[0] for point in pts]
        ys = [point[1] for point in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def _bbox_from_point_ids(self, point_ids: Iterable[int]) -> BBox:
        return self._bbox_from_points(self.points[point_id] for point_id in point_ids)

    @staticmethod
    def _merge_bboxes(left: BBox | None, right: BBox | None) -> BBox | None:
        if left is None:
            return right
        if right is None:
            return left
        return (
            min(left[0], right[0]),
            min(left[1], right[1]),
            max(left[2], right[2]),
            max(left[3], right[3]),
        )

    @staticmethod
    def _expand_bbox(bbox: BBox, padding: float) -> BBox:
        return (
            bbox[0] - padding,
            bbox[1] - padding,
            bbox[2] + padding,
            bbox[3] + padding,
        )

    @staticmethod
    def _bbox_gap(left: BBox, right: BBox) -> float:
        dx = max(right[0] - left[2], left[0] - right[2], 0.0)
        dy = max(right[1] - left[3], left[1] - right[3], 0.0)
        return math.hypot(dx, dy)

    @staticmethod
    def _bbox_area(bbox: BBox) -> float:
        return max(bbox[2] - bbox[0], 0.0) * max(bbox[3] - bbox[1], 0.0)

    @staticmethod
    def _bbox_intersects(left: BBox, right: BBox, *, tolerance: float = 0.0) -> bool:
        return not (
            left[2] < right[0] - tolerance
            or left[0] > right[2] + tolerance
            or left[3] < right[1] - tolerance
            or left[1] > right[3] + tolerance
        )
