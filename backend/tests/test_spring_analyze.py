"""Tests for the Spring execution-manifest analyzer.

Line spans are the manifest's load-bearing property: downstream features slice
source files with them, so a drifting ``line_start`` silently corrupts every
code snippet the product shows.  These tests therefore assert spans by reading
the fixture back and checking what is *on* the reported line, rather than by
pinning bare integers that would need updating whenever the fixture moves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from parser.spring_analyze import SpringAnalyzer, analyze_repository, join_route

FIXTURE = Path(__file__).parent / "fixtures" / "demo-shop"


@pytest.fixture(scope="module")
def manifest() -> "dict[str, Any]":
    """Analyze the demo Spring app once for the whole module."""
    return analyze_repository(FIXTURE)


def source_line(repo_relative: str, line: int) -> str:
    """Return a 1-based line from a fixture file, stripped of indentation."""
    text = (FIXTURE / repo_relative).read_text(encoding="utf-8")
    return text.splitlines()[line - 1].strip()


def find_entry_point(manifest: "dict[str, Any]", function: str) -> "dict[str, Any]":
    """Look up an entry point by its handler method name."""
    matches = [entry for entry in manifest["entry_points"] if entry["function"] == function]
    assert matches, f"no entry point for {function}"
    return matches[0]


def find_function(manifest: "dict[str, Any]", class_name: str, name: str) -> "dict[str, Any]":
    """Look up a traced function by owning class and method name."""
    matches = [
        record
        for record in manifest["functions"].values()
        if record["class"] == class_name and record["name"] == name
    ]
    assert matches, f"no function for {class_name}.{name}"
    return matches[0]


# --------------------------------------------------------------------------- #
# Line spans
# --------------------------------------------------------------------------- #


def test_entry_point_span_starts_at_mapping_annotation(manifest: "dict[str, Any]") -> None:
    """A handler's span opens on its mapping annotation, not the signature."""
    entry = find_entry_point(manifest, "createOrder")
    assert source_line(entry["file"], entry["line_start"]) == '@PostMapping("/checkout")'
    assert source_line(entry["file"], entry["line_end"]) == "}"


def test_span_survives_brace_in_javadoc(manifest: "dict[str, Any]") -> None:
    """A stray ``}`` in a javadoc block above the class cannot shift spans."""
    entry = find_entry_point(manifest, "createOrder")
    javadoc = source_line(entry["file"], 17)
    assert "}" in javadoc and "javadoc" in javadoc, "fixture no longer exercises this case"
    assert entry["line_start"] > 17


def test_span_survives_brace_in_string_literal(manifest: "dict[str, Any]") -> None:
    """A ``{`` inside a string literal does not open a block for brace matching."""
    helper = find_function(manifest, "OrderController", "validate")
    body = [
        source_line(helper["file"], line)
        for line in range(helper["line_start"], helper["line_end"] + 1)
    ]
    assert any("not a block" in line for line in body), "fixture no longer exercises this case"
    assert body[-1] == "}"


def test_span_covers_multi_line_signature(manifest: "dict[str, Any]") -> None:
    """A signature split across lines is fully contained in the span."""
    entry = find_entry_point(manifest, "getOrder")
    assert source_line(entry["file"], entry["line_start"]).startswith("@GetMapping")
    assert source_line(entry["file"], entry["line_start"] + 1).endswith("getOrder(")
    assert source_line(entry["file"], entry["line_end"]) == "}"


def test_every_span_is_well_formed(manifest: "dict[str, Any]") -> None:
    """No record may carry an inverted, zero or out-of-file line range."""
    records = list(manifest["entry_points"]) + list(manifest["functions"].values())
    for record in records:
        total = len((FIXTURE / record["file"]).read_text(encoding="utf-8").splitlines())
        assert 1 <= record["line_start"] <= record["line_end"] <= total, record


def test_sink_line_points_at_the_call(manifest: "dict[str, Any]") -> None:
    """A sink's line holds the call its description quotes."""
    for sink in manifest["sinks"]:
        call = sink["description"].split(" in ")[0]
        receiver, _, remainder = call.partition(".")
        method = remainder.split("(")[0]
        line = source_line(sink["file"], sink["line"])
        assert receiver in line and method in line, sink


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def test_route_combines_class_and_method_paths(manifest: "dict[str, Any]") -> None:
    """The class-level ``@RequestMapping`` prefixes the method-level path."""
    assert find_entry_point(manifest, "createOrder")["path"] == "/orders/checkout"
    assert find_entry_point(manifest, "getOrder")["path"] == "/orders/{id}"


