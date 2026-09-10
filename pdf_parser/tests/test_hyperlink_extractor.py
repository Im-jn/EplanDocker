"""Tests for the page-level hyperlink extractor boundary."""

from __future__ import annotations

import warnings

from pdf_parser.processors.hyperlink_extractor import (
    attach_hyperlink_targets,
    attach_hyperlinks,
    attach_transfer_targets,
    extract_hyperlinks,
    split_transfers,
)
from pdf_parser.pages_manager import TextBase


def _hyperlink(*, source_bbox=(10, 10, 30, 20), text_indices=(), target_page=4):
    return {
        "source_page": 2,
        "source_bbox": source_bbox,
        "source_component": None,
        "target_page": target_page,
        "target_bbox": None,
        "action_chain": [],
        "_text_indices": list(text_indices),
        "_text_candidates": [
            {"text_index": index, "coverage": 1.0, "iou": 1.0}
            for index in text_indices
        ],
    }


def test_extracts_all_internal_hyperlinks_without_requiring_xref_text():
    texts = TextBase([
        {"index": 9, "text": "ordinary label", "location": (10, 10, 30, 20)},
    ])
    links = [
        {"bbox": (10, 10, 30, 20), "source_page": 2, "target_page": 2},
        {"bbox": (40, 10, 60, 20), "source_page": 2, "target_page": 4},
    ]

    result = extract_hyperlinks(texts, links, page_number=2, page_count=5)

    assert result["page_number"] == 2
    assert result["text_indices"] == {9}
    assert [item["target_page"] for item in result["hyperlinks"]] == [2, 4]


def test_prefers_component_already_matched_to_overlapping_text():
    hyperlinks = [_hyperlink(source_bbox=(90, 0, 100, 10), text_indices=(9,))]
    components = [
        {"id": 0, "bbox": (0, 0, 10, 10), "attributes": {}},
        {"id": 1, "bbox": (90, 0, 100, 10), "attributes": {}},
    ]
    text_matches = [{
        "text": "ordinary label",
        "text_indices": [9],
        "component_id": 0,
        "role": "description",
    }]

    result = attach_hyperlinks(hyperlinks, components, text_matches=text_matches)

    assert result[0]["source_component"] == 0
    assert set(result[0]) == {
        "source_page", "source_bbox", "source_component", "target_page",
        "target_bbox", "action_chain",
    }
    assert result[0]["source_bbox"] == (90, 0, 100, 10)
    assert "hyperlinks" not in components[0]["attributes"]


def test_directly_attaches_hyperlink_to_smallest_overlapping_component():
    hyperlinks = [_hyperlink(source_bbox=(5, 5, 10, 10))]
    components = [
        {"id": 3, "bbox": (0, 0, 20, 20), "attributes": {}},
        {"id": 4, "bbox": (4, 4, 11, 11), "attributes": {}},
    ]

    result = attach_hyperlinks(hyperlinks, components)

    assert result[0]["source_component"] == 4


def test_keeps_unresolved_hyperlink_instead_of_dropping_it():
    hyperlinks = [_hyperlink(source_bbox=(100, 100, 110, 110))]
    components = [{"id": 3, "bbox": (0, 0, 10, 10), "attributes": {}}]

    result = attach_hyperlinks(hyperlinks, components, max_distance=20)

    assert result[0]["source_component"] is None


def test_splits_arrow_component_links_as_transfers_and_removes_action_chain():
    links = [
        _hyperlink(source_bbox=(0, 0, 10, 10)),
        _hyperlink(source_bbox=(20, 0, 30, 10)),
        _hyperlink(source_bbox=(40, 0, 50, 10)),
    ]
    links[0]["source_component"] = 3
    links[1]["source_component"] = 4
    links[2]["source_component"] = None
    links[0]["action_chain"] = [{"xref": 1}]
    links[1]["action_chain"] = [{"xref": 2}]
    components = [
        {"id": 3, "type": "component", "element_ids": [7]},
        {"id": 4, "type": "component", "element_ids": [8]},
    ]
    elements = [
        {"id": 7, "type": "arrow"},
        {"id": 8, "type": "symbol"},
    ]

    result = split_transfers(links, components, elements)

    assert [link["source_component"] for link in result["transfers"]] == [3]
    assert [link["source_component"] for link in result["hyperlinks"]] == [4, None]
    assert all(
        "action_chain" not in link
        for collection in result.values()
        for link in collection
    )


