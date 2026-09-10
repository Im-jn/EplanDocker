"""Extract symbol definitions from Symbol Overview tables."""

from __future__ import annotations

from typing import Any, Sequence

from shapely.geometry import Point, box

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase, TextBase, VectorBase
from pdf_parser.tools.vector_box import VectorBoxDetector
from pdf_parser.tools.vector_entity import VectorDisjointSet
from pdf_parser.tools.vector_matcher import VectorMatcher
from pdf_parser.utils import bbox_from_shapes, coerce_bbox


BBox = tuple[float, float, float, float]
COORDINATE_TOLERANCE_PT = PARSER_CONFIG.symbols.coordinate_tolerance_pt
MIN_BOUNDARY_COVERAGE_RATIO = PARSER_CONFIG.symbols.min_boundary_coverage_ratio


def extract_symbols(vector_base: VectorBase, text_base: TextBase) -> dict[str, Any]:
    """Return unique symbols and records referencing them by index."""
    if not isinstance(vector_base, VectorBase):
        raise TypeError("vector_base must be a VectorBase")
    if not isinstance(text_base, TextBase):
        raise TypeError("text_base must be a TextBase")
    if not vector_base.vectors:
        return {"symbols": [], "records": []}

    records: list[dict[str, Any]] = []
    for table_vectors, table_bbox in _table_entities(vector_base):
        faces = VectorBoxDetector(VectorBase(table_vectors)).detect_cells()
        grid = _logical_grid(faces, table_bbox)
        if len(grid) < 2 or not grid[0]:
            continue

        x_boundaries, y_boundaries = _grid_boundaries(grid)
        content = [
            [
                {
                    "bbox": cell,
                    "vectors": _vectors_in_cell(vector_base, cell, x_boundaries, y_boundaries),
                    "texts": text_base.in_region(box(*cell)),
                }
                for cell in row
            ]
            for row in grid
        ]
        content = _split_table_cells(content)
        symbol_column = _symbol_column(content)
        if symbol_column is None:
            continue

        for row in content:
            symbols = row[symbol_column]["vectors"]
            if not symbols:
                continue
            records.append({
                "symbol": symbols,
                "type": _join_text(row[:symbol_column]),
                "descriptions": _join_text(row[symbol_column + 1 :]),
            })
    return _deduplicate_symbols(records, vector_base)


