"""Tests for Step 3: Journal (append-only event log with fsync).

What we verify:

Basic append + close:
- Append adds an event with a ts timestamp
- File contains one JSON object per line
- close() flushes and closes the file handle
- Re-opening the same run_id appends to the existing file
- Each line is valid JSON

Replay:
- replay() reads all events back
- replay() returns empty events list when file doesn't exist
- replay() of a fresh Journal is read-only (append raises)
- replay() restores events in the order they were written

fsync + durability:
- Each append calls fsync (verifiable by inspecting write calls)
- A partial-line file (from a kill-9 mid-write) replays cleanly, skipping the truncated line

Schema:
- The reserved KIND_* constants are present and stable
- New kind values pass through without breaking replay

Inspection helpers:
- steps_completed() returns step names in order
- steps_failed() returns {name: error}
- verifier_verdicts() returns the verifier events
- agent_calls() returns the agent_call events
- final_outcome() returns the last terminal kind, or None

Context manager:
- with Journal(...) as j: ... is equivalent to append + close
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugins.hermes_workflow.journal import Journal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_journal_root(tmp_path: Path) -> Path:
    return tmp_path / "journals"


# ---------------------------------------------------------------------------
# Basic append + close
# ---------------------------------------------------------------------------

class TestAppendAndClose:
    def test_append_sets_timestamp(self, tmp_journal_root):
        j = Journal("r_test", tmp_journal_root)
        try:
            j.append({"kind": "run_started", "workflow": "demo"})
            assert len(j.events) == 1
            assert "ts" in j.events[0]
            assert isinstance(j.events[0]["ts"], float)
        finally:
            j.close()

    def test_file_is_jsonl(self, tmp_journal_root):
        j = Journal("r_test", tmp_journal_root)
        try:
            j.append({"kind": "step_started", "step": "alpha"})
            j.append({"kind": "step_completed", "step": "alpha"})
        finally:
            j.close()

        lines = (tmp_journal_root / "r_test.journal").read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_close_releases_handle(self, tmp_journal_root):
        j = Journal("r_close", tmp_journal_root)
        j.append({"kind": "test"})
        j.close()
        # Re-appending should now raise.
        with pytest.raises(RuntimeError, match="read-only"):
            j.append({"kind": "test2"})

    def test_reopen_appends_to_existing(self, tmp_journal_root):
        j1 = Journal("r_persist", tmp_journal_root)
        j1.append({"kind": "step_started", "step": "a"})
        j1.close()

        j2 = Journal("r_persist", tmp_journal_root)
        try:
            j2.append({"kind": "step_completed", "step": "a"})
        finally:
            j2.close()

        # Both events present in file.
        lines = (tmp_journal_root / "r_persist.journal").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["kind"] == "step_started"
        assert json.loads(lines[1])["kind"] == "step_completed"

    def test_append_preserves_existing_ts(self, tmp_journal_root):
        j = Journal("r_ts", tmp_journal_root)
        try:
            j.append({"kind": "run_started", "ts": 12345.678})
            assert j.events[0]["ts"] == 12345.678
        finally:
            j.close()

    def test_context_manager(self, tmp_journal_root):
        with Journal("r_ctx", tmp_journal_root) as j:
            j.append({"kind": "run_started"})

        # File exists, was closed by __exit__.
        assert (tmp_journal_root / "r_ctx.journal").exists()
        # Re-appending after the with-block raises.
        with pytest.raises(RuntimeError, match="read-only"):
            j.append({"kind": "extra"})


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_reads_all_events(self, tmp_journal_root):
        # Write.
        j = Journal("r_replay", tmp_journal_root)
        try:
            j.append({"kind": "run_started", "workflow": "x"})
            j.append({"kind": "step_started", "step": "alpha"})
            j.append({"kind": "step_completed", "step": "alpha"})
            j.append({"kind": "run_completed"})
        finally:
            j.close()

        # Read.
        replayed = Journal.replay("r_replay", tmp_journal_root)
        assert len(replayed.events) == 4
        assert replayed.events[0]["kind"] == "run_started"
        assert replayed.events[1]["kind"] == "step_started"
        assert replayed.events[2]["kind"] == "step_completed"
        assert replayed.events[3]["kind"] == "run_completed"

    def test_replay_missing_file_returns_empty(self, tmp_journal_root):
        replayed = Journal.replay("r_never_written", tmp_journal_root)
        assert replayed.events == []
        # Not a hard failure — inspection tools want to see "nothing here"
        # rather than crash on missing files.

    def test_replay_is_read_only(self, tmp_journal_root):
        j = Journal("r_replay_ro", tmp_journal_root)
        try:
            j.append({"kind": "run_started"})
        finally:
            j.close()

        replayed = Journal.replay("r_replay_ro", tmp_journal_root)
        with pytest.raises(RuntimeError, match="read-only"):
            replayed.append({"kind": "step_started"})

    def test_replay_skips_truncated_lines(self, tmp_journal_root):
        # Simulate a kill-9 mid-write: the file has one complete JSON line
        # and a half-written line that is not valid JSON.
        tmp_journal_root.mkdir(parents=True, exist_ok=True)
        path = tmp_journal_root / "r_partial.journal"
        truncated = '{"kind": "step_started", "step'    # unterminated
        with open(path, "w") as fp:
            fp.write('{"kind": "run_started"}\n')
            fp.write(truncated)
        replayed = Journal.replay("r_partial", tmp_journal_root)
        assert len(replayed.events) == 1
        assert replayed.events[0]["kind"] == "run_started"

    def test_replay_skips_blank_lines(self, tmp_journal_root):
        tmp_journal_root.mkdir(parents=True, exist_ok=True)
        path = tmp_journal_root / "r_blank.journal"
        with open(path, "w") as fp:
            fp.write('{"kind": "run_started"}\n')
            fp.write("\n")
            fp.write('{"kind": "step_completed"}\n')
        replayed = Journal.replay("r_blank", tmp_journal_root)
        assert len(replayed.events) == 2

    def test_replay_tolerates_nested_data(self, tmp_journal_root):
        j = Journal("r_nested", tmp_journal_root)
        try:
            j.append({"kind": "step_completed", "step": "alpha",
                       "evidence": {"files_changed": ["a.py"], "tests_run": 5}})
        finally:
            j.close()

        replayed = Journal.replay("r_nested", tmp_journal_root)
        assert replayed.events[0]["evidence"]["files_changed"] == ["a.py"]
        assert replayed.events[0]["evidence"]["tests_run"] == 5


# ---------------------------------------------------------------------------
# fsync durability
# ---------------------------------------------------------------------------

class TestFsync:
    def test_append_calls_fsync(self, tmp_journal_root, monkeypatch):
        """Verify fsync is called per append by monkeypatching os.fsync."""
        fsync_calls: list[int] = []

        original_fsync = os.fsync

        def tracking_fsync(fd: int) -> None:
            fsync_calls.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", tracking_fsync)

        j = Journal("r_fsync", tmp_journal_root)
        try:
            j.append({"kind": "run_started"})
            j.append({"kind": "step_started", "step": "a"})
            j.append({"kind": "step_completed", "step": "a"})
        finally:
            j.close()

        # 3 appends + 1 close = 4 fsync calls.
        assert len(fsync_calls) == 4

    def test_data_survives_immediate_replay(self, tmp_journal_root):
        """After append + close, replay must see all events without sleeps."""
        j = Journal("r_durable", tmp_journal_root)
        try:
            for i in range(50):
                j.append({"kind": "agent_call", "n": i})
        finally:
            j.close()

        replayed = Journal.replay("r_durable", tmp_journal_root)
        assert len(replayed.events) == 50
        assert [e["n"] for e in replayed.events] == list(range(50))


# ---------------------------------------------------------------------------
# Schema / KIND constants
# ---------------------------------------------------------------------------

class TestSchema:
    def test_reserved_kinds_present(self):
        expected = [
            "run_started", "run_completed", "run_failed",
            "run_halted", "run_cancelled",
            "step_started", "step_completed", "step_failed",
            "verifier_returned", "agent_call", "agent_response",
        ]
        for name in expected:
            kind_value = getattr(Journal, f"KIND_{name.upper()}")
            assert kind_value == name

    def test_unknown_kinds_round_trip(self, tmp_journal_root):
        j = Journal("r_unknown", tmp_journal_root)
        try:
            j.append({"kind": "future_event_we_dont_know_about", "data": "ok"})
        finally:
            j.close()

        replayed = Journal.replay("r_unknown", tmp_journal_root)
        assert replayed.events[0]["kind"] == "future_event_we_dont_know_about"
        assert replayed.events[0]["data"] == "ok"


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------

class TestInspectionHelpers:
    def _build(self, tmp_journal_root):
        j = Journal("r_inspect", tmp_journal_root)
        j.append({"kind": "run_started", "workflow": "demo"})
        j.append({"kind": "step_started", "step": "alpha"})
        j.append({"kind": "verifier_returned", "step": "alpha",
                   "valid": True, "reason": "ok"})
        j.append({"kind": "step_completed", "step": "alpha"})
        j.append({"kind": "step_started", "step": "beta"})
        j.append({"kind": "verifier_returned", "step": "beta",
                   "valid": False, "reason": "tests failed"})
        j.append({"kind": "step_failed", "step": "beta", "error": "verifier rejected"})
        j.append({"kind": "agent_call", "step": "alpha", "prompt_chars": 100})
        j.append({"kind": "agent_response", "step": "alpha", "tokens_out": 50})
        j.append({"kind": "run_failed"})
        j.close()
        return Journal.replay("r_inspect", tmp_journal_root)

    def test_steps_completed(self, tmp_journal_root):
        j = self._build(tmp_journal_root)
        assert j.steps_completed() == ["alpha"]

    def test_steps_failed(self, tmp_journal_root):
        j = self._build(tmp_journal_root)
        failed = j.steps_failed()
        assert "beta" in failed
        assert "verifier rejected" in failed["beta"]

    def test_verifier_verdicts(self, tmp_journal_root):
        j = self._build(tmp_journal_root)
        verdicts = j.verifier_verdicts()
        assert len(verdicts) == 2
        assert verdicts[0]["valid"] is True
        assert verdicts[1]["valid"] is False

    def test_agent_calls(self, tmp_journal_root):
        j = self._build(tmp_journal_root)
        calls = j.agent_calls()
        assert len(calls) == 1
        assert calls[0]["prompt_chars"] == 100

    def test_final_outcome_returns_last_terminal_kind(self, tmp_journal_root):
        j = self._build(tmp_journal_root)
        assert j.final_outcome() == "run_failed"

    def test_final_outcome_in_order(self, tmp_journal_root):
        j = Journal("r_outcome", tmp_journal_root)
        j.append({"kind": "run_started"})
        j.append({"kind": "run_completed"})
        j.close()
        replayed = Journal.replay("r_outcome", tmp_journal_root)
        assert replayed.final_outcome() == "run_completed"

    def test_final_outcome_none_when_no_terminal(self, tmp_journal_root):
        j = Journal("r_no_terminal", tmp_journal_root)
        j.append({"kind": "run_started"})
        j.close()
        replayed = Journal.replay("r_no_terminal", tmp_journal_root)
        assert replayed.final_outcome() is None
