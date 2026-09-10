"""Tests for the manifest -> agent-skeleton compressor.

Two properties matter most here.  The skeleton must contain *nothing* the agent
does not need -- a leaked ``file`` or ``line_start`` key costs tokens on every
request -- and it must contain *everything* it does, since the agent cannot
fall back to the manifest to fill a gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from parser.compress import (
    TYPE_ENTRY_POINT,
    TYPE_INTERMEDIARY,
    TYPE_SINK,
    UnknownEntryPointError,
    compress_for_agent,
    main,
)
from parser.spring_analyze import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "demo-shop"

#: Keys the skeleton is allowed to carry on a node.  Anything else is a leak.
ALLOWED_NODE_KEYS = {"id", "type", "signature", "class", "calls"}

#: Keys that must never reach the agent.
FORBIDDEN_KEYS = {"file", "line", "line_start", "line_end", "warnings", "description"}


@pytest.fixture(scope="module")
def manifest() -> "dict[str, Any]":
    """A real manifest from the demo Spring app."""
    return analyze_repository(FIXTURE)


@pytest.fixture(scope="module")
def skeleton(manifest: "dict[str, Any]") -> "dict[str, Any]":
    """The compressed skeleton for the checkout entry point."""
    return compress_for_agent(manifest, "ep_1")


def synthetic(**overrides: Any) -> "dict[str, Any]":
    """Build a minimal hand-written manifest for graph-shape tests."""
    base: "dict[str, Any]" = {
        "entry_points": [
            {
                "id": "ep_1",
                "method": "POST",
                "path": "/orders/checkout",
                "function": "createOrder",
                "class": "OrderController",
                "file": "OrderController.java",
                "line_start": 1,
                "line_end": 9,
                "parameters": [{"name": "order", "type": "OrderRequest"}],
                "calls": ["fn_1"],
            }
        ],
        "functions": {
            "fn_1": {
                "name": "charge",
                "class": "PaymentServiceImpl",
                "resolved_from_interface": "PaymentService",
                "parameters": [{"name": "order", "type": "Order"}],
                "file": "PaymentServiceImpl.java",
                "line_start": 20,
                "line_end": 35,
                "calls": ["sink_1"],
                "type": "intermediary",
            }
        },
        "sinks": [
            {
                "id": "sink_1",
                "kind": "db_write",
                "call": "orderRepository.save(order)",
                "description": "orderRepository.save(order) in PaymentServiceImpl.java line 30",
                "class": "PaymentServiceImpl",
                "file": "PaymentServiceImpl.java",
                "line": 30,
            }
        ],
        "warnings": ["something the agent must never see"],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Output shape
# --------------------------------------------------------------------------- #


def test_matches_the_documented_shape() -> None:
    """The skeleton is exactly the contract downstream code was written against."""
    assert compress_for_agent(synthetic(), "ep_1") == {
        "entry_point": "POST /orders/checkout -> createOrder(OrderRequest)",
        "trace": [
            {
                "id": "ep_1",
                "type": "entry_point",
                "signature": "POST /orders/checkout -> createOrder(OrderRequest)",
                "class": "OrderController",
                "calls": ["fn_1"],
            },
            {
                "id": "fn_1",
                "type": "intermediary",
                "signature": "charge(Order)",
                "class": "PaymentServiceImpl",
                "calls": ["sink_1"],
            },
            {
                "id": "sink_1",
                "type": "sink",
                "signature": "orderRepository.save(order)",
                "class": None,
                "calls": [],
            },
        ],
    }


def test_top_level_keys_are_only_entry_point_and_trace(skeleton: "dict[str, Any]") -> None:
    """No warnings, no manifest passthrough."""
    assert set(skeleton) == {"entry_point", "trace"}


def test_nodes_carry_no_extra_keys(skeleton: "dict[str, Any]") -> None:
    """Every node is exactly the five documented fields."""
    for node in skeleton["trace"]:
        assert set(node) == ALLOWED_NODE_KEYS, node


def test_no_file_or_line_information_leaks(skeleton: "dict[str, Any]") -> None:
    """Locations are the click-to-chat feature's concern, not the agent's."""
    serialized = json.dumps(skeleton)
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in serialized, f"{key} leaked into the skeleton"


def test_entry_point_field_mirrors_the_first_node(skeleton: "dict[str, Any]") -> None:
    """The headline signature is the entry point's own."""
    assert skeleton["entry_point"] == skeleton["trace"][0]["signature"]
    assert skeleton["trace"][0]["type"] == TYPE_ENTRY_POINT


