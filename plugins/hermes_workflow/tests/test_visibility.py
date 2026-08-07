"""Tests for the visibility layer: EventTranslator + card renderers + snapshot.

What we verify:

EventTranslator.translate():
- run_started -> GatewayNotice(kind="workflow_run_started")
- step_started -> ToolCallChunk(tool_name=step, index=0), resets per-step counter
- agent_call -> ToolCallChunk(tool_name="ask_agent", index=N), monotonic per step
- agent_response -> ToolCallFinished(tool_name="ask_agent", ok=True, index=N)
- verifier_returned (pass) -> ToolCallFinished(ok=True)
- verifier_returned (fail) -> ToolCallFinished(ok=False)
- step_completed -> ToolCallFinished(ok=True) with duration from evidence
- step_failed -> ToolCallFinished(ok=False)
- run_completed -> GatewayNotice(kind="workflow_run_completed")
- run_failed -> GatewayNotice with error info
- run_halted -> GatewayNotice with reason
- run_cancelled -> GatewayNotice with reason
- unknown kind -> None
- per-step call index resets when a new step_started arrives

EventTranslator.snapshot_for_run():
- returns schema with run_id, workflow, state, started_at, elapsed, steps[]
- each step has name, state, agent_calls, tokens_in/out, verifier_verdict, attempts
- step with active agent shows in active_agents
- finished agent has duration populated
- verifier verdict reflected (pass/fail)

ThreeTierCardRenderer (per spec section 16):
- Tier 1 (TUI/desktop) renders full card tree with nested agent sub-cards
- Tier 2 (chat) renders one line per step with collapsed agent summary
- Tier 3 (iMessage/SMS) renders plain text with no formatting
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from plugins.hermes_workflow.visibility import (
    EventTranslator,
    StepCallCounter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_started_ev(**overrides) -> dict:
    base = {
        "kind": "run_started",
        "run_id": "r_test123",
        "workflow": "demo_wf",
        "max_concurrent": 16,
        "max_total": 1000,
        "ts": 1700000000.0,
    }
    base.update(overrides)
    return base


def _step_started_ev(step: str, ts: float = 1700000001.0) -> dict:
    return {"kind": "step_started", "run_id": "r_test123",
            "step": step, "ts": ts}


def _agent_call_ev(step: str, prompt: str = "do the thing",
                    model: str = "sonnet",
                    ts: float = 1700000002.0) -> dict:
    return {"kind": "agent_call", "run_id": "r_test123", "step": step,
            "prompt_chars": len(prompt), "prompt_preview": prompt,
            "model": model, "max_tokens": None, "ts": ts}


def _agent_response_ev(step: str, duration: float = 1.2,
                         tokens_in: int = 100, tokens_out: int = 50,
                         ts: float = 1700000003.0) -> dict:
    return {"kind": "agent_response", "run_id": "r_test123", "step": step,
            "tool_calls": ["Read"], "tokens_in": tokens_in,
            "tokens_out": tokens_out, "duration": duration,
            "text_chars": 200, "ts": ts}


def _verifier_ev(step: str, valid: bool, reason: str = "ok",
                  ts: float = 1700000004.0) -> dict:
    return {"kind": "verifier_returned", "run_id": "r_test123",
            "step": step, "valid": valid, "reason": reason, "ts": ts}


def _step_completed_ev(step: str, duration: float = 0.5,
                        ts: float = 1700000005.0) -> dict:
    return {"kind": "step_completed", "run_id": "r_test123", "step": step,
            "evidence": {"files_changed": [], "commands_run": [],
                         "exit_codes": [], "tests_run": 0,
                         "tests_passed": 0,
                         "duration_seconds": duration},
            "attempts": 1, "ts": ts}


def _run_completed_ev(ts: float = 1700000010.0) -> dict:
    return {"kind": "run_completed", "run_id": "r_test123", "ts": ts}


# ---------------------------------------------------------------------------
# StepCallCounter
# ---------------------------------------------------------------------------

class TestStepCallCounter:
    def test_starts_at_zero(self):
        c = StepCallCounter()
        assert c.current() == 0

    def test_next_increments(self):
        c = StepCallCounter()
        assert c.next() == 1
        assert c.next() == 2
        assert c.next() == 3

    def test_reset_returns_to_zero(self):
        c = StepCallCounter()
        c.next()
        c.next()
        c.reset()
        assert c.current() == 0
        assert c.next() == 1


# ---------------------------------------------------------------------------
# EventTranslator.translate
# ---------------------------------------------------------------------------

class TestTranslateRunEvents:
    def test_run_started(self):
        tr = EventTranslator()
        out = tr.translate(_run_started_ev())
        assert out is not None
        # Stubs in test environment (no hermes gateway import): check attrs
        assert getattr(out, "kind", None) == "workflow_run_started"
        assert getattr(out, "text", "") == "run r_test123 started: demo_wf"
        assert out.extra["run_id"] == "r_test123"
        assert out.extra["workflow"] == "demo_wf"

    def test_run_completed(self):
        tr = EventTranslator()
        out = tr.translate(_run_completed_ev())
        assert getattr(out, "kind", None) == "workflow_run_completed"

    def test_run_failed_carries_error(self):
        tr = EventTranslator()
        out = tr.translate({
            "kind": "run_failed", "run_id": "r_x",
            "error": "boom", "error_type": "RuntimeError",
        })
        assert getattr(out, "kind", None) == "workflow_run_failed"
        assert out.extra["error"] == "boom"
        assert out.extra["error_type"] == "RuntimeError"

    def test_run_halted_carries_reason(self):
        tr = EventTranslator()
        out = tr.translate({
            "kind": "run_halted", "run_id": "r_x",
            "reason": "max_total reached",
        })
        assert getattr(out, "kind", None) == "workflow_run_halted"
        assert out.extra["reason"] == "max_total reached"

    def test_run_cancelled(self):
        tr = EventTranslator()
        out = tr.translate({"kind": "run_cancelled", "run_id": "r_x",
                            "reason": "user"})
        assert getattr(out, "kind", None) == "workflow_run_cancelled"
        assert out.extra["reason"] == "user"


class TestTranslateStepEvents:
    def test_step_started_resets_counter(self):
        tr = EventTranslator()
        # First step: two agent calls.
        tr.translate(_step_started_ev("alpha"))
        a1 = tr.translate(_agent_call_ev("alpha"))
        a2 = tr.translate(_agent_call_ev("alpha"))
        assert a1.index == 1
        assert a2.index == 2
        # New step starts: counter resets.
        tr.translate(_step_started_ev("beta"))
        b1 = tr.translate(_agent_call_ev("beta"))
        assert b1.index == 1

    def test_agent_call_preview_truncated(self):
        tr = EventTranslator()
        tr.translate(_step_started_ev("alpha"))
        out = tr.translate(_agent_call_ev(
            "alpha", prompt="x" * 200))
        # Preview is truncated to 80 chars + ellipsis.
        assert len(out.preview) == 81        # 80 + "…"

    def test_agent_call_short_preview(self):
        tr = EventTranslator()
        tr.translate(_step_started_ev("alpha"))
        out = tr.translate(_agent_call_ev("alpha", prompt="short"))
        assert out.preview == "short"
        assert "…" not in out.preview

    def test_agent_response_uses_current_index(self):
        tr = EventTranslator()
        tr.translate(_step_started_ev("alpha"))
        tr.translate(_agent_call_ev("alpha"))        # index 1
        out = tr.translate(_agent_response_ev("alpha", duration=2.5))
        assert out.index == 1
        assert out.duration == 2.5
        assert out.ok is True

    def test_verifier_pass_is_ok_true(self):
        tr = EventTranslator()
        out = tr.translate(_verifier_ev("alpha", valid=True))
        assert out.ok is True

    def test_verifier_fail_is_ok_false(self):
        tr = EventTranslator()
        out = tr.translate(_verifier_ev("alpha", valid=False, reason="no"))
        assert out.ok is False

    def test_step_completed_carries_duration(self):
        tr = EventTranslator()
        out = tr.translate(_step_completed_ev("alpha", duration=4.7))
        assert out.duration == 4.7
        assert out.ok is True

    def test_step_failed_is_not_ok(self):
        tr = EventTranslator()
        out = tr.translate({"kind": "step_failed", "run_id": "r_x",
                            "step": "alpha", "error": "boom"})
        assert out.ok is False


class TestTranslateUnknown:
    def test_unknown_kind_returns_none(self):
        tr = EventTranslator()
        assert tr.translate({"kind": "some_future_event"}) is None

    def test_completely_missing_kind_returns_none(self):
        tr = EventTranslator()
        assert tr.translate({"foo": "bar"}) is None


class TestTranslateAgentCallIndex:
    def test_indices_are_monotonic_per_step(self):
        tr = EventTranslator()
        tr.translate(_step_started_ev("alpha"))
        for expected in (1, 2, 3, 4):
            out = tr.translate(_agent_call_ev("alpha"))
            assert out.index == expected

    def test_indices_reset_between_steps(self):
        tr = EventTranslator()
        tr.translate(_step_started_ev("alpha"))
        for _ in range(5):
            tr.translate(_agent_call_ev("alpha"))
        tr.translate(_step_started_ev("beta"))
        # First agent call in beta gets index 1, not 6.
        out = tr.translate(_agent_call_ev("beta"))
        assert out.index == 1


# ---------------------------------------------------------------------------
# EventTranslator.snapshot_for_run
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_empty_journal(self):
        tr = EventTranslator()
        snap = tr.snapshot_for_run("r_empty", [])
        assert snap["run_id"] == "r_empty"
        assert snap["state"] == "unknown"
        assert snap["steps"] == []

    def test_run_started_then_completed(self):
        tr = EventTranslator()
        events = [_run_started_ev(), _run_completed_ev()]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["state"] == "done"
        assert snap["workflow"] == "demo_wf"
        assert snap["started_at"] == 1700000000.0
        assert snap["elapsed_seconds"] == 10.0    # 1700000010 - 1700000000

    def test_step_with_one_agent_call_then_response(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            _step_started_ev("alpha"),
            _agent_call_ev("alpha"),
            _agent_response_ev("alpha", duration=2.5,
                                tokens_in=100, tokens_out=50),
            _step_completed_ev("alpha", duration=3.0),
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert len(snap["steps"]) == 1
        step = snap["steps"][0]
        assert step["name"] == "alpha"
        assert step["agent_calls"] == 1
        assert step["tokens_in"] == 100
        assert step["tokens_out"] == 50
        assert step["state"] == "verified"
        assert step["duration_seconds"] == 3.0
        # Active agent was finished by the response event.
        assert len(step["active_agents"]) == 1
        assert step["active_agents"][0]["duration_so_far_seconds"] == 2.5
        assert step["active_agents"][0]["ok"] is True

    def test_verifier_pass_verdict(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            _step_started_ev("guarded"),
            _verifier_ev("guarded", valid=True, reason="27/27 pass"),
            _step_completed_ev("guarded"),
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["steps"][0]["verifier_verdict"] == "pass"

    def test_verifier_fail_verdict(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            _step_started_ev("guarded"),
            _verifier_ev("guarded", valid=False, reason="tests failed"),
            _step_completed_ev("guarded"),
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["steps"][0]["verifier_verdict"] == "fail"

    def test_run_failed_state(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            {"kind": "run_failed", "run_id": "r_test",
             "error": "boom", "error_type": "RuntimeError"},
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["state"] == "failed"

    def test_run_halted_state(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            {"kind": "run_halted", "run_id": "r_test",
             "reason": "max_total"},
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["state"] == "halted"

    def test_run_cancelled_state(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            {"kind": "run_cancelled", "run_id": "r_test"},
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["state"] == "cancelled"


# ---------------------------------------------------------------------------
# Three-tier card renderer
# ---------------------------------------------------------------------------

class TestThreeTierCardRenderer:
    """The renderer takes the snapshot dict from EventTranslator and
    returns a string in the tier-specific format.

    Tier 1 (TUI/desktop): full card tree, with nested agent sub-cards.
    Tier 2 (Discord/Telegram/Slack): one line per step, collapsed agent summary.
    Tier 3 (iMessage/SMS): plain text, one line per step, no formatting.
    """

    def test_tier1_full_card_tree(self):
        from plugins.hermes_workflow.visibility import ThreeTierCardRenderer
        r = ThreeTierCardRenderer()
        snap = {
            "run_id": "r_test",
            "workflow": "demo_wf",
            "state": "done",
            "started_at": 1700000000.0,
            "elapsed_seconds": 12.4,
            "steps": [
                {"name": "alpha", "state": "verified",
                 "agent_calls": 1, "tokens_in": 89, "tokens_out": 53,
                 "verifier_verdict": "pass", "attempts": 1,
                 "duration_seconds": 0.3,
                 "active_agents": [
                     {"index": 1, "prompt_preview": "do alpha",
                      "duration_so_far_seconds": 0.3, "ok": True}
                 ]},
            ],
        }
        out = r.render(snap, tier=1)
        # State is "done", so the run boundary markers are ■ (done), not ▶ (running).
        assert "■" in out
        assert "▶" not in out        # running marker not used for done state
        assert "alpha" in out
        assert "verified" in out or "pass" in out
        assert "do alpha" in out              # nested agent preview
        assert "↳ done in 0.3s" in out         # nested agent completion

    def test_tier2_one_line_per_step(self):
        from plugins.hermes_workflow.visibility import ThreeTierCardRenderer
        r = ThreeTierCardRenderer()
        snap = {
            "run_id": "r_test",
            "workflow": "demo_wf",
            "state": "done",
            "started_at": 1700000000.0,
            "elapsed_seconds": 12.4,
            "steps": [
                {"name": "alpha", "state": "verified",
                 "agent_calls": 1, "tokens_in": 89, "tokens_out": 53,
                 "verifier_verdict": "pass", "attempts": 1,
                 "duration_seconds": 0.3, "active_agents": []},
                {"name": "beta", "state": "failed",
                 "agent_calls": 0, "tokens_in": 0, "tokens_out": 0,
                 "verifier_verdict": None, "attempts": 1,
                 "duration_seconds": 0.0, "active_agents": []},
            ],
        }
        out = r.render(snap, tier=2)
        # No agent previews in tier 2.
        assert "do alpha" not in out
        # One line per step.
        for step_name in ("alpha", "beta"):
            assert step_name in out
        # Failed step shows its state.
        assert "fail" in out
        # Verdict pass suffix.
        assert "pass" in out

    def test_tier3_plain_text(self):
        from plugins.hermes_workflow.visibility import ThreeTierCardRenderer
        r = ThreeTierCardRenderer()
        snap = {
            "run_id": "r_test",
            "workflow": "demo_wf",
            "state": "done",
            "started_at": 1700000000.0,
            "elapsed_seconds": 12.4,
            "steps": [
                {"name": "alpha", "state": "verified",
                 "agent_calls": 1, "tokens_in": 89, "tokens_out": 53,
                 "verifier_verdict": "pass", "attempts": 1,
                 "duration_seconds": 0.3, "active_agents": []},
            ],
        }
        out = r.render(snap, tier=3)
        # Plain text: no Unicode markers.
        assert "▶" not in out
        assert "■" not in out
        assert "🔧" not in out
        # Step is named plainly.
        assert "alpha" in out
        # Verdict stated as plain word.
        assert "pass" in out

    def test_unknown_tier_raises(self):
        from plugins.hermes_workflow.visibility import ThreeTierCardRenderer
        r = ThreeTierCardRenderer()
        snap = {"run_id": "r_x", "state": "running",
                "workflow": "x", "started_at": 0, "elapsed_seconds": 0,
                "steps": []}
        with pytest.raises(ValueError, match="tier"):
            r.render(snap, tier=99)


# ---------------------------------------------------------------------------
# Integration: a workflow's journal events round-trip through the translator
# ---------------------------------------------------------------------------

class TestEndToEndTranslation:
    def test_full_run_journal_translates_cleanly(self):
        """Simulate a full run's journal events end-to-end. Every event
        maps to a StreamEvent; no exceptions; no None returns for known
        kinds."""
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            _step_started_ev("list"),
            _agent_call_ev("list"),
            _agent_response_ev("list"),
            _verifier_ev("list", valid=True),
            _step_completed_ev("list"),
            _step_started_ev("process"),
            _agent_call_ev("process", prompt="process the diff"),
            _agent_response_ev("process"),
            _verifier_ev("process", valid=False, reason="diff too large"),
            _verifier_ev("process", valid=True, reason="ok"),    # retry
            _step_completed_ev("process"),
            _run_completed_ev(),
        ]
        out_events = [tr.translate(e) for e in events]
        # All translated to non-None StreamEvents.
        assert all(e is not None for e in out_events)
        # Two run-state events at boundaries, six step events, plus
        # agent_call/response/verifier events for each step.
        kinds = [type(e).__name__ for e in out_events]
        # The first is a GatewayNotice (run_started).
        assert "GatewayNotice" in kinds[0] or "Notice" in kinds[0]

    def test_snapshot_after_full_run_is_consistent(self):
        tr = EventTranslator()
        events = [
            _run_started_ev(),
            _step_started_ev("only"),
            _agent_call_ev("only"),
            _agent_response_ev("only", duration=2.0,
                                tokens_in=100, tokens_out=50),
            _step_completed_ev("only", duration=2.5),
            _run_completed_ev(),
        ]
        snap = tr.snapshot_for_run("r_test", events)
        assert snap["state"] == "done"
        assert snap["elapsed_seconds"] == 10.0
        assert len(snap["steps"]) == 1
        assert snap["steps"][0]["tokens_in"] == 100
        assert snap["steps"][0]["tokens_out"] == 50
        assert snap["steps"][0]["state"] == "verified"
