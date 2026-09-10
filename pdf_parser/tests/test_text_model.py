"""Tests for the Text model and TextMatcher region queries."""

from __future__ import annotations

from shapely.geometry import Polygon

from pdf_parser.pages_manager import Text, TextBase
from pdf_parser.tools.text_matcher import TextMatcher


def _text(content: str, bbox: tuple[float, float, float, float]) -> dict[str, object]:
    return {"text": content, "location": bbox}


def test_text_normalizes_single_item_properties_and_preserves_legacy_output():
    text = Text.from_record({
        "index": 4,
        "text": {"content": " Motor "},
        "location": (1, 2, 5, 6),
        "font_size": 7,
        "block_index": 3,
    })

    assert text.text == "Motor"
    assert text.bbox == (1.0, 2.0, 5.0, 6.0)
    assert text.location == text.bbox
    assert text.center == (3.0, 4.0)
    assert text.source_index == 4
    assert text.metadata == {"block_index": 3}
    assert text["block_index"] == 3
    assert text.to_dict() == {
        "block_index": 3,
        "index": 4,
        "text": "Motor",
        "location": (1.0, 2.0, 5.0, 6.0),
        "font_size": 7.0,
    }


def test_text_base_owns_text_items_and_accepts_text_or_dict_inputs():
    original = Text("left", (1, 1, 3, 3), source_index=2)
    text_base = TextBase([original, _text("right", (8, 1, 10, 3))])

    assert all(isinstance(text, Text) for text in text_base)
    assert text_base[0] is not original
    assert text_base[0].source_index == 2
    assert text_base.to_list()[1]["text"] == "right"


def test_text_in_box_returns_texts_with_centers_inside_bbox():
    text_base = TextBase([
        _text("inside", (1.0, 1.0, 3.0, 3.0)),
        _text("outside", (6.0, 6.0, 8.0, 8.0)),
    ])

    result = TextMatcher(text_base).text_in_box((0, 0, 5, 5))
    dict_result = TextMatcher(text_base).text_in_box(
        {"x0": 0, "y0": 0, "x1": 5, "y1": 5}
    )

    assert isinstance(result, TextBase)
    assert isinstance(dict_result, TextBase)
    assert [text["text"] for text in result.to_list()] == ["inside"]
    assert [text["text"] for text in dict_result.to_list()] == ["inside"]


def test_text_in_polygon_accepts_shapely_polygon_and_points_record():
    text_base = TextBase([
        _text("left", (1.0, 1.0, 3.0, 3.0)),
        _text("right", (8.0, 1.0, 10.0, 3.0)),
    ])
    polygon = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])

    result = TextMatcher(text_base).text_in_polygon(polygon)
    points_result = TextMatcher(text_base).text_in_polygon(
        {"points": [(0, 0), (5, 0), (5, 5), (0, 5)]}
    )

    assert isinstance(result, TextBase)
    assert isinstance(points_result, TextBase)
    assert [text["text"] for text in result.to_list()] == ["left"]
    assert [text["text"] for text in points_result.to_list()] == ["left"]
