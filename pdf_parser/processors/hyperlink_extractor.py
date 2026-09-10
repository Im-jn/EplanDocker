"""Extract PDF hyperlinks and attach their source and target components."""

from __future__ import annotations

import warnings
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import TextBase
from pdf_parser.tools.hyperlink_tools import HyperlinkTools
from pdf_parser.utils import coerce_bbox


def extract_hyperlinks(
    text_base: TextBase,
    links: list[dict[str, Any]],
    *,
    page_number: int,
    page_count: int,
) -> dict[str, Any]:
    """Normalize every internal page hyperlink, retaining overlapping text indices."""
    tools = HyperlinkTools(links, page_number=page_number, page_count=page_count)
    hyperlinks = tools.normalize_hyperlinks(text_base)
    return {
        "page_number": int(page_number),
        "hyperlinks": hyperlinks,
        "text_indices": {
            int(text_index)
            for hyperlink in hyperlinks
            for text_index in hyperlink.get("_text_indices", [])
        },
    }


def attach_hyperlinks(
    hyperlinks: list[dict[str, Any]],
    components: list[dict[str, Any]],
    *,
    text_matches: list[dict[str, Any]] | None = None,
    max_distance: float = PARSER_CONFIG.hyperlinks.component_max_distance_pt,
) -> list[dict[str, Any]]:
    """Associate hyperlinks through matched text first, then component geometry."""
    return HyperlinkTools.attach_to_components(
        hyperlinks,
        components,
        text_matches=text_matches,
        max_distance=max_distance,
    )


