"""Compose the final diagram graph from intermediate element relations."""

from __future__ import annotations

import re
from typing import Any

from shapely.geometry import Polygon, box

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.utils import bbox_from_shapes, bbox_to_dict, coerce_bbox


CONTAINMENT_RATIO = PARSER_CONFIG.diagram.component_containment_ratio


def handle_relations(
    result: dict[str, Any],
    relations: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build final component/net entities and graph edges.

    ``relations`` may contain temporary ``element:*`` nodes. Those nodes are
    consumed here and never escape into the final graph.
    """
    if not isinstance(result, dict):
        raise TypeError("result must be the main extract_diagram result")
    if not isinstance(relations, list):
        raise TypeError("relations must be a list of extracted edges")
    if "elements" not in result:
        return _handle_legacy_relations(result, relations)

    _transfer_wire_marks(result, relations)
    wires = list(result.get("wires", []))
    _ensure_wire_text_fields(wires)
    elements = list(result.get("elements", []))
    components, component_by_element = _element_components(elements, relations)
    nets = _wire_nets(wires, relations)
    final_relations = _final_relations(
        relations,
        component_by_element=component_by_element,
        components=components,
        nets=nets,
        groups=list(result.get("groups", [])),
    )
    result["components"] = components
    result["nets"] = nets
    result["relations"] = final_relations
    return {
        "components": components,
        "nets": nets,
        "relations": final_relations,
    }


def _handle_legacy_relations(
    result: dict[str, Any],
    relations: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Keep the pre-v2 composer API usable while stored data migrates."""
    translated = [
        {
            **edge,
            "source": str(edge.get("source", "")).replace("component:", "element:", 1),
            "target": str(edge.get("target", "")).replace("component:", "element:", 1),
        }
        for edge in relations
    ]
    original_components = list(result.get("components", []))
    temporary_elements = []
    for index, component in enumerate(original_components):
        normalized = dict(component)
        normalized.setdefault("id", index)
        normalized.setdefault("type", "unknown")
        normalized.setdefault("shape", [])
        normalized.setdefault("bbox", (0, 0, 0, 0))
        normalized.setdefault("attributes", {})
        normalized.setdefault("title", [])
        normalized.setdefault("descriptions", [])
        temporary_elements.append(normalized)
    temporary = {
        "elements": temporary_elements,
        "groups": [],
        "wires": list(result.get("wires", [])),
    }
    handled = handle_relations(temporary, translated)
    retained_ids = {element["id"] for element in temporary["elements"]}
    result["components"] = [
        component for index, component in enumerate(original_components)
        if component.get("id", index) in retained_ids
    ]
    result["wires"] = temporary["wires"]
    relations[:] = [
        {
            **edge,
            "source": str(edge.get("source", "")).replace("element:", "component:", 1),
            "target": str(edge.get("target", "")).replace("element:", "component:", 1),
        }
        for edge in translated
    ]
    nets = [
        {
            "id": net["id"],
            "wire_ids": net["wire_ids"],
            "endpoints": net["endpoints"],
        }
        for net in handled["nets"]
    ]
    compounds = [
        {
            "id": component["id"],
            "root_component_id": component["attributes"]["root_element_id"],
            "sub_component_ids": component["element_ids"][1:],
            "endpoints": component["endpoints"],
        }
        for component in handled["components"]
    ]
    return {"nets": nets, "compounds": compounds}


def _transfer_wire_marks(
    result: dict[str, Any],
    relations: list[dict[str, Any]],
) -> None:
    """Move wire-mark text to its wire and remove the intermediate artifact."""
    wires = list(result.get("wires", []))
    _ensure_wire_text_fields(wires)
    wire_by_vector_id = {
        id(vector): wire
        for wire in wires
        for vector in wire.get("vectors", [])
    }
    retained: list[dict[str, Any]] = []
    removed_refs: set[str] = set()
    for element_index, element in enumerate(result.get("elements", [])):
        if element.get("type") != "wire_mark":
            retained.append(element)
            continue
        element_id = element.get("id", element_index)
        removed_refs.add(f"element:{element_id}")
        wire = wire_by_vector_id.get(id(element.get("attributes", {}).get("wire_vector")))
        if wire is not None:
            _extend_unique(wire["titles"], _text_values(element, "titles", "title"))
            _extend_unique(wire["descriptions"], _text_values(element, "descriptions"))
    result["elements"] = retained
    if removed_refs:
        relations[:] = [
            edge for edge in relations
            if edge.get("source") not in removed_refs and edge.get("target") not in removed_refs
        ]


def _element_components(
    elements: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[Any, int]]:
    element_by_id = {
        element.get("id", index): element
        for index, element in enumerate(elements)
    }
    children = {element_id: set() for element_id in element_by_id}
    parent_by_child: dict[Any, Any] = {}
    endpoints_by_element = {element_id: set() for element_id in element_by_id}
    for edge in relations:
        if edge.get("type") != "contains":
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if not source.startswith("element:"):
            continue
        parent_id = _coerce_node_id(source)
        if parent_id not in element_by_id:
            continue
        if target.startswith("element:"):
            child_id = _coerce_node_id(target)
            if child_id in element_by_id and child_id != parent_id:
                children[parent_id].add(child_id)
                parent_by_child.setdefault(child_id, parent_id)
        elif target.startswith("endpoint:"):
            endpoints_by_element[parent_id].add(int(target.split(":", 1)[1]))

    roots = [element_id for element_id in element_by_id if element_id not in parent_by_child]
    visited: set[Any] = set()
    components: list[dict[str, Any]] = []
    component_by_element: dict[Any, int] = {}
    for root_id in [*roots, *element_by_id]:
        if root_id in visited:
            continue
        member_ids: list[Any] = []
        endpoint_ids: set[int] = set()
        stack = [root_id]
        while stack:
            element_id = stack.pop()
            if element_id in visited:
                continue
            visited.add(element_id)
            member_ids.append(element_id)
            endpoint_ids.update(endpoints_by_element.get(element_id, set()))
            stack.extend(sorted(children.get(element_id, set()), key=str, reverse=True))

        members = [element_by_id[element_id] for element_id in member_ids]
        component_id = len(components)
        for element_id in member_ids:
            component_by_element[element_id] = component_id
        shape = _unique_objects([
            vector
            for element in members
            for vector in [
                *element.get("shape", []),
                *element.get("attributes", {}).get("content", []),
            ]
        ])
        titles: list[str] = []
        descriptions: list[str] = []
        for element in members:
            _extend_unique(titles, _text_values(element, "titles", "title"))
            _extend_unique(descriptions, _text_values(element, "descriptions"))
        components.append({
            "id": component_id,
            "type": "component",
            "shape": shape,
            "bbox": _merged_bbox(members, shape),
            "attributes": {
                "root_element_id": root_id,
                "element_types": list(dict.fromkeys(str(item.get("type", "")) for item in members)),
            },
            "title": titles,
            "descriptions": descriptions,
            "element_ids": member_ids,
            "endpoints": sorted(endpoint_ids),
        })
    return components, component_by_element


def _wire_nets(
    wires: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    parent = list(range(len(wires)))
    rank = [0] * len(wires)
    index_by_ref = {
        f"wire:{wire.get('id', index)}": index for index, wire in enumerate(wires)
    }

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1

    wires_by_endpoint: dict[str, list[int]] = {}
    for edge in edges:
        if edge.get("type") not in {"wire_connection", "connection"}:
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        wire_index = index_by_ref.get(source)
        if wire_index is not None and target.startswith("endpoint:"):
            wires_by_endpoint.setdefault(target, []).append(wire_index)
    for indices in wires_by_endpoint.values():
        for index in indices[1:]:
            union(indices[0], index)

    groups: dict[int, list[int]] = {}
    for index in range(len(wires)):
        groups.setdefault(find(index), []).append(index)
    ordered = sorted(
        (group for group in groups.values() if len(group) > 1),
        key=lambda group: group[0],
    )
    endpoints_by_root: dict[int, set[int]] = {}
    for endpoint_ref, indices in wires_by_endpoint.items():
        endpoint_id = int(endpoint_ref.split(":", 1)[1])
        for index in indices:
            endpoints_by_root.setdefault(find(index), set()).add(endpoint_id)

    nets = []
    for net_id, indices in enumerate(ordered):
        members = [wires[index] for index in indices]
        shapes = [vector for wire in members for vector in wire.get("vectors", [])]
        nets.append({
            "id": net_id,
            "type": "net",
            "shape": [],
            "bbox": _shape_bbox_or_none(shapes),
            "attributes": {},
            "title": _common_wire_name(members),
            "descriptions": [],
            "wire_ids": [wire.get("id", index) for index, wire in zip(indices, members)],
            "endpoints": sorted(endpoints_by_root.get(find(indices[0]), set())),
        })
    return nets


def _common_wire_name(wires: list[dict[str, Any]]) -> list[str]:
    """Prefer common titles, then common descriptions, as the net name."""
    titles = _common_wire_values(wires, "titles", "title")
    if titles:
        return titles
    descriptions = _common_wire_values(wires, "descriptions")
    return descriptions or _common_wire_description_tokens(wires)


def _common_wire_description_tokens(wires: list[dict[str, Any]]) -> list[str]:
    """Find shared identifier-like tokens embedded in wire descriptions."""
    if not wires:
        return []

    def tokens(wire: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for description in _text_values(wire, "descriptions"):
            for token in re.findall(
                r"[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)*",
                description,
            ):
                if not re.search(r"[A-Za-z]", token) or not re.search(r"\d", token):
                    continue
                if token.casefold() not in {value.casefold() for value in values}:
                    values.append(token)
        return values

    first_tokens = tokens(wires[0])
    common = {token.casefold() for token in first_tokens}
    for wire in wires[1:]:
        common.intersection_update(token.casefold() for token in tokens(wire))
        if not common:
            return []
    return [token for token in first_tokens if token.casefold() in common]


def _common_wire_values(
    wires: list[dict[str, Any]],
    *fields: str,
) -> list[str]:
    """Return values present on every wire, ordered by the first wire."""
    if not wires:
        return []
    first_values = list(dict.fromkeys(_text_values(wires[0], *fields)))
    common = set(first_values)
    for wire in wires[1:]:
        common.intersection_update(_text_values(wire, *fields))
        if not common:
            return []
    return [value for value in first_values if value in common]


def _final_relations(
    intermediate: list[dict[str, Any]],
    *,
    component_by_element: dict[Any, int],
    components: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    final: list[dict[str, Any]] = []
    for edge in intermediate:
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if edge.get("type") in {"wire_connection", "connection"}:
            if source.startswith("wire:") and target.startswith("endpoint:"):
                final.append({"type": "connection", "source": source, "target": target})
        elif edge.get("type") == "contains" and source.startswith("element:") and target.startswith("endpoint:"):
            component_id = component_by_element.get(_coerce_node_id(source))
            if component_id is not None:
                final.append({
                    "type": "connection",
                    "source": f"component:{component_id}",
                    "target": target,
                })

    for net in nets:
        for wire_id in net["wire_ids"]:
            final.append({
                "type": "contains",
                "source": f"net:{net['id']}",
                "target": f"wire:{wire_id}",
            })

    for group in groups:
        group["component_ids"] = []
    for component in components:
        owner = _smallest_containing_group(component, groups)
        if owner is None:
            continue
        owner["component_ids"].append(component["id"])
        final.append({
            "type": "contains",
            "source": f"group:{owner['id']}",
            "target": f"component:{component['id']}",
        })
    return _unique_relations(final)


def _smallest_containing_group(
    component: dict[str, Any],
    groups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    component_geometry = box(*coerce_bbox(component["bbox"]))
    candidates = []
    for group in groups:
        polygon = group.get("attributes", {}).get("polygon")
        group_geometry = polygon if isinstance(polygon, Polygon) else box(*coerce_bbox(group["bbox"]))
        if component_geometry.area <= 0:
            covered = group_geometry.covers(component_geometry)
        else:
            covered = group_geometry.intersection(component_geometry).area / component_geometry.area >= CONTAINMENT_RATIO
        if covered:
            candidates.append((group_geometry.area, str(group.get("id")), group))
    return min(candidates, default=None, key=lambda item: item[:2])[2] if candidates else None


def _merged_bbox(members: list[dict[str, Any]], shape: list[Any]) -> dict[str, float]:
    if shape:
        return bbox_to_dict(bbox_from_shapes(shape))
    boxes = [coerce_bbox(member["bbox"]) for member in members]
    return bbox_to_dict((
        min(item[0] for item in boxes),
        min(item[1] for item in boxes),
        max(item[2] for item in boxes),
        max(item[3] for item in boxes),
    ))


def _shape_bbox_or_none(shape: list[Any]) -> dict[str, float] | None:
    if not shape:
        return None
    try:
        return bbox_to_dict(bbox_from_shapes(shape))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _ensure_wire_text_fields(wires: list[dict[str, Any]]) -> None:
    for wire in wires:
        wire.setdefault("titles", [])
        wire.setdefault("descriptions", [])


def _text_values(item: dict[str, Any], *fields: str) -> list[str]:
    values: list[str] = []
    for field in fields:
        raw = item.get(field, [])
        if isinstance(raw, str):
            raw = [raw]
        values.extend(str(value) for value in raw if str(value))
    return values


def _extend_unique(target: list[str], values: list[str]) -> None:
    seen = set(target)
    for value in values:
        if value not in seen:
            target.append(value)
            seen.add(value)


def _unique_objects(values: list[Any]) -> list[Any]:
    seen: set[int] = set()
    unique = []
    for value in values:
        if id(value) not in seen:
            unique.append(value)
            seen.add(id(value))
    return unique


def _unique_relations(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for relation in relations:
        key = (relation.get("type"), relation.get("source"), relation.get("target"))
        if key not in seen:
            seen.add(key)
            unique.append(relation)
    return unique


def _coerce_node_id(node_ref: str) -> Any:
    raw_id = node_ref.split(":", 1)[1]
    try:
        return int(raw_id)
    except ValueError:
        return raw_id
