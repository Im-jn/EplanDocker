from __future__ import annotations

import json
import sys
from base64 import b64encode
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from shapely.geometry.base import BaseGeometry

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.processors.diagram_extractor import (
    assign_remaining_vectors_to_elements,
    extract_diagram,
    merge_diagram_results,
)
from pdf_parser.processors.hyperlink_extractor import (
    attach_hyperlinks,
    extract_hyperlinks,
    split_transfers,
)
from pdf_parser.processors.diagram_serializer import serialize_diagram
from pdf_parser.processors.relation_extractor import (
    merge_adjacent_box_elements,
    organize_relation,
)
from pdf_parser.processors.relation_composer import handle_relations
from pdf_parser.processors.symbol_extractor import extract_symbols, merge_symbol_results
from pdf_parser.processors.table_extractor import extract_table, reconstruct_table
from pdf_parser.llm_judger import LLMConfig, PersistentDiagramClassifier
from pdf_parser.pages_manager import PathBase, PdfPageManager, TextBase, VectorBase
from pdf_parser.pages_manager.split_page import SplitPageDetector
from pdf_parser.tools.text_matcher import TextMatcher
from pdf_parser.tools.endpoints_tools import EndpointTools
from pdf_parser.tools.vector_box import VectorBoxDetector
from pdf_parser.tools.vector_entity import VectorDisjointSet
from pdf_parser.tools.vector_matcher import VectorMatcher
from pdf_parser.tools.vector_pin import VectorPinDetector
from pdf_parser.tools.vector_visualize import render_vector_text_png
from pdf_parser.utils import bbox_from_shapes, bbox_to_dict, resolve_repo_relative


PAGE_CACHE_LIMIT = PARSER_CONFIG.api_cache.page_limit
ENTITY_CACHE_LIMIT = PARSER_CONFIG.api_cache.entity_limit
RESULT_CACHE_LIMIT = PARSER_CONFIG.api_cache.result_limit
SYMBOL_CACHE_LIMIT = PARSER_CONFIG.api_cache.symbol_limit
# When two symbol matches overlap by more than this fraction of the smaller box,
# they are treated as the same physical mark and only the richer symbol is kept.
SYMBOL_MATCH_OVERLAP_RATIO = PARSER_CONFIG.vector_matcher.symbol_overlap_ratio
DEFAULT_LLM_CONFIG = LLMConfig.from_env()
PERSISTENCE_VERSION = 14
PERSISTENCE_ROOT = resolve_repo_relative("./storage/cache/frontend")
PARSING_RESULT_ROOT = resolve_repo_relative("./storage/output/pdf_parsing_result")

_page_cache: OrderedDict[tuple[str, int, int, int], dict[str, Any]] = OrderedDict()
_entity_base_cache: OrderedDict[tuple[tuple[str, int, int, int], tuple[int, ...]], VectorBase] = OrderedDict()
_result_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_symbol_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_document_result_cache: OrderedDict[tuple[str, int, int], dict[str, Any]] = OrderedDict()


def _lru_get(cache: OrderedDict[Any, Any], key: Any) -> Any | None:
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


