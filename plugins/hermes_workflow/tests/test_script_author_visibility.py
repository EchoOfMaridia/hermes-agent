"""Tests for the script-author event path through EventTranslator.

The translator's regular ``translate()`` method handles journal events
emitted by the workflow runtime. ScriptAuthor events use a different
vocabulary — they're not journal entries, they're notifier payloads —
so they need a sibling translator method.

What we verify here:

- ``translate_script_author_event(("stage_started", {"stage": "llm_call"}))``
  → ``GatewayNotice(kind="script_author_stage_started", ...)``
- Same for ``stage_completed`` / ``stage_failed``
- ``("token", {"delta": "abc", "stage": "llm_call"})`` →
  ``ToolCallChunk(tool_name="script_author_llm", preview="abc", index=N)``
- ``("llm_completed", {...})`` → ``ToolCallFinished(tool_name="script_author_llm", ok=True)``
- ``("artifact_posted", {...})`` → ``LongToolHint(text=<script body>, extra=...)``
  with ``kind="script_author_artifact"``
- Index for ``ToolCallChunk`` is monotonic across calls
- Unknown kinds return ``None``
"""

from __future__ import annotations

import pytest

# Stub the gateway imports that EventTranslator needs.
import sys
import types


def _ensure_stream_event_stubs():
    """Import the StreamEvent types (or the test stubs EventTranslator
    falls back to). Tuple of (EventTranslator, ToolCallChunk,
    ToolCallFinished, GatewayNotice). LongToolHint is reserved for the
    runtime's long-tool nudges; script-author doesn't use it."""
    from plugins.hermes_workflow.visibility import (
        EventTranslator, ToolCallChunk, ToolCallFinished, GatewayNotice,
    )
    return EventTranslator, ToolCallChunk, ToolCallFinished, GatewayNotice


_ET, _TCC, _TCF, _GN = _ensure_stream_event_stubs()


class TestScriptAuthorEventTranslation:
    def test_stage_started_becomes_gateway_notice(self):
        tr = _ET()
        out = tr.translate_script_author_event(
            ("stage_started", {"stage": "llm_call"})
        )
        assert isinstance(out, _GN)
        assert out.kind == "script_author_stage_started"
        assert out.text == "llm_call"
        assert out.extra == {"stage": "llm_call",
                              "state": "started"}

    def test_stage_completed_becomes_gateway_notice(self):
        tr = _ET()
        out = tr.translate_script_author_event(
            ("stage_completed", {"stage": "llm_call", "ok": True})
        )
        assert isinstance(out, _GN)
        assert out.kind == "script_author_stage_completed"
        assert "llm_call" in out.text
        assert out.extra == {"stage": "llm_call", "state": "completed",
                              "ok": True}

    def test_stage_failed_becomes_gateway_notice(self):
        tr = _ET()
        out = tr.translate_script_author_event(
            ("stage_failed", {"stage": "safety_check",
                                "error": "subprocess term forbidden"})
        )
        assert isinstance(out, _GN)
        assert out.kind == "script_author_stage_failed"
        assert "safety_check" in out.text
        assert "subprocess term forbidden" in out.text
        assert out.extra["stage"] == "safety_check"
        assert out.extra["error"] == "subprocess term forbidden"

    def test_token_becomes_tool_call_chunk(self):
        tr = _ET()
        out = tr.translate_script_author_event(
            ("token", {"delta": "abc", "stage": "llm_call"})
        )
        assert isinstance(out, _TCC)
        assert out.tool_name == "script_author_llm"
        assert out.preview == "abc"
        assert out.index == 1
        assert out.args == {"stage": "llm_call"}

    def test_token_index_is_monotonic_across_chunks(self):
        tr = _ET()
        a = tr.translate_script_author_event(
            ("token", {"delta": "x", "stage": "llm_call"}))
        b = tr.translate_script_author_event(
            ("token", {"delta": "y", "stage": "llm_call"}))
        c = tr.translate_script_author_event(
            ("token", {"delta": "z", "stage": "llm_call"}))
        assert a.index == 1
        assert b.index == 2
        assert c.index == 3

    def test_llm_completed_becomes_tool_call_finished(self):
        tr = _ET()
        # First emit a chunk so the index is at 1.
        tr.translate_script_author_event(
            ("token", {"delta": "hi", "stage": "llm_call"}))
        out = tr.translate_script_author_event((
            "llm_completed",
            {"chars": 2, "text": "hi", "parsed": {"name": "demo"}},
        ))
        assert isinstance(out, _TCF)
        assert out.tool_name == "script_author_llm"
        assert out.ok is True
        assert out.index == 1

    def test_artifact_posted_becomes_gateway_notice(self):
        tr = _ET()
        out = tr.translate_script_author_event((
            "artifact_posted",
            {"name": "demo", "path": "/tmp/demo.py",
             "run_id": "za_demo_abcdef12",
             "body_preview": "from plugins.hermes_workflow import ..."},
        ))
        assert isinstance(out, _GN)
        assert out.kind == "script_author_artifact"
        assert out.extra["name"] == "demo"
        assert out.extra["path"] == "/tmp/demo.py"
        assert out.extra["run_id"] == "za_demo_abcdef12"
        assert "demo" in out.text
        assert "from plugins.hermes_workflow import" in out.text

    def test_unknown_kind_returns_none(self):
        tr = _ET()
        out = tr.translate_script_author_event(
            ("garbage_kind", {"x": 1})
        )
        assert out is None