def split_transfers(
    hyperlinks: list[dict[str, Any]],
    components: list[dict[str, Any]],
    elements: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Separate links owned by arrow elements and remove storage-only evidence."""
    arrow_element_ids = {
        element.get("id")
        for element in elements
        if element.get("type") == "arrow"
    }
    arrow_component_ids = {
        component.get("id")
        for component in components
        if component.get("type") == "arrow"
        or arrow_element_ids.intersection(component.get("element_ids", []))
    }
    result: dict[str, list[dict[str, Any]]] = {
        "hyperlinks": [],
        "transfers": [],
    }
    for hyperlink in hyperlinks:
        compact = {
            key: value
            for key, value in hyperlink.items()
            if key != "action_chain"
        }
        collection = (
            "transfers"
            if compact.get("source_component") in arrow_component_ids
            else "hyperlinks"
        )
        result[collection].append(compact)
    return result


def attach_hyperlink_targets(
    hyperlinks: list[dict[str, Any]],
    components_by_page: Mapping[int, Sequence[dict[str, Any]]],
    *,
    max_distance: float = PARSER_CONFIG.hyperlinks.component_max_distance_pt,
) -> list[dict[str, Any]]:
    """Attach target-page components while retaining each precise target bbox."""
    if max_distance < 0:
        raise ValueError("max_distance must be non-negative")

    resolved = [
        {
            key: value
            for key, value in hyperlink.items()
            if key != "target_component"
        }
        for hyperlink in hyperlinks
    ]
    links_by_target_page: dict[int, list[int]] = defaultdict(list)
    for index, hyperlink in enumerate(resolved):
        target_page = _positive_int(hyperlink.get("target_page"))
        if target_page is not None:
            links_by_target_page[target_page].append(index)

    normalized_components = {
        int(page_number): list(components)
        for page_number, components in components_by_page.items()
    }
    for target_page, link_indices in links_by_target_page.items():
        components = normalized_components.get(target_page, [])
        if not components:
            continue
        for link_index in link_indices:
            target_bbox = resolved[link_index].get("target_bbox")
            if target_bbox is None:
                continue
            component = _match_target_component(
                target_bbox,
                components,
                max_distance=max_distance,
            )
            if component is not None:
                resolved[link_index]["target_component"] = int(component["id"])
    return resolved


def attach_transfer_targets(
    transfers: list[dict[str, Any]],
    components_by_page: Mapping[int, Sequence[dict[str, Any]]],
    elements_by_page: Mapping[int, Sequence[dict[str, Any]]],
    *,
    max_distance: float = PARSER_CONFIG.hyperlinks.component_max_distance_pt,
) -> list[dict[str, Any]]:
    """Attach transfers only to arrow components and warn on invalid targets."""
    if max_distance < 0:
        raise ValueError("max_distance must be non-negative")

    normalized_components = {
        int(page_number): list(components)
        for page_number, components in components_by_page.items()
    }
    normalized_elements = {
        int(page_number): list(elements)
        for page_number, elements in elements_by_page.items()
    }
    resolved: list[dict[str, Any]] = []
    for transfer in transfers:
        record = {
            key: value
            for key, value in transfer.items()
            if key != "target_component"
        }
        target_page = _positive_int(record.get("target_page"))
        target_bbox = record.get("target_bbox")
        components = normalized_components.get(target_page or -1, [])
        elements = normalized_elements.get(target_page or -1, [])
        arrow_components = _components_containing_element_type(
            components,
            elements,
            "arrow",
        )
        target_component = (
            _match_target_component(
                target_bbox,
                arrow_components,
                max_distance=max_distance,
            )
            if target_bbox is not None and arrow_components
            else None
        )
        if target_component is not None:
            record["target_component"] = int(target_component["id"])
            resolved.append(record)
            continue

        generic_component = (
            _match_target_component(
                target_bbox,
                components,
                max_distance=max_distance,
            )
            if target_bbox is not None and components
            else None
        )
        _warn_invalid_transfer_target(
            record,
            generic_component=generic_component,
            target_page_has_components=bool(components),
            target_page_has_arrows=bool(arrow_components),
        )
        resolved.append(record)
    return resolved


def _components_containing_element_type(
    components: Sequence[dict[str, Any]],
    elements: Sequence[dict[str, Any]],
    element_type: str,
) -> list[dict[str, Any]]:
    matching_element_ids = {
        element.get("id")
        for element in elements
        if element.get("type") == element_type
    }
    return [
        component
        for component in components
        if matching_element_ids.intersection(
            component.get("elements", component.get("element_ids", []))
        )
    ]


def _warn_invalid_transfer_target(
    transfer: dict[str, Any],
    *,
    generic_component: dict[str, Any] | None,
    target_page_has_components: bool,
    target_page_has_arrows: bool,
) -> None:
    if generic_component is not None:
        reason = f"target region matched non-arrow component {generic_component.get('id')}"
    elif not target_page_has_components:
        reason = "target page has no diagram components"
    elif not target_page_has_arrows:
        reason = "target page has no arrow components"
    elif transfer.get("target_bbox") is None:
        reason = "target bbox is unavailable"
    else:
        reason = "target region did not match an arrow component"
    warnings.warn(
        "Invalid transfer target: "
        f"source_page={transfer.get('source_page')}, "
        f"source_component={transfer.get('source_component')}, "
        f"target_page={transfer.get('target_page')}, "
        f"target_bbox={transfer.get('target_bbox')}; {reason}",
        RuntimeWarning,
        stacklevel=3,
    )


def _match_target_component(
    target_bbox: Any,
    components: Sequence[dict[str, Any]],
    *,
    max_distance: float,
) -> dict[str, Any] | None:
    try:
        target = coerce_bbox(target_bbox)
    except (KeyError, TypeError, ValueError):
        return None

    overlapping = []
    target_area = _bbox_area(target)
    for component in components:
        component_bbox = component.get("bbox")
        if component_bbox is None:
            continue
        try:
            overlap = _bbox_overlap_area(target, component_bbox)
            component_area = _bbox_area(component_bbox)
            component_id = int(component["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if overlap <= 0.0:
            continue
        overlapping.append(
            (
                overlap / component_area if component_area > 0.0 else 0.0,
                overlap / target_area if target_area > 0.0 else 0.0,
                -component_area,
                -component_id,
                component,
            )
        )
    if overlapping:
        return max(overlapping, key=lambda item: item[:-1])[-1]

    nearby = []
    for component in components:
        component_bbox = component.get("bbox")
        if component_bbox is None:
            continue
        try:
            gap = _bbox_gap(target, component_bbox)
            area = _bbox_area(component_bbox)
            component_id = int(component["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if gap <= max_distance:
            nearby.append((gap, area, component_id, component))
    if not nearby:
        return None
    return min(nearby, key=lambda item: item[:-1])[-1]


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _bbox_overlap_area(left: Any, right: Any) -> float:
    ax0, ay0, ax1, ay1 = coerce_bbox(left)
    bx0, by0, bx1, by1 = coerce_bbox(right)
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0,
        min(ay1, by1) - max(ay0, by0),
    )


def _bbox_area(value: Any) -> float:
    x0, y0, x1, y1 = coerce_bbox(value)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _bbox_gap(left: Any, right: Any) -> float:
    ax0, ay0, ax1, ay1 = coerce_bbox(left)
    bx0, by0, bx1, by1 = coerce_bbox(right)
    dx = max(ax0 - bx1, bx0 - ax1, 0.0)
    dy = max(ay0 - by1, by0 - ay1, 0.0)
    return (dx * dx + dy * dy) ** 0.5
