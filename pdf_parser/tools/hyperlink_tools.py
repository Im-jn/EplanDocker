"""Match normalized PDF link annotations to cross-reference text and components."""

from __future__ import annotations

import re
from collections import defaultdict
from math import floor
from typing import Any

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager.page_class import TextBase
from pdf_parser.utils import bbox_to_dict, coerce_bbox


_XREF_SLASH_RE = re.compile(r"([A-Za-z]{1,4}\d{0,3})\s*/\s*([A-Za-z0-9]{2})")
_XREF_DOT_RE = re.compile(r"(\d{1,3})\.([A-Za-z0-9])\s*[:.]\s*([A-Za-z0-9])\b")
_LINK_DESTINATION_REF_RE = re.compile(r"/(A|Dest)\s+(\d+)\s+\d+\s+R\b")
_NEXT_VALUE_RE = re.compile(
    r"/Next\s+(?:\[(?P<array>.*?)\]|(?P<single>\d+\s+\d+\s+R))",
    re.DOTALL,
)
_OBJECT_REF_RE = re.compile(r"(\d+)\s+\d+\s+R\b")
_HIGHLIGHT_JS_RE = re.compile(r"highlight\\?\([^[]*\[([^\]]+)\]", re.DOTALL)


def _iou(left: Any, right: Any) -> float:
    ax0, ay0, ax1, ay1 = coerce_bbox(left)
    bx0, by0, bx1, by1 = coerce_bbox(right)
    width = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    height = max(0.0, min(ay1, by1) - max(ay0, by0))
    intersection = width * height
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - intersection
    return intersection / union if union > 0 else 0.0


