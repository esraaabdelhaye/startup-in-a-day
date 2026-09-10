"""Tests for Claude-backed trace synthesis.

Every test here stubs the API client: the suite must stay free, offline and
deterministic.  What is worth testing is not the model's prose but the code
around it -- fence tolerance, the single repair attempt, and the validation
that stops broken Mermaid from reaching the frontend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from agent import trace_synthesizer
from agent.trace_synthesizer import (
    MERMAID_PREFIX,
    Synthesis,
    TraceSynthesisError,
    build_prompt,
    parse_json_object,
    strip_code_fences,
    synthesize,
    synthesize_trace,
    validate_payload,
)

GOOD_MERMAID = "sequenceDiagram\n    participant OrderController\n    OrderController->>X: go()"
GOOD_PAYLOAD = {"narrative": "A request arrives and is handled.", "mermaid": GOOD_MERMAID}

SKELETON: "dict[str, Any]" = {
    "entry_point": "POST /orders/checkout -> createOrder(Order)",
    "trace": [
        {
            "id": "ep_1",
            "type": "entry_point",
            "signature": "POST /orders/checkout -> createOrder(Order)",
            "class": "OrderController",
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


class FakeBlock:
    """Stand-in for an SDK text content block."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeUsage:
    """Stand-in for an SDK usage object."""

    def __init__(self, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    """Stand-in for an SDK Message."""

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [FakeBlock(text)]
        self.usage = FakeUsage()
        self.stop_reason = stop_reason


class FakeMessages:
    """Records requests and replays a scripted list of responses."""

    def __init__(self, responses: "list[Any]") -> None:
        self._responses = list(responses)
        self.requests: "list[dict[str, Any]]" = []

    def create(self, **kwargs: Any) -> "FakeResponse":
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("the code made more API calls than the test scripted")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    """Stand-in for ``anthropic.Anthropic``."""

    def __init__(self, responses: "list[Any]") -> None:
        self.messages = FakeMessages(responses)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    """Install a scripted fake client and hand the test its recorder."""

    def install(*responses: Any) -> "FakeClient":
        client = FakeClient(list(responses))
        monkeypatch.setattr(trace_synthesizer, "build_client", lambda: client)
        return client

    return install


def reply(payload: "dict[str, Any]") -> "FakeResponse":
    """A response whose body is the JSON serialization of ``payload``."""
    return FakeResponse(json.dumps(payload))


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_returns_narrative_and_mermaid(api) -> None:
    """The documented return shape is exactly two keys."""
    api(reply(GOOD_PAYLOAD))
    assert synthesize_trace(SKELETON) == GOOD_PAYLOAD


def test_reports_token_usage(api) -> None:
    """``synthesize`` exposes what the call cost."""
    api(reply(GOOD_PAYLOAD))
    result = synthesize(SKELETON)
    assert isinstance(result, Synthesis)
    assert (result.input_tokens, result.output_tokens, result.attempts) == (100, 50, 1)


def test_sends_one_request_with_a_system_prompt(api) -> None:
    """A single call, with the contract in the system prompt."""
    client = api(reply(GOOD_PAYLOAD))
    synthesize_trace(SKELETON)
    assert len(client.messages.requests) == 1
    request = client.messages.requests[0]
    assert request["model"] == trace_synthesizer.MODEL
    assert "JSON" in request["system"]
    assert len(request["messages"]) == 1


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def test_prompt_contains_the_trace_and_entry_point() -> None:
    """Everything the model needs is in the one message."""
    prompt = build_prompt(SKELETON)
    assert SKELETON["entry_point"] in prompt
    assert "orderRepository.save(order)" in prompt
    assert "sequenceDiagram" in prompt


def test_prompt_serializes_the_trace_compactly() -> None:
    """The skeleton exists to save tokens; indenting it would give them back."""
    prompt = build_prompt(SKELETON)
    assert '{"id":"ep_1"' in prompt
    assert '{\n  "id"' not in prompt


def test_prompt_carries_no_file_or_line_data() -> None:
    """Locations never reach the model, because the skeleton never had them."""
    prompt = build_prompt(SKELETON)
    for leaked in ("line_start", "line_end", ".java", "warnings"):
        assert leaked not in prompt


# --------------------------------------------------------------------------- #
# Defensive parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "wrapped",
    [
        "```json\n{}\n```",
        "```\n{}\n```",
        "  ```JSON\n{}\n```  ",
    ],
)
def test_strip_code_fences_removes_wrappers(wrapped: str) -> None:
    """Fences in any of their usual shapes come off."""
    assert strip_code_fences(wrapped) == "{}"


