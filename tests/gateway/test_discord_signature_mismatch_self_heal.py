"""Tests for the Discord slash-command error-driven re-sync.

Background: discord.py's ``tree.on_error`` fires when any slash command
raises an ``AppCommandError``.  We use this hook as a self-healing
trigger — when Discord's cached command signatures diverge from our
local tree (the bug from the 2026-06-26 ``/steer`` failure), the
mismatch error is the FIRST observable symptom, well before the
staleness window expires.

The error handler must:
1. Detect ``CommandSignatureMismatch`` specifically (not all errors).
2. Trigger an immediate forced re-sync, bypassing the staleness skip.
3. Coalesce concurrent triggers so a spam of mismatched commands does
   not produce a sync storm.

Tests:
- ``test_on_error_signature_mismatch_triggers_sync`` — the core contract.
- ``test_on_error_unrelated_error_does_not_trigger_sync`` — don't sync
  on every error; only on signature mismatches.
- ``test_on_error_response_sent_to_user`` — the user gets feedback
  explaining what happened, not silence.
- ``test_concurrent_mismatch_errors_coalesce_to_single_sync`` — burst
  protection.

Implementation goes into DiscordAdapter._register_slash_commands()
in plugins/platforms/discord/adapter.py.  We register a single global
``tree.on_error`` callback that detects the mismatch, schedules a
forced sync, and responds to the user.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Setup: install a sufficient discord mock BEFORE any test imports
# anything that touches discord.  We do this at module import time so
# it's available to every test in this file regardless of order.
# ---------------------------------------------------------------------------

def _install_discord_mock():
    """Install a comprehensive discord mock into sys.modules.

    The conftest's _ensure_discord_mock is fine for general gateway
    tests but doesn't include the error classes we need for
    CommandSignatureMismatch handling.  We patch it.
    """
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return  # Real library installed

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.Message = type("Message", (), {})

    class _FakeEmbed:
        def __init__(self, *, title=None, description=None, color=None, **_):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer = None
        def add_field(self, *, name=None, value=None, inline=False, **_):
            self.fields.append({"name": name, "value": value, "inline": inline})
            return self
        def set_footer(self, *, text=None, icon_url=None, **_):
            self.footer = {"text": text, "icon_url": icon_url}
            return self
    discord_mod.Embed = _FakeEmbed

    class _FakeView:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []
        def add_item(self, item):
            self.children.append(item)
    discord_mod.ui = SimpleNamespace(View=_FakeView, button=lambda *a, **k: (lambda fn: fn), Button=object)

    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)

    # Error classes that we can isinstance-check in the production code
    class CommandSignatureMismatch(Exception):
        def __init__(self, command=None):
            self.command = command
            super().__init__(f"The signature for command {command!r} is different...")

    class AppCommandError(Exception):
        pass

    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
        CommandSignatureMismatch=CommandSignatureMismatch,
        AppCommandError=AppCommandError,
        CommandTree=MagicMock,  # used in test_tree_on_error_is_registered
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules["discord"] = discord_mod
    sys.modules["discord.ext"] = ext_mod
    sys.modules["discord.ext.commands"] = commands_mod
    sys.modules["discord.app_commands"] = discord_mod.app_commands


# NOTE: We do NOT install the discord mock at module-import time.
# Doing so would pollute sys.modules["discord"] before the conftest's
# installer runs in other test files, breaking tests that depend on
# conftest's canonical mock shape (e.g. discord.app_commands.Group,
# discord.app_commands.Command).  The autouse fixture below is the
# only installer and it runs the conftest mock FIRST, then augments
# with the error classes we need.


# ---------------------------------------------------------------------------
# Per-test fixture: ensure the mock stays installed (defensive — some
# test runners reload modules between tests).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ensure_mock_discord(request):
    """Install a sufficient discord mock before each test runs.

    Critical invariant: this fixture MUST NOT replace sys.modules['discord']
    if it's already set, because other test files share that module
    reference via the adapter's at-import-time binding.  If we replace
    the module object, downstream test files that call their own
    ``_ensure_discord_mock`` create a new ``discord.DMChannel`` class
    on the new mock — but the adapter, which already imported discord,
    is still pointing at OUR DMChannel class.  ``isinstance(channel,
    discord.DMChannel)`` returns False because the classes differ.

    Strategy: ONLY augment the existing discord module in-place.  If
    sys.modules['discord'] is missing, install a fresh mock first.
    """
    import sys as _sys

    if "discord" not in _sys.modules:
        # First test in this session — install a fresh mock.
        _install_discord_mock()
    else:
        # Augment the existing mock in-place.  Don't replace the module
        # object — that breaks class identity for code that imported it
        # earlier (notably, the adapter module which captured
        # ``discord.DMChannel`` at import time).
        pass

    discord_mod = _sys.modules["discord"]

    # Ensure the classes we need exist.  If they don't, define them on
    # the existing module object (no module replacement).
    if not hasattr(discord_mod, "DMChannel"):
        discord_mod.DMChannel = type("DMChannel", (), {})

    # Add app_commands submodule if missing (some conftest installers
    # don't add it, but our adapter does ``from discord.app_commands
    # import CommandSignatureMismatch`` inside a closure).
    if not hasattr(discord_mod, "app_commands"):
        discord_mod.app_commands = SimpleNamespace()

    app_commands = discord_mod.app_commands

    if not hasattr(app_commands, "CommandSignatureMismatch"):
        class CommandSignatureMismatch(Exception):
            def __init__(self, command=None):
                self.command = command
                super().__init__(f"The signature for command {command!r} is different...")

        app_commands.CommandSignatureMismatch = CommandSignatureMismatch

    if not hasattr(app_commands, "AppCommandError"):
        class AppCommandError(Exception):
            pass
        app_commands.AppCommandError = AppCommandError

    if not hasattr(app_commands, "describe"):
        app_commands.describe = lambda **kwargs: (lambda fn: fn)
    if not hasattr(app_commands, "choices"):
        app_commands.choices = lambda **kwargs: (lambda fn: fn)
    if not hasattr(app_commands, "Choice"):
        app_commands.Choice = lambda **kwargs: SimpleNamespace(**kwargs)
    if not hasattr(app_commands, "Command"):
        app_commands.Command = MagicMock

    # Register discord.app_commands in sys.modules so the closure import works.
    if "discord.app_commands" not in _sys.modules:
        _sys.modules["discord.app_commands"] = app_commands

    yield
    # Clear module-level coalesce state so the next test starts fresh.
    from plugins.platforms.discord import adapter as adapter_module
    adapter_module._force_resync_in_flight.clear()
    adapter_module._force_resync_last_run.clear()


def _make_adapter_with_tree():
    """Build a DiscordAdapter with the minimum surface for the on_error
    handler to work: a stubbed client whose ``tree`` exposes the
    error-handler decorator and ``on_error`` attribute.  Returns the
    adapter, the stub client, and the tree."""
    from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
    from gateway.config import PlatformConfig

    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)

    # Build a stub tree that mimics discord.py's CommandTree error-decorator
    # contract: ``@tree.error`` registers the coroutine as ``tree.on_error``.
    class _StubTree:
        def __init__(self):
            self.on_error = None

        def error(self, coro):
            # discord.py's CommandTree.error registers and returns the coro
            self.on_error = coro
            return coro

        def command(self, *args, **kwargs):
            # Stub for other adapters tests; never used here.
            def _decorator(fn):
                return fn
            return _decorator

        def get_commands(self):
            return []

        async def fetch_commands(self):
            return []

    client = MagicMock()
    client.application_id = 1493358753561444383
    client.tree = _StubTree()
    adapter._client = client

    # Install the on_error handler the same way _register_slash_commands
    # does in production.  We call the method directly because the full
    # _register_slash_commands path needs full Discord state.
    adapter._install_slash_command_error_handler(client.tree)
    return adapter, client, client.tree


# ---------------------------------------------------------------------------
# Test A: the on_error handler exists on the tree
# ---------------------------------------------------------------------------

def test_tree_on_error_is_registered_on_adapter_init():
    """After _install_slash_command_error_handler runs, the client.tree.on_error
    attribute should be set to a callable coroutine handler.  We assert
    this with a minimal mock because the full init path is too heavy
    for a unit test."""
    adapter, client, tree = _make_adapter_with_tree()
    assert callable(tree.on_error), (
        f"Discord adapter must register a tree.on_error handler so we "
        f"can self-heal CommandSignatureMismatch.  Got {tree.on_error!r}.  "
        f"See commit b70d8eee5 context — /steer failed because Discord "
        f"cache diverged and we had no path to detect it from the runtime."
    )


# ---------------------------------------------------------------------------
# Test B: a CommandSignatureMismatch triggers a forced sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_error_signature_mismatch_triggers_forced_sync(request):
    """When Discord raises ``CommandSignatureMismatch`` for any slash
    command, the adapter's tree.on_error handler must trigger an
    immediate forced re-sync (bypassing the staleness skip window).

    This is the runtime self-healing path.  Before this fix, the only
    way to reconcile was waiting for the 24h staleness window to
    expire, or manually deleting the state file."""
    from discord.app_commands import CommandSignatureMismatch
    from plugins.platforms.discord.adapter import (
        DiscordAdapter, _force_slash_command_resync,
    )

    adapter, client, tree = _make_adapter_with_tree()

    sync_called = {"count": 0, "policy": None}

    async def fake_force_sync():
        sync_called["count"] += 1
        return {"total": 0, "unchanged": 0, "updated": 1, "recreated": 0, "created": 0, "deleted": 0}

    monkey = patch.object(adapter, "_safe_sync_slash_commands", fake_force_sync)
    monkey.start()
    request.addfinalizer(monkey.stop)

    interaction = MagicMock()
    interaction.command = MagicMock()
    interaction.command.name = "steer"
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    error = CommandSignatureMismatch(interaction.command)

    # Trigger the on_error handler (which we expect to be installed by
    # _register_slash_commands).  Manually invoke the installed handler.
    handler = tree.on_error
    assert handler is not None, "tree.on_error not set"
    await handler(interaction, error)

    # Give the coalesced sync task a moment to run
    await asyncio.sleep(0.01)

    assert sync_called["count"] == 1, (
        f"Signature mismatch must trigger exactly one forced re-sync; "
        f"got {sync_called['count']} calls.  Without this self-healing "
        f"path, /steer (and any other divergent command) stays broken "
        f"until the 24h staleness window expires."
    )


# ---------------------------------------------------------------------------
# Test C: unrelated errors do NOT trigger a sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_error_unrelated_error_does_not_trigger_sync(request):
    """A non-signature error (e.g. CheckFailure, CommandOnCooldown)
    must NOT trigger a re-sync.  We only react to CommandSignatureMismatch
    because that's the specific signal of cache drift."""
    from plugins.platforms.discord.adapter import (
        DiscordAdapter, _force_slash_command_resync,
    )

    adapter, client, tree = _make_adapter_with_tree()
    sync_called = {"count": 0}

    async def fake_force_sync():
        sync_called["count"] += 1
        return {}

    monkey = patch.object(adapter, "_safe_sync_slash_commands", fake_force_sync)
    monkey.start()
    request.addfinalizer(monkey.stop)

    interaction = MagicMock()
    interaction.command = MagicMock()
    interaction.command.name = "steer"
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    # A generic AppCommandError that is NOT CommandSignatureMismatch
    from discord.app_commands import AppCommandError
    other_error = AppCommandError("some other failure")

    handler = tree.on_error
    await handler(interaction, other_error)
    await asyncio.sleep(0.01)

    assert sync_called["count"] == 0, (
        f"Non-signature errors must not trigger a sync; got "
        f"{sync_called['count']} calls.  We only react to "
        f"CommandSignatureMismatch as a cache-drift signal."
    )


