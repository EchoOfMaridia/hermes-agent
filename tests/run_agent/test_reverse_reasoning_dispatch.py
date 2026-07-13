"""Regression tests for the "reverse reasoning" dispatch bug.

Symptom (intermittent, hermes TUI):
    The reasoning panel and the response bubble render out of temporal
    order. The reasoning pane contains "I need to: 1. find ... 2. ... 3. give URL"
    while the visible response already narrates a tool call that the model
    has produced AND a list of routes the user can hit (a past-tense
    summary of completed steps). The reasoning block reads as future-tense
    planning while the response is in past tense — the panels are inverted
    relative to model intent.

Root-cause hypotheses (any of these would surface this class):

    A. The two streaming callbacks (``reasoning_callback`` and
       ``stream_delta_callback``) are fired out of arrival order, so a
       later reasoning chunk populates the reasoning panel BEFORE the
       earlier content chunk reaches the response panel.
    B. A reasoning token enters ``stream_delta_callback`` (content
       pipeline) while a content token enters ``reasoning_callback``
       (reasoning pipeline) — the routing is reversed.
    C. State retention across turns: reasoning text carried in a prior
       turn's buffer leaks into the current turn's reasoning display
       when the per-turn reset is broken.

These tests pin the routing/dispatch invariants so that any regression
to the bug class fails fast and points to the exact site.

Coverage targets:

    TestFireReasoningDeltaRoutingInvariant
        _fire_reasoning_delta fires ONLY the reasoning callback (and any
        secondary listener) and never the content/stream callback.

    TestInterleavedReasoningContentOrder
        When chunks arrive interleaved (R, C, R, C), the callback
        invocation sequence matches the chunk arrival order.

    TestReasoningOnlyChunkDoesNotEmitContent
        A chunk with reasoning_content but no delta.content must NOT
        produce a stream_delta_callback fire — content panel stays empty.

    TestContentOnlyChunkDoesNotEmitReasoning
        Symmetric: content-only chunk must not fire reasoning_callback.

    TestReasoningPanelBufferIsolationAcrossTurns
        Reasoning accumulated during turn N must not be visible to turn
        N+1's reasoning callback. (Per-pin of TestLastReasoningPerTurn
        but at the dispatch-streaming level, before messages persist.)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_agent():
    """Bare AIAgent stub — no client, no init, just the callback slots."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent.verbose_logging = False
    return agent


def _make_stream_chunk(
    content=None,
    tool_calls=None,
    finish_reason=None,
    model=None,
    reasoning_content=None,
    usage=None,
):
    """Mimic OpenAI ChatCompletionChunk for streaming loops in chat_completion_helpers."""
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=usage)


# ── Test 1: routing invariant ────────────────────────────────────────────────


class TestFireReasoningDeltaRoutingInvariant:
    """A reasoning delta must NEVER reach the content/stream callback."""

    def test_pure_reasoning_only_calls_reasoning_callback(self):
        agent = _make_agent()
        reasoning_captured = []
        content_captured = []
        agent.reasoning_callback = lambda t: reasoning_captured.append(t)
        agent.stream_delta_callback = lambda t: content_captured.append(t)

        agent._fire_reasoning_delta("thinking...")

        assert reasoning_captured == ["thinking..."]
        assert content_captured == [], (
            "REVERSE-REASONING BUG: reasoning delta reached the content "
            "callback. Reasoning text would leak into the response panel."
        )

    def test_pure_reasoning_with_no_callback_set_is_safe(self):
        """No callback registered: _fire_reasoning_delta is a no-op."""
        agent = _make_agent()
        # Both callbacks are None — must not raise.
        agent._fire_reasoning_delta("thinking...")

    def test_reasoning_callback_exception_does_not_propagate(self):
        """Callback errors are swallowed by the existing try/except."""
        agent = _make_agent()

        def _explode(_text):
            raise RuntimeError("display layer is on fire")

        agent.reasoning_callback = _explode
        # Must not raise into the streaming loop.
        agent._fire_reasoning_delta("thinking...")


# ── Test 2: temporal order under interleaved chunks ───────────────────────────


class TestInterleavedReasoningContentOrder:
    """R-C-R-C-R-C chunks → callbacks fire in arrival order.

    This is the load-bearing test for the screenshot's "panels inverted"
    symptom. The streaming loop processes chunks one at a time. For each
    chunk with reasoning, the reasoning_callback fires in chunk order.
    For each chunk with content, the stream_delta_callback fires in chunk
    order. If the dispatch reorders them, the reasoning pane accumulates
    future-tense thoughts while the response pane accumulates past-tense
    actions — visually, the panels look inverted.
    """

    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_interleaved_chunks_preserve_callback_order(
        self, mock_create, mock_close
    ):
        from run_agent import AIAgent

        chunks = [
            _make_stream_chunk(reasoning_content="Reason-A1"),
            _make_stream_chunk(content="Content-A1"),
            _make_stream_chunk(reasoning_content="Reason-A2"),
            _make_stream_chunk(content="Content-A2"),
            _make_stream_chunk(reasoning_content="Reason-A3"),
            _make_stream_chunk(content="Content-A3"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]

        # Track callback invocations AS A STREAM so we can assert order.
        events = []

        def _capture(stream_tag):
            def _cb(text):
                events.append((stream_tag, text))
            return _cb

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)
        mock_create.return_value = mock_client

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            stream_delta_callback=_capture("content"),
            reasoning_callback=_capture("reasoning"),
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False

        agent._interruptible_streaming_api_call({})

        # Drain stream callbacks that fire AFTER the response is built.
        from run_agent import AIAgent as _AIAgent
        # The streamed order should be R1, C1, R2, C2, R3, C3.
        assert events == [
            ("reasoning", "Reason-A1"),
            ("content", "Content-A1"),
            ("reasoning", "Reason-A2"),
            ("content", "Content-A2"),
            ("reasoning", "Reason-A3"),
            ("content", "Content-A3"),
        ], (
            f"REVERSE-REASONING BUG: callback dispatch order diverged from "
            f"chunk arrival order. Got: {events}"
        )


