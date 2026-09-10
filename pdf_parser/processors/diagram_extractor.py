"""Extract intermediate elements, groups, and wires from diagram content."""

from __future__ import annotations

from numbers import Integral
from typing import Any, Sequence

from shapely.geometry import LineString, MultiLineString, Point as ShapelyPoint, Polygon, box
from shapely.ops import snap, unary_union
from shapely.strtree import STRtree

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase, TextBase, VectorBase
from pdf_parser.tools.vector_box import VectorBoxDetector
from pdf_parser.tools.vector_matcher import VectorMatcher
from pdf_parser.tools.vector_pin import VectorPinDetector
from pdf_parser.utils import bbox_from_shapes, bbox_to_dict, coerce_bbox


Point = tuple[float, float]
WIRE_ENDPOINT_TOLERANCE_PT = PARSER_CONFIG.geometry.topology_tolerance_pt
WIRE_SEED_MIN_LENGTH_PT = PARSER_CONFIG.diagram.wire_seed_min_length_pt
WIRE_TERMINAL_DIAGONAL_MAX_LENGTH_PT = (
    PARSER_CONFIG.diagram.wire_terminal_diagonal_max_length_pt
)


def extract_diagram(
    vector_base: VectorBase,
    text_base: TextBase,
    symbol_list: Sequence[Sequence[PathBase]],
) -> dict[str, Any]:
    """Find diagram elements, groups, and wires."""
    if not isinstance(vector_base, VectorBase):
        raise TypeError("vector_base must be a VectorBase")
    if not isinstance(text_base, TextBase):
        raise TypeError("text_base must be a TextBase")

    elements, groups, remaining_vectors = extract_vector_elements(vector_base, symbol_list)
    wires, remaining_vectors = extract_wires(remaining_vectors)

    return {
        "elements": elements,
        "groups": groups,
        "components": _legacy_component_view(elements),
        "_legacy_relation_nodes": True,
        "wires": wires,
    }, remaining_vectors


