"""Detect the drawing/content area on a PDF page from page vectors.

The content bbox is derived from the page's outermost frame: a raw union-find
(no nearby/containment merges) groups the page vectors, the entity holding the
outermost frame is the one with the largest bbox, its member vectors are
slightly extended at both ends so T-junctions reliably cross, and the largest
closed face built from them yields the content bbox.
"""

from __future__ import annotations

import math
from typing import Any

from shapely.geometry import LineString, MultiLineString, Polygon, box
from shapely.ops import polygonize, unary_union

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.tools.vector_entity import VectorDisjointSet
from pdf_parser.utils import bbox_to_dict, coerce_bbox

from .page_class import PathBase, TextBase, VectorBase
from .pdf_page import PageData


BBox = tuple[float, float, float, float]
Point = tuple[float, float]

VECTOR_EXTEND_PT = PARSER_CONFIG.split_page.vector_extend_pt
CURVE_SEGMENTS = PARSER_CONFIG.geometry.curve_segments
MIN_CONTENT_INSET_PT = PARSER_CONFIG.split_page.min_content_inset_pt
PAGE_FRAME_EDGE_TOLERANCE_RATIO = PARSER_CONFIG.split_page.page_frame_edge_tolerance_ratio
CONTENT_EDGE_VECTOR_TOLERANCE_PT = PARSER_CONFIG.split_page.content_edge_vector_tolerance_pt
MIN_FRAME_VECTOR_SPAN_RATIO = PARSER_CONFIG.split_page.min_frame_vector_span_ratio