def test_strip_code_fences_leaves_bare_text_alone() -> None:
    """Unfenced content is returned untouched apart from whitespace."""
    assert strip_code_fences('  {"a": 1}  ') == '{"a": 1}'


def test_fenced_response_parses_without_a_retry(api) -> None:
    """A fenced reply is recovered in-process rather than costing another call."""
    client = api(FakeResponse(f"```json\n{json.dumps(GOOD_PAYLOAD)}\n```"))
    assert synthesize_trace(SKELETON) == GOOD_PAYLOAD
    assert len(client.messages.requests) == 1


def test_non_object_json_is_rejected() -> None:
    """A bare list is not a valid response."""
    with pytest.raises(TraceSynthesisError, match="expected a JSON object"):
        parse_json_object("[1, 2]")


def test_empty_response_is_rejected() -> None:
    """An empty body fails before json.loads sees it."""
    with pytest.raises(TraceSynthesisError, match="empty response"):
        parse_json_object("   ")


# --------------------------------------------------------------------------- #
# The single repair attempt
# --------------------------------------------------------------------------- #


def test_invalid_json_triggers_exactly_one_retry(api) -> None:
    """The model is shown its own output and asked again -- once."""
    client = api(FakeResponse("Sure! Here you go:"), reply(GOOD_PAYLOAD))
    assert synthesize_trace(SKELETON) == GOOD_PAYLOAD

    assert len(client.messages.requests) == 2
    second = client.messages.requests[1]["messages"]
    assert [message["role"] for message in second] == ["user", "assistant", "user"]
    assert second[1]["content"] == "Sure! Here you go:"
    assert "not valid JSON" in second[2]["content"]


def test_retry_usage_is_accumulated(api) -> None:
    """Both calls are counted, so budget tracking stays honest."""
    api(FakeResponse("nope"), reply(GOOD_PAYLOAD))
    result = synthesize(SKELETON)
    assert (result.input_tokens, result.output_tokens, result.attempts) == (200, 100, 2)


def test_second_invalid_response_raises(api) -> None:
    """There is no third attempt."""
    client = api(FakeResponse("nope"), FakeResponse("still nope"))
    with pytest.raises(TraceSynthesisError, match="not valid JSON"):
        synthesize_trace(SKELETON)
    assert len(client.messages.requests) == 2


def test_valid_json_with_bad_mermaid_does_not_retry(api) -> None:
    """Only *parse* failures are retried; a bad diagram is a hard error."""
    client = api(reply({"narrative": "n", "mermaid": "graph TD; A-->B"}))
    with pytest.raises(TraceSynthesisError, match=MERMAID_PREFIX):
        synthesize_trace(SKELETON)
    assert len(client.messages.requests) == 1


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_mermaid_must_start_with_sequence_diagram() -> None:
    """A flowchart is not what the frontend is going to render."""
    with pytest.raises(TraceSynthesisError, match="must start with"):
        validate_payload({"narrative": "n", "mermaid": "flowchart LR\n  A-->B"})


@pytest.mark.parametrize("bad", ["", "   ", None, 42, ["sequenceDiagram"]])
def test_missing_or_non_string_mermaid_is_rejected(bad: Any) -> None:
    """Empty, blank and wrong-typed diagrams all fail the same way."""
    with pytest.raises(TraceSynthesisError, match="mermaid"):
        validate_payload({"narrative": "n", "mermaid": bad})


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
def test_missing_or_non_string_narrative_is_rejected(bad: Any) -> None:
    """The narrative is half the product; an empty one is a failure."""
    with pytest.raises(TraceSynthesisError, match="narrative"):
        validate_payload({"narrative": bad, "mermaid": GOOD_MERMAID})


def test_mermaid_fenced_inside_the_json_string_is_unwrapped() -> None:
    """Models sometimes fence the diagram *within* an otherwise valid object."""
    narrative, mermaid = validate_payload(
        {"narrative": "n", "mermaid": f"```mermaid\n{GOOD_MERMAID}\n```"}
    )
    assert narrative == "n"
    assert mermaid == GOOD_MERMAID


