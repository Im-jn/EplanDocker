from pdf_parser.processors.page_classifier import classify_info_texts


def test_classifies_symbol_overview_across_text_spans() -> None:
    assert (
        classify_info_texts([{"text": "Symbol"}, {"text": "Overview"}])
        == "symbol_overview"
    )
    assert classify_info_texts([{"text": "Symbol Overview1"}]) == "symbol_overview"
    assert classify_info_texts(["<td>Symbol Overview2</td>"]) == "symbol_overview"


def test_classifies_supported_diagram_titles() -> None:
    assert classify_info_texts([{"text": "&SINGLE"}]) == "single"
    assert classify_info_texts([{"text": "&MULTI"}]) == "multi"
    assert classify_info_texts([{"text": "&PLC_IO"}]) == "plc_io"


def test_leaves_unsupported_page_type_unclassified() -> None:
    assert classify_info_texts([{"text": "Cover page"}]) is None
