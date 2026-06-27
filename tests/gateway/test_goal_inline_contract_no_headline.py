"""Regression tests for /goal when input is ONLY contract fields.

The bug: when the user typed ``/goal verify: tests pass`` (no headline),
``parse_contract`` correctly identified the contract fields but returned
an empty headline. The gateway ``_handle_goal_command`` then did
``args = headline or args`` — which re-fed the original input back as the
goal text. The user-visible result was a goal whose ``goal`` field was
the literal string ``"verify: tests pass"`` AND a contract carrying the
same verification — the agent was told to work toward the string
``"verify: tests pass"`` instead of the user's actual intent. From the
chat side, the reply also had to render the duplicate.

The fix: ``parse_contract`` now synthesizes a non-empty headline from the
contract when the user provided only fields, and the callers drop the
``or args`` fallback that re-injected the stripped field lines.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource


# ────────────────────────────────────────────────────────────────────
# Fixtures — minimal GatewayRunner via __new__ so we skip the heavy
# __init__ (env loading, platform adapters, process registry wiring).
# The runner has the slash-command mixin mounted via MRO so the test
# exercises the real handler.
# ────────────────────────────────────────────────────────────────────


def _make_runner(tmp_path, monkeypatch):
    """Construct a GatewayRunner wired to a temp HERMES_HOME.

    Bypasses ``__init__`` (env, adapters, process registry) so the test
    doesn't need a live gateway. Wires the minimal attributes the
    ``_handle_goal_command`` code path reads: ``adapters``, ``session_store``
    (mocked), plus the goal-continuation enqueue helpers which the handler
    calls after a successful set.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal-module DB cache so it picks up the new HERMES_HOME.
    from hermes_cli import goals

    goals._DB_CACHE.clear()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}

    # Session store stub: returns a fresh SessionEntry per source so each
    # test gets an isolated session_id without polluting the next.
    store = MagicMock()

    def _get_or_create(source):
        sid = f"test-sid-{source.chat_id}-{source.thread_id}"
        return SessionEntry(
            session_key=f"test:{source.chat_id}:{source.thread_id}",
            session_id=sid,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=source.platform,
            display_name="Test",
        )

    store.get_or_create_session.side_effect = _get_or_create
    runner.session_store = store

    # The post-set kickoff path needs these — stub them so we don't need
    # a real FIFO enqueue or pending-continuation tracker.
    runner._session_key_for_source = lambda src: (
        f"{src.platform.value}:{src.chat_id}:{src.thread_id}"
        if getattr(src, "platform", None) is not None
        and not isinstance(src.platform, str)
        else f"{src.platform}:{src.chat_id}:{src.thread_id}"
    )
    runner._clear_goal_pending_continuations = lambda *_a, **_k: None
    runner._enqueue_fifo = lambda *_a, **_k: None

    return runner


def _discord_source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="1493384010691510282",
        thread_id="1520067260939632743",
        user_id="111",
        user_name="Echo_of_Maridia",
    )


def _event(text, source=None):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source or _discord_source(),
        message_id="m1",
    )


# ────────────────────────────────────────────────────────────────────
# Regression: /goal with ONLY contract fields must produce a clean
# goal text (no re-injected ``verify:`` / ``outcome:`` lines) and a
# non-empty contract.
# ────────────────────────────────────────────────────────────────────


def test_goal_verify_only_synthesizes_clean_headline(tmp_path, monkeypatch):
    """``/goal verify: tests pass`` must not echo ``verify: tests pass``
    as the goal text."""
    runner = _make_runner(tmp_path, monkeypatch)
    out = asyncio.run(runner._handle_goal_command(_event("/goal verify: tests pass")))

    # Reply must announce a real goal set, not "No active goal" and not
    # the bare field string.
    assert "Goal set" in out
    assert "verify:" not in out.split("\nCompletion contract:")[0]

    # Goal stored on disk must not carry the stripped field prefix.
    from hermes_cli.goals import GoalManager

    sid = "test-sid-1493384010691510282-1520067260939632743"
    mgr = GoalManager(session_id=sid)
    assert mgr.state is not None
    assert "verify:" not in mgr.state.goal
    assert mgr.state.goal  # non-empty
    # The contract still carries the verification — that part of the
    # user's intent is preserved.
    assert mgr.state.contract.verification == "tests pass"


