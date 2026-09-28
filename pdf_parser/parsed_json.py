"""Build the compact parsed JSON exposed to downstream work packages.

Entities and edges receive document-qualified global ids shared with the parsed
graph database: ``<pdf stem>/p<page>/<kind>/<page-local id or index>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pdf_parser.diagram_pdf_parser import save_pdf_info


ENTITY_COLLECTIONS = {
    "components": "component",
    "endpoints": "endpoint",
    "wires": "wire",
    "nets": "net",
    "groups": "group",
}
LINK_COLLECTIONS = {"hyperlinks": "hyperlink", "transfers": "transfer"}
DROPPED_DIAGRAM_FIELDS = frozenset({"elements", "remaining_vectors", "symbols"})
DROPPED_INFO_TABLE_FIELDS = frozenset({"rows", "columns", "raw_cells"})
CELL_FIELDS = ("bbox", "text", "row", "col", "rowspan", "colspan")


def build_parsed_json(
    pdf_info: Mapping[str, Any],
    *,
    filename: str | None = None,
) -> dict[str, Any]:
    """Return parser output without internals, using global entity and edge ids."""
    document = dict(pdf_info.get("document", {}))
    if filename is not None:
        document["filename"] = filename
    parsed = {key: value for key, value in pdf_info.items() if key != "pages"}
    parsed["document"] = document
    if isinstance(pdf_info.get("symbol_overview"), Mapping):
        parsed["symbol_overview"] = {
            "records": list(pdf_info["symbol_overview"].get("records", [])),
        }
    parsed["pages"] = {
        page_num: _page(page_record, str(document["filename"]), int(page_num))
        for page_num, page_record in pdf_info.get("pages", {}).items()
    }
    return parsed


def global_id(filename: str, page: Any, kind: str, local_id: Any) -> str:
    """Return the id shared by parsed JSON and the parsed graph database."""
    return f"{Path(filename).stem}/p{int(page)}/{kind}/{local_id}"


def save_parsed_json(parsed: dict[str, Any], output_directory: str | Path) -> Path:
    """Save built parsed JSON as ``<output_directory>/<pdf stem>.json``."""
    stem = Path(str(parsed["document"]["filename"])).stem
    return save_pdf_info(parsed, Path(output_directory) / f"{stem}.json")


def _page(page_record: Mapping[str, Any], filename: str, page_num: int) -> dict[str, Any]:
    page = dict(page_record)
    if isinstance(page.get("info_table"), Mapping):
        page["info_table"] = _info_table(page["info_table"])
    if isinstance(page.get("diagram"), Mapping):
        page["diagram"] = _diagram(page["diagram"], filename, page_num)
    return page


def _info_table(info_table: Mapping[str, Any]) -> dict[str, Any]:
    table = {
        key: value
        for key, value in info_table.items()
        if key not in DROPPED_INFO_TABLE_FIELDS
    }
    table["cells"] = [
        {
            **{field: cell.get(field) for field in CELL_FIELDS},
            "bbox": list(cell.get("bbox") or []),
        }
        for cell in info_table.get("cells", [])
    ]
    return table


def _diagram(diagram: Mapping[str, Any], filename: str, page: int) -> dict[str, Any]:
    compact = {
        key: value
        for key, value in diagram.items()
        if key not in DROPPED_DIAGRAM_FIELDS
    }
    for collection, kind in ENTITY_COLLECTIONS.items():
        if isinstance(compact.get(collection), list):
            compact[collection] = [
                {
                    **{key: value for key, value in entity.items() if key != "elements"},
                    "id": global_id(filename, page, kind, entity["id"]),
                }
                for entity in compact[collection]
            ]
    if isinstance(compact.get("relations"), list):
        compact["relations"] = [
            {
                "id": global_id(filename, page, "relation", index),
                **relation,
                "source": _reference_id(filename, page, relation.get("source")),
                "target": _reference_id(filename, page, relation.get("target")),
            }
            for index, relation in enumerate(compact["relations"])
        ]
    for collection, kind in LINK_COLLECTIONS.items():
        if isinstance(compact.get(collection), list):
            compact[collection] = [
                {
                    "id": global_id(filename, page, kind, index),
                    **link,
                    "source_component": _component_id(
                        filename, link.get("source_page"), link.get("source_component")
                    ),
                    "target_component": _component_id(
                        filename, link.get("target_page"), link.get("target_component")
                    ),
                }
                for index, link in enumerate(compact[collection])
            ]
    if isinstance(compact.get("remaining_text"), list):
        compact["remaining_text"] = [
            {
                "id": global_id(filename, page, "text", index),
                **{key: value for key, value in text.items() if key != "text_indices"},
                "nearby": [
                    global_id(filename, page, "component", component_id)
                    for component_id in text.get("nearby", [])
                ],
            }
            for index, text in enumerate(compact["remaining_text"])
        ]
    return compact


def _reference_id(filename: str, page: int, reference: Any) -> Any:
    """Convert a page-local ``kind:id`` relation endpoint to a global id."""
    kind, separator, local_id = str(reference).partition(":")
    if not separator or kind not in ENTITY_COLLECTIONS.values():
        return reference
    return global_id(filename, page, kind, local_id)


def _component_id(filename: str, page: Any, component_id: Any) -> str | None:
    if component_id is None or page is None:
        return None
    return global_id(filename, page, "component", component_id)
