"""Extract intermediate relations between diagram elements and wires."""

from __future__ import annotations

from numbers import Integral
from typing import Any

from shapely.geometry import LineString, Point as ShapelyPoint, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.tools.endpoints_tools import EndpointTools
from pdf_parser.utils import coerce_bbox


RELATION_POINT_TOLERANCE_PT = PARSER_CONFIG.diagram.relation_point_tolerance_pt
WIRE_ENDPOINT_FALLBACK_MARGIN_PT = (
    PARSER_CONFIG.diagram.wire_endpoint_fallback_margin_pt
)
BOX_ADJACENCY_MARGIN_PT = PARSER_CONFIG.diagram.box_adjacency_margin_pt
ELEMENT_CONTAINMENT_RATIO = PARSER_CONFIG.diagram.component_containment_ratio


def merge_adjacent_box_elements(
    elements: list[dict[str, Any]],
    *,
    margin: float = BOX_ADJACENCY_MARGIN_PT,
) -> dict[Any, Any]:
    """Merge neighboring box records and return dropped-to-kept ID aliases.

    A box fully contained inside another box remains an independent element;
    only boxes whose exterior regions touch or nearly touch are grouped.
    """
    if margin < 0:
        raise ValueError("box adjacency margin must be non-negative")

    box_indices = [
        index for index, element in enumerate(elements)
        if element.get("type") == "box"
    ]
    if len(box_indices) < 2:
        return {}

    geometries = [_element_relation_geometry(elements[index]) for index in box_indices]
    tree = STRtree(geometries)
    parent = list(range(len(box_indices)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left, geometry in enumerate(geometries):
        for tree_result in tree.query(
            geometry,
            predicate="dwithin",
            distance=margin,
        ):
            right = _tree_result_index(tree_result)
            if right <= left:
                continue
            right_geometry = geometries[right]
            if geometry.covers(right_geometry) or right_geometry.covers(geometry):
                continue
            if geometry.boundary.distance(right_geometry.boundary) > margin:
                continue
            union(left, right)

    groups: dict[int, list[int]] = {}
    for local_index, element_index in enumerate(box_indices):
        groups.setdefault(find(local_index), []).append(element_index)

    aliases: dict[Any, Any] = {}
    dropped_indices: set[int] = set()
    for member_indices in groups.values():
        if len(member_indices) < 2:
            continue
        kept_index = max(
            member_indices,
            key=lambda index: (
                _element_relation_geometry(elements[index]).area,
                -index,
            ),
        )
        kept = elements[kept_index]
        kept_id = kept.get("id", kept_index)
        members = [elements[index] for index in member_indices]
        _merge_box_records(kept, members)
        for index in member_indices:
            if index == kept_index:
                continue
            dropped_indices.add(index)
            aliases[elements[index].get("id", index)] = kept_id

    if dropped_indices:
        elements[:] = [
            element for index, element in enumerate(elements)
            if index not in dropped_indices
        ]
    return aliases


def _merge_box_records(
    kept: dict[str, Any],
    members: list[dict[str, Any]],
) -> None:
    shapes = _unique_objects([
        vector for member in members for vector in member.get("shape", [])
    ])
    contents = _unique_objects([
        vector
        for member in members
        for vector in member.get("attributes", {}).get("content", [])
    ])
    polygons = [_element_relation_geometry(member) for member in members]
    merged_geometry = unary_union(polygons)
    kept_attributes = dict(kept.get("attributes", {}))
    kept_attributes["polygon"] = merged_geometry
    kept_attributes["content"] = contents
    kept["shape"] = shapes
    kept["bbox"] = {
        "x0": float(merged_geometry.bounds[0]),
        "y0": float(merged_geometry.bounds[1]),
        "x1": float(merged_geometry.bounds[2]),
        "y1": float(merged_geometry.bounds[3]),
    }
    kept["attributes"] = kept_attributes
    for field in ("title", "titles", "descriptions"):
        values = []
        for member in members:
            for value in member.get(field, []):
                if value not in values:
                    values.append(value)
        if values or field in kept:
            kept[field] = values


def _unique_objects(values: list[Any]) -> list[Any]:
    seen: set[int] = set()
    unique = []
    for value in values:
        key = id(value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def organize_relation(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract wire-connection and direct element-containment edges.

    Wires connect only to independent endpoints. Endpoints are absorbed by
    elements through the same ``contains`` relation used by the element
    hierarchy; transitive ancestor edges are not duplicated.
    """
    if not isinstance(result, dict):
        raise TypeError("result must be the main extract_diagram result")

    legacy_component_input = bool(result.get("_legacy_relation_nodes")) or (
        "elements" not in result and "components" in result
    )
    element_key = "elements" if "elements" in result else "components"
    element_node_kind = "component" if legacy_component_input else "element"
    elements = list(result.get(element_key, []))
    wires = list(result.get("wires", []))
    endpoints = list(result.get("endpoints", []))
    edges: list[dict[str, Any]] = list(result.get("relations", []))
    aliases = merge_adjacent_box_elements(elements)
    if aliases:
        result[element_key] = elements
        edges = _remap_merged_element_relations(
            edges,
            aliases,
            element_node_kind,
        )
    if any("free_endpoints" in wire for wire in wires):
        endpoint_tools = EndpointTools(wires, elements)
        endpoints = endpoint_tools.build()
        result[element_key] = elements
        result["endpoints"] = endpoints
        edges = endpoint_tools.relations
    connection_geometries: list[Any] = []
    connection_indices_by_geometry: list[int] = []
    for element_index, element in enumerate(elements):
        geometry = _element_connection_geometry(element)
        if geometry is None or geometry.is_empty:
            continue
        connection_geometries.append(geometry)
        connection_indices_by_geometry.append(element_index)
    connection_tree = STRtree(connection_geometries) if connection_geometries else None

    edges.extend(_endpoint_containment_relations(
        elements,
        endpoints,
        connection_indices_by_geometry,
        connection_tree,
        element_node_kind,
    ))

    element_geometries = [_element_relation_geometry(element) for element in elements]
    element_geometry_tree = STRtree(element_geometries)
    for child_index, child_geometry in enumerate(element_geometries):
        child_id = elements[child_index].get("id", child_index)
        parent_candidate = min(
            (
                (element_geometries[parent_index].area, parent_index)
                for tree_result in element_geometry_tree.query(
                    child_geometry,
                    predicate="intersects",
                )
                if (parent_index := _tree_result_index(tree_result)) != child_index
                and element_geometries[parent_index].area > child_geometry.area
                and _coverage_ratio(
                    child_geometry,
                    element_geometries[parent_index],
                ) >= ELEMENT_CONTAINMENT_RATIO
            ),
            default=None,
        )
        if parent_candidate is None:
            continue
        _, parent_index = parent_candidate
        parent_id = elements[parent_index].get("id", parent_index)
        edges.append({
            "type": "contains",
            "source": f"{element_node_kind}:{parent_id}",
            "target": f"{element_node_kind}:{child_id}",
        })

    # wire and wire connections
    edges.extend(_wire_endpoint_relations(wires, endpoints, edges))
    edges.extend(_fallback_wire_endpoint_relations(
        elements,
        endpoints,
        edges,
        connection_indices_by_geometry,
        connection_tree,
        element_node_kind,
    ))
    return _unique_relations(edges)


def _remap_merged_element_relations(
    relations: list[dict[str, Any]],
    aliases: dict[Any, Any],
    element_node_kind: str,
) -> list[dict[str, Any]]:
    aliases_by_text = {str(source): target for source, target in aliases.items()}
    prefix = f"{element_node_kind}:"
    remapped: list[dict[str, Any]] = []
    for relation in relations:
        record = dict(relation)
        for field in ("source", "target"):
            ref = str(record.get(field, ""))
            if not ref.startswith(prefix):
                continue
            node_id = ref.split(":", 1)[1]
            if node_id in aliases_by_text:
                record[field] = f"{element_node_kind}:{aliases_by_text[node_id]}"
        if (
            record.get("type") == "contains"
            and record.get("source") == record.get("target")
        ):
            continue
        remapped.append(record)
    return remapped


def _coverage_ratio(child: BaseGeometry, parent: BaseGeometry) -> float:
    """Return how much of the child geometry is covered by a parent candidate."""
    intersection = child.intersection(parent)
    if child.area > 0:
        return intersection.area / child.area
    if child.length > 0:
        return intersection.length / child.length
    return 1.0 if parent.covers(child) else 0.0


def _endpoint_containment_relations(
    elements: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
    element_indices_by_geometry: list[int],
    element_tree: STRtree | None,
    element_node_kind: str = "element",
) -> list[dict[str, Any]]:
    """Return direct element absorption edges for independent endpoints."""
    relations: list[dict[str, Any]] = []
    if element_tree is None:
        return relations

    for endpoint_index, endpoint in enumerate(endpoints):
        endpoint_id = int(endpoint.get("id", endpoint_index))
        endpoint_geometry = _endpoint_geometry(endpoint)
        if endpoint_geometry is None or endpoint_geometry.is_empty:
            continue
        geometry_indices = sorted(
            _tree_result_index(result)
            for result in element_tree.query(
                endpoint_geometry,
                predicate="dwithin",
                distance=RELATION_POINT_TOLERANCE_PT,
            )
        )
        candidates = []
        for geometry_index in geometry_indices:
            element_index = element_indices_by_geometry[geometry_index]
            geometry = element_tree.geometries[geometry_index]
            candidates.append((geometry.area, element_index))
        if candidates:
            _, element_index = min(candidates)
            element_id = elements[element_index].get("id", element_index)
            relations.append({
                "type": "contains",
                "source": f"{element_node_kind}:{element_id}",
                "target": f"endpoint:{endpoint_id}",
            })
    return relations


def _fallback_wire_endpoint_relations(
    elements: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
    existing_edges: list[dict[str, Any]],
    element_indices_by_geometry: list[int],
    element_tree: STRtree | None,
    element_node_kind: str = "element",
    *,
    margin: float = WIRE_ENDPOINT_FALLBACK_MARGIN_PT,
) -> list[dict[str, Any]]:
    """Attach still-unowned wire endpoints to their nearest element."""
    if element_tree is None or margin <= 0:
        return []

    wire_endpoint_refs = {
        str(edge.get("target", ""))
        for edge in existing_edges
        if edge.get("type") in {"wire_connection", "connection"}
        and str(edge.get("source", "")).startswith("wire:")
        and str(edge.get("target", "")).startswith("endpoint:")
    }
    owned_endpoint_refs = {
        str(edge.get("target", ""))
        for edge in existing_edges
        if edge.get("type") == "contains"
        and str(edge.get("source", "")).startswith(f"{element_node_kind}:")
        and str(edge.get("target", "")).startswith("endpoint:")
    }
    unowned_refs = wire_endpoint_refs - owned_endpoint_refs
    if not unowned_refs:
        return []

    relations: list[dict[str, Any]] = []
    for endpoint_index, endpoint in enumerate(endpoints):
        endpoint_id = int(endpoint.get("id", endpoint_index))
        endpoint_ref = f"endpoint:{endpoint_id}"
        if endpoint_ref not in unowned_refs:
            continue
        endpoint_geometry = _endpoint_geometry(endpoint)
        if endpoint_geometry is None or endpoint_geometry.is_empty:
            continue
        candidates = []
        for tree_result in element_tree.query(
            endpoint_geometry,
            predicate="dwithin",
            distance=margin,
        ):
            geometry_index = _tree_result_index(tree_result)
            element_index = element_indices_by_geometry[geometry_index]
            geometry = element_tree.geometries[geometry_index]
            distance = float(geometry.distance(endpoint_geometry))
            candidates.append((distance, float(geometry.area), element_index))
        if not candidates:
            continue
        _, _, element_index = min(candidates)
        element_id = elements[element_index].get("id", element_index)
        relations.append({
            "type": "contains",
            "source": f"{element_node_kind}:{element_id}",
            "target": endpoint_ref,
        })
    return relations


def _wire_endpoint_relations(
    wires: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
    existing_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Connect wires whose geometry meets an endpoint owned by another wire.

    An STRtree limits exact distance checks to nearby line strings, so
    this does not degenerate into comparing every endpoint with every vector.
    """
    indexed_lines: list[LineString] = []
    wire_index_by_line: list[int] = []
    for wire_index, wire in enumerate(wires):
        for vector in wire.get("vectors", []):
            geometry = _wire_relation_line(vector)
            if geometry is None or geometry.length <= 2 * RELATION_POINT_TOLERANCE_PT:
                continue
            indexed_lines.append(geometry)
            wire_index_by_line.append(wire_index)

    if not indexed_lines:
        return []

    tree = STRtree(indexed_lines)
    endpoint_by_id = {
        int(endpoint.get("id", endpoint_index)): endpoint
        for endpoint_index, endpoint in enumerate(endpoints)
    }
    relations: list[dict[str, Any]] = []
    seen = {
        (edge.get("source"), edge.get("target"))
        for edge in existing_edges
        if edge.get("type") == "wire_connection"
    }
    connected_endpoint_ids = {
        int(str(edge["target"]).split(":", 1)[1])
        for edge in existing_edges
        if edge.get("type") == "wire_connection"
        and str(edge.get("target", "")).startswith("endpoint:")
    }
    for endpoint_id in connected_endpoint_ids:
        endpoint = endpoint_by_id.get(endpoint_id)
        if endpoint is None:
            continue
        endpoint_geometry = _endpoint_geometry(endpoint)
        if endpoint_geometry is None or endpoint_geometry.is_empty:
            continue

        for tree_result in tree.query(
            endpoint_geometry,
            predicate="dwithin",
            distance=RELATION_POINT_TOLERANCE_PT,
        ):
            line_index = _tree_result_index(tree_result)
            segment_wire_index = wire_index_by_line[line_index]
            wire_id = wires[segment_wire_index].get("id", segment_wire_index)
            relation_key = (f"wire:{wire_id}", f"endpoint:{endpoint_id}")
            if relation_key in seen:
                continue
            seen.add(relation_key)
            relations.append({
                "type": "wire_connection",
                "source": relation_key[0],
                "target": relation_key[1],
            })
    return relations


def _unique_relations(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for relation in relations:
        key = (relation.get("type"), relation.get("source"), relation.get("target"))
        if key not in seen:
            seen.add(key)
            unique.append(relation)
    return unique


def _endpoint_geometry(endpoint: dict[str, Any]) -> BaseGeometry | None:
    geometries = [
        geometry
        for vector in endpoint.get("shape", [])
        if (geometry := _component_vector_geometry(vector)) is not None
        and not geometry.is_empty
    ]
    if geometries:
        return unary_union(geometries)
    bbox = coerce_bbox(endpoint["bbox"])
    if bbox[0] == bbox[2] and bbox[1] == bbox[3]:
        return ShapelyPoint(bbox[0], bbox[1])
    return box(*bbox)


def _wire_relation_line(vector: Any) -> LineString | None:
    if getattr(vector, "type", None) != "line":
        return None
    points = [
        (float(point[0]), float(point[1]))
        for point in getattr(vector, "points", [])
    ]
    if len(points) < 2 or len(set(points)) < 2:
        return None
    return LineString(points)


def _tree_result_index(result: Any) -> int:
    if isinstance(result, Integral):
        return int(result)
    raise TypeError("STRtree index results require shapely>=2.0")


def _element_connection_geometry(element: dict[str, Any]) -> Any | None:
    """Geometry which a free wire endpoint may electrically connect to."""
    polygon = element.get("attributes", {}).get("polygon")
    if isinstance(polygon, BaseGeometry) and not polygon.is_empty:
        # The polygon already includes its boundary, so unioning its shape
        # vectors again would only repeat geometry work.
        return polygon

    geometries: list[Any] = []
    for vector in element.get("shape", []):
        geometry = _component_vector_geometry(vector)
        if geometry is not None and not geometry.is_empty:
            geometries.append(geometry)
    if not geometries:
        return None
    return unary_union(geometries)


def _component_vector_geometry(vector: Any) -> BaseGeometry | None:
    points = [
        (float(point[0]), float(point[1]))
        for point in getattr(vector, "points", [])
    ]
    vector_type = getattr(vector, "type", None)
    if vector_type == "curve" and len(points) == 4:
        p0, p1, p2, p3 = points
        sampled = []
        for step in range(17):
            t = step / 16.0
            u = 1.0 - t
            sampled.append((
                u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
            ))
        return LineString(sampled)
    if vector_type in {"rect", "quad"} and len(points) >= 3:
        return LineString([*points, points[0]])
    if len(points) >= 2 and len(set(points)) >= 2:
        return LineString(points)
    if len(points) == 1:
        # Keep the old point-contact behavior for single-point component paths.
        return ShapelyPoint(points[0])
    return None


def _element_relation_geometry(element: dict[str, Any]) -> Any:
    polygon = element.get("attributes", {}).get("polygon")
    if isinstance(polygon, BaseGeometry) and not polygon.is_empty:
        return polygon
    return box(*coerce_bbox(element["bbox"]))
