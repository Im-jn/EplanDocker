from __future__ import annotations

import argparse
import html
import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    from ..pages_manager import PdfPageManager, VectorBase
    from ..pages_manager.page_class import coerce_bbox
    from ..pages_manager.split_page import SplitPageDetector
    from ..tools.vector_box import VectorBoxDetector
    from ..tools.vector_visualize import render_vector_text_png
    from ..utils import resolve_repo_relative
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from pages_manager import PdfPageManager, VectorBase
    from pages_manager.page_class import coerce_bbox
    from pages_manager.split_page import SplitPageDetector
    from tools.vector_box import VectorBoxDetector
    from tools.vector_visualize import render_vector_text_png
    from utils import resolve_repo_relative


Point = tuple[float, float]
BBox = tuple[float, float, float, float]

IGNORED_SUBCELL_VECTOR_TYPES = {"quad", "curve"}
DIAGONAL_SUBCELL_TYPES = {"left_diagonal", "right_diagonal"}


def extract_table(content: dict[str, Any]) -> dict[str, Any]:
    """Detect cells and return text spans from the information region."""
    detection_vectors = _table_detection_vectors(content["vectors"])
    raw_cells = VectorBoxDetector(detection_vectors).detect_cells()
    return {
        "raw_cells": raw_cells,
        "texts": content["text"].to_list(),
        "vectors": content["vectors"].to_list(),
    }


def reconstruct_table(
    raw_cells: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    vectors: Any | None = None,
) -> dict[str, Any]:
    """Build a structured table only from detected cell bboxes and text locations."""
    cells = _cells_from_bboxes(raw_cells)
    table = _table_from_cells(cells)
    _assign_text_to_cells(texts, table["cells"])
    if vectors is not None:
        _attach_subcell_boundaries(table["cells"], _vector_records(vectors))
        for cell in table["cells"]:
            _assign_subcell_texts(cell)
    return {
        "html": _generate_html_table(table),
        "cells": table["cells"],
        "rows": table["rows"],
        "columns": table["columns"],
        "raw_cells": raw_cells,
    }


