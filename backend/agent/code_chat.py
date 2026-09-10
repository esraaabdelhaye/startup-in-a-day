"""Answer a developer's question about one specific piece of source code.

Where :mod:`agent.trace_synthesizer` reasons about the *shape* of a call graph
and never sees a line of Java, this module is the opposite: it sends the exact
source of one method and nothing else.  That split is why the manifest keeps
file paths and line numbers even though the compressed skeleton drops them.

The prompt is deliberately thin -- the snippet plus the question is the whole
context -- so answers stay grounded in the code that was actually read rather
than in guesses about the surrounding project.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import anthropic

from agent.trace_synthesizer import MODEL, TraceSynthesisError, build_client

__all__ = ["answer_question", "CodeChatError", "MAX_HISTORY_TURNS"]

#: Answers are read in a small panel, so they need room but not much.
MAX_TOKENS = 2048

#: Prior turns accepted for a follow-up question, oldest first.  Capped so a
#: long conversation cannot quietly grow the cost of every later question.
MAX_HISTORY_TURNS = 10

#: One line of framing: without it answers drift into essay length, which does
#: not fit the panel the user is reading them in.
SYSTEM_PROMPT = (
    "You answer a developer's questions about a specific Java method they are "
    "looking at. Be concise and concrete, and ground every claim in the code "
    "you were given. If the code alone cannot answer the question, say so."
)


class CodeChatError(RuntimeError):
    """Raised when a question cannot be answered."""


def answer_question(
    snippet: str,
    question: str,
    label: "Optional[str]" = None,
    history: "Optional[Sequence[dict[str, str]]]" = None,
) -> str:
    """Ask Claude a question about one method's source.

    Args:
        snippet: The exact source lines the question is about.
        question: The developer's question.
        label: Optional ``Class.method`` name, included so the model can refer
            to the method by name instead of "this code".
        history: Prior ``{"role", "content"}`` turns for a follow-up question.
            Only the most recent :data:`MAX_HISTORY_TURNS` are sent.

    Returns:
        The answer text.

    Raises:
        CodeChatError: If the question is empty or the API call fails.
    """
    if not question.strip():
        raise CodeChatError("question is empty")
    if not snippet.strip():
        raise CodeChatError("no source was found for that node")

    try:
        client = build_client()
    except TraceSynthesisError as error:
        # Same credential requirement as synthesis; reuse its message.
        raise CodeChatError(str(error)) from None

    messages = _build_messages(snippet, question, label, history)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
    except anthropic.AuthenticationError:
        raise CodeChatError("the configured ANTHROPIC_API_KEY was rejected") from None
    except anthropic.RateLimitError:
        raise CodeChatError("rate limited by the Claude API; retry shortly") from None
    except anthropic.APIStatusError as error:
        raise CodeChatError(f"Claude API returned HTTP {error.status_code}") from None
    except anthropic.APIConnectionError:
        raise CodeChatError("could not reach the Claude API; check connectivity") from None

    answer = "".join(block.text for block in response.content if block.type == "text").strip()
    if not answer:
        raise CodeChatError("the model returned an empty answer")
    return answer


def _build_messages(
    snippet: str,
    question: str,
    label: "Optional[str]",
    history: "Optional[Sequence[dict[str, str]]]",
) -> "list[dict[str, Any]]":
    """Assemble the conversation, with the snippet only in the first turn.

    Follow-ups reuse the original turn rather than resending the source, so a
    second question costs little more than the question itself.
    """
    heading = f"Here is a Java method, {label}:" if label else "Here is a Java method:"
    opening = f"{heading}\n\n```java\n{snippet}\n```"

    turns = list(history or [])[-MAX_HISTORY_TURNS:]
    if not turns:
        return [{"role": "user", "content": f"{opening}\n\nAnswer this question about it: {question}"}]

    messages: "list[dict[str, Any]]" = [{"role": "user", "content": opening}]
    messages.extend({"role": turn["role"], "content": turn["content"]} for turn in turns)
    messages.append({"role": "user", "content": question})
    return messages
