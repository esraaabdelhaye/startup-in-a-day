"""Tests for the HTTP layer.

Synthesis is stubbed throughout: these tests are about routing, status codes,
caching and payload shape, not about the model.  The one thing worth being
strict on is that file paths and line numbers do not escape through the API
before the chat feature is the one asking for them.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.code_chat import CodeChatError
from agent.trace_synthesizer import TraceSynthesisError
from api import server as server_module
from api.server import create_app, load_env_file

FIXTURE = Path(__file__).parent / "fixtures" / "demo-shop"

STUB_RESULT = {
    "narrative": "A request arrives and is handled.",
    "mermaid": "sequenceDiagram\n    OrderController->>OrderServiceImpl: checkout(Order)",
}


@pytest.fixture
def stub_synthesis(monkeypatch: pytest.MonkeyPatch):
    """Replace synthesis with a counter-backed stub."""
    calls = {"count": 0}

    def fake(skeleton: "dict[str, Any]") -> "dict[str, Any]":
        calls["count"] += 1
        return dict(STUB_RESULT)

    monkeypatch.setattr(server_module, "synthesize_trace", fake)
    return calls


@pytest.fixture
def client(stub_synthesis) -> "TestClient":
    """A client against an app serving the demo-shop fixture."""
    with TestClient(create_app(FIXTURE)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def test_lists_entry_points(client: "TestClient") -> None:
    """The left panel gets the endpoints the parser found."""
    body = client.get("/api/entry-points").json()
    assert body["repo"] == "demo-shop"
    routes = {(entry["method"], entry["path"]) for entry in body["entry_points"]}
    assert routes == {("POST", "/orders/checkout"), ("GET", "/orders/{id}")}


def test_entry_points_expose_only_list_fields(client: "TestClient") -> None:
    """Locations stay server-side until the chat feature needs them."""
    body = client.get("/api/entry-points").json()
    for entry in body["entry_points"]:
        assert set(entry) == {"id", "method", "path", "function", "class"}


def test_warnings_are_surfaced(client: "TestClient") -> None:
    """Parse problems are visible to the UI rather than silently dropped."""
    body = client.get("/api/entry-points").json()
    assert any("Broken.java" in warning for warning in body["warnings"])


# --------------------------------------------------------------------------- #
# Traces
# --------------------------------------------------------------------------- #


def test_returns_narrative_mermaid_and_trace(client: "TestClient") -> None:
    """The right panel gets prose, a diagram, and the nodes behind it."""
    body = client.get("/api/trace/ep_1").json()
    assert body["narrative"] == STUB_RESULT["narrative"]
    assert body["mermaid"].startswith("sequenceDiagram")
    assert body["entry_point"] == "POST /orders/checkout -> createOrder(Order)"
    assert [node["id"] for node in body["trace"]][0] == "ep_1"


def test_trace_nodes_carry_no_locations(client: "TestClient") -> None:
    """The skeleton's exclusions survive the trip through the API."""
    serialized = json.dumps(client.get("/api/trace/ep_1").json()["trace"])
    for leaked in ("file", "line_start", "line_end"):
        assert f'"{leaked}"' not in serialized


def test_second_request_is_served_from_cache(
    client: "TestClient", stub_synthesis: "dict[str, int]"
) -> None:
    """Re-opening an endpoint must not bill a second Claude call."""
    first = client.get("/api/trace/ep_1").json()
    second = client.get("/api/trace/ep_1").json()

    assert stub_synthesis["count"] == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["mermaid"] == second["mermaid"]


def test_distinct_endpoints_are_synthesized_separately(
    client: "TestClient", stub_synthesis: "dict[str, int]"
) -> None:
    """The cache is per entry point, not global."""
    client.get("/api/trace/ep_1")
    client.get("/api/trace/ep_2")
    assert stub_synthesis["count"] == 2


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_entry_point_is_404(client: "TestClient") -> None:
    """A bad id is a client error, with the available ids in the detail."""
    response = client.get("/api/trace/ep_99")
    assert response.status_code == 404
    assert "ep_1" in response.json()["detail"]


