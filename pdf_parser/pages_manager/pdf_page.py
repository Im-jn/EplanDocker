"""Shared PDF page management and vector extraction helpers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import fitz

from pdf_parser.utils import bbox_to_dict, resolve_repo_relative

from .page_class import TextBase, VectorBase, coerce_bbox as _coerce_bbox


BBox = tuple[float, float, float, float]
Point = tuple[float, float]
PageData = dict[str, Any]


def _fmt_num(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _pt(point: Any) -> Point:
    return (float(point.x), float(point.y))


def bbox_from_points(points: Sequence[Point]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bboxes_intersect(left: Any, right: Any) -> bool:
    lx0, ly0, lx1, ly1 = _coerce_bbox(left)
    rx0, ry0, rx1, ry1 = _coerce_bbox(right)
    return min(lx1, rx1) >= max(lx0, rx0) and min(ly1, ry1) >= max(ly0, ry0)


def _code_line(p0: Point, p1: Point) -> str:
    return f"{_fmt_num(p0[0])} {_fmt_num(p0[1])} m\n{_fmt_num(p1[0])} {_fmt_num(p1[1])} l"


def _code_curve(p1: Point, p2: Point, p3: Point) -> str:
    return (
        f"{_fmt_num(p1[0])} {_fmt_num(p1[1])} "
        f"{_fmt_num(p2[0])} {_fmt_num(p2[1])} "
        f"{_fmt_num(p3[0])} {_fmt_num(p3[1])} c"
    )


def _code_rect(rect: fitz.Rect) -> str:
    return (
        f"{_fmt_num(rect.x0)} {_fmt_num(rect.y0)} "
        f"{_fmt_num(rect.width)} {_fmt_num(rect.height)} re"
    )


def _code_quad(points: Sequence[Point]) -> str:
    p0, p1, p2, p3 = points
    return (
        f"{_fmt_num(p0[0])} {_fmt_num(p0[1])} m\n"
        f"{_fmt_num(p1[0])} {_fmt_num(p1[1])} l\n"
        f"{_fmt_num(p2[0])} {_fmt_num(p2[1])} l\n"
        f"{_fmt_num(p3[0])} {_fmt_num(p3[1])} l\nh"
    )


def _cubic_value(a: float, b: float, c: float, d: float, t: float) -> float:
    u = 1.0 - t
    return (u**3 * a) + (3 * u**2 * t * b) + (3 * u * t**2 * c) + (t**3 * d)


def _cubic_axis_extrema(a: float, b: float, c: float, d: float) -> list[float]:
    # Derivative roots for a cubic Bezier axis.
    aa = -a + 3 * b - 3 * c + d
    bb = 2 * (a - 2 * b + c)
    cc = b - a
    roots: list[float] = []
    if abs(aa) < 1e-12:
        if abs(bb) >= 1e-12:
            roots.append(-cc / bb)
    else:
        disc = bb * bb - 4 * aa * cc
        if disc >= 0:
            sqrt_disc = math.sqrt(disc)
            roots.append((-bb + sqrt_disc) / (2 * aa))
            roots.append((-bb - sqrt_disc) / (2 * aa))
    return [t for t in roots if 0.0 < t < 1.0]


def cubic_bbox(points: Sequence[Point]) -> BBox:
    p0, p1, p2, p3 = points
    ts = {0.0, 1.0}
    ts.update(_cubic_axis_extrema(p0[0], p1[0], p2[0], p3[0]))
    ts.update(_cubic_axis_extrema(p0[1], p1[1], p2[1], p3[1]))
    sampled = [
        (
            _cubic_value(p0[0], p1[0], p2[0], p3[0], t),
            _cubic_value(p0[1], p1[1], p2[1], p3[1], t),
        )
        for t in ts
    ]
    return bbox_from_points(sampled)


class PdfPageManager:
    """Manage a PDF document, selected page, and basic page vector records."""

    def __init__(self, pdf_path: str | Path):
        self.pdf_path = resolve_repo_relative(str(pdf_path))
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")

        self.doc = fitz.open(self.pdf_path)
        self.page: fitz.Page | None = None
        self.page_number: int | None = None
        self.page_height_pt: float | None = None
        self.page_bbox: BBox | None = None
        self.page_vector: list[dict[str, Any]] = []
        self.page_texts: list[dict[str, Any]] = []
        self.page_links: list[dict[str, Any]] = []
        self.page_data: PageData | None = None

    @property
    def page_count(self) -> int:
        return int(self.doc.page_count)

    def close(self) -> None:
        self.doc.close()

    def __enter__(self) -> PdfPageManager:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def require_page(self) -> fitz.Page:
        if self.page is None:
            raise RuntimeError("Please select a page with goto() first")
        return self.page

    def goto(self, page_number: int) -> PageData:
        """Select a 1-based page and rebuild its reusable page data."""
        self._select_page(page_number)
        vectors = self.extract_page_vectors()
        texts = self.extract_page_texts()
        links = self.extract_page_links()
        self.page_data = self.build_page_data(vectors, texts, links)
        return self.page_data

    def goto_region(self, page_number: int, bbox: Any) -> PageData:
        """Select a page and extract only vectors and text intersecting ``bbox``."""
        self._select_page(page_number)
        vectors = self.extract_page_vectors(region_bbox=bbox)
        texts = self.extract_page_texts(region_bbox=bbox)
        self.page_links = []
        self.page_data = self.build_page_data(vectors, texts, [])
        return self.page_data

    def _select_page(self, page_number: int) -> None:
        if page_number < 1 or page_number > self.page_count:
            raise ValueError(f"page_number out of range: {page_number}")

        self.page_number = int(page_number)
        self.page = self.doc[self.page_number - 1]
        self.page_height_pt = float(self.page.rect.height)
        self.page_bbox = (0.0, 0.0, float(self.page.rect.width), float(self.page.rect.height))

    def extract_page_vectors(self, *, region_bbox: Any | None = None) -> list[dict[str, Any]]:
        """Extract drawing primitives as source-like code plus point sets and bboxes."""
        page = self.require_page()
        vectors: list[dict[str, Any]] = []

        for path_index, path in enumerate(page.get_drawings(extended=False)):
            if region_bbox is not None and path.get("rect") is not None:
                if not _bboxes_intersect(path["rect"], region_bbox):
                    continue
            path_meta = {
                "path_index": path_index,
                "seqno": path.get("seqno"),
                "path_type": path.get("type"),
                "stroke_width": path.get("width"),
                "color": path.get("color"),
                "fill": path.get("fill"),
                "dashes": path.get("dashes"),
            }
            for item_index, item in enumerate(path.get("items") or []):
                shape = self._shape_from_item(item, path_meta, item_index)
                if shape is None:
                    continue
                if region_bbox is not None:
                    if not _bboxes_intersect(bbox_from_points(shape["points"]), region_bbox):
                        continue
                vectors.append(shape)

        self.page_vector = vectors
        return self.page_vector

    def extract_page_texts(self, *, region_bbox: Any | None = None) -> list[dict[str, Any]]:
        """Extract text spans with PyMuPDF page-space bboxes."""
        page = self.require_page()
        texts: list[dict[str, Any]] = []
        clip = fitz.Rect(_coerce_bbox(region_bbox)) if region_bbox is not None else None
        text_dict = page.get_text(
            "dict",
            flags=fitz.TEXT_PRESERVE_WHITESPACE,
            clip=clip,
        )

        for block_index, block in enumerate(text_dict.get("blocks") or []):
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block.get("lines") or []):
                direction = tuple(float(value) for value in line.get("dir", (1.0, 0.0)))
                for span_index, span in enumerate(line.get("spans") or []):
                    content = str(span.get("text") or "").strip()
                    bbox_raw = span.get("bbox")
                    if not content or not bbox_raw or len(bbox_raw) < 4:
                        continue
                    bbox_value = _coerce_bbox(bbox_raw[:4])
                    if region_bbox is not None and not _bboxes_intersect(bbox_value, region_bbox):
                        continue
                    texts.append(
                        {
                            "index": len(texts),
                            "text": content,
                            "bbox": bbox_to_dict(bbox_value),
                            "font": span.get("font"),
                            "font_size": float(span.get("size") or 0.0),
                            "block_index": block_index,
                            "line_index": line_index,
                            "span_index": span_index,
                            "line_direction": direction,
                        }
                    )

        self.page_texts = texts
        return self.page_texts

    def extract_page_links(self) -> list[dict[str, Any]]:
        """Extract internal PDF link annotations using public 1-based page numbers."""
        from pdf_parser.tools.hyperlink_tools import HyperlinkTools

        page = self.require_page()
        links: list[dict[str, Any]] = []
        for index, link in enumerate(page.get_links()):
            source_bbox = link.get("from")
            target_page = self._public_link_page(link.get("page"))
            if source_bbox is None or target_page is None:
                continue
            inspected = HyperlinkTools.inspect_pdf_link(self.doc, link)
            links.append(
                {
                    "index": index,
                    "bbox": bbox_to_dict(_coerce_bbox(source_bbox)),
                    "source_page": int(self.page_number or page.number + 1),
                    "target_page": target_page,
                    "kind": int(link.get("kind") or 0),
                    "target_highlight_region": inspected["target_highlight_region"],
                    "annotation_xref": inspected["annotation_xref"],
                    "annotation_source": inspected["annotation_source"],
                    "action_chain": inspected["action_chain"],
                    "properties": {
                        key: str(value) if isinstance(value, (fitz.Point, fitz.Rect)) else value
                        for key, value in link.items()
                    },
                }
            )
        self.page_links = links
        return self.page_links

    @staticmethod
    def _public_link_page(value: Any) -> int | None:
        """Normalize PyMuPDF GoTo integers and named/Fit page strings to 1-based."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value + 1 if value >= 0 else None
        try:
            page_number = int(str(value))
        except (TypeError, ValueError):
            return None
        return page_number if page_number >= 1 else None

    def _shape_from_item(
        self,
        item: tuple[Any, ...],
        path_meta: dict[str, Any],
        item_index: int,
    ) -> dict[str, Any] | None:
        op = item[0]
        try:
            if op == "l":
                _, p0_raw, p1_raw = item
                p0 = _pt(p0_raw)
                p1 = _pt(p1_raw)
                return {
                    "type": "line",
                    "code": _code_line(p0, p1),
                    "points": [p0, p1],
                    "path_meta": {**path_meta, "item_index": item_index},
                }
            if op == "c":
                _, p0_raw, p1_raw, p2_raw, p3_raw = item
                p0 = _pt(p0_raw)
                p1 = _pt(p1_raw)
                p2 = _pt(p2_raw)
                p3 = _pt(p3_raw)
                return {
                    "type": "curve",
                    "code": _code_curve(p1, p2, p3),
                    "points": [p0, p1, p2, p3],
                    "path_meta": {**path_meta, "item_index": item_index},
                }
            if op == "re":
                rect_raw = item[1]
                rect = rect_raw if isinstance(rect_raw, fitz.Rect) else fitz.Rect(rect_raw)
                points = [
                    (float(rect.x0), float(rect.y0)),
                    (float(rect.x1), float(rect.y0)),
                    (float(rect.x1), float(rect.y1)),
                    (float(rect.x0), float(rect.y1)),
                ]
                return {
                    "type": "rect",
                    "code": _code_rect(rect),
                    "points": points,
                    "path_meta": {**path_meta, "item_index": item_index},
                }
            if op == "qu":
                _, quad = item
                points = [_pt(quad.ul), _pt(quad.ur), _pt(quad.lr), _pt(quad.ll)]
                return {
                    "type": "quad",
                    "code": _code_quad(points),
                    "points": points,
                    "path_meta": {**path_meta, "item_index": item_index},
                }
        except Exception:
            return None
        return None

    def build_page_data(
        self,
        vectors: list[dict[str, Any]] | None = None,
        texts: list[dict[str, Any]] | None = None,
        links: list[dict[str, Any]] | None = None,
    ) -> PageData:
        """Build the current page data package consumed by parser helpers."""
        if self.page_number is None or self.page_height_pt is None or self.page_bbox is None:
            raise RuntimeError("Please select a page with goto() first")
        if vectors is None:
            vectors = self.page_vector
        if texts is None:
            texts = self.page_texts
        if links is None:
            links = self.page_links

        vector_base = VectorBase(vectors=vectors)
        text_base = TextBase(texts)

        return {
            "pdf_path": str(self.pdf_path),
            "page_number": self.page_number,
            "page_count": self.page_count,
            "page_height_pt": self.page_height_pt,
            "page_bbox": self.page_bbox,
            "vectors": vector_base,
            "texts": text_base,
            "links": [dict(link) for link in links],
        }
