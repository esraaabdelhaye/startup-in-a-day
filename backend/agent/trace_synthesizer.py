"""Turn a compressed skeleton into a narrative and a Mermaid sequence diagram.

This is the stage after :mod:`parser.compress`: one Claude call per selected
entry point, producing the two things the frontend renders -- a short
plain-English account of what happens, and the same chain as Mermaid.js
``sequenceDiagram`` source.

The skeleton exists to keep this call cheap, so the prompt around it stays
deliberately small: a short system prompt, the trace as compact JSON, and the
response contract.  There is no retry beyond a single JSON-repair attempt and
no caching -- at one call per user click, neither would pay for itself.

Usage::

    python -m agent.trace_synthesizer <manifest.json> <entry_point_id>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import anthropic

from parser.compress import UnknownEntryPointError, compress_for_agent

__all__ = ["synthesize_trace", "synthesize", "Synthesis", "TraceSynthesisError", "main"]


#: Model used for synthesis.  Sonnet is enough for a formatting-and-summary
#: task; change this one constant to move the whole pipeline to another model.
MODEL = "claude-sonnet-4-6"

#: Headroom for a long diagram.  Only tokens actually generated are billed, so
#: this is a truncation guard rather than a cost lever.
MAX_TOKENS = 8192

#: Every Mermaid sequence diagram must open with this keyword.
MERMAID_PREFIX = "sequenceDiagram"

SYSTEM_PROMPT = (
    "You explain Spring Boot execution traces to developers seeing the codebase "
    "for the first time. Reply with a single JSON object and nothing else: no "
    "markdown fences, no preamble, no commentary."
)

RESPONSE_CONTRACT = (
    'Return a JSON object with exactly two string keys:\n'
    '"narrative": 2-4 sentences, chronological, plain English, for a developer '
    "unfamiliar with this codebase. Describe what happens, not what the JSON says.\n"
    f'"mermaid": Mermaid.js sequenceDiagram source for the same chain, beginning '
    f'with "{MERMAID_PREFIX}". Declare one participant per distinct class in the '
    "trace and show the calls between them in order. Render each sink as a call to "
    "the receiver named in its signature."
)

JSON_REPAIR_PROMPT = (
    "That was not valid JSON. Resend the same content as a single raw JSON object "
    "with the keys \"narrative\" and \"mermaid\". No markdown fences, no other text."
)

#: Matches a whole response wrapped in a fenced code block.
_FENCED = re.compile(r"\A\s*```[A-Za-z0-9_+-]*\s*\n(?P<body>.*?)\n?\s*```\s*\Z", re.DOTALL)


class TraceSynthesisError(RuntimeError):
    """Raised when synthesis cannot produce a usable narrative and diagram."""


@dataclass(frozen=True)
class Synthesis:
    """A completed synthesis plus what the call cost.

    Token counts are carried so callers can track spend against a budget;
    :func:`synthesize_trace` drops them to return the documented shape.
    """

    narrative: str
    mermaid: str
    input_tokens: int
    output_tokens: int
    attempts: int


def synthesize_trace(skeleton: "dict[str, Any]") -> "dict[str, Any]":
    """Produce a narrative and Mermaid diagram for a compressed skeleton.

    Args:
        skeleton: Output of :func:`parser.compress.compress_for_agent`.

    Returns:
        ``{"narrative": str, "mermaid": str}``, with the Mermaid source
        validated to be non-empty and to start with ``sequenceDiagram``.

    Raises:
        TraceSynthesisError: If the key is missing, the API call fails, or the
            model does not return usable JSON after one repair attempt.
    """
    result = synthesize(skeleton)
    return {"narrative": result.narrative, "mermaid": result.mermaid}


def synthesize(skeleton: "dict[str, Any]") -> "Synthesis":
    """Same as :func:`synthesize_trace`, but also reports token usage."""
    if not skeleton.get("trace"):
        raise TraceSynthesisError("skeleton has an empty trace; nothing to synthesize")

    client = build_client()
    messages: "list[dict[str, Any]]" = [{"role": "user", "content": build_prompt(skeleton)}]

    text, usage = _request(client, messages)
    attempts = 1
    try:
        payload = parse_json_object(text)
    except TraceSynthesisError:
        # One repair attempt: show the model its own output and ask again.
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": JSON_REPAIR_PROMPT})
        retry_text, retry_usage = _request(client, messages)
        attempts = 2
        usage = (usage[0] + retry_usage[0], usage[1] + retry_usage[1])
        payload = parse_json_object(retry_text)

    narrative, mermaid = validate_payload(payload)
    return Synthesis(
        narrative=narrative,
        mermaid=mermaid,
        input_tokens=usage[0],
        output_tokens=usage[1],
        attempts=attempts,
    )


def build_client() -> "anthropic.Anthropic":
    """Construct an API client from ``ANTHROPIC_API_KEY``.

    The key is read by the SDK from the environment and is never logged or
    included in an error message.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise TraceSynthesisError(
            "ANTHROPIC_API_KEY is not set; export it before running trace synthesis"
        )
    return anthropic.Anthropic()