# --------------------------------------------------------------------------- #
# Signatures
# --------------------------------------------------------------------------- #


def test_signatures_on_a_real_manifest(skeleton: "dict[str, Any]") -> None:
    """Each node type renders in its documented one-line form."""
    by_id = {node["id"]: node for node in skeleton["trace"]}
    assert by_id["ep_1"]["signature"] == "POST /orders/checkout -> createOrder(Order)"
    assert by_id["fn_2"]["signature"] == "checkout(Order)"
    assert by_id["fn_2"]["class"] == "OrderServiceImpl"
    assert by_id["sink_1"]["signature"] == "orderRepository.save(order)"


def test_sink_class_is_null(skeleton: "dict[str, Any]") -> None:
    """Sinks are operations; the issuing class is identified by the caller."""
    sinks = [node for node in skeleton["trace"] if node["type"] == TYPE_SINK]
    assert sinks and all(node["class"] is None for node in sinks)


def test_parameterless_method_renders_empty_parens() -> None:
    """A method with no parameters still reads as a call."""
    manifest = synthetic()
    manifest["functions"]["fn_1"]["parameters"] = []
    skeleton = compress_for_agent(manifest, "ep_1")
    assert skeleton["trace"][1]["signature"] == "charge()"


def test_sink_signature_falls_back_to_description() -> None:
    """A manifest predating the structured ``call`` field still compresses."""
    manifest = synthetic()
    del manifest["sinks"][0]["call"]
    skeleton = compress_for_agent(manifest, "ep_1")
    assert skeleton["trace"][2]["signature"] == "orderRepository.save(order)"


def test_missing_parameters_key_does_not_crash() -> None:
    """Signature rendering degrades rather than failing on a sparse manifest."""
    manifest = synthetic()
    del manifest["functions"]["fn_1"]["parameters"]
    assert compress_for_agent(manifest, "ep_1")["trace"][1]["signature"] == "charge()"


# --------------------------------------------------------------------------- #
# Graph traversal
# --------------------------------------------------------------------------- #


def test_only_reachable_nodes_are_included(manifest: "dict[str, Any]") -> None:
    """``findOrder`` belongs to the other endpoint and must not appear."""
    reachable = {node["id"] for node in compress_for_agent(manifest, "ep_1")["trace"]}
    other = {node["id"] for node in compress_for_agent(manifest, "ep_2")["trace"]}
    assert "fn_3" in other and "fn_3" not in reachable
    assert "ep_2" not in reachable


def test_traversal_is_depth_first(manifest: "dict[str, Any]") -> None:
    """A branch is fully expanded before the next one starts.

    ``checkout`` calls ``charge`` and then saves; the nodes reached through
    ``charge`` must therefore precede ``checkout``'s own sink.
    """
    order = [node["id"] for node in compress_for_agent(manifest, "ep_1")["trace"]]
    assert order.index("fn_4") < order.index("sink_4") < order.index("sink_1")


def test_edges_come_from_the_manifest_not_re_derived() -> None:
    """Traversal follows ``calls`` verbatim, even when it is unusual."""
    manifest = synthetic()
    manifest["entry_points"][0]["calls"] = ["sink_1"]  # skip the intermediary
    skeleton = compress_for_agent(manifest, "ep_1")
    assert [node["id"] for node in skeleton["trace"]] == ["ep_1", "sink_1"]


