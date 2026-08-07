"""Tests for clarify_gateway's public surface that platform adapters rely on.

These tests pin the contracts that platform adapters (Discord, Telegram,
WhatsApp) depend on so they DON'T have to reach into private module-level
state. The Discord adapter historically reached into `_entries` directly to
look up the canonical choice text for a clicked button — that private-state
leak is what caused the Discord "buttons grayed out, agent stuck" bug:

  1. User clicks button → adapter disables all child buttons (grays them out).
  2. Adapter reaches into `_entries` to look up the canonical text.
  3. If the entry was already cleaned up (timeout race, session-boundary
     cleanup, etc.), the lookup returns None and the adapter falls back to
     the truncated button label as the answer.
  4. If the resolve_gateway_clarify call fails after the buttons are
     disabled, the agent never gets the answer. The user can't re-click
     because all buttons are gray. The agent thread blocks forever on
     `wait_for_response`. The session must be destroyed to recover.

The fix is to expose a public `resolve_choice_by_index(clarify_id, index)`
helper that the adapter calls, instead of reaching into private state. This
gives us:

  - One place to handle "entry already gone" (resolve with the supplied label
    as a final fallback, mark it as `unresolved` mode if you want, or just
    resolve with the supplied text — the choice is yours in the helper).
  - One place to handle "resolve already fired" (idempotent no-op, return
    a flag the adapter can log).
  - One place where the bug class gets fixed and stays fixed.

These tests pin that helper's contract. The Discord adapter patch will
swap its private-state reach for a call to the helper.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


class TestResolveChoiceByIndex:
    """The public helper platform adapters (Discord / Telegram) need.

    Without this, adapters reach into `_entries` directly. That works until
    the entry gets cleared by a session-boundary cleanup or a late timeout,
    at which point the adapter falls back to the truncated button label —
    and the agent receives a value that doesn't match `choices_offered`,
    so clarify_tool tags it `unresolved` and the gate halts. Meanwhile the
    user sees grayed-out buttons and no recourse.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_resolve_choice_by_index_returns_choice_text(self):
        """Happy path: entry exists, resolve returns the canonical text."""
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Approve?", ["Approve and ship", "Deny", "Change"])
        ok = cm.resolve_choice_by_index("id1", 1)  # pick "Deny"
        assert ok is True

        # The waiting thread receives the canonical text, not the index.
        result = cm.wait_for_response("id1", timeout=0.1)
        assert result == "Deny"

    def test_resolve_choice_by_index_zero(self):
        """Index 0 → first choice."""
        from tools import clarify_gateway as cm

        cm.register("id2", "sk2", "Pick", ["Alpha", "Beta", "Gamma"])
        assert cm.resolve_choice_by_index("id2", 0) is True
        assert cm.wait_for_response("id2", timeout=0.1) == "Alpha"

    def test_resolve_choice_by_index_last(self):
        """Last index → last choice."""
        from tools import clarify_gateway as cm

        cm.register("id3", "sk3", "Pick", ["A", "B", "C"])
        assert cm.resolve_choice_by_index("id3", 2) is True
        assert cm.wait_for_response("id3", timeout=0.1) == "C"

    def test_resolve_choice_by_index_out_of_range_returns_false(self):
        """Out-of-range index → no-op, NOT a resolve."""
        from tools import clarify_gateway as cm

        cm.register("id4", "sk4", "Pick", ["A", "B"])
        # Index 5 doesn't exist
        assert cm.resolve_choice_by_index("id4", 5) is False
        # Entry is still pending — wait_for_response times out cleanly.
        assert cm.wait_for_response("id4", timeout=0.1) is None

    def test_resolve_choice_by_index_unknown_id_returns_false(self):
        """Unknown clarify_id → False, no resolve."""
        from tools import clarify_gateway as cm

        assert cm.resolve_choice_by_index("nope", 0) is False

    def test_resolve_choice_by_index_on_entry_with_no_choices(self):
        """Open-ended clarifies have no choices array; resolve-by-index is a no-op."""
        from tools import clarify_gateway as cm

        cm.register("id5", "sk5", "Free form?", None)
        assert cm.resolve_choice_by_index("id5", 0) is False

    def test_resolve_choice_by_index_idempotent_after_resolve(self):
        """A late second click (Discord single-use race) → no-op."""
        from tools import clarify_gateway as cm

        cm.register("id6", "sk6", "Pick", ["A", "B"])
        assert cm.resolve_choice_by_index("id6", 0) is True
        # Late second click — Discord sometimes hits this when the user
        # double-clicks before the view's `resolved` flag flips.
        assert cm.resolve_choice_by_index("id6", 1) is False

    def test_resolve_choice_by_index_after_timeout_returns_false(self):
        """Entry cleared by timeout → no-op.

        This is the exact race that bricks Discord sessions: the gateway
        timeout fires, clear_session pops the entry, then the user's late
        button click arrives. Today the adapter reaches into _entries,
        gets None, falls back to the truncated label, and the agent
        receives a value that doesn't match any choice. With this helper,
        the late click is a clean no-op so the user gets a 'prompt
        expired' fallback instead of a stuck agent.
        """
        from tools import clarify_gateway as cm

        cm.register("id7", "sk7", "Pick", ["A", "B"])
        # Force a timeout — wait_for_response removes the entry
        assert cm.wait_for_response("id7", timeout=0.05) is None
        # Late click
        assert cm.resolve_choice_by_index("id7", 0) is False


