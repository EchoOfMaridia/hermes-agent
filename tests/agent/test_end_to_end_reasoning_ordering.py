"""
End-to-end test for the "reasoning after message" bug.

This test exercises the full flow:
  1. API response with each known failure mode
  2. ``_build_assistant_message`` produces a normalised msg dict
  3. The msg is appended to ``messages`` (mimicking the conversation loop)
  4. ``last_reasoning`` is computed by walking messages backwards
  5. We assert that ``last_reasoning`` matches the thinking AND
     ``last message's content`` is just the answer — never the thinking
     or a raw tag.

If the bug is dead, every scenario shows reasoning present in
``last_reasoning`` and absent from the final ``content``.
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


def _mock_assistant_msg(content=None, reasoning=None, reasoning_content=None):
    msg = SimpleNamespace(content=content, tool_calls=None)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
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


def _walk_messages_for_last_reasoning(messages: list[dict]) -> "str | None":
    """Mirror conversation_loop.py's last_reasoning resolution."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            break
        if msg.get("role") == "assistant" and msg.get("reasoning"):
            return msg["reasoning"]
    return None


@pytest.mark.parametrize(
    "scenario_name, api_msg, expected_thinking_substring, expected_answer_substring",
    [
        (
            "m3_list_typed",
            _mock_assistant_msg(
                content=[
                    {"type": "thinking", "thinking": "User wants to know the answer."},
                    {"type": "text", "text": "The answer is 4."},
                ],
            ),
            "User wants to know",
            "The answer is 4.",
        ),
        (
            "unterminated_with_answer",
            _mock_assistant_msg(
                content="<think>Let me work this out\n\nThe answer is 4.",
            ),
            "Let me work this out",
            "The answer is 4.",
        ),
        (
            "unterminated_pure_thinking",
            _mock_assistant_msg(
                content="<think>Just thinking, no answer.",
            ),
            "Just thinking",
            "",
        ),
        (
            "closed_inline",
            _mock_assistant_msg(
                content="<think>Let me calculate 2+2.</think>\n\nThe answer is 4.",
            ),
            "Let me calculate 2+2",
            "The answer is 4.",
        ),
        (
            "structured_field",
            _mock_assistant_msg(
                content="The answer is 4.",
                reasoning="Internal thought process",
            ),
            "Internal thought process",
            "The answer is 4.",
        ),
    ],
)
def test_full_pipeline_no_reasoning_leak(
    agent, scenario_name, api_msg, expected_thinking_substring, expected_answer_substring,
):
    """Each scenario: reasoning captured, content clean, message-walk finds
    reasoning, content has no thinking tags or thinking text."""
    msg = agent._build_assistant_message(api_msg, "stop")
    messages = [
        {"role": "user", "content": "What is 2+2?"},
        msg,
    ]

    # 1. The persisted message must have reasoning in the right field
    assert msg["reasoning"], (
        f"[{scenario_name}] msg['reasoning'] is empty — reasoning will never "
        f"be shown to the user. Bug regression."
    )
    assert expected_thinking_substring in msg["reasoning"], (
        f"[{scenario_name}] expected {expected_thinking_substring!r} in "
        f"reasoning, got {msg['reasoning']!r}"
    )

    # 2. Content must be clean — no raw tags, no thinking text
    assert "<think>" not in str(msg["content"])
    assert "</think>" not in str(msg["content"])
    assert "<think>" not in str(msg["content"])
    assert expected_thinking_substring not in str(msg["content"]), (
        f"[{scenario_name}] thinking text leaked into content: {msg['content']!r}"
    )

    # 3. Answer must be present (when one is expected)
    if expected_answer_substring:
        assert expected_answer_substring in str(msg["content"]), (
            f"[{scenario_name}] expected answer {expected_answer_substring!r} "
            f"in content, got {msg['content']!r}"
        )

    # 4. The conversation-loop's last_reasoning walk must find the reasoning
    last_reasoning = _walk_messages_for_last_reasoning(messages)
    assert last_reasoning is not None, (
        f"[{scenario_name}] last_reasoning walk returned None — the CLI "
        f"display layer will skip the reasoning box and the user will see "
        f"the response without the corresponding reasoning."
    )
    assert expected_thinking_substring in last_reasoning
