"""Regression test for cua-driver portal pre-flight.

On Linux Wayland, ``cua-driver 0.17.0`` synthesizes input through
``xdg-desktop-portal``'s ``org.freedesktop.portal.RemoteDesktop`` interface
(via libei). When ``xdg-desktop-portal`` is not running on the session
bus, the MCP handshake hangs indefinitely waiting for libei — the
``_lifecycle_coro`` reaches ``mcp-initialize``, the ``stdio_client``
context opens the subprocess, but the bridge worker never reports ready,
so the ``_ready_event.wait(timeout=30.0)`` in ``_start_lifecycle_locked``
exhausts and the operator sees the opaque:

    cua-driver session never reached ready (timeout 30s; stuck in phase:
    mcp-initialize). Run `hermes computer-use doctor` and check
    ~/.hermes/logs/agent.log for the phase timings.

With no portal running, this hang is permanent — the doctor confirms
the portal is reachable enough to ping, but the MCP subprocess will
never make progress. The right fix is a fast pre-flight probe BEFORE
launching the lifecycle coro: detect "xdg-desktop-portal service
unavailable on the session bus" and surface an actionable error in
milliseconds instead of making the user wait 30 seconds for a
nondeterministic timeout.

This test asserts that when the session bus does NOT expose a working
``org.freedesktop.portal.Desktop`` service, ``_start_lifecycle_locked``
raises ``RuntimeError`` referencing ``xdg-desktop-portal`` directly
(so the operator knows what to install / start), within a second of
the pre-flight probe — not after the full 30-second ``_ready_event``
timeout.

Hermetic — no real subprocess, no real DBus, no portal required.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, cast
from unittest.mock import MagicMock, patch as _patch

import pytest


class TestPortalPreflight:
    def test_missing_portal_fails_fast_with_actionable_error(self):
        """When xdg-desktop-portal is not on the session bus,
        ``_start_lifecycle_locked`` must raise with an actionable
        message referencing the portal — instead of waiting 30s on
        a wedged ``mcp-initialize`` and surfacing a phase-timeout."""
        from tools.computer_use import cua_backend

        session = cast(Any, cua_backend._CuaDriverSession.__new__(
            cua_backend._CuaDriverSession
        ))
        session._lock = threading.Lock()
        session._ready_event = threading.Event()
        session._setup_error = None
        session._shutdown_event = None
        session._startup_phase = "binary-check"
        session._signal_shutdown_locked = lambda: None

        fake_bridge = MagicMock()
        fake_bridge._loop = MagicMock()
        session._bridge = fake_bridge

        # Simulate "xdg-desktop-portal not on session bus": the bus
        # name lookup raises a DBus error (matching what
        # dbus-send returns when no portal service is registered).
        class _NoPortalOnBus(Exception):
            pass

        def _probe_returns_no_portal():
            # Pretend the DBus call timed out / service unknown.
            raise RuntimeError(
                "xdg-desktop-portal not reachable on the session bus "
                "(dbus-send probe timed out after 0.5s). "
                "cua-driver requires xdg-desktop-portal on Linux Wayland "
                "for input injection (libei RemoteDesktop backend). "
                "Install the portal for your compositor and retry."
            )

        # If the implementation does the right thing, it must NOT
        # block on the 30s ready_event.wait — that's what we assert.
        # _ready_event.wait patched to return False would simulate the
        # bug; we patch it to track whether it was even called.
        ready_wait_called = {"count": 0}

        def _track_ready_wait(timeout=None):
            ready_wait_called["count"] += 1
            return False  # simulate timeout, but pre-flight should never reach this

        t0 = time.monotonic()
        with _patch.object(cua_backend, "_probe_xdg_desktop_portal",
                           _probe_returns_no_portal), \
             _patch.object(asyncio, "run_coroutine_threadsafe",
                           return_value=MagicMock()), \
             _patch.object(cua_backend._CuaDriverSession, "_lifecycle_coro",
                           lambda self: None), \
             _patch.object(threading.Event, "wait", _track_ready_wait):
            with pytest.raises(RuntimeError) as exc_info:
                session._start_lifecycle_locked()
        elapsed = time.monotonic() - t0

        msg = str(exc_info.value)
        assert "xdg-desktop-portal" in msg, (
            "Pre-flight failure must name the missing service so the "
            "operator knows exactly what to install. Got: " + msg
        )
        assert elapsed < 5.0, (
            f"Pre-flight took {elapsed:.1f}s — should fail in milliseconds, "
            f"not block on the 30s ready_event timeout. The hang on "
            f"missing-portal is back."
        )
        assert ready_wait_called["count"] == 0, (
            "_start_lifecycle_locked reached the 30s ready_event wait "
            "instead of failing fast at the portal pre-flight. The "
            "pre-flight check is wired in the wrong place."
        )

    def test_portal_available_does_not_block_pre_flight(self):
        """When xdg-desktop-portal IS reachable on the session bus,
        pre-flight must pass and the call must proceed to the normal
        ready_event wait path. This is the no-regression guard for
        the pre-flight addition."""
        from tools.computer_use import cua_backend

        session = cast(Any, cua_backend._CuaDriverSession.__new__(
            cua_backend._CuaDriverSession
        ))
        session._lock = threading.Lock()
        session._ready_event = threading.Event()
        session._setup_error = None
        session._shutdown_event = None
        session._startup_phase = "binary-check"
        session._signal_shutdown_locked = lambda: None

        fake_bridge = MagicMock()
        fake_bridge._loop = MagicMock()
        session._bridge = fake_bridge

        # Simulate "portal is fine": probe returns a sentinel value,
        # no exception. We expect _start_lifecycle_locked to then
        # proceed to its normal flow, which (with our patches) will
        # hit the lifecycle coro and ready_event wait path.
        ready_wait_called = {"count": 0}

        def _track_ready_wait(*_args, **_kwargs):
            ready_wait_called["count"] += 1
            # Set _setup_error to simulate the lifecycle coro raising
            # so we don't enter the infinite wait_for_lifecycle_event
            # loop.
            session._setup_error = RuntimeError("simulated downstream failure")
            return True

        with _patch.object(cua_backend, "_probe_xdg_desktop_portal",
                           lambda: ("org.freedesktop.portal.Desktop",)), \
             _patch.object(asyncio, "run_coroutine_threadsafe",
                           return_value=MagicMock()), \
             _patch.object(cua_backend._CuaDriverSession, "_lifecycle_coro",
                           lambda self: None), \
             _patch.object(threading.Event, "wait", _track_ready_wait):
            # The pre-flight passes; _start_lifecycle_locked must NOT
            # raise from the pre-flight check itself (it may raise
            # from the downstream lifecycle if _setup_error is set,
            # but that's the existing path, not a pre-flight error).
            try:
                session._start_lifecycle_locked()
            except RuntimeError as e:
                # Downstream lifecycle failure is acceptable here; what
                # we care about is that the message does NOT name
                # xdg-desktop-portal — because the pre-flight passed.
                assert "xdg-desktop-portal" not in str(e), (
                    f"Pre-flight should have passed (portal reported "
                    f"available), but a portal-related error was raised: "
                    f"{e}"
                )
            assert ready_wait_called["count"] == 1, (
                "When portal pre-flight passes, the call must proceed "
                "to the normal ready_event wait path."
            )