def test_endpoint_text_match_falls_back_to_component_geometry():
    hyperlinks = [_hyperlink(source_bbox=(11, 0, 20, 10), text_indices=(9,))]
    components = [{"id": 3, "bbox": (0, 0, 10, 10), "attributes": {}}]
    endpoint_matches = [{
        "text": "N04/2C",
        "text_indices": [9],
        "endpoint_id": 0,
        "role": "description",
    }]

    result = attach_hyperlinks(hyperlinks, components, text_matches=endpoint_matches)

    assert result[0]["source_component"] == 3


def test_component_and_matched_text_compete_by_bbox_coverage():
    texts = TextBase([
        {
            "index": 258,
            "text": "-CT2",
            "location": (372.365, 303.877, 389.120, 311.878),
        },
    ])
    links = [{
        "bbox": (362.873, 281.547, 387.316, 305.126),
        "target_page": 127,
    }]
    hyperlink = extract_hyperlinks(
        texts,
        links,
        page_number=12,
        page_count=300,
    )["hyperlinks"]
    components = [
        {"id": 6, "bbox": (360.773, 283.802, 381.389, 296.686)},
        {"id": 7, "bbox": (402.004, 304.417, 422.620, 317.302)},
    ]
    text_matches = [{
        "text": "-CT2",
        "text_indices": [258],
        "component_id": 7,
    }]

    result = attach_hyperlinks(
        hyperlink,
        components,
        text_matches=text_matches,
    )

    assert result[0]["source_component"] == 6


def test_attaches_target_components_by_target_page_and_keeps_target_bbox():
    target_bbox = {"x0": 5.0, "y0": 5.0, "x1": 10.0, "y1": 10.0}
    hyperlinks = [
        {
            "source_page": 2,
            "source_component": 6,
            "target_page": 4,
            "target_bbox": target_bbox,
            "action_chain": [],
        },
        {
            "source_page": 3,
            "source_component": 1,
            "target_page": 8,
            "target_bbox": {"x0": 20.0, "y0": 20.0, "x1": 25.0, "y1": 25.0},
            "action_chain": [],
        },
        {
            "source_page": 3,
            "source_component": 2,
            "target_page": 9,
            "target_bbox": {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0},
            "action_chain": [],
        },
    ]
    components_by_page = {
        4: [
            {"id": 3, "bbox": (0, 0, 20, 20)},
            {"id": 4, "bbox": (4, 4, 11, 11)},
        ],
        8: [{"id": 9, "bbox": (100, 100, 110, 110)}],
    }

    result = attach_hyperlink_targets(
        hyperlinks,
        components_by_page,
        max_distance=10,
    )

    assert result[0]["target_component"] == 4
    assert result[0]["target_bbox"] == target_bbox
    assert "target_component" not in result[1]
    assert "target_component" not in result[2]


def test_transfer_targets_only_arrow_components_and_warns_otherwise():
    transfers = [
        {
            "source_page": 2,
            "source_component": 6,
            "target_page": 4,
            "target_bbox": {"x0": 4.0, "y0": 4.0, "x1": 11.0, "y1": 11.0},
        },
        {
            "source_page": 2,
            "source_component": 7,
            "target_page": 4,
            "target_bbox": {"x0": 40.0, "y0": 40.0, "x1": 50.0, "y1": 50.0},
        },
        {
            "source_page": 2,
            "source_component": 8,
            "target_page": 9,
            "target_bbox": {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0},
        },
    ]
    components_by_page = {
        4: [
            {"id": 3, "bbox": (0, 0, 20, 20), "elements": [10]},
            {"id": 4, "bbox": (4, 4, 11, 11), "elements": [11]},
            {"id": 5, "bbox": (40, 40, 50, 50), "elements": [10]},
        ],
    }
    elements_by_page = {
        4: [
            {"id": 10, "type": "symbol"},
            {"id": 11, "type": "arrow"},
        ],
    }

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = attach_transfer_targets(
            transfers,
            components_by_page,
            elements_by_page,
            max_distance=5,
        )

    assert result[0]["target_component"] == 4
    assert "target_component" not in result[1]
    assert "target_component" not in result[2]
    assert len(caught) == 2
    assert "non-arrow component 5" in str(caught[0].message)
    assert "target page has no diagram components" in str(caught[1].message)
