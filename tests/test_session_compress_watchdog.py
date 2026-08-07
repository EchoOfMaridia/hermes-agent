"""Regression tests for the ``session.compress`` RPC watchdog + desktop
timeout contract.

Why these tests exist
---------------------
``/compress`` in the desktop and TUI surfaces calls the gateway's
``session.compress`` method (``tui_gateway/server.py:7650``). Real-world
history loads routinely take 30-120 seconds because the underlying
``agent._compress_context`` runs an auxiliary LLM call. Two failure modes
were eating the user:

1.  The desktop front-end gateway client (``apps/shared/src/json-rpc-gateway.ts:61``)
    uses a 30s default request timeout; the desktop wrapper
    (``apps/desktop/src/hermes.ts:177``) configures the same value at the
    ``HermesGateway`` constructor. ``use-prompt-actions.ts:1518`` does NOT
    pass a per-call ``timeoutMs``, so a normal compress that takes >30s
    surfaces as ``compression failed: request timed out: session.compress``
    even though the gateway is still working. Fix: pass a long
    ``SESSION_COMPRESS_REQUEST_TIMEOUT_MS`` from the action handler.

2.  The gateway's ``session.compress`` handler synchronously calls
    ``_compress_session_history`` which calls ``agent._compress_context``.
    If the aux LLM call wedges (network partition, provider hang),
    the RPC hangs indefinitely: the front-end times out at its 30s
    default but the dispatcher pool worker is still spinning. Fix:
    wrap the call in a Future with a configured watchdog and return
    a structured ``_err(rid, 5041, ...)`` so the user gets an
    actionable message instead of a silent hang.

These tests pin the contract: the desktop constant is exported with the
right magnitude, and the server-side handler returns a bounded error
within the watchdog window instead of hanging the dispatcher pool
worker.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
import time
import types
import concurrent.futures
from pathlib import Path

import pytest

# Import the gateway module up front — but the test_protocol fixture
# reimports ``tui_gateway.server`` to reset shared state, which would
# overwrite our module-level reference. Instead, every test in this
# file calls ``server_module = importlib.import_module('tui_gateway.server')``
# inside the body so it sees whatever state the test runner hands it.
# We use a property-like accessor that re-fetches the module each call.
from tui_gateway import server as server_module_initial


def _server_module():
    """Return a fresh handle to the gateway server module.

    Tests that run after a sibling fixture reimports ``tui_gateway.server``
    (e.g. ``tui_gateway/test_protocol.py::server``) need to see the SAME
    module that fixture just patched; an at-module-import-time binding
    would freeze the pre-test view. This helper forces a new import
    lookup at call time, which respects whatever the import system has
    cached — and because Python caches per name, the lookup is cheap.
    """
    return importlib.import_module("tui_gateway.server")


DESKTOP_HERMES_FILE = (
    Path(__file__).resolve().parent.parent
    / "apps"
    / "desktop"
    / "src"
    / "hermes.ts"
)


@pytest.fixture()
def server():
    """Yield the gateway server module. Individual tests opt into a
    smaller watchdog ceiling via ``monkeypatch.setattr(server,
    "_SESSION_COMPRESS_WATCHDOG_S", 0.5)`` to skip the production 30s
    safety floor (which exists to prevent an operator from turning the
    watchdog into a fast-fail that hides the 30s front-end timeout).

    The fixture also restores ``server._methods["session.compress"]`` to
    the real handler if a sibling fixture (e.g.
    ``tests/tui_gateway/test_protocol.py::test_dispatch_session_compress_does_not_block_fast_handler``)
    overwrote it with a stub during a previous test in the same
    session — without this restore, our watchdog assertions get
    sidetracked into the stub's ``{"done": True}`` response and the
    test suite hard-fails when the test_protocol tests run first."""
    mod = _server_module()
    try:
        # If a previous fixture overwrote session.compress with a stub,
        # put the REAL handler back (captured once at module import
        # time before any sibling fixture had a chance to clobber it)
        # so our watchdog assertions hit the production code path.
        # ``_methods`` is the registry the @method decorator populates;
        # clearing or restoring individual entries doesn't break
        # @method's invariants because @method only WRITES to _methods
        # — it never reads from it.
        if mod._methods.get("session.compress") is not _REAL_SESSION_COMPRESS:
            mod._methods["session.compress"] = _REAL_SESSION_COMPRESS
        yield mod
    finally:
        mod._sessions.clear()


# Snapshot of the real session.compress handler captured at module import
# time, BEFORE any sibling fixture has a chance to overwrite
# ``_methods["session.compress"]`` with a test stub. The fixture above
# reinstalls this on every activation so test ordering doesn't change
# which handler is invoked.
_REAL_SESSION_COMPRESS = server_module_initial._methods.get("session.compress")


def _session(history=None, agent=None):
    """Minimal session dict that satisfies ``session.compress``'s lock /
    agent expectations without dragging in a real AIAgent."""
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(
            session_id="session-key",
            _cached_system_prompt="you are hermes",
            tools=[],
        ),
        "session_key": "session-key",
        "history": list(history) if history else [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
    }


def test_session_compress_watchdog_returns_5041_when_compress_history_wedges(
    server, monkeypatch
):
    """When ``_compress_session_history`` exceeds the configured watchdog
    ceiling, ``session.compress`` MUST return a structured JSON-RPC error
    (``code = 5041``) within the ceiling window. Without the watchdog the
    dispatcher pool worker is tied up forever and the front-end can only
    time out client-side — the user sees an opaque "request timed out"
    instead of an actionable "compress did not finish in N seconds".

    This test pins: the error is structured (not a hang), the error code
    is 5041, and the response lands within the ceiling window. We
    redirect the gateway's call to a private ThreadPoolExecutor with
    one worker so the slow LLM emulation actually runs there (and the
    main pool stays free for teardown), then monkey-patch
    ``_compress_session_history`` to block past the watchdog threshold.
    """
    # Tests opt out of the production 30s safety floor — pytest-timeout's
    # session default would otherwise fire while the handler is mid-sleep,
    # producing a misleading stack trace instead of the clean 5041 envelope
    # we're asserting on. Setting the attribute directly skips the
    # safety check (the safety check only runs at module import).
    monkeypatch.setattr(server, "_SESSION_COMPRESS_WATCHDOG_S", 0.5)
    watchdog_seconds = server._SESSION_COMPRESS_WATCHDOG_S
    assert watchdog_seconds > 0, (
        "module-level _SESSION_COMPRESS_WATCHDOG_S must be a positive number of "
        "seconds; without it the session.compress RPC can hang the dispatcher pool "
        "forever. See tui_gateway/server.py around line 7650."
    )
    hang_duration = watchdog_seconds + 1.0
    cancel_event = threading.Event()

    def slow_compress(*args, **kwargs):
        # Sleep in short slices, checking cancel_event between slices
        # so the test's dedicated executor exits promptly on teardown.
        slept = 0.0
        while slept < hang_duration:
            if cancel_event.wait(min(0.05, hang_duration - slept)):
                return 0, {}
            slept += 0.05
        return 0, {}

    # Use a private single-worker pool for the slow LLM call so the
    # gateway's production pool isn't tied up across this test's
    # teardown. We must keep the Future interface intact: the handler
    # calls ``future.result(timeout=...)`` so we just redirect submit
    # to our executor and re-attach a matching ``.result`` method.
    class _FutureProxy:
        def __init__(self, real):
            self._real = real

        def result(self, timeout=None):
            return self._real.result(timeout=timeout)

        def cancel(self):
            return self._real.cancel()

        def done(self):
            return self._real.done()

    isolated_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="test-compress-watcher",
    )

    def isolated_submit(fn, *args, **kwargs):
        return _FutureProxy(isolated_executor.submit(fn, *args, **kwargs))

    monkeypatch.setattr(server, "_pool", types.SimpleNamespace(submit=isolated_submit))
    monkeypatch.setattr(server, "_compress_session_history", slow_compress)
    monkeypatch.setattr(server, "_status_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server, "_sync_session_key_after_compress", lambda *args, **kwargs: None
    )

    history = [{"role": "user", "content": f"msg-{i}"} for i in range(8)]
    server._sessions["slow-sid"] = _session(history=history)

    try:
        start = time.monotonic()
        resp = server.handle_request(
            {
                "id": "slow-1",
                "method": "session.compress",
                "params": {"session_id": "slow-sid"},
            }
        )
        elapsed = time.monotonic() - start

        assert "error" in resp, (
            f"session.compress hung or returned a result envelope; expected a "
            f"JSON-RPC error. resp={resp!r}"
        )
        assert resp["error"]["code"] == 5041, (
            f"session.compress watchdog must surface as code 5041; got "
            f"{resp['error'].get('code')!r}: {resp['error'].get('message')!r}"
        )
        assert re.search(
            r"compress.*timed out|did not finish",
            resp["error"]["message"],
            re.IGNORECASE,
        ), (
            f"5041 message must mention that compress did not finish; got "
            f"{resp['error']['message']!r}"
        )
        assert elapsed < watchdog_seconds + 5.0, (
            f"session.compress watchdog took {elapsed:.1f}s; expected to return "
            f"within ~{watchdog_seconds}s"
        )
    finally:
        cancel_event.set()
        # Shut the isolated executor down WITHOUT waiting for the
        # still-sleeping future — that's the whole point of the
        # watchdog test. ``wait=False`` lets pytest exit immediately.
        isolated_executor.shutdown(wait=False, cancel_futures=True)
        server._sessions.pop("slow-sid", None)

    # Per-method note: a healthy "happy path" compress must NOT trip the
    # watchdog. The companion counter-test
    # ``test_session_compress_returns_result_on_fast_compression`` pins
    # that — we don't repeat the assertion here because it would force
    # this test to wait for a slow worker to finish or be cancelled.


def test_session_compress_returns_result_on_fast_compression(server, monkeypatch):
    """Counter-test: a fast, healthy compress MUST still return the success
    envelope unchanged. The watchdog must NOT poison the happy path — only
    the wedged one."""
    compressed = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "summary"},
    ]

    def fast_compress(*args, **kwargs):
        return 8, {"session_total_tokens": 100}

    monkeypatch.setattr(server, "_compress_session_history", fast_compress)

    info_payload = {"model": "test-model", "session_id": "session-key"}
    agent = types.SimpleNamespace(
        session_id="session-key",
        _cached_system_prompt="you are hermes",
        tools=[],
    )
    # Build a >=4-message history so the handler emits the early
    # compressing status_update (avoids a separate branch in the handler).
    history = [
        {"role": "user", "content": f"msg-{i}"} for i in range(8)
    ]
    server._sessions["fast-sid"] = _session(history=history, agent=agent)

    monkeypatch.setattr(server, "_status_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server,
        "_sync_session_key_after_compress",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda agent, session: info_payload,
    )
    monkeypatch.setattr(
        "agent.manual_compression_feedback.summarize_manual_compression",
        lambda before, after, before_t, after_t: {
            "headline": "Compressed: 8 → 0 messages",
            "token_line": "",
            "note": None,
            "noop": False,
        },
        raising=False,
    )

    try:
        resp = server.handle_request(
            {
                "id": "fast-1",
                "method": "session.compress",
                "params": {"session_id": "fast-sid"},
            }
        )
    finally:
        server._sessions.pop("fast-sid", None)

    assert "result" in resp, f"fast compress must succeed; got {resp!r}"
    assert resp["result"]["status"] == "compressed"


def test_session_compress_watchdog_error_message_is_friendly_hint_compatible(
    server, monkeypatch
):
    """The 5041 message wording must match the regex used by the desktop
    front-end helper ``isCompressTimeoutError`` (see
    apps/desktop/src/app/session/hooks/use-prompt-actions.ts). If the
    wording drifts, the front-end falls back to the raw ``compression
    failed: ...`` blob and loses the actionable hint. Pin the wording
    here so a server-side edit that breaks the user-facing message can't
    ship silently."""
    monkeypatch.setattr(server, "_SESSION_COMPRESS_WATCHDOG_S", 0.1)
    cancel_event = threading.Event()

    def slow_compress(*args, **kwargs):
        cancel_event.wait(5.0)
        return 0, {}

    isolated_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    class _FutureProxy:
        def __init__(self, real):
            self._real = real

        def result(self, timeout=None):
            return self._real.result(timeout=timeout)

        def cancel(self):
            return self._real.cancel()

        def done(self):
            return self._real.done()

    def isolated_submit(fn, *args, **kwargs):
        return _FutureProxy(isolated_executor.submit(fn, *args, **kwargs))

    history = [{"role": "user", "content": f"msg-{i}"} for i in range(8)]
    server._sessions["slow-sid"] = _session(history=history)
    monkeypatch.setattr(server, "_pool", types.SimpleNamespace(submit=isolated_submit))
    monkeypatch.setattr(server, "_compress_session_history", slow_compress)
    monkeypatch.setattr(server, "_status_update", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_sync_session_key_after_compress", lambda *a, **k: None
    )

    try:
        resp = server.handle_request(
            {
                "id": "msg-1",
                "method": "session.compress",
                "params": {"session_id": "slow-sid"},
            }
        )

        assert "error" in resp, f"watchdog must produce an error envelope; got {resp!r}"
        assert resp["error"]["code"] == 5041
        message = resp["error"]["message"]

        # The desktop helper ``isCompressTimeoutError`` matches this
        # exact phrase. Keep the wording in lockstep.
        assert re.search(
            r"compress did not finish within \d+s", message
        ), (
            f"5041 message must match the front-end regex "
            f"(isCompressTimeoutError / compressTimeoutHint). Got {message!r}; "
            f"the desktop helper would fall back to the raw 'compression failed' "
            f"line and the user loses the actionable retry-vs-/new hint."
        )
    finally:
        cancel_event.set()
        isolated_executor.shutdown(wait=False, cancel_futures=True)
        server._sessions.pop("slow-sid", None)


# -------- Desktop front-end timeout constant --------


def test_session_compress_timeout_floor_is_at_least_watchdog():
    """Pin the shared gateway's per-method timeout map so session.compress
    has a sane default even when a call site forgets to pass a per-call
    ``timeoutMs``. The earlier bug — front-end rejecting /compress with
    ``request timed out: session.compress`` after 30s while the gateway
    was still working — was caused by a flat 30s default applied to
    every RPC. The fix is a per-method FLOOR in
    ``apps/shared/src/json-rpc-gateway.ts::LONG_METHOD_TIMEOUT_MS`` AND
    a bumped generic default in
    ``apps/desktop/src/hermes.ts::DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS``.
    Either alone is insufficient: a per-method floor without a
    bumped default still trips a slow ``session.resume``, and a
    bumped default without a per-method floor leaves a wedged /compress
    hanging past the global default. Both layers are required."""
    import re
    from pathlib import Path

    shared_gateway = Path(__file__).resolve().parent.parent / "apps" / "shared" / "src" / "json-rpc-gateway.ts"
    desktop_hermes = Path(__file__).resolve().parent.parent / "apps" / "desktop" / "src" / "hermes.ts"

    shared_source = shared_gateway.read_text(encoding="utf-8")

    # The shared gateway must declare a per-method ceiling for
    # session.compress. Without it, every front-end that uses the
    # shared gateway as-is trips the 30s default and surfaces the
    # same bug forever. Accept both bare integer literals and small
    # multiplicative expressions like ``8 * 60 * 1000`` (the latter
    # keeps the inline value readable; ``requestTimeoutMs`` ceiling
    # values are large enough that naked numbers lose meaning).
    shared_compress_match = re.search(
        r"""['"]session\.compress['"]\s*:\s*([^,\n}]+?)\s*[,}\n]""",
        shared_source,
    )
    assert shared_compress_match, (
        f"apps/shared/src/json-rpc-gateway.ts must declare a "
        f"per-method timeoutMs override for session.compress; otherwise "
        f"every long-running compress RPC trips the gateway client's default "
        f"timer and the user sees ``request timed out: session.compress``. "
        f"Fix: add 'session.compress': <ms> to LONG_METHOD_TIMEOUT_MS."
    )
    raw_compress_expr = shared_compress_match.group(1).strip()
    assert re.fullmatch(r"[\d_\s*]+", raw_compress_expr), (
        f"shared gateway per-method session.compress value must be a "
        f"literal multiplication of digits (e.g. ``8 * 60 * 1000``); "
        f"got {raw_compress_expr!r}"
    )
    shared_compress_ms = 1
    for token in raw_compress_expr.replace(" ", "").split("*"):
        shared_compress_ms *= int(token.replace("_", ""))

    assert shared_compress_ms >= 5 * 60 * 1000, (
        f"shared gateway per-method session.compress ceiling must be >= "
        f"5 minutes (300_000ms); got {shared_compress_ms}. Below 5 minutes, "
        f"real /compress calls (which routinely take 30-120s on real "
        f"histories) will still trip the timer."
    )

    # The desktop layer must also have a sane generic default. The 30s
    # original default is what surfaced the bug in production; a per-method
    # floor alone is not enough — a slow RPC the floor doesn't cover
    # (e.g. session.resume) would still trip.
    desktop_source = desktop_hermes.read_text(encoding="utf-8")

    desktop_default_match = re.search(
        r"DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS\s*=\s*([^;,\n]+?)(?:[;,\n]|$)",
        desktop_source,
    )
    assert desktop_default_match, (
        f"apps/desktop/src/hermes.ts must export "
        f"DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS — without it, the desktop's "
        f"HermesGateway constructor uses the shared 120s default, which "
        f"still trips before compress can answer."
    )

    raw = desktop_default_match.group(1).strip()
    assert re.fullmatch(r"[\d_\s*]+", raw), (
        f"DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS must be a literal "
        f"multiplication of digits; got {raw!r}"
    )
    desktop_default_ms = 1
    for token in raw.replace(" ", "").split("*"):
        desktop_default_ms *= int(token.replace("_", ""))

    assert desktop_default_ms >= 3 * 60 * 1000, (
        f"DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS must be >= 3 minutes "
        f"(180_000ms); got {desktop_default_ms}. Below 3 minutes, slow "
        f"RPCs that aren't in the per-method map (e.g. session.resume on "
        f"a long history) still trip the timer."
    )


