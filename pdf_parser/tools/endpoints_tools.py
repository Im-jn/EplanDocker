"""Normalize page-level endpoints and describe their wire connections."""

from __future__ import annotations

import math
from typing import Any, Sequence

from shapely.geometry import LineString, Point as ShapelyPoint, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase
from pdf_parser.utils import bbox_to_dict, coerce_bbox


Point = tuple[float, float]
ELEMENT_ENDPOINT_TYPES = {"endpoint_circle"}


class EndpointTools:
    """Build independent endpoints and wire-to-endpoint relation edges."""

    def __init__(
        self,
        wires: Sequence[dict[str, Any]],
        elements: list[dict[str, Any]] | None = None,
        *,
        merge_tolerance: float = PARSER_CONFIG.geometry.topology_tolerance_pt,
        element_match_tolerance: float = (
            PARSER_CONFIG.diagram.endpoint_component_tolerance_pt
        ),
        component_match_tolerance: float | None = None,
    ):
        if component_match_tolerance is not None:
            element_match_tolerance = component_match_tolerance
        if merge_tolerance < 0:
            raise ValueError("merge_tolerance must be non-negative")
        if element_match_tolerance < 0:
            raise ValueError("element_match_tolerance must be non-negative")
        self.wires = list(wires)
        self.elements = elements
        self.merge_tolerance = float(merge_tolerance)
        self.element_match_tolerance = float(element_match_tolerance)
        self.relations: list[dict[str, Any]] = []

    def build(self) -> list[dict[str, Any]]:
        """Build endpoints and remove the former endpoint element artifacts."""
        graphical_endpoints = self._extract_element_endpoints()
        point_endpoints = self._collect_wire_points()
        unmatched_point_endpoints: list[dict[str, Any]] = []

        for point_endpoint in point_endpoints:
            matched = self._nearest_graphical_endpoint(
                point_endpoint["_anchor"],
                graphical_endpoints,
                point_endpoint["wire_ids"],
            )
            if matched is None:
                unmatched_point_endpoints.append(point_endpoint)
                continue
            self._merge_wire_ids(matched, point_endpoint["wire_ids"])

        graphical_endpoints.extend(unmatched_point_endpoints)
        endpoints = sorted(
            graphical_endpoints,
            key=lambda endpoint: (
                coerce_bbox(endpoint["bbox"])[1],
                coerce_bbox(endpoint["bbox"])[0],
                endpoint.get("_source_element_id", math.inf),
            ),
        )
        for endpoint_id, endpoint in enumerate(endpoints):
            endpoint["id"] = endpoint_id

        point_endpoint_id = {
            self._point_key(endpoint["_anchor"]): endpoint["id"]
            for endpoint in endpoints
            if endpoint["type"] == "point"
        }
        relations: list[dict[str, Any]] = []
        for wire_order, wire in enumerate(self.wires):
            wire_id = int(wire.get("id", wire_order))
            endpoint_ids: list[int] = []
            for raw_point in wire.pop("free_endpoints", []):
                point = self._point(raw_point)
                endpoint = self._endpoint_for_wire_point(
                    point,
                    wire_id,
                    endpoints,
                    point_endpoint_id,
                )
                if endpoint["id"] not in endpoint_ids:
                    endpoint_ids.append(endpoint["id"])
            wire.pop("endpoints", None)
            relations.extend({
                "type": "wire_connection",
                "source": f"wire:{wire_id}",
                "target": f"endpoint:{endpoint_id}",
            } for endpoint_id in endpoint_ids)

        for endpoint in endpoints:
            endpoint.pop("_anchor", None)
            endpoint.pop("_geometry", None)
            endpoint.pop("_source_element_id", None)
            endpoint.pop("wire_ids", None)
        self.relations = relations
        return endpoints

    def _extract_element_endpoints(self) -> list[dict[str, Any]]:
        if self.elements is None:
            return []
        endpoints: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for element_order, element in enumerate(self.elements):
            element_type = element.get("type")
            if element_type not in ELEMENT_ENDPOINT_TYPES:
                element["id"] = len(remaining)
                remaining.append(element)
                continue
            geometry = _shape_geometry(element.get("shape", []))
            bbox = bbox_to_dict(coerce_bbox(element["bbox"]))
            endpoints.append({
                "id": -1,
                "type": element_type.removeprefix("endpoint_"),
                "shape": list(element.get("shape", [])),
                "bbox": bbox,
                "wire_ids": [],
                "attributes": dict(element.get("attributes", {})),
                "title": list(element.get("title", [])),
                "descriptions": list(element.get("descriptions", [])),
                "_anchor": geometry.centroid.coords[0] if geometry and not geometry.is_empty
                else _bbox_center(bbox),
                "_geometry": geometry,
                "_source_element_id": element.get("id", element_order),
            })
        self.elements[:] = remaining
        return endpoints

    def _collect_wire_points(self) -> list[dict[str, Any]]:
        endpoints: list[dict[str, Any]] = []
        for wire_order, wire in enumerate(self.wires):
            wire_id = int(wire.get("id", wire_order))
            for raw_point in wire.get("free_endpoints", []):
                point = self._point(raw_point)
                endpoint = next(
                    (
                        candidate
                        for candidate in endpoints
                        if math.dist(candidate["_anchor"], point) <= self.merge_tolerance
                    ),
                    None,
                )
                if endpoint is None:
                    endpoint = self._point_record(point)
                    endpoints.append(endpoint)
                self._merge_wire_ids(endpoint, [wire_id])
        return endpoints

    def _nearest_graphical_endpoint(
        self,
        point: Point,
        endpoints: list[dict[str, Any]],
        wire_ids: list[int],
    ) -> dict[str, Any] | None:
        candidates = []
        point_geometry = ShapelyPoint(point)
        for endpoint in endpoints:
            if set(endpoint["wire_ids"]).intersection(wire_ids):
                continue
            geometry = endpoint.get("_geometry")
            distance = (
                point_geometry.distance(geometry)
                if geometry is not None and not geometry.is_empty
                else math.dist(point, endpoint["_anchor"])
            )
            if distance <= self.element_match_tolerance:
                bbox = coerce_bbox(endpoint["bbox"])
                area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                candidates.append((
                    distance,
                    area,
                    endpoint.get("_source_element_id", math.inf),
                    endpoint,
                ))
        return min(candidates, default=None, key=lambda item: item[:3])[3] if candidates else None

    def _endpoint_for_wire_point(
        self,
        point: Point,
        wire_id: int,
        endpoints: list[dict[str, Any]],
        point_endpoint_id: dict[tuple[int, int], int],
    ) -> dict[str, Any]:
        point_id = point_endpoint_id.get(self._point_key(point))
        if point_id is not None:
            return endpoints[point_id]
        candidates = [
            endpoint
            for endpoint in endpoints
            if wire_id in endpoint["wire_ids"]
            and _point_to_endpoint_distance(point, endpoint) <= self.element_match_tolerance
        ]
        if not candidates:
            raise RuntimeError("wire endpoint was not normalized")
        return min(candidates, key=lambda endpoint: _point_to_endpoint_distance(point, endpoint))

    def _point_record(self, point: Point) -> dict[str, Any]:
        x, y = point
        return {
            "id": -1,
            "type": "point",
            "shape": [PathBase(type="point", points=[point])],
            "bbox": {"x0": x, "y0": y, "x1": x, "y1": y},
            "wire_ids": [],
            "attributes": {},
            "title": [],
            "descriptions": [],
            "_anchor": point,
            "_geometry": ShapelyPoint(point),
        }

    @staticmethod
    def _merge_wire_ids(endpoint: dict[str, Any], wire_ids: list[int]) -> None:
        endpoint["wire_ids"] = sorted(set(endpoint["wire_ids"]).union(wire_ids))

    def _point_key(self, point: Point) -> tuple[int, int]:
        tolerance = self.merge_tolerance or 1e-9
        return (round(point[0] / tolerance), round(point[1] / tolerance))

    @staticmethod
    def _point(raw_point: Any) -> Point:
        try:
            if len(raw_point) != 2:
                raise ValueError
            return (float(raw_point[0]), float(raw_point[1]))
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError("wire free endpoint must contain two coordinates") from exc


