from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import fitz


ROUND_DIGITS = 3


def round_float(value: float, digits: int = ROUND_DIGITS) -> float:
    return round(float(value), digits)


def page_height_pt(page: fitz.Page) -> float:
    """MediaBox height in points (PyMuPDF uses top-left origin; height matches PDF vertical extent)."""
    return float(page.rect.height)


def point_to_pdf(x: float, y: float, height: float) -> tuple[float, float]:
    """Map PyMuPDF page coords (origin top-left, y down) to PDF user space (origin bottom-left, y up)."""
    return (float(x), height - float(y))


def rect_to_pdf_bbox(rect: fitz.Rect, height: float) -> dict[str, float]:
    """Convert PyMuPDF Rect to bbox dict using PDF coordinates (matches pdf.js / legacy pipeline)."""
    x0 = float(rect.x0)
    x1 = float(rect.x1)
    y0_pdf = height - float(rect.y1)
    y1_pdf = height - float(rect.y0)
    return {
        "x0": round_float(min(x0, x1)),
        "y0": round_float(min(y0_pdf, y1_pdf)),
        "x1": round_float(max(x0, x1)),
        "y1": round_float(max(y0_pdf, y1_pdf)),
        "width": round_float(abs(x1 - x0)),
        "height": round_float(abs(y1_pdf - y0_pdf)),
    }