def test_synthesis_failure_is_502(
    client: "TestClient", monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upstream failure is reported as such, with a readable detail."""

    def boom(skeleton: "dict[str, Any]") -> "dict[str, Any]":
        raise TraceSynthesisError("model returned nonsense")

    monkeypatch.setattr(server_module, "synthesize_trace", boom)
    response = client.get("/api/trace/ep_2")
    assert response.status_code == 502
    assert response.json()["detail"] == "model returned nonsense"


def test_failed_trace_is_not_cached(
    client: "TestClient", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure must not poison the cache and block a later retry."""
    monkeypatch.setattr(
        server_module,
        "synthesize_trace",
        lambda skeleton: (_ for _ in ()).throw(TraceSynthesisError("transient")),
    )
    assert client.get("/api/trace/ep_1").status_code == 502

    monkeypatch.setattr(server_module, "synthesize_trace", lambda skeleton: dict(STUB_RESULT))
    assert client.get("/api/trace/ep_1").status_code == 200


# --------------------------------------------------------------------------- #
# Static assets
# --------------------------------------------------------------------------- #


def test_serves_the_frontend(client: "TestClient") -> None:
    """The UI is served from the same origin, so no CORS setup is needed."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Codebase Digital Twin" in response.text


@pytest.mark.parametrize("asset", ["/app.js", "/styles.css"])
def test_serves_static_assets(client: "TestClient", asset: str) -> None:
    """The static mount does not shadow, or get shadowed by, the API."""
    assert client.get(asset).status_code == 200


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_answer(monkeypatch: pytest.MonkeyPatch) -> "dict[str, Any]":
    """Capture what would be sent to Claude instead of calling it."""
    seen: "dict[str, Any]" = {}

    def fake(snippet: str, question: str, label=None, history=None) -> str:
        seen.update(snippet=snippet, question=question, label=label, history=history)
        return "It charges the order and saves it."

    monkeypatch.setattr(server_module, "answer_question", fake)
    return seen


def test_ask_sends_the_exact_source_lines(
    client: "TestClient", stub_answer: "dict[str, Any]"
) -> None:
    """The snippet is the method's real body, read from disk by line span."""
    response = client.post(
        "/api/ask", json={"function_id": "fn_4", "question": "What does this do?"}
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "It charges the order and saves it."

    snippet = stub_answer["snippet"]
    assert "public void charge(Order order)" in snippet
    assert "this.orderRepository.save(order)" in snippet
    # The span stops at the method, not the rest of the class.
    assert "class PaymentServiceImpl" not in snippet


def test_ask_reports_the_source_location(
    client: "TestClient", stub_answer: "dict[str, Any]"
) -> None:
    """The response says which lines were read, for display and debugging."""
    body = client.post("/api/ask", json={"function_id": "fn_4", "question": "?"}).json()
    assert body["label"] == "PaymentServiceImpl.charge"
    assert body["file"].endswith("PaymentServiceImpl.java")
    assert body["line_start"] < body["line_end"]


def test_ask_works_for_entry_points(
    client: "TestClient", stub_answer: "dict[str, Any]"
) -> None:
    """Handlers are addressable too, not just intermediary functions."""
    client.post("/api/ask", json={"function_id": "ep_1", "question": "?"})
    assert stub_answer["label"] == "OrderController.createOrder"
    assert "createOrder" in stub_answer["snippet"]


def test_ask_gives_a_sink_surrounding_context(
    client: "TestClient", stub_answer: "dict[str, Any]"
) -> None:
    """A sink is one line; alone it would tell the model almost nothing."""
    client.post("/api/ask", json={"function_id": "sink_1", "question": "?"})
    assert len(stub_answer["snippet"].splitlines()) > 1
    assert "save(order)" in stub_answer["snippet"]


def test_ask_forwards_history_for_follow_ups(
    client: "TestClient", stub_answer: "dict[str, Any]"
) -> None:
    """A second question carries the first exchange."""
    history = [
        {"role": "user", "content": "What does it do?"},
        {"role": "assistant", "content": "It charges the order."},
    ]
    client.post(
        "/api/ask",
        json={"function_id": "fn_4", "question": "Why save twice?", "history": history},
    )
    assert stub_answer["history"] == history


def test_ask_rejects_unknown_function_id(client: "TestClient") -> None:
    """Stale frontend state gets a clear 404, not a crash."""
    response = client.post("/api/ask", json={"function_id": "fn_999", "question": "?"})
    assert response.status_code == 404
    assert "fn_999" in response.json()["detail"]


def test_ask_reports_upstream_failure(
    client: "TestClient", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model failure is a 502 with a readable message."""

    def boom(**kwargs: Any) -> str:
        raise CodeChatError("rate limited by the Claude API; retry shortly")

    monkeypatch.setattr(server_module, "answer_question", boom)
    response = client.post("/api/ask", json={"function_id": "fn_4", "question": "?"})
    assert response.status_code == 502
    assert "rate limited" in response.json()["detail"]


def test_ask_requires_both_fields(client: "TestClient") -> None:
    """A malformed body is rejected by validation, not at runtime."""
    assert client.post("/api/ask", json={"question": "?"}).status_code == 422


def test_ask_after_upload_uses_the_new_project(
    client: "TestClient", stub_answer: "dict[str, Any]"
) -> None:
    """Ids resolve against the currently loaded manifest, not the old one."""
    post_zip(client, build_zip({"PingController.java": CONTROLLER}))
    client.post("/api/ask", json={"function_id": "ep_1", "question": "?"})
    assert stub_answer["label"] == "PingController.ping"


# -- Snippet reading ------------------------------------------------------ #


def test_read_snippet_returns_exactly_the_requested_lines(tmp_path: Path) -> None:
    """Line spans are inclusive and 1-based, matching the manifest."""
    source = tmp_path / "A.java"
    source.write_text("\n".join(f"line{n}" for n in range(1, 11)), encoding="utf-8")
    location = server_module.SourceLocation("A.m", "A.java", 3, 5)
    assert server_module.read_snippet(tmp_path, location) == "line3\nline4\nline5"


def test_read_snippet_clamps_to_the_file(tmp_path: Path) -> None:
    """An over-long span (a sink near the end of a file) does not error."""
    source = tmp_path / "A.java"
    source.write_text("a\nb\nc", encoding="utf-8")
    location = server_module.SourceLocation("A.m", "A.java", 2, 99)
    assert server_module.read_snippet(tmp_path, location) == "b\nc"


def test_read_snippet_refuses_paths_outside_the_repo(tmp_path: Path) -> None:
    """A path escaping the analyzed tree must never become a file read."""
    (tmp_path / "secret.txt").write_text("classified", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    location = server_module.SourceLocation("x", "../secret.txt", 1, 1)
    with pytest.raises(FileNotFoundError):
        server_module.read_snippet(repo, location)


def test_locate_node_returns_none_for_unknown_ids() -> None:
    """Lookup failure is a value, not an exception, for the caller to shape."""
    assert server_module.locate_node({"entry_points": [], "functions": {}}, "fn_1") is None


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #

CONTROLLER = """
package com.up;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class PingController {
    @GetMapping("/ping")
    public String ping() { return "pong"; }
}
"""


def build_zip(members: "dict[str, str]") -> bytes:
    """Serialize ``{name: text}`` into zip bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def post_zip(client: "TestClient", payload: bytes, filename: str = "project.zip"):
    """Upload ``payload`` as a multipart file."""
    return client.post("/api/upload", files={"file": (filename, payload, "application/zip")})


def test_upload_replaces_the_loaded_repository(client: "TestClient") -> None:
    """After an upload the list reflects the archive, not the fixture."""
    response = post_zip(client, build_zip({"src/PingController.java": CONTROLLER}))
    assert response.status_code == 200

    body = response.json()
    assert body["repo"] == "project.zip"
    assert body["java_files"] == 1
    assert [(e["method"], e["path"]) for e in body["entry_points"]] == [("GET", "/ping")]

    # The change is durable, not just reflected in the upload response.
    assert client.get("/api/entry-points").json()["entry_points"][0]["path"] == "/ping"


def test_upload_clears_traces_from_the_previous_repository(
    client: "TestClient", stub_synthesis: "dict[str, int]"
) -> None:
    """Cached traces belong to the old code and must not survive."""
    client.get("/api/trace/ep_1")
    post_zip(client, build_zip({"PingController.java": CONTROLLER}))

    # ep_1 now means the uploaded project's first endpoint, freshly synthesized.
    assert client.get("/api/trace/ep_1").json()["cached"] is False
    assert stub_synthesis["count"] == 2


def test_upload_ignores_non_java_files(client: "TestClient") -> None:
    """Only Java source is extracted; everything else is never written."""
    payload = build_zip(
        {
            "PingController.java": CONTROLLER,
            "README.md": "# hi",
            "target/app.jar": "binary",
            "run.sh": "rm -rf /",
        }
    )
    assert post_zip(client, payload).json()["java_files"] == 1


@pytest.mark.parametrize(
    ("filename", "payload", "status", "fragment"),
    [
        ("notes.txt", b"hello", 400, "zip archive"),
        ("project.zip", b"definitely not a zip", 400, "not a valid zip"),
    ],
)
def test_upload_rejects_bad_input(
    client: "TestClient", filename: str, payload: bytes, status: int, fragment: str
) -> None:
    """Wrong extension and corrupt bytes both fail cleanly."""
    response = post_zip(client, payload, filename=filename)
    assert response.status_code == status
    assert fragment in response.json()["detail"]


def test_upload_rejects_archive_without_java(client: "TestClient") -> None:
    """An archive with nothing to analyze is a user error worth naming."""
    response = post_zip(client, build_zip({"README.md": "# hi"}))
    assert response.status_code == 400
    assert "No .java files" in response.json()["detail"]


def test_upload_leaves_previous_repository_loaded_on_failure(client: "TestClient") -> None:
    """A rejected upload must not leave the app with nothing loaded."""
    post_zip(client, b"not a zip")
    assert client.get("/api/entry-points").json()["repo"] == "demo-shop"


# -- Archive safety ------------------------------------------------------- #


@pytest.mark.parametrize(
    "hostile",
    [
        "../escaped.java",
        "../../escaped.java",
        "sub/../../escaped.java",
        "/absolute/escaped.java",
        "\\\\server\\share\\escaped.java",
        "C:/Windows/escaped.java",
    ],
)
def test_zip_slip_members_are_refused(tmp_path: Path, hostile: str) -> None:
    """No archive member may resolve outside the extraction directory."""
    destination = tmp_path / "extract"
    destination.mkdir()
    target = server_module.safe_member_target(destination, hostile)
    assert target is None or target.is_relative_to(destination.resolve())


def test_zip_slip_upload_writes_nothing_outside(tmp_path: Path, client: "TestClient") -> None:
    """End to end: a traversal entry is skipped, not written to disk."""
    canary = tmp_path / "canary.java"
    payload = build_zip({"../../../../../../canary.java": CONTROLLER, "ok/Ping.java": CONTROLLER})

    response = post_zip(client, payload)
    assert response.status_code == 200
    assert response.json()["java_files"] == 1
    assert not canary.exists()


def test_oversized_expansion_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zip bomb is stopped by bytes actually read, not the declared size."""
    monkeypatch.setattr(server_module, "MAX_EXTRACTED_BYTES", 1024)
    payload = build_zip({"Big.java": "x" * 20_000})
    destination = tmp_path / "extract"
    destination.mkdir()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        with pytest.raises(server_module.ArchiveError, match="expands beyond"):
            server_module.extract_java_sources(archive, destination)


def test_too_many_files_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file-count cap bounds work even when each file is tiny."""
    monkeypatch.setattr(server_module, "MAX_JAVA_FILES", 2)
    payload = build_zip({f"F{index}.java": "class F {}" for index in range(5)})
    destination = tmp_path / "extract"
    destination.mkdir()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        with pytest.raises(server_module.ArchiveError, match="more than"):
            server_module.extract_java_sources(archive, destination)


def test_oversized_upload_is_rejected(
    client: "TestClient", monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request body itself is capped before decompression is attempted."""
    monkeypatch.setattr(server_module, "MAX_UPLOAD_BYTES", 64)
    response = post_zip(client, build_zip({"PingController.java": CONTROLLER}))
    assert response.status_code == 413
    assert "larger than" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #


def test_load_env_file_sets_missing_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values come from the file when the environment has nothing."""
    monkeypatch.delenv("DIGITAL_TWIN_PROBE", raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\n\nDIGITAL_TWIN_PROBE=from-file\n", encoding="utf-8")

    assert load_env_file(env) == ["DIGITAL_TWIN_PROBE"]
    import os

    assert os.environ["DIGITAL_TWIN_PROBE"] == "from-file"


def test_real_environment_wins_over_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported variable is never overwritten by the file."""
    monkeypatch.setenv("DIGITAL_TWIN_PROBE", "from-shell")
    env = tmp_path / ".env"
    env.write_text("DIGITAL_TWIN_PROBE=from-file\n", encoding="utf-8")

    assert load_env_file(env) == []
    import os

    assert os.environ["DIGITAL_TWIN_PROBE"] == "from-shell"


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """No .env is a normal setup, not a failure."""
    assert load_env_file(tmp_path / "nope.env") == []