# ---------------------------------------------------------------------------
# Test D: coalesce concurrent triggers into one sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_signature_mismatches_coalesce_to_single_sync(request):
    """If 5 different slash commands all hit signature mismatch in
    quick succession, we must NOT spawn 5 concurrent syncs.  Coalesce
    to one sync task with the others awaiting the same result."""
    from discord.app_commands import CommandSignatureMismatch
    from plugins.platforms.discord.adapter import (
        DiscordAdapter, _force_slash_command_resync,
    )

    adapter, client, tree = _make_adapter_with_tree()
    sync_called = {"count": 0}

    async def slow_sync():
        sync_called["count"] += 1
        await asyncio.sleep(0.05)
        return {"total": 0, "unchanged": 0, "updated": 0, "recreated": 0, "created": 0, "deleted": 0}

    monkey = patch.object(adapter, "_safe_sync_slash_commands", slow_sync)
    monkey.start()
    request.addfinalizer(monkey.stop)

    interaction = MagicMock()
    interaction.command = MagicMock()
    interaction.command.name = "steer"
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    err = CommandSignatureMismatch(interaction.command)

    handler = tree.on_error

    # Fire 5 concurrent errors
    await asyncio.gather(*(handler(interaction, err) for _ in range(5)))
    await asyncio.sleep(0.1)  # let slow_sync finish

    monkey.stop()
    assert sync_called["count"] == 1, (
        f"5 concurrent mismatches must coalesce to 1 sync; got "
        f"{sync_called['count']}.  Without coalescing, a user clicking "
        f"5 commands in quick succession (or 5 users each hitting 1) "
        f"would spawn 5 Discord API rate-limit-bound sync requests."
    )