def merge_diagram_results(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-entity diagram results into one page-level result."""
    elements: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    remains: list[PathBase] = []

    for entity_id, result in enumerate(results):
        source_elements = result.get("elements", result.get("components", []))
        for element in source_elements:
            element["id"] = len(elements)
            element.setdefault("attributes", {})["entity_id"] = entity_id
            elements.append(element)
        for group in result.get("groups", []):
            group["id"] = len(groups)
            group.setdefault("attributes", {})["entity_id"] = entity_id
            groups.append(group)
        for wire in result.get("wires", []):
            wire["id"] = len(wires)
            wire["entity_id"] = entity_id
            wires.append(wire)
        remaining = result.get("remains")
        if isinstance(remaining, VectorBase):
            remains.extend(remaining.vectors)

    merged = {
        "elements": elements,
        "groups": groups,
        "components": _legacy_component_view(elements),
        "wires": wires,
    }
    if remains:
        merged["remains"] = VectorBase(remains)
    return merged


def extract_wires(vector_base: VectorBase) -> tuple[list[dict[str, Any]], VectorBase]:
    """Extract wire nets seeded by long horizontal/vertical vectors."""
    if not isinstance(vector_base, VectorBase):
        raise TypeError("vector_base must be a VectorBase")
    if not vector_base.vectors:
        return [], vector_base

    parent = list(range(len(vector_base.vectors)))
    rank = [0] * len(vector_base.vectors)
    seed_vector_ids: set[int] = set()
    terminal_diagonal_ids: set[int] = set()
    endpoint_keys_by_vector: list[list[tuple[int, int]]] = []
    vector_ids_by_endpoint: dict[tuple[int, int], list[int]] = {}

    for vector_id, vector in enumerate(vector_base.vectors):
        if _is_wire_seed_vector(vector):
            seed_vector_ids.add(vector_id)
        if _is_terminal_diagonal(vector):
            terminal_diagonal_ids.add(vector_id)
        endpoint_keys = []
        for endpoint in _wire_vector_endpoints(vector):
            key = _wire_point_key(endpoint)
            endpoint_keys.append(key)
            vector_ids_by_endpoint.setdefault(key, []).append(vector_id)
        endpoint_keys_by_vector.append(endpoint_keys)

    for vector_ids in vector_ids_by_endpoint.values():
        propagating_ids = [
            vector_id for vector_id in vector_ids if vector_id not in terminal_diagonal_ids
        ]
        if len(propagating_ids) < 2:
            continue
        first = propagating_ids[0]
        for vector_id in propagating_ids[1:]:
            _union(parent, rank, first, vector_id)

    if not seed_vector_ids:
        return [], vector_base

    grouped: dict[int, list[int]] = {}
    for vector_id in range(len(vector_base.vectors)):
        grouped.setdefault(_find(parent, vector_id), []).append(vector_id)

    nets: list[dict[str, Any]] = []
    seeded_groups = [
        vector_ids
        for vector_ids in grouped.values()
        if seed_vector_ids.intersection(vector_ids)
    ]
    seeded_groups.sort(key=lambda ids: ids[0])
    seeded_group_index_by_root = {
        _find(parent, vector_ids[0]): group_index
        for group_index, vector_ids in enumerate(seeded_groups)
    }
    # A diagonal may terminate one seeded wire, but never joins two seeded
    # groups or carries connectivity onward through its other endpoint.
    for diagonal_id in sorted(terminal_diagonal_ids):
        adjacent_groups: set[int] = set()
        for endpoint_key in endpoint_keys_by_vector[diagonal_id]:
            for adjacent_id in vector_ids_by_endpoint.get(endpoint_key, []):
                if adjacent_id == diagonal_id or adjacent_id in terminal_diagonal_ids:
                    continue
                group_index = seeded_group_index_by_root.get(_find(parent, adjacent_id))
                if group_index is not None:
                    adjacent_groups.add(group_index)
        if adjacent_groups:
            seeded_groups[min(adjacent_groups)].append(diagonal_id)

    for net_id, vector_ids in enumerate(seeded_groups):
        vectors = [vector_base.vectors[vector_id] for vector_id in vector_ids]
        nets.append({
            "id": net_id,
            "vectors": vectors,
            "free_endpoints": [],
        })

    removed_vector_ids = {
        id(vector)
        for net in nets
        for vector in net["vectors"]
    }
    remaining = remove_matched_vector_ids(vector_base, removed_vector_ids)
    return _merge_and_recover_wire_vectors(nets, remaining)


def _merge_and_recover_wire_vectors(
    nets: list[dict[str, Any]],
    remaining: VectorBase,
) -> tuple[list[dict[str, Any]], VectorBase]:
    """Merge touching seeded nets and recover attached axis-aligned remnants."""
    if not nets:
        return nets, remaining

    remaining_candidates = [
        vector for vector in remaining.vectors if _is_axis_aligned_line(vector)
    ]
    net_count = len(nets)
    node_count = net_count + len(remaining_candidates)
    parent = list(range(node_count))
    rank = [0] * node_count
    geometries: list[LineString] = []
    owner_by_geometry: list[int] = []
    vector_by_geometry: list[PathBase] = []
    geometry_by_vector_id: dict[int, LineString] = {}

    for net_index, net in enumerate(nets):
        for vector in net["vectors"]:
            geometry = _wire_connection_geometry(vector)
            if geometry is None:
                continue
            geometry_by_vector_id[id(vector)] = geometry
            if _is_terminal_diagonal(vector):
                continue
            geometries.append(geometry)
            owner_by_geometry.append(net_index)
            vector_by_geometry.append(vector)
    for candidate_index, vector in enumerate(remaining_candidates):
        geometry = _wire_connection_geometry(vector)
        if geometry is None:
            continue
        geometry_by_vector_id[id(vector)] = geometry
        geometries.append(geometry)
        owner_by_geometry.append(net_count + candidate_index)
        vector_by_geometry.append(vector)

    if len(set(owner_by_geometry)) > 1:
        tree = STRtree(geometries)
        for geometry_index, vector in enumerate(vector_by_geometry):
            owner = owner_by_geometry[geometry_index]
            for endpoint in _wire_vector_endpoints(vector):
                point = ShapelyPoint(endpoint)
                for tree_result in tree.query(
                    point,
                    predicate="dwithin",
                    distance=WIRE_ENDPOINT_TOLERANCE_PT,
                ):
                    other_geometry_index = _strtree_result_index(tree_result)
                    if other_geometry_index == geometry_index:
                        continue
                    other_owner = owner_by_geometry[other_geometry_index]
                    if owner == other_owner:
                        continue
                    _union(parent, rank, owner, other_owner)

    net_indices_by_root: dict[int, list[int]] = {}
    candidate_indices_by_root: dict[int, list[int]] = {}
    for net_index in range(net_count):
        net_indices_by_root.setdefault(_find(parent, net_index), []).append(net_index)
    for candidate_index in range(len(remaining_candidates)):
        root = _find(parent, net_count + candidate_index)
        if root in net_indices_by_root:
            candidate_indices_by_root.setdefault(root, []).append(candidate_index)

    recovered_ids: set[int] = set()
    recovered_nets: list[dict[str, Any]] = []
    ordered_roots = sorted(net_indices_by_root, key=lambda root: net_indices_by_root[root][0])
    for net_id, root in enumerate(ordered_roots):
        vectors = [
            vector
            for net_index in net_indices_by_root[root]
            for vector in nets[net_index]["vectors"]
        ]
        recovered = [
            remaining_candidates[index]
            for index in candidate_indices_by_root.get(root, [])
        ]
        vectors.extend(recovered)
        recovered_ids.update(id(vector) for vector in recovered)
        recovered_nets.append({
            "id": net_id,
            "vectors": vectors,
            "free_endpoints": _wire_free_endpoints(vectors, geometry_by_vector_id),
            "titles": [],
            "descriptions": [],
        })

    return recovered_nets, remove_matched_vector_ids(remaining, recovered_ids)


def _wire_free_endpoints(
    vectors: Sequence[PathBase],
    geometry_by_vector_id: dict[int, LineString] | None = None,
) -> list[Point]:
    """Return degree-one nodes from the merged topology of a wire net."""
    geometries: list[LineString] = []
    for vector in vectors:
        geometry = geometry_by_vector_id.get(id(vector)) if geometry_by_vector_id is not None else None
        if geometry is None:
            geometry = _wire_connection_geometry(vector)
        if geometry is not None and not geometry.is_empty:
            geometries.append(geometry)
    if not geometries:
        return []

    # Self-snapping preserves the existing endpoint tolerance. unary_union then
    # removes duplicate/overlapping strokes and nodes every true intersection.
    linework = MultiLineString([list(geometry.coords) for geometry in geometries])
    merged = unary_union(snap(
        linework,
        linework,
        WIRE_ENDPOINT_TOLERANCE_PT,
    ))
    merged_lines = (
        list(merged.geoms)
        if merged.geom_type == "MultiLineString"
        else [merged]
    )

    degree_by_node: dict[tuple[int, int], int] = {}
    point_by_node: dict[tuple[int, int], Point] = {}
    for line in merged_lines:
        if line.geom_type != "LineString" or line.is_empty:
            continue
        coordinates = list(line.coords)
        for raw_endpoint in (coordinates[0], coordinates[-1]):
            endpoint = (float(raw_endpoint[0]), float(raw_endpoint[1]))
            node = _wire_point_key(endpoint)
            degree_by_node[node] = degree_by_node.get(node, 0) + 1
            point_by_node.setdefault(node, endpoint)

    free = [
        point_by_node[node]
        for node, degree in degree_by_node.items()
        if degree == 1
    ]
    return sorted(free, key=lambda point: (point[1], point[0]))


def _wire_connection_geometry(vector: PathBase) -> LineString | None:
    points = [(float(point[0]), float(point[1])) for point in vector.points]
    if len(points) < 2 or len(set(points)) < 2:
        return None
    if vector.type == "curve" and len(points) == 4:
        return _vector_geometry(vector)
    if vector.type in {"rect", "quad"} and len(points) >= 3:
        return LineString([*points, points[0]])
    return LineString(points)


def _is_axis_aligned_line(vector: PathBase) -> bool:
    points = _wire_vector_endpoints(vector)
    if vector.type != "line" or len(points) != 2:
        return False
    left, right = points
    dx = abs(right[0] - left[0])
    dy = abs(right[1] - left[1])
    return max(dx, dy) > WIRE_ENDPOINT_TOLERANCE_PT and (
        dx <= WIRE_ENDPOINT_TOLERANCE_PT or dy <= WIRE_ENDPOINT_TOLERANCE_PT
    )


def _is_terminal_diagonal(vector: PathBase) -> bool:
    points = _wire_vector_endpoints(vector)
    if vector.type != "line" or len(points) != 2:
        return False
    left, right = points
    dx = abs(right[0] - left[0])
    dy = abs(right[1] - left[1])
    return (
        dx > WIRE_ENDPOINT_TOLERANCE_PT
        and dy > WIRE_ENDPOINT_TOLERANCE_PT
        and dx * dx + dy * dy <= WIRE_TERMINAL_DIAGONAL_MAX_LENGTH_PT**2
    )


def _strtree_result_index(result: Any) -> int:
    if isinstance(result, Integral):
        return int(result)
    raise TypeError("STRtree index results require shapely>=2.0")


def extract_vector_elements(
    vector_base: VectorBase,
    symbol_list: Sequence[Sequence[PathBase]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], VectorBase]:
    elements: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []

    # step 1: symbol extraction
    symbols = [list(symbol) for symbol in symbol_list]
    if any(not symbol for symbol in symbols):
        raise ValueError("symbol_list must not contain empty symbols")
    if not vector_base.vectors:
        return [], [], vector_base

    vector_by_source_index = {
        vector_base.source_index(index): vector
        for index, vector in enumerate(vector_base.vectors)
    }
    candidates: list[dict[str, Any]] = []

    if symbols:
        matcher = VectorMatcher(vector_base, page_height_pt=1.0)
        matches_by_symbol = matcher.match_patterns(symbols)
        for symbol_id, (symbol, symbol_matches) in enumerate(zip(symbols, matches_by_symbol)):
            for match in symbol_matches:
                source_indices = frozenset(int(vector["index"]) for vector in match["vectors"])
                shape_vectors = [
                    vector_by_source_index[source_index]
                    for source_index in sorted(source_indices)
                ]
                candidates.append({
                    "symbol_id": symbol_id,
                    "symbol_vector_count": len(symbol),
                    "source_indices": source_indices,
                    "shape": shape_vectors,
                    "bbox": bbox_from_shapes(shape_vectors),
                })

    selected = _resolve_element_conflicts(candidates)
    selected.sort(key=lambda candidate: (
        candidate["bbox"][1],
        candidate["bbox"][0],
        candidate["symbol_id"],
    ))
    matched_indices = {
        source_index
        for candidate in selected
        for source_index in candidate["source_indices"]
    }

    for element_id, candidate in enumerate(selected):
        elements.append(
            _element_record(
                element_id=element_id,
                element_type="symbol",
                shape=candidate["shape"],
                bbox=candidate["bbox"],
                attributes={"symbol_id": candidate["symbol_id"]},
            )
        )

    # step 2: endpoints detection
    remaining_vectors = remove_matched_vectors(vector_base, matched_indices)
    endpoint_detector = VectorPinDetector(remaining_vectors)
    removed_vector_ids: set[int] = set()
    for region in endpoint_detector.detect_circle_pins():
        vectors = list(region.get("vectors", []))
        vector_ids = {id(vector) for vector in vectors}
        if not vector_ids or removed_vector_ids.intersection(vector_ids):
            continue
        elements.append(
            _element_record(
                element_id=len(elements),
                element_type="endpoint_circle",
                shape=vectors,
                bbox=region["bbox"],
            )
        )
        removed_vector_ids.update(vector_ids)
    remaining_vectors = remove_matched_vector_ids(remaining_vectors, removed_vector_ids)

    endpoint_detector = VectorPinDetector(remaining_vectors)
    removed_vector_ids = set()
    arrow_regions = []
    for region in endpoint_detector.detect_arrow_pins():
        vectors = list(region.get("vectors", []))
        vector_ids = {id(vector) for vector in vectors}
        if not vector_ids or removed_vector_ids.intersection(vector_ids):
            continue
        elements.append(
            _element_record(
                element_id=len(elements),
                element_type="arrow",
                shape=vectors,
                bbox=region["bbox"],
                attributes={
                    "direction": region["direction"],
                    "polygon": region["polygon"],
                },
            )
        )
        arrow_regions.append(region)
        removed_vector_ids.update(vector_ids)
    remaining_vectors = remove_arrow_vectors(
        remaining_vectors,
        arrow_regions,
        removed_vector_ids,
    )

    endpoint_detector = VectorPinDetector(remaining_vectors)
    removed_vector_ids = set()
    for region in endpoint_detector.detect_wire_mark():
        vectors = list(region.get("vectors", []))
        vector_ids = {id(vector) for vector in vectors}
        if not vector_ids or removed_vector_ids.intersection(vector_ids):
            continue
        elements.append(
            _element_record(
                element_id=len(elements),
                element_type="wire_mark",
                shape=vectors,
                bbox=region["bbox"],
                attributes={
                    "wire_vector": region["crossing_vector"],
                    "wire_vector_index": region["crossing_vector_index"],
                },
            )
        )
        removed_vector_ids.update(vector_ids)
    remaining_vectors = remove_matched_vector_ids(remaining_vectors, removed_vector_ids)

    # step 3: box detection and dashed detection
    box_detector = VectorBoxDetector(remaining_vectors)
    removed_vector_ids = set()
    for region in box_detector.detect_boxes():
        shape_vectors = list(region.get("vectors", []))
        vector_ids = {id(vector) for vector in shape_vectors}
        if not vector_ids or removed_vector_ids.intersection(vector_ids):
            continue
        # A box owns the area enclosed by its exterior ring. Interior rings
        # produced by polygonize are nested shapes, not holes in box ownership.
        box_polygon = Polygon(region["polygon"].exterior)
        element = _element_record(
            element_id=len(elements),
            element_type="box",
            shape=shape_vectors,
            bbox=region["bbox"],
            attributes={"polygon": box_polygon, "content": []},
        )
        elements.append(element)
        removed_vector_ids.update(vector_ids)
    remaining_vectors = remove_matched_vector_ids(remaining_vectors, removed_vector_ids)

    dashed_detector = VectorBoxDetector(remaining_vectors)
    removed_vector_ids = set()
    for region in dashed_detector.detect_dashed("dashed"):
        shape_vectors = list(region.get("vectors", []))
        vector_ids = {id(vector) for vector in shape_vectors}
        if not vector_ids or removed_vector_ids.intersection(vector_ids):
            continue
        dashed_polygon = Polygon(region["polygon"].exterior)
        element = _element_record(
            element_id=len(elements),
            element_type="box",
            shape=shape_vectors,
            bbox=region["bbox"],
            attributes={"polygon": dashed_polygon, "content": [], "line_style": "dashed"},
        )
        elements.append(element)
        removed_vector_ids.update(vector_ids)
    remaining_vectors = remove_matched_vector_ids(remaining_vectors, removed_vector_ids)

    group_detector = VectorBoxDetector(remaining_vectors)
    removed_vector_ids = set()
    for region in group_detector.detect_groups():
        shape_vectors = list(region.get("vectors", []))
        vector_ids = {id(vector) for vector in shape_vectors}
        if not vector_ids or removed_vector_ids.intersection(vector_ids):
            continue
        group_polygon = Polygon(region["polygon"].exterior)
        groups.append({
            "id": len(groups),
            "type": "group",
            "shape": shape_vectors,
            "bbox": dict(region["bbox"]),
            "attributes": {"polygon": group_polygon, "line_style": "dash_dotted"},
            "title": [],
            "descriptions": [],
        })
        removed_vector_ids.update(vector_ids)
    remaining_vectors = remove_matched_vector_ids(remaining_vectors, removed_vector_ids)
    remaining_vectors = assign_remaining_vectors_to_elements(remaining_vectors, elements)

    return elements, groups, remaining_vectors


def extract_vector_components(
    vector_base: VectorBase,
    symbol_list: Sequence[Sequence[PathBase]],
) -> tuple[list[dict[str, Any]], VectorBase]:
    """Compatibility adapter for callers migrating to ``extract_vector_elements``."""
    elements, _groups, remaining = extract_vector_elements(vector_base, symbol_list)
    return _legacy_component_view(elements), remaining


def _legacy_component_view(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the pre-v2 intermediate view without affecting final elements."""
    records = []
    for element in elements:
        record = dict(element)
        if element.get("attributes", {}).get("line_style") == "dashed":
            record["type"] = "dashed"
        records.append(record)
    return records


def _element_record(
    *,
    element_id: int,
    element_type: str,
    shape: list[PathBase],
    bbox: Any,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": element_id,
        "type": element_type,
        "shape": shape,
        "bbox": bbox_to_dict(bbox) if not isinstance(bbox, dict) else dict(bbox),
        "attributes": dict(attributes or {}),
        "title": [],
        "descriptions": [],
    }


def _wire_vector_endpoints(vector: PathBase) -> list[Point]:
    points = [(float(point[0]), float(point[1])) for point in vector.points]
    if not points:
        return []
    if len(points) == 1:
        return [points[0]]
    return [points[0], points[-1]]


def _is_wire_seed_vector(vector: PathBase) -> bool:
    points = _wire_vector_endpoints(vector)
    if vector.type != "line" or len(points) != 2:
        return False
    left, right = points
    dx = abs(right[0] - left[0])
    dy = abs(right[1] - left[1])
    return (
        (dx <= WIRE_ENDPOINT_TOLERANCE_PT or dy <= WIRE_ENDPOINT_TOLERANCE_PT)
        and max(dx, dy) >= WIRE_SEED_MIN_LENGTH_PT
    )


def _wire_point_key(point: Point) -> tuple[int, int]:
    return (
        int(round(point[0] / WIRE_ENDPOINT_TOLERANCE_PT)),
        int(round(point[1] / WIRE_ENDPOINT_TOLERANCE_PT)),
    )


def _find(parent: list[int], item: int) -> int:
    if parent[item] != item:
        parent[item] = _find(parent, parent[item])
    return parent[item]


def _union(parent: list[int], rank: list[int], left: int, right: int) -> bool:
    left_root = _find(parent, left)
    right_root = _find(parent, right)
    if left_root == right_root:
        return False
    if rank[left_root] < rank[right_root]:
        left_root, right_root = right_root, left_root
    parent[right_root] = left_root
    if rank[left_root] == rank[right_root]:
        rank[left_root] += 1
    return True


def _resolve_element_conflicts(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            -candidate["symbol_vector_count"],
            candidate["symbol_id"],
            candidate["bbox"][1],
            candidate["bbox"][0],
        ),
    )
    selected: list[dict[str, Any]] = []
    occupied_indices: set[int] = set()
    for candidate in ordered:
        if occupied_indices.intersection(candidate["source_indices"]):
            continue
        selected.append(candidate)
        occupied_indices.update(candidate["source_indices"])
    return selected


