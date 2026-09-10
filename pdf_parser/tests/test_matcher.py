"""Synthetic regression test for VectorMatcher pattern matching.

Run from the repository root:

    python -m pdf_parser.tests.test_matcher
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Sequence

from pdf_parser.pages_manager import PathBase, TextBase, VectorBase
from pdf_parser.tools.vector_matcher import ShapeMatch, VectorMatcher
from pdf_parser.tools.vector_visualize import render_vector_text_png


Point = tuple[float, float]

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "storage" / "output" / "test_matcher"
PAGE_HEIGHT_PT = 500.0


def _line(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    code: str,
    dashed: bool = False,
) -> PathBase:
    path_meta = {"dashes": "[3 2] 0"} if dashed else {"dashes": "[] 0"}
    return PathBase(type="line", code=code, points=[(x0, y0), (x1, y1)], path_meta=path_meta)


def _rect(x0: float, y0: float, x1: float, y1: float, *, code: str) -> PathBase:
    return PathBase(
        type="rect",
        code=code,
        points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
    )


def _curve(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    x3: float,
    y3: float,
    *,
    code: str,
) -> PathBase:
    return PathBase(type="curve", code=code, points=[(x0, y0), (x1, y1), (x2, y2), (x3, y3)])


def _target_component() -> list[PathBase]:
    """Build a small asymmetric component with body, pins, terminals, and marks."""
    return [
        _rect(20, 20, 80, 70, code="body"),
        _line(10, 35, 20, 35, code="left-pin-top"),
        _line(10, 55, 20, 55, code="left-pin-bottom"),
        _line(80, 45, 95, 45, code="right-pin"),
        _line(50, 70, 50, 88, code="bottom-pin"),
        _line(35, 32, 65, 58, code="diagonal-a"),
        _line(65, 32, 35, 58, code="diagonal-b"),
        _curve(26, 44, 34, 28, 48, 28, 56, 44, code="upper-arc"),
        _curve(44, 48, 52, 64, 66, 64, 74, 48, code="lower-arc"),
        _rect(28, 25, 38, 35, code="terminal-a"),
        _rect(62, 55, 72, 65, code="terminal-b"),
        _line(48, 20, 48, 12, code="top-mark"),
    ]


def _transform_point(
    point: Point,
    *,
    rotation_degrees: float,
    scale: float,
    translation: Point,
) -> Point:
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    x = point[0] * scale
    y = point[1] * scale
    return (
        cos_a * x - sin_a * y + translation[0],
        sin_a * x + cos_a * y + translation[1],
    )


def _transform_shapes(
    shapes: Sequence[PathBase],
    *,
    rotation_degrees: float = 0.0,
    scale: float = 1.0,
    translation: Point = (0.0, 0.0),
    label: str,
) -> list[PathBase]:
    transformed: list[PathBase] = []
    for shape in shapes:
        inner = shape.inner_value
        transformed.append(
            PathBase(
                type=shape.type,
                code=f"{label}:{inner.get('code', '')}",
                points=[
                    _transform_point(
                        point,
                        rotation_degrees=rotation_degrees,
                        scale=scale,
                        translation=translation,
                    )
                    for point in shape.points
                ],
                path_meta={"group": label},
            )
        )
    return transformed


def _text(label: str, x: float, y: float) -> dict[str, object]:
    return {
        "text": label,
        "location": (x, y, x + 120, y + 12),
        "font_size": 8,
    }


def _build_candidate_base(target_shapes: Sequence[PathBase]) -> tuple[VectorBase, TextBase]:
    groups: list[tuple[str, list[PathBase], tuple[float, float]]] = []
    groups.append(
        (
            "same",
            _transform_shapes(target_shapes, translation=(140, 30), label="same"),
            (138, 18),
        )
    )
    groups.append(
        (
            "rot+90",
            _transform_shapes(target_shapes, rotation_degrees=90, translation=(360, 45), label="rot+90"),
            (288, 133),
        )
    )
    groups.append(
        (
            "rot-90",
            _transform_shapes(target_shapes, rotation_degrees=270, translation=(160, 310), label="rot-90"),
            (160, 190),
        )
    )
    groups.append(
        (
            "rot+scale",
            _transform_shapes(
                target_shapes,
                rotation_degrees=90,
                scale=1.35,
                translation=(520, 40),
                label="rot+scale",
            ),
            (400, 160),
        )
    )
    groups.append(
        (
            "missing-part",
            _transform_shapes(target_shapes[:-2], translation=(340, 260), label="missing-part"),
            (338, 248),
        )
    )
    extra_part = [_line(25, 65, 75, 65, code="extra-internal-link")]
    groups.append(
        (
            "extra-part",
            _transform_shapes([*target_shapes, *extra_part], translation=(500, 260), label="extra-part"),
            (498, 248),
        )
    )

    vectors = [shape for _, shapes, _ in groups for shape in shapes]
    texts = [_text(label, *label_origin) for label, _, label_origin in groups]
    return VectorBase(vectors), TextBase(texts)


def _matched_indices(records: Iterable[dict[str, object]]) -> list[int]:
    return sorted(int(record["primary_anchor"]["matched_index"]) for record in records)


def run_test() -> None:
    target_shapes = _target_component()
    target_base = VectorBase(target_shapes)
    target_text = TextBase([_text("target", 18, 4)])
    candidate_base, candidate_text = _build_candidate_base(target_shapes)

    target_png = render_vector_text_png(
        target_base,
        target_text,
        output_dir=OUTPUT_DIR,
        filename="target_component.png",
    )
    candidate_png = render_vector_text_png(
        candidate_base,
        candidate_text,
        output_dir=OUTPUT_DIR,
        filename="candidate_components.png",
    )

    matcher = VectorMatcher(candidate_base, page_height_pt=PAGE_HEIGHT_PT)
    matches = matcher.match_pattern(
        target_shapes,
        tolerance=0.75,
        scale_range=(0.5, 2.0),
        rotation_degrees=(0, 90, 180, 270),
        include_debug=False,
    )

    print(f"target image: {target_png}")
    print(f"candidate image: {candidate_png}")
    print(f"matched groups: {len(matches)}")
    for index, match in enumerate(matches, start=1):
        transform = match["transform"]
        print(
            f"{index}. anchor={match['primary_anchor']['matched_index']} "
            f"rotation={transform['rotation_degrees']} "
            f"scale={transform['scale']:.3f} "
            f"shape_count={match['shape_count']} "
            f"bbox={match['bbox_mupdf']}"
        )

    assert len(matches) == 5, "vectors inside the target bbox must not invalidate a match"
    assert _matched_indices(matches) == [1, 13, 25, 37, 59]

    solid = _line(0, 0, 10, 0, code="solid")
    dashed = _line(20, 0, 30, 0, code="dashed", dashed=True)
    style_matcher = VectorMatcher(VectorBase([solid, dashed]), page_height_pt=PAGE_HEIGHT_PT)
    solid_matches = style_matcher.match_shape(solid)
    assert {match.vector for match in solid_matches} == {solid}
    assert {round(match.rotation_degrees) for match in solid_matches} == {0, 180}

    # Compare transforms where the target lives, rather than rounding or
    # comparing translations at the distant global origin.
    curve_transform = ShapeMatch(
        vector=solid,
        rotation_degrees=0.0,
        scale=0.9985799041236226,
        translation=(290.15855273172826, -89.92170640111797),
        max_error=0.0,
    )
    line_transform = ShapeMatch(
        vector=solid,
        rotation_degrees=0.0,
        scale=1.0000086135611908,
        translation=(289.1238031187552, -90.7137282076061),
        max_error=0.0,
    )
    assert abs(curve_transform.translation[0] - line_transform.translation[0]) > 1.0
    assert style_matcher._translation_equality(
        curve_transform,
        line_transform,
        reference_point=(720.0, 551.0),
        translation_tolerance=1.0,
        scale_tolerance=0.002,
    )

    ambiguous_target = [
        _line(0, 0, 10, 0, code="anchor-a"),
        _line(20, 10, 25, 10, code="anchor-b"),
    ]
    ambiguous_candidate = [
        _line(90, 100, 100, 100, code="candidate-a"),
        _line(75, 90, 80, 90, code="candidate-b"),
    ]
    ambiguous_matcher = VectorMatcher(VectorBase(ambiguous_candidate), page_height_pt=PAGE_HEIGHT_PT)
    ambiguous_matches = ambiguous_matcher.match_pattern(
        ambiguous_target,
        missing_vector_ratio=0,
    )
    assert len(ambiguous_matches) == 1
    assert round(ambiguous_matches[0]["transform"]["rotation_degrees"]) == 180

    target_style_group = [
        _line(0, 0, 10, 0, code="top", dashed=True),
        _line(0, 10, 10, 10, code="bottom"),
    ]
    swapped_style_group = [
        _line(0, 0, 10, 0, code="top"),
        _line(0, 10, 10, 10, code="bottom", dashed=True),
    ]
    assert not style_matcher.compare_shape_groups(
        target_style_group,
        swapped_style_group,
        rotation_degrees=0,
        scale=1,
        translation=(0, 0),
    )

    ratio_targets = [
        _line(0, index * 2, 10, index * 2, code=f"target-{index}")
        for index in range(20)
    ]
    ratio_candidates = _transform_shapes(
        ratio_targets[:-1],
        translation=(100, 0),
        label="missing-one",
    )
    assert style_matcher.compare_shape_groups(
        ratio_targets,
        ratio_candidates,
        rotation_degrees=0,
        scale=1,
        translation=(100, 0),
        missing_vector_ratio=0.05,
    )
    assert not style_matcher.compare_shape_groups(
        ratio_targets,
        ratio_candidates,
        rotation_degrees=0,
        scale=1,
        translation=(100, 0),
        missing_vector_ratio=0.049,
    )

    extra_candidates = [
        *ratio_targets,
        *[
            _line(index / 4, 0.5, index / 4, 37.5, code=f"extra-{index}")
            for index in range(40)
        ],
    ]
    assert style_matcher.compare_shape_groups(
        ratio_targets,
        extra_candidates,
        rotation_degrees=0,
        scale=1,
        translation=(0, 0),
    )


if __name__ == "__main__":
    run_test()
