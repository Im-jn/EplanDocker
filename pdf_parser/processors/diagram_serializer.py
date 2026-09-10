"""Normalize rich extraction records into the compact persisted diagram schema."""

from __future__ import annotations

import math
from typing import Any, Iterable

from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.utils import bbox_from_shapes, bbox_to_dict, coerce_bbox


def serialize_diagram(result: dict[str, Any], *, page: int) -> dict[str, Any]:
    """Return the compact graph while retaining unowned vectors separately."""
    elements = [dict(element) for element in result.get("elements", [])]
    original_element_ids = [element.get("id", index) for index, element in enumerate(elements)]
    for element_id, element in enumerate(elements):
        element["id"] = element_id
    element_id_map = dict(zip(original_element_ids, range(len(elements))))

    components = [
        _entity(
            item,
            entity_type="component",
            page=page,
            element_ids=[
                element_id_map[element_id]
                for element_id in item.get("element_ids", [])
                if element_id in element_id_map
            ],
        )
        for item in result.get("components", [])
    ]

    wire_element_by_id: dict[Any, int] = {}
    wires = []
    for index, wire in enumerate(result.get("wires", [])):
        wire_id = wire.get("id", index)
        element_id = _append_shape_element(
            elements,
            source=wire,
            element_type="wire",
            shape=wire.get("vectors", []),
            source_id=wire_id,
        )
        wire_element_by_id[wire_id] = element_id
        wires.append(_entity(
            wire,
            entity_type="wire",
            page=page,
            element_ids=[element_id],
            bbox=elements[element_id]["bbox"],
        ))

    endpoints = []
    for index, endpoint in enumerate(result.get("endpoints", [])):
        endpoint_id = endpoint.get("id", index)
        element_id = _append_shape_element(
            elements,
            source=endpoint,
            element_type=str(endpoint.get("type", "endpoint")),
            shape=endpoint.get("shape", []),
            source_id=endpoint_id,
            source_type="endpoint",
        )
        endpoints.append(_entity(
            endpoint,
            entity_type="endpoint",
            page=page,
            element_ids=[element_id],
            bbox=elements[element_id]["bbox"],
        ))

    groups = []
    for index, group in enumerate(result.get("groups", [])):
        group_id = group.get("id", index)
        element_id = _append_shape_element(
            elements,
            source=group,
            element_type="group",
            shape=group.get("shape", []),
            source_id=group_id,
        )
        groups.append(_entity(
            group,
            entity_type="group",
            page=page,
            element_ids=[element_id],
            bbox=elements[element_id]["bbox"],
        ))

    nets = []
    for net in result.get("nets", []):
        element_ids = [
            wire_element_by_id[wire_id]
            for wire_id in net.get("wire_ids", [])
            if wire_id in wire_element_by_id
        ]
        nets.append(_entity(
            net,
            entity_type="net",
            page=page,
            element_ids=element_ids,
            bbox=_elements_bbox(elements, element_ids),
        ))

    remaining = result.get("remaining_vector")
    remaining_vectors = (
        remaining.vectors if isinstance(remaining, VectorBase)
        else list(remaining or [])
    )

    remaining_text = _remaining_text_with_nearby(
        result.get("remaining_text", []),
        components,
    )

    return {
        "elements": elements,
        "components": components,
        "endpoints": endpoints,
        "wires": wires,
        "nets": nets,
        "groups": groups,
        "relations": [dict(relation) for relation in result.get("relations", [])],
        "remaining_vectors": remaining_vectors,
        "remaining_text": remaining_text,
    }