def test_http_verb_and_parameters_are_captured(manifest: "dict[str, Any]") -> None:
    """Verb comes from the mapping annotation; parameters keep declared types."""
    entry = find_entry_point(manifest, "createOrder")
    assert entry["method"] == "POST"
    assert entry["parameters"] == [{"name": "order", "type": "Order"}]


@pytest.mark.parametrize(
    ("prefix", "suffix", "expected"),
    [
        ("/orders", "/checkout", "/orders/checkout"),
        ("orders", "checkout", "/orders/checkout"),
        ("/orders/", "/", "/orders"),
        ("", "/checkout", "/checkout"),
        ("", "", "/"),
    ],
)
def test_join_route(prefix: str, suffix: str, expected: str) -> None:
    """Route joining normalizes slashes without dropping segments."""
    assert join_route(prefix, suffix) == expected


# --------------------------------------------------------------------------- #
# Call resolution
# --------------------------------------------------------------------------- #


def test_interface_call_resolves_to_implementation(manifest: "dict[str, Any]") -> None:
    """A call on an injected interface is recorded against its impl class."""
    checkout = find_function(manifest, "OrderServiceImpl", "checkout")
    assert checkout["resolved_from_interface"] == "OrderService"
    assert checkout["type"] == "intermediary"


def test_same_class_helper_is_traced(manifest: "dict[str, Any]") -> None:
    """An unqualified call to a private helper becomes a graph node."""
    helper = find_function(manifest, "OrderController", "validate")
    assert helper["resolved_from_interface"] is None
    entry = find_entry_point(manifest, "createOrder")
    assert any(manifest["functions"].get(cid) is helper for cid in entry["calls"])


def test_constructor_injected_dependency_resolves(manifest: "dict[str, Any]") -> None:
    """``private final`` fields wired by constructor are resolvable receivers.

    ``PaymentServiceImpl`` carries no ``@Autowired``, so its outgoing calls only
    appear if constructor injection is understood.
    """
    charge = find_function(manifest, "PaymentServiceImpl", "charge")
    kinds = {manifest_sink["kind"] for manifest_sink in _sinks_of(manifest, charge)}
    assert kinds == {"http_call", "db_write"}


def test_this_qualified_calls_are_resolved(manifest: "dict[str, Any]") -> None:
    """``this.field.call()`` resolves like a bare ``field.call()``.

    ``javalang`` parses an explicit ``this.`` receiver into a ``This`` node
    whose selectors hold the field and the call, leaving the invocation's own
    qualifier empty.  Without special handling those calls disappear entirely,
    which is easy to miss because the unqualified form still works.
    """
    charge = find_function(manifest, "PaymentServiceImpl", "charge")
    source = (FIXTURE / charge["file"]).read_text(encoding="utf-8")
    assert "this.orderRepository.save(order)" in source, "fixture no longer exercises this case"

    descriptions = {sink["description"].split(" in ")[0] for sink in _sinks_of(manifest, charge)}
    assert "orderRepository.save(order)" in descriptions, "the field name must survive into the description"


def test_call_chain_reaches_the_database(manifest: "dict[str, Any]") -> None:
    """Controller -> service -> payment service -> repository resolves end to end."""
    entry = find_entry_point(manifest, "createOrder")
    checkout = find_function(manifest, "OrderServiceImpl", "checkout")
    charge = find_function(manifest, "PaymentServiceImpl", "charge")

    assert _id_of(manifest, checkout) in entry["calls"]
    assert _id_of(manifest, charge) in checkout["calls"]
    assert any(sink["kind"] == "db_write" for sink in _sinks_of(manifest, charge))


def test_depth_limit_is_respected() -> None:
    """``max_depth=1`` records the first hop but does not expand it.

    The directly-called functions are still useful nodes, so they are emitted;
    what the limit cuts off is tracing *their* outgoing calls, which must be
    reported as a warning rather than looking like a leaf.
    """
    shallow = analyze_repository(FIXTURE, max_depth=1)
    assert shallow["functions"], "the first hop should still be recorded"
    assert all(not record["calls"] for record in shallow["functions"].values())
    assert not shallow["sinks"], "sinks live one hop further out than depth 1"
    assert any("Depth limit" in warning for warning in shallow["warnings"])