class SplitPageDetector:
    """Estimate the useful drawing area from the page's outermost frame."""

    def __init__(
        self,
        *,
        extend_length: float = VECTOR_EXTEND_PT,
        edge_tolerance_ratio: float = PAGE_FRAME_EDGE_TOLERANCE_RATIO,
        min_content_inset: float = MIN_CONTENT_INSET_PT,
    ):
        if extend_length < 0:
            raise ValueError("extend_length must be non-negative")
        if edge_tolerance_ratio < 0:
            raise ValueError("edge_tolerance_ratio must be non-negative")
        if min_content_inset < 0:
            raise ValueError("min_content_inset must be non-negative")
        self.extend_length = float(extend_length)
        self.edge_tolerance_ratio = float(edge_tolerance_ratio)
        self.min_content_inset = float(min_content_inset)

    def detect(self, page_data: PageData) -> dict[str, Any]:
        page_bbox = page_data["page_bbox"]
        content_bbox = self.detect_content_bbox(page_data)
        info_bbox = self.detect_info_bbox(page_data, content_bbox)
        return {
            "pdf_path": str(page_data.get("pdf_path", "")),
            "page_number": int(page_data["page_number"]),
            "page_bbox": bbox_to_dict(page_bbox),
            "content_bbox": content_bbox,
            "info_bbox": info_bbox,
        }

    def detect_content_bbox(self, page_data: PageData) -> dict[str, float]:
        """Return the inferred content bbox in PyMuPDF page coordinates."""
        vector_base = self._vector_base(page_data)
        page_bbox = coerce_bbox(page_data["page_bbox"])

        frame_vectors = self._frame_entity_vectors(vector_base)
        if not frame_vectors:
            return bbox_to_dict(self._fallback_bbox(page_bbox))

        largest_face = self._largest_face(frame_vectors)
        if largest_face is None:
            return bbox_to_dict(self._fallback_bbox(page_bbox))

        return bbox_to_dict(coerce_bbox(largest_face.bounds))

    def detect_info_bbox(
        self,
        page_data: PageData,
        content_bbox: Any,
    ) -> dict[str, float] | None:
        """Return the page area below the detected content bbox."""
        px0, py0, px1, py1 = coerce_bbox(page_data["page_bbox"])
        _, _, _, content_y1 = coerce_bbox(content_bbox)
        info_y0 = max(content_y1, py0)

        if info_y0 >= py1:
            return None
        return bbox_to_dict((px0, info_y0, px1, py1))

    def region_content(
        self,
        page_data: PageData,
        content_bbox: Any | None = None,
        *,
        exclude_frame_vectors: bool = True,
    ) -> dict[str, Any]:
        """Return vectors and text filtered to the content area."""
        content_bbox_value = coerce_bbox(content_bbox or self.detect_content_bbox(page_data))
        source_base = self._vector_base(page_data)
        vectors = []
        source_indices = []
        for vector_id, vector in enumerate(source_base.vectors):
            if not self._vector_intersects_bbox(vector, content_bbox_value):
                continue
            if exclude_frame_vectors and self._is_content_frame_vector(vector, content_bbox_value):
                continue
            clipped_vectors = (
                [vector]
                if exclude_frame_vectors
                else self._clip_vector_to_bbox(vector, content_bbox_value)
            )
            vectors.extend(clipped_vectors)
            source_indices.extend([source_base.source_index(vector_id)] * len(clipped_vectors))
        text_base = self._text_base(page_data)
        return {
            "vectors": VectorBase(vectors, source_indices=source_indices),
            "text": TextBase(text_base.in_region(box(*content_bbox_value))),
        }

    # -- frame entity + face detection --------------------------------------

    def _frame_entity_vectors(self, vector_base: VectorBase) -> list[PathBase]:
        """Group vectors with a raw union-find and return the outermost frame's members."""
        if not vector_base.vectors:
            return []

        entities = VectorDisjointSet(
            vector_base,
            merge_contained=False,
            merge_nearby=False,
        )

        best_root: int | None = None
        best_area = -1.0
        for root in entities.groups():
            bbox = entities.entities[root].bbox
            if bbox is None:
                continue
            area = self._bbox_area(bbox)
            if area > best_area:
                best_area = area
                best_root = root

        if best_root is None:
            return []
        return [
            entities.vectors[vector_id].vector
            for vector_id in entities.groups()[best_root]
        ]

    def _largest_face(self, vectors: list[PathBase]) -> Polygon | None:
        lines = [
            LineString(self._extend_polyline(self._polyline_points(vector)))
            for vector in vectors
        ]
        lines = [line for line in lines if not line.is_empty]
        if not lines:
            return None

        noded = unary_union(MultiLineString(lines))
        faces = [
            polygon
            for polygon in polygonize(noded)
            if isinstance(polygon, Polygon) and not polygon.is_empty
        ]
        if not faces:
            return None
        return max(faces, key=lambda polygon: polygon.area)

    def _extend_polyline(self, points: list[Point]) -> list[Point]:
        """Extend the two free ends outward so T-junctions reliably cross."""
        if len(points) < 2 or self.extend_length <= 0:
            return points
        if math.dist(points[0], points[-1]) <= 1e-9:
            return points  # closed ring: no free endpoints to extend
        extended = list(points)
        extended[0] = self._extend_point(extended[1], extended[0])
        extended[-1] = self._extend_point(extended[-2], extended[-1])
        return extended

    def _extend_point(self, origin: Point, tip: Point) -> Point:
        dx = tip[0] - origin[0]
        dy = tip[1] - origin[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            return tip
        scale = self.extend_length / length
        return (tip[0] + dx * scale, tip[1] + dy * scale)

    def _polyline_points(self, vector: PathBase) -> list[Point]:
        points = [(float(point[0]), float(point[1])) for point in vector.points]
        if vector.type == "curve" and len(points) == 4:
            return self._sample_cubic(points)
        if vector.type in {"rect", "quad"} and points:
            return [*points, points[0]]
        if len(points) >= 2:
            return points
        x0, y0, x1, y1 = coerce_bbox(vector.bbox)
        return [(x0, y0), (x1, y1)]

    @staticmethod
    def _sample_cubic(points: list[Point]) -> list[Point]:
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

    # -- content vector filtering -------------------------------------------

    @staticmethod
    def _vector_base(page_data: PageData) -> VectorBase:
        vectors = page_data["vectors"]
        if isinstance(vectors, VectorBase):
            return vectors
        raise TypeError("page_data['vectors'] must be a VectorBase")

    @staticmethod
    def _text_base(page_data: PageData) -> TextBase:
        texts = page_data["texts"]
        if isinstance(texts, TextBase):
            return texts
        if isinstance(texts, list):
            return TextBase(texts)
        raise TypeError("page_data['texts'] must be a TextBase")

    @staticmethod
    def _vector_intersects_bbox(vector: PathBase, bbox: BBox) -> bool:
        return SplitPageDetector._vector_geometry(vector).intersects(box(*bbox))

    @staticmethod
    def _clip_vector_to_bbox(vector: PathBase, bbox: BBox) -> list[PathBase]:
        region = box(*bbox)
        geometry = SplitPageDetector._vector_geometry(vector)
        if geometry.is_empty or not geometry.intersects(region):
            return []
        if region.covers(geometry):
            return [vector]

        clipped = geometry.intersection(region)
        parts = SplitPageDetector._line_parts(clipped)
        path_meta = vector.inner_value["path_meta"]
        return [
            PathBase(
                type="line",
                points=[(float(x), float(y)) for x, y in part.coords],
                path_meta=path_meta,
            )
            for part in parts
            if not part.is_empty and float(part.length) > 0
        ]

    @staticmethod
    def _line_parts(geometry: Any) -> list[LineString]:
        if geometry.is_empty:
            return []
        if isinstance(geometry, LineString):
            return [geometry]
        if isinstance(geometry, MultiLineString):
            return [part for part in geometry.geoms if isinstance(part, LineString)]
        if hasattr(geometry, "geoms"):
            parts: list[LineString] = []
            for part in geometry.geoms:
                parts.extend(SplitPageDetector._line_parts(part))
            return parts
        return []

    @staticmethod
    def _is_content_frame_vector(vector: PathBase, bbox: BBox) -> bool:
        vx0, vy0, vx1, vy1 = coerce_bbox(vector.bbox)
        bx0, by0, bx1, by1 = bbox
        bbox_width = bx1 - bx0
        bbox_height = by1 - by0
        width = vx1 - vx0
        height = vy1 - vy0
        tolerance = CONTENT_EDGE_VECTOR_TOLERANCE_PT

        long_horizontal = width >= bbox_width * MIN_FRAME_VECTOR_SPAN_RATIO and height <= tolerance
        long_vertical = height >= bbox_height * MIN_FRAME_VECTOR_SPAN_RATIO and width <= tolerance
        on_horizontal_edge = (
            abs(vy0 - by0) <= tolerance
            or abs(vy1 - by0) <= tolerance
            or abs(vy0 - by1) <= tolerance
            or abs(vy1 - by1) <= tolerance
        )
        on_vertical_edge = (
            abs(vx0 - bx0) <= tolerance
            or abs(vx1 - bx0) <= tolerance
            or abs(vx0 - bx1) <= tolerance
            or abs(vx1 - bx1) <= tolerance
        )
        if (long_horizontal and on_horizontal_edge) or (long_vertical and on_vertical_edge):
            return True

        if not (on_horizontal_edge or on_vertical_edge):
            return False
        geometry = SplitPageDetector._vector_geometry(vector)
        total_length = float(geometry.length)
        if total_length <= 0:
            return False
        inside_length = float(geometry.intersection(box(*bbox)).length)
        return inside_length / total_length <= 0.25

    @staticmethod
    def _vector_geometry(vector: PathBase) -> LineString:
        points = [(float(point[0]), float(point[1])) for point in vector.points]
        if vector.type in {"rect", "quad"} and points:
            points = [*points, points[0]]
        if len(points) >= 2:
            return LineString(points)
        x0, y0, x1, y1 = coerce_bbox(vector.bbox)
        return LineString([(x0, y0), (x1, y1)])

    # -- helpers ------------------------------------------------------------

    def _fallback_bbox(self, page_bbox: BBox) -> BBox:
        px0, py0, px1, py1 = page_bbox
        inset = max(min(px1 - px0, py1 - py0) * 0.01, self.min_content_inset)
        return (px0 + inset, py0 + inset, px1 - inset, py1 - inset)

    @staticmethod
    def _bbox_area(bbox: BBox) -> float:
        return max(bbox[2] - bbox[0], 0.0) * max(bbox[3] - bbox[1], 0.0)
