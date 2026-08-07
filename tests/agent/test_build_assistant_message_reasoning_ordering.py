"""
Regression tests for the "reasoning appears after the message" bug.

The user-reported symptom: reasoning text shows up after the assistant
content, never making it to the reasoning box.

We test ``build_assistant_message`` directly because the display layer
in ``cli.py`` only knows about ``result['last_reasoning']`` — if the
builder does not put reasoning into ``msg['reasoning']`` and strip it
from ``msg['content']``, the display layer cannot recover.

Each test maps to a real provider shape observed in the wild:

* DeepSeek v4 / Kimi / MiniMax M3 (list-typed content blocks) — refs #21944
* MiniMax M2.7 / NIM (unterminated <think>) — refs #8878 / #9568
* Closed <think> inline (the common path)
* OpenRouter unified reasoning_details
* Empty content + reasoning-only (DeepSeek v4 tool-call step)
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"Test tool {n}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_assistant_msg(
    content=None,
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
    reasoning_details=None,
):
    """SimpleNamespace mimicking OpenAI ChatCompletionMessage."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
    if reasoning_details is not None:
        msg.reasoning_details = reasoning_details
    return msg


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


# ── BUG REGRESSION: list-typed content (DeepSeek v4 / Kimi / MiniMax M3) ──


class TestListTypedContentReasoning:
    """List-typed content blocks must:
       1. NOT crash _sanitize_surrogates
       2. capture 'thinking'/'reasoning' typed blocks into msg['reasoning']
       3. strip the thinking blocks from msg['content'] (which becomes the
          concatenation of remaining text blocks)
       4. leave content non-empty when there are text blocks
    """

    def test_does_not_crash(self, agent):
        msg = _mock_assistant_msg(
            content=[
                {"type": "thinking", "thinking": "Let me work this out"},
                {"type": "text", "text": "The answer is 42."},
            ],
        )
        # Must not raise TypeError
        result = agent._build_assistant_message(msg, "stop")
        assert result is not None

    def test_thinking_block_captured_as_reasoning(self, agent):
        msg = _mock_assistant_msg(
            content=[
                {"type": "thinking", "thinking": "Let me work this out"},
                {"type": "text", "text": "The answer is 42."},
            ],
        )
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] is not None
        assert "Let me work this out" in result["reasoning"]

    def test_thinking_block_stripped_from_content(self, agent):
        msg = _mock_assistant_msg(
            content=[
                {"type": "thinking", "thinking": "Let me work this out"},
                {"type": "text", "text": "The answer is 42."},
            ],
        )
        result = agent._build_assistant_message(msg, "stop")
        # The "thinking" text must NOT appear in the user-visible content
        assert "Let me work this out" not in result["content"]
        # The actual answer must still be there
        assert "The answer is 42." in result["content"]

    def test_reasoning_typed_block_captured(self, agent):
        """Some providers use 'reasoning' as the type, not 'thinking'."""
        msg = _mock_assistant_msg(
            content=[
                {"type": "reasoning", "text": "Internal chain of thought"},
                {"type": "text", "text": "Final answer"},
            ],
        )
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] is not None
        assert "Internal chain of thought" in result["reasoning"]
        assert "Internal chain of thought" not in result["content"]


# ── BUG REGRESSION: unterminated <think> block (MiniMax M2.7 / NIM) ──


