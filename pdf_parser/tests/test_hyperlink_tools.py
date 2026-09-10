"""Tests for normalized PDF hyperlink and cross-reference matching."""

from __future__ import annotations

from pdf_parser.pages_manager import TextBase
from pdf_parser.pages_manager import PdfPageManager
from pdf_parser.tools.hyperlink_tools import HyperlinkTools


class _FakeDocument:
    def __init__(self, objects):
        self.objects = objects

    def xref_object(self, xref, compressed=False):
        assert compressed is False
        return self.objects[xref]


def test_keeps_cross_page_text_parser_available_as_fallback():
    texts = TextBase([
        {"index": 7, "text": "N04/2C", "location": (10, 10, 30, 20)},
        {"index": 8, "text": "M06/3D", "location": (40, 10, 60, 20)},
    ])
    links = [
        {"index": 1, "bbox": (10, 10, 30, 20), "source_page": 2, "target_page": 4},
    ]
    tools = HyperlinkTools(links, page_number=2, page_count=5)

    references = tools.match_cross_page_texts(texts)
    assert len(references) == 1
    assert references[0]["text_index"] == 7
    assert references[0]["target_page"] == 4
    assert references[0]["target_label"] == "N04"
    assert references[0]["target_zone"] == "2C"


def test_rejects_self_links_and_out_of_range_targets():
    texts = TextBase([{"text": "12.K:4", "location": (10, 10, 30, 20)}])
    links = [
        {"index": 1, "bbox": (10, 10, 30, 20), "target_page": 2},
        {"index": 2, "bbox": (10, 10, 30, 20), "target_page": 8},
    ]

    references = HyperlinkTools(links, page_number=2, page_count=5).match_cross_page_texts(texts)

    assert references == []


def test_page_manager_normalizes_integer_and_named_link_pages():
    assert PdfPageManager._public_link_page(0) == 1
    assert PdfPageManager._public_link_page(126) == 127
    assert PdfPageManager._public_link_page("127") == 127
    assert PdfPageManager._public_link_page(-1) is None


def test_inspects_recursive_next_actions_and_extracts_javascript_highlight():
    doc = _FakeDocument({
        10: "<< /Subtype /Link /A 20 0 R >>",
        20: "<< /S /GoTo /D [ 99 0 R /Fit ] /Next [ 30 0 R 40 0 R ] >>",
        30: r"<< /S /JavaScript /JS (highlight\(0, [10, 20, 30, 45]\);) >>",
        40: "<< /S /Named /Next 20 0 R >>",
    })

    result = HyperlinkTools.inspect_pdf_link(
        doc,
        {"xref": 10, "page": "4", "view": "Fit"},
    )

    assert [item["xref"] for item in result["action_chain"]] == [20, 30, 40]
    assert result["target_highlight_region"] == {
        "type": "rectangle",
        "bbox": {
            "x0": 10.0,
            "y0": 20.0,
            "x1": 30.0,
            "y1": 45.0,
        },
        "raw_bbox": {
            "x0": 10.0,
            "y0": 20.0,
            "x1": 30.0,
            "y1": 45.0,
        },
        "coordinate_space": "PDF user space (bottom-left origin)",
        "source": "/Next 30 0 R JavaScript highlight()",
    }


def test_reference_keeps_normalized_target_highlight_region():
    region = {
        "type": "rectangle",
        "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4, "width": 2, "height": 2},
    }
    texts = TextBase([{"index": 1, "text": "N04/2C", "location": (10, 10, 30, 20)}])
    links = [{
        "index": 2,
        "bbox": (10, 10, 30, 20),
        "target_page": 4,
        "target_highlight_region": region,
        "action_chain": [{"role": "Next", "xref": 30}],
    }]

    reference = HyperlinkTools(links, page_number=2, page_count=5).match_cross_page_texts(texts)[0]

    assert reference["target_highlight_region"] == region
    assert reference["action_chain"] == [{"role": "Next", "xref": 30}]


def test_normalizes_highlight_bbox_to_compact_hyperlink_shape():
    texts = TextBase([])
    links = [{
        "bbox": (10, 10, 30, 20),
        "target_page": 2,
        "target_highlight_region": {
            "type": "rectangle",
            "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
        },
        "action_chain": [{"role": "Next", "xref": 30}],
    }]

    hyperlink = HyperlinkTools(
        links, page_number=2, page_count=5,
    ).normalize_hyperlinks(texts)[0]

    assert hyperlink["source_page"] == 2
    assert hyperlink["target_page"] == 2
    assert hyperlink["target_bbox"] == {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}
    assert hyperlink["action_chain"] == [{"role": "Next", "xref": 30}]