def _lru_put(cache: OrderedDict[Any, Any], key: Any, value: Any, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _page_cache_key(pdf_path: Path, page: int) -> tuple[str, int, int, int]:
    stat = pdf_path.stat()
    return (str(pdf_path), int(stat.st_mtime_ns), int(stat.st_size), page)


def _document_extract_info(
    pdf_path: Path,
    page: int,
    *,
    result_root: Path = PARSING_RESULT_ROOT,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one diagram page from ``<PDF stem>.json`` when available."""
    document_result, result_filename = _load_document_result(
        pdf_path,
        result_root=result_root,
    )
    if document_result is None:
        return None, result_filename

    pages = document_result.get("pages", {})
    page_result = pages.get(str(page)) if isinstance(pages, dict) else None
    if not isinstance(page_result, dict):
        return None, result_filename
    diagram = page_result.get("diagram")
    required_diagram_fields = (
        "elements",
        "components",
        "endpoints",
        "wires",
        "nets",
        "groups",
        "relations",
    )
    if not isinstance(diagram, dict) or not all(
        isinstance(diagram.get(field), list) for field in required_diagram_fields
    ):
        return None, result_filename
    return {
        "category": "extract_info",
        "page_number": page,
        **page_result,
    }, result_filename


def _load_document_result(
    pdf_path: Path,
    *,
    result_root: Path = PARSING_RESULT_ROOT,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load a document result from the API layout or the legacy flat layout."""
    candidates = (
        result_root / pdf_path.parent.name / "result.json",
        result_root / f"{pdf_path.stem}.json",
    )
    result_path: Path | None = None
    stat = None
    for candidate in candidates:
        try:
            stat = candidate.stat()
            result_path = candidate
            break
        except OSError:
            continue
    if result_path is None or stat is None:
        return None, None

    cache_key = (str(result_path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    document_result = _lru_get(_document_result_cache, cache_key)
    if document_result is None:
        try:
            with result_path.open("r", encoding="utf-8") as result_file:
                loaded = json.load(result_file)
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(loaded, dict):
            return None, None
        document_result = loaded
        _lru_put(_document_result_cache, cache_key, document_result, 1)
    return document_result, result_path.name


def _document_symbol_result(
    pdf_path: Path,
    *,
    result_root: Path = PARSING_RESULT_ROOT,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project the parser's symbol_overview into the reader's read-only format."""
    document_result, result_filename = _load_document_result(
        pdf_path,
        result_root=result_root,
    )
    if document_result is None:
        return None, result_filename
    overview = document_result.get("symbol_overview")
    if not isinstance(overview, dict):
        return None, result_filename

    raw_symbols = overview.get("symbols", [])
    raw_records = overview.get("records", [])
    if not isinstance(raw_symbols, list) or not isinstance(raw_records, list):
        return None, result_filename

    symbols: list[dict[str, Any]] = []
    for group in raw_symbols:
        vectors = group if isinstance(group, list) else []
        valid_vectors: list[tuple[dict[str, Any], list[list[float]]]] = []
        all_points: list[list[float]] = []
        for vector in vectors:
            if not isinstance(vector, dict):
                continue
            points = [
                [float(point[0]), float(point[1])]
                for point in vector.get("points", [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if not points:
                continue
            valid_vectors.append((vector, points))
            all_points.extend(points)
        x0 = min((point[0] for point in all_points), default=0.0)
        y0 = min((point[1] for point in all_points), default=0.0)
        x1 = max((point[0] for point in all_points), default=x0)
        y1 = max((point[1] for point in all_points), default=y0)
        shapes = []
        for vector, points in valid_vectors:
            path_meta = vector.get("path_meta", {})
            dashes = path_meta.get("dashes") if isinstance(path_meta, dict) else None
            shapes.append({
                "type": vector.get("type"),
                "points": [[px - x0, py - y0] for px, py in points],
                "dashed": bool(dashes and str(dashes).strip() not in {"[] 0", "[]"}),
            })
        symbols.append({
            "shapes": shapes,
            "width": float(x1 - x0),
            "height": float(y1 - y0),
        })

    records = []
    for record in raw_records:
        if not isinstance(record, dict):
            continue
        description = record.get("descriptions", "")
        if isinstance(description, list):
            description = "; ".join(str(value) for value in description)
        records.append({
            "symbol": int(record.get("symbol", 0)),
            "name": str(record.get("type") or "(unnamed)"),
            "description": str(description or ""),
        })

    pages = document_result.get("pages", {})
    symbol_pages = sorted(
        int(page_number)
        for page_number, page_result in pages.items()
        if isinstance(page_result, dict)
        and page_result.get("page_type") == "symbol_overview"
        and str(page_number).isdigit()
    ) if isinstance(pages, dict) else []
    return {
        "category": "symbols",
        "symbols": symbols,
        "records": records,
        "pages": symbol_pages,
    }, result_filename


def _document_trace_index(
    pdf_path: Path,
    *,
    result_root: Path = PARSING_RESULT_ROOT,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a compact document graph for endpoint/transfer tracing."""
    document_result, result_filename = _load_document_result(
        pdf_path,
        result_root=result_root,
    )
    if document_result is None:
        return None, result_filename

    pages = document_result.get("pages", {})
    if not isinstance(pages, dict):
        return None, result_filename

    page_indexes: list[dict[str, Any]] = []
    transfers: list[dict[str, Any]] = []
    entity_fields = ("id", "type", "page", "bbox", "title", "descriptions")
    transfer_fields = (
        "source_page",
        "source_component",
        "target_page",
        "target_component",
    )

    for page_key, page_result in pages.items():
        if not isinstance(page_result, dict):
            continue
        diagram = page_result.get("diagram")
        if not isinstance(diagram, dict):
            continue
        try:
            page_number = int(page_key)
        except (TypeError, ValueError):
            continue

        def compact_entities(field: str) -> list[dict[str, Any]]:
            entities = diagram.get(field, [])
            if not isinstance(entities, list):
                return []
            return [
                {key: entity.get(key) for key in entity_fields if key in entity}
                for entity in entities
                if isinstance(entity, dict)
            ]

        relations = diagram.get("relations", [])
        page_indexes.append({
            "page_number": page_number,
            "components": compact_entities("components"),
            "wires": compact_entities("wires"),
            "endpoints": compact_entities("endpoints"),
            "nets": compact_entities("nets"),
            "relations": [
                {
                    "type": relation.get("type"),
                    "source": relation.get("source"),
                    "target": relation.get("target"),
                }
                for relation in relations
                if isinstance(relation, dict)
                and (
                    relation.get("type") == "connection"
                    or (
                        relation.get("type") == "contains"
                        and str(relation.get("source", "")).startswith("net:")
                        and str(relation.get("target", "")).startswith("wire:")
                    )
                )
            ] if isinstance(relations, list) else [],
        })

        crosspage = page_result.get("crosspage_relations", {})
        page_transfers = crosspage.get("transfers", []) if isinstance(crosspage, dict) else []
        if isinstance(page_transfers, list):
            transfers.extend(
                {
                    key: transfer.get(key)
                    for key in transfer_fields
                    if key in transfer
                }
                for transfer in page_transfers
                if isinstance(transfer, dict)
            )

    page_indexes.sort(key=lambda item: item["page_number"])
    return {
        "category": "trace_index",
        "pages": page_indexes,
        "transfers": transfers,
    }, result_filename


def _cached_page_data(pdf_path: Path, page: int) -> tuple[tuple[str, int, int, int], dict[str, Any]]:
    key = _page_cache_key(pdf_path, page)
    cached = _lru_get(_page_cache, key)
    if cached is not None:
        return key, cached
    with PdfPageManager(pdf_path) as pages:
        page_data = pages.goto(page)
    _lru_put(_page_cache, key, page_data, PAGE_CACHE_LIMIT)
    return key, page_data


def _result_cache_key(
    page_key: tuple[str, int, int, int],
    payload: dict[str, Any],
    mode: str,
) -> tuple[Any, ...]:
    bbox = payload.get("bbox")
    bbox_key = tuple(sorted(bbox.items())) if isinstance(bbox, dict) else tuple(bbox or ())
    return (
        page_key,
        mode,
        tuple(sorted(int(index) for index in payload.get("entity_indices", []))),
        bbox_key,
        str(payload.get("coord_space", "pdf")),
        float(payload.get(
            "missing_vector_ratio",
            PARSER_CONFIG.vector_matcher.missing_vector_ratio,
        )),
        float(payload.get(
            "split_edge_tolerance_ratio",
            PARSER_CONFIG.split_page.page_frame_edge_tolerance_ratio,
        )),
        float(payload.get(
            "split_min_content_inset",
            PARSER_CONFIG.split_page.min_content_inset_pt,
        )),
    )


def _mupdf_bbox_to_pdf(bbox: dict[str, float] | tuple[float, float, float, float], page_height: float) -> dict[str, float]:
    if isinstance(bbox, dict):
        x0, y0, x1, y1 = float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])
    else:
        x0, y0, x1, y1 = bbox
    pdf_y0 = page_height - y1
    pdf_y1 = page_height - y0
    return {"x0": x0, "y0": pdf_y0, "x1": x1, "y1": pdf_y1, "width": x1 - x0, "height": pdf_y1 - pdf_y0}


def _pdf_bbox_to_mupdf(bbox: dict[str, Any], page_height: float) -> dict[str, float]:
    x0, y0, x1, y1 = float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])
    mupdf_y0 = page_height - y1
    mupdf_y1 = page_height - y0
    return {"x0": x0, "y0": mupdf_y0, "x1": x1, "y1": mupdf_y1, "width": x1 - x0, "height": mupdf_y1 - mupdf_y0}


def _split_detector_from_payload(payload: dict[str, Any]) -> SplitPageDetector:
    return SplitPageDetector(
        edge_tolerance_ratio=float(payload.get(
            "split_edge_tolerance_ratio",
            PARSER_CONFIG.split_page.page_frame_edge_tolerance_ratio,
        )),
        min_content_inset=float(payload.get(
            "split_min_content_inset",
            PARSER_CONFIG.split_page.min_content_inset_pt,
        )),
    )


def _region_content(
    page_data: dict[str, Any],
    split_detector: SplitPageDetector,
    content_bbox: Any | None = None,
    *,
    exclude_frame_vectors: bool = True,
) -> dict[str, Any]:
    return split_detector.region_content(
        page_data,
        content_bbox,
        exclude_frame_vectors=exclude_frame_vectors,
    )


def _query_selected_shapes(
    matcher: VectorMatcher,
    bbox: dict[str, Any],
    *,
    slack: float,
    coord_space: str = "pdf",
) -> list[PathBase]:
    return matcher.query_bbox(
        bbox=(float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])),
        slack=slack,
        coord_space=coord_space,
    )