# ---------------------------------------------------------------------------
# Test E: the user gets feedback that something happened
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_error_responds_to_user_with_explanation(request):
    """The user should see a follow-up message that the slash command
    signature is being reconciled, not just silence.  Discord already
    shows 'The application did not respond' — we add a followup so
    the user knows what's happening."""
    from discord.app_commands import CommandSignatureMismatch
    from plugins.platforms.discord.adapter import (
        DiscordAdapter, _force_slash_command_resync,
    )

    adapter, client, tree = _make_adapter_with_tree()
    async def fake_sync():
        return {}
    patcher = patch.object(adapter, "_safe_sync_slash_commands", fake_sync)
    patcher.start()
    request.addfinalizer(patcher.stop)

    interaction = MagicMock()
    interaction.command = MagicMock()
    interaction.command.name = "steer"
    # Default: response.is_done() returns False (we want to take the
    # "send_message" branch, not the followup branch)
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    err = CommandSignatureMismatch(interaction.command)

    await tree.on_error(interaction, err)
    await asyncio.sleep(0.01)

    interaction.response.send_message.assert_called()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    content = call_kwargs.get("content") or (
        interaction.response.send_message.call_args.args[0]
        if interaction.response.send_message.call_args.args else ""
    )
    ephemeral = call_kwargs.get("ephemeral", False)
    assert "sync" in content.lower() or "reconcil" in content.lower() or "drift" in content.lower(), (
        f"User-facing followup should mention the sync/reconciliation; "
        f"got: {content!r}"
    )
    assert ephemeral is True, (
        "User-facing followup must be ephemeral (visible only to the "
        "user) so we don't spam the channel"
    )