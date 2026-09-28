"""Document-level component and wire tracing over persisted parser results."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Literal


NodeKind = Literal["component", "wire"]
ResponseFormat = Literal["subgraph", "hops"]
NodeKey = tuple[int, NodeKind, str]


class TraceStartNotFoundError(ValueError):
    """Raised when the requested starting entity is absent from the document."""


@dataclass
class _TraceGraph:
    pages: dict[int, dict[str, Any]] = field(default_factory=dict)
    nodes: dict[NodeKey, dict[str, Any]] = field(default_factory=dict)
    adjacency: dict[NodeKey, set[NodeKey]] = field(default_factory=dict)
    endpoint_nodes: dict[tuple[int, str], set[NodeKey]] = field(default_factory=dict)
    net_wires: dict[tuple[int, str], set[NodeKey]] = field(default_factory=dict)
    transfers: list[dict[str, Any]] = field(default_factory=list)
    transfer_pairs: list[tuple[NodeKey, NodeKey, dict[str, Any]]] = field(default_factory=list)

    def connect(self, left: NodeKey, right: NodeKey) -> None:
        if left == right or left not in self.nodes or right not in self.nodes:
            return
        self.adjacency.setdefault(left, set()).add(right)
        self.adjacency.setdefault(right, set()).add(left)


def trace_document(
    document_result: dict[str, Any],
    *,
    start_page: int,
    start_kind: NodeKind,
    start_id: int | str,
    max_hops: int,
    response_format: ResponseFormat = "subgraph",
) -> dict[str, Any]:
    """Trace components and wires using the same hop semantics as the reader UI."""
    return _trace_graph(
        _build_graph(document_result),
        start_page=start_page,
        start_kind=start_kind,
        start_id=start_id,
        max_hops=max_hops,
        response_format=response_format,
    )


def trace_document_file(
    result_path: str | Path,
    *,
    start_page: int,
    start_kind: NodeKind,
    start_id: int | str,
    max_hops: int,
    response_format: ResponseFormat = "subgraph",
) -> dict[str, Any]:
    """Trace a persisted result, caching the latest document graph in memory."""
    path = Path(result_path)
    stat = path.stat()
    graph = _load_graph(str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    return _trace_graph(
        graph,
        start_page=start_page,
        start_kind=start_kind,
        start_id=start_id,
        max_hops=max_hops,
        response_format=response_format,
    )


@lru_cache(maxsize=1)
def _load_graph(path: str, mtime_ns: int, size: int) -> _TraceGraph:
    del mtime_ns, size
    with Path(path).open("r", encoding="utf-8") as stream:
        document_result = json.load(stream)
    if not isinstance(document_result, dict):
        raise ValueError("document parsing result must be an object")
    return _build_graph(document_result)


def _trace_graph(
    graph: _TraceGraph,
    *,
    start_page: int,
    start_kind: NodeKind,
    start_id: int | str,
    max_hops: int,
    response_format: ResponseFormat,
) -> dict[str, Any]:
    if max_hops < 0:
        raise ValueError("max_hops must be non-negative")
    if start_kind not in {"component", "wire"}:
        raise ValueError("start_kind must be component or wire")
    if response_format not in {"subgraph", "hops"}:
        raise ValueError("response_format must be subgraph or hops")

    start = (int(start_page), start_kind, str(start_id))
    if start not in graph.nodes:
        raise TraceStartNotFoundError(
            f"{start_kind} {start_id} was not found on page {start_page}"
        )

    layers: list[set[NodeKey]] = [{start}]
    layer_evidence: list[list[dict[str, Any]]] = [[]]
    visited = {start}
    for _ in range(max_hops):
        nodes, evidence = _expand_one_hop(graph, layers[-1], visited)
        if not nodes:
            break
        layers.append(nodes)
        layer_evidence.append(evidence)
        visited.update(nodes)

    base = {
        "start": {"page": start[0], "kind": start[1], "id": graph.nodes[start]["id"]},
        "max_hops": max_hops,
        "direction": "any",
        "response_format": response_format,
        "reached_hops": len(layers) - 1,
    }
    if response_format == "hops":
        return {
            **base,
            "hops": [
                {
                    "hop": hop,
                    "pages": _layer_pages(graph, nodes, layer_evidence[hop]),
                }
                for hop, nodes in enumerate(layers)
            ],
        }
    return {**base, "result": {"pages": _subgraph_pages(graph, visited)}}


def _build_graph(document_result: dict[str, Any]) -> _TraceGraph:
    graph = _TraceGraph()
    raw_pages = document_result.get("pages", {})
    if not isinstance(raw_pages, dict):
        return graph

    for raw_page, page_record in raw_pages.items():
        if not isinstance(page_record, dict):
            continue
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            continue
        diagram = page_record.get("diagram")
        if not isinstance(diagram, dict):
            continue
        graph.pages[page_number] = page_record
        for kind, collection in (("component", "components"), ("wire", "wires")):
            for entity in diagram.get(collection, []):
                if not isinstance(entity, dict) or entity.get("id") is None:
                    continue
                key = (page_number, kind, str(entity["id"]))
                graph.nodes[key] = entity
                graph.adjacency.setdefault(key, set())

        for relation in diagram.get("relations", []):
            if not isinstance(relation, dict):
                continue
            source = str(relation.get("source", ""))
            target = str(relation.get("target", ""))
            if relation.get("type") == "connection":
                endpoint_ref, node_ref = _endpoint_and_node_refs(source, target)
                node = _node_key(page_number, node_ref)
                if endpoint_ref and node in graph.nodes:
                    graph.endpoint_nodes.setdefault(
                        (page_number, endpoint_ref.split(":", 1)[1]), set()
                    ).add(node)
            elif (
                relation.get("type") == "contains"
                and source.startswith("net:")
                and target.startswith("wire:")
            ):
                wire = _node_key(page_number, target)
                if wire in graph.nodes:
                    graph.net_wires.setdefault(
                        (page_number, source.split(":", 1)[1]), set()
                    ).add(wire)

        crosspage = page_record.get("crosspage_relations", {})
        transfers = crosspage.get("transfers", []) if isinstance(crosspage, dict) else []
        for transfer in transfers:
            if isinstance(transfer, dict):
                graph.transfers.append(dict(transfer))

    for nodes in graph.endpoint_nodes.values():
        for left, right in combinations(nodes, 2):
            graph.connect(left, right)
    for wires in graph.net_wires.values():
        for left, right in combinations(wires, 2):
            graph.connect(left, right)

    seen_transfers: set[tuple[int, str, int, str]] = set()
    for transfer in graph.transfers:
        try:
            source = (
                int(transfer["source_page"]),
                "component",
                str(transfer["source_component"]),
            )
            target = (
                int(transfer["target_page"]),
                "component",
                str(transfer["target_component"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        pair_key = (source[0], source[2], target[0], target[2])
        if pair_key in seen_transfers or source not in graph.nodes or target not in graph.nodes:
            continue
        seen_transfers.add(pair_key)
        graph.transfer_pairs.append((source, target, transfer))
        graph.connect(source, target)
    return graph


def _expand_one_hop(
    graph: _TraceGraph,
    frontier: set[NodeKey],
    visited: set[NodeKey],
) -> tuple[set[NodeKey], list[dict[str, Any]]]:
    """Expand one reader-style hop, including component-connected net wires."""
    net_nodes, net_evidence = _net_expansion(graph, frontier, visited)
    if net_nodes:
        return net_nodes, net_evidence

    nodes: set[NodeKey] = set()
    evidence: list[dict[str, Any]] = []
    component_connected_wires: set[NodeKey] = set()
    for (page, endpoint_id), adjacent in graph.endpoint_nodes.items():
        traced = adjacent.intersection(visited)
        sources = traced.intersection(frontier)
        if not sources:
            continue
        for candidate in adjacent.difference(visited):
            nodes.add(candidate)
            for source in sources:
                evidence.append({
                    "kind": "endpoint",
                    "page": page,
                    "id": endpoint_id,
                    "source": source,
                    "target": candidate,
                })
                if source[1] == "component" and candidate[1] == "wire":
                    component_connected_wires.add(candidate)

    if component_connected_wires:
        expanded, expanded_evidence = _net_expansion(
            graph,
            component_connected_wires,
            visited.union(nodes),
        )
        nodes.update(expanded)
        evidence.extend(expanded_evidence)

    for source, target, transfer in graph.transfer_pairs:
        if source in frontier and target not in visited:
            nodes.add(target)
            evidence.append({"kind": "transfer", "source": source, "target": target, "record": transfer})
        if target in frontier and source not in visited:
            nodes.add(source)
            evidence.append({"kind": "transfer", "source": target, "target": source, "record": transfer})
    return nodes.difference(visited), evidence


def _net_expansion(
    graph: _TraceGraph,
    sources: set[NodeKey],
    visited: set[NodeKey],
) -> tuple[set[NodeKey], list[dict[str, Any]]]:
    nodes: set[NodeKey] = set()
    evidence: list[dict[str, Any]] = []
    for (page, net_id), wires in graph.net_wires.items():
        selected = wires.intersection(sources)
        if not selected:
            continue
        for candidate in wires.difference(visited):
            nodes.add(candidate)
            for source in selected:
                evidence.append({
                    "kind": "net",
                    "page": page,
                    "id": net_id,
                    "source": source,
                    "target": candidate,
                })
    return nodes, evidence


def _subgraph_pages(graph: _TraceGraph, selected: set[NodeKey]) -> list[dict[str, Any]]:
    endpoint_keys = {
        key for key, nodes in graph.endpoint_nodes.items()
        if len(nodes.intersection(selected)) >= 2
    }
    net_keys = {
        key for key, wires in graph.net_wires.items()
        if wires.intersection(selected)
    }
    transfers = [
        record for source, target, record in graph.transfer_pairs
        if source in selected and target in selected
    ]
    return _project_pages(graph, selected, endpoint_keys, net_keys, transfers)


def _layer_pages(
    graph: _TraceGraph,
    nodes: set[NodeKey],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    endpoint_keys = {
        (int(item["page"]), str(item["id"]))
        for item in evidence if item["kind"] == "endpoint"
    }
    net_keys = {
        (int(item["page"]), str(item["id"]))
        for item in evidence if item["kind"] == "net"
    }
    transfers = [item["record"] for item in evidence if item["kind"] == "transfer"]
    relation_nodes = set(nodes)
    for item in evidence:
        relation_nodes.update((item["source"], item["target"]))
    return _project_pages(
        graph,
        nodes,
        endpoint_keys,
        net_keys,
        transfers,
        relation_nodes=relation_nodes,
    )


def _project_pages(
    graph: _TraceGraph,
    selected: set[NodeKey],
    endpoint_keys: set[tuple[int, str]],
    net_keys: set[tuple[int, str]],
    transfers: list[dict[str, Any]],
    *,
    relation_nodes: set[NodeKey] | None = None,
) -> list[dict[str, Any]]:
    relation_nodes = selected if relation_nodes is None else relation_nodes
    transfers_by_source_page: dict[int, list[dict[str, Any]]] = {}
    for transfer in _unique_transfers(transfers):
        try:
            source_page = int(transfer["source_page"])
        except (KeyError, TypeError, ValueError):
            continue
        transfers_by_source_page.setdefault(source_page, []).append(dict(transfer))

    page_numbers = {
        key[0] for key in selected
    }.union(
        page for page, _ in endpoint_keys
    ).union(
        page for page, _ in net_keys
    ).union(transfers_by_source_page)
    pages: list[dict[str, Any]] = []
    for page_number in sorted(page_numbers):
        page_record = graph.pages.get(page_number, {})
        diagram = page_record.get("diagram", {})
        components = [
            _public_entity(graph.nodes[key])
            for key in _sorted_nodes(selected)
            if key[0] == page_number and key[1] == "component"
        ]
        wires = [
            _public_entity(graph.nodes[key])
            for key in _sorted_nodes(selected)
            if key[0] == page_number and key[1] == "wire"
        ]
        endpoint_ids = {item_id for page, item_id in endpoint_keys if page == page_number}
        net_ids = {item_id for page, item_id in net_keys if page == page_number}
        endpoints = [
            _public_entity(entity)
            for entity in diagram.get("endpoints", [])
            if str(entity.get("id")) in endpoint_ids
        ]
        nets = [
            _public_entity(entity)
            for entity in diagram.get("nets", [])
            if str(entity.get("id")) in net_ids
        ]
        relations = _project_relations(
            diagram.get("relations", []),
            page_number,
            relation_nodes,
            endpoint_ids,
            net_ids,
        )
        pages.append({
            "page_number": page_number,
            "components": components,
            "wires": wires,
            "endpoints": endpoints,
            "nets": nets,
            "relations": relations,
            "transfers": transfers_by_source_page.get(page_number, []),
        })
    return pages


def _project_relations(
    relations: Iterable[Any],
    page: int,
    selected: set[NodeKey],
    endpoint_ids: set[str],
    net_ids: set[str],
) -> list[dict[str, Any]]:
    projected = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        source = str(relation.get("source", ""))
        target = str(relation.get("target", ""))
        if relation.get("type") == "connection":
            endpoint_ref, node_ref = _endpoint_and_node_refs(source, target)
            if (
                endpoint_ref
                and endpoint_ref.split(":", 1)[1] in endpoint_ids
                and _node_key(page, node_ref) in selected
            ):
                projected.append(dict(relation))
        elif (
            relation.get("type") == "contains"
            and source.startswith("net:")
            and target.startswith("wire:")
            and source.split(":", 1)[1] in net_ids
            and _node_key(page, target) in selected
        ):
            projected.append(dict(relation))
    return projected


def _public_entity(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in entity.items()
        if key not in {"elements", "element_ids"}
    }


def _endpoint_and_node_refs(left: str, right: str) -> tuple[str, str]:
    if left.startswith("endpoint:") and right.startswith(("component:", "wire:")):
        return left, right
    if right.startswith("endpoint:") and left.startswith(("component:", "wire:")):
        return right, left
    return "", ""


def _node_key(page: int, reference: str) -> NodeKey:
    kind, _, node_id = reference.partition(":")
    normalized_kind: NodeKind = "component" if kind == "component" else "wire"
    return (page, normalized_kind, node_id)


def _sorted_nodes(nodes: Iterable[NodeKey]) -> list[NodeKey]:
    return sorted(nodes, key=lambda key: (key[0], key[1], _natural_id(key[2])))


def _natural_id(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def _unique_transfers(transfers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for transfer in transfers:
        key = (
            transfer.get("source_page"),
            transfer.get("source_component"),
            transfer.get("target_page"),
            transfer.get("target_component"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(transfer)
    return unique