def test_goal_outcome_only_uses_outcome_as_headline(tmp_path, monkeypatch):
    """``/goal outcome: fix the resume race`` — outcome is the canonical
    "done"; it IS the headline."""
    runner = _make_runner(tmp_path, monkeypatch)
    out = asyncio.run(runner._handle_goal_command(_event("/goal outcome: fix the resume race")))

    assert "Goal set" in out

    from hermes_cli.goals import GoalManager

    sid = "test-sid-1493384010691510282-1520067260939632743"
    mgr = GoalManager(session_id=sid)
    assert mgr.state is not None
    assert mgr.state.goal == "fix the resume race"
    assert mgr.state.contract.outcome == "fix the resume race"


def test_goal_multiple_fields_uses_verification_synthesis(tmp_path, monkeypatch):
    """Multiple contract fields, no headline → synthesizer picks the
    verification as the headline anchor."""
    runner = _make_runner(tmp_path, monkeypatch)
    text = (
        "/goal verify: tests pass\n"
        "constraints: keep the /login response shape\n"
        "boundaries: only touch services/auth and its tests"
    )
    out = asyncio.run(runner._handle_goal_command(_event(text)))

    assert "Goal set" in out

    from hermes_cli.goals import GoalManager

    sid = "test-sid-1493384010691510282-1520067260939632743"
    mgr = GoalManager(session_id=sid)
    assert mgr.state is not None
    # The "Achieve:" synthesis prefix must appear in the goal text,
    # referencing the verification line.
    assert "tests pass" in mgr.state.goal
    # None of the field prefixes leak back into the goal text.
    for prefix in ("verify:", "constraints:", "boundaries:"):
        assert prefix not in mgr.state.goal
    # Contract fields are preserved verbatim.
    assert mgr.state.contract.verification == "tests pass"
    assert mgr.state.contract.constraints == "keep the /login response shape"
    assert mgr.state.contract.boundaries == "only touch services/auth and its tests"


def test_goal_normal_text_unchanged(tmp_path, monkeypatch):
    """Sanity: a plain free-form goal must still work end-to-end and
    not pick up a synthesized prefix."""
    runner = _make_runner(tmp_path, monkeypatch)
    out = asyncio.run(runner._handle_goal_command(_event("/goal fix the resume race")))

    assert "Goal set" in out
    assert "fix the resume race" in out

    from hermes_cli.goals import GoalManager

    sid = "test-sid-1493384010691510282-1520067260939632743"
    mgr = GoalManager(session_id=sid)
    assert mgr.state is not None
    assert mgr.state.goal == "fix the resume race"
    assert mgr.state.contract.is_empty()


def test_goal_headline_plus_fields_still_works(tmp_path, monkeypatch):
    """Existing behavior — headline + inline fields — must not regress."""
    runner = _make_runner(tmp_path, monkeypatch)
    text = (
        "/goal Migrate auth to JWT\n"
        "verify: the auth test suite passes\n"
        "constraints: keep the /login response shape unchanged"
    )
    out = asyncio.run(runner._handle_goal_command(_event(text)))

    assert "Goal set" in out

    from hermes_cli.goals import GoalManager

    sid = "test-sid-1493384010691510282-1520067260939632743"
    mgr = GoalManager(session_id=sid)
    assert mgr.state is not None
    # Headline is the goal — no "Achieve:" synthesis, no field prefixes.
    assert mgr.state.goal == "Migrate auth to JWT"
    assert "Achieve:" not in mgr.state.goal
    assert mgr.state.contract.verification == "the auth test suite passes"
    assert mgr.state.contract.constraints == "keep the /login response shape unchanged"


def test_goal_after_set_status_reports_active(tmp_path, monkeypatch):
    """After the inline-contract /goal, /goal status must report the
    active goal (not "No active goal"). This is the symptom the
    operator saw: setting a goal looked like it produced "No active goal"
    because the stored goal text was empty/unparseable and a later
    status check showed the empty state."""
    runner = _make_runner(tmp_path, monkeypatch)

    # Set with inline contract fields only.
    asyncio.run(runner._handle_goal_command(_event("/goal verify: tests pass")))

    # Status check must show an active goal, not the empty state.
    out = asyncio.run(runner._handle_goal_command(_event("/goal status")))
    assert "No active goal" not in out
    assert "active" in out.lower()
