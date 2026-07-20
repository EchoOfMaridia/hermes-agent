"""Regression test: Discord post-stream MEDIA: delivery must use the in-flight streaming adapter.

When a streaming turn is finalized (already_sent=True), the gateway runs
``_deliver_media_from_response`` against an adapter that should be the SAME
``discord.Client`` instance the stream consumer was editing.

A reconnect handler swapping ``self.adapters[platform]`` to a replacement
adapter between stream-start and post-stream delivery must NOT redirect
the post-stream MEDIA: post to the replacement — Discord adapter's
``send_multiple_images`` targets the channel through its own ``_client``,
and the replacement's ``_client`` is a fresh websocket that doesn't
share the streamed message's edit history.

This test exercises the dispatcher's adapter-pinning logic in isolation:
given an ``agent_result`` dict (with or without the pinned in-flight
adapter key), the expected delivery target must match the contract.

The contract lives at ``gateway/run.py:_handle_message_with_agent`` around
the streaming-final block (``already_sent`` branch).
"""

from __future__ import annotations

import asyncio
import tempfile
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.config import Platform


def _make_adapter(name):
    """Minimal adapter that records which one handled delivery."""
    adapter = SimpleNamespace(
        name=name,
        _client=MagicMock(),
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_multiple_images=AsyncMock(
            return_value=SendResult(success=True, message_id=f"msg-from-{name}")
        ),
        send_voice=AsyncMock(return_value=SendResult(success=True)),
        send_video=AsyncMock(return_value=SendResult(success=True)),
        send_document=AsyncMock(return_value=SendResult(success=True)),
    )
    return adapter


def _resolve_post_stream_adapter(runner, agent_result, source):
    """Mirror the dispatcher's adapter-selection logic at the streaming-final
    block. The FIX is: prefer ``agent_result.get('_in_flight_stream_adapter')``
    so a reconnect swap of ``self.adapters[platform]`` does not move the
    post-stream delivery off the in-flight adapter.

    Note: ``self.adapters`` is keyed by ``source.platform.value`` (string),
    not the ``Platform`` enum. ``_adapter_for_source`` handles that mapping
    internally; the resolver helpers below mirror it explicitly so the
    dispatcher contract is locked at the dict level.
    """
    pinned = agent_result.get("_in_flight_stream_adapter")
    if pinned is not None:
        return pinned
    return runner.adapters.get(source.platform.value)


def _build_source_event(platform=Platform.DISCORD, **source_overrides):
    from gateway.session import SessionSource

    source = SessionSource(
        platform=platform,
        chat_id="1528518686204231760",
        chat_type="thread",
        thread_id="1528518686204231760",
        user_name="Echo_of_Maridia",
        **source_overrides,
    )
    event = MessageEvent(
        text="Send the screenshots here",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1528539891175260161",
    )
    return source, event


@pytest.mark.asyncio
async def test_post_stream_media_pinned_to_inflight_adapter_after_swap():
    """After a fatal-error swap has installed a replacement adapter in
    self.adapters[discord], the post-stream MEDIA: delivery target must be
    the pinned in-flight adapter — not the freshly swapped replacement."""
    v1 = _make_adapter("v1-inflight")
    v2 = _make_adapter("v2-replacement")

    source, event = _build_source_event()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"\x89PNG\r\n\x1a\nregression")
    os.close(fd)

    try:
        runner = SimpleNamespace(
            adapters={"discord": v2},  # post-swap dict.
            _thread_metadata_for_source=lambda source, anchor=None: {"thread_id": source.thread_id},
            _reply_anchor_for_event=lambda event: event.message_id,
        )
        agent_result = {
            "already_sent": True,
            "failed": False,
            "final_response": f"Here is the screenshot\nMEDIA:{path}",
            # The new contract: this key carries the in-flight stream adapter
            # from the dispatching layer (gated on _stream_consumer.adapter).
            "_in_flight_stream_adapter": v1,
        }

        from gateway.run import GatewayRunner

        pinned = _resolve_post_stream_adapter(runner, agent_result, source)
        assert pinned is v1, (
            f"pinned adapter must be the in-flight adapter (v1); "
            f"got {pinned.name if pinned else None}"
        )

        await GatewayRunner._deliver_media_from_response(
            runner,
            agent_result["final_response"],
            event,
            pinned,
        )
        assert v1.send_multiple_images.await_count == 1
        assert v2.send_multiple_images.await_count == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_post_stream_media_falls_through_when_no_inflight_pin():
    """When the dispatcher did not thread an in-flight adapter (e.g. CLI
    gateway, or non-streaming flows), the existing fallback to
    self.adapters.get(platform) continues to work — the fix MUST NOT
    regress the no-streaming path."""
    only = _make_adapter("only")

    source, event = _build_source_event()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"\x89PNG\r\n\x1a\nregression")
    os.close(fd)

    try:
        runner = SimpleNamespace(
            adapters={"discord": only},
            _thread_metadata_for_source=lambda source, anchor=None: {"thread_id": source.thread_id},
            _reply_anchor_for_event=lambda event: event.message_id,
        )
        agent_result = {
            "already_sent": True,
            "failed": False,
            "final_response": f"hi\nMEDIA:{path}",
        }
        pinned = _resolve_post_stream_adapter(runner, agent_result, source)
        assert pinned is only

        from gateway.run import GatewayRunner

        await GatewayRunner._deliver_media_from_response(
            runner,
            agent_result["final_response"],
            event,
            pinned,
        )
        assert only.send_multiple_images.await_count == 1
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_dispatcher_picks_inflight_when_swap_replaces_dict():
    """The decision itself — not the full delivery — fails on HEAD because
    the current code reads ``self.adapters.get(source.platform)`` at the
    streaming-final block. Pin the decision in isolation so the fix lands
    even if other code paths shift."""
    v1 = _make_adapter("v1-inflight")
    v2 = _make_adapter("v2-replacement")

    runner = SimpleNamespace(
        adapters={"discord": v2},
    )
    agent_result = {
        "already_sent": True,
        "_in_flight_stream_adapter": v1,
    }
    source = SimpleNamespace(platform=Platform.DISCORD)
    pinned = _resolve_post_stream_adapter(runner, agent_result, source)
    assert pinned is v1


def test_dispatcher_picks_swapped_adapter_without_pin():
    """Pre-fix behaviour: without the pin key, ``self.adapters.get(platform)``
    resolves to whatever the dictionary now holds. The fix MUST keep this
    fallback so non-streaming flows are unaffected."""
    v2 = _make_adapter("v2-replacement")
    runner = SimpleNamespace(adapters={"discord": v2})
    agent_result = {"already_sent": True}
    source = SimpleNamespace(platform=Platform.DISCORD)
    pinned = _resolve_post_stream_adapter(runner, agent_result, source)
    assert pinned is v2
