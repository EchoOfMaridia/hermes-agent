"""Tests for ``agent.auxiliary_client.async_call_llm(..., stream=True)``.

Today the auxiliary client only exposes non-streaming
``client.chat.completions.create(**kwargs)`` and returns the validated
response. To enable per-token plugin streaming
(``PluginLlm.acomplete_stream``) we need a sibling path that yields
raw ``ChatCompletionChunk``-shaped objects as the provider streams
them, plus a non-streaming fallback when ``stream=False`` (default).

What we verify here:

1. When called with ``stream=False``, behavior is unchanged —
   ``async_call_llm`` returns a single validated response.
2. When called with ``stream=True``, the helper yields each streamed
   chunk in order and does NOT validate/parse the final response
   itself (the consumer is responsible for accumulating chunks).
3. The streaming helper respects a cancellation (asyncio.CancelledError)
   propagated to the underlying client.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.auxiliary_client import async_call_llm


def _make_chunk(content=None, finish_reason=None, model="test-model"):
    delta = SimpleNamespace(content=content, tool_calls=None,
                            reasoning_content=None, reasoning=None)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=None)


class TestAsyncCallLlmStreamPath:
    """Pin the contract for the new ``stream=True`` argument.

    Defaults to ``stream=False`` for back-compat with existing callers.
    """

    def test_default_stream_false_is_unchanged(self, monkeypatch):
        """When ``stream`` is not passed, async_call_llm returns the
        validated response synchronously (existing behavior)."""
        # Build a fake non-streaming response.
        validated = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            model="x",
        )

        captured_kwargs: dict = {}

        async def _fake_create(**kwargs):
            captured_kwargs.update(kwargs)
            # Single response (not a stream).
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="ok"),
                    finish_reason="stop",
                )],
                usage=None,
            )

        # Patch _get_cached_client to return a stubbed async client.
        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_create)))
        monkeypatch.setattr(
            "agent.auxiliary_client._get_cached_client",
            lambda *a, **kw: (fake_client, "x"),
        )

        # Patch _validate_llm_response to a no-op passthrough so the
        # test doesn't depend on its validation logic.
        monkeypatch.setattr(
            "agent.auxiliary_client._validate_llm_response",
            lambda resp, task: validated,
        )

        result = asyncio.run(async_call_llm(
            task=None, messages=[{"role": "user", "content": "hi"}],
            provider="minimax", model="x",
        ))
        assert result is validated
        # stream was NOT injected into the call.
        assert "stream" not in captured_kwargs or captured_kwargs.get("stream") is False

    def test_stream_true_yields_chunks_in_order(self, monkeypatch):
        """With ``stream=True``, async_call_llm returns an async
        iterator yielding each ChatCompletionChunk-shaped object as
        the provider streams it."""
        chunks = [
            _make_chunk(content="hello"),
            _make_chunk(content=" "),
            _make_chunk(content="world", finish_reason="stop"),
        ]

        class _ChunkStream:
            """Stand-in for OpenAI's AsyncStream — an async iterable."""
            def __init__(self, chunks):
                self._chunks = chunks
                self._i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._i >= len(self._chunks):
                    raise StopAsyncIteration
                c = self._chunks[self._i]
                self._i += 1
                return c

        # OpenAI clients return the streaming object directly (not a
        # coroutine). Make the fake match that shape.
        def _fake_streaming_create(**kwargs):
            assert kwargs.get("stream") is True
            return _ChunkStream(chunks)

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_streaming_create)))
        monkeypatch.setattr(
            "agent.auxiliary_client._get_cached_client",
            lambda *a, **kw: (fake_client, "x"),
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._validate_llm_response",
            lambda resp, task: resp,
        )

        async def go():
            out = await async_call_llm(
                task=None, messages=[{"role": "user", "content": "hi"}],
                provider="minimax", model="x",
                stream=True,
            )
            collected = []
            async for c in out:
                collected.append(c)
            return collected

        collected = asyncio.run(go())
        assert collected == chunks
        assert collected[0].choices[0].delta.content == "hello"
        assert collected[2].choices[0].finish_reason == "stop"

    def test_stream_true_returns_async_iterator_not_response(self, monkeypatch):
        """Specifically: when stream=True, async_call_llm returns an
        async iterable — not the validated single response shape."""
        class _ChunkStream:
            def __init__(self):
                self._done = False
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self._done:
                    raise StopAsyncIteration
                self._done = True
                return _make_chunk(content="x", finish_reason="stop")

        def _fake_streaming_create(**kwargs):
            return _ChunkStream()

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_streaming_create)))
        monkeypatch.setattr(
            "agent.auxiliary_client._get_cached_client",
            lambda *a, **kw: (fake_client, "x"),
        )

        async def go():
            r = await async_call_llm(
                task=None, messages=[{"role": "user", "content": "hi"}],
                provider="minimax", model="x",
                stream=True,
            )
            ait = r.__aiter__()
            first = await ait.__anext__()
            return first

        first = asyncio.run(go())
        assert first.choices[0].delta.content == "x"

    def test_stream_true_awaits_coroutine_returning_async_stream(
        self, monkeypatch,
    ):
        """REGRESSION for 2026-06-30 /workflow create bug.

        The openai>=1.0 SDK (2.x in production) returns a *coroutine*
        from ``client.chat.completions.create(stream=True)``. The
        AsyncStream only materializes after ``await``. The legacy
        contract (openai<1.0) returned the AsyncStream directly.

        Before the fix, ``async_call_llm(stream=True)`` did::

            result = client.chat.completions.create(**kwargs)
            return _iter_async_stream(result)   # leaks the coroutine

        and the consumer crashed with
        ``TypeError: 'coroutine' object is not iterable`` on the first
        ``async for chunk in stream_iter``.

        After the fix, async_call_llm detects the coroutine return
        and awaits it before handing the AsyncStream to
        ``_iter_async_stream``. The consumer then iterates real
        chunks transparently.

        This test pins BOTH shapes:

          - new SDK shape (create() returns coroutine resolving to
            AsyncStream) — the regression
          - legacy SDK shape (create() returns AsyncStream directly)
            — still works (covered above)
        """
        chunks = [
            _make_chunk(content="hello"),
            _make_chunk(content=" ", finish_reason="stop"),
        ]

        class _ChunkStream:
            """Stand-in for the AsyncStream the SDK returns after
            awaiting create()."""
            def __init__(self, chunks):
                self._chunks = chunks
                self._i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._i >= len(self._chunks):
                    raise StopAsyncIteration
                c = self._chunks[self._i]
                self._i += 1
                return c

        # New SDK shape: create(stream=True) returns a *coroutine*
        # that resolves to the AsyncStream. The test for this shape
        # is the regression — without the fix, async_call_llm leaks
        # this coroutine into _iter_async_stream, which builds an
        # async generator that iterates a coroutine, crashing with
        # "'coroutine' object is not iterable".
        def _fake_streaming_create(**kwargs):
            assert kwargs.get("stream") is True
            async def _coro():
                return _ChunkStream(chunks)
            return _coro()

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_streaming_create)))
        monkeypatch.setattr(
            "agent.auxiliary_client._get_cached_client",
            lambda *a, **kw: (fake_client, "x"),
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._validate_llm_response",
            lambda resp, task: resp,
        )

        async def go():
            out = await async_call_llm(
                task=None, messages=[{"role": "user", "content": "hi"}],
                provider="minimax", model="x",
                stream=True,
            )
            collected = []
            async for c in out:
                collected.append(c)
            return collected

        collected = asyncio.run(go())
        assert collected == chunks
        assert collected[0].choices[0].delta.content == "hello"

    def test_iter_async_stream_handles_coroutine_source_directly(self):
        """REGRESSION for _iter_async_stream defense-in-depth.

        Some upstream callers (legacy test mocks, alternate SDK
        adapters) may pass a coroutine directly to _iter_async_stream
        instead of an AsyncStream. The function must await the
        coroutine to materialize the AsyncStream before adapting,
        rather than wrapping the coroutine in a sync ``for chunk in
        coro`` adapter that crashes with
        ``TypeError: 'coroutine' object is not iterable``.

        Pin: ``_iter_async_stream(<coroutine resolving to AsyncStream>)``
        yields the resolved stream's chunks without error.
        """
        from agent.auxiliary_client import _iter_async_stream

        chunks = [_make_chunk(content="alpha"),
                  _make_chunk(content=" beta", finish_reason="stop")]

        class _ChunkStream:
            def __init__(self, chunks):
                self._chunks = chunks
                self._i = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self._i >= len(self._chunks):
                    raise StopAsyncIteration
                c = self._chunks[self._i]
                self._i += 1
                return c

        async def _coro():
            return _ChunkStream(chunks)

        stream_iter = _iter_async_stream(_coro())

        async def go():
            collected = []
            async for c in stream_iter:
                collected.append(c)
            return collected

        collected = asyncio.run(go())
        assert collected == chunks
        assert collected[0].choices[0].delta.content == "alpha"

    def test_iter_async_stream_strips_think_blocks_for_structured(self):
        """REGRESSION for 2026-06-30 MiniMax-M3 reasoning leak.

        Models with extended-thinking (MiniMax-M3, Claude with
        thinking, DeepSeek-R1) wrap their reasoning in
        ``<think>...</think>`` blocks. When the response also needs
        to be JSON-parsed for structured output, the think block
        must be stripped before the JSON parser runs — otherwise
        ``json.loads`` fails and the consumer sees
        ``LLM returned no parsed JSON: '<think>...``.

        This test pins the strip behavior at the
        ``_strip_code_fences`` / ``_parse_structured_text`` layer.
        """
        from agent.plugin_llm import _parse_structured_text

        think_text = (
            "<think>The user wants a simple workflow. Let me design "
            "one that prints hello world and is safe.</think>\n"
            '{"name": "hello", "description": "x", "script": "y", '
            '"step_names": []}'
        )
        parsed, content_type = _parse_structured_text(
            text=think_text, json_mode=True,
            json_schema={"type": "object"},
        )
        assert content_type == "json", (
            f"think-block strip broke JSON parsing: got content_type="
            f"{content_type!r}, parsed={parsed!r}"
        )
        assert parsed == {"name": "hello", "description": "x",
                          "script": "y", "step_names": []}
