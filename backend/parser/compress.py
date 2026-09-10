"""Compress a manifest into a token-efficient skeleton for the Claude API.

Trace and diagram generation only needs to know *what runs in what order*, not
where the code lives.  Sending the full manifest would spend most of its tokens
on file paths, line numbers, warnings, and endpoints the user did not click on.
This module walks the call graph the parser already built and emits only the
nodes reachable from one selected entry point, each reduced to a single line.

File paths and line numbers are deliberately dropped.  The click-to-chat
feature reads those from the full manifest by node id, so the two concerns stay
separate: this skeleton is for reasoning about flow, the manifest is for
fetching source.

Usage::

    python -m parser.compress <manifest.json> <entry_point_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

__all__ = ["compress_for_agent", "UnknownEntryPointError", "main"]


#: Node type emitted for the selected controller handler.
TYPE_ENTRY_POINT = "entry_point"

#: Node type emitted for a traced application method.
TYPE_INTERMEDIARY = "intermediary"

#: Node type emitted for a terminal database or outbound-HTTP operation.
TYPE_SINK = "sink"


class UnknownEntryPointError(KeyError):
    """Raised when the requested entry point id is not in the manifest."""


def compress_for_agent(manifest: "dict[str, Any]", entry_point_id: str) -> "dict[str, Any]":
    """Reduce ``manifest`` to the execution skeleton for one entry point.

    The call graph is taken as-is from the manifest's ``calls`` lists -- this
    function never re-derives edges.  Traversal is depth-first from the entry
    point, so ``trace`` reads in execution order: a handler is followed by what
    it calls first, and each branch is fully expanded before the next begins.

    A node reached more than once (a shared helper, or a cycle) appears once,
    at its first visit.  Edges to it are still recorded on every caller, so the
    graph stays reconstructible from the ``calls`` fields.

    Args:
        manifest: A manifest as produced by :mod:`parser.spring_analyze`.
        entry_point_id: Id of the entry point the user selected, e.g. ``ep_1``.

    Returns:
        A dict with an ``entry_point`` signature and an ordered ``trace`` list.
        Every node carries ``id``, ``type``, ``signature``, ``class`` and
        ``calls``; nothing else, and no node unreachable from the entry point.

    Raises:
        UnknownEntryPointError: If ``entry_point_id`` is not in the manifest.
    """
    entry_points = {node["id"]: node for node in manifest.get("entry_points") or []}
    functions = dict(manifest.get("functions") or {})
    sinks = {node["id"]: node for node in manifest.get("sinks") or []}

    if entry_point_id not in entry_points:
        available = ", ".join(sorted(entry_points, key=_id_sort_key)) or "none"
        raise UnknownEntryPointError(
            f"{entry_point_id!r} is not an entry point in this manifest (available: {available})"
        )

    def describe(node_id: str) -> "Optional[dict[str, Any]]":
        """Reduce one manifest node to its skeleton form, if it is known."""
        if node_id in entry_points:
            node = entry_points[node_id]
            return {
                "id": node_id,
                "type": TYPE_ENTRY_POINT,
                "signature": entry_point_signature(node),
                "class": node.get("class"),
            }
        if node_id in functions:
            node = functions[node_id]
            return {
                "id": node_id,
                "type": node.get("type") or TYPE_INTERMEDIARY,
                "signature": function_signature(node),
                "class": node.get("class"),
            }
        if node_id in sinks:
            return {
                "id": node_id,
                "type": TYPE_SINK,
                "signature": sink_signature(sinks[node_id]),
                # A sink is an operation, not a class member: the class that
                # issues it is already identified by its caller in the chain.
                "class": None,
            }
        return None

    def calls_of(node_id: str) -> "list[str]":
        """Outgoing edges of a node, dropping any id the manifest lacks."""
        node = entry_points.get(node_id) or functions.get(node_id) or {}
        return [
            call_id
            for call_id in node.get("calls") or []
            if call_id in entry_points or call_id in functions or call_id in sinks
        ]

    trace: "list[dict[str, Any]]" = []
    for node_id in walk_depth_first(entry_point_id, calls_of):
        node = describe(node_id)
        if node is None:  # pragma: no cover - calls_of already filters these
            continue
        node["calls"] = calls_of(node_id)
        trace.append(node)

    return {"entry_point": trace[0]["signature"], "trace": trace}


def _id_sort_key(node_id: str) -> "tuple[str, int, str]":
    """Sort ``ep_2`` before ``ep_10`` instead of lexicographically."""
    prefix, _, suffix = node_id.rpartition("_")
    return (prefix, int(suffix), node_id) if suffix.isdigit() else (node_id, 0, node_id)


def walk_depth_first(start: str, calls_of: "Callable[[str], list[str]]") -> "list[str]":
    """Return node ids reachable from ``start``, in depth-first visit order.

    Iterative rather than recursive so a deep or cyclic graph cannot exhaust
    the stack, and each node is yielded once no matter how often it is reached.
    """
    order: "list[str]" = []
    seen: "set[str]" = set()
    stack = [start]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        order.append(node_id)
        # Reversed so the first callee is popped first and order reads forwards.
        stack.extend(reversed(calls_of(node_id)))
    return order


def entry_point_signature(node: "dict[str, Any]") -> str:
    """One-line signature, e.g. ``POST /orders/checkout -> createOrder(Order)``."""
    verb = node.get("method") or "ANY"
    path = node.get("path") or "/"
    return f"{verb} {path} -> {node.get('function')}({format_parameters(node)})"


def function_signature(node: "dict[str, Any]") -> str:
    """One-line signature for a traced method, e.g. ``charge(Order)``."""
    return f"{node.get('name')}({format_parameters(node)})"


def sink_signature(node: "dict[str, Any]") -> str:
    """One-line signature for a sink, e.g. ``orderRepository.save(order)``.

    Prefers the manifest's structured ``call`` field, falling back to trimming
    the location suffix off ``description`` for manifests produced before that
    field existed.
    """
    call = node.get("call")
    if call:
        return str(call)
    description = str(node.get("description") or "")
    return description.split(" in ")[0].strip() or node.get("id", "")


def format_parameters(node: "dict[str, Any]") -> str:
    """Render declared parameter types as a comma-separated list.

    Types alone, not names: the agent needs the shape of the call, and names
    would cost tokens without changing the trace.
    """
    return ", ".join(
        str(parameter.get("type") or "?") for parameter in node.get("parameters") or []
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    """CLI entry point: print a skeleton and report the token savings."""
    arguments = _parse_args(argv)

    try:
        manifest_text = arguments.manifest.read_text(encoding="utf-8")
    except OSError as error:
        print(f"error: could not read {arguments.manifest}: {error}", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        print(f"error: {arguments.manifest} is not valid JSON: {error}", file=sys.stderr)
        return 2

    try:
        skeleton = compress_for_agent(manifest, arguments.entry_point_id)
    except UnknownEntryPointError as error:
        print(f"error: {error.args[0]}", file=sys.stderr)
        return 2

    payload = json.dumps(skeleton, indent=arguments.indent, ensure_ascii=False)
    print(payload)
    print(_format_savings(manifest_text, payload, skeleton), file=sys.stderr)
    return 0


def _format_savings(manifest_text: str, payload: str, skeleton: "dict[str, Any]") -> str:
    """Summarize how much smaller the skeleton is than the whole manifest.

    Both sides are measured as the JSON actually sent, since that is what the
    request would be billed for.
    """
    before = len(manifest_text)
    after = len(payload)
    reduction = (1 - after / before) * 100 if before else 0.0
    return (
        f"manifest: {before:,} chars | skeleton: {after:,} chars | "
        f"reduction: {reduction:.1f}% | nodes: {len(skeleton['trace'])}"
    )


def _parse_args(argv: "Optional[Sequence[str]]") -> argparse.Namespace:
    """Build and run the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m parser.compress",
        description="Compress a manifest into an execution skeleton for one entry point.",
    )
    parser.add_argument("manifest", type=Path, help="path to a manifest JSON file")
    parser.add_argument("entry_point_id", help="id of the selected entry point, e.g. ep_1")
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