def build_bbox(points: list[tuple[float, float]], margin: float = 0.0) -> dict[str, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0 = min(xs) - margin
    y0 = min(ys) - margin
    x1 = max(xs) + margin
    y1 = max(ys) + margin
    return {
        "x0": round_float(x0),
        "y0": round_float(y0),
        "x1": round_float(x1),
        "y1": round_float(y1),
        "width": round_float(x1 - x0),
        "height": round_float(y1 - y0),
    }


def quad_to_commands(quad: fitz.Quad, height: float) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
    pts = [quad.ul, quad.ur, quad.lr, quad.ll]
    pdf_pts = [point_to_pdf(p.x, p.y, height) for p in pts]
    cmds: list[dict[str, Any]] = []
    cmds.append({"op": "M", "points": [[round_float(pdf_pts[0][0]), round_float(pdf_pts[0][1])]]})
    for px, py in pdf_pts[1:]:
        cmds.append({"op": "L", "points": [[round_float(px), round_float(py)]]})
    cmds.append({"op": "Z"})
    return cmds, pdf_pts


def rect_item_to_commands(rect: fitz.Rect, height: float) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
    """Expand PDF 're'-style rectangle into M/L/L/L/Z in PDF coordinates."""
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    corners_mupdf = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    pdf_corners = [point_to_pdf(x, y, height) for x, y in corners_mupdf]
    cmds: list[dict[str, Any]] = []
    cmds.append({"op": "M", "points": [[round_float(pdf_corners[0][0]), round_float(pdf_corners[0][1])]]})
    for px, py in pdf_corners[1:]:
        cmds.append({"op": "L", "points": [[round_float(px), round_float(py)]]})
    cmds.append({"op": "Z"})
    return cmds, pdf_corners


def drawing_items_to_commands(items: list, height: float) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
    """Flatten PyMuPDF drawing items into {M,L,C,Z} commands and collect points (PDF space)."""
    commands: list[dict[str, Any]] = []
    points: list[tuple[float, float]] = []
    current: tuple[float, float] | None = None

    def ensure_move(pdf_x: float, pdf_y: float) -> None:
        nonlocal current
        pt = (pdf_x, pdf_y)
        if current is None or (abs(current[0] - pt[0]) > 1e-9 or abs(current[1] - pt[1]) > 1e-9):
            commands.append({"op": "M", "points": [[round_float(pdf_x), round_float(pdf_y)]]})
            points.append(pt)
            current = pt

    for item in items:
        op = item[0]
        if op == "l":
            _, p0, p1 = item
            x0, y0 = point_to_pdf(p0.x, p0.y, height)
            x1, y1 = point_to_pdf(p1.x, p1.y, height)
            ensure_move(x0, y0)
            commands.append({"op": "L", "points": [[round_float(x1), round_float(y1)]]})
            points.append((x1, y1))
            current = (x1, y1)
        elif op == "c":
            _, p0, p1, p2, p3 = item
            x0, y0 = point_to_pdf(p0.x, p0.y, height)
            cx1, cy1 = point_to_pdf(p1.x, p1.y, height)
            cx2, cy2 = point_to_pdf(p2.x, p2.y, height)
            x3, y3 = point_to_pdf(p3.x, p3.y, height)
            ensure_move(x0, y0)
            commands.append(
                {
                    "op": "C",
                    "points": [
                        [round_float(cx1), round_float(cy1)],
                        [round_float(cx2), round_float(cy2)],
                        [round_float(x3), round_float(y3)],
                    ],
                }
            )
            for pt in ((cx1, cy1), (cx2, cy2), (x3, y3)):
                points.append(pt)
            current = (x3, y3)
        elif op == "re":
            rect = item[1]
            rect = rect if isinstance(rect, fitz.Rect) else fitz.Rect(rect)
            sub_cmds, sub_pts = rect_item_to_commands(rect, height)
            commands.extend(sub_cmds)
            points.extend(sub_pts)
            current = sub_pts[-1] if sub_pts else current
        elif op == "qu":
            _, quad = item
            sub_cmds, sub_pts = quad_to_commands(quad, height)
            commands.extend(sub_cmds)
            points.extend(sub_pts)
            current = sub_pts[-1] if sub_pts else current
        else:
            # Unknown operator — skip geometry but avoid crashing on newer MuPDF variants.
            continue

    return commands, points


def paint_operator_for_drawing(path: dict[str, Any]) -> str:
    t = path.get("type") or "s"
    fill = path.get("fill")
    stroke = path.get("color") is not None or (path.get("width") or 0) > 0
    even_odd = path.get("even_odd")

    if t == "fs":
        return "B"
    if t == "f":
        return "f*" if even_odd else "f"
    if t == "s":
        return "S"

    if isinstance(fill, (tuple, list)) and stroke:
        return "B"
    if isinstance(fill, (tuple, list)):
        return "f*" if even_odd else "f"
    return "S"


def snippet_from_commands(commands: list[dict[str, Any]], paint_op: str, limit: int = 1800) -> str:
    parts: list[str] = []
    for cmd in commands:
        op = cmd["op"]
        if op == "M":
            x, y = cmd["points"][0]
            parts.append(f"{x} {y} m")
        elif op == "L":
            x, y = cmd["points"][0]
            parts.append(f"{x} {y} l")
        elif op == "C":
            (x1, y1), (x2, y2), (x3, y3) = cmd["points"]
            parts.append(f"{x1} {y1} {x2} {y2} {x3} {y3} c")
        elif op == "Z":
            parts.append("h")
    parts.append(paint_op)
    text = "\n".join(parts)
    if len(text) > limit:
        return text[:limit] + "\n...[truncated]"
    return text


def ensure_xref_detail(doc: fitz.Document, object_details: dict[str, dict[str, Any]], xref: int, kind_label: str, description: str) -> str:
    key = f"{xref} 0 R"
    if key in object_details:
        return key
    try:
        raw_source = doc.xref_object(xref, compressed=False)
    except Exception:
        raw_source = None
    object_details[key] = {
        "object_ref": key,
        "kind_label": kind_label,
        "description": description,
        "raw_source": raw_source,
        "decoded_stream_preview": None,
    }
    return key


def simplify_link_from_pymupdf(
    link: dict[str, Any],
    doc: fitz.Document | None = None,
) -> dict[str, Any]:
    kind_code = link.get("kind")
    target: Any = None
    kind = "unknown"

    if kind_code == fitz.LINK_URI:
        kind = "external_uri"
        target = {"uri": link.get("uri")}
    elif kind_code == fitz.LINK_GOTO:
        kind = "internal_goto_action"
        target = build_link_target(link, doc)
    elif kind_code in (fitz.LINK_NAMED,):
        if link.get("page") is not None:
            kind = "internal_goto_action"
            target = build_link_target(link, doc)
        else:
            kind = "internal_named"
            target = {"name": link.get("name")}

    return {
        "kind": kind,
        "target": target,
        "action": None,
    }


def build_link_target(
    link: dict[str, Any],
    doc: fitz.Document | None = None,
) -> dict[str, Any]:
    """Keep the destination page and make its visible target region explicit."""
    target: dict[str, Any] = {"page": link.get("page")}
    for field in ("view", "viewrect", "to", "zoom"):
        value = link.get(field)
        if isinstance(value, fitz.Point):
            value = [float(value.x), float(value.y)]
        if value is not None:
            target[field] = value

    chained_region = extract_chained_highlight_region(doc, link) if doc is not None else None
    viewrect = link.get("viewrect")
    if chained_region is not None:
        target["target_highlight_region"] = chained_region
    elif viewrect is not None:
        values: list[float] = []
        try:
            if isinstance(viewrect, str):
                values = [float(value.strip()) for value in viewrect.split(",")]
            else:
                values = [float(value) for value in viewrect]
        except (TypeError, ValueError):
            values = []
        target["target_highlight_region"] = (
            {
                "type": "rectangle",
                "x": values[0],
                "y": values[1],
                "width": values[2],
                "height": values[3],
            }
            if len(values) == 4
            else {"type": "rectangle", "raw": viewrect}
        )
    elif link.get("view") == "Fit":
        target["target_highlight_region"] = {"type": "whole_page"}
    elif link.get("to") is not None:
        to_value = target.get("to")
        target["target_highlight_region"] = {"type": "point", "to": to_value}
    else:
        target["target_highlight_region"] = None
    return target


_LINK_DESTINATION_REF_RE = re.compile(r"/(A|Dest)\s+(\d+)\s+\d+\s+R\b")
_NEXT_VALUE_RE = re.compile(
    r"/Next\s+(?:\[(?P<array>.*?)\]|(?P<single>\d+\s+\d+\s+R))",
    re.DOTALL,
)
_OBJECT_REF_RE = re.compile(r"(\d+)\s+\d+\s+R\b")
_HIGHLIGHT_JS_RE = re.compile(r"highlight\\?\([^[]*\[([^\]]+)\]", re.DOTALL)


def linked_action_objects(
    doc: fitz.Document,
    annotation_source: str,
) -> list[tuple[str, int, str]]:
    """Follow /A or /Dest and every subsequent /Next action without looping."""
    pending = [
        (match.group(1), int(match.group(2)))
        for match in _LINK_DESTINATION_REF_RE.finditer(annotation_source)
    ]
    resolved: list[tuple[str, int, str]] = []
    seen: set[int] = set()
    while pending:
        role, referenced_xref = pending.pop(0)
        if referenced_xref in seen:
            continue
        seen.add(referenced_xref)
        try:
            source = doc.xref_object(referenced_xref, compressed=False)
        except Exception as exc:
            source = f"unavailable: {exc}"
        resolved.append((role, referenced_xref, source))
        if source.startswith("unavailable:"):
            continue
        for next_match in _NEXT_VALUE_RE.finditer(source):
            value = next_match.group("array") or next_match.group("single") or ""
            pending.extend(
                ("Next", int(ref_match.group(1)))
                for ref_match in _OBJECT_REF_RE.finditer(value)
            )
    return resolved


def extract_chained_highlight_region(
    doc: fitz.Document,
    link: dict[str, Any],
) -> dict[str, Any] | None:
    xref = link.get("xref")
    if not isinstance(xref, int) or xref <= 0:
        return None
    try:
        annotation_source = doc.xref_object(xref, compressed=False)
    except Exception:
        return None
    for role, referenced_xref, source in linked_action_objects(doc, annotation_source):
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
        return {
            "type": "rectangle",
            "x0": round_float(x0),
            "y0": round_float(y0),
            "x1": round_float(x1),
            "y1": round_float(y1),
            "width": round_float(x1 - x0),
            "height": round_float(y1 - y0),
            "source": f"/{role} {referenced_xref} 0 R JavaScript highlight()",
        }
    return None


def hyperlink_source(doc: fitz.Document, link: dict[str, Any]) -> str:
    """Return the complete annotation source plus every property exposed by PyMuPDF."""
    sections: list[str] = []
    xref = link.get("xref")
    if isinstance(xref, int) and xref > 0:
        try:
            annotation_source = doc.xref_object(xref, compressed=False)
            sections.append("%% Raw PDF link annotation object\n" + annotation_source)
            for role, referenced_xref, source in linked_action_objects(doc, annotation_source):
                sections.append(
                    f"%% Raw PDF /{role} object ({referenced_xref} 0 R)\n" + source
                )
        except Exception as exc:
            sections.append(f"%% Raw PDF link annotation object unavailable: {exc}")

    sections.append(
        "%% PyMuPDF Page.get_links() properties\n"
        + json.dumps(link, default=str, ensure_ascii=False, indent=2)
    )
    if link.get("page") is not None:
        sections.append(
            "%% Target highlight region\n"
            + json.dumps(
                build_link_target(link, doc).get("target_highlight_region"),
                default=str,
                ensure_ascii=False,
                indent=2,
            )
        )
    return "\n\n".join(sections)


def extract_vector_paths_pymupdf(
    doc: fitz.Document,
    page: fitz.Page,
    page_number: int,
    height: float,
    object_details: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    drawings = page.get_drawings(extended=False)
    page_ref = ensure_xref_detail(
        doc,
        object_details,
        page.xref,
        "Page object",
        "Page dictionary (PyMuPDF); vector geometry from get_drawings().",
    )

    for idx, path in enumerate(drawings, start=1):
        commands, points = drawing_items_to_commands(path.get("items") or [], height)
        if not commands or not points:
            continue

        paint_op = paint_operator_for_drawing(path)
        width = float(path.get("width") or 1.0)
        bbox = build_bbox(list(points), margin=max(width * 0.5, 0.6)) or rect_to_pdf_bbox(path["rect"], height)
        effective_w = max(width, 0.6)

        item_id = f"page-{page_number:04d}-vector-{idx:05d}"
        snippet = snippet_from_commands(commands, paint_op)
        hl_end = len(snippet)

        item: dict[str, Any] = {
            "id": item_id,
            "kind": "vector_path",
            "page_number": page_number,
            "paint_operator": paint_op,
            "bbox": bbox,
            "commands": commands,
            "line_width": round_float(width),
            "effective_line_width": round_float(effective_w),
            "source": {
                "object_ref": page_ref,
                "context_chain": [page_ref],
                "snippet": snippet,
                "highlight_start": 0,
                "highlight_end": hl_end,
            },
            "source_comment": (
                "Geometry extracted with PyMuPDF Page.get_drawings() (merged content streams, "
                "user space aligned with the PDF viewer). Snippet is a normalized path re-serialization, not raw stream bytes."
            ),
            "reference_chain": [{"object_ref": page_ref, "role": "Page content (PyMuPDF vector export)"}],
            "summary": {
                "command_count": len(commands),
                "point_count": len(points),
            },
        }
        items.append(item)

    return items, warnings


def extract_text_items_pymupdf(
    doc: fitz.Document,
    page: fitz.Page,
    page_number: int,
    height: float,
    object_details: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    page_ref = ensure_xref_detail(
        doc,
        object_details,
        page.xref,
        "Page object",
        "Page dictionary (PyMuPDF).",
    )
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    items: list[dict[str, Any]] = []
    counter = 0

    for block in text_dict.get("blocks") or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                content = (span.get("text") or "").strip()
                if not content:
                    continue
                bbox_raw = span.get("bbox")
                if not bbox_raw or len(bbox_raw) < 4:
                    continue
                x0, y0, x1, y1 = bbox_raw[:4]
                rect = fitz.Rect(x0, y0, x1, y1)
                bbox = rect_to_pdf_bbox(rect, height)
                counter += 1
                item_id = f"page-{page_number:04d}-text-{counter:05d}"
                snippet = content[:400]
                item: dict[str, Any] = {
                    "id": item_id,
                    "kind": "text",
                    "page_number": page_number,
                    "bbox": bbox,
                    "text": {
                        "content": content,
                        "raw_glyph_text": None,
                        "operator": "pymupdf_span",
                        "font": span.get("font"),
                        "font_size": round_float(float(span.get("size") or 0)),
                        "decoded_via_tounicode": False,
                    },
                    "source": {
                        "object_ref": page_ref,
                        "context_chain": [page_ref],
                        "snippet": snippet,
                        "highlight_start": 0,
                        "highlight_end": len(snippet),
                    },
                    "source_comment": (
                        "Text span from PyMuPDF get_text('dict'): bounds and content are interpreter output, "
                        "not raw Tj operands from content streams."
                    ),
                    "reference_chain": [{"object_ref": page_ref, "role": "Page text (PyMuPDF)"}],
                    "summary": {"char_count": len(content)},
                }
                items.append(item)

    return items, warnings


def extract_image_items_pymupdf(
    doc: fitz.Document,
    page: fitz.Page,
    page_number: int,
    height: float,
    object_details: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    page_ref = ensure_xref_detail(
        doc,
        object_details,
        page.xref,
        "Page object",
        "Page dictionary (PyMuPDF).",
    )
    items: list[dict[str, Any]] = []
    counter = 0

    for info in page.get_images(full=True) or []:
        xref = int(info[0])
        width_px = info[2] if len(info) > 2 else None
        height_px = info[3] if len(info) > 3 else None
        name = str(info[7]) if len(info) > 7 else f"img{xref}"

        img_ref = ensure_xref_detail(
            doc,
            object_details,
            xref,
            "Image XObject",
            "Raster image referenced from page content.",
        )

        rects = page.get_image_rects(xref) or []
        if not rects:
            warnings.append(f"No placements found for image xref {xref} on page {page_number}.")
            continue

        for r in rects:
            rect = r[0] if isinstance(r, tuple) else r
            if not isinstance(rect, fitz.Rect):
                rect = fitz.Rect(rect)
            counter += 1
            bbox = rect_to_pdf_bbox(rect, height)
            item_id = f"page-{page_number:04d}-image-{counter:05d}"
            snippet = f"/{name} Do  % PyMuPDF image xref {xref}"
            item: dict[str, Any] = {
                "id": item_id,
                "kind": "image",
                "page_number": page_number,
                "bbox": bbox,
                "image": {
                    "name": name,
                    "pixel_width": width_px,
                    "pixel_height": height_px,
                    "filters": [],
                    "object_ref": img_ref,
                },
                "source": {
                    "object_ref": page_ref,
                    "context_chain": [page_ref],
                    "snippet": snippet,
                    "highlight_start": 0,
                    "highlight_end": len(snippet),
                },
                "source_comment": (
                    "Image placement from PyMuPDF Page.get_image_rects(); pixel size from get_images(full=True)."
                ),
                "reference_chain": [
                    {"object_ref": page_ref, "role": "Page content (PyMuPDF)"},
                    {"object_ref": img_ref, "role": "Image XObject"},
                ],
                "summary": {
                    "draw_width": bbox["width"],
                    "draw_height": bbox["height"],
                },
            }
            items.append(item)

    return items, warnings


def extract_link_items_pymupdf(
    doc: fitz.Document,
    page: fitz.Page,
    page_number: int,
    height: float,
    object_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    page_ref = ensure_xref_detail(
        doc,
        object_details,
        page.xref,
        "Page object",
        "Page dictionary (PyMuPDF).",
    )
    items: list[dict[str, Any]] = []

    for index, link in enumerate(page.get_links() or [], start=1):
        rect_m = link.get("from")
        if not isinstance(rect_m, fitz.Rect):
            continue
        bbox = rect_to_pdf_bbox(rect_m, height)
        readable = simplify_link_from_pymupdf(link, doc)

        xref = link.get("xref")
        annot_key = page_ref
        raw_source = hyperlink_source(doc, link)
        if isinstance(xref, int) and xref > 0:
            annot_key = ensure_xref_detail(
                doc,
                object_details,
                xref,
                "Link annotation",
                "Link annotation resolved by PyMuPDF.",
            )

        item: dict[str, Any] = {
            "id": f"page-{page_number:04d}-link-{index:05d}",
            "kind": "link",
            "page_number": page_number,
            "bbox": bbox,
            "link": {
                "kind": readable.get("kind"),
                "target": readable.get("target"),
                "action": readable.get("action"),
            },
            "source": {
                "object_ref": annot_key,
                "context_chain": [annot_key],
                "snippet": raw_source,
                "highlight_start": 0,
                "highlight_end": len(raw_source),
            },
            "source_comment": (
                "Complete raw PDF link annotation followed by every property returned by "
                "PyMuPDF Page.get_links(); source coordinates are converted to PDF user space like other layers."
            ),
            "reference_chain": [
                {"object_ref": annot_key, "role": "Link annotation (PyMuPDF)"},
                {"object_ref": page_ref, "role": "Owning page object"},
            ],
        }
        items.append(item)

    return items


def build_page_data(doc: fitz.Document, page: fitz.Page, page_number: int) -> dict[str, Any]:
    height = page_height_pt(page)
    object_details: dict[str, dict[str, Any]] = {}

    vector_items, vector_warnings = extract_vector_paths_pymupdf(doc, page, page_number, height, object_details)
    text_items, text_warnings = extract_text_items_pymupdf(doc, page, page_number, height, object_details)
    image_items, image_warnings = extract_image_items_pymupdf(doc, page, page_number, height, object_details)
    link_items = extract_link_items_pymupdf(doc, page, page_number, height, object_details)

    contents = page.get_contents()
    content_labels = [f"{int(xref)} 0 R" for xref in contents]

    warnings = list(dict.fromkeys(vector_warnings + text_warnings + image_warnings))

    items = vector_items + text_items + image_items + link_items

    return {
        "page_number": page_number,
        "page_object_ref": f"{page.xref} 0 R",
        "page_size": {
            "width_pt": round_float(float(page.rect.width)),
            "height_pt": round_float(height),
        },
        "content_streams": content_labels,
        "item_counts": {
            "vector_path": len(vector_items),
            "text": len(text_items),
            "image": len(image_items),
            "link": len(link_items),
        },
        "warnings": warnings,
        "object_details": object_details,
        "items": items,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_pdf_header(pdf_path: Path) -> str:
    try:
        return pdf_path.read_bytes()[:32].split(b"\r")[0].split(b"\n")[0].decode("latin-1", errors="replace")
    except OSError:
        return ""


ProgressCallback = Callable[[int, int], None]


def format_progress(current: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return "[?] 0/0"
    ratio = min(max(current / total, 0.0), 1.0)
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    percent = ratio * 100
    return f"[{bar}] {current}/{total} {percent:5.1f}%"


def build_document_data(
    pdf_path: Path,
    data_root: Path,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    doc_slug = _safe_slug(pdf_path.stem)
    pdf_target_path = data_root / "pdfs" / f"{doc_slug}.pdf"
    pdf_target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf_path, pdf_target_path)

    doc = fitz.open(pdf_path)
    try:
        doc_root = data_root / "documents" / doc_slug
        pages_root = doc_root / "pages"
        page_entries: list[dict[str, Any]] = []

        for index in range(doc.page_count):
            page = doc[index]
            page_number = index + 1
            page_data = build_page_data(doc, page, page_number)
            page_entries.append(
                {
                    "page_number": page_data["page_number"],
                    "page_size": page_data["page_size"],
                    "item_counts": page_data["item_counts"],
                    "warnings": page_data["warnings"],
                    "data_url": f"/reader-data/documents/{doc_slug}/pages/page-{page_number:04d}.json",
                }
            )
            write_json(pages_root / f"page-{page_number:04d}.json", page_data)
            if progress_callback is not None:
                progress_callback(page_number, doc.page_count)

        header = read_pdf_header(pdf_target_path)
        document_payload = {
            "id": doc_slug,
            "title": pdf_path.name,
            "pdf_url": f"/reader-data/pdfs/{doc_slug}.pdf",
            "page_count": len(page_entries),
            "pages": page_entries,
            "resolved_object_count": max(doc.xref_length() - 1, 0),
            "header": header,
        }
        write_json(doc_root / "document.json", document_payload)
        return document_payload
    finally:
        doc.close()


def _safe_slug(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name.strip())
    return safe or "document"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build interactive data for the PDF reader frontend (PyMuPDF).")
    parser.add_argument(
        "--input-dir",
        default="storage/data/eplan_pdf",
        help="Directory that contains source PDF files.",
    )
    parser.add_argument(
        "--output-dir",
        default="pdf_reader/public/reader-data",
        help="Directory where generated reader data will be written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    input_dir = repo_root / args.input_dir
    output_dir = repo_root / args.output_dir

    pdf_paths = sorted(input_dir.glob("*.pdf"))
    if not pdf_paths:
        raise SystemExit(f"No PDF files found under {input_dir}.")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    documents: list[dict[str, Any]] = []
    for pdf_index, pdf_path in enumerate(pdf_paths, start=1):
        prefix = f"[{pdf_index}/{len(pdf_paths)}] {pdf_path.name}"
        print(f"{prefix}")

        def show_page_progress(current: int, total: int) -> None:
            print(f"\r  pages {format_progress(current, total)}", end="", flush=True)

        document = build_document_data(pdf_path, output_dir, progress_callback=show_page_progress)
        documents.append(document)
        print()

    write_json(output_dir / "manifest.json", {"documents": documents})

    print(f"Built reader data for {len(documents)} PDF file(s) via PyMuPDF.")
    for document in documents:
        print(
            f"- {document['title']}: pages={document['page_count']}, "
            f"objects≈{document['resolved_object_count']}"
        )
    print(f"Reader data written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
