from __future__ import annotations

import math

from pdf_parser.pages_manager import PathBase, VectorBase
from pdf_parser.tools.vector_index import SymbolVectorIndex
from pdf_parser.tools.vector_matcher import (
    ShapeMatch,
    VectorMatcher,
    _ShapeMatchTransformIndex,
)


def _transform(
    shape: PathBase,
    *,
    rotation: float,
    scale: float,
    translation: tuple[float, float],
    mirrored: bool = False,
) -> PathBase:
    angle = math.radians(rotation)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    points = []
    for x, y in shape.points:
        if mirrored:
            x = -x
        x *= scale
        y *= scale
        points.append((
            cosine * x - sine * y + translation[0],
            sine * x + cosine * y + translation[1],
        ))
    return PathBase(type=shape.type, points=points, path_meta=shape.inner_value.get("path_meta"))


def _match_keys(matches: list[dict[str, object]]) -> set[tuple[int, int, int, int]]:
    return {
        tuple(round(float(match["bbox_mupdf"][key]) * 1000) for key in ("x0", "y0", "x1", "y1"))
        for match in matches
    }


def test_line_index_folds_endpoint_direction_and_quarter_turns() -> None:
    line = PathBase(type="line", points=[(0, 0), (12, 3)])
    index = SymbolVectorIndex([[line]])

    for rotation in (0, 90, 180, 270):
        candidate = _transform(line, rotation=rotation, scale=1.4, translation=(80, 50))
        assert index.query(candidate)
        reversed_candidate = PathBase(type="line", points=list(reversed(candidate.points)))
        assert index.query(reversed_candidate)


def test_multi_point_index_supports_rotated_scaled_curves() -> None:
    curve = PathBase(type="curve", points=[(0, 0), (2, -4), (8, -3), (11, 1)])
    index = SymbolVectorIndex([[curve]])
    candidate = _transform(curve, rotation=270, scale=1.7, translation=(120, 75))
    assert index.query(candidate)


def test_indexed_catalog_matching_matches_legacy_results() -> None:
    symbols = [
        [
            PathBase(type="line", points=[(0, 0), (10, 2)]),
            PathBase(type="curve", points=[(18, 5), (20, 0), (25, 0), (28, 6)]),
        ],
        [
            PathBase(type="rect", points=[(0, 0), (8, 0), (8, 6), (0, 6)]),
            PathBase(type="line", points=[(14, 3), (22, 3)]),
        ],
    ]
    page_vectors: list[PathBase] = []
    for symbol, rotation, scale, translation in (
        (symbols[0], 90, 1.3, (100, 40)),
        (symbols[0], 270, 0.8, (220, 160)),
        (symbols[1], 180, 1.5, (360, 120)),
    ):
        page_vectors.extend(
            _transform(shape, rotation=rotation, scale=scale, translation=translation)
            for shape in symbol
        )
    page_vectors.append(PathBase(type="line", points=[(500, 500), (530, 507)]))

    matcher = VectorMatcher(VectorBase(page_vectors), page_height_pt=600)
    legacy = [matcher.match_pattern(symbol, missing_vector_ratio=0) for symbol in symbols]
    indexed = matcher.match_patterns(symbols, missing_vector_ratio=0)

    assert [_match_keys(matches) for matches in indexed] == [
        _match_keys(matches) for matches in legacy
    ]


def test_indexed_matching_supports_mirror_combined_with_rotation() -> None:
    symbol = [
        PathBase(type="curve", points=[(0, 0), (3, -7), (9, -2), (13, 4)]),
        PathBase(type="line", points=[(20, 3), (27, 8)]),
        PathBase(type="line", points=[(4, 16), (15, 13)]),
    ]
    candidate = [
        _transform(
            shape,
            rotation=90,
            scale=1.25,
            translation=(180, 90),
            mirrored=True,
        )
        for shape in symbol
    ]
    matcher = VectorMatcher(VectorBase(candidate), page_height_pt=400)

    assert matcher.match_patterns([symbol], allow_mirror=False, missing_vector_ratio=0) == [[]]
    matches = matcher.match_patterns([symbol], allow_mirror=True, missing_vector_ratio=0)[0]

    assert len(matches) == 1
    assert matches[0]["transform"]["mirrored"] is True
    assert round(matches[0]["transform"]["rotation_degrees"]) == 90


def test_symmetric_anchor_stops_after_first_successful_form() -> None:
    symbol = [
        PathBase(type="line", points=[(0, 0), (12, 4)]),
        PathBase(type="line", points=[(20, 10), (32, 14)]),
    ]
    candidates = [
        PathBase(type="line", points=[(50, 30), (62, 34)]),
        PathBase(type="line", points=[(70, 40), (82, 44)]),
    ]

    class CountingMatcher(VectorMatcher):
        comparisons = 0

        def compare_shape_groups(self, *args: object, **kwargs: object) -> bool:
            self.comparisons += 1
            return super().compare_shape_groups(*args, **kwargs)

    matcher = CountingMatcher(VectorBase(candidates), page_height_pt=100)
    matches = matcher.match_patterns([symbol], allow_mirror=True, missing_vector_ratio=0)[0]

    assert len(matches) == 1
    assert matcher.comparisons == 1


def test_any_two_of_three_anchors_can_seed_group_matching() -> None:
    symbol = [
        PathBase(type="line", points=[(0, 0), (8, 2)]),
        PathBase(type="line", points=[(20, 12), (25, 20)]),
        PathBase(type="line", points=[(45, 3), (52, 9)]),
    ]
    # The third selected anchor is absent, while the complete group matcher is
    # explicitly allowed to miss one of the three vectors.
    candidates = [
        _transform(shape, rotation=270, scale=1.2, translation=(140, 90))
        for shape in symbol[:2]
    ]
    matcher = VectorMatcher(VectorBase(candidates), page_height_pt=200)

    matches = matcher.match_patterns(
        [symbol],
        missing_vector_ratio=0.34,
    )[0]

    assert len(matches) == 1
    assert round(matches[0]["transform"]["rotation_degrees"]) == 270


def test_transform_index_never_drops_an_exactly_compatible_match() -> None:
    shape = PathBase(type="line", points=[(0, 0), (1, 0)])
    reference_point = (17.25, -9.5)
    matches = [
        ShapeMatch(
            vector=shape,
            rotation_degrees=rotation,
            scale=scale,
            translation=(x, y),
            max_error=0,
            mirrored=mirrored,
        )
        for rotation in (0, 90, 180, 270)
        for mirrored in (False, True)
        for scale in (0.998, 0.999999, 1.0, 1.001999, 1.004)
        for x in (-2.000001, -1.0, -0.000001, 0.999999, 2.0)
        for y in (-1.000001, 0.0, 0.999999)
    ]
    index = _ShapeMatchTransformIndex(
        matches,
        reference_point=reference_point,
        translation_tolerance=1.0,
        scale_tolerance=0.002,
    )

    for query in matches:
        expected = {
            id(candidate)
            for candidate in matches
            if VectorMatcher._translation_equality(
                query,
                candidate,
                reference_point=reference_point,
                translation_tolerance=1.0,
                scale_tolerance=0.002,
            )
        }
        recalled = {id(candidate) for candidate in index.candidates(query)}
        assert expected <= recalled
