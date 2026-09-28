"""Store parsed diagram entities and their links in a SQLite node/edge graph.

Nodes are the ``components``/``endpoints``/``wires``/``nets``/``groups`` of the
parsed JSON; edges are its ``relations``, ``transfers`` and ``hyperlinks``. Node
and edge ids are the global ids already assigned in the parsed JSON, so rows
and JSON records correspond one to one. Links or relations whose endpoints are
not stored nodes are skipped here; the parsed JSON keeps them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from pdf_parser.parsed_json import ENTITY_COLLECTIONS, LINK_COLLECTIONS


NODE_COLUMNS = {"id", "type", "page", "bbox", "title", "descriptions"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    filename TEXT PRIMARY KEY,
    page_count INTEGER
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL REFERENCES documents(filename) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    kind TEXT NOT NULL,
    x0 REAL,
    y0 REAL,
    x1 REAL,
    y1 REAL,
    title TEXT NOT NULL,
    descriptions TEXT NOT NULL,
    properties TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL REFERENCES documents(filename) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    source_page INTEGER NOT NULL,
    target_page INTEGER NOT NULL,
    properties TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_page ON nodes (filename, page, kind);
CREATE INDEX IF NOT EXISTS edges_source ON edges (source_id, kind);
CREATE INDEX IF NOT EXISTS edges_target ON edges (target_id, kind);
"""


def connect_parsed_graph(database_path: str | Path) -> sqlite3.Connection:
    """Open the graph database, creating its schema when needed."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    return connection


def save_parsed_graph(parsed: Mapping[str, Any], database_path: str | Path) -> dict[str, int]:
    """Replace one document's nodes and edges, keyed by its PDF filename."""
    document = parsed.get("document", {})
    filename = str(document["filename"])
    with closing(connect_parsed_graph(database_path)) as connection, connection:
        connection.execute("DELETE FROM documents WHERE filename = ?", (filename,))
        connection.execute(
            "INSERT INTO documents (filename, page_count) VALUES (?, ?)",
            (filename, document.get("page_count")),
        )
        node_ids: set[str] = set()
        for page, kind, entity in _nodes(parsed):
            bbox = entity.get("bbox") or {}
            connection.execute(
                "INSERT INTO nodes (id, filename, page, kind, x0, y0, x1, y1, "
                "title, descriptions, properties) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity["id"],
                    filename,
                    page,
                    kind,
                    bbox.get("x0"),
                    bbox.get("y0"),
                    bbox.get("x1"),
                    bbox.get("y1"),
                    _json(entity.get("title", [])),
                    _json(entity.get("descriptions", [])),
                    _json({key: value for key, value in entity.items() if key not in NODE_COLUMNS}),
                ),
            )
            node_ids.add(entity["id"])

        edge_count = 0
        for edge in _edges(parsed):
            if edge[2] not in node_ids or edge[3] not in node_ids:
                continue
            connection.execute(
                "INSERT INTO edges (id, kind, source_id, target_id, source_page, "
                "target_page, properties, filename) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*edge, filename),
            )
            edge_count += 1
    return {"nodes": len(node_ids), "edges": edge_count}


def _nodes(parsed: Mapping[str, Any]) -> Iterator[tuple[int, str, dict[str, Any]]]:
    for page, diagram in _diagrams(parsed):
        for collection, kind in ENTITY_COLLECTIONS.items():
            for entity in diagram.get(collection, []):
                yield page, kind, entity


def _edges(parsed: Mapping[str, Any]) -> Iterator[tuple[str, str, Any, Any, int, int, str]]:
    """Yield ``(id, kind, source_id, target_id, source_page, target_page, properties)``."""
    for page, diagram in _diagrams(parsed):
        for relation in diagram.get("relations", []):
            yield (
                relation["id"],
                str(relation["type"]),
                relation.get("source"),
                relation.get("target"),
                page,
                page,
                _json({}),
            )
        for collection, kind in LINK_COLLECTIONS.items():
            for link in diagram.get(collection, []):
                yield (
                    link["id"],
                    kind,
                    link.get("source_component"),
                    link.get("target_component"),
                    int(link["source_page"]),
                    int(link["target_page"]),
                    _json({
                        "source_bbox": link.get("source_bbox"),
                        "target_bbox": link.get("target_bbox"),
                    }),
                )


def _diagrams(parsed: Mapping[str, Any]) -> Iterator[tuple[int, Mapping[str, Any]]]:
    for page_key, page_record in parsed.get("pages", {}).items():
        diagram = page_record.get("diagram") if isinstance(page_record, Mapping) else None
        if isinstance(diagram, Mapping):
            yield int(page_key), diagram


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
