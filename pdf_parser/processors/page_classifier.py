"""Classify page roles from title-block text in the information region."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


SYMBOL_OVERVIEW = "symbol_overview"
SINGLE = "single"
MULTI = "multi"
PLC_IO = "plc_io"
DIAGRAM_TYPES = (SINGLE, MULTI, PLC_IO)


def classify_info_texts(texts: Iterable[Any]) -> str | None:
    """Return the supported page type encoded by info-region text spans."""
    normalized = " ".join(
        value
        for value in (_normalize(_text_value(text)) for text in texts)
        if value
    )
    if not normalized:
        return None

    if re.search(r"\bSYMBOL\s+OVERVIEW", normalized):
        return SYMBOL_OVERVIEW
    if re.search(r"\bPLC\s+IO\b", normalized):
        return PLC_IO
    if re.search(r"\bMULTI\b", normalized):
        return MULTI
    if re.search(r"\bSINGLE\b", normalized):
        return SINGLE
    return None


def _text_value(text: Any) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, dict):
        value = text.get("text", "")
    else:
        value = getattr(text, "text", "")
    if isinstance(value, dict):
        value = value.get("content", "")
    return str(value)


def _normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()