def _cells_from_bboxes(raw_cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = []
    for source in raw_cells:
        try:
            x0, y0, x1, y1 = _cell_bbox(source)
        except Exception:
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        cells.append(
            {
                "bbox": (x0, y0, x1, y1),
                "text": "",
                "texts": [],
                "source": source,
            }
        )
    return cells


def _table_from_cells(cells: list[dict[str, Any]]) -> dict[str, Any]:
    if not cells:
        return _empty_table()

    x_edges = _unique_edges([edge for cell in cells for edge in (_cell_bbox(cell)[0], _cell_bbox(cell)[2])])
    y_edges = _unique_edges([edge for cell in cells for edge in (_cell_bbox(cell)[1], _cell_bbox(cell)[3])])
    if len(x_edges) < 2 or len(y_edges) < 2:
        return _empty_table()

    table_cells = []
    for cell in cells:
        x0, y0, x1, y1 = _cell_bbox(cell)
        col_start = x_edges.index(float(x0))
        col_end = x_edges.index(float(x1))
        row_start = y_edges.index(float(y0))
        row_end = y_edges.index(float(y1))
        if col_end <= col_start or row_end <= row_start:
            continue

        normalized = dict(cell)
        normalized["bbox"] = (x_edges[col_start], y_edges[row_start], x_edges[col_end], y_edges[row_end])
        normalized["row"] = row_start
        normalized["col"] = col_start
        normalized["rowspan"] = row_end - row_start
        normalized["colspan"] = col_end - col_start
        table_cells.append(normalized)

    table_cells.sort(key=lambda cell: (cell["row"], cell["col"], _cell_bbox(cell)[1], _cell_bbox(cell)[0]))
    return {
        "rows": _edge_intervals(y_edges),
        "columns": _edge_intervals(x_edges),
        "cells": table_cells,
    }


def _assign_text_to_cells(texts: list[dict[str, Any]], cells: list[dict[str, Any]]) -> None:
    """Assign each text span to the cell with the largest bbox overlap."""
    for text in texts:
        text_bbox = _text_bbox(text)
        candidates = []
        for cell in cells:
            overlap = _bbox_overlap_area(text_bbox, _cell_bbox(cell))
            if overlap > 0.0:
                candidates.append((overlap, cell))
        if not candidates:
            continue

        _, target = max(
            candidates,
            key=lambda item: (
                item[0],
                -item[1].get("rowspan", 1) * item[1].get("colspan", 1),
                -_bbox_area(_cell_bbox(item[1])),
            ),
        )
        target["texts"].append(text)

    for cell in cells:
        cell["texts"].sort(key=lambda item: (_text_bbox(item)[1], _text_bbox(item)[0]))
        cell["text"] = _format_cell_text(cell["texts"])


def _generate_html_table(table: dict[str, Any]) -> str:
    rows = len(table["rows"])
    cols = len(table["columns"])
    anchors = {(cell["row"], cell["col"]): cell for cell in table["cells"]}
    covered = _covered_slots(table["cells"])
    html_lines = ["<table border=\"1\" cellspacing=\"0\" cellpadding=\"4\">"]

    for row_index in range(rows):
        row_anchor_cols = [col for row, col in anchors if row == row_index]
        last_anchor_col = max(row_anchor_cols) if row_anchor_cols else -1
        html_lines.append("  <tr>")
        for col_index in range(cols):
            if (row_index, col_index) in covered and (row_index, col_index) not in anchors:
                continue

            cell = anchors.get((row_index, col_index))
            if cell is None:
                if col_index <= last_anchor_col:
                    html_lines.append("    <td></td>")
                continue

            html_lines.append(f"    <td{_html_cell_attrs(cell)}>{_html_cell_content(cell)}</td>")
        html_lines.append("  </tr>")
    html_lines.append("</table>")
    return "\n".join(html_lines)


def _html_cell_attrs(cell: dict[str, Any]) -> str:
    attrs = []
    if cell["rowspan"] > 1:
        attrs.append(f"rowspan=\"{cell['rowspan']}\"")
    if cell["colspan"] > 1:
        attrs.append(f"colspan=\"{cell['colspan']}\"")
    return f" {' '.join(attrs)}" if attrs else ""


def _html_cell_content(cell: dict[str, Any]) -> str:
    subcells = cell.get("subcells") or []
    if not subcells:
        return html.escape(cell.get("text", ""))

    parts = []
    for subcell in subcells:
        label = subcell["label"]
        first = subcell["first_text"]
        second = subcell["second_text"]
        parts.append(f"{html.escape(first)} {html.escape(label)} {html.escape(second)}")
    return "<br>".join(parts)


def _attach_subcell_boundaries(cells: list[dict[str, Any]], vectors: list[dict[str, Any]]) -> None:
    for cell in cells:
        lines = _subcell_boundaries_for_cell(cell, vectors)
        cell["subcell_boundaries"] = lines
        cell["subcells"] = []


def _subcell_boundaries_for_cell(
    cell: dict[str, Any],
    vectors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(cell.get("texts", [])) < 2:
        return []

    bbox = _cell_bbox(cell)
    source_indices = set(cell.get("source", {}).get("vector_indices", []))
    candidates = []
    seen = set()
    for vector in vectors:
        if vector.get("type") in IGNORED_SUBCELL_VECTOR_TYPES:
            continue
        vector_index = int(vector.get("index", -1))
        if vector_index in source_indices:
            continue
        for start, end in _vector_segments(vector):
            kind = _subcell_boundary_kind(start, end)
            if kind is None:
                continue
            if not _segment_inside_cell(start, end, bbox):
                continue
            if _is_cell_outer_boundary(start, end, bbox):
                continue
            if kind in DIAGONAL_SUBCELL_TYPES and not _diagonal_touches_cell_boundary(start, end, bbox):
                continue
            length = math.dist(start, end)
            if length <= 0.0:
                continue
            key = _subcell_boundary_key(kind, start, end)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_subcell_boundary_record(kind, start, end, vector_index, length))
    candidates.sort(key=lambda line: (_line_sort_key(line), line["vector_index"]))
    return candidates


def _subcell_boundary_key(kind: str, start: Point, end: Point) -> tuple[Any, ...]:
    x0, y0, x1, y1 = _segment_bbox(start, end)
    return (kind, x0, y0, x1, y1)


def _subcell_boundary_record(
    kind: str,
    start: Point,
    end: Point,
    vector_index: int,
    length: float,
) -> dict[str, Any]:
    return {
        "type": kind,
        "label": f"<{kind}>",
        "points": (start, end),
        "bbox": _segment_bbox(start, end),
        "vector_index": vector_index,
        "length": length,
    }


def _vector_records(vectors: Any) -> list[dict[str, Any]]:
    if hasattr(vectors, "to_list"):
        return vectors.to_list()
    return list(vectors)


def _vector_segments(vector: dict[str, Any]) -> list[tuple[Point, Point]]:
    points = [(float(point[0]), float(point[1])) for point in vector.get("points", [])]
    if len(points) < 2:
        return []
    if vector.get("type") in {"rect", "quad"} and len(points) >= 4:
        closed = [*points[:4], points[0]]
        return list(zip(closed, closed[1:]))
    if vector.get("type") == "line" and len(points) == 2:
        return [(points[0], points[1])]
    return list(zip(points, points[1:]))


def _subcell_boundary_kind(
    start: Point,
    end: Point,
) -> str | None:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0.0 and dy != 0.0:
        return "vertical_subcell"
    if dy == 0.0 and dx != 0.0:
        return "horrizontal_subcell"
    if dx == 0.0 or dy == 0.0:
        return None

    left, right = (start, end) if start[0] <= end[0] else (end, start)
    return "right_diagonal" if right[1] > left[1] else "left_diagonal"


def _segment_inside_cell(
    start: Point,
    end: Point,
    bbox: BBox,
) -> bool:
    x0, y0, x1, y1 = bbox
    return all(
        x0 <= point[0] <= x1
        and y0 <= point[1] <= y1
        for point in (start, end)
    )


def _is_cell_outer_boundary(
    start: Point,
    end: Point,
    bbox: BBox,
) -> bool:
    x0, y0, x1, y1 = bbox
    if start[0] == end[0] and start[0] in {x0, x1}:
        return True
    if start[1] == end[1] and start[1] in {y0, y1}:
        return True
    return False


def _diagonal_touches_cell_boundary(
    start: Point,
    end: Point,
    bbox: BBox,
) -> bool:
    start_sides = _cell_boundary_sides(start, bbox)
    end_sides = _cell_boundary_sides(end, bbox)
    if not start_sides or not end_sides:
        return False
    if start_sides == end_sides:
        return False

    touched_sides = start_sides | end_sides
    touches_horizontal = bool(touched_sides & {"top", "bottom"})
    touches_vertical = bool(touched_sides & {"left", "right"})
    return touches_horizontal and touches_vertical


def _cell_boundary_sides(
    point: Point,
    bbox: BBox,
) -> set[str]:
    x0, y0, x1, y1 = bbox
    sides = set()
    if point[0] == x0:
        sides.add("left")
    if point[0] == x1:
        sides.add("right")
    if point[1] == y0:
        sides.add("top")
    if point[1] == y1:
        sides.add("bottom")
    return sides


def _segment_bbox(
    start: Point,
    end: Point,
) -> BBox:
    return (
        min(start[0], end[0]),
        min(start[1], end[1]),
        max(start[0], end[0]),
        max(start[1], end[1]),
    )


def _line_sort_key(line: dict[str, Any]) -> tuple[float, float, float]:
    x0, y0, x1, y1 = line["bbox"]
    return (y0, x0, -(x1 - x0 + y1 - y0))


def _format_cell_text(texts: list[dict[str, Any]]) -> str:
    lines = _group_text_lines(texts)
    ordered_texts = [
        text
        for line in lines
        for text in sorted(line, key=lambda item: (_text_bbox(item)[0], item.get("index", 0)))
    ]
    return " ".join(text.get("text", "") for text in ordered_texts if text.get("text")).strip()


def _assign_subcell_texts(cell: dict[str, Any]) -> None:
    subcells = []
    split_signatures = set()
    for boundary in cell.get("subcell_boundaries", []):
        first_texts, second_texts = _split_texts_by_boundary(cell.get("texts", []), boundary)
        if not first_texts or not second_texts:
            continue
        first_texts, second_texts = _order_split_text_groups(first_texts, second_texts, boundary)
        signature = (
            tuple(_text_identity(text) for text in first_texts),
            tuple(_text_identity(text) for text in second_texts),
        )
        reverse_signature = (signature[1], signature[0])
        if signature in split_signatures or reverse_signature in split_signatures:
            continue
        split_signatures.add(signature)
        subcells.append(
            {
                **boundary,
                "first_texts": first_texts,
                "second_texts": second_texts,
                "first_text": _format_cell_text(first_texts),
                "second_text": _format_cell_text(second_texts),
                "splits_text": True,
            }
        )
    cell["subcells"] = subcells
    cell["subcell_boundaries"] = [_subcell_boundary_from_subcell(subcell) for subcell in subcells]


def _order_split_text_groups(
    first_texts: list[dict[str, Any]],
    second_texts: list[dict[str, Any]],
    boundary: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first_key = _text_group_visual_key(first_texts)
    second_key = _text_group_visual_key(second_texts)
    if boundary["type"] == "horrizontal_subcell":
        return (first_texts, second_texts) if first_key[1] <= second_key[1] else (second_texts, first_texts)
    return (first_texts, second_texts) if first_key[0] <= second_key[0] else (second_texts, first_texts)


def _text_group_visual_key(texts: list[dict[str, Any]]) -> tuple[float, float]:
    bboxes = [_text_bbox(text) for text in texts]
    center_x = sum((bbox[0] + bbox[2]) / 2.0 for bbox in bboxes) / len(bboxes)
    center_y = sum((bbox[1] + bbox[3]) / 2.0 for bbox in bboxes) / len(bboxes)
    return (center_x, center_y)


def _subcell_boundary_from_subcell(subcell: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": subcell["type"],
        "label": subcell["label"],
        "points": subcell["points"],
        "bbox": subcell["bbox"],
        "vector_index": subcell["vector_index"],
        "length": subcell["length"],
    }


def _text_identity(text: dict[str, Any]) -> tuple[Any, ...]:
    if "index" in text:
        return ("index", text["index"])
    return ("text", text.get("text", ""), *_text_bbox(text))


def _split_texts_by_boundary(
    texts: list[dict[str, Any]],
    boundary: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first = []
    second = []
    for text in texts:
        center = _bbox_center(_text_bbox(text))
        side = _boundary_side(center, boundary)
        if side <= 0:
            first.append(text)
        else:
            second.append(text)
    first.sort(key=lambda item: (_text_bbox(item)[1], _text_bbox(item)[0]))
    second.sort(key=lambda item: (_text_bbox(item)[1], _text_bbox(item)[0]))
    return first, second


def _boundary_side(point: tuple[float, float], boundary: dict[str, Any]) -> float:
    start, end = boundary["points"]
    kind = boundary["type"]
    if kind == "vertical_subcell":
        x_line = (start[0] + end[0]) / 2.0
        return point[0] - x_line
    if kind == "horrizontal_subcell":
        y_line = (start[1] + end[1]) / 2.0
        return point[1] - y_line
    return ((end[0] - start[0]) * (point[1] - start[1])) - ((end[1] - start[1]) * (point[0] - start[0]))


def _bbox_center(bbox: BBox) -> Point:
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _group_text_lines(texts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for text in sorted(texts, key=lambda item: (_text_bbox(item)[1], _text_bbox(item)[0])):
        bbox = _text_bbox(text)
        for line in lines:
            if _vertical_overlap(bbox, _line_bbox(line)) > 0.0:
                line.append(text)
                break
        else:
            lines.append([text])
    return sorted(lines, key=lambda line: _line_bbox(line)[1])


def _line_bbox(line: list[dict[str, Any]]) -> BBox:
    bboxes = [_text_bbox(text) for text in line]
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def _vertical_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(min(left[3], right[3]) - max(left[1], right[1]), 0.0)


def _covered_slots(cells: list[dict[str, Any]]) -> set[tuple[int, int]]:
    covered = set()
    for cell in cells:
        for row in range(cell["row"], cell["row"] + cell["rowspan"]):
            for col in range(cell["col"], cell["col"] + cell["colspan"]):
                covered.add((row, col))
    return covered


def _unique_edges(values: list[float]) -> list[float]:
    return sorted({float(value) for value in values})


def _edge_intervals(edges: list[float]) -> list[dict[str, float]]:
    return [
        {"index": index, "start": edges[index], "end": edges[index + 1]}
        for index in range(len(edges) - 1)
    ]


def _bbox_overlap_area(
    left: BBox,
    right: BBox,
) -> float:
    left_x0, left_y0, left_x1, left_y1 = left
    right_x0, right_y0, right_x1, right_y1 = right
    width = min(left_x1, right_x1) - max(left_x0, right_x0)
    height = min(left_y1, right_y1) - max(left_y0, right_y0)
    return max(width, 0.0) * max(height, 0.0)


def _bbox_area(bbox: BBox) -> float:
    x0, y0, x1, y1 = bbox
    return max(x1 - x0, 0.0) * max(y1 - y0, 0.0)


def _table_detection_vectors(vectors: VectorBase) -> VectorBase:
    """Keep only vectors that should contribute to table-cell faces.

    The table reconstruction itself trusts VectorBoxDetector's closed faces.
    This filter only removes obvious drawing/logo noise before detection.
    """
    quad_edges = {
        _segment_key(left, right)
        for vector in vectors
        if vector.type == "quad" and len(vector.points) >= 4
        for left, right in zip(vector.points[:4], [*vector.points[1:4], vector.points[0]])
    }
    curve_path_keys = {
        path_key
        for vector in vectors
        if vector.type == "curve"
        for path_key in [_path_key(vector)]
        if path_key is not None
    }

    kept_vectors = []
    kept_indices = []
    for index, vector in enumerate(vectors):
        if vector.type in {"quad", "curve"}:
            continue
        path_key = _path_key(vector)
        if path_key is not None and path_key in curve_path_keys:
            continue
        if vector.type == "line" and len(vector.points) >= 2:
            if _line_orientation(vector) is None:
                continue
            if _segment_key(vector.points[0], vector.points[1]) in quad_edges:
                continue
        kept_vectors.append(vector)
        kept_indices.append(vectors.source_index(index))

    return VectorBase(kept_vectors, source_indices=kept_indices)


def _path_key(vector: Any) -> tuple[Any, Any, Any] | None:
    key = (
        vector.path_meta_value("path_index"),
        vector.path_meta_value("seqno"),
        vector.path_meta_value("path_type"),
    )
    if all(value is None for value in key):
        return None
    return key


def _segment_key(left: Any, right: Any) -> tuple[tuple[float, float], tuple[float, float]]:
    """Normalize a line segment so opposite drawing directions match."""
    first = (float(left[0]), float(left[1]))
    second = (float(right[0]), float(right[1]))
    return tuple(sorted((first, second)))  # type: ignore[return-value]


def _line_orientation(vector: Any) -> str | None:
    left, right = vector.points[:2]
    if float(left[0]) == float(right[0]):
        return "v"
    if float(left[1]) == float(right[1]):
        return "h"
    return None


def _print_empty_cell_vectors(result: dict[str, Any], limit: int | None = None) -> None:
    """Print source vectors for empty cells, smallest cells first."""
    empty_cells = [
        (index, cell)
        for index, cell in enumerate(result["cells"])
        if not cell.get("text", "").strip()
    ]
    empty_cells.sort(key=lambda item: (_bbox_area(_cell_bbox(item[1])), _cell_bbox(item[1])[1], _cell_bbox(item[1])[0]))
    if limit is not None:
        empty_cells = empty_cells[:limit]

    print("\n=== EMPTY CELL SOURCE VECTORS ===")
    if not empty_cells:
        print("No empty cells.")
        return

    for index, cell in empty_cells:
        bbox = _cell_bbox(cell)
        source = cell.get("source", {})
        vectors = source.get("vectors", [])
        vector_indices = source.get("vector_indices", [])
        print(
            f"Cell {index}: row={cell.get('row')} col={cell.get('col')} "
            f"rowspan={cell.get('rowspan')} colspan={cell.get('colspan')} "
            f"bbox={bbox} area={_bbox_area(bbox):.3f}"
        )
        print(f"  vector_indices={vector_indices}")
        for vector in vectors:
            print(
                f"  vector {vector.get('index')}: type={vector.get('type')} "
                f"bbox={vector.get('bbox')} points={vector.get('points')}"
            )


def _print_subcell_debug(result: dict[str, Any], limit: int | None = None) -> None:
    """Print cells with multiple text spans and any kept subcell boundaries."""
    cells = [
        (index, cell)
        for index, cell in enumerate(result["cells"])
        if len(cell.get("texts", [])) >= 2 or cell.get("subcells")
    ]
    cells.sort(key=lambda item: (_cell_bbox(item[1])[1], _cell_bbox(item[1])[0]))
    if limit is not None:
        cells = cells[:limit]

    print("\n=== SUBCELL DEBUG ===")
    if not cells:
        print("No cells with multiple text spans or kept subcells.")
        return

    for index, cell in cells:
        bbox = _cell_bbox(cell)
        print(
            f"Cell {index}: row={cell.get('row')} col={cell.get('col')} "
            f"rowspan={cell.get('rowspan')} colspan={cell.get('colspan')} bbox={bbox}"
        )
        print(f"  text={cell.get('text', '')!r}")
        print(f"  text_count={len(cell.get('texts', []))}")
        for text in cell.get("texts", []):
            print(f"    text_span={text.get('text', '')!r} bbox={_text_bbox(text)}")

        subcells = cell.get("subcells", [])
        print(f"  kept_subcell_count={len(subcells)}")
        for subcell in subcells:
            print(
                f"    {subcell.get('label')} vector={subcell.get('vector_index')} "
                f"points={subcell.get('points')} first={subcell.get('first_text')!r} "
                f"second={subcell.get('second_text')!r}"
            )


def _cell_bbox(cell: dict[str, Any]) -> BBox:
    return coerce_bbox(cell["bbox"])


def _text_bbox(text: dict[str, Any]) -> BBox:
    return coerce_bbox(text["location"])


def _empty_table() -> dict[str, Any]:
    return {"rows": [], "columns": [], "cells": []}


def _bbox_to_dict(bbox: BBox) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    return {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}


def _bbox_intersects(left: Any, right: Any) -> bool:
    left_x0, left_y0, left_x1, left_y1 = coerce_bbox(left)
    right_x0, right_y0, right_x1, right_y1 = coerce_bbox(right)
    return (
        min(left_x1, right_x1) >= max(left_x0, right_x0)
        and min(left_y1, right_y1) >= max(left_y0, right_y0)
    )


def _points_bbox(points: list[Any]) -> BBox:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_overlap_length(left_start: float, left_end: float, right_start: float, right_end: float) -> float:
    return max(min(left_end, right_end) - max(left_start, right_start), 0.0)


def _is_horizontal_bbox(bbox: Any) -> bool:
    x0, y0, x1, y1 = coerce_bbox(bbox)
    return x1 > x0 and y0 == y1


def _is_vertical_bbox(bbox: Any) -> bool:
    x0, y0, x1, y1 = coerce_bbox(bbox)
    return y1 > y0 and x0 == x1


def _snap_bbox_to_nearest_vector_edges(pages: PdfPageManager, bbox: Any) -> dict[str, float]:
    page = pages.require_page()
    page_bbox = pages.page_bbox
    if page_bbox is None:
        raise RuntimeError("page bbox is not initialized")

    x0, y0, x1, y1 = coerce_bbox(bbox)
    px0, py0, px1, py1 = coerce_bbox(page_bbox)
    snapped = [x0, y0, x1, y1]

    nearest_top: tuple[float, float] | None = None
    nearest_bottom: tuple[float, float] | None = None
    nearest_left: tuple[float, float] | None = None
    nearest_right: tuple[float, float] | None = None

    for path_index, path in enumerate(page.get_drawings(extended=False)):
        path_bbox = path.get("rect")
        if path_bbox is None:
            path_may_affect_x = path_may_affect_y = True
        else:
            path_x0, path_y0, path_x1, path_y1 = coerce_bbox(path_bbox)
            path_may_affect_y = _bbox_overlap_length(path_x0, path_x1, x0, x1) > 0.0
            path_may_affect_x = _bbox_overlap_length(path_y0, path_y1, y0, y1) > 0.0
            if not path_may_affect_x and not path_may_affect_y:
                continue

        path_meta = {
            "path_index": path_index,
            "seqno": path.get("seqno"),
            "path_type": path.get("type"),
            "stroke_width": path.get("width"),
            "color": path.get("color"),
            "fill": path.get("fill"),
            "dashes": path.get("dashes"),
        }
        for item_index, item in enumerate(path.get("items") or []):
            shape = pages._shape_from_item(item, path_meta, item_index)
            if shape is None:
                continue

            vector_bbox = _points_bbox(shape["points"])
            vx0, vy0, vx1, vy1 = vector_bbox
            if path_may_affect_y and _is_horizontal_bbox(vector_bbox):
                if _bbox_overlap_length(vx0, vx1, x0, x1) > 0.0:
                    if vy1 <= y0 and (nearest_top is None or vy1 > nearest_top[0]):
                        nearest_top = (vy1, vy0)
                    if vy0 >= y1 and (nearest_bottom is None or vy0 < nearest_bottom[0]):
                        nearest_bottom = (vy0, vy1)
            if path_may_affect_x and _is_vertical_bbox(vector_bbox):
                if _bbox_overlap_length(vy0, vy1, y0, y1) > 0.0:
                    if vx1 <= x0 and (nearest_left is None or vx1 > nearest_left[0]):
                        nearest_left = (vx1, vx0)
                    if vx0 >= x1 and (nearest_right is None or vx0 < nearest_right[0]):
                        nearest_right = (vx0, vx1)

    if nearest_left is not None:
        snapped[0] = max(nearest_left[1], px0)
    if nearest_top is not None:
        snapped[1] = max(nearest_top[1], py0)
    if nearest_right is not None:
        snapped[2] = min(nearest_right[1], px1)
    if nearest_bottom is not None:
        snapped[3] = min(nearest_bottom[1], py1)

    return _bbox_to_dict(tuple(snapped))


def _scale_bbox(
    reference_bbox: Any,
    reference_page_bbox: Any,
    target_page_bbox: Any,
) -> dict[str, float]:
    ref_x0, ref_y0, ref_x1, ref_y1 = coerce_bbox(reference_page_bbox)
    target_x0, target_y0, target_x1, target_y1 = coerce_bbox(target_page_bbox)
    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = coerce_bbox(reference_bbox)

    ref_width = ref_x1 - ref_x0
    ref_height = ref_y1 - ref_y0
    target_width = target_x1 - target_x0
    target_height = target_y1 - target_y0
    if ref_width <= 0.0 or ref_height <= 0.0:
        raise ValueError(f"Invalid reference page bbox: {reference_page_bbox}")

    if ref_width == target_width and ref_height == target_height:
        return _bbox_to_dict((bbox_x0, bbox_y0, bbox_x1, bbox_y1))

    x_scale = target_width / ref_width
    y_scale = target_height / ref_height
    return _bbox_to_dict(
        (
            target_x0 + ((bbox_x0 - ref_x0) * x_scale),
            target_y0 + ((bbox_y0 - ref_y0) * y_scale),
            target_x0 + ((bbox_x1 - ref_x0) * x_scale),
            target_y0 + ((bbox_y1 - ref_y0) * y_scale),
        )
    )


def _detect_reference_info_bbox(
    pages: PdfPageManager,
    split_detector: SplitPageDetector,
    page_number: int,
) -> tuple[dict[str, float] | None, dict[str, float]]:
    page_data = pages.goto(page_number)
    split_result = split_detector.detect(page_data)
    info_bbox = split_result["info_bbox"]
    if info_bbox is None:
        print(
            f"No information region found on reference page {page_number}; "
            "skipping pages with this layout.",
            file=sys.stderr,
        )
    return info_bbox, split_result["page_bbox"]


def _page_bbox_for_page(pages: PdfPageManager, page_number: int) -> BBox:
    page = pages.doc[page_number - 1]
    return (0.0, 0.0, float(page.rect.width), float(page.rect.height))


def _page_size_key(page_bbox: Any) -> tuple[float, float]:
    x0, y0, x1, y1 = coerce_bbox(page_bbox)
    return (round(x1 - x0, 3), round(y1 - y0, 3))


def _cached_layout_info_bbox(
    pages: PdfPageManager,
    split_detector: SplitPageDetector,
    page_number: int,
    layout_cache: dict[tuple[float, float], dict[str, Any]],
) -> dict[str, Any]:
    page_bbox = _page_bbox_for_page(pages, page_number)
    layout_key = _page_size_key(page_bbox)
    if layout_key not in layout_cache:
        info_bbox, reference_page_bbox = _detect_reference_info_bbox(
            pages,
            split_detector,
            page_number,
        )
        layout_cache[layout_key] = {
            "layout_key": layout_key,
            "reference_page": page_number,
            "reference_info_bbox": info_bbox,
            "reference_page_bbox": reference_page_bbox,
            "skip": info_bbox is None,
        }
    return layout_cache[layout_key]


def _goto_page_header(pages: PdfPageManager, page_number: int) -> None:
    if page_number < 1 or page_number > pages.page_count:
        raise ValueError(f"page_number out of range: {page_number}")

    pages.page_number = int(page_number)
    pages.page = pages.doc[page_number - 1]
    pages.page_height_pt = float(pages.page.rect.height)
    pages.page_bbox = (0.0, 0.0, float(pages.page.rect.width), float(pages.page.rect.height))


def _extract_region_vectors(pages: PdfPageManager, region_bbox: Any) -> list[dict[str, Any]]:
    page = pages.require_page()
    vectors: list[dict[str, Any]] = []

    for path_index, path in enumerate(page.get_drawings(extended=False)):
        path_bbox = path.get("rect")
        if path_bbox is not None and not _bbox_intersects(path_bbox, region_bbox):
            continue

        path_meta = {
            "path_index": path_index,
            "seqno": path.get("seqno"),
            "path_type": path.get("type"),
            "stroke_width": path.get("width"),
            "color": path.get("color"),
            "fill": path.get("fill"),
            "dashes": path.get("dashes"),
        }
        for item_index, item in enumerate(path.get("items") or []):
            shape = pages._shape_from_item(item, path_meta, item_index)
            if shape is None:
                continue
            if _bbox_intersects(_points_bbox(shape["points"]), region_bbox):
                vectors.append(shape)

    return vectors


def _extract_region_texts(pages: PdfPageManager, region_bbox: Any) -> list[dict[str, Any]]:
    fitz = _fitz_module()
    page = pages.require_page()
    clip = _bbox_to_rect(region_bbox)
    try:
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE, clip=clip)
    except TypeError:
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    texts: list[dict[str, Any]] = []
    for block in text_dict.get("blocks") or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                content = str(span.get("text") or "").strip()
                bbox_raw = span.get("bbox")
                if not content or not bbox_raw or len(bbox_raw) < 4:
                    continue
                bbox_value = coerce_bbox(bbox_raw[:4])
                if not _bbox_intersects(bbox_value, region_bbox):
                    continue
                texts.append(
                    {
                        "index": len(texts),
                        "text": content,
                        "bbox": _bbox_to_dict(bbox_value),
                        "font": span.get("font"),
                        "font_size": float(span.get("size") or 0.0),
                    }
                )
    return texts


def _region_page_data(pages: PdfPageManager, page_number: int, region_bbox: Any) -> dict[str, Any]:
    if pages.page_number != page_number:
        _goto_page_header(pages, page_number)
    vectors = _extract_region_vectors(pages, region_bbox)
    texts = _extract_region_texts(pages, region_bbox)
    pages.page_vector = vectors
    pages.page_texts = texts
    pages.page_data = pages.build_page_data(vectors, texts)
    return pages.page_data


def _fitz_module() -> Any:
    import fitz

    return fitz


def _bbox_to_rect(bbox: Any) -> Any:
    fitz = _fitz_module()
    x0, y0, x1, y1 = coerce_bbox(bbox)
    return fitz.Rect(x0, y0, x1, y1)


def _manual_output_root(output_dir: str | Path | None, pdf_path: str | Path) -> Path:
    root = Path(output_dir) if output_dir else pdf_path.parent
    return root / pdf_path.stem


def _output_pdf_path(output_pdf: str | None, output_dir: str | Path, pdf_path: Path | None = None,) -> Path:
    if output_pdf:
        return Path(output_pdf)
    return Path(output_dir) / f"{pdf_path.stem}_title_block_info_bbox.pdf"


def _save_info_bbox_pdf(
    source_doc: Any,
    page_results: list[dict[str, Any]],
    output_pdf: Path,
) -> None:
    fitz = _fitz_module()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    out_doc = fitz.open()
    try:
        for page_result in page_results:
            if page_result.get("skipped"):
                continue
            page_number = int(page_result["page"])
            source_page = source_doc[page_number - 1]
            clip = _bbox_to_rect(page_result.get("extraction_bbox") or page_result["info_bbox"])
            clip &= source_page.rect
            if clip.is_empty:
                raise ValueError(
                    f"Empty info_bbox clip for page {page_number}: {page_result['info_bbox']}"
                )

            out_page = out_doc.new_page(width=clip.width, height=clip.height)
            out_page.show_pdf_page(out_page.rect, source_doc, page_number - 1, clip=clip)
        if out_doc.page_count:
            out_doc.save(output_pdf)
        else:
            print("No info_bbox pages to save.", file=sys.stderr)
    finally:
        out_doc.close()


def _json_output_root(json_output_dir: str | None, manual_output_root: Path) -> Path:
    if json_output_dir:
        return Path(json_output_dir)
    return manual_output_root / "json"


def _save_page_result_json(page_result: dict[str, Any], output_root: Path) -> Path:
    folder = output_root / ("skipped" if page_result.get("skipped") else "tables")
    folder.mkdir(parents=True, exist_ok=True)
    output_path = folder / f"page_{int(page_result['page']):4d}.json"
    payload = dict(page_result)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not page_result.get("skipped"):
        html_path = folder / f"page_{int(page_result['page']):4d}.html"
        html_path.write_text(payload["html"], encoding="utf-8")
    return output_path


def _save_page_results_json(page_results: list[dict[str, Any]], output_root: Path) -> None:
    for page_result in page_results:
        _save_page_result_json(page_result, output_root)


def _print_progress(current: int, total: int, page_number: int | None = None) -> None:
    bar_width = 30
    ratio = current / total if total else 1.0
    filled = int(round(bar_width * ratio))
    bar = "#" * filled + "-" * (bar_width - filled)
    percent = ratio * 100.0
    page_text = f" page {page_number}" if page_number is not None else ""
    end = "\n" if current >= total else "\r"
    print(
        f"Processing{page_text}: [{bar}] {current}/{total} ({percent:5.1f}%)",
        end=end,
        file=sys.stderr,
        flush=True,
    )


def _page_numbers(args: argparse.Namespace, page_count: int) -> list[int]:
    if args.page is not None:
        if args.page < 1 or args.page > page_count:
            raise ValueError(f"--page must be between 1 and {page_count}: {args.page}")
        return [args.page]
    return list(range(1, page_count + 1))


def _process_page(
    pages: PdfPageManager,
    split_detector: SplitPageDetector,
    page_number: int,
    layout_cache: dict[tuple[float, float], dict[str, Any]],
    *,
    adaptive_info_bbox: bool,
    render_debug: bool,
) -> dict[str, Any]:
    page_bbox = _page_bbox_for_page(pages, page_number)
    layout_info = _cached_layout_info_bbox(pages, split_detector, page_number, layout_cache)
    if layout_info.get("skip"):
        return {
            "page": page_number,
            "layout_key": layout_info["layout_key"],
            "layout_reference_page": layout_info["reference_page"],
            "info_bbox": None,
            "extraction_bbox": None,
            "html": "",
            "result": _empty_table(),
            "skipped": True,
            "skip_reason": "No information region found for this page layout.",
        }

    info_bbox = _scale_bbox(
        layout_info["reference_info_bbox"],
        layout_info["reference_page_bbox"],
        page_bbox,
    )
    _goto_page_header(pages, page_number)
    extraction_bbox = (
        _snap_bbox_to_nearest_vector_edges(pages, info_bbox)
        if adaptive_info_bbox
        else info_bbox
    )
    page_data = _region_page_data(pages, page_number, extraction_bbox)
    content = split_detector.region_content(page_data, extraction_bbox, exclude_frame_vectors=False)
    if render_debug:
        render_vector_text_png(
            content["vectors"],
            content["text"],
            "storage/output/images",
            f"vector_text_page_{page_number}.png",
        )
    extracted = extract_table(content)
    result = reconstruct_table(extracted["raw_cells"], extracted["texts"], extracted["vectors"])
    if not result["cells"]:
        return {
            "page": page_number,
            "layout_key": layout_info["layout_key"],
            "layout_reference_page": layout_info["reference_page"],
            "info_bbox": info_bbox,
            "extraction_bbox": extraction_bbox,
            "html": "",
            "result": result,
            "skipped": True,
            "skip_reason": "No table cells detected in the information region.",
        }

    return {
        "page": page_number,
        "layout_key": layout_info["layout_key"],
        "layout_reference_page": layout_info["reference_page"],
        "info_bbox": info_bbox,
        "extraction_bbox": extraction_bbox,
        "html": result["html"],
        "result": result,
        "skipped": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract title-block tables using one detected reference info bbox.")
    parser.add_argument(
        "--pdf-file-path",
        dest="pdf_file_path",
        default="./storage/data/eplan_pdf/000_1.pdf",
        help="Input PDF path.",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=None,
        help="Optional 1-based page number to inspect. If omitted, all pages are processed.",
    )
    parser.add_argument(
        "--reference-page",
        type=int,
        default=None,
        help=(
            "Optional 1-based page used to seed the bbox cache for that page size. "
            "Other page sizes are detected on first use."
        ),
    )
    parser.add_argument(
        "--print-page",
        type=int,
        default=435,
        help="Only print HTML/debug output for this 1-based page after processing all pages.",
    )
    parser.add_argument(
        "--render-debug",
        action="store_true",
        help="Render per-page vector/text debug PNGs into storage/output/images.",
    )
    parser.add_argument(
        "--no-adaptive-info-bbox",
        action="store_true",
        help="Use the reused info_bbox exactly instead of snapping it to nearest vector edges.",
    )
    parser.add_argument(
        "--save-info-pdf",
        action="store_true",
        default=True,
        help="Save all reused info_bbox crops into one PDF for visual inspection.",
    )
    parser.add_argument(
        "--output-dir",
        default="storage/output/images",
        help="Directory for the default info_bbox PDF when --output-pdf is omitted.",
    )
    parser.add_argument(
        "--output-pdf",
        default=None,
        help="Output PDF path for info_bbox crops. Also enables saving the PDF.",
    )
    parser.add_argument(
        "--json-output-dir",
        default=None,
        help="Optional override directory for per-page JSON results. Defaults to <output-dir>/<pdf-stem>/json.",
    )
    parser.add_argument(
        "--debug-empty-cells",
        action="store_true",
        help="Print source vectors for empty cells, ordered from smallest to largest.",
    )
    parser.add_argument(
        "--debug-empty-cell-limit",
        type=int,
        default=None,
        help="Maximum number of empty cells to print.",
    )
    parser.add_argument(
        "--debug-subcells",
        action="store_true",
        help="Print cells with multiple text spans and kept subcell boundaries.",
    )
    parser.add_argument(
        "--debug-subcell-limit",
        type=int,
        default=None,
        help="Maximum number of subcell debug cells to print.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pdf_path = resolve_repo_relative(args.pdf_file_path)
    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")

    with PdfPageManager(pdf_path) as pages:
        split_detector = SplitPageDetector()
        page_numbers = _page_numbers(args, pages.page_count)
        layout_cache: dict[tuple[float, float], dict[str, Any]] = {}
        if args.reference_page is not None:
            if args.reference_page < 1 or args.reference_page > pages.page_count:
                raise ValueError(
                    f"--reference-page must be between 1 and {pages.page_count}: {args.reference_page}"
                )
            _cached_layout_info_bbox(
                pages,
                split_detector,
                args.reference_page,
                layout_cache,
            )

        page_results = []
        _print_progress(0, len(page_numbers))
        for index, page_number in enumerate(page_numbers, start=1):
            page_result = _process_page(
                pages,
                split_detector,
                page_number,
                layout_cache,
                adaptive_info_bbox=not args.no_adaptive_info_bbox,
                render_debug=args.render_debug,
            )
            page_results.append(page_result)
            _print_progress(index, len(page_numbers), page_number)

        manual_output_root = _manual_output_root(args.output_dir, pdf_path)
        output_pdf = None
        if args.save_info_pdf or args.output_pdf:
            output_pdf = Path(args.output_pdf) if args.output_pdf else manual_output_root / "title_block_info_bbox.pdf"
            _save_info_bbox_pdf(pages.doc, page_results, output_pdf)

    json_output_root = _json_output_root(args.json_output_dir, manual_output_root)
    _save_page_results_json(page_results, json_output_root)

    print("=== LAYOUT INFO BBOX CACHE ===")
    for layout_info in sorted(layout_cache.values(), key=lambda item: item["layout_key"]):
        status = "skipped" if layout_info.get("skip") else "detected"
        print(
            f"size={layout_info['layout_key']} "
            f"reference_page={layout_info['reference_page']} "
            f"status={status} "
            f"info_bbox={layout_info['reference_info_bbox']}"
        )
    if output_pdf is not None:
        print(f"=== INFO BBOX PDF ===\n{output_pdf}")
    print(f"=== MANUAL OUTPUT DIR ===\n{manual_output_root}")
    print(f"=== PAGE JSON DIR ===\n{json_output_root}")
    for page_result in page_results:
        if args.print_page is not None and page_result["page"] != args.print_page:
            continue

        if page_result.get("skipped"):
            print(
                f"\n=== SKIPPED PAGE {page_result['page']} "
                f"(layout_ref={page_result['layout_reference_page']}) ==="
            )
            print(page_result["skip_reason"])
            continue

        result = page_result["result"]
        print(
            f"\n=== HTML TABLE PAGE {page_result['page']} "
            f"(layout_ref={page_result['layout_reference_page']}) ==="
        )
        print(result["html"])
        if args.debug_empty_cells:
            _print_empty_cell_vectors(result, args.debug_empty_cell_limit)
        if args.debug_subcells:
            _print_subcell_debug(result, args.debug_subcell_limit)


if __name__ == "__main__":
    main()
