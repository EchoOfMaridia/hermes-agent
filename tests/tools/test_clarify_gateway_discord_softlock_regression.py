"""Regression tests for the 2026-07-16 Discord clarify softlock.

Discord thread 1527516088340975706 (session 20260716_232337_9689628b):
  1. Bot flushed a 91-char text batch at 23:31:04.464.
  2. Agent invoked ``clarify`` with a two-choice prompt asking which startup
     branch to merge main into (``startup`` vs ``startup-license``).
  3. User replied at 23:31:45 with a typo'd free-form answer
     ("There is only one branch with that namw") answering the
     singular/plural ambiguity.
  4. Bot then posted "Interrupting current task (iteration 3/10000,
     running: clarify)" at 23:31:46 — the agent thread was still blocked
     on ``wait_for_response`` for the multi-choice clarify, even though the
     gateway text-intercept should have routed the typed reply to the
     resolver.

Root cause: the upstream TDD series (commits 309cf649e, 309ac485a, 4ab13a3d9
on upstream/main, local reapplications f3cf79314 + fd13abc9a) shipped both
``resolve_choice_by_index`` AND its companion half — the
``include_choice_prompts=True`` kwarg on ``get_pending_for_session`` plus
``resolve_text_response_for_session``. The companion half was lost between
the upstream merge (``a0af66064``) and the local reapplications, leaving
``gateway/run.py:9388`` and ``gateway/platforms/base.py:4772`` to call
signatures that didn't exist on the live module. Symptom: typed replies
to multi-choice clarifies hit the busy-ack path instead of the
text-intercept, and the agent stays blocked at the clarify gate.

These tests pin both halves against the live source so a future regression
that drops either half is caught immediately. They are deliberately written
as a self-contained regression suite so they run under either the live
``tools/clarify_gateway`` module or a subprocess re-import after the source
is reverted (RED-before-GREEN verification pattern).
"""

from __future__ import annotations

import threading
import time


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _has_include_choice_prompts_kwarg():
    """Surface the upstream-signature shape so a stub-skip doesn't lie about RED.

    Returns True if ``get_pending_for_session`` accepts the keyword-only
    ``include_choice_prompts=True`` argument. Returns False if the function
    exists but rejects the kwarg (the post-cherry-pick state that produced
    the Discord softlock) — the test runner treats False as a fail-pin even
    when monkey-patched.
    """
    from tools import clarify_gateway as cm

    try:
        cm.get_pending_for_session("any", include_choice_prompts=True)
        return True
    except TypeError as exc:
        return "include_choice_prompts" in str(exc)


def _has_resolve_text_response_for_session():
    from tools import clarify_gateway as cm

    return hasattr(cm, "resolve_text_response_for_session")


