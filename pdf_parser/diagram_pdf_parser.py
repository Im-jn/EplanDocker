"""Functional orchestration for extracting electrical-diagram information from a PDF."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Any

from shapely.geometry.base import BaseGeometry
from tqdm.auto import tqdm

from pdf_parser.processors.diagram_extractor import (
    assign_remaining_vectors_to_elements,
    extract_diagram,
    merge_diagram_results,
)
from pdf_parser.processors.hyperlink_extractor import (
    attach_hyperlink_targets,
    attach_hyperlinks,
    attach_transfer_targets,
    extract_hyperlinks,
    split_transfers,
)
from pdf_parser.processors.diagram_serializer import serialize_diagram
from pdf_parser.processors.page_classifier import (
    DIAGRAM_TYPES,
    SYMBOL_OVERVIEW,
    classify_info_texts,
)
from pdf_parser.processors.relation_composer import handle_relations
from pdf_parser.processors.relation_extractor import (
    merge_adjacent_box_elements,
    organize_relation,
)
from pdf_parser.processors.symbol_extractor import extract_symbols, merge_symbol_results
from pdf_parser.processors.table_extractor import extract_table, reconstruct_table
from pdf_parser.llm_judger import LLMConfig, PersistentDiagramClassifier
from pdf_parser.pages_manager import PdfPageManager
from pdf_parser.pages_manager.page_class import PathBase, TextBase, VectorBase
from pdf_parser.pages_manager.split_page import SplitPageDetector
from pdf_parser.tools.text_matcher import TextMatcher
from pdf_parser.tools.endpoints_tools import EndpointTools
from pdf_parser.tools.vector_entity import VectorDisjointSet
from pdf_parser.tools.vector_visualize import render_vector_text_png
from pdf_parser.utils import resolve_repo_relative


ProgressCallback = Callable[[str], None]


def save_pdf_info(pdf_info: dict[str, Any], output_file: str | Path) -> Path:
    """Convert parser output to plain data and atomically save it as JSON."""
    if not isinstance(pdf_info, dict):
        raise TypeError("pdf_info must be a dictionary")

    output_path = resolve_repo_relative(str(output_file))
    if output_path.suffix.lower() != ".json":
        raise ValueError("pdf_info output file must use the .json extension")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(
                _json_value(pdf_info),
                temporary_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return output_path


def load_pdf_info(input_file: str | Path) -> dict[str, Any]:
    """Load plain JSON output previously written by :func:`save_pdf_info`."""
    input_path = resolve_repo_relative(str(input_file))
    with input_path.open("r", encoding="utf-8") as input_stream:
        pdf_info = json.load(input_stream)
    if not isinstance(pdf_info, dict):
        raise TypeError("Saved pdf_info must be a dictionary")
    return pdf_info


def _json_value(value: Any) -> Any:
    """Return the minimal plain-data representation used by JSON persistence."""
    if isinstance(value, VectorBase):
        return [_json_value(record) for record in value.to_list()]
    if isinstance(value, TextBase):
        return [_json_value(record) for record in value.to_list()]
    if isinstance(value, PathBase):
        return _json_value(value.to_dict())
    if isinstance(value, BaseGeometry):
        return _json_value(value.__geo_interface__)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def parse_diagram_pdf(
    pdf_file_path: str | Path,
    *,
    llm_config: LLMConfig | None = None,
    progress_callback: ProgressCallback | None = None,
    entity_image_directory: str | Path = "./storage/output/images/entities",
    remaining_image_directory: str | Path = "./storage/output/images",
    checkpoint_file: str | Path | None = None,
    resume: bool = True,
    show_page_progress: bool = False,
) -> dict[str, Any]:
    """Classify pages from their info tables and extract supported drawing content."""
    started_at = perf_counter()
    pdf_path = resolve_repo_relative(str(pdf_file_path))
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    report = progress_callback or _ignore_progress
    llm_config = llm_config or LLMConfig()
    diagram_classifier = PersistentDiagramClassifier(llm_config, pdf_path)
    checkpoint_path = (
        resolve_repo_relative(str(checkpoint_file)) if checkpoint_file is not None else None
    )
    entity_image_directory = resolve_repo_relative(str(entity_image_directory))
    remaining_image_directory = resolve_repo_relative(str(remaining_image_directory))
    symbol_results: list[dict[str, Any]] = []
    diagram_page_numbers: list[int] = []

    with PdfPageManager(pdf_path) as pages:
        report(f"Opened {pdf_path.name} ({pages.page_count} pages)")
        report("Detecting page content region from page 1")
        page_data = pages.goto(1)
        split_detector = SplitPageDetector()
        splited_box = split_detector.detect(page_data)
        if splited_box is None:
            raise ValueError("No content region found")
        content_bbox = splited_box["content_bbox"]
        info_bbox = splited_box["info_bbox"]
        if info_bbox is None:
            raise ValueError("No information region found")

        pdf_info: dict[str, Any] = {
            "pages": {},
            "symbol_overview": {},
            "crosspage_relations": {
                "hyperlinks": [],
                "transfers": [],
            },
        }
        report("Scanning page information regions")
        page_numbers = tqdm(
            range(1, pages.page_count + 1),
            desc="[pdf_parser] Classifying pages",
            unit="page",
            dynamic_ncols=True,
            disable=None if show_page_progress else True,
        )
        for page_num in page_numbers:
            page_data = pages.goto_region(page_num, info_bbox)
            info_content = split_detector.region_content(
                page_data,
                info_bbox,
                exclude_frame_vectors=False,
            )
            extracted_table = extract_table(info_content)
            info_table = reconstruct_table(
                extracted_table["raw_cells"],
                extracted_table["texts"],
                extracted_table["vectors"],
            )
            page_type = classify_info_texts((info_table["html"],))
            if page_type is None:
                page_type = classify_info_texts(info_content["text"])
            page_record = {
                "page_type": page_type,
                "info_table": info_table,
                "crosspage_relations": {
                    "hyperlinks": [],
                    "transfers": [],
                },
                "diagram": {},
            }
            pdf_info["pages"][page_num] = page_record

            if page_type == SYMBOL_OVERVIEW:
                drawing_page_data = pages.goto(page_num)
                content = split_detector.region_content(
                    drawing_page_data,
                    content_bbox,
                    exclude_frame_vectors=True,
                )
                symbols = extract_symbols(content["vectors"], content["text"])
                symbol_results.append(symbols)
                page_record["diagram"] = symbols
            elif page_type in DIAGRAM_TYPES:
                diagram_page_numbers.append(page_num)
            page_numbers.set_postfix(
                type=page_type or "unsupported",
                cells=len(info_table["cells"]),
            )

        symbol_catalog = merge_symbol_results(symbol_results)
        symbol_list = symbol_catalog["symbols"]
        report(f"Symbol catalog ready: {len(symbol_list)} unique symbols")

        pdf_info["symbol_overview"] = symbol_catalog
        completed_pages: set[int] = set()
        if resume and checkpoint_path is not None and checkpoint_path.is_file():
            completed_pages = _restore_checkpoint(
                checkpoint_path,
                document_id=diagram_classifier.store.document_id,
                page_count=pages.page_count,
                pdf_info=pdf_info,
            )
            report(
                f"Resuming from {checkpoint_path.name}: "
                f"{len(completed_pages)} diagram pages already complete"
            )
        for page_index, page_num in enumerate(diagram_page_numbers, start=1):
            if page_num in completed_pages:
                page_record = pdf_info["pages"][page_num]
                for collection in ("hyperlinks", "transfers"):
                    pdf_info["crosspage_relations"][collection].extend(
                        page_record.get("crosspage_relations", {}).get(collection, [])
                    )
                report(
                    f"Diagram page {page_index}/{len(diagram_page_numbers)}: "
                    f"page {page_num} restored from checkpoint"
                )
                continue
            page_started_at = perf_counter()
            report(
                f"Diagram page {page_index}/{len(diagram_page_numbers)}: page {page_num}"
            )
            page_data = pages.goto(page_num)
            content = split_detector.region_content(
                page_data,
                content_bbox,
                exclude_frame_vectors=True,
            )
            content_entities = VectorDisjointSet(content["vectors"]).entity_records()
            report(
                f"Page {page_num}: {len(content_entities)} entities, "
                f"{len(content['vectors'])} vectors, {len(content['text'])} texts"
            )

            content_texts = TextMatcher(content["text"])
            entity_results: list[dict[str, Any]] = []
            remaining_vector_records = []
            for entity_index, entity in enumerate(content_entities, start=1):
                entity_started_at = perf_counter()
                report(
                    f"Page {page_num}: judging entity {entity_index}/{len(content_entities)} "
                    f"({len(entity['vectors'])} vectors)"
                )
                entity_vectors = VectorBase(entity["vectors"])
                entity_texts = content_texts.text_in_box(entity["bbox"])
                if _is_tiny_nontext_entity(entity, len(entity_vectors), len(entity_texts)):
                    remaining_vector_records.extend(entity_vectors.vectors)
                    report(
                        f"Page {page_num}: entity {entity_index} skipped before LLM "
                        f"(tiny non-text entity, {len(entity_vectors)} vectors)"
                    )
                    continue

                def render_entity_image() -> Path:
                    entity_image = render_vector_text_png(
                        entity_vectors,
                        entity_texts,
                        output_dir=entity_image_directory,
                        filename=f"page_{page_num}_entity_{entity_index}.png",
                    )
                    return entity_image

                decision = diagram_classifier.classify_entity(
                    page_number=page_num,
                    entity_index=entity_index,
                    vectors=entity_vectors,
                    texts=entity_texts,
                    image_factory=render_entity_image,
                )
                classification = decision.classification

                report(
                    f"Page {page_num}: entity {entity_index} classified as "
                    f"{classification.diagram_type} "
                    f"(confidence={classification.confidence:.2f}, "
                    f"source={'cache' if decision.cache_hit else 'llm'})"
                )
                if classification.diagram_type != "electrical":
                    remaining_vector_records.extend(entity_vectors.vectors)
                    report(
                        f"Page {page_num}: entity {entity_index} skipped "
                        f"({perf_counter() - entity_started_at:.1f}s)"
                    )
                    continue

                extracted_content, entity_remaining_vectors = extract_diagram(
                    entity_vectors,
                    entity_texts,
                    symbol_list,
                )
                entity_results.append(extracted_content)
                remaining_vector_records.extend(entity_remaining_vectors.vectors)
                report(
                    f"Page {page_num}: entity {entity_index} finished, "
                    f"{len(extracted_content['elements'])} elements, "
                    f"{len(extracted_content['wires'])} wires "
                    f"({perf_counter() - entity_started_at:.1f}s)"
                )

            in_page_info = merge_diagram_results(entity_results)
            remaining_vector_base = assign_remaining_vectors_to_elements(
                VectorBase(remaining_vector_records),
                in_page_info["elements"],
            )
            merge_adjacent_box_elements(in_page_info["elements"])
            endpoint_tools = EndpointTools(
                in_page_info["wires"],
                in_page_info["elements"],
            )
            endpoints = endpoint_tools.build()
            in_page_info["endpoints"] = endpoints
            in_page_info["relations"] = endpoint_tools.relations
            text_result = TextMatcher(content["text"]).match_elements(
                in_page_info["elements"],
                endpoints=endpoints,
                symbol_records=symbol_catalog["records"],
            )
            relations = organize_relation(in_page_info)
            in_page_info.update(handle_relations(in_page_info, relations))
            element_to_component = {
                element_id: component["id"]
                for component in in_page_info["components"]
                for element_id in component["element_ids"]
            }
            for match in text_result["matches"]:
                if "element_id" in match:
                    element_id = match.pop("element_id")
                    match["component_id"] = element_to_component.get(element_id, element_id)

            hyperlink_info = extract_hyperlinks(
                page_data["texts"],
                page_data["links"],
                page_number=page_num,
                page_count=page_data["page_count"],
            )
            attached_links = attach_hyperlinks(
                hyperlink_info["hyperlinks"],
                in_page_info["components"],
                text_matches=text_result["matches"],
            )
            page_links = split_transfers(
                attached_links,
                in_page_info["components"],
                in_page_info["elements"],
            )
            in_page_info["remaining_vector"] = remaining_vector_base
            in_page_info["remaining_text"] = text_result["remaining_text"]
            pdf_info["pages"][page_num]["diagram"] = serialize_diagram(
                in_page_info,
                page=page_num,
            )
            pdf_info["pages"][page_num]["crosspage_relations"] = page_links
            for collection in ("hyperlinks", "transfers"):
                pdf_info["crosspage_relations"][collection].extend(
                    page_links[collection]
                )
            completed_pages.add(page_num)
            if checkpoint_path is not None:
                _save_checkpoint(
                    checkpoint_path,
                    document_id=diagram_classifier.store.document_id,
                    page_count=pages.page_count,
                    completed_pages=completed_pages,
                    pdf_info=pdf_info,
                )
            report(
                f"Page {page_num} finished: {len(in_page_info['components'])} components, "
                f"{len(text_result['matches'])} matched texts, "
                f"{len(text_result['remaining_text'])} remaining texts, "
                f"{len(page_links['hyperlinks'])} hyperlinks, "
                f"{len(page_links['transfers'])} transfers "
                f"({perf_counter() - page_started_at:.1f}s)"
            )

        report("Resolving hyperlink and transfer target components")
        components_by_page = {
            int(page_num): page_record["diagram"].get("components", [])
            for page_num, page_record in pdf_info["pages"].items()
            if isinstance(page_record.get("diagram"), dict)
        }
        elements_by_page = {
            int(page_num): page_record["diagram"].get("elements", [])
            for page_num, page_record in pdf_info["pages"].items()
            if isinstance(page_record.get("diagram"), dict)
        }
        for collection in ("hyperlinks", "transfers"):
            resolved_links = (
                attach_transfer_targets(
                    pdf_info["crosspage_relations"][collection],
                    components_by_page,
                    elements_by_page,
                )
                if collection == "transfers"
                else attach_hyperlink_targets(
                    pdf_info["crosspage_relations"][collection],
                    components_by_page,
                )
            )
            pdf_info["crosspage_relations"][collection] = resolved_links
            resolved_target_count = sum(
                link.get("target_component") is not None
                for link in resolved_links
            )
            report(
                f"Resolved {resolved_target_count}/"
                f"{len(resolved_links)} {collection} target components"
            )
            links_by_source_page: dict[int, list[dict[str, Any]]] = {}
            for link in resolved_links:
                source_page = int(link["source_page"])
                links_by_source_page.setdefault(source_page, []).append(link)
            for page_num, page_record in pdf_info["pages"].items():
                page_record["crosspage_relations"][collection] = (
                    links_by_source_page.get(int(page_num), [])
                )

    report(
        f"Finished {len(diagram_page_numbers)} diagram pages in "
        f"{perf_counter() - started_at:.1f}s"
    )
    return pdf_info


def _save_checkpoint(
    checkpoint_path: Path,
    *,
    document_id: str,
    page_count: int,
    completed_pages: set[int],
    pdf_info: dict[str, Any],
) -> None:
    """Atomically persist only fully completed diagram-page results."""
    completed = sorted(completed_pages)
    payload = {
        "version": 14,
        "document_id": document_id,
        "page_count": int(page_count),
        "completed_pages": completed,
        "pages": {
            str(page_num): {
                "diagram": pdf_info["pages"][page_num]["diagram"],
                "crosspage_relations": pdf_info["pages"][page_num][
                    "crosspage_relations"
                ],
            }
            for page_num in completed
        },
    }
    save_pdf_info(payload, checkpoint_path)


def _restore_checkpoint(
    checkpoint_path: Path,
    *,
    document_id: str,
    page_count: int,
    pdf_info: dict[str, Any],
) -> set[int]:
    """Restore completed pages, rejecting stale or malformed checkpoints."""
    payload = load_pdf_info(checkpoint_path)
    if (
        payload.get("version") != 14
        or payload.get("document_id") != document_id
        or payload.get("page_count") != page_count
    ):
        raise ValueError(
            f"Checkpoint does not belong to the current PDF: {checkpoint_path}"
        )
    completed_raw = payload.get("completed_pages")
    stored_pages = payload.get("pages")
    if not isinstance(completed_raw, list) or not isinstance(stored_pages, dict):
        raise ValueError(f"Invalid checkpoint: {checkpoint_path}")

    completed: set[int] = set()
    for raw_page_num in completed_raw:
        page_num = int(raw_page_num)
        stored_page = stored_pages.get(str(page_num))
        page_record = pdf_info["pages"].get(page_num)
        if not isinstance(stored_page, dict) or not isinstance(page_record, dict):
            raise ValueError(f"Invalid checkpoint page {page_num}: {checkpoint_path}")
        page_record["diagram"] = dict(stored_page.get("diagram", {}))
        stored_crosspage = stored_page.get("crosspage_relations", {})
        page_record["crosspage_relations"] = {
            "hyperlinks": list(stored_crosspage.get("hyperlinks", [])),
            "transfers": list(stored_crosspage.get("transfers", [])),
        }
        completed.add(page_num)
    return completed


def _is_tiny_nontext_entity(entity: dict[str, Any], vector_count: int, text_count: int) -> bool:
    """Return whether an entity is too small and sparse to justify an LLM request."""
    bbox = entity["bbox"]
    return (
        vector_count <= 20
        and text_count == 0
        and float(bbox["x1"]) - float(bbox["x0"]) <= 20.0
        and float(bbox["y1"]) - float(bbox["y0"]) <= 20.0
    )


def _ignore_progress(_: str) -> None:
    pass
