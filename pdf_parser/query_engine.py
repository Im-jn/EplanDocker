"""Transport-independent query entry point used by the API service.

The legacy implementation still lives in :mod:`pdf_parser.vector_api` while it
is decomposed incrementally. HTTP and process management no longer depend on
that module's stdin/stdout server.
"""

from __future__ import annotations

from typing import Any

from pdf_parser.vector_api import handle as _legacy_handle


def query(payload: dict[str, Any]) -> dict[str, Any]:
    return _legacy_handle(payload)