def _bbox_gap(left: Any, right: Any) -> float:
    ax0, ay0, ax1, ay1 = coerce_bbox(left)
    bx0, by0, bx1, by1 = coerce_bbox(right)
    dx = max(ax0 - bx1, bx0 - ax1, 0.0)
    dy = max(ay0 - by1, by0 - ay1, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _bbox_coverage(container: Any, target: Any) -> float:
    """Return the fraction of the target bbox covered by the container bbox."""
    ax0, ay0, ax1, ay1 = coerce_bbox(container)
    bx0, by0, bx1, by1 = coerce_bbox(target)
    width = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    height = max(0.0, min(ay1, by1) - max(ay0, by0))
    target_area = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    return width * height / target_area if target_area > 0 else 0.0


class _BBoxGrid:
    """Small page-local spatial index for bbox candidate lookups."""

    def __init__(self, boxes: list[Any], *, cell_size: float = 64.0):
        self.cell_size = float(cell_size)
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, value in enumerate(boxes):
            for cell in self._cells_for(value):
                self.cells[cell].append(index)

    def candidates(self, value: Any, *, padding: float = 0.0) -> list[int]:
        x0, y0, x1, y1 = coerce_bbox(value)
        query = (x0 - padding, y0 - padding, x1 + padding, y1 + padding)
        return sorted({
            index
            for cell in self._cells_for(query)
            for index in self.cells.get(cell, ())
        })

    def _cells_for(self, value: Any) -> list[tuple[int, int]]:
        x0, y0, x1, y1 = coerce_bbox(value)
        left, right = sorted((floor(x0 / self.cell_size), floor(x1 / self.cell_size)))
        top, bottom = sorted((floor(y0 / self.cell_size), floor(y1 / self.cell_size)))
        return [
            (column, row)
            for column in range(left, right + 1)
            for row in range(top, bottom + 1)
        ]


class HyperlinkTools:
    """Resolve cross-page references from normalized page links and a TextBase."""

    def __init__(self, links: list[dict[str, Any]], *, page_number: int, page_count: int):
        self.links = [dict(link) for link in links]
        self.page_number = int(page_number)
        self.page_count = int(page_count)

    @staticmethod
    def inspect_pdf_link(doc: Any, link: dict[str, Any]) -> dict[str, Any]:
        """Resolve the annotation action chain and its visible destination region."""
        xref = link.get("xref")
        annotation_source: str | None = None
        action_chain: list[dict[str, Any]] = []
        if isinstance(xref, int) and xref > 0:
            try:
                annotation_source = doc.xref_object(xref, compressed=False)
            except Exception:
                annotation_source = None
            if annotation_source is not None:
                action_chain = HyperlinkTools._linked_action_objects(doc, annotation_source)

        target_page_height = HyperlinkTools._target_page_height(doc, link.get("page"))
        return {
            "annotation_xref": xref if isinstance(xref, int) and xref > 0 else None,
            "annotation_source": annotation_source,
            "action_chain": action_chain,
            "target_highlight_region": HyperlinkTools._target_highlight_region(
                link,
                action_chain,
                target_page_height=target_page_height,
            ),
        }

    @staticmethod
    def _target_page_height(doc: Any, page_value: Any) -> float | None:
        if isinstance(page_value, bool):
            return None
        try:
            page_index = int(page_value) if isinstance(page_value, int) else int(str(page_value)) - 1
            if page_index < 0 or page_index >= int(doc.page_count):
                return None
            return float(doc[page_index].rect.height)
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _linked_action_objects(
        doc: Any,
        annotation_source: str,
    ) -> list[dict[str, Any]]:
        """Follow /A or /Dest and recursive /Next references with cycle protection."""
        pending = [
            (match.group(1), int(match.group(2)), 0)
            for match in _LINK_DESTINATION_REF_RE.finditer(annotation_source)
        ]
        resolved: list[dict[str, Any]] = []
        seen: set[int] = set()
        while pending:
            role, referenced_xref, depth = pending.pop(0)
            if referenced_xref in seen:
                continue
            seen.add(referenced_xref)
            try:
                source = doc.xref_object(referenced_xref, compressed=False)
            except Exception:
                source = None
            resolved.append(
                {
                    "role": role,
                    "xref": referenced_xref,
                    "depth": depth,
                    "source": source,
                }
            )
            if source is None:
                continue
            for next_match in _NEXT_VALUE_RE.finditer(source):
                value = next_match.group("array") or next_match.group("single") or ""
                pending.extend(
                    ("Next", int(ref_match.group(1)), depth + 1)
                    for ref_match in _OBJECT_REF_RE.finditer(value)
                )
        return resolved

    @staticmethod
    def _target_highlight_region(
        link: dict[str, Any],
        action_chain: list[dict[str, Any]],
        *,
        target_page_height: float | None = None,
    ) -> dict[str, Any] | None:
        """Prefer EPLAN /Next JavaScript highlight(), then standard PDF views."""
        for action in action_chain:
            source = action.get("source")
            if not isinstance(source, str):
                continue
            match = _HIGHLIGHT_JS_RE.search(source)
            if match is None:
                continue
            try:
                values = [float(value.strip()) for value in match.group(1).split(",")]
            except ValueError:
                continue
            if len(values) != 4:
                continue
            x0, y0, x1, y1 = values
            raw_bbox = bbox_to_dict(tuple(round(value, 6) for value in (x0, y0, x1, y1)))
            page_bbox = (
                bbox_to_dict(
                    tuple(
                        round(value, 6)
                        for value in (x0, target_page_height - y1, x1, target_page_height - y0)
                    )
                )
                if target_page_height is not None
                else raw_bbox
            )
            return {
                "type": "rectangle",
                "bbox": page_bbox,
                "raw_bbox": raw_bbox,
                "coordinate_space": (
                    "PyMuPDF target page (top-left origin)"
                    if target_page_height is not None
                    else "PDF user space (bottom-left origin)"
                ),
                "source": f"/{action['role']} {action['xref']} 0 R JavaScript highlight()",
            }

        viewrect = link.get("viewrect")
        if viewrect is not None:
            try:
                values = (
                    [float(value.strip()) for value in viewrect.split(",")]
                    if isinstance(viewrect, str)
                    else [float(value) for value in viewrect]
                )
            except (TypeError, ValueError):
                values = []
            if len(values) == 4:
                x, y, width, height = values
                return {
                    "type": "rectangle",
                    "bbox": bbox_to_dict((x, y, x + width, y + height)),
                    "source": "PyMuPDF viewrect",
                }
            return {"type": "rectangle", "raw": viewrect, "source": "PyMuPDF viewrect"}
        if link.get("view") == "Fit":
            return {"type": "whole_page", "source": "PDF /Fit"}
        return None

    def match_cross_page_texts(
        self,
        text_base: TextBase,
        *,
        min_iou: float = PARSER_CONFIG.hyperlinks.min_iou,
    ) -> list[dict[str, Any]]:
        """Return real cross-page links whose annotation overlaps xref-like text."""
        if not isinstance(text_base, TextBase):
            raise TypeError("text_base must be a TextBase")
        references: list[dict[str, Any]] = []
        for text_index, text in enumerate(text_base):
            parsed = self._parse_reference(text["text"])
            if parsed is None:
                continue
            candidates = []
            for link in self.links:
                target_page = int(link.get("target_page") or 0)
                if target_page == self.page_number or not 1 <= target_page <= self.page_count:
                    continue
                overlap = _iou(text["location"], link["bbox"])
                if overlap >= min_iou:
                    candidates.append((overlap, link))
            if not candidates:
                continue
            overlap, link = max(candidates, key=lambda item: item[0])
            label, zone = parsed
            references.append(
                {
                    "text": text["text"],
                    "text_index": int(text.get("index", text_index)),
                    "bbox": bbox_to_dict(coerce_bbox(text["location"])),
                    "source_page": self.page_number,
                    "target_page": int(link["target_page"]),
                    "target_label": label,
                    "target_zone": zone,
                    "kind": "xref",
                    "via": "pdf_link",
                    "annotation_index": link.get("index"),
                    "target_highlight_region": link.get("target_highlight_region"),
                    "action_chain": link.get("action_chain", []),
                    "overlap": overlap,
                }
            )
        return references

    def normalize_hyperlinks(self, text_base: TextBase) -> list[dict[str, Any]]:
        """Return every internal page hyperlink in the canonical compact format.

        Overlapping text indices are private assignment evidence and are removed
        when the hyperlink is attached to a component.
        """
        if not isinstance(text_base, TextBase):
            raise TypeError("text_base must be a TextBase")
        texts = list(text_base)
        text_boxes = [text["location"] for text in texts]
        text_grid = _BBoxGrid(text_boxes)
        hyperlinks: list[dict[str, Any]] = []
        for link in self.links:
            target_page = int(link.get("target_page") or 0)
            if not 1 <= target_page <= self.page_count:
                continue
            source_bbox = bbox_to_dict(coerce_bbox(link["bbox"]))
            overlapping_texts = []
            for position in text_grid.candidates(source_bbox):
                text = texts[position]
                overlap = _iou(source_bbox, text["location"])
                if overlap <= 0.0:
                    continue
                overlapping_texts.append(
                    {
                        "text_index": int(text.get("index", position)),
                        "coverage": _bbox_coverage(source_bbox, text["location"]),
                        "iou": overlap,
                    }
                )
            target_region = link.get("target_highlight_region")
            target_bbox = None
            if isinstance(target_region, dict) and target_region.get("bbox") is not None:
                target_bbox = bbox_to_dict(coerce_bbox(target_region["bbox"]))
            hyperlinks.append(
                {
                    "source_page": self.page_number,
                    "source_bbox": source_bbox,
                    "source_component": None,
                    "target_page": target_page,
                    "target_bbox": target_bbox,
                    "action_chain": list(link.get("action_chain", [])),
                    "_text_indices": [
                        item["text_index"] for item in overlapping_texts
                    ],
                    "_text_candidates": overlapping_texts,
                }
            )
        return hyperlinks

    @staticmethod
    def attach_to_components(
        hyperlinks: list[dict[str, Any]],
        components: list[dict[str, Any]],
        *,
        text_matches: list[dict[str, Any]] | None = None,
        max_distance: float = PARSER_CONFIG.hyperlinks.component_max_distance_pt,
    ) -> list[dict[str, Any]]:
        """Attach every hyperlink to a component when assignment evidence exists."""
        component_by_id = {int(component["id"]): component for component in components}
        component_boxes = [component["bbox"] for component in components]
        component_grid = _BBoxGrid(component_boxes)
        text_match_by_index = {
            int(text_index): match
            for match in text_matches or []
            if "component_id" in match
            for text_index in match.get("text_indices", [])
        }
        attached: list[dict[str, Any]] = []
        for hyperlink in hyperlinks:
            source_bbox = hyperlink["source_bbox"]
            overlapping_indices = [
                index
                for index in component_grid.candidates(source_bbox)
                if _iou(source_bbox, component_boxes[index]) > 0.0
            ]
            candidates: list[tuple[float, float, float, int, dict[str, Any]]] = []
            for index in overlapping_indices:
                candidate = components[index]
                candidates.append(
                    (
                        _bbox_coverage(source_bbox, component_boxes[index]),
                        _iou(source_bbox, component_boxes[index]),
                        -HyperlinkTools._bbox_area(component_boxes[index]),
                        -int(candidate["id"]),
                        candidate,
                    )
                )
            for text_candidate in hyperlink.get("_text_candidates", []):
                text_match = text_match_by_index.get(int(text_candidate["text_index"]))
                if text_match is None:
                    continue
                component_id = int(text_match["component_id"])
                candidate = component_by_id.get(component_id)
                if candidate is None:
                    continue
                candidates.append(
                    (
                        float(text_candidate["coverage"]),
                        float(text_candidate["iou"]),
                        -HyperlinkTools._bbox_area(candidate["bbox"]),
                        -component_id,
                        candidate,
                    )
                )
            component = max(candidates, default=None, key=lambda item: item[:-1])
            component = component[-1] if component is not None else None

            if component is None and components:
                nearby = [
                    components[index]
                    for index in component_grid.candidates(
                        source_bbox,
                        padding=max_distance,
                    )
                    if _bbox_gap(source_bbox, component_boxes[index]) <= max_distance
                ]
                if nearby:
                    component = min(
                        nearby,
                        key=lambda item: _bbox_gap(source_bbox, item["bbox"]),
                    )

            record = {
                key: value
                for key, value in hyperlink.items()
                if not key.startswith("_")
            }
            if component is not None:
                record["source_component"] = component["id"]
            attached.append(record)
        return attached

    @staticmethod
    def _bbox_area(value: Any) -> float:
        x0, y0, x1, y1 = coerce_bbox(value)
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    @staticmethod
    def _parse_reference(text: str) -> tuple[str, str] | None:
        match = _XREF_SLASH_RE.search(text)
        if match:
            return match.group(1), match.group(2).upper()
        match = _XREF_DOT_RE.search(text)
        if match:
            return match.group(1), (match.group(2) + match.group(3)).upper()
        return None