# ── Test 3: reasoning-only chunk must not emit content ────────────────────────


class TestReasoningOnlyChunkDoesNotEmitContent:
    """A reasoning-only delta.content=None must keep content callback silent."""

    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_reasoning_only_chunk_silences_content_callback(
        self, mock_create, mock_close
    ):
        from run_agent import AIAgent

        chunks = [
            _make_stream_chunk(reasoning_content="just thinking"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]

        content_calls = []
        reasoning_calls = []
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)
        mock_create.return_value = mock_client

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            stream_delta_callback=lambda t: content_calls.append(t),
            reasoning_callback=lambda t: reasoning_calls.append(t),
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False

        agent._interruptible_streaming_api_call({})

        assert reasoning_calls == ["just thinking"]
        assert content_calls == [], (
            f"REVERSE-REASONING BUG: reasoning-only chunk leaked into "
            f"content callback. content_calls={content_calls!r}"
        )


# ── Test 4: content-only chunk must not emit reasoning ────────────────────────


class TestContentOnlyChunkDoesNotEmitReasoning:
    """Symmetric. Content tokens must never reach the reasoning panel."""

    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_content_only_chunk_silences_reasoning_callback(
        self, mock_create, mock_close
    ):
        from run_agent import AIAgent

        chunks = [
            _make_stream_chunk(content="hello"),
            _make_stream_chunk(content=" world"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]

        reasoning_calls = []
        content_calls = []
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)
        mock_create.return_value = mock_client

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            stream_delta_callback=lambda t: content_calls.append(t),
            reasoning_callback=lambda t: reasoning_calls.append(t),
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False

        agent._interruptible_streaming_api_call({})

        assert content_calls == ["hello", " world"]
        assert reasoning_calls == [], (
            f"REVERSE-REASONING BUG: content-only chunk leaked into "
            f"reasoning callback. reasoning_calls={reasoning_calls!r}"
        )


# ── Test 5: per-turn reset — reasoning from turn N does not leak into turn N+1 ──


class TestReasoningPanelBufferIsolationAcrossTurns:
    """Reasoning emitted during turn N must not fire the reasoning callback
    during turn N+1's stream.

    Bug shape (the most insidious flavor of "reverse reasoning"):
    the reasoning panel buffer from a prior turn persists. When the next
    turn's reasoning starts emitting, the buffer's stale text either:
        (a) fires ``reasoning_callback`` retroactively (replays content
            that already shipped in turn N's reasoning panel), or
        (b) prefixes turn N+1's first chunk with turn N's tail text.

    Pin: a fresh reasoning_callback registered between two separate calls
    to ``_interruptible_streaming_api_call`` must only receive the
    chunks emitted during the second call.
    """

    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_second_call_sees_only_its_own_reasoning(
        self, mock_create, mock_close
    ):
        from run_agent import AIAgent

        turn_n_chunks = [
            _make_stream_chunk(reasoning_content="Turn-N-thought-A"),
            _make_stream_chunk(reasoning_content=" Turn-N-thought-B"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]
        turn_n1_chunks = [
            _make_stream_chunk(reasoning_content="Turn-N+1-thought-A"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]

        mock_client = MagicMock()

        # First call streams turn_n, second call streams turn_n1.
        mock_client.chat.completions.create.side_effect = [
            iter(turn_n_chunks),
            iter(turn_n1_chunks),
        ]
        mock_create.return_value = mock_client

        # Capture reasoning fired during turn N.
        turn_n_reasoning: list[str] = []
        turn_n1_reasoning: list[str] = []

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False

        # ── Turn N
        agent.reasoning_callback = lambda t: turn_n_reasoning.append(t)
        agent._interruptible_streaming_api_call({})

        # ── Turn N+1 — FRESH callback. Must NOT see anything from turn N.
        agent.reasoning_callback = lambda t: turn_n1_reasoning.append(t)
        agent._interruptible_streaming_api_call({})

        assert turn_n_reasoning == [
            "Turn-N-thought-A",
            " Turn-N-thought-B",
        ], f"Turn N reasoning call sequence diverged: {turn_n_reasoning!r}"

        assert turn_n1_reasoning == ["Turn-N+1-thought-A"], (
            f"REVERSE-REASONING BUG (cross-turn leak): turn N+1 reasoning "
            f"callback received stale data: {turn_n1_reasoning!r}. If you see "
            f"turn-N text appearing here, the per-turn reasoning buffer is "
            f"not being reset between turns."
        )