def merge_symbol_results(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merge extraction results and remap records to globally unique symbols."""
    if not results:
        return {"symbols": [], "records": []}

    symbol_groups: list[list[PathBase]] = []
    for result_index, result in enumerate(results):
        if not isinstance(result, dict):
            raise TypeError(f"results[{result_index}] must be a dict")
        if "symbols" not in result or "records" not in result:
            raise ValueError(f"results[{result_index}] must contain symbols and records")
        for symbol_index, symbol in enumerate(result["symbols"]):
            group = list(symbol)
            if not group:
                raise ValueError(f"results[{result_index}]['symbols'][{symbol_index}] must not be empty")
            symbol_groups.append(group)

    if not symbol_groups:
        if any(result["records"] for result in results):
            raise ValueError("records cannot reference an empty symbols list")
        return {"symbols": [], "records": []}

    matcher = VectorMatcher(
        VectorBase([vector for group in symbol_groups for vector in group]),
        page_height_pt=1.0,
    )
    unique_symbols: list[list[PathBase]] = []
    merged_records: list[dict[str, Any]] = []

    for result_index, result in enumerate(results):
        index_map: dict[int, int] = {}
        for old_index, symbol in enumerate(result["symbols"]):
            group = list(symbol)
            new_index = next(
                (
                    index
                    for index, candidate in enumerate(unique_symbols)
                    if _same_symbol(candidate, group, matcher)
                ),
                None,
            )
            if new_index is None:
                new_index = len(unique_symbols)
                unique_symbols.append(group)
            index_map[old_index] = new_index

        for record_index, record in enumerate(result["records"]):
            if not isinstance(record, dict):
                raise TypeError(f"results[{result_index}]['records'][{record_index}] must be a dict")
            old_index = record.get("symbol")
            if not isinstance(old_index, int) or old_index not in index_map:
                raise ValueError(
                    f"results[{result_index}]['records'][{record_index}] has an invalid symbol index"
                )
            merged_record = dict(record)
            merged_record["symbol"] = index_map[old_index]
            merged_records.append(merged_record)

    return {"symbols": unique_symbols, "records": merged_records}


def _deduplicate_symbols(
    records: list[dict[str, Any]],
    vector_base: VectorBase,
) -> dict[str, Any]:
    unique_symbols: list[list[PathBase]] = []
    indexed_records: list[dict[str, Any]] = []
    matcher = VectorMatcher(vector_base, page_height_pt=1.0)

    for record in records:
        symbol = list(record["symbol"])
        symbol_index = next(
            (
                index
                for index, candidate in enumerate(unique_symbols)
                if _same_symbol(candidate, symbol, matcher)
            ),
            None,
        )
        if symbol_index is None:
            symbol_index = len(unique_symbols)
            unique_symbols.append(symbol)
        indexed_records.append({
            "symbol": symbol_index,
            "type": record["type"],
            "descriptions": record["descriptions"],
        })

    return {"symbols": unique_symbols, "records": indexed_records}


def _same_symbol(
    target: Sequence[PathBase],
    candidate: Sequence[PathBase],
    matcher: VectorMatcher,
) -> bool:
    if len(target) != len(candidate):
        return False
    if sorted(str(vector.type) for vector in target) != sorted(str(vector.type) for vector in candidate):
        return False

    target_bbox = bbox_from_shapes(target)
    candidate_bbox = bbox_from_shapes(candidate)
    target_width = target_bbox[2] - target_bbox[0]
    target_height = target_bbox[3] - target_bbox[1]
    candidate_width = candidate_bbox[2] - candidate_bbox[0]
    candidate_height = candidate_bbox[3] - candidate_bbox[1]
    if (
        abs(target_width - candidate_width) > COORDINATE_TOLERANCE_PT
        or abs(target_height - candidate_height) > COORDINATE_TOLERANCE_PT
    ):
        return False

    translation = (
        candidate_bbox[0] - target_bbox[0],
        candidate_bbox[1] - target_bbox[1],
    )
    return matcher.compare_shape_groups(
        target,
        candidate,
        rotation_degrees=0.0,
        scale=1.0,
        translation=translation,
        tolerance=COORDINATE_TOLERANCE_PT,
        type_sensitive=True,
    )


def _table_entities(vector_base: VectorBase) -> list[tuple[list[PathBase], BBox]]:
    entities = VectorDisjointSet(
        vector_base,
        merge_contained=False,
        merge_nearby=False,
    ).entity_records()
    outer: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        bbox_value = coerce_bbox(entity["bbox"])
        if any(
            index != other_index
            and _strictly_contains(coerce_bbox(other["bbox"]), bbox_value)
            for other_index, other in enumerate(entities)
        ):
            continue
        outer.append(entity)

    tables: list[tuple[list[PathBase], BBox]] = []
    for entity in outer:
        bbox_value = coerce_bbox(entity["bbox"])
        vectors = list(entity["vectors"])
        if VectorBoxDetector(VectorBase(vectors)).detect_cells():
            tables.append((vectors, bbox_value))
    tables.sort(key=lambda item: (item[1][1], item[1][0]))
    return tables


def _logical_grid(faces: list[dict[str, Any]], table_bbox: BBox) -> list[list[BBox]]:
    if not faces:
        return []
    face_bboxes = [coerce_bbox(face["bbox"]) for face in faces]
    x_values = _main_boundaries(
        face_bboxes,
        axis=0,
        outer=(table_bbox[0], table_bbox[2]),
        coverage_outer=(table_bbox[1], table_bbox[3]),
    )
    y_values = _main_boundaries(
        face_bboxes,
        axis=1,
        outer=(table_bbox[1], table_bbox[3]),
        coverage_outer=(table_bbox[0], table_bbox[2]),
    )
    if len(x_values) < 2 or len(y_values) < 2:
        return []
    return [
        [
            (x_values[column], y_values[row], x_values[column + 1], y_values[row + 1])
            for column in range(len(x_values) - 1)
        ]
        for row in range(len(y_values) - 1)
    ]


def _main_boundaries(
    bboxes: list[BBox],
    *,
    axis: int,
    outer: tuple[float, float],
    coverage_outer: tuple[float, float],
) -> list[float]:
    values = [bbox[axis] for bbox in bboxes] + [bbox[axis + 2] for bbox in bboxes]
    clustered = _cluster_coordinates(values)
    span = max(coverage_outer[1] - coverage_outer[0], 0.0)
    selected = [
        center
        for center in clustered
        if _boundary_coverage(bboxes, axis=axis, coordinate=center, outer=coverage_outer)
        >= span * MIN_BOUNDARY_COVERAGE_RATIO
    ]
    selected.extend(outer)
    return _cluster_coordinates(selected)


def _boundary_coverage(
    bboxes: Sequence[BBox],
    *,
    axis: int,
    coordinate: float,
    outer: tuple[float, float],
) -> float:
    """Return the union length covered by face edges at one grid coordinate."""
    cross_axis = 1 - axis
    intervals = sorted(
        (
            max(bbox[cross_axis], outer[0]),
            min(bbox[cross_axis + 2], outer[1]),
        )
        for bbox in bboxes
        if (
            abs(bbox[axis] - coordinate) <= COORDINATE_TOLERANCE_PT
            or abs(bbox[axis + 2] - coordinate) <= COORDINATE_TOLERANCE_PT
        )
        and bbox[cross_axis] < outer[1]
        and bbox[cross_axis + 2] > outer[0]
    )
    if not intervals:
        return 0.0

    coverage = 0.0
    start, end = intervals[0]
    for next_start, next_end in intervals[1:]:
        if next_start <= end + COORDINATE_TOLERANCE_PT:
            end = max(end, next_end)
        else:
            coverage += max(end - start, 0.0)
            start, end = next_start, next_end
    return coverage + max(end - start, 0.0)


def _cluster_coordinates(values: Sequence[float]) -> list[float]:
    clusters: list[list[float]] = []
    for value in sorted(float(item) for item in values):
        if clusters and abs(value - sum(clusters[-1]) / len(clusters[-1])) <= COORDINATE_TOLERANCE_PT:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _vectors_in_cell(
    vector_base: VectorBase,
    cell: BBox,
    x_boundaries: Sequence[float],
    y_boundaries: Sequence[float],
) -> list[PathBase]:
    region = box(*cell)
    result: list[PathBase] = []
    for vector in vector_base.vectors:
        bbox_value = coerce_bbox(vector.bbox)
        center = Point((bbox_value[0] + bbox_value[2]) / 2.0, (bbox_value[1] + bbox_value[3]) / 2.0)
        if region.covers(center) and not _is_grid_vector(bbox_value, x_boundaries, y_boundaries):
            result.append(vector)
    return result


def _split_table_cells(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Expand rows containing full-width or full-height cell splitters."""
    expanded: list[list[dict[str, Any]]] = []
    for row in rows:
        splitters = [_cell_splitters(cell) for cell in row]
        has_horizontal = any(horizontal is not None for horizontal, _ in splitters)
        has_vertical = any(vertical is not None for _, vertical in splitters)
        horizontal_sides = (0, 1) if has_horizontal else (None,)
        vertical_sides = (0, 1) if has_vertical else (None,)

        for horizontal_side in horizontal_sides:
            for vertical_side in vertical_sides:
                expanded.append([
                    _split_cell_content(
                        cell,
                        horizontal=cell_splitters[0],
                        horizontal_side=horizontal_side,
                        vertical=cell_splitters[1],
                        vertical_side=vertical_side,
                    )
                    for cell, cell_splitters in zip(row, splitters)
                ])
    return expanded


def _cell_splitters(cell: dict[str, Any]) -> tuple[PathBase | None, PathBase | None]:
    x0, y0, x1, y1 = coerce_bbox(cell["bbox"])
    width = x1 - x0
    height = y1 - y0
    horizontal: list[PathBase] = []
    vertical: list[PathBase] = []

    for vector in cell["vectors"]:
        if vector.type != "line":
            continue
        vx0, vy0, vx1, vy1 = coerce_bbox(vector.bbox)
        vector_width = vx1 - vx0
        vector_height = vy1 - vy0
        if (
            vector_height <= COORDINATE_TOLERANCE_PT
            and abs(vector_width - width) <= COORDINATE_TOLERANCE_PT
            and y0 + COORDINATE_TOLERANCE_PT < (vy0 + vy1) / 2.0 < y1 - COORDINATE_TOLERANCE_PT
        ):
            horizontal.append(vector)
        elif (
            vector_width <= COORDINATE_TOLERANCE_PT
            and abs(vector_height - height) <= COORDINATE_TOLERANCE_PT
            and x0 + COORDINATE_TOLERANCE_PT < (vx0 + vx1) / 2.0 < x1 - COORDINATE_TOLERANCE_PT
        ):
            vertical.append(vector)

    horizontal.sort(key=lambda vector: coerce_bbox(vector.bbox)[1])
    vertical.sort(key=lambda vector: coerce_bbox(vector.bbox)[0])
    return (
        horizontal[0] if horizontal else None,
        vertical[0] if vertical else None,
    )


def _split_cell_content(
    cell: dict[str, Any],
    *,
    horizontal: PathBase | None,
    horizontal_side: int | None,
    vertical: PathBase | None,
    vertical_side: int | None,
) -> dict[str, Any]:
    horizontal_coordinate = _vector_center(horizontal, axis=1) if horizontal is not None else None
    vertical_coordinate = _vector_center(vertical, axis=0) if vertical is not None else None
    splitter_ids = {id(vector) for vector in (horizontal, vertical) if vector is not None}

    vectors = [
        vector
        for vector in cell["vectors"]
        if id(vector) not in splitter_ids
        and _belongs_to_partition(
            coerce_bbox(vector.bbox),
            horizontal_coordinate=horizontal_coordinate,
            horizontal_side=horizontal_side,
            vertical_coordinate=vertical_coordinate,
            vertical_side=vertical_side,
        )
    ]
    texts = [
        text
        for text in cell["texts"]
        if _belongs_to_partition(
            coerce_bbox(text["location"]),
            horizontal_coordinate=horizontal_coordinate,
            horizontal_side=horizontal_side,
            vertical_coordinate=vertical_coordinate,
            vertical_side=vertical_side,
        )
    ]

    x0, y0, x1, y1 = coerce_bbox(cell["bbox"])
    if horizontal_coordinate is not None and horizontal_side is not None:
        if horizontal_side == 0:
            y1 = horizontal_coordinate
        else:
            y0 = horizontal_coordinate
    if vertical_coordinate is not None and vertical_side is not None:
        if vertical_side == 0:
            x1 = vertical_coordinate
        else:
            x0 = vertical_coordinate
    return {"bbox": (x0, y0, x1, y1), "vectors": vectors, "texts": texts}


def _belongs_to_partition(
    bbox_value: BBox,
    *,
    horizontal_coordinate: float | None,
    horizontal_side: int | None,
    vertical_coordinate: float | None,
    vertical_side: int | None,
) -> bool:
    center_x = (bbox_value[0] + bbox_value[2]) / 2.0
    center_y = (bbox_value[1] + bbox_value[3]) / 2.0
    if horizontal_coordinate is not None and horizontal_side is not None:
        if (center_y <= horizontal_coordinate) != (horizontal_side == 0):
            return False
    if vertical_coordinate is not None and vertical_side is not None:
        if (center_x <= vertical_coordinate) != (vertical_side == 0):
            return False
    return True


def _vector_center(vector: PathBase, *, axis: int) -> float:
    bbox_value = coerce_bbox(vector.bbox)
    return (bbox_value[axis] + bbox_value[axis + 2]) / 2.0


def _is_grid_vector(bbox_value: BBox, x_boundaries: Sequence[float], y_boundaries: Sequence[float]) -> bool:
    x0, y0, x1, y1 = bbox_value
    if abs(x1 - x0) <= COORDINATE_TOLERANCE_PT:
        return any(abs(x0 - boundary) <= COORDINATE_TOLERANCE_PT for boundary in x_boundaries)
    if abs(y1 - y0) <= COORDINATE_TOLERANCE_PT:
        return any(abs(y0 - boundary) <= COORDINATE_TOLERANCE_PT for boundary in y_boundaries)
    return False


def _symbol_column(rows: list[list[dict[str, Any]]]) -> int | None:
    if not rows or not rows[0]:
        return None
    scores = [
        sum(bool(row[column]["vectors"]) for row in rows[1:])
        for column in range(len(rows[0]))
    ]
    best = max(range(len(scores)), key=scores.__getitem__)
    return best if scores[best] else None


def _join_text(cells: Sequence[dict[str, Any]]) -> str:
    texts = [text for cell in cells for text in cell["texts"] if text["text"]]
    texts.sort(key=lambda text: (coerce_bbox(text["location"])[1], coerce_bbox(text["location"])[0]))
    return " ".join(text["text"] for text in texts)


def _grid_boundaries(grid: list[list[BBox]]) -> tuple[list[float], list[float]]:
    x_values = [grid[0][0][0], *(cell[2] for cell in grid[0])]
    y_values = [grid[0][0][1], *(row[0][3] for row in grid)]
    return x_values, y_values


def _strictly_contains(outer: BBox, inner: BBox) -> bool:
    covers = outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]
    return covers and any(abs(left - right) > COORDINATE_TOLERANCE_PT for left, right in zip(outer, inner))
