"""X2 — Mid-session operator-directive reminder regression tests.

Pinned contract (TDD-first, written BEFORE production code):

1. Every Nth turn (default N=5), a user-role operator-directive reminder
   message is appended to the in-flight api_messages list.
2. Non-Nth turns get NO reminder.
3. The reminder text includes the violation class enumeration.
4. The reminder injection does NOT mutate the agent's cached system prompt.
5. When cfg["agent"]["operator_directive_reminder_every_n_turns"] is 0 or
   negative, NO reminder is ever injected (operator opt-out).

Reminders are the X2 axis of the T2 plan — TPipe ships mid-session
reinforcement at Pipe.kt:3379; Hermes had no equivalent. These tests
pin the new Hermes mechanism to that behavior.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# Where the production functions will live (NOT YET IMPORTED — RED PHASE).
# The import will fail on first run; that's expected and correct.
try:
    from agent.prompt_builder import _OPERATOR_DIRECTIVE_REMINDER
except ImportError:  # RED phase marker
    _OPERATOR_DIRECTIVE_REMINDER = None  # type: ignore[assignment]

try:
    from agent.conversation_loop import _maybe_inject_operator_reminder
except ImportError:  # RED phase marker
    _maybe_inject_operator_reminder = None  # type: ignore[assignment]


def _baseline_messages() -> List[Dict[str, Any]]:
    """A baseline conversation history used by the tests."""
    return [
        {"role": "system", "content": "operator directive ..."},
        {"role": "user", "content": "user's turn 1"},
        {"role": "assistant", "content": "assistant's turn 1"},
        {"role": "user", "content": "user's turn 2"},
        {"role": "assistant", "content": "assistant's turn 2"},
    ]


# ── Test 1 ─────────────────────────────────────────────────────────────────
# On turn 5 with N=5, the reminder IS injected.

def test_reminder_present_on_turn_5():
    """Turn counter 5 with N=5 should produce a reminder-injected list."""
    msgs = _baseline_messages()
    out = _maybe_inject_operator_reminder(
        list(msgs),
        turn_count=5,
        every_n_turns=5,
        reminder_text=_OPERATOR_DIRECTIVE_REMINDER,
    )
    assert out is not None, "RED: _maybe_inject_operator_reminder must exist and return a list"
    assert len(out) > len(msgs), "RED: reminder must append a new message"
    last = out[-1]
    assert last["role"] == "user", f"RED: reminder must be user-role; got {last.get('role')!r}"
    # Reminder content text body
    assert isinstance(last.get("content"), str)
    assert len(last["content"]) > 100, "RED: reminder body must be substantial"


# ── Test 2 ─────────────────────────────────────────────────────────────────
# On turn 4 with N=5, NO reminder.

def test_reminder_absent_on_turn_4():
    """Turn 4 with N=5 should leave the messages untouched."""
    msgs = _baseline_messages()
    out = _maybe_inject_operator_reminder(
        list(msgs),
        turn_count=4,
        every_n_turns=5,
        reminder_text=_OPERATOR_DIRECTIVE_REMINDER,
    )
    assert out is not None
    assert len(out) == len(msgs), (
        f"RED: turn 4 (N=5) must not inject; got +{len(out) - len(msgs)} messages"
    )


# ── Test 3 ─────────────────────────────────────────────────────────────────
# Reminder content includes the violation class enumeration.

def test_reminder_includes_class_enumeration():
    """The reminder text must reference the violation classes by name."""
    assert _OPERATOR_DIRECTIVE_REMINDER is not None, (
        "RED: _OPERATOR_DIRECTIVE_REMINDER constant must exist in agent.prompt_builder"
    )
    assert isinstance(_OPERATOR_DIRECTIVE_REMINDER, str)
    assert "Class 1" in _OPERATOR_DIRECTIVE_REMINDER, (
        "RED: reminder must enumerate Class 1"
    )
    # Spot-check several classes — full enumeration is the contract.
    for cls in ("Class 2", "Class 4", "Class 7", "Class 8"):
        assert cls in _OPERATOR_DIRECTIVE_REMINDER, (
            f"RED: reminder must enumerate {cls}"
        )


# ── Test 4 ─────────────────────────────────────────────────────────────────
# Reminder injection does not mutate the agent's cached system prompt.

def test_reminder_does_not_mutate_cached_system_prompt():
    """Caching contract: a reminder is per-message, not a system-prompt mutation."""
    msgs = _baseline_messages()
    # Pre-existing system message stays in place, untouched
    original_sys = msgs[0]["content"]
    out = _maybe_inject_operator_reminder(
        list(msgs),
        turn_count=5,
        every_n_turns=5,
        reminder_text=_OPERATOR_DIRECTIVE_REMINDER,
    )
    assert out[0]["role"] == "system"
    assert out[0]["content"] == original_sys, (
        "RED: system message must remain byte-identical after reminder injection"
    )
    # Reminder is a NEW message at the end, not an in-place mutation
    assert len(out) - len(msgs) == 1, "RED: exactly one new message per reminder turn"


# ── Test 5 ─────────────────────────────────────────────────────────────────
# Operator opt-out: cfg["agent"]["operator_directive_reminder_every_n_turns"] <= 0.

def test_reminder_disabled_by_config():
    """When N=0 (or negative), no reminder — operator opt-out path."""
    msgs = _baseline_messages()
    out = _maybe_inject_operator_reminder(
        list(msgs),
        turn_count=10,
        every_n_turns=0,
        reminder_text=_OPERATOR_DIRECTIVE_REMINDER,
    )
    assert out is not None
    assert len(out) == len(msgs), "RED: N=0 must disable reminders"