def test_default_depth_reaches_three_hops(manifest: "dict[str, Any]") -> None:
    """The default depth is enough for controller -> service -> collaborator."""
    charge = find_function(manifest, "PaymentServiceImpl", "charge")
    assert charge["calls"], "the third hop must still have its calls traced"


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #


def test_repository_calls_are_classified_by_verb(manifest: "dict[str, Any]") -> None:
    """``save`` is a write, ``findById`` is a read."""
    by_call = {sink["description"].split(" in ")[0]: sink["kind"] for sink in manifest["sinks"]}
    assert by_call["orderRepository.save(order)"] == "db_write"
    assert by_call["orderRepository.findById(id)"] == "db_read"


def test_functions_record_their_parameters(manifest: "dict[str, Any]") -> None:
    """Traced methods carry declared parameters, not just entry points.

    :mod:`parser.compress` renders one-line signatures such as ``charge(Order)``
    from these, so dropping them would silently degrade every skeleton.
    """
    assert find_function(manifest, "PaymentServiceImpl", "charge")["parameters"] == [
        {"name": "order", "type": "Order"}
    ]


def test_sinks_expose_the_call_separately_from_the_description(
    manifest: "dict[str, Any]",
) -> None:
    """``call`` is machine-readable; ``description`` adds the human location."""
    sink = next(sink for sink in manifest["sinks"] if sink["kind"] == "db_read")
    assert sink["call"] == "orderRepository.findById(id)"
    assert sink["description"].startswith(sink["call"] + " in ")


def test_rest_template_is_an_http_sink(manifest: "dict[str, Any]") -> None:
    """Outbound HTTP clients are sinks, not intermediaries to recurse into."""
    http = [sink for sink in manifest["sinks"] if sink["kind"] == "http_call"]
    assert len(http) == 1
    assert http[0]["description"].startswith("restTemplate.postForObject(")


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_unparseable_file_warns_without_crashing(manifest: "dict[str, Any]") -> None:
    """A malformed source file is skipped and reported, not fatal."""
    assert any("Broken.java" in warning for warning in manifest["warnings"])
    assert manifest["entry_points"], "the rest of the repo must still be analyzed"


def test_multiple_implementations_warn_and_pick_one(manifest: "dict[str, Any]") -> None:
    """Two impls of one interface: first wins, the other is reported."""
    assert any(
        "PaymentService" in warning and "multiple implementations" in warning
        for warning in manifest["warnings"]
    )
    assert find_function(manifest, "PaymentServiceImpl", "charge")


def test_empty_directory_yields_an_empty_manifest(tmp_path: Path) -> None:
    """A repo with no Java at all produces a valid, empty manifest."""
    assert analyze_repository(tmp_path) == {
        "entry_points": [],
        "functions": {},
        "sinks": [],
        "warnings": [],
    }


def test_analysis_is_deterministic() -> None:
    """Two runs produce byte-identical manifests, so ids are stable."""
    assert analyze_repository(FIXTURE) == analyze_repository(FIXTURE)


def test_build_directories_are_skipped(tmp_path: Path) -> None:
    """Compiled output under ``target/`` is not mistaken for source."""
    source = (FIXTURE / "src/main/java/com/demo/controller/OrderController.java").read_text(
        encoding="utf-8"
    )
    build_output = tmp_path / "target" / "generated"
    build_output.mkdir(parents=True)
    (build_output / "OrderController.java").write_text(source, encoding="utf-8")
    assert analyze_repository(tmp_path)["entry_points"] == []


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def test_registry_maps_interfaces_to_implementations() -> None:
    """Pass 1 alone builds a usable interface -> implementation map."""
    analyzer = SpringAnalyzer(FIXTURE)
    analyzer.scan_components()
    assert analyzer._interface_impls["OrderService"] == "OrderServiceImpl"
    assert analyzer._types["OrderServiceImpl"].injected_fields == {
        "paymentService": "PaymentService",
        "orderRepository": "OrderRepository",
    }
    assert analyzer._types["OrderRepository"].is_repository


def _id_of(manifest: "dict[str, Any]", record: "dict[str, Any]") -> str:
    """Reverse-lookup a function's id from its record."""
    for function_id, candidate in manifest["functions"].items():
        if candidate is record:
            return function_id
    raise AssertionError("function not present in manifest")


def _sinks_of(manifest: "dict[str, Any]", record: "dict[str, Any]") -> "list[dict[str, Any]]":
    """Sinks directly reachable from a function record."""
    by_id = {sink["id"]: sink for sink in manifest["sinks"]}
    return [by_id[cid] for cid in record["calls"] if cid in by_id]