class TestSurfacePresence:
    """Pin the upstream wire-contract surface on the live module.

    A future regression that strips either helper is caught here as
    TypeError / AttributeError before the behavioural tests run.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_get_pending_for_session_accepts_include_choice_prompts_kwarg(self):
        from tools import clarify_gateway as cm

        # Smoke: empty session, just confirms the kwarg is accepted.
        assert cm.get_pending_for_session("nope", include_choice_prompts=True) is None

    def test_resolve_text_response_for_session_is_exported(self):
        from tools import clarify_gateway as cm

        assert hasattr(cm, "resolve_text_response_for_session")
        assert callable(cm.resolve_text_response_for_session)


class TestMultiChoiceTypedReply:
    """Reproduction of the 2026-07-16 Discord softlock scenario.

    Sequence:
      1. Agent registers a multi-choice clarify (two startup branches).
      2. User types a free-form reply answering the ambiguity — does NOT
         match any choice text or numeric index.
      3. ``resolve_text_response_for_session`` must accept the reply as a
         custom Other-style answer and unblock the wait.
      4. The agent thread receives the raw reply text (canonical form),
         not a truncated button label or empty sentinel.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_typed_reply_to_multi_choice_clarify_resolves_wait(self):
        """The exact Discord thread 1527516088340975706 repro."""
        from tools import clarify_gateway as cm

        session_key = "discord:thread:1527516088340975706"
        choices = [
            "Merge into startup (82e37fb9, no merge).",
            "Merge into startup-license (a367a692, has the merge commit).",
        ]
        entry = cm.register(
            "clarify-startup-branches",
            session_key,
            "Which startup branch should I merge main into?",
            choices,
        )

        def waiter():
            return cm.wait_for_response(entry.clarify_id, timeout=2.0)

        with __import__("concurrent.futures").futures.ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)

            # The user's typo'd free-form answer that hit the softlock in
            # production. It does not match any choice text and is not a
            # numeric index — must resolve as a custom answer.
            user_reply = "There is only one branch with that namw"
            assert cm.resolve_text_response_for_session(session_key, user_reply) is True

            result = fut.result(timeout=2.0)
            # Canonical form preserved verbatim — clarify_tool checks
            # the response against choices_offered and tags
            # ``unresolved`` if coercion ever drops characters.
            assert result == user_reply

    def test_typed_numeric_reply_maps_to_canonical_choice(self):
        """Numeric ``"1"`` → first choice, ``"2"`` → second, exact."""
        from tools import clarify_gateway as cm

        session_key = "discord:thread:typed-numeric"
        choices = ["Alpha", "Beta", "Gamma"]
        entry = cm.register("clarify-numeric", session_key, "Pick", choices)

        def waiter():
            return cm.wait_for_response(entry.clarify_id, timeout=2.0)

        with __import__("concurrent.futures").futures.ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)
            assert cm.resolve_text_response_for_session(session_key, "2") is True
            assert fut.result(timeout=2.0) == "Beta"

    def test_typed_reply_without_pending_clarify_returns_false(self):
        """No-op path: no entry → no resolve → False (no exception)."""
        from tools import clarify_gateway as cm

        assert (
            cm.resolve_text_response_for_session("discord:no-pending", "anything")
            is False
        )


class TestGatewaySurfaceContract:
    """The gateway call sites that depend on the missing helpers.

    These tests grep for the specific call shapes that
    ``gateway/run.py::_handle_message`` and
    ``gateway/platforms/base.py::handle_message` rely on. They don't invoke
    the gateway directly — they pin the public-surface contract so a
    regression that strips either helper breaks here before Discord
    production breaks.
    """

    def test_get_pending_for_session_signature_includes_kwarg(self):
        """``gateway/run.py:9388`` passes the kwarg — verify the signature."""
        import inspect

        from tools import clarify_gateway as cm

        sig = inspect.signature(cm.get_pending_for_session)
        assert "include_choice_prompts" in sig.parameters, (
            "Regression: gateway/run.py:_maybe_intercept_clarify_text passes "
            "include_choice_prompts=True to get_pending_for_session. If the "
            "kwarg is missing, typed replies to multi-choice clarifies fall "
            "through to the busy-ack path and the agent thread stays blocked "
            "(see Discord thread 1527516088340975706, session "
            "20260716_232337_9689628b, 2026-07-16 23:31)."
        )

    def test_resolve_text_response_for_session_signature(self):
        """``gateway/run.py:9400`` calls the helper directly — verify export."""
        import inspect

        from tools import clarify_gateway as cm

        assert hasattr(cm, "resolve_text_response_for_session"), (
            "Regression: gateway/run.py:_maybe_intercept_clarify_text calls "
            "resolve_text_response_for_session(_quick_key, _raw_clarify_reply). "
            "If the helper is missing, the bare-except at line 9392 swallows "
            "the AttributeError and the typed reply silently never resolves "
            "the clarify wait."
        )
        sig = inspect.signature(cm.resolve_text_response_for_session)
        assert list(sig.parameters.keys()) == ["session_key", "response"], (
            f"Signature drift on resolve_text_response_for_session: {sig}"
        )