def _vector_record(vector: PathBase, *, index: int | None = None) -> dict[str, Any]:
    record = vector.to_dict()
    if index is not None:
        record["index"] = index
    return record


def _compact_record(record: dict[str, Any], vector_base: VectorBase) -> dict[str, Any]:
    index_by_id = {
        id(vector): vector_base.source_index(index)
        for index, vector in enumerate(vector_base.vectors)
    }
    polygon = record.get("polygon")
    compact = {
        "category": record["category"],
        "bbox": record["bbox"],
        "vectors": [
            _vector_record(vector, index=index_by_id.get(id(vector)))
            for vector in record["vectors"]
        ],
        "points": record["points"],
        "polygon": polygon.__geo_interface__ if polygon is not None else None,
    }
    if record.get("direction") is not None:
        compact["direction"] = record["direction"]
    return compact


def _parse_pages(payload: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for value in payload.get("pages", []):
        try:
            pages.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(dict.fromkeys(pages))


def _merged_symbols(
    pdf_path: Path,
    symbol_pages: list[int],
    split_detector: SplitPageDetector,
    cache_key: tuple[Any, ...],
) -> dict[str, Any]:
    """Extract and merge symbols across overview pages, caching the merged result."""
    cached = _lru_get(_symbol_cache, cache_key)
    if cached is not None:
        return cached
    results: list[dict[str, Any]] = []
    with PdfPageManager(pdf_path) as pages:
        for page_num in symbol_pages:
            if page_num < 1 or page_num > pages.page_count:
                continue
            page_data = pages.goto(page_num)
            split_result = split_detector.detect(page_data)
            content_bbox = split_result["content_bbox"]
            if content_bbox is None:
                continue
            content = split_detector.region_content(
                page_data,
                content_bbox,
                exclude_frame_vectors=True,
            )
            results.append(extract_symbols(content["vectors"], content["text"]))
    merged = merge_symbol_results(results)
    _lru_put(_symbol_cache, cache_key, merged, SYMBOL_CACHE_LIMIT)
    return merged


def _symbol_cache_key(
    pdf_path: Path,
    symbol_pages: list[int],
    payload: dict[str, Any],
) -> tuple[Any, ...]:
    stat = pdf_path.stat()
    return (
        str(pdf_path),
        int(stat.st_mtime_ns),
        int(stat.st_size),
        tuple(symbol_pages),
        float(payload.get(
            "split_edge_tolerance_ratio",
            PARSER_CONFIG.split_page.page_frame_edge_tolerance_ratio,
        )),
        float(payload.get(
            "split_min_content_inset",
            PARSER_CONFIG.split_page.min_content_inset_pt,
        )),
    )


def _persistence_directory(pdf_path: Path) -> Path:
    stat = pdf_path.stat()
    fingerprint = sha256(
        f"{pdf_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:20]
    directory = PERSISTENCE_ROOT / fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cache_digest(cache_key: tuple[Any, ...]) -> str:
    return sha256(repr(cache_key).encode("utf-8")).hexdigest()[:20]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def _read_persistence_index(pdf_path: Path) -> dict[str, Any]:
    stored = _read_json(_persistence_directory(pdf_path) / "index.json")
    if stored is not None and stored.get("version") == PERSISTENCE_VERSION:
        return stored
    return {
        "version": PERSISTENCE_VERSION,
        "latest_extract": {},
    }


def _update_persistence_index(pdf_path: Path, **updates: Any) -> None:
    directory = _persistence_directory(pdf_path)
    index = _read_persistence_index(pdf_path)
    index.update(updates)
    index["version"] = PERSISTENCE_VERSION
    _write_json(directory / "index.json", index)


def _serialize_symbol_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbols": [[vector.to_dict() for vector in group] for group in catalog["symbols"]],
        "records": _json_safe(catalog["records"]),
    }


def _deserialize_symbol_catalog(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbols": [
            [PathBase.from_record(vector) for vector in group]
            for group in value.get("symbols", [])
        ],
        "records": list(value.get("records", [])),
    }


def _persist_symbols(
    pdf_path: Path,
    cache_key: tuple[Any, ...],
    pages: list[int],
    catalog: dict[str, Any],
    result: dict[str, Any],
) -> None:
    filename = f"symbols_{_cache_digest(cache_key)}.json"
    _write_json(
        _persistence_directory(pdf_path) / filename,
        {
            "version": PERSISTENCE_VERSION,
            "pages": pages,
            "catalog": _serialize_symbol_catalog(catalog),
            "result": result,
        },
    )
    _update_persistence_index(pdf_path, latest_symbols=filename)