def _remaining_text_with_nearby(
    remaining_text: Iterable[dict[str, Any]],
    components: list[dict[str, Any]],
    *,
    candidate_limit: int = 5,
    nearest_ratio: float = 1.2,
) -> list[dict[str, Any]]:
    """Add component IDs whose bbox distance is within the nearest-distance band."""
    component_boxes: list[tuple[int, Any, tuple[float, float, float, float]]] = []
    for order, component in enumerate(components):
        try:
            component_bbox = coerce_bbox(component["bbox"])
        except (KeyError, TypeError, ValueError):
            continue
        component_boxes.append((order, component.get("id"), component_bbox))

    records: list[dict[str, Any]] = []
    for raw_record in remaining_text:
        record = dict(raw_record)
        try:
            text_bbox = coerce_bbox(record["bbox"])
        except (KeyError, TypeError, ValueError):
            record["nearby"] = []
            records.append(record)
            continue
        nearest = sorted(
            (
                _bbox_distance(text_bbox, component_bbox),
                order,
                component_id,
            )
            for order, component_id, component_bbox in component_boxes
        )[:candidate_limit]
        if not nearest:
            record["nearby"] = []
        else:
            maximum_distance = nearest[0][0] * nearest_ratio
            record["nearby"] = [
                component_id
                for distance, _, component_id in nearest
                if distance <= maximum_distance + 1e-9
            ]
        records.append(record)
    return records


def _bbox_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    left_x0, left_y0, left_x1, left_y1 = left
    right_x0, right_y0, right_x1, right_y1 = right
    dx = max(left_x0 - right_x1, right_x0 - left_x1, 0.0)
    dy = max(left_y0 - right_y1, right_y0 - left_y1, 0.0)
    return math.hypot(dx, dy)


def _entity(
    source: dict[str, Any],
    *,
    entity_type: str,
    page: int,
    element_ids: Iterable[Any],
    bbox: Any | None = None,
) -> dict[str, Any]:
    return {
        "id": source.get("id"),
        "type": entity_type,
        "page": int(page),
        "bbox": _bbox_or_none(source.get("bbox") if bbox is None else bbox),
        "title": _text_list(source, "title", "titles"),
        "descriptions": _text_list(source, "descriptions"),
        "elements": list(element_ids),
    }


def _append_shape_element(
    elements: list[dict[str, Any]],
    *,
    source: dict[str, Any],
    element_type: str,
    shape: Iterable[PathBase],
    source_id: Any,
    source_type: str | None = None,
) -> int:
    vectors = list(shape)
    attributes = dict(source.get("attributes", {}))
    structural_fields = {
        "id", "type", "shape", "vectors", "bbox", "title", "titles",
        "descriptions", "attributes",
    }
    for key, value in source.items():
        if key not in structural_fields and key not in attributes:
            attributes[key] = value
    attributes["source_type"] = source_type or element_type
    if source_id is not None:
        attributes["source_id"] = source_id
    if "entity_id" in source:
        attributes["entity_id"] = source["entity_id"]
    element_id = len(elements)
    elements.append({
        "id": element_id,
        "type": element_type,
        "shape": vectors,
        "bbox": _shape_bbox(vectors, source.get("bbox")),
        "attributes": attributes,
        "title": _text_list(source, "title", "titles"),
        "descriptions": _text_list(source, "descriptions"),
    })
    return element_id


def _shape_bbox(shape: list[PathBase], fallback: Any = None) -> dict[str, float] | None:
    if shape:
        return bbox_to_dict(bbox_from_shapes(shape))
    return _bbox_or_none(fallback)


def _elements_bbox(
    elements: list[dict[str, Any]],
    element_ids: list[int],
) -> dict[str, float] | None:
    boxes = [elements[element_id].get("bbox") for element_id in element_ids]
    boxes = [coerce_bbox(value) for value in boxes if value is not None]
    if not boxes:
        return None
    return bbox_to_dict((
        min(value[0] for value in boxes),
        min(value[1] for value in boxes),
        max(value[2] for value in boxes),
        max(value[3] for value in boxes),
    ))


def _bbox_or_none(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    try:
        return bbox_to_dict(coerce_bbox(value))
    except (KeyError, TypeError, ValueError):
        return None


def _text_list(source: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = source.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        for value in raw:
            text = str(value)
            if text and text not in values:
                values.append(text)
    return values
