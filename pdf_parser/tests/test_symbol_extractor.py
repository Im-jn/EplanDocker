"""Synthetic regression tests for Symbol Overview extraction."""

from __future__ import annotations

from pdf_parser.processors.symbol_extractor import extract_symbols, merge_symbol_results
from pdf_parser.pages_manager import PathBase, TextBase, VectorBase


def _line(x0: float, y0: float, x1: float, y1: float) -> PathBase:
    return PathBase(type="line", points=[(x0, y0), (x1, y1)])


def _rect(x0: float, y0: float, x1: float, y1: float, *, code: str) -> PathBase:
    return PathBase(
        type="rect",
        code=code,
        points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
    )


def _text(value: str, x: float, y: float) -> dict[str, object]:
    return {"text": value, "location": (x, y, x + 8, y + 5)}


def run_test() -> None:
    # The header merges the first two columns. A frequency-based boundary
    # detector used to merge the header and first data row as a result.
    table = [
        _line(0, 0, 100, 0),
        _line(0, 10, 100, 10),
        _line(0, 100, 100, 100),
        _line(0, 0, 0, 100),
        _line(50, 0, 50, 100),
        _line(100, 0, 100, 100),
        _line(30, 10, 30, 100),
    ]
    symbol = _rect(35, 35, 45, 55, code="symbol")
    texts = TextBase([
        _text("Type / Symbol", 8, 2),
        _text("Description", 65, 2),
        _text("A", 8, 40),
        _text("PLC box", 65, 40),
    ])

    result = extract_symbols(VectorBase([*table, symbol]), texts)
    records = result["records"]

    assert result["symbols"] == [[symbol]]
    assert len(records) == 1
    assert records[0]["symbol"] == 0
    assert records[0]["type"] == "A"
    assert records[0]["descriptions"] == "PLC box"

    _run_split_cell_test()
    _run_merge_results_test()


def _run_split_cell_test() -> None:
    table = [
        _line(0, 0, 100, 0),
        _line(0, 10, 100, 10),
        _line(0, 55, 100, 55),
        _line(0, 100, 100, 100),
        _line(0, 0, 0, 100),
        _line(50, 0, 50, 100),
        _line(100, 0, 100, 100),
        _line(30, 10, 30, 100),
    ]
    horizontal_splitter = _line(30, 32.5, 50, 32.5)
    vertical_splitter = _line(75, 10, 75, 55)
    top_symbol = _rect(35, 15, 45, 27, code="top-symbol")
    bottom_symbol = _rect(35, 38, 45, 50, code="bottom-symbol")
    second_symbol = _rect(35, 70, 45, 85, code="second-symbol")
    texts = TextBase([
        _text("Type / Symbol", 8, 2),
        _text("Description", 65, 2),
        _text("A", 8, 25),
        _text("Left", 58, 25),
        _text("Right", 82, 25),
        _text("B", 8, 72),
        _text("Base", 65, 72),
    ])

    result = extract_symbols(
        VectorBase([
            *table,
            horizontal_splitter,
            vertical_splitter,
            top_symbol,
            bottom_symbol,
            second_symbol,
        ]),
        texts,
    )
    records = result["records"]

    assert result["symbols"] == [[top_symbol], [second_symbol]]
    assert [record["symbol"] for record in records] == [0, 0, 0, 0, 1]
    assert [record["type"] for record in records] == ["A", "A", "A", "A", "B"]
    assert [record["descriptions"] for record in records] == [
        "Left",
        "Right",
        "Left",
        "Right",
        "Base",
    ]


def _run_merge_results_test() -> None:
    first_symbol = _rect(0, 0, 10, 20, code="first")
    translated_copy = _rect(40, 30, 50, 50, code="translated-copy")
    distinct_symbol = _rect(60, 30, 75, 50, code="distinct")
    first = {
        "symbols": [[first_symbol]],
        "records": [{"symbol": 0, "type": "A", "descriptions": "First"}],
    }
    second = {
        "symbols": [[translated_copy], [distinct_symbol]],
        "records": [
            {"symbol": 0, "type": "B", "descriptions": "Duplicate"},
            {"symbol": 1, "type": "C", "descriptions": "Distinct"},
        ],
    }

    merged = merge_symbol_results([first, second])

    assert merged["symbols"] == [[first_symbol], [distinct_symbol]]
    assert [record["symbol"] for record in merged["records"]] == [0, 0, 1]
    assert [record["type"] for record in merged["records"]] == ["A", "B", "C"]


if __name__ == "__main__":
    run_test()
