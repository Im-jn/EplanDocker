"""JSON persistence for document-scoped entity classifications."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from pdf_parser.pages_manager import TextBase, VectorBase
from pdf_parser.utils import resolve_repo_relative


STORE_VERSION = 1
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class EntityStore:
    """Persist classifications for entities belonging to one exact PDF file."""

    def __init__(
        self,
        pdf_path: str | Path,
        storage_directory: str | Path = "./storage/entity",
    ):
        self.pdf_path = resolve_repo_relative(str(pdf_path))
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")
        stat = self.pdf_path.stat()
        self.document_id = _document_sha256(
            str(self.pdf_path),
            int(stat.st_size),
            int(stat.st_mtime_ns),
        )
        self.storage_directory = resolve_repo_relative(str(storage_directory))
        self.path = self.storage_directory / f"{self.document_id}.json"
        self.seed_path = (
            Path(__file__).resolve().parent
            / "seeds"
            / f"{self.document_id}.json"
        )

    def get(
        self,
        page_number: int,
        entity_index: int,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return a cached classification only when the entity fingerprint agrees."""
        payload = self._load()
        record = payload["entities"].get(_entity_key(page_number, entity_index))
        if not isinstance(record, dict):
            return None
        if record.get("entity_fingerprint") != fingerprint:
            return None
        classification = record.get("classification")
        return dict(classification) if isinstance(classification, dict) else None

    def put(
        self,
        page_number: int,
        entity_index: int,
        fingerprint: str,
        classification: Mapping[str, Any],
        *,
        source: str,
        model: str | None,
    ) -> None:
        """Atomically add or replace one entity classification."""
        self.storage_directory.mkdir(parents=True, exist_ok=True)
        with _path_lock(self.path):
            payload = self._load()
            payload["entities"][_entity_key(page_number, entity_index)] = {
                "page_number": int(page_number),
                "entity_index": int(entity_index),
                "entity_fingerprint": str(fingerprint),
                "classification": dict(classification),
                "source": str(source),
                "model": str(model) if model else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save(payload)

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "document": {
                "sha256": self.document_id,
                "file_name": self.pdf_path.name,
            },
            "entities": {},
        }

    def _load(self) -> dict[str, Any]:
        source_path = self.path if self.path.is_file() else self.seed_path
        if not source_path.is_file():
            return self._empty_payload()
        try:
            with source_path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid entity store: {source_path}") from exc
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            raise ValueError(f"Unsupported entity store: {source_path}")
        document = payload.get("document")
        if not isinstance(document, dict) or document.get("sha256") != self.document_id:
            raise ValueError(f"Classification store document mismatch: {source_path}")
        if not isinstance(payload.get("entities"), dict):
            raise ValueError(
                f"Classification store entities must be an object: {source_path}"
            )
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.storage_directory,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            temporary_path.replace(self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def entity_fingerprint(vector_base: VectorBase, text_base: TextBase) -> str:
    """Return a stable hash of the entity geometry, style, and owned text."""
    vectors = []
    for vector in vector_base:
        vectors.append(
            {
                "type": vector.type,
                "points": [
                    [round(float(x), 6), round(float(y), 6)]
                    for x, y in vector.points
                ],
                "style": {
                    key: vector.path_meta_value(key)
                    for key in ("stroke_width", "color", "fill", "dashes")
                },
            }
        )
    texts = [
        {
            "text": text.text,
            "bbox": [round(float(value), 6) for value in text.bbox],
            "font_size": round(float(text.font_size), 6),
            "direction": [round(float(value), 6) for value in text.direction],
        }
        for text in text_base
    ]
    encoded = json.dumps(
        {"vectors": vectors, "texts": texts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=32)
def _document_sha256(path: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entity_key(page_number: int, entity_index: int) -> str:
    page_number = int(page_number)
    entity_index = int(entity_index)
    if page_number < 1 or entity_index < 1:
        raise ValueError("page_number and entity_index must be positive")
    return f"{page_number}:{entity_index}"


def _path_lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())
