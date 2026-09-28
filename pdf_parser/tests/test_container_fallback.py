"""Tests for releasing dashed boxes that frame several labelled devices."""

from __future__ import annotations

from shapely.geometry import box

from pdf_parser.pages_manager import TextBase
from pdf_parser.processors.diagram_extractor import _release_container_boxes


CONTENT_AREA = 1000.0 * 1000.0


def _frame(x0: float, y0: float, x1: float, y1: float, *, line_style: str | None = "dashed") -> dict:
    attributes = {"polygon": box(x0, y0, x1, y1), "content": []}
    if line_style is not None:
        attributes["line_style"] = line_style
    return {"id": 0, "type": "box", "shape": [], "bbox": (x0, y0, x1, y1), "attributes": attributes}


def _part(index: int, x: float, y: float, element_type: str = "box") -> dict:
    return {
        "id": index,
        "type": element_type,
        "shape": [],
        "bbox": (x, y, x + 20, y + 20),
        "attributes": {"content": []},
    }


def _labelled_parts(count: int, *, tag=lambda index: f"-K{index}") -> tuple[list[dict], TextBase]:
    parts = [_part(index + 1, 50 + 45 * index, 100) for index in range(count)]
    texts = TextBase([
        {"text": tag(index), "location": (50 + 45 * index, 124, 65 + 45 * index, 130)}
        for index in range(count)
    ])
    return parts, texts


def _release(elements: list[dict], texts: TextBase | None, content_area: float | None = CONTENT_AREA):
    groups: list[dict] = []
    _release_container_boxes(elements, groups, texts, content_area)
    return groups


def test_frame_around_many_labelled_devices_becomes_a_group() -> None:
    parts, texts = _labelled_parts(12, tag=lambda index: "3 -PS2" if index == 0 else f"-K{index}")
    elements = [_frame(0, 0, 600, 600), *parts]

    groups = _release(elements, texts)

    assert len(groups) == 1
    assert groups[0]["attributes"]["line_style"] == "dashed"
    assert groups[0]["attributes"]["released_container"] is True
    assert groups[0]["bbox"] == {"x0": 0.0, "y0": 0.0, "x1": 600.0, "y1": 600.0}
    assert [element["id"] for element in elements] == list(range(12))
    assert all(element["type"] == "box" and "line_style" not in element["attributes"] for element in elements)


def test_single_device_with_many_parts_stays_a_component() -> None:
    parts = [_part(index + 1, 20 + 18 * (index % 30), 100 + 40 * (index // 30)) for index in range(30)]
    texts = TextBase([
        {"text": "-PS1", "location": (10, 10, 40, 20)},
        *(
            {"text": f"X{index}", "location": (20 + 18 * index, 122, 30 + 18 * index, 128)}
            for index in range(30)
        ),
    ])
    elements = [_frame(0, 0, 600, 600), *parts]

    assert _release(elements, texts) == []
    assert len(elements) == 31


def test_small_frames_labelled_text_only_frames_and_solid_boxes_are_kept() -> None:
    parts, texts = _labelled_parts(12)
    cases = [
        [_frame(0, 0, 600, 150, line_style="dashed"), *parts],  # 9% of the drawing area
        [_frame(0, 0, 600, 600, line_style=None), *parts],      # solid box
        [_frame(0, 0, 600, 600)],                               # tags but no inner elements
    ]
    for elements in cases:
        count = len(elements)
        assert _release(elements, texts) == []
        assert len(elements) == count


def test_fallback_needs_texts_and_content_area() -> None:
    parts, texts = _labelled_parts(12)
    for text_base, area in ((None, CONTENT_AREA), (texts, None), (texts, 0.0)):
        elements = [_frame(0, 0, 600, 600), *parts]
        assert _release(elements, text_base, area) == []