class TestUnterminatedThinkBlockReasoning:
    """Unterminated <think> blocks must be captured into reasoning AND
    not leave the entire content empty (the model still emits the answer
    AFTER the open tag)."""

    def test_unterminated_block_captures_pre_close_content(self, agent):
        msg = _mock_assistant_msg(
            content="<think>Let me work this out\n\nThe answer is 42.",
        )
        result = agent._build_assistant_message(msg, "stop")
        # Reasoning must be captured
        assert result["reasoning"] is not None, (
            "Unterminated <think> produced no reasoning capture. "
            "This means the post-response display will show NO reasoning "
            "and the user will see the model output with the raw "
            "thinking text still inline (because content is empty "
            "when the strip regex incorrectly consumes the answer too)."
        )

    def test_unterminated_block_preserves_answer_text(self, agent):
        """When MiniMax M2.7 sends <think>...answer with no close tag,
        the answer must survive in content, not be eaten by the strip pass."""
        msg = _mock_assistant_msg(
            content="<think>Let me work this out\n\nThe answer is 42.",
        )
        result = agent._build_assistant_message(msg, "stop")
        # The user-visible answer must be present
        assert "The answer is 42." in result["content"], (
            f"Answer was stripped along with the reasoning. "
            f"content={result['content']!r}, reasoning={result['reasoning']!r}"
        )

    def test_unterminated_block_after_prior_line_preserves_answer(self, agent):
        """When the model emits text on the first line, then an unterminated
        think block on the second line followed by the answer, the prior-line
        text up to the think tag must survive AND the answer after the think
        block must survive.

        This is the 'Some text first\\n<think>think\\n\\nanswer' pattern that
        was missed because the unterminated-extraction pattern only matched at
        ^ (start of string), not after a newline.  The strip pass then ate
        everything from the \\n before the tag to end-of-string, consuming both
        the think content and the answer.  Refs the 2026-06-09 investigation.
        """
        msg = _mock_assistant_msg(
            content="Some text first\n<think>Let me work this out\n\nThe answer is 42.",
        )
        result = agent._build_assistant_message(msg, "stop")
        # The answer after the think block must survive
        assert "The answer is 42." in result["content"], (
            f"Answer was stripped. content={result['content']!r}"
        )
        # The thinking must be captured
        assert result["reasoning"] is not None, (
            f"Thinking not captured. content={result['content']!r}"
        )


# ── BUG REGRESSION: closed <think> inline (the common path) ──


class TestClosedThinkBlockReasoning:
    """Closed <think>...</think> blocks must be captured into reasoning
    and stripped from content."""

    def test_closed_block_captured_and_stripped(self, agent):
        msg = _mock_assistant_msg(
            content="<think>Let me think about this carefully</think>\n\nThe answer is 42.",
        )
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] == "Let me think about this carefully"
        assert result["content"] == "The answer is 42."
        assert "<think>" not in result["content"]
        assert "Let me think" not in result["content"]


# ── BUG REGRESSION: structured reasoning fields (OpenRouter / DeepSeek v4 SDK) ──


class TestStructuredReasoningFields:
    """When the provider sets msg.reasoning or msg.reasoning_content
    directly, that wins. Content should still be clean."""

    def test_sdk_reasoning_field_wins(self, agent):
        msg = _mock_assistant_msg(
            content="The answer is 42.",
            reasoning="summary chain of thought",
        )
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] == "summary chain of thought"
        assert result["content"] == "The answer is 42."

    def test_reasoning_details_extracted(self, agent):
        details = [{"type": "reasoning.summary", "text": "step1", "signature": "sig1"}]
        msg = _mock_assistant_msg(
            content="The answer is 42.",
            reasoning_details=details,
        )
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] is not None
        assert "step1" in result["reasoning"]
        assert "step1" not in result["content"]


# ── BUG REGRESSION: reasoning-only / thinking-only messages ──


class TestReasoningOnlyMessages:
    """A message that only has reasoning (no text content) must:
       1. populate msg['reasoning']
       2. have empty msg['content']
       3. NOT include the raw reasoning in content
    """

    def test_thinking_only_via_inline_tag(self, agent):
        msg = _mock_assistant_msg(content="<think>just thinking, no answer yet</think>")
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] is not None
        assert "just thinking" in result["reasoning"]
        assert "just thinking" not in result["content"]

    def test_thinking_only_via_list_block(self, agent):
        msg = _mock_assistant_msg(
            content=[{"type": "thinking", "thinking": "Internal only"}],
        )
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] is not None
        assert "Internal only" in result["reasoning"]
        assert "Internal only" not in result["content"]
