"""Regression tests for MCP tool discovery at gateway startup.

Three contracts, all derived from a live incident on 2026-06-26 where
the running gateway went ~10 hours without MCP tools even though
``hermes mcp list`` showed them configured:

A. **Discovery failures must surface.** When ``discover_mcp_tools()``
   raises at startup, the gateway must log at WARNING (not DEBUG) so an
   operator who greps ``errors.log`` can see why MCP tools are absent
   instead of staring at a silent gap for hours.

B. **Discovery must complete before the runner accepts messages.**
   The agent tool list is frozen at build time.  If the runner starts
   accepting platform messages before discovery finishes, the first
   session snapshots an empty tool set.

C. **Cached agents must be refreshed after startup discovery.**
   Any agent that already cached its tool list during the (theoretical)
   pre-discovery window needs ``refresh_agent_mcp_tools`` invoked.
   This mirrors the /reload-mcp slash command path.

The implementation lives in :func:`gateway.run._run_startup_mcp_discovery`,
extracted from ``start_gateway`` so tests can drive the contracts
directly without spinning up the full gateway event loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_module_cache():
    """Each test patches ``tools.mcp_tool``.  ``gateway.run`` imports it
    lazily inside ``_run_startup_mcp_discovery`` so module-cache eviction
    is only needed if a previous test imported gateway.run directly.
    """
    yield
    # No-op: gateway.run is imported fresh inside each test below.


def _install_mcp_tool_mock(monkeypatch, *, discover_side_effect=None,
                           discover_return=None):
    """Install a mock ``tools.mcp_tool`` module so the lazy import inside
    ``_run_startup_mcp_discovery`` picks it up.  Returns the mock module.
    """
    mock = MagicMock()
    if discover_side_effect is not None:
        mock.discover_mcp_tools.side_effect = discover_side_effect
    if discover_return is not None:
        mock.discover_mcp_tools.return_value = discover_return
    refresh_mock = MagicMock()
    mock.refresh_agent_mcp_tools = refresh_mock

    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mock)
    return mock


# ---------------------------------------------------------------------------
# A. Discovery failures log at WARNING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_failure_logs_at_warning(
    monkeypatch, caplog
):
    """When ``discover_mcp_tools`` raises, the helper must emit a
    WARNING-level log entry — not a DEBUG one that vanishes at the
    default INFO level.

    Repro: a missing ``mcp`` SDK, a hung stdio subprocess, or a
    misconfigured command all raise from the discovery call.  Hiding
    the failure at DEBUG left an operator unable to tell why
    configured MCP tools were absent from agent tool sets.
    """
    def _explode():
        raise RuntimeError("simulated discovery failure")

    _install_mcp_tool_mock(monkeypatch, discover_side_effect=_explode)

    # Import fresh so the lazy import picks up the mock.
    if "gateway.run" in sys.modules:
        del sys.modules["gateway.run"]
    from gateway.run import _run_startup_mcp_discovery

    runner = MagicMock()
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()

    with caplog.at_level(logging.WARNING):
        # Must not raise — the helper swallows the exception after logging.
        await _run_startup_mcp_discovery(runner)

    failure_records = [
        r for r in caplog.records
        if "MCP tool discovery failed" in r.getMessage()
    ]
    assert failure_records, (
        "Expected a log entry mentioning 'MCP tool discovery failed' "
        "so operators can see why MCP tools are missing."
    )
    # Critical assertion: it MUST be at WARNING level, not DEBUG.
    assert any(r.levelno >= logging.WARNING for r in failure_records), (
        f"MCP discovery failure logged at {failure_records[0].levelname} "
        f"(expected WARNING or higher). DEBUG is invisible at the default "
        f"log level and was the root cause of the silent gap incident."
    )


# ---------------------------------------------------------------------------
# B. Discovery completes before caller proceeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_completes_before_helper_returns(
    monkeypatch
):
    """``_run_startup_mcp_discovery`` must not return until
    ``discover_mcp_tools`` has completed.  ``run_in_executor`` awaits
    the future, so by the time the helper returns discovery is done.

    We assert via call ordering: discovery finishes, then helper returns.
    """
    discovery_started = threading.Event()
    discovery_finished = threading.Event()

    def _slow_discover():
        discovery_started.set()
        time.sleep(0.05)
        discovery_finished.set()

    _install_mcp_tool_mock(monkeypatch, discover_side_effect=_slow_discover)

    if "gateway.run" in sys.modules:
        del sys.modules["gateway.run"]
    from gateway.run import _run_startup_mcp_discovery

    runner = MagicMock()
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()

    await _run_startup_mcp_discovery(runner)

    assert discovery_started.is_set(), "discover_mcp_tools was not invoked"
    assert discovery_finished.is_set(), (
        "_run_startup_mcp_discovery returned before discovery finished — "
        "the runner would start accepting messages with no MCP tools."
    )


# ---------------------------------------------------------------------------
# C. Cached agents are refreshed after discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_refreshes_cached_agents(monkeypatch):
    """After discovery completes, ``refresh_agent_mcp_tools`` must be
    invoked against every cached agent so existing sessions pick up
    newly-discovered tools on their next turn.

    The cache will normally be empty at cold start; this test simulates
    the bug case where agents already cached before discovery finished
    (e.g. pre-existing session or test setup).
    """
    refresh_calls: list[str] = []

    def _record_refresh(agent, quiet_mode=True):
        refresh_calls.append(getattr(agent, "session_id", "<no-id>"))

    mock = _install_mcp_tool_mock(monkeypatch, discover_return=[])
    mock.refresh_agent_mcp_tools.side_effect = _record_refresh

    if "gateway.run" in sys.modules:
        del sys.modules["gateway.run"]
    from gateway.run import _run_startup_mcp_discovery

    class _Agent:
        def __init__(self, session_id):
            self.session_id = session_id

    runner = MagicMock()
    runner._agent_cache = {
        "session_a": (_Agent("session_a"),),
        "session_b": (_Agent("session_b"),),
    }
    runner._agent_cache_lock = threading.Lock()

    await _run_startup_mcp_discovery(runner)

    mock.discover_mcp_tools.assert_called()
    # Both cached agents must have been refreshed.
    assert sorted(refresh_calls) == ["session_a", "session_b"], (
        f"Expected refresh_agent_mcp_tools to be called for both cached "
        f"agents (session_a, session_b), got: {refresh_calls}.  Without "
        f"this, agents that cached before discovery finished never see "
        f"MCP tools."
    )


@pytest.mark.asyncio
async def test_discovery_is_noop_when_cache_is_empty(monkeypatch):
    """The common cold-start case has no cached agents — the refresh
    loop must be a defensive no-op rather than crashing or doing
    unnecessary work.
    """
    refresh_mock = MagicMock()
    mock = _install_mcp_tool_mock(monkeypatch, discover_return=[])
    mock.refresh_agent_mcp_tools = refresh_mock

    if "gateway.run" in sys.modules:
        del sys.modules["gateway.run"]
    from gateway.run import _run_startup_mcp_discovery

    runner = MagicMock()
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()

    await _run_startup_mcp_discovery(runner)

    mock.discover_mcp_tools.assert_called()
    refresh_mock.assert_not_called()