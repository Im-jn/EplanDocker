from pathlib import Path
from tempfile import TemporaryDirectory

import fitz

from pdf_parser.pages_manager import PdfPageManager


def test_goto_region_extracts_only_intersecting_page_content() -> None:
    with TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "regions.pdf"
        document = fitz.open()
        page = document.new_page(width=200, height=200)
        page.draw_line((10, 10), (80, 10))
        page.draw_line((120, 120), (180, 120))
        page.insert_text((20, 30), "inside")
        page.insert_text((130, 150), "outside")
        document.save(pdf_path)
        document.close()

        with PdfPageManager(pdf_path) as pages:
            page_data = pages.goto_region(1, (0, 0, 100, 100))

        assert [text.text for text in page_data["texts"]] == ["inside"]
        assert len(page_data["vectors"]) == 1
        assert page_data["links"] == []
