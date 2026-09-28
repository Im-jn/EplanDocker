"""Document-level n-hop tracing over the parsed graph database.

A hop is counted when the trace arrives at a node whose kind is not skipped.
Arriving at a skipped kind (``endpoint``/``net``/``group``/``wire``) is free, so
the trace keeps expanding through it until it reaches counted nodes, and the hop
ends there. Transfers are edges between components and therefore cost one hop.
Every node is reached at most once, which keeps loops from being re-expanded.

With ``direction`` set to ``upstream`` or ``downstream``, every hop also has to
head toward the page's input or output arrows; see :class:`_DirectedTracer`.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pdf_parser.page_stream import ARROW_IO, PageGraph
from pdf_parser.parsed_graph import connect_parsed_graph
from pdf_parser.parsed_json import global_id, id_kind, id_local_part


ResponseFormat = Literal["subgraph", "hops"]
TraceDirection = Literal["any", "upstream", "downstream"]
SKIPPABLE_KINDS = frozenset({"endpoint", "net", "group", "wire"})
DEFAULT_SKIP = ("endpoint", "net", "group")
NODE_KINDS = ("component", "wire", "endpoint", "net", "group")
TRACE_EDGE_KINDS = ("connection", "contains", "transfer")
_QUERY_CHUNK = 400


class TraceDocumentNotFoundError(LookupError):
    """Raised when the document has no rows in the parsed graph database."""


class TraceStartNotFoundError(ValueError):
    """Raised when the requested starting node is absent from the document."""


def trace_document(
    database_path: str | Path,
    *,
    start_id: str | None = None,
    file: str | None = None,
    start_page: int | None = None,
    start_kind: str | None = None,
    start_local_id: int | str | None = None,
    max_hops: int,
    skip: Iterable[str] = DEFAULT_SKIP,
    direction: TraceDirection = "any",
    response_format: ResponseFormat = "subgraph",
) -> dict[str, Any]:
    """Trace from a global node id, or from the uploaded PDF ``file`` name with
    ``start_page``/``start_kind``/``start_local_id``.

    Callers only need the filename they submitted, never an internal document id.
    ``direction`` other than ``any`` keeps every hop heading up- or downstream.
    """
    skipped = frozenset(skip)
    if direction not in {"any", *ARROW_IO}:
        raise ValueError("direction must be any, upstream or downstream")
    if max_hops < 0:
        raise ValueError("max_hops must be non-negative")
    if not skipped <= SKIPPABLE_KINDS:
        raise ValueError(f"skip may only contain {sorted(SKIPPABLE_KINDS)}")
    if response_format not in {"subgraph", "hops"}:
        raise ValueError("response_format must be subgraph or hops")
    if start_id is None:
        if file is None or start_page is None or start_kind is None or start_local_id is None:
            raise ValueError("start needs a global id, or file, page, kind and id")
        start_id = global_id(file, start_page, start_kind, start_local_id)

    with closing(connect_parsed_graph(database_path)) as connection:
        row = connection.execute("SELECT filename FROM nodes WHERE id = ?", (start_id,)).fetchone()
        if row is None:
            document = start_id.rsplit("/", 3)[0]
            known = {Path(name).stem for (name,) in connection.execute("SELECT filename FROM documents")}
            if document not in known:
                raise TraceDocumentNotFoundError(f"No parsed graph data for {document}")
            raise TraceStartNotFoundError(f"{start_id} was not found")
        filename = str(row[0])

        tracer = _GraphTracer(connection, skipped)
        if direction == "any":
            dist = tracer.expand(start_id, max_hops)
            selected = tracer.select_output(start_id, dist)
            edge_records = tracer.induced_edges(selected, dist)
        else:
            directed = _DirectedTracer(connection, skipped, ARROW_IO[direction])
            dist, selected, edge_ids = directed.expand(start_id, max_hops)
            edge_records = [
                (edge, max(dist[edge["source"]], dist[edge["target"]]))
                for edge in (directed.edges[edge_id] for edge_id in _sorted_ids(edge_ids))
            ]
        open_endpoints = tracer.open_endpoints(selected)
        node_records = tracer.node_records(selected, dist)

    base = {
        "file": filename,
        "start": {
            "id": start_id,
            "page": node_records[start_id]["page"],
            "kind": node_records[start_id]["type"],
        },
        "max_hops": max_hops,
        "skip": sorted(skipped),
        "direction": direction,
        "response_format": response_format,
        "reached_hops": max(dist[node] for node in selected),
        "open_endpoints": open_endpoints,
    }
    if response_format == "hops":
        return {
            **base,
            "hops": [
                {
                    "hop": hop,
                    "pages": _project_pages(
                        [record for record in node_records.values() if record["hop"] == hop],
                        [edge for edge in edge_records if edge[1] == hop],
                    ),
                }
                for hop in range(base["reached_hops"] + 1)
            ],
        }
    return {
        **base,
        "result": {
            "pages": _project_pages(list(node_records.values()), edge_records),
        },
    }


class _GraphTracer:
    def __init__(self, connection: sqlite3.Connection, skipped: frozenset[str]) -> None:
        self.connection = connection
        self.skipped = skipped
        self.adjacency: dict[str, list[tuple[str, str]]] = {}
        self.edges: dict[str, dict[str, Any]] = {}

    def expand(self, start: str, max_hops: int) -> dict[str, int]:
        """Return the hop count of every node reached within ``max_hops``."""
        dist = {start: 0}
        layer = [start]
        for hop in range(max_hops + 1):
            # Absorb skipped kinds into the current hop before counting the next.
            wave = list(layer)
            while wave:
                self.fetch(wave)
                reached = []
                for node in wave:
                    for _, neighbor in self.adjacency[node]:
                        if neighbor not in dist and id_kind(neighbor) in self.skipped:
                            dist[neighbor] = hop
                            reached.append(neighbor)
                layer.extend(reached)
                wave = reached
            if hop == max_hops:
                break
            next_layer = []
            for node in layer:
                for _, neighbor in self.adjacency[node]:
                    if neighbor not in dist:
                        dist[neighbor] = hop + 1
                        next_layer.append(neighbor)
            if not next_layer:
                break
            layer = next_layer
        return dist

    def select_output(self, start: str, dist: dict[str, int]) -> set[str]:
        """Keep counted nodes plus skipped nodes on the paths that reached them."""
        counted = {node for node in dist if id_kind(node) not in self.skipped} | {start}
        selected = set(counted)
        stack = list(counted)
        while stack:
            node = stack.pop()
            if node == start:
                continue
            step = 0 if id_kind(node) in self.skipped else 1
            for _, parent in self.adjacency.get(node, []):
                if (
                    parent not in selected
                    and parent in dist
                    and dist[parent] + step == dist[node]
                    and id_kind(parent) in self.skipped
                ):
                    selected.add(parent)
                    stack.append(parent)
        # Skipped nodes that join two traced nodes, e.g. resolved endpoints.
        for node in dist:
            if node not in selected and sum(
                neighbor in selected for _, neighbor in self.adjacency.get(node, [])
            ) >= 2:
                selected.add(node)
        return selected

    def open_endpoints(self, selected: set[str]) -> list[dict[str, Any]]:
        """Return endpoints joining traced nodes to nodes the trace did not reach."""
        self.fetch(selected)
        endpoints = {node for node in selected if id_kind(node) == "endpoint"}
        endpoints.update(
            neighbor
            for node in selected
            for _, neighbor in self.adjacency.get(node, [])
            if id_kind(neighbor) == "endpoint"
        )
        self.fetch(endpoints)
        result = []
        for endpoint in _sorted_ids(endpoints):
            neighbors = {neighbor for _, neighbor in self.adjacency[endpoint]}
            sources = neighbors & selected
            candidates = neighbors - selected
            if sources and candidates:
                result.append({
                    "id": endpoint,
                    "page": _id_page(endpoint),
                    "sources": _sorted_ids(sources),
                    "candidates": _sorted_ids(candidates),
                })
        return result

    def node_records(self, selected: set[str], dist: dict[str, int]) -> dict[str, dict[str, Any]]:
        records = {}
        for chunk in _chunks(sorted(selected)):
            rows = self.connection.execute(
                "SELECT id, page, kind, subclass, x0, y0, x1, y1, title, descriptions, properties "
                f"FROM nodes WHERE id IN ({_placeholders(chunk)})",
                chunk,
            )
            for node_id, page, kind, subclass, x0, y0, x1, y1, title, descriptions, properties in rows:
                records[node_id] = {
                    "id": node_id,
                    "type": kind,
                    "subclass": subclass,
                    "page": page,
                    "bbox": None if x0 is None else {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                    "title": json.loads(title),
                    "descriptions": json.loads(descriptions),
                    **json.loads(properties),
                    "hop": dist[node_id],
                }
        return records

    def induced_edges(
        self,
        selected: set[str],
        dist: dict[str, int],
    ) -> list[tuple[dict[str, Any], int]]:
        """Return ``(edge record, hop)`` for trace edges between selected nodes.

        Parallel transfers between the same source and target component collapse
        to the one with the smallest id; each direction is kept.
        """
        edge_ids = {
            edge_id
            for node in selected
            for edge_id, neighbor in self.adjacency.get(node, [])
            if neighbor in selected
        }
        edges: dict[Any, dict[str, Any]] = {}
        for edge_id in _sorted_ids(edge_ids):
            edge = self.edges[edge_id]
            key = (edge["source"], edge["target"]) if edge["type"] == "transfer" else edge_id
            edges.setdefault(key, edge)
        return [
            (edge, max(dist[edge["source"]], dist[edge["target"]]))
            for edge in edges.values()
        ]

    def fetch(self, nodes: Iterable[str]) -> None:
        """Load the trace edges of nodes whose adjacency is not cached yet."""
        missing = [node for node in nodes if node not in self.adjacency]
        for chunk in _chunks(missing):
            members = set(chunk)
            for node in chunk:
                self.adjacency[node] = []
            placeholders = _placeholders(chunk)
            rows = self.connection.execute(
                "SELECT id, kind, source_id, target_id, source_page, target_page, properties "
                "FROM edges WHERE kind IN ('connection', 'contains', 'transfer') "
                f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))",
                [*chunk, *chunk],
            )
            for edge_id, kind, source, target, source_page, target_page, properties in rows:
                self.edges.setdefault(edge_id, {
                    "id": edge_id,
                    "type": kind,
                    "source": source,
                    "target": target,
                    "source_page": source_page,
                    "target_page": target_page,
                    **json.loads(properties),
                })
                if source in members:
                    self.adjacency[source].append((edge_id, target))
                if target in members:
                    self.adjacency[target].append((edge_id, source))


class _DirectedTracer:
    """Hop expansion that keeps heading toward arrows whose ``page_io`` is ``target_io``.

    From a node that can reach such arrows on its page, a hop only follows the
    shortest paths toward them: each step must bring one of those arrows one edge
    closer (see :class:`PageGraph`). From a node that reaches none of them,
    including every node of a page without such arrows, a hop may go to any
    in-page neighbour. Only those target arrows cross to another page, through
    their transfers; arrows of the opposite direction never do.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        skipped: frozenset[str],
        target_io: str,
    ) -> None:
        self.connection = connection
        self.skipped = skipped
        self.target_io = target_io
        self.page_of: dict[str, PageGraph] = {}
        self.edges: dict[str, dict[str, Any]] = {}

    def expand(self, start: str, max_hops: int) -> tuple[dict[str, int], set[str], set[str]]:
        """Return hop counts, traversed nodes and traversed edge ids."""
        dist = {start: 0}
        nodes = {start}
        edges: set[str] = set()
        layer = [start]
        for hop in range(max_hops):
            seen = frozenset(dist)
            next_layer: list[str] = []
            for node in layer:
                found, via_nodes, via_edges = self._hop(node, seen)
                for target in sorted(found):
                    if target not in dist:
                        dist[target] = hop + 1
                        next_layer.append(target)
                for via in via_nodes:
                    dist.setdefault(via, hop)
                nodes.update(found, via_nodes)
                edges.update(via_edges)
            if not next_layer:
                break
            layer = next_layer
        return dist, nodes, edges

    def _hop(self, node: str, seen: frozenset[str]) -> tuple[set[str], set[str], set[str]]:
        page = self._page(node)
        if page.is_arrow(node) and page.nodes[node].get("page_io") == self.target_io:
            return self._cross_page(node, seen)
        targets = [
            arrow for arrow in page.arrows(self.target_io)
            if arrow != node and node in page.distances(arrow)
        ]
        if not targets:
            return self._walk(node, seen, page, lambda _x, _y: True)
        found: set[str] = set()
        via_nodes: set[str] = set()
        via_edges: set[str] = set()
        for arrow in targets:
            distance = page.distances(arrow)

            def closer(x: str, y: str, distance: dict[str, int] = distance, arrow: str = arrow) -> bool:
                return (
                    y in distance
                    and distance[y] == distance[x] - 1
                    and (y == arrow or not page.is_arrow(y))
                )

            walk = self._walk(node, seen, page, closer)
            found |= walk[0]
            via_nodes |= walk[1]
            via_edges |= walk[2]
        return found, via_nodes, via_edges

    def _walk(
        self,
        start: str,
        seen: frozenset[str],
        page: PageGraph,
        allowed: Callable[[str, str], bool],
    ) -> tuple[set[str], set[str], set[str]]:
        """Walk allowed in-page steps through skipped kinds up to the first counted nodes."""
        predecessors: dict[str, list[tuple[str, str]]] = {start: []}
        found: set[str] = set()
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for edge_id, neighbor in page.adjacency[current]:
                if neighbor == start or not allowed(current, neighbor):
                    continue
                if neighbor in predecessors:
                    predecessors[neighbor].append((edge_id, current))
                    continue
                predecessors[neighbor] = [(edge_id, current)]
                if id_kind(neighbor) in self.skipped:
                    queue.append(neighbor)
                elif neighbor not in seen:
                    found.add(neighbor)
        # Keep only the connectors and edges on routes that ended at a new node.
        via_nodes: set[str] = set()
        via_edges: set[str] = set()
        stack = list(found)
        while stack:
            for edge_id, previous in predecessors[stack.pop()]:
                via_edges.add(edge_id)
                if previous != start and previous not in via_nodes:
                    via_nodes.add(previous)
                    stack.append(previous)
        return found, via_nodes, via_edges

    def _cross_page(self, arrow: str, seen: frozenset[str]) -> tuple[set[str], set[str], set[str]]:
        """Follow a target arrow's transfers, one edge per partner arrow."""
        partners: dict[str, str] = {}
        for edge_id, kind, source, target, source_page, target_page, properties in self.connection.execute(
            "SELECT id, kind, source_id, target_id, source_page, target_page, properties "
            "FROM edges WHERE kind = 'transfer' AND (source_id = ? OR target_id = ?) ORDER BY id",
            (arrow, arrow),
        ):
            self.edges.setdefault(edge_id, {
                "id": edge_id,
                "type": kind,
                "source": source,
                "target": target,
                "source_page": source_page,
                "target_page": target_page,
                **json.loads(properties),
            })
            partners.setdefault(target if source == arrow else source, edge_id)
        found = {partner for partner in partners if partner not in seen}
        return found, set(), {partners[partner] for partner in found}

    def _page(self, node: str) -> PageGraph:
        if node not in self.page_of:
            filename, page_number = self.connection.execute(
                "SELECT filename, page FROM nodes WHERE id = ?",
                (node,),
            ).fetchone()
            page = PageGraph(self.connection, filename, page_number)
            self.edges.update(page.edges)
            for page_node in page.nodes:
                self.page_of[page_node] = page
        return self.page_of[node]


