"""Regression test: bare ``@bot`` mentions must ACK, not silently disappear.

Before this fix, a user typing just ``@MiniMax-Bot`` in a channel where the bot
is configured to ``require_mention=True`` would see their ping swallowed with
no response — making the bot look dead. The bare-mention filter at
``plugins/platforms/discord/adapter.py`` deliberately drops the message to
avoid spawning an empty-text agent turn, but the drop was unconditional.

The contract this test pins:
    1. Bare mention pings produce a short ack reply in the same channel.
    2. Ack failures (channel.send raises) return ``False`` and do NOT crash.
    3. A missing channel returns ``False`` cleanly.

We test the helper ``_handle_bare_mention_ack`` directly rather than driving
the full ``on_message`` closure (which is harder to invoke in isolation and
has a much wider blast radius of dependencies).
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_discord_stub() -> None:
    """Provide a minimal ``discord`` module stub for adapter import."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.AllowedMentions = MagicMock
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    sys.modules["discord"] = discord_mod


def _make_message(*, channel=None):
    """Build a fake discord.Message with the shape ``_handle_bare_mention_ack`` reads."""
    user = SimpleNamespace(id=999, display_name="EchoOfMaridia", name="echo_of_maridia", bot=False)
    msg = MagicMock()
    msg.author = user
    msg.channel = channel
    return msg


@pytest.fixture
def adapter():
    """Build a bare DiscordAdapter with just the attributes the helper touches."""
    _install_discord_stub()
    from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: WPS433
    from gateway.config import Platform  # noqa: WPS433

    a = DiscordAdapter.__new__(DiscordAdapter)
    # The base class exposes ``name`` as a property that reads
    # ``self.platform.value.title()``. Set the underlying attribute so the
    # helper's logger.info can format the log line.
    a.platform = Platform.DISCORD
    return a


@pytest.mark.asyncio
async def test_bare_mention_sends_ack(adapter, caplog):
    """A bare @bot ping must ack in-channel with the liveness phrase."""
    channel = MagicMock()
    channel.id = 1524095466160259246
    channel.send = AsyncMock()
    msg = _make_message(channel=channel)

    with caplog.at_level(logging.INFO, logger="plugins.platforms.discord.adapter"):
        result = await adapter._handle_bare_mention_ack(msg)

    assert result is True, "helper should report ack-sent True"
    channel.send.assert_awaited_once()
    sent = channel.send.await_args.args[0]
    assert sent == "👋 yes?", f"expected '👋 yes?', got {sent!r}"

    # The diagnostic log line must be present so silent regressions trip this test.
    assert any("Bare mention-only ping" in rec.getMessage() for rec in caplog.records), (
        "expected 'Bare mention-only ping' diagnostic log line, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_bare_mention_ack_send_failure_returns_false_and_logs(adapter, caplog):
    """If channel.send raises, helper returns False and logs at DEBUG — no crash."""
    channel = MagicMock()
    channel.id = 1524095466160259246
    channel.send = AsyncMock(side_effect=RuntimeError("network blip"))
    msg = _make_message(channel=channel)

    with caplog.at_level(logging.DEBUG, logger="plugins.platforms.discord.adapter"):
        # Must NOT raise — helper swallows the error and returns False.
        result = await adapter._handle_bare_mention_ack(msg)

    assert result is False, "ack failure must return False"
    assert any("bare-mention ack send failed" in rec.getMessage() for rec in caplog.records), (
        "expected DEBUG log line for ack failure, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_bare_mention_ack_returns_false_when_channel_missing(adapter):
    """A message with no ``.channel`` attribute returns False cleanly."""
    msg = _make_message(channel=None)
    # Must NOT raise on getattr-fallback.
    result = await adapter._handle_bare_mention_ack(msg)
    assert result is False, "missing-channel must return False, not spawn a turn"


@pytest.mark.asyncio
async def test_bare_mention_ack_handles_author_without_display_name(adapter):
    """Author with neither ``display_name`` nor ``name`` must not crash the log line."""
    channel = MagicMock()
    channel.id = 1
    channel.send = AsyncMock()
    weird_author = SimpleNamespace(bot=False)  # no display_name, no name
    msg = MagicMock()
    msg.author = weird_author
    msg.channel = channel

    # Must not raise on getattr cascade.
    result = await adapter._handle_bare_mention_ack(msg)
    assert result is True
    channel.send.assert_awaited_once()