def test_desktop_session_compress_timeout_constant_exported_with_safe_magnitude():
    """Pin that apps/desktop/src/hermes.ts exports
    ``SESSION_COMPRESS_REQUEST_TIMEOUT_MS`` with a value >= 60s.

    Why this matters: the front-end gateway client defaults to a 30s
    request timeout (``apps/shared/src/json-rpc-gateway.ts:61``). The
    desktop wrapper constructor sets ``requestTimeoutMs`` to 30s
    (``hermes.ts:177``). ``/compress`` calls the gateway without a
    per-call override, so a real compress that takes >30s is
    interpreted client-side as a timeout and the user sees
    ``compression failed: request timed out: session.compress``.

    Pinning the constant magnitude forces anyone editing the file to
    keep the value large enough to cover real-world history
    compressions (which routinely take 30-120s).
    """
    assert DESKTOP_HERMES_FILE.exists(), (
        f"desktop surface file not found at {DESKTOP_HERMES_FILE}; the test "
        f"is no longer tracking the right file"
    )

    source = DESKTOP_HERMES_FILE.read_text(encoding="utf-8")

    # The export must exist (constant name, not value, on the line).
    assert re.search(
        r"export\s+const\s+SESSION_COMPRESS_REQUEST_TIMEOUT_MS\s*[=:]\s*\d[\d_]*",
        source,
    ), (
        "apps/desktop/src/hermes.ts must export "
        "SESSION_COMPRESS_REQUEST_TIMEOUT_MS. Without it, use-prompt-actions.ts "
        "drops back to the 30s HermesGateway default and real /compress "
        "operations surface as 'request timed out: session.compress'."
    )

    # Extract the literal value so the magnitude check uses the real
    # number, not a stale comment. The value can be either a plain int
    # literal OR a small constant-times-literal expression like
    # ``8 * 60_000`` — the latter keeps the comment readable while a
    # naked big number does not. We parse the second form by walking
    # the expression left-to-right and applying Python operator
    # precedence (multiplication and division only — no addition or
    # exponentiation; the constant lives on the right-hand side of `=`
    # so the grammar is intentionally narrow).
    assign_match = re.search(
        r"export\s+const\s+SESSION_COMPRESS_REQUEST_TIMEOUT_MS\s*=\s*([^;,]+?)(?:\n|$)",
        source,
    )
    assert assign_match, (
        "could not locate SESSION_COMPRESS_REQUEST_TIMEOUT_MS assignment"
    )
    raw_expr = assign_match.group(1).strip()
    # Reject anything that isn't a tame multiplication of digits and
    # underscores — the constant should never reach into a runtime
    # expression (env reads, function calls, ternaries, etc).
    assert re.fullmatch(r"[\d_\s*]+", raw_expr), (
        f"SESSION_COMPRESS_REQUEST_TIMEOUT_MS must be a small constant "
        f"expression of literal digits; got {raw_expr!r}"
    )

    def _eval(expr: str) -> int:
        # Tiny, local eval: only digits, underscores, ``*``, and
        # whitespace. Avoids importing the ast module just for one
        # addition. We tokenise on ``*`` and multiply a stream of ints.
        total = 1
        for token in expr.replace(" ", "").split("*"):
            assert token, f"empty factor in expression {expr!r}"
            total *= int(token.replace("_", ""))
        return total

    value = _eval(raw_expr)

    # Lower bound: must cover any realistic compression. We use 60s as
    # the floor because the user-reported failure window started at
    # 30s (the gateway default). An upper bound prevents an
    # accidental "set to MAX_SAFE_INTEGER" regression: a runaway value
    # causes a hung RPC to keep the dispatcher pool worker spinning
    # invisibly, the same bug class this fixture exists to prevent.
    assert value >= 60_000, (
        f"SESSION_COMPRESS_REQUEST_TIMEOUT_MS must be >= 60_000 (60s); got "
        f"{value}. Below 60s the compress RPC will trip the front-end's "
        f"default timeout on real-world history loads."
    )
    assert value <= 30 * 60_000, (
        f"SESSION_COMPRESS_REQUEST_TIMEOUT_MS must be <= 30 * 60_000 (30m); "
        f"got {value}. Larger values mask a hung server behind a never-firing "
        f"timeout. The server-side watchdog in tui_gateway/server.py must do "
        f"the bound enforcement, NOT a wider client-side timeout."
    )