def remove_matched_vectors(vector_base: VectorBase, removed: set[int]) -> VectorBase:
    vectors: list[PathBase] = []
    source_indices: list[int] = []
    for index, vector in enumerate(vector_base.vectors):
        source_index = vector_base.source_index(index)
        if source_index in removed:
            continue
        vectors.append(vector)
        source_indices.append(source_index)
    return VectorBase(vectors, source_indices=source_indices)


def remove_matched_vector_ids(vector_base: VectorBase, removed: set[int]) -> VectorBase:
    vectors: list[PathBase] = []
    source_indices: list[int] = []
    for index, vector in enumerate(vector_base.vectors):
        if id(vector) in removed:
            continue
        vectors.append(vector)
        source_indices.append(vector_base.source_index(index))
    return VectorBase(vectors, source_indices=source_indices)


def remove_arrow_vectors(
    vector_base: VectorBase,
    arrow_regions: Sequence[dict[str, Any]],
    removed: set[int],
    *,
    boundary_tolerance: float = PARSER_CONFIG.diagram.arrow_removal_boundary_tolerance_pt,
) -> VectorBase:
    """Remove detected arrows and duplicate stroke outlines on the same boundary."""
    boundaries = []
    for region in arrow_regions:
        points = [(float(point[0]), float(point[1])) for point in region.get("points", [])]
        if len(points) < 3:
            continue
        polygon = Polygon(points)
        if not polygon.is_empty:
            boundaries.append(polygon.boundary.buffer(boundary_tolerance))

    vectors: list[PathBase] = []
    source_indices: list[int] = []
    for index, vector in enumerate(vector_base.vectors):
        if id(vector) in removed:
            continue
        geometry = _vector_line_geometry(vector)
        if geometry is not None and any(boundary.covers(geometry) for boundary in boundaries):
            continue
        vectors.append(vector)
        source_indices.append(vector_base.source_index(index))
    return VectorBase(vectors, source_indices=source_indices)


