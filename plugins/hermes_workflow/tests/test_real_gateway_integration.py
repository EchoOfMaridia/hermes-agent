"""Integration test: visibility layer emits real StreamEvent instances.

This test exercises ``visibility.translate`` against the ACTUAL hermes
gateway ``StreamEvent`` types (``gateway.stream_events``). It verifies
that:

- Every journal kind produces a real ``StreamEvent`` (not a stub).
- The runtime instance is the correct subclass (ToolCallChunk,
  ToolCallFinished, GatewayNotice).
- The fields are populated correctly (tool_name, preview, args, index
  for ToolCallChunk; kind for GatewayNotice).

Run with the hermes repo on sys.path:

    cd /home/cage/Desktop/Workspaces/HermesDesktop
    python -m pytest plugins/hermes_workflow/tests/test_real_gateway_integration.py -v

The test is automatically skipped when the gateway module is unavailable
(e.g., when running outside the hermes repo). This makes it safe in
CI without breaking standalone plugin consumers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Skip the entire module when gateway.stream_events is not importable.
# The plugin is usable without hermes installed (e.g., as a standalone
# Python package) — the real-type integration is opt-in by environment.
HERMES_REPO = Path("/home/cage/Desktop/Workspaces/HermesDesktop")
if str(HERMES_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_REPO))

try:
    from gateway.stream_events import (
        GatewayNotice,
        LongToolHint,
        MessageChunk,
        StreamEvent,
        ToolCallChunk,
        ToolCallFinished,
    )
    HAS_REAL_STREAM_EVENTS = True
except ImportError:
    HAS_REAL_STREAM_EVENTS = False

pytestmark = pytest.mark.skipif(
    not HAS_REAL_STREAM_EVENTS,
    reason="gateway.stream_events not importable; real-type integration "
            "test skipped (run from inside the HermesDesktop repo)",
)


@pytest.fixture
def translator():
    """Construct a real-typed translator for each test."""
    from plugins.hermes_workflow.visibility import EventTranslator
    return EventTranslator()


class TestTranslateProducesRealTypes:
    """Each journal kind must yield a real StreamEvent subclass instance."""

    def test_run_started_is_real_gateway_notice(self, translator):
        ev = translator.translate({
            "kind": "run_started",
            "run_id": "r_test",
            "workflow": "demo",
            "ts": 100.0,
        })
        assert isinstance(ev, GatewayNotice)
        assert ev.kind == "workflow_run_started"
        # Real GatewayNotice carries run_id in extra (no top-level field).
        assert ev.extra.get("run_id") == "r_test"

    def test_step_started_is_real_tool_call_chunk(self, translator):
        ev = translator.translate({
            "kind": "step_started",
            "run_id": "r_test",
            "step": "audit_security",
            "attempt": 1,
            "ts": 100.1,
        })
        assert isinstance(ev, ToolCallChunk)
        assert ev.tool_name == "audit_security"
        # index starts at 0 for step_started; agent_call uses call_index.

    def test_step_completed_is_real_tool_call_finished(self, translator):
        ev = translator.translate({
            "kind": "step_completed",
            "run_id": "r_test",
            "step": "audit_security",
            "ok": True,
            "files_changed": ["audit.md"],
            "tests_run": 1,
            "tests_passed": 1,
            "duration_seconds": 0.1,
            "ts": 100.2,
        })
        assert isinstance(ev, ToolCallFinished)
        assert ev.tool_name == "audit_security"
        assert ev.ok is True

    def test_run_failed_is_real_gateway_notice(self, translator):
        ev = translator.translate({
            "kind": "run_failed",
            "run_id": "r_test",
            "reason": "verifier rejected",
            "ts": 100.5,
        })
        assert isinstance(ev, GatewayNotice)
        assert ev.kind == "workflow_run_failed"

    def test_agent_call_is_real_tool_call_chunk(self, translator):
        ev = translator.translate({
            "kind": "agent_call",
            "run_id": "r_test",
            "step": "review",
            "call_index": 2,
            "prompt_preview": "Review auth.py for bugs",
            "model": "sonnet",
            "ts": 100.0,
        })
        assert isinstance(ev, ToolCallChunk)
        # agent_call surfaces as tool_name="ask_agent"; the step name
        # is in args so adapters can render it.
        assert ev.tool_name == "ask_agent"
        assert ev.index == 2
        assert ev.args is not None
        assert ev.args["step"] == "review"
        assert ev.preview is not None
        assert "Review auth.py" in ev.preview

    def test_agent_response_is_real_tool_call_finished(self, translator):
        ev = translator.translate({
            "kind": "agent_response",
            "run_id": "r_test",
            "step": "review",
            "call_index": 2,
            "text_chars": 11,
            "text_preview": "Looks good.",
            "tokens_in": 120,
            "tokens_out": 30,
            "duration": 0.4,
            "ts": 100.4,
        })
        assert isinstance(ev, ToolCallFinished)
        # tool_name mirrors agent_call ("ask_agent") so adapter can pair.
        assert ev.tool_name == "ask_agent"
        assert ev.ok is True
        assert ev.index == 2
        # ToolCallFinished has no text field by design — output travels
        # to the agent's history, not the stream. Verify only the
        # stream-level fields.
        assert ev.duration == 0.4

    def test_unknown_kind_returns_none(self, translator):
        ev = translator.translate({"kind": "totally_made_up_kind"})
        assert ev is None

    def test_every_kind_subclass_of_streamevent(self, translator):
        """Sanity: every non-None translation is a StreamEvent subclass."""
        from plugins.hermes_workflow.journal import Journal
        all_kinds = [
            Journal.KIND_RUN_STARTED,
            Journal.KIND_RUN_COMPLETED,
            Journal.KIND_RUN_FAILED,
            Journal.KIND_RUN_HALTED,
            Journal.KIND_RUN_CANCELLED,
            Journal.KIND_STEP_STARTED,
            Journal.KIND_STEP_COMPLETED,
            Journal.KIND_STEP_FAILED,
            Journal.KIND_VERIFIER_RETURNED,
            Journal.KIND_AGENT_CALL,
            Journal.KIND_AGENT_RESPONSE,
        ]
        for kind in all_kinds:
            event = {"kind": kind, "run_id": "r", "ts": 0.0,
                     "step": "s", "workflow": "w",
                     "ok": True, "reason": "x",
                     "prompt": "p", "text": "t",
                     "call_index": 0}
            ev = translator.translate(event)
            if ev is not None:
                assert isinstance(ev, StreamEvent), (
                    f"kind {kind!r} produced {type(ev).__name__}, "
                    f"not a StreamEvent subclass"
                )


class TestRealGatewayAcceptsOurEvents:
    """Plug the visibility layer into a real GatewayEventDispatcher.

    This catches the failure mode where we produce events the
    dispatcher's ``_dispatch`` method would reject. Verifies the
    translate output is structurally compatible with the dispatcher's
    type guards (isinstance checks).
    """

    def test_translated_events_pass_dispatcher_type_guards(self, translator):
        from gateway.stream_dispatch import GatewayEventDispatcher

        # Construct a dispatcher with the minimum viable surface.
        # The base adapter has all the rendering hooks we need; we
        # pass None for the streaming sink because we are only testing
        # type-guard acceptance, not rendering.
        class _StubAdapter:
            def render_message_event(self, event, sink):
                pass

            def format_tool_event(self, event, **kwargs):
                return "stub"

        dispatcher = GatewayEventDispatcher(adapter=_StubAdapter(), sink=None)

        # Translate every kind and ensure _dispatch accepts each.
        kinds = ["run_started", "step_started", "step_completed",
                 "step_failed", "run_completed", "run_failed",
                 "agent_call", "agent_response"]
        for kind in kinds:
            event = {"kind": kind, "run_id": "r", "ts": 0.0,
                     "step": "s", "workflow": "w",
                     "ok": True, "reason": "x",
                     "prompt": "p", "text": "t",
                     "call_index": 0, "attempt": 1,
                     "files_changed": [], "tests_run": 0,
                     "tests_passed": 0, "duration_seconds": 0.1,
                     "model": "sonnet", "tokens_in": 0,
                     "tokens_out": 0}
            stream_event = translator.translate(event)
            if stream_event is not None:
                # The dispatcher's dispatch() catches all exceptions
                # silently, but we can call _dispatch directly to see
                # if the type guard passes.
                dispatcher._dispatch(stream_event)