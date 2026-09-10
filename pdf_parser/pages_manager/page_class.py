"""Reusable page data container classes."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Iterator

from shapely.geometry import Point as ShapelyPoint
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


def coerce_bbox(bbox: Any) -> BBox:
    if isinstance(bbox, dict):
        return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
    if all(hasattr(bbox, attr) for attr in ("x0", "y0", "x1", "y1")):
        return (float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1))
    if len(bbox) != 4:
        raise ValueError("bbox must contain four values")
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def _region_geometry(region: Any) -> BaseGeometry:
    if isinstance(region, BaseGeometry):
        return region
    if isinstance(region, dict):
        if "points" in region:
            return Polygon([(float(point[0]), float(point[1])) for point in region["points"]])
        if "bbox" in region:
            return box(*coerce_bbox(region["bbox"]))
        return box(*coerce_bbox(region))
    values = list(region)
    if len(values) == 4 and all(isinstance(value, Real) for value in values):
        return box(*coerce_bbox(values))
    return Polygon([(float(point[0]), float(point[1])) for point in values])


def _bbox_to_dict(bbox: BBox) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    return {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}


def _bbox_from_points(points: list[Point]) -> BBox:
    if not points:
        raise ValueError("path must contain at least one point")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _cubic_value(a: float, b: float, c: float, d: float, t: float) -> float:
    u = 1.0 - t
    return (u**3 * a) + (3 * u**2 * t * b) + (3 * u * t**2 * c) + (t**3 * d)


def _cubic_axis_extrema(a: float, b: float, c: float, d: float) -> list[float]:
    qa = -a + 3 * b - 3 * c + d
    qb = 2 * (a - 2 * b + c)
    qc = b - a

    roots: list[float] = []
    if abs(qa) < 1e-12:
        if abs(qb) >= 1e-12:
            roots.append(-qc / qb)
    else:
        discriminant = qb * qb - 4 * qa * qc
        if discriminant >= 0:
            sqrt_d = discriminant**0.5
            roots.extend(((-qb + sqrt_d) / (2 * qa), (-qb - sqrt_d) / (2 * qa)))
    return [t for t in roots if 0.0 < t < 1.0]


def _cubic_bbox(points: list[Point]) -> BBox:
    if len(points) != 4:
        return _bbox_from_points(points)
    p0, p1, p2, p3 = points
    ts = [0.0, 1.0]
    ts.extend(_cubic_axis_extrema(p0[0], p1[0], p2[0], p3[0]))
    ts.extend(_cubic_axis_extrema(p0[1], p1[1], p2[1], p3[1]))
    sampled = [
        (
            _cubic_value(p0[0], p1[0], p2[0], p3[0], t),
            _cubic_value(p0[1], p1[1], p2[1], p3[1], t),
        )
        for t in ts
    ]
    return _bbox_from_points(sampled)


class PathBase:
    """Normalized PDF drawing path with generated geometry helpers."""

    def __init__(
        self,
        *,
        type: str | None = None,
        code: str = "",
        points: list[Any] | None = None,
        path_meta: dict[str, Any] | None = None,
    ):
        self.type = type  # "line", "curve", "rect", "quad"
        self.points = [(float(point[0]), float(point[1])) for point in (points or [])]
        self.bbox = self._build_bbox()
        self._code = code
        self._path_meta = dict(path_meta or {})

    @classmethod
    def from_record(cls, record: Any) -> "PathBase":
        if isinstance(record, PathBase):
            return record
        if not isinstance(record, dict):
            record = dict(record)
        payload = dict(record)
        return cls(
            type=payload.get("type"),
            code=payload.get("code", ""),
            points=payload.get("points", []),
            path_meta=payload.get("path_meta"),
        )

    @property
    def inner_value(self) -> dict[str, Any]:
        return {
            "code": self._code,
            "path_meta": dict(self._path_meta),
        }

    def path_meta_value(self, key: str, default: Any = None) -> Any:
        return self._path_meta.get(key, default)

    @property
    def is_dashed(self) -> bool:
        """Return whether the path metadata describes a dashed stroke."""
        dashes = self.path_meta_value("dashes")
        if dashes is None or dashes is False:
            return False
        if isinstance(dashes, str):
            normalized = dashes.strip().lower().replace(" ", "")
            return normalized not in {"", "none", "null", "[]0", "[]"}
        if isinstance(dashes, dict):
            return any(bool(value) for value in dashes.values())
        if isinstance(dashes, (list, tuple)):
            for value in dashes:
                if isinstance(value, str):
                    normalized = value.strip().lower().replace(" ", "")
                    if normalized not in {"", "none", "null", "[]0", "[]"}:
                        return True
                elif isinstance(value, (int, float)) and float(value) != 0.0:
                    return True
            return False
        return bool(dashes)

    @property
    def dash_pattern(self) -> tuple[float, ...]:
        """Return the numeric PDF dash array, excluding its phase value."""
        dashes = self.path_meta_value("dashes")
        if isinstance(dashes, str):
            match = re.search(r"\[([^]]*)\]", dashes)
            if match is None:
                return ()
            return tuple(
                float(value)
                for value in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", match.group(1))
            )
        if isinstance(dashes, dict):
            dashes = dashes.get("array", dashes.get("pattern", ()))
        if isinstance(dashes, (list, tuple)):
            values = []
            for value in dashes:
                if isinstance(value, (int, float)):
                    values.append(float(value))
            return tuple(values)
        return ()

    @property
    def dash_type(self) -> str:
        """Classify a stroke as solid, ordinary dashed, or dash-dotted."""
        if not self.is_dashed:
            return "solid"
        pattern = self.dash_pattern
        # EPLAN's ordinary dashed boundary uses one on/off pair. Dash-dot
        # boundaries contain at least two on/off pairs (for example the
        # [9.0482 1.131 .7087 1.131] pattern in example.pdf page 13).
        return "dash_dotted" if len(pattern) >= 4 else "dashed"

    def _build_bbox(self) -> dict[str, float]:
        if self.type == "curve" and len(self.points) == 4:
            return _bbox_to_dict(_cubic_bbox(self.points))
        return _bbox_to_dict(_bbox_from_points(self.points))

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {}
        if self.type is not None:
            record["type"] = self.type
        record["code"] = self._code
        record["points"] = list(self.points)
        record["path_meta"] = dict(self._path_meta)
        record["bbox"] = dict(self.bbox)
        return record


@dataclass
class VectorBase:
    """Reusable vector package consumed by vector tools."""

    vectors: list[PathBase]
    source_indices: list[int] | None = None
    tree: STRtree = field(init=False)

    def __post_init__(self) -> None:
        if self.source_indices is None:
            self.source_indices = [
                int(vector.get("index", index)) if isinstance(vector, dict) else index
                for index, vector in enumerate(self.vectors)
            ]
        self.vectors = [PathBase.from_record(vector) for vector in self.vectors]
        if len(self.source_indices) != len(self.vectors):
            raise ValueError("source_indices length must match vectors length")
        self.tree = STRtree([box(*coerce_bbox(vector.bbox)) for vector in self.vectors])

    @property
    def vector_count(self) -> int:
        return len(self.vectors)

    def __len__(self) -> int:
        return len(self.vectors)

    def __iter__(self):
        return iter(self.vectors)

    def __getitem__(self, index: int) -> PathBase:
        return self.vectors[index]

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {"index": self.source_indices[index], **vector.to_dict()}
            for index, vector in enumerate(self.vectors)
        ]

    def source_index(self, vector_id: int) -> int:
        return int(self.source_indices[vector_id])


@dataclass
class Text:
    """One normalized PDF text item and its source metadata."""

    text: str
    bbox: BBox
    font_size: float = 0.0
    direction: Point = (1.0, 0.0)
    source_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.text = str(self.text).strip()
        self.bbox = coerce_bbox(self.bbox)
        self.font_size = float(self.font_size or 0.0)
        self.direction = (float(self.direction[0]), float(self.direction[1]))
        if self.source_index is not None:
            self.source_index = int(self.source_index)
        self.metadata = dict(self.metadata)

    @classmethod
    def from_record(cls, record: Any) -> "Text":
        if isinstance(record, Text):
            return record.copy()
        if not isinstance(record, dict):
            record = dict(record)

        content = record.get("text", "")
        if isinstance(content, dict):
            content = content.get("content", "")
        bbox = record.get("location") or record.get("bbox") or record.get("position")
        if bbox is None:
            raise ValueError("text record must include location, bbox, or position")

        direction = record.get("line_direction", record.get("direction", (1.0, 0.0)))
        source_index = record.get("source_index", record.get("index"))
        reserved = {
            "text", "location", "bbox", "position", "font_size",
            "line_direction", "direction", "source_index", "index", "metadata",
        }
        metadata = dict(record.get("metadata") or {})
        metadata.update({key: value for key, value in record.items() if key not in reserved})
        return cls(
            text=str(content),
            bbox=coerce_bbox(bbox),
            font_size=float(record.get("font_size") or 0.0),
            direction=direction,
            source_index=source_index,
            metadata=metadata,
        )

    @property
    def location(self) -> BBox:
        """Legacy alias for the canonical bbox."""
        return self.bbox

    @property
    def center(self) -> Point:
        x0, y0, x1, y1 = self.bbox
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    def copy(self) -> "Text":
        return Text(
            text=self.text,
            bbox=self.bbox,
            font_size=self.font_size,
            direction=self.direction,
            source_index=self.source_index,
            metadata=self.metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the legacy text-record shape used by external JSON output."""
        record = dict(self.metadata)
        if self.source_index is not None:
            record["index"] = self.source_index
        record["text"] = self.text
        record["location"] = self.bbox
        if self.font_size:
            record["font_size"] = self.font_size
        if self.direction != (1.0, 0.0):
            record["line_direction"] = self.direction
        return record

    def get(self, key: str, default: Any = None) -> Any:
        """Provide temporary dict-style compatibility for existing consumers."""
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "text": self.text,
            "location": self.bbox,
            "bbox": self.bbox,
            "font_size": self.font_size,
            "line_direction": self.direction,
            "direction": self.direction,
            "index": self.source_index,
            "source_index": self.source_index,
        }
        if key in aliases:
            value = aliases[key]
            if value is None and key in {"index", "source_index"}:
                raise KeyError(key)
            return value
        return self.metadata[key]


