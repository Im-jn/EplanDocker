"""Central defaults for PDF geometry and extraction behavior."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GeometryConfig:
    """Precision shared by algorithms that operate on the same topology."""

    topology_tolerance_pt: float = 0.01
    curve_segments: int = 16


@dataclass(frozen=True)
class TextMatchingConfig:
    """Page-wide text clustering and staged target matching."""

    proximity_threshold_pt: float = 6.0
    arrow_proximity_threshold_pt: float = 10.0
    endpoint_proximity_threshold_pt: float = 5.0
    wire_mark_proximity_threshold_pt: float = 7.0
    symbol_proximity_threshold_pt: float = 30.0
    endpoint_max_chars: int = 8
    endpoint_max_tokens: int = 2
    proximity_ambiguity_margin_pt: float = 1.5
    diagonal_distance_factor: float = 1.6
    cluster_distance_pt: float = 5.0
    merge_distance_pt: float = 5.0
    alignment_tolerance_pt: float = 2.0
    font_tolerance_pt: float = 0.2


@dataclass(frozen=True)
class DiagramConfig:
    """Wire, endpoint, and electrical-relation extraction."""

    endpoint_component_tolerance_pt: float = 10.0
    relation_point_tolerance_pt: float = 0.1
    wire_endpoint_fallback_margin_pt: float = 5.0
    box_adjacency_margin_pt: float = 1.0
    component_containment_ratio: float = 0.6
    wire_seed_min_length_pt: float = 8.0
    # EPLAN's nominal break mark is an 8.018 pt diagonal after conversion.
    wire_terminal_diagonal_max_length_pt: float = 10.0
    arrow_removal_boundary_tolerance_pt: float = 0.1


@dataclass(frozen=True)
class SplitPageConfig:
    """Page-frame detection and drawing-region extraction."""

    vector_extend_pt: float = 2.0
    min_content_inset_pt: float = 1.0
    page_frame_edge_tolerance_ratio: float = 0.04
    content_edge_vector_tolerance_pt: float = 2.0
    min_frame_vector_span_ratio: float = 0.35


@dataclass(frozen=True)
class SymbolConfig:
    """Symbol deduplication and overview-table parsing."""

    coordinate_tolerance_pt: float = 0.8
    min_boundary_coverage_ratio: float = 0.9


@dataclass(frozen=True)
class VectorGeometryConfig:
    """Vector entity merging and closed-region detection."""

    entity_containment_tolerance_pt: float = 1.0
    entity_nearby_merge_gap_pt: float = 8.0
    entity_min_containment_face_area_pt2: float = 100.0
    closed_edge_tolerance_pt: float = 0.75
    min_region_area_pt2: float = 1.0
    circle_min_circularity: float = 0.82
    circle_max_aspect_error: float = 0.25


@dataclass(frozen=True)
class VectorPinConfig:
    """Circle-pin, arrow, and wire-mark detection."""

    min_pin_size_pt: float = 1.0
    max_circle_diameter_pt: float = 12.0
    circle_size_absolute_tolerance_pt: float = 0.5
    circle_size_relative_tolerance: float = 0.08
    max_arrow_size_pt: float = 20.0
    min_arrow_area_pt2: float = 0.5
    point_tolerance_pt: float = 0.5
    isosceles_leg_error_ratio: float = 0.15
    equilateral_side_error_ratio: float = 0.1
    direction_axis_error_ratio: float = 0.2
    max_wire_mark_length_pt: float = 12.0
    wire_mark_endpoint_tolerance_pt: float = 0.5
    wire_mark_center_tolerance_pt: float = 0.35
    wire_mark_axis_tolerance_degrees: float = 5.0
    wire_mark_angle_cluster_tolerance_degrees: float = 3.0
    wire_mark_length_relative_tolerance: float = 0.08


@dataclass(frozen=True)
class VectorMatcherConfig:
    """Spatial queries and transformed vector-pattern matching."""

    query_slack_pt: float = 0.01
    anchor_adjacency_gap_pt: float = 0.75
    shape_tolerance_pt: float = 0.75
    scale_min: float = 0.5
    shape_scale_max: float = 2.0
    pattern_scale_max: float = 2.5
    missing_vector_ratio: float = 0.1
    symbol_overlap_ratio: float = 0.5
    rotations_degrees: tuple[int, ...] = (0, 90, 180, 270)


@dataclass(frozen=True)
class HyperlinkConfig:
    """Hyperlink recognition and component attachment."""

    min_iou: float = 0.45
    component_max_distance_pt: float = 70.0


@dataclass(frozen=True)
class ApiCacheConfig:
    """In-process frontend API cache sizes."""

    page_limit: int = 12
    entity_limit: int = 128
    result_limit: int = 256
    symbol_limit: int = 16


@dataclass(frozen=True)
class ParserConfig:
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    text_matching: TextMatchingConfig = field(default_factory=TextMatchingConfig)
    diagram: DiagramConfig = field(default_factory=DiagramConfig)
    split_page: SplitPageConfig = field(default_factory=SplitPageConfig)
    symbols: SymbolConfig = field(default_factory=SymbolConfig)
    vector_geometry: VectorGeometryConfig = field(default_factory=VectorGeometryConfig)
    vector_pin: VectorPinConfig = field(default_factory=VectorPinConfig)
    vector_matcher: VectorMatcherConfig = field(default_factory=VectorMatcherConfig)
    hyperlinks: HyperlinkConfig = field(default_factory=HyperlinkConfig)
    api_cache: ApiCacheConfig = field(default_factory=ApiCacheConfig)


PARSER_CONFIG = ParserConfig()
