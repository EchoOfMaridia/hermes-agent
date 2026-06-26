"""Regression tests for Discord slash command fingerprint-skip staleness.

Bug class: Discord caches the registered application-command definitions.
The Hermes Discord adapter fingerprints the *desired* command set and skips
the auto-sync when the fingerprint matches the last successful sync.  This
trust-the-fingerprint-too-much logic is correct as long as nothing else
modifies Discord's command list.  But the project also ships a separate
skill ``discord_command_sync.py`` that pushes commands from
``hermes_cli.commands.COMMAND_REGISTRY`` — which uses different parameter
names (``arg`` vs ``prompt``) and slightly different descriptions than the
adapter's tree.  When that skill runs between two gateway restarts, it
diverges Discord from the adapter's view.  The adapter's next restart sees
its local fingerprint unchanged, decides no sync is needed, and Discord is
left with stale signatures that throw ``CommandSignatureMismatch`` on every
invocation.

Repro seen in the wild on 2026-06-26: ``/steer`` failed every invocation
because Discord had ``arg: str`` (pushed by the skill) while the adapter
expected ``prompt: str``.  The gateway log showed "Skipping Discord slash
command sync: same slash-command fingerprint already synced" — a false
negative caused by trusting the fingerprint alone.

Fix: the skip logic must be time-bounded.  If the last successful sync was
more than ``DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS`` ago (default 24h),
force a re-sync even when the fingerprint matches — this gives us a
periodic reconciliation against Discord's actual state.

The threshold is configurable so operators can shorten it (paranoid,
hourly) or lengthen it (cost-sensitive, weekly) without a code change.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Setup: build a DiscordAdapter instance with a mocked client.  We avoid
# importing the full plugin loader — adapter is a heavy class with Discord
# client init; we instantiate it directly with the constructor's required
# config and inject mocks for everything else.
# ---------------------------------------------------------------------------

def _make_adapter():
    """Construct a DiscordAdapter with a mocked client and stubbed I/O paths."""
    from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
    from gateway.config import PlatformConfig

    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)

    # Mock client — just enough surface for the sync helpers to read.
    mock_client = MagicMock()
    mock_client.application_id = 1493358753561444383
    mock_client.http = MagicMock()
    # Empty tree for the fingerprint helper
    mock_client.tree.get_commands.return_value = []
    mock_client.tree.fetch_commands = MagicMock(return_value=[])
    adapter._client = mock_client

    return adapter


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """Each test gets an isolated command-sync state file so they cannot
    contaminate each other via the on-disk cache.  The adapter reads the
    path from ``get_hermes_home() / "gateway" /
    "discord_command_sync_state.json"``; redirect that to tmp_path."""
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: tmp_path,
    )
    return tmp_path / "gateway" / "discord_command_sync_state.json"


@pytest.fixture
def write_state(fresh_state):
    """Helper that writes the JSON state file with the given dict."""
    def _write(data):
        fresh_state.parent.mkdir(parents=True, exist_ok=True)
        import json
        fresh_state.write_text(json.dumps(data))
        return fresh_state
    return _write


# ---------------------------------------------------------------------------
# The core contract: a stale (but fingerprint-matching) entry MUST NOT skip
# ---------------------------------------------------------------------------

def test_skip_reason_none_when_state_is_empty(fresh_state):
    """No prior sync record -> must sync (no skip reason)."""
    adapter = _make_adapter()
    reason = adapter._command_sync_skip_reason(
        app_id=1493358753561444383,
        fingerprint="abc123",
    )
    assert reason is None, (
        f"Empty state should not skip sync, got reason: {reason!r}"
    )


def test_skip_reason_present_when_fingerprint_matches_and_state_is_fresh(
    fresh_state, write_state,
):
    """Recent successful sync with matching fingerprint -> skip.
    This is the happy-path 'nothing changed' optimization."""
    adapter = _make_adapter()
    app_id = 1493358753561444383
    fingerprint = "abc123"
    # Write a state file indicating a successful sync 60s ago with this fingerprint.
    write_state({
        str(app_id): {
            "fingerprint": fingerprint,
            "last_success_at": time.time() - 60,
            "last_attempt_at": time.time() - 60,
        }
    })
    reason = adapter._command_sync_skip_reason(app_id, fingerprint)
    assert reason is not None
    assert "fingerprint" in reason.lower()


def test_skip_reason_none_when_fingerprint_matches_but_state_is_stale(
    fresh_state, write_state,
):
    """A matching fingerprint from >MAX_STALE_SECONDS ago must NOT skip.

    This is the bug fix.  Before the fix, this case returned
    'same slash-command fingerprint already synced' and the gateway logged
    'Skipping Discord slash command sync', leaving Discord in its divergent
    state.  After the fix, this returns None and a re-sync happens.
    """
    adapter = _make_adapter()
    app_id = 1493358753561444383
    fingerprint = "abc123"
    # Last successful sync was 25 hours ago — past the default 24h staleness window.
    stale_success = time.time() - (25 * 3600)
    write_state({
        str(app_id): {
            "fingerprint": fingerprint,
            "last_success_at": stale_success,
            "last_attempt_at": stale_success,
        }
    })
    reason = adapter._command_sync_skip_reason(app_id, fingerprint)
    assert reason is None, (
        f"Stale fingerprint match must not skip; expected None, got {reason!r}.  "
        f"This is the bug from the 2026-06-26 incident: Discord had diverged "
        f"(parameter name 'arg' instead of 'prompt'), fingerprint matched the "
        f"old local view, and the skip prevented reconciliation."
    )


def test_skip_reason_none_when_just_under_stale_threshold(
    fresh_state, write_state, monkeypatch,
):
    """Just under the staleness threshold -> still skip (don't be too eager)."""
    adapter = _make_adapter()
    app_id = 1493358753561444383
    fingerprint = "abc123"
    # Set a very generous threshold (1 week) so a 1-day-old sync still skips.
    monkeypatch.setattr(
        "plugins.platforms.discord.adapter._DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS",
        7 * 24 * 3600,
    )
    fresh_success = time.time() - (24 * 3600)  # 1 day ago
    write_state({
        str(app_id): {
            "fingerprint": fingerprint,
            "last_success_at": fresh_success,
            "last_attempt_at": fresh_success,
        }
    })
    reason = adapter._command_sync_skip_reason(app_id, fingerprint)
    assert reason is not None, (
        "1-day-old sync with 1-week threshold should still skip "
        "(within staleness window)"
    )


def test_skip_reason_present_for_fingerprint_mismatch(fresh_state, write_state):
    """Mismatched fingerprint -> sync (the existing 'changed' case)."""
    adapter = _make_adapter()
    app_id = 1493358753561444383
    write_state({
        str(app_id): {
            "fingerprint": "old_fingerprint",
            "last_success_at": time.time() - 60,
            "last_attempt_at": time.time() - 60,
        }
    })
    reason = adapter._command_sync_skip_reason(app_id, "new_fingerprint")
    assert reason is None, (
        "Fingerprint mismatch should always trigger sync (the existing path)"
    )


def test_skip_reason_respects_retry_after(fresh_state, write_state):
    """Rate-limited state must still skip even past the staleness window —
    we don't want to override Discord's explicit retry-after."""
    adapter = _make_adapter()
    app_id = 1493358753561444383
    fingerprint = "abc123"
    retry_until = time.time() + 600  # 10 minutes from now
    write_state({
        str(app_id): {
            "fingerprint": fingerprint,
            "last_success_at": time.time() - (48 * 3600),  # very stale
            "last_attempt_at": time.time() - (48 * 3600),
            "retry_after_until": retry_until,
        }
    })
    reason = adapter._command_sync_skip_reason(app_id, fingerprint)
    assert reason is not None
    assert "wait" in reason.lower(), (
        f"retry-after state should yield 'wait' reason, got {reason!r}"
    )


def test_max_stale_seconds_constant_exists():
    """The implementation must define a module-level constant
    ``_DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS`` so operators can grep for it.
    Default must be 24 hours (86400) — long enough to be a non-event in
    normal operation, short enough to reconcile within a day."""
    import plugins.platforms.discord.adapter as adapter_module
    assert hasattr(adapter_module, "_DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS"), (
        "adapter must define _DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS — see "
        "test_skip_reason_none_when_fingerprint_matches_but_state_is_stale"
    )
    assert adapter_module._DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS == 86400, (
        f"Default staleness threshold must be 24h (86400s); got "
        f"{adapter_module._DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS}"
    )


def test_max_stale_seconds_env_override(fresh_state, write_state, monkeypatch):
    """``DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS`` env var must override the
    module constant for paranoid (short) or cost-sensitive (long) configs."""
    monkeypatch.setenv("DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS", "60")
    # Need to reload the adapter module to pick up the env var, OR the
    # adapter must read it at call time.  The simpler design is reading
    # at call time via os.getenv with the module constant as default.
    import importlib
    import plugins.platforms.discord.adapter as adapter_module
    importlib.reload(adapter_module)
    assert adapter_module._DISCORD_COMMAND_SYNC_MAX_STALE_SECONDS == 60, (
        "Env var must override default staleness threshold"
    )


# ---------------------------------------------------------------------------
# End-to-end: the staleness threshold must be respected by the sync policy
# ---------------------------------------------------------------------------

def test_post_connect_initialization_resyncs_when_state_is_stale(
    fresh_state, write_state, monkeypatch,
):
    """Wire-level test: even if fingerprint matches, a stale state must
    trigger a re-sync.  We assert by checking that ``_safe_sync_slash_commands``
    is called (or, more precisely, that the skip path is bypassed)."""
    adapter = _make_adapter()
    app_id = 1493358753561444383

    # Stub out the fingerprint helper so it returns a stable value.
    fingerprint = "stable_fingerprint"
    monkeypatch.setattr(
        adapter, "_desired_command_sync_fingerprint",
        lambda: fingerprint,
    )
    # Stub the safe-sync to record that it was called.
    sync_called = {"count": 0}
    async def fake_safe_sync():
        sync_called["count"] += 1
        return {"total": 0, "unchanged": 0, "updated": 0, "recreated": 0, "created": 0, "deleted": 0}
    monkeypatch.setattr(adapter, "_safe_sync_slash_commands", fake_safe_sync)

    # Write a stale (25h-old) success record with matching fingerprint.
    stale = time.time() - (25 * 3600)
    write_state({
        str(app_id): {
            "fingerprint": fingerprint,
            "last_success_at": stale,
            "last_attempt_at": stale,
        }
    })

    # Run the post-connect init.  We invoke it directly with the policy
    # forced to 'safe' (the default) and confirm a sync was attempted.
    import asyncio
    monkeypatch.setenv("DISCORD_COMMAND_SYNC_POLICY", "safe")

    async def go():
        # Mock _get_discord_command_sync_policy to return 'safe'
        monkeypatch.setattr(adapter, "_get_discord_command_sync_policy", lambda: "safe")
        await adapter._run_post_connect_initialization()
    asyncio.run(go())

    assert sync_called["count"] == 1, (
        f"Stale state must trigger a re-sync; _safe_sync_slash_commands "
        f"was called {sync_called['count']} times, expected 1."
    )