@dataclass
class TextBase:
    """Collection operations for normalized Text items."""

    texts: list[Text | dict[str, Any]]

    def __post_init__(self) -> None:
        self.texts = [Text.from_record(text) for text in self.texts]

    @property
    def text_count(self) -> int:
        return len(self.texts)

    def __len__(self) -> int:
        return len(self.texts)

    def __iter__(self) -> Iterator[Text]:
        return iter(self.texts)

    def __getitem__(self, index: int) -> Text:
        return self.texts[index]

    def to_list(self) -> list[dict[str, Any]]:
        return [text.to_dict() for text in self.texts]

    def in_region(self, region: Any) -> list[Text]:
        """Return text records whose location center is inside the region."""
        geometry = _region_geometry(region)
        return [
            text.copy()
            for text in self.texts
            if geometry.covers(ShapelyPoint(text.center))
        ]

    def near_region(self, region: Any, threshold: float) -> list[Text]:
        """Return text records outside the region but within threshold of its boundary."""
        if threshold < 0:
            raise ValueError("threshold must be non-negative")
        geometry = _region_geometry(region)
        expanded = geometry.buffer(float(threshold))
        return [
            text.copy()
            for text in self.texts
            if not geometry.covers(ShapelyPoint(text.center))
            and expanded.covers(ShapelyPoint(text.center))
        ]
