"""Interpret arrow elements as page inputs or outputs."""

from __future__ import annotations

import math
from typing import Any, Iterable

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.utils import coerce_bbox


Point = tuple[float, float]
_DIRECTION_VECTORS: dict[str, Point] = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up": (0.0, -1.0),
    "down": (0.0, 1.0),
}


def classify_arrow_page_io(result: dict[str, Any]) -> None:
    """Set ``page_io`` on every arrow element and its owning component.

    The direction of a wire is measured locally from its shared endpoint into
    the page circuit.  An arrow pointing with that direction is an input; an
    arrow pointing against it is an output.  Missing, perpendicular, or
    conflicting geometric evidence remains ``unknown``.
    """
    elements = list(result.get("elements", []))
    components = list(result.get("components", []))
    endpoints = list(result.get("endpoints", []))
    wires = list(result.get("wires", []))
    relations = list(result.get("relations", []))

    endpoint_by_id = {
        endpoint.get("id", index): endpoint
        for index, endpoint in enumerate(endpoints)
    }
    wire_by_id = {
        wire.get("id", index): wire
        for index, wire in enumerate(wires)
    }
    endpoint_ids_by_component: dict[Any, set[Any]] = {}
    wire_ids_by_endpoint: dict[Any, set[Any]] = {}
    for relation in relations:
        if relation.get("type") != "connection":
            continue
        source = str(relation.get("source", ""))
        target = str(relation.get("target", ""))
        if not target.startswith("endpoint:"):
            continue
        endpoint_id = _node_id(target)
        if source.startswith("component:"):
            endpoint_ids_by_component.setdefault(_node_id(source), set()).add(endpoint_id)
        elif source.startswith("wire:"):
            wire_ids_by_endpoint.setdefault(endpoint_id, set()).add(_node_id(source))

    components_by_element: dict[Any, list[dict[str, Any]]] = {}
    for component in components:
        for element_id in component.get("element_ids", component.get("elements", [])):
            components_by_element.setdefault(element_id, []).append(component)

    component_results: dict[int, list[str]] = {}
    for element_index, element in enumerate(elements):
        if element.get("type") != "arrow":
            continue
        element_id = element.get("id", element_index)
        owners = components_by_element.get(element_id, [])
        classifications: list[str] = []
        for component in owners:
            classifications.extend(_component_arrow_evidence(
                component,
                element,
                endpoint_ids_by_component,
                wire_ids_by_endpoint,
                endpoint_by_id,
                wire_by_id,
            ))
        page_io = _combine_classifications(classifications)
        element.setdefault("attributes", {})["page_io"] = page_io
        for component in owners:
            component_results.setdefault(id(component), []).append(page_io)

    for component in components:
        values = component_results.get(id(component))
        if values:
            component["page_io"] = _combine_classifications(values)


def _component_arrow_evidence(
    component: dict[str, Any],
    arrow: dict[str, Any],
    endpoint_ids_by_component: dict[Any, set[Any]],
    wire_ids_by_endpoint: dict[Any, set[Any]],
    endpoint_by_id: dict[Any, dict[str, Any]],
    wire_by_id: dict[Any, dict[str, Any]],
) -> list[str]:
    arrow_direction = _DIRECTION_VECTORS.get(
        str(arrow.get("attributes", {}).get("direction", ""))
    )
    if arrow_direction is None:
        return []

    evidence: list[str] = []
    component_id = component.get("id")
    for endpoint_id in endpoint_ids_by_component.get(component_id, set()):
        endpoint = endpoint_by_id.get(endpoint_id)
        if endpoint is None:
            continue
        point = _endpoint_point(endpoint)
        if point is None:
            continue
        for wire_id in wire_ids_by_endpoint.get(endpoint_id, set()):
            wire = wire_by_id.get(wire_id)
            if wire is None:
                continue
            for local_direction in _wire_directions_from_endpoint(wire, point):
                score = _cosine(arrow_direction, local_direction)
                if score >= PARSER_CONFIG.diagram.arrow_flow_min_cosine:
                    evidence.append("input")
                elif score <= -PARSER_CONFIG.diagram.arrow_flow_min_cosine:
                    evidence.append("output")
    return evidence


def _wire_directions_from_endpoint(wire: dict[str, Any], point: Point) -> Iterable[Point]:
    tolerance = max(
        PARSER_CONFIG.geometry.topology_tolerance_pt,
        PARSER_CONFIG.diagram.relation_point_tolerance_pt,
    )
    for vector in wire.get("vectors", []):
        points = [
            (float(raw_point[0]), float(raw_point[1]))
            for raw_point in getattr(vector, "points", [])
        ]
        if len(points) < 2:
            continue
        if math.dist(point, points[0]) <= tolerance:
            neighbor = _first_distinct(points[1:], point, tolerance)
            if neighbor is not None:
                yield (neighbor[0] - point[0], neighbor[1] - point[1])
        if math.dist(point, points[-1]) <= tolerance:
            neighbor = _first_distinct(reversed(points[:-1]), point, tolerance)
            if neighbor is not None:
                yield (neighbor[0] - point[0], neighbor[1] - point[1])


def _endpoint_point(endpoint: dict[str, Any]) -> Point | None:
    try:
        x0, y0, x1, y1 = coerce_bbox(endpoint["bbox"])
    except (KeyError, TypeError, ValueError):
        return None
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _first_distinct(
    points: Iterable[Point],
    origin: Point,
    tolerance: float,
) -> Point | None:
    return next((point for point in points if math.dist(point, origin) > tolerance), None)


def _cosine(left: Point, right: Point) -> float:
    denominator = math.hypot(*left) * math.hypot(*right)
    if denominator <= 0.0:
        return 0.0
    return (left[0] * right[0] + left[1] * right[1]) / denominator


def _combine_classifications(values: Iterable[str]) -> str:
    resolved = {value for value in values if value in {"input", "output"}}
    return resolved.pop() if len(resolved) == 1 else "unknown"


def _node_id(reference: str) -> Any:
    value = reference.split(":", 1)[1]
    try:
        return int(value)
    except ValueError:
        return value