def _point_to_endpoint_distance(point: Point, endpoint: dict[str, Any]) -> float:
    geometry = endpoint.get("_geometry")
    if geometry is not None and not geometry.is_empty:
        return ShapelyPoint(point).distance(geometry)
    return math.dist(point, endpoint["_anchor"])


def _shape_geometry(shape: Sequence[Any]) -> BaseGeometry | None:
    geometries = [
        geometry
        for vector in shape
        if (geometry := _vector_geometry(vector)) is not None and not geometry.is_empty
    ]
    return unary_union(geometries) if geometries else None


def _vector_geometry(vector: Any) -> BaseGeometry | None:
    points = [(float(point[0]), float(point[1])) for point in getattr(vector, "points", [])]
    vector_type = getattr(vector, "type", None)
    if vector_type == "curve" and len(points) == 4:
        p0, p1, p2, p3 = points
        sampled = []
        for step in range(17):
            t = step / 16
            u = 1 - t
            sampled.append((
                u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
            ))
        return LineString(sampled)
    if vector_type in {"rect", "quad"} and len(points) >= 3:
        return Polygon(points)
    if len(points) >= 2 and len(set(points)) >= 2:
        return LineString(points)
    if len(points) == 1:
        return ShapelyPoint(points[0])
    return None


def _bbox_center(bbox: dict[str, float]) -> Point:
    return ((bbox["x0"] + bbox["x1"]) / 2, (bbox["y0"] + bbox["y1"]) / 2)