def build_prompt(skeleton: "dict[str, Any]") -> str:
    """Render the single user message sent to the model.

    The trace is serialized without whitespace: it is machine input, and
    indentation would be pure token overhead.
    """
    trace = json.dumps(skeleton["trace"], separators=(",", ":"), ensure_ascii=False)
    return (
        f"Entry point: {skeleton.get('entry_point', '')}\n"
        f"Call graph (each node lists the ids it calls):\n{trace}\n\n"
        f"{RESPONSE_CONTRACT}"
    )


def _request(
    client: "anthropic.Anthropic", messages: "list[dict[str, Any]]"
) -> "tuple[str, tuple[int, int]]":
    """Send one request and return its text plus ``(input, output)`` tokens.

    SDK errors are re-raised as :class:`TraceSynthesisError` so callers do not
    have to depend on ``anthropic`` exception types, and so nothing from the
    request environment leaks into the message.
    """
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
    except anthropic.AuthenticationError:
        raise TraceSynthesisError("the configured ANTHROPIC_API_KEY was rejected") from None
    except anthropic.RateLimitError:
        raise TraceSynthesisError("rate limited by the Claude API; retry shortly") from None
    except anthropic.APIStatusError as error:
        raise TraceSynthesisError(f"Claude API returned HTTP {error.status_code}") from None
    except anthropic.APIConnectionError:
        raise TraceSynthesisError("could not reach the Claude API; check connectivity") from None

    if response.stop_reason == "max_tokens":
        raise TraceSynthesisError(
            f"model output hit the {MAX_TOKENS}-token limit and was truncated"
        )

    text = "".join(block.text for block in response.content if block.type == "text")
    usage = (response.usage.input_tokens, response.usage.output_tokens)
    return text, usage


def strip_code_fences(text: str) -> str:
    """Remove a surrounding markdown code fence, if the model added one."""
    match = _FENCED.match(text)
    return match.group("body").strip() if match else text.strip()


def parse_json_object(text: str) -> "dict[str, Any]":
    """Parse a model response into a JSON object, tolerating code fences."""
    candidate = strip_code_fences(text)
    if not candidate:
        raise TraceSynthesisError("model returned an empty response")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise TraceSynthesisError(f"model response was not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise TraceSynthesisError(f"model returned {type(payload).__name__}, expected a JSON object")
    return payload


def validate_payload(payload: "dict[str, Any]") -> "tuple[str, str]":
    """Check both fields are present and the Mermaid source is well-formed.

    Broken diagram syntax is surfaced here rather than passed on: the frontend
    renders this string directly, and a silent failure there is far harder to
    diagnose than a loud one here.
    """
    narrative = payload.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        raise TraceSynthesisError("model response is missing a non-empty 'narrative'")

    mermaid = payload.get("mermaid")
    if not isinstance(mermaid, str) or not mermaid.strip():
        raise TraceSynthesisError("model response is missing a non-empty 'mermaid'")

    # The model occasionally fences the diagram inside the JSON string itself.
    mermaid = strip_code_fences(mermaid)
    if not mermaid.startswith(MERMAID_PREFIX):
        first_line = mermaid.splitlines()[0] if mermaid.splitlines() else ""
        raise TraceSynthesisError(
            f"Mermaid source must start with {MERMAID_PREFIX!r}, got {first_line!r}"
        )
    return narrative.strip(), mermaid


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    """CLI entry point: manifest + entry point id -> narrative and diagram."""
    arguments = _parse_args(argv)

    try:
        manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"error: could not read {arguments.manifest}: {error}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as error:
        print(f"error: {arguments.manifest} is not valid JSON: {error}", file=sys.stderr)
        return 2

    try:
        skeleton = compress_for_agent(manifest, arguments.entry_point_id)
    except UnknownEntryPointError as error:
        print(f"error: {error.args[0]}", file=sys.stderr)
        return 2

    try:
        result = synthesize(skeleton)
    except TraceSynthesisError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"narrative": result.narrative, "mermaid": result.mermaid},
            indent=arguments.indent,
            ensure_ascii=False,
        )
    )
    print(
        f"model: {MODEL} | input: {result.input_tokens:,} tokens | "
        f"output: {result.output_tokens:,} tokens | api calls: {result.attempts}",
        file=sys.stderr,
    )
    return 0


def _parse_args(argv: "Optional[Sequence[str]]") -> argparse.Namespace:
    """Build and run the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m agent.trace_synthesizer",
        description="Synthesize a narrative and Mermaid sequence diagram for one entry point.",
    )
    parser.add_argument("manifest", type=Path, help="path to a manifest JSON file")
    parser.add_argument("entry_point_id", help="id of the selected entry point, e.g. ep_1")
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
