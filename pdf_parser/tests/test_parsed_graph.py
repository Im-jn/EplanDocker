"""Tests for the SQLite node/edge graph built from parsed JSON."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from pdf_parser.parsed_graph import connect_parsed_graph, save_parsed_graph
from pdf_parser.parsed_json import build_parsed_json


def _entity(entity_id: int, entity_type: str, page: int, **extra) -> dict:
    return {
        "id": entity_id,
        "type": entity_type,
        "page": page,
        "bbox": {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0},
        "title": ["K1"] if entity_type == "component" else [],
        "descriptions": [],
        "elements": [0],
        **extra,
    }


def _parsed(filename: str = "plan.pdf") -> dict:
    return build_parsed_json({
        "document": {"filename": "source.pdf", "page_count": 2},
        "symbol_overview": {"symbols": [], "records": []},
        "pages": {
            1: {
                "page_type": "multi",
                "diagram": {
                    "components": [
                        _entity(0, "component", 1, subclass="symbol"),
                        _entity(1, "component", 1, subclass="arrow", page_io="output"),
                    ],
                    "endpoints": [_entity(0, "endpoint", 1)],
                    "wires": [_entity(0, "wire", 1)],
                    "nets": [_entity(0, "net", 1)],
                    "groups": [_entity(0, "group", 1)],
                    "relations": [
                        {"type": "connection", "source": "wire:0", "target": "endpoint:0"},
                        {"type": "connection", "source": "component:0", "target": "endpoint:0"},
                        {"type": "contains", "source": "net:0", "target": "wire:0"},
                        {"type": "contains", "source": "group:0", "target": "component:0"},
                        {"type": "contains", "source": "group:0", "target": "component:99"},
                    ],
                    "hyperlinks": [
                        {"source_page": 1, "source_component": 0, "target_page": 2, "target_component": 0,
                         "source_bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "target_bbox": None},
                        {"source_page": 1, "source_component": None, "target_page": 2},
                    ],
                    "transfers": [
                        {"source_page": 1, "source_component": 1, "target_page": 2, "target_component": 0},
                        {"source_page": 1, "source_component": 1, "target_page": 2},
                    ],
                },
            },
            2: {"page_type": "single", "diagram": {
                "components": [_entity(0, "component", 2, subclass="assembly")],
            }},
            3: {"page_type": "symbol_overview", "diagram": {"records": []}},
        },
    }, filename=filename)


def test_stores_entities_as_nodes_with_json_global_ids() -> None:
    parsed = _parsed()
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        assert save_parsed_graph(parsed, database) == {"nodes": 7, "edges": 6}

        with closing(connect_parsed_graph(database)) as connection:
            rows = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT id, filename, page, kind, x0, title, properties FROM nodes"
                )
            }
        json_ids = {
            entity["id"]
            for page in parsed["pages"].values()
            for collection in ("components", "endpoints", "wires", "nets", "groups")
            for entity in page["diagram"].get(collection, [])
        }
        assert set(rows) == json_ids
        assert rows["plan/p1/component/0"] == ("plan.pdf", 1, "component", 1.0, '["K1"]', "{}")
        assert rows["plan/p1/component/1"][5] == '{"page_io":"output"}'
        assert rows["plan/p2/component/0"][1:3] == (2, "component")


def test_nodes_store_component_subclass_and_kind_for_other_nodes() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(_parsed(), database)

        with closing(connect_parsed_graph(database)) as connection:
            subclasses = dict(connection.execute("SELECT id, subclass FROM nodes"))
    assert subclasses == {
        "plan/p1/component/0": "symbol",
        "plan/p1/component/1": "arrow",
        "plan/p2/component/0": "assembly",
        "plan/p1/endpoint/0": "endpoint",
        "plan/p1/wire/0": "wire",
        "plan/p1/net/0": "net",
        "plan/p1/group/0": "group",
    }


def test_adds_subclass_to_databases_created_without_it() -> None:
    import sqlite3

    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        with closing(sqlite3.connect(database)) as legacy, legacy:
            legacy.execute("CREATE TABLE documents (filename TEXT PRIMARY KEY, page_count INTEGER)")
            legacy.execute(
                "CREATE TABLE nodes (id TEXT PRIMARY KEY, filename TEXT NOT NULL, page INTEGER NOT NULL, "
                "kind TEXT NOT NULL, x0 REAL, y0 REAL, x1 REAL, y1 REAL, title TEXT NOT NULL, "
                "descriptions TEXT NOT NULL, properties TEXT NOT NULL)"
            )
            legacy.execute("INSERT INTO documents VALUES ('old.pdf', 1)")
            for node_id, kind in (("old/p1/component/0", "component"), ("old/p1/wire/0", "wire")):
                legacy.execute(
                    "INSERT INTO nodes VALUES (?, 'old.pdf', 1, ?, 0, 0, 1, 1, '[]', '[]', '{}')",
                    (node_id, kind),
                )

        with closing(connect_parsed_graph(database)) as connection:
            subclasses = dict(connection.execute("SELECT id, subclass FROM nodes"))
    assert subclasses == {"old/p1/component/0": None, "old/p1/wire/0": "wire"}


def test_stores_resolved_relations_and_links_as_edges_with_json_ids() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(_parsed(), database)

        with closing(connect_parsed_graph(database)) as connection:
            edges = set(connection.execute(
                "SELECT id, kind, source_id, target_id, source_page, target_page FROM edges"
            ))
        assert edges == {
            ("plan/p1/relation/0", "connection", "plan/p1/wire/0", "plan/p1/endpoint/0", 1, 1),
            ("plan/p1/relation/1", "connection", "plan/p1/component/0", "plan/p1/endpoint/0", 1, 1),
            ("plan/p1/relation/2", "contains", "plan/p1/net/0", "plan/p1/wire/0", 1, 1),
            ("plan/p1/relation/3", "contains", "plan/p1/group/0", "plan/p1/component/0", 1, 1),
            ("plan/p1/hyperlink/0", "hyperlink", "plan/p1/component/0", "plan/p2/component/0", 1, 2),
            ("plan/p1/transfer/0", "transfer", "plan/p1/component/1", "plan/p2/component/0", 1, 2),
        }


def test_reparse_replaces_only_the_same_document() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(_parsed("plan.pdf"), database)
        save_parsed_graph(_parsed("other.pdf"), database)
        save_parsed_graph(_parsed("plan.pdf"), database)

        with closing(connect_parsed_graph(database)) as connection:
            counts = dict(connection.execute(
                "SELECT filename, COUNT(*) FROM nodes GROUP BY filename"
            ).fetchall())
            edge_counts = dict(connection.execute(
                "SELECT filename, COUNT(*) FROM edges GROUP BY filename"
            ).fetchall())
        assert counts == {"plan.pdf": 7, "other.pdf": 7}
        assert edge_counts == {"plan.pdf": 6, "other.pdf": 6}


def test_export_writes_public_json_and_graph() -> None:
    from pdf_parser.diagram_pdf_parser import load_pdf_info
    from pdf_parser.parsed_graph import export_parsed_outputs

    pdf_info = {
        "document": {"filename": "source.pdf", "page_count": 1},
        "pages": {1: {"diagram": {"components": [_entity(0, "component", 1, elements=[0])]}}},
    }
    with TemporaryDirectory() as directory:
        root = Path(directory)
        json_path, counts = export_parsed_outputs(
            pdf_info,
            filename="plan.pdf",
            parsed_json_directory=root / "parsed_json",
            graph_path=root / "graph.sqlite3",
            document_id="doc_1",
        )

        assert json_path == (root / "parsed_json" / "plan.json").resolve()
        assert load_pdf_info(json_path)["pages"]["1"]["diagram"]["components"][0]["id"] == "plan/p1/component/0"
        assert counts == {"nodes": 1, "edges": 0}


def test_adds_document_id_to_databases_created_without_it() -> None:
    import sqlite3

    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        with closing(sqlite3.connect(database)) as legacy, legacy:
            legacy.execute("CREATE TABLE documents (filename TEXT PRIMARY KEY, page_count INTEGER)")
            legacy.execute("INSERT INTO documents VALUES ('old.pdf', 3)")

        save_parsed_graph(_parsed("plan.pdf"), database, document_id="doc_1")

        with closing(connect_parsed_graph(database)) as connection:
            documents = set(connection.execute(
                "SELECT filename, document_id, page_count FROM documents"
            ).fetchall())
        assert documents == {("old.pdf", None, 3), ("plan.pdf", "doc_1", 2)}


def test_document_id_links_rows_and_replaces_renamed_uploads() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "graph.sqlite3"
        save_parsed_graph(_parsed("plan.pdf"), database, document_id="doc_1")
        save_parsed_graph(_parsed("renamed.pdf"), database, document_id="doc_1")
        save_parsed_graph(_parsed("other.pdf"), database)

        with closing(connect_parsed_graph(database)) as connection:
            documents = set(connection.execute(
                "SELECT filename, document_id FROM documents"
            ).fetchall())
            filenames = {row[0] for row in connection.execute("SELECT filename FROM nodes")}
        assert documents == {("renamed.pdf", "doc_1"), ("other.pdf", None)}
        assert filenames == {"renamed.pdf", "other.pdf"}