def _load_persisted_symbols(
    pdf_path: Path,
    cache_key: tuple[Any, ...] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[int]] | None:
    directory = _persistence_directory(pdf_path)
    filename = (
        _read_persistence_index(pdf_path).get("latest_symbols")
        if cache_key is None
        else f"symbols_{_cache_digest(cache_key)}.json"
    )
    if not isinstance(filename, str):
        return None
    stored = _read_json(directory / filename)
    if (
        not stored
        or stored.get("version") != PERSISTENCE_VERSION
        or not isinstance(stored.get("catalog"), dict)
    ):
        return None
    return (
        _deserialize_symbol_catalog(stored["catalog"]),
        dict(stored.get("result", {})),
        [int(page) for page in stored.get("pages", [])],
    )


def _hidden_text_ownership(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the transient frontend text lookup without a persisted module."""
    ownership: list[dict[str, Any]] = []
    for match in matches:
        record: dict[str, Any] = {
            "text_indices": [int(index) for index in match.get("text_indices", [])],
        }
        if "component_id" in match:
            record["component_id"] = match["component_id"]
        elif "endpoint_id" in match:
            record["endpoint_id"] = match["endpoint_id"]
        else:
            continue
        ownership.append(record)
    return ownership


def _persist_extract_info(pdf_path: Path, page: int, result: dict[str, Any]) -> None:
    filename = f"extract_page_{page}.json"
    persisted = {key: value for key, value in result.items() if key != "_text_ownership"}
    _write_json(_persistence_directory(pdf_path) / filename, persisted)
    index = _read_persistence_index(pdf_path)
    latest_extract = dict(index.get("latest_extract", {}))
    latest_extract[str(page)] = filename
    _update_persistence_index(pdf_path, latest_extract=latest_extract)


def _load_persisted_extract_info(pdf_path: Path, page: int) -> dict[str, Any] | None:
    filename = _read_persistence_index(pdf_path).get("latest_extract", {}).get(str(page))
    if not isinstance(filename, str):
        return None
    return _read_json(_persistence_directory(pdf_path) / filename)


def _bbox_overlap_ratio(a: dict[str, float], b: dict[str, float]) -> float:
    """Return the intersection area as a fraction of the smaller box's area."""
    ix0 = max(a["x0"], b["x0"])
    iy0 = max(a["y0"], b["y0"])
    ix1 = min(a["x1"], b["x1"])
    iy1 = min(a["y1"], b["y1"])
    inter_w = ix1 - ix0
    inter_h = iy1 - iy0
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    intersection = inter_w * inter_h
    area_a = max(a["x1"] - a["x0"], 0.0) * max(a["y1"] - a["y0"], 0.0)
    area_b = max(b["x1"] - b["x0"], 0.0) * max(b["y1"] - b["y0"], 0.0)
    smaller = min(area_a, area_b)
    if smaller <= 0:
        return 0.0
    return intersection / smaller


def _suppress_overlapping_matches(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop overlapping matches, keeping the one whose symbol has more vectors.

    A small symbol is often just a part of a larger symbol, so it is also found
    inside the larger symbol's match. Sorting by vector count and suppressing any
    box that overlaps an already-kept richer box removes those nested duplicates.
    """
    ordered = sorted(candidates, key=lambda item: item["vectors"], reverse=True)
    kept: list[dict[str, Any]] = []
    for candidate in ordered:
        if any(
            _bbox_overlap_ratio(candidate["box"], other["box"]) > SYMBOL_MATCH_OVERLAP_RATIO
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _symbol_payload(merged: dict[str, Any]) -> dict[str, Any]:
    """Serialize merged symbol results with each symbol normalized to a local origin."""
    symbols: list[dict[str, Any]] = []
    for group in merged["symbols"]:
        x0, y0, x1, y1 = bbox_from_shapes(group)
        shapes = [
            {
                "type": vector.type,
                "points": [(float(px) - x0, float(py) - y0) for px, py in vector.points],
                "dashed": bool(vector.is_dashed),
            }
            for vector in group
        ]
        symbols.append(
            {
                "shapes": shapes,
                "width": float(x1 - x0),
                "height": float(y1 - y0),
            }
        )
    records = [
        {
            "symbol": int(record["symbol"]),
            "name": record["type"],
            "description": record["descriptions"],
        }
        for record in merged["records"]
    ]
    return {"category": "symbols", "symbols": symbols, "records": records}


def _emit_progress(
    callback: Any | None,
    progress: int,
    stage: str,
    message: str,
) -> None:
    if callback is not None:
        callback({"progress": max(0, min(int(progress), 100)), "stage": stage, "message": message})


def _vector_indices(vectors: list[PathBase], index_by_id: dict[int, int]) -> list[int]:
    return sorted({index_by_id[id(vector)] for vector in vectors if id(vector) in index_by_id})


def _component_image_data_url(component: dict[str, Any], directory: str, index: int) -> str | None:
    shape = list(component.get("shape", []))
    if not shape:
        return None
    image_path = render_vector_text_png(
        VectorBase(shape),
        output_dir=directory,
        filename=f"component_{index}.png",
    )
    encoded = b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _is_tiny_nontext_entity(entity: dict[str, Any], vector_count: int, text_count: int) -> bool:
    """Return whether an entity is too small and sparse to justify an LLM request."""
    bbox = entity["bbox"]
    return (
        vector_count <= 20
        and text_count == 0
        and float(bbox["x1"]) - float(bbox["x0"]) <= 20.0
        and float(bbox["y1"]) - float(bbox["y0"]) <= 20.0
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, PathBase):
        return value.to_dict()
    if isinstance(value, VectorBase):
        return [_json_safe(vector) for vector in value.vectors]
    if isinstance(value, TextBase):
        return value.to_list()
    if isinstance(value, BaseGeometry):
        return value.__geo_interface__
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _extract_page_info(
    pdf_path: Path,
    page: int,
    payload: dict[str, Any],
    progress_callback: Any | None,
) -> dict[str, Any]:
    if not bool(payload.get("force_regenerate", False)):
        persisted_result = _load_persisted_extract_info(pdf_path, page)
        if persisted_result is not None:
            _emit_progress(progress_callback, 100, "restore", "Loaded saved Extract Info")
            return persisted_result
    symbol_pages = _parse_pages(payload)
    if not symbol_pages:
        raise ValueError("Extract Info requires at least one symbol overview page")
    _emit_progress(progress_callback, 3, "symbols", "Loading symbol catalog")
    split_detector = _split_detector_from_payload(payload)
    symbol_key = _symbol_cache_key(pdf_path, symbol_pages, payload)
    symbol_catalog = _lru_get(_symbol_cache, symbol_key)
    if symbol_catalog is None:
        persisted_symbols = _load_persisted_symbols(pdf_path, symbol_key)
        if persisted_symbols is not None:
            symbol_catalog = persisted_symbols[0]
            _lru_put(_symbol_cache, symbol_key, symbol_catalog, SYMBOL_CACHE_LIMIT)
    if symbol_catalog is None:
        raise ValueError(
            "Symbol extraction result is unavailable; run Symbols extraction first"
        )

    _emit_progress(progress_callback, 12, "page", f"Loading page {page}")
    _, page_data = _cached_page_data(pdf_path, page)
    split_result = split_detector.detect(page_data)
    info_bbox = split_result.get("info_bbox")
    if info_bbox is None:
        info_table = reconstruct_table([], [], [])
    else:
        _emit_progress(progress_callback, 16, "info_table", "Extracting page information table")
        info_content = _region_content(
            page_data,
            split_detector,
            info_bbox,
            exclude_frame_vectors=False,
        )
        extracted_table = extract_table(info_content)
        info_table = reconstruct_table(
            extracted_table["raw_cells"],
            extracted_table["texts"],
            extracted_table["vectors"],
        )
    content = _region_content(
        page_data,
        split_detector,
        split_result["content_bbox"],
        exclude_frame_vectors=True,
    )
    content_base = content["vectors"]
    index_by_vector_id = {
        id(vector): content_base.source_index(index)
        for index, vector in enumerate(content_base.vectors)
    }
    _emit_progress(progress_callback, 20, "entities", "Grouping page vectors into entities")
    entities = VectorDisjointSet(content_base).entity_records()
    content_texts = TextMatcher(content["text"])
    llm_provider = str(payload.get("llm_provider") or DEFAULT_LLM_CONFIG.provider)
    base_llm_config = LLMConfig.local() if llm_provider == "local" else DEFAULT_LLM_CONFIG
    config_kwargs: dict[str, Any] = {
        "provider": llm_provider,
        "model": str(payload.get("llm_model") or base_llm_config.model),
        "base_url": str(payload.get("llm_base_url") or base_llm_config.base_url),
        "timeout_seconds": float(payload.get(
            "llm_timeout",
            base_llm_config.timeout_seconds,
        )),
        "reasoning_effort": (
            None
            if llm_provider == "local"
            else base_llm_config.reasoning_effort
        ),
    }
    if payload.get("llm_api_key"):
        config_kwargs["api_key"] = str(payload["llm_api_key"])
    elif base_llm_config.api_key:
        config_kwargs["api_key"] = base_llm_config.api_key
    if payload.get("llm_storage_directory"):
        config_kwargs["storage_directory"] = str(payload["llm_storage_directory"])
    use_llm_gate = bool(payload.get("use_llm_gate", True))
    classifier = (
        PersistentDiagramClassifier(LLMConfig(**config_kwargs), pdf_path)
        if use_llm_gate
        else None
    )
    entity_results: list[dict[str, Any]] = []
    remaining_vectors: list[PathBase] = []

    # Interactive page extraction uses an isolated temporary directory that is
    # removed as soon as the request finishes; entity previews never reach storage.
    with TemporaryDirectory() as image_directory:
        for entity_index, entity in enumerate(entities):
            progress = 22 + int(50 * (entity_index + 1) / max(len(entities), 1))
            _emit_progress(
                progress_callback,
                progress,
                "entity",
                f"Processing entity {entity_index + 1}/{len(entities)}",
            )
            entity_vectors = VectorBase(entity["vectors"])
            entity_texts = content_texts.text_in_box(entity["bbox"])
            if use_llm_gate:
                if _is_tiny_nontext_entity(entity, len(entity_vectors), len(entity_texts)):
                    remaining_vectors.extend(entity_vectors.vectors)
                    _emit_progress(
                        progress_callback,
                        progress,
                        "entity",
                        f"Skipped tiny non-text entity {entity_index + 1}/{len(entities)}",
                    )
                    continue
                if classifier is None:
                    raise RuntimeError("Persistent diagram classifier is unavailable")

                decision = classifier.classify_entity(
                    page_number=page,
                    entity_index=entity_index + 1,
                    vectors=entity_vectors,
                    texts=entity_texts,
                    image_factory=lambda: render_vector_text_png(
                        entity_vectors,
                        entity_texts,
                        output_dir=image_directory,
                        filename=f"entity_{entity_index}.png",
                    ),
                )
                classification = decision.classification
                _emit_progress(
                    progress_callback,
                    progress,
                    "entity",
                    f"{'Cache' if decision.cache_hit else 'LLM'} judged entity "
                    f"{entity_index + 1}/{len(entities)} as {classification.diagram_type}",
                )
                if classification.diagram_type != "electrical":
                    remaining_vectors.extend(entity_vectors.vectors)
                    continue
            extracted, entity_remaining = extract_diagram(
                entity_vectors,
                entity_texts,
                symbol_catalog["symbols"],
            )
            entity_results.append(extracted)
            remaining_vectors.extend(entity_remaining.vectors)

        _emit_progress(progress_callback, 76, "merge", "Merging extracted entities")
        page_result = merge_diagram_results(entity_results)
        page_remaining = assign_remaining_vectors_to_elements(
            VectorBase(remaining_vectors),
            page_result["elements"],
        )
        merge_adjacent_box_elements(page_result["elements"])
        endpoint_tools = EndpointTools(
            page_result["wires"],
            page_result["elements"],
        )
        endpoints = endpoint_tools.build()
        page_result["endpoints"] = endpoints
        page_result["relations"] = endpoint_tools.relations
        text_result = TextMatcher(content["text"]).match_elements(
            page_result["elements"],
            endpoints=endpoints,
            symbol_records=symbol_catalog["records"],
        )
        hyperlink_info = extract_hyperlinks(
            page_data["texts"],
            page_data["links"],
            page_number=page,
            page_count=page_data["page_count"],
        )
        relations = organize_relation(page_result)
        page_result.update(handle_relations(page_result, relations))
        element_to_component = {
            element_id: component["id"]
            for component in page_result["components"]
            for element_id in component["element_ids"]
        }
        for match in text_result["matches"]:
            if "element_id" in match:
                element_id = match.pop("element_id")
                match["component_id"] = element_to_component.get(element_id, element_id)
        attached_links = attach_hyperlinks(
            hyperlink_info["hyperlinks"],
            page_result["components"],
            text_matches=text_result["matches"],
        )
        page_links = split_transfers(
            attached_links,
            page_result["components"],
            page_result["elements"],
        )
        page_result["remaining_vector"] = page_remaining
        page_result["remaining_text"] = text_result["remaining_text"]
        diagram = serialize_diagram(page_result, page=page)
        for element in diagram["elements"]:
            element["vector_indices"] = _vector_indices(element.get("shape", []), index_by_vector_id)

    result = {
        "category": "extract_info",
        "page_number": page,
        "symbol_pages": symbol_pages,
        "diagram": diagram,
        "info_table": info_table,
        "crosspage_relations": page_links,
        "_text_ownership": _hidden_text_ownership(text_result["matches"]),
    }
    serialized = _json_safe(result)
    _persist_extract_info(pdf_path, page, serialized)
    _emit_progress(progress_callback, 100, "complete", "Page extraction complete and saved")
    return serialized


def handle(payload: dict[str, Any], progress_callback: Any | None = None) -> dict[str, Any]:
    pdf_path = resolve_repo_relative(str(payload["pdf_path"]))
    page = int(payload["page"])
    mode = str(payload.get("mode", "match"))
    coord_space = str(payload.get("coord_space", "pdf"))
    search_scope = str(payload.get("search_scope", "global"))
    search_page = int(payload.get("search_page", page))
    missing_vector_ratio = float(payload.get(
        "missing_vector_ratio",
        PARSER_CONFIG.vector_matcher.missing_vector_ratio,
    ))
    scale_min = float(payload.get(
        "scale_min",
        PARSER_CONFIG.vector_matcher.scale_min,
    ))
    scale_max = float(payload.get(
        "scale_max",
        PARSER_CONFIG.vector_matcher.pattern_scale_max,
    ))
    if missing_vector_ratio < 0 or missing_vector_ratio >= 1:
        raise ValueError("missing_vector_ratio must be in the range [0, 1)")
    if scale_min <= 0 or scale_max <= 0 or scale_min > scale_max:
        raise ValueError("scale range must be positive and ordered")

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if mode == "persisted_state":
        symbols_result, symbols_result_filename = _document_symbol_result(pdf_path)
        document_extract_info, document_result_filename = _document_extract_info(
            pdf_path,
            page,
        )
        return {
            "category": "persisted_state",
            "symbols": symbols_result,
            "symbols_source": "document_result" if symbols_result is not None else None,
            "extract_info": document_extract_info,
            "extract_info_source": "document_result" if document_extract_info is not None else None,
            "document_result_filename": document_result_filename or symbols_result_filename,
        }

    if mode == "trace_index":
        trace_index, document_result_filename = _document_trace_index(pdf_path)
        if trace_index is None:
            raise FileNotFoundError(
                f"Document parsing result not found for trace index: {pdf_path.stem}.json"
            )
        return {
            **trace_index,
            "document_result_filename": document_result_filename,
        }

    if mode == "extract_info":
        return _extract_page_info(pdf_path, page, payload, progress_callback)

    if mode == "symbols":
        symbol_pages = _parse_pages(payload)
        if not symbol_pages:
            raise ValueError("No symbol overview pages provided")
        split_detector = _split_detector_from_payload(payload)
        cache_key = _symbol_cache_key(pdf_path, symbol_pages, payload)
        restored = None
        if not bool(payload.get("force_regenerate", False)):
            restored = _load_persisted_symbols(pdf_path, cache_key)
        if restored is not None:
            merged, result, _ = restored
            _lru_put(_symbol_cache, cache_key, merged, SYMBOL_CACHE_LIMIT)
            result["restored"] = True
            return result
        if bool(payload.get("force_regenerate", False)):
            _symbol_cache.pop(cache_key, None)
        merged = _merged_symbols(pdf_path, symbol_pages, split_detector, cache_key)
        result = _symbol_payload(merged)
        result["pages"] = symbol_pages
        _persist_symbols(pdf_path, cache_key, symbol_pages, merged, result)
        return result

    if mode == "symbol_search":
        symbol_pages = _parse_pages(payload)
        if not symbol_pages:
            raise ValueError("No symbol overview pages provided")
        split_detector = _split_detector_from_payload(payload)
        cache_key = _symbol_cache_key(pdf_path, symbol_pages, payload)
        merged = _merged_symbols(pdf_path, symbol_pages, split_detector, cache_key)
        symbols = merged["symbols"]

        page_key, page_data = _cached_page_data(pdf_path, page)
        page_height = float(page_data["page_height_pt"])
        split_result = split_detector.detect(page_data)
        content_bbox = split_result["content_bbox"]
        search_region = _region_content(
            page_data,
            split_detector,
            content_bbox,
            exclude_frame_vectors=True,
        )
        selected_indices = {int(index) for index in payload.get("entity_indices", [])}
        if not selected_indices:
            raise ValueError("Select a vector entity before searching for symbols")
        entity_vectors = []
        entity_source_indices = []
        for vector_id, vector in enumerate(search_region["vectors"].vectors):
            source_index = search_region["vectors"].source_index(vector_id)
            if source_index in selected_indices:
                entity_vectors.append(vector)
                entity_source_indices.append(source_index)
        if not entity_vectors:
            raise ValueError("Selected entity was not found on the current page")
        entity_base = VectorBase(entity_vectors, source_indices=entity_source_indices)
        matcher = VectorMatcher(entity_base, page_height_pt=page_height)

        candidates: list[dict[str, Any]] = []
        for index, group in enumerate(symbols):
            if not group:
                continue
            matches = matcher.match_pattern(
                group,
                missing_vector_ratio=missing_vector_ratio,
                scale_range=(scale_min, scale_max),
            )
            for match in matches:
                candidates.append(
                    {
                        "symbol": index,
                        "vectors": len(group),
                        "box": _mupdf_bbox_to_pdf(match["bbox_mupdf"], page_height),
                    }
                )

        kept = _suppress_overlapping_matches(candidates)
        boxes_by_symbol: dict[int, list[dict[str, float]]] = {}
        for candidate in kept:
            boxes_by_symbol.setdefault(candidate["symbol"], []).append(candidate["box"])
        symbol_matches = [
            {"symbol": symbol_index, "boxes": boxes}
            for symbol_index, boxes in sorted(boxes_by_symbol.items())
        ]

        return {
            "category": "symbol_search",
            "page": page,
            "pages": symbol_pages,
            "entity_root": int(payload.get("entity_root", min(entity_source_indices))),
            "symbol_count": len(symbols),
            "matches": symbol_matches,
        }

    if mode == "split_page":
        page_key, page_data = _cached_page_data(pdf_path, page)
        result_key = _result_cache_key(page_key, payload, mode)
        cached_result = _lru_get(_result_cache, result_key)
        if cached_result is not None:
            return cached_result

        split_detector = _split_detector_from_payload(payload)
        split_result = split_detector.detect(page_data)
        result = {
            "category": "split_page",
            "page_number": split_result["page_number"],
            "page_bbox": split_result["page_bbox"],
            "content_bbox": split_result["content_bbox"],
            "info_bbox": split_result["info_bbox"],
        }
        _lru_put(_result_cache, result_key, result, RESULT_CACHE_LIMIT)
        return result

    if mode in {"entities", "entity_region"}:
        page_key, page_data = _cached_page_data(pdf_path, page)
        result_key = _result_cache_key(page_key, payload, mode)
        cached_result = _lru_get(_result_cache, result_key)
        if cached_result is not None:
            return cached_result

        page_height = float(page_data["page_height_pt"])
        split_detector = _split_detector_from_payload(payload)
        split_result = split_detector.detect(page_data)
        content_bbox = split_result["content_bbox"]
        content_region = _region_content(
            page_data,
            split_detector,
            content_bbox,
            exclude_frame_vectors=True,
        )
        content_base = content_region["vectors"]
        if "bbox" in payload:
            content_bbox = (
                payload["bbox"]
                if str(payload.get("coord_space", "pdf")) == "mupdf"
                else _pdf_bbox_to_mupdf(payload["bbox"], page_height)
            )
            content_region = _region_content(page_data, split_detector, content_bbox)
            content_base = content_region["vectors"]
        entities = VectorDisjointSet(content_base)
        if mode == "entity_region":
            selected_indices = {int(index) for index in payload.get("entity_indices", [])}
            selected_root = int(payload["entity_root"]) if "entity_root" in payload else None
            selected_record: dict[str, Any] | None = None
            selected_internal_root: int | None = None
            for internal_root, vector_ids in entities.groups().items():
                source_indices = {
                    entities.vector_base.source_index(vector_id)
                    for vector_id in vector_ids
                }
                record_root = min(source_indices) if source_indices else int(internal_root)
                if (selected_indices and source_indices == selected_indices) or (
                    selected_root is not None and record_root == selected_root
                ):
                    selected_internal_root = entities.find(internal_root)
                    entity = entities.entities[selected_internal_root]
                    selected_record = {
                        "category": "entity",
                        "bbox": bbox_to_dict(entity.bbox or (0.0, 0.0, 0.0, 0.0)),
                        "vectors": [
                            entities.vectors[vector_id].vector
                            for vector_id in sorted(vector_ids)
                        ],
                        "points": [
                            entities.points[point_id]
                            for point_id in sorted(entity.point_ids)
                        ],
                        "polygon": entities.entity_merged_face(selected_internal_root),
                    }
                    break

            if selected_internal_root is None or selected_record is None:
                raise ValueError("Selected entity was not found on the rebuilt page entities")

            result = _compact_record(selected_record, content_base)
            _lru_put(_result_cache, result_key, result, RESULT_CACHE_LIMIT)
            return result

        entity_result = entities.result()
        source_index_by_vector_id = {
            id(vector): content_base.source_index(vector_id)
            for vector_id, vector in enumerate(content_base.vectors)
        }
        for record in entity_result:
            source_indices = [
                source_index_by_vector_id[id(vector)]
                for vector in record["vectors"]
            ]
            entity_key = tuple(sorted(source_indices))
            if entity_key:
                _lru_put(
                    _entity_base_cache,
                    (page_key, entity_key),
                    VectorBase(record["vectors"], source_indices=source_indices),
                    ENTITY_CACHE_LIMIT,
                )
        result = {
            "category": "entities",
            "entities": [
                _compact_record(record, content_base)
                for record in entity_result
            ],
        }
        _lru_put(_result_cache, result_key, result, RESULT_CACHE_LIMIT)
        return result

    if mode in {"boxes", "circles", "dashed", "groups", "cells", "pin_circles", "pin_arrows", "pin_wire_marks"}:
        page_key = _page_cache_key(pdf_path, page)
        result_key = _result_cache_key(page_key, payload, mode)
        cached_result = _lru_get(_result_cache, result_key)
        if cached_result is not None:
            return cached_result

        selected_indices = tuple(sorted(int(index) for index in payload.get("entity_indices", [])))
        content_base = _lru_get(_entity_base_cache, (page_key, selected_indices)) if selected_indices else None
        entity_vectors = payload.get("entity_vectors")
        if content_base is None and isinstance(entity_vectors, list):
            content_base = VectorBase(entity_vectors)
            if selected_indices:
                _lru_put(
                    _entity_base_cache,
                    (page_key, selected_indices),
                    content_base,
                    ENTITY_CACHE_LIMIT,
                )
        if content_base is not None:
            detector = VectorBoxDetector(content_base)
            if mode == "pin_circles":
                regions = VectorPinDetector(content_base).detect_circle_pins()
            elif mode == "pin_arrows":
                regions = VectorPinDetector(content_base).detect_arrow_pins()
            elif mode == "pin_wire_marks":
                regions = VectorPinDetector(content_base).detect_wire_mark()
            elif mode == "boxes":
                regions = detector.detect_boxes()
            elif mode == "circles":
                regions = detector.detect_circles()
            elif mode == "cells":
                regions = detector.detect_cells()
            elif mode == "groups":
                regions = detector.detect_groups()
            else:
                regions = detector.detect_dashed()
            result = {
                "category": mode,
                "regions": [
                    _compact_record(region, content_base)
                    for region in regions
                ],
            }
            _lru_put(_result_cache, result_key, result, RESULT_CACHE_LIMIT)
            return result

        with PdfPageManager(pdf_path) as pages:
            page_data = pages.goto(page)
            page_height = float(page_data["page_height_pt"])
            split_detector = _split_detector_from_payload(payload)
            content_bbox = None
            selected_index_set = set(selected_indices)
            if selected_index_set:
                split_result = split_detector.detect(page_data)
                content_region = _region_content(
                    page_data,
                    split_detector,
                    split_result["content_bbox"],
                    exclude_frame_vectors=False,
                )
                content_vectors = content_region["vectors"].vectors
                entity_vectors = []
                entity_source_indices = []
                for vector_id, vector in enumerate(content_vectors):
                    source_index = content_region["vectors"].source_index(vector_id)
                    if source_index in selected_index_set:
                        entity_vectors.append(vector)
                        entity_source_indices.append(source_index)
                content_vectors = entity_vectors
                content_base = VectorBase(entity_vectors, source_indices=entity_source_indices)
            elif "bbox" in payload:
                content_bbox = (
                    payload["bbox"]
                    if str(payload.get("coord_space", "pdf")) == "mupdf"
                    else _pdf_bbox_to_mupdf(payload["bbox"], page_height)
                )
                content_region = _region_content(page_data, split_detector, content_bbox)
                content_base = content_region["vectors"]
            else:
                content_region = _region_content(page_data, split_detector, content_bbox)
                content_base = content_region["vectors"]
            detector = VectorBoxDetector(content_base)
            if mode == "pin_circles":
                regions = VectorPinDetector(content_base, page_height_pt=page_height).detect_circle_pins()
            elif mode == "pin_arrows":
                regions = VectorPinDetector(content_base, page_height_pt=page_height).detect_arrow_pins()
            elif mode == "pin_wire_marks":
                regions = VectorPinDetector(content_base, page_height_pt=page_height).detect_wire_mark()
            elif mode == "boxes":
                regions = detector.detect_boxes()
            elif mode == "circles":
                regions = detector.detect_circles()
            elif mode == "cells":
                regions = detector.detect_cells()
            elif mode == "groups":
                regions = detector.detect_groups()
            else:
                regions = detector.detect_dashed()

        result = {
            "category": mode,
            "regions": [
                _compact_record(region, content_base)
                for region in regions
            ],
        }
        _lru_put(_result_cache, result_key, result, RESULT_CACHE_LIMIT)
        return result

    if mode not in {"match", "select"}:
        raise ValueError(f"Unsupported mode: {mode!r}")

    bbox = payload["bbox"]

    with PdfPageManager(pdf_path) as pages:
        page_data = pages.goto(page)
        split_detector = _split_detector_from_payload(payload)
        use_content_vectors = bool(payload.get("use_content_vectors", True))
        if use_content_vectors:
            vector_base = _region_content(page_data, split_detector)["vectors"]
        else:
            vector_base = page_data["vectors"]
        page_height = float(page_data["page_height_pt"])
        matcher = VectorMatcher(vector_base, page_height_pt=page_height)
        target_shape_records = payload.get("target_shapes")
        if mode == "match" and isinstance(target_shape_records, list) and target_shape_records:
            hits = [PathBase.from_record(record) for record in target_shape_records]
            target_source = "payload"
        else:
            hits = _query_selected_shapes(
                matcher,
                bbox,
                slack=float(payload.get(
                    "select_slack",
                    0.0,
                )),
                coord_space=coord_space,
            )
            target_source = "bbox"
        selected_bbox_mupdf = bbox_from_shapes(hits) if hits else None
        if target_source == "payload":
            selected_shape_records = [
                _vector_record(
                    vector,
                    index=(int(record["index"]) if isinstance(record, dict) and record.get("index") is not None else None),
                )
                for vector, record in zip(hits, target_shape_records)
            ]
        else:
            selected_shape_records = [
                _vector_record(vector, index=matcher.source_index(vector))
                for vector in hits
            ]

        result: dict[str, Any] = {
            "page": page,
            "source_vector_count": len(page_data["vectors"]),
            "vector_count": vector_base.vector_count,
            "selected_shape_count": len(hits),
            "target_source": target_source,
            "selected_bbox_pdf": _mupdf_bbox_to_pdf(selected_bbox_mupdf, page_height) if selected_bbox_mupdf else None,
            "selected_shapes": selected_shape_records,
        }

        if mode == "select":
            return result

        all_matches: list[dict[str, Any]] = []
        if search_scope == "current":
            search_pages = [search_page]
        else:
            search_pages = list(range(1, pages.page_count + 1))

        for match_page in search_pages:
            match_page_data = pages.goto(match_page)
            match_vector_base = (
                _region_content(match_page_data, split_detector)["vectors"]
                if use_content_vectors
                else match_page_data["vectors"]
            )
            match_page_height = float(match_page_data["page_height_pt"])
            page_matcher = VectorMatcher(match_vector_base, page_height_pt=match_page_height)
            for match in page_matcher.match_pattern(
                hits,
                missing_vector_ratio=missing_vector_ratio,
                scale_range=(scale_min, scale_max),
            ):
                all_matches.append(
                    {
                        **match,
                        "page_number": match_page,
                        "bbox_pdf": _mupdf_bbox_to_pdf(match["bbox_mupdf"], match_page_height),
                    }
                )

        result["searched_page_count"] = len(search_pages)
        result["target_page"] = page
        result["search_pages"] = search_pages
        result["matches"] = all_matches
        return result


def _response(payload: dict[str, Any], progress_callback: Any | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, "result": handle(payload, progress_callback)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _serve() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            request_id = request.get("id")

            def progress_callback(event: dict[str, Any]) -> None:
                print(json.dumps({"id": request_id, "event": "progress", **event}), flush=True)

            response = _response(request.get("payload", {}), progress_callback)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        response["id"] = request.get("id")
        # Keep the line protocol independent of the Windows console code page.
        # JSON escapes are decoded back to Unicode by JSON.parse in the Vite worker.
        print(json.dumps(response, ensure_ascii=True), flush=True)


def main() -> None:
    if "--server" in sys.argv:
        _serve()
        return
    response = _response(json.loads(sys.stdin.read()))
    print(json.dumps(response, ensure_ascii=True))
    if not response["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