def _vector_line_geometry(vector: PathBase) -> LineString | None:
    points = [(float(point[0]), float(point[1])) for point in vector.points]
    if vector.type == "line" and len(points) == 2:
        return LineString(points)
    if vector.type in {"rect", "quad"} and len(points) >= 3:
        return LineString([*points, points[0]])
    return None


def assign_remaining_vectors_to_boxes(
    vector_base: VectorBase,
    containers: Sequence[tuple[dict[str, Any], Polygon]],
) -> VectorBase:
    """Assign vectors to the deepest nested closed element containing them."""
    if not containers or not vector_base.vectors:
        return vector_base

    # For nested polygons, smaller area is equivalent to deeper hierarchy.
    ordered_containers = sorted(containers, key=lambda item: item[1].area)
    container_tree = STRtree([polygon for _, polygon in ordered_containers])
    remaining: list[PathBase] = []
    source_indices: list[int] = []
    for index, vector in enumerate(vector_base.vectors):
        geometry = _vector_geometry(vector)
        owner_indices = container_tree.query(geometry, predicate="covered_by")
        owner_index = min(
            (_strtree_result_index(result) for result in owner_indices),
            default=None,
        )
        if owner_index is None:
            remaining.append(vector)
            source_indices.append(vector_base.source_index(index))
            continue
        owner = ordered_containers[owner_index][0]
        owner.setdefault("attributes", {}).setdefault("content", []).append(vector)
        shape = owner.setdefault("shape", [])
        if all(id(existing) != id(vector) for existing in shape):
            shape.append(vector)
    return VectorBase(remaining, source_indices=source_indices)


def assign_remaining_vectors_to_elements(
    vector_base: VectorBase,
    elements: Sequence[dict[str, Any]],
) -> VectorBase:
    """Assign every unowned vector to the smallest element covering it."""
    owners: list[tuple[dict[str, Any], Polygon]] = []
    for element in elements:
        element.setdefault("attributes", {}).setdefault("content", [])
        polygon = element["attributes"].get("polygon")
        if not isinstance(polygon, Polygon) or polygon.is_empty:
            polygon = box(*coerce_bbox(element["bbox"]))
        if polygon.is_empty:
            continue
        owners.append((element, Polygon(polygon.exterior)))
    return assign_remaining_vectors_to_boxes(vector_base, owners)


def _vector_geometry(vector: PathBase) -> LineString:
    points = [(float(point[0]), float(point[1])) for point in vector.points]
    if vector.type == "curve" and len(points) == 4:
        p0, p1, p2, p3 = points
        sampled = []
        for step in range(17):
            t = step / 16.0
            u = 1.0 - t
            sampled.append(
                (
                    u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
                )
            )
        return LineString(sampled)
    if vector.type in {"rect", "quad"} and len(points) >= 3:
        return LineString([*points, points[0]])
    return LineString(points)
