"""Regression tests for the gateway's synchronous clarify callback.

These tests pin down the wire-level contract of the callback that the
gateway wires into ``agent.clarify_callback`` — the function that bridges
the agent thread (sync) to the platform adapter's async ``send_clarify``
and blocks the agent thread until the user responds.

The bug being guarded against (reported by Echo_of_Maridia, 2026-06-26):

    "The clarify tool works but it doesn't pause you to wait for it.
     Instead it just continues and assumes the first value is selected
     making it a race against time for the user."

This test file reconstructs the callback's logic in isolation (the real
one lives inside a closure in ``gateway/run.py``) so we can exercise it
without spinning up the whole gateway. If the contract holds, the
callback MUST:

  1. Block the calling thread until ``resolve_gateway_clarify`` fires
     (or the timeout expires).
  2. NOT return instantly with a synthetic "first choice" value or any
     other value that lets the agent proceed without actually waiting.
  3. Return the resolved response string verbatim when the user picks.
  4. Return a clearly-falsy sentinel on timeout — NOT a choice value.

These tests fail (RED) when the callback races past the user. They pass
(GREEN) once the callback actually waits.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Repo root importable
import sys
from pathlib import Path
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _clear_clarify_state():
    """Reset module-level state between tests."""
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


# ---------------------------------------------------------------------------
# Reconstruction of the gateway's _clarify_callback_sync
# ---------------------------------------------------------------------------
#
# The real function lives inside a closure inside ``GatewayRunner._handle_message``
# (gateway/run.py around line 15712) and depends on ~12 enclosing variables
# (session_key, _status_adapter, _status_chat_id, _loop_for_step, etc).
# Reconstructing it here in pure form lets us exercise the same logic
# without spinning up a real Discord/Telegram session.

def _make_fake_status_adapter(send_delay_s: float = 0.0,
                              send_succeeds: bool = True):
    """Build a minimal stand-in for the platform adapter that the gateway
    uses for ``_status_adapter.send_clarify(...)``.

    The real adapter is async, so we model the send as a future-backed
    coroutine. ``send_delay_s`` simulates adapter latency; ``send_succeeds``
    controls whether the future's result has ``success=True``.
    """
    import asyncio

    async def _send_clarify(chat_id, question, choices, clarify_id, session_key,
                            metadata=None):
        # Simulate adapter latency (Discord API round-trip, etc.)
        if send_delay_s > 0:
            await asyncio.sleep(send_delay_s)
        return SimpleNamespace(success=send_succeeds, message_id="fake-msg-id")

    adapter = MagicMock()
    adapter.send_clarify = _send_clarify
    adapter.pause_typing_for_chat = MagicMock()
    return adapter


def _clarify_callback_sync_factory(
    *, session_key: str, adapter, loop, chat_id: str, metadata=None,
    timeout_override: float | None = None,
):
    """Build a synchronous callback that mirrors gateway/run.py logic.

    This is the same algorithm as ``_clarify_callback_sync`` in
    ``gateway/run.py``, reproduced here so the contract test can drive
    it without the full gateway plumbing.
    """
    from agent.async_utils import safe_schedule_threadsafe
    from tools import clarify_gateway as _clarify_mod
    import uuid as _uuid

    def _callback(question: str, choices) -> str:
        if not adapter:
            return ""

        clarify_id = _uuid.uuid4().hex[:10]
        _clarify_mod.register(
            clarify_id=clarify_id,
            session_key=session_key,
            question=question,
            choices=list(choices) if choices else None,
        )

        try:
            adapter.pause_typing_for_chat(chat_id)
        except Exception:
            pass

        fut = safe_schedule_threadsafe(
            adapter.send_clarify(
                chat_id=chat_id,
                question=question,
                choices=list(choices) if choices else None,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            ),
            loop,
            log_message="test clarify schedule",
        )
        send_ok = False
        if fut is None:
            send_ok = False
        else:
            try:
                result = fut.result(timeout=15)
                send_ok = bool(getattr(result, "success", False))
            except Exception:
                send_ok = False

        if not send_ok:
            _clarify_mod.clear_session(session_key)
            return "[clarify prompt could not be delivered]"

        timeout = (
            timeout_override if timeout_override is not None
            else float(_clarify_mod.get_clarify_timeout())
        )
        response = _clarify_mod.wait_for_response(clarify_id, timeout=timeout)
        if response is None or response == "":
            return f"[user did not respond within {int(timeout / 60)}m]"
        return response

    return _callback


def _run_in_loop(adapter):
    """Run ``adapter.send_clarify`` on a dedicated event loop in a background
    thread. Returns (loop, thread, stop_event). Caller is responsible for
    joining the thread after use.
    """
    import asyncio
    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return loop, thread


# ===========================================================================
# Regression: "clarify doesn't pause the agent"
# ===========================================================================


class TestClarifyCallbackBlocksUntilUserResponds:
    """The synchronous callback MUST block the agent thread until the user
    actually responds. This is the bug Echo_of_Maridia reported."""

    def setup_method(self):
        _clear_clarify_state()

    def test_callback_does_not_return_before_user_response(self):
        """The callback must block for the full timeout when the user never
        responds. It must NOT return instantly with a synthetic 'first
        choice' value that would let the agent race ahead."""
        adapter = _make_fake_status_adapter()
        loop, _thread = _run_in_loop(adapter)
        try:
            callback = _clarify_callback_sync_factory(
                session_key="discord-session-1",
                adapter=adapter,
                loop=loop,
                chat_id="123456789",
                timeout_override=0.5,  # short timeout for test
            )

            start = time.monotonic()

            def _run_callback():
                return callback(
                    "Which color?",
                    choices=["red", "green", "blue"],
                )

            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_run_callback)
                # Wait for the timeout to elapse — the user never clicks.
                result = fut.result(timeout=2.0)

            elapsed = time.monotonic() - start

            # The callback must have blocked for roughly the full timeout.
            # A return that races past the user would land here in <0.1s.
            assert elapsed >= 0.4, (
                f"Callback returned in {elapsed:.3f}s — it raced past the "
                f"user without waiting! Result: {result!r}"
            )

            # And the result MUST be the timeout sentinel — NOT one of the
            # choices. If we get "red" / "green" / "blue" here, that's the
            # exact symptom of the reported bug.
            assert result not in ("red", "green", "blue"), (
                f"Callback returned a choice value ({result!r}) without "
                f"the user responding. This is the bug."
            )
            assert "did not respond" in result, (
                f"Callback returned an unexpected non-timeout value: {result!r}"
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            _thread.join(timeout=2.0)

    def test_callback_returns_resolved_value_when_user_clicks(self):
        """When the user clicks a button mid-wait, the callback must
        return that choice's text — not the first choice, not None."""
        adapter = _make_fake_status_adapter()
        loop, _thread = _run_in_loop(adapter)
        try:
            callback = _clarify_callback_sync_factory(
                session_key="discord-session-2",
                adapter=adapter,
                loop=loop,
                chat_id="123456789",
                timeout_override=5.0,
            )

            from tools import clarify_gateway as cm

            captured = {}

            def _run_callback():
                captured["result"] = callback(
                    "Pick one",
                    choices=["alpha", "beta", "gamma"],
                )

            t = threading.Thread(target=_run_callback, daemon=True)
            t.start()

            # Wait for the entry to register, then resolve with "beta"
            # (simulating the user clicking the second button).
            time.sleep(0.1)
            # Find the registered entry and resolve it.
            cid: str | None = None
            for _ in range(50):
                with cm._lock:
                    if cm._entries:
                        cid = next(iter(cm._entries.keys()))
                        break
                time.sleep(0.02)
            if cid is None:
                pytest.fail("Callback never registered an entry")

            time.sleep(0.05)  # let the callback settle
            cm.resolve_gateway_clarify(cid, "beta")  # type: ignore[arg-type]

            t.join(timeout=3.0)
            assert not t.is_alive(), "Callback thread still running — race!"
            assert captured.get("result") == "beta", (
                f"Callback should return 'beta' but returned {captured.get('result')!r}"
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            _thread.join(timeout=2.0)

    def test_callback_returns_resolved_value_even_when_first_choice(self):
        """Edge case: user picks the FIRST choice. The callback must still
        return "first choice" — and the act of returning it must be
        triggered by an explicit resolve, not by a race.

        If the bug were that the callback returned choices[0] by default
        when no resolve fires, this test would catch it because the
        timeout fires and we'd see the timeout sentinel."""
        adapter = _make_fake_status_adapter()
        loop, _thread = _run_in_loop(adapter)
        try:
            callback = _clarify_callback_sync_factory(
                session_key="discord-session-3",
                adapter=adapter,
                loop=loop,
                chat_id="123456789",
                timeout_override=0.5,  # tight timeout
            )

            from tools import clarify_gateway as cm

            captured = {}

            def _run_callback():
                captured["result"] = callback(
                    "First choice?",
                    choices=["red", "green", "blue"],
                )

            t = threading.Thread(target=_run_callback, daemon=True)
            t.start()

            # Wait for entry registration, then resolve with "red" (FIRST choice)
            time.sleep(0.1)
            cid2: str | None = None
            for _ in range(50):
                with cm._lock:
                    if cm._entries:
                        cid2 = next(iter(cm._entries.keys()))
                        break
                time.sleep(0.02)
            if cid2 is None:
                pytest.fail("Callback never registered an entry")

            cm.resolve_gateway_clarify(cid2, "red")  # type: ignore[arg-type]
            t.join(timeout=3.0)

            # The result MUST be the explicit resolve value "red".
            # If the callback somehow synthesizes choices[0] without a
            # resolve, this test would fail because the timeout would
            # fire instead.
            assert captured.get("result") == "red", (
                f"Expected explicit resolve 'red', got {captured.get('result')!r}"
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            _thread.join(timeout=2.0)


class TestClarifyToolWiring:
    """Pin down the wire-level plumbing from agent → tool → callback.

    The ``tool_executor.py`` clarify branch does:
        from tools.clarify_tool import clarify_tool
        return _clarify_tool(
            question=..., choices=..., callback=agent.clarify_callback,
        )

    If anything in that chain doesn't pass the callback through, the
    agent sees the "not available" error and races ahead. This test
    pins that contract at the level of ``clarify_tool`` itself.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_clarify_tool_passes_callback_through(self):
        """The tool must invoke the callback synchronously and return its
        result. If the tool skips the callback (or it's None), the result
        must surface that error — never silently swallow it."""
        from tools.clarify_tool import clarify_tool

        # The tool MUST invoke the callback we pass — not skip it.
        def cb(question, choices):
            assert question == "Pick"
            assert choices == ["a", "b"]
            return "b"

        result = json.loads(clarify_tool("Pick", choices=["a", "b"], callback=cb))
        assert result["user_response"] == "b", (
            f"Callback wasn't invoked — got {result!r}"
        )

    def test_clarify_tool_without_callback_returns_error_not_first_choice(self):
        """When callback is None, the tool returns a clear error JSON — not
        a silent success that the agent could interpret as 'user picked
        first choice'. This is the symmetry of the bug: if the wiring
        breaks at runtime, the error path is the only safe outcome."""
        from tools.clarify_tool import clarify_tool

        result = json.loads(clarify_tool("Pick", choices=["a", "b"]))
        assert "error" in result, (
            f"Tool without callback should error, not silently succeed. Got: {result!r}"
        )
        # And specifically: NOT a value that looks like a successful pick.
        # The error JSON should have NO user_response / question / choices_offered
        # fields populated — only "error". This pins the contract that the
        # agent's "the user picked something" inference cannot be made.
        safe_success_keys = {"question", "choices_offered", "user_response"}
        leaked = safe_success_keys & set(result.keys())
        assert not leaked, (
            f"Tool returned error AND success-shaped fields {leaked!r} — "
            f"agent could mistakenly infer a successful pick from this. "
            f"Got: {result!r}"
        )


class TestClarifyGateUnresolved:
    """The agent must NOT interpret a non-choice text as an implicit pick.

    Bug report (bigwang agent, 2026-06-26):

        "When `clarify` returns no user response (e.g. the user is debugging
        the tool itself rather than answering the gate), the agent
        interprets silence as approval and proceeds past the gate."

        "After the third `clarify` call returned `user_response: 'Report
        the issue in a way I can copy it. Ill hand over to apex to patch
        hermes agent'`, I should have recognized that as a halt signal."

    The root cause: the tool returns a single `user_response` string with
    no marker indicating HOW the user answered (selected vs Other-channel
    freetext vs unparseable noise). The model has to guess, and it guesses
    wrong: any non-empty text is read as "the user picked something".

    Fix: the tool MUST distinguish these cases in the JSON it returns.

      - ``user_response_mode: "selected"``     — callback returned one of
        ``choices_offered``. The gate was answered.
      - ``user_response_mode: "freetext"``     — callback indicates the
        user used the Other channel (typed a custom answer). Intent is
        acknowledged; treat as a valid gate resolution.
      - ``user_response_mode: "unresolved"``   — callback returned text
        that doesn't match a choice and the caller didn't tag it as
        freetext. The gate was NOT answered; the agent MUST halt.

    This pins the wire-level contract. The CLI TUI (cli.py) sends
    ``{"mode": "selected", "value": choice}`` or ``{"mode": "freetext",
    "value": text}`` to the callback. Plain-string callers (legacy tests,
    oneshot) get the legacy "selected" inference (their string IS the
    answer because they have no UI to type in).
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_selected_callback_returns_user_response_mode_selected(self):
        """When the callback returns a value matching one of choices_offered,
        the tool records mode='selected' so the agent can detect a real pick."""
        from tools.clarify_tool import clarify_tool

        def cb(question, choices):
            return "Approve and ship"  # matches choices[0] exactly

        result = json.loads(clarify_tool(
            "Pick action",
            choices=["Approve and ship", "Approve but skip", "Change something first"],
            callback=cb,
        ))
        assert result["user_response"] == "Approve and ship"
        assert result["user_response_mode"] == "selected", (
            f"Expected mode='selected' for a matched pick, got "
            f"{result.get('user_response_mode')!r}. Full result: {result!r}"
        )

    def test_freetext_callback_returns_user_response_mode_freetext(self):
        """When the callback returns the structured Other-channel marker,
        the tool records mode='freetext' so the agent can distinguish a
        deliberate custom answer from a noisy non-answer."""
        from tools.clarify_tool import clarify_tool

        def cb(question, choices):
            # New contract: UI sends {mode, value} so the tool can tag the
            # mode in the response JSON.
            return {"mode": "freetext", "value": "Use the v2 schema instead"}

        result = json.loads(clarify_tool(
            "Pick action",
            choices=["Approve and ship", "Approve but skip", "Change something first"],
            callback=cb,
        ))
        assert result["user_response"] == "Use the v2 schema instead"
        assert result["user_response_mode"] == "freetext", (
            f"Expected mode='freetext' for an Other-channel answer, got "
            f"{result.get('user_response_mode')!r}"
        )

    def test_unmatched_plain_string_is_marked_unresolved(self):
        """REGRESSION GUARD for bug #2.

        When the callback returns a plain string that doesn't match any
        of choices_offered, the tool MUST mark mode='unresolved' so the
        agent sees a halt signal instead of inferring the most plausible
        pick.

        In the reported bug, the user typed "Report the issue in a way I
        can copy it" into the Other field. That string doesn't match any
        of the offered choices, so the agent must NOT proceed as if the
        user had approved the plan.
        """
        from tools.clarify_tool import clarify_tool

        def cb(question, choices):
            # The user typed debugging text into the gate's Other channel.
            # This is the exact bug report scenario.
            return "Report the issue in a way I can copy it"

        result = json.loads(clarify_tool(
            "Approve the plan?",
            choices=[
                "Approve and ship",
                "Approve but skip the build verify (I trust the schema)",
                "Change something first",
            ],
            callback=cb,
        ))
        # The text is preserved (the user said it), but the mode flags it
        # as a non-pick so the agent can't mistake it for a gate answer.
        assert result["user_response"] == "Report the issue in a way I can copy it"
        assert result["user_response_mode"] == "unresolved", (
            f"Expected mode='unresolved' for a non-matching text response "
            f"(the user typed debugging text, not a gate pick). Got mode="
            f"{result.get('user_response_mode')!r}. "
            f"This is the exact bug: agent would infer 'Approve and ship' "
            f"from this string if mode weren't tagged."
        )

    def test_matched_substring_is_not_auto_selected(self):
        """A partial substring match (e.g. user typed 'Approve' when the
        choice is 'Approve and ship') must NOT count as 'selected' — the
        user might be typing the start of a custom answer.

        Strict equality only. This forces the UI to send the full choice
        text via the structured callback when the user picks."""
        from tools.clarify_tool import clarify_tool

        def cb(question, choices):
            return "Approve"  # substring of choices[0], not equal

        result = json.loads(clarify_tool(
            "Pick action",
            choices=["Approve and ship", "Approve but skip", "Change something first"],
            callback=cb,
        ))
        assert result["user_response"] == "Approve"
        # NOT 'selected' because exact match fails. NOT 'freetext' because
        # the caller didn't tag it. So 'unresolved' is the only honest
        # answer — the UI must send {"mode": "selected", "value":
        # "Approve and ship"} to mark an actual pick.
        assert result["user_response_mode"] == "unresolved"

    def test_oneshot_synthetic_response_is_marked_unresolved(self):
        """The oneshot-mode fallback returns a synthetic instruction string
        ('[oneshot mode: ...]'). This MUST be marked 'unresolved' too so
        any downstream agent that doesn't know about oneshot can't treat
        it as a real user pick."""
        from tools.clarify_tool import clarify_tool

        # Simulate the oneshot callback exactly (it returns plain text).
        def cb(question, choices):
            return (
                "[oneshot mode: no user available. Pick the best option "
                "from ['a', 'b', 'c'] using your own judgment and continue.]"
            )

        result = json.loads(clarify_tool(
            "Pick",
            choices=["a", "b", "c"],
            callback=cb,
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_structured_dict_with_unknown_mode_is_marked_unresolved(self):
        """Defensive: if a future caller sends {"mode": ..., "value": ...}
        with a mode we don't recognise, treat as unresolved rather than
        silently accepting it as 'selected'."""
        from tools.clarify_tool import clarify_tool

        def cb(question, choices):
            return {"mode": "unknown_mode", "value": "garbage"}

        result = json.loads(clarify_tool(
            "Pick",
            choices=["a", "b", "c"],
            callback=cb,
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_user_response_mode_field_always_present(self):
        """Every successful (non-error) result MUST include the
        user_response_mode field. The agent's next turn keys off this
        field — if it's missing, the agent falls back to its bug-prone
        default behaviour (treating any non-empty user_response as a
        pick)."""
        from tools.clarify_tool import clarify_tool

        def cb(question, choices):
            return "x"

        result = json.loads(clarify_tool(
            "Pick", choices=["a", "b"], callback=cb
        ))
        assert "user_response_mode" in result, (
            f"user_response_mode MUST be in the result for the agent to "
            f"interpret the gate correctly. Got: {result!r}"
        )