class TestForceCancelAllClarifies:
    """A safe 'panic button' for stuck sessions.

    User quote (the bug): 'Stopping agent is no good either, resulting in
    the entire session needing to be destroyed which is a massive waste.'

    The user wants ONE atomic action that unblocks the agent thread
    without destroying the session. Today the only escape is to delete the
    session. This helper provides the escape valve: every pending entry
    gets resolved with the explicit sentinel so the agent thread
    unblocks and the agent's clarify_tool receives an `unresolved` mode
    response (which it MUST halt on, per the skill).

    Wired into /stop, /new, and any session-boundary cleanup.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_force_cancel_returns_number_cancelled(self):
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Pick", ["A", "B"])
        cm.register("id2", "sk1", "Pick", ["A", "B"])
        cancelled = cm.force_cancel_session("sk1")
        assert cancelled == 2

    def test_force_cancel_unblocks_waiting_threads(self):
        """Threads blocked on wait_for_response must unblock cleanly."""
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Pick", ["A", "B"])

        def waiter():
            return cm.wait_for_response("id1", timeout=10.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)
            cancelled = cm.force_cancel_session("sk1")
            assert cancelled == 1
            result = fut.result(timeout=2.0)
            # Empty string — agent code distinguishes this via the
            # user_response_mode = "unresolved" tag that clarify_tool now emits.
            assert result == ""

    def test_force_cancel_on_unknown_session_returns_zero(self):
        from tools import clarify_gateway as cm

        assert cm.force_cancel_session("nope") == 0

    def test_force_cancel_does_not_raise_when_entry_already_resolved(self):
        """A late force-cancel on a session that already resolved must not blow up."""
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Pick", ["A", "B"])
        cm.resolve_gateway_clarify("id1", "A")  # resolved normally
        # Now force-cancel — should be idempotent
        assert cm.force_cancel_session("sk1") == 0

    def test_force_cancel_is_distinct_from_clear_session(self):
        """clear_session is the existing function. force_cancel_session is
        an alias-shaped surface that signals intent. Both must unblock the
        same set of threads; force_cancel_session exists so call sites in
        /stop and /new can read like a panic-button."""
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Pick", ["A", "B"])
        cm.register("id2", "sk2", "Pick", ["A", "B"])
        # force_cancel targets ONE session
        assert cm.force_cancel_session("sk1") == 1
        # sk2 is untouched
        assert cm.has_pending("sk2") is True


class TestClarifyGatewayDiscordResolveContract:
    """End-to-end: a Discord-style click → adapter calls helper → agent thread unblocks.

    Simulates the exact flow that was bricking sessions:

      1. Agent thread calls clarify_tool with multi-choice
      2. Adapter renders N buttons (simulated here — we test the resolve path)
      3. User clicks button → adapter calls resolve_choice_by_index(clarify_id, idx)
      4. Agent thread's wait_for_response returns the canonical text
      5. clarify_tool tags user_response_mode = "selected" because the
         resolved text matches one of choices_offered
      6. Agent proceeds cleanly

    Without this contract, the adapter would race the cleanup paths and
    brick the session.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_end_to_end_button_click_resolves_with_canonical_text(self):
        from tools import clarify_gateway as cm

        # Step 1-2: agent calls clarify with 3 choices
        cm.register(
            "discord-click-1",
            "sk-discord",
            "Approve the plan?",
            ["Approve and ship", "Deny", "Request changes"],
        )

        # Step 3: agent thread blocks
        def agent_thread():
            return cm.wait_for_response("discord-click-1", timeout=2.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(agent_thread)

            # Simulate a small Discord interaction latency
            time.sleep(0.05)

            # Step 4: user clicks "Deny" (index 1) → adapter calls helper
            ok = cm.resolve_choice_by_index("discord-click-1", 1)
            assert ok is True

            # Step 5: agent thread receives the canonical text
            result = fut.result(timeout=2.0)
            assert result == "Deny"

            # Step 6: clarify_tool would tag this as "selected" because
            # "Deny" strictly matches choices_offered[1]
            from tools.clarify_tool import clarify_tool

            def cb(q, c):
                return result  # the canonical text from the helper

            payload = clarify_tool(
                "Approve the plan?",
                choices=["Approve and ship", "Deny", "Request changes"],
                callback=cb,
            )
            import json
            parsed = json.loads(payload)
            assert parsed["user_response_mode"] == "selected"
            assert parsed["user_response"] == "Deny"

    def test_orphan_click_after_timeout_is_clean_noop(self):
        """Late Discord click after the gateway already cleared the entry
        must NOT crash, NOT fall back to truncated label, NOT pin anything.

        Today: the adapter reaches into _entries, gets None, falls back to
        the truncated label, and the agent receives a value that doesn't
        match choices_offered — clarify_tool tags it 'unresolved', the
        gate halts, the agent is stuck because every button is gray.
        With the helper: clean noop, the user can see the prompt expired,
        and the agent can re-prompt with the next clarify call.
        """
        from tools import clarify_gateway as cm

        cm.register(
            "discord-late-click",
            "sk-late",
            "Pick",
            ["A", "B"],
        )

        # Simulate timeout firing
        cm.wait_for_response("discord-late-click", timeout=0.05)
        # Late click arrives
        result = cm.resolve_choice_by_index("discord-late-click", 0)
        assert result is False  # clean noop, no fallback to truncated label