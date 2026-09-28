"""Find a component's upstream or downstream arrows within its own page.

Arrow components (``subclass == "arrow"``) mark where a page's circuits enter or
leave: ``page_io == "input"`` arrows are upstream ends and ``"output"`` arrows
are downstream ends. From a starting component, a breadth-first search over the
page's own ``connection``/``contains`` edges finds the shortest path to each
matching arrow; the components along those paths are the related upstream or
downstream components on this page.

Groups are not traversed because they only frame components and do not connect
them electrically. Arrows end a path: a path may reach one but never continues
through it, since an arrow is a page boundary.

:class:`PageGraph` holds these rules so directional tracing applies the same
ones to every hop.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pdf_parser.parsed_graph import connect_parsed_graph


Direction = Literal["upstream", "downstream"]
ARROW_IO = {"upstream": "input", "downstream": "output"}


class PageStreamStartError(ValueError):
    """Raised when the start id is missing or is not a component."""


class PageGraph:
    """One page's nodes and in-page ``connection``/``contains`` edges."""

    def __init__(self, connection: sqlite3.Connection, filename: str, page: int) -> None:
        self.filename = filename
        self.page = page
        self.nodes = {
            row[0]: {
                "id": row[0],
                "kind": row[1],
                "subclass": row[2],
                "title": json.loads(row[3]),
                "descriptions": json.loads(row[4]),
                **json.loads(row[5]),
            }
            for row in connection.execute(
                "SELECT id, kind, subclass, title, descriptions, properties "
                "FROM nodes WHERE filename = ? AND page = ?",
                (filename, page),
            )
        }
        self.edges: dict[str, dict[str, Any]] = {}
        self.adjacency: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in self.nodes}
        for edge_id, kind, source, target, properties in connection.execute(
            "SELECT id, kind, source_id, target_id, properties FROM edges "
            "WHERE filename = ? AND source_page = ? AND target_page = ? "
            "AND kind IN ('connection', 'contains')",
            (filename, page, page),
        ):
            if source not in self.adjacency or target not in self.adjacency:
                continue
            self.edges[edge_id] = {
                "id": edge_id,
                "type": kind,
                "source": source,
                "target": target,
                "source_page": page,
                "target_page": page,
                **json.loads(properties),
            }
            self.adjacency[source].append((edge_id, target))
            self.adjacency[target].append((edge_id, source))
        for neighbors in self.adjacency.values():
            neighbors.sort(key=lambda item: item[1])
        self._searches: dict[str, tuple[dict[str, str | None], dict[str, int]]] = {}

    def is_arrow(self, node_id: str) -> bool:
        return self.nodes[node_id]["subclass"] == "arrow"

    def arrows(self, page_io: str) -> list[str]:
        """Return this page's arrow components with the given ``page_io``."""
        return sorted(
            node_id
            for node_id, node in self.nodes.items()
            if node["kind"] == "component" and self.is_arrow(node_id) and node.get("page_io") == page_io
        )

    def distances(self, source: str) -> dict[str, int]:
        """Edge distances from ``source`` under the page stream rules."""
        return self._search(source)[1]

    def shortest_path_tree(self, source: str) -> dict[str, str | None]:
        """Breadth-first parents from ``source`` under the page stream rules."""
        return self._search(source)[0]

    def _search(self, source: str) -> tuple[dict[str, str | None], dict[str, int]]:
        """Cached BFS that skips groups and never expands an arrow other than ``source``."""
        if source not in self._searches:
            parents: dict[str, str | None] = {source: None}
            distances = {source: 0}
            queue = deque([source])
            while queue:
                node_id = queue.popleft()
                if node_id != source and self.is_arrow(node_id):
                    continue
                for _, neighbor in self.adjacency[node_id]:
                    if neighbor not in parents and self.nodes[neighbor]["kind"] != "group":
                        parents[neighbor] = node_id
                        distances[neighbor] = distances[node_id] + 1
                        queue.append(neighbor)
            self._searches[source] = (parents, distances)
        return self._searches[source]