def test_narrative_is_stripped() -> None:
    """Leading and trailing whitespace does not reach the frontend."""
    narrative, _ = validate_payload({"narrative": "  spaced  ", "mermaid": GOOD_MERMAID})
    assert narrative == "spaced"


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_missing_api_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key means a readable message, not an SDK stack trace."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(TraceSynthesisError, match="ANTHROPIC_API_KEY is not set"):
        synthesize_trace(SKELETON)


def test_connection_error_is_mapped(api) -> None:
    """SDK exceptions become TraceSynthesisError so callers need no SDK import."""
    api(anthropic.APIConnectionError(request=_request_stub()))
    with pytest.raises(TraceSynthesisError, match="could not reach"):
        synthesize_trace(SKELETON)


def test_rate_limit_error_is_mapped(api) -> None:
    """A 429 is reported as retryable in plain language."""
    request = _request_stub()
    api(
        anthropic.RateLimitError(
            "rate limited", response=httpx2.Response(429, request=request), body=None
        )
    )
    with pytest.raises(TraceSynthesisError, match="rate limited"):
        synthesize_trace(SKELETON)


def test_api_key_never_appears_in_an_error(api, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret must not leak into logs through an exception message.

    Exercised on the SDK failure path, where an unguarded implementation would
    be most tempted to echo request or client state into the message.
    """
    secret = "sk-ant-test-do-not-log"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    request = _request_stub()
    request.headers["x-api-key"] = secret
    api(
        anthropic.AuthenticationError(
            "invalid key", response=httpx2.Response(401, request=request), body=None
        )
    )
    with pytest.raises(TraceSynthesisError) as caught:
        synthesize_trace(SKELETON)
    assert secret not in str(caught.value)
    assert "rejected" in str(caught.value)


def _request_stub() -> "httpx2.Request":
    """A minimal request object for constructing SDK exceptions."""
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_empty_trace_is_rejected_before_calling_the_api(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to describe means no request is made at all."""
    client = api(reply(GOOD_PAYLOAD))
    with pytest.raises(TraceSynthesisError, match="empty trace"):
        synthesize_trace({"entry_point": "x", "trace": []})
    assert client.messages.requests == []


def test_truncated_output_is_reported(api) -> None:
    """Hitting max_tokens is called out instead of failing as bad JSON."""
    api(FakeResponse('{"narrative": "cut off', stop_reason="max_tokens"))
    with pytest.raises(TraceSynthesisError, match="truncated"):
        synthesize_trace(SKELETON)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def manifest_file(tmp_path: Path) -> Path:
    """Write a one-endpoint manifest the CLI can compress."""
    manifest = {
        "entry_points": [
            {
                "id": "ep_1",
                "method": "POST",
                "path": "/orders/checkout",
                "function": "createOrder",
                "class": "OrderController",
                "parameters": [],
                "calls": [],
            }
        ],
        "functions": {},
        "sinks": [],
        "warnings": [],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_cli_prints_result_and_usage(
    tmp_path: Path, api, capsys: pytest.CaptureFixture[str]
) -> None:
    """Result JSON on stdout, token accounting on stderr."""
    api(reply(GOOD_PAYLOAD))
    assert trace_synthesizer.main([str(manifest_file(tmp_path)), "ep_1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == GOOD_PAYLOAD
    assert "tokens" in captured.err


def test_cli_reports_unknown_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad id fails before any API call."""
    assert trace_synthesizer.main([str(manifest_file(tmp_path)), "ep_99"]) == 2
    assert "ep_99" in capsys.readouterr().err


def test_cli_reports_synthesis_failure(
    tmp_path: Path, api, capsys: pytest.CaptureFixture[str]
) -> None:
    """A synthesis error exits 1 with a message, not a traceback."""
    api(FakeResponse("nope"), FakeResponse("still nope"))
    assert trace_synthesizer.main([str(manifest_file(tmp_path)), "ep_1"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_reports_missing_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing file is a clean error."""
    assert trace_synthesizer.main([str(tmp_path / "nope.json"), "ep_1"]) == 2
    assert "could not read" in capsys.readouterr().err