def _project_pages(
    nodes: list[dict[str, Any]],
    edges: list[tuple[dict[str, Any], int]],
) -> list[dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}

    def page_entry(page_number: int) -> dict[str, Any]:
        return pages.setdefault(page_number, {
            "page_number": page_number,
            **{f"{kind}s": [] for kind in NODE_KINDS},
            "relations": [],
            "transfers": [],
        })

    for node in sorted(nodes, key=lambda record: _id_sort_key(record["id"])):
        page_entry(node["page"])[f"{node['type']}s"].append(node)
    for edge, _ in sorted(edges, key=lambda item: _id_sort_key(item[0]["id"])):
        if edge["type"] == "transfer":
            page_entry(edge["source_page"])["transfers"].append({
                "id": edge["id"],
                "source_page": edge["source_page"],
                "source_component": edge["source"],
                "target_page": edge["target_page"],
                "target_component": edge["target"],
                "source_bbox": edge.get("source_bbox"),
                "target_bbox": edge.get("target_bbox"),
            })
        else:
            page_entry(edge["source_page"])["relations"].append({
                "id": edge["id"],
                "type": edge["type"],
                "source": edge["source"],
                "target": edge["target"],
            })
    return [pages[page_number] for page_number in sorted(pages)]


def _chunks(items: list[str]) -> Iterator[list[str]]:
    for index in range(0, len(items), _QUERY_CHUNK):
        yield items[index:index + _QUERY_CHUNK]


def _placeholders(items: list[str]) -> str:
    return ", ".join("?" for _ in items)


def _id_page(item_id: str) -> int:
    return int(item_id.rsplit("/", 3)[-3][1:])


def _id_sort_key(item_id: str) -> tuple[int, str, int, str]:
    local = id_local_part(item_id)
    return (
        _id_page(item_id),
        id_kind(item_id),
        int(local) if local.isdigit() else -1,
        local,
    )


def _sorted_ids(ids: Iterable[str]) -> list[str]:
    return sorted(ids, key=_id_sort_key)
