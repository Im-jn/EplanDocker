"""Translation-invariant index for matching symbol anchor vectors.

The index stores the anchors of every known symbol.  Page vectors can then be
streamed through it once, instead of scanning the complete page separately for
every anchor.  Queries are deliberately conservative: the exact similarity
transform and tolerance checks remain the responsibility of ``VectorMatcher``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import PathBase
from pdf_parser.utils import select_anchor_shapes


@dataclass(frozen=True)
class AnchorRef:
    """Identity and geometry of one selected symbol anchor."""

    symbol_id: int
    anchor_index: int
    shape: PathBase


@dataclass
class _KDNode:
    point: tuple[float, ...]
    value: AnchorRef
    axis: int
    left: "_KDNode | None" = None
    right: "_KDNode | None" = None


class _KDTree:
    """Small immutable KD-tree supporting Euclidean radius queries."""

    def __init__(self, entries: Iterable[tuple[tuple[float, ...], AnchorRef]]):
        values = list(entries)
        self.dimensions = len(values[0][0]) if values else 0
        self.root = self._build(values, depth=0)

    def _build(
        self,
        entries: list[tuple[tuple[float, ...], AnchorRef]],
        *,
        depth: int,
    ) -> _KDNode | None:
        if not entries:
            return None
        axis = depth % self.dimensions
        entries.sort(key=lambda entry: entry[0][axis])
        middle = len(entries) // 2
        point, value = entries[middle]
        return _KDNode(
            point=point,
            value=value,
            axis=axis,
            left=self._build(entries[:middle], depth=depth + 1),
            right=self._build(entries[middle + 1 :], depth=depth + 1),
        )

    def query_radius(self, point: tuple[float, ...], radius: float) -> list[AnchorRef]:
        if self.root is None or len(point) != self.dimensions:
            return []
        radius_squared = radius * radius
        results: list[AnchorRef] = []

        def visit(node: _KDNode | None) -> None:
            if node is None:
                return
            delta = point[node.axis] - node.point[node.axis]
            distance_squared = sum(
                (left - right) ** 2 for left, right in zip(point, node.point)
            )
            if distance_squared <= radius_squared:
                results.append(node.value)
            if delta <= radius:
                visit(node.left)
            if delta >= -radius:
                visit(node.right)

        visit(self.root)
        return results


def _length(shape: PathBase) -> float:
    points = shape.points
    if len(points) < 2:
        return 0.0
    if len(points) == 2:
        return math.dist(points[0], points[1])
    return max(
        (math.dist(left, right) for index, left in enumerate(points) for right in points[index + 1 :]),
        default=0.0,
    )


def _line_direction_feature(shape: PathBase) -> tuple[float, float]:
    left, right = shape.points
    angle = math.atan2(right[1] - left[1], right[0] - left[0])
    # Four times the angle makes both endpoint reversal (pi) and every allowed
    # quarter turn (pi/2) map to exactly the same point on the unit circle.
    return (math.cos(4.0 * angle), math.sin(4.0 * angle))


def _distance_signature(shape: PathBase) -> tuple[float, ...]:
    distances = sorted(
        math.dist(left, right)
        for index, left in enumerate(shape.points)
        for right in shape.points[index + 1 :]
    )
    diameter = max(distances, default=0.0)
    if diameter <= 1e-12:
        return tuple(0.0 for _ in distances)
    return tuple(distance / diameter for distance in distances)


class SymbolVectorIndex:
    """KD-tree index over anchors selected from a complete symbol catalog."""

    def __init__(self, symbol_list: Sequence[Sequence[PathBase]]):
        self.symbols = [list(symbol) for symbol in symbol_list]
        if any(not symbol for symbol in self.symbols):
            raise ValueError("symbol_list must not contain empty symbols")

        self.anchors_by_symbol: list[list[AnchorRef]] = []
        grouped: dict[tuple[str, bool, int], list[AnchorRef]] = {}
        for symbol_id, symbol in enumerate(self.symbols):
            anchors = [
                AnchorRef(symbol_id, anchor_index, shape)
                for anchor_index, shape in enumerate(select_anchor_shapes(symbol))
            ]
            self.anchors_by_symbol.append(anchors)
            for anchor in anchors:
                grouped.setdefault(self._category(anchor.shape), []).append(anchor)

        self._lengths: dict[int, float] = {}
        self._trees: dict[tuple[str, bool, int], _KDTree] = {}
        for category, anchors in grouped.items():
            entries = []
            for anchor in anchors:
                self._lengths[id(anchor)] = _length(anchor.shape)
                entries.append((self._feature(anchor.shape), anchor))
            self._trees[category] = _KDTree(entries)

    @staticmethod
    def _category(shape: PathBase) -> tuple[str, bool, int]:
        return (shape.type, shape.is_dashed, len(shape.points))

    @staticmethod
    def _feature(shape: PathBase) -> tuple[float, ...]:
        if len(shape.points) == 2:
            return _line_direction_feature(shape)
        return _distance_signature(shape)

    def query(
        self,
        candidate: PathBase,
        *,
        tolerance: float = PARSER_CONFIG.vector_matcher.shape_tolerance_pt,
        scale_range: tuple[float, float] = (
            PARSER_CONFIG.vector_matcher.scale_min,
            PARSER_CONFIG.vector_matcher.pattern_scale_max,
        ),
        allow_mirror: bool = True,
    ) -> list[AnchorRef]:
        """Return a conservative set of anchors that may match ``candidate``."""
        tree = self._trees.get(self._category(candidate))
        if tree is None:
            return []

        candidate_length = _length(candidate)
        if candidate_length <= 1e-12:
            radius = math.inf
        elif len(candidate.points) == 2:
            # A fixed-angle fit centers endpoint error across both ends.  This
            # bound is intentionally a little wider than the exact condition.
            angle_error = math.asin(min(1.0, 2.0 * tolerance / candidate_length))
            # The folded feature's greatest distinct angular separation is 45
            # degrees.  Beyond that point sin(2*error) decreases again, so a
            # short line must conservatively query the complete unit circle.
            radius = (
                2.0
                if angle_error >= math.pi / 4.0
                else 2.0 * math.sin(2.0 * angle_error)
            ) + 1e-12
        else:
            # Each pair distance may move by 2*tolerance.  Normalising by the
            # diameter adds another conservative factor of two.
            per_dimension = min(1.0, 4.0 * tolerance / candidate_length)
            radius = math.sqrt(tree.dimensions) * per_dimension + 1e-12

        feature = self._feature(candidate)
        query_features = [feature]
        if allow_mirror and len(candidate.points) == 2:
            # Reflection changes theta to pi-theta.  In the folded line feature
            # that preserves cosine and negates sine.
            query_features.append((feature[0], -feature[1]))
        possible_by_id: dict[int, AnchorRef] = {}
        for query_feature in query_features:
            for anchor in tree.query_radius(query_feature, radius):
                possible_by_id[id(anchor)] = anchor
        possible = possible_by_id.values()
        min_scale, max_scale = scale_range
        result: list[AnchorRef] = []
        for anchor in possible:
            anchor_length = self._lengths[id(anchor)]
            if anchor_length <= 1e-12:
                if candidate_length <= tolerance:
                    result.append(anchor)
                continue
            scale = candidate_length / anchor_length
            # Length can differ slightly while the point error remains within
            # tolerance, so retain a conservative scale-bound margin.
            scale_margin = 2.0 * tolerance / anchor_length
            if min_scale - scale_margin <= scale <= max_scale + scale_margin:
                result.append(anchor)
        return result