def test_shared_node_appears_once_but_keeps_every_edge() -> None:
    """A helper called twice is one node; both callers still point at it."""
    manifest = synthetic()
    manifest["entry_points"][0]["calls"] = ["fn_1", "fn_2"]
    manifest["functions"]["fn_2"] = {
        "name": "refund",
        "class": "PaymentServiceImpl",
        "parameters": [],
        "calls": ["sink_1"],
        "type": "intermediary",
    }
    skeleton = compress_for_agent(manifest, "ep_1")
    ids = [node["id"] for node in skeleton["trace"]]
    assert ids.count("sink_1") == 1
    by_id = {node["id"]: node for node in skeleton["trace"]}
    assert by_id["fn_1"]["calls"] == ["sink_1"]
    assert by_id["fn_2"]["calls"] == ["sink_1"]


def test_cycle_terminates() -> None:
    """Mutually recursive services do not hang or repeat."""
    manifest = synthetic()
    manifest["functions"]["fn_1"]["calls"] = ["fn_2"]
    manifest["functions"]["fn_2"] = {
        "name": "retry",
        "class": "PaymentServiceImpl",
        "parameters": [],
        "calls": ["fn_1"],
        "type": "intermediary",
    }
    ids = [node["id"] for node in compress_for_agent(manifest, "ep_1")["trace"]]
    assert ids == ["ep_1", "fn_1", "fn_2"]


def test_dangling_call_ids_are_dropped() -> None:
    """An id with no node is removed so the skeleton stays self-consistent."""
    manifest = synthetic()
    manifest["functions"]["fn_1"]["calls"] = ["sink_1", "fn_missing"]
    skeleton = compress_for_agent(manifest, "ep_1")
    by_id = {node["id"]: node for node in skeleton["trace"]}
    assert by_id["fn_1"]["calls"] == ["sink_1"]
    referenced = {call for node in skeleton["trace"] for call in node["calls"]}
    assert referenced <= set(by_id), "every referenced id must exist in the trace"


def test_entry_point_with_no_calls_yields_a_single_node() -> None:
    """A handler that only returns a view is still a valid one-node trace."""
    manifest = synthetic()
    manifest["entry_points"][0]["calls"] = []
    skeleton = compress_for_agent(manifest, "ep_1")
    assert len(skeleton["trace"]) == 1
    assert skeleton["trace"][0]["calls"] == []


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_entry_point_raises_with_available_ids(manifest: "dict[str, Any]") -> None:
    """The error tells the caller what they could have asked for."""
    with pytest.raises(UnknownEntryPointError) as caught:
        compress_for_agent(manifest, "ep_99")
    message = caught.value.args[0]
    assert "ep_99" in message and "ep_1" in message


def test_function_id_is_not_accepted_as_an_entry_point(manifest: "dict[str, Any]") -> None:
    """Only entry points can start a trace."""
    with pytest.raises(UnknownEntryPointError):
        compress_for_agent(manifest, "fn_1")


# --------------------------------------------------------------------------- #
# Size
# --------------------------------------------------------------------------- #


def test_skeleton_is_substantially_smaller(manifest: "dict[str, Any]") -> None:
    """The whole point: fewer characters on the wire."""
    full = len(json.dumps(manifest))
    compressed = len(json.dumps(compress_for_agent(manifest, "ep_1")))
    assert compressed < full / 2


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_prints_skeleton_and_savings(
    tmp_path: Path, manifest: "dict[str, Any]", capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI writes JSON to stdout and the size comparison to stderr."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert main([str(path), "ep_1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["trace"][0]["id"] == "ep_1"
    assert "reduction:" in captured.err


def test_cli_reports_unknown_entry_point(
    tmp_path: Path, manifest: "dict[str, Any]", capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad id exits non-zero with a readable message, not a traceback."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main([str(path), "ep_99"]) == 2
    assert "ep_99" in capsys.readouterr().err


def test_cli_reports_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A malformed manifest is a clean error, not a crash."""
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")

    assert main([str(path), "ep_1"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_reports_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing manifest is a clean error too."""
    assert main([str(tmp_path / "nope.json"), "ep_1"]) == 2
    assert "could not read" in capsys.readouterr().err
