"""Render VectorBase and TextBase content into a PNG image."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

from pdf_parser.pages_manager import TextBase, VectorBase
from pdf_parser.pages_manager.page_class import coerce_bbox


def render_vector_text_png(
    vector_base: VectorBase,
    text_base: TextBase | None = None,
    output_dir: str | Path = "storage/output/images",
    filename: str = "vector_text.png",
) -> Path:
    """Render vectors and optional text after aligning their minimum bbox to zero.

    The image is written to ``output_dir`` (created if needed) and the written
    file path is returned.
    """
    png_bytes = render_vector_text_png_bytes(vector_base, text_base)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    output_file = output_path / filename
    output_file.write_bytes(png_bytes)
    return output_file


def render_vector_text_png_bytes(
    vector_base: VectorBase,
    text_base: TextBase | None = None,
) -> bytes:
    """Render vectors and optional text directly to PNG bytes.

    This form is intended for transient consumers such as multimodal model
    requests, where writing every rendered entity to persistent storage would
    only waste disk space.
    """
    if not isinstance(vector_base, VectorBase):
        raise TypeError("vector_base must be a VectorBase")
    if text_base is not None and not isinstance(text_base, TextBase):
        raise TypeError("text_base must be a TextBase")
    if not vector_base.vectors and not text_base:
        raise ValueError("vector_base and text_base must not both be empty")

    padding = 8.0
    bboxes = [coerce_bbox(vector.bbox) for vector in vector_base.vectors]
    if text_base is not None:
        bboxes.extend(coerce_bbox(text["location"]) for text in text_base)

    min_x = min(bbox[0] for bbox in bboxes)
    min_y = min(bbox[1] for bbox in bboxes)
    max_x = max(bbox[2] for bbox in bboxes)
    max_y = max(bbox[3] for bbox in bboxes)
    width = max(max_x - min_x + padding * 2.0, 1.0)
    height = max(max_y - min_y + padding * 2.0, 1.0)

    def point(raw: Any) -> fitz.Point:
        return fitz.Point(float(raw[0]) - min_x + padding, float(raw[1]) - min_y + padding)

    def rect(raw_bbox: Any) -> fitz.Rect:
        x0, y0, x1, y1 = coerce_bbox(raw_bbox)
        return fitz.Rect(
            x0 - min_x + padding,
            y0 - min_y + padding,
            x1 - min_x + padding,
            y1 - min_y + padding,
        )

    doc = fitz.open()
    try:
        page = doc.new_page(width=width, height=height)
        shape = page.new_shape()

        for vector in vector_base.vectors:
            points = vector.points
            if vector.type == "line" and len(points) >= 2:
                shape.draw_line(point(points[0]), point(points[1]))
            elif vector.type == "curve" and len(points) == 4:
                shape.draw_bezier(
                    point(points[0]),
                    point(points[1]),
                    point(points[2]),
                    point(points[3]),
                )
            elif vector.type in {"rect", "quad"} and len(points) >= 4:
                shifted = [point(raw_point) for raw_point in points[:4]]
                for left, right in zip(shifted, [*shifted[1:], shifted[0]]):
                    shape.draw_line(left, right)
            elif len(points) >= 2:
                shifted = [point(raw_point) for raw_point in points]
                for left, right in zip(shifted, shifted[1:]):
                    shape.draw_line(left, right)
            else:
                shape.draw_rect(rect(vector.bbox))

        shape.finish(width=0.6, color=(0.0, 0.0, 0.0), fill=None, closePath=False)
        shape.commit()

        for text in text_base or ():
            content = str(text.get("text", ""))
            if not content:
                continue
            text_rect = rect(text["location"])
            if text_rect.is_empty:
                continue
            font_size = max(float(text.get("font_size") or 8.0), 4.0)
            baseline = fitz.Point(text_rect.x0, text_rect.y1 - font_size * 0.2)
            page.insert_text(
                baseline,
                content,
                fontsize=font_size,
                fontname="helv",
                color=(0.0, 0.2, 0.8),
            )

        return page.get_pixmap(alpha=False).tobytes("png")
    finally:
        doc.close()
