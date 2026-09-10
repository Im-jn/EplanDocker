"""Assign page text to extracted components or wire endpoints."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import cmp_to_key
from numbers import Real
from typing import Any

from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from pdf_parser.config import PARSER_CONFIG
from pdf_parser.pages_manager import Text, TextBase
from pdf_parser.utils import coerce_bbox


ARROW_TYPE = "arrow"
CONTAINER_TYPES = frozenset({"box", "dashed"})
TEXT_MATCH_COMPONENT_TYPES = CONTAINER_TYPES | {ARROW_TYPE}
SYMBOL_TYPE = "symbol"
WIRE_MARK_TYPE = "wire_mark"
DEVICE_TAG_RE = re.compile(r"^[+\-=]{1,2}\s*[A-Za-z][A-Za-z0-9_.-]*$")
CROSS_REFERENCE_RE = re.compile(
    r"(?:^[A-Za-z]?\d+[./][A-Za-z0-9.:-]+$)|(?:^\d+\.[A-Za-z]+:\d+$)",
    re.IGNORECASE,
)
PARAMETER_RE = re.compile(
    r"^(?:"
    r"IP\s*\d{2}"
    r"|(?:\d+(?:[.,]\d+)?\s*)+"
    r"(?:m?[VAW]|k[VAW]|Hz|kHz|MHz|Ω|ohm|mm(?:²|2)?|cm|m|°C|bar|Pa|%)"
    r"(?:\s*(?:AC|DC))?"
    r")$",
    re.IGNORECASE,
)
PIN_TOKEN_RE = re.compile(r"^(?=.{1,4}$)(?=.*\d)[A-Za-z0-9.]+$")


@dataclass(frozen=True)
class _Assignment:
    text: Text
    position: int
    target: dict[str, Any] | None
    via: str
    distance: float


@dataclass
class _FlowEdge:
    to: int
    reverse: int
    capacity: int
    cost: float


class TextMatcher:
    """Match individual Text items, then classify globally clustered content."""

    def __init__(
        self,
        text_base: TextBase,
        *,
        proximity_threshold: float = PARSER_CONFIG.text_matching.proximity_threshold_pt,
        arrow_proximity_threshold: float = PARSER_CONFIG.text_matching.arrow_proximity_threshold_pt,
        endpoint_proximity_threshold: float = PARSER_CONFIG.text_matching.endpoint_proximity_threshold_pt,
        wire_mark_proximity_threshold: float = PARSER_CONFIG.text_matching.wire_mark_proximity_threshold_pt,
        symbol_proximity_threshold: float = PARSER_CONFIG.text_matching.symbol_proximity_threshold_pt,
        endpoint_max_chars: int = PARSER_CONFIG.text_matching.endpoint_max_chars,
        endpoint_max_tokens: int = PARSER_CONFIG.text_matching.endpoint_max_tokens,
        proximity_ambiguity_margin: float = PARSER_CONFIG.text_matching.proximity_ambiguity_margin_pt,
        diagonal_distance_factor: float = PARSER_CONFIG.text_matching.diagonal_distance_factor,
        cluster_distance: float = PARSER_CONFIG.text_matching.cluster_distance_pt,
        merge_distance: float = PARSER_CONFIG.text_matching.merge_distance_pt,
        alignment_tolerance: float = PARSER_CONFIG.text_matching.alignment_tolerance_pt,
        font_tolerance: float = PARSER_CONFIG.text_matching.font_tolerance_pt,
    ):
        if not isinstance(text_base, TextBase):
            raise TypeError("text_base must be a TextBase")
        for name, value in (
            ("proximity_threshold", proximity_threshold),
            ("arrow_proximity_threshold", arrow_proximity_threshold),
            ("endpoint_proximity_threshold", endpoint_proximity_threshold),
            ("wire_mark_proximity_threshold", wire_mark_proximity_threshold),
            ("symbol_proximity_threshold", symbol_proximity_threshold),
            ("endpoint_max_chars", endpoint_max_chars),
            ("endpoint_max_tokens", endpoint_max_tokens),
            ("proximity_ambiguity_margin", proximity_ambiguity_margin),
            ("diagonal_distance_factor", diagonal_distance_factor),
            ("cluster_distance", cluster_distance),
            ("merge_distance", merge_distance),
            ("alignment_tolerance", alignment_tolerance),
            ("font_tolerance", font_tolerance),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        self.text_base = text_base
        self.proximity_threshold = float(proximity_threshold)
        self.arrow_proximity_threshold = float(arrow_proximity_threshold)
        self.endpoint_proximity_threshold = float(endpoint_proximity_threshold)
        self.wire_mark_proximity_threshold = float(wire_mark_proximity_threshold)
        self.symbol_proximity_threshold = float(symbol_proximity_threshold)
        self.endpoint_max_chars = int(endpoint_max_chars)
        self.endpoint_max_tokens = int(endpoint_max_tokens)
        self.proximity_ambiguity_margin = float(proximity_ambiguity_margin)
        self.diagonal_distance_factor = float(diagonal_distance_factor)
        self.cluster_distance = float(cluster_distance)
        self.merge_distance = float(merge_distance)
        self.alignment_tolerance = float(alignment_tolerance)
        self.font_tolerance = float(font_tolerance)

    def text_in_box(self, bbox: Any) -> TextBase:
        """Return texts whose bbox centers are inside the requested bbox."""
        return TextBase(self.text_base.in_region(box(*coerce_bbox(bbox))))

    def text_in_polygon(self, polygon: Any) -> TextBase:
        """Return texts whose bbox centers are inside the requested polygon."""
        geometry = self._polygon_geometry(polygon)
        if geometry is None:
            raise ValueError("polygon must contain geometry, points, or a bbox")
        return TextBase(self.text_base.in_region(geometry))

    def match_components(
        self,
        components: list[dict[str, Any]],
        *,
        endpoints: list[dict[str, Any]] | None = None,
        symbol_records: list[dict[str, Any]] | None = None,
        excluded_indices: set[int] | None = None,
    ) -> dict[str, Any]:
        """Cluster text, then match specific targets before symbol-bbox fallback."""
        excluded = excluded_indices or set()
        endpoint_records = endpoints or []
        candidates = [
            (position, text)
            for position, text in enumerate(self.text_base)
            if self._text_index(text, position) not in excluded
        ]
        matches: list[dict[str, Any]] = []
        matched_positions: set[int] = set()
        symbol_components = [
            component for component in components
            if component.get("type") == SYMBOL_TYPE
        ]
        clusters = self._spatial_clusters([
            _Assignment(text, position, None, "unmatched", float("inf"))
            for position, text in candidates
        ])

        remaining_clusters = self._match_endpoint_clusters(
            clusters,
            endpoint_records,
            matches,
            matched_positions,
        )
        wire_marks = [
            component for component in components
            if component.get("type") == WIRE_MARK_TYPE
        ]
        remaining_clusters = self._match_proximity_clusters(
            remaining_clusters,
            wire_marks,
            matches,
            matched_positions,
            threshold=self.wire_mark_proximity_threshold,
            allow_containment=False,
        )
        if symbol_records is not None:
            remaining_clusters = self._match_symbol_clusters(
                remaining_clusters,
                symbol_components,
                symbol_records,
                matches,
                matched_positions,
            )
        else:
            # Keep callers without a catalog functional while production uses
            # marker-aware matching.
            remaining_clusters = self._match_proximity_clusters(
                remaining_clusters,
                symbol_components,
                matches,
                matched_positions,
                threshold=self.proximity_threshold,
                allow_containment=False,
            )
        arrows = [
            component for component in components
            if component.get("type") == ARROW_TYPE
        ]
        remaining_clusters = self._match_proximity_clusters(
            remaining_clusters,
            arrows,
            matches,
            matched_positions,
            threshold=self.arrow_proximity_threshold,
            allow_containment=False,
        )
        containers = [
            component for component in components
            if component.get("type") in CONTAINER_TYPES
        ]
        remaining_clusters = self._match_proximity_clusters(
            remaining_clusters,
            containers,
            matches,
            matched_positions,
            threshold=self.proximity_threshold,
            allow_containment=True,
        )
        # A symbol bbox can overlap endpoint labels and other meaningful text.
        # Treat bbox containment as the final fallback so more specific targets
        # always get the first opportunity to claim their text.
        self._match_symbol_bbox_containment(
            [
                (assignment.position, assignment.text)
                for cluster in remaining_clusters
                for assignment in cluster
            ],
            symbol_components,
            matches,
            matched_positions,
        )

        remaining_text = self._remaining_cluster_records(clusters, matched_positions)
        return {
            "components": components,
            "endpoints": endpoint_records,
            "matches": matches,
            "remaining_text": remaining_text,
        }

    def match_elements(
        self,
        elements: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Match text to intermediate elements using final-model terminology."""
        result = self.match_components(elements, **kwargs)
        for match in result["matches"]:
            if "component_id" in match:
                match["element_id"] = match.pop("component_id")
        result["elements"] = result.pop("components")
        return result

    def _match_symbol_bbox_containment(
        self,
        candidates: list[tuple[int, Text]],
        symbols: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        matched_positions: set[int],
    ) -> list[tuple[int, Text]]:
        """Consume still-unmatched text covered by a symbol bbox as descriptions."""
        assignments: list[_Assignment] = []
        remaining: list[tuple[int, Text]] = []
        target_order_by_identity = {
            id(symbol): order for order, symbol in enumerate(symbols)
        }
        for position, text in candidates:
            text_polygon = box(*text.bbox)
            containing = [
                (self._target_area(symbol), symbol_order, symbol)
                for symbol_order, symbol in enumerate(symbols)
                if box(*coerce_bbox(symbol["bbox"])).covers(text_polygon)
            ]
            if not containing:
                remaining.append((position, text))
                continue
            _, _, symbol = min(
                containing,
                key=lambda item: (
                    item[0],
                    self._center_distance(text.bbox, item[2]["bbox"]),
                    int(item[2].get("id", item[1])),
                ),
            )
            assignments.append(
                _Assignment(text, position, symbol, "symbol_bbox", 0.0)
            )

        assignments_by_symbol: dict[int, list[_Assignment]] = {}
        for assignment in assignments:
            assignments_by_symbol.setdefault(id(assignment.target), []).append(assignment)
        for symbol_identity, symbol_assignments in assignments_by_symbol.items():
            symbol = symbol_assignments[0].target
            if symbol is None:
                continue
            symbol_order = target_order_by_identity[symbol_identity]
            for cluster in self._spatial_clusters(symbol_assignments):
                self._store_cluster(
                    cluster,
                    self._merge_same_font_content(cluster),
                    symbol,
                    symbol_order,
                    matches,
                    matched_positions,
                    all_descriptions=True,
                )
        return remaining

    def _match_endpoint_clusters(
        self,
        clusters: list[list[_Assignment]],
        endpoints: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        matched_positions: set[int],
    ) -> list[list[_Assignment]]:
        eligible = []
        for cluster_index, cluster in enumerate(clusters):
            groups = self._merge_same_font_content(cluster)
            if not all(self._is_endpoint_content_group(group) for group in groups):
                continue
            cluster_bbox = self._cluster_bbox(cluster)
            distances = sorted(
                (
                    self._edge_distance(cluster_bbox, endpoint["bbox"]),
                    endpoint_order,
                    endpoint,
                )
                for endpoint_order, endpoint in enumerate(endpoints)
            )
            if not distances or distances[0][0] > self.endpoint_proximity_threshold:
                continue
            if (
                len(distances) > 1
                and distances[1][0] <= self.endpoint_proximity_threshold
                and distances[1][0] - distances[0][0] < self.proximity_ambiguity_margin
            ):
                continue
            distance, endpoint_order, endpoint = distances[0]
            eligible.append((distance, cluster_index, endpoint_order, endpoint, groups))

        used_clusters: set[int] = set()
        used_endpoints: set[int] = set()
        for _, cluster_index, endpoint_order, endpoint, groups in sorted(eligible):
            if cluster_index in used_clusters or endpoint_order in used_endpoints:
                continue
            self._store_cluster(
                clusters[cluster_index],
                groups,
                endpoint,
                endpoint_order,
                matches,
                matched_positions,
                entity_kind="endpoint",
            )
            used_clusters.add(cluster_index)
            used_endpoints.add(endpoint_order)
        return [
            cluster for index, cluster in enumerate(clusters)
            if index not in used_clusters
        ]

    def _is_endpoint_content_group(self, group: dict[str, Any]) -> bool:
        compact = " ".join(str(group.get("text", "")).strip().split())
        return bool(compact) and (
            len(compact) <= self.endpoint_max_chars
            and len(compact.split()) <= self.endpoint_max_tokens
        )

    def _match_symbol_clusters(
        self,
        clusters: list[list[_Assignment]],
        symbols: list[dict[str, Any]],
        symbol_records: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        matched_positions: set[int],
    ) -> list[list[_Assignment]]:
        markers_by_symbol: dict[int, set[str]] = {}
        for record in symbol_records:
            symbol_id = record.get("symbol")
            if not isinstance(symbol_id, int):
                continue
            markers_by_symbol.setdefault(symbol_id, set()).update(
                self._record_markers(record.get("type", ""))
            )

        candidates = []
        groups_by_cluster = [
            self._merge_same_font_content(cluster)
            for cluster in clusters
        ]
        for symbol_order, symbol in enumerate(symbols):
            symbol_id = symbol.get("attributes", {}).get("symbol_id")
            markers = markers_by_symbol.get(symbol_id, set())
            if not markers:
                continue
            for cluster_index, groups in enumerate(groups_by_cluster):
                marker_match = self._best_marker_group(groups, markers)
                if marker_match is None:
                    continue
                quality, marker_group_index = marker_match
                distance = self._edge_distance(
                    self._cluster_bbox(clusters[cluster_index]),
                    symbol["bbox"],
                )
                if distance > self.symbol_proximity_threshold:
                    continue
                candidates.append((
                    quality,
                    distance,
                    cluster_index,
                    symbol_order,
                    marker_group_index,
                    symbol,
                ))

        selected = self._minimum_cost_maximum_symbol_matching(
            candidates,
            symbol_count=len(symbols),
            cluster_count=len(clusters),
        )
        used_clusters: set[int] = set()
        for (
            _quality,
            _distance,
            cluster_index,
            symbol_order,
            marker_group_index,
            symbol,
        ) in selected:
            self._store_cluster(
                clusters[cluster_index],
                groups_by_cluster[cluster_index],
                symbol,
                symbol_order,
                matches,
                matched_positions,
                forced_title_group=marker_group_index,
            )
            used_clusters.add(cluster_index)
        return [
            cluster for index, cluster in enumerate(clusters)
            if index not in used_clusters
        ]

    def _minimum_cost_maximum_symbol_matching(
        self,
        candidates: list[tuple[int, float, int, int, int, dict[str, Any]]],
        *,
        symbol_count: int,
        cluster_count: int,
    ) -> list[tuple[int, float, int, int, int, dict[str, Any]]]:
        """Return a maximum-cardinality, minimum-cost bipartite matching."""
        if not candidates:
            return []

        source = 0
        symbol_offset = 1
        cluster_offset = symbol_offset + symbol_count
        sink = cluster_offset + cluster_count
        graph: list[list[_FlowEdge]] = [[] for _ in range(sink + 1)]

        def add_edge(left: int, right: int, capacity: int, cost: float) -> _FlowEdge:
            forward = _FlowEdge(right, len(graph[right]), capacity, cost)
            reverse = _FlowEdge(left, len(graph[left]), 0, -cost)
            graph[left].append(forward)
            graph[right].append(reverse)
            return forward

        for symbol_order in range(symbol_count):
            add_edge(source, symbol_offset + symbol_order, 1, 0.0)
        for cluster_index in range(cluster_count):
            add_edge(cluster_offset + cluster_index, sink, 1, 0.0)

        maximum_pairs = min(symbol_count, cluster_count)
        marker_weight = self.symbol_proximity_threshold * (maximum_pairs + 1)
        candidate_edges: list[tuple[_FlowEdge, tuple[int, float, int, int, int, dict[str, Any]]]] = []
        for candidate in candidates:
            quality, distance, cluster_index, symbol_order, _, _ = candidate
            # The marker term dominates every possible total distance change,
            # making the optimization lexicographic after cardinality.
            cost = quality * marker_weight + distance
            edge = add_edge(
                symbol_offset + symbol_order,
                cluster_offset + cluster_index,
                1,
                cost,
            )
            candidate_edges.append((edge, candidate))

        node_count = len(graph)
        while True:
            distances = [float("inf")] * node_count
            previous: list[tuple[int, int] | None] = [None] * node_count
            distances[source] = 0.0
            for _ in range(node_count - 1):
                changed = False
                for node, edges in enumerate(graph):
                    if math.isinf(distances[node]):
                        continue
                    for edge_index, edge in enumerate(edges):
                        if edge.capacity <= 0:
                            continue
                        candidate_distance = distances[node] + edge.cost
                        if candidate_distance + 1e-9 < distances[edge.to]:
                            distances[edge.to] = candidate_distance
                            previous[edge.to] = (node, edge_index)
                            changed = True
                if not changed:
                    break
            if previous[sink] is None:
                break

            node = sink
            while node != source:
                prior = previous[node]
                if prior is None:
                    raise RuntimeError("incomplete symbol matching augmenting path")
                prior_node, edge_index = prior
                edge = graph[prior_node][edge_index]
                edge.capacity -= 1
                graph[node][edge.reverse].capacity += 1
                node = prior_node

        selected = [
            candidate
            for edge, candidate in candidate_edges
            if edge.capacity == 0
        ]
        return sorted(selected, key=lambda item: (item[2], item[3]))

    def _match_proximity_clusters(
        self,
        clusters: list[list[_Assignment]],
        targets: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        matched_positions: set[int],
        *,
        threshold: float,
        allow_containment: bool,
    ) -> list[list[_Assignment]]:
        remaining_clusters: list[list[_Assignment]] = []
        target_order_by_identity = {
            id(target): order for order, target in enumerate(targets)
        }
        for cluster in clusters:
            raw = [(item.position, item.text) for item in cluster]
            assignments, remaining = self._assign_by_proximity(
                raw,
                targets,
                threshold=threshold,
            )
            if allow_containment:
                contained, remaining = self._assign_by_containment(remaining, targets)
                assignments.extend(contained)
            assignment_by_position = {
                assignment.position: assignment for assignment in assignments
            }
            voted_cluster = [
                assignment_by_position.get(item.position, item)
                for item in cluster
            ]
            groups = self._merge_same_font_content(voted_cluster)
            largest_font = max(group["font_size"] for group in groups)
            has_font_hierarchy = any(
                not math.isclose(
                    group["font_size"],
                    largest_font,
                    abs_tol=self.font_tolerance,
                )
                for group in groups
            )
            for group in groups:
                group["role"] = (
                    "title"
                    if has_font_hierarchy and math.isclose(
                        group["font_size"],
                        largest_font,
                        abs_tol=self.font_tolerance,
                    )
                    else "description"
                )
            vote = self._vote_cluster_target(
                voted_cluster,
                groups,
                targets,
                target_order_by_identity,
            ) if targets else None
            if vote is None:
                remaining_clusters.append(cluster)
                continue
            target, target_order = vote
            self._store_cluster(
                voted_cluster,
                groups,
                target,
                target_order,
                matches,
                matched_positions,
            )
        return remaining_clusters

    def _store_cluster(
        self,
        cluster: list[_Assignment],
        groups: list[dict[str, Any]],
        target: dict[str, Any],
        target_order: int,
        matches: list[dict[str, Any]],
        matched_positions: set[int],
        *,
        forced_title_group: int | None = None,
        entity_kind: str = "component",
        all_descriptions: bool = False,
    ) -> None:
        title_group_indices = set() if all_descriptions else self._title_group_indices(
            groups,
            forced_title_group=forced_title_group,
        )
        context = self._cluster_context(cluster, groups, title_group_indices)
        matched_positions.update(item.position for item in cluster)
        for group_index, group in enumerate(groups):
            is_endpoint = entity_kind == "endpoint"
            role = "title" if group_index in title_group_indices else "description"
            if role == "title":
                target.setdefault("title", []).append(group["text"])
            match = {
                "text": group["text"],
                "text_indices": [
                    self._text_index(item.text, item.position)
                    for item in group["assignments"]
                ],
                "role": role,
                "semantic_role": self.classify_semantic_role(group["text"]),
            }
            target_id = int(target.get("id", target_order))
            if is_endpoint:
                match["endpoint_id"] = target_id
            else:
                match["component_id"] = target_id
            matches.append(match)
        target.setdefault("descriptions", []).append(context)

    def _title_group_indices(
        self,
        groups: list[dict[str, Any]],
        *,
        forced_title_group: int | None = None,
    ) -> set[int]:
        if forced_title_group is not None:
            return {forced_title_group}
        largest_font = max(group["font_size"] for group in groups)
        if all(
            math.isclose(group["font_size"], largest_font, abs_tol=self.font_tolerance)
            for group in groups
        ):
            return set()
        return {
            index
            for index, group in enumerate(groups)
            if math.isclose(group["font_size"], largest_font, abs_tol=self.font_tolerance)
        }

    @staticmethod
    def _cluster_context(
        cluster: list[_Assignment],
        groups: list[dict[str, Any]],
        title_group_indices: set[int],
    ) -> str:
        title_positions = {
            item.position
            for index, group in enumerate(groups)
            if index in title_group_indices
            for item in group["assignments"]
        }
        title = " ".join(
            group["text"]
            for index, group in enumerate(groups)
            if index in title_group_indices
        ).strip()
        description = " ".join(
            item.text.text
            for item in cluster
            if item.position not in title_positions
        ).strip()
        if title and description:
            return f"{title}: {description}"
        return title or description

    def _remaining_cluster_records(
        self,
        clusters: list[list[_Assignment]],
        matched_positions: set[int],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for cluster in clusters:
            remaining = [item for item in cluster if item.position not in matched_positions]
            if not remaining:
                continue
            groups = self._merge_same_font_content(remaining)
            title_group_indices = self._title_group_indices(groups)
            context = self._cluster_context(remaining, groups, title_group_indices)
            x0, y0, x1, y1 = self._cluster_bbox(remaining)
            records.append({
                "title": [
                    group["text"]
                    for index, group in enumerate(groups)
                    if index in title_group_indices
                ],
                "descriptions": [context],
                "text": context,
                "text_indices": [
                    self._text_index(item.text, item.position)
                    for item in remaining
                ],
                "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            })
        return records

    @staticmethod
    def _record_markers(value: Any) -> set[str]:
        return {
            token.upper()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]*", str(value))
        }

    @staticmethod
    def _best_marker_group(
        groups: list[dict[str, Any]],
        markers: set[str],
    ) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        for group_index, group in enumerate(groups):
            tokens = re.findall(r"[+\-=]?[A-Za-z][A-Za-z0-9_.-]*", group["text"])
            for token in tokens:
                normalized = token.lstrip("+-=").upper()
                for marker in markers:
                    quality = None
                    if normalized == marker:
                        quality = 0
                    elif re.fullmatch(re.escape(marker) + r"[0-9_.-]+", normalized):
                        quality = 1
                    if quality is not None:
                        candidate = (quality, group_index)
                        best = candidate if best is None else min(best, candidate)
        return best

    @staticmethod
    def _cluster_bbox(cluster: list[_Assignment]) -> tuple[float, float, float, float]:
        boxes = [coerce_bbox(item.text.bbox) for item in cluster]
        return (
            min(bbox[0] for bbox in boxes),
            min(bbox[1] for bbox in boxes),
            max(bbox[2] for bbox in boxes),
            max(bbox[3] for bbox in boxes),
        )

    def _assign_by_proximity(
        self,
        candidates: list[tuple[int, Text]],
        targets: list[dict[str, Any]],
        *,
        threshold: float | None = None,
    ) -> tuple[list[_Assignment], list[tuple[int, Text]]]:
        active_threshold = self.proximity_threshold if threshold is None else threshold
        assignments: list[_Assignment] = []
        remaining: list[tuple[int, Text]] = []
        for position, text in candidates:
            distances = [
                (
                    self._edge_distance(
                        text.bbox,
                        target["bbox"],
                        diagonal_factor=self.diagonal_distance_factor,
                    ),
                    target_order,
                    target,
                )
                for target_order, target in enumerate(targets)
            ]
            if not distances:
                remaining.append((position, text))
                continue
            distances.sort(
                key=lambda item: (
                    item[0],
                    self._target_area(item[2]),
                    int(item[2].get("id", item[1])),
                )
            )
            distance, _, target = distances[0]
            second_distance = distances[1][0] if len(distances) > 1 else float("inf")
            ambiguous = (
                second_distance <= active_threshold
                and second_distance - distance < self.proximity_ambiguity_margin
            )
            if distance > active_threshold or ambiguous:
                remaining.append((position, text))
                continue
            assignments.append(_Assignment(text, position, target, "proximity", distance))
        return assignments, remaining

    def _assign_by_containment(
        self,
        candidates: list[tuple[int, Text]],
        components: list[dict[str, Any]],
    ) -> tuple[list[_Assignment], list[tuple[int, Text]]]:
        containers = [
            (component_order, component, polygon)
            for component_order, component in enumerate(components)
            if component.get("type") in TEXT_MATCH_COMPONENT_TYPES
            if (polygon := self._component_polygon(component)) is not None
        ]
        assignments: list[_Assignment] = []
        remaining: list[tuple[int, Text]] = []
        for position, text in candidates:
            text_polygon = box(*text.bbox)
            containing = [
                (float(polygon.area), component_order, component)
                for component_order, component, polygon in containers
                if polygon.covers(text_polygon)
            ]
            if not containing:
                remaining.append((position, text))
                continue
            _, _, component = min(
                containing,
                key=lambda item: (
                    item[0],
                    self._center_distance(text.bbox, item[2]["bbox"]),
                    int(item[2].get("id", item[1])),
                ),
            )
            assignments.append(_Assignment(text, position, component, "containment", 0.0))
        return assignments, remaining

    def _vote_cluster_target(
        self,
        cluster: list[_Assignment],
        content_groups: list[dict[str, Any]],
        targets: list[dict[str, Any]],
        target_order_by_identity: dict[int, int],
    ) -> tuple[dict[str, Any], int] | None:
        largest_font = max(group["font_size"] for group in content_groups)
        has_font_hierarchy = any(
            not math.isclose(
                group["font_size"],
                largest_font,
                abs_tol=self.font_tolerance,
            )
            for group in content_groups
        )
        if has_font_hierarchy:
            electorate = [
                assignment
                for group in content_groups
                if group["role"] == "title"
                for assignment in group["assignments"]
                if assignment.target is not None
            ]
        else:
            electorate = [
                assignment
                for assignment in cluster
                if assignment.target is not None
            ]

        if not electorate and has_font_hierarchy:
            electorate = [
                assignment
                for assignment in cluster
                if assignment.target is not None
            ]
        if not electorate:
            return None

        votes: dict[int, list[_Assignment]] = {}
        for assignment in electorate:
            target_order = target_order_by_identity[id(assignment.target)]
            votes.setdefault(target_order, []).append(assignment)
        winner_order = min(
            votes,
            key=lambda order: (
                -len(votes[order]),
                sum(assignment.distance for assignment in votes[order]),
                self._target_area(targets[order]),
                int(targets[order].get("id", order)),
            ),
        )
        return targets[winner_order], winner_order

    @staticmethod
    def classify_semantic_role(text: str) -> str:
        """Classify what content means without changing its title/description level."""
        compact = " ".join(str(text).strip().split())
        if not compact:
            return "description"
        if DEVICE_TAG_RE.fullmatch(compact):
            return "device_tag"
        if CROSS_REFERENCE_RE.fullmatch(compact):
            return "cross_reference"
        if PARAMETER_RE.fullmatch(compact):
            return "parameter"
        tokens = compact.split()
        if tokens and all(PIN_TOKEN_RE.fullmatch(token) for token in tokens):
            return "pin_label"
        return "description"

    def _spatial_clusters(self, assignments: list[_Assignment]) -> list[list[_Assignment]]:
        parent = list(range(len(assignments)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left_index, left in enumerate(assignments):
            for right_index in range(left_index + 1, len(assignments)):
                right = assignments[right_index]
                if self._are_spatially_related(
                    left.text,
                    right.text,
                    self.cluster_distance,
                ):
                    union(left_index, right_index)

        grouped: dict[int, list[_Assignment]] = {}
        for index, assignment in enumerate(assignments):
            grouped.setdefault(find(index), []).append(assignment)
        clusters = list(grouped.values())
        for cluster in clusters:
            self._sort_assignments(cluster)
        clusters.sort(key=lambda cluster: self._geometric_order(cluster[0]))
        return clusters

    def _merge_same_font_content(
        self,
        cluster: list[_Assignment],
    ) -> list[dict[str, Any]]:
        parent = list(range(len(cluster)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left_index, left in enumerate(cluster):
            for right_index in range(left_index + 1, len(cluster)):
                right = cluster[right_index]
                if not math.isclose(
                    left.text.font_size,
                    right.text.font_size,
                    abs_tol=self.font_tolerance,
                ):
                    continue
                adaptive_distance = max(
                    self.merge_distance,
                    0.8 * max(left.text.font_size, right.text.font_size),
                )
                if self._are_spatially_related(
                    left.text,
                    right.text,
                    adaptive_distance,
                ):
                    union(left_index, right_index)

        grouped: dict[int, list[_Assignment]] = {}
        for index, assignment in enumerate(cluster):
            grouped.setdefault(find(index), []).append(assignment)

        contents = []
        for group in grouped.values():
            self._sort_assignments(group)
            contents.append({
                "text": " ".join(item.text.text for item in group),
                "font_size": max(item.text.font_size for item in group),
                "assignments": group,
            })
        contents.sort(key=lambda group: self._geometric_order(group["assignments"][0]))
        return contents

    @staticmethod
    def _edge_distance(
        left: Any,
        right: Any,
        *,
        diagonal_factor: float = PARSER_CONFIG.text_matching.diagonal_distance_factor,
    ) -> float:
        """Return the nearest parallel bbox-edge distance with projection guards."""
        ax0, ay0, ax1, ay1 = coerce_bbox(left)
        bx0, by0, bx1, by1 = coerce_bbox(right)
        distances: list[float] = []
        if min(ay1, by1) >= max(ay0, by0):
            distances.extend(abs(a - b) for a in (ax0, ax1) for b in (bx0, bx1))
        if min(ax1, bx1) >= max(ax0, bx0):
            distances.extend(abs(a - b) for a in (ay0, ay1) for b in (by0, by1))
        if distances:
            return min(distances)
        return TextMatcher._bbox_gap(left, right) * diagonal_factor

    def _are_spatially_related(
        self,
        left: Text,
        right: Text,
        threshold: float,
    ) -> bool:
        ax0, ay0, ax1, ay1 = coerce_bbox(left.bbox)
        bx0, by0, bx1, by1 = coerce_bbox(right.bbox)
        if not self._directions_are_parallel(left, right):
            return False

        horizontal_layout = self._anchors_align(
            (ay0, ay1, (ay0 + ay1) / 2.0),
            (by0, by1, (by0 + by1) / 2.0),
        )
        vertical_layout = self._anchors_align(
            (ax0, ax1, (ax0 + ax1) / 2.0),
            (bx0, bx1, (bx0 + bx1) / 2.0),
        )
        if not (horizontal_layout or vertical_layout):
            return False
        return self._bbox_gap(left.bbox, right.bbox) <= threshold

    def _anchors_align(
        self,
        left: tuple[float, float, float],
        right: tuple[float, float, float],
    ) -> bool:
        """Return whether starts, ends, or centers share an alignment line."""
        return any(
            abs(left_anchor - right_anchor) <= self.alignment_tolerance
            for left_anchor, right_anchor in zip(left, right)
        )

    @staticmethod
    def _directions_are_parallel(left: Text, right: Text) -> bool:
        """Reject mixing horizontal and vertical text while allowing reversed baselines."""
        return TextMatcher._is_vertical(left) == TextMatcher._is_vertical(right)

    @staticmethod
    def _bbox_gap(left: Any, right: Any) -> float:
        ax0, ay0, ax1, ay1 = coerce_bbox(left)
        bx0, by0, bx1, by1 = coerce_bbox(right)
        dx = max(ax0 - bx1, bx0 - ax1, 0.0)
        dy = max(ay0 - by1, by0 - ay1, 0.0)
        return math.hypot(dx, dy)

    @staticmethod
    def _center_distance(left: Any, right: Any) -> float:
        ax0, ay0, ax1, ay1 = coerce_bbox(left)
        bx0, by0, bx1, by1 = coerce_bbox(right)
        return math.hypot(
            (ax0 + ax1 - bx0 - bx1) / 2.0,
            (ay0 + ay1 - by0 - by1) / 2.0,
        )

    @staticmethod
    def _target_area(target: dict[str, Any]) -> float:
        x0, y0, x1, y1 = coerce_bbox(target["bbox"])
        return max(x1 - x0, 0.0) * max(y1 - y0, 0.0)

    @staticmethod
    def _component_polygon(component: dict[str, Any]) -> BaseGeometry | None:
        polygon = component.get("attributes", {}).get("polygon")
        return TextMatcher._polygon_geometry(polygon)

    @staticmethod
    def _polygon_geometry(value: Any) -> BaseGeometry | None:
        polygon = value
        if isinstance(polygon, BaseGeometry) and not polygon.is_empty:
            return polygon
        if isinstance(polygon, dict):
            if "polygon" in polygon:
                return TextMatcher._polygon_geometry(polygon["polygon"])
            if polygon.get("type") == "Polygon" and polygon.get("coordinates"):
                return Polygon(polygon["coordinates"][0], polygon["coordinates"][1:])
            if "points" in polygon:
                return Polygon(polygon["points"])
            if "bbox" in polygon:
                return box(*coerce_bbox(polygon["bbox"]))
        if polygon is not None:
            try:
                values = list(polygon)
            except TypeError:
                return None
            if len(values) == 4 and all(isinstance(item, Real) for item in values):
                return box(*coerce_bbox(values))
            if values:
                return Polygon([(float(point[0]), float(point[1])) for point in values])
        return None

    @staticmethod
    def _same_pdf_line(left: Text, right: Text) -> bool:
        left_block = left.metadata.get("block_index")
        right_block = right.metadata.get("block_index")
        left_line = left.metadata.get("line_index")
        right_line = right.metadata.get("line_index")
        return (
            left_block is not None
            and left_line is not None
            and left_block == right_block
            and left_line == right_line
        )

    @classmethod
    def _sort_assignments(cls, assignments: list[_Assignment]) -> None:
        assignments.sort(key=cmp_to_key(cls._compare_reading_order))

    @classmethod
    def _compare_reading_order(cls, left: _Assignment, right: _Assignment) -> int:
        if cls._same_pdf_line(left.text, right.text):
            left_span = int(left.text.metadata.get("span_index", left.position))
            right_span = int(right.text.metadata.get("span_index", right.position))
            if left_span != right_span:
                return -1 if left_span < right_span else 1

        left_vertical = cls._is_vertical(left.text)
        right_vertical = cls._is_vertical(right.text)
        if left_vertical and right_vertical:
            left_x, left_y, _, _ = left.text.bbox
            right_x, right_y, _, _ = right.text.bbox
            left_key = (
                left_x,
                left_y if left.text.direction[1] >= 0 else -left_y,
                left.position,
            )
            right_key = (
                right_x,
                right_y if right.text.direction[1] >= 0 else -right_y,
                right.position,
            )
        else:
            left_x, left_y, _, _ = left.text.bbox
            right_x, right_y, _, _ = right.text.bbox
            left_key = (
                left_y,
                left_x if left.text.direction[0] >= 0 else -left_x,
                left.position,
            )
            right_key = (
                right_y,
                right_x if right.text.direction[0] >= 0 else -right_x,
                right.position,
            )
        return (left_key > right_key) - (left_key < right_key)

    @staticmethod
    def _is_vertical(text: Text) -> bool:
        return abs(text.direction[1]) > abs(text.direction[0])

    @staticmethod
    def _geometric_order(assignment: _Assignment) -> tuple[float, float, int]:
        x0, y0, _, _ = assignment.text.bbox
        return (y0, x0, assignment.position)

    @staticmethod
    def _text_index(text: Text, position: int) -> int:
        return text.source_index if text.source_index is not None else position