def trace_page_stream(
    database_path: str | Path,
    component_id: str,
    direction: Direction,
) -> dict[str, Any]:
    """Return the shortest in-page path from ``component_id`` to each arrow of ``direction``."""
    if direction not in ARROW_IO:
        raise ValueError("direction must be upstream or downstream")

    with closing(connect_parsed_graph(database_path)) as connection:
        start = connection.execute(
            "SELECT filename, page, kind FROM nodes WHERE id = ?",
            (component_id,),
        ).fetchone()
        if start is None:
            raise PageStreamStartError(f"{component_id} was not found")
        filename, page, kind = start
        if kind != "component":
            raise PageStreamStartError(f"{component_id} is a {kind}, not a component")
        graph = PageGraph(connection, filename, page)

    arrow_io = ARROW_IO[direction]
    parents = graph.shortest_path_tree(component_id)
    arrows = []
    related: set[str] = set()
    unreachable = []
    for target in graph.arrows(arrow_io):
        if target == component_id:
            continue
        if target not in parents:
            unreachable.append(target)
            continue
        path = _path_to(target, parents)
        via = [
            node_id for node_id in path[1:-1]
            if graph.nodes[node_id]["kind"] == "component"
        ]
        related.update(via)
        arrows.append({
            **_summary(graph.nodes[target]),
            "distance": len(path) - 1,
            "path": path,
            "components": via,
        })

    return {
        "file": filename,
        "page": page,
        "start": _summary(graph.nodes[component_id]),
        "direction": direction,
        "arrow_io": arrow_io,
        "arrows": sorted(arrows, key=lambda arrow: (arrow["distance"], arrow["id"])),
        "unreachable_arrows": unreachable,
        "components": [_summary(graph.nodes[node_id]) for node_id in _sorted_ids(related)],
    }


def page_arrow_distances(database_path: str | Path, file: str, page: int) -> dict[str, Any]:
    """Return one page's arrows and every node's distance to each of them.

    Nodes and arrows are ``kind:page-local id`` references so a client holding
    the page-local trace index can decide per hop which nodes head up- or
    downstream: a node is closer to an arrow when its distance is smaller.
    """
    stem = Path(file).stem
    with closing(connect_parsed_graph(database_path)) as connection:
        filename = next(
            (
                name
                for (name,) in connection.execute("SELECT filename FROM documents")
                if Path(name).stem == stem
            ),
            None,
        )
        if filename is None:
            raise LookupError(f"No parsed graph data for {file}")
        graph = PageGraph(connection, filename, int(page))

    def local_ref(node_id: str) -> str:
        kind, local = node_id.rsplit("/", 2)[-2:]
        return f"{kind}:{local}"

    arrows = {io: graph.arrows(io) for io in ARROW_IO.values()}
    return {
        "file": filename,
        "page": int(page),
        "arrows": {io: [local_ref(arrow) for arrow in ids] for io, ids in arrows.items()},
        "distances": {
            local_ref(arrow): {
                local_ref(node_id): distance
                for node_id, distance in graph.distances(arrow).items()
            }
            for ids in arrows.values()
            for arrow in ids
        },
    }


def _path_to(target: str, parents: dict[str, str | None]) -> list[str]:
    path = [target]
    while parents[path[-1]] is not None:
        path.append(parents[path[-1]])
    return path[::-1]


def _summary(node: dict[str, Any]) -> dict[str, Any]:
    return {
        key: node[key]
        for key in ("id", "subclass", "title", "descriptions", "page_io")
        if key in node
    }


def _sorted_ids(ids: Iterable[str]) -> list[str]:
    def key(node_id: str) -> tuple[str, int, str]:
        kind, local = node_id.rsplit("/", 2)[-2:]
        return (kind, int(local) if local.isdigit() else -1, local)

    return sorted(ids, key=key)
